# Loan Module — HR and Technical Audit

This audit covers the current DRF loan module in `apps/loan`, including its models, serializers, services, API views, notifications, reports, administration, migrations, and tests.

The module is a solid approval-and-tracking foundation, but it is not yet a complete employee lending system. Its strongest areas are workflow control, authorization, notifications, and auditability. Its largest gaps are configurable loan policies, affordability controls, financial transaction accounting, payroll integration, and lifecycle support.

---

## 1. Current capability summary

### Data and policy

- `LoanType` stores a name and description.
- `LoanSettings` controls whether line-manager approval is required and which department or unit observes loans.
- `LoanApplication` stores the requested amount, tenure, purpose, status, computed installment, outstanding balance, and lifecycle dates.
- `LoanRepaymentSchedule` stores installment number, due date, amount due, and payment status.
- `LoanApprovalLog` records workflow actions, actors, comments, status transitions, and timestamps.

### Application workflow

The current workflow is:

`DRAFT → PENDING_MANAGER → PENDING_HR → PENDING_ED → PENDING_MD → APPROVED → ACTIVE → CLOSED or LIQUIDATED`

An application may also move from a pending approval state to `REJECTED`.

The module supports:

- Employee draft creation and editing.
- Eligibility validation on creation and submission.
- Optional line-manager approval.
- HR, Executive Director, and Managing Director approvals.
- HR disbursement.
- Equal monthly repayment schedule generation.
- HR-managed installment payment statuses.
- Early liquidation.
- Closure following resignation.
- Email and in-app notifications.
- Outstanding-loan, schedule-summary, and employee-ledger reports.
- CSV export.

---

## 2. What is built well

### 2.1 Workflow authorization

Approval stages are restricted by role, and the line-manager stage validates the identity of the employee's actual department manager. Applications from line managers can be routed to the Management department line manager.

This is stronger than relying on role membership alone and should remain part of the design.

### 2.2 Audit trail

`LoanApprovalLog` provides a useful operational audit trail containing:

- The actor.
- Action performed.
- Previous and new statuses.
- Comment.
- Timestamp.

Approval logs are read-only through the API and Django administration interface. The same pattern should be extended to policy changes and financial transactions.

### 2.3 Transactional state changes

Important workflow changes use database transactions, and notifications are queued after successful commits. This reduces the risk of notifying users about state changes that were rolled back.

### 2.4 Notifications

The module notifies:

- Line managers and HR when action is required.
- ED and MD when applications reach their stages.
- Employees after approval, rejection, disbursement, liquidation, and resignation closure.
- Configured observers for informational visibility.

### 2.5 Reporting foundation

The module provides:

- Active loans with outstanding balances.
- Upcoming installment summaries grouped by month.
- Full employee loan history, repayment schedule, and approval logs.
- Streamed CSV exports.

### 2.6 Basic eligibility controls

The module prevents:

- Unconfirmed employees from applying.
- Employees with an approved or active loan from taking another loan.
- Non-positive loan amounts.
- Tenures outside the current global 1–12 month limit.

---

## 3. Audit findings and gaps

### 3.1 Critical — loan policies are not configurable

`LoanType` contains only `name` and `description`. It does not define the rules governing that loan scheme. Important rules are hardcoded:

- Tenure is globally limited to 1–12 months.
- The approval chain is fixed in Python.
- The active-loan limit is fixed at one.
- Loan calculations assume zero interest.
- Loan types are exposed through a read-only API.

HR therefore cannot create, edit, deactivate, or version operational loan policies without a code change and deployment.

**Business impact**

- Policy changes depend on developers.
- Different schemes cannot have different eligibility, amount, tenure, or interest rules.
- Historical applications cannot reliably identify the exact policy terms under which they were approved.

**Required remediation**

- Introduce a versioned `LoanPolicy` model linked to `LoanType`.
- Expose HR-only policy management endpoints.
- Resolve and snapshot the active policy when an application is submitted.
- Prevent policy edits from retroactively changing submitted or active loans.

### 3.2 Critical — no affordability or debt-service assessment

The module does not compare the proposed installment with salary or existing deductions.

Industry-standard controls commonly include:

- Maximum installment as a percentage of net pay.
- Maximum aggregate employee debt-service ratio.
- Consideration of existing statutory and voluntary deductions.
- Minimum residual or take-home pay after deductions.

**Risk**

An employee may receive a loan whose installment exceeds affordable payroll capacity.

### 3.3 Critical — self-approval is not fully prevented

The line-manager stage validates the specific manager, but HR, ED, and MD stages only validate role membership. An applicant who also holds one of those roles could potentially approve their own application at that stage.

**Required remediation**

- Reject every approval or rejection action where `request.user == loan.employee`.
- Prefer configurable separation-of-duties rules, but self-approval prevention should be mandatory.
- Add tests for applicants holding HR, ED, or MD roles.

### 3.4 Critical — repayment accounting is status-based rather than transaction-based

An installment records only `PENDING`, `PAID`, or `OVERDUE`. It does not record:

- Amount actually paid.
- Payment date.
- Payment source or method.
- Payroll run or external reference.
- Partial payment.
- Reversal or correction.
- Actor who recorded the payment.

`outstanding_balance` is recalculated from schedule rows not marked `PAID`, so a partially paid installment cannot be represented accurately.

**Required remediation**

- Add an immutable `LoanTransaction` or `LoanPayment` ledger.
- Derive balances from posted transactions.
- Treat schedule status as a derived value.
- Support partial payments, overpayments, reversals, and allocations to installments.

### 3.5 High — repayment schedule rounding can overstate the total due

Every installment is calculated using ceiling rounding on `amount / tenure`. Applying the rounded-up value to every installment can make the repayment schedule total exceed the principal.

**Required remediation**

- Calculate standard installments using the selected rounding rule.
- Set the final installment to `principal + interest + fees - prior installments`.
- Assert that schedule totals exactly match the contractual total.
- Add tests for values that do not divide evenly by tenure.

### 3.6 High — interest, fees, and financial terms are absent

The current implementation assumes interest-free principal-only loans. It cannot represent:

- Flat interest.
- Reducing-balance interest.
- Processing or administrative fees.
- Late-payment penalties.
- Interest rebates on early settlement.
- Taxable benefit or notional interest.
- Multiple currencies.

Even if current company loans are interest-free, these fields should be explicit policy choices rather than implicit assumptions.

### 3.7 High — no payroll or finance integration

HR manually changes installment statuses. There is no:

- Payroll deduction instruction.
- Deduction result import.
- Payroll cut-off rule.
- Arrears carry-forward.
- Finance disbursement confirmation.
- General-ledger or reconciliation reference.

**Recommended approach**

Start with import/export contracts and an immutable deduction ledger before implementing direct payroll integration.

### 3.8 High — disbursement lacks accounting detail

Disbursement stores only `disbursed_at`. It does not record:

- Actual amount disbursed.
- Bank account or payment channel.
- Payment reference.
- Disbursing officer.
- Fees deducted at source.
- Value date.
- Failed or reversed disbursement.

### 3.9 High — liquidation and resignation closure lose settlement evidence

Liquidation and resignation handling set the outstanding balance to zero without recording the amount recovered or waived.

**Risk**

The system cannot distinguish between:

- A fully paid settlement.
- A payroll final-entitlement deduction.
- A write-off.
- A waiver.
- An administrative closure.

Each outcome should be represented by posted financial transactions and a settlement reason.

### 3.10 Medium — eligibility logic is incomplete

The appraisal eligibility check is currently a placeholder that always passes. The module also lacks:

- Minimum service length.
- Employment-type restrictions.
- Grade or salary-band restrictions.
- Disciplinary or performance conditions.
- Re-borrowing cooldown.
- Policy-specific concurrent-loan rules.
- Eligibility revalidation at final approval or disbursement.

### 3.11 Medium — fixed approval chain

All applications follow the same senior approval chain regardless of loan type or amount.

Industry practice commonly routes by:

- Loan scheme.
- Amount band.
- Department or entity.
- Risk or exception status.

Small salary advances may require only manager and HR approval, while large vehicle or housing loans may require Finance and MD approval.

### 3.12 Medium — missing employee lifecycle actions

The module does not support:

- Withdrawal before final approval.
- Cancellation before disbursement.
- Resubmission after rejection.
- Employee-requested early settlement.
- Partial prepayment.
- Top-up.
- Restructuring or refinancing.
- Payment holiday or moratorium.
- Write-off.

### 3.13 Medium — no supporting documents, guarantors, or collateral

There is no model or workflow for:

- Supporting documents.
- Required-document checklists by policy.
- Guarantors or co-signers.
- Collateral.
- Consent to payroll deductions.
- Employee acceptance of approved terms.

### 3.14 Medium — no policy-change audit

Approval activity is logged, but changes to `LoanSettings` are not. The system does not record:

- Who changed a setting.
- Previous and new values.
- Reason for change.
- Effective date.
- Approval of the policy change.

### 3.15 Medium — reporting gaps

Additional operational reports are needed:

- Approval ageing and SLA breaches.
- Delinquency and arrears ageing.
- Collections and repayment history.
- Disbursements by period and scheme.
- Loan exposure by department and employee.
- Affordability exceptions.
- Policy exceptions.
- Settlement, write-off, and waiver report.
- Payroll reconciliation.

---

## 4. Target architecture

The recommended target separates policy, contract, schedule, and transactions.

### Policy layer

- `LoanSettings` — organization-wide defaults and controls.
- `LoanType` — scheme identity and availability.
- `LoanPolicy` — versioned scheme terms with effective dates.
- `LoanApprovalRule` — configurable routing by scheme and amount band.
- `LoanPolicyChangeLog` — immutable audit of policy changes.

### Contract layer

- `LoanApplication` — request and approval lifecycle.
- `LoanContract` or policy snapshot fields — immutable approved terms.
- `LoanAttachment` — supporting and contractual documents.
- `LoanGuarantor` — optional guarantor details and consent.
- `LoanDisbursement` — actual payment event and finance reference.

### Servicing layer

- `LoanRepaymentSchedule` — expected contractual installments.
- `LoanTransaction` — immutable payments, payroll deductions, fees, interest, waivers, write-offs, and reversals.
- `LoanSettlement` — liquidation, final-entitlement recovery, or write-off details.

Balances and schedule statuses should be derived from posted transactions rather than manually overwritten.

---

## 5. Prioritized implementation strategy

## Phase 1 — Controls and calculation correctness

> Resolve audit and financial-integrity risks before expanding features.

- [ ] Block self-approval at every approval stage.
- [ ] Correct schedule rounding and balance the final installment.
- [ ] Add database constraints for positive amounts and valid financial values.
- [ ] Require a reason for liquidation, closure, waiver, and adjustment.
- [ ] Add tests for concurrent actions and invalid status transitions.
- [ ] Use row locking (`select_for_update`) for approval, disbursement, and payment posting.

**Implementation strategy**

1. Centralize workflow transitions in a service rather than duplicating state changes across view actions.
2. Lock the application row inside each transactional transition.
3. Implement a pure schedule calculator and test totals, dates, and rounding.
4. Add separation-of-duties tests before release.

---

## Phase 2 — Configurable and versioned policies

- [ ] Extend `LoanType` with `is_active`.
- [ ] Add `LoanPolicy` with effective dates, amount limits, tenure limits, interest, fees, eligibility, and affordability rules.
- [ ] Add HR-only policy CRUD endpoints.
- [ ] Add policy validation and activation controls.
- [ ] Snapshot resolved policy terms on submission.
- [ ] Add `LoanPolicyChangeLog`.
- [ ] Replace hardcoded validation with policy-driven validation.

**Implementation strategy**

1. Seed a version-1 policy for each existing loan type using current behavior.
2. Keep existing applications linked to the seeded policy or backfill a contractual snapshot.
3. Introduce a single policy resolver, such as `get_effective_policy(loan_type, date)`.
4. Route all application validation and schedule generation through the resolved policy.
5. Never mutate an activated policy version; create a new version with a future effective date.

---

## Phase 3 — Financial ledger and servicing

- [ ] Add `LoanTransaction`.
- [ ] Record actual amount and date for every repayment.
- [ ] Support partial payments and reversals.
- [ ] Allocate payments to installments.
- [ ] Derive outstanding principal, interest, fees, and arrears.
- [ ] Add `LoanDisbursement` and `LoanSettlement`.
- [ ] Replace direct zeroing of balances with settlement transactions.

**Implementation strategy**

1. Define transaction types and posting rules.
2. Make transactions immutable; corrections must create reversal entries.
3. Add a balance service that recalculates from the ledger.
4. Backfill current paid schedule rows into payment transactions.
5. Reconcile migrated balances before enabling ledger-based calculations.

---

## Phase 4 — Affordability and payroll integration

- [ ] Add salary and deduction inputs to eligibility evaluation.
- [ ] Enforce installment-to-net-pay and residual-pay thresholds.
- [ ] Generate payroll deduction instructions.
- [ ] Import payroll deduction results.
- [ ] Handle missed, partial, and reversed deductions.
- [ ] Add payroll reconciliation reports.

**Implementation strategy**

1. Define a stable interface to the payroll module instead of directly coupling model internals.
2. Begin with CSV/API import and export if payroll is external.
3. Require idempotency keys and external references for imported transactions.
4. Test duplicate imports, late payroll runs, and correction runs.

---

## Phase 5 — Flexible workflow and employee servicing

- [ ] Add amount-band and scheme-specific approval rules.
- [ ] Add approver delegation, reminders, and escalation.
- [ ] Add withdrawal, cancellation, resubmission, and offer acceptance.
- [ ] Add early repayment, top-up, restructure, moratorium, and write-off workflows.
- [ ] Add documents, guarantors, consent, and approved-term acceptance.

---

## Phase 6 — Reporting, compliance, and operations

- [ ] Add arrears ageing and portfolio exposure reports.
- [ ] Add approval SLA and policy-exception reports.
- [ ] Add statements and settlement certificates.
- [ ] Add tax/perquisite reporting where required.
- [ ] Add configurable data retention and document-access controls.

---

## 6. Migration and release approach

1. Preserve current API behavior while introducing policy and ledger models behind feature flags.
2. Seed policy versions that reproduce the current 1–12 month, interest-free behavior.
3. Backfill policy snapshots for existing applications.
4. Backfill financial transactions from paid installments and documented settlements.
5. Run reconciliation reports comparing old and new outstanding balances.
6. Enable policy-driven applications for new drafts first.
7. Enable ledger-driven servicing after reconciliation sign-off by HR and Finance.
8. Retain rollback and read-only access to legacy fields during the transition.

---

## 7. Definition of done

- [ ] Every submitted loan is tied to immutable contractual policy terms.
- [ ] HR can create, schedule, activate, deactivate, and supersede policies without code changes.
- [ ] No employee can approve their own loan.
- [ ] Schedule totals exactly match contractual principal, interest, and fees.
- [ ] Every balance change has an immutable financial transaction and actor/reference.
- [ ] Payroll imports are idempotent and reconcilable.
- [ ] Policy changes and exceptions are auditable.
- [ ] Unit and API tests cover permissions, transitions, calculations, migrations, and concurrency.
- [ ] HR and Finance sign off on migrated balances and operational reports.

---

## 8. Out of scope for the first release

- Multi-country lending regulation packs.
- Credit-bureau integration.
- External consumer lending.
- Complex collateral valuation.
- Full accounting general-ledger integration.

These can be added after policy versioning, transaction accounting, affordability, and payroll reconciliation are stable.
