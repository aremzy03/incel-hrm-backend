from decouple import config

from .base import *  # noqa: F401,F403

DEBUG = True

# PostgreSQL — mirrors base.py; override individual values in .env as needed.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": config("DB_NAME", default="hrm_db"),
        "USER": config("DB_USER", default="postgres"),
        "PASSWORD": config("DB_PASSWORD", default=""),
        "HOST": config("DB_HOST", default="localhost"),
        "PORT": config("DB_PORT", default="5432"),
    }
}

# Use SQLite for test runs so tests do not depend on Postgres permissions.
if "test" in sys.argv:  # noqa: F405  (sys imported in base.py)
    DATABASES["default"] = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "test_db.sqlite3",  # noqa: F405 (BASE_DIR in base.py)
    }

CORS_ALLOW_ALL_ORIGINS = True

REGISTRATION_OPEN = True
PUBLIC_DEPARTMENT_ACCESS = True

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
