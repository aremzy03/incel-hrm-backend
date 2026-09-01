from django.contrib.auth import get_user_model
from rest_framework import serializers

from apps.accounts.models import RoleName

from .models import (
    AccrualMethod,
    ApproverDelegate,
    ApproverSource,
    CalendarAssignment,
    CalendarHoliday,
    HolidayCalendar,
    LeaveApprovalLog,
    LeaveBalance,
    LeaveBalanceTransaction,
    LeaveBlackoutPeriod,
    LeavePolicy,
    LeavePolicyAssignment,
    LeavePolicyStatus,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveSettings,
    LeaveSettingsAuditLog,
    LeaveType,
    LeaveWorkflowStage,
    LeaveWorkflowTemplate,
    LeaveYearType,
    PublicHoliday,
    WorkingCalendar,
)
from .services import (
    adjust_balance_for_reconciled_edit,
    apply_policy_snapshot,
    ensure_leave_balance_record,
    find_conflicting_assignments,
    get_eligible_relievers,
    reconcile_leave_request,
    reliever_required,
    validate_assignment_scope,
    validate_blackout_periods,
    validate_cover_person_assignment,
    validate_half_day_request,
    validate_reconcile_balance,
    validate_reconcile_row,
    WorkingDaysService,
)
from . import messages as leave_messages

User = get_user_model()


def _leave_type_code(leave_type) -> str:
    return (getattr(leave_type, "code", None) or "").upper()


def _assert_leave_type_usable(leave_type, employee):
    if leave_type is None:
        return
    if not leave_type.is_active:
        raise serializers.ValidationError(
            {"leave_type": leave_messages.inactive_leave_type()}
        )
    if _leave_type_code(leave_type) == LeaveType.Code.MATERNITY and getattr(employee, "gender", None) != "FEMALE":
        raise serializers.ValidationError(
            {"leave_type": leave_messages.maternity_not_eligible()}
        )
    if _leave_type_code(leave_type) == LeaveType.Code.PATERNITY and getattr(employee, "gender", None) != "MALE":
        raise serializers.ValidationError(
            {"leave_type": leave_messages.paternity_not_eligible()}
        )


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
    reason = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = LeaveType
        fields = (
            "id",
            "name",
            "code",
            "description",
            "default_days",
            "is_active",
            "display_order",
            "calendar_color",
            "created_at",
            "updated_at",
            "reason",
        )
        read_only_fields = ("id", "created_at", "updated_at")
        extra_kwargs = {"code": {"required": False}}

    def create(self, validated_data):
        validated_data.pop("reason", None)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        validated_data.pop("reason", None)
        return super().update(instance, validated_data)

    def validate_code(self, value):
        return (value or "").strip().upper().replace("-", "_")

    def validate(self, attrs):
        instance = self.instance
        if instance is None:
            return attrs
        if "code" in attrs and attrs["code"] != instance.code:
            if instance.requests.exists():
                raise serializers.ValidationError(
                    {"code": leave_messages.leave_type_code_immutable()}
                )
        return attrs


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
    remaining_days = serializers.DecimalField(max_digits=8, decimal_places=2, read_only=True)
    available_days = serializers.DecimalField(max_digits=8, decimal_places=2, read_only=True)

    class Meta:
        model = LeaveBalance
        fields = (
            "id",
            "employee",
            "leave_type",
            "year",
            "allocated_days",
            "used_days",
            "pending_days",
            "carried_forward_days",
            "carry_forward_expires_on",
            "remaining_days",
            "available_days",
        )
        read_only_fields = fields


class LeaveBalanceTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeaveBalanceTransaction
        fields = (
            "id",
            "leave_balance",
            "leave_request",
            "transaction_type",
            "source",
            "delta_used_days",
            "delta_pending_days",
            "delta_allocated_days",
            "actor",
            "reason",
            "effective_date",
            "created_at",
        )
        read_only_fields = fields


class LeaveBalanceAdjustSerializer(serializers.Serializer):
    delta = serializers.DecimalField(max_digits=8, decimal_places=2)
    reason = serializers.CharField()
    effective_date = serializers.DateField(required=False, allow_null=True)

    def validate_reason(self, value):
        if not (value or "").strip():
            raise serializers.ValidationError(leave_messages.balance_adjust_reason_required())
        return value.strip()

    def validate_delta(self, value):
        if value == 0:
            raise serializers.ValidationError(leave_messages.balance_adjust_delta_zero())
        return value


class LeaveBlackoutPeriodSerializer(serializers.ModelSerializer):
    reason = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = LeaveBlackoutPeriod
        fields = (
            "id",
            "name",
            "start_date",
            "end_date",
            "enforcement",
            "leave_types",
            "department",
            "is_active",
            "created_at",
            "updated_at",
            "reason",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def validate(self, attrs):
        start = attrs.get("start_date", getattr(self.instance, "start_date", None))
        end = attrs.get("end_date", getattr(self.instance, "end_date", None))
        if start and end and start > end:
            raise serializers.ValidationError(
                {"end_date": leave_messages.invalid_date_range()}
            )
        return attrs


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
    blackout_override_reason = serializers.CharField(
        write_only=True, required=False, allow_blank=True
    )

    class Meta:
        model = LeaveRequest
        fields = (
            "leave_type",
            "start_date",
            "end_date",
            "reason",
            "is_emergency",
            "cover_person",
            "is_half_day",
            "half_day_period",
            "blackout_override_reason",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get("request")
        if request is None:
            return

        hr_override = self.context.get("hr_override", False)
        applicant = self.context.get("applicant", request.user)
        leave_type = None
        if self.instance is not None:
            leave_type = self.instance.leave_type
        elif getattr(self, "initial_data", None):
            lt_id = self.initial_data.get("leave_type")
            if lt_id:
                leave_type = LeaveType.objects.filter(pk=lt_id).first()
        if hr_override:
            self.fields["cover_person"].queryset = User.objects.filter(is_active=True)
        else:
            self.fields["cover_person"].queryset = get_eligible_relievers(
                applicant, leave_type
            ).relievers

    def validate(self, attrs):
        start_date = attrs.get("start_date")
        end_date = attrs.get("end_date")

        if start_date and end_date:
            if start_date > end_date:
                raise serializers.ValidationError(
                    {"end_date": leave_messages.invalid_date_range()}
                )

        request = self.context["request"]
        employee = self.context.get("applicant", request.user)
        hr_override = self.context.get("hr_override", False)
        leave_type = attrs.get("leave_type")
        cover_person = attrs.get("cover_person", serializers.empty)
        is_half_day = attrs.get(
            "is_half_day",
            self.instance.is_half_day if self.instance is not None else False,
        )
        half_day_period = attrs.get(
            "half_day_period",
            self.instance.half_day_period if self.instance is not None else "",
        )
        if not is_half_day:
            attrs.setdefault("half_day_period", "")
            half_day_period = attrs.get("half_day_period") or ""

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
                    {
                        "cover_person": leave_messages.reliever_required(
                            merged_leave_type
                        )
                    }
                )

            if (
                cover_person is serializers.empty
                and merged_cover_person is None
                and self.instance.status != LeaveRequestStatus.DRAFT
                and reliever_required(preview)
            ):
                raise serializers.ValidationError(
                    {
                        "cover_person": leave_messages.reliever_required(
                            merged_leave_type
                        )
                    }
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
            _assert_leave_type_usable(leave_type, employee)

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
        if leave_type_for_overlap and start_for_overlap and end_for_overlap:
            validate_half_day_request(
                leave_type_for_overlap,
                start_for_overlap,
                end_for_overlap,
                is_half_day,
                half_day_period,
                employee=overlap_employee,
            )

        if start_for_overlap and end_for_overlap:
            WorkingDaysService.check_overlapping_leave(
                employee=overlap_employee,
                start_date=start_for_overlap,
                end_date=end_for_overlap,
                exclude_id=exclude_id,
            )

        WorkingDaysService.check_department_leave_overlap(
            employee=overlap_employee,
            start_date=start_for_overlap,
            end_date=end_for_overlap,
            leave_type=leave_type_for_overlap,
            exclude_id=exclude_id,
        )

        if leave_type_for_overlap and start_for_overlap and end_for_overlap:
            working_days = WorkingDaysService.calculate_working_days(
                start_for_overlap,
                end_for_overlap,
                leave_type=leave_type_for_overlap,
                is_half_day=is_half_day,
                employee=overlap_employee,
            )
            year = start_for_overlap.year
            WorkingDaysService.validate_leave_balance(
                employee=overlap_employee,
                leave_type=leave_type_for_overlap,
                year=year,
                requested_days=working_days,
            )

        if leave_type_for_overlap and start_for_overlap and end_for_overlap:
            override_reason = attrs.pop("blackout_override_reason", "") or ""
            validate_blackout_periods(
                start_date=start_for_overlap,
                end_date=end_for_overlap,
                leave_type=leave_type_for_overlap,
                employee=overlap_employee,
                actor=request.user,
                override_reason=override_reason,
            )

        return attrs

    def create(self, validated_data):
        validated_data.pop("blackout_override_reason", None)
        employee = self.context["request"].user
        start_date = validated_data["start_date"]
        end_date = validated_data["end_date"]
        is_half_day = validated_data.get("is_half_day", False)
        total_working_days = WorkingDaysService.calculate_working_days(
            start_date,
            end_date,
            leave_type=validated_data.get("leave_type"),
            is_half_day=is_half_day,
            employee=employee,
        )

        instance = LeaveRequest(
            employee=employee,
            status=LeaveRequestStatus.DRAFT,
            total_working_days=total_working_days,
            **validated_data,
        )
        apply_policy_snapshot(instance)
        instance.save()
        return instance


# ---------------------------------------------------------------------------
# LeaveRequest — read
# ---------------------------------------------------------------------------

class LeaveRequestReadSerializer(serializers.ModelSerializer):
    employee = _EmployeeMinimalSerializer(read_only=True)
    leave_type = LeaveTypeSerializer(read_only=True)
    cover_person = _EmployeeMinimalSerializer(read_only=True)
    reconciled_by = _EmployeeMinimalSerializer(read_only=True)
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
            "is_half_day",
            "half_day_period",
            "reason",
            "is_emergency",
            "status",
            "status_display",
            "is_reconciled",
            "reconciled_by",
            "reconciled_at",
            "reconciliation_note",
            "policy",
            "policy_version",
            "calculation_snapshot",
            "workflow_snapshot",
            "stage_entered_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


# ---------------------------------------------------------------------------
# LeaveRequest — HR reconciliation
# ---------------------------------------------------------------------------

class LeaveRequestReconcileSerializer(serializers.Serializer):
    """
    HR-only: record backdated leave for an employee without the approval workflow.
    Creates an APPROVED request, deducts balance, and queues stakeholder notifications.
    """

    employee = serializers.PrimaryKeyRelatedField(queryset=User.objects.filter(is_active=True))
    leave_type = serializers.PrimaryKeyRelatedField(queryset=LeaveType.objects.all())
    start_date = serializers.DateField()
    end_date = serializers.DateField()
    reason = serializers.CharField(required=False, allow_blank=True, default="")
    reconciliation_note = serializers.CharField()
    cover_person = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(is_active=True),
        required=False,
        allow_null=True,
    )
    allow_insufficient_balance = serializers.BooleanField(required=False, default=False)
    notify_department_colleagues = serializers.BooleanField(required=False, default=False)

    def validate_reconciliation_note(self, value):
        note = (value or "").strip()
        if not note:
            raise serializers.ValidationError(leave_messages.reconciliation_note_required())
        return note

    def validate(self, attrs):
        validate_reconcile_row(
            employee=attrs["employee"],
            leave_type=attrs["leave_type"],
            start_date=attrs["start_date"],
            end_date=attrs["end_date"],
            cover_person=attrs.get("cover_person"),
            allow_insufficient_balance=attrs.get("allow_insufficient_balance", False),
        )
        return attrs

    def create(self, validated_data):
        hr_user = self.context["request"].user
        return reconcile_leave_request(
            hr_user=hr_user,
            employee=validated_data["employee"],
            leave_type=validated_data["leave_type"],
            start_date=validated_data["start_date"],
            end_date=validated_data["end_date"],
            reason=validated_data.get("reason", ""),
            reconciliation_note=validated_data["reconciliation_note"],
            cover_person=validated_data.get("cover_person"),
            allow_insufficient_balance=validated_data.get("allow_insufficient_balance", False),
        )


class LeaveRequestReconcileRowSerializer(serializers.Serializer):
    """One row in a bulk reconcile request."""

    employee = serializers.PrimaryKeyRelatedField(queryset=User.objects.filter(is_active=True))
    leave_type = serializers.PrimaryKeyRelatedField(queryset=LeaveType.objects.all())
    start_date = serializers.DateField()
    end_date = serializers.DateField()
    reason = serializers.CharField(required=False, allow_blank=True, default="")
    reconciliation_note = serializers.CharField()
    cover_person = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(is_active=True),
        required=False,
        allow_null=True,
    )

    def validate_reconciliation_note(self, value):
        note = (value or "").strip()
        if not note:
            raise serializers.ValidationError(leave_messages.reconciliation_note_required())
        return note


class LeaveRequestBulkReconcileSerializer(serializers.Serializer):
    """HR-only: reconcile multiple backdated leave rows in one request."""

    rows = LeaveRequestReconcileRowSerializer(many=True)
    allow_insufficient_balance = serializers.BooleanField(required=False, default=False)
    notify_department_colleagues = serializers.BooleanField(required=False, default=False)

    def validate_rows(self, value):
        if not value:
            raise serializers.ValidationError(leave_messages.bulk_reconcile_rows_required())
        return value


class LeaveRequestReconcileEditSerializer(serializers.ModelSerializer):
    """HR-only: edit a reconciled APPROVED leave request with balance adjustment."""

    cover_person = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(is_active=True),
        required=False,
        allow_null=True,
    )
    edit_note = serializers.CharField(required=False, allow_blank=True, default="")
    allow_insufficient_balance = serializers.BooleanField(required=False, default=False)

    class Meta:
        model = LeaveRequest
        fields = (
            "leave_type",
            "start_date",
            "end_date",
            "reason",
            "cover_person",
            "edit_note",
            "allow_insufficient_balance",
        )

    def validate(self, attrs):
        instance = self.instance
        leave_type = attrs.get("leave_type", instance.leave_type)
        start_date = attrs.get("start_date", instance.start_date)
        end_date = attrs.get("end_date", instance.end_date)
        cover_person = attrs.get("cover_person", instance.cover_person)
        allow_insufficient = attrs.get("allow_insufficient_balance", False)

        if start_date > end_date:
            raise serializers.ValidationError(
                {"end_date": leave_messages.invalid_date_range()}
            )

        validate_reconcile_row(
            employee=instance.employee,
            leave_type=leave_type,
            start_date=start_date,
            end_date=end_date,
            cover_person=cover_person,
            allow_insufficient_balance=allow_insufficient,
            exclude_request_id=instance.pk,
        )

        preview = LeaveRequest(
            employee=instance.employee,
            leave_type=leave_type,
            start_date=start_date,
            end_date=end_date,
            cover_person=cover_person,
        )
        old_preview = LeaveRequest(
            employee=instance.employee,
            leave_type=instance.leave_type,
            start_date=instance.start_date,
            end_date=instance.end_date,
        )
        preview.total_working_days = WorkingDaysService.calculate_working_days(
            start_date, end_date, leave_type=leave_type
        )
        old_preview.total_working_days = instance.total_working_days

        if not allow_insufficient:
            from .services import _year_days_for_request, split_working_days_by_year

            old_map = _year_days_for_request(old_preview)
            new_splits = split_working_days_by_year(
                start_date, end_date, leave_type=leave_type
            )
            new_map = {(leave_type.id, year): days for year, days in new_splits.items()}
            all_keys = set(old_map) | set(new_map)
            for leave_type_id, year in all_keys:
                old_days = old_map.get((leave_type_id, year), 0)
                new_days = new_map.get((leave_type_id, year), 0)
                delta = new_days - old_days
                if delta <= 0:
                    continue
                lt = leave_type if leave_type_id == leave_type.id else instance.leave_type
                if leave_type_id != lt.id:
                    lt = LeaveType.objects.get(pk=leave_type_id)
                ensure_leave_balance_record(instance.employee, lt, year)
                WorkingDaysService.validate_leave_balance(
                    employee=instance.employee,
                    leave_type=lt,
                    year=year,
                    requested_days=delta,
                )

        return attrs

    def update(self, instance, validated_data):
        from copy import copy

        from django.db import transaction

        from .models import ApprovalAction, LeaveApprovalLog

        edit_note = validated_data.pop("edit_note", "")
        allow_insufficient = validated_data.pop("allow_insufficient_balance", False)

        old_snapshot = copy(instance)
        old_snapshot.leave_type = instance.leave_type
        old_snapshot.start_date = instance.start_date
        old_snapshot.end_date = instance.end_date
        old_snapshot.total_working_days = instance.total_working_days

        hr_user = self.context["request"].user
        with transaction.atomic():
            for attr, value in validated_data.items():
                setattr(instance, attr, value)
            instance.save()

            adjust_balance_for_reconciled_edit(
                old_snapshot,
                instance,
                actor=hr_user,
                reason=edit_note or "Reconciled leave edited by HR.",
                allow_insufficient_balance=allow_insufficient,
            )
            LeaveApprovalLog.objects.create(
                leave_request=instance,
                actor=hr_user,
                action=ApprovalAction.MODIFY,
                previous_status=instance.status,
                new_status=instance.status,
                comment=edit_note or "Reconciled leave edited by HR.",
            )

        return instance


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
            "is_half_day",
            "half_day_period",
        )
        read_only_fields = fields


# ---------------------------------------------------------------------------
# LeavePolicy
# ---------------------------------------------------------------------------

class LeavePolicySerializer(serializers.ModelSerializer):
    leave_type_detail = LeaveTypeSerializer(source="leave_type", read_only=True)
    reason = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = LeavePolicy
        fields = (
            "id",
            "name",
            "leave_type",
            "leave_type_detail",
            "status",
            "version",
            "effective_from",
            "effective_to",
            "annual_entitlement",
            "accrual_method",
            "accrual_rate",
            "prorate_new_joiners",
            "carry_forward",
            "carry_forward_max_days",
            "carry_forward_expiry_months",
            "forfeit_unused",
            "half_day_allowed",
            "weekend_excluded",
            "public_holiday_excluded",
            "forfeited_on_resignation",
            "allow_backdated",
            "maximum_backdate_days",
            "reliever_required",
            "reliever_scope",
            "overlap_control_enabled",
            "overlap_scope",
            "maximum_people_absent",
            "overlap_enforcement",
            "created_at",
            "updated_at",
            "reason",
        )
        read_only_fields = (
            "id",
            "status",
            "version",
            "created_at",
            "updated_at",
        )

    def validate(self, attrs):
        instance = self.instance
        if instance is not None and instance.status != LeavePolicyStatus.DRAFT:
            raise serializers.ValidationError(
                {"status": leave_messages.policy_edit_draft_only()}
            )
        effective_from = attrs.get(
            "effective_from", instance.effective_from if instance else None
        )
        effective_to = attrs.get(
            "effective_to", instance.effective_to if instance else None
        )
        if effective_from and effective_to and effective_to < effective_from:
            raise serializers.ValidationError(
                {"effective_to": leave_messages.policy_effective_to_before_from()}
            )
        carry_forward = attrs.get(
            "carry_forward", instance.carry_forward if instance else False
        )
        expiry_months = attrs.get(
            "carry_forward_expiry_months",
            instance.carry_forward_expiry_months if instance else None,
        )
        if expiry_months and not carry_forward:
            raise serializers.ValidationError(
                {
                    "carry_forward_expiry_months": (
                        leave_messages.policy_carry_forward_expiry_requires_flag()
                    )
                }
            )
        method = attrs.get(
            "accrual_method", instance.accrual_method if instance else AccrualMethod.UPFRONT
        )
        if method not in AccrualMethod.values:
            raise serializers.ValidationError(
                {"accrual_method": leave_messages.policy_invalid_accrual_method()}
            )
        return attrs

    def create(self, validated_data):
        validated_data.pop("reason", None)
        validated_data["status"] = LeavePolicyStatus.DRAFT
        validated_data.setdefault("version", 0)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        validated_data.pop("reason", None)
        return super().update(instance, validated_data)


class LeavePolicyActionSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default="")
    keep_existing_active = serializers.BooleanField(required=False, default=False)


class LeaveSettingsAuditLogSerializer(serializers.ModelSerializer):
    actor = _EmployeeMinimalSerializer(read_only=True)

    class Meta:
        model = LeaveSettingsAuditLog
        fields = (
            "id",
            "actor",
            "created_at",
            "object_type",
            "object_id",
            "action",
            "previous_values",
            "new_values",
            "reason",
            "ip_address",
        )
        read_only_fields = fields


class LeavePolicyAssignmentSerializer(serializers.ModelSerializer):
    policy_detail = LeavePolicySerializer(source="policy", read_only=True)
    employee_detail = _EmployeeMinimalSerializer(source="employee", read_only=True)
    leave_type = serializers.UUIDField(source="policy.leave_type_id", read_only=True)
    reason = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = LeavePolicyAssignment
        fields = (
            "id",
            "policy",
            "policy_detail",
            "leave_type",
            "scope_type",
            "scope_id",
            "employee",
            "employee_detail",
            "priority",
            "effective_from",
            "effective_to",
            "is_active",
            "created_at",
            "updated_at",
            "reason",
        )
        read_only_fields = ("id", "created_at", "updated_at", "leave_type")

    def validate(self, attrs):
        instance = self.instance
        scope_type = attrs.get("scope_type", getattr(instance, "scope_type", None))
        scope_id = attrs.get("scope_id", getattr(instance, "scope_id", "") if instance else "")
        employee = attrs.get("employee", getattr(instance, "employee", None) if instance else None)
        if "employee" in attrs and attrs["employee"] is None:
            employee = None
        policy = attrs.get("policy", getattr(instance, "policy", None) if instance else None)
        effective_from = attrs.get(
            "effective_from", instance.effective_from if instance else None
        )
        effective_to = attrs.get(
            "effective_to", instance.effective_to if instance else None
        )
        if effective_from and effective_to and effective_to < effective_from:
            raise serializers.ValidationError(
                {"effective_to": leave_messages.policy_effective_to_before_from()}
            )
        validate_assignment_scope(scope_type, scope_id, employee)
        if policy is None:
            raise serializers.ValidationError(
                {"policy": leave_messages.assignment_policy_required()}
            )
        if scope_type == "EMPLOYEE" and employee is not None:
            attrs["scope_id"] = str(employee.pk)
        elif scope_type == "ORGANIZATION":
            attrs["scope_id"] = ""
        preview = LeavePolicyAssignment(
            policy=policy,
            scope_type=scope_type,
            scope_id=(attrs.get("scope_id") if "scope_id" in attrs else (scope_id or "")),
            employee=employee,
            priority=attrs.get("priority", instance.priority if instance else 0),
            effective_from=effective_from,
            effective_to=effective_to,
            is_active=attrs.get("is_active", instance.is_active if instance else True),
        )
        if preview.is_active:
            conflicts = find_conflicting_assignments(
                preview, exclude_pk=instance.pk if instance else None
            )
            if conflicts:
                first = conflicts[0]
                raise serializers.ValidationError(
                    {"non_field_errors": leave_messages.assignment_conflict(first)}
                )
        return attrs

    def create(self, validated_data):
        validated_data.pop("reason", None)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        validated_data.pop("reason", None)
        return super().update(instance, validated_data)


class LeaveAccrualPreviewSerializer(serializers.Serializer):
    as_of = serializers.DateField(required=False)
    year = serializers.IntegerField(required=False, min_value=2000, max_value=2100)
    month = serializers.IntegerField(required=False, min_value=1, max_value=12)
    include_rollover = serializers.BooleanField(default=True)
    include_monthly = serializers.BooleanField(default=True)
    include_weekly = serializers.BooleanField(default=False)
    include_anniversary = serializers.BooleanField(default=False)
    include_carry_expiry = serializers.BooleanField(default=True)


class LeaveSettingsSerializer(serializers.ModelSerializer):
    reason = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = LeaveSettings
        fields = (
            "id",
            "leave_year_type",
            "leave_year_start_month",
            "leave_year_start_day",
            "cross_year_deduction_rule",
            "default_timezone",
            "default_working_calendar",
            "default_holiday_calendar",
            "notify_applicant_on_submit",
            "notify_applicant_on_decision",
            "notify_approver",
            "notify_reliever",
            "notify_department_reminder",
            "reminder_lead_hours",
            "allow_hr_override",
            "prevent_self_approval",
            "approval_sla_hours",
            "encashment_allowed",
            "encashment_max_days",
            "updated_at",
            "reason",
        )
        read_only_fields = ("id", "updated_at")

    def validate(self, attrs):
        month = attrs.get(
            "leave_year_start_month",
            getattr(self.instance, "leave_year_start_month", 1),
        )
        day = attrs.get(
            "leave_year_start_day",
            getattr(self.instance, "leave_year_start_day", 1),
        )
        if not 1 <= int(month) <= 12:
            raise serializers.ValidationError(
                {"leave_year_start_month": leave_messages.settings_leave_year_month_invalid()}
            )
        if not 1 <= int(day) <= 28:
            raise serializers.ValidationError(
                {"leave_year_start_day": leave_messages.settings_leave_year_day_invalid()}
            )
        year_type = attrs.get(
            "leave_year_type", getattr(self.instance, "leave_year_type", LeaveYearType.CALENDAR)
        )
        if year_type == LeaveYearType.CALENDAR:
            attrs["leave_year_start_month"] = 1
            attrs["leave_year_start_day"] = 1
        hours = attrs.get("reminder_lead_hours")
        if hours is not None and int(hours) < 1:
            raise serializers.ValidationError(
                {"reminder_lead_hours": leave_messages.settings_reminder_lead_hours_invalid()}
            )
        return attrs

    def update(self, instance, validated_data):
        validated_data.pop("reason", None)
        return super().update(instance, validated_data)


class WorkingCalendarSerializer(serializers.ModelSerializer):
    reason = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = WorkingCalendar
        fields = (
            "id",
            "name",
            "is_active",
            "is_org_default",
            "timezone",
            "weekdays",
            "hours_per_day",
            "effective_from",
            "effective_to",
            "created_at",
            "updated_at",
            "reason",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def validate_weekdays(self, value):
        if not isinstance(value, list) or not value:
            raise serializers.ValidationError(leave_messages.calendar_weekdays_required())
        cleaned = []
        for item in value:
            try:
                day = int(item)
            except (TypeError, ValueError):
                raise serializers.ValidationError(leave_messages.calendar_weekday_invalid())
            if day < 0 or day > 6:
                raise serializers.ValidationError(leave_messages.calendar_weekday_invalid())
            if day not in cleaned:
                cleaned.append(day)
        return cleaned

    def create(self, validated_data):
        validated_data.pop("reason", None)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        validated_data.pop("reason", None)
        return super().update(instance, validated_data)


class CalendarHolidaySerializer(serializers.ModelSerializer):
    class Meta:
        model = CalendarHoliday
        fields = (
            "id",
            "name",
            "date",
            "is_recurring",
            "is_full_day",
            "observed_date",
            "location_scope",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")


class HolidayCalendarSerializer(serializers.ModelSerializer):
    holidays = CalendarHolidaySerializer(many=True, required=False)
    reason = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = HolidayCalendar
        fields = (
            "id",
            "name",
            "is_active",
            "is_org_default",
            "timezone",
            "effective_from",
            "effective_to",
            "holidays",
            "created_at",
            "updated_at",
            "reason",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def create(self, validated_data):
        holidays = validated_data.pop("holidays", [])
        validated_data.pop("reason", None)
        calendar = HolidayCalendar.objects.create(**validated_data)
        for row in holidays:
            CalendarHoliday.objects.create(calendar=calendar, **row)
        return calendar

    def update(self, instance, validated_data):
        holidays = validated_data.pop("holidays", None)
        validated_data.pop("reason", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        if holidays is not None:
            instance.holidays.all().delete()
            for row in holidays:
                CalendarHoliday.objects.create(calendar=instance, **row)
        return instance


class CalendarAssignmentSerializer(serializers.ModelSerializer):
    reason = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = CalendarAssignment
        fields = (
            "id",
            "working_calendar",
            "holiday_calendar",
            "employee",
            "department",
            "is_active",
            "effective_from",
            "effective_to",
            "created_at",
            "updated_at",
            "reason",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def validate(self, attrs):
        employee = attrs.get("employee", getattr(self.instance, "employee", None))
        department = attrs.get("department", getattr(self.instance, "department", None))
        if bool(employee) == bool(department):
            raise serializers.ValidationError(
                leave_messages.calendar_assignment_target_invalid()
            )
        working = attrs.get("working_calendar", getattr(self.instance, "working_calendar", None))
        holiday = attrs.get("holiday_calendar", getattr(self.instance, "holiday_calendar", None))
        if working is None and holiday is None:
            raise serializers.ValidationError(
                leave_messages.calendar_assignment_calendars_required()
            )
        return attrs

    def create(self, validated_data):
        validated_data.pop("reason", None)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        validated_data.pop("reason", None)
        return super().update(instance, validated_data)


class LeaveWorkflowStageSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeaveWorkflowStage
        fields = (
            "id",
            "order",
            "approver_source",
            "status_code",
            "named_user",
            "role_name",
            "sla_hours",
            "skip_if_unresolved",
            "is_optional",
            "skip_if_requester_roles",
            "use_management_line_manager_for_line_manager_requester",
        )
        read_only_fields = ("id",)

    def validate(self, attrs):
        source = attrs.get(
            "approver_source",
            getattr(self.instance, "approver_source", None),
        )
        named_user = attrs.get(
            "named_user",
            getattr(self.instance, "named_user", None) if self.instance else None,
        )
        role_name = attrs.get(
            "role_name",
            getattr(self.instance, "role_name", "") if self.instance else "",
        )
        if source == ApproverSource.NAMED_USER and named_user is None:
            raise serializers.ValidationError(
                {"named_user": leave_messages.workflow_named_user_required()}
            )
        if source == ApproverSource.ROLE and not role_name:
            raise serializers.ValidationError(
                {"role_name": leave_messages.workflow_role_required()}
            )
        return attrs


class LeaveWorkflowTemplateSerializer(serializers.ModelSerializer):
    stages = LeaveWorkflowStageSerializer(many=True, required=False)
    reason = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = LeaveWorkflowTemplate
        fields = (
            "id",
            "name",
            "is_active",
            "is_org_default",
            "leave_type",
            "mode",
            "reject_comment_required",
            "approve_comment_required",
            "sla_hours",
            "auto_approve_after_sla",
            "stages",
            "created_at",
            "updated_at",
            "reason",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def validate(self, attrs):
        leave_type = attrs.get(
            "leave_type", getattr(self.instance, "leave_type", None) if self.instance else None
        )
        if "leave_type" in attrs and attrs["leave_type"] is None:
            leave_type = None
        is_active = attrs.get(
            "is_active", getattr(self.instance, "is_active", True) if self.instance else True
        )
        if is_active and leave_type is not None:
            qs = LeaveWorkflowTemplate.objects.filter(
                is_active=True, leave_type=leave_type
            )
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError(
                    {"leave_type": leave_messages.workflow_duplicate_active_for_leave_type()}
                )
        if attrs.get("is_org_default"):
            qs = LeaveWorkflowTemplate.objects.filter(is_org_default=True)
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError(
                    {"is_org_default": leave_messages.workflow_single_org_default()}
                )
        stages = attrs.get("stages")
        if stages is not None:
            orders = [s.get("order") for s in stages]
            if len(orders) != len(set(orders)):
                raise serializers.ValidationError(
                    {"stages": leave_messages.workflow_stage_order_unique()}
                )
        return attrs

    def _save_stages(self, template, stages_data):
        template.stages.all().delete()
        for row in stages_data:
            LeaveWorkflowStage.objects.create(template=template, **row)

    def create(self, validated_data):
        validated_data.pop("reason", None)
        stages = validated_data.pop("stages", [])
        template = LeaveWorkflowTemplate.objects.create(**validated_data)
        self._save_stages(template, stages)
        return template

    def update(self, instance, validated_data):
        validated_data.pop("reason", None)
        stages = validated_data.pop("stages", None)
        for key, value in validated_data.items():
            setattr(instance, key, value)
        instance.save()
        if stages is not None:
            self._save_stages(instance, stages)
        return instance


class LeaveWorkflowSimulateSerializer(serializers.Serializer):
    employee = serializers.PrimaryKeyRelatedField(queryset=User.objects.filter(is_active=True))
    leave_type = serializers.PrimaryKeyRelatedField(
        queryset=LeaveType.objects.all(), required=False, allow_null=True
    )
    total_working_days = serializers.DecimalField(
        max_digits=8, decimal_places=2, required=False
    )


class ApproverDelegateSerializer(serializers.ModelSerializer):
    user_detail = _EmployeeMinimalSerializer(source="user", read_only=True)
    delegate_detail = _EmployeeMinimalSerializer(source="delegate", read_only=True)
    reason = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = ApproverDelegate
        fields = (
            "id",
            "user",
            "user_detail",
            "delegate",
            "delegate_detail",
            "start_date",
            "end_date",
            "is_active",
            "created_at",
            "updated_at",
            "reason",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def validate(self, attrs):
        user = attrs.get("user", getattr(self.instance, "user", None))
        delegate = attrs.get("delegate", getattr(self.instance, "delegate", None))
        start_date = attrs.get("start_date", getattr(self.instance, "start_date", None))
        end_date = attrs.get("end_date", getattr(self.instance, "end_date", None))
        if user and delegate and user.pk == delegate.pk:
            raise serializers.ValidationError(
                {"delegate": leave_messages.delegate_cannot_be_self()}
            )
        if start_date and end_date and end_date < start_date:
            raise serializers.ValidationError(
                {"end_date": leave_messages.invalid_date_range()}
            )
        return attrs

    def create(self, validated_data):
        validated_data.pop("reason", None)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        validated_data.pop("reason", None)
        return super().update(instance, validated_data)
