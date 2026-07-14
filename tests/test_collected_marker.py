"""Tests for the #445 persistent collected-marker writers.

The marker (``/var/lib/litclock/.last-collected-marker.json``) has TWO writers
in two languages by design:

  * ``src/collected_marker.py`` — Python, used by the IP-geo resolvers
    (``location_resolver`` + ``setup_server``) running as ``pi``.
  * ``scripts/litclock-mark-collected.sh`` — ``/bin/sh``, used by the NM
    dispatcher running as ``root`` (it must not depend on the venv).

This file pins each writer's behavior AND their cross-language parity: they
share one file + sidecar lock + JSON format, so writing one key with each
must yield a single marker carrying both. That parity test is the guard
against the two implementations drifting (the same cross-language flock
precedent as the env.sh writers, #274).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import collected_marker  # noqa: E402

SHELL_WRITER = REPO_ROOT / "scripts" / "litclock-mark-collected.sh"
HAS_JQ = shutil.which("jq") is not None
_shell_available = pytest.mark.skipif(
    not (SHELL_WRITER.exists() and HAS_JQ),
    reason="shell writer or jq unavailable",
)


def _read(path) -> dict:
    return json.loads(Path(path).read_text())


def _run_shell(key, marker):
    env = {**os.environ, "LITCLOCK_COLLECTED_MARKER": str(marker)}
    return subprocess.run([str(SHELL_WRITER), key], env=env, capture_output=True, text=True)


class TestPythonWriter:
    def test_writes_section_key_with_iso_timestamp(self, tmp_path):
        marker = tmp_path / ".last-collected-marker.json"
        assert collected_marker.mark_collected("network", str(marker)) is True
        data = _read(marker)
        assert "network" in data
        # ISO-8601 UTC, seconds precision (matches the shell writer's `date`).
        assert data["network"].endswith("+00:00")
        assert datetime.fromisoformat(data["network"]).tzinfo is not None

    def test_read_modify_write_preserves_other_key(self, tmp_path):
        marker = tmp_path / ".last-collected-marker.json"
        collected_marker.mark_collected("network", str(marker))
        collected_marker.mark_collected("time-location", str(marker))
        assert set(_read(marker)) == {"network", "time-location"}

    def test_invalid_section_is_noop(self, tmp_path):
        marker = tmp_path / ".last-collected-marker.json"
        assert collected_marker.mark_collected("bogus", str(marker)) is False
        assert not marker.exists()

    def test_missing_dir_returns_false(self, tmp_path):
        marker = tmp_path / "nope" / ".last-collected-marker.json"
        assert collected_marker.mark_collected("network", str(marker)) is False

    def test_malformed_existing_self_heals(self, tmp_path):
        marker = tmp_path / ".last-collected-marker.json"
        marker.write_text("{garbage not json")
        assert collected_marker.mark_collected("network", str(marker)) is True
        assert set(_read(marker)) == {"network"}

    def test_marker_is_world_readable(self, tmp_path):
        # control_server (pi) must be able to read a marker even when the NM
        # dispatcher (root) wrote it — so the file is 0644.
        marker = tmp_path / ".last-collected-marker.json"
        collected_marker.mark_collected("network", str(marker))
        assert marker.stat().st_mode & 0o044  # group + other read bits set

    def test_env_var_overrides_path(self, tmp_path, monkeypatch):
        marker = tmp_path / ".last-collected-marker.json"
        monkeypatch.setenv("LITCLOCK_COLLECTED_MARKER", str(marker))
        assert collected_marker.mark_collected("time-location") is True
        assert "time-location" in _read(marker)


@_shell_available
class TestShellWriter:
    def test_writes_key(self, tmp_path):
        marker = tmp_path / ".last-collected-marker.json"
        r = _run_shell("network", marker)
        assert r.returncode == 0, r.stderr
        assert "network" in _read(marker)

    def test_invalid_key_is_noop(self, tmp_path):
        marker = tmp_path / ".last-collected-marker.json"
        r = _run_shell("bogus", marker)
        assert r.returncode == 0
        assert not marker.exists()

    def test_preserves_existing_key(self, tmp_path):
        marker = tmp_path / ".last-collected-marker.json"
        _run_shell("network", marker)
        _run_shell("time-location", marker)
        assert set(_read(marker)) == {"network", "time-location"}

    def test_marker_world_readable(self, tmp_path):
        marker = tmp_path / ".last-collected-marker.json"
        _run_shell("network", marker)
        assert marker.stat().st_mode & 0o044

    def test_symlink_at_marker_is_not_followed(self, tmp_path):
        """#387 C2: the writer runs as root in production, in a pi-writable dir.
        A pi-planted symlink at the marker path must NOT be written through —
        the writer drops the link and creates a fresh regular file, leaving the
        symlink victim untouched."""
        victim = tmp_path / "victim"
        victim.write_text("ORIGINAL\n")
        marker = tmp_path / ".last-collected-marker.json"
        marker.symlink_to(victim)
        r = _run_shell("network", marker)
        assert r.returncode == 0, r.stderr
        # Victim untouched (not overwritten through the link).
        assert victim.read_text() == "ORIGINAL\n"
        # Marker is now a real regular file (link dropped), carrying the key.
        assert not marker.is_symlink()
        assert "network" in _read(marker)

    def test_no_predictable_tmp_file_left_behind(self, tmp_path):
        """The old `$MARKER.tmp.$$` staging path was symlink-guessable; the
        mktemp rewrite must not leave a predictable tmp artifact behind."""
        marker = tmp_path / ".last-collected-marker.json"
        _run_shell("network", marker)
        leftovers = list(tmp_path.glob(".last-collected-marker.json.tmp.*"))
        assert leftovers == [], f"unexpected predictable tmp files: {leftovers}"


@_shell_available
class TestCrossLanguageParity:
    """Writing one key with the shell writer and the other with the Python
    writer must yield a single marker carrying BOTH keys, in the same ISO
    format. This is the drift guard between the two implementations."""

    def test_shell_network_then_python_time_location(self, tmp_path):
        marker = tmp_path / ".last-collected-marker.json"
        assert _run_shell("network", marker).returncode == 0
        assert collected_marker.mark_collected("time-location", str(marker)) is True
        data = _read(marker)
        assert set(data) == {"network", "time-location"}
        for value in data.values():
            assert datetime.fromisoformat(value).tzinfo is not None

    def test_python_network_then_shell_time_location(self, tmp_path):
        marker = tmp_path / ".last-collected-marker.json"
        assert collected_marker.mark_collected("network", str(marker)) is True
        assert _run_shell("time-location", marker).returncode == 0
        assert set(_read(marker)) == {"network", "time-location"}


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
