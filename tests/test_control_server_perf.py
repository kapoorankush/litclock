"""Perf-path tests for #419 PR2 + #433 follow-ups.

Covers:

- ``DIAG_JOURNAL_TIMEOUT_S`` — journalctl-specific 8 s budget (#427).
- ``_read_journal_tail`` call-site invariants (timeout, ttl, key, argv).
- ``log_buffer.MemoryLogHandler.snapshot`` — atomic
  ``(entries, total, latest_seq)`` under one lock acquire. The route now
  calls this in place of three separate lock-takes.
- ``log_buffer.MemoryLogHandler.get_logs(..., order='asc')`` —
  post-filter on the limit-selected newest-N slice. ``limit`` still means
  "newest N" regardless of order (#419 D10).

#433 dropped the module-level ``ThreadPoolExecutor`` pool in favour of a
serial loop, and the empty-units short-circuit (P-1 lazy-tail callers
pass ``()`` when no unit needs a tail). The pool-lifecycle tests that
lived here pre-#433 are gone with the pool itself (~125 LOC).
"""

from __future__ import annotations

import logging
import threading

import pytest

from control_server import log_buffer
from control_server.routes.diagnostics import _collectors


class TestNoJournalPool:
    """#433 explicitly deleted the journal-tail ``ThreadPoolExecutor``.
    Per /review T-4: positively assert the absence so a future refactor
    that re-introduces a pool (even a single-worker one that's effectively
    serial) — with the F10/M1/M2 race-guard surface that came with the
    pre-#433 plumbing — has to update this test to land. The reexport
    contract test pins the PUBLIC-symbol case; this pins module-level
    private state."""

    def test_journal_pool_symbol_does_not_exist(self):
        assert not hasattr(_collectors, "_JOURNAL_POOL")
        assert not hasattr(_collectors, "_get_journal_pool")
        assert not hasattr(_collectors, "_shutdown_journal_pool")
        assert not hasattr(_collectors, "_JOURNAL_POOL_LOCK")
        assert not hasattr(_collectors, "reset_for_tests")


class TestBatchedJournalTails:
    """#433: ``_batched_journal_tails`` is a serial loop. The P-1
    lazy-tail caller in ``_build_service_states`` passes ``()`` on a
    healthy clock; the function must short-circuit without invoking
    ``_read_journal_tail`` even once."""

    def test_empty_units_short_circuits(self):
        # Defensive: empty input is the healthy-clock case and must NOT
        # invoke _read_journal_tail (and therefore must NOT shell out).
        out = _collectors._batched_journal_tails((), n=3)
        assert out == {}

    def test_serial_call_per_unit(self, monkeypatch):
        # Pin the contract: one _read_journal_tail call per unit, in the
        # order the caller passed. A future refactor that batched units
        # via a single multi-unit journalctl invocation (and lost per-
        # unit failure isolation) would fail this.
        called: list[tuple[str, int]] = []

        def fake_read(unit, n):
            called.append((unit, n))
            return [f"line-for-{unit}"]

        monkeypatch.setattr(_collectors, "_read_journal_tail", fake_read)
        units = ("litclock.service", "litclock-control.service")
        out = _collectors._batched_journal_tails(units, n=3)
        assert called == [("litclock.service", 3), ("litclock-control.service", 3)]
        assert out == {
            "litclock.service": ["line-for-litclock.service"],
            "litclock-control.service": ["line-for-litclock-control.service"],
        }


class TestJournalTimeoutBudget:
    """v0.214.2 hotfix regression — the journalctl-specific timeout must
    exceed the fast-call timeout so a slow Pi Zero 2W journal query
    doesn't get killed mid-flight, returning empty stdout, falsely
    tripping the services anomaly + the oxblood "Clock isn't running"
    banner on a healthy clock.

    Caught by authorclock hardware QA: a single
    ``journalctl --no-pager -n 3 -u litclock-control.service -o short-iso``
    clocked at 3.95s on a Pi Zero 2W with a few weeks of journal storage.
    The pre-v0.214.2 3s budget tripped on every call.
    """

    def test_journal_timeout_exceeds_fast_call_timeout(self):
        from control_server.routes.diagnostics._collectors import (
            DIAG_JOURNAL_TIMEOUT_S,
            DIAG_SUBPROC_TIMEOUT_S,
        )

        assert DIAG_JOURNAL_TIMEOUT_S > DIAG_SUBPROC_TIMEOUT_S, (
            f"journalctl needs more budget than fast calls (uname / nmcli / ip). "
            f"Got DIAG_JOURNAL_TIMEOUT_S={DIAG_JOURNAL_TIMEOUT_S}, "
            f"DIAG_SUBPROC_TIMEOUT_S={DIAG_SUBPROC_TIMEOUT_S}."
        )

    def test_journal_timeout_has_pi_zero_headroom(self):
        from control_server.routes.diagnostics._collectors import DIAG_JOURNAL_TIMEOUT_S

        # Hardware-observed worst case: 3.95s on authorclock under
        # transient CPU load. Pin at >= 5s so any small regression
        # below that observed worst case immediately fails this test.
        assert DIAG_JOURNAL_TIMEOUT_S >= 5.0, (
            f"DIAG_JOURNAL_TIMEOUT_S={DIAG_JOURNAL_TIMEOUT_S} is too tight for Pi Zero 2W. "
            "Hardware QA observed 3.95s for a single -n 3 -u <unit> query; "
            "anything below 5s risks tripping the services anomaly false positive."
        )

    def test_journal_timeout_under_cache_ttl(self):
        """Codex /review on #427 — the cache TTL is the natural upper
        bound for any per-call timeout. A timeout exceeding the TTL
        means a single slow journalctl call could outlive its own
        cache entry, defeating the amortization the cache exists to
        provide. Pins the wider invariant so a future careless bump
        to e.g. 30s gets caught here, not on hardware.
        """
        from control_server.routes.diagnostics._collectors import (
            DIAG_JOURNAL_TIMEOUT_S,
            DIAG_SUBPROC_TTL_S,
        )

        assert DIAG_JOURNAL_TIMEOUT_S < DIAG_SUBPROC_TTL_S, (
            f"DIAG_JOURNAL_TIMEOUT_S={DIAG_JOURNAL_TIMEOUT_S} >= "
            f"DIAG_SUBPROC_TTL_S={DIAG_SUBPROC_TTL_S} — "
            "a per-call timeout that exceeds the cache TTL means the "
            "cache can't amortize the worst-case cost."
        )

    def test_journal_timeout_under_sse_heartbeat(self):
        """Defensive future-proofing: if a future code path ever blocks
        the SSE generator on diagnostics (today /api/logs/stream
        consumes log_buffer in-memory and doesn't call journalctl, so
        no current desync risk), the journal timeout budget pre-bounds
        the block below one heartbeat. Pin the invariant so a future
        bump doesn't cross that line silently.
        """
        from control_server.routes.diagnostics._collectors import DIAG_JOURNAL_TIMEOUT_S
        from control_server.routes.diagnostics._sse import SSE_HEARTBEAT_INTERVAL_S

        assert DIAG_JOURNAL_TIMEOUT_S < SSE_HEARTBEAT_INTERVAL_S, (
            f"DIAG_JOURNAL_TIMEOUT_S={DIAG_JOURNAL_TIMEOUT_S} >= "
            f"SSE_HEARTBEAT_INTERVAL_S={SSE_HEARTBEAT_INTERVAL_S} — "
            "a future code path that blocks the SSE generator on "
            "diagnostics could miss a wire heartbeat. Belt-and-"
            "suspenders for an interaction that doesn't exist today."
        )

    def test_read_journal_tail_uses_journal_timeout_not_fast_timeout(self, monkeypatch):
        """The actual call site MUST use DIAG_JOURNAL_TIMEOUT_S, not the
        fast-call DIAG_SUBPROC_TIMEOUT_S. Pre-v0.214.2 the call site used
        the latter; bumping the constant alone wouldn't fix it.
        """
        captured: list[dict] = []

        def fake_cached_subprocess(key, argv, *, timeout, ttl):
            captured.append({"key": key, "argv": argv, "timeout": timeout, "ttl": ttl})
            return ""

        monkeypatch.setattr(_collectors, "cached_subprocess", fake_cached_subprocess)
        # #428 PR1a: _read_journal_tail's call site now goes through
        # ``cached_subprocess_or_empty`` (CQ-1 helper at the boundary).
        # Patch the new binding so the timeout-budget invariant test
        # still observes the call. Pre-#428 this single monkeypatch on
        # ``cached_subprocess`` was sufficient; per
        # [[learning-reexport-not-monkeypatch-compat]] Python binds names
        # in each module's namespace at import time.
        monkeypatch.setattr(_collectors, "cached_subprocess_or_empty", fake_cached_subprocess)

        _collectors._read_journal_tail("litclock.service", n=3)

        assert len(captured) == 1
        call = captured[0]
        assert call["key"] == "diag-journal-litclock.service"
        assert call["argv"][0] == "journalctl"
        # The headline assertion: journal call uses the journal-specific timeout.
        assert call["timeout"] == _collectors.DIAG_JOURNAL_TIMEOUT_S, (
            f"_read_journal_tail used timeout={call['timeout']}, expected "
            f"DIAG_JOURNAL_TIMEOUT_S={_collectors.DIAG_JOURNAL_TIMEOUT_S}. "
            "If this fails the v0.214.2 regression has been re-introduced."
        )
        # Also pin the ttl kwarg. #436 DECOUPLED the journal tail's cache
        # window from the shared 20s DIAG_SUBPROC_TTL_S onto its own
        # DIAG_JOURNAL_TTL_S (45s), raised ABOVE the 30s PWA poll interval so a
        # still-unhealthy unit reuses the cached tail instead of re-forking the
        # ~5-7s cold journalctl every poll. A contributor collapsing this back
        # to DIAG_SUBPROC_TTL_S (or to DIAG_JOURNAL_TIMEOUT_S) would reintroduce
        # the per-poll re-fork the timeout assertion above wouldn't catch.
        assert call["ttl"] == _collectors.DIAG_JOURNAL_TTL_S, (
            f"_read_journal_tail used ttl={call['ttl']}, expected "
            f"DIAG_JOURNAL_TTL_S={_collectors.DIAG_JOURNAL_TTL_S}. "
            "The journal cache window must stay above the 30s poll interval "
            "(#436) so a stuck-failed unit doesn't re-fork journalctl per poll."
        )


class TestFastCallBudgets:
    """#430 — per-call budgets for the FAST diagnostics subprocess calls.

    The converse of ``TestJournalTimeoutBudget``. Each fast call (nmcli, iw,
    systemctl, timedatectl, git, ip route, uname) now reads its OWN timeout
    constant rather than the single shared ``DIAG_SUBPROC_TIMEOUT_S``, so a
    future bump for a call that turns out slow-under-load (the journalctl
    story) doesn't loosen the cheap kernel calls. These pin the invariants
    that must hold for ANY seeded or tuned value, so the eventual Pi-measured
    tuning (``scripts/diag-subprocess-timing.py``) is a guarded one-line edit.

    The call-site wiring (each reader passes ITS constant) is pinned
    separately, with sentinel monkeypatching, in
    ``test_control_server_diagnostics_readers.py::TestFastReaderTimeoutContract``.
    """

    PER_CALL_CONSTANTS = (
        "DIAG_NMCLI_TIMEOUT_S",
        "DIAG_IW_LINK_TIMEOUT_S",
        "DIAG_SYSTEMCTL_TIMEOUT_S",
        "DIAG_TIMEDATECTL_TIMEOUT_S",
        "DIAG_GIT_HEAD_TIMEOUT_S",
        "DIAG_IP_ROUTE_TIMEOUT_S",
        "DIAG_UNAME_TIMEOUT_S",
    )

    def _value(self, name: str) -> float:
        return getattr(_collectors, name)

    @pytest.mark.parametrize("name", PER_CALL_CONSTANTS)
    def test_positive(self, name: str):
        assert self._value(name) > 0, f"{name} must be a positive timeout"

    @pytest.mark.parametrize("name", PER_CALL_CONSTANTS)
    def test_under_cache_ttl(self, name: str):
        # A per-call timeout that meets/exceeds the cache window would let a
        # single call outlive its own cache entry — the same ceiling the
        # journal budget must respect (test_journal_timeout_under_cache_ttl).
        assert self._value(name) < _collectors.DIAG_SUBPROC_TTL_S, (
            f"{name}={self._value(name)} >= DIAG_SUBPROC_TTL_S="
            f"{_collectors.DIAG_SUBPROC_TTL_S}; a fast call can't outlive its cache window."
        )

    @pytest.mark.parametrize("name", PER_CALL_CONSTANTS)
    def test_not_slower_than_journal_outlier(self, name: str):
        # journalctl is the deliberately-slowest diagnostics call; no "fast"
        # call should ever be budgeted at or above it. If a measurement says a
        # fast call needs >= the journal budget, it isn't "fast" anymore —
        # re-classify it rather than bump past the outlier.
        assert self._value(name) <= _collectors.DIAG_JOURNAL_TIMEOUT_S, (
            f"{name}={self._value(name)} > DIAG_JOURNAL_TIMEOUT_S="
            f"{_collectors.DIAG_JOURNAL_TIMEOUT_S}; a fast call out-budgeting the journal outlier."
        )

    @pytest.mark.parametrize("name", PER_CALL_CONSTANTS)
    def test_under_sse_heartbeat(self, name: str):
        # Defensive, mirrors test_journal_timeout_under_sse_heartbeat: a single
        # fast call must finish well inside the SSE heartbeat so a slow call
        # can't starve the keep-alive.
        from control_server.routes.diagnostics._sse import SSE_HEARTBEAT_INTERVAL_S

        assert self._value(name) < SSE_HEARTBEAT_INTERVAL_S, (
            f"{name}={self._value(name)} >= SSE_HEARTBEAT_INTERVAL_S={SSE_HEARTBEAT_INTERVAL_S}."
        )

    def test_journal_still_strictly_exceeds_every_fast_call(self):
        # The journal outlier must stay strictly the slowest budget — the
        # invariant TestJournalTimeoutBudget asserts against the single shared
        # base, extended here to every per-call constant.
        for name in self.PER_CALL_CONSTANTS:
            assert _collectors.DIAG_JOURNAL_TIMEOUT_S > self._value(name), f"DIAG_JOURNAL_TIMEOUT_S must stay > {name}."

    def test_seeded_at_shared_base_until_measured(self):
        # #430 HONESTY GUARD / tuning tripwire. The per-call budgets are SEEDED
        # at the shared base (behaviour-preserving) and must be tuned only from
        # Pi Zero 2W measurements, never guessed p99s (the #444 call). This
        # pins the seed so any value change is a deliberate, reviewed edit with
        # a measurement citation. WHEN YOU TUNE a constant from
        # scripts/diag-subprocess-timing.py data: update this test to the new
        # value(s) and cite the measurement in the commit / constant comment.
        for name in self.PER_CALL_CONSTANTS:
            assert self._value(name) == _collectors.DIAG_SUBPROC_TIMEOUT_S, (
                f"{name} has diverged from the seeded base. If this is an intentional "
                "tune from #430 Pi measurements, update this guard + cite the data."
            )


class TestSnapshotAtomicity:
    """``handler.snapshot()`` returns ``(entries, total, latest_seq)``
    consistent across one lock acquire."""

    def setup_method(self):
        log_buffer.reset_for_tests()
        log_buffer.init_memory_handler()
        self.handler = log_buffer.get_memory_handler()
        assert self.handler is not None

    def teardown_method(self):
        log_buffer.reset_for_tests()

    def _emit(self, level: int, msg: str):
        record = logging.LogRecord(name="t", level=level, pathname="", lineno=0, msg=msg, args=(), exc_info=None)
        self.handler.emit(record)

    def test_empty_buffer_returns_zeros_and_empty_list(self):
        entries, total, latest = self.handler.snapshot()
        assert entries == []
        assert total == 0
        assert latest == 0

    def test_populated_buffer_returns_consistent_triple(self):
        for i in range(5):
            self._emit(logging.INFO, f"msg-{i}")
        entries, total, latest = self.handler.snapshot()
        assert len(entries) == 5
        assert total == 5
        # latest_seq is the highest seq across the buffer.
        assert latest == max(e.seq for e in entries)

    def test_snapshot_respects_limit(self):
        for i in range(10):
            self._emit(logging.INFO, f"msg-{i}")
        entries, total, latest = self.handler.snapshot(limit=3)
        assert len(entries) == 3
        # total is still the FULL buffer count, not the limit-bounded count.
        assert total == 10
        assert latest == max(e.seq for e in entries) + 0  # latest from full buffer
        # entries are newest-first by default.
        assert entries[0].seq > entries[-1].seq

    def test_snapshot_with_asc_order(self):
        for i in range(10):
            self._emit(logging.INFO, f"msg-{i}")
        entries, total, _latest = self.handler.snapshot(limit=3, order="asc")
        # Same 3 newest entries, oldest-first.
        assert len(entries) == 3
        assert entries[0].seq < entries[-1].seq
        # Total still reflects the full buffer.
        assert total == 10

    def test_snapshot_filters_by_level(self):
        self._emit(logging.INFO, "info-1")
        self._emit(logging.ERROR, "error-1")
        self._emit(logging.INFO, "info-2")
        entries, total, _ = self.handler.snapshot(level="ERROR")
        assert len(entries) == 1
        assert entries[0].message == "error-1"
        # total reflects the level-filtered count, not the buffer total.
        assert total == 1

    def test_snapshot_under_concurrent_emit(self):
        """If snapshot() takes the lock once and emit() also takes the
        lock, the snapshot must reflect a SINGLE consistent buffer state —
        not three different states across three sequential lock-takes.

        This test fires emit() from a background thread while the main
        thread takes a snapshot; the snapshot's entries + total + latest
        must agree internally (no off-by-one across the in-flight emit).
        """
        for i in range(50):
            self._emit(logging.INFO, f"baseline-{i}")

        stop = threading.Event()

        def background_emit():
            n = 0
            while not stop.is_set():
                self._emit(logging.INFO, f"bg-{n}")
                n += 1

        bg = threading.Thread(target=background_emit, daemon=True)
        bg.start()
        try:
            for _ in range(50):
                entries, total, latest = self.handler.snapshot()
                # Internal consistency: latest must be the seq of the
                # newest entry visible in the snapshot.
                if entries:
                    assert latest == entries[0].seq, (
                        f"snapshot drift: latest={latest} vs entries[0].seq={entries[0].seq}"
                    )
                # total reflects current buffer length; for an unfiltered
                # snapshot total >= len(entries).
                assert total >= len(entries)
        finally:
            stop.set()
            bg.join(timeout=2.0)


class TestGetLogsAscOrder:
    """``get_logs(order='asc')`` semantic (#419 D10): limit picks newest N,
    order post-sorts the slice. Without this discipline a caller that
    flips order accidentally swaps "newest N" for "oldest N"."""

    def setup_method(self):
        log_buffer.reset_for_tests()
        log_buffer.init_memory_handler()
        self.handler = log_buffer.get_memory_handler()
        assert self.handler is not None

    def teardown_method(self):
        log_buffer.reset_for_tests()

    def _emit(self, msg: str):
        record = logging.LogRecord(
            name="t",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=msg,
            args=(),
            exc_info=None,
        )
        self.handler.emit(record)

    def test_desc_default_unchanged(self):
        for i in range(10):
            self._emit(f"msg-{i}")
        entries = self.handler.get_logs(limit=4)
        # Default is desc: newest first.
        assert [e.message for e in entries] == ["msg-9", "msg-8", "msg-7", "msg-6"]

    def test_asc_returns_newest_N_in_chronological_order(self):
        """The headline D10 contract: limit=4 + order='asc' returns the
        4 NEWEST entries (msg-6..msg-9), then reorders them ascending
        (msg-6 first). NOT the 4 oldest (msg-0..msg-3)."""
        for i in range(10):
            self._emit(f"msg-{i}")
        entries = self.handler.get_logs(limit=4, order="asc")
        assert [e.message for e in entries] == ["msg-6", "msg-7", "msg-8", "msg-9"]

    def test_asc_without_limit_returns_entire_buffer_chronologically(self):
        for i in range(5):
            self._emit(f"msg-{i}")
        entries = self.handler.get_logs(order="asc")
        assert [e.message for e in entries] == [f"msg-{i}" for i in range(5)]

    def test_asc_with_since_seq_filter(self):
        """The since_seq filter applies BEFORE the limit + order pass — a
        reconnecting SSE client wants 'entries newer than X, in chrono
        order, capped at MAX_ENTRIES.'"""
        for i in range(5):
            self._emit(f"msg-{i}")
        cutoff = self.handler.get_logs(limit=1)[0].seq - 3  # 3rd-newest seq
        entries = self.handler.get_logs(since_seq=cutoff, order="asc")
        # Entries with seq > cutoff, oldest-first.
        assert len(entries) == 3
        assert entries[0].message == "msg-2"
        assert entries[-1].message == "msg-4"

    def test_invalid_order_raises(self):
        """Misspelling ``order`` shouldn't silently fall through to the
        wrong default — the validator catches it loudly."""
        with pytest.raises(ValueError):
            self.handler.get_logs(order="oldest")

    def test_snapshot_invalid_order_raises(self):
        with pytest.raises(ValueError):
            self.handler.snapshot(order="newest")
