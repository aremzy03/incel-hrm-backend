from django.contrib import admin

from .models import (
    ApproverDelegate,
    CalendarAssignment,
    HolidayCalendar,
    LeaveApprovalLog,
    LeaveBalance,
    LeaveBalanceTransaction,
    LeaveBlackoutPeriod,
    LeavePolicy,
    LeavePolicyAssignment,
    LeaveRequest,
    LeaveSettings,
    LeaveSettingsAuditLog,
    LeaveType,
    LeaveWorkflowStage,
    LeaveWorkflowTemplate,
    PublicHoliday,
    WorkingCalendar,
)


class LeaveApprovalLogInline(admin.TabularInline):
    model = LeaveApprovalLog
    extra = 0
    can_delete = False
    readonly_fields = (
        "actor",
        "action",
        "comment",
        "timestamp",
        "previous_status",
        "new_status",
    )
    fields = readonly_fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(LeaveType)
class LeaveTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "default_days", "is_active", "display_order")
    search_fields = ("name", "code")
    list_filter = ("is_active",)
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(LeavePolicy)
class LeavePolicyAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "leave_type",
        "status",
        "version",
        "annual_entitlement",
        "effective_from",
        "effective_to",
        "carry_forward",
        "accrual_method",
        "prorate_new_joiners",
        "half_day_allowed",
        "reliever_required",
        "overlap_control_enabled",
        "allow_backdated",
        "maximum_backdate_days",
    )
    list_filter = ("status", "leave_type")
    readonly_fields = ("id", "status", "version", "created_at", "updated_at")


@admin.register(LeavePolicyAssignment)
class LeavePolicyAssignmentAdmin(admin.ModelAdmin):
    list_display = (
        "policy",
        "scope_type",
        "scope_id",
        "employee",
        "priority",
        "effective_from",
        "effective_to",
        "is_active",
    )
    list_filter = ("scope_type", "is_active")
    search_fields = ("scope_id", "employee__email", "policy__name")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(PublicHoliday)
class PublicHolidayAdmin(admin.ModelAdmin):
    list_display = ("name", "date", "is_recurring")
    list_filter = ("is_recurring",)
    search_fields = ("name",)
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(WorkingCalendar)
class WorkingCalendarAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "is_org_default", "timezone")
    list_filter = ("is_active", "is_org_default")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(HolidayCalendar)
class HolidayCalendarAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "is_org_default", "timezone")
    list_filter = ("is_active", "is_org_default")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(CalendarAssignment)
class CalendarAssignmentAdmin(admin.ModelAdmin):
    list_display = (
        "employee",
        "department",
        "working_calendar",
        "holiday_calendar",
        "is_active",
    )
    list_filter = ("is_active",)
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(LeaveSettings)
class LeaveSettingsAdmin(admin.ModelAdmin):
    list_display = (
        "leave_year_type",
        "leave_year_start_month",
        "leave_year_start_day",
        "cross_year_deduction_rule",
        "updated_at",
    )
    readonly_fields = ("id", "singleton_key", "created_at", "updated_at")


@admin.register(LeaveBlackoutPeriod)
class LeaveBlackoutPeriodAdmin(admin.ModelAdmin):
    list_display = ("name", "start_date", "end_date", "enforcement", "department", "is_active")
    list_filter = ("enforcement", "is_active")
    filter_horizontal = ("leave_types",)
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(LeaveBalance)
class LeaveBalanceAdmin(admin.ModelAdmin):
    list_display = (
        "employee",
        "leave_type",
        "year",
        "allocated_days",
        "used_days",
        "pending_days",
        "carried_forward_days",
    )
    list_filter = ("leave_type", "year")
    search_fields = ("employee__email", "employee__first_name")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(LeaveRequest)
class LeaveRequestAdmin(admin.ModelAdmin):
    list_display = (
        "employee",
        "leave_type",
        "start_date",
        "end_date",
        "total_working_days",
        "is_half_day",
        "status",
        "is_reconciled",
        "created_at",
    )
    list_filter = ("status", "leave_type", "is_reconciled", "created_at")
    search_fields = ("employee__email", "employee__first_name")
    readonly_fields = (
        "id",
        "total_working_days",
        "department_reminder_sent_at",
        "workflow_snapshot",
        "stage_entered_at",
        "sla_notified_at",
        "is_reconciled",
        "reconciled_by",
        "reconciled_at",
        "created_at",
        "updated_at",
    )
    inlines = (LeaveApprovalLogInline,)


@admin.register(LeaveApprovalLog)
class LeaveApprovalLogAdmin(admin.ModelAdmin):
    list_display = ("leave_request", "actor", "action", "previous_status", "new_status", "timestamp")
    list_filter = ("action",)
    search_fields = ("actor__email", "leave_request__employee__email")
    readonly_fields = ("id", "timestamp")


@admin.register(LeaveBalanceTransaction)
class LeaveBalanceTransactionAdmin(admin.ModelAdmin):
    list_display = (
        "leave_balance",
        "transaction_type",
        "source",
        "delta_used_days",
        "delta_allocated_days",
        "leave_request",
        "actor",
        "created_at",
    )
    list_filter = ("transaction_type", "source", "created_at")
    search_fields = (
        "leave_balance__employee__email",
        "leave_request__employee__email",
        "reason",
    )
    readonly_fields = (
        "id",
        "leave_balance",
        "leave_request",
        "transaction_type",
        "source",
        "delta_used_days",
        "delta_pending_days",
        "delta_allocated_days",
        "idempotency_key",
        "actor",
        "reason",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(LeaveSettingsAuditLog)
class LeaveSettingsAuditLogAdmin(admin.ModelAdmin):
    list_display = (
        "action",
        "object_type",
        "object_id",
        "actor",
        "created_at",
    )
    list_filter = ("action", "object_type")
    search_fields = ("reason", "actor__email")
    readonly_fields = (
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

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class LeaveWorkflowStageInline(admin.TabularInline):
    model = LeaveWorkflowStage
    extra = 0


@admin.register(LeaveWorkflowTemplate)
class LeaveWorkflowTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "is_org_default", "leave_type", "auto_approve_after_sla")
    list_filter = ("is_active", "is_org_default")
    readonly_fields = ("id", "created_at", "updated_at")
    inlines = (LeaveWorkflowStageInline,)


@admin.register(ApproverDelegate)
class ApproverDelegateAdmin(admin.ModelAdmin):
    list_display = ("user", "delegate", "start_date", "end_date", "is_active")
    list_filter = ("is_active",)
    search_fields = ("user__email", "delegate__email")
    readonly_fields = ("id", "created_at", "updated_at")
