import datetime

import pytest

from apps.leave.models import LeaveRequest, LeaveRequestStatus, PublicHoliday


@pytest.mark.django_db
def test_leave_request_save_excludes_weekends_and_public_holidays(django_user_model):
    """
    Regression test: LeaveRequest.save() should compute total_working_days
    excluding weekends and PublicHoliday dates.
    """
    user = django_user_model.objects.create_user(email="emp@example.com", password="pass1234")

    # Monday → Friday week, with Wed as a public holiday
    start = datetime.date(2026, 6, 8)  # Mon
    end = datetime.date(2026, 6, 12)  # Fri

    PublicHoliday.objects.create(
        name="Test Public Holiday",
        date=datetime.date(2026, 6, 10),
        is_recurring=False,
    )

    leave_type = None
    # LeaveType is required FK; create minimal compatible row via ORM import
    from apps.leave.models import LeaveType

    leave_type = LeaveType.objects.create(name="Annual-Test", default_days=10)

    req = LeaveRequest.objects.create(
        employee=user,
        leave_type=leave_type,
        cover_person=user,
        start_date=start,
        end_date=end,
        status=LeaveRequestStatus.DRAFT,
    )

    # Mon, Tue, Thu, Fri = 4 working days (Wed holiday excluded)
    assert req.total_working_days == 4

