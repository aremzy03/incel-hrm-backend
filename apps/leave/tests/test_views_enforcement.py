"""
Tests for leave enforcement rules at the view layer.

Covers:
- LeaveRequestViewSet queryset scoping and draft visibility
- Logs visibility rules
- submit / approve / reject / cancel actions and role enforcement
- Calendar exposure after final approval
"""

import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Department, Role, RoleName, UserRole
from apps.leave.models import (
    LeaveApprovalLog,
    LeaveBalance,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveType,
)


User = get_user_model()


def make_department(name="Engineering"):
    """
    Helper for creating departments in tests.

    Uses get_or_create to avoid UNIQUE constraint errors when a department
    with the same name already exists (e.g. seeded HR department).
    """
    dept, _ = Department.objects.get_or_create(name=name)
    return dept


def make_role(name):
    return Role.objects.get(name=name)


def make_user(email, *, roles=None, department=None, is_staff=False):
    user = User.objects.create_user(email=email, password="testpass123")
    user.is_staff = is_staff
    if department is not None:
        user.department = department
    user.save(update_fields=["is_staff", "department", "updated_at"])

    roles = roles or []
    for r in roles:
        role = make_role(r)
        UserRole.objects.get_or_create(user=user, role=role)
    return user


def make_leave_type(name="Annual", default_days=21):
    lt, _ = LeaveType.objects.get_or_create(
        name=name, defaults={"default_days": default_days}
    )
    return lt


def make_request(
    employee,
    leave_type,
    *,
    status=LeaveRequestStatus.DRAFT,
    cover_person=None,
    start=None,
    end=None,
):
    today = datetime.date.today()
    start = start or today
    end = end or (today + datetime.timedelta(days=1))
    return LeaveRequest.objects.create(
        employee=employee,
        leave_type=leave_type,
        start_date=start,
        end_date=end,
        status=status,
        cover_person=cover_person,
        total_working_days=1,
    )


class LeaveRequestVisibilityTests(TestCase):
    def setUp(self):
        self.client = APIClient()

        self.dept_a = make_department("Dept A")
        self.dept_b = make_department("Dept B")

        self.annual = make_leave_type("Annual", 21)
        self.sick = make_leave_type("Sick", 14)

        self.emp_a = make_user(
            "emp_a@test.com",
            roles=[RoleName.EMPLOYEE],
            department=self.dept_a,
        )
        self.emp_b = make_user(
            "emp_b@test.com",
            roles=[RoleName.EMPLOYEE],
            department=self.dept_a,
        )
        self.emp_c = make_user(
            "emp_c@test.com",
            roles=[RoleName.EMPLOYEE],
            department=self.dept_b,
        )
        self.lm_a = make_user(
            "lm_a@test.com",
            roles=[RoleName.LINE_MANAGER],
            department=self.dept_a,
        )
        self.hr = make_user(
            "hr@test.com",
            roles=[RoleName.HR],
            department=make_department("Human Resources (HR)"),
        )
        self.ed = make_user(
            "ed@test.com",
            roles=[RoleName.EXECUTIVE_DIRECTOR],
        )
        self.cover_user = make_user(
            "cover@test.com",
            roles=[RoleName.EMPLOYEE],
            department=self.dept_a,
        )

        self.draft_a = make_request(
            self.emp_a, self.annual, status=LeaveRequestStatus.DRAFT, cover_person=self.cover_user
        )
        self.pending_manager_a = make_request(
            self.emp_a,
            self.annual,
            status=LeaveRequestStatus.PENDING_MANAGER,
        )
        self.approved_a = make_request(
            self.emp_a,
            self.sick,
            status=LeaveRequestStatus.APPROVED,
        )
        self.draft_b = make_request(
            self.emp_b, self.annual, status=LeaveRequestStatus.DRAFT
        )

        self.list_url = reverse("leave-request-list")

    def _ids(self, response):
        """
        Return set of IDs from a list or paginated list response.

        Handles both plain lists and paginated responses with a ``results`` key.
        """
        data = response.data
        if isinstance(data, dict) and "results" in data:
            data = data["results"]
        return {str(item["id"]) for item in data}

    def test_employee_sees_all_own_requests_including_drafts(self):
        self.client.force_authenticate(self.emp_a)
        resp = self.client.get(self.list_url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = self._ids(resp)
        self.assertIn(str(self.draft_a.id), ids)
        self.assertIn(str(self.pending_manager_a.id), ids)
        self.assertIn(str(self.approved_a.id), ids)
        self.assertNotIn(str(self.draft_b.id), ids)

    def test_privileged_user_does_not_see_others_drafts(self):
        self.client.force_authenticate(self.hr)
        resp = self.client.get(self.list_url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = self._ids(resp)
        self.assertNotIn(str(self.draft_a.id), ids)
        self.assertNotIn(str(self.draft_b.id), ids)
        self.assertIn(str(self.pending_manager_a.id), ids)
        self.assertIn(str(self.approved_a.id), ids)

    def test_line_manager_does_not_see_others_drafts(self):
        self.client.force_authenticate(self.lm_a)
        resp = self.client.get(self.list_url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = self._ids(resp)
        self.assertNotIn(str(self.draft_a.id), ids)
        self.assertNotIn(str(self.draft_b.id), ids)
        self.assertIn(str(self.pending_manager_a.id), ids)
        self.assertIn(str(self.approved_a.id), ids)

    def test_cover_person_sees_request_after_submission_only(self):
        self.client.force_authenticate(self.cover_user)
        # While draft, cover person should not see it
        resp = self.client.get(self.list_url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = self._ids(resp)
        self.assertNotIn(str(self.draft_a.id), ids)

        # Once non-draft, they should see it
        self.draft_a.status = LeaveRequestStatus.PENDING_MANAGER
        self.draft_a.save(update_fields=["status", "updated_at"])
        resp = self.client.get(self.list_url)
        ids = self._ids(resp)
        self.assertIn(str(self.draft_a.id), ids)

    def test_uninvolved_employee_does_not_see_others_requests(self):
        self.client.force_authenticate(self.emp_c)
        resp = self.client.get(self.list_url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = self._ids(resp)
        self.assertEqual(ids, set())


class LeaveRequestLogsVisibilityTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        dept = make_department("Dept A")
        self.emp_a = make_user(
            "owner@test.com",
            roles=[RoleName.EMPLOYEE],
            department=dept,
        )
        self.cover = make_user(
            "cover@test.com",
            roles=[RoleName.EMPLOYEE],
            department=dept,
        )
        self.lm = make_user(
            "lm@test.com",
            roles=[RoleName.LINE_MANAGER],
            department=dept,
        )
        self.hr = make_user(
            "hr@test.com",
            roles=[RoleName.HR],
            department=make_department("Human Resources (HR)"),
        )
        self.other = make_user(
            "other@test.com",
            roles=[RoleName.EMPLOYEE],
            department=dept,
        )

        annual = make_leave_type("Annual", 21)
        self.req = make_request(
            self.emp_a,
            annual,
            status=LeaveRequestStatus.DRAFT,
            cover_person=self.cover,
        )

        self.logs_url = reverse("leave-request-logs", args=[self.req.id])

    def test_owner_can_view_logs_for_draft(self):
        self.client.force_authenticate(self.emp_a)
        resp = self.client.get(self.logs_url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_non_owner_cannot_view_logs_for_draft(self):
        for user in (self.cover, self.lm, self.hr, self.other):
            self.client.force_authenticate(user)
            resp = self.client.get(self.logs_url)
            # Non-owners cannot see draft logs; depending on queryset scoping
            # this may surface as 403 (denied) or 404 (not found).
            self.assertIn(resp.status_code, (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND))

    def test_cover_and_line_manager_can_view_logs_after_submission(self):
        self.req.status = LeaveRequestStatus.PENDING_MANAGER
        self.req.save(update_fields=["status", "updated_at"])

        self.client.force_authenticate(self.cover)
        resp = self.client.get(self.logs_url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        self.client.force_authenticate(self.lm)
        resp = self.client.get(self.logs_url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_uninvolved_employee_cannot_view_logs_after_submission(self):
        self.req.status = LeaveRequestStatus.PENDING_MANAGER
        self.req.save(update_fields=["status", "updated_at"])

        self.client.force_authenticate(self.other)
        resp = self.client.get(self.logs_url)
        self.assertIn(resp.status_code, (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND))


class LeaveRequestSubmitTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        dept = make_department("Dept A")
        self.emp = make_user(
            "emp@test.com",
            roles=[RoleName.EMPLOYEE],
            department=dept,
        )
        self.lm = make_user(
            "lm@test.com",
            roles=[RoleName.LINE_MANAGER],
            department=dept,
        )
        self.hr = make_user(
            "hr@test.com",
            roles=[RoleName.HR],
            department=make_department("Human Resources (HR)"),
        )
        annual = make_leave_type("Annual", 21)
        self.req = make_request(
            self.emp,
            annual,
            status=LeaveRequestStatus.DRAFT,
        )
        self.submit_url = reverse("leave-request-submit", args=[self.req.id])

    def test_owner_can_submit_draft(self):
        dept = self.emp.department
        dept.line_manager = self.lm
        dept.save(update_fields=["line_manager", "updated_at"])

        self.client.force_authenticate(self.emp)
        resp = self.client.post(self.submit_url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, LeaveRequestStatus.PENDING_MANAGER)
        self.assertTrue(
            LeaveApprovalLog.objects.filter(
                leave_request=self.req, new_status=LeaveRequestStatus.PENDING_MANAGER
            ).exists()
        )

    def test_non_owner_cannot_submit(self):
        self.client.force_authenticate(self.hr)
        resp = self.client.post(self.submit_url)
        self.assertIn(resp.status_code, (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND))

    def test_cannot_submit_non_draft(self):
        self.req.status = LeaveRequestStatus.PENDING_MANAGER
        self.req.save(update_fields=["status", "updated_at"])

        self.client.force_authenticate(self.emp)
        resp = self.client.post(self.submit_url)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_submit_requires_line_manager(self):
        self.client.force_authenticate(self.emp)
        resp = self.client.post(self.submit_url)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("department", resp.data)


class LeaveRequestApproveTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        dept = make_department("Dept A")
        self.emp = make_user(
            "emp@test.com",
            roles=[RoleName.EMPLOYEE],
            department=dept,
        )
        self.lm = make_user(
            "lm@test.com",
            roles=[RoleName.LINE_MANAGER],
            department=dept,
        )
        self.hr = make_user(
            "hr@test.com",
            roles=[RoleName.HR],
            department=make_department("Human Resources (HR)"),
        )
        self.ed = make_user(
            "ed@test.com",
            roles=[RoleName.EXECUTIVE_DIRECTOR],
        )
        annual = make_leave_type("Annual", 21)
        self.req = make_request(
            self.emp,
            annual,
            status=LeaveRequestStatus.PENDING_MANAGER,
        )
        self.approve_url = reverse("leave-request-approve", args=[self.req.id])

    def test_line_manager_approves_from_pending_manager(self):
        self.client.force_authenticate(self.lm)
        resp = self.client.post(self.approve_url, {"comment": "ok"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, LeaveRequestStatus.PENDING_HR)

    def test_wrong_role_cannot_approve_at_stage(self):
        self.client.force_authenticate(self.hr)
        resp = self.client.post(self.approve_url, {"comment": "ok"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_full_approval_chain_and_calendar(self):
        LeaveBalance.objects.update_or_create(
            employee=self.emp,
            leave_type=self.req.leave_type,
            year=self.req.start_date.year,
            defaults={"allocated_days": 10, "used_days": 0},
        )

        self.client.force_authenticate(self.lm)
        self.client.post(self.approve_url, {"comment": "manager ok"}, format="json")
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, LeaveRequestStatus.PENDING_HR)

        self.client.force_authenticate(self.hr)
        self.client.post(self.approve_url, {"comment": "hr ok"}, format="json")
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, LeaveRequestStatus.PENDING_ED)

        self.client.force_authenticate(self.ed)
        self.client.post(self.approve_url, {"comment": "ed ok"}, format="json")
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, LeaveRequestStatus.APPROVED)

        balance = LeaveBalance.objects.get(
            employee=self.emp,
            leave_type=self.req.leave_type,
            year=self.req.start_date.year,
        )
        self.assertEqual(balance.used_days, self.req.total_working_days)

        calendar_url = reverse("leave-calendar")
        self.client.force_authenticate(self.emp)
        resp = self.client.get(calendar_url, {"year": self.req.start_date.year})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(
            any(str(item["id"]) == str(self.req.id) for item in resp.data)
        )


class LeaveRequestRejectTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        dept = make_department("Dept A")
        self.emp = make_user(
            "emp@test.com",
            roles=[RoleName.EMPLOYEE],
            department=dept,
        )
        self.lm = make_user(
            "lm@test.com",
            roles=[RoleName.LINE_MANAGER],
            department=dept,
        )
        annual = make_leave_type("Annual", 21)
        self.req = make_request(
            self.emp,
            annual,
            status=LeaveRequestStatus.PENDING_MANAGER,
        )
        self.reject_url = reverse("leave-request-reject", args=[self.req.id])

    def test_reject_requires_comment(self):
        self.client.force_authenticate(self.lm)
        resp = self.client.post(self.reject_url, {"comment": ""}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_only_role_matched_approver_can_reject(self):
        self.client.force_authenticate(self.lm)
        resp = self.client.post(self.reject_url, {"comment": "no"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, LeaveRequestStatus.REJECTED)

        self.req.status = LeaveRequestStatus.PENDING_MANAGER
        self.req.save(update_fields=["status", "updated_at"])
        other = make_user(
            "other@test.com",
            roles=[RoleName.EMPLOYEE],
            department=self.emp.department,
        )
        self.client.force_authenticate(other)
        resp = self.client.post(self.reject_url, {"comment": "no"}, format="json")
        self.assertIn(resp.status_code, (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND))


class LeaveRequestCancelTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        dept = make_department("Dept A")
        self.emp = make_user(
            "emp@test.com",
            roles=[RoleName.EMPLOYEE],
            department=dept,
        )
        self.hr = make_user(
            "hr@test.com",
            roles=[RoleName.HR],
            department=make_department("Human Resources (HR)"),
            is_staff=True,
        )
        annual = make_leave_type("Annual", 21)
        self.req = make_request(
            self.emp,
            annual,
            status=LeaveRequestStatus.DRAFT,
        )
        self.cancel_url = reverse("leave-request-cancel", args=[self.req.id])

    def test_hr_can_cancel_non_terminal(self):
        # HR can cancel for non-draft, non-terminal statuses they can see.
        for status_value in (
            LeaveRequestStatus.PENDING_TEAM_LEAD,
            LeaveRequestStatus.PENDING_SUPERVISOR,
            LeaveRequestStatus.PENDING_MANAGER,
            LeaveRequestStatus.PENDING_HR,
            LeaveRequestStatus.PENDING_ED,
        ):
            self.req.status = status_value
            self.req.save(update_fields=["status", "updated_at"])
            self.client.force_authenticate(self.hr)
            resp = self.client.post(self.cancel_url, {"comment": "cleanup"}, format="json")
            self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_owner_can_cancel_only_draft_or_pending_manager(self):
        self.client.force_authenticate(self.emp)

        self.req.status = LeaveRequestStatus.DRAFT
        self.req.save(update_fields=["status", "updated_at"])
        resp = self.client.post(self.cancel_url, {"comment": "change"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        self.req.status = LeaveRequestStatus.PENDING_MANAGER
        self.req.save(update_fields=["status", "updated_at"])
        resp = self.client.post(self.cancel_url, {"comment": "change"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        self.req.status = LeaveRequestStatus.PENDING_TEAM_LEAD
        self.req.save(update_fields=["status", "updated_at"])
        resp = self.client.post(self.cancel_url, {"comment": "change"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        for status_value in (
            LeaveRequestStatus.PENDING_HR,
            LeaveRequestStatus.PENDING_ED,
            LeaveRequestStatus.APPROVED,
        ):
            self.req.status = status_value
            self.req.save(update_fields=["status", "updated_at"])
            resp = self.client.post(self.cancel_url, {"comment": "change"}, format="json")
            self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_cancel_terminal_statuses(self):
        for status_value in (LeaveRequestStatus.REJECTED, LeaveRequestStatus.CANCELLED):
            self.req.status = status_value
            self.req.save(update_fields=["status", "updated_at"])
            self.client.force_authenticate(self.hr)
            resp = self.client.post(self.cancel_url, {"comment": "cleanup"}, format="json")
            self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class LeaveRequestCreateTests(TestCase):
    """
    Tests for creating leave requests for each leave type and key edge cases.
    """

    def setUp(self):
        self.client = APIClient()
        self.dept = make_department("Dept A")
        self.other_dept = make_department("Dept B")

        # Ensure all core leave types exist
        self.annual = make_leave_type("Annual", 21)
        self.sick = make_leave_type("Sick", 14)
        self.casual = make_leave_type("Casual", 5)
        self.maternity = make_leave_type("Maternity", 90)
        self.paternity = make_leave_type("Paternity", 14)

        today = datetime.date.today()
        self.year = today.year
        self.start = today
        self.end = today + datetime.timedelta(days=1)

        User = get_user_model()
        # Employees with explicit gender and department so eligibility and overlap behave as expected
        self.female_emp = User.objects.create_user(
            email="female@test.com",
            password="testpass123",
            gender="FEMALE",
            department=self.dept,
            date_of_birth=datetime.date(1990, 1, 1),
        )
        self.male_emp = User.objects.create_user(
            email="male@test.com",
            password="testpass123",
            gender="MALE",
            department=self.dept,
            date_of_birth=datetime.date(1990, 1, 1),
        )
        self.other_emp = User.objects.create_user(
            email="other@test.com",
            password="testpass123",
            gender="FEMALE",
            department=self.dept,
            date_of_birth=datetime.date(1990, 1, 1),
        )
        self.cover_same_dept = User.objects.create_user(
            email="cover_same@test.com",
            password="testpass123",
            gender="FEMALE",
            department=self.dept,
            date_of_birth=datetime.date(1990, 1, 1),
        )
        self.cover_other_dept = User.objects.create_user(
            email="cover_other@test.com",
            password="testpass123",
            gender="FEMALE",
            department=self.other_dept,
            date_of_birth=datetime.date(1990, 1, 1),
        )

        self.list_url = reverse("leave-request-list")

    def _auth(self, user):
        self.client.force_authenticate(user)

    def _create_balance(self, employee, leave_type, allocated=10, used=0):
        balance, _ = LeaveBalance.objects.update_or_create(
            employee=employee,
            leave_type=leave_type,
            year=self.year,
            defaults={"allocated_days": allocated, "used_days": used},
        )
        return balance

    def _post_create(self, user, leave_type, cover_person=None, start=None, end=None):
        self._auth(user)
        start = start or self.start
        end = end or self.end
        payload = {
            "leave_type": str(leave_type.id),
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "reason": "Reason",
            "is_emergency": False,
            "cover_person": str(cover_person.id) if cover_person else None,
        }
        # Remove cover_person key if None so serializer doesn't treat it as explicit null
        if payload["cover_person"] is None:
            payload.pop("cover_person")
        return self.client.post(self.list_url, payload, format="json")

    def test_create_annual_success(self):
        self._create_balance(self.female_emp, self.annual)
        resp = self._post_create(self.female_emp, self.annual, cover_person=self.cover_same_dept)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            LeaveRequest.objects.filter(
                employee=self.female_emp, leave_type=self.annual
            ).exists()
        )

    def test_create_annual_without_cover_person_success(self):
        self._create_balance(self.female_emp, self.annual)
        resp = self._post_create(self.female_emp, self.annual, cover_person=None)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        leave_request = LeaveRequest.objects.filter(
            employee=self.female_emp,
            leave_type=self.annual,
            start_date=self.start,
            end_date=self.end,
        ).order_by("-created_at").first()
        self.assertIsNotNone(leave_request)
        self.assertIsNone(leave_request.cover_person)

    def test_annual_department_overlap_blocked_for_other_employee(self):
        # Existing active Annual request for another employee in same department
        make_request(
            self.other_emp,
            self.annual,
            status=LeaveRequestStatus.PENDING_MANAGER,
            start=self.start,
            end=self.end,
        )
        self._create_balance(self.female_emp, self.annual)
        resp = self._post_create(self.female_emp, self.annual, cover_person=self.cover_same_dept)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("leave_request", resp.data)

    def test_sick_overlap_allowed_for_same_department(self):
        # Existing Sick request for another employee in same department
        make_request(
            self.other_emp,
            self.sick,
            status=LeaveRequestStatus.PENDING_MANAGER,
            start=self.start,
            end=self.end,
        )
        self._create_balance(self.female_emp, self.sick)
        resp = self._post_create(self.female_emp, self.sick, cover_person=self.cover_same_dept)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_maternity_requires_female(self):
        # Male employee with balance for Maternity should still be rejected by gender rule
        self._create_balance(self.male_emp, self.maternity)
        resp = self._post_create(self.male_emp, self.maternity, cover_person=self.cover_same_dept)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("leave_type", resp.data)

    def test_paternity_requires_male(self):
        self._create_balance(self.female_emp, self.paternity)
        resp = self._post_create(self.female_emp, self.paternity, cover_person=self.cover_same_dept)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("leave_type", resp.data)

    def test_cover_person_cannot_be_self(self):
        self._create_balance(self.female_emp, self.annual)
        resp = self._post_create(self.female_emp, self.annual, cover_person=self.female_emp)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("cover_person", resp.data)

    def test_cover_person_must_be_same_department(self):
        self._create_balance(self.female_emp, self.annual)
        resp = self._post_create(self.female_emp, self.annual, cover_person=self.cover_other_dept)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("cover_person", resp.data)

    def test_insufficient_balance_blocks_creation(self):
        # Balance with zero remaining days
        self._create_balance(self.female_emp, self.annual, allocated=1, used=1)
        # Request 2 working days
        start = self.start
        end = start + datetime.timedelta(days=2)
        resp = self._post_create(self.female_emp, self.annual, cover_person=self.cover_same_dept, start=start, end=end)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("leave_balance", resp.data)

