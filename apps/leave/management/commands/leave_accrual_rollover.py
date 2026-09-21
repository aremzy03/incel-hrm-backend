"""Dry-run or apply leave accrual, carry-forward, and year-end expiry."""

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.leave.services import preview_or_run_accrual


class Command(BaseCommand):
    help = (
        "Run leave-year rollover / accrual. Default is --dry-run. "
        "Pass --apply to write ledger rows (idempotent)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--year", type=int, help="Leave year to create (default: current).")
        parser.add_argument("--month", type=int, help="Month for monthly accrual.")
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist changes. Without this flag the command only prints a preview.",
        )
        parser.add_argument("--no-rollover", action="store_true")
        parser.add_argument("--no-monthly", action="store_true")
        parser.add_argument("--weekly", action="store_true")
        parser.add_argument("--anniversary", action="store_true")
        parser.add_argument("--no-carry-expiry", action="store_true")

    def handle(self, *args, **options):
        dry_run = not options["apply"]
        as_of = timezone.localdate()
        result = preview_or_run_accrual(
            as_of=as_of,
            year=options.get("year"),
            month=options.get("month"),
            include_rollover=not options["no_rollover"],
            include_monthly=not options["no_monthly"],
            include_weekly=options["weekly"],
            include_anniversary=options["anniversary"],
            include_carry_expiry=not options["no_carry_expiry"],
            dry_run=dry_run,
        )
        mode = "DRY-RUN" if dry_run else "APPLIED"
        self.stdout.write(f"{mode} as_of={result['as_of']} year={result['year']}")
        self.stdout.write(f"actions={result['action_count']} skipped={len(result['skipped'])}")
        for row in result["actions"][:50]:
            self.stdout.write(str(row))
        if result["action_count"] > 50:
            self.stdout.write(f"... {result['action_count'] - 50} more")
        if dry_run:
            self.stdout.write(self.style.WARNING("No rows written. Re-run with --apply to persist."))
