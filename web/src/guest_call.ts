// Joining a call without a Zulip account.
//
// A visitor reading a web-public channel is allowed into its calls: making a
// channel web-public is the decision that anyone who can see it may be heard in
// it. What they lack is an identity, so this is the whole of what is different
// from an ordinary join — ask what to call them, then mint against the guest
// endpoint instead of the member one.
//
// The token that comes back is deliberately weaker than a member's: a generated
// id that cannot be mistaken for a Zulip user's, never a moderator claim, one
// room, short-lived. None of that is decided here; it is decided at the mint,
// which is the only place it can be enforced.

import {$} from "jquery";
import * as z from "zod/mini";

import render_guest_call_name from "../templates/guest_call_name.hbs";

import * as channel from "./channel.ts";
import * as dialog_widget from "./dialog_widget.ts";
import {start_embedded_call} from "./embedded_call.ts";
import * as feedback_widget from "./feedback_widget.ts";
import {$t, $t_html} from "./i18n.ts";
import * as lounge_rooms from "./lounge_rooms.ts";
import {page_params} from "./page_params.ts";

const call_response_schema = z.object({url: z.string()});
const error_response_schema = z.object({msg: z.string()});
const knock_response_schema = z.object({knock_id: z.string()});
const knock_status_schema = z.object({admitted: z.boolean()});

export function is_spectator(): boolean {
    return page_params.is_spectator;
}

// Say why a call was refused, rather than letting the click do nothing.
//
// Zulip's default handling of a 400 on this path is silence, which is the worst
// of the options: the door being shut is a state the user can do something about
// — wait, or ask someone to join — and a control that simply fails to respond
// teaches them only that the button is broken. The server's own wording is used
// because it is the one place the rule is stated.
//
// Lives here rather than in the two callers so a member and a visitor get the
// same answer to the same refusal.
export function report_call_refusal(xhr: JQuery.jqXHR): void {
    const message =
        error_response_schema.safeParse(xhr.responseJSON).data?.msg ??
        $t({defaultMessage: "You cannot join this call right now."});
    feedback_widget.show({
        populate($container) {
            $container.text(message);
        },
        title_text: $t({defaultMessage: "Cannot join"}),
        hide_delay: 6000,
    });
}

// Remembered for the session so a visitor is asked once rather than every time
// they move between rooms in a lounge. Not persisted: it is not an account, and
// storing it would make a throwaway name outlive the visit that needed it.
let remembered_name = "";

// `target` is whatever identifies the conversation to the guest endpoint:
// {stream_id} for a web-public voice channel, {lounge_room_id} for a room in a
// web-public lounge.
export function join_as_guest(
    target: Record<string, number | string>,
    options: Parameters<typeof start_embedded_call>[1] = {},
): void {
    function mint(full_name: string): void {
        remembered_name = full_name;
        void channel.post({
            url: "/json/calls/jitsi/create_as_guest",
            data: {...target, full_name},
            success(response: unknown): void {
                const parsed = call_response_schema.safeParse(response);
                if (!parsed.success) {
                    return;
                }
                void start_embedded_call(parsed.data.url, options);
            },
            error(xhr): void {
                report_call_refusal(xhr);
            },
        });
    }

    ask_for_a_name({
        title: $t_html({defaultMessage: "Join the call"}),
        submit: $t({defaultMessage: "Join"}),
        on_name: mint,
    });
}

// Ask a visitor what to call them, then do the thing that needed a name.
//
// Shared by joining and by knocking, because the two ask the same question for
// the same reason: there is no account to read a name off, and somebody is about
// to be shown to other people. Knocking needs it more than joining does — a
// moderator deciding whether to admit "Guest" has been told nothing at all.
//
// Blank is allowed and becomes a plain "Guest". Refusing to proceed until
// somebody names themselves would be asking for an identity on a channel whose
// whole point is not requiring one; a moderator is free to turn down a knock
// that will not say who it is.
function ask_for_a_name(opts: {
    title: string;
    submit: string;
    on_name: (name: string) => void;
}): void {
    if (remembered_name !== "") {
        opts.on_name(remembered_name);
        return;
    }
    dialog_widget.launch({
        modal_title_html: opts.title,
        modal_content_html: render_guest_call_name(),
        id: "guest_call_name",
        modal_submit_button_text: opts.submit,
        on_click() {
            opts.on_name(($<HTMLInputElement>("#guest_call_name_input").val() ?? "").trim());
            dialog_widget.close();
        },
        on_shown: () => $("#guest_call_name_input").trigger("focus"),
    });
}

// How often to ask whether a knock has been answered. A visitor has no event
// queue of their own, so this is the only way they can find out; the knock
// expires on its own well before the polling would become a nuisance.
const KNOCK_POLL_MS = 3000;

// Ask, as a visitor, to be let into a private room — and keep asking whether the
// answer has come, since there is nowhere to push it to.
//
// The knock id is held only in this tab. It is the visitor's whole claim, it is
// good for one room and one entry, and it dies with the page: a visitor who
// reloads has to ask again, which is the same rule everybody else's knock
// follows.
export function knock_as_guest(
    room_id: number,
    options: Parameters<typeof start_embedded_call>[1] = {},
): void {
    ask_for_a_name({
        title: $t_html({defaultMessage: "Ask to join"}),
        submit: $t({defaultMessage: "Ask to join"}),
        on_name(full_name: string): void {
            remembered_name = full_name;
            void channel.post({
                url: "/json/calls/jitsi/knock_as_guest",
                data: {lounge_room_id: room_id, full_name},
                success(response: unknown): void {
                    const parsed = knock_response_schema.safeParse(response);
                    if (!parsed.success) {
                        return;
                    }
                    lounge_rooms.record_own_knock(room_id);
                    feedback_widget.show({
                        populate($container) {
                            $container.text(
                                $t({
                                    defaultMessage:
                                        "We have asked. You will join automatically if you are let in.",
                                }),
                            );
                        },
                        title_text: $t({defaultMessage: "Waiting to be let in"}),
                        hide_delay: 6000,
                    });
                    poll_for_admission(room_id, parsed.data.knock_id, options);
                },
                error(xhr): void {
                    report_call_refusal(xhr);
                },
            });
        },
    });
}

function poll_for_admission(
    room_id: number,
    knock_id: string,
    options: Parameters<typeof start_embedded_call>[1],
): void {
    const give_up_at = Date.now() + lounge_rooms.KNOCK_TTL_MS;

    function ask(): void {
        if (Date.now() > give_up_at) {
            // The knock has expired server-side too, so there is nothing left to
            // wait for and saying so beats waiting silently forever.
            feedback_widget.show({
                populate($container) {
                    $container.text($t({defaultMessage: "Nobody answered. You can ask again."}));
                },
                title_text: $t({defaultMessage: "No answer"}),
                hide_delay: 6000,
            });
            return;
        }
        void channel.get({
            url: "/json/calls/jitsi/knock_status",
            data: {lounge_room_id: room_id, knock_id},
            success(response: unknown): void {
                const parsed = knock_status_schema.safeParse(response);
                if (!parsed.success) {
                    return;
                }
                if (!parsed.data.admitted) {
                    setTimeout(ask, KNOCK_POLL_MS);
                    return;
                }
                // Admitted. Spend the knock immediately rather than making them
                // click again: they already said they wanted in, and the id is
                // good for one entry and a short while.
                join_as_guest({lounge_room_id: room_id, knock_id}, options);
            },
            error(): void {
                setTimeout(ask, KNOCK_POLL_MS);
            },
        });
    }

    setTimeout(ask, KNOCK_POLL_MS);
}
