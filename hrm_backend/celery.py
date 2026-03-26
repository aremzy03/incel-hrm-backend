import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hrm_backend.settings.dev")

app = Celery("hrm_backend")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
