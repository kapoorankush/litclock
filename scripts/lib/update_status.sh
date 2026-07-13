# shellcheck shell=bash
#
# LitClock — update status file helpers (#245 M5 D4 / D9 / F6 / F9).
#
# Sourced by scripts/update.sh. Writes /run/litclock/update.status as JSON
# the PWA's /api/update/status endpoint reads and the Updates tab renders as
# a 7-row reading-list (D3).
#
# JSON shape (D9 lock):
#   {
#     "state":            "running" | "complete" | "failed_reverted" | "failed_unrecovered",
#     "phase_index":      1..7,
#     "phase_name":       <one of the seven D3 names>,
#     "started_at_unix":  <epoch seconds — set once at update start>,
#     "finished_at_unix": <epoch seconds — set on terminal states>,
#     "from_version":     <pre-update SHA, set at start>,
#     "to_version":       <post-update SHA, set when known>,
#     "error":            <human-readable string — only set on terminal failure states>
#   }
#
# Writes are atomic via mv-tmp (renaming on the same filesystem is atomic
# on ext4 / tmpfs; PWA readers never see a torn file). All writes go through
# `jq -nc --arg ...` (NEVER printf interpolation) so error strings with
# quotes / newlines / backticks can't corrupt the JSON. F6 in plan.

# Where to write. Override via env in tests; default to the tmpfs dir
# /run/litclock provisioned by systemd/tmpfiles.d/litclock.conf (owner pi).
LITCLOCK_UPDATE_STATUS_FILE="${LITCLOCK_UPDATE_STATUS_FILE:-/run/litclock/update.status}"

# 7 user-facing phase names (D3 lock). update.sh calls _set_phase <index>
# at each phase boundary; the helper looks up the name from this array.
# Indices are 1-based for symmetry with the row numbers in DESIGN.md.
_LITCLOCK_PHASE_NAMES=(
    "PADDING"  # index 0 unused (1-based)
    "Checking for updates"      # 1 — Phase 1 + resolver
    "Pulling new code"          # 2 — Phase 2 + 2b
    "Syncing quote images"      # 3 — Phase 2c
    "Updating Python packages"  # 4 — Phase 3 + 4
    "Verifying clock starts"    # 5 — Phase 4.5 (smoke)
    "Installing services"       # 6 — Phase 5 + 5b + 6
    "Restarting"                # 7 — Phase 7
)

# Internal state — caller never sets directly.
_LITCLOCK_UPDATE_STARTED_AT=""
_LITCLOCK_UPDATE_FROM_VERSION=""
_LITCLOCK_UPDATE_TO_VERSION=""
_LITCLOCK_UPDATE_PHASE_INDEX=0

# _write_status_json <state> [error]
#
# Atomic write of the status file. Caller passes the terminal state; everything
# else (phase, versions, timestamps) is read from the cached internal state.
_write_status_json() {
    local state="$1" err="${2:-}"
    local phase_index="${_LITCLOCK_UPDATE_PHASE_INDEX:-0}"
    local phase_name=""
    if [ "$phase_index" -ge 1 ] && [ "$phase_index" -le 7 ]; then
        phase_name="${_LITCLOCK_PHASE_NAMES[$phase_index]}"
    fi
    local started_at="${_LITCLOCK_UPDATE_STARTED_AT:-}"
    local from_version="${_LITCLOCK_UPDATE_FROM_VERSION:-}"
    local to_version="${_LITCLOCK_UPDATE_TO_VERSION:-}"

    # Terminal states stamp finished_at_unix. running leaves it null so the
    # PWA can compute "duration so far" from now-started_at_unix.
    local finished_at=""
    case "$state" in
        complete|failed_reverted|failed_unrecovered)
            finished_at="$(date +%s 2>/dev/null || echo 0)"
            ;;
    esac

    local parent
    parent="$(dirname "$LITCLOCK_UPDATE_STATUS_FILE")"
    if [ ! -d "$parent" ]; then
        mkdir -p "$parent" 2>/dev/null || sudo mkdir -p "$parent" 2>/dev/null || return 1
    fi

    # Build JSON via jq with --arg (string) + --argjson (literal). jq escapes
    # every string field; --argjson is only used for already-validated
    # numerics or null. Empty strings render as JSON null via the if-empty
    # branches so the consumer sees explicit nulls, not empty strings.
    if ! command -v jq >/dev/null 2>&1; then
        # jq is in apt's default Bookworm install, but if it's somehow
        # missing the loud warn lets the operator know to apt install jq.
        # Fallback: just don't write status — the PWA will see a missing
        # file and render "idle" until the next phase.
        echo "[update-status] warn: jq not available; status file will not be written" >&2
        return 1
    fi

    local tmp="${LITCLOCK_UPDATE_STATUS_FILE}.tmp.$$"
    if ! jq -nc \
        --arg state "$state" \
        --argjson phase_index "${phase_index:-0}" \
        --arg phase_name "$phase_name" \
        --arg started_at "$started_at" \
        --arg finished_at "$finished_at" \
        --arg from_version "$from_version" \
        --arg to_version "$to_version" \
        --arg error "$err" \
        '{
            state: $state,
            phase_index: ($phase_index | if . == 0 then null else . end),
            phase_name: ($phase_name | if . == "" then null else . end),
            started_at_unix: ($started_at | if . == "" then null else (. | tonumber) end),
            finished_at_unix: ($finished_at | if . == "" then null else (. | tonumber) end),
            from_version: ($from_version | if . == "" then null else . end),
            to_version: ($to_version | if . == "" then null else . end),
            error: ($error | if . == "" then null else . end)
        }' > "$tmp" 2>/dev/null; then
        rm -f "$tmp"
        return 1
    fi
    mv "$tmp" "$LITCLOCK_UPDATE_STATUS_FILE" 2>/dev/null || { rm -f "$tmp"; return 1; }
    return 0
}

# update_status_init <from_version>
# Call once at the very top of update.sh, before any phase work. Stamps the
# started_at + from_version into the cache so subsequent _write_status_json
# calls inherit them.
update_status_init() {
    _LITCLOCK_UPDATE_FROM_VERSION="$1"
    _LITCLOCK_UPDATE_STARTED_AT="$(date +%s 2>/dev/null || echo 0)"
    _LITCLOCK_UPDATE_PHASE_INDEX=0
    _LITCLOCK_UPDATE_TO_VERSION=""
}

# update_status_set_to_version <new_sha>
# Call when the post-update target SHA becomes known (after Phase 2's git
# reset --hard). Cached for the rest of the run.
update_status_set_to_version() {
    _LITCLOCK_UPDATE_TO_VERSION="$1"
}

# update_status_set_phase <phase_index>
# Mark progress into one of the 7 D3 phases. Writes state=running.
update_status_set_phase() {
    local idx="$1"
    if ! [[ "$idx" =~ ^[1-7]$ ]]; then
        echo "[update-status] warn: invalid phase index '$idx' (must be 1..7)" >&2
        return 1
    fi
    _LITCLOCK_UPDATE_PHASE_INDEX="$idx"
    _write_status_json "running" ""
}

# update_status_complete
# Terminal state on a clean update. D4: must be the very last write.
update_status_complete() {
    _LITCLOCK_UPDATE_PHASE_INDEX=7
    _write_status_json "complete" ""
}

# update_status_failed_reverted [error]
# Terminal state on Phase 4.5 smoke-fail (clock running on OLD_SHA, update
# did not stick). The PWA renders distinct copy: "rolled back to v0.210.0,
# clock is running normally" rather than alarming the user about a failure.
update_status_failed_reverted() {
    local err="${1:-Smoke test failed; reverted to previous version.}"
    _write_status_json "failed_reverted" "$err"
}

# update_status_failed_unrecovered [error]
# Terminal state from the EXIT/TERM/INT/HUP trap when update.sh dies before
# completing. The PWA renders "manual recovery needed" copy.
update_status_failed_unrecovered() {
    local err="${1:-Update did not complete and was not recovered.}"
    _write_status_json "failed_unrecovered" "$err"
}
