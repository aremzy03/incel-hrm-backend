import csv
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


DEFAULT_PASSWORD = "incel123"


def normalize_gender(value: str) -> str:
    v = (value or "").strip().upper()
    if v in {"MALE", "M"}:
        return "MALE"
    if v in {"FEMALE", "F"}:
        return "FEMALE"
    return ""


def clean(value: Any) -> str:
    return (value or "").strip()


class Command(BaseCommand):
    help = "Seed/update accounts.User records from a CSV file."

    def add_arguments(self, parser):
        parser.add_argument(
            "csv_path",
            nargs="?",
            default="hr_software_user_data.csv",
            help="Path to the CSV file (default: hr_software_user_data.csv).",
        )
        parser.add_argument(
            "--reset-passwords",
            action="store_true",
            help="Also reset passwords for existing users (default: only set on create).",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        from django.apps import apps

        User = apps.get_model("accounts", "User")
        Department = apps.get_model("accounts", "Department")

        csv_path = Path(options["csv_path"]).expanduser().resolve()
        if not csv_path.exists():
            raise CommandError(f"CSV file not found: {csv_path}")

        created_count = 0
        updated_count = 0
        skipped_no_email = 0
        skipped_no_department = 0
        password_set_count = 0

        with csv_path.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            required_columns = {
                "First Name",
                "Last Name",
                "Other Names",
                "Email Address",
                "Passwords",
                "Gender",
                "Department",
            }
            if not reader.fieldnames:
                raise CommandError("CSV appears to have no header row.")
            missing = required_columns - set(reader.fieldnames)
            if missing:
                raise CommandError(f"CSV missing required columns: {sorted(missing)}")

            for row in reader:
                email = clean(row.get("Email Address")).lower()
                if not email:
                    skipped_no_email += 1
                    continue

                department_name = clean(row.get("Department"))
                if not department_name:
                    skipped_no_department += 1
                    continue

                department, _ = Department.objects.get_or_create(
                    name=department_name,
                    defaults={"description": ""},
                )

                defaults = {
                    "first_name": clean(row.get("First Name")),
                    "last_name": clean(row.get("Last Name")),
                    "other_names": clean(row.get("Other Names")),
                    "gender": normalize_gender(clean(row.get("Gender"))),
                    "department": department,
                    "is_active": True,
                }

                user, created = User.objects.update_or_create(email=email, defaults=defaults)
                if created:
                    created_count += 1
                else:
                    updated_count += 1

                raw_pw = clean(row.get("Passwords")) or DEFAULT_PASSWORD
                if created or options["reset_passwords"]:
                    user.set_password(raw_pw)
                    user.save(update_fields=["password"])
                    password_set_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                "Seed complete. "
                f"created={created_count}, updated={updated_count}, "
                f"passwords_set={password_set_count}, "
                f"skipped_no_email={skipped_no_email}, skipped_no_department={skipped_no_department}"
            )
        )

