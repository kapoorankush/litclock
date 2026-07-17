#!/bin/bash
#
# Reset LitClock Setup
#
# Lightweight reset that puts the clock back into first-boot setup mode.
# By default, preserves WiFi so you stay connected. Use --wipe-wifi for a
# full fresh-flash simulation (no WiFi, no env, no setup marker) — useful
# for iterating on first-boot UX without rebuilding the image.
#
# Usage: sudo ./scripts/reset-setup.sh [--yes] [--wipe-wifi] [--reboot] [--gift-mode]
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

# Source shared state-file helpers for atomic_write_env_sh (#274) — the
# env.sh writer-lock that interoperates with src/config.py's fcntl.flock
# on the sidecar. Path resolved relative to this script so the sourcing
# survives a `sudo ./scripts/reset-setup.sh` invocation. state.sh ships
# in the same release as this script, so a missing file means a broken
# install — hard-fail rather than silently dropping the lock.
_THIS_SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$_THIS_SCRIPT_DIR/lib/state.sh"
AUTO_YES=false
DO_REBOOT=false
WIPE_WIFI=false
GIFT_MODE=false
# #510: --strict-env-wipe makes a Step 3 env.sh wipe failure FATAL *before* any
# destructive/irreversible step (WiFi wipe, reboot). Used by the PWA Factory
# reset (litclock-reset.service): a factory reset promises a clean slate, so a
# failed config wipe must abort with the device still reachable (WiFi intact) to
# retry — never wipe WiFi + reboot into a stale-config setup. Plain/dev resets
# leave it false (best-effort, unchanged).
STRICT_ENV_WIPE=false
GIFT_MESSAGE_FILE=""
# #393: tracks whether the Step 3 env.sh wipe failed (lock timeout / write
# error). In --gift-mode a failed wipe is fatal — see the end-of-script gift
# branch. Plain resets ignore it (best-effort).
ENV_WIPE_FAILED=false

# Parse flags. `--message-file FILE` (#280) lets the PWA's prepare-for-gift
# endpoint hand us a personalized welcome message to plumb into the
# shutdown splash. Reading from a file (not an inline arg) keeps the
# message out of the process list / journal and avoids quoting/escape
# hazards across the sudo boundary.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --yes|-y) AUTO_YES=true; shift ;;
        --reboot) DO_REBOOT=true; shift ;;
        --wipe-wifi) WIPE_WIFI=true; shift ;;
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
        *)
            echo "Usage: sudo $0 [--yes] [--wipe-wifi] [--reboot] [--gift-mode] [--message-file FILE]"
            echo "  --yes               Skip confirmation prompt"
            echo "  --wipe-wifi         Also delete saved WiFi networks (full fresh-flash simulation)"
            echo "  --strict-env-wipe   Abort (before WiFi wipe / reboot) if the env.sh wipe fails (#510)"
            echo "  --reboot            Reboot after reset"
            echo "  --gift-mode         Prepare for shipping: wipe WiFi, write welcome-splash marker, power off"
            echo "  --message-file FILE Read welcome message from FILE; persisted to /etc/litclock/.welcome-message"
            echo "                      (only meaningful with --gift-mode; #280)"
            exit 1
            ;;
    esac
done

# Check if running as root
if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}This script must be run as root (sudo)${NC}"
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
    # #280: if --message-file is set, copy its content to .welcome-message.
    # Bounded to 80 chars (M3's GIFT_MODE_MESSAGE_MAX_LEN post-#319 — was
    # 280 before the renderer learned to word-wrap). Anything longer is
    # truncated rather than rejected to keep the script lenient on input.
    # If the file is missing/empty, shutdown-splash.sh falls back to the
    # "Welcome to LitClock" default — that's the explicit no-personal-note
    # path for the gifter who just wanted to ship without typing anything.
    #
    # #316 /review CRITICAL fix — TOCTOU symlink-swap defense. The naive
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
        # #387: use the SYSTEM python3, never "$INSTALL_DIR/venv/bin/python3".
        # This runs as root (via litclock-prepare-for-gift.service), and the venv
        # interpreter lives in the pi-writable repo — running it as root would let
        # pi swap the interpreter for arbitrary root code. The heredoc below is
        # stdlib-only (os + O_NOFOLLOW), so the root-owned /usr/bin/python3 works.
        if /usr/bin/python3 - "$GIFT_MESSAGE_FILE" "$CONFIG_DIR/.welcome-message" <<'PY'
import os, sys
src, dst = sys.argv[1], sys.argv[2]
try:
    fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
except OSError:
    sys.exit(1)  # missing or symlinked — caller falls back to default welcome
try:
    # #319: matches GIFT_MODE_MESSAGE_MAX_LEN in src/config.py (was 280).
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
fi
echo ""
if [[ "$WIPE_WIFI" == "true" ]]; then
    echo -e "${YELLOW}WiFi WILL be wiped — next boot will create the hotspot.${NC}"
else
    echo -e "${GREEN}WiFi credentials will be preserved.${NC}"
    echo -e "Pass ${YELLOW}--wipe-wifi${NC} for a full fresh-flash simulation."
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

# Issue #282: tell shutdown-splash.sh we're rebooting, not powering off.
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
if [[ "$DO_REBOOT" == "true" ]]; then
    trap 'rm -f /run/litclock/shutdown-action 2>/dev/null' EXIT
    if HINT_TMP=$(mktemp -p /run .litclock-hint.XXXXXX 2>/dev/null); then
        printf 'reboot\n' > "$HINT_TMP"
        chmod 0644 "$HINT_TMP"
        mv -T -- "$HINT_TMP" /run/litclock/shutdown-action 2>/dev/null \
            || rm -f -- "$HINT_TMP" 2>/dev/null
    fi
fi

# Step 1: Stop all litclock services that may be running or stuck.
#
# #274: stop litclock-control.service BEFORE the env.sh rewrite below so
# the PWA cannot land a Settings save concurrent with our defaults
# overwrite. Even though atomic_write_env_sh serializes against the
# Python writer via flock, dropping the contention surface to "shell
# writer only" eliminates the 30s lock-wait window on a stuck PWA save
# and makes the user-visible behavior deterministic.
echo -n "Stopping litclock services... "
systemctl stop litclock.timer 2>/dev/null || true
systemctl stop litclock.service 2>/dev/null || true
systemctl stop litclock-control.service 2>/dev/null || true
systemctl stop litclock-firstboot.service 2>/dev/null || true
systemctl stop litclock-splash.service 2>/dev/null || true
systemctl stop litclock-shutdown.service 2>/dev/null || true
# Kill any lingering setup server or clock processes
pkill -f setup_server.py 2>/dev/null || true
pkill -f literary_clock.py 2>/dev/null || true
echo -e "${GREEN}done${NC}"

# Step 2: Remove setup-complete flag
echo -n "Removing setup-complete flag... "
rm -f "$CONFIG_DIR/.setup-complete"
# EPIC #383 PR2 (#388): clear the handoff marker too. The handoff phase is
# active when .setup-complete exists AND .handoff-complete is absent, so a
# lingering .handoff-complete would skip the post-WiFi splash when this device
# re-provisions. Cleared on every reset (gift or plain) since both return the
# device to a fresh-setup state. (litclock-wifi-reset.sh clears it too, for the
# same reason — a WiFi change can mean a new timezone.)
rm -f "$CONFIG_DIR/.handoff-complete"
echo -e "${GREEN}done${NC}"

# Step 3: Clear env.sh to defaults via atomic_write_env_sh (#274) — the
# shared sidecar flock interlocks with src/config.py's atomic_update
# from the PWA. On lock timeout (rc=75) or any other write failure,
# warn + continue: reset-setup is best-effort across many steps and
# aborting halfway leaves the device in a worse state than a config
# we re-write on next boot.
echo -n "Resetting configuration... "
if [[ -f "$INSTALL_DIR/env.sh" ]]; then
    # #337 A3: WEATHER_LOCATION_MODE + WEATHER_IP_COUNTRY belong here so a
    # gift-recipient whose first-boot IP-geo fails (network issue, blocked
    # ip-api) lands on MODE=auto rather than inheriting the gifter's
    # MODE=specific from a stale env.sh write — the on-boot reresolve
    # service would then no-op forever and the recipient would stay stuck
    # with no location until they manually visited the PWA.
    DEFAULTS='export OPENWEATHERMAP_APIKEY=
export WEATHER_LATITUDE=
export WEATHER_LONGITUDE=
export WEATHER_LOCATION_NAME=
export WEATHER_UNITS=imperial
export WEATHER_LOCATION_MODE=auto
export WEATHER_IP_COUNTRY=
export WEATHER_TTL=3600
export ALLOW_NSFW_QUOTES=false
'
    if atomic_write_env_sh "$INSTALL_DIR/env.sh" "$DEFAULTS"; then
        echo -e "${GREEN}done${NC}"
    else
        _rc=$?
        # #393: record the failure so --gift-mode can abort before poweroff.
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

# #510: fail-closed for the PWA Factory reset. A factory reset promises a clean
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

# Step 3.5 (gift mode only): reset the system timezone to UTC (#389).
# The timezone is system state (timedatectl / /etc/localtime), NOT env.sh, so
# the Step 3 config wipe doesn't touch it — a gifted device would otherwise
# boot showing the GIFTER's timezone until the recipient's first-boot IP-geo
# resolves theirs, leaking the gifter's location. UTC is the neutral default;
# the recipient's tz is set by the EPIC #383 first-boot IP-geo (or the PR2
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

# Step 5: Clear SSL certificates (will be regenerated on next boot)
echo -n "Clearing SSL certificates... "
rm -rf "$INSTALL_DIR/.certs" 2>/dev/null || true
echo -e "${GREEN}done${NC}"

# Step 6: Clear signal file and logs
echo -n "Clearing signal file... "
rm -f /tmp/litclock-setup-done 2>/dev/null || true
echo -e "${GREEN}done${NC}"

# Step 6.5: Clear weather cache. Stale cache from a prior session with
# different units would be served under the new unit label — bug caught
# during issue #175 QA on 2026-04-11. The provider code also sweeps orphans
# now, but clearing here is belt-and-suspenders for any path that bypasses
# provider construction (e.g. a cloned SD card at first boot).
# #434 moved the live cache to /run/litclock (tmpfs); clear both the tmpfs
# copy (survives a no-reboot reset) and any legacy SD-resident file.
echo -n "Clearing weather cache... "
rm -f "$INSTALL_DIR"/weather-cache*.json /run/litclock/weather-cache*.json 2>/dev/null || true
echo -e "${GREEN}done${NC}"

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
    if [[ "$DO_REBOOT" != "true" ]]; then
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
        # #393: the env.sh wipe is the load-bearing privacy step for a gift —
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
        echo -e "${YELLOW}  sudo $0 --gift-mode${NC}"
        exit 1
    fi
    # #528: force SSH off before shipping. The image ships SSH off, but an
    # owner who enabled it (QA, recovery, tinkering) would otherwise hand
    # the recipient a device with SSH listening + the well-known default
    # creds the moment it joins THEIR network. Idempotent belt-and-
    # suspenders across every way SSH can be on:
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
    # Deliberately AFTER the env-wipe-failed gate above: on a failed prep
    # the device stays on and the owner may still need SSH to fix it. Runs
    # even over an SSH session — pam_systemd puts interactive sessions in
    # their own scope, so stopping the unit doesn't kill the invoking shell
    # (and the poweroff below ends it anyway). Recipient re-enables via
    # console per docs/recovery.md, same as a fresh flash.
    echo -n "Disabling SSH for shipping... "
    systemctl disable --now ssh.socket 2>/dev/null || true
    systemctl disable --now ssh.service 2>/dev/null || true
    raspi-config nonint do_ssh 1 2>/dev/null || true
    rm -f /boot/ssh /boot/ssh.txt /boot/firmware/ssh /boot/firmware/ssh.txt 2>/dev/null || true
    echo -e "${GREEN}done${NC}"

    # #528 /review: SSH-off is a security GATE, so verify port 22 is
    # actually closed rather than trusting the best-effort disables above
    # (each is `|| true`, and socket-activation means the service state
    # alone doesn't prove the port is shut). If sshd still listens, refuse
    # to power off — same posture as the env-wipe failure below: shipping a
    # device with SSH + default creds reachable on the recipient's network
    # is exactly what this step exists to prevent. `ss` ships in iproute2
    # (always present on Pi OS); if it can't run we can't verify, so warn
    # and proceed rather than hard-block a gift on missing tooling.
    if command -v ss >/dev/null 2>&1; then
        # Extract the local port (last colon-field of the Local Address
        # column) and match EXACTLY 22 — avoids false hits on :2222, :220…
        if ss -H -ltn 2>/dev/null | awk '{n=split($4,a,":"); print a[n]}' | grep -qx 22; then
            echo -e "${RED}========================================${NC}"
            echo -e "${RED}  SSH still listening — do NOT ship this device${NC}"
            echo -e "${RED}========================================${NC}"
            echo -e "${RED}Port 22 is still open after disabling SSH. NOT powering off${NC}"
            echo -e "${RED}so a device with SSH + default creds isn't shipped. Check:${NC}"
            echo -e "${YELLOW}  systemctl status ssh.socket ssh.service${NC}"
            exit 1
        fi
    else
        echo -e "${YELLOW}Note: 'ss' unavailable — could not verify port 22 is closed.${NC}"
    fi

    # Marker was written earlier (pre-stop) so shutdown-splash has already
    # painted the welcome screen by now. Just power off.
    echo "Gift mode: powering off."
    echo "On next power-on, recipient will see the welcome splash and first-boot setup."
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
