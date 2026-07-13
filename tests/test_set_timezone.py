"""Tests for scripts/litclock-set-timezone (#387 tz-wrapper).

The root-owned wrapper is the security boundary for the arbitrary-tz sudo path:
sudoers/020 authorizes its fixed path, and the wrapper re-validates the argument
against the kernel zoneinfo list in root-owned code (a pi caller cannot be
trusted). These tests pin that validation and that a rejected tz never reaches
`timedatectl set-timezone`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = REPO_ROOT / "scripts" / "litclock-set-timezone"

_STUB = """#!/bin/sh
# Stub timedatectl: `list-timezones` prints a fixed set; `set-timezone <tz>`
# records the tz to $STUB_LOG and exits 0.
case "$1" in
    list-timezones) printf 'UTC\\nAmerica/Chicago\\nEurope/London\\nAsia/Kolkata\\n' ;;
    set-timezone) printf '%s\\n' "$2" >> "$STUB_LOG" ;;
    *) exit 64 ;;
esac
"""


def _run(tmp_path: Path, arg: str | None, *args: str, with_timedatectl: bool = True):
    """Run the wrapper with a stub `timedatectl` on PATH (NOT an env override —
    the wrapper resolves timedatectl via PATH precisely so it's stubbable in
    tests while staying safe under sudo's secure_path in production)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "set-timezone.log"
    if with_timedatectl:
        stub = bindir / "timedatectl"
        stub.write_text(_STUB)
        stub.chmod(0o755)
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "STUB_LOG": str(log)}
    argv = ["bash", str(WRAPPER)]
    if arg is not None:
        argv.append(arg)
    argv.extend(args)
    proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=10)
    applied = log.read_text().splitlines() if log.exists() else []
    return proc, applied


def test_valid_tz_is_applied(tmp_path):
    proc, applied = _run(tmp_path, "America/Chicago")
    assert proc.returncode == 0, proc.stderr
    assert applied == ["America/Chicago"]


def test_unknown_tz_rejected_and_not_applied(tmp_path):
    proc, applied = _run(tmp_path, "Moon/Base")
    assert proc.returncode == 3
    assert "unknown timezone" in proc.stderr.lower()
    assert applied == []  # never reached set-timezone


def test_illegal_characters_rejected_before_validation(tmp_path):
    # A shell-metacharacter payload must be refused by the structural pre-filter,
    # never passed to timedatectl.
    proc, applied = _run(tmp_path, "Europe/London; rm -rf /")
    assert proc.returncode == 2
    assert applied == []


def test_empty_argument_rejected(tmp_path):
    proc, applied = _run(tmp_path, None)
    assert proc.returncode == 2
    assert applied == []


def test_extra_args_are_ignored(tmp_path):
    """sudoers/020 authorizes the wrapper path with any args; only $1 is used.
    A second arg must never be applied as a timezone."""
    proc, applied = _run(tmp_path, "America/Chicago", "Europe/London")
    assert proc.returncode == 0, proc.stderr
    assert applied == ["America/Chicago"], "only the first arg may be applied"


def test_empty_timezone_list_rejects_everything(tmp_path):
    """If `timedatectl list-timezones` yields nothing, no tz validates → reject
    (never silently accept an unvalidated tz)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "timedatectl").write_text("#!/bin/sh\nexit 0\n")  # prints nothing
    (bindir / "timedatectl").chmod(0o755)
    log = tmp_path / "set-timezone.log"
    proc = subprocess.run(
        ["bash", str(WRAPPER), "America/Chicago"],
        env={"PATH": f"{bindir}:/usr/bin:/bin", "STUB_LOG": str(log)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 3
    assert not log.exists()


def test_missing_timedatectl_errors_cleanly(tmp_path):
    """No timedatectl on PATH → exit non-zero with a clear message, never a
    silent success or an unbounded failure."""
    import shutil

    bash = shutil.which("bash") or "/bin/bash"
    bindir = tmp_path / "emptybin"
    bindir.mkdir()
    proc = subprocess.run(
        [bash, str(WRAPPER), "America/Chicago"],
        env={"PATH": str(bindir)},  # bash invoked by abs path; no timedatectl anywhere
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode != 0
    assert "not found" in proc.stderr.lower()


def test_no_env_var_binary_override(tmp_path):
    """Regression (#387 security /review): the wrapper must NOT take its
    timedatectl path from an env var — that would be a root-code-exec hole if
    an env_keep ever leaked it across sudo."""
    body = WRAPPER.read_text()
    # The name may appear in a cautionary comment; what must NOT appear is an
    # actual expansion that reads the binary path from the environment.
    assert "${LITCLOCK_TIMEDATECTL" not in body, "wrapper must not read its binary path from the environment"
    assert "$LITCLOCK_TIMEDATECTL " not in body


def test_repo_copy_is_executable():
    import stat

    mode = WRAPPER.stat().st_mode
    assert mode & stat.S_IXUSR, "wrapper must be executable in the repo"


def test_passes_shell_syntax_check():
    proc = subprocess.run(["bash", "-n", str(WRAPPER)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
