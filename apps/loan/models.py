import uuid

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


# ---------------------------------------------------------------------------
# Mixins
# ---------------------------------------------------------------------------

class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# ---------------------------------------------------------------------------
# LoanType
# ---------------------------------------------------------------------------

class LoanType(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)

    class Meta:
        verbose_name = "Loan Type"
        verbose_name_plural = "Loan Types"
        ordering = ["name"]

    def __str__(self):
        return self.name


# ---------------------------------------------------------------------------
# LoanApplication
# ---------------------------------------------------------------------------

class LoanApplicationStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    PENDING_HR = "PENDING_HR", "Pending HR"
    PENDING_ED = "PENDING_ED", "Pending Executive Director"
    PENDING_MD = "PENDING_MD", "Pending Managing Director"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    ACTIVE = "ACTIVE", "Active"
    CLOSED = "CLOSED", "Closed"
    LIQUIDATED = "LIQUIDATED", "Liquidated"


class LoanApplication(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="loan_applications",
    )
    loan_type = models.ForeignKey(
        LoanType,
        on_delete=models.PROTECT,
        related_name="applications",
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    tenure_months = models.PositiveIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(12)],
    )
    monthly_installment = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Computed on approval.",
    )
    purpose = models.TextField()
    status = models.CharField(
        max_length=20,
        choices=LoanApplicationStatus.choices,
        default=LoanApplicationStatus.DRAFT,
    )
    outstanding_balance = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    disbursed_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    resignation_deducted = models.BooleanField(default=False)

    class Meta:
        verbose_name = "Loan Application"
        verbose_name_plural = "Loan Applications"
        ordering = ["-created_at"]

    def __str__(self):
        return (
            f"{self.employee} — {self.loan_type.name} "
            f"({self.amount}) [{self.status}]"
        )


# ---------------------------------------------------------------------------
# LoanRepaymentSchedule
# ---------------------------------------------------------------------------

class LoanRepaymentPaymentStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    PAID = "PAID", "Paid"
    OVERDUE = "OVERDUE", "Overdue"


class LoanRepaymentSchedule(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    loan = models.ForeignKey(
        LoanApplication,
        on_delete=models.CASCADE,
        related_name="repayment_schedule",
    )
    installment_number = models.PositiveIntegerField()
    due_date = models.DateField(
        help_text="Calculated from disbursed_at month.",
    )
    amount_due = models.DecimalField(max_digits=12, decimal_places=2)
    payment_status = models.CharField(
        max_length=20,
        choices=LoanRepaymentPaymentStatus.choices,
        default=LoanRepaymentPaymentStatus.PENDING,
        db_index=True,
    )

    class Meta:
        verbose_name = "Loan Repayment Schedule Entry"
        verbose_name_plural = "Loan Repayment Schedule Entries"
        unique_together = ("loan", "installment_number")
        ordering = ["loan", "installment_number"]

    def __str__(self):
        return (
            f"Loan {self.loan_id} — installment #{self.installment_number} "
            f"due {self.due_date}"
        )


# ---------------------------------------------------------------------------
# LoanApprovalLog
# ---------------------------------------------------------------------------

class LoanApprovalAction(models.TextChoices):
    SUBMIT = "SUBMIT", "Submit"
    APPROVE = "APPROVE", "Approve"
    REJECT = "REJECT", "Reject"
    DISBURSE = "DISBURSE", "Disburse"
    LIQUIDATE = "LIQUIDATE", "Liquidate"
    CLOSE = "CLOSE", "Close"


class LoanApprovalLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    loan = models.ForeignKey(
        LoanApplication,
        on_delete=models.CASCADE,
        related_name="logs",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="loan_approval_actions",
    )
    action = models.CharField(max_length=10, choices=LoanApprovalAction.choices)
    comment = models.TextField(blank=True)
    previous_status = models.CharField(
        max_length=20,
        choices=LoanApplicationStatus.choices,
        blank=True,
    )
    new_status = models.CharField(
        max_length=20,
        choices=LoanApplicationStatus.choices,
        blank=True,
    )
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Loan Approval Log"
        verbose_name_plural = "Loan Approval Logs"
        ordering = ["timestamp"]

    def __str__(self):
        return (
            f"{self.actor} {self.action} on loan {self.loan_id} "
            f"at {self.timestamp:%Y-%m-%d %H:%M}"
        )
