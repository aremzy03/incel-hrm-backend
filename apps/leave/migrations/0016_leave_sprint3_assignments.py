import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("leave", "0015_leave_sprint2_enforcement"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="leavepolicy",
            name="unique_active_policy_per_leave_type",
        ),
        migrations.AddField(
            model_name="leaverequest",
            name="policy",
            field=models.ForeignKey(
                blank=True,
                help_text="Policy snapshot taken at submit (or last date-range recalc while draft).",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="leave_requests",
                to="leave.leavepolicy",
            ),
        ),
        migrations.AddField(
            model_name="leaverequest",
            name="policy_version",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="LeavePolicyAssignment",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "scope_type",
                    models.CharField(
                        choices=[
                            ("ORGANIZATION", "Organization"),
                            ("DEPARTMENT", "Department"),
                            ("UNIT", "Unit"),
                            ("TEAM", "Team"),
                            ("EMPLOYMENT_TYPE", "Employment type"),
                            ("EMPLOYEE", "Employee"),
                        ],
                        max_length=32,
                    ),
                ),
                (
                    "scope_id",
                    models.CharField(
                        blank=True,
                        help_text="Department/unit/team UUID, or employment-type code. Empty for organization.",
                        max_length=64,
                    ),
                ),
                (
                    "priority",
                    models.IntegerField(
                        default=0,
                        help_text="Higher wins when two assignments share the same specificity.",
                    ),
                ),
                ("effective_from", models.DateField()),
                ("effective_to", models.DateField(blank=True, null=True)),
                ("is_active", models.BooleanField(default=True)),
                (
                    "employee",
                    models.ForeignKey(
                        blank=True,
                        help_text="Required when scope_type is EMPLOYEE.",
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="leave_policy_assignments",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "policy",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="assignments",
                        to="leave.leavepolicy",
                    ),
                ),
            ],
            options={
                "verbose_name": "Leave Policy Assignment",
                "verbose_name_plural": "Leave Policy Assignments",
                "ordering": ["-priority", "-effective_from"],
            },
        ),
        migrations.AddIndex(
            model_name="leavepolicyassignment",
            index=models.Index(
                fields=["scope_type", "scope_id", "is_active"],
                name="leave_leave_scope_t_8a1c21_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="leavepolicyassignment",
            index=models.Index(
                fields=["policy", "is_active"],
                name="leave_leave_policy__b2e4f0_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="leavepolicyassignment",
            index=models.Index(
                fields=["employee", "is_active"],
                name="leave_leave_employe_c3d901_idx",
            ),
        ),
    ]
