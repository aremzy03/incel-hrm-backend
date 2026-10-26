from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Department, Role, RoleName, UserRole
from apps.appraisal.models import (
    AppraisalCycle,
    AppraisalCycleStatus,
    AppraisalQuestion,
    AppraisalQuestionAnswer,
    AppraisalSection,
    AppraisalTemplate,
    AppraisalType,
    AppraisalTypeCode,
    EmployeeAppraisal,
    EmployeeAppraisalStatus,
    PerformanceCategory,
)
from apps.appraisal.services import calculate_appraisal_scores

User = get_user_model()


def ensure_role(name: str) -> Role:
    role, _ = Role.objects.get_or_create(name=name, defaults={"description": name})
    return role


def make_user(email: str, password="testpass123", roles=None, department=None):
    user = User.objects.create_user(
        email=email,
        password=password,
        first_name="Test",
        last_name="User",
        department=department,
    )
    for r in roles or []:
        role = ensure_role(r)
        UserRole.objects.get_or_create(user=user, role=role)
    return user


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

        # Appraisal Type & Template
        self.appraisal_type = AppraisalType.objects.create(
            name="End of Year Appraisal",
            code=AppraisalTypeCode.END_OF_YEAR,
        )

        self.template = AppraisalTemplate.objects.create(
            title="Sales End of Year Template",
            appraisal_type=self.appraisal_type,
            department=self.sales_dept,
        )

        # Section 1: 50% weight, rating scale max 5
        self.sec1 = AppraisalSection.objects.create(
            template=self.template,
            title="Sales Strategy",
            section_code="SEC_1",
            weight=Decimal("50.00"),
            rating_scale_max=5,
            order=1,
        )
        self.q1_1 = AppraisalQuestion.objects.create(section=self.sec1, prompt="Develop Strategy", order=1)
        self.q1_2 = AppraisalQuestion.objects.create(section=self.sec1, prompt="Execute Plan", order=2)

        # Section 2: 50% weight, rating scale max 5
        self.sec2 = AppraisalSection.objects.create(
            template=self.template,
            title="Revenue Achievement",
            section_code="SEC_2",
            weight=Decimal("50.00"),
            rating_scale_max=5,
            order=2,
        )
        self.q2_1 = AppraisalQuestion.objects.create(section=self.sec2, prompt="Hit Targets", order=1)
        self.q2_2 = AppraisalQuestion.objects.create(section=self.sec2, prompt="Grow Accounts", order=2)

        # Appraisal Cycle
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
        self.assertEqual(appraisal.answers.count(), 4)

    def test_end_to_end_appraisal_lifecycle(self):
        # 1. Activate Cycle
        self.client.force_authenticate(user=self.hr_user)
        self.client.post(f"/api/v1/appraisal-cycles/{self.cycle.id}/activate/")

        appraisal = EmployeeAppraisal.objects.get(employee=self.employee, cycle=self.cycle)

        # 2. Phase 1 & 2 Employee Self-Assessment
        self.client.force_authenticate(user=self.employee)
        url_self = f"/api/v1/appraisals/{appraisal.id}/self-submit/"
        payload_self = {
            "key_achievements": "Achieved 120% of Q3 target",
            "noteworthy_achievements": "Closed key Enterprise account",
            "challenges_encountered": "Lead generation pipeline bottleneck",
            "support_needed": "Additional SDR support",
            "ratings": [
                {"question_id": str(self.q1_1.id), "self_rating": 4},
                {"question_id": str(self.q1_2.id), "self_rating": 4},
                {"question_id": str(self.q2_1.id), "self_rating": 5},
                {"question_id": str(self.q2_2.id), "self_rating": 5},
            ],
            "is_draft": False,
        }
        resp_self = self.client.post(url_self, payload_self, format="json")
        self.assertEqual(resp_self.status_code, status.HTTP_200_OK)

        appraisal.refresh_from_db()
        self.assertEqual(appraisal.status, EmployeeAppraisalStatus.SELF_SUBMITTED)
        # Self score calculation: Sec1: (8/10)*50 = 40.00%, Sec2: (10/10)*50 = 50.00%. Total = 90.00%
        self.assertEqual(appraisal.self_score_total, Decimal("90.00"))

        # 3. Phase 2 & 3 Supervisor Assessment
        self.client.force_authenticate(user=self.manager)
        url_sup = f"/api/v1/appraisals/{appraisal.id}/supervisor-submit/"
        payload_sup = {
            "ratings": [
                {"question_id": str(self.q1_1.id), "supervisor_rating": 5},
                {"question_id": str(self.q1_2.id), "supervisor_rating": 5},
                {"question_id": str(self.q2_1.id), "supervisor_rating": 4},
                {"question_id": str(self.q2_2.id), "supervisor_rating": 4},
            ],
            "strengths": "Strong sales leadership and deal closing skills",
            "areas_for_development": "Time management and CRM updates",
            "training_development_needs": "Advanced Enterprise Negotiation Course",
            "goals_next_year": "Expand regional enterprise market share by 25%",
            "recommendations": ["COMMENDATION_LETTER", "BONUS_GIFTS"],
            "supervisor_comments": "Exceptional contributor throughout the year",
            "is_draft": False,
        }
        resp_sup = self.client.post(url_sup, payload_sup, format="json")
        self.assertEqual(resp_sup.status_code, status.HTTP_200_OK)

        appraisal.refresh_from_db()
        self.assertEqual(appraisal.status, EmployeeAppraisalStatus.PENDING_APPRAISEE_ACCEPTANCE)
        # Supervisor score calculation: Sec1: (10/10)*50 = 50.00%, Sec2: (8/10)*50 = 40.00%. Total = 90.00%
        self.assertEqual(appraisal.supervisor_score_total, Decimal("90.00"))
        self.assertEqual(appraisal.performance_category, PerformanceCategory.EXCEPTIONAL)

        # 4. Phase 4 Employee Sign-Off
        self.client.force_authenticate(user=self.employee)
        url_signoff = f"/api/v1/appraisals/{appraisal.id}/appraisee-signoff/"
        resp_signoff = self.client.post(url_signoff, {"appraisee_agrees": True, "appraisee_additional_comments": "Thank you for the support!"})
        self.assertEqual(resp_signoff.status_code, status.HTTP_200_OK)

        appraisal.refresh_from_db()
        self.assertEqual(appraisal.status, EmployeeAppraisalStatus.PENDING_HR_CALIBRATION)

        # 5. HR Calibration
        self.client.force_authenticate(user=self.hr_user)
        url_hr = f"/api/v1/appraisals/{appraisal.id}/hr-calibrate/"
        resp_hr = self.client.post(url_hr, {"hr_observations": "Clean record. Outstanding performance.", "hr_recommended_action": "Approve bonus & commendation"})
        self.assertEqual(resp_hr.status_code, status.HTTP_200_OK)

        appraisal.refresh_from_db()
        self.assertEqual(appraisal.status, EmployeeAppraisalStatus.PENDING_MANAGEMENT_DECISION)

        # 6. Management / Committee Finalize
        url_finalize = f"/api/v1/appraisals/{appraisal.id}/finalize/"
        resp_fin = self.client.post(url_finalize, {"management_comments": "Approved by Committee", "committee_decision": "Award Exceptional Performer Bonus"})
        self.assertEqual(resp_fin.status_code, status.HTTP_200_OK)

        appraisal.refresh_from_db()
        self.assertEqual(appraisal.status, EmployeeAppraisalStatus.FINALIZED)
        self.assertIsNotNone(appraisal.finalized_at)

    def test_employee_disagree_requires_reason(self):
        self.client.force_authenticate(user=self.hr_user)
        self.client.post(f"/api/v1/appraisal-cycles/{self.cycle.id}/activate/")

        appraisal = EmployeeAppraisal.objects.get(employee=self.employee, cycle=self.cycle)
        appraisal.status = EmployeeAppraisalStatus.PENDING_APPRAISEE_ACCEPTANCE
        appraisal.save()

        self.client.force_authenticate(user=self.employee)
        url_signoff = f"/api/v1/appraisals/{appraisal.id}/appraisee-signoff/"
        resp_fail = self.client.post(url_signoff, {"appraisee_agrees": False})
        self.assertEqual(resp_fail.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("appraisee_disagree_reason", resp_fail.data)

        resp_ok = self.client.post(url_signoff, {"appraisee_agrees": False, "appraisee_disagree_reason": "I disagree with the rating on Section 2."})
        self.assertEqual(resp_ok.status_code, status.HTTP_200_OK)
