# Generated manually for tutorial progress tracking

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0014_user_personnel_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="UserTutorialProgress",
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
                ("tour_id", models.CharField(max_length=64)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("COMPLETED", "Completed"),
                            ("DISMISSED", "Dismissed"),
                        ],
                        max_length=16,
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="tutorial_progress",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "User Tutorial Progress",
                "verbose_name_plural": "User Tutorial Progress",
                "ordering": ["tour_id"],
            },
        ),
        migrations.AddConstraint(
            model_name="usertutorialprogress",
            constraint=models.UniqueConstraint(
                fields=("user", "tour_id"),
                name="accounts_user_tutorial_progress_unique",
            ),
        ),
    ]
