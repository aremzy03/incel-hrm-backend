# Reconciliation hardening: ledger, policy backdating fields

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("leave", "0012_leave_reconciliation"),
    ]

    operations = [
        migrations.AddField(
            model_name="leavepolicy",
            name="allow_backdated",
            field=models.BooleanField(
                default=True,
                help_text="When False, reconciled leave cannot start before today.",
            ),
        ),
        migrations.AddField(
            model_name="leavepolicy",
            name="maximum_backdate_days",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Max days in the past for reconciled leave start_date. Null = unlimited.",
                null=True,
            ),
        ),
        migrations.CreateModel(
            name="LeaveBalanceTransaction",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=__import__("uuid").uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "transaction_type",
                    models.CharField(
                        choices=[
                            ("DEDUCT", "Deduct"),
                            ("REFUND", "Refund"),
                            ("ADJUST", "Adjust"),
                        ],
                        max_length=10,
                    ),
                ),
                (
                    "source",
                    models.CharField(
                        choices=[
                            ("APPROVAL", "Approval"),
                            ("RECONCILE", "Reconcile"),
                            ("CANCEL_REFUND", "Cancel refund"),
                            ("RECONCILE_EDIT", "Reconcile edit"),
                            ("HR_ADJUST", "HR adjust"),
                        ],
                        max_length=20,
                    ),
                ),
                (
                    "delta_used_days",
                    models.IntegerField(
                        help_text="Change applied to used_days (positive = deduct, negative = refund)."
                    ),
                ),
                ("reason", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="leave_balance_transactions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "leave_balance",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="transactions",
                        to="leave.leavebalance",
                    ),
                ),
                (
                    "leave_request",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="balance_transactions",
                        to="leave.leaverequest",
                    ),
                ),
            ],
            options={
                "verbose_name": "Leave Balance Transaction",
                "verbose_name_plural": "Leave Balance Transactions",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="leavebalancetransaction",
            index=models.Index(
                fields=["leave_request", "transaction_type"],
                name="leave_leave_leave_r_8f0a1b_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="leavebalancetransaction",
            index=models.Index(
                fields=["leave_balance", "-created_at"],
                name="leave_leave_leave_b_2c4d3e_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="leavebalancetransaction",
            constraint=models.UniqueConstraint(
                condition=models.Q(("transaction_type", "REFUND")),
                fields=("leave_request",),
                name="unique_refund_per_leave_request",
            ),
        ),
    ]
