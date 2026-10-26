# Staff Appraisal Module — Frontend Integration Guide (Next.js)

Base Endpoint Prefix: **`/api/v1/`**  
Authentication: **`Authorization: Bearer <access_token>`**  
Target Framework: **Next.js (App Router / Pages Router)**  

---

## 1. Overview & Information Architecture (IA)

The Staff Appraisal Module supports a multi-stage performance evaluation workflow. The initial release focuses on the **End of Year Appraisal**, following a **4-Phase Evaluation Structure** that applies universally across departments, while allowing department-specific questionnaires and weighted scoring.

### Suggested Pages & Navigation Structure

| Page Route | Page Purpose | Target Roles | Key Actions |
| :--- | :--- | :--- | :--- |
| `/dashboard/appraisals/me` | My Appraisals | All Employees | View past & active appraisals, launch current self-assessment, complete sign-off |
| `/dashboard/appraisals/team` | Team Appraisals | Line Managers / Supervisors | Review direct report self-assessments, fill supervisor ratings (Phase 2 & 3), set next year goals & recommendations |
| `/dashboard/appraisals/all` | All Appraisals & Calibration | HR Admin, Management, Committee | Monitor completion, perform HR calibration, enter management final decisions, export reports |
| `/dashboard/appraisals/[id]` | Appraisal Interactive Form | All Roles (role-aware) | 4-Phase Stepper/Tab form view & editing |
| `/dashboard/appraisals/admin/cycles` | Appraisal Cycle Manager | HR Admin | Launch new annual/probation cycles, set deadlines, auto-assign appraisals |
| `/dashboard/appraisals/admin/templates` | Template & Question Builder | HR Admin | Configure department templates, sections, weights %, and Likert rating questions |

---

## 2. Universal 4-Phase Appraisal Form Layout

When rendering `/dashboard/appraisals/[id]`, the UI should present a **Tabbed or Stepper Interface** corresponding to the 4 phases:

```
[ Phase 1: Self-Assessment ] ➔ [ Phase 2: Performance Ratings ] ➔ [ Phase 3: Supervisor Evaluation ] ➔ [ Phase 4: 1-on-1 & Sign-off ]
```

### Phase Breakdown & Field Mappings

#### Phase 1: Employee Self-Assessment (Qualitative Text)
- **Fields**:
  - `key_achievements` (Textarea): Key achievements based on previous annual goals.
  - `noteworthy_achievements` (Textarea): Other noteworthy accomplishments.
  - `challenges_encountered` (Textarea): Obstacles and challenges met.
  - `support_needed` (Textarea): Support needed from manager and management.
- **Primary Actor**: Employee (Appraisee).
- **Active States**: `DRAFT`, `NOT_STARTED`, `REOPENED_FOR_EMPLOYEE`.

#### Phase 2: Job Performance Assessment (Quantitative Scoring)
- **UI Structure**: Accordion or Section Cards per Section (e.g., *SECTION A: SALES STRATEGY & BUSINESS DEVELOPMENT - 15%*).
- **Questions**: Each question renders two input columns:
  1. **Self Rating (1–5 or 1–4)**: Editable by Employee during Phase 1 & 2.
  2. **Supervisor Rating (1–5 or 1–4)**: Editable by Line Manager during Phase 2 & 3.
  3. **Comments (Optional per question)**: Rationale for rating given.
- **Real-time Live Calculated Display**:
  - **Section Weighted Score**: $\text{Section Score} = \left( \frac{\text{Sum of Ratings}}{\text{Questions Count} \times \text{Scale Max}} \right) \times \text{Section Weight \%}$
  - **Grand Total Score**: Sum of all section weighted scores.

#### Phase 3: Supervisor Assessment (Qualitative Strengths & Training)
- **Fields**:
  - `strengths` (Textarea): Key employee strengths.
  - `areas_for_development` (Textarea): Specific areas needing improvement.
  - `training_development_needs` (Textarea): Recommended training, certifications, or coaching.
- **Primary Actor**: Supervisor / Line Manager.
- **Active States**: `SELF_SUBMITTED`, `REOPENED_FOR_SUPERVISOR`.

#### Phase 4: 1-on-1 Review, Goals, Outcome & Sign-Offs
- **1-on-1 & Goals**:
  - `goals_next_year` (Textarea): Focus areas and targets for next year.
  - `recommendations` (Multi-select Checkboxes): Outcome recommendation choices (e.g. `COMMENDATION_LETTER`, `BONUS_GIFTS`, `PROMOTION_NEXT_GRADE`, `WARNING_LETTER`, `PIP_PROBATION`, etc.).
  - `recommendation_others` (Text): Free-text suggestion if "Others" selected.
  - `supervisor_comments` (Textarea): General line manager comments.
- **Auto Performance Tier Display**:
  - Automatically mapped from Supervisor Score:
    - **90.00% – 100.00%**: `Exceptional Performer` (Badge: Emerald/Green)
    - **80.00% – 89.99%**: `High Performer` (Badge: Blue)
    - **65.00% – 79.99%**: `Good Performer` (Badge: Sky/Cyan)
    - **50.00% – 64.99%**: `Developing Performer` (Badge: Amber/Yellow)
    - **Below 50.00%**: `Under Performer` (Badge: Red)
- **Appraisee Acceptance Sign-Off**:
  - `appraisee_agrees` (Radio / Switch): `True` (I agree) or `False` (I disagree).
  - `appraisee_disagree_reason` (Textarea): **Mandatory if `appraisee_agrees === false`**.
  - `appraisee_additional_comments` (Textarea): Optional additional comments.
- **HR Review & Calibration**:
  - `hr_observations` (Textarea): Recurring disciplinary cases, notable achievements.
  - `hr_recommended_action` (Textarea): HR action items.
- **Management Review & Appraisal Committee Decision**:
  - `management_comments` (Textarea): Management remarks.
  - `committee_decision` (Textarea): Official final decision.

---

## 3. Workflow State Machine & Action Buttons Matrix

| Status String | UI Status Badge Label | Badge Style | Primary Action Button | Target Role | Next Endpoint Call |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `DRAFT` / `NOT_STARTED` | Self Assessment Draft | Gray / Slate | **Submit Self Assessment** | Employee | `POST /appraisals/{id}/self-submit/` |
| `SELF_SUBMITTED` | Pending Manager Review | Orange / Amber | **Submit Manager Assessment** | Supervisor | `POST /appraisals/{id}/supervisor-submit/` |
| `PENDING_APPRAISEE_ACCEPTANCE` | Pending Sign-Off | Purple / Indigo | **Complete Sign-Off** | Employee | `POST /appraisals/{id}/appraisee-signoff/` |
| `PENDING_HR_CALIBRATION` | Pending HR Calibration | Blue / Info | **Submit HR Calibration** | HR Admin | `POST /appraisals/{id}/hr-calibrate/` |
| `PENDING_MANAGEMENT_DECISION` | Pending Committee Decision | Teal / Cyan | **Finalize Appraisal** | Management | `POST /appraisals/{id}/finalize/` |
| `FINALIZED` | Finalized & Completed | Green / Success | *(Read-only)* | All | N/A |
| `REOPENED_FOR_EMPLOYEE` | Reopened for Employee | Red / Warning | **Resubmit Self Assessment** | Employee | `POST /appraisals/{id}/self-submit/` |
| `REOPENED_FOR_SUPERVISOR` | Reopened for Manager | Red / Warning | **Resubmit Manager Assessment** | Supervisor | `POST /appraisals/{id}/supervisor-submit/` |

> **Note for HR Administrators**: HR Admins see an explicit **"Reopen Appraisal"** button on any appraisal in progress to trigger `POST /appraisals/{id}/reopen/`.

---

## 4. REST API Endpoint Reference & Request Payloads

### A. Appraisal Cycle & Template Management (HR Admin)

#### 1. List / Create Appraisal Cycles
- **`GET /api/v1/appraisal-cycles/`**
- **`POST /api/v1/appraisal-cycles/`**
  ```json
  {
    "name": "2026 End of Year Appraisal Cycle",
    "appraisal_type": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "start_date": "2026-11-01",
    "end_date": "2026-12-31",
    "submission_deadline": "2026-12-15"
  }
  ```

#### 2. Activate Cycle & Auto-Assign Appraisals
- **`POST /api/v1/appraisal-cycles/{id}/activate/`**
  - Instantiates `EmployeeAppraisal` for all active employees based on their department template.
  - **Response**: `{ "detail": "Appraisal cycle activated successfully. 45 employee appraisals created.", "spawned_count": 45 }`

#### 3. Templates & Question Builder
- **`GET /api/v1/appraisal-templates/`**
- **`GET /api/v1/appraisal-types/`**

---

### B. Employee & Manager Appraisal Workflows

#### 1. List Appraisals
- **`GET /api/v1/appraisals/my-appraisals/`**: Logged-in employee's appraisals.
- **`GET /api/v1/appraisals/team-appraisals/`**: Appraisals of logged-in manager's direct reports.
- **`GET /api/v1/appraisals/`**: All appraisals (HR & Executive Management view).

#### 2. Retrieve Complete Appraisal Detail
- **`GET /api/v1/appraisals/{id}/`**
  - Returns full nested JSON including employee info, supervisor info, template sections, questions, answered ratings, Phase 1 self text, Phase 3 supervisor text, Phase 4 sign-off, total calculated scores, and performance tier code.

#### 3. Submit Employee Self-Assessment (Phase 1 & Phase 2)
- **`POST /api/v1/appraisals/{id}/self-submit/`**
  ```json
  {
    "key_achievements": "Achieved 120% of annual sales target.",
    "noteworthy_achievements": "Spearheaded key enterprise deal closing.",
    "challenges_encountered": "Lead generation delays in Q2.",
    "support_needed": "Dedicated SDR resource for outbound leads.",
    "ratings": [
      {
        "question_id": "c1a2b3c4-0000-0000-0000-000000000001",
        "self_rating": 4,
        "comments": "Consistently executed sales strategy"
      },
      {
        "question_id": "c1a2b3c4-0000-0000-0000-000000000002",
        "self_rating": 5,
        "comments": "Exceeded quarterly quota"
      }
    ],
    "is_draft": false
  }
  ```
  *Set `"is_draft": true` to save progress without advancing status.*

#### 4. Submit Supervisor Assessment (Phase 2 & Phase 3)
- **`POST /api/v1/appraisals/{id}/supervisor-submit/`**
  ```json
  {
    "ratings": [
      {
        "question_id": "c1a2b3c4-0000-0000-0000-000000000001",
        "supervisor_rating": 5,
        "comments": "Strong execution"
      }
    ],
    "strengths": "Outstanding deal closing and negotiation capabilities.",
    "areas_for_development": "Time management on CRM documentation.",
    "training_development_needs": "Advanced Enterprise Negotiation Masterclass.",
    "goals_next_year": "Expand enterprise market revenue by 25%.",
    "recommendations": ["COMMENDATION_LETTER", "BONUS_GIFTS"],
    "recommendation_others": "",
    "supervisor_comments": "Exceptional contribution to company revenue.",
    "is_draft": false
  }
  ```

#### 5. Employee Sign-Off (Phase 4)
- **`POST /api/v1/appraisals/{id}/appraisee-signoff/`**
  ```json
  {
    "appraisee_agrees": true,
    "appraisee_disagree_reason": "",
    "appraisee_additional_comments": "I agree with my line manager's review and appreciate the support."
  }
  ```
  *If `appraisee_agrees` is `false`, `appraisee_disagree_reason` is **required**.*

#### 6. HR Calibration
- **`POST /api/v1/appraisals/{id}/hr-calibrate/`**
  ```json
  {
    "hr_observations": "Clean disciplinary record. Top 5% performance in Sales department.",
    "hr_recommended_action": "Approved for performance bonus and commendation letter."
  }
  ```

#### 7. Executive Management & Committee Finalize
- **`POST /api/v1/appraisals/{id}/finalize/`**
  ```json
  {
    "management_comments": "Approved by Management Board.",
    "committee_decision": "Awarded Exceptional Performer Bonus & Salary Grade Increment."
  }
  ```

#### 8. HR Reopen Override
- **`POST /api/v1/appraisals/{id}/reopen/`**
  ```json
  {
    "target_stage": "REOPENED_FOR_EMPLOYEE",
    "reason": "Missing key achievement details in Phase 1 self-assessment."
  }
  ```
  *(Options for `target_stage`: `"REOPENED_FOR_EMPLOYEE"` or `"REOPENED_FOR_SUPERVISOR"`)*

#### 9. View Audit History Log
- **`GET /api/v1/appraisals/{id}/audit-logs/`**

---

## 5. TypeScript Types & Interface Definitions

Copy these definitions directly into your Next.js project (e.g. `@/types/appraisal.ts`):

```typescript
export type AppraisalStatusCode =
  | 'NOT_STARTED'
  | 'DRAFT'
  | 'SELF_SUBMITTED'
  | 'PENDING_APPRAISEE_ACCEPTANCE'
  | 'PENDING_HR_CALIBRATION'
  | 'PENDING_MANAGEMENT_DECISION'
  | 'FINALIZED'
  | 'REOPENED_FOR_EMPLOYEE'
  | 'REOPENED_FOR_SUPERVISOR';

export type PerformanceCategoryCode =
  | 'EXCEPTIONAL'
  | 'HIGH'
  | 'GOOD'
  | 'DEVELOPING'
  | 'UNDER';

export type RecommendationCode =
  | 'COMMENDATION_LETTER'
  | 'SPECIALIZED_TRAINING'
  | 'BONUS_GIFTS'
  | 'SPECIAL_RECOGNITION'
  | 'CONSIDERED_FUTURE_PROMOTION'
  | 'PROMOTION_NEXT_GRADE'
  | 'PROMOTION_SUPERVISORY'
  | 'PERFORMANCE_MEMO'
  | 'TAILORED_TRAINING'
  | 'WARNING_LETTER'
  | 'PIP_PROBATION'
  | 'ROLE_TRANSFER'
  | 'DEMOTION'
  | 'TERMINATION'
  | 'OTHERS';

export interface AppraisalQuestion {
  id: string;
  section: string;
  prompt: string;
  description: string;
  order: number;
}

export interface AppraisalSection {
  id: string;
  template: string;
  title: string;
  section_code: string;
  weight: string; // e.g. "15.00"
  rating_scale_max: number; // e.g. 5 or 4
  order: number;
  questions: AppraisalQuestion[];
}

export interface AppraisalTemplate {
  id: string;
  title: string;
  department_name?: string;
  appraisal_type_name: string;
  description: string;
  sections: AppraisalSection[];
}

export interface AppraisalQuestionAnswer {
  id: string;
  question: string;
  question_prompt: string;
  section_id: string;
  section_code: string;
  self_rating: number | null;
  supervisor_rating: number | null;
  comments: string;
}

export interface AppraisalSelfPhase1 {
  id: string;
  key_achievements: string;
  noteworthy_achievements: string;
  challenges_encountered: string;
  support_needed: string;
}

export interface AppraisalSupervisorPhase3 {
  id: string;
  strengths: string;
  areas_for_development: string;
  training_development_needs: string;
}

export interface AppraisalPhase4Signoff {
  id: string;
  goals_next_year: string;
  recommendations: RecommendationCode[];
  recommendation_others: string;
  supervisor_comments: string;
  appraisee_agrees: boolean | null;
  appraisee_disagree_reason: string;
  appraisee_additional_comments: string;
  hr_observations: string;
  hr_recommended_action: string;
  management_comments: string;
  committee_decision: string;
}

export interface EmployeeAppraisalDetail {
  id: string;
  employee: {
    id: string;
    email: string;
    first_name: string;
    last_name: string;
  };
  supervisor?: {
    id: string;
    email: string;
    first_name: string;
    last_name: string;
  };
  cycle: {
    id: string;
    name: string;
    submission_deadline: string;
  };
  template: AppraisalTemplate;
  status: AppraisalStatusCode;
  status_display: string;
  self_score_total: string; // Decimal string e.g. "90.00"
  supervisor_score_total: string; // Decimal string e.g. "90.00"
  performance_category: PerformanceCategoryCode | null;
  phase1_self: AppraisalSelfPhase1 | null;
  answers: AppraisalQuestionAnswer[];
  phase3_supervisor: AppraisalSupervisorPhase3 | null;
  phase4_signoff: AppraisalPhase4Signoff | null;
  self_submitted_at: string | null;
  supervisor_submitted_at: string | null;
  appraisee_signed_at: string | null;
  hr_reviewed_at: string | null;
  finalized_at: string | null;
  created_at: string;
}
```

---

## 6. Frontend Developer Checklist

- [ ] Create route `/dashboard/appraisals/me` with table of user's active & past appraisals.
- [ ] Create route `/dashboard/appraisals/team` for supervisors to view subordinates' pending reviews.
- [ ] Build `/dashboard/appraisals/[id]` with 4-phase tabbed/stepper form.
- [ ] Implement live score calculation in Phase 2 rating table using section weights and question counts.
- [ ] Enforce mandatory validation on `appraisee_disagree_reason` when employee selects "I Disagree" during Phase 4 sign-off.
- [ ] Add status badge coloring: `DRAFT` (Gray), `SELF_SUBMITTED` (Amber), `PENDING_APPRAISEE_ACCEPTANCE` (Purple), `PENDING_HR_CALIBRATION` (Blue), `PENDING_MANAGEMENT_DECISION` (Cyan), `FINALIZED` (Green), `REOPENED` (Red).
- [ ] Connect HR cycle management pages `/dashboard/appraisals/admin/cycles`.
