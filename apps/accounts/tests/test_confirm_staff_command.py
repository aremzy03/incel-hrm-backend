from datetime import date
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import ConfirmationStatus

User = get_user_model()


class ConfirmStaffCommandTests(TestCase):
    def setUp(self):
        self.user1 = User.objects.create_user(
            email="user1@example.com",
            password="testpass123",
            first_name="User",
            last_name="One",
            staff_id="EMP001",
            confirmation_status=ConfirmationStatus.PENDING,
        )
        self.user2 = User.objects.create_user(
            email="user2@example.com",
            password="testpass123",
            first_name="User",
            last_name="Two",
            staff_id="EMP002",
            confirmation_status=ConfirmationStatus.PENDING,
        )
        self.user3 = User.objects.create_user(
            email="user3@example.com",
            password="testpass123",
            first_name="User",
            last_name="Three",
            staff_id="EMP003",
            confirmation_status=ConfirmationStatus.PENDING,
        )

    def test_confirm_with_exclude_emails(self):
        out = StringIO()
        call_command(
            "confirm_staff",
            "--all-pending",
            "--exclude-emails",
            "user2@example.com",
            stdout=out,
        )

        self.user1.refresh_from_db()
        self.user2.refresh_from_db()
        self.user3.refresh_from_db()

        self.assertEqual(self.user1.confirmation_status, ConfirmationStatus.CONFIRMED)
        self.assertEqual(self.user1.confirmation_date, timezone.localdate())

        self.assertEqual(self.user2.confirmation_status, ConfirmationStatus.PENDING)
        self.assertIsNone(self.user2.confirmation_date)

        self.assertEqual(self.user3.confirmation_status, ConfirmationStatus.CONFIRMED)
        self.assertEqual(self.user3.confirmation_date, timezone.localdate())

    def test_dry_run_does_not_modify_database(self):
        out = StringIO()
        call_command(
            "confirm_staff",
            "--all-pending",
            "--dry-run",
            stdout=out,
        )

        self.user1.refresh_from_db()
        self.user2.refresh_from_db()

        self.assertEqual(self.user1.confirmation_status, ConfirmationStatus.PENDING)
        self.assertIsNone(self.user1.confirmation_date)

    def test_confirm_with_custom_date(self):
        out = StringIO()
        custom_date_str = "2026-01-15"
        call_command(
            "confirm_staff",
            "--emails",
            "user1@example.com",
            "--date",
            custom_date_str,
            stdout=out,
        )

        self.user1.refresh_from_db()
        self.assertEqual(self.user1.confirmation_status, ConfirmationStatus.CONFIRMED)
        self.assertEqual(self.user1.confirmation_date, date(2026, 1, 15))
