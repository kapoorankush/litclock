"""The conftest network guard refuses, records and FAILS (litclock-dev#881).

A guard that only ever sees clean runs is indistinguishable from one that is
broken: it never fires, so it never proves anything. These force the condition.

The load-bearing one is `test_a_background_thread_refusal_fails_an_otherwise_green_run`.
A refusal raised inside a background thread cannot fail the test it came from —
the exception dies with the thread and the test passes — so without the
`pytest_sessionfinish` backstop the whole guard would be a yellow line under a
green run, which is the silent-allow shape litclock-dev#860/litclock-dev#864 catalogued.
That test runs a child pytest whose only test PASSES and asserts the child still
exits non-zero.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests import conftest
from tests.conftest import (
    _ESCAPE_REPORT_LIMIT,
    NetworkAccessAttempted,
    _guarded_getaddrinfo,
    _is_loopback_host,
    _NetworkAttemptState,
    foreign_attempts,
    network_attempts_summary_line,
    pytest_sessionstart,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# The child source for `test_a_refusal_in_teardown_survives_into_the_next_test`,
# held at module level so its own triple quotes do not nest inside the method's
# docstring.
# Child source for the proxy test. The child inherits `http_proxy`; conftest's
# import-time clearing is what must remove it.
CHILD_ASSERTS_NO_PROXY = """
    import os

    def test_the_conftest_cleared_the_inherited_proxy():
        for var in ("http_proxy", "https_proxy", "ftp_proxy", "all_proxy"):
            assert var not in os.environ, var
            assert var.upper() not in os.environ, var.upper()
        assert os.environ.get("no_proxy") == "*"
"""


CHILD_LEAKS_IN_TEARDOWN = """
    import socket, pytest

    @pytest.fixture
    def leaks_on_teardown():
        yield
        try:
            socket.getaddrinfo("ip-api.com", 80)   # the teardown window
        except BaseException:
            pass                                    # swallowed, as the real caller does

    def test_a_leaks_during_its_own_teardown(leaks_on_teardown):
        assert True

    def test_b_runs_after_and_passes():
        assert True
"""




@pytest.fixture
def quarantined_attempts(monkeypatch):
    """Let a test trip the REAL guard without touching the real ledger.

    The guard and both session hooks look `_NETWORK_ATTEMPTS` up on the conftest
    module at call time, so pointing that name at a throwaway state redirects
    every write a quarantined test causes — including `pytest_sessionstart`'s
    `clear()`, which the hook tests below call for real.

    Two earlier versions were wrong in the same direction, both found by review.
    Snapshot-and-restore overwrote the ledger wholesale, discarding any refusal
    an unrelated escaped thread recorded meanwhile. Filtering the restore by
    nodeid did not help: the guard attributes to the test CURRENTLY RUNNING, so a
    foreign thread's refusal during a quarantined test carries this test's nodeid
    too and was deleted just the same. Redirecting the destination removes the
    class — nothing is ever deleted from the real ledger.

    Anything the test did not `expect()` is migrated back afterwards, so a
    genuine leak that lands in the throwaway still reaches session finish.
    """
    real = conftest._NETWORK_ATTEMPTS
    fresh = _NetworkAttemptState()
    expected: set[str] = set()
    fresh.expect = expected.add
    fresh.real = real          # the live ledger, reachable while it is shadowed
    monkeypatch.setattr(conftest, "_NETWORK_ATTEMPTS", fresh)
    yield fresh
    real.attempts.extend(foreign_attempts(fresh.attempts, expected))


class TestTheGuardIsInstalledAndDiscriminates:
    def test_it_is_installed_on_the_socket_module(self):
        """Not a fixture, so it is not subject to the finalizer ordering that
        causes litclock-dev#881 in the first place."""
        assert socket.getaddrinfo is _guarded_getaddrinfo

    def test_loopback_still_resolves(self):
        """The control-server tests bind local servers; blocking these would
        trade a real regression for the one being prevented."""
        assert socket.getaddrinfo("127.0.0.1", 80)
        assert socket.getaddrinfo("localhost", 80)

    @pytest.mark.parametrize(
        "host",
        [
            None, "", "localhost", "LOCALHOST", "localhost.", b"localhost",
            "localhost.localdomain", "ip6-localhost",
            "127.0.0.1", "127.0.0.2", "::1", "0:0:0:0:0:0:0:1", "::1%0",
            "::ffff:127.0.0.1", "0.0.0.0", "::",
        ],
    )
    def test_every_loopback_spelling_is_allowed(self, host):
        """Each of these resolves to this machine, and each would have been
        REFUSED by the frozenset-of-strings the guard first shipped as — a false
        red waiting for the first test that used one (review measured all of
        them). The classifier is `ipaddress`, so the numeric forms are decided
        rather than listed."""
        assert _is_loopback_host(host) is True

    @pytest.mark.parametrize(
        "host", ["8.8.8.8", "1.1.1.1", "ip-api.com", "example.com", "127.1", "127.0.0.1."]
    )
    def test_non_loopback_is_refused_including_literals(self, host):
        """The negative half. Literals matter: `socket.create_connection` passes
        one to `getaddrinfo` too, so a hardcoded IP does NOT slip past on that
        path. `127.1` is `inet_aton` shorthand that IS loopback on this box but
        is deliberately refused — the classifier sticks to forms `ipaddress`
        understands, and refusing is the safe direction.

        `127.0.0.1.` is the one that bit: stripping the trailing dot BEFORE the
        numeric parse made it classify as a numeric loopback, while the resolver
        treats that exact spelling as a NAME needing DNS — approved, forwarded,
        resolved, no ledger entry (review). The guard forwards the original host,
        so the numeric path has to judge the original.
        """
        assert _is_loopback_host(host) is False

    def test_the_guard_writes_to_the_quarantined_state_not_the_real_ledger(self, quarantined_attempts):
        """Redirection, not restoration. The real ledger is never written to and
        so can never be corrupted by a test that trips the guard on purpose."""
        quarantined_attempts.expect("ip-api.com")
        before = len(quarantined_attempts.real.attempts)
        with pytest.raises(NetworkAccessAttempted):
            socket.getaddrinfo("ip-api.com", 80)
        assert len(quarantined_attempts.attempts) == 1, "the refusal went to the throwaway"
        assert len(quarantined_attempts.real.attempts) == before, "the real ledger was not written to"

    def test_the_refusal_is_not_swallowed_by_except_exception(self, quarantined_attempts):
        """`NetworkAccessAttempted` derives from `BaseException` for this reason.

        `geocoding.ip_geolocate` and `location_resolver.resolve_location_from_ip`
        both wrap their work in `except Exception`. As an `AssertionError`
        subclass the refusal was caught, logged "IP geolocation failed" and
        retried down the 1/3/9s ladder — a refusal that failed nothing.
        """
        quarantined_attempts.expect("ip-api.com")
        with pytest.raises(NetworkAccessAttempted):
            try:
                socket.getaddrinfo("ip-api.com", 80)
            except Exception:  # noqa: BLE001 - the swallow being defended against
                pytest.fail("a broad `except Exception` swallowed the refusal")

    def test_no_proxy_is_forced_in_this_process(self):
        """Weak on its own — this box sets no proxy, so it would pass with the
        clearing deleted. `TestItActuallyFailsTheRun` has the one that can fail."""
        assert os.environ.get("no_proxy") == "*"

    def test_a_remote_host_is_refused_and_recorded(self, quarantined_attempts):
        quarantined_attempts.expect("ip-api.com")
        with pytest.raises(NetworkAccessAttempted) as exc:
            socket.getaddrinfo("ip-api.com", 80)
        assert "ip-api.com" in str(exc.value)
        assert len(quarantined_attempts.attempts) == 1
        nodeid, host, thread = quarantined_attempts.attempts[0]
        assert host == "ip-api.com"
        assert "test_a_remote_host_is_refused_and_recorded" in nodeid
        assert thread == "MainThread"

    def test_the_refusal_names_litclock_dev_881_for_the_teardown_case(self, quarantined_attempts):
        """The message has to tell the two shapes apart, because the fix differs:
        a MainThread call is a missing stub, a thread call is the ordering bug."""
        quarantined_attempts.expect("example.invalid")
        with pytest.raises(NetworkAccessAttempted) as exc:
            socket.getaddrinfo("example.invalid", 443)
        assert "litclock-dev#881" in str(exc.value)
        assert "background thread" in str(exc.value)


class TestTheSummaryLine:
    """A pure function of its own state object, like `escaped_threads_summary_line`
    beside it — these pass their OWN state rather than mutating the singleton."""

    def test_it_is_silent_when_nothing_was_refused(self):
        assert network_attempts_summary_line(_NetworkAttemptState()) is None

    def test_it_names_the_test_the_host_and_the_thread(self):
        state = _NetworkAttemptState()
        state.attempts.append(("tests/test_x.py::test_y", "ip-api.com", "setup-wifi-connect"))
        line = network_attempts_summary_line(state)
        assert "tests/test_x.py::test_y" in line
        assert "ip-api.com" in line
        assert "setup-wifi-connect" in line
        assert "The run has been failed." in line

    def test_it_does_not_say_more_at_exactly_the_limit(self):
        """The `hidden == 0` boundary: an off-by-one producing "(+0 more)" would
        otherwise go unnoticed."""
        state = _NetworkAttemptState()
        for i in range(_ESCAPE_REPORT_LIMIT):
            state.attempts.append((f"tests/test_x.py::test_{i}", "ip-api.com", "t"))
        line = network_attempts_summary_line(state)
        assert "more)" not in line
        assert all(f"test_{i}" in line for i in range(_ESCAPE_REPORT_LIMIT))

    def test_it_truncates_a_flood(self):
        state = _NetworkAttemptState()
        for i in range(_ESCAPE_REPORT_LIMIT + 3):
            state.attempts.append((f"tests/test_x.py::test_{i}", "ip-api.com", "t"))
        line = network_attempts_summary_line(state)
        assert "(+3 more)" in line
        assert f"test_{_ESCAPE_REPORT_LIMIT}" not in line


class TestItActuallyFailsTheRun:
    """Forced-condition child runs. `-p tests.conftest` loads the repo's conftest
    as a plugin, so the child gets the import-time patch and the hooks without
    the test file having to live in `tests/`. Verified load-bearing: dropping the
    flag makes the child exit 0, so these cannot pass for the wrong reason.

    The child runs CONFIG-LESS — rootdir is the tmp dir, so `pyproject.toml`'s
    `pythonpath`, `required_plugins` and `timeout` do not apply. That means
    `import setup_server` fails there and the autouse `_reset_setup_server_state`
    early-returns, so no child test may depend on that fixture (review)."""

    @staticmethod
    def _child(tmp_path, body, env=None):
        test_file = tmp_path / "test_child.py"
        test_file.write_text(textwrap.dedent(body))
        child_env = dict(os.environ)
        child_env.update(env or {})
        return subprocess.run(
            [sys.executable, "-m", "pytest", str(test_file), "-p", "tests.conftest", "-q", "-p", "no:cacheprovider"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            env=child_env,
        )

    def test_a_background_thread_refusal_fails_an_otherwise_green_run(self, tmp_path):
        """THE test. The child's only test passes; the run must still be red.

        This is the litclock-dev#881 shape exactly: the resolution happens off
        the main thread, so nothing can fail the test itself.
        """
        r = self._child(tmp_path, '''
            import socket, threading

            def test_passes_while_a_thread_resolves():
                def go():
                    try:
                        socket.getaddrinfo("ip-api.com", 80)
                    except Exception:
                        pass          # swallowed, exactly as the real connect thread does
                t = threading.Thread(target=go)
                t.start()
                t.join()
                assert True
        ''')
        assert "1 passed" in r.stdout, f"the child's test should PASS:\n{r.stdout}\n{r.stderr}"
        assert r.returncode != 0, (
            "the run must be FAILED by the refusal even though every test passed — "
            f"without that backstop the guard is a yellow line under a green suite:\n{r.stdout}"
        )
        assert "litclock-dev#881" in r.stdout and "ip-api.com" in r.stdout

    def test_a_refusal_in_teardown_survives_into_the_next_test(self, tmp_path):
        """THE litclock-dev#881 timing, which the two child runs above cannot reach.

        Both of those resolve inside a single test body, so a regression that
        merely DROPS the ledger between tests keeps them green. Proven in review:
        adding `_NETWORK_ATTEMPTS.attempts.clear()` to `_reset_setup_server_state`
        — the same kind of between-test isolation reset this conftest does
        everywhere else — left the whole file passing while silently allowing
        exactly the leak the guard exists to catch.

        So this child has TWO tests: the first leaks from a fixture FINALIZER,
        which is the teardown window itself, and the second is a trivial pass
        that follows it. Both must pass and the run must still be red.

        Written not to depend on `_reset_setup_server_state`: the child runs
        config-less (see `_child`), so `pythonpath` is unset, `import
        setup_server` raises ModuleNotFoundError and that fixture early-returns.
        """
        r = self._child(tmp_path, CHILD_LEAKS_IN_TEARDOWN)
        assert "2 passed" in r.stdout, f"both child tests should PASS:\n{r.stdout}\n{r.stderr}"
        assert r.returncode != 0, (
            "a refusal recorded in test A's teardown must still fail the run after test B "
            f"has come and gone — otherwise a per-test ledger reset would go unnoticed:\n{r.stdout}"
        )
        assert "litclock-dev#881" in r.stdout and "ip-api.com" in r.stdout

    def test_a_proxy_variable_in_the_environment_is_cleared(self, tmp_path):
        """The proxy bypass, tested where it can actually fail.

        With `http_proxy` pointing at a loopback address, the only name resolved
        is the PROXY's — which is allowlisted — and the request for `ip-api.com`
        goes out through it with no refusal recorded at all. That is the guard's
        whole property defeated by an environment variable, reproduced in review
        on the shape a corporate proxy, mitmproxy or Docker leaves behind.

        The in-process assertion above cannot catch a regression here, because
        this box sets no proxy to begin with. This child is given one.
        """
        r = self._child(
            tmp_path,
            CHILD_ASSERTS_NO_PROXY,
            env={"http_proxy": "http://127.0.0.1:3128", "HTTPS_PROXY": "http://127.0.0.1:3128"},
        )
        assert "1 passed" in r.stdout, f"{r.stdout}\n{r.stderr}"
        assert r.returncode == 0, f"{r.stdout}"

    def test_a_clean_child_run_stays_green(self, tmp_path):
        """The control for the test above: the backstop must not fail runs that
        did not touch the network, or it would be a permanent red."""
        r = self._child(tmp_path, '''
            import socket

            def test_touches_only_loopback():
                assert socket.getaddrinfo("127.0.0.1", 80)
        ''')
        assert "1 passed" in r.stdout, f"{r.stdout}\n{r.stderr}"
        assert r.returncode == 0, f"a clean run must stay green:\n{r.stdout}"
        assert "litclock-dev#881" not in r.stdout


class TestTheSessionStartHook:
    """`pytest_sessionstart` clears the ledger and refuses xdist.

    Both are invisible to every other test here: the ledger only matters across
    two in-process sessions, and xdist is not installed. Called directly with a
    stub session for that reason.
    """

    class _StubPM:
        def __init__(self, xdist):
            self._xdist = xdist

        def hasplugin(self, name):
            return name == "xdist" and self._xdist

    class _StubConfig:
        def __init__(self, xdist, nprocs):
            self.pluginmanager = TestTheSessionStartHook._StubPM(xdist)
            self._nprocs = nprocs

        def getoption(self, name, default=None):
            return self._nprocs if name == "numprocesses" else default

    class _StubSession:
        def __init__(self, xdist=False, nprocs=None):
            self.config = TestTheSessionStartHook._StubConfig(xdist, nprocs)

    def test_it_clears_a_previous_sessions_ledger(self, quarantined_attempts):
        """Process-lifetime state outlives a session when pytest is driven
        in-process, and a refusal from run 1 would otherwise redden run 2 —
        reproduced in review with two `pytest.main()` calls."""
        quarantined_attempts.attempts.append(("stale::test", "ip-api.com", "MainThread"))
        pytest_sessionstart(self._StubSession())
        assert quarantined_attempts.attempts == []

    def test_it_refuses_xdist(self, quarantined_attempts):
        """Under `-n` a worker's exitstatus is snapshotted before ordinary hooks
        run and the controller ignores it, so the backstop is disarmed while the
        red line still prints — the looks-like-it-works shape. Loud beats that."""
        with pytest.raises(pytest.UsageError, match="litclock-dev#881"):
            pytest_sessionstart(self._StubSession(xdist=True, nprocs=4))

    def test_it_allows_a_plain_run(self, quarantined_attempts):
        pytest_sessionstart(self._StubSession(xdist=True, nprocs=None))
        pytest_sessionstart(self._StubSession(xdist=False, nprocs=None))


class TestTheQuarantinePartition:
    """`foreign_attempts` decides what a quarantining test may swallow.

    Tested as a pure function, because the fixture's whole job is to leave no
    trace — asserting the round trip from inside a test that uses it would
    require observing after its own teardown.
    """

    def test_an_expected_main_thread_refusal_is_the_tests_own(self):
        attempts = [("t::a", "ip-api.com", "MainThread")]
        assert foreign_attempts(attempts, {"ip-api.com"}) == []

    def test_a_different_host_belongs_to_the_run(self):
        attempts = [("t::a", "elsewhere.example", "MainThread")]
        assert foreign_attempts(attempts, {"ip-api.com"}) == attempts

    def test_the_same_host_from_another_thread_belongs_to_the_run(self):
        """The case a nodeid filter could not see: the guard attributes to the
        test CURRENTLY RUNNING, so an escaped `setup-wifi-connect` thread
        resolving during a quarantined test carries that test's nodeid too, and
        the earlier filter deleted it (review reproduced the loss)."""
        attempts = [("t::a", "ip-api.com", "setup-wifi-connect")]
        assert foreign_attempts(attempts, {"ip-api.com"}) == attempts

    def test_nothing_expected_means_everything_is_the_runs(self):
        attempts = [("t::a", "ip-api.com", "MainThread")]
        assert foreign_attempts(attempts, set()) == attempts
