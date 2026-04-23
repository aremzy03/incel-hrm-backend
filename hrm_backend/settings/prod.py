from decouple import config

from .base import *  # noqa: F401,F403

DEBUG = config("DEBUG", default=False, cast=bool)

SECURE_SSL_REDIRECT = config("SECURE_SSL_REDIRECT", default=True, cast=bool)
SESSION_COOKIE_SECURE = config("SESSION_COOKIE_SECURE", default=True, cast=bool)
CSRF_COOKIE_SECURE = config("CSRF_COOKIE_SECURE", default=True, cast=bool)
SECURE_HSTS_SECONDS = config("SECURE_HSTS_SECONDS", default=31536000, cast=int)
SECURE_HSTS_INCLUDE_SUBDOMAINS = config(
    "SECURE_HSTS_INCLUDE_SUBDOMAINS", default=True, cast=bool
)
SECURE_HSTS_PRELOAD = config("SECURE_HSTS_PRELOAD", default=True, cast=bool)

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
