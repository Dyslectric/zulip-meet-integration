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
import {page_params} from "./page_params.ts";

const call_response_schema = z.object({url: z.string()});
const error_response_schema = z.object({msg: z.string()});

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
    target: Record<string, number>,
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

    if (remembered_name !== "") {
        mint(remembered_name);
        return;
    }

    dialog_widget.launch({
        modal_title_html: $t_html({defaultMessage: "Join the call"}),
        modal_content_html: render_guest_call_name(),
        id: "guest_call_name",
        modal_submit_button_text: $t({defaultMessage: "Join"}),
        on_click() {
            // Blank is allowed and becomes a plain "Guest". Refusing to let
            // somebody in until they name themselves would be asking for an
            // identity on a channel whose whole point is not requiring one.
            mint(($<HTMLInputElement>("#guest_call_name_input").val() ?? "").trim());
            dialog_widget.close();
        },
        on_shown: () => $("#guest_call_name_input").trigger("focus"),
    });
}
