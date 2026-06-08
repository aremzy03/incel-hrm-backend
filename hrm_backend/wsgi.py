import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hrm_backend.settings.dev")

from psycogreen.gevent import patch_psycopg

patch_psycopg()

from django.core.wsgi import get_wsgi_application

application = get_wsgi_application()
