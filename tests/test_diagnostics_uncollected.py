"""Tests for the #432 grey "Not yet collected" tier.

Three layers locked by the v0.214.4 plan (#432):

1. ``_compute_uncollected(values)`` — pure predicate. Reads the
   ``DIAG_LAST_IP_PATH`` config + the marker file on disk; gates the
   ``network`` + ``time-location`` sections per D3.
2. ``_compute_section_states(values)`` — applies uncollected-wins
   precedence in ONE place. (Inverted from the plan's locked anomaly-wins
   direction; see the helper's docstring for why — anomaly-wins on
   overlap would leave the user-reported fresh-flash bug unfixed.)
3. Route handlers (``/api/diagnostics`` + ``/diagnostics``) call only the
   helper so server + SSR + 30s poll never disagree on precedence.

The plan's "Iron Rule" (T1 — regression tests): every existing anomaly
path must still fire when its conditions are met independent of the new
uncollected logic. Three explicit regression cases below assert that.
"""

from __future__ import annotations

import json
import sys
import textwrap
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from control_server import create_app  # noqa: E402
from control_server.routes.diagnostics import (  # noqa: E402
    _anomalies,
    _sse,
)

# ---- Helpers ----------------------------------------------------------------


def _clean_values() -> dict:
    """A values dict where every gate is "clean" — no anomaly, no
    uncollected. Tests mutate per-case."""
    return {
        "cpu_temp_c": 50.0,
        "disk_free_pct": 50.0,
        "memory_free_mb": 200,
        "signal_dbm": -55,
        "lan_ip": "192.168.1.100",
        "ssid": "TestNet",
        "last_dhcp_at": None,
        "weather_enabled": True,
        "weather_location_name": "Austin, Texas",
        "weather_location_mode": "auto",
        # Relative to now, NOT a fixed date: the time-location anomaly trips
        # when last_ip_geo_at ages past ANOMALY_LAST_IPGEO_AGE_S (7 days), so a
        # hardcoded timestamp is a date-bomb that silently reddens this "clean"
        # fixture once wall-clock passes the threshold. 1 day keeps it fresh.
        "last_ip_geo_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        "service_states": {"litclock.service": {"state": "active"}},
        "quote": "non-empty",
        "picked_at": time.time(),
        "setup_complete": True,
        "handoff_complete": True,
        "recent_log_entries": [],
    }


@pytest.fixture
def app_with_marker(tmp_path):
    """Build an app whose ``DIAG_LAST_IP_PATH`` points at a present marker.

    Tests that need the marker absent override via ``app.config`` directly
    or use the ``app_no_marker`` fixture below.

    ``DIAG_COLLECTED_MARKER_PATH`` is pinned at a never-exists path so these
    legacy cases deterministically exercise the v0.214.4 tmpfs fallback in
    ``_read_collected_sections`` (#445) regardless of the host's
    ``/var/lib/litclock`` — the persistent-marker behavior is covered
    separately in :class:`TestPersistentCollectedMarker`.
    """
    last_ip = tmp_path / "last-rendered-ip"
    last_ip.write_text("192.168.1.100\n")
    env = tmp_path / "env.sh"
    env.write_text("WEATHER_ENABLED=false\n")
    return create_app(
        {
            "ENV_FILE": str(env),
            "DIAG_LAST_IP_PATH": str(last_ip),
            "DIAG_COLLECTED_MARKER_PATH": str(tmp_path / "no-persistent-marker.json"),
        }
    )


@pytest.fixture
def app_no_marker(tmp_path):
    """Build an app whose ``DIAG_LAST_IP_PATH`` is absent on disk. The
    persistent collected-marker is also absent, so the predicate falls back
    to the tmpfs check (#445)."""
    env = tmp_path / "env.sh"
    env.write_text("WEATHER_ENABLED=false\n")
    return create_app(
        {
            "ENV_FILE": str(env),
            "DIAG_LAST_IP_PATH": str(tmp_path / "never-exists"),
            "DIAG_COLLECTED_MARKER_PATH": str(tmp_path / "no-persistent-marker.json"),
        }
    )


# ---- _compute_uncollected — network -----------------------------------------


class TestComputeUncollectedNetwork:
    """D3 network gate: marker absent AND lan_ip empty AND ssid empty."""

    def test_marker_absent_lan_ip_empty_ssid_empty_marks_uncollected(self, app_no_marker):
        v = _clean_values()
        v["lan_ip"] = ""
        v["ssid"] = ""
        with app_no_marker.app_context():
            assert _anomalies._compute_uncollected(v) == ["network"]

    def test_marker_present_does_not_mark_uncollected(self, app_with_marker):
        v = _clean_values()
        v["lan_ip"] = ""
        v["ssid"] = ""
        with app_with_marker.app_context():
            assert "network" not in _anomalies._compute_uncollected(v)

    def test_ssid_present_does_not_mark_uncollected(self, app_no_marker):
        # The live-network sanity gate (D3 condition 3): SSID present with
        # empty lan_ip is a real DHCP failure, NOT uncollected. Must stay
        # an anomaly, not be silently muted to grey.
        v = _clean_values()
        v["lan_ip"] = ""
        v["ssid"] = "TestNet"
        with app_no_marker.app_context():
            assert "network" not in _anomalies._compute_uncollected(v)

    def test_lan_ip_present_does_not_mark_uncollected(self, app_no_marker):
        v = _clean_values()
        v["lan_ip"] = "192.168.1.5"
        v["ssid"] = ""
        with app_no_marker.app_context():
            assert "network" not in _anomalies._compute_uncollected(v)

    def test_low_signal_anomaly_blocks_uncollected(self, app_no_marker):
        # Fix A (Codex adversarial #1): if signal_dbm independently trips
        # the ANOMALY_SIGNAL_DBM threshold, refuse to mark the section
        # uncollected. Pre-fix, _compute_section_states would suppress
        # the signal anomaly along with the marker-empty one — masking a
        # real radio-degradation problem as "Just settling in."
        v = _clean_values()
        v["lan_ip"] = ""
        v["ssid"] = ""
        v["signal_dbm"] = -85  # below ANOMALY_SIGNAL_DBM = -75
        with app_no_marker.app_context():
            assert "network" not in _anomalies._compute_uncollected(v)
            # And the precedence helper preserves the anomaly tier.
            anomalies, uncollected = _anomalies._compute_section_states(v)
        assert "network" in anomalies
        assert "network" not in uncollected

    def test_stale_dhcp_anomaly_blocks_uncollected(self, app_no_marker):
        # Fix A symmetric: stale DHCP age (> 24h) is a real renewal
        # failure, not "data was never collected." Uncollected must NOT
        # fire even if marker absent + lan_ip + ssid all empty.
        v = _clean_values()
        v["lan_ip"] = ""
        v["ssid"] = ""
        v["last_dhcp_at"] = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
        with app_no_marker.app_context():
            assert "network" not in _anomalies._compute_uncollected(v)
            anomalies, uncollected = _anomalies._compute_section_states(v)
        assert "network" in anomalies
        assert "network" not in uncollected


# ---- _compute_uncollected — time-location -----------------------------------


class TestComputeUncollectedTimeLocation:
    """D3 time-location gate: weather_enabled AND mode=auto AND name empty AND
    last_ip_geo_at empty."""

    def test_all_four_conditions_marks_uncollected(self, app_with_marker):
        v = _clean_values()
        v["weather_enabled"] = True
        v["weather_location_mode"] = "auto"
        v["weather_location_name"] = ""
        v["last_ip_geo_at"] = ""
        with app_with_marker.app_context():
            assert _anomalies._compute_uncollected(v) == ["time-location"]

    def test_specific_mode_with_empty_name_stays_anomaly(self, app_with_marker):
        # D3 carve-out: a user-configured specific mode with no name is a
        # real anomaly, not uncollected. The user deliberately picked a
        # location; if it didn't resolve, that's a failure to surface.
        v = _clean_values()
        v["weather_enabled"] = True
        v["weather_location_mode"] = "specific"
        v["weather_location_name"] = ""
        v["last_ip_geo_at"] = ""
        with app_with_marker.app_context():
            assert "time-location" not in _anomalies._compute_uncollected(v)

    def test_weather_disabled_does_not_mark_uncollected(self, app_with_marker):
        # Weather off → time-location section is information-only; no
        # uncollected tier applies because the user isn't expecting data.
        v = _clean_values()
        v["weather_enabled"] = False
        v["weather_location_mode"] = "auto"
        v["weather_location_name"] = ""
        v["last_ip_geo_at"] = ""
        with app_with_marker.app_context():
            assert "time-location" not in _anomalies._compute_uncollected(v)

    def test_name_present_does_not_mark_uncollected(self, app_with_marker):
        v = _clean_values()
        v["weather_location_name"] = "Austin, Texas"
        v["last_ip_geo_at"] = ""
        with app_with_marker.app_context():
            assert "time-location" not in _anomalies._compute_uncollected(v)

    def test_last_ip_geo_at_present_does_not_mark_uncollected(self, app_with_marker):
        # D3 condition 4 isolation: if the last IP-geo timestamp was
        # written (data WAS collected once, even if name is now empty
        # because the user cleared it), the section is NOT uncollected.
        # Locks the 4-condition gate against degradation to 3.
        v = _clean_values()
        v["weather_enabled"] = True
        v["weather_location_mode"] = "auto"
        v["weather_location_name"] = ""
        v["last_ip_geo_at"] = "2026-06-11T12:00:00+00:00"
        with app_with_marker.app_context():
            assert "time-location" not in _anomalies._compute_uncollected(v)

    def test_mode_none_treated_as_auto(self, app_with_marker):
        # Fix C (Codex structured review #1): legacy / pre-#337 env files
        # don't set WEATHER_LOCATION_MODE, so the predicate accepts None
        # and "" alongside "auto". Without this, legacy Pis kept showing
        # the orange "Location stale" false positive #432 was opened to
        # remove. Locks the loosened gate so a future tightening that
        # demands an explicit "auto" string would surface here.
        v = _clean_values()
        v["weather_enabled"] = True
        v["weather_location_mode"] = None
        v["weather_location_name"] = ""
        v["last_ip_geo_at"] = ""
        with app_with_marker.app_context():
            assert _anomalies._compute_uncollected(v) == ["time-location"]
        v["weather_location_mode"] = ""
        with app_with_marker.app_context():
            assert _anomalies._compute_uncollected(v) == ["time-location"]

    def test_weather_enabled_string_forms_still_trip_uncollected(self, app_no_marker):
        # config readers may surface WEATHER_ENABLED as the literal string
        # 'true' or '1' depending on env loader path. The predicate
        # accepts both alongside Python True — assert all three forms
        # produce identical uncollected output.
        base = _clean_values()
        base["weather_enabled"] = True
        base["weather_location_mode"] = "auto"
        base["weather_location_name"] = ""
        base["last_ip_geo_at"] = ""
        with app_no_marker.app_context():
            assert _anomalies._compute_uncollected(base) == ["time-location"]
            base["weather_enabled"] = "true"
            assert _anomalies._compute_uncollected(base) == ["time-location"]
            base["weather_enabled"] = "1"
            assert _anomalies._compute_uncollected(base) == ["time-location"]


# ---- _compute_section_states — precedence truth table -----------------------


class TestSectionStatesPrecedence:
    """The truth table — UNCOLLECTED wins everywhere it overlaps with an
    anomaly (the helper's documented inversion of the plan's locked
    direction; see _compute_section_states for the rationale). The
    helper is the single source of truth."""

    def test_anomaly_only_returns_anomaly_not_uncollected(self, app_with_marker):
        # Row 2: anomaly true + uncollected_raw false → anomaly.
        v = _clean_values()
        v["signal_dbm"] = -85  # trips the network anomaly
        with app_with_marker.app_context():
            anomalies, uncollected = _anomalies._compute_section_states(v)
        assert "network" in anomalies
        assert "network" not in uncollected

    def test_uncollected_wins_on_overlap_fresh_flash_case(self, app_no_marker):
        # The user-reported fresh-flash case from #432: marker absent,
        # lan_ip empty, ssid empty, weather_enabled, mode=auto, name
        # empty, last_ip_geo_at empty. BOTH predicates fire for BOTH
        # sections (network: lan_ip empty trips anomaly + uncollected;
        # time-location: name empty trips anomaly + uncollected).
        # Uncollected-wins precedence ensures the user sees grey, not the
        # orange pills that #432 was opened to fix.
        v = _clean_values()
        v["lan_ip"] = ""
        v["ssid"] = ""
        v["last_dhcp_at"] = None
        v["weather_enabled"] = True
        v["weather_location_mode"] = "auto"
        v["weather_location_name"] = ""
        v["last_ip_geo_at"] = ""
        with app_no_marker.app_context():
            anomalies, uncollected = _anomalies._compute_section_states(v)
        assert "network" in uncollected
        assert "network" not in anomalies
        assert "time-location" in uncollected
        assert "time-location" not in anomalies

    def test_neither_returns_ok_in_both_lists(self, app_with_marker):
        # Row 4: anomaly false + uncollected_raw false → ok.
        v = _clean_values()
        with app_with_marker.app_context():
            anomalies, uncollected = _anomalies._compute_section_states(v)
        assert "network" not in anomalies
        assert "network" not in uncollected
        assert "time-location" not in anomalies
        assert "time-location" not in uncollected

    def test_overlap_row_uncollected_wins(self, app_no_marker, monkeypatch):
        # Row 1 (truth-table — both predicates fire). Per the helper's
        # docstring (uncollected wins on overlap), a section appearing
        # in BOTH raw lists is moved to uncollected and removed from
        # anomalies. The carve-out justification: the user-reported #432
        # bug only closes when the grey tier paints in the overlap case.
        v = _clean_values()
        v["weather_enabled"] = True
        v["weather_location_mode"] = "specific"
        v["weather_location_name"] = ""  # trips time-location anomaly

        # Force the overlap by stubbing _compute_uncollected to claim
        # time-location IS uncollected, regardless of what the real
        # predicate would say. Isolates the precedence helper from the
        # uncollected predicate's gating logic.
        monkeypatch.setattr(_anomalies, "_compute_uncollected", lambda values: ["time-location"])
        with app_no_marker.app_context():
            anomalies, uncollected = _anomalies._compute_section_states(v)

        assert "time-location" in uncollected, "uncollected-wins precedence: overlap row paints grey, not orange"
        assert "time-location" not in anomalies


# ---- Iron-rule regression: _compute_anomalies still fires unchanged ---------


class TestAnomalyRegressionUnchanged:
    """Per the plan's IRON RULE for T1: every existing anomaly trigger must
    still fire when its conditions are met, independent of the new
    uncollected logic."""

    def test_low_signal_still_trips_network_anomaly(self, app_no_marker):
        v = _clean_values()
        v["signal_dbm"] = -85  # below ANOMALY_SIGNAL_DBM = -75
        with app_no_marker.app_context():
            anomalies, uncollected = _anomalies._compute_section_states(v)
        assert "network" in anomalies
        assert "network" not in uncollected

    def test_stale_dhcp_still_trips_network_anomaly(self, app_no_marker):
        v = _clean_values()
        # 25 h old DHCP → past the 24 h threshold.
        v["last_dhcp_at"] = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
        with app_no_marker.app_context():
            anomalies, uncollected = _anomalies._compute_section_states(v)
        assert "network" in anomalies

    def test_lan_ip_empty_with_ssid_present_stays_anomaly_not_uncollected(self, app_no_marker):
        # The SSID-present sanity gate: association with no IP is a real
        # DHCP failure, NOT uncollected. Must remain a real network
        # anomaly (lan_ip empty trips _compute_anomalies' network branch).
        v = _clean_values()
        v["lan_ip"] = ""
        v["ssid"] = "TestNet"
        with app_no_marker.app_context():
            anomalies, uncollected = _anomalies._compute_section_states(v)
        assert "network" in anomalies
        assert "network" not in uncollected


# ---- Error handling — predicate stays pure under filesystem errors ----------


class TestComputeUncollectedErrorPath:
    def test_filesystem_oserror_conservatively_marks_marker_as_present(self, app_with_marker, monkeypatch):
        # Per the plan's failure-modes table: OSError on Path.exists() →
        # conservative anomaly hiding — section stays in last-known state
        # rather than flapping to grey.
        def _raise(self):
            raise OSError("simulated EACCES")

        monkeypatch.setattr(Path, "exists", _raise)
        v = _clean_values()
        v["lan_ip"] = ""
        v["ssid"] = ""
        with app_with_marker.app_context():
            assert "network" not in _anomalies._compute_uncollected(v)

    def test_outside_app_context_falls_back_to_default(self, tmp_path, monkeypatch):
        # The predicate is pure-Python and must not raise when called
        # without a Flask app context — falls back to the env default.
        monkeypatch.setattr(
            _anomalies,
            "DEFAULT_LAST_RENDERED_IP_PATH",
            str(tmp_path / "never-exists"),
        )
        v = _clean_values()
        v["lan_ip"] = ""
        v["ssid"] = ""
        # No app_context() — call directly.
        assert _anomalies._compute_uncollected(v) == ["network"]

    def test_empty_marker_path_is_treated_as_absent(self, app_with_marker):
        # Operator override DIAG_LAST_IP_PATH='' — the predicate's
        # `bool(marker_path) and Path(marker_path).exists()` short-circuits
        # on the empty string, treating the marker as absent. Locks the
        # short-circuit behavior so a future refactor that drops the
        # bool() guard would surface here.
        app_with_marker.config["DIAG_LAST_IP_PATH"] = ""
        v = _clean_values()
        v["lan_ip"] = ""
        v["ssid"] = ""
        with app_with_marker.app_context():
            assert "network" in _anomalies._compute_uncollected(v)


# ---- Route-level integration — /api/diagnostics envelope --------------------


@pytest.fixture
def integ_app(tmp_path):
    """End-to-end fixture: env.sh + missing marker + handoff-incomplete so
    the JSON envelope carries the new ``uncollected`` field."""
    env = tmp_path / "env.sh"
    env.write_text(
        textwrap.dedent("""\
            WEATHER_LATITUDE=
            WEATHER_LONGITUDE=
            WEATHER_LOCATION_NAME=
            WEATHER_LOCATION_MODE=auto
            WEATHER_IP_COUNTRY=
            WEATHER_UNITS=
            WEATHER_ENABLED=true
            ALLOW_NSFW_QUOTES=false
        """)
    )
    quote_path = tmp_path / "current-quote.json"
    quote_path.write_text(
        json.dumps(
            {
                "quote": "non-empty",
                "author": "Author",
                "title": "Title",
                "time": "12:00",
                "picked_at": time.time(),
            }
        )
    )
    setup_complete = tmp_path / ".setup-complete"
    setup_complete.touch()
    handoff_complete = tmp_path / ".handoff-complete"
    handoff_complete.touch()
    images_version = tmp_path / ".images-version"
    images_version.write_text("v4\n")
    # Marker INTENTIONALLY absent — this is the fresh-flash / fresh-update
    # case that #432 is opening to fix.
    return create_app(
        {
            "ENV_FILE": str(env),
            "DIAG_OS_RELEASE_PATH": "/nonexistent",
            "DIAG_PROC_UPTIME_PATH": "/nonexistent",
            "DIAG_PROC_MEMINFO_PATH": "/nonexistent",
            "DIAG_DISK_TARGET": str(tmp_path),
            "DIAG_LAST_IP_PATH": str(tmp_path / "never-exists"),
            "DIAG_CURRENT_QUOTE_PATH": str(quote_path),
            "DIAG_IMAGES_VERSION_PATH": str(images_version),
            "DIAG_GIFT_MODE_MARKER": str(tmp_path / "never-exists-gift"),
            "DIAG_THERMAL_PATH": "/nonexistent",
            "SETUP_COMPLETE_FILE": str(setup_complete),
            "HANDOFF_COMPLETE_FILE": str(handoff_complete),
        }
    )


class TestEnvelopeUncollectedField:
    def test_api_diagnostics_envelope_contains_uncollected_list(self, integ_app, monkeypatch):
        # Patch the network readers so lan_ip + ssid come back empty
        # (mirrors a fresh-flash Pi where nm-dispatcher hasn't fired).
        monkeypatch.setattr(_sse, "collect_diagnostics", lambda: _fresh_values())
        client = integ_app.test_client()
        resp = client.get("/api/diagnostics")
        assert resp.status_code == 200
        body = resp.get_json()
        assert "uncollected" in body, "envelope must carry the new uncollected field"
        assert isinstance(body["uncollected"], list)
        # Both sections are in the "data never collected" state on this
        # fresh-flash fixture.
        assert "network" in body["uncollected"]
        assert "time-location" in body["uncollected"]
        # And neither tripped a real anomaly (precedence preserved).
        assert "network" not in body["anomalies"]
        assert "time-location" not in body["anomalies"]

    def test_page_diagnostics_html_carries_muted_pill_class(self, integ_app, monkeypatch):
        monkeypatch.setattr(_sse, "collect_diagnostics", lambda: _fresh_values())
        client = integ_app.test_client()
        resp = client.get("/diagnostics")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        # Both uncollected sections rendered with the muted pill class.
        assert "diag-section__pill--muted" in html
        # Section-aware placeholder copy lands in SSR (no timing claim).
        assert "Network details fill in once your clock sees a network event." in html
        assert "Location details fill in once your clock resolves its location." in html
        # Banner reads "Just settling in." (the settling tier).
        assert "Just settling in." in html
        # Body copy reflects BOTH sections.
        assert "first network and location checks" in html
        # F8 fix — the live region wraps title+body permanently so SR
        # users hear severity changes including settling → ok recovery,
        # WITHOUT re-announcing the 30s-updating meta line. The outer
        # banner must NOT carry role/aria-live; the inner wrapper does.
        assert "data-diag-banner-live" in html
        assert 'role="status"' in html
        assert 'aria-live="polite"' in html
        # Negative assertion: the outer banner element's opening tag must
        # not include role="status" anymore (pre-F8 the SSR put it there
        # only during settling; F8 moves it to the wrapper permanently).
        banner_open = html.find('<section class="status-banner')
        assert banner_open >= 0
        banner_tag = html[banner_open : banner_open + 400]
        assert 'role="status"' not in banner_tag, (
            "outer banner must not carry role=status after F8 fix — the live "
            "wrapper inside __copy is the authoritative live region"
        )

    def test_envelope_uncollected_empty_when_marker_present(self, integ_app, monkeypatch, tmp_path):
        # With the marker file present + clean values, neither section is
        # uncollected. Backward compat assertion: `uncollected` is always
        # a list (never null), even when empty.
        marker = tmp_path / "marker-present"
        marker.write_text("192.168.1.100\n")
        integ_app.config["DIAG_LAST_IP_PATH"] = str(marker)

        def _clean_collected():
            v = _fresh_values()
            v["lan_ip"] = "192.168.1.100"
            v["ssid"] = "TestNet"
            v["weather_location_name"] = "Austin, Texas"
            v["last_ip_geo_at"] = "2026-06-11T00:00:00+00:00"
            return v

        monkeypatch.setattr(_sse, "collect_diagnostics", _clean_collected)
        client = integ_app.test_client()
        resp = client.get("/api/diagnostics")
        body = resp.get_json()
        assert body["uncollected"] == []


def _fresh_values() -> dict:
    """A complete collect_diagnostics()-shaped dict for the fresh-flash
    case: network marker absent, lan_ip + ssid empty, weather enabled +
    mode=auto + name empty + last_ip_geo_at empty.

    Must include every key in PRIVACY_POLICY so the schema-match warning
    doesn't fire mid-test (the route's _check_schema_match calls
    current_app.logger.warning, not raise, but keeping the schema in
    lockstep with prod payload keeps this test honest)."""
    return {
        # build-version
        "app_version": "v0.214.4-test",
        "git_head": "abc1234",
        "images_version": "v4",
        "last_update_at": None,
        "last_update_version": None,
        # system
        "kernel": None,
        "os_release": None,
        "uptime_s": 60,
        "uptime_human": "1m",
        "cpu_temp_c": 50.0,
        "memory_free_mb": 200,
        "disk_free_pct": 50.0,
        # network — uncollected case
        "iface": None,
        "ssid": "",
        "lan_ip": "",
        "gateway": None,
        "signal_dbm": -55,
        "last_dhcp_at": None,
        # time-location — uncollected case
        "timezone": "America/Chicago",
        "weather_location_name": "",
        "weather_lat": None,
        "weather_lon": None,
        "weather_location_mode": "auto",
        "weather_ip_country": None,
        "weather_units": None,
        "weather_enabled": True,
        "last_ip_geo_at": "",
        # services
        "service_states": {"litclock.service": {"state": "active", "journal_tail": []}},
        # last-quote
        "quote": "non-empty",
        "author": "Author",
        "title": "Title",
        "time": "12:00",
        "picked_at": time.time(),
        # setup-markers
        "setup_complete": True,
        "handoff_complete": True,
        "gift_mode_active": False,
        # allowlist flags
        "allow_nsfw_quotes": False,
        # recent log entries
        "recent_log_entries": [],
    }


# ---- #445 persistent collected-marker --------------------------------------


def _app_with_persistent_marker(tmp_path, marker_obj, *, last_ip_present=False):
    """Build an app with a persistent collected-marker on disk.

    ``marker_obj``: a dict to ``json.dump`` as the marker, a raw ``str`` for
    the malformed-file case, or ``None`` to leave the file absent.
    ``last_ip_present``: whether the legacy tmpfs marker also exists — used to
    prove the persistent marker (not the tmpfs file) drives the verdict once
    it exists."""
    env = tmp_path / "env.sh"
    env.write_text("WEATHER_ENABLED=false\n")
    marker = tmp_path / ".last-collected-marker.json"
    if marker_obj is not None:
        marker.write_text(marker_obj if isinstance(marker_obj, str) else json.dumps(marker_obj))
    last_ip = tmp_path / "last-rendered-ip"
    if last_ip_present:
        last_ip.write_text("192.168.1.100\n")
    return create_app(
        {
            "ENV_FILE": str(env),
            "DIAG_LAST_IP_PATH": str(last_ip),
            "DIAG_COLLECTED_MARKER_PATH": str(marker),
        }
    )


class TestPersistentCollectedMarker:
    """#445 — the persistent marker (section key present == ever collected)
    replaces the reboot-wiped tmpfs check, with fallback to the tmpfs check
    (network) / env-only gate (time-location) when the marker is
    absent/unreadable."""

    def _network_unseen(self) -> dict:
        v = _clean_values()
        v["lan_ip"] = ""
        v["ssid"] = ""
        return v

    def _time_location_unseen(self) -> dict:
        v = _clean_values()
        v["weather_location_name"] = ""
        v["last_ip_geo_at"] = ""
        return v

    def test_network_key_present_not_uncollected(self, tmp_path):
        # network recorded → NOT grey even though lan_ip + ssid empty (the
        # exact post-reboot window the old tmpfs predicate flashed grey on).
        app = _app_with_persistent_marker(tmp_path, {"network": "2026-06-13T00:00:00+00:00"})
        with app.app_context():
            assert "network" not in _anomalies._compute_uncollected(self._network_unseen())

    def test_network_key_absent_marks_uncollected_ignoring_tmpfs(self, tmp_path):
        # Marker present but no network key. last_ip_present=True proves the
        # tmpfs file is NOT consulted once the persistent marker exists.
        app = _app_with_persistent_marker(
            tmp_path, {"time-location": "2026-06-13T00:00:00+00:00"}, last_ip_present=True
        )
        with app.app_context():
            assert "network" in _anomalies._compute_uncollected(self._network_unseen())

    def test_time_location_key_present_not_uncollected(self, tmp_path):
        app = _app_with_persistent_marker(tmp_path, {"time-location": "2026-06-13T00:00:00+00:00"})
        with app.app_context():
            assert "time-location" not in _anomalies._compute_uncollected(self._time_location_unseen())

    def test_time_location_key_absent_marks_uncollected(self, tmp_path):
        app = _app_with_persistent_marker(tmp_path, {"network": "2026-06-13T00:00:00+00:00"})
        with app.app_context():
            assert "time-location" in _anomalies._compute_uncollected(self._time_location_unseen())

    def test_empty_marker_both_uncollected(self, tmp_path):
        app = _app_with_persistent_marker(tmp_path, {})
        v = self._network_unseen()
        v["weather_location_name"] = ""
        v["last_ip_geo_at"] = ""
        with app.app_context():
            out = _anomalies._compute_uncollected(v)
        assert "network" in out
        assert "time-location" in out

    def test_malformed_marker_falls_back_to_legacy(self, tmp_path):
        # Garbage JSON → _read_collected_sections returns None → legacy:
        # network uses tmpfs existence (absent → uncollected); time-location
        # uses the env-only gate (→ uncollected).
        app = _app_with_persistent_marker(tmp_path, "{not valid json")
        v = self._network_unseen()
        v["weather_location_name"] = ""
        v["last_ip_geo_at"] = ""
        with app.app_context():
            out = _anomalies._compute_uncollected(v)
        assert "network" in out
        assert "time-location" in out

    def test_absent_marker_falls_back_to_tmpfs_present(self, tmp_path):
        # No persistent marker; tmpfs last-rendered-ip PRESENT → legacy path
        # says network was collected (not grey).
        app = _app_with_persistent_marker(tmp_path, None, last_ip_present=True)
        with app.app_context():
            assert "network" not in _anomalies._compute_uncollected(self._network_unseen())

    def test_read_collected_sections_returns_key_set(self, tmp_path):
        app = _app_with_persistent_marker(tmp_path, {"network": "x", "time-location": "y"})
        with app.app_context():
            assert _anomalies._read_collected_sections() == {"network", "time-location"}

    def test_read_collected_sections_absent_returns_none(self, tmp_path):
        app = _app_with_persistent_marker(tmp_path, None)
        with app.app_context():
            assert _anomalies._read_collected_sections() is None

    def test_read_collected_sections_non_object_returns_none(self, tmp_path):
        # A JSON array (not an object) is not a valid marker → legacy fallback.
        app = _app_with_persistent_marker(tmp_path, "[1, 2, 3]")
        with app.app_context():
            assert _anomalies._read_collected_sections() is None


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
