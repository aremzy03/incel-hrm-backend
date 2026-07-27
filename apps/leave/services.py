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

from .models import (
    BalanceTransactionSource,
    BalanceTransactionType,
    LeaveBalance,
    LeaveBalanceTransaction,
    LeavePolicy,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveType,
    PublicHoliday,
)
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
    balance, _ = LeaveBalance.objects.get_or_create(
        employee=employee,
        leave_type=leave_type,
        year=year,
        defaults={
            "allocated_days": leave_type.default_days,
            "used_days": 0,
        },
    )
    return balance


def get_active_policy(leave_type) -> Optional[LeavePolicy]:
    """Return the most recently created policy for a leave type, if any."""
    return (
        LeavePolicy.objects.filter(leave_type=leave_type)
        .order_by("-created_at")
        .first()
    )


def validate_backdating_for_reconcile(leave_type, start_date: datetime.date) -> None:
    """
    Enforce LeavePolicy backdating rules for HR reconciliation.
    Permissive when no policy exists.
    """
    policy = get_active_policy(leave_type)
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
) -> dict[int, int]:
    """Return {calendar_year: working_days} for each year spanned by the range."""
    if start_date > end_date:
        return {}

    if start_date.year == end_date.year:
        days = calculate_working_days(start_date, end_date)
        return {start_date.year: days} if days else {}

    result: dict[int, int] = {}
    for year in range(start_date.year, end_date.year + 1):
        segment_start = start_date if year == start_date.year else datetime.date(year, 1, 1)
        segment_end = end_date if year == end_date.year else datetime.date(year, 12, 31)
        days = calculate_working_days(segment_start, segment_end)
        if days:
            result[year] = days
    return result


def _year_days_for_request(leave_request) -> dict[tuple, int]:
    """Map (leave_type_id, year) -> working days for a leave request."""
    splits = split_working_days_by_year(leave_request.start_date, leave_request.end_date)
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


def record_balance_change(
    *,
    employee,
    leave_type,
    year: int,
    delta_used_days: int,
    transaction_type: str,
    source: str,
    leave_request=None,
    actor=None,
    reason: str = "",
    allow_insufficient_balance: bool = False,
) -> LeaveBalanceTransaction:
    """
    Atomically update used_days and write an immutable ledger row.
    delta_used_days > 0 deducts; delta_used_days < 0 refunds.
    """
    from django.db import transaction
    from django.db.models import F

    if delta_used_days == 0:
        raise ValidationError({"leave_balance": "Balance delta cannot be zero."})

    balance = ensure_leave_balance_record(employee, leave_type, year)

    if delta_used_days > 0 and not allow_insufficient_balance:
        remaining = balance.allocated_days - balance.used_days
        if remaining < delta_used_days:
            raise ValidationError(
                {
                    "leave_balance": (
                        f"Insufficient leave balance for {leave_type.name} in {year}. "
                        f"Available: {remaining}, Requested: {delta_used_days}"
                    )
                }
            )

    with transaction.atomic():
        LeaveBalance.objects.filter(pk=balance.pk).update(
            used_days=F("used_days") + delta_used_days,
        )
        return LeaveBalanceTransaction.objects.create(
            leave_balance=balance,
            leave_request=leave_request,
            transaction_type=transaction_type,
            source=source,
            delta_used_days=delta_used_days,
            actor=actor,
            reason=reason,
        )


def deduct_leave_balance(
    leave_request,
    *,
    source: str = BalanceTransactionSource.APPROVAL,
    actor=None,
    reason: str = "",
    allow_insufficient_balance: bool = False,
) -> list[LeaveBalanceTransaction]:
    """Deduct working days across calendar years with ledger entries."""
    if has_balance_been_deducted(leave_request):
        return list(
            LeaveBalanceTransaction.objects.filter(
                leave_request=leave_request,
                transaction_type=BalanceTransactionType.DEDUCT,
            )
        )

    year_days = split_working_days_by_year(
        leave_request.start_date,
        leave_request.end_date,
    )
    transactions = []
    for year, days in year_days.items():
        txn = record_balance_change(
            employee=leave_request.employee,
            leave_type=leave_request.leave_type,
            year=year,
            delta_used_days=days,
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
    """
    if not has_balance_been_deducted(leave_request):
        return []
    if has_balance_been_refunded(leave_request):
        raise ValidationError(
            {"leave_balance": "Balance has already been refunded for this leave request."}
        )

    deduct_txns = LeaveBalanceTransaction.objects.filter(
        leave_request=leave_request,
        transaction_type=BalanceTransactionType.DEDUCT,
    ).select_related("leave_balance", "leave_balance__leave_type")

    transactions = []
    for deduct_txn in deduct_txns:
        refund_days = -deduct_txn.delta_used_days
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

    year_days = split_working_days_by_year(start_date, end_date)
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

    validate_backdating_for_reconcile(leave_type, start_date)
    validate_reconcile_balance(
        employee,
        leave_type,
        start_date,
        end_date,
        allow_insufficient_balance=allow_insufficient_balance,
    )

    with transaction.atomic():
        leave_request = LeaveRequest.objects.create(
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
        raise ValidationError({"end_date": "end_date must be on or after start_date."})

    if leave_type.name in ("Maternity", "Maternity Leave") and getattr(employee, "gender", None) != "FEMALE":
        raise ValidationError({"leave_type": "Maternity leave is only available for female staff."})
    if leave_type.name in ("Paternity", "Paternity Leave") and getattr(employee, "gender", None) != "MALE":
        raise ValidationError({"leave_type": "Paternity leave is only available for male staff."})

    validate_backdating_for_reconcile(leave_type, start_date)

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
            "An approved leave request already exists for this employee, "
            "leave type, and date range."
        )


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
