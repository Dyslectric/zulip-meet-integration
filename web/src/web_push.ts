import * as channel from "./channel.ts";

// Web Push subscription flow. Runs on load: if the user has already granted
// notification permission and the server has Web Push configured, register the
// service worker and make sure the browser is subscribed. The service worker
// itself (served from /service-worker.js) handles incoming push and clicks.

type WebPushConfig = {
    web_push_enabled: boolean;
    vapid_public_key: string;
};

function is_web_push_config(data: unknown): data is WebPushConfig {
    return (
        typeof data === "object" &&
        data !== null &&
        "web_push_enabled" in data &&
        typeof (data as {web_push_enabled: unknown}).web_push_enabled === "boolean" &&
        "vapid_public_key" in data &&
        typeof (data as {vapid_public_key: unknown}).vapid_public_key === "string"
    );
}

function url_base64_to_uint8_array(base64_string: string): Uint8Array<ArrayBuffer> {
    const padding = "=".repeat((4 - (base64_string.length % 4)) % 4);
    const base64 = (base64_string + padding).replaceAll("-", "+").replaceAll("_", "/");
    const raw = window.atob(base64);
    const output = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i += 1) {
        output[i] = raw.charCodeAt(i);
    }
    return output;
}

function get_config(): Promise<WebPushConfig | undefined> {
    return new Promise((resolve) => {
        void channel.get({
            url: "/json/users/me/web_push_subscription",
            success(data: unknown) {
                resolve(is_web_push_config(data) ? data : undefined);
            },
            error() {
                resolve(undefined);
            },
        });
    });
}

function save_subscription(subscription: PushSubscription): Promise<void> {
    const json = subscription.toJSON();
    return new Promise((resolve) => {
        void channel.post({
            url: "/json/users/me/web_push_subscription",
            data: {
                endpoint: subscription.endpoint,
                p256dh: json.keys?.["p256dh"] ?? "",
                auth: json.keys?.["auth"] ?? "",
            },
            success() {
                resolve();
            },
            error() {
                resolve();
            },
        });
    });
}

// Whether this browser can receive Web Push at all. Mobile browsers that
// implement the Push API can, so this is what should gate offering the
// notification permission prompt, rather than a mobile check.
export function is_supported(): boolean {
    return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

// Registers the service worker and makes sure this browser is subscribed.
// The caller must already have notification permission granted.
export async function subscribe(): Promise<void> {
    if (!is_supported()) {
        return;
    }

    const config = await get_config();
    if (config === undefined || !config.web_push_enabled || config.vapid_public_key === "") {
        return;
    }

    let registration;
    try {
        registration = await navigator.serviceWorker.register("/service-worker.js");
    } catch {
        // Registration can fail on insecure origins or if the browser blocks
        // it; there's nothing actionable to do here.
        return;
    }

    let subscription = await registration.pushManager.getSubscription();
    subscription ??= await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: url_base64_to_uint8_array(config.vapid_public_key),
    });

    await save_subscription(subscription);
}

// Runs on load: if permission is already granted, make sure we're subscribed.
// The actual permission request happens from a user gesture in
// settings_notifications.ts.
export async function initialize(): Promise<void> {
    if (typeof Notification === "undefined" || Notification.permission !== "granted") {
        return;
    }
    await subscribe();
}
