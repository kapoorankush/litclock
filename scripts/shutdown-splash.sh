#!/bin/bash
#
# Shutdown Splash Screen for LitClock
#
# Displays a farewell message on the e-ink display during shutdown or reboot.
# Since e-ink is bistable, the message persists on screen while the Pi is off.
#
# Detects reboot vs shutdown and picks an appropriate literary quote.
#

INSTALL_DIR="${LITCLOCK_DIR:-/home/pi/litclock}"
PYTHON="$INSTALL_DIR/venv/bin/python3"

# Resolve action in priority order:
#   0. /run/litclock-splash-suppress    → root-only: exit without painting (litclock-dev#529)
#   1. /etc/litclock/.welcome-mode      → gift-mode welcome (consumed by first-boot.sh)
#   2. /run/litclock/shutdown-action    → explicit hint from reset-setup.sh / future callers
#   3. systemctl list-jobs reboot.target → best-effort detection (racy when our ExecStop
#                                          fires from a mid-script `systemctl stop`
#                                          before the reboot job is enqueued — issue litclock-dev#282)
#   4. fall through                     → poweroff (final-state quote)
#
# Hint-file hardening: /run/litclock/ is pi-owned 0755 (per tmpfiles.d), so a
# pi-level process can plant a hostile hint. Defenses: (a) `! -L` rejects
# symlinks (don't read attacker-pointed targets); (b) `timeout 1 head -c 32`
# bounds read time + size (defeats FIFO-blocks-shutdown and large-file DoS);
# (c) explicit `reboot|poweroff` allowlist — anything else falls through to
# the list-jobs detection, so a spoofed "junk" hint can't suppress the
# legitimate reboot signal.
# litclock-dev#529: splash suppression. The first-boot Setup-Incomplete
# poweroff has already painted its recovery instructions and needs them to
# PERSIST on the bistable e-ink through shutdown — so it touches this marker
# (via sudo) and we exit without painting anything. The marker lives directly
# in root-owned /run, NOT in pi-owned /run/litclock/ next to the action hint,
# so that creating it requires root. Note: every shipped image carries the 010
# passwordless-sudo grant — kept deliberately, since the drop planned under
# litclock-dev#387/litclock-dev#82 was reversed 2026-07-12 — so a pi-level process can `sudo touch` it
# today. Such a process already has full root, so the marker adds no new
# exposure there; the root-owned path is the correct shape regardless (it is
# what keeps this out of the scoped 020 allowlist, where granting it WOULD hand
# pi the gift-welcome mute), and it would become a hard boundary if the posture
# were ever revisited. tmpfs, so it self-clears on the next boot; no stale
# suppression can survive.
# litclock-dev#861 — the echo below: without it a suppressed shutdown and a
# script that died on its first command leave the same (empty) journal.
if [[ -f /run/litclock-splash-suppress ]] && [[ ! -L /run/litclock-splash-suppress ]]; then
    echo "shutdown splash: suppressed by /run/litclock-splash-suppress"
    exit 0
fi

SHUTDOWN_ACTION=""
ACTION_SOURCE=""
if [[ -f /etc/litclock/.welcome-mode ]]; then
    SHUTDOWN_ACTION="welcome"
    ACTION_SOURCE="welcome-marker"
elif [[ -f /run/litclock/shutdown-action ]] && [[ ! -L /run/litclock/shutdown-action ]]; then
    raw="$(timeout 1 head -c 32 /run/litclock/shutdown-action 2>/dev/null | tr -d '[:space:]')"
    case "$raw" in
        reboot|poweroff) SHUTDOWN_ACTION="$raw"; ACTION_SOURCE="hint-file" ;;
    esac
fi
if [[ -z "$SHUTDOWN_ACTION" ]]; then
    # litclock-dev#862 — ASK AS ROOT, and say so when the question fails.
    #
    # `systemctl` talks to PID 1 over the D-Bus system bus, and falls back to
    # PID 1's private socket when the bus is gone. That socket is
    # `srwx------ root root`, so the fallback is root-only — and this script
    # runs `User=pi`. Measured on the bench 2026-09-18: ~10s into a shutdown
    # `dbus.service` is already stopped, so as `pi` every systemctl call
    # returns `Failed to connect to bus: Connection refused`, while
    # `sudo -n systemctl list-jobs` answers correctly and shows
    # `reboot.target start waiting`.
    #
    # That 10s is not hypothetical: litclock-dev#856 made this ExecStop wait
    # for the boot splash's stop job (up to its TimeoutStopSec=10s), so a
    # reboot taken during the ~11s boot-splash window landed here EVERY time
    # with no answer and painted the poweroff splash on a device that was
    # coming straight back up. Before litclock-dev#856 this ran at T+0 and worked only
    # because dbus happened to still be alive — a race, not a mechanism.
    #
    # Root first, then the unprivileged call (which still answers early in a
    # shutdown, and is the only one that works if 010_pi-nopasswd is ever
    # withdrawn), then give up LOUDLY. Both are bounded: sudo and systemctl
    # each have to be able to fail fast inside a 30s TimeoutStopSec that also
    # has to fit a ~7s paint.
    JOBS=""
    if JOBS=$(timeout 5 sudo -n systemctl list-jobs 2>/dev/null) && [[ -n "$JOBS" ]]; then
        ACTION_SOURCE="list-jobs(root)"
    elif JOBS=$(timeout 5 systemctl list-jobs 2>/dev/null) && [[ -n "$JOBS" ]]; then
        ACTION_SOURCE="list-jobs(unprivileged)"
    else
        JOBS=""
        ACTION_SOURCE="list-jobs(UNAVAILABLE)"
    fi
    # litclock-dev#861 — an empty/failed probe and a genuine poweroff used to
    # be the same `poweroff`, which is exactly why litclock-dev#862 sat unnoticed. The
    # ANSWER still defaults to poweroff (painting "Restarting…" on a device
    # that is really powering off is the worse error, and the PWA factory
    # reset relies on this arm), but the SOURCE now says which one happened.
    if [[ -n "$JOBS" ]] && grep -q "reboot\.target" <<<"$JOBS"; then
        SHUTDOWN_ACTION="reboot"
    else
        SHUTDOWN_ACTION="poweroff"
    fi
fi

# litclock-dev#861 — journald is the only diagnostic channel on this device,
# and the painted variant is otherwise unrecoverable: the next boot repaints
# over it. Name the tier too, because a wrong answer and an unasked question
# look identical on the glass.
echo "shutdown splash: action=${SHUTDOWN_ACTION} source=${ACTION_SOURCE}"

case "$SHUTDOWN_ACTION" in
    welcome)
        # Gift mode: device was prepped via `reset-setup.sh --gift-mode` and is
        # being shipped to a recipient. Paint a friendly welcome — this persists
        # on the e-ink while powered off, so it's the recipient's first impression.
        # litclock-dev#280: if the gifter wrote a personalized message via the PWA's
        # Prepare-for-Gifting flow (or via reset-setup.sh --message), it lives
        # at /etc/litclock/.welcome-message. Use it as the TITLE, falling back
        # to "Welcome to LitClock" for the default-gift case. The message is
        # bounded to 80 chars by the M3 validator + the textarea maxlength
        # (litclock-dev#319 lowered the ceiling from 280 once the renderer learned to
        # word-wrap), so it's safe to use as the title argument. Embedded
        # newlines pass through bash command substitution (only trailing
        # newlines get stripped) and the renderer honors `\n` as a hard
        # line break. Hardening:
        #   - `! -L` rejects symlinks (don't read attacker-pointed targets)
        #   - `timeout 1 head -c 100` bounds read time + size (defends against
        #     FIFO-blocks-shutdown + truncated overruns); 100 = 80 + slack
        #     for trailing whitespace/newline
        #   - `sed 's/[[:space:]]*$//'` strips trailing whitespace per line —
        #     preserves embedded newlines so a multi-line welcome survives
        WELCOME_MESSAGE_FILE="/etc/litclock/.welcome-message"
        WELCOME_TITLE=""
        if [[ -f "$WELCOME_MESSAGE_FILE" ]] && [[ ! -L "$WELCOME_MESSAGE_FILE" ]]; then
            WELCOME_TITLE="$(timeout 1 head -c 100 "$WELCOME_MESSAGE_FILE" 2>/dev/null | sed -e 's/[[:space:]]*$//')"
        fi
        # litclock-dev#532: title + instruction copy resolve from the catalog
        # (shutdown.splash.welcome.*), and so — since T7 — does the farewell
        # line, one of the device language's own shutdown.farewell.welcome.N
        # chosen by the painter. A gifter's custom message overrides the title.
        PREFIX="shutdown.splash.welcome"
        TITLE_OVERRIDE="$WELCOME_TITLE"
        FAREWELL="welcome:submessage"
        ;;
    reboot)
        # Reboot: transient state, keep LitClock branding prominent
        PREFIX="shutdown.splash.reboot"
        TITLE_OVERRIDE=""
        FAREWELL="reboot:submessage"
        ;;
    *)
        # Shutdown: final state — this persists on screen while powered off.
        # Designed to look intentional if the device is shipped or displayed.
        PREFIX="shutdown.splash.poweroff"
        TITLE_OVERRIDE=""
        FAREWELL="poweroff:message"
        ;;
esac

if [[ -f "$INSTALL_DIR/src/eink_display.py" ]]; then
    cd "$INSTALL_DIR" || exit 0
    # --catalog-prefix fills the copy; --farewell has the painter pick one of
    # the device language's farewell lines for this screen (a language ships
    # 1..N of its own, litclock-dev#532 T7); a custom gift title rides as a literal.
    SPLASH_ARGS=(--catalog-prefix "$PREFIX" --farewell "$FAREWELL")
    # Title LAST, behind the `--` end-of-options separator: the custom gift
    # title is user-typed (80 chars, PWA) and a space-free leading-dash
    # value like "-Mom-" would otherwise be parsed as an unknown OPTION —
    # argparse exits 2 and the welcome splash paints nothing (probed live;
    # pre-existing in the literal form, fixed while converting).
    [[ -n "$TITLE_OVERRIDE" ]] && SPLASH_ARGS+=(-- "$TITLE_OVERRIDE")
    # timeout matches first-boot's paint shape (Codex slice-2 /review): the
    # catalog adds file reads to a paint that runs while the system is
    # unwinding — a wedged read must not eat the whole TimeoutStopSec
    # budget and leave stale e-ink.
    rc=0
    timeout 20 $PYTHON src/eink_display.py status "${SPLASH_ARGS[@]}" || rc=$?
    # Exit 2 is argparse refusing an option before anything is painted: a
    # painter older than this script (a half-applied tree) does not know
    # --farewell. Paint the screen without the line rather than nothing —
    # this frame stays on the glass while the clock is off (litclock-dev#923 review).
    if [[ "$rc" -eq 2 ]]; then
        echo "shutdown splash: painter refused --farewell; painting without it"
        PLAIN_ARGS=(--catalog-prefix "$PREFIX")
        [[ -n "$TITLE_OVERRIDE" ]] && PLAIN_ARGS+=(-- "$TITLE_OVERRIDE")
        timeout 20 $PYTHON src/eink_display.py status "${PLAIN_ARGS[@]}" || true
    fi
fi
