"""
Business-logic layer for loan management.
"""

import calendar
from datetime import date
from decimal import ROUND_CEILING, Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import (
    LoanApplication,
    LoanApplicationStatus,
    LoanRepaymentPaymentStatus,
    LoanRepaymentSchedule,
)


def _add_months(base: date, months: int) -> date:
    """Return *base* plus *months* calendar months, clamping day to month length."""
    month_index = base.month - 1 + months
    year = base.year + month_index // 12
    month = month_index % 12 + 1
    day = min(base.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _ceil_money(amount: Decimal) -> Decimal:
    return amount.quantize(Decimal("0.01"), rounding=ROUND_CEILING)


class LoanEligibilityService:
    """Eligibility checks and repayment schedule generation for loans."""

    ACTIVE_LOAN_STATUSES = (
        LoanApplicationStatus.APPROVED,
        LoanApplicationStatus.ACTIVE,
    )

    @classmethod
    def check_eligibility(cls, employee) -> None:
        """
        Run all eligibility checks in sequence.

        Raises
        ------
        ValidationError
            On the first failed check.
        """
        if not employee.is_confirmed:
            raise ValidationError("Only confirmed staff may apply for a loan.")

        active = LoanApplication.objects.filter(
            employee=employee,
            status__in=cls.ACTIVE_LOAN_STATUSES,
        ).exists()
        if active:
            raise ValidationError("You already have an active loan.")

        # TODO: Replace with real appraisal check when appraisal module is built.
        # Hook: AppraisalService.get_last_rating(employee) > AVERAGE_THRESHOLD
        is_appraisal_eligible = True  # placeholder — always passes for now
        if not is_appraisal_eligible:
            raise ValidationError(
                "Appraisal rating does not meet loan eligibility."
            )

    @classmethod
    @transaction.atomic
    def generate_repayment_schedule(cls, loan: LoanApplication) -> list[LoanRepaymentSchedule]:
        """
        Build repayment schedule on disbursement (APPROVED → ACTIVE).

        Sets ``monthly_installment``, creates schedule rows, and sets
        ``outstanding_balance`` to the loan principal.
        """
        if not loan.disbursed_at:
            raise ValidationError("Loan must be disbursed before generating a schedule.")

        monthly_installment = _ceil_money(
            Decimal(loan.amount) / Decimal(loan.tenure_months)
        )
        base_date = timezone.localdate(loan.disbursed_at)

        schedule_rows = [
            LoanRepaymentSchedule(
                loan=loan,
                installment_number=n,
                due_date=_add_months(base_date, n),
                amount_due=monthly_installment,
                payment_status=LoanRepaymentPaymentStatus.PENDING,
            )
            for n in range(1, loan.tenure_months + 1)
        ]

        created = LoanRepaymentSchedule.objects.bulk_create(schedule_rows)

        loan.monthly_installment = monthly_installment
        loan.outstanding_balance = loan.amount
        loan.save(update_fields=["monthly_installment", "outstanding_balance", "updated_at"])

        return created

    @classmethod
    def sync_overdue_installments(cls, *, loan=None, loan_id=None) -> int:
        """
        Mark PENDING installments as OVERDUE when due_date is before today.

        Only applies to installments on ACTIVE loans. PAID rows are never changed.
        """
        today = timezone.localdate()
        qs = LoanRepaymentSchedule.objects.filter(
            loan__status=LoanApplicationStatus.ACTIVE,
            payment_status=LoanRepaymentPaymentStatus.PENDING,
            due_date__lt=today,
        )
        if loan_id is not None:
            qs = qs.filter(loan_id=loan_id)
        elif loan is not None:
            qs = qs.filter(loan=loan)
        return qs.update(payment_status=LoanRepaymentPaymentStatus.OVERDUE)

    @classmethod
    def recalculate_outstanding_balance(cls, loan: LoanApplication) -> Decimal:
        """Sum amount_due for installments not marked PAID."""
        unpaid_total = (
            loan.repayment_schedule.exclude(
                payment_status=LoanRepaymentPaymentStatus.PAID
            ).aggregate(total=Sum("amount_due"))["total"]
            or Decimal("0")
        )
        loan.outstanding_balance = unpaid_total
        loan.save(update_fields=["outstanding_balance", "updated_at"])
        return unpaid_total
