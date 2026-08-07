// The rooms inside a lounge, and which lounges are open in the sidebar.
//
// A room lasts only as long as somebody is in it, so there is no history to keep
// and nothing to cache across a reload: this module holds the present moment and
// re-asks the server whenever it might have changed. The two things that change
// it are a `lounge_rooms` event (somebody started one) and occupancy going to
// zero (the last person left, and the server has already deleted the row).
//
// Occupancy itself is not here. It arrives through the same feed as every other
// call's, in jitsi_sidebar, which is also what draws these rows — a room's
// roster is the same thing as a channel call's roster, and having two ways to
// render one would guarantee they drifted.

import * as z from "zod/mini";

import * as channel from "./channel.ts";
import {page_params} from "./page_params.ts";

export const lounge_room_schema = z.object({
    id: z.number(),
    channel_id: z.number(),
    name: z.string(),
    creator_id: z.nullable(z.number()),
    is_private: z.boolean(),
    can_join: z.boolean(),
    can_knock: z.boolean(),
    can_administer: z.boolean(),
    // The door is shut for want of a moderator. Deliberately separate from
    // `can_join`: that one is shut to *you* and stays shut, this one is shut to
    // everyone until somebody who runs the room walks in, and the two must not
    // look the same.
    waiting_for_doorman: z.boolean(),
    knockable_by_users: z.boolean(),
    knockable_by_guests: z.boolean(),
    // Sent only to whoever moderates the room: the lounge is entitled to see
    // that a room is locked, not to see who has been let into it.
    invited_user_ids: z.optional(z.array(z.number())),
});
export type LoungeRoom = z.infer<typeof lounge_room_schema>;

const rooms_response_schema = z.object({rooms: z.array(lounge_room_schema)});

// Keyed by lounge. A lounge with no live rooms has no entry rather than an empty
// one, so "is anything happening in here" is a single lookup.
const rooms_by_channel = new Map<number, LoungeRoom[]>();

// Which lounges the user has opened. Expansion is a client-side thing and stays
// one: it says what you are looking at, not what is true of the lounge, so it
// must not follow you to another device or be lost when the room list changes.
const expanded = new Set<number>();

let on_change: () => void = () => {
    // Set by initialize(); a no-op until then so an early event is harmless.
};

// How often a visitor re-asks for the room list. Matched to the occupancy poll
// so that the two halves of what they are shown -- which rooms exist, and who is
// in them -- cannot disagree for long.
const VISITOR_POLL_MS = page_params.development_environment ? 2000 : 15000;

export function initialize(redraw: () => void): void {
    on_change = redraw;
    fetch_rooms();

    // A visitor with no account has no event queue, so `lounge_rooms` events --
    // the thing that tells everybody else a room was started, locked, or opened
    // to knocking -- never reach them. Without this their sidebar is frozen at
    // page load: a room made non-knockable still offers them an ask control, and
    // a room started after they arrived never appears at all.
    //
    // Polling rather than anything cleverer for the same reason the knock status
    // is polled: there is nowhere to push to.
    if (page_params.is_spectator) {
        setInterval(fetch_rooms, VISITOR_POLL_MS);
    }
}

export function rooms_in(channel_id: number): LoungeRoom[] {
    return rooms_by_channel.get(channel_id) ?? [];
}

// A room row carries only its id, so the click handler has to look the rest up.
// Undefined when the room ended between the draw and the click, which is an
// ordinary thing to happen in a lounge rather than an error.
export function room_by_id(room_id: number): LoungeRoom | undefined {
    for (const list of rooms_by_channel.values()) {
        const found = list.find((room) => room.id === room_id);
        if (found !== undefined) {
            return found;
        }
    }
    return undefined;
}

export function is_expanded(channel_id: number): boolean {
    return expanded.has(channel_id);
}

export function toggle_expanded(channel_id: number): void {
    if (expanded.has(channel_id)) {
        expanded.delete(channel_id);
    } else {
        expanded.add(channel_id);
    }
    on_change();
}

// Re-ask for every lounge rather than for the one that changed. The rule for
// whether a given user may join a given room is the server's, and a per-user
// answer cannot be broadcast in an event, so the event says only "look again";
// asking for everything keeps this module from having to merge two shapes of
// truth. Lounges change rarely enough that the extra rows cost nothing.
export function fetch_rooms(): void {
    void channel.get({
        url: "/json/lounges/rooms",
        success(raw: unknown): void {
            const parsed = rooms_response_schema.safeParse(raw);
            if (!parsed.success) {
                return;
            }
            rooms_by_channel.clear();
            for (const room of parsed.data.rooms) {
                const list = rooms_by_channel.get(room.channel_id) ?? [];
                list.push(room);
                rooms_by_channel.set(room.channel_id, list);
                // The answer to a knock is the room opening, so a room this
                // user can now join is one their knock has been answered by.
                // Nothing else says so, and nothing else needs to.
                if (room.can_join) {
                    my_knocks.delete(room.id);
                }
            }
            on_change();
        },
    });
}

// A `lounge_rooms` event: this lounge's rooms are not what they were.
export function handle_rooms_changed(): void {
    fetch_rooms();
}

// The last set of live rooms the occupancy feed reported, so a change in it can
// be noticed. Empty string rather than an empty set so the first poll after a
// reload compares equal to itself and does not refetch what initialize() just
// fetched.
let last_live_key = "";

// Reconcile against the occupancy poll, which lists every live room there is.
//
// Everything else in this sidebar heals itself on that poll; without this the
// room list would be the one thing that could not. It changes only on a pushed
// event, and a push can be missed -- the service restarts, the tab was asleep,
// the event was dropped -- after which a room that has ended sits in the sidebar
// indefinitely, advertising a conversation that is not happening. That is the
// failure people actually hit, because the push they miss is usually the one
// saying the room they just left is over.
//
// Refetches rather than deleting locally: the feed says which rooms are occupied,
// not which exist, and a room that has been started but not yet entered is in
// the second set and not the first. Asking the server keeps that distinction
// where it is made instead of guessing at it here.
//
// Keyed on *who* is in each room and not merely on which rooms are live, because
// `waiting_for_doorman` turns on the difference. A doorman leaving a room
// that still has other people in it changes nothing about the set of live rooms,
// so a set-only key would leave the sidebar offering a way in that the mint has
// already started refusing — which is precisely the case this whole field exists
// to make visible. Members only: a guest carries no user id and can never be the
// one holding a door open.
export function reconcile_live_rooms(live: {room_id: number; member_ids: number[]}[]): void {
    const key = live
        .map((room) => `${room.room_id}:${room.member_ids.toSorted((a, b) => a - b).join("+")}`)
        .toSorted()
        .join(",");
    if (key === last_live_key) {
        return;
    }
    last_live_key = key;
    fetch_rooms();
}

// The last person left a room, so the server has already deleted it. Dropping it
// here too means the row goes at the same moment the roster does, rather than
// lingering until the refetch lands.
export function forget_room(room_id: number): void {
    knocks_by_room.delete(room_id);
    my_knocks.delete(room_id);
    for (const [channel_id, list] of rooms_by_channel) {
        const remaining = list.filter((room) => room.id !== room_id);
        if (remaining.length === list.length) {
            continue;
        }
        if (remaining.length === 0) {
            rooms_by_channel.delete(channel_id);
        } else {
            rooms_by_channel.set(channel_id, remaining);
        }
        on_change();
        return;
    }
}

// -- knocking ---------------------------------------------------------------
//
// A knock is a live request and is kept nowhere, on the server or here: somebody
// is at the door now, and in a couple of minutes they are not. Holding them
// would turn a lounge into a queue of stale requests to work through, which is
// the opposite of what an ambient-presence surface is for.

// How long somebody stands at the door before we stop showing them there. Long
// enough to answer a knock you did not see immediately, short enough that a
// person who knocked and wandered off is not still being offered.
export const KNOCK_TTL_MS = 2 * 60 * 1000;

// Two kinds of person can be at a door, and they are answered differently: an
// account holder is admitted by widening the room's invited set, a visitor by
// marking the short-lived knock they will come back with. So a knocker carries
// which it is, and the key is namespaced to keep the two from colliding.
export type Knocker =
    | {key: string; kind: "user"; user_id: number; expires_at: number}
    | {key: string; kind: "guest"; knock_id: string; name: string; expires_at: number};

// room id -> key -> knocker. Only ever populated for a user who moderates the
// room, because the server only tells those users.
const knocks_by_room = new Map<number, Map<string, Knocker>>();

// Rooms this user has knocked on, and when that knock goes quiet. Local because
// the server keeps no record to ask: it says only that the request went out.
const my_knocks = new Map<number, number>();

function remember_knock(room_id: number, knocker: Knocker): void {
    const knockers = knocks_by_room.get(room_id) ?? new Map<string, Knocker>();
    knockers.set(knocker.key, knocker);
    knocks_by_room.set(room_id, knockers);
    // Checked against the stored expiry rather than trusted: knocking again
    // pushes the deadline out, and the earlier timer must not then take the
    // person off the door while they are still waiting.
    setTimeout(() => {
        if ((knocks_by_room.get(room_id)?.get(knocker.key)?.expires_at ?? 0) <= Date.now()) {
            clear_knock(room_id, knocker.key);
        }
    }, KNOCK_TTL_MS + 500);
    on_change();
}

export function record_knock(room_id: number, user_id: number): void {
    remember_knock(room_id, {
        key: `user:${user_id}`,
        kind: "user",
        user_id,
        expires_at: Date.now() + KNOCK_TTL_MS,
    });
}

// A visitor, who has no id to look up: the name they typed is all there is to
// show, and it arrives already marked as a guest's so that no client is tempted
// to render it as though the deployment vouched for it.
export function record_guest_knock(room_id: number, knock_id: string, name: string): void {
    remember_knock(room_id, {
        key: `guest:${knock_id}`,
        kind: "guest",
        knock_id,
        name,
        expires_at: Date.now() + KNOCK_TTL_MS,
    });
}

export function knockers_at(room_id: number): Knocker[] {
    const knockers = knocks_by_room.get(room_id);
    if (knockers === undefined) {
        return [];
    }
    const now = Date.now();
    return knockers
        .values()
        .filter((knocker) => knocker.expires_at > now)
        .toArray();
}

export function clear_knock(room_id: number, key: string): void {
    const knockers = knocks_by_room.get(room_id);
    if (knockers?.delete(key) !== true) {
        return;
    }
    if (knockers.size === 0) {
        knocks_by_room.delete(room_id);
    }
    on_change();
}

// This user has asked to be let into a room. Recorded so the control can say so
// rather than inviting them to ask again while the first ask is still standing.
export function record_own_knock(room_id: number): void {
    my_knocks.set(room_id, Date.now() + KNOCK_TTL_MS);
    setTimeout(() => {
        if ((my_knocks.get(room_id) ?? 0) <= Date.now()) {
            my_knocks.delete(room_id);
            on_change();
        }
    }, KNOCK_TTL_MS + 500);
    on_change();
}

export function has_knocked(room_id: number): boolean {
    return (my_knocks.get(room_id) ?? 0) > Date.now();
}
