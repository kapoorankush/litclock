#!/bin/bash
#
# Reset LitClock Setup
#
# Reset that puts the clock back into first-boot setup mode.
#
# BY DEFAULT IT ERASES BOTH SAVED PASSWORDS (litclock-dev#666): your WiFi and
# the setup network's. That is what "factory reset" means to the person asking
# for one, and it is what the PWA card promises. Use --keep-wifi to keep both —
# the device stays on its network, never starts a setup network, and an SSH
# session survives the reset. That flag is for a technical user resetting their
# own clock; the PWA has no way to pass it.
#
# Usage: sudo ./scripts/reset-setup.sh [--yes] [--keep-wifi] [--reboot] [--gift-mode]
#
# --gift-mode prepares the device for shipping: wipes WiFi, resets config,
# writes a marker so the next shutdown-splash paints a welcome message
# (instead of "Powered Off"), and powers off. Implies --wipe-wifi --yes.
#

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

INSTALL_DIR="/home/pi/litclock"
CONFIG_DIR="/etc/litclock"
# Same override convention as the other scripts (wifi-watchdog, bootcheck,
# lkg-record, update) and as src/wifi_provision.py's STATE_DIR.
STATE_DIR="${LITCLOCK_STATE_DIR:-/var/lib/litclock}"

# Source shared state-file helpers for atomic_write_env_sh (litclock-dev#274) — the
# env.sh writer-lock that interoperates with src/config.py's fcntl.flock
# on the sidecar. Path resolved relative to this script so the sourcing
# survives a `sudo ./scripts/reset-setup.sh` invocation. state.sh ships
# in the same release as this script, so a missing file means a broken
# install — hard-fail rather than silently dropping the lock.
_THIS_SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$_THIS_SCRIPT_DIR/lib/state.sh"

# ── Function definitions — ALL of them, hoisted above every caller ──────────
# litclock-dev#719: bash resolves function names at execution time, and the
# litclock-dev#666 reordering left rotate_hotspot_password_for_handoff called 160
# lines before its definition — every default factory reset hit `command
# not found`, kept the permanent setup key, and still printed Reset
# Complete. Definitions live here, before the first executable step, so a
# future call-site move cannot recreate the class; a structural test pins
# def-before-first-call for every function in this file.

# litclock-dev#528 + litclock-dev#636: force SSH off before the device leaves the owner's
# hands, shared by BOTH handoff paths — gift mode (ships to a recipient) and
# the non-gift factory-reset poweroff (the PWA copy invites "move or pass the
# clock on"). The image ships SSH off, but an owner who enabled it (QA,
# recovery, tinkering) would otherwise hand over a device with SSH listening +
# the well-known default creds the moment it joins the next network.
# Idempotent belt-and-suspenders across every way SSH can be on:
#   - ssh.socket — Raspberry Pi OS Bookworm SOCKET-ACTIVATES sshd: pid 1
#     holds port 22 via ssh.socket and spawns sshd per-connection.
#     Disabling ssh.service alone leaves the socket listening, so the
#     socket MUST be disabled — this is the load-bearing unit on
#     current images (hardware QA 2026-07-16 caught a service-only
#     disable leaving port 22 open after reprovision). Disabled in a
#     SEPARATE call from ssh.service so a missing unit on an older
#     service-only image can't abort the other disable (/review).
#   - ssh.service — the classic always-on unit (older images).
#   - raspi-config do_ssh 1 — the canonical toggle; covers whatever the
#     image's native mechanism is.
#   - boot-partition flags — sshswitch.service turns SSH back on at boot
#     if a bare `ssh` file exists on /boot or /boot/firmware.
# Callers place this AFTER their fatal env-wipe gates: on a failed prep the
# device stays on and the owner may still need SSH to fix it (--poweroff
# implies --strict-env-wipe, so its wipe failure aborted long before here).
# Runs fine over an SSH session — pam_systemd puts interactive sessions in
# their own scope, so stopping the unit doesn't kill the invoking shell
# (and the caller's poweroff ends it anyway). Re-enable via console per
# docs/recovery.md, same as a fresh flash.
disable_ssh_for_handoff() {
    echo -n "Disabling SSH before handoff... "
    systemctl disable --now ssh.socket 2>/dev/null || true
    systemctl disable --now ssh.service 2>/dev/null || true
    raspi-config nonint do_ssh 1 2>/dev/null || true
    rm -f /boot/ssh /boot/ssh.txt /boot/firmware/ssh /boot/firmware/ssh.txt 2>/dev/null || true
    echo -e "${GREEN}done${NC}"

    # litclock-dev#528 /review: SSH-off is a security GATE, so verify port 22 is
    # actually closed rather than trusting the best-effort disables above
    # (each is `|| true`, and socket-activation means the service state
    # alone doesn't prove the port is shut). If sshd still listens, refuse
    # to power off: handing over a device with SSH + default creds
    # reachable on someone else's network is exactly what this step exists
    # to prevent. `ss` ships in iproute2 (always present on Pi OS); if it
    # can't run we can't verify, so warn and proceed rather than
    # hard-block a handoff on missing tooling.
    if command -v ss >/dev/null 2>&1; then
        # Capture ss's output AND exit status separately, rather than piping
        # `ss 2>/dev/null | grep` (litclock-dev#636 /review): a suppressed-stderr
        # pipe conflates two very different outcomes — "ss ran, port 22 is
        # closed" (empty output) and "ss itself errored" (also empty output).
        # A verification gate that reads an ss failure as "verified closed" is
        # fail-OPEN, the exact posture this gate exists to forbid. Treat an ss
        # error like an absent ss: warn and proceed (we can't verify), never
        # silently pass.
        local ss_out ss_rc
        ss_out=$(ss -H -ltn 2>/dev/null)
        ss_rc=$?
        if [[ "$ss_rc" -ne 0 ]]; then
            echo -e "${YELLOW}Note: 'ss' failed to run — could not verify port 22 is closed.${NC}"
        # Extract the local port (last colon-field of the Local Address
        # column) and match EXACTLY 22 — avoids false hits on :2222, :220…
        elif printf '%s\n' "$ss_out" | awk '{n=split($4,a,":"); print a[n]}' | grep -qx 22; then
            echo -e "${RED}========================================${NC}"
            echo -e "${RED}  SSH still listening — do NOT hand this device over${NC}"
            echo -e "${RED}========================================${NC}"
            echo -e "${RED}Port 22 is still open after disabling SSH. NOT powering off${NC}"
            echo -e "${RED}so a device with SSH + default creds isn't passed on. Check:${NC}"
            echo -e "${YELLOW}  systemctl status ssh.socket ssh.service${NC}"
            exit 1
        fi
    else
        echo -e "${YELLOW}Note: 'ss' unavailable — could not verify port 22 is closed.${NC}"
    fi
}

# litclock-dev#660 — shared handoff rotation. Both terminal paths that hand the
# device to another person must clear the persisted setup-WiFi key, and before
# litclock-dev#660 only gift mode did.
#
# SUPERSEDED BY litclock-dev#666/litclock-dev#664 (owner decision). The rule used to be
# "rotate only when the WiFi is gone AND the device is leaving", and the arms
# below encoded that pairing. It was carefully reasoned and it was still too
# clever: it meant a factory reset could leave the previous owner holding a
# working, permanent key to the next owner's setup network, depending on which
# flag combination got used.
#
# The rule now is the one a person means when they say "factory reset":
#
#     BOTH SAVED PASSWORDS GO -- the WiFi one and the setup-hotspot one.
#
# So rotation is gated on WIPE_WIFI alone, and WIPE_WIFI now defaults TRUE.
# Every terminal path -- power off, reboot, or just finish -- rotates, because
# in all three the next boot raises a setup hotspot and a stale key there is
# exactly the litclock-dev#660 leak.
#
# `--keep-wifi` is the single opt-out and it preserves BOTH, which is coherent
# rather than a special case: if the WiFi survives, the clock returns to its own
# network and never raises a hotspot at all, so there is no setup network for a
# rotated key to protect and rotating would strand the owner's phone for
# nothing. That is the old "same owner, moved house" path, now reached
# deliberately by a flag instead of by accident through a flag combination.
#
# The bench QA doc's "same owner, moved house" case still exists and still
# asserts the key is unchanged -- it is now `--keep-wifi` rather than a bare
# `--poweroff`.
#
# Fails CLOSED, matching the env.sh precedent: `rm -f` returns 0 for a missing
# file but not for a read-only remount (the Pi's most common degradation), and
# the script has no `set -e`, so an unverified delete would print "done" and
# ship the key. The temp glob catches staging files orphaned by a
# SIGKILL/power-cut between mkstemp and os.replace, each holding a real past PSK.
#
# `-L` alongside `-e` because `-e` is FALSE for a dangling symlink, so a failed
# unlink of one would report success (litclock-dev#663). The invariant is that no
# entry survives at the password path, whatever it points at. The glob half needs
# no equivalent: `compgen -G` matches NAMES, so it already sees a dangling link.
#
# rm's own diagnosis is surfaced rather than discarded: "Read-only file system"
# means the card is dying and "Operation not permitted" means ownership drift,
# and those need opposite remedies.
rotate_hotspot_password_for_handoff() {
    local _rm_err
    echo -n "Clearing the saved setup-WiFi password... "
    _rm_err=$(rm -f "$STATE_DIR/hotspot-password" "$STATE_DIR"/.hotspot-password.* 2>&1 >/dev/null) || true
    if [[ -e "$STATE_DIR/hotspot-password" || -L "$STATE_DIR/hotspot-password" ]] ||
        compgen -G "$STATE_DIR/.hotspot-password.*" >/dev/null 2>&1; then
        echo -e "${RED}FAILED${NC}"
        echo -e "${RED}========================================${NC}"
        # litclock-dev#666 /review: this used to be handoff-only, so it now fired
        # on an ordinary `sudo reset-setup.sh --reboot` telling the operator not
        # to pass on a device they were keeping. Fail-closed is right; the copy
        # has to match the path that reached it.
        if [[ "${GIFT_MODE:-false}" == "true" || "${DO_POWEROFF:-false}" == "true" ]]; then
            echo -e "${RED}  Handoff prep FAILED — do NOT pass this device on${NC}"
        else
            echo -e "${RED}  Reset FAILED — the setup password is still on this device${NC}"
        fi
        echo -e "${RED}========================================${NC}"
        echo -e "${RED}The setup network's password could not be removed from $STATE_DIR.${NC}"
        if [[ "${GIFT_MODE:-false}" == "true" || "${DO_POWEROFF:-false}" == "true" ]]; then
            echo -e "${RED}Handing the device over now would give the next owner a network${NC}"
            echo -e "${RED}key you still know.${NC}"
        else
            echo -e "${RED}The reset did not finish. Fix the cause and re-run it; the device${NC}"
            echo -e "${RED}has NOT been rebooted or powered off.${NC}"
        fi
        [[ -n "$_rm_err" ]] && echo -e "${RED}rm said: $_rm_err${NC}"
        exit 1
    fi
    echo -e "${GREEN}done${NC}"
}

AUTO_YES=false
DO_REBOOT=false
# litclock-dev#627 — power OFF after the reset instead of rebooting. The PWA
# Factory reset uses this: after a full wipe the next power-on runs first-boot
# regardless, so rebooting into a live hotspot is wrong when the owner is
# packing the clock up to move house or hand it on (a non-gift handoff). Unlike
# --gift-mode this writes NO welcome-splash marker, so the shutdown splash paints
# the plain "Powered Off" screen.
DO_POWEROFF=false
# litclock-dev#666/litclock-dev#664 (owner decision): a factory reset CLEARS BOTH SAVED
# PASSWORDS -- the WiFi one and the setup-hotspot one. That is what "factory
# reset" means to the person tapping it, and it is what the PWA card already
# promises ("Erases everything -- all settings and WiFi").
#
# So this defaults TRUE and `--keep-wifi` is the opt-out. The opt-out is a
# COMMAND-LINE-ONLY affordance: someone typing it into a shell has already
# self-identified as technical, and knows they are keeping the device on its
# network. litclock-reset.service (the PWA path) must never pass it -- a test
# pins that, because the whole point is that the tapped path cannot silently
# become the lenient one.
WIPE_WIFI=true
# Tracks whether --keep-wifi was TYPED, separately from the resulting value.
# --gift-mode also sets WIPE_WIFI, so testing the value alone made the conflict
# check order-dependent: `--keep-wifi --gift-mode` left WIPE_WIFI=true and passed,
# silently discarding a flag the operator had typed (/review).
KEEP_WIFI_REQUESTED=false
GIFT_MODE=false
# litclock-dev#510: --strict-env-wipe makes a Step 3 env.sh wipe failure FATAL *before* any
# destructive/irreversible step (WiFi wipe, reboot). Used by the PWA Factory
# reset (litclock-reset.service): a factory reset promises a clean slate, so a
# failed config wipe must abort with the device still reachable (WiFi intact) to
# retry — never wipe WiFi + reboot into a stale-config setup. Plain/dev resets
# leave it false (best-effort, unchanged).
STRICT_ENV_WIPE=false
GIFT_MESSAGE_FILE=""
# litclock-dev#532 pickers 5b: --language-file source + the validated code.
# GIFT_LANGUAGE_CODE is interpolated into the Step 3 env.sh defaults, so it
# is ONLY ever set from the shape-validated python read below (never raw).
GIFT_LANGUAGE_FILE=""
GIFT_LANGUAGE_CODE=""
# litclock-dev#393: tracks whether the Step 3 env.sh wipe failed (lock timeout / write
# error). In --gift-mode a failed wipe is fatal — see the end-of-script gift
# branch. Plain resets ignore it (best-effort).
ENV_WIPE_FAILED=false

# Parse flags. `--message-file FILE` (litclock-dev#280) lets the PWA's prepare-for-gift
# endpoint hand us a personalized welcome message to plumb into the
# shutdown splash. Reading from a file (not an inline arg) keeps the
# message out of the process list / journal and avoids quoting/escape
# hazards across the sudo boundary.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --yes|-y) AUTO_YES=true; shift ;;
        --reboot) DO_REBOOT=true; shift ;;
        # --poweroff implies --strict-env-wipe (litclock-dev#627 /review): a
        # power-off is a "clean slate then gone" signal, so a failed config wipe
        # must abort BEFORE powering off — never ship / relocate a device that
        # powered off with stale config. The factory-reset unit passes both
        # explicitly; this makes a bare manual `--poweroff` safe too.
        --poweroff) DO_POWEROFF=true; STRICT_ENV_WIPE=true; shift ;;
        # Accepted and a no-op since litclock-dev#666 made the wipe the default.
        # Kept because litclock-reset.service passes it explicitly and older
        # runbooks/docs use it; removing it would break both for no gain.
        --wipe-wifi) WIPE_WIFI=true; shift ;;
        --keep-wifi) WIPE_WIFI=false; KEEP_WIFI_REQUESTED=true; shift ;;
        --strict-env-wipe) STRICT_ENV_WIPE=true; shift ;;
        --gift-mode)
            GIFT_MODE=true
            WIPE_WIFI=true
            AUTO_YES=true
            shift
            ;;
        --message-file)
            GIFT_MESSAGE_FILE="${2:-}"
            shift 2
            ;;
        --language-file)
            # litclock-dev#532: file carrying the gifter's language code for
            # the recipient. File-not-arg for the same reason as
            # --message-file: stays out of the process list / journal.
            GIFT_LANGUAGE_FILE="${2:-}"
            shift 2
            ;;
        *)
            echo "Usage: sudo $0 [--yes] [--keep-wifi] [--reboot] [--gift-mode] [--message-file FILE]"
            echo "  --yes               Skip confirmation prompt"
            echo "  --keep-wifi         Keep saved WiFi AND the setup network's password. For a"
            echo "                      technical user resetting their own clock over SSH — the"
            echo "                      device stays on its network and never starts a setup network."
            echo "  --wipe-wifi         No-op; the wipe is the default since litclock-dev#666"
            echo "  --strict-env-wipe   Abort (before WiFi wipe / reboot) if the env.sh wipe fails (litclock-dev#510)"
            echo "  --reboot            Reboot after reset"
            echo "  --poweroff          Power off after reset (no gift splash; implies --strict-env-wipe; litclock-dev#627). Excludes --reboot"
            echo "  --gift-mode         Prepare for shipping: wipe WiFi, write welcome-splash marker, power off"
            echo "  --message-file FILE Read welcome message from FILE; persisted to /etc/litclock/.welcome-message"
            echo "                      (only meaningful with --gift-mode; litclock-dev#280)"
            echo "  --language-file FILE Read recipient language code from FILE; persisted to"
            echo "                      /etc/litclock/.gift-language and seeded into env.sh"
            echo "                      (only meaningful with --gift-mode; litclock-dev#532)"
            echo ""
            echo "  --poweroff and --gift-mode DISABLE SSH before powering down, so a device"
            echo "  being handed on gets the same posture as a fresh flash (litclock-dev#528)."
            echo "  To get back in: put a blank 'ssh' file in the SD card boot partition, or"
            echo "  use the console. See docs/recovery.md. --keep-wifi does NOT skip this."
            exit 1
            ;;
    esac
done

# Check if running as root
if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}This script must be run as root (sudo)${NC}"
    exit 1
fi

# --reboot and --poweroff are mutually exclusive (litclock-dev#627): they pick
# the terminal action, and the end-of-script branch can only do one. Fail
# closed rather than silently letting the branch order decide.
if [[ "$DO_REBOOT" == "true" && "$DO_POWEROFF" == "true" ]]; then
    echo -e "${RED}--reboot and --poweroff are mutually exclusive${NC}"
    exit 1
fi

# --gift-mode and --keep-wifi are mutually exclusive (litclock-dev#666 /review).
# Same failure shape as the pair above, and a worse consequence: --gift-mode
# sets WIPE_WIFI=true inside its own arm, so with --keep-wifi the LAST flag on
# the command line silently won.
#
#     --gift-mode --keep-wifi   ->  WIPE_WIFI=false   <-- ships the gifter's PSK
#     --keep-wifi --gift-mode   ->  WIPE_WIFI=true
#
# The first ordering skips Step 7 and hands the recipient a device carrying the
# gifter's home WiFi credentials in /etc/NetworkManager/system-connections/,
# while this script's header and the README both promise gift mode "implies
# --wipe-wifi". Before litclock-dev#666, WIPE_WIFI could only be turned ON, so
# this hazard is created by --keep-wifi and has to be closed by it.
#
# Rejected rather than re-asserted: "prepare this to hand to someone else, but
# keep my WiFi on it" is not a coherent request, and silently overriding one of
# the two flags the operator typed is how the ordering bug happens again.
if [[ "$GIFT_MODE" == "true" && "$KEEP_WIFI_REQUESTED" == "true" ]]; then
    echo -e "${RED}--gift-mode and --keep-wifi are mutually exclusive${NC}"
    echo -e "${RED}A gift is handed to someone else, so its WiFi must not go with it.${NC}"
    exit 1
fi

# Write gift-mode marker + optional welcome message early, before any
# `systemctl stop` below. Stopping litclock-shutdown.service fires its
# ExecStop (shutdown-splash.sh), which reads both files to decide between
# welcome and "Powered Off" content. Writing at end-of-script would be too
# late: the service is already inactive by then and won't re-fire ExecStop
# on the subsequent poweroff.
if [[ "$GIFT_MODE" == "true" ]]; then
    mkdir -p "$CONFIG_DIR"
    touch "$CONFIG_DIR/.welcome-mode"
    # litclock-dev#280: if --message-file is set, copy its content to .welcome-message.
    # Bounded to 80 chars (M3's GIFT_MODE_MESSAGE_MAX_LEN post-litclock-dev#319 — was
    # 280 before the renderer learned to word-wrap). Anything longer is
    # truncated rather than rejected to keep the script lenient on input.
    # If the file is missing/empty, shutdown-splash.sh falls back to the
    # "Welcome to LitClock" default — that's the explicit no-personal-note
    # path for the gifter who just wanted to ship without typing anything.
    #
    # litclock-dev#316 /review CRITICAL fix — TOCTOU symlink-swap defense. The naive
    # `[[ ! -L ... ]] && head -c 80 ...` is racy: a pi-level adversary can
    # rename(2) a symlink over $GIFT_MESSAGE_FILE between the test and the
    # read. Since this script runs as root via the litclock-prepare-for-gift
    # systemd unit, `head` would follow the symlink and copy 80 bytes of
    # /etc/shadow / /root/.ssh/... into /etc/litclock/.welcome-message, which
    # shutdown-splash.sh then paints on the e-ink (visible to physical
    # observers). Pi→root file disclosure via the display side channel.
    # Defense: open the file with O_NOFOLLOW from Python inside this same
    # privileged context — O_NOFOLLOW refuses to follow a symlink at the
    # moment of open, surviving the rename race.
    if [[ -n "$GIFT_MESSAGE_FILE" ]]; then
        # litclock-dev#387: use the SYSTEM python3, never "$INSTALL_DIR/venv/bin/python3".
        # This runs as root (via litclock-prepare-for-gift.service), and the venv
        # interpreter lives in the pi-writable repo — running it as root would let
        # pi swap the interpreter for arbitrary root code. The heredoc below is
        # stdlib-only (os + O_NOFOLLOW), so the root-owned /usr/bin/python3 works.
        if /usr/bin/python3 - "$GIFT_MESSAGE_FILE" "$CONFIG_DIR/.welcome-message" <<'PY'
import os, stat, sys
src, dst = sys.argv[1], sys.argv[2]
# O_NONBLOCK + S_ISREG: same pi-FIFO-hang defense as the language read
# below (5b security /review found the gap there; this pre-existing read
# shared it — same file, same class, fixed together).
try:
    fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
except OSError:
    sys.exit(1)  # missing or symlinked — caller falls back to default welcome
try:
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        sys.exit(1)
    # litclock-dev#319: matches GIFT_MODE_MESSAGE_MAX_LEN in src/config.py (was 280).
    data = os.read(fd, 80)
finally:
    os.close(fd)
# os.O_TRUNC for atomicity vs partial overwrite; explicit 0o644 so the file
# is operator-readable (root:root, all-read) regardless of script umask.
out_fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
try:
    os.write(out_fd, data)
finally:
    os.close(out_fd)
PY
        then
            :  # python wrote it; success
        else
            # python exited 1 → source file missing or symlinked → wipe any
            # stale .welcome-message so shutdown-splash.sh falls back to the
            # default greeting.
            rm -f "$CONFIG_DIR/.welcome-message"
        fi
    else
        # Ensure no stale .welcome-message from a previous --gift-mode run
        # leaks into this one. Explicit absence = use default text.
        rm -f "$CONFIG_DIR/.welcome-message"
    fi

    # litclock-dev#532 pickers 5b: persist the gifter's language choice to
    # the root-owned .gift-language marker, HERE (before any systemctl
    # stop) for the same write-before-stops reason as the welcome files —
    # once bash surfaces are catalog-routed, the ExecStop welcome splash
    # consults this marker. Same litclock-dev#316 O_NOFOLLOW read defense as
    # --message-file (root reading a pi-writable tmpfs path), plus a shape
    # gate (lowercase BCP-47, mirrors config._LANGUAGE_CODE_RE): the code
    # is interpolated into the Step 3 env.sh defaults below and env.sh is
    # sourced by root-run scripts, so root must never trust the PWA's raw
    # bytes. Root re-validates SHAPE only, deliberately: registry
    # membership is enforced at every read site (setup_server's
    # active-codes gate, first-boot's consumable check, the config
    # validator on any PWA write), so a raced-in shape-valid code is
    # inert, never executable. The write side uses O_NOFOLLOW too.
    # Invalid/missing source →
    # no code, and any STALE marker from a previous gift is removed so an
    # old choice can't leak into this shipment.
    if [[ -n "$GIFT_LANGUAGE_FILE" ]]; then
        # -I: isolated mode — a root-privileged parser must not honor
        # PYTHON* env vars or user site-packages (Codex 5b /review).
        if GIFT_LANGUAGE_CODE=$(/usr/bin/python3 -I - "$GIFT_LANGUAGE_FILE" "$CONFIG_DIR/.gift-language" <<'PY'
import os, re, stat, sys
src, dst = sys.argv[1], sys.argv[2]
# O_NONBLOCK alongside O_NOFOLLOW (the repo's TOCTOU-close rule, and the
# 5b security /review): without it, a pi-placed FIFO at this pi-owned
# tmpfs path blocks root's open until the unit's 60s timeout — a
# pi->root gift-prep DoS. The S_ISREG fstat then rejects any non-regular
# file the nonblocking open let through. Matches first-boot.sh's marker
# read posture.
try:
    fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
except OSError:
    sys.exit(1)  # missing or symlinked — no language choice this gift
try:
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        sys.exit(1)
    code = os.read(fd, 64).decode("utf-8", errors="replace").strip()
finally:
    os.close(fd)
# Shape gate BEFORE any write: mirrors config._LANGUAGE_CODE_RE. Exit 1 on
# anything else so the caller clears stale state instead of persisting junk.
if not re.fullmatch(r"[a-z][a-z0-9]{0,7}(-[a-z0-9]{1,8}){0,2}", code):
    sys.exit(1)
out_fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
try:
    # mode arg applies only on CREATE — fchmod corrects drift on a
    # pre-existing file (Codex 5b /review; /etc/litclock is root-owned so
    # ownership is root's already).
    os.fchmod(out_fd, 0o644)
    os.write(out_fd, code.encode("ascii"))
finally:
    os.close(out_fd)
print(code)
PY
        ); then
            :  # marker written; GIFT_LANGUAGE_CODE seeds env.sh in Step 3
        else
            GIFT_LANGUAGE_CODE=""
            rm -f "$CONFIG_DIR/.gift-language"
        fi
    else
        rm -f "$CONFIG_DIR/.gift-language"
    fi
fi

echo "========================================"
echo "  Reset LitClock Setup"
echo "========================================"
echo ""
echo "This will:"
echo "  - Clear configuration (API key, location)"
echo "  - Re-enable first-boot setup service"
echo "  - Stop the clock timer"
if [[ "$WIPE_WIFI" == "true" ]]; then
    echo -e "  - ${RED}Delete saved WiFi networks${NC}"
    echo -e "  - ${RED}Delete the setup network's password${NC} (a new one is made next setup)"
fi
echo ""
# litclock-dev#666: name BOTH passwords. The old copy mentioned only WiFi, so
# the one credential a previous owner could keep was the one the prompt never
# said anything about.
if [[ "$WIPE_WIFI" == "true" ]]; then
    echo -e "${YELLOW}Both saved passwords WILL be erased — your WiFi and the setup network's.${NC}"
    echo -e "${YELLOW}Next boot brings up the LitClock-Setup network with a NEW password,${NC}"
    echo -e "${YELLOW}shown on the clock's screen. Any phone that saved the old one must re-read it.${NC}"
    echo -e "Pass ${GREEN}--keep-wifi${NC} to keep both and stay on this network."
else
    echo -e "${GREEN}--keep-wifi: your WiFi and the setup network's password are both kept.${NC}"
    echo -e "The clock returns to its own network and never starts a setup network."
fi
echo ""

if [[ "$AUTO_YES" != "true" ]]; then
    read -p "Continue? (y/N) " -n 1 -r
    echo ""
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Cancelled."
        exit 0
    fi
fi

echo ""

# litclock-dev#532 pickers 5b, trap (c): a NON-gift reset clears any stale
# .gift-language left by an abandoned gift setup (gift reset → never
# provisioned → later factory reset by a new owner) — otherwise the next
# first-boot's picker silently defaults to the old gifter's language.
# Placed AFTER the confirmation (a cancelled reset must change nothing)
# but BEFORE the service stops (Codex 5b /review P3: once bash surfaces
# are catalog-routed, the ExecStop splash consults the marker — a plain
# reset's splash must not paint in the abandoned gift's language). Gift
# resets manage the marker in their own arm above (overwrite-or-remove).
if [[ "$GIFT_MODE" != "true" ]]; then
    rm -f "$CONFIG_DIR/.gift-language"
fi

# Issue litclock-dev#282: tell shutdown-splash.sh we're rebooting, not powering off.
# The `systemctl stop litclock-shutdown.service` below fires ExecStop
# (shutdown-splash.sh) BEFORE the actual `systemctl reboot` at end-of-script
# enqueues reboot.target, so splash's list-jobs detection comes up empty and
# falls through to "Powered Off". The hint file steers it.
#
# Atomic write via root-owned /run/ tmpfile + `mv -T` (rename(2)). A direct
# `>` redirect into pi-owned /run/litclock/ would follow attacker-planted
# symlinks and let pi-level processes coerce root into truncating arbitrary
# files (/etc/passwd, /etc/sudoers, …). /run/ is root:root 0755 so pi cannot
# pre-plant the tmp path; rename() replaces the destination atomically
# without traversing any pre-existing symlink there.
#
# Hint write is gated behind the y/N prompt and an EXIT trap so user-cancel
# or mid-script abort doesn't leave a stale "reboot" hint that misleads a
# later unrelated shutdown.
# litclock-dev#718 + litclock-dev#727: clear any stale splash-suppress marker from an
# earlier same-boot plain-arm run FIRST, so this run's arm decides afresh —
# a stale marker outranks even the gift welcome in shutdown-splash.sh's
# ladder and would mute the splash the re-armed stop edge below is about
# to paint. `rm -f` removes the link itself, never a symlink target.
rm -f /run/litclock-splash-suppress 2>/dev/null || true
if [[ "$DO_REBOOT" == "true" ]]; then
    trap 'rm -f /run/litclock/shutdown-action 2>/dev/null' EXIT
    if HINT_TMP=$(mktemp -p /run .litclock-hint.XXXXXX 2>/dev/null); then
        printf 'reboot\n' > "$HINT_TMP"
        chmod 0644 "$HINT_TMP"
        mv -T -- "$HINT_TMP" /run/litclock/shutdown-action 2>/dev/null \
            || rm -f -- "$HINT_TMP" 2>/dev/null
    fi
elif [[ "$DO_POWEROFF" == "true" ]]; then
    # litclock-dev#627 — actively clear any stale "reboot" hint so
    # shutdown-splash paints "Powered Off", not "Restarting…", on a
    # factory-reset power-off. A prior manual `--reboot` run SIGTERM'd before
    # its EXIT trap fired (e.g. by the Conflicts=litclock-update.service kill)
    # could leave one this boot. `rm -f` removes the link itself, never a
    # symlink target, so this is safe against a pi-planted symlink.
    rm -f /run/litclock/shutdown-action 2>/dev/null || true
elif [[ "$GIFT_MODE" != "true" ]]; then
    # litclock-dev#718 (owner decision 2026-08-23: the PWA path must never
    # see a false "Powered Off"; it always takes --poweroff, where the words
    # come true moments later — this arm is CLI-only). A plain run — no
    # --reboot, no --poweroff, not gift — stops services and hands control
    # back to an operator at a console. Painting "Powered Off" on a live
    # device invites pulling power at the exact moment the closing text asks
    # for a reboot. Suppress the splash instead: the last-painted content
    # persists on the bistable panel and the console carries the
    # instruction. Root-owned /run path (the litclock-dev#529 contract;
    # root-only creation, ! -L checked by the reader); tmpfs, so it
    # self-clears on the next boot. Gift mode is excluded because the
    # suppress marker outranks the welcome splash in the reader's ladder.
    # EXIT-trap belt (/review litclock-dev#731): an interrupt between this touch and the
    # stop edge below would otherwise leave the marker muting every later
    # splash this boot while the unit is still armed. Same residual SIGKILL
    # window as the reboot hint's trap; /run is tmpfs so power-loss clears it.
    trap 'rm -f /run/litclock-splash-suppress 2>/dev/null' EXIT
    touch /run/litclock-splash-suppress 2>/dev/null || true
fi

# Step 1: Stop all litclock services that may be running or stuck.
#
# litclock-dev#274: stop litclock-control.service BEFORE the env.sh rewrite below so
# the PWA cannot land a Settings save concurrent with our defaults
# overwrite. Even though atomic_write_env_sh serializes against the
# Python writer via flock, dropping the contention surface to "shell
# writer only" eliminates the 30s lock-wait window on a stuck PWA save
# and makes the user-visible behavior deterministic.
# litclock-dev#665: clear any PREVIOUS failure marker before starting, so the
# file means "the most recent reset attempt failed" rather than "a reset failed
# once, ever". Without this it is a stale marker of exactly the litclock-dev#672 class —
# nothing would ever clear it, and a device that failed once and succeeded on
# the retry would keep warning its owner not to pass it on, forever. The
# OnFailure unit writes it again if THIS attempt fails.
rm -f "$STATE_DIR/reset-failed" 2>/dev/null || true
# Verify, don't assume (litclock-dev#673's lesson). A directory, a symlink, or a
# read-only remount leaves the marker in place and `rm -f` still returns 0. This
# WARNS rather than aborting: a stale warning marker is the fail-safe direction
# — it tells an owner not to pass on a device that is actually fine — whereas
# aborting the reset over bookkeeping would strand them with no reset at all.
if [ -e "$STATE_DIR/reset-failed" ] || [ -L "$STATE_DIR/reset-failed" ]; then
    echo "WARNING: could not clear $STATE_DIR/reset-failed; this device may keep reporting a failed reset."
fi

echo -n "Stopping litclock services... "
systemctl stop litclock.timer 2>/dev/null || true
systemctl stop litclock.service 2>/dev/null || true
systemctl stop litclock-control.service 2>/dev/null || true
systemctl stop litclock-firstboot.service 2>/dev/null || true
systemctl stop litclock-splash.service 2>/dev/null || true
# /review litclock-dev#731: a same-boot retry can land while the litclock-dev#725
# failure painter (OnFailure of the PREVIOUS attempt) is still painting —
# two eink processes would race on SPI, and if the retry's painter loses,
# the panel keeps "Do NOT pass it on" through a successful reset: the
# exact litclock-dev#727 outcome through a different door. Stop it first;
# the re-armed edge below repaints the correct content moments later.
systemctl stop litclock-reset-failed.service 2>/dev/null || true
# litclock-dev#727: the shutdown/welcome splash is the STOP edge of a
# RemainAfterExit=yes oneshot, and that edge exists once per boot — an
# earlier failed reset already spent it, so a same-boot retry painted
# nothing and e-ink persistence carried the failure splash ("Do NOT pass
# it on") through a SUCCESSFUL reset's poweroff — the gift-retry case
# ships the box with its scariest message. Re-arm before stopping:
# `start` on the inactive unit runs ExecStart (/bin/true) and marks it
# active again; on an already-active unit it is a no-op, so the first run
# is unchanged. DELIBERATELY BLOCKING — the usual "--no-block from inside
# a service" rule does not apply and would break the fix: a queued
# (unstarted) start job is simply REPLACED by the stop on the next line,
# so the unit never goes active and the retry paints nothing again.
# There is no job cycle to deadlock on (litclock-shutdown orders only
# against shutdown targets and litclock.service); `timeout 10` is the
# belt if that analysis is ever wrong, and a swallowed failure would
# recreate the no-paint retry — so it WARNS instead of hiding.
timeout 10 systemctl start litclock-shutdown.service 2>/dev/null \
    || echo "WARNING: could not re-arm the shutdown splash; this run may not paint its final screen."
systemctl stop litclock-shutdown.service 2>/dev/null || true
# The stop edge above has consumed the suppress decision — clear the
# marker NOW so it cannot mute anything else this boot (/review litclock-dev#731;
# the first-boot.sh principle: the marker protects the ONE stop just
# requested, never the rest of the boot). No-op on arms that never wrote it.
rm -f /run/litclock-splash-suppress 2>/dev/null || true
# litclock-dev#676 made the handoff fallback RECURRING, so it is now a live
# writer of .handoff-complete rather than a one-shot that fired long ago.
# The marker removal order below (.setup-complete first) already closes the
# window where both of its conditions hold, but a recurring marker writer
# belongs in this list on its own merits — litclock-dev#673's lesson was to stop the
# writers first and verify, not to rely on an ordering staying correct.
systemctl stop litclock-handoff-fallback.timer 2>/dev/null || true
systemctl stop litclock-handoff-fallback.service 2>/dev/null || true
# Kill any lingering setup server or clock processes
pkill -f setup_server.py 2>/dev/null || true
pkill -f literary_clock.py 2>/dev/null || true
echo -e "${GREEN}done${NC}"

# Step 2: Remove setup-complete flag
echo -n "Removing setup-complete flag... "
rm -f "$CONFIG_DIR/.setup-complete"
# EPIC litclock-dev#383 PR2 (litclock-dev#388): clear the handoff marker too. The handoff phase is
# active when .setup-complete exists AND .handoff-complete is absent, so a
# lingering .handoff-complete would skip the post-WiFi splash when this device
# re-provisions. Cleared on every reset (gift or plain) since both return the
# device to a fresh-setup state. (litclock-wifi-reset.sh clears it too, for the
# same reason — a WiFi change can mean a new timezone.)
rm -f "$CONFIG_DIR/.handoff-complete"
echo -e "${GREEN}done${NC}"

# Step 3: Clear env.sh to defaults via atomic_write_env_sh (litclock-dev#274) — the
# shared sidecar flock interlocks with src/config.py's atomic_update
# from the PWA. On lock timeout (rc=75) or any other write failure,
# warn + continue: reset-setup is best-effort across many steps and
# aborting halfway leaves the device in a worse state than a config
# we re-write on next boot.
echo -n "Resetting configuration... "
if [[ -f "$INSTALL_DIR/env.sh" ]]; then
    # litclock-dev#337 A3: WEATHER_LOCATION_MODE + WEATHER_IP_COUNTRY belong here so a
    # gift-recipient whose first-boot IP-geo fails (network issue, blocked
    # ip-api) lands on MODE=auto rather than inheriting the gifter's
    # MODE=specific from a stale env.sh write — the on-boot reresolve
    # service would then no-op forever and the recipient would stay stuck
    # with no location until they manually visited the PWA.
    # litclock-dev#532 pickers 5b: belt directly at the interpolation
    # point (Codex 5b /review: the $() capture is trusted stdout — a future
    # stray print in the python step must not reach a root-written env.sh).
    # Same alphabet as the python shape gate; anything else empties the
    # seed rather than shipping it.
    if [[ -n "$GIFT_LANGUAGE_CODE" && ! "$GIFT_LANGUAGE_CODE" =~ ^[a-z][a-z0-9-]{0,16}$ ]]; then
        echo -e "${YELLOW}gift language code failed the final shape check; seeding empty${NC}"
        GIFT_LANGUAGE_CODE=""
    fi
    # In --gift-mode the validated language
    # code (shape-gated [a-z0-9-] in the gift arm above — safe to
    # interpolate) seeds LITCLOCK_LANGUAGE so EVERY env-reading surface on
    # the recipient's device boots in the gifter's chosen language. Plain
    # resets leave it empty, which keeps Accept-Language negotiation alive
    # on the next first-boot (the litclock-dev#743 empty-seed contract).
    DEFAULTS="export OPENWEATHERMAP_APIKEY=
export WEATHER_LATITUDE=
export WEATHER_LONGITUDE=
export WEATHER_LOCATION_NAME=
export WEATHER_UNITS=imperial
export WEATHER_LOCATION_MODE=auto
export WEATHER_IP_COUNTRY=
export WEATHER_TTL=3600
export ALLOW_NSFW_QUOTES=false
export LITCLOCK_LANGUAGE=$GIFT_LANGUAGE_CODE
"
    if atomic_write_env_sh "$INSTALL_DIR/env.sh" "$DEFAULTS"; then
        echo -e "${GREEN}done${NC}"
    else
        _rc=$?
        # litclock-dev#393: record the failure so --gift-mode can abort before poweroff.
        # A surviving WEATHER_LATITUDE/LONGITUDE leaks the gifter's location and
        # can pass PR2's handoff "tz known" proxy → wrong-time clock for the
        # recipient. Plain resets stay best-effort and ignore this flag.
        ENV_WIPE_FAILED=true
        if [[ "$_rc" == "75" ]]; then
            echo -e "${YELLOW}skipped (env.sh locked by another writer)${NC}"
        else
            echo -e "${YELLOW}failed (rc=$_rc) — env.sh untouched${NC}"
        fi
        unset _rc
    fi
else
    echo -e "${GREEN}done${NC}"
fi

# litclock-dev#510: fail-closed for the PWA Factory reset. A factory reset promises a clean
# slate; if the config wipe failed, do NOT proceed to the destructive/irreversible
# steps (WiFi wipe, reboot) — that would leave the owner rebooted into a setup
# with stale settings, believing everything was erased. Abort here (before Step 7
# WiFi wipe + the end-of-script reboot) with WiFi still up so the PWA can report
# the failure and the owner can retry. Only --strict-env-wipe callers hit this;
# plain/dev resets stay best-effort. (Gift mode has its own abort-before-poweroff
# guard below and never sets --strict-env-wipe.)
if [[ "$STRICT_ENV_WIPE" == "true" && "$ENV_WIPE_FAILED" == "true" ]]; then
    echo -e "${RED}Factory reset aborted: could not wipe env.sh (config left intact, WiFi untouched)." >&2
    echo -e "Retry the reset once the device is idle.${NC}" >&2
    exit 1
fi

# Step 3.5 (gift mode only): reset the system timezone to UTC (litclock-dev#389).
# The timezone is system state (timedatectl / /etc/localtime), NOT env.sh, so
# the Step 3 config wipe doesn't touch it — a gifted device would otherwise
# boot showing the GIFTER's timezone until the recipient's first-boot IP-geo
# resolves theirs, leaking the gifter's location. UTC is the neutral default;
# the recipient's tz is set by the EPIC litclock-dev#383 first-boot IP-geo (or the PR2
# browser-tz handoff fallback). Best-effort, like the rest of this script —
# timedatectl can be absent/unavailable in odd environments; a warning beats
# aborting the gift prep. Scoped to gift mode: a plain reset of your own device
# has no privacy reason to forget your timezone.
if [[ "$GIFT_MODE" == "true" ]]; then
    echo -n "Resetting timezone to UTC... "
    if command -v timedatectl >/dev/null 2>&1 && timedatectl set-timezone UTC 2>/dev/null; then
        echo -e "${GREEN}done${NC}"
    else
        echo -e "${YELLOW}skipped (timedatectl unavailable)${NC}"
    fi
fi

# Step 4: Re-enable first-boot service
echo -n "Enabling first-boot service... "
systemctl enable litclock-firstboot.service 2>/dev/null || true
echo -e "${GREEN}done${NC}"

# Step 5: Clear SSL certificates (legacy; nothing regenerates these since litclock-dev#715)
echo -n "Clearing legacy SSL certificates... "
rm -rf "$INSTALL_DIR/.certs" 2>/dev/null || true
echo -e "${GREEN}done${NC}"

# Step 6: Clear signal file and logs
echo -n "Clearing signal file... "
rm -f /tmp/litclock-setup-done 2>/dev/null || true
echo -e "${GREEN}done${NC}"

# Step 6.5: Clear weather cache. Stale cache from a prior session with
# different units would be served under the new unit label — bug caught
# during issue litclock-dev#175 QA on 2026-04-11. The provider code also sweeps orphans
# now, but clearing here is belt-and-suspenders for any path that bypasses
# provider construction (e.g. a cloned SD card at first boot).
# litclock-dev#434 moved the live cache to /run/litclock (tmpfs); clear both the tmpfs
# copy (survives a no-reboot reset) and any legacy SD-resident file.
echo -n "Clearing weather cache... "
rm -f "$INSTALL_DIR"/weather-cache*.json /run/litclock/weather-cache*.json 2>/dev/null || true
echo -e "${GREEN}done${NC}"

# litclock-dev#666/litclock-dev#664 — rotate on EVERY non-gift path when the WiFi is going,
# not just the power-off one. A reset that reboots, or one that just finishes and
# leaves the operator to power-cycle, raises the same setup network on the next
# boot; gating on the power-off was what let a stale key survive a reset that
# took a different exit. Gift mode rotates in its own arm at the end,
# deliberately after the litclock-dev#393 abort gate.
#
# PLACED BEFORE STEP 7, AND THE ORDER IS LOAD-BEARING (/review). Step 7 deletes
# the WiFi keyfiles, which is the one step that can take the script's own
# network with it. On the new default path an operator runs this over SSH; if
# the session dies during the wipe the script is SIGHUP'd, and with the rotation
# after Step 7 that leaves WiFi gone AND the old setup key intact -- exactly the
# litclock-dev#660 leak, silently, on the path litclock-dev#666 just made the
# default. Rotating first is a local unlink with no network dependency, so it
# cannot be interrupted by the thing it precedes.
#
# Still after the litclock-dev#510 gate directly above, so a failed env.sh wipe aborts
# BEFORE anything irreversible: no path rotates and then aborts.
if [[ "$GIFT_MODE" != "true" && "$WIPE_WIFI" == "true" ]]; then
    # litclock-dev#719 — the call's OWN status is checked (not just the file
    # state below): with no password file on disk, a `command not found`
    # would leave nothing to survive and the outcome belt alone would pass.
    # The function's genuine failure path exits the script itself, so a
    # nonzero here means the call never ran.
    if ! rotate_hotspot_password_for_handoff; then
        # Copy branches on the path, like the function's own failure banner:
        # a plain --reboot reset is not a handoff (/review litclock-dev#720 round 3).
        if [[ "${GIFT_MODE:-false}" == "true" || "${DO_POWEROFF:-false}" == "true" ]]; then
            echo -e "${RED}The rotation FAILED to run — do NOT hand this device on (litclock-dev#719).${NC}"
        else
            echo -e "${RED}The rotation FAILED to run — the setup password is still on this device (litclock-dev#719).${NC}"
        fi
        exit 1
    fi
    # litclock-dev#719 — belt for the call above: it once failed as `command
    # not found` (definition then sat 160 lines below) and the reset still
    # printed Reset Complete. The function's own fail-closed check cannot run
    # if the function never does, so the OUTCOME is verified at the site: no
    # entry may survive at the password path once a wipe-wifi reset passes
    # this point.
    if [[ -e "$STATE_DIR/hotspot-password" || -L "$STATE_DIR/hotspot-password" ]] ||
        compgen -G "$STATE_DIR/.hotspot-password.*" >/dev/null 2>&1; then
        if [[ "${GIFT_MODE:-false}" == "true" || "${DO_POWEROFF:-false}" == "true" ]]; then
            echo -e "${RED}The setup-WiFi password SURVIVED the reset — do NOT hand this device on.${NC}"
        else
            echo -e "${RED}The setup-WiFi password SURVIVED the reset — it is still on this device.${NC}"
        fi
        echo -e "${RED}The rotation did not run or did not take effect (litclock-dev#719).${NC}"
        exit 1
    fi
fi

# Step 7: Optionally wipe saved WiFi networks for fresh-flash simulation.
# Only deletes WiFi-type NetworkManager connection profiles — wired
# ethernet, VPN (OpenVPN/WireGuard), bluetooth PAN, etc. live in the same
# directory and must NOT be touched. A power user with a USB ethernet
# dongle for debugging would otherwise lose their wired fallback on every
# --wipe-wifi run.
if [[ "$WIPE_WIFI" == "true" ]]; then
    echo -n "Wiping saved WiFi networks... "
    NM_DIR=/etc/NetworkManager/system-connections
    if [[ -d "$NM_DIR" ]]; then
        # Connection profiles are keyfile-format .nmconnection files. Match
        # ones that declare type=wifi in the [connection] section.
        shopt -s nullglob
        for conn in "$NM_DIR"/*.nmconnection "$NM_DIR"/*; do
            [[ -f "$conn" ]] || continue
            if grep -qE '^type=wifi$' "$conn" 2>/dev/null; then
                rm -f "$conn"
            fi
        done
        shopt -u nullglob
    fi
    # Legacy wpa_supplicant — reset to bare country config
    if [[ -f /etc/wpa_supplicant/wpa_supplicant.conf ]]; then
        cat > /etc/wpa_supplicant/wpa_supplicant.conf << 'EOF'
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=US
EOF
    fi
    echo -e "${GREEN}done${NC}"
    if [[ "$DO_REBOOT" != "true" && "$DO_POWEROFF" != "true" ]]; then
        echo -e "${YELLOW}Note: WiFi is wiped but NetworkManager is still holding the active${NC}"
        echo -e "${YELLOW}      connection in memory. Reboot (or add --reboot) to actually drop it.${NC}"
    fi
fi

echo ""
echo "========================================"
echo -e "${GREEN}  Reset Complete!${NC}"
echo "========================================"
echo ""

if [[ "$GIFT_MODE" == "true" ]]; then
    if [[ "$ENV_WIPE_FAILED" == "true" ]]; then
        # litclock-dev#393: the env.sh wipe is the load-bearing privacy step for a gift —
        # it clears the gifter's WEATHER_LATITUDE/LONGITUDE/LOCATION_NAME. It
        # failed (lock timeout rc=75 or a write error), so stale coordinates may
        # still be in env.sh. If we shipped the device and the recipient's
        # first-boot IP-geo then hard-failed, PR2's handoff treats the leftover
        # latitude as "timezone known" and starts quotes at the GIFTER's old
        # time. Powering off is the "ready to ship" signal, so a failed wipe is
        # FATAL in gift mode: refuse to power off, surface the error, exit
        # non-zero. The device stays on (showing the welcome splash already
        # painted at the Step 1 service stop) — re-run gift prep once the
        # contending env.sh writer releases the lock. Plain non-gift resets
        # never reach here; they keep best-effort behavior.
        echo -e "${RED}========================================${NC}"
        echo -e "${RED}  Gift prep FAILED — do NOT ship this device${NC}"
        echo -e "${RED}========================================${NC}"
        echo -e "${RED}env.sh could not be reset to defaults, so it may still hold your${NC}"
        echo -e "${RED}location. NOT powering off so a stale device isn't shipped.${NC}"
        echo -e "${YELLOW}Re-run once nothing else is writing env.sh:${NC}"
        # 5b adversarial /review F2: retry via the UNIT, not a bare
        # --gift-mode — the bare form has no --message-file/--language-file,
        # so its else-arms would rm the .welcome-message and .gift-language
        # this failed run already wrote, destroying the choices the retry
        # exists to ship. The unit re-reads the still-staged tmpfs files.
        echo -e "${YELLOW}  sudo systemctl start litclock-prepare-for-gift.service${NC}"
        exit 1
    fi
    # litclock-dev#620 / litclock-dev#660 — clear the persisted setup-WiFi key
    # before the device leaves this owner. Deliberately AFTER the litclock-dev#393 abort
    # gate: a gift prep that fails leaves the device with its CURRENT owner, and
    # rotating there would drop that owner into the very trap litclock-dev#620 removes.
    if ! rotate_hotspot_password_for_handoff; then
        echo -e "${RED}The rotation FAILED to run — do NOT hand this device on (litclock-dev#719).${NC}"
        exit 1
    fi
    # litclock-dev#719 — same call-site belt as the non-gift site above.
    if [[ -e "$STATE_DIR/hotspot-password" || -L "$STATE_DIR/hotspot-password" ]] ||
        compgen -G "$STATE_DIR/.hotspot-password.*" >/dev/null 2>&1; then
        echo -e "${RED}The setup-WiFi password SURVIVED the reset — do NOT hand this device on.${NC}"
        echo -e "${RED}The rotation did not run or did not take effect (litclock-dev#719).${NC}"
        exit 1
    fi

    # litclock-dev#528 — shared handoff gate; see disable_ssh_for_handoff above.
    # Deliberately AFTER the env-wipe-failed gate: on a failed prep the device
    # stays on and the owner may still need SSH to fix it. Also AFTER the
    # rotation, for the same reason — that block fails CLOSED and can exit 1,
    # which leaves the device with its CURRENT owner, and disabling SSH first
    # would strip that owner's remote access on the exact path where they still
    # need it to recover. SSH-off is the last thing before poweroff.
    disable_ssh_for_handoff

    # Marker was written earlier (pre-stop) so shutdown-splash has already
    # painted the welcome screen by now. Just power off.
    echo "Gift mode: powering off."
    echo "On next power-on, recipient will see the welcome splash and first-boot setup."
    poweroff
elif [[ "$DO_POWEROFF" == "true" ]]; then
    # litclock-dev#627 — non-gift factory reset. No reboot-hint was written
    # above, so shutdown-splash paints the plain "Powered Off" screen (no
    # welcome message — this is a relocation / non-gift handoff, not a gift).
    # The --strict-env-wipe guard (used by litclock-reset.service) has already
    # aborted before here if the config wipe failed, so a clean slate is
    # guaranteed by the time we power off. On next power-on the wiped config
    # makes first-boot run into the setup hotspot.
    #
    # litclock-dev#666 — the setup password was already rotated above, on every
    # non-gift path where the WiFi is going. This arm no longer decides that;
    # it only picks the terminal action.
    # Rotation already happened above, on every non-gift path (litclock-dev#666).
    #
    # litclock-dev#528 — the same handoff gate as gift mode. litclock-dev#627 made this
    # the moving/handing-on reset, so the device leaves this owner here just as
    # surely, and the copy above it says so. Last thing before poweroff.
    disable_ssh_for_handoff
    echo "Powering off. Power the clock on again to set it up fresh."
    poweroff
elif [[ "$DO_REBOOT" == "true" ]]; then
    echo "Rebooting now..."
    # Use `systemctl reboot` directly (bare `/sbin/reboot` forwards to it
    # on Bookworm anyway). Cleaner systemd integration; not a race fix.
    systemctl reboot
else
    echo "Reboot to enter setup mode:"
    echo "  sudo reboot"
fi
