import datetime

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Department, Role, RoleName, UserRole
from apps.leave.models import (
    CalendarHoliday,
    CrossYearDeductionRule,
    HolidayCalendar,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveSettings,
    LeaveSettingsAuditLog,
    LeaveType,
    LeaveYearType,
    PublicHoliday,
    SettingsAuditAction,
    WorkingCalendar,
)
from apps.leave.services import (
    WorkingDaysService,
    get_leave_settings,
    leave_year_for_date,
    leave_year_start_date,
    split_working_days_by_year,
)
from apps.leave.tasks import run_leave_year_rollover
from apps.leave.utils import calculate_working_days
from django.contrib.auth import get_user_model

User = get_user_model()


class LeaveSprint5SettingsCalendarsTests(APITestCase):
    def setUp(self):
        self.password = "testpass123"
        self.department = Department.objects.create(name="Engineering-S5")
        self.hr_department, _ = Department.objects.get_or_create(name="Human Resources (HR)")
        self.employee = self._create_user("emp-s5@test.com", [RoleName.EMPLOYEE], self.department)
        self.hr_user = self._create_user("hr-s5@test.com", [RoleName.HR], self.hr_department)
        self.annual = LeaveType.objects.get(code=LeaveType.Code.ANNUAL)
        self.settings_url = reverse("leave-settings")
        self.working_url = reverse("working-calendar-list")
        self.holiday_url = reverse("holiday-calendar-list")

    def _create_user(self, email, roles, department=None):
        user = User.objects.create_user(email=email, password=self.password, department=department)
        for role_name in roles:
            role, _ = Role.objects.get_or_create(name=role_name)
            UserRole.objects.get_or_create(user=user, role=role)
        return user

    def test_default_calendar_matches_mon_fri_public_holiday(self):
        start = datetime.date(2026, 7, 6)  # Mon
        end = datetime.date(2026, 7, 10)  # Fri
        PublicHoliday.objects.get_or_create(
            date=datetime.date(2026, 7, 8),
            defaults={"name": "S5 Holiday", "is_recurring": False},
        )
        legacy = calculate_working_days(start, end)
        via_service = WorkingDaysService.calculate_working_days(
            start, end, leave_type=self.annual, employee=self.employee
        )
        self.assertEqual(legacy, 4)
        self.assertEqual(via_service, 4)

    def test_settings_permissions(self):
        resp = self.client.get(self.settings_url)
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

        self.client.force_authenticate(self.employee)
        get_resp = self.client.get(self.settings_url)
        self.assertEqual(get_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(get_resp.data["leave_year_type"], LeaveYearType.CALENDAR)
        patch_resp = self.client.patch(
            self.settings_url,
            {"notify_approver": False},
            format="json",
        )
        self.assertEqual(patch_resp.status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(self.hr_user)
        hr_patch = self.client.patch(
            self.settings_url,
            {
                "leave_year_type": LeaveYearType.FISCAL,
                "leave_year_start_month": 4,
                "leave_year_start_day": 1,
                "reason": "Fiscal year April",
            },
            format="json",
        )
        self.assertEqual(hr_patch.status_code, status.HTTP_200_OK)
        self.assertEqual(hr_patch.data["leave_year_type"], LeaveYearType.FISCAL)
        self.assertEqual(hr_patch.data["leave_year_start_month"], 4)
        self.assertTrue(
            LeaveSettingsAuditLog.objects.filter(
                object_type="LeaveSettings", action=SettingsAuditAction.UPDATE
            ).exists()
        )
        settings_row = get_leave_settings()
        self.assertEqual(leave_year_start_date(2026, settings_row), datetime.date(2026, 4, 1))
        self.assertEqual(leave_year_for_date(datetime.date(2026, 3, 31), settings_row), 2025)
        self.assertEqual(leave_year_for_date(datetime.date(2026, 4, 1), settings_row), 2026)

    def test_employee_cannot_create_working_calendar(self):
        self.client.force_authenticate(self.employee)
        resp = self.client.post(
            self.working_url,
            {"name": "Sat crew", "weekdays": [0, 1, 2, 3, 4, 5]},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_assigned_holiday_calendar_affects_new_working_day_calc(self):
        self.client.force_authenticate(self.hr_user)
        cal_resp = self.client.post(
            self.holiday_url,
            {
                "name": "Site A holidays",
                "is_org_default": False,
                "holidays": [
                    {
                        "name": "Site shutdown",
                        "date": "2026-07-07",
                        "is_recurring": False,
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(cal_resp.status_code, status.HTTP_201_CREATED)
        holiday_cal_id = cal_resp.data["id"]
        assign_url = reverse("leave-calendar-assignment-list")
        assign_resp = self.client.post(
            assign_url,
            {
                "holiday_calendar": holiday_cal_id,
                "employee": str(self.employee.pk),
            },
            format="json",
        )
        self.assertEqual(assign_resp.status_code, status.HTTP_201_CREATED)

        start = datetime.date(2026, 7, 6)
        end = datetime.date(2026, 7, 10)
        other = self._create_user("other-s5@test.com", [RoleName.EMPLOYEE], self.department)
        default_days = WorkingDaysService.calculate_working_days(
            start, end, leave_type=self.annual, employee=other
        )
        assigned_days = WorkingDaysService.calculate_working_days(
            start, end, leave_type=self.annual, employee=self.employee
        )
        self.assertEqual(default_days, 5)
        self.assertEqual(assigned_days, 4)

        req = LeaveRequest.objects.create(
            employee=self.employee,
            leave_type=self.annual,
            start_date=start,
            end_date=end,
            status=LeaveRequestStatus.DRAFT,
        )
        self.assertEqual(req.total_working_days, 4)
        self.assertIsNotNone(req.calculation_snapshot)
        stored = req.total_working_days
        CalendarHoliday.objects.filter(calendar_id=holiday_cal_id).delete()
        req.status = LeaveRequestStatus.PENDING_MANAGER
        req.save(update_fields=["status", "updated_at"])
        req.refresh_from_db()
        self.assertEqual(req.total_working_days, stored)

    def test_public_holidays_migrated_into_default_holiday_calendar(self):
        settings_row = get_leave_settings()
        self.assertIsNotNone(settings_row.default_holiday_calendar_id)
        holiday_cal = HolidayCalendar.objects.get(is_org_default=True)
        public_count = PublicHoliday.objects.count()
        copied = CalendarHoliday.objects.filter(calendar=holiday_cal).count()
        self.assertGreaterEqual(copied, public_count)
        self.assertTrue(WorkingCalendar.objects.filter(is_org_default=True).exists())

    def test_cross_year_split_default_and_start_year_option(self):
        start = datetime.date(2026, 12, 31)
        end = datetime.date(2027, 1, 4)
        splits = split_working_days_by_year(start, end, leave_type=self.annual, employee=self.employee)
        self.assertIn(2026, splits)
        self.assertIn(2027, splits)
        LeaveSettings.objects.filter(singleton_key="default").update(
            cross_year_deduction_rule=CrossYearDeductionRule.START_YEAR
        )
        start_year = split_working_days_by_year(
            start, end, leave_type=self.annual, employee=self.employee
        )
        self.assertEqual(list(start_year.keys()), [2026])
        self.assertEqual(start_year[2026], splits[2026] + splits[2027])

    def test_rollover_without_year_skips_when_not_leave_year_start(self):
        LeaveSettings.objects.filter(singleton_key="default").update(
            leave_year_type=LeaveYearType.FISCAL,
            leave_year_start_month=4,
            leave_year_start_day=1,
        )
        today = datetime.date.today()
        if (today.month, today.day) != (4, 1):
            result = run_leave_year_rollover(year=None, dry_run=True)
            self.assertTrue(result.get("skipped"))
            self.assertEqual(result.get("reason"), "not_leave_year_start")
        explicit = run_leave_year_rollover(year=2027, dry_run=True)
        self.assertFalse(explicit.get("skipped"))
        self.assertIn("actions", explicit)
