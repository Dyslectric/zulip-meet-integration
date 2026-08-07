from datetime import timedelta
from typing import Any

import orjson
from django.utils.timezone import now as timezone_now
from typing_extensions import override

from zerver.lib.call_presence import get_call_occupants
from zerver.lib.lounges import reconcile_lounge_rooms, room_moderator_ids, user_moderates_room
from zerver.lib.test_classes import ZulipTestCase
from zerver.models import LoungeRoom, UserProfile
from zerver.models.streams import CallDoorPolicyEnum, get_stream

CONFERENCING_SECRET = "conferencing-secret-of-some-length"

JWT_SETTINGS: dict[str, Any] = dict(
    JITSI_SERVER_URL="https://jitsi.example.com",
    JITSI_JWT_APP_ID="zulip",
    JITSI_JWT_APP_SECRET="test-app-secret-of-at-least-32-bytes!!",
    JITSI_JWT_PRIVATE_KEY=None,
    JITSI_JWT_ISSUER="zulip",
    JITSI_JWT_AUDIENCE="jitsi",
    JITSI_ROOM_KEY="test-room-key",
    JITSI_DEFAULT_TENANT=None,
    JITSI_TENANT_BY_GROUP={},
    JITSI_CONFERENCING_URL=None,
)


class LoungeRoomTest(ZulipTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.user = self.example_user("hamlet")
        self.other = self.example_user("cordelia")
        self.login_user(self.user)

        self.subscribe(self.user, "Denmark")
        self.subscribe(self.other, "Denmark")
        self.lounge = get_stream("Denmark", self.user.realm)
        self.lounge.is_lounge = True
        self.lounge.voice_video_enabled = False
        self.lounge.save(update_fields=["is_lounge", "voice_video_enabled"])

    def start_room(self, name: str = "Design sync", *, is_private: bool = False) -> dict[str, Any]:
        result = self.client_post(
            f"/json/lounges/{self.lounge.id}/rooms",
            {"name": name, "is_private": orjson.dumps(is_private).decode()},
        )
        data = self.assert_json_success(result)
        room = data["room"]
        assert isinstance(room, dict)
        return room

    # -- starting a room --------------------------------------------------

    def test_starting_a_room_requires_a_lounge(self) -> None:
        self.lounge.is_lounge = False
        self.lounge.save(update_fields=["is_lounge"])
        result = self.client_post(f"/json/lounges/{self.lounge.id}/rooms", {"name": "Nope"})
        self.assert_json_error(result, "This channel is not a lounge.")

    def test_starting_a_room_requires_subscription(self) -> None:
        """Being able to read the lounge is not being one of the people in it.
        A room is a call, and a call is for a known set of people."""
        self.unsubscribe(self.user, "Denmark")
        result = self.client_post(f"/json/lounges/{self.lounge.id}/rooms", {"name": "Nope"})
        self.assert_json_error(result, "Not subscribed to this lounge")

    def test_a_room_needs_a_name(self) -> None:
        result = self.client_post(f"/json/lounges/{self.lounge.id}/rooms", {"name": "   "})
        self.assert_json_error(result, "Room name cannot be empty.")

        long_name = "x" * (LoungeRoom.MAX_NAME_LENGTH + 1)
        result = self.client_post(f"/json/lounges/{self.lounge.id}/rooms", {"name": long_name})
        self.assert_json_error(result, "Room name is too long.")

    def test_starting_a_room_records_who_started_it(self) -> None:
        room = self.start_room()
        self.assertEqual(room["name"], "Design sync")
        self.assertEqual(room["creator_id"], self.user.id)
        self.assertFalse(room["is_private"])
        self.assertTrue(room["can_join"])

    # -- listing ----------------------------------------------------------

    def test_an_idle_lounge_lists_nothing(self) -> None:
        result = self.client_get("/json/lounges/rooms")
        self.assertEqual(self.assert_json_success(result)["rooms"], [])

    def test_rooms_are_listed_to_the_lounge(self) -> None:
        room = self.start_room()
        self.login_user(self.other)
        result = self.client_get("/json/lounges/rooms")
        rooms = self.assert_json_success(result)["rooms"]
        self.assertEqual([r["id"] for r in rooms], [room["id"]])

    def test_a_lounge_you_are_not_in_is_not_listed(self) -> None:
        self.start_room()
        outsider = self.example_user("othello")
        # Subscribed by default in the test fixtures, which would have made this
        # pass without testing anything.
        self.unsubscribe(outsider, "Denmark")
        self.login_user(outsider)
        result = self.client_get("/json/lounges/rooms")
        self.assertEqual(self.assert_json_success(result)["rooms"], [])

    def test_a_web_public_lounge_needs_no_subscription(self) -> None:
        """The same exception an ordinary web-public voice channel makes. Such a
        channel is open to anyone who can see it, and anonymous visitors get in
        through the guest path -- so holding a logged-in reader to a stricter bar
        than an anonymous one would be incoherent."""
        self.lounge.is_web_public = True
        self.lounge.save(update_fields=["is_web_public"])
        reader = self.example_user("othello")
        self.unsubscribe(reader, "Denmark")
        self.login_user(reader)

        room = None
        with self.settings(**JWT_SETTINGS):
            self.login_user(self.user)
            room = self.start_room()
            self.login_user(reader)
            result = self.client_post("/json/calls/jitsi/create", {"lounge_room_id": room["id"]})
        self.assert_json_success(result)

    def test_a_private_room_is_visible_but_locked(self) -> None:
        """Visible but locked is the whole design: a lounge is an ambient-presence
        surface, so people must not vanish into rooms nobody can see."""
        room = self.start_room("Quiet word", is_private=True)

        self.login_user(self.other)
        result = self.client_get("/json/lounges/rooms")
        listed = self.assert_json_success(result)["rooms"]
        self.assertEqual([r["id"] for r in listed], [room["id"]])
        self.assertTrue(listed[0]["is_private"])
        self.assertFalse(listed[0]["can_join"])

    # -- room settings ----------------------------------------------------

    def patch_room(self, room_id: int, **params: Any) -> Any:
        return self.client_patch(f"/json/lounges/rooms/{room_id}", params)

    def test_only_a_moderator_may_change_a_room(self) -> None:
        room = self.start_room()
        self.login_user(self.other)
        result = self.patch_room(room["id"], is_private="true")
        self.assert_json_error(result, "You do not have permission to change this room.")
        self.assertFalse(LoungeRoom.objects.get(id=room["id"]).is_private)

    def test_a_room_that_has_ended_cannot_be_changed(self) -> None:
        room = self.start_room()
        LoungeRoom.objects.filter(id=room["id"]).delete()
        result = self.patch_room(room["id"], is_private="true")
        self.assert_json_error(result, "This room has ended.")

    def test_changing_a_room_needs_access_to_its_lounge(self) -> None:
        """A room id is not a way past the lounge: an outsider gets the same
        answer they would get for the lounge itself."""
        room = self.start_room()
        outsider = self.example_user("othello")
        self.unsubscribe(outsider, "Denmark")
        self.login_user(outsider)
        result = self.patch_room(room["id"], is_private="true")
        self.assert_json_error(result, "Not subscribed to this lounge")

    def test_locking_a_room_after_the_fact(self) -> None:
        room = self.start_room()
        result = self.patch_room(room["id"], is_private="true")
        updated = self.assert_json_success(result)["room"]
        self.assertTrue(updated["is_private"])

        # Locked from now on: everyone else is refused, the moderator is not.
        self.login_user(self.other)
        rooms = self.assert_json_success(self.client_get("/json/lounges/rooms"))["rooms"]
        self.assertFalse(rooms[0]["can_join"])

    def test_the_knock_switches_are_settable(self) -> None:
        room = self.start_room("Quiet word", is_private=True)
        result = self.patch_room(room["id"], knockable_by_users="false", knockable_by_guests="true")
        updated = self.assert_json_success(result)["room"]
        self.assertFalse(updated["knockable_by_users"])
        self.assertTrue(updated["knockable_by_guests"])

    def test_omitted_settings_are_left_alone(self) -> None:
        """PATCH means what it says: a dialog that only changed one switch must
        not quietly reset the others to whatever its defaults were."""
        room = self.start_room("Quiet word", is_private=True)
        self.patch_room(room["id"], knockable_by_users="false")
        updated = self.assert_json_success(self.patch_room(room["id"], is_private="false"))["room"]
        self.assertFalse(updated["is_private"])
        self.assertFalse(updated["knockable_by_users"])

    def test_inviting_someone_unlocks_a_private_room_for_them(self) -> None:
        room = self.start_room("Quiet word", is_private=True)
        self.patch_room(room["id"], invited_user_ids=orjson.dumps([self.other.id]).decode())

        self.login_user(self.other)
        with self.settings(**JWT_SETTINGS):
            result = self.client_post("/json/calls/jitsi/create", {"lounge_room_id": room["id"]})
        self.assert_json_success(result)

    def test_the_invited_set_is_replaced_not_added_to(self) -> None:
        room = self.start_room("Quiet word", is_private=True)
        third = self.example_user("othello")
        self.patch_room(room["id"], invited_user_ids=orjson.dumps([self.other.id]).decode())
        updated = self.assert_json_success(
            self.patch_room(room["id"], invited_user_ids=orjson.dumps([third.id]).decode())
        )["room"]
        self.assertEqual(updated["invited_user_ids"], [third.id])

        # Uninviting does not eject: it changes who may come in from now on.
        self.login_user(self.other)
        with self.settings(**JWT_SETTINGS):
            result = self.client_post("/json/calls/jitsi/create", {"lounge_room_id": room["id"]})
        self.assert_json_error(result, "This room is private.")

    def test_only_real_accounts_may_be_invited(self) -> None:
        room = self.start_room("Quiet word", is_private=True)
        result = self.patch_room(room["id"], invited_user_ids=orjson.dumps([-1]).decode())
        self.assert_json_error(result, "No such user")

    def test_the_guest_list_is_only_shown_to_moderators(self) -> None:
        """The lounge is entitled to see that a room is locked, not to see who
        has been let into it."""
        room = self.start_room("Quiet word", is_private=True)
        self.patch_room(room["id"], invited_user_ids=orjson.dumps([self.other.id]).decode())

        mine = self.assert_json_success(self.client_get("/json/lounges/rooms"))["rooms"][0]
        self.assertTrue(mine["can_administer"])
        self.assertEqual(mine["invited_user_ids"], [self.other.id])

        self.login_user(self.other)
        theirs = self.assert_json_success(self.client_get("/json/lounges/rooms"))["rooms"][0]
        self.assertFalse(theirs["can_administer"])
        self.assertNotIn("invited_user_ids", theirs)

    # -- knocking and admitting -------------------------------------------

    def test_you_cannot_knock_on_a_room_that_would_let_you_in(self) -> None:
        """There is nothing to ask for. Both halves of "no" say the same thing:
        neither is worth distinguishing to the caller."""
        room = self.start_room()
        self.login_user(self.other)
        result = self.client_post(f"/json/lounges/rooms/{room['id']}/knock")
        self.assert_json_error(result, "You cannot ask to join this room.")

    def test_you_cannot_knock_on_a_room_that_does_not_take_knocks(self) -> None:
        room = self.start_room("Quiet word", is_private=True)
        self.patch_room(room["id"], knockable_by_users="false")
        self.login_user(self.other)
        result = self.client_post(f"/json/lounges/rooms/{room['id']}/knock")
        self.assert_json_error(result, "You cannot ask to join this room.")

    def test_a_knock_reaches_the_rooms_moderators(self) -> None:
        room = self.start_room("Quiet word", is_private=True)
        self.login_user(self.other)
        with self.capture_send_event_calls(expected_num_events=1) as events:
            self.assert_json_success(self.client_post(f"/json/lounges/rooms/{room['id']}/knock"))
        self.assertEqual(events[0]["event"]["type"], "lounge_knock")
        self.assertEqual(events[0]["event"]["room_id"], room["id"])
        self.assertEqual(events[0]["event"]["user_id"], self.other.id)
        # The creator moderates the room, so they are told; the knocker is not.
        self.assertIn(self.user.id, events[0]["users"])
        self.assertNotIn(self.other.id, events[0]["users"])

    def test_the_moderator_set_matches_the_moderator_rule(self) -> None:
        """`room_moderator_ids` restates `user_moderates_room` so a knock can be
        addressed to a set rather than asked one user at a time. The two drifting
        apart would deliver knocks to people who cannot answer them, or hide them
        from people who can, so it is asserted rather than assumed."""
        room_dict = self.start_room()
        room = LoungeRoom.objects.get(id=room_dict["id"])
        moderator_ids = room_moderator_ids(self.lounge, room)

        for user in UserProfile.objects.filter(realm=self.user.realm, is_active=True, is_bot=False):
            self.assertEqual(
                user.id in moderator_ids,
                user_moderates_room(user, self.lounge, room),
                f"{user.email} disagrees",
            )

    def test_admitting_someone_lets_them_in(self) -> None:
        room = self.start_room("Quiet word", is_private=True)
        self.assert_json_success(
            self.client_post(f"/json/lounges/rooms/{room['id']}/admit", {"user_id": self.other.id})
        )
        self.login_user(self.other)
        with self.settings(**JWT_SETTINGS):
            result = self.client_post("/json/calls/jitsi/create", {"lounge_room_id": room["id"]})
        self.assert_json_success(result)

    def test_admitting_adds_rather_than_replaces(self) -> None:
        """Two moderators answering two knocks in the same moment must not undo
        each other, which a whole-set replacement would do."""
        room = self.start_room("Quiet word", is_private=True)
        third = self.example_user("othello")
        self.client_post(f"/json/lounges/rooms/{room['id']}/admit", {"user_id": self.other.id})
        self.client_post(f"/json/lounges/rooms/{room['id']}/admit", {"user_id": third.id})

        listed = self.assert_json_success(self.client_get("/json/lounges/rooms"))["rooms"][0]
        self.assertEqual(listed["invited_user_ids"], sorted([self.other.id, third.id]))

    def test_only_a_moderator_may_admit(self) -> None:
        room = self.start_room("Quiet word", is_private=True)
        third = self.example_user("othello")
        self.login_user(self.other)
        result = self.client_post(f"/json/lounges/rooms/{room['id']}/admit", {"user_id": third.id})
        self.assert_json_error(result, "You do not have permission to change this room.")

    def test_admitting_tells_the_lounge_to_look_again(self) -> None:
        """How the admitted user finds out: their sidebar re-asks and the room
        has grown a way in. There is no separate "you are in" message."""
        room = self.start_room("Quiet word", is_private=True)
        with self.capture_send_event_calls(expected_num_events=1) as events:
            self.client_post(f"/json/lounges/rooms/{room['id']}/admit", {"user_id": self.other.id})
        self.assertEqual(events[0]["event"]["type"], "lounge_rooms")
        self.assertIn(self.other.id, events[0]["users"])

    # -- joining ----------------------------------------------------------

    def test_joining_a_room_mints_a_token_for_that_room_alone(self) -> None:
        first = self.start_room("One")
        second = self.start_room("Two")

        with self.settings(**JWT_SETTINGS):
            result = self.client_post("/json/calls/jitsi/create", {"lounge_room_id": first["id"]})
            first_data = self.assert_json_success(result)
            result = self.client_post("/json/calls/jitsi/create", {"lounge_room_id": second["id"]})
            second_data = self.assert_json_success(result)

        # Two rooms in one lounge are two rooms, not one shared by accident.
        self.assertNotEqual(first_data["room"], second_data["room"])

    def test_joining_takes_exactly_one_kind_of_target(self) -> None:
        room = self.start_room()
        with self.settings(**JWT_SETTINGS):
            result = self.client_post(
                "/json/calls/jitsi/create",
                {"lounge_room_id": room["id"], "stream_id": self.lounge.id},
            )
        self.assert_json_error(
            result, "Specify exactly one of stream_id, user_ids or lounge_room_id"
        )

    def test_a_private_room_refuses_everyone_but_its_moderators(self) -> None:
        room = self.start_room("Quiet word", is_private=True)
        self.login_user(self.other)
        with self.settings(**JWT_SETTINGS):
            result = self.client_post("/json/calls/jitsi/create", {"lounge_room_id": room["id"]})
        self.assert_json_error(result, "This room is private.")

    def test_a_public_room_admits_anyone_in_the_lounge(self) -> None:
        room = self.start_room()
        self.login_user(self.other)
        with self.settings(**JWT_SETTINGS):
            result = self.client_post("/json/calls/jitsi/create", {"lounge_room_id": room["id"]})
        self.assert_json_success(result)

    def test_joining_a_room_requires_being_in_its_lounge(self) -> None:
        room = self.start_room()
        outsider = self.example_user("othello")
        self.unsubscribe(outsider, "Denmark")
        self.login_user(outsider)
        with self.settings(**JWT_SETTINGS):
            result = self.client_post("/json/calls/jitsi/create", {"lounge_room_id": room["id"]})
        self.assert_json_error(result, "Not subscribed to this lounge")

    def test_a_room_that_has_ended_says_so(self) -> None:
        room = self.start_room()
        LoungeRoom.objects.filter(id=room["id"]).delete()
        with self.settings(**JWT_SETTINGS):
            result = self.client_post("/json/calls/jitsi/create", {"lounge_room_id": room["id"]})
        self.assert_json_error(result, "This room has ended.")

    def test_whoever_started_the_room_moderates_it(self) -> None:
        """Not "whoever joined first", which is what Jitsi does left to itself and
        which hands the room to a passer-by."""
        room = self.start_room()
        with self.settings(**JWT_SETTINGS):
            mine = self.assert_json_success(
                self.client_post("/json/calls/jitsi/create", {"lounge_room_id": room["id"]})
            )
            self.login_user(self.other)
            theirs = self.assert_json_success(
                self.client_post("/json/calls/jitsi/create", {"lounge_room_id": room["id"]})
            )

        self.assertEqual(self.jwt_moderator(mine["url"]), "true")
        self.assertEqual(self.jwt_moderator(theirs["url"]), "false")

    def jwt_moderator(self, url: str) -> str:
        from urllib.parse import parse_qs, urlsplit

        import jwt

        token = parse_qs(urlsplit(url).query)["jwt"][0]
        claims = jwt.decode(token, options={"verify_signature": False})
        moderator = claims["context"]["user"]["moderator"]
        assert isinstance(moderator, str)
        return moderator

    # -- "no moderator present" -------------------------------------------

    def report_occupants(self, room_id: int, user_ids: list[int], *, active: bool = True) -> None:
        with self.settings(JITSI_CONFERENCING_SECRET=CONFERENCING_SECRET):
            self.occupancy_hook(
                realm_id=self.user.realm_id,
                stream_id=self.lounge.id,
                lounge_room_id=room_id,
                active=active,
                count=len(user_ids),
                occupants=[{"user_id": user_id, "name": "someone"} for user_id in user_ids],
            )

    def set_door_policy(self, policy: CallDoorPolicyEnum) -> None:
        self.lounge.call_door_policy = policy.value
        self.lounge.save(update_fields=["call_door_policy"])

    def require_moderator(self) -> None:
        self.set_door_policy(CallDoorPolicyEnum.moderator)

    def join_as_other(self) -> Any:
        self.login_user(self.other)
        with self.settings(**JWT_SETTINGS):
            return self.client_post("/json/calls/jitsi/create", {"lounge_room_id": self.room_id})

    def test_nobody_new_may_enter_once_the_moderators_have_gone(self) -> None:
        room = self.start_room()
        self.room_id = room["id"]
        self.require_moderator()

        # Hamlet started the room, so he moderates it. While he is in, others in.
        self.report_occupants(self.room_id, [self.user.id])
        self.assert_json_success(self.join_as_other())

        # He leaves; the call is still live, and the door shuts behind him.
        self.login_user(self.user)
        self.report_occupants(self.room_id, [self.other.id])
        self.assert_json_error(
            self.join_as_other(), "Nobody who moderates this call is in it right now."
        )

    def test_a_moderator_is_never_turned_away(self) -> None:
        """Or a room with the setting on could never have a first person in it."""
        room = self.start_room()
        self.room_id = room["id"]
        self.require_moderator()
        self.report_occupants(self.room_id, [self.other.id])

        with self.settings(**JWT_SETTINGS):
            result = self.client_post("/json/calls/jitsi/create", {"lounge_room_id": self.room_id})
        self.assert_json_success(result)

    def test_a_room_nobody_has_joined_admits_nobody_but_a_moderator(self) -> None:
        """The bug an earlier version had. Treating "no report" as "we do not
        know, so let them in" sounds cautious, but a room nobody has started has
        no report either -- so the first person in was always admitted and the
        setting did nothing at all."""
        room = self.start_room()
        self.room_id = room["id"]
        self.require_moderator()
        # No report has ever arrived for this room.
        self.assert_json_error(
            self.join_as_other(), "Nobody who moderates this call is in it right now."
        )

    def test_a_moderator_walking_in_reopens_the_door(self) -> None:
        """What makes failing closed safe: there is no state a non-moderator can
        get stuck in that a moderator cannot clear by doing the obvious thing."""
        room = self.start_room()
        self.room_id = room["id"]
        self.require_moderator()
        self.assert_json_error(
            self.join_as_other(), "Nobody who moderates this call is in it right now."
        )

        self.login_user(self.user)
        self.report_occupants(self.room_id, [self.user.id])
        self.assert_json_success(self.join_as_other())

    def test_the_setting_off_changes_nothing(self) -> None:
        room = self.start_room()
        self.room_id = room["id"]
        self.report_occupants(self.room_id, [self.other.id])
        self.assert_json_success(self.join_as_other())

    def test_the_shut_door_is_reported_before_the_click(self) -> None:
        """The refusal is enforced at the mint either way; this is so a client can
        stop offering a way in rather than leaving somebody to discover the rule
        by bouncing off it."""
        room = self.start_room()
        self.room_id = room["id"]
        self.require_moderator()

        self.login_user(self.other)
        listed = self.assert_json_success(self.client_get("/json/lounges/rooms"))["rooms"][0]
        self.assertTrue(listed["waiting_for_doorman"])
        # Not conflated with being locked out: the room is public, and saying
        # otherwise would put a padlock on it and hide that waiting would help.
        self.assertTrue(listed["can_join"])

        self.login_user(self.user)
        self.report_occupants(self.room_id, [self.user.id])
        self.login_user(self.other)
        listed = self.assert_json_success(self.client_get("/json/lounges/rooms"))["rooms"][0]
        self.assertFalse(listed["waiting_for_doorman"])

    def test_a_moderator_is_never_shown_as_waiting(self) -> None:
        """Their arrival is what opens the door, so they must always be offered
        the way in."""
        room = self.start_room()
        self.room_id = room["id"]
        self.require_moderator()
        listed = self.assert_json_success(self.client_get("/json/lounges/rooms"))["rooms"][0]
        self.assertFalse(listed["waiting_for_doorman"])

    def test_a_lounge_with_the_setting_off_never_waits(self) -> None:
        room = self.start_room()
        self.room_id = room["id"]
        self.login_user(self.other)
        listed = self.assert_json_success(self.client_get("/json/lounges/rooms"))["rooms"][0]
        self.assertFalse(listed["waiting_for_doorman"])

    def test_an_ended_call_leaves_no_note_behind(self) -> None:
        """A report saying the call is over drops the note rather than storing an
        empty one. Ids are never reused so nothing can inherit it, but a note that
        outlived the call it described would be a lie about a live one."""
        room = self.start_room()
        self.room_id = room["id"]
        self.report_occupants(self.room_id, [self.user.id])
        self.assertEqual(
            get_call_occupants(
                realm_id=self.user.realm_id,
                stream_id=self.lounge.id,
                lounge_room_id=self.room_id,
            ),
            [self.user.id],
        )

        self.report_occupants(self.room_id, [], active=False)
        self.assertIsNone(
            get_call_occupants(
                realm_id=self.user.realm_id,
                stream_id=self.lounge.id,
                lounge_room_id=self.room_id,
            )
        )

    def test_an_empty_but_live_call_is_not_a_moderated_one(self) -> None:
        """Active with nobody in it is a real state -- minted and not yet entered
        -- and it is emphatically not "a moderator is present"."""
        room = self.start_room()
        self.room_id = room["id"]
        self.require_moderator()
        self.report_occupants(self.room_id, [])
        self.assert_json_error(
            self.join_as_other(), "Nobody who moderates this call is in it right now."
        )

    # -- lifecycle --------------------------------------------------------

    def test_a_room_nobody_entered_is_reaped(self) -> None:
        """There is no "it emptied" report for a room Prosody never saw, so an
        abandoned start would otherwise advertise a conversation that is not
        happening."""
        room = self.start_room()
        LoungeRoom.objects.filter(id=room["id"]).update(
            date_created=timezone_now() - timedelta(seconds=LoungeRoom.UNJOINED_GRACE_SECONDS + 1)
        )

        result = self.client_get("/json/lounges/rooms")
        self.assertEqual(self.assert_json_success(result)["rooms"], [])
        self.assertFalse(LoungeRoom.objects.filter(id=room["id"]).exists())

    def test_a_room_in_use_is_not_reaped(self) -> None:
        room = self.start_room()
        LoungeRoom.objects.filter(id=room["id"]).update(
            date_created=timezone_now() - timedelta(seconds=LoungeRoom.UNJOINED_GRACE_SECONDS + 1),
            first_joined_at=timezone_now(),
        )

        result = self.client_get("/json/lounges/rooms")
        rooms = self.assert_json_success(result)["rooms"]
        self.assertEqual([r["id"] for r in rooms], [room["id"]])

    def test_a_room_the_service_has_forgotten_is_reaped(self) -> None:
        """The room ended, but the report saying so never arrived -- the service
        restarted and forgot it. Nothing else will ever say it is over, so the
        service's own account of what is live has to be able to settle it."""
        room = self.start_room()
        LoungeRoom.objects.filter(id=room["id"]).update(
            date_created=timezone_now() - timedelta(seconds=LoungeRoom.UNJOINED_GRACE_SECONDS + 1),
            first_joined_at=timezone_now(),
        )

        # Present in the feed: still live, left alone.
        reconcile_lounge_rooms({room["id"]})
        self.assertTrue(LoungeRoom.objects.filter(id=room["id"]).exists())

        # Absent from it: over, whatever the row believes.
        reconcile_lounge_rooms(set())
        self.assertFalse(LoungeRoom.objects.filter(id=room["id"]).exists())

    def test_a_just_started_room_survives_reconciliation(self) -> None:
        """Between minting a room and the service being told about it there is a
        moment where it is legitimately in one place and not the other. Reaping
        on that would make starting a room a race against the notice."""
        room = self.start_room()
        reconcile_lounge_rooms(set())
        self.assertTrue(LoungeRoom.objects.filter(id=room["id"]).exists())

    def test_an_unreachable_service_reaps_nothing(self) -> None:
        """The failure mode that matters: treating a failed fetch as an empty feed
        would delete every live room in the deployment at exactly the moment the
        service is least able to say otherwise. The view must not get that far."""
        room = self.start_room()
        LoungeRoom.objects.filter(id=room["id"]).update(
            date_created=timezone_now() - timedelta(seconds=LoungeRoom.UNJOINED_GRACE_SECONDS + 1),
            first_joined_at=timezone_now(),
        )
        # No conferencing service configured: the endpoint bails out before it
        # has any account of what is live.
        with self.settings(**JWT_SETTINGS):
            result = self.client_get("/json/calls/jitsi/occupancy_all")
        self.assert_json_success(result)
        self.assertTrue(LoungeRoom.objects.filter(id=room["id"]).exists())

    def occupancy_hook(self, **body: Any) -> Any:
        return self.client_post(
            "/api/internal/jitsi/occupancy",
            orjson.dumps(body).decode(),
            content_type="application/json",
            headers={"Authorization": f"Bearer {CONFERENCING_SECRET}"},
        )

    def test_occupancy_marks_a_room_as_entered(self) -> None:
        room = self.start_room()
        with self.settings(JITSI_CONFERENCING_SECRET=CONFERENCING_SECRET):
            result = self.occupancy_hook(
                realm_id=self.user.realm_id,
                stream_id=self.lounge.id,
                lounge_room_id=room["id"],
                active=True,
                count=1,
                occupants=[{"user_id": self.user.id, "name": "Hamlet"}],
            )
        self.assertEqual(result.status_code, 200)
        stored = LoungeRoom.objects.get(id=room["id"])
        self.assertIsNotNone(stored.first_joined_at)

    def test_a_started_but_unentered_room_survives_its_first_report(self) -> None:
        """The report that follows minting a room says active with nobody in it,
        because nobody has walked through the door yet. Reading that as "over"
        deletes the room seconds after somebody starts it -- which is exactly
        what it did, and it looked like the call vanishing from under them."""
        room = self.start_room()
        with self.settings(JITSI_CONFERENCING_SECRET=CONFERENCING_SECRET):
            result = self.occupancy_hook(
                realm_id=self.user.realm_id,
                stream_id=self.lounge.id,
                lounge_room_id=room["id"],
                active=True,
                count=0,
                occupants=[],
            )
        self.assertEqual(result.status_code, 200)
        self.assertTrue(LoungeRoom.objects.filter(id=room["id"]).exists())

    def test_a_momentarily_empty_room_survives(self) -> None:
        """The gap between the last person leaving and the next arriving is also
        active-with-nobody-in-it, and is not the room ending either."""
        room = self.start_room()
        with self.settings(JITSI_CONFERENCING_SECRET=CONFERENCING_SECRET):
            self.occupancy_hook(
                realm_id=self.user.realm_id,
                stream_id=self.lounge.id,
                lounge_room_id=room["id"],
                active=True,
                count=1,
                occupants=[{"user_id": self.user.id, "name": "Hamlet"}],
            )
            self.occupancy_hook(
                realm_id=self.user.realm_id,
                stream_id=self.lounge.id,
                lounge_room_id=room["id"],
                active=True,
                count=0,
                occupants=[],
            )
        self.assertTrue(LoungeRoom.objects.filter(id=room["id"]).exists())

    def test_a_room_is_deleted_when_it_empties(self) -> None:
        room = self.start_room()
        with self.settings(JITSI_CONFERENCING_SECRET=CONFERENCING_SECRET):
            self.occupancy_hook(
                realm_id=self.user.realm_id,
                stream_id=self.lounge.id,
                lounge_room_id=room["id"],
                active=True,
                count=1,
                occupants=[{"user_id": self.user.id, "name": "Hamlet"}],
            )
            self.occupancy_hook(
                realm_id=self.user.realm_id,
                stream_id=self.lounge.id,
                lounge_room_id=room["id"],
                active=False,
                count=0,
                occupants=[],
            )
        self.assertFalse(LoungeRoom.objects.filter(id=room["id"]).exists())

    def test_an_unauthorized_caller_cannot_end_a_room(self) -> None:
        room = self.start_room()
        with self.settings(JITSI_CONFERENCING_SECRET=CONFERENCING_SECRET):
            result = self.client_post(
                "/api/internal/jitsi/occupancy",
                orjson.dumps(
                    {
                        "realm_id": self.user.realm_id,
                        "stream_id": self.lounge.id,
                        "lounge_room_id": room["id"],
                        "active": False,
                        "count": 0,
                    }
                ).decode(),
                content_type="application/json",
                headers={"Authorization": "Bearer wrong"},
            )
        self.assertEqual(result.status_code, 404)
        self.assertTrue(LoungeRoom.objects.filter(id=room["id"]).exists())
