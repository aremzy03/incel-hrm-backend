from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Department, DepartmentMembership, Role, RoleName, Team, Unit, UserRole


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


class BulkMembershipEndpointsTests(APITestCase):
    def setUp(self):
        self.password = "testpass123"

        # Ensure core roles exist for permissions checks.
        for role_name in (
            RoleName.EMPLOYEE,
            RoleName.HR,
            RoleName.LINE_MANAGER,
            RoleName.EXECUTIVE_DIRECTOR,
            RoleName.MANAGING_DIRECTOR,
        ):
            ensure_role(role_name)

        self.dept_a = Department.objects.get_or_create(name="Dept A")[0]
        self.dept_b = Department.objects.get_or_create(name="Dept B")[0]

        self.hr = make_user(
            "hr@test.com",
            password=self.password,
            roles=[RoleName.HR],
            department=Department.objects.get_or_create(name="Human Resources (HR)")[0],
            is_staff=False,
        )
        self.staff_admin = make_user(
            "admin@test.com",
            password=self.password,
            roles=[],
            department=None,
            is_staff=True,
        )

        self.line_manager_a = make_user(
            "lm_a@test.com",
            password=self.password,
            roles=[RoleName.LINE_MANAGER],
            department=self.dept_a,
        )
        self.dept_a.line_manager = self.line_manager_a
        self.dept_a.save(update_fields=["line_manager", "updated_at"])

        self.unit_a1 = Unit.objects.create(name="Unit A1", department=self.dept_a)
        self.unit_b1 = Unit.objects.create(name="Unit B1", department=self.dept_b)

        self.team_a1 = Team.objects.create(name="Team A1", unit=self.unit_a1)

        self.user_in_a = make_user(
            "user_a@test.com",
            password=self.password,
            roles=[RoleName.EMPLOYEE],
            department=self.dept_a,
        )
        self.user_in_a.unit = self.unit_a1
        self.user_in_a.save(update_fields=["unit", "updated_at"])

        self.user_in_b = make_user(
            "user_b@test.com",
            password=self.password,
            roles=[RoleName.EMPLOYEE],
            department=self.dept_b,
        )
        self.user_in_b.unit = self.unit_b1
        self.user_in_b.save(update_fields=["unit", "updated_at"])

    def _auth(self, user):
        self.client.force_authenticate(user=user)

    def test_department_bulk_add_sets_department_and_creates_membership_partial(self):
        self._auth(self.hr)

        url = reverse("department-bulk-add-members", args=[self.dept_a.id])
        payload = {
            "user_ids": [str(self.user_in_a.id), "00000000-0000-0000-0000-000000000000"],
        }
        resp = self.client.post(url, payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn(str(self.user_in_a.id), resp.data["succeeded_user_ids"])
        self.assertEqual(len(resp.data["failed"]), 1)
        self.assertEqual(resp.data["failed"][0]["code"], "not_found")

        self.user_in_a.refresh_from_db()
        self.assertEqual(self.user_in_a.department_id, self.dept_a.id)
        self.assertTrue(
            DepartmentMembership.objects.filter(user=self.user_in_a, department=self.dept_a).exists()
        )

    def test_department_bulk_add_rejects_conflicting_unit_without_clear_conflicts(self):
        self._auth(self.hr)

        url = reverse("department-bulk-add-members", args=[self.dept_a.id])
        payload = {"user_ids": [str(self.user_in_b.id)]}
        resp = self.client.post(url, payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["succeeded_user_ids"], [])
        self.assertEqual(resp.data["failed"][0]["code"], "department_conflict")

    def test_unit_bulk_add_allows_line_manager_and_enforces_department(self):
        self._auth(self.line_manager_a)

        url = reverse("unit-bulk-add-members", args=[self.unit_a1.id])
        payload = {"user_ids": [str(self.user_in_a.id)]}
        resp = self.client.post(url, payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["failed"], [])

        self.user_in_a.refresh_from_db()
        self.assertEqual(self.user_in_a.unit_id, self.unit_a1.id)
        self.assertEqual(self.user_in_a.department_id, self.dept_a.id)

        # User from Dept B should fail without clear_conflicts
        payload2 = {"user_ids": [str(self.user_in_b.id)]}
        resp2 = self.client.post(url, payload2, format="json")
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        self.assertEqual(resp2.data["succeeded_user_ids"], [])
        self.assertEqual(resp2.data["failed"][0]["code"], "department_conflict")

    def test_unit_bulk_add_can_move_user_with_clear_conflicts(self):
        self._auth(self.line_manager_a)

        url = reverse("unit-bulk-add-members", args=[self.unit_a1.id])
        payload = {"user_ids": [str(self.user_in_b.id)], "clear_conflicts": True}
        resp = self.client.post(url, payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["failed"], [])

        self.user_in_b.refresh_from_db()
        self.assertEqual(self.user_in_b.department_id, self.dept_a.id)
        self.assertEqual(self.user_in_b.unit_id, self.unit_a1.id)

    def test_team_bulk_add_requires_same_unit(self):
        self._auth(self.hr)

        url = reverse("team-bulk-add-members", args=[self.team_a1.id])
        payload = {"user_ids": [str(self.user_in_a.id), str(self.user_in_b.id)]}
        resp = self.client.post(url, payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        self.assertIn(str(self.user_in_a.id), resp.data["succeeded_user_ids"])
        self.assertEqual(len(resp.data["failed"]), 1)
        self.assertEqual(resp.data["failed"][0]["code"], "unit_mismatch")

        self.user_in_a.refresh_from_db()
        self.assertEqual(self.user_in_a.team_id, self.team_a1.id)

    def test_user_department_update_allows_null_to_clear_when_no_unit_or_team(self):
        self._auth(self.hr)

        # Ensure user has no unit/team, then clear department.
        self.user_in_a.unit = None
        self.user_in_a.team = None
        self.user_in_a.save(update_fields=["unit", "team", "updated_at"])

        url = reverse("user-department-update", args=[self.user_in_a.id])
        resp = self.client.patch(url, {"department": None}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK, msg=f"{resp.data}")

        self.user_in_a.refresh_from_db()
        self.assertIsNone(self.user_in_a.department_id)

    def test_department_bulk_remove_clears_department_unit_team_and_membership_partial(self):
        self._auth(self.hr)

        # Put user_in_a into dept_a + unit_a1 + team_a1 and add membership
        self.user_in_a.department = self.dept_a
        self.user_in_a.unit = self.unit_a1
        self.user_in_a.team = self.team_a1
        self.user_in_a.save(update_fields=["department", "unit", "team", "updated_at"])
        DepartmentMembership.objects.get_or_create(user=self.user_in_a, department=self.dept_a)

        url = reverse("department-bulk-remove-members", args=[self.dept_a.id])
        payload = {"user_ids": [str(self.user_in_a.id), str(self.user_in_b.id)]}
        resp = self.client.post(url, payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn(str(self.user_in_a.id), resp.data["succeeded_user_ids"])
        self.assertEqual(len(resp.data["failed"]), 1)
        self.assertEqual(resp.data["failed"][0]["code"], "not_in_department")

        self.user_in_a.refresh_from_db()
        self.assertIsNone(self.user_in_a.department_id)
        self.assertIsNone(self.user_in_a.unit_id)
        self.assertIsNone(self.user_in_a.team_id)
        self.assertFalse(
            DepartmentMembership.objects.filter(user=self.user_in_a, department=self.dept_a).exists()
        )

    def test_unit_bulk_remove_clears_unit_and_team(self):
        self._auth(self.line_manager_a)

        # Put user_in_a into unit_a1 + team_a1
        self.user_in_a.unit = self.unit_a1
        self.user_in_a.team = self.team_a1
        self.user_in_a.save(update_fields=["unit", "team", "updated_at"])

        url = reverse("unit-bulk-remove-members", args=[self.unit_a1.id])
        resp = self.client.post(url, {"user_ids": [str(self.user_in_a.id)]}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["failed"], [])

        self.user_in_a.refresh_from_db()
        self.assertIsNone(self.user_in_a.unit_id)
        self.assertIsNone(self.user_in_a.team_id)

    def test_team_bulk_remove_clears_team_only_and_requires_membership(self):
        self._auth(self.hr)

        self.user_in_a.unit = self.unit_a1
        self.user_in_a.team = self.team_a1
        self.user_in_a.save(update_fields=["unit", "team", "updated_at"])

        url = reverse("team-bulk-remove-members", args=[self.team_a1.id])
        resp = self.client.post(url, {"user_ids": [str(self.user_in_a.id), str(self.user_in_b.id)]}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn(str(self.user_in_a.id), resp.data["succeeded_user_ids"])
        self.assertEqual(len(resp.data["failed"]), 1)
        self.assertEqual(resp.data["failed"][0]["code"], "not_in_team")

        self.user_in_a.refresh_from_db()
        self.assertIsNone(self.user_in_a.team_id)

    def test_line_manager_role_grant_adds_user_to_management_department(self):
        user = make_user(
            "lm.membership@test.com",
            password=self.password,
            roles=[],
            department=self.dept_a,
        )
        lm_role = ensure_role(RoleName.LINE_MANAGER)
        UserRole.objects.create(user=user, role=lm_role)

        from apps.accounts.models import get_or_create_management_department

        mgmt_dept = get_or_create_management_department()
        self.assertTrue(
            DepartmentMembership.objects.filter(user=user, department=mgmt_dept).exists()
        )

    def test_department_line_manager_delete_reverts_role_and_removes_management_membership(self):
        from apps.accounts.models import get_or_create_management_department

        # Assign line manager
        self._auth(self.hr)
        url = reverse("department-line-manager", args=[self.dept_a.id])
        resp = self.client.post(url, {"user_id": str(self.user_in_a.id)}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        self.assertTrue(self.user_in_a.has_role(RoleName.LINE_MANAGER))
        mgmt = get_or_create_management_department()
        self.assertTrue(DepartmentMembership.objects.filter(user=self.user_in_a, department=mgmt).exists())

        # Remove line manager
        resp2 = self.client.delete(url)
        self.assertEqual(resp2.status_code, status.HTTP_204_NO_CONTENT)

        self.user_in_a.refresh_from_db()
        self.assertFalse(self.user_in_a.has_role(RoleName.LINE_MANAGER))
        # Multi-role behavior: revocation removes only LINE_MANAGER; it does not force EMPLOYEE.
        # (This user started as EMPLOYEE in setup, so they still have it.)
        self.assertTrue(self.user_in_a.has_role(RoleName.EMPLOYEE))
        self.assertFalse(DepartmentMembership.objects.filter(user=self.user_in_a, department=mgmt).exists())

    def test_unit_supervisor_assign_and_revoke_updates_role(self):
        self._auth(self.line_manager_a)

        supervisor = make_user(
            "unit.supervisor@test.com",
            password=self.password,
            roles=[],
            department=self.dept_a,
        )

        url = reverse("unit-supervisor", args=[self.unit_a1.id])
        resp = self.client.post(url, {"user_id": str(supervisor.id)}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK, msg=f"{resp.data}")

        supervisor.refresh_from_db()
        self.assertTrue(supervisor.has_role(RoleName.SUPERVISOR))

        resp2 = self.client.delete(url)
        self.assertEqual(resp2.status_code, status.HTTP_200_OK, msg=f"{resp2.data}")

        supervisor.refresh_from_db()
        self.assertFalse(supervisor.has_role(RoleName.SUPERVISOR))

    def test_team_lead_assign_and_revoke_updates_role(self):
        self._auth(self.hr)

        lead = make_user(
            "team.lead@test.com",
            password=self.password,
            roles=[],
            department=self.dept_a,
        )
        lead.unit = self.unit_a1
        lead.save(update_fields=["unit", "updated_at"])

        url = reverse("team-team-lead", args=[self.team_a1.id])
        resp = self.client.post(url, {"user_id": str(lead.id)}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK, msg=f"{resp.data}")

        lead.refresh_from_db()
        self.assertTrue(lead.has_role(RoleName.TEAM_LEAD))

        resp2 = self.client.delete(url)
        self.assertEqual(resp2.status_code, status.HTTP_200_OK, msg=f"{resp2.data}")

        lead.refresh_from_db()
        self.assertFalse(lead.has_role(RoleName.TEAM_LEAD))

    def test_multi_role_hr_can_be_unit_supervisor_and_retains_hr_on_revoke(self):
        self._auth(self.line_manager_a)

        hr_supervisor = make_user(
            "hr.supervisor@test.com",
            password=self.password,
            roles=[RoleName.HR],
            department=self.dept_a,
        )

        url = reverse("unit-supervisor", args=[self.unit_a1.id])
        resp = self.client.post(url, {"user_id": str(hr_supervisor.id)}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK, msg=f"{resp.data}")

        hr_supervisor.refresh_from_db()
        self.assertTrue(hr_supervisor.has_role(RoleName.HR))
        self.assertTrue(hr_supervisor.has_role(RoleName.SUPERVISOR))

        resp2 = self.client.delete(url)
        self.assertEqual(resp2.status_code, status.HTTP_200_OK, msg=f"{resp2.data}")

        hr_supervisor.refresh_from_db()
        self.assertTrue(hr_supervisor.has_role(RoleName.HR))
        self.assertFalse(hr_supervisor.has_role(RoleName.SUPERVISOR))

