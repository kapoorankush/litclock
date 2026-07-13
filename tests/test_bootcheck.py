"""Tests for scripts/litclock-bootcheck.sh (LKG auto-revert consumer, #209).

The consumer half of the LKG mechanism. Driven by litclock-bootcheck.timer,
it decides "did the clock paint since this boot?" via the tmpfs heartbeat and,
on a persistent failure, self-heals by routing recovery through update.sh's
complete installer against the recorded last-known-good SHA.

State machine under test:

  update in progress (lock held) ─► defer
  post-update grace fresh          ─► defer
  heartbeat mtime >= boot epoch    ─► HEALTHY: clear counter + recovering
  otherwise                        ─► fail; count += 1
    count < THRESHOLD              ─► reboot to retry
    count >= THRESHOLD
      already recovering           ─► give up (marker + splash)
      no/invalid/absent LKG        ─► give up
      valid LKG                    ─► pin rollback-target + blocked-sha,
                                       mark recovering, clear counter,
                                       trigger litclock-update.service

Privileged actions (reboot / update trigger / splash) are injected via env so
the tests observe them through marker files instead of really rebooting.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "litclock-bootcheck.sh"

_GIT_ENV = {
    "PATH": "/usr/bin:/bin",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(install: Path, *args: str) -> str:
    env = {**_GIT_ENV, "HOME": str(install.parent)}
    return subprocess.run(
        ["git", *args], cwd=install, env=env, capture_output=True, text=True, check=True
    ).stdout.strip()


def _make_sandbox(tmp_path: Path) -> tuple[Path, str, str]:
    """INSTALL_DIR with two commits: returns (install, lkg_sha, head_sha).

    lkg_sha is the FIRST commit (a valid ancestor to revert to); head_sha is
    HEAD (the "current/bad" code).
    """
    install = tmp_path / "install"
    install.mkdir()
    (install / "scripts").symlink_to(REPO_ROOT / "scripts")
    _git(install, "init", "-q", "-b", "master")
    _git(install, "config", "user.email", "t@t")
    _git(install, "config", "user.name", "t")
    (install / "README").write_text("v1\n")
    _git(install, "add", "README")
    _git(install, "commit", "-q", "-m", "one")
    lkg = _git(install, "rev-parse", "HEAD")
    (install / "README").write_text("v2\n")
    _git(install, "add", "README")
    _git(install, "commit", "-q", "-m", "two")
    head = _git(install, "rev-parse", "HEAD")
    return install, lkg, head


def _run(
    *,
    install: Path,
    state_dir: Path,
    heartbeat_file: Path,
    lock_file: Path,
    markers: Path,
    threshold: int = 3,
    boot_epoch: int | None = None,
    grace_seconds: int = 900,
    update_cmd: str | None = None,
    extra_path: str | None = None,
) -> subprocess.CompletedProcess[str]:
    if boot_epoch is None:
        boot_epoch = int(time.time()) - 1000
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(install.parent),
        "LITCLOCK_DIR": str(install),
        "LITCLOCK_STATE_DIR": str(state_dir),
        "LITCLOCK_HEARTBEAT_FILE": str(heartbeat_file),
        "LITCLOCK_UPDATE_LOCK_FILE": str(lock_file),
        "LITCLOCK_LKG_GRACE_SECONDS": str(grace_seconds),
        "LITCLOCK_BOOTCHECK_THRESHOLD": str(threshold),
        "LITCLOCK_BOOT_EPOCH": str(boot_epoch),
        # Observe privileged actions through marker files.
        "LITCLOCK_BOOTCHECK_REBOOT_CMD": f"touch {markers}/rebooted",
        "LITCLOCK_BOOTCHECK_UPDATE_CMD": update_cmd or f"touch {markers}/update-triggered",
        "LITCLOCK_BOOTCHECK_SPLASH_CMD": f"touch {markers}/splash",
    }
    if extra_path:
        env["PATH"] = f"{extra_path}:{env['PATH']}"
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=15, check=False)


def _mark_heartbeat(heartbeat_file: Path, *, mtime: int) -> None:
    heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
    heartbeat_file.touch()
    os.utime(heartbeat_file, (mtime, mtime))


class _Env:
    """Bundle of the paths a run needs, created under tmp_path."""

    def __init__(self, tmp_path: Path):
        self.install, self.lkg, self.head = _make_sandbox(tmp_path)
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.markers = tmp_path / "markers"
        self.markers.mkdir()
        self.heartbeat = tmp_path / "run" / "heartbeat"
        self.lock = self.state / "update.lock"
        self.lock.touch()

    def run(self, **kw):
        return _run(
            install=self.install,
            state_dir=self.state,
            heartbeat_file=self.heartbeat,
            lock_file=self.lock,
            markers=self.markers,
            **kw,
        )

    # convenience accessors
    def count(self) -> str | None:
        f = self.state / "boot-fail-count"
        return f.read_text().strip() if f.exists() else None

    def sf(self, name: str) -> Path:
        return self.state / name

    def did(self, marker: str) -> bool:
        return (self.markers / marker).exists()


# ── HEALTHY ──────────────────────────────────────────────────────────


def test_healthy_clears_counter_no_action(tmp_path):
    e = _Env(tmp_path)
    now = int(time.time())
    _mark_heartbeat(e.heartbeat, mtime=now)  # painted after boot_epoch
    (e.sf("boot-fail-count")).write_text("2")
    r = e.run(boot_epoch=now - 500)
    assert r.returncode == 0
    assert e.count() is None  # cleared
    assert not e.did("rebooted")
    assert not e.did("update-triggered")


def test_healthy_clears_recovering_marker(tmp_path):
    e = _Env(tmp_path)
    now = int(time.time())
    _mark_heartbeat(e.heartbeat, mtime=now)
    e.sf("bootcheck-recovering").write_text("")
    e.run(boot_epoch=now - 500)
    assert not e.sf("bootcheck-recovering").exists()


# ── FAILED BOOT BELOW THRESHOLD → reboot ─────────────────────────────


def test_first_failure_increments_and_reboots(tmp_path):
    e = _Env(tmp_path)
    # no heartbeat at all → failed boot
    r = e.run()
    assert r.returncode == 0
    assert e.count() == "1"
    assert e.did("rebooted")
    assert not e.did("update-triggered")


def test_second_failure_reboots_no_revert(tmp_path):
    e = _Env(tmp_path)
    e.sf("boot-fail-count").write_text("1")
    e.run()
    assert e.count() == "2"
    assert e.did("rebooted")
    assert not e.did("update-triggered")


def test_stale_heartbeat_before_boot_is_a_failure(tmp_path):
    """A heartbeat from a PRIOR boot (mtime < boot epoch) is not health."""
    e = _Env(tmp_path)
    now = int(time.time())
    _mark_heartbeat(e.heartbeat, mtime=now - 5000)  # older than boot_epoch
    e.run(boot_epoch=now - 1000)
    assert e.count() == "1"
    assert e.did("rebooted")


# ── AT THRESHOLD → rollback via update.sh ────────────────────────────


def test_threshold_pins_lkg_and_triggers_update(tmp_path):
    e = _Env(tmp_path)
    e.sf("boot-fail-count").write_text("2")  # this run makes it 3
    e.sf("lkg-sha").write_text(e.lkg + "\n")
    e.run()
    assert e.did("update-triggered")
    assert not e.did("rebooted")  # threshold path hands off, does not reboot
    assert e.sf("rollback-target").read_text().strip() == e.lkg
    assert e.sf("blocked-sha").read_text().strip() == e.head
    assert e.sf("bootcheck-recovering").exists()
    assert e.count() is None  # fresh window for recovered code


def test_threshold_no_lkg_gives_up(tmp_path):
    e = _Env(tmp_path)
    e.sf("boot-fail-count").write_text("2")
    # no lkg-sha file at all
    e.run()
    assert e.sf("bootcheck-gave-up").exists()
    assert e.did("splash")
    assert not e.did("update-triggered")
    assert not e.did("rebooted")


def test_threshold_invalid_lkg_gives_up(tmp_path):
    e = _Env(tmp_path)
    e.sf("boot-fail-count").write_text("2")
    e.sf("lkg-sha").write_text("not-a-sha\n")
    e.run()
    assert e.sf("bootcheck-gave-up").exists()
    assert not e.did("update-triggered")


def test_threshold_lkg_not_in_repo_gives_up(tmp_path):
    e = _Env(tmp_path)
    e.sf("boot-fail-count").write_text("2")
    e.sf("lkg-sha").write_text("0" * 40 + "\n")  # valid shape, absent commit
    e.run()
    assert e.sf("bootcheck-gave-up").exists()
    assert not e.did("update-triggered")


def test_already_recovering_gives_up_no_second_revert(tmp_path):
    e = _Env(tmp_path)
    e.sf("boot-fail-count").write_text("2")
    e.sf("lkg-sha").write_text(e.lkg + "\n")
    e.sf("bootcheck-recovering").write_text("")  # rollback already happened
    e.run()
    assert e.sf("bootcheck-gave-up").exists()
    assert e.did("splash")
    assert not e.did("update-triggered")  # does NOT revert again
    assert not e.did("rebooted")


# ── DEFERRAL GATES ───────────────────────────────────────────────────


def test_grace_fresh_defers_no_count_change(tmp_path):
    e = _Env(tmp_path)
    e.sf("post-update-grace-until").touch()  # fresh mtime
    r = e.run()
    assert r.returncode == 0
    assert e.count() is None
    assert not e.did("rebooted")
    assert not e.did("update-triggered")


def test_update_lock_held_defers(tmp_path):
    e = _Env(tmp_path)
    fd = os.open(str(e.lock), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        r = e.run()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert r.returncode == 0
    assert e.count() is None  # never evaluated
    assert not e.did("rebooted")
    assert not e.did("update-triggered")


# ── STRUCTURAL INVARIANTS (pin design decisions) ─────────────────────


def test_script_uses_scoped_reboot_and_update_grants():
    body = SCRIPT.read_text()
    # Reboot + trigger default to the exact sudoers/020-authorized argv.
    assert "sudo systemctl reboot" in body
    assert "sudo systemctl start --no-block litclock-update.service" in body


def test_script_releases_lock_before_triggering_update():
    body = SCRIPT.read_text()
    # The update trigger must run AFTER _release_lock so update.sh can acquire
    # the same lock. Assert the release call precedes the trigger invocation.
    assert "_release_lock" in body
    rel = body.rindex("_release_lock")
    trig = body.rindex("$UPDATE_TRIGGER_CMD")
    assert rel < trig, "must release update.lock before triggering update.sh"


# ── GIVE-UP IS TERMINAL (C4) ─────────────────────────────────────────


def test_give_up_is_terminal_no_repeat(tmp_path):
    """Once bootcheck-gave-up is set, a subsequent unhealthy poll does nothing —
    no re-run of the (possibly broken) splash, no reboot, no counter change."""
    e = _Env(tmp_path)
    e.sf("bootcheck-gave-up").write_text("prior give-up")
    r = e.run()  # unhealthy (no heartbeat)
    assert r.returncode == 0
    assert not e.did("splash")  # did NOT re-run the splash
    assert not e.did("rebooted")
    assert not e.did("update-triggered")
    assert e.count() is None  # counter untouched


def test_healthy_boot_clears_give_up_marker(tmp_path):
    """A device that recovers by other means resumes monitoring."""
    e = _Env(tmp_path)
    now = int(time.time())
    _mark_heartbeat(e.heartbeat, mtime=now)
    e.sf("bootcheck-gave-up").write_text("stale")
    e.run(boot_epoch=now - 500)
    assert not e.sf("bootcheck-gave-up").exists()


# ── TRIGGER FAILURE (C5) ─────────────────────────────────────────────


def test_trigger_failure_does_not_mark_recovering(tmp_path):
    """If `systemctl start` fails, bootcheck must NOT set bootcheck-recovering
    (else the next threshold gives up without ever rolling back) and must leave
    the counter at threshold so the next tick re-triggers promptly."""
    e = _Env(tmp_path)
    e.sf("boot-fail-count").write_text("2")
    e.sf("lkg-sha").write_text(e.lkg + "\n")
    e.run(update_cmd="false")  # trigger command fails
    # rollback-target + blocked-sha still pinned (update.sh will consume later)
    assert e.sf("rollback-target").read_text().strip() == e.lkg
    # but recovery is NOT marked, and the counter is retained for a prompt retry
    assert not e.sf("bootcheck-recovering").exists()
    assert e.count() == "3"


# ── THRESHOLD BOUNDARY (T1) ──────────────────────────────────────────


def test_threshold_one_reverts_on_first_failure(tmp_path):
    """threshold=1 → the very first failed boot skips the reboot branch and goes
    straight to rollback. Pins the `-lt` vs `-ge` boundary at the smallest legal
    threshold."""
    e = _Env(tmp_path)
    e.sf("lkg-sha").write_text(e.lkg + "\n")
    e.run(threshold=1)
    assert e.did("update-triggered")
    assert not e.did("rebooted")
    assert e.sf("rollback-target").read_text().strip() == e.lkg


# ── COUNTER SANITIZATION (T2) ────────────────────────────────────────


def test_corrupt_counter_treated_as_zero(tmp_path):
    e = _Env(tmp_path)
    e.sf("boot-fail-count").write_text("garbage\nnope")
    e.run()
    # 'garbage' → tr strips non-digits → 'nope' has none → 0, then +1 = 1
    assert e.count() == "1"
    assert e.did("rebooted")


# ── HEARTBEAT BOUNDARY (T3) ──────────────────────────────────────────


def test_heartbeat_mtime_equal_boot_epoch_is_healthy(tmp_path):
    """mtime == boot_epoch counts as painted-this-boot (>= comparison)."""
    e = _Env(tmp_path)
    x = int(time.time()) - 300
    _mark_heartbeat(e.heartbeat, mtime=x)
    e.sf("boot-fail-count").write_text("1")
    e.run(boot_epoch=x)
    assert e.count() is None  # healthy → cleared
    assert not e.did("rebooted")


# ── PREFLIGHT GUARDS (T5) ────────────────────────────────────────────


def test_non_git_install_dir_exits_clean(tmp_path):
    """A non-git INSTALL_DIR must exit 0 with no state mutation / no reboot."""
    non_git = tmp_path / "notgit"
    non_git.mkdir()
    (non_git / "scripts").symlink_to(REPO_ROOT / "scripts")
    state = tmp_path / "state"
    state.mkdir()
    markers = tmp_path / "markers"
    markers.mkdir()
    lock = state / "update.lock"
    lock.touch()
    r = _run(
        install=non_git,
        state_dir=state,
        heartbeat_file=tmp_path / "run" / "heartbeat",
        lock_file=lock,
        markers=markers,
    )
    assert r.returncode == 0
    assert not (state / "boot-fail-count").exists()
    assert not (markers / "rebooted").exists()


# ── COUNTER-PERSIST FAILURE GUARD (T8) ───────────────────────────────


def test_counter_persist_failure_aborts_without_reboot(tmp_path):
    """If the counter can't be persisted, bootcheck must abort WITHOUT rebooting
    (else an unbounded reboot loop on a read-only / full state dir). Force BOTH
    the direct write (state dir read-only) AND the sudo-tee fallback (stub sudo
    that fails) to fail — otherwise a passwordless-sudo dev box would write the
    counter as root and reboot."""
    stub = tmp_path / "stubbin"
    stub.mkdir()
    (stub / "sudo").write_text("#!/bin/sh\nexit 1\n")
    (stub / "sudo").chmod(0o755)
    e = _Env(tmp_path)
    e.state.chmod(0o555)
    try:
        r = e.run(extra_path=str(stub))
    finally:
        e.state.chmod(0o755)
    assert r.returncode == 0
    assert not e.did("rebooted")
    assert not e.did("update-triggered")
