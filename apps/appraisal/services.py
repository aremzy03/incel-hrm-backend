from decimal import Decimal, ROUND_HALF_UP
from django.db import transaction
from django.utils import timezone

from .models import (
    AppraisalAuditLog,
    EmployeeAppraisal,
    EmployeeAppraisalStatus,
    PerformanceCategory,
)


def calculate_appraisal_scores(appraisal: EmployeeAppraisal) -> EmployeeAppraisal:
    """
    Computes weighted section scores and grand totals for both employee self-rating
    and supervisor rating according to the template section weights and rating scales.
    Updates performance_category based on supervisor grand total.
    """
    sections = appraisal.template.sections.prefetch_related("questions").all()
    
    self_grand_total = Decimal("0.00")
    supervisor_grand_total = Decimal("0.00")
    
    answers_map = {
        ans.question_id: ans for ans in appraisal.answers.select_related("question").all()
    }

    for section in sections:
        questions = section.questions.all()
        num_questions = questions.count()
        if num_questions == 0:
            continue

        rating_max = section.rating_scale_max or 5
        section_weight = Decimal(str(section.weight))
        max_possible_raw = num_questions * rating_max

        self_raw = 0
        supervisor_raw = 0

        for q in questions:
            ans = answers_map.get(q.id)
            if ans:
                if ans.self_rating is not None:
                    self_raw += ans.self_rating
                if ans.supervisor_rating is not None:
                    supervisor_raw += ans.supervisor_rating

        if max_possible_raw > 0:
            section_self_weighted = (Decimal(self_raw) / Decimal(max_possible_raw)) * section_weight
            section_supervisor_weighted = (Decimal(supervisor_raw) / Decimal(max_possible_raw)) * section_weight

            self_grand_total += section_self_weighted
            supervisor_grand_total += section_supervisor_weighted

    # Round to 2 decimal places
    appraisal.self_score_total = self_grand_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    appraisal.supervisor_score_total = supervisor_grand_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # Classify Performance Tier based on Supervisor Grand Total Score
    sup_score = appraisal.supervisor_score_total
    if sup_score >= Decimal("90.00"):
        appraisal.performance_category = PerformanceCategory.EXCEPTIONAL
    elif sup_score >= Decimal("80.00"):
        appraisal.performance_category = PerformanceCategory.HIGH
    elif sup_score >= Decimal("65.00"):
        appraisal.performance_category = PerformanceCategory.GOOD
    elif sup_score >= Decimal("50.00"):
        appraisal.performance_category = PerformanceCategory.DEVELOPING
    else:
        appraisal.performance_category = PerformanceCategory.UNDER

    appraisal.save(update_fields=["self_score_total", "supervisor_score_total", "performance_category", "updated_at"])
    return appraisal


def transition_appraisal_status(
    appraisal: EmployeeAppraisal,
    new_status: str,
    actor=None,
    comments: str = "",
) -> EmployeeAppraisal:
    """
    Safely transitions appraisal status, logs audit event, and updates transition timestamps.
    """
    with transaction.atomic():
        prev_status = appraisal.status
        appraisal.status = new_status
        
        now = timezone.now()
        if new_status == EmployeeAppraisalStatus.SELF_SUBMITTED and not appraisal.self_submitted_at:
            appraisal.self_submitted_at = now
        elif new_status == EmployeeAppraisalStatus.PENDING_APPRAISEE_ACCEPTANCE and not appraisal.supervisor_submitted_at:
            appraisal.supervisor_submitted_at = now
        elif new_status == EmployeeAppraisalStatus.PENDING_HR_CALIBRATION and not appraisal.appraisee_signed_at:
            appraisal.appraisee_signed_at = now
        elif new_status == EmployeeAppraisalStatus.PENDING_MANAGEMENT_DECISION and not appraisal.hr_reviewed_at:
            appraisal.hr_reviewed_at = now
        elif new_status == EmployeeAppraisalStatus.FINALIZED and not appraisal.finalized_at:
            appraisal.finalized_at = now

        appraisal.save()

        AppraisalAuditLog.objects.create(
            appraisal=appraisal,
            actor=actor,
            action=f"Transitioned status to {new_status}",
            previous_status=prev_status,
            new_status=new_status,
            comments=comments,
        )

    return appraisal
