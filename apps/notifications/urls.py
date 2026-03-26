from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .sse import notifications_stream
from .views import NotificationViewSet

router = DefaultRouter()
router.register(r"notifications", NotificationViewSet, basename="notification")

urlpatterns = [
    path("notifications/stream/", notifications_stream, name="notifications-stream"),
] + router.urls

