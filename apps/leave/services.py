"""
Business-logic layer for leave management.

Keeping computation and validation here (instead of views/serializers) makes
the logic easy to unit-test and reuse across API endpoints, Celery tasks, etc.
"""

import calendar
import datetime
import uuid
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.accounts.models import (
    ContractType,
    Department,
    DepartmentMembership,
    RoleName,
    Team,
    Unit,
    get_or_create_management_department,
)

from .models import (
    AccrualMethod,
    AssignmentScopeType,
    BalanceTransactionSource,
    BalanceTransactionType,
    CalendarAssignment,
    CalendarHoliday,
    CrossYearDeductionRule,
    HalfDayPeriod,
    HolidayCalendar,
    LeaveBalance,
    LeaveBalanceTransaction,
    LeavePolicy,
    LeavePolicyAssignment,
    LeavePolicyStatus,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveSettings,
    LeaveSettingsAuditLog,
    LeaveType,
    LeaveYearType,
    OverlapEnforcement,
    OverlapScope,
    PublicHoliday,
    SettingsAuditAction,
    WorkingCalendar,
)
from .utils import DEFAULT_WORKING_WEEKDAYS, calculate_working_days, format_leave_days
from . import messages as leave_messages

User = get_user_model()

RELIEVER_EXEMPT_LEAVE_CODES = (
    LeaveType.Code.SICK,
    LeaveType.Code.MATERNITY,
    LeaveType.Code.PATERNITY,
)
STAFFING_CONTROL_LEAVE_CODES = (
    LeaveType.Code.ANNUAL,
    LeaveType.Code.CASUAL,
)
POLICY_SNAPSHOT_FIELDS = (
    "name",
    "leave_type_id",
    "status",
    "version",
    "effective_from",
    "effective_to",
    "annual_entitlement",
    "accrual_method",
    "accrual_rate",
    "prorate_new_joiners",
    "carry_forward",
    "carry_forward_max_days",
    "carry_forward_expiry_months",
    "forfeit_unused",
    "half_day_allowed",
    "weekend_excluded",
    "public_holiday_excluded",
    "forfeited_on_resignation",
    "allow_backdated",
    "maximum_backdate_days",
    "reliever_required",
    "reliever_scope",
    "overlap_control_enabled",
    "overlap_scope",
    "maximum_people_absent",
    "overlap_enforcement",
)
LEAVE_TYPE_SNAPSHOT_FIELDS = (
    "name",
    "code",
    "description",
    "default_days",
    "is_active",
    "display_order",
    "calendar_color",
)

PENDING_HOLD_STATUSES = (
    LeaveRequestStatus.PENDING_TEAM_LEAD,
    LeaveRequestStatus.PENDING_SUPERVISOR,
    LeaveRequestStatus.PENDING_MANAGER,
    LeaveRequestStatus.PENDING_HR,
    LeaveRequestStatus.PENDING_ED,
)

IN_FLIGHT_OR_APPROVED_STATUSES = PENDING_HOLD_STATUSES + (LeaveRequestStatus.APPROVED,)


def _to_decimal(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _leave_type_code(leave_type) -> str:
    return (getattr(leave_type, "code", None) or "").upper()


def get_eligible_leave_types(user):
    """Return active leave types the user can apply for, based on gender."""
    qs = LeaveType.objects.filter(is_active=True)
    gender = getattr(user, "gender", None)
    if gender == "FEMALE":
        qs = qs.exclude(code=LeaveType.Code.PATERNITY)
    elif gender == "MALE":
        qs = qs.exclude(code=LeaveType.Code.MATERNITY)
    return qs


def resolve_org_scope(employee) -> tuple[Optional[str], dict]:
    """
    Return (scope_level, user_filters) for the lowest applicable org level.

    scope_level is 'team' | 'unit' | 'department', or None when the employee
    has no department. user_filters are kwargs for User.objects.filter().
    """
    if not getattr(employee, "department_id", None):
        return None, {}

    dept_id = employee.department_id
    department_has_units = Unit.objects.filter(department_id=dept_id).exists()
    department_has_teams = Team.objects.filter(unit__department_id=dept_id).exists()

    if department_has_teams and getattr(employee, "team_id", None):
        return "team", {"team_id": employee.team_id}
    if department_has_units and getattr(employee, "unit_id", None):
        return "unit", {"unit_id": employee.unit_id}
    return "department", {"department_id": dept_id}


def _leave_overlap_scope_filters(employee, overlap_scope: str = OverlapScope.AUTO) -> dict:
    """Prefix org-scope filters with employee__ for LeaveRequest queries."""
    if overlap_scope == OverlapScope.ORGANIZATION:
        return {}
    if overlap_scope == OverlapScope.TEAM:
        if not getattr(employee, "team_id", None):
            return {}
        return {"employee__team_id": employee.team_id}
    if overlap_scope == OverlapScope.UNIT:
        if not getattr(employee, "unit_id", None):
            return {}
        return {"employee__unit_id": employee.unit_id}
    if overlap_scope == OverlapScope.DEPARTMENT:
        if not getattr(employee, "department_id", None):
            return {}
        return {"employee__department_id": employee.department_id}

    scope_level, filters = resolve_org_scope(employee)
    if scope_level is None:
        return {}
    return {f"employee__{key}": value for key, value in filters.items()}


def _department_members_queryset(department):
    """Return active users in a department, handling Management membership."""
    mgmt = get_or_create_management_department()
    if department.pk == mgmt.pk:
        member_ids = DepartmentMembership.objects.filter(
            department=department
        ).values_list("user_id", flat=True)
        return User.objects.filter(pk__in=member_ids, is_active=True)
    return User.objects.filter(department=department, is_active=True)


def get_department_leave_reminder_recipients(employee) -> list:
    """
    Recipients for the pre-leave department broadcast:
    department colleagues, department line manager, all HR, all EDs.
    Deduplicated; excludes the employee going on leave.
    """
    users_by_id: dict = {}

    if getattr(employee, "department_id", None):
        for user in _department_members_queryset(employee.department).exclude(pk=employee.pk):
            users_by_id[user.pk] = user

    line_manager = employee.get_department_line_manager()
    if line_manager and line_manager.pk != employee.pk:
        users_by_id[line_manager.pk] = line_manager

    role_recipients = User.objects.filter(
        is_active=True,
        user_roles__role__name__in=(RoleName.HR, RoleName.EXECUTIVE_DIRECTOR),
    ).distinct()
    for user in role_recipients:
        if user.pk != employee.pk:
            users_by_id[user.pk] = user

    return list(users_by_id.values())


def get_leave_approval_stakeholders(employee) -> list:
    """
    Everyone who would have been in the approval chain for *employee*:
    team lead, unit supervisor, department line manager, all HR, all EDs,
    and the employee themselves. Deduplicated.
    """
    users_by_id: dict = {employee.pk: employee}

    team = getattr(employee, "team", None)
    if team and team.team_lead_id:
        users_by_id[team.team_lead_id] = team.team_lead

    unit = getattr(employee, "unit", None)
    if unit and unit.supervisor_id:
        users_by_id[unit.supervisor_id] = unit.supervisor

    line_manager = employee.get_department_line_manager()
    if line_manager:
        users_by_id[line_manager.pk] = line_manager

    role_recipients = User.objects.filter(
        is_active=True,
        user_roles__role__name__in=(RoleName.HR, RoleName.EXECUTIVE_DIRECTOR),
    ).distinct()
    for user in role_recipients:
        users_by_id[user.pk] = user

    return list(users_by_id.values())


def ensure_leave_balance_record(employee, leave_type, year: int) -> LeaveBalance:
    """Create a balance row for the year when missing (e.g. backdated reconciliation)."""
    on_date = datetime.date(year, 1, 1)
    balance, _ = LeaveBalance.objects.get_or_create(
        employee=employee,
        leave_type=leave_type,
        year=year,
        defaults={
            "allocated_days": get_annual_entitlement(
                leave_type, on_date=on_date, employee=employee
            ),
            "used_days": Decimal("0"),
            "pending_days": Decimal("0"),
        },
    )
    return balance


ASSIGNMENT_SCOPE_RANK = {
    AssignmentScopeType.EMPLOYEE: 100,
    AssignmentScopeType.TEAM: 80,
    AssignmentScopeType.UNIT: 70,
    AssignmentScopeType.DEPARTMENT: 50,
    AssignmentScopeType.EMPLOYMENT_TYPE: 40,
    AssignmentScopeType.ORGANIZATION: 10,
}


@dataclass(frozen=True)
class PolicyResolution:
    policy: Optional[LeavePolicy]
    assignment: Optional[LeavePolicyAssignment]
    assignment_scope: Optional[str]
    effective_date: datetime.date
    source: str  # "assignment" | "fallback"


def _policy_effective_on(qs, on_date: Optional[datetime.date]):
    if on_date is None:
        return qs
    return qs.filter(
        Q(effective_from__isnull=True) | Q(effective_from__lte=on_date)
    ).filter(Q(effective_to__isnull=True) | Q(effective_to__gte=on_date))


def _assignment_effective_on(qs, on_date: datetime.date):
    return qs.filter(effective_from__lte=on_date).filter(
        Q(effective_to__isnull=True) | Q(effective_to__gte=on_date)
    )


def assignment_matches_employee(assignment: LeavePolicyAssignment, employee) -> bool:
    if employee is None:
        return False
    scope = assignment.scope_type
    scope_id = (assignment.scope_id or "").strip()
    if scope == AssignmentScopeType.EMPLOYEE:
        return assignment.employee_id == employee.pk
    if scope == AssignmentScopeType.ORGANIZATION:
        return True
    if scope == AssignmentScopeType.DEPARTMENT:
        return bool(employee.department_id) and str(employee.department_id) == scope_id
    if scope == AssignmentScopeType.UNIT:
        return bool(employee.unit_id) and str(employee.unit_id) == scope_id
    if scope == AssignmentScopeType.TEAM:
        return bool(employee.team_id) and str(employee.team_id) == scope_id
    if scope == AssignmentScopeType.EMPLOYMENT_TYPE:
        return bool(employee.contract_type) and employee.contract_type == scope_id
    return False


def _fallback_active_policy(leave_type, on_date: Optional[datetime.date]) -> Optional[LeavePolicy]:
    qs = _policy_effective_on(
        LeavePolicy.objects.filter(
            leave_type=leave_type,
            status=LeavePolicyStatus.ACTIVE,
        ),
        on_date,
    )
    assigned_ids = LeavePolicyAssignment.objects.filter(
        is_active=True,
        policy__leave_type=leave_type,
        policy__status=LeavePolicyStatus.ACTIVE,
    ).values_list("policy_id", flat=True)
    unassigned = qs.exclude(pk__in=assigned_ids)
    pool = unassigned if unassigned.exists() else qs
    return pool.order_by("-version", "-effective_from", "-created_at").first()


def get_active_policy(
    leave_type,
    on_date: Optional[datetime.date] = None,
    employee=None,
) -> Optional[LeavePolicy]:
    """
    Return the ACTIVE LeavePolicy for *leave_type*.

    When *employee* is provided, assignment precedence applies.
    Without an employee (or with no matching assignment), fall back to the
    unassigned ACTIVE policy for the type (Sprint 1/2 behaviour).
    """
    if leave_type is None:
        return None
    if employee is not None:
        return resolve_leave_policy(employee, leave_type, on_date).policy
    return _fallback_active_policy(leave_type, on_date)


def resolve_leave_policy(
    employee,
    leave_type,
    on_date: Optional[datetime.date] = None,
) -> PolicyResolution:
    """Pick the policy that applies to *employee* on *on_date*."""
    effective_date = on_date or timezone.localdate()
    if leave_type is None:
        return PolicyResolution(None, None, None, effective_date, "fallback")

    candidates = list(
        _assignment_effective_on(
            LeavePolicyAssignment.objects.filter(
                is_active=True,
                policy__leave_type=leave_type,
                policy__status=LeavePolicyStatus.ACTIVE,
            ).select_related("policy", "employee"),
            effective_date,
        )
    )
    matching = [a for a in candidates if assignment_matches_employee(a, employee)]
    if matching:
        matching.sort(
            key=lambda a: (
                ASSIGNMENT_SCOPE_RANK.get(a.scope_type, 0),
                a.priority,
                a.effective_from or datetime.date.min,
            ),
            reverse=True,
        )
        winner = matching[0]
        policy = winner.policy
        if not _policy_date_ok(policy, effective_date):
            policy = None
        if policy is not None:
            return PolicyResolution(
                policy,
                winner,
                winner.scope_type,
                effective_date,
                "assignment",
            )

    fallback = _fallback_active_policy(leave_type, effective_date)
    return PolicyResolution(fallback, None, None, effective_date, "fallback")


def _policy_date_ok(policy: LeavePolicy, on_date: datetime.date) -> bool:
    if policy.effective_from and policy.effective_from > on_date:
        return False
    if policy.effective_to and policy.effective_to < on_date:
        return False
    return True


def get_annual_entitlement(
    leave_type,
    on_date: Optional[datetime.date] = None,
    employee=None,
) -> int:
    """Policy annual_entitlement when present; otherwise LeaveType.default_days."""
    policy = get_active_policy(leave_type, on_date=on_date, employee=employee)
    if policy is not None:
        return policy.annual_entitlement
    return leave_type.default_days


def working_day_flags_for_leave_type(
    leave_type,
    on_date: Optional[datetime.date] = None,
    employee=None,
) -> tuple[bool, bool]:
    """Return (weekend_excluded, public_holiday_excluded). Defaults True, True."""
    policy = get_active_policy(leave_type, on_date=on_date, employee=employee)
    if policy is None:
        return True, True
    return policy.weekend_excluded, policy.public_holiday_excluded


@dataclass(frozen=True)
class ResolvedCalendars:
    working_calendar: Optional[WorkingCalendar]
    holiday_calendar: Optional[HolidayCalendar]
    weekdays: tuple[int, ...]
    include_global_public_holidays: bool


def get_leave_settings() -> LeaveSettings:
    """Return the singleton LeaveSettings row, creating defaults if needed."""
    obj = (
        LeaveSettings.objects.select_related(
            "default_working_calendar",
            "default_holiday_calendar",
        )
        .filter(singleton_key="default")
        .first()
    )
    if obj is not None:
        return obj
    working, holiday = ensure_default_calendars()
    return LeaveSettings.objects.create(
        singleton_key="default",
        default_working_calendar=working,
        default_holiday_calendar=holiday,
    )


def ensure_default_calendars() -> tuple[WorkingCalendar, HolidayCalendar]:
    working = WorkingCalendar.objects.filter(is_org_default=True).first()
    if working is None:
        working = WorkingCalendar.objects.create(
            name="Standard weekdays (Mon–Fri)",
            is_org_default=True,
            weekdays=list(DEFAULT_WORKING_WEEKDAYS),
            timezone="Africa/Lagos",
        )
    holiday = HolidayCalendar.objects.filter(is_org_default=True).first()
    if holiday is None:
        holiday = HolidayCalendar.objects.create(
            name="Organization public holidays",
            is_org_default=True,
            timezone="Africa/Lagos",
        )
    return working, holiday


def resolve_employee_calendars(employee=None, on_date: Optional[datetime.date] = None) -> ResolvedCalendars:
    settings_row = get_leave_settings()
    assignment = None
    if employee is not None:
        qs = CalendarAssignment.objects.filter(is_active=True).select_related(
            "working_calendar", "holiday_calendar"
        )
        if on_date is not None:
            qs = qs.filter(
                Q(effective_from__isnull=True) | Q(effective_from__lte=on_date)
            ).filter(Q(effective_to__isnull=True) | Q(effective_to__gte=on_date))
        assignment = qs.filter(employee_id=employee.pk).first()
        if assignment is None and getattr(employee, "department_id", None):
            assignment = qs.filter(
                employee__isnull=True, department_id=employee.department_id
            ).first()

    working = None
    holiday = None
    if assignment is not None:
        working = assignment.working_calendar if assignment.working_calendar_id else None
        holiday = assignment.holiday_calendar if assignment.holiday_calendar_id else None
    if working is None:
        working = settings_row.default_working_calendar
    if holiday is None:
        holiday = settings_row.default_holiday_calendar

    weekdays = tuple(DEFAULT_WORKING_WEEKDAYS)
    if working is not None and working.weekdays:
        weekdays = tuple(int(d) for d in working.weekdays)

    include_global = holiday is None or bool(getattr(holiday, "is_org_default", False))
    return ResolvedCalendars(
        working_calendar=working,
        holiday_calendar=holiday,
        weekdays=weekdays,
        include_global_public_holidays=include_global,
    )


def load_holiday_sets(
    start_date: datetime.date,
    end_date: datetime.date,
    resolved: ResolvedCalendars,
) -> tuple[set, list]:
    holidays_in_range: set[datetime.date] = set()
    recurring: list[tuple[int, int]] = []

    if resolved.holiday_calendar is not None:
        cal_id = resolved.holiday_calendar.pk
        holidays_in_range.update(
            CalendarHoliday.objects.filter(
                calendar_id=cal_id,
                is_recurring=False,
                date__range=(start_date, end_date),
            ).values_list("date", flat=True)
        )
        holidays_in_range.update(
            d
            for d in CalendarHoliday.objects.filter(
                calendar_id=cal_id,
                is_recurring=False,
                observed_date__range=(start_date, end_date),
            ).values_list("observed_date", flat=True)
            if d
        )
        recurring.extend(
            CalendarHoliday.objects.filter(
                calendar_id=cal_id, is_recurring=True
            ).values_list("date__month", "date__day")
        )

    if resolved.include_global_public_holidays:
        holidays_in_range.update(
            PublicHoliday.objects.filter(
                is_recurring=False,
                date__range=(start_date, end_date),
            ).values_list("date", flat=True)
        )
        recurring.extend(
            PublicHoliday.objects.filter(is_recurring=True).values_list(
                "date__month", "date__day"
            )
        )
    return holidays_in_range, recurring


def working_day_calculation_snapshot(
    *,
    start_date,
    end_date,
    leave_type=None,
    employee=None,
    is_half_day=False,
    total_working_days=None,
) -> dict:
    weekend_excluded, public_holiday_excluded = working_day_flags_for_leave_type(
        leave_type, on_date=start_date, employee=employee
    )
    resolved = resolve_employee_calendars(employee, on_date=start_date)
    return {
        "weekend_excluded": weekend_excluded,
        "public_holiday_excluded": public_holiday_excluded,
        "working_weekdays": list(resolved.weekdays),
        "working_calendar_id": str(resolved.working_calendar.pk)
        if resolved.working_calendar
        else None,
        "holiday_calendar_id": str(resolved.holiday_calendar.pk)
        if resolved.holiday_calendar
        else None,
        "is_half_day": is_half_day,
        "total_working_days": str(total_working_days) if total_working_days is not None else None,
        "start_date": start_date.isoformat() if start_date else None,
        "end_date": end_date.isoformat() if end_date else None,
    }


def calculate_working_days_for_leave_type(
    start_date: datetime.date,
    end_date: datetime.date,
    leave_type=None,
    on_date: Optional[datetime.date] = None,
    is_half_day: bool = False,
    employee=None,
):
    weekend_excluded, public_holiday_excluded = working_day_flags_for_leave_type(
        leave_type,
        on_date=on_date or start_date,
        employee=employee,
    )
    resolved = resolve_employee_calendars(employee, on_date=on_date or start_date)
    holidays_in_range, recurring = load_holiday_sets(start_date, end_date, resolved)
    return calculate_working_days(
        start_date,
        end_date,
        weekend_excluded=weekend_excluded,
        public_holiday_excluded=public_holiday_excluded,
        is_half_day=is_half_day,
        working_weekdays=resolved.weekdays,
        holidays_in_range=holidays_in_range,
        recurring_holidays=recurring,
    )


def snapshot_leave_settings(settings_row: LeaveSettings) -> dict:
    return {
        "leave_year_type": settings_row.leave_year_type,
        "leave_year_start_month": settings_row.leave_year_start_month,
        "leave_year_start_day": settings_row.leave_year_start_day,
        "cross_year_deduction_rule": settings_row.cross_year_deduction_rule,
        "default_timezone": settings_row.default_timezone,
        "default_working_calendar_id": str(settings_row.default_working_calendar_id)
        if settings_row.default_working_calendar_id
        else None,
        "default_holiday_calendar_id": str(settings_row.default_holiday_calendar_id)
        if settings_row.default_holiday_calendar_id
        else None,
        "notify_applicant_on_submit": settings_row.notify_applicant_on_submit,
        "notify_applicant_on_decision": settings_row.notify_applicant_on_decision,
        "notify_approver": settings_row.notify_approver,
        "notify_reliever": settings_row.notify_reliever,
        "notify_department_reminder": settings_row.notify_department_reminder,
        "reminder_lead_hours": settings_row.reminder_lead_hours,
        "allow_hr_override": settings_row.allow_hr_override,
        "prevent_self_approval": settings_row.prevent_self_approval,
        "approval_sla_hours": settings_row.approval_sla_hours,
        "encashment_allowed": settings_row.encashment_allowed,
        "encashment_max_days": (
            str(settings_row.encashment_max_days)
            if settings_row.encashment_max_days is not None
            else None
        ),
    }


def leave_year_boundary_month_day(settings_row: Optional[LeaveSettings] = None) -> tuple[int, int]:
    settings_row = settings_row or get_leave_settings()
    if settings_row.leave_year_type == LeaveYearType.FISCAL:
        month = min(max(int(settings_row.leave_year_start_month or 1), 1), 12)
        day = min(max(int(settings_row.leave_year_start_day or 1), 1), 28)
        return month, day
    return 1, 1


def leave_year_start_date(year: int, settings_row: Optional[LeaveSettings] = None) -> datetime.date:
    month, day = leave_year_boundary_month_day(settings_row)
    return datetime.date(year, month, day)


def leave_year_for_date(on_date: datetime.date, settings_row: Optional[LeaveSettings] = None) -> int:
    """Calendar year of the leave-year *start* that contains *on_date*.

    LeaveBalance.year and accrual idempotency keys continue to use this integer.
    Anniversary type does not change org-wide year numbering (still 1 Jan).
    """
    start = leave_year_start_date(on_date.year, settings_row)
    if on_date >= start:
        return on_date.year
    return on_date.year - 1


def reminder_lead_days(settings_row: Optional[LeaveSettings] = None) -> int:
    settings_row = settings_row or get_leave_settings()
    hours = max(int(settings_row.reminder_lead_hours or 24), 1)
    return max((hours + 23) // 24, 1)


def sync_public_holiday_to_default_calendar(*, name: str, date, is_recurring: bool = False):
    """Keep the org-default HolidayCalendar in sync with PublicHoliday writes."""
    _, holiday_cal = ensure_default_calendars()
    CalendarHoliday.objects.update_or_create(
        calendar=holiday_cal,
        date=date,
        defaults={"name": name, "is_recurring": is_recurring, "is_full_day": True},
    )


def snapshot_leave_type(leave_type) -> dict:
    return {
        field: getattr(leave_type, field)
        for field in LEAVE_TYPE_SNAPSHOT_FIELDS
    }


def snapshot_leave_policy(policy) -> dict:
    data = {}
    for field in POLICY_SNAPSHOT_FIELDS:
        value = getattr(policy, field)
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        if field.endswith("_id"):
            data[field] = str(value) if value else None
        else:
            data[field] = value
    data["id"] = str(policy.pk) if policy.pk else None
    return data


def snapshot_leave_assignment(assignment) -> dict:
    return {
        "id": str(assignment.pk) if assignment.pk else None,
        "policy_id": str(assignment.policy_id) if assignment.policy_id else None,
        "scope_type": assignment.scope_type,
        "scope_id": assignment.scope_id or "",
        "employee_id": str(assignment.employee_id) if assignment.employee_id else None,
        "priority": assignment.priority,
        "effective_from": assignment.effective_from.isoformat()
        if assignment.effective_from
        else None,
        "effective_to": assignment.effective_to.isoformat()
        if assignment.effective_to
        else None,
        "is_active": assignment.is_active,
    }


def apply_policy_snapshot(leave_request) -> None:
    """Stamp policy_id + version onto a request without rewriting stored day counts."""
    policy = get_active_policy(
        leave_request.leave_type,
        on_date=leave_request.start_date,
        employee=leave_request.employee,
    )
    leave_request.policy = policy
    leave_request.policy_version = policy.version if policy is not None else None


def _parse_scope_uuid(scope_id: str):
    try:
        return uuid.UUID(str(scope_id))
    except (ValueError, TypeError, AttributeError):
        return None


def validate_assignment_scope(scope_type, scope_id, employee) -> None:
    scope_id = (scope_id or "").strip()
    if scope_type == AssignmentScopeType.ORGANIZATION:
        if scope_id:
            raise ValidationError(
                {"scope_id": leave_messages.assignment_scope_org_no_scope_id()}
            )
        if employee is not None:
            raise ValidationError(
                {"employee": leave_messages.assignment_scope_org_no_employee()}
            )
        return
    if scope_type == AssignmentScopeType.EMPLOYEE:
        if employee is None:
            raise ValidationError(
                {"employee": leave_messages.assignment_scope_employee_required()}
            )
        return
    if employee is not None:
        raise ValidationError(
            {"employee": leave_messages.assignment_scope_employee_only_for_employee_type()}
        )
    if scope_type == AssignmentScopeType.EMPLOYMENT_TYPE:
        valid = {choice[0] for choice in ContractType.choices}
        if scope_id not in valid:
            raise ValidationError(
                {"scope_id": leave_messages.assignment_scope_invalid_contract_type(valid)}
            )
        return
    parsed = _parse_scope_uuid(scope_id)
    if parsed is None:
        raise ValidationError({"scope_id": leave_messages.assignment_scope_uuid_required()})
    if scope_type == AssignmentScopeType.DEPARTMENT:
        if not Department.objects.filter(pk=parsed).exists():
            raise ValidationError({"scope_id": leave_messages.assignment_scope_department_not_found()})
    elif scope_type == AssignmentScopeType.UNIT:
        if not Unit.objects.filter(pk=parsed).exists():
            raise ValidationError({"scope_id": leave_messages.assignment_scope_unit_not_found()})
    elif scope_type == AssignmentScopeType.TEAM:
        if not Team.objects.filter(pk=parsed).exists():
            raise ValidationError({"scope_id": leave_messages.assignment_scope_team_not_found()})
    else:
        raise ValidationError({"scope_type": leave_messages.assignment_scope_unsupported()})


def _date_ranges_overlap(from_a, to_a, from_b, to_b) -> bool:
    start = max(from_a, from_b)
    end_a = to_a or datetime.date.max
    end_b = to_b or datetime.date.max
    end = min(end_a, end_b)
    return start <= end


def find_conflicting_assignments(assignment, *, exclude_pk=None):
    """Same leave type + same scope identity + overlapping effective window."""
    leave_type_id = assignment.policy.leave_type_id
    employee_id = assignment.employee_id
    scope_id = (assignment.scope_id or "").strip()
    qs = LeavePolicyAssignment.objects.filter(
        is_active=True,
        policy__leave_type_id=leave_type_id,
        scope_type=assignment.scope_type,
        scope_id=scope_id,
    )
    if employee_id:
        qs = qs.filter(employee_id=employee_id)
    else:
        qs = qs.filter(employee__isnull=True)
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    conflicts = []
    for other in qs.select_related("policy"):
        if not other.is_active:
            continue
        if _date_ranges_overlap(
            assignment.effective_from,
            assignment.effective_to,
            other.effective_from,
            other.effective_to,
        ):
            conflicts.append(other)
    return conflicts


def employees_matching_assignment(assignment):
    qs = User.objects.filter(is_active=True)
    scope = assignment.scope_type
    scope_id = (assignment.scope_id or "").strip()
    if scope == AssignmentScopeType.EMPLOYEE:
        return qs.filter(pk=assignment.employee_id)
    if scope == AssignmentScopeType.ORGANIZATION:
        return qs
    if scope == AssignmentScopeType.DEPARTMENT:
        return qs.filter(department_id=scope_id)
    if scope == AssignmentScopeType.UNIT:
        return qs.filter(unit_id=scope_id)
    if scope == AssignmentScopeType.TEAM:
        return qs.filter(team_id=scope_id)
    if scope == AssignmentScopeType.EMPLOYMENT_TYPE:
        return qs.filter(contract_type=scope_id)
    return qs.none()


def preview_policy_impact(policy, on_date: Optional[datetime.date] = None, limit: int = 200):
    """Employees who currently resolve to *policy* on *on_date*."""
    on_date = on_date or timezone.localdate()
    leave_type = policy.leave_type
    employees = list(
        User.objects.filter(is_active=True).select_related("department", "unit", "team")
    )
    rows = []
    for employee in employees:
        resolution = resolve_leave_policy(employee, leave_type, on_date)
        if resolution.policy is None or resolution.policy.pk != policy.pk:
            continue
        rows.append(
            {
                "id": str(employee.pk),
                "email": employee.email,
                "first_name": employee.first_name,
                "last_name": employee.last_name,
                "assignment_scope": resolution.assignment_scope,
                "source": resolution.source,
            }
        )
        if len(rows) >= limit:
            break
    total = 0
    if len(rows) < limit:
        total = len(rows)
    else:
        total = 0
        for employee in employees:
            resolution = resolve_leave_policy(employee, leave_type, on_date)
            if resolution.policy is not None and resolution.policy.pk == policy.pk:
                total += 1
    return {
        "effective_date": on_date.isoformat(),
        "employee_count": total,
        "employees": rows,
        "truncated": total > len(rows),
    }
    return data


def record_settings_audit(
    *,
    actor,
    object_type: str,
    object_id,
    action: str,
    previous_values=None,
    new_values=None,
    reason: str = "",
    request=None,
) -> LeaveSettingsAuditLog:
    ip_address = None
    if request is not None:
        ip_address = request.META.get("REMOTE_ADDR")
    return LeaveSettingsAuditLog.objects.create(
        actor=actor,
        object_type=object_type,
        object_id=object_id,
        action=action,
        previous_values=previous_values,
        new_values=new_values,
        reason=reason or "",
        ip_address=ip_address,
    )


def next_policy_version(leave_type) -> int:
    from django.db.models import Max

    current = LeavePolicy.objects.filter(leave_type=leave_type).aggregate(
        Max("version")
    )["version__max"]
    return (current or 0) + 1


def publish_leave_policy(
    policy,
    *,
    actor,
    reason: str = "",
    request=None,
    keep_existing_active: bool = False,
) -> LeavePolicy:
    """Activate a draft policy.

    By default archives other unassigned ACTIVE policies for the type (Sprint 1).
    Policies that still have active assignments are never auto-archived.
    Pass keep_existing_active=True to publish a concurrent assigned policy.
    """
    from django.db import transaction

    if policy.status != LeavePolicyStatus.DRAFT:
        raise ValidationError({"status": leave_messages.policy_publish_draft_only()})

    previous = snapshot_leave_policy(policy)
    today = timezone.localdate()
    with transaction.atomic():
        active_qs = LeavePolicy.objects.select_for_update().filter(
            leave_type=policy.leave_type,
            status=LeavePolicyStatus.ACTIVE,
        ).exclude(pk=policy.pk)
        if keep_existing_active:
            active_qs = active_qs.none()
        else:
            assigned_ids = LeavePolicyAssignment.objects.filter(
                is_active=True,
                policy_id__in=active_qs.values("pk"),
            ).values_list("policy_id", flat=True)
            active_qs = active_qs.exclude(pk__in=assigned_ids)
        for existing in active_qs:
            existing_prev = snapshot_leave_policy(existing)
            existing.status = LeavePolicyStatus.ARCHIVED
            if existing.effective_to is None:
                existing.effective_to = today
            existing.save(update_fields=["status", "effective_to", "updated_at"])
            record_settings_audit(
                actor=actor,
                object_type="LeavePolicy",
                object_id=existing.pk,
                action=SettingsAuditAction.ARCHIVE,
                previous_values=existing_prev,
                new_values=snapshot_leave_policy(existing),
                reason=reason or "Superseded by a newly published policy.",
                request=request,
            )

        policy.status = LeavePolicyStatus.ACTIVE
        policy.version = next_policy_version(policy.leave_type)
        if policy.effective_from is None:
            policy.effective_from = today
        policy.save()

    record_settings_audit(
        actor=actor,
        object_type="LeavePolicy",
        object_id=policy.pk,
        action=SettingsAuditAction.PUBLISH,
        previous_values=previous,
        new_values=snapshot_leave_policy(policy),
        reason=reason,
        request=request,
    )
    return policy


def archive_leave_policy(policy, *, actor, reason: str = "", request=None) -> LeavePolicy:
    if policy.status == LeavePolicyStatus.ARCHIVED:
        raise ValidationError({"status": leave_messages.policy_already_archived()})
    previous = snapshot_leave_policy(policy)
    policy.status = LeavePolicyStatus.ARCHIVED
    if policy.effective_to is None:
        policy.effective_to = timezone.localdate()
    policy.save(update_fields=["status", "effective_to", "updated_at"])
    record_settings_audit(
        actor=actor,
        object_type="LeavePolicy",
        object_id=policy.pk,
        action=SettingsAuditAction.ARCHIVE,
        previous_values=previous,
        new_values=snapshot_leave_policy(policy),
        reason=reason,
        request=request,
    )
    return policy


def clone_leave_policy(policy, *, actor, reason: str = "", request=None) -> LeavePolicy:
    clone = LeavePolicy(
        name=f"{policy.name} (draft)" if policy.name else "",
        leave_type=policy.leave_type,
        status=LeavePolicyStatus.DRAFT,
        version=0,
        effective_from=None,
        effective_to=None,
        annual_entitlement=policy.annual_entitlement,
        accrual_method=policy.accrual_method,
        accrual_rate=policy.accrual_rate,
        prorate_new_joiners=policy.prorate_new_joiners,
        carry_forward=policy.carry_forward,
        carry_forward_max_days=policy.carry_forward_max_days,
        carry_forward_expiry_months=policy.carry_forward_expiry_months,
        forfeit_unused=policy.forfeit_unused,
        half_day_allowed=policy.half_day_allowed,
        weekend_excluded=policy.weekend_excluded,
        public_holiday_excluded=policy.public_holiday_excluded,
        forfeited_on_resignation=policy.forfeited_on_resignation,
        allow_backdated=policy.allow_backdated,
        maximum_backdate_days=policy.maximum_backdate_days,
        reliever_required=policy.reliever_required,
        reliever_scope=policy.reliever_scope,
        overlap_control_enabled=policy.overlap_control_enabled,
        overlap_scope=policy.overlap_scope,
        maximum_people_absent=policy.maximum_people_absent,
        overlap_enforcement=policy.overlap_enforcement,
    )
    clone.save()
    record_settings_audit(
        actor=actor,
        object_type="LeavePolicy",
        object_id=clone.pk,
        action=SettingsAuditAction.CLONE,
        previous_values={"cloned_from": str(policy.pk)},
        new_values=snapshot_leave_policy(clone),
        reason=reason,
        request=request,
    )
    return clone


def validate_backdating_for_reconcile(leave_type, start_date: datetime.date, employee=None) -> None:
    """
    Enforce LeavePolicy backdating rules for HR reconciliation.
    Permissive when no policy exists.
    """
    policy = get_active_policy(leave_type, employee=employee)
    if policy is None:
        return

    today = timezone.localdate()
    if start_date >= today:
        return

    if not policy.allow_backdated:
        raise ValidationError(
            {
                "start_date": (
                    f"Backdated leave is not allowed for {leave_type.name}. "
                    "Update the leave policy or choose a current/future start date."
                )
            }
        )

    if policy.maximum_backdate_days is not None:
        earliest = today - datetime.timedelta(days=policy.maximum_backdate_days)
        if start_date < earliest:
            raise ValidationError(
                {
                    "start_date": (
                        f"Start date is too far in the past. "
                        f"Maximum backdate for {leave_type.name} is "
                        f"{policy.maximum_backdate_days} day(s) "
                        f"(earliest allowed: {earliest.isoformat()})."
                    )
                }
            )


def split_working_days_by_year(
    start_date: datetime.date,
    end_date: datetime.date,
    leave_type=None,
    is_half_day: bool = False,
    employee=None,
) -> dict[int, Decimal]:
    """Return {calendar_year: working_days} for each year spanned by the range.

    Cross-year rule comes from LeaveSettings.cross_year_deduction_rule.
    SPLIT is the existing reconcile behaviour. START_YEAR charges the start year only.
    Year keys remain calendar years so LeaveBalance.year / ledger keys stay stable.
    """
    if start_date > end_date:
        return {}

    settings_row = get_leave_settings()
    if settings_row.cross_year_deduction_rule == CrossYearDeductionRule.START_YEAR:
        days = (
            calculate_working_days_for_leave_type(
                start_date,
                end_date,
                leave_type=leave_type,
                on_date=start_date,
                is_half_day=is_half_day,
                employee=employee,
            )
            if leave_type is not None
            else calculate_working_days(start_date, end_date, is_half_day=is_half_day)
        )
        return {start_date.year: days} if days else {}

    weekend_excluded = True
    public_holiday_excluded = True
    if leave_type is not None:
        weekend_excluded, public_holiday_excluded = working_day_flags_for_leave_type(
            leave_type,
            on_date=start_date,
            employee=employee,
        )

    resolved = resolve_employee_calendars(employee, on_date=start_date)
    holidays_in_range, recurring = load_holiday_sets(start_date, end_date, resolved)

    if start_date.year == end_date.year:
        days = calculate_working_days(
            start_date,
            end_date,
            weekend_excluded=weekend_excluded,
            public_holiday_excluded=public_holiday_excluded,
            is_half_day=is_half_day,
            working_weekdays=resolved.weekdays,
            holidays_in_range=holidays_in_range,
            recurring_holidays=recurring,
        )
        return {start_date.year: days} if days else {}

    result: dict[int, Decimal] = {}
    for year in range(start_date.year, end_date.year + 1):
        segment_start = start_date if year == start_date.year else datetime.date(year, 1, 1)
        segment_end = end_date if year == end_date.year else datetime.date(year, 12, 31)
        days = calculate_working_days(
            segment_start,
            segment_end,
            weekend_excluded=weekend_excluded,
            public_holiday_excluded=public_holiday_excluded,
            is_half_day=is_half_day and year == start_date.year,
            working_weekdays=resolved.weekdays,
            holidays_in_range=holidays_in_range,
            recurring_holidays=recurring,
        )
        if days:
            result[year] = days
    return result


def _year_days_for_request(leave_request) -> dict[tuple, Decimal]:
    """Map (leave_type_id, year) -> working days for a leave request."""
    splits = split_working_days_by_year(
        leave_request.start_date,
        leave_request.end_date,
        leave_type=leave_request.leave_type,
        is_half_day=getattr(leave_request, "is_half_day", False),
        employee=leave_request.employee,
    )
    return {(leave_request.leave_type_id, year): days for year, days in splits.items()}


def has_balance_been_deducted(leave_request) -> bool:
    return LeaveBalanceTransaction.objects.filter(
        leave_request=leave_request,
        transaction_type=BalanceTransactionType.DEDUCT,
    ).exists()


def has_balance_been_refunded(leave_request) -> bool:
    return LeaveBalanceTransaction.objects.filter(
        leave_request=leave_request,
        transaction_type=BalanceTransactionType.REFUND,
    ).exists()


def has_balance_been_reserved(leave_request) -> bool:
    return LeaveBalanceTransaction.objects.filter(
        leave_request=leave_request,
        transaction_type=BalanceTransactionType.RESERVE,
    ).exists()


def has_balance_been_released(leave_request) -> bool:
    return LeaveBalanceTransaction.objects.filter(
        leave_request=leave_request,
        transaction_type=BalanceTransactionType.RELEASE,
    ).exists()


def record_balance_change(
    *,
    employee,
    leave_type,
    year: int,
    delta_used_days=0,
    delta_pending_days=0,
    delta_allocated_days=0,
    transaction_type: str,
    source: str,
    leave_request=None,
    actor=None,
    reason: str = "",
    allow_insufficient_balance: bool = False,
    idempotency_key: Optional[str] = None,
    extra_balance_updates: Optional[dict] = None,
    effective_date=None,
) -> LeaveBalanceTransaction:
    """
    Atomically update used_days / pending_days / allocated_days and write a ledger row.
    delta_used_days > 0 deducts; delta_used_days < 0 refunds.
    delta_pending_days > 0 reserves; delta_pending_days < 0 releases or consumes.
    delta_allocated_days > 0 credits entitlement; < 0 expires/forfeits allocation.
    """
    from django.db import IntegrityError, transaction
    from django.db.models import F

    delta_used_days = _to_decimal(delta_used_days)
    delta_pending_days = _to_decimal(delta_pending_days)
    delta_allocated_days = _to_decimal(delta_allocated_days)

    if delta_used_days == 0 and delta_pending_days == 0 and delta_allocated_days == 0:
        raise ValidationError({"leave_balance": "Balance delta cannot be zero."})

    if idempotency_key:
        existing = LeaveBalanceTransaction.objects.filter(
            idempotency_key=idempotency_key
        ).first()
        if existing:
            return existing

    if delta_allocated_days != 0 and delta_used_days == 0 and delta_pending_days == 0:
        balance, _ = LeaveBalance.objects.get_or_create(
            employee=employee,
            leave_type=leave_type,
            year=year,
            defaults={
                "allocated_days": Decimal("0.00"),
                "used_days": Decimal("0.00"),
                "pending_days": Decimal("0.00"),
            },
        )
    else:
        balance = ensure_leave_balance_record(employee, leave_type, year)
    net_new = delta_used_days + delta_pending_days - delta_allocated_days

    if net_new > 0 and not allow_insufficient_balance:
        available = _to_decimal(balance.allocated_days) - _to_decimal(balance.used_days) - _to_decimal(
            balance.pending_days
        )
        if available < net_new:
            raise ValidationError(
                {
                    "leave_balance": leave_messages.insufficient_leave_balance(
                        leave_type,
                        year,
                        available=available,
                        requested=net_new,
                        format_days=format_leave_days,
                    )
                }
            )

    updates = {
        "used_days": F("used_days") + delta_used_days,
        "pending_days": F("pending_days") + delta_pending_days,
        "allocated_days": F("allocated_days") + delta_allocated_days,
    }
    if extra_balance_updates:
        updates.update(extra_balance_updates)

    try:
        with transaction.atomic():
            LeaveBalance.objects.filter(pk=balance.pk).update(**updates)
            return LeaveBalanceTransaction.objects.create(
                leave_balance=balance,
                leave_request=leave_request,
                transaction_type=transaction_type,
                source=source,
                delta_used_days=delta_used_days,
                delta_pending_days=delta_pending_days,
                delta_allocated_days=delta_allocated_days,
                idempotency_key=idempotency_key,
                actor=actor,
                reason=reason,
                effective_date=effective_date,
            )
    except IntegrityError:
        if idempotency_key:
            existing = LeaveBalanceTransaction.objects.filter(
                idempotency_key=idempotency_key
            ).first()
            if existing:
                return existing
        raise


def _request_year_days(leave_request) -> dict[int, Decimal]:
    return split_working_days_by_year(
        leave_request.start_date,
        leave_request.end_date,
        leave_type=leave_request.leave_type,
        is_half_day=getattr(leave_request, "is_half_day", False),
        employee=leave_request.employee,
    )


def reserve_leave_balance(
    leave_request,
    *,
    actor=None,
    reason: str = "",
) -> list[LeaveBalanceTransaction]:
    """Reserve working days as pending on submit (not for auto-approved / already deducted)."""
    if has_balance_been_deducted(leave_request) or has_balance_been_reserved(leave_request):
        return list(
            LeaveBalanceTransaction.objects.filter(
                leave_request=leave_request,
                transaction_type=BalanceTransactionType.RESERVE,
            )
        )

    year_days = _request_year_days(leave_request)
    transactions = []
    for year, days in year_days.items():
        txn = record_balance_change(
            employee=leave_request.employee,
            leave_type=leave_request.leave_type,
            year=year,
            delta_pending_days=days,
            transaction_type=BalanceTransactionType.RESERVE,
            source=BalanceTransactionSource.SUBMIT,
            leave_request=leave_request,
            actor=actor,
            reason=reason or "Leave submitted; balance reserved.",
        )
        transactions.append(txn)
    return transactions


def release_leave_balance(
    leave_request,
    *,
    actor=None,
    reason: str = "",
    source: str = BalanceTransactionSource.CANCEL_RELEASE,
) -> list[LeaveBalanceTransaction]:
    """Release a pending hold when a request is rejected or cancelled before approval."""
    if has_balance_been_deducted(leave_request):
        return []
    if not has_balance_been_reserved(leave_request):
        return []
    if has_balance_been_released(leave_request):
        return list(
            LeaveBalanceTransaction.objects.filter(
                leave_request=leave_request,
                transaction_type=BalanceTransactionType.RELEASE,
            )
        )

    reserve_txns = LeaveBalanceTransaction.objects.filter(
        leave_request=leave_request,
        transaction_type=BalanceTransactionType.RESERVE,
    ).select_related("leave_balance", "leave_balance__leave_type")

    transactions = []
    for reserve_txn in reserve_txns:
        release_days = -reserve_txn.delta_pending_days
        if release_days == 0:
            continue
        balance = reserve_txn.leave_balance
        txn = record_balance_change(
            employee=balance.employee,
            leave_type=balance.leave_type,
            year=balance.year,
            delta_pending_days=release_days,
            transaction_type=BalanceTransactionType.RELEASE,
            source=source,
            leave_request=leave_request,
            actor=actor,
            reason=reason,
            allow_insufficient_balance=True,
        )
        transactions.append(txn)
    return transactions


def deduct_leave_balance(
    leave_request,
    *,
    source: str = BalanceTransactionSource.APPROVAL,
    actor=None,
    reason: str = "",
    allow_insufficient_balance: bool = False,
) -> list[LeaveBalanceTransaction]:
    """Deduct working days across calendar years with ledger entries.

    If a pending reserve exists, consume it (pending → used) in the same DEDUCT rows.
    """
    if has_balance_been_deducted(leave_request):
        return list(
            LeaveBalanceTransaction.objects.filter(
                leave_request=leave_request,
                transaction_type=BalanceTransactionType.DEDUCT,
            )
        )

    consume_pending = has_balance_been_reserved(leave_request) and not has_balance_been_released(
        leave_request
    )
    year_days = _request_year_days(leave_request)
    transactions = []
    for year, days in year_days.items():
        txn = record_balance_change(
            employee=leave_request.employee,
            leave_type=leave_request.leave_type,
            year=year,
            delta_used_days=days,
            delta_pending_days=(-days if consume_pending else 0),
            transaction_type=BalanceTransactionType.DEDUCT,
            source=source,
            leave_request=leave_request,
            actor=actor,
            reason=reason,
            allow_insufficient_balance=allow_insufficient_balance,
        )
        transactions.append(txn)
    return transactions


def restore_leave_balance(
    leave_request,
    *,
    actor=None,
    reason: str = "",
) -> list[LeaveBalanceTransaction]:
    """
    Refund deducted days when an APPROVED request is cancelled.
    Guards against double-refund via unique REFUND constraint per request.
    Does not touch pending holds (those use release_leave_balance).
    """
    if not has_balance_been_deducted(leave_request):
        return []
    if has_balance_been_refunded(leave_request):
        raise ValidationError(
            {
                "leave_balance": (
                    "The leave balance for this request has already been refunded. "
                    "No further refund action is required."
                )
            }
        )

    deduct_txns = LeaveBalanceTransaction.objects.filter(
        leave_request=leave_request,
        transaction_type=BalanceTransactionType.DEDUCT,
    ).select_related("leave_balance", "leave_balance__leave_type")

    transactions = []
    for deduct_txn in deduct_txns:
        refund_days = -deduct_txn.delta_used_days
        if refund_days == 0:
            continue
        balance = deduct_txn.leave_balance
        txn = record_balance_change(
            employee=balance.employee,
            leave_type=balance.leave_type,
            year=balance.year,
            delta_used_days=refund_days,
            transaction_type=BalanceTransactionType.REFUND,
            source=BalanceTransactionSource.CANCEL_REFUND,
            leave_request=leave_request,
            actor=actor,
            reason=reason,
            allow_insufficient_balance=True,
        )
        transactions.append(txn)
    return transactions


def adjust_balance_for_reconciled_edit(
    old_request,
    new_request,
    *,
    actor,
    reason: str = "",
    allow_insufficient_balance: bool = False,
) -> list[LeaveBalanceTransaction]:
    """
    Apply net balance delta when HR edits a reconciled APPROVED request.
    Handles leave type and/or date changes including cross-year splits.
    """
    old_map = _year_days_for_request(old_request)
    new_map = _year_days_for_request(new_request)
    all_keys = set(old_map) | set(new_map)

    transactions = []
    for leave_type_id, year in all_keys:
        old_days = old_map.get((leave_type_id, year), 0)
        new_days = new_map.get((leave_type_id, year), 0)
        delta = new_days - old_days
        if delta == 0:
            continue

        leave_type = (
            new_request.leave_type
            if leave_type_id == new_request.leave_type_id
            else old_request.leave_type
        )
        if leave_type_id != leave_type.id:
            leave_type = LeaveType.objects.get(pk=leave_type_id)

        txn = record_balance_change(
            employee=new_request.employee,
            leave_type=leave_type,
            year=year,
            delta_used_days=delta,
            transaction_type=BalanceTransactionType.ADJUST,
            source=BalanceTransactionSource.RECONCILE_EDIT,
            leave_request=new_request,
            actor=actor,
            reason=reason,
            allow_insufficient_balance=allow_insufficient_balance,
        )
        transactions.append(txn)
    return transactions


def validate_reconcile_balance(
    employee,
    leave_type,
    start_date: datetime.date,
    end_date: datetime.date,
    *,
    allow_insufficient_balance: bool = False,
) -> None:
    """Validate sufficient balance per calendar year spanned by the date range."""
    if allow_insufficient_balance:
        return

    year_days = split_working_days_by_year(
        start_date, end_date, leave_type=leave_type, employee=employee
    )
    for year, days in year_days.items():
        ensure_leave_balance_record(employee, leave_type, year)
        WorkingDaysService.validate_leave_balance(
            employee=employee,
            leave_type=leave_type,
            year=year,
            requested_days=days,
        )


def reconcile_leave_request(
    *,
    hr_user,
    employee,
    leave_type,
    start_date: datetime.date,
    end_date: datetime.date,
    reason: str,
    reconciliation_note: str,
    cover_person=None,
    allow_insufficient_balance: bool = False,
) -> LeaveRequest:
    """
    Create an APPROVED, backdated leave request recorded by HR, deduct balance,
    and write an audit log. Caller should queue stakeholder notifications.
    """
    from django.db import transaction

    from .models import ApprovalAction, LeaveApprovalLog

    validate_backdating_for_reconcile(leave_type, start_date, employee=employee)
    validate_reconcile_balance(
        employee,
        leave_type,
        start_date,
        end_date,
        allow_insufficient_balance=allow_insufficient_balance,
    )

    with transaction.atomic():
        leave_request = LeaveRequest(
            employee=employee,
            leave_type=leave_type,
            cover_person=cover_person,
            start_date=start_date,
            end_date=end_date,
            reason=reason,
            status=LeaveRequestStatus.APPROVED,
            is_reconciled=True,
            reconciled_by=hr_user,
            reconciled_at=timezone.now(),
            reconciliation_note=reconciliation_note,
        )
        apply_policy_snapshot(leave_request)
        leave_request.save()
        LeaveApprovalLog.objects.create(
            leave_request=leave_request,
            actor=hr_user,
            action=ApprovalAction.RECONCILE,
            previous_status="",
            new_status=LeaveRequestStatus.APPROVED,
            comment=reconciliation_note,
        )
        deduct_leave_balance(
            leave_request,
            source=BalanceTransactionSource.RECONCILE,
            actor=hr_user,
            reason=reconciliation_note,
            allow_insufficient_balance=allow_insufficient_balance,
        )

    return leave_request


def validate_reconcile_row(
    *,
    employee,
    leave_type,
    start_date: datetime.date,
    end_date: datetime.date,
    cover_person=None,
    allow_insufficient_balance: bool = False,
    exclude_request_id=None,
) -> None:
    """Shared validation for single and bulk reconcile."""
    if start_date > end_date:
        raise ValidationError({"end_date": leave_messages.invalid_date_range()})

    if _leave_type_code(leave_type) == LeaveType.Code.MATERNITY and getattr(employee, "gender", None) != "FEMALE":
        raise ValidationError({"leave_type": leave_messages.maternity_not_eligible()})
    if _leave_type_code(leave_type) == LeaveType.Code.PATERNITY and getattr(employee, "gender", None) != "MALE":
        raise ValidationError({"leave_type": leave_messages.paternity_not_eligible()})

    validate_backdating_for_reconcile(leave_type, start_date, employee=employee)

    WorkingDaysService.check_overlapping_leave(
        employee=employee,
        start_date=start_date,
        end_date=end_date,
        exclude_id=exclude_request_id,
    )
    WorkingDaysService.check_department_leave_overlap(
        employee=employee,
        start_date=start_date,
        end_date=end_date,
        leave_type=leave_type,
        exclude_id=exclude_request_id,
    )
    validate_reconcile_balance(
        employee,
        leave_type,
        start_date,
        end_date,
        allow_insufficient_balance=allow_insufficient_balance,
    )

    if cover_person is not None:
        preview = LeaveRequest(
            employee=employee,
            leave_type=leave_type,
            start_date=start_date,
            end_date=end_date,
            cover_person=cover_person,
        )
        validate_cover_person_assignment(preview, cover_person, hr_override=True)

    duplicate_qs = LeaveRequest.objects.filter(
        employee=employee,
        leave_type=leave_type,
        start_date=start_date,
        end_date=end_date,
        status=LeaveRequestStatus.APPROVED,
    )
    if exclude_request_id:
        duplicate_qs = duplicate_qs.exclude(pk=exclude_request_id)
    if duplicate_qs.exists():
        raise ValidationError(
            leave_messages.duplicate_reconciled_leave(
                employee,
                leave_type,
                start_date,
                end_date,
            )
        )


def leave_starts_within_reminder_window(leave_request, *, today: Optional[datetime.date] = None) -> bool:
    """True when leave starts within the configured reminder lead (default ≈24h)."""
    today = today or timezone.localdate()
    lead = reminder_lead_days()
    return leave_request.start_date <= today + datetime.timedelta(days=lead)


def _relievers_at_scope(employee, scope_level: str, filters: dict):
    """Build queryset of eligible relievers at a given org scope."""
    if scope_level == "department":
        department = employee.department
        if department is None:
            return User.objects.none()
        return _department_members_queryset(department).exclude(pk=employee.pk)

    return (
        User.objects.filter(is_active=True, **filters)
        .exclude(pk=employee.pk)
        .select_related("department", "unit", "team")
    )


@dataclass(frozen=True)
class RelieverScopeResult:
    scope_level: Optional[str]
    effective_scope_level: Optional[str]
    fallback_applied: bool
    relievers: object  # QuerySet[User]


def get_eligible_relievers(employee, leave_type=None) -> RelieverScopeResult:
    """
    Return eligible relievers scoped to the employee's org level, cascading
    upward (team -> unit -> department) when no colleagues exist (AUTO scope).
    """
    policy = get_active_policy(leave_type, employee=employee) if leave_type is not None else None
    reliever_scope = getattr(policy, "reliever_scope", None) or OverlapScope.AUTO

    if reliever_scope == OverlapScope.ORGANIZATION:
        relievers = User.objects.filter(is_active=True).exclude(pk=employee.pk)
        return RelieverScopeResult(
            OverlapScope.ORGANIZATION,
            OverlapScope.ORGANIZATION,
            False,
            relievers,
        )

    if reliever_scope == OverlapScope.TEAM:
        filters = {"team_id": employee.team_id} if getattr(employee, "team_id", None) else None
        relievers = (
            _relievers_at_scope(employee, "team", filters)
            if filters
            else User.objects.none()
        )
        return RelieverScopeResult("team", "team", False, relievers)
    if reliever_scope == OverlapScope.UNIT:
        filters = {"unit_id": employee.unit_id} if getattr(employee, "unit_id", None) else None
        relievers = (
            _relievers_at_scope(employee, "unit", filters)
            if filters
            else User.objects.none()
        )
        return RelieverScopeResult("unit", "unit", False, relievers)
    if reliever_scope == OverlapScope.DEPARTMENT:
        relievers = _relievers_at_scope(employee, "department", {})
        return RelieverScopeResult("department", "department", False, relievers)

    primary_scope, primary_filters = resolve_org_scope(employee)
    if primary_scope is None:
        return RelieverScopeResult(None, None, False, User.objects.none())

    cascade_order: list[tuple[str, dict]] = [(primary_scope, primary_filters)]

    if primary_scope == "team" and getattr(employee, "unit_id", None):
        cascade_order.append(("unit", {"unit_id": employee.unit_id}))
    if primary_scope in ("team", "unit") and getattr(employee, "department_id", None):
        cascade_order.append(("department", {"department_id": employee.department_id}))

    for scope_level, filters in cascade_order:
        relievers = _relievers_at_scope(employee, scope_level, filters)
        if relievers.exists():
            fallback_applied = scope_level != primary_scope
            return RelieverScopeResult(
                scope_level=primary_scope,
                effective_scope_level=scope_level,
                fallback_applied=fallback_applied,
                relievers=relievers,
            )

    return RelieverScopeResult(
        scope_level=primary_scope,
        effective_scope_level=primary_scope,
        fallback_applied=False,
        relievers=User.objects.none(),
    )


def reliever_required(leave_request) -> bool:
    """Return True when a cover person must be assigned before submission."""
    employee = leave_request.employee
    if employee.has_role(RoleName.MANAGING_DIRECTOR) or employee.has_role(RoleName.EXECUTIVE_DIRECTOR):
        return False

    leave_type = leave_request.leave_type
    leave_type_code = _leave_type_code(leave_type)
    if leave_request.is_emergency and leave_type_code != LeaveType.Code.SICK:
        return False

    policy = get_active_policy(
        leave_type,
        on_date=getattr(leave_request, "start_date", None),
        employee=employee,
    )
    if policy is not None:
        return bool(policy.reliever_required)

    if leave_type_code in RELIEVER_EXEMPT_LEAVE_CODES:
        return False
    return True


def validate_cover_person_availability(
    cover_person,
    start_date: datetime.date,
    end_date: datetime.date,
    exclude_request_id: Optional[object] = None,
) -> None:
    """Raise ValidationError if cover_person has APPROVED leave overlapping dates."""
    if not (cover_person and start_date and end_date):
        return

    qs = LeaveRequest.objects.filter(
        employee=cover_person,
        start_date__lte=end_date,
        end_date__gte=start_date,
        status__in=IN_FLIGHT_OR_APPROVED_STATUSES,
    )
    if exclude_request_id is not None:
        qs = qs.exclude(pk=exclude_request_id)

    if qs.exists():
        conflicting = qs.select_related("employee", "leave_type").order_by("start_date").first()
        raise ValidationError(
            {
                "cover_person": leave_messages.reliever_unavailable(
                    cover_person,
                    conflicting,
                )
            }
        )


def validate_cover_person_assignment(
    leave_request,
    cover_person,
    *,
    hr_override: bool = False,
) -> None:
    """
    Validate an explicitly assigned cover person (create/PATCH).
    Does not enforce presence — use validate_cover_person_for_submission for that.
    """
    if cover_person is None:
        return

    employee = leave_request.employee

    if cover_person == employee:
        raise ValidationError({"cover_person": leave_messages.self_as_reliever()})

    if hr_override and not get_leave_settings().allow_hr_override:
        hr_override = False

    if hr_override:
        if not cover_person.is_active:
            raise ValidationError({"cover_person": leave_messages.reliever_inactive()})
    else:
        scope_result = get_eligible_relievers(employee, leave_request.leave_type)
        if not scope_result.relievers.filter(pk=cover_person.pk).exists():
            level = scope_result.effective_scope_level or "organisation"
            raise ValidationError(
                {"cover_person": leave_messages.reliever_not_eligible(level)}
            )

    validate_cover_person_availability(
        cover_person,
        leave_request.start_date,
        leave_request.end_date,
        exclude_request_id=leave_request.pk,
    )


def validate_cover_person_for_submission(
    leave_request,
    *,
    hr_override: bool = False,
) -> None:
    """Enforce reliever rules at submit / create-and-submit boundaries."""
    if not reliever_required(leave_request):
        return

    if leave_request.cover_person_id is None:
        raise ValidationError(
            {
                "cover_person": leave_messages.reliever_required(
                    leave_request.leave_type
                )
            }
        )

    validate_cover_person_assignment(
        leave_request,
        leave_request.cover_person,
        hr_override=hr_override,
    )


def validate_half_day_request(
    leave_type,
    start_date,
    end_date,
    is_half_day,
    half_day_period,
    employee=None,
) -> None:
    if not is_half_day:
        if half_day_period:
            raise ValidationError(
                {
                    "half_day_period": (
                        "half_day_period can only be set when is_half_day is true. "
                        "Clear half_day_period or set is_half_day to true."
                    )
                }
            )
        return

    policy = get_active_policy(leave_type, on_date=start_date, employee=employee)
    allowed = bool(policy.half_day_allowed) if policy is not None else False
    if not allowed:
        raise ValidationError(
            {
                "is_half_day": (
                    f"Half-day {leave_messages.leave_type_label(leave_type)} leave is not allowed "
                    "under your current leave policy. Request full-day leave instead, "
                    "or ask HR to enable half-day leave on the policy."
                )
            }
        )
    if start_date and end_date and start_date != end_date:
        raise ValidationError(
            {
                "end_date": (
                    "Half-day leave must start and end on the same date. "
                    "Set end_date equal to start_date or request a full-day range."
                )
            }
        )
    if not half_day_period:
        raise ValidationError(
            {
                "half_day_period": (
                    "Select AM or PM to indicate which half of the day you will be away."
                )
            }
        )
    if half_day_period not in HalfDayPeriod.values:
        raise ValidationError(
            {
                "half_day_period": (
                    "half_day_period must be AM or PM. "
                    "Use AM for a morning half-day or PM for an afternoon half-day."
                )
            }
        )


def _overlap_controlled_leave_type_ids(on_date=None):
    qs = LeavePolicy.objects.filter(
        status=LeavePolicyStatus.ACTIVE,
        overlap_control_enabled=True,
    )
    if on_date is not None:
        qs = qs.filter(
            Q(effective_from__isnull=True) | Q(effective_from__lte=on_date)
        ).filter(Q(effective_to__isnull=True) | Q(effective_to__gte=on_date))
    ids = list(qs.values_list("leave_type_id", flat=True))
    if ids:
        return ids
    return list(
        LeaveType.objects.filter(code__in=STAFFING_CONTROL_LEAVE_CODES).values_list("id", flat=True)
    )


class WorkingDaysService:
    """Stateless helper for working-day calculations and leave validations."""

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @staticmethod
    def calculate_working_days(
        start_date: datetime.date,
        end_date: datetime.date,
        leave_type=None,
        is_half_day: bool = False,
        employee=None,
    ):
        if leave_type is not None:
            return calculate_working_days_for_leave_type(
                start_date,
                end_date,
                leave_type,
                is_half_day=is_half_day,
                employee=employee,
            )
        return calculate_working_days(start_date, end_date, is_half_day=is_half_day)

    @staticmethod
    def validate_leave_balance(
        employee,
        leave_type,
        year: int,
        requested_days,
    ) -> None:
        """
        Raise ``ValidationError`` if the employee does not have enough available
        balance (allocated - used - pending) for *requested_days*.
        """
        requested_days = _to_decimal(requested_days)
        try:
            balance = LeaveBalance.objects.get(
                employee=employee,
                leave_type=leave_type,
                year=year,
            )
        except LeaveBalance.DoesNotExist:
            raise ValidationError(
                {
                    "leave_balance": leave_messages.no_leave_balance(leave_type, year)
                }
            )

        available = _to_decimal(balance.available_days)
        if available < requested_days:
            raise ValidationError(
                {
                    "leave_balance": leave_messages.insufficient_leave_balance(
                        leave_type,
                        year,
                        available=available,
                        requested=requested_days,
                        format_days=format_leave_days,
                    )
                }
            )

    @staticmethod
    def check_overlapping_leave(
        employee,
        start_date: datetime.date,
        end_date: datetime.date,
        exclude_id: Optional[object] = None,
    ) -> None:
        """
        Raise ``ValidationError`` if *employee* has an in-flight or approved
        leave request whose date range overlaps with the given window.
        """
        qs = LeaveRequest.objects.filter(
            employee=employee,
            start_date__lte=end_date,
            end_date__gte=start_date,
            status__in=IN_FLIGHT_OR_APPROVED_STATUSES,
        )

        if exclude_id is not None:
            qs = qs.exclude(pk=exclude_id)

        if qs.exists():
            conflicting = (
                qs.select_related("leave_type")
                .order_by("start_date")
                .first()
            )
            raise ValidationError(
                {"leave_request": leave_messages.self_overlapping_leave(conflicting)}
            )

    @staticmethod
    def check_department_leave_overlap(
        employee,
        start_date: datetime.date,
        end_date: datetime.date,
        leave_type=None,
        exclude_id: Optional[object] = None,
    ) -> list[str]:
        """
        Enforce configurable staffing overlap. Defaults match historical
        Annual/Casual one-person-per-org-scope blocking.

        Returns a list of warning messages when overlap_enforcement is WARN.
        """
        if not getattr(employee, "department_id", None):
            return []
        if not start_date or not end_date:
            return []

        policy = (
            get_active_policy(leave_type, on_date=start_date, employee=employee)
            if leave_type
            else None
        )
        if policy is not None:
            if not policy.overlap_control_enabled:
                return []
            overlap_scope = policy.overlap_scope or OverlapScope.AUTO
            max_absent = policy.maximum_people_absent or 1
            enforcement = policy.overlap_enforcement or OverlapEnforcement.BLOCK
        else:
            if leave_type and _leave_type_code(leave_type) not in STAFFING_CONTROL_LEAVE_CODES:
                return []
            overlap_scope = OverlapScope.AUTO
            max_absent = 1
            enforcement = OverlapEnforcement.BLOCK

        if overlap_scope == OverlapScope.ORGANIZATION:
            scope_filters = {}
        else:
            scope_filters = _leave_overlap_scope_filters(employee, overlap_scope)
            if overlap_scope != OverlapScope.ORGANIZATION and not scope_filters:
                return []

        type_ids = _overlap_controlled_leave_type_ids(on_date=start_date)

        qs = (
            LeaveRequest.objects.filter(
                **scope_filters,
                leave_type_id__in=type_ids,
                start_date__lte=end_date,
                end_date__gte=start_date,
                status__in=IN_FLIGHT_OR_APPROVED_STATUSES,
            )
            .exclude(employee=employee)
        )

        if exclude_id is not None:
            qs = qs.exclude(pk=exclude_id)

        absent_count = qs.values("employee_id").distinct().count()
        if absent_count < max_absent:
            return []

        conflicting = (
            qs.select_related("employee", "leave_type")
            .order_by("start_date")
            .first()
        )
        message = leave_messages.colleague_overlapping_leave(
            conflicting,
            requested_leave_type=leave_type,
        )

        if enforcement == OverlapEnforcement.WARN:
            return [message]
        raise ValidationError({"leave_request": message})


# ---------------------------------------------------------------------------
# Accrual, carry-forward, expiry (Sprint 4)
# ---------------------------------------------------------------------------

_DAY_QUANTUM = Decimal("0.01")


def quantize_leave_days(value) -> Decimal:
    return _to_decimal(value).quantize(_DAY_QUANTUM, rounding=ROUND_HALF_UP)


def employee_hire_date(employee) -> datetime.date:
    joined = getattr(employee, "date_joined", None)
    if joined is None:
        return timezone.localdate()
    if isinstance(joined, datetime.datetime):
        return timezone.localtime(joined).date() if timezone.is_aware(joined) else joined.date()
    return joined


def calendar_days_in_year(year: int) -> int:
    return 366 if calendar.isleap(year) else 365


def prorate_entitlement(
    annual_entitlement,
    join_date: datetime.date,
    year: int,
    *,
    enabled: bool = True,
) -> Decimal:
    """Reduce first-year entitlement by remaining calendar days in *year*."""
    annual = quantize_leave_days(annual_entitlement)
    if not enabled:
        return annual
    year_start = datetime.date(year, 1, 1)
    year_end = datetime.date(year, 12, 31)
    if join_date > year_end:
        return Decimal("0.00")
    if join_date <= year_start:
        return annual
    remaining = (year_end - join_date).days + 1
    return quantize_leave_days(annual * Decimal(remaining) / Decimal(calendar_days_in_year(year)))


def interval_count_for_method(method: str) -> int:
    if method == AccrualMethod.WEEKLY:
        return 52
    if method == AccrualMethod.MONTHLY:
        return 12
    return 1


def interval_accrual_rate(annual_entitlement, method: str, accrual_rate=None) -> Decimal:
    annual = quantize_leave_days(annual_entitlement)
    if accrual_rate is not None:
        return quantize_leave_days(accrual_rate)
    intervals = interval_count_for_method(method)
    if intervals <= 1:
        return annual
    return quantize_leave_days(annual / Decimal(intervals))


def apply_carry_forward(
    unused_days,
    *,
    allowed: bool,
    max_days=None,
) -> Decimal:
    """Return days to credit into the next year. Zero when carry-forward is disabled."""
    unused = max(quantize_leave_days(unused_days), Decimal("0.00"))
    if not allowed:
        return Decimal("0.00")
    if max_days is not None:
        return min(unused, quantize_leave_days(max_days))
    return unused


def carry_forward_expiry_date(year: int, expiry_months: Optional[int]) -> Optional[datetime.date]:
    """Last calendar day carried days remain valid in *year* (the destination leave year)."""
    if not expiry_months:
        return None
    month = expiry_months
    expiry_year = year
    while month > 12:
        month -= 12
        expiry_year += 1
    last_day = calendar.monthrange(expiry_year, month)[1]
    return datetime.date(expiry_year, month, last_day)


def accrue_for_year(
    annual_entitlement,
    *,
    method: str = AccrualMethod.UPFRONT,
    join_date: Optional[datetime.date] = None,
    year: int,
    prorate_new_joiners: bool = False,
    accrual_rate=None,
) -> Decimal:
    """Full-year credit after proration (cap for monthly/weekly = prorated annual)."""
    hire = join_date or datetime.date(year, 1, 1)
    return prorate_entitlement(
        annual_entitlement, hire, year, enabled=prorate_new_joiners
    )


def accrue_for_interval(
    annual_entitlement,
    *,
    method: str,
    year: int,
    month: Optional[int] = None,
    week: Optional[int] = None,
    as_of: Optional[datetime.date] = None,
    join_date: Optional[datetime.date] = None,
    prorate_new_joiners: bool = False,
    accrual_rate=None,
) -> Decimal:
    """
    Days credited for a single interval (one month, one week, or the annual lump).
    Does not exceed remaining room up to accrue_for_year().
    """
    cap = accrue_for_year(
        annual_entitlement,
        method=method,
        join_date=join_date,
        year=year,
        prorate_new_joiners=prorate_new_joiners,
        accrual_rate=accrual_rate,
    )
    if cap <= 0:
        return Decimal("0.00")

    hire = join_date or datetime.date(year, 1, 1)
    as_of = as_of or datetime.date(year, 12, 31)

    if method == AccrualMethod.UPFRONT:
        return cap

    if method == AccrualMethod.ANNIVERSARY:
        anniversary = datetime.date(year, hire.month, min(hire.day, calendar.monthrange(year, hire.month)[1]))
        if as_of < anniversary:
            return Decimal("0.00")
        return cap

    rate = interval_accrual_rate(annual_entitlement, method, accrual_rate)
    if method == AccrualMethod.MONTHLY:
        if month is None:
            month = as_of.month
        period_start = datetime.date(year, month, 1)
        if hire > datetime.date(year, month, calendar.monthrange(year, month)[1]):
            return Decimal("0.00")
        if hire.year == year and hire.month == month and prorate_new_joiners:
            days_in_month = calendar.monthrange(year, month)[1]
            remaining = days_in_month - hire.day + 1
            return quantize_leave_days(rate * Decimal(remaining) / Decimal(days_in_month))
        if period_start.year * 12 + period_start.month < hire.year * 12 + hire.month:
            return Decimal("0.00")
        return min(rate, cap)

    if method == AccrualMethod.WEEKLY:
        if week is None:
            week = as_of.isocalendar()[1]
        if as_of.isocalendar()[0] != year and week:
            pass
        iso_year, _, _ = datetime.date(year, 12, 28).isocalendar()
        week_monday = datetime.date.fromisocalendar(year, min(max(week, 1), 52), 1)
        if hire > week_monday + datetime.timedelta(days=6):
            return Decimal("0.00")
        return min(rate, cap)

    return cap


def accrued_to_date(
    annual_entitlement,
    *,
    method: str,
    year: int,
    as_of: datetime.date,
    join_date: Optional[datetime.date] = None,
    prorate_new_joiners: bool = False,
    accrual_rate=None,
) -> Decimal:
    cap = accrue_for_year(
        annual_entitlement,
        method=method,
        join_date=join_date,
        year=year,
        prorate_new_joiners=prorate_new_joiners,
        accrual_rate=accrual_rate,
    )
    if method in (AccrualMethod.UPFRONT, AccrualMethod.ANNIVERSARY):
        return accrue_for_interval(
            annual_entitlement,
            method=method,
            year=year,
            as_of=as_of,
            join_date=join_date,
            prorate_new_joiners=prorate_new_joiners,
            accrual_rate=accrual_rate,
        )

    total = Decimal("0.00")
    if method == AccrualMethod.MONTHLY:
        last_month = 12 if as_of.year > year else (as_of.month if as_of.year == year else 0)
        for month in range(1, last_month + 1):
            total += accrue_for_interval(
                annual_entitlement,
                method=method,
                year=year,
                month=month,
                as_of=as_of,
                join_date=join_date,
                prorate_new_joiners=prorate_new_joiners,
                accrual_rate=accrual_rate,
            )
        return min(quantize_leave_days(total), cap)

    last_week = as_of.isocalendar()[1] if as_of.year == year else (52 if as_of.year > year else 0)
    for week in range(1, last_week + 1):
        total += accrue_for_interval(
            annual_entitlement,
            method=method,
            year=year,
            week=week,
            as_of=as_of,
            join_date=join_date,
            prorate_new_joiners=prorate_new_joiners,
            accrual_rate=accrual_rate,
        )
    return min(quantize_leave_days(total), cap)


def _accrual_idempotency_key(employee_id, leave_type_id, policy_id, period: str) -> str:
    return f"accrual:{employee_id}:{leave_type_id}:{policy_id}:{period}"


def _unused_days(balance: LeaveBalance) -> Decimal:
    unused = (
        _to_decimal(balance.allocated_days)
        - _to_decimal(balance.used_days)
        - _to_decimal(balance.pending_days)
    )
    return max(quantize_leave_days(unused), Decimal("0.00"))


def _active_employees():
    return User.objects.filter(is_active=True)


def preview_or_run_accrual(
    *,
    as_of: Optional[datetime.date] = None,
    year: Optional[int] = None,
    month: Optional[int] = None,
    week: Optional[int] = None,
    include_rollover: bool = False,
    include_monthly: bool = False,
    include_weekly: bool = False,
    include_anniversary: bool = False,
    include_carry_expiry: bool = False,
    dry_run: bool = True,
) -> dict:
    """
    Accrual / rollover engine. When dry_run=True, no rows are written.
    """
    as_of = as_of or timezone.localdate()
    target_year = year if year is not None else as_of.year
    target_month = month if month is not None else as_of.month
    iso_week = week if week is not None else as_of.isocalendar()[1]

    created = []
    skipped = []
    employees = list(_active_employees())
    leave_types = list(LeaveType.objects.filter(is_active=True))

    def _note(action, employee, leave_type, extra):
        row = {
            "action": action,
            "employee_id": str(employee.pk),
            "employee_email": employee.email,
            "leave_type": leave_type.code,
            **extra,
        }
        created.append(row)
        return row

    if include_rollover:
        prior_year = target_year - 1
        for employee in employees:
            hire = employee_hire_date(employee)
            for leave_type in get_eligible_leave_types(employee):
                on_date = leave_year_start_date(target_year)
                resolution = resolve_leave_policy(employee, leave_type, on_date)
                policy = resolution.policy
                if policy is None:
                    policy = resolve_leave_policy(employee, leave_type, as_of).policy
                if policy is None:
                    skipped.append({"reason": "no_policy", "employee": employee.email, "leave_type": leave_type.code})
                    continue
                prior_on = leave_year_start_date(target_year) - datetime.timedelta(days=1)
                prior_policy = resolve_leave_policy(employee, leave_type, prior_on).policy or policy

                year_amount = accrue_for_year(
                    policy.annual_entitlement,
                    method=policy.accrual_method,
                    join_date=hire,
                    year=target_year,
                    prorate_new_joiners=policy.prorate_new_joiners,
                    accrual_rate=policy.accrual_rate,
                )
                jan_credit = accrue_for_interval(
                    policy.annual_entitlement,
                    method=policy.accrual_method,
                    year=target_year,
                    month=1,
                    week=1,
                    as_of=on_date,
                    join_date=hire,
                    prorate_new_joiners=policy.prorate_new_joiners,
                    accrual_rate=policy.accrual_rate,
                )

                prior = LeaveBalance.objects.filter(
                    employee=employee, leave_type=leave_type, year=prior_year
                ).first()
                unused = _unused_days(prior) if prior else Decimal("0.00")
                carried = apply_carry_forward(
                    unused,
                    allowed=prior_policy.carry_forward,
                    max_days=prior_policy.carry_forward_max_days,
                )
                expire_unused = Decimal("0.00")
                if prior and not prior_policy.carry_forward and unused > 0:
                    expire_unused = unused
                expires_on = (
                    carry_forward_expiry_date(target_year, prior_policy.carry_forward_expiry_months)
                    if carried > 0
                    else None
                )

                period_key = f"{target_year}"
                if policy.accrual_method == AccrualMethod.MONTHLY:
                    period_key = f"{target_year}-01"
                elif policy.accrual_method == AccrualMethod.WEEKLY:
                    period_key = f"{target_year}-w01"
                elif policy.accrual_method == AccrualMethod.ANNIVERSARY:
                    period_key = f"{target_year}-anniv"

                _note(
                    "rollover",
                    employee,
                    leave_type,
                    {
                        "year": target_year,
                        "accrual_days": str(jan_credit if policy.accrual_method != AccrualMethod.UPFRONT else year_amount),
                        "carry_forward_days": str(carried),
                        "expire_prior_days": str(expire_unused),
                        "policy_id": str(policy.pk),
                    },
                )
                if dry_run:
                    continue
                _apply_year_rollover_employee(
                    employee=employee,
                    leave_type=leave_type,
                    policy=policy,
                    prior_policy=prior_policy,
                    target_year=target_year,
                    prior_year=prior_year,
                    hire=hire,
                    year_amount=year_amount,
                    jan_credit=jan_credit,
                    carried=carried,
                    expire_unused=expire_unused,
                    expires_on=expires_on,
                    period_key=period_key,
                    prior_balance=prior,
                )

    if include_monthly:
        for employee in employees:
            hire = employee_hire_date(employee)
            for leave_type in get_eligible_leave_types(employee):
                on_date = datetime.date(target_year, target_month, 1)
                policy = resolve_leave_policy(employee, leave_type, on_date).policy
                if policy is None or policy.accrual_method != AccrualMethod.MONTHLY:
                    continue
                amount = accrue_for_interval(
                    policy.annual_entitlement,
                    method=policy.accrual_method,
                    year=target_year,
                    month=target_month,
                    as_of=on_date,
                    join_date=hire,
                    prorate_new_joiners=policy.prorate_new_joiners,
                    accrual_rate=policy.accrual_rate,
                )
                if amount <= 0:
                    continue
                _note(
                    "monthly_accrual",
                    employee,
                    leave_type,
                    {"year": target_year, "month": target_month, "days": str(amount), "policy_id": str(policy.pk)},
                )
                if dry_run:
                    continue
                _credit_accrual(
                    employee,
                    leave_type,
                    policy,
                    target_year,
                    amount,
                    _accrual_idempotency_key(
                        employee.pk, leave_type.pk, policy.pk, f"{target_year}-{target_month:02d}"
                    ),
                    reason=f"Monthly accrual {target_year}-{target_month:02d}.",
                )

    if include_weekly:
        for employee in employees:
            hire = employee_hire_date(employee)
            for leave_type in get_eligible_leave_types(employee):
                policy = resolve_leave_policy(employee, leave_type, as_of).policy
                if policy is None or policy.accrual_method != AccrualMethod.WEEKLY:
                    continue
                amount = accrue_for_interval(
                    policy.annual_entitlement,
                    method=policy.accrual_method,
                    year=target_year,
                    week=iso_week,
                    as_of=as_of,
                    join_date=hire,
                    prorate_new_joiners=policy.prorate_new_joiners,
                    accrual_rate=policy.accrual_rate,
                )
                if amount <= 0:
                    continue
                _note(
                    "weekly_accrual",
                    employee,
                    leave_type,
                    {"year": target_year, "week": iso_week, "days": str(amount), "policy_id": str(policy.pk)},
                )
                if dry_run:
                    continue
                _credit_accrual(
                    employee,
                    leave_type,
                    policy,
                    target_year,
                    amount,
                    _accrual_idempotency_key(
                        employee.pk, leave_type.pk, policy.pk, f"{target_year}-w{iso_week:02d}"
                    ),
                    reason=f"Weekly accrual {target_year} week {iso_week}.",
                )

    if include_anniversary:
        for employee in employees:
            hire = employee_hire_date(employee)
            if hire.month != as_of.month or hire.day != as_of.day:
                continue
            for leave_type in get_eligible_leave_types(employee):
                policy = resolve_leave_policy(employee, leave_type, as_of).policy
                if policy is None or policy.accrual_method != AccrualMethod.ANNIVERSARY:
                    continue
                amount = accrue_for_interval(
                    policy.annual_entitlement,
                    method=policy.accrual_method,
                    year=target_year,
                    as_of=as_of,
                    join_date=hire,
                    prorate_new_joiners=policy.prorate_new_joiners,
                    accrual_rate=policy.accrual_rate,
                )
                if amount <= 0:
                    continue
                _note(
                    "anniversary_accrual",
                    employee,
                    leave_type,
                    {"year": target_year, "days": str(amount), "policy_id": str(policy.pk)},
                )
                if dry_run:
                    continue
                _credit_accrual(
                    employee,
                    leave_type,
                    policy,
                    target_year,
                    amount,
                    _accrual_idempotency_key(
                        employee.pk, leave_type.pk, policy.pk, f"{target_year}-anniv"
                    ),
                    reason=f"Anniversary accrual {target_year}.",
                )

    if include_carry_expiry:
        qs = LeaveBalance.objects.filter(
            carry_forward_expires_on__lte=as_of,
            carried_forward_days__gt=0,
            employee__is_active=True,
        ).select_related("employee", "leave_type")
        for balance in qs:
            unused = _unused_days(balance)
            expire_amount = min(_to_decimal(balance.carried_forward_days), unused)
            if expire_amount <= 0:
                continue
            _note(
                "carry_forward_expiry",
                balance.employee,
                balance.leave_type,
                {
                    "year": balance.year,
                    "days": str(expire_amount),
                    "expires_on": balance.carry_forward_expires_on.isoformat(),
                },
            )
            if dry_run:
                continue
            key = f"cf-expiry:{balance.employee_id}:{balance.leave_type_id}:{balance.year}:{balance.carry_forward_expires_on.isoformat()}"
            record_balance_change(
                employee=balance.employee,
                leave_type=balance.leave_type,
                year=balance.year,
                delta_allocated_days=-expire_amount,
                transaction_type=BalanceTransactionType.EXPIRY,
                source=BalanceTransactionSource.EXPIRY_JOB,
                reason=f"Carry-forward expired on {balance.carry_forward_expires_on.isoformat()}.",
                allow_insufficient_balance=True,
                idempotency_key=key,
                extra_balance_updates={
                    "carried_forward_days": Decimal("0.00"),
                    "carry_forward_expires_on": None,
                },
            )

    return {
        "dry_run": dry_run,
        "as_of": as_of.isoformat(),
        "year": target_year,
        "actions": created,
        "skipped": skipped,
        "action_count": len(created),
    }


def _credit_accrual(employee, leave_type, policy, year, amount, idempotency_key, reason: str):
    if amount <= 0:
        return None
    cap = accrue_for_year(
        policy.annual_entitlement,
        method=policy.accrual_method,
        join_date=employee_hire_date(employee),
        year=year,
        prorate_new_joiners=policy.prorate_new_joiners,
        accrual_rate=policy.accrual_rate,
    )
    balance, _ = LeaveBalance.objects.get_or_create(
        employee=employee,
        leave_type=leave_type,
        year=year,
        defaults={
            "allocated_days": Decimal("0.00"),
            "used_days": Decimal("0.00"),
            "pending_days": Decimal("0.00"),
        },
    )
    already = _to_decimal(balance.allocated_days) - _to_decimal(balance.carried_forward_days)
    room = cap - already
    credit = min(quantize_leave_days(amount), max(room, Decimal("0.00")))
    if credit <= 0:
        existing = LeaveBalanceTransaction.objects.filter(idempotency_key=idempotency_key).first()
        return existing
    return record_balance_change(
        employee=employee,
        leave_type=leave_type,
        year=year,
        delta_allocated_days=credit,
        transaction_type=BalanceTransactionType.ACCRUAL,
        source=BalanceTransactionSource.ACCRUAL_JOB,
        reason=reason,
        allow_insufficient_balance=True,
        idempotency_key=idempotency_key,
    )


def _apply_year_rollover_employee(
    *,
    employee,
    leave_type,
    policy,
    prior_policy,
    target_year,
    prior_year,
    hire,
    year_amount,
    jan_credit,
    carried,
    expire_unused,
    expires_on,
    period_key,
    prior_balance,
):
    from django.db import transaction

    with transaction.atomic():
        if expire_unused > 0 and prior_balance:
            record_balance_change(
                employee=employee,
                leave_type=leave_type,
                year=prior_year,
                delta_allocated_days=-expire_unused,
                transaction_type=BalanceTransactionType.EXPIRY,
                source=BalanceTransactionSource.YEAR_ROLLOVER,
                reason=f"Unused {prior_year} entitlement expired (carry-forward disabled).",
                allow_insufficient_balance=True,
                idempotency_key=f"expiry:{employee.pk}:{leave_type.pk}:{prior_year}",
            )

        accrual_amount = (
            year_amount if policy.accrual_method == AccrualMethod.UPFRONT else jan_credit
        )
        if policy.accrual_method == AccrualMethod.ANNIVERSARY:
            accrual_amount = Decimal("0.00")
        if accrual_amount > 0:
            _credit_accrual(
                employee,
                leave_type,
                policy,
                target_year,
                accrual_amount,
                _accrual_idempotency_key(employee.pk, leave_type.pk, policy.pk, period_key),
                reason=f"Leave-year {target_year} accrual ({policy.accrual_method}).",
            )

        if carried > 0:
            cf_key = f"carry:{employee.pk}:{leave_type.pk}:{prior_policy.pk}:{prior_year}->{target_year}"
            extra = {
                "carried_forward_days": F_carried(carried),
                "carry_forward_expires_on": expires_on,
            }
            record_balance_change(
                employee=employee,
                leave_type=leave_type,
                year=target_year,
                delta_allocated_days=carried,
                transaction_type=BalanceTransactionType.CARRY_FORWARD,
                source=BalanceTransactionSource.YEAR_ROLLOVER,
                reason=f"Carry-forward from {prior_year} (cap applied).",
                allow_insufficient_balance=True,
                idempotency_key=cf_key,
                extra_balance_updates=extra,
            )


def F_carried(carried):
    from django.db.models import F

    return F("carried_forward_days") + carried


def _termination_already_settled(balance) -> bool:
    return LeaveBalanceTransaction.objects.filter(
        leave_balance=balance,
        source=BalanceTransactionSource.TERMINATION,
        transaction_type__in=(
            BalanceTransactionType.FORFEIT,
            BalanceTransactionType.ENCASH,
        ),
    ).exists()


def settle_balances_on_termination(employee) -> list:
    """
    On deactivation: FORFEIT unused days when policy.forfeited_on_resignation;
    otherwise ENCASH unused days when LeaveSettings.encashment_allowed.
    Idempotent — never double-forfeit or double-encash the same balance year.
    """
    written = []
    as_of = timezone.localdate()
    settings_row = get_leave_settings()
    balances = LeaveBalance.objects.filter(employee=employee, year=as_of.year).select_related(
        "leave_type"
    )
    for balance in balances:
        if _termination_already_settled(balance):
            continue
        unused = _unused_days(balance)
        if unused <= 0:
            continue
        policy = resolve_leave_policy(employee, balance.leave_type, as_of).policy
        extra = {
            "carried_forward_days": Decimal("0.00"),
            "carry_forward_expires_on": None,
        }
        if policy is not None and policy.forfeited_on_resignation:
            txn = record_balance_change(
                employee=employee,
                leave_type=balance.leave_type,
                year=balance.year,
                delta_allocated_days=-unused,
                transaction_type=BalanceTransactionType.FORFEIT,
                source=BalanceTransactionSource.TERMINATION,
                reason="Employment ended; unused leave forfeited per policy.",
                allow_insufficient_balance=True,
                idempotency_key=f"forfeit:{employee.pk}:{balance.leave_type_id}:{balance.year}",
                extra_balance_updates=extra,
                effective_date=as_of,
            )
            written.append(txn)
            continue
        if not settings_row.encashment_allowed:
            continue
        encash_days = unused
        if settings_row.encashment_max_days is not None:
            encash_days = min(encash_days, _to_decimal(settings_row.encashment_max_days))
        if encash_days <= 0:
            continue
        txn = record_balance_change(
            employee=employee,
            leave_type=balance.leave_type,
            year=balance.year,
            delta_allocated_days=-encash_days,
            transaction_type=BalanceTransactionType.ENCASH,
            source=BalanceTransactionSource.TERMINATION,
            reason="Employment ended; unused leave encashed for payroll settlement.",
            allow_insufficient_balance=True,
            idempotency_key=f"encash:{employee.pk}:{balance.leave_type_id}:{balance.year}",
            extra_balance_updates=extra,
            effective_date=as_of,
        )
        written.append(txn)
    return written


def forfeit_balances_on_termination(employee) -> list:
    """Back-compat alias used by Sprint 4 tests and the deactivation signal."""
    return settle_balances_on_termination(employee)


def adjust_leave_balance(
    balance: LeaveBalance,
    *,
    delta,
    reason: str,
    actor,
    effective_date=None,
    allow_insufficient_balance: bool = True,
) -> LeaveBalanceTransaction:
    """HR credit/debit of allocated_days. Positive delta increases entitlement."""
    delta = _to_decimal(delta)
    if delta == 0:
        raise ValidationError({"delta": leave_messages.balance_adjust_delta_zero()})
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": leave_messages.balance_adjust_reason_required()})
    return record_balance_change(
        employee=balance.employee,
        leave_type=balance.leave_type,
        year=balance.year,
        delta_allocated_days=delta,
        transaction_type=BalanceTransactionType.ADJUST,
        source=BalanceTransactionSource.HR_ADJUST,
        actor=actor,
        reason=reason,
        allow_insufficient_balance=allow_insufficient_balance,
        effective_date=effective_date,
    )


def overlapping_blackouts(
    *,
    start_date,
    end_date,
    leave_type,
    employee,
):
    from .models import LeaveBlackoutPeriod

    if not start_date or not end_date or not leave_type:
        return []
    qs = LeaveBlackoutPeriod.objects.filter(
        is_active=True,
        start_date__lte=end_date,
        end_date__gte=start_date,
    )
    dept_id = getattr(employee, "department_id", None)
    if dept_id:
        qs = qs.filter(Q(department__isnull=True) | Q(department_id=dept_id))
    else:
        qs = qs.filter(department__isnull=True)
    matched = []
    for period in qs.prefetch_related("leave_types"):
        types = list(period.leave_types.all())
        if types and leave_type not in types and leave_type.pk not in {t.pk for t in types}:
            continue
        matched.append(period)
    return matched


def validate_blackout_periods(
    *,
    start_date,
    end_date,
    leave_type,
    employee,
    actor=None,
    override_reason: str = "",
) -> list:
    """
    Enforce BLOCK blackouts. HR may override when LeaveSettings.allow_hr_override
    and a non-empty override_reason is provided. Returns WARN periods (non-blocking).
    """
    from .models import BlackoutEnforcement
    from apps.accounts.models import RoleName

    periods = overlapping_blackouts(
        start_date=start_date,
        end_date=end_date,
        leave_type=leave_type,
        employee=employee,
    )
    warnings = [p for p in periods if p.enforcement == BlackoutEnforcement.WARN]
    blocks = [p for p in periods if p.enforcement == BlackoutEnforcement.BLOCK]
    if not blocks:
        return warnings

    is_hr = bool(
        actor
        and (
            getattr(actor, "is_staff", False)
            or (hasattr(actor, "has_role") and actor.has_role(RoleName.HR))
        )
    )
    settings_row = get_leave_settings()
    reason = (override_reason or "").strip()
    if is_hr and settings_row.allow_hr_override and reason:
        return warnings

    names = ", ".join(p.name for p in blocks)
    raise ValidationError(
        leave_messages.blackout_blocked(
            names,
            hr_may_override=bool(is_hr and settings_row.allow_hr_override),
        )
    )


def utilization_report(*, year: int, department_id=None) -> list[dict]:
    from django.db.models import Sum

    qs = LeaveBalance.objects.filter(year=year).select_related(
        "employee__department", "leave_type"
    )
    if department_id:
        qs = qs.filter(employee__department_id=department_id)
    rows = (
        qs.values(
            "employee__department_id",
            "employee__department__name",
            "leave_type_id",
            "leave_type__name",
            "leave_type__code",
        )
        .annotate(
            allocated=Sum("allocated_days"),
            used=Sum("used_days"),
            pending=Sum("pending_days"),
        )
        .order_by("employee__department__name", "leave_type__name")
    )
    result = []
    for row in rows:
        allocated = _to_decimal(row["allocated"] or 0)
        used = _to_decimal(row["used"] or 0)
        result.append(
            {
                "department_id": str(row["employee__department_id"])
                if row["employee__department_id"]
                else None,
                "department_name": row["employee__department__name"] or "Unassigned",
                "leave_type_id": str(row["leave_type_id"]),
                "leave_type_name": row["leave_type__name"],
                "leave_type_code": row["leave_type__code"],
                "allocated_days": str(allocated),
                "used_days": str(used),
                "pending_days": str(_to_decimal(row["pending"] or 0)),
                "utilization": str(
                    (used / allocated).quantize(Decimal("0.0001")) if allocated else Decimal("0")
                ),
            }
        )
    return result


def who_is_out_report(*, start_date, end_date, department_id=None) -> list[dict]:
    qs = (
        LeaveRequest.objects.filter(status=LeaveRequestStatus.APPROVED)
        .filter(start_date__lte=end_date, end_date__gte=start_date)
        .select_related("employee__department", "leave_type")
        .order_by("start_date", "employee__email")
    )
    if department_id:
        qs = qs.filter(employee__department_id=department_id)
    return [
        {
            "id": str(req.id),
            "employee_id": str(req.employee_id),
            "employee_email": req.employee.email,
            "department_id": str(req.employee.department_id)
            if req.employee.department_id
            else None,
            "department_name": getattr(req.employee.department, "name", None) or "Unassigned",
            "leave_type": req.leave_type.name,
            "leave_type_code": req.leave_type.code,
            "start_date": req.start_date.isoformat(),
            "end_date": req.end_date.isoformat(),
            "total_working_days": str(req.total_working_days),
        }
        for req in qs
    ]


def liability_report(*, year: int, department_id=None) -> list[dict]:
    rows = utilization_report(year=year, department_id=department_id)
    for row in rows:
        allocated = _to_decimal(row["allocated_days"])
        used = _to_decimal(row["used_days"])
        row["liability_days"] = str(allocated - used)
    return rows

