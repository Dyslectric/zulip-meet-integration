from django.conf import settings
from django.contrib.staticfiles.storage import staticfiles_storage
from django.http import HttpRequest, HttpResponse, JsonResponse

from zerver.decorator import human_users_only
from zerver.lib.response import json_success
from zerver.lib.typed_endpoint import typed_endpoint, typed_endpoint_without_parameters
from zerver.models import UserProfile, WebPushSubscription


def manifest_webmanifest(request: HttpRequest) -> JsonResponse:
    """Serve the PWA web app manifest.

    This makes Zulip installable to a home screen / desktop (``display:
    standalone``) and gives the app a stable root ``scope`` under which a
    service worker can be registered for Web Push notifications.
    """
    manifest = {
        "name": "Zulip",
        "short_name": "Zulip",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": "#ffffff",
        "icons": [
            {
                "src": staticfiles_storage.url("images/logo/zulip-icon-512x512.png"),
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": staticfiles_storage.url("images/logo/zulip-icon-square.svg"),
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "maskable",
            },
        ],
    }
    return JsonResponse(manifest, content_type="application/manifest+json")


# The Web Push service worker, inlined so it is always present in the running
# deployment. Production ships compiled webpack bundles, not the raw web/ source
# tree, so this cannot be read from a file on disk. Push-only: no fetch handler
# and no caching, so it can't interfere with Zulip's asset pipeline.
SERVICE_WORKER_JS = """\
/* Zulip Web Push service worker. Payloads come from
 * zerver.lib.push_notifications:
 *   add:    {type: "add", title, body, url, tag, message_id, icon?}
 *   remove: {type: "remove", message_ids: [...]}
 */
self.addEventListener("push", (event) => {
    if (!event.data) {
        return;
    }

    let payload;
    try {
        payload = event.data.json();
    } catch {
        return;
    }

    if (payload.type === "remove") {
        const ids = new Set(payload.message_ids ?? []);
        event.waitUntil(
            self.registration.getNotifications().then((notifications) => {
                for (const notification of notifications) {
                    if (notification.data && ids.has(notification.data.message_id)) {
                        notification.close();
                    }
                }
            }),
        );
        return;
    }

    const title = payload.title ?? "Zulip";
    const options = {
        body: payload.body ?? "",
        icon: payload.icon,
        tag: payload.tag,
        data: {url: payload.url ?? "/", message_id: payload.message_id},
    };
    event.waitUntil(self.registration.showNotification(title, options));
});

/* The browser can retire our subscription on its own -- a worker update, key
 * rotation, storage pressure, or the push service expiring the endpoint. Left
 * alone, the stored endpoint is dead and this device silently stops receiving
 * notifications until the page is next loaded. Re-subscribe immediately, and ask
 * any open client to send the new subscription to the server: a worker has no
 * CSRF token, so it cannot do that itself. With no client open, the next page
 * load saves it, since the client always stores its current subscription.
 */
self.addEventListener("pushsubscriptionchange", (event) => {
    event.waitUntil(
        (async () => {
            let subscription = event.newSubscription;
            if (!subscription) {
                const key = event.oldSubscription?.options?.applicationServerKey;
                if (!key) {
                    return;
                }
                subscription = await self.registration.pushManager.subscribe({
                    userVisibleOnly: true,
                    applicationServerKey: key,
                });
            }
            const clients = await self.clients.matchAll({
                type: "window",
                includeUncontrolled: true,
            });
            for (const client of clients) {
                client.postMessage({type: "web_push_subscription_changed"});
            }
        })(),
    );
});

self.addEventListener("notificationclick", (event) => {
    event.notification.close();
    const url = (event.notification.data && event.notification.data.url) || "/";
    event.waitUntil(
        self.clients
            .matchAll({type: "window", includeUncontrolled: true})
            .then((clientList) => {
                for (const client of clientList) {
                    if ("focus" in client) {
                        void client.focus();
                        if ("navigate" in client) {
                            void client.navigate(url);
                        }
                        return undefined;
                    }
                }
                return self.clients.openWindow(url);
            }),
    );
});
"""


def service_worker(request: HttpRequest) -> HttpResponse:
    """Serve the Web Push service worker from the site root.

    A worker's control is limited to the path it's served from, so serving it
    at ``/service-worker.js`` (with ``Service-Worker-Allowed: /``) is what lets
    it receive push for the whole origin; a worker under ``/static/`` could
    only control ``/static/``.
    """
    response = HttpResponse(SERVICE_WORKER_JS, content_type="text/javascript")
    response["Service-Worker-Allowed"] = "/"
    return response


@human_users_only
@typed_endpoint_without_parameters
def web_push_config(request: HttpRequest, user_profile: UserProfile) -> HttpResponse:
    """Tell the client whether Web Push is available and hand it the VAPID
    public key it needs as the ``applicationServerKey`` for
    PushManager.subscribe()."""
    return json_success(
        request,
        data={
            "web_push_enabled": settings.WEB_PUSH_ENABLED,
            "vapid_public_key": settings.VAPID_PUBLIC_KEY or "",
        },
    )


@human_users_only
@typed_endpoint
def add_web_push_subscription(
    request: HttpRequest,
    user_profile: UserProfile,
    *,
    endpoint: str,
    p256dh: str,
    auth: str,
) -> HttpResponse:
    """Store (or refresh) a browser Web Push subscription for this user."""
    WebPushSubscription.objects.update_or_create(
        user_profile=user_profile,
        endpoint=endpoint,
        defaults={"p256dh": p256dh, "auth": auth},
    )
    return json_success(request)


@human_users_only
@typed_endpoint
def remove_web_push_subscription(
    request: HttpRequest,
    user_profile: UserProfile,
    *,
    endpoint: str,
) -> HttpResponse:
    """Drop a browser Web Push subscription (e.g. the user disabled it or the
    browser rotated it)."""
    WebPushSubscription.objects.filter(user_profile=user_profile, endpoint=endpoint).delete()
    return json_success(request)
