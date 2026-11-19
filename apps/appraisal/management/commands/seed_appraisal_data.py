from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.appraisal.models import (
    AppraisalPhase,
    AppraisalQuestion,
    AppraisalSection,
    AppraisalTemplate,
    AppraisalTemplateVersion,
    AppraisalType,
    AppraisalTypeCode,
    QuestionResponseType,
    SectionAudience,
    SectionKind,
    TemplateScope,
    TemplateVersionStatus,
)


def equal_weights(count: int) -> list[Decimal]:
    if count <= 0:
        return []
    base = (Decimal("100") / Decimal(count)).quantize(Decimal("0.01"))
    weights = [base] * count
    weights[-1] = (Decimal("100") - base * (count - 1)).quantize(Decimal("0.01"))
    return weights


class Command(BaseCommand):
    help = "Seeds appraisal types and an organization-wide end-of-year template."

    def handle(self, *args, **options):
        self.stdout.write("Seeding appraisal types...")
        appraisal_types_data = [
            {
                "code": AppraisalTypeCode.END_OF_YEAR,
                "name": "End of Year Appraisal",
                "description": "Annual performance assessment covering achievements, job KPIs, and goals.",
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
            self.stdout.write(f"  [{'Created' if created else 'Exists'}] AppraisalType: {obj.name}")

        eoy_type = app_type_objs[AppraisalTypeCode.END_OF_YEAR]
        template = AppraisalTemplate.objects.filter(
            appraisal_type=eoy_type,
            scope=TemplateScope.ORG,
        ).first()
        if template and template.versions.filter(status=TemplateVersionStatus.PUBLISHED).exists():
            self.stdout.write(self.style.SUCCESS("Organization end-of-year template is already published."))
            return

        if template is None:
            template = AppraisalTemplate.objects.create(
                title="End of Year Appraisal",
                appraisal_type=eoy_type,
                scope=TemplateScope.ORG,
                description="Organization-wide end-of-year form. Department, unit, and team templates can override it.",
                is_active=True,
            )
            self.stdout.write(f"  [Created] AppraisalTemplate: {template.title}")

        version = AppraisalTemplateVersion.objects.create(
            template=template,
            version_number=(template.versions.order_by("-version_number").values_list("version_number", flat=True).first() or 0) + 1,
            status=TemplateVersionStatus.PUBLISHED,
            published_at=timezone.now(),
        )

        reflection_phase = AppraisalPhase.objects.create(version=version, title="Self reflection", order=1)
        ratings_phase = AppraisalPhase.objects.create(version=version, title="Performance ratings", order=2)
        review_phase = AppraisalPhase.objects.create(version=version, title="Supervisor review", order=3)

        reflection = AppraisalSection.objects.create(
            version=version,
            phase=reflection_phase,
            title="Self reflection",
            section_code="REFLECTION",
            kind=SectionKind.NARRATIVE,
            audience=SectionAudience.EMPLOYEE,
            order=1,
        )
        for index, prompt in enumerate(
            [
                "Key achievements based on the goals set in your previous appraisal",
                "Challenges you encountered",
                "Support you need from your manager and from management",
            ],
            start=1,
        ):
            AppraisalQuestion.objects.create(
                section=reflection,
                prompt=prompt,
                order=index,
                response_type=QuestionResponseType.LONG_TEXT,
                required=True,
            )

        scored_sections = [
            ("SALES STRATEGY & BUSINESS DEVELOPMENT", "SECTION_A", Decimal("15.00"), 5, [
                "Development of Sales Strategy: Develops and implements an effective sales strategy aligned with the Company's overall business objectives.",
                "Sales Planning: Ensures sales activities are focused on measurable commercial outcomes.",
                "Business Development: Develops strategic relationships that generate new business.",
            ]),
            ("REVENUE & TARGET ACHIEVEMENT", "SECTION_B", Decimal("20.00"), 5, [
                "Achievement of Sales Targets: Consistently works towards achieving or exceeding assigned sales targets.",
                "Revenue Growth: Identifies opportunities to increase sales from existing and new customers.",
                "Sales Conversion: Effectively converts leads and opportunities into confirmed business.",
                "Sales Productivity: Monitors calls, meetings, proposals, quotations, follow-ups, negotiations, and conversions.",
            ]),
            ("SALES PIPELINE & OPPORTUNITY MANAGEMENT", "SECTION_C", Decimal("20.00"), 4, [
                "Lead Management: Ensures leads are properly captured, assigned, followed up, and converted.",
                "Pipeline Management: Reviews pipeline quality, value, age, probability, and expected closing dates.",
                "Opportunity Tracking: Maintains accurate records of customer interactions, proposals, negotiations, and next actions.",
                "Sales Reporting & CRM Management: Ensures accurate and timely updating of CRM or approved sales records.",
            ]),
            ("CUSTOMER ACQUISITION, RETENTION & RELATIONSHIP MANAGEMENT", "SECTION_D", Decimal("20.00"), 5, [
                "Customer Acquisition: Develops effective strategies for acquiring new customers.",
                "Customer Retention: Monitors customer relationships and identifies potential customer attrition.",
                "Key Account Management: Identifies opportunities for account growth, upselling, and cross-selling.",
                "Customer Satisfaction: Uses customer feedback to improve sales and service processes.",
            ]),
            ("SALES TEAM LEADERSHIP & PEOPLE MANAGEMENT", "SECTION_E", Decimal("15.00"), 5, [
                "Sales Team Leadership: Provides clear direction and leadership to the Sales team.",
                "Coaching & Mentoring: Helps sales team members improve prospecting, negotiation, closing, account management, and customer engagement skills.",
                "Delegation & Accountability: Avoids excessive micromanagement while maintaining appropriate control.",
            ]),
            ("SALES & MARKETING COLLABORATION", "SECTION_F", Decimal("10.00"), 5, [
                "Collaboration with Marketing: Works closely with Marketing to align campaigns with sales objectives.",
                "Market Intelligence: Provides Marketing and Management with relevant information on customer preferences, competitor activities, pricing, and market trends.",
            ]),
        ]
        for order, (title, code, weight, scale, prompts) in enumerate(scored_sections, start=1):
            section = AppraisalSection.objects.create(
                version=version,
                phase=ratings_phase,
                title=title,
                section_code=code,
                kind=SectionKind.SCORED,
                audience=SectionAudience.SUPERVISOR,
                weight=weight,
                rating_scale_max=scale,
                order=order,
            )
            for index, (prompt, question_weight) in enumerate(zip(prompts, equal_weights(len(prompts))), start=1):
                AppraisalQuestion.objects.create(
                    section=section,
                    prompt=prompt,
                    order=index,
                    response_type=QuestionResponseType.RATING,
                    counts_toward_score=True,
                    required=True,
                    weight=question_weight,
                )

        review = AppraisalSection.objects.create(
            version=version,
            phase=review_phase,
            title="Supervisor review",
            section_code="REVIEW",
            kind=SectionKind.NARRATIVE,
            audience=SectionAudience.SUPERVISOR,
            order=8,
        )
        for index, prompt in enumerate(
            [
                "Key strengths",
                "Areas for development",
                "Training and development needs",
                "Goals for the next year",
            ],
            start=1,
        ):
            AppraisalQuestion.objects.create(
                section=review,
                prompt=prompt,
                order=index,
                response_type=QuestionResponseType.LONG_TEXT,
                required=True,
            )
        AppraisalQuestion.objects.create(
            section=review,
            prompt="Outcome recommendations",
            order=5,
            response_type=QuestionResponseType.MULTI_SELECT,
            required=False,
            options=[
                "Letter of commendation",
                "Bonus or gift",
                "Promotion",
                "Performance improvement plan",
                "No change",
            ],
        )

        self.stdout.write(self.style.SUCCESS("Appraisal data successfully seeded."))
