import uuid
from decimal import Decimal

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def forwards(apps, schema_editor):
    AppraisalTemplate = apps.get_model("appraisal", "AppraisalTemplate")
    AppraisalTemplateVersion = apps.get_model("appraisal", "AppraisalTemplateVersion")
    AppraisalSection = apps.get_model("appraisal", "AppraisalSection")
    AppraisalQuestion = apps.get_model("appraisal", "AppraisalQuestion")
    EmployeeAppraisal = apps.get_model("appraisal", "EmployeeAppraisal")

    used_keys = set(
        AppraisalTemplate.objects.exclude(scope_key="").values_list("appraisal_type_id", "scope_key")
    )
    for template in AppraisalTemplate.objects.all():
        if template.department_id:
            scope = "DEPARTMENT"
            scope_key = f"DEPARTMENT:{template.department_id}"
        else:
            scope = "ORG"
            scope_key = "ORG"
        if (template.appraisal_type_id, scope_key) in used_keys:
            scope = ""
            scope_key = f"UNASSIGNED:{template.pk}"
        template.scope = scope
        template.scope_key = scope_key
        template.save(update_fields=["scope", "scope_key"])
        used_keys.add((template.appraisal_type_id, scope_key))

        version = AppraisalTemplateVersion.objects.create(
            id=uuid.uuid4(),
            template=template,
            version_number=1,
            status="PUBLISHED",
        )
        sections = AppraisalSection.objects.filter(template_id=template.pk)
        for section in sections:
            section.version = version
            section.kind = "SCORED"
            section.audience = "SUPERVISOR"
            section.save(update_fields=["version", "kind", "audience"])
            questions = list(section.questions.all().order_by("order", "created_at"))
            count = len(questions)
            if not count:
                continue
            base = (Decimal("100") / Decimal(count)).quantize(Decimal("0.01"))
            weights = [base] * count
            weights[-1] = (Decimal("100") - base * (count - 1)).quantize(Decimal("0.01"))
            for question, weight in zip(questions, weights):
                question.response_type = "RATING"
                question.counts_toward_score = True
                question.required = True
                question.weight = weight
                question.save(
                    update_fields=["response_type", "counts_toward_score", "required", "weight"]
                )

    for appraisal in EmployeeAppraisal.objects.all():
        appraisal.official_score = appraisal.supervisor_score_total
        appraisal.save(update_fields=["official_score"])


def backwards(apps, schema_editor):
    AppraisalSection = apps.get_model("appraisal", "AppraisalSection")
    AppraisalTemplateVersion = apps.get_model("appraisal", "AppraisalTemplateVersion")
    for section in AppraisalSection.objects.select_related("version"):
        if section.version_id and section.template_id is None:
            section.template_id = section.version.template_id
            section.save(update_fields=["template"])
    AppraisalTemplateVersion.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("appraisal", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AppraisalTemplateVersion",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("version_number", models.PositiveIntegerField()),
                ("status", models.CharField(choices=[("DRAFT", "Draft"), ("PUBLISHED", "Published"), ("ARCHIVED", "Archived")], default="DRAFT", max_length=20)),
                ("published_at", models.DateTimeField(blank=True, null=True)),
                ("published_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="published_appraisal_template_versions", to=settings.AUTH_USER_MODEL)),
                ("template", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="versions", to="appraisal.appraisaltemplate")),
            ],
            options={
                "verbose_name": "Appraisal Template Version",
                "verbose_name_plural": "Appraisal Template Versions",
                "ordering": ["-version_number"],
            },
        ),
        migrations.AddField(
            model_name="appraisaltemplate",
            name="scope",
            field=models.CharField(blank=True, choices=[("ORG", "Organization-wide"), ("DEPARTMENT", "Department"), ("UNIT", "Unit"), ("TEAM", "Team")], default="", max_length=20),
        ),
        migrations.AddField(
            model_name="appraisaltemplate",
            name="scope_key",
            field=models.CharField(default="", editable=False, max_length=80),
        ),
        migrations.AddField(
            model_name="appraisaltemplate",
            name="unit",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="appraisal_templates", to="accounts.unit"),
        ),
        migrations.AddField(
            model_name="appraisaltemplate",
            name="team",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="appraisal_templates", to="accounts.team"),
        ),
        migrations.AddField(
            model_name="appraisalsection",
            name="version",
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name="sections", to="appraisal.appraisaltemplateversion"),
        ),
        migrations.AddField(
            model_name="appraisalsection",
            name="kind",
            field=models.CharField(choices=[("SCORED", "Scored"), ("INFORMATIONAL_RATING", "Informational rating"), ("NARRATIVE", "Narrative")], default="NARRATIVE", max_length=30),
        ),
        migrations.AddField(
            model_name="appraisalsection",
            name="audience",
            field=models.CharField(choices=[("EMPLOYEE", "Employee"), ("SUPERVISOR", "Supervisor"), ("BOTH", "Employee and supervisor"), ("HR", "HR"), ("MANAGEMENT", "Management")], default="EMPLOYEE", max_length=20),
        ),
        migrations.AlterField(
            model_name="appraisalsection",
            name="weight",
            field=models.DecimalField(decimal_places=2, default=Decimal("0.00"), help_text="Percentage weight of this section in the official score. Only scored sections use this.", max_digits=5, validators=[django.core.validators.MinValueValidator(Decimal("0.00")), django.core.validators.MaxValueValidator(Decimal("100.00"))]),
        ),
        migrations.AddField(
            model_name="appraisalquestion",
            name="response_type",
            field=models.CharField(choices=[("RATING", "Rating"), ("SHORT_TEXT", "Short text"), ("LONG_TEXT", "Long text"), ("YES_NO", "Yes / No"), ("MULTI_SELECT", "Multi-select")], default="RATING", max_length=20),
        ),
        migrations.AddField(
            model_name="appraisalquestion",
            name="counts_toward_score",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="appraisalquestion",
            name="required",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="appraisalquestion",
            name="weight",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True, validators=[django.core.validators.MinValueValidator(Decimal("0.00")), django.core.validators.MaxValueValidator(Decimal("100.00"))]),
        ),
        migrations.AddField(
            model_name="appraisalquestion",
            name="options",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="employeeappraisal",
            name="template_version",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="employee_appraisals", to="appraisal.appraisaltemplateversion"),
        ),
        migrations.AddField(
            model_name="employeeappraisal",
            name="official_score",
            field=models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=5),
        ),
        migrations.AddField(
            model_name="appraisalquestionanswer",
            name="respondent",
            field=models.CharField(choices=[("EMPLOYEE", "Employee"), ("SUPERVISOR", "Supervisor"), ("HR", "HR"), ("MANAGEMENT", "Management")], default="EMPLOYEE", max_length=20),
        ),
        migrations.AddField(
            model_name="appraisalquestionanswer",
            name="value",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.RunPython(forwards, backwards),
        migrations.AlterField(
            model_name="appraisalsection",
            name="version",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="sections", to="appraisal.appraisaltemplateversion"),
        ),
        migrations.RemoveField(
            model_name="appraisalsection",
            name="template",
        ),
        migrations.AlterUniqueTogether(
            name="appraisalquestionanswer",
            unique_together={("appraisal", "question", "respondent")},
        ),
        migrations.AddConstraint(
            model_name="appraisaltemplate",
            constraint=models.UniqueConstraint(fields=("appraisal_type", "scope_key"), name="unique_appraisal_template_target"),
        ),
        migrations.AddConstraint(
            model_name="appraisaltemplateversion",
            constraint=models.UniqueConstraint(fields=("template", "version_number"), name="unique_template_version_number"),
        ),
        migrations.AddConstraint(
            model_name="appraisaltemplateversion",
            constraint=models.UniqueConstraint(condition=models.Q(status="DRAFT"), fields=("template",), name="one_draft_version_per_template"),
        ),
        migrations.AddConstraint(
            model_name="appraisaltemplateversion",
            constraint=models.UniqueConstraint(condition=models.Q(status="PUBLISHED"), fields=("template",), name="one_published_version_per_template"),
        ),
    ]
