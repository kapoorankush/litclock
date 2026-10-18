"""litclock-dev#82 PII sweep guard: no tracked file names the maintainer's location.

The litclock-dev#82 sweep replaced every reference to the maintainer's city and zip with
Austin, TX (30.27, -97.74), the project's placeholder location. Nothing
guarded it, and litclock-dev#929 put the city back into three test fixtures. This test is
the guard. It matches the city and the zip only. Coordinates are deliberately
NOT guarded, although leaks of bare coordinates have happened: matching them
here would mean spelling them out, which publishes the very thing the guard
exists to keep out. A coordinate-only fixture still needs a reviewer's eye.

Scope is every TRACKED file except:
- files that are tracked but ignored (maintainer-local docs such as DESIGN.md),
  which never leave the maintainer's checkout;
- CHANGELOG.md, which records the sweep itself; and
- every ``.csv``: the quote corpus is literature, where the city's name is also
  ordinary slang for San Francisco, and machine-written ``stats/*.csv`` files
  must never be able to turn master red with nobody having changed anything.

The patterns are assembled from pieces so this file does not match itself.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

PATTERNS = [
    re.compile("Fris" + "co"),
    re.compile("750" + "33"),
]


def _git(*args: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=True).stdout
    return [line for line in out.split("\0") if line]


def _portable_files() -> list[Path]:
    tracked = set(_git("ls-files", "-z"))
    ignored = set(_git("ls-files", "-z", "-ci", "--exclude-standard"))
    return [REPO / f for f in sorted(tracked - ignored - {"CHANGELOG.md"}) if not f.endswith(".csv")]


def test_the_scan_covers_the_tree():
    """A scan of nothing passes for the wrong reason."""
    files = _portable_files()
    assert len(files) > 400  # 482 on 2026-10-16
    assert REPO / "tests" / "test_reset_setup_sh.py" in files
    assert REPO / "DESIGN.md" not in files
    assert all(f.suffix != ".csv" for f in files)


def test_no_portable_file_names_the_maintainers_location():
    hits = []
    for path in _portable_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue  # binary, or deleted in the working tree
        for pattern in PATTERNS:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                hits.append(f"{path.relative_to(REPO)}:{line}: {pattern.pattern}")
    assert not hits, "the maintainer's location is back (use Austin, TX 30.27,-97.74):\n" + "\n".join(hits)
