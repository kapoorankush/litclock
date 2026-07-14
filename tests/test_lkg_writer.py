"""Tests for scripts/litclock-lkg-record.sh (issues #209, #241).

The LKG writer is a fast oneshot driven by litclock-lkg.timer (#241).
It records HEAD to /var/lib/litclock/lkg-sha when three gates pass:

  1. Post-update grace marker is absent or older than GRACE_SECONDS.
  2. Render heartbeat (/run/litclock/heartbeat) is fresher than
     HEARTBEAT_MAX_AGE_SECONDS — proves the clock is actually rendering.
  3. lkg-sha != HEAD (idempotency — skip when already recorded).

The bootcheck/revert path that consumes this marker is deferred (its own
issue). Today the writer is observability-only.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "litclock-lkg-record.sh"


def _make_sandbox(tmp_path: Path, *, with_git: bool = True) -> Path:
    """Build a minimal INSTALL_DIR the script can operate against."""
    install = tmp_path / "install"
    install.mkdir()
    # The script sources scripts/lib/state.sh relative to its own location.
    # Symlink the repo's scripts/ in so the relative resolution works without
    # copying the whole tree.
    (install / "scripts").symlink_to(REPO_ROOT / "scripts")
    if with_git:
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
        subprocess.run(["git", "init", "-q", "-b", "master"], cwd=install, env=env, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=install, env=env, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=install, env=env, check=True)
        (install / "README").write_text("hello\n")
        subprocess.run(["git", "add", "README"], cwd=install, env=env, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=install, env=env, check=True)
    return install


def _head_sha(install: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=install,
        env={"PATH": "/usr/bin:/bin", "HOME": str(install.parent)},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _run(
    *,
    install: Path,
    state_dir: Path,
    heartbeat_file: Path | None = None,
    grace_seconds: int = 900,
    heartbeat_max_age: int = 180,
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(install.parent),
        "LITCLOCK_DIR": str(install),
        "LITCLOCK_STATE_DIR": str(state_dir),
        "LITCLOCK_LKG_GRACE_SECONDS": str(grace_seconds),
        "LITCLOCK_LKG_HEARTBEAT_MAX_AGE_SECONDS": str(heartbeat_max_age),
    }
    if heartbeat_file is not None:
        env["LITCLOCK_HEARTBEAT_FILE"] = str(heartbeat_file)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _touch(path: Path, *, age_seconds: int = 0) -> None:
    """Create file and backdate its mtime by age_seconds."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    if age_seconds:
        now = int(__import__("time").time())
        os.utime(path, (now - age_seconds, now - age_seconds))


# ── State machine: the three gates × pass/fail ───────────────────────


class TestStateMachine:
    """Exhaustively cover the gate matrix. The interesting transitions
    are when ALL three gates pass (then we write) and any one fails
    (then we skip)."""

    def _setup(self, tmp_path):
        install = _make_sandbox(tmp_path)
        state = tmp_path / "state"
        state.mkdir()
        heartbeat = tmp_path / "run" / "litclock" / "heartbeat"
        heartbeat.parent.mkdir(parents=True)
        return install, state, heartbeat

    def test_all_gates_pass_writes_lkg(self, tmp_path):
        install, state, heartbeat = self._setup(tmp_path)
        _touch(heartbeat, age_seconds=10)  # fresh

        r = _run(install=install, state_dir=state, heartbeat_file=heartbeat)

        assert r.returncode == 0, r.stderr
        marker = state / "lkg-sha"
        assert marker.exists(), f"expected write; stderr: {r.stderr}"
        assert marker.read_text().strip() == _head_sha(install)

    def test_grace_marker_fresh_skips_write(self, tmp_path):
        install, state, heartbeat = self._setup(tmp_path)
        _touch(heartbeat, age_seconds=10)
        _touch(state / "post-update-grace-until", age_seconds=60)  # well inside 900s

        r = _run(install=install, state_dir=state, heartbeat_file=heartbeat)

        assert r.returncode == 0
        assert not (state / "lkg-sha").exists(), "grace gate must block write"
        assert "grace" in r.stdout.lower() or "grace" in r.stderr.lower()

    def test_grace_marker_expired_allows_write(self, tmp_path):
        install, state, heartbeat = self._setup(tmp_path)
        _touch(heartbeat, age_seconds=10)
        _touch(state / "post-update-grace-until", age_seconds=1000)  # past 900s

        r = _run(install=install, state_dir=state, heartbeat_file=heartbeat)

        assert r.returncode == 0
        assert (state / "lkg-sha").exists(), "expired grace must NOT block"

    def test_grace_marker_absent_allows_write(self, tmp_path):
        install, state, heartbeat = self._setup(tmp_path)
        _touch(heartbeat, age_seconds=10)

        r = _run(install=install, state_dir=state, heartbeat_file=heartbeat)

        assert r.returncode == 0
        assert (state / "lkg-sha").exists()

    def test_heartbeat_missing_skips_write(self, tmp_path):
        install, state, heartbeat = self._setup(tmp_path)
        # Don't create heartbeat — simulates a clock that never rendered.

        r = _run(install=install, state_dir=state, heartbeat_file=heartbeat)

        assert r.returncode == 0
        assert not (state / "lkg-sha").exists(), "missing heartbeat must block"
        assert "heartbeat" in r.stdout.lower() or "heartbeat" in r.stderr.lower()

    def test_heartbeat_stale_skips_write(self, tmp_path):
        install, state, heartbeat = self._setup(tmp_path)
        _touch(heartbeat, age_seconds=600)  # >>180s

        r = _run(install=install, state_dir=state, heartbeat_file=heartbeat)

        assert r.returncode == 0
        assert not (state / "lkg-sha").exists(), "stale heartbeat must block"

    def test_idempotent_when_lkg_already_matches_head(self, tmp_path):
        install, state, heartbeat = self._setup(tmp_path)
        _touch(heartbeat, age_seconds=10)
        # Pre-seed lkg-sha = HEAD. Script should noop without rewriting.
        marker = state / "lkg-sha"
        marker.write_text(_head_sha(install) + "\n")
        before_mtime = marker.stat().st_mtime
        # Backdate so any rewrite would change mtime.
        os.utime(marker, (before_mtime - 100, before_mtime - 100))
        before_mtime = marker.stat().st_mtime

        r = _run(install=install, state_dir=state, heartbeat_file=heartbeat)

        assert r.returncode == 0
        assert marker.stat().st_mtime == before_mtime, "idempotent: must not rewrite when SHA matches"

    def test_writes_when_lkg_differs_from_head(self, tmp_path):
        install, state, heartbeat = self._setup(tmp_path)
        _touch(heartbeat, age_seconds=10)
        marker = state / "lkg-sha"
        marker.write_text("0" * 40 + "\n")  # stale SHA

        r = _run(install=install, state_dir=state, heartbeat_file=heartbeat)

        assert r.returncode == 0
        assert marker.read_text().strip() == _head_sha(install)


# ── Graceful degradation ─────────────────────────────────────────────


class TestGracefulDegradation:
    def test_no_git_repo_exits_cleanly(self, tmp_path):
        install = _make_sandbox(tmp_path, with_git=False)
        state = tmp_path / "state"
        state.mkdir()
        heartbeat = tmp_path / "hb"
        _touch(heartbeat, age_seconds=10)

        r = _run(install=install, state_dir=state, heartbeat_file=heartbeat)

        assert r.returncode == 0
        assert not (state / "lkg-sha").exists()
        assert "not a git repo" in r.stderr.lower() or "warn" in r.stderr.lower()

    def test_state_dir_created_if_missing(self, tmp_path):
        install = _make_sandbox(tmp_path)
        state = tmp_path / "state-does-not-exist-yet"
        heartbeat = tmp_path / "hb"
        _touch(heartbeat, age_seconds=10)

        r = _run(install=install, state_dir=state, heartbeat_file=heartbeat)

        assert r.returncode == 0
        assert state.exists(), "script must create STATE_DIR when missing"
        assert (state / "lkg-sha").exists()


# ── Atomic write ─────────────────────────────────────────────────────


class TestAtomicWrite:
    def test_write_uses_atomic_helper(self, tmp_path):
        """The recorded SHA replaces any prior content cleanly. We can't
        observe a half-write directly without racing — so we settle for:
        starting from arbitrary stale content, the final state is the
        new SHA exactly (proving rename semantics worked)."""
        install = _make_sandbox(tmp_path)
        state = tmp_path / "state"
        state.mkdir()
        heartbeat = tmp_path / "hb"
        _touch(heartbeat, age_seconds=10)
        marker = state / "lkg-sha"
        marker.write_text("old" * 14 + "\n")

        r = _run(install=install, state_dir=state, heartbeat_file=heartbeat)

        assert r.returncode == 0
        assert marker.read_text().strip() == _head_sha(install)

    def test_no_tmp_file_left_on_success(self, tmp_path):
        install = _make_sandbox(tmp_path)
        state = tmp_path / "state"
        state.mkdir()
        heartbeat = tmp_path / "hb"
        _touch(heartbeat, age_seconds=10)

        r = _run(install=install, state_dir=state, heartbeat_file=heartbeat)
        assert r.returncode == 0
        leftovers = [p.name for p in state.iterdir() if ".tmp" in p.name or ".XXXXXX" in p.name]
        assert not leftovers, f"tmp file left behind: {leftovers}"


# ── Structural invariants — guardrails against future regressions ───


class TestStructural:
    """Grep invariants pinning the post-#241 design decisions."""

    def _body(self):
        return SCRIPT.read_text()

    def test_no_sleep_in_script(self):
        """Issue #241 — `sleep` was the source of the heartbeat-window
        race. The polling design must have NO sleep at all."""
        body = self._body()
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert not stripped.startswith("sleep "), f"sleep found in non-comment line: {line!r}"

    def test_no_systemctl_is_active_check(self):
        """Issue #241 — `systemctl is-active` was the buggy gate. The
        replacement gate is the heartbeat mtime check."""
        body = self._body()
        assert "systemctl is-active" not in body, "is-active gate was the bug; do not reintroduce"

    def test_sources_lib_state(self):
        """The script must use the shared atomic helpers, not its own
        copy of the rename logic (D3)."""
        body = self._body()
        assert "lib/state.sh" in body, "must source scripts/lib/state.sh"
        assert "atomic_write_file" in body, "must call shared atomic helper"

    def test_no_bootcheck_logic_shipped(self):
        """v1 is writer-only. The bootcheck/revert follow-up ships as its
        own PR."""
        body = self._body()
        assert "git reset --hard" not in body, "bootcheck/revert is a separate issue"
        assert "reboot" not in body, "bootcheck/reboot is a separate issue"

    def test_three_gates_present(self):
        """The state machine has exactly three gates. Pin them so a refactor
        can't silently collapse one."""
        body = self._body()
        # Each gate has a distinctive variable or path reference.
        assert "post-update-grace-until" in body, "GATE 1 (grace marker) missing"
        assert "HEARTBEAT_FILE" in body or "/run/litclock/heartbeat" in body, "GATE 2 (heartbeat) missing"
        assert "rev-parse HEAD" in body, "GATE 3 (HEAD compare) missing"
