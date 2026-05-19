"""
HTTP-level checks for loan API endpoints (mirrors INCEL HRM Loan API Postman collection).

Postman MCP ``runCollection`` is not wired to this agent; these tests provide the same coverage.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import ConfirmationStatus, Role, RoleName, UserRole
from apps.loan.models import LoanApplicationStatus, LoanType

User = get_user_model()


def ensure_role(name: str) -> Role:
    role, _ = Role.objects.get_or_create(name=name, defaults={"description": name})
    return role


def make_user(email: str, *, password="testpass123", roles=None, **extra):
    user = User.objects.create_user(
        email=email,
        password=password,
        confirmation_status=ConfirmationStatus.CONFIRMED,
        **extra,
    )
    for role_name in roles or []:
        role = ensure_role(role_name)
        UserRole.objects.get_or_create(user=user, role=role)
    return user


def make_loan_type():
    return LoanType.objects.get_or_create(
        name="API Test Loan Type",
        defaults={"description": "For loan API tests"},
    )[0]


@patch("apps.loan.views.notify_loan_closed.delay")
@patch("apps.loan.views.notify_loan_liquidated.delay")
@patch("apps.loan.views.notify_loan_disbursed.delay")
@patch("apps.loan.views.notify_loan_decision.delay")
@patch("apps.loan.views.notify_loan_submitted.delay")
@patch("apps.loan.views.notify_next_approver.delay")
class LoanAPIEndpointTests(APITestCase):
    """Covers list/create/retrieve/patch/submit/approve/reject/disburse/liquidate/resignation/logs/DELETE."""

    def setUp(self):
        for name in (
            RoleName.EMPLOYEE,
            RoleName.HR,
            RoleName.EXECUTIVE_DIRECTOR,
            RoleName.MANAGING_DIRECTOR,
        ):
            ensure_role(name)

        self.employee = make_user("loan-emp@test.com", roles=[RoleName.EMPLOYEE])
        self.hr = make_user("loan-hr@test.com", roles=[RoleName.HR])
        self.ed = make_user("loan-ed@test.com", roles=[RoleName.EXECUTIVE_DIRECTOR])
        self.md = make_user("loan-md@test.com", roles=[RoleName.MANAGING_DIRECTOR])

        self.loan_type = make_loan_type()

    def _loan_list_url(self):
        return reverse("loan-application-list")

    def _loan_detail_url(self, pk):
        return reverse("loan-application-detail", kwargs={"pk": str(pk)})

    def _action(self, pk, action):
        return reverse(f"loan-application-{action}", kwargs={"pk": str(pk)})

    def test_loan_types_list_authenticated(self, _next, _submitted, _decision, _disbursed, _liquidated, _closed):
        self.client.force_authenticate(self.employee)
        r = self.client.get(reverse("loan-type-list"))
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(r.data), 1)

    def test_full_workflow_disburse_logs_delete_405(self, _next, _submitted, _decision, _disbursed, _liquidated, _closed):
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

    def test_reject_at_hr_stage(self, _next, _submitted, _decision, _disbursed, _liquidated, _closed):
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

        self.client.force_authenticate(self.hr)
        r = self.client.post(
            self._action(loan_id, "reject"),
            {"comment": "No thanks"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.data["status"], LoanApplicationStatus.REJECTED)

    def test_liquidate_active_loan(self, _next, _submitted, _decision, _disbursed, _liquidated, _closed):
        emp = make_user("loan-emp-liq@test.com", roles=[RoleName.EMPLOYEE])
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
        for user, comment in (
            (self.hr, "HR"),
            (self.ed, "ED"),
            (self.md, "MD comment for liquidate path"),
        ):
            self.client.force_authenticate(user)
            body = {"comment": comment} if user == self.md else {"comment": comment}
            r = self.client.post(self._action(loan_id, "approve"), body, format="json")
            self.assertEqual(r.status_code, status.HTTP_200_OK, (user.email, r.data))

        self.client.force_authenticate(self.hr)
        r = self.client.post(self._action(loan_id, "disburse"), {}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)

        r = self.client.post(self._action(loan_id, "liquidate"), {}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.data["status"], LoanApplicationStatus.LIQUIDATED)

    def test_handle_resignation_active_loan(self, _next, _submitted, _decision, _disbursed, _liquidated, _closed):
        emp = make_user("loan-emp-resign@test.com", roles=[RoleName.EMPLOYEE])
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
        for user, comment in (
            (self.hr, "HR"),
            (self.ed, "ED"),
            (self.md, "MD comment for resignation path"),
        ):
            self.client.force_authenticate(user)
            r = self.client.post(
                self._action(loan_id, "approve"),
                {"comment": comment},
                format="json",
            )
            self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)

        self.client.force_authenticate(self.hr)
        r = self.client.post(self._action(loan_id, "disburse"), {}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)

        r = self.client.post(self._action(loan_id, "handle-resignation"), {}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.data["status"], LoanApplicationStatus.CLOSED)
        self.assertTrue(r.data.get("resignation_deducted"))
