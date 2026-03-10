"""
Leave management API views.

Viewset summary
---------------
LeaveTypeViewSet           – Full CRUD for HR/Admin; list/retrieve for any authenticated
LeaveBalanceViewSet        – ReadOnly, role-filtered queryset + ?employee=&year= filters
LeaveRequestViewSet        – Full CRUD minus DELETE, role-filtered queryset
  custom actions:
    POST  submit/:id/      – Employee: DRAFT → PENDING_MANAGER
    POST  approve/:id/     – Stage-based role transitions
    POST  reject/:id/      – Matching approver at current stage (comment required)
    POST  cancel/:id/      – Employee (own DRAFT/PENDING_MANAGER) or HR (any active)
    GET   logs/:id/        – Approval audit trail (HR, Manager, ED, or request owner)
DepartmentCalendarView     – GET /api/v1/calendar/  dept-scoped approved leave
"""

import datetime

from django.db import transaction
from django.db.models import Q
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import RoleName
from apps.accounts.permissions import IsEmployee, IsHR

from .models import (
    ApprovalAction,
    LeaveApprovalLog,
    LeaveBalance,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveType,
)
from .serializers import (
    CalendarEntrySerializer,
    LeaveApprovalLogSerializer,
    LeaveBalanceSerializer,
    LeaveRequestCreateSerializer,
    LeaveRequestReadSerializer,
    LeaveTypeSerializer,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_privileged(user) -> bool:
    """True for staff, HR, ED, or MD — can see/act on any request."""
    return (
        user.is_staff
        or user.has_role(RoleName.HR)
        or user.has_role(RoleName.EXECUTIVE_DIRECTOR)
        or user.has_role(RoleName.MANAGING_DIRECTOR)
    )


def _create_log(*, leave_request, actor, action, previous_status, new_status, comment=""):
    LeaveApprovalLog.objects.create(
        leave_request=leave_request,
        actor=actor,
        action=action,
        previous_status=previous_status,
        new_status=new_status,
        comment=comment,
    )


def _deduct_leave_balance(leave_request) -> None:
    """
    Atomically add total_working_days to used_days for the employee's balance.
    Uses F() to avoid a read-modify-write race condition.
    """
    from django.db.models import F

    LeaveBalance.objects.filter(
        employee=leave_request.employee,
        leave_type=leave_request.leave_type,
        year=leave_request.start_date.year,
    ).update(used_days=F("used_days") + leave_request.total_working_days)


# ---------------------------------------------------------------------------
# LeaveType
# ---------------------------------------------------------------------------

class LeaveTypeViewSet(viewsets.ModelViewSet):
    """
    GET    /api/v1/leave-types/       — any authenticated user
    POST   /api/v1/leave-types/       — HR or admin
    GET    /api/v1/leave-types/:id/  — any authenticated user
    PUT    /api/v1/leave-types/:id/  — HR or admin
    PATCH  /api/v1/leave-types/:id/  — HR or admin
    DELETE /api/v1/leave-types/:id/  — HR or admin
    """

    queryset = LeaveType.objects.all()
    serializer_class = LeaveTypeSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [permissions.IsAuthenticated()]
        return [permissions.IsAuthenticated(), (IsHR | permissions.IsAdminUser)()]


# ---------------------------------------------------------------------------
# LeaveBalance
# ---------------------------------------------------------------------------

class LeaveBalanceViewSet(viewsets.ReadOnlyModelViewSet):
    """
    GET /api/v1/leave-balances/
    Supports ?employee=<uuid>&year=<int> query params.
    Privileged roles (HR, Line Manager, ED, MD, staff) see all records.
    Regular employees see only their own.
    """

    serializer_class = LeaveBalanceSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = LeaveBalance.objects.select_related("employee", "leave_type").all()

        if not (
            user.is_staff
            or user.has_role(RoleName.HR)
            or user.has_role(RoleName.LINE_MANAGER)
            or user.has_role(RoleName.EXECUTIVE_DIRECTOR)
            or user.has_role(RoleName.MANAGING_DIRECTOR)
        ):
            qs = qs.filter(employee=user)

        employee_id = self.request.query_params.get("employee")
        year = self.request.query_params.get("year")

        if employee_id:
            qs = qs.filter(employee_id=employee_id)
        if year:
            qs = qs.filter(year=year)

        return qs


# ---------------------------------------------------------------------------
# LeaveRequest
# ---------------------------------------------------------------------------

# Approval stage machine: current_status → (next_status, required_role)
_APPROVAL_TRANSITIONS = {
    LeaveRequestStatus.PENDING_MANAGER: (
        LeaveRequestStatus.PENDING_HR,
        RoleName.LINE_MANAGER,
    ),
    LeaveRequestStatus.PENDING_HR: (
        LeaveRequestStatus.PENDING_ED,
        RoleName.HR,
    ),
    LeaveRequestStatus.PENDING_ED: (
        LeaveRequestStatus.APPROVED,
        RoleName.EXECUTIVE_DIRECTOR,
    ),
}

# Rejection map: current_status → required_role
_REJECTION_ROLES = {
    LeaveRequestStatus.PENDING_MANAGER: RoleName.LINE_MANAGER,
    LeaveRequestStatus.PENDING_HR: RoleName.HR,
    LeaveRequestStatus.PENDING_ED: RoleName.EXECUTIVE_DIRECTOR,
}


class LeaveRequestViewSet(viewsets.ModelViewSet):
    """
    Viewset for leave requests.

    Queryset scoping by role:
      - Privileged (HR / ED / MD / staff): all requests
      - Line Manager: own department's requests
      - Employee: own requests only

    Serializer selection:
      - write actions (create, partial_update): LeaveRequestCreateSerializer
      - read actions: LeaveRequestReadSerializer

    HTTP method restrictions:
      - PUT  → 405 (use PATCH)
      - DELETE → 405 (use cancel action)
    """

    permission_classes = [permissions.IsAuthenticated]

    def get_permissions(self):
        if self.action == "partial_update":
            return [permissions.IsAuthenticated(), IsHR()]
        return [permissions.IsAuthenticated()]

    def get_serializer_class(self):
        if self.action in ("create", "partial_update"):
            return LeaveRequestCreateSerializer
        return LeaveRequestReadSerializer

    def get_queryset(self):
        user = self.request.user
        qs = LeaveRequest.objects.select_related("employee", "leave_type", "cover_person").all()

        if _is_privileged(user):
            return qs

        if user.has_role(RoleName.LINE_MANAGER) and user.department_id:
            return qs.filter(employee__department_id=user.department_id)

        return qs.filter(employee=user)

    # ------------------------------------------------------------------
    # Blocked HTTP methods
    # ------------------------------------------------------------------

    def update(self, request, *args, **kwargs):
        if not kwargs.get("partial"):
            return Response(
                {"detail": "PUT is not supported. Use PATCH for partial updates."},
                status=status.HTTP_405_METHOD_NOT_ALLOWED,
            )
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        return Response(
            {"detail": "DELETE is not supported. Use the cancel action instead."},
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )

    # ------------------------------------------------------------------
    # submit — Employee: DRAFT → PENDING_MANAGER
    # ------------------------------------------------------------------

    @action(detail=True, methods=["post"], url_path="submit")
    def submit(self, request, pk=None):
        leave_request = self.get_object()

        if leave_request.employee != request.user:
            raise PermissionDenied("You can only submit your own leave requests.")

        if leave_request.status != LeaveRequestStatus.DRAFT:
            raise ValidationError(
                {"status": f"Only DRAFT requests can be submitted. Current status: {leave_request.status}."}
            )

        lm = leave_request.employee.get_department_line_manager()
        if lm is None:
            raise ValidationError(
                {"department": "Your department has no line manager assigned. Contact HR."}
            )

        prev_status = leave_request.status
        leave_request.status = LeaveRequestStatus.PENDING_MANAGER
        leave_request.save(update_fields=["status", "updated_at"])

        _create_log(
            leave_request=leave_request,
            actor=request.user,
            action=ApprovalAction.MODIFY,
            previous_status=prev_status,
            new_status=LeaveRequestStatus.PENDING_MANAGER,
            comment="Submitted for manager approval.",
        )

        return Response(LeaveRequestReadSerializer(leave_request).data)

    # ------------------------------------------------------------------
    # approve — Stage-based transitions with role enforcement
    # ------------------------------------------------------------------

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None):
        leave_request = self.get_object()

        if leave_request.status not in _APPROVAL_TRANSITIONS:
            raise ValidationError(
                {
                    "status": (
                        f"Request cannot be approved from status '{leave_request.status}'. "
                        f"Approvable statuses: {list(_APPROVAL_TRANSITIONS)}"
                    )
                }
            )

        next_status, required_role = _APPROVAL_TRANSITIONS[leave_request.status]

        if not request.user.has_role(required_role):
            raise PermissionDenied(
                f"Only a user with role '{required_role}' can approve at this stage "
                f"(current status: {leave_request.status})."
            )

        prev_status = leave_request.status

        with transaction.atomic():
            leave_request.status = next_status
            leave_request.save(update_fields=["status", "updated_at"])

            if next_status == LeaveRequestStatus.APPROVED:
                _deduct_leave_balance(leave_request)

            _create_log(
                leave_request=leave_request,
                actor=request.user,
                action=ApprovalAction.APPROVE,
                previous_status=prev_status,
                new_status=next_status,
                comment=request.data.get("comment", ""),
            )

        return Response(LeaveRequestReadSerializer(leave_request).data)

    # ------------------------------------------------------------------
    # reject — Role-matched rejection at current stage (comment required)
    # ------------------------------------------------------------------

    @action(detail=True, methods=["post"], url_path="reject")
    def reject(self, request, pk=None):
        leave_request = self.get_object()

        comment = request.data.get("comment", "").strip()
        if not comment:
            raise ValidationError({"comment": "A comment is required when rejecting a request."})

        if leave_request.status not in _REJECTION_ROLES:
            raise ValidationError(
                {
                    "status": (
                        f"Request cannot be rejected from status '{leave_request.status}'. "
                        f"Rejectable statuses: {list(_REJECTION_ROLES)}"
                    )
                }
            )

        required_role = _REJECTION_ROLES[leave_request.status]
        if not request.user.has_role(required_role):
            raise PermissionDenied(
                f"Only a user with role '{required_role}' can reject at this stage."
            )

        prev_status = leave_request.status

        with transaction.atomic():
            leave_request.status = LeaveRequestStatus.REJECTED
            leave_request.save(update_fields=["status", "updated_at"])

            _create_log(
                leave_request=leave_request,
                actor=request.user,
                action=ApprovalAction.REJECT,
                previous_status=prev_status,
                new_status=LeaveRequestStatus.REJECTED,
                comment=comment,
            )

        return Response(LeaveRequestReadSerializer(leave_request).data)

    # ------------------------------------------------------------------
    # cancel — Employee (own DRAFT/PENDING_MANAGER) or HR (any active)
    # ------------------------------------------------------------------

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, pk=None):
        leave_request = self.get_object()
        user = request.user

        is_hr = user.is_staff or user.has_role(RoleName.HR)
        is_owner = leave_request.employee == user

        terminal_statuses = {LeaveRequestStatus.REJECTED, LeaveRequestStatus.CANCELLED}

        if leave_request.status in terminal_statuses:
            raise ValidationError(
                {"status": f"Request is already {leave_request.status} and cannot be cancelled."}
            )

        if is_hr:
            pass  # HR can cancel any non-terminal request
        elif is_owner:
            allowed = {LeaveRequestStatus.DRAFT, LeaveRequestStatus.PENDING_MANAGER}
            if leave_request.status not in allowed:
                raise ValidationError(
                    {
                        "status": (
                            "You can only cancel requests in DRAFT or PENDING_MANAGER status. "
                            f"Current status: {leave_request.status}."
                        )
                    }
                )
        else:
            raise PermissionDenied("You do not have permission to cancel this request.")

        prev_status = leave_request.status

        with transaction.atomic():
            leave_request.status = LeaveRequestStatus.CANCELLED
            leave_request.save(update_fields=["status", "updated_at"])

            _create_log(
                leave_request=leave_request,
                actor=user,
                action=ApprovalAction.CANCEL,
                previous_status=prev_status,
                new_status=LeaveRequestStatus.CANCELLED,
                comment=request.data.get("comment", ""),
            )

        return Response(LeaveRequestReadSerializer(leave_request).data)

    # ------------------------------------------------------------------
    # logs — Approval audit trail
    # ------------------------------------------------------------------

    @action(detail=True, methods=["get"], url_path="logs")
    def logs(self, request, pk=None):
        leave_request = self.get_object()
        user = request.user

        can_view = (
            _is_privileged(user)
            or user.has_role(RoleName.LINE_MANAGER)
            or leave_request.employee == user
        )
        if not can_view:
            raise PermissionDenied("You do not have permission to view the approval log.")

        logs_qs = leave_request.logs.select_related("actor").all()
        serializer = LeaveApprovalLogSerializer(logs_qs, many=True)
        return Response(serializer.data)


# ---------------------------------------------------------------------------
# Department Calendar
# ---------------------------------------------------------------------------

_PRIVILEGED_ROLES = frozenset({
    RoleName.HR,
    RoleName.EXECUTIVE_DIRECTOR,
    RoleName.MANAGING_DIRECTOR,
})


class DepartmentCalendarView(APIView):
    """
    GET /api/v1/calendar/?year=<int>&month=<int>[&department=<uuid>]

    Returns approved leave requests scoped by the caller's role:
      - Employee / Line Manager → own department only
      - HR / ED / MD / staff    → all departments (optionally filtered)
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        today = datetime.date.today()
        year = int(request.query_params.get("year", today.year))
        month = request.query_params.get("month")

        qs = (
            LeaveRequest.objects
            .filter(status=LeaveRequestStatus.APPROVED)
            .select_related("employee__department", "leave_type")
        )

        if month:
            month = int(month)
            period_start = datetime.date(year, month, 1)
            if month == 12:
                period_end = datetime.date(year + 1, 1, 1)
            else:
                period_end = datetime.date(year, month + 1, 1)
            qs = qs.filter(
                Q(start_date__lt=period_end) & Q(end_date__gte=period_start)
            )
        else:
            qs = qs.filter(
                Q(start_date__year=year) | Q(end_date__year=year)
            )

        has_privilege = (
            user.is_staff
            or any(user.has_role(r) for r in _PRIVILEGED_ROLES)
        )

        if has_privilege:
            dept_filter = request.query_params.get("department")
            if dept_filter:
                qs = qs.filter(employee__department_id=dept_filter)
        else:
            if not user.department_id:
                return Response([])
            qs = qs.filter(employee__department_id=user.department_id)

        qs = qs.order_by("start_date")
        serializer = CalendarEntrySerializer(qs, many=True)
        return Response(serializer.data)
