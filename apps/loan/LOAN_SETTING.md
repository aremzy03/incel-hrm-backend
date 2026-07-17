# Loan Module — Industry-Standard Settings and Policy Strategy

This document defines the recommended HR-editable settings for the loan module and an implementation strategy for introducing them safely.

Settings are divided into two layers:

- **Layer 1 — Global loan settings:** organization-wide controls and defaults.
- **Layer 2 — Loan scheme policy:** versioned rules for each loan type, such as Personal Loan, Compassionate Loan, Salary Advance, Vehicle Loan, or Housing Loan.

The global layer should provide defaults and hard organizational controls. The scheme layer should provide the terms specific to each loan product. Scheme settings may override global defaults only where the global setting explicitly permits an override.

---

## 1. Design principles

### 1.1 Configuration must not rewrite existing contracts

Changing a policy must affect only applications submitted on or after the new policy's effective date. Existing submitted, approved, or active loans must retain the terms accepted at submission or approval.

### 1.2 Policy versions must be immutable after activation

HR should create a new policy version instead of editing an active version in place. Each version should have:

- Version number.
- Effective start date.
- Optional effective end date.
- Status: `DRAFT`, `SCHEDULED`, `ACTIVE`, `RETIRED`.
- Creator and approver.
- Change reason.

### 1.3 Policy resolution must be deterministic

At submission, the system should resolve exactly one effective policy for the employee, loan type, legal entity, and date. Ambiguous or overlapping active policy versions must be rejected.

### 1.4 Settings must be auditable

Every setting change should record:

- Actor.
- Timestamp.
- Previous and new values.
- Change reason.
- Effective date.
- Approval, where maker-checker control is enabled.

### 1.5 Financial and compliance controls must be safe by default

Self-approval prevention, audit logging, immutable financial transactions, and policy snapshotting should not be optional.

---

## 2. Layer 1 — Global loan settings

Layer 1 applies across the organization and should extend the current `LoanSettings` singleton.

## 2.1 Module availability and scope

Recommended settings:

- `loans_enabled` — enable or suspend new applications.
- `application_suspension_message` — employee-facing explanation when applications are disabled.
- `allowed_legal_entities` — optional organizational scope.
- `default_currency` — default contractual and reporting currency.
- `allow_multi_currency` — whether policies may use another currency.
- `employee_self_service_enabled` — allow employees to create applications.
- `hr_can_apply_on_behalf` — allow authorized HR staff to create an employee application with an audit reason.

**Expected behavior**

- Disabling applications must not stop repayment processing for active loans.
- HR-created applications must identify both the employee and submitting HR actor.

## 2.2 Global eligibility defaults

Recommended settings:

- `require_confirmed_employment`.
- `minimum_service_months`.
- `allowed_employment_types`.
- `minimum_appraisal_rating`.
- `appraisal_lookback_months`.
- `maximum_concurrent_active_loans`.
- `cooldown_months_after_settlement`.
- `block_when_in_notice_period`.
- `block_when_on_disciplinary_action`.
- `block_when_payroll_suspended`.

These values are defaults. A scheme may make eligibility stricter. Relaxing a mandatory global restriction should require an explicitly authorized policy override.

## 2.3 Global affordability controls

Recommended settings:

- `affordability_check_enabled`.
- `salary_basis`: `NET`, `GROSS`, or `BASIC`.
- `maximum_installment_percent`.
- `maximum_total_deduction_percent`.
- `minimum_residual_pay_amount`.
- `include_statutory_deductions`.
- `include_voluntary_deductions`.
- `include_existing_loan_deductions`.
- `salary_lookback_months`.
- `allow_affordability_override`.
- `affordability_override_roles`.
- `affordability_override_requires_reason`.

**Recommended calculation**

`available repayment capacity = allowed salary percentage - existing qualifying deductions`

The proposed installment must not exceed available capacity, and the remaining pay must not fall below the configured residual-pay threshold.

Every override should be visible in the approval history and exception reports.

## 2.4 Approval and separation-of-duties controls

Recommended settings:

- `require_line_manager_approval` — already present.
- `prevent_self_approval` — mandatory and always enabled.
- `maker_checker_policy_changes_enabled`.
- `require_approval_comment`.
- `require_rejection_comment`.
- `require_return_comment`.
- `approval_sla_hours`.
- `reminder_interval_hours`.
- `maximum_reminders`.
- `escalation_enabled`.
- `delegate_approval_enabled`.
- `approval_matrix_mode`: `GLOBAL`, `BY_SCHEME`, or `BY_AMOUNT`.
- `finance_verification_required_before_disbursement`.

The approval matrix itself should be stored as rows rather than JSON configuration. Each rule should identify:

- Applicable scheme or all schemes.
- Minimum and maximum amount.
- Sequence number.
- Required role or specific organizational relationship.
- Whether the stage is mandatory.
- Whether unanimous or one-of-many approval is required.

## 2.5 Disbursement controls

Recommended settings:

- `allowed_disbursement_methods`.
- `default_disbursement_method`.
- `require_payment_reference`.
- `require_finance_confirmation`.
- `allow_partial_disbursement`.
- `processing_fee_deduction_mode`: `UPFRONT`, `FINANCED`, or `NOT_APPLICABLE`.
- `disbursement_cutoff_day`.
- `minimum_days_between_approval_and_disbursement`.
- `employee_acceptance_required`.
- `acceptance_expiry_days`.

## 2.6 Repayment and payroll controls

Recommended settings:

- `payroll_integration_enabled`.
- `repayment_collection_method`: `PAYROLL`, `BANK_TRANSFER`, `MIXED`, or `MANUAL`.
- `repayment_start_rule`: `NEXT_PAYROLL`, `NEXT_MONTH`, or `AFTER_GRACE_PERIOD`.
- `default_grace_period_months`.
- `payroll_deduction_cutoff_day`.
- `arrears_carry_forward_enabled`.
- `partial_payment_enabled`.
- `overpayment_handling`: `REDUCE_TENURE`, `REDUCE_INSTALLMENT`, or `HOLD_AS_CREDIT`.
- `payment_allocation_order`, for example fees → penalties → interest → principal.
- `overdue_grace_days`.
- `automatic_overdue_marking_enabled`.
- `automatic_close_when_balance_zero`.
- `early_settlement_enabled`.
- `early_settlement_notice_days`.

## 2.7 Exit and exceptional-event controls

Recommended settings:

- `recover_on_resignation`.
- `recover_on_termination`.
- `final_entitlement_recovery_enabled`.
- `maximum_final_entitlement_deduction_percent`.
- `remaining_balance_action`: `DIRECT_PAYMENT`, `REPAYMENT_PLAN`, `WRITE_OFF_REVIEW`, or `LEGAL_RECOVERY`.
- `death_in_service_action`: `INSURANCE`, `WAIVER_REVIEW`, `ESTATE_RECOVERY`, or `POLICY_SPECIFIC`.
- `write_off_requires_approval`.
- `waiver_requires_approval`.
- `restructure_enabled`.
- `moratorium_enabled`.

Closing a loan must never silently erase a balance. The selected action should create settlement, waiver, recovery, or write-off transactions.

## 2.8 Notifications and visibility

The current observer department and unit settings should be retained and expanded with:

- `observer_department` — already present.
- `observer_unit` — already present.
- `observer_events`.
- `notify_employee_on_each_approval_stage`.
- `notify_on_overdue`.
- `overdue_reminder_interval_days`.
- `notify_manager_on_employee_default`.
- `notify_finance_on_approval`.
- `notify_payroll_on_disbursement`.
- `notification_channels`: email, in-app, SMS, or supported integrations.

Observer access should be read-only unless a separate approval or servicing role is assigned.

## 2.9 Reporting, compliance, and data controls

Recommended settings:

- `policy_exception_reporting_enabled`.
- `taxable_benefit_calculation_enabled`.
- `notional_interest_rate`.
- `data_retention_years`.
- `attachment_retention_years`.
- `mask_bank_details_for_non_finance_users`.
- `require_export_reason`.
- `large_export_approval_threshold`.
- `timezone`.
- `financial_year_start_month`.

Tax and statutory rules should be configurable by legal entity where the organization operates in more than one jurisdiction.

---

## 3. Layer 2 — Loan scheme policy

Layer 2 defines the terms for a particular loan type. `LoanType` should remain the stable scheme identity, while a separate versioned `LoanPolicy` stores effective terms.

## 3.1 Scheme identity and availability

Recommended fields:

- `loan_type`.
- `version`.
- `policy_code`.
- `display_name`.
- `employee_description`.
- `terms_and_conditions`.
- `status`.
- `effective_from`.
- `effective_to`.
- `application_open_from` and `application_open_to`.
- `is_employee_visible`.
- `priority`.

`LoanType` should also have an `is_active` flag so HR can retire a scheme without deleting historical records.

## 3.2 Amount limits

Recommended fields:

- `currency`.
- `minimum_amount`.
- `maximum_amount`.
- `maximum_salary_multiple`.
- `salary_multiple_basis`: basic, gross, or net.
- `maximum_amount_rule`: fixed cap, salary multiple, or lower of both.
- `minimum_increment`.
- `annual_employee_limit`.
- `lifetime_employee_limit`, where relevant.
- `scheme_portfolio_limit`.
- `monthly_disbursement_budget`.

The approved amount should never exceed the policy cap that was effective at submission unless a separately authorized exception is recorded.

## 3.3 Tenure and repayment timing

Recommended fields:

- `minimum_tenure_months`.
- `maximum_tenure_months`.
- `allowed_tenure_increment`.
- `grace_period_months`.
- `first_repayment_rule`.
- `repayment_frequency`: monthly, fortnightly, or supported payroll frequency.
- `repayment_day_rule`.
- `allow_balloon_payment`.
- `allow_employee_tenure_choice`.

These fields replace the current global hardcoded 1–12 month rule.

## 3.4 Interest settings

Recommended fields:

- `interest_method`: `NONE`, `FLAT`, or `REDUCING_BALANCE`.
- `annual_interest_rate`.
- `interest_compounding_frequency`.
- `day_count_convention`, if daily accrual is supported.
- `rounding_mode`.
- `rounding_precision`.
- `interest_rebate_on_early_settlement`.
- `subsidized_market_rate`.

For interest-free loans, `interest_method=NONE` and `annual_interest_rate=0` should be explicit.

## 3.5 Fees and penalties

Recommended fields:

- `processing_fee_type`: none, fixed, or percentage.
- `processing_fee_value`.
- `processing_fee_cap`.
- `processing_fee_treatment`: upfront deduction or financed.
- `late_fee_type`.
- `late_fee_value`.
- `late_fee_grace_days`.
- `early_settlement_fee_type`.
- `early_settlement_fee_value`.
- `allow_fee_waiver`.

Fees and penalties must be posted to the financial ledger and must not be hidden inside the principal balance.

## 3.6 Scheme-specific eligibility

Recommended fields:

- `minimum_service_months`.
- `allowed_employment_types`.
- `allowed_grades`.
- `allowed_departments`.
- `allowed_locations`.
- `minimum_appraisal_rating`.
- `minimum_age`.
- `maximum_age_at_maturity`.
- `maximum_concurrent_scheme_loans`.
- `allow_with_other_loan_types`.
- `cooldown_months`.
- `require_clean_repayment_history`.
- `maximum_prior_overdue_days`.

Scheme restrictions should make global rules stricter unless an authorized global override is explicitly supported.

## 3.7 Scheme-specific affordability

Recommended fields:

- `maximum_installment_percent`.
- `maximum_total_deduction_percent`.
- `minimum_residual_pay_amount`.
- `affordability_basis_override`.
- `allow_policy_exception`.
- `exception_approval_matrix`.

For example, a salary advance may use a stricter maximum installment percentage than a long-term housing loan.

## 3.8 Scheme-specific workflow

Recommended fields:

- `approval_matrix`.
- `finance_review_required`.
- `risk_review_required`.
- `committee_approval_required`.
- `employee_acceptance_required`.
- `guarantor_approval_required`.
- `required_approval_count`.
- `allow_auto_approval`.
- `auto_approval_maximum_amount`.

Workflow stages should be represented as related rule records, not hardcoded status dictionaries.

## 3.9 Documents, guarantors, and collateral

Recommended settings:

- `required_document_types`.
- `minimum_guarantor_count`.
- `guarantor_eligibility_rule`.
- `guarantor_maximum_exposure`.
- `collateral_required`.
- `allowed_collateral_types`.
- `valuation_required`.
- `employee_consent_required`.
- `payroll_deduction_authorization_required`.

Each document requirement should define whether it is required at draft, submission, approval, or disbursement.

## 3.10 Prepayment, restructuring, and settlement

Recommended fields:

- `partial_prepayment_allowed`.
- `minimum_prepayment_amount`.
- `prepayment_application_rule`: reduce tenure or reduce installment.
- `full_settlement_allowed`.
- `settlement_quote_valid_days`.
- `top_up_allowed`.
- `top_up_minimum_repaid_percent`.
- `restructure_allowed`.
- `maximum_restructures`.
- `moratorium_allowed`.
- `maximum_moratorium_months`.
- `write_off_rule`.

## 3.11 Tax and accounting treatment

Recommended fields:

- `taxable_benefit_applicable`.
- `notional_interest_rate_override`.
- `principal_gl_code`.
- `interest_income_gl_code`.
- `fee_income_gl_code`.
- `write_off_gl_code`.
- `cost_center_rule`.

Accounting codes are optional until general-ledger integration is introduced, but the data model should leave a clear extension point.

---

## 4. Recommended Django model structure

### Extend `LoanSettings`

Keep it as the organization-wide singleton initially, but group fields logically in serializers and the HR interface. If multi-entity support is expected, replace the singleton with one settings row per legal entity.

### Extend `LoanType`

Keep stable descriptive fields:

- `id`.
- `name`.
- `description`.
- `code`.
- `is_active`.
- `created_at`.
- `updated_at`.

Do not place mutable contractual values directly on `LoanType` if policy versioning is required.

### Add `LoanPolicy`

The model should include:

- Foreign key to `LoanType`.
- Version and lifecycle status.
- Effective dates.
- Amount, tenure, interest, fee, eligibility, affordability, repayment, and settlement fields.
- Created-by, approved-by, and change-reason fields.
- Database constraints preventing invalid ranges and overlapping active versions.

### Add related configuration models

Use related rows where a list or ordered structure is required:

- `LoanApprovalRule`.
- `LoanRequiredDocument`.
- `LoanPolicyEmploymentType`.
- `LoanPolicyGrade`.
- `LoanPolicyDepartment`.
- `LoanPolicyChangeLog`.

Avoid storing approval workflows or financial rules as unrestricted JSON because relational rows provide stronger validation, querying, and auditing.

### Snapshot policy terms on the application

Add to `LoanApplication`:

- `policy` foreign key.
- `policy_version`.
- `policy_snapshot` as a read-only serialized snapshot for legal and audit evidence.
- Approved principal, interest rate/method, fees, contractual total, tenure, and installment.

The relational policy reference supports reporting; the snapshot preserves exact contractual terms.

---

## 5. Validation and precedence rules

Recommended precedence:

1. Mandatory system controls.
2. Legal-entity global settings.
3. Effective loan policy.
4. Approved and audited exception.

Validation should occur at:

- Draft creation — basic field and scheme availability checks.
- Submission — complete eligibility, affordability, documents, and policy resolution.
- Each approval — policy still valid and no conflict-of-interest.
- Final approval — recompute approved terms.
- Disbursement — revalidate employment/payroll status and use the approved contractual snapshot.

After disbursement, policy changes must not alter the schedule unless a formal restructure creates amended contractual terms.

---

## 6. HR settings API strategy

Recommended endpoints:

- `GET/PATCH /api/v1/loan-settings/` — global settings.
- `GET/POST /api/v1/loan-types/` — list/create schemes.
- `GET/PATCH /api/v1/loan-types/{id}/` — edit descriptive fields or deactivate.
- `GET/POST /api/v1/loan-types/{id}/policies/` — list/create versions.
- `GET/PATCH /api/v1/loan-policies/{id}/` — edit only while `DRAFT`.
- `POST /api/v1/loan-policies/{id}/submit/`.
- `POST /api/v1/loan-policies/{id}/approve/`.
- `POST /api/v1/loan-policies/{id}/schedule/`.
- `POST /api/v1/loan-policies/{id}/retire/`.
- `GET /api/v1/loan-policy-change-logs/`.
- `POST /api/v1/loan-policies/{id}/preview/` — calculate sample eligibility and schedule without saving an application.

Permissions:

- Employees may read active employee-visible schemes and relevant terms.
- HR policy administrators may create and edit drafts.
- Authorized HR or management approvers may activate policies.
- Finance may review financial terms and accounting configuration.
- Auditors receive read-only access to settings and change history.

---

## 7. HR administration experience

The HR settings interface should provide:

- Global settings grouped into Eligibility, Affordability, Workflow, Repayment, Notifications, and Compliance.
- A scheme list showing active policy version and effective date.
- Draft, compare, preview, submit, approve, schedule, and retire actions.
- Side-by-side comparison between current and proposed versions.
- Warnings for policy overlap, invalid amount ranges, and incomplete approval matrices.
- A repayment-schedule preview using sample salary, amount, and tenure.
- Employee-impact preview showing who becomes eligible or ineligible.
- Read-only change history.

Dangerous settings should require confirmation and a change reason. Policy activation should optionally use maker-checker approval.

---

## 8. Implementation phases

## Phase 1 — Schema and current-behavior migration

- [ ] Add `code` and `is_active` to `LoanType`.
- [ ] Add versioned `LoanPolicy`.
- [ ] Add policy lifecycle and change-log models.
- [ ] Seed one policy per existing loan type.
- [ ] Configure seeded policies as interest-free, 1–12 months, and one active loan to preserve current behavior.
- [ ] Backfill existing applications with policy references and snapshots.

**Implementation strategy**

1. Introduce nullable policy references.
2. Seed and verify policy rows.
3. Backfill applications.
4. Add non-null constraints only after reconciliation.

## Phase 2 — Policy-driven validation and calculations

- [ ] Add one policy resolution service.
- [ ] Replace hardcoded tenure and concurrent-loan checks.
- [ ] Add amount, service, employment, and affordability validation.
- [ ] Add a pure interest and schedule calculation service.
- [ ] Snapshot contractual terms at submission or approval.
- [ ] Correct final-installment rounding.

## Phase 3 — HR policy management API

- [ ] Convert loan-type management to role-aware CRUD.
- [ ] Add policy draft, approval, activation, retirement, and preview endpoints.
- [ ] Enforce draft-only editing.
- [ ] Add maker-checker and change-reason controls.
- [ ] Add policy comparison and audit-log serializers.

## Phase 4 — Configurable approval matrix

- [ ] Add ordered approval rules.
- [ ] Resolve workflow by policy and approved amount.
- [ ] Preserve approval actors and decisions when a policy is superseded.
- [ ] Add delegation, reminders, SLA escalation, and conflict-of-interest validation.

## Phase 5 — Financial ledger and payroll settings

- [ ] Add disbursement, transaction, and settlement models.
- [ ] Add payroll deduction configuration and import/export.
- [ ] Support partial payments, arrears, reversals, prepayment, and settlement.
- [ ] Derive balances from transactions.

## Phase 6 — Advanced controls

- [ ] Add guarantors, collateral, and document requirements.
- [ ] Add tax/perquisite and accounting configuration.
- [ ] Add multi-entity and multi-currency policy scopes if needed.
- [ ] Add portfolio limits and budget controls.

---

## 9. Testing requirements

- [ ] Only authorized HR users can edit settings or draft policies.
- [ ] Active policies cannot be edited in place.
- [ ] Effective-date ranges cannot overlap for the same scope.
- [ ] Policy resolution returns exactly one policy.
- [ ] Historical loans retain original terms after a new policy activates.
- [ ] Global mandatory controls cannot be weakened by a scheme.
- [ ] Affordability calculations include configured deductions.
- [ ] Approval routing selects the correct amount band and scheme.
- [ ] Self-approval is blocked.
- [ ] Schedule totals reconcile exactly.
- [ ] Policy changes record actor, reason, before/after values, and effective date.
- [ ] Concurrent edits and activations are transaction-safe.

---

## 10. Minimum viable settings release

The first production-ready settings release should include:

### Layer 1 minimum

- Enable/disable applications.
- Confirmed-employment requirement.
- Minimum service.
- Maximum concurrent loans.
- Affordability basis and installment percentage.
- Self-approval prevention.
- Approval reminders and SLA.
- Observer department/unit.
- Payroll collection method.
- Overdue grace period.

### Layer 2 minimum

- Active flag and effective dates.
- Minimum/maximum amount.
- Salary-multiple cap.
- Minimum/maximum tenure.
- Interest method and rate.
- Processing fee.
- Scheme eligibility.
- Affordability override.
- Approval matrix.
- Grace period.
- Early settlement rule.
- Required documents.

This minimum set turns the existing module from a hardcoded workflow into an HR-manageable policy system while preserving a clear path to payroll, accounting, and advanced servicing.
