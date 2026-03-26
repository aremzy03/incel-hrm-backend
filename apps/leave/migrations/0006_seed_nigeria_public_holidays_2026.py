import datetime

from django.db import migrations


def seed_public_holidays(apps, schema_editor):
    PublicHoliday = apps.get_model("leave", "PublicHoliday")

    # Seeded from "Nigeria High Commission — Public Holidays for 2026"
    # https://nigeriahcottawa.ca/public-holidays/
    holidays = [
        # Fixed-date
        ("New Year’s Day", datetime.date(2026, 1, 1), True),
        ("Workers’ Day", datetime.date(2026, 5, 1), True),
        ("Democracy Day", datetime.date(2026, 6, 12), True),
        ("Independence Anniversary", datetime.date(2026, 10, 1), True),
        ("Christmas Public Holiday", datetime.date(2026, 12, 25), True),
        ("Boxing Day Holiday", datetime.date(2026, 12, 26), True),
        # Movable / lunar
        ("Eid el fitr", datetime.date(2026, 3, 20), False),
        ("Eid el fitr Public Holiday", datetime.date(2026, 3, 21), False),
        ("Good Friday", datetime.date(2026, 4, 3), False),
        ("Easter Monday", datetime.date(2026, 4, 6), False),
        ("Eid UI-Adha", datetime.date(2026, 5, 27), False),
        ("Eid UI-Adha Public Holiday", datetime.date(2026, 5, 28), False),
        ("Id el Maulud Public Holiday", datetime.date(2026, 8, 26), False),
    ]

    for name, date, is_recurring in holidays:
        PublicHoliday.objects.update_or_create(
            date=date,
            defaults={
                "name": name,
                "is_recurring": is_recurring,
            },
        )


def unseed_public_holidays(apps, schema_editor):
    PublicHoliday = apps.get_model("leave", "PublicHoliday")
    dates = [
        datetime.date(2026, 1, 1),
        datetime.date(2026, 3, 20),
        datetime.date(2026, 3, 21),
        datetime.date(2026, 4, 3),
        datetime.date(2026, 4, 6),
        datetime.date(2026, 5, 1),
        datetime.date(2026, 5, 27),
        datetime.date(2026, 5, 28),
        datetime.date(2026, 6, 12),
        datetime.date(2026, 8, 26),
        datetime.date(2026, 10, 1),
        datetime.date(2026, 12, 25),
        datetime.date(2026, 12, 26),
    ]
    PublicHoliday.objects.filter(date__in=dates).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("leave", "0005_add_pending_supervisor_status"),
    ]

    operations = [
        migrations.RunPython(seed_public_holidays, reverse_code=unseed_public_holidays),
    ]

