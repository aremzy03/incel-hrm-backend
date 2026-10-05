# Leave Sprint 5 — Global settings and calendars

This note is for frontend and API consumers. Sprints 1–4 stay in place (policies, assignments, pending/refund, accrual). Sprint 5 adds **organization LeaveSettings**, **working/holiday calendars**, and wires leave-year start into **rollover timing** without changing ledger idempotency keys or rewriting historical balances.

Historical `allocated_days` / `pending_days` / approved `total_working_days` are **not** rewritten when settings or calendars change. New requests use the calendar resolved **at calculation time** and store a `calculation_snapshot`.

---

## What changed

- Singleton `LeaveSettings` (one global row; there is no legal-entity/org model). Defaults match today’s behaviour.
- `WorkingCalendar` (weekdays, hours, timezone) and `HolidayCalendar` (named holiday lists). Existing `PublicHoliday` rows were **copied** into the org-default holiday calendar. `GET /public-holidays/` still works. CSV upload also upserts the default holiday calendar.
- Assign calendars to an **employee** or **department** (no Location model). Unassigned employees use org defaults: Mon–Fri + union of default holiday calendar **and** `PublicHoliday` (so legacy holiday rows still count).
- Notification tasks read toggles from `LeaveSettings`. Defaults are all **on**; reminder lead remains **24 hours**.
- Cross-year deduction: `SPLIT` reuses `split_working_days_by_year()` (calendar years, same as reconcile). `START_YEAR` charges the start date’s calendar year only.
- Accrual **idempotency keys and `LeaveBalance.year` stay calendar-year integers**. Fiscal leave-year settings change *when* Beat rollover runs and which `on_date` is used for policy resolution, not the key format.

---

## LeaveSettings fields

| Field | Default | Meaning |
| --- | --- | --- |
| `leave_year_type` | `CALENDAR` | `CALENDAR`, `FISCAL`, or `ANNIVERSARY`. |
| `leave_year_start_month` / `leave_year_start_day` | `1` / `1` | Used when type is `FISCAL` (day capped 1–28). Calendar/anniversary org jobs still use **1 January**. |
| `cross_year_deduction_rule` | `SPLIT` | `SPLIT` or `START_YEAR`. |
| `default_timezone` | `Africa/Lagos` | Display / calendar timezone default. |
| `default_working_calendar` / `default_holiday_calendar` | Seeded defaults | Org fallback. |
| `notify_applicant_on_submit` | `true` | Gates `notify_leave_submitted`. |
| `notify_applicant_on_decision` | `true` | Gates `notify_leave_decision`. |
| `notify_approver` | `true` | Gates `notify_approver_required`. |
| `notify_reliever` | `true` | Gates `notify_reliever_assigned`. |
| `notify_department_reminder` | `true` | Gates department 24h (configurable) reminder. |
| `reminder_lead_hours` | `24` | Converted to whole days (`ceil(hours/24)`) for start-date matching. |
| `allow_hr_override` | `true` | Existing HR reliever-scope bypass. Set `false` to force the same reliever rules as employees. |
| `prevent_self_approval` | `false` | Off by default so current routing is unchanged. When `true`, approver cannot be the requester. |

Changing these fields never reallocates balances.

---

## Accrual / leave year

- `LeaveBalance.year` remains the **calendar year of the leave-year start** (for calendar type: the year itself; for fiscal starting 1 April 2026, dates from 1 Apr 2026–31 Mar 2027 use year `2026`).
- Idempotency keys are still `accrual:{employee}:{type}:{policy}:{period}` with periods like `2026` / `2026-01`. Do not change keys after going fiscal or jobs will double-credit.
- Beat `leave-year-rollover` is still scheduled **1 Jan**. If `year` is omitted, the task **no-ops** unless today is the configured start (`01-01` or fiscal month/day). Run `leave_accrual_rollover --year YYYY --apply` on the fiscal start, or reschedule Beat.
- `ANNIVERSARY` leave-year type does **not** give each employee a personal balance year; org jobs stay on 1 Jan. Policy `accrual_method=ANNIVERSARY` is unchanged from Sprint 4.

---

## APIs

Base path `/api/v1/`. Authenticated read; HR or Django admin write unless noted.

### General settings

| Method | Path | Auth |
| --- | --- | --- |
| GET | `/leave-settings/` | Any authenticated |
| PATCH | `/leave-settings/` | HR / admin |

Optional write-only `reason` is stored on `LeaveSettingsAuditLog`.

### Working calendars

| Method | Path |
| --- | --- |
| GET/POST | `/working-calendars/` |
| GET/PATCH/DELETE | `/working-calendars/{id}/` |

Fields: `name`, `is_active`, `is_org_default`, `timezone`, `weekdays` (Monday=0 … Sunday=6), `hours_per_day`, `effective_from`, `effective_to`.

Default calendar: `[0,1,2,3,4]` (Mon–Fri).

### Holiday calendars

| Method | Path |
| --- | --- |
| GET/POST | `/holiday-calendars/` |
| GET/PATCH/DELETE | `/holiday-calendars/{id}/` |
| POST | `/holiday-calendars/{id}/holidays/` — append one holiday |
| DELETE | `/holiday-calendars/{id}/holidays/{holiday_id}/` |

Nested `holidays` on create/PATCH: `name`, `date`, `is_recurring`, `is_full_day`, `observed_date`, `location_scope`. **PATCH with `holidays: [...]` replaces the full list.** Omit `holidays` to edit calendar metadata only.

### Calendar assignments

| Method | Path |
| --- | --- |
| GET/POST | `/leave-calendar-assignments/` |
| GET/PATCH/DELETE | `/leave-calendar-assignments/{id}/` |

Set **either** `employee` **or** `department`, plus `working_calendar` and/or `holiday_calendar`. Employee assignment wins over department. There is no Location model.

### Legacy holidays

`GET /public-holidays/` and `POST /public-holidays/upload/` remain. Upload upserts `PublicHoliday` **and** the org-default `HolidayCalendar`.

Custom (non-default) holiday calendars do **not** include global `PublicHoliday` rows.

---

## Frontend guide

### General settings page

- Leave year: radio Calendar vs Fiscal. If Fiscal, show start month/day. Anniversary: show that org rollover stays 1 Jan; per-hire accrual is still the policy method.
- Timezone default.
- Cross-year: Split (recommended, current reconcile) vs Start year.
- Notification checkboxes matching the five toggles; reminder hours (default 24).
- `allow_hr_override` / `prevent_self_approval` as advanced safeguards. Do not enable self-approval block without checking whether HR/managers ever approve their own requests.
- Confirm: “This does not change existing balances or approved leave day counts.”

### Calendar editors

- Working calendar: name, weekday chips (Mon–Sun), hours/day, timezone, active, effective dates.
- Holiday calendar: table of name/date/recurring; add/remove rows via POST/DELETE holiday endpoints (prefer those over replacing the whole array).
- Assignments: pick employee or department, attach working and/or holiday calendar.
- Preview: for a sample employee, show counted days for a date range (client can call existing request create validation, or compute from GET calendars). Do not backfill old requests.

### Leave year

- After switching to fiscal, schedule rollover on that date. Until then, 1 Jan Beat will skip.

### Requests

- Show `calculation_snapshot` on request detail for support (weekdays, calendar ids, flags). Do not edit it in the UI.

---

## Out of this sprint (Sprint 6+)

- Workflow templates and stages, delegation, SLA escalation (keep hard-coded routing).
- Blackout periods, encashment, balance adjust API (if not already elsewhere).

## Notes for Sprint 6

- Snapshot the selected workflow on submit the same way policy + `calculation_snapshot` are stamped; do not rewrite historical routing.
- Delegation/SLA can read `reminder_lead_hours` / notification toggles already on `LeaveSettings`; add stage-level SLA hours on templates rather than replacing global reminder settings.
- `prevent_self_approval` is the hook for “requester cannot approve”; default is still off.
- Keep approval status strings (`PENDING_TEAM_LEAD`, …) until templates are proven, then map stages onto them.
