from datetime import timedelta
from urllib.parse import parse_qs, urlparse

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import MagicLinkToken
from apps.accounts.services import (
    MagicLinkError,
    build_magic_link_url,
    create_magic_link_token,
    exchange_magic_link_token,
    inspect_magic_link_token,
)

User = get_user_model()


class MagicLinkServiceTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="manager@example.com",
            password="testpass123",
            first_name="Jane",
            last_name="Manager",
        )

    def test_build_magic_link_url_format(self):
        url = build_magic_link_url(
            user=self.user,
            destination_path="/leave/requests/123",
        )
        self.assertTrue(url.startswith("http://localhost:3000/auth/magic-link"))

        parsed = urlparse(url)
        params = parse_qs(parsed.query)

        self.assertIn("token", params)
        self.assertIn("next", params)
        self.assertIn("email", params)
        self.assertEqual(params["next"][0], "/leave/requests/123")
        self.assertEqual(params["email"][0], self.user.email)

        # Check DB record exists and is unconsumed
        db_token = MagicLinkToken.objects.get(user=self.user)
        self.assertIsNone(db_token.consumed_at)
        self.assertFalse(db_token.is_expired)
        self.assertTrue(db_token.is_valid)

    def test_inspect_magic_link_token(self):
        raw_token, _ = create_magic_link_token(
            user=self.user,
            destination_path="/leave/requests/456",
        )

        info = inspect_magic_link_token(raw_token)
        self.assertEqual(info["email"], self.user.email)
        self.assertEqual(info["destination_path"], "/leave/requests/456")
        self.assertTrue(info["is_valid"])
        self.assertFalse(info["is_expired"])
        self.assertFalse(info["is_consumed"])

        # Token still valid and unconsumed
        db_token = MagicLinkToken.objects.get(user=self.user)
        self.assertIsNone(db_token.consumed_at)

    def test_inspect_invalid_token(self):
        with self.assertRaises(MagicLinkError) as ctx:
            inspect_magic_link_token("non-existent-token")
        self.assertEqual(ctx.exception.code, "INVALID_TOKEN")

    def test_exchange_magic_link_token_success(self):
        raw_token, _ = create_magic_link_token(
            user=self.user,
            destination_path="/loans/requests/789",
        )

        user, dest, tokens = exchange_magic_link_token(
            raw_token=raw_token,
            ip="192.168.1.100",
            user_agent="TestAgent/1.0",
        )

        self.assertEqual(user, self.user)
        self.assertEqual(dest, "/loans/requests/789")
        self.assertIn("access", tokens)
        self.assertIn("refresh", tokens)

        # DB record updated
        db_token = MagicLinkToken.objects.get(user=self.user)
        self.assertIsNotNone(db_token.consumed_at)
        self.assertEqual(db_token.consumed_by_ip, "192.168.1.100")
        self.assertEqual(db_token.consumed_user_agent, "TestAgent/1.0")

    def test_exchange_single_use_enforcement(self):
        raw_token, _ = create_magic_link_token(
            user=self.user,
            destination_path="/leave/requests/123",
        )

        # First exchange succeeds
        exchange_magic_link_token(raw_token)

        # Second exchange fails
        with self.assertRaises(MagicLinkError) as ctx:
            exchange_magic_link_token(raw_token)
        self.assertEqual(ctx.exception.code, "TOKEN_ALREADY_USED")

    def test_exchange_expired_token(self):
        raw_token, token_obj = create_magic_link_token(
            user=self.user,
            destination_path="/leave/requests/123",
            expires_in=timedelta(hours=-1),  # Already expired
        )

        with self.assertRaises(MagicLinkError) as ctx:
            exchange_magic_link_token(raw_token)
        self.assertEqual(ctx.exception.code, "TOKEN_EXPIRED")

    def test_exchange_inactive_user(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])

        raw_token, _ = create_magic_link_token(
            user=self.user,
            destination_path="/leave/requests/123",
        )

        with self.assertRaises(MagicLinkError) as ctx:
            exchange_magic_link_token(raw_token)
        self.assertEqual(ctx.exception.code, "USER_INACTIVE")


class MagicLinkEndpointAPITests(APITestCase):
    def setUp(self):
        cache.clear()
        self.exchange_url = reverse("magic-link-exchange")
        self.inspect_url = reverse("magic-link-inspect")
        self.user = User.objects.create_user(
            email="employee@example.com",
            password="testpass123",
            first_name="John",
            last_name="Employee",
        )

    def test_exchange_api_success(self):
        raw_token, _ = create_magic_link_token(
            user=self.user,
            destination_path="/leave/requests/abc-123",
        )

        response = self.client.post(
            self.exchange_url,
            {"token": raw_token},
            format="json",
            HTTP_USER_AGENT="Mozilla/5.0 (Macintosh)",
            REMOTE_ADDR="127.0.0.1",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)
        self.assertEqual(response.data["destination_path"], "/leave/requests/abc-123")
        self.assertEqual(response.data["user"]["email"], self.user.email)

    def test_exchange_api_already_used(self):
        raw_token, _ = create_magic_link_token(
            user=self.user,
            destination_path="/leave/requests/abc-123",
        )

        # First exchange
        resp1 = self.client.post(self.exchange_url, {"token": raw_token}, format="json")
        self.assertEqual(resp1.status_code, status.HTTP_200_OK)

        # Second exchange
        resp2 = self.client.post(self.exchange_url, {"token": raw_token}, format="json")
        self.assertEqual(resp2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp2.data["code"], "TOKEN_ALREADY_USED")

    def test_exchange_api_expired(self):
        raw_token, _ = create_magic_link_token(
            user=self.user,
            destination_path="/leave/requests/abc-123",
            expires_in=timedelta(hours=-1),
        )

        response = self.client.post(self.exchange_url, {"token": raw_token}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["code"], "TOKEN_EXPIRED")

    def test_exchange_api_invalid_token(self):
        response = self.client.post(
            self.exchange_url,
            {"token": "invalid_fake_token"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["code"], "INVALID_TOKEN")

    def test_inspect_api_success(self):
        raw_token, _ = create_magic_link_token(
            user=self.user,
            destination_path="/leave/requests/inspect-target",
        )

        response = self.client.get(f"{self.inspect_url}?token={raw_token}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["email"], self.user.email)
        self.assertEqual(response.data["destination_path"], "/leave/requests/inspect-target")
        self.assertTrue(response.data["is_valid"])

    def test_inspect_api_invalid(self):
        response = self.client.get(f"{self.inspect_url}?token=unknown-token")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["code"], "INVALID_TOKEN")

