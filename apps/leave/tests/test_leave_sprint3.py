import datetime

from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import (
    ContractType,
    Department,
    Role,
    RoleName,
    Team,
    Unit,
    UserRole,
)
from apps.leave.models import (
    AssignmentScopeType,
    LeavePolicy,
    LeavePolicyAssignment,
    LeavePolicyStatus,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveSettingsAuditLog,
    LeaveType,
    SettingsAuditAction,
)
from apps.leave.services import (
    apply_policy_snapshot,
    clone_leave_policy,
    get_active_policy,
    publish_leave_policy,
    resolve_leave_policy,
)

from django.contrib.auth import get_user_model

User = get_user_model()


class LeaveSprint3AssignmentTests(APITestCase):
    def setUp(self):
        self.password = "testpass123"
        self.eng = Department.objects.create(name="Sprint3 Engineering")
        self.sales = Department.objects.create(name="Sprint3 Sales")
        self.hr_department, _ = Department.objects.get_or_create(
            name="Human Resources (HR)"
        )
        self.unit = Unit.objects.create(name="Platform", department=self.eng)
        self.team = Team.objects.create(name="Core", unit=self.unit)

        self.eng_employee = self._create_user(
            "eng-s3@test.com", [RoleName.EMPLOYEE], self.eng
        )
        self.eng_employee.unit = self.unit
        self.eng_employee.team = self.team
        self.eng_employee.contract_type = ContractType.PERMANENT
        self.eng_employee.save()

        self.sales_employee = self._create_user(
            "sales-s3@test.com", [RoleName.EMPLOYEE], self.sales
        )
        self.sales_employee.contract_type = ContractType.CONTRACT
        self.sales_employee.save()

        self.intern_dept = Department.objects.create(name="Sprint3 Interns")
        self.intern = self._create_user(
            "intern-s3@test.com", [RoleName.EMPLOYEE], self.intern_dept
        )
        self.intern.contract_type = ContractType.INTERN
        self.intern.save()

        self.hr_user = self._create_user(
            "hr-s3@test.com", [RoleName.HR], self.hr_department
        )
        self.annual = LeaveType.objects.get(code=LeaveType.Code.ANNUAL)
        self.default_policy = get_active_policy(self.annual)
        self.assertIsNotNone(self.default_policy)
        self.today = timezone.localdate()
        self.assignments_url = reverse("leave-policy-assignment-list")
        self.resolution_url = reverse("leave-policy-resolution")

    def _create_user(self, email, roles, department=None):
        user = User.objects.create_user(
            email=email, password=self.password, department=department
        )
        for role_name in roles:
            role, _ = Role.objects.get_or_create(name=role_name)
            UserRole.objects.get_or_create(user=user, role=role)
        return user

    def _publish_extra_policy(self, name, entitlement):
        draft = clone_leave_policy(self.default_policy, actor=self.hr_user)
        draft.name = name
        draft.annual_entitlement = entitlement
        draft.save()
        return publish_leave_policy(
            draft,
            actor=self.hr_user,
            reason="Sprint 3 extra policy",
            keep_existing_active=True,
        )

    def test_fallback_when_no_assignment(self):
        resolved = resolve_leave_policy(self.eng_employee, self.annual, self.today)
        self.assertEqual(resolved.source, "fallback")
        self.assertEqual(resolved.policy.pk, self.default_policy.pk)
        self.assertIsNone(resolved.assignment_scope)

    def test_employee_assignment_wins_over_department(self):
        dept_policy = self._publish_extra_policy("Eng annual", 30)
        emp_policy = self._publish_extra_policy("Person annual", 40)
        LeavePolicyAssignment.objects.create(
            policy=dept_policy,
            scope_type=AssignmentScopeType.DEPARTMENT,
            scope_id=str(self.eng.id),
            priority=5,
            effective_from=self.today,
            is_active=True,
        )
        LeavePolicyAssignment.objects.create(
            policy=emp_policy,
            scope_type=AssignmentScopeType.EMPLOYEE,
            scope_id=str(self.eng_employee.id),
            employee=self.eng_employee,
            priority=0,
            effective_from=self.today,
            is_active=True,
        )
        resolved = resolve_leave_policy(self.eng_employee, self.annual, self.today)
        self.assertEqual(resolved.source, "assignment")
        self.assertEqual(resolved.assignment_scope, AssignmentScopeType.EMPLOYEE)
        self.assertEqual(resolved.policy.pk, emp_policy.pk)
        sales = resolve_leave_policy(self.sales_employee, self.annual, self.today)
        self.assertEqual(sales.policy.pk, self.default_policy.pk)

    def test_team_wins_over_department_and_priority_breaks_ties(self):
        team_policy = self._publish_extra_policy("Team annual", 28)
        dept_policy = self._publish_extra_policy("Dept annual", 22)
        LeavePolicyAssignment.objects.create(
            policy=dept_policy,
            scope_type=AssignmentScopeType.DEPARTMENT,
            scope_id=str(self.eng.id),
            priority=99,
            effective_from=self.today,
            is_active=True,
        )
        LeavePolicyAssignment.objects.create(
            policy=team_policy,
            scope_type=AssignmentScopeType.TEAM,
            scope_id=str(self.team.id),
            priority=0,
            effective_from=self.today,
            is_active=True,
        )
        resolved = resolve_leave_policy(self.eng_employee, self.annual, self.today)
        self.assertEqual(resolved.policy.pk, team_policy.pk)

        low = self._publish_extra_policy("Low prio intern", 10)
        high = self._publish_extra_policy("High prio intern", 12)
        LeavePolicyAssignment.objects.create(
            policy=low,
            scope_type=AssignmentScopeType.EMPLOYMENT_TYPE,
            scope_id=ContractType.INTERN,
            priority=1,
            effective_from=self.today,
            is_active=True,
        )
        LeavePolicyAssignment.objects.create(
            policy=high,
            scope_type=AssignmentScopeType.EMPLOYMENT_TYPE,
            scope_id=ContractType.INTERN,
            priority=8,
            effective_from=self.today + datetime.timedelta(days=400),
            effective_to=self.today + datetime.timedelta(days=500),
            is_active=True,
        )
        intern_now = resolve_leave_policy(self.intern, self.annual, self.today)
        self.assertEqual(intern_now.policy.pk, low.pk)

    def test_effective_dates(self):
        future_policy = self._publish_extra_policy("Future eng", 33)
        start = self.today + datetime.timedelta(days=30)
        LeavePolicyAssignment.objects.create(
            policy=future_policy,
            scope_type=AssignmentScopeType.DEPARTMENT,
            scope_id=str(self.eng.id),
            priority=0,
            effective_from=start,
            is_active=True,
        )
        before = resolve_leave_policy(self.eng_employee, self.annual, self.today)
        self.assertEqual(before.policy.pk, self.default_policy.pk)
        after = resolve_leave_policy(self.eng_employee, self.annual, start)
        self.assertEqual(after.policy.pk, future_policy.pk)

    def test_conflicting_overlap_rejected(self):
        extra = self._publish_extra_policy("Conflict policy", 19)
        self.client.force_authenticate(self.hr_user)
        payload = {
            "policy": str(extra.id),
            "scope_type": AssignmentScopeType.DEPARTMENT,
            "scope_id": str(self.eng.id),
            "priority": 0,
            "effective_from": self.today.isoformat(),
            "reason": "Assign eng",
        }
        first = self.client.post(self.assignments_url, payload, format="json")
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        second = self.client.post(self.assignments_url, payload, format="json")
        self.assertEqual(second.status_code, status.HTTP_400_BAD_REQUEST)

    def test_employee_cannot_write_assignments(self):
        self.client.force_authenticate(self.eng_employee)
        list_resp = self.client.get(self.assignments_url)
        self.assertEqual(list_resp.status_code, status.HTTP_200_OK)
        extra = self._publish_extra_policy("No write", 18)
        create_resp = self.client.post(
            self.assignments_url,
            {
                "policy": str(extra.id),
                "scope_type": AssignmentScopeType.ORGANIZATION,
                "effective_from": self.today.isoformat(),
            },
            format="json",
        )
        self.assertEqual(create_resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_hr_assignment_writes_audit(self):
        extra = self._publish_extra_policy("Audited assign", 21)
        self.client.force_authenticate(self.hr_user)
        resp = self.client.post(
            self.assignments_url,
            {
                "policy": str(extra.id),
                "scope_type": AssignmentScopeType.DEPARTMENT,
                "scope_id": str(self.sales.id),
                "effective_from": self.today.isoformat(),
                "reason": "Sales pack",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            LeaveSettingsAuditLog.objects.filter(
                object_id=resp.data["id"],
                object_type="LeavePolicyAssignment",
                action=SettingsAuditAction.CREATE,
            ).exists()
        )

    def test_resolution_endpoint_and_permissions(self):
        extra = self._publish_extra_policy("Sales pack", 15)
        LeavePolicyAssignment.objects.create(
            policy=extra,
            scope_type=AssignmentScopeType.DEPARTMENT,
            scope_id=str(self.sales.id),
            priority=0,
            effective_from=self.today,
            is_active=True,
        )
        self.client.force_authenticate(self.sales_employee)
        own = self.client.get(
            self.resolution_url,
            {"leave_type": str(self.annual.id), "date": self.today.isoformat()},
        )
        self.assertEqual(own.status_code, status.HTTP_200_OK)
        self.assertEqual(own.data["resolved_policy"]["id"], str(extra.id))
        self.assertEqual(own.data["assignment_scope"], AssignmentScopeType.DEPARTMENT)

        other = self.client.get(
            self.resolution_url,
            {
                "leave_type": str(self.annual.id),
                "employee": str(self.eng_employee.id),
            },
        )
        self.assertEqual(other.status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(self.hr_user)
        hr = self.client.get(
            self.resolution_url,
            {
                "leave_type": str(self.annual.id),
                "employee": str(self.sales_employee.id),
                "date": self.today.isoformat(),
            },
        )
        self.assertEqual(hr.status_code, status.HTTP_200_OK)
        self.assertEqual(hr.data["source"], "assignment")

    def test_impact_preview(self):
        extra = self._publish_extra_policy("Sales impact", 16)
        LeavePolicyAssignment.objects.create(
            policy=extra,
            scope_type=AssignmentScopeType.DEPARTMENT,
            scope_id=str(self.sales.id),
            priority=0,
            effective_from=self.today,
            is_active=True,
        )
        self.client.force_authenticate(self.hr_user)
        url = reverse("leave-policy-impact-preview", args=[extra.id])
        resp = self.client.get(url, {"date": self.today.isoformat()})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        emails = {row["email"] for row in resp.data["employees"]}
        self.assertIn(self.sales_employee.email, emails)
        self.assertNotIn(self.eng_employee.email, emails)

        self.client.force_authenticate(self.eng_employee)
        denied = self.client.get(url)
        self.assertEqual(denied.status_code, status.HTTP_403_FORBIDDEN)

    def test_snapshot_on_request_survives_assignment_change(self):
        extra = self._publish_extra_policy("Snap policy", 27)
        extra.weekend_excluded = False
        extra.public_holiday_excluded = False
        LeavePolicy.objects.filter(pk=extra.pk).update(
            weekend_excluded=False, public_holiday_excluded=False
        )
        extra.refresh_from_db()
        LeavePolicyAssignment.objects.create(
            policy=extra,
            scope_type=AssignmentScopeType.EMPLOYEE,
            scope_id=str(self.eng_employee.id),
            employee=self.eng_employee,
            priority=0,
            effective_from=self.today,
            is_active=True,
        )
        start = datetime.date(self.today.year + 1, 6, 7)
        while start.weekday() >= 5:
            start += datetime.timedelta(days=1)
        end = start + datetime.timedelta(days=4)
        req = LeaveRequest(
            employee=self.eng_employee,
            leave_type=self.annual,
            start_date=start,
            end_date=end,
            status=LeaveRequestStatus.DRAFT,
        )
        apply_policy_snapshot(req)
        req.status = LeaveRequestStatus.PENDING_MANAGER
        req.save()
        self.assertEqual(req.policy_id, extra.pk)
        self.assertEqual(req.policy_version, extra.version)
        original_days = req.total_working_days

        LeavePolicyAssignment.objects.filter(employee=self.eng_employee).update(
            is_active=False
        )
        req.status = LeaveRequestStatus.APPROVED
        req.save(update_fields=["status", "updated_at"])
        req.refresh_from_db()
        self.assertEqual(req.total_working_days, original_days)
        self.assertEqual(req.policy_id, extra.pk)

    def test_get_active_policy_with_employee_uses_assignment(self):
        extra = self._publish_extra_policy("Named", 31)
        LeavePolicyAssignment.objects.create(
            policy=extra,
            scope_type=AssignmentScopeType.DEPARTMENT,
            scope_id=str(self.eng.id),
            priority=0,
            effective_from=self.today,
            is_active=True,
        )
        self.assertEqual(
            get_active_policy(
                self.annual, on_date=self.today, employee=self.eng_employee
            ).pk,
            extra.pk,
        )
        self.assertEqual(
            get_active_policy(self.annual, on_date=self.today).pk,
            self.default_policy.pk,
        )

    def test_default_publish_still_archives_unassigned_active(self):
        self.client.force_authenticate(self.hr_user)
        create_resp = self.client.post(
            reverse("leave-policy-list"),
            {
                "name": "Replacement annual",
                "leave_type": str(self.annual.id),
                "annual_entitlement": 24,
            },
            format="json",
        )
        self.assertEqual(create_resp.status_code, status.HTTP_201_CREATED)
        publish_resp = self.client.post(
            reverse("leave-policy-publish", args=[create_resp.data["id"]]),
            {"reason": "Replace default"},
            format="json",
        )
        self.assertEqual(publish_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(
            LeavePolicy.objects.filter(
                leave_type=self.annual, status=LeavePolicyStatus.ACTIVE
            ).count(),
            1,
        )
