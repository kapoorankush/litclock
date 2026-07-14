#!/bin/sh
# Record that a diagnostics section's data has been collected on this Pi,
# in a PERSISTENT JSON marker that survives reboot (#445).
#
# Replaces the v0.214.4 "is the tmpfs IP marker present right now" predicate
# with "has this section EVER been collected" — kills the ~5-10s post-reboot
# grey "Not yet collected" flicker on /diagnostics (the marker on /run is
# wiped at every boot; this one on /var/lib is not).
#
# Usage:  litclock-mark-collected.sh <section-key>     # network | time-location
#
# Marker schema (one ISO-8601 UTC timestamp per collected section):
#   { "network": "2026-06-13T15:54:00+00:00",
#     "time-location": "2026-06-13T15:51:00+00:00" }
#
# Concurrency: the NM dispatcher (writes `network`, as root) and the IP-geo
# resolver (writes `time-location`, as pi) can race on the same file. We
# read-modify-write under flock on a sidecar lock so neither clobbers the
# other's key. The Python writer (src/collected_marker.py) uses the same
# file + lock + format — tests/test_collected_marker.py pins the parity.
#
# Permissions note: /var/lib/litclock is `0755 pi pi` (systemd-tmpfiles), so
# pi writes directly and root (this script, run from the dispatcher) writes
# fine too. The marker is chmod 0644 so control_server (pi) can always read
# it regardless of which user wrote it. The lock is opened READ-ONLY for
# flock, so a root-owned lock never blocks the pi writer and vice-versa.
#
# Best-effort by contract: ALWAYS exits 0. Callers are an NM dispatcher and
# boot oneshots that must not fail on a marker hiccup — a missing marker just
# degrades to the legacy tmpfs check on the read side.
#
# Install: the git-tracked source lives in scripts/ (mode 0755) and is the
# install SOURCE only. #387 C1 installs a ROOT-OWNED copy to
# /usr/local/lib/litclock/litclock-mark-collected.sh (install.sh / update.sh /
# pi-gen); the NM dispatcher invokes THAT copy so a root run never executes a
# pi-writable script. The pi-side writer is the separate src/collected_marker.py.
#
# WARNING: do NOT wire anything to run the pi-writable repo copy as root — that
# reopens the exact pi->root vector #387 C1 closes. Only the installed root-owned
# copy may be executed with privilege.

set -u

KEY="${1:-}"
# Only the two real section keys are accepted — a typo'd key would silently
# create a junk entry the read side ignores, so fail closed (no-op).
case "$KEY" in
    network | time-location) ;;
    *) exit 0 ;;
esac

MARKER="${LITCLOCK_COLLECTED_MARKER:-/var/lib/litclock/.last-collected-marker.json}"
LOCK="$MARKER.lock"
DIR=$(dirname "$MARKER")

# Dir missing (sandbox / CI / pre-tmpfiles) — nothing we can durably write to.
[ -d "$DIR" ] || exit 0
command -v jq >/dev/null 2>&1 || exit 0

TS=$(date -u +%Y-%m-%dT%H:%M:%S+00:00 2>/dev/null) || exit 0

_write() {
    # #387 C2: this script runs as ROOT (invoked by the NM dispatcher) and the
    # marker lives in the 0755 pi pi /var/lib/litclock dir, so guard every root
    # touch against a pi-planted symlink. Drop a symlink at $MARKER rather than
    # reading or renaming through it.
    if [ -L "$MARKER" ]; then
        rm -f "$MARKER" 2>/dev/null || true
    fi
    existing='{}'
    if [ -f "$MARKER" ] && [ ! -L "$MARKER" ]; then
        existing=$(cat "$MARKER" 2>/dev/null) || existing='{}'
        [ -n "$existing" ] || existing='{}'
    fi
    # mktemp creates with O_EXCL + a random name, so a pre-planted symlink at a
    # predictable path (the old "$MARKER.tmp.$$" was guessable in this
    # pi-writable dir) can no longer redirect this root write.
    tmp=$(mktemp "$DIR/.last-collected.XXXXXX" 2>/dev/null) || return
    # `if type=="object"` rebuilds {} from a malformed/non-object existing
    # file rather than letting jq error — a corrupt marker self-heals to a
    # clean one carrying just this key.
    if printf '%s' "$existing" | jq --arg k "$KEY" --arg ts "$TS" \
        'if type=="object" then .[$k]=$ts else {($k):$ts} end' > "$tmp" 2>/dev/null; then
        chmod 0644 "$tmp" 2>/dev/null || true
        # Atomic replace so the unlocked reader never sees a torn file. Works
        # regardless of the old file's owner (rename perms are on the dir,
        # which is pi-owned; root bypasses).
        mv -f "$tmp" "$MARKER" 2>/dev/null || rm -f "$tmp" 2>/dev/null || true
    else
        rm -f "$tmp" 2>/dev/null || true
    fi
}

# Ensure the lock node exists, then flock a READ-ONLY fd on it (read open
# needs no write perm, so cross-owner flock always works). touch failure
# (e.g. mtime update on a root-owned lock by pi) is harmless — the node
# already exists and the read-open below still succeeds.
touch "$LOCK" 2>/dev/null || true
if command -v flock >/dev/null 2>&1 && [ -e "$LOCK" ]; then
    # Only write UNDER the lock. If the lock fd can't be opened or the lock
    # can't be taken within 5s, SKIP — do NOT fall back to an unlocked write,
    # which could interleave with the other writer's read-modify-write and
    # drop its key (the Python writer likewise returns False on lock failure).
    # Best-effort: a skipped write just leaves the marker as-is; the next
    # event re-records it.
    ( flock -w 5 9 2>/dev/null || exit 0; _write ) 9<"$LOCK" 2>/dev/null || true
else
    # No flock binary at all (not expected on the Pi — util-linux ships it):
    # degrade to an unlocked write rather than never recording collection.
    _write
fi

exit 0
