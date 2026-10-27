"""Tests for loan email notifications (HTML templates & plain-text fallbacks)."""

from decimal import Decimal
from unittest.mock import patch

from django.core import mail
from django.test import TestCase

from apps.accounts.models import RoleName
from apps.loan.models import (
    LoanApplication,
    LoanApplicationStatus,
    LoanApprovalAction,
    LoanApprovalLog,
    LoanRepaymentSchedule,
)
from apps.loan.tasks import (
    notify_loan_approver_required,
    notify_loan_observers,
    notify_loan_decision,
    notify_next_approver,
    notify_loan_disbursed,
    notify_loan_liquidated,
    notify_loan_closed,
)
from apps.loan.tests.helpers import (
    ensure_role,
    make_loan_type,
    make_user,
    setup_department_with_line_manager,
)


class LoanEmailNotificationsTests(TestCase):
    def setUp(self):
        mail.outbox.clear()
        self.dept, self.lm = setup_department_with_line_manager(dept_name="Engineering")
        self.employee = make_user(
            "employee@test.com",
            first_name="Jane",
            last_name="Doe",
            department=self.dept,
        )
        self.hr_user = make_user("hr@test.com", roles=[RoleName.HR])
        self.ed_user = make_user("ed@test.com", roles=[RoleName.EXECUTIVE_DIRECTOR])
        self.loan_type = make_loan_type("Personal Loan")

        self.loan = LoanApplication.objects.create(
            employee=self.employee,
            loan_type=self.loan_type,
            amount=Decimal("120000.00"),
            tenure_months=6,
            purpose="Emergency repairs",
            status=LoanApplicationStatus.PENDING_MANAGER,
        )

    def test_notify_loan_approver_required_pending_manager_sends_html(self):
        ok = notify_loan_approver_required(str(self.loan.id))
        self.assertTrue(ok)
        self.assertEqual(len(mail.outbox), 1)

        msg = mail.outbox[0]
        self.assertIn("Jane Doe", msg.subject)
        self.assertIn(self.lm.email, msg.to)

        # Check plain text
        self.assertIn("Jane Doe", msg.body)
        self.assertIn("Personal Loan", msg.body)
        self.assertIn("120,000.00", msg.body)

        # Check HTML alternative
        self.assertEqual(len(msg.alternatives), 1)
        html_content, mimetype = msg.alternatives[0]
        self.assertEqual(mimetype, "text/html")
        self.assertIn("<!doctype html>", html_content)
        self.assertIn("Incel HRM", html_content)
        self.assertIn("Jane Doe", html_content)
        self.assertIn("120,000.00", html_content)
        self.assertIn("Emergency repairs", html_content)

    def test_notify_loan_approver_required_pending_hr_sends_html(self):
        self.loan.status = LoanApplicationStatus.PENDING_HR
        self.loan.save(update_fields=["status"])

        ok = notify_loan_approver_required(str(self.loan.id))
        self.assertTrue(ok)
        self.assertEqual(len(mail.outbox), 1)

        msg = mail.outbox[0]
        self.assertIn(self.hr_user.email, msg.to)
        html_content, mimetype = msg.alternatives[0]
        self.assertEqual(mimetype, "text/html")
        self.assertIn("New Loan Application", msg.subject)

    @patch("apps.loan.tasks.users_in_observer_scope")
    def test_notify_loan_observers_sends_html(self, mock_observers):
        observer = make_user("observer@test.com")
        mock_observers.return_value = [observer]

        ok = notify_loan_observers(str(self.loan.id))
        self.assertTrue(ok)
        self.assertEqual(len(mail.outbox), 1)

        msg = mail.outbox[0]
        self.assertIn("FYI", msg.subject)
        self.assertIn(observer.email, msg.to)

        html_content, mimetype = msg.alternatives[0]
        self.assertEqual(mimetype, "text/html")
        self.assertIn("information only — no approval action is required", html_content)

    def test_notify_loan_decision_approved_sends_html(self):
        self.loan.status = LoanApplicationStatus.APPROVED
        self.loan.monthly_installment = Decimal("20000.00")
        self.loan.save(update_fields=["status", "monthly_installment"])

        ok = notify_loan_decision(str(self.loan.id), decision=LoanApplicationStatus.APPROVED)
        self.assertTrue(ok)
        self.assertEqual(len(mail.outbox), 1)

        msg = mail.outbox[0]
        self.assertIn("Approved", msg.subject)
        self.assertIn(self.employee.email, msg.to)

        html_content, mimetype = msg.alternatives[0]
        self.assertEqual(mimetype, "text/html")
        self.assertIn("Loan application approved", html_content)
        self.assertIn("20,000.00", html_content)
        self.assertIn("HR will disburse", html_content)

    def test_notify_loan_decision_rejected_sends_html(self):
        self.loan.status = LoanApplicationStatus.REJECTED
        self.loan.save(update_fields=["status"])

        ok = notify_loan_decision(
            str(self.loan.id),
            decision=LoanApplicationStatus.REJECTED,
            comment="Exceeds maximum affordability",
        )
        self.assertTrue(ok)
        self.assertEqual(len(mail.outbox), 1)

        msg = mail.outbox[0]
        self.assertIn("Not Approved", msg.subject)
        self.assertIn(self.employee.email, msg.to)

        html_content, mimetype = msg.alternatives[0]
        self.assertEqual(mimetype, "text/html")
        self.assertIn("Loan application not approved", html_content)
        self.assertIn("Exceeds maximum affordability", html_content)

    def test_notify_next_approver_pending_ed_sends_html(self):
        self.loan.status = LoanApplicationStatus.PENDING_ED
        self.loan.save(update_fields=["status"])

        LoanApprovalLog.objects.create(
            loan=self.loan,
            actor=self.lm,
            action=LoanApprovalAction.APPROVE,
            previous_status=LoanApplicationStatus.PENDING_MANAGER,
            new_status=LoanApplicationStatus.PENDING_HR,
            comment="LM endorsement",
        )

        ok = notify_next_approver(str(self.loan.id))
        self.assertTrue(ok)
        self.assertEqual(len(mail.outbox), 1)

        msg = mail.outbox[0]
        self.assertIn(self.ed_user.email, msg.to)

        html_content, mimetype = msg.alternatives[0]
        self.assertEqual(mimetype, "text/html")
        self.assertIn("Approval history", html_content)
        self.assertIn("LM endorsement", html_content)

    def test_notify_loan_disbursed_sends_html(self):
        self.loan.status = LoanApplicationStatus.ACTIVE
        self.loan.monthly_installment = Decimal("20000.00")
        self.loan.outstanding_balance = Decimal("120000.00")
        self.loan.save(update_fields=["status", "monthly_installment", "outstanding_balance"])

        import datetime
        LoanRepaymentSchedule.objects.create(
            loan=self.loan,
            installment_number=1,
            due_date=datetime.date(2026, 11, 25),
            amount_due=Decimal("20000.00"),
            payment_status="PENDING",
        )

        ok = notify_loan_disbursed(str(self.loan.id))
        self.assertTrue(ok)
        self.assertEqual(len(mail.outbox), 1)

        msg = mail.outbox[0]
        self.assertIn("Disbursed", msg.subject)
        self.assertIn(self.employee.email, msg.to)

        html_content, mimetype = msg.alternatives[0]
        self.assertEqual(mimetype, "text/html")
        self.assertIn("Your loan has been disbursed", html_content)
        self.assertIn("Repayment schedule", html_content)
        self.assertIn("20,000.00", html_content)

    def test_notify_loan_liquidated_sends_html(self):
        self.loan.status = LoanApplicationStatus.LIQUIDATED
        self.loan.save(update_fields=["status"])

        ok = notify_loan_liquidated(str(self.loan.id))
        self.assertTrue(ok)
        self.assertEqual(len(mail.outbox), 1)

        msg = mail.outbox[0]
        self.assertIn("Liquidated", msg.subject)
        self.assertIn(self.employee.email, msg.to)

        html_content, mimetype = msg.alternatives[0]
        self.assertEqual(mimetype, "text/html")
        self.assertIn("Your loan has been liquidated", html_content)

    def test_notify_loan_closed_sends_html(self):
        self.loan.status = LoanApplicationStatus.CLOSED
        self.loan.save(update_fields=["status"])

        ok = notify_loan_closed(str(self.loan.id))
        self.assertTrue(ok)
        self.assertEqual(len(mail.outbox), 1)

        msg = mail.outbox[0]
        self.assertIn("Closed", msg.subject)
        self.assertIn(self.employee.email, msg.to)

        html_content, mimetype = msg.alternatives[0]
        self.assertEqual(mimetype, "text/html")
        self.assertIn("Your loan has been closed", html_content)
