import csv
from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import ConfirmationStatus, User


class Command(BaseCommand):
    help = "Batch confirm staff members with optional exclusion/exception filters."

    def add_arguments(self, parser):
        # Target selection group
        target_group = parser.add_mutually_exclusive_group(required=True)
        target_group.add_argument(
            "--all-pending",
            action="store_true",
            help="Target all active, non-exited users whose confirmation_status is currently PENDING.",
        )
        target_group.add_argument(
            "--all-active",
            action="store_true",
            help="Target all active, non-exited users in the system.",
        )
        target_group.add_argument(
            "--emails",
            nargs="+",
            help="One or more employee login emails to target.",
        )
        target_group.add_argument(
            "--staff-ids",
            nargs="+",
            help="One or more employee staff IDs to target.",
        )
        target_group.add_argument(
            "--file",
            type=str,
            help="Path to a text file (one identifier per line) or CSV containing an 'email' or 'staff_id' column.",
        )

        # Exclusion / Exception flags
        parser.add_argument(
            "--exclude-emails",
            nargs="+",
            default=[],
            help="One or more employee login emails to EXCLUDE/skip from confirmation.",
        )
        parser.add_argument(
            "--exclude-staff-ids",
            nargs="+",
            default=[],
            help="One or more employee staff IDs to EXCLUDE/skip from confirmation.",
        )
        parser.add_argument(
            "--exclude-file",
            type=str,
            default=None,
            help="Path to a text file or CSV with emails/staff IDs to EXCLUDE from confirmation.",
        )

        # Operational options
        parser.add_argument(
            "--date",
            type=str,
            default=None,
            help="Confirmation date in YYYY-MM-DD format (defaults to today's local date).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Simulate the confirmation and display results without making changes to the database.",
        )

    def _parse_date(self, date_str):
        if not date_str:
            return timezone.localdate()
        try:
            return datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            raise CommandError(f"Invalid date format: '{date_str}'. Expected YYYY-MM-DD.")

    def _read_identifiers_from_file(self, file_path_str: str) -> list[str]:
        path = Path(file_path_str).expanduser().resolve()
        if not path.exists():
            raise CommandError(f"File not found: {path}")

        identifiers = []
        with path.open("r", encoding="utf-8-sig") as f:
            sample = f.read(2048)
            f.seek(0)

            # Check if CSV format with header
            if "," in sample and any(h in sample.lower() for h in ["email", "staff_id"]):
                reader = csv.DictReader(f)
                email_key = next((k for k in reader.fieldnames if k.lower() == "email"), None)
                staff_key = next((k for k in reader.fieldnames if k.lower() == "staff_id"), None)
                for row in reader:
                    val = (row.get(email_key) or row.get(staff_key) or "").strip()
                    if val:
                        identifiers.append(val)
            else:
                # Plain text line by line
                for line in f:
                    val = line.strip()
                    if val and not val.startswith("#"):
                        identifiers.append(val)

        return identifiers

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        confirm_date = self._parse_date(options["date"])

        if dry_run:
            self.stdout.write(self.style.WARNING("=== RUNNING IN DRY-RUN MODE (No database changes) ===\n"))

        # -------------------------------------------------------------
        # 1. Compile Exclusions (Exceptions)
        # -------------------------------------------------------------
        excluded_emails = {e.strip().lower() for e in options["exclude_emails"] if e.strip()}
        excluded_staff_ids = {s.strip() for s in options["exclude_staff_ids"] if s.strip()}

        if options["exclude_file"]:
            file_excluded = self._read_identifiers_from_file(options["exclude_file"])
            for item in file_excluded:
                if "@" in item:
                    excluded_emails.add(item.lower())
                else:
                    excluded_staff_ids.add(item)

        if excluded_emails or excluded_staff_ids:
            self.stdout.write(
                self.style.NOTICE(
                    f"Configured exclusions: {len(excluded_emails)} email(s), {len(excluded_staff_ids)} staff ID(s)."
                )
            )

        # -------------------------------------------------------------
        # 2. Resolve Target Users QuerySet
        # -------------------------------------------------------------
        base_qs = User.objects.filter(is_active=True, is_exited=False)
        target_identifiers = None

        if options["all_pending"]:
            qs = base_qs.filter(confirmation_status=ConfirmationStatus.PENDING)
        elif options["all_active"]:
            qs = base_qs
        elif options["emails"]:
            target_identifiers = [e.strip().lower() for e in options["emails"] if e.strip()]
            qs = base_qs.filter(email__in=target_identifiers)
        elif options["staff_ids"]:
            target_identifiers = [s.strip() for s in options["staff_ids"] if s.strip()]
            qs = base_qs.filter(staff_id__in=target_identifiers)
        elif options["file"]:
            target_identifiers = self._read_identifiers_from_file(options["file"])
            lowercased = [x.lower() for x in target_identifiers]
            qs = base_qs.filter(Q(email__in=lowercased) | Q(staff_id__in=target_identifiers))

        matched_users = list(qs.order_by("email"))
        matched_emails = {u.email.lower() for u in matched_users}
        matched_staff_ids = {u.staff_id for u in matched_users if u.staff_id}

        # Check for target identifiers not found
        if target_identifiers is not None:
            missing = [
                ident for ident in target_identifiers
                if ident.lower() not in matched_emails and ident not in matched_staff_ids
            ]
            if missing:
                self.stdout.write(
                    self.style.WARNING(
                        f"Warning: {len(missing)} target identifier(s) not found or inactive:\n - "
                        + "\n - ".join(missing)
                        + "\n"
                    )
                )

        if not matched_users:
            self.stdout.write(self.style.NOTICE("No users matched for confirmation."))
            return

        # -------------------------------------------------------------
        # 3. Process Confirmation with Exclusion Checks
        # -------------------------------------------------------------
        newly_confirmed = 0
        already_confirmed = 0
        skipped_excluded = 0

        with transaction.atomic():
            for user in matched_users:
                user_email_lower = user.email.lower()
                user_staff_id = user.staff_id or ""

                # Check if excluded
                if user_email_lower in excluded_emails or (user_staff_id and user_staff_id in excluded_staff_ids):
                    skipped_excluded += 1
                    self.stdout.write(
                        self.style.WARNING(f" - [EXCLUDED] {user.email} ({user_staff_id or 'No Staff ID'})")
                    )
                    continue

                # Check if already confirmed with the same date
                if user.is_confirmed and user.confirmation_date == confirm_date:
                    already_confirmed += 1
                    self.stdout.write(
                        f" - [SKIP] {user.email} (already confirmed on {user.confirmation_date})"
                    )
                    continue

                # Apply confirmation
                user.confirmation_status = ConfirmationStatus.CONFIRMED
                user.confirmation_date = confirm_date

                if not dry_run:
                    user.save(update_fields=["confirmation_status", "confirmation_date", "updated_at"])

                status_label = "WOULD CONFIRM" if dry_run else "CONFIRMED"
                self.stdout.write(
                    self.style.SUCCESS(
                        f" - [{status_label}] {user.email} ({user_staff_id or 'No Staff ID'}) -> {confirm_date}"
                    )
                )
                newly_confirmed += 1

            if dry_run:
                transaction.set_rollback(True)

        # -------------------------------------------------------------
        # 4. Summary Output
        # -------------------------------------------------------------
        self.stdout.write("\n" + "=" * 50)
        action_word = "Would confirm" if dry_run else "Successfully confirmed"
        self.stdout.write(self.style.SUCCESS(f"Summary: {action_word} {newly_confirmed} user(s)."))
        if skipped_excluded:
            self.stdout.write(self.style.WARNING(f"Excluded: {skipped_excluded} user(s)."))
        if already_confirmed:
            self.stdout.write(f"Already confirmed: {already_confirmed} user(s).")
        self.stdout.write(f"Effective confirmation date: {confirm_date}")
