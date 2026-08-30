#!/usr/bin/env python3
"""Refresh tests/fixtures/disable_ssh_for_handoff.golden (litclock-dev#708).

Run from the repo root, IN THE SAME COMMIT as a deliberate change to
disable_ssh_for_handoff in scripts/reset-setup.sh — and remember the same
change is owed to the other repo (litclock-dev#657). Uses the exact anchors
the parity test uses; the test's shape assertions backstop a truncated cut.
"""

from pathlib import Path

c = Path("scripts/reset-setup.sh").read_text()
d = c.index("disable_ssh_for_handoff() {")
h = c.rindex("\n\n", 0, d) + 2
e = c.index("\n}\n", d) + len("\n}\n")
Path("tests/fixtures/disable_ssh_for_handoff.golden").write_text(c[h:e])
print(f"golden refreshed: {e - h} bytes")
