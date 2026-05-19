from rest_framework.permissions import BasePermission

from .models import RoleName


class _HasRole(BasePermission):
    """Base class — subclasses declare `role_name`."""

    role_name: str = ""

    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.has_role(self.role_name)
        )


class IsEmployee(_HasRole):
    role_name = RoleName.EMPLOYEE


class IsLineManager(_HasRole):
    role_name = RoleName.LINE_MANAGER


class IsHR(_HasRole):
    role_name = RoleName.HR


class IsExecutiveDirector(_HasRole):
    role_name = RoleName.EXECUTIVE_DIRECTOR


class IsManagingDirector(_HasRole):
    role_name = RoleName.MANAGING_DIRECTOR


class IsSupervisor(_HasRole):
    role_name = RoleName.SUPERVISOR


class IsOwnerOrHR(BasePermission):
    """
    Object-level permission: the target user, HR role, or Django staff may access the object.
    Used for personnel detail (self-service + HR).
    """

    def has_object_permission(self, request, view, obj):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if getattr(obj, "pk", None) == user.pk:
            return True
        if user.has_role(RoleName.HR):
            return True
        if user.is_staff:
            return True
        return False
