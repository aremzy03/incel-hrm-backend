"""Leave lifecycle hooks (termination forfeiture)."""

from django.contrib.auth import get_user_model
from django.db.models.signals import post_delete, pre_save
from django.dispatch import receiver

from .models import LeaveAttachment

User = get_user_model()


@receiver(post_delete, sender=LeaveAttachment)
def delete_leave_attachment_file(sender, instance, **kwargs):
    """Remove the stored object when the row is deleted, including cascades."""
    name = instance.file.name
    if not name:
        return
    instance.file.storage.delete(name)


@receiver(pre_save, sender=User)
def forfeit_leave_on_deactivation(sender, instance, **kwargs):
    """When an active employee is deactivated, forfeit remaining days if policy says so."""
    if not instance.pk:
        return
    try:
        previous = sender.objects.get(pk=instance.pk)
    except sender.DoesNotExist:
        return
    if previous.is_active and not instance.is_active:
        from .services import forfeit_balances_on_termination

        forfeit_balances_on_termination(instance)
