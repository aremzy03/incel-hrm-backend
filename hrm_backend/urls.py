from django.contrib import admin
from django.http import HttpResponse
from django.urls import include, path

from apps.accounts.urls import auth_urlpatterns, department_urlpatterns, role_urlpatterns


def health(_request):
    return HttpResponse("ok", content_type="text/plain")


urlpatterns = [
    path("health/", health),
    path("admin/", admin.site.urls),
    path("api/v1/auth/", include(auth_urlpatterns)),
    path("api/v1/", include(role_urlpatterns)),
    path("api/v1/", include(department_urlpatterns)),
    path("api/v1/", include("apps.leave.urls")),
    path("api/v1/", include("apps.notifications.urls")),
]
