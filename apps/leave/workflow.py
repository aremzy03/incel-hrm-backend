"""Configurable leave approval workflows (Sprint 6).

Snapshots are taken at submit. Later template edits must not change in-flight routing.
API statuses stay PENDING_TEAM_LEAD / PENDING_SUPERVISOR / PENDING_MANAGER / PENDING_HR / PENDING_ED.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from . import messages as leave_messages

from apps.accounts.models import RoleName, get_or_create_management_department

from .models import (
    DEFAULT_WORKFLOW_NAME,
    ApproverDelegate,
    ApproverSource,
    LeaveRequestStatus,
    LeaveWorkflowStage,
    LeaveWorkflowTemplate,
)

User = get_user_model()

SENIOR_ROLES = (
    RoleName.TEAM_LEAD,
    RoleName.SUPERVISOR,
    RoleName.LINE_MANAGER,
    RoleName.HR,
    RoleName.EXECUTIVE_DIRECTOR,
    RoleName.MANAGING_DIRECTOR,
)

SOURCE_TO_ROLE = {
    ApproverSource.TEAM_LEAD: RoleName.TEAM_LEAD,
    ApproverSource.SUPERVISOR: RoleName.SUPERVISOR,
    ApproverSource.LINE_MANAGER: RoleName.LINE_MANAGER,
    ApproverSource.HR: RoleName.HR,
    ApproverSource.EXECUTIVE_DIRECTOR: RoleName.EXECUTIVE_DIRECTOR,
}

SOURCE_TO_STATUS = {
    ApproverSource.TEAM_LEAD: LeaveRequestStatus.PENDING_TEAM_LEAD,
    ApproverSource.SUPERVISOR: LeaveRequestStatus.PENDING_SUPERVISOR,
    ApproverSource.LINE_MANAGER: LeaveRequestStatus.PENDING_MANAGER,
    ApproverSource.HR: LeaveRequestStatus.PENDING_HR,
    ApproverSource.EXECUTIVE_DIRECTOR: LeaveRequestStatus.PENDING_ED,
}

LEGACY_TRANSITIONS = {
    LeaveRequestStatus.PENDING_TEAM_LEAD: (
        LeaveRequestStatus.PENDING_SUPERVISOR,
        RoleName.TEAM_LEAD,
    ),
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

LEGACY_REJECTION_ROLES = {status: role for status, (_, role) in LEGACY_TRANSITIONS.items()}


def default_stage_specs():
    senior = list(SENIOR_ROLES)
    md_ed = [RoleName.EXECUTIVE_DIRECTOR, RoleName.MANAGING_DIRECTOR]
    return [
        {
            "order": 1,
            "approver_source": ApproverSource.TEAM_LEAD,
            "status_code": LeaveRequestStatus.PENDING_TEAM_LEAD,
            "skip_if_unresolved": True,
            "is_optional": False,
            "skip_if_requester_roles": senior,
            "use_management_line_manager_for_line_manager_requester": False,
            "sla_hours": None,
        },
        {
            "order": 2,
            "approver_source": ApproverSource.SUPERVISOR,
            "status_code": LeaveRequestStatus.PENDING_SUPERVISOR,
            "skip_if_unresolved": True,
            "is_optional": False,
            "skip_if_requester_roles": senior,
            "use_management_line_manager_for_line_manager_requester": False,
            "sla_hours": None,
        },
        {
            "order": 3,
            "approver_source": ApproverSource.LINE_MANAGER,
            "status_code": LeaveRequestStatus.PENDING_MANAGER,
            "skip_if_unresolved": False,
            "is_optional": False,
            "skip_if_requester_roles": md_ed,
            "use_management_line_manager_for_line_manager_requester": True,
            "sla_hours": None,
        },
        {
            "order": 4,
            "approver_source": ApproverSource.HR,
            "status_code": LeaveRequestStatus.PENDING_HR,
            "skip_if_unresolved": False,
            "is_optional": False,
            "skip_if_requester_roles": [RoleName.HR, *md_ed],
            "use_management_line_manager_for_line_manager_requester": False,
            "sla_hours": None,
        },
        {
            "order": 5,
            "approver_source": ApproverSource.EXECUTIVE_DIRECTOR,
            "status_code": LeaveRequestStatus.PENDING_ED,
            "skip_if_unresolved": False,
            "is_optional": False,
            "skip_if_requester_roles": md_ed,
            "use_management_line_manager_for_line_manager_requester": False,
            "sla_hours": None,
        },
    ]


def ensure_default_workflow_template() -> LeaveWorkflowTemplate:
    template, created = LeaveWorkflowTemplate.objects.get_or_create(
        name=DEFAULT_WORKFLOW_NAME,
        defaults={
            "is_active": True,
            "is_org_default": True,
            "reject_comment_required": True,
            "approve_comment_required": False,
            "auto_approve_after_sla": False,
        },
    )
    if created or not template.stages.exists():
        template.stages.all().delete()
        for spec in default_stage_specs():
            LeaveWorkflowStage.objects.create(template=template, **spec)
        if not template.is_org_default:
            template.is_org_default = True
            template.save(update_fields=["is_org_default", "updated_at"])
    return template


def resolve_workflow_template(leave_type=None) -> LeaveWorkflowTemplate | None:
    if leave_type is not None:
        typed = (
            LeaveWorkflowTemplate.objects.filter(
                is_active=True, leave_type=leave_type
            )
            .prefetch_related("stages")
            .first()
        )
        if typed:
            return typed
    default = (
        LeaveWorkflowTemplate.objects.filter(is_active=True, is_org_default=True)
        .prefetch_related("stages")
        .first()
    )
    if default:
        return default
    named = (
        LeaveWorkflowTemplate.objects.filter(is_active=True, name=DEFAULT_WORKFLOW_NAME)
        .prefetch_related("stages")
        .first()
    )
    return named


def _requester_roles(employee) -> set[str]:
    names = set()
    if employee is None:
        return names
    if hasattr(employee, "has_role"):
        for role in RoleName:
            if employee.has_role(role):
                names.add(role)
    return names


def _stage_dict(stage: LeaveWorkflowStage) -> dict:
    return {
        "id": str(stage.id),
        "order": stage.order,
        "approver_source": stage.approver_source,
        "status_code": stage.status_code,
        "named_user_id": str(stage.named_user_id) if stage.named_user_id else None,
        "role_name": stage.role_name or "",
        "sla_hours": stage.sla_hours,
        "skip_if_unresolved": stage.skip_if_unresolved,
        "is_optional": stage.is_optional,
        "skip_if_requester_roles": list(stage.skip_if_requester_roles or []),
        "use_management_line_manager_for_line_manager_requester": (
            stage.use_management_line_manager_for_line_manager_requester
        ),
    }


def stage_is_resolved(stage: dict, employee) -> bool:
    return bool(resolve_stage_approvers(stage, employee))


def resolve_stage_approvers(stage: dict, employee) -> list:
    source = stage.get("approver_source")
    use_mgmt = stage.get("use_management_line_manager_for_line_manager_requester") and employee and employee.has_role(
        RoleName.LINE_MANAGER
    )
    if source == ApproverSource.TEAM_LEAD:
        team = getattr(employee, "team", None)
        lead = getattr(team, "team_lead", None) if team else None
        return [lead] if lead else []
    if source == ApproverSource.SUPERVISOR:
        unit = getattr(employee, "unit", None)
        supervisor = getattr(unit, "supervisor", None) if unit else None
        return [supervisor] if supervisor else []
    if source == ApproverSource.LINE_MANAGER:
        if use_mgmt:
            mgmt = get_or_create_management_department()
            return [mgmt.line_manager] if mgmt.line_manager_id else []
        manager = employee.get_department_line_manager() if employee else None
        return [manager] if manager else []
    if source == ApproverSource.HR:
        return list(User.objects.filter(is_active=True, user_roles__role__name=RoleName.HR).distinct())
    if source == ApproverSource.EXECUTIVE_DIRECTOR:
        return list(
            User.objects.filter(
                is_active=True, user_roles__role__name=RoleName.EXECUTIVE_DIRECTOR
            ).distinct()
        )
    if source == ApproverSource.NAMED_USER:
        uid = stage.get("named_user_id")
        if not uid:
            return []
        user = User.objects.filter(pk=uid, is_active=True).first()
        return [user] if user else []
    if source == ApproverSource.ROLE:
        role_name = stage.get("role_name") or ""
        if not role_name:
            return []
        return list(
            User.objects.filter(is_active=True, user_roles__role__name=role_name).distinct()
        )
    return []


def _role_skips(stage: dict, requester_roles: set[str]) -> bool:
    skip_roles = {str(r) for r in (stage.get("skip_if_requester_roles") or [])}
    return bool(skip_roles & requester_roles)


def build_applicable_stages(template: LeaveWorkflowTemplate, employee) -> list[dict]:
    requester_roles = _requester_roles(employee)
    raw = [_stage_dict(s) for s in template.stages.all().order_by("order")]
    after_role = [s for s in raw if not _role_skips(s, requester_roles)]
    remaining = []
    prefix = True
    for stage in after_role:
        resolved = stage_is_resolved(stage, employee)
        if stage.get("is_optional") and not resolved:
            continue
        if prefix and stage.get("skip_if_unresolved") and not resolved:
            continue
        prefix = False
        remaining.append(stage)
    return remaining


def snapshot_workflow_for_employee(employee, leave_type=None) -> dict:
    template = resolve_workflow_template(leave_type)
    if template is None:
        template = ensure_default_workflow_template()
    stages = build_applicable_stages(template, employee)
    manager_approver_is_management = bool(
        employee
        and employee.has_role(RoleName.LINE_MANAGER)
        and any(s.get("use_management_line_manager_for_line_manager_requester") for s in stages)
    )
    skip_hr_stage = employee.has_role(RoleName.HR) if employee else False
    first_status = stages[0]["status_code"] if stages else LeaveRequestStatus.APPROVED
    return {
        "template_id": str(template.id),
        "template_name": template.name,
        "reject_comment_required": template.reject_comment_required,
        "approve_comment_required": template.approve_comment_required,
        "auto_approve_after_sla": template.auto_approve_after_sla,
        "template_sla_hours": template.sla_hours,
        "skip_hr_stage": skip_hr_stage,
        "manager_approver_is_management": manager_approver_is_management,
        "stages": stages,
        "active_stage_order": stages[0]["order"] if stages else None,
        "first_status": first_status,
    }


def plan_leave_submission(employee, leave_type=None) -> dict:
    snapshot = snapshot_workflow_for_employee(employee, leave_type)
    first_status = snapshot["first_status"]
    manager_approver_is_management = snapshot["manager_approver_is_management"]
    skip_hr_stage = snapshot["skip_hr_stage"]
    if first_status != LeaveRequestStatus.APPROVED:
        if manager_approver_is_management:
            mgmt = get_or_create_management_department()
            if mgmt.line_manager_id is None:
                raise ValidationError(
                    {"department": leave_messages.management_missing_line_manager()}
                )
        else:
            lm = employee.get_department_line_manager() if employee else None
            if lm is None:
                raise ValidationError(
                    {"department": leave_messages.department_missing_line_manager()}
                )
    return {
        "first_status": first_status,
        "skip_hr_stage": skip_hr_stage,
        "manager_approver_is_management": manager_approver_is_management,
        "workflow_snapshot": snapshot,
    }


def current_snapshot_stage(leave_request) -> dict | None:
    snapshot = leave_request.workflow_snapshot or {}
    stages = snapshot.get("stages") or []
    for stage in stages:
        if stage.get("status_code") == leave_request.status:
            return stage
    return None


def next_status_from_snapshot(leave_request) -> str:
    snapshot = leave_request.workflow_snapshot or {}
    stages = snapshot.get("stages") or []
    found = False
    for stage in stages:
        if found:
            return stage["status_code"]
        if stage.get("status_code") == leave_request.status:
            found = True
    if found:
        return LeaveRequestStatus.APPROVED
    next_status, _ = LEGACY_TRANSITIONS[leave_request.status]
    if (
        leave_request.skip_hr_stage
        and leave_request.status == LeaveRequestStatus.PENDING_MANAGER
        and next_status == LeaveRequestStatus.PENDING_HR
    ):
        return LeaveRequestStatus.PENDING_ED
    return next_status


def required_role_for_status(leave_request) -> str | None:
    stage = current_snapshot_stage(leave_request)
    if stage:
        source = stage.get("approver_source")
        if source == ApproverSource.ROLE:
            return stage.get("role_name") or None
        if source == ApproverSource.NAMED_USER:
            return None
        return SOURCE_TO_ROLE.get(source)
    _, role = LEGACY_TRANSITIONS.get(leave_request.status, (None, None))
    return role


def active_delegates_for(primary_user, on_date=None):
    if primary_user is None:
        return []
    on_date = on_date or timezone.localdate()
    return list(
        ApproverDelegate.objects.filter(
            user=primary_user,
            is_active=True,
            start_date__lte=on_date,
            end_date__gte=on_date,
        ).select_related("delegate")
    )


def users_covering_as_delegate(delegate_user, on_date=None):
    if delegate_user is None:
        return []
    on_date = on_date or timezone.localdate()
    return list(
        ApproverDelegate.objects.filter(
            delegate=delegate_user,
            is_active=True,
            start_date__lte=on_date,
            end_date__gte=on_date,
        ).select_related("user")
    )


def expand_with_delegates(users, on_date=None) -> list:
    seen = {}
    for user in users:
        if user is None:
            continue
        seen[user.pk] = user
        for row in active_delegates_for(user, on_date=on_date):
            if row.delegate_id:
                seen[row.delegate_id] = row.delegate
    return list(seen.values())


def _passes_org_identity(user, leave_request, stage: dict | None) -> bool:
    status_code = leave_request.status
    source = (stage or {}).get("approver_source")
    employee = leave_request.employee

    if source == ApproverSource.NAMED_USER:
        return str(user.pk) == str((stage or {}).get("named_user_id"))

    if status_code == LeaveRequestStatus.PENDING_MANAGER and leave_request.manager_approver_is_management:
        mgmt = get_or_create_management_department()
        return mgmt.line_manager_id == user.pk

    if status_code == LeaveRequestStatus.PENDING_TEAM_LEAD or source == ApproverSource.TEAM_LEAD:
        team = getattr(employee, "team", None)
        if not team:
            return False
        same_team_member = getattr(user, "team_id", None) == team.pk
        is_configured_lead = team.team_lead_id == user.pk
        return bool(is_configured_lead or same_team_member)

    if status_code == LeaveRequestStatus.PENDING_SUPERVISOR or source == ApproverSource.SUPERVISOR:
        unit = getattr(employee, "unit", None)
        if not unit:
            return False
        same_unit_member = getattr(user, "unit_id", None) == unit.pk
        is_configured_supervisor = unit.supervisor_id == user.pk
        return bool(is_configured_supervisor or same_unit_member)

    return True


def _user_passes_stage(user, leave_request, *, required_role: str | None, stage: dict | None) -> bool:
    if required_role and not user.has_role(required_role):
        if (stage or {}).get("approver_source") == ApproverSource.NAMED_USER:
            pass
        else:
            return False
    return _passes_org_identity(user, leave_request, stage)


def actor_may_decide(user, leave_request, *, prevent_self_approval: bool) -> None:
    if leave_request.status not in LEGACY_TRANSITIONS:
        raise ValidationError(
            {
                "status": (
                    f"Request cannot be actioned from status '{leave_request.status}'. "
                    f"Approvable statuses: {list(LEGACY_TRANSITIONS)}"
                )
            }
        )
    if prevent_self_approval and user.pk == leave_request.employee_id:
        raise PermissionDenied(
            "You cannot approve or reject your own leave request. "
            "Another approver in the workflow must action it."
        )

    stage = current_snapshot_stage(leave_request)
    required_role = required_role_for_status(leave_request)

    if _user_passes_stage(user, leave_request, required_role=required_role, stage=stage):
        return

    for row in users_covering_as_delegate(user):
        primary = row.user
        if _user_passes_stage(primary, leave_request, required_role=required_role, stage=stage):
            return

    if required_role:
        raise PermissionDenied(
            f"You cannot action this request at the "
            f"{leave_messages.leave_request_status_label(leave_request.status)} stage. "
            f"Only a user with the {required_role.replace('_', ' ').lower()} role "
            "or their active delegate may approve or reject it."
        )
    raise PermissionDenied(
        "You are not assigned as an approver for this leave request at its current stage."
    )


def comment_requirements(leave_request) -> tuple[bool, bool]:
    snapshot = leave_request.workflow_snapshot or {}
    reject_required = snapshot.get("reject_comment_required", True)
    approve_required = snapshot.get("approve_comment_required", False)
    return bool(approve_required), bool(reject_required)


def sla_hours_for_request(leave_request, leave_settings=None) -> int | None:
    stage = current_snapshot_stage(leave_request)
    snapshot = leave_request.workflow_snapshot or {}
    if stage and stage.get("sla_hours"):
        return int(stage["sla_hours"])
    if snapshot.get("template_sla_hours"):
        return int(snapshot["template_sla_hours"])
    if leave_settings and getattr(leave_settings, "approval_sla_hours", None):
        return int(leave_settings.approval_sla_hours)
    return None


def stage_is_overdue(leave_request, leave_settings=None) -> bool:
    hours = sla_hours_for_request(leave_request, leave_settings=leave_settings)
    if not hours:
        return False
    entered = leave_request.stage_entered_at or leave_request.updated_at
    if entered is None:
        return False
    return timezone.now() >= entered + timedelta(hours=hours)


def simulate_workflow(template: LeaveWorkflowTemplate, employee, leave_type=None) -> dict:
    stages = build_applicable_stages(template, employee)
    rows = []
    for stage in stages:
        approvers = resolve_stage_approvers(stage, employee)
        rows.append(
            {
                **stage,
                "resolved_approvers": [
                    {
                        "id": str(u.pk),
                        "email": u.email,
                        "first_name": u.first_name,
                        "last_name": u.last_name,
                    }
                    for u in approvers
                ],
            }
        )
    first_status = rows[0]["status_code"] if rows else LeaveRequestStatus.APPROVED
    return {
        "template_id": str(template.id),
        "template_name": template.name,
        "employee_id": str(employee.pk) if employee else None,
        "leave_type_id": str(leave_type.pk) if leave_type else None,
        "first_status": first_status,
        "stages": rows,
        "notes": (
            "Duration/department routing conditions are not evaluated; "
            "pick a leave-type-specific template or the org default."
        ),
    }


def advance_snapshot_pointer(leave_request, new_status: str) -> dict:
    snapshot = deepcopy(leave_request.workflow_snapshot or {})
    stages = snapshot.get("stages") or []
    snapshot["active_stage_order"] = None
    for stage in stages:
        if stage.get("status_code") == new_status:
            snapshot["active_stage_order"] = stage.get("order")
            break
    return snapshot
