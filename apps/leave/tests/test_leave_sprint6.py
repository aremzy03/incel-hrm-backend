import datetime

from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Department, Role, RoleName, UserRole
from apps.leave.models import (
    ApproverDelegate,
    DEFAULT_WORKFLOW_NAME,
    LeaveBalance,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveSettingsAuditLog,
    LeaveType,
    LeaveWorkflowTemplate,
    SettingsAuditAction,
)
from apps.leave.tasks import escalate_stale_leave_approvals
from apps.leave.workflow import default_stage_specs, ensure_default_workflow_template

from django.contrib.auth import get_user_model

User = get_user_model()


class LeaveSprint6WorkflowTests(APITestCase):
    def setUp(self):
        self.password = "testpass123"
        self.department = Department.objects.create(name="Sprint6 Eng")
        self.hr_department, _ = Department.objects.get_or_create(name="Human Resources (HR)")
        self.employee = self._create_user("emp-s6@test.com", [RoleName.EMPLOYEE], self.department)
        self.cover = self._create_user("cover-s6@test.com", [RoleName.EMPLOYEE], self.department)
        self.line_manager = self._create_user(
            "lm-s6@test.com", [RoleName.LINE_MANAGER], self.department
        )
        self.delegate = self._create_user("del-s6@test.com", [RoleName.EMPLOYEE], self.department)
        self.hr_user = self._create_user("hr-s6@test.com", [RoleName.HR], self.hr_department)
        self.ed = self._create_user(
            "ed-s6@test.com",
            [RoleName.EXECUTIVE_DIRECTOR, RoleName.LINE_MANAGER],
            self.hr_department,
        )
        self.department.line_manager = self.line_manager
        self.department.save(update_fields=["line_manager"])

        self.annual = LeaveType.objects.get(code=LeaveType.Code.ANNUAL)
        self.year = timezone.now().year + 10
        self.start = self._weekday(datetime.date(self.year, 4, 6))
        self.end = self.start + datetime.timedelta(days=2)
        LeaveBalance.objects.update_or_create(
            employee=self.employee,
            leave_type=self.annual,
            year=self.start.year,
            defaults={"allocated_days": 21, "used_days": 0, "pending_days": 0},
        )
        self.workflows_url = reverse("leave-workflow-list")
        self.list_url = reverse("leave-request-list")

    def _create_user(self, email, roles, department=None):
        user = User.objects.create_user(email=email, password=self.password, department=department)
        for role_name in roles:
            role, _ = Role.objects.get_or_create(name=role_name)
            UserRole.objects.get_or_create(user=user, role=role)
        return user

    def _weekday(self, date):
        while date.weekday() >= 5:
            date += datetime.timedelta(days=1)
        return date

    def _submit_employee_request(self):
        self.client.force_authenticate(self.employee)
        create = self.client.post(
            self.list_url,
            {
                "leave_type": str(self.annual.id),
                "start_date": self.start.isoformat(),
                "end_date": self.end.isoformat(),
                "reason": "Sprint 6",
                "is_emergency": False,
                "cover_person": str(self.cover.id),
            },
            format="json",
        )
        self.assertEqual(create.status_code, status.HTTP_201_CREATED, create.data)
        if "id" in create.data:
            request_id = create.data["id"]
        else:
            req = LeaveRequest.objects.filter(employee=self.employee).order_by("-created_at").first()
            self.assertIsNotNone(req, msg=create.data)
            request_id = str(req.id)
        submit = self.client.post(reverse("leave-request-submit", args=[request_id]), format="json")
        self.assertEqual(submit.status_code, status.HTTP_200_OK, submit.data)
        return request_id, submit.data

    def test_default_template_matches_old_chain(self):
        template = ensure_default_workflow_template()
        self.assertEqual(template.name, DEFAULT_WORKFLOW_NAME)
        self.assertTrue(template.is_org_default)
        self.assertFalse(template.auto_approve_after_sla)
        stages = list(template.stages.order_by("order"))
        specs = default_stage_specs()
        self.assertEqual(len(stages), 5)
        for stage, spec in zip(stages, specs):
            self.assertEqual(stage.approver_source, spec["approver_source"])
            self.assertEqual(stage.status_code, spec["status_code"])
            self.assertEqual(stage.order, spec["order"])

        request_id, data = self._submit_employee_request()
        self.assertEqual(data["status"], LeaveRequestStatus.PENDING_MANAGER)
        req = LeaveRequest.objects.get(pk=request_id)
        snapshot_codes = [s["status_code"] for s in req.workflow_snapshot["stages"]]
        self.assertEqual(
            snapshot_codes,
            [
                LeaveRequestStatus.PENDING_MANAGER,
                LeaveRequestStatus.PENDING_HR,
                LeaveRequestStatus.PENDING_ED,
            ],
        )

    def test_snapshot_ignores_later_template_edits(self):
        request_id, _ = self._submit_employee_request()
        template = LeaveWorkflowTemplate.objects.get(name=DEFAULT_WORKFLOW_NAME)
        self.client.force_authenticate(self.hr_user)
        patch = self.client.patch(
            reverse("leave-workflow-detail", args=[template.pk]),
            {
                "stages": [
                    {
                        "order": 1,
                        "approver_source": "HR",
                        "status_code": LeaveRequestStatus.PENDING_HR,
                        "skip_if_requester_roles": [],
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(patch.status_code, status.HTTP_200_OK, patch.data)

        self.client.force_authenticate(self.line_manager)
        approve = self.client.post(
            reverse("leave-request-approve", args=[request_id]),
            {"comment": "ok"},
            format="json",
        )
        self.assertEqual(approve.status_code, status.HTTP_200_OK, approve.data)
        self.assertEqual(approve.data["status"], LeaveRequestStatus.PENDING_HR)

    def test_delegate_can_approve(self):
        request_id, _ = self._submit_employee_request()
        today = timezone.localdate()
        ApproverDelegate.objects.create(
            user=self.line_manager,
            delegate=self.delegate,
            start_date=today,
            end_date=today + datetime.timedelta(days=7),
            is_active=True,
        )
        self.client.force_authenticate(self.delegate)
        approve = self.client.post(
            reverse("leave-request-approve", args=[request_id]),
            {"comment": "covering"},
            format="json",
        )
        self.assertEqual(approve.status_code, status.HTTP_200_OK, approve.data)
        self.assertEqual(approve.data["status"], LeaveRequestStatus.PENDING_HR)

    def test_employees_cannot_edit_workflows(self):
        template = ensure_default_workflow_template()
        self.client.force_authenticate(self.employee)
        create = self.client.post(
            self.workflows_url,
            {
                "name": "Rogue workflow",
                "is_active": True,
                "stages": [
                    {
                        "order": 1,
                        "approver_source": "HR",
                        "status_code": LeaveRequestStatus.PENDING_HR,
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(create.status_code, status.HTTP_403_FORBIDDEN)
        patch = self.client.patch(
            reverse("leave-workflow-detail", args=[template.pk]),
            {"name": "Hacked"},
            format="json",
        )
        self.assertEqual(patch.status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(self.hr_user)
        listed = self.client.get(self.workflows_url)
        self.assertEqual(listed.status_code, status.HTTP_200_OK)
        simulate = self.client.post(
            reverse("leave-workflow-simulate", args=[template.pk]),
            {"employee": str(self.employee.id), "leave_type": str(self.annual.id)},
            format="json",
        )
        self.assertEqual(simulate.status_code, status.HTTP_200_OK, simulate.data)
        self.assertEqual(simulate.data["first_status"], LeaveRequestStatus.PENDING_MANAGER)

    def test_hr_workflow_write_is_audited(self):
        self.client.force_authenticate(self.hr_user)
        create = self.client.post(
            self.workflows_url,
            {
                "name": "Annual-only chain",
                "is_active": True,
                "leave_type": str(self.annual.id),
                "stages": [
                    {
                        "order": 1,
                        "approver_source": "LINE_MANAGER",
                        "status_code": LeaveRequestStatus.PENDING_MANAGER,
                    },
                    {
                        "order": 2,
                        "approver_source": "EXECUTIVE_DIRECTOR",
                        "status_code": LeaveRequestStatus.PENDING_ED,
                    },
                ],
            },
            format="json",
        )
        self.assertEqual(create.status_code, status.HTTP_201_CREATED, create.data)
        self.assertTrue(
            LeaveSettingsAuditLog.objects.filter(
                object_type="LeaveWorkflowTemplate", action=SettingsAuditAction.CREATE
            ).exists()
        )

    def test_sla_beat_reminds_without_auto_approve(self):
        request_id, _ = self._submit_employee_request()
        req = LeaveRequest.objects.get(pk=request_id)
        snapshot = req.workflow_snapshot
        snapshot["stages"][0]["sla_hours"] = 1
        snapshot["auto_approve_after_sla"] = False
        req.workflow_snapshot = snapshot
        req.stage_entered_at = timezone.now() - datetime.timedelta(hours=2)
        req.sla_notified_at = None
        req.save(update_fields=["workflow_snapshot", "stage_entered_at", "sla_notified_at"])

        result = escalate_stale_leave_approvals()
        self.assertGreaterEqual(result["reminded"], 1)
        req.refresh_from_db()
        self.assertEqual(req.status, LeaveRequestStatus.PENDING_MANAGER)
        self.assertIsNotNone(req.sla_notified_at)
