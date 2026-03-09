from django.urls import path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .views import (
    AssignRoleView,
    DepartmentViewSet,
    MeView,
    RegisterView,
    RemoveRoleView,
    RoleListCreateView,
    UserDepartmentUpdateView,
)

auth_urlpatterns = [
    path("login/", TokenObtainPairView.as_view(), name="token-obtain-pair"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token-refresh"),
    path("register/", RegisterView.as_view(), name="register"),
    path("me/", MeView.as_view(), name="me"),
]

role_urlpatterns = [
    path("roles/", RoleListCreateView.as_view(), name="role-list-create"),
    path("users/<uuid:user_id>/roles/", AssignRoleView.as_view(), name="user-role-assign"),
    path(
        "users/<uuid:user_id>/roles/<uuid:role_id>/",
        RemoveRoleView.as_view(),
        name="user-role-remove",
    ),
]

department_router = DefaultRouter()
department_router.register(r"departments", DepartmentViewSet, basename="department")

department_urlpatterns = department_router.urls + [
    path(
        "users/<uuid:user_id>/department/",
        UserDepartmentUpdateView.as_view(),
        name="user-department-update",
    ),
]
