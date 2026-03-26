"""
Unit tests for apps.leave.services.WorkingDaysService.

Test matrix
-----------
calculate_working_days
  - single weekday
  - single weekend day (Saturday)
  - single weekend day (Sunday)
  - full Mon–Fri week (5 days)
  - Friday–Monday span (only Fri + Mon count)
  - range containing a non-recurring public holiday mid-range
  - range containing a recurring public holiday mid-range
  - range where start_date > end_date → 0
  - two consecutive public holidays at start of range

validate_leave_balance
  - sufficient balance (exact boundary: remaining == requested)
  - insufficient balance
  - no balance record exists

check_overlapping_leave
  - no existing requests → passes
  - exact same date range → raises
  - partial overlap at start
  - partial overlap at end
  - new range completely inside existing range
  - new range completely contains existing range
  - REJECTED request does not block (adjacent)
  - CANCELLED request does not block
  - exclude_id skips the specified request
"""

import datetime

from django.test import TestCase
from rest_framework.exceptions import ValidationError

from apps.leave.models import (
    LeaveBalance,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveType,
    PublicHoliday,
)
from apps.leave.services import WorkingDaysService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_user(email="worker@test.com"):
    from django.contrib.auth import get_user_model
    User = get_user_model()
    return User.objects.create_user(email=email, password="testpass123")


def make_leave_type(name="Annual", default_days=21):
    lt, _ = LeaveType.objects.get_or_create(
        name=name, defaults={"default_days": default_days}
    )
    return lt


def make_balance(employee, leave_type, year=2025, allocated=10, used=0):
    return LeaveBalance.objects.create(
        employee=employee,
        leave_type=leave_type,
        year=year,
        allocated_days=allocated,
        used_days=used,
    )


def make_request(employee, leave_type, start, end, status=LeaveRequestStatus.PENDING_MANAGER):
    return LeaveRequest.objects.create(
        employee=employee,
        leave_type=leave_type,
        start_date=start,
        end_date=end,
        status=status,
    )


# ---------------------------------------------------------------------------
# calculate_working_days
# ---------------------------------------------------------------------------

class CalculateWorkingDaysTests(TestCase):

    def test_single_weekday(self):
        # Wednesday
        d = datetime.date(2025, 1, 8)
        self.assertEqual(WorkingDaysService.calculate_working_days(d, d), 1)

    def test_single_saturday_returns_zero(self):
        d = datetime.date(2025, 1, 4)  # Saturday
        self.assertEqual(WorkingDaysService.calculate_working_days(d, d), 0)

    def test_single_sunday_returns_zero(self):
        d = datetime.date(2025, 1, 5)  # Sunday
        self.assertEqual(WorkingDaysService.calculate_working_days(d, d), 0)

    def test_full_weekday_week(self):
        # Mon 6 Jan – Fri 10 Jan 2025
        start = datetime.date(2025, 1, 6)
        end = datetime.date(2025, 1, 10)
        self.assertEqual(WorkingDaysService.calculate_working_days(start, end), 5)

    def test_friday_to_monday_span(self):
        # Fri 10 Jan + Sat + Sun + Mon 13 Jan → only Fri and Mon count
        start = datetime.date(2025, 1, 10)  # Friday
        end = datetime.date(2025, 1, 13)    # Monday
        self.assertEqual(WorkingDaysService.calculate_working_days(start, end), 2)

    def test_start_after_end_returns_zero(self):
        start = datetime.date(2025, 1, 10)
        end = datetime.date(2025, 1, 8)
        self.assertEqual(WorkingDaysService.calculate_working_days(start, end), 0)

    def test_non_recurring_public_holiday_mid_range(self):
        # Mon–Fri week, Wednesday is a public holiday → 4 working days
        holiday_date = datetime.date(2025, 1, 8)  # Wednesday
        PublicHoliday.objects.create(
            name="Special Day", date=holiday_date, is_recurring=False
        )
        start = datetime.date(2025, 1, 6)
        end = datetime.date(2025, 1, 10)
        self.assertEqual(WorkingDaysService.calculate_working_days(start, end), 4)

    def test_recurring_public_holiday_mid_range(self):
        # Create a recurring holiday stored as 2024 date but matching Wed 8 Jan by (month=1, day=8)
        PublicHoliday.objects.create(
            name="Recurring Day",
            date=datetime.date(2024, 1, 8),  # stored year irrelevant for recurring
            is_recurring=True,
        )
        start = datetime.date(2025, 1, 6)
        end = datetime.date(2025, 1, 10)
        # Wednesday 8 Jan 2025 matches month=1, day=8 → excluded
        self.assertEqual(WorkingDaysService.calculate_working_days(start, end), 4)

    def test_two_consecutive_holidays_at_start(self):
        # Mon and Tue are holidays; Wed–Fri remain → 3 days
        PublicHoliday.objects.create(
            name="Holiday Mon", date=datetime.date(2025, 1, 6), is_recurring=False
        )
        PublicHoliday.objects.create(
            name="Holiday Tue", date=datetime.date(2025, 1, 7), is_recurring=False
        )
        start = datetime.date(2025, 1, 6)
        end = datetime.date(2025, 1, 10)
        self.assertEqual(WorkingDaysService.calculate_working_days(start, end), 3)

    def test_holiday_on_weekend_not_double_counted(self):
        # A public holiday that falls on a Saturday should not affect the count
        PublicHoliday.objects.create(
            name="Weekend Holiday", date=datetime.date(2025, 1, 4), is_recurring=False
        )
        start = datetime.date(2025, 1, 6)  # Monday
        end = datetime.date(2025, 1, 10)   # Friday
        self.assertEqual(WorkingDaysService.calculate_working_days(start, end), 5)


# ---------------------------------------------------------------------------
# validate_leave_balance
# ---------------------------------------------------------------------------

class ValidateLeaveBalanceTests(TestCase):

    def setUp(self):
        self.employee = make_user("balance@test.com")
        self.leave_type = make_leave_type()

    def test_exact_boundary_passes(self):
        # remaining == requested should not raise
        make_balance(self.employee, self.leave_type, year=2025, allocated=5, used=0)
        # Should not raise
        WorkingDaysService.validate_leave_balance(
            self.employee, self.leave_type, 2025, requested_days=5
        )

    def test_sufficient_balance_passes(self):
        make_balance(self.employee, self.leave_type, year=2025, allocated=10, used=3)
        WorkingDaysService.validate_leave_balance(
            self.employee, self.leave_type, 2025, requested_days=5
        )

    def test_insufficient_balance_raises(self):
        make_balance(self.employee, self.leave_type, year=2025, allocated=5, used=3)
        # remaining = 2, requesting 3
        with self.assertRaises(ValidationError) as ctx:
            WorkingDaysService.validate_leave_balance(
                self.employee, self.leave_type, 2025, requested_days=3
            )
        detail = str(ctx.exception.detail)
        self.assertIn("Insufficient leave balance", detail)
        self.assertIn("Available: 2", detail)
        self.assertIn("Requested: 3", detail)

    def test_no_balance_record_raises(self):
        # No LeaveBalance row exists
        with self.assertRaises(ValidationError) as ctx:
            WorkingDaysService.validate_leave_balance(
                self.employee, self.leave_type, 2025, requested_days=1
            )
        detail = str(ctx.exception.detail)
        self.assertIn("No leave balance found", detail)

    def test_wrong_year_raises(self):
        make_balance(self.employee, self.leave_type, year=2025, allocated=10, used=0)
        with self.assertRaises(ValidationError):
            WorkingDaysService.validate_leave_balance(
                self.employee, self.leave_type, 2100, requested_days=1
            )


# ---------------------------------------------------------------------------
# check_overlapping_leave
# ---------------------------------------------------------------------------

class CheckOverlappingLeaveTests(TestCase):

    def setUp(self):
        self.employee = make_user("overlap@test.com")
        self.leave_type = make_leave_type("Sick", default_days=14)

    # --- helpers ---

    def _make_active(self, start, end):
        return make_request(
            self.employee, self.leave_type, start, end,
            status=LeaveRequestStatus.PENDING_MANAGER,
        )

    # --- passing cases ---

    def test_no_existing_requests_passes(self):
        WorkingDaysService.check_overlapping_leave(
            self.employee,
            datetime.date(2025, 3, 3),
            datetime.date(2025, 3, 7),
        )

    def test_adjacent_range_before_passes(self):
        # Existing: Mon–Fri; new: following Mon–Fri
        self._make_active(datetime.date(2025, 3, 3), datetime.date(2025, 3, 7))
        WorkingDaysService.check_overlapping_leave(
            self.employee,
            datetime.date(2025, 3, 10),
            datetime.date(2025, 3, 14),
        )

    def test_adjacent_range_after_passes(self):
        self._make_active(datetime.date(2025, 3, 10), datetime.date(2025, 3, 14))
        WorkingDaysService.check_overlapping_leave(
            self.employee,
            datetime.date(2025, 3, 3),
            datetime.date(2025, 3, 7),
        )

    def test_rejected_request_does_not_block(self):
        make_request(
            self.employee, self.leave_type,
            datetime.date(2025, 3, 3), datetime.date(2025, 3, 7),
            status=LeaveRequestStatus.REJECTED,
        )
        WorkingDaysService.check_overlapping_leave(
            self.employee,
            datetime.date(2025, 3, 3),
            datetime.date(2025, 3, 7),
        )

    def test_cancelled_request_does_not_block(self):
        make_request(
            self.employee, self.leave_type,
            datetime.date(2025, 3, 3), datetime.date(2025, 3, 7),
            status=LeaveRequestStatus.CANCELLED,
        )
        WorkingDaysService.check_overlapping_leave(
            self.employee,
            datetime.date(2025, 3, 3),
            datetime.date(2025, 3, 7),
        )

    def test_exclude_id_skips_own_request(self):
        req = self._make_active(datetime.date(2025, 3, 3), datetime.date(2025, 3, 7))
        # Editing the same request should not conflict with itself
        WorkingDaysService.check_overlapping_leave(
            self.employee,
            datetime.date(2025, 3, 3),
            datetime.date(2025, 3, 7),
            exclude_id=req.pk,
        )

    def test_different_employee_does_not_block(self):
        other = make_user("other@test.com")
        make_request(
            other, self.leave_type,
            datetime.date(2025, 3, 3), datetime.date(2025, 3, 7),
        )
        # Same dates for a different employee — should pass
        WorkingDaysService.check_overlapping_leave(
            self.employee,
            datetime.date(2025, 3, 3),
            datetime.date(2025, 3, 7),
        )

    # --- failing cases ---

    def test_exact_same_range_raises(self):
        self._make_active(datetime.date(2025, 3, 3), datetime.date(2025, 3, 7))
        with self.assertRaises(ValidationError) as ctx:
            WorkingDaysService.check_overlapping_leave(
                self.employee,
                datetime.date(2025, 3, 3),
                datetime.date(2025, 3, 7),
            )
        self.assertIn("overlapping", str(ctx.exception.detail))

    def test_partial_overlap_at_start_raises(self):
        # Existing ends on Wednesday; new starts on Monday → overlap Mon–Wed
        self._make_active(datetime.date(2025, 3, 3), datetime.date(2025, 3, 5))
        with self.assertRaises(ValidationError):
            WorkingDaysService.check_overlapping_leave(
                self.employee,
                datetime.date(2025, 3, 4),
                datetime.date(2025, 3, 7),
            )

    def test_partial_overlap_at_end_raises(self):
        self._make_active(datetime.date(2025, 3, 5), datetime.date(2025, 3, 7))
        with self.assertRaises(ValidationError):
            WorkingDaysService.check_overlapping_leave(
                self.employee,
                datetime.date(2025, 3, 3),
                datetime.date(2025, 3, 6),
            )

    def test_new_range_inside_existing_raises(self):
        self._make_active(datetime.date(2025, 3, 3), datetime.date(2025, 3, 14))
        with self.assertRaises(ValidationError):
            WorkingDaysService.check_overlapping_leave(
                self.employee,
                datetime.date(2025, 3, 5),
                datetime.date(2025, 3, 7),
            )

    def test_new_range_contains_existing_raises(self):
        self._make_active(datetime.date(2025, 3, 5), datetime.date(2025, 3, 7))
        with self.assertRaises(ValidationError):
            WorkingDaysService.check_overlapping_leave(
                self.employee,
                datetime.date(2025, 3, 3),
                datetime.date(2025, 3, 14),
            )

    def test_single_day_overlap_raises(self):
        # Existing is single-day Wednesday; new range spans the whole week
        self._make_active(datetime.date(2025, 3, 5), datetime.date(2025, 3, 5))
        with self.assertRaises(ValidationError):
            WorkingDaysService.check_overlapping_leave(
                self.employee,
                datetime.date(2025, 3, 3),
                datetime.date(2025, 3, 7),
            )
