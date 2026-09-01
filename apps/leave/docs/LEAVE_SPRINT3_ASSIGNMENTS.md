# Leave Sprint 3 — Policy assignments and versioning

This note is for frontend and API consumers. Sprint 1 (policy lifecycle) and Sprint 2 (pending hold, half-day, staffing flags) stay in place. Sprint 3 adds **who gets which policy**.

Historical request day counts and `LeaveBalance.allocated_days` are still **not** rewritten when you assign a new policy or publish a new entitlement.

---

## What changed

- **`LeavePolicyAssignment`** maps a policy to a population: organization, department, unit, team, employment type (`User.contract_type`), or a single employee.
- **`resolve_leave_policy(employee, leave_type, on_date)`** picks a policy. `get_active_policy(leave_type, on_date=, employee=)` uses that when `employee` is passed.
- **Fallback:** if nothing matches, behaviour is unchanged — the unassigned ACTIVE policy for that leave type (highest version).
- Multiple ACTIVE policies per leave type are allowed so assigned packs can coexist with the org default. Publishing a draft still **archives other unassigned ACTIVE policies** unless you send `keep_existing_active: true`. Policies that already have **active assignments** are never auto-archived.
- ACTIVE policies remain immutable via PATCH. Clone → edit → publish.
- **`LeaveRequest.policy` / `policy_version`** are snapshotted on create and refreshed at submit. Later assignment edits do not change stored `total_working_days`.
- Pending holds stay on `(employee, leave_type, year)`. Assignment changes do not move reserved days onto another entitlement row.
- Assignment writes are audited on `LeaveSettingsAuditLog` (`object_type=LeavePolicyAssignment`).

### Org scopes that exist in this codebase

| `scope_type` | `scope_id` | Notes |
| --- | --- | --- |
| `ORGANIZATION` | empty | All active users. |
| `DEPARTMENT` | department UUID | `User.department` |
| `UNIT` | unit UUID | `User.unit` |
| `TEAM` | team UUID | `User.team` |
| `EMPLOYMENT_TYPE` | `PERMANENT` / `FIXED_TERM` / `CONTRACT` / `INTERN` / `OTHER` | `User.contract_type` |
| `EMPLOYEE` | set automatically to the employee UUID | Requires `employee` |

**Not yet mappable** (no first-class FKs): legal entity, country/work location, grade/band/job level. Use employee exceptions until those models exist.

---

## Resolution rules

On `on_date` (default today):

1. Active assignments whose policy is **ACTIVE**, leave type matches, and `effective_from` ≤ date ≤ `effective_to` (open-ended if `effective_to` is null).
2. Assignment must match the employee’s org attributes.
3. **Specificity** (high wins): employee → team → unit → department → employment type → organization.
4. **`priority`** (higher wins) breaks ties at the same specificity.
5. If the winning policy is outside its own `effective_from`/`effective_to`, it is skipped.
6. Otherwise **fallback** to the unassigned ACTIVE policy for the type.

Overlapping **active** assignments with the same leave type **and** the same scope identity (`scope_type` + `scope_id` + employee) are rejected at create/update with HTTP 400.

---

## API endpoints

Base path: `/api/v1/`. Authentication required.

### Assignments

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/leave-policy-assignments/` | Any authenticated | List. Filters: `leave_type`, `policy`, `scope_type`, `is_active`. |
| POST | `/leave-policy-assignments/` | HR or admin | Create. |
| GET | `/leave-policy-assignments/{id}/` | Any authenticated | Retrieve. |
| PATCH | `/leave-policy-assignments/{id}/` | HR or admin | Update. |
| DELETE | `/leave-policy-assignments/{id}/` | HR or admin | Delete. |

**Write body**

```json
{
  "policy": "<uuid>",
  "scope_type": "DEPARTMENT",
  "scope_id": "<department-uuid>",
  "employee": null,
  "priority": 0,
  "effective_from": "2026-01-01",
  "effective_to": null,
  "is_active": true,
  "reason": "Engineering pack"
}
```

Employee exception: `"scope_type": "EMPLOYEE", "employee": "<user-uuid>"` (`scope_id` is filled for you).

Organization: `"scope_type": "ORGANIZATION"` and omit `scope_id` / `employee`.

**Conflict error (400)**

`non_field_errors`: overlapping active assignment for the same leave type and scope.

### Resolution

| Method | Path | Auth |
| --- | --- | --- |
| GET | `/leave-policy-resolution/?employee=&leave_type=&date=` | Employee: self only. HR/admin: any employee. `leave_type` required. `date` ISO, default today. `employee` default = current user. |

**Response**

```json
{
  "employee": "<uuid>",
  "leave_type": "<uuid>",
  "effective_date": "2026-08-14",
  "source": "assignment",
  "assignment_scope": "DEPARTMENT",
  "assignment": { },
  "resolved_policy": { }
}
```

`source` is `assignment` or `fallback`. `resolved_policy` may be null if nothing is ACTIVE.

### Policy impact preview

| Method | Path | Auth |
| --- | --- | --- |
| GET | `/leave-policies/{id}/impact-preview/?date=` | HR or admin |

Employees who **currently resolve** to this policy on `date` (assignment or fallback). Payload: `employee_count`, `employees` (id, email, names, `assignment_scope`, `source`), `truncated`, `effective_date`, nested `policy`.

### Publish (Sprint 1, extra flag)

`POST /leave-policies/{id}/publish/`

```json
{ "reason": "Go live", "keep_existing_active": true }
```

Use `keep_existing_active: true` when publishing a departmental/exception policy that must sit beside the org default. Default `false` still replaces the unassigned ACTIVE policy (Sprint 1).

Clone / archive / audit-log are unchanged. Do not PATCH ACTIVE policies.

### Leave requests

Read serializer now includes `policy` (UUID or null) and `policy_version`. Show these on request detail as “calculated with policy vN”.

---

## Frontend guide

### New settings screen: Policy assignments

HR-only. Table: leave type, policy name/version, scope, target label, priority, effective dates, active.

Create flow:

1. Choose leave type, then an **ACTIVE** policy (clone+publish a pack first if needed, with `keep_existing_active`).
2. Choose scope and pick the department/unit/team/employee/contract type.
3. Set `effective_from` (and optional `effective_to`, `priority`).
4. If 400 conflict: show the overlapping window and ask HR to end-date or deactivate the old row.

### Impact preview

Before go-live, open the policy and call impact-preview. Show count + sample emails. Confirm: “Existing balances are not auto-reallocated. Pending holds stay on the employee’s current year balance for this leave type.”

### Employee apply / profile

- Load `GET /leave-policy-resolution/?leave_type=<id>` for the selected type.
- Use `resolved_policy` for half-day, reliever, weekend flags, entitlement **for new requests**.
- Do not assume the type’s only ACTIVE policy is the employee’s policy.
- On request detail, prefer snapshotted `policy` / `policy_version` over live resolution.

### Permissions

| Role | Assignments | Resolution | Impact preview |
| --- | --- | --- | --- |
| Employee | GET list/retrieve | Self only | 403 |
| HR / Admin | Full CRUD | Any employee | GET |

---

## Out of this sprint

- Accrual Beat, carry-forward jobs, pro-rata joiners (Sprint 4).
- Rewriting `LeaveBalance.allocated_days` when assignment or entitlement changes.
- Workflow templates, working calendars, blackouts, encashment.
- Legal entity / location / grade assignment targets.

## Notes for Sprint 4 (accrual / carry-forward / ledger jobs)

- Accrue against **resolved** policy for each employee on the accrual date; do not assume one ACTIVE policy per type.
- Idempotency keys should include `policy_id` + version **or** leave type + year, but **do not** transfer `pending_days` when an assignment changes mid-year.
- New year `LeaveBalance` rows should call `get_annual_entitlement(leave_type, employee=..., on_date=...)`.
- Carry-forward caps come from the policy that applies on 1 Jan (or configured leave-year start), not from historical request snapshots.
- Existing `LeaveBalanceTransaction` types already include DEDUCT/REFUND/ADJUST/RESERVE/RELEASE; add ACCRUAL / CARRY_FORWARD / EXPIRY without mutating approved request snapshots.
