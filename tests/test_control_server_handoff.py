"""Tests for the post-WiFi PWA handoff (EPIC #383 PR2, #388).

Covers control_server/handoff.py + routes/handoff.py + the banner wiring:

- handoff state predicates (is_handoff_active, timezone_known via lat proxy).
- marker write (idempotent; sudo fallback when the direct write is denied).
- completion gating: auto/implicit triggers must NOT start a wrong-time clock
  (design-review A2) — they only complete when the timezone is known.
- POST /api/handoff/done (success / tz-unknown 409 / inactive idempotent).
- POST /api/handoff/set-timezone (valid / invalid / missing).
- 120s auto timer schedules + its callback respects the tz gate.
- Settings-save during handoff is an implicit completion.
- The banner renders + data-handoff-active is set only during the window.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import geocoding
from control_server import create_app  # noqa: E402
from control_server import handoff as handoff_mod  # noqa: E402

# ─── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def markers(tmp_path: Path):
    """Setup/handoff marker paths under a tmp dir (writable, so the direct
    marker write path is exercised without sudo)."""
    return {
        "setup": tmp_path / ".setup-complete",
        "handoff": tmp_path / ".handoff-complete",
    }


def _write_env(tmp_path: Path, *, lat: str = "", lon: str = "", **extra: str) -> str:
    lines = [
        f"export WEATHER_LATITUDE={lat}",
        f"export WEATHER_LONGITUDE={lon}",
        f"export WEATHER_LOCATION_NAME={extra.get('name', '')}",
        f"export WEATHER_UNITS={extra.get('units', 'imperial')}",
        f"export ALLOW_NSFW_QUOTES={extra.get('nsfw', 'false')}",
    ]
    p = tmp_path / "env.sh"
    p.write_text("\n".join(lines) + "\n")
    return str(p)


@pytest.fixture
def make_app(tmp_path: Path, markers):
    """Factory: build an app whose handoff state we control. ``located`` toggles
    whether IP-geo "succeeded" (coords present → tz known)."""

    def _make(*, setup: bool = True, handoff_done: bool = False, located: bool = False):
        if located:
            env_file = _write_env(tmp_path, lat="30.27", lon="-97.74", name="Austin, TX")
        else:
            env_file = _write_env(tmp_path)
        if setup:
            markers["setup"].touch()
        if handoff_done:
            markers["handoff"].touch()
        app = create_app(
            {
                "ENV_FILE": env_file,
                "VERSION_OVERRIDE": "v0.test",
                "SETUP_COMPLETE_FILE": str(markers["setup"]),
                "HANDOFF_COMPLETE_FILE": str(markers["handoff"]),
                "HANDOFF_TIMEOUT_S": 120.0,
            }
        )
        return app

    return _make


# ─── state predicates ─────────────────────────────────────────────────────


class TestStatePredicates:
    def test_inactive_before_setup(self, make_app, markers):
        markers["setup"].unlink(missing_ok=True)
        app = make_app(setup=False)
        assert handoff_mod.is_handoff_active(app) is False

    def test_active_after_setup_before_handoff(self, make_app):
        app = make_app(setup=True, handoff_done=False)
        assert handoff_mod.is_handoff_active(app) is True

    def test_inactive_after_handoff_complete(self, make_app):
        app = make_app(setup=True, handoff_done=True)
        assert handoff_mod.is_handoff_active(app) is False

    def test_timezone_known_tracks_latitude(self, make_app):
        assert handoff_mod.timezone_known(make_app(located=True)) is True
        assert handoff_mod.timezone_known(make_app(located=False)) is False


# ─── marker write ───────────────────────────────────────────────────────────


class TestMarkComplete:
    def test_writes_marker(self, make_app, markers):
        app = make_app()
        assert handoff_mod.mark_handoff_complete(app) is True
        assert markers["handoff"].exists()

    def test_idempotent(self, make_app, markers):
        app = make_app(handoff_done=True)
        # Already exists — still True, no error.
        assert handoff_mod.mark_handoff_complete(app) is True
        assert markers["handoff"].exists()

    def test_falls_back_to_sudo_when_direct_write_denied(self, make_app, markers, monkeypatch):
        app = make_app()

        def _deny(self):  # noqa: ANN001 — Path.touch signature
            raise PermissionError("read-only /etc")

        monkeypatch.setattr(handoff_mod.Path, "touch", _deny)

        recorded = {}

        def _fake_run(argv, **kwargs):  # noqa: ANN001
            recorded["argv"] = argv
            # Create the file WITHOUT Path.touch (it's monkeypatched to deny).
            open(str(markers["handoff"]), "w").close()  # simulate `sudo touch`
            return None

        monkeypatch.setattr(handoff_mod.subprocess, "run", _fake_run)
        assert handoff_mod.mark_handoff_complete(app) is True
        # Must shell out via sudo — /etc/litclock is root-owned and
        # control_server runs as pi. argv matches sudoers/020 verbatim.
        assert recorded["argv"] == ["sudo", handoff_mod._TOUCH, str(markers["handoff"])]


# ─── completion gating (A2: never start a wrong-time clock) ─────────────────


class TestCompletionGating:
    def test_completes_when_timezone_known(self, make_app, markers):
        app = make_app(located=True)
        assert handoff_mod.complete_if_timezone_known(app) is True
        assert markers["handoff"].exists()

    def test_blocks_when_timezone_unknown(self, make_app, markers):
        app = make_app(located=False)
        assert handoff_mod.complete_if_timezone_known(app) is False
        assert not markers["handoff"].exists()

    def test_noop_when_not_active(self, make_app):
        app = make_app(handoff_done=True)
        # Already complete → True, no-op.
        assert handoff_mod.complete_if_timezone_known(app) is True


# ─── 120s auto timer ────────────────────────────────────────────────────────


class TestAutoTimer:
    def test_schedules_with_configured_delay(self, make_app, monkeypatch):
        app = make_app(located=True)
        captured = {}

        class _FakeTimer:
            def __init__(self, delay, fn):
                captured["delay"] = delay
                captured["fn"] = fn
                self.daemon = False
                self.name = ""

            def start(self):
                captured["started"] = True

        monkeypatch.setattr(handoff_mod.threading, "Timer", _FakeTimer)
        handoff_mod.start_auto_timer(app)
        assert captured["delay"] == 120.0
        assert captured["started"] is True

    def test_callback_writes_when_located(self, make_app, markers, monkeypatch):
        app = make_app(located=True)
        captured = {}

        class _FakeTimer:
            def __init__(self, delay, fn):
                captured["fn"] = fn

            def start(self):
                pass

        monkeypatch.setattr(handoff_mod.threading, "Timer", _FakeTimer)
        handoff_mod.start_auto_timer(app, delay=0.01)
        captured["fn"]()  # fire the timer body synchronously
        assert markers["handoff"].exists()

    def test_callback_blocks_when_unlocated(self, make_app, markers, monkeypatch):
        app = make_app(located=False)
        captured = {}

        class _FakeTimer:
            def __init__(self, delay, fn):
                captured["fn"] = fn

            def start(self):
                pass

        monkeypatch.setattr(handoff_mod.threading, "Timer", _FakeTimer)
        handoff_mod.start_auto_timer(app, delay=0.01)
        captured["fn"]()
        assert not markers["handoff"].exists()


# ─── POST /api/handoff/done ─────────────────────────────────────────────────


class TestDoneEndpoint:
    def test_success_when_located(self, make_app, markers):
        client = make_app(located=True).test_client()
        r = client.post("/api/handoff/done")
        assert r.status_code == 200
        assert r.get_json()["ok"] is True
        assert markers["handoff"].exists()

    def test_409_when_timezone_unknown(self, make_app, markers):
        client = make_app(located=False).test_client()
        r = client.post("/api/handoff/done")
        assert r.status_code == 409
        assert r.get_json()["error"]["code"] == "timezone_required"
        assert not markers["handoff"].exists()

    def test_idempotent_when_inactive(self, make_app):
        client = make_app(handoff_done=True).test_client()
        r = client.post("/api/handoff/done")
        assert r.status_code == 200
        assert r.get_json()["ok"] is True


# ─── POST /api/handoff/set-timezone ─────────────────────────────────────────


class TestSetTimezoneEndpoint:
    def test_valid_timezone_sets_and_completes(self, make_app, markers, monkeypatch):

        monkeypatch.setattr(geocoding, "set_system_timezone", lambda tz: (True, None))
        client = make_app(located=False).test_client()
        r = client.post("/api/handoff/set-timezone", json={"timezone": "America/Chicago"})
        assert r.status_code == 200
        assert r.get_json()["timezone"] == "America/Chicago"
        assert markers["handoff"].exists()

    def test_invalid_timezone_rejected(self, make_app, markers, monkeypatch):

        monkeypatch.setattr(geocoding, "set_system_timezone", lambda tz: (False, "unknown tz"))
        client = make_app(located=False).test_client()
        r = client.post("/api/handoff/set-timezone", json={"timezone": "Mars/Olympus"})
        assert r.status_code == 422
        assert r.get_json()["error"]["code"] == "invalid_timezone"
        assert not markers["handoff"].exists()

    def test_missing_timezone_rejected(self, make_app):
        client = make_app(located=False).test_client()
        r = client.post("/api/handoff/set-timezone", json={})
        assert r.status_code == 422
        assert r.get_json()["error"]["code"] == "timezone_required"

    def test_non_dict_body_rejected(self, make_app):
        client = make_app(located=False).test_client()
        r = client.post("/api/handoff/set-timezone", json=["not-a-dict"])
        assert r.status_code == 422
        assert r.get_json()["error"]["code"] == "timezone_required"

    def test_noop_when_handoff_inactive(self, make_app, markers, monkeypatch):
        """Outside the handoff window this must NOT set the system timezone —
        it's not a permanent CSRF-less tz setter (Settings owns tz post-handoff)."""

        called = []
        monkeypatch.setattr(geocoding, "set_system_timezone", lambda tz: called.append(tz) or (True, None))
        client = make_app(handoff_done=True).test_client()
        r = client.post("/api/handoff/set-timezone", json={"timezone": "America/Chicago"})
        assert r.status_code == 200
        assert r.get_json()["ok"] is True
        assert called == []  # set_system_timezone never invoked


# ─── settings-save implicit completion ──────────────────────────────────────


class TestImplicitCompletionOnSave:
    def test_save_during_handoff_completes_when_located(self, make_app, markers, monkeypatch):
        app = make_app(located=True)
        # Stub the systemctl ad-hoc tick fired after a successful save.
        import control_server.routes.settings as settings_routes

        monkeypatch.setattr(settings_routes, "_ad_hoc_tick", lambda: None)
        client = app.test_client()
        from control_server.csrf import CSRF_ACTION

        token, _ = app.extensions["csrf_tokens"].issue(CSRF_ACTION)
        # JSON path: csrf_token in the body, Origin must match host (mirrors
        # tests/test_control_server_settings.py::TestApiSettingsPost).
        r = client.post(
            "/api/settings",
            json={"ALLOW_NSFW_QUOTES": "true", "csrf_token": token},
            headers={"Origin": "http://localhost"},  # matches test-client request.host
        )
        assert r.status_code == 200, r.get_data(as_text=True)
        assert markers["handoff"].exists()


# ─── banner rendering ───────────────────────────────────────────────────────


class TestBannerRendering:
    def test_banner_and_attr_present_during_handoff(self, make_app):
        client = make_app(located=True).test_client()
        body = client.get("/").get_data(as_text=True)
        assert "data-handoff-active" in body
        assert 'id="handoff-banner"' in body
        assert "Setup complete" in body
        assert "Austin, TX" in body

    def test_failure_state_banner(self, make_app):
        client = make_app(located=False).test_client()
        body = client.get("/").get_data(as_text=True)
        assert 'data-handoff-state="failure"' in body
        assert "handoff-set-tz" in body

    def test_no_banner_when_complete(self, make_app):
        client = make_app(handoff_done=True).test_client()
        body = client.get("/").get_data(as_text=True)
        assert "data-handoff-active" not in body
        assert 'id="handoff-banner"' not in body


# ─── #399 connected-SSID resolver + e-ink ctx plumbing ──────────────────────


class TestConnectedSsidResolver:
    """The handoff splash paints a "phone must be on this WiFi" caveat
    next to the QR (#399). The SSID it shows comes from
    ``handoff.connected_ssid()`` — these tests pin the resolver's
    contract: defensive against any failure, returns empty string (not
    None) on the no-WiFi / nmcli-missing / permissions-denied paths so
    splash callers can `if ssid:` cleanly."""

    def test_returns_ssid_from_wifi_provision(self, monkeypatch):
        """Happy path: wifi_provision.get_wifi_ssid returns the current
        SSID; the resolver passes it through (after a strip)."""
        import sys
        from unittest.mock import MagicMock

        mock_wifi = MagicMock()
        mock_wifi.get_wifi_ssid = lambda: "MyHomeWiFi"
        monkeypatch.setitem(sys.modules, "wifi_provision", mock_wifi)
        assert handoff_mod.connected_ssid() == "MyHomeWiFi"

    def test_strips_whitespace(self, monkeypatch):
        """nmcli output sometimes has trailing newlines — handoff.connected_ssid
        must strip so the splash centering math doesn't account for invisible
        glyphs."""
        import sys
        from unittest.mock import MagicMock

        mock_wifi = MagicMock()
        mock_wifi.get_wifi_ssid = lambda: "  MyHomeWiFi  \n"
        monkeypatch.setitem(sys.modules, "wifi_provision", mock_wifi)
        assert handoff_mod.connected_ssid() == "MyHomeWiFi"

    def test_returns_empty_when_wifi_provision_returns_none(self, monkeypatch):
        """No WiFi connection yet → wifi_provision returns None. Resolver
        must return "" so callers can use the truthy/falsy distinction."""
        import sys
        from unittest.mock import MagicMock

        mock_wifi = MagicMock()
        mock_wifi.get_wifi_ssid = lambda: None
        monkeypatch.setitem(sys.modules, "wifi_provision", mock_wifi)
        assert handoff_mod.connected_ssid() == ""

    def test_returns_empty_on_any_exception(self, monkeypatch):
        """Defensive contract: ANY exception from wifi_provision (nmcli
        missing, subprocess failure, permissions, import error) must be
        swallowed and return "". The caveat is decorative — failing the
        whole handoff render over an SSID lookup would be unconscionable."""
        import sys
        from unittest.mock import MagicMock

        mock_wifi = MagicMock()

        def _boom():
            raise RuntimeError("nmcli not found")

        mock_wifi.get_wifi_ssid = _boom
        monkeypatch.setitem(sys.modules, "wifi_provision", mock_wifi)
        assert handoff_mod.connected_ssid() == ""

    def test_returns_empty_when_wifi_provision_import_fails(self, monkeypatch):
        """A test/dev box without wifi_provision on the path must not
        cascade-fail the handoff splash render."""
        import sys

        # Force the lazy import to ModuleNotFoundError.
        monkeypatch.setitem(sys.modules, "wifi_provision", None)
        # Use the dict directly so the import statement raises.
        assert handoff_mod.connected_ssid() == ""


# ─── #388 fresh-flash fix: splash paints via a short-lived SUBPROCESS ────────


class TestRenderEinkSplashSubprocess:
    """control_server is LONG-LIVED, so it must paint the handoff splash via a
    short-lived subprocess (which frees the lgpio line claims on exit) — NOT
    in-process. An in-process paint holds the e-ink GPIO for the process
    lifetime, and litclock.service (the per-minute quote painter) then dies with
    lgpio 'GPIO busy', leaving the clock stuck on the splash (fresh-flash test-Pi
    QA 2026-07-06). Pin that the paint routes through the eink_display CLI's
    ``handoff-splash`` subcommand, never an in-process display_image()."""

    def _patch_ctx(self, monkeypatch):
        monkeypatch.setattr(handoff_mod, "handoff_context", lambda app: {"location": "Austin, Texas"})
        monkeypatch.setattr(handoff_mod, "current_timezone", lambda: "America/Chicago")
        monkeypatch.setattr(handoff_mod, "connected_ssid", lambda: "HomeWiFi")
        monkeypatch.setattr(handoff_mod, "qr_url", lambda app: "http://192.168.1.5")

    def test_paints_via_subprocess_handoff_splash_command(self, monkeypatch):
        import json

        self._patch_ctx(monkeypatch)
        calls = []

        class _Result:
            returncode = 0
            stderr = ""
            stdout = ""

        def _fake_run(cmd, **kw):
            calls.append((cmd, kw))
            return _Result()

        monkeypatch.setattr(handoff_mod.subprocess, "run", _fake_run)

        assert handoff_mod.render_eink_splash(app=object()) is True
        assert len(calls) == 1, "splash must be painted by exactly one subprocess"
        cmd, kw = calls[0]
        # Routes through the eink_display CLI's handoff-splash subcommand.
        assert cmd[1].endswith("eink_display.py")
        assert cmd[2] == "handoff-splash"
        assert "http://192.168.1.5" in cmd
        # The computed tz + ssid are carried as valid JSON.
        settings = json.loads(cmd[cmd.index("--settings-json") + 1])
        assert settings["timezone"] == "America/Chicago"
        assert settings["connected_ssid"] == "HomeWiFi"
        # Bounded + never raises into control_server startup.
        assert kw.get("timeout")
        assert kw.get("check") is False

    def test_returns_false_when_painter_subprocess_fails(self, monkeypatch):
        self._patch_ctx(monkeypatch)

        class _Result:
            returncode = 1
            stderr = "epd init failed"
            stdout = ""

        monkeypatch.setattr(handoff_mod.subprocess, "run", lambda cmd, **kw: _Result())
        assert handoff_mod.render_eink_splash(app=object()) is False

    def test_painter_exception_is_swallowed_non_fatal(self, monkeypatch):
        self._patch_ctx(monkeypatch)

        def _boom(cmd, **kw):
            raise OSError("no such file")

        monkeypatch.setattr(handoff_mod.subprocess, "run", _boom)
        # A painter failure must NOT crash control_server startup.
        assert handoff_mod.render_eink_splash(app=object()) is False

    def test_timeout_does_not_leak_argv_pii_into_log(self, monkeypatch, caplog):
        # /review: str(subprocess.TimeoutExpired) embeds the full argv, which
        # carries the settings JSON (SSID + location). The timeout handler must
        # NOT log the exception object, or that PII leaks into the diagnostics
        # log buffer + journald (redact_text doesn't scrub SSID/location).
        self._patch_ctx(monkeypatch)  # connected_ssid="HomeWiFi", location "Austin, Texas"

        def _timeout(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 20))

        monkeypatch.setattr(handoff_mod.subprocess, "run", _timeout)
        with caplog.at_level("WARNING"):
            assert handoff_mod.render_eink_splash(app=object()) is False
        log_text = " ".join(r.getMessage() for r in caplog.records)
        assert "HomeWiFi" not in log_text, "SSID leaked into the log"
        assert "Austin" not in log_text, "location leaked into the log"
        assert "settings-json" not in log_text, "argv leaked into the log"
        assert "timed out" in log_text, "the timeout itself should still be reported"
