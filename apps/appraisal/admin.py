from django.contrib import admin

from .models import (
    AppraisalAuditLog,
    AppraisalCycle,
    AppraisalPhase4Signoff,
    AppraisalQuestion,
    AppraisalQuestionAnswer,
    AppraisalSection,
    AppraisalSelfPhase1,
    AppraisalSupervisorPhase3,
    AppraisalTemplate,
    AppraisalType,
    EmployeeAppraisal,
)


class AppraisalQuestionInline(admin.TabularInline):
    model = AppraisalQuestion
    extra = 1


class AppraisalSectionInline(admin.StackedInline):
    model = AppraisalSection
    extra = 1


@admin.register(AppraisalType)
class AppraisalTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "is_active", "created_at")
    search_fields = ("name", "code")


@admin.register(AppraisalCycle)
class AppraisalCycleAdmin(admin.ModelAdmin):
    list_display = ("name", "appraisal_type", "start_date", "end_date", "submission_deadline", "status")
    list_filter = ("status", "appraisal_type")
    search_fields = ("name",)


@admin.register(AppraisalTemplate)
class AppraisalTemplateAdmin(admin.ModelAdmin):
    list_display = ("title", "appraisal_type", "department", "is_active", "created_at")
    list_filter = ("appraisal_type", "department", "is_active")
    inlines = [AppraisalSectionInline]


@admin.register(AppraisalSection)
class AppraisalSectionAdmin(admin.ModelAdmin):
    list_display = ("title", "template", "section_code", "weight", "rating_scale_max", "order")
    list_filter = ("template",)
    inlines = [AppraisalQuestionInline]


@admin.register(AppraisalQuestion)
class AppraisalQuestionAdmin(admin.ModelAdmin):
    list_display = ("prompt", "section", "order")
    list_filter = ("section__template", "section")


class AppraisalQuestionAnswerInline(admin.TabularInline):
    model = AppraisalQuestionAnswer
    extra = 0
    readonly_fields = ("question",)


@admin.register(EmployeeAppraisal)
class EmployeeAppraisalAdmin(admin.ModelAdmin):
    list_display = (
        "employee",
        "supervisor",
        "cycle",
        "template",
        "status",
        "self_score_total",
        "supervisor_score_total",
        "performance_category",
        "created_at",
    )
    list_filter = ("status", "performance_category", "cycle")
    search_fields = ("employee__email", "employee__first_name", "employee__last_name")
    inlines = [AppraisalQuestionAnswerInline]


admin.site.register(AppraisalSelfPhase1)
admin.site.register(AppraisalSupervisorPhase3)
admin.site.register(AppraisalPhase4Signoff)
admin.site.register(AppraisalAuditLog)
