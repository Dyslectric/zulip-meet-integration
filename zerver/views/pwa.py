import os

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


def service_worker(request: HttpRequest) -> HttpResponse:
    """Serve the Web Push service worker from the site root.

    A worker's control is limited to the path it's served from, so serving it
    at ``/service-worker.js`` (with ``Service-Worker-Allowed: /``) is what lets
    it receive push for the whole origin; a worker under ``/static/`` could
    only control ``/static/``.
    """
    path = os.path.join(settings.DEPLOY_ROOT, "web", "service-worker.js")
    with open(path, "rb") as f:
        content = f.read()
    response = HttpResponse(content, content_type="text/javascript")
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
