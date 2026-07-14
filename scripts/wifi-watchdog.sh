#!/bin/bash
# LitClock WiFi watchdog — reboots if WiFi is unreachable for too long.
#
# Triggered every 5 min by systemd/wifi-watchdog.timer. Counter persists
# at /var/lib/litclock/wifi-watchdog-reboots so reboot attempts survive
# the reboots they trigger (#245 M5 D8 — F4 in plan: was /tmp, but /tmp
# may be tmpfs on some configs and /var/lib/litclock is the canonical
# project state dir already provisioned by tmpfiles.d).
#
# Behaviour matrix (M5 OV1 / D8):
#
#   No /etc/litclock/.setup-complete   → exit 0 (skip)        [F3]
#       (Pi is in firstboot AP-mode; no LAN by design. Watchdog rebooting
#       here would tear down the hotspot the user is trying to use.)
#
#   No default route                    → fall through to ping fallback   [F2]
#       (Moved-house failure mode has no default route. The pre-M5 script
#       early-exited 0 here, which made OV1's firstboot fallback structurally
#       unreachable. Use 1.1.1.1 as a fallback target so the counter still
#       increments and the brick-loop guard / firstboot fallback fires.)
#
#   Ping succeeds                       → reset counter, exit 0
#   count < 2                           → increment, reboot
#   count == 2 (about to do 3rd reboot) → rm .setup-complete, increment, reboot
#                                            (firstboot fallback — Pi enters
#                                            AP-mode on next boot, hotspot
#                                            reappears, user re-provisions)
#   count >= 5                          → log + exit (brick-loop guard)
#
# Counter is cleared by the success path here AND by
# src/wifi_provision.connect_to_wifi() on a successful re-provisioning so
# the moved-house user doesn't see a stale count==3 falsely re-trigger
# firstboot in the first 5 minutes after re-provisioning.

set -u

STATE_DIR="${LITCLOCK_STATE_DIR:-/var/lib/litclock}"
COUNTER_FILE="${LITCLOCK_WIFI_WATCHDOG_COUNTER:-${STATE_DIR}/wifi-watchdog-reboots}"
SETUP_COMPLETE_FILE="${LITCLOCK_SETUP_COMPLETE_FILE:-/etc/litclock/.setup-complete}"
PING_FALLBACK="${LITCLOCK_WIFI_WATCHDOG_FALLBACK:-1.1.1.1}"
MAX_REBOOTS="${LITCLOCK_WIFI_WATCHDOG_MAX_REBOOTS:-5}"
FIRSTBOOT_FALLBACK_AT="${LITCLOCK_WIFI_WATCHDOG_FIRSTBOOT_AT:-2}"
REBOOT_CMD="${LITCLOCK_WIFI_WATCHDOG_REBOOT_CMD:-/sbin/reboot}"

log() {
    logger -t wifi-watchdog "$1" 2>/dev/null || echo "wifi-watchdog: $1" >&2
}

# F3 — skip during firstboot AP-mode. Without setup-complete the Pi is
# deliberately running its own hotspot (no LAN by design); rebooting would
# tear down the hotspot the user is trying to connect to.
if [ ! -f "$SETUP_COMPLETE_FILE" ]; then
    exit 0
fi

# F2 — pick a ping target. Prefer the default-route gateway when present
# (so a flaky ISP near home is detected via the local router, not via the
# public Internet). Fall back to PING_FALLBACK when no default route exists
# — that's the moved-house signature, and we MUST still increment the
# counter so the firstboot-fallback path can fire.
PING_TARGET="$(ip -4 route show default 2>/dev/null | awk '{print $3}' | head -1)"
if [ -z "$PING_TARGET" ]; then
    PING_TARGET="$PING_FALLBACK"
fi

for _ in 1 2 3; do
    if ping -c 1 -W 5 "$PING_TARGET" > /dev/null 2>&1; then
        # WiFi up — reset the reboot counter so a transient outage that
        # cleared on retry doesn't accumulate toward the firstboot threshold.
        rm -f "$COUNTER_FILE"
        exit 0
    fi
    sleep 10
done

# Read current counter (0 if missing or unparseable).
COUNT=0
if [ -f "$COUNTER_FILE" ]; then
    raw="$(cat "$COUNTER_FILE" 2>/dev/null || echo 0)"
    case "$raw" in
        ''|*[!0-9]*) COUNT=0 ;;
        *) COUNT="$raw" ;;
    esac
fi

# Brick-loop guard — never reboot more than MAX_REBOOTS times in a row.
if [ "$COUNT" -ge "$MAX_REBOOTS" ]; then
    log "WiFi unreachable but reboot limit ($MAX_REBOOTS) reached, skipping reboot"
    exit 1
fi

# F8 — explicit pre-increment semantic for D8's tiered threshold.
# When COUNT == FIRSTBOOT_FALLBACK_AT (default 2), we are ABOUT to do the
# 3rd reboot. That's the moment to drop into firstboot mode: the user has
# given the network 3 boot attempts and it still isn't working. Wipe
# .setup-complete so the next boot puts the Pi back in AP-mode + captive
# portal, where the user can reprovision against whatever WiFi is actually
# available (e.g. a new house).
if [ "$COUNT" -eq "$FIRSTBOOT_FALLBACK_AT" ]; then
    log "WiFi still unreachable after ${COUNT} reboots — entering firstboot fallback"
    if rm -f "$SETUP_COMPLETE_FILE" 2>/dev/null; then
        :
    elif sudo rm -f "$SETUP_COMPLETE_FILE" 2>/dev/null; then
        :
    else
        log "Failed to remove $SETUP_COMPLETE_FILE — firstboot fallback may not engage"
    fi

    # Hardware QA on test Pi (2026-04-30) caught: deleting .setup-complete is
    # NOT enough. litclock-firstboot.service is `disable`d at the end of every
    # successful first-boot run (disable_first_boot() in scripts/first-boot.sh),
    # so on the next boot systemd doesn't even consider firing it — the
    # ConditionPathExists check never runs because the unit isn't in the boot
    # graph. Re-enable it explicitly here so the firstboot fallback actually
    # engages on the next boot. Best-effort: a missing systemctl or sudo
    # denial would be very surprising in this code path (we already accept
    # the larger blast radius of `reboot`), so log + continue if it fails.
    if systemctl is-enabled --quiet litclock-firstboot.service 2>/dev/null; then
        : # already enabled, nothing to do
    elif sudo systemctl enable litclock-firstboot.service 2>/dev/null; then
        log "Re-enabled litclock-firstboot.service for fallback boot"
    else
        log "Failed to re-enable litclock-firstboot.service — fallback may not engage"
    fi
fi

NEW_COUNT=$((COUNT + 1))
# Best-effort write — if the state dir doesn't exist yet (pre-M5 install
# without the tmpfiles.d entry), fall back to sudo to bootstrap.
mkdir -p "$STATE_DIR" 2>/dev/null || sudo mkdir -p "$STATE_DIR" 2>/dev/null || true
if ! echo "$NEW_COUNT" > "$COUNTER_FILE" 2>/dev/null; then
    echo "$NEW_COUNT" | sudo tee "$COUNTER_FILE" >/dev/null 2>&1 || true
fi

log "WiFi unreachable, rebooting (attempt ${NEW_COUNT}/${MAX_REBOOTS})"
"$REBOOT_CMD"
