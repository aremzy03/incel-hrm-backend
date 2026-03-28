from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .throttles import LoginThrottle, RefreshThrottle


class ThrottledTokenObtainPairView(TokenObtainPairView):
    throttle_classes = [LoginThrottle]


class ThrottledTokenRefreshView(TokenRefreshView):
    throttle_classes = [RefreshThrottle]
