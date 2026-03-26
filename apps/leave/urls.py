from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    DepartmentCalendarView,
    LeaveBalanceViewSet,
    LeaveRequestViewSet,
    LeaveTypeViewSet,
    PublicHolidayViewSet,
)

router = DefaultRouter()
router.register(r"leave-types", LeaveTypeViewSet, basename="leave-type")
router.register(r"leave-balances", LeaveBalanceViewSet, basename="leave-balance")
router.register(r"leave-requests", LeaveRequestViewSet, basename="leave-request")
router.register(r"public-holidays", PublicHolidayViewSet, basename="public-holiday")

urlpatterns = router.urls + [
    path("calendar/", DepartmentCalendarView.as_view(), name="leave-calendar"),
]
