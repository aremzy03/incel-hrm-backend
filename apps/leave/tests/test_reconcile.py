import datetime
import io

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Department, Role, RoleName, UserRole
from apps.leave.models import (
    ApprovalAction,
    BalanceTransactionType,
    LeaveApprovalLog,
    LeaveBalance,
    LeaveBalanceTransaction,
    LeavePolicy,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveType,
)
from apps.leave.services import split_working_days_by_year
from apps.notifications.models import Notification, NotificationType


User = get_user_model()


class LeaveReconcileApiTests(APITestCase):
    def setUp(self):
        self.password = "testpass123"
        self.current_year = timezone.now().year
        self.backdated_start = datetime.date(self.current_year - 1, 3, 10)
        self.backdated_end = datetime.date(self.current_year - 1, 3, 14)

        self.department = Department.objects.create(name="Engineering")
        self.hr_department, _ = Department.objects.get_or_create(
            name="Human Resources (HR)"
        )

        self.employee = self._create_user_with_roles(
            "employee.reconcile@test.com", [RoleName.EMPLOYEE], department=self.department
        )
        self.line_manager = self._create_user_with_roles(
            "line.manager.reconcile@test.com",
            [RoleName.LINE_MANAGER],
            department=self.department,
        )
        self.hr_user = self._create_user_with_roles(
            "hr.reconcile@test.com", [RoleName.HR], department=self.hr_department
        )
        self.executive_director = self._create_user_with_roles(
            "ed.reconcile@test.com",
            [RoleName.EXECUTIVE_DIRECTOR, RoleName.LINE_MANAGER],
            department=self.hr_department,
        )

        self.department.line_manager = self.line_manager
        self.department.save(update_fields=["line_manager", "updated_at"])

        self.leave_type, _ = LeaveType.objects.get_or_create(
            name="Annual",
            defaults={"default_days": 21},
        )
        self.balance, _ = LeaveBalance.objects.update_or_create(
            employee=self.employee,
            leave_type=self.leave_type,
            year=self.backdated_start.year,
            defaults={"allocated_days": 21, "used_days": 0},
        )

        self.reconcile_url = reverse("leave-request-reconcile")
        self.bulk_reconcile_url = reverse("leave-request-bulk-reconcile")

    def _create_user_with_roles(self, email, roles, department=None):
        user = User.objects.create_user(
            email=email,
            password=self.password,
            department=department,
        )
        for role_name in roles:
            role, _ = Role.objects.get_or_create(name=role_name)
            UserRole.objects.get_or_create(user=user, role=role)
        return user

    def _auth(self, user):
        self.client.force_authenticate(user=user)

    def _reconcile_payload(self, **overrides):
        payload = {
            "employee": str(self.employee.id),
            "leave_type": str(self.leave_type.id),
            "start_date": self.backdated_start.isoformat(),
            "end_date": self.backdated_end.isoformat(),
            "reason": "Absent without prior application",
            "reconciliation_note": "Confirmed with line manager",
        }
        payload.update(overrides)
        return payload

    @override_settings(
        CELERY_TASK_ALWAYS_EAGER=True,
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        FRONTEND_BASE_URL="http://localhost:3000",
    )
    def test_hr_can_reconcile_backdated_leave(self):
        self._auth(self.hr_user)
        mail.outbox.clear()

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                self.reconcile_url,
                self._reconcile_payload(),
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["is_reconciled"])
        self.assertEqual(response.data["status"], LeaveRequestStatus.APPROVED)

        leave_request = LeaveRequest.objects.get(id=response.data["id"])
        self.balance.refresh_from_db()
        self.assertEqual(self.balance.used_days, leave_request.total_working_days)

        self.assertTrue(
            LeaveBalanceTransaction.objects.filter(
                leave_request=leave_request,
                transaction_type=BalanceTransactionType.DEDUCT,
            ).exists()
        )

        log = LeaveApprovalLog.objects.get(leave_request=leave_request)
        self.assertEqual(log.action, ApprovalAction.RECONCILE)

        notifications = Notification.objects.filter(
            type=NotificationType.LEAVE_RECONCILED,
            data__leave_request_id=str(leave_request.id),
        )
        recipient_ids = {str(n.recipient_id) for n in notifications}
        self.assertIn(str(self.employee.id), recipient_ids)
        self.assertIn(str(self.line_manager.id), recipient_ids)

    def test_non_hr_cannot_reconcile(self):
        self._auth(self.employee)
        response = self.client.post(
            self.reconcile_url,
            self._reconcile_payload(),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_reconcile_requires_reconciliation_note(self):
        self._auth(self.hr_user)
        response = self.client.post(
            self.reconcile_url,
            self._reconcile_payload(reconciliation_note="   "),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reconcile_blocks_duplicate_approved_request(self):
        self._auth(self.hr_user)
        self.client.post(self.reconcile_url, self._reconcile_payload(), format="json")
        second = self.client.post(
            self.reconcile_url, self._reconcile_payload(), format="json"
        )
        self.assertEqual(second.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reconcile_blocks_insufficient_balance(self):
        self.balance.used_days = 21
        self.balance.save(update_fields=["used_days", "updated_at"])

        self._auth(self.hr_user)
        response = self.client.post(
            self.reconcile_url,
            self._reconcile_payload(),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reconcile_allow_insufficient_balance_override(self):
        self.balance.used_days = 21
        self.balance.save(update_fields=["used_days", "updated_at"])

        self._auth(self.hr_user)
        response = self.client.post(
            self.reconcile_url,
            self._reconcile_payload(allow_insufficient_balance=True),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.balance.refresh_from_db()
        leave_request = LeaveRequest.objects.get(id=response.data["id"])
        self.assertGreater(self.balance.used_days, 21)

    def test_reconcile_respects_backdating_policy(self):
        LeavePolicy.objects.create(
            leave_type=self.leave_type,
            annual_entitlement=21,
            allow_backdated=False,
        )
        self._auth(self.hr_user)
        response = self.client.post(
            self.reconcile_url,
            self._reconcile_payload(),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("start_date", response.data)

    def test_cancel_reconciled_leave_restores_balance(self):
        self._auth(self.hr_user)
        create_resp = self.client.post(
            self.reconcile_url,
            self._reconcile_payload(),
            format="json",
        )
        leave_request_id = create_resp.data["id"]
        leave_request = LeaveRequest.objects.get(id=leave_request_id)
        deducted = leave_request.total_working_days
        self.balance.refresh_from_db()
        self.assertEqual(self.balance.used_days, deducted)

        cancel_url = reverse("leave-request-cancel", kwargs={"pk": leave_request_id})
        cancel_resp = self.client.post(cancel_url, {"comment": "Recorded in error"}, format="json")
        self.assertEqual(cancel_resp.status_code, status.HTTP_200_OK)

        self.balance.refresh_from_db()
        self.assertEqual(self.balance.used_days, 0)
        self.assertTrue(
            LeaveBalanceTransaction.objects.filter(
                leave_request=leave_request,
                transaction_type=BalanceTransactionType.REFUND,
            ).exists()
        )

    def test_double_refund_on_cancel_blocked(self):
        self._auth(self.hr_user)
        create_resp = self.client.post(
            self.reconcile_url,
            self._reconcile_payload(),
            format="json",
        )
        leave_request_id = create_resp.data["id"]
        cancel_url = reverse("leave-request-cancel", kwargs={"pk": leave_request_id})
        self.client.post(cancel_url, {}, format="json")

        second_cancel = self.client.post(cancel_url, {}, format="json")
        self.assertEqual(second_cancel.status_code, status.HTTP_400_BAD_REQUEST)

    def test_hr_can_edit_reconciled_leave_and_adjust_balance(self):
        self._auth(self.hr_user)
        create_resp = self.client.post(
            self.reconcile_url,
            self._reconcile_payload(),
            format="json",
        )
        leave_request_id = create_resp.data["id"]
        shorter_end = self.backdated_start + datetime.timedelta(days=1)

        patch_resp = self.client.patch(
            reverse("leave-request-detail", kwargs={"pk": leave_request_id}),
            {
                "end_date": shorter_end.isoformat(),
                "edit_note": "Corrected end date with manager",
            },
            format="json",
        )
        self.assertEqual(patch_resp.status_code, status.HTTP_200_OK)

        leave_request = LeaveRequest.objects.get(id=leave_request_id)
        self.balance.refresh_from_db()
        self.assertEqual(self.balance.used_days, leave_request.total_working_days)
        self.assertTrue(
            LeaveBalanceTransaction.objects.filter(
                leave_request=leave_request,
                transaction_type=BalanceTransactionType.ADJUST,
            ).exists()
        )

    def test_cross_year_reconcile_splits_balance_by_year(self):
        dec_start = datetime.date(self.current_year - 1, 12, 30)
        jan_end = datetime.date(self.current_year, 1, 3)
        balance_prev, _ = LeaveBalance.objects.update_or_create(
            employee=self.employee,
            leave_type=self.leave_type,
            year=dec_start.year,
            defaults={"allocated_days": 21, "used_days": 0},
        )
        balance_curr, _ = LeaveBalance.objects.update_or_create(
            employee=self.employee,
            leave_type=self.leave_type,
            year=jan_end.year,
            defaults={"allocated_days": 21, "used_days": 0},
        )

        self._auth(self.hr_user)
        response = self.client.post(
            self.reconcile_url,
            self._reconcile_payload(start_date=dec_start.isoformat(), end_date=jan_end.isoformat()),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        year_splits = split_working_days_by_year(dec_start, jan_end)
        balance_prev.refresh_from_db()
        balance_curr.refresh_from_db()
        self.assertEqual(balance_prev.used_days, year_splits[dec_start.year])
        self.assertEqual(balance_curr.used_days, year_splits[jan_end.year])

    def test_bulk_reconcile_creates_multiple_requests(self):
        colleague = self._create_user_with_roles(
            "colleague.reconcile@test.com",
            [RoleName.EMPLOYEE],
            department=self.department,
        )
        LeaveBalance.objects.update_or_create(
            employee=colleague,
            leave_type=self.leave_type,
            year=self.backdated_start.year,
            defaults={"allocated_days": 21, "used_days": 0},
        )
        alt_start = self.backdated_start + datetime.timedelta(days=30)
        alt_end = alt_start + datetime.timedelta(days=2)

        self._auth(self.hr_user)
        response = self.client.post(
            self.bulk_reconcile_url,
            {
                "rows": [
                    {
                        "employee": str(self.employee.id),
                        "leave_type": str(self.leave_type.id),
                        "start_date": self.backdated_start.isoformat(),
                        "end_date": self.backdated_end.isoformat(),
                        "reconciliation_note": "Row one",
                    },
                    {
                        "employee": str(colleague.id),
                        "leave_type": str(self.leave_type.id),
                        "start_date": alt_start.isoformat(),
                        "end_date": alt_end.isoformat(),
                        "reconciliation_note": "Row two",
                    },
                ],
            },
            format="json",
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_201_CREATED, status.HTTP_207_MULTI_STATUS),
        )
        self.assertEqual(response.data["created_count"], 2)

    def test_bulk_reconcile_csv_upload(self):
        self._auth(self.hr_user)
        csv_content = (
            "email,leave_type,start_date,end_date,reconciliation_note,reason\n"
            f"{self.employee.email},Annual,{self.backdated_start.isoformat()},"
            f"{self.backdated_end.isoformat()},CSV note,Absent\n"
        )
        url = reverse("leave-request-bulk-reconcile-csv")
        response = self.client.post(
            url,
            {"file": io.BytesIO(csv_content.encode("utf-8"))},
            format="multipart",
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_201_CREATED, status.HTTP_207_MULTI_STATUS),
        )
        self.assertEqual(response.data["created_count"], 1)

    def test_list_filter_is_reconciled(self):
        self._auth(self.hr_user)
        self.client.post(self.reconcile_url, self._reconcile_payload(), format="json")

        list_url = reverse("leave-request-list")
        reconciled_resp = self.client.get(list_url, {"is_reconciled": "true"})
        results = reconciled_resp.data["results"]
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["is_reconciled"])

    @override_settings(
        CELERY_TASK_ALWAYS_EAGER=True,
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        FRONTEND_BASE_URL="http://localhost:3000",
    )
    def test_notify_department_colleagues_flag(self):
        colleague = self._create_user_with_roles(
            "dept.colleague@test.com",
            [RoleName.EMPLOYEE],
            department=self.department,
        )
        self._auth(self.hr_user)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                self.reconcile_url,
                self._reconcile_payload(notify_department_colleagues=True),
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        leave_request_id = response.data["id"]
        notifications = Notification.objects.filter(
            type=NotificationType.LEAVE_RECONCILED,
            data__leave_request_id=leave_request_id,
        )
        recipient_ids = {str(n.recipient_id) for n in notifications}
        self.assertIn(str(colleague.id), recipient_ids)
