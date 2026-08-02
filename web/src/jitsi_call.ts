import $ from "jquery";

import * as channel from "./channel";
import * as narrow_state from "./narrow_state";

function open_call(): void {
    const stream_id = narrow_state.stream_id();
    const pm_ids = narrow_state.pm_ids();

    let data: {stream_id: number} | {user_ids: string};
    if (stream_id !== undefined) {
        data = {stream_id};
    } else if (pm_ids !== undefined && pm_ids.length > 0) {
        data = {user_ids: JSON.stringify(pm_ids)};
    } else {
        return;
    }

    void channel.post({
        url: "/json/calls/jitsi/create",
        data,
        success(raw_data) {
            const {url} = raw_data as {url: string};
            window.open(url, "_blank", "noopener,noreferrer");
        },
    });
}

export function initialize(): void {
    $("body").on("click", ".jitsi-call-button", (e) => {
        e.preventDefault();
        e.stopPropagation();
        open_call();
    });
}
TSEOF
