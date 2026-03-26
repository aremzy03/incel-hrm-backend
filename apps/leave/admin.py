from django.contrib import admin

from .models import (
    LeaveApprovalLog,
    LeaveBalance,
    LeavePolicy,
    LeaveRequest,
    LeaveType,
    PublicHoliday,
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
    list_display = ("name", "default_days")
    search_fields = ("name",)
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(LeavePolicy)
class LeavePolicyAdmin(admin.ModelAdmin):
    list_display = (
        "leave_type",
        "annual_entitlement",
        "carry_forward",
        "half_day_allowed",
    )
    list_filter = ("leave_type",)
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(PublicHoliday)
class PublicHolidayAdmin(admin.ModelAdmin):
    list_display = ("name", "date", "is_recurring")
    list_filter = ("is_recurring",)
    search_fields = ("name",)
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(LeaveBalance)
class LeaveBalanceAdmin(admin.ModelAdmin):
    list_display = ("employee", "leave_type", "year", "allocated_days", "used_days")
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
        "status",
        "created_at",
    )
    list_filter = ("status", "leave_type", "created_at")
    search_fields = ("employee__email", "employee__first_name")
    readonly_fields = ("id", "total_working_days", "created_at", "updated_at")
    inlines = (LeaveApprovalLogInline,)


@admin.register(LeaveApprovalLog)
class LeaveApprovalLogAdmin(admin.ModelAdmin):
    list_display = ("leave_request", "actor", "action", "previous_status", "new_status", "timestamp")
    list_filter = ("action",)
    search_fields = ("actor__email", "leave_request__employee__email")
    readonly_fields = ("id", "timestamp")
