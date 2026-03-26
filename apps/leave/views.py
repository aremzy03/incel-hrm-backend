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

import csv
import datetime
import io

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
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
    PublicHoliday,
)
from .serializers import (
    CalendarEntrySerializer,
    LeaveApprovalLogSerializer,
    LeaveBalanceSerializer,
    LeaveRequestCreateSerializer,
    LeaveRequestReadSerializer,
    LeaveTypeSerializer,
    PublicHolidaySerializer,
)
from .tasks import (
    notify_approver_required,
    notify_leave_decision,
    notify_leave_submitted,
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
    Each authenticated user can only see their own balances.
    """

    serializer_class = LeaveBalanceSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = LeaveBalance.objects.select_related("employee", "leave_type")
        return qs.filter(employee=user)


# ---------------------------------------------------------------------------
# PublicHoliday
# ---------------------------------------------------------------------------

class PublicHolidayViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = PublicHolidaySerializer

    def get_queryset(self):
        qs = PublicHoliday.objects.all().order_by("date")
        year = self.request.query_params.get("year")
        if year:
            try:
                year_int = int(year)
            except ValueError:
                raise ValidationError({"year": "year must be an integer."})
            qs = qs.filter(Q(is_recurring=True) | Q(date__year=year_int))
        return qs

    @action(
        detail=False,
        methods=["post"],
        url_path="upload",
        permission_classes=[permissions.IsAuthenticated, IsHR | permissions.IsAdminUser],
    )
    def upload(self, request):
        """
        POST /api/v1/public-holidays/upload/
        Multipart form-data with file field: `file`
        CSV columns: name,date   (date format YYYY-MM-DD)
        Upserts by `date`.
        """
        upload_file = request.FILES.get("file")
        if not upload_file:
            raise ValidationError({"file": "CSV file is required (multipart field 'file')."})

        try:
            text = upload_file.read().decode("utf-8-sig")
        except Exception:
            raise ValidationError({"file": "Unable to read file as UTF-8 text."})

        reader = csv.DictReader(io.StringIO(text))
        required = {"name", "date"}
        if not reader.fieldnames or not required.issubset(set(h.strip() for h in reader.fieldnames)):
            raise ValidationError({"file": "CSV header must include: name,date"})

        created = 0
        updated = 0
        errors = []

        for idx, row in enumerate(reader, start=2):  # header is line 1
            name = (row.get("name") or "").strip()
            date_str = (row.get("date") or "").strip()

            if not name or not date_str:
                errors.append({"line": idx, "error": "name and date are required"})
                continue

            try:
                date = datetime.date.fromisoformat(date_str)
            except ValueError:
                errors.append({"line": idx, "error": "date must be YYYY-MM-DD"})
                continue

            obj, was_created = PublicHoliday.objects.update_or_create(
                date=date,
                defaults={"name": name, "is_recurring": False},
            )
            if was_created:
                created += 1
            else:
                updated += 1

        return Response({"created": created, "updated": updated, "errors": errors})


# ---------------------------------------------------------------------------
# LeaveRequest
# ---------------------------------------------------------------------------

# Approval stage machine: current_status → (next_status, required_role)
_APPROVAL_TRANSITIONS = {
    LeaveRequestStatus.PENDING_SUPERVISOR: (
        LeaveRequestStatus.PENDING_MANAGER,
        RoleName.SUPERVISOR,
    ),
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
    LeaveRequestStatus.PENDING_SUPERVISOR: RoleName.SUPERVISOR,
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
        # All leave request actions require authentication. Additional
        # per-action checks are enforced inside the view methods.
        return [permissions.IsAuthenticated()]

    def get_serializer_class(self):
        if self.action in ("create", "partial_update"):
            return LeaveRequestCreateSerializer
        return LeaveRequestReadSerializer

    def partial_update(self, request, *args, **kwargs):
        """
        PATCH /api/v1/leave-requests/{id}/

        - Request owner: can edit their own request only while in DRAFT.
        - HR: can edit any request, regardless of status.
        """
        leave_request = self.get_object()
        user = request.user

        is_hr = user.has_role(RoleName.HR)
        is_owner = leave_request.employee == user

        if not (is_hr or is_owner):
            raise PermissionDenied("You do not have permission to modify this leave request.")

        if is_owner and leave_request.status != LeaveRequestStatus.DRAFT:
            raise ValidationError(
                {
                    "status": (
                        "You can only edit your own leave requests while they are in DRAFT status. "
                        f"Current status: {leave_request.status}."
                    )
                }
            )

        return super().partial_update(request, *args, **kwargs)

    def get_queryset(self):
        user = self.request.user
        qs = LeaveRequest.objects.select_related("employee", "leave_type", "cover_person").all()

        # Draft visibility rule:
        # - Only the request owner ever sees DRAFT requests.
        # - All other users (including HR/ED/MD/Line Manager/cover_person) see
        #   only non-draft requests for others.
        from django.db.models import Q

        owner_q = Q(employee=user)
        non_draft_q = ~Q(status=LeaveRequestStatus.DRAFT)
        cover_q = Q(cover_person=user)

        if _is_privileged(user):
            # HR / ED / MD / staff:
            # - See all non-draft requests for everyone.
            # - Plus their own requests (including drafts).
            return qs.filter(owner_q | non_draft_q)

        # Supervisor: see unit members' non-draft requests and own requests
        if user.has_role(RoleName.SUPERVISOR) and getattr(user, "supervised_unit_id", None):
            unit_q = Q(employee__unit_id=user.supervised_unit_id)
            visible_q = owner_q | (non_draft_q & (unit_q | cover_q))
            return qs.filter(visible_q)

        if user.has_role(RoleName.LINE_MANAGER) and user.department_id:
            dept_q = Q(employee__department_id=user.department_id)
            # Line Manager:
            # - Own requests (any status, including drafts).
            # - Non-draft requests for employees in their department.
            # - Non-draft requests where they are cover_person.
            visible_q = owner_q | (non_draft_q & (dept_q | cover_q))
            return qs.filter(visible_q)

        # Regular employee:
        # - Own requests (any status, including drafts).
        # - Non-draft requests where they are cover_person.
        visible_q = owner_q | (non_draft_q & cover_q)
        return qs.filter(visible_q)

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

        # Determine first approval stage based on employee role and unit
        employee = leave_request.employee
        if employee.has_role(RoleName.SUPERVISOR):
            first_status = LeaveRequestStatus.PENDING_MANAGER
        elif getattr(employee, "unit_id", None) and getattr(employee.unit, "supervisor_id", None):
            first_status = LeaveRequestStatus.PENDING_SUPERVISOR
        else:
            first_status = LeaveRequestStatus.PENDING_MANAGER

        prev_status = leave_request.status
        leave_request.status = first_status
        leave_request.save(update_fields=["status", "updated_at"])

        _create_log(
            leave_request=leave_request,
            actor=request.user,
            action=ApprovalAction.MODIFY,
            previous_status=prev_status,
            new_status=first_status,
            comment="Submitted for approval.",
        )

        transaction.on_commit(
            lambda: notify_leave_submitted.delay(str(leave_request.id))
        )
        transaction.on_commit(
            lambda: notify_approver_required.delay(str(leave_request.id))
        )

        return Response(LeaveRequestReadSerializer(leave_request).data)

    # ------------------------------------------------------------------
    # create_and_submit — create DRAFT and immediately submit
    # ------------------------------------------------------------------

    @action(detail=False, methods=["post"], url_path="create-and-submit")
    def create_and_submit(self, request):
        """
        POST /api/v1/leave-requests/create-and-submit/

        Creates a new leave request as DRAFT for the authenticated user and
        immediately submits it (DRAFT → PENDING_MANAGER), performing the same
        validations as the regular create + submit flow.
        """
        serializer = LeaveRequestCreateSerializer(
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        leave_request = serializer.save()

        # Determine first approval stage as in submit()
        employee = leave_request.employee
        if employee.has_role(RoleName.SUPERVISOR):
            first_status = LeaveRequestStatus.PENDING_MANAGER
        elif getattr(employee, "unit_id", None) and getattr(employee.unit, "supervisor_id", None):
            first_status = LeaveRequestStatus.PENDING_SUPERVISOR
        else:
            first_status = LeaveRequestStatus.PENDING_MANAGER

        prev_status = leave_request.status
        leave_request.status = first_status
        leave_request.save(update_fields=["status", "updated_at"])

        _create_log(
            leave_request=leave_request,
            actor=request.user,
            action=ApprovalAction.MODIFY,
            previous_status=prev_status,
            new_status=first_status,
            comment="Created and submitted for approval.",
        )

        transaction.on_commit(
            lambda: notify_leave_submitted.delay(str(leave_request.id))
        )
        transaction.on_commit(
            lambda: notify_approver_required.delay(str(leave_request.id))
        )

        return Response(
            LeaveRequestReadSerializer(leave_request).data,
            status=status.HTTP_201_CREATED,
        )

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

        user = request.user

        # Role check
        if not user.has_role(required_role):
            raise PermissionDenied(
                f"Only a user with role '{required_role}' can approve at this stage "
                f"(current status: {leave_request.status})."
            )

        # Additional identity check for supervisor stage: must be the supervisor of the employee's unit
        if leave_request.status == LeaveRequestStatus.PENDING_SUPERVISOR:
            unit = getattr(leave_request.employee, "unit", None)
            if not unit or unit.supervisor_id != user.pk:
                raise PermissionDenied(
                    "Only the supervisor of the employee's unit can approve at this stage."
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

        transaction.on_commit(
            lambda: notify_approver_required.delay(str(leave_request.id))
        )
        if next_status == LeaveRequestStatus.APPROVED:
            transaction.on_commit(
                lambda: notify_leave_decision.delay(
                    str(leave_request.id),
                    LeaveRequestStatus.APPROVED,
                    request.data.get("comment", ""),
                )
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
        user = request.user
        if not user.has_role(required_role):
            raise PermissionDenied(
                f"Only a user with role '{required_role}' can reject at this stage."
            )

        if leave_request.status == LeaveRequestStatus.PENDING_SUPERVISOR:
            unit = getattr(leave_request.employee, "unit", None)
            if not unit or unit.supervisor_id != user.pk:
                raise PermissionDenied(
                    "Only the supervisor of the employee's unit can reject at this stage."
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

        transaction.on_commit(
            lambda: notify_leave_decision.delay(
                str(leave_request.id),
                LeaveRequestStatus.REJECTED,
                comment,
            )
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
            allowed = {
                LeaveRequestStatus.DRAFT,
                LeaveRequestStatus.PENDING_SUPERVISOR,
                LeaveRequestStatus.PENDING_MANAGER,
            }
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
        is_owner = leave_request.employee == user
        is_draft = leave_request.status == LeaveRequestStatus.DRAFT

        # Only the owner can ever see logs for DRAFT requests.
        if not is_owner:
            if is_draft:
                raise PermissionDenied("You do not have permission to view the approval log.")

            can_view = _is_privileged(user) or leave_request.cover_person == user

            # Line manager of the employee's department can view
            if user.has_role(RoleName.LINE_MANAGER) and user.department_id:
                if leave_request.employee.department_id == user.department_id:
                    can_view = True

            # Unit supervisor can view logs for their unit members
            if user.has_role(RoleName.SUPERVISOR) and getattr(user, "supervised_unit_id", None):
                if leave_request.employee.unit_id == user.supervised_unit_id:
                    can_view = True
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
