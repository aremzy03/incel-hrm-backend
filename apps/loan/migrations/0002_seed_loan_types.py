from django.db import migrations

LOAN_TYPES = [
    ("Personal Loan", "Interest-free personal loan for employees."),
    ("Compassionate Loan", "Interest-free compassionate loan for employees in need."),
]


def seed_loan_types(apps, schema_editor):
    LoanType = apps.get_model("loan", "LoanType")
    for name, description in LOAN_TYPES:
        LoanType.objects.get_or_create(
            name=name,
            defaults={"description": description},
        )


def unseed_loan_types(apps, schema_editor):
    LoanType = apps.get_model("loan", "LoanType")
    LoanType.objects.filter(name__in=[lt[0] for lt in LOAN_TYPES]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("loan", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_loan_types, reverse_code=unseed_loan_types),
    ]
