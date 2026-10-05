"""
Leave management API views.

Viewset summary
---------------
LeaveTypeViewSet           – Full CRUD for HR/Admin; list/retrieve for any authenticated
LeaveBalanceViewSet        – ReadOnly, role-filtered queryset + ?employee=&year= filters
LeaveRequestViewSet        – Full CRUD minus DELETE, role-filtered queryset
  custom actions:
    POST  submit/:id/      – Employee: DRAFT → PENDING_MANAGER
    POST  create-and-submit/ – Create + submit atomically
    POST  reconcile/       – HR: backdated leave without approval workflow
    POST  bulk-reconcile/  – HR: batch backdated leave
    POST  bulk-reconcile-csv/ – HR: CSV upload for bulk reconcile
    GET   eligible-relievers/ – Org-scoped reliever picker for current user
    POST  approve/:id/     – Stage-based role transitions
    POST  reject/:id/      – Matching approver at current stage (comment required)
    POST  cancel/:id/      – Employee (own DRAFT/PENDING_MANAGER) or HR (any active)
    GET   logs/:id/        – Approval audit trail (HR, Manager, ED, or request owner)
DepartmentCalendarView     – GET /api/v1/calendar/  dept-scoped approved leave
"""

import csv
import datetime
import io
import json

from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import RoleName, Team, Unit, get_or_create_management_department
from apps.accounts.permissions import IsEmployee, IsHR

from .models import (
    ApprovalAction,
    ApproverDelegate,
    BalanceTransactionSource,
    CalendarAssignment,
    CalendarHoliday,
    HolidayCalendar,
    LeaveApprovalLog,
    LeaveBalance,
    LeaveBalanceTransaction,
    LeaveBlackoutPeriod,
    LeavePolicy,
    LeavePolicyAssignment,
    LeavePolicyStatus,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveSettingsAuditLog,
    LeaveType,
    LeaveWorkflowTemplate,
    PublicHoliday,
    SettingsAuditAction,
    WorkingCalendar,
)
from .serializers import (
    CalendarAssignmentSerializer,
    CalendarEntrySerializer,
    CalendarHolidaySerializer,
    HolidayCalendarSerializer,
    LeaveApprovalLogSerializer,
    LeaveBalanceAdjustSerializer,
    LeaveBalanceSerializer,
    LeaveBalanceTransactionSerializer,
    LeaveBlackoutPeriodSerializer,
    LeavePolicyActionSerializer,
    LeavePolicyAssignmentSerializer,
    LeavePolicySerializer,
    LeaveRequestBulkReconcileSerializer,
    LeaveRequestCreateSerializer,
    LeaveRequestReadSerializer,
    LeaveRequestReconcileEditSerializer,
    LeaveRequestReconcileSerializer,
    LeaveSettingsAuditLogSerializer,
    LeaveSettingsSerializer,
    LeaveTypeSerializer,
    LeaveAccrualPreviewSerializer,
    LeaveWorkflowSimulateSerializer,
    LeaveWorkflowTemplateSerializer,
    ApproverDelegateSerializer,
    PublicHolidaySerializer,
    WorkingCalendarSerializer,
    _EmployeeMinimalSerializer,
)
from . import messages as leave_messages
from .services import (
    apply_policy_snapshot,
    archive_leave_policy,
    clone_leave_policy,
    deduct_leave_balance,
    get_eligible_leave_types,
    get_eligible_relievers,
    get_leave_settings,
    preview_or_run_accrual,
    preview_policy_impact,
    record_settings_audit,
    release_leave_balance,
    reserve_leave_balance,
    leave_starts_within_reminder_window,
    publish_leave_policy,
    reconcile_leave_request,
    resolve_leave_policy,
    restore_leave_balance,
    snapshot_leave_assignment,
    snapshot_leave_policy,
    snapshot_leave_settings,
    snapshot_leave_type,
    sync_public_holiday_to_default_calendar,
    validate_cover_person_for_submission,
    validate_reconcile_row,
)
from .workflow import (
    LEGACY_TRANSITIONS,
    actor_may_decide,
    advance_snapshot_pointer,
    comment_requirements,
    next_status_from_snapshot,
    plan_leave_submission,
    simulate_workflow,
)
from .tasks import (
    notify_approver_required,
    notify_department_leave_reminder,
    notify_leave_decision,
    notify_leave_reconciled,
    notify_reliever_assigned,
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


def _queue_final_approval_notifications(leave_request, *, decision_comment: str = "") -> None:
    """
    Queue emails/notifications after a leave request reaches APPROVED:
    - decision email to the applicant
    - reliever assignment email when a cover person is set
    - department reminder immediately when start is within ~24 hours
    """
    leave_request_id = str(leave_request.id)
    transaction.on_commit(
        lambda: notify_leave_decision.delay(
            leave_request_id,
            LeaveRequestStatus.APPROVED,
            decision_comment,
        )
    )
    if leave_request.cover_person_id:
        transaction.on_commit(
            lambda: notify_reliever_assigned.delay(leave_request_id)
        )
    if leave_starts_within_reminder_window(leave_request):
        transaction.on_commit(
            lambda: notify_department_leave_reminder.delay(leave_request_id)
        )


def _can_view_employee_leave_profile(viewer, employee) -> bool:
    """Whether *viewer* may see another employee's leave history or balances."""
    if viewer.pk == employee.pk:
        return True
    if _is_privileged(viewer):
        return True

    if viewer.has_role(RoleName.LINE_MANAGER) and viewer.department_id:
        if employee.department_id == viewer.department_id:
            return True

    if viewer.has_role(RoleName.SUPERVISOR) and getattr(employee, "unit_id", None):
        unit = getattr(employee, "unit", None)
        configured = unit and getattr(unit, "supervisor_id", None) == viewer.pk
        same_unit = getattr(viewer, "unit_id", None) == employee.unit_id
        if configured or same_unit:
            return True

    if viewer.has_role(RoleName.TEAM_LEAD) and getattr(employee, "team_id", None):
        team = getattr(employee, "team", None)
        configured = team and getattr(team, "team_lead_id", None) == viewer.pk
        same_team = getattr(viewer, "team_id", None) == employee.team_id
        if configured or same_team:
            return True

    return False


def _create_log(*, leave_request, actor, action, previous_status, new_status, comment=""):
    LeaveApprovalLog.objects.create(
        leave_request=leave_request,
        actor=actor,
        action=action,
        previous_status=previous_status,
        new_status=new_status,
        comment=comment,
    )


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
    DELETE /api/v1/leave-types/:id/  — HR or admin (blocked if in use)
    POST   /api/v1/leave-types/:id/activate/
    POST   /api/v1/leave-types/:id/deactivate/
    """

    queryset = LeaveType.objects.all()
    serializer_class = LeaveTypeSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [permissions.IsAuthenticated()]
        return [permissions.IsAuthenticated(), (IsHR | permissions.IsAdminUser)()]

    def perform_create(self, serializer):
        instance = serializer.save()
        record_settings_audit(
            actor=self.request.user,
            object_type="LeaveType",
            object_id=instance.pk,
            action=SettingsAuditAction.CREATE,
            previous_values=None,
            new_values=snapshot_leave_type(instance),
            reason=self.request.data.get("reason", ""),
            request=self.request,
        )

    def perform_update(self, serializer):
        previous = snapshot_leave_type(serializer.instance)
        instance = serializer.save()
        record_settings_audit(
            actor=self.request.user,
            object_type="LeaveType",
            object_id=instance.pk,
            action=SettingsAuditAction.UPDATE,
            previous_values=previous,
            new_values=snapshot_leave_type(instance),
            reason=self.request.data.get("reason", ""),
            request=self.request,
        )

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.policies.exists() or instance.balances.exists() or instance.requests.exists():
            raise ValidationError({"detail": leave_messages.leave_type_delete_blocked()})
        previous = snapshot_leave_type(instance)
        response = super().destroy(request, *args, **kwargs)
        record_settings_audit(
            actor=request.user,
            object_type="LeaveType",
            object_id=instance.pk,
            action=SettingsAuditAction.DELETE,
            previous_values=previous,
            new_values=None,
            reason=request.data.get("reason", "") if hasattr(request, "data") else "",
            request=request,
        )
        return response

    @action(detail=True, methods=["post"], url_path="activate")
    def activate(self, request, pk=None):
        instance = self.get_object()
        previous = snapshot_leave_type(instance)
        instance.is_active = True
        instance.save(update_fields=["is_active", "updated_at"])
        record_settings_audit(
            actor=request.user,
            object_type="LeaveType",
            object_id=instance.pk,
            action=SettingsAuditAction.ACTIVATE,
            previous_values=previous,
            new_values=snapshot_leave_type(instance),
            reason=request.data.get("reason", ""),
            request=request,
        )
        return Response(LeaveTypeSerializer(instance).data)

    @action(detail=True, methods=["post"], url_path="deactivate")
    def deactivate(self, request, pk=None):
        instance = self.get_object()
        previous = snapshot_leave_type(instance)
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at"])
        record_settings_audit(
            actor=request.user,
            object_type="LeaveType",
            object_id=instance.pk,
            action=SettingsAuditAction.DEACTIVATE,
            previous_values=previous,
            new_values=snapshot_leave_type(instance),
            reason=request.data.get("reason", ""),
            request=request,
        )
        return Response(LeaveTypeSerializer(instance).data)


# ---------------------------------------------------------------------------
# LeavePolicy
# ---------------------------------------------------------------------------

class LeavePolicyViewSet(viewsets.ModelViewSet):
    """
    GET    /api/v1/leave-policies/            — authenticated
    POST   /api/v1/leave-policies/            — HR/Admin (creates DRAFT)
    GET    /api/v1/leave-policies/:id/        — authenticated
    PATCH  /api/v1/leave-policies/:id/        — HR/Admin (DRAFT only)
    DELETE /api/v1/leave-policies/:id/        — HR/Admin (DRAFT only)
    POST   /api/v1/leave-policies/:id/publish/
    POST   /api/v1/leave-policies/:id/archive/
    POST   /api/v1/leave-policies/:id/clone/
    GET    /api/v1/leave-policies/:id/audit-log/
    """

    queryset = LeavePolicy.objects.select_related("leave_type").all()
    serializer_class = LeavePolicySerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [permissions.IsAuthenticated()]
        return [permissions.IsAuthenticated(), (IsHR | permissions.IsAdminUser)()]

    def perform_create(self, serializer):
        instance = serializer.save()
        record_settings_audit(
            actor=self.request.user,
            object_type="LeavePolicy",
            object_id=instance.pk,
            action=SettingsAuditAction.CREATE,
            previous_values=None,
            new_values=snapshot_leave_policy(instance),
            reason=self.request.data.get("reason", ""),
            request=self.request,
        )

    def perform_update(self, serializer):
        previous = snapshot_leave_policy(serializer.instance)
        instance = serializer.save()
        record_settings_audit(
            actor=self.request.user,
            object_type="LeavePolicy",
            object_id=instance.pk,
            action=SettingsAuditAction.UPDATE,
            previous_values=previous,
            new_values=snapshot_leave_policy(instance),
            reason=self.request.data.get("reason", ""),
            request=self.request,
        )

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.status != LeavePolicyStatus.DRAFT:
            raise ValidationError({"status": leave_messages.policy_delete_draft_only()})
        previous = snapshot_leave_policy(instance)
        response = super().destroy(request, *args, **kwargs)
        record_settings_audit(
            actor=request.user,
            object_type="LeavePolicy",
            object_id=instance.pk,
            action=SettingsAuditAction.DELETE,
            previous_values=previous,
            new_values=None,
            reason=request.data.get("reason", "") if hasattr(request, "data") else "",
            request=request,
        )
        return response

    @action(detail=True, methods=["post"], url_path="publish")
    def publish(self, request, pk=None):
        serializer = LeavePolicyActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        policy = publish_leave_policy(
            self.get_object(),
            actor=request.user,
            reason=serializer.validated_data.get("reason", ""),
            request=request,
            keep_existing_active=serializer.validated_data.get(
                "keep_existing_active", False
            ),
        )
        return Response(LeavePolicySerializer(policy).data)

    @action(detail=True, methods=["post"], url_path="archive")
    def archive(self, request, pk=None):
        serializer = LeavePolicyActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        policy = archive_leave_policy(
            self.get_object(),
            actor=request.user,
            reason=serializer.validated_data.get("reason", ""),
            request=request,
        )
        return Response(LeavePolicySerializer(policy).data)

    @action(detail=True, methods=["post"], url_path="clone")
    def clone(self, request, pk=None):
        serializer = LeavePolicyActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        policy = clone_leave_policy(
            self.get_object(),
            actor=request.user,
            reason=serializer.validated_data.get("reason", ""),
            request=request,
        )
        return Response(LeavePolicySerializer(policy).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"], url_path="audit-log")
    def audit_log(self, request, pk=None):
        policy = self.get_object()
        if not (
            request.user.is_staff
            or request.user.has_role(RoleName.HR)
            or request.user.is_superuser
        ):
            raise PermissionDenied(leave_messages.permission_policy_audit())
        logs = LeaveSettingsAuditLog.objects.filter(
            object_type="LeavePolicy",
            object_id=policy.pk,
        )
        return Response(LeaveSettingsAuditLogSerializer(logs, many=True).data)

    @action(detail=True, methods=["get"], url_path="impact-preview")
    def impact_preview(self, request, pk=None):
        policy = self.get_object()
        on_date = request.query_params.get("date")
        parsed = None
        if on_date:
            try:
                parsed = datetime.date.fromisoformat(on_date)
            except ValueError:
                raise ValidationError({"date": leave_messages.iso_date_required("date")})
        payload = preview_policy_impact(policy, on_date=parsed)
        payload["policy"] = LeavePolicySerializer(policy).data
        return Response(payload)


# ---------------------------------------------------------------------------
# LeavePolicyAssignment
# ---------------------------------------------------------------------------

class LeavePolicyAssignmentViewSet(viewsets.ModelViewSet):
    """CRUD /api/v1/leave-policy-assignments/ — HR/Admin write; authenticated read."""

    queryset = LeavePolicyAssignment.objects.select_related(
        "policy", "policy__leave_type", "employee"
    ).all()
    serializer_class = LeavePolicyAssignmentSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [permissions.IsAuthenticated()]
        return [permissions.IsAuthenticated(), (IsHR | permissions.IsAdminUser)()]

    def get_queryset(self):
        qs = super().get_queryset()
        leave_type = self.request.query_params.get("leave_type")
        policy = self.request.query_params.get("policy")
        scope_type = self.request.query_params.get("scope_type")
        is_active = self.request.query_params.get("is_active")
        if leave_type:
            qs = qs.filter(policy__leave_type_id=leave_type)
        if policy:
            qs = qs.filter(policy_id=policy)
        if scope_type:
            qs = qs.filter(scope_type=scope_type)
        if is_active is not None:
            if is_active.lower() in ("true", "1", "yes"):
                qs = qs.filter(is_active=True)
            elif is_active.lower() in ("false", "0", "no"):
                qs = qs.filter(is_active=False)
        return qs

    def perform_create(self, serializer):
        instance = serializer.save()
        record_settings_audit(
            actor=self.request.user,
            object_type="LeavePolicyAssignment",
            object_id=instance.pk,
            action=SettingsAuditAction.CREATE,
            previous_values=None,
            new_values=snapshot_leave_assignment(instance),
            reason=self.request.data.get("reason", ""),
            request=self.request,
        )

    def perform_update(self, serializer):
        previous = snapshot_leave_assignment(serializer.instance)
        instance = serializer.save()
        record_settings_audit(
            actor=self.request.user,
            object_type="LeavePolicyAssignment",
            object_id=instance.pk,
            action=SettingsAuditAction.UPDATE,
            previous_values=previous,
            new_values=snapshot_leave_assignment(instance),
            reason=self.request.data.get("reason", ""),
            request=self.request,
        )

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        previous = snapshot_leave_assignment(instance)
        response = super().destroy(request, *args, **kwargs)
        record_settings_audit(
            actor=request.user,
            object_type="LeavePolicyAssignment",
            object_id=instance.pk,
            action=SettingsAuditAction.DELETE,
            previous_values=previous,
            new_values=None,
            reason=request.data.get("reason", "") if hasattr(request, "data") else "",
            request=request,
        )
        return response


class LeavePolicyResolutionView(APIView):
    """GET /api/v1/leave-policy-resolution/?employee=&leave_type=&date="""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from apps.accounts.models import User

        leave_type_id = request.query_params.get("leave_type")
        if not leave_type_id:
            raise ValidationError({"leave_type": "This query parameter is required."})
        leave_type = LeaveType.objects.filter(pk=leave_type_id).first()
        if leave_type is None:
            raise ValidationError({"leave_type": "Leave type not found."})

        employee_id = request.query_params.get("employee")
        if employee_id:
            employee = User.objects.filter(pk=employee_id).first()
            if employee is None:
                raise ValidationError({"employee": "Employee not found."})
            is_privileged = (
                request.user.is_staff
                or request.user.is_superuser
                or request.user.has_role(RoleName.HR)
            )
            if employee != request.user and not is_privileged:
                raise PermissionDenied("You can only resolve policies for yourself.")
        else:
            employee = request.user

        on_date = request.query_params.get("date")
        parsed = timezone.localdate()
        if on_date:
            try:
                parsed = datetime.date.fromisoformat(on_date)
            except ValueError:
                raise ValidationError({"date": leave_messages.iso_date_required("date")})

        resolution = resolve_leave_policy(employee, leave_type, parsed)
        return Response(
            {
                "employee": str(employee.pk),
                "leave_type": str(leave_type.pk),
                "effective_date": resolution.effective_date.isoformat(),
                "source": resolution.source,
                "assignment_scope": resolution.assignment_scope,
                "assignment": (
                    LeavePolicyAssignmentSerializer(resolution.assignment).data
                    if resolution.assignment
                    else None
                ),
                "resolved_policy": (
                    LeavePolicySerializer(resolution.policy).data
                    if resolution.policy
                    else None
                ),
            }
        )


# ---------------------------------------------------------------------------
# LeaveBalance
# ---------------------------------------------------------------------------

class LeaveBalanceViewSet(viewsets.ReadOnlyModelViewSet):
    """
    GET /api/v1/leave-balances/
    Authenticated users see their own balances.
    Approvers / HR may pass ?employee=<uuid> for a subordinate they can view.
    HR/Admin: POST adjust/, GET transactions/ on a balance id.
    """

    serializer_class = LeaveBalanceSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        from apps.accounts.models import User

        user = self.request.user
        qs = LeaveBalance.objects.select_related("employee", "leave_type")

        if self.action in ("retrieve", "adjust", "transactions") and self.kwargs.get("pk"):
            if user.is_staff or user.has_role(RoleName.HR):
                return qs
            return qs.filter(employee=user)

        employee_param = self.request.query_params.get("employee")
        if employee_param:
            try:
                target = User.objects.select_related("unit", "team").get(pk=employee_param)
            except (User.DoesNotExist, ValueError):
                raise ValidationError({"employee": "Invalid employee id."})
            if not _can_view_employee_leave_profile(user, target):
                raise PermissionDenied(
                    "You do not have permission to view this employee's leave balances."
                )
            qs = qs.filter(
                employee=target,
                leave_type__in=get_eligible_leave_types(target),
            )
        else:
            qs = qs.filter(employee=user, leave_type__in=get_eligible_leave_types(user))

        year = self.request.query_params.get("year")
        if year:
            try:
                year_int = int(year)
            except ValueError:
                raise ValidationError({"year": "year must be an integer."})
            qs = qs.filter(year=year_int)
        return qs

    @action(
        detail=True,
        methods=["post"],
        url_path="adjust",
        permission_classes=[permissions.IsAuthenticated, IsHR | permissions.IsAdminUser],
    )
    def adjust(self, request, pk=None):
        from .services import adjust_leave_balance

        balance = self.get_object()
        serializer = LeaveBalanceAdjustSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        txn = adjust_leave_balance(
            balance,
            delta=serializer.validated_data["delta"],
            reason=serializer.validated_data["reason"],
            actor=request.user,
            effective_date=serializer.validated_data.get("effective_date"),
        )
        balance.refresh_from_db()
        return Response(
            {
                "balance": LeaveBalanceSerializer(balance).data,
                "transaction": LeaveBalanceTransactionSerializer(txn).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["get"], url_path="transactions")
    def transactions(self, request, pk=None):
        balance = self.get_object()
        viewer = request.user
        if not (
            viewer.is_staff
            or viewer.has_role(RoleName.HR)
            or balance.employee_id == viewer.id
            or _can_view_employee_leave_profile(viewer, balance.employee)
        ):
            raise PermissionDenied(leave_messages.permission_balance_ledger())
        qs = balance.transactions.select_related("actor", "leave_request").order_by("-created_at")
        return Response(LeaveBalanceTransactionSerializer(qs, many=True).data)


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
            sync_public_holiday_to_default_calendar(
                name=name, date=date, is_recurring=False
            )
            if was_created:
                created += 1
            else:
                updated += 1

        return Response({"created": created, "updated": updated, "errors": errors})


# ---------------------------------------------------------------------------
# LeaveRequest
# ---------------------------------------------------------------------------

_APPROVAL_TRANSITIONS = LEGACY_TRANSITIONS
_REJECTION_ROLES = {status: role for status, (_, role) in LEGACY_TRANSITIONS.items()}


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
        if self.action in ("reconcile", "bulk_reconcile", "bulk_reconcile_csv"):
            return [permissions.IsAuthenticated(), IsHR()]
        # All leave request actions require authentication. Additional
        # per-action checks are enforced inside the view methods.
        return [permissions.IsAuthenticated()]

    def get_serializer_class(self):
        if self.action == "reconcile":
            return LeaveRequestReconcileSerializer
        if self.action in ("bulk_reconcile", "bulk_reconcile_csv"):
            return LeaveRequestBulkReconcileSerializer
        if self.action in ("create", "partial_update"):
            return LeaveRequestCreateSerializer
        return LeaveRequestReadSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if self.action == "partial_update":
            pk = self.kwargs.get("pk")
            leave_request = (
                LeaveRequest.objects.select_related("employee")
                .filter(pk=pk)
                .first()
            )
            if leave_request is not None:
                user = self.request.user
                is_hr = user.has_role(RoleName.HR)
                is_owner = leave_request.employee == user
                context["hr_override"] = is_hr and not is_owner
                context["applicant"] = leave_request.employee
        return context

    def partial_update(self, request, *args, **kwargs):
        """
        PATCH /api/v1/leave-requests/{id}/

        - Request owner: can edit their own request only while in DRAFT.
        - HR: can edit any request; reconciled APPROVED requests use balance-adjust flow.
        """
        leave_request = LeaveRequest.objects.select_related("employee").get(pk=kwargs.get("pk"))
        user = request.user

        is_hr = user.has_role(RoleName.HR)
        is_owner = leave_request.employee == user

        if not (is_hr or is_owner):
            raise PermissionDenied("You do not have permission to modify this leave request.")

        if is_owner and leave_request.status != LeaveRequestStatus.DRAFT:
            raise ValidationError(
                {
                    "status": (
                        "You can only edit your own leave requests while they are still drafts. "
                        f"This request is currently "
                        f"{leave_messages.leave_request_status_label(leave_request.status)}. "
                        "Cancel it and create a new request, or ask HR for help."
                    )
                }
            )

        if (
            is_hr
            and leave_request.is_reconciled
            and leave_request.status == LeaveRequestStatus.APPROVED
        ):
            serializer = LeaveRequestReconcileEditSerializer(
                leave_request,
                data=request.data,
                partial=True,
                context={"request": request},
            )
            serializer.is_valid(raise_exception=True)
            leave_request = serializer.save()
            leave_request = LeaveRequest.objects.select_related(
                "employee",
                "leave_type",
                "cover_person",
                "reconciled_by",
            ).get(pk=leave_request.pk)
            return Response(LeaveRequestReadSerializer(leave_request).data)

        return super().partial_update(request, *args, **kwargs)

    # ------------------------------------------------------------------
    # eligible_relievers — org-scoped reliever picker for the current user
    # ------------------------------------------------------------------

    @action(detail=False, methods=["get"], url_path="eligible-relievers")
    def eligible_relievers(self, request):
        leave_type = None
        leave_type_id = request.query_params.get("leave_type")
        if leave_type_id:
            leave_type = LeaveType.objects.filter(pk=leave_type_id).first()
        scope_result = get_eligible_relievers(request.user, leave_type)
        return Response(
            {
                "scope_level": scope_result.scope_level,
                "effective_scope_level": scope_result.effective_scope_level,
                "fallback_applied": scope_result.fallback_applied,
                "relievers": _EmployeeMinimalSerializer(
                    scope_result.relievers, many=True
                ).data,
            }
        )

    def get_queryset(self):
        user = self.request.user
        qs = LeaveRequest.objects.select_related(
            "employee",
            "employee__department",
            "employee__unit",
            "employee__team",
            "leave_type",
            "cover_person",
        ).all()

        from django.db.models import OuterRef, Q, Subquery

        from .models import ApprovalAction, LeaveApprovalLog

        latest_reject_prev_status = LeaveApprovalLog.objects.filter(
            leave_request_id=OuterRef("pk"),
            action=ApprovalAction.REJECT,
        ).order_by("-timestamp").values("previous_status")[:1]

        latest_reject_actor_id = LeaveApprovalLog.objects.filter(
            leave_request_id=OuterRef("pk"),
            action=ApprovalAction.REJECT,
        ).order_by("-timestamp").values("actor_id")[:1]

        qs = qs.annotate(rejected_from_status=Subquery(latest_reject_prev_status))
        qs = qs.annotate(rejected_by_id=Subquery(latest_reject_actor_id))

        owner_q = Q(employee=user)
        cover_q = Q(cover_person=user)
        priv = _is_privileged(user)
        non_draft_q = ~Q(status=LeaveRequestStatus.DRAFT)
        cover_non_draft_q = cover_q & non_draft_q

        # Org-level visibility for APPROVED only.
        approved_org_q = Q(pk__isnull=True)  # default false
        if getattr(user, "department_id", None):
            dept_id = user.department_id
            department_has_units = Unit.objects.filter(department_id=dept_id).exists()
            department_has_teams = Team.objects.filter(unit__department_id=dept_id).exists()
            if department_has_teams and getattr(user, "team_id", None):
                approved_org_q = Q(employee__team_id=user.team_id)
            elif department_has_units and getattr(user, "unit_id", None):
                approved_org_q = Q(employee__unit_id=user.unit_id)
            else:
                approved_org_q = Q(employee__department_id=dept_id)

        approved_visible_q = Q(status=LeaveRequestStatus.APPROVED) & (
            Q(pk__isnull=False) if priv else approved_org_q
        )

        # Cumulative approver visibility for pending statuses.
        team_lead_pred = Q(employee__team__team_lead_id=user.pk)
        if getattr(user, "team_id", None) and user.has_role(RoleName.TEAM_LEAD):
            team_lead_pred = team_lead_pred | Q(employee__team_id=user.team_id)

        supervisor_pred = Q(employee__unit__supervisor_id=user.pk)
        if getattr(user, "unit_id", None) and user.has_role(RoleName.SUPERVISOR):
            supervisor_pred = supervisor_pred | Q(employee__unit_id=user.unit_id)

        manager_pred = Q(pk__isnull=True)  # false by default
        if user.has_role(RoleName.LINE_MANAGER):
            # Line manager visibility: line manager role within their department.
            # (Do not require Department.line_manager to be set.)
            if getattr(user, "department_id", None):
                manager_pred = Q(employee__department_id=user.department_id)
            mgmt = get_or_create_management_department()
            if mgmt.line_manager_id == user.pk:
                manager_pred = manager_pred | Q(manager_approver_is_management=True)

        hr_pred = Q(pk__isnull=False) if user.has_role(RoleName.HR) else Q(pk__isnull=True)
        ed_pred = Q(pk__isnull=False) if user.has_role(RoleName.EXECUTIVE_DIRECTOR) else Q(pk__isnull=True)

        pending_team_lead_q = Q(status=LeaveRequestStatus.PENDING_TEAM_LEAD) & team_lead_pred
        pending_supervisor_q = Q(status=LeaveRequestStatus.PENDING_SUPERVISOR) & (team_lead_pred | supervisor_pred)
        pending_manager_q = Q(status=LeaveRequestStatus.PENDING_MANAGER) & (team_lead_pred | supervisor_pred | manager_pred)
        pending_hr_q = Q(status=LeaveRequestStatus.PENDING_HR) & (team_lead_pred | supervisor_pred | manager_pred | hr_pred)
        pending_ed_q = Q(status=LeaveRequestStatus.PENDING_ED) & (team_lead_pred | supervisor_pred | manager_pred | hr_pred | ed_pred)
        pending_visible_q = pending_team_lead_q | pending_supervisor_q | pending_manager_q | pending_hr_q | pending_ed_q

        # Exception: if the requester is a LINE_MANAGER, make their pending requests
        # immediately visible to HR and ED (even before the HR/ED stages).
        pending_statuses = (
            LeaveRequestStatus.PENDING_TEAM_LEAD,
            LeaveRequestStatus.PENDING_SUPERVISOR,
            LeaveRequestStatus.PENDING_MANAGER,
            LeaveRequestStatus.PENDING_HR,
            LeaveRequestStatus.PENDING_ED,
        )
        requester_is_line_manager = Q(employee__user_roles__role__name=RoleName.LINE_MANAGER)
        hr_or_ed_viewing = hr_pred | ed_pred
        pending_visible_q = pending_visible_q | (Q(status__in=pending_statuses) & requester_is_line_manager & hr_or_ed_viewing)

        # Draft requests: creator only. cover_person only sees non-draft.
        base_visible = owner_q | cover_non_draft_q

        # Terminal (REJECTED/CANCELLED)
        terminal_visible_q = Q(status__in=(LeaveRequestStatus.REJECTED, LeaveRequestStatus.CANCELLED)) & (
            Q(pk__isnull=False) if priv else base_visible
        )

        # Exception for REJECTED: make it visible to all approvers *before* the rejecting stage.
        rejected_q = Q(status=LeaveRequestStatus.REJECTED)
        rejected_team_lead_q = rejected_q & Q(
            rejected_from_status__in=(
                LeaveRequestStatus.PENDING_SUPERVISOR,
                LeaveRequestStatus.PENDING_MANAGER,
                LeaveRequestStatus.PENDING_HR,
                LeaveRequestStatus.PENDING_ED,
            )
        ) & team_lead_pred
        rejected_supervisor_q = rejected_q & Q(
            rejected_from_status__in=(
                LeaveRequestStatus.PENDING_MANAGER,
                LeaveRequestStatus.PENDING_HR,
                LeaveRequestStatus.PENDING_ED,
            )
        ) & supervisor_pred
        rejected_manager_q = rejected_q & Q(
            rejected_from_status__in=(
                LeaveRequestStatus.PENDING_HR,
                LeaveRequestStatus.PENDING_ED,
            )
        ) & manager_pred
        rejected_hr_q = rejected_q & Q(
            rejected_from_status__in=(LeaveRequestStatus.PENDING_ED,)
        ) & hr_pred

        rejected_by_actor_q = rejected_q & Q(rejected_by_id=user.pk)

        terminal_visible_q = terminal_visible_q | (
            rejected_team_lead_q
            | rejected_supervisor_q
            | rejected_manager_q
            | rejected_hr_q
            | rejected_by_actor_q
        )

        visible_q = base_visible | approved_visible_q | pending_visible_q | terminal_visible_q
        return qs.filter(visible_q).distinct()

    def filter_queryset(self, queryset):
        qs = super().filter_queryset(queryset)
        if self.action != "list":
            return qs

        employee_id = self.request.query_params.get("employee")
        if employee_id:
            qs = qs.filter(employee_id=employee_id)

        status_param = self.request.query_params.get("status")
        if status_param:
            statuses = [s.strip() for s in status_param.split(",") if s.strip()]
            if len(statuses) == 1:
                qs = qs.filter(status=statuses[0])
            elif statuses:
                qs = qs.filter(status__in=statuses)

        leave_type_param = self.request.query_params.get("leave_type")
        if leave_type_param:
            leave_type_ids = [lt.strip() for lt in leave_type_param.split(",") if lt.strip()]
            if len(leave_type_ids) == 1:
                qs = qs.filter(leave_type_id=leave_type_ids[0])
            elif leave_type_ids:
                qs = qs.filter(leave_type_id__in=leave_type_ids)

        exclude_id = self.request.query_params.get("exclude")
        if exclude_id:
            qs = qs.exclude(pk=exclude_id)

        is_reconciled = self.request.query_params.get("is_reconciled")
        if is_reconciled is not None:
            if is_reconciled.lower() in ("true", "1", "yes"):
                qs = qs.filter(is_reconciled=True)
            elif is_reconciled.lower() in ("false", "0", "no"):
                qs = qs.filter(is_reconciled=False)

        return qs

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
        leave_request = LeaveRequest.objects.select_related("employee", "employee__department", "employee__unit", "employee__team").get(pk=pk)

        if leave_request.employee != request.user:
            raise PermissionDenied("You can only submit your own leave requests.")

        if leave_request.status != LeaveRequestStatus.DRAFT:
            raise ValidationError(
                {"status": leave_messages.submit_not_draft(leave_request.status)}
            )

        plan = plan_leave_submission(leave_request.employee, leave_request.leave_type)
        first_status = plan["first_status"]
        skip_hr_stage = plan["skip_hr_stage"]
        manager_approver_is_management = plan["manager_approver_is_management"]
        workflow_snapshot = plan["workflow_snapshot"]

        validate_cover_person_for_submission(leave_request, hr_override=False)

        prev_status = leave_request.status
        now = timezone.now()
        with transaction.atomic():
            apply_policy_snapshot(leave_request)
            leave_request.status = first_status
            leave_request.skip_hr_stage = skip_hr_stage
            leave_request.manager_approver_is_management = manager_approver_is_management
            leave_request.workflow_snapshot = workflow_snapshot
            leave_request.stage_entered_at = now
            leave_request.sla_notified_at = None
            leave_request.save(
                update_fields=[
                    "status",
                    "skip_hr_stage",
                    "manager_approver_is_management",
                    "workflow_snapshot",
                    "stage_entered_at",
                    "sla_notified_at",
                    "policy",
                    "policy_version",
                    "updated_at",
                ]
            )

            _create_log(
                leave_request=leave_request,
                actor=request.user,
                action=ApprovalAction.MODIFY,
                previous_status=prev_status,
                new_status=first_status,
                comment="Submitted for approval.",
            )

            if first_status == LeaveRequestStatus.APPROVED:
                deduct_leave_balance(
                    leave_request,
                    source=BalanceTransactionSource.APPROVAL,
                    actor=request.user,
                    reason="Auto-approved based on requester role.",
                )
            else:
                reserve_leave_balance(
                    leave_request,
                    actor=request.user,
                    reason="Leave submitted; balance reserved.",
                )

        if first_status == LeaveRequestStatus.APPROVED:
            _queue_final_approval_notifications(
                leave_request,
                decision_comment="Auto-approved based on requester role.",
            )
        else:
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

        plan = plan_leave_submission(leave_request.employee, leave_request.leave_type)
        first_status = plan["first_status"]
        skip_hr_stage = plan["skip_hr_stage"]
        manager_approver_is_management = plan["manager_approver_is_management"]
        workflow_snapshot = plan["workflow_snapshot"]

        validate_cover_person_for_submission(leave_request, hr_override=False)

        prev_status = leave_request.status
        now = timezone.now()
        with transaction.atomic():
            apply_policy_snapshot(leave_request)
            leave_request.status = first_status
            leave_request.skip_hr_stage = skip_hr_stage
            leave_request.manager_approver_is_management = manager_approver_is_management
            leave_request.workflow_snapshot = workflow_snapshot
            leave_request.stage_entered_at = now
            leave_request.sla_notified_at = None
            leave_request.save(
                update_fields=[
                    "status",
                    "skip_hr_stage",
                    "manager_approver_is_management",
                    "workflow_snapshot",
                    "stage_entered_at",
                    "sla_notified_at",
                    "policy",
                    "policy_version",
                    "updated_at",
                ]
            )

            _create_log(
                leave_request=leave_request,
                actor=request.user,
                action=ApprovalAction.MODIFY,
                previous_status=prev_status,
                new_status=first_status,
                comment="Created and submitted for approval.",
            )

            if first_status == LeaveRequestStatus.APPROVED:
                deduct_leave_balance(
                    leave_request,
                    source=BalanceTransactionSource.APPROVAL,
                    actor=request.user,
                    reason="Auto-approved based on requester role.",
                )
            else:
                reserve_leave_balance(
                    leave_request,
                    actor=request.user,
                    reason="Leave submitted; balance reserved.",
                )

        if first_status == LeaveRequestStatus.APPROVED:
            _queue_final_approval_notifications(
                leave_request,
                decision_comment="Auto-approved based on requester role.",
            )
        else:
            transaction.on_commit(
                lambda: notify_approver_required.delay(str(leave_request.id))
            )

        return Response(
            LeaveRequestReadSerializer(leave_request).data,
            status=status.HTTP_201_CREATED,
        )

    # ------------------------------------------------------------------
    # reconcile — HR: backdated leave without approval workflow
    # ------------------------------------------------------------------

    @action(detail=False, methods=["post"], url_path="reconcile")
    def reconcile(self, request):
        """
        POST /api/v1/leave-requests/reconcile/

        HR records backdated leave for an employee who was absent without applying.
        Creates an APPROVED request, deducts leave balance, writes an audit log,
        and notifies approval-chain stakeholders (informational only).
        """
        serializer = LeaveRequestReconcileSerializer(
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        notify_department = serializer.validated_data.get("notify_department_colleagues", False)

        with transaction.atomic():
            leave_request = serializer.save()

        transaction.on_commit(
            lambda: notify_leave_reconciled.delay(
                str(leave_request.id),
                notify_department_colleagues=notify_department,
            )
        )

        leave_request = LeaveRequest.objects.select_related(
            "employee",
            "leave_type",
            "cover_person",
            "reconciled_by",
        ).get(pk=leave_request.pk)

        return Response(
            LeaveRequestReadSerializer(leave_request).data,
            status=status.HTTP_201_CREATED,
        )

    def _run_bulk_reconcile(
        self,
        *,
        hr_user,
        rows: list[dict],
        allow_insufficient_balance: bool,
        notify_department_colleagues: bool,
    ) -> dict:
        created = []
        errors = []

        for index, row in enumerate(rows):
            try:
                validate_reconcile_row(
                    employee=row["employee"],
                    leave_type=row["leave_type"],
                    start_date=row["start_date"],
                    end_date=row["end_date"],
                    cover_person=row.get("cover_person"),
                    allow_insufficient_balance=allow_insufficient_balance,
                )
                with transaction.atomic():
                    leave_request = reconcile_leave_request(
                        hr_user=hr_user,
                        employee=row["employee"],
                        leave_type=row["leave_type"],
                        start_date=row["start_date"],
                        end_date=row["end_date"],
                        reason=row.get("reason", ""),
                        reconciliation_note=row["reconciliation_note"],
                        cover_person=row.get("cover_person"),
                        allow_insufficient_balance=allow_insufficient_balance,
                    )
                transaction.on_commit(
                    lambda lr_id=str(leave_request.id), nd=notify_department_colleagues: notify_leave_reconciled.delay(
                        lr_id,
                        notify_department_colleagues=nd,
                    )
                )
                created.append(str(leave_request.id))
            except ValidationError as exc:
                detail = exc.detail if hasattr(exc, "detail") else str(exc)
                errors.append({"index": index, "errors": detail})
            except Exception as exc:
                errors.append({"index": index, "errors": str(exc)})

        return {"created": created, "errors": errors, "created_count": len(created)}

    @action(detail=False, methods=["post"], url_path="bulk-reconcile")
    def bulk_reconcile(self, request):
        """
        POST /api/v1/leave-requests/bulk-reconcile/

        HR batch reconcile. Body: { rows: [...], allow_insufficient_balance?, notify_department_colleagues? }
        """
        serializer = LeaveRequestBulkReconcileSerializer(
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        rows = serializer.validated_data["rows"]
        result = self._run_bulk_reconcile(
            hr_user=request.user,
            rows=rows,
            allow_insufficient_balance=serializer.validated_data.get(
                "allow_insufficient_balance", False
            ),
            notify_department_colleagues=serializer.validated_data.get(
                "notify_department_colleagues", False
            ),
        )
        status_code = (
            status.HTTP_201_CREATED
            if result["created_count"] and not result["errors"]
            else status.HTTP_207_MULTI_STATUS
            if result["created_count"]
            else status.HTTP_400_BAD_REQUEST
        )
        return Response(result, status=status_code)

    @action(detail=False, methods=["post"], url_path="bulk-reconcile-csv")
    def bulk_reconcile_csv(self, request):
        """
        POST /api/v1/leave-requests/bulk-reconcile-csv/

        Multipart CSV upload. Columns:
        email, leave_type, start_date, end_date, reconciliation_note
        Optional: reason, cover_person_email
        Form fields: allow_insufficient_balance, notify_department_colleagues (true/false)
        """
        upload_file = request.FILES.get("file")
        if not upload_file:
            raise ValidationError({"file": "CSV file is required (multipart field 'file')."})

        try:
            text = upload_file.read().decode("utf-8-sig")
        except Exception:
            raise ValidationError({"file": "Unable to read file as UTF-8 text."})

        reader = csv.DictReader(io.StringIO(text))
        required = {"email", "leave_type", "start_date", "end_date", "reconciliation_note"}
        if not reader.fieldnames or not required.issubset(
            {h.strip().lower() for h in reader.fieldnames}
        ):
            raise ValidationError(
                {"file": "CSV header must include: email, leave_type, start_date, end_date, reconciliation_note"}
            )

        field_map = {h.strip().lower(): h for h in reader.fieldnames}
        rows = []
        parse_errors = []

        for line_no, row in enumerate(reader, start=2):
            email = (row.get(field_map["email"]) or "").strip().lower()
            leave_type_name = (row.get(field_map["leave_type"]) or "").strip()
            start_str = (row.get(field_map["start_date"]) or "").strip()
            end_str = (row.get(field_map["end_date"]) or "").strip()
            reconciliation_note = (row.get(field_map["reconciliation_note"]) or "").strip()
            reason_key = field_map.get("reason")
            reason = (row.get(reason_key, "") if reason_key else "").strip()
            cover_key = field_map.get("cover_person_email")
            cover_email = (
                (row.get(cover_key, "") if cover_key else "").strip().lower()
            )

            if not all([email, leave_type_name, start_str, end_str, reconciliation_note]):
                parse_errors.append({"line": line_no, "error": "Missing required column value."})
                continue

            try:
                start_date = datetime.date.fromisoformat(start_str)
                end_date = datetime.date.fromisoformat(end_str)
            except ValueError:
                parse_errors.append({"line": line_no, "error": "Dates must be YYYY-MM-DD."})
                continue

            from django.contrib.auth import get_user_model

            User = get_user_model()
            try:
                employee = User.objects.get(email=email, is_active=True)
            except User.DoesNotExist:
                parse_errors.append({"line": line_no, "error": f"Unknown active user: {email}"})
                continue

            try:
                leave_type = LeaveType.objects.get(name=leave_type_name)
            except LeaveType.DoesNotExist:
                parse_errors.append(
                    {"line": line_no, "error": f"Unknown leave type: {leave_type_name}"}
                )
                continue

            cover_person = None
            if cover_email:
                try:
                    cover_person = User.objects.get(email=cover_email, is_active=True)
                except User.DoesNotExist:
                    parse_errors.append(
                        {"line": line_no, "error": f"Unknown cover person: {cover_email}"}
                    )
                    continue

            rows.append(
                {
                    "employee": employee,
                    "leave_type": leave_type,
                    "start_date": start_date,
                    "end_date": end_date,
                    "reason": reason,
                    "reconciliation_note": reconciliation_note,
                    "cover_person": cover_person,
                }
            )

        allow_insufficient = request.data.get("allow_insufficient_balance", "false").lower() in (
            "true",
            "1",
            "yes",
        )
        notify_department = request.data.get("notify_department_colleagues", "false").lower() in (
            "true",
            "1",
            "yes",
        )

        if not rows and parse_errors:
            return Response(
                {"created": [], "errors": parse_errors, "created_count": 0},
                status=status.HTTP_400_BAD_REQUEST,
            )

        result = self._run_bulk_reconcile(
            hr_user=request.user,
            rows=rows,
            allow_insufficient_balance=allow_insufficient,
            notify_department_colleagues=notify_department,
        )
        result["parse_errors"] = parse_errors
        if parse_errors:
            result["errors"] = parse_errors + result.get("errors", [])

        status_code = status.HTTP_201_CREATED
        if result["created_count"] and (result.get("errors") or parse_errors):
            status_code = status.HTTP_207_MULTI_STATUS
        elif not result["created_count"]:
            status_code = status.HTTP_400_BAD_REQUEST

        return Response(result, status=status_code)

    # ------------------------------------------------------------------
    # approve — Stage-based transitions with role enforcement
    # ------------------------------------------------------------------

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None):
        leave_request = LeaveRequest.objects.select_related("employee", "employee__department", "employee__unit", "employee__team").get(pk=pk)

        if leave_request.status not in _APPROVAL_TRANSITIONS:
            raise ValidationError(
                {
                    "status": leave_messages.approve_invalid_status(
                        leave_request.status,
                        _APPROVAL_TRANSITIONS,
                    )
                }
            )

        user = request.user
        comment = request.data.get("comment", "")
        approve_required, _ = comment_requirements(leave_request)
        if approve_required and not str(comment).strip():
            raise ValidationError(
                {"comment": leave_messages.comment_required_for_approve()}
            )

        actor_may_decide(
            user,
            leave_request,
            prevent_self_approval=get_leave_settings().prevent_self_approval,
        )

        prev_status = leave_request.status
        next_status = next_status_from_snapshot(leave_request)
        now = timezone.now()

        with transaction.atomic():
            leave_request.status = next_status
            leave_request.workflow_snapshot = advance_snapshot_pointer(leave_request, next_status)
            leave_request.stage_entered_at = now
            leave_request.sla_notified_at = None
            leave_request.save(
                update_fields=[
                    "status",
                    "workflow_snapshot",
                    "stage_entered_at",
                    "sla_notified_at",
                    "updated_at",
                ]
            )

            if next_status == LeaveRequestStatus.APPROVED:
                deduct_leave_balance(
                    leave_request,
                    source=BalanceTransactionSource.APPROVAL,
                    actor=request.user,
                    reason=comment,
                )

            _create_log(
                leave_request=leave_request,
                actor=request.user,
                action=ApprovalAction.APPROVE,
                previous_status=prev_status,
                new_status=next_status,
                comment=comment,
            )

        transaction.on_commit(
            lambda: notify_approver_required.delay(str(leave_request.id))
        )
        if next_status == LeaveRequestStatus.APPROVED:
            _queue_final_approval_notifications(
                leave_request,
                decision_comment=comment,
            )

        return Response(LeaveRequestReadSerializer(leave_request).data)

    # ------------------------------------------------------------------
    # reject — Role-matched rejection at current stage (comment required)
    # ------------------------------------------------------------------

    @action(detail=True, methods=["post"], url_path="reject")
    def reject(self, request, pk=None):
        leave_request = LeaveRequest.objects.select_related("employee", "employee__department", "employee__unit", "employee__team").get(pk=pk)

        comment = request.data.get("comment", "").strip()
        _, reject_required = comment_requirements(leave_request)
        if reject_required and not comment:
            raise ValidationError(
                {"comment": leave_messages.comment_required_for_reject()}
            )

        if leave_request.status not in _REJECTION_ROLES:
            raise ValidationError(
                {
                    "status": leave_messages.reject_invalid_status(
                        leave_request.status,
                        _REJECTION_ROLES,
                    )
                }
            )

        actor_may_decide(
            request.user,
            leave_request,
            prevent_self_approval=get_leave_settings().prevent_self_approval,
        )

        prev_status = leave_request.status

        with transaction.atomic():
            leave_request.status = LeaveRequestStatus.REJECTED
            leave_request.save(update_fields=["status", "updated_at"])

            release_leave_balance(
                leave_request,
                actor=request.user,
                reason=comment,
                source=BalanceTransactionSource.REJECT_RELEASE,
            )

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
        leave_request = LeaveRequest.objects.select_related("employee").get(pk=pk)
        user = request.user

        is_hr = user.is_staff or user.has_role(RoleName.HR)
        is_owner = leave_request.employee == user

        terminal_statuses = {LeaveRequestStatus.REJECTED, LeaveRequestStatus.CANCELLED}

        if leave_request.status in terminal_statuses:
            raise ValidationError(
                {
                    "status": leave_messages.cancel_not_allowed(
                        leave_request.status
                    )
                }
            )

        if is_hr:
            pass  # HR can cancel any non-terminal request
        elif is_owner:
            allowed = {
                LeaveRequestStatus.DRAFT,
                LeaveRequestStatus.PENDING_TEAM_LEAD,
                LeaveRequestStatus.PENDING_SUPERVISOR,
                LeaveRequestStatus.PENDING_MANAGER,
            }
            if leave_request.status not in allowed:
                raise ValidationError(
                    {
                        "status": (
                            "You can only cancel your own leave while it is still a draft "
                            "or waiting for team lead, supervisor, or manager approval. "
                            f"This request is currently "
                            f"{leave_messages.leave_request_status_label(leave_request.status)}. "
                            "Contact HR if you need to withdraw it after that stage."
                        )
                    }
                )
        else:
            raise PermissionDenied("You do not have permission to cancel this request.")

        prev_status = leave_request.status
        was_approved = prev_status == LeaveRequestStatus.APPROVED
        cancel_comment = request.data.get("comment", "")

        with transaction.atomic():
            leave_request.status = LeaveRequestStatus.CANCELLED
            leave_request.save(update_fields=["status", "updated_at"])

            if was_approved:
                restore_leave_balance(
                    leave_request,
                    actor=user,
                    reason=cancel_comment or "Leave request cancelled.",
                )
            else:
                release_leave_balance(
                    leave_request,
                    actor=user,
                    reason=cancel_comment or "Leave request cancelled.",
                    source=BalanceTransactionSource.CANCEL_RELEASE,
                )

            _create_log(
                leave_request=leave_request,
                actor=user,
                action=ApprovalAction.CANCEL,
                previous_status=prev_status,
                new_status=LeaveRequestStatus.CANCELLED,
                comment=cancel_comment,
            )

        return Response(LeaveRequestReadSerializer(leave_request).data)

    # ------------------------------------------------------------------
    # logs — Approval audit trail
    # ------------------------------------------------------------------

    @action(detail=True, methods=["get"], url_path="logs")
    def logs(self, request, pk=None):
        leave_request = LeaveRequest.objects.select_related("employee", "employee__department", "employee__unit", "employee__team", "cover_person").get(pk=pk)
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
            if user.has_role(RoleName.SUPERVISOR):
                if leave_request.employee.unit_id:
                    configured = getattr(leave_request.employee.unit, "supervisor_id", None) == user.pk
                    same_unit = getattr(user, "unit_id", None) == leave_request.employee.unit_id
                    if configured or same_unit:
                        can_view = True

            # Team lead can view logs for their team members
            if user.has_role(RoleName.TEAM_LEAD):
                if leave_request.employee.team_id:
                    configured = getattr(leave_request.employee.team, "team_lead_id", None) == user.pk
                    same_team = getattr(user, "team_id", None) == leave_request.employee.team_id
                    if configured or same_team:
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


class LeaveAccrualPreviewView(APIView):
    """
    POST /api/v1/leave-accrual/preview/ — HR/Admin dry-run of accrual/rollover.
    Never writes balances; use the management command or Beat to apply.
    """

    permission_classes = [permissions.IsAuthenticated, (IsHR | permissions.IsAdminUser)]

    def post(self, request):
        serializer = LeaveAccrualPreviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = preview_or_run_accrual(
            as_of=serializer.validated_data.get("as_of"),
            year=serializer.validated_data.get("year"),
            month=serializer.validated_data.get("month"),
            include_rollover=serializer.validated_data.get("include_rollover", True),
            include_monthly=serializer.validated_data.get("include_monthly", True),
            include_weekly=serializer.validated_data.get("include_weekly", False),
            include_anniversary=serializer.validated_data.get("include_anniversary", False),
            include_carry_expiry=serializer.validated_data.get("include_carry_expiry", True),
            dry_run=True,
        )
        return Response(payload)


class LeaveSettingsView(APIView):
    """GET/PATCH /api/v1/leave-settings/ — singleton org settings."""

    def get_permissions(self):
        if self.request.method == "GET":
            return [permissions.IsAuthenticated()]
        return [permissions.IsAuthenticated(), (IsHR | permissions.IsAdminUser)()]

    def get(self, request):
        return Response(LeaveSettingsSerializer(get_leave_settings()).data)

    def patch(self, request):
        instance = get_leave_settings()
        previous = snapshot_leave_settings(instance)
        serializer = LeaveSettingsSerializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        instance.refresh_from_db()
        record_settings_audit(
            actor=request.user,
            object_type="LeaveSettings",
            object_id=instance.pk,
            action=SettingsAuditAction.UPDATE,
            previous_values=previous,
            new_values=snapshot_leave_settings(instance),
            reason=request.data.get("reason", ""),
            request=request,
        )
        return Response(LeaveSettingsSerializer(instance).data)


def _json_safe(value):
    return json.loads(json.dumps(value, default=str))


def _audit_calendar_write(*, actor, request, instance, action, object_type, previous, new_values):
    record_settings_audit(
        actor=actor,
        object_type=object_type,
        object_id=instance.pk,
        action=action,
        previous_values=_json_safe(previous) if previous is not None else None,
        new_values=_json_safe(new_values) if new_values is not None else None,
        reason=request.data.get("reason", "") if hasattr(request, "data") else "",
        request=request,
    )


class _CalendarWriteMixin:
    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [permissions.IsAuthenticated()]
        return [permissions.IsAuthenticated(), (IsHR | permissions.IsAdminUser)()]


class WorkingCalendarViewSet(_CalendarWriteMixin, viewsets.ModelViewSet):
    queryset = WorkingCalendar.objects.all()
    serializer_class = WorkingCalendarSerializer

    def perform_create(self, serializer):
        instance = serializer.save()
        _audit_calendar_write(
            actor=self.request.user,
            request=self.request,
            instance=instance,
            action=SettingsAuditAction.CREATE,
            object_type="WorkingCalendar",
            previous=None,
            new_values=WorkingCalendarSerializer(instance).data,
        )

    def perform_update(self, serializer):
        previous = WorkingCalendarSerializer(serializer.instance).data
        instance = serializer.save()
        _audit_calendar_write(
            actor=self.request.user,
            request=self.request,
            instance=instance,
            action=SettingsAuditAction.UPDATE,
            object_type="WorkingCalendar",
            previous=previous,
            new_values=WorkingCalendarSerializer(instance).data,
        )

    def perform_destroy(self, instance):
        previous = WorkingCalendarSerializer(instance).data
        pk = instance.pk
        instance.delete()
        record_settings_audit(
            actor=self.request.user,
            object_type="WorkingCalendar",
            object_id=pk,
            action=SettingsAuditAction.DELETE,
            previous_values=_json_safe(previous),
            new_values=None,
            reason=self.request.data.get("reason", "") if hasattr(self.request, "data") else "",
            request=self.request,
        )


class HolidayCalendarViewSet(_CalendarWriteMixin, viewsets.ModelViewSet):
    queryset = HolidayCalendar.objects.prefetch_related("holidays").all()
    serializer_class = HolidayCalendarSerializer

    def perform_create(self, serializer):
        instance = serializer.save()
        _audit_calendar_write(
            actor=self.request.user,
            request=self.request,
            instance=instance,
            action=SettingsAuditAction.CREATE,
            object_type="HolidayCalendar",
            previous=None,
            new_values=HolidayCalendarSerializer(instance).data,
        )

    def perform_update(self, serializer):
        previous = HolidayCalendarSerializer(serializer.instance).data
        instance = serializer.save()
        _audit_calendar_write(
            actor=self.request.user,
            request=self.request,
            instance=instance,
            action=SettingsAuditAction.UPDATE,
            object_type="HolidayCalendar",
            previous=previous,
            new_values=HolidayCalendarSerializer(instance).data,
        )

    def perform_destroy(self, instance):
        previous = HolidayCalendarSerializer(instance).data
        pk = instance.pk
        instance.delete()
        record_settings_audit(
            actor=self.request.user,
            object_type="HolidayCalendar",
            object_id=pk,
            action=SettingsAuditAction.DELETE,
            previous_values=_json_safe(previous),
            new_values=None,
            reason=self.request.data.get("reason", "") if hasattr(self.request, "data") else "",
            request=self.request,
        )

    @action(detail=True, methods=["post"], url_path="holidays")
    def add_holiday(self, request, pk=None):
        calendar = self.get_object()
        serializer = CalendarHolidaySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        holiday = CalendarHoliday.objects.create(calendar=calendar, **serializer.validated_data)
        record_settings_audit(
            actor=request.user,
            object_type="CalendarHoliday",
            object_id=holiday.pk,
            action=SettingsAuditAction.CREATE,
            previous_values=None,
            new_values=_json_safe(CalendarHolidaySerializer(holiday).data),
            reason=request.data.get("reason", ""),
            request=request,
        )
        return Response(CalendarHolidaySerializer(holiday).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["delete"], url_path="holidays/(?P<holiday_id>[^/.]+)")
    def delete_holiday(self, request, pk=None, holiday_id=None):
        calendar = self.get_object()
        holiday = CalendarHoliday.objects.filter(calendar=calendar, pk=holiday_id).first()
        if holiday is None:
            raise ValidationError({"holiday_id": "Holiday not found on this calendar."})
        previous = CalendarHolidaySerializer(holiday).data
        hid = holiday.pk
        holiday.delete()
        record_settings_audit(
            actor=request.user,
            object_type="CalendarHoliday",
            object_id=hid,
            action=SettingsAuditAction.DELETE,
            previous_values=_json_safe(previous),
            new_values=None,
            reason=request.data.get("reason", "") if hasattr(request, "data") else "",
            request=request,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class CalendarAssignmentViewSet(_CalendarWriteMixin, viewsets.ModelViewSet):
    queryset = CalendarAssignment.objects.select_related(
        "working_calendar", "holiday_calendar", "employee", "department"
    ).all()
    serializer_class = CalendarAssignmentSerializer

    def perform_create(self, serializer):
        instance = serializer.save()
        _audit_calendar_write(
            actor=self.request.user,
            request=self.request,
            instance=instance,
            action=SettingsAuditAction.CREATE,
            object_type="CalendarAssignment",
            previous=None,
            new_values=CalendarAssignmentSerializer(instance).data,
        )

    def perform_update(self, serializer):
        previous = CalendarAssignmentSerializer(serializer.instance).data
        instance = serializer.save()
        _audit_calendar_write(
            actor=self.request.user,
            request=self.request,
            instance=instance,
            action=SettingsAuditAction.UPDATE,
            object_type="CalendarAssignment",
            previous=previous,
            new_values=CalendarAssignmentSerializer(instance).data,
        )

    def perform_destroy(self, instance):
        previous = CalendarAssignmentSerializer(instance).data
        pk = instance.pk
        instance.delete()
        record_settings_audit(
            actor=self.request.user,
            object_type="CalendarAssignment",
            object_id=pk,
            action=SettingsAuditAction.DELETE,
            previous_values=_json_safe(previous),
            new_values=None,
            reason=self.request.data.get("reason", "") if hasattr(self.request, "data") else "",
            request=self.request,
        )


class LeaveWorkflowTemplateViewSet(_CalendarWriteMixin, viewsets.ModelViewSet):
    queryset = LeaveWorkflowTemplate.objects.prefetch_related("stages").all()
    serializer_class = LeaveWorkflowTemplateSerializer

    def perform_create(self, serializer):
        instance = serializer.save()
        record_settings_audit(
            actor=self.request.user,
            object_type="LeaveWorkflowTemplate",
            object_id=instance.pk,
            action=SettingsAuditAction.CREATE,
            previous_values=None,
            new_values=_json_safe(LeaveWorkflowTemplateSerializer(instance).data),
            reason=self.request.data.get("reason", ""),
            request=self.request,
        )

    def perform_update(self, serializer):
        previous = LeaveWorkflowTemplateSerializer(serializer.instance).data
        instance = serializer.save()
        record_settings_audit(
            actor=self.request.user,
            object_type="LeaveWorkflowTemplate",
            object_id=instance.pk,
            action=SettingsAuditAction.UPDATE,
            previous_values=_json_safe(previous),
            new_values=_json_safe(LeaveWorkflowTemplateSerializer(instance).data),
            reason=self.request.data.get("reason", ""),
            request=self.request,
        )

    def perform_destroy(self, instance):
        previous = LeaveWorkflowTemplateSerializer(instance).data
        pk = instance.pk
        instance.delete()
        record_settings_audit(
            actor=self.request.user,
            object_type="LeaveWorkflowTemplate",
            object_id=pk,
            action=SettingsAuditAction.DELETE,
            previous_values=_json_safe(previous),
            new_values=None,
            reason=self.request.data.get("reason", "") if hasattr(self.request, "data") else "",
            request=self.request,
        )

    @action(detail=True, methods=["post"], url_path="simulate")
    def simulate(self, request, pk=None):
        template = self.get_object()
        serializer = LeaveWorkflowSimulateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = simulate_workflow(
            template,
            serializer.validated_data["employee"],
            leave_type=serializer.validated_data.get("leave_type"),
        )
        return Response(payload)


class ApproverDelegateViewSet(viewsets.ModelViewSet):
    serializer_class = ApproverDelegateSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = ApproverDelegate.objects.select_related("user", "delegate").all()
        user = self.request.user
        if user.is_staff or user.has_role(RoleName.HR):
            return qs
        return qs.filter(Q(user=user) | Q(delegate=user))

    def perform_create(self, serializer):
        user = self.request.user
        is_hr = user.is_staff or user.has_role(RoleName.HR)
        target = serializer.validated_data.get("user")
        if not is_hr and target != user:
            raise PermissionDenied("You can only create delegations for yourself.")
        instance = serializer.save()
        record_settings_audit(
            actor=user,
            object_type="ApproverDelegate",
            object_id=instance.pk,
            action=SettingsAuditAction.CREATE,
            previous_values=None,
            new_values=_json_safe(ApproverDelegateSerializer(instance).data),
            reason=self.request.data.get("reason", ""),
            request=self.request,
        )

    def perform_update(self, serializer):
        user = self.request.user
        is_hr = user.is_staff or user.has_role(RoleName.HR)
        if not is_hr and serializer.instance.user_id != user.pk:
            raise PermissionDenied("You can only edit your own delegations.")
        previous = ApproverDelegateSerializer(serializer.instance).data
        instance = serializer.save()
        record_settings_audit(
            actor=user,
            object_type="ApproverDelegate",
            object_id=instance.pk,
            action=SettingsAuditAction.UPDATE,
            previous_values=_json_safe(previous),
            new_values=_json_safe(ApproverDelegateSerializer(instance).data),
            reason=self.request.data.get("reason", ""),
            request=self.request,
        )

    def perform_destroy(self, instance):
        user = self.request.user
        is_hr = user.is_staff or user.has_role(RoleName.HR)
        if not is_hr and instance.user_id != user.pk:
            raise PermissionDenied("You can only delete your own delegations.")
        previous = ApproverDelegateSerializer(instance).data
        pk = instance.pk
        instance.delete()
        record_settings_audit(
            actor=user,
            object_type="ApproverDelegate",
            object_id=pk,
            action=SettingsAuditAction.DELETE,
            previous_values=_json_safe(previous),
            new_values=None,
            reason=self.request.data.get("reason", "") if hasattr(self.request, "data") else "",
            request=self.request,
        )


class LeaveBlackoutPeriodViewSet(_CalendarWriteMixin, viewsets.ModelViewSet):
    queryset = LeaveBlackoutPeriod.objects.prefetch_related("leave_types").select_related(
        "department"
    )
    serializer_class = LeaveBlackoutPeriodSerializer

    def perform_create(self, serializer):
        instance = serializer.save()
        _audit_calendar_write(
            actor=self.request.user,
            request=self.request,
            instance=instance,
            action=SettingsAuditAction.CREATE,
            object_type="LeaveBlackoutPeriod",
            previous=None,
            new_values=LeaveBlackoutPeriodSerializer(instance).data,
        )

    def perform_update(self, serializer):
        previous = LeaveBlackoutPeriodSerializer(serializer.instance).data
        instance = serializer.save()
        _audit_calendar_write(
            actor=self.request.user,
            request=self.request,
            instance=instance,
            action=SettingsAuditAction.UPDATE,
            object_type="LeaveBlackoutPeriod",
            previous=previous,
            new_values=LeaveBlackoutPeriodSerializer(instance).data,
        )

    def perform_destroy(self, instance):
        previous = LeaveBlackoutPeriodSerializer(instance).data
        pk = instance.pk
        instance.delete()
        record_settings_audit(
            actor=self.request.user,
            object_type="LeaveBlackoutPeriod",
            object_id=pk,
            action=SettingsAuditAction.DELETE,
            previous_values=_json_safe(previous),
            new_values=None,
            reason=self.request.data.get("reason", "") if hasattr(self.request, "data") else "",
            request=self.request,
        )


class LeaveReportsView(APIView):
    """
    GET /api/v1/leave-reports/{kind}/
    kind: utilization | who-is-out | liability
    Optional ?format=csv for HR export.
    """

    permission_classes = [permissions.IsAuthenticated, (IsHR | permissions.IsAdminUser)]

    def get(self, request, kind):
        from .services import liability_report, utilization_report, who_is_out_report

        today = datetime.date.today()
        year = request.query_params.get("year", today.year)
        try:
            year = int(year)
        except (TypeError, ValueError):
            raise ValidationError({"year": "year must be an integer."})
        department_id = request.query_params.get("department") or None
        as_csv = (request.query_params.get("export") or request.query_params.get("format") or "").lower() == "csv"

        if kind == "utilization":
            rows = utilization_report(year=year, department_id=department_id)
            headers = [
                "department_id",
                "department_name",
                "leave_type_id",
                "leave_type_name",
                "leave_type_code",
                "allocated_days",
                "used_days",
                "pending_days",
                "utilization",
            ]
        elif kind == "liability":
            rows = liability_report(year=year, department_id=department_id)
            headers = [
                "department_id",
                "department_name",
                "leave_type_id",
                "leave_type_name",
                "leave_type_code",
                "allocated_days",
                "used_days",
                "pending_days",
                "utilization",
                "liability_days",
            ]
        elif kind == "who-is-out":
            scope = (request.query_params.get("scope") or "today").lower()
            if scope == "week":
                start = today - datetime.timedelta(days=today.weekday())
                end = start + datetime.timedelta(days=6)
            else:
                start = end = today
            date_from = request.query_params.get("from")
            date_to = request.query_params.get("to")
            if date_from:
                start = datetime.date.fromisoformat(date_from)
            if date_to:
                end = datetime.date.fromisoformat(date_to)
            rows = who_is_out_report(
                start_date=start, end_date=end, department_id=department_id
            )
            headers = [
                "id",
                "employee_id",
                "employee_email",
                "department_id",
                "department_name",
                "leave_type",
                "leave_type_code",
                "start_date",
                "end_date",
                "total_working_days",
            ]
        else:
            raise ValidationError(
                {"kind": "kind must be utilization, who-is-out, or liability."}
            )

        if as_csv:
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
            response = HttpResponse(buf.getvalue(), content_type="text/csv")
            response["Content-Disposition"] = f'attachment; filename="leave-{kind}.csv"'
            return response
        return Response({"kind": kind, "year": year, "results": rows})
