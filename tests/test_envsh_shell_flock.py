"""Tests for scripts/lib/state.sh env.sh writer-lock helpers (issue litclock-dev#274).

Three layers of coverage:

1. Structural (grep) tests — fast, catch regressions of the helper
   wiring at the call sites.
2. Cross-process flock integration — actually exercises shell + Python
   contention on the sidecar lock that interoperates between
   `fcntl.flock` (src/config.py) and `flock(1)` (the shell helpers).
   This is the regression test that would have caught litclock-dev#274.
3. No-flock fallback — `flock(1)` may be absent in some sandbox/CI
   environments; the helper must degrade to an unlocked write rather
   than fail outright (mirrors scripts/update.sh:71).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
STATE_SH = SCRIPTS_DIR / "lib" / "state.sh"
UPDATE_SH = SCRIPTS_DIR / "update.sh"
RESET_SH = SCRIPTS_DIR / "reset-setup.sh"
PREPARE_SH = SCRIPTS_DIR / "prepare-for-cloning.sh"

# Make src/ importable for the cross-process test.
sys.path.insert(0, str(REPO_ROOT / "src"))


# ─── 1. Structural tests ──────────────────────────────────────────────


class TestStructure:
    """Grep invariants on the scripts. Cheap; runs in any sandbox."""

    def test_update_sh_phase3_runs_under_flock(self) -> None:
        """Phase 3's per-var `>>` merge must run inside `with_env_lock`
        (or, equivalently, an explicit `flock -w 30 -E 75`). Without it
        the merge races src/config.py:atomic_update via its sidecar."""
        content = UPDATE_SH.read_text()
        phase3_start = content.find("Phase 3: Merge new env vars")
        phase3_end = content.find("Phase 4:", phase3_start)
        assert phase3_start != -1, "Phase 3 header missing"
        assert phase3_end != -1, "Phase 4 header missing"
        phase3_body = content[phase3_start:phase3_end]
        assert "with_env_lock" in phase3_body or "flock -w 30 -E 75" in phase3_body, (
            "Phase 3 env.sh merge must run under with_env_lock or explicit flock — "
            "raw `>> env.sh` would race the PWA's atomic_update writer"
        )

    def test_reset_setup_uses_atomic_write_env_sh(self) -> None:
        """reset-setup.sh must rewrite env.sh via atomic_write_env_sh
        so the shared sidecar flock is held during the write."""
        content = RESET_SH.read_text()
        assert 'atomic_write_env_sh "$INSTALL_DIR/env.sh"' in content, (
            "reset-setup.sh must call atomic_write_env_sh, not bare cat-heredoc"
        )

    def test_prepare_for_cloning_uses_atomic_write_env_sh(self) -> None:
        """prepare-for-cloning.sh must rewrite env.sh via atomic_write_env_sh."""
        content = PREPARE_SH.read_text()
        assert 'atomic_write_env_sh "$INSTALL_DIR/env.sh"' in content, (
            "prepare-for-cloning.sh must call atomic_write_env_sh, not bare cat-heredoc"
        )

    def test_lockfile_path_matches_python(self) -> None:
        """state.sh's ENV_FILE_DEFAULT must compute the same path
        src/config.py uses (`/home/pi/litclock/env.sh`) so the sidecar
        lock file resolves to the same inode on both sides. Drifting
        the path would silently break the cross-language interlock."""
        content = STATE_SH.read_text()
        assert "${LITCLOCK_ENV_FILE:-/home/pi/litclock/env.sh}" in content, (
            "state.sh ENV_FILE_DEFAULT must match src/config.py:ENV_FILE_DEFAULT — "
            "both must resolve to /home/pi/litclock/env.sh by default and honor "
            "$LITCLOCK_ENV_FILE override"
        )

    def test_helper_uses_mv_not_redirect(self) -> None:
        """atomic_write_env_sh must stage to a tmp file and `mv -f`
        atomically (rename(2) is atomic on ext4). A direct `>` redirect
        into $dest would tear the file on power loss."""
        content = STATE_SH.read_text()
        # Extract the body of atomic_write_env_sh + _atomic_write_env_sh_finalize.
        helper_start = content.find("atomic_write_env_sh()")
        assert helper_start != -1, "atomic_write_env_sh missing"
        # End at the next blank line after the closing brace of with_env_lock —
        # which sits below the finalize helper. Easier: take to EOF; both helpers
        # are at the bottom of the file.
        helper_body = content[helper_start:]
        assert "mv -f" in helper_body, "atomic_write_env_sh body must use `mv -f` for atomic rename"
        # No bare `> "$dest"` (redirect-to-dest) anywhere — only the
        # staged-tmp pattern. Allow `> "$tmp"` and `> "$lock"` (different vars).
        for forbidden in ('> "$dest"', '>"$dest"', '>> "$dest"', '>>"$dest"'):
            assert forbidden not in helper_body, (
                f"atomic_write_env_sh body must not contain `{forbidden}` — all writes go to the staged tmp, then mv"
            )

    def test_no_production_path_unlinks_sidecar_lock(self) -> None:
        """litclock-dev#274 follow-up — pin the lockfile-inode-stability invariant
        documented at `scripts/lib/state.sh:143-147`.

        Unlinking `env.sh.lock` while a writer holds it breaks the
        cross-writer flock interlock: the next `: > "$lock"` creates a
        new inode and both writers proceed against unrelated lockfiles
        (silent break — no error surfaced anywhere).

        This test scans all production shell + Python for any path that
        could unlink the sidecar and asserts none exist. Test scope is
        `scripts/` + `src/`; tests/ is intentionally excluded since test
        cleanup legitimately removes test-scope lockfiles.

        If you're adding a legitimate need to rotate the sidecar (e.g.,
        a session-bounded rotation), update this test AND state.sh's
        invariant comment together — that forces a review of the
        interlock semantics rather than letting the regression land
        silently.
        """
        forbidden_patterns = [
            re.compile(r"\brm\s+-[rf]+\b[^#]*env\.sh\.lock"),
            re.compile(r"\bunlink\b[^#]*env\.sh\.lock"),
            re.compile(r"os\.(remove|unlink)\([^)]*env\.sh\.lock"),
            re.compile(r"\.unlink\(\s*\)[^#]*env\.sh\.lock"),
        ]

        violations: list[str] = []
        scan_roots = [SCRIPTS_DIR, REPO_ROOT / "src"]
        for root in scan_roots:
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                if path.suffix not in (".sh", ".py"):
                    continue
                try:
                    content = path.read_text()
                except (OSError, UnicodeDecodeError):
                    continue
                for lineno, line in enumerate(content.splitlines(), start=1):
                    stripped = line.lstrip()
                    if stripped.startswith("#"):
                        continue
                    for pat in forbidden_patterns:
                        if pat.search(line):
                            rel = path.relative_to(REPO_ROOT)
                            violations.append(f"{rel}:{lineno}: {line.strip()}")
                            break

        assert not violations, (
            "Production code must NEVER unlink env.sh.lock — doing so creates "
            'a new inode on the next `: > "$lock"` and the cross-writer flock '
            "interlock silently breaks. See scripts/lib/state.sh:143-147.\n"
            "Violations:\n  " + "\n  ".join(violations)
        )


# ─── 2. Cross-process flock integration ───────────────────────────────


@pytest.fixture
def flock_or_skip() -> str:
    """Skip flock-dependent tests when the binary is missing — covered
    separately by the no-flock fallback test."""
    flock_path = shutil.which("flock")
    if not flock_path:
        pytest.skip("flock(1) not on PATH — covered by test_no_flock_fallback")
    return flock_path


@pytest.fixture
def shell_env_factory(tmp_path: Path):
    """Build an environment dict + a Bash command string that sources
    state.sh and exposes the helpers, with LITCLOCK_ENV_FILE pointed
    at a tmp_path env.sh.

    Returns (env_dict, env_sh_path, lock_path) for the tmp_path layout.
    """
    env_sh = tmp_path / "env.sh"
    env_sh.write_text("export WEATHER_UNITS=imperial\n")
    lock_path = tmp_path / "env.sh.lock"

    proc_env = os.environ.copy()
    proc_env["LITCLOCK_ENV_FILE"] = str(env_sh)
    return proc_env, env_sh, lock_path


def _bash_call_helper(helper: str, args: list[str], proc_env: dict) -> subprocess.CompletedProcess:
    """Run a Bash one-liner that sources state.sh and invokes one of
    its helpers. ENV_FILE_DEFAULT is re-computed inside the bash subshell
    from LITCLOCK_ENV_FILE so the lockfile path tracks the tmp_path."""
    quoted_args = " ".join(f"'{a}'" for a in args)
    script = (
        f". '{STATE_SH}' && "
        f'ENV_FILE_DEFAULT="${{LITCLOCK_ENV_FILE:-/home/pi/litclock/env.sh}}" '
        f"{helper} {quoted_args}"
    )
    return subprocess.run(
        ["bash", "-c", script],
        env=proc_env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_python_writer_blocks_on_shell_held_flock(
    flock_or_skip: str, shell_env_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hold the sidecar flock from a background shell process for ~0.5s.
    A foreground Python call into config.atomic_update must wait for the
    shell to release before its os.replace lands. Proves the locks
    interoperate: flock(1) and fcntl.flock both hit flock(2) syscalls
    on the same fd and contend.

    This is the regression test that would have caught litclock-dev#274 — without
    the shared lock, the call returns immediately and the user's update
    races silently with the shell write.
    """
    import config

    proc_env, env_sh, lock_path = shell_env_factory
    monkeypatch.setenv("LITCLOCK_ENV_FILE", str(env_sh))

    # Touch the lockfile so flock(1) has something to open.
    lock_path.touch()

    hold_seconds = 0.5
    # Background bash holds an exclusive flock on the sidecar for hold_seconds.
    bg = subprocess.Popen(
        ["bash", "-c", f"exec {flock_or_skip} -x '{lock_path}' sleep {hold_seconds}"],
    )
    try:
        # Give the background process a moment to actually acquire the lock.
        # (subprocess.Popen returns before the child execs flock.)
        time.sleep(0.1)

        t0 = time.monotonic()
        config.atomic_update({"WEATHER_UNITS": "metric"}, env_sh)
        elapsed = time.monotonic() - t0
    finally:
        bg.wait(timeout=5)

    # The Python writer should have been forced to wait for the shell
    # lock to release. Allow a small slack: hold_seconds=0.5, we slept
    # 0.1s before timing, so the wait is at least ~0.3s. Without the
    # interlock the call returns in <50ms.
    assert elapsed >= 0.3, (
        f"atomic_update returned in {elapsed:.3f}s while shell held the flock — "
        "the cross-language sidecar lock isn't interlocking. Regression of litclock-dev#274."
    )

    # And the update must have actually landed once the lock cleared.
    assert "WEATHER_UNITS=metric" in env_sh.read_text()


def test_shell_helper_blocks_on_python_held_flock(flock_or_skip: str, shell_env_factory) -> None:
    """Reverse direction: Python holds the lock via _exclusive_lock,
    a shell atomic_write_env_sh against the same sidecar must exit 75
    (the timeout exit code). Proves the lock direction is bidirectional,
    that 75 actually surfaces to the shell caller, AND that the helper
    itself (not just the underlying flock primitive) wraps the contract
    correctly — a regression in atomic_write_env_sh's lock acquisition
    would surface here.

    Uses LITCLOCK_ENV_LOCK_WAIT=1 to keep the helper's wait short so the
    test doesn't stall the suite for 30s.
    """
    import config

    proc_env, env_sh, _lock_path = shell_env_factory
    proc_env["LITCLOCK_ENV_LOCK_WAIT"] = "1"

    # Hold the Python flock for the entire duration of the shell call.
    with config._exclusive_lock(env_sh):  # noqa: SLF001 — test of the contract
        proc = _bash_call_helper(
            "atomic_write_env_sh",
            [str(env_sh), "export WEATHER_UNITS=metric\n"],
            proc_env,
        )

    assert proc.returncode == 75, (
        f"atomic_write_env_sh should exit 75 when Python holds the lock, "
        f"got rc={proc.returncode} stderr={proc.stderr!r} stdout={proc.stdout!r}. "
        "The fcntl/flock interlock isn't bidirectional OR the helper isn't "
        "honoring LITCLOCK_ENV_LOCK_WAIT."
    )

    # And the destination must NOT have been touched.
    assert env_sh.read_text() == "export WEATHER_UNITS=imperial\n", (
        "atomic_write_env_sh wrote the destination despite the lock timeout — "
        "the staged tmp must be cleaned up and dest left alone on rc=75."
    )


# ─── 3. No-flock fallback ─────────────────────────────────────────────


def test_atomic_write_env_sh_fallback_when_flock_missing(tmp_path: Path) -> None:
    """If flock(1) is absent (busybox sandboxes, minimal CI containers),
    atomic_write_env_sh must still write the destination and emit a
    warn to stderr. Refusing to write would break those environments;
    matches the degrade-to-unguarded pattern at scripts/update.sh:71."""
    env_sh = tmp_path / "env.sh"
    env_sh.write_text("export WEATHER_UNITS=imperial\n")

    # Build a PATH that contains coreutils (mv, mktemp, stat, chmod, …)
    # but NOT flock. /bin and /usr/bin both ship flock on Debian, so we
    # stage a sandbox dir with symlinks to the coreutils we need but
    # explicitly omit flock.
    sandbox_bin = tmp_path / "sandbox_bin"
    sandbox_bin.mkdir()
    # bash, mktemp, mv, stat, chmod, chown, rm, printf, cat, grep, sed,
    # head, tail, tr, dirname, basename, sleep, touch, ls, command, test —
    # cover everything the helper might invoke.
    for tool in (
        "bash",
        "mktemp",
        "mv",
        "stat",
        "chmod",
        "chown",
        "rm",
        "printf",
        "cat",
        "grep",
        "sed",
        "head",
        "tail",
        "tr",
        "dirname",
        "basename",
        "sleep",
        "touch",
        "ls",
        "test",
        "sudo",
    ):
        real = shutil.which(tool)
        if real:
            os.symlink(real, sandbox_bin / tool)

    # Sanity: ensure our sandbox PATH does NOT resolve flock.
    proc_env = {
        "PATH": str(sandbox_bin),
        "LITCLOCK_ENV_FILE": str(env_sh),
        # Preserve HOME so bash doesn't complain in some setups.
        "HOME": os.environ.get("HOME", str(tmp_path)),
    }
    assert shutil.which("flock", path=str(sandbox_bin)) is None, (
        "test bug: sandbox PATH still resolves flock — pick a different tool list"
    )

    new_body = "export WEATHER_UNITS=metric\nexport WEATHER_TTL=3600\n"
    script = f". '{STATE_SH}' && atomic_write_env_sh '{env_sh}' \"$NEW_BODY\""
    proc_env["NEW_BODY"] = new_body
    proc = subprocess.run(
        ["bash", "-c", script],
        env=proc_env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert proc.returncode == 0, (
        f"atomic_write_env_sh should still write when flock is missing, got rc={proc.returncode} stderr={proc.stderr!r}"
    )
    # The body must have actually landed.
    assert env_sh.read_text() == new_body, (
        f"env.sh contents wrong after no-flock fallback write: {env_sh.read_text()!r}"
    )
    # And a warn must have surfaced so an operator knows we degraded.
    assert re.search(r"flock\(1\)\s+unavailable", proc.stderr), (
        f"no-flock fallback must emit a warn to stderr, got stderr={proc.stderr!r}"
    )


# ─── 4. Security regression tests ────────────────────────────────────


def test_atomic_write_env_sh_refuses_symlink_destination(tmp_path: Path) -> None:
    """If an attacker plants a symlink at env.sh (parent dir is pi-owned,
    so a pi-shell-compromise scenario), `stat -c '%a'` returns the
    symlink's own mode (0777) — without the rejection guard, that gets
    chmod'd onto the staged tmp and the resulting env.sh ships
    world-writable with the OPENWEATHERMAP_APIKEY inside. reset-setup.sh
    and prepare-for-cloning.sh both run as root, so this is a
    defense-in-depth concern flagged by /review on PR litclock-dev#366.

    The helper must refuse to proceed, return non-zero, and leave the
    destination untouched.
    """
    target = tmp_path / "real_target"
    target.write_text("payload\n")
    env_sh_link = tmp_path / "env.sh"
    env_sh_link.symlink_to(target)

    proc_env = os.environ.copy()
    proc_env["LITCLOCK_ENV_FILE"] = str(env_sh_link)
    new_body = "export WEATHER_UNITS=metric\n"
    script = (
        f". '{STATE_SH}' && "
        f'ENV_FILE_DEFAULT="${{LITCLOCK_ENV_FILE}}" '
        f"atomic_write_env_sh '{env_sh_link}' \"$NEW_BODY\""
    )
    proc_env["NEW_BODY"] = new_body
    proc = subprocess.run(
        ["bash", "-c", script],
        env=proc_env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert proc.returncode != 0, (
        f"atomic_write_env_sh should reject symlink destinations, got rc={proc.returncode} stderr={proc.stderr!r}"
    )
    # Symlink and target must both be unchanged.
    assert env_sh_link.is_symlink(), "symlink was replaced — the rejection guard failed"
    assert env_sh_link.resolve() == target.resolve(), "symlink redirected"
    assert target.read_text() == "payload\n", "symlink target body was modified"
    # And the operator gets a clear error so they can investigate.
    assert "symlink" in proc.stderr.lower(), (
        f"error message must mention symlink so the operator knows what happened, got stderr={proc.stderr!r}"
    )


def test_atomic_write_env_sh_uses_0644_on_first_boot(flock_or_skip: str, tmp_path: Path) -> None:
    """When dest doesn't exist (first-boot path), mktemp stages the file
    at 0600 — which would block the pi-user `source env.sh` in
    runtheclock.sh from reading it on a fresh install. The finalize
    helper must explicitly chmod 0644 before the rename. Adversarial
    review on PR litclock-dev#366 flagged this as the next-caller footgun.
    """
    env_sh = tmp_path / "env.sh"  # deliberately does NOT exist
    assert not env_sh.exists(), "test bug: dest should not pre-exist"

    proc_env = os.environ.copy()
    proc_env["LITCLOCK_ENV_FILE"] = str(env_sh)
    new_body = "export WEATHER_UNITS=metric\n"
    script = (
        f". '{STATE_SH}' && ENV_FILE_DEFAULT=\"${{LITCLOCK_ENV_FILE}}\" atomic_write_env_sh '{env_sh}' \"$NEW_BODY\""
    )
    proc_env["NEW_BODY"] = new_body
    proc = subprocess.run(
        ["bash", "-c", script],
        env=proc_env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert proc.returncode == 0, f"first-boot write failed: rc={proc.returncode} stderr={proc.stderr!r}"
    assert env_sh.exists() and env_sh.read_text() == new_body
    mode = env_sh.stat().st_mode & 0o777
    assert mode == 0o644, f"first-boot env.sh must be 0644 so pi-user `source env.sh` works, got {oct(mode)}"


# ─── 5. Cross-writer timeout symmetry (litclock-dev#274 follow-up #4) ─────────────


def test_python_writer_times_out_when_shell_holds_flock_too_long(
    flock_or_skip: str, shell_env_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """litclock-dev#274 follow-up #4 — symmetry: shell-side helpers already exit 75
    on a 30s flock timeout. The Python side must now match.

    Hold the sidecar flock from a background shell for 3 seconds. With
    a Python-side budget of 0.3s, `atomic_update` must raise TimeoutError
    rather than block until the shell releases (the pre-litclock-dev#274-followup-#4
    behavior). Surface as TimeoutError so the route handler can emit the
    504 ENV_LOCK_TIMEOUT envelope instead of letting the Flask worker
    accumulate a stuck thread.
    """
    import config

    proc_env, env_sh, lock_path = shell_env_factory
    monkeypatch.setenv("LITCLOCK_ENV_FILE", str(env_sh))
    lock_path.touch()

    # Background bash holds the lock for 3s — well beyond the 0.3s budget.
    bg = subprocess.Popen(
        ["bash", "-c", f"exec {flock_or_skip} -x '{lock_path}' sleep 3"],
    )
    try:
        # Give the background process time to acquire.
        time.sleep(0.1)
        # Drop the module-level default to a tight budget so the test
        # finishes quickly. Restored in finally.
        original_default = config.ENV_LOCK_WAIT_DEFAULT
        config.ENV_LOCK_WAIT_DEFAULT = 0.3
        try:
            t0 = time.monotonic()
            with pytest.raises(TimeoutError, match=r"env\.sh lock held"):
                config.atomic_update({"WEATHER_UNITS": "metric"}, env_sh)
            elapsed = time.monotonic() - t0
        finally:
            config.ENV_LOCK_WAIT_DEFAULT = original_default
    finally:
        bg.wait(timeout=10)

    # Must raise within ~budget + small slack. Pre-fix would have waited
    # the full 3s; post-fix must exit near 0.3s.
    assert elapsed < 1.5, (
        f"atomic_update should raise TimeoutError near the 0.3s budget, took {elapsed:.3f}s — "
        "regression of litclock-dev#274 follow-up #4 bounded-wait"
    )


def test_python_and_shell_share_lock_wait_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Symmetry contract: `LITCLOCK_ENV_LOCK_WAIT=N` must affect BOTH the
    shell helpers (`scripts/lib/state.sh`'s `flock -w "$wait_seconds"`)
    AND the Python side (`config.ENV_LOCK_WAIT_DEFAULT`). Without this
    pin, a future refactor could drift the env-var name on one side and
    silently break the documented promise.

    Pinned via grep so the test catches a rename in either direction.
    """
    state_sh = STATE_SH.read_text()
    # Shell side: state.sh reads LITCLOCK_ENV_LOCK_WAIT into its wait_seconds.
    assert "LITCLOCK_ENV_LOCK_WAIT" in state_sh, (
        "state.sh must honor LITCLOCK_ENV_LOCK_WAIT — symmetry with Python side"
    )
    # Python side: config._parse_env_lock_wait_default reads the same var.
    import config

    assert hasattr(config, "ENV_LOCK_WAIT_DEFAULT"), (
        "config.ENV_LOCK_WAIT_DEFAULT must exist — Python-side budget for the "
        "env.sh flock acquire timeout (litclock-dev#274 follow-up #4)"
    )
    # End-to-end: setting the env-var changes the parsed default.
    monkeypatch.setenv("LITCLOCK_ENV_LOCK_WAIT", "7")
    assert config._parse_env_lock_wait_default() == 7.0, (
        "Python parser must honor LITCLOCK_ENV_LOCK_WAIT — matching shell-side budget"
    )


# ─── 6. Durability barriers (litclock-dev#893) ───────────────────────
#
# rename is atomic for readers, not durable: the staged bytes must be fsynced
# BEFORE `mv` publishes them (else a power cut can leave a zero-length file)
# and the directory AFTER (else the rename itself can be lost). A power cut
# cannot be staged, so these shadow `sync` and `mv` with loggers that delegate
# to the real binaries and assert on the ORDER, which is the whole property.


def _stub_bin(tmp_path: Path, fail: str = "") -> tuple[Path, Path]:
    """`sync` + `mv` loggers. ``fail`` = "file" or "dir" makes `sync` exit 1
    for that kind of operand (and only that kind) instead of syncing."""
    real_sync, real_mv = shutil.which("sync"), shutil.which("mv")
    assert real_sync and real_mv
    stub = tmp_path / "stub_bin"
    stub.mkdir()
    log = tmp_path / "calls.log"
    fail_test = {"": "false", "file": '[[ -f "$1" ]]', "dir": '[[ -d "$1" ]]'}[fail]
    (stub / "sync").write_text(
        f"#!/bin/bash\necho \"sync $*\" >> '{log}'\nif {fail_test}; then exit 1; fi\nexec '{real_sync}' \"$@\"\n"
    )
    (stub / "mv").write_text(f"#!/bin/bash\necho \"mv $*\" >> '{log}'\nexec '{real_mv}' \"$@\"\n")
    for f in ("sync", "mv"):
        (stub / f).chmod(0o755)
    return stub, log


def _env_harness(tmp_path: Path, fail: str = "") -> tuple[dict, Path, Path]:
    stub, log = _stub_bin(tmp_path, fail)
    home = tmp_path / "home"
    home.mkdir()
    env_sh = home / "env.sh"
    env_sh.write_text("export WEATHER_UNITS=imperial\n")
    proc_env = os.environ.copy()
    proc_env["PATH"] = f"{stub}:{proc_env['PATH']}"
    proc_env["LITCLOCK_ENV_FILE"] = str(env_sh)
    proc_env["NEW_BODY"] = "export WEATHER_UNITS=metric\n"
    return proc_env, env_sh, log


def _run_atomic_write(proc_env: dict, env_sh: Path) -> subprocess.CompletedProcess:
    script = (
        f". '{STATE_SH}' && ENV_FILE_DEFAULT=\"${{LITCLOCK_ENV_FILE}}\" atomic_write_env_sh '{env_sh}' \"$NEW_BODY\""
    )
    return subprocess.run(["bash", "-c", script], env=proc_env, capture_output=True, text=True, timeout=30)


def _staged_leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name.startswith("env.sh.") and p.name != "env.sh.lock")


def _assert_synced_around_the_rename(calls: list[str], dest: Path) -> None:
    mv_idx = [i for i, c in enumerate(calls) if c.startswith("mv ")]
    assert len(mv_idx) == 1, calls
    m = mv_idx[0]
    staged = calls[m].split()[-2]
    assert Path(staged).parent == dest.parent and staged != str(dest), calls
    assert f"sync {staged}" in calls[:m], f"staged file must be synced BEFORE the rename: {calls}"
    assert f"sync {dest.parent}" in calls[m + 1 :], f"its directory must be synced AFTER the rename: {calls}"


def test_atomic_write_env_sh_syncs_file_before_rename_and_dir_after(flock_or_skip: str, tmp_path: Path) -> None:
    """Every shell env.sh writer — reset-setup, clone prep, first-boot,
    update.sh's Stage B flip — goes through this finalizer."""
    proc_env, env_sh, log = _env_harness(tmp_path)
    proc = _run_atomic_write(proc_env, env_sh)
    assert proc.returncode == 0, proc.stderr
    assert env_sh.read_text() == "export WEATHER_UNITS=metric\n"
    _assert_synced_around_the_rename(log.read_text().splitlines(), env_sh)


def test_atomic_write_env_sh_failed_file_sync_leaves_env_untouched(flock_or_skip: str, tmp_path: Path) -> None:
    """A failed data flush aborts before the rename, destination untouched and
    no staged copy left behind (a leaked `env.sh.XXXXXX` is a full copy of
    env.sh, API key included)."""
    proc_env, env_sh, log = _env_harness(tmp_path, fail="file")
    proc = _run_atomic_write(proc_env, env_sh)
    assert proc.returncode != 0
    assert "could not flush" in proc.stderr
    assert env_sh.read_text() == "export WEATHER_UNITS=imperial\n"
    assert not any(c.startswith("mv ") for c in log.read_text().splitlines())
    assert _staged_leftovers(env_sh.parent) == []


def test_atomic_write_env_sh_failed_dir_sync_still_succeeds_but_says_so(flock_or_skip: str, tmp_path: Path) -> None:
    """The new file is in place and its bytes are durable, so a failed
    directory sync must not report the write as failed — reset-setup would
    print "env.sh untouched" over a file it had replaced — but it must not be
    silent either (owner decision, litclock-dev#893 review)."""
    proc_env, env_sh, _ = _env_harness(tmp_path, fail="dir")
    proc = _run_atomic_write(proc_env, env_sh)
    assert proc.returncode == 0, proc.stderr
    assert env_sh.read_text() == "export WEATHER_UNITS=metric\n"
    assert "could not sync directory" in proc.stderr, proc.stderr


def test_atomic_write_env_sh_no_flock_failed_finalize_leaves_no_staged_copy(tmp_path: Path) -> None:
    """The no-flock fallback used to `return $?` straight out of a failed
    finalize, leaking the staged env.sh copy; the flock path always cleaned
    up. PATH holds the failing `sync` stub and coreutils, but no flock."""
    stub, _ = _stub_bin(tmp_path, fail="file")
    for tool in ("bash", "mktemp", "stat", "chmod", "chown", "rm", "dirname", "basename"):
        real = shutil.which(tool)
        if real:
            os.symlink(real, stub / tool)
    assert shutil.which("flock", path=str(stub)) is None
    env_sh = tmp_path / "env.sh"
    env_sh.write_text("export WEATHER_UNITS=imperial\n")
    proc_env = {"PATH": str(stub), "HOME": str(tmp_path), "NEW_BODY": "export WEATHER_UNITS=metric\n"}
    proc = _run_atomic_write(proc_env | {"LITCLOCK_ENV_FILE": str(env_sh)}, env_sh)
    assert proc.returncode != 0
    assert "flock(1) unavailable" in proc.stderr, "test bug: did not take the no-flock path"
    assert env_sh.read_text() == "export WEATHER_UNITS=imperial\n"
    assert _staged_leftovers(tmp_path) == []


def test_atomic_write_env_sh_without_sync_still_writes(flock_or_skip: str, tmp_path: Path) -> None:
    """`sync` absent (busybox sandboxes) degrades to the unsynced write rather
    than refusing — pinned deliberately, not left to a tool-list accident."""
    sandbox = tmp_path / "bin"
    sandbox.mkdir()
    for tool in ("bash", "mktemp", "mv", "stat", "chmod", "chown", "rm", "dirname", "basename", "flock"):
        real = shutil.which(tool)
        if real:
            os.symlink(real, sandbox / tool)
    assert shutil.which("sync", path=str(sandbox)) is None
    env_sh = tmp_path / "env.sh"
    env_sh.write_text("export WEATHER_UNITS=imperial\n")
    proc_env = {"PATH": str(sandbox), "HOME": str(tmp_path), "LITCLOCK_ENV_FILE": str(env_sh)}
    proc = _run_atomic_write(proc_env | {"NEW_BODY": "export WEATHER_UNITS=metric\n"}, env_sh)
    assert proc.returncode == 0, proc.stderr
    assert env_sh.read_text() == "export WEATHER_UNITS=metric\n"


def _run_atomic_write_file(proc_env: dict, dest: Path, content: str) -> subprocess.CompletedProcess:
    proc_env = proc_env | {"CONTENT": content}
    script = f". '{STATE_SH}' && atomic_write_file '{dest}' \"$CONTENT\""
    return subprocess.run(["bash", "-c", script], env=proc_env, capture_output=True, text=True, timeout=30)


def test_atomic_write_file_syncs_file_before_rename_and_dir_after(tmp_path: Path) -> None:
    """/var/lib/litclock state is mostly written FRESH — rollback-target,
    blocked-sha, the self-test pass record — which ext4's auto_da_alloc does
    not cover, so it was more exposed than env.sh. An empty rollback-target
    after a power cut silently cancels the auto-rollback (litclock-dev#893 red team)."""
    stub, log = _stub_bin(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    dest = state / "rollback-target"
    proc_env = os.environ.copy() | {"PATH": f"{stub}:{os.environ['PATH']}"}
    proc = _run_atomic_write_file(proc_env, dest, "a" * 40)
    assert proc.returncode == 0, proc.stderr
    assert dest.read_text() == "a" * 40
    _assert_synced_around_the_rename(log.read_text().splitlines(), dest)


def test_atomic_write_file_failed_file_sync_does_not_publish(tmp_path: Path) -> None:
    stub, log = _stub_bin(tmp_path, fail="file")
    state = tmp_path / "state"
    state.mkdir()
    dest = state / "rollback-target"
    proc_env = os.environ.copy() | {"PATH": f"{stub}:{os.environ['PATH']}"}
    proc = _run_atomic_write_file(proc_env, dest, "a" * 40)
    assert proc.returncode != 0
    assert not dest.exists()
    assert list(state.iterdir()) == [], "the staged copy must be removed"
    assert not any(c.startswith("mv ") for c in log.read_text().splitlines())


def test_atomic_write_file_failed_dir_sync_still_succeeds_but_says_so(tmp_path: Path) -> None:
    stub, _ = _stub_bin(tmp_path, fail="dir")
    state = tmp_path / "state"
    state.mkdir()
    dest = state / "blocked-sha"
    proc_env = os.environ.copy() | {"PATH": f"{stub}:{os.environ['PATH']}"}
    proc = _run_atomic_write_file(proc_env, dest, "b" * 40)
    assert proc.returncode == 0, proc.stderr
    assert dest.read_text() == "b" * 40
    assert "could not sync directory" in proc.stderr, proc.stderr


def _sudo_fallback_bin(tmp_path: Path, fail_first_file_sync: bool) -> tuple[Path, Path]:
    """Force atomic_write_file's `sudo tee` fallback: `mktemp` fails, `sudo`
    runs its arguments as-is, and `sync` logs whether it ran under sudo."""
    stub, log = _stub_bin(tmp_path)
    real_sync = shutil.which("sync")
    flag = tmp_path / "failed-once"
    fail = f"[[ -f \"$1\" && ! -e '{flag}' ]] && {{ touch '{flag}'; exit 1; }}\n" if fail_first_file_sync else ""
    (stub / "sync").write_text(
        f"#!/bin/bash\necho \"sync${{IN_SUDO:+ (sudo)}} $*\" >> '{log}'\n{fail}exec '{real_sync}' \"$@\"\n"
    )
    (stub / "sudo").write_text('#!/bin/bash\nIN_SUDO=1 exec "$@"\n')
    (stub / "mktemp").write_text("#!/bin/bash\nexit 1\n")
    for f in ("sync", "sudo", "mktemp"):
        (stub / f).chmod(0o755)
    return stub, log


def test_atomic_write_file_sudo_fallback_flushes_once_with_sudo(tmp_path: Path) -> None:
    stub, log = _sudo_fallback_bin(tmp_path, fail_first_file_sync=False)
    state = tmp_path / "state"
    state.mkdir()
    dest = state / "rollback-target"
    proc = _run_atomic_write_file(os.environ.copy() | {"PATH": f"{stub}:{os.environ['PATH']}"}, dest, "a" * 40)
    assert proc.returncode == 0, proc.stderr
    assert dest.read_text() == "a" * 40
    calls = log.read_text().splitlines()
    file_syncs = [c for c in calls if c.startswith("sync") and ".tmp." in c]
    assert file_syncs == [f"sync (sudo) {dest}.tmp.{file_syncs[0].rsplit('.', 1)[-1]}"], calls


def test_atomic_write_file_sudo_fallback_does_not_retry_a_failed_flush(tmp_path: Path) -> None:
    """A retried fsync can succeed after the kernel dropped the failed pages;
    an unprivileged try followed by a sudo retry published exactly the file a
    flush had just rejected (litclock-dev#893 review, Codex)."""
    stub, log = _sudo_fallback_bin(tmp_path, fail_first_file_sync=True)
    state = tmp_path / "state"
    state.mkdir()
    dest = state / "rollback-target"
    proc = _run_atomic_write_file(os.environ.copy() | {"PATH": f"{stub}:{os.environ['PATH']}"}, dest, "a" * 40)
    assert proc.returncode != 0
    assert not dest.exists(), "a file whose flush failed must not be published"
    assert list(state.iterdir()) == [], "the staged copy must be removed"
