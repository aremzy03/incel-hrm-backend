from rest_framework import serializers

from apps.accounts.models import Department, Team, Unit
from apps.accounts.serializers import UserSerializer

from .models import (
    AppraisalAuditLog,
    AppraisalCycle,
    AppraisalPhase,
    AppraisalPhase4Signoff,
    AppraisalPhaseSubmission,
    AppraisalQuestion,
    AppraisalQuestionAnswer,
    AppraisalSection,
    AppraisalSelfPhase1,
    AppraisalSupervisorPhase3,
    AppraisalTemplate,
    AppraisalTemplateVersion,
    AppraisalType,
    EmployeeAppraisal,
    OutcomeRecommendationChoice,
    QuestionResponseType,
    SectionAudience,
    SectionKind,
    TemplateScope,
    TemplateVersionStatus,
)
from .services import score_is_provisional


class AppraisalTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = AppraisalType
        fields = ["id", "name", "code", "description", "is_active", "created_at"]


class AppraisalCycleSerializer(serializers.ModelSerializer):
    appraisal_type_detail = AppraisalTypeSerializer(source="appraisal_type", read_only=True)

    class Meta:
        model = AppraisalCycle
        fields = [
            "id",
            "name",
            "appraisal_type",
            "appraisal_type_detail",
            "start_date",
            "end_date",
            "submission_deadline",
            "status",
            "created_by",
            "created_at",
        ]
        read_only_fields = ["created_by", "created_at"]


class AppraisalQuestionSerializer(serializers.ModelSerializer):
    class Meta:
        model = AppraisalQuestion
        fields = [
            "id",
            "section",
            "prompt",
            "description",
            "order",
            "response_type",
            "counts_toward_score",
            "required",
            "weight",
            "options",
        ]


class AppraisalSectionSerializer(serializers.ModelSerializer):
    questions = AppraisalQuestionSerializer(many=True, read_only=True)

    class Meta:
        model = AppraisalSection
        fields = [
            "id",
            "version",
            "title",
            "section_code",
            "kind",
            "audience",
            "weight",
            "rating_scale_max",
            "order",
            "questions",
        ]


class AppraisalPhaseSerializer(serializers.ModelSerializer):
    sections = AppraisalSectionSerializer(many=True, read_only=True)

    class Meta:
        model = AppraisalPhase
        fields = ["id", "title", "order", "sections"]


class AppraisalTemplateVersionSerializer(serializers.ModelSerializer):
    phases = AppraisalPhaseSerializer(many=True, read_only=True)
    sections = AppraisalSectionSerializer(many=True, read_only=True)

    class Meta:
        model = AppraisalTemplateVersion
        fields = [
            "id",
            "version_number",
            "status",
            "published_at",
            "phases",
            "sections",
        ]


def _display_version(template: AppraisalTemplate):
    return (
        template.versions.filter(status=TemplateVersionStatus.PUBLISHED).first()
        or template.versions.filter(status=TemplateVersionStatus.DRAFT).first()
    )


class AppraisalTemplateSerializer(serializers.ModelSerializer):
    sections = serializers.SerializerMethodField()
    department_name = serializers.CharField(source="department.name", read_only=True, allow_null=True)
    unit_name = serializers.CharField(source="unit.name", read_only=True, allow_null=True)
    team_name = serializers.CharField(source="team.name", read_only=True, allow_null=True)
    appraisal_type_name = serializers.CharField(source="appraisal_type.name", read_only=True)
    assignment_locked = serializers.BooleanField(read_only=True)
    draft = serializers.SerializerMethodField()
    published = serializers.SerializerMethodField()

    class Meta:
        model = AppraisalTemplate
        fields = [
            "id",
            "appraisal_type",
            "appraisal_type_name",
            "title",
            "description",
            "is_active",
            "scope",
            "department",
            "department_name",
            "unit",
            "unit_name",
            "team",
            "team_name",
            "assignment_locked",
            "sections",
            "draft",
            "published",
            "created_at",
            "updated_at",
        ]

    def get_sections(self, obj):
        pinned = self.context.get("template_version")
        version = pinned if pinned is not None and pinned.template_id == obj.id else _display_version(obj)
        if version is None:
            return []
        return AppraisalSectionSerializer(version.sections.all(), many=True).data

    def get_draft(self, obj):
        draft = obj.versions.filter(status=TemplateVersionStatus.DRAFT).first()
        if draft is None:
            return None
        return AppraisalTemplateVersionSerializer(draft).data

    def get_published(self, obj):
        published = obj.versions.filter(status=TemplateVersionStatus.PUBLISHED).first()
        if published is None:
            return None
        return AppraisalTemplateVersionSerializer(published).data


class AppraisalTemplateListSerializer(serializers.ModelSerializer):
    appraisal_type_name = serializers.CharField(source="appraisal_type.name", read_only=True)
    department_name = serializers.CharField(source="department.name", read_only=True, allow_null=True)
    unit_name = serializers.CharField(source="unit.name", read_only=True, allow_null=True)
    team_name = serializers.CharField(source="team.name", read_only=True, allow_null=True)
    has_draft = serializers.SerializerMethodField()
    published_version_number = serializers.SerializerMethodField()

    class Meta:
        model = AppraisalTemplate
        fields = [
            "id",
            "title",
            "description",
            "appraisal_type",
            "appraisal_type_name",
            "scope",
            "department",
            "department_name",
            "unit",
            "unit_name",
            "team",
            "team_name",
            "is_active",
            "has_draft",
            "published_version_number",
            "updated_at",
        ]

    def get_has_draft(self, obj):
        return obj.versions.filter(status=TemplateVersionStatus.DRAFT).exists()

    def get_published_version_number(self, obj):
        published = obj.versions.filter(status=TemplateVersionStatus.PUBLISHED).first()
        return published.version_number if published else None


class AppraisalQuestionWriteSerializer(serializers.Serializer):
    prompt = serializers.CharField(allow_blank=True)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    order = serializers.IntegerField(required=False, min_value=1)
    response_type = serializers.ChoiceField(choices=QuestionResponseType.choices)
    counts_toward_score = serializers.BooleanField(required=False, default=False)
    required = serializers.BooleanField(required=False, default=False)
    weight = serializers.DecimalField(
        max_digits=5, decimal_places=2, required=False, allow_null=True
    )
    options = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )


class AppraisalSectionWriteSerializer(serializers.Serializer):
    title = serializers.CharField(allow_blank=True)
    section_code = serializers.CharField(required=False, allow_blank=True, default="")
    kind = serializers.ChoiceField(choices=SectionKind.choices)
    audience = serializers.ChoiceField(choices=SectionAudience.choices)
    weight = serializers.DecimalField(max_digits=5, decimal_places=2, required=False, default=0)
    rating_scale_max = serializers.IntegerField(required=False, min_value=1, max_value=10, default=5)
    order = serializers.IntegerField(required=False, min_value=1)
    questions = AppraisalQuestionWriteSerializer(many=True, required=False, default=list)


class AppraisalPhaseWriteSerializer(serializers.Serializer):
    title = serializers.CharField(allow_blank=True)
    order = serializers.IntegerField(required=False, min_value=1)
    sections = AppraisalSectionWriteSerializer(many=True, required=False, default=list)


class AppraisalTemplateWriteSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=200)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    appraisal_type = serializers.PrimaryKeyRelatedField(queryset=AppraisalType.objects.all())
    scope = serializers.ChoiceField(
        choices=TemplateScope.choices, required=False, allow_blank=True, default=""
    )
    department = serializers.PrimaryKeyRelatedField(
        queryset=Department.objects.all(), required=False, allow_null=True
    )
    unit = serializers.PrimaryKeyRelatedField(
        queryset=Unit.objects.all(), required=False, allow_null=True
    )
    team = serializers.PrimaryKeyRelatedField(
        queryset=Team.objects.all(), required=False, allow_null=True
    )
    is_active = serializers.BooleanField(required=False)
    phases = AppraisalPhaseWriteSerializer(many=True, required=False)
    sections = AppraisalSectionWriteSerializer(many=True, required=False)


class AppraisalSelfPhase1Serializer(serializers.ModelSerializer):
    class Meta:
        model = AppraisalSelfPhase1
        fields = [
            "id",
            "key_achievements",
            "noteworthy_achievements",
            "challenges_encountered",
            "support_needed",
        ]


class AppraisalQuestionAnswerSerializer(serializers.ModelSerializer):
    question_prompt = serializers.CharField(source="question.prompt", read_only=True)
    section_code = serializers.CharField(source="question.section.section_code", read_only=True)
    section_id = serializers.UUIDField(source="question.section.id", read_only=True)

    class Meta:
        model = AppraisalQuestionAnswer
        fields = [
            "id",
            "question",
            "question_prompt",
            "section_id",
            "section_code",
            "respondent",
            "value",
            "self_rating",
            "supervisor_rating",
            "comments",
        ]


class AppraisalSupervisorPhase3Serializer(serializers.ModelSerializer):
    class Meta:
        model = AppraisalSupervisorPhase3
        fields = [
            "id",
            "strengths",
            "areas_for_development",
            "training_development_needs",
        ]


class AppraisalPhase4SignoffSerializer(serializers.ModelSerializer):
    class Meta:
        model = AppraisalPhase4Signoff
        fields = [
            "id",
            "goals_next_year",
            "recommendations",
            "recommendation_others",
            "supervisor_comments",
            "appraisee_agrees",
            "appraisee_disagree_reason",
            "appraisee_additional_comments",
            "hr_observations",
            "hr_recommended_action",
            "management_comments",
            "committee_decision",
        ]


class EmployeeAppraisalListSerializer(serializers.ModelSerializer):
    employee = UserSerializer(read_only=True)
    supervisor = UserSerializer(read_only=True)
    cycle_name = serializers.CharField(source="cycle.name", read_only=True)
    template_title = serializers.CharField(source="template.title", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = EmployeeAppraisal
        fields = [
            "id",
            "employee",
            "supervisor",
            "cycle",
            "cycle_name",
            "template",
            "template_title",
            "status",
            "status_display",
            "self_score_total",
            "supervisor_score_total",
            "official_score",
            "performance_category",
            "self_submitted_at",
            "supervisor_submitted_at",
            "appraisee_signed_at",
            "finalized_at",
            "created_at",
        ]


class AppraisalPhaseSubmissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = AppraisalPhaseSubmission
        fields = ["id", "phase", "respondent", "submitted_at"]


class EmployeeAppraisalDetailSerializer(serializers.ModelSerializer):
    employee = UserSerializer(read_only=True)
    supervisor = UserSerializer(read_only=True)
    cycle = AppraisalCycleSerializer(read_only=True)
    template = AppraisalTemplateSerializer(read_only=True)
    form = serializers.SerializerMethodField()
    phase1_self = AppraisalSelfPhase1Serializer(read_only=True)
    answers = AppraisalQuestionAnswerSerializer(many=True, read_only=True)
    phase_submissions = AppraisalPhaseSubmissionSerializer(many=True, read_only=True)
    phase3_supervisor = AppraisalSupervisorPhase3Serializer(read_only=True)
    phase4_signoff = AppraisalPhase4SignoffSerializer(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    score_is_provisional = serializers.SerializerMethodField()

    class Meta:
        model = EmployeeAppraisal
        fields = [
            "id",
            "employee",
            "supervisor",
            "cycle",
            "template",
            "template_version",
            "form",
            "status",
            "status_display",
            "self_score_total",
            "supervisor_score_total",
            "official_score",
            "score_is_provisional",
            "performance_category",
            "phase1_self",
            "answers",
            "phase_submissions",
            "phase3_supervisor",
            "phase4_signoff",
            "self_submitted_at",
            "supervisor_submitted_at",
            "appraisee_signed_at",
            "hr_reviewed_at",
            "finalized_at",
            "created_at",
            "updated_at",
        ]

    def get_form(self, obj):
        if not obj.template_version_id:
            return None
        return AppraisalTemplateVersionSerializer(obj.template_version).data

    def get_score_is_provisional(self, obj):
        return score_is_provisional(obj)

    def to_representation(self, instance):
        if instance.template_version_id:
            self.fields["template"].context["template_version"] = instance.template_version
        return super().to_representation(instance)


class AppraisalAuditLogSerializer(serializers.ModelSerializer):
    actor_name = serializers.CharField(source="actor.get_full_name", read_only=True)

    class Meta:
        model = AppraisalAuditLog
        fields = [
            "id",
            "actor",
            "actor_name",
            "action",
            "previous_status",
            "new_status",
            "comments",
            "timestamp",
        ]


# ---------------------------------------------------------------------------
# Workflow Action Input Payloads
# ---------------------------------------------------------------------------

class AnswerSelfRatingItemSerializer(serializers.Serializer):
    question_id = serializers.UUIDField()
    self_rating = serializers.IntegerField(min_value=1, max_value=10)
    comments = serializers.CharField(required=False, allow_blank=True, default="")


class Phase1And2SelfSubmitSerializer(serializers.Serializer):
    key_achievements = serializers.CharField(required=False, allow_blank=True, default="")
    noteworthy_achievements = serializers.CharField(required=False, allow_blank=True, default="")
    challenges_encountered = serializers.CharField(required=False, allow_blank=True, default="")
    support_needed = serializers.CharField(required=False, allow_blank=True, default="")
    ratings = AnswerSelfRatingItemSerializer(many=True, required=False, default=list)
    is_draft = serializers.BooleanField(default=False)


class AnswerSupervisorRatingItemSerializer(serializers.Serializer):
    question_id = serializers.UUIDField()
    supervisor_rating = serializers.IntegerField(min_value=1, max_value=10)
    comments = serializers.CharField(required=False, allow_blank=True, default="")


class Phase2And3SupervisorSubmitSerializer(serializers.Serializer):
    # Phase 2 Ratings
    ratings = AnswerSupervisorRatingItemSerializer(many=True, required=False, default=list)
    # Phase 3 Qualitative
    strengths = serializers.CharField(required=False, allow_blank=True, default="")
    areas_for_development = serializers.CharField(required=False, allow_blank=True, default="")
    training_development_needs = serializers.CharField(required=False, allow_blank=True, default="")
    # Phase 4 Goal & Recommendations
    goals_next_year = serializers.CharField(required=False, allow_blank=True, default="")
    recommendations = serializers.ListField(
        child=serializers.ChoiceField(choices=OutcomeRecommendationChoice.choices),
        required=False,
        default=list,
    )
    recommendation_others = serializers.CharField(required=False, allow_blank=True, default="")
    supervisor_comments = serializers.CharField(required=False, allow_blank=True, default="")
    is_draft = serializers.BooleanField(default=False)


class AppraiseeSignoffSerializer(serializers.Serializer):
    appraisee_agrees = serializers.BooleanField()
    appraisee_disagree_reason = serializers.CharField(required=False, allow_blank=True, default="")
    appraisee_additional_comments = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        if not attrs.get("appraisee_agrees") and not attrs.get("appraisee_disagree_reason"):
            raise serializers.ValidationError({
                "appraisee_disagree_reason": "Reason for disagreement is compulsory when employee disagrees with appraisal rating."
            })
        return attrs


class HRCalibrationSerializer(serializers.Serializer):
    hr_observations = serializers.CharField(required=False, allow_blank=True, default="")
    hr_recommended_action = serializers.CharField(required=False, allow_blank=True, default="")


class ManagementFinalizeSerializer(serializers.Serializer):
    management_comments = serializers.CharField(required=False, allow_blank=True, default="")
    committee_decision = serializers.CharField(required=False, allow_blank=True, default="")


class ReopenAppraisalSerializer(serializers.Serializer):
    target_stage = serializers.ChoiceField(
        choices=["REOPENED_FOR_EMPLOYEE", "REOPENED_FOR_SUPERVISOR"]
    )
    reason = serializers.CharField(required=True)


class DynamicAnswerItemSerializer(serializers.Serializer):
    question_id = serializers.UUIDField()
    value = serializers.JSONField(required=False, allow_null=True)


class DynamicStepSubmitSerializer(serializers.Serializer):
    answers = DynamicAnswerItemSerializer(many=True, required=False, default=list)
    is_draft = serializers.BooleanField(default=False)
    phase_id = serializers.UUIDField(required=False, allow_null=True)
