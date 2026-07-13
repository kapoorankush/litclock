#!/bin/bash
#
# LitClock Image Downloader
#
# Downloads the quote-image set pinned by .images-version from the project's
# GitHub Releases and extracts it into images/. Idempotent: if the on-disk
# marker (images/.installed-version) matches the pinned version, it is a no-op.
#
# Called by:
#   scripts/install.sh        (DIY install flow, right after clone)
#   scripts/update.sh         (Phase 2c, after git pull)
#   .github/workflows/build-image.yml  (bakes into pi-gen OS image)
#
# Download strategy:
#   Uses the GitHub REST API asset endpoint
#   (/repos/OWNER/REPO/releases/assets/ID) with Accept: application/octet-stream.
#   This is the only URL pattern that works reliably for BOTH private and
#   public repos — the browser-download URL (github.com/.../releases/download/)
#   returns 404 on private repos even with a valid Bearer token.
#
# Failure semantics: network, HTTP, and integrity errors against remote state
# exit 0 (graceful — the clock falls back to time-only display when no images
# exist). Programming/local errors (missing .images-version, empty version,
# empty SHA file indicating a broken release, final swap failure) exit 1 so
# they aren't silently swallowed.
#
# Concurrency: an flock on ${REPO_ROOT}/.litclock-images.lock prevents two
# concurrent invocations from racing on the atomic swap.
#
# Usage:
#   download_images.sh                (reads .images-version from repo root)
#   download_images.sh --force        (fetch even if marker matches)
#   download_images.sh --repo-root /path/to/litclock
#
# Auth (for private repos — removable once the repo is public):
#   export GH_TOKEN=...      (preferred)
#   export GITHUB_TOKEN=...  (also honored — set automatically in GH Actions)

set -uo pipefail

# Colors (only if stdout is a tty)
if [ -t 1 ]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; NC=''
fi

log_info()  { printf "%b[INFO]%b %s\n" "${GREEN}" "${NC}" "$1"; }
log_warn()  { printf "%b[WARN]%b %s\n" "${YELLOW}" "${NC}" "$1"; }
log_error() { printf "%b[ERROR]%b %s\n" "${RED}" "${NC}" "$1" >&2; }

# Shared GitHub API helpers (github_api_curl — auth, timeout, graceful-offline).
# Path is resolved relative to this script so it works for both the installed
# copy (/home/pi/litclock/scripts/...) and in-tree test runs. The -f guard
# matches scripts/update.sh and lets a partial checkout (lib missing) fail
# loudly at the call site instead of with a cryptic "command not found".
_THIS_SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
if [[ ! -f "$_THIS_SCRIPT_DIR/lib/github_api.sh" ]]; then
    log_error "Required library scripts/lib/github_api.sh is missing"
    exit 1
fi
# shellcheck source=/dev/null
. "$_THIS_SCRIPT_DIR/lib/github_api.sh"

# #293: Byte-integrity verification against the bundled `images/files.sha256`
# sidecar (produced by scripts/release_images.sh, packaged inside the tarball
# since v5). Catches accidental corruption (partial extract, fsck, manual rm)
# but NOT local tampering — the sidecar lives next to the PNGs and shares the
# same trust level. The cryptographic root of trust is the externally-fetched
# `tarball.sha256` at Step 4.
# Returns:
#   0  — sidecar present and every file matches
#   1  — sidecar present but one or more files mismatch / are missing
#   2  — sidecar absent (legacy release, can't verify, caller decides what to do)
# Always invoked with a single argument: the absolute path of the images dir
# to verify. On rc=1, the caller may invoke verify_byte_manifest_detail to
# capture the FAILED-line list for the operator log.
verify_byte_manifest() {
    local images_dir="$1"
    local sidecar="$images_dir/files.sha256"
    if [ ! -f "$sidecar" ]; then
        return 2
    fi
    # `sha256sum -c` reads paths relative to its CWD. The sidecar's entries
    # are `./quote_HHMM_N.png` / `./metadata/quote_..._credits.png`, so cd
    # into $images_dir before invoking. --quiet suppresses the per-file "OK"
    # lines; mismatches still print "FAILED" and the final summary.
    # --strict makes malformed/garbage lines a hard failure rather than a
    # silent warning — without it, a corrupted sidecar with valid + garbage
    # lines would pass verification on the strength of the valid lines alone.
    ( cd "$images_dir" && sha256sum --quiet --strict -c files.sha256 ) >/dev/null 2>&1
}

# Re-run sha256sum -c capturing FAILED lines and log up to the first 10 to
# the operator. Called only on verify_byte_manifest rc=1 so we eat the
# verification cost twice on the (rare) failure path — cheaper than always
# capturing stderr and discarding it on the success path.
verify_byte_manifest_detail() {
    local images_dir="$1"
    local sidecar="$images_dir/files.sha256"
    if [ ! -f "$sidecar" ]; then
        return 0
    fi
    local count=0
    while IFS= read -r line; do
        log_error "  $line"
        count=$((count + 1))
        if [ "$count" -ge 10 ]; then
            log_error "  …(truncated; re-run \`cd $images_dir && sha256sum -c files.sha256\` for the full list)"
            break
        fi
    done < <( cd "$images_dir" && sha256sum --strict -c files.sha256 2>&1 | grep -E ': FAILED|: No such file|improperly formatted' )
}

# #313: completeness check — every file manifest.json references must be
# accounted for in files.sha256. Catches the partial-emission shape where
# the byte sidecar is internally consistent but doesn't cover the file set
# the runtime expects. Returns:
#   0 — manifest.json absent OR sidecar absent OR completeness OK
#   1 — manifest.json present and references files not in the sidecar
# Reasoning for "manifest.json absent → OK": legacy v1-v4 releases lacked
# both the sidecar AND the bundled manifest.json (release_images.sh only
# started requiring manifest in #299, which post-dates the sidecar). The
# release-time gate in release_images.sh is the primary defense; this
# consumer-side check is defense-in-depth.
verify_manifest_completeness() {
    local images_dir="$1"
    local manifest="$images_dir/manifest.json"
    local sidecar="$images_dir/files.sha256"
    if [ ! -f "$manifest" ] || [ ! -f "$sidecar" ]; then
        return 0
    fi
    python3 - "$images_dir" >&2 <<'PY' || return 1
import json
import sys
from pathlib import Path

images = Path(sys.argv[1])
expected_main = set(json.loads((images / "manifest.json").read_text()).get("files", {}).keys())
if not expected_main:
    sys.exit(0)  # empty manifest — treat as no completeness claim
sidecar_paths = {
    ln.split("  ", 1)[1].lstrip("./")
    for ln in (images / "files.sha256").read_text().splitlines()
    if "  " in ln
}
expected_credits = {f"metadata/{f.replace('.png', '_credits.png')}" for f in expected_main}
missing = sorted((expected_main | expected_credits) - sidecar_paths)
if missing:
    for f in missing[:10]:
        print(f"  completeness gap: {f}", file=sys.stderr)
    if len(missing) > 10:
        print(f"  ... +{len(missing) - 10} more", file=sys.stderr)
    sys.exit(1)
PY
}

# Post-install verify-failure rollback. Quarantine the new (broken) content
# under a timestamped name so the operator can debug what shipped wrong, then
# restore OLD_DIR (if any) to $IMAGES_DIR. Shared by both byte-mismatch and
# manifest-completeness-mismatch paths (#313) so the failure response is the
# same regardless of which gate caught the bad release.
#
# #314: when the OLD content was ALSO corrupt (we got here via short-circuit
# verify-fail → re-download), DON'T roll back — restoring OLD_DIR would
# restore the very content we were trying to escape, leaving the clock
# rendering known-bad PNGs. Instead, quarantine OLD_DIR as `.prev` and set
# the update-failed marker so the e-ink renders the "!" glyph. The clock
# falls back to time-only via literary_clock.py's `quote_meta is None` path
# (existing behavior when $IMAGES_DIR is empty).
_post_install_verify_failure_rollback() {
    local failed_dir
    failed_dir="${IMAGES_DIR}.failed.$(date -u +%Y%m%dT%H%M%SZ)"
    if ! mv "$IMAGES_DIR" "$failed_dir"; then
        log_error "Could not move broken images aside — $IMAGES_DIR contains partial/corrupt content; manual cleanup required"
        exit 1
    fi
    log_warn "Broken content preserved at $failed_dir — rm manually after debugging"
    if [ "${VERIFY_FAILED_AT_SHORT_CIRCUIT:-0}" -eq 1 ] && [ -n "$OLD_DIR" ] && [ -d "$OLD_DIR" ]; then
        local old_failed_dir="${failed_dir}.prev"
        if mv "$OLD_DIR" "$old_failed_dir" 2>/dev/null; then
            log_warn "Previous content was also corrupt — kept at $old_failed_dir; clock will render time-only"
        else
            log_warn "Previous content was also corrupt — could not move aside, removing"
            rm -rf "$OLD_DIR"
        fi
        _set_update_failed_marker
        return 0
    fi
    if [ -n "$OLD_DIR" ] && [ -d "$OLD_DIR" ]; then
        if mv "$OLD_DIR" "$IMAGES_DIR"; then
            log_warn "Rolled back to previous content at $IMAGES_DIR"
        else
            log_error "Rollback failed — $OLD_DIR is orphan on disk and $IMAGES_DIR is empty; manual recovery required"
            exit 1
        fi
    else
        log_warn "No previous content to roll back to — $IMAGES_DIR is now empty"
    fi
}

# #314: update-failed marker primitives. Set by quarantine paths to tell
# literary_clock.py to render the corner "!" glyph; cleared on next
# successful install. /var/lib/litclock may not exist in test environments
# (or after a botched install) — best-effort: try to create the parent dir,
# then write the marker. Never fail the script over the marker, but don't
# silently no-op when the parent dir is the only reason the write would
# fail.
_set_update_failed_marker() {
    local marker="${LITCLOCK_UPDATE_FAILED_MARKER:-/var/lib/litclock/update-failed}"
    local marker_dir
    marker_dir=$(dirname "$marker")
    if [ ! -d "$marker_dir" ]; then
        mkdir -p "$marker_dir" 2>/dev/null || true
    fi
    if [ -d "$marker_dir" ]; then
        : > "$marker" 2>/dev/null || true
    fi
}

_clear_update_failed_marker() {
    local marker="${LITCLOCK_UPDATE_FAILED_MARKER:-/var/lib/litclock/update-failed}"
    if [ -f "$marker" ]; then
        rm -f "$marker" 2>/dev/null || true
    fi
}

# #314: if the on-disk content was already known-corrupt (short-circuit
# verify-fail → fall through to download) AND the download/extract path
# can't deliver a clean replacement (network down, GitHub down, asset
# missing, extract failure), quarantine the corrupt $IMAGES_DIR rather
# than leave it rendering bad PNGs on the e-ink. Clock falls back to
# time-only via literary_clock.py's existing $IMAGES_DIR-empty path.
#
# No-op unless VERIFY_FAILED_AT_SHORT_CIRCUIT=1, so the normal "no marker,
# never installed, network down" graceful-offline still exits 0 untouched.
quarantine_if_verify_failed() {
    if [ "${VERIFY_FAILED_AT_SHORT_CIRCUIT:-0}" -ne 1 ]; then
        return 0
    fi
    if [ ! -d "$IMAGES_DIR" ]; then
        return 0
    fi
    local failed_dir
    failed_dir="${IMAGES_DIR}.failed.$(date -u +%Y%m%dT%H%M%SZ)"
    log_warn "QUARANTINE: $IMAGES_DIR → $failed_dir (reason: short-circuit verify-fail + re-download offline)"
    log_warn "Could not refresh corrupt content; clock will render time-only until network restored"
    if mv "$IMAGES_DIR" "$failed_dir"; then
        _set_update_failed_marker
    else
        log_error "Could not move $IMAGES_DIR aside — corrupt content remains on disk"
        # Set the marker even on mv failure: the corrupt PNGs are still
        # rendering, so the operator still needs the "!" glyph signal.
        # Without this, the worst case (mv fails AND no marker) is silent
        # bad-content render forever.
        _set_update_failed_marker
    fi
}

# Defaults — overridable via env or flags for testing.
REPO_SLUG="${LITCLOCK_REPO_SLUG:-kapoorankush/litclock}"
API_BASE_URL="${LITCLOCK_API_BASE_URL:-https://api.github.com}"
ASSET_NAME="${LITCLOCK_ASSET_NAME:-litclock-images.tar.gz}"
REPO_ROOT=""
FORCE=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --force)
            FORCE=1
            shift
            ;;
        --repo-root)
            REPO_ROOT="${2:-}"
            shift 2
            ;;
        *)
            log_error "Unknown argument: $1"
            echo "Usage: $0 [--force] [--repo-root PATH]" >&2
            exit 1
            ;;
    esac
done

# Resolve repo root: explicit flag wins, else walk up from script dir to find .images-version.
if [ -z "$REPO_ROOT" ]; then
    SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
    REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
fi

PIN_FILE="$REPO_ROOT/.images-version"
IMAGES_DIR="$REPO_ROOT/images"
MARKER_FILE="$IMAGES_DIR/.installed-version"
LOCK_FILE="$REPO_ROOT/.litclock-images.lock"

# #314: tracks whether the on-disk content was already known-bad when we
# entered the download path. Drives:
#   * graceful-offline quarantine (avoid leaving corrupt PNGs rendering)
#   * post-install double-verify-fail policy (don't restore corrupt OLD_DIR)
VERIFY_FAILED_AT_SHORT_CIRCUIT=0

if [ ! -f "$PIN_FILE" ]; then
    log_error ".images-version not found at $PIN_FILE"
    exit 1
fi

PINNED_VERSION=$(tr -d '[:space:]' < "$PIN_FILE")
if [ -z "$PINNED_VERSION" ]; then
    log_error ".images-version is empty"
    exit 1
fi

# Single-instance lock. If another run is in progress, don't race —
# exit 0 (caller treats this as "nothing to do").
exec 200>"$LOCK_FILE"
if ! flock -n 200; then
    log_warn "Another download_images.sh run is in progress — skipping"
    exit 0
fi

# #293 idempotency: sweep orphan in-flight dirs left by previous runs that
# died mid-swap (SIGTERM, power loss, kernel oom). The flock above guarantees
# no live instance owns these. Recovery rule:
#   * If $IMAGES_DIR is MISSING and an `.old.*` orphan exists, restore it —
#     a previous run died between `mv IMAGES_DIR images.old.PID` and the
#     completion of the swap, leaving the appliance with no images at all.
#   * Otherwise, treat `.old.*` / `.broken.*` orphans as dead weight and rm.
#   * `.failed.*` siblings are kept as broken-content forensics, but bounded
#     to the most-recent 3 (#314): a Pi offline for weeks could otherwise
#     accumulate hundreds of quarantine dirs and fill the SD.
shopt -s nullglob
_orphans=( "${IMAGES_DIR}".old.* "${IMAGES_DIR}".broken.* )
shopt -u nullglob
if [ "${#_orphans[@]}" -gt 0 ]; then
    if [ ! -d "$IMAGES_DIR" ]; then
        for _d in "${_orphans[@]}"; do
            if [[ "$_d" == *.old.* ]]; then
                log_warn "$IMAGES_DIR missing — recovering from orphan $_d"
                if mv "$_d" "$IMAGES_DIR"; then
                    log_warn "  recovered. Run with --force to retry the interrupted update."
                    break
                fi
            fi
        done
    fi
    # Re-glob; the recovery may have consumed one entry.
    shopt -s nullglob
    _orphans=( "${IMAGES_DIR}".old.* "${IMAGES_DIR}".broken.* )
    shopt -u nullglob
    for _d in "${_orphans[@]}"; do
        log_warn "Removing orphan dir from prior crashed run: $_d"
        rm -rf "$_d"
    done
fi

# #314: bound `.failed.*` accumulation. UTC-timestamped names are
# lex-sortable; keep the 3 newest for forensics, prune the rest.
shopt -s nullglob
_failed=( "${IMAGES_DIR}".failed.* )
shopt -u nullglob
if [ "${#_failed[@]}" -gt 3 ]; then
    _failed_sorted=()
    while IFS= read -r _line; do
        _failed_sorted+=( "$_line" )
    done < <( printf '%s\n' "${_failed[@]}" | LC_ALL=C sort -r )
    for ((_i=3; _i<${#_failed_sorted[@]}; _i++)); do
        log_warn "Pruning old quarantine dir: ${_failed_sorted[$_i]}"
        rm -rf "${_failed_sorted[$_i]}"
    done
fi

# Short-circuit when marker matches, unless --force. The M7 retro incident
# (#293, 2026-05-10) proved the marker alone can lie: tar extraction can
# finish, the marker can flip to vN, while individual PNGs stay at vN-1
# content. Spot-check the bundled byte-integrity sidecar before trusting it.
if [ "$FORCE" -eq 0 ] && [ -f "$MARKER_FILE" ]; then
    INSTALLED_VERSION=$(tr -d '[:space:]' < "$MARKER_FILE")
    if [ "$INSTALLED_VERSION" = "$PINNED_VERSION" ]; then
        verify_byte_manifest "$IMAGES_DIR"
        verify_rc=$?
        case "$verify_rc" in
            0)
                log_info "Images already at ${PINNED_VERSION} — skipping download"
                exit 0
                ;;
            2)
                # Legacy release (v1–v4) without files.sha256. Nothing to
                # verify against — trust the marker so existing Pis don't
                # re-download on every run.
                log_info "Images already at ${PINNED_VERSION} (legacy release, marker-only check) — skipping download"
                exit 0
                ;;
            *)
                log_warn "Marker reports ${PINNED_VERSION} but on-disk PNGs don't match the bundled byte manifest — forcing re-download"
                # #314: remember the on-disk content is corrupt so that:
                #   1. If the re-download path takes a graceful-offline exit
                #      (network down, asset missing, etc.), we quarantine the
                #      corrupt PNGs rather than leave them rendering.
                #   2. If post-install verify ALSO fails on the new content,
                #      we don't roll back to the known-corrupt OLD_DIR.
                VERIFY_FAILED_AT_SHORT_CIRCUIT=1
                ;;
        esac
    else
        log_info "Image update: ${INSTALLED_VERSION} → ${PINNED_VERSION}"
    fi
else
    log_info "Fetching images at ${PINNED_VERSION}"
fi

RELEASE_TAG="litclock-images-${PINNED_VERSION}"

# Auth handling lives in lib/github_api.sh::github_api_auth_args — it honors
# GH_TOKEN then GITHUB_TOKEN. No-op once the repo is public.

# Put all working files ON THE SAME FILESYSTEM as $IMAGES_DIR so the final
# swap is a true rename (no cross-FS cp+rm that could fail mid-way and
# destroy the previous good set). mktemp under $REPO_ROOT instead of /tmp.
TMPDIR_ROOT=$(mktemp -d "${REPO_ROOT}/.litclock-images-staging.XXXXXX")
trap 'rm -rf "$TMPDIR_ROOT"' EXIT

TARBALL="$TMPDIR_ROOT/$ASSET_NAME"
SHA_FILE="$TMPDIR_ROOT/${ASSET_NAME}.sha256"

# Step 1: fetch release metadata to resolve asset IDs.
# Using the REST API endpoint is required for private-repo downloads
# (browser URL returns 404 even with Bearer auth on private repos).
META_FILE="$TMPDIR_ROOT/release.json"
META_URL="${API_BASE_URL}/repos/${REPO_SLUG}/releases/tags/${RELEASE_TAG}"
if ! github_api_curl "$META_URL" "application/vnd.github+json" "$META_FILE"; then
    log_warn "Failed to fetch release metadata for ${RELEASE_TAG} — leaving existing images untouched"
    quarantine_if_verify_failed
    exit 0
fi

# Step 2: extract the two asset IDs we need from the metadata.
ASSET_IDS=$(python3 -c "
import json, sys
data = json.load(open('$META_FILE'))
ids = {a['name']: a['id'] for a in data.get('assets', [])}
tar = ids.get('$ASSET_NAME')
sha = ids.get('${ASSET_NAME}.sha256')
if tar is None or sha is None:
    sys.stderr.write('Release is missing expected assets (tar={}, sha={})\n'.format(tar is not None, sha is not None))
    sys.exit(1)
print(tar)
print(sha)
" 2>&1) || {
    log_warn "$ASSET_IDS"
    quarantine_if_verify_failed
    exit 0
}
TAR_ID=$(echo "$ASSET_IDS" | sed -n '1p')
SHA_ID=$(echo "$ASSET_IDS" | sed -n '2p')

# Step 3: download both assets via the API endpoint (octet-stream).
# NB: assets can be multi-MB and slower than metadata, so use a longer timeout
# here rather than the lib default of 10s.
TAR_DL_URL="${API_BASE_URL}/repos/${REPO_SLUG}/releases/assets/${TAR_ID}"
SHA_DL_URL="${API_BASE_URL}/repos/${REPO_SLUG}/releases/assets/${SHA_ID}"

if ! LITCLOCK_GITHUB_API_TIMEOUT="${LITCLOCK_IMAGES_DOWNLOAD_TIMEOUT:-300}" \
        github_api_curl "$TAR_DL_URL" "application/octet-stream" "$TARBALL"; then
    log_warn "Failed to download tarball asset (id ${TAR_ID}) — leaving existing images untouched"
    quarantine_if_verify_failed
    exit 0
fi

if ! github_api_curl "$SHA_DL_URL" "application/octet-stream" "$SHA_FILE"; then
    log_warn "Failed to download SHA256 asset (id ${SHA_ID}) — cannot verify integrity, skipping"
    quarantine_if_verify_failed
    exit 0
fi

# Step 4: verify. Empty SHA file is a broken release (publisher error) — exit 1.
# SHA mismatch is also a hard error — don't overwrite good images with suspect data.
EXPECTED_SHA=$(awk '{print $1}' "$SHA_FILE")
if [ -z "$EXPECTED_SHA" ]; then
    log_error "Empty SHA file downloaded from ${SHA_DL_URL} — release is malformed"
    # #314: even though this is a publisher error (exit 1, not graceful 0),
    # the on-disk content may already be known-corrupt. Quarantine it before
    # exiting so the corrupt PNGs don't keep rendering until the publisher
    # cuts a fixed release. Without this, a single bad release ships a
    # stuck bad-content render across the fleet with no "!" glyph signal.
    quarantine_if_verify_failed
    exit 1
fi

ACTUAL_SHA=$(sha256sum "$TARBALL" | awk '{print $1}')
if [ "$EXPECTED_SHA" != "$ACTUAL_SHA" ]; then
    log_error "SHA256 mismatch — expected $EXPECTED_SHA, got $ACTUAL_SHA"
    log_warn "Leaving existing images untouched"
    quarantine_if_verify_failed
    exit 0
fi

# Step 5: extract. Use hardening flags to refuse symlink escapes and refuse
# to set ownership/permissions from the tar metadata. GNU tar 1.30+ already
# refuses ".." path traversal by default; these flags are belt-and-suspenders.
STAGING="$TMPDIR_ROOT/staging"
mkdir -p "$STAGING"
if ! tar -xzf "$TARBALL" -C "$STAGING" --no-same-owner --no-same-permissions --no-overwrite-dir; then
    log_error "Extraction failed (disk full or corrupt tarball)"
    quarantine_if_verify_failed
    exit 0
fi

# The tarball is "tar czf - images/" so it unpacks to STAGING/images.
UNPACKED="$STAGING/images"
if [ ! -d "$UNPACKED" ]; then
    log_error "Tarball did not contain an images/ directory at root"
    quarantine_if_verify_failed
    exit 0
fi

# Stamp the marker inside the unpacked dir so the swap is atomic.
echo "$PINNED_VERSION" > "$UNPACKED/.installed-version"

# Step 6: atomic swap. $STAGING and $IMAGES_DIR are on the same filesystem
# (both under $REPO_ROOT), so mv is a true rename. Still handle failure
# carefully — if the swap mv fails, restore the old images and exit 1.
OLD_DIR=""
if [ -d "$IMAGES_DIR" ]; then
    OLD_DIR="${IMAGES_DIR}.old.$$"
    if ! mv "$IMAGES_DIR" "$OLD_DIR"; then
        log_error "Failed to move existing images aside — aborting, no changes made"
        exit 1
    fi
fi

if ! mv "$UNPACKED" "$IMAGES_DIR"; then
    log_error "Final swap failed — attempting to restore previous images"
    if [ -n "$OLD_DIR" ] && [ -d "$OLD_DIR" ]; then
        if mv "$OLD_DIR" "$IMAGES_DIR"; then
            log_warn "Previous images restored"
        else
            log_error "Restore also failed — $OLD_DIR is orphan on disk"
        fi
    fi
    exit 1
fi

# Step 7 (#293): post-install byte-integrity verification. The tarball SHA256
# check confirms the bytes we downloaded are intact; this check confirms the
# bytes we EXTRACTED match — catching the M7-retro silent-failure mode where
# the marker advanced but on-disk PNG content didn't. Run BEFORE rm'ing
# OLD_DIR so we can roll back if anything's amiss.
verify_byte_manifest "$IMAGES_DIR"
verify_rc=$?
case "$verify_rc" in
    0)
        # #313: bytes match the sidecar; now confirm the sidecar covers
        # every file referenced by manifest.json. A partial-emission bug in
        # quote_to_image.php (credits-write fails, main-write succeeds) would
        # produce a sidecar that's internally consistent but incomplete vs
        # runtime expectations. release_images.sh's release-time gate is the
        # primary defense; this is defense-in-depth.
        if ! verify_manifest_completeness "$IMAGES_DIR"; then
            log_error "Byte sidecar passes BUT manifest.json references files not in sidecar — release is partial"
            _post_install_verify_failure_rollback
            exit 1
        fi
        log_info "Byte-integrity verification OK"
        ;;
    2)
        log_warn "Release tarball did not include files.sha256 sidecar — skipping byte verification (legacy release)"
        ;;
    *)
        log_error "Byte-integrity verification FAILED — on-disk content does not match the bundled manifest"
        verify_byte_manifest_detail "$IMAGES_DIR"
        # Preserve the broken new content under a timestamped name so the
        # operator can inspect WHAT shipped wrong — the M7 retro lesson was
        # "don't lose the evidence." Operator removes manually after debugging
        # (see PLAN-LitClock-Control-PWA.md retention policy follow-up).
        _post_install_verify_failure_rollback
        exit 1
        ;;
esac

# Only safe to delete OLD_DIR after successful swap AND verification.
if [ -n "$OLD_DIR" ] && [ -d "$OLD_DIR" ]; then
    rm -rf "$OLD_DIR"
fi

# #314: successful install — clear the update-failed glyph so the e-ink
# returns to clean rendering on next tick.
_clear_update_failed_marker

log_info "Installed images at ${PINNED_VERSION}"
