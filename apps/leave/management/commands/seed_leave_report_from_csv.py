"""Import historical leave rows from an HR export CSV."""

import csv
import datetime
from pathlib import Path
from typing import Any

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import F

from apps.leave.models import LeaveBalance, LeaveRequest, LeaveRequestStatus, LeaveType

User = get_user_model()

DATE_FMT = "%d/%m/%Y"
REASON_MAX_LEN = 5000

REQUIRED_COLUMNS = {
    "email Address",
    "Employee Leave Request Start Date",
    "Employee Leave Request End Date",
    "Employee Leave Request Days Taken",
    "Leave Type Name",
    "Employee Leave Request Status",
}

TERMINAL_STATUSES = {
    LeaveRequestStatus.APPROVED,
    LeaveRequestStatus.REJECTED,
    LeaveRequestStatus.CANCELLED,
}


def clean(value: Any) -> str:
    return (value or "").strip()


def parse_date(value: str) -> datetime.date:
    return datetime.datetime.strptime(clean(value), DATE_FMT).date()


class Command(BaseCommand):
    help = (
        "Seed LeaveRequest rows and increment LeaveBalance.used_days from an export CSV "
        "(APPROVED rows only for balance increments)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "csv_path",
            nargs="?",
            default="report_export_file_2026_04_30_11_12_261777543946.csv",
            help="Path to the CSV file.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse and report counts only; do not write to the database.",
        )

    def handle(self, *args, **options):
        csv_path = Path(options["csv_path"]).expanduser().resolve()
        if not csv_path.exists():
            raise CommandError(f"CSV file not found: {csv_path}")

        dry_run = options["dry_run"]
        if dry_run:
            self._run(csv_path, dry_run=True)
        else:
            with transaction.atomic():
                self._run(csv_path, dry_run=False)

    def _run(self, csv_path: Path, *, dry_run: bool) -> None:
        created_requests = 0
        skipped_duplicate_file = 0
        skipped_duplicate_db = 0
        skipped_missing_user = 0
        skipped_missing_leave_type = 0
        skipped_bad_date = 0
        skipped_bad_days = 0
        skipped_bad_status = 0
        balance_increments = 0
        day_mismatch_warnings = 0
        seen_keys: set[tuple[Any, ...]] = set()

        with csv_path.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                raise CommandError("CSV appears to have no header row.")
            missing = REQUIRED_COLUMNS - set(reader.fieldnames)
            if missing:
                raise CommandError(f"CSV missing required columns: {sorted(missing)}")

            for row in reader:
                email = clean(row.get("email Address")).lower()
                if not email:
                    continue

                try:
                    start_date = parse_date(row.get("Employee Leave Request Start Date", ""))
                    end_date = parse_date(row.get("Employee Leave Request End Date", ""))
                except ValueError:
                    skipped_bad_date += 1
                    continue

                if start_date > end_date:
                    skipped_bad_date += 1
                    continue

                try:
                    days_taken = int(clean(row.get("Employee Leave Request Days Taken")))
                except ValueError:
                    skipped_bad_days += 1
                    continue

                if days_taken < 0:
                    skipped_bad_days += 1
                    continue

                leave_type_name = clean(row.get("Leave Type Name"))
                status_raw = clean(row.get("Employee Leave Request Status")).upper()
                if status_raw not in TERMINAL_STATUSES:
                    skipped_bad_status += 1
                    continue

                dup_key = (email, start_date, end_date, leave_type_name, status_raw)
                if dup_key in seen_keys:
                    skipped_duplicate_file += 1
                    continue
                seen_keys.add(dup_key)

                try:
                    user = User.objects.get(email=email)
                except User.DoesNotExist:
                    skipped_missing_user += 1
                    continue

                try:
                    leave_type = LeaveType.objects.get(name=leave_type_name)
                except LeaveType.DoesNotExist:
                    skipped_missing_leave_type += 1
                    continue

                status = status_raw
                if LeaveRequest.objects.filter(
                    employee=user,
                    leave_type=leave_type,
                    start_date=start_date,
                    end_date=end_date,
                    status=status,
                ).exists():
                    skipped_duplicate_db += 1
                    continue

                if dry_run:
                    created_requests += 1
                    if status == LeaveRequestStatus.APPROVED:
                        balance_increments += 1
                    continue

                year = start_date.year
                LeaveBalance.objects.get_or_create(
                    employee=user,
                    leave_type=leave_type,
                    year=year,
                    defaults={
                        "allocated_days": leave_type.default_days,
                        "used_days": 0,
                    },
                )

                desc = clean(row.get("Leave Type Description"))
                reason = desc[:REASON_MAX_LEN] if desc else ""

                leave_request = LeaveRequest.objects.create(
                    employee=user,
                    leave_type=leave_type,
                    start_date=start_date,
                    end_date=end_date,
                    status=status,
                    reason=reason,
                )

                computed = leave_request.total_working_days
                if computed != days_taken:
                    day_mismatch_warnings += 1
                    self.stdout.write(
                        self.style.WARNING(
                            f"Days mismatch for {email} {start_date}–{end_date} "
                            f"{leave_type_name}: CSV={days_taken}, computed={computed}"
                        )
                    )

                if status == LeaveRequestStatus.APPROVED:
                    LeaveBalance.objects.filter(
                        employee=user,
                        leave_type=leave_type,
                        year=year,
                    ).update(used_days=F("used_days") + days_taken)
                    balance_increments += 1

                created_requests += 1

        mode = "DRY-RUN " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{mode}Leave seed complete. "
                f"requests_created_or_would_create={created_requests}, "
                f"balance_increments_or_would={balance_increments}, "
                f"skipped_duplicate_file={skipped_duplicate_file}, "
                f"skipped_duplicate_db={skipped_duplicate_db}, "
                f"skipped_missing_user={skipped_missing_user}, "
                f"skipped_missing_leave_type={skipped_missing_leave_type}, "
                f"skipped_bad_date={skipped_bad_date}, "
                f"skipped_bad_days={skipped_bad_days}, "
                f"skipped_bad_status={skipped_bad_status}, "
                f"day_mismatch_warnings={day_mismatch_warnings}"
            )
        )
