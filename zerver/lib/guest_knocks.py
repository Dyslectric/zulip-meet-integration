"""A visitor asking to be let into a private room, and being let in.

Admitting a Zulip user widens `LoungeRoom.invited_users`, which is a set of
accounts. A visitor has no account, so there is nothing to put in it — which is
why anonymous knocking looked impossible and was documented as such. What was
missing is that being admitted does not have to mean *becoming a member of a
set*; it only has to mean *this one person may come through, now*.

So a guest knock is a short-lived capability, minted when they ask and spent when
they join:

* **The knock id is the identity.** It is unguessable, belongs to one room, and
  is worth nothing anywhere else. The visitor never acquires a name the
  deployment has to store, and nothing about them outlives the conversation.

* **It is a cache entry, not a row**, for the same reason everything else about a
  room is: a room lasts as long as it is had, and a request to enter one cannot
  outlive the room it is about. An entry that expires or is evicted simply means
  the visitor has to knock again, which is the correct behaviour and needs no
  cleanup path.

* **The name is theirs to give and is marked as unverified wherever it is
  shown.** A moderator deciding whether to admit somebody deserves to see who is
  asking; they also deserve not to be fooled by it, so the name a visitor types
  is treated exactly as their display name is — kept, bounded, and never
  presented as though the deployment stands behind it.
"""

import secrets

from zerver.lib.cache import cache_delete, cache_get, cache_set

#: How long a knock stands before the visitor has to ask again. Matches the
#: client's own sense of a live request: a knock is somebody at the door now, and
#: somebody who wandered off two minutes ago is not.
GUEST_KNOCK_TTL_SECONDS = 120

#: Long enough that guessing one is not a way in, since the id *is* the
#: entitlement once a moderator has admitted it.
GUEST_KNOCK_ID_BYTES = 16


def guest_knock_cache_key(*, realm_id: int, room_id: int, knock_id: str) -> str:
    return f"guest_knock:{realm_id}:{room_id}:{knock_id}"


def record_guest_knock(*, realm_id: int, room_id: int, display_name: str) -> str:
    """Note that somebody is at the door, and hand them the id to come back with."""
    knock_id = secrets.token_urlsafe(GUEST_KNOCK_ID_BYTES)
    cache_set(
        guest_knock_cache_key(realm_id=realm_id, room_id=room_id, knock_id=knock_id),
        {"name": display_name, "admitted": False},
        timeout=GUEST_KNOCK_TTL_SECONDS,
    )
    return knock_id


def get_guest_knock(*, realm_id: int, room_id: int, knock_id: str) -> dict[str, object] | None:
    """The knock, or None if it never existed, expired, or is for another room.

    Scoped by room in the key rather than checked afterwards, so a knock admitted
    to one room is not a way into the room next door.
    """
    stored = cache_get(guest_knock_cache_key(realm_id=realm_id, room_id=room_id, knock_id=knock_id))
    if stored is None:
        return None
    # cache_get hands back the stored tuple; our value is its first item.
    value = stored[0] if isinstance(stored, tuple) else stored
    if not isinstance(value, dict):
        return None
    return value


def admit_guest_knock(*, realm_id: int, room_id: int, knock_id: str) -> bool:
    """Let this one visitor through. False if there is no such knock to answer.

    The TTL is deliberately *not* extended here. Admitting says "come in now",
    not "you may come in whenever": a visitor who is admitted and wanders off has
    to ask again, which is the same rule everybody else's knock follows.
    """
    knock = get_guest_knock(realm_id=realm_id, room_id=room_id, knock_id=knock_id)
    if knock is None:
        return False
    knock["admitted"] = True
    cache_set(
        guest_knock_cache_key(realm_id=realm_id, room_id=room_id, knock_id=knock_id),
        knock,
        timeout=GUEST_KNOCK_TTL_SECONDS,
    )
    return True


def guest_knock_is_admitted(*, realm_id: int, room_id: int, knock_id: str) -> bool:
    knock = get_guest_knock(realm_id=realm_id, room_id=room_id, knock_id=knock_id)
    return knock is not None and knock.get("admitted") is True


def spend_guest_knock(*, realm_id: int, room_id: int, knock_id: str) -> None:
    """Drop a knock that has been used to get in.

    One knock, one entry. Without this the id would stay good for the rest of its
    two minutes and could be passed to somebody else — a small window, but the
    whole point of the id is that it admits exactly the person the moderator
    looked at and said yes to.
    """
    cache_delete(guest_knock_cache_key(realm_id=realm_id, room_id=room_id, knock_id=knock_id))
