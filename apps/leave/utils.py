import datetime


def calculate_working_days(start_date: datetime.date, end_date: datetime.date) -> int:
    """
    Return the number of working days between *start_date* and *end_date*
    (both inclusive).

    A working day is a weekday (Mon–Fri) that is not a PublicHoliday.
    Recurring holidays are matched by (month, day) regardless of year.
    """
    if start_date > end_date:
        return 0

    # Import lazily to avoid circular import with apps.leave.models
    from .models import PublicHoliday

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

