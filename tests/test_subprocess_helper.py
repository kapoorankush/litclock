"""Tests for control_server/_subprocess.py (#416 T2 extraction + C2=A parameterized ttl)."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from control_server import _subprocess  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_cache():
    """Empty cache between tests so a previous case's entry can't satisfy
    a later case's lookup. Also runs after the test so an unrelated suite
    starts fresh."""
    _subprocess.clear_cache()
    yield
    _subprocess.clear_cache()


class TestCachedSubprocess:
    def test_returns_stdout_stripped_on_success(self):
        out = _subprocess.cached_subprocess("smoke-echo", ["echo", "hello"])
        assert out == "hello"

    def test_returns_empty_string_on_nonzero_exit(self):
        # rc != 0 is a SUCCESSFUL run that signaled non-success — distinct
        # from a subprocess failure. Returns "" (the cached representation
        # of "binary ran but produced no stdout"), NOT None.
        out = _subprocess.cached_subprocess("smoke-false", ["false"])
        assert out == ""

    def test_returns_none_on_missing_binary(self):
        # #428 PR1a: FileNotFoundError now surfaces as None so PR1b can
        # short-cache transient subprocess failures separately from
        # successful empty-stdout results.
        out = _subprocess.cached_subprocess("smoke-missing", ["nonexistent_binary_xyz_42"])
        assert out is None

    def test_returns_none_on_timeout(self):
        # #428 PR1a: subprocess.TimeoutExpired now surfaces as None.
        # Pre-PR1a returned "", indistinguishable from a successful
        # run that produced empty stdout.
        out = _subprocess.cached_subprocess("smoke-timeout", ["sleep", "5"], timeout=0.05)
        assert out is None

    def test_cache_hit_returns_prior_result(self):
        # First call runs the real subprocess; second call returns the
        # cached value even if we point the argv at a different command.
        _subprocess.cached_subprocess("k", ["echo", "first"])
        # Different argv, same key. Cached value wins inside the ttl window.
        out = _subprocess.cached_subprocess("k", ["echo", "second"])
        assert out == "first"

    def test_ttl_zero_means_always_refresh(self, monkeypatch):
        # With ttl=0 each call must re-run the subprocess. Verify by
        # spying on subprocess.run.
        calls = {"n": 0}
        real_run = subprocess.run

        def spy(*args, **kwargs):
            calls["n"] += 1
            return real_run(*args, **kwargs)

        monkeypatch.setattr(subprocess, "run", spy)
        _subprocess.cached_subprocess("k", ["echo", "x"], ttl=0)
        _subprocess.cached_subprocess("k", ["echo", "x"], ttl=0)
        assert calls["n"] == 2

    def test_long_ttl_caches_across_calls(self, monkeypatch):
        # With a generous ttl, two calls within the window do one subprocess.
        calls = {"n": 0}
        real_run = subprocess.run

        def spy(*args, **kwargs):
            calls["n"] += 1
            return real_run(*args, **kwargs)

        monkeypatch.setattr(subprocess, "run", spy)
        _subprocess.cached_subprocess("k", ["echo", "x"], ttl=60)
        _subprocess.cached_subprocess("k", ["echo", "x"], ttl=60)
        assert calls["n"] == 1

    def test_status_legacy_wrapper_uses_module_cache(self):
        """Status's _cached_subprocess shim must hit the same module-level
        cache so a status call and a future diagnostics call can share a
        warm SSID lookup if both are in-flight."""
        from control_server.routes import status

        status._cached_subprocess("shared-k", ["echo", "warm"])
        # A subsequent call through the module-level helper sees the same
        # entry — the wrapper is a thin pass-through.
        out = _subprocess.cached_subprocess("shared-k", ["echo", "different"])
        assert out == "warm"


class TestCachedSubprocessOrEmpty:
    """#428 PR1a CQ-1: convenience wrapper for display callers (8 sites
    in _network.py + _collectors.py) that treat "subprocess failed" as
    "binary produced no stdout." Coerces None → "" so
    ``raw.splitlines()``/``raw.strip()`` idioms keep working.
    """

    def test_passes_through_success_string(self):
        out = _subprocess.cached_subprocess_or_empty("smoke-or-empty-echo", ["echo", "hello"])
        assert out == "hello"

    def test_passes_through_empty_string_on_nonzero_exit(self):
        # rc != 0 → "" from raw helper → "" from wrapper. Wrapper does NOT
        # collapse "" and None into the same surface; only None coerces.
        out = _subprocess.cached_subprocess_or_empty("smoke-or-empty-false", ["false"])
        assert out == ""

    def test_coerces_none_to_empty_string_on_missing_binary(self):
        # Raw helper returns None for the missing-binary case. Wrapper
        # coerces so the caller can immediately ``.splitlines()`` without
        # an AttributeError on None.
        out = _subprocess.cached_subprocess_or_empty("smoke-or-empty-missing", ["nonexistent_binary_xyz_42"])
        assert out == ""

    def test_coerces_none_to_empty_string_on_timeout(self):
        out = _subprocess.cached_subprocess_or_empty("smoke-or-empty-timeout", ["sleep", "5"], timeout=0.05)
        assert out == ""

    def test_returns_str_type_unconditionally(self):
        # Type contract: helper signature is `-> str`, never `-> str | None`.
        # Verify by checking type at runtime on a failure path.
        out = _subprocess.cached_subprocess_or_empty("smoke-or-empty-type", ["nonexistent_binary_xyz_43"])
        assert isinstance(out, str)


class TestCachedSubprocessReturnType:
    """#428 PR1a OV-2: callers using truthy guards (`if not raw:`) work
    unchanged with the new Optional[str] return. Pin the contract via
    explicit return-type assertions so a future refactor that re-collapses
    None and "" can't silently regress the classifier callers' contract."""

    def test_success_returns_str(self):
        out = _subprocess.cached_subprocess("type-success", ["echo", "ok"])
        assert isinstance(out, str)
        assert out == "ok"

    def test_nonzero_exit_returns_empty_str_not_none(self):
        # Critical: rc != 0 must stay distinguishable from subprocess
        # failure. PR1b's failure-TTL branch reads this distinction.
        out = _subprocess.cached_subprocess("type-nonzero", ["false"])
        assert out is not None
        assert out == ""

    def test_subprocess_failure_returns_none_not_empty_str(self):
        # Critical: subprocess failure (missing binary) must stay
        # distinguishable from a successful empty-stdout run.
        out = _subprocess.cached_subprocess("type-fail", ["nonexistent_binary_xyz_44"])
        assert out is None

    def test_cache_stores_none_for_failure_within_ttl(self):
        # Failure result is cached, returned for repeat lookups within
        # the failure-TTL window (min(caller_ttl, FAILURE_TTL_CAP_S)).
        # The two calls below happen within microseconds, well under the
        # 5s failure cap — second call returns the cached None, not the
        # would-be echo value.
        out1 = _subprocess.cached_subprocess("type-fail-cached", ["nonexistent_binary_xyz_45"], ttl=60)
        out2 = _subprocess.cached_subprocess("type-fail-cached", ["echo", "would-replace-cache-if-uncached"], ttl=60)
        assert out1 is None
        # Second call returns the cached None, not the would-be echo value.
        assert out2 is None


class _MockClock:
    """Manually advance ``_time.monotonic`` so failure-TTL tests don't
    have to ``time.sleep(5)`` between calls. Mirrors freezegun's
    ``tick`` API; freezegun isn't in our pinned dev deps, so this is a
    one-purpose stand-in.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


class TestFailureTtl:
    """#428 PR1b /plan-eng-review P-2: failure cache window.

    Pre-PR1b, every cached entry used the caller's ``ttl`` regardless of
    whether the cached value was a success or a subprocess failure.
    Diagnostics passes ``ttl=20`` — a transient nmcli timeout pinned the
    cache to ``None`` for 20 seconds, hiding recovery from the next 6-7
    poll cycles. PR1b applies ``min(ttl, FAILURE_TTL_CAP_S)`` to
    failure entries so recovery surfaces within ~5s. Success entries
    keep the full caller ``ttl`` so steady-state cache warmth is
    preserved on the long-poll routes.
    """

    def test_failure_ttl_cap_constant_is_5s(self):
        # Pin the constant — a future contributor bumping it to e.g. 30s
        # would silently re-introduce the pre-PR1b 20s-failure-pin class
        # of bugs on diagnostics, defeating the whole P-2 fix.
        assert _subprocess.FAILURE_TTL_CAP_S == 5.0

    def test_failure_expires_at_5s_when_caller_ttl_is_larger(self, monkeypatch):
        # Diagnostics-class caller (ttl=20s). Failure entry must rotate
        # at 5s, NOT 20s. This is the headline P-2 fix.
        clock = _MockClock()
        monkeypatch.setattr(_subprocess._time, "monotonic", clock)

        out1 = _subprocess.cached_subprocess("ttl-fail-cap", ["nonexistent_xyz_b1"], ttl=20)
        assert out1 is None

        # 3s later: still within failure TTL. Cached None returned, no
        # re-run. Use a key-collision argv (echo) that WOULD produce a
        # different result if the subprocess re-ran.
        clock.advance(3.0)
        out2 = _subprocess.cached_subprocess("ttl-fail-cap", ["echo", "would-rerun"], ttl=20)
        assert out2 is None  # cached failure, not the echo result

        # 6s total: PAST failure TTL (5s). Cache expired, subprocess
        # re-runs. The echo argv succeeds, producing "would-rerun".
        clock.advance(3.0)
        out3 = _subprocess.cached_subprocess("ttl-fail-cap", ["echo", "would-rerun"], ttl=20)
        assert out3 == "would-rerun"

    def test_failure_uses_caller_ttl_when_smaller_than_5s(self, monkeypatch):
        # A hypothetical fast-poll caller (ttl=1s) must keep their
        # tighter cadence on failures too. min(1, 5) == 1, so failure
        # entries expire at 1s. Otherwise this caller would see a 5s
        # cached failure despite asking for a 1s window.
        clock = _MockClock()
        monkeypatch.setattr(_subprocess._time, "monotonic", clock)

        out1 = _subprocess.cached_subprocess("ttl-fail-fast", ["nonexistent_xyz_b2"], ttl=1)
        assert out1 is None

        # 0.5s later: still within caller TTL.
        clock.advance(0.5)
        out2 = _subprocess.cached_subprocess("ttl-fail-fast", ["echo", "would-rerun"], ttl=1)
        assert out2 is None

        # 1.5s total: PAST caller TTL. Cache expired.
        clock.advance(1.0)
        out3 = _subprocess.cached_subprocess("ttl-fail-fast", ["echo", "would-rerun"], ttl=1)
        assert out3 == "would-rerun"

    def test_success_uses_full_caller_ttl_unchanged(self, monkeypatch):
        # Regression guard: PR1b ONLY changes the failure path. Success
        # entries must still honour the caller's full ``ttl`` so the
        # 20s diagnostics cache stays warm across the 30s poll cadence.
        clock = _MockClock()
        monkeypatch.setattr(_subprocess._time, "monotonic", clock)

        out1 = _subprocess.cached_subprocess("ttl-success", ["echo", "hello"], ttl=20)
        assert out1 == "hello"

        # 10s later: well inside the 20s window. Cache hit.
        clock.advance(10.0)
        out2 = _subprocess.cached_subprocess("ttl-success", ["echo", "would-replace"], ttl=20)
        assert out2 == "hello"

        # 25s total: past 20s. Cache expired, re-run.
        clock.advance(15.0)
        out3 = _subprocess.cached_subprocess("ttl-success", ["echo", "would-replace"], ttl=20)
        assert out3 == "would-replace"

    def test_nonzero_exit_uses_full_caller_ttl(self, monkeypatch):
        # Non-zero exit returns "" (the binary RAN but signalled
        # non-success), distinct from a subprocess failure. Per the
        # PR1a contract this is a SUCCESS path — must use the full
        # caller TTL, not the failure TTL. Otherwise an `is-active`
        # check on an inactive service would needlessly re-fork every
        # 5s on diagnostics, defeating the cache.
        clock = _MockClock()
        monkeypatch.setattr(_subprocess._time, "monotonic", clock)

        out1 = _subprocess.cached_subprocess("ttl-nonzero", ["false"], ttl=20)
        assert out1 == ""

        # 10s later: past failure-TTL (5s) but inside success-TTL (20s).
        # MUST still be cached because "" is a success, not a failure.
        clock.advance(10.0)
        out2 = _subprocess.cached_subprocess("ttl-nonzero", ["echo", "would-replace"], ttl=20)
        assert out2 == ""

    def test_failure_then_success_overwrites_cache_with_full_ttl(self, monkeypatch):
        # Recovery scenario: binary failed at t=0, recovered at t=6.
        # The recovered (success) entry must use the full caller TTL,
        # not the 5s failure TTL. A previous failure entry must NOT
        # poison the next success entry's TTL.
        clock = _MockClock()
        monkeypatch.setattr(_subprocess._time, "monotonic", clock)

        # t=0: failure cached for 5s
        out1 = _subprocess.cached_subprocess("ttl-recover", ["nonexistent_xyz_b3"], ttl=20)
        assert out1 is None

        # t=6: failure expired, re-run with success argv → cached for 20s
        clock.advance(6.0)
        out2 = _subprocess.cached_subprocess("ttl-recover", ["echo", "recovered"], ttl=20)
        assert out2 == "recovered"

        # t=16: still within 20s success TTL, cache hit
        clock.advance(10.0)
        out3 = _subprocess.cached_subprocess("ttl-recover", ["echo", "would-replace"], ttl=20)
        assert out3 == "recovered"

    def test_failure_entry_not_born_stale_when_subprocess_exceeds_ttl_cap(self, monkeypatch):
        # #428 PR1b /review ADV-1 (cross-model Claude+Codex): the
        # write-time monotonic must be captured AFTER subprocess.run
        # returns. Otherwise a wedged subprocess that exceeds
        # FAILURE_TTL_CAP_S (5s) births a cache entry that's already
        # expired. journalctl's 8s timeout > 5s cap is the concrete
        # production case: pre-fix, every /api/diagnostics request
        # would re-fork the 8s timeout because the cached failure was
        # always "stale" before any caller could read it.
        #
        # Reproduce by spying on subprocess.run: when invoked, advance
        # the mock clock by 8 seconds and raise TimeoutExpired (the
        # FileNotFoundError / SubprocessError path is symmetric).
        clock = _MockClock()
        monkeypatch.setattr(_subprocess._time, "monotonic", clock)

        def slow_run(*args, **kwargs):
            clock.advance(8.0)  # simulate journalctl-class 8s timeout
            raise subprocess.TimeoutExpired(args[0], 8.0)

        monkeypatch.setattr(subprocess, "run", slow_run)

        # Caller A at t=0 invokes; subprocess "runs" for 8s.
        out_a = _subprocess.cached_subprocess("born-stale", ["sleep", "5"], ttl=20)
        assert out_a is None  # failure path

        # Caller B at t=8.1, just 0.1s after A's subprocess returned.
        # MUST hit the cache (within the 5s failure-TTL window from
        # WRITE time, not from the pre-subprocess time). Use a stub
        # that would re-raise if the cache MISSED; if we re-run, the
        # test would advance the clock and the assertion would fail.
        clock.advance(0.1)
        out_b = _subprocess.cached_subprocess("born-stale", ["sleep", "5"], ttl=20)
        assert out_b is None
        # The headline assertion: the cache hit, so subprocess.run was
        # invoked exactly once (by caller A). If the cache entry were
        # born stale, caller B would re-invoke slow_run and the clock
        # would advance again.
        assert clock() == pytest.approx(8.1)

    def test_ttl_zero_means_always_refresh_on_failure(self, monkeypatch):
        # Mirror of TestCachedSubprocess::test_ttl_zero_means_always_refresh
        # for the failure path. min(0, 5) == 0 → every call re-forks even
        # on a cached failure. A refactor that swapped min() for clamp()
        # or flipped the strict-< comparison would silently regress this
        # without a test failure on the success-path equivalent.
        calls = {"n": 0}
        real_run = subprocess.run

        def spy(*args, **kwargs):
            calls["n"] += 1
            return real_run(*args, **kwargs)

        monkeypatch.setattr(subprocess, "run", spy)
        # Both calls hit a binary that doesn't exist → FileNotFoundError
        # → None return. With ttl=0, BOTH calls must re-run the subprocess
        # despite the cached None from the first call.
        _subprocess.cached_subprocess("ttl-fail-zero", ["nonexistent_xyz_b5"], ttl=0)
        _subprocess.cached_subprocess("ttl-fail-zero", ["nonexistent_xyz_b5"], ttl=0)
        assert calls["n"] == 2

    def test_or_empty_helper_also_observes_failure_ttl(self, monkeypatch):
        # Cross-helper: `cached_subprocess_or_empty` is a thin pass-
        # through, so the failure-TTL branch in `cached_subprocess`
        # transparently applies. Display callers (_network.py readers,
        # _collectors.py readers) ALSO benefit from quicker recovery.
        clock = _MockClock()
        monkeypatch.setattr(_subprocess._time, "monotonic", clock)

        out1 = _subprocess.cached_subprocess_or_empty("ttl-fail-wrap", ["nonexistent_xyz_b4"], ttl=20)
        assert out1 == ""  # None coerced to "" by wrapper

        # 3s: cached failure, wrapper returns ""
        clock.advance(3.0)
        out2 = _subprocess.cached_subprocess_or_empty("ttl-fail-wrap", ["echo", "would-rerun"], ttl=20)
        assert out2 == ""

        # 6s: failure TTL expired (5s), subprocess re-runs successfully
        clock.advance(3.0)
        out3 = _subprocess.cached_subprocess_or_empty("ttl-fail-wrap", ["echo", "would-rerun"], ttl=20)
        assert out3 == "would-rerun"


class TestConcurrentMutation:
    """PR1 adversarial pass: the compound get/move/popitem/assign
    sequence is now under ``_cache_lock`` because raw OrderedDict
    mutations are NOT atomic. Without the lock, concurrent misses on
    different keys could over-evict, and a hit racing with a popitem
    could KeyError on move_to_end."""

    def test_concurrent_lookups_and_inserts_no_error(self):
        import threading

        errors: list[BaseException] = []
        stop = threading.Event()

        def reader() -> None:
            try:
                while not stop.is_set():
                    _subprocess.cached_subprocess("shared-k", ["echo", "warm"])
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        def churn() -> None:
            i = 0
            try:
                while not stop.is_set():
                    _subprocess.cached_subprocess(f"churn-{i % 100}", ["echo", "x"])
                    i += 1
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(3)]
        threads += [threading.Thread(target=churn) for _ in range(3)]
        for t in threads:
            t.start()
        time.sleep(0.5)
        stop.set()
        for t in threads:
            t.join(timeout=2.0)
        assert not errors, f"concurrency raised: {errors!r}"
        # Cache is still bounded after churn.
        assert len(_subprocess._cache) <= _subprocess.MAX_CACHE_ENTRIES


class TestCacheBounds:
    """The cache is hard-capped at MAX_CACHE_ENTRIES with LRU eviction so
    a future contributor varying the key per call (e.g. embedding a
    hostname) can't leak entries forever (#416 PR1 /review ASK-5=A)."""

    def test_cap_prevents_unbounded_growth(self):
        # Fill past the cap with unique keys.
        for i in range(_subprocess.MAX_CACHE_ENTRIES + 20):
            _subprocess.cached_subprocess(f"key-{i}", ["echo", str(i)])
        # Hard invariant: cache never grows past the cap.
        assert len(_subprocess._cache) == _subprocess.MAX_CACHE_ENTRIES

    def test_lru_evicts_oldest_on_overflow(self):
        # Insert one entry; fill the rest of the cap with distinct keys.
        _subprocess.cached_subprocess("OLDEST", ["echo", "first"])
        for i in range(_subprocess.MAX_CACHE_ENTRIES):
            _subprocess.cached_subprocess(f"filler-{i}", ["echo", "x"])
        # OLDEST was the least-recently-used and should have been evicted
        # when the cache reached MAX_CACHE_ENTRIES + 1 logical inserts.
        assert "OLDEST" not in _subprocess._cache
        assert len(_subprocess._cache) == _subprocess.MAX_CACHE_ENTRIES

    def test_warm_hit_refreshes_lru_position(self):
        # Insert OLDER first, then NEWER. OLDER is the older insertion.
        _subprocess.cached_subprocess("OLDER", ["echo", "old"])
        _subprocess.cached_subprocess("NEWER", ["echo", "new"])
        # Fill the cap with throwaway keys so the next insert triggers
        # eviction. After this: cache holds OLDER, NEWER, and the
        # fillers, with OLDER at the LRU position.
        for i in range(_subprocess.MAX_CACHE_ENTRIES - 2):
            _subprocess.cached_subprocess(f"filler-{i}", ["echo", "x"])
        assert len(_subprocess._cache) == _subprocess.MAX_CACHE_ENTRIES
        # Touch OLDER — this is a TTL hit (default 5s, no sleep) so the
        # code path runs ``_cache.move_to_end(key)`` and OLDER becomes
        # MRU, displacing NEWER as the older of the two.
        _subprocess.cached_subprocess("OLDER", ["echo", "ignored"])
        # One overflow insert evicts the new LRU — which is NEWER, NOT
        # OLDER. That's the whole point of LRU bookkeeping.
        _subprocess.cached_subprocess("overflow", ["echo", "z"])
        assert "OLDER" in _subprocess._cache, "OLDER was refreshed to MRU; it must survive the overflow."
        assert "NEWER" not in _subprocess._cache, (
            "After OLDER's refresh, NEWER became the oldest of the two and should be the eviction victim."
        )

    def test_overwrite_existing_key_does_not_evict(self):
        # When a key is already in the cache, updating it must not trip
        # the eviction path (popitem with 0 entries would still error,
        # but more importantly: it shouldn't pretend an unrelated entry
        # needs to leave).
        keys = [f"k-{i}" for i in range(_subprocess.MAX_CACHE_ENTRIES)]
        for k in keys:
            _subprocess.cached_subprocess(k, ["echo", "x"], ttl=0)
        # Re-insert one — TTL=0 forces a re-run, but the key is reused.
        _subprocess.cached_subprocess(keys[0], ["echo", "x"], ttl=0)
        # Cap is unchanged; no eviction happened on the overwrite.
        assert len(_subprocess._cache) == _subprocess.MAX_CACHE_ENTRIES
        # Every original key is still present.
        for k in keys:
            assert k in _subprocess._cache


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
