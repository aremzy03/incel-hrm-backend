from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404
from rest_framework import generics, permissions, status, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .models import Department, get_or_create_hr_department, Role, RoleName, UserRole
from .permissions import IsExecutiveDirector, IsHR
from .serializers import (
    DepartmentLineManagerSerializer,
    DepartmentSerializer,
    RegisterSerializer,
    RoleSerializer,
    UserCreateSerializer,
    UserDepartmentUpdateSerializer,
    UserRoleSerializer,
    UserSerializer,
    UserUpdateSerializer,
)

User = get_user_model()

__all__ = [
    "TokenObtainPairView",
    "TokenRefreshView",
    "RegisterView",
    "MeView",
    "UserViewSet",
    "RoleViewSet",
    "AssignRoleView",
    "RemoveRoleView",
    "DepartmentViewSet",
    "UserDepartmentUpdateView",
    "DepartmentLineManagerView",
    "DepartmentMembersView",
]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class RegisterView(generics.CreateAPIView):
    """POST /api/v1/auth/register/"""

    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)


class MeView(APIView):
    """GET /api/v1/auth/me/"""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
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
    GET    /api/v1/departments/       — anyone (no auth required)
    POST   /api/v1/departments/       — HR or admin
    GET    /api/v1/departments/:id/   — anyone (no auth required)
    PUT    /api/v1/departments/:id/   — HR or admin
    PATCH  /api/v1/departments/:id/   — HR or admin
    DELETE /api/v1/departments/:id/   — HR or admin
    """

    queryset = Department.objects.all()
    serializer_class = DepartmentSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [permissions.AllowAny()]
        return [permissions.IsAuthenticated(), (IsHR | permissions.IsAdminUser)()]


class UserDepartmentUpdateView(APIView):
    """PATCH /api/v1/users/:id/department/ — HR or admin only."""

    permission_classes = [permissions.IsAuthenticated, IsHR | permissions.IsAdminUser]

    def patch(self, request, user_id):
        user = get_object_or_404(User, pk=user_id)
        serializer = UserDepartmentUpdateSerializer(data=request.data, context={"user": user})
        serializer.is_valid(raise_exception=True)
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

        lm_role = Role.objects.filter(name=RoleName.LINE_MANAGER).first()
        if lm_role:
            UserRole.objects.get_or_create(user=user, role=lm_role)

        department.line_manager = user
        department.save(update_fields=["line_manager", "updated_at"])
        return Response(DepartmentSerializer(department).data)

    def delete(self, request, pk):
        department = get_object_or_404(Department, pk=pk)
        department.line_manager = None
        department.save(update_fields=["line_manager", "updated_at"])
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
        UserRole.objects.filter(user=user).delete()
        user_role = serializer.save(user=user)
        role = user_role.role

        if role.name == RoleName.HR:
            hr_dept = get_or_create_hr_department()
            user.department = hr_dept
            user.save(update_fields=["department", "updated_at"])
        elif role.name in (RoleName.EXECUTIVE_DIRECTOR, RoleName.MANAGING_DIRECTOR):
            user.department = None
            user.save(update_fields=["department", "updated_at"])

        return Response(serializer.data, status=status.HTTP_201_CREATED)


class RemoveRoleView(APIView):
    """DELETE /api/v1/users/:id/roles/:role_id/ — remove a role from a user (HR or admin)."""

    permission_classes = [permissions.IsAuthenticated, IsHR | permissions.IsAdminUser]

    def delete(self, request, user_id, role_id):
        user = get_object_or_404(User, pk=user_id)
        user_role = get_object_or_404(UserRole, user=user, role_id=role_id)
        user_role.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
