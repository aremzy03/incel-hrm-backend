"""
Services for user authentication and accounts module, including magic link tokens.
"""

from datetime import timedelta
import hashlib
import logging
import secrets
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import update_last_login
from django.db import transaction
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from .models import MagicLinkToken

User = get_user_model()
logger = logging.getLogger(__name__)


class MagicLinkError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def hash_token(raw_token: str) -> str:
    """Hash raw token with SHA-256 for secure storage."""
    return hashlib.sha256(raw_token.strip().encode("utf-8")).hexdigest()


def create_magic_link_token(
    user: User,
    destination_path: str,
    expires_in: timedelta = None,
) -> tuple[str, MagicLinkToken]:
    """
    Generate a secure single-use magic link token for a user and target path.
    """
    if expires_in is None:
        hours = getattr(settings, "MAGIC_LINK_EXPIRE_HOURS", 72)
        expires_in = timedelta(hours=hours)

    raw_token = secrets.token_urlsafe(32)
    token_hash = hash_token(raw_token)
    expires_at = timezone.now() + expires_in

    clean_destination = destination_path.strip() if destination_path else "/"
    if not clean_destination.startswith("/"):
        clean_destination = f"/{clean_destination}"

    token_obj = MagicLinkToken.objects.create(
        user=user,
        token_hash=token_hash,
        destination_path=clean_destination,
        expires_at=expires_at,
    )
    return raw_token, token_obj


def build_magic_link_url(
    user: User,
    destination_path: str,
    expires_in: timedelta = None,
) -> str:
    """
    Generate the complete URL for an email link containing a magic link token.
    Points to frontend landing route: {FRONTEND_BASE_URL}/auth/magic-link?token=...&next=...&email=...
    """
    raw_token, _ = create_magic_link_token(
        user=user,
        destination_path=destination_path,
        expires_in=expires_in,
    )

    base = (getattr(settings, "FRONTEND_BASE_URL", "") or "").rstrip("/")
    clean_destination = destination_path.strip() if destination_path else "/"
    if not clean_destination.startswith("/"):
        clean_destination = f"/{clean_destination}"

    query = urlencode(
        {
            "token": raw_token,
            "next": clean_destination,
            "email": user.email,
        }
    )

    if base:
        return f"{base}/auth/magic-link?{query}"
    return f"/auth/magic-link?{query}"


def inspect_magic_link_token(raw_token: str) -> dict:
    """
    Inspect the status of a magic link token without consuming it.
    Safe for pre-checks and profile mismatch checks.
    """
    if not raw_token or not raw_token.strip():
        raise MagicLinkError(code="INVALID_TOKEN", message="Token is required.")

    token_hash = hash_token(raw_token)
    token_obj = (
        MagicLinkToken.objects.select_related("user")
        .filter(token_hash=token_hash)
        .first()
    )

    if not token_obj:
        raise MagicLinkError(code="INVALID_TOKEN", message="Invalid magic link token.")

    return {
        "email": token_obj.user.email,
        "destination_path": token_obj.destination_path,
        "is_valid": token_obj.is_valid,
        "is_expired": token_obj.is_expired,
        "is_consumed": token_obj.consumed_at is not None,
    }


def exchange_magic_link_token(
    raw_token: str,
    ip: str = None,
    user_agent: str = "",
) -> tuple[User, str, dict]:
    """
    Atomically exchange a single-use magic link token for JWT session tokens.
    """
    if not raw_token or not raw_token.strip():
        raise MagicLinkError(code="INVALID_TOKEN", message="Token is required.")

    token_hash = hash_token(raw_token)

    with transaction.atomic():
        token_obj = (
            MagicLinkToken.objects.select_for_update()
            .select_related("user")
            .filter(token_hash=token_hash)
            .first()
        )

        if not token_obj:
            raise MagicLinkError(code="INVALID_TOKEN", message="Invalid magic link token.")

        if token_obj.consumed_at is not None:
            raise MagicLinkError(
                code="TOKEN_ALREADY_USED",
                message="This magic link has already been used.",
            )

        if token_obj.is_expired:
            raise MagicLinkError(
                code="TOKEN_EXPIRED",
                message="This magic link has expired.",
            )

        user = token_obj.user
        if not user.is_active:
            raise MagicLinkError(
                code="USER_INACTIVE",
                message="User account is inactive.",
                status_code=403,
            )

        token_obj.consumed_at = timezone.now()
        token_obj.consumed_by_ip = ip
        token_obj.consumed_user_agent = user_agent[:1000] if user_agent else ""
        token_obj.save(update_fields=["consumed_at", "consumed_by_ip", "consumed_user_agent"])

    refresh = RefreshToken.for_user(user)
    tokens = {
        "access": str(refresh.access_token),
        "refresh": str(refresh),
    }

    if getattr(settings, "SIMPLE_JWT", {}).get("UPDATE_LAST_LOGIN", False):
        update_last_login(None, user)

    return user, token_obj.destination_path, tokens

