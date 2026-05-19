from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("loan", "0002_seed_loan_types"),
    ]

    operations = [
        migrations.AddField(
            model_name="loanrepaymentschedule",
            name="payment_status",
            field=models.CharField(
                choices=[
                    ("PENDING", "Pending"),
                    ("PAID", "Paid"),
                    ("OVERDUE", "Overdue"),
                ],
                db_index=True,
                default="PENDING",
                max_length=20,
            ),
        ),
    ]
