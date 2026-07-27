# Generated manually for leave reconciliation MVP

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("leave", "0011_leave_reminder_and_reliever_notifications"),
    ]

    operations = [
        migrations.AddField(
            model_name="leaverequest",
            name="is_reconciled",
            field=models.BooleanField(
                default=False,
                help_text="True when HR recorded this leave retroactively without the approval workflow.",
            ),
        ),
        migrations.AddField(
            model_name="leaverequest",
            name="reconciled_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="leaverequest",
            name="reconciliation_note",
            field=models.TextField(
                blank=True,
                help_text="HR justification for backdated / reconciled leave.",
            ),
        ),
        migrations.AddField(
            model_name="leaverequest",
            name="reconciled_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="reconciled_leave_requests",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="leaveapprovallog",
            name="action",
            field=models.CharField(
                choices=[
                    ("APPROVE", "Approve"),
                    ("REJECT", "Reject"),
                    ("CANCEL", "Cancel"),
                    ("MODIFY", "Modify"),
                    ("RECONCILE", "Reconcile"),
                ],
                max_length=10,
            ),
        ),
    ]
