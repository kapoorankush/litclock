#!/bin/bash
#
# Boot Splash Screen for LitClock
#
# Paints a "Starting..." status frame on the e-ink. The actual current-quote
# render is triggered by ExecStartPost in litclock-splash.service so systemd's
# job queue serializes it against timer-fired litclock.service runs (avoids
# the SPI/GPIO contention that produced lgpio "GPIO busy" errors before
# issue litclock-dev#269 was fixed).
#

INSTALL_DIR="${LITCLOCK_DIR:-/home/pi/litclock}"
PYTHON="$INSTALL_DIR/venv/bin/python3"

if [[ -f "$INSTALL_DIR/src/eink_display.py" ]]; then
    # `return` at top level is a bash error that CONTINUES execution — it
    # guarded nothing (litclock-dev#725 review). exit 0 actually skips the paint.
    cd "$INSTALL_DIR" || exit 0
    # litclock-dev#532 bulk extraction: triplet resolves from the language
    # catalog inside the same spawn (boot.splash.starting.*).
    timeout 20 "$PYTHON" src/eink_display.py status --catalog-prefix boot.splash.starting || true
fi
