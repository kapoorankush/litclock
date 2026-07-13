#!/bin/bash
# LitClock — Reset-WiFi handler (#245 M5, D11 + D12).
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
#   1. Stop litclock-control.service so the running waitress process doesn't
#      linger on the dead LAN interface as a zombie. firstboot.service brings
#      it back when /etc/litclock/.setup-complete reappears.
#   2. Delete every NetworkManager connection of type 802-11-wireless by UUID
#      (not SSID — connection IDs are not guaranteed equal to SSID, and
#      duplicate/escaped SSIDs make name-based delete brittle).
#   3. Remove /etc/litclock/.setup-complete so litclock-firstboot.service
#      will re-run its hotspot + captive-portal provisioning flow.
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

# Pre-flight — nmcli must be installed and executable. If it's missing
# (rare but possible after a failed apt upgrade or pi-gen drift), the
# wifi-wipe step on line ~60 silently emits an empty UUID list and we
# proceed to remove .setup-complete + restart firstboot anyway. Result:
# NetworkManager's saved connections stay on disk, firstboot detects
# "already on WiFi", exits oneshot, and the Pi appears stuck (no
# hotspot, no LAN drop). Fail loud + early instead. /review caught
# this; see PR #284 review notes.
if ! command -v "$NMCLI" >/dev/null 2>&1; then
    log "error: nmcli not found at $NMCLI — refusing to reset (would soft-brick)"
    log "       install with: sudo apt install network-manager"
    exit 1
fi

# Step 1 — stop the control server. We're about to take the LAN down; the
# running waitress process should exit cleanly. firstboot.service brings the
# control server back when ConditionPathExists=/etc/litclock/.setup-complete
# is satisfied again post-provisioning.
log "Stopping litclock-control.service"
"$SYSTEMCTL" stop litclock-control.service 2>&1 || \
    log "warn: could not stop litclock-control.service (may not be running)"

# Step 2 — wipe ALL wifi-type connections by UUID (D12).
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

# Step 3 — remove the setup-complete marker so firstboot.service re-runs.
# We do this AFTER the nmcli wipe because firstboot's own logic gates on
# .setup-complete; the order here mirrors the user-intent ("forget WiFi,
# then restart provisioning").
log "Removing $SETUP_COMPLETE_FILE"
if [ -f "$SETUP_COMPLETE_FILE" ]; then
    if ! rm -f "$SETUP_COMPLETE_FILE" 2>&1; then
        log "error: could not remove $SETUP_COMPLETE_FILE — firstboot will not re-enter AP-mode"
        exit 1
    fi
fi

# EPIC #383 PR2 (#388): clear the handoff marker too. A WiFi reset is exactly
# when the egress timezone may change (moving house, regifting). env.sh is
# preserved, but if the new network's IP-geo FAILS the resolver leaves the
# stale lat/tz in place — and if .handoff-complete still existed, the re-
# provision would skip the handoff entirely and start a WRONG-TIME clock
# (defeating the A2 invariant). Clearing it forces the handoff (IP-geo
# re-resolve + browser-tz fallback) to re-run. Best-effort: a stuck marker
# here only costs a missed re-handoff, so don't abort the reset on failure.
log "Removing $HANDOFF_COMPLETE_FILE"
rm -f "$HANDOFF_COMPLETE_FILE" 2>/dev/null || log "warning: could not remove $HANDOFF_COMPLETE_FILE"

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
