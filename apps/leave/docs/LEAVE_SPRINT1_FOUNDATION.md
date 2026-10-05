# Leave Sprint 1 — Policy foundation

This note is for frontend and API consumers. It describes the Sprint 1 leave settings work shipped in the HRM backend.

Sprint 1 makes leave types and leave policies first-class, versioned configuration. Runtime allocation and working-day counts now prefer an **active policy**. Historical approved request totals are **not** recalculated when a policy changes.

---

## What changed

- **Leave types** have a stable unique `code`, `is_active`, `display_order`, and optional `calendar_color`. Codes are backfilled (`ANNUAL`, `SICK`, `CASUAL`, `MATERNITY`, `PATERNITY`). Business rules use codes, not display names.
- **Leave policies** have identity/lifecycle fields: `name`, `status` (`DRAFT` / `ACTIVE` / `ARCHIVED`), `effective_from`, `effective_to`, `version`.
- Every existing leave type is seeded with **one ACTIVE policy** whose `annual_entitlement` matches `LeaveType.default_days`.
- `get_active_policy(leave_type, on_date=None)` is the resolver. Entitlement uses `LeavePolicy.annual_entitlement`, with `LeaveType.default_days` only as fallback.
- Working-day calculation honors `weekend_excluded` and `public_holiday_excluded` on the active policy.
- HR/Admin can manage types and policies over the API. Employees can read; they cannot write.
- Configuration writes are recorded on `LeaveSettingsAuditLog`.
- Deleting a leave type is blocked when it has policies, balances, or requests. Deactivate instead.
- Published (ACTIVE) policies cannot be PATCHed. Clone a draft, edit, then publish. Publishing archives the previous active policy for that type (one active policy per type in this sprint).

`LeaveType.default_days` remains on the type for fallback and display. Do not treat it as the runtime source of truth when an active policy exists.

---

## Features

| Feature | Behaviour |
| --- | --- |
| Leave type codes | Unique, uppercase. Immutable after leave requests exist. |
| Activate / deactivate types | Inactive types stay on historical requests/reports but cannot be used for new applications. |
| Draft policies | Freely editable and deletable. |
| Publish | Sets status `ACTIVE`, assigns next version, sets `effective_from` to today if missing, archives the previous active policy. |
| Archive | Sets status `ARCHIVED` and `effective_to` to today if missing. |
| Clone | Copies an existing policy into a new `DRAFT`. |
| Entitlement | New balances (user create, reconcile year rows, CSV seed) use policy entitlement. |
| Working days | New/edited date ranges use policy weekend/holiday flags. Existing stored `total_working_days` is not rewritten on status-only saves. |
| Audit | Create/update/delete/publish/archive/activate/deactivate/clone. |

---

## API endpoints

Base path: `/api/v1/`. All endpoints require authentication unless noted.

### Leave types

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/leave-types/` | Any authenticated | List types (includes inactive). |
| POST | `/leave-types/` | HR or Django admin | Create type. |
| GET | `/leave-types/{id}/` | Any authenticated | Retrieve. |
| PATCH / PUT | `/leave-types/{id}/` | HR or admin | Update. |
| DELETE | `/leave-types/{id}/` | HR or admin | Delete only if unused. |
| POST | `/leave-types/{id}/activate/` | HR or admin | Set `is_active=true`. |
| POST | `/leave-types/{id}/deactivate/` | HR or admin | Set `is_active=false`. |

**Leave type fields (read)**

| Field | Type | Notes |
| --- | --- | --- |
| `id` | UUID | |
| `name` | string | Employee-facing label. Unique. |
| `code` | string | Machine id, e.g. `ANNUAL`. Optional on create (derived from name). |
| `description` | string | |
| `default_days` | integer | Fallback entitlement only. |
| `is_active` | boolean | |
| `display_order` | integer | Sort key. Seeded types: Annual 10, Sick 20, Casual 30, Maternity 40, Paternity 50. |
| `calendar_color` | string | Optional hex/token for UI calendars. |
| `created_at` / `updated_at` | datetime | |

**Write extras:** `reason` (optional string, write-only) stored on the audit log.

**Create example**

```json
{
  "name": "Study Leave",
  "code": "STUDY",
  "default_days": 5,
  "description": "Exam / study time",
  "display_order": 60,
  "calendar_color": "#4F46E5",
  "reason": "Added after union agreement"
}
```

**Errors**

| Status | When |
| --- | --- |
| 401 | Unauthenticated. |
| 403 | Employee POST/PATCH/DELETE/activate/deactivate. |
| 400 | Duplicate `name`/`code`; changing `code` after requests exist; DELETE while policies/balances/requests exist. |

---

### Leave policies

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/leave-policies/` | Any authenticated | List. |
| POST | `/leave-policies/` | HR or admin | Create **DRAFT**. |
| GET | `/leave-policies/{id}/` | Any authenticated | Retrieve. |
| PATCH / PUT | `/leave-policies/{id}/` | HR or admin | Edit **DRAFT only**. |
| DELETE | `/leave-policies/{id}/` | HR or admin | Delete **DRAFT only**. |
| POST | `/leave-policies/{id}/publish/` | HR or admin | Activate this draft. |
| POST | `/leave-policies/{id}/archive/` | HR or admin | Archive draft or active. |
| POST | `/leave-policies/{id}/clone/` | HR or admin | New draft copy (`201`). |
| GET | `/leave-policies/{id}/audit-log/` | HR or admin | Audit rows for this policy. |

**Policy fields (read)**

| Field | Type | Notes |
| --- | --- | --- |
| `id` | UUID | |
| `name` | string | e.g. “Annual Policy”. |
| `leave_type` | UUID | FK. |
| `leave_type_detail` | object | Nested leave type. |
| `status` | `DRAFT` \| `ACTIVE` \| `ARCHIVED` | Read-only on create/update; change via publish/archive. |
| `version` | integer | `0` on drafts; incremented on publish. |
| `effective_from` / `effective_to` | date \| null | |
| `annual_entitlement` | integer | Days granted per leave year. |
| `carry_forward` | boolean | Stored; accrual engine is later. |
| `half_day_allowed` | boolean | Stored; half-day requests are later. |
| `weekend_excluded` | boolean | Default `true`. |
| `public_holiday_excluded` | boolean | Default `true`. |
| `forfeited_on_resignation` | boolean | Stored; termination engine is later. |
| `allow_backdated` | boolean | Already used by reconcile. |
| `maximum_backdate_days` | integer \| null | Already used by reconcile. |
| `created_at` / `updated_at` | datetime | |

**Write extras:** `reason` (optional). Publish/archive/clone body:

```json
{ "reason": "Go live for 2026" }
```

**Create draft example**

```json
{
  "name": "Annual 2027",
  "leave_type": "<uuid>",
  "annual_entitlement": 25,
  "weekend_excluded": true,
  "public_holiday_excluded": true,
  "carry_forward": false,
  "half_day_allowed": false,
  "allow_backdated": true,
  "effective_from": "2027-01-01",
  "reason": "Increase entitlement"
}
```

**Errors**

| Status | When |
| --- | --- |
| 403 | Employee write, or employee GET audit-log. |
| 400 | PATCH/DELETE on ACTIVE/ARCHIVED; publish when not DRAFT; `effective_to` before `effective_from`; unique one-ACTIVE-per-type violation (should not happen if you always publish through the action). |

Publishing policy A for Annual archives the previous Annual ACTIVE policy. New requests and new balances use the new entitlement and day-count flags from `effective_from`. Existing approved `total_working_days` stay as stored.

---

### Audit log payload

Returned from `GET /leave-policies/{id}/audit-log/`:

| Field | Type |
| --- | --- |
| `id` | UUID |
| `actor` | `{ id, email, first_name, last_name }` or null |
| `created_at` | datetime |
| `object_type` | `"LeavePolicy"` or `"LeaveType"` |
| `object_id` | UUID |
| `action` | `CREATE` `UPDATE` `DELETE` `PUBLISH` `ARCHIVE` `ACTIVATE` `DEACTIVATE` `CLONE` |
| `previous_values` / `new_values` | object or null |
| `reason` | string |
| `ip_address` | string or null |

There is no global audit list endpoint in this sprint. Type writes are stored with `object_type=LeaveType`.

---

## How the frontend should implement this

### Screens

Add a **Leave settings** area (HR/Admin only) with at least:

1. **Leave types** — table of name, code, active, default days, display order, calendar color.
2. **Policies** — filter by leave type and status. Show name, version, status, entitlement, effective dates.
3. **Policy editor** — draft form.
4. **Audit** — panel on the policy detail page.

Employees keep using existing leave request flows. On apply forms, prefer types with `is_active=true`. Show `name`; send `id`. You may display `code` in admin tables only.

### Permissions

| Role | Types | Policies |
| --- | --- | --- |
| Employee | GET list/retrieve | GET list/retrieve |
| HR / Admin | Full write + activate/deactivate | Full write + publish/archive/clone + audit |

Hide settings navigation from employees. Disable write controls unless the session user is HR or staff.

### Draft / publish UX

Recommended flow:

1. HR opens the active policy (read-only banner: “Published — clone to change”).
2. **Clone** → new draft (optionally prefill name as “{name} (draft)”).
3. Edit entitlement, weekend/holiday flags, backdating, effective dates.
4. Show a **change summary** (diff against the current ACTIVE policy using GET of both).
5. Confirm: “Publishing archives the current active policy for this leave type. Existing approved leave is not recalculated.”
6. POST `publish/` with a required `reason` in the UI (backend allows blank; product should require it).
7. Toast + refresh list. Previous policy appears as `ARCHIVED`.

Do **not** PATCH `status` yourself. Always use `publish/` and `archive/`.

Warn before publishing if `annual_entitlement` changes: existing employee balances for the year are **not** auto-updated in Sprint 1. Only newly created balance rows pick up the new entitlement.

### Fields to show

**Leave type form:** name, code (locked after create if requests exist), description, default days (label as “fallback days”), active toggle, display order, calendar color, reason.

**Policy form (draft):** name, leave type (locked after create), annual entitlement, weekend excluded, public holiday excluded, carry forward, half day allowed, forfeited on resignation, allow backdated, maximum backdate days, effective from/to, reason.

**Policy read-only (active/archived):** all of the above plus status and version. Actions: Clone, Archive (if not already archived), View audit.

For employee calendars, `weekend_excluded` / `public_holiday_excluded` affect how many days a **new** request will consume. Preview working days using the same date range the employee selected; the backend computes `total_working_days` on save.

### Leave request UI

- Reliever / overlap rules still follow type **codes** (`ANNUAL`/`CASUAL` staffing overlap; `SICK`/`MATERNITY`/`PATERNITY` reliever exemptions). Renaming “Annual” in the UI will not break rules.
- Maternity/Paternity eligibility still follows gender vs `MATERNITY` / `PATERNITY` codes.
- Inactive types: hide on the apply form; still show on historical request detail.

### Suggested list filters

- Policies: dedicated `?leave_type=` / `?status=` query params are not implemented yet — filter client-side from GET list.
- Types: filter `is_active` client-side.

---

## Out of this sprint (do not build UI as if they exist)

- Policy assignments by department/location/grade.
- Impact preview (`/impact-preview/`).
- Accrual Beat jobs / carry-forward execution.
- Half-day request schema.
- Workflow templates and calendars as settings objects.
- Automatic rewrite of existing `LeaveBalance.allocated_days` when entitlement changes.
- Global audit list for leave types (type audits are written but only queryable via ORM / admin for now).
