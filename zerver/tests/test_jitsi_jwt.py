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
from zerver.models import LoungeRoom, UserProfile
from zerver.models.streams import CallDoorPolicyEnum, StreamTopicsPolicyEnum, get_stream
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
        # Calls exist only on voice channels, and being one is opt-in, so the
        # channel these tests mint calls for has to be marked as one. Every
        # channel used to allow calls by default, which is why the tests below
        # never had to say so.
        stream = get_stream("Denmark", self.user.realm)
        stream.voice_video_enabled = True
        stream.save(update_fields=["voice_video_enabled"])

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
            self.assert_json_error(
                result, "Specify exactly one of stream_id, user_ids or lounge_room_id"
            )

            result = self.client_post(
                "/json/calls/jitsi/create",
                {
                    "stream_id": self.get_stream_id("Denmark"),
                    "user_ids": orjson.dumps([self.example_user("othello").id]).decode(),
                },
            )
            self.assert_json_error(
                result, "Specify exactly one of stream_id, user_ids or lounge_room_id"
            )

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
        # setUp marked this one a voice channel; confirm that took before we
        # start toggling it.
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

    def test_a_web_public_channel_may_be_a_voice_channel(self) -> None:
        """Reverses an earlier rule. A web-public voice channel is open to whoever
        can see it, unauthenticated visitors included; that is the point of it,
        and whoever administers the channel decides by making it web-public at
        all."""
        stream_id = self.get_stream_id("Denmark")
        self.login_user(self.example_user("iago"))

        stream = get_stream("Denmark", self.example_user("hamlet").realm)
        stream.is_web_public = True
        stream.voice_video_enabled = False
        stream.save(update_fields=["is_web_public", "voice_video_enabled"])
        result = self.client_patch(
            f"/json/streams/{stream_id}", {"voice_video_enabled": orjson.dumps(True).decode()}
        )
        self.assert_json_success(result)
        self.assertTrue(get_stream("Denmark", self.user.realm).voice_video_enabled)

    def test_a_web_public_voice_channel_mints_a_call(self) -> None:
        """The token endpoint no longer refuses a web-public channel. Reading it
        is enough for a logged-in user: they need not be subscribed, because a
        web-public channel is open to anyone who can see it."""
        stream_id = self.get_stream_id("Denmark")
        stream = get_stream("Denmark", self.user.realm)
        stream.is_web_public = True
        stream.voice_video_enabled = True
        stream.save(update_fields=["is_web_public", "voice_video_enabled"])

        with self.settings(**JWT_SETTINGS):
            result = self.client_post("/json/calls/jitsi/create", {"stream_id": stream_id})
        self.assert_json_success(result)

    def test_a_new_channel_is_not_a_voice_channel_unless_asked(self) -> None:
        # Being a voice channel is opt-in: an ordinary new channel is not one,
        # and asking makes it one -- including a web-public one.
        realm = self.example_user("hamlet").realm
        ordinary, _ = create_stream_if_needed(realm, "ordinary")
        self.assertFalse(ordinary.voice_video_enabled)

        opted_in, _ = create_stream_if_needed(realm, "with calls", voice_video_enabled=True)
        self.assertTrue(opted_in.voice_video_enabled)

        web_public, _ = create_stream_if_needed(
            realm, "open to all", is_web_public=True, voice_video_enabled=True
        )
        self.assertTrue(web_public.voice_video_enabled)

    def test_a_new_channel_is_not_a_lounge_unless_asked(self) -> None:
        # Being a lounge is opt-in on the same terms as being a voice channel,
        # web-public ones included.
        realm = self.example_user("hamlet").realm
        ordinary, _ = create_stream_if_needed(realm, "ordinary rooms")
        self.assertFalse(ordinary.is_lounge)

        opted_in, _ = create_stream_if_needed(realm, "with rooms", is_lounge=True)
        self.assertTrue(opted_in.is_lounge)

        web_public, _ = create_stream_if_needed(
            realm, "rooms open to all", is_web_public=True, is_lounge=True
        )
        self.assertTrue(web_public.is_lounge)

    def test_a_channel_cannot_be_a_lounge_and_a_voice_channel_at_once(self) -> None:
        """The two are the same answer to "you talk here" told differently -- one
        room that is always there against many that are not -- so a channel is at
        most one of them. Asking for both is refused; converting between them is
        not, and drops the kind being left behind."""
        realm = self.example_user("hamlet").realm

        # Neither route to the pair is open at creation.
        both, _ = create_stream_if_needed(
            realm, "both at once", voice_video_enabled=True, is_lounge=True
        )
        self.assertTrue(both.is_lounge)
        self.assertFalse(both.voice_video_enabled)

        stream_id = self.get_stream_id("Denmark")
        self.login_user(self.example_user("iago"))

        # setUp left Denmark a voice channel. Asking for both at once is a
        # contradiction and is refused.
        self.assertTrue(get_stream("Denmark", realm).voice_video_enabled)
        result = self.client_patch(
            f"/json/streams/{stream_id}",
            {
                "voice_video_enabled": orjson.dumps(True).decode(),
                "is_lounge": orjson.dumps(True).decode(),
            },
        )
        self.assert_json_error(result, "A channel cannot be both a voice channel and a lounge.")

        # Asking only to become a lounge is not a contradiction: it is a choice
        # to stop being a voice channel, and the row must not keep both flags.
        result = self.client_patch(
            f"/json/streams/{stream_id}", {"is_lounge": orjson.dumps(True).decode()}
        )
        self.assert_json_success(result)
        denmark = get_stream("Denmark", realm)
        self.assertTrue(denmark.is_lounge)
        self.assertFalse(denmark.voice_video_enabled)

    def test_a_web_public_channel_may_be_a_lounge(self) -> None:
        """The counterpart of the voice-channel reversal: a lounge may be
        web-public, and its rooms are then open to unauthenticated visitors."""
        stream_id = self.get_stream_id("Denmark")
        self.login_user(self.example_user("iago"))
        realm = self.example_user("hamlet").realm

        stream = get_stream("Denmark", realm)
        stream.is_web_public = True
        stream.voice_video_enabled = False
        stream.save(update_fields=["is_web_public", "voice_video_enabled"])
        result = self.client_patch(
            f"/json/streams/{stream_id}", {"is_lounge": orjson.dumps(True).decode()}
        )
        self.assert_json_success(result)
        denmark = get_stream("Denmark", realm)
        self.assertTrue(denmark.is_web_public)
        self.assertTrue(denmark.is_lounge)

        # Deliberately not also asserting the two-in-one-request case: turning a
        # channel web-public needs can_create_web_public_streams, which is
        # Zulip's own permission and nothing to do with this rule. Testing it
        # here would only assert who the fixture user happens to be.

    def test_a_lounge_has_no_topics(self) -> None:
        """A lounge keeps its conversations in rooms, so it carries no topics.
        Becoming one pins the channel single-threaded, and asking for a topics
        policy in the same breath is refused rather than quietly overridden."""
        stream_id = self.get_stream_id("Denmark")
        self.login_user(self.example_user("iago"))
        realm = self.example_user("hamlet").realm

        result = self.client_patch(
            f"/json/streams/{stream_id}",
            {
                "is_lounge": orjson.dumps(True).decode(),
                "topics_policy": "allow_empty_topic",
            },
        )
        self.assert_json_error(result, "Lounges have no topics: a conversation in one is a room.")

        result = self.client_patch(
            f"/json/streams/{stream_id}", {"is_lounge": orjson.dumps(True).decode()}
        )
        self.assert_json_success(result)
        self.assertEqual(
            get_stream("Denmark", realm).topics_policy,
            StreamTopicsPolicyEnum.empty_topic_only.value,
        )

    def test_a_lounge_does_not_mint_a_channel_call(self) -> None:
        """A lounge's calls are its rooms, which are minted per room rather than
        per channel. Until that exists, the channel-level endpoint refuses one:
        a lounge is not a voice channel, and only a voice channel has a call."""
        self.subscribe(self.user, "Denmark")
        stream_id = self.get_stream_id("Denmark")
        stream = get_stream("Denmark", self.user.realm)
        stream.voice_video_enabled = False
        stream.is_lounge = True
        stream.save(update_fields=["voice_video_enabled", "is_lounge"])

        with self.settings(**JWT_SETTINGS):
            result = self.client_post("/json/calls/jitsi/create", {"stream_id": stream_id})
        self.assert_json_error(result, "Voice and video calls are disabled in this channel")

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


class JitsiModeratorDoorPolicyTest(ZulipTestCase):
    """`call_door_policy` on an ordinary voice channel.

    A door policy, not a kill switch: it is checked when a token is minted and
    nowhere else, so a session already established is never reached into.
    """

    CONFERENCING_SECRET = "conferencing-secret-of-some-length"

    @override
    def setUp(self) -> None:
        super().setUp()
        self.admin = self.example_user("iago")
        self.member = self.example_user("hamlet")
        self.other = self.example_user("cordelia")
        self.stream = get_stream("Denmark", self.member.realm)
        self.stream.voice_video_enabled = True
        self.stream.call_door_policy = CallDoorPolicyEnum.moderator.value
        self.stream.save(update_fields=["voice_video_enabled", "call_door_policy"])
        self.subscribe(self.member, "Denmark")
        self.subscribe(self.other, "Denmark")

    def report_occupants(self, user_ids: list[int], *, active: bool = True) -> None:
        with self.settings(JITSI_CONFERENCING_SECRET=self.CONFERENCING_SECRET):
            self.client_post(
                "/api/internal/jitsi/occupancy",
                orjson.dumps(
                    {
                        "realm_id": self.member.realm_id,
                        "stream_id": self.stream.id,
                        "active": active,
                        "count": len(user_ids),
                        "occupants": [
                            {"user_id": user_id, "name": "someone"} for user_id in user_ids
                        ],
                    }
                ).decode(),
                content_type="application/json",
                headers={"Authorization": f"Bearer {self.CONFERENCING_SECRET}"},
            )

    def join_as(self, user: UserProfile) -> Any:
        self.login_user(user)
        with self.settings(**JWT_SETTINGS):
            return self.client_post("/json/calls/jitsi/create", {"stream_id": self.stream.id})

    def test_the_door_shuts_when_the_last_moderator_leaves(self) -> None:
        self.report_occupants([self.admin.id, self.member.id])
        self.assert_json_success(self.join_as(self.other))

        self.report_occupants([self.member.id])
        self.assert_json_error(
            self.join_as(self.other), "Nobody who moderates this call is in it right now."
        )

    def test_a_moderator_may_always_walk_in(self) -> None:
        """Otherwise a channel with this setting on could never have a first
        person in it, and the setting would be a lock rather than a policy."""
        self.report_occupants([self.member.id])
        self.assert_json_success(self.join_as(self.admin))

    def test_a_call_nobody_has_reported_on_admits_nobody(self) -> None:
        """A channel call nobody has joined has no report, which is not the same
        as a moderator being in it. Letting the first person in regardless is what
        made this setting a no-op."""
        self.assert_json_error(
            self.join_as(self.other), "Nobody who moderates this call is in it right now."
        )

    def closed_ids(self, user: UserProfile) -> Any:
        self.login_user(user)
        with self.settings(**JWT_SETTINGS):
            result = self.client_get("/json/calls/jitsi/occupancy_all")
        return self.assert_json_success(result)["closed_channel_ids"]

    def test_a_shut_door_is_named_in_the_feed(self) -> None:
        """So the sidebar can withhold the call button rather than let it fail on
        click. Reported even with no live call, because that is precisely when a
        non-moderator cannot start one either."""
        self.assertEqual(self.closed_ids(self.other), [self.stream.id])

        self.report_occupants([self.member.id])
        self.assertEqual(self.closed_ids(self.other), [self.stream.id])

        self.report_occupants([self.admin.id])
        self.assertEqual(self.closed_ids(self.other), [])

    def test_a_moderator_is_never_told_the_door_is_shut(self) -> None:
        self.assertEqual(self.closed_ids(self.admin), [])

    def test_a_channel_without_the_setting_is_never_listed(self) -> None:
        self.stream.call_door_policy = CallDoorPolicyEnum.anarchy.value
        self.stream.save(update_fields=["call_door_policy"])
        self.assertEqual(self.closed_ids(self.other), [])

    def test_the_setting_is_only_accepted_where_there_are_calls(self) -> None:
        """A door policy needs a door. An ordinary text channel has no calls for
        it to govern, so asking for it there is refused rather than stored and
        forgotten."""
        self.login_user(self.admin)
        text_channel = get_stream("Verona", self.admin.realm)
        text_channel.voice_video_enabled = False
        text_channel.is_lounge = False
        text_channel.save(update_fields=["voice_video_enabled", "is_lounge"])

        result = self.client_patch(
            f"/json/streams/{text_channel.id}",
            {"call_door_policy": "moderator"},
        )
        self.assert_json_error(
            result, "A call door policy can only be set on a voice channel or a lounge."
        )

        # Asking for anarchy is not asking for anything, so it is not refused.
        self.assert_json_success(
            self.client_patch(f"/json/streams/{text_channel.id}", {"call_door_policy": "anarchy"})
        )

    def test_an_unknown_policy_is_refused(self) -> None:
        self.login_user(self.admin)
        result = self.client_patch(
            f"/json/streams/{self.stream.id}", {"call_door_policy": "whatever"}
        )
        self.assert_json_error(result, "Invalid call_door_policy")

    def test_the_setting_is_dropped_with_the_calls_it_governed(self) -> None:
        """A stale rule waiting to reappear if the channel is ever made a voice
        channel again is a surprise nobody asked for."""
        self.login_user(self.admin)
        result = self.client_patch(
            f"/json/streams/{self.stream.id}",
            {"voice_video_enabled": orjson.dumps(False).decode()},
        )
        self.assert_json_success(result)
        self.assertEqual(
            get_stream("Denmark", self.admin.realm).call_door_policy,
            CallDoorPolicyEnum.anarchy.value,
        )

    # -- the authenticated-user doorman -----------------------------------

    def use_authenticated_doorman(self) -> None:
        self.stream.call_door_policy = CallDoorPolicyEnum.authenticated_user.value
        self.stream.is_web_public = True
        self.stream.save(update_fields=["call_door_policy", "is_web_public"])

    def guest_join(self) -> Any:
        self.logout()
        with self.settings(**JWT_SETTINGS, WEB_PUBLIC_STREAMS_ENABLED=True):
            return self.client_post(
                "/json/calls/jitsi/create_as_guest", {"stream_id": str(self.stream.id)}
            )

    def test_a_visitor_may_join_a_call_a_member_is_in(self) -> None:
        """The point of this policy: visitors may join a conversation members are
        having. An ordinary member, not a moderator, is enough to hold it open."""
        self.use_authenticated_doorman()
        self.report_occupants([self.member.id])
        self.assert_json_success(self.guest_join())

    def test_a_visitor_may_not_start_one_alone(self) -> None:
        """...and cannot hold a conversation among themselves. Nobody reported
        inside means no account holder inside."""
        self.use_authenticated_doorman()
        self.assert_json_error(
            self.guest_join(), "Nobody from this organization is in this call right now."
        )

    def test_visitors_do_not_hold_the_door_for_each_other(self) -> None:
        """A guest carries no user id, so the roster the check reads is empty of
        them however many there are. That is the whole point rather than an
        accident of the storage."""
        self.use_authenticated_doorman()
        self.report_occupants([])
        self.assert_json_error(
            self.guest_join(), "Nobody from this organization is in this call right now."
        )

    def test_the_door_shuts_when_the_last_member_leaves(self) -> None:
        self.use_authenticated_doorman()
        self.report_occupants([self.member.id])
        self.assert_json_success(self.guest_join())

        self.report_occupants([])
        self.assert_json_error(
            self.guest_join(), "Nobody from this organization is in this call right now."
        )

    def test_an_account_holder_is_never_refused(self) -> None:
        """They are the doorman. A policy that could shut them out would leave a
        channel no way to have a first person in it."""
        self.use_authenticated_doorman()
        self.assert_json_success(self.join_as(self.other))
        # ...including someone who moderates nothing here.
        self.assertEqual(self.closed_ids(self.other), [])

    def test_a_visitor_is_told_the_door_is_shut(self) -> None:
        self.use_authenticated_doorman()
        self.logout()
        with self.settings(**JWT_SETTINGS, WEB_PUBLIC_STREAMS_ENABLED=True):
            result = self.client_get("/json/calls/jitsi/occupancy_all")
        self.assertEqual(self.assert_json_success(result)["closed_channel_ids"], [self.stream.id])

    def test_anarchy_asks_for_nothing(self) -> None:
        self.stream.call_door_policy = CallDoorPolicyEnum.anarchy.value
        self.stream.is_web_public = True
        self.stream.save(update_fields=["call_door_policy", "is_web_public"])
        self.assert_json_success(self.guest_join())
        self.assertEqual(self.closed_ids(self.other), [])

    def test_a_guest_gets_the_same_door(self) -> None:
        """And gets it hardest: there is nothing an anonymous visitor could be
        that would exempt them."""
        self.stream.is_web_public = True
        self.stream.save(update_fields=["is_web_public"])
        self.report_occupants([self.member.id])

        self.logout()
        with self.settings(**JWT_SETTINGS, WEB_PUBLIC_STREAMS_ENABLED=True):
            result = self.client_post(
                "/json/calls/jitsi/create_as_guest", {"stream_id": str(self.stream.id)}
            )
        self.assert_json_error(result, "Nobody who moderates this call is in it right now.")


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

    def _channel_room(self, stream_id: int, **extra: object) -> dict[str, object]:
        return {
            "stream_id": stream_id,
            "active": True,
            "count": 1,
            "occupants": [],
            "drifted": False,
            **extra,
        }

    def test_a_visitor_is_shown_a_web_public_channels_call_and_nothing_else(self) -> None:
        """A visitor's entitlement is web-public and only that -- the same bar
        the guest token endpoint applies, so what they are shown is exactly what
        they could join."""
        hamlet = self.example_user("hamlet")
        realm = hamlet.realm

        open_channel = get_stream("Denmark", realm)
        open_channel.is_web_public = True
        open_channel.voice_video_enabled = True
        open_channel.save(update_fields=["is_web_public", "voice_video_enabled"])
        closed = get_stream("Verona", realm)

        self.logout()
        with (
            self.settings(
                JITSI_CONFERENCING_URL="http://conferencing.example",
                JITSI_CONFERENCING_SECRET="s3cret",
                WEB_PUBLIC_STREAMS_ENABLED=True,
            ),
            mock.patch("zerver.views.video_calls.requests.get") as fake_get,
        ):
            fake_get.return_value = mock.Mock(
                json=lambda: {
                    "rooms": [
                        self._channel_room(open_channel.id),
                        self._channel_room(closed.id),
                        self._dm_room([hamlet.id], realm.id),
                    ]
                }
            )
            result = self.client_get("/json/calls/jitsi/occupancy_all")

        rooms = self.assert_json_success(result)["rooms"]
        assert isinstance(rooms, list)
        self.assertEqual([room["stream_id"] for room in rooms], [open_channel.id])


class JitsiGuestCallTest(ZulipTestCase):
    """Tokens for a visitor with no Zulip account.

    Reachable without logging in and only ever for a web-public channel. The
    administrator's web-public toggle is the control: what it says is that
    anyone who can see this channel can be heard in it.
    """

    GUEST_SETTINGS: dict[str, Any] = {
        **JWT_SETTINGS,
        "JITSI_CONFERENCING_URL": None,
        "WEB_PUBLIC_STREAMS_ENABLED": True,
    }

    @override
    def setUp(self) -> None:
        super().setUp()
        self.user = self.example_user("hamlet")
        self.realm = self.user.realm
        self.stream = get_stream("Denmark", self.realm)
        self.stream.is_web_public = True
        self.stream.voice_video_enabled = True
        self.stream.save(update_fields=["is_web_public", "voice_video_enabled"])

    def decode(self, url: str) -> dict[str, Any]:
        token = parse_qs(urlsplit(url).query)["jwt"][0]
        return jwt.decode(
            token,
            JWT_SETTINGS["JITSI_JWT_APP_SECRET"],
            algorithms=["HS256"],
            audience="jitsi",
            issuer="zulip",
        )

    def guest_call(self, **params: str) -> Any:
        self.logout()
        with self.settings(**self.GUEST_SETTINGS):
            return self.client_post("/json/calls/jitsi/create_as_guest", params)

    def test_a_visitor_lands_in_the_same_room_as_a_member(self) -> None:
        """The whole point: the guest path is a different way in to the same
        conversation, not a parallel one nobody else is in."""
        self.login_user(self.user)
        with self.settings(**self.GUEST_SETTINGS):
            members = self.assert_json_success(
                self.client_post("/json/calls/jitsi/create", {"stream_id": self.stream.id})
            )
        guests = self.assert_json_success(self.guest_call(stream_id=str(self.stream.id)))
        self.assertEqual(guests["room"], members["room"])
        self.assertEqual(guests["tenant"], members["tenant"])

    def test_a_guest_identity_cannot_be_mistaken_for_a_zulip_user(self) -> None:
        """Load-bearing rather than cosmetic. Everything downstream reads
        `context.user.id` as a Zulip user ID when it parses as one, so a visitor
        able to present "7" would appear as user 7 -- name, avatar and all -- in
        every roster this deployment draws."""
        data = self.assert_json_success(self.guest_call(stream_id=str(self.stream.id)))
        guest_id = self.decode(data["url"])["context"]["user"]["id"]
        self.assertTrue(guest_id.startswith("guest-"))
        self.assertFalse(guest_id.isdigit())

    def test_a_guest_is_never_a_moderator(self) -> None:
        """A moderator claim says who runs a conversation, and it cannot be held
        by somebody the deployment cannot name."""
        data = self.assert_json_success(self.guest_call(stream_id=str(self.stream.id)))
        self.assertEqual(self.decode(data["url"])["context"]["user"]["moderator"], "false")

    def test_two_visitors_are_two_people(self) -> None:
        first = self.assert_json_success(self.guest_call(stream_id=str(self.stream.id)))
        second = self.assert_json_success(self.guest_call(stream_id=str(self.stream.id)))
        self.assertNotEqual(
            self.decode(first["url"])["context"]["user"]["id"],
            self.decode(second["url"])["context"]["user"]["id"],
        )

    def test_a_chosen_name_is_marked_as_a_guests(self) -> None:
        """Unverified, so it is shown as what it is. A visitor who types a
        colleague's name gets that name and the fact that they are a guest."""
        data = self.assert_json_success(
            self.guest_call(stream_id=str(self.stream.id), full_name="Hamlet")
        )
        self.assertEqual(self.decode(data["url"])["context"]["user"]["name"], "Hamlet (guest)")

    def test_an_unnamed_visitor_is_a_guest(self) -> None:
        data = self.assert_json_success(self.guest_call(stream_id=str(self.stream.id)))
        self.assertEqual(self.decode(data["url"])["context"]["user"]["name"], "Guest")

    def test_a_name_cannot_run_on_forever(self) -> None:
        data = self.assert_json_success(
            self.guest_call(stream_id=str(self.stream.id), full_name="x" * 500)
        )
        name = self.decode(data["url"])["context"]["user"]["name"]
        self.assertEqual(name, "x" * 40 + " (guest)")

    def test_a_channel_that_is_not_web_public_is_refused(self) -> None:
        self.stream.is_web_public = False
        self.stream.save(update_fields=["is_web_public"])
        result = self.guest_call(stream_id=str(self.stream.id))
        self.assert_json_error(result, "Invalid channel ID")

    def test_a_channel_with_calls_turned_off_is_refused(self) -> None:
        self.stream.voice_video_enabled = False
        self.stream.save(update_fields=["voice_video_enabled"])
        result = self.guest_call(stream_id=str(self.stream.id))
        self.assert_json_error(result, "Voice and video calls are disabled in this channel")

    def test_exactly_one_kind_of_target(self) -> None:
        result = self.guest_call()
        self.assert_json_error(result, "Specify exactly one of stream_id or lounge_room_id")

    # -- lounge rooms ------------------------------------------------------

    def make_lounge_room(self, *, is_private: bool = False) -> Any:
        self.stream.voice_video_enabled = False
        self.stream.is_lounge = True
        self.stream.save(update_fields=["voice_video_enabled", "is_lounge"])
        self.subscribe(self.user, "Denmark")
        self.login_user(self.user)
        result = self.client_post(
            f"/json/lounges/{self.stream.id}/rooms",
            {"name": "Open house", "is_private": orjson.dumps(is_private).decode()},
        )
        return self.assert_json_success(result)["room"]

    def test_a_visitor_may_join_a_room_in_a_web_public_lounge(self) -> None:
        room = self.make_lounge_room()
        data = self.assert_json_success(self.guest_call(lounge_room_id=str(room["id"])))
        self.assertEqual(self.decode(data["url"])["context"]["user"]["moderator"], "false")

    def test_a_private_room_refuses_a_visitor(self) -> None:
        """Locked hardest of all against somebody with no identity: there is no
        invited set an anonymous visitor could be in."""
        room = self.make_lounge_room(is_private=True)
        result = self.guest_call(lounge_room_id=str(room["id"]))
        self.assert_json_error(result, "This room is private.")

    def test_a_room_that_has_ended_says_so(self) -> None:
        room = self.make_lounge_room()
        LoungeRoom.objects.filter(id=room["id"]).delete()
        result = self.guest_call(lounge_room_id=str(room["id"]))
        self.assert_json_error(result, "This room has ended.")

    def test_a_room_in_a_lounge_that_is_not_web_public_is_refused(self) -> None:
        room = self.make_lounge_room()
        self.stream.is_web_public = False
        self.stream.save(update_fields=["is_web_public"])
        result = self.guest_call(lounge_room_id=str(room["id"]))
        self.assert_json_error(result, "Invalid channel ID")

    # -- what a visitor is told exists ------------------------------------

    def list_rooms(self) -> Any:
        self.logout()
        with self.settings(**self.GUEST_SETTINGS):
            return self.client_get("/json/lounges/rooms")

    def test_a_visitor_is_shown_the_rooms_they_could_join(self) -> None:
        """Without this the guest token endpoint has nothing to point at: a
        visitor cannot join a room they were never told exists."""
        room = self.make_lounge_room()
        listed = self.assert_json_success(self.list_rooms())["rooms"]
        self.assertEqual([r["id"] for r in listed], [room["id"]])
        self.assertTrue(listed[0]["can_join"])
        # Nothing to knock with and nothing to administer: being let in means
        # being added to a set of Zulip accounts, and they have none.
        self.assertFalse(listed[0]["can_knock"])
        self.assertFalse(listed[0]["can_administer"])
        self.assertNotIn("invited_user_ids", listed[0])

    def test_a_visitor_sees_a_private_room_but_cannot_join_it(self) -> None:
        """Visible but locked holds for a visitor too: the room is listed, and
        the lock is the point."""
        room = self.make_lounge_room(is_private=True)
        listed = self.assert_json_success(self.list_rooms())["rooms"]
        self.assertEqual([r["id"] for r in listed], [room["id"]])
        self.assertFalse(listed[0]["can_join"])

    def test_a_visitor_is_not_shown_a_lounge_that_is_not_web_public(self) -> None:
        self.make_lounge_room()
        self.stream.is_web_public = False
        self.stream.save(update_fields=["is_web_public"])
        self.assertEqual(self.assert_json_success(self.list_rooms())["rooms"], [])

    def test_a_visitor_sees_no_occupancy_when_the_service_is_absent(self) -> None:
        """The endpoint is reachable without logging in; with no conferencing
        service configured it reports nothing rather than erroring, exactly as
        it does for a member."""
        self.logout()
        with self.settings(**self.GUEST_SETTINGS):
            result = self.client_get("/json/calls/jitsi/occupancy_all")
        self.assertEqual(self.assert_json_success(result)["rooms"], [])
