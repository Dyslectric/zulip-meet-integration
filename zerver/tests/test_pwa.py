import base64
from unittest import mock

import orjson
from pywebpush import WebPushException

from zerver.actions.message_flags import do_clear_mobile_push_notifications_for_ids
from zerver.lib.push_notifications import (
    has_web_push_credentials,
    push_notifications_configured,
    send_web_push_notifications,
)
from zerver.lib.test_classes import ZulipTestCase
from zerver.models import UserMessage, UserProfile, WebPushSubscription

VAPID_TEST_SETTINGS = dict(
    WEB_PUSH_ENABLED=True,
    VAPID_PRIVATE_KEY=base64.b64encode(b"dummy-pem").decode(),
    VAPID_PUBLIC_KEY="test-public-key",
    VAPID_CONTACT_EMAIL="mailto:admin@example.com",
)


class WebManifestTest(ZulipTestCase):
    def test_manifest_webmanifest(self) -> None:
        # The manifest is public: it must be fetchable without login so the
        # browser can offer "Install" on the login page too.
        result = self.client_get("/manifest.webmanifest")
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result["Content-Type"].startswith("application/manifest+json"))

        manifest = orjson.loads(result.content)
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["start_url"], "/")
        self.assertEqual(manifest["scope"], "/")
        # Installability requires at least one sufficiently large icon.
        self.assertTrue(any(icon["sizes"] == "512x512" for icon in manifest["icons"]))

    def test_service_worker(self) -> None:
        result = self.client_get("/service-worker.js")
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result["Content-Type"].startswith("text/javascript"))
        # The worker is served from / so it must be allowed root scope.
        self.assertEqual(result["Service-Worker-Allowed"], "/")
        self.assertIn(b"addEventListener", result.content)


class WebPushGateTest(ZulipTestCase):
    def test_web_push_counts_as_configured(self) -> None:
        with self.settings(WEB_PUSH_ENABLED=True):
            self.assertTrue(has_web_push_credentials())
            # A VAPID keypair alone should make the server consider push
            # "configured", so handle_push_notification won't bail early.
            self.assertTrue(push_notifications_configured())
        with self.settings(WEB_PUSH_ENABLED=False):
            self.assertFalse(has_web_push_credentials())


class WebPushSubscriptionTest(ZulipTestCase):
    def test_web_push_config(self) -> None:
        self.login("hamlet")
        with self.settings(WEB_PUSH_ENABLED=True, VAPID_PUBLIC_KEY="test-public-key"):
            result = self.client_get("/json/users/me/web_push_subscription")
        response = self.assert_json_success(result)
        self.assertTrue(response["web_push_enabled"])
        self.assertEqual(response["vapid_public_key"], "test-public-key")

    def test_add_update_remove_subscription(self) -> None:
        hamlet = self.example_user("hamlet")
        self.login_user(hamlet)
        params = {
            "endpoint": "https://push.example.com/sub/abc",
            "p256dh": "p256dh-key",
            "auth": "auth-secret",
        }
        # Add
        self.assert_json_success(self.client_post("/json/users/me/web_push_subscription", params))
        sub = WebPushSubscription.objects.get(user_profile=hamlet, endpoint=params["endpoint"])
        self.assertEqual(sub.p256dh, "p256dh-key")

        # Re-subscribing with the same endpoint updates in place (no duplicate).
        params["p256dh"] = "p256dh-key-2"
        self.assert_json_success(self.client_post("/json/users/me/web_push_subscription", params))
        self.assertEqual(
            WebPushSubscription.objects.filter(
                user_profile=hamlet, endpoint=params["endpoint"]
            ).count(),
            1,
        )
        sub.refresh_from_db()
        self.assertEqual(sub.p256dh, "p256dh-key-2")

        # Remove
        self.assert_json_success(
            self.client_delete(
                "/json/users/me/web_push_subscription", {"endpoint": params["endpoint"]}
            )
        )
        self.assertFalse(
            WebPushSubscription.objects.filter(
                user_profile=hamlet, endpoint=params["endpoint"]
            ).exists()
        )


class WebPushSenderTest(ZulipTestCase):
    def _subscribe(
        self, user: UserProfile, endpoint: str = "https://push.example.com/sub/1"
    ) -> WebPushSubscription:
        return WebPushSubscription.objects.create(
            user_profile=user, endpoint=endpoint, p256dh="p256dh", auth="auth"
        )

    def test_send(self) -> None:
        hamlet = self.example_user("hamlet")
        self._subscribe(hamlet)
        payload = {"type": "add", "title": "T", "body": "B"}
        with self.settings(**VAPID_TEST_SETTINGS), mock.patch("pywebpush.webpush") as webpush_mock:
            send_web_push_notifications(hamlet, payload)
        webpush_mock.assert_called_once()
        kwargs = webpush_mock.call_args.kwargs
        self.assertEqual(
            kwargs["subscription_info"]["endpoint"], "https://push.example.com/sub/1"
        )
        self.assertEqual(orjson.loads(kwargs["data"]), payload)
        # A non-zero TTL so the push service holds messages for offline phones.
        self.assertGreater(kwargs["ttl"], 0)

    def test_disabled_is_noop(self) -> None:
        hamlet = self.example_user("hamlet")
        self._subscribe(hamlet)
        with self.settings(WEB_PUSH_ENABLED=False), mock.patch("pywebpush.webpush") as webpush_mock:
            send_web_push_notifications(hamlet, {"type": "add"})
        webpush_mock.assert_not_called()

    def test_stale_subscription_pruned(self) -> None:
        hamlet = self.example_user("hamlet")
        sub = self._subscribe(hamlet)
        response = mock.Mock(status_code=410)
        with (
            self.settings(**VAPID_TEST_SETTINGS),
            mock.patch(
                "pywebpush.webpush", side_effect=WebPushException("gone", response=response)
            ),
        ):
            send_web_push_notifications(hamlet, {"type": "add"})
        self.assertFalse(WebPushSubscription.objects.filter(id=sub.id).exists())


class WebPushRevocationTest(ZulipTestCase):
    def test_web_push_subscription_gets_remove_event(self) -> None:
        hamlet = self.example_user("hamlet")
        othello = self.example_user("othello")
        WebPushSubscription.objects.create(
            user_profile=hamlet, endpoint="https://push.example.com/h", p256dh="p", auth="a"
        )
        message_id = self.send_personal_message(othello, hamlet)
        user_message = UserMessage.objects.get(user_profile=hamlet, message_id=message_id)
        user_message.flags.active_mobile_push_notification = True
        user_message.save(update_fields=["flags"])

        with mock.patch(
            "zerver.actions.message_flags.queue_event_on_commit"
        ) as queue_mock:
            do_clear_mobile_push_notifications_for_ids([hamlet.id], [message_id])

        # A "remove" event should be enqueued for hamlet because their
        # WebPushSubscription now counts as a registered push device.
        queue_mock.assert_called_once()
        notice = queue_mock.call_args.args[1]
        self.assertEqual(notice["type"], "remove")
        self.assertEqual(notice["user_profile_id"], hamlet.id)
        self.assertEqual(notice["message_ids"], [message_id])
