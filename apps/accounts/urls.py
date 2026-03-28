from django.urls import path
from rest_framework.routers import DefaultRouter

from .throttled_auth_views import ThrottledTokenObtainPairView, ThrottledTokenRefreshView
from .views import (
    AssignRoleView,
    DepartmentDetailView,
    DepartmentLineManagerView,
    DepartmentMembersView,
    DepartmentViewSet,
    MeView,
    RegisterView,
    RemoveRoleView,
    RoleViewSet,
    UnitViewSet,
    UserDepartmentUpdateView,
    UserViewSet,
    UserProfileView,
)

auth_urlpatterns = [
    path("login/", ThrottledTokenObtainPairView.as_view(), name="token-obtain-pair"),
    path("token/refresh/", ThrottledTokenRefreshView.as_view(), name="token-refresh"),
    path("register/", RegisterView.as_view(), name="register"),
    path("me/", MeView.as_view(), name="me"),
    path("profile/", UserProfileView.as_view(), name="profile"),
]

role_router = DefaultRouter()
role_router.register(r"users", UserViewSet, basename="user")
role_router.register(r"roles", RoleViewSet, basename="role")
role_router.register(r"units", UnitViewSet, basename="unit")

role_urlpatterns = role_router.urls + [
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
    path(
        "departments/<uuid:pk>/line-manager/",
        DepartmentLineManagerView.as_view(),
        name="department-line-manager",
    ),
    path(
        "departments/<uuid:pk>/members/",
        DepartmentMembersView.as_view(),
        name="department-members",
    ),
    path(
        "departments/<uuid:pk>/detail/",
        DepartmentDetailView.as_view(),
        name="department-detail",
    ),
]
