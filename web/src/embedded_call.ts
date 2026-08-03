// Embedded, minimizable Jitsi call inside Zulip. Track B of
// docs/embedded-call-and-core-hook-design.md §2.
//
// Replaces the navbar call button's `window.open(url)` (wired in ui_init.js) with
// a JitsiMeetExternalAPI iframe hosted INSIDE Zulip, so the user keeps chatting
// and navigating during the call. Rules, each load-bearing:
//   1. The call container lives at the APP ROOT (document.body), never in a narrow
//      — Zulip is a SPA and an iframe in the narrow is unmounted on channel switch,
//      dropping the call. Created once, kept for the life of the page.
//   2. Minimize/maximize/resize are all CSS state or inline size on that one
//      persistent element — never a DOM removal (unmounting leaves the call).
//   3. Single active call (v1): starting another replaces the current one.
// The in-call roster in the bar comes free from the External API events.

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
// The inline width/height held while maximized, so restore-down returns to
// whatever size the user had dragged the panel to.
let saved_size: {width: string; height: string} | null = null;

// -- parsing -----------------------------------------------------------------

// The calls endpoint returns a full join URL
//   https://<meet>/<tenant>/<room>?jwt=<token>
// The External API wants the pieces separately, so the backend response is reused
// as-is rather than changed.
function parse_jitsi_url(raw_url: string): {domain: string; room_name: string; jwt: string | undefined} {
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
// created here so the rest of the module only toggles classes and inline size.
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
    return root;
}

function frame_node(): HTMLElement {
    return ensure_container().querySelector<HTMLElement>(".jec-frame")!;
}

// -- lifecycle ---------------------------------------------------------------

// Start (or switch to) an embedded call from a join URL. Exported; called where
// ui_init.js's .jitsi-call-button handler used to `window.open(url)`.
export async function start_embedded_call(raw_url: string): Promise<void> {
    const {domain, room_name, jwt} = parse_jitsi_url(raw_url);

    if (current !== null) {
        if (current.url === raw_url) {
            restore_call(); // same call: bring it back into view
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

    current = {api, url: raw_url, minimized: false, participants: 0};
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
    // The External API gives us the roster for free — no service round-trip.
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

export function minimize_call(): void {
    if (current === null) {
        return;
    }
    current.minimized = true;
    const root = ensure_container();
    root.classList.remove("maximized");
    root.classList.add("minimized");
}

export function restore_call(): void {
    if (current === null) {
        return;
    }
    current.minimized = false;
    ensure_container().classList.remove("minimized");
}

// Toggle filling the whole window. Maximizing clears any inline drag-resize size
// (so the .maximized CSS wins) and remembers it; restore-down puts it back.
export function toggle_maximize(): void {
    if (current === null) {
        return;
    }
    const root = ensure_container();
    if (root.classList.contains("maximized")) {
        root.classList.remove("maximized");
        root.style.width = saved_size?.width ?? "";
        root.style.height = saved_size?.height ?? "";
        saved_size = null;
    } else {
        saved_size = {width: root.style.width, height: root.style.height};
        root.style.width = "";
        root.style.height = "";
        current.minimized = false;
        root.classList.remove("minimized");
        root.classList.add("maximized");
    }
}

// Drag the top-left corner to resize. The panel is anchored bottom-right, so its
// size is just the distance from the pointer to that fixed corner.
function start_resize(event: MouseEvent): void {
    if (current === null) {
        return;
    }
    const root = ensure_container();
    if (root.classList.contains("minimized") || root.classList.contains("maximized")) {
        return; // only the normal docked panel is resizable
    }
    event.preventDefault();
    // Disable the iframe's pointer events during the drag, or it swallows the
    // mousemoves and the resize stalls the moment the pointer is over the video.
    root.classList.add("resizing");
    const on_move = (move: MouseEvent): void => {
        const width = window.innerWidth - EDGE_GAP - move.clientX;
        const height = window.innerHeight - EDGE_GAP - move.clientY;
        root.style.width = `${Math.max(MIN_WIDTH, Math.min(width, window.innerWidth - 2 * EDGE_GAP))}px`;
        root.style.height = `${Math.max(MIN_HEIGHT, Math.min(height, window.innerHeight - 2 * EDGE_GAP))}px`;
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
    update_count();
}

function hide_container(): void {
    const root = document.querySelector<HTMLElement>("#jitsi-embedded-call");
    if (root !== null) {
        root.classList.add("hidden");
        root.classList.remove("minimized", "maximized", "resizing");
        root.querySelector(".jec-frame")!.replaceChildren(); // drop the disposed iframe
    }
    saved_size = null;
}

function update_count(): void {
    const element = ensure_container().querySelector<HTMLElement>(".jec-count")!;
    const count = current?.participants ?? 0;
    element.textContent = count > 0 ? `· ${count} in call` : "";
}
