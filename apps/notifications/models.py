import uuid

from django.conf import settings
from django.db import models


class NotificationType(models.TextChoices):
    LEAVE_SUBMITTED = "LEAVE_SUBMITTED", "Leave submitted"
    LEAVE_ACTION_REQUIRED = "LEAVE_ACTION_REQUIRED", "Leave action required"
    LEAVE_APPROVED = "LEAVE_APPROVED", "Leave approved"
    LEAVE_REJECTED = "LEAVE_REJECTED", "Leave rejected"


class Notification(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    title = models.CharField(max_length=255)
    body = models.TextField(blank=True)
    type = models.CharField(max_length=50, choices=NotificationType.choices)
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recipient", "-created_at"]),
            models.Index(fields=["recipient", "read_at"]),
        ]

    @property
    def is_read(self) -> bool:
        return self.read_at is not None
