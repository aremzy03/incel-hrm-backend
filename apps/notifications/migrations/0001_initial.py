from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Notification",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("title", models.CharField(max_length=255)),
                ("body", models.TextField(blank=True)),
                (
                    "type",
                    models.CharField(
                        choices=[
                            ("LEAVE_SUBMITTED", "Leave submitted"),
                            ("LEAVE_ACTION_REQUIRED", "Leave action required"),
                            ("LEAVE_APPROVED", "Leave approved"),
                            ("LEAVE_REJECTED", "Leave rejected"),
                        ],
                        max_length=50,
                    ),
                ),
                ("data", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("read_at", models.DateTimeField(blank=True, null=True)),
                (
                    "recipient",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="notifications",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="notification",
            index=models.Index(fields=["recipient", "-created_at"], name="notificatio_recipient_7a1db0_idx"),
        ),
        migrations.AddIndex(
            model_name="notification",
            index=models.Index(fields=["recipient", "read_at"], name="notificatio_recipient_7b3bb8_idx"),
        ),
    ]

