from rest_framework import serializers
from apps.accounts.serializers import UserSerializer

from .models import (
    AppraisalAuditLog,
    AppraisalCycle,
    AppraisalPhase4Signoff,
    AppraisalQuestion,
    AppraisalQuestionAnswer,
    AppraisalSection,
    AppraisalSelfPhase1,
    AppraisalSupervisorPhase3,
    AppraisalTemplate,
    AppraisalType,
    EmployeeAppraisal,
    OutcomeRecommendationChoice,
)


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
        fields = ["id", "section", "prompt", "description", "order"]


class AppraisalSectionSerializer(serializers.ModelSerializer):
    questions = AppraisalQuestionSerializer(many=True, read_only=True)

    class Meta:
        model = AppraisalSection
        fields = [
            "id",
            "template",
            "title",
            "section_code",
            "weight",
            "rating_scale_max",
            "order",
            "questions",
        ]


class AppraisalTemplateSerializer(serializers.ModelSerializer):
    sections = AppraisalSectionSerializer(many=True, read_only=True)
    department_name = serializers.CharField(source="department.name", read_only=True)
    appraisal_type_name = serializers.CharField(source="appraisal_type.name", read_only=True)

    class Meta:
        model = AppraisalTemplate
        fields = [
            "id",
            "appraisal_type",
            "appraisal_type_name",
            "title",
            "department",
            "department_name",
            "description",
            "is_active",
            "sections",
            "created_at",
        ]


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
            "performance_category",
            "self_submitted_at",
            "supervisor_submitted_at",
            "appraisee_signed_at",
            "finalized_at",
            "created_at",
        ]


class EmployeeAppraisalDetailSerializer(serializers.ModelSerializer):
    employee = UserSerializer(read_only=True)
    supervisor = UserSerializer(read_only=True)
    cycle = AppraisalCycleSerializer(read_only=True)
    template = AppraisalTemplateSerializer(read_only=True)
    phase1_self = AppraisalSelfPhase1Serializer(read_only=True)
    answers = AppraisalQuestionAnswerSerializer(many=True, read_only=True)
    phase3_supervisor = AppraisalSupervisorPhase3Serializer(read_only=True)
    phase4_signoff = AppraisalPhase4SignoffSerializer(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = EmployeeAppraisal
        fields = [
            "id",
            "employee",
            "supervisor",
            "cycle",
            "template",
            "status",
            "status_display",
            "self_score_total",
            "supervisor_score_total",
            "performance_category",
            "phase1_self",
            "answers",
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
