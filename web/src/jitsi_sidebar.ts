// Call-aware left sidebar. Polls the bulk occupancy feed
// (GET /json/calls/jitsi/occupancy_all) and augments each channel row that has a
// live call: swaps its privacy glyph for a speaker (with a lock overlay for
// private channels) and shows the participants' avatars beneath the row.
//
// The augmentation is re-applied on every poll, so it self-heals after Zulip
// re-renders the stream list (which would otherwise wipe our injected nodes).
//
// The speaking ring is driven separately, through set_speaking(), by the
// embedded call — occupancy only carries join/leave, never who is talking, so a
// glow is possible only for the one call you are actually in (its Jitsi
// dominant-speaker events). Every other channel shows avatars without a glow.

import {$} from "jquery";
import * as z from "zod/mini";

import * as channel from "./channel.ts";
import * as people from "./people.ts";
import * as sub_store from "./sub_store.ts";

// A room in the bulk feed is a channel call (stream_id) or a DM/group call
// (user_ids); exactly one of the two identifies it.
const occupancy_all_schema = z.object({
    rooms: z.array(
        z.object({
            stream_id: z.optional(z.number()),
            user_ids: z.optional(z.array(z.number())),
            active: z.boolean(),
            count: z.number(),
            occupants: z.array(
                z.object({
                    name: z.string(),
                    user_id: z.nullable(z.number()),
                }),
            ),
            drifted: z.boolean(),
        }),
    ),
});

type SidebarOccupancy = {
    stream_id: number;
    active: boolean;
    count: number;
    occupants: {name: string; user_id: number | null}[];
    drifted: boolean;
};

// A pushed jitsi_occupancy server event (one channel's live roster).
const pushed_occupancy_schema = z.object({
    stream_id: z.number(),
    active: z.boolean(),
    count: z.number(),
    occupants: z.array(
        z.object({
            name: z.string(),
            user_id: z.nullable(z.number()),
        }),
    ),
});

// The push (a jitsi_occupancy server event → apply_pushed_occupancy) is the fast
// path; this poll is only a slow safety net that heals any missed event.
const POLL_MS = 15000;
const SVG_NS = "http://www.w3.org/2000/svg";

let poll_interval_id: number | undefined;
// Latest occupancy for each channel with a live call.
const occupancy_by_stream = new Map<number, SidebarOccupancy>();
// Participant-set keys (see dm_key) of the DM/group conversations with a live
// call. Only presence matters for a DM row: it shows a speaker, not a roster.
const dm_calls = new Set<string>();
// The display names currently speaking in the one call we are in, keyed by the
// channel it belongs to. Names (not user ids) because that is what the Jitsi web
// relay reports and what the occupant avatars carry. A set, not one name — the
// per-participant relay can mark several people speaking at once. Empty for every
// channel we are only observing (there is no speaker data for those).
const speaking_by_stream = new Map<number, Set<string>>();

export function initialize(): void {
    if (poll_interval_id !== undefined) {
        return;
    }
    poll();
    poll_interval_id = window.setInterval(poll, POLL_MS);
}

function poll(): void {
    void channel.get({
        url: "/json/calls/jitsi/occupancy_all",
        success(raw: unknown): void {
            ingest(raw);
            apply();
        },
    });
}

function ingest(raw: unknown): void {
    occupancy_by_stream.clear();
    dm_calls.clear();
    const parsed = occupancy_all_schema.safeParse(raw);
    if (!parsed.success) {
        return;
    }
    for (const room of parsed.data.rooms) {
        if (!room.active) {
            continue;
        }
        if (room.stream_id !== undefined) {
            occupancy_by_stream.set(room.stream_id, {...room, stream_id: room.stream_id});
        } else if (room.user_ids !== undefined) {
            dm_calls.add(dm_key(room.user_ids));
        }
    }
}

// DM conversations are identified by their full participant set, so a call's
// key is every participant sorted. A row may or may not list you among its
// participants, so callers add you and the Set below collapses the duplicate.
function dm_key(user_ids: number[]): string {
    return [...new Set(user_ids)].toSorted((a, b) => a - b).join(",");
}

// A pushed jitsi_occupancy client event: an instant update for one channel, so the
// sidebar reflects a join/leave without waiting for the poll. `active: false` means
// the call ended → drop the row.
export function apply_pushed_occupancy(event: unknown): void {
    const parsed = pushed_occupancy_schema.safeParse(event);
    if (!parsed.success) {
        return;
    }
    const data = parsed.data;
    if (data.active) {
        occupancy_by_stream.set(data.stream_id, {...data, drifted: false});
    } else {
        occupancy_by_stream.delete(data.stream_id);
    }
    apply();
}

// Reconcile every stream row against the current occupancy: augment the ones with
// a live call, strip the augmentation from the ones without. Idempotent.
function apply(): void {
    for (const li of $("#stream_filters .narrow-filter")) {
        const $li = $(li);
        const stream_id = Number.parseInt($li.attr("data-stream-id") ?? "", 10);
        const occupancy = Number.isNaN(stream_id) ? undefined : occupancy_by_stream.get(stream_id);
        if (occupancy === undefined) {
            clear_row($li);
        } else {
            augment_row($li, stream_id, occupancy);
        }
    }
    apply_dm_rows();
}

// A DM/group row gets a speaker beside its label while a call is live there.
// Unlike channels there are no avatars: the row is already a list of people.
// Exported because the speaker lives in a slot the DM list owns: re-rendering
// the list empties it, so pm_list calls this after it rebuilds rather than
// leaving the speaker missing until the next poll.
export function apply_dm_rows(): void {
    const me = people.my_current_user_id();
    for (const li of $(".dm-list-item[data-user-ids-string]")) {
        const slot = li.querySelector(".jitsi-dm-speaker-slot");
        if (slot === null) {
            continue;
        }
        const row_ids = (li.getAttribute("data-user-ids-string") ?? "")
            .split(",")
            .map(Number)
            .filter((id) => !Number.isNaN(id));
        // The row omits you; a call's participant set includes you.
        const active = dm_calls.has(dm_key([...row_ids, me]));
        const has_icon = slot.querySelector(".jitsi-call-speaker-icon") !== null;
        if (active && !has_icon) {
            slot.append(make_speaker_icon());
        } else if (!active && has_icon) {
            slot.replaceChildren();
        }
    }
}

// A loudspeaker, built as SVG so it needs no addition to the icon font and
// inherits currentColor (the channel's existing privacy color).
function make_speaker_icon(): SVGSVGElement {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.classList.add("jitsi-call-speaker-icon");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("aria-hidden", "true");
    const body = document.createElementNS(SVG_NS, "path");
    body.setAttribute("d", "M8.5 2.4 4.7 5.4H2.2v5.2h2.5l3.8 3z");
    body.setAttribute("fill", "currentColor");
    svg.append(body);
    for (const d of ["M11 5.6a3.2 3.2 0 0 1 0 4.8", "M12.7 3.8a5.6 5.6 0 0 1 0 8.4"]) {
        const wave = document.createElementNS(SVG_NS, "path");
        wave.setAttribute("d", d);
        wave.setAttribute("fill", "none");
        wave.setAttribute("stroke", "currentColor");
        wave.setAttribute("stroke-width", "1.2");
        wave.setAttribute("stroke-linecap", "round");
        svg.append(wave);
    }
    return svg;
}

function augment_row($li: JQuery, stream_id: number, occupancy: SidebarOccupancy): void {
    const is_private = sub_store.get(stream_id)?.invite_only ?? false;
    $li.addClass("jitsi-call-active").toggleClass("jitsi-call-private", is_private);
    ensure_icons($li, is_private);
    render_occupants($li, stream_id, occupancy);
}

// A small padlock, drawn rather than taken from the icon font: the font's
// .zulip-icon-lock is sized by a more specific sidebar rule, which made the
// badge as large as the speaker it is supposed to sit in the corner of.
function make_lock_badge(): SVGSVGElement {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.classList.add("jitsi-call-lock-badge");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("aria-hidden", "true");
    const shackle = document.createElementNS(SVG_NS, "path");
    shackle.setAttribute("d", "M5 7.5V5.4a3 3 0 0 1 6 0v2.1");
    shackle.setAttribute("fill", "none");
    shackle.setAttribute("stroke", "currentColor");
    shackle.setAttribute("stroke-width", "1.8");
    svg.append(shackle);
    const body = document.createElementNS(SVG_NS, "rect");
    body.setAttribute("x", "3");
    body.setAttribute("y", "7");
    body.setAttribute("width", "10");
    body.setAttribute("height", "7");
    body.setAttribute("rx", "1.4");
    body.setAttribute("fill", "currentColor");
    svg.append(body);
    return svg;
}

function ensure_icons($li: JQuery, is_private: boolean): void {
    const privacy = $li.find(".stream-privacy").first().get(0);
    if (privacy === undefined) {
        return;
    }
    // The speaker and its lock live in a wrapper sized to the glyph, so the
    // badge anchors to the speaker's corner rather than to the whole cell.
    let glyph = privacy.querySelector(".jitsi-call-glyph");
    if (glyph === null) {
        glyph = document.createElement("span");
        glyph.className = "jitsi-call-glyph";
        glyph.append(make_speaker_icon());
        privacy.append(glyph);
    }
    const lock = glyph.querySelector(".jitsi-call-lock-badge");
    if (is_private && lock === null) {
        glyph.append(make_lock_badge());
    } else if (!is_private && lock !== null) {
        lock.remove();
    }
}

function render_occupants($li: JQuery, stream_id: number, occupancy: SidebarOccupancy): void {
    let container = $li.children(".jitsi-sidebar-occupants").get(0);
    if (container === undefined) {
        container = document.createElement("div");
        container.className = "jitsi-sidebar-occupants";
        const header = $li.children(".bottom_left_row").first().get(0);
        if (header === undefined) {
            $li.get(0)?.append(container);
        } else {
            header.after(container);
        }
    }
    container.replaceChildren();

    // A drifted or nameless roster: an honest count chip rather than wrong avatars.
    if (occupancy.drifted || occupancy.occupants.length === 0) {
        const chip = document.createElement("span");
        chip.className = "jitsi-sidebar-count";
        chip.textContent = `${occupancy.count} in call`;
        container.append(chip);
        return;
    }

    const speaking = speaking_by_stream.get(stream_id);
    for (const person of occupancy.occupants) {
        const row = document.createElement("div");
        row.className = "jitsi-sidebar-occupant";

        const avatar = document.createElement("span");
        avatar.className = "jitsi-sidebar-avatar";
        avatar.title = person.name;
        if (speaking?.has(person.name) === true) {
            avatar.classList.add("speaking");
        }
        if (person.user_id !== null) {
            const img = document.createElement("img");
            img.src = `/avatar/${person.user_id}/medium`;
            img.alt = person.name;
            avatar.append(img);
        } else {
            // No Zulip id: a nameless initial, never an avatar request that 404s.
            avatar.textContent = [...person.name][0]?.toUpperCase() ?? "?";
        }
        row.append(avatar);

        const name = document.createElement("span");
        name.className = "jitsi-sidebar-name";
        name.textContent = person.name;
        row.append(name);

        container.append(row);
    }
}

function clear_row($li: JQuery): void {
    if (!$li.hasClass("jitsi-call-active")) {
        return;
    }
    $li.removeClass("jitsi-call-active jitsi-call-private");
    $li.find(".stream-privacy .jitsi-call-glyph").remove();
    $li.children(".jitsi-sidebar-occupants").remove();
}

// Called by the embedded call with the display names currently speaking in the one
// call the user is in (empty to clear, e.g. when the call ends). Every avatar in
// the matching channel is lit or unlit to match; other channels have no speaker
// data at all.
export function set_speaking(stream_id: number, names: readonly string[]): void {
    if (names.length === 0) {
        speaking_by_stream.delete(stream_id);
    } else {
        speaking_by_stream.set(stream_id, new Set(names));
    }
    const speaking = speaking_by_stream.get(stream_id);
    const li = document.querySelector(
        `#stream_filters .narrow-filter[data-stream-id="${CSS.escape(String(stream_id))}"]`,
    );
    if (li === null) {
        return;
    }
    for (const avatar of li.querySelectorAll<HTMLElement>(".jitsi-sidebar-avatar")) {
        avatar.classList.toggle("speaking", speaking?.has(avatar.title) ?? false);
    }
}
