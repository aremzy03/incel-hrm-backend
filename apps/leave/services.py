"""
Business-logic layer for leave management.

Keeping computation and validation here (instead of views/serializers) makes
the logic easy to unit-test and reuse across API endpoints, Celery tasks, etc.
"""

import datetime
from dataclasses import dataclass
from typing import Optional

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.accounts.models import (
    DepartmentMembership,
    RoleName,
    Team,
    Unit,
    get_or_create_management_department,
)

from .models import LeaveBalance, LeaveRequest, LeaveRequestStatus, LeaveType, PublicHoliday
from .utils import calculate_working_days

User = get_user_model()

RELIEVER_EXEMPT_LEAVE_TYPES = (
    "Sick",
    "Maternity",
    "Maternity Leave",
    "Paternity",
    "Paternity Leave",
)


def get_eligible_leave_types(user):
    """Return leave types the user can apply for, based on gender."""
    qs = LeaveType.objects.all()
    gender = getattr(user, "gender", None)
    if gender == "FEMALE":
        qs = qs.exclude(name="Paternity Leave")
    elif gender == "MALE":
        qs = qs.exclude(name="Maternity Leave")
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


def _leave_overlap_scope_filters(employee) -> dict:
    """Prefix resolve_org_scope filters with employee__ for LeaveRequest queries."""
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


def leave_starts_within_reminder_window(leave_request, *, today: Optional[datetime.date] = None) -> bool:
    """True when leave starts today or tomorrow (≈24h before start day)."""
    today = today or timezone.localdate()
    return leave_request.start_date <= today + datetime.timedelta(days=1)


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


def get_eligible_relievers(employee) -> RelieverScopeResult:
    """
    Return eligible relievers scoped to the employee's org level, cascading
    upward (team -> unit -> department) when no colleagues exist.
    """
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

    leave_type_name = leave_request.leave_type.name
    if leave_type_name in RELIEVER_EXEMPT_LEAVE_TYPES:
        return False

    if leave_request.is_emergency and leave_type_name != "Sick":
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
        status=LeaveRequestStatus.APPROVED,
    )
    if exclude_request_id is not None:
        qs = qs.exclude(pk=exclude_request_id)

    if qs.exists():
        raise ValidationError(
            {
                "cover_person": (
                    "The selected reliever already has approved leave that overlaps "
                    "with the requested dates."
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
        raise ValidationError(
            {"cover_person": "You cannot assign yourself as the cover person."}
        )

    if hr_override:
        if not cover_person.is_active:
            raise ValidationError({"cover_person": "The reliever must be an active user."})
    else:
        scope_result = get_eligible_relievers(employee)
        if not scope_result.relievers.filter(pk=cover_person.pk).exists():
            level = scope_result.effective_scope_level or "organisation"
            raise ValidationError(
                {
                    "cover_person": (
                        f"The reliever must be an active colleague in your {level}."
                    )
                }
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
            {"cover_person": "A reliever is required before submitting this leave request."}
        )

    validate_cover_person_assignment(
        leave_request,
        leave_request.cover_person,
        hr_override=hr_override,
    )


class WorkingDaysService:
    """Stateless helper for working-day calculations and leave validations."""

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @staticmethod
    def calculate_working_days(start_date: datetime.date, end_date: datetime.date) -> int:
        return calculate_working_days(start_date, end_date)

    @staticmethod
    def validate_leave_balance(
        employee,
        leave_type,
        year: int,
        requested_days: int,
    ) -> None:
        """
        Raise ``ValidationError`` if the employee does not have enough remaining
        balance for *requested_days* of *leave_type* in *year*.

        Raises
        ------
        ValidationError
            When no balance record exists or remaining_days < requested_days.
        """
        try:
            balance = LeaveBalance.objects.get(
                employee=employee,
                leave_type=leave_type,
                year=year,
            )
        except LeaveBalance.DoesNotExist:
            raise ValidationError(
                {
                    "leave_balance": (
                        f"No leave balance found for {leave_type.name} in {year}."
                    )
                }
            )

        if balance.remaining_days < requested_days:
            raise ValidationError(
                {
                    "leave_balance": (
                        f"Insufficient leave balance. "
                        f"Available: {balance.remaining_days}, "
                        f"Requested: {requested_days}"
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
        Raise ``ValidationError`` if *employee* has an active (non-rejected,
        non-cancelled) leave request whose date range overlaps with the
        given *start_date*–*end_date* window.

        Pass *exclude_id* when editing an existing request so that the request
        being edited does not trigger a false conflict.

        Raises
        ------
        ValidationError
            When an overlapping active leave request is found.
        """
        qs = LeaveRequest.objects.filter(
            employee=employee,
            # Overlap condition: existing.start <= new.end AND existing.end >= new.start
            start_date__lte=end_date,
            end_date__gte=start_date,
            status=LeaveRequestStatus.APPROVED,
        )

        if exclude_id is not None:
            qs = qs.exclude(pk=exclude_id)

        if qs.exists():
            raise ValidationError(
                {"leave_request": "You have an overlapping leave request."}
            )

    @staticmethod
    def check_department_leave_overlap(
        employee,
        start_date: datetime.date,
        end_date: datetime.date,
        leave_type=None,
        exclude_id: Optional[object] = None,
    ) -> None:
        """
        Raise ``ValidationError`` if another employee in the same department
        already has an active (non-rejected, non-cancelled) leave request
        overlapping the given date range.

        This rule applies only to Annual and Casual leave. For Sick, Maternity,
        Paternity, and other types, multiple employees in the same department
        may be on leave at the same time.
        """
        if not getattr(employee, "department_id", None):
            return
        if not start_date or not end_date:
            return
        # Only enforce "one per department" for Annual and Casual leave
        if leave_type and leave_type.name not in ("Annual", "Casual"):
            return

        scope_filters = _leave_overlap_scope_filters(employee)
        if not scope_filters:
            return

        # Only check overlaps with other Annual/Casual requests (active statuses)
        qs = (
            LeaveRequest.objects.filter(
                **scope_filters,
                leave_type__name__in=("Annual", "Casual"),
                start_date__lte=end_date,
                end_date__gte=start_date,
                status=LeaveRequestStatus.APPROVED,
            )
            .exclude(employee=employee)
        )

        if exclude_id is not None:
            qs = qs.exclude(pk=exclude_id)

        if qs.exists():
            raise ValidationError(
                {
                    "leave_request": (
                        "Another employee in your department already has an Annual or "
                        "Casual leave request that overlaps with the requested dates."
                    )
                }
            )
