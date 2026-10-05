# Sprint 7: balance adjust ledger date, blackouts, encashment settings.

import django.db.models.deletion
from django.db import migrations, models
import uuid


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("accounts", "0015_user_tutorial_progress"),
        ("leave", "0019_leave_sprint6_workflows"),
    ]

    operations = [
        migrations.AddField(
            model_name="leavesettings",
            name="encashment_allowed",
            field=models.BooleanField(
                default=False,
                help_text="When True, unused days that are not forfeited on termination are recorded as ENCASH for payroll.",
            ),
        ),
        migrations.AddField(
            model_name="leavesettings",
            name="encashment_max_days",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Cap on days encashed per balance year. Null = no cap.",
                max_digits=8,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="leavebalancetransaction",
            name="effective_date",
            field=models.DateField(
                blank=True,
                help_text="Optional HR-stated effective date for adjustments / settlements.",
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="leavebalancetransaction",
            name="transaction_type",
            field=models.CharField(
                choices=[
                    ("DEDUCT", "Deduct"),
                    ("REFUND", "Refund"),
                    ("ADJUST", "Adjust"),
                    ("RESERVE", "Reserve"),
                    ("RELEASE", "Release"),
                    ("ACCRUAL", "Accrual"),
                    ("CARRY_FORWARD", "Carry-forward"),
                    ("EXPIRY", "Expiry"),
                    ("FORFEIT", "Forfeit"),
                    ("ENCASH", "Encashment"),
                ],
                max_length=16,
            ),
        ),
        migrations.CreateModel(
            name="LeaveBlackoutPeriod",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("name", models.CharField(max_length=200)),
                ("start_date", models.DateField()),
                ("end_date", models.DateField()),
                (
                    "enforcement",
                    models.CharField(
                        choices=[("BLOCK", "Hard block"), ("WARN", "Warning only")],
                        default="BLOCK",
                        max_length=8,
                    ),
                ),
                ("is_active", models.BooleanField(default=True)),
                (
                    "department",
                    models.ForeignKey(
                        blank=True,
                        help_text="Null = organization-wide.",
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="leave_blackout_periods",
                        to="accounts.department",
                    ),
                ),
                (
                    "leave_types",
                    models.ManyToManyField(
                        blank=True,
                        help_text="Empty means all leave types.",
                        related_name="blackout_periods",
                        to="leave.leavetype",
                    ),
                ),
            ],
            options={
                "verbose_name": "Leave Blackout Period",
                "verbose_name_plural": "Leave Blackout Periods",
                "ordering": ["-start_date"],
            },
        ),
    ]
