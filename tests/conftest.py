"""Shared fixtures for LitClock tests."""

import ipaddress
import json
import os
import shlex
import socket
import subprocess
import textwrap
import threading
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


# --- litclock-dev#881: no unit test may touch the network -------------------
#
# Installed at IMPORT, not as a fixture, and that is the whole point. The bug
# this closes is fixture-ORDERING: `_reset_setup_server_state` below does not
# request `monkeypatch`, so it is set up first and finalized LAST — its
# `reset_state()` drain therefore runs AFTER monkeypatch has restored the real
# `setup_server._resolve_location_from_ip`. A connect thread still draining at
# that moment reaches the LIVE resolver, whatever the test stubbed. A guard that
# is itself a function-scoped fixture would be subject to the same ordering it
# exists to defend against. (A SESSION-scoped one would outlive monkeypatch and
# would also work; import-time simply has no ordering to reason about at all.)
#
# What that late call does is why this is not tidiness. The resolver's success
# path calls `set_system_timezone()` BEFORE the env write, which shells out to
# `sudo /usr/local/lib/litclock/litclock-set-timezone`. That sudo call is
# sandboxed by nothing, so on a Pi — or any box where the installer has run — a
# `pytest tests/` reaching this window can change the SYSTEM TIMEZONE. (It needs
# `setup_server.ENV_FILE` to be set for the resolver to get that far, which a
# fully-restored teardown may not have; the hazard is real but conditional.) It
# is also a live call on a 1/3/9s ladder with a 5s socket timeout — ~33s of
# nominal ladder, not an enforced deadline — against a rate-limited free tier,
# which is the CI-flake mechanism from litclock-dev#876.
#
# litclock-dev#879's finding was that a per-class list of stubs is a list of the
# classes somebody remembered. This is the structural half. It is deliberately
# NOT a replacement for those stubs: a stub says what a test MEANS, this says
# only that no DNS left the process.
#
# SCOPE, stated exactly, because the first draft of this comment got it backwards
# in both halves (review). It covers IN-PROCESS DNS. `socket.create_connection`,
# `http.client`, `urllib` and `requests` all resolve through here — INCLUDING for
# a literal IP, which `create_connection` still passes to `getaddrinfo`, so
# literals do NOT slip past on those paths. What genuinely bypasses it is a raw
# `socket.connect` to a literal, and this tree has two:
# `src/literary_clock.py::_resolve_lan_ip` and `src/control_server/handoff.py`
# both `sock.connect(("1.1.1.1", 80))` on a UDP socket to pick a route. No packet
# leaves for those, and the literary_clock tests stub `socket.socket` — but the
# shape exists, and the earlier claim that "nothing in this tree does that" was
# false. Also outside the guard: SUBPROCESSES. `ScriptSandbox` and every
# bash-script test inherit no Python patch, so a script that shells out to `curl`
# egresses normally. Widening to `socket.connect` would have to tell the loopback
# servers the control-server tests bind apart from real egress, trading a live
# risk of false failures for a hypothetical leak.
#
# Proxies are neutralised rather than left as a hole: with `http_proxy` pointing
# at a loopback address, the only name resolved is the PROXY's, which is
# allowlisted, and the request for `ip-api.com` goes out through it with no
# refusal recorded at all (reproduced in review, on a box with a corporate,
# mitmproxy or Docker proxy variable set). So the vars are cleared here.
for _var in ("http_proxy", "https_proxy", "ftp_proxy", "all_proxy", "no_proxy"):
    os.environ.pop(_var, None)
    os.environ.pop(_var.upper(), None)
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "*"

_REAL_GETADDRINFO = socket.getaddrinfo

# Names that mean this machine. Numeric forms are NOT listed: they are classified
# by `ipaddress` below, because a frozenset of spellings refused `127.0.0.2`,
# `::ffff:127.0.0.1` and `::1%0` — all genuinely loopback here — and would have
# turned any future test using one into a false red (review).
_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})


def _is_loopback_host(host) -> bool:
    """True when `host` cannot leave this machine.

    `None` and `""` are the wildcard-bind forms. Bytes are a valid `getaddrinfo`
    host. On the NAME path a trailing dot is a legal FQDN and case is not
    significant, so both are normalised away there — but not before the numeric
    parse, for the reason given inline below.

    Two forms are refused although they would resolve to loopback, both
    deliberately: `inet_aton` shorthand such as `127.1`, and unicode spellings
    such as a unicode look-alike of `localhost` that the resolver NFKC-folds back
    to the ASCII spelling. Refusing
    is the safe direction — it costs a false red that nothing in this tree
    triggers, where accepting would cost the guarantee.
    """
    if host is None:
        return True
    if isinstance(host, (bytes, bytearray)):
        try:
            host = host.decode("ascii")
        except UnicodeDecodeError:
            return False
    if not isinstance(host, str):
        return False
    raw = host.strip()
    if not raw:
        return True
    try:
        addr = ipaddress.ip_address(raw.split("%", 1)[0])
    except ValueError:
        # The trailing dot is stripped ONLY here, on the name path. Doing it
        # before the numeric parse made `"127.0.0.1."` classify as a numeric
        # loopback while the resolver still treats that exact spelling as a NAME
        # needing DNS — approved, forwarded, and resolved with no ledger entry
        # (reproduced in review). The guard forwards the ORIGINAL host, so the
        # numeric path must judge the original.
        return raw.rstrip(".").casefold() in _LOOPBACK_NAMES
    # `::ffff:127.0.0.1` is loopback but `IPv6Address.is_loopback` is False for
    # it, so unmap first.
    if addr.version == 6 and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_loopback or addr.is_unspecified


class _NetworkAttemptState:
    """Resolutions the guard refused (litclock-dev#881).

    ``attempts`` is ``(test nodeid, host, thread name)`` per occurrence. The
    thread name is carried because it narrows the diagnosis: a refusal from
    ``MainThread`` is usually an unstubbed call in the test body, while one from
    a background thread is usually the litclock-dev#881 teardown window. Neither
    is proof — an unstubbed worker thread looks the same — so read it as a
    pointer, not a verdict.
    """

    def __init__(self) -> None:
        self.attempts: list[tuple[str, str, str]] = []


_NETWORK_ATTEMPTS = _NetworkAttemptState()

# The nodeid a refusal is attributed to: the last test to START, which is the
# test ACTIVE WHEN OBSERVED and not necessarily the one that spawned the thread.
# For a leak that drains inside its own teardown those coincide, which is the
# common case; for a thread that outlives its test — the shape `_ESCAPED_THREADS`
# below exists to track — the name printed is whichever test started next. The
# earlier version of this comment claimed the spawning test, which is wrong.
_CURRENT_NODEID = "<no test running>"


class NetworkAccessAttempted(BaseException):
    """Raised in whichever thread tried to leave the box.

    `BaseException`, not `Exception`, and that is load-bearing: the callers this
    guard is aimed at swallow broadly. `geocoding.ip_geolocate` and
    `location_resolver.resolve_location_from_ip` both wrap their work in
    `except Exception`, so an `AssertionError` subclass was caught, logged as
    "IP geolocation failed" and RETRIED down the 1/3/9s ladder — the refusal
    downgraded to a soft failure that fails nothing (measured in review).
    Deriving from `BaseException` means a main-thread refusal really does fail
    its own test instead of being absorbed by the code under test.
    """


def _guarded_getaddrinfo(host, *args, **kwargs):
    if _is_loopback_host(host):
        return _REAL_GETADDRINFO(host, *args, **kwargs)
    _NETWORK_ATTEMPTS.attempts.append(
        (_CURRENT_NODEID, str(host), threading.current_thread().name)
    )
    raise NetworkAccessAttempted(
        f"a unit test tried to resolve {host!r} (thread {threading.current_thread().name}). "
        "Unit tests must not touch the network: stub the caller. If this came from a "
        "background thread during teardown, it is litclock-dev#881 — the stub was already "
        "restored by monkeypatch while the thread was still draining."
    )


socket.getaddrinfo = _guarded_getaddrinfo


def foreign_attempts(attempts, expected):
    """The refusals in ``attempts`` a quarantining test did NOT ask for.

    Split out as a pure function for the same reason
    ``network_attempts_summary_line`` is: a test can hand it its own lists
    instead of exercising it through a fixture whose whole job is to leave no
    trace, which made the round trip impossible to assert from inside.

    ``expected`` holds hosts the test deliberately tripped; those are its own
    business. Everything else — a different host, or the same host from another
    thread — belongs to the run and is migrated back to the real ledger.
    """
    return [a for a in attempts if a[1] not in expected or a[2] != "MainThread"]


def network_attempts_summary_line(state: _NetworkAttemptState) -> str | None:
    """The reported text for ``state``, or None when nothing was refused.

    A pure function of the state, matching ``escaped_threads_summary_line``
    below and for the same reason: tests call it with their OWN state object
    rather than mutating the module singleton.
    """
    if not state.attempts:
        return None
    shown = state.attempts[:_ESCAPE_REPORT_LIMIT]
    hidden = len(state.attempts) - len(shown)
    detail = "; ".join(f"{nodeid} -> {host} ({thread})" for nodeid, host, thread in shown)
    more = f" (+{hidden} more)" if hidden else ""
    return (
        f"[litclock-dev#881] {len(state.attempts)} network resolution(s) REFUSED during the "
        f"run: {detail}{more}. A call from a non-main thread points at the litclock-dev#881 "
        "teardown window; one from MainThread points at a missing stub. The run has been failed."
    )


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


def pytest_runtest_logstart(nodeid, location):
    """Attribute later refusals to the test that is starting (litclock-dev#881)."""
    global _CURRENT_NODEID
    _CURRENT_NODEID = nodeid


def pytest_sessionstart(session):
    """Start each session with an empty ledger (litclock-dev#881).

    Process-lifetime state outlives a session when pytest is driven in-process
    (`pytest.main()` twice, some IDE runners), and a refusal from run 1 would
    then redden run 2 — a self-inflicted version of the poisoning this guard is
    meant to detect (found in review).

    xdist is refused outright rather than silently tolerated. A worker's
    `exitstatus` is snapshotted before ordinary hooks run and the controller does
    not treat a worker's exit status as a failed test, so under `-n` the backstop
    below is disarmed while still printing its red line — the exact
    looks-like-it-works shape litclock-dev#860/litclock-dev#864 catalogued. Wire
    worker-to-controller reporting before removing this.
    """
    _NETWORK_ATTEMPTS.attempts.clear()
    if session.config.pluginmanager.hasplugin("xdist") and session.config.getoption("numprocesses", None):
        raise pytest.UsageError(
            "litclock-dev#881: the network guard's run-failing backstop does not survive "
            "xdist workers. Wire worker-to-controller reporting before using -n."
        )


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    """Fail the run if anything was refused (litclock-dev#881).

    A refusal raised in a BACKGROUND thread cannot fail the test it came from:
    the exception dies with the thread and the test passes. Reporting alone
    would therefore be the silent-allow shape this repo keeps getting bitten by
    (litclock-dev#860/litclock-dev#864), so the exit status is set here as well. A MAIN
    thread refusal fails its own test on its own — `NetworkAccessAttempted`
    derives from `BaseException` precisely so the code under test cannot swallow
    it. This only converts a green run to red, never the reverse.

    `trylast` so it samples the ledger after other plugins' session hooks have
    run. The residual is honest and unfixable from inside the process: a refusal
    recorded AFTER this hook — a thread that outlives the whole session, or a
    later rung of the 1/3/9s retry ladder spilling past the final test — is
    recorded and then dropped, and the run exits 0. The FIRST refusal of any
    leak is prompt, which is why the common case is caught; a leak starting in
    the session's last test is the one that can escape.
    """
    if _NETWORK_ATTEMPTS.attempts and exitstatus == 0:
        session.exitstatus = 1


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
    # litclock-dev#881 — red, not yellow: unlike the two above, this one has
    # already failed the run in pytest_sessionfinish.
    network_line = network_attempts_summary_line(_NETWORK_ATTEMPTS)
    if network_line is not None:
        terminalreporter.write_line(network_line, red=True, bold=True)
