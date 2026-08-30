#!/bin/bash
#
# Prepare LitClock SD Card for Cloning
#
# Run this script after you have a fully working LitClock setup.
# It will reset the configuration so the card can be cloned and given
# to friends/family who will go through their own first-boot setup.
#
# Usage: sudo ./scripts/prepare-for-cloning.sh
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# litclock-dev#660 — this script POWERS THE PI OFF when it finishes, so that no
# boot can happen between preparing the card and imaging it.
#
# Why that is load-bearing rather than a convenience: Step 8 deletes the
# persisted setup-WiFi key, but this script also (re-)enables
# litclock-firstboot.service and Step 1 removes .setup-complete — which is
# correct, because a CLONED card must run first-boot. The consequence is that
# booting the PREPARED MASTER even once runs create_hotspot() ->
# _load_or_create_hotspot_password(), which mints and fsyncs a fresh PERMANENT
# key straight back into $STATE_DIR. That key then rides every clone, which is
# exactly what Step 8 exists to prevent. The regression is silent: by then this
# script has already printed "done" and "SD Card Ready for Cloning!".
#
# An accidental reboot is not the only way in. litclock-dev#659 records that the
# prepared card's intended end state is indistinguishable from a brick (frozen
# panel, port 80 refused), so power-cycling it "to see if it is alive" is a
# realistic operator move — and that is the exact action that resurrects the key.
#
# --no-poweroff opts out for testing and CI. It prints a loud warning instead,
# because with it the hazard above is live again.
POWEROFF_WHEN_DONE=true
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-poweroff) POWEROFF_WHEN_DONE=false; shift ;;
        *)
            echo "Usage: sudo $0 [--no-poweroff]"
            echo "  --no-poweroff   Leave the Pi running when done. The card must NOT be"
            echo "                  booted again before imaging (litclock-dev#660)."
            echo ""
            echo "The default (poweroff) run DISABLES SSH before powering off, so clones"
            echo "ship in fresh-flash posture (#57); over SSH your session drops at"
            echo "that step — expected. --no-poweroff keeps SSH for inspection, and the"
            echo "imaged card then carries this master's SSH posture."
            exit 1
            ;;
    esac
done

# Check if running as root
if [[ $EUID -ne 0 ]]; then
   echo -e "${RED}This script must be run as root (sudo)${NC}"
   exit 1
fi

INSTALL_DIR="/home/pi/litclock"
CONFIG_DIR="/etc/litclock"
# Same override convention as reset-setup.sh and src/wifi_provision.py.
STATE_DIR="${LITCLOCK_STATE_DIR:-/var/lib/litclock}"
# Defined HERE, not at first use. Step 3 (the optional WiFi wipe) runs long
# before Step 8, and this script has no `set -u` — so a definition placed at
# Step 8 would leave Step 3 expanding an empty string into `rm -f /*`, as root.
# The ordering is the whole safety property.
#
# NOT environment-overridable, deliberately (/review, security). STATE_DIR can
# be, because it only ever has fixed filenames appended — a wrong value there
# removes two named paths. This one feeds an unbounded `rm -f "$DIR"/*` running
# as root, so a wrong value wipes a whole directory. The tests inject the
# variable straight into the lifted span, so an override bought nothing.
_NM_PROFILE_DIR="/etc/NetworkManager/system-connections"
# Same rationale, and additionally: the test harness EXECUTES the lifted wipe
# branch, and a hardcoded /etc path inside an executed span would overwrite the
# host's real wpa_supplicant.conf when the suite runs as root (/review litclock-dev#710).
_WPA_SUPPLICANT_CONF="/etc/wpa_supplicant/wpa_supplicant.conf"

# Source shared state-file helpers for atomic_write_env_sh (litclock-dev#274) — the
# env.sh writer-lock that interoperates with src/config.py's fcntl.flock
# on the sidecar. state.sh ships in the same release as this script, so
# a missing file means a broken install — hard-fail rather than silently
# dropping the lock.
_THIS_SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$_THIS_SCRIPT_DIR/lib/state.sh"

# litclock-dev#701 — the opt-in WiFi wipe in Step 3 deletes EVERY NetworkManager
# connection, wired included, so an operator running this over any NM-managed
# link loses that link mid-loop and SIGHUP kills the script AT STEP 3 — before
# Step 8 removes the setup-hotspot key this script exists to remove. No banner
# prints, and a dropped session is also what a normal reboot-y script looks
# like (CLAUDE.md documents exactly that for reset-setup.sh --wipe-wifi), so
# the half-prepared card gets imaged anyway and every clone ships the key.
#
# Detection is two-layered because sudo's env_reset strips SSH_CONNECTION and
# SSH_TTY by default: keep the env check (it survives `sudo -E` and env_keep
# setups), then walk the process ancestry for the SSH daemon (`sshd`, or
# `sshd-session` on OpenSSH >= 9.8) or mosh. A local or serial console login
# has neither — and those are exactly the sessions that survive the wipe.
# Returns: 0 = network session (wipe would SIGHUP this script);
#          1 = confirmed local (the walk reached init with no SSH daemon);
#          2 = could not determine (ps unusable, or the walk did not finish).
# 2 exists because "ps failed" must not read as "local" — the wipe is refused
# on 0 AND 2, and only a walk that actually completed earns a 1 (/review litclock-dev#710:
# the first draft's `|| break` turned any ps hiccup into fail-open).
#
# tmux/screen panes, nohup and systemd-run ancestries deliberately read as
# LOCAL: their processes are reparented away from sshd, and that is exactly
# why they SURVIVE the wipe — the property this function tests is "will the
# wipe kill this script", not "is a human somewhere on SSH". A tmux pane whose
# SSH client drops keeps running to completion, banner and all.
_is_network_session() {
    if [[ -n "${SSH_CONNECTION:-}" || -n "${SSH_TTY:-}" ]]; then
        return 0
    fi
    # (No separate `command -v ps` fast-path: a missing ps fails the first
    # `ps` call below into the same `|| return 2` — one guard, not two.)
    local _pid _comm _hops
    _pid=$$
    _hops=0
    while [[ -n "$_pid" && "$_pid" != "1" && "$_pid" != "0" ]]; do
        # Hop cap: pid-table weirdness (a cycle from pid reuse, a runaway
        # fake) must end in "could not determine", never in an infinite loop
        # or a silent "local".
        _hops=$((_hops + 1))
        if (( _hops > 64 )); then
            return 2
        fi
        _comm=$(ps -o comm= -p "$_pid" 2>/dev/null) || return 2
        case "$_comm" in
            sshd*|mosh-server*|dropbear*) return 0 ;;
        esac
        # No pipeline here (/review litclock-dev#710 round 2): `ps | tr` reports TR's
        # status, so a mid-walk ps failure — an ancestor exiting between the
        # comm read and this one, an ordinary race — slipped through as an
        # empty pid and the loop exited into "confirmed local". Capture, then
        # require a numeric answer; anything else is "could not determine".
        _pid=$(ps -o ppid= -p "$_pid" 2>/dev/null) || return 2
        _pid=${_pid//[[:space:]]/}
        [[ "$_pid" =~ ^[0-9]+$ ]] || return 2
    done
    # The walk finished at init: this really is a local/serial login.
    return 1
}

# #57 / litclock-dev#657 — the SSH handoff gate, for the highest-fanout
# handoff path there is: docs/sd-card-cloning.md is "Creating SD Cards for
# Friends & Family", and whatever SSH posture the master holds when this
# script powers it off is frozen into the image and every clone of it. The
# operator preparing masters typically does so over SSH — precisely the state
# that must not ride the card. The function BODY below is byte-identical to
# reset-setup.sh's copy (and so to public's), pinned against
# tests/fixtures/disable_ssh_for_handoff.golden; only this header differs,
# because the rationale above reset-setup's copy is specific to its two arms.
# Refresh path on deliberate change: tests/fixtures/refresh_ssh_gate_golden.py
# regenerates the golden from reset-setup.sh; this copy must then match it.
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

# litclock-dev#701 — a run that dies half-way is indistinguishable from one
# that finished, because the failure the operator sees (a dropped session, a
# dead terminal) is also what success looks like from a phone. The marker is
# written before the first mutation and removed after the last, so the NEXT
# run can say "the previous run did not finish" instead of relying on the
# operator to notice a banner that never printed.
_UNFINISHED_MARKER="$STATE_DIR/clone-prep-unfinished"

echo "========================================"
echo "  Prepare LitClock for Cloning"
echo "========================================"
echo ""
echo -e "${YELLOW}WARNING: This will reset the clock configuration!${NC}"
echo "The SD card will be ready to clone for distribution."
echo ""
# litclock-dev#701 — say both things BEFORE the confirm, while stopping is free.
# rc captured with `|| _rc=$?`: a bare call returning 1/2 under `set -e`
# outside a condition would kill the script here.
_NET_SESSION_RC=0
_is_network_session || _NET_SESSION_RC=$?
if [[ "$_NET_SESSION_RC" == "0" ]]; then
    echo -e "${YELLOW}You are connected over the network (SSH). The optional WiFi wipe will${NC}"
    echo -e "${YELLOW}be REFUSED from here: deleting the connections would drop this session${NC}"
    echo -e "${YELLOW}and kill the script before it removes the setup-WiFi key${NC}"
    echo -e "${YELLOW}(litclock-dev#701). To clear WiFi, run from the local console.${NC}"
    echo ""
elif [[ "$_NET_SESSION_RC" == "2" ]]; then
    echo -e "${YELLOW}Could not determine whether this is a network session (ps unusable).${NC}"
    echo -e "${YELLOW}The optional WiFi wipe will be refused to be safe (litclock-dev#701).${NC}"
    echo ""
fi
if [[ -e "$_UNFINISHED_MARKER" || -L "$_UNFINISHED_MARKER" ]]; then
    echo -e "${YELLOW}A previous run of this script did NOT finish (litclock-dev#701).${NC}"
    echo -e "${YELLOW}This card is part-way prepared and may still carry the setup-WiFi key.${NC}"
    echo -e "${YELLOW}Do not clone it until a run completes.${NC}"
    echo -e "${YELLOW}(If you know the previous run DID finish — its removal step warned it${NC}"
    echo -e "${YELLOW}could not retire the marker — clear it: sudo rm -f $_UNFINISHED_MARKER)${NC}"
    echo ""
fi
read -p "Are you sure you want to continue? (y/N) " -n 1 -r
echo ""

if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Cancelled."
    exit 0
fi

echo ""
echo "Preparing SD card for cloning..."
echo ""
# litclock-dev#701 — written before the first mutation. If this write fails the
# card is read-only or $STATE_DIR is unwritable, which are the same failure
# modes that would break the credential removal below — so stop HERE, while
# nothing has been touched, rather than discover it at Step 8.
if ! mkdir -p "$STATE_DIR" 2>/dev/null || ! touch "$_UNFINISHED_MARKER" 2>/dev/null; then
    echo -e "${RED}Could not write $_UNFINISHED_MARKER.${NC}"
    echo -e "${RED}The card may be read-only or $STATE_DIR unwritable — the same failure${NC}"
    echo -e "${RED}modes that would break the credential removal this script exists for.${NC}"
    echo -e "${RED}Nothing has been changed. Fix that first, then run again.${NC}"
    exit 1
fi


# Step 1: Stop the setup-state writers, then remove the setup-state markers.
#
# The markers below are RE-CREATABLE, so they must be removed with their
# writers already down. reset-setup.sh has always done it in this order (it
# stops six units before its own marker removal); this script did not, and
# litclock-dev#673 /review found two live paths that put .handoff-complete
# straight back:
#
#   - litclock-control.service (the PWA). src/control_server/handoff.py
#     re-creates the marker via `sudo touch` on the Done tap AND on any
#     Settings save, so an operator with the PWA open in a tab can reinstate it.
#   - litclock-update.service. scripts/update.sh re-creates the marker as its
#     EPIC litclock-dev#383 PR2 migration step. The unit is gated on .setup-complete so a
#     NEW run cannot start once the marker removal below has run, but a run
#     that already passed that check keeps going for minutes and re-touches
#     the marker near the end.
#
# Both reinstate exactly the defect litclock-dev#673 fixes, and both do it
# SILENTLY -- this script would still print "SD Card Ready for Cloning!". That
# is the same failure shape litclock-dev#660 closed by powering the Pi off.
#
# litclock-dev#274: stopping litclock-control.service also keeps the PWA from landing a
# Settings save concurrent with the env.sh overwrite in Step 2. Best-effort
# (`|| true`) under the `set -e` at line 12 — a missing or already-stopped unit
# must not abort the prep flow. litclock.timer is stopped in Step 4, which is
# late but harmless: litclock.service only READS these markers.
echo -n "Stopping setup-state writers... "
systemctl stop litclock-control.service 2>/dev/null || true
systemctl stop litclock-update.timer 2>/dev/null || true
systemctl stop litclock-update.service 2>/dev/null || true
# litclock-dev#676 made the handoff fallback RECURRING, so it is now a live
# writer of .handoff-complete on a card being prepared for cloning.
systemctl stop litclock-handoff-fallback.timer 2>/dev/null || true
systemctl stop litclock-handoff-fallback.service 2>/dev/null || true
echo -e "${GREEN}done${NC}"

# litclock-dev#673: clear the handoff marker too, exactly as reset-setup.sh
# does. Both scripts return the device to a fresh-setup state, so both must
# clear every marker a systemd unit gates on. (Non-gate markers such as
# .welcome-message are gift-mode-only and intentionally NOT mirrored here.)
# litclock.service is gated on .handoff-complete via ConditionPathExists, so
# leaving it behind means the master card — and every card cloned from it —
# ships with the quote gate already satisfied by the PREVIOUS owner's marker.
# Observed on bench QA before v0.224.0: the clock started rendering 12s after
# boot, during the recipient's setup, contending with first-boot for the panel,
# where a fresh flash correctly logs "skipped because of an unmet condition
# check" until the operator taps Done. The timer is stopped in Step 4 but never
# disabled, so it returns on the next boot and the stale marker is the only
# thing standing between a clone and a literary quote painted over the WiFi
# setup instructions the recipient is trying to read.
echo -n "Removing setup-state markers... "
# `|| true` under the `set -e` at line 12, with the existence check below as the
# real gate -- the same shape as Step 8 (litclock-dev#649), which this step lacked.
# Without it a failing `rm` terminates the script ON THIS LINE: `done` is never
# printed (the terminal is left mid-line), no diagnostic appears, and the run
# dies BEFORE env.sh is scrubbed, WiFi is wiped, the legacy certs are cleared and
# the hotspot password is deleted. `chattr +i` reproduces it; the realistic
# causes are the two Step 8 already names, a card remounting read-only or
# ownership drift on $CONFIG_DIR, plus a re-run after an earlier abort.
#
# .setup-complete is removed FIRST, and that order is load-bearing: update.sh
# hard-exits when .setup-complete is missing, so clearing it first shuts the
# door on a concurrent updater re-touching .handoff-complete. Step 1 stopping
# the unit is the primary defence; this is the second.
#
# With two markers there is now a PARTIAL failure state -- one gone, one
# surviving -- which is exactly the stale-marker condition litclock-dev#673 is
# about. It has to be named, not inferred from a missing `done`.
_MARKER_ERR=$(rm -f "$CONFIG_DIR/.setup-complete" 2>&1 >/dev/null) || true
_MARKER_ERR+=$(rm -f "$CONFIG_DIR/.handoff-complete" 2>&1 >/dev/null) || true
_SURVIVORS=()
# litclock-dev#665: a clone must not ship carrying the master's reset-failure
# marker — the recipient would be told not to pass on a card that is fine.
rm -f "$STATE_DIR/reset-failed" 2>/dev/null || true

for _m in .setup-complete .handoff-complete; do
    # `-L` alongside `-e` because `-e` follows symlinks and is false for a
    # dangling one. The invariant is "no entry survives at the marker path" --
    # a surviving entry means the removal did not do what it claimed.
    if [[ -e "$CONFIG_DIR/$_m" || -L "$CONFIG_DIR/$_m" ]]; then
        _SURVIVORS+=("$_m")
    fi
done
if (( ${#_SURVIVORS[@]} )); then
    echo -e "${RED}FAILED${NC}"
    echo -e "${RED}Could not remove ${_SURVIVORS[*]} from $CONFIG_DIR.${NC}"
    if [[ -n "$_MARKER_ERR" ]]; then
        echo -e "${RED}${_MARKER_ERR}${NC}"
    fi
    echo -e "${RED}Do NOT clone this card — every copy would skip part of setup.${NC}"
    exit 1
fi
unset _MARKER_ERR _SURVIVORS _m
echo -e "${GREEN}done${NC}"

# Step 2: Clear env.sh credentials.
#
# The PWA writer was stopped in Step 1 (litclock-dev#274). Write defaults via
# atomic_write_env_sh, which holds the shared sidecar flock against the Python
# writer; the explicit `|| true` on the helper call is required because `set -e`
# would otherwise treat a lock timeout (rc=75) as fatal and kill the whole prep
# flow halfway through.

echo -n "Clearing configuration (env.sh)... "
if [[ -f "$INSTALL_DIR/env.sh" ]]; then
    # litclock-dev#337 A3: defensive MODE + IP_COUNTRY defaults so a cloned image's
    # first boot lands on MODE=auto (on-boot reresolve will populate the
    # rest). Without these, a cloned env.sh would inherit whatever MODE
    # the cloner had — could be "specific" with stale coords for a
    # location 1000 miles from the cloned device's actual WiFi.
    DEFAULTS='export OPENWEATHERMAP_APIKEY=
export WEATHER_LATITUDE=
export WEATHER_LONGITUDE=
export WEATHER_UNITS=imperial
export WEATHER_LOCATION_MODE=auto
export WEATHER_IP_COUNTRY=
export WEATHER_TTL=3600
export ALLOW_NSFW_QUOTES=false
'
    # `|| true` not needed: every code path inside the if/else below
    # ends with a 0-exit statement, so `set -e` won't trip.
    if atomic_write_env_sh "$INSTALL_DIR/env.sh" "$DEFAULTS"; then
        echo -e "${GREEN}done${NC}"
    else
        _rc=$?
        if [[ "$_rc" == "75" ]]; then
            echo -e "${YELLOW}skipped (env.sh locked by another writer)${NC}"
        else
            echo -e "${YELLOW}failed (rc=$_rc) — env.sh untouched${NC}"
        fi
        unset _rc
        true  # explicit success for `set -e`
    fi
else
    echo -e "${GREEN}done${NC}"
fi

# Step 3: Clear WiFi credentials (optional - ask user)
echo ""
read -p "Clear saved WiFi networks? (y/N) " -n 1 -r
echo ""
if [[ $REPLY =~ ^[Yy]$ ]]; then
    # litclock-dev#701 — fail closed rather than start a wipe that kills the
    # session (and this script) part-way through. Steps 1-2 have already run,
    # so name the state that leaves the card in, exactly as the survivor-check
    # abort below does.
    _WIPE_NET_RC=0
    _is_network_session || _WIPE_NET_RC=$?
    if [[ "$_WIPE_NET_RC" == "2" ]]; then
        echo -e "${RED}REFUSED: could not determine whether this is a network session${NC}"
        echo -e "${RED}(ps unusable). Clearing WiFi from a network session would kill this${NC}"
        echo -e "${RED}script before Step 8 removes the setup-WiFi key, so the wipe is${NC}"
        echo -e "${RED}refused when that cannot be ruled out (litclock-dev#701).${NC}"
        echo -e "${RED}This card is already part-way prepared: markers cleared, env.sh${NC}"
        echo -e "${RED}scrubbed, setup-WiFi key NOT yet removed. Do NOT clone it.${NC}"
        echo -e "${RED}Run this script again from the local console.${NC}"
        exit 1
    fi
    if [[ "$_WIPE_NET_RC" == "0" ]]; then
        echo -e "${RED}REFUSED: you are connected over the network.${NC}"
        echo -e "${RED}Clearing WiFi would drop this session and SIGHUP would kill the${NC}"
        echo -e "${RED}script HERE — before Step 8 removes the setup-WiFi key — and the${NC}"
        echo -e "${RED}dropped session looks exactly like the script finishing${NC}"
        echo -e "${RED}(litclock-dev#701).${NC}"
        echo -e "${RED}This card is already part-way prepared: markers cleared, env.sh${NC}"
        echo -e "${RED}scrubbed, setup-WiFi key NOT yet removed. Do NOT clone it.${NC}"
        echo -e "${RED}Run this script again from the local console.${NC}"
        exit 1
    fi
    echo -n "Clearing WiFi credentials... "
    # NetworkManager connections
    # Delete through NetworkManager FIRST, then remove the files. Removing files
    # alone is exactly the half-measure Step 8 calls out: NM holds these
    # connections in memory — the operator's own network is ACTIVE right now —
    # and a live daemon can write a profile back after the file is gone
    # (/review, security). reset-setup.sh warns about this rather than fixing
    # it; a step that prints a verified `done` has to actually do it.
    while read -r _con; do
        [[ -n "$_con" ]] || continue
        nmcli connection delete "$_con" >/dev/null 2>&1 || true
    done < <(nmcli -t -f NAME connection show 2>/dev/null || true)
    rm -f "$_NM_PROFILE_DIR"/* 2>/dev/null || true
    # litclock-dev#653: verify, don't assume. This was `rm ... || true` followed
    # by an unconditional `done`, so an operator who explicitly ASKED to wipe
    # could be told it worked while home WiFi PSKs survived — the same
    # "reported success it did not achieve" shape as litclock-dev#649, arriving by the
    # opposite route: there the `|| true` was missing, here it is present with
    # nothing behind it.
    #
    # `find`, not `compgen -G "$DIR/*"`: the glob skips dotfiles, so a
    # `.`-prefixed keyfile or an editor swap file holding a PSK would pass the
    # gate (/review, security).
    if [[ -n "$(find "$_NM_PROFILE_DIR" -mindepth 1 -print -quit 2>/dev/null)" ]]; then
        echo -e "${RED}FAILED${NC}"
        echo -e "${RED}Saved WiFi profiles survive in $_NM_PROFILE_DIR.${NC}"
        echo -e "${RED}Do NOT clone this card — every copy would carry those network passwords.${NC}"
        # Say what state the card is in. Aborting HERE stops before Step 8, so
        # the setup-hotspot key has NOT been cleared yet and the card is already
        # part-way reset — markers gone, env.sh scrubbed. An operator told only
        # "do not clone" cannot know either of those (/review, security).
        echo -e "${RED}The setup-WiFi key has NOT been cleared yet, and this card is already${NC}"
        echo -e "${RED}part-way prepared. Fix the cause, then run this script again from the start.${NC}"
        exit 1
    fi
    # wpa_supplicant (legacy)
    if [[ -f "$_WPA_SUPPLICANT_CONF" ]]; then
        cat > "$_WPA_SUPPLICANT_CONF" << 'EOF'
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=US
EOF
    fi
    echo -e "${GREEN}done${NC}"
else
    echo "Keeping WiFi credentials."
fi

# Step 4: Stop clock timer and re-enable first-boot service
echo -n "Stopping clock timer... "
systemctl stop litclock.timer 2>/dev/null || true
echo -e "${GREEN}done${NC}"

echo -n "Enabling first-boot service... "
systemctl enable litclock-firstboot.service 2>/dev/null || true
echo -e "${GREEN}done${NC}"

# Step 5: Clear logs and caches.
# IMPORTANT: do NOT add `rm -f "$INSTALL_DIR"/env.sh.lock` here (or any other
# unlink of the env.sh sidecar lockfile). Per scripts/lib/state.sh:143-147,
# removing the sidecar between writes creates a new inode on the next
# `: > "$lock"` and the cross-writer flock interlock silently breaks (the
# shell writers and the Python PWA writer end up holding flocks on
# unrelated inodes). The globs below intentionally scope to *.log and
# weather-cache*.json — they won't match env.sh.lock. Pinned by
# tests/test_envsh_shell_flock.py::test_no_production_path_unlinks_sidecar_lock.
echo -n "Clearing logs and caches... "
rm -f "$INSTALL_DIR"/*.log 2>/dev/null || true
rm -f "$INSTALL_DIR"/weather-cache*.json 2>/dev/null || true
rm -f /tmp/litclock-* 2>/dev/null || true
# Clear journal logs older than 1 day
# litclock-dev#654: `--vacuum-time` operates ONLY on ARCHIVED journal files —
# man journalctl is explicit that it "will not remove active journal files". The
# documented cloning flow is provision, verify, then prepare, all in ONE boot,
# so the setup PSK that reached the journal via sudo's command-audit line is in
# the ACTIVE file, which a bare vacuum cannot see. Rotate first so everything
# written so far becomes archived, then vacuum to nothing: a card being cloned
# for strangers should ship no journal at all, and this step's job is "this card
# must carry nothing about this device".
journalctl --rotate 2>/dev/null || true
journalctl --vacuum-time=1s 2>/dev/null || true
echo -e "${GREEN}done${NC}"

# Step 6: Clear bash history
echo -n "Clearing bash history... "
rm -f /home/pi/.bash_history 2>/dev/null || true
rm -f /root/.bash_history 2>/dev/null || true
history -c 2>/dev/null || true
echo -e "${GREEN}done${NC}"

# Step 7: Clear legacy SSL certificates (nothing regenerates these since litclock-dev#715)
echo -n "Clearing legacy SSL certificates... "
rm -rf "$INSTALL_DIR/.certs" 2>/dev/null || true
echo -e "${GREEN}done${NC}"

# Step 8: Clear the persisted setup-hotspot password (litclock-dev#620).
#
# This script's whole purpose is cloning ONE prepared card into MANY cards for
# other people (docs/sd-card-cloning.md, "Creating SD Cards for Friends &
# Family"), and its precondition is a fully provisioned working clock — which
# means /var/lib/litclock/hotspot-password exists by then. Since litclock-dev#620 that file
# is PERMANENT (a plain factory reset deliberately preserves it), so without
# this step every clone would broadcast `LitClock-Setup` with the SAME WPA2 key,
# known to whoever made the cards and never rotated on any recipient device.
#
# Same reasoning as the WiFi profiles, bash history and SSL certs cleared
# above: anything that identifies or authenticates THIS device must not ride
# the image. The glob catches staging files orphaned by a power cut between
# mkstemp and os.replace, each holding a real past password.
echo -n "Clearing setup-hotspot password... "
# `|| true` under the `set -e` at line 12 (litclock-dev#649). Without it, a genuinely
# failing `rm` terminates the script ON THIS LINE, so the `if` below never
# runs and none of its three RED lines ever print — the warning written
# specifically to stop someone cloning a compromised card was unreachable in
# exactly the situation it exists for. The existence check that follows is the
# real gate; `rm`'s exit status is redundant with it. Unlike the unguarded
# `rm` at line 61, this one has a failure branch to reach, which is the whole
# difference: there, aborting IS the handling.
#
# `chattr +i` is the easy way to reproduce, but the realistic cause is a
# degrading SD card remounting read-only, or ownership drift on $STATE_DIR —
# at the exact moment someone is preparing cards to hand to other people.
#
# rm's stderr is captured rather than discarded so the failure branch can name
# the CAUSE. The two realistic ones need opposite remedies — "Read-only file
# system" means the card is dying, "Operation not permitted" means ownership
# drift — and an operator told only "could not remove" cannot tell them apart.
# litclock-dev#653: the state file is NOT the only place the key lives.
# `nmcli device wifi hotspot` writes a PERSISTENT connection profile —
# /etc/NetworkManager/system-connections/litclock-hotspot.nmconnection —
# containing the PSK, and two things have to go right for it not to ride the
# card, neither of which is guaranteed:
#
#   * teardown_hotspot() deletes it with check=False, so a failed delete is
#     silent (litclock-dev#616 is prior art that NM profile state does not always end up
#     where we assume);
#   * the WiFi wipe in Step 3 is opt-in and DEFAULTS TO KEEP, because many
#     cloners want to keep their test WiFi for the recipient.
#
# So the realistic path is: prepare a card without wiping WiFi, on a device
# where teardown did not remove the profile, and ship a working key for
# LitClock-Setup while this step prints `done`. Since litclock-dev#620 that key is
# PERMANENT, which is what makes it matter — pre-litclock-dev#620 it expired on the next
# provisioning cycle.
#
# Cleared UNCONDITIONALLY, independent of the opt-in wipe: this is OUR profile,
# not the operator's network, so the "keep my test WiFi" rationale does not
# cover it.
# A GLOB, not one filename (/review, security — demonstrated). NetworkManager's
# keyfile writer derives the filename from the connection id and disambiguates
# collisions with a numeric suffix, so a failed teardown delete — the premise of
# this whole fix — leaves `litclock-hotspot-1.nmconnection` behind. It also
# writes through a write-temp-then-rename, so a power cut can orphan
# `litclock-hotspot.nmconnection.XXXXXX`. Both hold the real PSK, and an
# exact-name check printed `done` over the top of them. The state-file half of
# this same line already globs for exactly this class of orphan.
_SETUP_NET_PROFILE_GLOB="$_NM_PROFILE_DIR/litclock-hotspot*"
nmcli connection delete litclock-hotspot >/dev/null 2>&1 || true
_RM_ERR=$(rm -f "$STATE_DIR/hotspot-password" "$STATE_DIR"/.hotspot-password.* \
    "$_NM_PROFILE_DIR"/litclock-hotspot* 2>&1 >/dev/null) || true
# `-L` alongside `-e` because `-e` FOLLOWS symlinks and is false for a dangling
# one — so a failed unlink of a dangling symlink at this path would have printed
# `done`. The invariant this step needs is "no entry survives at the password
# path", not "no readable file survives"; a surviving entry means the removal
# did not do what it claimed, whatever it points at. The glob half needs no
# equivalent: `compgen -G` matches NAMES, so it already sees a dangling link.
# The check is the gate, so it must gate on EVERY place the key lives — the
# state file, its staging files, and the NM profile (litclock-dev#653). A check that
# covered only the state file printed `done` for a card still carrying the
# profile.
if [[ -e "$STATE_DIR/hotspot-password" || -L "$STATE_DIR/hotspot-password" ]] ||
    compgen -G "$STATE_DIR/.hotspot-password.*" >/dev/null 2>&1 ||
    compgen -G "$_SETUP_NET_PROFILE_GLOB" >/dev/null 2>&1; then
    echo -e "${RED}FAILED${NC}"
    echo -e "${RED}Could not remove the setup-WiFi password from $STATE_DIR${NC}"
    echo -e "${RED}or its saved network profile at $_SETUP_NET_PROFILE_GLOB.${NC}"
    if [[ -n "$_RM_ERR" ]]; then
        echo -e "${RED}${_RM_ERR}${NC}"
    fi
    echo -e "${RED}Do NOT clone this card — every copy would share a key you know.${NC}"
    exit 1
fi
echo -e "${GREEN}done${NC}"

echo ""
echo "========================================"
echo -e "${GREEN}  SD Card Ready for Cloning!${NC}"
echo "========================================"
echo ""
if [[ "$POWEROFF_WHEN_DONE" == "true" ]]; then
    echo "Next steps:"
    echo "1. Wait for the green activity LED to stop, then remove the SD card"
    echo "2. Clone it using Win32 Disk Imager or dd"
    echo "3. Write clones to new SD cards"
    echo ""
    echo -e "${YELLOW}Do NOT power this card on again before you image it.${NC}"
    echo -e "${YELLOW}A single boot re-creates the setup-WiFi key this script just${NC}"
    echo -e "${YELLOW}removed, and every clone would then share it (litclock-dev#660).${NC}"
else
    echo "Next steps:"
    echo "1. Shut down the Pi:  sudo shutdown -h now"
    echo "2. Remove the SD card"
    echo "3. Clone it using Win32 Disk Imager or dd"
    echo "4. Write clones to new SD cards"
    echo ""
    echo -e "${YELLOW}SSH was NOT disabled (--no-poweroff is the inspection path), so the${NC}"
    echo -e "${YELLOW}imaged card will carry this master's SSH posture onto every clone${NC}"
    echo -e "${YELLOW}(#57). Run without --no-poweroff for cards you hand to others.${NC}"
    echo -e "${RED}--no-poweroff was used, so this Pi is still running.${NC}"
    echo -e "${RED}Do NOT let it boot again before imaging: a single boot re-creates${NC}"
    echo -e "${RED}the setup-WiFi key this script just removed, and every clone would${NC}"
    echo -e "${RED}then share it (litclock-dev#660).${NC}"
    echo ""
    # litclock-dev#659 — say it here, at the moment the state is created. The
    # panel froze and :80 started refusing several steps ago; by the time anyone
    # looks again this terminal is gone and all that is left is a frozen clock
    # and a dead PWA. Name the MARKERS, not the stopped units: the units are the
    # transient half, and an operator told "this stopped the timer" reasonably
    # starts the timer, which exits 0 and changes nothing because
    # litclock.service is ConditionPathExists-gated on a marker Step 1 deleted.
    # Sending someone into a second dead end is worse than saying nothing.
    echo -e "${YELLOW}The display is frozen on the last quote and port 80 is refusing${NC}"
    echo -e "${YELLOW}connections. That is expected. This script cleared${NC}"
    echo -e "${YELLOW}/etc/litclock/.setup-complete and .handoff-complete, and${NC}"
    echo -e "${YELLOW}litclock-control.service and litclock.service are${NC}"
    echo -e "${YELLOW}ConditionPathExists-gated on them — so restarting either exits 0${NC}"
    echo -e "${YELLOW}and changes nothing. There is nothing to debug (litclock-dev#659).${NC}"
fi
echo ""
echo "When a cloned card boots, it will:"
echo "- Show 'Welcome!' on the e-ink display"
echo "- Bring up the LitClock-Setup WiFi network if needed"
echo "- Display QR code for phone setup"
echo ""
echo -e "Tip: To reconfigure without a full clone reset, use ${YELLOW}scripts/reset-setup.sh${NC} instead."
echo ""

# #57 — SSH off is the last mutation before poweroff, mirroring
# reset-setup.sh's two handoff arms: after the fail-closed password check
# (an aborted prep leaves the operator their remote access, same ordering
# rule), and only on the poweroff arm — a --no-poweroff run is the
# inspection path, and stripping the inspector's own access would be the
# wrong trade; it gets a warning instead (below).
if [[ "$POWEROFF_WHEN_DONE" == "true" ]]; then
    if [[ "${_NET_SESSION_RC:-2}" == "0" ]]; then
        echo -e "${YELLOW}SSH is about to be disabled: THIS SESSION WILL DROP. That is${NC}"
        echo -e "${YELLOW}expected — the Pi powers itself off within ~15 seconds. Wait for${NC}"
        echo -e "${YELLOW}the green LED to stop, then image the card (#57).${NC}"
        echo -e "${YELLOW}If it has NOT powered off within a minute, do NOT image it —${NC}"
        echo -e "${YELLOW}something refused the handoff; run again from the local console.${NC}"
    elif [[ "${_NET_SESSION_RC:-2}" == "2" ]]; then
        # Could-not-determine gets the same courtesy (/review litclock-dev#713): it is
        # about to lose its output to the redirect below, so if it WAS a
        # network session, this is the last line the operator sees.
        echo -e "${YELLOW}SSH is about to be disabled. If you are connected over the network,${NC}"
        echo -e "${YELLOW}this session will drop — expected; the Pi powers off within ~15s.${NC}"
        echo -e "${YELLOW}Wait for the green LED to stop, then image the card (#57).${NC}"
        echo -e "${YELLOW}If it has NOT powered off within a minute, do NOT image it —${NC}"
        echo -e "${YELLOW}something refused the handoff; run again from the local console.${NC}"
    fi
    # Ignore SIGHUP from here: disabling sshd kills this script's own SSH
    # session, and dying HERE would skip the poweroff — leaving a running Pi
    # whose next boot re-mints the setup key Step 8 just removed
    # (litclock-dev#660). Unlike the Step 3 wipe (litclock-dev#701), the
    # remaining work is exactly [gate, marker, poweroff] — seconds, with the
    # full summary already printed above.
    trap '' HUP
    # The HUP trap alone is NOT enough (verified on a real pty): once sshd
    # dies, this script's stdout is a closed pty, every echo fails with EIO,
    # and under `set -e` the FIRST echo after the drop — the gate's own
    # "done" — kills the script before the marker retires or poweroff runs.
    # So on any session that may drop (network, or could-not-determine),
    # send the rest to /dev/null: the operator has already been shown the
    # full summary and the drop warning, and a local console (verdict 1)
    # keeps its output. Deliberately not the journal — Step 5 just vacuumed
    # it, and a clone card should stay that way.
    # `:-2`, not `:-1` (/review litclock-dev#713): an unset verdict must default to the
    # fail-SAFE direction — redirect — because the un-redirected failure mode
    # is an EIO-killed tail with no poweroff. Unreachable today (the verdict
    # is set before the confirm), but the default should not pick the worse
    # arm if that ever changes.
    if [[ "${_NET_SESSION_RC:-2}" != "1" ]]; then
        exec >/dev/null 2>&1
    fi
    disable_ssh_for_handoff
fi

# litclock-dev#701 — every step above completed and verified itself; retire the
# unfinished-run marker after the last gate, so any earlier death — including
# an SSH gate that refused the handoff (its exit 1 leaves the marker for the
# next, console, run to report) — stays visible. A surviving marker here is
# the benign direction (a false "did not finish" warning next run), so warn
# rather than abort — the card itself is fully prepared.
rm -f "$_UNFINISHED_MARKER" 2>/dev/null || true
if [[ -e "$_UNFINISHED_MARKER" || -L "$_UNFINISHED_MARKER" ]]; then
    echo -e "${YELLOW}Could not remove $_UNFINISHED_MARKER. Every step DID finish — the${NC}"
    echo -e "${YELLOW}card is safe to clone — but the next run of this script will falsely${NC}"
    echo -e "${YELLOW}warn that this one died half-way (litclock-dev#701).${NC}"
fi

# litclock-dev#660 — power off LAST, after the operator has seen the whole
# summary above, so nothing can boot this card before it is imaged. Mirrors
# gift mode in reset-setup.sh, which powers off for the same reason: the device
# is leaving, and a boot in between would undo the step that made it safe to.
if [[ "$POWEROFF_WHEN_DONE" == "true" ]]; then
    echo "Powering off now so the card cannot boot before you image it."
    # litclock-dev#660 review: `poweroff` can fail (logind/D-Bus unavailable is a
    # realistic state on the same degrading card Step 8 exists to catch). Under
    # `set -e` an unchecked failure would kill the script right after printing
    # the line above, leaving the operator a false statement, no fallback, and a
    # running Pi they believe is off — while "remove the SD card" is step 1 of
    # what they just read.
    poweroff || {
        echo -e "${RED}Power-off FAILED — the Pi is still running.${NC}"
        echo -e "${RED}Run 'sudo shutdown -h now' and wait for it to halt BEFORE${NC}"
        echo -e "${RED}removing the card. Do not let it boot again first: a single${NC}"
        echo -e "${RED}boot re-creates the key just removed (litclock-dev#660).${NC}"
        # litclock-dev#659 — this arm reaches the same frozen state the
        # --no-poweroff arm warns about, so it needs the same sentence. The
        # SUCCESSFUL default path does not: it halts, and there is no panel
        # left to misread.
        echo -e "${RED}The display is frozen and port 80 is refusing connections. That${NC}"
        echo -e "${RED}part is expected: this script cleared /etc/litclock/.setup-complete${NC}"
        echo -e "${RED}and .handoff-complete, which litclock-control.service and${NC}"
        echo -e "${RED}litclock.service are ConditionPathExists-gated on, so restarting${NC}"
        echo -e "${RED}them changes nothing (litclock-dev#659).${NC}"
        exit 1
    }
fi
