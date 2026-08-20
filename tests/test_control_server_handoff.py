"""Tests for the post-WiFi PWA handoff (EPIC litclock-dev#383 PR2, litclock-dev#388).

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

import os
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
        assert handoff_mod.mark_handoff_complete(app, handoff_mod.TRIGGER_DONE_BUTTON) is True
        assert markers["handoff"].exists()

    def test_idempotent(self, make_app, markers):
        app = make_app(handoff_done=True)
        # Already exists — still True, no error.
        assert handoff_mod.mark_handoff_complete(app, handoff_mod.TRIGGER_DONE_BUTTON) is True
        assert markers["handoff"].exists()

    def test_falls_back_to_sudo_when_direct_write_denied(self, make_app, markers, monkeypatch, caplog):
        """The PRODUCTION path, and the only one the device ever runs.

        /etc/litclock is root-owned and control_server runs as pi, so the direct
        O_EXCL create always fails EACCES on a Pi. Every test here uses a
        writable tmp dir, so without this the fix's payload — the completion log
        — was exercised only on a path production never takes. /review found the
        sudo-path log line mutation-survivable for exactly that reason.
        """
        app = make_app()
        real_open = handoff_mod.os.open

        def _deny(path, flags, *args):  # noqa: ANN001
            if str(path) == str(markers["handoff"]):
                raise PermissionError("read-only /etc")
            return real_open(path, flags, *args)

        monkeypatch.setattr(handoff_mod.os, "open", _deny)

        recorded = {}

        def _fake_run(argv, **kwargs):  # noqa: ANN001
            recorded["argv"] = argv
            # Create the file via the UNPATCHED os.open — Path.touch() would go
            # back through the denied one, and the real `sudo touch` is a
            # separate process that the patch never reaches.
            os.close(real_open(str(markers["handoff"]), os.O_CREAT | os.O_WRONLY, 0o644))
            return None

        monkeypatch.setattr(handoff_mod.subprocess, "run", _fake_run)
        with caplog.at_level("INFO", logger="control_server.handoff"):
            assert handoff_mod.mark_handoff_complete(app, handoff_mod.TRIGGER_DONE_BUTTON) is True

        # Must shell out via sudo — /etc/litclock is root-owned and
        # control_server runs as pi. argv matches sudoers/020 verbatim.
        assert recorded["argv"] == ["sudo", handoff_mod._TOUCH, str(markers["handoff"])]
        assert f"{handoff_mod.COMPLETED_VIA_PREFIX} {handoff_mod.TRIGGER_DONE_BUTTON}" in caplog.text, (
            "the sudo path is the only one a Pi runs; its completion line must be attributed too"
        )

    def test_sudo_reporting_success_but_writing_nothing_is_not_silent(
        self, make_app, markers, monkeypatch, caplog
    ):
        """sudo returned 0 and the marker still isn't there. Rare, and the
        fallback timer will retry — but the caller returns False and every
        caller reads that as "try later", so it must not pass unremarked."""
        app = make_app()
        real_open = handoff_mod.os.open

        def _deny(path, flags, *args):  # noqa: ANN001
            if str(path) == str(markers["handoff"]):
                raise PermissionError("read-only /etc")
            return real_open(path, flags, *args)

        monkeypatch.setattr(handoff_mod.os, "open", _deny)
        monkeypatch.setattr(handoff_mod.subprocess, "run", lambda argv, **kw: None)  # writes nothing

        with caplog.at_level("INFO", logger="control_server.handoff"):
            assert handoff_mod.mark_handoff_complete(app, handoff_mod.TRIGGER_DONE_BUTTON) is False

        assert handoff_mod.COMPLETED_VIA_PREFIX not in caplog.text, "must not claim a completion that did not happen"
        assert "is still absent" in caplog.text


# ─── completion gating (A2: never start a wrong-time clock) ─────────────────


class TestCompletionGating:
    def test_completes_when_timezone_known(self, make_app, markers):
        app = make_app(located=True)
        assert handoff_mod.complete_if_timezone_known(app, handoff_mod.TRIGGER_SETTINGS_SAVE) is True
        assert markers["handoff"].exists()

    def test_blocks_when_timezone_unknown(self, make_app, markers):
        app = make_app(located=False)
        assert handoff_mod.complete_if_timezone_known(app, handoff_mod.TRIGGER_SETTINGS_SAVE) is False
        assert not markers["handoff"].exists()

    def test_noop_when_not_active(self, make_app):
        app = make_app(handoff_done=True)
        # Already complete → True, no-op.
        assert handoff_mod.complete_if_timezone_known(app, handoff_mod.TRIGGER_SETTINGS_SAVE) is True


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


# ─── litclock-dev#399 connected-SSID resolver + e-ink ctx plumbing ──────────────────────


class TestConnectedSsidResolver:
    """The handoff splash paints a "phone must be on this WiFi" caveat
    next to the QR (litclock-dev#399). The SSID it shows comes from
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


# ─── litclock-dev#388 fresh-flash fix: splash paints via a short-lived SUBPROCESS ────────


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


# ─── completion attribution (litclock-dev#646) ───────────────────────────────────────
#
# journald is the only diagnostic channel on this device, so "how did the
# handoff complete?" is answerable only from these lines. Asserting that
# .handoff-complete EXISTS proves nothing here — it is written correctly on
# every path, and was written correctly the whole time the bug was live. The
# defect was purely in what got reported, so these assert the log content
# against the path that actually did the work.
#
# Hardware tally from the dev-20260815-b0c0590 bench run, three completions and
# three misattributions:
#
#   cycle | actual path   | completed | timer logged | mtime moved?
#   ------|---------------|-----------|--------------|-------------
#     2   | Done button   | T+41s     | T+127s       | no
#     3   | Done button   | T+92s     | T+127s       | no
#     4   | settings save | T+19s     | T+127s       | no
#
# Cycle 4 is the clearest: the path that did the work was silent, so the
# journal's ONLY record of that handoff was the false one, with nothing to
# contrast it against.

_COMPLETED_VIA = "handoff: completed via"


def _completion_lines(caplog) -> list[str]:
    return [r.message % r.args if r.args else r.message for r in caplog.records if "handoff:" in str(r.msg)]


class TestCompletionAttribution:
    @pytest.mark.parametrize(
        "trigger_attr",
        ["TRIGGER_DONE_BUTTON", "TRIGGER_SETTINGS_SAVE", "TRIGGER_BROWSER_TZ", "TRIGGER_AUTO_TIMER"],
    )
    def test_every_trigger_names_itself(self, make_app, markers, caplog, trigger_attr):
        trigger = getattr(handoff_mod, trigger_attr)
        app = make_app()
        with caplog.at_level("INFO", logger="control_server.handoff"):
            assert handoff_mod.mark_handoff_complete(app, trigger) is True

        assert markers["handoff"].exists()
        # `trigger` must be non-empty for the assertion below to mean anything:
        # with an empty constant it degrades to "handoff: completed via " in
        # text, which the emitted line satisfies. /review found exactly that.
        assert trigger.strip(), f"{trigger_attr} is blank — the assertion below cannot fail"
        assert f"{_COMPLETED_VIA} {trigger}" in caplog.text

    def test_the_four_triggers_are_distinguishable(self):
        """Attribution is worthless if two paths log the same thing.

        Exact-uniqueness is NOT enough, which /review demonstrated: setting
        TRIGGER_SETTINGS_SAVE to "the Done" — a PREFIX of TRIGGER_DONE_BUTTON —
        left the entire suite green while every journal grep and both negative
        assertions in this file became ambiguous. Prefix-freeness is already a
        live invariant, because production composes the timer's trigger as
        "the auto-complete timer (120s)" and tests match the constant against
        it as a prefix.
        """
        triggers = [
            handoff_mod.TRIGGER_DONE_BUTTON,
            handoff_mod.TRIGGER_SETTINGS_SAVE,
            handoff_mod.TRIGGER_BROWSER_TZ,
            handoff_mod.TRIGGER_AUTO_TIMER,
        ]
        assert len(set(triggers)) == len(triggers)
        for t in triggers:
            assert t.strip(), "a blank trigger would log an unattributed completion"
        for a in triggers:
            for b in triggers:
                if a is not b:
                    assert not a.startswith(b), f"{a!r} starts with {b!r} — greps and negative assertions blur"

    def test_the_composed_timer_trigger_still_names_the_timer(self):
        """Production passes an f-string, not the bare constant. If the
        composition ever stopped leading with the constant, every assertion in
        this file that matches the constant as a prefix would silently stop
        covering the real string."""
        composed = f"{handoff_mod.TRIGGER_AUTO_TIMER} (120s)"
        assert composed.startswith(handoff_mod.TRIGGER_AUTO_TIMER)
        assert "120s" in composed

    def test_an_already_complete_handoff_is_not_reported_as_a_completion(self, make_app, caplog):
        """The bug, stated directly. A trigger arriving after the handoff is
        already done must not claim it did the work."""
        app = make_app(handoff_done=True)
        with caplog.at_level("INFO", logger="control_server.handoff"):
            assert handoff_mod.mark_handoff_complete(app, handoff_mod.TRIGGER_AUTO_TIMER) is True

        assert _COMPLETED_VIA not in caplog.text, "claimed a completion it did not perform"
        assert "already complete" in caplog.text, "and it must not be silent about having fired"

    def test_the_timer_does_not_take_credit_for_the_done_button(self, make_app, markers, caplog, monkeypatch):
        """The exact bench scenario, end to end: Done completes the handoff,
        then the 120s timer expires behind it.

        Before litclock-dev#646 this produced `auto-completed after 120s timeout` while
        the marker mtime showed the Done button had finished it 79s earlier —
        and that line was the journal's only record, so a reader had no way to
        detect the error.
        """
        app = make_app(located=True)

        captured = {}

        class _FakeTimer:
            def __init__(self, delay, fn):
                captured["fn"] = fn

            def start(self):
                pass

        monkeypatch.setattr(handoff_mod.threading, "Timer", _FakeTimer)

        with caplog.at_level("INFO", logger="control_server.handoff"):
            # The Done button gets there first.
            handoff_mod.mark_handoff_complete(app, handoff_mod.TRIGGER_DONE_BUTTON)
            mtime_after_done = markers["handoff"].stat().st_mtime_ns

            # ...then the timer expires.
            handoff_mod.start_auto_timer(app, delay=120.0)
            captured["fn"]()

        assert markers["handoff"].stat().st_mtime_ns == mtime_after_done, (
            "the timer must not rewrite the marker — the bench runs confirmed it never did"
        )
        assert f"{_COMPLETED_VIA} {handoff_mod.TRIGGER_DONE_BUTTON}" in caplog.text
        assert f"{_COMPLETED_VIA} {handoff_mod.TRIGGER_AUTO_TIMER}" not in caplog.text, (
            "the timer claimed a completion the Done button performed — this is litclock-dev#646"
        )
        assert "already complete" in caplog.text

    def test_the_timer_does_take_credit_when_it_really_completed(self, make_app, markers, caplog, monkeypatch):
        """Converse of the above. Suppressing the false claim must not suppress
        the true one — a genuine timeout completion is exactly the case the
        original line was written for."""
        app = make_app(located=True)
        captured = {}

        class _FakeTimer:
            def __init__(self, delay, fn):
                captured["fn"] = fn

            def start(self):
                pass

        monkeypatch.setattr(handoff_mod.threading, "Timer", _FakeTimer)
        with caplog.at_level("INFO", logger="control_server.handoff"):
            handoff_mod.start_auto_timer(app, delay=120.0)
            captured["fn"]()

        assert markers["handoff"].exists()
        assert f"{_COMPLETED_VIA} {handoff_mod.TRIGGER_AUTO_TIMER}" in caplog.text
        assert "120s" in caplog.text, "the delay is worth keeping — it dates the completion"

    def test_a_blocked_completion_names_the_trigger_that_was_blocked(self, make_app, caplog):
        """"leaving splash up" with no trigger named leaves the reader unable to
        tell a timer expiry from a settings save that arrived too early."""
        app = make_app(located=False)
        with caplog.at_level("INFO", logger="control_server.handoff"):
            assert handoff_mod.complete_if_timezone_known(app, handoff_mod.TRIGGER_SETTINGS_SAVE) is False

        assert handoff_mod.TRIGGER_SETTINGS_SAVE in caplog.text
        assert "timezone is not yet known" in caplog.text
        assert _COMPLETED_VIA not in caplog.text

    def test_a_post_handoff_settings_save_stays_quiet(self, make_app, caplog):
        """The not-our-job branch must NOT log. Every settings save for the life
        of the device passes through here, and burying the completion lines
        under that noise would undo the point of the change."""
        app = make_app(handoff_done=True)
        with caplog.at_level("INFO", logger="control_server.handoff"):
            assert handoff_mod.complete_if_timezone_known(app, handoff_mod.TRIGGER_SETTINGS_SAVE) is True

        assert _completion_lines(caplog) == []


class TestCompletionAttributionThroughTheRoutes:
    """The unit tests above pass the trigger explicitly, so they cannot catch a
    route wired to the WRONG constant — which would be a silent misattribution
    of exactly the kind litclock-dev#646 is about."""

    def test_done_endpoint_is_attributed_to_the_done_button(self, make_app, caplog):
        app = make_app(located=True)
        with caplog.at_level("INFO", logger="control_server.handoff"), app.test_client() as c:
            r = c.post("/api/handoff/done")

        assert r.status_code == 200
        assert f"{_COMPLETED_VIA} {handoff_mod.TRIGGER_DONE_BUTTON}" in caplog.text

    def test_set_timezone_endpoint_is_attributed_to_the_browser_fallback(self, make_app, caplog, monkeypatch):
        # The one completer allowed to run with latitude empty.
        app = make_app(located=False)
        monkeypatch.setattr(geocoding, "set_system_timezone", lambda tz: (True, None))

        with caplog.at_level("INFO", logger="control_server.handoff"), app.test_client() as c:
            r = c.post("/api/handoff/set-timezone", json={"timezone": "America/Chicago"})

        assert r.status_code == 200
        assert f"{_COMPLETED_VIA} {handoff_mod.TRIGGER_BROWSER_TZ}" in caplog.text
        assert f"{_COMPLETED_VIA} {handoff_mod.TRIGGER_DONE_BUTTON}" not in caplog.text

    def test_settings_save_is_attributed_to_the_settings_save_trigger(self, make_app, markers, caplog, monkeypatch):
        """The route the whole issue's clearest example came through.

        Bench cycle 4 completed by saving a setting, which logged nothing — so
        the journal's ONLY record of that handoff was the timer's false claim,
        with nothing to contrast against. The unit tests pass the trigger
        explicitly and so cannot catch this call site wired to the wrong
        constant; /review confirmed rewiring it left the whole suite green.
        """
        import control_server.routes.settings as settings_routes
        from control_server.csrf import CSRF_ACTION

        app = make_app(located=True)
        monkeypatch.setattr(settings_routes, "_ad_hoc_tick", lambda: None)
        token, _ = app.extensions["csrf_tokens"].issue(CSRF_ACTION)

        with caplog.at_level("INFO", logger="control_server.handoff"):
            r = app.test_client().post(
                "/api/settings",
                json={"ALLOW_NSFW_QUOTES": "true", "csrf_token": token},
                headers={"Origin": "http://localhost"},
            )

        assert r.status_code == 200, r.get_data(as_text=True)
        assert markers["handoff"].exists()
        assert f"{handoff_mod.COMPLETED_VIA_PREFIX} {handoff_mod.TRIGGER_SETTINGS_SAVE}" in caplog.text
        assert handoff_mod.TRIGGER_DONE_BUTTON not in caplog.text
        assert handoff_mod.TRIGGER_AUTO_TIMER not in caplog.text


class TestConcurrentTriggers:
    """Four triggers can race. One marker must produce exactly one claim."""

    def test_two_triggers_racing_produce_one_completion_line(self, make_app, markers):
        """exists()-then-touch was not atomic: two threads produced ONE marker
        and TWO 'completed via' lines naming different paths — litclock-dev#646's defect
        reached by another route. The loser also bumped the marker's mtime,
        which is the very signal QA used to overturn the original
        misattribution.
        """
        import logging
        import threading

        app = make_app(located=True)
        records: list[str] = []
        lock = threading.Lock()

        class _Collect(logging.Handler):
            def emit(self, record):
                with lock:
                    records.append(record.getMessage())

        logger = logging.getLogger("control_server.handoff")
        handler = _Collect()
        logger.addHandler(handler)
        prior_level = logger.level
        logger.setLevel(logging.INFO)

        barrier = threading.Barrier(2)

        def _race(trigger):
            barrier.wait()
            handoff_mod.mark_handoff_complete(app, trigger)

        try:
            threads = [
                threading.Thread(target=_race, args=(handoff_mod.TRIGGER_DONE_BUTTON,)),
                threading.Thread(target=_race, args=(handoff_mod.TRIGGER_AUTO_TIMER,)),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(prior_level)

        completions = [m for m in records if m.startswith(handoff_mod.COMPLETED_VIA_PREFIX)]
        noops = [m for m in records if "already complete" in m]

        assert markers["handoff"].exists()
        assert len(completions) == 1, f"one marker, one claim — got {completions}"
        assert len(noops) == 1, f"the loser must say it did nothing — got {records}"


def test_the_fallback_script_uses_the_same_canonical_phrasing():
    """The sixth completion path, and the one that runs precisely when
    control_server is broken (litclock-dev#646 /review F10).

    scripts/litclock-handoff-fallback.sh is bash, so it cannot import
    COMPLETED_VIA_PREFIX. Before this, an operator grepping the canonical string
    on a Pi rescued by litclock-handoff-fallback.timer got NOTHING and would
    conclude the handoff never completed — the same wrong-conclusion class this
    whole change exists to kill.
    """
    raw = (Path(__file__).resolve().parents[1] / "scripts" / "litclock-handoff-fallback.sh").read_text()
    # Comment lines stripped: the script's own comment EXPLAINS this contract
    # and quotes the string, so asserting against the raw file is satisfied by
    # prose alone — verified by deleting the echo and watching this pass.
    script = "\n".join(ln for ln in raw.splitlines() if not ln.lstrip().startswith("#"))

    assert handoff_mod.COMPLETED_VIA_PREFIX in script, (
        "the fallback completer must announce itself with the same string the Python paths use, "
        "or grepping for it on a rescued device reports a handoff that never happened"
    )
    # ...and it must not impersonate one of the in-process triggers.
    for trigger in (
        handoff_mod.TRIGGER_DONE_BUTTON,
        handoff_mod.TRIGGER_SETTINGS_SAVE,
        handoff_mod.TRIGGER_BROWSER_TZ,
        handoff_mod.TRIGGER_AUTO_TIMER,
    ):
        assert f"{handoff_mod.COMPLETED_VIA_PREFIX} {trigger}" not in script, (
            f"the fallback names itself {trigger!r}, which control_server also claims"
        )


def test_the_timer_still_speaks_when_the_marker_appears_mid_check(make_app, markers, monkeypatch, caplog):
    """litclock-dev#646 /review F4.

    An earlier fix checked is_handoff_active in the timer AND again inside
    complete_if_timezone_known, with an env.sh read between them. A marker
    appearing in that window made the timer emit NOTHING — silently
    reintroducing the gap the change exists to close. One check now, so there
    is no window to fall into.
    """
    app = make_app(located=True)
    captured = {}

    class _FakeTimer:
        def __init__(self, delay, fn):
            captured["fn"] = fn

        def start(self):
            pass

    monkeypatch.setattr(handoff_mod.threading, "Timer", _FakeTimer)

    # The marker lands before the timer body runs — the interleaving under test.
    markers["handoff"].touch()

    with caplog.at_level("INFO", logger="control_server.handoff"):
        handoff_mod.start_auto_timer(app, delay=120.0)
        captured["fn"]()

    lines = [r.getMessage() for r in caplog.records]
    assert lines, "the timer must never fire silently — that is the whole issue"
    assert handoff_mod.COMPLETED_VIA_PREFIX not in caplog.text
    assert "already complete" in caplog.text
    assert handoff_mod.TRIGGER_AUTO_TIMER in caplog.text


def test_a_timer_firing_before_setup_completes_says_so(make_app, markers, monkeypatch, caplog):
    """is_handoff_active is false for two different reasons. Reporting a device
    whose setup never finished as "already complete" would be a new false
    claim, in a change about not making false claims."""
    app = make_app(setup=True, located=True)
    markers["setup"].unlink()
    captured = {}

    class _FakeTimer:
        def __init__(self, delay, fn):
            captured["fn"] = fn

        def start(self):
            pass

    monkeypatch.setattr(handoff_mod.threading, "Timer", _FakeTimer)
    with caplog.at_level("INFO", logger="control_server.handoff"):
        handoff_mod.start_auto_timer(app, delay=120.0)
        captured["fn"]()

    assert "setup is not complete" in caplog.text
    assert "already complete" not in caplog.text
    assert handoff_mod.COMPLETED_VIA_PREFIX not in caplog.text
