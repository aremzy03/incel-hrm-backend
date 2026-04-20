import os

from celery import Celery

# Allow the environment to choose the settings module.
# Default to production-safe settings.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hrm_backend.settings.prod")

app = Celery("hrm_backend")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
