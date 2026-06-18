"""
HTTP-level checks for loan API endpoints (mirrors INCEL HRM Loan API Postman collection).

Postman MCP ``runCollection`` is not wired to this agent; these tests provide the same coverage.
"""

from unittest.mock import patch

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import RoleName
from apps.loan.models import LoanApplicationStatus

from .helpers import (
    ensure_role,
    make_loan_type,
    make_user,
    setup_department_with_line_manager,
)


@patch("apps.loan.views.notify_loan_observers.delay")
@patch("apps.loan.views.notify_loan_approver_required.delay")
@patch("apps.loan.views.notify_loan_closed.delay")
@patch("apps.loan.views.notify_loan_liquidated.delay")
@patch("apps.loan.views.notify_loan_disbursed.delay")
@patch("apps.loan.views.notify_loan_decision.delay")
@patch("apps.loan.views.notify_next_approver.delay")
class LoanAPIEndpointTests(APITestCase):
    """Covers list/create/retrieve/patch/submit/approve/reject/disburse/liquidate/resignation/logs/DELETE."""

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
            dept_name="Endpoint Test Dept"
        )
        self.employee = make_user(
            "loan-emp@test.com",
            roles=[RoleName.EMPLOYEE],
            department=self.department,
        )
        self.hr = make_user("loan-hr@test.com", roles=[RoleName.HR])
        self.ed = make_user("loan-ed@test.com", roles=[RoleName.EXECUTIVE_DIRECTOR])
        self.md = make_user("loan-md@test.com", roles=[RoleName.MANAGING_DIRECTOR])

        self.loan_type = make_loan_type("API Test Loan Type")

    def _loan_list_url(self):
        return reverse("loan-application-list")

    def _loan_detail_url(self, pk):
        return reverse("loan-application-detail", kwargs={"pk": str(pk)})

    def _action(self, pk, action):
        return reverse(f"loan-application-{action}", kwargs={"pk": str(pk)})

    def _approve_full_chain(self, loan_id):
        self.client.force_authenticate(self.line_manager)
        r = self.client.post(
            self._action(loan_id, "approve"),
            {"comment": "LM ok"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertEqual(r.data["status"], LoanApplicationStatus.PENDING_HR)

        self.client.force_authenticate(self.hr)
        r = self.client.post(
            self._action(loan_id, "approve"),
            {"comment": "HR ok"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.data["status"], LoanApplicationStatus.PENDING_ED)

        self.client.force_authenticate(self.ed)
        r = self.client.post(
            self._action(loan_id, "approve"),
            {"comment": "ED ok"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.data["status"], LoanApplicationStatus.PENDING_MD)

        self.client.force_authenticate(self.md)
        r = self.client.post(
            self._action(loan_id, "approve"),
            {"comment": "MD required comment"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.data["status"], LoanApplicationStatus.APPROVED)

    def test_loan_types_list_authenticated(self, *_mocks):
        self.client.force_authenticate(self.employee)
        r = self.client.get(reverse("loan-type-list"))
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(r.data), 1)

    def test_full_workflow_disburse_logs_delete_405(self, *_mocks):
        self.client.force_authenticate(self.employee)
        create = self.client.post(
            self._loan_list_url(),
            {
                "loan_type": str(self.loan_type.id),
                "amount": "5000.00",
                "tenure_months": 6,
                "purpose": "API test",
            },
            format="json",
        )
        self.assertEqual(create.status_code, status.HTTP_201_CREATED, create.data)
        loan_id = create.data["id"]

        r = self.client.get(self._loan_detail_url(loan_id))
        self.assertEqual(r.status_code, status.HTTP_200_OK)

        r = self.client.patch(
            self._loan_detail_url(loan_id),
            {"amount": "5200.00"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)

        r = self.client.post(self._action(loan_id, "submit"), {}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.data["status"], LoanApplicationStatus.PENDING_MANAGER)

        self._approve_full_chain(loan_id)

        self.client.force_authenticate(self.hr)
        r = self.client.post(self._action(loan_id, "disburse"), {}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.data["status"], LoanApplicationStatus.ACTIVE)

        r = self.client.get(self._action(loan_id, "logs"))
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertIsInstance(r.data, list)
        self.assertGreaterEqual(len(r.data), 1)

        self.client.force_authenticate(self.employee)
        r = self.client.delete(self._loan_detail_url(loan_id))
        self.assertEqual(r.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_reject_at_hr_stage(self, *_mocks):
        self.client.force_authenticate(self.employee)
        create = self.client.post(
            self._loan_list_url(),
            {
                "loan_type": str(self.loan_type.id),
                "amount": "3000.00",
                "tenure_months": 3,
                "purpose": "Reject test",
            },
            format="json",
        )
        loan_id = create.data["id"]
        self.client.post(self._action(loan_id, "submit"), {}, format="json")

        self.client.force_authenticate(self.line_manager)
        self.client.post(
            self._action(loan_id, "approve"),
            {"comment": "LM ok"},
            format="json",
        )

        self.client.force_authenticate(self.hr)
        r = self.client.post(
            self._action(loan_id, "reject"),
            {"comment": "No thanks"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.data["status"], LoanApplicationStatus.REJECTED)

    def test_liquidate_active_loan(self, *_mocks):
        emp = make_user(
            "loan-emp-liq@test.com",
            roles=[RoleName.EMPLOYEE],
            department=self.department,
        )
        self.client.force_authenticate(emp)
        create = self.client.post(
            self._loan_list_url(),
            {
                "loan_type": str(self.loan_type.id),
                "amount": "4000.00",
                "tenure_months": 4,
                "purpose": "Liquidate test",
            },
            format="json",
        )
        loan_id = create.data["id"]
        self.client.post(self._action(loan_id, "submit"), {}, format="json")
        self._approve_full_chain(loan_id)

        self.client.force_authenticate(self.hr)
        r = self.client.post(self._action(loan_id, "disburse"), {}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)

        r = self.client.post(self._action(loan_id, "liquidate"), {}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.data["status"], LoanApplicationStatus.LIQUIDATED)

    def test_handle_resignation_active_loan(self, *_mocks):
        emp = make_user(
            "loan-emp-resign@test.com",
            roles=[RoleName.EMPLOYEE],
            department=self.department,
        )
        self.client.force_authenticate(emp)
        create = self.client.post(
            self._loan_list_url(),
            {
                "loan_type": str(self.loan_type.id),
                "amount": "3500.00",
                "tenure_months": 3,
                "purpose": "Resignation test",
            },
            format="json",
        )
        loan_id = create.data["id"]
        self.client.post(self._action(loan_id, "submit"), {}, format="json")
        self._approve_full_chain(loan_id)

        self.client.force_authenticate(self.hr)
        r = self.client.post(self._action(loan_id, "disburse"), {}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)

        r = self.client.post(self._action(loan_id, "handle-resignation"), {}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.data["status"], LoanApplicationStatus.CLOSED)
        self.assertTrue(r.data.get("resignation_deducted"))
