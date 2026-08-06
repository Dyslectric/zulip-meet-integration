import time
from datetime import datetime, timezone
from typing import Any
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import jwt
import orjson
import time_machine
from django.core.signing import Signer
from typing_extensions import override

from zerver.actions.user_groups import check_add_user_group
from zerver.lib.jitsi_token import (
    JitsiTokenError,
    derive_room_name,
    jitsi_jwt_is_configured,
    mint_jitsi_token,
)
from zerver.lib.streams import create_stream_if_needed
from zerver.lib.test_classes import ZulipTestCase
from zerver.models import UserProfile
from zerver.models.streams import get_stream
from zerver.views.video_calls import EPOCH_SIGNER_SALT

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
)


class JitsiTokenLibraryTest(ZulipTestCase):
    def test_room_names_are_stable_and_scoped(self) -> None:
        with self.settings(JITSI_ROOM_KEY="k"):
            first = derive_room_name("realm:1|channel:7", 0)
            self.assertEqual(first, derive_room_name("realm:1|channel:7", 0))
            self.assertNotEqual(first, derive_room_name("realm:1|channel:8", 0))
            self.assertNotEqual(first, derive_room_name("realm:2|channel:7", 0))
            # Rotating the epoch rotates the room; that is the "start a fresh
            # meeting" primitive and the recovery path if a link leaks.
            self.assertNotEqual(first, derive_room_name("realm:1|channel:7", 1))

    def test_room_names_do_not_leak_their_inputs(self) -> None:
        with self.settings(JITSI_ROOM_KEY="k"):
            room = derive_room_name("realm:1|channel:7", 0)
        self.assertTrue(room.startswith("c-"))
        self.assertEqual(len(room), 18)
        self.assertNotIn("7", room[2:4])

    def test_rotating_the_room_key_rekeys_everything(self) -> None:
        with self.settings(JITSI_ROOM_KEY="one"):
            before = derive_room_name("scope", 0)
        with self.settings(JITSI_ROOM_KEY="two"):
            after = derive_room_name("scope", 0)
        self.assertNotEqual(before, after)

    def test_refuses_a_wildcard_room(self) -> None:
        # A wildcard room claim is a skeleton key for the whole deployment and
        # the failure mode is silent. Nothing should ever mint one.
        with self.settings(**JWT_SETTINGS), self.assertRaises(JitsiTokenError):
            mint_jitsi_token(tenant="engineering", room="*", user_context={"id": "1", "name": "x"})

    def test_refuses_an_uppercase_tenant(self) -> None:
        with self.settings(**JWT_SETTINGS), self.assertRaises(JitsiTokenError):
            mint_jitsi_token(
                tenant="Engineering", room="c-abc", user_context={"id": "1", "name": "x"}
            )

    def test_refuses_a_non_string_user_field(self) -> None:
        # Zulip user IDs are integers, and a numeric value inside the user
        # context makes Prosody throw rather than degrade.
        with self.settings(**JWT_SETTINGS), self.assertRaises(JitsiTokenError):
            mint_jitsi_token(
                tenant="engineering",
                room="c-abc",
                user_context={"id": 1, "name": "x"},  # type: ignore[typeddict-item]
            )

    def test_not_configured(self) -> None:
        with self.settings(JITSI_JWT_APP_ID=None):
            self.assertFalse(jitsi_jwt_is_configured())


class JitsiCreateCallTest(ZulipTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.user = self.example_user("hamlet")
        self.login_user(self.user)

    def decode(self, url: str) -> dict[str, Any]:
        token = parse_qs(urlsplit(url).query)["jwt"][0]
        return jwt.decode(
            token,
            JWT_SETTINGS["JITSI_JWT_APP_SECRET"],
            algorithms=["HS256"],
            audience="jitsi",
            issuer="zulip",
        )

    def test_requires_configuration(self) -> None:
        with self.settings(JITSI_JWT_APP_ID=None):
            result = self.client_post(
                "/json/calls/jitsi/create", {"stream_id": self.get_stream_id("Denmark")}
            )
        self.assert_json_error(result, "Jitsi Meet (JWT) credentials have not been configured")

    def test_requires_exactly_one_target(self) -> None:
        with self.settings(**JWT_SETTINGS):
            result = self.client_post("/json/calls/jitsi/create", {})
            self.assert_json_error(result, "Specify exactly one of stream_id or user_ids")

            result = self.client_post(
                "/json/calls/jitsi/create",
                {
                    "stream_id": self.get_stream_id("Denmark"),
                    "user_ids": orjson.dumps([self.example_user("othello").id]).decode(),
                },
            )
            self.assert_json_error(result, "Specify exactly one of stream_id or user_ids")

    def test_channel_with_calls_disabled_is_refused(self) -> None:
        """A channel with voice/video off must not mint a token, even though the
        client would normally hide the affordance."""
        self.subscribe(self.user, "Denmark")
        stream_id = self.get_stream_id("Denmark")
        stream = get_stream("Denmark", self.user.realm)
        stream.voice_video_enabled = False
        stream.save(update_fields=["voice_video_enabled"])

        with self.settings(**JWT_SETTINGS):
            result = self.client_post("/json/calls/jitsi/create", {"stream_id": stream_id})
        self.assert_json_error(result, "Voice and video calls are disabled in this channel")

    def test_voice_video_enabled_can_be_toggled(self) -> None:
        stream_id = self.get_stream_id("Denmark")
        # Channels allow calls by default, so existing channels keep working.
        self.assertTrue(get_stream("Denmark", self.user.realm).voice_video_enabled)

        # Changing the setting requires permission to administer the channel.
        self.login_user(self.example_user("iago"))

        result = self.client_patch(
            f"/json/streams/{stream_id}", {"voice_video_enabled": orjson.dumps(False).decode()}
        )
        self.assert_json_success(result)
        self.assertFalse(get_stream("Denmark", self.user.realm).voice_video_enabled)

        result = self.client_patch(
            f"/json/streams/{stream_id}", {"voice_video_enabled": orjson.dumps(True).decode()}
        )
        self.assert_json_success(result)
        self.assertTrue(get_stream("Denmark", self.user.realm).voice_video_enabled)

    def test_a_web_public_channel_cannot_have_calls(self) -> None:
        """Anyone on the internet can read a web-public channel, so it must not be
        able to reach the state of having calls -- by either route."""
        stream_id = self.get_stream_id("Denmark")
        self.login_user(self.example_user("iago"))

        # Route one: enable calls on a channel that is already web-public.
        stream = get_stream("Denmark", self.example_user("hamlet").realm)
        stream.is_web_public = True
        stream.voice_video_enabled = False
        stream.save(update_fields=["is_web_public", "voice_video_enabled"])
        result = self.client_patch(
            f"/json/streams/{stream_id}", {"voice_video_enabled": orjson.dumps(True).decode()}
        )
        self.assert_json_error(
            result, "Web-public channels cannot have voice and video calls enabled."
        )

        # Asking for both at once is a contradiction, and is refused.
        stream.is_web_public = False
        stream.voice_video_enabled = False
        stream.save(update_fields=["is_web_public", "voice_video_enabled"])
        result = self.client_patch(
            f"/json/streams/{stream_id}",
            {
                "is_web_public": orjson.dumps(True).decode(),
                "voice_video_enabled": orjson.dumps(True).decode(),
            },
        )
        self.assert_json_error(
            result, "Web-public channels cannot have voice and video calls enabled."
        )

    def test_a_web_public_channel_never_mints_a_call(self) -> None:
        """Even carrying the stale combination -- web-public with calls still
        switched on, which an older client or a privacy change can leave behind --
        a web-public channel is refused a token. The rule is enforced where it
        matters rather than by silently rewriting the channel's other settings."""
        self.subscribe(self.user, "Denmark")
        stream_id = self.get_stream_id("Denmark")
        stream = get_stream("Denmark", self.user.realm)
        stream.is_web_public = True
        stream.voice_video_enabled = True
        stream.save(update_fields=["is_web_public", "voice_video_enabled"])

        with self.settings(**JWT_SETTINGS):
            result = self.client_post("/json/calls/jitsi/create", {"stream_id": stream_id})
        self.assert_json_error(result, "Voice and video calls are disabled in this channel")

    def test_a_new_web_public_channel_starts_without_calls(self) -> None:
        # Channels allow calls by default, so a web-public one has to be created
        # opted out rather than never opted in.
        realm = self.example_user("hamlet").realm
        with_calls, _ = create_stream_if_needed(realm, "ordinary")
        self.assertTrue(with_calls.voice_video_enabled)
        web_public, _ = create_stream_if_needed(realm, "open to all", is_web_public=True)
        self.assertFalse(web_public.voice_video_enabled)

    def test_subscribed_user_gets_a_scoped_token(self) -> None:
        self.subscribe(self.user, "Denmark")
        stream_id = self.get_stream_id("Denmark")
        with self.settings(**JWT_SETTINGS):
            result = self.client_post("/json/calls/jitsi/create", {"stream_id": stream_id})
        data = self.assert_json_success(result)

        claims = self.decode(data["url"])
        self.assertEqual(claims["room"], data["room"])
        self.assertEqual(claims["sub"], data["tenant"])
        # context.user.id is the Zulip user ID as a string, which is what makes
        # occupancy events from Prosody directly attributable later.
        self.assertEqual(claims["context"]["user"]["id"], str(self.user.id))
        # Two minutes, not thirty: the token's only job is to get through the door.
        self.assertEqual(claims["exp"] - claims["iat"], 120)
        self.assertLess(claims["nbf"], claims["iat"])
        self.assertTrue(data["url"].startswith("https://jitsi.example.com/"))
        self.assertIn(f"/{data['tenant']}/{data['room']}", data["url"])

    def test_unsubscribed_user_is_refused(self) -> None:
        """The check that none of Zulip's other call endpoints perform.

        Denmark is a public channel, so the user can read it without being
        subscribed. Subscription is what we treat as membership, because once
        Prosody trusts our signature the token is the only thing between a user
        and a conversation they are not part of.
        """
        stream_id = self.get_stream_id("Denmark")
        self.unsubscribe(self.user, "Denmark")
        with self.settings(**JWT_SETTINGS):
            result = self.client_post("/json/calls/jitsi/create", {"stream_id": stream_id})
        self.assert_json_error(result, "Not subscribed to this channel")

    def test_private_channel_a_user_cannot_see_is_refused(self) -> None:
        owner = self.example_user("iago")
        self.make_stream("secrets", invite_only=True)
        self.subscribe(owner, "secrets")
        stream_id = self.get_stream_id("secrets")
        with self.settings(**JWT_SETTINGS):
            result = self.client_post("/json/calls/jitsi/create", {"stream_id": stream_id})
        self.assert_json_error(result, "Invalid channel ID")

    def test_direct_message_room_is_symmetric(self) -> None:
        othello = self.example_user("othello")
        with self.settings(**JWT_SETTINGS):
            mine = self.assert_json_success(
                self.client_post(
                    "/json/calls/jitsi/create",
                    {"user_ids": orjson.dumps([othello.id]).decode()},
                )
            )
            self.login_user(othello)
            theirs = self.assert_json_success(
                self.client_post(
                    "/json/calls/jitsi/create",
                    {"user_ids": orjson.dumps([self.user.id]).decode()},
                )
            )
        # Both participants must derive the same room or they call into
        # different empty rooms.
        self.assertEqual(mine["room"], theirs["room"])

    def test_direct_message_to_a_nonexistent_user_is_refused(self) -> None:
        with self.settings(**JWT_SETTINGS):
            result = self.client_post(
                "/json/calls/jitsi/create", {"user_ids": orjson.dumps([99999]).decode()}
            )
        self.assert_json_error(result, "No such user")

    def test_rotating_the_epoch_changes_the_room(self) -> None:
        self.subscribe(self.user, "Denmark")
        stream_id = self.get_stream_id("Denmark")
        with self.settings(**JWT_SETTINGS):
            first = self.assert_json_success(
                self.client_post("/json/calls/jitsi/create", {"stream_id": stream_id})
            )
            rotated = self.assert_json_success(
                self.client_post(
                    "/json/calls/jitsi/create",
                    {
                        "stream_id": stream_id,
                        "epoch_token": first["epoch_token"],
                        "rotate": "true",
                    },
                )
            )
            # The rotated epoch round-trips, so a caller holding it keeps
            # deriving the new room rather than falling back to the old one.
            again = self.assert_json_success(
                self.client_post(
                    "/json/calls/jitsi/create",
                    {"stream_id": stream_id, "epoch_token": rotated["epoch_token"]},
                )
            )
        self.assertNotEqual(first["room"], rotated["room"])
        self.assertEqual(rotated["room"], again["room"])

    def test_a_forged_epoch_token_is_refused(self) -> None:
        self.subscribe(self.user, "Denmark")
        stream_id = self.get_stream_id("Denmark")
        with self.settings(**JWT_SETTINGS):
            result = self.client_post(
                "/json/calls/jitsi/create",
                {"stream_id": stream_id, "epoch_token": "not-a-signed-token"},
            )
            self.assert_json_error(result, "Invalid epoch token")

    def test_an_epoch_token_from_another_conversation_is_refused(self) -> None:
        """Binding the epoch to its scope stops it being replayed elsewhere."""
        self.subscribe(self.user, "Denmark")
        stream_id = self.get_stream_id("Denmark")
        foreign = Signer(salt=EPOCH_SIGNER_SALT).sign_object(
            {"scope": "realm:1|channel:999999", "epoch": 3}
        )
        with self.settings(**JWT_SETTINGS):
            result = self.client_post(
                "/json/calls/jitsi/create",
                {"stream_id": stream_id, "epoch_token": foreign},
            )
        self.assert_json_error(result, "Invalid epoch token")

    def test_tenant_comes_from_group_membership(self) -> None:
        self.subscribe(self.user, "Denmark")
        stream_id = self.get_stream_id("Denmark")
        # Created through the action rather than the ORM: NamedUserGroup's own
        # realm column is realm_for_sharding, so objects.create(realm=...) leaves
        # it null and the insert is rejected.
        check_add_user_group(
            self.user.realm, "conf-engineering", [self.user], acting_user=self.user
        )

        with self.settings(
            **{**JWT_SETTINGS, "JITSI_TENANT_BY_GROUP": {"conf-engineering": "Engineering"}}
        ):
            data = self.assert_json_success(
                self.client_post("/json/calls/jitsi/create", {"stream_id": stream_id})
            )
        # Lowercased: Prosody compares `sub` against the tenant path segment.
        self.assertEqual(data["tenant"], "engineering")
        self.assertEqual(self.decode(data["url"])["sub"], "engineering")

    def test_tenant_falls_back_to_the_realm_subdomain(self) -> None:
        self.subscribe(self.user, "Denmark")
        stream_id = self.get_stream_id("Denmark")
        with self.settings(**JWT_SETTINGS):
            data = self.assert_json_success(
                self.client_post("/json/calls/jitsi/create", {"stream_id": stream_id})
            )
        self.assertEqual(data["tenant"], self.user.realm.subdomain.lower())

    def test_moderator_flag_tracks_channel_administration(self) -> None:
        self.subscribe(self.user, "Denmark")
        stream_id = self.get_stream_id("Denmark")
        self.user.role = UserProfile.ROLE_MEMBER
        self.user.save(update_fields=["role"])
        with self.settings(**JWT_SETTINGS):
            data = self.assert_json_success(
                self.client_post("/json/calls/jitsi/create", {"stream_id": stream_id})
            )
        claims = self.decode(data["url"])
        # A string inside context.user, which is the shape token_affiliation
        # reads; there is no blessed top-level moderator claim in Jitsi.
        self.assertEqual(claims["context"]["user"]["moderator"], "false")
        self.assertNotIn("moderator", claims)

    def test_token_expires_and_is_rejected_afterwards(self) -> None:
        self.subscribe(self.user, "Denmark")
        stream_id = self.get_stream_id("Denmark")
        with self.settings(**JWT_SETTINGS):
            data = self.assert_json_success(
                self.client_post("/json/calls/jitsi/create", {"stream_id": stream_id})
            )
        token = parse_qs(urlsplit(data["url"]).query)["jwt"][0]
        # Travel past the two-minute lifetime rather than using a negative
        # leeway: leeway applies to iat as well, and PyJWT checks that first, so
        # a large negative value makes the token look issued in the future
        # (ImmatureSignatureError) instead of expired.
        with (
            time_machine.travel(
                datetime.fromtimestamp(time.time() + 300, tz=timezone.utc), tick=False
            ),
            self.assertRaises(jwt.ExpiredSignatureError),
        ):
            jwt.decode(
                token,
                JWT_SETTINGS["JITSI_JWT_APP_SECRET"],
                algorithms=["HS256"],
                audience="jitsi",
                issuer="zulip",
                options={"verify_exp": True},
            )


class JitsiOccupancyAllTest(ZulipTestCase):
    """The bulk sidebar feed must not leak a call from a conversation the
    requesting user is not part of."""

    def _dm_room(self, user_ids: list[int], realm_id: int) -> dict[str, object]:
        return {
            "user_ids": sorted(user_ids),
            "realm_id": realm_id,
            "active": True,
            "count": 1,
            "occupants": [],
            "drifted": False,
        }

    def _fetch(self, rooms: list[dict[str, object]]) -> dict[str, object]:
        with (
            self.settings(
                JITSI_CONFERENCING_URL="http://conferencing.example",
                JITSI_CONFERENCING_SECRET="s3cret",
            ),
            mock.patch("zerver.views.video_calls.requests.get") as fake_get,
        ):
            fake_get.return_value = mock.Mock(json=lambda: {"rooms": rooms})
            result = self.client_get("/json/calls/jitsi/occupancy_all")
        return self.assert_json_success(result)

    def test_only_direct_message_calls_the_user_is_in_are_returned(self) -> None:
        hamlet = self.example_user("hamlet")
        othello = self.example_user("othello")
        cordelia = self.example_user("cordelia")
        self.login_user(hamlet)

        mine = self._dm_room([hamlet.id, othello.id], hamlet.realm_id)
        theirs = self._dm_room([othello.id, cordelia.id], hamlet.realm_id)
        response = self._fetch([mine, theirs])

        rooms = response["rooms"]
        assert isinstance(rooms, list)
        self.assertEqual(
            [room["user_ids"] for room in rooms],
            [sorted([hamlet.id, othello.id])],
        )

    def test_a_direct_message_call_in_another_realm_is_dropped(self) -> None:
        hamlet = self.example_user("hamlet")
        othello = self.example_user("othello")
        self.login_user(hamlet)

        elsewhere = self._dm_room([hamlet.id, othello.id], hamlet.realm_id + 100)
        response = self._fetch([elsewhere])

        self.assertEqual(response["rooms"], [])
