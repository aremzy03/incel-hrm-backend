from django.db import migrations

ROLES = [
    ("EMPLOYEE", "Default role for all staff members."),
    ("LINE_MANAGER", "Manages a team and approves leave requests."),
    ("HR", "Human Resources — manages employee records and policies."),
    ("EXECUTIVE_DIRECTOR", "Executive Director with elevated approval rights."),
    ("MANAGING_DIRECTOR", "Managing Director with highest-level access."),
]


def seed_roles(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    for name, description in ROLES:
        Role.objects.get_or_create(name=name, defaults={"description": description})


def unseed_roles(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    Role.objects.filter(name__in=[r[0] for r in ROLES]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0002_role_userrole"),
    ]

    operations = [
        migrations.RunPython(seed_roles, reverse_code=unseed_roles),
    ]
