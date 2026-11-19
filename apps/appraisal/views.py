from django.contrib.auth import get_user_model
from django.db import models, transaction
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.models import RoleName
from apps.accounts.permissions import IsHR

from .models import (
    AnswerRespondent,
    AppraisalAuditLog,
    AppraisalCycle,
    AppraisalCycleStatus,
    AppraisalPhase4Signoff,
    AppraisalQuestionAnswer,
    AppraisalSelfPhase1,
    AppraisalSupervisorPhase3,
    AppraisalTemplate,
    AppraisalTemplateVersion,
    AppraisalType,
    EmployeeAppraisal,
    EmployeeAppraisalStatus,
    TemplateScope,
)
from .serializers import (
    AppraisalAuditLogSerializer,
    AppraisalCycleSerializer,
    AppraisalTemplateListSerializer,
    AppraisalTemplateSerializer,
    AppraisalTemplateVersionSerializer,
    AppraisalTemplateWriteSerializer,
    AppraisalTypeSerializer,
    AppraiseeSignoffSerializer,
    DynamicStepSubmitSerializer,
    EmployeeAppraisalDetailSerializer,
    EmployeeAppraisalListSerializer,
    HRCalibrationSerializer,
    ManagementFinalizeSerializer,
    Phase1And2SelfSubmitSerializer,
    Phase2And3SupervisorSubmitSerializer,
    ReopenAppraisalSerializer,
)
from .services import (
    TemplateBuildError,
    calculate_appraisal_scores,
    clear_phase_submissions,
    content_phases_complete,
    duplicate_template,
    missing_required_prompts,
    publish_template,
    published_version_for,
    record_phase_submission,
    resolve_appraisal_supervisor,
    resolve_submit_phase,
    resolve_template_for_employee,
    respondents_required_for_phase,
    save_actor_answers,
    save_template_document,
    spawn_answer_shells,
    sync_phase_workflow,
    transition_appraisal_status,
)

User = get_user_model()


class AppraisalTypeViewSet(viewsets.ModelViewSet):
    queryset = AppraisalType.objects.all()
    serializer_class = AppraisalTypeSerializer
    permission_classes = [IsAuthenticated]

    def get_permissions(self):
        if self.action in ["create", "update", "partial_update", "destroy"]:
            return [IsHR()]
        return [IsAuthenticated()]


class AppraisalCycleViewSet(viewsets.ModelViewSet):
    queryset = AppraisalCycle.objects.all()
    serializer_class = AppraisalCycleSerializer
    permission_classes = [IsAuthenticated]

    def get_permissions(self):
        if self.action in ["create", "update", "partial_update", "destroy", "activate"]:
            return [IsHR()]
        return [IsAuthenticated()]

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=True, methods=["post"], url_path="activate")
    def activate(self, request, pk=None):
        cycle = self.get_object()
        if cycle.status == AppraisalCycleStatus.ACTIVE:
            return Response({"detail": "Appraisal cycle is already active."}, status=status.HTTP_400_BAD_REQUEST)

        org_template, org_version = published_version_for(cycle.appraisal_type_id, TemplateScope.ORG)
        if org_version is None:
            return Response(
                {
                    "detail": "Publish an organization-wide template for this appraisal type before activating the cycle."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        employees = User.objects.filter(is_active=True).select_related(
            "department__line_manager",
            "unit__supervisor",
            "team",
        )
        spawned_count = 0
        by_template: dict[str, int] = {}

        with transaction.atomic():
            cycle.status = AppraisalCycleStatus.ACTIVE
            cycle.save()

            for emp in employees:
                template, version = resolve_template_for_employee(emp, cycle.appraisal_type)
                if version is None:
                    template, version = org_template, org_version

                supervisor = resolve_appraisal_supervisor(emp)

                appraisal, created = EmployeeAppraisal.objects.get_or_create(
                    employee=emp,
                    cycle=cycle,
                    defaults={
                        "template": template,
                        "template_version": version,
                        "supervisor": supervisor,
                        "status": EmployeeAppraisalStatus.DRAFT,
                    },
                )
                if created:
                    spawned_count += 1
                    by_template[str(template.id)] = by_template.get(str(template.id), 0) + 1
                    spawn_answer_shells(appraisal)

        return Response({
            "detail": f"Appraisal cycle activated successfully. {spawned_count} employee appraisals created.",
            "spawned_count": spawned_count,
            "by_template": by_template,
        })


class AppraisalTemplateViewSet(viewsets.ModelViewSet):
    queryset = AppraisalTemplate.objects.select_related(
        "appraisal_type", "department", "unit", "team"
    ).prefetch_related(
        "versions",
        "versions__phases",
        "versions__phases__sections",
        "versions__phases__sections__questions",
        "versions__sections",
        "versions__sections__questions",
    )
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_serializer_class(self):
        if self.action == "list":
            return AppraisalTemplateListSerializer
        return AppraisalTemplateSerializer

    def get_permissions(self):
        if self.action in ["create", "partial_update", "publish", "duplicate"]:
            return [IsHR()]
        return [IsAuthenticated()]

    def _template_error(self, exc: TemplateBuildError):
        body = {"detail": exc.detail}
        if exc.existing_template_id:
            body["existing_template_id"] = exc.existing_template_id
        return Response(body, status=status.HTTP_400_BAD_REQUEST)

    def create(self, request, *args, **kwargs):
        serializer = AppraisalTemplateWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        template = AppraisalTemplate(
            title=serializer.validated_data["title"],
            appraisal_type=serializer.validated_data["appraisal_type"],
        )
        try:
            save_template_document(template, serializer.validated_data)
        except TemplateBuildError as exc:
            return self._template_error(exc)
        return Response(AppraisalTemplateSerializer(template).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        template = self.get_object()
        serializer = AppraisalTemplateWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = {
            key: serializer.validated_data[key]
            for key in request.data
            if key in serializer.validated_data
        }
        try:
            save_template_document(template, data)
        except TemplateBuildError as exc:
            return self._template_error(exc)
        template.refresh_from_db()
        return Response(AppraisalTemplateSerializer(template).data)

    @action(detail=True, methods=["post"], url_path="publish")
    def publish(self, request, pk=None):
        template = self.get_object()
        try:
            publish_template(template, request.user)
        except TemplateBuildError as exc:
            return self._template_error(exc)
        template.refresh_from_db()
        return Response(AppraisalTemplateSerializer(template).data)

    @action(detail=True, methods=["post"], url_path="duplicate")
    def duplicate(self, request, pk=None):
        template = self.get_object()
        copy = duplicate_template(template)
        return Response(AppraisalTemplateSerializer(copy).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"], url_path="versions")
    def versions(self, request, pk=None):
        template = self.get_object()
        versions = template.versions.all()
        return Response(AppraisalTemplateVersionSerializer(versions, many=True).data)

    @action(detail=True, methods=["get"], url_path=r"versions/(?P<version_id>[^/.]+)")
    def version_detail(self, request, pk=None, version_id=None):
        template = self.get_object()
        version = get_object_or_404(AppraisalTemplateVersion, pk=version_id, template=template)
        return Response(AppraisalTemplateVersionSerializer(version).data)


class EmployeeAppraisalViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = EmployeeAppraisal.objects.select_related(
        "employee", "supervisor", "cycle", "template", "template_version"
    ).prefetch_related(
        "answers",
        "answers__question",
        "answers__question__section",
        "template_version__phases",
        "template_version__phases__sections",
        "template_version__phases__sections__questions",
        "template_version__sections",
        "template_version__sections__questions",
        "phase_submissions",
    ).all()
    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        if self.action == "list":
            return EmployeeAppraisalListSerializer
        return EmployeeAppraisalDetailSerializer

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()

        # HR and Admin can view all
        if user.is_staff or user.has_role(RoleName.HR) or user.has_role(RoleName.MANAGING_DIRECTOR) or user.has_role(RoleName.EXECUTIVE_DIRECTOR):
            return qs

        # Supervisor can view direct reports + self
        return qs.filter(models.Q(employee=user) | models.Q(supervisor=user))

    @action(detail=False, methods=["get"], url_path="my-appraisals")
    def my_appraisals(self, request):
        qs = self.get_queryset().filter(employee=request.user)
        serializer = EmployeeAppraisalListSerializer(qs, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=["get"], url_path="team-appraisals")
    def team_appraisals(self, request):
        qs = self.get_queryset().filter(supervisor=request.user)
        serializer = EmployeeAppraisalListSerializer(qs, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=["post"], url_path="self-submit")
    def self_submit(self, request, pk=None):
        """Phase 1 & Phase 2 Self-Assessment submission by appraisee."""
        appraisal = self.get_object()
        user = request.user

        if appraisal.employee != user and not (user.is_staff or user.has_role(RoleName.HR)):
            return Response({"detail": "Only the appraisee or HR can fill self-assessment."}, status=status.HTTP_403_FORBIDDEN)

        valid_statuses = [
            EmployeeAppraisalStatus.DRAFT,
            EmployeeAppraisalStatus.NOT_STARTED,
            EmployeeAppraisalStatus.REOPENED_FOR_EMPLOYEE,
        ]
        if appraisal.status not in valid_statuses:
            return Response({"detail": f"Appraisal cannot be modified in state {appraisal.get_status_display()}"}, status=status.HTTP_400_BAD_REQUEST)

        if appraisal.template_version_id:
            return self._submit_versioned(
                request,
                appraisal,
                actor="employee",
                next_status=EmployeeAppraisalStatus.SELF_SUBMITTED,
                comment="Employee submitted their section of the appraisal",
            )

        serializer = Phase1And2SelfSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        with transaction.atomic():
            # Phase 1
            phase1, _ = AppraisalSelfPhase1.objects.get_or_create(appraisal=appraisal)
            phase1.key_achievements = data.get("key_achievements", phase1.key_achievements)
            phase1.noteworthy_achievements = data.get("noteworthy_achievements", phase1.noteworthy_achievements)
            phase1.challenges_encountered = data.get("challenges_encountered", phase1.challenges_encountered)
            phase1.support_needed = data.get("support_needed", phase1.support_needed)
            phase1.save()

            # Phase 2 Ratings
            for item in data.get("ratings", []):
                q_id = item["question_id"]
                rating = item["self_rating"]
                ans_comments = item.get("comments", "")
                
                ans, created = AppraisalQuestionAnswer.objects.get_or_create(
                    appraisal=appraisal,
                    question_id=q_id,
                )
                ans.self_rating = rating
                if ans_comments:
                    ans.comments = ans_comments
                ans.save()

            # Re-calculate score
            calculate_appraisal_scores(appraisal)

            if not data.get("is_draft", False):
                transition_appraisal_status(
                    appraisal,
                    EmployeeAppraisalStatus.SELF_SUBMITTED,
                    actor=user,
                    comments="Employee submitted Phase 1 and Phase 2 self-assessment",
                )

        return Response(EmployeeAppraisalDetailSerializer(appraisal).data)

    @action(detail=True, methods=["post"], url_path="supervisor-submit")
    def supervisor_submit(self, request, pk=None):
        """Phase 2 & Phase 3 Supervisor assessment submission by line manager."""
        appraisal = self.get_object()
        user = request.user

        if appraisal.supervisor_id is None:
            return Response(
                {
                    "detail": "This appraisal has no supervisor. Assign a unit supervisor or a department line manager first."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        is_supervisor = appraisal.supervisor == user or user.has_role(RoleName.LINE_MANAGER) or user.has_role(RoleName.SUPERVISOR)
        if not is_supervisor and not (user.is_staff or user.has_role(RoleName.HR)):
            return Response({"detail": "Only assigned supervisor, Line Manager, or HR can submit supervisor assessment."}, status=status.HTTP_403_FORBIDDEN)

        if appraisal.template_version_id:
            return self._submit_versioned(
                request,
                appraisal,
                actor="supervisor",
                next_status=EmployeeAppraisalStatus.PENDING_APPRAISEE_ACCEPTANCE,
                comment="Supervisor submitted their section of the appraisal",
            )

        valid_statuses = [
            EmployeeAppraisalStatus.SELF_SUBMITTED,
            EmployeeAppraisalStatus.REOPENED_FOR_SUPERVISOR,
        ]
        if appraisal.status not in valid_statuses and not (user.is_staff or user.has_role(RoleName.HR)):
            return Response({"detail": f"Appraisal is not ready for supervisor review (Current state: {appraisal.get_status_display()})"}, status=status.HTTP_400_BAD_REQUEST)

        serializer = Phase2And3SupervisorSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        with transaction.atomic():
            # Update supervisor ratings for questions
            for item in data.get("ratings", []):
                q_id = item["question_id"]
                rating = item["supervisor_rating"]
                ans_comments = item.get("comments", "")

                ans, _ = AppraisalQuestionAnswer.objects.get_or_create(
                    appraisal=appraisal,
                    question_id=q_id,
                )
                ans.supervisor_rating = rating
                if ans_comments:
                    ans.comments = ans_comments
                ans.save()

            # Re-calculate score & category
            calculate_appraisal_scores(appraisal)

            # Phase 3
            phase3, _ = AppraisalSupervisorPhase3.objects.get_or_create(appraisal=appraisal)
            phase3.strengths = data.get("strengths", phase3.strengths)
            phase3.areas_for_development = data.get("areas_for_development", phase3.areas_for_development)
            phase3.training_development_needs = data.get("training_development_needs", phase3.training_development_needs)
            phase3.save()

            # Phase 4 Goals & Recommendations
            phase4, _ = AppraisalPhase4Signoff.objects.get_or_create(appraisal=appraisal)
            phase4.goals_next_year = data.get("goals_next_year", phase4.goals_next_year)
            phase4.recommendations = data.get("recommendations", phase4.recommendations)
            phase4.recommendation_others = data.get("recommendation_others", phase4.recommendation_others)
            phase4.supervisor_comments = data.get("supervisor_comments", phase4.supervisor_comments)
            phase4.save()

            if not data.get("is_draft", False):
                transition_appraisal_status(
                    appraisal,
                    EmployeeAppraisalStatus.PENDING_APPRAISEE_ACCEPTANCE,
                    actor=user,
                    comments="Supervisor submitted ratings, evaluation and recommendations",
                )

        return Response(EmployeeAppraisalDetailSerializer(appraisal).data)

    @action(detail=True, methods=["post"], url_path="appraisee-signoff")
    def appraisee_signoff(self, request, pk=None):
        """Phase 4 Employee Acceptance / Signoff (Agree or Disagree)."""
        appraisal = self.get_object()
        user = request.user

        if appraisal.employee != user and not (user.is_staff or user.has_role(RoleName.HR)):
            return Response({"detail": "Only the appraisee can sign off on their appraisal outcome."}, status=status.HTTP_403_FORBIDDEN)

        if appraisal.status != EmployeeAppraisalStatus.PENDING_APPRAISEE_ACCEPTANCE:
            return Response({"detail": f"Appraisal is not pending appraisee sign-off (State: {appraisal.get_status_display()})"}, status=status.HTTP_400_BAD_REQUEST)

        if appraisal.template_version_id and not content_phases_complete(appraisal):
            return Response(
                {"detail": "Finish every phase before sign-off."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = AppraiseeSignoffSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        with transaction.atomic():
            phase4, _ = AppraisalPhase4Signoff.objects.get_or_create(appraisal=appraisal)
            phase4.appraisee_agrees = data["appraisee_agrees"]
            phase4.appraisee_disagree_reason = data.get("appraisee_disagree_reason", "")
            phase4.appraisee_additional_comments = data.get("appraisee_additional_comments", "")
            phase4.save()

            transition_appraisal_status(
                appraisal,
                EmployeeAppraisalStatus.PENDING_HR_CALIBRATION,
                actor=user,
                comments=f"Appraisee signed off (Agrees: {data['appraisee_agrees']})",
            )

        return Response(EmployeeAppraisalDetailSerializer(appraisal).data)

    @action(detail=True, methods=["post"], url_path="hr-calibrate")
    def hr_calibrate(self, request, pk=None):
        """HR Review & Calibration step."""
        appraisal = self.get_object()
        user = request.user

        if not (user.is_staff or user.has_role(RoleName.HR)):
            return Response({"detail": "Only HR Administrators can calibrate appraisals."}, status=status.HTTP_403_FORBIDDEN)

        if appraisal.status != EmployeeAppraisalStatus.PENDING_HR_CALIBRATION:
            return Response({"detail": f"Appraisal is not pending HR calibration (State: {appraisal.get_status_display()})"}, status=status.HTTP_400_BAD_REQUEST)

        if appraisal.template_version_id:
            return self._submit_versioned(
                request,
                appraisal,
                actor="hr",
                next_status=EmployeeAppraisalStatus.PENDING_MANAGEMENT_DECISION,
                comment="HR completed calibration",
            )

        serializer = HRCalibrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        with transaction.atomic():
            phase4, _ = AppraisalPhase4Signoff.objects.get_or_create(appraisal=appraisal)
            phase4.hr_observations = data.get("hr_observations", phase4.hr_observations)
            phase4.hr_recommended_action = data.get("hr_recommended_action", phase4.hr_recommended_action)
            phase4.save()

            transition_appraisal_status(
                appraisal,
                EmployeeAppraisalStatus.PENDING_MANAGEMENT_DECISION,
                actor=user,
                comments="HR completed calibration and recommendations",
            )

        return Response(EmployeeAppraisalDetailSerializer(appraisal).data)

    @action(detail=True, methods=["post"], url_path="finalize")
    def finalize(self, request, pk=None):
        """Executive Management & Appraisal Committee Finalization."""
        appraisal = self.get_object()
        user = request.user

        is_management = user.has_role(RoleName.MANAGING_DIRECTOR) or user.has_role(RoleName.EXECUTIVE_DIRECTOR) or user.has_role(RoleName.HR) or user.is_staff
        if not is_management:
            return Response({"detail": "Only Management or HR can finalize appraisals."}, status=status.HTTP_403_FORBIDDEN)

        if appraisal.status != EmployeeAppraisalStatus.PENDING_MANAGEMENT_DECISION:
            return Response({"detail": f"Appraisal is not pending management decision (State: {appraisal.get_status_display()})"}, status=status.HTTP_400_BAD_REQUEST)

        if appraisal.template_version_id:
            return self._submit_versioned(
                request,
                appraisal,
                actor="management",
                next_status=EmployeeAppraisalStatus.FINALIZED,
                comment="Management finalized the appraisal",
            )

        serializer = ManagementFinalizeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        with transaction.atomic():
            phase4, _ = AppraisalPhase4Signoff.objects.get_or_create(appraisal=appraisal)
            phase4.management_comments = data.get("management_comments", phase4.management_comments)
            phase4.committee_decision = data.get("committee_decision", phase4.committee_decision)
            phase4.save()

            transition_appraisal_status(
                appraisal,
                EmployeeAppraisalStatus.FINALIZED,
                actor=user,
                comments="Management and Appraisal Committee finalized appraisal",
            )

        return Response(EmployeeAppraisalDetailSerializer(appraisal).data)

    @action(detail=True, methods=["post"], url_path="reopen")
    def reopen(self, request, pk=None):
        """HR override to reopen appraisal to employee or supervisor stage."""
        appraisal = self.get_object()
        user = request.user

        if not (user.is_staff or user.has_role(RoleName.HR)):
            return Response({"detail": "Only HR Administrators can reopen appraisals."}, status=status.HTTP_403_FORBIDDEN)

        serializer = ReopenAppraisalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        target_stage = serializer.validated_data["target_stage"]
        reason = serializer.validated_data["reason"]

        transition_appraisal_status(
            appraisal,
            target_stage,
            actor=user,
            comments=f"Reopened by HR to {target_stage}. Reason: {reason}",
        )
        if target_stage == EmployeeAppraisalStatus.REOPENED_FOR_EMPLOYEE:
            clear_phase_submissions(appraisal, "employee")
        elif target_stage == EmployeeAppraisalStatus.REOPENED_FOR_SUPERVISOR:
            clear_phase_submissions(appraisal, "supervisor")

        return Response(EmployeeAppraisalDetailSerializer(appraisal).data)

    @action(detail=True, methods=["get"], url_path="audit-logs")
    def audit_logs(self, request, pk=None):
        appraisal = self.get_object()
        logs = appraisal.audit_logs.all()
        return Response(AppraisalAuditLogSerializer(logs, many=True).data)

    def _submit_versioned(self, request, appraisal, actor, next_status, comment):
        serializer = DynamicStepSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        phase, block = resolve_submit_phase(appraisal, actor, data.get("phase_id"))
        if block:
            return Response({"detail": block}, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            errors = save_actor_answers(appraisal, actor, data.get("answers") or [], phase)
            if errors:
                transaction.set_rollback(True)
                return Response(errors, status=status.HTTP_400_BAD_REQUEST)
            if phase is not None and actor == "supervisor" and not data.get("is_draft"):
                required = respondents_required_for_phase(phase)
                employee_submitted = appraisal.phase_submissions.filter(
                    phase=phase,
                    respondent=AnswerRespondent.EMPLOYEE,
                ).exists()
                if AnswerRespondent.EMPLOYEE in required and not employee_submitted:
                    transaction.set_rollback(True)
                    return Response(
                        {"detail": "The employee must submit this phase first."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            if not data.get("is_draft"):
                missing = missing_required_prompts(appraisal, actor, phase)
                if missing:
                    transaction.set_rollback(True)
                    return Response(
                        {
                            "detail": "Answer the required questions before submitting.",
                            "missing": missing,
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if phase is not None:
                    record_phase_submission(appraisal, phase, actor)
            calculate_appraisal_scores(appraisal)
            if not data.get("is_draft"):
                sync_phase_workflow(appraisal, actor, request.user, comment, next_status)
        appraisal.refresh_from_db()
        return Response(EmployeeAppraisalDetailSerializer(appraisal).data)
