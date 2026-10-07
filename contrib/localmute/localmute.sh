#!/usr/bin/env bash
# Stop hearing Waydroid locally while the bot keeps streaming it.
#
# Retargets Waydroid's stream at BotSink, so WirePlumber drops the link to your
# headphones. The bot's BotSink link is untouched. "off" clears the target so
# the stream follows your default output again.
#
# Usage: localmute.sh [on|off|toggle|status]   (default: toggle)
set -euo pipefail

WAYDROID_NODE="${WAYDROID_NODE:-Waydroid}"
BOT_SINK_NODE="${BOT_SINK_NODE:-BotSink}"

# Prints "<sink-input index> <node id>" for Waydroid's stream.
waydroid_stream() {
    # pactl warns on stderr about non-ASCII stream titles; harmless.
    pactl -f json list sink-inputs 2>/dev/null | python3 -c '
import json, sys
for s in json.load(sys.stdin):
    if s["properties"].get("node.name") == sys.argv[1]:
        print(s["index"], s["properties"]["object.id"])
        break
' "$WAYDROID_NODE"
}

# Sinks Waydroid's left channel is linked to, other than BotSink. pactl can't
# tell us this: it reports the bot's parallel link as the stream's sink.
# Non-sink consumers (e.g. OBS app-audio capture) aren't local playback.
local_outputs() {
    pw-link -l | awk -v src="$WAYDROID_NODE:output_FL" -v bot="$BOT_SINK_NODE" \
        -v sinks="$(pactl list short sinks | cut -f2)" '
        BEGIN { n = split(sinks, s, "\n"); for (i = 1; i <= n; i++) is_sink[s[i]] = 1 }
        !/^ / { in_block = ($0 == src); next }
        in_block && /\|->/ {
            sub(/^ *\|-> /, ""); split($0, a, ":")
            if (is_sink[a[1]] && a[1] != bot) print a[1]
        }'
}

notify() {
    echo "$1"
    command -v notify-send >/dev/null && notify-send -a musicbot -t 1500 "$1" || true
}

read -r idx node < <(waydroid_stream) || { notify "No Waydroid stream (nothing playing?)"; exit 1; }
outputs="$(local_outputs)"
muted=$([[ -z "$outputs" ]] && echo 1 || echo 0)

action="${1:-toggle}"
if [[ "$action" == toggle ]]; then
    action=$([[ $muted == 1 ]] && echo off || echo on)
fi

case "$action" in
    on)
        # Not pactl move-sink-input: pactl already reports the stream on BotSink
        # (the bot's parallel link), so the move is a no-op. Set the target
        # directly; pulse sink indices are PipeWire object serials.
        bot_serial="$(pactl list short sinks | awk -v n="$BOT_SINK_NODE" '$2 == n { print $1 }')"
        pw-metadata "$node" target.object "$bot_serial" >/dev/null
        notify "Local audio muted — bot still streaming"
        ;;
    off)
        # Drop the explicit target so it follows the default sink again.
        pw-metadata -d "$node" target.object >/dev/null 2>&1 || true
        pw-metadata -d "$node" target.node >/dev/null 2>&1 || true
        sleep 0.3
        if [[ -z "$(local_outputs)" ]]; then
            pactl move-sink-input "$idx" @DEFAULT_SINK@
        fi
        notify "Local audio on"
        ;;
    status)
        [[ $muted == 1 ]] && echo "muted" || echo "on (-> ${outputs//$'\n'/, })"
        ;;
    *)
        echo "usage: $0 [on|off|toggle|status]" >&2; exit 2
        ;;
esac
