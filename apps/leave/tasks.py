import json

from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags

import redis

from apps.accounts.models import RoleName, get_or_create_management_department
from apps.notifications.models import Notification, NotificationType

from .models import LeaveRequest, LeaveRequestStatus

User = get_user_model()


def _employee_name(leave_request: LeaveRequest) -> str:
    return leave_request.employee.get_full_name() or leave_request.employee.email


def _leave_request_action_url(leave_request: LeaveRequest) -> str:
    base = (getattr(settings, "FRONTEND_BASE_URL", "") or "").rstrip("/")
    if not base:
        return ""
    return f"{base}/leave/requests/{leave_request.id}"


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
    msg.send(fail_silently=True)
    return True


def _publish_notifications(*, redis_url: str, user_ids: list[str], payload: dict) -> None:
    if not user_ids:
        return
    client = redis.from_url(redis_url, decode_responses=True)
    data = json.dumps(payload)
    for user_id in user_ids:
        client.publish(f"notifications:user:{user_id}", data)


@shared_task
def notify_leave_submitted(leave_request_id: str) -> bool:
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
    subject = f"Leave Request Awaiting Your Approval — {employee_name}"
    body = (
        f"Employee: {employee_name}\n"
        f"Leave Type: {leave_request.leave_type.name}\n"
        f"Dates: {leave_request.start_date} to {leave_request.end_date}\n"
        f"Total Days: {leave_request.total_working_days}\n"
        f"Reason: {leave_request.reason or 'N/A'}\n"
    )

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
        text_template="email/leave_action_required.txt",
        template_context={
            "employee_name": employee_name,
            "leave_type": leave_request.leave_type.name,
            "start_date": leave_request.start_date,
            "end_date": leave_request.end_date,
            "total_days": leave_request.total_working_days,
            "status": leave_request.status,
            "reason": leave_request.reason or "",
            "action_url": _leave_request_action_url(leave_request),
        },
    )


@shared_task
def notify_leave_decision(leave_request_id: str, decision: str, comment: str = "") -> bool:
    try:
        leave_request = (
            LeaveRequest.objects.select_related("employee", "leave_type")
            .get(pk=leave_request_id)
        )
    except LeaveRequest.DoesNotExist:
        return False

    employee_name = _employee_name(leave_request)
    if decision == LeaveRequestStatus.APPROVED:
        decision_message = "Your leave request has been approved."
        ntype = NotificationType.LEAVE_APPROVED
    else:
        decision_message = f"Your leave request was rejected. Reason: {comment or 'No reason provided.'}"
        ntype = NotificationType.LEAVE_REJECTED

    subject = f"Leave Request Decision — {employee_name}"
    notification = Notification.objects.create(
        recipient=leave_request.employee,
        title=subject,
        body=decision_message,
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
        text_body=decision_message,
        recipients=[leave_request.employee.email],
        html_template="email/leave_decision.html",
        text_template="email/leave_decision.txt",
        template_context={
            "employee_name": employee_name,
            "leave_type": leave_request.leave_type.name if leave_request.leave_type_id else "",
            "start_date": leave_request.start_date,
            "end_date": leave_request.end_date,
            "total_days": leave_request.total_working_days,
            "status": leave_request.status,
            "decision_message": decision_message,
            "comment": comment or "",
            "action_url": _leave_request_action_url(leave_request),
        },
    )


@shared_task
def notify_approver_required(leave_request_id: str) -> bool:
    try:
        leave_request = (
            LeaveRequest.objects.select_related("employee", "leave_type", "employee__department", "employee__unit", "employee__team")
            .get(pk=leave_request_id)
        )
    except LeaveRequest.DoesNotExist:
        return False

    recipient_users: list[User] = []
    if leave_request.status == LeaveRequestStatus.PENDING_TEAM_LEAD:
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

    employee_name = _employee_name(leave_request)
    subject = f"Leave Request Awaiting Your Approval — {employee_name}"
    body = "A leave request is waiting for your approval."
    recipients = [u.email for u in recipient_users if getattr(u, "email", None)]
    user_ids: list[str] = []
    for user in recipient_users:
        notification = Notification.objects.create(
            recipient=user,
            title=subject,
            body=(
                f"{body}\n\n"
                f"Employee: {employee_name}\n"
                f"Leave Type: {leave_request.leave_type.name}\n"
                f"Dates: {leave_request.start_date} to {leave_request.end_date}\n"
                f"Total Days: {leave_request.total_working_days}\n"
                f"Current Status: {leave_request.status}\n"
            ),
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
        text_body=(
            f"{body}\n\n"
            f"Employee: {employee_name}\n"
            f"Leave Type: {leave_request.leave_type.name}\n"
            f"Dates: {leave_request.start_date} to {leave_request.end_date}\n"
            f"Total Days: {leave_request.total_working_days}\n"
            f"Current Status: {leave_request.status}\n"
        ),
        recipients=recipients,
        html_template="email/leave_action_required.html",
        text_template="email/leave_action_required.txt",
        template_context={
            "employee_name": employee_name,
            "leave_type": leave_request.leave_type.name,
            "start_date": leave_request.start_date,
            "end_date": leave_request.end_date,
            "total_days": leave_request.total_working_days,
            "status": leave_request.status,
            "reason": leave_request.reason or "",
            "action_url": _leave_request_action_url(leave_request),
        },
    )
