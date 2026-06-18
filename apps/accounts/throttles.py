from django.core.exceptions import ImproperlyConfigured
from rest_framework.settings import api_settings
from rest_framework.throttling import SimpleRateThrottle


class ScopedRateThrottle(SimpleRateThrottle):
    """Read throttle rates from settings at request time (supports override_settings in tests)."""

    def get_rate(self):
        try:
            return api_settings.DEFAULT_THROTTLE_RATES[self.scope]
        except KeyError:
            msg = "No default throttle rate set for '%s' scope" % self.scope
            raise ImproperlyConfigured(msg)


class AuthEndpointThrottle(ScopedRateThrottle):
    """IP-based throttle for unauthenticated auth endpoints (register, refresh)."""

    def get_cache_key(self, request, view):
        ident = self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


class LoginThrottle(ScopedRateThrottle):
    """Per-email throttle for login; falls back to IP when email is absent."""

    scope = "login"

    def get_cache_key(self, request, view):
        email = ""
        if hasattr(request, "data"):
            raw = request.data.get("email") or request.data.get("username")
            if raw:
                email = str(raw).strip().lower()

        ident = email if email else self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


class RegisterThrottle(AuthEndpointThrottle):
    scope = "register"


class RefreshThrottle(AuthEndpointThrottle):
    scope = "refresh"


class PasswordChangeThrottle(ScopedRateThrottle):
    """Per-user throttle for authenticated password change (mitigate current-password guessing)."""

    scope = "password_change"

    def get_cache_key(self, request, view):
        if request.user and request.user.is_authenticated:
            ident = str(request.user.pk)
        else:
            ident = self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}
