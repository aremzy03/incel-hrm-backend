# Generated manually for leave reconciliation MVP

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0005_leave_reminder_and_reliever_notifications"),
    ]

    operations = [
        migrations.AlterField(
            model_name="notification",
            name="type",
            field=models.CharField(
                choices=[
                    ("LEAVE_SUBMITTED", "Leave submitted"),
                    ("LEAVE_ACTION_REQUIRED", "Leave action required"),
                    ("LEAVE_APPROVED", "Leave approved"),
                    ("LEAVE_REJECTED", "Leave rejected"),
                    ("LEAVE_RELIEVER_ASSIGNED", "Leave reliever assigned"),
                    ("LEAVE_DEPARTMENT_REMINDER", "Leave department reminder"),
                    ("LEAVE_RECONCILED", "Leave reconciled"),
                    ("LOAN_SUBMITTED", "Loan submitted"),
                    ("LOAN_APPROVED", "Loan approved"),
                    ("LOAN_REJECTED", "Loan rejected"),
                    ("LOAN_DISBURSED", "Loan disbursed"),
                    ("LOAN_LIQUIDATED", "Loan liquidated"),
                    ("LOAN_CLOSED", "Loan closed"),
                    ("LOAN_OBSERVER_NOTICE", "Loan observer notice"),
                    ("LOAN_ACTION_REQUIRED", "Loan action required"),
                ],
                max_length=50,
            ),
        ),
    ]
