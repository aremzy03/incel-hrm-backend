# XXA         Leave Sprint 4 — Accrual and carry-forward

This note is for frontend and API consumers. Sprints 1–3 stay in place: policy lifecycle, pending hold / refund, and employee-aware policy resolution. Sprint 4 adds **when entitlement is credited**, **year-end rollover**, and **ledgered** accrual / carry-forward / expiry.

Historical `allocated_days` on existing year rows are **not** rewritten when you change a policy or assignment. New-year jobs use `resolve_leave_policy` / `get_annual_entitlement(..., employee=, on_date=)` for that employee.

---

## What changed

- Policies gain accrual and carry-forward settings (see table below). Existing `carry_forward` is the allow-flag (`carry_forward_allowed` is the same boolean).
- `LeaveBalance` exposes `carried_forward_days` and `carry_forward_expires_on`.
- Ledger types: `ACCRUAL`, `CARRY_FORWARD`, `EXPIRY`, `FORFEIT` (plus existing DEDUCT / REFUND / ADJUST / RESERVE / RELEASE). Every allocation change writes a row with an **idempotency key**.
- Celery Beat jobs create next-year balances, credit accrual, apply carry-forward (with cap), expire unused when carry-forward is off, and expire carried days after `carry_forward_expiry_months`.
- Deactivating an employee (`is_active` True → False) forfeits remaining current-year days when `forfeited_on_resignation` is true (ledger `FORFEIT`). No payroll encashment.
- HR dry-run: `POST /api/v1/leave-accrual/preview/` and `manage.py leave_accrual_rollover` (default dry-run).

Pending holds and approved-cancel refunds are unchanged.

---



## Policy fields


| Field                         | Type                                             | Default   | Meaning                                                                               |
| ----------------------------- | ------------------------------------------------ | --------- | ------------------------------------------------------------------------------------- |
| `accrual_method`              | `UPFRONT` | `MONTHLY` | `WEEKLY` | `ANNIVERSARY` | `UPFRONT` | When days are credited.                                                               |
| `accrual_rate`                | decimal | null                                   | null      | Per-interval credit. Null = `annual_entitlement / 12` (monthly) or `/ 52` (weekly).   |
| `prorate_new_joiners`         | boolean                                          | `false`   | First year = remaining calendar days / days in year × entitlement.                    |
| `carry_forward`               | boolean                                          | `false`   | Unused days may roll into the next year.                                              |
| `carry_forward_max_days`      | decimal | null                                   | null      | Cap on days carried. Null = no cap.                                                   |
| `carry_forward_expiry_months` | int | null                                       | null      | Carried days expire at the end of that month in the **new** year (e.g. `3` → 31 Mar). |
| `forfeit_unused`              | boolean                                          | `true`    | Documented year-end behaviour when carry-forward is off (engine expires unused).      |
| `forfeited_on_resignation`    | boolean                                          | `true`    | On deactivation, remaining unused days are forfeited.                                 |


Clone copies these fields. ACTIVE policies remain immutable via PATCH.

---



## Jobs and Beat schedules

All jobs are idempotent (safe Beat re-run). Keys include employee, leave type, policy, and period.


| Beat name                    | Task                                              | When (UTC)         |
| ---------------------------- | ------------------------------------------------- | ------------------ |
| `leave-year-rollover`        | `apps.leave.tasks.run_leave_year_rollover`        | 1 Jan 00:10        |
| `leave-monthly-accrual`      | `apps.leave.tasks.run_leave_monthly_accrual`      | 1st of month 00:20 |
| `leave-weekly-accrual`       | `apps.leave.tasks.run_leave_weekly_accrual`       | Monday 00:25       |
| `leave-anniversary-accrual`  | `apps.leave.tasks.run_leave_anniversary_accrual`  | Daily 00:35        |
| `leave-carry-forward-expiry` | `apps.leave.tasks.run_leave_carry_forward_expiry` | Daily 00:40        |


**Year rollover** (for each active employee + eligible leave type):

1. Resolve policy on 1 Jan of the target year (assignments win).
2. Create the target-year `LeaveBalance` if missing.
3. Credit `UPFRONT` full (prorated) entitlement, or January slice for `MONTHLY` / week 1 for `WEEKLY`. `ANNIVERSARY` waits for the hire anniversary.
4. From the **prior** year: unused = allocated − used − pending. If prior policy `carry_forward`, credit `min(unused, max_days)` as `CARRY_FORWARD`. If not, reduce prior `allocated_days` with `EXPIRY`.

**Management command**

```bash
DJANGO_SETTINGS_MODULE=hrm_backend.settings.dev .venv/bin/python manage.py leave_accrual_rollover --year 2027
DJANGO_SETTINGS_MODULE=hrm_backend.settings.dev .venv/bin/python manage.py leave_accrual_rollover --year 2027 --apply
```

Default is dry-run. `--apply` writes ledger rows.

---



## APIs

Base path `/api/v1/`.

### Policy CRUD

Same Sprint 1–3 endpoints. Read/write the new fields on **drafts**.

### Balances

`GET /leave-balances/` now includes:


| Field                      | Notes                                                                                |
| -------------------------- | ------------------------------------------------------------------------------------ |
| `carried_forward_days`     | Part of `allocated_days` that came from last year.                                   |
| `carry_forward_expires_on` | Date or null. After this, a Beat job may reduce allocated by remaining carried days. |


`available_days` is still allocated − used − pending.

### Accrual preview (HR / admin)

`POST /leave-accrual/preview/`

```json
{
  "year": 2027,
  "as_of": "2027-01-01",
  "include_rollover": true,
  "include_monthly": true,
  "include_weekly": false,
  "include_anniversary": false,
  "include_carry_expiry": true
}
```

Always **dry-run**. Response: `dry_run`, `as_of`, `year`, `action_count`, `actions[]` (employee email, leave type, days), `skipped[]`.

Employees receive 403.

---

  

## Frontend guide



### Policy editor (draft)

Add an **Accrual** section: method, optional rate, prorate new joiners.

Add a **Year-end** section: carry forward toggle, max days, expiry months. If carry-forward is off, show that unused days will expire at year-end (ledgered). Keep forfeited on resignation.

Do not PATCH ACTIVE policies.

### Employee / HR balances

- Show **available** as today.
- If `carried_forward_days` > 0, show “X days carried from last year” and expiry date.
- Next-year rows appear after rollover (or after an employee’s first request in that year via the existing ensure-balance path). Prefer the job so the ledger exists.
- Do not treat a published entitlement change as rewriting this year’s allocated days.



### HR tools

- Settings: **Accrual preview** button → POST preview with the upcoming year. Show action count and a sample table (email, type, accrual days, carry-forward days).
- Confirm copy: “This is a preview. Beat or `leave_accrual_rollover --apply` writes balances.”



### Termination

When HR deactivates a user, remaining current-year days for types with `forfeited_on_resignation` go to zero. Encashment / payroll is not in this sprint.

---



## Out of this sprint (Sprint 5+)

- Organization `LeaveSettings` singleton: leave-year start (fiscal vs calendar), notification toggles, reminder intervals.
- Working / holiday calendars as assignable objects (still using global `PublicHoliday` + weekend flags).
- Workflow templates, blackouts, full encashment.



## Notes for Sprint 5

- Rollover currently assumes a **calendar year** (1 Jan). Fiscal `leave_year_start_month` should become the period key and Beat date.
- Notification settings should wrap existing leave emails rather than hard-coded 24h reminders.
- Calendar assignments should feed `calculate_working_days` without recalculating stored request totals.

