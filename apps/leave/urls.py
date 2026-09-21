from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    ApproverDelegateViewSet,
    CalendarAssignmentViewSet,
    DepartmentCalendarView,
    HolidayCalendarViewSet,
    LeaveAccrualPreviewView,
    LeaveBalanceViewSet,
    LeaveBlackoutPeriodViewSet,
    LeavePolicyAssignmentViewSet,
    LeavePolicyResolutionView,
    LeavePolicyViewSet,
    LeaveReportsView,
    LeaveRequestViewSet,
    LeaveSettingsView,
    LeaveTypeViewSet,
    LeaveWorkflowTemplateViewSet,
    PublicHolidayViewSet,
    WorkingCalendarViewSet,
)

router = DefaultRouter()
router.register(r"leave-types", LeaveTypeViewSet, basename="leave-type")
router.register(r"leave-policies", LeavePolicyViewSet, basename="leave-policy")
router.register(
    r"leave-policy-assignments",
    LeavePolicyAssignmentViewSet,
    basename="leave-policy-assignment",
)
router.register(r"leave-balances", LeaveBalanceViewSet, basename="leave-balance")
router.register(r"leave-requests", LeaveRequestViewSet, basename="leave-request")
router.register(r"public-holidays", PublicHolidayViewSet, basename="public-holiday")
router.register(r"working-calendars", WorkingCalendarViewSet, basename="working-calendar")
router.register(r"holiday-calendars", HolidayCalendarViewSet, basename="holiday-calendar")
router.register(
    r"leave-calendar-assignments",
    CalendarAssignmentViewSet,
    basename="leave-calendar-assignment",
)
router.register(r"leave-workflows", LeaveWorkflowTemplateViewSet, basename="leave-workflow")
router.register(
    r"leave-approver-delegates",
    ApproverDelegateViewSet,
    basename="leave-approver-delegate",
)
router.register(
    r"leave-blackout-periods",
    LeaveBlackoutPeriodViewSet,
    basename="leave-blackout-period",
)

urlpatterns = router.urls + [
    path("calendar/", DepartmentCalendarView.as_view(), name="leave-calendar"),
    path(
        "leave-policy-resolution/",
        LeavePolicyResolutionView.as_view(),
        name="leave-policy-resolution",
    ),
    path(
        "leave-accrual/preview/",
        LeaveAccrualPreviewView.as_view(),
        name="leave-accrual-preview",
    ),
    path("leave-settings/", LeaveSettingsView.as_view(), name="leave-settings"),
    path(
        "leave-reports/<str:kind>/",
        LeaveReportsView.as_view(),
        name="leave-reports",
    ),
]
