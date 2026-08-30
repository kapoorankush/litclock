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
# so that creating it requires root. Note: on images carrying the 010
# passwordless-sudo grant (all current images), a pi-level process can still
# `sudo touch` it — but such a process already has full root, so the marker
# adds no new exposure there; the root-owned path only becomes a real boundary
# once 010 is dropped. tmpfs, so it self-clears on the next boot; no stale
# suppression can survive.
if [[ -f /run/litclock-splash-suppress ]] && [[ ! -L /run/litclock-splash-suppress ]]; then
    exit 0
fi

SHUTDOWN_ACTION=""
if [[ -f /etc/litclock/.welcome-mode ]]; then
    SHUTDOWN_ACTION="welcome"
elif [[ -f /run/litclock/shutdown-action ]] && [[ ! -L /run/litclock/shutdown-action ]]; then
    raw="$(timeout 1 head -c 32 /run/litclock/shutdown-action 2>/dev/null | tr -d '[:space:]')"
    case "$raw" in
        reboot|poweroff) SHUTDOWN_ACTION="$raw" ;;
    esac
fi
if [[ -z "$SHUTDOWN_ACTION" ]]; then
    if systemctl list-jobs 2>/dev/null | grep -q "reboot.target"; then
        SHUTDOWN_ACTION="reboot"
    else
        SHUTDOWN_ACTION="poweroff"
    fi
fi

case "$SHUTDOWN_ACTION" in
    welcome)
        # Gift mode: device was prepped via `reset-setup.sh --gift-mode` and is
        # being shipped to a recipient. Paint a friendly welcome — this persists
        # on the e-ink while powered off, so it's the recipient's first impression.
        QUOTES=(
            '"It was the best of times..." — Dickens'
            '"Call me Ishmael." — Melville'
            '"All happy families are alike." — Tolstoy'
            '"It was a bright cold day in April." — Orwell'
            '"Many years later, as he faced the firing squad..." — García Márquez'
        )
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
        # litclock-dev#532 slice 2: title + instruction copy resolve from
        # the catalog (shutdown.splash.welcome.*); a gifter's custom
        # message overrides the title, and the farewell quote stays a
        # curated literal (per-language sets are a registry-schema
        # question — the scope audit's item 2 — not a translation).
        PREFIX="shutdown.splash.welcome"
        TITLE_OVERRIDE="$WELCOME_TITLE"
        MESSAGE_OVERRIDE=""
        SUBMESSAGE_OVERRIDE="${QUOTES[$((RANDOM % ${#QUOTES[@]}))]}"
        ;;
    reboot)
        # Reboot: transient state, keep LitClock branding prominent
        QUOTES=(
            '"To sleep, perchance to dream." — Shakespeare'
            '"I have promises to keep." — Robert Frost'
            '"Not all those who wander are lost." — Tolkien'
            '"Be back before the next chapter begins."'
            '"And miles to go before I sleep." — Robert Frost'
        )
        PREFIX="shutdown.splash.reboot"
        TITLE_OVERRIDE=""
        MESSAGE_OVERRIDE=""
        SUBMESSAGE_OVERRIDE="${QUOTES[$((RANDOM % ${#QUOTES[@]}))]}"
        ;;
    *)
        # Shutdown: final state — this persists on screen while powered off.
        # Designed to look intentional if the device is shipped or displayed.
        QUOTES=(
            '"The rest is silence." — Shakespeare'
            '"And so it goes." — Kurt Vonnegut'
            '"After all, tomorrow is another day." — Margaret Mitchell'
            '"So we beat on, boats against the current." — Fitzgerald'
            '"A far, far better rest that I go to." — Dickens'
        )
        PREFIX="shutdown.splash.poweroff"
        TITLE_OVERRIDE=""
        MESSAGE_OVERRIDE="${QUOTES[$((RANDOM % ${#QUOTES[@]}))]}"
        SUBMESSAGE_OVERRIDE=""
        ;;
esac

if [[ -f "$INSTALL_DIR/src/eink_display.py" ]]; then
    cd "$INSTALL_DIR" || exit 0
    # --catalog-prefix fills the parts not explicitly overridden; the quote
    # (and a custom gift title) ride as explicit literals.
    SPLASH_ARGS=(--catalog-prefix "$PREFIX")
    [[ -n "$MESSAGE_OVERRIDE" ]] && SPLASH_ARGS+=(--message "$MESSAGE_OVERRIDE")
    [[ -n "$SUBMESSAGE_OVERRIDE" ]] && SPLASH_ARGS+=(--submessage "$SUBMESSAGE_OVERRIDE")
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
    timeout 20 $PYTHON src/eink_display.py status "${SPLASH_ARGS[@]}" || true
fi
