from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    LoanApplicationViewSet,
    LoanSettingsView,
    LoanTypeViewSet,
    ReportEmployeeLoanLedgerView,
    ReportOutstandingLoansView,
    ReportScheduleSummaryView,
)

router = DefaultRouter()
router.register(r"loan-types", LoanTypeViewSet, basename="loan-type")
router.register(r"loan-applications", LoanApplicationViewSet, basename="loan-application")

urlpatterns = [
    path(
        "loan-settings/",
        LoanSettingsView.as_view(),
        name="loan-settings",
    ),
    path(
        "loans/reports/outstanding/",
        ReportOutstandingLoansView.as_view(),
        name="loan-report-outstanding",
    ),
    path(
        "loans/reports/schedule-summary/",
        ReportScheduleSummaryView.as_view(),
        name="loan-report-schedule-summary",
    ),
    path(
        "loans/reports/employee-ledger/<uuid:employee_id>/",
        ReportEmployeeLoanLedgerView.as_view(),
        name="loan-report-employee-ledger",
    ),
] + router.urls
