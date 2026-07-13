"""Tests for the M3 Settings tab — control_server routes, CSRF, save+apply.

Coverage:
- GET /settings renders all 5 sections + pre-fills from env.sh.
- POST /settings (no-JS PRG path): 303 on success, 200 with field errors on
  validation failure, 403 on missing CSRF / Origin mismatch.
- POST /api/settings (JSON path): same envelope, PATCH-merge semantics,
  per-field validation errors.
- GET /api/geocode preview returns resolved name + lat/lon.
- save+apply triggers `systemctl start --no-block litclock.service` (D1).
- Concurrent save fixture (already covered in test_config) extended with the
  M3 free-form keys.
- CSRF unit tests (csrf.CsrfTokenStore + origin_matches_host).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make src/ importable (mirrors test_config.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config  # noqa: E402
import geocoding  # noqa: E402
from control_server import create_app  # noqa: E402
from control_server.csrf import (  # noqa: E402
    CSRF_ACTION,
    CsrfTokenStore,
    origin_matches_host,
)

# ─── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def env_file(tmp_path: Path) -> str:
    p = tmp_path / "env.sh"
    p.write_text(
        "WEATHER_ENABLED=true\n"
        "WEATHER_LATITUDE=30.27\n"
        "WEATHER_LONGITUDE=-97.74\n"
        "WEATHER_LOCATION_NAME=\n"
        "WEATHER_UNITS=imperial\n"
        "ALLOW_NSFW_QUOTES=false\n"
        # #280: GIFT_MODE_ENABLED dropped; only GIFT_MODE_MESSAGE persists.
        "GIFT_MODE_MESSAGE=\n"
    )
    return str(p)


@pytest.fixture
def app(env_file: str):
    return create_app({"ENV_FILE": env_file, "VERSION_OVERRIDE": "v0.test"})


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def csrf_token(app) -> str:
    token, _ = app.extensions["csrf_tokens"].issue(CSRF_ACTION)
    return token


@pytest.fixture(autouse=True)
def _stub_systemctl(monkeypatch):
    """Stub out the ad-hoc systemctl tick so tests don't shell out. We
    capture calls so test_save_triggers_ad_hoc_tick can assert on them."""
    calls: list[list[str]] = []

    def fake_run(argv, *args, **kwargs):
        calls.append(list(argv))
        from subprocess import CompletedProcess

        # Pretend the litclock unit is inactive so the ad-hoc tick's
        # `is-active` poll returns "inactive" and the thread proceeds
        # to the actual `start` call. _service_is_active parses stdout
        # rather than the exit code (see comment in routes/settings.py
        # for why).
        if len(argv) >= 3 and argv[1] == "is-active":
            return CompletedProcess(argv, 3, "inactive\n", "")
        return CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("control_server.routes.settings.subprocess.run", fake_run)
    return calls


def _wait_for_tick_thread(timeout: float = 2.0) -> None:
    """The ad-hoc tick runs on a daemon thread; tests need to wait for it
    to finish before asserting on the captured subprocess calls. We poll
    the threading enumerator for any thread named "ad-hoc-tick" and join
    it with a generous-but-bounded timeout."""
    import threading
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        threads = [t for t in threading.enumerate() if t.name == "ad-hoc-tick" and t.is_alive()]
        if not threads:
            return
        for t in threads:
            t.join(timeout=0.1)


@pytest.fixture(autouse=True)
def _stub_geocoding(monkeypatch):
    """Default: geocode_location returns Austin, TX. Tests that need a
    different result override this with their own monkeypatch."""

    def fake_geocode(query, country_code=None):
        return {
            "lat": "30.27",
            "lon": "-97.74",
            "display_name": "Austin, Travis County, Texas, United States",
            "timezone": "America/Chicago",
        }

    def fake_ip_geo():
        return {"lat": "0", "lon": "0", "country_code": "US", "timezone": "UTC"}

    def fake_tz(lat, lon):
        return "America/Chicago"

    import geocoding

    monkeypatch.setattr(geocoding, "geocode_location", fake_geocode)
    monkeypatch.setattr(geocoding, "ip_geolocate", fake_ip_geo)
    monkeypatch.setattr(geocoding, "timezone_from_coords", fake_tz)
    # set_system_timezone shells out — stub the success path.

    monkeypatch.setattr(geocoding, "set_system_timezone", lambda tz: (True, None))


# ─── CSRF store unit tests ──────────────────────────────────────────────────


class TestCsrfTokenStore:
    def test_validate_passes_for_freshly_issued(self) -> None:
        store = CsrfTokenStore()
        token, _expires = store.issue("settings")
        assert store.validate("settings", token) is True

    def test_validate_is_multi_use(self) -> None:
        """D4: same token validates multiple times within the TTL window."""
        store = CsrfTokenStore()
        token, _ = store.issue("settings")
        assert store.validate("settings", token) is True
        assert store.validate("settings", token) is True
        assert store.validate("settings", token) is True

    def test_validate_rejects_unknown_token(self) -> None:
        store = CsrfTokenStore()
        assert store.validate("settings", "garbage-token") is False

    def test_validate_rejects_wrong_action(self) -> None:
        store = CsrfTokenStore()
        token, _ = store.issue("settings")
        assert store.validate("reboot", token) is False

    def test_validate_rejects_after_ttl_expiry(self) -> None:
        store = CsrfTokenStore(ttl_seconds=0)
        token, _ = store.issue("settings")
        # Past TTL — sweep prunes it.
        assert store.validate("settings", token) is False


class TestOriginMatchesHost:
    def _request(self, *, host: str, origin: str | None = None, referer: str | None = None):
        from werkzeug.test import EnvironBuilder

        headers = {}
        if origin is not None:
            headers["Origin"] = origin
        if referer is not None:
            headers["Referer"] = referer
        builder = EnvironBuilder(method="POST", path="/api/settings", headers=headers)
        env = builder.get_environ()
        env["HTTP_HOST"] = host
        from flask import Flask

        app = Flask(__name__)
        with app.test_request_context(environ_overrides=env):
            from flask import request as flask_request

            return origin_matches_host(flask_request)

    def test_matching_origin_passes(self) -> None:
        assert self._request(host="litclock.local:8443", origin="http://litclock.local:8443") is True

    def test_mismatched_origin_fails(self) -> None:
        assert self._request(host="litclock.local:8443", origin="http://attacker.example") is False

    def test_referer_fallback_passes(self) -> None:
        assert self._request(host="192.168.1.10:8443", referer="http://192.168.1.10:8443/settings") is True

    def test_both_missing_fails_closed(self) -> None:
        assert self._request(host="litclock.local:8443") is False

    def test_null_origin_fails(self) -> None:
        assert self._request(host="litclock.local:8443", origin="null") is False


# ─── GET /settings ──────────────────────────────────────────────────────────


class TestSettingsGet:
    def test_renders_with_200(self, client) -> None:
        resp = client.get("/settings")
        assert resp.status_code == 200

    def test_includes_all_sections(self, client) -> None:
        """#317 item 7: the gift section moved to /system. #337 A9-A11
        reordered + renamed: Settings now renders Location / Weather /
        Temperature / Advanced (note: 'temperature-h' replaces 'units-h')."""
        resp = client.get("/settings")
        body = resp.data.decode()
        for marker in (
            "settings-location-h",
            "settings-weather-h",
            "settings-temperature-h",  # #337 A11 rename: Units → Temperature
            "settings-advanced-h",
        ):
            assert marker in body
        assert "settings-gift-h" not in body, (
            "#317 item 7: Prepare-for-Gifting must not render on /settings — "
            "it lives on /system alongside the other one-shot destructive actions"
        )
        # #337 A11: the old "settings-units-h" id was renamed to
        # "settings-temperature-h" because the section now shows only the
        # temperature units (drop the mph/km/h wind labels that misleadingly
        # suggested a wind preference).
        assert "settings-units-h" not in body, (
            "#337 A11: 'Units' section renamed to 'Temperature' — the old settings-units-h id should no longer render"
        )

    def test_pre_fills_from_env_file(self, client, env_file) -> None:
        """#337 A9/A11/A13: assertions follow the new IA.
        * Latitude now lives under the Location section's `<details>Advanced`,
          pre-filled when MODE=specific (else empty by design — `data-advanced-lat`
          input value is gated on current_mode in the template).
        * Temperature units are now radios in a segmented pill (per A13), not
          a `<select>` — pre-selected via the `checked` attribute, not `selected`.
        * Default test env has MODE absent → reads as 'auto', so Latitude
          pre-fill won't appear in the template (Auto mode hides Advanced
          inputs by setting `value=""` in the Jinja conditional). Verify the
          input element still exists in the DOM for keep-it-present assertions."""
        resp = client.get("/settings")
        body = resp.data.decode()
        # Temperature units radio pre-selected per A13. Default test env
        # has WEATHER_UNITS=imperial.
        assert 'value="imperial"' in body and "checked" in body, (
            "Temperature units must pre-select imperial via checked attribute"
        )
        # Latitude input is present in the DOM (under <details>Advanced).
        assert 'name="WEATHER_LATITUDE"' in body
        # The Location section is present and has the segmented pill.
        assert "data-mode-pill" in body
        # The Place input is in the DOM (hidden when in Auto mode).
        assert 'name="location_query"' in body

    def test_csrf_token_in_form(self, client) -> None:
        resp = client.get("/settings")
        assert b'name="csrf_token"' in resp.data

    def test_success_banner_rendered_on_query_param(self, client) -> None:
        resp = client.get("/settings?saved=weather&name=Austin%2C%20TX")
        body = resp.data.decode()
        assert "Saved." in body
        assert "Austin, TX" in body

    def test_destructive_button_class_removed_from_settings_css(self, client) -> None:
        """#317 item 7: the Prepare-for-Gifting card moved to /system, so the
        `.settings-button--destructive` rule that lived in settings.css
        (added in #316 to fix a UA-default-gray regression) is now dead
        code on the Settings side. The matching destructive styling lives
        in system.css under `[data-action="prepare_for_gift"]`. Pin the
        absence so a stale rule can't drift back in."""
        css = client.get("/static/css/settings.css").data.decode()
        assert ".settings-button--destructive" not in css, (
            "#317 item 7: .settings-button--destructive must not live on the "
            "Settings side anymore — the destructive Prepare button is on /system"
        )
        # Cross-check: the destructive styling DOES exist on the system side.
        system_css = client.get("/static/css/system.css").data.decode()
        assert '[data-action="prepare_for_gift"]' in system_css, (
            "system.css must style the Prepare-for-Gifting destructive submit with --error"
        )

    def test_secondary_button_class_removed_under_a10_ia(self, client) -> None:
        """#337 A10 (locked 2026-06-01) removed the Clear weather location
        affordance from the new IA — the Automatic pill IS the reset, so the
        secondary button it styled has no users left. The CSS class was the
        original Review C3 fix for the disabled-looking grey-chip regression;
        that win is preserved by deleting the now-dead rule (no rule = no
        risk of regressing into a wrong variant).

        If a future feature needs a Secondary button, port DESIGN.md
        "Buttons" line 218-223 (transparent bg + --accent text + --accent
        border) into a fresh rule alongside its caller — don't resurrect
        this one in isolation."""
        css = client.get("/static/css/settings.css").data.decode()
        # Strip /* ... */ comments before checking — the post-A10 removal
        # comment legitimately names the dead class so a future reader knows
        # why the rule is gone. We're testing for live CSS rules, not the
        # absence of the string from comment prose.
        import re as _re

        css_no_comments = _re.sub(r"/\*.*?\*/", "", css, flags=_re.DOTALL)
        for cls in (
            ".settings-button--secondary",
            ".settings-form--clear",
            ".settings-form__actions--clear",
            ".settings-row__help--clear",
        ):
            # Look for the class followed by a CSS-rule signal (whitespace+brace,
            # colon for pseudo, or comma for selector list) — not just the
            # substring (which would match grep-bait inside comments).
            assert not _re.search(_re.escape(cls) + r"[\s:,]*\{", css_no_comments), (
                f"#337 A10: {cls} is dead code (Clear button removed). "
                "If you need Secondary styling, port DESIGN.md Buttons spec into a fresh rule."
            )


# ─── POST /settings (HTML PRG) ──────────────────────────────────────────────


class _PostHelpers:
    def post_form(self, client, *, csrf_token: str, section: str, **fields):
        data = {"csrf_token": csrf_token, "section": section, **fields}
        return client.post(
            "/settings",
            data=data,
            base_url="http://litclock.local",
            headers={"Origin": "http://litclock.local"},
        )

    def _post_json(self, client, payload, *, csrf_token: str | None = None, origin: str = "http://litclock.local"):
        """Shared JSON-POST helper for any test class that mixes in _PostHelpers.
        Originally defined only on TestApiSettingsPost; promoted up so the new
        #337 /review-followup test classes (TestSyncQuickAndA14Backstop,
        TestApiSystemSetTimezone) can use the same shape without duplicating."""
        body = dict(payload)
        if csrf_token is not None and "csrf_token" not in body:
            body["csrf_token"] = csrf_token
        return client.post(
            "/api/settings",
            json=body,
            base_url="http://litclock.local",
            headers={"Origin": origin},
        )


class TestSettingsPost(_PostHelpers):
    def test_303_on_success(self, client, csrf_token, env_file) -> None:
        resp = self.post_form(client, csrf_token=csrf_token, section="units", WEATHER_UNITS="metric")
        assert resp.status_code == 303
        assert "/settings?saved=units" in resp.headers["Location"]
        assert config.load_config(env_file)["WEATHER_UNITS"] == "metric"

    def test_403_without_csrf(self, client) -> None:
        resp = client.post(
            "/settings",
            data={"section": "units", "WEATHER_UNITS": "metric"},
            base_url="http://litclock.local",
            headers={"Origin": "http://litclock.local"},
        )
        assert resp.status_code == 403

    def test_403_with_origin_mismatch(self, client, csrf_token) -> None:
        resp = client.post(
            "/settings",
            data={"csrf_token": csrf_token, "section": "units", "WEATHER_UNITS": "metric"},
            base_url="http://litclock.local",
            headers={"Origin": "http://attacker.example"},
        )
        assert resp.status_code == 403

    def test_403_with_no_origin_or_referer(self, client, csrf_token) -> None:
        resp = client.post(
            "/settings",
            data={"csrf_token": csrf_token, "section": "units", "WEATHER_UNITS": "metric"},
            base_url="http://litclock.local",
        )
        assert resp.status_code == 403

    def test_validation_failure_re_renders_with_field_error(self, client, csrf_token) -> None:
        resp = self.post_form(
            client,
            csrf_token=csrf_token,
            section="location",
            WEATHER_LATITUDE="200",
            WEATHER_LONGITUDE="0",
        )
        assert resp.status_code == 422
        body = resp.data.decode()
        assert "between -90 and 90" in body or "between" in body

    def test_unchecked_checkbox_writes_false(self, client, csrf_token, env_file) -> None:
        """HTML checkboxes that aren't checked don't appear in the form
        payload at all. The route synthesises the missing booleans for the
        named section so an off->on->off cycle actually reaches `false`."""
        resp = self.post_form(client, csrf_token=csrf_token, section="advanced")
        assert resp.status_code == 303
        assert config.load_config(env_file)["ALLOW_NSFW_QUOTES"] == "false"

    def test_show_diagnostics_shortcut_round_trips(self, client, csrf_token, env_file) -> None:
        """#416 PR3c F31 — opt-in ribbon expansion toggle. False default
        protects the owner persona; helper persona flips it on so the
        affordance is discoverable. Round-trip through the Advanced
        section's form."""
        # ON
        resp = self.post_form(
            client,
            csrf_token=csrf_token,
            section="advanced",
            SHOW_DIAGNOSTICS_SHORTCUT="true",
            ALLOW_NSFW_QUOTES="false",
        )
        assert resp.status_code == 303
        assert config.load_config(env_file)["SHOW_DIAGNOSTICS_SHORTCUT"] == "true"
        # OFF (synthesised because the checkbox is unchecked)
        resp = self.post_form(client, csrf_token=csrf_token, section="advanced")
        assert resp.status_code == 303
        cfg = config.load_config(env_file)
        assert cfg["SHOW_DIAGNOSTICS_SHORTCUT"] == "false"
        assert cfg["ALLOW_NSFW_QUOTES"] == "false"

    def test_uppercase_bool_in_env_preserves_toggle_state(self, client, csrf_token, env_file) -> None:
        """/review F-CASE-DRIFT (Codex P2): a manual env edit setting
        SHOW_DIAGNOSTICS_SHORTCUT=TRUE (or ALLOW_NSFW_QUOTES=TRUE)
        must render the checkbox CHECKED, so a subsequent Advanced
        save (which synthesises 'false' for unchecked checkboxes via
        bool_keys) doesn't silently revert the helper-persona's
        manual setting.
        """
        from pathlib import Path

        # Write the env.sh with uppercase TRUE (the case _validate_bool
        # accepts but the writer normalises only on PWA-driven saves).
        Path(env_file).write_text(
            "export WEATHER_LATITUDE=0\n"
            "export WEATHER_LONGITUDE=0\n"
            "export WEATHER_UNITS=imperial\n"
            "export WEATHER_LOCATION_MODE=auto\n"
            "export WEATHER_ENABLED=false\n"
            "export ALLOW_NSFW_QUOTES=TRUE\n"
            "export SHOW_DIAGNOSTICS_SHORTCUT=TRUE\n"
        )
        body = client.get("/settings").data.decode()
        # Both toggles should render CHECKED + aria-checked=true.
        assert 'id="allow_nsfw_quotes"' in body
        assert 'id="show_diagnostics_shortcut"' in body
        nsfw_row = body[body.find('id="allow_nsfw_quotes"') : body.find('id="allow_nsfw_quotes"') + 400]
        diag_row = body[body.find('id="show_diagnostics_shortcut"') : body.find('id="show_diagnostics_shortcut"') + 400]
        assert 'aria-checked="true"' in nsfw_row, f"NSFW row should be checked under uppercase TRUE: {nsfw_row!r}"
        assert "checked" in nsfw_row.split(">")[0]
        assert 'aria-checked="true"' in diag_row, f"Diag row should be checked under uppercase TRUE: {diag_row!r}"
        assert "checked" in diag_row.split(">")[0]
        # Body data attribute also lifts from the uppercase env value.
        root_body = client.get("/").data
        assert b"data-diag-ribbon-expanded" in root_body

    def test_show_diagnostics_shortcut_drives_body_attribute(self, client, csrf_token, env_file) -> None:
        """The diag_shortcut_expanded context processor lifts the env
        value onto body[data-diag-ribbon-expanded]; CSS in drawer.css
        responds to that attribute to expand the ribbon's full label."""
        # Default (false) → body lacks the attribute.
        body = client.get("/").data
        assert b"data-diag-ribbon-expanded" not in body
        # Flip on.
        self.post_form(
            client,
            csrf_token=csrf_token,
            section="advanced",
            SHOW_DIAGNOSTICS_SHORTCUT="true",
            ALLOW_NSFW_QUOTES="false",
        )
        body = client.get("/").data
        assert b"data-diag-ribbon-expanded" in body

    def test_gift_mode_message_round_trips_with_punctuation(self, client, csrf_token, env_file) -> None:
        """#280: gift section now persists ONLY the message draft. Toggle was
        dropped — gift mode is a one-shot action via /api/system/prepare-for-gift."""
        msg = 'O\'Brien said "hi"; back later'
        resp = self.post_form(
            client,
            csrf_token=csrf_token,
            section="gift",
            GIFT_MODE_MESSAGE=msg,
        )
        assert resp.status_code == 303
        cfg = config.load_config(env_file)
        assert cfg["GIFT_MODE_MESSAGE"] == msg

    def test_gift_mode_rejects_dollar_sign(self, client, csrf_token) -> None:
        """#317 item 7 — gift writer still lives on POST /settings (centralised
        persistence), but failure now re-renders the System tab where the
        textarea lives, so the inline error message lands next to the input."""
        resp = self.post_form(
            client,
            csrf_token=csrf_token,
            section="gift",
            GIFT_MODE_MESSAGE="hi $(whoami)",
        )
        assert resp.status_code == 422
        assert b"may not contain" in resp.data

    def test_gift_mode_rejects_overlong(self, client, csrf_token) -> None:
        """#319 lowered the cap to 80 chars."""
        resp = self.post_form(
            client,
            csrf_token=csrf_token,
            section="gift",
            GIFT_MODE_MESSAGE="x" * 81,
        )
        assert resp.status_code == 422

    def test_invalid_section_400s(self, client, csrf_token) -> None:
        resp = client.post(
            "/settings",
            data={"csrf_token": csrf_token, "section": "bogus"},
            base_url="http://litclock.local",
            headers={"Origin": "http://litclock.local"},
        )
        assert resp.status_code == 400


# ─── #325 — Clear weather location ──────────────────────────────────────────


class TestClearWeatherLocation(_PostHelpers):
    """Issue #325 — explicit "Clear weather location" affordance.

    Before this: a user who set a city had no UI path to clear it. Settings
    -> empty City -> Save was a no-op (the geocode block is gated on `if
    location_query:`). Only escape hatch was SSH + sed.

    Locked decision (eng-review Tension 7 rejected): clear=1 zeroes the
    three weather location keys (WEATHER_LOCATION_NAME, WEATHER_LATITUDE,
    WEATHER_LONGITUDE). WEATHER_ENABLED stays unchanged — the honest
    label on the Clear button informs the user that weather pauses.
    S2-style radio modes (city / GPS / none) are deferred to #337.
    """

    @pytest.fixture
    def env_with_city(self, tmp_path: Path) -> str:
        """env.sh with a city set, so the Clear affordance has something
        to clear. Matches the live "user set city, wants to clear" state."""
        p = tmp_path / "env.sh"
        p.write_text(
            "WEATHER_ENABLED=true\n"
            "WEATHER_LATITUDE=30.27\n"
            "WEATHER_LONGITUDE=-97.74\n"
            "WEATHER_LOCATION_NAME=Austin, Texas\n"
            "WEATHER_UNITS=imperial\n"
            "ALLOW_NSFW_QUOTES=false\n"
            "GIFT_MODE_MESSAGE=\n"
        )
        return str(p)

    def test_clear_zeroes_location_keys_and_preserves_enabled(self, client, csrf_token, env_with_city, app) -> None:
        """clear=1 zeroes the three weather location keys but leaves
        WEATHER_ENABLED alone (Tension 7 rejected). Other env keys
        (UNITS, ALLOW_NSFW_QUOTES, GIFT_MODE_MESSAGE) untouched."""
        with app.app_context():
            app.config["ENV_FILE"] = env_with_city
            resp = self.post_form(
                client,
                csrf_token=csrf_token,
                section="weather",
                clear="1",
            )
        assert resp.status_code == 303, resp.data
        cfg = config.load_config(env_with_city)
        # Three weather location keys zeroed.
        assert cfg["WEATHER_LOCATION_NAME"] == ""
        assert cfg["WEATHER_LATITUDE"] == ""
        assert cfg["WEATHER_LONGITUDE"] == ""
        # WEATHER_ENABLED preserved (Tension 7 locked decision).
        assert cfg["WEATHER_ENABLED"] == "true"
        # Other env keys untouched.
        assert cfg["WEATHER_UNITS"] == "imperial"
        assert cfg["ALLOW_NSFW_QUOTES"] == "false"

    def test_clear_skips_geocoding(self, client, csrf_token, env_with_city, app, monkeypatch) -> None:
        """Defense-in-depth: when clear=1 is set, the geocoding code path
        must not run at all. Without this, a confused client sending both
        clear=1 AND location_query could trigger an unwanted geocode
        call, which costs an HTTP round-trip to Nominatim and could fail
        for reasons unrelated to the clear request."""
        geocode_calls: list[str] = []

        import geocoding

        def fake_geocode(query, country_code=None):
            geocode_calls.append(query)
            return {"lat": "0", "lon": "0", "display_name": "should-not-resolve"}

        monkeypatch.setattr(geocoding, "geocode_location", fake_geocode)

        with app.app_context():
            app.config["ENV_FILE"] = env_with_city
            resp = self.post_form(
                client,
                csrf_token=csrf_token,
                section="weather",
                clear="1",
                location_query="Some City",  # SHOULD be ignored when clear=1
            )
        assert resp.status_code == 303
        assert geocode_calls == [], (
            "clear=1 must short-circuit before the geocoding block — "
            "otherwise a stray location_query field would silently override the clear"
        )
        cfg = config.load_config(env_with_city)
        # Cleared, NOT geocoded to (0,0) or "should-not-resolve".
        assert cfg["WEATHER_LOCATION_NAME"] == ""
        assert cfg["WEATHER_LATITUDE"] == ""
        assert cfg["WEATHER_LONGITUDE"] == ""

    def test_toggle_only_save_without_clear_preserves_location(self, client, csrf_token, env_with_city, app) -> None:
        """Toggle-only regression: the bug #325 fixes is "empty City + Save
        = no-op". The new behavior must ONLY kick in when clear=1 is
        explicitly present. A toggle-only POST (WEATHER_ENABLED=false,
        no clear=1, no location_query) must preserve the city.

        This pins the locked invariant: clear=1 is the EXPLICIT opt-in.
        The bare empty-City save MUST stay a no-op for the location keys."""
        with app.app_context():
            app.config["ENV_FILE"] = env_with_city
            # No clear field. WEATHER_ENABLED unchecked = false (form path
            # synthesises). location_query absent.
            resp = self.post_form(
                client,
                csrf_token=csrf_token,
                section="weather",
                # NOTE: NO clear field, NO location_query, NO WEATHER_ENABLED
            )
        assert resp.status_code == 303, resp.data
        cfg = config.load_config(env_with_city)
        # WEATHER_ENABLED flipped to false (HTML checkbox unchecked = false).
        assert cfg["WEATHER_ENABLED"] == "false"
        # But the city + coords are PRESERVED — the #325 fix must not
        # regress into "any weather save clears the city".
        assert cfg["WEATHER_LOCATION_NAME"] == "Austin, Texas"
        assert cfg["WEATHER_LATITUDE"] == "30.27"
        assert cfg["WEATHER_LONGITUDE"] == "-97.74"

    def test_clear_form_removed_under_a10_ia(self, client, tmp_path, app) -> None:
        """#337 A10 (locked 2026-06-01 by /plan-design-review): the Clear
        affordance is removed entirely from the new IA. The Automatic radio
        IS the reset — once the mode pill is in, Clear became redundant AND
        ambiguous ('after clear, what state are we in?'). This pins the
        removal at the template level so a stale re-introduction can't drift
        back in (it would re-create the same UX trap A10 closed).

        The original test (test_clear_form_only_renders_when_any_weather_
        location_key_is_set) tested A10's predecessor — the Review I6
        coord-only render rule. That rule is preserved at the IA level by
        the Location section's Currently sublabel showing coords when
        WEATHER_LOCATION_NAME is empty (see test_currently_sublabel_*).

        The server-side `clear=1` handling stays as a defensive backstop in
        ``_coerce_payload`` for any orphaned client still POSTing it
        — but no template emits it anymore."""
        for shape in (
            "WEATHER_LOCATION_NAME=\nWEATHER_LATITUDE=\nWEATHER_LONGITUDE=\n",
            "WEATHER_LOCATION_NAME=\nWEATHER_LATITUDE=30.27\nWEATHER_LONGITUDE=-97.74\n",
            "WEATHER_LOCATION_NAME=Austin, Texas\nWEATHER_LATITUDE=30.27\nWEATHER_LONGITUDE=-97.74\n",
        ):
            env = tmp_path / "env.sh"
            env.write_text(shape)
            with app.app_context():
                app.config["ENV_FILE"] = str(env)
                body = client.get("/settings").data.decode()
            assert 'name="clear"' not in body, (
                "#337 A10: the Clear form is removed in the new IA. "
                "The Automatic pill IS the reset; Clear was redundant/ambiguous."
            )
            assert "Clear location" not in body, "A10: Clear-location button must not render in any env state"

    def test_clear_form_currently_hint_falls_back_to_coords(self, client, tmp_path, app) -> None:
        """Review I6 nicety: when WEATHER_LOCATION_NAME is empty but lat/lon
        are populated (env.sh.sample shape), the "Currently:" hint must
        surface the coords rather than rendering an em-dash that lies about
        the live weather state."""
        coord_only_env = tmp_path / "coord-only-env.sh"
        coord_only_env.write_text("WEATHER_LOCATION_NAME=\nWEATHER_LATITUDE=30.27\nWEATHER_LONGITUDE=-97.74\n")
        with app.app_context():
            app.config["ENV_FILE"] = str(coord_only_env)
            body = client.get("/settings").data.decode()
        assert "Currently:" in body
        assert "30.27, -97.74" in body, (
            "Currently hint must surface coords as a fallback when name is empty (review I6)"
        )


# ─── POST /api/settings (JSON) ──────────────────────────────────────────────


class TestApiSettingsPost(_PostHelpers):
    """`_post_json` is inherited from `_PostHelpers` (promoted there during
    the #337 /review-followup so the new sync-quick + system-tz test classes
    can share the same JSON-POST shape)."""

    def test_success_envelope_on_valid_save(self, client, csrf_token, env_file) -> None:
        resp = self._post_json(
            client,
            {"section": "units", "WEATHER_UNITS": "metric"},
            csrf_token=csrf_token,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert "WEATHER_UNITS" in data["saved"]
        assert config.load_config(env_file)["WEATHER_UNITS"] == "metric"

    def test_patch_merge_only_writes_provided_keys(self, client, csrf_token, env_file) -> None:
        before = config.load_config(env_file)
        resp = self._post_json(
            client,
            {"WEATHER_UNITS": "metric"},  # no section, no other keys
            csrf_token=csrf_token,
        )
        assert resp.status_code == 200
        after = config.load_config(env_file)
        assert after["WEATHER_UNITS"] == "metric"
        # Other keys untouched.
        assert after["WEATHER_LATITUDE"] == before["WEATHER_LATITUDE"]
        assert after["ALLOW_NSFW_QUOTES"] == before["ALLOW_NSFW_QUOTES"]

    def test_validation_envelope_on_bad_lat(self, client, csrf_token) -> None:
        resp = self._post_json(
            client,
            {"WEATHER_LATITUDE": "200"},
            csrf_token=csrf_token,
        )
        assert resp.status_code == 422
        data = resp.get_json()
        assert data["ok"] is False
        assert data["error"]["code"] == "validation_failed"
        assert "WEATHER_LATITUDE" in data["error"]["fields"]

    def test_403_on_missing_csrf(self, client) -> None:
        resp = self._post_json(client, {"WEATHER_UNITS": "metric"})
        assert resp.status_code == 403
        data = resp.get_json()
        assert data["ok"] is False
        assert data["error"]["code"] == "forbidden"

    def test_400_on_invalid_section(self, client, csrf_token) -> None:
        resp = self._post_json(
            client,
            {"section": "no-such-section", "WEATHER_UNITS": "metric"},
            csrf_token=csrf_token,
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"]["code"] == "invalid_section"

    def test_400_on_non_json_body(self, client, csrf_token) -> None:
        # Send form-encoded body to the JSON endpoint — should reject.
        resp = client.post(
            "/api/settings",
            data="not-json",
            content_type="application/json",
            base_url="http://litclock.local",
            headers={"Origin": "http://litclock.local"},
        )
        assert resp.status_code == 403  # CSRF rejects first (no token in body)

    def test_no_store_cache_header_on_api(self, client, csrf_token) -> None:
        resp = self._post_json(
            client,
            {"section": "units", "WEATHER_UNITS": "imperial"},
            csrf_token=csrf_token,
        )
        assert resp.headers.get("Cache-Control") == "no-store"

    def test_json_patch_does_not_synth_unrelated_booleans_to_false(self, client, csrf_token, env_file) -> None:
        """D2 strict PATCH-merge: a JSON caller setting one location field
        must not silently flip the unrelated WEATHER_ENABLED toggle to false.
        The HTML form path DOES synth missing checkboxes (unchecked checkbox
        doesn't submit) — but JSON callers explicitly send what they want changed.

        #337 A9 update: WEATHER_LATITUDE moved from the Weather section to the
        Location section; PATCH-merge invariant is unchanged but the section
        identifier in the test payload follows the new IA. Cross-section
        bool-isolation is the property being pinned (WEATHER_ENABLED lives in
        Weather, lat lives in Location — a write to one must not perturb the
        other).
        """
        # Pre-state: WEATHER_ENABLED is true.
        assert config.load_config(env_file)["WEATHER_ENABLED"] == "true"

        resp = self._post_json(
            client,
            {"section": "location", "WEATHER_LATITUDE": "33.5"},
            csrf_token=csrf_token,
        )
        assert resp.status_code == 200
        cfg = config.load_config(env_file)
        # Lat updated...
        assert cfg["WEATHER_LATITUDE"] == "33.5"
        # ...but the bool we never touched stayed put (lives in the Weather
        # section per A9 — Location-section PATCH leaves it alone).
        assert cfg["WEATHER_ENABLED"] == "true"

    def test_504_envelope_on_env_lock_timeout(self, client, csrf_token, env_file, monkeypatch) -> None:
        """#274 follow-up #4 — when `_exclusive_lock` raises TimeoutError
        (env.sh sidecar flock held > budget by another writer), the
        settings route must surface HTTP 504 with the structured
        `env_lock_timeout` envelope so the PWA can show a real "settings
        file is busy, retry" message instead of a generic 500 or a
        hanging spinner.

        Pins both the status code AND the envelope shape — settings.js's
        retry-toast UX gates on `error.code === "env_lock_timeout"` to
        prefer the actionable server message over the generic fallback.
        """
        import re

        def fake_atomic_update(*args, **kwargs):
            raise TimeoutError("env.sh lock held >30s — another writer is stuck.")

        monkeypatch.setattr(config, "atomic_update", fake_atomic_update)
        resp = self._post_json(
            client,
            {"section": "units", "WEATHER_UNITS": "metric"},
            csrf_token=csrf_token,
        )
        assert resp.status_code == 504, (
            f"TimeoutError from atomic_update must surface as HTTP 504 — got {resp.status_code}"
        )
        data = resp.get_json()
        assert data["ok"] is False
        assert data["error"]["code"] == "env_lock_timeout", (
            "structured envelope code must be env_lock_timeout — settings.js gates its retry-toast on this exact string"
        )
        # Slug-format pin: must follow the project-wide snake_case
        # envelope convention (errors.py:_DEFAULTS, errors.py:_slug_from_name).
        # Adversarial /review on PR-1b caught a SCREAMING_SNAKE outlier
        # that would have broken a future global slug-normalization
        # hardening; pin the format so a rename can't regress it.
        assert re.fullmatch(r"[a-z][a-z0-9_]*", data["error"]["code"]), (
            f"envelope code must match snake_case ^[a-z][a-z0-9_]*$; got: {data['error']['code']!r}"
        )
        # Message should be human-readable and mention retry semantics so
        # the PWA can pass it directly to the user.
        assert "busy" in data["error"]["message"].lower() or "try" in data["error"]["message"].lower(), (
            f"504 envelope message must hint at retry; got: {data['error']['message']!r}"
        )


# ─── geocoding integration ─────────────────────────────────────────────────


class TestApiGeocode:
    def test_returns_resolved_location(self, client) -> None:
        resp = client.get("/api/geocode?q=Austin%2C+TX")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["lat"] == "30.27"
        assert data["short_name"] == "Austin, Texas"

    def test_400_on_missing_q(self, client) -> None:
        resp = client.get("/api/geocode")
        assert resp.status_code == 400
        assert resp.get_json()["error"]["code"] == "invalid_request"

    def test_422_on_geocode_failure(self, client, monkeypatch) -> None:
        import geocoding

        monkeypatch.setattr(geocoding, "geocode_location", lambda q, country_code=None: {"error": "Location not found"})
        resp = client.get("/api/geocode?q=zzzzz")
        assert resp.status_code == 422
        data = resp.get_json()
        assert data["error"]["code"] == "geocode_failed"

    def test_worldwide_flag_skips_ip_bias(self, client, monkeypatch) -> None:
        """#337 /review testing-gap: `/api/geocode?worldwide=1` must call
        `geocode_location(query, country_code=None)` — no IP-country bias.
        Pin via spy + explosion if ip_geolocate is called (worldwide must
        skip the lookup entirely)."""
        import geocoding

        monkeypatch.setattr(geocoding, "ip_geolocate", lambda: pytest.fail("worldwide=1 must skip ip_geolocate"))
        seen_cc = []

        def fake_geocode(q, country_code=None):
            seen_cc.append(country_code)
            return {
                "lat": "51.5",
                "lon": "-0.14",
                "display_name": "Buckingham Palace, London, England",
                "timezone": "Europe/London",
                "country_code": "GB",
            }

        monkeypatch.setattr(geocoding, "geocode_location", fake_geocode)
        resp = client.get("/api/geocode?q=SW1A+1AA&worldwide=1")
        assert resp.status_code == 200
        assert seen_cc == [None]
        assert resp.get_json()["country_code"] == "GB"

    def test_worldwide_absent_keeps_ip_bias(self, client, monkeypatch) -> None:
        """Default (no worldwide param) must still pass the IP country to Nominatim."""
        import geocoding

        monkeypatch.setattr(geocoding, "ip_geolocate", lambda: {"country_code": "US"})
        seen_cc = []

        def fake_geocode(q, country_code=None):
            seen_cc.append(country_code)
            return {
                "lat": "30.27",
                "lon": "-97.74",
                "display_name": "Austin, TX",
                "timezone": "America/Chicago",
                "country_code": "US",
            }

        monkeypatch.setattr(geocoding, "geocode_location", fake_geocode)
        client.get("/api/geocode?q=Austin")
        assert seen_cc == ["US"]

    def test_country_code_in_response(self, client, monkeypatch) -> None:
        """#337 A16 — response bubbles the resolved country (None when absent)."""
        import geocoding

        monkeypatch.setattr(
            geocoding,
            "geocode_location",
            lambda q, country_code=None: {
                "lat": "30",
                "lon": "-97",
                "display_name": "Anywhere",
                "timezone": None,
                "country_code": None,
            },
        )
        resp = client.get("/api/geocode?q=somewhere&worldwide=1")
        assert resp.get_json()["country_code"] is None


class TestApiSystemSetTimezone(_PostHelpers):
    """#337 A18 — new steady-state timezone setter. Replaces the gated-on-
    handoff /api/handoff/set-timezone endpoint that silently no-op'd in the
    PWA settings context. Codex /review caught the no-op; this route is the
    always-on companion."""

    def _post_tz(self, client, payload, *, csrf_token: str | None = None):
        body = dict(payload)
        if csrf_token is not None and "csrf_token" not in body:
            body["csrf_token"] = csrf_token
        return client.post(
            "/api/system/set-timezone",
            json=body,
            base_url="http://litclock.local",
            headers={"Origin": "http://litclock.local"},
        )

    def test_sets_timezone_on_valid_input(self, client, csrf_token, monkeypatch) -> None:

        called = []
        monkeypatch.setattr(geocoding, "set_system_timezone", lambda tz: (called.append(tz), (True, None))[1])
        resp = self._post_tz(client, {"timezone": "America/Chicago"}, csrf_token=csrf_token)
        assert resp.status_code == 200, resp.data
        assert called == ["America/Chicago"]
        assert resp.get_json() == {"ok": True, "timezone": "America/Chicago"}

    def test_422_on_invalid_timezone(self, client, csrf_token, monkeypatch) -> None:

        monkeypatch.setattr(geocoding, "set_system_timezone", lambda tz: (False, "not in IANA list"))
        resp = self._post_tz(client, {"timezone": "Fake/Zone"}, csrf_token=csrf_token)
        assert resp.status_code == 422
        assert resp.get_json()["error"]["code"] == "invalid_timezone"

    def test_422_on_missing_timezone(self, client, csrf_token) -> None:
        resp = self._post_tz(client, {}, csrf_token=csrf_token)
        assert resp.status_code == 422
        assert resp.get_json()["error"]["code"] == "timezone_required"

    def test_403_without_csrf(self, client) -> None:
        """CRITICAL: this endpoint changes system state without confirm-token
        (unlike /api/system/reboot). CSRF is the only auth layer; missing
        token MUST 403."""
        import json as _json

        resp = client.post(
            "/api/system/set-timezone",
            data=_json.dumps({"timezone": "America/Chicago"}),
            headers={"Content-Type": "application/json", "Origin": "http://litclock.local"},
        )
        assert resp.status_code == 403


class TestSaveAndApplyTriggers(_PostHelpers):
    def test_save_triggers_ad_hoc_tick(self, client, csrf_token, _stub_systemctl) -> None:
        """D1: every successful save fires `systemctl start --no-block
        litclock.service` so the user sees their change land in ~3s."""
        resp = self.post_form(client, csrf_token=csrf_token, section="units", WEATHER_UNITS="metric")
        assert resp.status_code == 303
        _wait_for_tick_thread()
        assert any(
            ["start", "--no-block", "litclock.service"] == call[2:] and call[0] == "sudo" for call in _stub_systemctl
        ), f"no systemctl invocation: {_stub_systemctl}"

    def test_service_is_active_parses_stdout_not_exit_code(self, monkeypatch) -> None:
        """Hardware-QA fix 2026-04-29: on Pi Bookworm, `systemctl is-active
        --quiet` returns exit code 3 for the "activating" state, so an
        exit-code-only check would let the polling thread fire mid-render
        and have the start coalesced. _service_is_active must parse the
        state string instead and treat "activating" as busy."""
        from subprocess import CompletedProcess

        from control_server.routes import settings as settings_mod

        states_seen: list[bool] = []

        def fake_run_state(argv, *args, **kwargs):
            return CompletedProcess(argv, 3, "activating\n", "")

        monkeypatch.setattr(settings_mod.subprocess, "run", fake_run_state)
        states_seen.append(settings_mod._service_is_active("litclock.service"))

        def fake_run_inactive(argv, *args, **kwargs):
            return CompletedProcess(argv, 3, "inactive\n", "")

        monkeypatch.setattr(settings_mod.subprocess, "run", fake_run_inactive)
        states_seen.append(settings_mod._service_is_active("litclock.service"))

        # activating → busy (True), inactive → free (False)
        assert states_seen == [True, False]

    def test_ad_hoc_tick_polls_is_active_before_starting(self, client, csrf_token, _stub_systemctl) -> None:
        """Hardware-QA fix 2026-04-29: when a render is in flight (~9s),
        bare `systemctl start --no-block` of a oneshot unit is silently
        coalesced and the ad-hoc tick is dropped — user waits up to 60s
        for the next OnCalendar fire. Fix: poll `is-active` first, only
        fire after the in-flight render finishes."""
        resp = self.post_form(client, csrf_token=csrf_token, section="units", WEATHER_UNITS="metric")
        assert resp.status_code == 303
        _wait_for_tick_thread()
        # The tick thread must have called `is-active` BEFORE `start`.
        is_active_idx = next(
            (i for i, c in enumerate(_stub_systemctl) if len(c) >= 2 and c[1] == "is-active"),
            -1,
        )
        start_idx = next(
            (i for i, c in enumerate(_stub_systemctl) if "start" in c and "--no-block" in c),
            -1,
        )
        assert is_active_idx >= 0, f"is-active never called: {_stub_systemctl}"
        assert start_idx >= 0, f"start never called: {_stub_systemctl}"
        assert is_active_idx < start_idx, "is-active must precede start so we don't coalesce mid-render"

    def test_no_tick_on_validation_failure(self, client, csrf_token, _stub_systemctl) -> None:
        resp = self.post_form(
            client,
            csrf_token=csrf_token,
            section="location",
            WEATHER_LATITUDE="200",
            WEATHER_LONGITUDE="0",
        )
        assert resp.status_code == 422
        # No systemctl call when the save fails.
        assert _stub_systemctl == []

    def test_ad_hoc_tick_aborts_when_shutdown_imminent(self, client, csrf_token, _stub_systemctl) -> None:
        """#362 D7 — the ad-hoc tick thread polls is-active for up to 15s
        before firing ``systemctl start litclock.service``. If
        ``/api/system/{reboot,poweroff}`` flips ``_SHUTDOWN_IMMINENT``
        during that window, the thread MUST abort instead of firing a
        start that would re-open the timer-queued-job race.

        Hand the thread a True flag BEFORE the save fires so the abort
        path triggers deterministically (no real-time race in the test)."""
        from control_server.routes import system as system_mod

        # Flip the module-level flag BEFORE the save fires.
        with system_mod._SHUTDOWN_IMMINENT_LOCK:
            system_mod._SHUTDOWN_IMMINENT = True
        try:
            resp = self.post_form(client, csrf_token=csrf_token, section="units", WEATHER_UNITS="metric")
            assert resp.status_code == 303
            _wait_for_tick_thread()
            # The ad-hoc tick thread must have aborted before any
            # `systemctl start litclock.service` call. is-active polling
            # may or may not have fired (we tolerate either) but the
            # actual start MUST NOT appear in the captured calls.
            assert not any(
                "start" in call and "--no-block" in call and "litclock.service" in call for call in _stub_systemctl
            ), f"ad-hoc tick must abort when _SHUTDOWN_IMMINENT is set; saw start call in {_stub_systemctl}"
        finally:
            # Always reset the flag so subsequent tests aren't poisoned.
            with system_mod._SHUTDOWN_IMMINENT_LOCK:
                system_mod._SHUTDOWN_IMMINENT = False

    def test_geocode_triggers_timezone_update(self, client, csrf_token, env_file, monkeypatch) -> None:
        """When a city is supplied and resolution succeeds, set_system_timezone
        runs with the timezone derived from the new coordinates.

        #337 A9 update: section moved from 'weather' to 'location' per the new
        IA shift (lat/lon owned by Location, Weather is toggle only)."""
        tz_calls: list[str] = []

        monkeypatch.setattr(geocoding, "set_system_timezone", lambda tz: (tz_calls.append(tz), (True, None))[1])
        resp = self.post_form(
            client,
            csrf_token=csrf_token,
            section="location",
            WEATHER_LOCATION_MODE="specific",
            location_query="Austin, TX",
        )
        assert resp.status_code == 303
        assert tz_calls == ["America/Chicago"]
        # WEATHER_LOCATION_NAME populated server-side.
        cfg = config.load_config(env_file)
        assert "Austin" in cfg["WEATHER_LOCATION_NAME"]


class TestSyncQuickAndA14Backstop(_PostHelpers):
    """#337 /review testing-gaps fills. Pin the server-side behavior of:
    * A12 sync-quick on Specific→Auto switch + Auto save-as-refresh
    * A14 server backstop: MODE=specific + empty Place + empty Advanced → 422
    * A16 country-change UNITS-flip via Specific save (same vs different country)
    * A5/A12 worldwide flag on the save path (not just /api/geocode)
    """

    def test_specific_to_auto_fires_sync_quick(self, client, csrf_token, env_file, monkeypatch) -> None:
        """A12: when MODE flips specific→auto, _save_and_apply calls
        location_resolver.resolve_location_from_ip(retries=False)."""
        # Set up env with MODE=specific persisted.
        from pathlib import Path as _P

        cfg_pre = config.load_config(env_file)
        new_env = (
            "WEATHER_LOCATION_MODE=specific\n"
            + "\n".join(f"{k}={v}" for k, v in cfg_pre.items() if k != "WEATHER_LOCATION_MODE")
            + "\n"
        )
        _P(env_file).write_text(new_env)
        # Spy on the sync-quick resolver — replace it before _save_and_apply imports it.
        import location_resolver

        called = []
        monkeypatch.setattr(
            location_resolver,
            "resolve_location_from_ip",
            lambda retries=True, env_file=None: (called.append(retries), True)[1],
        )
        resp = self._post_json(client, {"section": "location", "WEATHER_LOCATION_MODE": "auto"}, csrf_token=csrf_token)
        assert resp.status_code == 200, resp.data
        assert called == [False], "Specific→Auto must call resolver with retries=False (sync-quick)"
        body = resp.get_json()
        assert body.get("sync_quick") in ("succeeded", "failed")
        assert body["sync_quick"] == "succeeded"

    def test_auto_save_refresh_fires_sync_quick(self, client, csrf_token, monkeypatch) -> None:
        """A12: tapping Save while already in Auto mode also fires sync-quick
        (the 'refresh detection' semantics)."""
        import location_resolver

        called = []
        monkeypatch.setattr(
            location_resolver,
            "resolve_location_from_ip",
            lambda retries=True, env_file=None: (called.append(retries), True)[1],
        )
        resp = self._post_json(client, {"section": "location", "WEATHER_LOCATION_MODE": "auto"}, csrf_token=csrf_token)
        assert resp.status_code == 200
        assert called == [False]

    def test_sync_quick_failure_surfaces_in_response(self, client, csrf_token, monkeypatch) -> None:
        """A7: hard-fail returns sync_quick='failed' so the PWA can render
        'city couldn't auto-detect; next reboot will retry.'"""
        import location_resolver

        monkeypatch.setattr(
            location_resolver,
            "resolve_location_from_ip",
            lambda retries=True, env_file=None: False,  # bool-False signals failure
        )
        resp = self._post_json(client, {"section": "location", "WEATHER_LOCATION_MODE": "auto"}, csrf_token=csrf_token)
        assert resp.status_code == 200
        assert resp.get_json()["sync_quick"] == "failed"

    def test_a14_empty_specific_returns_422_with_actionable_message(self, client, csrf_token) -> None:
        """A14 server backstop for the JS-disabled Save button. POST with
        MODE=specific but no Place / no Advanced coords → 422."""
        resp = self._post_json(
            client, {"section": "location", "WEATHER_LOCATION_MODE": "specific"}, csrf_token=csrf_token
        )
        assert resp.status_code == 422
        body = resp.get_json()
        assert body["error"]["code"] == "validation_failed"
        fields = body["error"].get("fields", {})
        assert "Type a place or pick Automatic" in fields.get("location_query", "")

    def test_a14_specific_with_advanced_coords_only_accepted(self, client, csrf_token, monkeypatch) -> None:
        """A17: Specific mode with raw coords (Advanced) but no Place still
        valid — the A14 backstop checks (location_query OR lat OR lon)."""

        monkeypatch.setattr(geocoding, "set_system_timezone", lambda tz: (True, None))
        resp = self._post_json(
            client,
            {
                "section": "location",
                "WEATHER_LOCATION_MODE": "specific",
                "WEATHER_LATITUDE": "28.62",
                "WEATHER_LONGITUDE": "77.22",
                "WEATHER_LOCATION_NAME": "Custom coords",
            },
            csrf_token=csrf_token,
        )
        assert resp.status_code == 200, resp.data

    def test_specific_save_same_country_preserves_units(self, client, csrf_token, env_file, monkeypatch) -> None:
        """A16: same-country geocode → WEATHER_UNITS preserved (user override survives)."""
        from pathlib import Path as _P

        cfg_pre = config.load_config(env_file)
        new_env = (
            "WEATHER_UNITS=metric\nWEATHER_IP_COUNTRY=US\n"
            + "\n".join(f"{k}={v}" for k, v in cfg_pre.items() if k not in ("WEATHER_UNITS", "WEATHER_IP_COUNTRY"))
            + "\n"
        )
        _P(env_file).write_text(new_env)
        import geocoding

        monkeypatch.setattr(geocoding, "set_system_timezone", lambda tz: (True, None))
        monkeypatch.setattr(
            geocoding,
            "geocode_location",
            lambda q, country_code=None: {
                "lat": "30.27",
                "lon": "-97.74",
                "display_name": "Austin, Travis County, Texas, USA",
                "timezone": "America/Chicago",
                "country_code": "US",  # same as persisted
            },
        )
        self._post_json(
            client,
            {"section": "location", "WEATHER_LOCATION_MODE": "specific", "location_query": "Austin"},
            csrf_token=csrf_token,
        )
        cfg = config.load_config(env_file)
        assert cfg["WEATHER_UNITS"] == "metric", "manual Celsius pick must survive same-country save"

    def test_specific_save_country_change_flips_units(self, client, csrf_token, env_file, monkeypatch) -> None:
        """A16: cross-country save flips UNITS to new country default."""
        from pathlib import Path as _P

        cfg_pre = config.load_config(env_file)
        new_env = (
            "WEATHER_UNITS=imperial\nWEATHER_IP_COUNTRY=US\n"
            + "\n".join(f"{k}={v}" for k, v in cfg_pre.items() if k not in ("WEATHER_UNITS", "WEATHER_IP_COUNTRY"))
            + "\n"
        )
        _P(env_file).write_text(new_env)
        import geocoding

        monkeypatch.setattr(geocoding, "set_system_timezone", lambda tz: (True, None))
        monkeypatch.setattr(
            geocoding,
            "geocode_location",
            lambda q, country_code=None: {
                "lat": "51.5",
                "lon": "-0.14",
                "display_name": "London, England, UK",
                "timezone": "Europe/London",
                "country_code": "GB",  # different from persisted US
            },
        )
        self._post_json(
            client,
            {"section": "location", "WEATHER_LOCATION_MODE": "specific", "location_query": "London"},
            csrf_token=csrf_token,
        )
        cfg = config.load_config(env_file)
        assert cfg["WEATHER_UNITS"] == "metric", "country change must flip UNITS to GB default"
        assert cfg["WEATHER_IP_COUNTRY"] == "GB"

    def test_worldwide_flag_skips_ip_bias_on_save(self, client, csrf_token, env_file, monkeypatch) -> None:
        """A5/A12: worldwide=on form field threads through to geocode_location(country_code=None)."""
        import geocoding

        monkeypatch.setattr(
            geocoding, "ip_geolocate", lambda: pytest.fail("worldwide=on must skip ip_geolocate on save path")
        )
        seen = []
        monkeypatch.setattr(geocoding, "set_system_timezone", lambda tz: (True, None))

        def fake_geocode(q, country_code=None):
            seen.append(country_code)
            return {
                "lat": "51.5",
                "lon": "-0.14",
                "display_name": "Buckingham Palace, London, England",
                "timezone": "Europe/London",
                "country_code": "GB",
            }

        monkeypatch.setattr(geocoding, "geocode_location", fake_geocode)
        resp = self.post_form(
            client,
            csrf_token=csrf_token,
            section="location",
            WEATHER_LOCATION_MODE="specific",
            location_query="SW1A 1AA",
            worldwide="on",
        )
        assert resp.status_code == 303
        assert seen == [None]
        # `worldwide` is form-state only — must NOT be written to env.sh.
        cfg = config.load_config(env_file)
        assert "worldwide" not in cfg


# ─── prelaunch sanity (route registration) ──────────────────────────────────


class TestRouteRegistration:
    def test_settings_blueprint_registered(self, app) -> None:
        rules = sorted(r.rule for r in app.url_map.iter_rules())
        for required in ("/settings", "/api/settings", "/api/geocode"):
            assert required in rules, f"missing route {required}"

    def test_settings_stub_was_replaced(self, app) -> None:
        """Index.py used to register a stub /settings → base.html.j2 with
        `stub_message=...`. After M3 lands, the live route owns it. This
        guards against the stub silently shadowing the real settings view."""
        endpoints = {r.endpoint for r in app.url_map.iter_rules()}
        # Real settings tab lives on the settings blueprint, not index.
        assert "settings.settings_tab" in endpoints
        assert "index.settings_stub" not in endpoints


# ─── _short_location_name (resolved-name sanitization) ──────────────────────


class TestShortLocationName:
    """Sanitization + truncation of Nominatim's `display_name` before it gets
    written to WEATHER_LOCATION_NAME. The free-form validator rejects
    backtick / `$` / linebreaks / NUL and caps at 120 chars; the resolver
    can produce values that hit any of those failure modes, and a 422
    AFTER a successful geocode would be a confusing UX bug. Pin the
    defenses."""

    def _short(self, display: str) -> str:
        from control_server.routes.settings import _short_location_name

        return _short_location_name(display)

    def test_drops_country_keeps_state(self) -> None:
        assert self._short("Austin, Travis County, Texas, United States") == "Austin, Texas"

    def test_two_part_input_passes_through(self) -> None:
        assert self._short("Tokyo, Japan") == "Tokyo, Japan"

    def test_strips_forbidden_dollar_sign(self) -> None:
        # Hypothetical Nominatim row carrying '$' — must not write a value
        # the validator would reject.
        cleaned = self._short("Big$ Sur, Monterey County, California, United States")
        assert "$" not in cleaned
        ok, _err = config.validate_setting("WEATHER_LOCATION_NAME", cleaned)
        assert ok is True

    def test_strips_backtick(self) -> None:
        cleaned = self._short("Place`name, County, State, Country")
        assert "`" not in cleaned
        ok, _err = config.validate_setting("WEATHER_LOCATION_NAME", cleaned)
        assert ok is True

    def test_truncates_overlong_to_validator_cap(self) -> None:
        long_input = "X" * 200 + ", County, State, Country"
        result = self._short(long_input)
        assert len(result) <= config.WEATHER_LOCATION_NAME_MAX_LEN
        ok, _err = config.validate_setting("WEATHER_LOCATION_NAME", result)
        assert ok is True

    def test_empty_input_returns_empty(self) -> None:
        assert self._short("") == ""
