import datetime

from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Department, Role, RoleName, UserRole
from apps.leave.models import (
    LeaveBalance,
    LeavePolicy,
    LeavePolicyStatus,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveSettingsAuditLog,
    LeaveType,
    PublicHoliday,
    SettingsAuditAction,
)
from apps.leave.services import (
    get_active_policy,
    get_annual_entitlement,
    WorkingDaysService,
)
from apps.leave.utils import calculate_working_days

from django.contrib.auth import get_user_model

User = get_user_model()


class LeaveSettingsSprint1Tests(APITestCase):
    def setUp(self):
        self.password = "testpass123"
        self.department = Department.objects.create(name="Engineering")
        self.hr_department, _ = Department.objects.get_or_create(
            name="Human Resources (HR)"
        )
        self.employee = self._create_user("employee-s1@test.com", [RoleName.EMPLOYEE], self.department)
        self.hr_user = self._create_user("hr-s1@test.com", [RoleName.HR], self.hr_department)
        self.annual = LeaveType.objects.get(code=LeaveType.Code.ANNUAL)
        self.types_url = reverse("leave-type-list")
        self.policies_url = reverse("leave-policy-list")

    def _create_user(self, email, roles, department=None):
        user = User.objects.create_user(email=email, password=self.password, department=department)
        for role_name in roles:
            role, _ = Role.objects.get_or_create(name=role_name)
            UserRole.objects.get_or_create(user=user, role=role)
        return user

    def test_seed_gives_every_leave_type_an_active_policy(self):
        for leave_type in LeaveType.objects.all():
            self.assertTrue(leave_type.code)
            policy = get_active_policy(leave_type)
            self.assertIsNotNone(policy, leave_type.name)
            self.assertEqual(policy.status, LeavePolicyStatus.ACTIVE)
            self.assertEqual(policy.annual_entitlement, leave_type.default_days)

    def test_employee_can_list_types_but_cannot_write(self):
        self.client.force_authenticate(user=self.employee)
        list_resp = self.client.get(self.types_url)
        self.assertEqual(list_resp.status_code, status.HTTP_200_OK)
        create_resp = self.client.post(
            self.types_url,
            {"name": "Compassionate", "default_days": 3, "code": "COMPASSIONATE"},
            format="json",
        )
        self.assertEqual(create_resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_hr_can_create_and_deactivate_leave_type(self):
        self.client.force_authenticate(user=self.hr_user)
        create_resp = self.client.post(
            self.types_url,
            {
                "name": "Study Leave",
                "code": "STUDY",
                "default_days": 5,
                "reason": "New statutory category",
            },
            format="json",
        )
        self.assertEqual(create_resp.status_code, status.HTTP_201_CREATED)
        type_id = create_resp.data["id"]
        self.assertEqual(create_resp.data["code"], "STUDY")
        self.assertTrue(
            LeaveSettingsAuditLog.objects.filter(
                object_id=type_id, action=SettingsAuditAction.CREATE
            ).exists()
        )

        deactivate = self.client.post(
            reverse("leave-type-deactivate", args=[type_id]),
            {"reason": "Not in use yet"},
            format="json",
        )
        self.assertEqual(deactivate.status_code, status.HTTP_200_OK)
        self.assertFalse(deactivate.data["is_active"])

    def test_cannot_delete_leave_type_with_policies(self):
        self.client.force_authenticate(user=self.hr_user)
        resp = self.client.delete(reverse("leave-type-detail", args=[self.annual.id]))
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_employee_cannot_write_policies(self):
        self.client.force_authenticate(user=self.employee)
        list_resp = self.client.get(self.policies_url)
        self.assertEqual(list_resp.status_code, status.HTTP_200_OK)
        create_resp = self.client.post(
            self.policies_url,
            {
                "name": "Hacked policy",
                "leave_type": str(self.annual.id),
                "annual_entitlement": 99,
            },
            format="json",
        )
        self.assertEqual(create_resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_hr_draft_publish_archive_and_audit(self):
        self.client.force_authenticate(user=self.hr_user)
        create_resp = self.client.post(
            self.policies_url,
            {
                "name": "Annual 2027",
                "leave_type": str(self.annual.id),
                "annual_entitlement": 25,
                "weekend_excluded": True,
                "public_holiday_excluded": False,
                "reason": "Increase entitlement",
            },
            format="json",
        )
        self.assertEqual(create_resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(create_resp.data["status"], LeavePolicyStatus.DRAFT)
        draft_id = create_resp.data["id"]

        patch_resp = self.client.patch(
            reverse("leave-policy-detail", args=[draft_id]),
            {"annual_entitlement": 26, "reason": "Fine-tune days"},
            format="json",
        )
        self.assertEqual(patch_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(patch_resp.data["annual_entitlement"], 26)

        publish_resp = self.client.post(
            reverse("leave-policy-publish", args=[draft_id]),
            {"reason": "Go live"},
            format="json",
        )
        self.assertEqual(publish_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(publish_resp.data["status"], LeavePolicyStatus.ACTIVE)
        self.assertGreaterEqual(publish_resp.data["version"], 1)

        self.assertEqual(
            LeavePolicy.objects.filter(
                leave_type=self.annual, status=LeavePolicyStatus.ACTIVE
            ).count(),
            1,
        )
        self.assertEqual(get_annual_entitlement(self.annual), 26)

        active_id = publish_resp.data["id"]
        locked = self.client.patch(
            reverse("leave-policy-detail", args=[active_id]),
            {"annual_entitlement": 30},
            format="json",
        )
        self.assertEqual(locked.status_code, status.HTTP_400_BAD_REQUEST)

        audit = self.client.get(reverse("leave-policy-audit-log", args=[active_id]))
        self.assertEqual(audit.status_code, status.HTTP_200_OK)
        self.assertTrue(any(row["action"] == SettingsAuditAction.PUBLISH for row in audit.data))

        archive = self.client.post(
            reverse("leave-policy-archive", args=[active_id]),
            {"reason": "Retire"},
            format="json",
        )
        self.assertEqual(archive.status_code, status.HTTP_200_OK)
        self.assertEqual(archive.data["status"], LeavePolicyStatus.ARCHIVED)

    def test_policy_entitlement_used_for_new_balances(self):
        policy = get_active_policy(self.annual)
        policy.annual_entitlement = 18
        # ORM update of ACTIVE is allowed for this unit assertion of resolver/allocation.
        LeavePolicy.objects.filter(pk=policy.pk).update(annual_entitlement=18)
        policy.refresh_from_db()
        self.assertEqual(get_annual_entitlement(self.annual), 18)

        new_user = self._create_user("alloc-s1@test.com", [RoleName.EMPLOYEE], self.department)
        balance = LeaveBalance.objects.get(
            employee=new_user,
            leave_type=self.annual,
            year=timezone.now().year,
        )
        self.assertEqual(balance.allocated_days, 18)

    def test_working_days_honor_policy_weekend_and_holiday_flags(self):
        start = datetime.date(2026, 3, 2)  # Mon
        end = datetime.date(2026, 3, 8)  # Sun
        PublicHoliday.objects.get_or_create(
            date=datetime.date(2026, 3, 4),
            defaults={"name": "Sprint1 Holiday", "is_recurring": False},
        )

        isolated = LeaveType.objects.create(
            name="Counted Days Leave",
            code="COUNTED_DAYS",
            default_days=10,
        )
        LeavePolicy.objects.create(
            leave_type=isolated,
            name="Default calendar",
            status=LeavePolicyStatus.ACTIVE,
            version=1,
            effective_from=start,
            annual_entitlement=10,
            weekend_excluded=True,
            public_holiday_excluded=True,
        )
        default_days = WorkingDaysService.calculate_working_days(start, end, leave_type=isolated)
        self.assertEqual(default_days, 4)  # Mon Tue Thu Fri (Wed holiday)

        custom_type = LeaveType.objects.create(
            name="Calendar Day Leave",
            code="CALENDAR_DAY",
            default_days=10,
        )
        LeavePolicy.objects.create(
            leave_type=custom_type,
            name="Include weekends and holidays",
            status=LeavePolicyStatus.ACTIVE,
            version=1,
            effective_from=start,
            annual_entitlement=10,
            weekend_excluded=False,
            public_holiday_excluded=False,
        )
        counted = WorkingDaysService.calculate_working_days(start, end, leave_type=custom_type)
        self.assertEqual(counted, 7)

        self.assertEqual(
            calculate_working_days(
                start, end, weekend_excluded=True, public_holiday_excluded=True
            ),
            4,
        )

    def test_historical_request_days_not_rewritten_on_policy_change(self):
        start = datetime.date(2026, 6, 8)
        end = datetime.date(2026, 6, 12)
        req = LeaveRequest.objects.create(
            employee=self.employee,
            leave_type=self.annual,
            start_date=start,
            end_date=end,
            status=LeaveRequestStatus.APPROVED,
        )
        original = req.total_working_days
        self.assertGreater(original, 0)

        policy = get_active_policy(self.annual)
        LeavePolicy.objects.filter(pk=policy.pk).update(
            weekend_excluded=False,
            public_holiday_excluded=False,
        )
        req.status = LeaveRequestStatus.APPROVED
        req.save(update_fields=["status", "updated_at"])
        req.refresh_from_db()
        self.assertEqual(req.total_working_days, original)

    def test_employee_cannot_read_policy_audit_log(self):
        policy = get_active_policy(self.annual)
        self.client.force_authenticate(user=self.employee)
        resp = self.client.get(reverse("leave-policy-audit-log", args=[policy.id]))
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
