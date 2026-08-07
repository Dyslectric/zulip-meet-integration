"""Starting and listing the rooms inside a lounge.

Joining one is not here: that is minting a call token, and it belongs with the
rest of that in `zerver/views/video_calls.py` so there is exactly one place that
decides what a token says.
"""

from typing import Annotated, Any

from django.contrib.auth.models import AnonymousUser
from django.http import HttpRequest, HttpResponse
from django.utils.translation import gettext as _
from pydantic import Json, StringConstraints

from zerver.context_processors import get_valid_realm_from_request
from zerver.lib.exceptions import JsonableError, MissingAuthenticationError
from zerver.lib.guest_knocks import admit_guest_knock
from zerver.lib.lounges import (
    access_lounge_by_id,
    access_lounge_room_by_id,
    lounge_room_to_dict,
    notify_lounge_room_knock,
    reap_unjoined_lounge_rooms,
    user_may_knock,
    user_may_start_room,
    user_moderates_room,
)
from zerver.lib.response import json_success
from zerver.lib.stream_subscription import (
    get_active_subscriptions_for_stream_id,
    get_subscribed_stream_ids_for_user,
)
from zerver.lib.streams import access_web_public_stream
from zerver.lib.typed_endpoint import PathOnly, typed_endpoint
from zerver.lib.users import access_user_by_id
from zerver.models import LoungeRoom, Stream, UserProfile
from zerver.tornado.django_api import send_event_on_commit


def notify_lounge_rooms_changed(stream: Stream) -> None:
    """Tell a lounge's subscribers that its rooms are not what they were.

    Carries the channel and nothing else, on purpose. Whether a given user may
    join a given room depends on that user, so a single event cannot state it for
    everyone it goes to; rather than send a room dict that is right for some
    recipients and wrong for others, this says only "look again" and lets the
    listing endpoint answer per user. Rooms change rarely enough that the extra
    request costs nothing.
    """
    recipient_ids = list(
        get_active_subscriptions_for_stream_id(
            stream.id, include_deactivated_users=False
        ).values_list("user_profile_id", flat=True)
    )
    if not recipient_ids:
        return
    send_event_on_commit(
        stream.realm,
        {"type": "lounge_rooms", "channel_id": stream.id},
        recipient_ids,
    )


@typed_endpoint
def create_lounge_room(
    request: HttpRequest,
    user: UserProfile,
    *,
    stream_id: PathOnly[int],
    name: Annotated[str, StringConstraints(strip_whitespace=True)],
    is_private: Json[bool] = False,
) -> HttpResponse:
    """Start a room. The caller joins it separately, by asking for a token.

    Creating and joining are two steps rather than one so that joining has a
    single implementation: whoever starts a room enters it by exactly the path
    everyone else does.
    """
    stream = access_lounge_by_id(user, stream_id)
    if not user_may_start_room(user, stream):
        raise JsonableError(_("You do not have permission to start a room in this lounge."))

    if not name:
        raise JsonableError(_("Room name cannot be empty."))
    if len(name) > LoungeRoom.MAX_NAME_LENGTH:
        raise JsonableError(_("Room name is too long."))

    # Tidy before adding rather than after: the caller is about to look at this
    # lounge's rooms, and a stale one would be in the list they get back.
    reap_unjoined_lounge_rooms(stream)

    room = LoungeRoom.objects.create(channel=stream, name=name, creator=user, is_private=is_private)
    notify_lounge_rooms_changed(stream)
    return json_success(request, {"room": lounge_room_to_dict(room, stream=stream, user=user)})


@typed_endpoint
def knock_on_lounge_room(
    request: HttpRequest,
    user: UserProfile,
    *,
    room_id: PathOnly[int],
) -> HttpResponse:
    """Ask to be let into a room that would otherwise refuse you.

    Nothing is stored. The request reaches whoever is listening and is then
    over, so a knock cannot pile up, go stale, or need clearing out; a moderator
    who was not around when you knocked will not learn about it afterwards, and
    knocking again is the way to try again.
    """
    stream, room = access_lounge_room_by_id(user, room_id)
    if not user_may_knock(user, stream, room):
        # Covers both halves of "no": a room that would let you in anyway, and
        # one that does not take knocks. Neither is worth distinguishing to a
        # caller — there is nothing to ask for in the first case and nobody
        # listening in the second.
        raise JsonableError(_("You cannot ask to join this room."))
    notify_lounge_room_knock(stream, room, knocker=user)
    return json_success(request)


@typed_endpoint
def admit_to_lounge_room(
    request: HttpRequest,
    user: UserProfile,
    *,
    room_id: PathOnly[int],
    user_id: Json[int] | None = None,
    guest_knock_id: str | None = None,
) -> HttpResponse:
    """Let somebody in, by widening the invited set the join path already reads.

    Admitting an account holder adds nothing new to the way in: it makes the
    ordinary join path say yes, and that path stays the only one. So there is no
    second answer to "may this user enter" to keep in step with the first.

    Additive rather than a PATCH of the whole set, which is what the settings
    dialog sends. Two moderators answering two knocks in the same moment would
    each be working from a list taken before the other's change, and a
    replacement would silently undo one of them.

    A visitor has no account to add to that set, so their knock is marked
    admitted instead and they come back with it. The difference is where the
    "yes" is written down, not what it means: both end with the join path
    letting exactly one more person in.
    """
    stream, room = access_lounge_room_by_id(user, room_id)
    if not user_moderates_room(user, stream, room):
        raise JsonableError(_("You do not have permission to change this room."))

    if (user_id is None) == (guest_knock_id is None):
        raise JsonableError(_("Specify exactly one of user_id or guest_knock_id"))

    if guest_knock_id is not None:
        if not admit_guest_knock(
            realm_id=stream.realm_id, room_id=room.id, knock_id=guest_knock_id
        ):
            # The knock expired while the moderator was deciding, which is
            # ordinary rather than exceptional: they stand for two minutes.
            raise JsonableError(_("That request to join has expired."))
        # No `lounge_rooms` event: nobody's room list changes. The visitor is
        # polling for this answer, having no event queue of their own to push to.
        return json_success(request)

    assert user_id is not None
    admitted = access_user_by_id(user, user_id, allow_bots=False, for_admin=False)
    room.invited_users.add(admitted)
    # Reaches the admitted user too, whose sidebar then re-asks and finds the
    # room open to them. That is the whole of "you are in": there is no separate
    # message saying so, because the room growing a join button says it better.
    notify_lounge_rooms_changed(stream)
    return json_success(request)


@typed_endpoint
def update_lounge_room(
    request: HttpRequest,
    user: UserProfile,
    *,
    room_id: PathOnly[int],
    is_private: Json[bool] | None = None,
    knockable_by_users: Json[bool] | None = None,
    knockable_by_guests: Json[bool] | None = None,
    invited_user_ids: Json[list[int]] | None = None,
) -> HttpResponse:
    """Change a room's door policy while it is running.

    Only whoever moderates the room, which is the same set that gets a moderator
    claim in the call itself: the person who decides who may come in is the
    person the conference already treats as running it.

    **None of this ejects anybody.** Locking a room, or dropping somebody from
    the invited set, changes who may come in from now on and leaves the people
    already talking exactly where they are. That is the same rule as "no
    moderator present" — a door policy, not a kill switch — and it is why this
    endpoint has no need to talk to the conferencing service: everything it
    changes is decided the next time somebody asks for a token.

    `invited_user_ids` replaces the set rather than adding to it, which is what a
    pill box in a settings dialog naturally produces: the client sends the list
    it is showing. Omitting the field leaves the set alone.
    """
    stream, room = access_lounge_room_by_id(user, room_id)
    if not user_moderates_room(user, stream, room):
        raise JsonableError(_("You do not have permission to change this room."))

    update_fields = []
    if is_private is not None and is_private != room.is_private:
        room.is_private = is_private
        update_fields.append("is_private")
    if knockable_by_users is not None and knockable_by_users != room.knockable_by_users:
        room.knockable_by_users = knockable_by_users
        update_fields.append("knockable_by_users")
    if knockable_by_guests is not None and knockable_by_guests != room.knockable_by_guests:
        room.knockable_by_guests = knockable_by_guests
        update_fields.append("knockable_by_guests")
    if update_fields:
        room.save(update_fields=update_fields)

    invited_changed = False
    if invited_user_ids is not None:
        # Every id has to be a real, reachable account in this realm, so that a
        # moderator cannot stash arbitrary numbers here. Being invited is not by
        # itself a way into the lounge: the join path still asks for a
        # subscription, and this only unlocks the room for somebody who could
        # already reach it. Inviting a non-subscriber is therefore harmless and
        # simply does nothing until they are subscribed.
        invited = [
            access_user_by_id(user, user_id, allow_bots=False, for_admin=False)
            for user_id in sorted(set(invited_user_ids))
        ]
        if {u.id for u in invited} != {u.id for u in room.invited_users.all()}:
            room.invited_users.set(invited)
            invited_changed = True

    if update_fields or invited_changed:
        # Who may join is a per-user answer, so the event says only "look again";
        # see notify_lounge_rooms_changed.
        notify_lounge_rooms_changed(stream)

    return json_success(request, {"room": lounge_room_to_dict(room, stream=stream, user=user)})


@typed_endpoint
def get_lounge_rooms(
    request: HttpRequest,
    maybe_user_profile: UserProfile | AnonymousUser,
    *,
    stream_id: Json[int] | None = None,
) -> HttpResponse:
    """The live rooms in this user's lounges, or in one of them.

    Subscription is the entitlement, so the query is scoped to the lounges the
    user is in rather than filtered afterwards: a room in a lounge they are not
    subscribed to is not omitted from the answer, it is never in it.

    An unauthenticated visitor gets the web-public lounges instead, which is the
    set their sidebar shows anyway. Without this the guest call endpoint would
    have nothing to point at: a visitor cannot join a room they were never told
    exists, and a lounge that listed nothing to them would look idle rather than
    closed.
    """
    user: UserProfile | None = None
    if maybe_user_profile.is_authenticated:
        assert isinstance(maybe_user_profile, UserProfile)
        user = maybe_user_profile

    if user is None:
        realm = get_valid_realm_from_request(request)
        if stream_id is not None:
            stream = access_web_public_stream(stream_id, realm)
            if not stream.is_lounge:
                raise JsonableError(_("This channel is not a lounge."))
            lounges = [stream]
        else:
            if not realm.web_public_streams_enabled():
                raise MissingAuthenticationError
            lounges = list(
                Stream.objects.filter(
                    realm=realm, is_lounge=True, is_web_public=True, deactivated=False
                )
            )
        streams_by_id = {lounge.id: lounge for lounge in lounges}
        # Deliberately no reaping here. It deletes rows, and an unauthenticated
        # request should not be the thing that does that — the tidying is done
        # by anybody logged in who looks at the same lounge, which for a live
        # room is happening constantly.
    elif stream_id is not None:
        stream = access_lounge_by_id(user, stream_id)
        reap_unjoined_lounge_rooms(stream)
        streams_by_id = {stream.id: stream}
    else:
        subscribed = Stream.objects.filter(
            id__in=get_subscribed_stream_ids_for_user(user),
            realm=user.realm,
            is_lounge=True,
            deactivated=False,
        )
        streams_by_id = {lounge.id: lounge for lounge in subscribed}
        for lounge in streams_by_id.values():
            reap_unjoined_lounge_rooms(lounge)

    # The invited set is read for every room — to decide `can_join`, and again to
    # report it to a moderator — so it is fetched once for the whole listing
    # rather than once per room.
    queryset = (
        LoungeRoom.objects.filter(channel_id__in=streams_by_id)
        .prefetch_related("invited_users")
        .order_by("id")
    )
    rooms: list[dict[str, Any]] = [
        lounge_room_to_dict(room, stream=streams_by_id[room.channel_id], user=user)
        for room in queryset
    ]
    return json_success(request, {"rooms": rooms})
