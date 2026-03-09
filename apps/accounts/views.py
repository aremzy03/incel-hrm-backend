from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404
from rest_framework import generics, permissions, status, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .models import Department, Role, UserRole
from .permissions import IsHR
from .serializers import (
    DepartmentSerializer,
    RegisterSerializer,
    RoleSerializer,
    UserDepartmentUpdateSerializer,
    UserRoleSerializer,
    UserSerializer,
)

User = get_user_model()

__all__ = [
    "TokenObtainPairView",
    "TokenRefreshView",
    "RegisterView",
    "MeView",
    "RoleListCreateView",
    "AssignRoleView",
    "RemoveRoleView",
    "DepartmentViewSet",
    "UserDepartmentUpdateView",
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
        return [permissions.IsAuthenticated(), IsHR() | permissions.IsAdminUser()]


class UserDepartmentUpdateView(APIView):
    """PATCH /api/v1/users/:id/department/ — HR or admin only."""

    permission_classes = [permissions.IsAuthenticated, IsHR | permissions.IsAdminUser]

    def patch(self, request, user_id):
        user = get_object_or_404(User, pk=user_id)
        serializer = UserDepartmentUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user.department = serializer.validated_data["department"]
        user.save(update_fields=["department", "updated_at"])
        return Response(UserSerializer(user).data)


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

class RoleListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/v1/roles/  — list all roles   (HR or admin)
    POST /api/v1/roles/  — create a role    (HR or admin)
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
        serializer.save(user=user)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class RemoveRoleView(APIView):
    """DELETE /api/v1/users/:id/roles/:role_id/ — remove a role from a user (HR or admin)."""

    permission_classes = [permissions.IsAuthenticated, IsHR | permissions.IsAdminUser]

    def delete(self, request, user_id, role_id):
        user = get_object_or_404(User, pk=user_id)
        user_role = get_object_or_404(UserRole, user=user, role_id=role_id)
        user_role.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
