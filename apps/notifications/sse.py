import json
import time

import redis
from django.conf import settings
from django.db import close_old_connections
from django.http import HttpResponse, StreamingHttpResponse
from rest_framework_simplejwt.authentication import JWTAuthentication


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
    client = redis.from_url(redis_url, decode_responses=True)
    channel_name = f"notifications:user:{user.id}"
    pubsub = client.pubsub()
    pubsub.subscribe(channel_name)

    def event_stream():
        # Defensive: ensure this streaming generator doesn't hold onto a DB connection.
        close_old_connections()
        yield "event: ready\ndata: {}\n\n"
        last_keepalive = time.time()
        try:
            for message in pubsub.listen():
                now = time.time()
                if now - last_keepalive >= 15:
                    yield "event: keepalive\ndata: {}\n\n"
                    last_keepalive = now

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

    response = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response

