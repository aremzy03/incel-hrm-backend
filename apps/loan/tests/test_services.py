import datetime
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.accounts.models import ConfirmationStatus
from apps.loan.models import (
    LoanApplication,
    LoanApplicationStatus,
    LoanRepaymentPaymentStatus,
    LoanType,
)
from apps.loan.services import LoanEligibilityService, _add_months


def make_user(email="employee@test.com", *, confirmed=True):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    return User.objects.create_user(
        email=email,
        password="testpass123",
        confirmation_status=(
            ConfirmationStatus.CONFIRMED
            if confirmed
            else ConfirmationStatus.PENDING
        ),
    )


def make_loan_type():
    return LoanType.objects.get_or_create(
        name="Personal Loan",
        defaults={"description": "Test loan type"},
    )[0]


def make_loan(employee, *, status=LoanApplicationStatus.DRAFT, amount="10000.00", tenure=12):
    return LoanApplication.objects.create(
        employee=employee,
        loan_type=make_loan_type(),
        amount=Decimal(amount),
        tenure_months=tenure,
        purpose="Test purpose",
        status=status,
    )


class CheckEligibilityTests(TestCase):
    def test_unconfirmed_staff_blocked(self):
        employee = make_user(confirmed=False)

        with self.assertRaisesMessage(
            ValidationError,
            "Only confirmed staff may apply for a loan.",
        ):
            LoanEligibilityService.check_eligibility(employee)

    def test_active_loan_blocked(self):
        employee = make_user()
        make_loan(employee, status=LoanApplicationStatus.ACTIVE)

        with self.assertRaisesMessage(
            ValidationError,
            "You already have an active loan.",
        ):
            LoanEligibilityService.check_eligibility(employee)

    def test_eligible_employee_passes(self):
        employee = make_user()
        LoanEligibilityService.check_eligibility(employee)


class GenerateRepaymentScheduleTests(TestCase):
    def setUp(self):
        self.employee = make_user()
        self.loan = make_loan(self.employee, amount="10000.00", tenure=12)
        self.loan.disbursed_at = timezone.make_aware(
            datetime.datetime(2025, 1, 15, 10, 0, 0)
        )
        self.loan.save(update_fields=["disbursed_at", "updated_at"])

    def test_repayment_schedule_generates_correct_count(self):
        created = LoanEligibilityService.generate_repayment_schedule(self.loan)

        self.assertEqual(len(created), 12)
        self.assertEqual(self.loan.repayment_schedule.count(), 12)

    def test_repayment_schedule_due_dates_are_monthly(self):
        LoanEligibilityService.generate_repayment_schedule(self.loan)

        due_dates = list(
            self.loan.repayment_schedule.order_by("installment_number").values_list(
                "due_date", flat=True
            )
        )
        base = datetime.date(2025, 1, 15)
        expected = [_add_months(base, n) for n in range(1, 13)]

        self.assertEqual(due_dates, expected)

    def test_installment_amount_rounds_up(self):
        loan = make_loan(self.employee, amount="1000.00", tenure=3)
        loan.disbursed_at = timezone.now()
        loan.save(update_fields=["disbursed_at", "updated_at"])

        LoanEligibilityService.generate_repayment_schedule(loan)
        loan.refresh_from_db()

        # 1000 / 3 = 333.333… → 333.34
        self.assertEqual(loan.monthly_installment, Decimal("333.34"))
        self.assertTrue(
            all(row.amount_due == Decimal("333.34") for row in loan.repayment_schedule.all())
        )
        self.assertEqual(loan.outstanding_balance, Decimal("1000.00"))


class SyncOverdueInstallmentsTests(TestCase):
    def setUp(self):
        self.employee = make_user()
        self.loan = make_loan(self.employee, status=LoanApplicationStatus.ACTIVE)
        self.loan.disbursed_at = timezone.now()
        self.loan.save(update_fields=["disbursed_at", "updated_at"])
        LoanEligibilityService.generate_repayment_schedule(self.loan)

    def test_pending_past_due_becomes_overdue(self):
        installment = self.loan.repayment_schedule.first()
        installment.due_date = timezone.localdate() - datetime.timedelta(days=1)
        installment.save(update_fields=["due_date", "updated_at"])

        updated = LoanEligibilityService.sync_overdue_installments(loan=self.loan)

        self.assertEqual(updated, 1)
        installment.refresh_from_db()
        self.assertEqual(installment.payment_status, LoanRepaymentPaymentStatus.OVERDUE)

    def test_paid_installment_not_changed(self):
        installment = self.loan.repayment_schedule.first()
        installment.due_date = timezone.localdate() - datetime.timedelta(days=1)
        installment.payment_status = LoanRepaymentPaymentStatus.PAID
        installment.save(update_fields=["due_date", "payment_status", "updated_at"])

        LoanEligibilityService.sync_overdue_installments(loan=self.loan)

        installment.refresh_from_db()
        self.assertEqual(installment.payment_status, LoanRepaymentPaymentStatus.PAID)

    def test_future_due_stays_pending(self):
        installment = self.loan.repayment_schedule.first()
        installment.due_date = timezone.localdate() + datetime.timedelta(days=7)
        installment.save(update_fields=["due_date", "updated_at"])

        LoanEligibilityService.sync_overdue_installments(loan=self.loan)

        installment.refresh_from_db()
        self.assertEqual(installment.payment_status, LoanRepaymentPaymentStatus.PENDING)

    def test_non_active_loan_ignored(self):
        installment = self.loan.repayment_schedule.first()
        installment.due_date = timezone.localdate() - datetime.timedelta(days=1)
        installment.save(update_fields=["due_date", "updated_at"])
        self.loan.status = LoanApplicationStatus.LIQUIDATED
        self.loan.save(update_fields=["status", "updated_at"])

        updated = LoanEligibilityService.sync_overdue_installments(loan=self.loan)

        self.assertEqual(updated, 0)
        installment.refresh_from_db()
        self.assertEqual(installment.payment_status, LoanRepaymentPaymentStatus.PENDING)
