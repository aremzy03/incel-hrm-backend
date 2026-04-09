from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("leave", "0007_alter_leaverequest_cover_person_blank"),
    ]

    operations = [
        migrations.AlterField(
            model_name="leaverequest",
            name="status",
            field=models.CharField(
                max_length=20,
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
                default="DRAFT",
            ),
        ),
        migrations.AlterField(
            model_name="leaveapprovallog",
            name="previous_status",
            field=models.CharField(
                max_length=20,
                blank=True,
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
            ),
        ),
        migrations.AlterField(
            model_name="leaveapprovallog",
            name="new_status",
            field=models.CharField(
                max_length=20,
                blank=True,
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
            ),
        ),
    ]

