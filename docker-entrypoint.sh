#!/bin/sh
set -eu

role="${1:-web}"

case "$role" in
  web)
    exec gunicorn hrm_backend.wsgi:application -c gunicorn.conf.py
    ;;
  worker)
    exec celery -A hrm_backend worker -l info
    ;;
  beat)
    # Writable schedule file (container filesystem may be read-only on /app)
    exec celery -A hrm_backend beat -l info --schedule /tmp/celerybeat-schedule
    ;;
  *)
    exec "$@"
    ;;
esac
