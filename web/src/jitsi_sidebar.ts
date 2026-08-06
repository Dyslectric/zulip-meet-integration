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
import * as stream_data from "./stream_data.ts";
import * as sub_store from "./sub_store.ts";
import type {StreamSubscription} from "./sub_store.ts";

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
        // A room can be active with nobody in it -- created and not yet joined,
        // or everyone has left and the service has not retired it. From the
        // sidebar's point of view that is not a call: showing a speaker and
        // "0 in call" for it says something untrue.
        if (!room.active || room.count === 0) {
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
    // An empty room is not a call; see ingest.
    const live = data.active && data.count > 0;
    if (data.stream_id !== undefined) {
        if (live) {
            occupancy_by_stream.set(data.stream_id, {
                ...data,
                stream_id: data.stream_id,
                drifted: false,
            });
        } else {
            occupancy_by_stream.delete(data.stream_id);
        }
    } else if (data.user_ids !== undefined) {
        const key = dm_key(data.user_ids);
        if (live) {
            dm_calls.add(key);
        } else {
            dm_calls.delete(key);
        }
    }
    apply();
}

// Reconcile every stream row against the current occupancy: augment the ones with
// a live call, strip the augmentation from the ones without. Idempotent.
//
// Exported (as apply_channel_rows) for the same reason as apply_dm_rows below:
// the glyphs live in DOM the stream list owns, so a rebuild drops them. Without
// that a channel keeps the template's plain glyph until the next occupancy poll
// — most visible right after creating one, where the icon would be wrong for up
// to fifteen seconds. Deliberately does NOT touch the DM rows: the stream list
// has not rebuilt those, and pm_list calls apply_dm_rows itself when it has.
export function apply_channel_rows(): void {
    for (const li of $("#stream_filters .narrow-filter")) {
        const $li = $(li);
        const stream_id = Number.parseInt($li.attr("data-stream-id") ?? "", 10);
        const sub = Number.isNaN(stream_id) ? undefined : sub_store.get(stream_id);

        // The glyph is a property of the channel, not of any call in it.
        if (sub === undefined) {
            remove_channel_glyph($li);
        } else {
            ensure_channel_glyph($li, sub);
        }

        const occupancy = Number.isNaN(stream_id) ? undefined : occupancy_by_stream.get(stream_id);
        if (occupancy === undefined) {
            clear_row($li);
        } else {
            augment_row($li, stream_id, occupancy);
        }
    }
}

function apply(): void {
    apply_channel_rows();
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
    $li.addClass("jitsi-call-active");
    render_occupants($li, stream_id, occupancy);
}

// Whether a channel offers calls. A web-public channel never does, whatever its
// own setting says: a call is for a known set of people, and anyone on the
// internet can read such a channel. The server refuses to mint a token for one,
// so this keeps the sidebar from advertising what it would refuse.
// Both predicates live in stream_data, which is where channel capabilities
// belong: the narrow guard and the search filter need them too, and neither
// should have to depend on a sidebar module. Re-exported here because the call
// sites in this file and in stream_list already read from it.
export const channel_allows_calls = stream_data.channel_allows_calls;
export const channel_is_voice_channel = stream_data.channel_is_voice_channel;
export const channel_has_no_text_chat = stream_data.channel_has_no_text_chat;
// Re-exported rather than redefined: see stream_data for why these live there.

// What a sidebar row says about a channel is two glyphs, not one: a main glyph
// for what kind of conversation the channel is, and a corner badge for the
// qualifier. The main glyph answers "how do I talk here" — a speaker for a voice
// channel, a folder for one with topics, a hash for a single-threaded one. The
// badge answers "who can see it", except on a public voice channel that still
// carries text, where privacy has nothing to say and the hash marks the text.
type GlyphKind = "speaker" | "globe" | "folder" | "hashtag";
type BadgeKind = "lock" | "folder" | "hashtag";

// The main glyph is whatever is loudest about the channel, in that order: a
// voice channel is a speaker, a web-public one is a globe, and an ordinary one
// is a folder or a hash depending on whether it carries topics.
function glyph_kind(sub: StreamSubscription): GlyphKind {
    if (channel_is_voice_channel(sub)) {
        return "speaker";
    }
    if (sub.is_web_public) {
        return "globe";
    }
    return stream_data.is_empty_topic_only_channel(sub.stream_id) ? "hashtag" : "folder";
}

// The badge carries whatever the main glyph could not. A web-public channel has
// already spent its glyph on the globe, so the badge is where its topics show
// up; everywhere else privacy wins the slot, which is why a private voice
// channel with single-threaded text wears a lock rather than a hash and so
// reads the same as a private voice channel with no text at all.
function badge_kind(sub: StreamSubscription): BadgeKind | undefined {
    if (sub.is_web_public) {
        return stream_data.is_empty_topic_only_channel(sub.stream_id) ? undefined : "folder";
    }
    if (sub.invite_only) {
        return "lock";
    }
    if (channel_is_voice_channel(sub) && !sub.text_chat_disabled) {
        return "hashtag";
    }
    return undefined;
}

// The shapes, drawn rather than taken from the icon font: the font's glyphs are
// sized by a more specific sidebar rule, which made a badge as large as the
// glyph it is supposed to be a corner of. Each shape is drawn once and used at
// both sizes — a folder is the main glyph on a topical channel and the badge on
// a web-public one — with CSS doing the scaling.
function stroke_into(svg: SVGSVGElement, d: string, width: string): void {
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d);
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", width);
    path.setAttribute("stroke-linecap", "round");
    svg.append(path);
}

function draw_lock(svg: SVGSVGElement): void {
    stroke_into(svg, "M5 7.5V5.4a3 3 0 0 1 6 0v2.1", "1.8");
    const body = document.createElementNS(SVG_NS, "rect");
    body.setAttribute("x", "3");
    body.setAttribute("y", "7");
    body.setAttribute("width", "10");
    body.setAttribute("height", "7");
    body.setAttribute("rx", "1.4");
    body.setAttribute("fill", "currentColor");
    svg.append(body);
}

// A folder: the channel keeps its conversation in named topics.
function draw_folder(svg: SVGSVGElement): void {
    const body = document.createElementNS(SVG_NS, "path");
    body.setAttribute(
        "d",
        "M1.5 4.2c0-.7.6-1.2 1.2-1.2h3.1c.4 0 .8.2 1 .5l.9 1.2h5.6c.7 0 1.2.6 1.2 1.2v6.1c0 .7-.6 1.2-1.2 1.2H2.7c-.7 0-1.2-.6-1.2-1.2z",
    );
    body.setAttribute("fill", "currentColor");
    svg.append(body);
}

// A hash: a single-threaded channel, all of its messages in one conversation.
function draw_hashtag(svg: SVGSVGElement): void {
    for (const d of ["M6.3 2.5 4.9 13.5", "M11.4 2.5 10 13.5", "M3 6.2h10", "M2.6 10.2h10"]) {
        stroke_into(svg, d, "2");
    }
}

function blank_svg(class_name: string): SVGSVGElement {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.classList.add(class_name);
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("aria-hidden", "true");
    return svg;
}

function make_privacy_badge(kind: BadgeKind): SVGSVGElement {
    const svg = blank_svg("jitsi-privacy-badge");
    svg.dataset["privacy"] = kind;
    if (kind === "lock") {
        draw_lock(svg);
    } else if (kind === "folder") {
        draw_folder(svg);
    } else {
        draw_hashtag(svg);
    }
    return svg;
}

function make_glyph_icon(kind: GlyphKind): Element {
    if (kind === "speaker") {
        return make_speaker_icon();
    }
    // The globe and the hash are Zulip's own icon-font glyphs rather than ones
    // of ours: they are the established marks for web-public and for a channel,
    // and nicer than anything worth redrawing. Nesting them inside our wrapper
    // keeps them clear of the rule that hides the template's glyph, which only
    // reaches direct children. Our drawn hash survives only as the corner badge
    // on a voice channel, where the font glyph is sized by a more specific rule
    // than we can usefully fight.
    if (kind === "globe" || kind === "hashtag") {
        const i = document.createElement("i");
        i.className = kind === "globe" ? "zulip-icon zulip-icon-globe" : "zulip-icon zulip-icon-hashtag";
        i.setAttribute("aria-hidden", "true");
        return i;
    }
    const svg = blank_svg("jitsi-call-speaker-icon");
    draw_folder(svg);
    return svg;
}

// Replaces the template's privacy icon with the pair described above. Runs for
// every channel row, not just voice ones, so the whole sidebar speaks one
// vocabulary. Independent of whether a call is live: the speaker advertises that
// the channel supports calls at all.
function ensure_channel_glyph($li: JQuery, sub: StreamSubscription): void {
    const privacy = $li.find(".stream-privacy").first().get(0);
    if (privacy === undefined) {
        return;
    }
    $li.addClass("jitsi-custom-glyph");
    $li.toggleClass("jitsi-voice-channel", channel_is_voice_channel(sub));

    // The glyph and its badge live in a wrapper sized to the glyph, so the badge
    // anchors to the glyph's corner rather than to the whole cell.
    let glyph = privacy.querySelector(".jitsi-call-glyph");
    if (glyph === null) {
        glyph = document.createElement("span");
        glyph.className = "jitsi-call-glyph";
        privacy.append(glyph);
    }

    const kind = glyph_kind(sub);
    const icon = glyph.querySelector<HTMLElement | SVGElement>("[data-glyph]");
    if (icon === null || icon.dataset["glyph"] !== kind) {
        const next = make_glyph_icon(kind);
        if (next instanceof HTMLElement || next instanceof SVGElement) {
            next.dataset["glyph"] = kind;
        }
        if (icon === null) {
            glyph.prepend(next);
        } else {
            icon.replaceWith(next);
        }
    }

    const badge_wanted = badge_kind(sub);
    const badge = glyph.querySelector(".jitsi-privacy-badge");
    if (badge_wanted === undefined) {
        badge?.remove();
    } else if (badge === null) {
        glyph.append(make_privacy_badge(badge_wanted));
    } else if (badge instanceof SVGElement && badge.dataset["privacy"] !== badge_wanted) {
        // The channel's privacy or text mode changed under us.
        badge.replaceWith(make_privacy_badge(badge_wanted));
    }
}

function remove_channel_glyph($li: JQuery): void {
    if (!$li.hasClass("jitsi-custom-glyph")) {
        return;
    }
    $li.removeClass("jitsi-custom-glyph jitsi-voice-channel");
    $li.find(".stream-privacy .jitsi-call-glyph").remove();
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

// Strips what a live call adds. Deliberately leaves the speaker glyph alone:
// that belongs to the channel, and ensure_voice_glyph owns it.
function clear_row($li: JQuery): void {
    if (!$li.hasClass("jitsi-call-active")) {
        return;
    }
    $li.removeClass("jitsi-call-active");
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
