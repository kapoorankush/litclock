#!/usr/bin/env python3
"""Fail if CHANGELOG.md has no section for the release tag being cut.

litclock-dev#681. The PWA's update card does not read the GitHub release body.
``control_server.update_state.fetch_release_notes`` fetches ``CHANGELOG.md`` at
the tag and matches a heading for that exact tag; when no heading matches it
returns None and ``updates.html.j2`` hides the notes block entirely. So a
release tagged while its content is still under ``## [Unreleased]`` ships an
update card with no notes at all -- which is what happened to v0.223.0 and
v0.224.0, silently, twice.

THIS SCRIPT CALLS THE REAL EXTRACTOR. It deliberately does not reimplement the
heading regex: a second copy would drift from the one the device actually runs,
and then the gate would pass on releases the PWA still renders blank. Importing
the production function is what makes "CI is green" and "the card shows notes"
the same statement.

Usage:
    check-changelog-section.py v0.225.0 [--changelog CHANGELOG.md]

Exit codes:
    0  a section for the tag exists and is non-empty
    1  no section (or an empty one) -- the release would ship blank notes
    2  bad usage / unreadable CHANGELOG
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Release-shaped tags only, matching scripts/lib/github_api.sh's own filter.
# The workflow triggers on `v*`, which also catches RC and QA tags that never
# become an offered release -- gating those would block a QA build for a
# CHANGELOG section it has no reason to carry (/review).
_RELEASE_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")


def _load_extractor():
    """Load the PRODUCTION extractor WITHOUT importing the package.

    `from control_server.update_state import ...` executes
    src/control_server/__init__.py, which does `from flask import Flask`. The
    workflow step that runs this installs only pytest and pyyaml, so that import
    raised ModuleNotFoundError and the gate failed EVERY tag build -- with exit
    1, the same code as "no CHANGELOG section", so it would have read as the
    very thing it was written to detect (/review; my own check for this passed
    only because the dev box has flask installed).

    update_state.py imports nothing but stdlib and has no relative imports, so
    loading it directly by path is safe and keeps the point of the gate: it runs
    the same function the device runs, not a second copy of the regex.
    """
    path = REPO_ROOT / "src" / "control_server" / "update_state.py"
    spec = importlib.util.spec_from_file_location("_litclock_update_state", path)
    if spec is None or spec.loader is None:  # pragma: no cover - unreachable in-tree
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._extract_changelog_section


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tag", help="the release tag being cut, e.g. v0.225.0")
    parser.add_argument(
        "--changelog",
        default=str(REPO_ROOT / "CHANGELOG.md"),
        help="path to CHANGELOG.md (default: repo root)",
    )
    args = parser.parse_args(argv)

    if not _RELEASE_TAG_RE.match(args.tag):
        # Not a release the updater will ever offer, so it has no CHANGELOG
        # section to carry. Skipping is the point, not a loophole: the tag
        # shape is checked, and anything vMAJOR.MINOR.PATCH still gates.
        print(f"{args.tag} is not a release-shaped tag (vMAJOR.MINOR.PATCH) — no CHANGELOG section required")
        return 0

    path = Path(args.changelog)
    try:
        body = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: could not read {path}: {exc}", file=sys.stderr)
        return 2

    extract = _load_extractor()
    section = extract(body, args.tag)
    # A bare `### Changed` with nothing under it is non-empty, so it satisfies
    # the extractor and renders as a category heading and nothing else on the
    # card (/review). Require something that actually says what changed.
    bullets = [ln for ln in (section or "").splitlines() if ln.lstrip().startswith(("-", "*"))]
    if section and bullets:
        line_count = len(section.splitlines())
        print(f"CHANGELOG section for {args.tag}: OK ({line_count} lines will render on the update card)")
        return 0
    if section and not bullets:
        print(
            f"error: the CHANGELOG section for {args.tag} has headings but no entries.\n"
            f"The update card would render category headings and nothing else:\n\n"
            + "\n".join(f"    {ln}" for ln in section.splitlines()),
            file=sys.stderr,
        )
        return 1

    # Show what IS there, so the fix is obvious from the CI log alone.
    headings = [ln for ln in body.splitlines() if ln.startswith("## ")][:5]
    print(
        f"error: CHANGELOG.md has no section for {args.tag}.\n"
        f"\n"
        f"The update card in the PWA reads CHANGELOG.md at the tag, not the GitHub\n"
        f"release body. With no matching heading it renders NO release notes at all\n"
        f"-- the owner sees an update prompt with nothing explaining it.\n"
        f"\n"
        f"Fix: promote the [Unreleased] heading to the version being released.\n"
        f"\n"
        f"    ## [Unreleased]        ->   ## [{args.tag}] - YYYY-MM-DD\n"
        f"\n"
        f"then re-tag before pushing. Current headings:\n" + "\n".join(f"    {h}" for h in headings),
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
