from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import (
    Department,
    Role,
    RoleName,
    Team,
    Unit,
    UserRole,
    get_or_create_management_department,
)
from apps.appraisal.models import (
    AppraisalCycle,
    AppraisalPhase,
    AppraisalPhaseSubmission,
    AppraisalCycleStatus,
    AppraisalQuestion,
    AppraisalSection,
    AppraisalTemplate,
    AppraisalTemplateVersion,
    AppraisalType,
    AppraisalTypeCode,
    EmployeeAppraisal,
    EmployeeAppraisalStatus,
    PerformanceCategory,
    QuestionResponseType,
    SectionAudience,
    SectionKind,
    TemplateScope,
    TemplateVersionStatus,
)

User = get_user_model()


def ensure_role(name: str) -> Role:
    role, _ = Role.objects.get_or_create(name=name, defaults={"description": name})
    return role


def make_user(email: str, password="testpass123", roles=None, department=None, unit=None, team=None):
    user = User.objects.create_user(
        email=email,
        password=password,
        first_name="Test",
        last_name="User",
        department=department,
        unit=unit,
        team=team,
    )
    for role_name in roles or []:
        role = ensure_role(role_name)
        UserRole.objects.get_or_create(user=user, role=role)
    return user


def add_question(section, prompt, *, counts=False, weight=None, required=False, response_type=QuestionResponseType.RATING, options=None):
    return AppraisalQuestion.objects.create(
        section=section,
        prompt=prompt,
        order=section.questions.count() + 1,
        response_type=response_type,
        counts_toward_score=counts,
        required=required,
        weight=weight,
        options=options or [],
    )


def add_section(version, **kwargs):
    phase = version.phases.order_by("order").first()
    if phase is None:
        phase = AppraisalPhase.objects.create(version=version, title="Phase 1", order=1)
    return AppraisalSection.objects.create(version=version, phase=phase, **kwargs)


def publish_version(template):
    return AppraisalTemplateVersion.objects.create(
        template=template,
        version_number=1,
        status=TemplateVersionStatus.PUBLISHED,
        published_at=timezone.now(),
    )


class AppraisalWorkflowTests(APITestCase):
    def setUp(self):
        self.sales_dept = Department.objects.create(name="Sales Department")
        self.hr_user = make_user("hr@incelgroup.com", roles=[RoleName.HR])
        self.manager = make_user("manager@incelgroup.com", roles=[RoleName.LINE_MANAGER], department=self.sales_dept)
        self.sales_dept.line_manager = self.manager
        self.sales_dept.save()

        self.employee = make_user(
            "employee@incelgroup.com",
            roles=[RoleName.EMPLOYEE],
            department=self.sales_dept,
        )

        self.appraisal_type = AppraisalType.objects.create(
            name="End of Year Appraisal",
            code=AppraisalTypeCode.END_OF_YEAR,
        )

        self.org_template = AppraisalTemplate.objects.create(
            title="Organization End of Year",
            appraisal_type=self.appraisal_type,
            scope=TemplateScope.ORG,
        )
        org_version = publish_version(self.org_template)
        org_section = add_section(
            version=org_version,
            title="Organization notes",
            kind=SectionKind.NARRATIVE,
            audience=SectionAudience.EMPLOYEE,
            order=1,
        )
        add_question(
            org_section,
            "Organization prompt",
            response_type=QuestionResponseType.LONG_TEXT,
        )

        self.template = AppraisalTemplate.objects.create(
            title="Sales End of Year Template",
            appraisal_type=self.appraisal_type,
            department=self.sales_dept,
            scope=TemplateScope.DEPARTMENT,
        )
        self.version = publish_version(self.template)

        self.sec_employee = add_section(
            version=self.version,
            title="Sales Strategy",
            section_code="SEC_EMP",
            kind=SectionKind.SCORED,
            audience=SectionAudience.EMPLOYEE,
            weight=Decimal("50.00"),
            rating_scale_max=5,
            order=1,
        )
        self.q_emp_1 = add_question(self.sec_employee, "Develop Strategy", counts=True, weight=Decimal("50.00"), required=True)
        self.q_emp_2 = add_question(self.sec_employee, "Execute Plan", counts=True, weight=Decimal("50.00"), required=True)

        self.sec_supervisor = add_section(
            version=self.version,
            title="Revenue Achievement",
            section_code="SEC_SUP",
            kind=SectionKind.SCORED,
            audience=SectionAudience.SUPERVISOR,
            weight=Decimal("50.00"),
            rating_scale_max=5,
            order=2,
        )
        self.q_sup_1 = add_question(self.sec_supervisor, "Hit Targets", counts=True, weight=Decimal("60.00"), required=True)
        self.q_sup_2 = add_question(self.sec_supervisor, "Grow Accounts", counts=True, weight=Decimal("40.00"), required=True)

        self.sec_both = add_section(
            version=self.version,
            title="Discussion rating",
            kind=SectionKind.INFORMATIONAL_RATING,
            audience=SectionAudience.BOTH,
            rating_scale_max=5,
            order=3,
        )
        self.q_both = add_question(self.sec_both, "Working relationship", required=False)

        self.cycle = AppraisalCycle.objects.create(
            name="2026 End of Year Appraisal Cycle",
            appraisal_type=self.appraisal_type,
            start_date=date.today(),
            end_date=date.today() + timedelta(days=30),
            submission_deadline=date.today() + timedelta(days=20),
            created_by=self.hr_user,
        )

    def test_cycle_activation_spawns_employee_appraisals(self):
        self.client.force_authenticate(user=self.hr_user)
        url = f"/api/v1/appraisal-cycles/{self.cycle.id}/activate/"
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.cycle.refresh_from_db()
        self.assertEqual(self.cycle.status, AppraisalCycleStatus.ACTIVE)

        appraisal = EmployeeAppraisal.objects.filter(employee=self.employee, cycle=self.cycle).first()
        self.assertIsNotNone(appraisal)
        self.assertEqual(appraisal.supervisor, self.manager)
        self.assertEqual(appraisal.template, self.template)
        self.assertEqual(appraisal.template_version, self.version)
        # Two employee ratings, two supervisor ratings, and both sides of the discussion rating.
        self.assertEqual(appraisal.answers.count(), 6)

    def _activate(self):
        self.client.force_authenticate(user=self.hr_user)
        response = self.client.post(f"/api/v1/appraisal-cycles/{self.cycle.id}/activate/")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response

    def test_unit_supervisor_reviews_members_of_that_unit(self):
        unit_supervisor = make_user(
            "unit-supervisor@incelgroup.com",
            roles=[RoleName.SUPERVISOR],
            department=self.sales_dept,
        )
        unit = Unit.objects.create(name="Field Sales", department=self.sales_dept, supervisor=unit_supervisor)
        self.employee.unit = unit
        self.employee.save(update_fields=["unit", "updated_at"])

        self._activate()
        appraisal = EmployeeAppraisal.objects.get(employee=self.employee, cycle=self.cycle)
        self.assertEqual(appraisal.supervisor, unit_supervisor)

    def test_department_line_manager_reviews_when_the_unit_has_no_supervisor(self):
        unit = Unit.objects.create(name="Open Sales", department=self.sales_dept)
        self.employee.unit = unit
        self.employee.save(update_fields=["unit", "updated_at"])

        self._activate()
        appraisal = EmployeeAppraisal.objects.get(employee=self.employee, cycle=self.cycle)
        self.assertEqual(appraisal.supervisor, self.manager)

    def test_a_person_does_not_review_their_own_unit(self):
        unit = Unit.objects.create(name="Managed Sales", department=self.sales_dept, supervisor=self.employee)
        self.employee.unit = unit
        self.employee.save(update_fields=["unit", "updated_at"])

        self._activate()
        appraisal = EmployeeAppraisal.objects.get(employee=self.employee, cycle=self.cycle)
        self.assertEqual(appraisal.supervisor, self.manager)

    def test_department_line_manager_is_reviewed_by_management(self):
        management = get_or_create_management_department()
        management_lead = make_user(
            "management@incelgroup.com",
            roles=[RoleName.LINE_MANAGER],
            department=management,
        )
        management.line_manager = management_lead
        management.save(update_fields=["line_manager", "updated_at"])

        self._activate()
        appraisal = EmployeeAppraisal.objects.get(employee=self.manager, cycle=self.cycle)
        self.assertEqual(appraisal.supervisor, management_lead)

    def test_supervisor_submit_is_refused_when_nobody_is_assigned(self):
        empty = Department.objects.create(name="Unassigned Department")
        orphan = make_user("orphan@incelgroup.com", roles=[RoleName.EMPLOYEE], department=empty)

        self._activate()
        appraisal = EmployeeAppraisal.objects.get(employee=orphan, cycle=self.cycle)
        self.assertIsNone(appraisal.supervisor_id)

        self.client.force_authenticate(user=self.hr_user)
        response = self.client.post(
            f"/api/v1/appraisals/{appraisal.id}/supervisor-submit/",
            {"answers": [], "is_draft": True},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("no supervisor", response.data["detail"])

    def test_end_to_end_appraisal_lifecycle(self):
        self.client.force_authenticate(user=self.hr_user)
        self.client.post(f"/api/v1/appraisal-cycles/{self.cycle.id}/activate/")
        appraisal = EmployeeAppraisal.objects.get(employee=self.employee, cycle=self.cycle)

        self.client.force_authenticate(user=self.employee)
        resp_self = self.client.post(
            f"/api/v1/appraisals/{appraisal.id}/self-submit/",
            {
                "answers": [
                    {"question_id": str(self.q_emp_1.id), "value": 5},
                    {"question_id": str(self.q_emp_2.id), "value": 5},
                    {"question_id": str(self.q_both.id), "value": 1},
                ],
                "is_draft": False,
            },
            format="json",
        )
        self.assertEqual(resp_self.status_code, status.HTTP_200_OK, resp_self.data)
        appraisal.refresh_from_db()
        self.assertEqual(appraisal.status, EmployeeAppraisalStatus.SELF_SUBMITTED)
        # Employee section is half the form, both ratings are 5/5, so the employee contribution is 50.
        self.assertEqual(appraisal.self_score_total, Decimal("50.00"))

        self.client.force_authenticate(user=self.manager)
        resp_sup = self.client.post(
            f"/api/v1/appraisals/{appraisal.id}/supervisor-submit/",
            {
                "answers": [
                    {"question_id": str(self.q_sup_1.id), "value": 5},
                    {"question_id": str(self.q_sup_2.id), "value": 5},
                    {"question_id": str(self.q_both.id), "value": 1},
                ],
                "is_draft": False,
            },
            format="json",
        )
        self.assertEqual(resp_sup.status_code, status.HTTP_200_OK, resp_sup.data)
        appraisal.refresh_from_db()
        self.assertEqual(appraisal.status, EmployeeAppraisalStatus.PENDING_APPRAISEE_ACCEPTANCE)
        self.assertEqual(appraisal.supervisor_score_total, Decimal("50.00"))
        # 50 from the employee section + 50 from the supervisor section. The both-columns rating is ignored.
        self.assertEqual(appraisal.official_score, Decimal("100.00"))
        self.assertEqual(appraisal.performance_category, PerformanceCategory.EXCEPTIONAL)

        self.client.force_authenticate(user=self.employee)
        resp_signoff = self.client.post(
            f"/api/v1/appraisals/{appraisal.id}/appraisee-signoff/",
            {"appraisee_agrees": True, "appraisee_additional_comments": "Thank you for the support!"},
            format="json",
        )
        self.assertEqual(resp_signoff.status_code, status.HTTP_200_OK)
        appraisal.refresh_from_db()
        self.assertEqual(appraisal.status, EmployeeAppraisalStatus.PENDING_HR_CALIBRATION)

        self.client.force_authenticate(user=self.hr_user)
        resp_hr = self.client.post(
            f"/api/v1/appraisals/{appraisal.id}/hr-calibrate/",
            {"answers": [], "is_draft": False},
            format="json",
        )
        self.assertEqual(resp_hr.status_code, status.HTTP_200_OK, resp_hr.data)
        appraisal.refresh_from_db()
        self.assertEqual(appraisal.status, EmployeeAppraisalStatus.PENDING_MANAGEMENT_DECISION)

        resp_fin = self.client.post(
            f"/api/v1/appraisals/{appraisal.id}/finalize/",
            {"answers": [], "is_draft": False},
            format="json",
        )
        self.assertEqual(resp_fin.status_code, status.HTTP_200_OK, resp_fin.data)
        appraisal.refresh_from_db()
        self.assertEqual(appraisal.status, EmployeeAppraisalStatus.FINALIZED)
        self.assertIsNotNone(appraisal.finalized_at)

    def test_employee_disagree_requires_reason(self):
        self.client.force_authenticate(user=self.hr_user)
        self.client.post(f"/api/v1/appraisal-cycles/{self.cycle.id}/activate/")

        appraisal = EmployeeAppraisal.objects.get(employee=self.employee, cycle=self.cycle)
        appraisal.status = EmployeeAppraisalStatus.PENDING_APPRAISEE_ACCEPTANCE
        appraisal.save()
        phase = self.version.phases.get()
        for respondent in ("EMPLOYEE", "SUPERVISOR"):
            AppraisalPhaseSubmission.objects.create(
                appraisal=appraisal,
                phase=phase,
                respondent=respondent,
            )

        self.client.force_authenticate(user=self.employee)
        url_signoff = f"/api/v1/appraisals/{appraisal.id}/appraisee-signoff/"
        resp_fail = self.client.post(url_signoff, {"appraisee_agrees": False}, format="json")
        self.assertEqual(resp_fail.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("appraisee_disagree_reason", resp_fail.data)

        resp_ok = self.client.post(
            url_signoff,
            {"appraisee_agrees": False, "appraisee_disagree_reason": "I disagree with the rating on Section 2."},
            format="json",
        )
        self.assertEqual(resp_ok.status_code, status.HTTP_200_OK)

    def test_activation_requires_published_org_template(self):
        self.org_template.versions.update(status=TemplateVersionStatus.ARCHIVED)
        self.client.force_authenticate(user=self.hr_user)
        response = self.client.post(f"/api/v1/appraisal-cycles/{self.cycle.id}/activate/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.cycle.refresh_from_db()
        self.assertEqual(self.cycle.status, AppraisalCycleStatus.DRAFT)
        self.assertFalse(EmployeeAppraisal.objects.filter(cycle=self.cycle).exists())

    def test_both_scored_section_stores_scores_separately(self):
        shared = add_section(
            version=self.version,
            title="Shared ratings",
            kind=SectionKind.SCORED,
            audience=SectionAudience.BOTH,
            weight=Decimal("20.00"),
            rating_scale_max=5,
            order=4,
        )
        shared_question = add_question(shared, "Collaboration", counts=True, weight=Decimal("100.00"), required=True)

        self.client.force_authenticate(user=self.hr_user)
        self.client.post(f"/api/v1/appraisal-cycles/{self.cycle.id}/activate/")
        appraisal = EmployeeAppraisal.objects.get(employee=self.employee, cycle=self.cycle)

        self.client.force_authenticate(user=self.employee)
        employee_submit = self.client.post(
            f"/api/v1/appraisals/{appraisal.id}/self-submit/",
            {
                "answers": [
                    {"question_id": str(self.q_emp_1.id), "value": 5},
                    {"question_id": str(self.q_emp_2.id), "value": 5},
                    {"question_id": str(self.q_both.id), "value": 1},
                    {"question_id": str(shared_question.id), "value": 5},
                ],
                "is_draft": False,
            },
            format="json",
        )
        self.assertEqual(employee_submit.status_code, status.HTTP_200_OK, employee_submit.data)
        appraisal.refresh_from_db()
        self.assertEqual(appraisal.self_score_total, Decimal("70.00"))
        self.assertEqual(appraisal.official_score, Decimal("50.00"))

        self.client.force_authenticate(user=self.manager)
        supervisor_submit = self.client.post(
            f"/api/v1/appraisals/{appraisal.id}/supervisor-submit/",
            {
                "answers": [
                    {"question_id": str(self.q_sup_1.id), "value": 5},
                    {"question_id": str(self.q_sup_2.id), "value": 5},
                    {"question_id": str(self.q_both.id), "value": 1},
                    {"question_id": str(shared_question.id), "value": 1},
                ],
                "is_draft": False,
            },
            format="json",
        )
        self.assertEqual(supervisor_submit.status_code, status.HTTP_200_OK, supervisor_submit.data)
        appraisal.refresh_from_db()
        self.assertEqual(appraisal.supervisor_score_total, Decimal("54.00"))
        self.assertEqual(appraisal.self_score_total, Decimal("70.00"))
        self.assertEqual(appraisal.official_score, Decimal("100.00"))
        self.assertEqual(appraisal.performance_category, PerformanceCategory.EXCEPTIONAL)

    def test_checkbox_section_stores_every_selected_option(self):
        choices = add_section(
            version=self.version,
            title="Recommendations",
            kind=SectionKind.CHECKBOX,
            audience=SectionAudience.SUPERVISOR,
            order=5,
        )
        question = add_question(
            choices,
            "Suggested outcomes",
            required=True,
            response_type=QuestionResponseType.MULTI_SELECT,
            options=["Promotion", "Bonus", "No change"],
        )

        self.client.force_authenticate(user=self.hr_user)
        created = self.client.post(
            "/api/v1/appraisal-templates/",
            {
                "title": "Checkbox only",
                "appraisal_type": str(self.appraisal_type.id),
                "scope": "DEPARTMENT",
                "department": str(Department.objects.create(name="Operations").id),
                "sections": [
                    {
                        "title": "Outcomes",
                        "kind": "CHECKBOX",
                        "audience": "SUPERVISOR",
                        "questions": [
                            {
                                "prompt": "Suggested outcomes",
                                "response_type": "LONG_TEXT",
                                "options": ["Promotion", "Bonus"],
                            }
                        ],
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        published = self.client.post(f"/api/v1/appraisal-templates/{created.data['id']}/publish/")
        self.assertEqual(published.status_code, status.HTTP_200_OK, published.data)
        stored = published.data["published"]["sections"][0]["questions"][0]
        self.assertEqual(stored["response_type"], "MULTI_SELECT")
        self.assertEqual(stored["options"], ["Promotion", "Bonus"])

        self.client.post(f"/api/v1/appraisal-cycles/{self.cycle.id}/activate/")
        appraisal = EmployeeAppraisal.objects.get(employee=self.employee, cycle=self.cycle)
        self.client.force_authenticate(user=self.employee)
        self.client.post(
            f"/api/v1/appraisals/{appraisal.id}/self-submit/",
            {
                "answers": [
                    {"question_id": str(self.q_emp_1.id), "value": 5},
                    {"question_id": str(self.q_emp_2.id), "value": 5},
                    {"question_id": str(self.q_both.id), "value": 1},
                ],
                "is_draft": False,
            },
            format="json",
        )
        self.client.force_authenticate(user=self.manager)
        supervisor_submit = self.client.post(
            f"/api/v1/appraisals/{appraisal.id}/supervisor-submit/",
            {
                "answers": [
                    {"question_id": str(self.q_sup_1.id), "value": 5},
                    {"question_id": str(self.q_sup_2.id), "value": 5},
                    {"question_id": str(self.q_both.id), "value": 1},
                    {"question_id": str(question.id), "value": ["Promotion", "Bonus"]},
                ],
                "is_draft": False,
            },
            format="json",
        )
        self.assertEqual(supervisor_submit.status_code, status.HTTP_200_OK, supervisor_submit.data)
        answer = appraisal.answers.get(question=question, respondent="SUPERVISOR")
        self.assertEqual(answer.value, ["Promotion", "Bonus"])

    def test_blank_labels_publish_and_informational_ratings_can_score(self):
        info = add_section(
            version=self.version,
            title="",
            kind=SectionKind.INFORMATIONAL_RATING,
            audience=SectionAudience.EMPLOYEE,
            weight=Decimal("20.00"),
            rating_scale_max=5,
            order=6,
        )
        info_question = add_question(info, "", counts=True, weight=Decimal("100.00"), required=False)

        self.client.force_authenticate(user=self.hr_user)
        created = self.client.post(
            "/api/v1/appraisal-templates/",
            {
                "title": "Open labels",
                "appraisal_type": str(self.appraisal_type.id),
                "scope": "DEPARTMENT",
                "department": str(Department.objects.create(name="Support").id),
                "sections": [
                    {
                        "title": "",
                        "kind": "INFORMATIONAL_RATING",
                        "audience": "EMPLOYEE",
                        "weight": "0",
                        "rating_scale_max": 5,
                        "questions": [
                            {
                                "prompt": "",
                                "response_type": "RATING",
                                "counts_toward_score": False,
                            }
                        ],
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        published = self.client.post(f"/api/v1/appraisal-templates/{created.data['id']}/publish/")
        self.assertEqual(published.status_code, status.HTTP_200_OK, published.data)
        stored = published.data["published"]["sections"][0]
        self.assertEqual(stored["title"], "")
        self.assertEqual(stored["questions"][0]["prompt"], "")

        self.client.post(f"/api/v1/appraisal-cycles/{self.cycle.id}/activate/")
        appraisal = EmployeeAppraisal.objects.get(employee=self.employee, cycle=self.cycle)
        self.client.force_authenticate(user=self.employee)
        employee_submit = self.client.post(
            f"/api/v1/appraisals/{appraisal.id}/self-submit/",
            {
                "answers": [
                    {"question_id": str(self.q_emp_1.id), "value": 5},
                    {"question_id": str(self.q_emp_2.id), "value": 5},
                    {"question_id": str(self.q_both.id), "value": 1},
                    {"question_id": str(info_question.id), "value": 5},
                ],
                "is_draft": False,
            },
            format="json",
        )
        self.assertEqual(employee_submit.status_code, status.HTTP_200_OK, employee_submit.data)
        appraisal.refresh_from_db()
        self.assertEqual(appraisal.self_score_total, Decimal("70.00"))
        self.assertEqual(appraisal.official_score, Decimal("70.00"))

    def test_next_phase_stays_locked_until_the_current_phase_is_submitted(self):
        first = self.version.phases.order_by("order").first()
        second = AppraisalPhase.objects.create(version=self.version, title="Follow up", order=2)
        follow_up = AppraisalSection.objects.create(
            version=self.version,
            phase=second,
            title="Follow up",
            kind=SectionKind.NARRATIVE,
            audience=SectionAudience.EMPLOYEE,
            order=1,
        )
        follow_question = add_question(
            follow_up,
            "Anything else",
            required=True,
            response_type=QuestionResponseType.LONG_TEXT,
        )

        self.client.force_authenticate(user=self.hr_user)
        self.client.post(f"/api/v1/appraisal-cycles/{self.cycle.id}/activate/")
        appraisal = EmployeeAppraisal.objects.get(employee=self.employee, cycle=self.cycle)

        self.client.force_authenticate(user=self.employee)
        blocked = self.client.post(
            f"/api/v1/appraisals/{appraisal.id}/self-submit/",
            {
                "phase_id": str(second.id),
                "answers": [{"question_id": str(follow_question.id), "value": "Later"}],
                "is_draft": False,
            },
            format="json",
        )
        self.assertEqual(blocked.status_code, status.HTTP_400_BAD_REQUEST, blocked.data)

        opened = self.client.post(
            f"/api/v1/appraisals/{appraisal.id}/self-submit/",
            {
                "phase_id": str(first.id),
                "answers": [
                    {"question_id": str(self.q_emp_1.id), "value": 5},
                    {"question_id": str(self.q_emp_2.id), "value": 5},
                    {"question_id": str(self.q_both.id), "value": 1},
                ],
                "is_draft": False,
            },
            format="json",
        )
        self.assertEqual(opened.status_code, status.HTTP_200_OK, opened.data)
        appraisal.refresh_from_db()
        self.assertEqual(appraisal.status, EmployeeAppraisalStatus.DRAFT)

        self.client.force_authenticate(user=self.manager)
        shared = self.client.post(
            f"/api/v1/appraisals/{appraisal.id}/supervisor-submit/",
            {
                "phase_id": str(first.id),
                "answers": [
                    {"question_id": str(self.q_sup_1.id), "value": 5},
                    {"question_id": str(self.q_sup_2.id), "value": 5},
                    {"question_id": str(self.q_both.id), "value": 1},
                ],
                "is_draft": False,
            },
            format="json",
        )
        self.assertEqual(shared.status_code, status.HTTP_200_OK, shared.data)
        appraisal.refresh_from_db()
        self.assertEqual(appraisal.status, EmployeeAppraisalStatus.DRAFT)

        self.client.force_authenticate(user=self.employee)
        finished = self.client.post(
            f"/api/v1/appraisals/{appraisal.id}/self-submit/",
            {
                "phase_id": str(second.id),
                "answers": [{"question_id": str(follow_question.id), "value": "Done"}],
                "is_draft": False,
            },
            format="json",
        )
        self.assertEqual(finished.status_code, status.HTTP_200_OK, finished.data)
        appraisal.refresh_from_db()
        self.assertEqual(appraisal.status, EmployeeAppraisalStatus.PENDING_APPRAISEE_ACCEPTANCE)
        self.assertEqual(
            appraisal.answers.get(question=follow_question, respondent="EMPLOYEE").value,
            "Done",
        )

    def test_team_template_overrides_department_and_org(self):
        unit = Unit.objects.create(name="Enterprise", department=self.sales_dept)
        team = Team.objects.create(name="Hunters", unit=unit)
        self.employee.unit = unit
        self.employee.team = team
        self.employee.save()

        team_template = AppraisalTemplate.objects.create(
            title="Hunters template",
            appraisal_type=self.appraisal_type,
            scope=TemplateScope.TEAM,
            team=team,
        )
        team_version = publish_version(team_template)
        section = add_section(
            version=team_version,
            title="Team notes",
            kind=SectionKind.NARRATIVE,
            audience=SectionAudience.EMPLOYEE,
        )
        add_question(section, "Team prompt", response_type=QuestionResponseType.LONG_TEXT)

        self.client.force_authenticate(user=self.hr_user)
        response = self.client.post(f"/api/v1/appraisal-cycles/{self.cycle.id}/activate/")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        appraisal = EmployeeAppraisal.objects.get(employee=self.employee, cycle=self.cycle)
        self.assertEqual(appraisal.template, team_template)
        self.assertEqual(appraisal.template_version, team_version)

    def test_publish_rejects_bad_weights_and_duplicate_target(self):
        self.client.force_authenticate(user=self.hr_user)
        created = self.client.post(
            "/api/v1/appraisal-templates/",
            {
                "title": "Broken weights",
                "appraisal_type": str(self.appraisal_type.id),
                "scope": "ORG",
                "sections": [
                    {
                        "title": "Only half",
                        "kind": "SCORED",
                        "audience": "SUPERVISOR",
                        "weight": "50.00",
                        "rating_scale_max": 5,
                        "questions": [
                            {
                                "prompt": "One question",
                                "response_type": "RATING",
                                "counts_toward_score": True,
                                "weight": "50.00",
                                "required": True,
                            }
                        ],
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(created.status_code, status.HTTP_400_BAD_REQUEST, created.data)
        self.assertEqual(created.data["existing_template_id"], str(self.org_template.id))

        draft = self.client.post(
            "/api/v1/appraisal-templates/",
            {
                "title": "Weight check",
                "appraisal_type": str(self.appraisal_type.id),
                "scope": "DEPARTMENT",
                "department": str(Department.objects.create(name="Finance").id),
                "sections": [
                    {
                        "title": "Only half",
                        "kind": "SCORED",
                        "audience": "SUPERVISOR",
                        "weight": "40.00",
                        "rating_scale_max": 5,
                        "questions": [
                            {
                                "prompt": "One question",
                                "response_type": "RATING",
                                "counts_toward_score": True,
                                "weight": "40.00",
                                "required": True,
                            }
                        ],
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(draft.status_code, status.HTTP_201_CREATED, draft.data)
        published = self.client.post(f"/api/v1/appraisal-templates/{draft.data['id']}/publish/")
        self.assertEqual(published.status_code, status.HTTP_400_BAD_REQUEST, published.data)
        self.assertIn("100", " ".join(published.data["detail"]))

    def test_later_publish_does_not_change_launched_appraisal(self):
        self.client.force_authenticate(user=self.hr_user)
        self.client.post(f"/api/v1/appraisal-cycles/{self.cycle.id}/activate/")
        appraisal = EmployeeAppraisal.objects.get(employee=self.employee, cycle=self.cycle)
        original_prompt = self.q_emp_1.prompt

        updated = self.client.patch(
            f"/api/v1/appraisal-templates/{self.template.id}/",
            {
                "sections": [
                    {
                        "title": "Sales Strategy",
                        "kind": "SCORED",
                        "audience": "EMPLOYEE",
                        "weight": "100.00",
                        "rating_scale_max": 5,
                        "questions": [
                            {
                                "prompt": "A rewritten prompt",
                                "response_type": "RATING",
                                "counts_toward_score": True,
                                "weight": "100.00",
                                "required": True,
                            }
                        ],
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(updated.status_code, status.HTTP_200_OK, updated.data)
        published = self.client.post(f"/api/v1/appraisal-templates/{self.template.id}/publish/")
        self.assertEqual(published.status_code, status.HTTP_200_OK, published.data)

        appraisal.refresh_from_db()
        pinned_prompts = list(
            appraisal.template_version.sections.get(audience="EMPLOYEE").questions.values_list("prompt", flat=True)
        )
        self.assertIn(original_prompt, pinned_prompts)
        self.assertNotIn("A rewritten prompt", pinned_prompts)
        self.assertEqual(published.data["published"]["sections"][0]["questions"][0]["prompt"], "A rewritten prompt")
        self.assertNotEqual(appraisal.template_version_id, published.data["published"]["id"])
