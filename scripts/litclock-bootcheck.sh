#!/bin/bash
#
# LitClock — Last-Known-Good (LKG) auto-revert consumer (follow-up to #209).
#
# The #209 writer (litclock-lkg-record.sh) records the last SHA that actually
# rendered to /var/lib/litclock/lkg-sha. THIS script is the consumer: it
# detects a persistent post-update brick and self-heals the device without
# SSH or an SD reflash.
#
# Driven by litclock-bootcheck.timer (OnBootSec=12min + OnActiveSec, one
# decisive check per boot). Runs as pi; the only privileged actions are the
# scoped `systemctl reboot` and `systemctl start --no-block
# litclock-update.service` grants already in sudoers/020_litclock-control.
#
# ── HEALTH SIGNAL ────────────────────────────────────────────────────
# "Did the clock paint at all since THIS boot?" /run is tmpfs (wiped every
# boot), so /run/litclock/heartbeat existing with an mtime at/after boot
# means literary_clock.py completed at least one epd.display() this boot.
# The signal is network-independent (weather is optional), so a merely
# offline-but-healthy clock still heartbeats and is never reverted.
#
# ── STATE MACHINE ────────────────────────────────────────────────────
#
#   timer fires (boot + grace)
#        │
#        ▼
#   update in progress? ──yes──► exit 0 (defer to next tick)
#        │ no
#        ▼
#   post-update grace fresh? ──yes──► exit 0 (new code still settling)
#        │ no
#        ▼
#   heartbeat since boot? ──yes──► clear boot-fail-count (if set),
#        │ no                       bootcheck-recovering + gave-up, exit 0 (HEALTHY)
#        ▼
#   already gave up? ──yes──► exit 0 (terminal — no repaint/re-eval every poll)
#        │ no
#        ▼
#   boot-fail-count += 1
#        │
#        ├─ < THRESHOLD ─────────► sudo systemctl reboot (retry; a single
#        │                          failed render may be transient — auto-
#        │                          reboot confirms persistence without the
#        │                          user power-cycling a dead-looking clock)
#        │
#        └─ >= THRESHOLD
#              │
#              ├─ already recovering? ──yes──► GIVE UP: mark + best-effort
#              │                                splash, stop (LKG also bad /
#              │                                dead hardware → no reboot loop)
#              │
#              ├─ lkg-sha valid? ──no──► GIVE UP (no recovery target)
#              │
#              └─ yes ► pin rollback-target=lkg, blocked-sha=HEAD, trigger
#                        litclock-update.service; ON trigger success mark
#                        bootcheck-recovering + clear counter (rollback mode
#                        does the COMPLETE install: git+submodules+venv+units+smoke)
#
# Reboot budget is bounded: ~2 reboots to reach the first revert, the
# rollback (no reboot), then ~2 more before give-up. Dead e-ink hardware
# converges to the give-up state instead of looping forever.
#
# ── RESIDUAL LIMITS (documented) ─────────────────────────────────────
#   * If the release that ADDS bootcheck is itself so broken that bootcheck
#     or systemd can't run, no in-tree agent can self-heal (SD reflash).
#     update.sh (the recovery island) is more stable than this script for
#     exactly this reason — recovery is delegated to it, not done here.
#   * Corrupt/missing images/ on a normal boot reads as a failed boot;
#     reverting code won't fix missing data, but the revert is harmless and
#     the case is rare (needs a corrupt extract with no pending update).
#   * Health is "painted since THIS boot", not "still rendering." A release
#     that paints the first frame then dies every subsequent minute reads as
#     healthy and is NOT reverted — that is a mid-uptime failure, a different
#     class than a boot-time brick. This is deliberate: keying on continuous
#     freshness would let a marginal-but-usable Pi (an occasional >180s render
#     under memory pressure) trip an auto-reboot on a fundamentally healthy
#     device, which is worse than missing this rare class. The update.sh smoke
#     test is the first line of defense against a render that crashes.

set -uo pipefail

if [ -t 1 ]; then
    YELLOW='\033[1;33m'; NC='\033[0m'
else
    YELLOW=''; NC=''
fi
log_warn() { printf "%b[WARN]%b %s\n" "${YELLOW}" "${NC}" "$1" >&2; }
log_info() { printf "[INFO] %s\n" "$1"; }

INSTALL_DIR="${LITCLOCK_DIR:-/home/pi/litclock}"
STATE_DIR="${LITCLOCK_STATE_DIR:-/var/lib/litclock}"
LKG_SHA_FILE="$STATE_DIR/lkg-sha"
GRACE_FILE="$STATE_DIR/post-update-grace-until"
BOOT_FAIL_COUNT_FILE="$STATE_DIR/boot-fail-count"
BOOTCHECK_RECOVERING_FILE="$STATE_DIR/bootcheck-recovering"
GAVE_UP_FILE="$STATE_DIR/bootcheck-gave-up"
ROLLBACK_TARGET_FILE="$STATE_DIR/rollback-target"
BLOCKED_SHA_FILE="$STATE_DIR/blocked-sha"
HEARTBEAT_FILE="${LITCLOCK_HEARTBEAT_FILE:-/run/litclock/heartbeat}"
UPDATE_LOCK_FILE="${LITCLOCK_UPDATE_LOCK_FILE:-/var/lib/litclock/update.lock}"

# Tunables (env-overridable for tests).
GRACE_SECONDS="${LITCLOCK_LKG_GRACE_SECONDS:-900}"          # 15-min post-update soak
THRESHOLD="${LITCLOCK_BOOTCHECK_THRESHOLD:-3}"             # consecutive failed boots
# Boot epoch (seconds). /proc/stat btime is the wall-clock boot time; a
# heartbeat mtime >= this proves the clock painted THIS boot, not a stale
# tmpfs artifact (belt-and-suspenders — tmpfs is wiped on boot anyway).
BOOT_EPOCH="${LITCLOCK_BOOT_EPOCH:-$(awk '/^btime/{print $2}' /proc/stat 2>/dev/null || echo 0)}"
# Privileged actions, injectable so tests never actually reboot / start units.
REBOOT_CMD="${LITCLOCK_BOOTCHECK_REBOOT_CMD:-sudo systemctl reboot}"
UPDATE_TRIGGER_CMD="${LITCLOCK_BOOTCHECK_UPDATE_CMD:-sudo systemctl start --no-block litclock-update.service}"
# Best-effort give-up splash (shares the possibly-broken venv/display stack,
# so it may not paint — the persistent bootcheck-gave-up marker is the
# reliable signal). No-op by default; wired by the systemd unit.
SPLASH_CMD="${LITCLOCK_BOOTCHECK_SPLASH_CMD:-}"

# Shared atomic state helpers (atomic_write_file, atomic_remove_file).
_THIS_SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$_THIS_SCRIPT_DIR/lib/state.sh"

# ── Preflight ────────────────────────────────────────────────────────
if ! git -C "$INSTALL_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    log_warn "$INSTALL_DIR is not a git repo — nothing to check"
    exit 0
fi
if [ ! -d "$STATE_DIR" ]; then
    sudo mkdir -p "$STATE_DIR" 2>/dev/null || mkdir -p "$STATE_DIR" 2>/dev/null || {
        log_warn "Could not create $STATE_DIR"; exit 0
    }
fi

# ── Gate 1: defer while an update is in progress ─────────────────────
# Acquire the SAME lock update.sh uses. We hold it only for the read +
# state-write below, then RELEASE before triggering litclock-update.service
# (update.sh must be able to re-acquire it). flock absent (CI) → skip gate.
_LOCK_FD=""
if command -v flock >/dev/null 2>&1; then
    exec 9>"$UPDATE_LOCK_FILE" 2>/dev/null && _LOCK_FD=9
    if [ -n "$_LOCK_FD" ] && ! flock -n 9; then
        log_info "update in progress (lock held) — deferring to next tick"
        exit 0
    fi
fi
_release_lock() {
    [ -n "$_LOCK_FD" ] || return 0
    flock -u 9 2>/dev/null || true
    exec 9>&- 2>/dev/null || true
    _LOCK_FD=""
}

now=$(date +%s)

# ── Gate 2: post-update grace ────────────────────────────────────────
if [ -f "$GRACE_FILE" ]; then
    grace_age=$(( now - $(stat -c %Y "$GRACE_FILE" 2>/dev/null || echo "$now") ))
    if [ "$grace_age" -lt "$GRACE_SECONDS" ]; then
        log_info "post-update grace active (${grace_age}s < ${GRACE_SECONDS}s) — deferring"
        _release_lock; exit 0
    fi
fi

# ── Health: did the clock paint since this boot? ─────────────────────
healthy=0
if [ -f "$HEARTBEAT_FILE" ]; then
    hb_mtime=$(stat -c %Y "$HEARTBEAT_FILE" 2>/dev/null || echo 0)
    if [ "$hb_mtime" -ge "$BOOT_EPOCH" ]; then
        healthy=1
    fi
fi

if [ "$healthy" -eq 1 ]; then
    # HEALTHY — clear any prior failure state (including a stale give-up marker,
    # so a device that recovers by other means resumes normal monitoring). Gate
    # the counter removal on a non-empty file so a healthy device writes to the
    # SD card at most once.
    if [ -s "$BOOT_FAIL_COUNT_FILE" ]; then
        log_info "clock healthy — clearing failed-boot state"
        atomic_remove_file "$BOOT_FAIL_COUNT_FILE"
    fi
    atomic_remove_file "$BOOTCHECK_RECOVERING_FILE"
    atomic_remove_file "$GAVE_UP_FILE"
    _release_lock; exit 0
fi

# ── Terminal give-up ─────────────────────────────────────────────────
# Recovery was already exhausted this cycle (LKG also bad / hardware dead).
# Do NOTHING on subsequent unhealthy polls — otherwise the 5-min timer would
# re-run the (possibly broken) splash + rewrite state forever. The healthy
# branch above clears bootcheck-gave-up, so a device that later comes back
# resumes normal operation.
if [ -f "$GAVE_UP_FILE" ]; then
    log_info "bootcheck already gave up (recovery exhausted) — no further action"
    _release_lock; exit 0
fi

# ── Unhealthy boot: increment the persistent counter ─────────────────
count=0
if [ -s "$BOOT_FAIL_COUNT_FILE" ]; then
    count=$(tr -cd '0-9' < "$BOOT_FAIL_COUNT_FILE" 2>/dev/null || echo 0)
    [ -z "$count" ] && count=0
fi
count=$(( count + 1 ))
if ! atomic_write_file "$BOOT_FAIL_COUNT_FILE" "$count"; then
    log_warn "could not persist boot-fail-count — aborting this tick to avoid an unbounded loop"
    _release_lock; exit 0
fi
log_warn "no heartbeat since boot — failed boot #${count}/${THRESHOLD}"

# ── Below threshold: reboot to retry ─────────────────────────────────
if [ "$count" -lt "$THRESHOLD" ]; then
    log_warn "rebooting to retry (a single failed render may be transient)"
    _release_lock
    $REBOOT_CMD || log_warn "reboot command failed: $REBOOT_CMD"
    exit 0
fi

# ── At threshold ─────────────────────────────────────────────────────
give_up() {
    local reason="$1"
    log_warn "GIVING UP: $reason"
    atomic_write_file "$GAVE_UP_FILE" "$reason"
    if [ -n "$SPLASH_CMD" ]; then
        $SPLASH_CMD 2>/dev/null || log_warn "give-up splash failed (best-effort): $SPLASH_CMD"
    fi
    _release_lock
    exit 0
}

# Already tried a rollback this cycle and the recovered code ALSO failed
# THRESHOLD times → the LKG is bad too, or the hardware is dead. Stop.
if [ -f "$BOOTCHECK_RECOVERING_FILE" ]; then
    give_up "recovered code still failing after rollback (LKG bad or hardware fault)"
fi

# Resolve the recovery target (read_sha_file validates 40-hex + normalizes).
lkg=$(read_sha_file "$LKG_SHA_FILE")
if [ -z "$lkg" ]; then
    give_up "no valid last-known-good SHA to revert to"
fi
if ! git -C "$INSTALL_DIR" cat-file -e "${lkg}^{commit}" 2>/dev/null; then
    give_up "last-known-good SHA $lkg is not present in the local repo"
fi

HEAD_SHA=$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null || echo "")

log_warn "reverting to last-known-good $lkg via update.sh rollback mode"
# Pin the recovery target + suppress the bad release, then hand off to the
# installer we trust. update.sh does the COMPLETE, privileged install.
# NOTE: rollback-target + blocked-sha are OWNED by update.sh from here on — it
# consumes rollback-target and clears the recovery state in its Phase 7 (and
# deliberately KEEPS blocked-sha in rollback mode so the weekly timer won't
# reinstall the brick). bootcheck never clears them except on a healthy boot.
atomic_write_file "$ROLLBACK_TARGET_FILE" "$lkg"
if [[ "$HEAD_SHA" =~ ^[0-9a-f]{40}$ ]]; then
    atomic_write_file "$BLOCKED_SHA_FILE" "$HEAD_SHA"
fi

# Release the lock so update.sh can acquire it, then trigger the rollback.
# Mark bootcheck-recovering + clear the counter ONLY if the trigger actually
# fires — if `systemctl start` fails, the next threshold must RETRY the
# rollback, not see the recovering marker and give up prematurely. Leaving the
# counter at threshold means the next 5-min tick re-triggers promptly.
_release_lock
if $UPDATE_TRIGGER_CMD; then
    atomic_write_file "$BOOTCHECK_RECOVERING_FILE" ""
    atomic_remove_file "$BOOT_FAIL_COUNT_FILE"
else
    log_warn "could not trigger litclock-update.service ($UPDATE_TRIGGER_CMD) — will retry next cycle"
fi
exit 0
