"""Signal handlers for the accounts app."""

from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from .models import Role, RoleName, User, UserRole


@receiver(post_save, sender=User)
def assign_employee_role_on_create(sender, instance, created, **kwargs):
    """Assign the EMPLOYEE role to every newly created user."""
    if created:
        role = Role.objects.filter(name=RoleName.EMPLOYEE).first()
        if role:
            UserRole.objects.get_or_create(user=instance, role=role)


@receiver(post_save, sender=User)
def create_leave_balances_on_user_create(sender, instance, created, **kwargs):
    """Create default leave balances for newly created users."""
    if created:
        from apps.leave.models import LeaveBalance
        from apps.leave.services import get_eligible_leave_types

        year = timezone.now().year
        for leave_type in get_eligible_leave_types(instance):
            LeaveBalance.objects.get_or_create(
                employee=instance,
                leave_type=leave_type,
                year=year,
                defaults={"allocated_days": leave_type.default_days},
            )
