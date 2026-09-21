import re
import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
from django.utils import timezone


KNOWN_CODES = {
    "Annual": "ANNUAL",
    "Sick": "SICK",
    "Casual": "CASUAL",
    "Maternity": "MATERNITY",
    "Maternity Leave": "MATERNITY",
    "Paternity": "PATERNITY",
    "Paternity Leave": "PATERNITY",
}

DISPLAY_ORDER = {
    "ANNUAL": 10,
    "SICK": 20,
    "CASUAL": 30,
    "MATERNITY": 40,
    "PATERNITY": 50,
}


def _slug_code(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", (name or "").strip()).strip("_").upper()
    return (slug or "LEAVE_TYPE")[:32]


def backfill_leave_type_codes(apps, schema_editor):
    LeaveType = apps.get_model("leave", "LeaveType")
    used = set(LeaveType.objects.exclude(code="").values_list("code", flat=True))
    for leave_type in LeaveType.objects.all():
        code = KNOWN_CODES.get(leave_type.name) or _slug_code(leave_type.name)
        base = code
        suffix = 2
        while code in used:
            code = f"{base[:28]}_{suffix}"
            suffix += 1
        leave_type.code = code
        leave_type.display_order = DISPLAY_ORDER.get(code, 100)
        leave_type.is_active = True
        leave_type.save(update_fields=["code", "display_order", "is_active"])
        used.add(code)


def seed_leave_policies(apps, schema_editor):
    LeaveType = apps.get_model("leave", "LeaveType")
    LeavePolicy = apps.get_model("leave", "LeavePolicy")
    today = timezone.localdate()
    for leave_type in LeaveType.objects.all():
        if LeavePolicy.objects.filter(leave_type=leave_type, status="ACTIVE").exists():
            continue
        if LeavePolicy.objects.filter(leave_type=leave_type).exists():
            # Promote the newest existing policy rather than duplicating.
            policy = (
                LeavePolicy.objects.filter(leave_type=leave_type)
                .order_by("-created_at")
                .first()
            )
            policy.status = "ACTIVE"
            policy.version = max(policy.version or 0, 1)
            policy.name = policy.name or f"{leave_type.name} Policy"
            if not policy.effective_from:
                policy.effective_from = today
            policy.save()
            continue
        LeavePolicy.objects.create(
            leave_type=leave_type,
            name=f"{leave_type.name} Policy",
            status="ACTIVE",
            version=1,
            effective_from=today,
            annual_entitlement=leave_type.default_days,
            carry_forward=False,
            half_day_allowed=False,
            weekend_excluded=True,
            public_holiday_excluded=True,
            forfeited_on_resignation=True,
            allow_backdated=True,
        )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    # Postgres cannot CREATE INDEX on a table in the same transaction as
    # UPDATEs from AddField defaults / RunPython (pending trigger events).
    atomic = False

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("leave", "0013_reconciliation_hardening"),
    ]

    operations = [
        migrations.AddField(
            model_name="leavetype",
            name="calendar_color",
            field=models.CharField(blank=True, max_length=16),
        ),
        migrations.AddField(
            model_name="leavetype",
            name="code",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="leavetype",
            name="display_order",
            field=models.PositiveIntegerField(default=100),
        ),
        migrations.AddField(
            model_name="leavetype",
            name="is_active",
            field=models.BooleanField(default=True),
        ),
        migrations.RunPython(backfill_leave_type_codes, noop_reverse),
        migrations.AlterField(
            model_name="leavetype",
            name="code",
            field=models.CharField(
                help_text="Stable machine identifier. Immutable after leave requests exist.",
                max_length=32,
                unique=True,
            ),
        ),
        migrations.AlterField(
            model_name="leavetype",
            name="default_days",
            field=models.PositiveIntegerField(
                help_text="Fallback entitlement when no active LeavePolicy exists."
            ),
        ),
        migrations.AlterModelOptions(
            name="leavetype",
            options={
                "ordering": ["display_order", "name"],
                "verbose_name": "Leave Type",
                "verbose_name_plural": "Leave Types",
            },
        ),
        migrations.AddField(
            model_name="leavepolicy",
            name="effective_from",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="leavepolicy",
            name="effective_to",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="leavepolicy",
            name="name",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name="leavepolicy",
            name="status",
            field=models.CharField(
                choices=[
                    ("DRAFT", "Draft"),
                    ("ACTIVE", "Active"),
                    ("ARCHIVED", "Archived"),
                ],
                default="DRAFT",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="leavepolicy",
            name="version",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AlterField(
            model_name="leavepolicy",
            name="leave_type",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="policies",
                to="leave.leavetype",
            ),
        ),
        migrations.AlterModelOptions(
            name="leavepolicy",
            options={
                "ordering": ["leave_type__display_order", "leave_type__name", "-version"],
                "verbose_name": "Leave Policy",
                "verbose_name_plural": "Leave Policies",
            },
        ),
        migrations.AddIndex(
            model_name="leavepolicy",
            index=models.Index(
                fields=["leave_type", "status", "-version"],
                name="leave_leave_leave_t_status_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="leavepolicy",
            constraint=models.UniqueConstraint(
                condition=models.Q(status="ACTIVE"),
                fields=("leave_type",),
                name="unique_active_policy_per_leave_type",
            ),
        ),
        migrations.CreateModel(
            name="LeaveSettingsAuditLog",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("object_type", models.CharField(max_length=64)),
                ("object_id", models.UUIDField()),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("CREATE", "Create"),
                            ("UPDATE", "Update"),
                            ("DELETE", "Delete"),
                            ("PUBLISH", "Publish"),
                            ("ARCHIVE", "Archive"),
                            ("ACTIVATE", "Activate"),
                            ("DEACTIVATE", "Deactivate"),
                            ("CLONE", "Clone"),
                        ],
                        max_length=16,
                    ),
                ),
                ("previous_values", models.JSONField(blank=True, null=True)),
                ("new_values", models.JSONField(blank=True, null=True)),
                ("reason", models.TextField(blank=True)),
                (
                    "ip_address",
                    models.GenericIPAddressField(blank=True, null=True),
                ),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="leave_settings_audit_logs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Leave Settings Audit Log",
                "verbose_name_plural": "Leave Settings Audit Logs",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="leavesettingsauditlog",
            index=models.Index(
                fields=["object_type", "object_id", "-created_at"],
                name="leave_leave_object__audit_idx",
            ),
        ),
        migrations.RunPython(seed_leave_policies, noop_reverse),
    ]
