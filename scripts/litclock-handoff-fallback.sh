#!/bin/bash
# LitClock handoff last-resort completer (EPIC litclock-dev#383 PR2, litclock-dev#388, task T21).
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

# tz-known proxy: read the coordinates from env.sh and check they're non-empty.
# Strip the `export KEY=` prefix and any surrounding quotes/whitespace.
#
# BOTH keys, matching control_server/handoff.py's _has_location. This copy
# checked latitude alone, so an env.sh with a latitude and no longitude was
# "timezone unknown" to the PWA and "timezone known" here — two writers of the
# same marker disagreeing about the one predicate that gates it (/review).
#
# The trailing-comment strip is load-bearing: `WEATHER_LATITUDE= # unset` is
# ordinary shell, and without it the comment text became the value and read as
# a populated coordinate, so a device with NO location completed the handoff.
read_env_value() {
    local key="$1" line value
    line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$ENV_FILE" 2>/dev/null | tail -n1)"
    value="${line#*=}"
    value="${value%%#*}"
    value="${value//\"/}"
    value="${value//\'/}"
    value="${value//[[:space:]]/}"
    printf '%s' "$value"
}

lat_val="$(read_env_value WEATHER_LATITUDE)"
lon_val="$(read_env_value WEATHER_LONGITUDE)"

if [[ -z "$lat_val" || -z "$lon_val" ]]; then
    echo "handoff-fallback: timezone unknown (WEATHER_LATITUDE/LONGITUDE empty) — leaving splash up"
    exit 0
fi

# The `handoff: completed via ...` phrasing is deliberate and must match
# control_server/handoff.py's TRIGGER_* lines verbatim (litclock-dev#646). This script is
# a sixth completion path, and it is the one that runs precisely when
# control_server is broken — so an operator grepping the canonical string on a
# rescued Pi previously got NOTHING and would conclude the handoff never
# completed. That is the same wrong-conclusion class litclock-dev#646 exists to kill.
# tests/test_control_server_handoff.py asserts the two spellings agree.
if touch "$HANDOFF_FLAG" 2>/dev/null; then
    echo "handoff: completed via the last-resort fallback timer (control_server did not complete the handoff in time)"
    echo "handoff-fallback: wrote $HANDOFF_FLAG"
else
    echo "handoff-fallback: could not write $HANDOFF_FLAG" >&2
    exit 1
fi
