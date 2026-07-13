#!/bin/bash
#
# Boot Splash Screen for LitClock
#
# Paints a "Starting..." status frame on the e-ink. The actual current-quote
# render is triggered by ExecStartPost in litclock-splash.service so systemd's
# job queue serializes it against timer-fired litclock.service runs (avoids
# the SPI/GPIO contention that produced lgpio "GPIO busy" errors before
# issue #269 was fixed).
#

INSTALL_DIR="${LITCLOCK_DIR:-/home/pi/litclock}"
PYTHON="$INSTALL_DIR/venv/bin/python3"

if [[ -f "$INSTALL_DIR/src/eink_display.py" ]]; then
    cd "$INSTALL_DIR" || return
    timeout 20 "$PYTHON" src/eink_display.py status "LitClock" --message "Starting..." || true
fi
