# Leave Settings Roadmap

This document defines the settings an industry-standard HRM leave module should expose to authorized HR staff. It also describes how those settings should be modeled, validated, audited, exposed through the API, and introduced safely into the existing `apps/leave` application.

This roadmap complements `LEAVE_ROADMAP.md`. The broader roadmap covers leave functionality; this document focuses specifically on configurable settings and policy administration.

---

## Objectives

- [ ] Allow HR to change leave rules without code deployments.
- [ ] Make `LeavePolicy` the source of truth for entitlement and validation.
- [ ] Support different policies for employee groups, locations, departments, and employment types.
- [ ] Preserve historical behavior when policies change.
- [ ] Audit every policy, assignment, balance, calendar, and workflow change.
- [ ] Prevent invalid or internally inconsistent configurations.
- [ ] Keep authorization separate for policy viewing, editing, publishing, and balance adjustment.

---

## Configuration hierarchy

Settings should be separated into six areas:

1. **Leave Types** — what categories of leave exist.
2. **Leave Policies** — how each category behaves.
3. **Policy Assignments** — which employees receive each policy.
4. **General Leave Settings** — organization-wide defaults.
5. **Approval Workflows** — who approves and how requests move.
6. **Holiday and Working Calendars** — which dates count as working days.

Recommended rule precedence:

1. Employee-specific policy assignment
2. Employment group, grade, location, or department assignment
3. Organization-wide policy assignment
4. Leave type defaults only as a temporary fallback

The system should return the resolved policy and assignment source so HR can understand why an employee received a particular entitlement.

---

# Phase 1 — Leave type settings

## 1.1 Identity and presentation

### Settings

- [ ] `name` — employee-facing name, such as Annual Leave.
- [ ] `code` — immutable machine identifier, such as `ANNUAL`.
- [ ] `description` — explanation shown during application.
- [ ] `is_active` — controls new use without deleting historical records.
- [ ] `display_order` — controls ordering in employee forms.
- [ ] `calendar_color` — optional frontend calendar color.

### Rules

- A code must be unique and should not change after requests exist.
- Inactive types remain available in reports and historical requests.
- Deletion should be blocked when the leave type has policies, balances, or requests.
- Business rules must use `code`, not editable display names. Existing checks against names such as `"Annual"` and `"Sick"` should be migrated.

## 1.2 Classification

### Settings

- [ ] `payment_type` — `PAID`, `UNPAID`, or `PARTIALLY_PAID`.
- [ ] `balance_mode` — `LIMITED`, `UNLIMITED`, or `INFORMATIONAL`.
- [ ] `eligibility_gender` — `ANY`, `FEMALE`, `MALE`, or a legally appropriate configurable rule.
- [ ] `is_statutory` — identifies legally mandated leave.
- [ ] `requires_reason` — requires an employee explanation.
- [ ] `requires_attachment` — requires supporting evidence.
- [ ] `attachment_required_after_days` — e.g. medical certificate after two sick days.

### Implementation strategy

1. Extend `LeaveType` with a stable code, active status, and classification fields.
2. Backfill codes for existing types in a data migration.
3. Replace leave-name comparisons in services and serializers with codes.
4. Add database constraints for code uniqueness and valid attachment thresholds.
5. Keep leave types readable by employees but writable only by HR/Admin.

---

# Phase 2 — Core leave policy settings

`LeavePolicy` must become the runtime rule engine rather than an admin-only record.

## 2.1 Policy identity and lifecycle

### Settings

- [ ] `name` — descriptive name, e.g. “Nigeria Permanent Staff Annual Leave”.
- [ ] `leave_type` — category governed by the policy.
- [ ] `status` — `DRAFT`, `ACTIVE`, `ARCHIVED`.
- [ ] `effective_from` — first date on which the policy applies.
- [ ] `effective_to` — optional final effective date.
- [ ] `version` — immutable published policy revision number.
- [ ] `timezone` — optional when different legal entities operate in different zones.

### Rules

- Draft policies may be edited freely.
- Active policies should be versioned rather than overwritten.
- Published versions should be immutable except for controlled emergency correction.
- Effective periods for the same assignment scope must not overlap.
- Archived policies remain available for historical calculations.

## 2.2 Entitlement settings

### Settings

- [ ] `annual_entitlement` — total days or hours granted per leave year.
- [ ] `unit` — `DAYS` or `HOURS`.
- [ ] `balance_precision` — normally `0.5` day or an hourly increment.
- [ ] `allow_negative_balance` — allows advance leave.
- [ ] `negative_balance_limit` — maximum permitted advance.
- [ ] `maximum_balance` — cap beyond which accrual stops.
- [ ] `unlimited_balance` — for leave types that do not consume an entitlement.

### Validation

- Entitlement and limits must be non-negative.
- A negative limit is required only when negative balances are allowed.
- Limited and unlimited modes must be mutually exclusive.
- Precision must divide a normal working day without rounding drift.

## 2.3 Accrual settings

### Settings

- [ ] `accrual_method` — `UPFRONT`, `MONTHLY`, `WEEKLY`, `PAY_PERIOD`, or `ANNIVERSARY`.
- [ ] `accrual_rate` — amount earned per accrual interval.
- [ ] `accrual_start_rule` — leave-year start, employment date, or probation completion.
- [ ] `accrual_timing` — beginning or end of the interval.
- [ ] `prorate_new_joiners` — prorates entitlement by employment date.
- [ ] `prorate_leavers` — prorates entitlement on exit.
- [ ] `rounding_method` — down, nearest increment, or up.
- [ ] `waiting_period_days` — period before requests or accrual become available.

### Implementation strategy

1. Add a tested policy-resolution service.
2. Add pure accrual calculation functions before adding scheduled jobs.
3. Store each accrual and expiry as a ledger transaction rather than silently changing totals.
4. Use an idempotency key per employee, policy, and accrual period.
5. Run accrual through Celery Beat and provide an HR dry-run/preview endpoint.

## 2.4 Carry-forward and expiry

### Settings

- [ ] `carry_forward_allowed`.
- [ ] `carry_forward_max_days`.
- [ ] `carry_forward_percentage`.
- [ ] `carry_forward_expiry_months` or a fixed expiry date rule.
- [ ] `carry_forward_consumption_order` — carried balance before current entitlement.
- [ ] `forfeit_unused_balance`.

### Validation

- Only one of maximum days or percentage should be required unless product rules explicitly combine them.
- Expiry must be after the new leave year begins.
- Carry-forward fields must be ignored or cleared when carry-forward is disabled.

## 2.5 Working-day calculation

### Settings

- [ ] `exclude_weekends`.
- [ ] `exclude_public_holidays`.
- [ ] `allow_half_day`.
- [ ] `allow_hourly_leave`.
- [ ] `minimum_request_increment`.
- [ ] `count_non_working_days_between_dates` — useful for calendar-day statutory leave.

### Implementation strategy

1. Update `calculate_working_days` to accept the resolved policy and employee calendar.
2. Stop globally hard-coding Monday–Friday and holiday exclusion.
3. Use decimal-safe balance arithmetic for partial days.
4. Store the calculation inputs or a calculation snapshot on each request so historical totals do not change when policy settings change.

## 2.6 Request restrictions

### Settings

- [ ] `minimum_notice_days`.
- [ ] `maximum_advance_booking_days`.
- [ ] `allow_backdated_requests`.
- [ ] `maximum_backdate_days`.
- [ ] `minimum_duration`.
- [ ] `maximum_consecutive_days`.
- [ ] `maximum_requests_per_year`.
- [ ] `reason_required`.
- [ ] `reliever_required`.
- [ ] `allow_emergency_override`.
- [ ] `employee_cancellation_allowed`.
- [ ] `cancellation_cutoff_days`.

### Validation

- Emergency overrides must be explicit and audited.
- HR overrides require a reason.
- Cancellation of approved leave must invoke a balance refund transaction.
- Cross-year requests must either be split by balance year or follow a clearly configured rule.

## 2.7 Coverage and staffing controls

### Settings

- [ ] `overlap_control_enabled`.
- [ ] `overlap_scope` — team, unit, department, location, or organization.
- [ ] `maximum_people_absent`.
- [ ] `maximum_absence_percentage`.
- [ ] `reliever_scope` — team, unit, department, or organization.
- [ ] `reliever_confirmation_required`.

### Recommendation

Replace the current hard-coded “one Annual/Casual employee per organizational scope” rule with configurable staffing controls. A warning-only option should also exist so HR can permit exceptions without disabling the policy.

---

# Phase 3 — Policy assignment settings

Policies need an explicit assignment layer.

## 3.1 Assignment targets

### Supported targets

- [ ] Organization/legal entity.
- [ ] Country or work location.
- [ ] Department, unit, or team.
- [ ] Employment type: permanent, contract, intern, temporary.
- [ ] Grade, band, or job level.
- [ ] Employee-specific exception.

## 3.2 Assignment fields

- [ ] `policy`.
- [ ] `scope_type`.
- [ ] `scope_id`.
- [ ] `employee` for direct exceptions.
- [ ] `priority`.
- [ ] `effective_from`.
- [ ] `effective_to`.
- [ ] `is_active`.

## 3.3 Resolution rules

- Employee-specific assignments win over group assignments.
- More specific organizational assignments win over broader assignments.
- Explicit priority resolves ties at the same scope.
- Conflicting assignments should be rejected during publishing.
- The API should expose `resolved_policy`, `assignment_scope`, and `effective_date`.

### Implementation strategy

1. Add `LeavePolicyAssignment` as a separate model.
2. Centralize resolution in `resolve_leave_policy(employee, leave_type, on_date)`.
3. Cache resolution only with reliable invalidation after policy or employee changes.
4. Add an HR preview endpoint: “Which employees will this policy affect?”
5. Add a conflict report before publishing assignments.

---

# Phase 4 — General leave module settings

These are organization-wide defaults, not leave-type-specific rules.

## 4.1 Leave year

### Settings

- [ ] `leave_year_type` — calendar, fiscal, or employment anniversary.
- [ ] `leave_year_start_month`.
- [ ] `leave_year_start_day`.
- [ ] `cross_year_deduction_rule` — split by year or deduct from start year.
- [ ] `default_timezone`.

## 4.2 Defaults and safeguards

- [ ] `default_working_calendar`.
- [ ] `default_policy_fallback_enabled`.
- [ ] `allow_hr_override`.
- [ ] `hr_override_reason_required`.
- [ ] `prevent_self_approval`.
- [ ] `require_separation_of_duties`.
- [ ] `balance_display_mode` — available only or allocated/used/pending/available.

## 4.3 Notifications

### Settings

- [ ] Notify applicant on submission, approval, rejection, cancellation, and modification.
- [ ] Notify current approver.
- [ ] Approver reminder interval.
- [ ] Escalation threshold.
- [ ] Upcoming-leave reminder lead time.
- [ ] Notify reliever on assignment, change, approval, and cancellation.
- [ ] Department reminder enabled and recipient scope.
- [ ] Email, in-app, and realtime channel toggles.

### Implementation strategy

- Use a singleton `LeaveSettings` model per organization or legal entity.
- Do not store unrelated settings in environment variables.
- Validate changes through serializers and publish settings-change events.
- Read settings in notification tasks rather than hard-coding the current 24-hour behavior.

---

# Phase 5 — Approval workflow settings

## 5.1 Workflow templates

### Settings

- [ ] Workflow name and active status.
- [ ] Ordered approval stages.
- [ ] Approver source per stage: team lead, supervisor, line manager, HR, ED, named user, or role.
- [ ] Stage optionality and skip conditions.
- [ ] Whether approvals are sequential or parallel.
- [ ] Required approval count for parallel stages.
- [ ] Reject comment required.
- [ ] Approve comment required.
- [ ] Prevent requester from approving their own request.

## 5.2 Routing conditions

- [ ] Requester role.
- [ ] Leave type or policy.
- [ ] Request duration.
- [ ] Department, location, grade, or employment type.
- [ ] Emergency flag.
- [ ] Negative-balance usage.

## 5.3 Delegation and escalation

- [ ] Approver delegation enabled.
- [ ] Delegation start/end date.
- [ ] SLA hours per stage.
- [ ] Reminder frequency.
- [ ] Escalation recipient or next stage.
- [ ] Auto-approval allowed after SLA — normally disabled and used only with explicit governance.

### Implementation strategy

1. Introduce `LeaveWorkflowTemplate` and ordered `LeaveWorkflowStage`.
2. Snapshot the selected workflow and stages when a request is submitted.
3. Replace status-specific dictionaries with stage instances while preserving API status compatibility during migration.
4. Validate that each workflow can resolve an approver for its target population.
5. Provide a workflow simulator for HR before activation.

---

# Phase 6 — Holiday and working calendar settings

## 6.1 Working calendar

### Settings

- [ ] Working days of week.
- [ ] Standard hours per working day.
- [ ] Optional per-day working hours.
- [ ] Timezone.
- [ ] Calendar name, active status, and effective dates.

## 6.2 Holiday calendar

### Settings

- [ ] Holiday name and date.
- [ ] Recurring vs one-off.
- [ ] Country, state, region, or location scope.
- [ ] Full-day or partial-day holiday.
- [ ] Observed date.
- [ ] Optional vs mandatory holiday.

## 6.3 Assignment

- [ ] Assign calendars by location or employee.
- [ ] Define an organization default.
- [ ] Preview employees affected by calendar changes.

### Implementation strategy

1. Introduce `WorkingCalendar` and `HolidayCalendar` instead of one global holiday list.
2. Associate employees or locations with calendars.
3. Resolve the employee calendar at request calculation time.
4. Preserve current `PublicHoliday` data through a migration into the default calendar.

---

# Phase 7 — HR operational settings and tools

## 7.1 Balance adjustments

- [ ] Enable/disable manual adjustment by permission.
- [ ] Require adjustment reason and effective date.
- [ ] Support credit, debit, correction, carry-forward, accrual, expiry, and refund transaction types.
- [ ] Optional second approval for adjustments above a threshold.
- [ ] Prevent direct unaudited editing of `used_days` and `allocated_days`.

## 7.2 Blackout periods

- [ ] Name and date range.
- [ ] Applicable leave types and organizational scope.
- [ ] Hard block or warning-only mode.
- [ ] Exempt roles or employees.
- [ ] HR override with reason.

## 7.3 Encashment and termination

- [ ] Encashment allowed.
- [ ] Maximum encashable days.
- [ ] Eligible balance components.
- [ ] Calculation rate source.
- [ ] Forfeit, carry, or pay unused balance on termination.
- [ ] Payroll export status.

---

# Phase 8 — Security, permissions, and audit

## 8.1 Suggested permissions

- [ ] View leave types and published policies.
- [ ] Create/edit draft policies.
- [ ] Publish/archive policies.
- [ ] Assign policies.
- [ ] Edit general settings.
- [ ] Edit workflows.
- [ ] Manage calendars and holidays.
- [ ] Adjust balances.
- [ ] Apply HR overrides.
- [ ] View policy and balance audit logs.

Publishing and balance adjustment should be separate permissions from ordinary HR request processing.

## 8.2 Audit requirements

Every change should record:

- Actor.
- Timestamp.
- Object type and identifier.
- Previous and new values.
- Reason/comment.
- Effective date.
- Request/IP metadata where appropriate.
- Whether the action was an override.

Recommended implementation:

- `LeaveSettingsAuditLog` for configuration changes.
- `LeaveBalanceTransaction` for all balance movements.
- Immutable policy versions for published policy history.
- API endpoints to retrieve audit history for authorized HR/Admin users.

---

# Phase 9 — API design

## Proposed endpoints

### Leave types

- [ ] `GET/POST /api/v1/leave-types/`
- [ ] `GET/PATCH /api/v1/leave-types/{id}/`
- [ ] `POST /api/v1/leave-types/{id}/activate/`
- [ ] `POST /api/v1/leave-types/{id}/deactivate/`

### Policies

- [ ] `GET/POST /api/v1/leave-policies/`
- [ ] `GET/PATCH /api/v1/leave-policies/{id}/`
- [ ] `POST /api/v1/leave-policies/{id}/publish/`
- [ ] `POST /api/v1/leave-policies/{id}/archive/`
- [ ] `POST /api/v1/leave-policies/{id}/clone/`
- [ ] `GET /api/v1/leave-policies/{id}/impact-preview/`
- [ ] `GET /api/v1/leave-policies/{id}/audit-log/`

### Assignments

- [ ] `GET/POST /api/v1/leave-policy-assignments/`
- [ ] `GET/PATCH/DELETE /api/v1/leave-policy-assignments/{id}/`
- [ ] `GET /api/v1/leave-policy-resolution/?employee=&leave_type=&date=`

### General settings and workflows

- [ ] `GET/PATCH /api/v1/leave-settings/`
- [ ] CRUD `/api/v1/leave-workflows/`
- [ ] `POST /api/v1/leave-workflows/{id}/simulate/`

### Calendars and balances

- [ ] CRUD `/api/v1/working-calendars/`
- [ ] CRUD `/api/v1/holiday-calendars/`
- [ ] `POST /api/v1/leave-balances/{id}/adjust/`
- [ ] `GET /api/v1/leave-balances/{id}/transactions/`

## API behavior

- Use PATCH for partial edits.
- Return field-specific validation errors.
- Include `effective_from`, version, status, and last editor in policy responses.
- Use optimistic locking (`updated_at` or version number) to prevent HR users overwriting each other.
- Publishing, archiving, and balance adjustment should be explicit actions rather than generic PATCH side effects.

---

# Phase 10 — HR user experience

The frontend settings area should contain:

1. **Leave Types**
2. **Policies**
3. **Policy Assignments**
4. **Approval Workflows**
5. **Working & Holiday Calendars**
6. **General Settings**
7. **Balance Adjustments**
8. **Audit History**

## UX safeguards

- [ ] Draft/publish workflow with a change summary.
- [ ] Preview calculated entitlement for sample employees.
- [ ] Show warnings for conflicting assignments.
- [ ] Show affected employee count before publishing.
- [ ] Require confirmation for changes affecting active balances.
- [ ] Display policy resolution explanation on employee profiles.
- [ ] Prevent deletion of settings referenced by historical requests.
- [ ] Provide clone policy/version rather than forcing recreation.

---

# Recommended delivery plan

## Sprint 1 — Safe policy foundation

- [ ] Add leave-type codes and active state.
- [ ] Add policy lifecycle/effective dates.
- [ ] Seed one policy for every existing leave type.
- [ ] Implement policy resolver.
- [ ] Expose HR policy CRUD for drafts.
- [ ] Add settings audit log.

## Sprint 2 — Enforce existing policy fields

- [ ] Use policy entitlement during balance allocation.
- [ ] Enforce weekend and holiday settings.
- [ ] Prepare decimal balances and enforce half-day settings.
- [ ] Move hard-coded reliever and overlap decisions behind policy settings.

## Sprint 3 — Assignments and versioning

- [ ] Add policy assignments.
- [ ] Add assignment conflict validation.
- [ ] Add policy impact preview.
- [ ] Publish immutable policy versions.

## Sprint 4 — Accrual and carry-forward

- [ ] Add accrual configuration.
- [ ] Add balance transaction ledger.
- [ ] Add idempotent accrual and rollover jobs.
- [ ] Add carry-forward limits and expiry.

## Sprint 5 — Global settings and calendars

- [ ] Add leave-year and notification settings.
- [ ] Add working/holiday calendars and assignments.
- [ ] Migrate current public holidays.

## Sprint 6 — Configurable workflow

- [ ] Add workflow templates and stages.
- [ ] Add delegation, reminders, and escalation.
- [ ] Snapshot workflows on submission.

## Sprint 7 — HR operations

- [ ] Balance adjustment API.
- [ ] Blackout periods.
- [ ] Encashment/termination rules.
- [ ] Reports and audit views.

---

# Migration and compatibility strategy

- [ ] Preserve existing API responses during initial migrations.
- [ ] Generate stable codes for existing leave types.
- [ ] Create default policies from `LeaveType.default_days`.
- [ ] Treat current Monday–Friday and holiday behavior as default calendar behavior.
- [ ] Keep existing hard-coded approval routing until workflow templates are populated and tested.
- [ ] Add feature flags for policy resolution, accrual, and configurable workflow rollout.
- [ ] Provide management commands for dry-run migration and policy-resolution diagnostics.
- [ ] Never recalculate historical approved requests automatically after a policy edit.

---

# Testing strategy

## Unit tests

- [ ] Policy resolution precedence and effective dates.
- [ ] Accrual, proration, rounding, caps, carry-forward, and expiry.
- [ ] Working-day calculations for different calendars.
- [ ] Notice, backdating, duration, and overlap rules.
- [ ] Policy publication and version immutability.

## API tests

- [ ] HR/Admin permissions for each settings area.
- [ ] Employees cannot modify configuration.
- [ ] Draft/publish/archive transitions.
- [ ] Assignment conflict responses.
- [ ] Optimistic-lock conflict behavior.
- [ ] Audit records created for every write.

## Integration tests

- [ ] Policy edit affects new requests only from its effective date.
- [ ] Policy assignment changes resolve correctly.
- [ ] Accrual jobs are idempotent.
- [ ] Approved cancellation creates an exact refund transaction.
- [ ] Cross-year requests follow configured deduction rules.
- [ ] Existing leave data remains readable after migration.

---

# Definition of done

A leave setting is complete only when:

- [ ] It has a documented business meaning and safe default.
- [ ] It is validated at model/service and serializer levels.
- [ ] Runtime request and balance logic actually consumes it.
- [ ] Authorized HR users can manage it through an API.
- [ ] Unauthorized users are rejected by tested permissions.
- [ ] Changes are effective-dated or versioned where history matters.
- [ ] Every change is auditable.
- [ ] Existing records remain historically correct.
- [ ] Automated tests cover normal, boundary, and override behavior.

---

# Immediate priority

The first implementation should not attempt every setting at once. Start with:

1. Stable leave-type codes and active/inactive status.
2. Effective-dated, versioned `LeavePolicy`.
3. Policy API and audit history.
4. Runtime enforcement of entitlement, weekends, holidays, and half-day rules.
5. Policy assignments and resolution.
6. Accrual/carry-forward backed by a balance transaction ledger.

These items establish a safe configuration foundation for all later workflow, calendar, reporting, and payroll-related settings.
