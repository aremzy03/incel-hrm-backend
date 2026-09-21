import datetime
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase
from django.test import TestCase

from apps.accounts.models import Department, Role, RoleName, UserRole
from apps.leave.models import (
    BalanceTransactionType,
    HalfDayPeriod,
    LeaveBalance,
    LeaveBalanceTransaction,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveType,
    OverlapEnforcement,
)
from apps.leave.services import (
    WorkingDaysService,
    clone_leave_policy,
    deduct_leave_balance,
    get_active_policy,
    publish_leave_policy,
    reliever_required,
    release_leave_balance,
    reserve_leave_balance,
    restore_leave_balance,
)
from apps.leave.utils import format_leave_days

from django.contrib.auth import get_user_model

User = get_user_model()


class LeaveSprint2EnforcementTests(APITestCase):
    def setUp(self):
        self.password = "testpass123"
        self.department = Department.objects.create(name="Sprint2 Eng")
        self.hr_department, _ = Department.objects.get_or_create(
            name="Human Resources (HR)"
        )
        self.employee = self._create_user("emp-s2@test.com", [RoleName.EMPLOYEE], self.department)
        self.colleague = self._create_user("col-s2@test.com", [RoleName.EMPLOYEE], self.department)
        self.line_manager = self._create_user(
            "lm-s2@test.com", [RoleName.LINE_MANAGER], self.department
        )
        self.hr_user = self._create_user("hr-s2@test.com", [RoleName.HR], self.hr_department)
        self.department.line_manager = self.line_manager
        self.department.save(update_fields=["line_manager"])

        self.annual = LeaveType.objects.get(code=LeaveType.Code.ANNUAL)
        self.sick = LeaveType.objects.get(code=LeaveType.Code.SICK)
        self.year = timezone.now().year + 10
        self.start = self._weekday(datetime.date(self.year, 3, 2))
        self.end = self.start + datetime.timedelta(days=4)

        LeaveBalance.objects.update_or_create(
            employee=self.employee,
            leave_type=self.annual,
            year=self.start.year,
            defaults={"allocated_days": 21, "used_days": 0, "pending_days": 0},
        )
        self.list_url = reverse("leave-request-list")

    def _create_user(self, email, roles, department=None):
        user = User.objects.create_user(email=email, password=self.password, department=department)
        for role_name in roles:
            role, _ = Role.objects.get_or_create(name=role_name)
            UserRole.objects.get_or_create(user=user, role=role)
        return user

    def _weekday(self, date):
        while date.weekday() >= 5:
            date += datetime.timedelta(days=1)
        return date

    def _created_id(self, response):
        if "id" in response.data:
            return response.data["id"]
        req = LeaveRequest.objects.filter(employee=self.employee).order_by("-created_at").first()
        self.assertIsNotNone(req, msg=response.data)
        return str(req.id)

    def _payload(self, **overrides):
        data = {
            "leave_type": str(self.annual.id),
            "start_date": self.start.isoformat(),
            "end_date": self.end.isoformat(),
            "reason": "Sprint 2",
            "is_emergency": False,
            "cover_person": str(self.colleague.id),
        }
        data.update(overrides)
        return data

    def test_seeded_annual_policy_has_staffing_defaults(self):
        policy = get_active_policy(self.annual)
        self.assertTrue(policy.overlap_control_enabled)
        self.assertTrue(policy.reliever_required)
        self.assertEqual(policy.maximum_people_absent, 1)
        sick_policy = get_active_policy(self.sick)
        self.assertFalse(sick_policy.overlap_control_enabled)
        self.assertFalse(sick_policy.reliever_required)

    def test_half_day_rejected_when_policy_disallows(self):
        self.client.force_authenticate(self.employee)
        resp = self.client.post(
            self.list_url,
            self._payload(
                end_date=self.start.isoformat(),
                is_half_day=True,
                half_day_period=HalfDayPeriod.AM,
            ),
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("is_half_day", resp.data)

    def test_half_day_allowed_consumes_half(self):
        policy = get_active_policy(self.annual)
        draft = clone_leave_policy(policy, actor=self.hr_user)
        draft.half_day_allowed = True
        draft.save(update_fields=["half_day_allowed"])
        publish_leave_policy(draft, actor=self.hr_user, reason="Enable half day")

        self.client.force_authenticate(self.employee)
        resp = self.client.post(
            self.list_url,
            self._payload(
                end_date=self.start.isoformat(),
                is_half_day=True,
                half_day_period="AM",
            ),
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        req = LeaveRequest.objects.get(employee=self.employee, is_half_day=True)
        self.assertTrue(req.is_half_day)
        self.assertEqual(req.half_day_period, "AM")
        self.assertEqual(req.total_working_days, Decimal("0.5"))

    def test_submit_reserves_pending_and_approve_moves_to_used(self):
        self.client.force_authenticate(self.employee)
        create = self.client.post(self.list_url, self._payload(), format="json")
        self.assertEqual(create.status_code, status.HTTP_201_CREATED, create.data)
        request_id = self._created_id(create)
        submit = self.client.post(reverse("leave-request-submit", args=[request_id]))
        self.assertEqual(submit.status_code, status.HTTP_200_OK, submit.data)

        balance = LeaveBalance.objects.get(
            employee=self.employee, leave_type=self.annual, year=self.start.year
        )
        req = LeaveRequest.objects.get(pk=request_id)
        self.assertEqual(balance.pending_days, req.total_working_days)
        self.assertEqual(balance.used_days, Decimal("0"))
        self.assertTrue(
            LeaveBalanceTransaction.objects.filter(
                leave_request=req, transaction_type=BalanceTransactionType.RESERVE
            ).exists()
        )

        self.client.force_authenticate(self.line_manager)
        approve = self.client.post(reverse("leave-request-approve", args=[request_id]))
        self.assertEqual(approve.status_code, status.HTTP_200_OK)
        # Not final yet — still pending HR; pending should remain until APPROVED
        balance.refresh_from_db()
        self.assertEqual(balance.pending_days, req.total_working_days)
        self.assertEqual(balance.used_days, Decimal("0"))

    def test_final_approve_consumes_pending(self):
        self.client.force_authenticate(self.employee)
        create = self.client.post(self.list_url, self._payload(), format="json")
        self.assertEqual(create.status_code, status.HTTP_201_CREATED, create.data)
        request_id = self._created_id(create)
        self.client.post(reverse("leave-request-submit", args=[request_id]))
        req = LeaveRequest.objects.get(pk=request_id)
        days = req.total_working_days

        for user in (self.line_manager, self.hr_user):
            self.client.force_authenticate(user)
            self.client.post(reverse("leave-request-approve", args=[request_id]))

        ed = self._create_user(
            "ed-s2@test.com",
            [RoleName.EXECUTIVE_DIRECTOR, RoleName.LINE_MANAGER],
            self.hr_department,
        )
        self.client.force_authenticate(ed)
        final = self.client.post(reverse("leave-request-approve", args=[request_id]))
        self.assertEqual(final.status_code, status.HTTP_200_OK, final.data)
        self.assertEqual(final.data["status"], LeaveRequestStatus.APPROVED)

        balance = LeaveBalance.objects.get(
            employee=self.employee, leave_type=self.annual, year=self.start.year
        )
        self.assertEqual(balance.used_days, days)
        self.assertEqual(balance.pending_days, Decimal("0"))
        self.assertEqual(balance.available_days, Decimal("21") - days)

    def test_pending_hold_blocks_second_request(self):
        LeaveBalance.objects.update_or_create(
            employee=self.employee,
            leave_type=self.annual,
            year=self.start.year,
            defaults={"allocated_days": 5, "used_days": 0, "pending_days": 0},
        )
        self.client.force_authenticate(self.employee)
        create = self.client.post(self.list_url, self._payload(), format="json")
        self.assertEqual(create.status_code, status.HTTP_201_CREATED, create.data)
        self.client.post(reverse("leave-request-submit", args=[self._created_id(create)]))

        later_start = self.end + datetime.timedelta(days=7)
        while later_start.weekday() >= 5:
            later_start += datetime.timedelta(days=1)
        later_end = later_start + datetime.timedelta(days=4)
        second = self.client.post(
            self.list_url,
            self._payload(start_date=later_start.isoformat(), end_date=later_end.isoformat()),
            format="json",
        )
        self.assertEqual(second.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("leave_balance", second.data)

    def test_reject_releases_pending(self):
        self.client.force_authenticate(self.employee)
        create = self.client.post(self.list_url, self._payload(), format="json")
        self.assertEqual(create.status_code, status.HTTP_201_CREATED, create.data)
        request_id = self._created_id(create)
        self.client.post(reverse("leave-request-submit", args=[request_id]))

        self.client.force_authenticate(self.line_manager)
        reject = self.client.post(
            reverse("leave-request-reject", args=[request_id]),
            {"comment": "Not now"},
            format="json",
        )
        self.assertEqual(reject.status_code, status.HTTP_200_OK)
        balance = LeaveBalance.objects.get(
            employee=self.employee, leave_type=self.annual, year=self.start.year
        )
        self.assertEqual(balance.pending_days, Decimal("0"))
        self.assertEqual(balance.used_days, Decimal("0"))
        self.assertTrue(
            LeaveBalanceTransaction.objects.filter(
                leave_request_id=request_id,
                transaction_type=BalanceTransactionType.RELEASE,
            ).exists()
        )

    def test_cancel_approved_refunds_used_not_pending(self):
        req = LeaveRequest.objects.create(
            employee=self.employee,
            leave_type=self.annual,
            cover_person=self.colleague,
            start_date=self.start,
            end_date=self.end,
            status=LeaveRequestStatus.APPROVED,
        )
        deduct_leave_balance(req, actor=self.hr_user, reason="test deduct")
        balance = LeaveBalance.objects.get(
            employee=self.employee, leave_type=self.annual, year=self.start.year
        )
        used_before = balance.used_days
        self.assertGreater(used_before, 0)
        self.assertEqual(balance.pending_days, Decimal("0"))

        restore_leave_balance(req, actor=self.hr_user, reason="cancel")
        balance.refresh_from_db()
        self.assertEqual(balance.used_days, Decimal("0"))
        self.assertEqual(balance.pending_days, Decimal("0"))
        self.assertEqual(
            LeaveBalanceTransaction.objects.filter(
                leave_request=req, transaction_type=BalanceTransactionType.REFUND
            ).count(),
            1,
        )

    def test_overlap_disabled_allows_two_annual(self):
        policy = get_active_policy(self.annual)
        draft = clone_leave_policy(policy, actor=self.hr_user)
        draft.overlap_control_enabled = False
        draft.save(update_fields=["overlap_control_enabled"])
        publish_leave_policy(draft, actor=self.hr_user, reason="Disable overlap")

        LeaveBalance.objects.update_or_create(
            employee=self.colleague,
            leave_type=self.annual,
            year=self.start.year,
            defaults={"allocated_days": 21, "used_days": 0, "pending_days": 0},
        )
        LeaveRequest.objects.create(
            employee=self.colleague,
            leave_type=self.annual,
            start_date=self.start,
            end_date=self.end,
            status=LeaveRequestStatus.APPROVED,
        )
        WorkingDaysService.check_department_leave_overlap(
            employee=self.employee,
            start_date=self.start,
            end_date=self.end,
            leave_type=self.annual,
        )

    def test_overlap_warn_does_not_block(self):
        policy = get_active_policy(self.annual)
        draft = clone_leave_policy(policy, actor=self.hr_user)
        draft.overlap_enforcement = OverlapEnforcement.WARN
        draft.save(update_fields=["overlap_enforcement"])
        publish_leave_policy(draft, actor=self.hr_user, reason="Warn only")

        LeaveRequest.objects.create(
            employee=self.colleague,
            leave_type=self.annual,
            start_date=self.start,
            end_date=self.end,
            status=LeaveRequestStatus.APPROVED,
        )
        warnings = WorkingDaysService.check_department_leave_overlap(
            employee=self.employee,
            start_date=self.start,
            end_date=self.end,
            leave_type=self.annual,
        )
        self.assertTrue(warnings)

    def test_reliever_required_false_on_annual(self):
        policy = get_active_policy(self.annual)
        draft = clone_leave_policy(policy, actor=self.hr_user)
        draft.reliever_required = False
        draft.save(update_fields=["reliever_required"])
        publish_leave_policy(draft, actor=self.hr_user, reason="No reliever")

        preview = LeaveRequest(
            employee=self.employee,
            leave_type=self.annual,
            start_date=self.start,
            end_date=self.end,
            is_emergency=False,
        )
        self.assertFalse(reliever_required(preview))

    def test_in_flight_personal_overlap_blocked(self):
        LeaveRequest.objects.create(
            employee=self.employee,
            leave_type=self.annual,
            start_date=self.start,
            end_date=self.end,
            status=LeaveRequestStatus.PENDING_MANAGER,
        )
        with self.assertRaises(ValidationError):
            WorkingDaysService.check_overlapping_leave(
                self.employee, self.start, self.end
            )

    def test_format_leave_days(self):
        self.assertEqual(format_leave_days(Decimal("5.00")), "5")
        self.assertEqual(format_leave_days(Decimal("0.50")), "0.5")

    def test_balance_api_exposes_pending_and_available(self):
        balance = LeaveBalance.objects.get(
            employee=self.employee, leave_type=self.annual, year=self.start.year
        )
        balance.pending_days = Decimal("2.50")
        balance.used_days = Decimal("1.00")
        balance.save(update_fields=["pending_days", "used_days"])
        self.client.force_authenticate(self.employee)
        resp = self.client.get(reverse("leave-balance-list"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rows = resp.data if isinstance(resp.data, list) else resp.data.get("results", [])
        row = next(
            item
            for item in rows
            if item["leave_type"]["code"] == "ANNUAL" and item["year"] == self.start.year
        )
        self.assertEqual(Decimal(str(row["pending_days"])), Decimal("2.50"))
        self.assertIn("available_days", row)


class ReserveReleaseUnitTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name="Unit S2")
        self.user = User.objects.create_user(email="unit-s2@test.com", password="x")
        self.user.department = self.dept
        self.user.save()
        self.annual = LeaveType.objects.get(code=LeaveType.Code.ANNUAL)
        self.start = datetime.date(2036, 6, 2)
        while self.start.weekday() >= 5:
            self.start += datetime.timedelta(days=1)
        self.end = self.start + datetime.timedelta(days=1)
        LeaveBalance.objects.create(
            employee=self.user,
            leave_type=self.annual,
            year=self.start.year,
            allocated_days=10,
            used_days=0,
            pending_days=0,
        )

    def test_reserve_then_release_is_idempotent(self):
        req = LeaveRequest.objects.create(
            employee=self.user,
            leave_type=self.annual,
            start_date=self.start,
            end_date=self.end,
            status=LeaveRequestStatus.PENDING_MANAGER,
        )
        reserve_leave_balance(req)
        reserve_leave_balance(req)
        balance = LeaveBalance.objects.get(
            employee=self.user, leave_type=self.annual, year=self.start.year
        )
        self.assertEqual(
            LeaveBalanceTransaction.objects.filter(
                leave_request=req, transaction_type=BalanceTransactionType.RESERVE
            ).count(),
            1,
        )
        self.assertEqual(balance.pending_days, req.total_working_days)
        release_leave_balance(req)
        release_leave_balance(req)
        balance.refresh_from_db()
        self.assertEqual(balance.pending_days, Decimal("0"))
        self.assertEqual(
            LeaveBalanceTransaction.objects.filter(
                leave_request=req, transaction_type=BalanceTransactionType.RELEASE
            ).count(),
            1,
        )
