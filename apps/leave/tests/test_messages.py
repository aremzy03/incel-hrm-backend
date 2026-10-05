"""Tests for descriptive leave validation messages."""

import datetime

from django.test import TestCase

from apps.leave import messages as leave_messages
from apps.leave.models import LeaveRequestStatus


class LeaveMessageFormattingTests(TestCase):
    def test_colleague_overlap_includes_name_and_dates(self):
        class FakeEmployee:
            def get_full_name(self):
                return "Jane Doe"

            email = "jane@example.com"

        class FakeLeaveType:
            name = "Annual Leave"

        class FakeRequest:
            employee = FakeEmployee()
            leave_type = FakeLeaveType()
            status = LeaveRequestStatus.APPROVED
            start_date = datetime.date(2026, 3, 10)
            end_date = datetime.date(2026, 3, 14)

        message = leave_messages.colleague_overlapping_leave(FakeRequest())
        self.assertIn("Jane Doe", message)
        self.assertIn("10 Mar 2026", message)
        self.assertIn("different date range", message.lower())

    def test_email_action_required_copy(self):
        class FakeLeaveType:
            name = "Annual Leave"

        class FakeEmployee:
            def get_full_name(self):
                return "Jane Doe"

            email = "jane@example.com"

        class FakeRequest:
            employee = FakeEmployee()
            leave_type = FakeLeaveType()
            start_date = datetime.date(2026, 5, 1)
            end_date = datetime.date(2026, 5, 5)
            total_working_days = 5
            status = LeaveRequestStatus.PENDING_MANAGER
            reason = "Family event"

        body = leave_messages.email_action_required_body(
            FakeRequest(),
            action_url="https://app.example/leave/1",
        )
        self.assertIn("Jane Doe", body)
        self.assertIn("approve or reject", body.lower())
        self.assertIn("Review request:", body)

    def test_email_decision_rejected_copy(self):
        class FakeLeaveType:
            name = "Annual Leave"

        class FakeEmployee:
            def get_full_name(self):
                return "Jane Doe"

            email = "jane@example.com"

        class FakeRequest:
            employee = FakeEmployee()
            leave_type = FakeLeaveType()
            start_date = datetime.date(2026, 5, 1)
            end_date = datetime.date(2026, 5, 5)
            total_working_days = 5
            cover_person_id = None

        body = leave_messages.email_decision_body(
            FakeRequest(),
            approved=False,
            comment="Insufficient cover",
        )
        self.assertIn("not approved", body.lower())
        self.assertIn("Insufficient cover", body)
        self.assertIn("line manager or HR", body)
