"""
Loan management API views.
"""

import csv
import io
import json
from datetime import date, datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Prefetch, Q, Sum
from django.db.models.functions import TruncMonth
from django.http import StreamingHttpResponse
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import RoleName, get_or_create_management_department
from apps.accounts.permissions import IsHR

from .models import (
    LoanApplication,
    LoanApplicationStatus,
    LoanApprovalAction,
    LoanApprovalLog,
    LoanRepaymentSchedule,
    LoanSettings,
    LoanType,
)
from .serializers import (
    LoanApplicationCreateSerializer,
    LoanApplicationLedgerSerializer,
    LoanApplicationReadSerializer,
    LoanApprovalLogSerializer,
    LoanRepaymentSchedulePaymentStatusSerializer,
    LoanSettingsSerializer,
    LoanTypeSerializer,
)
from .services import (
    LoanEligibilityService,
    can_view_all_loans,
    get_loan_settings,
    is_loan_observer,
    is_loan_privileged,
)
from .tasks import (
    notify_loan_approver_required,
    notify_loan_closed,
    notify_loan_decision,
    notify_loan_disbursed,
    notify_loan_liquidated,
    notify_loan_observers,
    notify_next_approver,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TERMINAL_STATUSES = frozenset({
    LoanApplicationStatus.REJECTED,
    LoanApplicationStatus.CLOSED,
    LoanApplicationStatus.LIQUIDATED,
})

_APPROVAL_TRANSITIONS = {
    LoanApplicationStatus.PENDING_MANAGER: (
        LoanApplicationStatus.PENDING_HR,
        RoleName.LINE_MANAGER,
    ),
    LoanApplicationStatus.PENDING_HR: (
        LoanApplicationStatus.PENDING_ED,
        RoleName.HR,
    ),
    LoanApplicationStatus.PENDING_ED: (
        LoanApplicationStatus.PENDING_MD,
        RoleName.EXECUTIVE_DIRECTOR,
    ),
    LoanApplicationStatus.PENDING_MD: (
        LoanApplicationStatus.APPROVED,
        RoleName.MANAGING_DIRECTOR,
    ),
}

_REJECTION_ROLES = {
    LoanApplicationStatus.PENDING_MANAGER: RoleName.LINE_MANAGER,
    LoanApplicationStatus.PENDING_HR: RoleName.HR,
    LoanApplicationStatus.PENDING_ED: RoleName.EXECUTIVE_DIRECTOR,
    LoanApplicationStatus.PENDING_MD: RoleName.MANAGING_DIRECTOR,
}

_EMPLOYEE_PATCH_FIELDS = frozenset({"amount", "tenure_months", "purpose"})
_HR_PATCH_FIELDS = frozenset({"amount", "tenure_months", "purpose", "loan_type"})


def _is_privileged(user) -> bool:
    return is_loan_privileged(user)


def _line_manager_visibility_q(user) -> Q:
    manager_pred = Q(pk__isnull=True)
    if user.has_role(RoleName.LINE_MANAGER):
        if getattr(user, "department_id", None):
            manager_pred = Q(employee__department_id=user.department_id)
        mgmt = get_or_create_management_department()
        if mgmt.line_manager_id == user.pk:
            manager_pred = manager_pred | Q(manager_approver_is_management=True)
    return manager_pred


def _can_view_loan(user, loan) -> bool:
    if loan.employee_id == user.pk:
        return True
    if can_view_all_loans(user):
        return True
    return LoanApplication.objects.filter(
        Q(pk=loan.pk) & _line_manager_visibility_q(user)
    ).exists()


def _enforce_pending_manager_identity(*, loan, user) -> None:
    if loan.status != LoanApplicationStatus.PENDING_MANAGER:
        return
    if loan.manager_approver_is_management:
        mgmt = get_or_create_management_department()
        if mgmt.line_manager_id != user.pk:
            raise PermissionDenied(
                "Only the Management department line manager can act at this stage for this application."
            )
        return
    lm = loan.employee.get_department_line_manager()
    if not lm or lm.pk != user.pk:
        raise PermissionDenied(
            "Only the employee's department line manager can act at this stage."
        )


def _create_loan_log(*, loan, actor, action, previous_status, new_status, comment=""):
    LoanApprovalLog.objects.create(
        loan=loan,
        actor=actor,
        action=action,
        previous_status=previous_status,
        new_status=new_status,
        comment=comment,
    )


def _get_loan(pk):
    LoanEligibilityService.sync_overdue_installments(loan_id=pk)
    return (
        LoanApplication.objects.select_related("employee", "loan_type")
        .prefetch_related("repayment_schedule")
        .get(pk=pk)
    )


User = get_user_model()


def _report_wants_csv(request) -> bool:
    return request.query_params.get("format", "").lower() == "csv"


def _csv_cell(value):
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _streaming_csv_response(filename, fieldnames, row_dicts):
    """Stream CSV rows using the csv module; row_dicts is an iterable of dicts."""

    def chunks():
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=fieldnames)
        writer.writeheader()
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        for row in row_dicts:
            writer.writerow({k: _csv_cell(row.get(k)) for k in fieldnames})
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    response = StreamingHttpResponse(
        chunks(),
        content_type="text/csv; charset=utf-8",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _employee_display_name(user) -> str:
    full = user.get_full_name().strip()
    return full or user.email


class IsHROrLoanObserver(permissions.BasePermission):
    """HR staff or members of the configured loan observer department/unit."""

    message = "You do not have permission to access loan reports."

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        return user.has_role(RoleName.HR) or is_loan_observer(user)


class CanViewEmployeeLoanLedger(permissions.BasePermission):
    """HR, ED, MD, loan observer, or the employee themself."""

    message = "You do not have permission to view this employee's loan ledger."

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        employee_id = view.kwargs.get("employee_id")
        if employee_id is None:
            return False
        if str(user.pk) == str(employee_id):
            return True
        if is_loan_observer(user):
            return True
        return (
            user.has_role(RoleName.HR)
            or user.has_role(RoleName.EXECUTIVE_DIRECTOR)
            or user.has_role(RoleName.MANAGING_DIRECTOR)
        )


# ---------------------------------------------------------------------------
# Loan settings (HR only)
# ---------------------------------------------------------------------------


class LoanSettingsView(APIView):
    """
    GET /api/v1/loan-settings/ — read loan module configuration (HR only)
    PATCH /api/v1/loan-settings/ — update loan module configuration (HR only)
    """

    permission_classes = [permissions.IsAuthenticated, IsHR]

    def get(self, request, *args, **kwargs):
        settings_obj = LoanSettings.get_solo()
        return Response(LoanSettingsSerializer(settings_obj).data)

    def patch(self, request, *args, **kwargs):
        settings_obj = LoanSettings.get_solo()
        serializer = LoanSettingsSerializer(
            settings_obj,
            data=request.data,
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(LoanSettingsSerializer(settings_obj).data)


# ---------------------------------------------------------------------------
# HR reporting (read-only)
# ---------------------------------------------------------------------------


class ReportOutstandingLoansView(APIView):
    """
    GET /api/v1/loans/reports/outstanding/

    Active loans with balances and remaining installment counts.
    Query: ?loan_type=<uuid>&employee=<uuid>&format=csv
    """

    permission_classes = [permissions.IsAuthenticated, IsHROrLoanObserver]

    def get(self, request, *args, **kwargs):
        today = timezone.localdate()
        qs = (
            LoanApplication.objects.filter(status=LoanApplicationStatus.ACTIVE)
            .select_related("employee", "loan_type")
            .annotate(
                remaining_installments_count=Count(
                    "repayment_schedule",
                    filter=Q(repayment_schedule__due_date__gte=today),
                )
            )
            .order_by("employee__last_name", "employee__first_name", "created_at")
        )

        loan_type_id = request.query_params.get("loan_type")
        if loan_type_id:
            qs = qs.filter(loan_type_id=loan_type_id)
        employee_id = request.query_params.get("employee")
        if employee_id:
            qs = qs.filter(employee_id=employee_id)

        def row_dict(loan):
            return {
                "employee_name": _employee_display_name(loan.employee),
                "loan_type": loan.loan_type.name,
                "original_amount": loan.amount,
                "outstanding_balance": loan.outstanding_balance,
                "disbursed_at": loan.disbursed_at,
                "remaining_installments_count": loan.remaining_installments_count,
                "loan_id": str(loan.pk),
            }

        fieldnames = [
            "employee_name",
            "loan_type",
            "original_amount",
            "outstanding_balance",
            "disbursed_at",
            "remaining_installments_count",
            "loan_id",
        ]

        if _report_wants_csv(request):
            return _streaming_csv_response(
                "loan-outstanding-report.csv",
                fieldnames,
                (row_dict(loan) for loan in qs.iterator(chunk_size=100)),
            )

        data = [row_dict(loan) for loan in qs]
        return Response({"results": data})


class ReportScheduleSummaryView(APIView):
    """
    GET /api/v1/loans/reports/schedule-summary/

    Upcoming installments for active loans, grouped by calendar month.
    Query: ?format=csv
    """

    permission_classes = [permissions.IsAuthenticated, IsHROrLoanObserver]

    def get(self, request, *args, **kwargs):
        today = timezone.localdate()
        rows = (
            LoanRepaymentSchedule.objects.filter(
                loan__status=LoanApplicationStatus.ACTIVE,
                due_date__gte=today,
            )
            .annotate(month=TruncMonth("due_date"))
            .values("month")
            .annotate(
                total_amount_due=Sum("amount_due"),
                installment_count=Count("id"),
            )
            .order_by("month")
        )

        def normalize(row):
            m = row["month"]
            if hasattr(m, "strftime"):
                month_label = m.strftime("%Y-%m")
            else:
                month_label = str(m)[:7]
            return {
                "month_label": month_label,
                "total_amount_due": row["total_amount_due"] or Decimal("0"),
                "installment_count": row["installment_count"],
            }

        fieldnames = ["month_label", "total_amount_due", "installment_count"]

        if _report_wants_csv(request):
            return _streaming_csv_response(
                "loan-schedule-summary.csv",
                fieldnames,
                (normalize(r) for r in rows),
            )

        return Response({"results": [normalize(r) for r in rows]})


class ReportEmployeeLoanLedgerView(APIView):
    """
    GET /api/v1/loans/reports/employee-ledger/<employee_id>/

    Full loan history for one employee (applications + schedule + logs).
    Query: ?format=csv
    """

    permission_classes = [permissions.IsAuthenticated, CanViewEmployeeLoanLedger]

    def get(self, request, employee_id, *args, **kwargs):
        if not User.objects.filter(pk=employee_id).exists():
            raise NotFound("Employee not found.")

        LoanEligibilityService.sync_overdue_installments()

        loans_qs = (
            LoanApplication.objects.filter(employee_id=employee_id)
            .select_related("employee", "loan_type")
            .prefetch_related(
                Prefetch(
                    "repayment_schedule",
                    queryset=LoanRepaymentSchedule.objects.order_by("installment_number"),
                ),
                Prefetch(
                    "logs",
                    queryset=LoanApprovalLog.objects.select_related("actor").order_by(
                        "timestamp"
                    ),
                ),
            )
            .order_by("-created_at")
        )

        if _report_wants_csv(request):

            def decimal_json_default(obj):
                if isinstance(obj, Decimal):
                    return str(obj)
                if isinstance(obj, (datetime, date)):
                    return obj.isoformat()
                raise TypeError

            fieldnames = [
                "loan_id",
                "status",
                "loan_type",
                "amount",
                "tenure_months",
                "monthly_installment",
                "outstanding_balance",
                "disbursed_at",
                "closed_at",
                "created_at",
                "repayment_schedule_json",
                "logs_json",
            ]

            def rows():
                for loan in loans_qs:
                    sched = [
                        {
                            "installment_number": i.installment_number,
                            "due_date": i.due_date.isoformat(),
                            "amount_due": str(i.amount_due),
                        }
                        for i in loan.repayment_schedule.all()
                    ]
                    logs = [
                        {
                            "id": str(log.pk),
                            "action": log.action,
                            "comment": log.comment,
                            "previous_status": log.previous_status,
                            "new_status": log.new_status,
                            "timestamp": log.timestamp.isoformat(),
                        }
                        for log in loan.logs.all()
                    ]
                    yield {
                        "loan_id": str(loan.pk),
                        "status": loan.status,
                        "loan_type": loan.loan_type.name,
                        "amount": loan.amount,
                        "tenure_months": loan.tenure_months,
                        "monthly_installment": loan.monthly_installment,
                        "outstanding_balance": loan.outstanding_balance,
                        "disbursed_at": loan.disbursed_at,
                        "closed_at": loan.closed_at,
                        "created_at": loan.created_at,
                        "repayment_schedule_json": json.dumps(sched, default=decimal_json_default),
                        "logs_json": json.dumps(logs, default=decimal_json_default),
                    }

            return _streaming_csv_response(
                f"employee-loan-ledger-{employee_id}.csv",
                fieldnames,
                rows(),
            )

        serializer = LoanApplicationLedgerSerializer(loans_qs, many=True)
        return Response({"employee_id": str(employee_id), "loans": serializer.data})


# ---------------------------------------------------------------------------
# LoanType
# ---------------------------------------------------------------------------

class LoanTypeViewSet(viewsets.ReadOnlyModelViewSet):
    """GET /api/v1/loan-types/ — any authenticated user."""

    queryset = LoanType.objects.all()
    serializer_class = LoanTypeSerializer
    permission_classes = [permissions.IsAuthenticated]


# ---------------------------------------------------------------------------
# LoanApplication
# ---------------------------------------------------------------------------

class LoanApplicationViewSet(viewsets.ModelViewSet):
    """
    Loan applications: list/create/retrieve/patch plus approval workflow actions.

    DELETE is not supported.
    """

    permission_classes = [permissions.IsAuthenticated]
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_serializer_class(self):
        if self.action in ("create", "partial_update"):
            return LoanApplicationCreateSerializer
        return LoanApplicationReadSerializer

    def get_queryset(self):
        user = self.request.user
        qs = (
            LoanApplication.objects.select_related("employee", "loan_type", "employee__department")
            .prefetch_related("repayment_schedule")
            .all()
        )

        if can_view_all_loans(user):
            employee_id = self.request.query_params.get("employee")
            if employee_id:
                qs = qs.filter(employee_id=employee_id)
        else:
            visible_q = Q(employee=user) | _line_manager_visibility_q(user)
            qs = qs.filter(visible_q).distinct()

        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)

        loan_type_id = self.request.query_params.get("loan_type")
        if loan_type_id:
            qs = qs.filter(loan_type_id=loan_type_id)

        return qs

    def update(self, request, *args, **kwargs):
        if not kwargs.get("partial", False):
            return Response(
                {"detail": "PUT is not supported. Use PATCH for partial updates."},
                status=status.HTTP_405_METHOD_NOT_ALLOWED,
            )
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        return Response(
            {"detail": "DELETE is not supported."},
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            loan = serializer.save()
            _create_loan_log(
                loan=loan,
                actor=request.user,
                action=LoanApprovalAction.SUBMIT,
                previous_status="",
                new_status=LoanApplicationStatus.DRAFT,
                comment="Application created.",
            )

        return Response(
            LoanApplicationReadSerializer(loan).data,
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, *args, **kwargs):
        loan = _get_loan(kwargs["pk"])
        user = request.user
        is_hr = user.has_role(RoleName.HR) or user.is_staff
        is_owner = loan.employee == user

        if not (is_hr or is_owner):
            raise PermissionDenied("You do not have permission to modify this loan application.")

        if is_owner and not is_hr:
            if loan.status != LoanApplicationStatus.DRAFT:
                raise ValidationError(
                    {
                        "status": (
                            "You can only edit your own loan applications while they are in "
                            f"DRAFT status. Current status: {loan.status}."
                        )
                    }
                )
            disallowed = set(request.data.keys()) - _EMPLOYEE_PATCH_FIELDS
            if disallowed:
                raise ValidationError(
                    {
                        "detail": (
                            "You may only update amount, tenure_months, and purpose "
                            f"while in DRAFT. Invalid fields: {sorted(disallowed)}"
                        )
                    }
                )
        elif is_hr:
            if loan.status in _TERMINAL_STATUSES:
                raise ValidationError(
                    {
                        "status": (
                            f"Cannot edit a loan application in terminal status "
                            f"'{loan.status}'."
                        )
                    }
                )
            disallowed = set(request.data.keys()) - _HR_PATCH_FIELDS
            if disallowed:
                raise ValidationError(
                    {
                        "detail": (
                            "HR may only update amount, tenure_months, purpose, and loan_type. "
                            f"Invalid fields: {sorted(disallowed)}"
                        )
                    }
                )

        return super().partial_update(request, *args, **kwargs)

    def list(self, request, *args, **kwargs):
        LoanEligibilityService.sync_overdue_installments()
        return super().list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        loan = _get_loan(kwargs["pk"])
        if not _can_view_loan(request.user, loan):
            raise PermissionDenied("You do not have permission to view this loan application.")
        serializer = self.get_serializer(loan)
        return Response(serializer.data)

    # ------------------------------------------------------------------
    # submit — Employee: DRAFT → PENDING_MANAGER or PENDING_HR
    # ------------------------------------------------------------------

    @action(detail=True, methods=["post"], url_path="submit")
    def submit(self, request, pk=None):
        loan = _get_loan(pk)

        if loan.employee != request.user:
            raise PermissionDenied("You can only submit your own loan applications.")

        if loan.status != LoanApplicationStatus.DRAFT:
            raise ValidationError(
                {
                    "status": (
                        f"Only DRAFT applications can be submitted. Current status: {loan.status}."
                    )
                }
            )

        LoanEligibilityService.check_eligibility(request.user)

        loan_settings = get_loan_settings()
        employee = request.user
        manager_approver_is_management = False

        if loan_settings.require_line_manager_approval:
            new_status = LoanApplicationStatus.PENDING_MANAGER
            if employee.has_role(RoleName.LINE_MANAGER):
                manager_approver_is_management = True
            if manager_approver_is_management:
                mgmt = get_or_create_management_department()
                if mgmt.line_manager_id is None:
                    raise ValidationError(
                        {
                            "department": (
                                "Management department has no line manager assigned. Contact HR."
                            )
                        }
                    )
            else:
                lm = employee.get_department_line_manager()
                if lm is None:
                    raise ValidationError(
                        {
                            "department": (
                                "Your department has no line manager assigned. Contact HR."
                            )
                        }
                    )
        else:
            new_status = LoanApplicationStatus.PENDING_HR

        prev_status = loan.status

        with transaction.atomic():
            loan.status = new_status
            loan.manager_approver_is_management = manager_approver_is_management
            loan.save(
                update_fields=["status", "manager_approver_is_management", "updated_at"]
            )
            _create_loan_log(
                loan=loan,
                actor=request.user,
                action=LoanApprovalAction.SUBMIT,
                previous_status=prev_status,
                new_status=new_status,
                comment=request.data.get("comment", "Submitted for approval."),
            )

        loan_id = str(loan.id)
        if loan_settings.require_line_manager_approval:
            transaction.on_commit(
                lambda lid=loan_id: notify_loan_approver_required.delay(lid)
            )
        else:
            transaction.on_commit(
                lambda lid=loan_id: notify_loan_approver_required.delay(lid)
            )
            transaction.on_commit(lambda lid=loan_id: notify_loan_observers.delay(lid))

        return Response(LoanApplicationReadSerializer(loan).data)

    # ------------------------------------------------------------------
    # approve — LM → HR → ED → MD → APPROVED
    # ------------------------------------------------------------------

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None):
        loan = _get_loan(pk)

        if loan.status not in _APPROVAL_TRANSITIONS:
            raise ValidationError(
                {
                    "status": (
                        f"Application cannot be approved from status '{loan.status}'. "
                        f"Approvable statuses: {list(_APPROVAL_TRANSITIONS)}"
                    )
                }
            )

        next_status, required_role = _APPROVAL_TRANSITIONS[loan.status]
        user = request.user

        if not user.has_role(required_role):
            raise PermissionDenied(
                f"Only a user with role '{required_role}' can approve at this stage "
                f"(current status: {loan.status})."
            )

        _enforce_pending_manager_identity(loan=loan, user=user)

        comment = request.data.get("comment", "").strip()
        if loan.status == LoanApplicationStatus.PENDING_MD and not comment:
            raise ValidationError(
                {"comment": "A comment is required when the Managing Director approves."}
            )

        prev_status = loan.status

        with transaction.atomic():
            loan.status = next_status
            loan.save(update_fields=["status", "updated_at"])
            _create_loan_log(
                loan=loan,
                actor=request.user,
                action=LoanApprovalAction.APPROVE,
                previous_status=prev_status,
                new_status=next_status,
                comment=comment,
            )

        loan_id = str(loan.id)
        if next_status == LoanApplicationStatus.APPROVED:
            transaction.on_commit(
                lambda lid=loan_id, c=comment: notify_loan_decision.delay(
                    lid,
                    LoanApplicationStatus.APPROVED,
                    c,
                )
            )
        elif next_status == LoanApplicationStatus.PENDING_HR:
            transaction.on_commit(
                lambda lid=loan_id: notify_loan_approver_required.delay(lid)
            )
            transaction.on_commit(lambda lid=loan_id: notify_loan_observers.delay(lid))
        elif next_status in (
            LoanApplicationStatus.PENDING_ED,
            LoanApplicationStatus.PENDING_MD,
        ):
            transaction.on_commit(lambda lid=loan_id: notify_next_approver.delay(lid))

        return Response(LoanApplicationReadSerializer(loan).data)

    # ------------------------------------------------------------------
    # reject — Stage approver (comment required)
    # ------------------------------------------------------------------

    @action(detail=True, methods=["post"], url_path="reject")
    def reject(self, request, pk=None):
        loan = _get_loan(pk)

        comment = request.data.get("comment", "").strip()
        if not comment:
            raise ValidationError({"comment": "A comment is required when rejecting a loan application."})

        if loan.status not in _REJECTION_ROLES:
            raise ValidationError(
                {
                    "status": (
                        f"Application cannot be rejected from status '{loan.status}'. "
                        f"Rejectable statuses: {list(_REJECTION_ROLES)}"
                    )
                }
            )

        required_role = _REJECTION_ROLES[loan.status]
        if not request.user.has_role(required_role):
            raise PermissionDenied(
                f"Only a user with role '{required_role}' can reject at this stage."
            )

        _enforce_pending_manager_identity(loan=loan, user=request.user)

        prev_status = loan.status

        with transaction.atomic():
            loan.status = LoanApplicationStatus.REJECTED
            loan.save(update_fields=["status", "updated_at"])
            _create_loan_log(
                loan=loan,
                actor=request.user,
                action=LoanApprovalAction.REJECT,
                previous_status=prev_status,
                new_status=LoanApplicationStatus.REJECTED,
                comment=comment,
            )

        transaction.on_commit(
            lambda lid=str(loan.id), c=comment: notify_loan_decision.delay(
                lid,
                LoanApplicationStatus.REJECTED,
                c,
            )
        )

        return Response(LoanApplicationReadSerializer(loan).data)

    # ------------------------------------------------------------------
    # disburse — HR: APPROVED → ACTIVE
    # ------------------------------------------------------------------

    @action(detail=True, methods=["post"], url_path="disburse", permission_classes=[permissions.IsAuthenticated, IsHR])
    def disburse(self, request, pk=None):
        loan = _get_loan(pk)

        if loan.status != LoanApplicationStatus.APPROVED:
            raise ValidationError(
                {
                    "status": (
                        f"Only APPROVED loans can be disbursed. Current status: {loan.status}."
                    )
                }
            )

        prev_status = loan.status
        new_status = LoanApplicationStatus.ACTIVE
        now = timezone.now()

        with transaction.atomic():
            loan.status = new_status
            loan.disbursed_at = now
            loan.save(update_fields=["status", "disbursed_at", "updated_at"])
            LoanEligibilityService.generate_repayment_schedule(loan)
            loan.refresh_from_db()
            _create_loan_log(
                loan=loan,
                actor=request.user,
                action=LoanApprovalAction.DISBURSE,
                previous_status=prev_status,
                new_status=new_status,
                comment=request.data.get("comment", ""),
            )

        transaction.on_commit(lambda: notify_loan_disbursed.delay(str(loan.id)))

        return Response(LoanApplicationReadSerializer(loan).data)

    # ------------------------------------------------------------------
    # liquidate — HR: ACTIVE → LIQUIDATED
    # ------------------------------------------------------------------

    @action(detail=True, methods=["post"], url_path="liquidate", permission_classes=[permissions.IsAuthenticated, IsHR])
    def liquidate(self, request, pk=None):
        loan = _get_loan(pk)

        if loan.status != LoanApplicationStatus.ACTIVE:
            raise ValidationError(
                {
                    "status": (
                        f"Only ACTIVE loans can be liquidated. Current status: {loan.status}."
                    )
                }
            )

        prev_status = loan.status
        new_status = LoanApplicationStatus.LIQUIDATED
        now = timezone.now()

        with transaction.atomic():
            loan.status = new_status
            loan.outstanding_balance = 0
            loan.closed_at = now
            loan.save(
                update_fields=["status", "outstanding_balance", "closed_at", "updated_at"]
            )
            _create_loan_log(
                loan=loan,
                actor=request.user,
                action=LoanApprovalAction.LIQUIDATE,
                previous_status=prev_status,
                new_status=new_status,
                comment=request.data.get("comment", ""),
            )

        transaction.on_commit(lambda: notify_loan_liquidated.delay(str(loan.id)))

        return Response(LoanApplicationReadSerializer(loan).data)

    # ------------------------------------------------------------------
    # handle_resignation — HR: ACTIVE → CLOSED
    # ------------------------------------------------------------------

    @action(
        detail=True,
        methods=["post"],
        url_path="handle-resignation",
        permission_classes=[permissions.IsAuthenticated, IsHR],
    )
    def handle_resignation(self, request, pk=None):
        loan = _get_loan(pk)

        if loan.status != LoanApplicationStatus.ACTIVE:
            raise ValidationError(
                {
                    "status": (
                        f"Only ACTIVE loans can be closed on resignation. "
                        f"Current status: {loan.status}."
                    )
                }
            )

        prev_status = loan.status
        new_status = LoanApplicationStatus.CLOSED
        now = timezone.now()
        close_comment = "Deducted from final entitlement"

        with transaction.atomic():
            loan.status = new_status
            loan.resignation_deducted = True
            loan.outstanding_balance = 0
            loan.closed_at = now
            loan.save(
                update_fields=[
                    "status",
                    "resignation_deducted",
                    "outstanding_balance",
                    "closed_at",
                    "updated_at",
                ]
            )
            _create_loan_log(
                loan=loan,
                actor=request.user,
                action=LoanApprovalAction.CLOSE,
                previous_status=prev_status,
                new_status=new_status,
                comment=close_comment,
            )

        transaction.on_commit(lambda: notify_loan_closed.delay(str(loan.id)))

        return Response(LoanApplicationReadSerializer(loan).data)

    # ------------------------------------------------------------------
    # repayment schedule — HR: mark installment Pending / Paid / Overdue
    # ------------------------------------------------------------------

    @action(
        detail=True,
        methods=["patch"],
        url_path=r"repayment-schedule/(?P<schedule_id>[^/.]+)",
        permission_classes=[permissions.IsAuthenticated, IsHR],
    )
    def update_repayment_schedule_item(self, request, pk=None, schedule_id=None):
        loan = _get_loan(pk)

        if loan.status != LoanApplicationStatus.ACTIVE:
            raise ValidationError(
                {
                    "status": (
                        "Payment status can only be updated while the loan is ACTIVE. "
                        f"Current status: {loan.status}."
                    )
                }
            )

        try:
            schedule_item = loan.repayment_schedule.get(pk=schedule_id)
        except LoanRepaymentSchedule.DoesNotExist:
            raise NotFound("Repayment schedule installment not found for this loan.")

        serializer = LoanRepaymentSchedulePaymentStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            schedule_item.payment_status = serializer.validated_data["payment_status"]
            schedule_item.save(update_fields=["payment_status", "updated_at"])
            loan.refresh_from_db()
            LoanEligibilityService.recalculate_outstanding_balance(loan)

        loan = _get_loan(pk)
        return Response(LoanApplicationReadSerializer(loan).data)

    # ------------------------------------------------------------------
    # logs — HR, ED, MD, observers
    # ------------------------------------------------------------------

    @action(detail=True, methods=["get"], url_path="logs")
    def logs(self, request, pk=None):
        loan = _get_loan(pk)
        if not _can_view_loan(request.user, loan):
            raise PermissionDenied("You do not have permission to view loan approval logs.")

        logs_qs = loan.logs.select_related("actor").all()
        serializer = LoanApprovalLogSerializer(logs_qs, many=True)
        return Response(serializer.data)
