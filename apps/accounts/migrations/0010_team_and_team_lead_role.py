from django.db import migrations, models
import django.db.models.deletion
import uuid


def seed_team_lead_role(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    Role.objects.get_or_create(
        name="TEAM_LEAD",
        defaults={"description": "Lead of a team within a unit."},
    )


def unseed_team_lead_role(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    Role.objects.filter(name="TEAM_LEAD").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0009_alter_role_name"),
    ]

    operations = [
        migrations.CreateModel(
            name="Team",
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
                ("name", models.CharField(max_length=150)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "team_lead",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="led_team",
                        to="accounts.user",
                    ),
                ),
                (
                    "unit",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="teams",
                        to="accounts.unit",
                    ),
                ),
            ],
            options={
                "verbose_name": "Team",
                "verbose_name_plural": "Teams",
                "ordering": ["name"],
                "unique_together": {("unit", "name")},
            },
        ),
        migrations.AddField(
            model_name="user",
            name="team",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="members",
                to="accounts.team",
            ),
        ),
        migrations.RunPython(seed_team_lead_role, reverse_code=unseed_team_lead_role),
    ]

