"""Tests for control_server/routes/diagnostics.py /api/logs + SSE (#416 PR2 T7)."""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from control_server import create_app, log_buffer  # noqa: E402
from control_server.routes import diagnostics  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_log_buffer():
    log_buffer.reset_for_tests()
    # Root logger defaults to WARNING; bring it down so `log.info(...)`
    # actually reaches the handler under test.
    prior_root_level = logging.getLogger().level
    logging.getLogger().setLevel(logging.DEBUG)
    # Also reset the SSE registry between tests so prior sessions don't
    # bleed into the cap=6 check. Post-/review the registry is initialised
    # eagerly at module load (was racy lazy-init), so we clear in place
    # instead of reassigning to None.
    diagnostics._sse_registry.clear()
    # Reset the _lazy_cache too — kernel + os_release otherwise leak
    # across tests with different DIAG_OS_RELEASE_PATH overrides.
    diagnostics._lazy_cache.clear()
    yield
    log_buffer.reset_for_tests()
    diagnostics._sse_registry.clear()
    diagnostics._lazy_cache.clear()
    logging.getLogger().setLevel(prior_root_level)


@pytest.fixture
def app_with_buffer():
    """Build an app that has the in-memory buffer wired up."""
    log_buffer.init_memory_handler()
    return create_app({"ENV_FILE": "/tmp/_nonexistent.sh"})


def _emit(level: int, msg: str, name: str = "tst") -> None:
    """Push a record through the root logger so the installed
    MemoryLogHandler sees it."""
    logging.getLogger(name).log(level, msg)


class TestApiLogs:
    def test_returns_empty_when_buffer_empty(self, app_with_buffer):
        with app_with_buffer.test_client() as c:
            r = c.get("/api/logs")
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        assert body["entries"] == []
        assert body["total"] == 0
        assert body["latest_seq"] == 0

    def test_returns_entries_newest_first(self, app_with_buffer):
        # Emit through the logger so the singleton picks them up.
        logging.getLogger("t").setLevel(logging.INFO)
        _emit(logging.INFO, "first")
        _emit(logging.WARNING, "second")
        _emit(logging.ERROR, "third")
        with app_with_buffer.test_client() as c:
            r = c.get("/api/logs?limit=10")
        body = r.get_json()
        messages = [e["message"] for e in body["entries"]]
        assert messages == ["third", "second", "first"]

    def test_level_filter(self, app_with_buffer):
        logging.getLogger("t").setLevel(logging.INFO)
        _emit(logging.INFO, "info-one")
        _emit(logging.WARNING, "warn-one")
        _emit(logging.ERROR, "err-one")
        with app_with_buffer.test_client() as c:
            r = c.get("/api/logs?level=ERROR")
        body = r.get_json()
        assert [e["message"] for e in body["entries"]] == ["err-one"]

    def test_limit_clamped_to_max(self, app_with_buffer):
        logging.getLogger("t").setLevel(logging.INFO)
        for i in range(20):
            _emit(logging.INFO, f"m{i}")
        # Request 10000 — should clamp to MAX_ENTRIES.
        with app_with_buffer.test_client() as c:
            r = c.get("/api/logs?limit=10000")
        body = r.get_json()
        assert body["limit"] == log_buffer.MAX_ENTRIES
        assert len(body["entries"]) == 20  # only 20 exist

    def test_since_seq_filter(self, app_with_buffer):
        logging.getLogger("t").setLevel(logging.INFO)
        _emit(logging.INFO, "before")
        cutoff = log_buffer.get_memory_handler().latest_seq()
        _emit(logging.INFO, "after-1")
        _emit(logging.INFO, "after-2")
        with app_with_buffer.test_client() as c:
            r = c.get(f"/api/logs?since_seq={cutoff}")
        body = r.get_json()
        messages = [e["message"] for e in body["entries"]]
        assert "before" not in messages
        assert messages == ["after-2", "after-1"]

    def test_bad_limit_returns_400(self, app_with_buffer):
        with app_with_buffer.test_client() as c:
            r = c.get("/api/logs?limit=abc")
        assert r.status_code == 400
        assert r.get_json()["error"]["code"] == "bad_limit"

    def test_bad_since_seq_returns_400(self, app_with_buffer):
        with app_with_buffer.test_client() as c:
            r = c.get("/api/logs?since_seq=not-an-int")
        assert r.status_code == 400
        assert r.get_json()["error"]["code"] == "bad_since_seq"

    def test_bad_level_returns_400(self, app_with_buffer):
        # F-BAD-LEVEL regression: pre-/review the route silently returned
        # zero entries for any non-standard level value (no allowlist).
        with app_with_buffer.test_client() as c:
            r = c.get("/api/logs?level=foo")
        assert r.status_code == 400
        assert r.get_json()["error"]["code"] == "bad_level"

    @pytest.mark.parametrize("level", ["INFO", "WARNING", "ERROR", "DEBUG", "CRITICAL", "WARN"])
    def test_valid_levels_accepted(self, app_with_buffer, level):
        with app_with_buffer.test_client() as c:
            r = c.get(f"/api/logs?level={level}")
        assert r.status_code == 200

    def test_returns_503_when_buffer_uninitialized(self):
        # create_app() alone doesn't install the handler; only
        # app.py:main() does. So a test client without explicit init
        # should get a 503 envelope.
        log_buffer.reset_for_tests()
        app = create_app({"ENV_FILE": "/tmp/_nonexistent.sh"})
        with app.test_client() as c:
            r = c.get("/api/logs")
        assert r.status_code == 503
        assert r.get_json()["error"]["code"] == "log_buffer_unavailable"


class TestApiLogsStream:
    """SSE endpoint — we don't drain the full stream in unit tests
    (the generator runs the connection's lifetime). These tests verify
    the handshake, the sid contract, and the supersession registry."""

    def test_missing_sid_returns_400(self, app_with_buffer):
        with app_with_buffer.test_client() as c:
            r = c.get("/api/logs/stream")
        assert r.status_code == 400
        assert r.get_json()["error"]["code"] == "missing_sid"

    @pytest.mark.parametrize(
        "bad_sid",
        [
            # Length boundary cases.
            "ab",  # too short (< 4)
            "a" * 200,  # too long (> 128)
            # Disallowed ASCII characters.
            "has space",
            "has/slash",
            # Control characters: URL-encoded so they survive Werkzeug's
            # URL parser and reach the route's isalnum() shape gate
            # (#419 T9). A literal "\n" in the URL string is stripped by
            # the test-client URL builder; %00 / %0a are not.
            "null%00here",
            "newline%0aattack",
            # ``%`` is NOT URL-safe per the route's allowlist; the literal
            # ``%xx`` form encodes through and lands as the decoded byte,
            # which then fails isalnum().
            "percent%25raw",
            # Zero-width joiner — looks empty visually but isalnum() returns
            # True for many unicode chars; we want to reject anything that
            # isn't ASCII alphanumeric or ``-``/``_``.
            "zwj‍attack",
        ],
    )
    def test_bad_sid_shape_returns_400(self, app_with_buffer, bad_sid):
        with app_with_buffer.test_client() as c:
            r = c.get(f"/api/logs/stream?sid={bad_sid}")
        assert r.status_code == 400
        assert r.get_json()["error"]["code"] == "bad_sid"

    def test_bad_since_seq_returns_400(self, app_with_buffer):
        with app_with_buffer.test_client() as c:
            r = c.get("/api/logs/stream?sid=valid-1234&since_seq=oops")
        assert r.status_code == 400
        assert r.get_json()["error"]["code"] == "bad_since_seq"

    def test_returns_503_when_buffer_uninitialized(self):
        log_buffer.reset_for_tests()
        app = create_app({"ENV_FILE": "/tmp/_nonexistent.sh"})
        with app.test_client() as c:
            r = c.get("/api/logs/stream?sid=valid-1234")
        assert r.status_code == 503

    def test_valid_sid_starts_stream(self, app_with_buffer):
        with app_with_buffer.test_client() as c:
            r = c.get("/api/logs/stream?sid=valid-sid-9876")
        assert r.status_code == 200
        assert r.headers.get("Content-Type") == "text/event-stream"
        assert r.headers.get("Cache-Control", "").startswith("no-cache")

    # RFC 7230 §6.1 + PEP 3333 §"Other HTTP Features": these headers
    # are hop-by-hop and MUST be managed by the server (waitress), not
    # the WSGI app. Setting any of them in a Flask response causes
    # waitress to ``AssertionError`` on start_response → 500.
    HOP_BY_HOP_HEADERS = (
        "Connection",
        "Keep-Alive",
        "Proxy-Authenticate",
        "Proxy-Authorization",
        "TE",
        "Trailers",
        "Transfer-Encoding",
        "Upgrade",
    )

    @pytest.mark.parametrize(
        "path",
        [
            "/api/logs/stream?sid=valid-sid-9876",
            "/api/logs?limit=1",
            "/api/diagnostics",
            "/diagnostics",
        ],
    )
    def test_diagnostics_routes_do_not_set_hop_by_hop_headers(self, app_with_buffer, path):
        """v0.214.1 hotfix regression — parametrized over every
        diagnostics-package route so the next instance of this bug
        class gets caught wherever it lands, not just on
        /api/logs/stream where v0.214.0 happened to hit it.

        Flask test_client DOES preserve any header the app sets — it
        just doesn't ALSO raise AssertionError the way waitress would
        on a hop-by-hop name. So this assertion-style check is
        sufficient on its own to catch the regression cheaply, in
        milliseconds, with no waitress dependency. The slower
        ``test_sse_runs_under_waitress`` below is belt-and-suspenders
        for the case where werkzeug's behavior changes in the future
        or the bad header sneaks into a route this parametrize misses.
        """
        with app_with_buffer.test_client() as c:
            r = c.get(path)
        hop_by_hop_lower = {h.lower() for h in self.HOP_BY_HOP_HEADERS}
        present = {k for k in r.headers.keys() if k.lower() in hop_by_hop_lower}
        assert not present, (
            f"{path} response set hop-by-hop header(s) {present} — "
            "waitress will reject with 500. Drop them; the server "
            "manages connection lifetime."
        )

    def test_sse_runs_under_waitress(self, app_with_buffer):
        """v0.214.1 hotfix regression — the real production-server check.

        The Flask-test_client parametrize above catches the regression
        cheaply by inspecting the response's headers dict. This test
        is the belt-and-suspenders production parity check: spins up
        waitress on an ephemeral port in a thread, issues a raw
        HTTP/1.0 GET via stdlib socket, and asserts the response
        status line is 200 — not a 500 from waitress's PEP-3333
        enforcer. If a future werkzeug change masks the bad header
        from r.headers (unlikely but possible), this test still
        catches the production failure mode.

        Uses raw socket (not urllib) because urllib's body reader can
        block on waitress's stream chunking buffer; the status-line +
        headers state is the actual regression signal we want.

        ``threads=1`` matches the single-request scope of this test
        (4-thread default leaks 4 daemon threads into the pytest
        process). ``t.join`` cleanly reaps the worker on teardown so
        threading.enumerate() stays quiet across test runs.
        """
        import socket
        import threading

        from waitress.server import create_server

        # Bind to an ephemeral port to avoid collisions.
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()

        server = create_server(app_with_buffer, host="127.0.0.1", port=port, threads=1)
        t = threading.Thread(target=server.run, daemon=True)
        t.start()
        try:
            # create_server returns AFTER listen() so the port is
            # accepting connections immediately. No sleep needed.
            sock = socket.create_connection(("127.0.0.1", port), timeout=5)
            sock.sendall(b"GET /api/logs/stream?sid=waitress-test-1234 HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
            sock.settimeout(3.0)
            chunks: list[bytes] = []
            try:
                while True:
                    chunk = sock.recv(512)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if sum(len(c) for c in chunks) >= 400:
                        break
            except (TimeoutError, OSError):
                pass
            sock.close()
            data = b"".join(chunks).decode("utf-8", errors="replace")
            status_line = data.split("\r\n", 1)[0] if data else ""
            # Exact-match parse: 'HTTP/1.0 200 OK' → ['HTTP/1.0', '200', 'OK']
            parts = status_line.split(" ", 2)
            assert len(parts) >= 2 and parts[1] == "200", (
                f"waitress did not return 200 for SSE GET: {status_line!r}\n"
                f"Full first chunk: {data[:500]!r}\n"
                "Likely a hop-by-hop header in the response — v0.214.0 "
                "shipped with ``Connection: keep-alive`` which causes "
                "an AssertionError in waitress's PEP-3333 enforcer."
            )
            assert "text/event-stream" in data.lower(), f"Content-Type header missing from SSE response: {data[:500]!r}"
        finally:
            server.close()
            # Reap the worker thread so threading.enumerate() stays
            # quiet across the rest of the pytest run. 2s is plenty
            # for waitress to exit its accept loop after close().
            t.join(timeout=2.0)


class TestSseRegistry:
    """Unit-test the in-memory supersession + cap registry. The Flask
    integration test above only verifies the handshake; this exercise
    the LRU + same-sid replacement logic directly."""

    def test_cap_evicts_oldest(self):
        diagnostics._sse_registry.clear()
        from queue import Queue

        # Register one over the cap. LRU evict should fire.
        cap = diagnostics.SSE_MAX_CONCURRENT_STREAMS
        evicted_pre_cap: list = []
        for i in range(cap):
            session, superseded = diagnostics._register_sse(f"sid-{i}", Queue(maxsize=1))
            assert superseded is None
            evicted_pre_cap.append(session)
        # Now register one MORE; oldest (sid-0) should be evicted.
        session, superseded = diagnostics._register_sse(f"sid-{cap}", Queue(maxsize=1))
        assert superseded is not None
        assert superseded.sid == "sid-0"
        # Cleanup
        for s in evicted_pre_cap + [session]:
            diagnostics._unregister_sse(s.sid)

    def test_same_sid_supersedes_prior(self):
        diagnostics._sse_registry.clear()
        from queue import Queue

        s1, sup1 = diagnostics._register_sse("dup-sid", Queue(maxsize=1))
        assert sup1 is None
        s2, sup2 = diagnostics._register_sse("dup-sid", Queue(maxsize=1))
        assert sup2 is s1
        assert s2.sid == "dup-sid"
        diagnostics._unregister_sse("dup-sid")

    def test_unregister_is_idempotent(self):
        diagnostics._sse_registry.clear()
        from queue import Queue

        diagnostics._register_sse("solo", Queue(maxsize=1))
        diagnostics._unregister_sse("solo")
        diagnostics._unregister_sse("solo")  # second call — no error

    def test_unregister_by_identity_does_not_orphan_newer_session(self):
        # F-RACE-A regression (Codex reproduced this in-process):
        # 1) A registers sid=X
        # 2) B registers sid=X (same-sid replace; A is signalled to close)
        # 3) A's generator finally runs _unregister_sse(X) — without the
        #    identity check, B's live entry gets popped. The cap accounting
        #    drifts and an orphan subscriber stays attached to the buffer.
        # The fix: _unregister_sse(sid, session) only pops if the current
        # entry IS that session.
        diagnostics._sse_registry.clear()
        from queue import Queue

        a, _ = diagnostics._register_sse("dup", Queue(maxsize=1))
        b, sup = diagnostics._register_sse("dup", Queue(maxsize=1))
        assert sup is a
        # A's generator's finally tries to unregister using its OWN session.
        diagnostics._unregister_sse("dup", a)
        # B (the live session) must still be in the registry.
        assert diagnostics._sse_registry.get("dup") is b

    def test_capacity_eviction_close_reason_distinguishes(self):
        # F-CAP-EVT regression: evicted-by-cap sessions get
        # close_reason="capacity-exceeded"; same-sid replace gets
        # close_reason="superseded". The wire event mirrors the reason.
        diagnostics._sse_registry.clear()
        from queue import Queue

        cap = diagnostics.SSE_MAX_CONCURRENT_STREAMS
        for i in range(cap):
            diagnostics._register_sse(f"sid-{i}", Queue(maxsize=1))
        _, evicted_by_cap = diagnostics._register_sse("new-sid", Queue(maxsize=1))
        assert evicted_by_cap is not None
        assert evicted_by_cap.close_reason == "capacity-exceeded"
        # Same-sid replace uses a different reason.
        _, sup = diagnostics._register_sse("new-sid", Queue(maxsize=1))
        assert sup is not None
        assert sup.close_reason == "superseded"

    def test_concurrent_same_sid_register_is_safe(self):
        # Race coverage: 4 threads register the same sid simultaneously.
        # Exactly 1 ends as the live entry; the other 3 are returned as
        # superseded (with close_event signalled).
        diagnostics._sse_registry.clear()
        from queue import Queue

        superseded_seen: list = []
        lock = threading.Lock()

        def worker():
            _, sup = diagnostics._register_sse("racy-sid", Queue(maxsize=1))
            if sup is not None:
                with lock:
                    superseded_seen.append(sup)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(diagnostics._sse_registry) == 1
        assert len(superseded_seen) == 3


class TestSseFormat:
    def test_event_frame_shape(self):
        out = diagnostics._sse_format("entry", {"seq": 1, "message": "hello"})
        assert out.startswith("event: entry\n")
        assert out.endswith("\n\n")
        assert '"message": "hello"' in out

    def test_no_event_falls_back_to_default_handler(self):
        out = diagnostics._sse_format(None, {"k": "v"})
        assert "event:" not in out
        assert out.startswith("data: ")

    def test_json_payload_handles_unserializable_via_default_str(self):
        # default=str avoids 500ing if a future entry carries a non-JSON
        # value (e.g. a datetime).
        from datetime import UTC, datetime

        out = diagnostics._sse_format("entry", {"when": datetime.now(tz=UTC)})
        assert "when" in out


class TestEndToEndPump:
    """Drive _generate_sse() directly to verify backfill + hello frame +
    heartbeat behavior without going through Flask's response cycle.
    """

    def test_backfill_then_hello(self, app_with_buffer):
        logging.getLogger("t").setLevel(logging.INFO)
        # Snapshot the buffer's latest_seq BEFORE emitting so the SSE
        # backfill cutoff is anchored to "everything emitted from now on"
        # rather than a hardcoded 0. The hardcode silently turned brittle
        # whenever the test order changed or the buffer carried entries
        # from a prior test — replaced per #419 T10.
        handler = log_buffer.get_memory_handler()
        cutoff = handler.latest_seq() if handler is not None else 0
        _emit(logging.INFO, "before-1")
        _emit(logging.INFO, "before-2")
        with app_with_buffer.app_context():
            gen = diagnostics._generate_sse("test-pump-sid", since_seq=cutoff)
            # Pull a handful of frames synchronously. The generator
            # backfills (in chronological order) THEN sends the hello.
            frames = []
            for _ in range(3):
                try:
                    frames.append(next(gen))
                except StopIteration:
                    break
            # Backfill frames are event=entry.
            entry_frames = [f for f in frames if f.startswith("event: entry\n")]
            assert len(entry_frames) == 2
            assert any('"before-1"' in f for f in entry_frames)
            assert any('"before-2"' in f for f in entry_frames)
            # One hello frame appears after.
            hello_frames = [f for f in frames if f.startswith("event: hello\n")]
            assert len(hello_frames) == 1
        # Cleanup — close the generator + drop the registry entry.
        gen.close()

    def test_close_event_emits_superseded(self, app_with_buffer):
        with app_with_buffer.app_context():
            gen = diagnostics._generate_sse("supersedeable", since_seq=None)
            # Drain initial backfill + hello.
            next(gen)  # hello (no backfill since since_seq is None)
            # Trip the close event by registering a DIFFERENT generator
            # with the same sid. The old session.close_event gets set,
            # and the next get() loop iteration in the old generator
            # should yield "superseded" then return.
            session = diagnostics._sse_registry["supersedeable"]
            session.close_event.set()
            # Push one entry so the generator's get() returns immediately.
            logging.getLogger("t").info("triggers heartbeat path")
            # Iterate until we see the superseded frame or generator exits.
            seen_superseded = False
            for _ in range(20):
                try:
                    frame = next(gen)
                except StopIteration:
                    break
                if frame.startswith("event: superseded\n"):
                    seen_superseded = True
                    break
            assert seen_superseded, "close_event set but no superseded frame emitted"
        gen.close()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
