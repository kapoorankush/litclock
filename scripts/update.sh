#!/bin/bash
#
# LitClock Update Script
#
# Pulls the latest *blessed* code (latest GitHub Release, not origin/master)
# and applies changes to an existing installation. Non-interactive,
# idempotent, preserves user config. Run by hand OR by the weekly
# litclock-update.timer — both paths share the same flow.
#
#   Timer fires  ─► resolve_target_sha() ──► SHA? ──(no)──► exit 0, no mutation
#                                              │
#                                            (yes)
#                                              ▼
#   Phase 1  stop litclock.timer; clear /var/lib/litclock/lkg-sha
#   Phase 2  save OLD_SHA, git fetch --tags, git reset --hard <target>
#   Phase 2b cleanup stale files; sync obsolete systemd units
#   Phase 2c download_images.sh (graceful-offline)
#   Phase 3  merge env.sh.sample vars into env.sh
#   Phase 4  venv hash-gate → pip install if hash changed
#                  │
#   Phase 4.5      smoke: $PYTHON src/literary_clock.py --dry-run (60s hard timeout)
#                  │                      ╲
#                (pass)                  (fail)
#                  ▼                        ▼
#   Phase 5   install/enable systemd      git reset --hard $OLD_SHA
#             units; clear update-failed  rm .pip-packages-hash
#   Phase 6   chmod +x scripts            touch /var/lib/litclock/update-failed
#   Phase 7   start litclock.service      exit 1 (unit fails loud; timer unaffected)
#             start litclock.timer
#
# Graceful-offline: any failure before Phase 2 (resolver, git fetch) exits 0
# without mutating state — the clock keeps running on its pinned SHA and the
# next weekly tick tries again. Post-Phase-2 failures are loud.
#
# Usage: ./scripts/update.sh
#
# Run as the pi user (uses sudo only for systemd operations).
#

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ─── litclock-dev#334 Tension 3 — single-flight via flock ────────────────────────
# Two concurrent update.sh invocations (e.g. user taps Apply just as the
# weekly timer fires) would race on /run/litclock/update.status, the new
# /var/lib/litclock/last-update.json mirror, and the lkg-sha clear in
# Phase 1. Wrap the whole script body under an exclusive flock on
# /var/lib/litclock/update.lock — second runner exits immediately with a
# clear message rather than corrupting state.
#
# Pattern: if we don't already hold the lock (LITCLOCK_UPDATE_LOCK_HELD=1
# is set on the re-exec under flock), grab it and re-exec ourselves with
# the same args. flock -n returns 1 immediately if the lock is held; we
# convert that into a friendly message + exit 0 so the systemd timer
# treats it as "already running" not "broken".
#
# Best-effort: if we can't create the lock file (no permission on
# /var/lib/litclock yet, or running in a sandbox/CI without the state
# dir), skip the guard and run unguarded — pre-litclock-dev#334 behavior. The
# guard's job is to protect production; degrading to "no protection"
# in environments that can't host the lock is the right trade-off
# vs. failing the whole update.
LITCLOCK_UPDATE_LOCK_FILE="${LITCLOCK_UPDATE_LOCK_FILE:-/var/lib/litclock/update.lock}"
if [[ "${LITCLOCK_UPDATE_LOCK_HELD:-0}" != "1" ]] && command -v flock >/dev/null 2>&1; then
    _LITCLOCK_LOCK_DIR=$(dirname "$LITCLOCK_UPDATE_LOCK_FILE")
    if [[ ! -d "$_LITCLOCK_LOCK_DIR" ]]; then
        sudo mkdir -p "$_LITCLOCK_LOCK_DIR" 2>/dev/null \
            || mkdir -p "$_LITCLOCK_LOCK_DIR" 2>/dev/null \
            || true
    fi
    if [[ ! -e "$LITCLOCK_UPDATE_LOCK_FILE" ]]; then
        touch "$LITCLOCK_UPDATE_LOCK_FILE" 2>/dev/null \
            || sudo touch "$LITCLOCK_UPDATE_LOCK_FILE" 2>/dev/null \
            || true
        sudo chown pi:pi "$LITCLOCK_UPDATE_LOCK_FILE" 2>/dev/null || true
    fi
    # Re-check: only attempt flock if the file is actually present + readable.
    # Sandboxed / unprivileged environments may have no /var/lib/litclock —
    # in that case skip the guard, run unguarded (pre-litclock-dev#334 behavior).
    if [[ -e "$LITCLOCK_UPDATE_LOCK_FILE" ]]; then
        export LITCLOCK_UPDATE_LOCK_HELD=1
        # `flock -n -E 75 <file> <cmd>` — 75 is a custom exit status for
        # "lock held by another process" (default is 1, which collides
        # with normal command failures). On lock-held: print a friendly
        # message and exit 0 so the systemd timer treats it as "already
        # running" rather than "broken". On any other non-zero: bubble
        # up the inner script's exit code.
        # NOT `--close` (litclock-dev#835 review, rounds 8-10). Two hazards
        # sit on this one flag and they pull in opposite directions:
        #   * without it, every descendant inherits the lock descriptor, so
        #     a process leaked past the script's end (a hung network helper)
        #     keeps every later tick reporting "another update is in
        #     progress" until someone kills it;
        #   * with it, the lock lives only in this flock process, and systemd
        #     stops the whole control group at once — so a TERM kills flock
        #     first and the script's own signal cleanup below runs UNLOCKED,
        #     where a second updater (a PWA "update now") can start and race
        #     it (reproduced by the review with util-linux 2.39).
        # The inherited descriptor is the pre-existing behaviour and the
        # cleanup race is the worse of the two, so it stays. The leak side is
        # bounded elsewhere: the one gate that could leak a helper is under
        # coreutils `timeout` now, and the lock design itself is the follow-up
        # on litclock-dev#835.
        flock -n -E 75 "$LITCLOCK_UPDATE_LOCK_FILE" "$0" "$@"
        _rc=$?
        if [[ "$_rc" == "75" ]]; then
            log_warn "another update is in progress; try again later"
            exit 0
        fi
        exit "$_rc"
    else
        # Lock file couldn't be created (parent dir missing or unwritable
        # even after the sudo attempts above). Continue unguarded — but
        # log loud enough that anyone reading the journal can see we
        # degraded (review I3). Fresh-image flow + CI sandboxes hit this
        # path legitimately; on a real Pi it indicates broken provisioning.
        log_warn "concurrency guard skipped: could not flock $LITCLOCK_UPDATE_LOCK_FILE (parent dir missing or unwritable). Manual concurrent update.sh invocations are NOT serialized."
    fi
fi

INSTALL_DIR="${LITCLOCK_DIR:-/home/pi/litclock}"
PYTHON="$INSTALL_DIR/venv/bin/python3"
PIP="$INSTALL_DIR/venv/bin/pip"

# Load shared GitHub REST helpers (github_api_latest_release_tag, github_api_curl)
# and atomic state-file helpers (atomic_write_file, atomic_remove_file).
# Paths resolved relative to this script so the self-reexec path works.
_THIS_SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
if [[ -f "$_THIS_SCRIPT_DIR/lib/github_api.sh" ]]; then
    . "$_THIS_SCRIPT_DIR/lib/github_api.sh"
fi
# shellcheck source=/dev/null
. "$_THIS_SCRIPT_DIR/lib/state.sh"

# Update status file helpers (litclock-dev#245 M5). Optional source — older Pis on a SHA
# that predates M5 won't have lib/update_status.sh, so we tolerate it being
# absent and skip status writes via the no-op stubs below. F14 in plan: every
# post-M5 fire ships its own copy of this lib via the canonical scripts/.
if [[ -f "$_THIS_SCRIPT_DIR/lib/update_status.sh" ]]; then
    # shellcheck source=/dev/null
    . "$_THIS_SCRIPT_DIR/lib/update_status.sh"
else
    # No-op stubs so the rest of the script can call these unconditionally.
    update_status_init() { :; }
    update_status_set_to_version() { :; }
    update_status_set_phase() { :; }
    update_status_complete() { :; }
    update_status_failed_reverted() { :; }
    update_status_failed_unrecovered() { :; }
fi

# LitClock state dir. Marker-file catalog (incl. the LKG auto-revert set this
# script reads/writes: rollback-target, blocked-sha, boot-fail-count,
# bootcheck-recovering) lives in scripts/lib/state.sh — keep that the single list.
STATE_DIR="${LITCLOCK_STATE_DIR:-/var/lib/litclock}"
# NOTE: update.sh no longer references lkg-sha directly (LKG auto-revert,
# litclock-dev#209 follow-up). It is written only by litclock-lkg-record.sh and read only
# by litclock-bootcheck.sh; update.sh must NOT clear it (see Phase 1).
UPDATE_FAILED_FILE="$STATE_DIR/update-failed"
POST_UPDATE_GRACE_FILE="$STATE_DIR/post-update-grace-until"
# litclock-bootcheck (LKG auto-revert, follow-up to litclock-dev#209). bootcheck writes
# rollback-target when it has detected a persistent post-update brick and is
# routing recovery back through this installer; blocked-sha suppresses
# re-installing the release bootcheck reverted from; boot-fail-count +
# bootcheck-recovering are the failed-boot state a successful apply clears.
ROLLBACK_TARGET_FILE="$STATE_DIR/rollback-target"
BLOCKED_SHA_FILE="$STATE_DIR/blocked-sha"
BOOT_FAIL_COUNT_FILE="$STATE_DIR/boot-fail-count"
BOOTCHECK_RECOVERING_FILE="$STATE_DIR/bootcheck-recovering"
# litclock-dev#274 follow-up: marker written on Phase 3 flock-timeout (rc=75) so the
# Status hero can surface "env-vars merge skipped on last update" — the
# skip itself is correct (next weekly tick retries), but without this
# marker the user has no surface to see it happened. Marker is mtime-only;
# reader (control_server status route) clamps to `now - mtime < 86400` so
# the banner self-clears after a day even if the next update isn't run.
PHASE3_SKIPPED_FILE="$STATE_DIR/update-phase3-skipped"
# litclock-dev#245 M5 D6 — shared GH-API cache for /api/update/check + this script.
# 6h TTL. update.sh invalidates on Phase 7 success (deletes it) so the PWA
# shows "up to date" instantly post-update.
# litclock-dev#434 — this is a purely derived cache, so it lives on the /run/litclock
# tmpfs (kept off the SD card) rather than the persistent STATE_DIR. The
# default path AND the LITCLOCK_UPDATE_CHECK_CACHE full-path override are kept
# symmetric with src/control_server/update_state.py (DEFAULT_CACHE_FILE +
# cache_path()) so a harness that repoints one consumer repoints BOTH — the
# reader and the invalidator must never disagree on where the cache lives.
UPDATE_CHECK_CACHE_FILE="${LITCLOCK_UPDATE_CHECK_CACHE:-/run/litclock/update-check.json}"
# Pre-litclock-dev#434 installs kept this cache on the SD card at $STATE_DIR/update-check.json.
# Invalidated alongside the live file below so an in-place upgrade doesn't leave
# a stale flash-resident blob behind (one-time migration; rm -f is idempotent).
LEGACY_UPDATE_CHECK_CACHE_FILE="$STATE_DIR/update-check.json"
# litclock-dev#334 — persistent mirror of /run/litclock/update.status (state=complete).
# Written after update_status_complete validates, AFTER the EXIT trap is
# disarmed. Lets the Status hero "Last update" row survive the tmpfs clear
# of update.status at reboot during the 15-min LKG soak window AND the
# offline-graceful-exit window where Phase 1 already cleared lkg-sha but
# no new LKG was recorded yet.
LAST_UPDATE_FILE="$STATE_DIR/last-update.json"

# Resolve the target SHA for this update cycle.
# Path: /repos/.../tags → highest semver vX.Y.Z → git fetch <tag> → git rev-list -n 1 <tag>
# (Switched off /releases/latest in litclock-dev#247 — fine-grained PATs 404 on it for
# private repos. See scripts/lib/github_api.sh for the long version.)
#
# Emits the 40-char SHA on stdout when it succeeds.
# Emits an empty stdout and exit 0 on ANY failure (network, HTTP, parse,
# unknown tag) — the caller then falls back to the legacy origin/master
# reset path only when the resolver is unavailable because the lib wasn't
# sourced (fresh-image one-shot update on a pre-litclock-dev#209 Pi); otherwise a
# resolver failure is treated as graceful-offline and the whole script
# exits cleanly.
#
# `target_commitish` is deliberately NOT read here — for tag-push-created
# Releases GitHub stores the branch name (e.g. "master") there, which would
# silently flip the auto-update target to branch HEAD. Tag-by-name is the
# immutable source of truth.
# origin_repo_pair — emit "<owner> <repo>" derived from the origin remote.
#
# litclock-dev#721: the resolver used to hardcode the public pair, so a
# device whose origin is the DEVELOPMENT repo asked PUBLIC for its latest
# tag, then tried to fetch that tag from its own origin — where it has
# never existed — and exited cleanly. Every dev-image device was
# permanently pinned, and the checksum re-exec turned any update that
# touches update.sh into a half-application (reset done, Phases 2b-6
# skipped, because the re-exec'd script resolved a different, unfetchable
# target). Deriving the pair from origin is correct for BOTH repos by
# construction, which also keeps this file dev/public byte-portable.
#
# Accepts the two forms git actually writes: https://github.com/O/R(.git)
# and git@github.com:O/R(.git). Anything else falls back to the public
# pair — the pre-litclock-dev#721 behavior, right for the fleet and never worse.
origin_repo_pair() {
    local url owner_repo bare
    url=$(git remote get-url origin 2>/dev/null) || url=""
    # Trim surrounding whitespace — byte-parity with the Python resolver's
    # strip() in control_server/update_state.py (litclock-dev#728: the two
    # resolvers naming different repos is the bug class itself; a parity
    # test drives both from one URL table).
    url="${url#"${url%%[![:space:]]*}"}"
    url="${url%"${url##*[![:space:]]}"}"
    if [[ -z "$url" ]]; then
        # Distinct from the parse warning: git itself failed (missing
        # binary, not a repo). Debugging a wrong resolve starts here.
        printf '[resolve] warn: origin remote unreadable - using public pair\n' >&2
        printf '%s %s\n' "kapoorankush" "litclock"
        return
    fi
    owner_repo=""
    case "$url" in
        https://*)
            bare="$url"
            # Strip userinfo (https://user:token@github.com/O/R): a
            # credentialed origin must resolve to ITS pair — the fallback
            # would silently reproduce the litclock-dev#721 pinning for exactly the
            # origin shape a hand-provisioned PAT produces.
            if [[ "$bare" =~ ^https://[^/@]+@ ]]; then
                bare="https://${bare#https://*@}"
            fi
            case "$bare" in
                https://github.com/*)
                    owner_repo="${bare#https://github.com/}"
                    ;;
            esac
            ;;
        git@github.com:*)
            owner_repo="${url#git@github.com:}"
            ;;
    esac
    owner_repo="${owner_repo%/}"
    owner_repo="${owner_repo%.git}"
    # Exactly one slash, GitHub slug charset only, no dot-only components.
    # The charset gate keeps components with spaces / '?' / control chars
    # from reaching the API URL templates ('.'/'..' would path-traverse
    # api.github.com) — mirrored in the Python resolver (/review litclock-dev#729).
    if [[ "$owner_repo" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] \
        && [[ "${owner_repo%%/*}" != "." && "${owner_repo%%/*}" != ".." ]] \
        && [[ "${owner_repo##*/}" != "." && "${owner_repo##*/}" != ".." ]]; then
        printf '%s %s\n' "${owner_repo%%/*}" "${owner_repo##*/}"
    else
        # The fallback is only "never worse" for public-origin devices; on a
        # hand-modified origin it silently reproduces the litclock-dev#721 pinning — so
        # say which URL defeated the parser (/review litclock-dev#722), with any embedded
        # credential redacted before it can reach the journal (/review litclock-dev#729).
        printf '[resolve] warn: origin %s unparsed - using public pair\n' \
            "$(printf '%s' "$url" | sed -E 's|://[^/@]+@|://<redacted>@|g')" >&2
        printf '%s %s\n' "kapoorankush" "litclock"
    fi
}

resolve_target_sha() {
    # Lib absent → caller falls back to legacy path.
    if ! declare -F github_api_latest_release_tag >/dev/null 2>&1; then
        printf "[resolve] warn: github_api lib not available — caller will fall back\n" >&2
        return 0
    fi
    local tag _owner _repo
    read -r _owner _repo < <(origin_repo_pair)
    tag=$(github_api_latest_release_tag "$_owner" "$_repo")
    if [[ -z "$tag" ]]; then
        printf "[resolve] warn: no latest Release tag resolved\n" >&2
        return 0
    fi
    # Fetch ONLY the resolved tag, not the full tag set.
    #
    # Why this matters on a Pi: pi-gen builds the image from a shallow
    # clone, so the on-device .git is missing most of the project's
    # historical commits. A blanket all-tags fetch then has to backfill
    # every object reachable from every dev / image release tag (~30k+
    # objects) — on a Pi Zero 2W that exceeds the service's 120s
    # TimeoutStartSec and the update job is killed mid-resolver.
    #
    # --no-tags + an explicit refspec scopes the fetch to just the tag
    # we already named, which is at most a handful of commits ahead of
    # the device. ~100x faster on hardware in practice.
    #
    # The 30-second `timeout` is a hard ceiling so a stuck connection
    # or rogue ref-advert can never wedge the update past the resolver
    # phase. The whole resolver is graceful-offline (exit 0 + empty
    # stdout on any failure), so any timeout here just means
    # "skip this update tick — try again next week".
    timeout 30 git fetch --no-tags --quiet origin \
        "refs/tags/${tag}:refs/tags/${tag}" 2>/dev/null || true
    local sha
    sha=$(git rev-list -n 1 "$tag" 2>/dev/null)
    if [[ -z "$sha" ]]; then
        printf "[resolve] warn: could not resolve tag %s to a SHA (not present locally)\n" "$tag" >&2
        return 0
    fi
    printf "%s\n" "$sha"
}

# ─── Rename migration (author-clock → litclock) ──────────────────────
OLD_INSTALL_DIR="${AUTHOR_CLOCK_DIR:-/home/pi/author-clock}"

if [[ ! -d "$INSTALL_DIR" && -d "$OLD_INSTALL_DIR" ]]; then
    log_info "Migrating $OLD_INSTALL_DIR → $INSTALL_DIR..."

    # Stop old timer/service to avoid racing with the mv
    sudo systemctl stop authorclock.timer 2>/dev/null || true
    sudo systemctl stop authorclock.service 2>/dev/null || true

    # Rename directory
    mv "$OLD_INSTALL_DIR" "$INSTALL_DIR"

    # Venvs are NOT portable between paths — nuke it and the hash file
    rm -rf "$INSTALL_DIR/venv"
    rm -f "$INSTALL_DIR/.pip-packages-hash"

    # Migrate /etc/authorclock/ → /etc/litclock/ (preserves .setup-complete flag)
    if [[ -d /etc/authorclock ]]; then
        sudo mv /etc/authorclock /etc/litclock
    fi

    # Remove old systemd units (authorclock.service, authorclock.timer,
    # authorclock-firstboot.service, authorclock-splash.service, authorclock-shutdown.service)
    for old_unit in /etc/systemd/system/authorclock*.service \
                    /etc/systemd/system/authorclock*.timer; do
        [[ -f "$old_unit" ]] || continue
        old_name=$(basename "$old_unit")
        sudo systemctl stop "$old_name" 2>/dev/null || true
        sudo systemctl disable "$old_name" 2>/dev/null || true
        sudo rm -f "$old_unit"
    done
    sudo systemctl daemon-reload

    log_info "Migration complete"
fi

# ─── Pre-flight checks ───────────────────────────────────────────────

cd "$INSTALL_DIR" || { log_error "Install directory not found: $INSTALL_DIR"; exit 1; }

if [[ ! -d .git ]]; then
    log_error "$INSTALL_DIR is not a git repository"
    exit 1
fi

# Use the marker file, not `is-active` on the firstboot service: firstboot
# is Type=oneshot, so its state stays `active (exited)` permanently after a
# successful run. `is-active --quiet` would return true forever and gate
# every subsequent update. The marker is the same signal the systemd unit
# guards on (ConditionPathExists=/etc/litclock/.setup-complete).
if [[ ! -f /etc/litclock/.setup-complete ]]; then
    log_error "First-boot setup not yet complete. Complete setup first, then run update."
    exit 1
fi

# EPIC litclock-dev#383 PR2 (litclock-dev#388) migration (Option A). The new litclock.service is gated
# on /etc/litclock/.handoff-complete (the post-WiFi PWA handoff). Devices that
# provisioned BEFORE PR2 never ran the handoff flow, so that marker is absent —
# without this, the upgraded litclock.service would no-op on every timer tick
# and quotes would stop appearing (unacceptable for a Pi glued in its case).
#
# litclock-dev#675: the original reasoning was "we confirmed .setup-complete
# just above, so this device is past first-boot". That is FALSE during the
# handoff window, which is DEFINED as .setup-complete present and
# .handoff-complete absent. This is a completion path like any other, and
# design-review A2's rule applies to it: a clock that paints at the WRONG time
# is worse than no clock. handoff.py and litclock-handoff-fallback.sh both
# refuse when the timezone is unknown; this did not.
#
# It is reachable: the PWA is up during the handoff window and
# routes/updates.py has no handoff gate, and litclock-update.timer is
# Persistent=true with a stamp clone prep does not clear. Before litclock-dev#673 clones
# shipped with .handoff-complete already present so this was a no-op on every
# clone; now clones sit in the same window fresh flashes always have.
#
# THE DISCRIMINATOR IS THE INSTALLED UNIT, NOT A CLOCK. The obvious guard —
# "refuse when the timezone is unknown AND setup finished recently" — was tried
# and is wrong in both directions (/review, red team + Codex, independently):
#
#   * It EXPIRES exactly when it is needed. .handoff-complete can still be
#     missing an hour after setup ONLY when the timezone is unknown, because a
#     known timezone gets the marker written by the 120s auto-complete or the
#     10-minute fallback. So "marker missing AND old" is very nearly a
#     DEFINITION of "timezone unknown" — and litclock-update.timer carries
#     RandomizedDelaySec=7d, so the weekly tick lands inside a one-hour window
#     about 0.6% of the time. The guard would almost never apply.
#   * It depends on a clock this hardware does not have. The Pi has no RTC, so
#     pre-NTP system time can read EARLIER than the marker's mtime; the age goes
#     negative, compares as "recent", and the migration is refused on exactly
#     the long-provisioned device the condition was meant to protect. This file
#     already documents that hazard 1100 lines below (the pre-1970 stamp note).
#
# What actually identifies a pre-PR2 device is that its INSTALLED unit predates
# the gate. This block runs long before Phase 5 reinstalls units, so the old
# unit is still on disk and answers the question exactly, with no clock and no
# expiry. We migrate only when the device is POSITIVELY identified as pre-PR2;
# anything else honours the timezone refusal, indefinitely.
#
# litclock-dev#768 — THIS GUARD IS INERT WHENEVER THE OUTGOING SCRIPT ALREADY
# WROTE THE MARKER, which is every run of a release in [v0.214.0, v0.225.0).
# Not a defect in the guard; a consequence of where update.sh re-execs itself.
#
# The OUTGOING script runs first, in a different process, and in that window its
# copy of this same migration is UNCONDITIONAL. Cited against the PUBLIC tags,
# because that is the repository fielded devices track and therefore the script
# that actually runs on them — the development repo's own tags jump v0.222.0 -> v0.225.0
# and never saw a v0.224.x at all:
#
#   public v0.224.0 update.sh:313       if sudo touch /etc/litclock/.handoff-complete
#   public v0.224.0 update.sh:532       git reset --hard "$TARGET_SHA"
#   public v0.224.0 update.sh:543-552   re-exec guard, then exec the NEW update.sh
#
# So the marker is already present when this file loads and the `! -f` test
# below is false. The device gets the behaviour litclock-dev#675 calls unacceptable.
#
# THE WINDOW HAS A FLOOR, and the arm below depends on it. Releases before
# v0.214.0 have no handoff migration at all (`git show v0.212.2:scripts/update.sh`
# does not mention .handoff-complete), so a genuinely pre-PR2 device arrives with
# the marker ABSENT, this guard runs, and the handoff_pre_pr2 arm migrates it —
# which is the entire reason the INSTALLED_CLOCK_UNIT discriminator exists. Do
# not read "inert on old releases" as "that arm is dead code"; it is the arm
# that serves exactly the population it was written for.
#
# THE TRIGGER IS A TIMER TICK, NOT AN UPGRADE. The outgoing migration sits ABOVE
# the reachability gate (public v0.224.0 update.sh:320), above target resolution
# (:448) and above both graceful-offline `exit 0` arms (:480, :491). A device in
# that window writes the marker on its FIRST weekly tick, offline, with no
# release available and no version jump — so exposure is not limited to clocks
# taking a large hop, and the `sudo touch` failing (:316) is the only way the
# incoming guard stays live.
#
# It cannot be fixed by moving this block earlier: the old script's write happens
# in another process before any byte of this file runs. Closing it would mean
# REVOKING a marker — deleting .handoff-complete when the installed unit carries
# the gate and the timezone is unknown — which stops quotes on a fielded device
# and is a materially larger change than the refusal itself. Deliberately not
# done here; owner's call, tracked on litclock-dev#768.
#
# From v0.225.0 onward the outgoing script carries this guarded block itself, so
# every tick on a guarded release refuses correctly.
INSTALLED_CLOCK_UNIT="${LITCLOCK_INSTALLED_CLOCK_UNIT:-/etc/systemd/system/litclock.service}"

if [[ ! -f /etc/litclock/.handoff-complete ]]; then
    # tz-known proxy, shared with control_server/handoff.py and
    # litclock-handoff-fallback.sh: the IP-geo resolver sets the timezone and
    # writes lat/lon together, so populated coordinates mean it resolved.
    # Both keys, matching handoff.py's _has_location — the bash copies used to
    # check latitude alone, so an env.sh with lat but no lon was "unknown" to
    # the PWA and "known" here (/review).
    read_env_value() {
        local key="$1" line value
        line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$INSTALL_DIR/env.sh" 2>/dev/null | tail -n1)"
        value="${line#*=}"
        # Strip a trailing comment BEFORE stripping whitespace: `KEY= # unset`
        # is ordinary shell, and without this the comment text became the value
        # and read as a populated coordinate (/review, Codex).
        value="${value%%#*}"
        value="${value//\"/}"
        value="${value//\'/}"
        value="${value//[[:space:]]/}"
        printf '%s' "$value"
    }

    handoff_lat="$(read_env_value WEATHER_LATITUDE)"
    handoff_lon="$(read_env_value WEATHER_LONGITUDE)"

    # Positively pre-PR2: the unit is installed AND has no handoff gate.
    # A missing unit is NOT treated as pre-PR2 — quotes cannot paint without it
    # anyway, so refusing costs nothing and avoids starting a wrong-time clock
    # the moment Phase 5 installs the gated unit.
    handoff_pre_pr2=false
    if [[ -f "$INSTALLED_CLOCK_UNIT" ]] && ! grep -q "ConditionPathExists=/etc/litclock/.handoff-complete" "$INSTALLED_CLOCK_UNIT"; then
        handoff_pre_pr2=true
    fi

    if [[ -n "$handoff_lat" && -n "$handoff_lon" ]]; then
        handoff_reason="the timezone is known"
    elif [[ "$handoff_pre_pr2" == true ]]; then
        handoff_reason="this device predates the handoff (its installed litclock.service has no handoff gate)"
    else
        handoff_reason=""
    fi

    if [[ -z "$handoff_reason" ]]; then
        log_warn "Not completing the handoff: the timezone is not known (WEATHER_LATITUDE/LONGITUDE are empty) and this device is not a pre-handoff install."
        log_warn "Leaving the setup splash up. A clock painting at the WRONG time is worse than no clock — finish setup in the app (it offers your browser's timezone), and quotes will start."
    elif sudo touch /etc/litclock/.handoff-complete; then
        # Canonical phrasing, shared with control_server/handoff.py and
        # litclock-handoff-fallback.sh (litclock-dev#646): this is another completion
        # path, and an operator greps ONE string to find out how a device's
        # handoff completed. tests/test_control_server_handoff.py pins that the
        # spellings agree.
        log_info "handoff: completed via the in-place updater (pre-PR2 migration) — $handoff_reason"
    else
        log_warn "Could not create .handoff-complete marker — quotes may pause until the fallback timer fires (~10 min after next boot)"
    fi
fi

# Remote reachability is required for a normal update (we fetch the Release),
# but NOT for a bootcheck rollback: the recovery target is a local LKG SHA
# bootcheck already verified is present, so a git reset needs no network. A
# device bricked by a bad update that also broke connectivity must still be
# able to self-heal, so skip this gate when a rollback is pending.
# Bounded (litclock-dev#835 review): on the Phase 2 re-exec path
# litclock.timer is already stopped by the time the new script gets here,
# and the traps below are installed after this gate (they need the status
# init, and initialising before the gate would stamp an offline tick as
# unrecovered), so an ls-remote that hung would hold a stopped clock with no
# trap armed.
_LS_REMOTE_TIMEOUT_S="${LITCLOCK_LS_REMOTE_TIMEOUT_S:-30}"
# Bounded by coreutils `timeout`, which is the right tool and the one the
# rest of this script already uses: without --foreground it makes itself
# the leader of a new process group and, on expiry, signals that GROUP —
# TERM, then KILL after -k — so git's network helper dies with it however
# it re-parents or ignores TERM, and nothing that inherited the updater's
# flock descriptor can outlive the deadline. The litclock-dev#835 review
# tried three hand-rolled bash watchdogs here (per-pid kills, a
# file-published pgid, a self-reaping subshell) and broke each one under
# injected scheduling: an unprotected interval before a trap, a truncated
# pgid becoming a broadcast kill, a KILL landing on the sole cleanup owner.
# The lesson is that this is coreutils' job.
# Residual, stated: `timeout` bounds the MONITORED command's lifetime, not
# what it leaves behind. If git exited while a helper of its own lingered
# — on its own before the deadline, or because git honoured the TERM at
# expiry while its helper ignored it (timeout returns as soon as its direct
# child is gone and never reaches the -k KILL) — that helper is not
# timeout's to kill. git waits for its helpers and git-remote-https honours
# TERM, so both shapes are synthetic here; the consequence that would
# matter (a leaked descendant holding the update lock) is the lock-design
# follow-up on litclock-dev#835, see the flock comment at the top.
_remote_reachable() {
    timeout -k 5 "$_LS_REMOTE_TIMEOUT_S" git ls-remote --exit-code origin &>/dev/null
}
if [[ ! -f "$ROLLBACK_TARGET_FILE" ]] && ! _remote_reachable; then
    log_error "Cannot reach remote repository. Check network connectivity."
    exit 1
fi

# On re-exec, the original SHA is passed as $1 to preserve the version diff
OLD_SHA="${1:-$(git rev-parse --short HEAD)}"

# litclock-dev#245 M5 D9 — initialize the status-file cache so subsequent
# update_status_set_phase / *_complete / *_failed_* calls inherit started_at +
# from_version. Done unconditionally; the helper is a no-op when the lib is
# absent (pre-M5 self-reexec path).
update_status_init "$OLD_SHA"

# litclock-dev#835 round-4 review considered installing these traps ABOVE the
# `git ls-remote` gate (on the re-exec path Phase 1 has already stopped
# litclock.timer by then). Rejected: the EXIT trap's stamp needs
# update_status_init, and initialising the status before the gate would turn
# an ordinary offline tick into a "manual recovery needed" stamp. The gate
# is bounded to 30s instead, so the un-trapped window on the re-exec path is
# at most that, and the only things that signal us inside it are the reset
# units, which stop the timer themselves.
# litclock-dev#245 M5 D4 / F9 — install a finalize-on-exit trap. If the script exits
# normally (after update_status_complete / update_status_failed_reverted),
# _LITCLOCK_UPDATE_FINALIZED=1 is set and the trap is a no-op. Otherwise
# (uncaught error, kernel signal, systemd TimeoutStartSec sending SIGTERM)
# the trap stamps state=failed_unrecovered so the PWA never sees a stranded
# state=running file. SIGKILL escapes uncatchable traps (acceptable — A8's
# 90s manual-help fallback covers).
#
# litclock-dev#835 — two changes, both found by the v0.227.0 port review's
# adversarial passes and reproduced under bash before being fixed:
#
#   1. A SIGNAL TERMINATES THE RUN. One handler used to serve EXIT and
#      TERM/INT/HUP alike and never called exit. Bash resumes the script
#      after a signal handler returns, and this script has no `set -e`, so
#      systemd's SIGTERM did not stop the update: the interrupted command
#      died, the trap stamped failed_unrecovered, and the script CONTINUED —
#      the next update_status_set_phase rewrote the status to running, Phase
#      5 copied units and reloaded systemd, Phase 7 restarted the clock and
#      stamped complete — all inside systemd's stop window, until SIGKILL
#      landed wherever it happened to land. The "dark panel" of litclock-dev#835 was
#      that SIGKILL, TimeoutStopSec after the SIGTERM. The signal handler now
#      exits (143, the conventional SIGTERM status); the EXIT trap does the
#      rest, exactly once, after the mutation has stopped.
#
#   2. THE EXIT TRAP RE-ARMS litclock.timer on the unfinalized path, before
#      stamping. Phase 1 stops the timer and Phase 7 restarts it; the named
#      failure arms in between restart it by hand, and anything else — the
#      signal path above included — now relies on this. Bounded and
#      non-interactive (`timeout 10 sudo -n ... --no-block`): a trap that
#      blocked on a wedged manager or a password prompt would hold the stop
#      job past TimeoutStopSec and lose the status stamp with it. This is a
#      best-effort re-arm, not a recovery: a painter started on a torn tree
#      fails at import and the panel keeps its last frame, which is no worse
#      than a stopped timer, and the status file already says manual
#      recovery is needed. It is NOT covered by the bootcheck/LKG chain —
#      that chain asks "did this BOOT paint once", and a mid-uptime update
#      has always painted before it ran.
_LITCLOCK_UPDATE_FINALIZED=0
_LITCLOCK_UPDATE_CLEANED=0
# The one cleanup, IDEMPOTENT, reachable from both the EXIT trap and the
# signal handler. Round-3 review (Codex) measured the last gap in the
# previous shape: the EXIT trap began with `trap '' TERM INT HUP`, but that
# is not atomic with ENTERING the trap — a TERM landing between bash starting
# EXIT processing and that first statement fired the exiting signal handler
# inside the trap, and the nested exit does not restart EXIT processing
# (40/1000 runs in a stress probe lost both the re-arm and the stamp). So
# the signal handler no longer relies on EXIT being entered again: it runs
# this cleanup itself, and the flag makes the second entry a no-op.
_litclock_update_cleanup() {
    # FIRST statement: mask signals, before the flag is even read. Round-5
    # review (Codex) injected a TERM between "flag set" and "signals masked"
    # in the previous order: the handler called cleanup, saw "already
    # cleaned", and exited with neither re-arm nor stamp. Masked first, a
    # TERM that lands before this line reaches the handler (whose own first
    # statement masks too) and runs the whole cleanup with the flag still 0;
    # one that lands after it is simply ignored. That second TERM is not
    # hypothetical: the unit's main PID is the OUTER flock bash at the top
    # of this file, which has no trap and dies on TERM#1 at once; systemd
    # then enters stop-post and re-signals the rest of the control group
    # (FINAL_SIGTERM) while this inner bash may already be in here. Cleanup
    # is bounded, so ignoring signals for its duration is the right trade.
    trap '' TERM INT HUP
    [ "${_LITCLOCK_UPDATE_CLEANED:-0}" -eq 1 ] && return 0
    _LITCLOCK_UPDATE_CLEANED=1
    if [ "${_LITCLOCK_UPDATE_FINALIZED:-0}" -eq 0 ]; then
        # Re-arm the LKG writer's grace window BEFORE the painter can write
        # a fresh heartbeat: a TERM ≥900s after Phase 1's touch would
        # otherwise let litclock-lkg-record.sh promote this never-finalized
        # HEAD off the re-armed painter's first paint (round-2 review, F3).
        atomic_write_file "$POST_UPDATE_GRACE_FILE" "" 2>/dev/null || true
        # -k 5 is load-bearing, not a belt: `trap ''` makes children inherit
        # SIG_IGN, `timeout` re-installs its own handler so sudo is killable,
        # but sudo restores saved dispositions for its command, so systemctl
        # runs TERM-immune — only timeout's KILL of the process group bounds
        # it (round-2 review; GNU documents the TERM/KILL distinction).
        timeout -k 5 10 sudo -n systemctl start --no-block litclock.timer 2>/dev/null || true
        update_status_failed_unrecovered \
            "Update did not complete (signal or unexpected exit at phase ${_LITCLOCK_UPDATE_PHASE_INDEX:-?})." \
            2>/dev/null || true
    fi
    # litclock-dev#274 cleanup: Phase 3 side-channel tempfile that mapfile reads
    # ADDED_VARS from. Cleared on normal exit at line ~556, but a SIGKILL
    # mid-Phase-3 (power loss, OOM) would otherwise leak it across runs.
    [ -n "${_PHASE3_ADDED_FILE:-}" ] && rm -f "$_PHASE3_ADDED_FILE" 2>/dev/null
    return 0
}
_litclock_update_trap() {
    _litclock_update_cleanup
}
# Terminate on a signal, AFTER cleaning up. `exit` here also runs the EXIT
# trap, which the flag turns into a no-op. Bash runs this handler only once
# the interrupted foreground command has returned — prompt in practice,
# because systemd signals the whole control group and the foreground child
# dies with us; a child that ignored TERM would hold it off until
# TimeoutStopSec's SIGKILL. The `trap ''` is the handler's first statement
# so a signal arriving during it is processed after it, i.e. ignored.
_litclock_update_on_signal() {
    trap '' TERM INT HUP
    _litclock_update_cleanup
    exit 143
}
trap _litclock_update_trap EXIT
trap _litclock_update_on_signal TERM INT HUP


# Warn about local state that will be overwritten.
# litclock-dev#724: detached HEAD (rev-parse prints "HEAD") is the fleet's
# PERMANENT, correct state — devices are pinned to release-tag SHAs, so an
# unconditional branch warning fired on every single OTA run and was wrong
# every time, training operators to skim the one warning that matters.
# Detached AT a release tag is therefore silent. The tag ref is always
# local: Phase-2's fetch pins refs/tags/<tag> on every install, and flashed
# images clone --branch <tag>, which carries the tag. A detached
# NON-release commit — a hand checkout, a mid-rebase state, an
# interrupted-update recovery — is real anomalous state and still warns
# (/review litclock-dev#730). The copy names no destination: the modern path pins the
# resolved release target, rollback pins the recovery SHA, and the legacy
# no-lib fallback still resets to origin/master — the old "will be reset
# to master" text was wrong on all but the last.
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [[ "$CURRENT_BRANCH" == "HEAD" ]]; then
    if ! git describe --tags --exact-match --match 'v[0-9]*' HEAD >/dev/null 2>&1; then
        log_warn "Detached at non-release commit $(git rev-parse --short HEAD) — an update will replace it"
    fi
elif [[ "$CURRENT_BRANCH" != "master" ]]; then
    log_warn "Currently on branch '$CURRENT_BRANCH' — an update will not preserve it"
fi
if [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
    log_warn "Uncommitted changes detected — will be overwritten by update"
fi

echo ""
echo "========================================"
echo "  LitClock Update"
echo "========================================"
echo ""
log_info "Current version: $OLD_SHA"

# Ensure jq is installed — M5's status-file helper (scripts/lib/update_status.sh)
# requires it for atomic JSON writes that the PWA's /api/update/status endpoint
# reads. Hardware QA on test Pi 2026-04-30 caught the silent-skip case: when jq
# is missing, _write_status_json logs a warning + returns early, the apply still
# completes, but the PWA's phase reading-list never animates. Existing Pis built
# pre-M5 don't have jq in their apt set; install it here on the next update so
# the M5 reading-list works after a self-upgrade.
if ! command -v jq >/dev/null 2>&1; then
    log_info "Installing jq (required for M5 status-file helper)..."
    sudo apt-get install -y --no-install-recommends jq 2>&1 | tail -3 || \
        log_warn "jq install failed; PWA reading-list will not animate this run"
fi

# ─── Phase 1: Stop timer ─────────────────────────────────────────────

# Row 1 of the D3 phase reading-list: "Checking for updates"
update_status_set_phase 1

log_info "Stopping clock timer..."
sudo systemctl stop litclock.timer 2>/dev/null || true

# Arm the post-update grace marker NOW, before touching git (litclock-bootcheck
# LKG auto-revert). The LKG writer (litclock-lkg-record.sh) does NOT take
# update.lock; between the Phase 2 `git reset` (HEAD → new SHA) and the Phase 7
# grace touch, a stale-but-still-<180s heartbeat from the OLD code could let the
# writer bless the NEW, not-yet-painted SHA as lkg-sha — poisoning the recovery
# target with the exact bad release we might need to revert FROM. Writing the
# grace marker here (before HEAD changes) blocks the writer for the whole update
# window; Phase 7 re-touches it to extend the soak past the service restart.
atomic_write_file "$POST_UPDATE_GRACE_FILE" ""

# NOTE (litclock-bootcheck, LKG auto-revert): we deliberately do NOT clear
# lkg-sha here anymore. The heartbeat-gated writer (litclock-lkg-record.sh)
# only replaces lkg-sha once the NEW code has actually painted a frame, so
# retaining the old value means lkg-sha ALWAYS points at the last code that
# rendered. A dead-on-arrival update can never blank it → bootcheck always
# has a valid recovery target. Clearing it here (the pre-bootcheck behavior)
# re-opened the exact DOA gap the auto-revert exists to close.

# ─── Phase 2: Pull code ──────────────────────────────────────────────

# ─── Rollback mode (litclock-bootcheck LKG auto-revert) ──────────────
# When bootcheck has confirmed a persistent post-update brick it writes the
# last-known-good SHA to rollback-target and triggers litclock-update.service.
# In that mode we install THAT SHA (a complete install: git + submodules +
# venv hash-gate + units→/etc + sudoers + dispatcher + smoke) instead of
# resolving the latest Release. Crucially, a pip/smoke failure must NOT revert
# to OLD_SHA — OLD_SHA is the bad code we are fleeing — so REVERT_SHA is
# pinned to the rollback target and a failure simply stays on it (bootcheck's
# give-up path then handles a doubly-bad LKG).
ROLLBACK_MODE=0
REVERT_SHA="$OLD_SHA"
if [[ -f "$ROLLBACK_TARGET_FILE" ]]; then
    _rb=$(read_sha_file "$ROLLBACK_TARGET_FILE")
    if [[ -n "$_rb" ]]; then
        if git cat-file -e "${_rb}^{commit}" 2>/dev/null; then
            ROLLBACK_MODE=1
            REVERT_SHA="$_rb"
            log_warn "Rollback mode: bootcheck pinned recovery SHA $_rb"
        else
            log_error "Rollback target $_rb not present locally — cannot recover via update.sh; leaving pin for a later attempt"
            sudo systemctl start litclock.timer 2>/dev/null || true
            exit 1
        fi
    else
        log_warn "rollback-target present but malformed — ignoring, proceeding as a normal update"
    fi
fi

# Resolve the release-gated target SHA. Empty → graceful-offline:
# exit 0 without touching git state so the clock keeps running on its
# pinned SHA. The next weekly tick tries again.
TARGET_SHA=""
if [[ "$ROLLBACK_MODE" -eq 1 ]]; then
    TARGET_SHA="$REVERT_SHA"
    log_info "Rollback target SHA $TARGET_SHA (bypassing Release resolution)"
elif declare -F github_api_latest_release_tag >/dev/null 2>&1; then
    log_info "Resolving latest Release target SHA..."
    # resolver emits SHA on stdout, warns on stderr — let stderr flow through
    # to the user/journald; only stdout feeds the variable.
    TARGET_SHA=$(resolve_target_sha)
    # Strict: must be a 40-char hex SHA or empty.
    if [[ -n "$TARGET_SHA" && ! "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]]; then
        log_warn "Resolver returned a malformed value; treating as offline"
        TARGET_SHA=""
    fi
    # blocked-sha suppression (litclock-bootcheck): refuse to re-install a
    # Release SHA that bootcheck reverted away from, until a NEWER release
    # (a different SHA) supersedes it. Without this the weekly timer would
    # re-brick the device on the same bad release the day after a recovery.
    if [[ -n "$TARGET_SHA" && -f "$BLOCKED_SHA_FILE" ]]; then
        _blocked=$(read_sha_file "$BLOCKED_SHA_FILE")
        if [[ -n "$_blocked" && "$TARGET_SHA" == "$_blocked" ]]; then
            log_warn "Latest Release SHA $TARGET_SHA is blocked (bootcheck reverted from it) — skipping update"
            log_info "Clock continues on the recovered SHA. A newer Release will clear the block."
            # Terminal CLEAN status: this is a deliberate no-op, not a failure.
            # Without finalizing, the EXIT trap would stamp failed_unrecovered
            # (state was set to running by Phase 1) and the PWA would raise a
            # false "manual recovery needed" alarm every single weekly tick until
            # a newer release ships. The clock is running fine on the recovered
            # SHA, so "complete" is the honest state.
            update_status_complete 2>/dev/null || true
            _LITCLOCK_UPDATE_FINALIZED=1
            sudo systemctl start litclock.timer 2>/dev/null || true
            exit 0
        fi
    fi
fi

if [[ -z "$TARGET_SHA" ]]; then
    if declare -F github_api_latest_release_tag >/dev/null 2>&1; then
        log_warn "Could not resolve a blessed Release SHA — exiting cleanly (offline or no releases yet)"
        log_info "Clock continues on its pinned SHA. Next timer fire will retry."
        # Restart the timer so the clock keeps ticking — we stopped it in Phase 1.
        sudo systemctl start litclock.timer 2>/dev/null || true
        exit 0
    fi
    # No lib sourced (fresh-image run before github_api.sh lands) — fall back to
    # the legacy origin/master path so this script stays backwards-compatible
    # for a manual SSH update kicked off before the new units are installed.
    log_warn "github_api lib unavailable — falling back to origin/master (legacy path)"
fi

# Row 2 of the D3 phase reading-list: "Pulling new code" (covers Phase 2 + 2b).
update_status_set_phase 2

log_info "Pulling latest code..."
# Skip the fetch in rollback mode — the LKG target is already local (network
# may be down on a bricked device); a normal update needs the latest refs.
if [[ "$ROLLBACK_MODE" -ne 1 ]]; then
    git fetch origin master
fi

# Snapshot own checksum BEFORE pull so we can detect self-modification
SELF_SCRIPT="$(readlink -f "${BASH_SOURCE[0]}")"
OLD_SELF_HASH=$(md5sum "$SELF_SCRIPT" | cut -d' ' -f1)

# In rollback mode, snapshot THIS script (which has the rollback logic) BEFORE
# the reset. If the LKG target carries a different update.sh — very likely,
# since the LKG usually predates this rollback feature — the self-modification
# guard below would otherwise re-exec the LKG's update.sh, which has no
# rollback logic and would re-resolve the latest (bad) Release, resetting
# straight back to the brick. Re-execing the snapshot instead completes the
# pinned LKG install (it re-reads rollback-target from disk).
ROLLBACK_SELF_SNAPSHOT=""
if [[ "$ROLLBACK_MODE" -eq 1 ]]; then
    ROLLBACK_SELF_SNAPSHOT="$(mktemp /tmp/litclock-update-rollback.XXXXXX 2>/dev/null || echo "")"
    if [[ -n "$ROLLBACK_SELF_SNAPSHOT" ]] && cp "$SELF_SCRIPT" "$ROLLBACK_SELF_SNAPSHOT" 2>/dev/null; then
        chmod +x "$ROLLBACK_SELF_SNAPSHOT" 2>/dev/null || true
    else
        ROLLBACK_SELF_SNAPSHOT=""
    fi
fi

if [[ -n "$TARGET_SHA" ]]; then
    log_info "Target: Release SHA $TARGET_SHA"
    git reset --hard "$TARGET_SHA"
else
    # Legacy fallback path.
    git reset --hard origin/master
fi
git submodule update --init --recursive

# Re-exec if update.sh itself changed — bash holds a stale fd after
# git replaces the file, which can cause it to read garbled content
# from the new file at the old byte offset.
NEW_SELF_HASH=$(md5sum "$SELF_SCRIPT" | cut -d' ' -f1)
if [[ "$OLD_SELF_HASH" != "$NEW_SELF_HASH" ]]; then
    log_info "update.sh changed — re-executing with new version..."
    if [[ "$ROLLBACK_MODE" -eq 1 && -n "$ROLLBACK_SELF_SNAPSHOT" && -x "$ROLLBACK_SELF_SNAPSHOT" ]]; then
        # Rollback: re-exec the pre-reset snapshot (has rollback logic), NOT the
        # on-disk LKG update.sh (which would re-resolve the latest bad Release).
        # rollback-target persists on disk, so the snapshot re-detects rollback.
        exec bash "$ROLLBACK_SELF_SNAPSHOT" "$OLD_SHA"
    fi
    chmod +x "$SELF_SCRIPT"
    exec "$SELF_SCRIPT" "$OLD_SHA"
fi
# Snapshot no longer needed once we're past the re-exec point (same bytes).
[[ -n "$ROLLBACK_SELF_SNAPSHOT" ]] && rm -f "$ROLLBACK_SELF_SNAPSHOT" 2>/dev/null || true

NEW_SHA=$(git rev-parse --short HEAD)

# litclock-dev#245 M5 — record the post-Phase-2 SHA in the status file so the PWA can
# render "v0.210.0 → v0.211.0" copy from row 2 onward.
update_status_set_to_version "$NEW_SHA"

if [[ "$OLD_SHA" == "$NEW_SHA" ]]; then
    log_info "Already up to date ($OLD_SHA)"
else
    log_info "Updated: $OLD_SHA → $NEW_SHA"
fi

# ─── Phase 2b: Clean up stale files ──────────────────────────────────

# Root-level symlinks were created by old install.sh but are no longer
# needed — all systemd units use absolute paths into scripts/.
STALE_FILES=(
    "$INSTALL_DIR/boot-splash.sh"
    "$INSTALL_DIR/first-boot.sh"
    "$INSTALL_DIR/runtheclock.sh"
    "$INSTALL_DIR/shutdown-splash.sh"
    "$INSTALL_DIR/update.sh"
    "$INSTALL_DIR/src/api_key_server.py"
)
REMOVED=()
for f in "${STALE_FILES[@]}"; do
    if [[ -e "$f" || -L "$f" ]]; then
        rm -f "$f"
        REMOVED+=("$(basename "$f")")
    fi
done
if [[ ${#REMOVED[@]} -gt 0 ]]; then
    log_info "Removed stale files: ${REMOVED[*]}"
fi

# litclock-dev#604 — invalidate the runtime-render validation marker when
# any of its proof inputs changed in this update. The marker records that
# `validate_measurement.py check --stamp` proved THIS freetype reproduces
# THAT measurement dump with THESE fonts; it is gitignored, so the
# git-reset above never touches it, and without this step a font change,
# dump regen, freetype pin bump, or measurement-semantics change would
# leave a stale proof in force. Removing it drops flag-enabled devices to
# the pre-rendered tier until an operator re-runs check --stamp — a wrong
# fallback is impossible, a wrong render is not.
# The reader honors LITCLOCK_RUNTIME_VALIDATED_MARKER (set via env.sh,
# which runtheclock.sh sources) — resolve the SAME path here or a relocated
# marker silently survives the one invalidation layer that covers
# semantics-only proof changes, where the digest recompute can't help
# (litclock-dev#611 review). Subshell so env.sh can't mutate this script.
RUNTIME_MARKER=$(
    [[ -f "$INSTALL_DIR/env.sh" ]] && source "$INSTALL_DIR/env.sh" 2>/dev/null
    echo "${LITCLOCK_RUNTIME_VALIDATED_MARKER:-$INSTALL_DIR/.runtime-render-validated}"
)
if [[ -f "$RUNTIME_MARKER" ]]; then
    git diff --quiet "$OLD_SHA" "$NEW_SHA" -- \
        fonts/ \
        tools/gd-expected-measurements.json.gz \
        requirements.txt \
        src/quote_renderer.py \
        src/gd_measure.py \
        tools/validate_measurement.py 2>/dev/null
    _marker_diff_rc=$?
    if [[ "$_marker_diff_rc" -eq 1 ]]; then
        rm -f "$RUNTIME_MARKER"
        log_info "runtime-render validation marker removed: its proof inputs changed in this update (litclock-dev#604) — the next normal update re-earns it (or run tools/validate_measurement.py check --stamp by hand sooner)"
    elif [[ "$_marker_diff_rc" -gt 1 ]]; then
        # git diff itself failed (gc'd SHA, corrupt object) — we cannot
        # PROVE the inputs are unchanged, and a wrong fallback beats a
        # wrong render. Same action, honest log.
        rm -f "$RUNTIME_MARKER"
        log_info "runtime-render validation marker removed: could not verify its proof inputs across this update (git diff rc=$_marker_diff_rc) — the next normal update re-earns it (or run tools/validate_measurement.py check --stamp by hand sooner)"
    fi
fi

# Remove old systemd units that no longer exist in the repo
for installed_unit in /etc/systemd/system/litclock*.service \
                      /etc/systemd/system/litclock*.timer; do
    [[ -f "$installed_unit" ]] || continue
    unit_name=$(basename "$installed_unit")
    if [[ ! -f "$INSTALL_DIR/systemd/$unit_name" ]]; then
        sudo systemctl stop "$unit_name" 2>/dev/null || true
        sudo systemctl disable "$unit_name" 2>/dev/null || true
        sudo rm -f "$installed_unit"
        log_info "Removed obsolete systemd unit: $unit_name"
    fi
done

# ─── Phase 2c: Sync quote images ─────────────────────────────────────
# Quote images are NOT tracked in git (issue litclock-dev#82). Fetch them from the
# GitHub Release pinned by .images-version. No-op when on-disk marker
# already matches the pin.
#
# Graceful on failure: download_images.sh exits 0 on network/HTTP
# errors so an offline or transient failure doesn't break the update.
# Worst case: the clock degrades to time-only until the next run.

# Row 3 of the D3 phase reading-list: "Syncing quote images" (Phase 2c).
update_status_set_phase 3

if [[ -x "$INSTALL_DIR/scripts/download_images.sh" ]]; then
    log_info "Syncing quote images..."
    if ! "$INSTALL_DIR/scripts/download_images.sh" --repo-root "$INSTALL_DIR"; then
        log_warn "Quote image sync reported a non-zero status — continuing"
    fi
else
    log_warn "scripts/download_images.sh not found — skipping image sync"
fi

# Row 4 of the D3 phase reading-list: "Updating Python packages" (Phase 3 + 4).
update_status_set_phase 4

# ─── Phase 3: Merge new env vars ─────────────────────────────────────
#
# Issue litclock-dev#274: the per-var `echo >>` append idiom below races against the
# PWA Python writer (src/config.py:atomic_update) which holds a flock on
# `<env.sh>.lock`. Hoist the merge body into a function and run it via
# `with_env_lock` so both writers contend on the same sidecar inode.
#
# Subshell scope (with_env_lock runs CMD inside `(...)`): the helper
# captures `INSTALL_DIR` from the parent shell, mutates env.sh on disk,
# and emits its own log_info. Anything it sets in `ADDED_VARS` is local
# to the subshell — that's why we log inside the helper, not after.

ADDED_VARS=()

# Side-channel file: _phase3_merge_sample runs inside a subshell (via
# with_env_lock) so any bash array it builds is local to that subshell.
# Write added var names to a temp file inside the helper, then read
# them back into ADDED_VARS in the parent shell after the lock releases
# so the end-of-update summary still lists them. Cleanup runs at line
# ~556 on normal exit; the existing _litclock_update_trap (line 298)
# also `rm -f`s this on SIGKILL/SIGTERM/power loss so the tempfile
# doesn't accumulate in /tmp across failed weekly updates.
_PHASE3_ADDED_FILE=$(mktemp 2>/dev/null) || _PHASE3_ADDED_FILE="/tmp/litclock-update-added.$$"

_phase3_merge_sample() {
    local needs_newline=false
    local line varname

    : > "$_PHASE3_ADDED_FILE"

    # Ensure env.sh ends with a newline before appending
    if [[ -s "$INSTALL_DIR/env.sh" ]] && [[ "$(tail -c1 "$INSTALL_DIR/env.sh")" != "" ]]; then
        needs_newline=true
    fi
    while IFS= read -r line; do
        # Match both active and commented-out export lines
        if [[ "$line" =~ ^[#\ ]*(export[[:space:]]+)([A-Za-z_][A-Za-z0-9_]*)= ]]; then
            varname="${BASH_REMATCH[2]}"
            if ! grep -q "^[# ]*export[[:space:]]\+${varname}=" "$INSTALL_DIR/env.sh"; then
                if [[ "$needs_newline" == "true" ]]; then
                    echo "" >> "$INSTALL_DIR/env.sh"
                    needs_newline=false
                fi
                echo "$line" >> "$INSTALL_DIR/env.sh"
                printf '%s\n' "$varname" >> "$_PHASE3_ADDED_FILE"
            fi
        fi
    done < "$INSTALL_DIR/env.sh.sample"
}

# litclock-dev#274 follow-up — track whether we just wrote the Phase 3 skip marker
# inside the inner rc=75 branch below. EVERY other path that reaches
# Phase 3 must clear any stale marker from a prior run so the Status
# banner self-clears — including:
#   * The clean-Phase-3-run path (rc != 75)
#   * The first-boot copy-from-sample path (elif)
#   * The degenerate "neither env.sh nor env.sh.sample" no-op path
# Without this, a previous rc=75 run's banner stays visible for the full
# 24h freshness window even after the next update fixes the contention.
_phase3_marker_just_written=0

if [[ -f "$INSTALL_DIR/env.sh" && -f "$INSTALL_DIR/env.sh.sample" ]]; then
    # Force ENV_FILE_DEFAULT to track INSTALL_DIR (state.sh computed
    # ENV_FILE_DEFAULT at source time from /home/pi/litclock). The env
    # prefix is local to this call so it doesn't leak into the rest
    # of update.sh's shell.
    ENV_FILE_DEFAULT="$INSTALL_DIR/env.sh" \
        with_env_lock _phase3_merge_sample
    _phase3_rc=$?
    if [[ "$_phase3_rc" == "75" ]]; then
        # Lock held >30s by the PWA or another writer. Phase 3 is purely
        # opportunistic (next weekly tick retries); skip + continue rather
        # than abort the entire update.
        log_warn "env.sh locked by another writer — skipping sample merge this run (will retry next tick)"
        # litclock-dev#274 follow-up: stamp the marker so the PWA Status hero can
        # surface the skip. mtime-only — reader (control_server status
        # route) clamps to "< 1 day old" so the banner self-clears.
        if ! atomic_write_file "$PHASE3_SKIPPED_FILE" ""; then
            log_warn "Could not write $PHASE3_SKIPPED_FILE — PWA Status row will not surface the skip"
        fi
        _phase3_marker_just_written=1
    elif [[ -s "$_PHASE3_ADDED_FILE" ]]; then
        # Read added var names back from the side-channel file. mapfile
        # is bash-builtin and present on every Bookworm install.
        mapfile -t ADDED_VARS < "$_PHASE3_ADDED_FILE"
        if [[ ${#ADDED_VARS[@]} -gt 0 ]]; then
            log_info "Added new env vars: ${ADDED_VARS[*]}"
        fi
    fi
    unset _phase3_rc
elif [[ ! -f "$INSTALL_DIR/env.sh" ]]; then
    log_warn "No env.sh found — copying from sample"
    cp "$INSTALL_DIR/env.sh.sample" "$INSTALL_DIR/env.sh"
fi

# Clear the skip marker UNLESS we just wrote it on this run's rc=75 path.
# Hoisted outside the env.sh/env.sh.sample gate so every Phase 3 outcome
# self-clears a stale prior-run banner — including the first-boot copy
# path and the degenerate "no env.sh.sample" no-op.
if [[ "$_phase3_marker_just_written" == "0" ]]; then
    atomic_remove_file "$PHASE3_SKIPPED_FILE"
fi
unset _phase3_marker_just_written

rm -f "$_PHASE3_ADDED_FILE" 2>/dev/null
unset _PHASE3_ADDED_FILE

# ─── Phase 4: Update Python packages ─────────────────────────────────

REQUIREMENTS="$INSTALL_DIR/requirements.txt"
HASH_FILE="$INSTALL_DIR/.pip-packages-hash"

if [[ ! -f "$REQUIREMENTS" ]]; then
    log_error "requirements.txt not found"
    exit 1
fi

# Hash requirements.txt AND requirements-apt.txt together to detect
# package changes. Including the apt-filter file means adding a new
# name to it (e.g. a new apt-provisioned GPIO lib) triggers a pip
# re-run, which correctly removes stale pip-installed copies of
# packages that should now come from apt (litclock-dev#214).
REQUIREMENTS_APT="$INSTALL_DIR/requirements-apt.txt"
PACKAGES_HASH=$(cat "$REQUIREMENTS" "$REQUIREMENTS_APT" 2>/dev/null | md5sum | cut -d' ' -f1)

NEED_PIP=false
# --system-site-packages keeps the venv compatible with apt-provisioned
# GPIO libs (python3-gpiozero / spidev / lgpio / pigpio). Mirrors the
# pi-gen build at pi-gen/stage3/01-setup-app/00-run.sh — update.sh MUST
# stay in sync with that, otherwise a venv rebuild tries to pip-compile
# C extensions on an image with no gcc (litclock-dev#214).
if [[ ! -d "$INSTALL_DIR/venv" ]]; then
    log_warn "Virtual environment missing — recreating..."
    python3 -m venv --system-site-packages "$INSTALL_DIR/venv"
    NEED_PIP=true
elif ! grep -q "VIRTUAL_ENV=[\"']*$INSTALL_DIR/venv[\"']*$" "$INSTALL_DIR/venv/bin/activate" 2>/dev/null; then
    log_warn "Virtual environment has stale paths — recreating..."
    rm -rf "$INSTALL_DIR/venv"
    python3 -m venv --system-site-packages "$INSTALL_DIR/venv"
    NEED_PIP=true
elif ! "$PYTHON" -c "import PIL, requests" &>/dev/null 2>&1; then
    log_warn "Virtual environment broken — recreating..."
    rm -rf "$INSTALL_DIR/venv"
    python3 -m venv --system-site-packages "$INSTALL_DIR/venv"
    NEED_PIP=true
elif [[ ! -f "$HASH_FILE" ]] || [[ "$(cat "$HASH_FILE")" != "$PACKAGES_HASH" ]]; then
    NEED_PIP=true
fi

if [[ "$NEED_PIP" == "true" ]]; then
    log_info "Updating Python packages..."
    # Filter apt-provisioned names (requirements-apt.txt, defined above)
    # out of requirements.txt before pip install — they are reachable via
    # --system-site-packages and attempting to pip-install them triggers
    # sdist compilation (litclock-dev#214).
    REQUIREMENTS_FILTERED=$(mktemp)
    if [[ -f "$REQUIREMENTS_APT" ]]; then
        EXCLUDE_RE=$(grep -vE '^[[:space:]]*(#|$)' "$REQUIREMENTS_APT" | sed 's/\./\\./g' | paste -sd'|')
        grep -vE "^(${EXCLUDE_RE})==" "$REQUIREMENTS" > "$REQUIREMENTS_FILTERED"
    else
        cp "$REQUIREMENTS" "$REQUIREMENTS_FILTERED"
    fi
    # --upgrade forces pip to honor bumped pins. Without it, pip leaves an
    # already-installed package at the OLD version even after requirements.txt
    # bumps the pin (urllib3==2.6.3 → 2.7.0 reproduced this — litclock-dev#321). Security
    # fixes silently fail to propagate via the weekly auto-update path.
    #
    # We intentionally do NOT use --upgrade-strategy eager. Eager would also
    # weekly-upgrade unpinned transitives (e.g. flask's Werkzeug/Click/
    # itsdangerous/blinker/MarkupSafe), and Phase 4.5's smoke test only
    # imports literary_clock.py — it never touches Flask, so a transitive
    # release that breaks Flask 3.1.3 would ship silently and kill the
    # control PWA on next boot with no rollback signal. Transitive security
    # bumps belong at release-cut time via a lockfile (follow-up issue).
    if "$PIP" install --upgrade pip --quiet && "$PIP" install --upgrade -r "$REQUIREMENTS_FILTERED" --quiet; then
        echo "$PACKAGES_HASH" > "$HASH_FILE"
        log_info "Python packages updated"
        rm -f "$REQUIREMENTS_FILTERED"
    else
        # litclock-dev#324 + codex adversarial review of PR litclock-dev#349 — pip-install failure
        # must revert the git tree + exit, but the smoke-failure-branch
        # mirror is NOT a clean parallel:
        #
        # Smoke fires AFTER a successful pip install, so reverting git is
        # enough to leave the venv in a known-good state (the just-installed
        # packages match OLD_SHA's requirements.txt by definition — Phase 4
        # only ran because the hash changed, which it does on the NEW
        # requirements). Pip failure here is the opposite: pip ran with
        # --upgrade against NEW_SHA's requirements and DIED MID-STREAM.
        # Some packages may have been upgraded, others not. Reverting git
        # gives us OLD_SHA code + an indeterminate venv state. We cannot
        # guarantee a clean "failed_reverted" outcome — at any minute tick
        # the clock could crash on an import that resolves to the wrong
        # version. Lying about state ("clock is running fine on OLD_SHA")
        # is worse than admitting we don't know.
        #
        # So: ALWAYS report failed_unrecovered from this branch. Operator
        # gets a "manual recovery needed" signal; clock might still run
        # (most python packages are forward-compatible); status is honest.
        # The PWA's failed_unrecovered copy is "manual recovery needed",
        # not "clock is dead" — it's the correct user-facing string for
        # "venv state uncertain".
        log_error "pip install failed — reverting code to $REVERT_SHA (venv state uncertain)"
        rm -f "$REQUIREMENTS_FILTERED"
        # Capture revert exit codes so we can distinguish "code reverted,
        # venv uncertain" from "revert itself failed, both code and venv
        # uncertain". `|| true` is gone — we need the real status here.
        REVERT_OK=1
        # REVERT_SHA == OLD_SHA in a normal update (unchanged behavior); in
        # bootcheck rollback mode it is the LKG target, so a failure here
        # stays on the last-known-good rather than falling back to the bad code.
        # SELF-MODIFYING, and safe — litclock-dev#773 item 3 asked why this has
        # no re-exec guard when the pull path at :758 does. Measured with strace
        # against git 2.43.0: `git reset --hard` does `unlink()` then
        # `openat(O_WRONLY|O_CREAT|O_EXCL)`. O_EXCL means git NEVER writes into
        # the existing inode, so the bash executing this file keeps reading the
        # PRE-reset content through its already-open fd (verified end to end: a
        # script that git-resets itself mid-block, forks, and falls through to
        # top level ran every remaining statement correctly, inode 4906177 ->
        # 4906189). The stale-fd hazard the pull path guards needs an IN-PLACE
        # rewrite; git never does one.
        #
        # The pull path re-execs for a different reason anyway: it CONTINUES and
        # must run the new code against the new tree. A revert arm terminates,
        # so it never needs the new code. There is no asymmetry to defend.
        if ! git reset --hard "$REVERT_SHA" 2>&1 | sed 's/^/[revert] /'; then
            REVERT_OK=0
        fi
        # PIPESTATUS guard: the `git reset ... | sed` pipeline only fails
        # the if-test when sed itself fails; check PIPESTATUS[0] for the
        # actual git status.
        if [ "${PIPESTATUS[0]:-0}" -ne 0 ]; then
            REVERT_OK=0
        fi
        if ! git submodule update --init --recursive 2>&1 | sed 's/^/[revert] /'; then
            REVERT_OK=0
        fi
        if [ "${PIPESTATUS[0]:-0}" -ne 0 ]; then
            REVERT_OK=0
        fi
        # Delete the pip hash so the next timer fire re-attempts pip install
        # (without this, the next run sees an unchanged requirements.txt
        # under OLD_SHA but the indeterminate venv claims it matches NEW_SHA's
        # hash, and Phase 4 skips). Track this exit code too — if hash
        # removal fails we cannot guarantee re-attempt on next tick.
        if ! rm -f "$HASH_FILE"; then
            REVERT_OK=0
        fi
        # Loud failure indicator the e-ink can pick up.
        if ! atomic_write_file "$UPDATE_FAILED_FILE" ""; then
            log_warn "Could not write $UPDATE_FAILED_FILE — corner glyph will not render"
        fi
        # Still attempt to start the clock — even with an uncertain venv,
        # most python packages are forward-compatible and the clock has a
        # fighting chance of running. We do NOT use the result of this
        # attempt to decide the terminal status (that's bound to REVERT_OK
        # + the always-failed_unrecovered shape per codex review).
        log_info "Attempting to restore clock on previous SHA (venv state uncertain)..."
        sudo systemctl start litclock.service 2>/dev/null || true
        sudo systemctl start litclock.timer 2>/dev/null || true
        echo ""
        echo "========================================"
        if [ "$REVERT_OK" -eq 1 ]; then
            echo -e "${RED}  Update FAILED (pip install) — code reverted to $REVERT_SHA, venv state uncertain${NC}"
        else
            echo -e "${RED}  Update FAILED (pip install + revert) — code and venv state both uncertain${NC}"
        fi
        echo "========================================"
        # CRITICAL: set _LITCLOCK_UPDATE_FINALIZED=1 BEFORE the status write
        # so that even if the status write itself fails (jq missing, disk
        # full, fs read-only), the EXIT trap's failed_unrecovered fallback
        # still arms. If we set it AFTER and the status write throws, the
        # trap would not fire and update.status would be stuck at `running`.
        _LITCLOCK_UPDATE_FINALIZED=1
        # Terminal state is ALWAYS failed_unrecovered from this branch —
        # never failed_reverted — because the venv could be in any state
        # between "fully upgraded" and "fully on OLD_SHA pins" and we have
        # no cheap way to verify which. failed_unrecovered signals "manual
        # recovery needed" which is the honest answer.
        if [ "$REVERT_OK" -eq 1 ]; then
            update_status_failed_unrecovered \
                "pip install failed; git reverted to ${REVERT_SHA} but venv state uncertain — see /var/log/litclock/update.log and consider rebuilding venv."
        else
            update_status_failed_unrecovered \
                "pip install failed AND git revert failed; both code and venv state uncertain — manual recovery required (see /var/log/litclock/update.log)."
        fi
        exit 1
    fi
else
    log_info "Python packages up to date"
fi

# ─── Phase 4.5: Post-rebuild smoke test ──────────────────────────────
# Invoke --dry-run against the freshly-rebuilt venv. If it fails we have
# a bad release — revert the working tree + wipe the pip hash (so the next
# run re-runs pip; it does not by itself recreate the venv) + mark
# update-failed, skip Phase 5/7, and exit non-zero so the systemd unit
# records failure.
#
# 60s timeout bounds a hung render (missing font, broken corpus, I/O deadlock).
# The smoke test never touches GPIO/SPI — --dry-run defers display_driver import.
#
# Always run when PYTHON exists, even on same-SHA timer fires. A pip rebuild
# can break the venv without changing the git SHA (e.g. a transitive dep
# regressed against an unchanged requirements.txt) — gating on SHA-change
# alone would skip smoke in exactly that case and let a broken venv reach
# Phase 5/7. Cheap insurance: 60s ceiling, weekly cadence.
# Row 5 of the D3 phase reading-list: "Verifying clock starts" (Phase 4.5 smoke).
update_status_set_phase 5

# litclock-dev#773 — both of these are initialised HERE, outside the
# interpreter check, because the KEEP/revert dispatch below now runs for both
# arms. `smoke_rc` is belt, not load-bearing: today's two arms each assign it
# unconditionally, so deleting this line changes nothing — it guards a THIRD
# arm nobody has added yet. `smoke_no_interpreter` is load-bearing: the
# dispatch reads it to choose the terminal status, and only the else arm
# raises it.
smoke_rc=0
smoke_no_interpreter=0
if [[ -x "$PYTHON" ]]; then
    log_info "Running smoke test against rebuilt venv..."
    # Invoke the same way runtheclock.sh does (script-style, NOT `python -m
    # src.literary_clock`). literary_clock.py uses an absolute `from log
    # import setup_logging` that resolves only when Python adds the script's
    # parent directory (src/) to sys.path — i.e., script-style execution.
    # Module-style invocation puts the project root on sys.path instead, so
    # the import fails (`ModuleNotFoundError: No module named 'log'`) and
    # the smoke test reverts every successful update on every device. The
    # smoke test must mirror the production invocation path or it tests
    # nothing useful.
    timeout 60 "$PYTHON" src/literary_clock.py --dry-run 2>&1 | sed 's/^/[smoke] /'
    smoke_rc="${PIPESTATUS[0]}"
    # litclock-dev#532 (/review litclock-dev#738): the dry-run never imports the string
    # catalog, so a half-applied OTA missing languages/ passed smoke and
    # shipped raw keys to /api/status and every catalog-get script. Probe a
    # known key and require the RESOLVED value. The exit code cannot
    # discriminate: catalog-get exits 0 by contract and degrades visibly
    # instead — a missing key prints the key, and since litclock-dev#766 a
    # missing or broken strings_catalog does too, rather than exiting 1 with
    # empty stdout. Comparing the OUTPUT is what covers both, and it is why
    # that contract had to be made true rather than merely documented.
    #
    # litclock-dev#763 — LITCLOCK_LANGUAGE=en is load-bearing, not decoration.
    # The expected values are English literals, but catalog-get resolves through
    # strings_catalog.active_language(), which reads the process environment and
    # only THEN env.sh. litclock-update.service passes no LITCLOCK_LANGUAGE, so
    # the resolver falls through to _env_file_path()'s repo-root branch — the
    # running install's OWN env.sh — and the probe returns the device owner's
    # chosen language while the comparison stays English. (Setting
    # LITCLOCK_ENV_FILE would not have helped: it selects WHICH env.sh, and the
    # fallback already finds the right one. The pin is the fix because
    # active_language() gives the process environment precedence and returns
    # immediately for "en".)
    #
    # Latent only while the registry lists one active language. Once a release
    # activates a second and an owner selects it, the FIRST probe mismatches and
    # short-circuits the rest, smoke_rc=1 takes the revert arm, and nothing
    # writes a blocked-sha on that path — only bootcheck does — so the next
    # weekly tick resolves the same target and reverts again. It is not
    # unrecoverable: the re-exec above (update.sh changed => exec the new copy)
    # means the first release that modifies THIS file with the pin runs the
    # fixed gate and the device heals itself. Until such a release lands, an
    # affected device stays on its old SHA, shows "Update FAILED (reverted)" in
    # the PWA, and re-runs the whole pip install every week (the revert deletes
    # HASH_FILE, which sets NEED_PIP — it does not recreate the venv).
    #
    # The pin deliberately checks the CANONICAL catalog rather than the device's
    # own bundle. A missing translation degrades zz -> en inside get(), which is
    # survivable and not worth reverting an OTA over; serving the raw KEY is the
    # failure that is not, and that is the en -> key branch this still catches.
    # The cost is that the gate no longer exercises the owner's language at all
    # (litclock-dev#772).
    if [[ "$smoke_rc" -eq 0 ]]; then
        catalog_probe="$(LITCLOCK_LANGUAGE=en timeout 30 "$PYTHON" src/eink_display.py catalog-get status.relative.just_now 2>/dev/null)"
        if [[ "$catalog_probe" = "just now" ]]; then
            log_info "Catalog smoke passed"
        else
            log_error "Catalog smoke failed: catalog-get returned '$catalog_probe' (want 'just now')"
            smoke_rc=1
        fi
    fi
    # litclock-dev#532 bulk extraction (Codex slice-1 /review): the boot and
    # first-boot splashes now paint catalog TRIPLETS — a catalog that has
    # status.* but lost the splash prefixes would pass the probe above while
    # the recovery screens degrade. Probe the two prefixes whose loss hurts
    # most: the boot splash (every boot) and the strict recovery screen
    # (the copy a stuck user is left staring at).
    if [[ "$smoke_rc" -eq 0 ]]; then
        for probe in "boot.splash.starting.title=LitClock" "firstboot.splash.setup_incomplete.title=Setup Incomplete"; do
            probe_key="${probe%%=*}"
            probe_want="${probe#*=}"
            probe_got="$(LITCLOCK_LANGUAGE=en timeout 30 "$PYTHON" src/eink_display.py catalog-get "$probe_key" 2>/dev/null)"
            if [[ "$probe_got" != "$probe_want" ]]; then
                log_error "Catalog smoke failed: catalog-get $probe_key returned '$probe_got' (want '$probe_want')"
                smoke_rc=1
                break
            fi
        done
        [[ "$smoke_rc" -eq 0 ]] && log_info "Splash-triplet smoke passed"
    fi
    # litclock-dev#773 item 2 — the three probes above check VALUES, and values
    # cannot see truncation. Measured: an `en` bundle cut from 438 keys to just
    # those three passes every probe above green, with 435 strings gone and
    # every other status and splash surface degraded to raw keys. Catching that
    # by luck gets worse as litclock-dev#532's bulk extraction grows the surface.
    #
    # The expectation CANNOT be the committed strings.json: the loader reads
    # that same file, so comparing them is a no-op precisely when the file is
    # the thing that is damaged. It has to be a number that travels with
    # update.sh — which is the copy that has already re-exec'd itself onto the
    # new code, so it is present and current whenever the bundle is not.
    #
    # A FLOOR, not an equality: the catalog only grows, so a floor ages safely
    # (looser, never wrong) where an exact count would fail every release that
    # adds a string. tests/test_update_sh.py keeps it honest from the other
    # side — it fails if the floor drifts above the real count OR falls below
    # 75% of it, so a growing catalog forces a deliberate bump rather than
    # letting the gate quietly go slack.
    CATALOG_MIN_KEYS=400
    if [[ "$smoke_rc" -eq 0 ]]; then
        catalog_count="$(LITCLOCK_LANGUAGE=en timeout 30 "$PYTHON" src/eink_display.py catalog-count 2>/dev/null)"
        if [[ "$catalog_count" =~ ^[0-9]+$ ]] && [[ "$catalog_count" -ge "$CATALOG_MIN_KEYS" ]]; then
            log_info "Catalog size smoke passed ($catalog_count keys)"
        else
            log_error "Catalog smoke failed: catalog-count returned '$catalog_count' (want >= $CATALOG_MIN_KEYS)"
            smoke_rc=1
        fi
    fi
else
    # litclock-dev#773 — this arm is load-bearing. The gate used to be skipped
    # SILENTLY when $PYTHON was absent, and the update then walked on to Phase
    # 5/7 and reported SUCCESS having run nothing: not the dry run, not the
    # three pinned catalog probes — the whole inventory. A false GREEN
    # in the dangerous direction, firing exactly when the device is most likely
    # broken — Phase 4 has just built or repaired this venv, so a
    # non-executable interpreter HERE means that step did not do its job. The
    # block above already argues the neighbouring case ("a pip rebuild can break
    # the venv without changing the git SHA ... would let a broken venv reach
    # Phase 5/7"); a missing interpreter is the same argument, further along.
    log_error "Smoke test SKIPPED: $PYTHON is missing or not executable after Phase 4."
    log_error "Nothing was verified — treating this as a smoke FAILURE, not a success."
    smoke_rc=1
    smoke_no_interpreter=1
fi

# litclock-dev#773 — OUTSIDE the `-x $PYTHON` check on purpose. This used to
# live inside it, so a missing interpreter skipped the dispatch entirely and
# the update walked on to Phase 5/7 reporting SUCCESS. Setting smoke_rc in the
# else arm would have been a no-op: nothing read it out here.
# --- litclock-dev#835 budget helpers BEGIN ---
# The validator's bound (~1.6x the 182s measured on the bench Pi Zero 2W,
# 2026-09-12; tests/test_runtime_render_autostamp.py pins it against the
# unit's TimeoutStartSec) and the guard that decides whether THIS run can
# still afford it.
VALIDATOR_TIMEOUT_S=300
# What Phases 5-7 need AFTER the validator returns: unit installs, daemon
# reload, the clock restart and the stamp. Measured 16s on the bench Pi Zero
# 2W (litclock-dev#835); the reserve is generous because an update that runs
# out of budget here loses the stamp, not just the validation.
VALIDATOR_BUDGET_RESERVE_S=120

# The budget systemd LOADED for this unit, in whole seconds, on stdout
# (0 = no limit); non-zero return when unknown. Read as the raw D-Bus
# property — `busctl get-property` prints `t <microseconds>`, with
# 18446744073709551615 (UINT64_MAX) meaning infinity — rather than
# `systemctl show`, whose text is FORMAT_TIMESPAN output ("10min",
# "30min 1.500000s", "1month", "1y", ...) that a hand parser gets wrong in
# both directions (round-3 review: one rejected shape would have deferred
# forever; "500ms" parsed as the 0 that means unlimited). The object path
# is the D-Bus escaping of this unit's fixed name. The budget that applies
# to THIS run is the one the manager loaded, not the file in the checkout
# and not even the file in /etc: Phase 5 copies the new unit AFTER Phase
# 4.5 runs, so the first tick carrying a bigger TimeoutStartSec still runs
# under the old one — the exact tick on which every fielded device meets
# the validator for the first time (the bench's fresh image still carried
# 600 on 2026-09-13) — and a drop-in or a copied-but-not-reloaded file
# makes the on-disk main file wrong in both directions (round-2 review).
_UPDATE_UNIT_DBUS_PATH="/org/freedesktop/systemd1/unit/litclock_2dupdate_2eservice"
_update_budget_seconds() {
    local raw us
    raw=$(busctl get-property org.freedesktop.systemd1 "$_UPDATE_UNIT_DBUS_PATH" \
        org.freedesktop.systemd1.Service TimeoutStartUSec 2>/dev/null) || return 1
    [[ "$raw" =~ ^t\ ([0-9]+)$ ]] || return 1
    us="${BASH_REMATCH[1]}"
    # UINT64_MAX is infinity; anything of 19+ digits (≥ 10^18 µs, ~31,700
    # years) is "no limit" too. Decided on the DIGIT COUNT, never by
    # arithmetic: bash integers are signed 64-bit, and a 19-digit value
    # above 2^63-1 wraps negative in `(( ))` (round-5 review measured
    # 9223372036854775808 µs coming out as -9223372036854 s and deferring).
    if [[ ${#us} -ge 19 ]]; then echo 0; return 0; fi
    echo $(( us / 1000000 ))
}

# Seconds since systemd started THIS run's main process. Returns:
#   0  with the seconds on stdout — the run is this unit's invocation;
#   2  the run is not under systemd (no INVOCATION_ID: a manual
#      `sudo ./scripts/update.sh` has no start budget to respect);
#   1  the answer is UNKNOWN (an INVOCATION_ID that is not this unit's, a
#      failed query, a zero timestamp, an unreadable clock) — the caller must
#      treat unknown as "cannot afford it", never as "no budget".
# ExecMainStartTimestampMonotonic survives Phase 2's exec of the new script
# (same PID); $SECONDS does not. /proc/uptime is CLOCK_BOOTTIME, which
# equals the monotonic clock unless the box suspends; a Pi Zero does not.
_update_elapsed_seconds() {
    [[ -n "${INVOCATION_ID:-}" ]] || return 2
    local owner start_us now_s
    owner=$(systemctl show litclock-update.service -p InvocationID --value 2>/dev/null) || return 1
    [[ "$owner" == "$INVOCATION_ID" ]] || return 1
    start_us=$(systemctl show litclock-update.service -p ExecMainStartTimestampMonotonic --value 2>/dev/null) || return 1
    [[ "$start_us" =~ ^[0-9]+$ && "$start_us" -gt 0 ]] || return 1
    now_s=$(cut -d' ' -f1 /proc/uptime 2>/dev/null | cut -d. -f1)
    [[ "$now_s" =~ ^[0-9]+$ ]] || return 1
    echo $(( now_s - start_us / 1000000 ))
}

# 120s of margin covers Phase 5-7 (unit copies, daemon-reload, the first
# paint) after a validator that used its whole bound. It is a reserve, not
# an enforced bound on that work. Deferral is logged but never persisted: a
# device that NEVER has budget never validates, stays on the PNG tier, and
# the marker's absence is the only trace — the safe direction, and the
# negative-result memo still open on litclock-dev#835 is where a durable
# reason belongs.
_validation_fits_remaining_budget() {
    local elapsed budget rc
    elapsed=$(_update_elapsed_seconds); rc=$?
    if [[ $rc -eq 2 ]]; then
        return 0
    elif [[ $rc -ne 0 ]]; then
        log_info "deferring runtime-render validation to the next update: cannot determine how much of this run's systemd budget is left (litclock-dev#835)"
        return 1
    fi
    if ! budget=$(_update_budget_seconds); then
        log_info "deferring runtime-render validation to the next update: cannot read the unit's effective TimeoutStartSec (litclock-dev#835)"
        return 1
    fi
    # 0 here means UNLIMITED (the helper maps infinity to 0 and rounds a
    # finite sub-second budget to 0 as well — a budget under a second cannot
    # hold this run at all, and reaching Phase 4.5 under one is impossible,
    # so treating the two alike is harmless in that direction only).
    [[ "$budget" -eq 0 ]] && return 0
    if (( elapsed + VALIDATOR_TIMEOUT_S + VALIDATOR_BUDGET_RESERVE_S > budget )); then
        log_info "deferring runtime-render validation to the next update: ${elapsed}s of this run's ${budget}s budget are gone and the check needs up to ${VALIDATOR_TIMEOUT_S}s plus a ${VALIDATOR_BUDGET_RESERVE_S}s reserve for the phases after it, with litclock.timer stopped (litclock-dev#835)"
        return 1
    fi
    return 0
}
# --- litclock-dev#835 budget helpers END ---

if [[ "$smoke_rc" -eq 0 ]]; then
    log_info "Smoke test passed"
    atomic_remove_file "$UPDATE_FAILED_FILE"

    # litclock-dev#531 — (RE-)STAMP the runtime-render validation marker.
    #
    # Until this existed, NOTHING wrote that marker: not pi-gen, not this
    # script, and there is no install.sh. `_runtime_render_enabled()` requires
    # the flag AND the marker, so on every fielded device the flag was inert
    # and the runtime renderer was unreachable — shipped, soaked, and dead.
    # The block ~500 lines above only REVOKES it (litclock-dev#604) when an update
    # changes the proof inputs, which without a re-stamp is a one-way ratchet
    # down to the pre-rendered tier forever.
    #
    # POSITION IS LOAD-BEARING, three ways:
    #   * AFTER Phase 4, so the venv — and the freetype-py wheel whose bundled
    #     libfreetype this whole check is about — is the one that will run.
    #   * AFTER Phase 4.5, so a venv that cannot even import is not certified.
    #   * INSIDE the KEEP arm, so a smoke failure that git-resets the tree
    #     cannot leave a marker certifying code that was just reverted.
    #
    # NEVER fails the update. A failed or slow validation means no marker,
    # which means the pre-rendered PNG tier — the safe side. A wrong fallback
    # is impossible; a wrong render is not.
    #
    # Only when the marker is ABSENT: a surviving marker already passed this
    # check and its proof inputs are unchanged (the revoke block above is what
    # decides that), so re-running ~21s of work on x86 — 182s measured on a
    # Pi Zero 2W — on every weekly tick would be pure cost.
    #
    # Not in ROLLBACK_MODE (litclock-dev#835): that run exists to get the
    # last-known-good clock painting again as fast as possible, and the LKG's
    # proof inputs almost always differ from HEAD's, so the revoke block above
    # has just removed the marker. Spending minutes re-earning it here, with
    # litclock.timer still stopped, is the opposite of recovery. The next
    # normal update re-stamps.
    if [[ ! -f "$RUNTIME_MARKER" && -x "$PYTHON" && "${ROLLBACK_MODE:-0}" -ne 1 ]] && _validation_fits_remaining_budget; then
        log_info "Validating GD-exact measurement for the runtime renderer..."
        # Re-arm the LKG writer's grace window first: it is 900s from the
        # Phase 1 touch, this can take minutes, and a run past the window is
        # gated only by "no fresh heartbeat" (litclock-dev#835 review, F3).
        atomic_write_file "$POST_UPDATE_GRACE_FILE" ""
        # PIPESTATUS, not the pipeline's exit — the same lesson the smoke test
        # above already encodes. `if cmd | sed; then` tests SED, which always
        # succeeds, so the failure arm was unreachable and a failing validator
        # logged "reported success". Caught by an executed test, not by reading.
        # Bounded (litclock-dev#835): it was `timeout 600` inside a 600s unit,
        # which is decoration — systemd's SIGTERM landed first. The bound and
        # the remaining-budget guard above are what make this fit.
        timeout "$VALIDATOR_TIMEOUT_S" "$PYTHON" tools/validate_measurement.py check --stamp \
            --marker "$RUNTIME_MARKER" 2>&1 | sed 's/^/[validate] /'
        _validate_rc="${PIPESTATUS[0]}"
        if [[ "$_validate_rc" -eq 0 && -f "$RUNTIME_MARKER" ]]; then
            log_info "runtime-render validation PASSED — marker stamped; LITCLOCK_RUNTIME_RENDER will be honored"
        else
            log_info "runtime-render validation did not pass on this device (rc=$_validate_rc) — staying on pre-rendered images (this is not an update failure)"
            # A validator killed by `timeout` leaves its mkstemp litter next
            # to the marker; only the exact marker name is gitignored. The
            # glob is Python's tempfile scheme exactly — `<marker>.` plus
            # EIGHT characters from [a-z0-9_] — so nothing else that shares
            # the prefix can match (the first version used six `?`, which
            # missed the litter and would have matched a `.backup` sibling;
            # litclock-dev#835 port review). The marker path is
            # operator-settable, hence the variable prefix.
            rm -f "$RUNTIME_MARKER".[a-z0-9_][a-z0-9_][a-z0-9_][a-z0-9_][a-z0-9_][a-z0-9_][a-z0-9_][a-z0-9_] 2>/dev/null || true
        fi
        unset _validate_rc
    fi
else
    log_error "Smoke test failed (exit $smoke_rc) — reverting to $REVERT_SHA"
    # REVERT_SHA == OLD_SHA normally; in bootcheck rollback mode it is the
    # LKG target so a failed LKG smoke stays on LKG (never the bad code).
    # SELF-MODIFYING, and safe — litclock-dev#773 item 3 asked why this has
    # no re-exec guard when the pull path at :758 does. Measured with strace
    # against git 2.43.0: `git reset --hard` does `unlink()` then
    # `openat(O_WRONLY|O_CREAT|O_EXCL)`. O_EXCL means git NEVER writes into
    # the existing inode, so the bash executing this file keeps reading the
    # PRE-reset content through its already-open fd (verified end to end: a
    # script that git-resets itself mid-block, forks, and falls through to
    # top level ran every remaining statement correctly, inode 4906177 ->
    # 4906189). The stale-fd hazard the pull path guards needs an IN-PLACE
    # rewrite; git never does one.
    #
    # The pull path re-execs for a different reason anyway: it CONTINUES and
    # must run the new code against the new tree. A revert arm terminates,
    # so it never needs the new code. There is no asymmetry to defend.
    git reset --hard "$REVERT_SHA" 2>&1 | sed 's/^/[revert] /' || true
    git submodule update --init --recursive 2>&1 | sed 's/^/[revert] /' || true
    # Delete the pip hash so the next run re-runs `pip install` (it sets
    # NEED_PIP; it does NOT by itself recreate the venv — see :1236 and the
    # missing-interpreter arm below). Otherwise a revert could leave the venv
    # half-upgraded while the hash claims it matches.
    rm -f "$HASH_FILE"
    # Loud failure indicator the e-ink can pick up.
    if ! atomic_write_file "$UPDATE_FAILED_FILE" ""; then
        log_warn "Could not write $UPDATE_FAILED_FILE — corner glyph will not render"
    fi
    # Bring the clock back up on the OLD SHA before exiting.
    log_info "Restoring clock on previous SHA..."
    sudo systemctl start litclock.service 2>/dev/null || true
    sudo systemctl start litclock.timer 2>/dev/null || true
    echo ""
    echo "========================================"
    if [[ "$smoke_no_interpreter" -eq 1 ]]; then
        # litclock-dev#773 (/review) — the revert above CANNOT restore this
        # device: venv/ is not in git, so `git reset --hard` returns the code
        # and leaves the interpreter still missing, and runtheclock.sh sources
        # ./venv/bin/activate. The two `systemctl start`s just above therefore
        # cannot bring the clock back, and "rolled back, clock is fine" would
        # be a lie. The pip-failure branch ~250 lines up reasons exactly this
        # way for a strictly WEAKER condition (an INDETERMINATE venv): "Lying
        # about state is worse than admitting we don't know." A missing
        # interpreter is not indeterminate — it is known broken.
        #
        # Self-heals on the next weekly tick: Phase 4 recreates the venv on
        # whichever of its first three branches matches what survived — venv/
        # gone (branch 1), activate missing or stale (branch 2), or the import
        # probe returning 127 (branch 3). Branch 1 just creates it; 2 and 3
        # `rm -rf` first. NOT via the HASH_FILE deletion above, which only sets
        # NEED_PIP. But the owner must not be told the clock is running while
        # it is dead in the meantime.
        echo -e "${RED}  Update FAILED — venv interpreter missing; clock NOT restored${NC}"
        echo "========================================"
        update_status_failed_unrecovered \
            "Smoke test could not run: the venv interpreter was missing after Phase 4. Code reverted to ${REVERT_SHA}, but the venv is not in git so the clock may not restart until an update rebuilds it. The next scheduled update does that automatically."
    else
        echo -e "${RED}  Update FAILED (smoke test) — reverted to $REVERT_SHA${NC}"
        echo "========================================"
        # litclock-dev#245 M5 D9 — terminal status. PWA renders the "rolled back, clock
        # is fine" copy from this state (NOT the alarm-bell unrecovered copy).
        # Honest HERE because smoke fires after a SUCCESSFUL pip install, so
        # reverting git leaves a venv that matches REVERT_SHA's requirements.
        # REVERT_SHA == OLD_SHA normally; the LKG target in rollback mode (so we
        # never report the bad SHA we were fleeing).
        update_status_failed_reverted "Smoke test failed; reverted to ${REVERT_SHA}."
    fi
    _LITCLOCK_UPDATE_FINALIZED=1
    exit 1
fi

# Row 6 of the D3 phase reading-list: "Installing services" (Phase 5 + 5b + 6).
update_status_set_phase 6

# litclock-dev#245 M5 F14 — re-canonicalize /usr/local/bin/wifi-watchdog.sh on every
# update. Pre-M5 Pis have an inline-heredoc copy from install.sh that lacks
# OV1's firstboot fallback (D8) + F2's no-default-route handling. This
# unconditional install pulls in the canonical copy from scripts/, so the
# moved-house path becomes safe on the next watchdog tick after this update.
# Same for the litclock-wifi-reset.sh helper (D11) — installed even on Pis
# that didn't have it before so /api/wifi/reset works end-to-end.
if [[ -x "$INSTALL_DIR/scripts/wifi-watchdog.sh" ]]; then
    sudo install -m 0755 -o root -g root \
        "$INSTALL_DIR/scripts/wifi-watchdog.sh" /usr/local/bin/wifi-watchdog.sh
fi
if [[ -x "$INSTALL_DIR/scripts/litclock-wifi-reset.sh" ]]; then
    sudo install -m 0755 -o root -g root \
        "$INSTALL_DIR/scripts/litclock-wifi-reset.sh" /usr/local/bin/litclock-wifi-reset.sh
fi

# ─── Phase 5: Update systemd units ───────────────────────────────────

log_info "Updating systemd services..."
ENABLED_UNITS=()

# litclock-dev#241 migration — the previous litclock-lkg.service had
# WantedBy=litclock.service, which created a /etc/systemd/system/
# litclock.service.wants/litclock-lkg.service symlink. The rewritten
# service has no [Install] section, so the old symlink would silently
# keep firing the (now-different) unit on every litclock.service start.
# Disable the OLD unit before we cp the new one so its [Install] section
# is still parseable for symlink cleanup.
if [[ -f /etc/systemd/system/litclock-lkg.service ]] \
   && grep -q "WantedBy=litclock.service" /etc/systemd/system/litclock-lkg.service 2>/dev/null; then
    log_info "Migrating litclock-lkg.service from WantedBy hook to timer (litclock-dev#241)..."
    sudo systemctl disable litclock-lkg.service 2>/dev/null || true
    sudo systemctl stop litclock-lkg.service 2>/dev/null || true
fi

for unit in "$INSTALL_DIR"/systemd/*.service "$INSTALL_DIR"/systemd/*.timer; do
    [[ -f "$unit" ]] || continue
    name=$(basename "$unit")

    # wifi-watchdog units only if the watchdog script is installed
    if [[ "$name" == wifi-watchdog.* ]] && [[ ! -f /usr/local/bin/wifi-watchdog.sh ]]; then
        continue
    fi

    # firstboot service is managed by the image build/reset-setup.sh — don't re-enable
    if [[ "$name" == "litclock-firstboot.service" ]]; then
        sudo cp "$unit" /etc/systemd/system/
        continue
    fi

    # Detect whether this unit existed BEFORE this update — that distinguishes
    # "new install (auto-enable)" from "pre-existing (respect user state)".
    # `systemctl is-enabled` reports `disabled` for both "never enabled" and
    # "user explicitly disabled" — we cannot tell the two apart from systemctl
    # alone, so we use file existence as the discriminator. If the unit file
    # didn't exist in /etc/systemd/system/ before this run, it's a new unit
    # this release is shipping and we should enable it. Otherwise leave the
    # user's enable/disable choice intact (respects opt-out via
    # `systemctl disable --now litclock-update.timer`, which is the appliance
    # opt-out documented in the README per litclock-dev#209's design).
    was_pre_existing=true
    [[ -f "/etc/systemd/system/$name" ]] || was_pre_existing=false

    sudo cp "$unit" /etc/systemd/system/

    if [[ "$was_pre_existing" == "false" ]]; then
        # New unit — enable so it actually runs. Skip if it's "static" (no
        # [Install] section), "masked", etc. — only enable plain "disabled"
        # state, which is the post-cp default for a freshly-installed unit
        # that has [Install].
        if systemctl is-enabled "$name" 2>/dev/null | grep -q "^disabled$"; then
            # Record only on SUCCESS. update.sh has no errexit by design, so a
            # failed `systemctl enable` used to fall through and still append to
            # ENABLED_UNITS — the run then logged "Newly enabled: <unit>", the
            # summary reported it under "New services", and the OTA declared
            # success with the unit still disabled. For a timer that is the
            # brick-class outcome: nothing runs, nothing complains.
            if sudo systemctl enable "$name"; then
                ENABLED_UNITS+=("$name")
            else
                log_warn "failed to enable $name — it is installed but will NOT run"
            fi
        fi
    fi
done

sudo systemctl daemon-reload

# litclock-dev#676: daemon-reload does NOT re-arm a timer that has already
# elapsed — it keeps NextElapse=infinity — and the loop above starts only
# timers this run NEWLY enabled, which this one never is (pi-gen enables it).
# So a device that takes an OTA while sitting in the handoff window would not
# get the new cadence until its next boot, and that window is exactly the
# population the cadence exists for. Measured both ways: reload alone gave 0
# firings in 15s at a 5s cadence, restart resumed it immediately.
#
# Restarting this timer is safe to do unconditionally: it triggers a
# conditional oneshot that exits in milliseconds, and `restart` on a timer only
# re-bases its schedule. Deliberately NOT generalised to every timer —
# restarting litclock.timer mid-update could drop a minute's render, and
# restarting litclock-update.timer from inside litclock-update.service is a
# job-ordering hazard.
if systemctl is-enabled --quiet litclock-handoff-fallback.timer 2>/dev/null; then
    sudo systemctl restart litclock-handoff-fallback.timer 2>/dev/null \
        || log_warn "Could not re-arm litclock-handoff-fallback.timer; its cadence starts at the next boot"
fi

# litclock-dev#241 — install tmpfiles.d drop-ins for the tmpfs heartbeat dir and the
# /var/lib/litclock state dir, and materialize them now (avoids waiting until
# next boot). If --create fails, /run/litclock won't exist on the running
# system and the heartbeat will silently drop until the next reboot — log
# loudly so operators see it in journalctl rather than silently swallowing.
#
# Globbed, like the unit loop above and like pi-gen's 03-install-services.
# This was a hardcoded litclock.conf behind a soft `if [[ -f ]]`, which is
# two bugs in three lines: a second drop-in would never reach a fielded Pi
# via OTA, and a MISSING one skipped silently with no journal line at all.
# litclock-dev#547 is the same shape one directory up.
tmpfiles_installed=0
tmpfiles_seen=0
# Save and restore rather than blindly clearing: update.sh is 1400 lines and a
# future `shopt -s nullglob` above this point would otherwise be switched off
# here for every later phase.
_ng_saved=$(shopt -p nullglob)
shopt -s nullglob
sudo install -d -m 0755 /etc/tmpfiles.d
for conf in "$INSTALL_DIR"/systemd/tmpfiles.d/*.conf; do
    tmpfiles_seen=$(( tmpfiles_seen + 1 ))
    # Same regular-file requirement pi-gen's 03 uses. Catches ACCIDENTS — a
    # stray symlink or a directory named *.conf — not a hostile pi user. This
    # is a check-then-use on a pi-writable path, so the file can be swapped for
    # a symlink between the test and the `sudo install` below; treating it as a
    # privilege boundary would be wrong. It is not one here: the attacker would
    # be the pi user, who already runs this script and (per the shipped
    # 010_pi-nopasswd) already has full sudo. See the O_NOFOLLOW/O_NONBLOCK
    # note in the project learnings for the shape a real boundary needs.
    if [[ ! -f "$conf" || -L "$conf" ]]; then
        log_warn "skipping $(basename "$conf") — not a regular file"
        continue
    fi
    # `install -m 0644 -o root -g root`, not `cp`, matching 03-install-services.
    # cp applies the SOURCE mode, so a drop-in committed 0755 or 0600 landed
    # correctly on a flashed image and incorrectly on every OTA'd Pi — a
    # divergence only reproducible on the fielded fleet, which is the one
    # configuration QA never covers.
    #
    # The exit status is CHECKED and the counter only advances on success. It
    # used to increment unconditionally, which meant: cp fails (read-only /etc,
    # ENOSPC, EIO on a worn SD), the PREVIOUS release's drop-in is still present
    # so `systemd-tmpfiles --create` succeeds against the stale file and its
    # log_warn never fires, the counter reads 1 so the zero-drop-in warning
    # never fires either, and the OTA reports success having installed nothing.
    # That is the exact silent-swallow class this block was written to remove.
    if ! sudo install -m 0644 -o root -g root "$conf" /etc/tmpfiles.d/; then
        log_warn "failed to install $(basename "$conf") into /etc/tmpfiles.d/ — its rules were NOT refreshed"
        continue
    fi
    sudo systemd-tmpfiles --create "/etc/tmpfiles.d/$(basename "$conf")" \
        || log_warn "systemd-tmpfiles --create failed for $(basename "$conf") — its directories may not exist until reboot"
    tmpfiles_installed=$(( tmpfiles_installed + 1 ))
done
eval "$_ng_saved"
if [[ $tmpfiles_seen -eq 0 ]]; then
    # Not fatal — the previously installed drop-ins are still in /etc and the
    # clock keeps running off them. But it means this checkout is not what we
    # think it is, and the old soft `if` reported nothing at all here.
    log_warn "no tmpfiles.d drop-ins found under $INSTALL_DIR/systemd/tmpfiles.d/ — /run/litclock and /var/lib/litclock rules were not refreshed"
elif [[ $tmpfiles_installed -ne $tmpfiles_seen ]]; then
    log_warn "installed only ${tmpfiles_installed} of ${tmpfiles_seen} tmpfiles.d drop-ins — see warnings above"
fi

if [[ ${#ENABLED_UNITS[@]} -gt 0 ]]; then
    log_info "Newly enabled: ${ENABLED_UNITS[*]}"
fi

# litclock-dev#249/litclock-dev#251 — `enable` registers a unit but does not activate it in the
# current systemd session. For newly-enabled timers, also start them so
# the cadence kicks in immediately rather than waiting for next reboot.
# --no-block matches the litclock-control.service pattern below — update.sh
# can be invoked from inside litclock-update.service, where a synchronous
# `systemctl start` would deadlock systemd's job serializer.
for unit in "${ENABLED_UNITS[@]}"; do
    [[ "$unit" == *.timer ]] || continue
    log_info "Starting newly-installed timer: $unit"
    sudo systemctl start --no-block "$unit" 2>/dev/null || true
done

# ─── Phase 5b: Sync sudoers drops ────────────────────────────────────
#
# litclock-dev#245 M4 — Control PWA scoped sudo. Validate-then-install: visudo -c -f
# the source file in the repo first; only `install` it into /etc/sudoers.d/
# on a clean parse. A malformed sudoers entry locks out `sudo` system-wide,
# which would brick the appliance worse than any other M4 failure mode.
# Idempotent — diff against the installed copy and skip if unchanged.
for sudoers_src in "$INSTALL_DIR"/sudoers/*; do
    [[ -f "$sudoers_src" ]] || continue
    name=$(basename "$sudoers_src")
    installed="/etc/sudoers.d/$name"

    if ! sudo visudo -c -f "$sudoers_src" >/dev/null 2>&1; then
        log_error "$name failed visudo validation; refusing to install"
        continue
    fi

    if [[ -f "$installed" ]] && sudo cmp -s "$sudoers_src" "$installed"; then
        continue  # already in sync
    fi

    sudo install -m 0440 -o root -g root "$sudoers_src" "$installed"
    log_info "Installed sudoers drop: $name"
done

# ─── Phase 5c: Sync NetworkManager dispatcher (litclock-dev#309) ─────────────────
#
# Re-render the e-ink corner QR when wlan0's IP changes (router reboot,
# WiFi reconnect, lease expiry) so the displayed address never lags
# behind reality. Idempotent — mirrors the sudoers Phase 5b sync shape.
# Mode 0755 root:root: NM silently skips dispatcher scripts that don't
# match (group/world-writable is rejected).
NM_DISP_SRC="$INSTALL_DIR/scripts/nm-dispatcher/99-litclock-ip-change"
NM_DISP_DST="/etc/NetworkManager/dispatcher.d/99-litclock-ip-change"
if [[ -f "$NM_DISP_SRC" ]]; then
    if [[ ! -f "$NM_DISP_DST" ]] || ! sudo cmp -s "$NM_DISP_SRC" "$NM_DISP_DST"; then
        sudo install -d -m 0755 /etc/NetworkManager/dispatcher.d
        sudo install -m 0755 -o root -g root "$NM_DISP_SRC" "$NM_DISP_DST"
        log_info "Installed NetworkManager dispatcher: $(basename "$NM_DISP_DST")"
    fi
fi

# litclock-dev#387 — sync the root-owned privilege helpers so pi cannot rewrite what runs
# as root: the tz-wrapper, the dispatcher's mark-collected helper, and
# reset-setup.sh (run as root by litclock-prepare-for-gift.service). Installed
# to /usr/local/lib/litclock. Idempotent cmp-then-install, same shape as above.
sudo install -d -m 0755 /usr/local/lib/litclock 2>/dev/null || true
sudo install -d -m 0755 /usr/local/lib/litclock/lib 2>/dev/null || true
for _hlp in litclock-set-timezone litclock-mark-collected.sh reset-setup.sh; do
    _hlp_src="$INSTALL_DIR/scripts/$_hlp"
    _hlp_dst="/usr/local/lib/litclock/$_hlp"
    if [[ -f "$_hlp_src" ]]; then
        if [[ ! -f "$_hlp_dst" ]] || ! sudo cmp -s "$_hlp_src" "$_hlp_dst"; then
            sudo install -m 0755 -o root -g root "$_hlp_src" "$_hlp_dst"
            log_info "Installed privilege helper: $_hlp"
        fi
    fi
done
# reset-setup.sh sources lib/state.sh relative to its own dir, so the root-owned
# copy must ship a root-owned lib/state.sh alongside (0644 — sourced, not exec).
_st_src="$INSTALL_DIR/scripts/lib/state.sh"
_st_dst="/usr/local/lib/litclock/lib/state.sh"
if [[ -f "$_st_src" ]]; then
    if [[ ! -f "$_st_dst" ]] || ! sudo cmp -s "$_st_src" "$_st_dst"; then
        sudo install -m 0644 -o root -g root "$_st_src" "$_st_dst"
        log_info "Installed privilege helper lib: state.sh"
    fi
fi

# ─── Phase 5d: Idempotent systemd-journal group migration (litclock-dev#433) ─────
# pi-gen-built images already have `pi` in the systemd-journal group via
# stage1's default user setup, so this is a no-op for every deployed
# user we know about. Insurance against future pi-gen variants that
# might drop the default (or against manual fiddling that removes pi
# from the group). No-op when the group isn't present (e.g. an
# unusual base image) — getent's exit code short-circuits.
#
# /review found: don't log success unconditionally. update.sh runs without
# `set -e`, so a failed `sudo usermod` (sudo denied, PAM block, pi user
# missing) would silently emit a log line that lies to the user. Check the
# exit code explicitly — Phase 5d is non-critical (Phase 7 service restart
# rolls forward regardless), so a usermod failure is a warning, not fatal.
if getent group systemd-journal >/dev/null 2>&1; then
    if ! id -nG pi 2>/dev/null | grep -qw systemd-journal; then
        if sudo usermod -aG systemd-journal pi; then
            log_info "Added pi to systemd-journal group (litclock-dev#433); takes effect after service restart."
        else
            log_warn "Failed to add pi to systemd-journal group (litclock-dev#433); journal_tail may render empty for non-self units."
        fi
    fi
fi

# ─── Phase 6: Ensure scripts are executable ──────────────────────────

chmod +x "$INSTALL_DIR"/scripts/*.sh

# ─── Phase 7: Restart services ───────────────────────────────────────

# Row 7 of the D3 phase reading-list: "Restarting" (Phase 7).
update_status_set_phase 7

# Touch the post-update grace marker BEFORE any service restart (issue
# litclock-dev#241, decision D2 — mtime-only). The LKG writer reads `now - mtime` and
# skips promotion while inside the 15-min grace window. Writing this
# first guarantees that any lkg-record poll (scheduled or manual) firing
# during the restart sequence sees the gate, not a fresh SHA atop a fresh
# heartbeat.
atomic_write_file "$POST_UPDATE_GRACE_FILE" ""

# litclock-bootcheck (LKG auto-revert): a successful apply clears the
# failed-boot streak so the recovered/updated code gets a fresh window.
# rollback-target is consumed either way. In a NORMAL update we also clear
# the recovery marker + the blocked-sha (we have moved to a genuinely new
# release, so the old block no longer applies). In ROLLBACK mode we KEEP
# both: bootcheck-recovering bounds a re-rollback loop if the LKG itself
# proves bad, and blocked-sha must persist so next week's timer does not
# re-install the release we just reverted from.
atomic_remove_file "$BOOT_FAIL_COUNT_FILE"
atomic_remove_file "$ROLLBACK_TARGET_FILE"
if [[ "${ROLLBACK_MODE:-0}" -ne 1 ]]; then
    atomic_remove_file "$BOOTCHECK_RECOVERING_FILE"
    atomic_remove_file "$BLOCKED_SHA_FILE"
fi

log_info "Restarting services..."

# Do NOT `systemctl restart litclock-shutdown.service` here (litclock-dev#331).
# litclock-shutdown.service is a stop-hook unit (Type=oneshot,
# RemainAfterExit=yes, ExecStart=/bin/true, ExecStop runs
# shutdown-splash.sh). `restart` = stop+start, and the stop half fires
# ExecStop → e-ink paints "Powered Off" for ~11s mid-update while the Pi
# is happily mid-pip. There is no runtime state to restart; any unit-file
# changes were already picked up at daemon-reload in Phase 5.

# Refresh display BEFORE restarting the timer — the timer is still stopped
# (Phase 1), so there is no concurrent trigger to race with.
log_info "Refreshing display..."
sudo systemctl start litclock.service 2>/dev/null || true

sudo systemctl start litclock.timer

# litclock-dev#343 — control_server now binds port 80. Install + apply the sysctl that lets
# the pi service account bind it BEFORE the restart below, or the rebind fails.
# Idempotent: install overwrites, sysctl -w is a no-op if already 80. The file
# install persists across reboot regardless; we VERIFY the live apply and warn
# loudly if it didn't take, rather than silently move control_server onto an
# unbindable port (the unit's StartLimitIntervalSec=0 + the persisted file mean
# the next reboot self-heals, but the operator should know).
_SYSCTL_CONF=/etc/sysctl.d/30-litclock-unprivileged-ports.conf
if [[ -f "$INSTALL_DIR/sysctl.d/30-litclock-unprivileged-ports.conf" ]]; then
    sudo install -m 0644 -o root -g root \
        "$INSTALL_DIR/sysctl.d/30-litclock-unprivileged-ports.conf" \
        "$_SYSCTL_CONF" 2>/dev/null || true
    sudo sysctl -w net.ipv4.ip_unprivileged_port_start=80 > /dev/null 2>&1 || true
    # Read the live value from /proc, NOT `sysctl -n`: sysctl lives in /usr/sbin
    # which is not on the pi user's non-login PATH, so `sysctl -n` here returns
    # "command not found" and a false "could not lower floor" warning (caught in
    # litclock-dev#343 hardware QA). The /proc file is always readable with no PATH/sudo.
    _port_floor=$(cat /proc/sys/net/ipv4/ip_unprivileged_port_start 2>/dev/null || echo "")
    # litclock-dev#527 field incident: the FILE install was silently swallowed
    # (2>/dev/null || true) while `sysctl -w` set the live value to 80 — so the
    # old warning (live-value only) stayed quiet, the update "succeeded", and
    # the NEXT reboot reverted to 1024 and crash-looped control_server. Verify
    # the persisted file matches the source CONTENT, not just its existence: a
    # stale drop-in left by a prior version whose fresh overwrite then failed
    # (RO fs, disk full) would pass a mere `-f` check while being wrong
    # (/review, both passes). `cmp -s` is true only when the installed file is
    # byte-identical to what we shipped. (litclock-control.service also
    # self-heals the live value via ExecStartPre — this warning is the
    # human-visible half.)
    if ! cmp -s "$INSTALL_DIR/sysctl.d/30-litclock-unprivileged-ports.conf" "$_SYSCTL_CONF"; then
        echo "  WARNING (litclock-dev#343/litclock-dev#527): the persistent port-80 sysctl drop-in is"
        echo "  missing or stale at $_SYSCTL_CONF. The live floor may be 80 now but"
        echo "  will revert on reboot. Re-run: sudo install -m 0644 -o root -g root \\"
        echo "    $INSTALL_DIR/sysctl.d/30-litclock-unprivileged-ports.conf $_SYSCTL_CONF"
    fi
    if [[ "$_port_floor" != "80" ]]; then
        echo "  WARNING (litclock-dev#343): could not lower the unprivileged-port floor to 80"
        echo "  (net.ipv4.ip_unprivileged_port_start=${_port_floor:-unknown}). The Control"
        echo "  PWA may be unreachable on port 80 until the next reboot applies the"
        echo "  installed $_SYSCTL_CONF."
    fi
fi

# litclock-dev#245 M1 — Control PWA. Restart if it's already running so it picks up the
# new code; start --no-block if it's enabled but inactive (e.g., this is the
# update that brought the unit in for the first time, or a prior reboot was
# missed). `is-active` covers running; `is-enabled` catches the enabled-but-
# inactive case caught by issue litclock-dev#251.
if systemctl is-active --quiet litclock-control.service 2>/dev/null; then
    sudo systemctl restart litclock-control.service 2>/dev/null || true
elif systemctl is-enabled --quiet litclock-control.service 2>/dev/null; then
    sudo systemctl start --no-block litclock-control.service 2>/dev/null || true
fi

# ─── Summary ─────────────────────────────────────────────────────────

echo ""
echo "========================================"
echo -e "${GREEN}  Update Complete${NC}"
echo "========================================"
echo ""
echo "  Version: $OLD_SHA → $NEW_SHA"
[[ ${#ADDED_VARS[@]} -gt 0 ]] && echo "  New env vars: ${ADDED_VARS[*]}"
[[ ${#ENABLED_UNITS[@]} -gt 0 ]] && echo "  New services: ${ENABLED_UNITS[*]}"
echo ""
if [[ "${ROLLBACK_MODE:-0}" -eq 1 ]]; then
    # In a bootcheck rollback, OLD_SHA is the BAD code we just fled — printing
    # "git reset --hard $OLD_SHA" would tell the operator to re-brick. There is
    # no meaningful undo: this WAS the recovery.
    echo "  Recovered to last-known-good $NEW_SHA (auto-reverted from $OLD_SHA)"
else
    echo "  Rollback: git reset --hard $OLD_SHA"
fi
echo ""

# litclock-dev#245 M5 D6 — invalidate the GH-API cache so the PWA shows "up to date"
# instantly post-update instead of waiting up to 6h for the cache to expire.
# Best-effort: the live cache is on the /run/litclock tmpfs (litclock-dev#434, pi-owned via
# the tmpfiles.d entry); rm should succeed without sudo. Failure is non-fatal —
# the worst case is a stale "update available" indicator until the next 6h
# window. Also sweep the pre-litclock-dev#434 SD-resident copy at $STATE_DIR so upgraders
# don't leave a stale blob on the flash card.
for _cache_file in "$UPDATE_CHECK_CACHE_FILE" "$LEGACY_UPDATE_CHECK_CACHE_FILE"; do
    if [[ -f "$_cache_file" ]]; then
        rm -f "$_cache_file" 2>/dev/null \
            || sudo rm -f "$_cache_file" 2>/dev/null \
            || log_warn "Could not invalidate $_cache_file — PWA may show 'update available' until next 6h refresh"
    fi
done

# litclock-dev#245 M5 D4 — write the terminal "complete" status. The Phase 7 systemctl
# restart of litclock-control.service above kills the running waitress
# mid-PWA-poll; the PWA's first successful post-restart poll reads this
# state + new version and triggers reload via A8.
update_status_complete

# litclock-dev#334 Tension 1 — disarm the EXIT trap BEFORE persisting last-update.json.
# Otherwise an OS-level kill (SIGKILL escapes traps; SIGTERM does not but
# any uncaught error in the persist would still be racy) between the
# update_status_complete write and the persist write would let the trap
# overwrite update.status with state=failed_unrecovered, leaving the
# persistent file pointing at a "successful" update that the volatile
# file disagrees with on the next reboot. Disarm first, then persist.
_LITCLOCK_UPDATE_FINALIZED=1

# litclock-dev#334 Tension 2 — validate-then-cp. Read /run/litclock/update.status back
# (the file we just wrote), jq-validate that state == "complete" AND
# to_version == "$NEW_SHA" AND finished_at_unix is fresh (within last 60s).
# Only on a clean validation do we promote the file to the persistent
# /var/lib/litclock/last-update.json. Catches "we cp'd a stale or torn
# status file" without re-implementing the JSON schema; jq is the same
# source of truth update_status_complete already uses.
#
# Failure is best-effort: log_warn + continue. The Status hero falls back
# to lkg-sha (source 3) if last-update.json is missing — same UX as
# pre-litclock-dev#334 — so a write failure here can't regress the row.
_litclock_persist_last_update() {
    # Pre-M5 legacy path (update_status.sh not sourced) — there's no
    # /run/litclock/update.status to mirror, so persist is a no-op.
    local status_file="${LITCLOCK_UPDATE_STATUS_FILE:-}"
    if [[ -z "$status_file" ]]; then
        return 0
    fi
    if ! command -v jq >/dev/null 2>&1; then
        log_warn "jq missing — skipping last-update.json persist"
        return 0
    fi
    if [[ ! -r "$status_file" ]]; then
        log_warn "$status_file not readable — skipping last-update.json persist"
        return 0
    fi
    local now_unix
    now_unix=$(date +%s 2>/dev/null || echo 0)
    # litclock-dev#342 I2 — widen the freshness floor from 60s to 3600s. The gate's
    # original intent is "not torn / not stale-from-a-previous-update-run",
    # not minute-level freshness — so an hour-wide window is safe. The
    # narrow window bit on Pi Zero 2W cold-boot updates: the Pi has no
    # real-time clock, so post-power-on system time is pre-1970 until
    # chrony syncs ~30s after WiFi connects. If an update fires before
    # NTP settles (rare but possible via SSH-triggered apply or a fast
    # OnBootSec timer), update_status_complete stamps a pre-1970 time;
    # the 60s floor evaluates against real-2026, the timestamp falls
    # nowhere near it, gate rejects, persistent file never written, next
    # reboot's Status row falls back to em-dash. 3600s absorbs the NTP
    # sync lag while still catching multi-day-old stale files from
    # interrupted prior runs.
    local floor=$((now_unix - 3600))
    # jq -e returns non-zero (1) if the filter evaluates to false/null,
    # which is exactly the boolean we want for the validation gate.
    if ! jq -e --arg expected "$NEW_SHA" --argjson floor "$floor" \
        '.state == "complete" and .to_version == $expected and (.finished_at_unix // 0) > $floor' \
        "$status_file" >/dev/null 2>&1; then
        log_warn "update.status failed validation gate; skipping last-update.json persist"
        return 0
    fi
    # Atomic publish — write to .tmp then mv into place. install(1) would
    # work too but mv is consistent with _write_status_json's idiom and
    # already proven on ext4 / tmpfs.
    local parent tmp
    parent=$(dirname "$LAST_UPDATE_FILE")
    if [[ ! -d "$parent" ]]; then
        sudo mkdir -p "$parent" 2>/dev/null || mkdir -p "$parent" 2>/dev/null \
            || { log_warn "could not create $parent — skipping last-update.json persist"; return 0; }
    fi
    # litclock-dev#342 I4 — sweep orphan .tmp.* siblings from prior persist attempts
    # that died between the cp staging and the mv publish (disk full, OOM,
    # kill -9). Mirrors the manifest-sweep pattern from PR litclock-dev#293. The sweep
    # runs INSIDE the same flock that protects this update.sh invocation,
    # so it can't race with a concurrent legitimate persist. Best-effort:
    # any rm failure (e.g. parent dir transiently unwritable) falls through
    # silently — the orphan files are inert until the next sweep.
    rm -f "${LAST_UPDATE_FILE}.tmp."* 2>/dev/null || true
    tmp="${LAST_UPDATE_FILE}.tmp.$$"
    if ! cp "$status_file" "$tmp" 2>/dev/null; then
        log_warn "could not stage last-update.json — skipping persist"
        rm -f "$tmp" 2>/dev/null
        return 0
    fi
    chmod 0644 "$tmp" 2>/dev/null || true
    if ! mv "$tmp" "$LAST_UPDATE_FILE" 2>/dev/null; then
        log_warn "could not publish $LAST_UPDATE_FILE — skipping persist"
        rm -f "$tmp" 2>/dev/null
        return 0
    fi
    # Best-effort ownership fix — only matters when running as root via
    # litclock-update.service; the pi user already owns /var/lib/litclock
    # via the tmpfiles.d drop, so chown here is for defense-in-depth.
    chown pi:pi "$LAST_UPDATE_FILE" 2>/dev/null \
        || sudo chown pi:pi "$LAST_UPDATE_FILE" 2>/dev/null \
        || true
    return 0
}
_litclock_persist_last_update
