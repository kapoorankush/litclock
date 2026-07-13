#!/bin/bash
#
# LitClock Image Release Helper
#
# One command to publish a new quote-image release. Tars the current
# images/ directory, hashes it, and creates the GitHub release with
# both assets in one go.
#
# Usage:
#   scripts/release_images.sh v2
#
# What it does NOT do:
#   - Bump .images-version (that change belongs in a reviewed PR, not here).
#   - Tag a new OS image release. After bumping .images-version and merging,
#     manually trigger .github/workflows/build-image.yml (or push a v*
#     tag) so fresh SD flashes ship the new quotes.
#
# Expects:
#   - `gh` authenticated against the repo.
#   - Working tree clean (refuses to run otherwise — the tarball captures
#     your current images/ directory, so uncommitted state would leak).
#   - images/ populated (regenerate via image-gen/quote_to_image.php first
#     if you just edited the CSV).

set -euo pipefail

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

if [ "$#" -ne 1 ]; then
    echo "Usage: $0 vN" >&2
    echo "Example: $0 v2" >&2
    exit 1
fi

VERSION="$1"

if ! [[ "$VERSION" =~ ^v[0-9]+$ ]]; then
    log_error "Version must match 'v<integer>' (got: $VERSION)"
    exit 1
fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
cd "$REPO_ROOT"

# Working tree must not have uncommitted changes to TRACKED files. Untracked
# files (backup CSVs, scratch dirs, TODOS.md, etc.) don't affect the tarball
# since tar is pointed at images/ only — allow those to exist.
if ! git diff --quiet HEAD; then
    log_error "There are uncommitted changes to tracked files. Commit or stash before releasing."
    git diff --stat HEAD >&2
    exit 1
fi
if ! git diff --quiet --cached HEAD; then
    log_error "There are staged changes. Commit or unstage before releasing."
    git diff --cached --stat HEAD >&2
    exit 1
fi

if [ ! -d "$REPO_ROOT/images" ]; then
    log_error "No images/ directory at $REPO_ROOT/images — regenerate via image-gen/quote_to_image.php first"
    exit 1
fi

CURRENT_SHA=$(git rev-parse HEAD)
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)

# Pre-flight: does this release already exist?
RELEASE_TAG="litclock-images-${VERSION}"
if gh release view "$RELEASE_TAG" >/dev/null 2>&1; then
    log_error "Release $RELEASE_TAG already exists"
    log_warn "If you need to replace it, delete it first: gh release delete $RELEASE_TAG --yes"
    exit 1
fi

TMPDIR_ROOT=$(mktemp -d -t litclock-release.XXXXXX)
trap 'rm -rf "$TMPDIR_ROOT"' EXIT

TARBALL="$TMPDIR_ROOT/litclock-images.tar.gz"
SHA_FILE="$TMPDIR_ROOT/litclock-images.tar.gz.sha256"

# #293: stage images/ inside TMPDIR_ROOT so the byte manifest and the tarball
# both cover the SAME frozen byte set. Without staging, anything mutating
# $REPO_ROOT/images between manifest generation and tar creation (concurrent
# image-gen run, errant editor, fsck) would publish a tarball whose inner
# content doesn't match its own sidecar — every consumer install would then
# reject it. Staging closes that release-time TOCTOU.
log_info "Staging images/ for release (freezes the byte set)..."
STAGING_IMAGES="$TMPDIR_ROOT/images"
cp -a "$REPO_ROOT/images" "$STAGING_IMAGES"

# Re-anchor manifest.json to the staged copy. The original release flow read
# from $REPO_ROOT/images/manifest.json directly; with staging, we read from
# the frozen snapshot to match the byte set we'll actually publish.
MANIFEST_SRC="$STAGING_IMAGES/manifest.json"

# #299: refuse to publish without a manifest.json. If we created the release
# without one, the corpus-integrity CI gate would later fail PRs against the
# resulting tag — but the tag would already be occupied, blocking a clean
# rerun. Fail-loud here. (Originally checked after tarball build; moved
# earlier in #313 so the manifest-completeness gate can rely on its presence
# without re-checking.)
if [ ! -f "$MANIFEST_SRC" ]; then
    log_error "images/manifest.json missing — refuse to release without it (corpus-integrity CI gate would fail)."
    log_error "  Re-run image-gen/quote_to_image.php to regenerate the manifest, then retry."
    exit 1
fi

# #293: byte-integrity manifest. The existing manifest.json hashes corpus
# content (quote+title+author+timestring) for the CI corpus-drift gate — it
# does NOT detect partial-extract corruption on the consumer side. Generate
# a separate sha256sum-compatible sidecar of every PNG's bytes and bundle it
# inside the tarball so download_images.sh can verify post-install.
log_info "Generating byte-integrity manifest (images/files.sha256)..."
FILES_SHA="$STAGING_IMAGES/files.sha256"
# Hash all PNGs under images/ (root + metadata/). Sort for reproducibility:
# tarball SHA256 should be deterministic across release runs from identical
# image content, so sha256sum input order must be deterministic too.
# `xargs -r` (--no-run-if-empty) guards against the find-returns-nothing
# case: without it, sha256sum gets invoked with no args, reads stdin (empty
# pipe), and writes `e3b0...  -` to the sidecar — a corrupt entry that would
# fail verification on every consumer install. Belt-and-suspenders: also
# require ≥1 entry to publish.
( cd "$STAGING_IMAGES" \
  && find . -type f -name '*.png' -print0 \
     | LC_ALL=C sort -z \
     | xargs -0 -r sha256sum \
  ) > "$FILES_SHA"
FILES_SHA_COUNT=$(wc -l < "$FILES_SHA")
if [ "$FILES_SHA_COUNT" -lt 1 ]; then
    log_error "files.sha256 ended up empty — no PNGs found under $REPO_ROOT/images"
    log_error "  Regenerate via image-gen/quote_to_image.php first, then retry"
    exit 1
fi
log_info "  files.sha256: ${FILES_SHA_COUNT} PNGs hashed"

# #313: cross-check the byte sidecar against manifest.json's file set.
# files.sha256 verifies every PNG it lists matches its hash, but says nothing
# about completeness — a manifest.json claiming N quote buckets must be
# satisfied by N main PNGs (root) AND N credits PNGs (metadata/) in the
# sidecar. Without this, a partial-emission bug in quote_to_image.php (e.g.,
# the credits-write fails but the main-write succeeded) would silently ship
# a release whose runtime would crash on missing minutes.
log_info "Cross-checking byte manifest vs images/manifest.json..."
if ! python3 - "$STAGING_IMAGES" <<'PY'
import json
import sys
from pathlib import Path

staging = Path(sys.argv[1])
manifest = json.loads((staging / "manifest.json").read_text())
expected_main = set(manifest.get("files", {}).keys())
if not expected_main:
    print("ERROR: manifest.json has empty 'files' map — refuse to publish", file=sys.stderr)
    sys.exit(1)

# files.sha256 entries look like `<hex>  ./quote_HHMM_N.png` or
# `<hex>  ./metadata/quote_HHMM_N_credits.png`.
sidecar = (staging / "files.sha256").read_text().splitlines()
sidecar_paths = {ln.split("  ", 1)[1].lstrip("./") for ln in sidecar if "  " in ln}

expected_credits = {f"metadata/{f.replace('.png', '_credits.png')}" for f in expected_main}
missing_main = expected_main - sidecar_paths
missing_credits = expected_credits - sidecar_paths
extras = sidecar_paths - (expected_main | expected_credits)

if missing_main or missing_credits:
    print("ERROR: manifest completeness check failed — byte sidecar missing files referenced by manifest.json:", file=sys.stderr)
    for f in sorted(missing_main)[:10]:
        print(f"  missing main:    {f}", file=sys.stderr)
    if len(missing_main) > 10:
        print(f"  ... +{len(missing_main) - 10} more main", file=sys.stderr)
    for f in sorted(missing_credits)[:10]:
        print(f"  missing credits: {f}", file=sys.stderr)
    if len(missing_credits) > 10:
        print(f"  ... +{len(missing_credits) - 10} more credits", file=sys.stderr)
    print("  Regenerate images via: php image-gen/quote_to_image.php", file=sys.stderr)
    sys.exit(1)

if extras:
    print(f"WARN: {len(extras)} orphan PNGs in images/ not referenced by manifest.json:", file=sys.stderr)
    for f in sorted(extras)[:5]:
        print(f"  orphan: {f}", file=sys.stderr)
    if len(extras) > 5:
        print(f"  ... +{len(extras) - 5} more", file=sys.stderr)
    print("  (orphans are warned but not fatal — re-run quote_to_image.php to clean)", file=sys.stderr)

print(f"OK: {len(expected_main)} main + {len(expected_credits)} credits = {len(expected_main) + len(expected_credits)} required files all present")
PY
then
    log_error "Manifest completeness check FAILED — refuse to publish"
    exit 1
fi

log_info "Building tarball from staged images/ (this may take a minute)..."
tar -czf "$TARBALL" -C "$TMPDIR_ROOT" images

TARBALL_BYTES=$(stat -c%s "$TARBALL" 2>/dev/null || stat -f%z "$TARBALL")
log_info "Tarball size: $((TARBALL_BYTES / 1024 / 1024)) MB"

log_info "Computing SHA256..."
(cd "$TMPDIR_ROOT" && sha256sum litclock-images.tar.gz > litclock-images.tar.gz.sha256)
log_info "SHA256: $(awk '{print $1}' "$SHA_FILE")"

# #299: publish images/manifest.json as a separate top-level asset so the
# corpus-integrity CI workflow can fetch ~50KB of metadata instead of the
# whole tarball. The manifest is also bundled inside the tarball (it lives
# in images/), so Pis still get it via the normal install/update flow.
# (Existence already validated above before sidecar generation; #293 reads
# from the frozen staging snapshot so the manifest corresponds to the same
# byte set as the tarball + sidecar.)
MANIFEST_ASSET="$TMPDIR_ROOT/manifest.json"
cp "$MANIFEST_SRC" "$MANIFEST_ASSET"
RELEASE_FILES=("$TARBALL" "$SHA_FILE" "$MANIFEST_ASSET")
log_info "Including manifest.json as a release asset"

log_info "Creating release $RELEASE_TAG..."
RELEASE_NOTES=$(cat <<EOF
Pre-generated quote images for LitClock, pinned by \`.images-version\`.
Fetched at install and build time by \`scripts/download_images.sh\`.
Auto-generated, not for standalone use.

- Source commit: \`${CURRENT_SHA}\`
- Source branch at release time: \`${CURRENT_BRANCH}\`
- SHA256: \`$(awk '{print $1}' "$SHA_FILE")\`
- Tarball size: $((TARBALL_BYTES / 1024 / 1024)) MB
EOF
)
gh release create "$RELEASE_TAG" \
    --title "Quote Images ${VERSION}" \
    --notes "$RELEASE_NOTES" \
    "${RELEASE_FILES[@]}"

log_info "Done."
echo ""
log_info "Next steps (manual):"
echo "  1. On a feature branch, update .images-version to ${VERSION}"
echo "  2. Open a PR, review, merge to master"
echo "  3. Manually dispatch .github/workflows/build-image.yml (or push a v* tag)"
echo "     so fresh SD flashes ship the new quotes"
