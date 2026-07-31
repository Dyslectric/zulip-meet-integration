"""Room naming and JWT minting for self-hosted Jitsi Meet deployments.

Jitsi's JWT support is a *capability* model, not an identity model. Prosody's
``mod_auth_token`` verifies the signature and then checks two things: that the
``room`` claim matches the room being joined, and that the ``sub`` claim matches
the tenant. Everything else in the token, including the whole ``context`` object,
is decorative — the Jitsi documentation is explicit that none of it is used for
validation.

Two consequences shape this module:

* A token with ``room`` set to ``*`` is a skeleton key for the entire
  deployment, so minting one requires an explicit opt-in that no caller here
  uses.
* Tokens are validated when the client connects and again at MUC join, and never
  again. There is no revocation. Short expiry therefore bounds how long a leaked
  token is useful for *joining*, and does nothing about an established session.
  Hence the deliberately short default lifetime.

Room names are derived rather than random because a room name is functionally a
secret in any system where a token might be over-issued, and because room names
end up in pasted links, browser history and screenshots.
"""

import hashlib
import hmac
import time
from typing import Any, TypedDict

import jwt
from django.conf import settings

# The token's only job is to get the user through the door. Prosody validates at
# connection and at MUC join and never again, so a short window costs the user
# nothing (the client requests a fresh token if theirs goes stale) and shrinks
# the blast radius of a leak substantially.
JITSI_TOKEN_LIFETIME_SECONDS = 120

# Allowance for clock skew between this server and Prosody. Without it, `nbf`
# validation fails intermittently and confusingly.
JITSI_TOKEN_NOT_BEFORE_SKEW_SECONDS = 5

ROOM_NAME_PREFIX = "c-"
ROOM_NAME_HASH_LENGTH = 16


class JitsiTokenError(Exception):
    """A token we refuse to mint because Prosody would handle it badly."""


class JitsiUserContext(TypedDict, total=False):
    id: str
    name: str
    email: str
    avatar: str
    moderator: str


def jitsi_jwt_is_configured() -> bool:
    return (
        settings.JITSI_SERVER_URL is not None
        and settings.JITSI_JWT_APP_ID is not None
        and (settings.JITSI_JWT_APP_SECRET is not None or settings.JITSI_JWT_PRIVATE_KEY is not None)
    )


def derive_room_name(scope: str, epoch: int) -> str:
    """Derive an unguessable, stable room name for a conversation.

    ``scope`` identifies the conversation (a channel, or a sorted set of user
    IDs); ``epoch`` is a counter that rotates the room. Incrementing it gives a
    clean "start a fresh meeting" primitive and a clean recovery path if a link
    leaks. Rotating ``JITSI_ROOM_KEY`` rekeys every room at once.
    """
    if settings.JITSI_ROOM_KEY is None:
        raise JitsiTokenError("JITSI_ROOM_KEY is not configured")

    digest = hmac.new(
        settings.JITSI_ROOM_KEY.encode(),
        f"{scope}|{epoch}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return ROOM_NAME_PREFIX + digest[:ROOM_NAME_HASH_LENGTH]


def channel_scope(realm_id: int, stream_id: int) -> str:
    return f"realm:{realm_id}|channel:{stream_id}"


def direct_message_scope(realm_id: int, user_ids: list[int]) -> str:
    # Sorted so that either participant derives the same room.
    return f"realm:{realm_id}|dm:" + ",".join(str(user_id) for user_id in sorted(set(user_ids)))


def build_user_context(
    *,
    user_id: int,
    full_name: str,
    email: str = "",
    avatar_url: str = "",
    is_moderator: bool = False,
) -> JitsiUserContext:
    """Build ``context.user``.

    Every field must be a valid string. Passing ``None`` or a numeric type makes
    Prosody throw rather than degrade gracefully, and Zulip user IDs are
    integers, so the coercion here is load-bearing rather than cosmetic. Fields
    with no value are omitted entirely rather than sent as ``null``.

    ``moderator`` is a *string* nested inside ``context.user``, matching the
    shape 8x8's JaaS documents. There is no blessed top-level ``moderator``
    claim in upstream Jitsi; the ``token_affiliation`` module reads it from here.
    """
    context: JitsiUserContext = {
        "id": str(user_id),
        "name": full_name,
        "moderator": "true" if is_moderator else "false",
    }
    if email:
        context["email"] = email
    if avatar_url:
        context["avatar"] = avatar_url
    return context


def mint_jitsi_token(
    *,
    tenant: str,
    room: str,
    user_context: JitsiUserContext,
    group: str | None = None,
    lifetime_seconds: int = JITSI_TOKEN_LIFETIME_SECONDS,
) -> str:
    """Mint a narrow, short-lived, single-room capability token."""
    if not jitsi_jwt_is_configured():
        raise JitsiTokenError("Jitsi JWT is not configured")

    if tenant != tenant.lower():
        # `sub` must be the lowercase tenant; Prosody's domain verification
        # compares it against the tenant path segment.
        raise JitsiTokenError(f"tenant must be lowercase, got {tenant!r}")

    if "*" in room:
        # A wildcard room claim is catastrophic and the failure mode is silent.
        # Nothing in Zulip should ever mint one.
        raise JitsiTokenError("refusing to mint a wildcard room token")

    for field, value in user_context.items():
        if not isinstance(value, str):
            raise JitsiTokenError(f"context.user.{field} must be a string")

    issued_at = int(time.time())
    payload: dict[str, Any] = {
        "iss": settings.JITSI_JWT_ISSUER,
        "aud": settings.JITSI_JWT_AUDIENCE,
        "sub": tenant,
        "room": room,
        "iat": issued_at,
        "nbf": issued_at - JITSI_TOKEN_NOT_BEFORE_SKEW_SECONDS,
        "exp": issued_at + lifetime_seconds,
        "context": {
            "user": user_context,
            # Opt-in per deployment. Defaulting these off means a bug in
            # issuance logic cannot silently enable recording.
            "features": {
                "recording": False,
                "livestreaming": False,
                "transcription": False,
                "outbound-call": False,
            },
        },
    }
    if group is not None:
        payload["context"]["group"] = group

    if settings.JITSI_JWT_PRIVATE_KEY is not None:
        # Prosody locates the public key by fetching sha256(kid).pem from
        # asap_key_server, which is a static file server and NOT a JWKS
        # endpoint. See the deployment notes in prod_settings_template.py.
        if settings.JITSI_JWT_KEY_ID is None:
            raise JitsiTokenError("JITSI_JWT_KEY_ID is required for asymmetric signing")
        return jwt.encode(
            payload,
            settings.JITSI_JWT_PRIVATE_KEY,
            algorithm="RS256",
            headers={"kid": settings.JITSI_JWT_KEY_ID},
        )

    return jwt.encode(payload, settings.JITSI_JWT_APP_SECRET, algorithm="HS256")
