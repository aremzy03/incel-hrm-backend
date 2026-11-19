from django.db import migrations, models

ATTACHMENT_REQUIRED_CODES = ("SICK", "MATERNITY")


def seed_requires_attachment(apps, schema_editor):
    LeavePolicy = apps.get_model("leave", "LeavePolicy")
    for policy in LeavePolicy.objects.select_related("leave_type").all():
        code = (policy.leave_type.code or "").upper()
        if code in ATTACHMENT_REQUIRED_CODES and not policy.requires_attachment:
            policy.requires_attachment = True
            policy.save(update_fields=["requires_attachment"])


def clear_requires_attachment(apps, schema_editor):
    LeavePolicy = apps.get_model("leave", "LeavePolicy")
    for policy in LeavePolicy.objects.select_related("leave_type").all():
        code = (policy.leave_type.code or "").upper()
        if code in ATTACHMENT_REQUIRED_CODES and policy.requires_attachment:
            policy.requires_attachment = False
            policy.save(update_fields=["requires_attachment"])


class Migration(migrations.Migration):

    dependencies = [
        (
            "leave",
            "0021_rename_leave_appro_user_id_idx_leave_appro_user_id_91057a_idx_and_more",
        ),
    ]

    operations = [
        migrations.AddField(
            model_name="leavepolicy",
            name="requires_attachment",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "When True, a supporting document must be attached before submission. "
                    "Emergency sick leave is exempt."
                ),
            ),
        ),
        migrations.RunPython(seed_requires_attachment, clear_requires_attachment),
    ]
