"""Shared fixtures for LitClock tests."""

import json
import os
import shlex
import subprocess
import textwrap
import warnings
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# litclock-dev#607 review — control_server routes consult systemctl (via
# update_state.update_is_busy) on /updates renders and idle status polls.
# Several test modules exercise those routes without stubbing the gate, so
# without this, test outcomes would depend on the HOST's systemd state (a
# box where litclock-update.service is active would flip /updates into the
# in-progress render under unrelated tests). Point SYSTEMCTL_BIN at a path
# that cannot exist so the real gate fails fast and returns False. Must be
# set BEFORE any test module imports control_server (the module reads the
# env at import time) — conftest import order guarantees that. setdefault so
# a deliberate override still wins.
os.environ.setdefault("LITCLOCK_SYSTEMCTL", str(REPO_ROOT / "tests" / ".no-such-systemctl"))


@pytest.fixture
def tmp_env_file(tmp_path):
    """Create a temporary env.sh file with typical content."""
    env_file = tmp_path / "env.sh"
    env_file.write_text(
        textwrap.dedent("""\
            #!/usr/bin/env bash
            WEATHER_LATITUDE=0
            WEATHER_LONGITUDE=0
            WEATHER_UNITS=imperial
            OPENWEATHERMAP_APIKEY=
            ALLOW_NSFW_QUOTES=false
        """)
    )
    return str(env_file)


@dataclass
class SandboxResult:
    """Result of running a shell script in the sandbox."""

    completed: subprocess.CompletedProcess
    calls: list  # list of {"cmd": str, "args": [str, ...]} entries, in call order

    @property
    def stdout(self) -> str:
        return self.completed.stdout

    @property
    def stderr(self) -> str:
        return self.completed.stderr

    @property
    def returncode(self) -> int:
        return self.completed.returncode

    def calls_for(self, cmd: str) -> list:
        """All recorded invocations of a given stubbed command."""
        return [c for c in self.calls if c["cmd"] == cmd]


class ScriptSandbox:
    """A sandbox for running shell scripts with stubbed external commands.

    Each stub logs its invocation (command name + args) to a JSONL file so
    tests can assert on call order and arguments without having to capture
    stdout. Stubs default to exit 0 with empty stdout but can be customized.
    """

    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "install"
        self.root.mkdir(parents=True, exist_ok=True)
        self.bindir = tmp_path / "bin"
        self.bindir.mkdir(parents=True, exist_ok=True)
        self.call_log = tmp_path / "calls.jsonl"
        self.call_log.touch()

    def stub(self, name: str, exit_code: int = 0, stdout: str = "", stderr: str = "") -> Path:
        """Install a PATH shim for `name` that logs its args and exits."""
        script = self.bindir / name
        # Shell-escape the strings for safe embedding.
        esc_stdout = shlex.quote(stdout)
        esc_stderr = shlex.quote(stderr)
        esc_log = shlex.quote(str(self.call_log))
        esc_name = shlex.quote(name)
        script.write_text(
            textwrap.dedent(f"""\
                #!/bin/bash
                # Build the JSON array of args in ONE argv-based python call.
                # The previous per-argument `printf | python3 -c` pipeline lost
                # its FIRST element whenever the shim ran under a process-group
                # wrapper (`timeout`, `setsid`), corrupting the call log into
                # `"args": [,"ls-remote",...]` — litclock-dev#835 review, rounds
                # 4-9, which cost three hand-rolled watchdogs before the cause
                # was pinned on the harness rather than the script.
                args_json=$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "$@")
                printf '{{"cmd": %s, "args": %s}}\\n' '"'{esc_name}'"' "$args_json" >> {esc_log}
                if [[ -n {esc_stdout} ]]; then printf '%s' {esc_stdout}; fi
                if [[ -n {esc_stderr} ]]; then printf '%s' {esc_stderr} >&2; fi
                exit {exit_code}
                """)
        )
        script.chmod(0o755)
        return script

    def write_file(self, relpath: str, content: str, mode: int = 0o644) -> Path:
        """Write a file under the sandbox root and return its path."""
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(mode)
        return path

    def run(self, script_path: Path, args=None, env=None, cwd=None) -> SandboxResult:
        """Run a shell script with sandbox PATH and return the result + call log."""
        args = args or []
        run_env = {
            "PATH": f"{self.bindir}:/usr/bin:/bin",
            "HOME": str(self.root),
            "LITCLOCK_DIR": str(self.root),
        }
        if env:
            run_env.update(env)
        completed = subprocess.run(
            ["bash", str(script_path), *args],
            env=run_env,
            cwd=str(cwd or self.root),
            capture_output=True,
            text=True,
        )
        calls = []
        for line in self.call_log.read_text().splitlines():
            line = line.strip()
            if line:
                calls.append(json.loads(line))
        return SandboxResult(completed=completed, calls=calls)


@pytest.fixture
def script_sandbox(tmp_path):
    """Yields a ScriptSandbox for shell-script execution tests."""
    return ScriptSandbox(tmp_path)


@pytest.fixture
def repo_root():
    """Absolute Path to the repository root."""
    return REPO_ROOT


@pytest.fixture
def clean_env():
    """Remove LOG_LEVEL from environment, restore after test."""
    old = os.environ.pop("LOG_LEVEL", None)
    yield
    if old is not None:
        os.environ["LOG_LEVEL"] = old
    else:
        os.environ.pop("LOG_LEVEL", None)


@pytest.fixture(autouse=True)
def _reset_setup_server_state(request):
    """Clear setup_server's module-level connect-flow state between tests (litclock-dev#355).

    The WiFi connect handler spawns a daemon thread that writes
    ``WIFI_CONNECT_ERROR`` / ``WIFI_CONNECT_IN_FLIGHT`` asynchronously, so
    a late write from a prior test can leak into the next test's assertions
    under specific pytest orderings (caught on PR litclock-dev#353 CI run 25968703096).

    We import setup_server lazily and skip cleanly on ModuleNotFoundError
    so this fixture is a no-op when ``src`` is off ``sys.path`` (e.g. running
    a test file directly outside the pytest harness). We deliberately do NOT
    catch the broader ``ImportError`` — a syntax error or broken transitive
    import in ``setup_server.py`` should surface loudly, not silently disable
    test isolation. Reset runs both before and after the test for
    belt-and-suspenders isolation.
    """
    try:
        import setup_server
    except ModuleNotFoundError:
        yield
        return

    setup_server.reset_state()
    try:
        yield
    finally:
        # litclock-dev#786 finding 1: the return value used to be discarded, so
        # an escaped thread produced NO signal at all — the suite went green and
        # the consequence landed later, in a different test. Record it against
        # the test that leaked; pytest_terminal_summary reports it.
        escaped = setup_server.reset_state()
        if escaped:
            _ESCAPED_THREADS.escapes.append((request.node.nodeid, tuple(escaped)))



class _SigtermAbsorberState:
    """What the session absorber installed, exposed so tests can assert on it.

    ``installed`` is False on the graceful-degradation path (see the fixture),
    and ``handler`` is the exact callable we put on SIGTERM — tests assert
    IDENTITY against it, because "some callable is installed" is satisfied by
    any foreign handler and would leave the absorber gone but the suite green.

    ``deliberate`` is how many of ``absorbed`` were sent on purpose by the
    absorber's own tests. Only the excess is evidence of the bug; without this
    the report below would fire on every single run and mean nothing.
    """

    def __init__(self) -> None:
        self.installed = False
        self.handler = None
        self.absorbed: list[int] = []
        self.deliberate = 0

    @property
    def stray(self) -> int:
        return max(0, len(self.absorbed) - self.deliberate)


# Module-level, not fixture-local, because pytest_terminal_summary below is a
# hook and cannot request a fixture.
_SIGTERM_ABSORBER = _SigtermAbsorberState()


class _EscapedThreadState:
    """Tracked threads that outran ``reset_state``'s join budget (litclock-dev#786).

    ``escapes`` is ``(test nodeid, thread names)`` per occurrence, so the
    report names the test that LEAKED rather than the one that later died.
    That attribution is the whole value: the litclock-dev#769 symptom was exit
    143 in a different file, with nothing pointing back.
    """

    def __init__(self) -> None:
        self.escapes: list[tuple[str, tuple[str, ...]]] = []


_ESCAPED_THREADS = _EscapedThreadState()

# How many leaking tests the summary line names before it folds the rest into
# a "(+N more)". One name, so the three places the line depends on it cannot
# drift apart (litclock-dev#840 — it was the literal 5 written three times in one
# expression).
_ESCAPE_REPORT_LIMIT = 5


def escaped_threads_summary_line(state: _EscapedThreadState) -> str | None:
    """The reported text for ``state``, or None when nothing escaped.

    A pure function of the state, for the same reason
    ``absorber_summary_line`` is: tests call it with their OWN state object
    rather than mutating the module singleton.
    """
    if not state.escapes:
        return None
    shown = state.escapes[:_ESCAPE_REPORT_LIMIT]
    hidden = len(state.escapes) - len(shown)
    detail = "; ".join(f"{nodeid} -> {', '.join(names)}" for nodeid, names in shown)
    more = f" (+{hidden} more)" if hidden else ""
    return (
        f"[litclock-dev#786] {len(state.escapes)} test(s) left a tracked background thread "
        f"running past reset_state()'s join budget: {detail}{more}. This is the shape that "
        "produces a later, unrelated-looking `Terminated` / exit 143 — see litclock-dev#769. "
        "The run itself was not failed by this; the SIGTERM absorber is what keeps it green."
    )


@pytest.fixture(scope="session", autouse=True)
def _absorb_stray_sigterm():
    """Stop the test suite killing itself (litclock-dev#769).

    CI died intermittently with `Terminated` / exit 143 partway through
    ``test_wifi_retry_flow.py``. That is SIGTERM, not a hang — and the sender
    was the suite itself.

    ``setup_server._schedule_self_terminate()`` calls
    ``os.kill(os.getpid(), signal.SIGTERM)``. In production that is correct: the
    setup server terminates itself after the WiFi handoff so first-boot can
    proceed. Tests monkeypatch ``os.kill`` around it, and ``reset_state()``
    joins the background threads so none outlives its test — but that helper
    used to share ONE budget between a flag-drain and the join, so under load
    the drain consumed it and the join got ``timeout=0``. A connect thread then
    escaped, and fired the REAL ``os.kill`` after monkeypatch teardown.

    That root cause is FIXED (litclock-dev#781): the drain and the joins take
    separate budgets, and the join budget is handed out as an EQUAL SLICE per
    thread rather than first-come — a fresh-but-first-come budget merely moved
    the ``timeout=0`` to the second thread, reachable with the drain not stuck
    at all. ``test_sigterm_absorber.py`` —
    ``test_reset_state_gives_every_tracked_thread_a_share_of_its_own_budget`` —
    pins all three properties deterministically against the real
    ``reset_state``.

    This fixture stays as defence in depth. An equal share makes an escape far
    less likely, not impossible: any thread can outlive a FINITE slice, so the
    absorber still covers the residue. It is no longer masking a known,
    unfixed race.

    This is DEV-ONLY. ``reset_state()`` is called from tests and nowhere else
    (its own docstring: "Not used by production code paths"), and the failure
    needs a torn-down ``os.kill`` monkeypatch, which exists only under pytest.
    No device is affected — a clock's self-terminate does exactly what it should.

    So the fix is dev-only too: absorb SIGTERM for the session rather than put
    test-isolation state on a production code path. The trade-off, stated
    plainly: a SIGTERM that CI genuinely means — a cancelled job, a timeout —
    is absorbed as well, so the run continues until CI escalates to SIGKILL.
    That is acceptable because a killed run is already a discarded run, whereas
    the failure this prevents discards a run that was about to pass.

    Anything absorbed is reported through ``pytest_terminal_summary``, NOT
    through ``print`` — pytest's default fd-level capture swallows stderr from a
    passing test, so a printed line would be invisible on exactly the green runs
    where this fires. Masking a fault must not also hide it.
    """
    import signal

    state = _SIGTERM_ABSORBER

    def _handler(signum, frame):
        state.absorbed.append(signum)

    try:
        previous = signal.signal(signal.SIGTERM, _handler)
    except ValueError:
        # Not the main thread (xdist workers can be) — nothing to install.
        # Say so loudly: silently degrading to "no protection" is the same
        # failure mode as the inert pytest-timeout config (litclock-dev#769
        # item 2), where a setting that had stopped doing anything still read
        # as covered.
        warnings.warn(
            "litclock-dev#769: could not install the stray-SIGTERM absorber "
            "(not the main thread). SIGTERM keeps its default disposition, so a "
            "background thread that outlives its test can still kill this run "
            "with exit 143.",
            pytest.PytestWarning,
            stacklevel=1,
        )
        yield state
        return

    state.installed = True
    state.handler = _handler
    try:
        yield state
    finally:
        signal.signal(signal.SIGTERM, previous)
        state.installed = False
        state.handler = None


def absorber_summary_line(state):
    """The reported text for ``state``, or None when there is nothing to report.

    A pure function of the state, deliberately: tests call it with their OWN
    state object instead of swapping fields on the module singleton. The
    installed handler closes over the singleton, so a genuine stray landing
    during such a swap would append to the throwaway list and vanish — the one
    place the mask could hide the very fault it exists to surface (/review).
    """
    if not state.stray:
        return None
    return (
        f"[litclock-dev#769] absorbed {state.stray} stray SIGTERM(s) this run — the run was "
        "NOT killed. Most likely a background thread outlived its test and fired the real "
        "os.kill after monkeypatch teardown, i.e. reset_state()'s join budget lost a race. "
        "The absorber cannot tell that apart from a SIGTERM someone genuinely sent (a "
        "cancelled CI job, an operator's `timeout -s TERM`, a supervisor shutting the run "
        "down), so read it as "
        "'something signalled this run', not as proof of the race."
    )


def pytest_terminal_summary(terminalreporter):
    """Surface the absorber's findings where they survive capture.

    The whole reason litclock-dev#769 went undiagnosed is that exit 143 says
    nothing about which thread sent it. Reporting is therefore not decoration —
    it is the only channel by which the masked fault can still be seen, so it
    must not go through captured stdout/stderr.
    """
    line = absorber_summary_line(_SIGTERM_ABSORBER)
    if line is not None:
        terminalreporter.write_line(line, yellow=True, bold=True)
    # litclock-dev#786 — same channel, same reason: pytest's fd-level capture
    # discards stderr from a PASSING test, and a test that leaks a thread
    # passes. print() would be swallowed exactly when it matters.
    escaped_line = escaped_threads_summary_line(_ESCAPED_THREADS)
    if escaped_line is not None:
        terminalreporter.write_line(escaped_line, yellow=True, bold=True)
