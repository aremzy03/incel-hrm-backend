from django.db import migrations

LEAVE_TYPES = [
    ("Annual", "Standard annual leave entitlement.", 21),
    ("Sick", "Leave taken due to illness or medical appointment.", 14),
    ("Casual", "Short-notice or personal leave.", 5),
    ("Maternity", "Paid maternity leave for eligible employees.", 90),
]


def seed_leave_types(apps, schema_editor):
    LeaveType = apps.get_model("leave", "LeaveType")
    for name, description, default_days in LEAVE_TYPES:
        LeaveType.objects.get_or_create(
            name=name,
            defaults={"description": description, "default_days": default_days},
        )


def unseed_leave_types(apps, schema_editor):
    LeaveType = apps.get_model("leave", "LeaveType")
    LeaveType.objects.filter(name__in=[lt[0] for lt in LEAVE_TYPES]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("leave", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_leave_types, reverse_code=unseed_leave_types),
    ]
