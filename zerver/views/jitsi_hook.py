# The core message hook: internal, secret-authed send/edit for call messages.
#
# This is the enabling change of the "embedded calls / core-hook" design
# (docs/embedded-call-and-core-hook-design.md, section 1). The phase-3 bot posted
# call/roster messages through Zulip's PUBLIC API, which has two hard walls:
#
#   * it always sends AS THE BOT, so a DM/group message lands in a bot-inclusive
#     conversation, never the participants' real one; and
#   * a bot belongs to one realm, so multi-realm posting is impossible.
#
# Worse, the bot cannot EDIT a message older than the realm's
# ``message_content_edit_limit_seconds`` (600s by default) — so any call open
# longer than ten minutes freezes its roster, because every occupancy edit after
# that gets rejected by the public API's edit guards.
#
# These two endpoints move send/edit onto Zulip's server-privileged internal
# functions, which have none of those limits:
#
#   POST /api/internal/jitsi/message         -> send a call message, returns its id
#   POST /api/internal/jitsi/message/update  -> edit a call message by id
#
# Channels are authored by the realm's Notification Bot; DM/group messages are
# authored by the INITIATOR (so they appear in the real conversation, attributed
# to the person who started the call). Edits act as the message's own sender and
# deliberately bypass the content-edit-time-limit — that bypass is the whole
# reason this is a core hook and not a bot.
#
# SECURITY. These endpoints post and edit as ARBITRARY senders in ARBITRARY
# conversations — the most privileged surface in the system. Three rules, none
# optional:
#   1. Shared-secret bearer auth, compared in constant time. A bad or missing
#      secret gets a 404 (not 401): an undocumented endpoint should not confirm
#      it exists.
#   2. Never session/API-key authed and never on the REST route table, so no
#      logged-in user can reach it. It is a plain csrf-exempt view doing its own
#      auth.
#   3. Reachable only on the internal network. The bearer is the code-level gate;
#      the deployment gate is nginx restricting this path to the shared
#      `zulip-conferencing` docker subnet (see "Deploying" below).
#
# Drop this at zerver/views/jitsi_hook.py inside the container (same packaging as
# jitsi_calls.py) and register the two routes as described at the bottom.
#
# ---------------------------------------------------------------------------
# SIGNATURES PINNED TO ZULIP 12.1. The internal-send calls, the do_update_message
# request-object shape, render_incoming_message, MentionData/MentionBackend, and
# get_system_bot's import path were all checked against zulip/zulip tag 12.1 (not
# guessed). What remains is to RUN it: `./tools/test-backend` plus a live edit in
# the dev env, because a source read cannot catch a runtime wiring mistake.
# ---------------------------------------------------------------------------

import hmac
import json
import logging

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from zerver.actions.message_edit import build_message_edit_request, do_update_message
from zerver.actions.message_send import (
    internal_send_group_direct_message,
    internal_send_private_message,
    internal_send_stream_message,
    render_incoming_message,
)
from zerver.lib.mention import MentionBackend, MentionData
from zerver.lib.response import json_success
from zerver.lib.stream_subscription import get_active_subscriptions_for_stream_id
from zerver.models import Message, Realm, Stream, UserProfile
from zerver.models.users import get_system_bot
from zerver.tornado.django_api import send_event_on_commit

logger = logging.getLogger(__name__)


# -- auth --------------------------------------------------------------------


def _authorized(request: HttpRequest) -> bool:
    """Constant-time bearer check against the shared conferencing secret.

    The same secret the service already shares with Zulip for the phase-3
    `calls/created` notice and the occupancy query (settings.JITSI_CONFERENCING_
    SECRET == the service's EVENT_SYNC_SECRET). Reversed direction, same trust
    edge. An empty configured secret can never match a well-formed header, so a
    misconfigured deployment fails closed.
    """
    expected = getattr(settings, "JITSI_CONFERENCING_SECRET", "") or ""
    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not expected or not header.startswith(prefix):
        return False
    return hmac.compare_digest(header[len(prefix) :], expected)


def _not_found() -> HttpResponse:
    # 404, not 401: do not confirm the endpoint exists to an unauthorized caller.
    return JsonResponse({"result": "error", "msg": "Not found"}, status=404)


def _bad_request(msg: str) -> HttpResponse:
    return JsonResponse({"result": "error", "msg": msg}, status=400)


# -- sending -----------------------------------------------------------------


def _send_channel_message(realm: Realm, stream_id: int, topic: str, content: str) -> int | None:
    stream = Stream.objects.get(id=stream_id, realm=realm)
    # The Notification Bot exists in every realm and needs no subscription to
    # post via the internal path — this is exactly why the public-API bot could
    # not do multi-realm. A dedicated "Calls" bot is later polish; reusing
    # Notification Bot keeps v1 migration-free.
    sender = get_system_bot(settings.NOTIFICATION_BOT, realm.id)
    return internal_send_stream_message(sender, stream, topic, content)


def _send_direct_message(
    realm: Realm, initiator_id: int, participant_ids: list[int], content: str
) -> int | None:
    """Author a DM/group message AS THE INITIATOR into the real conversation.

    ``participant_ids`` is the full set including the initiator (the notice sends
    it that way). Authoring as the initiator is the capability the public bot API
    simply does not have: the message lands in the users' actual DM/group, not in
    a bot-inclusive one.
    """
    initiator = UserProfile.objects.get(id=initiator_id, realm=realm)
    full = sorted(set(participant_ids) | {initiator_id})
    others = [uid for uid in full if uid != initiator_id]

    if len(others) <= 1:
        # 1:1 DM (or a note-to-self). recipient is the other party, or the
        # initiator themselves for a self-DM.
        other_id = others[0] if others else initiator_id
        recipient = UserProfile.objects.get(id=other_id, realm=realm)
        return internal_send_private_message(initiator, recipient, content)

    recipients = list(UserProfile.objects.filter(id__in=full, realm=realm))
    return internal_send_group_direct_message(
        realm, initiator, content, recipient_users=recipients
    )


@csrf_exempt
def jitsi_hook_send(request: HttpRequest) -> HttpResponse:
    """POST /api/internal/jitsi/message — post a call message, return its id.

    Body (JSON): realm_id (int), content (str), topic (str, for channels), and
    exactly one of stream_id (int) or user_ids (list[int]); with user_ids,
    initiator_id (int) names the author.
    """
    if request.method != "POST":
        return _not_found()
    if not _authorized(request):
        return _not_found()

    data = _json_body(request)
    if data is None:
        return _bad_request("body must be JSON")

    realm_id = data.get("realm_id")
    content = data.get("content")
    if not isinstance(realm_id, int) or not isinstance(content, str) or not content:
        return _bad_request("realm_id (int) and content (str) are required")

    try:
        realm = Realm.objects.get(id=realm_id)
    except Realm.DoesNotExist:
        return _bad_request("unknown realm")

    stream_id = data.get("stream_id")
    user_ids = data.get("user_ids")
    try:
        if isinstance(stream_id, int):
            topic = str(data.get("topic") or "Calls")
            message_id = _send_channel_message(realm, stream_id, topic, content)
        elif isinstance(user_ids, list) and user_ids:
            initiator_id = data.get("initiator_id")
            if not isinstance(initiator_id, int):
                return _bad_request("initiator_id (int) is required for a direct message")
            message_id = _send_direct_message(
                realm, initiator_id, [int(u) for u in user_ids], content
            )
        else:
            return _bad_request("exactly one of stream_id or user_ids is required")
    except (Stream.DoesNotExist, UserProfile.DoesNotExist):
        # A conversation that no longer resolves is the caller's problem to see,
        # but it is a 400, not a 500: nothing here crashed.
        return _bad_request("conversation could not be resolved")

    if message_id is None:
        # internal_send_* returns None when it declined to send (e.g. an empty
        # recipient set). Report it rather than pretending a message exists.
        logger.warning("jitsi hook: internal send produced no message for realm %s", realm_id)
        return _bad_request("message was not sent")
    return json_success(request, {"message_id": message_id})


# -- editing -----------------------------------------------------------------


def _edit_message_content(message: Message, content: str) -> None:
    """Replace a message's content, acting as its own sender.

    Editing through this low-level path is what lets a call roster keep updating
    past the realm's content-edit time limit: that limit and every edit-policy
    check live ONLY in check_update_message (via validate_user_can_edit_message).
    build_message_edit_request and do_update_message do pure data work with no
    permission or deadline checks — confirmed against zulip/zulip tag 12.1. This
    is exactly why the hook exists and the public-API bot could not: the bot's
    edits freeze after ten minutes; these never do.

    A channel call message has sender=Notification Bot; a DM/group one has
    sender=initiator. Editing as the stored sender is correct and unrestricted.
    The shape below mirrors Zulip's own internal edit flow (build request →
    render → apply). Pinned to 12.1, still unrun until the dev-env test-backend.
    """
    mention_data = MentionData(
        mention_backend=MentionBackend(message.realm_id),
        content=content,
        message_sender=message.sender,
    )
    rendering_result = render_incoming_message(
        message, content, message.realm, mention_data=mention_data
    )
    message_edit_request = build_message_edit_request(
        message=message,
        user_profile=message.sender,
        propagate_mode="change_one",  # content-only edit: no topic/stream move
        content=content,
    )
    do_update_message(
        message.sender,  # user_profile — the acting user (the message's sender)
        message,  # target_message
        message_edit_request,
        False,  # send_notification_to_old_thread
        False,  # send_notification_to_new_thread
        rendering_result,
        set(),  # prior_mention_user_ids
        mention_data,
    )


@csrf_exempt
def jitsi_hook_update(request: HttpRequest) -> HttpResponse:
    """POST /api/internal/jitsi/message/update — edit a call message by id.

    Body (JSON): message_id (int), content (str). The message id is globally
    unique in Zulip, so this is realm-agnostic: the service can edit a message in
    any realm knowing only its id. An unknown id is a 400, not a crash.
    """
    if request.method != "POST":
        return _not_found()
    if not _authorized(request):
        return _not_found()

    data = _json_body(request)
    if data is None:
        return _bad_request("body must be JSON")

    message_id = data.get("message_id")
    content = data.get("content")
    if not isinstance(message_id, int) or not isinstance(content, str) or not content:
        return _bad_request("message_id (int) and content (str) are required")

    try:
        message = Message.objects.select_related("sender", "realm").get(id=message_id)
    except Message.DoesNotExist:
        return _bad_request("unknown message")

    _edit_message_content(message, content)
    return json_success(request, {})


# -- helpers -----------------------------------------------------------------


def _json_body(request: HttpRequest) -> dict | None:
    try:
        parsed = json.loads(request.body or b"{}")
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


@csrf_exempt
def jitsi_hook_occupancy(request: HttpRequest) -> HttpResponse:
    """POST /api/internal/jitsi/occupancy — push a channel's live occupancy to its
    subscribers as a `jitsi_occupancy` client event, so the call-aware sidebar
    updates the instant someone joins or leaves, with no polling.

    Body (JSON): realm_id (int), stream_id (int), active (bool), count (int),
    occupants (list of {user_id, name}). Bearer-authed like the other hook
    endpoints; the conferencing service is the only caller.
    """
    if request.method != "POST":
        return _not_found()
    if not _authorized(request):
        return _not_found()

    data = _json_body(request)
    if data is None:
        return _bad_request("invalid JSON")
    realm_id = data.get("realm_id")
    stream_id = data.get("stream_id")
    if not isinstance(realm_id, int) or not isinstance(stream_id, int):
        return _bad_request("realm_id and stream_id are required")

    try:
        realm = Realm.objects.get(id=realm_id)
        stream = Stream.objects.get(id=stream_id, realm=realm)
    except (Realm.DoesNotExist, Stream.DoesNotExist):
        # A call for a channel that has since gone away: nobody to notify, and not
        # an error worth a 400.
        return json_success(request)

    subscriber_ids = list(
        get_active_subscriptions_for_stream_id(
            stream.id, include_deactivated_users=False
        ).values_list("user_profile_id", flat=True)
    )
    if not subscriber_ids:
        return json_success(request)

    event = {
        "type": "jitsi_occupancy",
        "stream_id": stream.id,
        "active": bool(data.get("active", True)),
        "count": int(data.get("count", 0)),
        "occupants": data.get("occupants") or [],
    }
    send_event_on_commit(realm, event, subscriber_ids)
    return json_success(request)


# ---------------------------------------------------------------------------
# Deploying
#
# 1) Route registration. These are PLAIN Django views (their own bearer auth),
#    NOT rest_dispatch endpoints, so register them as bare paths in
#    zproject/urls.py — before the catch-all patterns, and outside the
#    `rest_path`/`/api/v1/` REST table:
#
#        from zerver.views.jitsi_hook import jitsi_hook_send, jitsi_hook_update
#        urls += [
#            path("api/internal/jitsi/message", jitsi_hook_send),
#            path("api/internal/jitsi/message/update", jitsi_hook_update),
#        ]
#
# 2) Reachability + isolation (nginx). The service is a separate container, so it
#    reaches Zulip over the shared `zulip-conferencing` docker network (the same
#    one phase 3 added for the reverse direction). In docker-zulip, custom app
#    location blocks live in nginx's `zulip-include-app.d/*.conf` and reuse the
#    common proxy include. In the custom image, drop a file there that proxies
#    this path to Django and allows ONLY the shared subnet:
#
#        # zulip-include-app.d/jitsi-internal.conf
#        location /api/internal/jitsi/ {
#            allow 172.20.0.0/16;   # the zulip-conferencing subnet
#            deny all;
#            include /etc/nginx/zulip-include/proxy;   # confirm include path
#            proxy_pass http://django;                 # confirm upstream name
#        }
#
#    so the bearer secret is the code gate and the subnet allowlist is the
#    network gate. CONFIRM against the running image whether the stock `location /`
#    already proxies `/api/internal/` (then this block only adds the deny-others
#    hardening) or blocks it (then this block is what lets the service reach it at
#    all). Zulip's own resolver handles the docker name fine in the other
#    direction, so set the service's ZULIP_HOOK_URL to this path by name.
#
# 3) Secret. Reuses settings.JITSI_CONFERENCING_SECRET (== the service's
#    EVENT_SYNC_SECRET). No new secret needed; a future hardening can split this
#    trust edge onto its own key.
# ---------------------------------------------------------------------------
