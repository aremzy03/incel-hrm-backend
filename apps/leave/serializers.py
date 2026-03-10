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

_COVER_PERSON_QUERYSET = User.objects.filter(is_active=True)


# ---------------------------------------------------------------------------
# Nested helpers
# ---------------------------------------------------------------------------

class _EmployeeMinimalSerializer(serializers.ModelSerializer):
    """Lightweight user representation used inside read serializers."""

    class Meta:
        model = User
        fields = ("id", "email", "first_name", "last_name")
        read_only_fields = fields


class _EmployeeCalendarSerializer(serializers.ModelSerializer):
    """Employee with department name for calendar entries."""

    department_name = serializers.CharField(source="department.name", default=None, read_only=True)

    class Meta:
        model = User
        fields = ("id", "email", "first_name", "last_name", "department_name")
        read_only_fields = fields


# ---------------------------------------------------------------------------
# LeaveType
# ---------------------------------------------------------------------------

class LeaveTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeaveType
        fields = ("id", "name", "description", "default_days", "created_at", "updated_at")
        read_only_fields = ("id", "created_at", "updated_at")


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
      3. WorkingDaysService.check_department_leave_overlap() (Annual/Casual only)
      4. WorkingDaysService.validate_leave_balance()
      5. cover_person validations (not self, same department)

    On create():
      - total_working_days is computed via WorkingDaysService.calculate_working_days()
      - status is set to DRAFT
      - employee is taken from request.user (passed via serializer context)
    """

    cover_person = serializers.PrimaryKeyRelatedField(queryset=_COVER_PERSON_QUERYSET)

    class Meta:
        model = LeaveRequest
        fields = ("leave_type", "start_date", "end_date", "reason", "is_emergency", "cover_person")

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
        cover_person = attrs.get("cover_person")

        if cover_person:
            if cover_person == employee:
                raise serializers.ValidationError(
                    {"cover_person": "You cannot assign yourself as the cover person."}
                )
            if getattr(employee, "department_id", None) and cover_person.department_id != employee.department_id:
                raise serializers.ValidationError(
                    {"cover_person": "The cover person must be in the same department as you."}
                )

        if leave_type:
            if leave_type.name == "Maternity" and getattr(employee, "gender", None) != "FEMALE":
                raise serializers.ValidationError(
                    {"leave_type": "Maternity leave is only available for female staff."}
                )
            if leave_type.name == "Paternity" and getattr(employee, "gender", None) != "MALE":
                raise serializers.ValidationError(
                    {"leave_type": "Paternity leave is only available for male staff."}
                )

        exclude_id = self.instance.pk if self.instance else None

        WorkingDaysService.check_overlapping_leave(
            employee=employee,
            start_date=start_date,
            end_date=end_date,
            exclude_id=exclude_id,
        )

        leave_type_for_overlap = leave_type or (self.instance.leave_type if self.instance else None)
        start_for_overlap = start_date or (self.instance.start_date if self.instance else None)
        end_for_overlap = end_date or (self.instance.end_date if self.instance else None)
        WorkingDaysService.check_department_leave_overlap(
            employee=employee,
            start_date=start_for_overlap,
            end_date=end_for_overlap,
            leave_type=leave_type_for_overlap,
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
    cover_person = _EmployeeMinimalSerializer(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = LeaveRequest
        fields = (
            "id",
            "employee",
            "leave_type",
            "cover_person",
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


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

class CalendarEntrySerializer(serializers.ModelSerializer):
    """Read-only representation of an approved leave for the department calendar."""

    employee = _EmployeeCalendarSerializer(read_only=True)
    leave_type = LeaveTypeSerializer(read_only=True)

    class Meta:
        model = LeaveRequest
        fields = (
            "id",
            "employee",
            "leave_type",
            "start_date",
            "end_date",
            "total_working_days",
        )
        read_only_fields = fields
