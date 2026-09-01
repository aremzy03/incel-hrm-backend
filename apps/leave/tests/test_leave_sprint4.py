import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Department, Role, RoleName, UserRole
from apps.leave.models import (
    AccrualMethod,
    AssignmentScopeType,
    BalanceTransactionType,
    LeaveBalance,
    LeaveBalanceTransaction,
    LeavePolicy,
    LeavePolicyAssignment,
    LeavePolicyStatus,
    LeaveType,
)
from apps.leave.services import (
    accrue_for_interval,
    accrue_for_year,
    apply_carry_forward,
    forfeit_balances_on_termination,
    get_active_policy,
    preview_or_run_accrual,
    prorate_entitlement,
    publish_leave_policy,
    quantize_leave_days,
)
from apps.leave.tasks import run_leave_year_rollover

User = get_user_model()


class AccrualPureFunctionTests(TestCase):
    def test_prorate_full_year_joiner(self):
        self.assertEqual(
            prorate_entitlement(21, datetime.date(2026, 1, 1), 2026, enabled=True),
            Decimal("21.00"),
        )

    def test_prorate_mid_year_joiner(self):
        # 2026 is not a leap year; 1 Jul–31 Dec = 184 days.
        expected = quantize_leave_days(Decimal("21") * Decimal("184") / Decimal("365"))
        self.assertEqual(
            prorate_entitlement(21, datetime.date(2026, 7, 1), 2026, enabled=True),
            expected,
        )

    def test_prorate_disabled_returns_full(self):
        self.assertEqual(
            prorate_entitlement(21, datetime.date(2026, 7, 1), 2026, enabled=False),
            Decimal("21.00"),
        )

    def test_accrue_for_year_matches_prorate(self):
        join = datetime.date(2026, 7, 1)
        self.assertEqual(
            accrue_for_year(24, join_date=join, year=2026, prorate_new_joiners=True),
            prorate_entitlement(24, join, 2026, enabled=True),
        )

    def test_monthly_interval_is_twelfth(self):
        amount = accrue_for_interval(
            12,
            method=AccrualMethod.MONTHLY,
            year=2026,
            month=3,
            join_date=datetime.date(2025, 1, 1),
        )
        self.assertEqual(amount, Decimal("1.00"))

    def test_carry_forward_cap(self):
        self.assertEqual(
            apply_carry_forward(Decimal("10"), allowed=True, max_days=5),
            Decimal("5.00"),
        )
        self.assertEqual(
            apply_carry_forward(Decimal("3"), allowed=True, max_days=5),
            Decimal("3.00"),
        )
        self.assertEqual(
            apply_carry_forward(Decimal("10"), allowed=False, max_days=5),
            Decimal("0.00"),
        )


class AccrualJobTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name="Sprint4 Accrual")
        self.annual = LeaveType.objects.get(code=LeaveType.Code.ANNUAL)
        self.policy = get_active_policy(self.annual)
        LeavePolicy.objects.filter(pk=self.policy.pk).update(
            accrual_method=AccrualMethod.UPFRONT,
            prorate_new_joiners=False,
            carry_forward=True,
            carry_forward_max_days=Decimal("5.00"),
            carry_forward_expiry_months=3,
            forfeited_on_resignation=True,
            effective_from=datetime.date(2020, 1, 1),
            effective_to=None,
        )
        self.policy.refresh_from_db()
        self.employee = User.objects.create_user(
            email="accrual-s4@test.com", password="testpass123"
        )
        self.employee.department = self.dept
        self.employee.date_joined = timezone.make_aware(
            datetime.datetime(2025, 1, 15, 9, 0, 0)
        )
        self.employee.save()
        LeaveBalance.objects.filter(employee=self.employee).delete()
        LeaveBalance.objects.create(
            employee=self.employee,
            leave_type=self.annual,
            year=2025,
            allocated_days=Decimal("21.00"),
            used_days=Decimal("10.00"),
            pending_days=Decimal("0.00"),
        )

    def test_rollover_carry_forward_cap_and_accrual(self):
        result = preview_or_run_accrual(
            as_of=datetime.date(2026, 1, 1),
            year=2026,
            include_rollover=True,
            include_monthly=False,
            include_weekly=False,
            include_anniversary=False,
            include_carry_expiry=False,
            dry_run=False,
        )
        self.assertGreaterEqual(result["action_count"], 1)
        bal_2026 = LeaveBalance.objects.get(
            employee=self.employee, leave_type=self.annual, year=2026
        )
        # 21 unused was 11; cap 5 carried + full 2026 entitlement (policy annual).
        entitlement = Decimal(self.policy.annual_entitlement)
        self.assertEqual(bal_2026.carried_forward_days, Decimal("5.00"))
        self.assertEqual(bal_2026.allocated_days, entitlement + Decimal("5.00"))
        self.assertEqual(bal_2026.carry_forward_expires_on, datetime.date(2026, 3, 31))
        cf = LeaveBalanceTransaction.objects.filter(
            leave_balance=bal_2026,
            transaction_type=BalanceTransactionType.CARRY_FORWARD,
        )
        self.assertEqual(cf.count(), 1)
        acc = LeaveBalanceTransaction.objects.filter(
            leave_balance=bal_2026,
            transaction_type=BalanceTransactionType.ACCRUAL,
        )
        self.assertEqual(acc.count(), 1)

    def test_idempotent_double_rollover(self):
        kwargs = dict(
            as_of=datetime.date(2026, 1, 1),
            year=2026,
            include_rollover=True,
            include_monthly=False,
            include_weekly=False,
            include_anniversary=False,
            include_carry_expiry=False,
            dry_run=False,
        )
        preview_or_run_accrual(**kwargs)
        preview_or_run_accrual(**kwargs)
        bal = LeaveBalance.objects.get(
            employee=self.employee, leave_type=self.annual, year=2026
        )
        entitlement = Decimal(self.policy.annual_entitlement)
        self.assertEqual(bal.allocated_days, entitlement + Decimal("5.00"))
        self.assertEqual(
            LeaveBalanceTransaction.objects.filter(
                leave_balance=bal, transaction_type=BalanceTransactionType.ACCRUAL
            ).count(),
            1,
        )
        self.assertEqual(
            LeaveBalanceTransaction.objects.filter(
                leave_balance=bal, transaction_type=BalanceTransactionType.CARRY_FORWARD
            ).count(),
            1,
        )

    def test_expiry_when_carry_forward_disabled(self):
        LeavePolicy.objects.filter(pk=self.policy.pk).update(carry_forward=False)
        self.policy.refresh_from_db()
        preview_or_run_accrual(
            as_of=datetime.date(2026, 1, 1),
            year=2026,
            include_rollover=True,
            include_monthly=False,
            include_weekly=False,
            include_anniversary=False,
            include_carry_expiry=False,
            dry_run=False,
        )
        prior = LeaveBalance.objects.get(
            employee=self.employee, leave_type=self.annual, year=2025
        )
        self.assertEqual(prior.allocated_days, Decimal("10.00"))
        self.assertTrue(
            LeaveBalanceTransaction.objects.filter(
                leave_balance=prior,
                transaction_type=BalanceTransactionType.EXPIRY,
            ).exists()
        )
        nxt = LeaveBalance.objects.get(
            employee=self.employee, leave_type=self.annual, year=2026
        )
        self.assertEqual(nxt.carried_forward_days, Decimal("0.00"))
        self.assertEqual(nxt.allocated_days, Decimal(self.policy.annual_entitlement))

    def test_employee_assignment_entitlement(self):
        LeavePolicy.objects.filter(pk=self.policy.pk).update(carry_forward=False)
        special = LeavePolicy.objects.create(
            name="S4 Exception Annual",
            leave_type=self.annual,
            status=LeavePolicyStatus.DRAFT,
            annual_entitlement=30,
            accrual_method=AccrualMethod.UPFRONT,
            carry_forward=False,
            effective_from=datetime.date(2026, 1, 1),
        )
        special = publish_leave_policy(special, actor=self.employee, keep_existing_active=True)
        LeavePolicyAssignment.objects.create(
            policy=special,
            scope_type=AssignmentScopeType.EMPLOYEE,
            employee=self.employee,
            scope_id=str(self.employee.pk),
            effective_from=datetime.date(2026, 1, 1),
            is_active=True,
        )
        preview_or_run_accrual(
            as_of=datetime.date(2026, 1, 1),
            year=2026,
            include_rollover=True,
            include_monthly=False,
            include_weekly=False,
            include_anniversary=False,
            include_carry_expiry=False,
            dry_run=False,
        )
        nxt = LeaveBalance.objects.get(
            employee=self.employee, leave_type=self.annual, year=2026
        )
        self.assertEqual(nxt.allocated_days, Decimal("30.00"))

    def test_proration_on_rollover(self):
        LeavePolicy.objects.filter(pk=self.policy.pk).update(
            prorate_new_joiners=True,
            carry_forward=False,
            annual_entitlement=21,
        )
        joiner = User.objects.create_user(email="joiner-s4@test.com", password="x")
        joiner.date_joined = timezone.make_aware(datetime.datetime(2026, 7, 1, 8, 0, 0))
        joiner.save()
        LeaveBalance.objects.filter(employee=joiner).delete()
        preview_or_run_accrual(
            as_of=datetime.date(2026, 1, 1),
            year=2026,
            include_rollover=True,
            include_monthly=False,
            include_weekly=False,
            include_anniversary=False,
            include_carry_expiry=False,
            dry_run=False,
        )
        expected = prorate_entitlement(21, datetime.date(2026, 7, 1), 2026, enabled=True)
        bal = LeaveBalance.objects.get(employee=joiner, leave_type=self.annual, year=2026)
        self.assertEqual(bal.allocated_days, expected)

    def test_carry_forward_expiry_job(self):
        LeaveBalance.objects.create(
            employee=self.employee,
            leave_type=self.annual,
            year=2026,
            allocated_days=Decimal("26.00"),
            used_days=Decimal("0.00"),
            carried_forward_days=Decimal("5.00"),
            carry_forward_expires_on=datetime.date(2026, 3, 31),
        )
        preview_or_run_accrual(
            as_of=datetime.date(2026, 4, 1),
            year=2026,
            include_rollover=False,
            include_monthly=False,
            include_weekly=False,
            include_anniversary=False,
            include_carry_expiry=True,
            dry_run=False,
        )
        bal = LeaveBalance.objects.get(
            employee=self.employee, leave_type=self.annual, year=2026
        )
        self.assertEqual(bal.allocated_days, Decimal("21.00"))
        self.assertEqual(bal.carried_forward_days, Decimal("0.00"))
        self.assertTrue(
            LeaveBalanceTransaction.objects.filter(
                leave_balance=bal, transaction_type=BalanceTransactionType.EXPIRY
            ).exists()
        )

    def test_beat_task_double_run(self):
        run_leave_year_rollover(year=2026, dry_run=False)
        run_leave_year_rollover(year=2026, dry_run=False)
        self.assertEqual(
            LeaveBalance.objects.filter(
                employee=self.employee, leave_type=self.annual, year=2026
            ).count(),
            1,
        )

    def test_forfeit_on_termination(self):
        bal = LeaveBalance.objects.get(
            employee=self.employee, leave_type=self.annual, year=2025
        )
        forfeit_balances_on_termination(self.employee)
        # year is 2026 in the environment; forfeit looks at current year.
        # Seed a current-year row and deactivate.
        this_year = datetime.date.today().year
        current, _ = LeaveBalance.objects.get_or_create(
            employee=self.employee,
            leave_type=self.annual,
            year=this_year,
            defaults={
                "allocated_days": Decimal("10.00"),
                "used_days": Decimal("2.00"),
            },
        )
        if current.allocated_days != Decimal("10.00"):
            current.allocated_days = Decimal("10.00")
            current.used_days = Decimal("2.00")
            current.pending_days = Decimal("0.00")
            current.save()
        forfeit_balances_on_termination(self.employee)
        current.refresh_from_db()
        self.assertEqual(current.available_days, Decimal("0.00"))
        self.assertTrue(
            LeaveBalanceTransaction.objects.filter(
                leave_balance=current,
                transaction_type=BalanceTransactionType.FORFEIT,
            ).exists()
        )
        self.employee.is_active = False
        self.employee.save()
        current.refresh_from_db()
        self.assertEqual(
            LeaveBalanceTransaction.objects.filter(
                leave_balance=current,
                transaction_type=BalanceTransactionType.FORFEIT,
            ).count(),
            1,
        )


class AccrualPreviewApiTests(APITestCase):
    def setUp(self):
        self.hr_dept, _ = Department.objects.get_or_create(name="Human Resources (HR)")
        self.hr = User.objects.create_user(email="hr-s4@test.com", password="testpass123")
        self.hr.department = self.hr_dept
        self.hr.save()
        role = Role.objects.get(name=RoleName.HR)
        UserRole.objects.get_or_create(user=self.hr, role=role)
        self.employee = User.objects.create_user(
            email="emp-s4-api@test.com", password="testpass123"
        )

    def test_hr_preview_dry_run(self):
        self.client.force_authenticate(self.hr)
        url = reverse("leave-accrual-preview")
        response = self.client.post(url, {"year": 2027, "include_monthly": False}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["dry_run"])
        self.assertEqual(response.data["year"], 2027)

    def test_employee_forbidden(self):
        self.client.force_authenticate(self.employee)
        url = reverse("leave-accrual-preview")
        response = self.client.post(url, {"year": 2027}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
