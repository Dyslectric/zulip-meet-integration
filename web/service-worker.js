/* Zulip Web Push service worker.
 *
 * Push-only: it handles incoming push messages and notification clicks and
 * nothing else. It deliberately does NOT register a `fetch` handler or cache
 * anything, so it cannot interfere with Zulip's normal asset pipeline.
 *
 * The payload shape is produced by zerver.lib.push_notifications:
 *   add:    {type: "add", title, body, url, tag, message_id, icon?}
 *   remove: {type: "remove", message_ids: [...]}
 */
/* eslint-env serviceworker */

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
        // Revocation: the message(s) were read/deleted elsewhere, so close
        // any notifications we're still showing for them.
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
        icon: payload.icon ?? "/static/images/logo/zulip-icon-512x512.png",
        tag: payload.tag,
        data: {url: payload.url ?? "/", message_id: payload.message_id},
    };
    event.waitUntil(self.registration.showNotification(title, options));
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
