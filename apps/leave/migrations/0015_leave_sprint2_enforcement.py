from decimal import Decimal

from django.db import migrations, models
from django.db.models import F, Q


PENDING_HOLD_STATUSES = (
    "PENDING_TEAM_LEAD",
    "PENDING_SUPERVISOR",
    "PENDING_MANAGER",
    "PENDING_HR",
    "PENDING_ED",
)

STAFFING_CODES = ("ANNUAL", "CASUAL")
RELIEVER_EXEMPT_CODES = ("SICK", "MATERNITY", "PATERNITY")


def configure_policy_staffing(apps, schema_editor):
    LeavePolicy = apps.get_model("leave", "LeavePolicy")
    for policy in LeavePolicy.objects.select_related("leave_type").all():
        code = (policy.leave_type.code or "").upper()
        policy.overlap_control_enabled = code in STAFFING_CODES
        policy.reliever_required = code not in RELIEVER_EXEMPT_CODES
        policy.overlap_scope = "AUTO"
        policy.reliever_scope = "AUTO"
        policy.maximum_people_absent = 1
        policy.overlap_enforcement = "BLOCK"
        policy.save(
            update_fields=[
                "overlap_control_enabled",
                "reliever_required",
                "overlap_scope",
                "reliever_scope",
                "maximum_people_absent",
                "overlap_enforcement",
            ]
        )


def backfill_pending_holds(apps, schema_editor):
    LeaveRequest = apps.get_model("leave", "LeaveRequest")
    LeaveBalance = apps.get_model("leave", "LeaveBalance")
    LeaveBalanceTransaction = apps.get_model("leave", "LeaveBalanceTransaction")
    LeavePolicy = apps.get_model("leave", "LeavePolicy")

    def entitlement(leave_type):
        policy = (
            LeavePolicy.objects.filter(leave_type=leave_type, status="ACTIVE")
            .order_by("-version")
            .first()
        )
        if policy is not None:
            return policy.annual_entitlement
        return leave_type.default_days

    qs = LeaveRequest.objects.filter(status__in=PENDING_HOLD_STATUSES).select_related(
        "employee", "leave_type"
    )
    for req in qs:
        already = LeaveBalanceTransaction.objects.filter(
            leave_request=req, transaction_type="RESERVE"
        ).exists()
        if already:
            continue
        days = req.total_working_days or Decimal("0")
        if days <= 0:
            continue
        year = req.start_date.year
        balance, _ = LeaveBalance.objects.get_or_create(
            employee=req.employee,
            leave_type=req.leave_type,
            year=year,
            defaults={
                "allocated_days": entitlement(req.leave_type),
                "used_days": Decimal("0"),
                "pending_days": Decimal("0"),
            },
        )
        LeaveBalance.objects.filter(pk=balance.pk).update(
            pending_days=F("pending_days") + days
        )
        LeaveBalanceTransaction.objects.create(
            leave_balance=balance,
            leave_request=req,
            transaction_type="RESERVE",
            source="SUBMIT",
            delta_used_days=Decimal("0"),
            delta_pending_days=days,
            reason="Sprint 2 backfill: pending hold for in-flight request.",
        )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("leave", "0014_leave_settings_sprint1"),
    ]

    operations = [
        migrations.AddField(
            model_name="leavepolicy",
            name="maximum_people_absent",
            field=models.PositiveIntegerField(
                default=1,
                help_text="Maximum other people who may already be absent in the overlap scope.",
            ),
        ),
        migrations.AddField(
            model_name="leavepolicy",
            name="overlap_control_enabled",
            field=models.BooleanField(
                default=False,
                help_text="When True, cap concurrent absences in overlap_scope for this leave type.",
            ),
        ),
        migrations.AddField(
            model_name="leavepolicy",
            name="overlap_enforcement",
            field=models.CharField(
                choices=[("BLOCK", "Block"), ("WARN", "Warning only")],
                default="BLOCK",
                max_length=8,
            ),
        ),
        migrations.AddField(
            model_name="leavepolicy",
            name="overlap_scope",
            field=models.CharField(
                choices=[
                    ("AUTO", "Auto (lowest org level)"),
                    ("TEAM", "Team"),
                    ("UNIT", "Unit"),
                    ("DEPARTMENT", "Department"),
                    ("ORGANIZATION", "Organization"),
                ],
                default="AUTO",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="leavepolicy",
            name="reliever_required",
            field=models.BooleanField(
                default=True,
                help_text="When True, a cover person is required before submission (MD/ED and emergency still exempt).",
            ),
        ),
        migrations.AddField(
            model_name="leavepolicy",
            name="reliever_scope",
            field=models.CharField(
                choices=[
                    ("AUTO", "Auto (lowest org level)"),
                    ("TEAM", "Team"),
                    ("UNIT", "Unit"),
                    ("DEPARTMENT", "Department"),
                    ("ORGANIZATION", "Organization"),
                ],
                default="AUTO",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="leaverequest",
            name="half_day_period",
            field=models.CharField(
                blank=True,
                choices=[("AM", "Morning"), ("PM", "Afternoon")],
                max_length=2,
            ),
        ),
        migrations.AddField(
            model_name="leaverequest",
            name="is_half_day",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="leavebalance",
            name="pending_days",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                help_text="Days reserved by in-flight (submitted, not yet approved/rejected) requests.",
                max_digits=8,
            ),
        ),
        migrations.AddField(
            model_name="leavebalancetransaction",
            name="delta_pending_days",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                help_text="Change applied to pending_days (positive = reserve, negative = release/consume).",
                max_digits=8,
            ),
        ),
        migrations.AlterField(
            model_name="leavebalance",
            name="allocated_days",
            field=models.DecimalField(decimal_places=2, max_digits=8),
        ),
        migrations.AlterField(
            model_name="leavebalance",
            name="used_days",
            field=models.DecimalField(
                decimal_places=2, default=Decimal("0.00"), max_digits=8
            ),
        ),
        migrations.AlterField(
            model_name="leaverequest",
            name="total_working_days",
            field=models.DecimalField(
                decimal_places=2, default=Decimal("0.00"), max_digits=8
            ),
        ),
        migrations.AlterField(
            model_name="leavebalancetransaction",
            name="delta_used_days",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                help_text="Change applied to used_days (positive = deduct, negative = refund).",
                max_digits=8,
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
                ],
                max_length=10,
            ),
        ),
        migrations.AlterField(
            model_name="leavebalancetransaction",
            name="source",
            field=models.CharField(
                choices=[
                    ("APPROVAL", "Approval"),
                    ("RECONCILE", "Reconcile"),
                    ("CANCEL_REFUND", "Cancel refund"),
                    ("RECONCILE_EDIT", "Reconcile edit"),
                    ("HR_ADJUST", "HR adjust"),
                    ("SUBMIT", "Submit"),
                    ("REJECT_RELEASE", "Reject release"),
                    ("CANCEL_RELEASE", "Cancel release"),
                ],
                max_length=20,
            ),
        ),
        migrations.AddConstraint(
            model_name="leavebalancetransaction",
            constraint=models.UniqueConstraint(
                condition=Q(transaction_type="RESERVE"),
                fields=("leave_request", "leave_balance"),
                name="unique_reserve_per_leave_request_balance",
            ),
        ),
        migrations.AddConstraint(
            model_name="leavebalancetransaction",
            constraint=models.UniqueConstraint(
                condition=Q(transaction_type="RELEASE"),
                fields=("leave_request", "leave_balance"),
                name="unique_release_per_leave_request_balance",
            ),
        ),
        migrations.RunPython(configure_policy_staffing, noop),
        migrations.RunPython(backfill_pending_holds, noop),
    ]
