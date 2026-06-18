"""Tests for loan settings and line-manager / observer workflow."""

from unittest.mock import patch

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import (
    Department,
    RoleName,
    Unit,
    get_or_create_management_department,
)
from apps.loan.models import LoanApplicationStatus, LoanSettings

from .helpers import (
    ensure_loan_settings,
    ensure_role,
    make_loan_type,
    make_user,
    setup_department_with_line_manager,
)


@patch("apps.loan.views.notify_loan_observers.delay")
@patch("apps.loan.views.notify_loan_approver_required.delay")
@patch("apps.loan.views.notify_next_approver.delay")
@patch("apps.loan.views.notify_loan_decision.delay")
class LoanSettingsAPITests(APITestCase):
    def setUp(self):
        for name in (
            RoleName.EMPLOYEE,
            RoleName.LINE_MANAGER,
            RoleName.HR,
            RoleName.EXECUTIVE_DIRECTOR,
            RoleName.MANAGING_DIRECTOR,
        ):
            ensure_role(name)

        self.department, self.line_manager = setup_department_with_line_manager()
        self.employee = make_user(
            "settings-emp@test.com",
            roles=[RoleName.EMPLOYEE],
            department=self.department,
        )
        self.hr = make_user("settings-hr@test.com", roles=[RoleName.HR])
        self.ed = make_user("settings-ed@test.com", roles=[RoleName.EXECUTIVE_DIRECTOR])
        self.md = make_user("settings-md@test.com", roles=[RoleName.MANAGING_DIRECTOR])
        self.loan_type = make_loan_type("Settings Loan Type")
        ensure_loan_settings(require_line_manager_approval=True)

    def _settings_url(self):
        return reverse("loan-settings")

    def _loan_list_url(self):
        return reverse("loan-application-list")

    def _action(self, pk, action):
        return reverse(f"loan-application-{action}", kwargs={"pk": str(pk)})

    def _create_and_submit(self):
        self.client.force_authenticate(self.employee)
        create = self.client.post(
            self._loan_list_url(),
            {
                "loan_type": str(self.loan_type.id),
                "amount": "5000.00",
                "tenure_months": 6,
                "purpose": "Settings test",
            },
            format="json",
        )
        self.assertEqual(create.status_code, status.HTTP_201_CREATED)
        loan_id = create.data["id"]
        submit = self.client.post(self._action(loan_id, "submit"), {}, format="json")
        self.assertEqual(submit.status_code, status.HTTP_200_OK, submit.data)
        return loan_id, submit.data

    def test_hr_can_get_settings(self, *_mocks):
        self.client.force_authenticate(self.hr)
        response = self.client.get(self._settings_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["require_line_manager_approval"])

    def test_non_hr_cannot_get_settings(self, *_mocks):
        self.client.force_authenticate(self.employee)
        response = self.client.get(self._settings_url())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_observer_department_and_unit_mutually_exclusive(self, *_mocks):
        finance = Department.objects.create(name="Finance Observer Dept")
        unit = Unit.objects.create(name="Finance Unit", department=finance)
        self.client.force_authenticate(self.hr)
        response = self.client.patch(
            self._settings_url(),
            {
                "observer_department_id": str(finance.id),
                "observer_unit_id": str(unit.id),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_submit_defaults_to_pending_manager(self, _decision, _next, _approver, _observers):
        with self.captureOnCommitCallbacks(execute=True):
            loan_id, data = self._create_and_submit()
        self.assertEqual(data["status"], LoanApplicationStatus.PENDING_MANAGER)
        _approver.assert_called_once_with(loan_id)
        _observers.assert_not_called()

    def test_submit_requires_line_manager(self, *_mocks):
        self.department.line_manager = None
        self.department.save(update_fields=["line_manager", "updated_at"])
        self.client.force_authenticate(self.employee)
        create = self.client.post(
            self._loan_list_url(),
            {
                "loan_type": str(self.loan_type.id),
                "amount": "5000.00",
                "tenure_months": 6,
                "purpose": "No LM",
            },
            format="json",
        )
        loan_id = create.data["id"]
        response = self.client.post(self._action(loan_id, "submit"), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_submit_without_lm_goes_to_hr_and_notifies_observers(
        self, _decision, _next, _approver, _observers
    ):
        ensure_loan_settings(require_line_manager_approval=False)
        with self.captureOnCommitCallbacks(execute=True):
            loan_id, data = self._create_and_submit()
        self.assertEqual(data["status"], LoanApplicationStatus.PENDING_HR)
        _approver.assert_called_once_with(loan_id)
        _observers.assert_called_once_with(loan_id)

    def test_line_manager_approves_then_hr_notified(
        self, _decision, _next, _approver, _observers
    ):
        loan_id, _ = self._create_and_submit()
        _approver.reset_mock()
        self.client.force_authenticate(self.line_manager)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                self._action(loan_id, "approve"),
                {"comment": "LM approved"},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], LoanApplicationStatus.PENDING_HR)
        _approver.assert_called_once_with(loan_id)
        _observers.assert_called_once_with(loan_id)

    def test_wrong_line_manager_cannot_approve(self, *_mocks):
        other_dept, _ = setup_department_with_line_manager(dept_name="Other Dept")
        wrong_lm = other_dept.line_manager
        loan_id, _ = self._create_and_submit()
        self.client.force_authenticate(wrong_lm)
        response = self.client.post(
            self._action(loan_id, "approve"),
            {"comment": "nope"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_line_manager_applicant_routes_to_management_lm(self, *_mocks):
        mgmt = get_or_create_management_department()
        mgmt_lm = make_user(
            "mgmt-lm@test.com",
            roles=[RoleName.LINE_MANAGER, RoleName.EXECUTIVE_DIRECTOR],
            department=mgmt,
        )
        mgmt.line_manager = mgmt_lm
        mgmt.save(update_fields=["line_manager", "updated_at"])

        lm_applicant = make_user(
            "lm-applicant@test.com",
            roles=[RoleName.EMPLOYEE, RoleName.LINE_MANAGER],
            department=self.department,
        )
        self.client.force_authenticate(lm_applicant)
        create = self.client.post(
            self._loan_list_url(),
            {
                "loan_type": str(self.loan_type.id),
                "amount": "4000.00",
                "tenure_months": 4,
                "purpose": "LM applicant",
            },
            format="json",
        )
        loan_id = create.data["id"]
        submit = self.client.post(self._action(loan_id, "submit"), {}, format="json")
        self.assertEqual(submit.status_code, status.HTTP_200_OK)
        self.assertEqual(submit.data["status"], LoanApplicationStatus.PENDING_MANAGER)
        self.assertTrue(submit.data["manager_approver_is_management"])

        self.client.force_authenticate(mgmt_lm)
        response = self.client.post(
            self._action(loan_id, "approve"),
            {"comment": "Mgmt LM ok"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], LoanApplicationStatus.PENDING_HR)

    def test_observer_can_list_retrieve_and_logs(self, *_mocks):
        finance = Department.objects.create(name="Finance Observer")
        observer = make_user(
            "finance-observer@test.com",
            roles=[RoleName.EMPLOYEE],
            department=finance,
        )
        ensure_loan_settings(observer_department=finance, observer_unit=None)

        loan_id, _ = self._create_and_submit()
        self.client.force_authenticate(self.line_manager)
        self.client.post(self._action(loan_id, "approve"), {"comment": "ok"}, format="json")

        self.client.force_authenticate(observer)
        list_resp = self.client.get(self._loan_list_url())
        self.assertEqual(list_resp.status_code, status.HTTP_200_OK)
        rows = list_resp.data.get("results", list_resp.data)
        ids = {row["id"] for row in rows}
        self.assertIn(loan_id, ids)

        detail = self.client.get(reverse("loan-application-detail", kwargs={"pk": loan_id}))
        self.assertEqual(detail.status_code, status.HTTP_200_OK)

        logs = self.client.get(self._action(loan_id, "logs"))
        self.assertEqual(logs.status_code, status.HTTP_200_OK)

    def test_observer_cannot_approve_or_disburse(self, *_mocks):
        finance = Department.objects.create(name="Finance Observer 2")
        observer = make_user(
            "finance-observer2@test.com",
            roles=[RoleName.EMPLOYEE],
            department=finance,
        )
        ensure_loan_settings(observer_department=finance, observer_unit=None)

        loan_id, _ = self._create_and_submit()
        self.client.force_authenticate(observer)
        approve = self.client.post(
            self._action(loan_id, "approve"),
            {"comment": "observer approve"},
            format="json",
        )
        self.assertEqual(approve.status_code, status.HTTP_403_FORBIDDEN)

        disburse = self.client.post(self._action(loan_id, "disburse"), {}, format="json")
        self.assertEqual(disburse.status_code, status.HTTP_403_FORBIDDEN)

    def test_observer_can_access_reports(self, *_mocks):
        finance = Department.objects.create(name="Finance Reports")
        observer = make_user(
            "finance-reports@test.com",
            roles=[RoleName.EMPLOYEE],
            department=finance,
        )
        ensure_loan_settings(observer_department=finance, observer_unit=None)
        self.client.force_authenticate(observer)

        for url_name in (
            "loan-report-outstanding",
            "loan-report-schedule-summary",
        ):
            response = self.client.get(reverse(url_name))
            self.assertEqual(response.status_code, status.HTTP_200_OK, url_name)

        ledger = self.client.get(
            reverse(
                "loan-report-employee-ledger",
                kwargs={"employee_id": str(self.employee.id)},
            )
        )
        self.assertEqual(ledger.status_code, status.HTTP_200_OK)
