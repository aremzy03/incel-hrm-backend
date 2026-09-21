# Generated manually for Sprint 5 leave settings and calendars.

from decimal import Decimal

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
import uuid


def seed_default_calendars_and_settings(apps, schema_editor):
    WorkingCalendar = apps.get_model("leave", "WorkingCalendar")
    HolidayCalendar = apps.get_model("leave", "HolidayCalendar")
    CalendarHoliday = apps.get_model("leave", "CalendarHoliday")
    PublicHoliday = apps.get_model("leave", "PublicHoliday")
    LeaveSettings = apps.get_model("leave", "LeaveSettings")

    working, _ = WorkingCalendar.objects.get_or_create(
        is_org_default=True,
        defaults={
            "name": "Standard weekdays (Mon–Fri)",
            "is_active": True,
            "timezone": "Africa/Lagos",
            "weekdays": [0, 1, 2, 3, 4],
            "hours_per_day": Decimal("8.00"),
        },
    )
    if not working.weekdays:
        working.weekdays = [0, 1, 2, 3, 4]
        working.save(update_fields=["weekdays"])

    holiday, _ = HolidayCalendar.objects.get_or_create(
        is_org_default=True,
        defaults={
            "name": "Organization public holidays",
            "is_active": True,
            "timezone": "Africa/Lagos",
        },
    )
    for ph in PublicHoliday.objects.all():
        CalendarHoliday.objects.get_or_create(
            calendar=holiday,
            date=ph.date,
            defaults={
                "name": ph.name,
                "is_recurring": ph.is_recurring,
                "is_full_day": True,
            },
        )

    LeaveSettings.objects.get_or_create(
        singleton_key="default",
        defaults={
            "leave_year_type": "CALENDAR",
            "leave_year_start_month": 1,
            "leave_year_start_day": 1,
            "cross_year_deduction_rule": "SPLIT",
            "default_timezone": "Africa/Lagos",
            "default_working_calendar": working,
            "default_holiday_calendar": holiday,
            "notify_applicant_on_submit": True,
            "notify_applicant_on_decision": True,
            "notify_approver": True,
            "notify_reliever": True,
            "notify_department_reminder": True,
            "reminder_lead_hours": 24,
            "allow_hr_override": True,
            "prevent_self_approval": False,
        },
    )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("accounts", "0015_user_tutorial_progress"),
        ("leave", "0017_leave_sprint4_accrual"),
    ]

    operations = [
        migrations.CreateModel(
            name="WorkingCalendar",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=150)),
                ("is_active", models.BooleanField(default=True)),
                ("is_org_default", models.BooleanField(default=False)),
                ("timezone", models.CharField(default="Africa/Lagos", max_length=64)),
                (
                    "weekdays",
                    models.JSONField(
                        default=list,
                        help_text="Python weekday numbers that count as working days (Monday=0 … Sunday=6).",
                    ),
                ),
                (
                    "hours_per_day",
                    models.DecimalField(decimal_places=2, default=Decimal("8.00"), max_digits=4),
                ),
                ("effective_from", models.DateField(blank=True, null=True)),
                ("effective_to", models.DateField(blank=True, null=True)),
            ],
            options={
                "verbose_name": "Working Calendar",
                "verbose_name_plural": "Working Calendars",
                "ordering": ["name"],
            },
        ),
        migrations.CreateModel(
            name="HolidayCalendar",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=150)),
                ("is_active", models.BooleanField(default=True)),
                ("is_org_default", models.BooleanField(default=False)),
                ("timezone", models.CharField(default="Africa/Lagos", max_length=64)),
                ("effective_from", models.DateField(blank=True, null=True)),
                ("effective_to", models.DateField(blank=True, null=True)),
            ],
            options={
                "verbose_name": "Holiday Calendar",
                "verbose_name_plural": "Holiday Calendars",
                "ordering": ["name"],
            },
        ),
        migrations.CreateModel(
            name="CalendarHoliday",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=150)),
                ("date", models.DateField()),
                ("is_recurring", models.BooleanField(default=False)),
                ("is_full_day", models.BooleanField(default=True)),
                ("observed_date", models.DateField(blank=True, null=True)),
                (
                    "location_scope",
                    models.CharField(
                        blank=True,
                        help_text="Optional country/state/location label. Empty = whole calendar.",
                        max_length=150,
                    ),
                ),
                (
                    "calendar",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="holidays",
                        to="leave.holidaycalendar",
                    ),
                ),
            ],
            options={
                "verbose_name": "Calendar Holiday",
                "verbose_name_plural": "Calendar Holidays",
                "ordering": ["date"],
                "unique_together": {("calendar", "date")},
            },
        ),
        migrations.CreateModel(
            name="CalendarAssignment",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("is_active", models.BooleanField(default=True)),
                ("effective_from", models.DateField(blank=True, null=True)),
                ("effective_to", models.DateField(blank=True, null=True)),
                (
                    "department",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="leave_calendar_assignments",
                        to="accounts.department",
                    ),
                ),
                (
                    "employee",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="leave_calendar_assignments",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "holiday_calendar",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="assignments",
                        to="leave.holidaycalendar",
                    ),
                ),
                (
                    "working_calendar",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="assignments",
                        to="leave.workingcalendar",
                    ),
                ),
            ],
            options={
                "verbose_name": "Calendar Assignment",
                "verbose_name_plural": "Calendar Assignments",
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="LeaveSettings",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("singleton_key", models.CharField(default="default", max_length=16, unique=True)),
                (
                    "leave_year_type",
                    models.CharField(
                        choices=[
                            ("CALENDAR", "Calendar year (1 January)"),
                            ("FISCAL", "Fiscal year (configured start date)"),
                            ("ANNIVERSARY", "Employment anniversary (org jobs still use calendar 1 Jan)"),
                        ],
                        default="CALENDAR",
                        max_length=16,
                    ),
                ),
                ("leave_year_start_month", models.PositiveSmallIntegerField(default=1)),
                ("leave_year_start_day", models.PositiveSmallIntegerField(default=1)),
                (
                    "cross_year_deduction_rule",
                    models.CharField(
                        choices=[
                            ("SPLIT", "Split working days by calendar year (existing reconcile behaviour)"),
                            ("START_YEAR", "Deduct all days from the start date's calendar year"),
                        ],
                        default="SPLIT",
                        help_text="SPLIT reuses split_working_days_by_year(); START_YEAR charges the start year only.",
                        max_length=16,
                    ),
                ),
                ("default_timezone", models.CharField(default="Africa/Lagos", max_length=64)),
                ("notify_applicant_on_submit", models.BooleanField(default=True)),
                ("notify_applicant_on_decision", models.BooleanField(default=True)),
                ("notify_approver", models.BooleanField(default=True)),
                ("notify_reliever", models.BooleanField(default=True)),
                ("notify_department_reminder", models.BooleanField(default=True)),
                (
                    "reminder_lead_hours",
                    models.PositiveIntegerField(
                        default=24,
                        help_text="Hours before leave start for department reminders (historically 24).",
                    ),
                ),
                (
                    "allow_hr_override",
                    models.BooleanField(
                        default=True,
                        help_text="When true, HR may bypass reliever-scope checks (existing behaviour).",
                    ),
                ),
                (
                    "prevent_self_approval",
                    models.BooleanField(
                        default=False,
                        help_text="When true, the requester cannot approve their own request. Default off to preserve current routing.",
                    ),
                ),
                (
                    "default_holiday_calendar",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="leave.holidaycalendar",
                    ),
                ),
                (
                    "default_working_calendar",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="leave.workingcalendar",
                    ),
                ),
            ],
            options={
                "verbose_name": "Leave Settings",
                "verbose_name_plural": "Leave Settings",
            },
        ),
        migrations.AddField(
            model_name="leaverequest",
            name="calculation_snapshot",
            field=models.JSONField(
                blank=True,
                help_text="Working-day inputs used when total_working_days was last computed. Not rewritten on calendar edits.",
                null=True,
            ),
        ),
        migrations.RunPython(seed_default_calendars_and_settings, noop_reverse),
    ]
