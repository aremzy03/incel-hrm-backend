from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import (
    Department,
    DepartmentMembership,
    MANAGEMENT_DEPARTMENT_NAME,
    Role,
    RoleName,
    UserRole,
    get_or_create_management_department,
)


User = get_user_model()


def ensure_role(name: str) -> Role:
    role, _ = Role.objects.get_or_create(name=name, defaults={"description": name})
    return role


def make_user(email: str, *, password="testpass123", roles=None, department=None, is_staff=False):
    user = User.objects.create_user(email=email, password=password)
    user.is_staff = is_staff
    if department is not None:
        user.department = department
    user.save(update_fields=["is_staff", "department", "updated_at"])

    for role_name in roles or []:
        role = ensure_role(role_name)
        UserRole.objects.get_or_create(user=user, role=role)
    return user


class DepartmentMembersCountTests(APITestCase):
    def setUp(self):
        # Ensure core roles exist (some signal paths depend on Role rows existing).
        for role_name in (RoleName.EMPLOYEE, RoleName.HR, RoleName.LINE_MANAGER):
            ensure_role(role_name)

        self.password = "testpass123"
        self.authed_user = make_user("auth@test.com", password=self.password, roles=[RoleName.EMPLOYEE])

        self.dept_a = Department.objects.get_or_create(name="Dept A")[0]
        self.dept_b = Department.objects.get_or_create(name="Dept B")[0]

        self.user_a1 = make_user("a1@test.com", password=self.password, roles=[RoleName.EMPLOYEE], department=self.dept_a)
        self.user_a2 = make_user("a2@test.com", password=self.password, roles=[RoleName.EMPLOYEE], department=self.dept_a)
        self.user_b1 = make_user("b1@test.com", password=self.password, roles=[RoleName.EMPLOYEE], department=self.dept_b)

        self.mgmt = get_or_create_management_department()
        assert self.mgmt.name == MANAGEMENT_DEPARTMENT_NAME

        # Ensure management membership is counted via DepartmentMembership (not User.department).
        DepartmentMembership.objects.get_or_create(user=self.user_a1, department=self.mgmt)
        DepartmentMembership.objects.get_or_create(user=self.user_b1, department=self.mgmt)

    def test_departments_list_includes_members_count(self):
        self.client.force_authenticate(user=self.authed_user)

        url = reverse("department-list")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, msg=f"{resp.data}")

        # Default pagination is enabled; departments are in resp.data["results"].
        results = resp.data.get("results", resp.data)
        by_name = {d["name"]: d for d in results}

        self.assertIn("members_count", by_name["Dept A"])
        self.assertEqual(by_name["Dept A"]["members_count"], 2)
        self.assertEqual(by_name["Dept B"]["members_count"], 1)
        self.assertEqual(by_name[MANAGEMENT_DEPARTMENT_NAME]["members_count"], 2)

