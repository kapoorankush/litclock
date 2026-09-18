"""The stray-SIGTERM absorber does what it claims (litclock-dev#769).

A session fixture that only fires on a rare race is indistinguishable from one
that is broken — it never runs, so it never proves anything. These force the
condition.

Nothing here sends a real SIGTERM without first proving the absorber is
installed. The fixture has a graceful-degradation path that installs no
handler; an unguarded ``os.kill`` on that path would end the run with
``Terminated`` / exit 143 — manufacturing, from this file, the exact CI
signature the fixture exists to remove.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from tests.conftest import (
    _SIGTERM_ABSORBER,
    _EscapedThreadState,
    _SigtermAbsorberState,
    absorber_summary_line,
    escaped_threads_summary_line,
    pytest_terminal_summary,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_the_handler_is_installed_during_the_session(_absorb_stray_sigterm):
    """The session fixture is autouse, so by the time any test runs SIGTERM must
    already be OURS — not Python's default (which terminates the process), and
    not some other callable.

    Identity, not callability: "something callable is installed" is satisfied by
    any foreign handler a plugin or a test-that-forgot-to-restore might leave
    behind, which would read as green with the absorber gone.
    """
    state = _absorb_stray_sigterm
    if state is not _SIGTERM_ABSORBER:
        pytest.fail("the fixture must expose the module-level singleton")
    assert state.installed, (
        "the absorber reports itself NOT installed — SIGTERM keeps its default "
        "disposition and a thread that outlives its test kills the run (litclock-dev#769)"
    )
    current = signal.getsignal(signal.SIGTERM)
    assert current is state.handler, (
        f"SIGTERM is handled by {current!r}, which is not the absorber's own handler "
        f"({state.handler!r}). Something clobbered it mid-session"
    )


def test_a_stray_sigterm_does_not_kill_the_run(_absorb_stray_sigterm):
    """The actual property, exercised: send ourselves the exact signal the
    escaped thread sends, and assert the handler COUNTED it.

    Surviving is not enough on its own — a handler that absorbs but records
    nothing would leave the reporting path (the only visible evidence this bug
    ever happened) silently dead. Asserting the count moves with the send is
    what makes that mutation red.
    """
    state = _absorb_stray_sigterm
    if not state.installed or signal.getsignal(signal.SIGTERM) is not state.handler:
        pytest.fail(
            "refusing to send a real SIGTERM: the absorber is not installed, so this "
            "would kill the run instead of failing it (litclock-dev#769)"
        )

    before = len(state.absorbed)
    state.deliberate += 1
    os.kill(os.getpid(), signal.SIGTERM)

    # CPython runs the Python-level handler at the next bytecode boundary, so
    # this normally does not spin at all; the bound is belt, not expectation.
    deadline = time.monotonic() + 5.0
    while len(state.absorbed) == before and time.monotonic() < deadline:
        time.sleep(0.01)

    assert len(state.absorbed) == before + 1, (
        "still alive, but the absorber did not record the signal — the handler ran "
        "without counting, or something else swallowed SIGTERM"
    )
    assert state.absorbed[-1] == signal.SIGTERM


class _RecordingReporter:
    """Stand-in for pytest's TerminalReporter — records what would be printed."""

    def __init__(self):
        self.lines: list[str] = []

    def write_line(self, line, **markup):
        self.lines.append(line)


def test_the_hook_itself_stays_silent_when_every_signal_was_deliberate(_absorb_stray_sigterm):
    """The HOOK's own guard, not just the pure function underneath it.

    Round 3 moved the reporting tests off ``pytest_terminal_summary`` and onto
    ``absorber_summary_line`` to close a swap window — and took the hook's
    ``if line is not None`` branch out of coverage with them (/review). Two
    mutants that had been dead came back to life: writing unconditionally
    (a bare ``None`` printed every run) and computing the text from the RAW
    absorbed count, which would announce a stray SIGTERM on every single run —
    the exact cry-wolf property the CHANGELOG claims is guarded.

    This drives the real hook against the LIVE singleton and asserts SILENCE.
    It makes its own deliberate send first, so ``stray`` is 0 by construction
    regardless of what order the file's tests run in.

    It is NOT read-only, and an earlier version of this docstring wrongly said
    so (/review): it increments ``deliberate`` on the live singleton and fires a
    real SIGTERM. The residual window is that POSIX coalesces pending SIGTERMs —
    two delivered while blocked invoke the handler once — so a genuine stray
    arriving between the increment and delivery could merge into the deliberate
    one and be discounted. Microseconds wide, and the same shape as
    ``test_a_stray_sigterm_does_not_kill_the_run``, which has always sent one;
    what round 3 removed was a much wider window where tests swapped fields ON
    the singleton for the whole duration of a call. Stated rather than claimed
    away.
    """
    state = _absorb_stray_sigterm
    if not state.installed or signal.getsignal(signal.SIGTERM) is not state.handler:
        pytest.fail("refusing to send a real SIGTERM: the absorber is not installed")

    before = len(state.absorbed)
    state.deliberate += 1
    os.kill(os.getpid(), signal.SIGTERM)
    deadline = time.monotonic() + 5.0
    while len(state.absorbed) == before and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(state.absorbed) == before + 1, (
        f"expected exactly one more absorbed signal, saw {len(state.absorbed) - before}. Either "
        "the deliberate send was not absorbed, or a genuine stray landed during this test — the "
        "session summary at the end of this run distinguishes them"
    )
    assert state.stray == 0, (
        f"a genuine stray SIGTERM landed during this run ({state.stray}) — this test cannot "
        "distinguish the hook staying silent from it correctly reporting that"
    )

    reporter = _RecordingReporter()
    pytest_terminal_summary(reporter)
    # SCOPED to the absorber's own line (/review). The hook writes TWO
    # independent lines: this one, and the litclock-dev#786 escaped-thread
    # report. Asserting on every line the hook emits made an unrelated thread
    # leak in an EARLIER test fail this one and blame the SIGTERM absorber —
    # order-dependent, and misattributed. conftest's design is explicit that an
    # escape must be reported without failing the run ("the run itself was not
    # failed by this"), so folding it into a pytest.fail here contradicts it.
    # Match the PREFIX, not a substring: the escaped-thread line's body also
    # mentions litclock-dev#769 (it explains that a leak is what produces the
    # exit-143 symptom), so a substring filter would still match it. Both lines
    # open with their own bracketed tag.
    absorber_lines = [ln for ln in reporter.lines if ln.startswith("[litclock-dev#769]")]
    if absorber_lines:
        # pytest.fail, not assert: this is the SOLE kill for both hook mutants,
        # and a bare assert is stripped under `PYTHONOPTIMIZE=1 --assert=plain`
        # together (/review measured 7 of 12 mutants going green that way).
        pytest.fail(
            "the terminal-summary hook wrote a line when every absorbed signal was deliberate. "
            f"It would cry wolf on every run: {absorber_lines!r}"
        )


def test_without_the_absorber_the_same_signal_is_fatal():
    """The control. Without this, the test above could pass on a platform where
    SIGTERM was already harmless, and would prove nothing about the fixture.

    Run in a CHILD so the default disposition is real, and assert the child
    dies by SIGTERM (-15).
    """
    child = subprocess.run(
        [sys.executable, "-c", textwrap.dedent("""
            import os, signal, time
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(5)
            print("SURVIVED")
        """)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == -signal.SIGTERM, (
        f"a default-disposition process did NOT die of SIGTERM (rc={child.returncode}). "
        "The sibling test above therefore proves nothing about the absorber"
    )
    assert "SURVIVED" not in child.stdout


def test_a_stray_signal_is_reported_where_capture_cannot_eat_it():
    """The evidence has to survive a GREEN run.

    pytest's default fd-level capture discards stdout/stderr from a passing
    test, and the escaped thread's SIGTERM lands during some later test that
    passes — so a ``print`` would be invisible on precisely the runs that
    matter, turning a loud exit-143 failure into a completely silent one.
    ``pytest_terminal_summary`` writes through the terminal reporter, which is
    not captured — and this file's sibling child-pytest test proves that hook
    is still wired.

    Called with its own state, never by swapping fields onto the live
    singleton: the installed handler closes over that singleton, so a real
    stray arriving mid-swap would be appended to the throwaway list and lost.
    """
    state = _SigtermAbsorberState()
    state.absorbed.extend([signal.SIGTERM, signal.SIGTERM])
    state.deliberate = 1

    line = absorber_summary_line(state)
    assert line, "a stray SIGTERM produced no summary line at all"
    assert "litclock-dev#769" in line
    assert " 1 stray SIGTERM" in line, (
        f"the report must count only the STRAY signals (2 absorbed - 1 deliberate); got: {line!r}"
    )


def test_the_absorbers_own_test_signal_is_not_reported_as_evidence():
    """Cry-wolf guard. ``test_a_stray_sigterm_does_not_kill_the_run`` sends one
    on purpose every single run; if that counted as evidence the report would
    fire always and mean nothing.
    """
    state = _SigtermAbsorberState()
    state.absorbed.append(signal.SIGTERM)
    state.deliberate = 1
    assert state.stray == 0
    assert absorber_summary_line(state) is None, (
        f"a deliberate signal was reported as evidence: {absorber_summary_line(state)!r}"
    )


class _JoinRecordingThread(threading.Thread):
    """A tracked thread that records the timeout every ``join`` is given.

    Timing assertions on a loaded CI box are flaky; the timeout VALUE is not.
    Recording it turns "the join got no budget" from an inference about
    wall-clock into a direct observation.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.join_timeouts: list[float | None] = []
        # monotonic() at each join CALL. The first one marks the end of the
        # drain loop, which is otherwise unobservable from outside reset_state.
        self.join_called_at: list[float] = []

    def join(self, timeout=None):
        self.join_timeouts.append(timeout)
        self.join_called_at.append(time.monotonic())
        return super().join(timeout)


# Wall-clock slack for the drain-loop ceiling in the test below, as a FRACTION
# of the budget. Strictly under 1.0 so a drain given 2x its budget (the
# smallest plausible wrong multiplier) is red with the flag stuck whatever the
# budget is set to -- an absolute slack could be silently outgrown by lowering
# `budget`. At budget=1.0 that is 0.9s, ~90x the loop's natural overshoot.
_DRAIN_OVERRUN_SLACK = 0.9


def test_reset_state_gives_every_tracked_thread_a_share_of_its_own_budget():
    """litclock-dev#781: no tracked thread may be joined with ``timeout=0``
    while budget remains.

    ``reset_state`` used to compute ONE ``deadline`` and spend it on both the
    ``WIFI_CONNECT_IN_FLIGHT`` drain loop and every ``t.join(...)``. With the
    flag stuck the drain burned it all and each join got ``timeout=0`` -- a
    tracked thread then survived the reset and later fired the real ``os.kill``
    after ``monkeypatch`` had restored it, which is how CI runs died at
    ``Terminated`` / exit 143 partway through ``test_wifi_retry_flow.py``.

    The first fix gave the joins their own budget but kept it first-come, which
    RELOCATED the defect to the second thread: the /review adversarial pass
    reproduced ``timeout=0`` with the drain not stuck at all, and since the
    retention filter deliberately keeps a live thread across resets, one stuck
    thread starved the tail forever. So this pins THREE properties at once, and
    a mutant that satisfies any two still goes red:

      a. the join budget is FRESH, not the drain's leftovers;
      b. it is SHARED among the threads, not minted per thread (which would
         make the total scale with ``len(_BG_THREADS)``);
      c. the share is EQUAL, not first-come, so no thread is starved.

    Asserted on the recorded timeout VALUES rather than on ``is_alive()``: the
    probes outlive any finite budget either way, so survival discriminates
    nothing. Wall-clock is bounded from BELOW as well as above, because
    without a floor the test never checks its own premise -- neutering the
    drain loop made an earlier version pass while proving nothing.
    """
    setup_server = pytest.importorskip("setup_server")

    budget = 1.0
    n_probes = 2
    # The drain ceiling below is budget + slack; a 2x drain lands at 2*budget,
    # so the slack must stay under one budget or that mutant goes green.
    drain_slack = budget * _DRAIN_OVERRUN_SLACK
    assert drain_slack < budget, "the drain slack must be strictly under one budget"
    share = budget / n_probes
    release = threading.Event()
    started = [threading.Event() for _ in range(n_probes)]

    def _outlives_the_budget(index):
        started[index].set()
        # Untimed on purpose. A timed wait is a cliff: if more than the timeout
        # elapsed between start() and reset_state(), probe[0] would exit early,
        # its join would return instantly, and the failure below would accuse
        # the wrong thing. The finally always releases, and these are daemons.
        # (Pre-litclock-dev#785 this comment also warned against waiting on a
        # module-level _BG_CANCEL Event, which was inert; that Event is gone.)
        release.wait()

    threads = []
    previous_in_flight = setup_server.WIFI_CONNECT_IN_FLIGHT
    try:
        # Everything below rests on the probes being the ONLY live tracked
        # threads: slices are handed out in registry order, so a live thread
        # left behind by an earlier test would take one and shift every number
        # here. reset_state RETAINS live threads across resets by design, so
        # this is reachable -- and without this guard the symptom is the
        # litclock-dev#781 assertion firing on a healthy tree, accusing the
        # fix under review. Three independent /review passes flagged it.
        with setup_server._BG_THREADS_LOCK:
            leftovers = [t.name for t in setup_server._BG_THREADS if t.is_alive()]
        if leftovers:
            pytest.fail(
                f"a prior test left live tracked threads {leftovers}; they would take a "
                "share of this test's join budget and make the assertions below fire "
                "spuriously. This is NOT a litclock-dev#781 regression -- find the leak"
            )

        for i in range(n_probes):
            probe = _JoinRecordingThread(
                target=_outlives_the_budget,
                args=(i,),
                name=f"litclock-dev781-probe-{i}",
                daemon=True,
            )
            # Same order as _spawn_bg: append and start under the lock, so
            # reset_state can never snapshot an appended-but-unstarted thread.
            # Inside the try, so the finally releases them however this exits --
            # left outside, a failure here leaked two live registered threads
            # into every later test (measured: 3.1s -> 51.2s for one file).
            with setup_server._BG_THREADS_LOCK:
                setup_server._BG_THREADS.append(probe)
                probe.start()
            threads.append(probe)
        for i, event in enumerate(started):
            # Per-probe events: one shared Event is satisfied by whichever probe
            # runs first and says nothing about the other.
            if not event.wait(5):
                pytest.fail(f"probe {i} never started")

        setup_server.WIFI_CONNECT_IN_FLIGHT = True  # stuck: the drain cannot win
        started_at = time.monotonic()
        setup_server.reset_state(wait_for_inflight=budget)
        elapsed = time.monotonic() - started_at

        for i, probe in enumerate(threads):
            if not probe.join_timeouts:
                pytest.fail(f"reset_state never joined probe {i} at all")
        first = threads[0].join_timeouts[0]
        second = threads[1].join_timeouts[0]

        # pytest.fail throughout, not assert: each of these is the SOLE kill for
        # its mutant, and with `PYTHONOPTIMIZE=1` AND `--assert=plain` together a
        # bare assert is stripped. Same convention as this file's siblings.
        if first is None or first < share * 0.8:
            pytest.fail(
                f"probe[0]'s join was given {first!r}s, under the {share}s share of a "
                f"{budget}s budget. The drain was stuck for its whole budget, so a value "
                "at or near zero means the join is still spending the drain's leftovers"
            )
        if first > share * 1.2:
            pytest.fail(
                f"probe[0]'s join was given {first!r}s against a {share}s share. A full "
                f"{budget}s means each thread is minting its own budget, so the total now "
                "scales with len(_BG_THREADS) instead of staying bounded"
            )
        if second is None or second < share * 0.5:
            pytest.fail(
                f"probe[0] was given {first!r}s and probe[1] only {second!r}s. probe[0] "
                "blocks for its whole slice, so a starved probe[1] means the budget is "
                "handed out first-come rather than as an equal share -- litclock-dev#781 "
                "relocated to the second thread rather than fixed"
            )

        # The premise, not a detail: without a floor, neutering the drain loop
        # leaves every value above trivially correct and the test proves nothing.
        # Load only pushes elapsed UP, so a floor is safe on a busy CI box.
        if elapsed < budget * 1.5:
            pytest.fail(
                f"reset_state returned in {elapsed:.2f}s against a {budget}s budget. The "
                "drain did not spend its own budget, so the join values above prove "
                "nothing about the two being separate"
            )

        # "Bounded" is asserted on what reset_state HANDED OUT, not on the clock
        # (litclock-dev#840: the old ceiling was `elapsed > 3 * budget` around an expected
        # 2.0s, ~1s of slack for a loaded runner). The join half is fully
        # observable: the timeouts it gave the probes must sum to at most one
        # budget, whatever n_probes is. A join budget minted per thread, or
        # doubled, shows up here deterministically; load cannot move a recorded
        # value.
        total_join = sum(t.join_timeouts[0] or 0.0 for t in threads)
        if total_join > budget * 1.01:
            pytest.fail(
                f"reset_state handed out {total_join:.2f}s of join timeouts against a "
                f"{budget}s budget ({[t.join_timeouts[0] for t in threads]}). The join half "
                "is no longer bounded by one shared budget"
            )
        # The drain half has no recorded value, so it IS a clock measurement --
        # but of the drain alone, ended by the first join call, not of the whole
        # reset with two join returns on top. The flag is stuck, so the drain
        # cannot finish EARLY: the smallest wrong multiplier, 2x, lands at >= 2.0s
        # and stays red for any slack under 1.0s. The slack is for scheduling
        # latency only; the loop's own overshoot is one 10ms sleep. This IS
        # still a wall-clock ceiling, by design: the drain has no recorded
        # value to assert on short of giving reset_state a controlled clock,
        # which is a src/ change and out of scope here.
        drain_elapsed = threads[0].join_called_at[0] - started_at
        if drain_elapsed > budget + drain_slack:
            pytest.fail(
                f"the drain ran {drain_elapsed:.2f}s against a {budget}s budget "
                f"(> budget + {drain_slack:.2f}s slack) — it is no longer bounded by "
                "wait_for_inflight"
            )

        for probe in threads:
            if probe not in setup_server._BG_THREADS:
                pytest.fail(
                    f"{probe.name} outran its join slice but was dropped from _BG_THREADS "
                    "— the is_alive() retention filter is gone, so no later reset can "
                    "join it and it can still fire os.kill into a later test"
                )
    finally:
        release.set()
        for probe in threads:
            probe.join(10)
        setup_server.WIFI_CONNECT_IN_FLIGHT = previous_in_flight
        setup_server.reset_state()


# The child half of test_the_summary_line_really_reaches_the_terminal. Under the
# env flag it simulates the actual bug — a signal nobody declared — by sending
# WITHOUT incrementing `deliberate`. Skipped in a normal run, so the suite never
# reports a stray it manufactured itself.
_FORCE_STRAY = "LITCLOCK_TEST_FORCE_STRAY_SIGTERM"
_FORCE_AUTOUSE_CHECK = "LITCLOCK_TEST_CHECK_AUTOUSE"


def test_forced_stray_signal_child_half(_absorb_stray_sigterm):
    if not os.environ.get(_FORCE_STRAY):
        pytest.skip(f"child half; set {_FORCE_STRAY}=1 to run")
    state = _absorb_stray_sigterm
    # NOT a bare assert: with `PYTHONOPTIMIZE=1` AND `--assert=plain` TOGETHER
    # an assert is stripped, and the very next statement sends a real SIGTERM —
    # the run dies at rc=-15 (measured on the pre-fix tree). Neither flag alone
    # does it: pytest's assertion rewriting emits `if`/`raise`, not `assert`, so
    # it survives -O. Its sibling guards use pytest.fail for the same reason.
    if not state.installed or signal.getsignal(signal.SIGTERM) is not state.handler:
        pytest.fail("refusing to send a real SIGTERM: the absorber is not installed")
    before = len(state.absorbed)
    os.kill(os.getpid(), signal.SIGTERM)  # NOT declared: this is the bug's shape
    deadline = time.monotonic() + 5.0
    while len(state.absorbed) == before and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(state.absorbed) == before + 1


def test_the_summary_line_really_reaches_the_terminal():
    """Wiring, not just behaviour.

    The direct-call tests above prove `pytest_terminal_summary` produces the
    right line; they cannot prove **pytest calls it**. A hook that stopped being
    collected — moved out of a conftest, renamed, shadowed — would leave those
    green and the evidence channel dead, which is the whole failure this design
    exists to avoid. So: run a REAL child pytest against this repo's real
    conftest, force one undeclared signal, and read the terminal output.
    """
    child = subprocess.run(
        [sys.executable, "-m", "pytest", f"{__file__}::test_forced_stray_signal_child_half", "-q"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, _FORCE_STRAY: "1"},
    )
    if child.returncode != 0:
        pytest.fail(
            f"the child run did not pass (rc={child.returncode})\n{child.stdout}\n{child.stderr}"
        )
    if "absorbed 1 stray SIGTERM" not in child.stdout:
        pytest.fail(
            "pytest did not emit the absorber's terminal-summary line. The hook produces the "
            "right text when called directly, so it is the WIRING that is broken — and with it "
            f"the only channel by which a masked litclock-dev#769 race stays visible.\n{child.stdout}"
        )


def test_autouse_child_half():
    """Deliberately does NOT request ``_absorb_stray_sigterm``.

    That is the whole point: every other test here requests the fixture, which
    INSTALLS it on demand, so none of them can observe its absence. Dropping
    ``autouse=True`` reverts the fix for all ~4100 tests while this file stays
    green — measured (/review). Only a test that never asks for the fixture can
    see whether pytest applied it unasked.
    """
    if not os.environ.get(_FORCE_AUTOUSE_CHECK):
        pytest.skip(f"child half; set {_FORCE_AUTOUSE_CHECK}=1 to run")
    # pytest.fail, not assert: under `PYTHONOPTIMIZE=1` AND `--assert=plain`
    # together, bare asserts are stripped and this guard goes vacuously green
    # (measured) — the same reason its siblings use pytest.fail.
    if not _SIGTERM_ABSORBER.installed:
        pytest.fail(
            "the absorber is NOT installed in a test that did not request it — `autouse=True` "
            "is gone from the fixture, so SIGTERM keeps its default disposition for the whole "
            "suite and litclock-dev#769 is back"
        )
    current = signal.getsignal(signal.SIGTERM)
    if current is not _SIGTERM_ABSORBER.handler:
        pytest.fail(f"SIGTERM is {current!r}, not the absorber's handler")


def test_the_fixture_is_applied_without_being_requested():
    """`autouse` is the single most load-bearing word in the fixture, and it is
    the one property this file structurally cannot check in-process — asking
    for the fixture is what installs it.

    So: a child pytest, selecting ONLY the test above, which never requests it.
    """
    child = subprocess.run(
        [sys.executable, "-m", "pytest", f"{__file__}::test_autouse_child_half", "-q"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, _FORCE_AUTOUSE_CHECK: "1"},
    )
    if child.returncode != 0:
        pytest.fail(
            "a test that does not request the absorber ran without it. `autouse=True` is the "
            "only thing applying it to the other ~4100 tests, and losing it reverts "
            f"litclock-dev#769 silently.\n{child.stdout}\n{child.stderr}"
        )
    if "1 passed" not in child.stdout:
        pytest.fail(f"the child half did not run: {child.stdout}")


# --------------------------------------------------------------------------
# litclock-dev#786 — an escape must be visible, and a thread appended DURING
# the reset must still be joined.
# --------------------------------------------------------------------------


def test_thread_appended_during_the_reset_is_still_joined():
    """litclock-dev#786 finding 2.

    ``reset_state`` used to snapshot ``_BG_THREADS`` once under the lock and
    join only that list. A request thread could set ``WIFI_CONNECT_IN_FLIGHT``
    and append a worker AFTER the drain and AFTER the snapshot; the reset then
    returned having never joined it, and cleared the globals out from under a
    live writer — reopening the duplicate-connect and late-``os.kill`` shapes.

    The append is timed deterministically rather than raced: the appending
    thread is ITSELF tracked, so ``reset_state`` is blocked joining it at the
    moment it appends. The new thread therefore always lands after the first
    snapshot, which is precisely the window the bug lived in.

    Two independent properties go red on the single-snapshot mutant, so this
    cannot pass by luck: the appended thread must have COMPLETED by the time
    ``reset_state`` returns, and the returned escape tuple must be empty.
    """
    setup_server = pytest.importorskip("setup_server")

    second_done = threading.Event()

    def _second():
        # Long enough that a reset which does NOT join it demonstrably returns
        # first, short enough to sit well inside the 2.0s budget.
        time.sleep(0.4)
        second_done.set()

    def _first():
        setup_server._spawn_bg(_second, name="probe-appended-during-reset")

    previous_in_flight = setup_server.WIFI_CONNECT_IN_FLIGHT
    try:
        setup_server.WIFI_CONNECT_IN_FLIGHT = False
        setup_server._spawn_bg(_first, name="probe-appender")

        escaped = setup_server.reset_state(wait_for_inflight=2.0)

        assert second_done.is_set(), (
            "reset_state returned before the thread appended during its own join had finished — "
            "it joined only its first snapshot (litclock-dev#786 finding 2)"
        )
        assert escaped == (), f"nothing should have escaped, got {escaped!r}"
        with setup_server._BG_THREADS_LOCK:
            assert setup_server._BG_THREADS == []
    finally:
        setup_server.WIFI_CONNECT_IN_FLIGHT = previous_in_flight
        setup_server.reset_state(wait_for_inflight=2.0)


def test_reset_state_reports_an_escape_instead_of_returning_none():
    """litclock-dev#786 finding 1.

    ``reset_state`` returned ``None``, so an escaped thread left no signal at
    all: the suite went green and the consequence landed later, in a different
    test, as ``Terminated`` / exit 143 with nothing pointing back. It now
    returns the escapees' names.
    """
    setup_server = pytest.importorskip("setup_server")

    release = threading.Event()

    def _outlives_the_budget():
        release.wait()

    previous_in_flight = setup_server.WIFI_CONNECT_IN_FLIGHT
    try:
        setup_server.WIFI_CONNECT_IN_FLIGHT = False
        setup_server._spawn_bg(_outlives_the_budget, name="probe-escapee")

        escaped = setup_server.reset_state(wait_for_inflight=0.05)

        assert escaped == ("probe-escapee",), (
            f"an escaped thread must be NAMED in the return value, got {escaped!r}"
        )
    finally:
        release.set()
        setup_server.WIFI_CONNECT_IN_FLIGHT = previous_in_flight
        setup_server.reset_state(wait_for_inflight=2.0)


def test_escaped_threads_summary_line_is_silent_when_nothing_escaped():
    assert escaped_threads_summary_line(_EscapedThreadState()) is None


def test_escaped_threads_summary_line_names_the_leaking_test():
    """The attribution is the value. litclock-dev#769's symptom was exit 143 in
    an unrelated file; a report that named only the count would not have helped."""
    state = _EscapedThreadState()
    state.escapes.append(("tests/test_wifi_retry_flow.py::test_x", ("setup-wifi-connect",)))
    line = escaped_threads_summary_line(state)
    assert line is not None
    assert "tests/test_wifi_retry_flow.py::test_x" in line
    assert "setup-wifi-connect" in line
    assert "litclock-dev#786" in line


def test_escaped_threads_summary_line_truncates_a_flood():
    from tests.conftest import _ESCAPE_REPORT_LIMIT

    hidden = 4
    total = _ESCAPE_REPORT_LIMIT + hidden
    state = _EscapedThreadState()
    for i in range(total):
        state.escapes.append((f"tests/t.py::test_{i}", ("setup-wifi-connect",)))
    line = escaped_threads_summary_line(state)
    assert line is not None
    assert f"{total} test(s)" in line
    assert f"(+{hidden} more)" in line
    assert f"test_{_ESCAPE_REPORT_LIMIT - 1} " in line, "the last name inside the limit must be shown"
    assert f"test_{_ESCAPE_REPORT_LIMIT} " not in line, "the first name past the limit must be folded"
