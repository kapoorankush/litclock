"""Threshold-boundary tests for ``_compute_anomalies``.

The pre-litclock-dev#419 tests in ``tests/test_control_server_diagnostics.py`` use
the real wall clock, which makes "exactly at the threshold" assertions
flaky AND lets hardcoded ISO timestamps silently rot past their own age
thresholds (see the pre-existing ``last_dhcp_at`` drift in
``TestAnomalyDetector._baseline`` that PR1 fixed as a drive-by).

This file pins the clock via :mod:`pytest`'s ``monkeypatch`` (no
freezegun dependency per litclock-dev#419 D6) and asserts behavior at:

- exactly at the threshold,
- 1 ms over (anomaly trips),
- 1 ms under (anomaly does NOT trip).

Each test covers one anomaly path: IP-geo age, quote age. DHCP age is no
longer a threshold at all (litclock-dev#552) — its suite asserts the trigger never
fires, at any lease age, rather than walking a boundary.
The clock is pinned at a fixed instant ``T0``; payload timestamps are
computed from T0 ± offset so the test math is deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from control_server.routes.diagnostics import _anomalies, _collectors

# Fixed instant for "now" inside the test. Picked far from real wall clock
# so a buggy test that DIDN'T monkeypatch would fail obviously.
T0 = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture()
def frozen_clock(monkeypatch):
    """Pin ``_anomalies.datetime.now`` and ``_anomalies.time.time`` so the
    threshold math is deterministic. Returns the pinned T0 so tests can
    derive ±offset payloads."""

    class _FrozenDateTime(datetime):
        """A datetime subclass with ``now`` returning T0 regardless of tz."""

        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            if tz is None:
                return T0.replace(tzinfo=None)
            return T0.astimezone(tz)

        @classmethod
        def fromisoformat(cls, s):  # type: ignore[override]
            # Delegate to the real datetime so dhcp_iso parsing still works.
            return datetime.fromisoformat(s)

    monkeypatch.setattr(_anomalies, "datetime", _FrozenDateTime)
    # _anomalies.time.time() is used for the quote-age path.
    monkeypatch.setattr(_anomalies.time, "time", lambda: T0.timestamp())
    return T0


def _baseline(frozen_now: datetime) -> dict:
    """Return a values dict that's "clean" relative to ``frozen_now`` — no
    section trips. Anchored to the frozen clock so the baseline can't rot."""
    recent_dhcp_iso = (frozen_now - timedelta(hours=1)).isoformat()
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
        "picked_at": frozen_now.timestamp(),
        "setup_complete": True,
        "handoff_complete": True,
        "recent_log_entries": [],
    }


class TestDhcpAgeIsNotAnAnomaly:
    """litclock-dev#552 — DHCP age must never drive the network anomaly.

    The NM dispatcher short-circuits on an unchanged IP, so a stable lease
    freezes ``last_dhcp_at`` at boot: the longer the network works perfectly,
    the older it looks. The old 24h threshold therefore reported a fault on
    exactly the healthiest clocks. Measured on the test Pi: signal -37 dBm,
    lan_ip present, nmcli CONNECTIVITY full, and a 291-hour-old timestamp was
    the only complaint.
    """

    def _baseline_with_dhcp(self, frozen_now: datetime, age: timedelta) -> dict:
        v = _baseline(frozen_now)
        v["last_dhcp_at"] = (frozen_now - age).isoformat()
        return v

    @pytest.mark.parametrize(
        "age",
        [
            timedelta(hours=23, minutes=59),
            timedelta(hours=24),
            timedelta(hours=24, milliseconds=1),
            timedelta(days=12),  # the reported case
            timedelta(days=365),
        ],
        ids=["under-24h", "at-24h", "just-over-24h", "12-days", "a-year"],
    )
    def test_no_network_anomaly_at_any_lease_age(self, frozen_clock, age):
        """An otherwise-healthy clock never reports a network fault, however
        long its lease has been stable."""
        v = self._baseline_with_dhcp(frozen_clock, age)
        assert "network" not in _anomalies._compute_anomalies(v)

    def test_real_faults_still_trip_regardless_of_lease_age(self, frozen_clock):
        """Removing the trigger must not blunt the checks that do work."""
        v = self._baseline_with_dhcp(frozen_clock, timedelta(days=12))
        v["lan_ip"] = ""
        assert "network" in _anomalies._compute_anomalies(v), "missing lan_ip must still trip"

        v = self._baseline_with_dhcp(frozen_clock, timedelta(days=12))
        v["signal_dbm"] = -90
        assert "network" in _anomalies._compute_anomalies(v), "weak signal must still trip"


class TestMissingLanIpSettlingWindow:
    """litclock-dev#596 — a missing LAN IP inside the post-boot settling window is not a
    fault. The /run marker (tmpfs, dispatcher-written on IP change) is briefly
    cold right after provisioning, and a brand-new owner opens Diagnostics over
    the very connection it would falsely flag as broken. Past the grace, a
    still-absent marker is a real 'never acquired an IP' fault and trips.
    """

    def _no_ip(self, frozen_now: datetime, uptime_s) -> dict:
        v = _baseline(frozen_now)
        v["lan_ip"] = None
        if uptime_s is not None:
            v["uptime_s"] = uptime_s
        return v

    def test_missing_ip_during_settling_is_not_a_network_fault(self, frozen_clock):
        v = self._no_ip(frozen_clock, uptime_s=_anomalies.ANOMALY_LAN_IP_SETTLE_S - 1)
        assert "network" not in _anomalies._compute_anomalies(v), (
            "a cold /run marker within the settling window must not amber-banner a connection that is serving the page"
        )

    def test_missing_ip_after_settling_trips(self, frozen_clock):
        v = self._no_ip(frozen_clock, uptime_s=_anomalies.ANOMALY_LAN_IP_SETTLE_S + 1)
        assert "network" in _anomalies._compute_anomalies(v), "past the grace, a still-absent LAN IP is a real fault"

    def test_missing_ip_at_the_grace_boundary_trips(self, frozen_clock):
        # Boundary is inclusive-fault: settling is strictly `< grace`.
        v = self._no_ip(frozen_clock, uptime_s=_anomalies.ANOMALY_LAN_IP_SETTLE_S)
        assert "network" in _anomalies._compute_anomalies(v)

    def test_missing_ip_with_unknown_uptime_trips(self, frozen_clock):
        # uptime absent (/proc/uptime unreadable) must NOT silently suppress a
        # real fault — settling requires a known, small uptime.
        v = self._no_ip(frozen_clock, uptime_s=None)
        assert "network" in _anomalies._compute_anomalies(v)

    def test_weak_signal_trips_even_within_the_settling_window(self, frozen_clock):
        # The grace only covers the missing-IP trigger, never a genuinely weak
        # radio — a -90 dBm link is a real problem regardless of uptime.
        v = self._no_ip(frozen_clock, uptime_s=1)
        v["signal_dbm"] = -90
        assert "network" in _anomalies._compute_anomalies(v)

    @pytest.mark.parametrize("bad_uptime", [True, False, -1, -0.001])
    def test_non_sane_uptime_does_not_suppress_the_fault(self, frozen_clock, bad_uptime):
        # bool and negative uptime must NOT count as settling — a garbage value
        # can't silently hide a real missing-IP fault. Unreachable in production
        # (_read_appliance_uptime_s returns int|None ≥ 0) but pinned so it stays
        # fail-safe. NaN/inf are covered by the strict `< grace` comparison.
        v = self._no_ip(frozen_clock, uptime_s=bad_uptime)
        assert "network" in _anomalies._compute_anomalies(v)

    def test_reported_scenario_through_the_precedence_layer(self, frozen_clock):
        # End-to-end via _compute_section_states (the effective route/template
        # state, uncollected-wins precedence), reproducing the RC report: SSID +
        # signal present, LAN IP missing (cold /run marker). SSID present means
        # the section is NOT "uncollected", so within the grace the fix must keep
        # 'network' out of BOTH anomalies and uncollected (grey/quiet, no amber),
        # and surface it as a real anomaly once settled.
        v = _baseline(frozen_clock)
        v["ssid"] = "HomeWiFi"
        v["lan_ip"] = None

        v["uptime_s"] = _anomalies.ANOMALY_LAN_IP_SETTLE_S - 1
        anomalies, uncollected = _anomalies._compute_section_states(v)
        assert "network" not in anomalies, "no amber 'Connection issue' during the settling window"
        assert "network" not in uncollected

        v["uptime_s"] = _anomalies.ANOMALY_LAN_IP_SETTLE_S + 1
        anomalies, uncollected = _anomalies._compute_section_states(v)
        assert "network" in anomalies, "a still-absent IP past the grace is a real fault"


class TestIpGeoAgeThreshold:
    """ANOMALY_LAST_IPGEO_AGE_S = 7 days. Only fires when weather is enabled."""

    def _payload_with_ipgeo(self, frozen_now: datetime, age: timedelta) -> dict:
        v = _baseline(frozen_now)
        # Enable weather AND set a valid location_name so the only path to
        # tripping the anomaly is the age check.
        v["weather_enabled"] = True
        v["weather_location_name"] = "San Francisco"
        v["last_ip_geo_at"] = (frozen_now - age).isoformat()
        return v

    def test_one_ms_under_threshold(self, frozen_clock):
        v = self._payload_with_ipgeo(
            frozen_clock,
            timedelta(seconds=_anomalies.ANOMALY_LAST_IPGEO_AGE_S, milliseconds=-1),
        )
        assert "time-location" not in _anomalies._compute_anomalies(v)

    def test_at_threshold_no_anomaly(self, frozen_clock):
        # Strict > → equality does NOT trip. Mirrors the symmetric tests
        # in TestDhcpAgeThreshold + TestQuoteAgeThreshold so a regression
        # that flipped > to >= gets caught here too. Codex /review #3.
        v = self._payload_with_ipgeo(
            frozen_clock,
            timedelta(seconds=_anomalies.ANOMALY_LAST_IPGEO_AGE_S),
        )
        assert "time-location" not in _anomalies._compute_anomalies(v)

    def test_one_ms_over_threshold_trips(self, frozen_clock):
        v = self._payload_with_ipgeo(
            frozen_clock,
            timedelta(seconds=_anomalies.ANOMALY_LAST_IPGEO_AGE_S, milliseconds=1),
        )
        assert "time-location" in _anomalies._compute_anomalies(v)


class TestQuoteAgeThreshold:
    """ANOMALY_QUOTE_AGE_S = 90 s. Uses ``time.time()`` not datetime.now."""

    def test_one_ms_under_threshold(self, frozen_clock):
        v = _baseline(frozen_clock)
        v["picked_at"] = frozen_clock.timestamp() - (_anomalies.ANOMALY_QUOTE_AGE_S - 0.001)
        assert "last-quote" not in _anomalies._compute_anomalies(v)

    def test_at_threshold_no_anomaly(self, frozen_clock):
        # Strict > → equality does not trip.
        v = _baseline(frozen_clock)
        v["picked_at"] = frozen_clock.timestamp() - _anomalies.ANOMALY_QUOTE_AGE_S
        assert "last-quote" not in _anomalies._compute_anomalies(v)

    def test_one_ms_over_threshold_trips(self, frozen_clock):
        v = _baseline(frozen_clock)
        v["picked_at"] = frozen_clock.timestamp() - (_anomalies.ANOMALY_QUOTE_AGE_S + 0.001)
        assert "last-quote" in _anomalies._compute_anomalies(v)


class TestFrozenClockSanity:
    """Confirm the frozen_clock fixture actually intercepts both clocks.

    A broken monkeypatch would silently fall back to wall-clock time and
    the threshold tests would become flaky again. Pin this so a future
    refactor of ``_anomalies.datetime`` resolution gets caught.
    """

    def test_datetime_now_returns_T0(self, frozen_clock):
        assert _anomalies.datetime.now(tz=UTC) == T0

    def test_time_time_returns_T0_timestamp(self, frozen_clock):
        assert _anomalies.time.time() == T0.timestamp()


class TestServicesOneshotLifecycle:
    """litclock-dev#443 — oneshot units (``litclock.service``) cycle through
    ``activating``/``deactivating`` every minute during the per-minute quote
    paint. A ``/diagnostics`` poll landing in that window must NOT trip the
    services anomaly (which escalates the banner to the oxblood "Clock isn't
    running" error tier). ``failed`` is still a real failure for oneshots,
    and non-oneshot units get no lifecycle pass.
    """

    def _with_service(self, frozen_now: datetime, unit: str, state: str) -> dict:
        v = _baseline(frozen_now)
        v["service_states"] = {unit: {"state": state}}
        return v

    def test_oneshot_activating_not_anomaly(self, frozen_clock):
        # The per-minute paint scenario: litclock.service mid-paint.
        v = self._with_service(frozen_clock, "litclock.service", "activating")
        assert "services" not in _anomalies._compute_anomalies(v)

    def test_oneshot_deactivating_not_anomaly(self, frozen_clock):
        v = self._with_service(frozen_clock, "litclock.service", "deactivating")
        assert "services" not in _anomalies._compute_anomalies(v)

    def test_oneshot_inactive_not_anomaly(self, frozen_clock):
        # Regression: the original carve-out (settled resting state) holds.
        v = self._with_service(frozen_clock, "litclock.service", "inactive")
        assert "services" not in _anomalies._compute_anomalies(v)

    def test_oneshot_failed_still_anomaly(self, frozen_clock):
        # Iron rule: a failed oneshot IS a real failure.
        v = self._with_service(frozen_clock, "litclock.service", "failed")
        assert "services" in _anomalies._compute_anomalies(v)

    def test_non_oneshot_activating_still_anomaly(self, frozen_clock):
        # Iron rule: only DIAG_ONESHOT_UNITS get the lifecycle pass.
        # litclock-control.service is a long-running unit, not a oneshot.
        v = self._with_service(frozen_clock, "litclock-control.service", "activating")
        assert "services" in _anomalies._compute_anomalies(v)

    def test_oneshot_skip_does_not_short_circuit_sibling_failure(self, frozen_clock):
        # The services loop uses continue (oneshot skip) + break (anomaly).
        # A skipped oneshot mid-paint must NOT hide a real failure on a
        # sibling non-oneshot unit, regardless of dict iteration order.
        v = _baseline(frozen_clock)
        v["service_states"] = {
            "litclock.service": {"state": "activating"},
            "litclock-control.service": {"state": "failed"},
        }
        assert "services" in _anomalies._compute_anomalies(v)
        # Reverse insertion order — the failure comes first.
        v["service_states"] = {
            "litclock-control.service": {"state": "failed"},
            "litclock.service": {"state": "activating"},
        }
        assert "services" in _anomalies._compute_anomalies(v)

    def test_oneshot_unknown_is_not_anomaly(self, frozen_clock):
        # Documented asymmetry: a oneshot in "unknown" (systemctl is-active
        # couldn't read state) is NOT a services anomaly here, yet
        # _is_obviously_healthy returns False for it (so it still pulls a
        # journal tail). Pin the no-anomaly half; the tail half lives in the
        # readers test. "unknown" is intentionally outside
        # DIAG_ONESHOT_NONANOMALY_STATES.
        v = self._with_service(frozen_clock, "litclock.service", "unknown")
        assert "services" not in _anomalies._compute_anomalies(v)

    def test_stuck_activating_surfaces_via_last_quote_backstop(self, frozen_clock):
        # The carve-out is durationless, so a genuinely wedged paint stuck in
        # "activating" is silenced on the services section. The safety net is
        # the last-quote anomaly: a hung paint stops advancing picked_at, so
        # once it ages past ANOMALY_QUOTE_AGE_S the clock still surfaces as
        # broken — via a more accurate signal than a per-minute services flap.
        # Lock that backstop so a future change can't weaken it silently (litclock-dev#443).
        v = self._with_service(frozen_clock, "litclock.service", "activating")
        v["picked_at"] = frozen_clock.timestamp() - (_anomalies.ANOMALY_QUOTE_AGE_S + 1)
        result = _anomalies._compute_anomalies(v)
        assert "services" not in result
        assert "last-quote" in result


class TestOneshotLockstep:
    """litclock-dev#443 — the anomaly verdict (``_compute_anomalies``) and the lazy-tail
    journal-fetch decision (``_is_obviously_healthy``, litclock-dev#433) are driven by the
    SAME ``DIAG_ONESHOT_NONANOMALY_STATES`` constant. Pin the semantic
    invariant so the two can never disagree on a oneshot lifecycle state — a
    unit flagged anomalous but denied its journal tail would lose the debug
    context the P-1 filter exists to preserve.
    """

    def test_anomaly_and_health_agree_on_oneshot_lifecycle_states(self, frozen_clock):
        unit = "litclock.service"
        assert unit in _collectors.DIAG_ONESHOT_UNITS
        for state in _collectors.DIAG_ONESHOT_NONANOMALY_STATES:
            v = _baseline(frozen_clock)
            v["service_states"] = {unit: {"state": state}}
            is_anomaly = "services" in _anomalies._compute_anomalies(v)
            is_healthy = _collectors._is_obviously_healthy(state, unit)
            # Non-anomaly states for a oneshot must read as obviously healthy.
            assert not is_anomaly
            assert is_healthy

    def test_failed_oneshot_is_anomaly_and_not_healthy(self, frozen_clock):
        unit = "litclock.service"
        v = _baseline(frozen_clock)
        v["service_states"] = {unit: {"state": "failed"}}
        assert "services" in _anomalies._compute_anomalies(v)
        assert _collectors._is_obviously_healthy("failed", unit) is False
