"""Tests for control_server/log_buffer.py (#416 T4 + OV-4=A + C3=A + OV-1=A).

Coverage:
- LogEntry shape + to_dict serialisation
- emit() appends + assigns monotonic seq
- get_logs() with limit / level / since_seq
- total_count, latest_seq
- subscribe / unsubscribe lifecycle, idempotency
- bounded-queue overflow drops to floor (OV-4=A)
- RedactingFilter installed by default (OV-1=A)
- init_memory_handler is idempotent
- waitress logger level override (OV-Misc=A item 4)
- concurrent emit + get_logs holds the lock (no `deque mutated` raise)

The handler module owns a process-level singleton; the autouse fixture
calls reset_for_tests() so state doesn't leak across cases.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from control_server import log_buffer  # noqa: E402
from control_server._redaction import REDACTED_TOKEN  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_singleton():
    log_buffer.reset_for_tests()
    yield
    log_buffer.reset_for_tests()


def _emit(handler: log_buffer.MemoryLogHandler, level: int, msg: str, name: str = "tst") -> None:
    """Build a minimal LogRecord and shove it through emit()."""
    rec = logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=None,
        exc_info=None,
    )
    handler.emit(rec)


class TestEmit:
    def test_appends_to_buffer_with_seq(self):
        h = log_buffer.MemoryLogHandler(max_entries=5)
        _emit(h, logging.INFO, "first")
        _emit(h, logging.WARNING, "second")
        entries = h.get_logs()
        assert [e.message for e in entries] == ["second", "first"]
        assert entries[1].seq < entries[0].seq

    def test_evicts_oldest_at_maxlen(self):
        h = log_buffer.MemoryLogHandler(max_entries=3)
        for i in range(5):
            _emit(h, logging.INFO, f"m{i}")
        entries = h.get_logs()
        # Newest-first; only the last 3 survive in the deque.
        assert [e.message for e in entries] == ["m4", "m3", "m2"]

    def test_does_not_raise_on_format_failure(self):
        h = log_buffer.MemoryLogHandler(max_entries=5)
        # Bad format string with missing key — getMessage() will raise.
        # The legitimate "format with mapping" call shape per Python's
        # logging docs is args=(mapping,) — a 1-tuple wrapping the dict —
        # NOT a bare dict (which LogRecord.__init__ itself rejects).
        rec = logging.LogRecord(
            name="t",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="%(missing)s",
            args=({"other": "v"},),
            exc_info=None,
        )
        # Should not bubble up; handleError swallows it.
        h.emit(rec)

    def test_to_dict_shape(self):
        h = log_buffer.MemoryLogHandler(max_entries=5)
        _emit(h, logging.INFO, "shape", name="my.logger")
        d = h.get_logs()[0].to_dict()
        assert set(d.keys()) == {"seq", "timestamp", "level", "logger", "message"}
        assert d["level"] == "INFO"
        assert d["logger"] == "my.logger"
        assert d["message"] == "shape"


class TestGetLogs:
    def test_limit_caps_results(self):
        h = log_buffer.MemoryLogHandler(max_entries=20)
        for i in range(10):
            _emit(h, logging.INFO, f"m{i}")
        assert len(h.get_logs(limit=3)) == 3

    def test_level_filter_case_insensitive(self):
        h = log_buffer.MemoryLogHandler(max_entries=20)
        _emit(h, logging.INFO, "a")
        _emit(h, logging.WARNING, "b")
        _emit(h, logging.ERROR, "c")
        warns = h.get_logs(level="warning")
        assert [e.message for e in warns] == ["b"]
        errs = h.get_logs(level="ERROR")
        assert [e.message for e in errs] == ["c"]

    def test_since_seq_returns_only_newer(self):
        h = log_buffer.MemoryLogHandler(max_entries=20)
        for i in range(5):
            _emit(h, logging.INFO, f"m{i}")
        cutoff = h.get_logs()[2].seq  # "m2", oldest in the get_logs slice
        newer = h.get_logs(since_seq=cutoff)
        assert all(e.seq > cutoff for e in newer)

    def test_empty_buffer_returns_empty_list(self):
        h = log_buffer.MemoryLogHandler(max_entries=5)
        assert h.get_logs() == []
        assert h.get_logs(limit=100) == []
        assert h.get_logs(level="ERROR") == []


class TestTotalCountAndSeq:
    def test_total_count_overall(self):
        h = log_buffer.MemoryLogHandler(max_entries=10)
        for i in range(3):
            _emit(h, logging.INFO, f"m{i}")
        assert h.total_count() == 3

    def test_total_count_per_level(self):
        h = log_buffer.MemoryLogHandler(max_entries=10)
        _emit(h, logging.INFO, "a")
        _emit(h, logging.INFO, "b")
        _emit(h, logging.WARNING, "c")
        assert h.total_count(level="INFO") == 2
        assert h.total_count(level="WARNING") == 1

    def test_latest_seq_empty(self):
        h = log_buffer.MemoryLogHandler(max_entries=5)
        assert h.latest_seq() == 0

    def test_latest_seq_after_emits(self):
        h = log_buffer.MemoryLogHandler(max_entries=5)
        _emit(h, logging.INFO, "a")
        _emit(h, logging.INFO, "b")
        assert h.latest_seq() == 2


class TestExcInfoCapture:
    """PR1 adversarial pass: log.exception(...) records carry exc_info
    but emit() was capturing only record.getMessage() — the traceback
    was lost, defeating the helper-paste-into-issue workflow."""

    def test_exception_traceback_appears_in_buffer(self):
        h = log_buffer.MemoryLogHandler(max_entries=5)
        log = logging.getLogger("test.exc_info")
        log.setLevel(logging.DEBUG)
        log.addHandler(h)
        try:
            raise ValueError("save failed: env.sh write")
        except ValueError:
            log.exception("env.sh save failed")
        stored = h.get_logs()[0].message
        assert "env.sh save failed" in stored
        assert "Traceback (most recent call last)" in stored
        assert "ValueError: save failed: env.sh write" in stored

    def test_exception_traceback_runs_through_redaction(self):
        # CRITICAL (PR1 codex adversarial pass): the appended traceback
        # bypassed the RedactingFilter (which ran on the bare message
        # only). A raised ValueError carrying a credential-shaped string
        # would otherwise leak verbatim through `log.exception(...)`.
        # Verify the fix by raising something with secrets in the args.
        from control_server._redaction import REDACTED_TOKEN

        # Install the redacting filter the same way init_memory_handler
        # does — the handler-direct test path doesn't otherwise.
        h = log_buffer.init_memory_handler()
        try:
            log = logging.getLogger("test.exc_redact")
            log.setLevel(logging.DEBUG)
            try:
                raise ValueError("boom PSK=supersecret12345 lat=37.774929")
            except ValueError:
                log.exception("route failed")
            stored = h.get_logs()[0].message
            assert "supersecret12345" not in stored, "traceback leaked PSK"
            assert "37.774929" not in stored, "traceback leaked exact coord"
            assert REDACTED_TOKEN in stored
            # Coordinate rounded inline; not redacted-as-marker.
            assert "37.77" in stored
        finally:
            log_buffer.reset_for_tests()

    def test_plain_log_call_has_no_traceback_section(self):
        h = log_buffer.MemoryLogHandler(max_entries=5)
        log = logging.getLogger("test.plain")
        log.setLevel(logging.INFO)
        log.addHandler(h)
        log.info("just a normal message")
        stored = h.get_logs()[0].message
        assert stored == "just a normal message"
        assert "Traceback" not in stored


class TestMessageSizeCap:
    """Per /review ASK-4=B: emit() truncates pathological message sizes so
    one bad caller can't blow the Pi Zero 2W's RSS budget. The cap is
    generous enough to carry full tracebacks + Flask debug dumps; only
    100 KB+ accidents get cut."""

    def test_normal_message_passes_through_unchanged(self):
        h = log_buffer.MemoryLogHandler(max_entries=5)
        _emit(h, logging.INFO, "regular log line, well under cap")
        assert h.get_logs()[0].message == "regular log line, well under cap"

    def test_message_at_cap_unchanged(self):
        # A message exactly equal to MAX_MESSAGE_BYTES (no marker needed).
        # ASCII so encoded byte length == char length.
        h = log_buffer.MemoryLogHandler(max_entries=5)
        msg = "x" * log_buffer.MAX_MESSAGE_BYTES
        _emit(h, logging.INFO, msg)
        stored = h.get_logs()[0].message
        assert len(stored.encode("utf-8")) == log_buffer.MAX_MESSAGE_BYTES
        assert log_buffer.TRUNCATION_MARKER not in stored

    def test_oversize_message_is_truncated_with_marker(self):
        h = log_buffer.MemoryLogHandler(max_entries=5)
        # 256 KB — way past the 64 KB cap. Mimics an accidental request-
        # body dump.
        msg = "A" * 262144
        _emit(h, logging.INFO, msg)
        stored = h.get_logs()[0].message
        assert stored.endswith(log_buffer.TRUNCATION_MARKER)
        # Counted in bytes now (PR1 adversarial pass) — char count would
        # let multi-byte content overshoot by 4×.
        assert len(stored.encode("utf-8")) == log_buffer.MAX_MESSAGE_BYTES

    def test_emoji_payload_respects_byte_budget(self):
        # PR1 adversarial pass: a payload of 4-byte UTF-8 codepoints
        # (emoji) under a char-count cap would consume ~4× the documented
        # budget. The byte-count cap holds the line.
        h = log_buffer.MemoryLogHandler(max_entries=5)
        # Each emoji is 4 bytes in UTF-8; 100K chars = 400KB of bytes.
        msg = "🔥" * 100_000
        _emit(h, logging.INFO, msg)
        stored = h.get_logs()[0].message
        encoded = stored.encode("utf-8")
        assert len(encoded) <= log_buffer.MAX_MESSAGE_BYTES
        assert stored.endswith(log_buffer.TRUNCATION_MARKER)

    def test_truncation_preserves_leading_content(self):
        # The cut keeps the start of the message (where the helper
        # context usually is) and appends the marker; the tail is lost.
        h = log_buffer.MemoryLogHandler(max_entries=5)
        prefix = "ERROR root: Saving env.sh failed with traceback: "
        body = "X" * 200000
        _emit(h, logging.INFO, prefix + body)
        stored = h.get_logs()[0].message
        assert stored.startswith(prefix)
        assert stored.endswith(log_buffer.TRUNCATION_MARKER)


class TestClear:
    """clear() is documented as "wired only to test fixtures" but it IS
    public API — a test framework that depends on it deserves coverage."""

    def test_clear_empties_buffer(self):
        h = log_buffer.MemoryLogHandler(max_entries=10)
        for i in range(5):
            _emit(h, logging.INFO, f"m{i}")
        assert h.total_count() == 5
        h.clear()
        assert h.get_logs() == []
        assert h.total_count() == 0
        # latest_seq reverts to 0 (empty buffer signal); the seq counter
        # itself does NOT reset — new emits keep climbing past the
        # pre-clear high water mark.
        assert h.latest_seq() == 0

    def test_clear_does_not_reset_seq_counter(self):
        h = log_buffer.MemoryLogHandler(max_entries=10)
        for i in range(3):
            _emit(h, logging.INFO, f"pre{i}")
        h.clear()
        _emit(h, logging.INFO, "post")
        # The single post-clear entry got seq=4, NOT seq=1 — clearing the
        # buffer doesn't reset the monotonic counter (SSE clients that
        # cached pre-clear seq values can still tell "this is newer").
        entries = h.get_logs()
        assert len(entries) == 1
        assert entries[0].seq == 4


class TestSubscribers:
    def test_subscribe_returns_queue_and_receives_entries(self):
        h = log_buffer.MemoryLogHandler(max_entries=5)
        q = h.subscribe()
        _emit(h, logging.INFO, "live")
        got = q.get(timeout=0.5)
        assert got.message == "live"

    def test_unsubscribe_removes_queue(self):
        h = log_buffer.MemoryLogHandler(max_entries=5)
        q = h.subscribe()
        assert h.subscriber_count() == 1
        h.unsubscribe(q)
        assert h.subscriber_count() == 0

    def test_unsubscribe_idempotent(self):
        h = log_buffer.MemoryLogHandler(max_entries=5)
        q = h.subscribe()
        h.unsubscribe(q)
        # Second unsubscribe is a no-op, not an error.
        h.unsubscribe(q)
        assert h.subscriber_count() == 0

    def test_multiple_subscribers_each_receive_copy(self):
        h = log_buffer.MemoryLogHandler(max_entries=5)
        q1 = h.subscribe()
        q2 = h.subscribe()
        _emit(h, logging.INFO, "broadcast")
        assert q1.get(timeout=0.5).message == "broadcast"
        assert q2.get(timeout=0.5).message == "broadcast"

    def test_bounded_queue_drops_to_floor_on_full(self):
        # OV-4=A — overflowed entries silently drop; emit() never blocks.
        h = log_buffer.MemoryLogHandler(max_entries=200)
        q = h.subscribe()
        # Fill past SUBSCRIBER_QUEUE_DEPTH (64).
        n_emit = log_buffer.SUBSCRIBER_QUEUE_DEPTH + 20
        for i in range(n_emit):
            _emit(h, logging.INFO, f"m{i}")
        assert q.qsize() == log_buffer.SUBSCRIBER_QUEUE_DEPTH

    def test_slow_subscriber_does_not_block_emit(self):
        # Even with a totally stalled subscriber, emit() returns promptly.
        h = log_buffer.MemoryLogHandler(max_entries=500)
        h.subscribe()  # never drained
        t0 = time.monotonic()
        for i in range(200):
            _emit(h, logging.INFO, f"m{i}")
        elapsed = time.monotonic() - t0
        # 200 emits should be sub-second on any dev machine.
        assert elapsed < 1.0, f"emit() blocked: {elapsed}s for 200 calls"

    def test_subscribe_plus_since_seq_covers_reconnect_gap(self):
        """The documented SSE reconnect contract: a client records the
        last seq it saw, drops the connection, reconnects, and asks
        get_logs(since_seq=N) to backfill any entries that landed during
        the gap (including entries it would have received via the live
        stream but missed because the queue dropped to floor).
        """
        h = log_buffer.MemoryLogHandler(max_entries=50)
        for i in range(3):
            _emit(h, logging.INFO, f"pre{i}")
        # Client connects, records latest seq it knows about.
        last_seen = h.latest_seq()
        q = h.subscribe()
        # Emits happen while the client is connected.
        for i in range(3):
            _emit(h, logging.INFO, f"post{i}")
        # Client drains its queue (everything the SSE stream pushed).
        received: list[str] = []
        while not q.empty():
            received.append(q.get_nowait().message)
        # Connection drops. On reconnect the client asks for everything
        # newer than last_seen — the backfill must cover the same set.
        backfill = [e.message for e in h.get_logs(since_seq=last_seen)]
        backfill.reverse()  # get_logs returns newest-first
        assert backfill == ["post0", "post1", "post2"]
        assert received == backfill
        # Seq values are strictly monotonic across the full buffer.
        seqs = [e.seq for e in h.get_logs(limit=100)]
        assert seqs == sorted(seqs, reverse=True)
        assert len(set(seqs)) == len(seqs)


class TestSubscriberChurn:
    """Subscribers come and go (page navigations, network blips); emit
    must coexist with concurrent subscribe/unsubscribe without raising
    `list mutated during iteration` from _notify_subscribers."""

    def test_emit_and_subscriber_churn_concurrently(self):
        h = log_buffer.MemoryLogHandler(max_entries=200)
        stop = threading.Event()
        errors: list[BaseException] = []

        def writer() -> None:
            i = 0
            try:
                while not stop.is_set():
                    _emit(h, logging.INFO, f"m{i}")
                    i += 1
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        def churn() -> None:
            try:
                while not stop.is_set():
                    q = h.subscribe()
                    h.unsubscribe(q)
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=writer) for _ in range(2)]
        threads += [threading.Thread(target=churn) for _ in range(3)]
        for t in threads:
            t.start()
        time.sleep(0.3)
        stop.set()
        for t in threads:
            t.join(timeout=2.0)
        assert not errors, f"churn concurrency raised: {errors!r}"
        # All churned subscribers cleaned up properly.
        assert h.subscriber_count() == 0


class TestInitAndIntegration:
    def test_init_returns_handler_and_installs_on_root(self):
        h = log_buffer.init_memory_handler()
        assert h is log_buffer.get_memory_handler()
        assert h in logging.getLogger().handlers

    def test_init_is_idempotent(self):
        h1 = log_buffer.init_memory_handler()
        h2 = log_buffer.init_memory_handler()
        assert h1 is h2
        # Only one handler installed on root.
        root_handlers = [x for x in logging.getLogger().handlers if isinstance(x, log_buffer.MemoryLogHandler)]
        assert len(root_handlers) == 1

    def test_redacting_filter_installed_by_default(self):
        h = log_buffer.init_memory_handler()
        log = logging.getLogger("test.redaction")
        log.setLevel(logging.INFO)
        log.info("Save failed: PSK=secretlongstring1234567 message tail")
        entries = h.get_logs()
        assert "secretlongstring1234567" not in entries[0].message
        assert REDACTED_TOKEN in entries[0].message

    def test_redact_off_keeps_value(self):
        log_buffer.reset_for_tests()
        h = log_buffer.init_memory_handler(redact=False)
        log = logging.getLogger("test.no_redact")
        log.setLevel(logging.INFO)
        log.info("PSK=verysecret_value_12345")
        assert "verysecret_value_12345" in h.get_logs()[0].message

    def test_waitress_log_level_default_warning(self):
        log_buffer.init_memory_handler()
        assert logging.getLogger("waitress").level == logging.WARNING
        assert logging.getLogger("waitress.queue").level == logging.WARNING

    def test_waitress_log_level_override(self):
        log_buffer.reset_for_tests()
        # Reset waitress loggers so the next init sees a clean slate.
        for name in ("waitress", "waitress.queue"):
            logging.getLogger(name).setLevel(logging.NOTSET)
        log_buffer.init_memory_handler(waitress_log_level="ERROR")
        assert logging.getLogger("waitress").level == logging.ERROR

    def test_reset_for_tests_drops_handler(self):
        h = log_buffer.init_memory_handler()
        log_buffer.reset_for_tests()
        assert log_buffer.get_memory_handler() is None
        assert h not in logging.getLogger().handlers


class TestConcurrency:
    """Concurrent emit + get_logs MUST NOT raise `RuntimeError: deque
    mutated during iteration`. Without the lock, this test reliably trips
    that exception on CPython 3.11/3.12."""

    def test_emit_and_read_concurrently(self):
        h = log_buffer.MemoryLogHandler(max_entries=200)
        stop = threading.Event()
        errors: list[BaseException] = []

        def writer() -> None:
            i = 0
            try:
                while not stop.is_set():
                    _emit(h, logging.INFO, f"m{i}")
                    i += 1
            except BaseException as e:  # noqa: BLE001 — propagate to main
                errors.append(e)

        def reader() -> None:
            try:
                while not stop.is_set():
                    _ = h.get_logs(limit=50)
                    _ = h.total_count()
                    _ = h.latest_seq()
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=writer) for _ in range(3)]
        threads += [threading.Thread(target=reader) for _ in range(3)]
        for t in threads:
            t.start()
        time.sleep(0.5)
        stop.set()
        for t in threads:
            t.join(timeout=2.0)
        assert not errors, f"concurrency raised: {errors!r}"


class TestDrainInto:
    def test_drains_entries_until_send_fails(self):
        h = log_buffer.MemoryLogHandler(max_entries=10)
        q = h.subscribe()
        for i in range(5):
            _emit(h, logging.INFO, f"m{i}")
        received: list[str] = []

        def send(entry: log_buffer.LogEntry) -> None:
            received.append(entry.message)
            if len(received) >= 5:
                raise RuntimeError("client gone")

        log_buffer.drain_into(q, send, heartbeat_interval_s=0.1)
        assert received == ["m0", "m1", "m2", "m3", "m4"]

    def test_heartbeat_fires_on_idle(self):
        h = log_buffer.MemoryLogHandler(max_entries=10)
        q = h.subscribe()
        beats = {"n": 0}

        def hb() -> None:
            beats["n"] += 1
            if beats["n"] >= 2:
                raise RuntimeError("stop")

        def send(_entry: log_buffer.LogEntry) -> None:
            raise AssertionError("no entries should arrive")

        # No emits — drain_into should hit the queue.Empty timeout twice
        # and fire on_heartbeat each time.
        log_buffer.drain_into(q, send, heartbeat_interval_s=0.05, on_heartbeat=hb)
        assert beats["n"] == 2

    def test_stop_after_idle_returns_cleanly(self):
        """Without on_heartbeat, ``last_activity`` is never refreshed during
        idle, so ``stop_after_idle_s`` MUST fire as soon as the first idle
        window elapses. A bug here (wrong sign, missing branch) would
        only surface in production SSE where the client could see the
        stream stall instead of cleanly disconnecting."""
        h = log_buffer.MemoryLogHandler(max_entries=10)
        q = h.subscribe()
        sent: list[log_buffer.LogEntry] = []

        def send(e: log_buffer.LogEntry) -> None:
            sent.append(e)

        t0 = time.monotonic()
        log_buffer.drain_into(
            q,
            send,
            heartbeat_interval_s=0.05,
            stop_after_idle_s=0.1,
        )
        elapsed = time.monotonic() - t0
        # Should return between 0.1s and 0.5s. Sub-1s is plenty of margin
        # for CI scheduling jitter.
        assert elapsed < 1.0, f"drain_into didn't honor stop_after_idle_s: {elapsed}s"
        assert sent == []

    def test_drains_redacted_entries_to_subscriber(self):
        # Integration: filter applied before subscriber sees the entry.
        h = log_buffer.init_memory_handler()
        q = h.subscribe()
        log = logging.getLogger("test.drain.redaction")
        log.setLevel(logging.INFO)
        log.warning("Save failed: PSK=secrethunter2_abcde longstring1234567")
        got = q.get(timeout=0.5)
        assert "secrethunter2_abcde" not in got.message


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
