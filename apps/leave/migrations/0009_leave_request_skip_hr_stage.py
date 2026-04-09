from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("leave", "0008_add_pending_team_lead_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="leaverequest",
            name="skip_hr_stage",
            field=models.BooleanField(
                default=False,
                help_text="If True, the manager stage transitions directly to ED (skipping HR).",
            ),
        ),
    ]

