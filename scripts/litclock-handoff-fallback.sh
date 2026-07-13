#!/bin/bash
# LitClock handoff last-resort completer (EPIC #383 PR2, #388, task T21).
#
# Driven by litclock-handoff-fallback.timer ~10 min after boot. If
# control_server crashed during the post-WiFi handoff window and never wrote
# /etc/litclock/.handoff-complete, this rescues the clock from a stuck splash.
#
# Design-review A2 guard: complete the handoff ONLY when the timezone is known.
# A populated WEATHER_LATITUDE means the IP-geo resolver succeeded and set the
# system timezone, so quotes will render at the correct time. If IP-geo failed
# (latitude empty), the timezone is unknown — leave the splash up rather than
# start a wrong-time clock (a wrong-time clock is worse than no clock). The
# user completes the handoff from the PWA (browser-tz fallback) instead.
#
# The unit's ConditionPathExists gates already ensure this only runs when setup
# finished AND the handoff hasn't, so this script just re-checks the tz gate.
set -u

CONFIG_DIR="${LITCLOCK_CONFIG_DIR:-/etc/litclock}"
ENV_FILE="${LITCLOCK_ENV_FILE:-/home/pi/litclock/env.sh}"
HANDOFF_FLAG="$CONFIG_DIR/.handoff-complete"

# tz-known proxy: read WEATHER_LATITUDE from env.sh and check it's non-empty.
# Strip the `export KEY=` prefix and any surrounding quotes/whitespace.
lat_line="$(grep -E '^[[:space:]]*(export[[:space:]]+)?WEATHER_LATITUDE=' "$ENV_FILE" 2>/dev/null | tail -n1)"
lat_val="${lat_line#*=}"
lat_val="${lat_val//\"/}"
lat_val="${lat_val//\'/}"
lat_val="${lat_val//[[:space:]]/}"

if [[ -z "$lat_val" ]]; then
    echo "handoff-fallback: timezone unknown (WEATHER_LATITUDE empty) — leaving splash up"
    exit 0
fi

if touch "$HANDOFF_FLAG" 2>/dev/null; then
    echo "handoff-fallback: wrote $HANDOFF_FLAG (control_server did not complete the handoff in time)"
else
    echo "handoff-fallback: could not write $HANDOFF_FLAG" >&2
    exit 1
fi
