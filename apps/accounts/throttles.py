from rest_framework.throttling import SimpleRateThrottle


class AuthEndpointThrottle(SimpleRateThrottle):
    """IP-based throttle for unauthenticated auth endpoints (login, refresh, register)."""

    def get_cache_key(self, request, view):
        ident = self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


class LoginThrottle(AuthEndpointThrottle):
    scope = "login"


class RegisterThrottle(AuthEndpointThrottle):
    scope = "register"


class RefreshThrottle(AuthEndpointThrottle):
    scope = "refresh"


class PasswordChangeThrottle(SimpleRateThrottle):
    """Per-user throttle for authenticated password change (mitigate current-password guessing)."""

    scope = "password_change"

    def get_cache_key(self, request, view):
        if request.user and request.user.is_authenticated:
            ident = str(request.user.pk)
        else:
            ident = self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}
