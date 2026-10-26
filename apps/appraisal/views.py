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
    AppraisalAuditLog,
    AppraisalCycle,
    AppraisalCycleStatus,
    AppraisalPhase4Signoff,
    AppraisalQuestion,
    AppraisalQuestionAnswer,
    AppraisalSelfPhase1,
    AppraisalSupervisorPhase3,
    AppraisalTemplate,
    AppraisalType,
    EmployeeAppraisal,
    EmployeeAppraisalStatus,
)
from .serializers import (
    AppraisalAuditLogSerializer,
    AppraisalCycleSerializer,
    AppraisalPhase4SignoffSerializer,
    AppraisalTemplateSerializer,
    AppraisalTypeSerializer,
    AppraiseeSignoffSerializer,
    EmployeeAppraisalDetailSerializer,
    EmployeeAppraisalListSerializer,
    HRCalibrationSerializer,
    ManagementFinalizeSerializer,
    Phase1And2SelfSubmitSerializer,
    Phase2And3SupervisorSubmitSerializer,
    ReopenAppraisalSerializer,
)
from .services import calculate_appraisal_scores, transition_appraisal_status

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

        # Spawn EmployeeAppraisal instances for all active employees
        employees = User.objects.filter(is_active=True)
        spawned_count = 0

        with transaction.atomic():
            cycle.status = AppraisalCycleStatus.ACTIVE
            cycle.save()

            for emp in employees:
                # Find department template or default template
                template = AppraisalTemplate.objects.filter(
                    appraisal_type=cycle.appraisal_type,
                    department=emp.department,
                    is_active=True,
                ).first()

                if not template:
                    template = AppraisalTemplate.objects.filter(
                        appraisal_type=cycle.appraisal_type,
                        department__isnull=True,
                        is_active=True,
                    ).first()

                if not template:
                    continue

                # Supervisor detection: employee's line manager or department line manager
                supervisor = getattr(emp, "line_manager", None)
                if not supervisor and emp.department:
                    supervisor = getattr(emp.department, "line_manager", None)

                appraisal, created = EmployeeAppraisal.objects.get_or_create(
                    employee=emp,
                    cycle=cycle,
                    defaults={
                        "template": template,
                        "supervisor": supervisor,
                        "status": EmployeeAppraisalStatus.DRAFT,
                    },
                )
                if created:
                    spawned_count += 1
                    # Initialize answers shell
                    questions = AppraisalQuestion.objects.filter(section__template=template)
                    for q in questions:
                        AppraisalQuestionAnswer.objects.create(appraisal=appraisal, question=q)

        return Response({
            "detail": f"Appraisal cycle activated successfully. {spawned_count} employee appraisals created.",
            "spawned_count": spawned_count,
        })


class AppraisalTemplateViewSet(viewsets.ModelViewSet):
    queryset = AppraisalTemplate.objects.all()
    serializer_class = AppraisalTemplateSerializer
    permission_classes = [IsAuthenticated]

    def get_permissions(self):
        if self.action in ["create", "update", "partial_update", "destroy"]:
            return [IsHR()]
        return [IsAuthenticated()]


class EmployeeAppraisalViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = EmployeeAppraisal.objects.select_related(
        "employee", "supervisor", "cycle", "template"
    ).prefetch_related(
        "answers", "answers__question", "answers__question__section"
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

        is_supervisor = appraisal.supervisor == user or user.has_role(RoleName.LINE_MANAGER) or user.has_role(RoleName.SUPERVISOR)
        if not is_supervisor and not (user.is_staff or user.has_role(RoleName.HR)):
            return Response({"detail": "Only assigned supervisor, Line Manager, or HR can submit supervisor assessment."}, status=status.HTTP_403_FORBIDDEN)

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

        return Response(EmployeeAppraisalDetailSerializer(appraisal).data)

    @action(detail=True, methods=["get"], url_path="audit-logs")
    def audit_logs(self, request, pk=None):
        appraisal = self.get_object()
        logs = appraisal.audit_logs.all()
        return Response(AppraisalAuditLogSerializer(logs, many=True).data)
