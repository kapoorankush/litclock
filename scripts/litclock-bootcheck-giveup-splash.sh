#!/bin/bash
#
# Best-effort e-ink splash painted when litclock-bootcheck gives up (the
# last-known-good is also bad, or the display hardware is dead).
#
# This deliberately shares the same venv + display stack that may be broken,
# so a failure to paint is EXPECTED and non-fatal — the persistent
# /var/lib/litclock/bootcheck-gave-up marker is the reliable signal that
# recovery was exhausted. Kept as a separate one-token script so the unit's
# LITCLOCK_BOOTCHECK_SPLASH_CMD is a single word (no shell word-splitting on
# the multi-word status message).
INSTALL_DIR="${LITCLOCK_DIR:-/home/pi/litclock}"

exec "$INSTALL_DIR/venv/bin/python3" "$INSTALL_DIR/src/eink_display.py" status \
    "Recovery failed" \
    --message "The clock couldn't repair itself after an update." \
    --submessage "Please re-flash the SD card — see the LitClock docs."
