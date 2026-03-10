from django.db import migrations


def seed_paternity_leave(apps, schema_editor):
    LeaveType = apps.get_model("leave", "LeaveType")
    LeaveType.objects.get_or_create(
        name="Paternity",
        defaults={
            "description": "Paid paternity leave for eligible employees.",
            "default_days": 14,
        },
    )


def unseed_paternity_leave(apps, schema_editor):
    LeaveType = apps.get_model("leave", "LeaveType")
    LeaveType.objects.filter(name="Paternity").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("leave", "0003_leaverequest_cover_person"),
    ]

    operations = [
        migrations.RunPython(seed_paternity_leave, reverse_code=unseed_paternity_leave),
    ]
