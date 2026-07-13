"""Threshold-boundary tests for ``_compute_anomalies``.

The pre-#419 tests in ``tests/test_control_server_diagnostics.py`` use
the real wall clock, which makes "exactly at the threshold" assertions
flaky AND lets hardcoded ISO timestamps silently rot past their own age
thresholds (see the pre-existing ``last_dhcp_at`` drift in
``TestAnomalyDetector._baseline`` that PR1 fixed as a drive-by).

This file pins the clock via :mod:`pytest`'s ``monkeypatch`` (no
freezegun dependency per #419 D6) and asserts behavior at:

- exactly at the threshold,
- 1 ms over (anomaly trips),
- 1 ms under (anomaly does NOT trip).

Each test covers one anomaly path: DHCP age, IP-geo age, quote age.
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


class TestDhcpAgeThreshold:
    """ANOMALY_DHCP_AGE_S = 24 h. Pin clock + walk the boundary."""

    def _baseline_with_dhcp(self, frozen_now: datetime, age: timedelta) -> dict:
        v = _baseline(frozen_now)
        v["last_dhcp_at"] = (frozen_now - age).isoformat()
        return v

    def test_below_threshold_no_anomaly(self, frozen_clock):
        v = self._baseline_with_dhcp(frozen_clock, timedelta(hours=23, minutes=59))
        assert "network" not in _anomalies._compute_anomalies(v)

    def test_one_ms_under_threshold(self, frozen_clock):
        # Exactly 24h MINUS 1ms — must not trip (strict >).
        v = self._baseline_with_dhcp(frozen_clock, timedelta(seconds=_anomalies.ANOMALY_DHCP_AGE_S, milliseconds=-1))
        assert "network" not in _anomalies._compute_anomalies(v)

    def test_at_threshold_no_anomaly(self, frozen_clock):
        # Exactly at the boundary — condition is ``age > ANOMALY_DHCP_AGE_S``
        # so equality does NOT trip.
        v = self._baseline_with_dhcp(frozen_clock, timedelta(seconds=_anomalies.ANOMALY_DHCP_AGE_S))
        assert "network" not in _anomalies._compute_anomalies(v)

    def test_one_ms_over_threshold_trips(self, frozen_clock):
        # Exactly 24h PLUS 1ms — anomaly fires.
        v = self._baseline_with_dhcp(frozen_clock, timedelta(seconds=_anomalies.ANOMALY_DHCP_AGE_S, milliseconds=1))
        assert "network" in _anomalies._compute_anomalies(v)


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
    """#443 — oneshot units (``litclock.service``) cycle through
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
        # Lock that backstop so a future change can't weaken it silently (#443).
        v = self._with_service(frozen_clock, "litclock.service", "activating")
        v["picked_at"] = frozen_clock.timestamp() - (_anomalies.ANOMALY_QUOTE_AGE_S + 1)
        result = _anomalies._compute_anomalies(v)
        assert "services" not in result
        assert "last-quote" in result


class TestOneshotLockstep:
    """#443 — the anomaly verdict (``_compute_anomalies``) and the lazy-tail
    journal-fetch decision (``_is_obviously_healthy``, #433) are driven by the
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
