import datetime

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import (
    Department,
    Role,
    RoleName,
    Team,
    Unit,
    UserRole,
    get_or_create_management_department,
)
from apps.leave.models import (
    LeaveBalance,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveType,
    PublicHoliday,
)


User = get_user_model()


class LeaveApiTests(APITestCase):
    def setUp(self):
        self.password = "testpass123"
        self.current_year = timezone.now().year
        self.base_start = self._first_weekday_of_year(self.current_year + 10)
        self.base_end = self.base_start + datetime.timedelta(days=3)

        self.department = Department.objects.create(name="Engineering")
        self.hr_department, _ = Department.objects.get_or_create(
            name="Human Resources (HR)"
        )

        self.employee = self._create_user_with_roles(
            "employee@test.com", [RoleName.EMPLOYEE], department=self.department
        )
        self.supervisor = self._create_user_with_roles(
            "supervisor@test.com", [RoleName.SUPERVISOR], department=self.department
        )
        self.line_manager = self._create_user_with_roles(
            "line.manager@test.com", [RoleName.LINE_MANAGER], department=self.department
        )
        self.hr_user = self._create_user_with_roles(
            "hr@test.com", [RoleName.HR], department=self.hr_department
        )
        self.executive_director = self._create_user_with_roles(
            "ed@test.com", [RoleName.EXECUTIVE_DIRECTOR, RoleName.LINE_MANAGER], department=self.hr_department
        )

        self.unit = Unit.objects.create(
            name="Backend Unit",
            department=self.department,
            supervisor=self.supervisor,
        )
        self.department.line_manager = self.line_manager
        self.department.save(update_fields=["line_manager", "updated_at"])

        # Keep employee without a unit to force first stage = PENDING_MANAGER.
        self.employee.unit = None
        self.employee.save(update_fields=["unit", "updated_at"])

        self.leave_type, _ = LeaveType.objects.get_or_create(
            name="Annual",
            defaults={"default_days": 21},
        )
        if self.leave_type.default_days != 21:
            self.leave_type.default_days = 21
            self.leave_type.save(update_fields=["default_days", "updated_at"])
        self.balance, _ = LeaveBalance.objects.update_or_create(
            employee=self.employee,
            leave_type=self.leave_type,
            year=self.base_start.year,
            defaults={"allocated_days": 21, "used_days": 0},
        )

        self._create_public_holidays()

        self.list_url = reverse("leave-request-list")
        self.create_and_submit_url = reverse("leave-request-create-and-submit")

    def _create_user_with_roles(self, email, roles, department=None):
        user = User.objects.create_user(
            email=email,
            password=self.password,
            department=department,
        )
        for role_name in roles:
            role, _ = Role.objects.get_or_create(name=role_name)
            UserRole.objects.get_or_create(user=user, role=role)
        return user

    def _first_weekday_of_year(self, year):
        date = datetime.date(year, 1, 2)
        while date.weekday() >= 5:
            date += datetime.timedelta(days=1)
        return date

    def _create_public_holidays(self):
        # Place holidays within a broad test window, but away from core request ranges.
        PublicHoliday.objects.get_or_create(
            date=self.base_start + datetime.timedelta(days=20),
            defaults={"name": "Holiday One", "is_recurring": False},
        )
        PublicHoliday.objects.get_or_create(
            date=self.base_start + datetime.timedelta(days=40),
            defaults={"name": "Holiday Two", "is_recurring": False},
        )
        PublicHoliday.objects.get_or_create(
            date=self.base_start + datetime.timedelta(days=60),
            defaults={"name": "Holiday Three", "is_recurring": False},
        )

    def _auth(self, user):
        self.client.force_authenticate(user=user)

    def _create_payload(self, start_date=None, end_date=None):
        start_date = start_date or self.base_start
        end_date = end_date or self.base_end
        return {
            "leave_type": str(self.leave_type.id),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "reason": "Annual leave",
            "is_emergency": False,
            "cover_person": str(self.supervisor.id),
        }

    def _create_request(self, user, start_date=None, end_date=None):
        self._auth(user)
        payload = self._create_payload(start_date=start_date, end_date=end_date)
        return self.client.post(
            self.list_url,
            payload,
            format="json",
        )

    def _resolve_created_request_id(self, response, start_date=None, end_date=None, employee=None):
        if "id" in response.data:
            return response.data["id"]

        employee = employee or self.employee
        start_date = start_date or self.base_start
        end_date = end_date or self.base_end
        leave_request = LeaveRequest.objects.filter(
            employee=employee,
            leave_type=self.leave_type,
            start_date=start_date,
            end_date=end_date,
        ).order_by("-created_at").first()
        self.assertIsNotNone(leave_request, msg=f"Create response data: {response.data}")
        return str(leave_request.id)

    def _submit_request(self, user, request_id):
        self._auth(user)
        return self.client.post(
            reverse("leave-request-submit", args=[request_id]),
            format="json",
        )

    def _approve_request(self, user, request_id, comment="approved"):
        self._auth(user)
        return self.client.post(
            reverse("leave-request-approve", args=[request_id]),
            {"comment": comment},
            format="json",
        )

    def _reject_request(self, user, request_id, comment="rejected"):
        self._auth(user)
        return self.client.post(
            reverse("leave-request-reject", args=[request_id]),
            {"comment": comment},
            format="json",
        )

    def _cancel_request(self, user, request_id, comment="cancelled"):
        self._auth(user)
        return self.client.post(
            reverse("leave-request-cancel", args=[request_id]),
            {"comment": comment},
            format="json",
        )

    def _create_and_submit(self, start_date=None, end_date=None):
        create_resp = self._create_request(
            self.employee, start_date=start_date, end_date=end_date
        )
        self.assertEqual(create_resp.status_code, status.HTTP_201_CREATED)
        leave_request_id = self._resolve_created_request_id(
            create_resp, start_date=start_date, end_date=end_date
        )
        submit_resp = self._submit_request(self.employee, leave_request_id)
        self.assertEqual(submit_resp.status_code, status.HTTP_200_OK)
        return leave_request_id

    def test_employee_can_submit_leave_request(self):
        create_resp = self._create_request(self.employee)
        self.assertEqual(create_resp.status_code, status.HTTP_201_CREATED)
        leave_request_id = self._resolve_created_request_id(create_resp)

        submit_resp = self._submit_request(self.employee, leave_request_id)
        self.assertEqual(submit_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(submit_resp.data["status"], LeaveRequestStatus.PENDING_MANAGER)

    def test_team_member_starts_at_team_lead(self):
        # Create team + team lead under unit; assign employee into unit+team
        team_lead = self._create_user_with_roles(
            "teamlead@test.com", [RoleName.TEAM_LEAD], department=self.department
        )
        team_lead.unit = self.unit
        team_lead.save(update_fields=["unit", "updated_at"])

        team = Team.objects.create(name="API Team", unit=self.unit, team_lead=team_lead)
        self.employee.unit = self.unit
        self.employee.team = team
        self.employee.save(update_fields=["unit", "team", "updated_at"])

        create_resp = self._create_request(self.employee)
        leave_request_id = self._resolve_created_request_id(create_resp)
        submit_resp = self._submit_request(self.employee, leave_request_id)
        self.assertEqual(submit_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(submit_resp.data["status"], LeaveRequestStatus.PENDING_TEAM_LEAD)

    def test_line_manager_request_routes_to_management_then_hr_then_ed(self):
        # LINE_MANAGER requester should go to Management dept line manager (ED), then HR, then ED final.
        LeaveBalance.objects.update_or_create(
            employee=self.line_manager,
            leave_type=self.leave_type,
            year=self.base_start.year,
            defaults={"allocated_days": 21, "used_days": 0},
        )
        self.department.line_manager = self.line_manager
        self.department.save(update_fields=["line_manager", "updated_at"])

        mgmt = get_or_create_management_department()
        mgmt.line_manager = self.executive_director
        mgmt.save(update_fields=["line_manager", "updated_at"])

        create_resp = self._create_request(self.line_manager)
        self.assertEqual(create_resp.status_code, status.HTTP_201_CREATED, msg=f"{create_resp.data}")
        request_id = self._resolve_created_request_id(create_resp, employee=self.line_manager)
        submit_resp = self._submit_request(self.line_manager, request_id)
        self.assertEqual(submit_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(submit_resp.data["status"], LeaveRequestStatus.PENDING_MANAGER)

        # ED approves as management dept line manager (LINE_MANAGER stage)
        manager_approve = self._approve_request(self.executive_director, request_id)
        self.assertEqual(manager_approve.status_code, status.HTTP_200_OK)
        self.assertEqual(manager_approve.data["status"], LeaveRequestStatus.PENDING_HR)

        # HR approves
        hr_approve = self._approve_request(self.hr_user, request_id)
        self.assertEqual(hr_approve.status_code, status.HTTP_200_OK)
        self.assertEqual(hr_approve.data["status"], LeaveRequestStatus.PENDING_ED)

        # ED final approves (2nd approval by ED)
        ed_approve = self._approve_request(self.executive_director, request_id)
        self.assertEqual(ed_approve.status_code, status.HTTP_200_OK)
        self.assertEqual(ed_approve.data["status"], LeaveRequestStatus.APPROVED)

    def test_hr_request_goes_to_manager_then_ed(self):
        # HR requester: manager approval should jump directly to ED (skipping HR stage)
        # Ensure HR department has a line manager
        hr_line_manager = self._create_user_with_roles(
            "hr.lm@test.com", [RoleName.LINE_MANAGER], department=self.hr_department
        )
        hr_cover = self._create_user_with_roles(
            "hr.cover@test.com", [RoleName.EMPLOYEE], department=self.hr_department
        )
        self.hr_department.line_manager = hr_line_manager
        self.hr_department.save(update_fields=["line_manager", "updated_at"])

        LeaveBalance.objects.update_or_create(
            employee=self.hr_user,
            leave_type=self.leave_type,
            year=self.base_start.year,
            defaults={"allocated_days": 21, "used_days": 0},
        )

        self._auth(self.hr_user)
        payload = self._create_payload()
        payload["cover_person"] = str(hr_cover.id)
        create_resp = self.client.post(self.list_url, payload, format="json")
        self.assertEqual(create_resp.status_code, status.HTTP_201_CREATED, msg=f"{create_resp.data}")
        request_id = self._resolve_created_request_id(create_resp, employee=self.hr_user)

        submit_resp = self._submit_request(self.hr_user, request_id)
        self.assertEqual(submit_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(submit_resp.data["status"], LeaveRequestStatus.PENDING_MANAGER)

        manager_approve = self._approve_request(hr_line_manager, request_id)
        self.assertEqual(manager_approve.status_code, status.HTTP_200_OK)
        self.assertEqual(manager_approve.data["status"], LeaveRequestStatus.PENDING_ED)

    def test_balance_validation_rejects_excess(self):
        self.balance.used_days = 20
        self.balance.save(update_fields=["used_days", "updated_at"])

        start_date = self.base_start + datetime.timedelta(days=5)
        end_date = start_date + datetime.timedelta(days=5)
        response = self._create_request(
            self.employee, start_date=start_date, end_date=end_date
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("leave_balance", response.data)
        self.assertIn("Insufficient leave balance", str(response.data["leave_balance"]))

    def test_full_approval_chain(self):
        leave_request_id = self._create_and_submit()

        manager_approve = self._approve_request(self.line_manager, leave_request_id)
        self.assertEqual(manager_approve.status_code, status.HTTP_200_OK)
        self.assertEqual(manager_approve.data["status"], LeaveRequestStatus.PENDING_HR)

        hr_approve = self._approve_request(self.hr_user, leave_request_id)
        self.assertEqual(hr_approve.status_code, status.HTTP_200_OK)
        self.assertEqual(hr_approve.data["status"], LeaveRequestStatus.PENDING_ED)

        ed_approve = self._approve_request(self.executive_director, leave_request_id)
        self.assertEqual(ed_approve.status_code, status.HTTP_200_OK)
        self.assertEqual(ed_approve.data["status"], LeaveRequestStatus.APPROVED)

        leave_request = LeaveRequest.objects.get(id=leave_request_id)
        self.balance.refresh_from_db()
        self.assertEqual(self.balance.used_days, leave_request.total_working_days)

    def test_rejection_ends_chain(self):
        leave_request_id = self._create_and_submit()

        manager_approve = self._approve_request(self.line_manager, leave_request_id)
        self.assertEqual(manager_approve.status_code, status.HTTP_200_OK)
        self.assertEqual(manager_approve.data["status"], LeaveRequestStatus.PENDING_HR)

        hr_reject = self._reject_request(self.hr_user, leave_request_id, comment="No cover")
        self.assertEqual(hr_reject.status_code, status.HTTP_200_OK)
        self.assertEqual(hr_reject.data["status"], LeaveRequestStatus.REJECTED)

        ed_attempt = self._approve_request(self.executive_director, leave_request_id)
        self.assertIn(
            ed_attempt.status_code,
            (status.HTTP_400_BAD_REQUEST, status.HTTP_403_FORBIDDEN),
        )

        leave_request = LeaveRequest.objects.get(id=leave_request_id)
        self.assertEqual(leave_request.status, LeaveRequestStatus.REJECTED)

    def test_employee_cannot_approve(self):
        leave_request_id = self._create_and_submit()

        response = self._approve_request(self.employee, leave_request_id)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_overlapping_leave_blocked(self):
        # Create and submit, then fully approve so it's APPROVED (only approved blocks overlaps now).
        leave_request_id = self._create_and_submit()
        self._approve_request(self.line_manager, leave_request_id)
        self._approve_request(self.hr_user, leave_request_id)
        self._approve_request(self.executive_director, leave_request_id)

        overlapping_start = self.base_start + datetime.timedelta(days=1)
        overlapping_end = self.base_end + datetime.timedelta(days=1)
        overlap_resp = self._create_request(
            self.employee,
            start_date=overlapping_start,
            end_date=overlapping_end,
        )
        self.assertEqual(overlap_resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("leave_request", overlap_resp.data)
        self.assertIn("overlapping leave request", str(overlap_resp.data["leave_request"]))

    def test_cancel_by_employee(self):
        create_resp = self._create_request(self.employee)
        self.assertEqual(create_resp.status_code, status.HTTP_201_CREATED)
        leave_request_id = self._resolve_created_request_id(create_resp)

        cancel_resp = self._cancel_request(self.employee, leave_request_id)
        self.assertEqual(cancel_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(cancel_resp.data["status"], LeaveRequestStatus.CANCELLED)

    def test_cancel_by_hr_any_stage(self):
        leave_request_id = self._create_and_submit()
        self._approve_request(self.line_manager, leave_request_id)
        self._approve_request(self.hr_user, leave_request_id)

        leave_request = LeaveRequest.objects.get(id=leave_request_id)
        self.assertEqual(leave_request.status, LeaveRequestStatus.PENDING_ED)

        cancel_resp = self._cancel_request(self.hr_user, leave_request_id)
        self.assertEqual(cancel_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(cancel_resp.data["status"], LeaveRequestStatus.CANCELLED)

    @override_settings(
        CELERY_TASK_ALWAYS_EAGER=True,
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        FRONTEND_BASE_URL="http://localhost:3000",
    )
    def test_submit_sends_email_only_to_current_stage_approver(self):
        mail.outbox.clear()

        create_resp = self._create_request(self.employee)
        leave_request_id = self._resolve_created_request_id(create_resp)
        with self.captureOnCommitCallbacks(execute=True):
            submit_resp = self._submit_request(self.employee, leave_request_id)
        self.assertEqual(submit_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(submit_resp.data["status"], LeaveRequestStatus.PENDING_MANAGER)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.line_manager.email])
        self.assertTrue(
            getattr(mail.outbox[0], "alternatives", None),
            msg="Expected multipart email with HTML alternative.",
        )

    @override_settings(
        CELERY_TASK_ALWAYS_EAGER=True,
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        FRONTEND_BASE_URL="http://localhost:3000",
    )
    def test_team_lead_stage_emails_team_lead_not_manager(self):
        mail.outbox.clear()

        team_lead = self._create_user_with_roles(
            "teamlead.notify@test.com", [RoleName.TEAM_LEAD], department=self.department
        )
        team_lead.unit = self.unit
        team_lead.save(update_fields=["unit", "updated_at"])

        team = Team.objects.create(name="Notify Team", unit=self.unit, team_lead=team_lead)
        self.employee.unit = self.unit
        self.employee.team = team
        self.employee.save(update_fields=["unit", "team", "updated_at"])

        create_resp = self._create_request(self.employee)
        leave_request_id = self._resolve_created_request_id(create_resp)
        with self.captureOnCommitCallbacks(execute=True):
            submit_resp = self._submit_request(self.employee, leave_request_id)
        self.assertEqual(submit_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(submit_resp.data["status"], LeaveRequestStatus.PENDING_TEAM_LEAD)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [team_lead.email])

    @override_settings(
        CELERY_TASK_ALWAYS_EAGER=True,
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        FRONTEND_BASE_URL="http://localhost:3000",
    )
    def test_emails_sent_at_each_stage_and_final_decision(self):
        mail.outbox.clear()

        with self.captureOnCommitCallbacks(execute=True):
            leave_request_id = self._create_and_submit()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.line_manager.email])

        with self.captureOnCommitCallbacks(execute=True):
            manager_approve = self._approve_request(self.line_manager, leave_request_id)
        self.assertEqual(manager_approve.status_code, status.HTTP_200_OK)
        self.assertEqual(manager_approve.data["status"], LeaveRequestStatus.PENDING_HR)
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(mail.outbox[1].to, [self.hr_user.email])

        with self.captureOnCommitCallbacks(execute=True):
            hr_approve = self._approve_request(self.hr_user, leave_request_id)
        self.assertEqual(hr_approve.status_code, status.HTTP_200_OK)
        self.assertEqual(hr_approve.data["status"], LeaveRequestStatus.PENDING_ED)
        self.assertEqual(len(mail.outbox), 3)
        self.assertEqual(mail.outbox[2].to, [self.executive_director.email])

        with self.captureOnCommitCallbacks(execute=True):
            ed_approve = self._approve_request(self.executive_director, leave_request_id)
        self.assertEqual(ed_approve.status_code, status.HTTP_200_OK)
        self.assertEqual(ed_approve.data["status"], LeaveRequestStatus.APPROVED)
        self.assertEqual(len(mail.outbox), 4)
        self.assertEqual(mail.outbox[3].to, [self.employee.email])

    @override_settings(
        CELERY_TASK_ALWAYS_EAGER=True,
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        FRONTEND_BASE_URL="http://localhost:3000",
    )
    def test_rejection_sends_decision_only_to_requester(self):
        mail.outbox.clear()

        with self.captureOnCommitCallbacks(execute=True):
            leave_request_id = self._create_and_submit()
        self.assertEqual(len(mail.outbox), 1)  # manager action required

        with self.captureOnCommitCallbacks(execute=True):
            manager_approve = self._approve_request(self.line_manager, leave_request_id)
        self.assertEqual(manager_approve.status_code, status.HTTP_200_OK)
        self.assertEqual(manager_approve.data["status"], LeaveRequestStatus.PENDING_HR)
        self.assertEqual(len(mail.outbox), 2)  # HR action required

        with self.captureOnCommitCallbacks(execute=True):
            hr_reject = self._reject_request(self.hr_user, leave_request_id, comment="No cover")
        self.assertEqual(hr_reject.status_code, status.HTTP_200_OK)
        self.assertEqual(hr_reject.data["status"], LeaveRequestStatus.REJECTED)
        self.assertEqual(len(mail.outbox), 3)  # requester decision
        self.assertEqual(mail.outbox[2].to, [self.employee.email])
