import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from .utils import calculate_working_days, slug_leave_type_code


def _code_from_leave_type_name(name: str) -> str:
    known = {
        "Annual": LeaveType.Code.ANNUAL,
        "Sick": LeaveType.Code.SICK,
        "Casual": LeaveType.Code.CASUAL,
        "Maternity": LeaveType.Code.MATERNITY,
        "Maternity Leave": LeaveType.Code.MATERNITY,
        "Paternity": LeaveType.Code.PATERNITY,
        "Paternity Leave": LeaveType.Code.PATERNITY,
    }
    if name in known:
        return known[name]
    return slug_leave_type_code(name)

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
    class Code:
        ANNUAL = "ANNUAL"
        SICK = "SICK"
        CASUAL = "CASUAL"
        MATERNITY = "MATERNITY"
        PATERNITY = "PATERNITY"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True)
    code = models.CharField(
        max_length=32,
        unique=True,
        help_text="Stable machine identifier. Immutable after leave requests exist.",
    )
    description = models.TextField(blank=True)
    default_days = models.PositiveIntegerField(
        help_text="Fallback entitlement when no active LeavePolicy exists.",
    )
    is_active = models.BooleanField(default=True)
    display_order = models.PositiveIntegerField(default=100)
    calendar_color = models.CharField(max_length=16, blank=True)

    class Meta:
        verbose_name = "Leave Type"
        verbose_name_plural = "Leave Types"
        ordering = ["display_order", "name"]

    def __str__(self):
        return f"{self.name} ({self.code})"

    def save(self, *args, **kwargs):
        if not self.code:
            self.code = _code_from_leave_type_name(self.name)
        self.code = self.code.strip().upper().replace("-", "_")
        super().save(*args, **kwargs)


# ---------------------------------------------------------------------------
# LeavePolicy
# ---------------------------------------------------------------------------

class LeavePolicyStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    ACTIVE = "ACTIVE", "Active"
    ARCHIVED = "ARCHIVED", "Archived"


class OverlapScope(models.TextChoices):
    AUTO = "AUTO", "Auto (lowest org level)"
    TEAM = "TEAM", "Team"
    UNIT = "UNIT", "Unit"
    DEPARTMENT = "DEPARTMENT", "Department"
    ORGANIZATION = "ORGANIZATION", "Organization"


class OverlapEnforcement(models.TextChoices):
    BLOCK = "BLOCK", "Block"
    WARN = "WARN", "Warning only"


class HalfDayPeriod(models.TextChoices):
    AM = "AM", "Morning"
    PM = "PM", "Afternoon"


class AccrualMethod(models.TextChoices):
    UPFRONT = "UPFRONT", "Upfront (annual lump sum)"
    MONTHLY = "MONTHLY", "Monthly"
    WEEKLY = "WEEKLY", "Weekly"
    ANNIVERSARY = "ANNIVERSARY", "Employment anniversary"


class LeavePolicy(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200, blank=True)
    leave_type = models.ForeignKey(
        LeaveType, on_delete=models.PROTECT, related_name="policies"
    )
    status = models.CharField(
        max_length=16,
        choices=LeavePolicyStatus.choices,
        default=LeavePolicyStatus.DRAFT,
        db_index=True,
    )
    version = models.PositiveIntegerField(default=0)
    effective_from = models.DateField(null=True, blank=True)
    effective_to = models.DateField(null=True, blank=True)
    annual_entitlement = models.PositiveIntegerField()
    accrual_method = models.CharField(
        max_length=16,
        choices=AccrualMethod.choices,
        default=AccrualMethod.UPFRONT,
        help_text="When entitlement is credited: lump sum, monthly, weekly, or anniversary.",
    )
    accrual_rate = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Days credited per accrual interval. Null = annual_entitlement divided by intervals.",
    )
    prorate_new_joiners = models.BooleanField(
        default=False,
        help_text="When True, first-year entitlement is reduced by remaining calendar days in the leave year.",
    )
    carry_forward = models.BooleanField(
        default=False,
        help_text="When True, unused days may roll into the next leave year (carry_forward_allowed).",
    )
    carry_forward_max_days = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Cap on days carried into the next year. Null = no cap.",
    )
    carry_forward_expiry_months = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Carried days expire this many months after the new leave year starts. Null = no expiry.",
    )
    forfeit_unused = models.BooleanField(
        default=True,
        help_text="When carry-forward is disabled, unused days are expired at year-end.",
    )
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
    reliever_required = models.BooleanField(
        default=True,
        help_text="When True, a cover person is required before submission (MD/ED and emergency still exempt).",
    )
    reliever_scope = models.CharField(
        max_length=16,
        choices=OverlapScope.choices,
        default=OverlapScope.AUTO,
    )
    overlap_control_enabled = models.BooleanField(
        default=False,
        help_text="When True, cap concurrent absences in overlap_scope for this leave type.",
    )
    overlap_scope = models.CharField(
        max_length=16,
        choices=OverlapScope.choices,
        default=OverlapScope.AUTO,
    )
    maximum_people_absent = models.PositiveIntegerField(
        default=1,
        help_text="Maximum other people who may already be absent in the overlap scope.",
    )
    overlap_enforcement = models.CharField(
        max_length=8,
        choices=OverlapEnforcement.choices,
        default=OverlapEnforcement.BLOCK,
    )

    class Meta:
        verbose_name = "Leave Policy"
        verbose_name_plural = "Leave Policies"
        ordering = ["leave_type__display_order", "leave_type__name", "-version"]
        indexes = [
            models.Index(fields=["leave_type", "status", "-version"]),
        ]

    def __str__(self):
        label = self.name or f"Policy — {self.leave_type.name}"
        return f"{label} [{self.status} v{self.version}]"

    @property
    def carry_forward_allowed(self) -> bool:
        return bool(self.carry_forward)

    def save(self, *args, **kwargs):
        if not self.name and self.leave_type_id:
            self.name = f"{self.leave_type.name} Policy"
        super().save(*args, **kwargs)


class AssignmentScopeType(models.TextChoices):
    ORGANIZATION = "ORGANIZATION", "Organization"
    DEPARTMENT = "DEPARTMENT", "Department"
    UNIT = "UNIT", "Unit"
    TEAM = "TEAM", "Team"
    EMPLOYMENT_TYPE = "EMPLOYMENT_TYPE", "Employment type"
    EMPLOYEE = "EMPLOYEE", "Employee"


class LeavePolicyAssignment(TimeStampedModel):
    """Maps an ACTIVE (or soon-to-be published) policy to an employee population."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    policy = models.ForeignKey(
        LeavePolicy, on_delete=models.PROTECT, related_name="assignments"
    )
    scope_type = models.CharField(max_length=32, choices=AssignmentScopeType.choices)
    scope_id = models.CharField(
        max_length=64,
        blank=True,
        help_text="Department/unit/team UUID, or employment-type code. Empty for organization.",
    )
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="leave_policy_assignments",
        help_text="Required when scope_type is EMPLOYEE.",
    )
    priority = models.IntegerField(
        default=0,
        help_text="Higher wins when two assignments share the same specificity.",
    )
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Leave Policy Assignment"
        verbose_name_plural = "Leave Policy Assignments"
        ordering = ["-priority", "-effective_from"]
        indexes = [
            models.Index(fields=["scope_type", "scope_id", "is_active"]),
            models.Index(fields=["policy", "is_active"]),
            models.Index(fields=["employee", "is_active"]),
        ]

    def __str__(self):
        return (
            f"{self.scope_type}:{self.scope_id or self.employee_id} → "
            f"{self.policy_id} [{self.priority}]"
        )


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


class LeaveYearType(models.TextChoices):
    CALENDAR = "CALENDAR", "Calendar year (1 January)"
    FISCAL = "FISCAL", "Fiscal year (configured start date)"
    ANNIVERSARY = "ANNIVERSARY", "Employment anniversary (org jobs still use calendar 1 Jan)"


class CrossYearDeductionRule(models.TextChoices):
    SPLIT = "SPLIT", "Split working days by calendar year (existing reconcile behaviour)"
    START_YEAR = "START_YEAR", "Deduct all days from the start date's calendar year"


class WorkingCalendar(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=150)
    is_active = models.BooleanField(default=True)
    is_org_default = models.BooleanField(default=False)
    timezone = models.CharField(max_length=64, default="Africa/Lagos")
    weekdays = models.JSONField(
        default=list,
        help_text="Python weekday numbers that count as working days (Monday=0 … Sunday=6).",
    )
    hours_per_day = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal("8.00")
    )
    effective_from = models.DateField(null=True, blank=True)
    effective_to = models.DateField(null=True, blank=True)

    class Meta:
        verbose_name = "Working Calendar"
        verbose_name_plural = "Working Calendars"
        ordering = ["name"]

    def __str__(self):
        return self.name


class HolidayCalendar(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=150)
    is_active = models.BooleanField(default=True)
    is_org_default = models.BooleanField(default=False)
    timezone = models.CharField(max_length=64, default="Africa/Lagos")
    effective_from = models.DateField(null=True, blank=True)
    effective_to = models.DateField(null=True, blank=True)

    class Meta:
        verbose_name = "Holiday Calendar"
        verbose_name_plural = "Holiday Calendars"
        ordering = ["name"]

    def __str__(self):
        return self.name


class CalendarHoliday(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    calendar = models.ForeignKey(
        HolidayCalendar, on_delete=models.CASCADE, related_name="holidays"
    )
    name = models.CharField(max_length=150)
    date = models.DateField()
    is_recurring = models.BooleanField(default=False)
    is_full_day = models.BooleanField(default=True)
    observed_date = models.DateField(null=True, blank=True)
    location_scope = models.CharField(
        max_length=150,
        blank=True,
        help_text="Optional country/state/location label. Empty = whole calendar.",
    )

    class Meta:
        verbose_name = "Calendar Holiday"
        verbose_name_plural = "Calendar Holidays"
        ordering = ["date"]
        unique_together = ("calendar", "date")

    def __str__(self):
        return f"{self.name} ({self.date})"


class CalendarAssignment(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    working_calendar = models.ForeignKey(
        WorkingCalendar,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assignments",
    )
    holiday_calendar = models.ForeignKey(
        HolidayCalendar,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assignments",
    )
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="leave_calendar_assignments",
    )
    department = models.ForeignKey(
        "accounts.Department",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="leave_calendar_assignments",
    )
    is_active = models.BooleanField(default=True)
    effective_from = models.DateField(null=True, blank=True)
    effective_to = models.DateField(null=True, blank=True)

    class Meta:
        verbose_name = "Calendar Assignment"
        verbose_name_plural = "Calendar Assignments"
        ordering = ["-created_at"]

    def __str__(self):
        target = self.employee_id or self.department_id or "org"
        return f"Calendar assignment {target}"


class LeaveSettings(TimeStampedModel):
    """Singleton organization leave settings (one row; no legal-entity model exists)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    singleton_key = models.CharField(max_length=16, unique=True, default="default")
    leave_year_type = models.CharField(
        max_length=16,
        choices=LeaveYearType.choices,
        default=LeaveYearType.CALENDAR,
    )
    leave_year_start_month = models.PositiveSmallIntegerField(default=1)
    leave_year_start_day = models.PositiveSmallIntegerField(default=1)
    cross_year_deduction_rule = models.CharField(
        max_length=16,
        choices=CrossYearDeductionRule.choices,
        default=CrossYearDeductionRule.SPLIT,
        help_text="SPLIT reuses split_working_days_by_year(); START_YEAR charges the start year only.",
    )
    default_timezone = models.CharField(max_length=64, default="Africa/Lagos")
    default_working_calendar = models.ForeignKey(
        WorkingCalendar,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    default_holiday_calendar = models.ForeignKey(
        HolidayCalendar,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    notify_applicant_on_submit = models.BooleanField(default=True)
    notify_applicant_on_decision = models.BooleanField(default=True)
    notify_approver = models.BooleanField(default=True)
    notify_reliever = models.BooleanField(default=True)
    notify_department_reminder = models.BooleanField(default=True)
    reminder_lead_hours = models.PositiveIntegerField(
        default=24,
        help_text="Hours before leave start for department reminders (historically 24).",
    )
    allow_hr_override = models.BooleanField(
        default=True,
        help_text="When true, HR may bypass reliever-scope checks (existing behaviour).",
    )
    prevent_self_approval = models.BooleanField(
        default=False,
        help_text="When true, the requester cannot approve their own request. Default off to preserve current routing.",
    )
    approval_sla_hours = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Optional org-wide pending-stage SLA in hours. Null = use stage sla_hours only.",
    )
    encashment_allowed = models.BooleanField(
        default=False,
        help_text="When True, unused days that are not forfeited on termination are recorded as ENCASH for payroll.",
    )
    encashment_max_days = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Cap on days encashed per balance year. Null = no cap.",
    )

    class Meta:
        verbose_name = "Leave Settings"
        verbose_name_plural = "Leave Settings"

    def __str__(self):
        return "Leave settings"


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
    allocated_days = models.DecimalField(max_digits=8, decimal_places=2)
    used_days = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("0.00"))
    pending_days = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Days reserved by in-flight (submitted, not yet approved/rejected) requests.",
    )
    carried_forward_days = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Portion of allocated_days that originated as carry-forward from the prior year.",
    )
    carry_forward_expires_on = models.DateField(
        null=True,
        blank=True,
        help_text="When carried_forward_days expire. Null if none or already expired.",
    )

    class Meta:
        verbose_name = "Leave Balance"
        verbose_name_plural = "Leave Balances"
        unique_together = ("employee", "leave_type", "year")
        ordering = ["-year", "leave_type__name"]

    @property
    def remaining_days(self):
        return self.allocated_days - self.used_days

    @property
    def available_days(self):
        return self.allocated_days - self.used_days - self.pending_days

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
    total_working_days = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal("0.00")
    )
    is_half_day = models.BooleanField(default=False)
    half_day_period = models.CharField(
        max_length=2,
        choices=HalfDayPeriod.choices,
        blank=True,
    )
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
    policy = models.ForeignKey(
        LeavePolicy,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="leave_requests",
        help_text="Policy snapshot taken at submit (or last date-range recalc while draft).",
    )
    policy_version = models.PositiveIntegerField(null=True, blank=True)
    calculation_snapshot = models.JSONField(
        null=True,
        blank=True,
        help_text="Working-day inputs used when total_working_days was last computed. Not rewritten on calendar edits.",
    )
    workflow_snapshot = models.JSONField(
        null=True,
        blank=True,
        help_text="Workflow template + stages captured at submit. Approvals use this, not live template edits.",
    )
    stage_entered_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the request entered the current pending approval status.",
    )
    sla_notified_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When an SLA reminder/escalation was last sent for the current stage.",
    )

    class Meta:
        verbose_name = "Leave Request"
        verbose_name_plural = "Leave Requests"
        ordering = ["-created_at"]

    def _compute_working_days(self):
        """Count working days using the resolved (or snapshotted) policy + calendar."""
        if not (self.start_date and self.end_date):
            return Decimal("0.00")
        from .services import calculate_working_days_for_leave_type, working_day_calculation_snapshot

        employee = self.employee if self.employee_id else None
        days = calculate_working_days_for_leave_type(
            self.start_date,
            self.end_date,
            leave_type=self.leave_type if self.leave_type_id else None,
            on_date=self.start_date,
            is_half_day=self.is_half_day,
            employee=employee,
        )
        self.calculation_snapshot = working_day_calculation_snapshot(
            start_date=self.start_date,
            end_date=self.end_date,
            leave_type=self.leave_type if self.leave_type_id else None,
            employee=employee,
            is_half_day=self.is_half_day,
            total_working_days=days,
        )
        return Decimal(days)

    def save(self, *args, **kwargs):
        # Do not recompute stored totals on status-only saves so policy edits
        # cannot rewrite historical approved request day counts.
        should_recompute = self._state.adding
        if not should_recompute and self.pk:
            previous = (
                LeaveRequest.objects.filter(pk=self.pk)
                .values(
                    "start_date",
                    "end_date",
                    "leave_type_id",
                    "is_half_day",
                    "half_day_period",
                )
                .first()
            )
            if previous is None:
                should_recompute = True
            elif (
                previous["start_date"] != self.start_date
                or previous["end_date"] != self.end_date
                or previous["leave_type_id"] != self.leave_type_id
                or previous["is_half_day"] != self.is_half_day
                or previous["half_day_period"] != self.half_day_period
            ):
                should_recompute = True
        if should_recompute:
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
    RESERVE = "RESERVE", "Reserve"
    RELEASE = "RELEASE", "Release"
    ACCRUAL = "ACCRUAL", "Accrual"
    CARRY_FORWARD = "CARRY_FORWARD", "Carry-forward"
    EXPIRY = "EXPIRY", "Expiry"
    FORFEIT = "FORFEIT", "Forfeit"
    ENCASH = "ENCASH", "Encashment"


class BalanceTransactionSource(models.TextChoices):
    APPROVAL = "APPROVAL", "Approval"
    RECONCILE = "RECONCILE", "Reconcile"
    CANCEL_REFUND = "CANCEL_REFUND", "Cancel refund"
    RECONCILE_EDIT = "RECONCILE_EDIT", "Reconcile edit"
    HR_ADJUST = "HR_ADJUST", "HR adjust"
    SUBMIT = "SUBMIT", "Submit"
    REJECT_RELEASE = "REJECT_RELEASE", "Reject release"
    CANCEL_RELEASE = "CANCEL_RELEASE", "Cancel release"
    ACCRUAL_JOB = "ACCRUAL_JOB", "Accrual job"
    YEAR_ROLLOVER = "YEAR_ROLLOVER", "Year rollover"
    EXPIRY_JOB = "EXPIRY_JOB", "Expiry job"
    TERMINATION = "TERMINATION", "Termination"


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
        max_length=16,
        choices=BalanceTransactionType.choices,
    )
    source = models.CharField(
        max_length=24,
        choices=BalanceTransactionSource.choices,
    )
    delta_used_days = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Change applied to used_days (positive = deduct, negative = refund).",
    )
    delta_pending_days = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Change applied to pending_days (positive = reserve, negative = release/consume).",
    )
    delta_allocated_days = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Change applied to allocated_days (positive = accrual/carry-forward, negative = expiry).",
    )
    idempotency_key = models.CharField(
        max_length=191,
        null=True,
        blank=True,
        unique=True,
        help_text="Stable key so Beat/jobs can re-run without double-crediting.",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="leave_balance_transactions",
    )
    reason = models.TextField(blank=True)
    effective_date = models.DateField(
        null=True,
        blank=True,
        help_text="Optional HR-stated effective date for adjustments / settlements.",
    )
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
            models.UniqueConstraint(
                fields=["leave_request", "leave_balance"],
                condition=models.Q(transaction_type=BalanceTransactionType.RESERVE),
                name="unique_reserve_per_leave_request_balance",
            ),
            models.UniqueConstraint(
                fields=["leave_request", "leave_balance"],
                condition=models.Q(transaction_type=BalanceTransactionType.RELEASE),
                name="unique_release_per_leave_request_balance",
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


# ---------------------------------------------------------------------------
# LeaveSettingsAuditLog — configuration change history
# ---------------------------------------------------------------------------

class SettingsAuditAction(models.TextChoices):
    CREATE = "CREATE", "Create"
    UPDATE = "UPDATE", "Update"
    DELETE = "DELETE", "Delete"
    PUBLISH = "PUBLISH", "Publish"
    ARCHIVE = "ARCHIVE", "Archive"
    ACTIVATE = "ACTIVATE", "Activate"
    DEACTIVATE = "DEACTIVATE", "Deactivate"
    CLONE = "CLONE", "Clone"


class LeaveSettingsAuditLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="leave_settings_audit_logs",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    object_type = models.CharField(max_length=64)
    object_id = models.UUIDField()
    action = models.CharField(max_length=16, choices=SettingsAuditAction.choices)
    previous_values = models.JSONField(null=True, blank=True)
    new_values = models.JSONField(null=True, blank=True)
    reason = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        verbose_name = "Leave Settings Audit Log"
        verbose_name_plural = "Leave Settings Audit Logs"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["object_type", "object_id", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.action} {self.object_type} {self.object_id}"


# ---------------------------------------------------------------------------
# Approval workflow templates
# ---------------------------------------------------------------------------

DEFAULT_WORKFLOW_NAME = "Standard approval chain"


class ApproverSource(models.TextChoices):
    TEAM_LEAD = "TEAM_LEAD", "Team lead"
    SUPERVISOR = "SUPERVISOR", "Supervisor"
    LINE_MANAGER = "LINE_MANAGER", "Line manager"
    HR = "HR", "HR"
    EXECUTIVE_DIRECTOR = "EXECUTIVE_DIRECTOR", "Executive director"
    NAMED_USER = "NAMED_USER", "Named user"
    ROLE = "ROLE", "Role"


class WorkflowMode(models.TextChoices):
    SEQUENTIAL = "SEQUENTIAL", "Sequential"


class LeaveWorkflowTemplate(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200, unique=True)
    is_active = models.BooleanField(default=True)
    is_org_default = models.BooleanField(default=False)
    leave_type = models.ForeignKey(
        LeaveType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="workflow_templates",
        help_text="When set, this template is preferred for that leave type over the org default.",
    )
    mode = models.CharField(
        max_length=16,
        choices=WorkflowMode.choices,
        default=WorkflowMode.SEQUENTIAL,
    )
    reject_comment_required = models.BooleanField(default=True)
    approve_comment_required = models.BooleanField(default=False)
    sla_hours = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Fallback SLA hours for stages that do not set sla_hours.",
    )
    auto_approve_after_sla = models.BooleanField(
        default=False,
        help_text="If true, Beat may auto-approve after SLA. Default off.",
    )

    class Meta:
        verbose_name = "Leave Workflow Template"
        verbose_name_plural = "Leave Workflow Templates"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["leave_type"],
                condition=models.Q(is_active=True, leave_type__isnull=False),
                name="unique_active_workflow_per_leave_type",
            ),
        ]

    def __str__(self):
        return self.name


class LeaveWorkflowStage(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    template = models.ForeignKey(
        LeaveWorkflowTemplate,
        on_delete=models.CASCADE,
        related_name="stages",
    )
    order = models.PositiveIntegerField()
    approver_source = models.CharField(max_length=32, choices=ApproverSource.choices)
    status_code = models.CharField(
        max_length=20,
        choices=LeaveRequestStatus.choices,
        help_text="API status used while this stage is pending (keeps PENDING_* compatibility).",
    )
    named_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    role_name = models.CharField(
        max_length=32,
        blank=True,
        help_text="Required when approver_source is ROLE.",
    )
    sla_hours = models.PositiveIntegerField(null=True, blank=True)
    skip_if_unresolved = models.BooleanField(
        default=False,
        help_text="Skip leading stages that cannot resolve an approver (prefix skip only).",
    )
    is_optional = models.BooleanField(
        default=False,
        help_text="If true, drop this stage when no approver can be resolved (including mid-chain).",
    )
    skip_if_requester_roles = models.JSONField(
        default=list,
        blank=True,
        help_text="Role names; if the requester has any, this stage is omitted from the snapshot.",
    )
    use_management_line_manager_for_line_manager_requester = models.BooleanField(
        default=False,
        help_text="When requester is a LINE_MANAGER, resolve approver from the Management department.",
    )

    class Meta:
        verbose_name = "Leave Workflow Stage"
        verbose_name_plural = "Leave Workflow Stages"
        ordering = ["template", "order"]
        unique_together = ("template", "order")

    def __str__(self):
        return f"{self.template.name} #{self.order} {self.approver_source}"


class ApproverDelegate(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="approver_delegations_given",
        help_text="Primary approver being covered.",
    )
    delegate = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="approver_delegations_received",
    )
    start_date = models.DateField()
    end_date = models.DateField()
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Approver Delegate"
        verbose_name_plural = "Approver Delegates"
        ordering = ["-start_date"]
        indexes = [
            models.Index(fields=["user", "delegate", "is_active"]),
        ]

    def __str__(self):
        return f"{self.user_id} → {self.delegate_id} ({self.start_date}–{self.end_date})"


class BlackoutEnforcement(models.TextChoices):
    BLOCK = "BLOCK", "Hard block"
    WARN = "WARN", "Warning only"


class LeaveBlackoutPeriod(TimeStampedModel):
    """Named date range when leave of selected types cannot (or should not) be taken."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    start_date = models.DateField()
    end_date = models.DateField()
    enforcement = models.CharField(
        max_length=8,
        choices=BlackoutEnforcement.choices,
        default=BlackoutEnforcement.BLOCK,
    )
    leave_types = models.ManyToManyField(
        LeaveType,
        blank=True,
        related_name="blackout_periods",
        help_text="Empty means all leave types.",
    )
    department = models.ForeignKey(
        "accounts.Department",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="leave_blackout_periods",
        help_text="Null = organization-wide.",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Leave Blackout Period"
        verbose_name_plural = "Leave Blackout Periods"
        ordering = ["-start_date"]

    def __str__(self):
        return f"{self.name} ({self.start_date}–{self.end_date})"
