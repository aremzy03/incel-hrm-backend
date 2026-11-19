"""Upload rules and permission checks for leave attachments.

Submission and reconcile policy checks live in services.py. This module
validates the file itself and who may add or delete a row.
"""

import os

from django.db import transaction
from rest_framework.exceptions import ValidationError

from apps.accounts.models import RoleName

from . import messages as leave_messages
from .models import LeaveAttachment, LeaveRequest, LeaveRequestStatus, LeaveType
from .services import PENDING_HOLD_STATUSES, _leave_type_code

MAX_ATTACHMENTS = 5
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
ALLOWED_CONTENT_TYPES = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def _normalize_content_type(value) -> str:
    ct = (value or "").split(";")[0].strip().lower()
    if ct == "image/jpg":
        return "image/jpeg"
    return ct


def _peek(uploaded, size: int) -> bytes:
    uploaded.seek(0)
    header = uploaded.read(size) or b""
    uploaded.seek(0)
    return header


def _matches_content_type(content_type: str, header: bytes) -> bool:
    if content_type == "application/pdf":
        return header.startswith(b"%PDF")
    if content_type == "image/jpeg":
        return header.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/webp":
        return len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP"
    return False


def safe_original_filename(name: str) -> str:
    cleaned = os.path.basename((name or "").replace("\\", "/"))
    cleaned = cleaned.replace("\r", "").replace("\n", "").strip()
    return (cleaned or "attachment")[:255]


def validate_leave_attachment_file(uploaded):
    """Return (content_type, size) or raise ValidationError."""
    if uploaded is None:
        raise ValidationError({"file": "Choose a file to upload."})
    size = getattr(uploaded, "size", None)
    if not size:
        raise ValidationError({"file": leave_messages.attachment_empty()})
    if size > MAX_ATTACHMENT_BYTES:
        raise ValidationError({"file": leave_messages.attachment_too_large()})
    content_type = _normalize_content_type(getattr(uploaded, "content_type", ""))
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise ValidationError({"file": leave_messages.attachment_type_rejected()})
    if not _matches_content_type(content_type, _peek(uploaded, 16)):
        raise ValidationError({"file": leave_messages.attachment_type_rejected()})
    return content_type, size


def save_leave_attachment(leave_request, uploaded, user) -> LeaveAttachment:
    """Validate and store one attachment. Enforces the 5-file cap."""
    content_type, size = validate_leave_attachment_file(uploaded)
    original = safe_original_filename(getattr(uploaded, "name", ""))
    ext = ALLOWED_CONTENT_TYPES[content_type]
    uploaded.name = f"upload{ext}"

    with transaction.atomic():
        LeaveRequest.objects.select_for_update().get(pk=leave_request.pk)
        if LeaveAttachment.objects.filter(request=leave_request).count() >= MAX_ATTACHMENTS:
            raise ValidationError({"file": leave_messages.attachment_limit()})
        attachment = LeaveAttachment(
            request=leave_request,
            original_filename=original,
            content_type=content_type,
            size=size,
            uploaded_by=user,
        )
        attachment.file = uploaded
        attachment.save()
    return attachment


def user_can_add_leave_attachment(user, leave_request) -> bool:
    """HR anytime. Owner on a draft. Owner on pending emergency sick (add only)."""
    if user.has_role(RoleName.HR):
        return True
    if leave_request.employee_id != user.pk:
        return False
    if leave_request.status == LeaveRequestStatus.DRAFT:
        return True
    return bool(
        leave_request.status in PENDING_HOLD_STATUSES
        and leave_request.is_emergency
        and _leave_type_code(leave_request.leave_type) == LeaveType.Code.SICK
    )


def user_can_delete_leave_attachment(user, leave_request) -> bool:
    """Only the owner, and only while the request is still a draft."""
    return (
        leave_request.employee_id == user.pk
        and leave_request.status == LeaveRequestStatus.DRAFT
    )
