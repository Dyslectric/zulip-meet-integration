import {$} from "jquery";
import assert from "minimalistic-assert";

import {electron_bridge} from "./electron_bridge.ts";
import {localstorage} from "./localstorage.ts";
import type {Message} from "./message_store.ts";

type NoticeMemory = Map<
    string,
    {
        obj: Notification | ElectronBridgeNotification;
        msg_count: number;
        message_id: number;
    }
>;

export const notice_memory: NoticeMemory = new Map();

export let NotificationAPI: typeof ElectronBridgeNotification | typeof Notification | undefined;

// Used for testing
export function set_notification_api(n: typeof NotificationAPI): void {
    NotificationAPI = n;
}

export class ElectronBridgeNotification extends EventTarget {
    title: string;
    dir: NotificationDirection;
    lang: string;
    body: string;
    tag: string;
    icon: string;
    data: unknown;
    close: () => void;

    constructor(title: string, options: NotificationOptions) {
        super();
        assert(electron_bridge?.new_notification !== undefined);
        const notification_data = electron_bridge.new_notification(
            title,
            options,
            (type, eventInit) => this.dispatchEvent(new Event(type, eventInit)),
        );
        this.title = notification_data.title;
        this.dir = notification_data.dir;
        this.lang = notification_data.lang;
        this.body = notification_data.body;
        this.tag = notification_data.tag;
        this.icon = notification_data.icon;
        this.data = notification_data.data;
        this.close = notification_data.close;
    }

    static get permission(): NotificationPermission {
        return Notification.permission;
    }

    static async requestPermission(
        callback?: (permission: NotificationPermission) => void,
    ): Promise<NotificationPermission> {
        if (callback) {
            callback(await Promise.resolve(Notification.permission));
        }
        return Notification.permission;
    }
}

if (electron_bridge?.new_notification) {
    NotificationAPI = ElectronBridgeNotification;
} else if (window.Notification) {
    NotificationAPI = window.Notification;
}

export function get_notifications(): NoticeMemory {
    return notice_memory;
}

export function initialize(): void {
    $(window).on("focus", () => {
        for (const notice_mem_entry of notice_memory.values()) {
            notice_mem_entry.obj.close();
        }
        notice_memory.clear();
    });
}

export function permission_state(): NotificationPermission {
    if (NotificationAPI === undefined) {
        // act like notifications are blocked if they do not have access to
        // the notification API.
        return "denied";
    }
    return NotificationAPI.permission;
}

export function close_notification(message: Message): void {
    for (const [key, notice_mem_entry] of notice_memory) {
        if (notice_mem_entry.message_id === message.id) {
            notice_mem_entry.obj.close();
            notice_memory.delete(key);
        }
    }
}

export function granted_desktop_notifications_permission(): boolean {
    return NotificationAPI?.permission === "granted";
}

export async function request_desktop_notifications_permission(): Promise<NotificationPermission> {
    if (NotificationAPI) {
        return await NotificationAPI.requestPermission();
    }
    // Act like notifications are blocked if they do not have access to
    // the notification API.
    return "denied";
}

// Ask for permission and report the state we actually ended up in.
//
// Deliberately does not trust what requestPermission resolves with: it can
// reject (some platforms refuse the request outright), and implementations that
// only ever had the older callback form resolve with nothing at all. The
// permission property is the state everything else keys off, so read that.
export async function request_permission_and_get_state(): Promise<NotificationPermission> {
    try {
        await request_desktop_notifications_permission();
    } catch {
        // Nothing to do with the error itself; the state below is the answer.
    }
    return permission_state();
}

// How long to leave the "enable notifications" banner alone after the user
// dismissed it, or after asking did not result in a grant. Without this the
// banner returns on every load wherever a grant does not stick -- an installed
// iOS web app, whose permission and storage are separate from the browser's and
// can be reset, is the case this exists for.
const BANNER_SNOOZE_MS = 7 * 24 * 60 * 60 * 1000;
const BANNER_SNOOZED_AT = "notificationsBannerSnoozedAt";
// Backstop for a context where localStorage is unavailable or being cleared:
// at least do not re-offer within the same session.
let banner_snoozed_this_session = false;

export function snooze_notifications_banner(): void {
    banner_snoozed_this_session = true;
    if (localstorage.supported()) {
        localstorage().set(BANNER_SNOOZED_AT, Date.now());
    }
}

// Call back if notification permission becomes granted outside our own flow --
// from the browser's own UI, or in another tab -- so a stale banner does not sit
// there. Silently does nothing where the Permissions API cannot answer for
// notifications, which is common.
export function watch_permission_granted(on_granted: () => void): void {
    if (!("permissions" in navigator)) {
        return;
    }
    void (async () => {
        try {
            const status = await navigator.permissions.query({name: "notifications"});
            status.addEventListener("change", () => {
                if (status.state === "granted") {
                    on_granted();
                }
            });
        } catch {
            // This browser cannot report on the notifications permission.
        }
    })();
}

export function notifications_banner_is_snoozed(): boolean {
    if (banner_snoozed_this_session) {
        return true;
    }
    if (!localstorage.supported()) {
        return false;
    }
    const snoozed_at = localstorage().get(BANNER_SNOOZED_AT);
    if (typeof snoozed_at !== "number") {
        return false;
    }
    return Date.now() - snoozed_at < BANNER_SNOOZE_MS;
}
