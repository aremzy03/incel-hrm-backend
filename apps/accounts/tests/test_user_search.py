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


class UserSearchTests(APITestCase):
    def setUp(self):
        for role_name in (RoleName.EMPLOYEE, RoleName.HR):
            ensure_role(role_name)

        self.password = "testpass123"
        self.hr_dept = Department.objects.get_or_create(name="Human Resources (HR)")[0]
        self.sales = Department.objects.get_or_create(name="Sales")[0]

        self.hr = make_user(
            "hr@test.com",
            password=self.password,
            roles=[RoleName.HR],
            department=self.hr_dept,
            first_name="Hannah",
            last_name="Recruiter",
        )

        self.alice = make_user(
            "alice@example.com",
            password=self.password,
            roles=[RoleName.EMPLOYEE],
            department=self.sales,
            first_name="Alice",
            last_name="Johnson",
            other_names="Marie",
            phone="08012340000",
        )
        self.bob = make_user(
            "bob@example.com",
            password=self.password,
            roles=[RoleName.EMPLOYEE],
            department=self.sales,
            first_name="Bob",
            last_name="Smith",
            other_names="",
            phone="08099990000",
        )

    def _auth(self, user):
        self.client.force_authenticate(user=user)

    def _results(self, resp):
        # Default pagination is enabled (PageNumberPagination).
        return resp.data.get("results", resp.data)

    def test_users_search_by_email(self):
        self._auth(self.hr)
        url = reverse("user-list")
        resp = self.client.get(url, {"search": "alice@"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, msg=f"{resp.data}")
        ids = {row["id"] for row in self._results(resp)}
        self.assertIn(str(self.alice.id), ids)
        self.assertNotIn(str(self.bob.id), ids)

    def test_users_search_by_name(self):
        self._auth(self.hr)
        url = reverse("user-list")
        resp = self.client.get(url, {"search": "john"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, msg=f"{resp.data}")
        ids = {row["id"] for row in self._results(resp)}
        self.assertIn(str(self.alice.id), ids)
        self.assertNotIn(str(self.bob.id), ids)

    def test_users_search_by_department_name(self):
        self._auth(self.hr)
        url = reverse("user-list")
        resp = self.client.get(url, {"search": "sales"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, msg=f"{resp.data}")
        ids = {row["id"] for row in self._results(resp)}
        self.assertIn(str(self.alice.id), ids)
        self.assertIn(str(self.bob.id), ids)
        self.assertNotIn(str(self.hr.id), ids)

