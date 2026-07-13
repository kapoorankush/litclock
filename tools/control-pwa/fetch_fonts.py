#!/usr/bin/env python3
"""Fetch self-hosted variable woff2 fonts for the LitClock Control PWA.

Pinned versions + SHA256 verification, idempotent.

Per locked decisions D5 + F1 + F4:

- Fraunces (variable, wght axis) — DESIGN.md serif. Italic axis lives in a
  separate woff2 file in Fontsource's package layout, so we ship both
  Fraunces-normal and Fraunces-italic per F4.
- Instrument Sans (variable, wght axis) — DESIGN.md sans-serif body face.
- Geist Mono (variable, wght axis) — DESIGN.md monospace face for
  Version / SSID / Uptime tabular display.

All fonts are SIL OFL — see NOTICE.md for license attribution (F1).

Output: src/control_server/static/fonts/ (4 files, ~150-300KB total).

Usage:
    python3 tools/control-pwa/fetch_fonts.py            # download
    python3 tools/control-pwa/fetch_fonts.py --check    # exit nonzero if any
                                                          # SHA256 mismatches

The fonts ship checked into the repo. CI runs --check to verify the on-disk
files still match the manifest. To upgrade a pinned version: bump the version
+ run without --check, then commit the resulting woff2 alongside an updated
SHA256 in this script.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = REPO_ROOT / "src" / "control_server" / "static" / "fonts"

# Pinned manifest — (filename, source URL, SHA256). Bump versions deliberately;
# CI's --check pass guards against accidental drift.
FONTS: tuple[tuple[str, str, str], ...] = (
    (
        "fraunces-wght-normal.woff2",
        "https://cdn.jsdelivr.net/npm/@fontsource-variable/fraunces@5.2.9/files/fraunces-latin-wght-normal.woff2",
        "7f9d191d999336d3b9790afa72e1358e50a13b06d4f289341e92a311967a80f9",
    ),
    (
        "fraunces-wght-italic.woff2",
        "https://cdn.jsdelivr.net/npm/@fontsource-variable/fraunces@5.2.9/files/fraunces-latin-wght-italic.woff2",
        "bceec2ef4d549efbc8df0194a8d5280b6a64c3e399244dffccd9ea1bd9ad6db7",
    ),
    (
        "instrument-sans-wght-normal.woff2",
        "https://cdn.jsdelivr.net/npm/@fontsource-variable/instrument-sans@5.2.8/files/instrument-sans-latin-wght-normal.woff2",
        "2ee17598a98d8a59e4df8152d015bec9ab8e4d5672cc0ab42bef806b568e3971",
    ),
    (
        "geist-mono-wght-normal.woff2",
        "https://cdn.jsdelivr.net/npm/@fontsource-variable/geist-mono@5.2.7/files/geist-mono-latin-wght-normal.woff2",
        "e9fb088eeacced307860d82ceedb0ae9ad2ebfa07a7c0a7279c8c961dd9d5fd3",
    ),
)


def _sha256(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "litclock-fetch-fonts/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — pinned hosts
        return resp.read()


def fetch(check_only: bool = False) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    drift = 0
    for filename, url, expected_sha in FONTS:
        dest = OUT_DIR / filename

        if check_only:
            if not dest.exists():
                print(f"MISSING: {dest.relative_to(REPO_ROOT)}")
                drift += 1
                continue
            actual_sha = _sha256(dest.read_bytes())
            if expected_sha and actual_sha != expected_sha:
                print(f"DRIFT:   {dest.relative_to(REPO_ROOT)}")
                print(f"         expected {expected_sha}")
                print(f"         got      {actual_sha}")
                drift += 1
            else:
                print(f"OK:      {dest.relative_to(REPO_ROOT)} (sha256 {actual_sha[:12]}…)")
            continue

        # Download mode — fetch, verify if pinned, write.
        print(f"fetch    {url}")
        try:
            blob = _download(url)
        except Exception as exc:  # network errors should be loud, not silent
            print(f"ERROR:   {filename}: {exc}", file=sys.stderr)
            return 2

        actual_sha = _sha256(blob)
        if expected_sha and actual_sha != expected_sha:
            print(
                f"ERROR:   {filename} SHA256 mismatch — expected {expected_sha}, got {actual_sha}",
                file=sys.stderr,
            )
            return 2

        dest.write_bytes(blob)
        print(f"wrote    {dest.relative_to(REPO_ROOT)} ({len(blob):,} bytes, sha256 {actual_sha[:12]}…)")
        if not expected_sha:
            print(
                f"NOTE:    no SHA256 pin yet for {filename}. Add to FONTS manifest:\n         {actual_sha}",
                file=sys.stderr,
            )

    if check_only and drift:
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit nonzero if any font file is missing or its SHA256 drifts from the manifest. No downloads.",
    )
    args = parser.parse_args()
    return fetch(check_only=args.check)


if __name__ == "__main__":
    sys.exit(main())
