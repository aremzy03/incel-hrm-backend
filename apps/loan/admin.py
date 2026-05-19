from django.contrib import admin

from .models import (
    LoanApplication,
    LoanApprovalLog,
    LoanRepaymentSchedule,
    LoanType,
)


class LoanRepaymentScheduleInline(admin.TabularInline):
    model = LoanRepaymentSchedule
    extra = 0
    can_delete = False
    show_change_link = True
    readonly_fields = (
        "id",
        "installment_number",
        "due_date",
        "amount_due",
        "created_at",
        "updated_at",
    )
    fields = readonly_fields + ("payment_status",)

    def has_add_permission(self, request, obj=None):
        return False


class LoanApprovalLogInline(admin.TabularInline):
    model = LoanApprovalLog
    extra = 0
    can_delete = False
    show_change_link = True
    readonly_fields = (
        "id",
        "actor",
        "action",
        "comment",
        "previous_status",
        "new_status",
        "timestamp",
    )
    fields = readonly_fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(LoanType)
class LoanTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "description")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(LoanApplication)
class LoanApplicationAdmin(admin.ModelAdmin):
    list_display = (
        "employee_full_name",
        "loan_type",
        "amount",
        "tenure_months",
        "monthly_installment",
        "status",
        "disbursed_at",
        "outstanding_balance",
        "created_at",
    )
    list_filter = ("status", "loan_type", "created_at")
    search_fields = ("employee__email", "employee__first_name", "employee__last_name")
    readonly_fields = (
        "monthly_installment",
        "outstanding_balance",
        "disbursed_at",
        "closed_at",
        "resignation_deducted",
        "created_at",
        "updated_at",
    )
    inlines = (LoanRepaymentScheduleInline, LoanApprovalLogInline)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related("employee", "loan_type")

    @admin.display(description="Employee")
    def employee_full_name(self, obj):
        user = obj.employee
        return user.get_full_name() or user.email


@admin.register(LoanRepaymentSchedule)
class LoanRepaymentScheduleAdmin(admin.ModelAdmin):
    list_display = (
        "loan_employee_display",
        "installment_number",
        "due_date",
        "amount_due",
        "payment_status",
    )
    list_filter = ("due_date", "payment_status")
    search_fields = ("loan__employee__email",)
    readonly_fields = ("id", "created_at", "updated_at")

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related("loan__employee")

    @admin.display(description="Loan")
    def loan_employee_display(self, obj):
        emp = obj.loan.employee
        name = emp.get_full_name() or emp.email
        return f"{name} ({obj.loan_id})"


@admin.register(LoanApprovalLog)
class LoanApprovalLogAdmin(admin.ModelAdmin):
    list_display = (
        "loan",
        "actor",
        "action",
        "previous_status",
        "new_status",
        "timestamp",
    )
    list_filter = ("action",)
    readonly_fields = (
        "id",
        "loan",
        "actor",
        "action",
        "comment",
        "previous_status",
        "new_status",
        "timestamp",
    )

    def has_add_permission(self, request):
        return False
