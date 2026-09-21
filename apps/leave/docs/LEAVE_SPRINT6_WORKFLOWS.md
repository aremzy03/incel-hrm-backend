# Leave Sprint 6 — Configurable approval workflows

Sprints 1–5 stay in place. Sprint 6 adds **workflow templates**, **submit-time snapshots**, **approver delegation**, and **SLA reminders**. Approval **status strings are unchanged** (`PENDING_TEAM_LEAD`, `PENDING_SUPERVISOR`, `PENDING_MANAGER`, `PENDING_HR`, `PENDING_ED`). Historical requests without a snapshot still use the old transition table.

Live template edits never rewrite in-flight routing. Blackout periods, encashment, and the balance-adjust API are **not** in this sprint (Sprint 7).

---

## What changed

- `LeaveWorkflowTemplate` + ordered `LeaveWorkflowStage` (sequential only).
- Seeded org-default **Standard approval chain** matching the previous hard-coded route.
- On submit / create-and-submit: `workflow_snapshot` is stored next to `policy` and `calculation_snapshot`.
- `ApproverDelegate` (primary, delegate, start/end, active). Approve/reject allow a covering delegate; role + org checks still run against the **primary**.
- Beat `escalate_stale_leave_approvals` (hourly at :15). Reminds current approvers (and active delegates) and notifies the **next** stage. Honors `LeaveSettings.notify_approver`. **Does not auto-approve** (`auto_approve_after_sla` defaults false and is not applied by the job).
- `prevent_self_approval` on `LeaveSettings` is enforced on approve/reject when enabled (still default **false**).
- Optional `LeaveSettings.approval_sla_hours` as org fallback when a stage/template has no `sla_hours`.
- Template selection: active template for the request’s **leave type**, else org default.

Parallel stages and duration/department routing rules are **not** implemented. Assign a leave-type-specific template or keep the org default.

---

## Status ↔ stage map (default template)

| Stage order | Approver source | API status | Skipped when |
| --- | --- | --- | --- |
| 1 | `TEAM_LEAD` | `PENDING_TEAM_LEAD` | Requester is TL/supervisor/LM/HR/ED/MD, **or** leading stage with no team lead |
| 2 | `SUPERVISOR` | `PENDING_SUPERVISOR` | Same senior roles, **or** leading unresolved (no unit supervisor) |
| 3 | `LINE_MANAGER` | `PENDING_MANAGER` | Requester is ED/MD. If requester is a line manager, approver is the **Management** department LM |
| 4 | `HR` | `PENDING_HR` | Requester is HR/ED/MD (HR skip) |
| 5 | `EXECUTIVE_DIRECTOR` | `PENDING_ED` | Requester is ED/MD (then the request is auto-`APPROVED`) |

After the last snapshotted stage, approve sets `APPROVED`. Reject from any pending stage still sets `REJECTED`.

Requests created before this sprint have `workflow_snapshot=null` and keep `PENDING_*` → next status as before (including HR-requester skip via `skip_hr_stage`).

---

## APIs

Base path `/api/v1/`.

### Workflows

| Method | Path | Auth |
| --- | --- | --- |
| GET | `/leave-workflows/` | Authenticated |
| POST | `/leave-workflows/` | HR / admin |
| GET/PATCH/DELETE | `/leave-workflows/{id}/` | GET any auth; write HR / admin |
| POST | `/leave-workflows/{id}/simulate/` | HR / admin |

Template fields: `name`, `is_active`, `is_org_default`, `leave_type` (optional), `mode` (`SEQUENTIAL` only), `reject_comment_required` (default true), `approve_comment_required` (default false), `sla_hours`, `auto_approve_after_sla` (default false), `stages[]`.

Stage fields: `order`, `approver_source` (`TEAM_LEAD`, `SUPERVISOR`, `LINE_MANAGER`, `HR`, `EXECUTIVE_DIRECTOR`, `NAMED_USER`, `ROLE`), `status_code`, `named_user`, `role_name`, `sla_hours`, `skip_if_unresolved` (prefix skip), `is_optional` (drop if unresolved even mid-chain), `skip_if_requester_roles`, `use_management_line_manager_for_line_manager_requester`.

**PATCH with `stages: [...]` replaces the full stage list.** Omit `stages` to edit template metadata only. Writes go to `LeaveSettingsAuditLog` (`object_type=LeaveWorkflowTemplate`). Optional write-only `reason`.

Simulate body: `{ "employee": "<uuid>", "leave_type": "<uuid>?", "total_working_days": optional }`. Returns resolved stages and `resolved_approvers`. `total_working_days` is ignored (duration routing not implemented).

### Delegates

| Method | Path | Auth |
| --- | --- | --- |
| GET/POST | `/leave-approver-delegates/` | Authenticated. Employees may create rows only with `user` = themselves. HR sees all. |
| GET/PATCH/DELETE | `/leave-approver-delegates/{id}/` | Owner or HR |

Fields: `user` (primary), `delegate`, `start_date`, `end_date`, `is_active`.

### Requests

Unchanged approve/reject/submit URLs. New read-only fields on the request: `workflow_snapshot`, `stage_entered_at`.

### Settings

`GET/PATCH /leave-settings/` includes `approval_sla_hours` (nullable). Notification toggles from Sprint 5 still gate emails (`notify_approver` for SLA reminders).

---

## Frontend guide

### Workflow builder

- Settings → Approval workflows. List templates; mark one org default.
- Stage table: drag/order, source dropdown, optional named user / role, SLA hours, skip-if-requester-role chips, optional/unresolved flags.
- Confirm: “Editing a live template does not change requests already submitted.”
- Do not invent new `PENDING_*` strings unless backend adds them; map custom sources onto existing statuses for API compatibility.
- Parallel / AND-split is not available.

### Simulate

- Pick employee + optional leave type. Show first status and each resolved approver email. Use before activating a new template.

### Delegation UI

- “Out of office”: primary, delegate, date range, active.
- Employees manage their own coverage; HR can set coverage for anyone.
- Inbox should treat the delegate as able to approve while the range is active.

### SLA

- Per-stage hours on the template; optional org fallback on general settings.
- Reminders reuse `notify_approver`. There is **no** auto-approve in the UI unless product later enables `auto_approve_after_sla` **and** backend starts applying it (currently stored but not executed).

### Request detail

- Show `workflow_snapshot.stages` as a read-only stepper. Do not edit the snapshot.

---

## Out of this sprint (Sprint 7)

- `POST /leave-balances/{id}/adjust/` with required reason + ledger row.
- Blackout periods (dept/org, date range, leave types).
- Encashment / termination unused-balance rules and payroll handoff.
- Utilization / liability reports and CSV export.
