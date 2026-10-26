import uuid
from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# ---------------------------------------------------------------------------
# Appraisal Core Types & Templates
# ---------------------------------------------------------------------------

class AppraisalTypeCode(models.TextChoices):
    END_OF_YEAR = "END_OF_YEAR", "End of Year Appraisal"
    PROBATION_90_DAY = "PROBATION_90_DAY", "90-Day Performance Check"
    PROBATION_6_MONTH = "PROBATION_6_MONTH", "6-Month Confirmation Appraisal"
    BI_ANNUAL_JULY = "BI_ANNUAL_JULY", "Bi-Annual July Appraisal"
    BI_ANNUAL_DECEMBER = "BI_ANNUAL_DECEMBER", "Bi-Annual December Appraisal"


class AppraisalType(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=150)
    code = models.CharField(
        max_length=50,
        choices=AppraisalTypeCode.choices,
        unique=True,
        default=AppraisalTypeCode.END_OF_YEAR,
    )
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Appraisal Type"
        verbose_name_plural = "Appraisal Types"
        ordering = ["name"]

    def __str__(self):
        return self.name


class AppraisalCycleStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    ACTIVE = "ACTIVE", "Active"
    CLOSED = "CLOSED", "Closed"
    ARCHIVED = "ARCHIVED", "Archived"


class AppraisalCycle(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    appraisal_type = models.ForeignKey(
        AppraisalType,
        on_delete=models.PROTECT,
        related_name="cycles",
    )
    start_date = models.DateField()
    end_date = models.DateField()
    submission_deadline = models.DateField()
    status = models.CharField(
        max_length=20,
        choices=AppraisalCycleStatus.choices,
        default=AppraisalCycleStatus.DRAFT,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_appraisal_cycles",
    )

    class Meta:
        verbose_name = "Appraisal Cycle"
        verbose_name_plural = "Appraisal Cycles"
        ordering = ["-start_date"]

    def __str__(self):
        return f"{self.name} ({self.get_status_display()})"


class AppraisalTemplate(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    appraisal_type = models.ForeignKey(
        AppraisalType,
        on_delete=models.PROTECT,
        related_name="templates",
    )
    title = models.CharField(max_length=200)
    department = models.ForeignKey(
        "accounts.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="appraisal_templates",
        help_text="Leave empty for organization-wide default template",
    )
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Appraisal Template"
        verbose_name_plural = "Appraisal Templates"

    def __str__(self):
        dept_str = f" - {self.department.name}" if self.department else " (General)"
        return f"{self.title}{dept_str}"


class AppraisalSection(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    template = models.ForeignKey(
        AppraisalTemplate,
        on_delete=models.CASCADE,
        related_name="sections",
    )
    title = models.CharField(max_length=255)
    section_code = models.CharField(max_length=50, blank=True)
    weight = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        help_text="Percentage weight of this section in total score, e.g. 15.00 for 15%",
        validators=[MinValueValidator(Decimal("0.00")), MaxValueValidator(Decimal("100.00"))],
    )
    rating_scale_max = models.PositiveSmallIntegerField(
        default=5,
        validators=[MinValueValidator(1), MaxValueValidator(10)],
        help_text="Maximum Likert rating scale value for questions in this section (e.g. 5 or 4)",
    )
    order = models.PositiveIntegerField(default=1)

    class Meta:
        verbose_name = "Appraisal Section"
        verbose_name_plural = "Appraisal Sections"
        ordering = ["order", "created_at"]

    def __str__(self):
        return f"{self.title} ({self.weight}%)"


class AppraisalQuestion(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    section = models.ForeignKey(
        AppraisalSection,
        on_delete=models.CASCADE,
        related_name="questions",
    )
    prompt = models.TextField()
    description = models.TextField(blank=True)
    order = models.PositiveIntegerField(default=1)

    class Meta:
        verbose_name = "Appraisal Question"
        verbose_name_plural = "Appraisal Questions"
        ordering = ["order", "created_at"]

    def __str__(self):
        return f"{self.section.section_code} Q{self.order}: {self.prompt[:60]}"


# ---------------------------------------------------------------------------
# Employee Appraisal Instance & Workflow
# ---------------------------------------------------------------------------

class EmployeeAppraisalStatus(models.TextChoices):
    NOT_STARTED = "NOT_STARTED", "Not Started"
    DRAFT = "DRAFT", "Draft"
    SELF_SUBMITTED = "SELF_SUBMITTED", "Self Submitted"
    PENDING_APPRAISEE_ACCEPTANCE = "PENDING_APPRAISEE_ACCEPTANCE", "Pending Appraisee Acceptance"
    PENDING_HR_CALIBRATION = "PENDING_HR_CALIBRATION", "Pending HR Calibration"
    PENDING_MANAGEMENT_DECISION = "PENDING_MANAGEMENT_DECISION", "Pending Management Decision"
    FINALIZED = "FINALIZED", "Finalized"
    REOPENED_FOR_EMPLOYEE = "REOPENED_FOR_EMPLOYEE", "Reopened for Employee"
    REOPENED_FOR_SUPERVISOR = "REOPENED_FOR_SUPERVISOR", "Reopened for Supervisor"


class PerformanceCategory(models.TextChoices):
    EXCEPTIONAL = "EXCEPTIONAL", "Exceptional Performer (90 - 100)"
    HIGH = "HIGH", "High Performer (80 - 89)"
    GOOD = "GOOD", "Good Performer (65 - 79)"
    DEVELOPING = "DEVELOPING", "Developing Performer (50 - 64)"
    UNDER = "UNDER", "Under Performer (below 50)"


class EmployeeAppraisal(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="appraisals",
    )
    supervisor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="subordinate_appraisals",
    )
    cycle = models.ForeignKey(
        AppraisalCycle,
        on_delete=models.PROTECT,
        related_name="employee_appraisals",
    )
    template = models.ForeignKey(
        AppraisalTemplate,
        on_delete=models.PROTECT,
        related_name="employee_appraisals",
    )
    status = models.CharField(
        max_length=35,
        choices=EmployeeAppraisalStatus.choices,
        default=EmployeeAppraisalStatus.DRAFT,
    )
    self_score_total = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    supervisor_score_total = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    performance_category = models.CharField(
        max_length=30,
        choices=PerformanceCategory.choices,
        null=True,
        blank=True,
    )
    
    # Workflow completion timestamps
    self_submitted_at = models.DateTimeField(null=True, blank=True)
    supervisor_submitted_at = models.DateTimeField(null=True, blank=True)
    appraisee_signed_at = models.DateTimeField(null=True, blank=True)
    hr_reviewed_at = models.DateTimeField(null=True, blank=True)
    finalized_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Employee Appraisal"
        verbose_name_plural = "Employee Appraisals"
        unique_together = ("employee", "cycle")
        ordering = ["-created_at"]

    def __str__(self):
        return f"Appraisal: {self.employee.get_full_name()} ({self.cycle.name})"


# ---------------------------------------------------------------------------
# Phase 1: Employee Self-Assessment
# ---------------------------------------------------------------------------

class AppraisalSelfPhase1(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    appraisal = models.OneToOneField(
        EmployeeAppraisal,
        on_delete=models.CASCADE,
        related_name="phase1_self",
    )
    key_achievements = models.TextField(
        blank=True,
        help_text="Key Achievements based on Goals set in Previous Appraisal",
    )
    noteworthy_achievements = models.TextField(
        blank=True,
        help_text="Other noteworthy achievements",
    )
    challenges_encountered = models.TextField(
        blank=True,
        help_text="Challenges encountered",
    )
    support_needed = models.TextField(
        blank=True,
        help_text="Support needed from both your Manager and Management",
    )

    class Meta:
        verbose_name = "Phase 1 Self-Assessment"


# ---------------------------------------------------------------------------
# Phase 2: Quantitative Answers (Self & Supervisor)
# ---------------------------------------------------------------------------

class AppraisalQuestionAnswer(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    appraisal = models.ForeignKey(
        EmployeeAppraisal,
        on_delete=models.CASCADE,
        related_name="answers",
    )
    question = models.ForeignKey(
        AppraisalQuestion,
        on_delete=models.PROTECT,
        related_name="answers",
    )
    self_rating = models.PositiveSmallIntegerField(null=True, blank=True)
    supervisor_rating = models.PositiveSmallIntegerField(null=True, blank=True)
    comments = models.TextField(blank=True)

    class Meta:
        verbose_name = "Appraisal Question Answer"
        verbose_name_plural = "Appraisal Question Answers"
        unique_together = ("appraisal", "question")


# ---------------------------------------------------------------------------
# Phase 3: Supervisor Assessment
# ---------------------------------------------------------------------------

class AppraisalSupervisorPhase3(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    appraisal = models.OneToOneField(
        EmployeeAppraisal,
        on_delete=models.CASCADE,
        related_name="phase3_supervisor",
    )
    strengths = models.TextField(
        blank=True,
        help_text="Key strengths identified by supervisor",
    )
    areas_for_development = models.TextField(
        blank=True,
        help_text="Specific areas needing improvement",
    )
    training_development_needs = models.TextField(
        blank=True,
        help_text="Specific training, certifications, or development initiatives",
    )

    class Meta:
        verbose_name = "Phase 3 Supervisor Assessment"


# ---------------------------------------------------------------------------
# Phase 4: 1-on-1 Review, Goals, Outcome & Sign-Off
# ---------------------------------------------------------------------------

class OutcomeRecommendationChoice(models.TextChoices):
    COMMENDATION_LETTER = "COMMENDATION_LETTER", "Letter of Commendation"
    SPECIALIZED_TRAINING = "SPECIALIZED_TRAINING", "Access to specialized Trainings"
    BONUS_GIFTS = "BONUS_GIFTS", "Bonus/ Gifts"
    SPECIAL_RECOGNITION = "SPECIAL_RECOGNITION", "Nomination for Special Recognition"
    CONSIDERED_FUTURE_PROMOTION = "CONSIDERED_FUTURE_PROMOTION", "Considered for Future Promotion"
    PROMOTION_NEXT_GRADE = "PROMOTION_NEXT_GRADE", "Promotion to next Grade / Role Expansion"
    PROMOTION_SUPERVISORY = "PROMOTION_SUPERVISORY", "Promotion to Supervisory Role"
    PERFORMANCE_MEMO = "PERFORMANCE_MEMO", "Performance Memo"
    TAILORED_TRAINING = "TAILORED_TRAINING", "Tailored Training/ Mentorship/ Coaching"
    WARNING_LETTER = "WARNING_LETTER", "Performance Warning Letter"
    PIP_PROBATION = "PIP_PROBATION", "Performance Improvement Plan (Probation)"
    ROLE_TRANSFER = "ROLE_TRANSFER", "Role Transfer"
    DEMOTION = "DEMOTION", "Demotion"
    TERMINATION = "TERMINATION", "Termination"
    OTHERS = "OTHERS", "Others (Kindly suggest)"


class AppraisalPhase4Signoff(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    appraisal = models.OneToOneField(
        EmployeeAppraisal,
        on_delete=models.CASCADE,
        related_name="phase4_signoff",
    )
    goals_next_year = models.TextField(
        blank=True,
        help_text="Specific Targets or focus areas for next year",
    )
    recommendations = models.JSONField(
        default=list,
        blank=True,
        help_text="List of selected outcome recommendation codes",
    )
    recommendation_others = models.TextField(blank=True)
    supervisor_comments = models.TextField(blank=True)
    
    # Appraisee Acceptance
    appraisee_agrees = models.BooleanField(null=True, blank=True)
    appraisee_disagree_reason = models.TextField(
        blank=True,
        help_text="Compulsory if appraisee disagrees with line manager rating",
    )
    appraisee_additional_comments = models.TextField(blank=True)
    
    # HR & Management / Committee Review
    hr_observations = models.TextField(
        blank=True,
        help_text="Key observations (recurring disciplinary cases, achievements not mentioned)",
    )
    hr_recommended_action = models.TextField(blank=True)
    management_comments = models.TextField(blank=True)
    committee_decision = models.TextField(blank=True)

    class Meta:
        verbose_name = "Phase 4 1-on-1 Sign-off & Recommendations"


# ---------------------------------------------------------------------------
# Audit Trail Log
# ---------------------------------------------------------------------------

class AppraisalAuditLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    appraisal = models.ForeignKey(
        EmployeeAppraisal,
        on_delete=models.CASCADE,
        related_name="audit_logs",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
    )
    action = models.CharField(max_length=100)
    previous_status = models.CharField(max_length=50, blank=True)
    new_status = models.CharField(max_length=50, blank=True)
    comments = models.TextField(blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Appraisal Audit Log"
        verbose_name_plural = "Appraisal Audit Logs"
        ordering = ["-timestamp"]
