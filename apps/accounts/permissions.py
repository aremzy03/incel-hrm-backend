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
