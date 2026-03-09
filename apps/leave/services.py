"""
Business-logic layer for leave management.

Keeping computation and validation here (instead of views/serializers) makes
the logic easy to unit-test and reuse across API endpoints, Celery tasks, etc.
"""

import datetime
from typing import Optional

from rest_framework.exceptions import ValidationError

from .models import LeaveBalance, LeaveRequest, LeaveRequestStatus, PublicHoliday


class WorkingDaysService:
    """Stateless helper for working-day calculations and leave validations."""

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @staticmethod
    def calculate_working_days(start_date: datetime.date, end_date: datetime.date) -> int:
        """
        Return the number of working days between *start_date* and *end_date*
        (both inclusive).

        A working day is a weekday (Mon–Fri) that is not a PublicHoliday.
        Recurring holidays are matched by (month, day) regardless of year.
        """
        if start_date > end_date:
            return 0

        # Fetch all holidays whose stored dates fall in the requested range,
        # plus all *recurring* holidays (matched by month+day below).
        holidays_in_range: set[datetime.date] = set(
            PublicHoliday.objects.filter(
                is_recurring=False,
                date__range=(start_date, end_date),
            ).values_list("date", flat=True)
        )

        recurring_holidays = list(
            PublicHoliday.objects.filter(is_recurring=True).values_list(
                "date__month", "date__day"
            )
        )

        count = 0
        current = start_date
        one_day = datetime.timedelta(days=1)

        while current <= end_date:
            # Skip weekends
            if current.weekday() >= 5:
                current += one_day
                continue

            # Skip exact-date public holidays
            if current in holidays_in_range:
                current += one_day
                continue

            # Skip recurring holidays matched by (month, day)
            if any(current.month == m and current.day == d for m, d in recurring_holidays):
                current += one_day
                continue

            count += 1
            current += one_day

        return count

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
        excluded_statuses = [
            LeaveRequestStatus.REJECTED,
            LeaveRequestStatus.CANCELLED,
        ]

        qs = LeaveRequest.objects.filter(
            employee=employee,
            # Overlap condition: existing.start <= new.end AND existing.end >= new.start
            start_date__lte=end_date,
            end_date__gte=start_date,
        ).exclude(status__in=excluded_statuses)

        if exclude_id is not None:
            qs = qs.exclude(pk=exclude_id)

        if qs.exists():
            raise ValidationError(
                {"leave_request": "You have an overlapping leave request."}
            )
