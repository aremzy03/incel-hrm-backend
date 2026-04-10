from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("leave", "0009_leave_request_skip_hr_stage"),
    ]

    operations = [
        migrations.AddField(
            model_name="leaverequest",
            name="manager_approver_is_management",
            field=models.BooleanField(
                default=False,
                help_text="If True, the PENDING_MANAGER approver is Management department line manager.",
            ),
        ),
    ]

