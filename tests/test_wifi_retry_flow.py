"""Tests for WiFi retry flow in setup_server (#139).

Covers the background WiFi connect/retry logic added to fix the phone
disconnect issue on Pi Zero 2W (can't do AP+Station simultaneously).
"""

import io
import threading
import time
import urllib.parse

import pytest

import setup_server

# ── Helpers ──────────────────────────────────────────────────────────


class FakeRequest:
    """Minimal stand-in for a socket-backed HTTP request."""

    def __init__(self, method, path, body=""):
        body_bytes = body.encode()
        raw = (
            f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nContent-Length: {len(body_bytes)}\r\n\r\n"
        ).encode() + body_bytes
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()

    def makefile(self, mode, buffering=-1):
        if "r" in mode:
            return self.rfile
        return self.wfile


def make_handler(request):
    """Create a SetupHandler without actually opening a socket."""
    handler = setup_server.SetupHandler.__new__(setup_server.SetupHandler)
    handler.rfile = request.rfile
    handler.wfile = request.wfile
    handler.requestline = request.rfile.readline().decode().strip()
    handler.command = handler.requestline.split()[0]
    handler.path = handler.requestline.split()[1]
    handler.request_version = "HTTP/1.1"
    handler.headers = {}

    # Parse headers from the raw request
    import http.client

    handler.rfile.seek(0)
    handler.rfile.readline()  # skip request line
    handler.headers = http.client.parse_headers(handler.rfile)

    handler.client_address = ("127.0.0.1", 0)
    handler.server = type("FakeServer", (), {"server_name": "localhost", "server_port": 8080})()
    handler.close_connection = True
    handler.responses = {}
    return handler


def post_setup(handler):
    """Call do_POST on the handler."""
    handler.do_POST()
    handler.wfile.seek(0)
    return handler.wfile.read().decode()


@pytest.fixture(autouse=True)
def reset_globals(monkeypatch):
    """Reset module globals between tests.

    The connect-flow globals (``WIFI_CONNECT_ERROR`` / ``WIFI_CONNECT_IN_FLIGHT``)
    are reset by the autouse fixture in ``conftest.py`` via
    ``setup_server.reset_state()`` — that path waits for any in-flight
    background thread to drain before clearing, which closes the race that
    caused #355. We keep ``monkeypatch.setattr`` here only for the
    configuration globals that benefit from restore semantics.
    """
    monkeypatch.setattr(setup_server, "PROVISIONING_MODE", False)
    monkeypatch.setattr(setup_server, "SIGNAL_FILE", None)
    monkeypatch.setattr(setup_server, "ENV_FILE", None)
    yield


# ── _build_setup_html banner tests ──────────────────────────────────


class TestWiFiBanners:
    """Test the wifi_error_banner logic in _build_setup_html."""

    def test_no_banner_by_default(self, monkeypatch):
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", False)
        html = setup_server._build_setup_html()
        assert "Couldn&rsquo;t join your WiFi" not in html
        assert "Connecting to WiFi..." not in html

    def test_connecting_banner_when_in_flight(self, monkeypatch):
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", True)
        html = setup_server._build_setup_html()
        assert "Connecting to WiFi..." in html
        assert 'meta http-equiv="refresh"' in html
        assert "Couldn&rsquo;t join your WiFi" not in html

    def test_error_banner_on_wifi_failure(self, monkeypatch):
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", "Wrong password")
        html = setup_server._build_setup_html()
        assert "Couldn&rsquo;t join your WiFi" in html
        assert "Wrong password" in html
        assert "try again" in html

    def test_error_banner_escapes_html(self, monkeypatch):
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", '<script>alert("xss")</script>')
        html = setup_server._build_setup_html()
        # The error banner must escape the injected content
        assert "&lt;script&gt;" in html
        # The injected content must NOT appear unescaped in the banner area
        banner_start = html.index("Couldn&rsquo;t join your WiFi")
        banner_end = html.index("try again", banner_start)
        banner_region = html[banner_start:banner_end]
        assert "<script>" not in banner_region

    def test_error_banner_with_literal_braces_in_error(self, monkeypatch):
        """Curly braces in error messages pass through verbatim — the
        banner is interpolated into an f-string template, not .format(),
        so doubling braces would leak `{{` `}}` into the rendered HTML."""
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", "Error {bad}")
        # No KeyError — f-string interpolation does not re-evaluate the
        # interpolated value. `{bad}` stays as literal `{bad}` in output.
        html = setup_server._build_setup_html()
        assert "Error {bad}" in html
        assert "Error {{bad}}" not in html

    def test_in_flight_takes_priority_over_error(self, monkeypatch):
        """When both flags are set, in-flight banner wins (connection in progress)."""
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", True)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", "Some old error")
        html = setup_server._build_setup_html()
        assert "Connecting to WiFi..." in html
        assert "Couldn&rsquo;t join your WiFi" not in html


# ── Success page content variations ─────────────────────────────────


class TestSuccessPageVariations:
    """Test that HTML_SUCCESS renders differently for provisioning vs normal mode."""

    def test_provisioning_mode_success_page(self):
        """Provisioning mode: heading='Settings Saved!', has meta-refresh."""
        result = setup_server.HTML_SUCCESS.format(
            wifi="MyNetwork",
            location="40.7, -74.0",
            units="Fahrenheit",
            timezone="America/New_York",
            heading="Settings Saved!",
            subtitle="Connecting to <strong>MyNetwork</strong>...",
            meta_refresh='<meta http-equiv="refresh" content="15;url=/setup">\n    ',
            footer="This page will automatically check the connection status.",
        )
        assert "Settings Saved!" in result
        assert "Connecting to <strong>MyNetwork</strong>" in result
        assert 'meta http-equiv="refresh"' in result
        assert "check the connection status" in result

    def test_normal_mode_success_page(self):
        """Normal mode: heading='Setup Complete!', no meta-refresh."""
        result = setup_server.HTML_SUCCESS.format(
            wifi="Already connected",
            location="40.7, -74.0",
            units="Celsius",
            timezone="Europe/London",
            heading="Setup Complete!",
            subtitle="Your LitClock is configured and ready to display literary quotes.",
            meta_refresh="",
            footer="You can close this page now. The clock will start displaying shortly.",
        )
        assert "Setup Complete!" in result
        assert "ready to display literary quotes" in result
        assert 'meta http-equiv="refresh"' not in result
        assert "close this page" in result

    def test_ssid_with_special_chars(self):
        """WiFi SSIDs with HTML special chars are escaped in the success page."""
        import html

        ssid = 'My "Network" & <Friends>'
        result = setup_server.HTML_SUCCESS.format(
            wifi=html.escape(ssid),
            location="0, 0",
            units="C",
            timezone="UTC",
            heading="Settings Saved!",
            subtitle=f"Connecting to <strong>{html.escape(ssid)}</strong>...",
            meta_refresh="",
            footer="footer",
        )
        assert "&amp;" in result
        assert "&lt;Friends&gt;" in result
        assert "<Friends>" not in result


# ── do_POST WiFi connect flow ───────────────────────────────────────


class TestDoPostWiFiFlow:
    """Test the do_POST handler's WiFi connect/retry paths."""

    def _make_post_body(self, **overrides):
        defaults = {
            "wifi_ssid": "TestNetwork",
            "wifi_password": "secret123",
            "latitude": "40.7",
            "longitude": "-74.0",
            "units": "metric",
            "timezone": "",
        }
        defaults.update(overrides)
        return urllib.parse.urlencode(defaults)

    def _do_post(self, body):
        req = FakeRequest("POST", "/setup", body)
        handler = make_handler(req)
        post_setup(handler)
        return handler

    def test_provisioning_sends_settings_saved_heading(self, monkeypatch, tmp_env_file):
        """In provisioning mode with WiFi SSID, response says 'Settings Saved!' not 'Setup Complete!'"""
        import sys
        import types

        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)

        # Provide a fake wifi_provision so the background thread completes quickly
        fake_wp = types.ModuleType("wifi_provision")
        fake_wp.connect_to_wifi = lambda ssid, pw: (False, "test")
        fake_wp.teardown_hotspot = lambda: None
        sys.modules.pop("wifi_provision", None)
        sys.modules["wifi_provision"] = fake_wp

        kill_calls = []
        monkeypatch.setattr("os.kill", lambda pid, sig: kill_calls.append((pid, sig)))

        try:
            body = self._make_post_body()
            req = FakeRequest("POST", "/setup", body)
            handler = make_handler(req)
            response = post_setup(handler)

            assert "Settings Saved!" in response
            assert 'meta http-equiv="refresh"' in response

            # Wait for background thread to finish
            deadline = time.monotonic() + 3.0
            while setup_server.WIFI_CONNECT_IN_FLIGHT and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            sys.modules.pop("wifi_provision", None)

    def test_normal_mode_sends_setup_complete(self, monkeypatch, tmp_env_file, mocker):
        """In normal mode (no provisioning), response says 'Setup Complete!'"""
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", False)
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        monkeypatch.setattr("os.kill", lambda pid, sig: None)
        # Isolate from this branch's async side-effects (test only asserts the
        # response text). The normal-mode thread sleeps 1s, then runs the IP-geo
        # resolver, then `_schedule_self_terminate(delay=2.0)` — which spawns a
        # daemon thread that os.kill()s 2s LATER. Crucially that thread does NOT
        # set WIFI_CONNECT_IN_FLIGHT, so a naive IN_FLIGHT-poll returns
        # instantly, the test ends, monkeypatches revert, and the *real*
        # delayed-SIGTERM thread then fires into a later test's patched os.kill
        # (the flaky leak that surfaced under PR2's connecting-splash latency).
        # Fix: stub the resolver (no retry budget) + record the self-terminate
        # call, then wait until that call lands — proving the thread consumed
        # our stubs and never spawned a real delayed-kill thread. The 2s-delay
        # timing itself is covered by test_no_wifi_branch_delays_sigterm_by_2s.
        monkeypatch.setattr(setup_server, "_resolve_location_from_ip", lambda: None)
        schedule_calls = []
        monkeypatch.setattr(setup_server, "_schedule_self_terminate", lambda delay=0.0: schedule_calls.append(delay))

        body = self._make_post_body()
        req = FakeRequest("POST", "/setup", body)
        handler = make_handler(req)
        response = post_setup(handler)

        assert "Setup Complete!" in response
        assert 'meta http-equiv="refresh"' not in response

        # Wait until the background thread reaches the (stubbed) self-terminate
        # — guarantees it finished using our stubs before they revert.
        deadline = time.monotonic() + 4.0
        while not schedule_calls and time.monotonic() < deadline:
            time.sleep(0.05)
        assert schedule_calls == [2.0]

    def test_in_flight_guard_blocks_duplicate_submit(self, monkeypatch, tmp_env_file):
        """If WIFI_CONNECT_IN_FLIGHT is True, do_POST returns early (sends success but no new thread)."""
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", True)

        thread_count_before = threading.active_count()

        body = self._make_post_body()
        req = FakeRequest("POST", "/setup", body)
        handler = make_handler(req)
        response = post_setup(handler)

        # Response still sent (success page) but no new thread spawned
        assert "Settings Saved!" in response
        # Give a moment to see if any new thread appeared
        time.sleep(0.1)
        assert threading.active_count() <= thread_count_before + 1

    def test_flags_set_before_thread_spawn(self, monkeypatch, tmp_env_file):
        """WIFI_CONNECT_IN_FLIGHT must be True immediately after do_POST returns (set before thread)."""
        import sys
        import types

        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)

        # Provide a fake wifi_provision so the background thread completes
        fake_wp = types.ModuleType("wifi_provision")
        fake_wp.connect_to_wifi = lambda ssid, pw: (False, "test")
        fake_wp.teardown_hotspot = lambda: None
        sys.modules.pop("wifi_provision", None)
        sys.modules["wifi_provision"] = fake_wp
        monkeypatch.setattr("os.kill", lambda pid, sig: None)

        try:
            body = self._make_post_body()
            req = FakeRequest("POST", "/setup", body)
            handler = make_handler(req)
            post_setup(handler)

            # Flag must be True immediately — set in handler, before thread runs
            assert setup_server.WIFI_CONNECT_IN_FLIGHT is True

            # Wait for thread to complete
            deadline = time.monotonic() + 3.0
            while setup_server.WIFI_CONNECT_IN_FLIGHT and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            sys.modules.pop("wifi_provision", None)


# ── _connect_and_teardown background thread ─────────────────────────


class TestConnectAndTeardown:
    """Test the background WiFi connect thread's success/failure/exception paths.

    These tests exercise the _connect_and_teardown closure that runs in a
    background thread.  The thread does `from wifi_provision import ...`
    at runtime, so we inject a fake wifi_provision module into sys.modules
    *before* the POST and keep it alive until the thread finishes.
    """

    def _make_post_body(self, **overrides):
        defaults = {
            "wifi_ssid": "TestNetwork",
            "wifi_password": "secret123",
            "latitude": "40.7",
            "longitude": "-74.0",
            "units": "metric",
            "timezone": "",
        }
        defaults.update(overrides)
        return urllib.parse.urlencode(defaults)

    @staticmethod
    def _wait_for_thread(timeout=3.0):
        """Wait for background daemon threads to finish."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not setup_server.WIFI_CONNECT_IN_FLIGHT:
                return
            time.sleep(0.05)

    def test_post_rejects_own_hotspot_ssid(self, monkeypatch, tmp_env_file):
        """Submitting the clock's own hotspot SSID is rejected up front — no
        connect attempt, no teardown. The picker filters it out, but a stale
        page or scripted POST could still send it; connecting would tear down
        the AP and fail on the single-radio chip."""
        import sys
        import types

        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        monkeypatch.setattr(setup_server, "HOTSPOT_SSID", "LitClock-Setup")

        connect_calls = []
        fake_wp = types.ModuleType("wifi_provision")
        fake_wp.connect_to_wifi = lambda ssid, pw: connect_calls.append((ssid, pw)) or (True, None)
        fake_wp.teardown_hotspot = lambda: None
        fake_wp.create_hotspot = lambda ssid=None, password=None: None
        sys.modules.pop("wifi_provision", None)
        sys.modules["wifi_provision"] = fake_wp

        try:
            body = self._make_post_body(wifi_ssid="LitClock-Setup")
            req = FakeRequest("POST", "/setup", body)
            handler = make_handler(req)
            response = post_setup(handler)

            assert "own setup network" in response
            assert connect_calls == []  # never tried to join its own AP
            assert setup_server.WIFI_CONNECT_IN_FLIGHT is False
        finally:
            sys.modules.pop("wifi_provision", None)

    def test_wifi_failure_sets_error_flag(self, monkeypatch, tmp_env_file):
        """When connect_to_wifi returns failure, WIFI_CONNECT_ERROR is set and server stays alive."""
        import sys
        import types

        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        monkeypatch.setattr(setup_server, "HOTSPOT_SSID", "LitClock-Setup")
        monkeypatch.setattr(setup_server, "HOTSPOT_PASSWORD", "test1234")

        restore_calls = []
        fake_wp = types.ModuleType("wifi_provision")
        fake_wp.connect_to_wifi = lambda ssid, pw: (False, "Incorrect WiFi password")
        fake_wp.teardown_hotspot = lambda: None
        fake_wp.create_hotspot = lambda ssid=None, password=None: (
            restore_calls.append((ssid, password)) or {"ssid": ssid, "password": password, "ip": "10.42.0.1"}
        )
        # Remove any cached import so the thread gets our fake
        sys.modules.pop("wifi_provision", None)
        sys.modules["wifi_provision"] = fake_wp

        kill_calls = []
        monkeypatch.setattr("os.kill", lambda pid, sig: kill_calls.append((pid, sig)))

        try:
            body = self._make_post_body()
            req = FakeRequest("POST", "/setup", body)
            handler = make_handler(req)
            post_setup(handler)

            self._wait_for_thread()

            assert setup_server.WIFI_CONNECT_ERROR == "Incorrect WiFi password"
            assert setup_server.WIFI_CONNECT_IN_FLIGHT is False
            assert len(kill_calls) == 0  # Server stays alive for retry
            # Hotspot should be restored for retry
            assert len(restore_calls) == 1
            assert restore_calls[0] == ("LitClock-Setup", "test1234")
        finally:
            sys.modules.pop("wifi_provision", None)

    def test_wifi_success_signals_and_shuts_down(self, monkeypatch, tmp_env_file, tmp_path):
        """When connect_to_wifi succeeds, signal_completion is called and SIGTERM sent."""
        import os
        import signal
        import sys
        import types

        signal_file = str(tmp_path / "signal")
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        monkeypatch.setattr(setup_server, "SIGNAL_FILE", signal_file)

        connect_calls = []
        teardown_calls = []

        def fake_connect(ssid, pw):
            connect_calls.append((ssid, pw))
            return (True, None)

        def fake_teardown():
            teardown_calls.append(True)

        fake_wp = types.ModuleType("wifi_provision")
        fake_wp.connect_to_wifi = fake_connect
        fake_wp.teardown_hotspot = fake_teardown
        fake_wp.create_hotspot = lambda ssid=None, password=None: None  # Not called on success
        sys.modules.pop("wifi_provision", None)
        sys.modules["wifi_provision"] = fake_wp

        kill_calls = []
        monkeypatch.setattr("os.kill", lambda pid, sig: kill_calls.append((pid, sig)))

        try:
            body = self._make_post_body()
            req = FakeRequest("POST", "/setup", body)
            handler = make_handler(req)
            post_setup(handler)

            self._wait_for_thread()

            assert setup_server.WIFI_CONNECT_IN_FLIGHT is False
            assert setup_server.WIFI_CONNECT_ERROR is None
            assert connect_calls == [("TestNetwork", "secret123")]
            assert len(teardown_calls) == 1
            assert os.path.exists(signal_file)
            assert len(kill_calls) == 1
            assert kill_calls[0] == (os.getpid(), signal.SIGTERM)
        finally:
            sys.modules.pop("wifi_provision", None)

    def test_exception_in_teardown_keeps_server_alive(self, monkeypatch, tmp_env_file, tmp_path):
        """If teardown_hotspot raises, the server stays alive and flag is cleared."""
        import sys
        import types

        signal_file = str(tmp_path / "signal")
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        monkeypatch.setattr(setup_server, "SIGNAL_FILE", signal_file)

        fake_wp = types.ModuleType("wifi_provision")
        fake_wp.connect_to_wifi = lambda ssid, pw: (True, None)
        fake_wp.teardown_hotspot = lambda: (_ for _ in ()).throw(RuntimeError("NM dbus error"))
        fake_wp.create_hotspot = lambda ssid=None, password=None: {
            "ssid": ssid,
            "password": password,
            "ip": "10.42.0.1",
        }
        sys.modules.pop("wifi_provision", None)
        sys.modules["wifi_provision"] = fake_wp

        kill_calls = []
        monkeypatch.setattr("os.kill", lambda pid, sig: kill_calls.append((pid, sig)))

        try:
            body = self._make_post_body()
            req = FakeRequest("POST", "/setup", body)
            handler = make_handler(req)
            post_setup(handler)

            self._wait_for_thread()

            # Flag must be cleared even on exception (finally block)
            assert setup_server.WIFI_CONNECT_IN_FLIGHT is False
            # Server stays alive — no SIGTERM
            assert len(kill_calls) == 0
        finally:
            sys.modules.pop("wifi_provision", None)

    def test_wifi_failure_without_credentials_skips_restore(self, monkeypatch, tmp_env_file):
        """When hotspot credentials aren't set, failure skips restore gracefully."""
        import sys
        import types

        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        monkeypatch.setattr(setup_server, "HOTSPOT_SSID", None)
        monkeypatch.setattr(setup_server, "HOTSPOT_PASSWORD", None)

        restore_calls = []
        fake_wp = types.ModuleType("wifi_provision")
        fake_wp.connect_to_wifi = lambda ssid, pw: (False, "Network not found")
        fake_wp.teardown_hotspot = lambda: None
        fake_wp.create_hotspot = lambda ssid=None, password=None: (
            restore_calls.append((ssid, password)) or {"ssid": ssid, "password": password, "ip": "10.42.0.1"}
        )
        sys.modules.pop("wifi_provision", None)
        sys.modules["wifi_provision"] = fake_wp

        monkeypatch.setattr("os.kill", lambda pid, sig: None)

        try:
            body = self._make_post_body()
            req = FakeRequest("POST", "/setup", body)
            handler = make_handler(req)
            post_setup(handler)

            self._wait_for_thread()

            assert setup_server.WIFI_CONNECT_ERROR == "Network not found"
            # create_hotspot should NOT be called when credentials are missing
            assert len(restore_calls) == 0
        finally:
            sys.modules.pop("wifi_provision", None)

    def test_hotspot_restore_retries_on_failure(self, monkeypatch, tmp_env_file):
        """Hotspot restore retries up to 3 times if create_hotspot fails."""
        import sys
        import types

        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        monkeypatch.setattr(setup_server, "HOTSPOT_SSID", "LitClock-Setup")
        monkeypatch.setattr(setup_server, "HOTSPOT_PASSWORD", "test1234")

        restore_calls = []

        def flaky_create_hotspot(ssid=None, password=None):
            restore_calls.append((ssid, password))
            # Fail first two attempts, succeed on third
            if len(restore_calls) < 3:
                return None
            return {"ssid": ssid, "password": password, "ip": "10.42.0.1"}

        fake_wp = types.ModuleType("wifi_provision")
        fake_wp.connect_to_wifi = lambda ssid, pw: (False, "Incorrect WiFi password")
        fake_wp.teardown_hotspot = lambda: None
        fake_wp.create_hotspot = flaky_create_hotspot
        sys.modules.pop("wifi_provision", None)
        sys.modules["wifi_provision"] = fake_wp

        monkeypatch.setattr("os.kill", lambda pid, sig: None)

        try:
            body = self._make_post_body()
            req = FakeRequest("POST", "/setup", body)
            handler = make_handler(req)
            post_setup(handler)

            self._wait_for_thread(timeout=15)

            assert setup_server.WIFI_CONNECT_ERROR == "Incorrect WiFi password"
            assert len(restore_calls) == 3  # Retried 3 times
        finally:
            sys.modules.pop("wifi_provision", None)


# ── reset_state() helper (#355) ─────────────────────────────────────


class TestResetState:
    """Lock in the test-isolation contract that the conftest autouse
    fixture relies on (#355). If ``reset_state()`` ever stops clearing one
    of the connect-flow globals, an order-dependent flake re-opens — pin
    the behavior here so a regression surfaces in the failing assertion
    rather than as an intermittent CI failure."""

    def test_reset_state_clears_connect_globals(self):
        setup_server.WIFI_CONNECT_IN_FLIGHT = True
        setup_server.WIFI_CONNECT_ERROR = "leaked from prior test"
        setup_server._WIFI_SCAN_CACHE = "<option>stale</option>"
        setup_server._WIFI_SCAN_TIME = 12345

        setup_server.reset_state(wait_for_inflight=0.0)

        assert setup_server.WIFI_CONNECT_IN_FLIGHT is False
        assert setup_server.WIFI_CONNECT_ERROR is None
        assert setup_server._WIFI_SCAN_CACHE is None
        assert setup_server._WIFI_SCAN_TIME == 0

    def test_reset_state_drains_inflight_thread_before_clearing(self):
        # The drain loop is the actual #355 race fix: without it, a still-live
        # background thread can write WIFI_CONNECT_ERROR *after* reset_state
        # zeroes it, re-opening the order-dependent flake. Pin the behavior
        # by spawning a thread that mimics the production write pattern
        # (clear in-flight, then write an error) and asserting reset_state
        # waited for the write to complete before clearing.
        setup_server.WIFI_CONNECT_IN_FLIGHT = True
        setup_server.WIFI_CONNECT_ERROR = None

        def late_writer():
            time.sleep(0.05)
            setup_server.WIFI_CONNECT_ERROR = "late write that must not leak"
            setup_server.WIFI_CONNECT_IN_FLIGHT = False

        t = threading.Thread(target=late_writer, daemon=True)
        t.start()

        setup_server.reset_state(wait_for_inflight=1.0)
        t.join(timeout=0.5)

        # If the drain loop short-circuited (the bug), the late write would
        # have landed *after* reset_state's zeroing and we'd see the error
        # string here. The drain forces reset_state to observe IN_FLIGHT=False
        # *after* the late writer ran, so the post-write zeroing wins.
        assert setup_server.WIFI_CONNECT_ERROR is None
        assert setup_server.WIFI_CONNECT_IN_FLIGHT is False

    # NOTE: these #478 tests assert on THEIR OWN thread handle (returned by
    # _spawn_bg), never on the global _BG_THREADS count/emptiness — the registry
    # is shared module state, so a benign entry left by another test would make
    # a "== []" / "len == 1" assertion flaky (the very class of cross-test
    # coupling this file exists to prevent). Threads are gated on an Event so
    # cleanup is deterministic, not timing-dependent.

    def test_reset_state_joins_registered_background_thread(self):
        # #478 — the flag-drain only covers threads that set IN_FLIGHT. _delayed
        # (SIGTERM timer) and _resolve_and_signal do NOT, so a prior test's
        # thread could outlive its case and fire its (now next-test-monkeypatched)
        # os.kill/retry into the next test. reset_state must JOIN every thread
        # spawned via _spawn_bg: a still-running registered thread must have
        # finished (and left the registry) by the time reset_state returns.
        fired = []

        def _slow():
            time.sleep(0.2)
            fired.append(1)

        t = setup_server._spawn_bg(_slow, name="test-478-leak")
        assert fired == []  # still running at call time

        setup_server.reset_state(wait_for_inflight=2.0)

        assert fired == [1], "reset_state returned WITHOUT joining the background thread"
        assert not t.is_alive()
        with setup_server._BG_THREADS_LOCK:
            assert t not in setup_server._BG_THREADS, "finished thread not dropped from registry"

    def test_reset_state_join_bounded_then_retains_then_reaps(self):
        # A thread that outruns the join budget must NOT hang reset_state, must
        # STAY registered (so a later reset can retry — not clear-then-forget,
        # which would orphan a live daemon that can still write globals), and be
        # dropped once it finishes.
        release = threading.Event()
        t = setup_server._spawn_bg(lambda: release.wait(5.0), name="test-478-slow")

        start = time.monotonic()
        setup_server.reset_state(wait_for_inflight=0.1)  # times out the join
        elapsed = time.monotonic() - start

        assert elapsed < 1.0, f"reset_state should time out the join, took {elapsed:.2f}s"
        assert t.is_alive(), "the slow thread should NOT have finished within the budget"
        with setup_server._BG_THREADS_LOCK:
            assert t in setup_server._BG_THREADS, "still-live thread was forgotten, not retained"

        release.set()  # let it finish; a budgeted reset now reaps it
        setup_server.reset_state(wait_for_inflight=2.0)
        assert not t.is_alive()
        with setup_server._BG_THREADS_LOCK:
            assert t not in setup_server._BG_THREADS

    def test_reset_state_cancels_delayed_sigterm_without_firing(self, monkeypatch):
        # #478 safety: joining a real _delayed SIGTERM timer must NOT wait out
        # its sleep and then fire os.kill. At test-teardown time monkeypatch has
        # already reverted os.kill to the real one (conftest tears monkeypatch
        # down BEFORE the reset fixture), so a fired SIGTERM would kill the
        # runner. reset_state sets _BG_CANCEL to wake the timer so it exits
        # WITHOUT calling os.kill. (os.kill is mocked here so a regression is a
        # clean assertion failure, not a dead test process.)
        kills = []
        monkeypatch.setattr("os.kill", lambda pid, sig: kills.append((pid, sig)))

        setup_server._schedule_self_terminate(delay=5.0)  # spawns a real _delayed
        start = time.monotonic()
        setup_server.reset_state(wait_for_inflight=1.0)
        elapsed = time.monotonic() - start

        assert elapsed < 1.0, f"reset_state waited out the timer instead of cancelling it ({elapsed:.2f}s)"
        assert kills == [], "the delayed timer fired os.kill despite being cancelled"

    def test_reset_state_joins_before_clearing_globals(self):
        # #478 follow-up (/review): the join must happen BEFORE reset_state
        # clears the connect-flow globals — otherwise a late write from a tracked
        # thread survives the clear and leaks into the next test (the exact bug
        # class the fix targets). A refactor moving the clear ahead of the join
        # would leave the other tests green (their threads write no global), so
        # pin the ordering directly.
        setup_server.WIFI_CONNECT_ERROR = None

        def _late_writer():
            time.sleep(0.1)
            setup_server.WIFI_CONNECT_ERROR = "leak from a tracked thread"

        setup_server._spawn_bg(_late_writer, name="test-478-latewrite")
        setup_server.reset_state(wait_for_inflight=2.0)

        assert setup_server.WIFI_CONNECT_ERROR is None, (
            "reset_state cleared the globals BEFORE joining the tracked thread — the late write leaked past the clear"
        )


# ── #364: SIGTERM-ordering regression tests ─────────────────────────


class TestConnectAndTeardownOrdering:
    """Lock in the first-boot service-sequencing invariants surfaced in #364.

    The success path must call (in order):
      connect_to_wifi → teardown_hotspot → _resolve_location_from_ip →
      signal_completion → os.kill (SIGTERM) → clear WIFI_CONNECT_IN_FLIGHT

    ``signal_completion`` is the handoff to ``litclock-firstboot.service``;
    if it doesn't fire before SIGTERM, the post-setup transition breaks.
    ``os.kill`` MUST be queued before IN_FLIGHT clears so reset_state's
    drain barrier observes a fully-quiescent thread (#364 regression).
    """

    def _make_post_body(self, **overrides):
        defaults = {
            "wifi_ssid": "TestNetwork",
            "wifi_password": "secret123",
            "latitude": "40.7",
            "longitude": "-74.0",
            "units": "metric",
            "timezone": "",
        }
        defaults.update(overrides)
        return urllib.parse.urlencode(defaults)

    @staticmethod
    def _wait_for_thread(timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not setup_server.WIFI_CONNECT_IN_FLIGHT:
                return
            time.sleep(0.05)

    @staticmethod
    def _install_fake_wp(monkeypatch, *, connect_result=(True, None), teardown_raises=False, calls=None):
        import sys
        import types

        fake_wp = types.ModuleType("wifi_provision")
        if calls is None:
            calls = []

        def fake_connect(ssid, pw):
            calls.append(("connect_to_wifi", (ssid, pw)))
            return connect_result

        def fake_teardown():
            calls.append(("teardown_hotspot", ()))
            if teardown_raises:
                raise RuntimeError("nmcli timeout")

        fake_wp.connect_to_wifi = fake_connect
        fake_wp.teardown_hotspot = fake_teardown
        fake_wp.create_hotspot = lambda ssid=None, password=None: {
            "ssid": ssid,
            "password": password,
            "ip": "10.42.0.1",
        }

        sys.modules.pop("wifi_provision", None)
        sys.modules["wifi_provision"] = fake_wp
        return calls

    def test_success_path_call_ordering(self, monkeypatch, tmp_env_file, tmp_path):
        """Pin the success-path call order: connect → teardown → resolve →
        signal_completion → SIGTERM. This IS the first-boot handoff."""
        import sys

        signal_file = str(tmp_path / "signal-done")
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        monkeypatch.setattr(setup_server, "SIGNAL_FILE", signal_file)

        call_log = []
        self._install_fake_wp(monkeypatch, connect_result=(True, None), calls=call_log)
        monkeypatch.setattr(
            setup_server,
            "_resolve_location_from_ip",
            lambda: call_log.append(("_resolve_location_from_ip", ())),
        )
        # Wrap signal_completion to record without breaking its behavior.
        real_signal_completion = setup_server.signal_completion

        def wrapped_signal_completion():
            call_log.append(("signal_completion", ()))
            return real_signal_completion()

        monkeypatch.setattr(setup_server, "signal_completion", wrapped_signal_completion)

        def mock_kill(pid, sig):
            call_log.append(("os.kill", (pid, sig)))

        monkeypatch.setattr("os.kill", mock_kill)

        try:
            body = self._make_post_body()
            req = FakeRequest("POST", "/setup", body)
            handler = make_handler(req)
            post_setup(handler)
            self._wait_for_thread()
        finally:
            sys.modules.pop("wifi_provision", None)

        names = [name for name, _ in call_log]
        # Restrict to the calls we care about; do_POST may make other unrelated
        # calls but the relative order of these must be preserved.
        wanted = {"connect_to_wifi", "teardown_hotspot", "_resolve_location_from_ip", "signal_completion", "os.kill"}
        filtered = [n for n in names if n in wanted]
        assert filtered == [
            "connect_to_wifi",
            "teardown_hotspot",
            "_resolve_location_from_ip",
            "signal_completion",
            "os.kill",
        ], f"call order regression: {filtered}"

    def test_success_path_signal_completion_fires_before_sigterm(
        self,
        monkeypatch,
        tmp_env_file,
        tmp_path,
    ):
        """Strict ordering pin: signal_completion BEFORE os.kill."""
        import sys

        signal_file = str(tmp_path / "signal-done")
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        monkeypatch.setattr(setup_server, "SIGNAL_FILE", signal_file)

        timeline = []
        self._install_fake_wp(monkeypatch, connect_result=(True, None))

        real_signal_completion = setup_server.signal_completion

        def wrapped_signal_completion():
            timeline.append("signal_completion")
            return real_signal_completion()

        monkeypatch.setattr(setup_server, "signal_completion", wrapped_signal_completion)
        monkeypatch.setattr("os.kill", lambda pid, sig: timeline.append("os.kill"))

        try:
            body = self._make_post_body()
            req = FakeRequest("POST", "/setup", body)
            handler = make_handler(req)
            post_setup(handler)
            self._wait_for_thread()
        finally:
            sys.modules.pop("wifi_provision", None)

        assert timeline == ["signal_completion", "os.kill"]

    def test_success_path_in_flight_still_true_at_sigterm(
        self,
        monkeypatch,
        tmp_env_file,
        tmp_path,
    ):
        """#364 REGRESSION TEST.

        Capture WIFI_CONNECT_IN_FLIGHT at the moment os.kill is called.
        Must observe ``True`` — if a future refactor reorders SIGTERM
        after the IN_FLIGHT clear, reset_state's drain barrier could
        return claiming "thread quiescent" while a SIGTERM is still
        pending in the kernel queue. Uses a threading.Event sync barrier
        (codex C5) for deterministic capture.
        """
        import sys

        signal_file = str(tmp_path / "signal-done")
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        monkeypatch.setattr(setup_server, "SIGNAL_FILE", signal_file)

        self._install_fake_wp(monkeypatch, connect_result=(True, None))

        in_flight_at_sigterm = []
        os_kill_called = threading.Event()

        def mock_kill(pid, sig):
            in_flight_at_sigterm.append(setup_server.WIFI_CONNECT_IN_FLIGHT)
            os_kill_called.set()

        monkeypatch.setattr("os.kill", mock_kill)

        try:
            body = self._make_post_body()
            req = FakeRequest("POST", "/setup", body)
            handler = make_handler(req)
            post_setup(handler)

            assert os_kill_called.wait(timeout=3.0), "SIGTERM never called"
            self._wait_for_thread()
        finally:
            sys.modules.pop("wifi_provision", None)

        assert in_flight_at_sigterm == [True], (
            f"SIGTERM queued after IN_FLIGHT cleared (race re-introduced): snapshot={in_flight_at_sigterm}"
        )

    def test_success_path_in_flight_cleared_after_thread_exits(
        self,
        monkeypatch,
        tmp_env_file,
        tmp_path,
    ):
        """After the background thread exits, IN_FLIGHT is False — the
        outer-finally invariant from the C4 fix (nested try/finally)."""
        import sys

        signal_file = str(tmp_path / "signal-done")
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        monkeypatch.setattr(setup_server, "SIGNAL_FILE", signal_file)

        self._install_fake_wp(monkeypatch, connect_result=(True, None))
        monkeypatch.setattr("os.kill", lambda pid, sig: None)

        try:
            body = self._make_post_body()
            req = FakeRequest("POST", "/setup", body)
            handler = make_handler(req)
            post_setup(handler)
            self._wait_for_thread()
        finally:
            sys.modules.pop("wifi_provision", None)

        assert setup_server.WIFI_CONNECT_IN_FLIGHT is False

    def test_success_path_reset_state_drain_blocks_until_sigterm_queued(
        self,
        monkeypatch,
        tmp_env_file,
        tmp_path,
    ):
        """When reset_state's drain returns, os.kill was already called.

        Detects regression of the drain-barrier false-positive: if a
        future refactor moves IN_FLIGHT-clear before SIGTERM, the drain
        loop could exit early and the post-drain check would see
        os_kill_called still un-set.
        """
        import sys

        signal_file = str(tmp_path / "signal-done")
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        monkeypatch.setattr(setup_server, "SIGNAL_FILE", signal_file)

        self._install_fake_wp(monkeypatch, connect_result=(True, None))

        os_kill_called = threading.Event()
        monkeypatch.setattr("os.kill", lambda pid, sig: os_kill_called.set())

        try:
            body = self._make_post_body()
            req = FakeRequest("POST", "/setup", body)
            handler = make_handler(req)
            post_setup(handler)

            # Drain via reset_state instead of polling IN_FLIGHT directly,
            # mimicking the conftest autouse fixture's contract.
            setup_server.reset_state(wait_for_inflight=5.0)
        finally:
            sys.modules.pop("wifi_provision", None)

        # When reset_state returns, SIGTERM must have been queued already.
        assert os_kill_called.is_set(), (
            "reset_state returned before SIGTERM was queued — drain-barrier false positive (#364 regression)."
        )


class TestConnectAndTeardownFailureAndExceptionPaths:
    """Failure / exception paths must NOT signal completion and MUST NOT
    SIGTERM. IN_FLIGHT clears via the outer finally regardless."""

    def _make_post_body(self, **overrides):
        defaults = {
            "wifi_ssid": "TestNetwork",
            "wifi_password": "secret123",
            "latitude": "40.7",
            "longitude": "-74.0",
            "units": "metric",
            "timezone": "",
        }
        defaults.update(overrides)
        return urllib.parse.urlencode(defaults)

    @staticmethod
    def _wait_for_thread(timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not setup_server.WIFI_CONNECT_IN_FLIGHT:
                return
            time.sleep(0.05)

    def test_wifi_failure_does_not_signal_or_sigterm(self, monkeypatch, tmp_env_file, tmp_path):
        """connect_to_wifi returns (False, error): no signal, no SIGTERM,
        IN_FLIGHT cleared, _restore_hotspot called."""
        import sys
        import types

        signal_file = str(tmp_path / "signal-done")
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        monkeypatch.setattr(setup_server, "SIGNAL_FILE", signal_file)
        monkeypatch.setattr(setup_server, "HOTSPOT_SSID", "LitClock-Setup")
        monkeypatch.setattr(setup_server, "HOTSPOT_PASSWORD", "test1234")

        restore_calls = []
        signal_calls = []
        fake_wp = types.ModuleType("wifi_provision")
        fake_wp.connect_to_wifi = lambda ssid, pw: (False, "wrong password")
        fake_wp.teardown_hotspot = lambda: None
        fake_wp.create_hotspot = lambda ssid=None, password=None: (
            restore_calls.append((ssid, password)) or {"ssid": ssid, "password": password, "ip": "10.42.0.1"}
        )
        sys.modules.pop("wifi_provision", None)
        sys.modules["wifi_provision"] = fake_wp

        monkeypatch.setattr(
            setup_server,
            "signal_completion",
            lambda: signal_calls.append(True),
        )
        kill_calls = []
        monkeypatch.setattr("os.kill", lambda pid, sig: kill_calls.append((pid, sig)))

        try:
            body = self._make_post_body()
            req = FakeRequest("POST", "/setup", body)
            handler = make_handler(req)
            post_setup(handler)
            self._wait_for_thread()
        finally:
            sys.modules.pop("wifi_provision", None)

        assert setup_server.WIFI_CONNECT_ERROR == "wrong password"
        assert setup_server.WIFI_CONNECT_IN_FLIGHT is False
        assert signal_calls == [], "signal_completion must not fire on WiFi failure"
        assert kill_calls == [], "SIGTERM must not fire on WiFi failure"
        assert len(restore_calls) == 1

    def test_wifi_exception_does_not_signal_or_sigterm(self, monkeypatch, tmp_env_file, tmp_path):
        """connect_to_wifi raises: except branch fires, no signal, no SIGTERM."""
        import sys
        import types

        signal_file = str(tmp_path / "signal-done")
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        monkeypatch.setattr(setup_server, "SIGNAL_FILE", signal_file)
        monkeypatch.setattr(setup_server, "HOTSPOT_SSID", "LitClock-Setup")
        monkeypatch.setattr(setup_server, "HOTSPOT_PASSWORD", "test1234")

        signal_calls = []
        fake_wp = types.ModuleType("wifi_provision")

        def boom(ssid, pw):
            raise RuntimeError("nmcli crashed")

        fake_wp.connect_to_wifi = boom
        fake_wp.teardown_hotspot = lambda: None
        fake_wp.create_hotspot = lambda ssid=None, password=None: {
            "ssid": ssid,
            "password": password,
            "ip": "10.42.0.1",
        }
        sys.modules.pop("wifi_provision", None)
        sys.modules["wifi_provision"] = fake_wp

        monkeypatch.setattr(
            setup_server,
            "signal_completion",
            lambda: signal_calls.append(True),
        )
        kill_calls = []
        monkeypatch.setattr("os.kill", lambda pid, sig: kill_calls.append((pid, sig)))

        try:
            body = self._make_post_body()
            req = FakeRequest("POST", "/setup", body)
            handler = make_handler(req)
            post_setup(handler)
            self._wait_for_thread()
        finally:
            sys.modules.pop("wifi_provision", None)

        assert "nmcli crashed" in (setup_server.WIFI_CONNECT_ERROR or "")
        assert setup_server.WIFI_CONNECT_IN_FLIGHT is False
        assert signal_calls == []
        assert kill_calls == []

    def test_teardown_hotspot_exception_does_not_sigterm(
        self,
        monkeypatch,
        tmp_env_file,
        tmp_path,
    ):
        """teardown_hotspot raises mid-flow: success_completed stays False,
        no SIGTERM, IN_FLIGHT cleared."""
        import sys
        import types

        signal_file = str(tmp_path / "signal-done")
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        monkeypatch.setattr(setup_server, "SIGNAL_FILE", signal_file)

        fake_wp = types.ModuleType("wifi_provision")
        fake_wp.connect_to_wifi = lambda ssid, pw: (True, None)
        fake_wp.teardown_hotspot = lambda: (_ for _ in ()).throw(RuntimeError("NM dbus error"))
        fake_wp.create_hotspot = lambda ssid=None, password=None: {
            "ssid": ssid,
            "password": password,
            "ip": "10.42.0.1",
        }
        sys.modules.pop("wifi_provision", None)
        sys.modules["wifi_provision"] = fake_wp

        kill_calls = []
        monkeypatch.setattr("os.kill", lambda pid, sig: kill_calls.append((pid, sig)))

        try:
            body = self._make_post_body()
            req = FakeRequest("POST", "/setup", body)
            handler = make_handler(req)
            post_setup(handler)
            self._wait_for_thread()
        finally:
            sys.modules.pop("wifi_provision", None)

        assert kill_calls == [], "SIGTERM must not fire when teardown raises"
        assert setup_server.WIFI_CONNECT_IN_FLIGHT is False

    def test_connect_teardown_when_signal_completion_fails_no_sigterm(
        self,
        monkeypatch,
        tmp_env_file,
        tmp_path,
    ):
        """D4 (revised after codex review): signal_completion returns False on
        touch failure (bool, not raise). Caller's `if signal_completion()` check
        leaves success_completed=False; outer finally clears IN_FLIGHT and does
        NOT SIGTERM. Critically: does NOT fall into the except branch (which
        would call _restore_hotspot and destroy the working WiFi connection).
        """
        import sys

        signal_file = str(tmp_path / "signal-done")
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        monkeypatch.setattr(setup_server, "SIGNAL_FILE", signal_file)

        TestConnectAndTeardownOrdering._install_fake_wp(
            monkeypatch,
            connect_result=(True, None),
        )

        # signal_completion returns False — simulates touch() failure (e.g.,
        # tmpfs full). Production code's no-raise-just-return-False contract.
        monkeypatch.setattr(setup_server, "signal_completion", lambda: False)
        kill_calls = []
        monkeypatch.setattr("os.kill", lambda pid, sig: kill_calls.append((pid, sig)))

        try:
            body = self._make_post_body()
            req = FakeRequest("POST", "/setup", body)
            handler = make_handler(req)
            post_setup(handler)

            deadline = time.monotonic() + 3.0
            while setup_server.WIFI_CONNECT_IN_FLIGHT and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            sys.modules.pop("wifi_provision", None)

        assert kill_calls == [], "SIGTERM must not fire when signal_completion returns False"
        assert setup_server.WIFI_CONNECT_IN_FLIGHT is False, (
            "IN_FLIGHT must clear via outer finally even when signal_completion fails"
        )
        # Production records the failure reason for observability.
        assert "signal file write failed" in (setup_server.WIFI_CONNECT_ERROR or "").lower()

    def test_connect_teardown_signal_failure_does_not_restore_hotspot(
        self,
        monkeypatch,
        tmp_env_file,
        tmp_path,
    ):
        """Codex post-review Finding 2 regression test.

        After teardown_hotspot has dropped the AP and the user's WiFi station
        is up, a signal_completion failure must NOT route through the except
        branch — that branch calls _restore_hotspot on the same wlan0 and
        would destroy the working WiFi connection. Pin that _restore_hotspot
        is never called when signal_completion fails on the success path.
        """
        import sys

        signal_file = str(tmp_path / "signal-done")
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        monkeypatch.setattr(setup_server, "SIGNAL_FILE", signal_file)

        TestConnectAndTeardownOrdering._install_fake_wp(
            monkeypatch,
            connect_result=(True, None),
        )

        monkeypatch.setattr(setup_server, "signal_completion", lambda: False)

        restore_calls = []
        monkeypatch.setattr(
            setup_server,
            "_restore_hotspot",
            lambda create_hp: restore_calls.append(create_hp),
        )
        monkeypatch.setattr("os.kill", lambda pid, sig: None)

        try:
            body = self._make_post_body()
            req = FakeRequest("POST", "/setup", body)
            handler = make_handler(req)
            post_setup(handler)

            deadline = time.monotonic() + 3.0
            while setup_server.WIFI_CONNECT_IN_FLIGHT and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            sys.modules.pop("wifi_provision", None)

        assert restore_calls == [], (
            "_restore_hotspot MUST NOT be called when signal_completion fails on "
            "the success path — WiFi station is up; restoring the AP would destroy it"
        )


class TestNoWiFiBranchSigterm:
    """No-WiFi-form-data branch (normal-mode setup): post-EPIC-383 the
    handler spawns a daemon thread that runs the IP-geo resolver, then
    fires signal_completion + schedules SIGTERM after a 2s flush delay.
    Pre-pivot this all ran synchronously in the request thread; the move
    to a thread keeps the response flush instant while the resolver's
    retry budget (~13s worst case) sits in the background."""

    def _make_post_body(self, **overrides):
        # Post-EPIC-383 the form only collects WiFi credentials; empty values
        # for both fields still trigger the else branch in normal mode.
        defaults = {
            "wifi_ssid": "",
            "wifi_password": "",
        }
        defaults.update(overrides)
        return urllib.parse.urlencode(defaults)

    @staticmethod
    def _wait_for(predicate, timeout=3.0, interval=0.02):
        """Poll until predicate() is truthy or timeout. The daemon thread
        spawned by do_POST's else branch does a 1s sleep + resolver + signal +
        schedule — predicate gives the test something concrete to wait on."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return False

    def test_no_wifi_branch_signals_then_schedules_terminate(
        self,
        monkeypatch,
        tmp_env_file,
    ):
        """Signal fires from the daemon thread (after resolver), then the
        helper is called with delay=2.0 to flush the response before SIGTERM."""
        # Normal mode means do_POST routes to the else branch — no
        # WIFI_CONNECT_IN_FLIGHT interaction.
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", False)
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        # Stub out the resolver + sleep so the daemon thread finishes
        # quickly without making real network calls.
        monkeypatch.setattr(setup_server, "_resolve_location_from_ip", lambda: None)
        monkeypatch.setattr("time.sleep", lambda _s: None)

        signal_calls = []

        def fake_signal_completion():
            signal_calls.append(True)
            return True  # Production contract: returns True on success.

        monkeypatch.setattr(setup_server, "signal_completion", fake_signal_completion)

        schedule_calls = []
        monkeypatch.setattr(
            setup_server,
            "_schedule_self_terminate",
            lambda delay=0.0: schedule_calls.append(delay),
        )

        body = self._make_post_body()
        req = FakeRequest("POST", "/setup", body)
        handler = make_handler(req)
        post_setup(handler)

        # Daemon thread runs resolver (stubbed) → signal → schedule.
        assert self._wait_for(lambda: signal_calls and schedule_calls), (
            f"daemon thread didn't fire signal+schedule: signal={signal_calls} schedule={schedule_calls}"
        )
        assert signal_calls == [True]
        assert schedule_calls == [2.0]
        # No IN_FLIGHT activity on this branch.
        assert setup_server.WIFI_CONNECT_IN_FLIGHT is False

    def test_no_wifi_branch_delays_sigterm_by_2s(self, monkeypatch, tmp_env_file):
        """End-to-end: os.kill fires ≥1.5s after the resolver finishes (the
        scheduler's 2s flush delay), <6s. Uses the real helper, mocks
        os.kill and stubs the resolver so the timing isn't perturbed by
        retry backoff.

        Post-EPIC-383: the no-WiFi branch wraps everything in a daemon thread
        that does ``time.sleep(1) → resolver → signal → schedule(delay=2.0)``,
        so the floor is 1s + ~0 (stubbed resolver) + 2s = ~3s."""
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", False)
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        monkeypatch.setattr(setup_server, "_resolve_location_from_ip", lambda: None)

        kill_event = threading.Event()
        kill_times = []

        def mock_kill(pid, sig):
            kill_times.append(time.monotonic() - t0)
            kill_event.set()

        monkeypatch.setattr("os.kill", mock_kill)

        body = self._make_post_body()
        req = FakeRequest("POST", "/setup", body)
        handler = make_handler(req)
        t0 = time.monotonic()
        post_setup(handler)

        assert kill_event.wait(timeout=6.0), "SIGTERM never fired"
        # Floor: 1s response-flush sleep + ~0 stubbed resolver + 2s schedule delay = ~3s
        assert kill_times[0] >= 2.5, f"SIGTERM fired too early: {kill_times[0]}s"
        assert kill_times[0] < 6.0, f"SIGTERM fired too late: {kill_times[0]}s"

    def test_no_wifi_branch_signal_completion_failure_does_not_sigterm(
        self,
        monkeypatch,
        tmp_env_file,
    ):
        """Codex post-review fix for D4 (replaces the prior raise-based test).

        When signal_completion returns False in the no-WiFi branch,
        _schedule_self_terminate must NOT be called — SIGTERMing the server
        with no signal file written would leave firstboot.sh waiting forever
        for a handoff that never arrives. The HTTP success response has
        already been flushed at this point, so the user sees the success
        page; the server staying up lets them recover (resubmit, or
        firstboot.sh times out and re-enters AP mode on next boot).

        Note: the prior shape of this test asserted that signal_completion
        raised. Production was changed to bool-return (codex Finding 1) so
        the response-already-flushed problem doesn't cascade into a hung
        server with no recovery path.
        """
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", False)
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        # Stub resolver + sleep so the daemon thread finishes fast.
        monkeypatch.setattr(setup_server, "_resolve_location_from_ip", lambda: None)
        monkeypatch.setattr("time.sleep", lambda _s: None)

        # signal_completion now returns False instead of raising.
        signal_event = threading.Event()

        def fake_signal_completion():
            signal_event.set()
            return False

        monkeypatch.setattr(setup_server, "signal_completion", fake_signal_completion)

        schedule_calls = []
        monkeypatch.setattr(
            setup_server,
            "_schedule_self_terminate",
            lambda delay=0.0: schedule_calls.append(delay),
        )

        body = self._make_post_body()
        req = FakeRequest("POST", "/setup", body)
        handler = make_handler(req)

        # No raise — do_POST returns normally; daemon thread does the work.
        post_setup(handler)
        assert signal_event.wait(timeout=3.0), "signal_completion never fired in daemon thread"

        assert schedule_calls == [], (
            "_schedule_self_terminate must not run when signal_completion returns False — "
            "would SIGTERM with no signal file and leave firstboot.sh waiting forever (#364)"
        )
