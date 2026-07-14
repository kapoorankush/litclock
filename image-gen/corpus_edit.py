"""One-command workflow for corpus edits.

Handles the full pipeline from a working-tree edit of
``image-gen/litclock_annotated.csv`` through a published image release and an
open PR. Wraps the existing tools — it does not replace them.

Subcommands:

    validate    Every changed row's timestring matches its HH:MM tag.
    diff        List HH:MM buckets whose contents differ from git HEAD,
                AND surface drift between images/manifest.json and the
                current CSV (post-#299).
    regenerate  Wipe dirty buckets' images, then run the PHP generator.
    ship MSG    validate -> regenerate -> bump .images-version
                -> commit on a new branch -> release_images.sh -> push -> gh pr create.

Why this exists: image filenames are ``quote_{HHMM}_{counter}.png`` with the
counter assigned by CSV row order per time bucket. Any add / delete / retag
invalidates every filename in the affected buckets. Pre-#299 the generator's
file_exists short-circuit silently preserved the stale images; post-#299 the
``images/manifest.json`` content-hash sidecar drives a content-aware skip and
``.github/workflows/corpus-integrity.yml`` blocks PRs that don't pair their
CSV change with a matching release. See issues #211 and #299.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from time_parser import validate_time_phrase

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_REL = "image-gen/litclock_annotated.csv"
CORPUS_PATH = REPO_ROOT / CORPUS_REL
IMAGES_DIR = REPO_ROOT / "images"
METADATA_DIR = IMAGES_DIR / "metadata"
MANIFEST_PATH = IMAGES_DIR / "manifest.json"
# Renamed aside (not deleted) before a full regen so a failed/interrupted PHP run
# rolls back to the last-known-good manifest instead of losing it (#502 review R4).
MANIFEST_BAK = IMAGES_DIR / "manifest.json.bak"
IMAGES_VERSION_REL = ".images-version"
IMAGES_VERSION_FILE = REPO_ROOT / IMAGES_VERSION_REL
PHP_GENERATOR_REL = "image-gen/quote_to_image.php"
PHP_GENERATOR = REPO_ROOT / PHP_GENERATOR_REL
RELEASE_SCRIPT = REPO_ROOT / "scripts" / "release_images.sh"
# Base branch a ship compares against to decide what changed on the feature branch.
SHIP_BASE_BRANCH = "master"

MIN_CORPUS_COLS = 5
VERSION_RE = re.compile(r"^v(\d+)$")


@dataclass(frozen=True)
class Row:
    time: str
    match: str
    quote: str
    title: str
    author: str
    is_nsfw: bool

    def fingerprint(self) -> str:
        h = hashlib.sha1()
        h.update(
            "|".join([self.time, self.match, self.quote, self.title, self.author, "1" if self.is_nsfw else "0"]).encode(
                "utf-8"
            )
        )
        return h.hexdigest()


def parse_corpus(text: str) -> list[Row]:
    rows: list[Row] = []
    reader = csv.reader(text.splitlines(), delimiter="|")
    for raw in reader:
        if len(raw) < MIN_CORPUS_COLS:
            continue
        is_nsfw = len(raw) >= 6 and raw[5].strip().upper() == "YES"
        rows.append(
            Row(
                time=raw[0].strip(),
                match=raw[1].strip(),
                quote=raw[2].strip(),
                title=raw[3].strip(),
                author=raw[4].strip(),
                is_nsfw=is_nsfw,
            )
        )
    return rows


def bucket_key(time: str) -> str:
    return time[:2] + time[3:5]


def bucket_fingerprints(rows: list[Row]) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        buckets[bucket_key(row.time)].append(row.fingerprint())
    return dict(buckets)


def image_content_hash(quote: str, title: str, author: str, timestring: str) -> str:
    """SHA1 of the per-row image content tuple.

    MUST stay byte-for-byte in sync with quote_to_image.php's hash logic
    (#299/F): trimmed CSV values for quote/title/author/timestring, JSON-array
    encoded with no whitespace, then SHA1 of the UTF-8 bytes.

    Python `json.dumps(..., separators=(",",":"), ensure_ascii=False)` matches
    PHP `json_encode($a, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE)`
    byte-for-byte for our inputs (verified for ASCII, UTF-8, and pipe/quote
    edge cases). Pre-F we joined with `|`, but pipe-in-field broke uniqueness.
    """
    payload = json.dumps(
        [quote, title, author, timestring],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def corpus_file_hash(csv_text: str | None = None) -> str:
    """SHA1 of the CSV file content (matches PHP sha1_file)."""
    if csv_text is None:
        return hashlib.sha1(CORPUS_PATH.read_bytes()).hexdigest()
    return hashlib.sha1(csv_text.encode("utf-8")).hexdigest()


def generator_file_hash() -> str:
    """SHA1 of the PHP generator — matches the ``generator_hash`` PHP writes to
    the manifest via ``sha1_file(__FILE__)``. A renderer change (bold logic,
    layout, fonts) bumps this without touching the CSV."""
    return hashlib.sha1(PHP_GENERATOR.read_bytes()).hexdigest()


def read_manifest() -> dict | None:
    """Load images/manifest.json, or return None if missing/unparseable.

    Manifest schema (#299):
        {
          "corpus_hash":    "<sha1 of litclock_annotated.csv>",
          "generator_hash": "<sha1 of quote_to_image.php>",
          "created_at":     "<ISO8601 UTC>",
          "files":          {"<filename>": "<image_content_hash>", ...}
        }
    """
    if not MANIFEST_PATH.exists():
        return None
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def per_row_filenames(rows: list[Row]) -> list[tuple[Row, str]]:
    """Yield (row, filename) for each row in CSV order.

    Mirrors quote_to_image.php's per-bucket counter: counter resets to 0 at
    each new HHMM bucket, increments for consecutive rows in the same bucket.
    The _nsfw suffix is included when row.is_nsfw is True.
    """
    counter = 0
    previous: str | None = None
    out: list[tuple[Row, str]] = []
    for row in rows:
        key = bucket_key(row.time)
        if previous is not None and key == previous:
            counter += 1
        else:
            counter = 0
        previous = key
        suffix = "_nsfw" if row.is_nsfw else ""
        out.append((row, f"quote_{key}_{counter}{suffix}.png"))
    return out


def manifest_mismatches(rows: list[Row], manifest_files: dict[str, str]) -> list[tuple[str, str | None, str]]:
    """List per-row hash mismatches against the manifest.

    Returns (filename, manifest_hash_or_None, expected_hash) for each row
    whose expected content hash disagrees with what the manifest recorded.
    """
    out: list[tuple[str, str | None, str]] = []
    for row, filename in per_row_filenames(rows):
        expected = image_content_hash(row.quote, row.title, row.author, row.match)
        actual = manifest_files.get(filename)
        if actual != expected:
            out.append((filename, actual, expected))
    return out


def run(
    cmd: list[str], *, cwd: Path | None = None, check: bool = True, capture: bool = False
) -> subprocess.CompletedProcess:
    """Small wrapper around subprocess.run so tests can monkeypatch one point."""
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=check,
        text=True,
        capture_output=capture,
    )


def read_head_corpus() -> str:
    """Return CSV contents at HEAD, or '' if the file is new."""
    try:
        result = run(
            ["git", "show", f"HEAD:{CORPUS_REL}"],
            cwd=REPO_ROOT,
            check=True,
            capture=True,
        )
    except subprocess.CalledProcessError:
        return ""
    return result.stdout


def current_branch() -> str:
    result = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=REPO_ROOT, capture=True)
    return result.stdout.strip()


def tracked_diff_paths() -> list[str]:
    result = run(["git", "diff", "--name-only", "HEAD"], cwd=REPO_ROOT, capture=True)
    return [p for p in result.stdout.splitlines() if p.strip()]


def compute_dirty_buckets(head_rows: list[Row], work_rows: list[Row]) -> set[str]:
    before = bucket_fingerprints(head_rows)
    after = bucket_fingerprints(work_rows)
    dirty: set[str] = set()
    for key in set(before) | set(after):
        if before.get(key) != after.get(key):
            dirty.add(key)
    return dirty


def diff_changed_rows(head_rows: list[Row], work_rows: list[Row]) -> list[Row]:
    """Rows that exist in work and not in head (by exact fingerprint).

    Used to decide which rows to time-validate. A retag shows up as a new
    fingerprint on the work side. Deletions drop off — we don't validate
    them (there's nothing to check).
    """
    head_fps = {r.fingerprint() for r in head_rows}
    return [r for r in work_rows if r.fingerprint() not in head_fps]


def validate_rows(rows: list[Row]) -> list[str]:
    errors: list[str] = []
    for row in rows:
        ok, parsed = validate_time_phrase(row.match, row.time)
        if not ok:
            errors.append(f"  time {row.time} / match {row.match!r} -> parsed {parsed!r}; quote: {row.quote[:80]!r}")
    return errors


def validate_bucket_contiguity(rows: list[Row]) -> list[str]:
    """Flag any HHMM bucket that appears non-contiguously in CSV row order.

    The PHP per-bucket counter (`$imagenumber`) only resets on a NEW bucket;
    if the same bucket reappears later in the CSV, both runs of rows compete
    for `quote_HHMM_0.png`, `quote_HHMM_1.png`, etc. and the second run
    silently overwrites the first. The Python mirror (per_row_filenames) has
    the same assumption. The current corpus is naturally sorted, but a
    manual edit could break ordering without any other check catching it.
    Run as part of `corpus_edit.py validate`. (#299/E)
    """
    errors: list[str] = []
    last_bucket: str | None = None
    seen_then_left: set[str] = set()
    for idx, row in enumerate(rows):
        key = bucket_key(row.time)
        if key != last_bucket:
            if key in seen_then_left:
                errors.append(
                    f"  bucket {key[:2]}:{key[2:]} reappears at CSV row {idx + 1} after a different bucket — "
                    "rows in the same time bucket must be contiguous (PHP counter resets corrupt filenames otherwise)."
                )
            if last_bucket is not None:
                seen_then_left.add(last_bucket)
            last_bucket = key
    return errors


def wipe_buckets(dirty: set[str], *, dry_run: bool) -> list[Path]:
    removed: list[Path] = []
    if not dirty:
        return removed
    for key in sorted(dirty):
        for directory, suffix in ((IMAGES_DIR, ".png"), (METADATA_DIR, "_credits.png")):
            if not directory.exists():
                continue
            pattern = f"quote_{key}_*{suffix}"
            for path in directory.glob(pattern):
                removed.append(path)
                if not dry_run:
                    path.unlink()
    return removed


def read_version() -> str:
    if not IMAGES_VERSION_FILE.exists():
        raise SystemExit(f"ERROR: {IMAGES_VERSION_FILE} missing")
    return IMAGES_VERSION_FILE.read_text().strip()


def next_version(current: str) -> str:
    m = VERSION_RE.match(current)
    if not m:
        raise SystemExit(f"ERROR: .images-version {current!r} is not in vN format")
    return f"v{int(m.group(1)) + 1}"


def write_version(version: str, *, dry_run: bool) -> None:
    if dry_run:
        return
    IMAGES_VERSION_FILE.write_text(version + "\n")


def _parse_sides() -> tuple[list[Row], list[Row]]:
    head_text = read_head_corpus()
    head_rows = parse_corpus(head_text) if head_text else []
    work_rows = parse_corpus(CORPUS_PATH.read_text(encoding="utf-8"))
    return head_rows, work_rows


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_validate(_args: argparse.Namespace) -> int:
    head_rows, work_rows = _parse_sides()

    # Bucket contiguity is a whole-corpus invariant, not a per-changed-row
    # check — a re-ordering that splits an existing bucket may not change any
    # row's fingerprint (just their order), so it slips past diff_changed_rows.
    contiguity_errors = validate_bucket_contiguity(work_rows)
    if contiguity_errors:
        print(
            f"ERROR: {len(contiguity_errors)} non-contiguous bucket(s) detected (PHP counter would corrupt filenames):",
            file=sys.stderr,
        )
        for line in contiguity_errors:
            print(line, file=sys.stderr)
        return 2

    changed = diff_changed_rows(head_rows, work_rows)
    if not changed:
        print("No changed rows vs HEAD; bucket contiguity OK; nothing else to validate.")
        return 0
    errors = validate_rows(changed)
    if errors:
        print(f"ERROR: {len(errors)} changed row(s) fail time-tag validation:", file=sys.stderr)
        for line in errors:
            print(line, file=sys.stderr)
        return 2
    print(f"OK: {len(changed)} changed row(s) validate cleanly; bucket contiguity OK.")
    return 0


def cmd_diff(_args: argparse.Namespace) -> int:
    head_rows, work_rows = _parse_sides()
    dirty = compute_dirty_buckets(head_rows, work_rows)

    # Section 1: dirty buckets vs git HEAD — what `ship` would regenerate.
    if dirty:
        print(f"{len(dirty)} dirty bucket(s) vs HEAD:")
        for key in sorted(dirty):
            before = bucket_fingerprints(head_rows).get(key, [])
            after = bucket_fingerprints(work_rows).get(key, [])
            print(f"  {key[:2]}:{key[2:]}  ({len(before)} -> {len(after)} row(s))")
    else:
        print("No dirty buckets vs HEAD.")

    # Section 2: manifest health — is the on-disk images/ in sync with the
    # current CSV? Independent of HEAD comparison; surfaces #299-class drift
    # (CSV edits made without re-running corpus_edit ship).
    print()
    manifest = read_manifest()
    if manifest is None:
        rel = MANIFEST_PATH.relative_to(REPO_ROOT) if MANIFEST_PATH.is_relative_to(REPO_ROOT) else MANIFEST_PATH
        print(f"Manifest: {rel} missing or unreadable — run regenerate to create it.")
        return 0

    expected_corpus = corpus_file_hash()
    actual_corpus = manifest.get("corpus_hash") if isinstance(manifest, dict) else None
    if expected_corpus == actual_corpus:
        print(f"Manifest corpus_hash matches current CSV ({expected_corpus[:12]}…).")
    else:
        print("Manifest corpus_hash MISMATCH:")
        print(f"  manifest:    {actual_corpus}")
        print(f"  current CSV: {expected_corpus}")
        print("  Run `python3 image-gen/corpus_edit.py regenerate` to refresh.")

    files = manifest.get("files", {}) if isinstance(manifest, dict) else {}
    if not isinstance(files, dict):
        files = {}
    mismatches = manifest_mismatches(work_rows, files)
    if mismatches:
        print(f"\n{len(mismatches)} per-row content-hash mismatch(es) (filename | manifest -> CSV):")
        for filename, mh, ch in mismatches[:20]:
            mh_disp = mh[:12] if mh else "(missing)"
            print(f"  {filename}  {mh_disp} -> {ch[:12]}")
        if len(mismatches) > 20:
            print(f"  ... and {len(mismatches) - 20} more.")
    return 0


def _manifest_out_of_sync() -> bool:
    """True when the on-disk manifest doesn't match the current CSV.

    Used by cmd_regenerate as a fallback trigger for the case where the CSV
    changed AND was already committed (so vs-HEAD diff is clean), but the
    on-disk images are stale relative to the committed CSV.
    """
    manifest = read_manifest()
    if manifest is None:
        return True
    if not isinstance(manifest, dict):
        return True
    return manifest.get("corpus_hash") != corpus_file_hash()


def _generator_out_of_sync() -> bool:
    """True when the manifest was written by a DIFFERENT quote_to_image.php than
    the current one. A renderer change (bold logic, layout, fonts) can alter any
    image WITHOUT touching the CSV — and PHP's per-image skip is content-hash
    (data) only, so it would skip every image. Without this check, `regenerate`
    / `ship` would silently no-op on a pure renderer change.
    """
    manifest = read_manifest()
    if manifest is None:
        return True
    if not isinstance(manifest, dict):
        return True
    return manifest.get("generator_hash") != generator_file_hash()


def _changed_vs_base(path_rel: str, base: str = SHIP_BASE_BRANCH) -> bool:
    """True when `path_rel` differs between the merge-base with `base` and HEAD —
    i.e. this feature branch changed that file in a COMMIT.

    Ship uses this (not the on-disk manifest) to decide what to ship: it's durable
    across a local `regenerate` (which rewrites the manifest and erases
    `_generator_out_of_sync`), it can't be faked by a missing local manifest, and
    it correctly sees committed CSV/renderer edits (#502 review R2). Returns False
    (and prints a warning) if the base ref is unavailable, so a shallow/base-less
    checkout degrades to "no committed change detected" rather than crashing.
    """
    probe = run(
        ["git", "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"], cwd=REPO_ROOT, capture=True, check=False
    )
    if probe.returncode != 0:
        print(f"WARN: base ref {base!r} not found — cannot detect committed changes to {path_rel}.", file=sys.stderr)
        return False
    result = run(["git", "diff", "--name-only", f"{base}...HEAD", "--", path_rel], cwd=REPO_ROOT, capture=True)
    return bool(result.stdout.strip())


def _remove_manifest_for_regen(*, dry_run: bool) -> None:
    """Rename the manifest aside so PHP (whose per-image skip is content-hash only)
    has no recorded hashes to match and regenerates the WHOLE corpus. Kept as a
    .bak so `_run_generator` can roll back on a failed/interrupted PHP run."""
    if dry_run or not MANIFEST_PATH.exists():
        return
    MANIFEST_PATH.replace(MANIFEST_BAK)


def _restore_manifest_bak() -> None:
    if MANIFEST_BAK.exists():
        MANIFEST_BAK.replace(MANIFEST_PATH)


def _discard_manifest_bak() -> None:
    if MANIFEST_BAK.exists():
        MANIFEST_BAK.unlink()


def _prepare_for_regen(dirty: list[str], manifest_drift: bool, generator_drift: bool, *, dry_run: bool) -> None:
    """Force PHP to regenerate the right images. SINGLE source of truth for the
    wipe-or-remove-manifest decision, shared by cmd_regenerate and cmd_ship.

    Order matters (#502 review R1): a generator (renderer) change can alter EVERY
    image, so it MUST win over a dirty-bucket subset — otherwise PHP's content-hash
    skip leaves the unchanged buckets on the OLD renderer and the release ships
    mixed old/new-renderer PNGs. Generator drift therefore dominates `dirty`.
    """
    if generator_drift:
        print("Generator (quote_to_image.php) changed — removing manifest to force a full regen.")
        # Still wipe dirty buckets FIRST: a full regen overwrites current rows in
        # place but does NOT delete orphaned PNGs left by removed/renumbered rows
        # (a mixed CSV+renderer edit) — only wipe_buckets clears those, and the
        # release tarball would otherwise ship the stale orphans (#503 review P1).
        if dirty:
            removed = wipe_buckets(dirty, dry_run=dry_run)
            print(f"  (also wiped {len(removed)} file(s) in {len(dirty)} dirty bucket(s) to clear orphans.)")
        _remove_manifest_for_regen(dry_run=dry_run)
    elif dirty:
        removed = wipe_buckets(dirty, dry_run=dry_run)
        print(f"Wiped {len(removed)} stale image file(s) across {len(dirty)} bucket(s).")
    elif manifest_drift:
        print(
            "Manifest out of sync with CSV but no buckets dirty vs HEAD — "
            "letting PHP regenerate by manifest hash without an explicit wipe."
        )


def _run_generator() -> None:
    """Run the PHP generator and verify the manifest afterward. Shared by
    cmd_regenerate and cmd_ship. Rolls back a backed-up manifest (see
    `_remove_manifest_for_regen`) if PHP dies or writes no manifest, so a failed
    regen never leaves the tree with no last-known-good manifest (#502 review R4/R5).
    """
    if not PHP_GENERATOR.exists():
        _restore_manifest_bak()
        raise SystemExit(f"ERROR: {PHP_GENERATOR} missing")
    if not IMAGES_DIR.exists():
        _restore_manifest_bak()
        raise SystemExit(f"ERROR: {IMAGES_DIR} missing — populate it first via scripts/download_images.sh.")
    print(f"Running {PHP_GENERATOR.relative_to(REPO_ROOT)}…")
    try:
        run(["php", str(PHP_GENERATOR)], cwd=PHP_GENERATOR.parent)
    except BaseException:
        _restore_manifest_bak()
        raise
    if not MANIFEST_PATH.exists():
        _restore_manifest_bak()
        raise SystemExit(
            f"ERROR: PHP completed but {MANIFEST_PATH.relative_to(REPO_ROOT)} is missing — "
            "manifest write must have failed (check PHP stdout for 'ERROR: failed to write manifest.json')."
        )
    # PHP wrote a fresh manifest; the backup is obsolete. Drop it before the sync
    # checks so a check failure leaves the NEW manifest on disk for investigation.
    _discard_manifest_bak()
    if _manifest_out_of_sync():
        raise SystemExit(
            "ERROR: PHP completed but manifest.corpus_hash still doesn't match the current CSV — "
            "investigate before shipping."
        )
    if _generator_out_of_sync():
        raise SystemExit(
            "ERROR: PHP completed but manifest.generator_hash still doesn't match quote_to_image.php — "
            "investigate before shipping."
        )


def cmd_regenerate(args: argparse.Namespace) -> int:
    head_rows, work_rows = _parse_sides()
    dirty = compute_dirty_buckets(head_rows, work_rows)
    manifest_drift = _manifest_out_of_sync()
    generator_drift = _generator_out_of_sync()
    if not dirty and not manifest_drift and not generator_drift:
        print("No dirty buckets, manifest in sync, generator unchanged; skipping regenerate.")
        return 0
    _prepare_for_regen(dirty, manifest_drift, generator_drift, dry_run=args.dry_run)
    if args.dry_run:
        print("[dry-run] skipping PHP generator.")
        return 0
    _run_generator()
    return 0


def cmd_ship(args: argparse.Namespace) -> int:
    """Ship a new image release. Two modes:

    - CSV-EDIT: an UNCOMMITTED corpus edit in the working tree (classic flow — edit
      the CSV on master; ship branches, regenerates the dirty buckets, commits
      CSV + .images-version, releases).
    - GENERATOR: the renderer (quote_to_image.php) changed in a COMMIT on this
      feature branch, with no pending CSV edit (#502 review). Ships in place on the
      branch: full regen, commit .images-version only, release.
    """
    diff_paths = tracked_diff_paths()
    csv_uncommitted = CORPUS_REL in diff_paths

    # Preflight — no stray uncommitted tracked changes. A dirty .images-version left
    # by a half-finished prior run lands here and is rejected, which prevents a
    # double version bump on rerun (#502 review R6).
    allowed = {CORPUS_REL}
    unexpected = [p for p in diff_paths if p not in allowed]
    if unexpected:
        print(
            "ERROR: working tree has tracked changes outside the corpus:\n  " + "\n  ".join(unexpected),
            file=sys.stderr,
        )
        return 2

    # GENERATOR trigger uses git-history (committed renderer change vs base), NOT the
    # on-disk manifest: the trigger must survive a local `regenerate` (which rewrites
    # the manifest and erases `_generator_out_of_sync`) and must not be fakeable by a
    # missing local manifest (#502 review R2).
    generator_ship = not csv_uncommitted and _changed_vs_base(PHP_GENERATOR_REL)

    if not csv_uncommitted and not generator_ship:
        print(
            "ERROR: nothing to ship — no uncommitted change to "
            f"{CORPUS_REL} and no committed change to {PHP_GENERATOR_REL} "
            f"vs {SHIP_BASE_BRANCH}.",
            file=sys.stderr,
        )
        return 2

    if not IMAGES_DIR.exists():
        print(
            f"ERROR: {IMAGES_DIR} missing — populate it first via scripts/download_images.sh.",
            file=sys.stderr,
        )
        return 2

    if generator_ship:
        return _cmd_ship_generator(args)
    return _cmd_ship_csv(args)


def _cmd_ship_csv(args: argparse.Namespace) -> int:
    head_rows, work_rows = _parse_sides()
    changed = diff_changed_rows(head_rows, work_rows)

    # Bucket contiguity is unsafe to skip — non-contiguous rows silently
    # produce filename collisions that overwrite previous rows' PNGs at
    # generation time. Always enforce, regardless of --skip-validate.
    contiguity_errors = validate_bucket_contiguity(work_rows)
    if contiguity_errors:
        print(
            "ERROR: non-contiguous bucket(s) — would corrupt filenames at generation time:",
            file=sys.stderr,
        )
        for line in contiguity_errors:
            print(line, file=sys.stderr)
        print(
            "       Sort the CSV by HH:MM (rows in the same bucket must be adjacent).",
            file=sys.stderr,
        )
        return 2

    if args.skip_validate:
        print("WARN: --skip-validate passed; time-tag validation disabled.", file=sys.stderr)
    else:
        val_errors = validate_rows(changed)
        if val_errors:
            print("ERROR: changed rows fail time-tag validation:", file=sys.stderr)
            for line in val_errors:
                print(line, file=sys.stderr)
            print("       Re-run with --skip-validate to override (rare — prefer fixing the row).", file=sys.stderr)
            return 2

    dirty = compute_dirty_buckets(head_rows, work_rows)
    if not dirty:
        print("ERROR: CSV changed but no buckets are dirty — nothing to ship.", file=sys.stderr)
        return 2

    # If the renderer ALSO changed (mixed edit), generator drift dominates and forces
    # a full regen inside _prepare_for_regen (#502 review R1) — the dirty subset alone
    # would leave unchanged buckets on the old renderer. Detect it via git-diff-vs-base
    # OR the on-disk manifest: the committed-renderer signal survives a local regen
    # that already refreshed the manifest (#503 review R2 seam).
    manifest_drift = _manifest_out_of_sync()
    generator_drift = _changed_vs_base(PHP_GENERATOR_REL) or _generator_out_of_sync()

    print(f"Changed rows: {len(changed)}   dirty buckets: {sorted(dirty)}")
    if args.dry_run:
        _prepare_for_regen(dirty, manifest_drift, generator_drift, dry_run=True)
        current = read_version()
        print(f"[dry-run] would bump {current} -> {next_version(current)}")
        print(f"[dry-run] would branch {_derive_branch(args)}, commit, release, push, open PR.")
        return 0

    # Branch off master (or continue an existing retry on the derived branch).
    branch = _derive_branch(args)
    current = current_branch()
    if current == "master":
        run(["git", "checkout", "-b", branch], cwd=REPO_ROOT)
    elif current != branch:
        print(
            f"ERROR: current branch {current!r} is neither master nor {branch!r}; "
            "check out master or the target branch before shipping.",
            file=sys.stderr,
        )
        return 2

    _prepare_for_regen(dirty, manifest_drift, generator_drift, dry_run=False)
    _run_generator()

    current_version, new_version = _bump_version()
    pr_body = _build_pr_body(changed, sorted(dirty), current_version, new_version, args.message)
    return _finalize_ship(branch, [CORPUS_REL, ".images-version"], new_version, pr_body, args)


def _cmd_ship_generator(args: argparse.Namespace) -> int:
    # In-place on the feature branch holding the committed renderer change. Never
    # master/detached — we don't commit a version bump straight to master, and a
    # detached HEAD can't be pushed as a branch (#502 review R3).
    branch = current_branch()
    if branch in ("master", "HEAD"):
        print(
            "ERROR: generator ship must run on the feature branch that holds your "
            f"committed {PHP_GENERATOR_REL} change (not master or a detached HEAD). "
            "Check out that branch and rerun.",
            file=sys.stderr,
        )
        return 2

    # Rerun-safety: if .images-version was already bumped on this branch, a prior
    # ship already cut the release — re-running would double-bump vN+1 -> vN+2 and
    # cut a second tag. Finish the prior ship with the recovery steps instead
    # (#503 review P1). The initial ship sees no bump vs base and proceeds.
    if _changed_vs_base(IMAGES_VERSION_REL):
        print(
            f"ERROR: {IMAGES_VERSION_REL} is already bumped on this branch vs "
            f"{SHIP_BASE_BRANCH} — a prior ship likely already cut the release. "
            "Finish it with the recovery steps (release_images.sh / git push / "
            "gh pr create), don't re-run ship (it would double-bump).",
            file=sys.stderr,
        )
        return 2

    # A mixed renderer + committed-CSV branch is not supported by the in-place
    # generator flow (it skips CSV validation + dirty-bucket orphan clearing). Fail
    # loud rather than ship stale/unvalidated rows (#503 review P1).
    if _changed_vs_base(CORPUS_REL):
        print(
            f"ERROR: this branch also has committed changes to {CORPUS_REL}. A mixed "
            "renderer+corpus ship isn't supported — ship the corpus edit on its own "
            "branch first, then rebase this renderer change onto master.",
            file=sys.stderr,
        )
        return 2

    print(f"Generator ship (renderer changed vs {SHIP_BASE_BRANCH}); branch {branch!r} — full regen.")
    if args.dry_run:
        # Force generator_drift=True: the release always regenerates from scratch for
        # determinism, regardless of whatever the local manifest currently records.
        _prepare_for_regen([], _manifest_out_of_sync(), True, dry_run=True)
        current = read_version()
        print(f"[dry-run] would bump {current} -> {next_version(current)}")
        print(f"[dry-run] would commit .images-version on {branch}, release, push, open PR.")
        return 0

    _prepare_for_regen([], _manifest_out_of_sync(), True, dry_run=False)
    _run_generator()

    current_version, new_version = _bump_version()
    pr_body = _build_pr_body([], [], current_version, new_version, args.message)
    return _finalize_ship(branch, [".images-version"], new_version, pr_body, args)


def _bump_version() -> tuple[str, str]:
    current_version = read_version()
    new_version = next_version(current_version)
    write_version(new_version, dry_run=False)
    print(f"Bumped .images-version: {current_version} -> {new_version}")
    return current_version, new_version


def _finalize_ship(
    branch: str, commit_paths: list[str], new_version: str, pr_body: str, args: argparse.Namespace
) -> int:
    """Commit the tracked ship files, then release + push + open the PR. Shared by
    both ship modes. Any post-commit failure prints a step-aware recovery script."""
    run(["git", "add", *commit_paths], cwd=REPO_ROOT)
    run(["git", "commit", "-m", args.message], cwd=REPO_ROOT)

    release_done = False
    try:
        if not args.no_release:
            if not RELEASE_SCRIPT.exists():
                raise SystemExit(f"ERROR: {RELEASE_SCRIPT} missing")
            run([str(RELEASE_SCRIPT), new_version], cwd=REPO_ROOT)
            release_done = True
        if not args.no_push:
            run(["git", "push", "-u", "origin", branch], cwd=REPO_ROOT)
            run(["gh", "pr", "create", "--title", args.message, "--body", pr_body], cwd=REPO_ROOT)
    except (subprocess.CalledProcessError, SystemExit) as exc:
        _print_recovery(branch, new_version, args, exc, release_done=release_done)
        raise

    print("Done.")
    return 0


def _print_recovery(
    branch: str, new_version: str, args: argparse.Namespace, exc: BaseException, *, release_done: bool
) -> None:
    """Print the exact commands to finish a ship that failed mid-flight.

    By this point the tracked ship files are already committed on `branch`. The user
    just needs to re-run whatever post-commit step blew up. `release_done` makes the
    hint step-aware: once the release_images.sh step has cut the tag, do NOT suggest
    re-running it — the tag already exists and a second run would error (#502 review #8).
    """
    print(f"\nERROR: ship failed after commit was made: {exc}", file=sys.stderr)
    print("The local commit is intact. To finish manually:", file=sys.stderr)
    if not args.no_release and not release_done:
        print(f"  scripts/release_images.sh {new_version}", file=sys.stderr)
    if not args.no_push:
        print(f"  git push -u origin {branch}", file=sys.stderr)
        print(f"  gh pr create --title {args.message!r} --body '…'", file=sys.stderr)


def _derive_branch(args: argparse.Namespace) -> str:
    if args.branch:
        return args.branch
    sha_result = run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, capture=True)
    # Timestamp suffix avoids `git checkout -b` collisions when ship retries on the same HEAD.
    return f"fix/corpus-edit-{sha_result.stdout.strip()}-{int(time.time())}"


def _build_pr_body(
    changed: list[Row],
    dirty: list[str],
    old_version: str,
    new_version: str,
    message: str,
) -> str:
    lines = [f"## {message}", ""]
    if changed or dirty:
        lines += [
            f"- Changed rows: {len(changed)}",
            f"- Dirty buckets: {', '.join(f'{k[:2]}:{k[2:]}' for k in dirty)}",
        ]
    else:
        # Generator ship — the renderer changed, not the corpus, so a full regen ran.
        lines.append("- Renderer change (`quote_to_image.php`) — full corpus regenerated, no CSV rows changed.")
    lines += [
        f"- Images version: {old_version} -> {new_version}",
        "",
        "Generated by `image-gen/corpus_edit.py ship` (issue #211).",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("validate", help="Check changed rows' time-tag parses correctly.")
    sub.add_parser("diff", help="List dirty HH:MM buckets vs HEAD.")

    p_regen = sub.add_parser("regenerate", help="Wipe dirty buckets, then run quote_to_image.php.")
    p_regen.add_argument("--dry-run", action="store_true")

    p_ship = sub.add_parser("ship", help="Validate, regenerate, bump, commit, release, push, PR.")
    p_ship.add_argument("message", help="Commit message / PR title.")
    p_ship.add_argument("--dry-run", action="store_true")
    p_ship.add_argument("--no-release", action="store_true", help="Skip scripts/release_images.sh.")
    p_ship.add_argument("--no-push", action="store_true", help="Skip git push and gh pr create.")
    p_ship.add_argument("--branch", default=None, help="Override auto-derived branch name.")
    p_ship.add_argument(
        "--skip-validate",
        action="store_true",
        help="Bypass the time-tag validator (rare — prefer fixing the row).",
    )

    args = parser.parse_args(argv)

    handlers = {
        "validate": cmd_validate,
        "diff": cmd_diff,
        "regenerate": cmd_regenerate,
        "ship": cmd_ship,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
