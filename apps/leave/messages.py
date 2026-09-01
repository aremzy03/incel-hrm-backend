"""User-facing validation and error messages for the leave module."""

from __future__ import annotations

import datetime

from django.contrib.auth import get_user_model

User = get_user_model()


def user_display_name(user) -> str:
    """Return the best available display name for an employee."""
    if user is None:
        return "Unknown employee"
    full = ""
    if hasattr(user, "get_full_name"):
        full = (user.get_full_name() or "").strip()
    if full:
        return full
    email = getattr(user, "email", None)
    if email:
        return email
    return str(getattr(user, "pk", "unknown"))


def format_short_date(value: datetime.date) -> str:
    return value.strftime("%d %b %Y")


def format_date_range(start_date: datetime.date, end_date: datetime.date) -> str:
    if start_date == end_date:
        return format_short_date(start_date)
    return f"{format_short_date(start_date)} to {format_short_date(end_date)}"


def leave_request_status_label(status: str) -> str:
    labels = {
        "DRAFT": "draft",
        "PENDING_TEAM_LEAD": "pending team lead approval",
        "PENDING_SUPERVISOR": "pending supervisor approval",
        "PENDING_MANAGER": "pending manager approval",
        "PENDING_HR": "pending HR approval",
        "PENDING_ED": "pending executive approval",
        "APPROVED": "approved",
        "REJECTED": "rejected",
        "CANCELLED": "cancelled",
    }
    return labels.get(status, status.replace("_", " ").lower())


def leave_type_label(leave_type) -> str:
    if leave_type is None:
        return "leave"
    return getattr(leave_type, "name", None) or "leave"


def self_overlapping_leave(existing) -> str:
    """Message when the applicant already has overlapping leave."""
    return (
        f"You already have {leave_request_status_label(existing.status)} "
        f"{leave_type_label(existing.leave_type)} leave from "
        f"{format_date_range(existing.start_date, existing.end_date)}. "
        "Please choose a different date range that does not overlap."
    )


def colleague_overlapping_leave(existing, *, requested_leave_type=None) -> str:
    """Message when staffing overlap rules block the request."""
    colleague = user_display_name(existing.employee)
    leave_name = leave_type_label(existing.leave_type or requested_leave_type)
    return (
        f"{colleague} already has {leave_request_status_label(existing.status)} "
        f"{leave_name} leave from "
        f"{format_date_range(existing.start_date, existing.end_date)}. "
        "Please choose a different date range or contact HR if an exception is required."
    )


def reliever_unavailable(cover_person, existing) -> str:
    """Message when the selected reliever is not available on the requested dates."""
    name = user_display_name(cover_person)
    return (
        f"{name} already has {leave_request_status_label(existing.status)} "
        f"{leave_type_label(existing.leave_type)} leave from "
        f"{format_date_range(existing.start_date, existing.end_date)} "
        "and cannot act as your reliever during that period. "
        "Please select another colleague as your reliever or choose different dates."
    )


def reliever_required(leave_type=None) -> str:
    leave_name = leave_type_label(leave_type)
    return (
        f"A reliever is required before you can submit this {leave_name} request. "
        "Select an eligible colleague who will cover your duties while you are away."
    )


def self_as_reliever() -> str:
    return (
        "You cannot assign yourself as the reliever. "
        "Choose another active colleague who will cover your duties."
    )


def reliever_not_eligible(scope_level: str) -> str:
    scope_labels = {
        "team": "team",
        "unit": "unit",
        "department": "department",
        "organization": "organisation",
        "organisation": "organisation",
    }
    scope = scope_labels.get(scope_level, scope_level or "organisation")
    return (
        f"The selected reliever is not an eligible active colleague in your {scope}. "
        "Use GET /api/v1/leave-requests/eligible-relievers/ to see valid options."
    )


def reliever_inactive() -> str:
    return (
        "The selected reliever is not an active user. "
        "Choose another active colleague or ask HR to reactivate the account."
    )


def invalid_date_range() -> str:
    return (
        "The end date must be on or after the start date. "
        "Adjust your dates so the leave period is valid."
    )


def inactive_leave_type() -> str:
    return (
        "This leave type is inactive and cannot be used for new requests. "
        "Choose another leave type or ask HR to reactivate it."
    )


def maternity_not_eligible() -> str:
    return (
        "Maternity leave is only available to female staff. "
        "Select a different leave type or contact HR if this profile is incorrect."
    )


def paternity_not_eligible() -> str:
    return (
        "Paternity leave is only available to male staff. "
        "Select a different leave type or contact HR if this profile is incorrect."
    )


def no_leave_balance(leave_type, year: int) -> str:
    return (
        f"No {leave_type_label(leave_type)} balance was found for {year}. "
        "Contact HR to confirm your entitlement or wait until your balance is allocated."
    )


def insufficient_leave_balance(
    leave_type,
    year: int,
    *,
    available,
    requested,
    format_days,
) -> str:
    return (
        f"Insufficient {leave_type_label(leave_type)} balance for {year}. "
        f"You have {format_days(available)} day(s) available but requested "
        f"{format_days(requested)} day(s). "
        "Shorten your date range, switch to a leave type with available balance, "
        "or contact HR for a balance adjustment."
    )


def duplicate_reconciled_leave(employee, leave_type, start_date, end_date) -> str:
    return (
        f"{user_display_name(employee)} already has an approved "
        f"{leave_type_label(leave_type)} request for "
        f"{format_date_range(start_date, end_date)}. "
        "Edit the existing request instead of creating a duplicate."
    )


def blackout_blocked(period_names: str, *, hr_may_override: bool) -> dict:
    payload = {
        "start_date": (
            f"Your selected dates fall within a blackout period ({period_names}). "
            "Choose dates outside the blackout or contact HR if leave is still required."
        )
    }
    if hr_may_override:
        payload["blackout_override_reason"] = (
            "As HR, you can override this blackout by resubmitting with "
            "blackout_override_reason explaining why the leave is allowed."
        )
    return payload


def submit_not_draft(current_status: str) -> str:
    return (
        f"Only draft leave requests can be submitted. "
        f"This request is currently {leave_request_status_label(current_status)}. "
        "Create a new request or cancel the current one if it is still pending approval."
    )


def approve_invalid_status(current_status: str, allowed_statuses) -> str:
    allowed = ", ".join(leave_request_status_label(s) for s in allowed_statuses)
    return (
        f"This request cannot be approved because it is "
        f"{leave_request_status_label(current_status)}. "
        f"Approval is only allowed while the request is: {allowed}."
    )


def reject_invalid_status(current_status: str, allowed_statuses) -> str:
    allowed = ", ".join(leave_request_status_label(s) for s in allowed_statuses)
    return (
        f"This request cannot be rejected because it is "
        f"{leave_request_status_label(current_status)}. "
        f"Rejection is only allowed while the request is: {allowed}."
    )


def cancel_not_allowed(current_status: str) -> str:
    return (
        f"This leave request cannot be cancelled from its current status "
        f"({leave_request_status_label(current_status)}). "
        "Contact HR if you need to change approved leave."
    )


def comment_required_for_approve() -> str:
    return (
        "A comment is required when approving this request. "
        "Add a short note explaining your decision before approving."
    )


def comment_required_for_reject() -> str:
    return (
        "A comment is required when rejecting this request. "
        "Add a reason so the employee understands why the request was declined."
    )


def department_missing_line_manager() -> str:
    return (
        "Your department does not have a line manager assigned, so this request "
        "cannot enter the approval workflow. Contact HR to assign a line manager "
        "before submitting leave."
    )


def management_missing_line_manager() -> str:
    return (
        "The Management department does not have a line manager assigned, so this "
        "request cannot enter the approval workflow. Contact HR to assign one "
        "before submitting leave."
    )


# ---------------------------------------------------------------------------
# HR settings / administration
# ---------------------------------------------------------------------------

def leave_type_code_immutable() -> str:
    return (
        "This leave type code cannot be changed because leave requests already exist. "
        "Create a new leave type if you need a different code."
    )


def leave_type_delete_blocked() -> str:
    return (
        "This leave type cannot be deleted because it has policies, balances, or "
        "historical requests. Deactivate it instead so employees cannot create new "
        "requests while history is preserved."
    )


def policy_edit_draft_only() -> str:
    return (
        "Only draft policies can be edited. Clone this policy to create a new draft, "
        "make your changes, then publish the new version."
    )


def policy_delete_draft_only() -> str:
    return (
        "Only draft policies can be deleted. Archive active or published policies "
        "instead so historical requests keep their policy reference."
    )


def policy_publish_draft_only() -> str:
    return (
        "Only draft policies can be published. Clone an active policy if you need "
        "to change its rules, then publish the new draft version."
    )


def policy_already_archived() -> str:
    return (
        "This policy is already archived and cannot be archived again. "
        "Clone it if you need a new draft based on these rules."
    )


def policy_effective_to_before_from() -> str:
    return (
        "The policy end date must be on or after the start date. "
        "Adjust effective_from and effective_to so the active period is valid."
    )


def policy_carry_forward_expiry_requires_flag() -> str:
    return (
        "Carry-forward expiry months apply only when carry_forward is enabled. "
        "Enable carry_forward or clear carry_forward_expiry_months."
    )


def policy_invalid_accrual_method() -> str:
    return (
        "The accrual method is not supported. Choose one of the configured accrual "
        "methods (for example UPFRONT, MONTHLY, or WEEKLY)."
    )


def assignment_effective_to_before_from() -> str:
    return policy_effective_to_before_from()


def assignment_policy_required() -> str:
    return (
        "Select the leave policy this assignment should apply. "
        "Create or publish a policy first if none is available."
    )


def assignment_scope_org_no_scope_id() -> str:
    return (
        "Organization-wide assignments must not set scope_id. "
        "Leave scope_id empty when scope_type is ORGANIZATION."
    )


def assignment_scope_org_no_employee() -> str:
    return (
        "Organization-wide assignments must not set employee. "
        "Use scope_type EMPLOYEE for a single-employee exception."
    )


def assignment_scope_employee_required() -> str:
    return (
        "Employee-specific assignments require an employee. "
        "Select the employee this policy exception applies to."
    )


def assignment_scope_employee_only_for_employee_type() -> str:
    return (
        "The employee field is only valid when scope_type is EMPLOYEE. "
        "Use scope_id to target a department, unit, team, or employment type."
    )


def assignment_scope_invalid_contract_type(valid_codes) -> str:
    return (
        "Employment-type assignments require a valid contract type code. "
        f"Use one of: {', '.join(sorted(valid_codes))}."
    )


def assignment_scope_uuid_required() -> str:
    return (
        "This assignment scope requires a valid UUID in scope_id. "
        "Select an existing department, unit, or team and paste its id."
    )


def assignment_scope_department_not_found() -> str:
    return (
        "No department was found with that scope_id. "
        "Check the department id or create the department first."
    )


def assignment_scope_unit_not_found() -> str:
    return (
        "No unit was found with that scope_id. "
        "Check the unit id or create the unit first."
    )


def assignment_scope_team_not_found() -> str:
    return (
        "No team was found with that scope_id. "
        "Check the team id or create the team first."
    )


def assignment_scope_unsupported() -> str:
    return (
        "This assignment scope type is not supported. "
        "Use ORGANIZATION, DEPARTMENT, UNIT, TEAM, EMPLOYMENT_TYPE, or EMPLOYEE."
    )


def assignment_conflict(conflict) -> str:
    end = format_short_date(conflict.effective_to) if conflict.effective_to else "open-ended"
    return (
        "This assignment overlaps an existing active assignment for the same leave "
        f"type and scope ({conflict.scope_type} from "
        f"{format_short_date(conflict.effective_from)} to {end}). "
        "Adjust the effective dates, deactivate the other assignment, or change "
        "the scope before saving."
    )


def settings_leave_year_month_invalid() -> str:
    return (
        "Leave year start month must be between 1 and 12. "
        "Use 1 for January or your fiscal year start month."
    )


def settings_leave_year_day_invalid() -> str:
    return (
        "Leave year start day must be between 1 and 28 so it is valid in every month. "
        "Choose the first day of your fiscal year within that range."
    )


def settings_reminder_lead_hours_invalid() -> str:
    return (
        "Reminder lead time must be at least 1 hour. "
        "Increase reminder_lead_hours so upcoming-leave alerts are sent in advance."
    )


def calendar_weekdays_required() -> str:
    return (
        "Select at least one working weekday (Monday=0 through Sunday=6). "
        "Most organisations use Monday–Friday: [0, 1, 2, 3, 4]."
    )


def calendar_weekday_invalid() -> str:
    return (
        "Each working weekday must be an integer from 0 (Monday) to 6 (Sunday). "
        "Remove invalid values and try again."
    )


def calendar_assignment_target_invalid() -> str:
    return (
        "Assign the calendar to either one employee or one department, not both "
        "and not neither. Choose exactly one target."
    )


def calendar_assignment_calendars_required() -> str:
    return (
        "Select a working calendar and/or a holiday calendar for this assignment. "
        "At least one calendar must be set."
    )


def workflow_named_user_required() -> str:
    return (
        "This approval stage uses a named approver. "
        "Select the user who should approve at this stage."
    )


def workflow_role_required() -> str:
    return (
        "This approval stage uses a role-based approver. "
        "Enter the role_name that should approve at this stage."
    )


def workflow_duplicate_active_for_leave_type() -> str:
    return (
        "Another active workflow already applies to this leave type. "
        "Deactivate the other workflow or choose a different leave type."
    )


def workflow_single_org_default() -> str:
    return (
        "Only one organization default workflow is allowed. "
        "Unset is_org_default on the existing default workflow first."
    )


def workflow_stage_order_unique() -> str:
    return (
        "Each workflow stage must have a unique order number. "
        "Renumber the stages so no two stages share the same order value."
    )


def delegate_cannot_be_self() -> str:
    return (
        "You cannot delegate approval authority to yourself. "
        "Choose another active user as the delegate."
    )


def balance_adjust_reason_required() -> str:
    return (
        "A reason is required for manual balance adjustments. "
        "Explain why the balance is being changed for audit purposes."
    )


def balance_adjust_delta_zero() -> str:
    return (
        "The adjustment amount cannot be zero. "
        "Enter a positive value to credit days or a negative value to debit days."
    )


def reconciliation_note_required() -> str:
    return (
        "A reconciliation note is required when HR records backdated leave. "
        "Briefly explain why the leave was recorded outside the normal workflow."
    )


def bulk_reconcile_rows_required() -> str:
    return (
        "Provide at least one row to reconcile. "
        "Add employee, leave type, dates, and a reconciliation note for each row."
    )


def hr_override_reason_required() -> str:
    return (
        "An override reason is required for this HR action. "
        "Explain why the normal validation rule is being bypassed."
    )


def iso_date_required(field_name: str = "date") -> str:
    return (
        f"Use an ISO date for {field_name} in YYYY-MM-DD format "
        "(for example 2026-03-15)."
    )


def permission_policy_audit() -> str:
    return (
        "Only HR or admin users can view policy audit history. "
        "Contact your administrator if you need access."
    )


def permission_balance_ledger() -> str:
    return (
        "You do not have permission to view this employee's balance ledger. "
        "You can only view your own balances unless you are HR or an authorised manager."
    )


# ---------------------------------------------------------------------------
# Email / in-app notification copy
# ---------------------------------------------------------------------------

def format_working_days(value) -> str:
    from .utils import format_leave_days

    return format_leave_days(value)


def leave_request_summary_lines(
    leave_request,
    *,
    include_reason: bool = True,
    include_status: bool = False,
    include_employee: bool = True,
) -> list[str]:
    lines = []
    if include_employee:
        lines.append(f"Employee: {user_display_name(leave_request.employee)}")
    lines.extend(
        [
            f"Leave type: {leave_type_label(leave_request.leave_type)}",
            f"Dates: {format_date_range(leave_request.start_date, leave_request.end_date)}",
            f"Total working days: {format_working_days(leave_request.total_working_days)}",
        ]
    )
    if include_status:
        lines.append(
            f"Current stage: {leave_request_status_label(leave_request.status)}"
        )
    if include_reason and getattr(leave_request, "reason", None):
        lines.append(f"Reason: {leave_request.reason}")
    elif include_reason:
        lines.append("Reason: Not provided")
    return lines


def leave_request_summary_block(leave_request, **kwargs) -> str:
    return "\n".join(leave_request_summary_lines(leave_request, **kwargs))


def email_action_required_subject(employee_name: str) -> str:
    return f"Action required: approve leave for {employee_name}"


def email_action_required_intro(employee_name: str) -> str:
    return (
        f"{employee_name} has submitted a leave request that is waiting for your "
        "approval. Please review the details below and approve or reject the "
        "request in the HRM portal."
    )


def email_action_required_body(leave_request, *, action_url: str = "") -> str:
    employee_name = user_display_name(leave_request.employee)
    parts = [
        email_action_required_intro(employee_name),
        "",
        leave_request_summary_block(leave_request, include_status=True),
    ]
    if action_url:
        parts.extend(["", f"Review request: {action_url}"])
    else:
        parts.extend(["", "Sign in to the HRM portal to review this request."])
    return "\n".join(parts)


def email_decision_subject(employee_name: str, *, approved: bool) -> str:
    if approved:
        return f"Your leave request has been approved — {employee_name}"
    return f"Your leave request was not approved — {employee_name}"


def email_decision_body(
    leave_request,
    *,
    approved: bool,
    comment: str = "",
    action_url: str = "",
) -> str:
    if approved:
        intro = (
            "Good news — your leave request has been approved. "
            "The dates below are now confirmed in the system."
        )
        if leave_request.cover_person_id:
            intro += (
                f" {user_display_name(leave_request.cover_person)} has been "
                "notified as your reliever."
            )
    else:
        intro = (
            "Your leave request was not approved at this time."
        )
        if comment.strip():
            intro += f" Approver comment: {comment.strip()}"
        else:
            intro += " No comment was provided by the approver."
        intro += (
            " Contact your line manager or HR if you need to discuss alternative "
            "dates or next steps."
        )

    parts = [intro, "", leave_request_summary_block(leave_request, include_employee=False)]
    if action_url:
        parts.extend(["", f"View request: {action_url}"])
    return "\n".join(parts)


def email_reliever_subject(employee_name: str) -> str:
    return f"You are assigned as reliever for {employee_name}"


def email_reliever_body(leave_request, *, action_url: str = "") -> str:
    employee_name = user_display_name(leave_request.employee)
    parts = [
        (
            f"You have been assigned as reliever for {employee_name}. "
            "Please coordinate with them and cover their duties during the "
            "period below."
        ),
        "",
        leave_request_summary_block(leave_request),
    ]
    if action_url:
        parts.extend(["", f"View request: {action_url}"])
    return "\n".join(parts)


def email_department_reminder_subject(employee_name: str, start_date) -> str:
    return f"Upcoming leave: {employee_name} from {format_short_date(start_date)}"


def email_department_reminder_body(
    leave_request,
    *,
    department_name: str,
    action_url: str = "",
) -> str:
    employee_name = user_display_name(leave_request.employee)
    parts = [
        (
            f"{employee_name} from {department_name} will be away on approved "
            f"{leave_type_label(leave_request.leave_type)} leave soon. "
            "Plan coverage accordingly."
        ),
        "",
        leave_request_summary_block(leave_request),
    ]
    if action_url:
        parts.extend(["", f"View calendar: {action_url}"])
    return "\n".join(parts)


def email_reconciled_subject(employee_name: str) -> str:
    return f"Leave recorded by HR for {employee_name}"


def email_reconciled_body(
    leave_request,
    *,
    reconciled_by_name: str,
    action_url: str = "",
) -> str:
    employee_name = user_display_name(leave_request.employee)
    parts = [
        (
            f"HR has recorded backdated leave for {employee_name}. "
            "No approval action is required — this notice is for your awareness."
        ),
        f"Recorded by: {reconciled_by_name}",
        "",
        leave_request_summary_block(leave_request),
        f"HR note: {leave_request.reconciliation_note or 'Not provided'}",
    ]
    if action_url:
        parts.extend(["", f"View record: {action_url}"])
    return "\n".join(parts)


def email_sla_escalation_subject(employee_name: str) -> str:
    return f"Escalation: leave approval overdue for {employee_name}"


def email_sla_escalation_body(leave_request) -> str:
    employee_name = user_display_name(leave_request.employee)
    return "\n".join(
        [
            (
                f"A leave request for {employee_name} has exceeded its approval "
                f"time limit and is still "
                f"{leave_request_status_label(leave_request.status)}. "
                "Please follow up with the current approver or take action if you "
                "are the next stage owner."
            ),
            "",
            leave_request_summary_block(leave_request, include_status=True),
        ]
    )

