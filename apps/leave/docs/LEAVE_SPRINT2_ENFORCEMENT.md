# Leave Sprint 2 — Policy enforcement and pending hold

This note is for frontend and API consumers. It describes Sprint 2 of the leave module.

Sprint 1 remains the source of policy identity, publish/clone, and entitlement for **new** balance rows. Sprint 2 enforces half-day, reliever/overlap settings already stored on `LeavePolicy`, and introduces a **pending balance hold** so two in-flight requests cannot over-book the same entitlement.

Historical `allocated_days` and stored `total_working_days` are still not rewritten when a policy is published.

---

## What changed

- Request and balance day fields are **decimals** (two places). Integers from the API are coerced (e.g. `5` → `5.00`).
- Leave requests support **`is_half_day`** and **`half_day_period`** (`AM` / `PM`), gated by `LeavePolicy.half_day_allowed`.
- **`pending_days`** on each `LeaveBalance` holds days for submitted-but-not-terminal requests.
- Available balance is **`allocated_days - used_days - pending_days`** (also returned as `available_days`).
- On **submit**, days are reserved (`RESERVE` ledger). On **final approve**, pending is consumed into used (`DEDUCT`, which also decreases pending). On **reject** or **cancel before approval**, pending is released (`RELEASE`). Cancel of **APPROVED** leave still **refunds used** only (`REFUND`); it does not double-touch pending.
- Staffing overlap and reliever rules are **policy fields**, seeded to match previous hard-coded Annual/Casual / Sick-Maternity-Paternity behaviour.
- Personal overlap and reliever-busy checks include **in-flight** statuses (pending stages), not only `APPROVED`.
- Emails format day counts without trailing `.00` (`5` or `0.5`).

---

## API endpoints / fields

Base path: `/api/v1/`. Authentication unchanged from Sprint 1.

### Leave requests — create / update / read

`POST /leave-requests/`, `PATCH /leave-requests/{id}/`, `POST /leave-requests/create-and-submit/`

| Field | Type | Notes |
| --- | --- | --- |
| `is_half_day` | boolean | Default `false`. |
| `half_day_period` | `AM` \| `PM` \| `""` | Required when `is_half_day` is true. |
| `total_working_days` | decimal | `0.5` for a valid half-day working date. |

Existing fields (`leave_type`, `start_date`, `end_date`, `reason`, `is_emergency`, `cover_person`) are unchanged.

**Half-day rules**

- Active policy must have `half_day_allowed=true`.
- `start_date` must equal `end_date`.
- If that calendar day is excluded (weekend/holiday per policy), `total_working_days` is `0`.

### Leave balances — read

`GET /leave-balances/`

| Field | Type | Notes |
| --- | --- | --- |
| `allocated_days` | decimal | Unchanged meaning; now decimal. |
| `used_days` | decimal | Approved / reconciled consumption. |
| `pending_days` | decimal | Reserved by in-flight requests. |
| `remaining_days` | decimal | `allocated - used` (does **not** subtract pending). |
| `available_days` | decimal | `allocated - used - pending` — use this on apply forms. |

### Leave policies — extra settings (draft write / all read)

Same endpoints as Sprint 1 (`/leave-policies/`, publish/clone/archive). ACTIVE policies remain immutable via PATCH.

| Field | Type | Seeded default |
| --- | --- | --- |
| `half_day_allowed` | boolean | `false` (unchanged). |
| `reliever_required` | boolean | `true` for Annual/Casual; `false` for Sick/Maternity/Paternity. |
| `reliever_scope` | `AUTO` \| `TEAM` \| `UNIT` \| `DEPARTMENT` \| `ORGANIZATION` | `AUTO` (cascade team → unit → department). |
| `overlap_control_enabled` | boolean | `true` for Annual/Casual; `false` otherwise. |
| `overlap_scope` | same as reliever_scope | `AUTO` (lowest org level, previous behaviour). |
| `maximum_people_absent` | integer | `1` (one other person already absent blocks). |
| `overlap_enforcement` | `BLOCK` \| `WARN` | `BLOCK`. `WARN` allows the request (no error). |

MD/ED still never require a reliever. Emergency Annual/Casual still skip reliever. Those overrides remain in code.

`GET /leave-requests/eligible-relievers/?leave_type=<uuid>` uses `reliever_scope` of that type’s active policy when provided.

### Ledger (not a public write API)

`LeaveBalanceTransaction.transaction_type`: existing `DEDUCT` / `REFUND` / `ADJUST`, plus `RESERVE` / `RELEASE`.

Sources include `SUBMIT`, `REJECT_RELEASE`, `CANCEL_RELEASE` in addition to approval/reconcile refund sources.

---

## Features and validation

| Event | Balance |
| --- | --- |
| Create DRAFT | No hold. Validate against `available_days`. |
| Submit (pending chain) | `pending_days += total_working_days` (`RESERVE`). |
| Submit auto-approve (MD/ED) | `used_days += …` only (`DEDUCT`). No reserve. |
| Intermediate approve | No balance change. |
| Final approve | `pending_days -= days`, `used_days += days` on the same `DEDUCT` row(s). |
| Reject / cancel while pending | `pending_days -= days` (`RELEASE`). |
| Cancel APPROVED | `used_days -=` via `REFUND`. Pending is already zero. Idempotent unique refund. |
| Reconcile | Still deducts used immediately (no pending). |

Available = allocated − used − pending. A second request is rejected when it would exceed available, including days already reserved.

Personal overlap: any `PENDING_*` or `APPROVED` request for the same employee.

Department/staffing overlap: only if the request’s active policy has `overlap_control_enabled`. Counts distinct other employees in `overlap_scope` who have in-flight or approved leave of **any** overlap-controlled type. `maximum_people_absent` is the cap on those other people (default 1 = previous “one Annual/Casual person in the org scope”). `WARN` returns no HTTP error.

Reliever busy: reliever’s in-flight or approved leave overlapping the dates.

Publishing a new entitlement still does **not** rewrite existing `allocated_days`.

---

## Frontend implementation guide

### Apply form

- Show **Available** as `available_days`, with a breakdown: allocated / used / pending.
- If `half_day_allowed` on the selected type’s **active** policy (from `GET /leave-policies/` filtered by leave type + `status=ACTIVE`):
  - Offer a half-day toggle.
  - Force a single date; collect AM/PM.
  - Preview `0.5` working days when the date is a working day.
- If `reliever_required` on that policy, require cover person (except hide/skip for emergency, or when the session user is MD/ED).
- Load relievers from `GET /leave-requests/eligible-relievers/?leave_type=<id>`.
- Calendar colour remains `leave_type.calendar_color`. Half-day entries expose `is_half_day` / `half_day_period` on request and calendar payloads.

### Policy editor (draft)

Add sections:

1. Half-day allowed.
2. Reliever required + reliever scope.
3. Overlap enabled, scope, maximum people absent, enforcement (Block vs Warn).

Keep clone → edit → publish. Do not PATCH ACTIVE policies.

Copy for overlap: “Block matches today’s Annual/Casual rule (one other person already off in the same team/unit/department). Warn records the conflict but still allows submit.”

### After submit

Refresh balances: `pending_days` should rise immediately; `used_days` should rise only after **final** approval.

If submit is rejected for balance, show available vs requested (including `0.5`).

---

## Out of this sprint

- Accrual Beat, carry-forward execution, pro-rata joiners.
- Policy assignments (department/grade/location).
- Workflow templates, blackout calendars, attachments, encashment.
- Hourly leave.
- Returning `WARN` overlap as a structured `warnings` array on the create response (warn currently means “do not block”; no extra payload).
- Rewriting historical `allocated_days` when entitlement changes.

## Notes for Sprint 3 (assignments / versioning)

- Resolver is still one ACTIVE policy per leave type. Assignments will need `get_active_policy(leave_type, employee=, on_date=)` with scope priority.
- Clone already copies Sprint 2 staffing/half-day fields; assignment objects should snapshot `policy_id` + `version` on the request so historical holds/day counts stay stable.
- Pending holds are per `(employee, leave_type, year)` — assignment changes mid-year must not silently move reserved days to another policy’s entitlement.
