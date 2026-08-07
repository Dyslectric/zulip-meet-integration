"""Who is in a call right now, remembered only long enough to answer one question.

`Stream.call_door_policy` needs to know, at the moment a token is minted, who is
currently in the call — a moderator, or anybody with an account, depending on
which doorman the channel asked for. Two things make that awkward: the answer
lives in the conferencing service, and the join path cannot afford a network
round trip to ask — a call people are trying to get into is exactly the wrong
place to add a new way to fail.

So Zulip keeps its own note. The occupancy hook already receives every report the
service sends, with the user ids of who is in the room, and writing those down as
they arrive costs nothing.

Three things about how that note is kept:

* **It is a cache, not a table.** Everything here is call state, which is not the
  kind of thing `Stream` or `LoungeRoom` hold — those describe how a conversation
  is configured, not what is happening in it this minute.

* **A moderator has to be positively visible.** Anything else — a call nobody has
  joined, a call whose occupants include no moderator, a call we have no report
  for — means no moderator is in there as far as this deployment can tell, and a
  non-moderator is turned away.

  This is the opposite of what an earlier version did. Treating "no report" as
  "we do not know, so let them in" sounds like the cautious choice, but a call
  nobody has started has no report either — so the very first person into a
  channel with the setting on was always let in, moderator or not, and the
  setting did nothing at all. That was found by testing it.

  What makes failing closed safe here is that **a moderator is never refused**.
  If a report really has been lost under a live call, the fix is for a moderator
  to walk in: their join re-reports the roster and the door opens again for
  everyone. There is no state a non-moderator can get stuck in that a moderator
  cannot clear by doing the obvious thing.

* **Only the occupant ids are stored, never a verdict.** Deciding who counts as a
  moderator takes queries, and occupancy reports arrive on every join and leave
  while a token is minted rarely. Storing the raw ids puts the work where it is
  needed and keeps the hook a pure write.
"""

from zerver.lib.cache import cache_delete, cache_get, cache_set

#: How long a report is trusted with nothing further heard. Reports arrive on
#: every join and leave, so this only bites in a call that has gone quiet for an
#: hour without ending — after which a non-moderator is turned away until a
#: moderator's arrival refreshes the roster.
CALL_OCCUPANTS_CACHE_TTL_SECONDS = 60 * 60


def call_occupants_cache_key(
    *, realm_id: int, stream_id: int, lounge_room_id: int | None = None
) -> str:
    room = "channel" if lounge_room_id is None else f"room:{lounge_room_id}"
    return f"call_occupants:{realm_id}:{stream_id}:{room}"


def record_call_occupants(
    *,
    realm_id: int,
    stream_id: int,
    lounge_room_id: int | None,
    active: bool,
    user_ids: list[int],
) -> None:
    """Note who the conferencing service says is in this call.

    A report saying the call is over drops the note rather than storing an empty
    one. The two are not the same: an empty roster in a live call means nobody is
    in it *right now*, and a dropped note means we have nothing to say — which is
    correct once there is no call to say anything about.
    """
    key = call_occupants_cache_key(
        realm_id=realm_id, stream_id=stream_id, lounge_room_id=lounge_room_id
    )
    if not active:
        cache_delete(key)
        return
    cache_set(key, sorted(set(user_ids)), timeout=CALL_OCCUPANTS_CACHE_TTL_SECONDS)


def get_call_occupants(
    *, realm_id: int, stream_id: int, lounge_room_id: int | None = None
) -> list[int] | None:
    """The occupants last reported, or None if we have no report to go on.

    None and an empty list are different facts — "we have heard nothing" against
    "the call is live and empty" — and this keeps them apart even though the one
    caller today treats them the same. Anything that later wants to distinguish
    them, such as telling a user why they were turned away, needs them separate.
    """
    occupants = cache_get(
        call_occupants_cache_key(
            realm_id=realm_id, stream_id=stream_id, lounge_room_id=lounge_room_id
        )
    )
    if occupants is None:
        return None
    # cache_get returns the stored tuple; the value we put in is the first item.
    value = occupants[0] if isinstance(occupants, tuple) else occupants
    if not isinstance(value, list):
        return None
    return [user_id for user_id in value if isinstance(user_id, int)]


def moderator_is_present(
    *, realm_id: int, stream_id: int, lounge_room_id: int | None, moderator_ids: set[int]
) -> bool:
    """Whether somebody who runs this call can be seen in it.

    Phrased as the thing a caller needs to be true, because that is the only
    state that admits anybody: a report we actually have, naming somebody from
    `moderator_ids`. The three ways of failing — a call nobody has joined, a call
    with no moderator among its occupants, and a call we have heard nothing about
    — are one answer here, because a deployment that cannot see a moderator has
    no grounds for saying one is there.
    """
    occupants = get_call_occupants(
        realm_id=realm_id, stream_id=stream_id, lounge_room_id=lounge_room_id
    )
    if occupants is None:
        return False
    return bool(set(occupants) & moderator_ids)


def authenticated_user_is_present(
    *, realm_id: int, stream_id: int, lounge_room_id: int | None
) -> bool:
    """Whether anybody with a Zulip account can be seen in this call.

    Needs no id list to compare against, because the roster is already only
    account holders: the occupancy hook keeps the integer user ids and drops the
    rest, so an anonymous visitor never appears here at all. A non-empty roster
    therefore *is* the answer, and a visitor can never be mistaken for the person
    holding the door open.
    """
    return bool(
        get_call_occupants(realm_id=realm_id, stream_id=stream_id, lounge_room_id=lounge_room_id)
    )
