import * as z from "zod/mini";

import * as blueslip from "./blueslip.ts";
import * as channel from "./channel.ts";

// Web Push subscription flow. Runs on load: if the user has already granted
// notification permission and the server has Web Push configured, register the
// service worker and make sure the browser is subscribed. The service worker
// itself (served from /service-worker.js) handles incoming push and clicks.

const web_push_config_schema = z.object({
    web_push_enabled: z.boolean(),
    vapid_public_key: z.string(),
});

type WebPushConfig = z.infer<typeof web_push_config_schema>;

function url_base64_to_uint8_array(base64_string: string): Uint8Array<ArrayBuffer> {
    const padding = "=".repeat((4 - (base64_string.length % 4)) % 4);
    const base64 = (base64_string + padding).replaceAll("-", "+").replaceAll("_", "/");
    // atob yields one character per byte, so each code point is the byte value.
    return Uint8Array.from(window.atob(base64), (character) => character.codePointAt(0) ?? 0);
}

async function get_config(): Promise<WebPushConfig | undefined> {
    return new Promise((resolve) => {
        void channel.get({
            url: "/json/users/me/web_push_subscription",
            success(data) {
                const parsed = web_push_config_schema.safeParse(data);
                resolve(parsed.success ? parsed.data : undefined);
            },
            error() {
                resolve(undefined);
            },
        });
    });
}

async function save_subscription(subscription: PushSubscription): Promise<void> {
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
            error(xhr) {
                // The browser is subscribed but the server does not know, so
                // this device is silent and nothing else will say why.
                blueslip.warn("Could not save the Web Push subscription", {
                    status: xhr.status,
                });
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

let listening_for_subscription_changes = false;

// The service worker re-subscribes when the browser retires a subscription, but
// it cannot POST the new one itself (no CSRF token), so it asks us to.
function listen_for_subscription_changes(): void {
    if (listening_for_subscription_changes) {
        return;
    }
    listening_for_subscription_changes = true;
    navigator.serviceWorker.addEventListener("message", (event: MessageEvent<unknown>) => {
        if (
            typeof event.data === "object" &&
            event.data !== null &&
            "type" in event.data &&
            event.data.type === "web_push_subscription_changed"
        ) {
            void subscribe();
        }
    });
}

// Whether this browser already holds a push subscription. Worth knowing
// independently of the permission property, which is not reliable everywhere:
// if we are subscribed, there is nothing to ask the user for.
export async function has_active_subscription(): Promise<boolean> {
    if (!is_supported()) {
        return false;
    }
    try {
        const registration = await navigator.serviceWorker.getRegistration("/");
        if (registration === undefined) {
            return false;
        }
        return (await registration.pushManager.getSubscription()) !== null;
    } catch {
        return false;
    }
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
    } catch (error) {
        // Nothing here can recover -- an insecure origin or a browser that
        // refuses workers is not something the page can talk its way out of --
        // but say so anyway. Every path in this function ends in "notifications
        // simply never arrive", which is indistinguishable from a quiet server
        // unless the reason is written down somewhere.
        blueslip.warn("Could not register the Web Push service worker", {
            reason: String(error),
        });
        return;
    }

    listen_for_subscription_changes();

    let subscription = await registration.pushManager.getSubscription();
    if (subscription === null) {
        try {
            subscription = await registration.pushManager.subscribe({
                userVisibleOnly: true,
                applicationServerKey: url_base64_to_uint8_array(config.vapid_public_key),
            });
        } catch (error) {
            // Browsers refuse for reasons the permission property does not
            // predict, and iOS is the awkward one: permission can read
            // "granted" in a home-screen app that still will not subscribe.
            // Unreported, this leaves a device that looks configured and
            // receives nothing.
            blueslip.warn("Could not subscribe to Web Push", {reason: String(error)});
            return;
        }
    }

    await save_subscription(subscription);
}

// Runs on load: if permission is already granted, make sure we're subscribed.
// The actual permission request happens from a user gesture in
// settings_notifications.ts and navbar_alerts.ts.
export async function initialize(): Promise<void> {
    if (typeof Notification === "undefined" || Notification.permission !== "granted") {
        return;
    }
    await subscribe();
}
