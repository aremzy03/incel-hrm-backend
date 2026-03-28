from decouple import config

from .base import *  # noqa: F401,F403

DEBUG = False

SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

_proxy_header = config(
    "SECURE_PROXY_SSL_HEADER", default="HTTP_X_FORWARDED_PROTO,https"
).strip()
if _proxy_header:
    if ", " in _proxy_header:
        _proto_key, _proto_val = _proxy_header.split(", ", 1)
    else:
        _proto_key, _proto_val = _proxy_header.split(",", 1)
    SECURE_PROXY_SSL_HEADER = (_proto_key.strip(), _proto_val.strip())
else:
    SECURE_PROXY_SSL_HEADER = None
