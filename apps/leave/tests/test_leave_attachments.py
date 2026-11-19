import datetime
import os
from pathlib import Path

os.environ["AWS_STORAGE_BUCKET_NAME"] = ""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Department, Role, RoleName, UserRole
from apps.leave.models import (
    ApprovalAction,
    LeaveApprovalLog,
    LeaveAttachment,
    LeaveBalance,
    LeavePolicy,
    LeavePolicyStatus,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveType,
)


User = get_user_model()

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF"


def _monday_on_or_after(day: datetime.date) -> datetime.date:
    while day.weekday() != 0:
        day += datetime.timedelta(days=1)
    return day


class LeaveAttachmentApiTests(APITestCase):
    def setUp(self):
        self.password = "testpass123"
        self.current_year = timezone.now().year
        self.start = _monday_on_or_after(datetime.date(self.current_year + 1, 6, 1))
        self.end = self.start + datetime.timedelta(days=2)
        self.back_start = _monday_on_or_after(datetime.date(self.current_year - 1, 3, 2))
        self.back_end = self.back_start + datetime.timedelta(days=2)

        self.department = Department.objects.create(name="Engineering Attachments")
        self.other_department = Department.objects.create(name="Finance Attachments")
        self.hr_department, _ = Department.objects.get_or_create(
            name="Human Resources (HR)"
        )

        self.employee = self._create_user_with_roles(
            "employee.attach@test.com",
            [RoleName.EMPLOYEE],
            department=self.department,
        )
        self.employee.gender = "FEMALE"
        self.employee.save(update_fields=["gender", "updated_at"])

        self.line_manager = self._create_user_with_roles(
            "manager.attach@test.com",
            [RoleName.LINE_MANAGER],
            department=self.department,
        )
        self.stranger = self._create_user_with_roles(
            "stranger.attach@test.com",
            [RoleName.EMPLOYEE],
            department=self.other_department,
        )
        self.hr_user = self._create_user_with_roles(
            "hr.attach@test.com",
            [RoleName.HR],
            department=self.hr_department,
        )
        self.department.line_manager = self.line_manager
        self.department.save(update_fields=["line_manager", "updated_at"])

        self.annual = self._leave_type("Annual", 21)
        self.sick = self._leave_type("Sick", 10)
        self.maternity = self._leave_type("Maternity", 90)

        self._policy(self.annual, requires_attachment=False, entitlement=21)
        self._policy(self.sick, requires_attachment=True, entitlement=10)
        self._policy(self.maternity, requires_attachment=True, entitlement=90)

        for leave_type, days in (
            (self.annual, 21),
            (self.sick, 10),
            (self.maternity, 90),
        ):
            for year in (self.start.year, self.back_start.year):
                LeaveBalance.objects.update_or_create(
                    employee=self.employee,
                    leave_type=leave_type,
                    year=year,
                    defaults={"allocated_days": days, "used_days": 0},
                )

        self.list_url = reverse("leave-request-list")
        self.create_and_submit_url = reverse("leave-request-create-and-submit")
        self.reconcile_url = reverse("leave-request-reconcile")
        self.bulk_url = reverse("leave-request-bulk-reconcile")

        self.media_dir = Path(settings.BASE_DIR) / "media" / "leave-attachments"
        self._before = set()
        if self.media_dir.exists():
            self._before = {path.name for path in self.media_dir.iterdir()}

    def tearDown(self):
        if not self.media_dir.exists():
            return
        for path in self.media_dir.iterdir():
            if path.name not in self._before and path.is_file():
                path.unlink()

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

    def _leave_type(self, name, days):
        leave_type, _ = LeaveType.objects.get_or_create(
            name=name,
            defaults={"default_days": days},
        )
        return leave_type

    def _policy(self, leave_type, *, requires_attachment, entitlement):
        # Seed migrations already publish an active policy per leave type.
        # Update that row so the test controls the resolved policy.
        policy = (
            LeavePolicy.objects.filter(
                leave_type=leave_type,
                status=LeavePolicyStatus.ACTIVE,
            )
            .order_by("-version", "-effective_from", "-created_at")
            .first()
        )
        if policy is None:
            return LeavePolicy.objects.create(
                leave_type=leave_type,
                status=LeavePolicyStatus.ACTIVE,
                version=1,
                annual_entitlement=entitlement,
                requires_attachment=requires_attachment,
                reliever_required=False,
                allow_backdated=True,
            )
        policy.requires_attachment = requires_attachment
        policy.reliever_required = False
        policy.allow_backdated = True
        policy.annual_entitlement = entitlement
        # Cover both future submissions and backdated reconciliation.
        policy.effective_from = None
        policy.effective_to = None
        policy.save(
            update_fields=[
                "requires_attachment",
                "reliever_required",
                "allow_backdated",
                "annual_entitlement",
                "effective_from",
                "effective_to",
                "updated_at",
            ]
        )
        return policy

    def _auth(self, user):
        self.client.force_authenticate(user=user)

    def _payload(self, leave_type, **overrides):
        payload = {
            "leave_type": str(leave_type.id),
            "start_date": self.start.isoformat(),
            "end_date": self.end.isoformat(),
            "reason": "Need the time",
            "is_emergency": False,
        }
        payload.update(overrides)
        return payload

    def _create_draft(self, leave_type, **overrides):
        self._auth(self.employee)
        response = self.client.post(
            self.list_url,
            self._payload(leave_type, **overrides),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return LeaveRequest.objects.get(id=response.data["id"])

    def _submit(self, leave_request):
        self._auth(self.employee)
        return self.client.post(
            reverse("leave-request-submit", args=[leave_request.id]),
            format="json",
        )

    def _pdf(self, name="note.pdf", content=PDF_BYTES, content_type="application/pdf"):
        return SimpleUploadedFile(name, content, content_type=content_type)

    def _upload(self, user, leave_request, upload=None):
        self._auth(user)
        return self.client.post(
            reverse("leave-request-attachments", args=[leave_request.id]),
            {"file": upload or self._pdf()},
            format="multipart",
        )

    def _response_bytes(self, response):
        if getattr(response, "streaming", False):
            return b"".join(response.streaming_content)
        return response.content

    def test_sick_and_maternity_submit_without_file_fails(self):
        sick = self._create_draft(self.sick)
        sick_submit = self._submit(sick)
        self.assertEqual(sick_submit.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("attachments", sick_submit.data)
        sick.refresh_from_db()
        self.assertEqual(sick.status, LeaveRequestStatus.DRAFT)

        self._auth(self.employee)
        sick_direct = self.client.post(
            self.create_and_submit_url,
            self._payload(
                self.sick,
                start_date=(self.start + datetime.timedelta(days=7)).isoformat(),
                end_date=(self.end + datetime.timedelta(days=7)).isoformat(),
            ),
            format="json",
        )
        self.assertEqual(sick_direct.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("attachments", sick_direct.data)

        maternity = self._create_draft(
            self.maternity,
            start_date=(self.start + datetime.timedelta(days=14)).isoformat(),
            end_date=(self.end + datetime.timedelta(days=14)).isoformat(),
        )
        maternity_submit = self._submit(maternity)
        self.assertEqual(maternity_submit.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("attachments", maternity_submit.data)

    def test_emergency_sick_submits_without_file_and_emergency_maternity_does_not(self):
        sick = self._create_draft(self.sick, is_emergency=True)
        sick_submit = self._submit(sick)
        self.assertEqual(sick_submit.status_code, status.HTTP_200_OK, sick_submit.data)
        sick.refresh_from_db()
        self.assertNotEqual(sick.status, LeaveRequestStatus.DRAFT)
        self.assertTrue(sick.is_emergency)

        maternity = self._create_draft(
            self.maternity,
            is_emergency=True,
            start_date=(self.start + datetime.timedelta(days=14)).isoformat(),
            end_date=(self.end + datetime.timedelta(days=14)).isoformat(),
        )
        maternity_submit = self._submit(maternity)
        self.assertEqual(maternity_submit.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("attachments", maternity_submit.data)
        maternity.refresh_from_db()
        self.assertEqual(maternity.status, LeaveRequestStatus.DRAFT)

    def test_annual_submits_without_file(self):
        self._auth(self.employee)
        response = self.client.post(
            self.create_and_submit_url,
            self._payload(self.annual),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertNotEqual(response.data["status"], LeaveRequestStatus.DRAFT)

    def test_visibility_and_owner_permissions(self):
        draft = self._create_draft(self.sick)
        uploaded = self._upload(self.employee, draft)
        self.assertEqual(uploaded.status_code, status.HTTP_201_CREATED, uploaded.data)
        attachment_id = uploaded.data["id"]
        self.assertNotIn("url", uploaded.data)
        self.assertNotIn("file", uploaded.data)
        stored = LeaveAttachment.objects.get(id=attachment_id)
        self.assertNotIn("note.pdf", stored.file.name)
        self.assertTrue(stored.file.name.endswith(".pdf"))
        stored_path = stored.file.path
        self.assertTrue(os.path.exists(stored_path))

        submit = self._submit(draft)
        self.assertEqual(submit.status_code, status.HTTP_200_OK, submit.data)

        self._auth(self.employee)
        delete_after_submit = self.client.delete(
            reverse(
                "leave-request-delete-attachment",
                kwargs={"pk": draft.id, "attachment_id": attachment_id},
            )
        )
        self.assertEqual(delete_after_submit.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(os.path.exists(stored_path))

        extra = self._upload(self.employee, draft, self._pdf("later.pdf"))
        self.assertEqual(extra.status_code, status.HTTP_403_FORBIDDEN)

        list_url = reverse("leave-request-attachments", args=[draft.id])
        link_url = reverse(
            "leave-request-attachment-link",
            kwargs={"pk": draft.id, "attachment_id": attachment_id},
        )

        self._auth(self.stranger)
        stranger_list = self.client.get(list_url)
        stranger_link = self.client.post(
            link_url, {"disposition": "inline"}, format="json"
        )
        self.assertEqual(stranger_list.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(stranger_link.status_code, status.HTTP_404_NOT_FOUND)

        self._auth(self.line_manager)
        approver_list = self.client.get(list_url)
        self.assertEqual(approver_list.status_code, status.HTTP_200_OK)
        self.assertEqual(len(approver_list.data), 1)
        self.assertEqual(approver_list.data[0]["original_filename"], "note.pdf")

        approver_link = self.client.post(
            link_url, {"disposition": "inline"}, format="json"
        )
        self.assertEqual(approver_link.status_code, status.HTTP_200_OK)
        self.assertIn(PDF_BYTES, self._response_bytes(approver_link))
        self.assertIn("inline", approver_link["Content-Disposition"])
        self.assertTrue(approver_link["Content-Type"].startswith("application/pdf"))

        download = self.client.post(
            link_url, {"disposition": "attachment"}, format="json"
        )
        self.assertEqual(download.status_code, status.HTTP_200_OK)
        self.assertIn("attachment", download["Content-Disposition"])

    def test_owner_can_add_during_pending_emergency_sick_only(self):
        emergency = self._create_draft(self.sick, is_emergency=True)
        self.assertEqual(self._submit(emergency).status_code, status.HTTP_200_OK)
        added = self._upload(self.employee, emergency)
        self.assertEqual(added.status_code, status.HTTP_201_CREATED, added.data)
        removed = self.client.delete(
            reverse(
                "leave-request-delete-attachment",
                kwargs={"pk": emergency.id, "attachment_id": added.data["id"]},
            )
        )
        self.assertEqual(removed.status_code, status.HTTP_403_FORBIDDEN)

        normal = self._create_draft(
            self.sick,
            start_date=(self.start + datetime.timedelta(days=21)).isoformat(),
            end_date=(self.end + datetime.timedelta(days=21)).isoformat(),
        )
        first = self._upload(self.employee, normal)
        self.assertEqual(first.status_code, status.HTTP_201_CREATED, first.data)
        self.assertEqual(self._submit(normal).status_code, status.HTTP_200_OK)
        second = self._upload(self.employee, normal, self._pdf("second.pdf"))
        self.assertEqual(second.status_code, status.HTTP_403_FORBIDDEN)

    def test_bad_content_type_and_oversize_are_rejected(self):
        draft = self._create_draft(self.annual)
        bad_type = self._upload(
            self.employee,
            draft,
            SimpleUploadedFile("notes.txt", b"hello", content_type="text/plain"),
        )
        self.assertEqual(bad_type.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("file", bad_type.data)

        mismatched = self._upload(
            self.employee,
            draft,
            SimpleUploadedFile("photo.pdf", b"\x89PNG\r\n\x1a\n", content_type="application/pdf"),
        )
        self.assertEqual(mismatched.status_code, status.HTTP_400_BAD_REQUEST)

        oversize = self._upload(
            self.employee,
            draft,
            SimpleUploadedFile(
                "big.pdf",
                b"x" * (5 * 1024 * 1024 + 1),
                content_type="application/pdf",
            ),
        )
        self.assertEqual(oversize.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(LeaveAttachment.objects.filter(request=draft).count(), 0)

    def test_delete_removes_the_stored_file(self):
        draft = self._create_draft(self.annual)
        uploaded = self._upload(self.employee, draft)
        self.assertEqual(uploaded.status_code, status.HTTP_201_CREATED, uploaded.data)
        attachment = LeaveAttachment.objects.get(id=uploaded.data["id"])
        stored_path = attachment.file.path
        self.assertTrue(os.path.exists(stored_path))

        self._auth(self.employee)
        response = self.client.delete(
            reverse(
                "leave-request-delete-attachment",
                kwargs={"pk": draft.id, "attachment_id": attachment.id},
            )
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(LeaveAttachment.objects.filter(id=attachment.id).exists())
        self.assertFalse(os.path.exists(stored_path))

    def test_hr_can_add_to_an_approved_request(self):
        draft = self._create_draft(self.annual)
        draft.status = LeaveRequestStatus.APPROVED
        draft.save(update_fields=["status", "updated_at"])
        uploaded = self._upload(self.hr_user, draft)
        self.assertEqual(uploaded.status_code, status.HTTP_201_CREATED, uploaded.data)

    def test_reconcile_requires_override_for_sick_and_maternity_only(self):
        self._auth(self.hr_user)
        sick_missing = self.client.post(
            self.reconcile_url,
            self._reconcile_payload(self.sick),
            format="json",
        )
        self.assertEqual(sick_missing.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("attachment_override_reason", sick_missing.data)

        note = "Confirmed with the line manager"
        reason = "Clinic note will be filed tomorrow"
        sick_ok = self.client.post(
            self.reconcile_url,
            self._reconcile_payload(
                self.sick,
                reconciliation_note=note,
                attachment_override_reason=reason,
            ),
            format="json",
        )
        self.assertEqual(sick_ok.status_code, status.HTTP_201_CREATED, sick_ok.data)
        leave_request = LeaveRequest.objects.get(id=sick_ok.data["id"])
        self.assertEqual(leave_request.reconciliation_note, note)
        log = LeaveApprovalLog.objects.get(
            leave_request=leave_request,
            action=ApprovalAction.RECONCILE,
        )
        self.assertIn(note, log.comment)
        self.assertIn(reason, log.comment)

        maternity_missing = self.client.post(
            self.reconcile_url,
            self._reconcile_payload(
                self.maternity,
                start_date=(self.back_start + datetime.timedelta(days=14)).isoformat(),
                end_date=(self.back_end + datetime.timedelta(days=14)).isoformat(),
            ),
            format="json",
        )
        self.assertEqual(maternity_missing.status_code, status.HTTP_400_BAD_REQUEST)

        maternity_ok = self.client.post(
            self.reconcile_url,
            self._reconcile_payload(
                self.maternity,
                start_date=(self.back_start + datetime.timedelta(days=14)).isoformat(),
                end_date=(self.back_end + datetime.timedelta(days=14)).isoformat(),
                attachment_override_reason="Birth record held by HR",
            ),
            format="json",
        )
        self.assertEqual(maternity_ok.status_code, status.HTTP_201_CREATED, maternity_ok.data)

        annual = self.client.post(
            self.reconcile_url,
            self._reconcile_payload(
                self.annual,
                start_date=(self.back_start + datetime.timedelta(days=28)).isoformat(),
                end_date=(self.back_end + datetime.timedelta(days=28)).isoformat(),
            ),
            format="json",
        )
        self.assertEqual(annual.status_code, status.HTTP_201_CREATED, annual.data)

    def test_bulk_reconcile_still_works_when_policy_requires_a_file(self):
        self.annual.policies.filter(status=LeavePolicyStatus.ACTIVE).update(
            requires_attachment=True
        )
        colleague = self._create_user_with_roles(
            "colleague.attach@test.com",
            [RoleName.EMPLOYEE],
            department=self.department,
        )
        LeaveBalance.objects.update_or_create(
            employee=colleague,
            leave_type=self.annual,
            year=self.back_start.year,
            defaults={"allocated_days": 21, "used_days": 0},
        )
        alt_start = self.back_start + datetime.timedelta(days=35)
        alt_end = alt_start + datetime.timedelta(days=2)
        self._auth(self.hr_user)
        response = self.client.post(
            self.bulk_url,
            {
                "rows": [
                    {
                        "employee": str(self.employee.id),
                        "leave_type": str(self.annual.id),
                        "start_date": self.back_start.isoformat(),
                        "end_date": self.back_end.isoformat(),
                        "reconciliation_note": "Bulk row one",
                    },
                    {
                        "employee": str(colleague.id),
                        "leave_type": str(self.annual.id),
                        "start_date": alt_start.isoformat(),
                        "end_date": alt_end.isoformat(),
                        "reconciliation_note": "Bulk row two",
                    },
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["created_count"], 2)
        self.assertEqual(response.data["errors"], [])

    def _reconcile_payload(self, leave_type, **overrides):
        payload = {
            "employee": str(self.employee.id),
            "leave_type": str(leave_type.id),
            "start_date": self.back_start.isoformat(),
            "end_date": self.back_end.isoformat(),
            "reason": "Absent without a request",
            "reconciliation_note": "Recorded from the attendance sheet",
        }
        payload.update(overrides)
        return payload
