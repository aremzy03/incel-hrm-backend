"""
Business-logic layer for leave management.

Keeping computation and validation here (instead of views/serializers) makes
the logic easy to unit-test and reuse across API endpoints, Celery tasks, etc.
"""

import datetime
from typing import Optional

from rest_framework.exceptions import ValidationError

from apps.accounts.models import Team, Unit

from .models import LeaveBalance, LeaveRequest, LeaveRequestStatus, LeaveType, PublicHoliday
from .utils import calculate_working_days


def get_eligible_leave_types(user):
    """Return leave types the user can apply for, based on gender."""
    qs = LeaveType.objects.all()
    gender = getattr(user, "gender", None)
    if gender == "FEMALE":
        qs = qs.exclude(name="Paternity")
    elif gender == "MALE":
        qs = qs.exclude(name="Maternity")
    return qs


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

        dept_id = employee.department_id

        department_has_units = Unit.objects.filter(department_id=dept_id).exists()
        department_has_teams = Team.objects.filter(unit__department_id=dept_id).exists()

        # Scope overlap rule to the lowest org level that exists:
        # - Team (if department has teams and employee is assigned to a team)
        # - Unit (if department has units and employee is assigned to a unit)
        # - Department (fallback)
        scope_filters = {"employee__department_id": dept_id}
        if department_has_teams and getattr(employee, "team_id", None):
            scope_filters = {"employee__team_id": employee.team_id}
        elif department_has_units and getattr(employee, "unit_id", None):
            scope_filters = {"employee__unit_id": employee.unit_id}

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
