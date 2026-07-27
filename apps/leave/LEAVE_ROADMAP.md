# Leave Module — Prioritized Roadmap

Actionable todo list derived from the HR audit of `apps/leave`.  
Items are ordered by impact and dependency. Complete earlier phases before later ones where noted.

---

## Leave reconciliation

> HR can record backdated leave for staff who were absent without applying through the app.

### MVP (shipped)

- [x] `POST /api/v1/leave-requests/reconcile/` — HR-only endpoint
- [x] `LeaveRequest` fields: `is_reconciled`, `reconciled_by`, `reconciled_at`, `reconciliation_note`
- [x] `ApprovalAction.RECONCILE` audit log entry
- [x] Balance deduction on reconcile (same `used_days` increment as final approval)
- [x] `notify_leave_reconciled` — informational email/in-app notice to approval-chain stakeholders + cover person
- [x] `NotificationType.LEAVE_RECONCILED`
- [x] List filter: `?is_reconciled=true|false`
- [x] API tests under `apps/leave/tests/test_reconcile.py`

### Phase 2 — Reconciliation hardening (shipped)

- [x] **Balance restoration on cancel** — HR cancel of APPROVED (including reconciled) decrements `used_days`; guarded by unique REFUND ledger row per request
- [x] **`allow_insufficient_balance` override** — optional flag on reconcile/bulk payloads; recorded in ledger
- [x] **Policy-driven backdating rules** — `LeavePolicy.allow_backdated` and `maximum_backdate_days` enforced on reconcile
- [x] **`LeaveBalanceTransaction` ledger** — immutable audit row per balance change (DEDUCT, REFUND, ADJUST)
- [x] **Department-wide awareness** — optional `notify_department_colleagues` on reconcile/bulk
- [x] **Edit reconciled leave** — HR PATCH on reconciled APPROVED requests with automatic balance delta + MODIFY log
- [x] **Cross-year reconciliation** — `split_working_days_by_year()` splits deduction/refund/adjust by calendar year
- [x] **Bulk reconcile** — `POST bulk-reconcile/` (JSON rows) and `POST bulk-reconcile-csv/` (CSV upload)

---

## Phase 1 — Activate LeavePolicy (foundation)

> Unlocks correct allocation, day-counting rules, and HR self-service policy editing. Do this first.

### 1.1 Wire `LeavePolicy` into runtime logic

- [ ] Resolve policy for a leave type (and optionally employee group/location later) in a single helper, e.g. `get_active_policy(leave_type) -> LeavePolicy`
- [ ] Use `LeavePolicy.annual_entitlement` instead of `LeaveType.default_days` when creating balances (`accounts/signals.py` + CSV seed fallback)
- [ ] Honor `weekend_excluded` and `public_holiday_excluded` inside `calculate_working_days()` (pass policy or flags; stop hard-coding for all types)
- [ ] Enforce `half_day_allowed` when half-day support lands (Phase 3); until then, reject half-day attempts if flag is false
- [ ] Seed default `LeavePolicy` rows for existing leave types (migration or management command) so every type has an active policy
- [ ] Document: `LeaveType.default_days` becomes fallback only when no policy exists (or deprecate it after migration)

### 1.2 LeavePolicy API for HR

- [ ] Add `LeavePolicySerializer` (read + write)
- [ ] Add `LeavePolicyViewSet` — list/retrieve for authenticated users; create/update/delete for HR/Admin
- [ ] Register route: `/api/v1/leave-policies/`
- [ ] Validate uniqueness / one-active-policy-per-type (or explicit effective dates if you choose that model later)
- [ ] Tests: CRUD permissions, policy fields affect balance creation and working-day calc

**Implementation strategy**

1. Add `get_active_policy(leave_type)` in `services.py`.
2. Data migration: create a `LeavePolicy` per existing `LeaveType` from current `default_days` + sensible defaults (`carry_forward=False`, weekends/holidays excluded, etc.).
3. Change balance creation + working-day calc to read the policy.
4. Expose ViewSet + serializer; keep Django admin as secondary.
5. Add unit tests before flipping production allocations.

---

## Phase 2 — Accrual, year rollover, and balance integrity

> Without this, balances go stale every January and carry-forward / resignation flags stay decorative.

### 2.1 Accrual & year-end engine

- [ ] Design accrual model: lump-sum Jan 1 vs monthly (e.g. entitlement/12). Prefer configurable on `LeavePolicy` (e.g. `accrual_frequency`: ANNUAL | MONTHLY)
- [ ] Celery Beat task: create next-year `LeaveBalance` rows for all active employees
- [ ] Apply carry-forward when `carry_forward=True`: unused days → next year (add `carry_forward_max_days` and optional `carry_forward_expiry_date` on policy if needed)
- [ ] When `carry_forward=False`, expire unused days (log adjustment for audit)
- [ ] Pro-rate mid-year joiners: allocation based on hire date / months remaining
- [ ] Honor `forfeited_on_resignation`: on termination, zero remaining (or pay out — see Phase 5)

### 2.2 Pending balance hold (prevent over-booking)

- [ ] Add `pending_days` (or reserved) on `LeaveBalance`, or compute reserved from non-terminal requests
- [ ] On submit: reserve `total_working_days` against available = `allocated - used - pending`
- [ ] On approve: move pending → used (or deduct used and clear pending)
- [ ] On reject/cancel (pre-approval): release pending
- [ ] Update `validate_leave_balance` and overlap checks to consider pending + in-flight requests (not only APPROVED)

### 2.3 Refund on cancel / early return of approved leave

- [ ] On cancel of APPROVED leave: decrement `used_days` by remaining unused days (full or partial)
- [ ] Optional: early-return action (PATCH end date + refund delta) with approval log `MODIFY`
- [ ] Guard against double-refund; always log actor + reason

**Implementation strategy**

1. Extend `LeavePolicy` (or a thin AccrualConfig) with accrual frequency + carry-forward cap/expiry.
2. Implement pure functions in `services.py` (`accrue_for_year`, `apply_carry_forward`, `prorate_entitlement`) with unit tests first.
3. Register Beat schedules in Celery config.
4. Introduce pending/reserve in the same PR as submit/approve/cancel balance changes so invariants stay consistent.
5. Run a one-off dry-run command against staging before enabling Beat in prod.

---

## Phase 3 — Half-day / fractional leave

- [ ] Change `total_working_days` to `DecimalField` (or store days × 2 as integers — prefer Decimal for clarity)
- [ ] Add request shape: `is_half_day` + `half_day_period` (AM/PM), or start/end time for hourly later
- [ ] Gate on `LeavePolicy.half_day_allowed`
- [ ] Update serializers, balance math, calendar display, and emails to show 0.5 correctly
- [ ] Migration + backfill existing integer days as `.0`

**Implementation strategy**

1. Schema migration first; keep API accepting integers (coerce to Decimal).
2. Validation only when `is_half_day=True`.
3. Update `WorkingDaysService` and all `F()` balance updates to use Decimal-safe arithmetic.

---

## Phase 4 — Workflow hardening

### 4.1 Documents / attachments

- [ ] Add `LeaveAttachment` model (FK to request, file, uploaded_by, created_at)
- [ ] Optional: require attachment for Sick / Maternity per policy flag (`requires_document`)
- [ ] Upload/list/delete endpoints; virus-scan / size limits as per org standards

### 4.2 Approver delegation & SLA escalation

- [ ] `ApproverDelegate` (user, delegate, start/end dates, active)
- [ ] Approval/reject: allow delegate when primary is covered
- [ ] Beat job: escalate or remind if pending longer than `sla_hours` (policy or global setting)
- [ ] Notify original + next stage on escalation

### 4.3 Notice, backdating, blackouts

- [ ] Policy fields: `min_notice_days`, `allow_backdated`, `max_consecutive_days`
- [ ] `LeaveBlackoutPeriod` (dept/org, date range, leave types)
- [ ] Enforce in `LeaveRequestCreateSerializer.validate`

**Implementation strategy**

1. Attachments are independent — ship anytime after Phase 1.
2. Delegation needs clear identity checks in `approve`/`reject` (extend existing role + org checks).
3. Blackout/notice rules belong in the same validation pipeline as balance/overlap.

---

## Phase 5 — HR operations & lifecycle

### 5.1 HR balance adjustment API

- [ ] `POST /leave-balances/{id}/adjust/` — delta + required reason
- [ ] Write `LeaveApprovalLog`-style or dedicated `LeaveBalanceAdjustment` audit row
- [ ] Restrict to HR/Admin; never silent admin-only edits without reason in production flows

### 5.2 Pro-rata, encashment, final settlement

- [ ] On hire: create balances with prorated allocation
- [ ] On resignation/termination: compute unused days; if not forfeited, create encashment record for payroll
- [ ] Hook from employee status change / offboarding signal

### 5.3 Reporting & liability

- [ ] Endpoints or reports: utilization by dept/type, who’s out today/week, accrual liability (allocated − used)
- [ ] Export CSV for finance/HR
- [ ] Optional dashboard aggregates for calendar year

**Implementation strategy**

1. Balance adjust is a small, high-value HR win — can ship mid-Phase 2.
2. Encashment needs a clear payroll handoff contract (even if payout is manual at first).
3. Reporting can start as read-only aggregations over existing models; no new write paths required.

---

## Phase 6 — Polish & edge cases

- [ ] Cross-year leave spanning Dec–Jan: split deduction by year or document single-year rule
- [ ] Negative balance / LWP (leave without pay) as optional policy
- [ ] Align cancel error messages with allowed statuses (team lead / supervisor / manager)
- [ ] Performance: optimize recurring-holiday matching in `utils.calculate_working_days` if calendars grow large
- [ ] Deprecate unused `notify_leave_submitted` if fully replaced by `notify_approver_required`, or wire it consistently

---

## Suggested delivery order (sprints)

| Sprint | Focus | Deliverables |
|--------|--------|--------------|
| 1 | Phase 1 | Policy wired + API + seed migration + tests |
| 2 | Phase 2.2–2.3 | Pending hold + refund on cancel |
| 3 | Phase 2.1 | Accrual Beat + carry-forward + pro-rata joiners |
| 4 | Phase 3 + 4.1 | Half-day + attachments |
| 5 | Phase 4.2–4.3 + 5.1 | Delegation/SLA + blackouts + balance adjust |
| 6 | Phase 5.2–5.3 + 6 | Encashment + reporting + polish |

---

## Definition of done (per item)

- [ ] Behavior covered by unit/API tests under `apps/leave/tests/`
- [ ] HR-facing behavior documented (API fields + admin notes)
- [ ] No silent balance changes without an audit log entry
- [ ] Celery tasks idempotent (safe to re-run Beat)

---

## Out of scope (for now)

- Multi-country / multi-entity policy packs (can extend `LeavePolicy` with org unit later)
- Full payroll integration (encashment can start as a ledger row only)
- Mobile-specific leave UX (frontend concern)
