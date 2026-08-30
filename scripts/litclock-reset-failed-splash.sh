#!/bin/bash
#
# Best-effort e-ink splash painted when litclock-reset.service FAILS
# (litclock-dev#665).
#
# A factory reset that fails closed is correct — litclock-dev#660 made it so — but on the
# PWA path there is nobody to tell. The route dispatches the unit with
# `systemctl start --no-block` and returns 200 immediately, and static/js/system.js
# deliberately never polls, rendering a terminal "Factory reset in progress…"
# card. So a rotation that fails on a read-only remount left the red "do NOT
# pass this device on" banner in a journal nobody reads, the card spinning
# forever, and an owner who eventually pulls the plug — after which the next
# boot raises the hotspot with the OLD key, the exact outcome the fail-closed
# guard exists to prevent.
#
# The e-ink is the one output surface that survives a frozen PWA, so that is
# where this says it.
#
# Same shape as litclock-bootcheck-giveup-splash.sh: a one-token script so the
# unit's ExecStart is a single word, sharing the venv + display stack that may
# itself be broken. A failure to paint is EXPECTED and non-fatal — the
# persistent marker written by the unit is the reliable signal.
INSTALL_DIR="${LITCLOCK_DIR:-/home/pi/litclock}"

# litclock-dev#725: lgpio creates its notify pipe (.lgd-nfy*) in the CWD. The
# unit runs User=pi, and without this cd its CWD is `/`, which pi cannot write
# — every gpiozero pin factory then falls back until display init dies with
# EINVAL and the panel keeps whatever it last showed (on the PWA reset path,
# the shutdown splash's "Powered Off", on a device that is still on). The cd
# is here rather than a unit WorkingDirectory= so a missing install dir can
# never take the unit's marker write down with it. `exit 0` because a missing
# install dir means the display stack is gone too and the marker is the
# reliable signal — but say so in the journal before going quietly.
cd "$INSTALL_DIR" || { echo "cannot cd to $INSTALL_DIR — skipping the failure splash; /var/lib/litclock/reset-failed is the signal" >&2; exit 0; }

# litclock-dev#532 slice 2: triplet from the catalog (reset.splash.failed.*);
# same doom-path degradation contract as the bootcheck splash above.
exec "$INSTALL_DIR/venv/bin/python3" "$INSTALL_DIR/src/eink_display.py" status \
    --catalog-prefix reset.splash.failed
