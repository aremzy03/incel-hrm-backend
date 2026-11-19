from decimal import Decimal, ROUND_HALF_UP

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import Team, Unit, get_or_create_management_department

from .models import (
    AnswerRespondent,
    AppraisalAuditLog,
    AppraisalPhase,
    AppraisalPhaseSubmission,
    AppraisalQuestion,
    AppraisalQuestionAnswer,
    AppraisalSection,
    AppraisalTemplate,
    AppraisalTemplateVersion,
    EmployeeAppraisal,
    EmployeeAppraisalStatus,
    PerformanceCategory,
    QuestionResponseType,
    SectionAudience,
    SectionKind,
    TemplateScope,
    TemplateVersionStatus,
)


class TemplateBuildError(Exception):
    def __init__(self, detail, existing_template_id=None):
        self.detail = detail
        self.existing_template_id = existing_template_id
        super().__init__(str(detail))


HUNDRED = Decimal("100.00")
ACTOR_RESPONDENT = {
    "employee": AnswerRespondent.EMPLOYEE,
    "supervisor": AnswerRespondent.SUPERVISOR,
    "hr": AnswerRespondent.HR,
    "management": AnswerRespondent.MANAGEMENT,
}
ACTOR_AUDIENCES = {
    "employee": {SectionAudience.EMPLOYEE, SectionAudience.BOTH},
    "supervisor": {SectionAudience.SUPERVISOR, SectionAudience.BOTH},
    "hr": {SectionAudience.HR},
    "management": {SectionAudience.MANAGEMENT},
}


def performance_category_for(score: Decimal) -> str:
    if score >= Decimal("90.00"):
        return PerformanceCategory.EXCEPTIONAL
    if score >= Decimal("80.00"):
        return PerformanceCategory.HIGH
    if score >= Decimal("65.00"):
        return PerformanceCategory.GOOD
    if score >= Decimal("50.00"):
        return PerformanceCategory.DEVELOPING
    return PerformanceCategory.UNDER


def _q2(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def respondents_for_section(section: AppraisalSection) -> list[str]:
    if section.audience == SectionAudience.BOTH:
        return [AnswerRespondent.EMPLOYEE, AnswerRespondent.SUPERVISOR]
    return [section.audience]


def calculate_appraisal_scores(appraisal: EmployeeAppraisal) -> EmployeeAppraisal:
    if appraisal.template_version_id:
        return _calculate_versioned_scores(appraisal)
    return _calculate_legacy_scores(appraisal)


def _section_source(appraisal: EmployeeAppraisal):
    if appraisal.template_version_id:
        return appraisal.template_version.sections.prefetch_related("questions").all()
    version = (
        appraisal.template.versions.filter(status=TemplateVersionStatus.PUBLISHED).first()
        or appraisal.template.versions.filter(status=TemplateVersionStatus.DRAFT).first()
    )
    if version is None:
        return AppraisalSection.objects.none()
    return version.sections.prefetch_related("questions").all()


def _calculate_legacy_scores(appraisal: EmployeeAppraisal) -> EmployeeAppraisal:
    """Equal-weight section scores from the original self and supervisor rating columns."""
    sections = _section_source(appraisal)
    self_grand_total = Decimal("0.00")
    supervisor_grand_total = Decimal("0.00")
    answers_map = {
        ans.question_id: ans
        for ans in AppraisalQuestionAnswer.objects.filter(appraisal=appraisal)
    }

    for section in sections:
        questions = list(section.questions.all())
        num_questions = len(questions)
        if num_questions == 0:
            continue
        rating_max = section.rating_scale_max or 5
        section_weight = Decimal(str(section.weight))
        max_possible_raw = num_questions * rating_max
        self_raw = 0
        supervisor_raw = 0
        for question in questions:
            ans = answers_map.get(question.id)
            if not ans:
                continue
            if ans.self_rating is not None:
                self_raw += ans.self_rating
            if ans.supervisor_rating is not None:
                supervisor_raw += ans.supervisor_rating
        if max_possible_raw > 0:
            self_grand_total += (Decimal(self_raw) / Decimal(max_possible_raw)) * section_weight
            supervisor_grand_total += (Decimal(supervisor_raw) / Decimal(max_possible_raw)) * section_weight

    appraisal.self_score_total = _q2(self_grand_total)
    appraisal.supervisor_score_total = _q2(supervisor_grand_total)
    appraisal.official_score = appraisal.supervisor_score_total
    appraisal.performance_category = performance_category_for(appraisal.official_score)
    appraisal.save(
        update_fields=[
            "self_score_total",
            "supervisor_score_total",
            "official_score",
            "performance_category",
            "updated_at",
        ]
    )
    return appraisal


def _rating_value(answer) -> Decimal | None:
    if answer is None or answer.value is None:
        return None
    if isinstance(answer.value, bool):
        return None
    if isinstance(answer.value, (int, float, Decimal)):
        return Decimal(str(answer.value))
    return None


def _calculate_versioned_scores(appraisal: EmployeeAppraisal) -> EmployeeAppraisal:
    sections = appraisal.template_version.sections.prefetch_related("questions").all()
    answers = {
        (ans.question_id, ans.respondent): ans
        for ans in AppraisalQuestionAnswer.objects.filter(appraisal=appraisal)
    }
    official = Decimal("0")
    employee_total = Decimal("0")
    supervisor_total = Decimal("0")

    for section in sections:
        if section.kind not in {SectionKind.SCORED, SectionKind.INFORMATIONAL_RATING}:
            continue
        scale = Decimal(section.rating_scale_max or 5)
        section_weight = Decimal(str(section.weight))
        if scale <= 0:
            continue
        for question in section.questions.all():
            if not question.counts_toward_score:
                continue
            question_weight = Decimal(str(question.weight or 0))
            respondents = (
                [AnswerRespondent.EMPLOYEE, AnswerRespondent.SUPERVISOR]
                if section.audience == SectionAudience.BOTH
                else [section.audience]
            )
            for respondent in respondents:
                answer = answers.get((question.id, respondent))
                rating = _rating_value(answer)
                if rating is None:
                    rating = Decimal("0")
                contribution = section_weight * (rating / scale) * (question_weight / HUNDRED)
                if respondent == AnswerRespondent.EMPLOYEE:
                    employee_total += contribution
                elif respondent == AnswerRespondent.SUPERVISOR:
                    supervisor_total += contribution
                if section.audience != SectionAudience.BOTH:
                    official += contribution

    appraisal.self_score_total = _q2(employee_total)
    appraisal.supervisor_score_total = _q2(supervisor_total)
    appraisal.official_score = _q2(official)
    appraisal.performance_category = performance_category_for(appraisal.official_score)
    appraisal.save(
        update_fields=[
            "self_score_total",
            "supervisor_score_total",
            "official_score",
            "performance_category",
            "updated_at",
        ]
    )
    return appraisal


def score_is_provisional(appraisal: EmployeeAppraisal) -> bool:
    if not appraisal.template_version_id:
        return False
    for section in appraisal.template_version.sections.prefetch_related("questions"):
        if section.kind not in {SectionKind.SCORED, SectionKind.INFORMATIONAL_RATING}:
            continue
        for question in section.questions.all():
            if not question.counts_toward_score:
                continue
            respondents = (
                [AnswerRespondent.EMPLOYEE, AnswerRespondent.SUPERVISOR]
                if section.audience == SectionAudience.BOTH
                else [section.audience]
            )
            for respondent in respondents:
                answer = appraisal.answers.filter(
                    question=question,
                    respondent=respondent,
                ).first()
                if _rating_value(answer) is None:
                    return True
    return False


def transition_appraisal_status(
    appraisal: EmployeeAppraisal,
    new_status: str,
    actor=None,
    comments: str = "",
) -> EmployeeAppraisal:
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


def _scope_key(scope: str, department_id=None, unit_id=None, team_id=None) -> str:
    if scope == TemplateScope.ORG:
        return "ORG"
    if scope == TemplateScope.DEPARTMENT and department_id:
        return f"DEPARTMENT:{department_id}"
    if scope == TemplateScope.UNIT and unit_id:
        return f"UNIT:{unit_id}"
    if scope == TemplateScope.TEAM and team_id:
        return f"TEAM:{team_id}"
    return ""


def _apply_scope(template: AppraisalTemplate, data: dict) -> None:
    scope = data.get("scope", template.scope)
    department = data.get("department", template.department_id)
    unit = data.get("unit", template.unit_id)
    team = data.get("team", template.team_id)
    if hasattr(department, "pk"):
        department = department.pk
    if hasattr(unit, "pk"):
        unit = unit.pk
    if hasattr(team, "pk"):
        team = team.pk

    if template.assignment_locked:
        same = (
            (scope or "") == (template.scope or "")
            and str(department or "") == str(template.department_id or "")
            and str(unit or "") == str(template.unit_id or "")
            and str(team or "") == str(template.team_id or "")
            and str(data.get("appraisal_type", template.appraisal_type_id)) == str(template.appraisal_type_id)
        )
        if not same:
            raise TemplateBuildError(
                "Appraisal type and assignment are locked after the first publish. Duplicate the template to target a different group."
            )
        return

    template.scope = scope or ""
    template.department_id = department or None
    template.unit_id = unit or None
    template.team_id = team or None
    if template.scope == TemplateScope.TEAM and template.team_id:
        team_row = Team.objects.select_related("unit").filter(pk=template.team_id).first()
        if team_row and team_row.unit_id:
            template.unit_id = team_row.unit_id
            template.department_id = team_row.unit.department_id
    elif template.scope == TemplateScope.UNIT and template.unit_id:
        unit_row = Unit.objects.filter(pk=template.unit_id).first()
        if unit_row:
            template.department_id = unit_row.department_id
    if "appraisal_type" in data and data["appraisal_type"]:
        template.appraisal_type_id = data["appraisal_type"].pk if hasattr(data["appraisal_type"], "pk") else data["appraisal_type"]


def _assert_target_available(template: AppraisalTemplate) -> None:
    key = _scope_key(template.scope, template.department_id, template.unit_id, template.team_id)
    if not key:
        return
    conflict = (
        AppraisalTemplate.objects.filter(appraisal_type_id=template.appraisal_type_id, scope_key=key)
        .exclude(pk=template.pk)
        .first()
    )
    if conflict:
        raise TemplateBuildError(
            "Another template for this appraisal type is already assigned to that target.",
            existing_template_id=str(conflict.id),
        )


def ensure_draft(template: AppraisalTemplate) -> AppraisalTemplateVersion:
    draft = template.versions.filter(status=TemplateVersionStatus.DRAFT).first()
    if draft:
        return draft
    published = template.versions.filter(status=TemplateVersionStatus.PUBLISHED).first()
    next_number = (template.versions.order_by("-version_number").values_list("version_number", flat=True).first() or 0) + 1
    draft = AppraisalTemplateVersion.objects.create(
        template=template,
        version_number=next_number,
        status=TemplateVersionStatus.DRAFT,
    )
    if published:
        clone_sections(published, draft)
    return draft


def clone_sections(source: AppraisalTemplateVersion, target: AppraisalTemplateVersion) -> None:
    for phase in source.phases.prefetch_related("sections__questions").all():
        sections = list(phase.sections.all())
        phase.pk = None
        phase.version = target
        phase.save()
        for section in sections:
            questions = list(section.questions.all())
            section.pk = None
            section.phase = phase
            section.version = target
            section.save()
            for question in questions:
                question.pk = None
                question.section = section
                question.save()


def _create_section(draft, phase, section_data, index):
    section = AppraisalSection.objects.create(
        version=draft,
        phase=phase,
        title=section_data.get("title", ""),
        section_code=section_data.get("section_code", ""),
        kind=section_data.get("kind") or SectionKind.NARRATIVE,
        audience=section_data.get("audience") or SectionAudience.EMPLOYEE,
        weight=section_data.get("weight") or Decimal("0"),
        rating_scale_max=section_data.get("rating_scale_max") or 5,
        order=section_data.get("order") or index,
    )
    for q_index, question_data in enumerate(section_data.get("questions") or [], start=1):
        weight = question_data.get("weight")
        response_type = question_data.get("response_type") or QuestionResponseType.LONG_TEXT
        counts_toward_score = bool(question_data.get("counts_toward_score"))
        if section.kind == SectionKind.CHECKBOX:
            response_type = QuestionResponseType.MULTI_SELECT
            counts_toward_score = False
        AppraisalQuestion.objects.create(
            section=section,
            prompt=question_data.get("prompt", ""),
            description=question_data.get("description", ""),
            order=question_data.get("order") or q_index,
            response_type=response_type,
            counts_toward_score=counts_toward_score,
            required=bool(question_data.get("required")),
            weight=weight if weight not in ("", None) else None,
            options=question_data.get("options") or [],
        )


def replace_draft_phases(draft: AppraisalTemplateVersion, phases_data: list[dict]) -> None:
    if draft.status != TemplateVersionStatus.DRAFT:
        raise TemplateBuildError("Published versions cannot be edited.")
    draft.phases.all().delete()
    for index, phase_data in enumerate(phases_data, start=1):
        phase = AppraisalPhase.objects.create(
            version=draft,
            title=phase_data.get("title", ""),
            order=phase_data.get("order") or index,
        )
        for section_index, section_data in enumerate(phase_data.get("sections") or [], start=1):
            _create_section(draft, phase, section_data, section_index)


def save_template_document(template: AppraisalTemplate, data: dict) -> AppraisalTemplate:
    if "title" in data:
        template.title = data["title"]
    if "description" in data:
        template.description = data["description"]
    if "is_active" in data:
        template.is_active = data["is_active"]
    _apply_scope(template, data)
    _assert_target_available(template)
    try:
        template.save()
    except IntegrityError as exc:
        raise TemplateBuildError(
            "Another template for this appraisal type is already assigned to that target."
        ) from exc
    if "phases" in data or "sections" in data:
        draft = ensure_draft(template)
        if "phases" in data:
            phases = data["phases"]
        else:
            phases = [{"title": "Phase 1", "sections": data.get("sections") or []}]
        replace_draft_phases(draft, phases)
    return template


def validate_draft(draft: AppraisalTemplateVersion) -> list[str]:
    errors: list[str] = []
    template = draft.template
    if template.scope not in TemplateScope.values:
        errors.append("Choose who this template is for before publishing.")
    elif template.scope == TemplateScope.DEPARTMENT and not template.department_id:
        errors.append("Choose a department.")
    elif template.scope == TemplateScope.UNIT and not template.unit_id:
        errors.append("Choose a unit.")
    elif template.scope == TemplateScope.TEAM and not template.team_id:
        errors.append("Choose a team.")

    phases = list(draft.phases.prefetch_related("sections__questions"))
    if not phases:
        errors.append("Add at least one phase before publishing.")
    scored_weights = []
    for phase in phases:
        phase_label = phase.title.strip() or "Untitled phase"
        if not phase.title.strip():
            errors.append("Every phase needs a title.")
        if not phase.sections.exists():
            errors.append(f"{phase_label} needs at least one section.")
        for section in phase.sections.all():
            label = section.title or "Untitled section"
            questions = list(section.questions.all())
            if section.kind in {SectionKind.SCORED, SectionKind.INFORMATIONAL_RATING}:
                strict = section.kind == SectionKind.SCORED
                if section.audience not in {
                    SectionAudience.EMPLOYEE,
                    SectionAudience.SUPERVISOR,
                    SectionAudience.HR,
                    SectionAudience.MANAGEMENT,
                    SectionAudience.BOTH,
                }:
                    errors.append(f"{label}: choose who fills this rated section.")
                counting = []
                for question in questions:
                    if question.response_type != QuestionResponseType.RATING:
                        errors.append(f"{label}: rating sections can only contain rating questions.")
                    if question.counts_toward_score:
                        counting.append(question)
                        if question.weight is None:
                            errors.append(f"{label}: scoring questions need a weight.")
                if strict and not counting:
                    errors.append(f"{label}: mark at least one rating as counting toward the score.")
                elif counting and not any("need a weight" in err and label in err for err in errors):
                    if not _weights_are_100([q.weight for q in counting]):
                        errors.append(f"{label}: weights of questions that count toward the score must add up to 100.")
                if strict and _q2(section.weight) <= 0:
                    errors.append(f"{label}: a scored section needs a weight above 0.")
                if strict and section.audience != SectionAudience.BOTH:
                    scored_weights.append(section.weight)
            elif section.kind == SectionKind.NARRATIVE:
                for question in questions:
                    if question.response_type == QuestionResponseType.RATING:
                        errors.append(f"{label}: narrative sections cannot contain rating questions.")
                    if question.response_type == QuestionResponseType.MULTI_SELECT and len(question.options or []) < 2:
                        errors.append(f"{label}: multi-select questions need at least two options.")
            elif section.kind == SectionKind.CHECKBOX:
                for question in questions:
                    if question.response_type != QuestionResponseType.MULTI_SELECT:
                        errors.append(f"{label}: checkbox sections can only contain checkbox questions.")
                    if len(question.options or []) < 2:
                        errors.append(f"{label}: each checkbox question needs at least two options.")
                    if question.counts_toward_score:
                        errors.append(f"{label}: checkbox answers cannot count toward the official score.")
            else:
                errors.append(f"{label}: unknown section kind.")

    if scored_weights and not _weights_are_100(scored_weights):
        errors.append("Weights of scored sections must add up to 100.")
    return errors


def _weights_are_100(values) -> bool:
    total = sum((Decimal(str(value or 0)) for value in values), Decimal("0"))
    return _q2(total) == HUNDRED


def publish_template(template: AppraisalTemplate, user) -> AppraisalTemplateVersion:
    draft = template.versions.filter(status=TemplateVersionStatus.DRAFT).first()
    if draft is None:
        raise TemplateBuildError("There is no draft to publish.")
    _assert_target_available(template)
    errors = validate_draft(draft)
    if errors:
        raise TemplateBuildError(errors)
    with transaction.atomic():
        current = template.versions.filter(status=TemplateVersionStatus.PUBLISHED).first()
        if current:
            current.status = TemplateVersionStatus.ARCHIVED
            current.save(update_fields=["status", "updated_at"])
        draft.status = TemplateVersionStatus.PUBLISHED
        draft.published_at = timezone.now()
        draft.published_by = user
        draft.save(update_fields=["status", "published_at", "published_by", "updated_at"])
    return draft


def duplicate_template(source: AppraisalTemplate) -> AppraisalTemplate:
    source_version = (
        source.versions.filter(status=TemplateVersionStatus.PUBLISHED).first()
        or source.versions.filter(status=TemplateVersionStatus.DRAFT).first()
    )
    copy = AppraisalTemplate.objects.create(
        title=f"Copy of {source.title}"[:200],
        description=source.description,
        appraisal_type=source.appraisal_type,
        is_active=True,
    )
    draft = AppraisalTemplateVersion.objects.create(
        template=copy,
        version_number=1,
        status=TemplateVersionStatus.DRAFT,
    )
    if source_version:
        clone_sections(source_version, draft)
    return copy


def published_version_for(appraisal_type_id, scope: str, **filters):
    template = AppraisalTemplate.objects.filter(
        appraisal_type_id=appraisal_type_id,
        scope=scope,
        is_active=True,
        **filters,
    ).first()
    if template is None:
        return None, None
    version = template.versions.filter(status=TemplateVersionStatus.PUBLISHED).first()
    if version is None:
        return None, None
    return template, version


def _other_active_person(person, employee):
    return person is not None and person.is_active and person.pk != employee.pk


def resolve_appraisal_supervisor(employee):
    """Choose who fills the supervisor sections for this employee.

    A unit supervisor reviews members of that unit. When the employee has no
    unit, or the unit has no supervisor, the department line manager reviews
    them. A person is never assigned to review themselves; the Management
    department line manager reviews a department line manager.
    """
    unit = getattr(employee, "unit", None)
    unit_supervisor = getattr(unit, "supervisor", None) if unit is not None else None
    if _other_active_person(unit_supervisor, employee):
        return unit_supervisor

    manager = employee.get_department_line_manager()
    if _other_active_person(manager, employee):
        return manager

    if manager is not None and manager.pk == employee.pk:
        management_lead = get_or_create_management_department().line_manager
        if _other_active_person(management_lead, employee):
            return management_lead
    return None


def resolve_template_for_employee(employee, appraisal_type):
    if employee.team_id:
        found = published_version_for(appraisal_type.id, TemplateScope.TEAM, team_id=employee.team_id)
        if found[1]:
            return found
    if employee.unit_id:
        found = published_version_for(appraisal_type.id, TemplateScope.UNIT, unit_id=employee.unit_id)
        if found[1]:
            return found
    if employee.department_id:
        found = published_version_for(
            appraisal_type.id,
            TemplateScope.DEPARTMENT,
            department_id=employee.department_id,
        )
        if found[1]:
            return found
    return published_version_for(appraisal_type.id, TemplateScope.ORG)


def spawn_answer_shells(appraisal: EmployeeAppraisal) -> None:
    version = appraisal.template_version
    if version is None:
        return
    for section in version.sections.prefetch_related("questions"):
        for question in section.questions.all():
            for respondent in respondents_for_section(section):
                AppraisalQuestionAnswer.objects.get_or_create(
                    appraisal=appraisal,
                    question=question,
                    respondent=respondent,
                )


def _is_empty(value) -> bool:
    return value is None or value == "" or value == []


def _coerce_answer(question: AppraisalQuestion, value):
    if _is_empty(value):
        return None, None
    if question.response_type == QuestionResponseType.RATING:
        try:
            rating = int(value)
        except (TypeError, ValueError):
            return None, "Enter a whole-number rating."
        scale = question.section.rating_scale_max or 5
        if rating < 1 or rating > scale:
            return None, f"Rating must be between 1 and {scale}."
        return rating, None
    if question.response_type == QuestionResponseType.YES_NO:
        if isinstance(value, bool):
            return value, None
        return None, "Choose yes or no."
    if question.response_type == QuestionResponseType.MULTI_SELECT:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            return None, "Choose one or more options."
        allowed = set(question.options or [])
        if any(item not in allowed for item in value):
            return None, "One of the selected options is not on this question."
        return value, None
    if not isinstance(value, str):
        return None, "Enter text."
    text = value.strip()
    if question.response_type == QuestionResponseType.SHORT_TEXT and len(text) > 500:
        return None, "Keep this answer under 500 characters."
    return text, None


def questions_for_actor(appraisal: EmployeeAppraisal, actor: str) -> dict:
    audiences = ACTOR_AUDIENCES[actor]
    found = {}
    for section in appraisal.template_version.sections.prefetch_related("questions"):
        if section.audience not in audiences:
            continue
        for question in section.questions.all():
            found[question.id] = question
    return found


def save_actor_answers(appraisal: EmployeeAppraisal, actor: str, items: list[dict], phase: AppraisalPhase | None = None) -> dict:
    respondent = ACTOR_RESPONDENT[actor]
    allowed = questions_for_actor(appraisal, actor)
    errors = {}
    for item in items:
        question = allowed.get(item["question_id"])
        if question is None:
            errors[str(item["question_id"])] = "This question is not on your step."
            continue
        if phase is not None and question.section.phase_id != phase.id:
            errors[str(question.id)] = "This question is not in the phase you are submitting."
            continue
        value, error = _coerce_answer(question, item.get("value"))
        if error:
            errors[str(question.id)] = error
            continue
        answer, _ = AppraisalQuestionAnswer.objects.get_or_create(
            appraisal=appraisal,
            question=question,
            respondent=respondent,
        )
        answer.value = value
        if question.response_type == QuestionResponseType.RATING and isinstance(value, int):
            if respondent == AnswerRespondent.EMPLOYEE:
                answer.self_rating = value
            elif respondent == AnswerRespondent.SUPERVISOR:
                answer.supervisor_rating = value
        answer.save()
    return errors


def respondents_required_for_phase(phase: AppraisalPhase) -> set[str]:
    required = set()
    for section in phase.sections.all():
        required.update(respondents_for_section(section))
    return required


def _ordered_phases(appraisal: EmployeeAppraisal):
    if not appraisal.template_version_id:
        return []
    return list(appraisal.template_version.phases.prefetch_related("sections").order_by("order", "created_at"))


def _submitted_respondents(appraisal: EmployeeAppraisal, phase: AppraisalPhase) -> set[str]:
    return set(
        appraisal.phase_submissions.filter(phase=phase).values_list("respondent", flat=True)
    )


def _respondents_that_block(actor: str, required: set[str]) -> set[str]:
    if actor in {"employee", "supervisor"}:
        return required & {AnswerRespondent.EMPLOYEE, AnswerRespondent.SUPERVISOR}
    return required


def earlier_phase_block(appraisal: EmployeeAppraisal, phase: AppraisalPhase, actor: str) -> str | None:
    for earlier in _ordered_phases(appraisal):
        if earlier.id == phase.id:
            return None
        needed = _respondents_that_block(actor, respondents_required_for_phase(earlier))
        if needed and not needed <= _submitted_respondents(appraisal, earlier):
            return "Finish the previous phase before submitting this one."
    return "That phase is not part of this appraisal."


def resolve_submit_phase(appraisal: EmployeeAppraisal, actor: str, phase_id):
    phases = _ordered_phases(appraisal)
    respondent = ACTOR_RESPONDENT[actor]
    if phase_id:
        phase = next((item for item in phases if str(item.id) == str(phase_id)), None)
        if phase is None:
            return None, "That phase is not part of this appraisal."
        if respondent not in respondents_required_for_phase(phase):
            return None, "This phase has no questions for you."
        return phase, earlier_phase_block(appraisal, phase, actor)
    owed = [phase for phase in phases if respondent in respondents_required_for_phase(phase)]
    if not owed:
        return None, None
    open_phases = [
        phase
        for phase in owed
        if earlier_phase_block(appraisal, phase, actor) is None
        and respondent not in _submitted_respondents(appraisal, phase)
    ]
    if len(open_phases) == 1:
        return open_phases[0], None
    if len(owed) == 1:
        return owed[0], earlier_phase_block(appraisal, owed[0], actor)
    return None, "Submit one phase at a time."


def actor_complete(appraisal: EmployeeAppraisal, actor: str) -> bool:
    respondent = ACTOR_RESPONDENT[actor]
    for phase in _ordered_phases(appraisal):
        if respondent not in respondents_required_for_phase(phase):
            continue
        if respondent not in _submitted_respondents(appraisal, phase):
            return False
    return True


def content_phases_complete(appraisal: EmployeeAppraisal) -> bool:
    return actor_complete(appraisal, "employee") and actor_complete(appraisal, "supervisor")


def sync_phase_workflow(appraisal: EmployeeAppraisal, actor: str, user, comment: str, next_status: str) -> None:
    early_statuses = {
        EmployeeAppraisalStatus.DRAFT,
        EmployeeAppraisalStatus.NOT_STARTED,
        EmployeeAppraisalStatus.REOPENED_FOR_EMPLOYEE,
        EmployeeAppraisalStatus.SELF_SUBMITTED,
        EmployeeAppraisalStatus.REOPENED_FOR_SUPERVISOR,
    }
    if appraisal.status not in early_statuses:
        if actor in {"hr", "management"} and actor_complete(appraisal, actor):
            transition_appraisal_status(appraisal, next_status, actor=user, comments=comment)
        return
    employee_done = actor_complete(appraisal, "employee")
    supervisor_done = actor_complete(appraisal, "supervisor")
    if employee_done and supervisor_done:
        transition_appraisal_status(
            appraisal,
            EmployeeAppraisalStatus.PENDING_APPRAISEE_ACCEPTANCE,
            actor=user,
            comments=comment,
        )
    elif employee_done and appraisal.status in {
        EmployeeAppraisalStatus.DRAFT,
        EmployeeAppraisalStatus.NOT_STARTED,
        EmployeeAppraisalStatus.REOPENED_FOR_EMPLOYEE,
    }:
        transition_appraisal_status(
            appraisal,
            EmployeeAppraisalStatus.SELF_SUBMITTED,
            actor=user,
            comments=comment,
        )


def record_phase_submission(appraisal: EmployeeAppraisal, phase: AppraisalPhase, actor: str) -> None:
    AppraisalPhaseSubmission.objects.get_or_create(
        appraisal=appraisal,
        phase=phase,
        respondent=ACTOR_RESPONDENT[actor],
        defaults={"submitted_at": timezone.now()},
    )


def clear_phase_submissions(appraisal: EmployeeAppraisal, actor: str) -> None:
    appraisal.phase_submissions.filter(respondent=ACTOR_RESPONDENT[actor]).delete()


def missing_required_prompts(appraisal: EmployeeAppraisal, actor: str, phase: AppraisalPhase | None = None) -> list[str]:
    respondent = ACTOR_RESPONDENT[actor]
    missing = []
    for question in questions_for_actor(appraisal, actor).values():
        if phase is not None and question.section.phase_id != phase.id:
            continue
        if not question.required:
            continue
        answer = AppraisalQuestionAnswer.objects.filter(
            appraisal=appraisal,
            question=question,
            respondent=respondent,
        ).first()
        if answer is None or _is_empty(answer.value):
            missing.append(question.prompt or "Untitled question")
    return missing
