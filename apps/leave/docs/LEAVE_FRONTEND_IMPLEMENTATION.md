# Leave frontend implementation index

Single map of the leave **settings** and **employee/HR request** APIs for the frontend. Base path: **`/api/v1/`**. All endpoints require authentication unless noted.

Sprint write-ups (read for field-level behaviour):

- [Sprint 1 — Policy foundation](LEAVE_SPRINT1_FOUNDATION.md)
- [Sprint 2 — Enforcement, half-day, pending hold](LEAVE_SPRINT2_ENFORCEMENT.md)
- [Sprint 3 — Assignments and versioning](LEAVE_SPRINT3_ASSIGNMENTS.md)
- [Sprint 4 — Accrual and carry-forward](LEAVE_SPRINT4_ACCRUAL.md)
- [Sprint 5 — Settings and calendars](LEAVE_SPRINT5_SETTINGS_CALENDARS.md)
- [Sprint 6 — Workflows, delegation, SLA](LEAVE_SPRINT6_WORKFLOWS.md)
- [Sprint 7 — HR ops: adjust, blackouts, encashment, reports](LEAVE_SPRINT7_HR_OPS.md)

Roadmaps: `apps/leave/LEAVE_ROADMAP.md`, `apps/leave/LEAVE_SETTINGS_ROADMAP.md`.

---

## Suggested settings IA

1. Leave types
2. Policies (draft / publish / clone / archive / impact preview)
3. Policy assignments + resolution debugger
4. Approval workflows + simulate
5. Working & holiday calendars + assignments
6. General leave settings
7. Blackout periods
8. Balance adjustments (on employee profile + ledger)
9. Reports (utilization, who is out, liability)
10. Audit history (`LeaveSettingsAuditLog` via policy/type/workflow write responses; no dedicated list API yet)

---

## HR settings APIs

| Area | Method | Path | Write auth |
| --- | --- | --- | --- |
| Types | GET/POST | `/leave-types/` | HR/admin write |
| Types | GET/PATCH/DELETE | `/leave-types/{id}/` | HR/admin write; delete only if unused |
| Types | POST | `/leave-types/{id}/activate/` `/deactivate/` | HR/admin |
| Policies | GET/POST | `/leave-policies/` | HR/admin write |
| Policies | GET/PATCH | `/leave-policies/{id}/` | PATCH drafts only |
| Policies | POST | `/leave-policies/{id}/publish/` `/archive/` `/clone/` | HR/admin |
| Policies | GET | `/leave-policies/{id}/impact-preview/` | HR/admin |
| Assignments | GET/POST | `/leave-policy-assignments/` | HR/admin write |
| Assignments | GET/PATCH/DELETE | `/leave-policy-assignments/{id}/` | HR/admin write |
| Resolution | GET | `/leave-policy-resolution/?employee=&leave_type=&date=` | Authenticated |
| Accrual preview | POST | `/leave-accrual/preview/` | HR/admin |
| Settings | GET/PATCH | `/leave-settings/` | HR/admin PATCH |
| Workflows | GET/POST | `/leave-workflows/` | HR/admin write |
| Workflows | GET/PATCH/DELETE | `/leave-workflows/{id}/` | HR/admin write |
| Workflows | POST | `/leave-workflows/{id}/simulate/` | HR/admin |
| Working calendars | CRUD | `/working-calendars/` | HR/admin write |
| Holiday calendars | CRUD | `/holiday-calendars/` | HR/admin write |
| Calendar holidays | nested on holiday calendars | see Sprint 5 | HR/admin write |
| Calendar assignments | CRUD | `/leave-calendar-assignments/` | HR/admin write |
| Public holidays | GET | `/public-holidays/` | Authenticated |
| Public holidays | POST | `/public-holidays/upload/` | HR/admin CSV |
| Delegates | CRUD | `/leave-approver-delegates/` | Owner or HR |
| Blackouts | CRUD | `/leave-blackout-periods/` | HR/admin write |
| Balances adjust | POST | `/leave-balances/{id}/adjust/` | HR/admin |
| Reports | GET | `/leave-reports/{utilization\|who-is-out\|liability}/` | HR/admin; `?export=csv` |

Optional write-only `reason` on most settings writes is stored on `LeaveSettingsAuditLog`.

### General settings fields (GET/PATCH `/leave-settings/`)

`leave_year_type`, `leave_year_start_month`, `leave_year_start_day`, `cross_year_deduction_rule`, `default_timezone`, `default_working_calendar`, `default_holiday_calendar`, notification toggles, `reminder_lead_hours`, `allow_hr_override`, `prevent_self_approval`, `approval_sla_hours`, **`encashment_allowed`**, **`encashment_max_days`**.

### Policy fields (high level)

Identity: `name`, `leave_type`, `status`, `version`, `effective_from`, `effective_to`.  
Entitlement/accrual: `annual_entitlement`, `accrual_method`, `accrual_rate`, `prorate_new_joiners`, `carry_forward`, `carry_forward_max_days`, `carry_forward_expiry_months`, `forfeit_unused`.  
Rules: `half_day_allowed`, `weekend_excluded`, `public_holiday_excluded`, `forfeited_on_resignation`, `allow_backdated`, `maximum_backdate_days`, `reliever_required`, `reliever_scope`, overlap fields.

---

## Employee / request APIs

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/leave-types/` | Eligible types also filtered at apply time |
| GET | `/leave-balances/` | Own; `?employee=` for allowed viewers |
| GET | `/leave-balances/{id}/` | |
| GET | `/leave-balances/{id}/transactions/` | Ledger |
| GET/POST | `/leave-requests/` | Create as DRAFT |
| GET/PATCH | `/leave-requests/{id}/` | Owner DRAFT; HR broader |
| POST | `/leave-requests/create-and-submit/` | Atomic create + submit |
| POST | `/leave-requests/{id}/submit/` | |
| POST | `/leave-requests/{id}/approve/` `/reject/` `/cancel/` | Role/stage + delegates |
| GET | `/leave-requests/{id}/logs/` | Audit trail |
| GET | `/leave-requests/eligible-relievers/` | Reliever picker |
| POST | `/leave-requests/reconcile/` | HR |
| POST | `/leave-requests/bulk-reconcile/` | HR |
| POST | `/leave-requests/bulk-reconcile-csv/` | HR |
| GET | `/calendar/` | Approved leave calendar |
| GET | `/leave-approver-delegates/` | Out-of-office coverage |

Create/PATCH request fields: `leave_type`, `start_date`, `end_date`, `reason`, `is_emergency`, `cover_person`, `is_half_day`, `half_day_period`, **`blackout_override_reason`** (HR override only).

Read extras: `status`, `total_working_days`, `policy`, `policy_version`, `calculation_snapshot`, `workflow_snapshot`, `stage_entered_at`, reconcile fields.

---

## Not built (do not invent UI)

- Leave attachments / medical certificates (Phase 4.1).
- Dedicated settings audit list endpoint.
- Payroll posting beyond `ENCASH` ledger rows.
- Auto-approve after SLA.
- Parallel / AND-split workflows.
- Separate Django perms for “adjust balances” vs generic HR.
- Policy `activate`/`deactivate` on types is implemented; hourly leave is not.
