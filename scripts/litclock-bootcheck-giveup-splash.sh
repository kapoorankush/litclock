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

# litclock-dev#532 slice 2: the triplet resolves from the catalog
# (bootcheck.splash.gave_up.*). Degradation on this doom path: a missing
# catalog serves visible key names, a missing strings_catalog MODULE is
# caught inside eink_display (painting key names) — the paint dies only if
# the display stack itself is broken, exactly as before.
exec "$INSTALL_DIR/venv/bin/python3" "$INSTALL_DIR/src/eink_display.py" status \
    --catalog-prefix bootcheck.splash.gave_up
