from datetime import date

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Department, Role, RoleName, UserRole


User = get_user_model()


def ensure_role(name: str) -> Role:
    role, _ = Role.objects.get_or_create(name=name, defaults={"description": name})
    return role


def make_user(email: str, *, password="testpass123", roles=None, department=None, is_staff=False, **extra):
    user = User.objects.create_user(email=email, password=password, **extra)
    user.is_staff = is_staff
    if department is not None:
        user.department = department
    user.save(update_fields=["is_staff", "department", "updated_at"])

    for role_name in roles or []:
        role = ensure_role(role_name)
        UserRole.objects.get_or_create(user=user, role=role)
    return user


class UserPersonnelDetailTests(APITestCase):
    def setUp(self):
        for role_name in (RoleName.EMPLOYEE, RoleName.HR):
            ensure_role(role_name)

        self.password = "testpass123"
        self.hr_dept = Department.objects.get_or_create(name="Human Resources (HR)")[0]
        self.sales = Department.objects.get_or_create(name="Sales")[0]

        self.hr = make_user(
            "hr-personnel@test.com",
            password=self.password,
            roles=[RoleName.HR],
            department=self.hr_dept,
        )

        self.alice = make_user(
            "alice-personnel@example.com",
            password=self.password,
            roles=[RoleName.EMPLOYEE],
            department=self.sales,
            first_name="Alice",
            last_name="Lee",
            date_of_birth=date(1990, 5, 10),
        )

        self.bob = make_user(
            "bob-personnel@example.com",
            password=self.password,
            roles=[RoleName.EMPLOYEE],
            department=self.sales,
        )

    def _url(self, user_id):
        return reverse("user-personnel-detail", kwargs={"user_id": str(user_id)})

    def test_owner_get_personnel(self):
        self.client.force_authenticate(user=self.alice)
        resp = self.client.get(self._url(self.alice.id))
        self.assertEqual(resp.status_code, status.HTTP_200_OK, msg=resp.data)
        self.assertEqual(resp.data["email"], "alice-personnel@example.com")
        self.assertEqual(resp.data["official_email"], "alice-personnel@example.com")
        self.assertEqual(resp.data["first_name"], "Alice")
        born = date(1990, 5, 10)
        today = date.today()
        expected_age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
        self.assertEqual(resp.data["age"], expected_age)
        self.assertIn("completeness_score", resp.data)

    def test_owner_cannot_read_other_personnel(self):
        self.client.force_authenticate(user=self.alice)
        resp = self.client.get(self._url(self.bob.id))
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_hr_can_read_any_personnel(self):
        self.client.force_authenticate(user=self.hr)
        resp = self.client.get(self._url(self.alice.id))
        self.assertEqual(resp.status_code, status.HTTP_200_OK, msg=resp.data)
        self.assertEqual(resp.data["email"], "alice-personnel@example.com")

    def test_owner_patch_updates_and_returns_read_shape(self):
        self.client.force_authenticate(user=self.alice)
        before = self.client.get(self._url(self.alice.id)).data["completeness_score"]
        resp = self.client.patch(
            self._url(self.alice.id),
            {"job_role": "Analyst", "staff_id": "STF-001", "state_of_origin": "Lagos", "lga": "Ikeja"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, msg=resp.data)
        self.assertEqual(resp.data["job_role"], "Analyst")
        self.assertEqual(resp.data["staff_id"], "STF-001")
        self.assertGreaterEqual(resp.data["completeness_score"], before)

    def test_hr_can_patch_other_user(self):
        self.client.force_authenticate(user=self.hr)
        resp = self.client.patch(
            self._url(self.bob.id),
            {"job_role": "Engineer", "qualification_school": "UNILAG", "qualification_degree": "B.Sc."},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, msg=resp.data)
        self.assertEqual(resp.data["job_role"], "Engineer")
        self.assertEqual(resp.data["qualification_school"], "UNILAG")

    def test_length_of_service_when_employment_set(self):
        self.client.force_authenticate(user=self.hr)
        resp = self.client.patch(
            self._url(self.alice.id),
            {"date_of_employment": "2020-01-15"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, msg=resp.data)
        self.assertIsNotNone(resp.data["length_of_service_years"])
        self.assertGreater(resp.data["length_of_service_years"], 0)
