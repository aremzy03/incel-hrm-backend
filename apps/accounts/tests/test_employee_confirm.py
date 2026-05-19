from datetime import date

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import ConfirmationStatus, Department, Role, RoleName, UserRole


User = get_user_model()


def ensure_role(name: str) -> Role:
    role, _ = Role.objects.get_or_create(name=name, defaults={"description": name})
    return role


def make_user(email: str, *, password="testpass123", roles=None, department=None, **extra):
    user = User.objects.create_user(email=email, password=password, **extra)
    if department is not None:
        user.department = department
        user.save(update_fields=["department", "updated_at"])

    for role_name in roles or []:
        role = ensure_role(role_name)
        UserRole.objects.get_or_create(user=user, role=role)
    return user


class EmployeeConfirmTests(APITestCase):
    def setUp(self):
        for role_name in (RoleName.EMPLOYEE, RoleName.HR):
            ensure_role(role_name)

        self.password = "testpass123"
        self.hr_dept = Department.objects.get_or_create(name="Human Resources (HR)")[0]
        self.sales = Department.objects.get_or_create(name="Sales")[0]

        self.hr = make_user(
            "hr-confirm@test.com",
            password=self.password,
            roles=[RoleName.HR],
            department=self.hr_dept,
        )

        self.employee = make_user(
            "employee-confirm@example.com",
            password=self.password,
            roles=[RoleName.EMPLOYEE],
            department=self.sales,
            confirmation_status=ConfirmationStatus.PENDING,
        )

    def _confirm_url(self, user_id):
        return reverse("employee-confirm", kwargs={"pk": str(user_id)})

    def _personnel_url(self, user_id):
        return reverse("user-personnel-detail", kwargs={"user_id": str(user_id)})

    def test_hr_confirm_sets_status_and_date(self):
        self.client.force_authenticate(user=self.hr)
        resp = self.client.patch(self._confirm_url(self.employee.id), {}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK, msg=resp.data)
        self.assertEqual(resp.data["confirmation_status"], ConfirmationStatus.CONFIRMED)
        self.assertEqual(resp.data["confirmation_date"], timezone.localdate().isoformat())
        self.assertTrue(resp.data["is_confirmed"])
        self.assertEqual(resp.data["confirmed_date"], resp.data["confirmation_date"])

        self.employee.refresh_from_db()
        self.assertEqual(self.employee.confirmation_status, ConfirmationStatus.CONFIRMED)
        self.assertEqual(self.employee.confirmation_date, timezone.localdate())

    def test_hr_confirm_is_idempotent(self):
        self.client.force_authenticate(user=self.hr)
        yesterday = date(2020, 1, 1)
        self.employee.confirmation_status = ConfirmationStatus.CONFIRMED
        self.employee.confirmation_date = yesterday
        self.employee.save(update_fields=["confirmation_status", "confirmation_date", "updated_at"])

        resp = self.client.patch(self._confirm_url(self.employee.id), {}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK, msg=resp.data)
        self.assertEqual(resp.data["confirmation_date"], timezone.localdate().isoformat())

        self.employee.refresh_from_db()
        self.assertEqual(self.employee.confirmation_date, timezone.localdate())

    def test_employee_cannot_confirm(self):
        self.client.force_authenticate(user=self.employee)
        resp = self.client.patch(self._confirm_url(self.employee.id), {}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_employee_cannot_set_confirmation_via_personnel_patch(self):
        self.client.force_authenticate(user=self.employee)
        resp = self.client.patch(
            self._personnel_url(self.employee.id),
            {"confirmation_status": ConfirmationStatus.CONFIRMED},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.confirmation_status, ConfirmationStatus.PENDING)

    def test_hr_can_set_confirmation_via_personnel_patch(self):
        self.client.force_authenticate(user=self.hr)
        resp = self.client.patch(
            self._personnel_url(self.employee.id),
            {"confirmation_status": ConfirmationStatus.PENDING, "confirmation_date": None},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, msg=resp.data)
        self.assertEqual(resp.data["confirmation_status"], ConfirmationStatus.PENDING)
        self.assertIsNone(resp.data["confirmation_date"])
        self.assertFalse(resp.data["is_confirmed"])
