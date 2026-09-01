import datetime
import re
from decimal import Decimal


def slug_leave_type_code(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", (name or "").strip()).strip("_").upper()
    return (slug or "LEAVE_TYPE")[:32]


def format_leave_days(value) -> str:
    """Render a day count without trailing .00 (e.g. 5 or 0.5)."""
    amount = Decimal(str(value))
    if amount == amount.to_integral_value():
        return str(int(amount))
    return format(amount.normalize(), "f")


DEFAULT_WORKING_WEEKDAYS = (0, 1, 2, 3, 4)  # Monday–Friday


def calculate_working_days(
    start_date: datetime.date,
    end_date: datetime.date,
    *,
    weekend_excluded: bool = True,
    public_holiday_excluded: bool = True,
    is_half_day: bool = False,
    working_weekdays: tuple[int, ...] | list[int] | None = None,
    holidays_in_range: set[datetime.date] | None = None,
    recurring_holidays: list[tuple[int, int]] | None = None,
):
    """
    Return the number of counted days between *start_date* and *end_date*
    (both inclusive) as a Decimal.

    Defaults match historical behaviour: Monday–Friday, public holidays excluded.
    Recurring holidays are matched by (month, day) regardless of year.
    Half-day requests count 0.5 when the single day is a working day.

    Pass *working_weekdays* / holiday sets when a resolved calendar is available.
    When holiday sets are omitted, global PublicHoliday rows are used (legacy default).
    """
    if start_date > end_date:
        return Decimal("0")

    weekdays = set(working_weekdays if working_weekdays is not None else DEFAULT_WORKING_WEEKDAYS)

    if public_holiday_excluded and holidays_in_range is None and recurring_holidays is None:
        from .models import PublicHoliday

        holidays_in_range = set(
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
    else:
        holidays_in_range = holidays_in_range or set()
        recurring_holidays = recurring_holidays or []

    count = 0
    current = start_date
    one_day = datetime.timedelta(days=1)

    while current <= end_date:
        if weekend_excluded and current.weekday() not in weekdays:
            current += one_day
            continue

        if public_holiday_excluded:
            if current in holidays_in_range:
                current += one_day
                continue
            if any(current.month == m and current.day == d for m, d in recurring_holidays):
                current += one_day
                continue

        count += 1
        current += one_day

    if is_half_day:
        return Decimal("0.5") if count > 0 else Decimal("0")
    return Decimal(count)

