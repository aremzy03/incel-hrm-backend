import io

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, RoleName, UserRole
from apps.leave.models import PublicHoliday


User = get_user_model()


def make_role(name):
    return Role.objects.get(name=name)


def make_user(email, *, roles=None):
    user = User.objects.create_user(email=email, password="testpass123")
    roles = roles or []
    for r in roles:
        role = make_role(r)
        UserRole.objects.get_or_create(user=user, role=role)
    return user


class PublicHolidaysApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.hr = make_user("hr@example.com", roles=[RoleName.HR])
        self.emp = make_user("emp@example.com", roles=[RoleName.EMPLOYEE])

        self.list_url = reverse("public-holiday-list")
        self.upload_url = reverse("public-holiday-upload")

    def test_list_requires_auth(self):
        resp = self.client.get(self.list_url)
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_list_returns_holidays(self):
        PublicHoliday.objects.create(name="New Year", date="2026-01-01", is_recurring=True)
        self.client.force_authenticate(self.emp)
        resp = self.client.get(self.list_url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(resp.data), 1)

    def test_upload_requires_hr(self):
        self.client.force_authenticate(self.emp)
        csv_bytes = io.BytesIO(b"name,date\nNew Year,2026-01-01\n")
        resp = self.client.post(self.upload_url, {"file": csv_bytes}, format="multipart")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_upload_upserts_by_date(self):
        self.client.force_authenticate(self.hr)

        # Create
        csv_bytes = io.BytesIO(b"name,date\nNew Year,2026-01-01\n")
        resp = self.client.post(self.upload_url, {"file": csv_bytes}, format="multipart")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["created"], 1)
        self.assertEqual(resp.data["updated"], 0)
        self.assertEqual(PublicHoliday.objects.count(), 1)

        # Update same date
        csv_bytes2 = io.BytesIO(b"name,date\nNew Year Updated,2026-01-01\n")
        resp2 = self.client.post(self.upload_url, {"file": csv_bytes2}, format="multipart")
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        self.assertEqual(resp2.data["created"], 0)
        self.assertEqual(resp2.data["updated"], 1)

        holiday = PublicHoliday.objects.get(date="2026-01-01")
        self.assertEqual(holiday.name, "New Year Updated")

