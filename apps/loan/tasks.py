"""
Celery tasks for loan application notifications (email + optional in-app).

Uses @shared_task so tasks bind to the Celery app in ``hrm_backend.celery`` via
``app.autodiscover_tasks()``. Always enqueue with ``.delay()`` from views — never
call these synchronously from request handlers.
"""

import json
import logging
from decimal import Decimal, ROUND_CEILING

from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.utils import timezone as django_timezone

from apps.accounts.models import RoleName, get_or_create_management_department
from apps.notifications.models import Notification, NotificationType

from .models import (
    LoanApplication,
    LoanApplicationStatus,
    LoanApprovalAction,
    LoanApprovalLog,
)
from .services import LoanEligibilityService, users_in_observer_scope

User = get_user_model()
logger = logging.getLogger(__name__)


def _employee_name(loan: LoanApplication) -> str:
    return loan.employee.get_full_name() or loan.employee.email


def _loan_action_url(loan: LoanApplication) -> str:
    base = (getattr(settings, "FRONTEND_BASE_URL", "") or "").rstrip("/")
    if not base:
        return ""
    return f"{base}/loans/requests/{loan.id}"


def _users_with_role(role_name: str):
    return (
        User.objects.filter(is_active=True, user_roles__role__name=role_name)
        .distinct()
        .order_by("email")
    )


def _send_email_if_possible(
    *,
    subject: str,
    text_body: str,
    recipients: list[str],
    html_template: str | None = None,
    text_template: str | None = None,
    template_context: dict | None = None,
) -> bool:
    """Same pattern as ``apps.leave.tasks`` — console backend in dev, SMTP in prod."""
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
            "Email send attempted. subject=%r recipients=%s sent_count=%s backend=%s",
            subject,
            recipients,
            sent_count,
            getattr(settings, "EMAIL_BACKEND", None),
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
    import redis

    client = redis.from_url(redis_url, decode_responses=True)
    data = json.dumps(payload)
    for user_id in user_ids:
        client.publish(f"notifications:user:{user_id}", data)


def _notify_users_in_app(*, users: list, title: str, body: str, ntype: str, data: dict) -> None:
    redis_url = settings.NOTIFICATIONS_REDIS_URL
    for user in users:
        if not user:
            continue
        notification = Notification.objects.create(
            recipient=user,
            title=title,
            body=body,
            type=ntype,
            data=data,
        )
        _publish_notifications(
            redis_url=redis_url,
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


def _submitted_timestamp(loan: LoanApplication) -> str:
    log = (
        LoanApprovalLog.objects.filter(
            loan=loan,
            action=LoanApprovalAction.SUBMIT,
            previous_status=LoanApplicationStatus.DRAFT,
        )
        .order_by("-timestamp")
        .first()
    )
    ts = log.timestamp if log else loan.updated_at
    return django_timezone.localtime(ts).strftime("%Y-%m-%d %H:%M %Z")


def _approval_history_summary(loan: LoanApplication) -> str:
    lines: list[str] = []
    for log in (
        LoanApprovalLog.objects.filter(loan=loan)
        .select_related("actor")
        .order_by("timestamp")
    ):
        actor_label = (
            (log.actor.get_full_name() or log.actor.email) if log.actor else "System"
        )
        prev = log.previous_status or "(start)"
        extra = f" — {log.comment}" if log.comment else ""
        lines.append(
            f"- {django_timezone.localtime(log.timestamp):%Y-%m-%d %H:%M} — "
            f"{log.get_action_display()}: {prev} → {log.new_status} ({actor_label}){extra}"
        )
    return "\n".join(lines) if lines else "(No approval history yet.)"


def _monthly_installment_line(loan: LoanApplication) -> str:
    if loan.monthly_installment is not None:
        return f"Monthly installment: {loan.monthly_installment}\n"
    approx = (Decimal(loan.amount) / Decimal(loan.tenure_months)).quantize(
        Decimal("0.01"), rounding=ROUND_CEILING
    )
    return (
        f"Monthly installment: {approx} (principal ÷ tenure; final figure set at disbursement)\n"
    )


@shared_task
def notify_loan_approver_required(loan_id: str) -> bool:
    """Notify the approver for the loan's current pending status."""
    try:
        loan = (
            LoanApplication.objects.select_related("employee", "employee__department", "loan_type")
            .get(pk=loan_id)
        )
    except LoanApplication.DoesNotExist:
        logger.warning(
            "notify_loan_approver_required: LoanApplication missing. loan_id=%s", loan_id
        )
        return False

    recipient_users: list = []
    if loan.status == LoanApplicationStatus.PENDING_MANAGER:
        if loan.manager_approver_is_management:
            mgmt = get_or_create_management_department()
            if mgmt.line_manager:
                recipient_users = [mgmt.line_manager]
        else:
            manager = loan.employee.get_department_line_manager()
            if manager:
                recipient_users = [manager]
        ntype = NotificationType.LOAN_ACTION_REQUIRED
        subject_prefix = "Loan Application Awaiting Your Approval"
    elif loan.status == LoanApplicationStatus.PENDING_HR:
        recipient_users = list(_users_with_role(RoleName.HR))
        ntype = NotificationType.LOAN_SUBMITTED
        subject_prefix = "New Loan Application"
    else:
        logger.info(
            "notify_loan_approver_required: skip (status not awaiting LM/HR). loan_id=%s status=%s",
            loan_id,
            loan.status,
        )
        return False

    if not recipient_users:
        logger.info(
            "notify_loan_approver_required: no recipients. loan_id=%s status=%s",
            loan_id,
            loan.status,
        )
        return False

    employee_name = _employee_name(loan)
    subject = f"{subject_prefix} — {employee_name}"
    body = (
        f"Employee: {employee_name}\n"
        f"Loan type: {loan.loan_type.name}\n"
        f"Amount: {loan.amount}\n"
        f"Tenure: {loan.tenure_months} month(s)\n"
        f"Purpose: {loan.purpose}\n"
        f"Current status: {loan.status}\n"
    )
    if loan.status == LoanApplicationStatus.PENDING_HR:
        body = (
            f"{body}"
            f"Date submitted: {_submitted_timestamp(loan)}\n"
        )

    emails = [u.email for u in recipient_users if u.email]
    _send_email_if_possible(subject=subject, text_body=body, recipients=emails)

    data = {
        "loan_id": str(loan.id),
        "status": loan.status,
        "action_url": _loan_action_url(loan),
    }
    _notify_users_in_app(
        users=recipient_users,
        title=subject,
        body=body,
        ntype=ntype,
        data=data,
    )
    return True


@shared_task
def notify_loan_observers(loan_id: str) -> bool:
    """FYI notification to configured observer department/unit members."""
    try:
        loan = LoanApplication.objects.select_related("employee", "loan_type").get(pk=loan_id)
    except LoanApplication.DoesNotExist:
        logger.warning("notify_loan_observers: LoanApplication missing. loan_id=%s", loan_id)
        return False

    observers = list(users_in_observer_scope())
    if not observers:
        logger.info("notify_loan_observers: no observer scope configured. loan_id=%s", loan_id)
        return False

    employee_name = _employee_name(loan)
    subject = f"Loan Application Update (FYI) — {employee_name}"
    body = (
        f"This loan application is for your information only — no action is required.\n\n"
        f"Employee: {employee_name}\n"
        f"Loan type: {loan.loan_type.name}\n"
        f"Amount: {loan.amount}\n"
        f"Tenure: {loan.tenure_months} month(s)\n"
        f"Current status: {loan.status}\n"
    )
    emails = [u.email for u in observers if u.email]
    _send_email_if_possible(subject=subject, text_body=body, recipients=emails)

    data = {
        "loan_id": str(loan.id),
        "status": loan.status,
        "action_url": _loan_action_url(loan),
    }
    _notify_users_in_app(
        users=observers,
        title=subject,
        body=body,
        ntype=NotificationType.LOAN_OBSERVER_NOTICE,
        data=data,
    )
    return True


@shared_task
def notify_loan_submitted(loan_id: str) -> bool:
    """Backward-compatible alias — delegates to approver-required for PENDING_HR."""
    return notify_loan_approver_required(loan_id)


@shared_task
def notify_loan_decision(loan_id: str, decision: str, comment: str = "") -> bool:
    """Email the employee when a loan is fully approved or rejected."""
    try:
        loan = LoanApplication.objects.select_related("employee", "loan_type").get(pk=loan_id)
    except LoanApplication.DoesNotExist:
        logger.warning("notify_loan_decision: LoanApplication missing. loan_id=%s", loan_id)
        return False

    employee = loan.employee
    if not employee.email:
        logger.info("notify_loan_decision: employee has no email. loan_id=%s", loan_id)
        return False

    if decision == LoanApplicationStatus.APPROVED:
        subject = "Your Loan Application Has Been Approved"
        body = (
            f"Dear {_employee_name(loan)},\n\n"
            f"Your loan application has been approved.\n\n"
            f"Amount: {loan.amount}\n"
            f"Tenure: {loan.tenure_months} month(s)\n"
            f"{_monthly_installment_line(loan)}"
            "\nNext steps:\n"
            "- HR will disburse the approved amount according to company policy.\n"
            "- You will receive a further update when the loan is activated and repayment "
            "schedule is available.\n"
        )
        ntype = NotificationType.LOAN_APPROVED
    elif decision == LoanApplicationStatus.REJECTED:
        subject = "Your Loan Application Was Not Approved"
        body = (
            f"Dear {_employee_name(loan)},\n\n"
            f"We regret to inform you that your loan application was not approved.\n\n"
            f"Loan type: {loan.loan_type.name}\n"
            f"Amount: {loan.amount}\n"
            f"Rejection comment: {comment or 'No comment provided.'}\n"
        )
        ntype = NotificationType.LOAN_REJECTED
    else:
        logger.warning(
            "notify_loan_decision: unsupported decision=%r loan_id=%s", decision, loan_id
        )
        return False

    _send_email_if_possible(subject=subject, text_body=body, recipients=[employee.email])

    _notify_users_in_app(
        users=[employee],
        title=subject,
        body=body,
        ntype=ntype,
        data={
            "loan_id": str(loan.id),
            "status": loan.status,
            "decision": decision,
            "action_url": _loan_action_url(loan),
        },
    )
    return True


@shared_task
def notify_next_approver(loan_id: str) -> bool:
    """
    After an intermediate approval (HR or ED), email the next approval tier.

    Current loan status must be PENDING_ED (notify Executive Directors) or
    PENDING_MD (notify Managing Directors).
    """
    try:
        loan = LoanApplication.objects.select_related("employee", "loan_type").get(pk=loan_id)
    except LoanApplication.DoesNotExist:
        logger.warning("notify_next_approver: LoanApplication missing. loan_id=%s", loan_id)
        return False

    if loan.status == LoanApplicationStatus.PENDING_ED:
        role = RoleName.EXECUTIVE_DIRECTOR
    elif loan.status == LoanApplicationStatus.PENDING_MD:
        role = RoleName.MANAGING_DIRECTOR
    else:
        logger.info(
            "notify_next_approver: skip (status not awaiting ED/MD). loan_id=%s status=%s",
            loan_id,
            loan.status,
        )
        return False

    recipients = list(_users_with_role(role))
    emails = [u.email for u in recipients if u.email]
    employee_name = _employee_name(loan)
    subject = f"Loan Application Awaiting Your Approval — {employee_name}"
    body = (
        f"A loan application is awaiting your approval.\n\n"
        f"Employee: {employee_name}\n"
        f"Loan type: {loan.loan_type.name}\n"
        f"Amount: {loan.amount}\n"
        f"Tenure: {loan.tenure_months} month(s)\n\n"
        f"Approval history:\n{_approval_history_summary(loan)}\n"
    )
    if not emails:
        logger.info(
            "notify_next_approver: no recipients for role=%s loan_id=%s", role, loan_id
        )
        return False

    _send_email_if_possible(subject=subject, text_body=body, recipients=emails)
    return True


def _schedule_summary_lines(loan: LoanApplication, limit: int = 6) -> str:
    rows = list(
        loan.repayment_schedule.order_by("installment_number").values(
            "installment_number", "due_date", "amount_due", "payment_status"
        )
    )
    if not rows:
        return "(Repayment schedule not available yet.)\n"
    lines: list[str] = []
    for row in rows[:limit]:
        lines.append(
            f"- #{row['installment_number']}: due {row['due_date']}, "
            f"{row['amount_due']} ({row['payment_status']})"
        )
    if len(rows) > limit:
        lines.append(f"- … and {len(rows) - limit} more installment(s)")
    return "\n".join(lines) + "\n"


@shared_task
def notify_loan_disbursed(loan_id: str) -> bool:
    """Notify the employee when HR disburses an approved loan."""
    try:
        loan = (
            LoanApplication.objects.select_related("employee", "loan_type")
            .prefetch_related("repayment_schedule")
            .get(pk=loan_id)
        )
    except LoanApplication.DoesNotExist:
        logger.warning("notify_loan_disbursed: LoanApplication missing. loan_id=%s", loan_id)
        return False

    employee = loan.employee
    if not employee.email:
        logger.info("notify_loan_disbursed: employee has no email. loan_id=%s", loan_id)
        return False

    subject = "Your Loan Has Been Disbursed"
    body = (
        f"Dear {_employee_name(loan)},\n\n"
        f"Your approved loan has been disbursed and is now active.\n\n"
        f"Loan type: {loan.loan_type.name}\n"
        f"Amount: {loan.amount}\n"
        f"{_monthly_installment_line(loan)}"
        f"Outstanding balance: {loan.outstanding_balance}\n\n"
        f"Repayment schedule:\n{_schedule_summary_lines(loan)}"
    )
    _send_email_if_possible(subject=subject, text_body=body, recipients=[employee.email])
    _notify_users_in_app(
        users=[employee],
        title=subject,
        body=body,
        ntype=NotificationType.LOAN_DISBURSED,
        data={
            "loan_id": str(loan.id),
            "status": loan.status,
            "action_url": _loan_action_url(loan),
        },
    )
    return True


@shared_task
def notify_loan_liquidated(loan_id: str) -> bool:
    """Notify the employee when HR liquidates an active loan early."""
    try:
        loan = LoanApplication.objects.select_related("employee", "loan_type").get(pk=loan_id)
    except LoanApplication.DoesNotExist:
        logger.warning("notify_loan_liquidated: LoanApplication missing. loan_id=%s", loan_id)
        return False

    employee = loan.employee
    if not employee.email:
        logger.info("notify_loan_liquidated: employee has no email. loan_id=%s", loan_id)
        return False

    subject = "Your Loan Has Been Liquidated"
    body = (
        f"Dear {_employee_name(loan)},\n\n"
        f"Your active loan has been liquidated by HR. No further repayments are due "
        f"under this loan.\n\n"
        f"Loan type: {loan.loan_type.name}\n"
        f"Original amount: {loan.amount}\n"
    )
    _send_email_if_possible(subject=subject, text_body=body, recipients=[employee.email])
    _notify_users_in_app(
        users=[employee],
        title=subject,
        body=body,
        ntype=NotificationType.LOAN_LIQUIDATED,
        data={
            "loan_id": str(loan.id),
            "status": loan.status,
            "action_url": _loan_action_url(loan),
        },
    )
    return True


@shared_task
def notify_loan_closed(loan_id: str) -> bool:
    """Notify the employee when HR closes a loan on resignation."""
    try:
        loan = LoanApplication.objects.select_related("employee", "loan_type").get(pk=loan_id)
    except LoanApplication.DoesNotExist:
        logger.warning("notify_loan_closed: LoanApplication missing. loan_id=%s", loan_id)
        return False

    employee = loan.employee
    if not employee.email:
        logger.info("notify_loan_closed: employee has no email. loan_id=%s", loan_id)
        return False

    subject = "Your Loan Has Been Closed"
    body = (
        f"Dear {_employee_name(loan)},\n\n"
        f"Your active loan has been closed following resignation processing. "
        f"The outstanding balance has been handled per company policy "
        f"(deducted from final entitlement where applicable).\n\n"
        f"Loan type: {loan.loan_type.name}\n"
        f"Original amount: {loan.amount}\n"
    )
    _send_email_if_possible(subject=subject, text_body=body, recipients=[employee.email])
    _notify_users_in_app(
        users=[employee],
        title=subject,
        body=body,
        ntype=NotificationType.LOAN_CLOSED,
        data={
            "loan_id": str(loan.id),
            "status": loan.status,
            "action_url": _loan_action_url(loan),
        },
    )
    return True


@shared_task
def mark_overdue_loan_installments() -> int:
    """Daily job: PENDING installments past due_date → OVERDUE on ACTIVE loans."""
    count = LoanEligibilityService.sync_overdue_installments()
    logger.info("mark_overdue_loan_installments: updated %s installment(s)", count)
    return count
