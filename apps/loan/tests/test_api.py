"""
Loan application API tests (DRF APITestCase).
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import RoleName
from apps.loan.models import (
    LoanApplication,
    LoanApplicationStatus,
    LoanApprovalLog,
    LoanRepaymentPaymentStatus,
    LoanRepaymentSchedule,
    LoanType,
)
from apps.loan.services import _add_months
from apps.loan.tests.helpers import ensure_role, make_user, setup_department_with_line_manager

User = get_user_model()


@patch("apps.loan.views.notify_loan_observers.delay")
@patch("apps.loan.views.notify_loan_approver_required.delay")
@patch("apps.loan.views.notify_loan_closed.delay")
@patch("apps.loan.views.notify_loan_liquidated.delay")
@patch("apps.loan.views.notify_loan_disbursed.delay")
@patch("apps.loan.views.notify_loan_decision.delay")
@patch("apps.loan.views.notify_next_approver.delay")
class LoanAPITests(APITestCase):
    def setUp(self):
        for name in (
            RoleName.EMPLOYEE,
            RoleName.LINE_MANAGER,
            RoleName.HR,
            RoleName.EXECUTIVE_DIRECTOR,
            RoleName.MANAGING_DIRECTOR,
        ):
            ensure_role(name)

        self.department, self.line_manager = setup_department_with_line_manager(
            dept_name="Loan API Test Dept"
        )
        self.employee_confirmed = make_user(
            "confirmed-emp@test.com",
            roles=[RoleName.EMPLOYEE],
            confirmed=True,
            department=self.department,
        )
        self.employee_unconfirmed = make_user(
            "unconfirmed-emp@test.com",
            roles=[RoleName.EMPLOYEE],
            confirmed=False,
        )
        self.hr_user = make_user("hr@test.com", roles=[RoleName.HR])
        self.ed_user = make_user("ed@test.com", roles=[RoleName.EXECUTIVE_DIRECTOR])
        self.md_user = make_user("md@test.com", roles=[RoleName.MANAGING_DIRECTOR])

        self.personal_loan, _ = LoanType.objects.get_or_create(
            name="Personal Loan",
            defaults={"description": "Personal loan product"},
        )

    def _list_url(self):
        return reverse("loan-application-list")

    def _detail_url(self, pk):
        return reverse("loan-application-detail", kwargs={"pk": str(pk)})

    def _action_url(self, pk, action):
        return reverse(f"loan-application-{action}", kwargs={"pk": str(pk)})

    def _repayment_schedule_url(self, loan_id, schedule_id):
        return reverse(
            "loan-application-update-repayment-schedule-item",
            kwargs={"pk": str(loan_id), "schedule_id": str(schedule_id)},
        )

    def _valid_payload(self, **overrides):
        data = {
            "loan_type": str(self.personal_loan.id),
            "amount": "5000.00",
            "tenure_months": 6,
            "purpose": "Test loan purpose",
        }
        data.update(overrides)
        return data

    def _create_draft(self, user=None, **payload_overrides):
        user = user or self.employee_confirmed
        self.client.force_authenticate(user)
        response = self.client.post(
            self._list_url(),
            self._valid_payload(**payload_overrides),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return response.data["id"]

    def _submit(self, loan_id, user=None):
        user = user or self.employee_confirmed
        self.client.force_authenticate(user)
        response = self.client.post(self._action_url(loan_id, "submit"), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response

    def _approve_chain(self, loan_id):
        self.client.force_authenticate(self.line_manager)
        r = self.client.post(
            self._action_url(loan_id, "approve"),
            {"comment": "LM approved"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertEqual(r.data["status"], LoanApplicationStatus.PENDING_HR)

        self.client.force_authenticate(self.hr_user)
        r = self.client.post(
            self._action_url(loan_id, "approve"),
            {"comment": "HR approved"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertEqual(r.data["status"], LoanApplicationStatus.PENDING_ED)

        self.client.force_authenticate(self.ed_user)
        r = self.client.post(
            self._action_url(loan_id, "approve"),
            {"comment": "ED approved"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertEqual(r.data["status"], LoanApplicationStatus.PENDING_MD)

        self.client.force_authenticate(self.md_user)
        r = self.client.post(
            self._action_url(loan_id, "approve"),
            {"comment": "MD approval comment"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertEqual(r.data["status"], LoanApplicationStatus.APPROVED)
        return r

    def test_unconfirmed_employee_cannot_apply(self, *_mocks):
        self.client.force_authenticate(self.employee_unconfirmed)
        response = self.client.post(
            self._list_url(),
            self._valid_payload(),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        body = str(response.data).lower()
        self.assertIn("confirmed", body)

    def test_confirmed_employee_can_apply(self, *_mocks):
        self.client.force_authenticate(self.employee_confirmed)
        response = self.client.post(
            self._list_url(),
            self._valid_payload(),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["status"], LoanApplicationStatus.DRAFT)

    def test_tenure_over_12_months_rejected(self, *_mocks):
        self.client.force_authenticate(self.employee_confirmed)
        response = self.client.post(
            self._list_url(),
            self._valid_payload(tenure_months=13),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_zero_amount_rejected(self, *_mocks):
        self.client.force_authenticate(self.employee_confirmed)
        response = self.client.post(
            self._list_url(),
            self._valid_payload(amount="0"),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_duplicate_active_loan_blocked(self, *_mocks):
        draft_id = self._create_draft()
        LoanApplication.objects.create(
            employee=self.employee_confirmed,
            loan_type=self.personal_loan,
            amount=Decimal("3000.00"),
            tenure_months=3,
            purpose="Existing active loan",
            status=LoanApplicationStatus.ACTIVE,
        )
        self.client.force_authenticate(self.employee_confirmed)
        response = self.client.post(
            self._action_url(draft_id, "submit"),
            {},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        body = str(response.data).lower()
        self.assertIn("active loan", body)

    def test_full_approval_chain(self, *_mocks):
        loan_id = self._create_draft()
        self._submit(loan_id)
        final = self._approve_chain(loan_id)
        self.assertEqual(final.data["status"], LoanApplicationStatus.APPROVED)

    def test_rejection_at_hr_stage(self, *_mocks):
        loan_id = self._create_draft()
        self._submit(loan_id)
        self.client.force_authenticate(self.line_manager)
        self.client.post(
            self._action_url(loan_id, "approve"),
            {"comment": "LM ok"},
            format="json",
        )
        self.client.force_authenticate(self.hr_user)
        response = self.client.post(
            self._action_url(loan_id, "reject"),
            {"comment": "Not eligible this cycle"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], LoanApplicationStatus.REJECTED)
        log = LoanApprovalLog.objects.filter(loan_id=loan_id).order_by("-timestamp").first()
        self.assertIsNotNone(log)
        self.assertEqual(log.comment, "Not eligible this cycle")

    def test_rejection_requires_comment(self, *_mocks):
        loan_id = self._create_draft()
        self._submit(loan_id)
        self.client.force_authenticate(self.line_manager)
        self.client.post(
            self._action_url(loan_id, "approve"),
            {"comment": "LM ok"},
            format="json",
        )
        self.client.force_authenticate(self.hr_user)
        response = self.client.post(
            self._action_url(loan_id, "reject"),
            {},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_disburse_generates_schedule(self, *_mocks):
        loan_id = self._create_draft(tenure_months=3, amount="9000.00")
        self._submit(loan_id)
        self._approve_chain(loan_id)

        self.client.force_authenticate(self.hr_user)
        response = self.client.post(
            self._action_url(loan_id, "disburse"),
            {},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], LoanApplicationStatus.ACTIVE)

        loan = LoanApplication.objects.get(pk=loan_id)
        self.assertEqual(loan.repayment_schedule.count(), 3)
        self.assertEqual(loan.outstanding_balance, Decimal("9000.00"))

        schedule = list(
            loan.repayment_schedule.order_by("installment_number").values_list(
                "due_date", flat=True
            )
        )
        base = timezone.localdate(loan.disbursed_at)
        expected = [_add_months(base, n) for n in range(1, 4)]
        self.assertEqual(schedule, expected)

        first = loan.repayment_schedule.order_by("installment_number").first()
        self.assertEqual(first.payment_status, LoanRepaymentPaymentStatus.PENDING)

    def test_hr_can_update_installment_payment_status(self, *_mocks):
        loan_id = self._create_draft(tenure_months=3, amount="9000.00")
        self._submit(loan_id)
        self._approve_chain(loan_id)
        self.client.force_authenticate(self.hr_user)
        self.client.post(self._action_url(loan_id, "disburse"), {}, format="json")

        installment = LoanRepaymentSchedule.objects.filter(loan_id=loan_id).first()
        response = self.client.patch(
            self._repayment_schedule_url(loan_id, installment.id),
            {"payment_status": LoanRepaymentPaymentStatus.PAID},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        installment.refresh_from_db()
        self.assertEqual(installment.payment_status, LoanRepaymentPaymentStatus.PAID)
        self.assertEqual(Decimal(response.data["outstanding_balance"]), Decimal("6000.00"))

    def test_employee_cannot_update_installment_payment_status(self, *_mocks):
        loan_id = self._create_draft(tenure_months=3, amount="9000.00")
        self._submit(loan_id)
        self._approve_chain(loan_id)
        self.client.force_authenticate(self.hr_user)
        self.client.post(self._action_url(loan_id, "disburse"), {}, format="json")
        installment = LoanRepaymentSchedule.objects.filter(loan_id=loan_id).first()

        self.client.force_authenticate(self.employee_confirmed)
        response = self.client.patch(
            self._repayment_schedule_url(loan_id, installment.id),
            {"payment_status": LoanRepaymentPaymentStatus.OVERDUE},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_retrieve_marks_past_due_installments_overdue(self, *_mocks):
        loan_id = self._create_draft(tenure_months=3, amount="9000.00")
        self._submit(loan_id)
        self._approve_chain(loan_id)
        self.client.force_authenticate(self.hr_user)
        self.client.post(self._action_url(loan_id, "disburse"), {}, format="json")

        installment = LoanRepaymentSchedule.objects.filter(loan_id=loan_id).first()
        installment.due_date = timezone.localdate() - timedelta(days=1)
        installment.save(update_fields=["due_date", "updated_at"])

        self.client.force_authenticate(self.employee_confirmed)
        response = self.client.get(self._detail_url(loan_id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        row = response.data["repayment_schedule"][0]
        self.assertEqual(row["payment_status"], LoanRepaymentPaymentStatus.OVERDUE)

    def test_disburse_enqueues_employee_notification(
        self, _next, _decision, _disbursed, _liquidated, _closed, _approver, _observers
    ):
        loan_id = self._create_draft()
        self._submit(loan_id)
        self._approve_chain(loan_id)
        self.client.force_authenticate(self.hr_user)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(self._action_url(loan_id, "disburse"), {}, format="json")
        _disbursed.assert_called_once_with(str(loan_id))

    def test_employee_cannot_approve(self, *_mocks):
        loan_id = self._create_draft()
        self._submit(loan_id)
        self.client.force_authenticate(self.employee_confirmed)
        response = self.client.post(
            self._action_url(loan_id, "approve"),
            {"comment": "self approve"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_hr_can_liquidate_active_loan(
        self, _next, _decision, _disbursed, _liquidated, _closed, _approver, _observers
    ):
        loan_id = self._create_draft()
        self._submit(loan_id)
        self._approve_chain(loan_id)
        self.client.force_authenticate(self.hr_user)
        self.client.post(self._action_url(loan_id, "disburse"), {}, format="json")

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                self._action_url(loan_id, "liquidate"),
                {},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], LoanApplicationStatus.LIQUIDATED)
        self.assertEqual(Decimal(response.data["outstanding_balance"]), Decimal("0"))
        _liquidated.assert_called_once_with(str(loan_id))

    def test_resignation_handler_closes_loan(
        self, _next, _decision, _disbursed, _liquidated, _closed, _approver, _observers
    ):
        loan_id = self._create_draft()
        self._submit(loan_id)
        self._approve_chain(loan_id)
        self.client.force_authenticate(self.hr_user)
        self.client.post(self._action_url(loan_id, "disburse"), {}, format="json")

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                self._action_url(loan_id, "handle-resignation"),
                {},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], LoanApplicationStatus.CLOSED)
        self.assertTrue(response.data["resignation_deducted"])
        _closed.assert_called_once_with(str(loan_id))
