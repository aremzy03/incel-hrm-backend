from django.contrib.auth import get_user_model
from rest_framework import serializers

from apps.accounts.models import RoleName

from .models import (
    LeaveApprovalLog,
    LeaveBalance,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveType,
    PublicHoliday,
)
from .services import (
    get_eligible_relievers,
    reliever_required,
    validate_cover_person_assignment,
    WorkingDaysService,
)

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


class PublicHolidaySerializer(serializers.ModelSerializer):
    class Meta:
        model = PublicHoliday
        fields = ("id", "name", "date", "is_recurring")
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
            "employee",
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
      1. start_date <= end_date (single-day leave allowed)
      2. WorkingDaysService.check_overlapping_leave()
      3. WorkingDaysService.check_department_leave_overlap() (Annual/Casual only)
      4. WorkingDaysService.validate_leave_balance()
      5. cover_person validations (org-scoped reliever rules)

    On create():
      - total_working_days is computed via WorkingDaysService.calculate_working_days()
      - status is set to DRAFT
      - employee is taken from request.user (passed via serializer context)
    """

    cover_person = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(is_active=True),
        required=False,
        allow_null=True,
    )

    class Meta:
        model = LeaveRequest
        fields = ("leave_type", "start_date", "end_date", "reason", "is_emergency", "cover_person")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get("request")
        if request is None:
            return

        hr_override = self.context.get("hr_override", False)
        applicant = self.context.get("applicant", request.user)
        if hr_override:
            self.fields["cover_person"].queryset = User.objects.filter(is_active=True)
        else:
            self.fields["cover_person"].queryset = get_eligible_relievers(applicant).relievers

    def validate(self, attrs):
        start_date = attrs.get("start_date")
        end_date = attrs.get("end_date")

        if start_date and end_date:
            if start_date > end_date:
                raise serializers.ValidationError(
                    {"end_date": "end_date must be on or after start_date."}
                )

        request = self.context["request"]
        employee = self.context.get("applicant", request.user)
        hr_override = self.context.get("hr_override", False)
        leave_type = attrs.get("leave_type")
        cover_person = attrs.get("cover_person", serializers.empty)

        if self.instance is not None:
            merged_leave_type = leave_type if leave_type is not None else self.instance.leave_type
            merged_is_emergency = (
                attrs["is_emergency"] if "is_emergency" in attrs else self.instance.is_emergency
            )
            merged_start = start_date if start_date is not None else self.instance.start_date
            merged_end = end_date if end_date is not None else self.instance.end_date

            if cover_person is serializers.empty:
                merged_cover_person = self.instance.cover_person
            else:
                merged_cover_person = cover_person

            preview = LeaveRequest(
                employee=self.instance.employee,
                leave_type=merged_leave_type,
                is_emergency=merged_is_emergency,
                start_date=merged_start,
                end_date=merged_end,
                cover_person=merged_cover_person,
            )

            if (
                cover_person is not serializers.empty
                and cover_person is None
                and self.instance.status != LeaveRequestStatus.DRAFT
                and reliever_required(preview)
            ):
                raise serializers.ValidationError(
                    {"cover_person": "A reliever is required for this leave request."}
                )

            if (
                cover_person is serializers.empty
                and merged_cover_person is None
                and self.instance.status != LeaveRequestStatus.DRAFT
                and reliever_required(preview)
            ):
                raise serializers.ValidationError(
                    {"cover_person": "A reliever is required for this leave request."}
                )

            if cover_person is not serializers.empty:
                validate_cover_person_assignment(
                    preview,
                    cover_person,
                    hr_override=hr_override,
                )
        elif cover_person is not serializers.empty and cover_person is not None:
            preview = LeaveRequest(
                employee=employee,
                leave_type=leave_type,
                is_emergency=attrs.get("is_emergency", False),
                start_date=start_date,
                end_date=end_date,
                cover_person=cover_person,
            )
            validate_cover_person_assignment(
                preview,
                cover_person,
                hr_override=hr_override,
            )

        if leave_type:
            if leave_type.name in ("Maternity", "Maternity Leave") and getattr(employee, "gender", None) != "FEMALE":
                raise serializers.ValidationError(
                    {"leave_type": "Maternity leave is only available for female staff."}
                )
            if leave_type.name in ("Paternity", "Paternity Leave") and getattr(employee, "gender", None) != "MALE":
                raise serializers.ValidationError(
                    {"leave_type": "Paternity leave is only available for male staff."}
                )

        exclude_id = self.instance.pk if self.instance else None
        overlap_employee = self.instance.employee if self.instance else employee
        start_for_overlap = start_date or (self.instance.start_date if self.instance else None)
        end_for_overlap = end_date or (self.instance.end_date if self.instance else None)

        if start_for_overlap and end_for_overlap:
            WorkingDaysService.check_overlapping_leave(
                employee=overlap_employee,
                start_date=start_for_overlap,
                end_date=end_for_overlap,
                exclude_id=exclude_id,
            )

        leave_type_for_overlap = leave_type or (self.instance.leave_type if self.instance else None)
        WorkingDaysService.check_department_leave_overlap(
            employee=overlap_employee,
            start_date=start_for_overlap,
            end_date=end_for_overlap,
            leave_type=leave_type_for_overlap,
            exclude_id=exclude_id,
        )

        if leave_type_for_overlap and start_for_overlap and end_for_overlap:
            if leave_type and start_date and end_date:
                working_days = WorkingDaysService.calculate_working_days(start_date, end_date)
                year = start_date.year
            else:
                working_days = WorkingDaysService.calculate_working_days(
                    start_for_overlap, end_for_overlap
                )
                year = start_for_overlap.year
            WorkingDaysService.validate_leave_balance(
                employee=overlap_employee,
                leave_type=leave_type_for_overlap,
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
