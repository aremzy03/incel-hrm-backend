from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import generics, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    Department,
    DepartmentMembership,
    Team,
    Unit,
    get_or_create_hr_department,
    get_or_create_management_department,
    Role,
    RoleName,
    UserRole,
)
from .throttles import RegisterThrottle
from .permissions import IsExecutiveDirector, IsHR
from .serializers import (
    BulkUserIdsSerializer,
    DepartmentLineManagerSerializer,
    DepartmentSerializer,
    RegisterSerializer,
    RoleSerializer,
    TeamSerializer,
    UnitSerializer,
    UserCreateSerializer,
    UserDepartmentUpdateSerializer,
    UserRoleSerializer,
    UserSelfUpdateSerializer,
    UserSerializer,
    UserUpdateSerializer,
)

User = get_user_model()

__all__ = [
    "RegisterView",
    "MeView",
    "UserProfileView",
    "UserViewSet",
    "RoleViewSet",
    "AssignRoleView",
    "RemoveRoleView",
    "DepartmentViewSet",
    "UserDepartmentUpdateView",
    "DepartmentLineManagerView",
    "DepartmentMembersView",
    "DepartmentBulkAddMembersView",
    "DepartmentBulkRemoveMembersView",
    "UnitViewSet",
    "TeamViewSet",
]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class RegisterView(generics.CreateAPIView):
    """POST /api/v1/auth/register/"""

    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]
    throttle_classes = [RegisterThrottle]

    def create(self, request, *args, **kwargs):
        if not settings.REGISTRATION_OPEN:
            return Response(
                {"detail": "Registration is disabled."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)


class MeView(APIView):
    """GET /api/v1/auth/me/"""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)


class UserProfileView(APIView):
    """
    GET   /api/v1/profile/        — authenticated user profile
    PATCH /api/v1/profile/        — update own basic profile fields
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)

    def patch(self, request):
        serializer = UserSelfUpdateSerializer(
            instance=request.user,
            data=request.data,
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(UserSerializer(request.user).data)


# ---------------------------------------------------------------------------
# Users (HR CRUD)
# ---------------------------------------------------------------------------

class UserViewSet(viewsets.ModelViewSet):
    """
    GET    /api/v1/users/       — list users   (HR or admin)
    POST   /api/v1/users/       — create user  (HR or admin)
    GET    /api/v1/users/:id/   — retrieve     (HR or admin)
    PUT    /api/v1/users/:id/   — full update  (HR or admin)
    PATCH  /api/v1/users/:id/   — partial      (HR or admin)
    DELETE /api/v1/users/:id/   — delete       (HR or admin)
    """

    queryset = User.objects.select_related("department").all()
    permission_classes = [permissions.IsAuthenticated, IsHR | permissions.IsAdminUser]

    def get_serializer_class(self):
        if self.action == "create":
            return UserCreateSerializer
        if self.action in ("update", "partial_update"):
            return UserUpdateSerializer
        return UserSerializer


# ---------------------------------------------------------------------------
# Departments
# ---------------------------------------------------------------------------

class DepartmentViewSet(viewsets.ModelViewSet):
    """
    GET    /api/v1/departments/       — auth required unless PUBLIC_DEPARTMENT_ACCESS
    POST   /api/v1/departments/       — HR or admin
    GET    /api/v1/departments/:id/   — same as list
    PUT    /api/v1/departments/:id/   — HR or admin
    PATCH  /api/v1/departments/:id/   — HR or admin
    DELETE /api/v1/departments/:id/   — HR or admin
    """

    queryset = Department.objects.all()
    serializer_class = DepartmentSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            if settings.PUBLIC_DEPARTMENT_ACCESS:
                return [permissions.AllowAny()]
            return [permissions.IsAuthenticated()]
        return [permissions.IsAuthenticated(), (IsHR | permissions.IsAdminUser)()]


class UserDepartmentUpdateView(APIView):
    """PATCH /api/v1/users/:id/department/ — HR or admin only."""

    permission_classes = [permissions.IsAuthenticated, IsHR | permissions.IsAdminUser]

    def patch(self, request, user_id):
        user = get_object_or_404(User, pk=user_id)
        serializer = UserDepartmentUpdateSerializer(data=request.data, context={"user": user})
        serializer.is_valid(raise_exception=True)
        # allow {"department": null} to clear the department
        if "department" not in serializer.validated_data:
            raise ValidationError({"department": "This field is required."})

        user.department = serializer.validated_data["department"]
        user.save(update_fields=["department", "updated_at"])
        return Response(UserSerializer(user).data)


class DepartmentLineManagerView(APIView):
    """
    POST   /api/v1/departments/:id/line-manager/ — assign line manager
    DELETE /api/v1/departments/:id/line-manager/ — revoke line manager
    Restricted to HR, Executive Director, or admin.
    """

    permission_classes = [
        permissions.IsAuthenticated,
        IsHR | IsExecutiveDirector | permissions.IsAdminUser,
    ]

    def post(self, request, pk):
        department = get_object_or_404(Department, pk=pk)
        serializer = DepartmentLineManagerSerializer(
            data=request.data,
            context={"department": department},
        )
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]

        # Ensure the LINE_MANAGER Role row exists, then add it (do not clear other roles).
        lm_role, _ = Role.objects.get_or_create(
            name=RoleName.LINE_MANAGER,
            defaults={"description": "Line Manager"},
        )
        UserRole.objects.get_or_create(user=user, role=lm_role)

        mgmt = get_or_create_management_department()
        DepartmentMembership.objects.get_or_create(user=user, department=mgmt)

        department.line_manager = user
        department.save(update_fields=["line_manager", "updated_at"])
        return Response(DepartmentSerializer(department).data)

    def delete(self, request, pk):
        department = get_object_or_404(Department, pk=pk)
        previous_line_manager = department.line_manager

        department.line_manager = None
        department.save(update_fields=["line_manager", "updated_at"])

        if previous_line_manager is not None:
            mgmt = get_or_create_management_department()
            DepartmentMembership.objects.filter(user=previous_line_manager, department=mgmt).delete()

            lm_role = Role.objects.filter(name=RoleName.LINE_MANAGER).first()
            if lm_role:
                UserRole.objects.filter(user=previous_line_manager, role=lm_role).delete()

        return Response(status=status.HTTP_204_NO_CONTENT)


class DepartmentMembersView(APIView):
    """GET /api/v1/departments/:id/members/ — list users in a department."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        department = get_object_or_404(Department, pk=pk)
        user = request.user

        can_access = (
            user.is_staff
            or user.has_role(RoleName.HR)
            or user.has_role(RoleName.EXECUTIVE_DIRECTOR)
            or user.has_role(RoleName.MANAGING_DIRECTOR)
            or (user.department_id == department.pk)
        )
        if not can_access:
            raise PermissionDenied("You can only view members of your own department.")

        members = User.objects.filter(department=department).select_related("department")
        return Response(UserSerializer(members, many=True).data)


class DepartmentDetailView(APIView):
    """
    GET /api/v1/departments/:id/detail/ — department + members + units + supervisors + line manager.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        department = get_object_or_404(Department, pk=pk)
        user = request.user

        can_access = (
            user.is_staff
            or user.has_role(RoleName.HR)
            or user.has_role(RoleName.EXECUTIVE_DIRECTOR)
            or user.has_role(RoleName.MANAGING_DIRECTOR)
            or (user.department_id == department.pk)
        )
        if not can_access:
            raise PermissionDenied("You do not have permission to view this department.")

        dept_data = DepartmentSerializer(department).data
        members = User.objects.filter(department=department).select_related("department")
        units = Unit.objects.filter(department=department).select_related("supervisor", "department")

        from .serializers import _UserMinimalSerializer  # local import to avoid circular

        members_data = _UserMinimalSerializer(members, many=True).data
        units_data = UnitSerializer(units, many=True).data

        payload = {
            "department": dept_data,
            "members": members_data,
            "units": units_data,
        }
        return Response(payload)


class DepartmentBulkAddMembersView(APIView):
    """
    POST /api/v1/departments/:id/bulk-add-members/

    Bulk-assign users to a department with partial success semantics.

    Semantics:
    - Sets User.department = department
    - Creates DepartmentMembership(user, department)

    Optional flags:
    - dry_run: validate only, no writes
    - clear_conflicts: if true, clears conflicting unit/team assignments as needed
    """

    permission_classes = [permissions.IsAuthenticated, IsHR | permissions.IsAdminUser]

    def post(self, request, pk):
        department = get_object_or_404(Department, pk=pk)

        serializer = BulkUserIdsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user_ids = serializer.validated_data["user_ids"]
        dry_run = serializer.validated_data["dry_run"]
        clear_conflicts = serializer.validated_data["clear_conflicts"]

        users = (
            User.objects.filter(pk__in=user_ids)
            .select_related(
                "department",
                "unit",
                "unit__department",
                "team",
                "team__unit",
                "team__unit__department",
            )
            .all()
        )
        users_by_id = {u.pk: u for u in users}

        succeeded_user_ids = []
        failed = []

        for user_id in user_ids:
            user = users_by_id.get(user_id)
            if user is None:
                failed.append(
                    {
                        "user_id": str(user_id),
                        "code": "not_found",
                        "error": "User not found.",
                    }
                )
                continue

            # Validate department consistency with existing unit/team.
            # If clear_conflicts is true, we will clear conflicting unit/team instead.
            if user.team_id is not None:
                team_dept_id = user.team.unit.department_id
                if team_dept_id != department.pk:
                    if not clear_conflicts:
                        failed.append(
                            {
                                "user_id": str(user.pk),
                                "code": "department_conflict",
                                "error": "User belongs to a team in a different department.",
                            }
                        )
                        continue

            if user.unit_id is not None:
                unit_dept_id = user.unit.department_id
                if unit_dept_id != department.pk:
                    if not clear_conflicts:
                        failed.append(
                            {
                                "user_id": str(user.pk),
                                "code": "department_conflict",
                                "error": "User belongs to a unit in a different department.",
                            }
                        )
                        continue

            if dry_run:
                succeeded_user_ids.append(str(user.pk))
                continue

            with transaction.atomic():
                update_fields = ["department", "updated_at"]

                if clear_conflicts:
                    # If user is moving departments, they cannot keep unit/team that are outside the department.
                    if user.team_id is not None and user.team.unit.department_id != department.pk:
                        user.team = None
                        update_fields.extend(["team"])
                    if user.unit_id is not None and user.unit.department_id != department.pk:
                        user.unit = None
                        update_fields.extend(["unit"])

                user.department = department
                user.save(update_fields=list(dict.fromkeys(update_fields)))

                DepartmentMembership.objects.get_or_create(
                    user=user,
                    department=department,
                )

            succeeded_user_ids.append(str(user.pk))

        return Response(
            {
                "target": {"department_id": str(department.pk)},
                "succeeded_user_ids": succeeded_user_ids,
                "failed": failed,
            },
            status=status.HTTP_200_OK,
        )


class DepartmentBulkRemoveMembersView(APIView):
    """
    POST /api/v1/departments/:id/bulk-remove-members/

    Bulk-remove users from a department with partial success semantics.

    Semantics:
    - If user's primary department matches, clears: department, unit, team
    - Removes DepartmentMembership(user, department) if present

    Optional flags:
    - dry_run: validate only, no writes
    """

    permission_classes = [permissions.IsAuthenticated, IsHR | permissions.IsAdminUser]

    def post(self, request, pk):
        department = get_object_or_404(Department, pk=pk)

        serializer = BulkUserIdsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user_ids = serializer.validated_data["user_ids"]
        dry_run = serializer.validated_data["dry_run"]

        users = (
            User.objects.filter(pk__in=user_ids)
            .select_related("department", "unit", "team")
            .all()
        )
        users_by_id = {u.pk: u for u in users}

        succeeded_user_ids = []
        failed = []

        for user_id in user_ids:
            user = users_by_id.get(user_id)
            if user is None:
                failed.append({"user_id": str(user_id), "code": "not_found", "error": "User not found."})
                continue

            if user.department_id != department.pk:
                failed.append(
                    {
                        "user_id": str(user.pk),
                        "code": "not_in_department",
                        "error": "User is not a member of this department.",
                    }
                )
                continue

            if dry_run:
                succeeded_user_ids.append(str(user.pk))
                continue

            with transaction.atomic():
                user.department = None
                user.unit = None
                user.team = None
                user.save(update_fields=["department", "unit", "team", "updated_at"])

                DepartmentMembership.objects.filter(user=user, department=department).delete()

            succeeded_user_ids.append(str(user.pk))

        return Response(
            {
                "target": {"department_id": str(department.pk)},
                "succeeded_user_ids": succeeded_user_ids,
                "failed": failed,
            },
            status=status.HTTP_200_OK,
        )


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


class UnitViewSet(viewsets.ModelViewSet):
    """
    Unit management within departments.

    - List: department members and privileged roles can list units of a department via filter.
    - Create: only the line manager of the department can create units for that department.
    - Retrieve: department members or privileged roles.
    - Update/Delete: only line manager of the unit's department.
    """

    queryset = Unit.objects.select_related("department", "supervisor").all()
    serializer_class = UnitSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        department_id = self.request.query_params.get("department")
        if department_id:
            qs = qs.filter(department_id=department_id)
        return qs

    def _is_privileged(self, user):
        return (
            user.is_staff
            or user.has_role(RoleName.HR)
            or user.has_role(RoleName.EXECUTIVE_DIRECTOR)
            or user.has_role(RoleName.MANAGING_DIRECTOR)
        )

    def _can_access_department(self, user, department):
        return self._is_privileged(user) or user.department_id == department.pk

    def perform_create(self, serializer):
        user = self.request.user
        department = serializer.validated_data.get("department")
        if department is None:
            raise ValidationError({"department_id": "A department is required to create a unit."})
        # Allow privileged roles (HR/ED/MD/staff) to create units for any department.
        if department.line_manager_id != user.pk and not self._is_privileged(user):
            raise PermissionDenied("Only the line manager of this department can create units.")
        serializer.save()

    def perform_update(self, serializer):
        # If a supervisor is assigned, ensure the user holds the SUPERVISOR role.
        instance = self.get_object()
        previous_supervisor_id = instance.supervisor_id
        updated_unit = serializer.save()

        new_supervisor_id = updated_unit.supervisor_id
        if new_supervisor_id and new_supervisor_id != previous_supervisor_id:
            supervisor_role = Role.objects.filter(name=RoleName.SUPERVISOR).first()
            if supervisor_role:
                UserRole.objects.get_or_create(user_id=new_supervisor_id, role=supervisor_role)

    def get_object(self):
        obj = super().get_object()
        user = self.request.user
        department = obj.department

        if self.request.method in ("GET",):
            if not self._can_access_department(user, department):
                raise PermissionDenied("You do not have permission to view this unit.")
        else:
            if department.line_manager_id != user.pk and not self._is_privileged(user):
                raise PermissionDenied("Only the line manager of this department can modify or delete units.")

        return obj

    # ---------------------------
    # Supervisor assignment
    # ---------------------------

    @action(detail=True, methods=["post", "delete"], url_path="supervisor")
    def supervisor(self, request, pk=None):
        """
        POST /api/v1/units/:id/supervisor/
        Body: { "user_id": "<uuid>" }

        DELETE /api/v1/units/:id/supervisor/
        """
        unit = self.get_object()  # enforces manage permissions

        if request.method == "DELETE":
            previous = unit.supervisor
            unit.supervisor = None
            unit.save(update_fields=["supervisor", "updated_at"])

            if previous is not None:
                supervisor_role = Role.objects.filter(name=RoleName.SUPERVISOR).first()
                if supervisor_role:
                    UserRole.objects.filter(user=previous, role=supervisor_role).delete()

            return Response(UnitSerializer(unit).data)

        user_id = request.data.get("user_id")
        if not user_id:
            raise ValidationError({"user_id": "user_id is required."})

        supervisor = get_object_or_404(User, pk=user_id)
        if supervisor.department_id != unit.department_id:
            raise ValidationError({"user_id": "Supervisor must belong to the same department as the unit."})

        supervised_unit = getattr(supervisor, "supervised_unit", None)
        if supervised_unit is not None and supervised_unit.pk != unit.pk:
            raise ValidationError({"user_id": "This user is already the supervisor of another unit."})

        unit.supervisor = supervisor
        unit.save(update_fields=["supervisor", "updated_at"])

        supervisor_role = Role.objects.filter(name=RoleName.SUPERVISOR).first()
        supervisor_role, _ = Role.objects.get_or_create(
            name=RoleName.SUPERVISOR,
            defaults={"description": "Supervisor"},
        )
        UserRole.objects.get_or_create(user=supervisor, role=supervisor_role)

        return Response(UnitSerializer(unit).data)

    @action(detail=True, methods=["post"], url_path="bulk-add-members")
    def bulk_add_members(self, request, pk=None):
        """
        POST /api/v1/units/:id/bulk-add-members/

        Bulk-assign users to a unit with partial success semantics.

        Semantics:
        - Sets User.unit = unit
        - Ensures User.department matches unit.department:
          - if department is None, it is set
          - if department differs, fails unless clear_conflicts=true (then moves + clears team as needed)
        - If user has a team not in this unit, fails unless clear_conflicts=true (then clears team)
        """

        unit = self.get_object()  # enforces manage permissions for POST via get_object()

        serializer = BulkUserIdsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user_ids = serializer.validated_data["user_ids"]
        dry_run = serializer.validated_data["dry_run"]
        clear_conflicts = serializer.validated_data["clear_conflicts"]

        users = (
            User.objects.filter(pk__in=user_ids)
            .select_related(
                "department",
                "unit",
                "team",
                "team__unit",
                "team__unit__department",
            )
            .all()
        )
        users_by_id = {u.pk: u for u in users}

        succeeded_user_ids = []
        failed = []

        for user_id in user_ids:
            user = users_by_id.get(user_id)
            if user is None:
                failed.append({"user_id": str(user_id), "code": "not_found", "error": "User not found."})
                continue

            # Validate/resolve department consistency.
            if user.department_id is not None and user.department_id != unit.department_id and not clear_conflicts:
                failed.append(
                    {
                        "user_id": str(user.pk),
                        "code": "department_conflict",
                        "error": "User belongs to a different department than the unit.",
                    }
                )
                continue

            # Team must be inside the unit, or be cleared if allowed.
            if user.team_id is not None and user.team.unit_id != unit.pk and not clear_conflicts:
                failed.append(
                    {
                        "user_id": str(user.pk),
                        "code": "team_conflict",
                        "error": "User belongs to a team in a different unit.",
                    }
                )
                continue

            if dry_run:
                succeeded_user_ids.append(str(user.pk))
                continue

            with transaction.atomic():
                update_fields = ["unit", "updated_at"]

                if clear_conflicts and user.team_id is not None and user.team.unit_id != unit.pk:
                    user.team = None
                    update_fields.append("team")

                # Move/set department as needed.
                if user.department_id is None:
                    user.department = unit.department
                    update_fields.append("department")
                elif clear_conflicts and user.department_id != unit.department_id:
                    # Moving departments implies clearing team (and any prior unit will be replaced below).
                    if user.team_id is not None:
                        user.team = None
                        update_fields.append("team")
                    user.department = unit.department
                    update_fields.append("department")

                user.unit = unit
                user.save(update_fields=list(dict.fromkeys(update_fields)))

            succeeded_user_ids.append(str(user.pk))

        return Response(
            {
                "target": {"unit_id": str(unit.pk)},
                "succeeded_user_ids": succeeded_user_ids,
                "failed": failed,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="bulk-remove-members")
    def bulk_remove_members(self, request, pk=None):
        """
        POST /api/v1/units/:id/bulk-remove-members/

        Bulk-remove users from a unit with partial success semantics.

        Semantics:
        - If user's unit matches, clears: unit, team
        - Department is not changed.
        """

        unit = self.get_object()  # enforces manage permissions for POST via get_object()

        serializer = BulkUserIdsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user_ids = serializer.validated_data["user_ids"]
        dry_run = serializer.validated_data["dry_run"]

        users = User.objects.filter(pk__in=user_ids).select_related("unit", "team").all()
        users_by_id = {u.pk: u for u in users}

        succeeded_user_ids = []
        failed = []

        for user_id in user_ids:
            user = users_by_id.get(user_id)
            if user is None:
                failed.append({"user_id": str(user_id), "code": "not_found", "error": "User not found."})
                continue

            if user.unit_id != unit.pk:
                failed.append(
                    {
                        "user_id": str(user.pk),
                        "code": "not_in_unit",
                        "error": "User is not a member of this unit.",
                    }
                )
                continue

            if dry_run:
                succeeded_user_ids.append(str(user.pk))
                continue

            with transaction.atomic():
                user.unit = None
                user.team = None
                user.save(update_fields=["unit", "team", "updated_at"])

            succeeded_user_ids.append(str(user.pk))

        return Response(
            {
                "target": {"unit_id": str(unit.pk)},
                "succeeded_user_ids": succeeded_user_ids,
                "failed": failed,
            },
            status=status.HTTP_200_OK,
        )


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------


class TeamViewSet(viewsets.ModelViewSet):
    """
    Team management within units.

    - List: unit/department members and privileged roles can list teams of a unit via filter.
    - Create: HR or line manager of the unit's department can create teams.
    - Retrieve: unit/department members or privileged roles.
    - Update/Delete: HR or line manager of the unit's department.
    """

    queryset = Team.objects.select_related("unit", "unit__department", "team_lead").all()
    serializer_class = TeamSerializer
    permission_classes = [permissions.IsAuthenticated]

    def _is_privileged(self, user):
        return (
            user.is_staff
            or user.has_role(RoleName.HR)
            or user.has_role(RoleName.EXECUTIVE_DIRECTOR)
            or user.has_role(RoleName.MANAGING_DIRECTOR)
        )

    def _can_access_unit(self, user, unit: Unit) -> bool:
        return self._is_privileged(user) or user.unit_id == unit.pk or user.department_id == unit.department_id

    def _can_manage_unit(self, user, unit: Unit) -> bool:
        if self._is_privileged(user):
            return True
        # Department line manager can manage teams for that department's units.
        return unit.department.line_manager_id == user.pk

    def get_queryset(self):
        qs = super().get_queryset()
        unit_id = self.request.query_params.get("unit")
        if unit_id:
            qs = qs.filter(unit_id=unit_id)
        return qs

    def perform_create(self, serializer):
        user = self.request.user
        unit = serializer.validated_data.get("unit")
        if unit is None:
            raise ValidationError({"unit_id": "A unit is required to create a team."})
        if not self._can_manage_unit(user, unit):
            raise PermissionDenied("Only HR or the line manager of this unit's department can create teams.")
        serializer.save()

    def perform_update(self, serializer):
        # If a team_lead is assigned, ensure the user holds the TEAM_LEAD role.
        instance = self.get_object()
        previous_team_lead_id = instance.team_lead_id
        updated_team = serializer.save()

        new_team_lead_id = updated_team.team_lead_id
        if new_team_lead_id and new_team_lead_id != previous_team_lead_id:
            team_lead_role = Role.objects.filter(name=RoleName.TEAM_LEAD).first()
            if team_lead_role:
                UserRole.objects.get_or_create(user_id=new_team_lead_id, role=team_lead_role)

    def get_object(self):
        obj = super().get_object()
        user = self.request.user
        unit = obj.unit

        if self.request.method in ("GET",):
            if not self._can_access_unit(user, unit):
                raise PermissionDenied("You do not have permission to view this team.")
        else:
            if not self._can_manage_unit(user, unit):
                raise PermissionDenied("Only HR or the line manager of this unit's department can modify or delete teams.")

        return obj

    # ---------------------------
    # Membership management
    # ---------------------------

    def _get_user_to_modify(self, request):
        user_id = request.data.get("user_id")
        if not user_id:
            raise ValidationError({"user_id": "user_id is required."})
        return get_object_or_404(User, pk=user_id)

    @action(detail=True, methods=["post"], url_path="add-member")
    def add_member(self, request, pk=None):
        team = self.get_object()
        if not self._can_manage_unit(request.user, team.unit):
            raise PermissionDenied("You do not have permission to manage this team.")

        member = self._get_user_to_modify(request)
        if member.unit_id != team.unit_id:
            raise ValidationError({"user_id": "User must belong to the same unit as the team."})

        member.team = team
        member.save(update_fields=["team", "updated_at"])
        return Response(TeamSerializer(team).data)

    @action(detail=True, methods=["post"], url_path="bulk-add-members")
    def bulk_add_members(self, request, pk=None):
        """
        POST /api/v1/teams/:id/bulk-add-members/

        Bulk-assign users to a team with partial success semantics.

        Constraints:
        - User must already belong to the same unit as the team (matches add_member behavior).
        """

        team = self.get_object()  # enforces manage permissions for POST via get_object()
        if not self._can_manage_unit(request.user, team.unit):
            raise PermissionDenied("You do not have permission to manage this team.")

        serializer = BulkUserIdsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user_ids = serializer.validated_data["user_ids"]
        dry_run = serializer.validated_data["dry_run"]

        users = User.objects.filter(pk__in=user_ids).select_related("unit", "team").all()
        users_by_id = {u.pk: u for u in users}

        succeeded_user_ids = []
        failed = []

        for user_id in user_ids:
            user = users_by_id.get(user_id)
            if user is None:
                failed.append({"user_id": str(user_id), "code": "not_found", "error": "User not found."})
                continue

            if user.unit_id != team.unit_id:
                failed.append(
                    {
                        "user_id": str(user.pk),
                        "code": "unit_mismatch",
                        "error": "User must belong to the same unit as the team.",
                    }
                )
                continue

            if dry_run:
                succeeded_user_ids.append(str(user.pk))
                continue

            with transaction.atomic():
                user.team = team
                user.save(update_fields=["team", "updated_at"])

            succeeded_user_ids.append(str(user.pk))

        return Response(
            {
                "target": {"team_id": str(team.pk)},
                "succeeded_user_ids": succeeded_user_ids,
                "failed": failed,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="bulk-remove-members")
    def bulk_remove_members(self, request, pk=None):
        """
        POST /api/v1/teams/:id/bulk-remove-members/

        Bulk-remove users from a team with partial success semantics.

        Semantics:
        - If user's team matches, clears: team
        """

        team = self.get_object()  # enforces manage permissions for POST via get_object()
        if not self._can_manage_unit(request.user, team.unit):
            raise PermissionDenied("You do not have permission to manage this team.")

        serializer = BulkUserIdsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user_ids = serializer.validated_data["user_ids"]
        dry_run = serializer.validated_data["dry_run"]

        users = User.objects.filter(pk__in=user_ids).select_related("team").all()
        users_by_id = {u.pk: u for u in users}

        succeeded_user_ids = []
        failed = []

        for user_id in user_ids:
            user = users_by_id.get(user_id)
            if user is None:
                failed.append({"user_id": str(user_id), "code": "not_found", "error": "User not found."})
                continue

            if user.team_id != team.pk:
                failed.append(
                    {
                        "user_id": str(user.pk),
                        "code": "not_in_team",
                        "error": "User is not a member of this team.",
                    }
                )
                continue

            if dry_run:
                succeeded_user_ids.append(str(user.pk))
                continue

            with transaction.atomic():
                user.team = None
                user.save(update_fields=["team", "updated_at"])

            succeeded_user_ids.append(str(user.pk))

        return Response(
            {
                "target": {"team_id": str(team.pk)},
                "succeeded_user_ids": succeeded_user_ids,
                "failed": failed,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="remove-member")
    def remove_member(self, request, pk=None):
        team = self.get_object()
        if not self._can_manage_unit(request.user, team.unit):
            raise PermissionDenied("You do not have permission to manage this team.")

        member = self._get_user_to_modify(request)
        if member.team_id != team.pk:
            raise ValidationError({"user_id": "User is not a member of this team."})

        member.team = None
        member.save(update_fields=["team", "updated_at"])
        return Response(TeamSerializer(team).data)

    @action(detail=True, methods=["post"], url_path="set-lead")
    def set_lead(self, request, pk=None):
        team = self.get_object()
        if not self._can_manage_unit(request.user, team.unit):
            raise PermissionDenied("You do not have permission to manage this team.")

        lead = self._get_user_to_modify(request)
        if lead.unit_id != team.unit_id:
            raise ValidationError({"user_id": "Team lead must belong to the same unit as the team."})

        team.team_lead = lead
        team.save(update_fields=["team_lead", "updated_at"])

        team_lead_role = Role.objects.filter(name=RoleName.TEAM_LEAD).first()
        team_lead_role, _ = Role.objects.get_or_create(
            name=RoleName.TEAM_LEAD,
            defaults={"description": "Team Lead"},
        )
        UserRole.objects.get_or_create(user_id=lead.pk, role=team_lead_role)

        return Response(TeamSerializer(team).data)

    @action(detail=True, methods=["post"], url_path="clear-lead")
    def clear_lead(self, request, pk=None):
        team = self.get_object()
        if not self._can_manage_unit(request.user, team.unit):
            raise PermissionDenied("You do not have permission to manage this team.")

        team.team_lead = None
        team.save(update_fields=["team_lead", "updated_at"])
        return Response(TeamSerializer(team).data)

    # ---------------------------
    # Team lead assignment (alias endpoint)
    # ---------------------------

    @action(detail=True, methods=["post", "delete"], url_path="team-lead")
    def team_lead(self, request, pk=None):
        """
        POST /api/v1/teams/:id/team-lead/
        Body: { "user_id": "<uuid>" }

        DELETE /api/v1/teams/:id/team-lead/
        """
        team = self.get_object()
        if not self._can_manage_unit(request.user, team.unit):
            raise PermissionDenied("You do not have permission to manage this team.")

        if request.method == "DELETE":
            previous = team.team_lead
            team.team_lead = None
            team.save(update_fields=["team_lead", "updated_at"])

            if previous is not None:
                team_lead_role = Role.objects.filter(name=RoleName.TEAM_LEAD).first()
                if team_lead_role:
                    UserRole.objects.filter(user=previous, role=team_lead_role).delete()

            return Response(TeamSerializer(team).data)

        user_id = request.data.get("user_id")
        if not user_id:
            raise ValidationError({"user_id": "user_id is required."})

        lead = get_object_or_404(User, pk=user_id)
        if lead.unit_id != team.unit_id:
            raise ValidationError({"user_id": "Team lead must belong to the same unit as the team."})

        led_team = getattr(lead, "led_team", None)
        if led_team is not None and led_team.pk != team.pk:
            raise ValidationError({"user_id": "This user is already the team lead of another team."})

        team.team_lead = lead
        team.save(update_fields=["team_lead", "updated_at"])

        team_lead_role = Role.objects.filter(name=RoleName.TEAM_LEAD).first()
        team_lead_role, _ = Role.objects.get_or_create(
            name=RoleName.TEAM_LEAD,
            defaults={"description": "Team Lead"},
        )
        UserRole.objects.get_or_create(user_id=lead.pk, role=team_lead_role)

        return Response(TeamSerializer(team).data)

# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

class RoleViewSet(viewsets.ModelViewSet):
    """
    GET    /api/v1/roles/       — list   (HR or admin)
    POST   /api/v1/roles/      — create (HR or admin)
    GET    /api/v1/roles/:id/  — retrieve (HR or admin)
    PUT    /api/v1/roles/:id/  — update (HR or admin)
    PATCH  /api/v1/roles/:id/  — partial (HR or admin)
    DELETE /api/v1/roles/:id/  — delete (HR or admin)
    """

    queryset = Role.objects.all()
    serializer_class = RoleSerializer
    permission_classes = [permissions.IsAuthenticated, IsHR | permissions.IsAdminUser]


# ---------------------------------------------------------------------------
# User → Role assignment
# ---------------------------------------------------------------------------

class AssignRoleView(APIView):
    """POST /api/v1/users/:id/roles/ — assign a role to a user (HR or admin)."""

    permission_classes = [permissions.IsAuthenticated, IsHR | permissions.IsAdminUser]

    def post(self, request, user_id):
        user = get_object_or_404(User, pk=user_id)
        serializer = UserRoleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        role_obj = serializer.validated_data["role_id"]

        user_role = serializer.save(user=user)
        role = user_role.role

        if role.name == RoleName.HR:
            # Keep existing behavior (HR users live in the HR department), but avoid
            # breaking line-manager assignments by force-moving departments.
            is_line_manager_somewhere = Department.objects.filter(line_manager=user).exists()
            if not is_line_manager_somewhere:
                hr_dept = get_or_create_hr_department()
                user.department = hr_dept
                user.save(update_fields=["department", "updated_at"])
        elif role.name in (RoleName.EXECUTIVE_DIRECTOR, RoleName.MANAGING_DIRECTOR):
            is_line_manager_somewhere = Department.objects.filter(line_manager=user).exists()
            if not is_line_manager_somewhere:
                user.department = None
                user.save(update_fields=["department", "updated_at"])
            # ED is preferred as the Management Dept line manager.
            if role.name == RoleName.EXECUTIVE_DIRECTOR:
                mgmt = get_or_create_management_department()
                mgmt.line_manager = user
                mgmt.save(update_fields=["line_manager", "updated_at"])
        elif role.name == RoleName.LINE_MANAGER:
            mgmt = get_or_create_management_department()
            DepartmentMembership.objects.get_or_create(user=user, department=mgmt)

            # A Line Manager must belong to a department so we know which
            # department's line_manager to update (unless the user is HR/ED/MD).
            if user.department is None and not (
                user.has_role(RoleName.HR)
                or user.has_role(RoleName.EXECUTIVE_DIRECTOR)
                or user.has_role(RoleName.MANAGING_DIRECTOR)
            ):
                raise ValidationError(
                    {
                        "department": (
                            "A Line Manager must belong to a department before "
                            "assigning the LINE_MANAGER role."
                        )
                    }
                )
            if user.department is not None:
                department = user.department
                department.line_manager = user
                department.save(update_fields=["line_manager", "updated_at"])

        return Response(serializer.data, status=status.HTTP_201_CREATED)


class RemoveRoleView(APIView):
    """DELETE /api/v1/users/:id/roles/:role_id/ — remove a role from a user (HR or admin)."""

    permission_classes = [permissions.IsAuthenticated, IsHR | permissions.IsAdminUser]

    def delete(self, request, user_id, role_id):
        user = get_object_or_404(User, pk=user_id)
        user_role = get_object_or_404(UserRole, user=user, role_id=role_id)
        user_role.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
