// Embedded, minimizable Jitsi call inside Zulip. Track B of
// docs/embedded-call-and-core-hook-design.md §2.
//
// Replaces the navbar call button's `window.open(url)` (wired in ui_init.js) with
// a JitsiMeetExternalAPI iframe hosted INSIDE Zulip, so the user keeps chatting
// and navigating during the call. Rules, each load-bearing:
//   1. The call container lives at the APP ROOT (document.body), never in a narrow
//      — Zulip is a SPA and an iframe in the narrow is unmounted on channel switch,
//      dropping the call. Created once, kept for the life of the page.
//   2. Minimize/maximize/move/resize are all CSS state or inline position/size on
//      that one persistent element — never a DOM removal (unmounting leaves the call).
//   3. Single active call (v1): starting another replaces the current one.
// The in-call roster in the bar comes free from the External API events.

import * as z from "zod/mini";

import * as jitsi_sidebar from "./jitsi_sidebar.ts";

// Minimal shape of the External API we use. The real object has far more; we type
// only what we call so strict TS stays happy without a full ambient declaration.
type JitsiExternalApi = {
    executeCommand: (command: string) => void;
    addListener: (event: string, handler: () => void) => void;
    dispose: () => void;
};
type JitsiExternalApiConstructor = new (
    domain: string,
    options: Record<string, unknown>,
) => JitsiExternalApi;

declare global {
    // eslint-disable-next-line @typescript-eslint/consistent-type-definitions
    interface Window {
        JitsiMeetExternalAPI?: JitsiExternalApiConstructor;
    }
}

type ActiveCall = {
    api: JitsiExternalApi;
    url: string;
    minimized: boolean;
    participants: number;
    label: string | undefined;
    stream_id: number | undefined;
    // The meet origin host, so a speaking postMessage can be checked to have come
    // from this call's iframe before it drives the sidebar glow.
    domain: string;
};

// Prefer a same-origin, version-pinned copy of external_api.js (drops the
// cross-origin script-src from the CSP); fall back to the meet origin.
const EXTERNAL_API_SAME_ORIGIN = "/external_api.js";

// Panel size clamps (px) and the gap it keeps from the viewport edges.
const EDGE_GAP = 16;
const MIN_WIDTH = 320;
const MIN_HEIGHT = 200;

let external_api_promise: Promise<void> | null = null;
let current: ActiveCall | null = null;
// The panel's free (docked) geometry — inline left/top/right/bottom/width/height
// — stashed while the panel is maximized or minimized, so expanding/restoring
// returns it to wherever the user had dragged and sized it. Null while the panel
// is in its free state (or there is no call).
let saved_geometry: {
    left: string;
    top: string;
    right: string;
    bottom: string;
    width: string;
    height: string;
} | null = null;

// -- parsing -----------------------------------------------------------------

// The calls endpoint returns a full join URL
//   https://<meet>/<tenant>/<room>?jwt=<token>
// The External API wants the pieces separately, so the backend response is reused
// as-is rather than changed.
function parse_jitsi_url(raw_url: string): {
    domain: string;
    room_name: string;
    jwt: string | undefined;
} {
    const url = new URL(raw_url);
    return {
        domain: url.host,
        room_name: url.pathname.replace(/^\/+/, ""), // "<tenant>/<room>"
        jwt: url.searchParams.get("jwt") ?? undefined,
    };
}

// -- loading the External API ------------------------------------------------

// eslint-disable-next-line @typescript-eslint/promise-function-async -- wraps the script load/error callbacks in a Promise
function load_script(src: string): Promise<void> {
    return new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = src;
        script.async = true;
        script.addEventListener("load", () => {
            resolve();
        });
        script.addEventListener("error", () => {
            reject(new Error(`failed to load ${src}`));
        });
        document.head.append(script);
    });
}

async function load_external_api_uncached(domain: string): Promise<void> {
    try {
        await load_script(EXTERNAL_API_SAME_ORIGIN);
    } catch {
        await load_script(`https://${domain}/external_api.js`);
    }
    if (window.JitsiMeetExternalAPI === undefined) {
        throw new Error("external_api.js loaded but JitsiMeetExternalAPI is missing");
    }
}

async function load_external_api(domain: string): Promise<void> {
    if (window.JitsiMeetExternalAPI !== undefined) {
        return;
    }
    external_api_promise ??= load_external_api_uncached(domain);
    try {
        await external_api_promise;
    } catch (error) {
        external_api_promise = null; // failed load: allow a retry on the next call
        throw error;
    }
}

// -- the persistent container ------------------------------------------------

// Built once, lives at document.body for the life of the page. Everything is
// created here so the rest of the module only toggles classes and inline geometry.
function ensure_container(): HTMLElement {
    const existing = document.querySelector<HTMLElement>("#jitsi-embedded-call");
    if (existing !== null) {
        return existing;
    }
    const root = document.createElement("div");
    root.id = "jitsi-embedded-call";
    root.className = "jitsi-embedded-call hidden";
    root.innerHTML = `
        <div class="jec-resize" title="Drag to resize"></div>
        <div class="jec-bar">
            <span class="jec-status">In call</span>
            <span class="jec-label" title="conversation"></span>
            <span class="jec-count" title="people in the call"></span>
            <span class="jec-spacer"></span>
            <button type="button" class="jec-btn jec-mute" title="Mute / unmute">Mute</button>
            <button type="button" class="jec-btn jec-restore" title="Return to the call">Expand</button>
            <button type="button" class="jec-btn jec-maximize" title="Fill the window">Maximize</button>
            <button type="button" class="jec-btn jec-unmaximize" title="Back to a window">Restore</button>
            <button type="button" class="jec-btn jec-minimize" title="Keep the call, shrink it">Minimize</button>
            <button type="button" class="jec-btn jec-leave" title="Leave the call">Leave</button>
        </div>
        <div class="jec-frame"></div>
    `;
    document.body.append(root);

    root.querySelector(".jec-minimize")!.addEventListener("click", minimize_call);
    root.querySelector(".jec-restore")!.addEventListener("click", restore_call);
    root.querySelector(".jec-maximize")!.addEventListener("click", toggle_maximize);
    root.querySelector(".jec-unmaximize")!.addEventListener("click", toggle_maximize);
    root.querySelector(".jec-leave")!.addEventListener("click", leave_call);
    root.querySelector(".jec-mute")!.addEventListener("click", () => {
        current?.api.executeCommand("toggleAudio");
    });
    root.querySelector<HTMLElement>(".jec-resize")!.addEventListener("mousedown", start_resize);
    root.querySelector<HTMLElement>(".jec-bar")!.addEventListener("mousedown", start_move);
    // Keep the minimized bar aligned under the (fluid-width) compose box on resize.
    window.addEventListener("resize", () => {
        if (current?.minimized === true) {
            dock_minimized_bar(root);
        }
    });
    // Per-participant speaking arrives as a postMessage from the meet iframe.
    window.addEventListener("message", handle_speaking_message);
    return root;
}

function frame_node(): HTMLElement {
    return ensure_container().querySelector<HTMLElement>(".jec-frame")!;
}

// -- lifecycle ---------------------------------------------------------------

// Start (or switch to) an embedded call from a join URL. Exported; called where
// ui_init.js's .jitsi-call-button handler used to `window.open(url)`. `label` is
// the conversation the call belongs to (e.g. "#engineering"), shown in the bar.
export async function start_embedded_call(
    raw_url: string,
    options: {label?: string; stream_id?: number} = {},
): Promise<void> {
    const {domain, room_name, jwt} = parse_jitsi_url(raw_url);

    if (current !== null) {
        if (current.url === raw_url) {
            current.label = options.label ?? current.label;
            restore_call(); // same call: bring it back into view
            update_label();
            return;
        }
        // Single active call (v1): starting a call in another conversation
        // replaces the current one. A confirm-dialog prompt (Zulip's
        // confirm_dialog) is a future nicety; for now the old call is left.
        dispose_current();
    }

    await load_external_api(domain);
    const JitsiMeetExternalAPI = window.JitsiMeetExternalAPI!;
    const api = new JitsiMeetExternalAPI(domain, {
        roomName: room_name,
        jwt,
        parentNode: frame_node(),
        configOverwrite: {prejoinPageEnabled: false},
    });

    current = {
        api,
        url: raw_url,
        minimized: false,
        participants: 0,
        label: options.label,
        stream_id: options.stream_id,
        domain,
    };
    wire_api_events(api);
    show_container();
}

function wire_api_events(api: JitsiExternalApi): void {
    const bump = (delta: number): void => {
        if (current === null) {
            return;
        }
        current.participants = Math.max(0, current.participants + delta);
        update_count();
    };
    // The External API gives us the roster count for free — no service round-trip.
    // Who is *speaking* comes separately, via the iframe's postMessage relay
    // (handle_speaking_message): the External API only exposes the single dominant
    // speaker, not per-participant audio, so it cannot drive a per-user glow.
    api.addListener("videoConferenceJoined", () => {
        bump(1);
    });
    api.addListener("participantJoined", () => {
        bump(1);
    });
    api.addListener("participantLeft", () => {
        bump(-1);
    });
    api.addListener("videoConferenceLeft", leave_call);
    // Fired when Jitsi itself wants to close (kicked, ended, hangup button).
    api.addListener("readyToClose", leave_call);
}

// The self-hosted Jitsi web build postMessages who is currently speaking (a list of
// display names) out of the iframe, since the External API only reports the single
// dominant speaker. Accept it only from this call's meet origin, then light up the
// matching avatars in the call's channel. Registered once, in ensure_container.
const speaking_message_schema = z.object({
    source: z.literal("zulip-jitsi-speaking"),
    speaking: z.array(z.string()),
});

function handle_speaking_message(event: MessageEvent): void {
    if (current?.stream_id === undefined) {
        return;
    }
    if (event.origin !== `https://${current.domain}`) {
        return; // only this call's iframe may drive the glow
    }
    const parsed = speaking_message_schema.safeParse(event.data);
    if (!parsed.success) {
        return;
    }
    jitsi_sidebar.set_speaking(current.stream_id, parsed.data.speaking);
}

export function minimize_call(): void {
    if (current === null) {
        return;
    }
    current.minimized = true;
    const root = ensure_container();
    root.classList.remove("maximized");
    // Drop the free geometry, then dock the compact bar directly under the compose
    // box (matching its width). stash_geometry clears the inline position first so
    // dock_minimized_bar can set its own.
    stash_geometry(root);
    root.classList.add("minimized");
    dock_minimized_bar(root);
}

export function restore_call(): void {
    if (current === null) {
        return;
    }
    current.minimized = false;
    const root = ensure_container();
    root.classList.remove("minimized");
    undock_minimized_bar();
    restore_geometry(root);
}

// Toggle filling the whole window. Maximizing stashes the free geometry (so the
// .maximized CSS wins) and remembers it; restore-down puts it back.
export function toggle_maximize(): void {
    if (current === null) {
        return;
    }
    const root = ensure_container();
    if (root.classList.contains("maximized")) {
        root.classList.remove("maximized");
        restore_geometry(root);
    } else {
        current.minimized = false;
        root.classList.remove("minimized");
        undock_minimized_bar();
        stash_geometry(root);
        root.classList.add("maximized");
    }
}

// -- moving and resizing -----------------------------------------------------

// The panel ships anchored to the bottom-right corner via CSS. The moment the
// user drags or resizes it, switch to explicit top-left inline positioning,
// materialising the current on-screen rect so nothing jumps. Idempotent.
function anchor_top_left(root: HTMLElement): void {
    if (root.style.left !== "") {
        return; // already top-left anchored
    }
    const rect = root.getBoundingClientRect();
    root.style.left = `${rect.left}px`;
    root.style.top = `${rect.top}px`;
    root.style.width = `${rect.width}px`;
    root.style.height = `${rect.height}px`;
    root.style.right = "auto";
    root.style.bottom = "auto";
}

// Stash the free geometry and clear it, so a state with its own CSS position
// (.minimized docked bar, .maximized full screen) is not overridden by inline
// styles. Only the first stash wins, so maximize-then-minimize keeps the original
// docked geometry to come back to.
function stash_geometry(root: HTMLElement): void {
    // Save the free geometry the first time only, so maximize-then-minimize (or
    // the reverse) still comes back to the panel the user had. Always clear the
    // inline geometry, so a state with its own CSS/JS position takes over cleanly.
    saved_geometry ??= {
        left: root.style.left,
        top: root.style.top,
        right: root.style.right,
        bottom: root.style.bottom,
        width: root.style.width,
        height: root.style.height,
    };
    root.style.left = "";
    root.style.top = "";
    root.style.right = "";
    root.style.bottom = "";
    root.style.width = "";
    root.style.height = "";
}

function restore_geometry(root: HTMLElement): void {
    if (saved_geometry === null) {
        return;
    }
    root.style.left = saved_geometry.left;
    root.style.top = saved_geometry.top;
    root.style.right = saved_geometry.right;
    root.style.bottom = saved_geometry.bottom;
    root.style.width = saved_geometry.width;
    root.style.height = saved_geometry.height;
    saved_geometry = null;
}

// Dock the minimized bar directly under the compose box, matching its width, and
// lift the compose box by the bar's height so the two never overlap. The message
// feed keeps a fixed 40%-of-viewport bottom whitespace (resize.ts), so shifting
// compose up within it needs no feed resize. Re-run on window resize.
function dock_minimized_bar(root: HTMLElement): void {
    // Match the visible compose box: #compose-content carries the box's border and
    // background (compose.css) and is the narrower, message-column-aligned element,
    // present whether compose is open or closed. #compose-container is a full-width
    // wrapper, so it is only a last-resort fallback.
    const compose =
        document.querySelector<HTMLElement>("#compose-content") ??
        document.querySelector<HTMLElement>("#compose-container") ??
        document.querySelector<HTMLElement>("#compose");
    if (compose === null) {
        return; // no compose here (e.g. a non-conversation view): keep the CSS fallback
    }
    const rect = compose.getBoundingClientRect();
    if (rect.width === 0) {
        return; // compose not laid out yet: keep the CSS fallback
    }
    root.style.left = `${rect.left}px`;
    root.style.width = `${rect.width}px`;
    root.style.right = "auto";
    root.style.top = "auto";
    root.style.bottom = "0";
    root.style.height = "";
    document.body.classList.add("jec-dock-minimized");
    document.body.style.setProperty("--jec-dock-gap", `${root.offsetHeight + 4}px`);
}

function undock_minimized_bar(): void {
    document.body.classList.remove("jec-dock-minimized");
    document.body.style.removeProperty("--jec-dock-gap");
}

// Drag the top-left corner to resize, keeping the panel's current bottom-right
// corner fixed. Works wherever the panel has been moved to.
function start_resize(event: MouseEvent): void {
    if (current === null) {
        return;
    }
    const root = ensure_container();
    if (root.classList.contains("minimized") || root.classList.contains("maximized")) {
        return; // only the normal docked panel is resizable
    }
    event.preventDefault();
    const rect = root.getBoundingClientRect();
    const right_edge = rect.right;
    const bottom_edge = rect.bottom;
    anchor_top_left(root);
    // Disable the iframe's pointer events during the drag, or it swallows the
    // mousemoves and the resize stalls the moment the pointer is over the video.
    root.classList.add("resizing");
    const on_move = (move: MouseEvent): void => {
        const left = Math.max(EDGE_GAP, Math.min(move.clientX, right_edge - MIN_WIDTH));
        const top = Math.max(EDGE_GAP, Math.min(move.clientY, bottom_edge - MIN_HEIGHT));
        root.style.left = `${left}px`;
        root.style.top = `${top}px`;
        root.style.width = `${right_edge - left}px`;
        root.style.height = `${bottom_edge - top}px`;
    };
    const on_up = (): void => {
        root.classList.remove("resizing");
        document.removeEventListener("mousemove", on_move);
        document.removeEventListener("mouseup", on_up);
    };
    document.addEventListener("mousemove", on_move);
    document.addEventListener("mouseup", on_up);
}

// Drag the title bar to move the whole panel, clamped to the viewport. A grab that
// lands on a button falls through to that button's own action.
function start_move(event: MouseEvent): void {
    if (current === null) {
        return;
    }
    const root = ensure_container();
    if (root.classList.contains("minimized") || root.classList.contains("maximized")) {
        return; // only the normal docked panel is movable
    }
    const target = event.target;
    if (target instanceof HTMLElement && target.closest(".jec-btn") !== null) {
        return; // let button clicks through
    }
    event.preventDefault();
    anchor_top_left(root);
    const rect = root.getBoundingClientRect();
    const grab_x = event.clientX - rect.left;
    const grab_y = event.clientY - rect.top;
    // Reuse .resizing to turn off the iframe's pointer events during the drag.
    root.classList.add("resizing");
    const on_move = (move: MouseEvent): void => {
        const max_left = Math.max(EDGE_GAP, window.innerWidth - rect.width - EDGE_GAP);
        const max_top = Math.max(EDGE_GAP, window.innerHeight - rect.height - EDGE_GAP);
        const left = Math.max(EDGE_GAP, Math.min(move.clientX - grab_x, max_left));
        const top = Math.max(EDGE_GAP, Math.min(move.clientY - grab_y, max_top));
        root.style.left = `${left}px`;
        root.style.top = `${top}px`;
    };
    const on_up = (): void => {
        root.classList.remove("resizing");
        document.removeEventListener("mousemove", on_move);
        document.removeEventListener("mouseup", on_up);
    };
    document.addEventListener("mousemove", on_move);
    document.addEventListener("mouseup", on_up);
}

export function leave_call(): void {
    dispose_current();
    hide_container();
}

export function is_call_active(): boolean {
    return current !== null;
}

function dispose_current(): void {
    if (current !== null) {
        if (current.stream_id !== undefined) {
            jitsi_sidebar.set_speaking(current.stream_id, []); // clear any glow
        }
        try {
            current.api.dispose();
        } catch (error) {
            // Disposing a half-dead call must not wedge the UI.
            // eslint-disable-next-line no-console
            console.warn("embedded call: dispose failed", error);
        }
    }
    current = null;
}

// -- small view helpers ------------------------------------------------------

function show_container(): void {
    const root = ensure_container();
    root.classList.remove("hidden", "minimized", "maximized");
    undock_minimized_bar();
    update_count();
    update_label();
}

function hide_container(): void {
    const root = document.querySelector<HTMLElement>("#jitsi-embedded-call");
    if (root !== null) {
        root.classList.add("hidden");
        root.classList.remove("minimized", "maximized", "resizing");
        root.querySelector(".jec-frame")!.replaceChildren(); // drop the disposed iframe
        // Reset to the default docked anchor so the next call starts fresh.
        root.style.left = "";
        root.style.top = "";
        root.style.right = "";
        root.style.bottom = "";
        root.style.width = "";
        root.style.height = "";
    }
    undock_minimized_bar();
    saved_geometry = null;
}

function update_count(): void {
    const element = ensure_container().querySelector<HTMLElement>(".jec-count")!;
    const count = current?.participants ?? 0;
    element.textContent = count > 0 ? `· ${count} in call` : "";
}

function update_label(): void {
    const element = ensure_container().querySelector<HTMLElement>(".jec-label")!;
    element.textContent = current?.label ?? "";
}
