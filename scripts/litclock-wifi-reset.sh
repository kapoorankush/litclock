#!/bin/bash
# LitClock — Reset-WiFi handler (litclock-dev#245 M5, D11 + D12).
#
# Invoked via `systemctl start --no-block litclock-wifi-reset.service` from
# /api/wifi/reset (sudoers-allowed). Drops the user back into firstboot
# AP-mode so they can re-provision against a different WiFi network.
#
# Sequence (D12: wipe ALL wifi profiles, not just the active one — otherwise
# NetworkManager auto-reconnects to a secondary saved network and firstboot
# detects "already on WiFi" and never enters AP-mode, soft-bricking the user
# on a gift-transfer / moved-house path):
#
#   Guard: if the setup marker is gone and first-boot is already running a
#      setup, exit 0 having changed nothing (a double-fire; litclock-dev#938).
#   Pre-flight: refuse (exit 1) if nmcli is missing.
#   0. ENABLE litclock-firstboot.service (litclock-dev#938) — before any
#      state changes, refusing to reset if it fails. See Step 0 below.
#   1. Stop litclock-control.service so the running waitress process doesn't
#      linger on the dead LAN interface as a zombie. firstboot.service brings
#      it back when /etc/litclock/.setup-complete reappears.
#   2. Remove /etc/litclock/.setup-complete (and .handoff-complete) so
#      litclock-firstboot.service re-runs its hotspot + captive-portal
#      provisioning flow. Before the wipe, so no interruption point leaves
#      "no WiFi, but marked set up" (litclock-dev#938 review).
#   3. Delete every NetworkManager connection of type 802-11-wireless by UUID
#      (not SSID — connection IDs are not guaranteed equal to SSID, and
#      duplicate/escaped SSIDs make name-based delete brittle).
#   4. Restart litclock-firstboot.service (Type=oneshot — restart re-fires it).
#
# env.sh is preserved verbatim — D1 lock: location, weather, gift-mode all
# stay set across the reset. The user only re-enters WiFi credentials.
#
# Run as root via the systemd unit's User=root. Single sudoers entry
# (litclock-wifi-reset.service) is the only privilege surface.
#
# Failures are logged to journald via the unit's StandardOutput=journal +
# StandardError=journal — `journalctl -u litclock-wifi-reset` shows the full
# story. The PWA already returned 200 by the time this fires; helper-failure
# visibility for v1 is journald only (e-ink overlay deferred to v2).

set -u

NMCLI="${LITCLOCK_NMCLI:-/usr/bin/nmcli}"
SYSTEMCTL="${LITCLOCK_SYSTEMCTL:-/usr/bin/systemctl}"
SETUP_COMPLETE_FILE="${LITCLOCK_SETUP_COMPLETE_FILE:-/etc/litclock/.setup-complete}"
HANDOFF_COMPLETE_FILE="${LITCLOCK_HANDOFF_COMPLETE_FILE:-/etc/litclock/.handoff-complete}"

log() {
    echo "[litclock-wifi-reset] $1"
}

# Already handed off (litclock-dev#938 review): the setup marker is gone and
# first-boot is running a setup right now. A second reset here — the PWA's
# token-restore path allows a double-fire, and a maintainer can re-run the
# unit — would delete every 802-11-wireless profile, the live LitClock-Setup
# hotspot included, and restart first-boot mid-setup. Change nothing. Exit 0
# so the unit's ExecStopPost failsafe also leaves the running setup alone.
# Two known limits, both CLI-only (owner decisions on the litclock-dev#938 review): the
# guard starts at "activating", so a first-boot whose start job is still
# QUEUED reads "inactive" and a second reset in that gap proceeds; and a
# WEDGED first-boot (activating forever) is no longer killed and rerun by a
# reset. Recover that one directly:
#   sudo systemctl restart --no-block litclock-firstboot.service
if [ ! -e "$SETUP_COMPLETE_FILE" ] \
    && [ "$("$SYSTEMCTL" is-active litclock-firstboot.service 2>/dev/null)" = activating ]; then
    log "first-boot is already provisioning (no setup marker) — nothing to reset"
    log "if that setup is stuck, run: sudo systemctl restart --no-block litclock-firstboot.service"
    exit 0
fi

# Pre-flight — nmcli must be installed and executable. If it's missing
# (rare but possible after a failed apt upgrade or pi-gen drift), the
# wifi-wipe step (Step 3) silently emits an empty UUID list, after Step 2
# has already removed .setup-complete, and we restart firstboot anyway. Result:
# NetworkManager's saved connections stay on disk, and first-boot's
# pre-connected path (litclock-dev#647) rejoins the same network and finishes
# setup inline, so the reset silently does nothing. Fail loud + early instead. /review caught
# this; see PR litclock-dev#284 review notes.
if ! command -v "$NMCLI" >/dev/null 2>&1; then
    log "error: nmcli not found at $NMCLI — refusing to reset (would soft-brick)"
    log "       install with: sudo apt install network-manager"
    exit 1
fi

# Step 0 — enable firstboot BEFORE touching anything (litclock-dev#938).
# A successful setup ends with disable_first_boot() (scripts/first-boot.sh),
# so on a provisioned clock the unit is DISABLED. Restarting it in Step 4
# runs it once, but nothing brings it back at the next boot: if setup is not
# finished within first-boot's 30-minute window, it paints "Setup incomplete
# — unplug and plug back in" and powers off, and after that power-cycle no
# hotspot is raised and the WiFi profiles are already gone. The clock sits on
# the boot splash for good, and an owner without a console has to reflash.
# A power cut or timeout after the WiFi wipe reached the same state, which is why
# this runs FIRST rather than beside the restart. Every other handoff to
# first-boot already enables it (reset-setup.sh, prepare-for-cloning.sh,
# wifi-watchdog.sh). Enabling while .setup-complete still exists is harmless:
# first-boot's check_setup_complete skips. A successful setup disables the
# unit again; a reset cut off before Step 2 leaves it enabled with the marker
# in place, and first-boot then runs its "already set up" path at each boot:
# it starts the clock as a normal boot would, which also re-enables
# litclock.timer if someone had disabled it by hand (owner decision on the
# litclock-dev#938 review: not worth touching first-boot.sh for).
# If it fails, refuse the reset, like the nmcli pre-flight above: a reset
# that cannot promise a retry is worse than no reset.
# --no-reload: enable's implicit daemon-reload is slow on a Zero 2 W and can
# fail AFTER the symlink is made, which would refuse a reset for nothing.
# The symlink is all the next boot reads; the unit is already loaded.
log "Enabling litclock-firstboot.service"
if ! "$SYSTEMCTL" enable --no-reload litclock-firstboot.service 2>&1; then
    log "error: could not enable litclock-firstboot.service — refusing to reset"
    log "       (an unfinished setup would leave the clock unrecoverable)"
    exit 1
fi

# Step 1 — stop the control server. We're about to take the LAN down; the
# running waitress process should exit cleanly. firstboot.service brings the
# control server back when ConditionPathExists=/etc/litclock/.setup-complete
# is satisfied again post-provisioning.
log "Stopping litclock-control.service"
"$SYSTEMCTL" stop litclock-control.service 2>&1 || \
    log "warn: could not stop litclock-control.service (may not be running)"

# Step 2 — remove the setup-complete marker so firstboot.service re-runs.
# BEFORE the WiFi wipe (litclock-dev#938 review; this used to come after it).
# With the marker gone first, any interruption from here on lands in
# provisioning. A power cut does so at the next boot: first-boot is already
# enabled (Step 0) and, seeing no marker, either takes the pre-connected path
# on WiFi that is still saved or raises the hotspot. A kill or timeout while
# the Pi stays up does so at once, through the unit's ExecStopPost= failsafe
# (systemd/litclock-wifi-reset.service), which restarts first-boot. The old
# order left a window where the WiFi was gone but the marker was not, so
# first-boot skipped and no hotspot came up. It also means a failed rm aborts
# before anything destructive.
log "Removing $SETUP_COMPLETE_FILE"
if [ -f "$SETUP_COMPLETE_FILE" ]; then
    if ! rm -f "$SETUP_COMPLETE_FILE" 2>&1; then
        log "error: could not remove $SETUP_COMPLETE_FILE — firstboot will not re-enter AP-mode"
        exit 1
    fi
fi

# EPIC litclock-dev#383 PR2 (litclock-dev#388): clear the handoff marker too. A WiFi reset is exactly
# when the egress timezone may change (moving house, regifting). env.sh is
# preserved, but if the new network's IP-geo FAILS the resolver leaves the
# stale lat/tz in place — and if .handoff-complete still existed, the re-
# provision would skip the handoff entirely and start a WRONG-TIME clock
# (defeating the A2 invariant). Clearing it forces the handoff (IP-geo
# re-resolve + browser-tz fallback) to re-run. Best-effort: a stuck marker
# here only costs a missed re-handoff, so don't abort the reset on failure.
log "Removing $HANDOFF_COMPLETE_FILE"
rm -f "$HANDOFF_COMPLETE_FILE" 2>/dev/null || log "warning: could not remove $HANDOFF_COMPLETE_FILE"

# Step 3 — wipe ALL wifi-type connections by UUID (D12).
# nmcli -t emits machine-parseable colon-separated rows. We read both UUID
# and TYPE so we can filter on type AND iterate by UUID (not name).
WIFI_UUIDS=()
while IFS=: read -r uuid ctype; do
    if [ "$ctype" = "802-11-wireless" ] && [ -n "$uuid" ]; then
        WIFI_UUIDS+=("$uuid")
    fi
done < <("$NMCLI" -t -f UUID,TYPE connection show 2>/dev/null || true)

if [ "${#WIFI_UUIDS[@]}" -eq 0 ]; then
    log "No wifi-type connections to delete"
else
    log "Deleting ${#WIFI_UUIDS[@]} wifi connection(s)"
    for uuid in "${WIFI_UUIDS[@]}"; do
        if "$NMCLI" connection delete "$uuid" 2>&1; then
            log "  deleted UUID=$uuid"
        else
            log "  warn: failed to delete UUID=$uuid (continuing)"
        fi
    done
fi

# Step 4 — restart firstboot. Type=oneshot units re-fire on `restart`.
#
# CRITICAL: --no-block. Without it, `systemctl restart` blocks until the
# unit's ExecStart (the entire AP-mode + captive-portal + WiFi-credential
# flow) finishes — which can take MINUTES. wifi-reset.service has its
# own TimeoutStartSec=60, so without --no-block the parent unit gets
# SIGTERM'd at +60s and the helper script dies with the firstboot flow
# still running in the kernel job queue. Hardware QA on test Pi caught
# this 2026-04-30. With --no-block, systemctl just hands the start
# request to systemd's job manager and returns immediately.
log "Restarting litclock-firstboot.service (--no-block)"
"$SYSTEMCTL" restart --no-block litclock-firstboot.service 2>&1 || \
    { log "error: failed to restart litclock-firstboot.service"; exit 1; }

log "Reset complete — Pi should appear as 'LitClock-Setup' hotspot shortly"
exit 0
