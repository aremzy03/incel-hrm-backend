from django.contrib.auth import get_user_model
from rest_framework import serializers

from .models import (
    LeaveApprovalLog,
    LeaveBalance,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveType,
)
from .services import WorkingDaysService

User = get_user_model()


# ---------------------------------------------------------------------------
# Nested helpers
# ---------------------------------------------------------------------------

class _EmployeeMinimalSerializer(serializers.ModelSerializer):
    """Lightweight user representation used inside read serializers."""

    class Meta:
        model = User
        fields = ("id", "email", "first_name", "last_name")
        read_only_fields = fields


# ---------------------------------------------------------------------------
# LeaveType
# ---------------------------------------------------------------------------

class LeaveTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeaveType
        fields = ("id", "name", "description", "default_days", "created_at", "updated_at")
        read_only_fields = fields


# ---------------------------------------------------------------------------
# LeaveBalance
# ---------------------------------------------------------------------------

class LeaveBalanceSerializer(serializers.ModelSerializer):
    leave_type = LeaveTypeSerializer(read_only=True)
    remaining_days = serializers.IntegerField(read_only=True)

    class Meta:
        model = LeaveBalance
        fields = (
            "id",
            "leave_type",
            "year",
            "allocated_days",
            "used_days",
            "remaining_days",
        )
        read_only_fields = fields


# ---------------------------------------------------------------------------
# LeaveRequest — write
# ---------------------------------------------------------------------------

class LeaveRequestCreateSerializer(serializers.ModelSerializer):
    """
    Used for POST (create) and PATCH (update) of leave requests.

    Validation pipeline:
      1. start_date < end_date
      2. WorkingDaysService.check_overlapping_leave()
      3. WorkingDaysService.validate_leave_balance()

    On create():
      - total_working_days is computed via WorkingDaysService.calculate_working_days()
      - status is set to DRAFT
      - employee is taken from request.user (passed via serializer context)
    """

    class Meta:
        model = LeaveRequest
        fields = ("leave_type", "start_date", "end_date", "reason", "is_emergency")

    def validate(self, attrs):
        start_date = attrs.get("start_date")
        end_date = attrs.get("end_date")

        if start_date and end_date:
            if start_date >= end_date:
                raise serializers.ValidationError(
                    {"end_date": "end_date must be after start_date."}
                )

        employee = self.context["request"].user
        leave_type = attrs.get("leave_type")

        # Determine exclude_id when updating an existing request.
        exclude_id = self.instance.pk if self.instance else None

        WorkingDaysService.check_overlapping_leave(
            employee=employee,
            start_date=start_date,
            end_date=end_date,
            exclude_id=exclude_id,
        )

        if leave_type and start_date and end_date:
            working_days = WorkingDaysService.calculate_working_days(start_date, end_date)
            year = start_date.year
            WorkingDaysService.validate_leave_balance(
                employee=employee,
                leave_type=leave_type,
                year=year,
                requested_days=working_days,
            )

        return attrs

    def create(self, validated_data):
        employee = self.context["request"].user
        start_date = validated_data["start_date"]
        end_date = validated_data["end_date"]
        total_working_days = WorkingDaysService.calculate_working_days(start_date, end_date)

        return LeaveRequest.objects.create(
            employee=employee,
            status=LeaveRequestStatus.DRAFT,
            total_working_days=total_working_days,
            **validated_data,
        )


# ---------------------------------------------------------------------------
# LeaveRequest — read
# ---------------------------------------------------------------------------

class LeaveRequestReadSerializer(serializers.ModelSerializer):
    employee = _EmployeeMinimalSerializer(read_only=True)
    leave_type = LeaveTypeSerializer(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = LeaveRequest
        fields = (
            "id",
            "employee",
            "leave_type",
            "start_date",
            "end_date",
            "total_working_days",
            "reason",
            "is_emergency",
            "status",
            "status_display",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


# ---------------------------------------------------------------------------
# LeaveApprovalLog
# ---------------------------------------------------------------------------

class LeaveApprovalLogSerializer(serializers.ModelSerializer):
    actor = _EmployeeMinimalSerializer(read_only=True)
    action_display = serializers.CharField(source="get_action_display", read_only=True)

    class Meta:
        model = LeaveApprovalLog
        fields = (
            "id",
            "leave_request",
            "actor",
            "action",
            "action_display",
            "comment",
            "timestamp",
            "previous_status",
            "new_status",
        )
        read_only_fields = fields
