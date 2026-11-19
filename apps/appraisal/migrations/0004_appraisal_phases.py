import uuid

import django.db.models.deletion
from django.db import migrations, models


def wrap_existing_sections(apps, schema_editor):
    Version = apps.get_model("appraisal", "AppraisalTemplateVersion")
    Phase = apps.get_model("appraisal", "AppraisalPhase")
    Section = apps.get_model("appraisal", "AppraisalSection")
    for version in Version.objects.all():
        phase = Phase.objects.create(version=version, title="Phase 1", order=1)
        Section.objects.filter(version=version, phase__isnull=True).update(phase=phase)


class Migration(migrations.Migration):

    dependencies = [
        ("appraisal", "0003_align_template_field_help"),
    ]

    operations = [
        migrations.CreateModel(
            name="AppraisalPhase",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("title", models.CharField(max_length=255)),
                ("order", models.PositiveIntegerField(default=1)),
                (
                    "version",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="phases",
                        to="appraisal.appraisaltemplateversion",
                    ),
                ),
            ],
            options={
                "verbose_name": "Appraisal Phase",
                "verbose_name_plural": "Appraisal Phases",
                "ordering": ["order", "created_at"],
            },
        ),
        migrations.AddField(
            model_name="appraisalsection",
            name="phase",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="sections",
                to="appraisal.appraisalphase",
            ),
        ),
        migrations.RunPython(wrap_existing_sections, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="appraisalsection",
            name="phase",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="sections",
                to="appraisal.appraisalphase",
            ),
        ),
    ]
