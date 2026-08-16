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

# Source shared state-file helpers for atomic_write_env_sh (litclock-dev#274) — the
# env.sh writer-lock that interoperates with src/config.py's fcntl.flock
# on the sidecar. state.sh ships in the same release as this script, so
# a missing file means a broken install — hard-fail rather than silently
# dropping the lock.
_THIS_SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$_THIS_SCRIPT_DIR/lib/state.sh"

echo "========================================"
echo "  Prepare LitClock for Cloning"
echo "========================================"
echo ""
echo -e "${YELLOW}WARNING: This will reset the clock configuration!${NC}"
echo "The SD card will be ready to clone for distribution."
echo ""
read -p "Are you sure you want to continue? (y/N) " -n 1 -r
echo ""

if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Cancelled."
    exit 0
fi

echo ""
echo "Preparing SD card for cloning..."
echo ""

# Step 1: Remove setup-complete flag
echo -n "Removing setup-complete flag... "
rm -f "$CONFIG_DIR/.setup-complete"
echo -e "${GREEN}done${NC}"

# Step 2: Clear env.sh credentials.
#
# litclock-dev#274: stop litclock-control.service before the rewrite so the PWA can't
# land a Settings save concurrent with our overwrite. Best-effort (`|| true`)
# under the `set -e` at line 12 — a missing/stopped service must not abort
# the prep flow. Then write defaults via atomic_write_env_sh which holds the
# shared sidecar flock against the Python writer; the explicit `|| true` on
# the helper call is required because `set -e` would otherwise treat a lock
# timeout (rc=75) as fatal and kill the whole prep flow halfway through.
echo -n "Stopping litclock-control.service... "
systemctl stop litclock-control.service 2>/dev/null || true
echo -e "${GREEN}done${NC}"

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
    echo -n "Clearing WiFi credentials... "
    # NetworkManager connections
    rm -f /etc/NetworkManager/system-connections/* 2>/dev/null || true
    # wpa_supplicant (legacy)
    if [[ -f /etc/wpa_supplicant/wpa_supplicant.conf ]]; then
        cat > /etc/wpa_supplicant/wpa_supplicant.conf << 'EOF'
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
journalctl --vacuum-time=1d 2>/dev/null || true
echo -e "${GREEN}done${NC}"

# Step 6: Clear bash history
echo -n "Clearing bash history... "
rm -f /home/pi/.bash_history 2>/dev/null || true
rm -f /root/.bash_history 2>/dev/null || true
history -c 2>/dev/null || true
echo -e "${GREEN}done${NC}"

# Step 7: Clear SSL certificates (will be regenerated on first boot)
echo -n "Clearing SSL certificates... "
rm -rf "$INSTALL_DIR/.certs" 2>/dev/null || true
echo -e "${GREEN}done${NC}"

# Step 8: Clear the persisted setup-hotspot password (litclock-dev#620).
#
# This script's whole purpose is cloning ONE prepared card into MANY cards for
# other people (docs/sd-card-cloning.md, "Creating SD Cards for Friends &
# Family"), and its precondition is a fully provisioned working clock — which
# means /var/lib/litclock/hotspot-password exists by then. Since #620 that file
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
_RM_ERR=$(rm -f "$STATE_DIR/hotspot-password" "$STATE_DIR"/.hotspot-password.* 2>&1 >/dev/null) || true
# `-L` alongside `-e` because `-e` FOLLOWS symlinks and is false for a dangling
# one — so a failed unlink of a dangling symlink at this path would have printed
# `done`. The invariant this step needs is "no entry survives at the password
# path", not "no readable file survives"; a surviving entry means the removal
# did not do what it claimed, whatever it points at. The glob half needs no
# equivalent: `compgen -G` matches NAMES, so it already sees a dangling link.
if [[ -e "$STATE_DIR/hotspot-password" || -L "$STATE_DIR/hotspot-password" ]] ||
    compgen -G "$STATE_DIR/.hotspot-password.*" >/dev/null 2>&1; then
    echo -e "${RED}FAILED${NC}"
    echo -e "${RED}Could not remove the setup-WiFi password from $STATE_DIR.${NC}"
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
    echo -e "${RED}--no-poweroff was used, so this Pi is still running.${NC}"
    echo -e "${RED}Do NOT let it boot again before imaging: a single boot re-creates${NC}"
    echo -e "${RED}the setup-WiFi key this script just removed, and every clone would${NC}"
    echo -e "${RED}then share it (litclock-dev#660).${NC}"
fi
echo ""
echo "When a cloned card boots, it will:"
echo "- Show 'Welcome!' on the e-ink display"
echo "- Bring up the LitClock-Setup WiFi network if needed"
echo "- Display QR code for phone setup"
echo ""
echo -e "Tip: To reconfigure without a full clone reset, use ${YELLOW}scripts/reset-setup.sh${NC} instead."
echo ""

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
        exit 1
    }
fi
