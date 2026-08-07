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
import {$t} from "./i18n.ts";
import type {LoungeRoom} from "./lounge_rooms.ts";
import * as lounge_rooms from "./lounge_rooms.ts";
import {page_params} from "./page_params.ts";
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
            // Present when this is a room inside a lounge. `stream_id` is set
            // too — it is the lounge — so this has to be checked first, or a
            // lounge's rooms would all be mistaken for calls of the lounge.
            lounge_room_id: z.optional(z.nullable(z.number())),
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
    // Channels whose call would refuse this user right now, for want of a
    // moderator. Computed by the server rather than worked out here: it depends
    // on who moderates the channel and who is in the call, and a client
    // reimplementing either would drift from the rule the mint enforces.
    closed_channel_ids: z.optional(z.array(z.number())),
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
    lounge_room_id: z.optional(z.nullable(z.number())),
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
//
// Much faster in development, where it is not a safety net at all but the main
// way you see anything: there is usually no conferencing service pushing events,
// so fifteen seconds is fifteen seconds of wondering whether the thing you just
// changed works. The cost that makes it slow in production -- a request per
// client per interval, for a feed that rarely changes -- is not a cost worth
// paying on one developer's machine.
const POLL_MS = page_params.development_environment ? 2000 : 15000;
const SVG_NS = "http://www.w3.org/2000/svg";

let poll_interval_id: number | undefined;
// Latest occupancy for each channel with a live call.
const occupancy_by_stream = new Map<number, SidebarOccupancy>();
// Rooms inside lounges, keyed by the room rather than by the channel: a lounge
// holds many at once, so the channel no longer says which one.
const occupancy_by_lounge_room = new Map<number, SidebarOccupancy>();
// Participant-set keys (see dm_key) of the DM/group conversations with a live
// call. Only presence matters for a DM row: it shows a speaker, not a roster.
const dm_calls = new Set<string>();
// The display names currently speaking in the one call we are in, keyed by the
// channel it belongs to. Names (not user ids) because that is what the Jitsi web
// relay reports and what the occupant avatars carry. A set, not one name — the
// per-participant relay can mark several people speaking at once. Empty for every
// channel we are only observing (there is no speaker data for those).
const speaking_by_stream = new Map<number, Set<string>>();
// Channels whose call currently refuses this user for want of a moderator. The
// call button is withheld on these rather than left to fail on click.
const closed_channels = new Set<number>();

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
    occupancy_by_lounge_room.clear();
    dm_calls.clear();
    const parsed = occupancy_all_schema.safeParse(raw);
    if (!parsed.success) {
        return;
    }
    closed_channels.clear();
    const closed_ids = parsed.data.closed_channel_ids ?? [];
    for (const stream_id of closed_ids) {
        closed_channels.add(stream_id);
    }
    for (const room of parsed.data.rooms) {
        // A room can be active with nobody in it -- created and not yet joined,
        // or everyone has left and the service has not retired it. From the
        // sidebar's point of view that is not a call: showing a speaker and
        // "0 in call" for it says something untrue.
        if (!room.active || room.count === 0) {
            continue;
        }
        // Checked before stream_id, which a lounge room carries as well: that is
        // the lounge it lives in, not a call belonging to the lounge itself.
        if (room.lounge_room_id !== undefined && room.lounge_room_id !== null) {
            occupancy_by_lounge_room.set(room.lounge_room_id, {
                ...room,
                stream_id: room.stream_id ?? 0,
            });
        } else if (room.stream_id !== undefined) {
            occupancy_by_stream.set(room.stream_id, {...room, stream_id: room.stream_id});
        } else if (room.user_ids !== undefined) {
            dm_calls.add(dm_key(room.user_ids));
        }
    }

    // This feed lists every live room there is, which makes it the one place
    // that can tell the room list it is out of date -- a pushed event can be
    // missed, and the one most likely to be missed is the one saying the room
    // you just left has ended.
    lounge_rooms.reconcile_live_rooms(
        occupancy_by_lounge_room
            .entries()
            .map(([room_id, occupancy]) => ({
                room_id,
                member_ids: occupancy.occupants
                    .map((person) => person.user_id)
                    .filter((user_id) => user_id !== null),
            }))
            .toArray(),
    );
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
    if (data.lounge_room_id !== undefined && data.lounge_room_id !== null) {
        if (live) {
            occupancy_by_lounge_room.set(data.lounge_room_id, {
                ...data,
                stream_id: data.stream_id ?? 0,
                drifted: false,
            });
        } else {
            occupancy_by_lounge_room.delete(data.lounge_room_id);
            // Only `active: false` means the room is over. An empty-but-active
            // room is one that has been started and not yet entered, or one
            // between its last occupant leaving and the next arriving; dropping
            // the row for those makes a room vanish moments after somebody
            // starts it. Emptiness clears the faces, nothing more.
            if (!data.active) {
                lounge_rooms.forget_room(data.lounge_room_id);
            }
        }
    } else if (data.stream_id !== undefined) {
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

        // Withhold the way in when the server says this channel's call would
        // refuse us. The mint refuses it either way; this stops the sidebar
        // advertising a door that is shut, and says why on hover. A lounge's
        // own row has no call button — its rooms carry their own — so this only
        // reaches voice channels.
        // Guarded on the class rather than applied every time, for the same
        // reason clear_row is: this runs for every channel row on every poll,
        // and reaching into a row that has not changed is work for nothing.
        const closed = !Number.isNaN(stream_id) && closed_channels.has(stream_id);
        if ($li.hasClass("jitsi-call-closed") !== closed) {
            $li.toggleClass("jitsi-call-closed", closed);
            $li.find(".jitsi-sidebar-call-button").attr(
                "data-tippy-content",
                closed
                    ? waiting_for_doorman_text(stream_id)
                    : $t({defaultMessage: "Start or join a call"}),
            );
        }

        if (sub !== undefined && channel_is_lounge(sub)) {
            // A lounge has no call of its own to show occupancy for; what it has
            // is rooms, and they carry their own rosters.
            clear_row($li);
            render_lounge($li, sub.stream_id);
            continue;
        }
        clear_lounge($li);

        const occupancy = Number.isNaN(stream_id) ? undefined : occupancy_by_stream.get(stream_id);
        if (occupancy === undefined) {
            clear_row($li);
        } else {
            augment_row($li, stream_id, occupancy);
        }
    }
}

// A lounge's rooms, listed beneath it while it is open. Collapsed shows nothing
// at all rather than a summary: the glyph already says whether anything is live,
// and a count would be a second, staler way of saying it.
function render_lounge($li: JQuery, stream_id: number): void {
    if (!lounge_rooms.is_expanded(stream_id)) {
        clear_lounge($li);
        return;
    }
    $li.addClass("jitsi-lounge-expanded");

    let container = $li.children(".jitsi-lounge-rooms").get(0);
    if (container === undefined) {
        container = document.createElement("div");
        container.className = "jitsi-lounge-rooms";
        const header = $li.children(".bottom_left_row").first().get(0);
        if (header === undefined) {
            $li.get(0)?.append(container);
        } else {
            header.after(container);
        }
    }
    container.replaceChildren();

    const rooms = lounge_rooms.rooms_in(stream_id);
    if (rooms.length === 0) {
        // The empty state is the ordinary state of a lounge, not a failure, so
        // it is stated plainly. It no longer says what to do: starting a room is
        // the plus button on the row above, where every other channel keeps the
        // same gesture, and repeating it here would be a second way to do one
        // thing.
        const empty = document.createElement("div");
        empty.className = "jitsi-lounge-empty";
        empty.textContent = $t({defaultMessage: "No one is talking yet."});
        container.append(empty);
    }

    for (const room of rooms) {
        container.append(make_room_block(room, occupancy_by_lounge_room.get(room.id), stream_id));
    }
}

// A room and the people in it: the room's own row, then one line per occupant
// beneath it, exactly as a channel with a live call lists its own. A lounge is
// for seeing who is around, and a row of overlapping faces answers "how many"
// when the question is "who".
function make_room_block(
    room: LoungeRoom,
    occupancy: SidebarOccupancy | undefined,
    stream_id: number,
): DocumentFragment {
    const fragment = document.createDocumentFragment();
    fragment.append(make_room_row(room, occupancy));

    // A drifted roster is one we know to be wrong, so the row's count stands on
    // its own and no names are listed: better to say "four people" than to name
    // three of them wrongly.
    const occupants = occupancy?.drifted === true ? [] : (occupancy?.occupants ?? []);
    // Only the room's moderators are ever told about a knock, so this list is
    // empty for everyone else without needing to be hidden from them.
    const knockers = lounge_rooms.knockers_at(room.id);
    if (occupants.length > 0 || knockers.length > 0) {
        const list = document.createElement("div");
        list.className = "jitsi-lounge-room-occupants";
        // The speaking ring is keyed by channel, and for a lounge that is the
        // lounge itself: the one call you are in is the one being reported on.
        const speaking = speaking_by_stream.get(stream_id);
        for (const person of occupants) {
            list.append(make_occupant_row(person, speaking));
        }
        // Beneath the people already inside, because that is where they are:
        // at the back of the room, not yet in it.
        for (const user_id of knockers) {
            list.append(make_knocker_row(room.id, user_id));
        }
        fragment.append(list);
    }
    return fragment;
}

function make_room_row(room: LoungeRoom, occupancy: SidebarOccupancy | undefined): HTMLElement {
    const row = document.createElement("div");
    row.className = "jitsi-lounge-room";
    row.dataset["loungeRoomId"] = String(room.id);
    // Deliberately not a button. Joining a call is loud and hard to undo -- it
    // takes over your microphone and announces you to everyone already in there
    // -- so it needs its own deliberate target rather than happening because you
    // clicked a row while looking at who was in it.
    //
    // A locked room reads as not-for-you and simply has no join control; the
    // server refuses it too, but you should be able to see that before trying.
    row.classList.toggle("locked", !room.can_join);

    const glyph = document.createElement("span");
    glyph.className = "jitsi-lounge-room-glyph";
    glyph.append(room.is_private ? make_room_lock_icon() : make_speaker_icon());
    row.append(glyph);

    const name = document.createElement("span");
    name.className = "jitsi-lounge-room-name";
    name.textContent = room.name;
    row.append(name);

    // The names are listed beneath, so the row carries a count only when there
    // is something the list cannot say: a roster we know to be wrong, or one we
    // have no names for at all.
    if (occupancy !== undefined && (occupancy.drifted || occupancy.occupants.length === 0)) {
        const chip = document.createElement("span");
        chip.className = "jitsi-sidebar-count";
        chip.textContent = `${occupancy.count}`;
        row.append(chip);
    }

    // Whoever runs the room can change who may come into it, from the room
    // itself: the settings are about this conversation and die with it, so they
    // do not belong anywhere in channel settings.
    if (room.can_administer) {
        const cog = document.createElement("div");
        cog.className = "jitsi-lounge-room-settings-button hidden-for-spectators";
        cog.setAttribute("role", "button");
        cog.setAttribute("tabindex", "0");
        cog.dataset["loungeRoomId"] = String(room.id);
        cog.title = $t({defaultMessage: "Room settings"});
        const cog_icon = document.createElement("i");
        cog_icon.className = "zulip-icon zulip-icon-gear";
        cog_icon.setAttribute("aria-hidden", "true");
        cog.append(cog_icon);
        row.append(cog);
    }

    // Shut for want of whoever holds the door. Said rather than merely enforced:
    // the join would be refused anyway, and a control that looks available until
    // you use it teaches nothing. Distinct from the locked state above because
    // the two are different problems — this one is fixed by somebody arriving,
    // and the wording has to leave that possible.
    if (room.waiting_for_doorman) {
        const waiting = document.createElement("div");
        waiting.className = "jitsi-lounge-room-waiting";
        waiting.title = waiting_for_doorman_text(room.channel_id);
        const icon = document.createElement("i");
        icon.className = "zulip-icon zulip-icon-time";
        icon.setAttribute("aria-hidden", "true");
        waiting.append(icon);
        row.append(waiting);
        row.classList.add("waiting-for-moderator");
        return row;
    }

    // The only way in. Matches the channel and DM rows' call button, so the
    // gesture for "put me in this call" is the same everywhere in the sidebar.
    if (room.can_join) {
        const join = document.createElement("div");
        // Left visible to a visitor with no account when the lounge is
        // web-public, and only then: that toggle is the decision that anyone who
        // can see this lounge may be heard in its rooms, and hiding the way in
        // would be the client overriding it. The server sends `can_join: false`
        // for a private room, so no button is drawn for one anyone is refused.
        const web_public = sub_store.get(room.channel_id)?.is_web_public === true;
        join.className = web_public
            ? "jitsi-lounge-room-call-button"
            : "jitsi-lounge-room-call-button hidden-for-spectators";
        join.setAttribute("role", "button");
        join.setAttribute("tabindex", "0");
        join.dataset["loungeRoomId"] = String(room.id);
        join.title = $t({defaultMessage: "Join this room"});
        const icon = document.createElement("i");
        icon.className = "zulip-icon zulip-icon-voice-call";
        icon.setAttribute("aria-hidden", "true");
        join.append(icon);
        row.append(join);
    } else if (room.can_knock) {
        // A locked room you may ask about gets an ask in the slot the join
        // control would occupy, so the row still answers "what can I do here"
        // in the same place. Once asked it stays visible and says so: knocking
        // again while the first ask is standing achieves nothing, and a control
        // that reverted would invite exactly that.
        const asked = lounge_rooms.has_knocked(room.id);
        const knock = document.createElement("div");
        knock.className = "jitsi-lounge-room-knock-button hidden-for-spectators";
        knock.classList.toggle("asked", asked);
        knock.setAttribute("role", "button");
        knock.setAttribute("tabindex", "0");
        knock.dataset["loungeRoomId"] = String(room.id);
        knock.title = asked
            ? $t({defaultMessage: "You have asked to join"})
            : $t({defaultMessage: "Ask to join this room"});
        const icon = document.createElement("i");
        icon.className = asked ? "zulip-icon zulip-icon-time" : "zulip-icon zulip-icon-user-plus";
        icon.setAttribute("aria-hidden", "true");
        knock.append(icon);
        row.append(knock);
    }
    return row;
}

// Which doorman is missing. The server says only *that* one is — whether the
// user is exempt depends on who they are, and it worked that out already — so
// naming them is the one part of this the client is allowed to do, from the
// channel's own policy. Falls back to the vaguer wording rather than guessing if
// the channel is not in the store, which happens for a moment after a reload.
function waiting_for_doorman_text(channel_id: number): string {
    const policy = sub_store.get(channel_id)?.call_door_policy;
    if (policy === "moderator") {
        return $t({defaultMessage: "Waiting for a moderator to join"});
    }
    if (policy === "authenticated_user") {
        return $t({defaultMessage: "Waiting for someone with an account to join"});
    }
    return $t({defaultMessage: "Waiting for someone to join"});
}

// Somebody standing at the door, listed where the people inside are listed. A
// lounge is for seeing who is around, and a person waiting to be let in is very
// much around; putting them anywhere else — a toast, a modal — would interrupt
// the moderator to say something the sidebar is already the place for.
function make_knocker_row(room_id: number, user_id: number): HTMLElement {
    const person = people.maybe_get_user_by_id(user_id, true);
    const row = make_occupant_row(
        {name: person?.full_name ?? $t({defaultMessage: "Someone"}), user_id},
        undefined,
    );
    row.classList.add("jitsi-lounge-knocker");

    const admit = document.createElement("div");
    admit.className = "jitsi-lounge-admit-button";
    admit.setAttribute("role", "button");
    admit.setAttribute("tabindex", "0");
    admit.dataset["loungeRoomId"] = String(room_id);
    admit.dataset["userId"] = String(user_id);
    admit.title = $t({defaultMessage: "Admit to this room"});
    const icon = document.createElement("i");
    icon.className = "zulip-icon zulip-icon-check";
    icon.setAttribute("aria-hidden", "true");
    admit.append(icon);
    row.append(admit);
    return row;
}

// Guarded on the class rather than just removing the container, for the same
// reason clear_row is: this runs for every channel row on every poll, and a row
// that never had rooms under it must not be touched at all.
function clear_lounge($li: JQuery): void {
    if (!$li.hasClass("jitsi-lounge-expanded")) {
        return;
    }
    $li.removeClass("jitsi-lounge-expanded");
    $li.children(".jitsi-lounge-rooms").remove();
}

// A padlock, for a room you have to be let into. Drawn at the speaker's size
// because it stands where the speaker stands, rather than reusing the corner
// badge, which is sized to sit on another glyph.
function make_room_lock_icon(): SVGSVGElement {
    const svg = blank_svg("jitsi-call-speaker-icon");
    draw_lock(svg);
    return svg;
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
// A speaker: the channel is a voice channel. Drawn into a caller-supplied svg
// like the other shapes here, rather than making its own, because it is now
// wanted at two sizes — the main glyph on an ordinary voice channel, and the
// corner badge on a web-public one, where the globe has taken the glyph.
function draw_speaker(svg: SVGSVGElement): void {
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
}

function make_speaker_icon(): SVGSVGElement {
    const svg = blank_svg("jitsi-call-speaker-icon");
    draw_speaker(svg);
    return svg;
}

function augment_row($li: JQuery, stream_id: number, occupancy: SidebarOccupancy): void {
    $li.addClass("jitsi-call-active");
    render_occupants($li, stream_id, occupancy);
}

// Whether a channel offers calls. Web-public channels are included: such a
// channel is open to whoever can see it, unauthenticated visitors included, and
// whether that is wanted is the administrator's decision, expressed by making it
// web-public at all. (This comment used to say the opposite, from before that
// rule was reversed; the predicate itself has been right throughout.)
// Both predicates live in stream_data, which is where channel capabilities
// belong: the narrow guard and the search filter need them too, and neither
// should have to depend on a sidebar module. Re-exported here because the call
// sites in this file and in stream_list already read from it.
export const channel_allows_calls = stream_data.channel_allows_calls;
export const channel_is_voice_channel = stream_data.channel_is_voice_channel;
export const channel_is_lounge = stream_data.channel_is_lounge;
export const channel_has_no_text_chat = stream_data.channel_has_no_text_chat;
// Re-exported rather than redefined: see stream_data for why these live there.

// What a sidebar row says about a channel is two glyphs, not one: a main glyph
// for what kind of conversation the channel is, and a corner badge for the
// qualifier. The main glyph answers "how do I talk here" — a speaker for a voice
// channel, bars for a lounge, a folder for one with topics, a hash for a
// single-threaded one. The badge answers "who can see it", except on a public
// voice channel that still carries text, where privacy has nothing to say and
// the hash marks the text.
type GlyphKind = "speaker" | "bars" | "globe" | "folder" | "hashtag";
type BadgeKind = "lock" | "folder" | "hashtag" | "bars" | "speaker";

// The main glyph is whatever is loudest about the channel, in that order: a
// voice channel is a speaker, a lounge is bars, a web-public one is a globe, and
// an ordinary one is a folder or a hash depending on whether it carries topics.
//
// Being web-public outranks all of it. Such a channel takes the globe for its
// glyph and drops what it otherwise would have worn — a speaker for a voice
// channel, bars for a lounge — into the badge. Readable by the entire internet
// is the louder fact about a channel than what shape its conversations take, and
// it is the one that has to be legible at a glance down a list of rows; the pair
// still says both things, in the order that matters.
//
// A lounge is checked after a voice channel only for tidiness — the server keeps
// a channel from being both, so the two branches are never in competition.
function glyph_kind(sub: StreamSubscription): GlyphKind {
    if (sub.is_web_public) {
        return "globe";
    }
    if (channel_is_voice_channel(sub)) {
        return "speaker";
    }
    if (channel_is_lounge(sub)) {
        return "bars";
    }
    return stream_data.is_empty_topic_only_channel(sub.stream_id) ? "hashtag" : "folder";
}

// The badge carries whatever the main glyph could not. A web-public channel has
// already spent its glyph on the globe, so the badge is where its kind goes: a
// speaker for a voice channel, bars for a lounge, its topics for anything else.
// Everywhere else privacy wins the slot, which is why a private voice channel
// with single-threaded text wears a lock rather than a hash and so reads the same
// as a private voice channel with no text at all.
//
// A plain public channel gets no badge at all. There is nothing to qualify — it
// is neither locked nor open to the internet — and a badge that only ever means
// "ordinary" is noise on every row that carries it.
function badge_kind(sub: StreamSubscription): BadgeKind | undefined {
    if (sub.is_web_public) {
        if (channel_is_voice_channel(sub)) {
            return "speaker";
        }
        if (channel_is_lounge(sub)) {
            return "bars";
        }
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

// Bars: a lounge, whose conversations are rooms rather than topics. Deliberately
// not a speaker — that mark is spent twice over already, on a voice channel here
// and on a talking participant in the occupancy rows, and a third meaning would
// blur both. Bars read as live audio without being either of those, and they
// leave room to animate when a room in the lounge is live, so the kind of
// channel and the state of it can end up in one glyph rather than a glyph and a
// badge.
//
// Four bars of uneven height, drawn as capsules by the round linecap. The tallest
// is second rather than first: a rising staircase reads as a signal-strength
// meter, which means something else entirely.
function draw_bars(svg: SVGSVGElement): void {
    for (const d of ["M3 6.5v3", "M6.33 3.5v9", "M9.67 5v6", "M13 7v2"]) {
        stroke_into(svg, d, "2");
    }
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
    switch (kind) {
        case "lock":
            draw_lock(svg);
            break;
        case "folder":
            draw_folder(svg);
            break;
        case "bars":
            draw_bars(svg);
            break;
        case "speaker":
            draw_speaker(svg);
            break;
        case "hashtag":
            draw_hashtag(svg);
            break;
    }
    return svg;
}

function make_glyph_icon(kind: GlyphKind): Element {
    if (kind === "speaker") {
        return make_speaker_icon();
    }
    if (kind === "bars") {
        const svg = blank_svg("jitsi-lounge-bars-icon");
        draw_bars(svg);
        return svg;
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
        i.className =
            kind === "globe" ? "zulip-icon zulip-icon-globe" : "zulip-icon zulip-icon-hashtag";
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
    $li.toggleClass("jitsi-lounge", channel_is_lounge(sub));

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
    if (icon?.dataset["glyph"] !== kind) {
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
        container.append(make_occupant_row(person, speaking));
    }
}

// One person in a call: their avatar and their name. Shared between a channel's
// occupant list and a lounge room's, because they are the same thing said in the
// same place -- who is in this call -- and two of these would drift apart.
function make_occupant_row(
    person: {name: string; user_id: number | null},
    speaking: Set<string> | undefined,
): HTMLElement {
    const row = document.createElement("div");
    row.className = "jitsi-sidebar-occupant";

    const avatar = document.createElement("span");
    avatar.className = "jitsi-sidebar-avatar";
    avatar.title = person.name;
    if (speaking?.has(person.name) === true) {
        avatar.classList.add("speaking");
    }
    if (person.user_id === null) {
        // No Zulip id: a nameless initial, never an avatar request that 404s.
        avatar.textContent = [...person.name][0]?.toUpperCase() ?? "?";
    } else {
        const img = document.createElement("img");
        img.src = `/avatar/${person.user_id}/medium`;
        img.alt = person.name;
        avatar.append(img);
    }
    row.append(avatar);

    const name = document.createElement("span");
    name.className = "jitsi-sidebar-name";
    name.textContent = person.name;
    row.append(name);
    return row;
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
