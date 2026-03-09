from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .models import Role, UserRole
from .permissions import IsHR
from .serializers import RegisterSerializer, RoleSerializer, UserRoleSerializer, UserSerializer

User = get_user_model()

__all__ = [
    "TokenObtainPairView",
    "TokenRefreshView",
    "RegisterView",
    "MeView",
    "RoleListCreateView",
    "AssignRoleView",
    "RemoveRoleView",
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
