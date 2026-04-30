from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Role, RoleName, UserRole

User = get_user_model()


def ensure_role(name: str) -> Role:
    role, _ = Role.objects.get_or_create(name=name, defaults={"description": name})
    return role


def make_user(email: str, *, password="testpass123", roles=None, is_staff=False):
    user = User.objects.create_user(email=email, password=password)
    user.is_staff = is_staff
    user.save(update_fields=["is_staff", "updated_at"])

    for role_name in roles or []:
        role = ensure_role(role_name)
        UserRole.objects.get_or_create(user=user, role=role)
    return user


class PasswordEndpointsTests(APITestCase):
    def setUp(self):
        self.old_password = "testpass123"
        self.new_password = "NewPassw0rd!234"

        ensure_role(RoleName.HR)
        ensure_role(RoleName.EMPLOYEE)

        self.employee = make_user(
            "employee@test.com",
            password=self.old_password,
            roles=[RoleName.EMPLOYEE],
        )
        self.hr = make_user(
            "hr@test.com",
            password=self.old_password,
            roles=[RoleName.HR],
        )
        self.admin = make_user(
            "admin@test.com",
            password=self.old_password,
            roles=[],
            is_staff=True,
        )

    def _auth(self, user):
        self.client.force_authenticate(user=user)

    def _mint_refresh_tokens(self, user, n=2):
        # Creating RefreshToken should create OutstandingToken rows when blacklist app is enabled.
        from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
        from rest_framework_simplejwt.tokens import RefreshToken

        existing = OutstandingToken.objects.filter(user=user).count()
        for _ in range(n):
            RefreshToken.for_user(user)
        return existing

    def test_password_change_success_blacklists_refresh_tokens(self):
        from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

        self._auth(self.employee)
        url = reverse("password-change")

        before_outstanding = self._mint_refresh_tokens(self.employee, n=2)
        outstanding = OutstandingToken.objects.filter(user=self.employee)
        self.assertGreaterEqual(outstanding.count(), before_outstanding + 2)

        resp = self.client.post(
            url,
            {
                "current_password": self.old_password,
                "new_password": self.new_password,
                "new_password_confirm": self.new_password,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT, msg=getattr(resp, "data", None))

        self.employee.refresh_from_db()
        self.assertTrue(self.employee.check_password(self.new_password))

        # All outstanding refresh tokens should be blacklisted for this user.
        self.assertEqual(
            BlacklistedToken.objects.filter(token__user=self.employee).count(),
            OutstandingToken.objects.filter(user=self.employee).count(),
        )

    def test_password_change_rejects_wrong_current_password(self):
        self._auth(self.employee)
        url = reverse("password-change")

        resp = self.client.post(
            url,
            {
                "current_password": "wrong-password",
                "new_password": self.new_password,
                "new_password_confirm": self.new_password,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("current_password", resp.data)

        self.employee.refresh_from_db()
        self.assertTrue(self.employee.check_password(self.old_password))

    def test_password_reset_requires_hr_or_admin(self):
        self._auth(self.employee)
        url = reverse("user-password-reset", args=[self.employee.id])

        resp = self.client.post(
            url,
            {"new_password": self.new_password, "new_password_confirm": self.new_password},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_password_reset_success_blacklists_target_refresh_tokens(self):
        from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

        self._auth(self.hr)
        url = reverse("user-password-reset", args=[self.employee.id])

        before_outstanding = self._mint_refresh_tokens(self.employee, n=2)
        self.assertGreaterEqual(
            OutstandingToken.objects.filter(user=self.employee).count(),
            before_outstanding + 2,
        )

        resp = self.client.post(
            url,
            {"new_password": self.new_password, "new_password_confirm": self.new_password},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT, msg=getattr(resp, "data", None))

        self.employee.refresh_from_db()
        self.assertTrue(self.employee.check_password(self.new_password))

        self.assertEqual(
            BlacklistedToken.objects.filter(token__user=self.employee).count(),
            OutstandingToken.objects.filter(user=self.employee).count(),
        )

    def test_password_reset_allows_django_admin_staff(self):
        self._auth(self.admin)
        url = reverse("user-password-reset", args=[self.employee.id])

        resp = self.client.post(
            url,
            {"new_password": self.new_password, "new_password_confirm": self.new_password},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT, msg=getattr(resp, "data", None))

