import multiprocessing
import os

bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:8000")
worker_class = "gevent"
workers = int(os.environ.get("GUNICORN_WORKERS", max(2, multiprocessing.cpu_count())))
worker_connections = int(os.environ.get("GUNICORN_WORKER_CONNECTIONS", "1000"))

# SSE streams are long-lived; 0 disables Gunicorn worker timeout.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "0"))
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", "30"))
keepalive = int(os.environ.get("GUNICORN_KEEPALIVE", "75"))

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
