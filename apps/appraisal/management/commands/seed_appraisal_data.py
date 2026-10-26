from decimal import Decimal
from django.core.management.base import BaseCommand
from apps.accounts.models import Department
from apps.appraisal.models import (
    AppraisalQuestion,
    AppraisalSection,
    AppraisalTemplate,
    AppraisalType,
    AppraisalTypeCode,
)


class Command(BaseCommand):
    help = "Seeds appraisal types and initial Sales End of Year appraisal template from sample Excel sheet."

    def handle(self, *args, **options):
        self.stdout.write("Seeding appraisal types...")

        appraisal_types_data = [
            {
                "code": AppraisalTypeCode.END_OF_YEAR,
                "name": "End of Year Appraisal",
                "description": "Annual performance assessment covering key achievements, job KPIs, qualitative strengths, and goal setting.",
            },
            {
                "code": AppraisalTypeCode.PROBATION_90_DAY,
                "name": "90-Day Performance Check",
                "description": "Probationary milestone check for newly hired staff.",
            },
            {
                "code": AppraisalTypeCode.PROBATION_6_MONTH,
                "name": "6-Month Confirmation Appraisal",
                "description": "Confirmation appraisal for staff completing 6 months probation.",
            },
            {
                "code": AppraisalTypeCode.BI_ANNUAL_JULY,
                "name": "Bi-Annual July Appraisal",
                "description": "Mid-year review cycle.",
            },
            {
                "code": AppraisalTypeCode.BI_ANNUAL_DECEMBER,
                "name": "Bi-Annual December Appraisal",
                "description": "Second half bi-annual review cycle.",
            },
        ]

        app_type_objs = {}
        for item in appraisal_types_data:
            obj, created = AppraisalType.objects.get_or_create(
                code=item["code"],
                defaults={"name": item["name"], "description": item["description"]},
            )
            app_type_objs[item["code"]] = obj
            status_str = "Created" if created else "Exists"
            self.stdout.write(f"  [{status_str}] AppraisalType: {obj.name}")

        eoy_type = app_type_objs[AppraisalTypeCode.END_OF_YEAR]

        # Seed Sales Department template if Sales department exists, else general template
        sales_dept = Department.objects.filter(name__icontains="Sales").first()

        template, created = AppraisalTemplate.objects.get_or_create(
            title="Sales Department End of Year Appraisal Template",
            appraisal_type=eoy_type,
            defaults={
                "department": sales_dept,
                "description": "Standard 4-Phase End of Year appraisal form for Sales team based on company performance matrix.",
                "is_active": True,
            },
        )
        status_str = "Created" if created else "Exists"
        self.stdout.write(f"  [{status_str}] AppraisalTemplate: {template.title}")

        # Define Sections & Questions from Excel sample
        sections_data = [
            {
                "title": "SALES STRATEGY & BUSINESS DEVELOPMENT – 15%",
                "section_code": "SECTION_A",
                "weight": Decimal("15.00"),
                "rating_scale_max": 5,
                "order": 1,
                "questions": [
                    "Development of Sales Strategy: Develops and implements an effective sales strategy aligned with the Company's overall business objectives.",
                    "Sales Planning: Ensures sales activities are focused on measurable commercial outcomes.",
                    "Business Development: Develops strategic relationships that generate new business.",
                ],
            },
            {
                "title": "REVENUE & TARGET ACHIEVEMENT – 20%",
                "section_code": "SECTION_B",
                "weight": Decimal("20.00"),
                "rating_scale_max": 5,
                "order": 2,
                "questions": [
                    "Achievement of Sales Targets: Consistently works towards achieving or exceeding assigned sales targets.",
                    "Revenue Growth: Identifies opportunities to increase sales from existing and new customers.",
                    "Sales Conversion: Effectively converts leads and opportunities into confirmed business.",
                    "Sales Productivity: Monitors calls, meetings, proposals, quotations, follow-ups, negotiations, and conversions.",
                ],
            },
            {
                "title": "SALES PIPELINE & OPPORTUNITY MANAGEMENT – 20%",
                "section_code": "SECTION_C",
                "weight": Decimal("20.00"),
                "rating_scale_max": 4,
                "order": 3,
                "questions": [
                    "Lead Management: Ensures leads are properly captured, assigned, followed up, and converted.",
                    "Pipeline Management: Reviews pipeline quality, value, age, probability, and expected closing dates.",
                    "Opportunity Tracking: Maintains accurate records of customer interactions, proposals, negotiations, and next actions.",
                    "Sales Reporting & CRM Management: Ensures accurate and timely updating of CRM or approved sales records.",
                ],
            },
            {
                "title": "CUSTOMER ACQUISITION, RETENTION & RELATIONSHIP MANAGEMENT – 20%",
                "section_code": "SECTION_D",
                "weight": Decimal("20.00"),
                "rating_scale_max": 5,
                "order": 4,
                "questions": [
                    "Customer Acquisition: Develops effective strategies for acquiring new customers.",
                    "Customer Retention: Monitors customer relationships and identifies potential customer attrition.",
                    "Key Account Management: Identifies opportunities for account growth, upselling, and cross-selling.",
                    "Customer Satisfaction: Uses customer feedback to improve sales and service processes.",
                ],
            },
            {
                "title": "SALES TEAM LEADERSHIP & PEOPLE MANAGEMENT – 15%",
                "section_code": "SECTION_E",
                "weight": Decimal("15.00"),
                "rating_scale_max": 5,
                "order": 5,
                "questions": [
                    "Sales Team Leadership: Provides clear direction and leadership to the Sales team.",
                    "Coaching & Mentoring: Helps sales team members improve prospecting, negotiation, closing, account management, and customer engagement skills.",
                    "Delegation & Accountability: Avoids excessive micromanagement while maintaining appropriate control.",
                ],
            },
            {
                "title": "SALES & MARKETING COLLABORATION – 10%",
                "section_code": "SECTION_F",
                "weight": Decimal("10.00"),
                "rating_scale_max": 5,
                "order": 6,
                "questions": [
                    "Collaboration with Marketing: Works closely with Marketing to align campaigns with sales objectives.",
                    "Market Intelligence: Provides Marketing and Management with relevant information on customer preferences, competitor activities, pricing, and market trends.",
                ],
            },
        ]

        for sec_info in sections_data:
            section_obj, sec_created = AppraisalSection.objects.get_or_create(
                template=template,
                section_code=sec_info["section_code"],
                defaults={
                    "title": sec_info["title"],
                    "weight": sec_info["weight"],
                    "rating_scale_max": sec_info["rating_scale_max"],
                    "order": sec_info["order"],
                },
            )
            sec_status = "Created" if sec_created else "Exists"
            self.stdout.write(f"    [{sec_status}] Section: {section_obj.title}")

            for idx, q_prompt in enumerate(sec_info["questions"], start=1):
                q_obj, q_created = AppraisalQuestion.objects.get_or_create(
                    section=section_obj,
                    prompt=q_prompt,
                    defaults={"order": idx},
                )
                q_status = "Created" if q_created else "Exists"
                self.stdout.write(f"      [{q_status}] Question {idx}: {q_prompt[:40]}...")

        self.stdout.write(self.style.SUCCESS("Appraisal data successfully seeded!"))
