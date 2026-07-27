import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from .utils import calculate_working_days

# ---------------------------------------------------------------------------
# Mixins
# ---------------------------------------------------------------------------

class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# ---------------------------------------------------------------------------
# LeaveType
# ---------------------------------------------------------------------------

class LeaveType(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    default_days = models.PositiveIntegerField()

    class Meta:
        verbose_name = "Leave Type"
        verbose_name_plural = "Leave Types"
        ordering = ["name"]

    def __str__(self):
        return self.name


# ---------------------------------------------------------------------------
# LeavePolicy
# ---------------------------------------------------------------------------

class LeavePolicy(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    leave_type = models.ForeignKey(
        LeaveType, on_delete=models.CASCADE, related_name="policies"
    )
    annual_entitlement = models.PositiveIntegerField()
    carry_forward = models.BooleanField(default=False)
    half_day_allowed = models.BooleanField(default=False)
    weekend_excluded = models.BooleanField(default=True)
    public_holiday_excluded = models.BooleanField(default=True)
    forfeited_on_resignation = models.BooleanField(default=True)
    allow_backdated = models.BooleanField(
        default=True,
        help_text="When False, reconciled leave cannot start before today.",
    )
    maximum_backdate_days = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Max days in the past for reconciled leave start_date. Null = unlimited.",
    )

    class Meta:
        verbose_name = "Leave Policy"
        verbose_name_plural = "Leave Policies"
        ordering = ["leave_type__name"]

    def __str__(self):
        return f"Policy — {self.leave_type.name}"


# ---------------------------------------------------------------------------
# PublicHoliday
# ---------------------------------------------------------------------------

class PublicHoliday(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=150)
    date = models.DateField(unique=True)
    is_recurring = models.BooleanField(
        default=False,
        help_text="If True, this holiday recurs on the same calendar date every year.",
    )

    class Meta:
        verbose_name = "Public Holiday"
        verbose_name_plural = "Public Holidays"
        ordering = ["date"]

    def __str__(self):
        return f"{self.name} ({self.date})"


# ---------------------------------------------------------------------------
# LeaveBalance
# ---------------------------------------------------------------------------

class LeaveBalance(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="leave_balances",
    )
    leave_type = models.ForeignKey(
        LeaveType, on_delete=models.CASCADE, related_name="balances"
    )
    year = models.PositiveIntegerField()
    allocated_days = models.PositiveIntegerField()
    used_days = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "Leave Balance"
        verbose_name_plural = "Leave Balances"
        unique_together = ("employee", "leave_type", "year")
        ordering = ["-year", "leave_type__name"]

    @property
    def remaining_days(self) -> int:
        return self.allocated_days - self.used_days

    def __str__(self):
        return (
            f"{self.employee.email} | {self.leave_type.name} | "
            f"{self.year} — {self.remaining_days} day(s) remaining"
        )


# ---------------------------------------------------------------------------
# LeaveRequest
# ---------------------------------------------------------------------------

class LeaveRequestStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    PENDING_TEAM_LEAD = "PENDING_TEAM_LEAD", "Pending Team Lead"
    PENDING_SUPERVISOR = "PENDING_SUPERVISOR", "Pending Supervisor"
    PENDING_MANAGER = "PENDING_MANAGER", "Pending Manager"
    PENDING_HR = "PENDING_HR", "Pending HR"
    PENDING_ED = "PENDING_ED", "Pending Executive Director"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    CANCELLED = "CANCELLED", "Cancelled"


class LeaveRequest(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="leave_requests",
    )
    leave_type = models.ForeignKey(
        LeaveType, on_delete=models.PROTECT, related_name="requests"
    )
    cover_person = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="covering_leave_requests",
    )
    start_date = models.DateField()
    end_date = models.DateField()
    total_working_days = models.PositiveIntegerField(default=0)
    reason = models.TextField(blank=True)
    is_emergency = models.BooleanField(default=False)
    status = models.CharField(
        max_length=20,
        choices=LeaveRequestStatus.choices,
        default=LeaveRequestStatus.DRAFT,
    )
    skip_hr_stage = models.BooleanField(
        default=False,
        help_text="If True, the manager stage transitions directly to ED (skipping HR).",
    )
    manager_approver_is_management = models.BooleanField(
        default=False,
        help_text="If True, the PENDING_MANAGER approver is Management department line manager.",
    )
    department_reminder_sent_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the 24h-before-start department reminder email was sent.",
    )
    is_reconciled = models.BooleanField(
        default=False,
        help_text="True when HR recorded this leave retroactively without the approval workflow.",
    )
    reconciled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reconciled_leave_requests",
    )
    reconciled_at = models.DateTimeField(null=True, blank=True)
    reconciliation_note = models.TextField(
        blank=True,
        help_text="HR justification for backdated / reconciled leave.",
    )

    class Meta:
        verbose_name = "Leave Request"
        verbose_name_plural = "Leave Requests"
        ordering = ["-created_at"]

    def _compute_working_days(self) -> int:
        """Count working days (excludes weekends and PublicHoliday)."""
        if not (self.start_date and self.end_date):
            return 0
        return calculate_working_days(self.start_date, self.end_date)

    def save(self, *args, **kwargs):
        self.total_working_days = self._compute_working_days()
        super().save(*args, **kwargs)

    def __str__(self):
        return (
            f"{self.employee.email} — {self.leave_type.name} "
            f"({self.start_date} → {self.end_date}) [{self.status}]"
        )


# ---------------------------------------------------------------------------
# LeaveBalanceTransaction — immutable balance audit ledger
# ---------------------------------------------------------------------------

class BalanceTransactionType(models.TextChoices):
    DEDUCT = "DEDUCT", "Deduct"
    REFUND = "REFUND", "Refund"
    ADJUST = "ADJUST", "Adjust"


class BalanceTransactionSource(models.TextChoices):
    APPROVAL = "APPROVAL", "Approval"
    RECONCILE = "RECONCILE", "Reconcile"
    CANCEL_REFUND = "CANCEL_REFUND", "Cancel refund"
    RECONCILE_EDIT = "RECONCILE_EDIT", "Reconcile edit"
    HR_ADJUST = "HR_ADJUST", "HR adjust"


class LeaveBalanceTransaction(models.Model):
    """
    Immutable ledger row for every change to LeaveBalance.used_days.
    delta_used_days: positive increases used_days (deduction), negative decreases (refund).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    leave_balance = models.ForeignKey(
        LeaveBalance,
        on_delete=models.PROTECT,
        related_name="transactions",
    )
    leave_request = models.ForeignKey(
        LeaveRequest,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="balance_transactions",
    )
    transaction_type = models.CharField(
        max_length=10,
        choices=BalanceTransactionType.choices,
    )
    source = models.CharField(
        max_length=20,
        choices=BalanceTransactionSource.choices,
    )
    delta_used_days = models.IntegerField(
        help_text="Change applied to used_days (positive = deduct, negative = refund).",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="leave_balance_transactions",
    )
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Leave Balance Transaction"
        verbose_name_plural = "Leave Balance Transactions"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["leave_request"],
                condition=models.Q(transaction_type=BalanceTransactionType.REFUND),
                name="unique_refund_per_leave_request",
            ),
        ]
        indexes = [
            models.Index(fields=["leave_request", "transaction_type"]),
            models.Index(fields=["leave_balance", "-created_at"]),
        ]

    def __str__(self):
        sign = "+" if self.delta_used_days >= 0 else ""
        return (
            f"{self.transaction_type} {sign}{self.delta_used_days}d "
            f"on balance {self.leave_balance_id}"
        )


# ---------------------------------------------------------------------------
# LeaveApprovalLog
# ---------------------------------------------------------------------------

class ApprovalAction(models.TextChoices):
    APPROVE = "APPROVE", "Approve"
    REJECT = "REJECT", "Reject"
    CANCEL = "CANCEL", "Cancel"
    MODIFY = "MODIFY", "Modify"
    RECONCILE = "RECONCILE", "Reconcile"


class LeaveApprovalLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    leave_request = models.ForeignKey(
        LeaveRequest, on_delete=models.CASCADE, related_name="logs"
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="approval_actions",
    )
    action = models.CharField(max_length=10, choices=ApprovalAction.choices)
    comment = models.TextField(blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    previous_status = models.CharField(
        max_length=20, choices=LeaveRequestStatus.choices, blank=True
    )
    new_status = models.CharField(
        max_length=20, choices=LeaveRequestStatus.choices, blank=True
    )

    class Meta:
        verbose_name = "Leave Approval Log"
        verbose_name_plural = "Leave Approval Logs"
        ordering = ["timestamp"]

    def __str__(self):
        return (
            f"{self.actor} {self.action} on "
            f"request #{self.leave_request_id} at {self.timestamp:%Y-%m-%d %H:%M}"
        )
