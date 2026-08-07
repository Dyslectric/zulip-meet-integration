"""Rooms inside a lounge: access, moderation, reaping and the wire shape.

The rules here are small but they are the whole of what a lounge is, so they live
in one place rather than being spelled out again at each endpoint that needs
them. Three of them matter:

* **Being subscribed to the lounge is the entitlement.** A room is not reachable
  by anyone who could not already see the lounge it is in, which means the
  ordinary channel access rules do all the work and rooms add no new way to
  reach a conversation.
* **A private room is visible but locked.** Everyone who can see the lounge sees
  that the room exists and who is in it; only those allowed may join. A lounge
  is an ambient-presence surface, and people disappearing into rooms nobody can
  see would defeat what it is for.
* **A room lasts only as long as it is had.** The row is deleted when the last
  person leaves, so there is no such thing as an old room and nothing to browse.
"""

from datetime import timedelta
from typing import Any

from django.utils.timezone import now as timezone_now
from django.utils.translation import gettext as _

from zerver.lib.call_presence import authenticated_user_is_present, moderator_is_present
from zerver.lib.exceptions import JsonableError
from zerver.lib.streams import access_stream_by_id
from zerver.lib.user_groups import get_recursive_group_members, is_user_in_group
from zerver.models import LoungeRoom, Stream, UserProfile
from zerver.models.streams import CallDoorPolicyEnum


def access_lounge_by_id(user: UserProfile, stream_id: int) -> Stream:
    """The lounge, or an error indistinguishable from it not being one.

    Subscription rather than mere readability is the bar, matching what a call in
    an ordinary channel asks for: a room is for a known set of people, and being
    able to read a channel is not being one of them.

    A web-public lounge is the deliberate exception, and the same one an ordinary
    web-public voice channel makes. Such a channel is open to anyone who can see
    it — that is what its administrator turned on — and anonymous visitors get in
    through the guest path. Holding a logged-in reader to a stricter bar than an
    anonymous one would be incoherent, so readability is enough here too.
    """
    stream, sub = access_stream_by_id(user, stream_id)
    if not stream.is_lounge:
        raise JsonableError(_("This channel is not a lounge."))
    if sub is None and not stream.is_web_public:
        raise JsonableError(_("Not subscribed to this lounge"))
    return stream


def access_lounge_room_by_id(user: UserProfile, room_id: int) -> tuple[Stream, LoungeRoom]:
    """A room and the lounge it is in, or an error saying it is over.

    A room that has since emptied is gone, and a request naming one is ordinary
    rather than exceptional: somebody clicked a room as the last person left. So
    the missing row is reported as the room having ended rather than as a bad
    request, and it says the same thing whether the row never existed or has been
    reaped — there is nothing to learn from the difference, and a room id is not
    a secret worth leaking the distinction over.

    The lounge is fetched through `access_lounge_by_id`, so reaching a room is
    exactly as hard as reaching the lounge it lives in and no easier.
    """
    try:
        room = LoungeRoom.objects.select_related("channel").get(
            id=room_id, channel__realm=user.realm
        )
    except LoungeRoom.DoesNotExist:
        raise JsonableError(_("This room has ended."))
    stream = access_lounge_by_id(user, room.channel_id)
    return stream, room


def user_moderates_room(user: UserProfile, stream: Stream, room: LoungeRoom) -> bool:
    """Who holds the moderator claim in a room.

    Whoever started it, plus whoever administers the lounge, plus realm admins.
    Deliberately not "whoever joined first", which is what Jitsi would do left to
    itself and which hands the room to a passer-by.
    """
    if user.is_realm_admin:
        return True
    if room.creator_id is not None and room.creator_id == user.id:
        return True
    return is_user_in_group(stream.can_administer_channel_group_id, user)


def channel_moderator_ids(stream: Stream) -> set[int]:
    """Who moderates calls in this channel: its administrators, plus the realm's.

    The same rule `create_jitsi_call` applies one user at a time when it decides
    whether to put a moderator claim in a token, stated as a set because two
    things need to ask "who" rather than "whether": addressing a knock, and
    deciding whether anybody who runs a call is currently in it.

    Restricted to humans. The set is used to address client events and to decide
    a door policy, and an administrator bot is neither in a call nor looking at a
    sidebar.
    """
    ids = set(
        stream.realm.get_human_admin_users().filter(is_active=True).values_list("id", flat=True)
    )
    ids.update(
        get_recursive_group_members(stream.can_administer_channel_group_id).values_list(
            "id", flat=True
        )
    )
    return ids


def room_moderator_ids(stream: Stream, room: LoungeRoom) -> set[int]:
    """Everyone `user_moderates_room` would say yes to, as a set.

    A second statement of the same rule, which is a thing this module otherwise
    refuses to have. It exists because a knock has to be *delivered* to the
    people who can answer it, and a predicate that takes one user at a time
    cannot be asked "who" without a query per subscriber. `test_lounges` asserts
    the two agree, which is what keeps the duplication honest.

    A room adds whoever started it to the channel's own moderators — that is the
    only difference between the two, and the whole of it.
    """
    ids = channel_moderator_ids(stream)
    if room.creator_id is not None:
        ids.add(room.creator_id)
    return ids


def user_may_join_room(user: UserProfile, stream: Stream, room: LoungeRoom) -> bool:
    """Whether this user may actually get into the room.

    A public room is open to everyone who can reach the lounge — that is the
    whole point of a lounge. A private one is open to whoever moderates it, plus
    anyone let in individually.

    This is the locked half of "visible but locked", and it stays the single
    place the rule lives: knocking widens the invited set rather than adding a
    second answer here, so everything that calls this keeps working.

    The invited set is walked in Python rather than queried, so that a caller
    listing a lounge's rooms can `prefetch_related("invited_users")` and pay for
    it once instead of once per room.
    """
    if not room.is_private:
        return True
    if user_moderates_room(user, stream, room):
        return True
    return any(invited.id == user.id for invited in room.invited_users.all())


def user_may_knock(user: UserProfile, stream: Stream, room: LoungeRoom) -> bool:
    """Whether this user may ask to be let in.

    Only meaningful for somebody the room would otherwise refuse — there is
    nothing to ask for otherwise. Guest *accounts* are governed by their own
    switch: an outside collaborator is a different proposition from a colleague,
    and a room may reasonably want the second without the first.

    Not about anonymous visitors, who cannot knock at all. Admitting somebody
    means adding them to a set of Zulip accounts, and a visitor with no account
    could not be admitted even if the room wanted to.
    """
    if not room.is_private or user_may_join_room(user, stream, room):
        return False
    if user.is_guest:
        return room.knockable_by_guests
    return room.knockable_by_users


def user_is_waiting_for_doorman(user: UserProfile | None, stream: Stream, room: LoungeRoom) -> bool:
    """Whether the room's door is shut on this user for want of whoever holds it.

    Deliberately a separate answer from `user_may_join_room` rather than folded
    into it. The two refusals are not the same thing and must not look the same:
    a private room is shut *to you*, permanently, and a room with no doorman is
    shut *to everyone*, until one walks in. Collapsing them would put a padlock
    on a public room and leave the user with no idea whether waiting would help.

    Reported to clients so that "you cannot get in right now" can be shown before
    the click rather than discovered by it, and so no client has to carry a second
    copy of a rule this fiddly. *Which* doorman is missing is left to the client
    to word from the channel's own policy; that much is presentation.

    `user` is None for an anonymous visitor, who is neither a moderator nor an
    account holder and so is exempt from nothing.
    """
    policy = stream.call_door_policy
    if policy == CallDoorPolicyEnum.moderator.value:
        # A moderator is never kept waiting; their arrival opens the door.
        if user is not None and user_moderates_room(user, stream, room):
            return False
        return not moderator_is_present(
            realm_id=stream.realm_id,
            stream_id=stream.id,
            lounge_room_id=room.id,
            moderator_ids=room_moderator_ids(stream, room),
        )
    if policy == CallDoorPolicyEnum.authenticated_user.value:
        # Any account holder holds this door open, so it never keeps one waiting.
        if user is not None:
            return False
        return not authenticated_user_is_present(
            realm_id=stream.realm_id, stream_id=stream.id, lounge_room_id=room.id
        )
    return False


def user_may_start_room(user: UserProfile, stream: Stream) -> bool:
    """Whether this user may start a room in this lounge.

    Deliberately separate from being subscribed. A lounge where everyone listens
    and a few convene is a reasonable thing to want, and tying the two together
    would force that to be expressed by keeping people out of the lounge itself.
    """
    return is_user_in_group(stream.can_create_rooms_group_id, user)


def reap_unjoined_lounge_rooms(stream: Stream) -> None:
    """Drop rooms that were started and never entered.

    Every other room is reaped by the conferencing service telling us it emptied.
    A room nobody ever joined never gets such a report — Prosody never saw it —
    so it needs a timer instead, or an abandoned "start a room" would sit in the
    sidebar advertising a conversation that is not happening.

    Called on the paths that read or add to a lounge's rooms rather than from a
    periodic job: those are exactly the moments the stale row would be seen, and
    a lounge nobody is looking at does not need tidying.
    """
    cutoff = timezone_now() - timedelta(seconds=LoungeRoom.UNJOINED_GRACE_SECONDS)
    LoungeRoom.objects.filter(
        channel=stream, first_joined_at=None, date_created__lt=cutoff
    ).delete()


def reconcile_lounge_rooms(live_room_ids: set[int]) -> None:
    """Drop rooms the conferencing service no longer knows about.

    A room ends when the service reports it empty. If that report never arrives —
    the service restarted and forgot the room, the request was lost, the process
    died mid-call — the row is stranded. Nothing else will ever say the room is
    over, so it sits in the sidebar advertising a conversation that is not
    happening, for as long as the deployment lives. The unjoined timer does not
    catch it either: that room *was* entered, so it is not the kind of room the
    timer is looking for.

    The service's live-room feed is the authority for what exists right now, so
    anything missing from it has ended. Rooms inside the grace period are spared:
    between a room being minted here and the service being told about it, there is
    a moment where it legitimately exists in one place and not the other.

    Callers must only pass a feed they actually fetched. Treating a failed fetch
    as an empty feed would delete every live room in the deployment the moment the
    service became briefly unreachable, which is precisely when it is least true.
    """
    cutoff = timezone_now() - timedelta(seconds=LoungeRoom.UNJOINED_GRACE_SECONDS)
    LoungeRoom.objects.filter(date_created__lt=cutoff).exclude(id__in=live_room_ids).delete()


def lounge_room_to_dict(
    room: LoungeRoom, *, stream: Stream, user: UserProfile | None
) -> dict[str, Any]:
    """What a client is told about a room.

    `can_join` is sent rather than left for the client to work out, because the
    rule behind it is going to grow a knock path and clients must not each carry
    their own copy of it. A locked room is still listed: it is visible, and the
    lock is the point.

    `invited_user_ids` goes only to whoever moderates the room, because it is the
    one thing here that is not already implied by the room being visible. Who has
    been let into a private conversation is a fact about that conversation, and
    the rest of the lounge is entitled to see that the room is locked, not to see
    the guest list.

    `user` is None for an anonymous visitor to a web-public lounge. A public room
    is open to them and a private one cannot be: being let in means being added
    to a set of Zulip accounts, and they have none — so there is nothing for them
    to knock with either, and nothing to administer.
    """
    if user is None:
        return {
            "id": room.id,
            "channel_id": room.channel_id,
            "name": room.name,
            "creator_id": room.creator_id,
            "is_private": room.is_private,
            "can_join": not room.is_private,
            "can_knock": False,
            "can_administer": False,
            "waiting_for_doorman": user_is_waiting_for_doorman(None, stream, room),
            "knockable_by_users": room.knockable_by_users,
            "knockable_by_guests": room.knockable_by_guests,
        }

    moderates = user_moderates_room(user, stream, room)
    room_dict: dict[str, Any] = {
        "id": room.id,
        "channel_id": room.channel_id,
        "name": room.name,
        "creator_id": room.creator_id,
        "is_private": room.is_private,
        "can_join": user_may_join_room(user, stream, room),
        "can_knock": user_may_knock(user, stream, room),
        "can_administer": moderates,
        "waiting_for_doorman": user_is_waiting_for_doorman(user, stream, room),
        "knockable_by_users": room.knockable_by_users,
        "knockable_by_guests": room.knockable_by_guests,
    }
    if moderates:
        room_dict["invited_user_ids"] = sorted(invited.id for invited in room.invited_users.all())
    return room_dict
