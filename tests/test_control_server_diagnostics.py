"""Tests for control_server/routes/diagnostics.py (#416 PR2 T6 + T7b).

Covers:
- collect_diagnostics() schema gate (matches PRIVACY_POLICY keys exactly).
- Per-row reader robustness when source files are missing.
- Anomaly detector for each section's locked threshold.
- GET /api/diagnostics response envelope.
- GET /diagnostics HTML placeholder.
- build_copy_payload() default redaction + reveal toggle.
"""

from __future__ import annotations

import json
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from control_server import create_app  # noqa: E402
from control_server._diagnostics_privacy import (  # noqa: E402
    PRIVACY_POLICY,
    REDACTED_VALUE,
    schema_keys,
)
from control_server.routes import diagnostics  # noqa: E402


@pytest.fixture
def diag_env(tmp_path):
    """Build a usable app with all DIAG_* config keys redirected to
    tmp_path (or to empty values so reads degrade to None / em-dash)."""
    env = tmp_path / "env.sh"
    env.write_text(
        textwrap.dedent("""\
            WEATHER_LATITUDE=37.7749
            WEATHER_LONGITUDE=-122.4194
            WEATHER_LOCATION_NAME=San Francisco
            WEATHER_LOCATION_MODE=specific
            WEATHER_IP_COUNTRY=US
            WEATHER_UNITS=imperial
            WEATHER_ENABLED=true
            ALLOW_NSFW_QUOTES=false
        """)
    )
    quote_path = tmp_path / "current-quote.json"
    quote_path.write_text(
        json.dumps(
            {
                "quote": "It was a bright cold day in April",
                "author": "George Orwell",
                "title": "1984",
                "time": "13:00",
                "picked_at": time.time(),
            }
        )
    )
    setup_complete = tmp_path / ".setup-complete"
    setup_complete.touch()
    handoff_complete = tmp_path / ".handoff-complete"
    handoff_complete.touch()
    images_version = tmp_path / ".images-version"
    images_version.write_text("litclock-images-v4\n")
    last_ip = tmp_path / "last-rendered-ip"
    last_ip.write_text("192.168.1.100\n")

    test_config = {
        "ENV_FILE": str(env),
        "DIAG_OS_RELEASE_PATH": "/nonexistent/os-release",
        "DIAG_PROC_UPTIME_PATH": "/nonexistent/uptime",
        "DIAG_PROC_MEMINFO_PATH": "/nonexistent/meminfo",
        "DIAG_DISK_TARGET": str(tmp_path),
        "DIAG_LAST_IP_PATH": str(last_ip),
        "DIAG_CURRENT_QUOTE_PATH": str(quote_path),
        "DIAG_IMAGES_VERSION_PATH": str(images_version),
        "DIAG_GIFT_MODE_MARKER": str(tmp_path / ".never-exists"),
        "DIAG_THERMAL_PATH": "/nonexistent/thermal",
        "SETUP_COMPLETE_FILE": str(setup_complete),
        "HANDOFF_COMPLETE_FILE": str(handoff_complete),
    }
    return create_app(test_config)


class TestSchemaContract:
    """The build-time gate: collect_diagnostics() MUST return a dict
    whose keys exactly match PRIVACY_POLICY.keys(). A new field on
    either side without the other is a build-time fail."""

    def test_collect_diagnostics_payload_matches_schema(self, diag_env):
        with diag_env.app_context():
            values = diagnostics.collect_diagnostics()
        assert set(values.keys()) == schema_keys()
        # The PRIVACY_POLICY itself is the source of truth — round-trip.
        assert set(values.keys()) == set(PRIVACY_POLICY.keys())

    def test_every_field_has_some_default(self, diag_env):
        # No field returns the literal ``None`` sentinel — a None value
        # is fine (the route's redact() path renders it as "") but the
        # KEY must be present. This catches a regression where a reader
        # raised and the field was dropped from the dict.
        with diag_env.app_context():
            values = diagnostics.collect_diagnostics()
        for field in schema_keys():
            assert field in values, f"missing field {field!r}"


class TestPerRowReaders:
    def test_env_settings_drive_weather_fields(self, diag_env):
        with diag_env.app_context():
            values = diagnostics.collect_diagnostics()
        assert values["weather_location_name"] == "San Francisco"
        assert values["weather_lat"] == 37.7749
        assert values["weather_lon"] == -122.4194
        assert values["weather_location_mode"] == "specific"
        assert values["weather_ip_country"] == "US"
        assert values["weather_units"] == "imperial"
        assert values["weather_enabled"] is True

    def test_missing_files_degrade_to_none(self, diag_env):
        # Override the paths so every file-backed read points at a
        # non-existent location. Schema must still be intact.
        diag_env.config["DIAG_LAST_IP_PATH"] = "/nonexistent/ip"
        diag_env.config["DIAG_CURRENT_QUOTE_PATH"] = "/nonexistent/quote"
        diag_env.config["DIAG_IMAGES_VERSION_PATH"] = "/nonexistent/images"
        with diag_env.app_context():
            values = diagnostics.collect_diagnostics()
        assert values["lan_ip"] is None
        assert values["quote"] is None
        assert values["images_version"] is None
        # Schema gate still passes.
        assert set(values.keys()) == schema_keys()

    def test_quote_payload_round_trips(self, diag_env):
        with diag_env.app_context():
            values = diagnostics.collect_diagnostics()
        assert values["quote"] == "It was a bright cold day in April"
        assert values["author"] == "George Orwell"
        assert values["title"] == "1984"
        assert values["time"] == "13:00"

    def test_setup_marker_presence(self, diag_env, tmp_path):
        # Both markers present in the fixture → both True.
        with diag_env.app_context():
            values = diagnostics.collect_diagnostics()
        assert values["setup_complete"] is True
        assert values["handoff_complete"] is True
        assert values["gift_mode_active"] is False


class TestAnomalyDetector:
    """OV-D-I-revised + P7.1=A locked the per-section thresholds. These
    tests pin the anomaly conditions so future config drift doesn't
    silently change the page's default-open behavior."""

    def _baseline(self) -> dict:
        # A "clean" values dict where every section is healthy.
        # last_dhcp_at is anchored to "1h ago" rather than a hardcoded
        # timestamp; the original hardcoded "2026-06-07T00:00:00+00:00"
        # silently rotted past the ANOMALY_DHCP_AGE_S=24h window on
        # 2026-06-08, tripping a spurious network anomaly. Drive-by fix
        # in #419 PR1 alongside the package split.
        from datetime import UTC as _UTC
        from datetime import datetime as _datetime
        from datetime import timedelta as _timedelta

        recent_dhcp_iso = (_datetime.now(tz=_UTC) - _timedelta(hours=1)).isoformat()
        return {
            "cpu_temp_c": 50.0,
            "disk_free_pct": 50.0,
            "memory_free_mb": 200,
            "signal_dbm": -55,
            "lan_ip": "192.168.1.100",
            "last_dhcp_at": recent_dhcp_iso,
            "weather_enabled": False,
            "service_states": {
                "litclock.service": {"state": "active"},
            },
            "quote": "the dummy",
            "picked_at": time.time(),
            "setup_complete": True,
            "handoff_complete": True,
            "recent_log_entries": [],
        }

    def test_baseline_has_no_anomalies(self):
        assert diagnostics._compute_anomalies(self._baseline()) == []

    def test_cpu_temp_above_threshold(self):
        v = self._baseline()
        v["cpu_temp_c"] = 80.0
        assert "system" in diagnostics._compute_anomalies(v)

    def test_disk_below_threshold(self):
        v = self._baseline()
        v["disk_free_pct"] = 5.0
        assert "system" in diagnostics._compute_anomalies(v)

    def test_memory_below_threshold(self):
        v = self._baseline()
        v["memory_free_mb"] = 20
        assert "system" in diagnostics._compute_anomalies(v)

    def test_low_signal_triggers_network(self):
        v = self._baseline()
        v["signal_dbm"] = -90
        assert "network" in diagnostics._compute_anomalies(v)

    def test_missing_lan_ip_triggers_network(self):
        v = self._baseline()
        v["lan_ip"] = None
        assert "network" in diagnostics._compute_anomalies(v)

    def test_failed_service_triggers_services(self):
        v = self._baseline()
        v["service_states"]["litclock.service"]["state"] = "failed"
        assert "services" in diagnostics._compute_anomalies(v)

    def test_active_service_with_empty_journal_does_not_trip_services(self):
        # #433 Iron-Rule regression: the v0.214.x oxblood "Clock isn't
        # running" banner false-positive came from a slow journalctl on
        # Pi Zero 2W returning empty stdout for an active service →
        # ``has_journal_access`` flipped to False → services anomaly →
        # banner. PR3 dropped the ``has_journal_access`` field entirely
        # (per /plan-eng-review A-3 + CMT-2). Pin the new contract: an
        # active service with an empty journal_tail must NOT trip the
        # anomaly. If this test fails, the pre-#433 false-positive class
        # has been re-introduced.
        v = self._baseline()
        v["service_states"]["litclock.service"] = {
            "state": "active",
            "journal_tail": [],
        }
        assert "services" not in diagnostics._compute_anomalies(v)

    def test_firstboot_oneshot_inactive_is_not_anomaly(self):
        v = self._baseline()
        v["service_states"] = {
            "litclock-firstboot.service": {"state": "inactive"},
        }
        assert "services" not in diagnostics._compute_anomalies(v)

    def test_reresolve_oneshot_inactive_is_not_anomaly(self):
        # Regression: /review caught that litclock-reresolve-location.service
        # is Type=oneshot (post-boot inactive) but the pre-/review code only
        # excluded "firstboot" by substring. Healthy boots tripped this.
        v = self._baseline()
        v["service_states"] = {
            "litclock-reresolve-location.service": {"state": "inactive"},
        }
        assert "services" not in diagnostics._compute_anomalies(v)

    def test_litclock_service_oneshot_inactive_is_not_anomaly(self):
        # Regression: litclock.service itself is also Type=oneshot (paints
        # one quote then exits). Falsely flagged as "inactive = anomaly"
        # pre-/review (Codex MEDIUM finding).
        v = self._baseline()
        v["service_states"] = {
            "litclock.service": {"state": "inactive"},
        }
        assert "services" not in diagnostics._compute_anomalies(v)

    def test_bool_picked_at_does_not_trip_quote_anomaly(self):
        # Regression: isinstance(True, int) is True in Python; a
        # handcrafted writer emitting JSON `true` could surface as
        # picked_at=True, which time.time() - 1.0 would treat as a 56-year-
        # old quote.  _is_numeric filters booleans now. (LitClock #372.)
        v = self._baseline()
        v["picked_at"] = True
        assert "last-quote" not in diagnostics._compute_anomalies(v)

    # ---- #453: the wedged-oneshot backstop -----------------------------
    # A oneshot (litclock.service) caught mid-paint in activating/deactivating
    # is intentionally NOT a services anomaly (#443 carve-out) — it cycles
    # through that window every minute in 2-5 s. The cross-model /review on
    # #449 worried that a oneshot *genuinely wedged* in activating for minutes
    # would then read fully-green with no banner. It does not: the clock
    # publishes /run/litclock/current-quote.json ATOMICALLY and only AFTER
    # ``epd.display()`` returns (literary_clock.py:_write_status_file), so a
    # hung paint leaves the PREVIOUS quote + its numeric ``picked_at`` in
    # place, which ages past ANOMALY_QUOTE_AGE_S (90 s) and trips the
    # ``last-quote`` anomaly. These tests pin that backstop so a future change
    # to the oneshot carve-out or the quote-age gate can't silently break the
    # only wedge detector.

    def test_wedged_oneshot_surfaces_via_stale_quote_age(self):
        # The headline #453 scenario: litclock.service stuck in `activating`
        # (services stays OK via the #443 carve-out) but the last painted
        # quote is now stale → `last-quote` fires, so the wedge IS surfaced.
        v = self._baseline()
        v["service_states"] = {"litclock.service": {"state": "activating"}}
        v["picked_at"] = time.time() - (diagnostics.ANOMALY_QUOTE_AGE_S + 30)
        anomalies = diagnostics._compute_anomalies(v)
        assert "services" not in anomalies, "oneshot mid-paint must NOT trip services (#443)"
        assert "last-quote" in anomalies, "a wedged paint must surface via stale quote age (#453)"

    def test_oneshot_activating_with_fresh_quote_is_clean(self):
        # The normal 2-5 s paint window: oneshot in `activating` + a fresh
        # quote → genuinely no anomaly. Confirms the backstop above is the
        # stale-age path, not a blanket "activating == anomaly".
        v = self._baseline()
        v["service_states"] = {"litclock.service": {"state": "activating"}}
        v["picked_at"] = time.time()
        assert diagnostics._compute_anomalies(v) == []

    def test_present_quote_with_absent_picked_at_does_not_trip(self):
        # The one residual gap #453 names: a present quote whose `picked_at`
        # is absent/non-numeric can't be age-checked, so `last-quote` does
        # NOT fire. This is intentional and consistent with the #372 bool
        # filter — and unreachable from real writers, which always publish a
        # numeric `picked_at` alongside the quote. Pinned so the deliberate
        # behavior is a documented decision, not an accident.
        v = self._baseline()
        del v["picked_at"]
        assert "last-quote" not in diagnostics._compute_anomalies(v)

    def test_bool_memory_does_not_trip_system_anomaly(self):
        v = self._baseline()
        v["memory_free_mb"] = True  # would be < 50 if treated as int(1)
        assert "system" not in diagnostics._compute_anomalies(v)

    def test_bool_signal_does_not_trip_network_anomaly(self):
        v = self._baseline()
        v["signal_dbm"] = True  # would be < -75? no, 1 > -75; but pin it
        assert "network" not in diagnostics._compute_anomalies(v)

    def test_stale_dhcp_triggers_network(self):
        from datetime import UTC, datetime, timedelta

        v = self._baseline()
        v["last_dhcp_at"] = (datetime.now(tz=UTC) - timedelta(hours=25)).isoformat()
        assert "network" in diagnostics._compute_anomalies(v)

    def test_malformed_dhcp_iso_does_not_raise(self):
        v = self._baseline()
        v["last_dhcp_at"] = "not-an-iso-string"
        # Must not raise; treats as "no DHCP info, no anomaly."
        diagnostics._compute_anomalies(v)

    def test_stale_ipgeo_triggers_time_location(self):
        from datetime import UTC, datetime, timedelta

        v = self._baseline()
        v["weather_enabled"] = True
        v["weather_location_name"] = "Austin"
        v["last_ip_geo_at"] = (datetime.now(tz=UTC) - timedelta(days=8)).isoformat()
        assert "time-location" in diagnostics._compute_anomalies(v)

    def test_stale_quote_triggers_last_quote(self):
        v = self._baseline()
        v["picked_at"] = time.time() - 1000  # way past 90s threshold
        assert "last-quote" in diagnostics._compute_anomalies(v)

    def test_handoff_missing_triggers_setup_markers(self):
        v = self._baseline()
        v["handoff_complete"] = False
        assert "setup-markers" in diagnostics._compute_anomalies(v)

    def test_error_in_recent_logs_triggers_section(self):
        v = self._baseline()
        v["recent_log_entries"] = [{"level": "ERROR", "message": "oops"}]
        assert "recent-log-entries" in diagnostics._compute_anomalies(v)

    def test_weather_disabled_suppresses_time_location_anomaly(self):
        v = self._baseline()
        v["weather_enabled"] = False
        v["weather_location_name"] = None  # would otherwise trip the rule
        assert "time-location" not in diagnostics._compute_anomalies(v)


class TestBuildServiceStatesTailless:
    """#436: ``_build_service_states`` NO LONGER fetches journal tails — a cold
    ``journalctl`` is ~5-7 s on a Pi Zero 2W and it sat on the synchronous SSR
    render path, so ``/diagnostics`` blocked first paint by that much exactly
    when a unit was unhealthy. Tails now hydrate per-unit via
    ``GET /api/diagnostics/journal`` after first paint (see TestJournalEndpoint).

    This pins the new contract so a refactor can't put the fork back on the
    render/poll path:

    - ``_build_service_states`` fires ZERO journalctl forks, always.
    - every unit's ``journal_tail`` is ``[]``.
    - the ``healthy`` flag matches :func:`_is_obviously_healthy` per state — the
      SAME state->tail matrix the pre-#436 lazy-tail filter used, now surfaced
      as ``data-diag-healthy`` to gate client-side hydration instead of a
      server-side fetch.
    """

    @pytest.fixture()
    def fork_spy(self, monkeypatch):
        # Spy BOTH the single-unit fork site and the (now-uncalled) batch
        # wrapper, so any regression that reintroduces a build-time fetch —
        # via either path — trips this.
        from control_server.routes.diagnostics import _collectors

        forks: list[str] = []

        def fake_read(unit, n=3):
            forks.append(unit)
            return []

        def fake_batch(units, n=3):
            forks.extend(units)
            return {u: [] for u in units}

        monkeypatch.setattr(_collectors, "_read_journal_tail", fake_read)
        monkeypatch.setattr(_collectors, "_batched_journal_tails", fake_batch)
        return forks

    def _set_active(self, monkeypatch, mapping):
        from control_server.routes.diagnostics import _collectors

        monkeypatch.setattr(
            _collectors,
            "_batched_is_active",
            lambda units: {u: mapping.get(u, "unknown") for u in units},
        )

    def _build(self, diag_env):
        from control_server.routes.diagnostics import _collectors

        with diag_env.app_context():
            return _collectors._build_service_states()

    def test_build_never_forks_and_tails_empty(self, fork_spy, monkeypatch, diag_env):
        # Even with a failed unit present (the case that USED to fetch a tail),
        # build forks nothing and every journal_tail is empty.
        self._set_active(
            monkeypatch,
            {
                "litclock.service": "active",
                "litclock-control.service": "failed",
                "litclock-firstboot.service": "inactive",
                "litclock-update.timer": "active",
                "litclock-reresolve-location.service": "inactive",
            },
        )
        states = self._build(diag_env)
        assert fork_spy == [], f"build must not fork journalctl, forked {fork_spy!r}"
        assert all(s["journal_tail"] == [] for s in states.values())

    @pytest.mark.parametrize(
        ("unit", "state", "healthy"),
        [
            # Obviously-healthy: active services + oneshot units at their
            # by-design inactive resting state → NOT a hydration target.
            ("litclock-control.service", "active", True),
            ("litclock.service", "inactive", True),  # oneshot resting
            ("litclock-firstboot.service", "inactive", True),  # oneshot resting
            ("litclock-reresolve-location.service", "inactive", True),  # oneshot resting
            # Not-healthy → the client hydrates a tail for these rows.
            ("litclock-control.service", "failed", False),
            ("litclock-update.timer", "inactive", False),  # non-oneshot inactive
            ("litclock-control.service", "activating", False),  # transient bad
            ("litclock-control.service", "deactivating", False),  # transient bad
            ("litclock-control.service", "unknown", False),  # is-active couldn't read
            ("litclock.service", "failed", False),  # oneshot but FAILED (carve-out is inactive-only)
        ],
    )
    def test_healthy_flag_matches_predicate(self, fork_spy, monkeypatch, diag_env, unit, state, healthy):
        self._set_active(monkeypatch, {unit: state})
        states = self._build(diag_env)
        assert states[unit]["healthy"] is healthy
        assert fork_spy == [], "reading the healthy flag must never fork journalctl"


class TestServiceStateModifier:
    """#449/#463 — the per-row chip COLOR is driven by ``state_modifier`` (the
    chip TEXT stays the literal systemd state). A oneshot caught mid-paint in
    ``activating``/``deactivating`` emits the neutral ``transient-ok`` tone so
    the row matches the OK section pill (#443 fixed the verdict but left the
    row ochre); a oneshot settled at its by-design ``inactive`` resting state
    emits the green ``settled-ok`` tone so it reads healthy, not idle-broken
    (#463). Every other unit/state keeps its literal modifier so the existing
    CSS (green ``--active``, ochre ``--failed``/``--activating``/
    ``--deactivating``) still applies.
    """

    @pytest.fixture()
    def no_tails(self, monkeypatch):
        from control_server.routes.diagnostics import _collectors

        monkeypatch.setattr(_collectors, "_batched_journal_tails", lambda units, n=3: {u: [] for u in units})

    def _modifiers(self, monkeypatch, diag_env, mapping):
        from control_server.routes.diagnostics import _collectors

        monkeypatch.setattr(
            _collectors,
            "_batched_is_active",
            lambda units: {u: mapping.get(u, "unknown") for u in units},
        )
        with diag_env.app_context():
            states = _collectors._build_service_states()
        return {u: info["state_modifier"] for u, info in states.items()}

    def test_oneshot_transient_emits_neutral_tone(self, no_tails, monkeypatch, diag_env):
        mods = self._modifiers(
            monkeypatch,
            diag_env,
            {
                "litclock.service": "activating",  # oneshot mid-paint
                "litclock-reresolve-location.service": "deactivating",  # oneshot mid-paint
                "litclock-control.service": "active",
            },
        )
        # Text stays literal; color tone is neutral so the chip doesn't read ochre.
        assert mods["litclock.service"] == "transient-ok"
        assert mods["litclock-reresolve-location.service"] == "transient-ok"
        assert mods["litclock-control.service"] == "active"

    def test_non_oneshot_transient_keeps_ochre_modifier(self, no_tails, monkeypatch, diag_env):
        # litclock-control.service is NOT a oneshot: a real activating/
        # deactivating IS a services anomaly, so the ochre chip is correct.
        mods = self._modifiers(
            monkeypatch,
            diag_env,
            {"litclock-control.service": "activating", "litclock.service": "active"},
        )
        assert mods["litclock-control.service"] == "activating"

    def test_failed_oneshot_keeps_failed_modifier(self, no_tails, monkeypatch, diag_env):
        # A failed oneshot is a real failure — must stay ochre, not transient-ok.
        mods = self._modifiers(monkeypatch, diag_env, {"litclock.service": "failed"})
        assert mods["litclock.service"] == "failed"

    def test_oneshot_inactive_emits_settled_ok(self, no_tails, monkeypatch, diag_env):
        # #463 — a oneshot settled at its by-design ``inactive`` resting state
        # is healthy, not idle-broken. It emits ``settled-ok`` (green --success
        # tone) so a non-tech recipient reads "fine", not a fault. Every
        # DIAG_ONESHOT_UNITS member is covered, incl. firstboot (permanently
        # inactive on a provisioned clock — the sharpest case).
        mods = self._modifiers(
            monkeypatch,
            diag_env,
            {
                "litclock.service": "inactive",
                "litclock-firstboot.service": "inactive",
                "litclock-reresolve-location.service": "inactive",
            },
        )
        assert mods["litclock.service"] == "settled-ok"
        assert mods["litclock-firstboot.service"] == "settled-ok"
        assert mods["litclock-reresolve-location.service"] == "settled-ok"

    def test_non_oneshot_inactive_keeps_literal_modifier(self, no_tails, monkeypatch, diag_env):
        # #463 guard — settled-ok is ONESHOT-only. A long-running unit going
        # ``inactive`` IS a real services anomaly, so it must keep its literal
        # modifier (never green): litclock-control.service is not a oneshot.
        mods = self._modifiers(monkeypatch, diag_env, {"litclock-control.service": "inactive"})
        assert mods["litclock-control.service"] == "inactive"


class TestApiDiagnostics:
    def test_returns_documented_shape(self, diag_env):
        with diag_env.test_client() as c:
            r = c.get("/api/diagnostics")
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        assert "values" in body
        assert isinstance(body["values"], dict)
        assert set(body["values"].keys()) == schema_keys()
        assert "anomalies" in body and isinstance(body["anomalies"], list)
        assert "copy_payload" in body and isinstance(body["copy_payload"], str)
        assert "section_order" in body and body["section_order"] == list(diagnostics.SECTION_IDS)
        # #449 — pin the per-row service_states contract at the envelope
        # boundary: every row carries state + state_modifier (drives the chip
        # color) + journal_tail. A serializer refactor that dropped
        # state_modifier would otherwise only surface as an untinted chip on
        # hardware, never in tests.
        services = body["values"]["service_states"]
        assert isinstance(services, dict) and services
        for unit, info in services.items():
            assert {"state", "state_modifier", "journal_tail"} <= info.keys(), f"row {unit} missing keys: {info}"

    def test_copy_payload_is_markdown_fenced(self, diag_env):
        with diag_env.test_client() as c:
            r = c.get("/api/diagnostics")
        body = r.get_json()
        assert body["copy_payload"].startswith("```markdown")
        assert body["copy_payload"].endswith("```")
        assert "# LitClock diagnostics" in body["copy_payload"]

    def test_diagnostics_page_renders_template(self, diag_env):
        # PR3a: real templated page (replaces PR2 placeholder).
        with diag_env.test_client() as c:
            r = c.get("/diagnostics")
        assert r.status_code == 200
        assert r.headers.get("Content-Type", "").startswith("text/html")
        body = r.data
        # Status banner is the page's visual anchor (D29).
        assert b"status-banner" in body
        # All 8 sections render with the data-diag-section hook.
        for section_id in (
            b"build-version",
            b"system",
            b"network",
            b"time-location",
            b"services",
            b"last-quote",
            b"setup-markers",
            b"recent-log-entries",
        ):
            assert b'data-diag-section="' + section_id + b'"' in body, f"missing section {section_id!r}"
        # Reveal pill renders with aria-pressed=false by default (sessionStorage off).
        assert b"data-diag-reveal" in body
        assert b'aria-pressed="false"' in body
        # Copy support payload card renders with the markdown fence content.
        assert b"data-diag-copy-block" in body
        assert b"```markdown" in body
        # Per-tab CSS slot wires up.
        assert b"css/diagnostics.css" in body
        # Per-tab JS slot wires up.
        assert b"js/diagnostics.js" in body
        # Polite live region for the announcer.
        assert b"data-diag-announcer" in body

    def test_diagnostics_page_renders_anomaly_state(self, diag_env, monkeypatch):
        # When any section is anomalous, the banner swaps to warning state
        # AND the matching <details> renders with the `open` attribute.
        # Forge a high CPU temp so the system section trips.
        # Patch the actual binding site in _collectors.py (#419 D8) — the
        # package namespace re-exports the name, but collect_diagnostics
        # looks it up inside _collectors.py at call time.
        monkeypatch.setattr(
            "control_server.routes.diagnostics._collectors._read_cpu_temp_c",
            lambda: 99.9,
        )
        with diag_env.test_client() as c:
            r = c.get("/diagnostics")
        body = r.data
        assert b"status-banner--warning" in body
        # The system section is open-by-default on this paint.
        assert b'data-diag-section="system"' in body
        # The open attribute lands on the system <details> specifically.
        # /review testing specialist: parse with html.parser to avoid
        # whitespace coupling — a future Jinja reformat would otherwise
        # flip this test red without behavior changing.
        from html.parser import HTMLParser

        class _SectionOpenFinder(HTMLParser):
            def __init__(self):
                super().__init__()
                self.system_open = False

            def handle_starttag(self, tag, attrs):
                if tag != "details":
                    return
                attr_dict = dict(attrs)
                if attr_dict.get("data-diag-section") == "system" and "open" in attr_dict:
                    self.system_open = True

        finder = _SectionOpenFinder()
        finder.feed(body.decode("utf-8"))
        assert finder.system_open, "expected <details data-diag-section='system' open>"

    def test_diagnostics_page_oneshot_transient_chip_is_neutral(self, diag_env, monkeypatch):
        # #449 — the SSR template path (distinct fallback chain from the JS
        # patch path) must also neutralize a oneshot mid-paint chip. Forge
        # litclock.service into the activating window and assert the rendered
        # HTML carries the transient-ok modifier, NOT the ochre --activating.
        monkeypatch.setattr(
            "control_server.routes.diagnostics._collectors._batched_is_active",
            lambda units: {u: ("activating" if u == "litclock.service" else "active") for u in units},
        )
        with diag_env.test_client() as c:
            r = c.get("/diagnostics")
        body = r.data
        # The literal state still renders as the chip text...
        assert b"diag-service__state--transient-ok" in body
        # ...but the ochre activating modifier must NOT be applied to the chip.
        assert b"diag-service__state--activating" not in body
        # Section pill reads OK (the #443 carve-out), not a services alert.
        assert b'data-diag-section="services"' in body

    def test_diagnostics_page_oneshot_inactive_chip_is_green(self, diag_env, monkeypatch):
        # #463 — the SSR template path must paint a settled oneshot ``inactive``
        # with the healthy green ``settled-ok`` tone, not the neutral base chip.
        # Forge firstboot (the permanently-inactive gift-recipient case) inactive.
        monkeypatch.setattr(
            "control_server.routes.diagnostics._collectors._batched_is_active",
            lambda units: {u: ("inactive" if u == "litclock-firstboot.service" else "active") for u in units},
        )
        with diag_env.test_client() as c:
            r = c.get("/diagnostics")
        body = r.data
        assert b"diag-service__state--settled-ok" in body
        # Section pill reads OK — a settled oneshot is not a services alert.
        assert b'data-diag-section="services"' in body

    def test_diagnostics_page_reveal_query_unredacts(self, diag_env):
        # ?reveal=location flips the values dict in the same template.
        with diag_env.test_client() as c:
            r = c.get("/diagnostics?reveal=location")
        body = r.data.decode("utf-8")
        # Without reveal the city is "San Francisco" in env.sh but the
        # response should expose it WITH the query.
        assert "San Francisco" in body

    def test_diagnostics_page_no_store_cache_control(self, diag_env):
        # /review fix F-SW-CACHE: the page can render unredacted SSID +
        # city + 6dp coords when ?reveal=location. Cache-Control: no-store
        # prevents the PWA service worker (and any LAN intermediary) from
        # persisting that rendering past the sessionStorage-scoped Reveal
        # window. Tested both the plain and the revealed paths.
        with diag_env.test_client() as c:
            r1 = c.get("/diagnostics")
            r2 = c.get("/diagnostics?reveal=location")
        for r in (r1, r2):
            cc = r.headers.get("Cache-Control", "")
            assert "no-store" in cc, f"expected no-store in Cache-Control, got {cc!r}"

    def test_diagnostics_page_html_escapes_user_values(self, diag_env, monkeypatch):
        # Jinja default autoescape is on for .html.j2 templates. Verify
        # an env value containing HTML payload renders escaped — defense
        # in depth alongside the JS textContent posture.
        from control_server.routes import diagnostics as diag_mod

        def fake_collect():
            return {k: None for k in diag_mod.schema_keys()} | {
                "weather_location_name": "<script>alert('xss')</script>",
                "service_states": {},
                "recent_log_entries": [],
                "weather_enabled": True,
            }

        # Patch the route's binding (#419 D8). The package re-exports
        # collect_diagnostics for plain imports, but the route in _sse.py
        # bound the name at import time and looks it up there at call time.
        monkeypatch.setattr(
            "control_server.routes.diagnostics._sse.collect_diagnostics",
            fake_collect,
        )
        with diag_env.test_client() as c:
            r = c.get("/diagnostics?reveal=location")
        body = r.data.decode("utf-8")
        assert "<script>alert(" not in body
        # The escaped form lands somewhere in the response.
        assert "&lt;script&gt;" in body or "&#x3c;script&#x3e;" in body

    def test_format_log_ts_filter_handles_bad_input(self, diag_env):
        # F-FILTER-COVERAGE: the @bp.app_template_filter is registered
        # globally; its three exception branches should each return ''
        # without raising. Exercise via the registered Jinja env.
        f = diag_env.jinja_env.filters["format_log_ts"]
        assert f(None) == ""
        assert f("garbage") == ""
        assert f([]) == ""
        # Future-overflow + negative; both should return '' not raise.
        assert f(10**30) == ""
        # Real timestamp formats correctly.
        out = f(0)
        assert ":" in out  # HH:MM:SS shape

    def test_schema_drift_logs_warning(self, diag_env, monkeypatch, caplog):
        # _check_schema_match is wired into page_diagnostics. A drift
        # surfaces via a journald warning. /review test-coverage gap.

        def drift_collect():
            return {"unknown_extra_key": 42}  # missing every real key

        # Patch the route binding (#419 D8) — see comment above for why.
        monkeypatch.setattr(
            "control_server.routes.diagnostics._sse.collect_diagnostics",
            drift_collect,
        )
        # Bring root logger down so the warning lands in caplog.
        import logging as _logging

        prior = _logging.getLogger().level
        _logging.getLogger().setLevel(_logging.WARNING)
        try:
            with caplog.at_level(_logging.WARNING), diag_env.test_client() as c:
                c.get("/diagnostics")
        finally:
            _logging.getLogger().setLevel(prior)
        assert any("DIAGNOSTICS_SCHEMA_DRIFT" in r.message for r in caplog.records)

    def test_api_diagnostics_500_envelope_when_collect_raises(self, diag_env, monkeypatch):
        # The /api/diagnostics route wraps collect_diagnostics in a final-
        # bailout try/except that returns the project's standard envelope
        # shape on failure. #419 T11 — pre-#419 the success path was
        # well-covered but the 500 envelope path was untested. A
        # regression that flipped the error to a plain Flask 500 (HTML
        # instead of JSON) would have shipped silently.

        def boom():
            raise RuntimeError("simulated collector crash")

        # Patch the route's actual binding site (#419 D8).
        monkeypatch.setattr(
            "control_server.routes.diagnostics._sse.collect_diagnostics",
            boom,
        )
        with diag_env.test_client() as c:
            r = c.get("/api/diagnostics")
        assert r.status_code == 500
        body = r.get_json()
        assert body is not None, "500 must still be JSON, not HTML"
        assert body.get("ok") is False
        assert body.get("error", {}).get("code") == "diagnostics_unavailable"
        assert "Diagnostics unavailable" in body.get("error", {}).get("message", "")

    def test_values_dict_redacts_ssid_and_city_by_default(self, diag_env):
        # F-LEAK-A regression: pre-/review the route jsonified the RAW
        # values dict. Any LAN client could GET /api/diagnostics and see
        # the exact SSID + city + 6dp lat/lon, walking around the entire
        # PRIVACY_POLICY contract. Codex's headline finding.
        with diag_env.app_context():
            values = diagnostics.collect_diagnostics()
        # diag_env populates env.sh with WEATHER_LOCATION_NAME=San Francisco
        # and WEATHER_LATITUDE=37.7749 — make sure they don't ship raw.
        assert values["weather_location_name"] == "San Francisco"  # collect IS raw
        with diag_env.test_client() as c:
            r = c.get("/api/diagnostics")
        body = r.get_json()
        envelope_values = body["values"]
        # The wire envelope MUST scrub them.
        assert envelope_values["weather_location_name"] == REDACTED_VALUE
        # 6dp coords go to 2dp on the wire.
        assert envelope_values["weather_lat"] == "37.77"
        assert envelope_values["weather_lon"] is not None
        assert "37.7749" not in str(envelope_values["weather_lat"])
        # revealed_groups echoes the reveal state.
        assert body["revealed_groups"] == []

    def test_reveal_location_query_unredacts(self, diag_env):
        with diag_env.test_client() as c:
            r = c.get("/api/diagnostics?reveal=location")
        body = r.get_json()
        envelope_values = body["values"]
        # Reveal toggle un-redacts the location group.
        assert envelope_values["weather_location_name"] == "San Francisco"
        # The rendered coord is the full-precision form per the policy.
        assert "37.7749" in str(envelope_values["weather_lat"])
        assert body["revealed_groups"] == ["location"]

    def test_unknown_reveal_group_is_ignored(self, diag_env):
        # Future client-server skew shouldn't 400 the surface.
        with diag_env.test_client() as c:
            r = c.get("/api/diagnostics?reveal=foo")
        assert r.status_code == 200
        body = r.get_json()
        assert body["revealed_groups"] == []
        assert body["values"]["weather_location_name"] == REDACTED_VALUE

    def test_safe_clear_fields_preserve_native_type(self, diag_env):
        # The redaction pass must NOT stringify dicts / bools / ints for
        # safe-clear fields. PR3 UI keys off the native types.
        with diag_env.test_client() as c:
            r = c.get("/api/diagnostics")
        envelope_values = r.get_json()["values"]
        # service_states is a nested dict.
        assert isinstance(envelope_values["service_states"], dict)
        # weather_enabled is a bool.
        assert isinstance(envelope_values["weather_enabled"], bool)
        # recent_log_entries is a list.
        assert isinstance(envelope_values["recent_log_entries"], list)

    def test_journal_tail_redacted_by_endpoint(self, diag_env, monkeypatch):
        # F-LEAK-B regression, moved to the per-unit endpoint (#436): tails no
        # longer flow through /api/diagnostics (build returns empty). The
        # per-unit /api/diagnostics/journal endpoint is now the ingest point
        # that must run redact_text() on every line. openweathermap.py logs
        # &appid=$KEY to journalctl; PSK/PSK-shaped secrets also appear.
        #
        # NOTE: redact_text scrubs PSK-shaped secrets + GH tokens + long tokens,
        # but by design does NOT scrub SSID (see _redaction.py) — the PWA is
        # unauthenticated on the LAN and a same-LAN client already knows the SSID
        # it's on. So no ssid= line here (a prior version had one, which
        # misleadingly implied SSID redaction that doesn't happen).
        raw_lines = [
            "2026-06-07 PSK=hunter2foobarbaz",
            "GET ...appid=ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
        ]
        # Patch the raw reader the endpoint calls (redaction happens AFTER, in
        # the route). Bind on _sse — that's where the route looks the name up.
        monkeypatch.setattr(
            "control_server.routes.diagnostics._sse._read_journal_tail",
            lambda unit, n=3: list(raw_lines),
        )
        with diag_env.test_client() as c:
            r = c.get("/api/diagnostics/journal?unit=litclock.service")
        assert r.status_code == 200
        body_text = r.data.decode("utf-8")
        # Raw secret-shaped strings must NOT appear anywhere in the response...
        assert "hunter2foobarbaz" not in body_text
        assert "ghp_AbCdEfGhIjKlMnOpQr" not in body_text
        # ...and the redaction token MUST be present, so a "no secrets" pass
        # can't be satisfied by the endpoint just returning nothing (testing OV).
        assert "***REDACTED***" in body_text


class TestJournalEndpoint:
    """#436 — ``GET /api/diagnostics/journal?unit=`` is the per-unit tail
    hydration source, split off the SSR/poll path so a cold journalctl never
    blocks first paint. SECURITY: ``unit`` feeds ``journalctl -u``, so it MUST
    be a member of the DIAG_UNITS allowlist or we'd expose arbitrary unit logs.
    """

    def test_unknown_unit_rejected_without_fork(self, diag_env, monkeypatch):
        from control_server.routes.diagnostics import _sse

        calls: list[str] = []
        monkeypatch.setattr(_sse, "_read_journal_tail", lambda unit, n=3: calls.append(unit) or [])
        with diag_env.test_client() as c:
            r = c.get("/api/diagnostics/journal?unit=sshd.service")
        assert r.status_code == 400
        body = r.get_json()
        assert body["ok"] is False
        assert body["error"]["code"] == "invalid_unit"
        assert calls == [], "must NOT fork journalctl for a non-allowlisted unit"

    def test_missing_unit_rejected(self, diag_env):
        with diag_env.test_client() as c:
            r = c.get("/api/diagnostics/journal")
        assert r.status_code == 400
        assert r.get_json()["error"]["code"] == "invalid_unit"

    def test_valid_unit_returns_tail(self, diag_env, monkeypatch):
        from control_server.routes.diagnostics import _sse

        monkeypatch.setattr(_sse, "_read_journal_tail", lambda unit, n=3: ["line a", "line b"])
        with diag_env.test_client() as c:
            r = c.get("/api/diagnostics/journal?unit=litclock.service")
        assert r.status_code == 200
        assert r.get_json() == {
            "ok": True,
            "unit": "litclock.service",
            "journal_tail": ["line a", "line b"],
            "lines": 3,
        }
        assert "no-store" in r.headers.get("Cache-Control", "")

    def test_endpoint_error_returns_envelope(self, diag_env, monkeypatch):
        from control_server.routes.diagnostics import _sse

        def boom(unit, n=3):
            raise RuntimeError("journalctl exploded")

        monkeypatch.setattr(_sse, "_read_journal_tail", boom)
        with diag_env.test_client() as c:
            r = c.get("/api/diagnostics/journal?unit=litclock.service")
        assert r.status_code == 500
        assert r.get_json()["error"]["code"] == "journal_unavailable"


class TestVerdictIndependentOfTails:
    """#436 CRITICAL REGRESSION GUARD: dropping journal tails off the SSR/poll
    path is only SAFE because the section/anomaly verdict never reads them
    (``_compute_anomalies`` looks at ``state`` only). If a future change makes
    the verdict depend on ``journal_tail``, this trips — and #436's whole basis
    (SSR paints the same verdict without the tails) would be broken.
    """

    def test_section_states_identical_with_and_without_tails(self, diag_env):
        import copy

        from control_server.routes.diagnostics import _sse

        with diag_env.app_context():
            values = _sse.collect_diagnostics()
            baseline = _sse._compute_section_states(values)
            tainted = copy.deepcopy(values)
            for info in tainted["service_states"].values():
                info["journal_tail"] = ["FATAL: everything is on fire", "panic panic panic"]
            after = _sse._compute_section_states(tainted)
        assert after == baseline, "section verdict must NOT depend on journal_tail"


class TestCopyPayloadRedaction:
    """build_copy_payload must default-redact PII fields and respect the
    reveal-group when invoked with revealed_groups."""

    def test_ssid_redacted_by_default(self, diag_env):
        # Inject a non-None ssid into the values; without the policy this
        # would land in the copy block verbatim.
        with diag_env.app_context():
            values = diagnostics.collect_diagnostics()
        values["ssid"] = "MyHomeWiFi"
        payload = diagnostics.build_copy_payload(values)
        assert "MyHomeWiFi" not in payload
        assert REDACTED_VALUE in payload

    def test_city_redacted_by_default(self, diag_env):
        with diag_env.app_context():
            values = diagnostics.collect_diagnostics()
        values["weather_location_name"] = "1234 Main St, San Francisco"
        payload = diagnostics.build_copy_payload(values)
        assert "1234 Main St" not in payload
        assert REDACTED_VALUE in payload

    def test_coords_rounded_to_2dp_by_default(self, diag_env):
        with diag_env.app_context():
            values = diagnostics.collect_diagnostics()
        values["weather_lat"] = 37.774929
        values["weather_lon"] = -122.419418
        payload = diagnostics.build_copy_payload(values)
        # 6-decimal precision should be gone.
        assert "37.774929" not in payload
        assert "-122.419418" not in payload
        # 2dp form is present.
        assert "37.77" in payload
        assert "-122.42" in payload

    def test_reveal_group_unredacts(self, diag_env):
        with diag_env.app_context():
            values = diagnostics.collect_diagnostics()
        values["ssid"] = "MyWiFi"
        values["weather_location_name"] = "San Francisco"
        revealed = frozenset({"location"})
        payload = diagnostics.build_copy_payload(values, revealed_groups=revealed)
        assert "MyWiFi" in payload
        assert "San Francisco" in payload

    # --- Edge-case payload shapes (#419 T9) -------------------------------
    # The pre-#419 tests covered the happy-path shape. These pin behavior
    # against the four edge cases the issue body called out: empty machine,
    # multi-service journal_tail, non-dict entries in recent_log_entries,
    # and string timestamps in log entries.

    def test_empty_machine_renders_safely(self, diag_env):
        """A diagnostics dict with no quote, no services, no logs should
        still produce a valid markdown-fenced payload — no IndexError or
        KeyError on empty collections."""
        with diag_env.app_context():
            values = diagnostics.collect_diagnostics()
        # Strip out the optional-content fields.
        values["service_states"] = {}
        values["recent_log_entries"] = []
        values["quote"] = None
        values["author"] = None
        values["title"] = None
        values["time"] = None
        payload = diagnostics.build_copy_payload(values)
        assert payload.startswith("```markdown")
        assert payload.endswith("```")
        # Optional sections do NOT appear when their content is empty.
        assert "## Services" not in payload
        assert "## Last quote" not in payload
        assert "## Recent log entries" not in payload

    def test_multiple_services_render_journal_tail_indent(self, diag_env):
        """Each service block lists its state + journal_tail with 4-space
        indent so the markdown renders as a nested code block."""
        with diag_env.app_context():
            values = diagnostics.collect_diagnostics()
        values["service_states"] = {
            "alpha.service": {
                "state": "active",
                "journal_tail": ["alpha log line 1", "alpha log line 2"],
            },
            "beta.service": {
                "state": "failed",
                "journal_tail": ["beta error"],
            },
        }
        payload = diagnostics.build_copy_payload(values)
        # Each unit appears once with its state.
        assert "**alpha.service:** active" in payload
        assert "**beta.service:** failed" in payload
        # Each journal_tail line gets a 4-space indent for readability.
        assert "    alpha log line 1" in payload
        assert "    beta error" in payload

    def test_non_dict_entry_in_recent_log_entries_is_skipped(self, diag_env):
        """A list with mixed shapes (e.g. partial deserialization) must
        not raise — the loop skips non-dict elements silently."""
        with diag_env.app_context():
            values = diagnostics.collect_diagnostics()
        values["recent_log_entries"] = [
            {"timestamp": 1700000000.0, "level": "INFO", "message": "real entry"},
            "string-not-dict",  # skipped silently
            None,  # skipped silently
            42,  # skipped silently
            {"timestamp": 1700000001.0, "level": "ERROR", "message": "second real entry"},
        ]
        payload = diagnostics.build_copy_payload(values)
        # The two real entries render.
        assert "real entry" in payload
        assert "second real entry" in payload
        # Garbage entries do NOT appear.
        assert "string-not-dict" not in payload

    def test_string_timestamp_in_log_entry_renders_empty_ts(self, diag_env):
        """The renderer only formats timestamps when they're numeric.
        A string timestamp (legacy logs, manual edit) renders ts as ``""``
        rather than raising on isinstance check."""
        with diag_env.app_context():
            values = diagnostics.collect_diagnostics()
        values["recent_log_entries"] = [
            {
                "timestamp": "2026-06-08T10:00:00",  # string, not numeric
                "level": "WARNING",
                "message": "legacy ts shape",
            },
        ]
        payload = diagnostics.build_copy_payload(values)
        # The entry renders without an HH:MM:SS prefix.
        assert "**WARNING** legacy ts shape" in payload


class TestNoSecretsLeak:
    """The keystone secret-leak gate — no value the privacy policy
    redacts should appear in the rendered copy payload OR the
    /api/diagnostics JSON values when reveal is off.

    Pairs with tests/test_diagnostics_no_secrets.py (PR2): that file
    runs the denylist regex against the full HTTP response body to catch
    secret-shaped strings the keys-allowlist might miss."""

    def test_redacted_fields_render_marker_in_copy_block(self, diag_env):
        # Synthesize a payload with values in every redaction class.
        values = {field: "REDACT_ME" for field in schema_keys()}
        values["weather_lat"] = 12.3456
        values["weather_lon"] = -89.0123
        values["service_states"] = {}
        values["recent_log_entries"] = []
        values["weather_enabled"] = True
        payload = diagnostics.build_copy_payload(values)
        # The user-PII fields (SSID + city) are policy=redacted; their
        # rows in the copy block must show the REDACTED_VALUE marker, not
        # the REDACT_ME sentinel we injected.
        for label in ("**SSID:**", "**City:**"):
            row = next(
                (ln for ln in payload.splitlines() if label in ln),
                None,
            )
            assert row is not None, f"missing row for {label}"
            assert "REDACT_ME" not in row, f"{label} row leaked sentinel: {row!r}"
            assert REDACTED_VALUE in row, f"{label} row missing redaction marker: {row!r}"
        # End-to-end: the copy payload at minimum doesn't contain the
        # exact 6dp coord we injected (the rounded() policy normalizes
        # them to 2dp).
        assert "12.3456" not in payload
        assert "-89.0123" not in payload


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])


class TestJournalEndpointDeepLines:
    """#416 follow-up — the ?lines= param lets a helper pull a DEEPER tail than
    the 3-line page preview, capped, with a distinct (non-poisoning) cache key."""

    def test_custom_lines_reads_deep_key_and_slices(self, diag_env, monkeypatch):
        from control_server.routes.diagnostics import _collectors, _sse

        seen = {}

        def fake(unit, n=3, cache_key=None):
            seen["n"] = n
            seen["cache_key"] = cache_key
            return [f"line-{i}" for i in range(n)]

        monkeypatch.setattr(_sse, "_read_journal_tail", fake)
        with diag_env.test_client() as c:
            r = c.get("/api/diagnostics/journal?unit=litclock.service&lines=25")
        assert r.status_code == 200
        body = r.get_json()
        assert body["lines"] == 25
        # ONE deep key per unit (reads up to MAX, then slices to 25) so a LAN peer
        # can't rotate ?lines=4..200 into distinct cold cache misses (DoS, /review).
        assert seen["n"] == _collectors.DIAG_JOURNAL_LINES_MAX
        assert seen["cache_key"] == "diag-journal-litclock.service-deep"
        assert len(body["journal_tail"]) == 25  # sliced to the requested depth

    def test_lines_capped_at_max(self, diag_env, monkeypatch):
        from control_server.routes.diagnostics import _collectors, _sse

        seen = {}
        monkeypatch.setattr(_sse, "_read_journal_tail", lambda unit, n=3, cache_key=None: seen.update(n=n) or [])
        with diag_env.test_client() as c:
            r = c.get("/api/diagnostics/journal?unit=litclock.service&lines=99999")
        assert r.get_json()["lines"] == _collectors.DIAG_JOURNAL_LINES_MAX
        assert seen["n"] == _collectors.DIAG_JOURNAL_LINES_MAX

    def test_non_numeric_lines_falls_back_to_default(self, diag_env, monkeypatch):
        from control_server.routes.diagnostics import _sse

        monkeypatch.setattr(_sse, "_read_journal_tail", lambda unit, n=3, cache_key=None: [])
        with diag_env.test_client() as c:
            r = c.get("/api/diagnostics/journal?unit=litclock.service&lines=notanumber")
        assert r.status_code == 200
        assert r.get_json()["lines"] == 3  # DIAG_JOURNAL_LINES_PER_UNIT

    def test_default_lines_uses_shared_page_cache_key(self, diag_env, monkeypatch):
        # lines omitted → default path must NOT pass a deep cache_key (shares the
        # warm page-preview cache).
        from control_server.routes.diagnostics import _sse

        seen = {}
        monkeypatch.setattr(
            _sse, "_read_journal_tail", lambda unit, n=3, cache_key=None: seen.update(cache_key=cache_key) or []
        )
        with diag_env.test_client() as c:
            c.get("/api/diagnostics/journal?unit=litclock.service")
        assert seen["cache_key"] is None


class TestSupportLogsEndpoint:
    """#416 follow-up — GET /api/diagnostics/support-logs: one downloadable text
    bundle (system state + deep per-unit logs) for shell-less support."""

    def test_returns_text_attachment_with_deep_logs(self, diag_env, monkeypatch):
        from control_server.routes.diagnostics import _sse

        monkeypatch.setattr(
            _sse, "_read_journal_tail", lambda unit, n=50, cache_key=None: [f"{unit} deep-line-{i}" for i in range(n)]
        )
        with diag_env.test_client() as c:
            r = c.get("/api/diagnostics/support-logs")
        assert r.status_code == 200
        assert r.mimetype == "text/plain"
        cd = r.headers.get("Content-Disposition", "")
        assert "attachment" in cd and "litclock-support-logs-" in cd and cd.endswith('.txt"')
        assert "no-store" in r.headers.get("Cache-Control", "")
        text = r.get_data(as_text=True)
        # System payload (copy block) + a deep per-unit section (sliced to the
        # last DIAG_SUPPORT_JOURNAL_LINES of the deep read).
        assert "## Logs (deep tail per unit)" in text
        assert "### litclock.service" in text
        assert "litclock.service deep-line-" in text

    def test_deep_read_uses_single_deep_cache_key(self, diag_env, monkeypatch):
        from control_server.routes.diagnostics import _sse

        keys = []
        monkeypatch.setattr(
            _sse,
            "_read_journal_tail",
            lambda unit, n=3, cache_key=None: keys.append(cache_key) or [],
        )
        with diag_env.test_client() as c:
            c.get("/api/diagnostics/support-logs")
        assert keys, "should have read at least one unit"
        # One deep key per unit — shared with the ?lines= path, no per-N fan-out.
        assert all(k and k.endswith("-deep") for k in keys), keys

    def test_redaction_applied_to_tail_lines(self, diag_env, monkeypatch):
        from control_server.routes.diagnostics import _sse

        # A raw line carrying an SSID-shaped secret must be redacted in the bundle.
        monkeypatch.setattr(
            _sse, "_read_journal_tail", lambda unit, n=50, cache_key=None: ["OPENWEATHERMAP_APIKEY=deadbeefsecret123"]
        )
        with diag_env.test_client() as c:
            r = c.get("/api/diagnostics/support-logs")
        text = r.get_data(as_text=True)
        assert "deadbeefsecret123" not in text, "secrets in journal lines must be redacted"

    def test_error_path_returns_plain_500(self, diag_env, monkeypatch):
        from control_server.routes.diagnostics import _sse

        monkeypatch.setattr(_sse, "collect_diagnostics", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        with diag_env.test_client() as c:
            r = c.get("/api/diagnostics/support-logs")
        assert r.status_code == 500
        assert r.mimetype == "text/plain"

    def test_page_has_download_link(self, diag_env):
        with diag_env.test_client() as c:
            r = c.get("/diagnostics")
        body = r.data.decode("utf-8")
        assert "/api/diagnostics/support-logs" in body
        assert "Download full logs" in body


class TestSupportLogsBundleAssembler:
    """Pure assembler — budget truncation names skipped units (no silent cap)."""

    def test_assembles_system_plus_per_unit(self):
        from control_server.routes.diagnostics._copy_payload import build_support_logs_bundle

        out = build_support_logs_bundle("SYSPAYLOAD", ("u1", "u2"), lambda u: [f"{u}-l"], budget_s=100)
        assert out.startswith("SYSPAYLOAD")
        assert "### u1" in out and "u1-l" in out and "### u2" in out

    def test_empty_tail_renders_placeholder(self):
        from control_server.routes.diagnostics._copy_payload import build_support_logs_bundle

        out = build_support_logs_bundle("SYS", ("u1",), lambda u: [], budget_s=100)
        assert "(no journal entries)" in out

    def test_budget_truncation_names_skipped_units(self):
        import itertools

        from control_server.routes.diagnostics._copy_payload import build_support_logs_bundle

        # clock jumps 10s per call → budget 0 trips immediately, all skipped.
        out = build_support_logs_bundle(
            "SYS", ("a", "b", "c"), lambda u: ["x"], budget_s=0, clock=itertools.count(0, 10).__next__
        )
        assert "truncated" in out
        assert "a, b, c" in out
        assert "x" not in out  # nothing was read


class TestParseLinesParam:
    """#416 fu — the clamp is DoS-critical: `journalctl -n 0` means 'all lines'
    (unbounded), so lines=0 MUST clamp to 1, and the max must hold exactly."""

    import pytest as _pytest

    @_pytest.mark.parametrize(
        "raw,expected",
        [
            ("0", 1),  # journalctl -n 0 == all lines → must clamp up
            ("-5", 1),
            ("1", 1),
            ("200", 200),  # exact max, unchanged
            ("201", 200),  # over max, clamped
            ("", 3),  # default
            (None, 3),
            ("3.5", 3),  # non-int → default
            ("  ", 3),  # whitespace non-int → default
        ],
    )
    def test_clamp(self, raw, expected):
        from control_server.routes.diagnostics._sse import _parse_lines_param

        assert _parse_lines_param(raw) == expected


class TestSupportLogsBundleBudgetBoundary:
    """#416 fu — pin the mid-loop truncation math + the strict `>` boundary."""

    def test_mid_loop_truncation_names_only_skipped(self):
        from control_server.routes.diagnostics._copy_payload import build_support_logs_bundle

        # start=0; u0 check=0 (read); u1 check=10 (>5 → skip u1,u2)
        clock = iter([0, 0, 10, 10]).__next__
        out = build_support_logs_bundle("SYS", ("u0", "u1", "u2"), lambda u: [f"{u}-l"], budget_s=5, clock=clock)
        assert "### u0" in out and "u0-l" in out
        assert "### u1" not in out and "### u2" not in out
        assert "u1, u2" in out and "truncated" in out

    def test_elapsed_equal_budget_does_not_truncate(self):
        from control_server.routes.diagnostics._copy_payload import build_support_logs_bundle

        # elapsed == budget on the check → `>` is False → the unit IS read.
        clock = iter([0, 5]).__next__
        out = build_support_logs_bundle("SYS", ("u0",), lambda u: ["l"], budget_s=5, clock=clock)
        assert "### u0" in out and "truncated" not in out


class TestSupportLogsEndpointMore:
    """#416 fu — HTTP-layer wiring for truncation, allowlist, empty, deep redaction."""

    def test_budget_truncation_reaches_response(self, diag_env, monkeypatch):
        from control_server.routes.diagnostics import _sse

        monkeypatch.setattr(_sse, "DIAG_SUPPORT_LOGS_BUDGET_S", 0.0)  # trips immediately
        monkeypatch.setattr(_sse, "_read_journal_tail", lambda unit, n=50, cache_key=None: ["x"])
        with diag_env.test_client() as c:
            r = c.get("/api/diagnostics/support-logs")
        assert r.status_code == 200
        assert "truncated" in r.get_data(as_text=True)

    def test_bundle_units_are_exactly_the_allowlist(self, diag_env, monkeypatch):
        import re

        from control_server.routes.diagnostics import _collectors, _sse

        monkeypatch.setattr(_sse, "_read_journal_tail", lambda unit, n=50, cache_key=None: ["l"])
        with diag_env.test_client() as c:
            r = c.get("/api/diagnostics/support-logs")
        rendered = set(re.findall(r"^### (.+)$", r.get_data(as_text=True), re.MULTILINE))
        assert rendered == set(_collectors.DIAG_UNITS), "bundle must iterate exactly the DIAG_UNITS allowlist"

    def test_empty_journal_placeholder_end_to_end(self, diag_env, monkeypatch):
        from control_server.routes.diagnostics import _sse

        monkeypatch.setattr(_sse, "_read_journal_tail", lambda unit, n=50, cache_key=None: [])
        with diag_env.test_client() as c:
            r = c.get("/api/diagnostics/support-logs")
        assert "(no journal entries)" in r.get_data(as_text=True)

    def test_content_type_has_single_charset(self, diag_env, monkeypatch):
        """Regression: mimetype was passed with an inline charset AND werkzeug
        appended one → 'text/plain; charset=utf-8; charset=utf-8' (QA-caught)."""
        from control_server.routes.diagnostics import _sse

        monkeypatch.setattr(_sse, "_read_journal_tail", lambda unit, n=50, cache_key=None: ["l"])
        with diag_env.test_client() as c:
            r = c.get("/api/diagnostics/support-logs")
        ct = r.headers.get("Content-Type", "")
        assert ct.count("charset=") == 1, f"charset must appear exactly once: {ct!r}"
        assert ct == "text/plain; charset=utf-8"

    def test_deep_single_unit_journal_path_redacts(self, diag_env, monkeypatch):
        from control_server.routes.diagnostics import _sse

        monkeypatch.setattr(
            _sse, "_read_journal_tail", lambda unit, n=3, cache_key=None: ["OPENWEATHERMAP_APIKEY=deadbeefsecret999"]
        )
        with diag_env.test_client() as c:
            r = c.get("/api/diagnostics/journal?unit=litclock.service&lines=25")
        assert "deadbeefsecret999" not in r.get_data(as_text=True), "deep ?lines path must redact too"
