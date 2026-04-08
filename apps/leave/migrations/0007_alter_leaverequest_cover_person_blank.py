from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("leave", "0006_seed_nigeria_public_holidays_2026"),
    ]

    operations = [
        migrations.AlterField(
            model_name="leaverequest",
            name="cover_person",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="covering_leave_requests",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
