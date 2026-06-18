from django.conf import settings
from django.core.cache import cache
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

User = get_user_model()

THROTTLE_SETTINGS = {
    **settings.REST_FRAMEWORK,
    "DEFAULT_THROTTLE_RATES": {
        **settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"],
        "login": "3/minute",
    },
}

LOC_MEM_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}


@override_settings(REST_FRAMEWORK=THROTTLE_SETTINGS, CACHES=LOC_MEM_CACHE)
class LoginThrottleTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.login_url = reverse("token-obtain-pair")
        self.user_a = User.objects.create_user(
            email="alice@example.com",
            password="testpass123",
        )
        self.user_b = User.objects.create_user(
            email="bob@example.com",
            password="testpass123",
        )

    def _login(self, email, password="wrong"):
        return self.client.post(
            self.login_url,
            {"email": email, "password": password},
            format="json",
        )

    def test_throttle_is_per_email_not_per_ip(self):
        for _ in range(3):
            response = self._login(self.user_a.email)
            self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        throttled = self._login(self.user_a.email)
        self.assertEqual(throttled.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertIn("throttled", throttled.data["detail"].lower())

        other_user = self._login(self.user_b.email)
        self.assertEqual(other_user.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_throttle_normalizes_email_case(self):
        for _ in range(3):
            self._login("Alice@Example.com")

        throttled = self._login("alice@example.com")
        self.assertEqual(throttled.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_successful_login_counts_toward_limit(self):
        for _ in range(3):
            response = self.client.post(
                self.login_url,
                {"email": self.user_a.email, "password": "testpass123"},
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)

        throttled = self._login(self.user_a.email)
        self.assertEqual(throttled.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
