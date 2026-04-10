from django.db import migrations, models
import django.db.models.deletion
import uuid


MANAGEMENT_DEPARTMENT_NAME = "Management"


def seed_management_department(apps, schema_editor):
    Department = apps.get_model("accounts", "Department")
    Department.objects.get_or_create(
        name=MANAGEMENT_DEPARTMENT_NAME,
        defaults={"description": "Default department for management chain routing."},
    )


def reverse_seed_management_department(apps, schema_editor):
    # No-op reverse: do not delete management department as it may have memberships.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0011_alter_role_name_choices_team_lead"),
    ]

    operations = [
        migrations.CreateModel(
            name="DepartmentMembership",
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
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "department",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="memberships",
                        to="accounts.department",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="department_memberships",
                        to="accounts.user",
                    ),
                ),
            ],
            options={
                "verbose_name": "Department Membership",
                "verbose_name_plural": "Department Memberships",
                "unique_together": {("user", "department")},
            },
        ),
        migrations.RunPython(seed_management_department, reverse_seed_management_department),
    ]

