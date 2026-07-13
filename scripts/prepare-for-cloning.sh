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

# Check if running as root
if [[ $EUID -ne 0 ]]; then
   echo -e "${RED}This script must be run as root (sudo)${NC}"
   exit 1
fi

INSTALL_DIR="/home/pi/litclock"
CONFIG_DIR="/etc/litclock"

# Source shared state-file helpers for atomic_write_env_sh (#274) — the
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
# #274: stop litclock-control.service before the rewrite so the PWA can't
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
    # #337 A3: defensive MODE + IP_COUNTRY defaults so a cloned image's
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

echo ""
echo "========================================"
echo -e "${GREEN}  SD Card Ready for Cloning!${NC}"
echo "========================================"
echo ""
echo "Next steps:"
echo "1. Shut down the Pi:  sudo shutdown -h now"
echo "2. Remove the SD card"
echo "3. Clone it using Win32 Disk Imager or dd"
echo "4. Write clones to new SD cards"
echo ""
echo "When a cloned card boots, it will:"
echo "- Show 'Welcome!' on the e-ink display"
echo "- Create WiFi hotspot if needed"
echo "- Display QR code for phone setup"
echo ""
echo -e "Tip: To reconfigure without a full clone reset, use ${YELLOW}scripts/reset-setup.sh${NC} instead."
echo ""
