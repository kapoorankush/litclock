#!/bin/bash
#
# LitClock — Last-Known-Good (LKG) writer (heartbeat-gated)
#
# Records the current HEAD SHA to /var/lib/litclock/lkg-sha when three
# gates all pass. Driven by litclock-lkg.timer (every 5 minutes after a
# 10-minute boot grace). Fast oneshot — exits in ~50ms when any gate
# blocks the write.
#
# Issue #241 — the previous design used `sleep $HEARTBEAT_SECONDS` plus a
# Requisite=litclock.service systemd dep. Both were broken: parallel-start
# meant the requisite check ran before litclock was active, and Type=oneshot
# litclock services almost never returned is-active=success 10 minutes
# later. The polling rewrite has no sleep, no is-active check, no race.
#
#   GATE 1: post-update grace?       (mtime of post-update-grace-until < 900s)
#   GATE 2: fresh render heartbeat?  (mtime of /run/litclock/heartbeat < 180s)
#   GATE 3: HEAD == recorded lkg?    (skip if already recorded; cheap idempotency)
#                                       │
#                                  (all pass)
#                                       ▼
#                                    write lkg-sha   (atomic: .tmp + mv)
#
# Bootcheck/revert is a separate follow-up (issue #241 → bootcheck-revert)
# and is intentionally NOT shipped here. This script is observability +
# substrate; consumption lands in its own PR after we have field data.

set -uo pipefail

if [ -t 1 ]; then
    YELLOW='\033[1;33m'
    NC='\033[0m'
else
    YELLOW=''; NC=''
fi

log_warn() { printf "%b[WARN]%b %s\n" "${YELLOW}" "${NC}" "$1" >&2; }
log_info() { printf "[INFO] %s\n" "$1"; }

INSTALL_DIR="${LITCLOCK_DIR:-/home/pi/litclock}"
STATE_DIR="${LITCLOCK_STATE_DIR:-/var/lib/litclock}"
LKG_SHA_FILE="$STATE_DIR/lkg-sha"
GRACE_FILE="$STATE_DIR/post-update-grace-until"
HEARTBEAT_FILE="${LITCLOCK_HEARTBEAT_FILE:-/run/litclock/heartbeat}"

# Tunables (env-overridable for tests).
GRACE_SECONDS="${LITCLOCK_LKG_GRACE_SECONDS:-900}"        # 15 min post-update soak
HEARTBEAT_MAX_AGE_SECONDS="${LITCLOCK_LKG_HEARTBEAT_MAX_AGE_SECONDS:-180}"  # 3 min

# Source shared atomic helpers (atomic_write_file, atomic_remove_file).
_THIS_SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$_THIS_SCRIPT_DIR/lib/state.sh"

# Pre-flight: we need a git repo. On a dev box this exits cleanly.
if ! git -C "$INSTALL_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    log_warn "$INSTALL_DIR is not a git repo — nothing to record"
    exit 0
fi

# Ensure state dir exists.
if [ ! -d "$STATE_DIR" ]; then
    sudo mkdir -p "$STATE_DIR" 2>/dev/null || mkdir -p "$STATE_DIR" 2>/dev/null || {
        log_warn "Could not create $STATE_DIR"
        exit 0
    }
fi

now=$(date +%s)

# GATE 1: post-update grace marker. update.sh Phase 7 touches this empty
# file after a successful update; for the next GRACE_SECONDS we refuse to
# promote a SHA so the smoke test isn't the only "this works" signal.
if [ -f "$GRACE_FILE" ]; then
    grace_mtime=$(stat -c %Y "$GRACE_FILE" 2>/dev/null || echo 0)
    grace_age=$(( now - grace_mtime ))
    if [ "$grace_age" -lt "$GRACE_SECONDS" ]; then
        log_info "post-update grace active (${grace_age}s < ${GRACE_SECONDS}s) — skip"
        exit 0
    fi
fi

# GATE 2: render heartbeat. literary_clock.py touches this after every
# successful epd.display() + epd.sleep() in production. If it's stale, the
# clock isn't actually rendering — don't promote whatever HEAD is.
if [ ! -f "$HEARTBEAT_FILE" ]; then
    log_info "heartbeat $HEARTBEAT_FILE missing — clock has not rendered yet, skip"
    exit 0
fi
hb_mtime=$(stat -c %Y "$HEARTBEAT_FILE" 2>/dev/null || echo 0)
hb_age=$(( now - hb_mtime ))
if [ "$hb_age" -ge "$HEARTBEAT_MAX_AGE_SECONDS" ]; then
    log_info "heartbeat stale (${hb_age}s ≥ ${HEARTBEAT_MAX_AGE_SECONDS}s) — skip"
    exit 0
fi

# GATE 3: idempotency. If lkg-sha already matches HEAD, do nothing.
HEAD_SHA=$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null)
if [ -z "$HEAD_SHA" ]; then
    log_warn "could not resolve HEAD — skip"
    exit 0
fi
if [ -f "$LKG_SHA_FILE" ]; then
    # read_sha_file (lib/state.sh) validates + normalizes; a corrupt lkg-sha
    # reads as empty, so this run overwrites it with the good HEAD (self-heal).
    RECORDED=$(read_sha_file "$LKG_SHA_FILE")
    if [ "$RECORDED" = "$HEAD_SHA" ]; then
        # Already recorded — nothing to do, no log noise.
        exit 0
    fi
fi

# All gates pass — atomic write.
if atomic_write_file "$LKG_SHA_FILE" "$HEAD_SHA
"; then
    log_info "recorded LKG: $HEAD_SHA"
else
    log_warn "atomic_write_file failed for $LKG_SHA_FILE"
fi

exit 0
