# Leave Sprint 7 — HR operations

Sprints 1–6 stay in place. Sprint 7 adds **manual balance adjustment**, **blackout periods**, **termination encashment vs forfeit**, and **read-only HR reports**.

Leave request attachments (`LeaveAttachment`) were **not** built this sprint (no media/upload stack in this backend). Track them as leftover Phase 4.1.

---

## What changed

- `POST /leave-balances/{id}/adjust/` credits or debits `allocated_days` with a required reason and optional `effective_date`. Ledger type `ADJUST`, source `HR_ADJUST`. Direct PATCH of `used_days` / `allocated_days` is still not exposed.
- `GET /leave-balances/{id}/transactions/` returns the immutable ledger for that row.
- `LeaveBlackoutPeriod`: name, date range, optional leave types (empty = all), optional department (null = org-wide), `BLOCK` or `WARN`.
- Create/PATCH of leave requests runs blackout checks. `BLOCK` returns 400. HR/admin may override when `LeaveSettings.allow_hr_override` is true **and** `blackout_override_reason` is provided. `WARN` periods do not block.
- On user deactivation: if the resolved policy has `forfeited_on_resignation`, unused current-year days are still `FORFEIT`. If not forfeited and `LeaveSettings.encashment_allowed`, unused days (capped by `encashment_max_days`) are `ENCASH` for payroll. Idempotent; never both.
- Reports: utilization by department/type, who is out today/week, accrual liability (`allocated − used`), plus CSV.

---

## LeaveSettings fields (new)

| Field | Default | Meaning |
| --- | --- | --- |
| `encashment_allowed` | `false` | Pay unused days on exit when the policy does **not** forfeit. |
| `encashment_max_days` | null | Cap per balance year. Null = no cap. |

`allow_hr_override` (Sprint 5) now also gates blackout overrides.

Termination outcome:

1. Policy `forfeited_on_resignation=true` → `FORFEIT` (same as Sprint 4).
2. Else if `encashment_allowed` → `ENCASH` (reduces `allocated_days`; ledger is the payroll handoff).
3. Else unused days stay on the balance (carry).

Full payroll export status is out of scope.

---

## APIs

Base path `/api/v1/`.

### Balances

| Method | Path | Auth |
| --- | --- | --- |
| POST | `/leave-balances/{id}/adjust/` | HR / Django admin |
| GET | `/leave-balances/{id}/transactions/` | Owner, HR/admin, or viewers already allowed to see that employee’s balances |

Adjust body:

```json
{
  "delta": "3.00",
  "reason": "Payroll audit correction",
  "effective_date": "2026-01-15"
}
```

Positive `delta` increases entitlement (`delta_allocated_days`). Negative decreases it. `reason` is required. Response: `{ "balance": {...}, "transaction": {...} }`.

### Blackouts

| Method | Path | Auth |
| --- | --- | --- |
| GET/POST | `/leave-blackout-periods/` | GET any auth; write HR / admin |
| GET/PATCH/DELETE | `/leave-blackout-periods/{id}/` | same |

Fields: `name`, `start_date`, `end_date`, `enforcement` (`BLOCK` \| `WARN`), `leave_types` (UUID list), `department` (UUID or null), `is_active`. Optional write-only `reason` for settings audit.

On `POST /leave-requests/` (and PATCH of dates/type), send optional `blackout_override_reason` when HR is overriding a block.

### Reports

| Method | Path | Auth |
| --- | --- | --- |
| GET | `/leave-reports/utilization/?year=` | HR / admin |
| GET | `/leave-reports/who-is-out/?scope=today\|week` | HR / admin |
| GET | `/leave-reports/liability/?year=` | HR / admin |

Optional `department=<uuid>` and `export=csv` (do not use `format=csv`; DRF treats `format` as a renderer suffix and it 404s). Who-is-out also accepts `from` / `to` (`YYYY-MM-DD`). Liability adds `liability_days` (`allocated − used`). Read-only.

---

## Frontend guide

### Balance adjustment

- Settings or employee profile → Leave balances. HR-only “Adjust” dialog: signed decimal, required reason, optional effective date.
- After save, refresh balances and show the new ledger row. Never offer a free-edit of allocated/used fields.
- Ledger tab: `GET .../transactions/`. Show type, deltas, actor, reason, effective date, created_at.

### Blackouts

- Settings → Blackout periods. Date range, leave types (empty = all), department (empty = org), BLOCK vs WARN, active toggle.
- On apply-leave 400 containing “blackout”, show the message. For HR, show an override reason field and resubmit with `blackout_override_reason`.
- WARN periods are informational only in this sprint (they do not fail validation).

### Encashment

- General leave settings: `encashment_allowed`, `encashment_max_days`.
- Policy still owns `forfeited_on_resignation`. If that flag is on, exit forfeits even when encashment is allowed.
- There is no payroll posting API. Filter ledger `ENCASH` + source `TERMINATION` for finance.

### Reports

- HR reports page: three tabs (utilization, who is out, liability). Year picker, optional department, “Download CSV” (`?export=csv`). Employees get 403.

---

## Out of this sprint

- `LeaveAttachment` / document required-by-policy (Phase 4.1).
- Second approval for large adjustments; dedicated Django permission vs HR role.
- Exempt employees/roles on blackouts (beyond HR override).
- Payroll file export status / rates.
- Auto-approve after SLA (still stored, not executed).
- Parallel workflow stages.
