"""In-memory ring buffer + SSE subscriber model for /api/logs (#416).

Inspired by tuanchris/dune-weaver's ``MemoryLogHandler`` pattern. Adapted
for Flask + waitress (sync threading model — no asyncio queues) and bound
to the project's specific defensive postures:

1. **Bounded ring buffer.** :class:`collections.deque` with ``maxlen=500``
   silently evicts the oldest entry on overflow. Memory cost is ~125 KB
   steady-state.

2. **Lock-guarded iteration** (eng-review C3=A). ``deque.append`` is
   thread-safe in CPython, but iterating the deque while another thread is
   appending raises ``RuntimeError: deque mutated during iteration``. The
   handler grabs ``_lock`` for any read that walks the buffer.

3. **Redaction at the source** (eng-review OV-1=A). A
   :class:`~control_server._redaction.RedactingFilter` is installed
   alongside the handler so every entry is sanitized BEFORE it lands in the
   buffer. The buffer is privacy-clean by construction.

4. **Non-blocking emit + per-subscriber bounded queue** (eng-review OV-4=A).
   Naïve handler.emit() would invoke subscriber callbacks synchronously on
   whatever thread called ``log.info()`` — a slow SSE writer would stall the
   request handler that just wrote the log line. Pattern: each subscriber
   gets a :class:`queue.Queue(maxsize=64)`. ``emit()`` does
   ``put_nowait``; on queue-full we drop the entry to floor (the live
   stream may miss a few lines, but the request never blocks). A dedicated
   writer thread per subscriber drains the queue and flushes to the wire
   (instantiated by the SSE route, not by this module).

5. **Waitress access-log suppression** (eng-review C3=A). Waitress logs
   every successful request through ``logging.getLogger("waitress")``. With
   the buffer attached to root, the drawer would scroll
   ``GET /api/status HTTP/1.1 200`` every 30s × every connected user.
   :func:`init_memory_handler` sets the waitress logger level to WARNING
   so the live drawer shows signal, not poll noise. Override via
   ``LITCLOCK_WAITRESS_LOG_LEVEL`` (eng-review OV-Misc=A item 4).

The handler is installed exactly once, in ``app.py:main()`` after
``basicConfig`` and before ``serve(...)`` (eng-review C3=A — NOT in
``create_app()``, so tests building apps via the factory don't accumulate
handler state across test cases).
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from itertools import count
from typing import Final

from ._redaction import RedactingFilter

# --- Configuration ----------------------------------------------------------

# Ring buffer depth. 500 entries × ~250 bytes avg ≈ 125 KB. Tested at 1000;
# kept at 500 to leave headroom on the Pi Zero 2W's ~30 MB control_server
# RSS budget.
MAX_ENTRIES: Final[int] = 500

# Per-message size cap (#416 PR1 /review ASK-4=B). Truncates a misbehaving
# caller (e.g. ``log.info("body=%s", huge_request_body)``) so one bad emit
# can't blow the buffer's steady-state budget. 64 KB is generous enough to
# carry full Python tracebacks + Flask debug dumps unchanged; worst-case
# 500 × 64 KB = 32 MB, well inside the Pi's 512 MB RAM.
#
# Counted in BYTES (UTF-8 encoded), not characters — a char-count cap would
# let an emoji- or CJK-heavy payload eat 2-4× the worst case (one codepoint
# = up to 4 bytes in UTF-8). This is the literal bound on the RSS budget;
# the message-stored-as-str cost is identical regardless of encoding form.
MAX_MESSAGE_BYTES: Final[int] = 65536
TRUNCATION_MARKER: Final[str] = " […truncated]"

# Per-subscriber outbound queue. Sized to absorb a brief network blip (a
# phone going through a Wi-Fi/cellular handoff) without dropping; past
# that, drops to floor and the client refetches on reconnect via the
# /api/logs?since_seq= endpoint.
SUBSCRIBER_QUEUE_DEPTH: Final[int] = 64


@dataclass
class LogEntry:
    """One record's worth of data, formatted at emit-time for cheap reads.

    A bound sequence number lets the SSE client request "everything since
    last entry I saw" on reconnect (eng-review OV-4=A — bounded queue
    overflow drops to floor; the seq lets the client detect the gap).
    """

    seq: int
    timestamp: float
    level: str
    logger: str
    message: str

    def to_dict(self) -> dict[str, object]:
        """JSON-serialisable shape for /api/logs + SSE event payloads."""
        return {
            "seq": self.seq,
            "timestamp": self.timestamp,
            "level": self.level,
            "logger": self.logger,
            "message": self.message,
        }


# --- The handler ------------------------------------------------------------


class MemoryLogHandler(logging.Handler):
    """Stores log records in a bounded ring buffer + fans out to subscribers.

    Thread-safe. Append + lookup hold ``_lock``; subscriber list mutations
    hold ``_subscribers_lock``. The two locks are NEVER acquired in
    nested order — every method takes either one or the other, never both.
    Iteration helpers (``get_logs``) snapshot the buffer under the lock and
    return a plain list.
    """

    def __init__(self, max_entries: int = MAX_ENTRIES) -> None:
        super().__init__()
        self.max_entries = max_entries
        self._buffer: deque[LogEntry] = deque(maxlen=max_entries)
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue[LogEntry]] = []
        self._subscribers_lock = threading.Lock()
        self._seq = count(1)

    # -- emit / append --

    def emit(self, record: logging.LogRecord) -> None:
        """Capture ``record`` into the buffer and notify subscribers.

        Never raises. ``logging.Handler.handleError`` swallows unexpected
        failures (matches stdlib behavior; misformatted records are a
        caller bug, not ours).

        Includes ``exc_info`` traceback when present (PR1 adversarial
        pass) so ``log.exception("…")`` calls land in the drawer with the
        full stack — the helper paste is the primary support surface
        after #387's image-hardening disables SSH, and a traceback-less
        entry defeats the whole purpose.

        ``record.getMessage()`` is byte-truncated at
        :data:`MAX_MESSAGE_BYTES` (PR1 adversarial pass) so one
        misbehaving caller can't blow the RSS budget. Byte-counting
        protects against high-bytes-per-char content (CJK, emoji) where
        a char-count cap could blow the limit by 4×.
        """
        try:
            message = record.getMessage()
            if record.exc_info or record.exc_text:
                if record.exc_info:
                    import traceback as _traceback  # noqa: PLC0415 — lazy; cold path

                    tb = "".join(_traceback.format_exception(*record.exc_info))
                else:
                    # exc_text caches a previously-formatted traceback
                    # (e.g. when the same record is handed to multiple
                    # handlers).
                    tb = record.exc_text or ""
                # CRITICAL (PR1 adversarial pass): the RedactingFilter ran
                # on record.getMessage() BEFORE emit() was called — but
                # the appended traceback bypasses that pre-filtering. Run
                # the assembled text through redact_text() here so the
                # buffer-stored entry is privacy-clean regardless of what
                # the traceback contains (a raised exception's args
                # commonly carry the offending value: e.g. ValueError(
                # "bad PSK=hunter2") would otherwise leak verbatim).
                from ._redaction import redact_text as _redact  # noqa: PLC0415

                message = _redact(f"{message}\n{tb.rstrip()}")
            encoded = message.encode("utf-8", errors="replace")
            if len(encoded) > MAX_MESSAGE_BYTES:
                marker_bytes = TRUNCATION_MARKER.encode("utf-8")
                cap = MAX_MESSAGE_BYTES - len(marker_bytes)
                # `errors="ignore"` drops any half-encoded codepoint at the
                # cut so we don't ship invalid UTF-8 to the SSE wire.
                message = encoded[:cap].decode("utf-8", errors="ignore") + TRUNCATION_MARKER
            entry = LogEntry(
                seq=next(self._seq),
                timestamp=record.created,
                level=record.levelname,
                logger=record.name,
                message=message,
            )
            with self._lock:
                self._buffer.append(entry)
            self._notify_subscribers(entry)
        except Exception:
            self.handleError(record)

    # -- reads --

    def get_logs(
        self,
        limit: int | None = None,
        level: str | None = None,
        since_seq: int | None = None,
        order: str = "desc",
    ) -> list[LogEntry]:
        """Return a list of entries, bounded by ``limit``.

        ``level`` filters case-insensitively against the LogRecord levelname
        (``"INFO"`` / ``"WARNING"`` / etc). ``since_seq`` returns only
        entries strictly newer than the given seq — used by the SSE client
        on reconnect to catch up without re-emitting the entire ring.

        ``order`` controls the RETURN ordering of the limit-selected slice
        (per #419 D10):

        - ``"desc"`` (default) — newest first. Limit picks the newest N.
        - ``"asc"`` — oldest first. Limit STILL picks the newest N, then
          the slice is reordered ascending. SSE backfill wants "the N most
          recent in chronological order" — NOT "the oldest N in the
          buffer." Keeping the limit semantic stable across orders means
          callers can't accidentally swap "newest 4" for "oldest 4" by
          flipping a single keyword.

        Codex /review #419 flagged the alternative semantic (limit selects
        by order direction) as a trap for backfill/reconnect callers.
        """
        if order not in ("asc", "desc"):
            raise ValueError(f"order must be 'asc' or 'desc', got {order!r}")

        with self._lock:
            entries = list(self._buffer)

        if since_seq is not None:
            entries = [e for e in entries if e.seq > since_seq]
        if level:
            wanted = level.upper()
            entries = [e for e in entries if e.level == wanted]

        # deque iterates oldest-first; reverse for newest-first selection
        # so ``limit`` always means "the N most recent."
        entries.reverse()

        if limit is not None and limit > 0:
            entries = entries[:limit]

        if order == "asc":
            # Reorder the selected newest-N slice into chronological order.
            # ``limit=4, order='asc'`` returns "the 4 most recent, oldest
            # first" — NOT "the 4 oldest in the buffer."
            entries.reverse()

        return entries

    def total_count(self, level: str | None = None) -> int:
        """Buffer occupancy (optionally per-level). For UI display only.

        Short-circuits on the common ``level is None`` case so the badge
        counter doesn't materialise a 500-entry list just to ``len()`` it.
        """
        if level is None:
            with self._lock:
                return len(self._buffer)
        with self._lock:
            entries = list(self._buffer)
        wanted = level.upper()
        return sum(1 for e in entries if e.level == wanted)

    def latest_seq(self) -> int:
        """The seq of the newest entry, or 0 if empty. Used by the SSE
        endpoint as the floor for ``since_seq`` resume."""
        with self._lock:
            if not self._buffer:
                return 0
            return self._buffer[-1].seq

    def snapshot(
        self,
        limit: int | None = None,
        level: str | None = None,
        since_seq: int | None = None,
        order: str = "desc",
    ) -> tuple[list[LogEntry], int, int]:
        """Atomic ``(entries, total, latest_seq)`` under one lock acquire.

        ``GET /api/logs`` reads all three values per request; before #419
        PR2 the handler took the ``_lock`` three times (one per call). That
        leaves a window where ``total_count`` and ``latest_seq`` can
        reflect different buffer states across an in-flight ``emit()``,
        so the badge counter and the resume-seq could disagree.

        This method takes ``_lock`` ONCE and computes everything off the
        consistent snapshot. The args mirror :meth:`get_logs` for drop-in
        replacement; ``order`` defaults to ``"desc"`` to match the old
        per-call shape.
        """
        if order not in ("asc", "desc"):
            raise ValueError(f"order must be 'asc' or 'desc', got {order!r}")

        with self._lock:
            buffer_snapshot = list(self._buffer)
            buffer_len = len(self._buffer)
            latest = self._buffer[-1].seq if self._buffer else 0

        # Apply the same filters as get_logs, but against the snapshot.
        entries = buffer_snapshot
        if since_seq is not None:
            entries = [e for e in entries if e.seq > since_seq]
        wanted = level.upper() if level else None
        if wanted:
            entries = [e for e in entries if e.level == wanted]

        entries.reverse()
        if limit is not None and limit > 0:
            entries = entries[:limit]
        if order == "asc":
            entries.reverse()

        if wanted is None:
            total = buffer_len
        else:
            total = sum(1 for e in buffer_snapshot if e.level == wanted)

        return entries, total, latest

    # -- subscribers (SSE) --

    def subscribe(self) -> queue.Queue[LogEntry]:
        """Register a new SSE client. Returns the Queue the SSE writer
        thread should drain. Caller is responsible for calling
        :meth:`unsubscribe` on close (typically via the SSE generator's
        ``finally`` block)."""
        q: queue.Queue[LogEntry] = queue.Queue(maxsize=SUBSCRIBER_QUEUE_DEPTH)
        with self._subscribers_lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[LogEntry]) -> None:
        """Idempotent: removes the queue if present, no-ops otherwise.

        Single ``list.remove`` + ``ValueError`` catch instead of an ``in``
        membership test followed by ``remove`` — the prior pattern walked
        the subscribers list twice on every disconnect. Trivial today
        (cap is 6) but the pattern shouldn't grow with the cap.
        """
        with self._subscribers_lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def subscriber_count(self) -> int:
        """Used by the SSE endpoint to enforce the per-process cap (eng-
        review OV-2=A: global cap of 6 to backstop thread starvation)."""
        with self._subscribers_lock:
            return len(self._subscribers)

    def _notify_subscribers(self, entry: LogEntry) -> None:
        """Push to each subscriber's queue without blocking on slow consumers.

        On Queue.Full we drop to floor. The client detects the gap on next
        reconnect via the seq-since handshake and refetches missing
        entries from the SSE endpoint's ``since_seq=…`` query string.

        Snapshot-then-iterate releases the subscribers lock before any
        :meth:`queue.Queue.put_nowait` call. ``put_nowait`` still takes
        the queue's own internal lock; keeping that out from under the
        subscribers lock means subscribe/unsubscribe/count callers never
        wait for fan-out, and emit serializes only on a single cheap
        list copy (cap=6).
        """
        with self._subscribers_lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(entry)
            except queue.Full:
                # Drop to floor. Live drawer may miss a line; the client
                # backfills via the SSE endpoint's since_seq= on reconnect.
                continue

    # -- maintenance --

    def clear(self) -> None:
        """Drop all entries. Wired only to test fixtures.

        There is no public route that calls this — /plan-eng-review OV-3=A
        dropped the would-be ``DELETE /api/logs`` endpoint because
        clearing in-memory state is a write, and issue #416 explicitly
        forbids any writes from the diagnostics surface. The drawer's
        "start fresh from now" feature is a client-side filter, not a
        server-side reset.
        """
        with self._lock:
            self._buffer.clear()


# --- Module-level singleton + bootstrap -------------------------------------

_singleton: MemoryLogHandler | None = None

# Stashed during install so reset_for_tests() can restore the prior
# state. Without this, a test that called ``init_memory_handler(
# waitress_log_level="ERROR")`` and then ``reset_for_tests()`` would
# leave the waitress loggers at ERROR forever — the next test starting
# fresh would silently inherit it.
_prior_waitress_levels: dict[str, int] = {}


def get_memory_handler() -> MemoryLogHandler | None:
    """Return the installed handler, or None if init was never called.

    Routes use this lazily so they can degrade to a 503 envelope when
    ``app.py:main()`` hasn't installed the handler (the common test-client
    path where ``create_app()`` is used directly)."""
    return _singleton


def init_memory_handler(
    max_entries: int = MAX_ENTRIES,
    *,
    waitress_log_level: str | None = None,
    redact: bool = True,
    install_on: logging.Logger | None = None,
) -> MemoryLogHandler:
    """Install the singleton handler on the root logger.

    Called from ``app.py:main()`` after ``logging.basicConfig`` and before
    ``serve(...)``. Idempotent: a second call returns the existing handler.

    ``waitress_log_level`` overrides the default WARNING for the
    ``waitress`` and ``waitress.queue`` loggers. Defaults to the
    ``LITCLOCK_WAITRESS_LOG_LEVEL`` env var, or WARNING when unset.
    """
    global _singleton  # noqa: PLW0603 — module-level singleton is intentional
    if _singleton is not None:
        return _singleton

    handler = MemoryLogHandler(max_entries=max_entries)
    handler.setLevel(logging.INFO)
    if redact:
        handler.addFilter(RedactingFilter())

    root = install_on if install_on is not None else logging.getLogger()
    root.addHandler(handler)

    # Hush the waitress access log so the live drawer doesn't drown in
    # poll noise. Per eng-review OV-Misc=A item 4: env-overridable.
    # Stash the prior levels so reset_for_tests() can restore them;
    # without that, a test that mutates the levels leaks state to every
    # following test that uses the root logger.
    level_name = waitress_log_level or os.environ.get("LITCLOCK_WAITRESS_LOG_LEVEL", "WARNING")
    level = getattr(logging, level_name.upper(), logging.WARNING)
    for name in ("waitress", "waitress.queue"):
        lg = logging.getLogger(name)
        _prior_waitress_levels[name] = lg.level
        lg.setLevel(level)

    _singleton = handler
    return handler


def reset_for_tests() -> None:
    """Tear the singleton down + restore mutated logger levels.

    Pytest fixtures call this between cases so the buffer + subscriber list
    + waitress-logger-level state doesn't leak across tests. The waitress
    level restore was added after review caught that the original reset
    only handled the singleton and let mutated levels persist
    indefinitely between cases.
    """
    global _singleton  # noqa: PLW0603
    if _singleton is not None:
        root = logging.getLogger()
        if _singleton in root.handlers:
            root.removeHandler(_singleton)
        _singleton = None
    for name, prior_level in _prior_waitress_levels.items():
        logging.getLogger(name).setLevel(prior_level)
    _prior_waitress_levels.clear()


__all__ = [
    "MAX_ENTRIES",
    "MAX_MESSAGE_BYTES",
    "SUBSCRIBER_QUEUE_DEPTH",
    "TRUNCATION_MARKER",
    "LogEntry",
    "MemoryLogHandler",
    "drain_into",
    "get_memory_handler",
    "init_memory_handler",
    "reset_for_tests",
]


# --- Free-form helper for the SSE writer thread -----------------------------


def drain_into(
    q: queue.Queue[LogEntry],
    send: Callable[[LogEntry], None],
    *,
    heartbeat_interval_s: float = 15.0,
    on_heartbeat: Callable[[], None] | None = None,
    stop_after_idle_s: float | None = None,
) -> None:
    """Pump entries from ``q`` to the wire via ``send`` (blocking generator
    helper, not a thread starter).

    Tucked here so the route module's SSE generator stays a simple wrapper.
    Returns when:
    - ``send`` raises (the client disconnected) — caller should unsubscribe.
    - ``stop_after_idle_s`` elapses with no entries AND no heartbeat fires.
    - ``on_heartbeat`` raises (rare; same shape as ``send``).
    """
    last_activity = time.monotonic()
    while True:
        try:
            entry = q.get(timeout=heartbeat_interval_s)
        except queue.Empty:
            if on_heartbeat is not None:
                try:
                    on_heartbeat()
                except Exception:
                    break
                last_activity = time.monotonic()
            if stop_after_idle_s is not None and (time.monotonic() - last_activity) > stop_after_idle_s:
                break
            continue
        try:
            send(entry)
        except Exception:
            # Client gone; caller's finally block runs unsubscribe.
            break
        last_activity = time.monotonic()
