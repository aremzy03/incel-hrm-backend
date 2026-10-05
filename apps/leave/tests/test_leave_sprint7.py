import datetime
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Department, Role, RoleName, UserRole
from apps.leave.models import (
    BalanceTransactionSource,
    BalanceTransactionType,
    LeaveBalance,
    LeaveBalanceTransaction,
    LeaveBlackoutPeriod,
    LeavePolicy,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveType,
)
from apps.leave.services import (
    forfeit_balances_on_termination,
    get_leave_settings,
    settle_balances_on_termination,
)

from django.contrib.auth import get_user_model

User = get_user_model()


class LeaveSprint7HrOpsTests(APITestCase):
    def setUp(self):
        self.password = "testpass123"
        self.department = Department.objects.create(name="Sprint7 Ops")
        self.hr_department, _ = Department.objects.get_or_create(name="Human Resources (HR)")
        self.employee = self._create_user("emp-s7@test.com", [RoleName.EMPLOYEE], self.department)
        self.cover = self._create_user("cover-s7@test.com", [RoleName.EMPLOYEE], self.department)
        self.hr_user = self._create_user("hr-s7@test.com", [RoleName.HR], self.hr_department)
        self.hr_cover = self._create_user("hrcover-s7@test.com", [RoleName.EMPLOYEE], self.hr_department)
        self.annual = LeaveType.objects.get(code=LeaveType.Code.ANNUAL)
        self.year = timezone.now().year + 11
        self.start = self._weekday(datetime.date(self.year, 5, 4))
        self.end = self.start + datetime.timedelta(days=2)
        self.balance, _ = LeaveBalance.objects.update_or_create(
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

    def test_adjust_requires_reason_and_writes_ledger(self):
        url = reverse("leave-balance-adjust", kwargs={"pk": self.balance.pk})
        self.client.force_authenticate(self.employee)
        denied = self.client.post(url, {"delta": "2.00", "reason": "correction"})
        self.assertEqual(denied.status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(self.hr_user)
        missing = self.client.post(url, {"delta": "2.00", "reason": ""})
        self.assertEqual(missing.status_code, status.HTTP_400_BAD_REQUEST)

        ok = self.client.post(
            url,
            {
                "delta": "3.00",
                "reason": "HR correction after payroll audit",
                "effective_date": "2026-01-15",
            },
        )
        self.assertEqual(ok.status_code, status.HTTP_200_OK)
        self.balance.refresh_from_db()
        self.assertEqual(self.balance.allocated_days, Decimal("24.00"))
        txn = LeaveBalanceTransaction.objects.get(
            leave_balance=self.balance,
            transaction_type=BalanceTransactionType.ADJUST,
            source=BalanceTransactionSource.HR_ADJUST,
        )
        self.assertEqual(txn.delta_allocated_days, Decimal("3.00"))
        self.assertEqual(txn.reason, "HR correction after payroll audit")
        self.assertEqual(txn.effective_date.isoformat(), "2026-01-15")
        self.assertEqual(txn.actor_id, self.hr_user.pk)

        ledger = self.client.get(
            reverse("leave-balance-transactions", kwargs={"pk": self.balance.pk})
        )
        self.assertEqual(ledger.status_code, status.HTTP_200_OK)
        self.assertTrue(any(row["transaction_type"] == "ADJUST" for row in ledger.json()))

    def test_blackout_blocks_create_unless_hr_override(self):
        LeaveBlackoutPeriod.objects.create(
            name="Year-end freeze",
            start_date=self.start,
            end_date=self.end,
            enforcement="BLOCK",
        )
        payload = {
            "leave_type": str(self.annual.id),
            "start_date": self.start.isoformat(),
            "end_date": self.end.isoformat(),
            "cover_person": str(self.cover.id),
        }
        self.client.force_authenticate(self.employee)
        blocked = self.client.post(self.list_url, payload)
        self.assertEqual(blocked.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("blackout", str(blocked.json()).lower())

        self.client.force_authenticate(self.hr_user)
        LeaveBalance.objects.update_or_create(
            employee=self.hr_user,
            leave_type=self.annual,
            year=self.start.year,
            defaults={"allocated_days": 21, "used_days": 0, "pending_days": 0},
        )
        settings_row = get_leave_settings()
        settings_row.allow_hr_override = True
        settings_row.save(update_fields=["allow_hr_override"])
        still = self.client.post(
            self.list_url,
            {
                "leave_type": str(self.annual.id),
                "start_date": self.start.isoformat(),
                "end_date": self.end.isoformat(),
                "cover_person": str(self.hr_cover.id),
            },
        )
        self.assertEqual(still.status_code, status.HTTP_400_BAD_REQUEST)
        allowed = self.client.post(
            self.list_url,
            {
                "leave_type": str(self.annual.id),
                "start_date": self.start.isoformat(),
                "end_date": self.end.isoformat(),
                "cover_person": str(self.hr_cover.id),
                "blackout_override_reason": "Critical travel already booked",
            },
        )
        self.assertEqual(allowed.status_code, status.HTTP_201_CREATED)

    def test_encash_not_forfeit_when_policy_does_not_forfeit(self):
        this_year = datetime.date.today().year
        policy = LeavePolicy.objects.filter(
            leave_type=self.annual, status="ACTIVE"
        ).order_by("-version").first()
        policy.forfeited_on_resignation = False
        policy.save(update_fields=["forfeited_on_resignation"])
        settings_row = get_leave_settings()
        settings_row.encashment_allowed = True
        settings_row.encashment_max_days = Decimal("10.00")
        settings_row.save(update_fields=["encashment_allowed", "encashment_max_days"])

        current, _ = LeaveBalance.objects.update_or_create(
            employee=self.employee,
            leave_type=self.annual,
            year=this_year,
            defaults={
                "allocated_days": Decimal("12.00"),
                "used_days": Decimal("2.00"),
                "pending_days": Decimal("0.00"),
            },
        )
        current.allocated_days = Decimal("12.00")
        current.used_days = Decimal("2.00")
        current.pending_days = Decimal("0.00")
        current.save()

        settle_balances_on_termination(self.employee)
        current.refresh_from_db()
        self.assertFalse(
            LeaveBalanceTransaction.objects.filter(
                leave_balance=current,
                transaction_type=BalanceTransactionType.FORFEIT,
            ).exists()
        )
        encash = LeaveBalanceTransaction.objects.get(
            leave_balance=current,
            transaction_type=BalanceTransactionType.ENCASH,
        )
        self.assertEqual(encash.delta_allocated_days, Decimal("-10.00"))
        settle_balances_on_termination(self.employee)
        self.assertEqual(
            LeaveBalanceTransaction.objects.filter(
                leave_balance=current,
                transaction_type=BalanceTransactionType.ENCASH,
            ).count(),
            1,
        )

    def test_forfeit_still_used_when_policy_forfeits(self):
        this_year = datetime.date.today().year
        current, _ = LeaveBalance.objects.update_or_create(
            employee=self.employee,
            leave_type=self.annual,
            year=this_year,
            defaults={
                "allocated_days": Decimal("8.00"),
                "used_days": Decimal("1.00"),
                "pending_days": Decimal("0.00"),
            },
        )
        current.allocated_days = Decimal("8.00")
        current.used_days = Decimal("1.00")
        current.save()
        forfeit_balances_on_termination(self.employee)
        self.assertTrue(
            LeaveBalanceTransaction.objects.filter(
                leave_balance=current,
                transaction_type=BalanceTransactionType.FORFEIT,
            ).exists()
        )
        self.assertFalse(
            LeaveBalanceTransaction.objects.filter(
                leave_balance=current,
                transaction_type=BalanceTransactionType.ENCASH,
            ).exists()
        )

    def test_reports_hr_only_and_csv(self):
        LeaveRequest.objects.create(
            employee=self.employee,
            leave_type=self.annual,
            cover_person=self.cover,
            start_date=datetime.date.today(),
            end_date=datetime.date.today(),
            status=LeaveRequestStatus.APPROVED,
            total_working_days=Decimal("1.00"),
        )
        util_url = reverse("leave-reports", kwargs={"kind": "utilization"})
        self.client.force_authenticate(self.employee)
        self.assertEqual(self.client.get(util_url).status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(self.hr_user)
        util = self.client.get(util_url, {"year": self.start.year})
        self.assertEqual(util.status_code, status.HTTP_200_OK)
        self.assertEqual(util.json()["kind"], "utilization")
        self.assertTrue(len(util.json()["results"]) >= 1)

        out = self.client.get(reverse("leave-reports", kwargs={"kind": "who-is-out"}))
        self.assertEqual(out.status_code, status.HTTP_200_OK)
        self.assertTrue(any(r["employee_email"] == self.employee.email for r in out.json()["results"]))

        csv_resp = self.client.get(
            reverse("leave-reports", kwargs={"kind": "liability"}),
            {"year": self.start.year, "export": "csv"},
        )
        self.assertEqual(csv_resp.status_code, status.HTTP_200_OK)
        self.assertIn("text/csv", csv_resp["Content-Type"])
        self.assertIn("liability_days", csv_resp.content.decode())
