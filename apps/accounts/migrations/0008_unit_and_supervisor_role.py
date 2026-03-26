from django.db import migrations, models
import django.db.models.deletion
import uuid


def seed_supervisor_role(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    Role.objects.get_or_create(
        name="SUPERVISOR",
        defaults={"description": "Supervisor of a unit within a department."},
    )


def unseed_supervisor_role(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    Role.objects.filter(name="SUPERVISOR").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0007_seed_hr_department"),
    ]

    operations = [
        migrations.CreateModel(
            name="Unit",
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
                    "department",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="units",
                        to="accounts.department",
                    ),
                ),
            ],
            options={
                "verbose_name": "Unit",
                "verbose_name_plural": "Units",
                "ordering": ["name"],
                "unique_together": {("department", "name")},
            },
        ),
        migrations.AddField(
            model_name="user",
            name="unit",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="members",
                to="accounts.unit",
            ),
        ),
        migrations.AddField(
            model_name="unit",
            name="supervisor",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="supervised_unit",
                to="accounts.user",
            ),
        ),
        migrations.RunPython(seed_supervisor_role, reverse_code=unseed_supervisor_role),
    ]

