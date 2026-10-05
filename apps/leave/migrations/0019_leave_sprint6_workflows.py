# Sprint 6: workflow templates, stages, approver delegates.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
import uuid


SENIOR = [
    "TEAM_LEAD",
    "SUPERVISOR",
    "LINE_MANAGER",
    "HR",
    "EXECUTIVE_DIRECTOR",
    "MANAGING_DIRECTOR",
]
MD_ED = ["EXECUTIVE_DIRECTOR", "MANAGING_DIRECTOR"]


def seed_default_workflow(apps, schema_editor):
    LeaveWorkflowTemplate = apps.get_model("leave", "LeaveWorkflowTemplate")
    LeaveWorkflowStage = apps.get_model("leave", "LeaveWorkflowStage")
    template, _ = LeaveWorkflowTemplate.objects.get_or_create(
        name="Standard approval chain",
        defaults={
            "is_active": True,
            "is_org_default": True,
            "reject_comment_required": True,
            "approve_comment_required": False,
            "auto_approve_after_sla": False,
        },
    )
    if not template.is_org_default:
        template.is_org_default = True
        template.save(update_fields=["is_org_default", "updated_at"])
    if LeaveWorkflowStage.objects.filter(template=template).exists():
        return
    specs = [
        (1, "TEAM_LEAD", "PENDING_TEAM_LEAD", True, SENIOR, False),
        (2, "SUPERVISOR", "PENDING_SUPERVISOR", True, SENIOR, False),
        (3, "LINE_MANAGER", "PENDING_MANAGER", False, MD_ED, True),
        (4, "HR", "PENDING_HR", False, ["HR", *MD_ED], False),
        (5, "EXECUTIVE_DIRECTOR", "PENDING_ED", False, MD_ED, False),
    ]
    for order, source, status_code, skip_unresolved, skip_roles, use_mgmt in specs:
        LeaveWorkflowStage.objects.create(
            template=template,
            order=order,
            approver_source=source,
            status_code=status_code,
            skip_if_unresolved=skip_unresolved,
            is_optional=False,
            skip_if_requester_roles=skip_roles,
            use_management_line_manager_for_line_manager_requester=use_mgmt,
        )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("leave", "0018_leave_sprint5_settings_calendars"),
    ]

    operations = [
        migrations.AddField(
            model_name="leavesettings",
            name="approval_sla_hours",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Optional org-wide pending-stage SLA in hours. Null = use stage sla_hours only.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="leaverequest",
            name="workflow_snapshot",
            field=models.JSONField(
                blank=True,
                help_text="Workflow template + stages captured at submit. Approvals use this, not live template edits.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="leaverequest",
            name="stage_entered_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When the request entered the current pending approval status.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="leaverequest",
            name="sla_notified_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When an SLA reminder/escalation was last sent for the current stage.",
                null=True,
            ),
        ),
        migrations.CreateModel(
            name="LeaveWorkflowTemplate",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=200, unique=True)),
                ("is_active", models.BooleanField(default=True)),
                ("is_org_default", models.BooleanField(default=False)),
                (
                    "mode",
                    models.CharField(
                        choices=[("SEQUENTIAL", "Sequential")],
                        default="SEQUENTIAL",
                        max_length=16,
                    ),
                ),
                ("reject_comment_required", models.BooleanField(default=True)),
                ("approve_comment_required", models.BooleanField(default=False)),
                (
                    "sla_hours",
                    models.PositiveIntegerField(
                        blank=True,
                        help_text="Fallback SLA hours for stages that do not set sla_hours.",
                        null=True,
                    ),
                ),
                (
                    "auto_approve_after_sla",
                    models.BooleanField(
                        default=False,
                        help_text="If true, Beat may auto-approve after SLA. Default off.",
                    ),
                ),
                (
                    "leave_type",
                    models.ForeignKey(
                        blank=True,
                        help_text="When set, this template is preferred for that leave type over the org default.",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="workflow_templates",
                        to="leave.leavetype",
                    ),
                ),
            ],
            options={
                "verbose_name": "Leave Workflow Template",
                "verbose_name_plural": "Leave Workflow Templates",
                "ordering": ["name"],
            },
        ),
        migrations.CreateModel(
            name="LeaveWorkflowStage",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("order", models.PositiveIntegerField()),
                (
                    "approver_source",
                    models.CharField(
                        choices=[
                            ("TEAM_LEAD", "Team lead"),
                            ("SUPERVISOR", "Supervisor"),
                            ("LINE_MANAGER", "Line manager"),
                            ("HR", "HR"),
                            ("EXECUTIVE_DIRECTOR", "Executive director"),
                            ("NAMED_USER", "Named user"),
                            ("ROLE", "Role"),
                        ],
                        max_length=32,
                    ),
                ),
                (
                    "status_code",
                    models.CharField(
                        choices=[
                            ("DRAFT", "Draft"),
                            ("PENDING_TEAM_LEAD", "Pending Team Lead"),
                            ("PENDING_SUPERVISOR", "Pending Supervisor"),
                            ("PENDING_MANAGER", "Pending Manager"),
                            ("PENDING_HR", "Pending HR"),
                            ("PENDING_ED", "Pending Executive Director"),
                            ("APPROVED", "Approved"),
                            ("REJECTED", "Rejected"),
                            ("CANCELLED", "Cancelled"),
                        ],
                        help_text="API status used while this stage is pending (keeps PENDING_* compatibility).",
                        max_length=20,
                    ),
                ),
                ("role_name", models.CharField(blank=True, help_text="Required when approver_source is ROLE.", max_length=32)),
                ("sla_hours", models.PositiveIntegerField(blank=True, null=True)),
                (
                    "skip_if_unresolved",
                    models.BooleanField(
                        default=False,
                        help_text="Skip leading stages that cannot resolve an approver (prefix skip only).",
                    ),
                ),
                (
                    "is_optional",
                    models.BooleanField(
                        default=False,
                        help_text="If true, drop this stage when no approver can be resolved (including mid-chain).",
                    ),
                ),
                (
                    "skip_if_requester_roles",
                    models.JSONField(
                        blank=True,
                        default=list,
                        help_text="Role names; if the requester has any, this stage is omitted from the snapshot.",
                    ),
                ),
                (
                    "use_management_line_manager_for_line_manager_requester",
                    models.BooleanField(
                        default=False,
                        help_text="When requester is a LINE_MANAGER, resolve approver from the Management department.",
                    ),
                ),
                (
                    "named_user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "template",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="stages",
                        to="leave.leaveworkflowtemplate",
                    ),
                ),
            ],
            options={
                "verbose_name": "Leave Workflow Stage",
                "verbose_name_plural": "Leave Workflow Stages",
                "ordering": ["template", "order"],
                "unique_together": {("template", "order")},
            },
        ),
        migrations.CreateModel(
            name="ApproverDelegate",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("start_date", models.DateField()),
                ("end_date", models.DateField()),
                ("is_active", models.BooleanField(default=True)),
                (
                    "delegate",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="approver_delegations_received",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        help_text="Primary approver being covered.",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="approver_delegations_given",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Approver Delegate",
                "verbose_name_plural": "Approver Delegates",
                "ordering": ["-start_date"],
            },
        ),
        migrations.AddIndex(
            model_name="approverdelegate",
            index=models.Index(fields=["user", "delegate", "is_active"], name="leave_appro_user_id_idx"),
        ),
        migrations.AddConstraint(
            model_name="leaveworkflowtemplate",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_active", True), ("leave_type__isnull", False)),
                fields=("leave_type",),
                name="unique_active_workflow_per_leave_type",
            ),
        ),
        migrations.RunPython(seed_default_workflow, noop_reverse),
    ]
