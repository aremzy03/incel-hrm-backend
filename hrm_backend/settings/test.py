"""
Isolated settings for the test suite.

Mirrors the e-sign app's test_settings: no Redis, no Postgres, eager Celery,
and in-memory cache/email so GitHub Actions does not need extra services.
"""

import os

os.environ.setdefault("SECRET_KEY", "django-insecure-ci-test-key-not-for-production")

from .base import *  # noqa: E402,F401,F403

DEBUG = True
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "test_db.sqlite3",
    }
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "hrm-test-cache",
    }
}

NOTIFICATIONS_REDIS_URL = ""

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
FRONTEND_BASE_URL = "http://localhost:3000"

# Disable global throttles for the suite; keep rate map so scoped throttles
# (login / register / refresh / password_change) remain configurable when a
# test opts into them via override_settings.
REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    "DEFAULT_THROTTLE_CLASSES": [],
    "DEFAULT_THROTTLE_RATES": {
        **REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"],
        "anon": "10000/hour",
        "user": "10000/hour",
        "login": "10000/minute",
        "register": "10000/minute",
        "refresh": "10000/minute",
        "password_change": "10000/minute",
    },
}
