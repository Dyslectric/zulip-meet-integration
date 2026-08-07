// Starting and joining rooms. The data lives in lounge_rooms; this is the half
// that talks to the user.

import {$} from "jquery";
import assert from "minimalistic-assert";
import * as z from "zod/mini";

import render_lounge_room_settings from "../templates/lounge_room_settings.hbs";
import render_start_lounge_room from "../templates/start_lounge_room.hbs";

import * as channel from "./channel.ts";
import * as dialog_widget from "./dialog_widget.ts";
import {start_embedded_call} from "./embedded_call.ts";
import * as guest_call from "./guest_call.ts";
import {$t, $t_html} from "./i18n.ts";
import type {LoungeRoom} from "./lounge_rooms.ts";
import * as lounge_rooms from "./lounge_rooms.ts";
import * as people from "./people.ts";
import * as pill_typeahead from "./pill_typeahead.ts";
import * as sub_store from "./sub_store.ts";
import * as user_pill from "./user_pill.ts";

const call_response_schema = z.object({url: z.string()});
const create_response_schema = z.object({
    room: lounge_rooms.lounge_room_schema,
});

// A room is labelled by itself rather than by its lounge: in a lounge the room
// is the conversation, and "#Watercooler" would name the building rather than
// the meeting you are in.
export function join_room(room: LoungeRoom): void {
    // A visitor with no account takes the guest path to the same room. Only a
    // public room in a web-public lounge is reachable that way — the server
    // refuses the rest — but the client does not need to know which: it asks,
    // and is told no if the answer is no.
    if (guest_call.is_spectator()) {
        guest_call.join_as_guest(
            {lounge_room_id: room.id},
            {label: room.name, stream_id: room.channel_id},
        );
        return;
    }
    void channel.post({
        url: "/json/calls/jitsi/create",
        data: {lounge_room_id: room.id},
        success(response: unknown): void {
            const parsed = call_response_schema.safeParse(response);
            if (!parsed.success) {
                return;
            }
            void start_embedded_call(parsed.data.url, {
                label: room.name,
                stream_id: room.channel_id,
            });
        },
        error(xhr): void {
            // A room can be refused for reasons the row could not have known
            // when it was drawn — it ended, or the last moderator left between
            // the draw and the click. Say so rather than doing nothing.
            guest_call.report_call_refusal(xhr);
        },
    });
}

// Starting a room is two requests, not one: the first makes the room, the second
// gets a token for it. That is deliberate — whoever starts a room enters it by
// exactly the path everyone else does, so there is only one way in to get wrong.
function create_and_join(channel_id: number): void {
    const name = $<HTMLInputElement>("#new_lounge_room_name").val() ?? "";
    const is_private = $<HTMLInputElement>("#new_lounge_room_private").is(":checked");

    dialog_widget.submit_api_request(
        channel.post,
        `/json/lounges/${channel_id}/rooms`,
        {name, is_private: JSON.stringify(is_private)},
        {
            success_continuation(response: unknown): void {
                const parsed = create_response_schema.safeParse(response);
                if (!parsed.success) {
                    return;
                }
                // The listing has not caught up yet — the event that refreshes it
                // is in flight — so join from what we were just told rather than
                // waiting to find the room in the list.
                join_room(parsed.data.room);
                lounge_rooms.fetch_rooms();
            },
        },
    );
}

// Ask to be let into a locked room. The server keeps no record of this — the
// request reaches whichever moderators are listening and is then over — so the
// pending state is local, and the answer, when it comes, is the room simply
// growing a way in.
export function knock_on_room(room: LoungeRoom): void {
    if (lounge_rooms.has_knocked(room.id)) {
        return;
    }
    // A visitor asks by a different route: they have no account to be added to
    // the room's invited set, so what they get back is a short-lived knock id to
    // return with, and they are asked for a name on the way — a moderator
    // deciding whether to admit "Guest" has been told nothing at all.
    if (guest_call.is_spectator()) {
        guest_call.knock_as_guest(room.id, {label: room.name});
        return;
    }
    // Recorded before the request rather than in its callback: the point of the
    // pending state is to stop a second ask, and the window it has to cover is
    // exactly the one where the first is still in flight.
    lounge_rooms.record_own_knock(room.id);
    void channel.post({url: `/json/lounges/rooms/${room.id}/knock`});
}

// Answer a knock. Additive on the server, so two moderators answering two knocks
// in the same moment do not undo each other.
export function admit_to_room(
    room_id: number,
    knocker_key: string,
    who: {user_id: number} | {guest_knock_id: string},
): void {
    void channel.post({
        url: `/json/lounges/rooms/${room_id}/admit`,
        data: who,
        success(): void {
            // They are through the door, so they are no longer at it. The room
            // list refreshes from the server's event for an account holder; a
            // visitor changes nobody's room list, and is polling for the answer
            // themselves, so taking them off the door here is the whole of it.
            lounge_rooms.clear_knock(room_id, knocker_key);
        },
        error(xhr): void {
            // A knock expires while a moderator is deciding often enough to be
            // worth saying rather than leaving the row sitting there.
            guest_call.report_call_refusal(xhr);
            lounge_rooms.clear_knock(room_id, knocker_key);
        },
    });
}

// The room's door policy, changed from the room itself. These settings are about
// this conversation and die with it, so there is nowhere in channel settings they
// could sensibly live.
//
// Nothing here ejects anybody: locking a room, or dropping somebody from the
// invited set, changes who may come in from now on and leaves the people already
// talking where they are. That is the same rule as "no moderator present" -- a
// door policy, not a kill switch.
export function room_settings(room: LoungeRoom): void {
    let invited_pills: user_pill.UserPillWidget | undefined;

    function submit(): void {
        const data: Record<string, string> = {
            is_private: JSON.stringify($("#lounge_room_private").is(":checked")),
            knockable_by_users: JSON.stringify($("#lounge_room_knockable_by_users").is(":checked")),
            knockable_by_guests: JSON.stringify(
                $("#lounge_room_knockable_by_guests").is(":checked"),
            ),
        };
        if (invited_pills !== undefined) {
            data["invited_user_ids"] = JSON.stringify(user_pill.get_user_ids(invited_pills));
        }
        dialog_widget.submit_api_request(channel.patch, `/json/lounges/rooms/${room.id}`, data, {
            success_continuation(): void {
                // The server event says only "look again", and the room list is
                // what the sidebar draws from, so re-ask rather than patching
                // the local copy: whether a given user may now join is the
                // server's answer, not one this dialog can work out.
                lounge_rooms.fetch_rooms();
            },
        });
    }

    dialog_widget.launch({
        modal_title_html: $t_html({defaultMessage: "Settings for {room}"}, {room: room.name}),
        modal_content_html: render_lounge_room_settings({
            is_private: room.is_private,
            knockable_by_users: room.knockable_by_users,
            knockable_by_guests: room.knockable_by_guests,
        }),
        id: "lounge_room_settings",
        modal_submit_button_text: $t({defaultMessage: "Save"}),
        loading_spinner: true,
        on_click: submit,
        post_render() {
            const $container = $("#lounge_room_invited_users");
            invited_pills = user_pill.create_pills($container);
            const invited_user_ids = room.invited_user_ids ?? [];
            for (const user_id of invited_user_ids) {
                // A user who has since been deactivated or made inaccessible is
                // skipped rather than shown as a broken pill; saving then drops
                // them, which is the right outcome for a guest list that only
                // has to outlive the conversation.
                const person = people.maybe_get_user_by_id(user_id, true);
                if (person !== undefined) {
                    user_pill.append_user(person, invited_pills);
                }
            }
            pill_typeahead.set_up_user($container.children(".input"), invited_pills, {
                exclude_bots: true,
            });

            // The knock switches and the guest list say nothing about an open
            // room -- nobody is refused entry to one -- so they follow the
            // privacy toggle rather than sitting there greyed out.
            $("#lounge_room_private").on("change", function () {
                $("#lounge_room_private_settings").toggleClass("hide", !$(this).is(":checked"));
            });
        },
    });
}

export function start_room(channel_id: number): void {
    const lounge = sub_store.get(channel_id);
    assert(lounge !== undefined);

    dialog_widget.launch({
        modal_title_html: $t_html(
            {defaultMessage: "Start a room in {lounge}"},
            {lounge: lounge.name},
        ),
        modal_content_html: render_start_lounge_room(),
        id: "start_lounge_room",
        modal_submit_button_text: $t({defaultMessage: "Start"}),
        loading_spinner: true,
        on_click() {
            create_and_join(channel_id);
        },
        on_shown: () => $("#new_lounge_room_name").trigger("focus"),
    });
}
