import json
import logging
import time

import redis
from django.conf import settings
from django.db import close_old_connections
from django.http import HttpResponse, StreamingHttpResponse
from rest_framework_simplejwt.authentication import JWTAuthentication

logger = logging.getLogger(__name__)

KEEPALIVE_SECONDS = 15


def _notifications_redis_client(redis_url: str) -> redis.Redis:
    # Pub/sub streams are long-lived; disable socket read timeouts so idle
    # subscriptions do not raise redis.exceptions.TimeoutError under gevent.
    return redis.from_url(
        redis_url,
        decode_responses=True,
        socket_timeout=None,
        socket_connect_timeout=5,
    )


def notifications_stream(request):
    """
    SSE endpoint for in-app notifications.

    NOTE: Implemented as a plain Django view (not DRF) to avoid DRF content
    negotiation returning 406 for `Accept: text/event-stream`.
    """
    authenticator = JWTAuthentication()
    user_auth_tuple = authenticator.authenticate(request)
    if not user_auth_tuple:
        return HttpResponse("Unauthorized", status=401)
    user, _token = user_auth_tuple

    # IMPORTANT: StreamingHttpResponse keeps the request "open" while the generator yields.
    # If JWT authentication touched the DB (User lookup), the DB connection can remain
    # checked out for the lifetime of the SSE stream unless we close it explicitly here.
    close_old_connections()

    redis_url = getattr(settings, "NOTIFICATIONS_REDIS_URL", None) or getattr(
        settings, "REDIS_URL", "redis://localhost:6379/0"
    )
    client = _notifications_redis_client(redis_url)
    channel_name = f"notifications:user:{user.id}"
    pubsub = client.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(channel_name)

    def event_stream():
        close_old_connections()
        yield "event: ready\ndata: {}\n\n"
        try:
            while True:
                try:
                    message = pubsub.get_message(timeout=KEEPALIVE_SECONDS)
                except redis.RedisError:
                    logger.exception("Redis pubsub read failed for channel %s", channel_name)
                    break

                if message is None:
                    yield "event: keepalive\ndata: {}\n\n"
                    continue

                if message.get("type") != "message":
                    continue

                data = message.get("data")
                if not data:
                    continue

                try:
                    payload = json.loads(data)
                except Exception:
                    payload = {"raw": data}

                yield f"event: notification\ndata: {json.dumps(payload)}\n\n"
        finally:
            try:
                pubsub.unsubscribe(channel_name)
                pubsub.close()
            except Exception:
                pass
            try:
                client.close()
            except Exception:
                pass

    response = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response
