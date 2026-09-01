import datetime
import json
import logging

from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import strip_tags

import redis

from apps.accounts.models import RoleName, get_or_create_management_department
from apps.notifications.models import Notification, NotificationType

from . import messages as leave_messages
from .models import LeaveRequest, LeaveRequestStatus
from .services import (
    get_department_leave_reminder_recipients,
    get_leave_approval_stakeholders,
    get_leave_settings,
    leave_year_boundary_month_day,
    leave_year_for_date,
    reminder_lead_days,
)

User = get_user_model()
logger = logging.getLogger(__name__)


def _employee_name(leave_request: LeaveRequest) -> str:
    return leave_messages.user_display_name(leave_request.employee)


def _leave_request_action_url(leave_request: LeaveRequest) -> str:
    base = (getattr(settings, "FRONTEND_BASE_URL", "") or "").rstrip("/")
    if not base:
        return ""
    return f"{base}/leave/requests/{leave_request.id}"


def _email_template_context(leave_request: LeaveRequest, **extra) -> dict:
    ctx = {
        "employee_name": _employee_name(leave_request),
        "leave_type": leave_messages.leave_type_label(leave_request.leave_type),
        "start_date": leave_request.start_date,
        "end_date": leave_request.end_date,
        "total_days": leave_messages.format_working_days(leave_request.total_working_days),
        "status": leave_request.status,
        "reason": leave_request.reason or "",
        "action_url": _leave_request_action_url(leave_request),
    }
    ctx.update(extra)
    return ctx


def _send_email_if_possible(
    *,
    subject: str,
    text_body: str,
    recipients: list[str],
    html_template: str | None = None,
    text_template: str | None = None,
    template_context: dict | None = None,
) -> bool:
    recipients = [email for email in recipients if email]
    if not recipients:
        logger.info("Email skipped (no recipients). subject=%r", subject)
        return False

    html_body = None
    rendered_text = text_body
    ctx = template_context or {}
    ctx.setdefault("subject", subject)
    if html_template:
        html_body = render_to_string(html_template, ctx)

    if text_template:
        rendered_text = render_to_string(text_template, ctx)
    elif html_body and not rendered_text:
        rendered_text = strip_tags(html_body)

    msg = EmailMultiAlternatives(
        subject=subject,
        body=rendered_text or "",
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
        to=recipients,
    )
    if html_body:
        msg.attach_alternative(html_body, "text/html")
    try:
        fail_silently = not bool(getattr(settings, "DEBUG", False))
        sent_count = msg.send(fail_silently=fail_silently)
        logger.info(
            "Email send attempted. subject=%r recipients=%s sent_count=%s backend=%s fail_silently=%s",
            subject,
            recipients,
            sent_count,
            getattr(settings, "EMAIL_BACKEND", None),
            fail_silently,
        )
        return bool(sent_count)
    except Exception:
        logger.exception(
            "Email send failed. subject=%r recipients=%s backend=%s",
            subject,
            recipients,
            getattr(settings, "EMAIL_BACKEND", None),
        )
        return False


def _publish_notifications(*, redis_url: str, user_ids: list[str], payload: dict) -> None:
    if not user_ids:
        return
    client = redis.from_url(redis_url, decode_responses=True)
    data = json.dumps(payload)
    for user_id in user_ids:
        client.publish(f"notifications:user:{user_id}", data)


@shared_task
def notify_leave_submitted(leave_request_id: str) -> bool:
    if not get_leave_settings().notify_applicant_on_submit:
        return False
    try:
        leave_request = (
            LeaveRequest.objects.select_related("employee", "leave_type", "employee__department")
            .get(pk=leave_request_id)
        )
    except LeaveRequest.DoesNotExist:
        return False

    manager = leave_request.employee.get_department_line_manager()
    if not manager:
        return False

    employee_name = _employee_name(leave_request)
    action_url = _leave_request_action_url(leave_request)
    subject = leave_messages.email_action_required_subject(employee_name)
    body = leave_messages.email_action_required_body(leave_request, action_url=action_url)

    notification = Notification.objects.create(
        recipient=manager,
        title=subject,
        body=body,
        type=NotificationType.LEAVE_SUBMITTED,
        data={
            "leave_request_id": str(leave_request.id),
            "status": leave_request.status,
        },
    )
    _publish_notifications(
        redis_url=settings.NOTIFICATIONS_REDIS_URL,
        user_ids=[str(manager.id)],
        payload={
            "notification_id": str(notification.id),
            "type": notification.type,
            "title": notification.title,
            "body": notification.body,
            "data": notification.data,
            "created_at": notification.created_at.isoformat(),
        },
    )

    return _send_email_if_possible(
        subject=subject,
        text_body=body,
        recipients=[manager.email],
        html_template="email/leave_action_required.html",
        template_context=_email_template_context(leave_request),
    )


@shared_task
def notify_leave_decision(leave_request_id: str, decision: str, comment: str = "") -> bool:
    if not get_leave_settings().notify_applicant_on_decision:
        return False
    try:
        leave_request = (
            LeaveRequest.objects.select_related("employee", "leave_type", "cover_person")
            .get(pk=leave_request_id)
        )
    except LeaveRequest.DoesNotExist:
        return False

    employee_name = _employee_name(leave_request)
    approved = decision == LeaveRequestStatus.APPROVED
    action_url = _leave_request_action_url(leave_request)
    subject = leave_messages.email_decision_subject(employee_name, approved=approved)
    body = leave_messages.email_decision_body(
        leave_request,
        approved=approved,
        comment=comment,
        action_url=action_url,
    )
    ntype = (
        NotificationType.LEAVE_APPROVED
        if approved
        else NotificationType.LEAVE_REJECTED
    )

    notification = Notification.objects.create(
        recipient=leave_request.employee,
        title=subject,
        body=body,
        type=ntype,
        data={
            "leave_request_id": str(leave_request.id),
            "status": leave_request.status,
        },
    )
    _publish_notifications(
        redis_url=settings.NOTIFICATIONS_REDIS_URL,
        user_ids=[str(leave_request.employee.id)],
        payload={
            "notification_id": str(notification.id),
            "type": notification.type,
            "title": notification.title,
            "body": notification.body,
            "data": notification.data,
            "created_at": notification.created_at.isoformat(),
        },
    )
    return _send_email_if_possible(
        subject=subject,
        text_body=body,
        recipients=[leave_request.employee.email],
        html_template="email/leave_decision.html",
        template_context=_email_template_context(
            leave_request,
            decision_message=body.split("\n\n")[0],
            comment=comment or "",
        ),
    )


@shared_task
def notify_approver_required(leave_request_id: str) -> bool:
    if not get_leave_settings().notify_approver:
        return False
    try:
        leave_request = (
            LeaveRequest.objects.select_related(
                "employee",
                "leave_type",
                "employee__department",
                "employee__unit",
                "employee__team",
            ).get(pk=leave_request_id)
        )
    except LeaveRequest.DoesNotExist:
        return False

    recipient_users: list[User] = []
    from .workflow import (
        current_snapshot_stage,
        expand_with_delegates,
        resolve_stage_approvers,
    )

    stage = current_snapshot_stage(leave_request)
    if stage:
        recipient_users = resolve_stage_approvers(stage, leave_request.employee)
    elif leave_request.status == LeaveRequestStatus.PENDING_TEAM_LEAD:
        team = getattr(leave_request.employee, "team", None)
        team_lead = getattr(team, "team_lead", None) if team else None
        if team_lead:
            recipient_users = [team_lead]
    elif leave_request.status == LeaveRequestStatus.PENDING_SUPERVISOR:
        supervisor = getattr(leave_request.employee.unit, "supervisor", None)
        if supervisor:
            recipient_users = [supervisor]
    elif leave_request.status == LeaveRequestStatus.PENDING_MANAGER:
        if getattr(leave_request, "manager_approver_is_management", False):
            mgmt = get_or_create_management_department()
            if mgmt.line_manager:
                recipient_users = [mgmt.line_manager]
        else:
            manager = leave_request.employee.get_department_line_manager()
            if manager:
                recipient_users = [manager]
    elif leave_request.status == LeaveRequestStatus.PENDING_HR:
        recipient_users = list(
            User.objects.filter(is_active=True, user_roles__role__name=RoleName.HR)
        )
    elif leave_request.status == LeaveRequestStatus.PENDING_ED:
        recipient_users = list(
            User.objects.filter(is_active=True, user_roles__role__name=RoleName.EXECUTIVE_DIRECTOR)
        )
    else:
        return False

    recipient_users = expand_with_delegates(recipient_users)
    if not recipient_users:
        return False

    employee_name = _employee_name(leave_request)
    action_url = _leave_request_action_url(leave_request)
    subject = leave_messages.email_action_required_subject(employee_name)
    body = leave_messages.email_action_required_body(leave_request, action_url=action_url)
    recipients = [u.email for u in recipient_users if getattr(u, "email", None)]
    user_ids: list[str] = []
    for user in recipient_users:
        notification = Notification.objects.create(
            recipient=user,
            title=subject,
            body=body,
            type=NotificationType.LEAVE_ACTION_REQUIRED,
            data={
                "leave_request_id": str(leave_request.id),
                "status": leave_request.status,
            },
        )
        user_ids.append(str(user.id))
        _publish_notifications(
            redis_url=settings.NOTIFICATIONS_REDIS_URL,
            user_ids=[str(user.id)],
            payload={
                "notification_id": str(notification.id),
                "type": notification.type,
                "title": notification.title,
                "body": notification.body,
                "data": notification.data,
                "created_at": notification.created_at.isoformat(),
            },
        )
    return _send_email_if_possible(
        subject=subject,
        text_body=body,
        recipients=recipients,
        html_template="email/leave_action_required.html",
        template_context=_email_template_context(leave_request),
    )


@shared_task
def notify_reliever_assigned(leave_request_id: str) -> bool:
    """Notify the cover person when a leave request is finally approved."""
    if not get_leave_settings().notify_reliever:
        return False
    try:
        leave_request = (
            LeaveRequest.objects.select_related(
                "employee",
                "leave_type",
                "cover_person",
            ).get(pk=leave_request_id)
        )
    except LeaveRequest.DoesNotExist:
        return False

    cover_person = leave_request.cover_person
    if not cover_person or not cover_person.is_active:
        return False

    employee_name = _employee_name(leave_request)
    action_url = _leave_request_action_url(leave_request)
    subject = leave_messages.email_reliever_subject(employee_name)
    body = leave_messages.email_reliever_body(leave_request, action_url=action_url)

    notification = Notification.objects.create(
        recipient=cover_person,
        title=subject,
        body=body,
        type=NotificationType.LEAVE_RELIEVER_ASSIGNED,
        data={
            "leave_request_id": str(leave_request.id),
            "status": leave_request.status,
        },
    )
    _publish_notifications(
        redis_url=settings.NOTIFICATIONS_REDIS_URL,
        user_ids=[str(cover_person.id)],
        payload={
            "notification_id": str(notification.id),
            "type": notification.type,
            "title": notification.title,
            "body": notification.body,
            "data": notification.data,
            "created_at": notification.created_at.isoformat(),
        },
    )

    return _send_email_if_possible(
        subject=subject,
        text_body=body,
        recipients=[cover_person.email],
        html_template="email/leave_reliever.html",
        template_context=_email_template_context(
            leave_request,
            reliever_name=leave_messages.user_display_name(cover_person),
        ),
    )


@shared_task
def notify_department_leave_reminder(leave_request_id: str) -> bool:
    """
    Notify department colleagues, line manager, HR, and ED that an approved
    leave starts within the configured reminder window (default ~24 hours).
    """
    if not get_leave_settings().notify_department_reminder:
        return False
    try:
        leave_request = (
            LeaveRequest.objects.select_related(
                "employee",
                "leave_type",
                "employee__department",
            ).get(pk=leave_request_id)
        )
    except LeaveRequest.DoesNotExist:
        return False

    if leave_request.status != LeaveRequestStatus.APPROVED:
        return False
    if leave_request.department_reminder_sent_at is not None:
        return False

    recipient_users = get_department_leave_reminder_recipients(leave_request.employee)
    employee_name = _employee_name(leave_request)
    department_name = (
        leave_request.employee.department.name
        if leave_request.employee.department_id
        else "Unassigned"
    )
    action_url = _leave_request_action_url(leave_request)
    subject = leave_messages.email_department_reminder_subject(
        employee_name,
        leave_request.start_date,
    )
    body = leave_messages.email_department_reminder_body(
        leave_request,
        department_name=department_name,
        action_url=action_url,
    )

    for user in recipient_users:
        notification = Notification.objects.create(
            recipient=user,
            title=subject,
            body=body,
            type=NotificationType.LEAVE_DEPARTMENT_REMINDER,
            data={
                "leave_request_id": str(leave_request.id),
                "status": leave_request.status,
            },
        )
        _publish_notifications(
            redis_url=settings.NOTIFICATIONS_REDIS_URL,
            user_ids=[str(user.id)],
            payload={
                "notification_id": str(notification.id),
                "type": notification.type,
                "title": notification.title,
                "body": notification.body,
                "data": notification.data,
                "created_at": notification.created_at.isoformat(),
            },
        )

    recipients = [u.email for u in recipient_users if getattr(u, "email", None)]
    sent = _send_email_if_possible(
        subject=subject,
        text_body=body,
        recipients=recipients,
        html_template="email/leave_department_reminder.html",
        template_context=_email_template_context(
            leave_request,
            department_name=department_name,
        ),
    )

    LeaveRequest.objects.filter(pk=leave_request.pk).update(
        department_reminder_sent_at=timezone.now()
    )
    return sent


@shared_task
def notify_leave_reconciled(
    leave_request_id: str,
    notify_department_colleagues: bool = False,
) -> bool:
    """
    Notify approval-chain stakeholders that HR recorded backdated leave.
    Informational only — no approval action is required.
    """
    try:
        leave_request = (
            LeaveRequest.objects.select_related(
                "employee",
                "leave_type",
                "cover_person",
                "reconciled_by",
            ).get(pk=leave_request_id)
        )
    except LeaveRequest.DoesNotExist:
        return False

    if not leave_request.is_reconciled:
        return False

    users_by_id = {user.pk: user for user in get_leave_approval_stakeholders(leave_request.employee)}
    if leave_request.cover_person_id and leave_request.cover_person.is_active:
        users_by_id[leave_request.cover_person_id] = leave_request.cover_person

    if notify_department_colleagues:
        for user in get_department_leave_reminder_recipients(leave_request.employee):
            users_by_id[user.pk] = user

    employee_name = _employee_name(leave_request)
    reconciled_by_name = (
        leave_messages.user_display_name(leave_request.reconciled_by)
        if leave_request.reconciled_by_id
        else "HR"
    )
    action_url = _leave_request_action_url(leave_request)
    subject = leave_messages.email_reconciled_subject(employee_name)
    body = leave_messages.email_reconciled_body(
        leave_request,
        reconciled_by_name=reconciled_by_name,
        action_url=action_url,
    )

    recipients = [u.email for u in users_by_id.values() if getattr(u, "email", None)]

    for user in users_by_id.values():
        notification = Notification.objects.create(
            recipient=user,
            title=subject,
            body=body,
            type=NotificationType.LEAVE_RECONCILED,
            data={
                "leave_request_id": str(leave_request.id),
                "status": leave_request.status,
                "is_reconciled": True,
            },
        )
        _publish_notifications(
            redis_url=settings.NOTIFICATIONS_REDIS_URL,
            user_ids=[str(user.id)],
            payload={
                "notification_id": str(notification.id),
                "type": notification.type,
                "title": notification.title,
                "body": notification.body,
                "data": notification.data,
                "created_at": notification.created_at.isoformat(),
            },
        )

    return _send_email_if_possible(
        subject=subject,
        text_body=body,
        recipients=recipients,
        html_template="email/leave_reconciled.html",
        template_context=_email_template_context(
            leave_request,
            reconciled_by_name=reconciled_by_name,
            reconciliation_note=leave_request.reconciliation_note or "",
        ),
    )


@shared_task
def notify_upcoming_approved_leaves() -> int:
    """
    Celery Beat entry: send department reminders for approved leaves starting
    tomorrow (≈24 hours before the start day).
    """
    today = timezone.localdate()
    lead = reminder_lead_days()
    target_start = today + datetime.timedelta(days=lead)
    qs = LeaveRequest.objects.filter(
        status=LeaveRequestStatus.APPROVED,
        start_date=target_start,
        department_reminder_sent_at__isnull=True,
    ).only("id")

    sent_count = 0
    for leave_request_id in qs.values_list("id", flat=True):
        if notify_department_leave_reminder(str(leave_request_id)):
            sent_count += 1
    return sent_count


@shared_task
def run_leave_year_rollover(year: int | None = None, dry_run: bool = False) -> dict:
    """Create next-year balances, accrue Jan/upfront, carry-forward or expire unused.

    When *year* is omitted (Beat), the job only runs on the configured leave-year
    start date (1 Jan for calendar/anniversary; fiscal month/day otherwise).
    Idempotency keys still use LeaveBalance.year integers and are unchanged.
    """
    from django.utils import timezone

    from .services import preview_or_run_accrual

    as_of = timezone.localdate()
    if year is None:
        month, day = leave_year_boundary_month_day()
        if (as_of.month, as_of.day) != (month, day):
            return {
                "dry_run": dry_run,
                "skipped": True,
                "reason": "not_leave_year_start",
                "as_of": as_of.isoformat(),
                "leave_year_start": f"{month:02d}-{day:02d}",
                "action_count": 0,
                "actions": [],
            }
        year = leave_year_for_date(as_of)
    return preview_or_run_accrual(
        as_of=as_of,
        year=year,
        include_rollover=True,
        include_monthly=False,
        include_weekly=False,
        include_anniversary=False,
        include_carry_expiry=False,
        dry_run=dry_run,
    )


@shared_task
def run_leave_monthly_accrual(year: int | None = None, month: int | None = None, dry_run: bool = False) -> dict:
    from django.utils import timezone

    from .services import preview_or_run_accrual

    as_of = timezone.localdate()
    return preview_or_run_accrual(
        as_of=as_of,
        year=year or as_of.year,
        month=month or as_of.month,
        include_rollover=False,
        include_monthly=True,
        include_weekly=False,
        include_anniversary=False,
        include_carry_expiry=False,
        dry_run=dry_run,
    )


@shared_task
def run_leave_weekly_accrual(dry_run: bool = False) -> dict:
    from django.utils import timezone

    from .services import preview_or_run_accrual

    as_of = timezone.localdate()
    return preview_or_run_accrual(
        as_of=as_of,
        year=as_of.year,
        include_rollover=False,
        include_monthly=False,
        include_weekly=True,
        include_anniversary=False,
        include_carry_expiry=False,
        dry_run=dry_run,
    )


@shared_task
def run_leave_anniversary_accrual(dry_run: bool = False) -> dict:
    from django.utils import timezone

    from .services import preview_or_run_accrual

    as_of = timezone.localdate()
    return preview_or_run_accrual(
        as_of=as_of,
        year=as_of.year,
        include_rollover=False,
        include_monthly=False,
        include_weekly=False,
        include_anniversary=True,
        include_carry_expiry=False,
        dry_run=dry_run,
    )


@shared_task
def run_leave_carry_forward_expiry(dry_run: bool = False) -> dict:
    from django.utils import timezone

    from .services import preview_or_run_accrual

    as_of = timezone.localdate()
    return preview_or_run_accrual(
        as_of=as_of,
        year=as_of.year,
        include_rollover=False,
        include_monthly=False,
        include_weekly=False,
        include_anniversary=False,
        include_carry_expiry=True,
        dry_run=dry_run,
    )


@shared_task
def escalate_stale_leave_approvals() -> dict:
    """Remind current approvers and notify the next stage when pending longer than sla_hours.

    Auto-approve after SLA stays off unless the request snapshot has auto_approve_after_sla.
    Skips sending when LeaveSettings.notify_approver is false.
    """
    from .services import PENDING_HOLD_STATUSES
    from .workflow import (
        current_snapshot_stage,
        expand_with_delegates,
        resolve_stage_approvers,
        sla_hours_for_request,
        stage_is_overdue,
    )

    settings_row = get_leave_settings()
    reminded = 0
    escalated = 0
    skipped_notify = 0
    qs = LeaveRequest.objects.filter(status__in=PENDING_HOLD_STATUSES).select_related(
        "employee",
        "leave_type",
        "employee__department",
        "employee__unit",
        "employee__team",
    )
    for leave_request in qs:
        if not sla_hours_for_request(leave_request, leave_settings=settings_row):
            continue
        if not stage_is_overdue(leave_request, leave_settings=settings_row):
            continue
        if leave_request.sla_notified_at:
            continue
        if not settings_row.notify_approver:
            skipped_notify += 1
            leave_request.sla_notified_at = timezone.now()
            leave_request.save(update_fields=["sla_notified_at", "updated_at"])
            continue

        notify_approver_required(str(leave_request.id))
        reminded += 1

        snapshot = leave_request.workflow_snapshot or {}
        stages = snapshot.get("stages") or []
        current = current_snapshot_stage(leave_request)
        next_stage = None
        if current:
            found = False
            for stage in stages:
                if found:
                    next_stage = stage
                    break
                if stage.get("order") == current.get("order"):
                    found = True
        if next_stage:
            next_users = expand_with_delegates(
                resolve_stage_approvers(next_stage, leave_request.employee)
            )
            employee_name = _employee_name(leave_request)
            subject = leave_messages.email_sla_escalation_subject(employee_name)
            body = leave_messages.email_sla_escalation_body(leave_request)
            for user in next_users:
                Notification.objects.create(
                    recipient=user,
                    title=subject,
                    body=body,
                    type=NotificationType.LEAVE_ACTION_REQUIRED,
                    data={
                        "leave_request_id": str(leave_request.id),
                        "status": leave_request.status,
                        "escalation": True,
                    },
                )
            if next_users:
                escalated += 1

        leave_request.sla_notified_at = timezone.now()
        leave_request.save(update_fields=["sla_notified_at", "updated_at"])

        snapshot_auto = bool((leave_request.workflow_snapshot or {}).get("auto_approve_after_sla"))
        if snapshot_auto:
            logger.info(
                "auto_approve_after_sla is enabled for request %s but auto-approve is not applied "
                "by this job (governance default remains off unless product enables a dedicated path).",
                leave_request.id,
            )

    return {"reminded": reminded, "escalated": escalated, "skipped_notify": skipped_notify}
