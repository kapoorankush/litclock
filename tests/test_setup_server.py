"""Tests for setup_server module — IP-geo resolver, timezone helper, captive portal API."""

import pathlib
import re
import subprocess
import sys
import threading

import pytest

import geocoding
import setup_server

# ── set_system_timezone ─────────────────────────────────────────────


class TestSetSystemTimezone:
    def test_empty_string(self):
        ok, err = setup_server.set_system_timezone("")
        assert ok is False
        assert "no timezone" in err.lower()

    def test_invalid_timezone(self, mocker):
        mocker.patch(
            "geocoding.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="America/New_York\nEurope/London\n"),
        )
        ok, err = setup_server.set_system_timezone("Invalid/Zone")
        assert ok is False
        assert "invalid" in err.lower()

    def test_valid_timezone(self, mocker):
        # First call: list-timezones; second call: set-timezone
        mocker.patch(
            "geocoding.subprocess.run",
            side_effect=[
                subprocess.CompletedProcess(args=[], returncode=0, stdout="America/New_York\nEurope/London\n"),
                subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            ],
        )
        ok, err = setup_server.set_system_timezone("America/New_York")
        assert ok is True
        assert err is None

    def test_timedatectl_not_found(self, mocker):
        mocker.patch("geocoding.subprocess.run", side_effect=FileNotFoundError)
        ok, err = setup_server.set_system_timezone("America/New_York")
        assert ok is False
        assert "not found" in err.lower()


# ── _update_env_location (EPIC litclock-dev#383 kwargs API) ───────────────────────


class TestUpdateEnvLocation:
    """T3 + T5: the resolver-side writer takes keyword args for the four
    LOCATION_ENV_KEYS plus an optional timezone. Atomic-write ordering is
    inverted from pre-pivot: timedatectl runs FIRST and a failure there
    aborts the env write entirely. Worst failure case becomes 'tz set,
    env stale' (correct clock time, no weather) instead of 'env populated,
    tz stale' (wrong-time clock — A2-revised hard-blocker)."""

    def test_writes_all_four_location_env_keys(self, tmp_env_file, monkeypatch):
        """T2/T3: all four geocoding.LOCATION_ENV_KEYS land in env.sh."""
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        monkeypatch.setattr(geocoding, "set_system_timezone", lambda tz: (True, None))

        setup_server._update_env_location(
            "30.27",
            "-97.74",
            location_name="Austin, TX",
            units="imperial",
            timezone="America/Chicago",
        )

        with open(tmp_env_file) as f:
            content = f.read()
        assert "WEATHER_LATITUDE=30.27" in content
        assert "WEATHER_LONGITUDE=-97.74" in content
        assert 'WEATHER_LOCATION_NAME="Austin, TX"' in content or "WEATHER_LOCATION_NAME='Austin, TX'" in content
        assert "WEATHER_UNITS=imperial" in content

    def test_writes_weather_location_name_separately(self, tmp_env_file, monkeypatch):
        """T3 / litclock-dev#380 regression: WEATHER_LOCATION_NAME must be written when
        location_name is supplied. The pre-pivot writer skipped this key,
        which is why the Status tab showed raw coords after first-boot."""
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        monkeypatch.setattr(geocoding, "set_system_timezone", lambda tz: (True, None))

        setup_server._update_env_location("30.27", "-97.74", location_name="Austin, TX", timezone="America/Chicago")

        with open(tmp_env_file) as f:
            content = f.read()
        # Quoted variants both accepted by config.atomic_update's shlex emitter.
        assert "Austin, TX" in content
        assert "WEATHER_LOCATION_NAME=" in content

    def test_skips_env_write_when_set_system_timezone_fails(self, tmp_env_file, monkeypatch):
        """T5: if timedatectl fails, abort the env write entirely. The clock
        showing quotes at the wrong time is a worse failure than 'no
        weather' — see plan A2 revision.

        Strong assertion: spy on config.atomic_update and verify it was NEVER
        called. The pre-strengthen version asserted before==after on the file
        contents, which would pass even if atomic_update fired and crashed
        mid-write (transactional contract leaves the file unchanged on
        ValueError too). Asserting on the call directly pins the control flow.
        """
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        monkeypatch.setattr(geocoding, "set_system_timezone", lambda tz: (False, "timedatectl unavailable"))

        import config as _config

        atomic_calls = []

        def spy_atomic_update(updates, path):
            atomic_calls.append(updates)

        monkeypatch.setattr(_config, "atomic_update", spy_atomic_update)

        setup_server._update_env_location(
            "30.27", "-97.74", location_name="Austin, TX", units="imperial", timezone="Bogus/Zone"
        )

        assert atomic_calls == [], (
            f"config.atomic_update must not run when set_system_timezone fails; got: {atomic_calls}"
        )

    def test_calls_set_system_timezone_before_atomic_update(self, tmp_env_file, monkeypatch):
        """T5: ordering invariant. set_system_timezone MUST run before
        config.atomic_update — codex outside-voice tension #3."""
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        events = []

        def fake_set_tz(tz):
            events.append("tz")
            return True, None

        import config as _config

        real_atomic_update = _config.atomic_update

        def tracking_atomic_update(updates, path):
            events.append("env")
            return real_atomic_update(updates, path)

        monkeypatch.setattr(geocoding, "set_system_timezone", fake_set_tz)
        monkeypatch.setattr(_config, "atomic_update", tracking_atomic_update)

        setup_server._update_env_location(
            "30.27", "-97.74", location_name="Austin, TX", units="imperial", timezone="America/Chicago"
        )

        assert events == ["tz", "env"], f"expected tz-then-env, got {events}"

    def test_skips_when_env_file_missing(self, monkeypatch):
        """No env file configured → no-op, no crash. Defensive guard for
        test setups where ENV_FILE may be None."""
        monkeypatch.setattr(setup_server, "ENV_FILE", None)
        # Should not raise:
        setup_server._update_env_location("30.27", "-97.74", location_name="Austin, TX")

    def test_skips_when_no_kwargs_supplied(self, tmp_env_file, monkeypatch):
        """All-None kwargs → no env write. PATCH semantics: missing fields
        are skipped, not zeroed."""
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        monkeypatch.setattr(geocoding, "set_system_timezone", lambda tz: (True, None))

        with open(tmp_env_file) as f:
            before = f.read()

        setup_server._update_env_location(None, None)

        with open(tmp_env_file) as f:
            after = f.read()
        assert before == after

    def test_rejects_invalid_coords_silently(self, tmp_env_file, monkeypatch):
        """Shell-injection probe in lat is caught by config.atomic_update's
        validator. The function prints a warning but does not raise, since
        it runs in a background thread where uncaught exceptions disappear.

        A valid timezone is passed so the call clears the litclock-dev#393 "coords without
        a tz" guard and actually reaches atomic_update's validator — the thing
        this test is pinning."""
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        monkeypatch.setattr(geocoding, "set_system_timezone", lambda tz: (True, None))

        with open(tmp_env_file) as f:
            before = f.read()

        # Should not raise even though "$(whoami)" is not a valid coord.
        setup_server._update_env_location("$(whoami)", "0", units="imperial", timezone="America/Chicago")

        with open(tmp_env_file) as f:
            after = f.read()
        assert before == after

    def test_skips_coords_when_timezone_unresolved(self, tmp_env_file, monkeypatch):
        """litclock-dev#393: coordinates must NOT be persisted without a resolved timezone.
        handoff.timezone_known() reads "tz correct" off a populated
        WEATHER_LATITUDE, so writing lat/lon with timezone=None would falsely
        pass the handoff gate and start a wrong-time clock. With no tz the writer
        must no-op — the same outcome as the resolver's hard-fail path.

        Triggered when ip-api returns coords with no ``timezone`` AND the offline
        timezone_from_coords fallback also returns None (degenerate/edge coords).
        """
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)

        import config as _config

        atomic_calls = []
        monkeypatch.setattr(_config, "atomic_update", lambda updates, path: atomic_calls.append(updates))

        # set_system_timezone must not be reached either — there's no tz to set.
        tz_calls = []
        monkeypatch.setattr(geocoding, "set_system_timezone", lambda tz: (tz_calls.append(tz), (True, None))[1])

        setup_server._update_env_location(
            "30.27", "-97.74", location_name="Austin, TX", units="imperial", timezone=None
        )

        assert atomic_calls == [], f"coords must not be written without a tz; got {atomic_calls}"
        assert tz_calls == [], "set_system_timezone must not run when timezone is None"

        with open(tmp_env_file) as f:
            content = f.read()
        assert "WEATHER_LATITUDE=30.27" not in content
        assert "WEATHER_LONGITUDE=-97.74" not in content

    def test_skips_partial_coords_even_with_timezone(self, tmp_env_file, monkeypatch):
        """litclock-dev#393 (review follow-up): a single-axis coord (lat present, lon
        missing) must NOT be written, even with a tz. handoff.py:_has_location
        needs BOTH axes but litclock-handoff-fallback.sh checks latitude alone —
        a lat-only env state would fool the shell fallback into completing the
        handoff. Coordinates are an atomic pair; a partial pair writes nothing."""
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)

        import config as _config

        atomic_calls = []
        monkeypatch.setattr(_config, "atomic_update", lambda updates, path: atomic_calls.append(updates))
        tz_calls = []
        monkeypatch.setattr(geocoding, "set_system_timezone", lambda tz: (tz_calls.append(tz), (True, None))[1])

        setup_server._update_env_location("30.27", None, location_name="Austin, TX", timezone="America/Chicago")

        assert atomic_calls == [], f"a single-axis coord must not be written; got {atomic_calls}"
        assert tz_calls == [], "set_system_timezone must not run for an incomplete coord pair"
        with open(tmp_env_file) as f:
            content = f.read()
        assert "WEATHER_LATITUDE=30.27" not in content

    def test_timezone_only_write_still_allowed(self, tmp_env_file, monkeypatch):
        """litclock-dev#393 guard must NOT block the PATCH-semantics tz-only write. The
        resolver docstring supports handing off "only timezone resolved" state
        (tz set, no coords): set_system_timezone runs, and a units update (no
        coords) still lands. The guard only fires for coords-without-tz."""
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        tz_calls = []
        monkeypatch.setattr(geocoding, "set_system_timezone", lambda tz: (tz_calls.append(tz), (True, None))[1])

        setup_server._update_env_location(None, None, units="imperial", timezone="America/Chicago")

        assert tz_calls == ["America/Chicago"], "tz-only write must still set the system timezone"
        with open(tmp_env_file) as f:
            content = f.read()
        assert "WEATHER_UNITS=imperial" in content


# ── _resolve_location_from_ip (EPIC litclock-dev#383 IP-geo retry pipeline) ──────


class TestResolveDeferredLocation:
    """T4 + T6: post-EPIC-383 the resolver runs IP-geo with 3 retries and
    derives units from country_code. There is no longer a 'stashed query'
    code path — the hotspot form collects WiFi credentials only."""

    @pytest.fixture(autouse=True)
    def _fast_retries(self, monkeypatch):
        """Stub out time.sleep so retry tests don't sit for 13 real seconds.
        Setup_server's resolver imports `time` locally, so we patch
        ``time.sleep`` at the module level — both call sites pick it up."""
        import time

        monkeypatch.setattr(time, "sleep", lambda _s: None)

    def _stub_update_env_location(self, monkeypatch):
        """Capture the kwargs the resolver hands to _update_env_location.

        litclock-dev#337 A4 extracted the resolver to location_resolver.py; the writer
        now also receives ``mode`` and ``ip_country`` kwargs (A1 + A6.1).
        ``**kwargs`` here keeps the legacy assertions working while letting
        the new-test cases inspect the new kwargs explicitly (see the
        ``calls[0].get("mode")`` / ``calls[0].get("ip_country")`` assertions
        in tests/test_location_resolver.py).
        """
        calls: list[dict] = []

        def fake(lat, lon, *, location_name=None, units=None, timezone=None, **kwargs):
            calls.append(
                {
                    "lat": lat,
                    "lon": lon,
                    "location_name": location_name,
                    "units": units,
                    "timezone": timezone,
                    **kwargs,
                }
            )

        monkeypatch.setattr(setup_server, "_update_env_location", fake)
        return calls

    def test_succeeds_first_attempt(self, monkeypatch, tmp_env_file):
        """T4: happy path — ip_geolocate returns valid result; one call
        to _update_env_location."""
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        ip_geo = {
            "lat": "30.27",
            "lon": "-97.74",
            "city": "Austin, TX",
            "country_code": "US",
            "timezone": "America/Chicago",
        }
        import geocoding

        attempts = []

        def ip_geo_stub():
            attempts.append(1)
            return ip_geo

        monkeypatch.setattr(geocoding, "ip_geolocate", ip_geo_stub)
        calls = self._stub_update_env_location(monkeypatch)

        setup_server._resolve_location_from_ip()

        assert len(attempts) == 1
        assert len(calls) == 1
        assert calls[0]["lat"] == "30.27"
        assert calls[0]["lon"] == "-97.74"
        assert calls[0]["location_name"] == "Austin, TX"
        assert calls[0]["timezone"] == "America/Chicago"
        assert calls[0]["units"] == "imperial"

    def test_retries_until_success_then_writes(self, monkeypatch, tmp_env_file):
        """T4: first 2 attempts return None (DNS race), third returns valid;
        the resolver still writes."""
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        ip_geo = {
            "lat": "48.85",
            "lon": "2.35",
            "city": "Paris, Île-de-France",
            "country_code": "FR",
            "timezone": "Europe/Paris",
        }
        import geocoding

        attempts = []

        def flaky_ip_geo():
            attempts.append(1)
            if len(attempts) < 3:
                return None
            return ip_geo

        monkeypatch.setattr(geocoding, "ip_geolocate", flaky_ip_geo)
        calls = self._stub_update_env_location(monkeypatch)

        setup_server._resolve_location_from_ip()

        assert len(attempts) == 3
        assert len(calls) == 1
        assert calls[0]["units"] == "metric"  # T6: non-US country → metric

    def test_hard_fail_after_all_retries(self, monkeypatch, tmp_env_file):
        """T4: ip_geolocate returns None every attempt → no env write.
        PR2 handoff splash will show 'Almost there — set timezone' copy."""
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        import geocoding

        attempts = []

        def always_none():
            attempts.append(1)
            return None

        monkeypatch.setattr(geocoding, "ip_geolocate", always_none)
        calls = self._stub_update_env_location(monkeypatch)

        setup_server._resolve_location_from_ip()

        # 1 initial + len(_IP_GEO_RETRY_DELAYS) retries
        assert len(attempts) == len(setup_server._IP_GEO_RETRY_DELAYS) + 1
        assert calls == [], "no env write on hard failure"

    def test_recovers_from_exceptions(self, monkeypatch, tmp_env_file):
        """T4: ip_geolocate raising must NOT propagate — the resolver runs in
        a daemon thread where exceptions disappear silently. The retry loop
        catches and continues; later success still writes."""
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        import geocoding

        attempts = []

        def raising_then_succeed():
            attempts.append(1)
            if len(attempts) == 1:
                raise ConnectionError("DNS not ready")
            return {
                "lat": "30.27",
                "lon": "-97.74",
                "city": "Austin, TX",
                "country_code": "US",
                "timezone": "America/Chicago",
            }

        monkeypatch.setattr(geocoding, "ip_geolocate", raising_then_succeed)
        calls = self._stub_update_env_location(monkeypatch)

        setup_server._resolve_location_from_ip()

        assert len(attempts) == 2
        assert len(calls) == 1

    def test_units_derive_us_imperial(self, monkeypatch, tmp_env_file):
        """T6: country_code == 'US' → imperial."""
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        import geocoding

        monkeypatch.setattr(
            geocoding,
            "ip_geolocate",
            lambda: {
                "lat": "30.27",
                "lon": "-97.74",
                "city": "Austin, TX",
                "country_code": "US",
                "timezone": "America/Chicago",
            },
        )
        calls = self._stub_update_env_location(monkeypatch)
        setup_server._resolve_location_from_ip()
        assert calls[0]["units"] == "imperial"

    def test_units_derive_fr_metric(self, monkeypatch, tmp_env_file):
        """T6: country_code == 'FR' → metric."""
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        import geocoding

        monkeypatch.setattr(
            geocoding,
            "ip_geolocate",
            lambda: {
                "lat": "48.85",
                "lon": "2.35",
                "city": "Paris, Île-de-France",
                "country_code": "FR",
                "timezone": "Europe/Paris",
            },
        )
        calls = self._stub_update_env_location(monkeypatch)
        setup_server._resolve_location_from_ip()
        assert calls[0]["units"] == "metric"

    def test_units_derive_none_imperial(self, monkeypatch, tmp_env_file):
        """T6 / A3: country_code missing → imperial fallback (matches
        existing reset-setup.sh default)."""
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        import geocoding

        monkeypatch.setattr(
            geocoding,
            "ip_geolocate",
            lambda: {
                "lat": "0",
                "lon": "0",
                "city": "Atlantis, ??",
                "country_code": None,
                "timezone": "UTC",
            },
        )
        calls = self._stub_update_env_location(monkeypatch)
        setup_server._resolve_location_from_ip()
        assert calls[0]["units"] == "imperial"

    def test_retry_delays_match_constant(self, monkeypatch, tmp_env_file):
        """Pin the sleep schedule to _IP_GEO_RETRY_DELAYS. If someone shortens
        the tuple to (1, 3) without intending to, the docstring's 'worst-case
        wall clock' regresses silently — the attempt-count test wouldn't
        notice."""
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        import time

        import geocoding

        sleep_args = []
        # Patch time.sleep at the module level since the resolver does
        # `import time` locally. The `_fast_retries` autouse fixture already
        # stubs it, so we need to override that with a capturing variant.
        monkeypatch.setattr(time, "sleep", lambda s: sleep_args.append(s))
        monkeypatch.setattr(geocoding, "ip_geolocate", lambda: None)
        self._stub_update_env_location(monkeypatch)

        setup_server._resolve_location_from_ip()

        assert tuple(sleep_args) == setup_server._IP_GEO_RETRY_DELAYS, (
            f"sleep schedule drifted: slept {sleep_args}, constant says {setup_server._IP_GEO_RETRY_DELAYS}"
        )

    def test_writer_keys_match_LOCATION_ENV_KEYS(self, tmp_env_file, monkeypatch):
        """Contract test: _update_env_location with all four kwargs populated
        must write exactly geocoding.LOCATION_ENV_KEYS. If someone adds a 5th
        key to the constant, this test reveals the writer drift."""
        import config as _config
        import geocoding

        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        monkeypatch.setattr(geocoding, "set_system_timezone", lambda tz: (True, None))

        captured: list[set[str]] = []
        monkeypatch.setattr(_config, "atomic_update", lambda updates, path: captured.append(set(updates.keys())))

        setup_server._update_env_location(
            "30.27",
            "-97.74",
            location_name="Austin, TX",
            units="imperial",
            timezone="America/Chicago",
        )

        assert captured == [set(geocoding.LOCATION_ENV_KEYS)], (
            f"writer keys drift: wrote {captured}, LOCATION_ENV_KEYS says {set(geocoding.LOCATION_ENV_KEYS)}"
        )

    def test_tz_from_coords_fallback_when_ip_geo_omits_timezone(self, monkeypatch, tmp_env_file):
        """When ip-api returns coords but no timezone, the resolver must fall
        back to timezone_from_coords(lat, lon). Without the fallback, the
        clock would proceed with system tz set to the Pi-default (often UTC)
        and render quotes at the wrong wall-clock time until PR2 ships
        browser-tz."""
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        import geocoding

        monkeypatch.setattr(
            geocoding,
            "ip_geolocate",
            lambda: {
                "lat": "30.27",
                "lon": "-97.74",
                "city": "Austin, TX",
                "country_code": "US",
                "timezone": None,
            },
        )
        monkeypatch.setattr(geocoding, "timezone_from_coords", lambda lat, lon: "America/Chicago")

        calls = self._stub_update_env_location(monkeypatch)
        setup_server._resolve_location_from_ip()

        assert len(calls) == 1
        assert calls[0]["timezone"] == "America/Chicago", (
            "resolver should derive tz from coords when ip-api returns no tz"
        )

    def test_propagates_none_tz_when_fallback_also_fails(self, monkeypatch, tmp_env_file):
        """litclock-dev#393: when ip-api returns coords with no timezone AND the offline
        timezone_from_coords fallback also returns None (degenerate/edge coords),
        the resolver must hand timezone=None to _update_env_location rather than
        fabricating a tz. The writer then skips the coord write (covered by
        TestUpdateEnvLocation.test_skips_coords_when_timezone_unresolved), so the
        handoff gate stays closed instead of starting a wrong-time clock."""
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        import geocoding

        monkeypatch.setattr(
            geocoding,
            "ip_geolocate",
            lambda: {
                "lat": "30.27",
                "lon": "-97.74",
                "city": "Austin, TX",
                "country_code": "US",
                "timezone": None,
            },
        )
        monkeypatch.setattr(geocoding, "timezone_from_coords", lambda lat, lon: None)

        calls = self._stub_update_env_location(monkeypatch)
        setup_server._resolve_location_from_ip()

        assert len(calls) == 1
        assert calls[0]["timezone"] is None, "resolver must propagate tz=None, not fabricate one"

    def test_ip_geo_empty_city_produces_no_location_name(self, monkeypatch, tmp_env_file):
        """ip-api returning city='' and regionName='' (cloud egress / VPN exit)
        previously produced WEATHER_LOCATION_NAME=', ' — a degenerate two-char
        string that surfaces in the PWA Status tab. After hardening
        ip_geolocate, empty components yield location_name=None (PATCH skip)."""
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        import geocoding

        monkeypatch.setattr(
            geocoding,
            "ip_geolocate",
            lambda: {
                "lat": "30.27",
                "lon": "-97.74",
                "city": None,
                "country_code": "US",
                "timezone": "America/Chicago",
            },
        )
        calls = self._stub_update_env_location(monkeypatch)
        setup_server._resolve_location_from_ip()

        assert len(calls) == 1
        assert calls[0]["location_name"] is None, (
            f"empty city should yield None, not a degenerate placeholder; got {calls[0]['location_name']!r}"
        )

    def test_degenerate_ip_geo_response_triggers_retry(self, monkeypatch, tmp_env_file):
        """ip-api returning lat=None/lon=None (rare degenerate response) must
        trigger a retry, not break out of the loop and silently no-op."""
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        import geocoding

        attempts = []

        def degenerate_then_good():
            attempts.append(1)
            if len(attempts) < 2:
                return {"lat": None, "lon": None, "city": None, "country_code": None, "timezone": None}
            return {
                "lat": "30.27",
                "lon": "-97.74",
                "city": "Austin, TX",
                "country_code": "US",
                "timezone": "America/Chicago",
            }

        monkeypatch.setattr(geocoding, "ip_geolocate", degenerate_then_good)
        calls = self._stub_update_env_location(monkeypatch)
        setup_server._resolve_location_from_ip()

        assert len(attempts) == 2, "degenerate response must trigger retry"
        assert len(calls) == 1


# ── _build_setup_html regression (T9 — EPIC litclock-dev#383 hotspot HTML shape) ──


class TestSetupHtmlPivotShape:
    """T9: the hotspot HTML must contain NO Location, Timezone, Units, or
    Content sections. The pivot ripped them out; this guard pins that they
    don't sneak back in via a future merge."""

    # All assertions below use uniquely-identifying tokens (form input names,
    # element IDs, function names) rather than English words so adding new
    # body copy mentioning "latitude" or "celsius" doesn't false-trigger.

    def test_no_location_section(self, monkeypatch):
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", False)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", None)
        html = setup_server._build_setup_html()
        assert 'name="location_query"' not in html
        assert 'name="latitude"' not in html
        assert 'name="longitude"' not in html
        assert 'id="lat-input"' not in html
        assert 'id="lon-input"' not in html
        assert 'id="location-preview"' not in html
        assert 'id="manual-lat"' not in html
        assert 'id="manual-lon"' not in html
        assert 'id="gps-btn"' not in html
        assert "getLocation(" not in html

    def test_no_timezone_section(self, monkeypatch):
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", False)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", None)
        html = setup_server._build_setup_html()
        assert 'name="timezone"' not in html
        assert 'id="timezone-select"' not in html
        assert 'id="tz-detected"' not in html
        assert 'id="tz-picker"' not in html
        assert 'id="tz-hidden"' not in html
        assert "detectTimezone(" not in html
        assert "showTzPicker(" not in html

    def test_no_temperature_units_section(self, monkeypatch):
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", False)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", None)
        html = setup_server._build_setup_html()
        assert 'name="units"' not in html
        assert 'value="imperial"' not in html
        assert 'value="metric"' not in html

    def test_no_content_nsfw_section(self, monkeypatch):
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", False)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", None)
        html = setup_server._build_setup_html()
        assert 'name="allow_nsfw"' not in html
        assert 'value="true"' not in html  # the NSFW checkbox was the only value="true" on the form

    def test_form_still_has_wifi_section_in_provisioning_mode(self, monkeypatch):
        """Sanity: the WiFi section is intact under PROVISIONING_MODE."""
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", False)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", None)
        html = setup_server._build_setup_html()
        assert 'name="wifi_ssid"' in html
        assert 'name="wifi_password"' in html

    def test_geocode_endpoint_no_longer_dispatched(self, monkeypatch):
        """T1: /geocode is removed from is_app_path. The dispatcher tuple
        is the canonical list of app routes; this guards against a future
        partial re-introduction."""
        # We check the source line directly — the tuple is a constant.
        import inspect

        src = inspect.getsource(setup_server.SetupHandler.do_GET)
        assert "/geocode" not in src, "/geocode endpoint should be removed in EPIC litclock-dev#383"


# ── WiFi scan caching ─────────────────────────────────────────────


class TestWifiScanCaching:
    def setup_method(self):
        # Reset cache state before each test
        setup_server._WIFI_SCAN_NETWORKS = None
        setup_server._WIFI_SCAN_TIME = 0
        setup_server._WIFI_SCAN_SSIDS = frozenset()

    def test_cache_returns_cached_result(self, monkeypatch):
        """Second call within TTL returns cached result without scanning."""
        scan_count = []
        fake_networks = [{"ssid": "TestNet", "signal": 80, "security": "WPA2"}]

        import sys
        from unittest.mock import MagicMock

        mock_wifi = MagicMock()
        mock_wifi.scan_wifi_networks = lambda: (scan_count.append(1), fake_networks)[1]
        monkeypatch.setitem(sys.modules, "wifi_provision", mock_wifi)
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)

        result1, empty1 = setup_server._wifi_network_options()
        result2, empty2 = setup_server._wifi_network_options()

        assert result1 == result2
        assert (empty1, empty2) == (False, False)
        assert len(scan_count) == 1  # Only scanned once
        assert "TestNet" in result1

    def test_cache_expires_after_ttl(self, monkeypatch):
        """Cache expires after TTL, triggering a fresh scan."""
        import time

        scan_count = []
        fake_networks = [{"ssid": "TestNet", "signal": 80, "security": "WPA2"}]

        import sys
        from unittest.mock import MagicMock

        mock_wifi = MagicMock()
        mock_wifi.scan_wifi_networks = lambda: (scan_count.append(1), fake_networks)[1]
        monkeypatch.setitem(sys.modules, "wifi_provision", mock_wifi)

        # First call populates cache
        setup_server._wifi_network_options()
        assert len(scan_count) == 1

        # Expire the cache by backdating the timestamp
        setup_server._WIFI_SCAN_TIME = time.monotonic() - setup_server._WIFI_SCAN_TTL - 1

        # Second call should scan again
        setup_server._wifi_network_options()
        assert len(scan_count) == 2

    def test_empty_results_not_cached(self, monkeypatch):
        """Empty scan results are not cached so next call retries."""
        import sys
        from unittest.mock import MagicMock

        mock_wifi = MagicMock()
        mock_wifi.scan_wifi_networks = lambda: []
        monkeypatch.setitem(sys.modules, "wifi_provision", mock_wifi)

        result, was_empty = setup_server._wifi_network_options()
        assert "No networks found" in result
        assert was_empty is True
        assert setup_server._WIFI_SCAN_NETWORKS is None

    def test_page_build_empty_scan_preserves_scan_evidence(self, monkeypatch):
        """The item-7 decision, pinned at the SECOND scan site (/review on
        litclock-dev#614): the /scan-wifi handler test alone would stay green if this
        function's empty branch started clearing _WIFI_SCAN_SSIDS — flipping
        a previously-seen typed SSID to a permanent `hidden yes` profile."""
        import sys
        from unittest.mock import MagicMock

        setup_server._WIFI_SCAN_SSIDS = frozenset({"homewifi"})
        mock_wifi = MagicMock()
        mock_wifi.scan_wifi_networks = lambda: []
        monkeypatch.setitem(sys.modules, "wifi_provision", mock_wifi)

        result, was_empty = setup_server._wifi_network_options()
        assert was_empty is True
        assert setup_server._WIFI_SCAN_SSIDS == frozenset({"homewifi"})

    def test_scan_exception_returns_empty_flag_and_touches_nothing(self, monkeypatch):
        """The except branch (nmcli dying) merges into the empty path today;
        pin that it stays flagged empty and leaves cache + evidence alone
        (/review on litclock-dev#614 — this branch had no direct test)."""
        import sys
        from unittest.mock import MagicMock

        setup_server._WIFI_SCAN_SSIDS = frozenset({"homewifi"})
        mock_wifi = MagicMock()

        def boom():
            raise RuntimeError("nmcli died")

        mock_wifi.scan_wifi_networks = boom
        monkeypatch.setitem(sys.modules, "wifi_provision", mock_wifi)

        result, was_empty = setup_server._wifi_network_options()
        assert was_empty is True
        assert setup_server._WIFI_SCAN_NETWORKS is None
        assert setup_server._WIFI_SCAN_SSIDS == frozenset({"homewifi"})

    def test_concurrent_page_builds_scan_once_under_the_lock(self, monkeypatch):
        """litclock-dev#615: _wifi_network_options must serialize scans under
        _SCAN_CACHE_LOCK like /scan-wifi does. Two concurrent GET / builds at a
        cold cache previously each fired their own nmcli rescan on the single
        hotspot radio. With the lock, the second thread blocks, finds the fresh
        cache on the recheck, and skips its scan — exactly one scan total.

        The test forces overlap: both threads rendezvous at a barrier, then the
        scan blocks briefly so the second thread is guaranteed to be waiting on
        the lock (or the cache-hit recheck) while the first is mid-scan. Without
        the lock this reliably records 2 scans; with it, 1.
        """
        import sys
        import threading
        import time
        from unittest.mock import MagicMock

        scan_count = []
        count_lock = threading.Lock()
        start = threading.Barrier(2)

        def slow_scan():
            # Each scan returns a DISTINCT SSID so the equality assertion below
            # is meaningful: with the lock the waiter serves thread A's cached
            # snapshot (identical results); a lockless mutant scans a second
            # time and returns a different SSID (results differ AND count == 2).
            with count_lock:
                n = len(scan_count)
                scan_count.append(1)
            time.sleep(0.2)  # hold the radio long enough for the other thread to pile up
            return [{"ssid": f"TestNet{n}", "signal": 80, "security": "WPA2"}]

        mock_wifi = MagicMock()
        mock_wifi.scan_wifi_networks = slow_scan
        monkeypatch.setitem(sys.modules, "wifi_provision", mock_wifi)
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)

        results = []
        res_lock = threading.Lock()

        def worker():
            start.wait()
            r, _ = setup_server._wifi_network_options()
            with res_lock:
                results.append(r)

        # daemon=True so a genuine deadlock (thread stuck on the lock) surfaces as
        # a test failure via the is_alive assert instead of hanging interpreter exit.
        threads = [threading.Thread(target=worker, daemon=True) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert all(not t.is_alive() for t in threads), "a page build deadlocked on the scan lock"
        assert len(scan_count) == 1, f"expected one serialized scan, got {len(scan_count)}"
        assert len(results) == 2
        assert "TestNet0" in results[0] and "TestNet0" in results[1], (
            "the waiter did not serve the first scan's cached snapshot"
        )

    def test_own_hotspot_ssid_filtered_out(self, monkeypatch):
        """The clock's own setup-hotspot SSID is never offered as a join target.

        Regression: a non-technical tester picked "LitClock-Setup" off the
        dropdown during first-boot QA, since the hotspot can show up in a scan
        of its own radio. We must filter it out by exact SSID match.
        """
        import sys
        from unittest.mock import MagicMock

        fake_networks = [
            {"ssid": "LitClock-Setup", "signal": 90, "security": ""},
            {"ssid": "HomeWiFi", "signal": 75, "security": "WPA2"},
        ]
        mock_wifi = MagicMock()
        mock_wifi.scan_wifi_networks = lambda: fake_networks
        monkeypatch.setitem(sys.modules, "wifi_provision", mock_wifi)
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "HOTSPOT_SSID", "LitClock-Setup")

        result, was_empty = setup_server._wifi_network_options()
        assert "LitClock-Setup" not in result
        assert "HomeWiFi" in result
        assert was_empty is False

    def test_no_filter_when_hotspot_ssid_unset(self, monkeypatch):
        """In normal (non-provisioning) mode HOTSPOT_SSID is None — filter is a
        no-op so a real network that happens to match nothing is preserved."""
        import sys
        from unittest.mock import MagicMock

        fake_networks = [{"ssid": "LitClock-Setup", "signal": 90, "security": ""}]
        mock_wifi = MagicMock()
        mock_wifi.scan_wifi_networks = lambda: fake_networks
        monkeypatch.setitem(sys.modules, "wifi_provision", mock_wifi)
        monkeypatch.setattr(setup_server, "HOTSPOT_SSID", None)

        result, _ = setup_server._wifi_network_options()
        assert "LitClock-Setup" in result

    def test_filter_emptying_list_returns_no_networks_uncached(self, monkeypatch):
        """If the hotspot is the only visible network (common on first boot —
        the clock's own AP is often the strongest SSID its radio sees),
        filtering empties the list and we must fall into the uncached
        'try refreshing' path, not cache an empty result."""
        import sys
        from unittest.mock import MagicMock

        mock_wifi = MagicMock()
        mock_wifi.scan_wifi_networks = lambda: [{"ssid": "LitClock-Setup", "signal": 95, "security": ""}]
        monkeypatch.setitem(sys.modules, "wifi_provision", mock_wifi)
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "HOTSPOT_SSID", "LitClock-Setup")

        result, was_empty = setup_server._wifi_network_options()
        assert "No networks found" in result
        assert "LitClock-Setup" not in result
        assert was_empty is True
        assert setup_server._WIFI_SCAN_NETWORKS is None

    def test_scan_wifi_endpoint_filters_own_hotspot(self, monkeypatch):
        """The /scan-wifi handler must filter the hotspot from BOTH the JSON it
        returns (the client rebuilds the dropdown from it on Refresh) AND the
        shared _WIFI_SCAN_NETWORKS it populates (feeds the server-rendered
        dropdown). Regression: filtering only _wifi_network_options left
        /scan-wifi as a bypass that re-exposed the hotspot via Refresh or a
        warm cache."""
        import sys
        from unittest.mock import MagicMock

        fake_networks = [
            {"ssid": "LitClock-Setup", "signal": 95, "security": ""},
            {"ssid": "HomeWiFi", "signal": 70, "security": "WPA2"},
        ]
        mock_wifi = MagicMock()
        mock_wifi.scan_wifi_networks = lambda: fake_networks
        monkeypatch.setitem(sys.modules, "wifi_provision", mock_wifi)
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "HOTSPOT_SSID", "LitClock-Setup")

        handler = _make_handler()
        handler.path = "/scan-wifi"
        handler.headers = {"Host": "litclock.setup"}
        sent = {}
        handler.send_json = lambda payload: sent.update(networks=payload)
        handler.do_GET()

        ssids = [n["ssid"] for n in sent["networks"]]
        assert "LitClock-Setup" not in ssids
        assert "HomeWiFi" in ssids
        # Cache feeding the server-rendered dropdown must also exclude it.
        assert setup_server._WIFI_SCAN_NETWORKS is not None
        cached_ssids = [n["ssid"] for n in setup_server._WIFI_SCAN_NETWORKS]
        assert "LitClock-Setup" not in cached_ssids
        assert "HomeWiFi" in cached_ssids

    def test_scan_wifi_endpoint_updates_scan_evidence(self, monkeypatch):
        """litclock-dev#605 item 7: _WIFI_SCAN_SSIDS was only ever asserted
        by monkeypatching it directly — nothing pinned that the /scan-wifi
        HANDLER feeds it. It must: the wifi_hidden decision in do_POST reads
        this set, and a Refresh tap is how a user proves a typed name is
        visible."""
        import sys
        from unittest.mock import MagicMock

        mock_wifi = MagicMock()
        mock_wifi.scan_wifi_networks = lambda: [{"ssid": "HomeWiFi", "signal": 70, "security": "WPA2"}]
        monkeypatch.setitem(sys.modules, "wifi_provision", mock_wifi)
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)

        handler = _make_handler()
        handler.path = "/scan-wifi"
        handler.headers = {"Host": "litclock.setup"}
        handler.send_json = lambda payload: None
        handler.do_GET()

        assert "homewifi" in setup_server._WIFI_SCAN_SSIDS

    def test_empty_rescan_preserves_scan_evidence(self, monkeypatch):
        """Pins the litclock-dev#605 item 7 decision: an empty rescan must
        NOT clear _WIFI_SCAN_SSIDS. Evidence accumulated from non-empty scans
        stays valid across empty rescans — clearing it would flip a typed,
        previously-seen name to `hidden yes`, which nmcli writes into the saved
        profile as a PERMANENT probe-request leak. Stale visibility evidence is
        transient; the leak is not."""
        import sys
        from unittest.mock import MagicMock

        setup_server._WIFI_SCAN_SSIDS = frozenset({"homewifi"})
        mock_wifi = MagicMock()
        mock_wifi.scan_wifi_networks = lambda: []
        monkeypatch.setitem(sys.modules, "wifi_provision", mock_wifi)
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)

        handler = _make_handler()
        handler.path = "/scan-wifi"
        handler.headers = {"Host": "litclock.setup"}
        sent = {}
        handler.send_json = lambda payload: sent.update(networks=payload)
        handler.do_GET()

        assert sent["networks"] == []
        assert setup_server._WIFI_SCAN_SSIDS == frozenset({"homewifi"})
        # Empty results are also never cached — the next call retries.
        assert setup_server._WIFI_SCAN_NETWORKS is None

    def test_non_empty_scan_unions_into_evidence_not_replaces(self, monkeypatch):
        """litclock-dev#615: scan evidence accumulates (UNION), it does not
        replace. A network genuinely visible earlier this session can be absent
        from a later scan (radio flicker); a replace would drop it and flip a
        typed, previously-seen name to a permanent `hidden yes` probe leak. The
        later scan's SSID must be ADDED, and the earlier one must SURVIVE."""
        import sys
        from unittest.mock import MagicMock

        # 'homewifi' was seen in an earlier scan; this scan sees only 'neighbor'.
        setup_server._WIFI_SCAN_SSIDS = frozenset({"homewifi"})
        mock_wifi = MagicMock()
        mock_wifi.scan_wifi_networks = lambda: [{"ssid": "Neighbor", "signal": 60, "security": "WPA2"}]
        monkeypatch.setitem(sys.modules, "wifi_provision", mock_wifi)
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)

        handler = _make_handler()
        handler.path = "/scan-wifi"
        handler.headers = {"Host": "litclock.setup"}
        handler.send_json = lambda payload: None
        handler.do_GET()

        # Union: the flickered-away 'homewifi' survives, 'neighbor' is added.
        assert setup_server._WIFI_SCAN_SSIDS == frozenset({"homewifi", "neighbor"})

    def test_page_build_scan_unions_into_evidence(self, monkeypatch):
        """The union write in the OTHER scan site (_wifi_network_options / the
        page build) must accumulate too, not just the /scan-wifi handler."""
        import sys
        from unittest.mock import MagicMock

        setup_server._WIFI_SCAN_NETWORKS = None
        setup_server._WIFI_SCAN_TIME = 0
        setup_server._WIFI_SCAN_SSIDS = frozenset({"homewifi"})
        mock_wifi = MagicMock()
        mock_wifi.scan_wifi_networks = lambda: [{"ssid": "Neighbor", "signal": 60, "security": "WPA2"}]
        monkeypatch.setitem(sys.modules, "wifi_provision", mock_wifi)
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)

        setup_server._wifi_network_options()
        assert setup_server._WIFI_SCAN_SSIDS == frozenset({"homewifi", "neighbor"})


# ── HTML_ERROR template ────────────────────────────────────────────


class TestHtmlError:
    def test_retry_link_present(self):
        """Error page contains a retry link that works without JS."""
        rendered = setup_server._error_page("Test error")
        assert 'href="/"' in rendered
        assert 'id="retry-link"' in rendered
        assert "Try again" in rendered

    def test_loading_feedback_script(self):
        """Error page includes JS for loading feedback (progressive enhancement)."""
        rendered = setup_server._error_page("Test error")
        assert "Loading..." in rendered
        assert "addEventListener" in rendered


# ── Captive portal bridge (iOS CNA) ────────────────────────────────


class TestCnaBridge:
    def test_bridge_contains_setup_link(self):
        """Bridge CTA links into the real setup form via the raw gateway IP
        (litclock-dev#526: no DNS dependency inside the CNA sheet); the
        hostname stays available in the small print for humans."""
        html = setup_server._build_cna_bridge_html()
        assert f'href="http://{setup_server.HOTSPOT_GATEWAY_IP}/setup"' in html
        assert "Open Setup" in html
        assert setup_server.SETUP_HOSTNAME in html

    def test_bridge_has_no_javascript(self):
        """Bridge must be JS-free — iOS CNA's WebView handles JS poorly and
        a JS-heavy response is what made the popup unreliable in the first place."""
        html = setup_server._build_cna_bridge_html()
        assert "<script" not in html.lower()
        assert "onclick" not in html.lower()

    def test_bridge_is_small(self):
        """Bridge must stay small so iOS CNA renders it reliably (~1 KB budget)."""
        html = setup_server._build_cna_bridge_html()
        assert len(html.encode("utf-8")) < 2048

    def test_bridge_does_not_trigger_apple_success_marker(self):
        """iOS CNA's "no captive portal" markers are specifically the
        `<TITLE>Success</TITLE>` and `<BODY>Success</BODY>` exact strings.
        Assert our title is our own, not Apple's. The earlier substring check
        was too strict (would trip on copy like 'Successfully connected')."""
        html = setup_server._build_cna_bridge_html()
        assert "<title>LitClock Setup</title>" in html
        assert "<TITLE>Success</TITLE>" not in html
        assert "<BODY>Success</BODY>" not in html

    def test_bridge_prints_ip_fallback(self):
        """If litclock.setup DNS fails, the user must still have 10.42.0.1
        printed somewhere in the bridge as an escape hatch."""
        html = setup_server._build_cna_bridge_html()
        assert setup_server.HOTSPOT_GATEWAY_IP in html

    def test_bridge_copy_is_browser_generic_not_safari(self):
        """litclock-dev#482: on-screen setup copy must not name Safari (wrong on Android /
        any non-iOS phone) — it should point at the generic 'browser'. Also
        must not quote a specific button label (/review: iOS's actual CNA
        escape button says "Open in Safari", so a quoted "Open in Browser"
        would misdescribe it — keep the instruction generic instead)."""
        html = setup_server._build_cna_bridge_html()
        assert "Safari" not in html
        assert "browser" in html.lower()

    def test_bridge_has_no_wispr_xml(self):
        """litclock-dev#526 /review: the bridge must NOT carry a WISPr XML
        Redirect block. MessageType 100 / ResponseCode 0 solicits a
        smart-client credential POST this server can't answer in valid
        WISPr, and a LoginURL pointing at the page itself is a redirect
        loop for a conforming WISPr state machine."""
        html = setup_server._build_cna_bridge_html()
        assert "WISPAccessGatewayParam" not in html

    def test_setup_hostname_uses_fake_tld(self):
        """Hostname must have a TLD so Safari treats it as a URL, not a search query."""
        assert "." in setup_server.SETUP_HOSTNAME
        assert setup_server.SETUP_HOSTNAME == "litclock.setup"


# ── WiFi error banner ─────────────────────────────────────────────


class TestWifiErrorBanner:
    def test_banner_mentions_rescan_fallback(self, monkeypatch):
        """The red error banner must tell the user what to do if the setup
        page doesn't auto-reload — phones auto-disconnect from the clock's
        setup network during the failed WiFi attempt, so the browser won't
        see the error until they rescan the QR code and rejoin. Without this
        instruction the user is stuck staring at a stale loading page."""
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", False)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", "wrong password")
        html = setup_server._build_setup_html()
        assert "rescan" in html.lower()
        assert "qr" in html.lower()
        assert "own network has restarted" in html.lower()
        # litclock-dev#555: "hotspot" reads as an instruction about the
        # user's OWN phone (iOS Personal Hotspot), i.e. the opposite
        # direction of travel. It must not creep back into user-facing copy.
        assert "hotspot" not in html.lower()

    def test_banner_distinguishes_wifi_from_setup_network_password(self, monkeypatch):
        """The banner must explicitly call out the user's WiFi password vs
        the one printed on the clock, so a user staring at the visible setup
        password doesn't type that one into the WiFi password field.

        EPIC litclock-dev#383 dropped the 'home' qualifier (which was scary jargon for
        non-tech users — issue litclock-dev#384); litclock-dev#555 dropped "hotspot"
        for the same reason. The disambiguation itself is still
        load-bearing copy — only the word for it changed.

        litclock-dev#610 review: this test used to pass a plain string and
        match "wifi password" anywhere on the page — which the form's own
        helper text satisfies, so it pinned nothing about the banner. The
        disambiguation advice now renders only for the bad_password class,
        and the assertions slice the banner region."""
        import wifi_provision

        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", False)
        monkeypatch.setattr(
            setup_server,
            "WIFI_CONNECT_ERROR",
            wifi_provision.WifiFailure("Incorrect WiFi password", wifi_provision.WIFI_FAIL_BAD_PASSWORD),
        )
        html = setup_server._build_setup_html()
        banner_start = html.index("Couldn’t join your WiFi")
        banner_end = html.index("</div>", banner_start)
        banner = html[banner_start:banner_end].lower()
        assert "wifi password" in banner
        assert "password shown on the clock" in banner
        # Regression: the dropped "home" qualifier must not creep back in.
        assert "home wifi" not in html.lower()

    def test_wifi_form_field_label_no_home_qualifier(self, monkeypatch):
        """The WiFi password input label must say 'Your WiFi Password' (no
        'Home' qualifier per litclock-dev#384). The hotspot-vs-WiFi cue lives in the
        error banner, not the field label."""
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", False)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", None)
        html = setup_server._build_setup_html()
        assert "Your WiFi Password" in html
        assert "Home WiFi" not in html


class TestSetupPagePickerCopy:
    """Issue litclock-dev#398: hardware QA with a non-tech user showed they did not
    realize they were supposed to pick their OWN home/office WiFi from
    the dropdown — the page gave no guidance, and "LitClock-Setup" looked
    like a selectable option. The code half (hotspot filter) shipped in
    litclock-dev#397; this class pins the copy half so future polish doesn't regress
    the orienting cues a first-time non-tech user depends on."""

    @pytest.fixture(autouse=True)
    def _clean_wifi_scan_state(self, monkeypatch):
        """Reset module-level WiFi scan state + stub the scanner. Without
        this, `_wifi_network_options()` (called by `_build_setup_html` in
        provisioning mode) reads the shared `_WIFI_SCAN_NETWORKS` global —
        which earlier tests may have populated with stale options — and
        on a cache miss falls through to the real `wifi_provision.
        scan_wifi_networks()`, which calls nmcli (slow / flaky on a dev
        box). Mirrors the pattern in `TestWifiNetworkScan`."""
        import sys
        from unittest.mock import MagicMock

        monkeypatch.setattr(setup_server, "_WIFI_SCAN_NETWORKS", None)
        monkeypatch.setattr(setup_server, "_WIFI_SCAN_TIME", 0)
        mock_wifi = MagicMock()
        mock_wifi.scan_wifi_networks = lambda: [
            {"ssid": "FakeHomeNet", "signal": 75, "security": "WPA2"},
        ]
        monkeypatch.setitem(sys.modules, "wifi_provision", mock_wifi)

    def test_subtitle_orients_provisioning_user_on_wifi(self, monkeypatch):
        """In provisioning mode the subtitle must orient the user on the
        one thing the page does — joining their own WiFi — and explicitly
        anchor on the phone they're holding ("the WiFi your phone normally
        uses") rather than generic "literary clock" filler."""
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", False)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", None)
        html = setup_server._build_setup_html()
        # Anchor on the phone — the strongest disambiguating cue for a
        # non-tech user mid-captive-portal. Pin the FULL subtitle so a
        # future copy edit doesn't accidentally drop the anchor.
        assert "Connect your clock to the WiFi your phone normally uses." in html
        # The pre-pivot "few easy steps" copy implied multiple steps; the
        # page is now one step. Guard against it sneaking back.
        assert "few easy steps" not in html.lower()

    def test_subtitle_shifts_to_joining_register_during_in_flight(self, monkeypatch):
        """While `WIFI_CONNECT_IN_FLIGHT=True` (user already submitted,
        page is auto-refreshing), the subtitle "Connect your clock to
        the WiFi…" reads as a stale instruction stacked above the blue
        "Connecting to WiFi…" banner. Replace with a "joining" register
        so the page reads coherently for the ~30s in-flight window."""
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", True)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", None)
        html = setup_server._build_setup_html()
        # The default provisioning subtitle must NOT show while in-flight.
        assert "Connect your clock to the WiFi" not in html
        # The connecting banner is still there (sanity).
        assert "Connecting to WiFi" in html
        # And the new "joining" register subtitle is present.
        assert "Joining your WiFi" in html

    def test_picker_section_explains_which_wifi(self, monkeypatch):
        """An explainer next to the dropdown must tell the user which
        network this is — explicitly NOT the LitClock-Setup network. The
        hotspot SSID is filtered from the dropdown (litclock-dev#397), but a user
        who doesn't read the explainer might still scan the page looking
        for "LitClock-Setup" and get confused when they don't see it.
        Calling out the hotspot by name turns absence-of-option into
        a deliberate signal."""
        import re

        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", False)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", None)
        # Pin HOTSPOT_SSID to the canonical name so the test doesn't
        # depend on whatever the previous test/run-mode set it to. Also
        # exercises the parameterized-SSID interpolation in the explainer.
        monkeypatch.setattr(setup_server, "HOTSPOT_SSID", "LitClock-Setup")
        html = setup_server._build_setup_html()
        # The explainer must call out LitClock-Setup by name, not just
        # "the setup network" (hand-wavy for a first-time user).
        # Scope the assertion to the WiFi section using the surrounding
        # HTML comments as boundaries (the section has nested <div>s, so
        # a non-greedy </div> regex stops at the wrong close tag).
        start = html.find("<!-- WiFi Section -->")
        end = html.find("<!-- Submit -->", start)
        assert start != -1 and end != -1, "WiFi/Submit markers missing — boundaries unclear"
        section = re.sub(r"\s+", " ", html[start:end])
        # Comma-free anchor. The earlier "Not LitClock-Setup," pinned an
        # appositive that reads at least as naturally as a contrastive
        # ("not A, [but] B") — which turns one network into two, the exact
        # confusion this issue exists to remove.
        assert "Not the LitClock-Setup network" in section, (
            "picker explainer must name the setup network, unambiguously, inside the WiFi section"
        )
        assert "Not LitClock-Setup," not in section, "the comma-appositive form is ambiguous; use a single noun phrase"
        # The "phone normally uses" anchor must repeat near the picker
        # (the subtitle test already covers the subtitle copy of this
        # phrase; here we pin the duplication inside the picker block).
        assert "phone normally uses" in section.lower(), (
            "picker explainer must repeat the 'phone normally uses' anchor near the dropdown, not just in the subtitle"
        )

    def test_picker_explainer_uses_runtime_hotspot_ssid(self, monkeypatch):
        """The hotspot name in the explainer must come from the runtime
        `HOTSPOT_SSID` (set via --hotspot-ssid CLI flag), not a hardcoded
        literal. Branded / customized builds set a different SSID, and
        the disambiguating cue ("not the X hotspot") actively lies if X
        is wrong. Also exercises html.escape on the SSID."""
        import re

        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", False)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", None)
        # A custom SSID that contains an HTML special char to verify
        # escaping. Set via the runtime global the way --hotspot-ssid would.
        monkeypatch.setattr(setup_server, "HOTSPOT_SSID", "Brand & Co Setup")
        html = setup_server._build_setup_html()
        start = html.find("<!-- WiFi Section -->")
        end = html.find("<!-- Submit -->", start)
        assert start != -1 and end != -1
        section = re.sub(r"\s+", " ", html[start:end])
        assert "Not the Brand &amp; Co Setup network" in section, (
            "custom SSID must appear INSIDE the disambiguation sentence, HTML-escaped"
        )
        # And the raw (unescaped) ampersand must NOT leak — that would
        # signal a missing escape.
        assert "Brand & Co Setup" not in section, "raw & in SSID must be HTML-escaped before interpolation"

    def test_password_helper_calls_out_password_disambiguation(self, monkeypatch):
        """The helper under the password input must call out the
        clock's-password-vs-WiFi-password distinction up-front, not just in
        the error banner. The setup password is on the e-ink in front of the
        user; typing it in is the natural first attempt. Wording must align
        with the error banner's "password shown on the clock" so the two
        surfaces don't drift."""
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", False)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", None)
        html = setup_server._build_setup_html()
        # Aligned with the error banner's exact phrasing — both surfaces
        # describe the SAME password to the SAME user; divergent wording
        # ("temporary" vs "setup") would confuse a non-tech user who
        # sees both on a failed-then-retry sequence.
        assert "password shown on the clock" in html.lower()
        # And it must keep the open-network escape hatch (a fraction of
        # users do have unsecured WiFi).
        assert "open networks" in html.lower()


# ── Captive portal routing & response headers ─────────────────────


def _make_handler():
    """Build a SetupHandler without going through BaseHTTPRequestHandler.__init__
    (which requires a real socket). Tests mock the wire-level send_* methods."""
    from unittest.mock import MagicMock

    handler = setup_server.SetupHandler.__new__(setup_server.SetupHandler)
    handler.headers = {}
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()
    return handler


class TestRedirectToSetup:
    def test_redirect_uses_setup_hostname(self):
        """_redirect_to_setup() must 302 to http://litclock.setup/setup —
        the clean hostname without :8080, routed via nftables 80→8080."""
        handler = _make_handler()
        handler._redirect_to_setup()
        handler.send_response.assert_called_once_with(302)
        handler.send_header.assert_called_once_with("Location", f"http://{setup_server.SETUP_HOSTNAME}/setup")
        handler.end_headers.assert_called_once()


class TestCaptivePortalProbeRouting:
    def test_ios_hotspot_detect_serves_bridge(self):
        handler = _make_handler()
        handler.send_html = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        assert handler._handle_captive_portal_probe("/hotspot-detect.html", "captive.apple.com") is True
        sent_html = handler.send_html.call_args[0][0]
        assert "Open Setup" in sent_html
        assert f"http://{setup_server.HOTSPOT_GATEWAY_IP}/setup" in sent_html

    def test_ios_library_test_success_serves_bridge(self):
        handler = _make_handler()
        handler.send_html = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        assert handler._handle_captive_portal_probe("/library/test/success.html", "www.apple.com") is True
        handler.send_html.assert_called_once()
        assert "Open Setup" in handler.send_html.call_args[0][0]

    def test_apple_host_with_root_path_serves_bridge(self):
        """Regression test for the path-`/` hole: Apple CNA probes that hit
        the root path on captive.apple.com must still get the bridge, not the
        full setup HTML. captive_portal.py explicitly notes 'Some phones just
        hit the root'."""
        handler = _make_handler()
        handler.send_html = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        assert handler._handle_captive_portal_probe("/", "captive.apple.com") is True
        handler.send_html.assert_called_once()
        assert "Open Setup" in handler.send_html.call_args[0][0]

    def test_android_probe_path_uses_redirect_not_bridge(self):
        """Android phones probe /generate_204 and expect a 302. They must NOT
        get the iOS-shaped HTML bridge."""
        handler = _make_handler()
        handler.send_html = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        assert handler._handle_captive_portal_probe("/generate_204", "connectivitycheck.gstatic.com") is True
        handler.send_html.assert_not_called()
        handler.send_response.assert_called_once_with(302)

    def test_google_host_fallback_uses_redirect_not_bridge(self):
        """Host-based fallback: non-Apple captive-portal hosts must get a 302,
        not the iOS-shaped HTML. Regression check against an earlier version
        of the fallback that served the bridge to everyone in
        CAPTIVE_PORTAL_HOSTS."""
        handler = _make_handler()
        handler.send_html = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        assert handler._handle_captive_portal_probe("/unknown", "connectivitycheck.gstatic.com") is True
        handler.send_html.assert_not_called()
        handler.send_response.assert_called_once_with(302)

    def test_windows_probe_uses_redirect(self):
        handler = _make_handler()
        handler.send_html = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        assert handler._handle_captive_portal_probe("/connecttest.txt", "www.msftconnecttest.com") is True
        handler.send_html.assert_not_called()
        handler.send_response.assert_called_once_with(302)

    def test_wispr_detection_probe_gets_302_off_apple_host(self):
        """litclock-dev#526: iOS 26.5.x stopped promoting a 200-HTML answer
        served under an Apple hostname to the CNA sheet. The detection probe
        (CaptiveNetworkSupport UA) must get a bodyless 302 to /cna on the
        raw gateway IP — the commercial-hotspot pattern that kept
        auto-popping — with the same no-store posture as send_html (a
        cached captive decision persisting across joins is the known
        failure mode)."""
        handler = _make_handler()
        handler.headers = {"User-Agent": "CaptiveNetworkSupport-514.120.2 wispr"}
        handler.send_html = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        assert handler._handle_captive_portal_probe("/hotspot-detect.html", "captive.apple.com") is True
        handler.send_html.assert_not_called()
        handler.send_response.assert_called_once_with(302)
        headers = dict(c.args for c in handler.send_header.call_args_list)
        assert headers["Location"] == setup_server.CNA_URL
        assert headers["Content-Length"] == "0"
        assert "no-store" in headers["Cache-Control"]
        handler.wfile.write.assert_not_called()

    def test_wispr_detection_probe_302_on_apple_host_fallback(self, capsys, monkeypatch):
        """The host-based fallback (Apple host, unexpected path) must apply
        the same detection-probe split as the path branch, point Location
        at /cna, and log the distinct '-hostmatch' branch label the
        hardware QA playbook decodes."""
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        handler = _make_handler()
        handler.headers = {"User-Agent": "CaptiveNetworkSupport-514.120.2 wispr"}
        handler.send_html = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        assert handler._handle_captive_portal_probe("/", "captive.apple.com") is True
        handler.send_html.assert_not_called()
        handler.send_response.assert_called_once_with(302)
        headers = dict(c.args for c in handler.send_header.call_args_list)
        assert headers["Location"] == setup_server.CNA_URL
        assert "-> cna-302-hostmatch" in capsys.readouterr().out

    def test_bare_apple_host_wispr_probe_intercepted(self):
        """litclock-dev#526 /review (Codex): bare apple.com is in Apple's
        documented probe-host list. Before the host-set expansion, a wispr
        probe to apple.com/ fell through to the FULL setup form under an
        Apple hostname — the exact response shape iOS 26 stopped promoting."""
        handler = _make_handler()
        handler.headers = {"User-Agent": "CaptiveNetworkSupport-514.120.2 wispr"}
        handler.send_html = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        assert handler._handle_captive_portal_probe("/", "apple.com") is True
        handler.send_response.assert_called_once_with(302)

    def test_ua_match_is_case_insensitive(self):
        """The UA gate must not depend on Apple's exact casing."""
        handler = _make_handler()
        handler.headers = {"User-Agent": "captivenetworksupport-999 wispr"}
        handler.send_html = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        assert handler._handle_captive_portal_probe("/hotspot-detect.html", "captive.apple.com") is True
        handler.send_response.assert_called_once_with(302)

    def test_missing_user_agent_gets_bridge_not_302(self):
        """No User-Agent header must be treated as a browser (200 bridge),
        not as the detection probe — documenting the no-UA contract
        explicitly instead of relying on _make_handler defaults."""
        handler = _make_handler()
        handler.headers = {}
        handler.send_html = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        assert handler._handle_captive_portal_probe("/hotspot-detect.html", "captive.apple.com") is True
        handler.send_html.assert_called_once()
        handler.send_response.assert_not_called()

    def test_safari_ua_on_probe_path_still_gets_bridge(self):
        """The CNA sheet's WebView (Safari-family UA) re-fetches the probe
        path when rendering; it must keep getting the 200 bridge, not a
        redirect loop through /cna."""
        handler = _make_handler()
        handler.headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Safari/604.1"
        }
        handler.send_html = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        assert handler._handle_captive_portal_probe("/hotspot-detect.html", "captive.apple.com") is True
        handler.send_html.assert_called_once()
        assert "Open Setup" in handler.send_html.call_args[0][0]

    def test_wispr_probe_log_branch_is_cna_302(self, capsys, monkeypatch):
        """The probe log must distinguish the detection-probe 302 from the
        bridge 200 so the next hardware repro can decode which branch fired."""
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        handler = _make_handler()
        handler.headers = {"User-Agent": "CaptiveNetworkSupport-514.120.2 wispr"}
        handler._handle_captive_portal_probe("/hotspot-detect.html", "captive.apple.com")
        out = capsys.readouterr().out
        assert "-> cna-302" in out
        assert "status=302" in out

    def test_probe_logs_host_ua_path_diagnostic_in_provisioning(self, capsys, monkeypatch):
        """litclock-dev#483: the access log records only the request line + status, so a
        'portal didn't auto-open' phone repro can't tell which host/UA iOS
        probed. In provisioning mode the probe handler must log Host + path +
        User-Agent so the next repro pins exactly what the phone asked for."""
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        handler = _make_handler()
        handler.send_html = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        handler.headers = {"User-Agent": "CaptiveNetworkSupport-355.200.27 wispr"}
        handler._handle_captive_portal_probe("/hotspot-detect.html", "captive.apple.com")
        out = capsys.readouterr().out
        assert "CAPTIVE-PROBE" in out
        assert "captive.apple.com" in out
        assert "/hotspot-detect.html" in out
        assert "CaptiveNetworkSupport" in out

    def test_probe_log_includes_response_branch_status_bytes(self, capsys, monkeypatch):
        """litclock-dev#526: receipt-only logging couldn't distinguish "sheet
        never opened" from "sheet opened and died". The log line must carry
        the response side (branch, status, byte count) and enough UA (200
        chars) to tell the CNA WebView from a manual Safari open."""
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        handler = _make_handler()
        handler.send_html = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        handler.headers = {"User-Agent": "x" * 190}
        handler._handle_captive_portal_probe("/hotspot-detect.html", "captive.apple.com")
        out = capsys.readouterr().out
        assert "-> cna-bridge" in out
        assert "status=200" in out
        assert "bytes=" in out and "bytes=0" not in out
        assert "x" * 150 in out  # UA no longer truncated at 80 chars

    def test_probe_diagnostic_silent_outside_provisioning(self, capsys, monkeypatch):
        """The diagnostic is provisioning-only. (Defense-in-depth pin: since
        litclock-dev#715 no production server runs with the flag unset, but
        the gate must not spam stdout — or touch self.headers — if one ever
        does.)"""
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", False)
        handler = _make_handler()
        handler.send_html = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        handler._handle_captive_portal_probe("/hotspot-detect.html", "captive.apple.com")
        assert "CAPTIVE-PROBE" not in capsys.readouterr().out

    def test_unrelated_host_and_path_returns_false(self):
        """Host not in CAPTIVE_PORTAL_HOSTS and path not a known probe — the
        handler should not match; the caller falls through to the real routes."""
        handler = _make_handler()
        handler.send_html = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        assert handler._handle_captive_portal_probe("/some/unknown/thing", "10.42.0.1") is False
        handler.send_html.assert_not_called()


class TestSendHtmlCacheControl:
    def _collect_headers(self, handler):
        return {call.args[0].lower(): call.args[1] for call in handler.send_header.call_args_list}

    def test_send_html_emits_no_cache_headers(self):
        """iOS CNA WebView and Safari aggressively cache captive-portal
        responses — a stale bridge from a prior boot would re-trigger the
        exact bug this PR fixes. Every HTML response must be marked
        no-store/no-cache/must-revalidate."""
        handler = _make_handler()
        handler.send_html("<html>test</html>")
        headers = self._collect_headers(handler)
        assert "cache-control" in headers
        cc = headers["cache-control"].lower()
        assert "no-store" in cc
        assert "no-cache" in cc
        assert "must-revalidate" in cc

    def test_send_html_emits_pragma_no_cache(self):
        """Older Safari / WebKit CNA WebViews fall back to Pragma when they
        don't understand modern Cache-Control directives. Pragma must be set
        alongside Cache-Control, or iOS 13- and some captive-portal sheets
        will still cache the response."""
        handler = _make_handler()
        handler.send_html("<html>test</html>")
        headers = self._collect_headers(handler)
        assert headers.get("pragma", "").lower() == "no-cache"

    def test_send_html_emits_expires_zero(self):
        """Belt-and-suspenders: Expires: 0 forces immediate expiration in
        any cache layer that ignores both Cache-Control and Pragma."""
        handler = _make_handler()
        handler.send_html("<html>test</html>")
        headers = self._collect_headers(handler)
        assert headers.get("expires") == "0"


# ── do_GET dispatch (the path-`/` hole fix) ────────────────────────


def _make_do_get_handler(path, host):
    """Build a SetupHandler for do_GET-level tests. urllib.parse.urlparse
    reads self.path directly; self.headers.get('Host') is read via the
    standard headers interface. Mock both, plus the wire-level methods."""
    from unittest.mock import MagicMock

    handler = setup_server.SetupHandler.__new__(setup_server.SetupHandler)
    handler.path = path
    headers_mock = MagicMock()
    headers_mock.get = lambda key, default="": {"Host": host}.get(key, default)
    handler.headers = headers_mock
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()
    handler.send_html = MagicMock()
    handler.send_json = MagicMock()
    return handler


class TestDoGetCaptivePortalDispatch:
    """The probe-handler unit tests above cover the handler in isolation.
    These tests exercise do_GET end-to-end: the new host-based dispatch
    (is_probe_host OR not is_app_path) must route correctly for every
    combination of path and Host header that matters."""

    def test_root_path_with_apple_host_serves_bridge(self, monkeypatch):
        """The path-`/` hole regression test at the do_GET level. A request
        to captive.apple.com/ with PROVISIONING_MODE must land on the bridge,
        NOT on the full setup HTML. Before the fix, path='/' was in the
        exclusion tuple and the probe handler never fired."""
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        handler = _make_do_get_handler("/", "captive.apple.com")
        handler.do_GET()
        handler.send_html.assert_called_once()
        sent = handler.send_html.call_args[0][0]
        assert "Open Setup" in sent, "apple host + root path must serve the CNA bridge"
        assert "scan-wifi" not in sent, "must NOT serve the full JS-heavy setup form"

    def test_root_path_with_gateway_host_serves_full_setup_form(self, monkeypatch):
        """Direct visit from the hotspot IP (or litclock.setup) with no
        probe-host signal should land on the real setup form, not the
        bridge. This is the 95% case — user typing the fallback URL in Safari."""
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", False)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", None)
        handler = _make_do_get_handler("/", "10.42.0.1")
        handler.do_GET()
        handler.send_html.assert_called_once()
        sent = handler.send_html.call_args[0][0]
        # The real setup form has the WiFi dropdown — the bridge does not
        assert "Your WiFi Password" in sent, "gateway host + / must serve real setup form"

    def test_probe_path_with_gateway_host_still_intercepted(self, monkeypatch):
        """Even from the gateway IP, a known probe path should be intercepted.
        (is_probe_host=False but is_app_path=False → probe handler fires.)"""
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        handler = _make_do_get_handler("/hotspot-detect.html", "10.42.0.1")
        handler.do_GET()
        handler.send_html.assert_called_once()
        assert "Open Setup" in handler.send_html.call_args[0][0]

    def test_android_probe_host_with_unknown_path_gets_302(self, monkeypatch):
        """Android probe host with an unrecognized path should route through
        the probe handler (because is_probe_host=True) and then hit the
        host-based fallback, which 302s non-Apple hosts to the setup URL."""
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        handler = _make_do_get_handler("/weird/path", "connectivitycheck.gstatic.com")
        handler.do_GET()
        handler.send_html.assert_not_called()
        handler.send_response.assert_called_with(302)

    def test_cna_path_on_gateway_host_serves_bridge(self, monkeypatch):
        """/cna is the detection-probe 302 target (litclock-dev#526): the
        bridge on our own host. It must be dispatched as an app path — not
        eaten by the probe handler — and serve the bridge, not the setup form."""
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        handler = _make_do_get_handler("/cna", "10.42.0.1")
        handler.do_GET()
        handler.send_html.assert_called_once()
        sent = handler.send_html.call_args[0][0]
        assert "Open Setup" in sent
        assert "scan-wifi" not in sent

    def test_cna_path_with_wispr_ua_terminates_redirect_chain(self, monkeypatch):
        """The wispr client following the cna-302 Location must land on a
        200 bridge, never another 302 — a redirect loop here means the CNA
        sheet never opens, and CI is the only guard (iOS captive behavior
        can't be simulated). Pins the loop-termination invariant of litclock-dev#526."""
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        handler = _make_do_get_handler("/cna", "10.42.0.1")
        ua = "CaptiveNetworkSupport-514.120.2 wispr"
        handler.headers.get = lambda k, d="": {"Host": "10.42.0.1", "User-Agent": ua}.get(k, d)
        handler.do_GET()
        handler.send_html.assert_called_once()
        assert "Open Setup" in handler.send_html.call_args[0][0]
        assert 302 not in [c.args[0] for c in handler.send_response.call_args_list]

    def test_cna_path_404_in_normal_mode(self, monkeypatch):
        """/cna is provisioning-only: outside provisioning there is no
        hotspot and the bridge would be a dead end, so the flag-off gate
        404s it like any other unknown path (defense-in-depth pin,
        litclock-dev#715)."""
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", False)
        handler = _make_do_get_handler("/cna", "192.168.1.50")
        handler.do_GET()
        handler.send_html.assert_not_called()
        handler.send_response.assert_called_once_with(404)

    def test_unknown_path_wispr_ua_catchall_redirects_to_cna(self, monkeypatch):
        """A detection probe from a host/path outside the known sets must
        still get the DNS-free /cna redirect from the provisioning
        catch-all, not the litclock.setup Location whose DNS-dependent
        shape is what iOS 26 distrusts (litclock-dev#526 /review F5)."""
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        handler = _make_do_get_handler("/some-future-probe", "netcts.cdn-apple.com")
        ua = "CaptiveNetworkSupport-514.120.2 wispr"
        handler.headers.get = lambda k, d="": {"Host": "netcts.cdn-apple.com", "User-Agent": ua}.get(k, d)
        handler.do_GET()
        handler.send_response.assert_called_with(302)
        headers = dict(c.args for c in handler.send_header.call_args_list)
        assert headers["Location"] == setup_server.CNA_URL

    def test_provisioning_mode_off_skips_probe_dispatch(self, monkeypatch):
        """Defense-in-depth gate pin: with the flag unset, probe dispatch
        must not fire. No production server runs with PROVISIONING_MODE
        False since litclock-dev#715 (no-flag mode exits 1), but the guard
        stays and this pins it."""
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", False)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", False)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", None)
        handler = _make_do_get_handler("/", "captive.apple.com")
        handler.do_GET()
        handler.send_html.assert_called_once()
        sent = handler.send_html.call_args[0][0]
        # The bridge must NOT appear when the gate is off
        assert "Open Setup" not in sent, "normal mode must not serve the bridge even to probe hosts"


# ── _restore_hotspot retry display hook ────────────────────────────


class TestRestoreHotspot:
    """The retry e-ink display refresh is the user's primary signal during
    a failed-WiFi-password retry. These tests pin the behavior so a future
    refactor doesn't silently regress the UX."""

    def _mock_eink(self, monkeypatch):
        import sys
        from unittest.mock import MagicMock

        mock_eink = MagicMock()
        mock_eink.HOTSPOT_RETRY_WIFI_PASSWORD = "wifi_password"
        mock_eink.HOTSPOT_RETRY_CONNECT_FAILED = "connect_failed"
        monkeypatch.setitem(sys.modules, "eink_display", mock_eink)
        return mock_eink

    def test_restore_hotspot_bad_password_selects_the_password_variant(self, monkeypatch):
        """litclock-dev#603: the wrong-password steps render only when the
        failure actually WAS the password."""
        from unittest.mock import MagicMock

        monkeypatch.setattr(setup_server, "HOTSPOT_SSID", "LitClock-Setup")
        monkeypatch.setattr(setup_server, "HOTSPOT_PASSWORD", "abc12345")

        create_hotspot = MagicMock(return_value={"ssid": "x", "password": "y", "ip": "z"})
        mock_eink = self._mock_eink(monkeypatch)

        setup_server._restore_hotspot(create_hotspot, failure_class="bad_password")

        create_hotspot.assert_called_once_with(ssid="LitClock-Setup", password="abc12345")
        mock_eink.display_hotspot_info.assert_called_once()
        kwargs = mock_eink.display_hotspot_info.call_args.kwargs
        assert kwargs.get("retry_reason") == "wifi_password"

    @pytest.mark.parametrize("failure_class", ["timeout", "not_found", "other", "no_ip", None])
    def test_restore_hotspot_non_password_failures_select_the_neutral_variant(self, monkeypatch, failure_class):
        """litclock-dev#603 — the bug this closes: a timeout painted
        wrong-password guidance. Every non-password class (and the
        no-evidence None) gets the class-neutral connect-failed steps;
        telling a user to retype a password that was never the problem
        sends them fixing the one thing known-least-likely broken."""
        from unittest.mock import MagicMock

        monkeypatch.setattr(setup_server, "HOTSPOT_SSID", "LitClock-Setup")
        monkeypatch.setattr(setup_server, "HOTSPOT_PASSWORD", "abc12345")

        create_hotspot = MagicMock(return_value={"ssid": "x", "password": "y", "ip": "z"})
        mock_eink = self._mock_eink(monkeypatch)

        setup_server._restore_hotspot(create_hotspot, failure_class=failure_class)

        kwargs = mock_eink.display_hotspot_info.call_args.kwargs
        assert kwargs.get("retry_reason") == "connect_failed"

    def test_bad_password_literal_is_in_lockstep_with_wifi_provision(self):
        """_restore_hotspot and the banner compare against the literal
        'bad_password' (a lazy constant import would be swallowed by the
        ImportError arm under the fake wifi_provision the retry tests
        inject). This pins the literal to the real constant so they can't
        drift apart."""
        import wifi_provision

        assert wifi_provision.WIFI_FAIL_BAD_PASSWORD == "bad_password"

    def test_restore_hotspot_swallows_eink_display_failure(self, monkeypatch):
        """If the e-ink display call raises (dev machine without hardware,
        SPI busy, waveshare driver crash), _restore_hotspot must still
        return cleanly — the hotspot stays up even though the retry e-ink
        screen is missing. The retry UX is nice-to-have, but a broken
        display must never take down the hotspot retry loop."""
        import sys
        from unittest.mock import MagicMock

        monkeypatch.setattr(setup_server, "HOTSPOT_SSID", "LitClock-Setup")
        monkeypatch.setattr(setup_server, "HOTSPOT_PASSWORD", "abc12345")

        mock_eink = MagicMock()
        mock_eink.HOTSPOT_RETRY_WIFI_PASSWORD = "wifi_password"
        mock_eink.display_hotspot_info.side_effect = RuntimeError("SPI busy")
        monkeypatch.setitem(sys.modules, "eink_display", mock_eink)

        create_hotspot = MagicMock(return_value={"ssid": "x", "password": "y"})
        # Must return cleanly — must not propagate the RuntimeError
        setup_server._restore_hotspot(create_hotspot)
        create_hotspot.assert_called_once()
        mock_eink.display_hotspot_info.assert_called_once()

    def test_restore_hotspot_gives_up_after_three_failures(self, monkeypatch):
        """If create_hotspot fails all 3 attempts, display_hotspot_info must
        NOT be called. Prevents a spurious retry screen when the hotspot
        isn't actually back up."""
        import sys
        from unittest.mock import MagicMock

        monkeypatch.setattr(setup_server, "HOTSPOT_SSID", "LitClock-Setup")
        monkeypatch.setattr(setup_server, "HOTSPOT_PASSWORD", "abc12345")
        # time.sleep between attempts — skip the 2s delay in tests
        import time

        monkeypatch.setattr(time, "sleep", lambda _s: None)

        create_hotspot = MagicMock(return_value=None)  # all 3 attempts fail

        mock_eink = MagicMock()
        monkeypatch.setitem(sys.modules, "eink_display", mock_eink)

        setup_server._restore_hotspot(create_hotspot)

        assert create_hotspot.call_count == 3
        mock_eink.display_hotspot_info.assert_not_called()

    def test_restore_hotspot_missing_credentials_returns_early(self, monkeypatch):
        """If HOTSPOT_SSID/HOTSPOT_PASSWORD weren't captured at startup,
        _restore_hotspot bails before calling create_hotspot."""
        from unittest.mock import MagicMock

        monkeypatch.setattr(setup_server, "HOTSPOT_SSID", None)
        monkeypatch.setattr(setup_server, "HOTSPOT_PASSWORD", None)

        create_hotspot = MagicMock()
        setup_server._restore_hotspot(create_hotspot)

        create_hotspot.assert_not_called()


# ── _schedule_self_terminate helper (litclock-dev#364) ──────────────────────────


class TestScheduleSelfTerminate:
    """Unit tests for the SIGTERM-scheduling helper introduced in litclock-dev#364.

    Centralizes the daemon-thread + os.kill pattern so the no-WiFi branch
    and the WiFi-connect success path share one tested implementation
    instead of each carrying its own bare ``os.kill`` call.
    """

    def test_signals_synchronously_from_calling_thread(self, monkeypatch):
        """Signals from the calling thread — no new thread spawn (the
        delay>0 daemon arm was removed with its only caller, litclock-dev#715).

        Use the Thread mock pattern (codex C6) rather than
        threading.active_count(): the suite has other daemon threads in
        flight and a count-based assertion would be flaky.
        """
        import os
        import signal as signal_mod
        from unittest.mock import patch

        kill_calls = []
        monkeypatch.setattr("os.kill", lambda pid, sig: kill_calls.append((pid, sig)))

        with patch("setup_server.threading.Thread") as mock_thread:
            setup_server._schedule_self_terminate()
            mock_thread.assert_not_called()

        assert kill_calls == [(os.getpid(), signal_mod.SIGTERM)]

    def test_signals_current_pid_and_sigterm(self, monkeypatch):
        """Signal target is (getpid(), SIGTERM)."""
        import os
        import signal as signal_mod
        import threading

        kill_event = threading.Event()
        kill_calls = []

        def mock_kill(pid, sig):
            kill_calls.append((pid, sig))
            kill_event.set()

        monkeypatch.setattr("os.kill", mock_kill)

        setup_server._schedule_self_terminate()
        assert kill_event.is_set()
        assert kill_calls[-1] == (os.getpid(), signal_mod.SIGTERM)
        assert kill_calls[-1] == (os.getpid(), signal_mod.SIGTERM)


# ── signal_completion bool return (litclock-dev#364 D4, codex post-review fix) ──


class TestSignalCompletionReturnsBool:
    """D4 (revised after codex review): signal_completion is best-effort and
    returns True/False instead of raising. A raise from this function would
    break two load-bearing call-site invariants:

    1. No-WiFi branch in do_POST — runs AFTER the HTTP success response is
       written. BaseHTTPRequestHandler cannot convert a raise into a 500
       once headers/body are flushed; a raise here would silently skip
       _schedule_self_terminate and leave the server hanging.

    2. WiFi-success branch — runs AFTER teardown_hotspot. A raise landing
       in the existing except branch would call _restore_hotspot on the
       same wlan0 we just joined to the user's WiFi, destroying the
       working connection.

    Bool return + explicit check at each call site avoids both regressions.
    """

    def test_signal_completion_returns_true_on_success(self, monkeypatch, tmp_path):
        """Happy path: touch() succeeds, returns True."""
        signal_file = str(tmp_path / "signal-done")
        monkeypatch.setattr(setup_server, "SIGNAL_FILE", signal_file)

        assert setup_server.signal_completion() is True
        from pathlib import Path

        assert Path(signal_file).exists()

    def test_signal_completion_returns_false_on_touch_failure(self, monkeypatch, tmp_path):
        """A Path.touch() failure is caught; function returns False."""
        from pathlib import Path

        signal_file = str(tmp_path / "signal-done")
        monkeypatch.setattr(setup_server, "SIGNAL_FILE", signal_file)

        def boom(self, *args, **kwargs):
            raise OSError("tmpfs full")

        monkeypatch.setattr(Path, "touch", boom)

        # No raise — bool return instead.
        assert setup_server.signal_completion() is False

    def test_signal_completion_returns_true_when_no_file_configured(self, monkeypatch):
        """When SIGNAL_FILE is None (test mode), returns True so callers
        proceed as if signal succeeded (the original swallow behavior).
        """
        monkeypatch.setattr(setup_server, "SIGNAL_FILE", None)

        assert setup_server.signal_completion() is True


# ── Structural / regression-prevention pins (litclock-dev#364) ──────────────────


class TestSetupServerStructuralInvariants:
    """Pins that future refactors cannot silently re-introduce the bug
    class fixed in litclock-dev#364. These run against the on-disk source — they
    do not exercise runtime behavior, but they fail loudly if the
    helper indirection is undone."""

    @staticmethod
    def _setup_server_source():
        import inspect

        return inspect.getsource(setup_server)

    def test_no_bare_os_kill_outside_helper(self):
        """The only bare ``os.kill(...SIGTERM)`` call site in setup_server is
        inside ``_schedule_self_terminate``. Pin via AST so a future refactor
        that re-inlines an ``os.kill`` call fails CI.
        """
        import ast

        source = self._setup_server_source()
        tree = ast.parse(source)

        offending = []

        class KillVisitor(ast.NodeVisitor):
            def __init__(self):
                self.in_helper_stack = []

            def visit_FunctionDef(self, node):
                in_helper = node.name == "_schedule_self_terminate" or (
                    self.in_helper_stack and self.in_helper_stack[-1]
                )
                self.in_helper_stack.append(in_helper)
                self.generic_visit(node)
                self.in_helper_stack.pop()

            def visit_Call(self, node):
                # Match os.kill(...) call expressions.
                func = node.func
                is_os_kill = (
                    isinstance(func, ast.Attribute)
                    and func.attr == "kill"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "os"
                )
                if is_os_kill and not (self.in_helper_stack and self.in_helper_stack[-1]):
                    offending.append(node.lineno)
                self.generic_visit(node)

        KillVisitor().visit(tree)

        assert offending == [], (
            f"Bare os.kill() found outside _schedule_self_terminate at lines: {offending}. "
            f"Route through _schedule_self_terminate to preserve the SIGTERM-ordering "
            f"invariant (litclock-dev#364)."
        )

    def test_bg_cancel_event_not_reintroduced(self):
        """litclock-dev#785 removed ``_BG_CANCEL``, an Event that was set and
        cleared by ``reset_state`` and waited on by nothing — dead since
        litclock-dev#715 deleted the ``_delayed`` timer it was built for, while
        four comments went on describing it as live. It misled three readers in
        one sitting, one of whom proposed adding a test that waited on it.

        Reviving it (the other option in the issue) was rejected: the only
        caller that would set it is ``reset_state``, which is test-only, so
        cancellation checks would be threaded through the first-boot
        provisioning path for something no device could ever trigger.

        This guard is the cheap half of not repeating that.
        """
        assert not hasattr(setup_server, "_BG_CANCEL"), (
            "_BG_CANCEL was re-introduced. If a cancellation Event is genuinely "
            "wanted, something must WAIT on it — see litclock-dev#785 for why "
            "the previous one was removed rather than revived."
        )
        src = self._setup_server_source()
        code = [
            f"{i}: {ln.strip()}"
            for i, ln in enumerate(src.splitlines(), 1)
            if "_BG_CANCEL" in ln and not ln.lstrip().startswith("#")
        ]
        assert not code, "only COMMENTS may mention _BG_CANCEL now:\n  " + "\n  ".join(code)

    def test_delayed_shutdown_function_removed(self):
        """The old ``_delayed_shutdown`` inner function in the no-WiFi
        branch is replaced by ``_schedule_self_terminate(delay=2.0)``. Pin
        via AST so a future refactor that re-introduces it fails CI.
        """
        import ast

        tree = ast.parse(self._setup_server_source())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                assert node.name != "_delayed_shutdown", (
                    f"_delayed_shutdown re-introduced at line {node.lineno}; "
                    f"use _schedule_self_terminate(delay=2.0) instead."
                )

    def test_schedule_self_terminate_docstring_mentions_364_and_reset_state(self):
        """The helper's docstring is load-bearing — it's the durable home
        for the SIGTERM-ordering invariant. Pin the canonical strings so
        a future refactor that tightens the docstring can't accidentally
        delete the invariant comment.
        """
        doc = setup_server._schedule_self_terminate.__doc__ or ""
        assert "litclock-dev#364" in doc, "helper docstring must reference issue litclock-dev#364"
        assert "reset_state" in doc, "helper docstring must reference reset_state"
        assert "IN_FLIGHT" in doc, "helper docstring must reference IN_FLIGHT"


class TestWifiPickerPlaceholderAndSort:
    """litclock-dev#554. Owner saw a network already selected in the picker
    and asked what decided it. Nothing did: a <select> with no `selected`
    option displays its first option, and first was whatever order nmcli
    returned. So a user who tapped Submit without opening the dropdown
    submitted a network they never chose — plausibly a neighbour's — and got
    a WiFi-join failure they had no way to diagnose, on a device with no
    keyboard. Same failure mode as litclock-dev#397's `_filter_own_hotspot`: the picker
    presenting something that looks like a decision already made.
    """

    def test_placeholder_leads_the_list_and_cannot_be_submitted(self):
        result = setup_server._build_wifi_options([{"ssid": "HomeWiFi", "signal": 80, "security": "WPA2"}])
        first = result.split("\n")[0].strip()
        assert first == f'<option value="" selected disabled>{setup_server._wifi_placeholder_text()}</option>'
        # `disabled` is what stops it being chosen; `value=""` is what makes
        # `required` reject it. Both, or the guard has a hole.
        assert "selected" in first and "disabled" in first

    def test_no_network_is_preselected(self):
        """The bug itself: `selected` must appear exactly once, on the
        placeholder. A `selected` on any real network re-creates it."""
        result = setup_server._build_wifi_options(
            [
                {"ssid": "HomeWiFi", "signal": 80, "security": "WPA2"},
                {"ssid": "Neighbour", "signal": 90, "security": "WPA2"},
            ]
        )
        assert result.count("selected") == 1
        for line in result.split("\n"):
            if "selected" in line:
                assert 'value=""' in line

    def test_sorted_by_signal_descending(self):
        """Sorted by us, not inherited from nmcli. Input is deliberately in
        the wrong order — a scan race or driver quirk produces exactly this
        and used to change which network sat in the display slot."""
        result = setup_server._build_wifi_options(
            [
                {"ssid": "Weak", "signal": 20, "security": "WPA2"},
                {"ssid": "Strong", "signal": 95, "security": "WPA2"},
                {"ssid": "Middling", "signal": 55, "security": "WPA2"},
            ]
        )
        assert result.index("Strong") < result.index("Middling") < result.index("Weak")

    def test_equal_signal_keeps_scan_order(self):
        """Python's sort is stable, so ties are deterministic rather than
        arbitrary — otherwise the order would still be untestable."""
        result = setup_server._build_wifi_options(
            [
                {"ssid": "Alpha", "signal": 60, "security": "WPA2"},
                {"ssid": "Bravo", "signal": 60, "security": "WPA2"},
            ]
        )
        assert result.index("Alpha") < result.index("Bravo")

    def test_missing_signal_sorts_last_rather_than_crashing(self):
        """/scan-wifi JSON is re-parsed client-side and could round-trip a
        null. A sort that raises here takes the whole setup page down."""
        result = setup_server._build_wifi_options(
            [
                {"ssid": "NoSignal", "signal": None, "security": "WPA2"},
                {"ssid": "Real", "signal": 40, "security": "WPA2"},
            ]
        )
        assert result.index("Real") < result.index("NoSignal")

    def test_manual_entry_option_trails_the_list(self):
        """A hidden SSID never appears in a scan, so before this there was no
        path to join one through setup at all. Last, so it doesn't compete
        with real networks for attention."""
        result = setup_server._build_wifi_options([{"ssid": "HomeWiFi", "signal": 80, "security": "WPA2"}])
        assert setup_server.MANUAL_SSID_VALUE in result
        assert result.index("HomeWiFi") < result.index(setup_server.MANUAL_SSID_VALUE)
        # The manual option must carry a NON-EMPTY value, or `required` would
        # reject the very path it exists to enable. Asserted against the
        # rendered markup: an earlier version compared the constant to "",
        # which is a tautology that could never fail.
        assert f'<option value="{setup_server.MANUAL_SSID_VALUE}" data-manual="1">' in result
        rest = result.split("\n", 1)[1]
        assert '<option value="">' not in rest, "only the placeholder may have an empty value"

    def test_empty_scan_still_offers_manual_entry(self, monkeypatch):
        """A user whose only network is hidden sees an empty scan every time.
        Dropping their one way to type it in would strand them on the setup
        page with no path forward."""
        import sys
        from unittest.mock import MagicMock

        monkeypatch.setattr(setup_server, "_WIFI_SCAN_NETWORKS", None)
        monkeypatch.setattr(setup_server, "_WIFI_SCAN_TIME", 0)
        mock_wifi = MagicMock()
        mock_wifi.scan_wifi_networks = lambda: []
        monkeypatch.setitem(sys.modules, "wifi_provision", mock_wifi)

        result, was_empty = setup_server._wifi_network_options()
        assert setup_server.MANUAL_SSID_VALUE in result
        assert setup_server._wifi_placeholder_empty_text() in result
        assert was_empty is True
        assert setup_server._WIFI_SCAN_NETWORKS is None

    def test_ssid_is_escaped_in_both_value_and_label(self):
        """An SSID is attacker-chosen text — any neighbour can name their AP —
        rendered into an HTML attribute AND element text.

        The fixture carries a double quote on purpose: without `quote=True`
        (html.escape's default) the value attribute breaks out, and the
        earlier version of this test asserted only the `<`/`>` half, so
        html.escape(..., quote=False) passed the whole suite."""
        result = setup_server._build_wifi_options([{"ssid": '<script>"x', "signal": 50, "security": "WPA2"}])
        assert '<option value="&lt;script&gt;&quot;x">&lt;script&gt;&quot;x (Medium)</option>' in result
        assert "<script>" not in result
        assert '"x' not in result


class TestWifiPickerMarkupAndScript:
    """The server-rendered markup and the Refresh path have to agree. A
    client-side rebuild that dropped the placeholder or the manual option
    would silently undo the server's fix the first time a user tapped
    Refresh (litclock-dev#554)."""

    @pytest.fixture(autouse=True)
    def _stub_scan(self, monkeypatch):
        import sys
        from unittest.mock import MagicMock

        monkeypatch.setattr(setup_server, "_WIFI_SCAN_NETWORKS", None)
        monkeypatch.setattr(setup_server, "_WIFI_SCAN_TIME", 0)
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", False)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", None)
        mock_wifi = MagicMock()
        mock_wifi.scan_wifi_networks = lambda: [{"ssid": "FakeHomeNet", "signal": 75, "security": "WPA2"}]
        monkeypatch.setitem(sys.modules, "wifi_provision", mock_wifi)

    def test_select_is_required(self):
        """`required` is the no-JS half of the fix: HTML validation runs
        without JavaScript, which is the norm inside captive-portal
        WebViews, and it rejects the empty submit before a slow
        captive-portal round trip."""
        html = setup_server._build_setup_html()
        select = next(line for line in html.split("\n") if 'name="wifi_ssid"' in line)
        assert "required" in select

    def test_nothing_autofocuses_ahead_of_the_network_picker(self):
        """litclock-dev#671: focus used to land on the password field, which is
        step 2 of a form whose step 1 is the picker. On a phone that opens the
        keyboard over the lower half of the page before the user has touched
        the control the page leads with, so they type a password, hit Submit,
        and get a validation bubble anchored to a <select> that may now be
        behind the keyboard. `required` (litclock-dev#554) means no wrong-network
        submit gets through, so this is friction rather than a correctness bug --
        but it steers the same audience litclock-dev#554 and litclock-dev#588
        were written for into the same wrong first move.

        Asserted across EVERY form control, not just the password input: moving
        `autofocus` to the <select> is NOT the fix. On mobile that can spring
        the native picker wheel open on load, which is louder than what it
        replaces, and captive-portal WebViews differ again. If we ever want it
        there it needs the same two-phone check as the rest of this flow, and
        this test failing is the prompt to go get one.

        Scoped to the control tags rather than a bare `"autofocus" not in html`
        (/review). The page embeds its own CSS and JS, so a substring check over
        the whole document would fail on a comment, a JS string or a
        `data-no-autofocus` attribute -- failing for a reason that has nothing
        to do with where the keyboard opens.
        """
        html = setup_server._build_setup_html()
        controls = re.findall(r"<(?:input|select|textarea)\b[^>]*>", html, re.I)
        # Guard the guard: if the extraction stopped matching, an empty list
        # would satisfy the assertion below without inspecting anything.
        assert any('name="wifi_ssid"' in c for c in controls), (
            f"control extraction missed the network picker; got {len(controls)} tags"
        )
        assert any('id="wifi-password"' in c for c in controls), (
            f"control extraction missed the password input; got {len(controls)} tags"
        )
        offenders = [c for c in controls if re.search(r"\bautofocus\b", c, re.I)]
        assert not offenders, (
            "no setup-form control may autofocus -- the keyboard opens over the "
            f"form before the user has picked a network. Offending tags: {offenders}"
        )

    def test_the_password_field_is_never_focused_programmatically(self):
        """The sibling half of litclock-dev#671. Dropping the attribute is
        pointless if a script grabs the same field on load. The page's only
        `.focus()` is on the manual-SSID input inside `onSsidChange()`, which
        runs when the user picks "My network isn't listed" -- user-initiated,
        after the picker, and correct.
        """
        html = setup_server._build_setup_html()
        assert not re.search(r"wifi-password['\"]\s*\)?\s*(?:\.\w+)*\.focus\s*\(", html), (
            "something focuses the WiFi password field from script; that "
            "reintroduces litclock-dev#671 with the attribute removed"
        )

    def test_manual_input_is_in_the_dom_not_js_generated(self):
        """<details>, not a JS-revealed field — it has to open with
        JavaScript disabled."""
        html = setup_server._build_setup_html()
        assert f'name="{setup_server.MANUAL_SSID_FIELD}"' in html
        assert "<details" in html
        assert "<summary" in html

    def test_manual_input_does_not_fight_the_phone_keyboard(self):
        """SSIDs are case- and space-exact. iOS autocapitalises and
        autocorrects a plain text input, which silently produces a name that
        will not match."""
        html = setup_server._build_setup_html()
        field = next(line for line in html.split("\n") if f'name="{setup_server.MANUAL_SSID_FIELD}"' in line)
        block = html[html.index(field) : html.index(field) + 400]
        assert 'autocapitalize="none"' in block
        assert 'autocorrect="off"' in block
        assert 'spellcheck="false"' in block

    def test_refresh_rebuilds_placeholder_and_manual_option(self):
        """Both the success and the failure branch of the Refresh handler."""
        html = setup_server._build_setup_html()
        script = html[html.index("<script>") : html.index("</script>")]
        # The manual option is re-appended on every terminal branch, so a
        # failed scan can't strip the hidden-network path. Since
        # litclock-dev#848 every branch reaches it through ONE tail helper,
        # so the claim is two assertions rather than a count over a region:
        # the helper re-appends, and every rebuild ends in the helper.
        assert "appendManualOption(select);" in _js_function(script, "finishRebuild")
        assert _js_function(script, "refreshNetworks").count("finishRebuild(select,") == 4
        assert "resetSsidOptions" in script
        # No bare `<option value="">…` replacing innerHTML — that was the
        # shape that produced a submittable empty first option.
        assert "select.innerHTML = '<option" not in script

    def test_refresh_sorts_client_side_too(self):
        """/scan-wifi returns the scan order; the client must not reintroduce
        the dependency the server just removed."""
        html = setup_server._build_setup_html()
        script = html[html.index("function refreshNetworks") :]
        assert "networks.sort(" in script

    def test_manual_sentinel_matches_between_python_and_js(self):
        """One constant, two languages. If they drift, the sentinel POSTs as
        a literal SSID and the clock tries to join a network named
        `__litclock_type_it_myself__`."""
        import json

        html = setup_server._build_setup_html()
        assert f"var MANUAL_SSID_VALUE = {json.dumps(setup_server.MANUAL_SSID_VALUE)};" in html


class TestWifiPickerReviewHardening:
    """Second-round findings from the litclock-dev#580 review. Each of these was verified
    by mutation: the guard was reverted and the suite stayed green, which is
    what made the gap worth closing rather than the code merely looking thin.
    """

    @pytest.fixture(autouse=True)
    def _stub_scan(self, monkeypatch):
        import sys
        from unittest.mock import MagicMock

        monkeypatch.setattr(setup_server, "_WIFI_SCAN_NETWORKS", None)
        monkeypatch.setattr(setup_server, "_WIFI_SCAN_TIME", 0)
        monkeypatch.setattr(setup_server, "_WIFI_SCAN_SSIDS", frozenset())
        monkeypatch.setattr(setup_server, "WIFI_LAST_MANUAL_SSID", "")
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", False)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", None)
        monkeypatch.setattr(setup_server, "HOTSPOT_SSID", "LitClock-Setup")
        mock_wifi = MagicMock()
        mock_wifi.scan_wifi_networks = lambda: [{"ssid": "FakeHomeNet", "signal": 75, "security": "WPA2"}]
        monkeypatch.setitem(sys.modules, "wifi_provision", mock_wifi)

    def _script(self):
        html = setup_server._build_setup_html()
        return html[html.index("function resetSsidOptions") :]

    # ── The Refresh path must not re-create the bug the server just fixed ──

    def test_refresh_never_disables_the_select(self):
        """THE bug this round found. A disabled control is excluded from the
        form data set AND skipped by constraint validation, so a submit
        landing mid-refresh POSTs no wifi_ssid at all — `required` silently
        bypassed, and the server sees an empty pick. Disabling it also had no
        recovery path if the fetch never settled."""
        # Assignment, not the substring — the code carries a comment naming
        # the hazard, and that comment must not make the guard untestable.
        assert "select.disabled =" not in setup_server._build_setup_html()

    def test_refresh_placeholder_is_selected_and_disabled(self):
        """resetSsidOptions is the JS twin of the server placeholder, and
        these two properties are the whole guard. Deleting either left all
        tests green while restoring a submittable, pre-selected first
        option — the originating litclock-dev#554 defect, via Refresh."""
        script = self._script()
        assert "ph.selected = true" in script
        assert "ph.disabled = true" in script
        assert "ph.value = ''" in script

    def test_scan_fetch_is_abortable_and_bounded(self):
        """The phone-to-clock link is exactly the link that drops. With no
        timeout a hung scan leaves the picker reading "Scanning..." forever,
        inside a captive-portal WebView that often has no reload control."""
        script = self._script()
        # Assert USE, not presence. Mutation caught the earlier version:
        # replacing `new AbortController()` with `null` still left the word
        # AbortController in the feature-detect, and swapping SCAN_TIMEOUT_MS
        # for a huge literal still left the constant declared. Both passed.
        assert "new AbortController()" in script
        assert "ctrl.abort()" in script
        assert "}}, SCAN_TIMEOUT_MS);" in script or "}, SCAN_TIMEOUT_MS);" in script
        assert "fetch('/scan-wifi', ctrl ? {signal: ctrl.signal} : undefined)" in script
        assert "clearTimeout(timer)" in script

    def test_manual_option_survives_even_while_scanning(self):
        """Refresh appends it up front too, so the hidden-network path is
        never absent from the dropdown at any point in the cycle. The interim
        rebuild is the FIRST finishRebuild call in the function, before the
        fetch is issued."""
        script = self._script()
        body = _js_function(script, "refreshNetworks")
        assert body.index("finishRebuild(select,") < body.index("fetch(")

    def test_ajax_empty_refresh_opens_the_manual_disclosure(self):
        """litclock-dev#615: the server-rendered empty-scan path force-opens the
        manual-SSID <details>, but a Refresh returning [] only swapped the
        placeholder — the hidden-network user who taps Refresh into an empty
        scan was left with the type-it-in field folded away. The empty branch
        must now open it too. (Reverting the one JS line left the suite green.)

        Since litclock-dev#848 the mechanism is different but the contract is
        the same: the branch SELECTS the manual option, and the gate that runs
        after every rebuild (syncManualSsid) shows and opens the disclosure
        for exactly that selection. The executed-DOM test in
        TestManualSsidGate proves the end state; this pins the branch itself.
        """
        script = self._script()
        empty_branch = script[script.index("networks.length === 0") :]
        empty_branch = empty_branch[
            : empty_branch.index("} else {") if "} else {" in empty_branch else len(empty_branch)
        ]
        # `true`, not the recomputed keepManual: an empty scan selects the
        # manual option whatever the dropdown said before it. The helper both
        # re-appends the option and runs the gate, which is what opens the
        # disclosure — selecting it is the whole mechanism since litclock-dev#848.
        assert "finishRebuild(select, true)" in empty_branch

    def test_onssidchange_is_defined_and_wired(self):
        """Deleting the onchange attribute left every test green while
        silently killing the one path this feature exists to add: the
        <details> stays collapsed, the input is never focused, and the user
        submits an empty manual field."""
        html = setup_server._build_setup_html()
        # The ATTRIBUTE, not the bare call — `onSsidChange()` also appears in
        # the function definition, so the earlier assertion survived deleting
        # the wiring entirely (mutation-verified).
        assert 'onchange="onSsidChange()"' in html
        assert "function onSsidChange" in html

    # ── One string, one constant ──

    def test_manual_label_is_one_constant_across_python_and_js(self):
        """The label appears in the option, the <details> summary, the help
        copy that quotes it, and the JS rebuild. Four literals would let the
        dropdown option drift from the disclosure it tells the user to open."""
        import html as html_mod
        import json

        html = setup_server._build_setup_html()
        # Three HTML surfaces (option label, <summary>, the help copy that
        # quotes it) all escaped, plus the JS literal via json.dumps.
        assert html.count(html_mod.escape(setup_server._manual_ssid_text())) >= 3
        assert f"opt.textContent = {json.dumps(setup_server._manual_ssid_text())};" in html

    # ── Retry render ──

    def test_retry_echoes_the_typed_ssid_back_and_opens_the_disclosure(self, monkeypatch):
        """Without this the retry page is blank and re-collapsed: the
        hidden-network owner has to remember what they typed while a red
        banner tells them to check their PASSWORD, when the likelier fault on
        that path is the SSID spelling."""
        monkeypatch.setattr(setup_server, "WIFI_LAST_MANUAL_SSID", "MyHiddenNet")
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", "Incorrect WiFi password")
        html = setup_server._build_setup_html()
        assert 'value="MyHiddenNet"' in html
        assert '<details id="manual-ssid" style="margin-bottom:14px;" open>' in html

    def test_echoed_ssid_is_html_escaped(self):
        """It round-trips through an HTML attribute, and it is attacker-typed."""
        setup_server.WIFI_LAST_MANUAL_SSID = 'evil" onfocus="x'
        try:
            html = setup_server._build_setup_html()
            assert 'evil" onfocus' not in html
            assert "&quot; onfocus" in html
        finally:
            setup_server.WIFI_LAST_MANUAL_SSID = ""

    def test_empty_scan_opens_the_disclosure(self, monkeypatch):
        """When the scan is empty, typing the name is the ONLY way forward and
        the Refresh button does nothing without JavaScript. Leaving the
        instructions folded inside a disclosure the user has to discover is
        how that user gets stuck."""
        import sys
        from unittest.mock import MagicMock

        mock_wifi = MagicMock()
        mock_wifi.scan_wifi_networks = lambda: []
        monkeypatch.setitem(sys.modules, "wifi_provision", mock_wifi)
        html = setup_server._build_setup_html()
        assert setup_server._wifi_placeholder_empty_text() in html
        assert '<details id="manual-ssid" style="margin-bottom:14px;" open>' in html

    def test_disclosure_stays_shut_on_a_clean_first_render(self):
        """The 95% case must not pay for the retry case with extra clutter."""
        html = setup_server._build_setup_html()
        assert '<details id="manual-ssid" style="margin-bottom:14px;">' in html

    # ── Byte budget, stated in bytes ──

    def test_manual_input_carries_a_client_side_length_guard(self):
        """The attribute interpolates SSID_MAX_BYTES (litclock-dev#605 item
        12 — it was a re-typed literal before, exactly the drift the
        constant exists to prevent). maxlength counts UTF-16 code units
        while the budget is bytes, so for multi-byte names the client guard
        is looser than the server's — deliberate: it stops the all-ASCII
        case early, and the do_POST byte check (pinned by its own tests) is
        the real gate."""
        html = setup_server._build_setup_html()
        at = html.index(f'maxlength="{setup_server.SSID_MAX_BYTES}"')
        assert setup_server.MANUAL_SSID_FIELD in html[at - 400 : at], "maxlength must sit on the manual SSID input"

    def test_maxlength_actually_interpolates_the_constant(self, monkeypatch):
        """Kills the equivalent mutant the assertion above cannot: with
        SSID_MAX_BYTES = 32, a re-typed literal renders identically. A
        sentinel value proves the attribute reads the constant at build
        time (/review on litclock-dev#614 — the item-12 fix was otherwise untestable)."""
        monkeypatch.setattr(setup_server, "SSID_MAX_BYTES", 48)
        html = setup_server._build_setup_html()
        assert 'maxlength="48"' in html
        assert 'maxlength="32"' not in html


class TestScannedSsids:
    """`hidden yes` is not a per-attempt flag — nmcli writes
    802-11-wireless.hidden=yes into the SAVED profile, so the clock then
    announces that SSID in cleartext probe requests for the life of the
    install. _scanned_ssids is what keeps that off a network the radio can
    plainly see (/review, litclock-dev#580)."""

    def test_casefolds_so_a_retyped_name_still_matches(self):
        """A human copying a name off a router label gets the capitalisation
        wrong constantly; that must not be read as 'the radio can't see it'."""
        assert setup_server._scanned_ssids([{"ssid": "HomeWiFi"}]) == frozenset({"homewifi"})

    def test_skips_nameless_entries(self):
        assert setup_server._scanned_ssids([{"ssid": ""}, {"signal": 50}]) == frozenset()

    def test_populated_from_a_scan(self, monkeypatch):
        import sys
        from unittest.mock import MagicMock

        monkeypatch.setattr(setup_server, "_WIFI_SCAN_NETWORKS", None)
        monkeypatch.setattr(setup_server, "_WIFI_SCAN_TIME", 0)
        monkeypatch.setattr(setup_server, "_WIFI_SCAN_SSIDS", frozenset())
        mock_wifi = MagicMock()
        mock_wifi.scan_wifi_networks = lambda: [{"ssid": "HomeWiFi", "signal": 70, "security": "WPA2"}]
        monkeypatch.setitem(sys.modules, "wifi_provision", mock_wifi)
        setup_server._wifi_network_options()
        assert "homewifi" in setup_server._WIFI_SCAN_SSIDS


class TestNetSignalCoercion:
    """Both arms of the except tuple, because narrowing it to TypeError alone
    left the whole suite green while a string signal would 500 the setup
    page — which on a captive portal is a user with no path forward."""

    def test_none_signal(self):
        assert setup_server._net_signal({"signal": None}) == 0

    def test_absent_signal(self):
        assert setup_server._net_signal({}) == 0

    def test_non_numeric_string_signal(self):
        """The ValueError arm. Untested before; `except TypeError` passed."""
        assert setup_server._net_signal({"signal": "excellent"}) == 0

    def test_numeric_string_signal_is_coerced_not_zeroed(self):
        assert setup_server._net_signal({"signal": "55"}) == 55

    def test_a_garbled_scan_still_renders_a_page(self):
        """The whole point: one malformed entry must not take the page down."""
        result = setup_server._build_wifi_options(
            [{"ssid": "Garbled", "signal": "excellent", "security": "WPA2"}, {"ssid": "Real", "signal": 40}]
        )
        assert result.index("Real") < result.index("Garbled")


class TestSentinelCollision:
    """The comment on MANUAL_SSID_VALUE claims a real AP broadcasting that
    exact name 'degrades to one extra step, never to a wrong network'.
    Asserted, not just asserted-in-prose."""

    def test_collision_renders_two_options_with_the_same_value(self):
        result = setup_server._build_wifi_options(
            [{"ssid": setup_server.MANUAL_SSID_VALUE, "signal": 90, "security": "WPA2"}]
        )
        assert result.count(f'value="{setup_server.MANUAL_SSID_VALUE}"') == 2


class TestNoHotspotJargonInUserFacingCopy:
    """litclock-dev#555. "Hotspot" is genuinely ambiguous in consumer usage:
    on iOS the word appears as *Personal Hotspot*, a thing YOU turn on to
    share YOUR connection; Android labels it *Hotspot & tethering*. So the
    one place a phone owner has already met the word teaches the opposite
    direction of travel from what we mean — join a network the clock is
    already broadcasting.

    A per-string assertion in each copy test would leave gaps; these sweep
    whole rendered surfaces, which is what catches the next well-meaning
    copy edit that reaches for the familiar word. Code comments and
    internal identifiers are untouched and out of scope — only what a
    recipient reads.

    These cover setup_server ONLY. The cross-module surfaces — the e-ink
    splash, first-boot.sh's painted titles, and the control_server route
    messages — are swept by TestNoHotspotJargonAcrossSurfaces below. An
    earlier version of this docstring claimed to sweep "whole rendered
    surfaces" while reaching one module family, which is precisely how
    "Hotspot Failed" and two API messages shipped green.
    """

    @pytest.fixture(autouse=True)
    def _provisioning(self, monkeypatch):
        import sys
        from unittest.mock import MagicMock

        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "HOTSPOT_SSID", "LitClock-Setup")
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", False)
        monkeypatch.setattr(setup_server, "_WIFI_SCAN_NETWORKS", None)
        monkeypatch.setattr(setup_server, "_WIFI_SCAN_TIME", 0)
        mock_wifi = MagicMock()
        mock_wifi.scan_wifi_networks = lambda: [{"ssid": "FakeHomeNet", "signal": 75, "security": "WPA2"}]
        monkeypatch.setitem(sys.modules, "wifi_provision", mock_wifi)

    @staticmethod
    def _copy_only(html):
        """Strip <option> elements before sweeping.

        The page interpolates scanned SSIDs into the dropdown, and a
        neighbour is perfectly entitled to broadcast "xfinitywifi Hotspot".
        Asserting over the raw page asserts a property of scan DATA, not of
        our copy — it would flip red for a non-defect the moment anyone made
        the scan mock realistic."""
        return re.sub(r"<option\b.*?</option>", "", html, flags=re.S)

    def test_setup_page_is_clean(self, monkeypatch):
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", None)
        assert "hotspot" not in self._copy_only(setup_server._build_setup_html()).lower()

    def test_a_neighbours_ssid_containing_the_word_is_not_a_failure(self, monkeypatch):
        """Pins the scoping above: our copy is clean, their SSID is theirs."""
        import sys
        from unittest.mock import MagicMock

        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", None)
        mock_wifi = MagicMock()
        mock_wifi.scan_wifi_networks = lambda: [{"ssid": "xfinitywifi Hotspot", "signal": 60, "security": "WPA2"}]
        monkeypatch.setitem(sys.modules, "wifi_provision", mock_wifi)
        html = setup_server._build_setup_html()
        assert "xfinitywifi Hotspot" in html, "the neighbour's network must still be offered"
        assert "hotspot" not in self._copy_only(html).lower()

    def test_setup_page_with_error_banner_is_clean(self, monkeypatch):
        """The retry path is where the word did the most damage — the user
        has already failed once and is re-reading every word."""
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", "Incorrect WiFi password")
        assert "hotspot" not in self._copy_only(setup_server._build_setup_html()).lower()

    def test_cna_bridge_page_is_clean(self):
        assert "hotspot" not in setup_server._build_cna_bridge_html().lower()

    def test_success_and_error_templates_are_clean(self):
        assert "hotspot" not in setup_server.HTML_SUCCESS.lower()
        assert "hotspot" not in setup_server._error_page("x").lower()


class TestNoHotspotJargonAcrossSurfaces:
    """The cross-module half of the sweep (litclock-dev#555).

    TestNoHotspotJargonInUserFacingCopy reaches setup_server only. Review
    found three surfaces it could not see, each of which shipped green:
    `scripts/first-boot.sh` painting "Hotspot Failed" to the SAME e-ink panel
    as the reworded labels, and the two control_server route messages that
    back the reworded PWA cards on the no-JS form-POST path.

    A recipient can meet the e-ink panel, the captive portal, the PWA and
    the API messages inside one setup session. One vocabulary or none.
    """

    REPO = pathlib.Path(__file__).resolve().parent.parent

    def test_eink_splash_copy_is_clean(self):
        import eink_display

        surfaces = [eink_display.SETUP_LABEL_NETWORK, eink_display.SETUP_LABEL_PASSWORD]
        for is_retry in (False, True):
            surfaces += eink_display.setup_instruction_lines("10.42.0.1", is_retry=is_retry)
        for text in surfaces:
            assert "hotspot" not in text.lower(), text

    def test_first_boot_painted_titles_are_clean(self):
        """`display_message` sites now paint catalog triplets (litclock-dev
        litclock-dev#532 bulk extraction), so the panel copy to vet lives in the en
        catalog: collect every prefix the script paints, then check the
        resolved catalog VALUES. This keeps the guard chain site→key→value
        — vetting the script's literals alone would observe nothing."""
        import json as _json

        script = (self.REPO / "scripts" / "first-boot.sh").read_text()
        prefixes = re.findall(r"display_message(?:_strict)?\s+([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)", script)
        assert prefixes, "no painted catalog prefixes found — the regex has drifted"
        catalog = _json.loads((self.REPO / "languages" / "en" / "strings.json").read_text())
        painted = [v for k, v in catalog.items() if any(k.startswith(p + ".") for p in prefixes)]
        assert painted, "no catalog values matched the painted prefixes"
        offenders = [t for t in painted if "hotspot" in t.lower()]
        assert not offenders, offenders
        # display_qr has NO call sites (retired by litclock-dev#647/litclock-dev#715 and pinned
        # absent by test_first_boot_flow) — no literal sweep for it here; a
        # sweep over zero sites observes nothing (slice-1 /review).

    def test_first_boot_console_banner_is_clean(self):
        """/etc/issue is the HDMI-console twin of the panel labels."""
        script = (self.REPO / "scripts" / "first-boot.sh").read_text()
        banner = re.findall(r'printf\s+"([^"]*)"', script)
        offenders = [b for b in banner if "hotspot" in b.lower()]
        assert not offenders, offenders

    def test_control_server_route_messages_are_clean(self):
        """These back the reset cards on the no-JS form-POST path, where the
        browser renders the raw JSON body as the user's terminal screen."""
        for name in ("wifi.py", "system.py"):
            src = (self.REPO / "src" / "control_server" / "routes" / name).read_text()
            for message in re.findall(r'"message":\s*\(?\s*((?:\s*"[^"]*")+)', src):
                assert "hotspot" not in message.lower(), f"{name}: {message}"

    def test_pwa_user_facing_copy_is_clean(self):
        """The confirm sheets and the reconnect cards. The card copy lives
        in the CATALOG since litclock-dev#532 slice 5, so the sweep's real
        subject is every catalog VALUE — the source-file scans below only
        cover what still lives inline (Codex slice-5 /review)."""
        import json as _json

        catalog = _json.loads(
            (self.REPO / "languages" / "en" / "strings.json").read_text(encoding="utf-8")
        )
        offenders = {
            k: v
            for k, v in catalog.items()
            if not k.startswith("_") and "hotspot" in str(v).lower()
        }
        assert not offenders, offenders
        tpl = (self.REPO / "src" / "control_server" / "templates" / "system.html.j2").read_text()
        js = (self.REPO / "src" / "control_server" / "static" / "js" / "system.js").read_text()
        # Template: visible text only, and JS: only the strings that reach innerHTML.
        for text in re.findall(r">([^<>]*hotspot[^<>]*)<", tpl, flags=re.I):
            raise AssertionError(f"system.html.j2: {text.strip()}")
        for line in js.split("\n"):
            if "reconnect-state__body" in line:
                assert "hotspot" not in line.lower(), line

    def test_operator_scripts_are_clean(self):
        """Semi-technical surfaces (SSH + sudo), but a human still reads them,
        and docs/sd-card-cloning.md was reworded to match."""
        for name in ("reset-setup.sh", "prepare-for-cloning.sh"):
            script = (self.REPO / "scripts" / name).read_text()
            for echoed in re.findall(r'echo\s+(?:-e\s+)?"([^"]*)"', script):
                assert "hotspot" not in echoed.lower(), f"{name}: {echoed}"


# ── litclock-dev#603: retry instruction variants ────────────────────


class TestRetryInstructionVariants:
    """The connect-failed retry steps. Importable on dev machines:
    eink_display needs Pillow at import time, not the waveshare driver,
    so this copy is testable outside tests/test_eink_display.py."""

    def test_connect_failed_variant_has_no_password_step(self):
        import eink_display

        lines = eink_display.setup_instruction_lines(
            "10.42.0.1", is_retry=True, retry_reason=eink_display.HOTSPOT_RETRY_CONNECT_FAILED
        )
        joined = " ".join(lines)
        # The whole point: no password guidance when the password wasn't
        # the failure. Radio-physical causes lead instead.
        assert "password" not in joined.lower()
        assert any("2.4GHz" in ln for ln in lines)
        assert lines[0].startswith("1. Rescan the QR code")
        # Fallback URLs keep the explicit scheme (litclock-dev#588 rules).
        assert "http://" in lines[-1]

    def test_password_variant_copy_is_unchanged(self):
        import eink_display

        lines = eink_display.setup_instruction_lines(
            "10.42.0.1", is_retry=True, retry_reason=eink_display.HOTSPOT_RETRY_WIFI_PASSWORD
        )
        assert any("WiFi password" in ln for ln in lines)

    def test_retry_without_reason_keeps_the_password_copy(self):
        """Backward compat: is_retry=True with no reason is the pre-litclock-dev#603
        call shape — it must keep rendering what it always rendered."""
        import eink_display

        lines = eink_display.setup_instruction_lines("10.42.0.1", is_retry=True)
        assert any("WiFi password" in ln for ln in lines)

    def test_both_retry_reasons_render_the_retry_title(self):
        """display_hotspot_info's is_retry gate must accept BOTH variants —
        a connect_failed reason that fell through to the non-retry splash
        would paint first-boot setup copy over a failed join."""
        import eink_display

        assert eink_display.HOTSPOT_RETRY_CONNECT_FAILED == "connect_failed"
        assert eink_display.HOTSPOT_RETRY_WIFI_PASSWORD == "wifi_password"


SRC_DIR = pathlib.Path(__file__).resolve().parents[1] / "src"


class TestNormalModeRetired:
    """litclock-dev#715: the pre-connected normal-mode page (self-signed
    HTTPS on 8443) is retired. The one remaining no-flag caller is
    first-boot.sh's missing-env fallback, whose contract is exit-1-loudly →
    wait_for_setup restart-loop → 1800s Setup-Incomplete ceiling → next-boot
    retry. These pin the SHAPE of the retirement, not a list of removed
    names."""

    def test_no_flag_invocation_exits_1_loudly(self, tmp_path):
        # Executed contract pin: env file PRESENT (so this isn't the
        # missing-env exit) and no --provisioning → exit 1 with a message,
        # before any socket is bound.
        env = tmp_path / "env.sh"
        env.write_text("# test env\n")
        result = subprocess.run(
            [sys.executable, str(SRC_DIR / "setup_server.py"), str(env), str(tmp_path / "sig")],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 1
        assert "provisioning" in (result.stdout + result.stderr).lower()

    def test_missing_env_still_exits_1(self, tmp_path):
        # The fallback arm's original trigger — must keep exiting 1 so the
        # restart-loop → ceiling mechanics are unchanged.
        result = subprocess.run(
            [
                sys.executable,
                str(SRC_DIR / "setup_server.py"),
                str(tmp_path / "missing-env.sh"),
                str(tmp_path / "sig"),
                "--provisioning",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 1

    def test_module_carries_no_tls_server_machinery(self):
        # Shape assertion: no attribute of the module is an ssl/TLS server
        # or cert generator, and the module does not import ssl or
        # https_cert. Asserting on import structure (not on the two removed
        # names) so a re-introduction under any name is caught.
        import ast as _ast

        tree = _ast.parse((SRC_DIR / "setup_server.py").read_text())
        imported = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, _ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert "ssl" not in imported, "ssl came back — the TLS server path was retired (litclock-dev#715)"
        assert "https_cert" not in imported, "https_cert came back (litclock-dev#715)"

    def test_first_boot_fallback_arm_survives(self):
        # The contract consumer: first-boot.sh's missing-env ELSE-ARM must
        # route through start_setup_server. Anchored inside the arm itself
        # (/review litclock-dev#732: a whole-file substring pin would be satisfied by
        # any future bare use of the name anywhere in the script).
        body = (SRC_DIR.parent / "scripts" / "first-boot.sh").read_text()
        anchor = "env.sh missing on the pre-connected path"
        assert anchor in body, "the missing-env fallback arm's log line vanished"
        arm = body[body.index(anchor) :]
        arm = arm[: arm.index("\n        fi\n") if "\n        fi\n" in arm else 400]
        code = "\n".join(ln for ln in arm.splitlines() if not ln.lstrip().startswith("#"))
        assert "start_setup_server" in code, (
            "the missing-env fallback arm no longer calls start_setup_server — "
            "the litclock-dev#715 retirement depends on that exit-1 restart-loop"
        )


@pytest.fixture(autouse=True)
def _fresh_language_state(monkeypatch):
    """/review litclock-dev#742 follow-up F3: the registry fixtures reset the catalog
    cache BEFORE but never after, positive-caching a two-language registry
    for process lifetime — any later registry-reading test inherited it
    (the litclock-dev#355 order-dependence class). Reset around every test here."""
    import strings_catalog

    monkeypatch.delenv("LITCLOCK_LANGUAGE", raising=False)
    strings_catalog.reset_cache()
    yield
    strings_catalog.reset_cache()


def _picker_registry(tmp_path, monkeypatch, de_status="active"):
    import json as _json

    import strings_catalog

    reg = tmp_path / "languages.json"
    reg.write_text(_json.dumps({
        "fleet_default": "en",
        "languages": {
            "en": {"code": "en", "native_name": "English", "status": "active",
                   "strings": "languages/en/strings.json"},
            "de": {"code": "de", "native_name": "Deutsch", "status": de_status,
                   "strings": "languages/de/strings.json"},
        },
    }))
    monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", reg)
    strings_catalog.reset_cache()
    return reg


class TestWifiCopyResolvesPerRequest:
    """litclock-dev#532 follow-up (Stage-4 gates /review): the picker's
    manual option and placeholders resolved at MODULE IMPORT, so a language
    persisted or gift-marker-carried mid-session kept rendering English on
    the retry re-render. Executed proof: a language switch reaches the
    copy — and the rendered option list — with no reimport."""

    def test_language_switch_reaches_picker_copy_without_reimport(self, tmp_path, monkeypatch):
        import json as _json

        import strings_catalog

        de_strings = tmp_path / "de-strings.json"
        de_strings.write_text(
            _json.dumps(
                {
                    "setup.wifi.manual_option": "DE-MANUAL",
                    "setup.wifi.placeholder": "DE-PLACEHOLDER",
                    "setup.wifi.placeholder_empty": "DE-EMPTY",
                }
            ),
            encoding="utf-8",
        )
        reg = tmp_path / "languages.json"
        reg.write_text(
            _json.dumps(
                {
                    "languages": {
                        "en": {"code": "en", "status": "active", "strings": "languages/en/strings.json"},
                        "de": {"code": "de", "status": "active", "strings": str(de_strings)},
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", reg)
        strings_catalog.reset_cache()
        # Baseline: English copy before the switch.
        assert setup_server._manual_ssid_text() != "DE-MANUAL"
        # The mid-session switch: no reimport, no restart.
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "de")
        assert setup_server._manual_ssid_text() == "DE-MANUAL"
        assert setup_server._wifi_placeholder_text() == "DE-PLACEHOLDER"
        assert setup_server._wifi_placeholder_empty_text() == "DE-EMPTY"
        # And the rendered option list carries it — the import-time
        # constants kept serving English exactly here.
        options = setup_server._build_wifi_options([])
        assert "DE-MANUAL" in options and "DE-PLACEHOLDER" in options

    def test_warm_scan_cache_rerenders_after_language_switch(self, tmp_path, monkeypatch):
        """The /review convergence finding (Codex + both subagents): the scan
        cache stored RENDERED option HTML, so a warm cache served the old
        language's placeholder + manual option for up to the 30s TTL after a
        switch. The cache now holds raw networks; a hit re-renders — in the
        new language, without touching the radio."""
        import sys as _sys
        from unittest.mock import MagicMock

        self._activate_de(tmp_path, monkeypatch)
        scan_calls = []
        mock_wifi = MagicMock()
        mock_wifi.scan_wifi_networks = lambda: scan_calls.append(1) or [
            {"ssid": "HomeWiFi", "signal": 80, "security": "WPA2"}
        ]
        monkeypatch.setitem(_sys.modules, "wifi_provision", mock_wifi)
        setup_server.reset_state()
        # Warm the cache in English.
        options, _ = setup_server._wifi_network_options()
        assert "DE-MANUAL" not in options and len(scan_calls) == 1
        # Switch mid-session; a warm-cache hit must re-render, not re-scan.
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "de")
        options, was_empty = setup_server._wifi_network_options()
        assert "DE-MANUAL" in options and "DE-PLACEHOLDER" in options
        assert "HomeWiFi" in options and was_empty is False
        assert len(scan_calls) == 1, "language switch must not trigger a radio rescan"

    def test_empty_scan_placeholder_tracks_language(self, tmp_path, monkeypatch):
        """placeholder_empty's only production path is the empty-scan branch
        of _wifi_network_options — cover it through that render (/review:
        the accessor-only assertion left this key without render-path
        proof)."""
        import sys as _sys
        from unittest.mock import MagicMock

        self._activate_de(tmp_path, monkeypatch)
        mock_wifi = MagicMock()
        mock_wifi.scan_wifi_networks = lambda: []
        monkeypatch.setitem(_sys.modules, "wifi_provision", mock_wifi)
        setup_server.reset_state()
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "de")
        options, was_empty = setup_server._wifi_network_options()
        assert was_empty is True and "DE-EMPTY" in options

    def test_env_file_channel_reaches_the_accessors(self, tmp_path, monkeypatch):
        """The production switch channel is the env.sh WRITE (mtime re-read),
        not the process env var — prove the accessors flip on a file rewrite
        with no reimport (/review: the env-var seam alone doesn't cover the
        channel the setup POST actually uses)."""
        self._activate_de(tmp_path, monkeypatch)
        env = tmp_path / "env.sh"
        env.write_text("export LITCLOCK_LANGUAGE=en\n", encoding="utf-8")
        monkeypatch.delenv("LITCLOCK_LANGUAGE", raising=False)
        monkeypatch.setenv("LITCLOCK_ENV_FILE", str(env))
        assert setup_server._manual_ssid_text() != "DE-MANUAL"
        import os as _os

        env.write_text("export LITCLOCK_LANGUAGE=de\n", encoding="utf-8")
        _os.utime(env, (_os.stat(env).st_atime, _os.stat(env).st_mtime + 2))
        assert setup_server._manual_ssid_text() == "DE-MANUAL"

    def _activate_de(self, tmp_path, monkeypatch):
        import json as _json

        import strings_catalog

        de_strings = tmp_path / "de-strings.json"
        de_strings.write_text(
            _json.dumps(
                {
                    "setup.wifi.manual_option": "DE-MANUAL",
                    "setup.wifi.placeholder": "DE-PLACEHOLDER",
                    "setup.wifi.placeholder_empty": "DE-EMPTY",
                }
            ),
            encoding="utf-8",
        )
        reg = tmp_path / "languages.json"
        reg.write_text(
            _json.dumps(
                {
                    "languages": {
                        "en": {"code": "en", "status": "active", "strings": "languages/en/strings.json"},
                        "de": {"code": "de", "status": "active", "strings": str(de_strings)},
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", reg)
        strings_catalog.reset_cache()


class TestLanguagePicker:
    """litclock-dev#532 pickers, hotspot arm: the no-JS <select> renders only
    on a multi-language fleet (a one-option select is pure friction), defaults
    from the gift marker, then Accept-Language, then English."""

    def test_single_language_fleet_renders_no_picker(self, monkeypatch):
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", False)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", None)
        html_out = setup_server._build_setup_html(accept_language="de-DE,de;q=0.9")
        assert 'name="litclock_language"' not in html_out

    def test_multilang_fleet_renders_picker_with_negotiated_default(self, tmp_path, monkeypatch):
        _picker_registry(tmp_path, monkeypatch)
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", False)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", None)
        monkeypatch.setattr(setup_server, "GIFT_LANGUAGE_MARKER", str(tmp_path / "absent-marker"))
        html_out = setup_server._build_setup_html(accept_language="de-DE,de;q=0.9,en;q=0.5")
        assert 'name="litclock_language"' in html_out
        assert '<option value="de" selected>Deutsch</option>' in html_out
        assert '<option value="en">English</option>' in html_out

    def test_gift_marker_overrides_negotiation(self, tmp_path, monkeypatch):
        _picker_registry(tmp_path, monkeypatch)
        marker = tmp_path / ".gift-language"
        marker.write_text("de\n")
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", False)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", None)
        monkeypatch.setattr(setup_server, "GIFT_LANGUAGE_MARKER", str(marker))
        # English phone, German gift: the recipient's clock defaults to the
        # gifter's choice.
        html_out = setup_server._build_setup_html(accept_language="en-US,en;q=0.9")
        assert '<option value="de" selected>Deutsch</option>' in html_out

    def test_marker_with_inactive_code_is_ignored(self, tmp_path, monkeypatch):
        _picker_registry(tmp_path, monkeypatch, de_status="incubating")
        marker = tmp_path / ".gift-language"
        marker.write_text("de\n")
        monkeypatch.setattr(setup_server, "GIFT_LANGUAGE_MARKER", str(marker))
        assert setup_server._default_language("en") == "en"


class TestLanguageRetryPrecedence:
    """/review litclock-dev#742 + 5b adversarial /review: a failed-WiFi retry must not
    silently reset the picker — this session's submission, then the
    PERSISTED env value, then the marker outrank Accept-Language. Persisted
    outranks the marker since 5b: a non-empty persisted value is always an
    explicit choice, and the old marker-first order let a kept inactive
    marker silently revert a recipient's pick after an OTA activation."""

    def test_persisted_env_value_beats_the_marker(self, tmp_path, monkeypatch):
        """The 5b losing-state regression: kept marker de (now active),
        recipient's persisted pick es — es must win on re-provision."""
        _picker_registry(tmp_path, monkeypatch)
        env = tmp_path / "env.sh"
        env.write_text("export LITCLOCK_LANGUAGE=en\n")
        marker = tmp_path / ".gift-language"
        marker.write_text("de")
        monkeypatch.setattr(setup_server, "ENV_FILE", str(env))
        monkeypatch.setattr(setup_server, "GIFT_LANGUAGE_MARKER", str(marker))
        monkeypatch.setattr(setup_server, "SUBMITTED_LANGUAGE", "")
        assert setup_server._default_language("de-DE") == "en"

    def test_persisted_env_value_beats_accept_language(self, tmp_path, monkeypatch):
        _picker_registry(tmp_path, monkeypatch)
        env = tmp_path / "env.sh"
        env.write_text("export LITCLOCK_LANGUAGE=de\n")
        monkeypatch.setattr(setup_server, "ENV_FILE", str(env))
        monkeypatch.setattr(setup_server, "GIFT_LANGUAGE_MARKER", str(tmp_path / "absent"))
        monkeypatch.setattr(setup_server, "SUBMITTED_LANGUAGE", "")
        assert setup_server._default_language("en-US,en;q=0.9") == "de"

    def test_session_submission_beats_the_marker(self, tmp_path, monkeypatch):
        # A gift recipient who overrode the gifter's choice keeps their
        # override on retry.
        _picker_registry(tmp_path, monkeypatch)
        marker = tmp_path / ".gift-language"
        marker.write_text("de")
        monkeypatch.setattr(setup_server, "GIFT_LANGUAGE_MARKER", str(marker))
        monkeypatch.setattr(setup_server, "SUBMITTED_LANGUAGE", "en")
        assert setup_server._default_language("de-DE") == "en"

    def test_marker_symlink_is_refused(self, tmp_path, monkeypatch):
        _picker_registry(tmp_path, monkeypatch)
        target = tmp_path / "target"
        target.write_text("de")
        link = tmp_path / ".gift-language"
        link.symlink_to(target)
        monkeypatch.setattr(setup_server, "GIFT_LANGUAGE_MARKER", str(link))
        monkeypatch.setattr(setup_server, "SUBMITTED_LANGUAGE", "")
        monkeypatch.setattr(setup_server, "ENV_FILE", str(tmp_path / "no-env"))
        # O_NOFOLLOW refuses the symlink → falls through to negotiation.
        assert setup_server._default_language("de-DE") == "de"  # via Accept-Language
        assert setup_server._read_marker_code() == ""

    def test_first_boot_consumes_the_marker_conditionally(self):
        """SUPERSEDED CONTRACT (pickers 5b, trap b): 5a consumed the marker
        unconditionally in the welcome-marker rm; 5b makes consumption
        conditional on the code being ACTIVE (honored) so a registry
        regression / OTA lag can't permanently erase the gifter's intent.
        The success block must still handle the marker (rm inside the
        honored branch), and the welcome markers stay unconditional.
        Deep coverage lives in test_first_boot_flow.py's
        TestGiftLanguageConditionalConsumption."""
        from pathlib import Path as _Path

        body = (_Path(setup_server.__file__).resolve().parents[1] / "scripts" / "first-boot.sh").read_text()
        code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
        assert "if gift_language_marker_consumable; then" in code
        idx = code.find("if gift_language_marker_consumable; then")
        assert "sudo rm -f /etc/litclock/.gift-language" in code[idx : idx + 300]
        # Welcome markers stay on the unconditional litclock-dev#316 rm, WITHOUT the
        # gift-language marker riding along.
        assert "sudo rm -f /etc/litclock/.welcome-mode /etc/litclock/.welcome-message" in code


class TestNegotiationReachableFromFreshBoot:
    """/review litclock-dev#742 follow-up F1 (the completing pass's catch): the seeded
    'en' default was indistinguishable from a persisted user choice, so the
    env-precedence step made Accept-Language negotiation DEAD CODE on every
    production first boot — the litclock-dev#646 tests-cover-what-production-never-
    runs class, emergent from two individually-correct review fixes."""

    def test_default_env_seed_lets_negotiation_fire(self, tmp_path, monkeypatch):
        _picker_registry(tmp_path, monkeypatch)
        env = tmp_path / "env.sh"
        env.write_text("export LITCLOCK_LANGUAGE=\n")  # the heredoc seed, verbatim
        monkeypatch.setattr(setup_server, "ENV_FILE", str(env))
        monkeypatch.setattr(setup_server, "GIFT_LANGUAGE_MARKER", str(tmp_path / "absent"))
        monkeypatch.setattr(setup_server, "SUBMITTED_LANGUAGE", "")
        assert setup_server._default_language("de-DE,de;q=0.9,en;q=0.5") == "de", (
            "a fresh default env must leave negotiation reachable — a seeded "
            "concrete code is indistinguishable from a user choice"
        )

    # litclock-dev#840 re-pointed these at the single source. The seed line used
    # to be hand-copied into first-boot.sh and reset-setup.sh, and this guard
    # walked both as raw text. `env_sh_defaults()` in scripts/lib/state.sh now
    # emits it, so reset-setup.sh no longer contains the string at all — the
    # old guard failed on a file that was behaving correctly, which is a guard
    # pointing at a moved target, not a regression. Kept HERE rather than moved
    # next to the other helper tests: its siblings above and below are the
    # behavioural half (`_default_language` with a fresh env vs a persisted
    # choice), and the /review litclock-dev#742 F1 + litclock-dev#743 reasoning this protects is
    # written in this class's docstring.

    def test_no_seeder_hardcodes_a_concrete_language(self):
        """The litclock-dev#742 F1 catch: a seeded concrete code is indistinguishable from
        a persisted user choice, so it makes Accept-Language negotiation DEAD
        CODE on every device the seeder provisions.

        Now covers scripts/lib/state.sh too, since that is where the line
        lives. The regex matches `export LITCLOCK_LANGUAGE=` followed by a
        LETTER, so the helper's legitimate `=$language` (a `$`) passes while
        `=en` fails — and so does a hardcoded default smuggled into the
        parameter expansion, because `${1-en}` would still have to render a
        concrete code into the emitted line, which the output check below
        catches directly.
        """
        from pathlib import Path as _Path

        root = _Path(setup_server.__file__).resolve().parents[1]
        concrete = re.compile(r"export LITCLOCK_LANGUAGE=[A-Za-z]")
        for script in ("scripts/lib/state.sh", "scripts/first-boot.sh", "scripts/reset-setup.sh"):
            body = (root / script).read_text()
            hit = concrete.search(body)
            assert not hit, (
                f"{script} seeds a concrete language ({hit.group(0)!r}) — negotiation goes dead "
                "on every device it provisions (/review litclock-dev#742 follow-up F1)"
            )

    def test_the_helper_emits_the_key_empty_by_default(self):
        """The empty-seed half, asserted on the helper's OUTPUT.

        Stronger than the substring this used to be: it catches a hardcoded
        default in the parameter expansion (`${1-en}`), which no source-text
        check for the literal `=en` would see. The empty value is what lets the
        dormant POST replace the line IN PLACE instead of appending a second one.
        """
        from tests.test_first_boot_flow import env_sh_defaults

        assert "export LITCLOCK_LANGUAGE=\n" in env_sh_defaults(), (
            "env_sh_defaults() must seed LITCLOCK_LANGUAGE EMPTY — a concrete default is "
            "indistinguishable from a user choice and kills negotiation (litclock-dev#742 F1, litclock-dev#743)"
        )
        # And the argument still works, so "empty" is not achieved by dropping
        # the knob: reset-setup.sh's gift-mode seed depends on it (litclock-dev#532).
        assert "export LITCLOCK_LANGUAGE=de\n" in env_sh_defaults("de")

    def test_first_boot_fallback_heredoc_still_seeds_it_empty(self, tmp_path):
        """first-boot's lib/state.sh-missing arm does NOT route through the
        helper (it is the arm for "state.sh is unusable"), so the line is still
        a literal there and needs its own check. EXECUTED — assert on the
        env.sh that arm actually writes."""
        from tests.test_first_boot_flow import _run_first_boot_default_env

        written = _run_first_boot_default_env(tmp_path, with_state_lib=False)
        assert "export LITCLOCK_LANGUAGE=\n" in written, (
            "first-boot's state.sh-missing fallback wrote an env.sh without the empty language "
            "seed — a device born from that arm cannot negotiate (litclock-dev#742 F1)"
        )

    def test_persisted_choice_still_survives_a_server_restart(self, tmp_path, monkeypatch):
        # The env step's real job: first-boot's crash-restart loop loses
        # SUBMITTED_LANGUAGE, but the POSTed choice lives in env.sh.
        _picker_registry(tmp_path, monkeypatch)
        env = tmp_path / "env.sh"
        env.write_text("export LITCLOCK_LANGUAGE=de\n")
        monkeypatch.setattr(setup_server, "ENV_FILE", str(env))
        monkeypatch.setattr(setup_server, "GIFT_LANGUAGE_MARKER", str(tmp_path / "absent"))
        monkeypatch.setattr(setup_server, "SUBMITTED_LANGUAGE", "")  # fresh process
        assert setup_server._default_language("en-US") == "de"


# ── litclock-dev#848 — the manual-SSID field is gated on the manual option ──


# Runs the setup page's ONE inline <script> against a minimal DOM stub, drives
# the manual-SSID gate through a scenario, and prints snapshots. Same shape as
# the JS-parity harness in test_translator_kit.py: the page's JS is not under
# vitest (vitest covers src/control_server/static/js only), and a source-text
# assertion cannot tell "the field ends up hidden" from "a comment mentions
# hidden". The stub models exactly what the script touches and throws on
# anything else, so a script that starts using a DOM feature the stub lacks
# fails loudly rather than silently passing.
#
# Two stub behaviours are modelled from the browser rather than invented, and
# the tests lean on both:
#   * setting option.selected = true DESELECTS its siblings, as a single
#     <select> does — otherwise selecting the manual option would leave the
#     placeholder selected too and .value would keep answering the old one;
#   * .value returns the SELECTED option's value, or '' when none is selected.
#     A real single-select never sits in that state — it auto-selects the
#     first non-disabled option — and the stub agrees with browsers here only
#     because every rebuild path in the page selects the (disabled)
#     placeholder first, which suppresses that auto-selection. A future
#     rebuild that appends options WITHOUT a leading selected placeholder
#     would diverge: the browser would silently select the first network, the
#     stub would report ''. Keep the placeholder-first discipline.
_GATE_HARNESS = r"""
'use strict';
const fs = require('fs');
const vm = require('vm');
const [, , scriptPath, scenarioPath] = process.argv;
const script = fs.readFileSync(scriptPath, 'utf8');
const scenario = JSON.parse(fs.readFileSync(scenarioPath, 'utf8'));

class Option {
  constructor() {
    this.value = ''; this.textContent = ''; this.disabled = false;
    this.dataset = {}; this._selected = false; this._parent = null;
  }
  get selected() { return this._selected; }
  set selected(v) {
    this._selected = !!v;
    if (this._selected && this._parent) {
      this._parent.children.forEach((o) => { if (o !== this) o._selected = false; });
    }
  }
}
class Select {
  constructor(options) {
    this.children = [];
    options.forEach((spec) => {
      const o = new Option();
      o.value = spec.value; o.disabled = spec.disabled; o._selected = spec.selected;
      if (spec.manual) o.dataset.manual = '1';
      o._parent = this;
      this.children.push(o);
    });
  }
  get options() { return this.children; }
  get value() { const hit = this.children.find((o) => o._selected); return hit ? hit.value : ''; }
  set value(v) {
    let done = false;
    this.children.forEach((o) => { o._selected = !done && o.value === v; if (o._selected) done = true; });
  }
  set innerHTML(v) { if (v !== '') throw new Error("stub supports innerHTML = '' only"); this.children = []; }
  appendChild(o) { o._parent = this; this.children.push(o); }
}
const select = new Select(scenario.options);
const details = { hidden: false, open: scenario.details_open };
const input = {
  value: scenario.input_value, required: scenario.input_required,
  focusCalls: 0, focus() { this.focusCalls += 1; },
};
const byId = { 'wifi-ssid': select, 'manual-ssid': details, 'wifi-ssid-manual': input };
const document = {
  getElementById(id) { return Object.prototype.hasOwnProperty.call(byId, id) ? byId[id] : null; },
  createElement(tag) { if (tag !== 'option') throw new Error('stub: createElement ' + tag); return new Option(); },
};
const listeners = {};
const window = {
  addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
};
let fetchCalls = 0;
function fetch() {
  fetchCalls += 1;
  const f = scenario.fetch;
  if (!f) return Promise.reject(new Error('fetch was not expected in this scenario'));
  if (f.reject) return Promise.reject(new Error('scan failed'));
  return Promise.resolve({ json: () => Promise.resolve(f.networks) });
}
const ctx = vm.createContext({ document, window, fetch, setTimeout, clearTimeout, AbortController, console });
vm.runInContext(script, ctx);  // the load-time pass runs here

function selectedOption() { return select.children.find((o) => o._selected) || null; }
function snapshot(label) {
  const sel = selectedOption();
  return {
    label, select: select.value, options: select.children.map((o) => o.value),
    manualOptions: select.children.filter((o) => o.dataset.manual).length,
    selectedIsManual: !!(sel && sel.dataset.manual),
    hidden: details.hidden, open: details.open, input: input.value,
    required: input.required, focusCalls: input.focusCalls, fetchCalls,
    listeners: Object.keys(listeners),
  };
}
(async () => {
  const out = [];
  for (const [op, arg] of scenario.steps) {
    if (op === 'select') select.value = arg;
    else if (op === 'selectIndex') select.children[arg].selected = true;
    else if (op === 'change') ctx.onSsidChange();
    else if (op === 'type') input.value = arg;
    else if (op === 'refresh') ctx.refreshNetworks();
    else if (op === 'fire') (listeners[arg] || []).forEach((fn) => fn());
    else if (op === 'settle') await new Promise((r) => setTimeout(r, 20));
    else if (op === 'snapshot') out.push(snapshot(arg));
    else throw new Error('unknown step ' + op);
  }
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { console.error(e.stack); process.exit(1); });
"""

_DETAILS_SHUT = '<details id="manual-ssid" style="margin-bottom:14px;">'
_DETAILS_OPEN = '<details id="manual-ssid" style="margin-bottom:14px;" open>'


def _js_function(script, name):
    """The body of one JS function, comments stripped.

    Both halves matter. Slicing to the function's OWN closing brace stops an
    assertion drifting into the next function; stripping `//` comments stops
    it being satisfied by prose — this repo has shipped both mistakes, and a
    gate whose only evidence is a comment describing the gate is precisely
    the failure these tests exist to rule out.
    """
    start = script.index(f"function {name}(")
    open_brace = script.index("{", start)
    depth = 0
    for i in range(open_brace, len(script)):
        if script[i] == "{":
            depth += 1
        elif script[i] == "}":
            depth -= 1
            if depth == 0:
                body = script[open_brace + 1 : i]
                return "\n".join(re.sub(r"//.*$", "", line) for line in body.split("\n"))
    raise AssertionError(f"unbalanced braces in function {name}")


def _page_state(html):
    """Lift the rendered picker's initial state out of the HTML, so the
    executed scenarios start from what the SERVER produced rather than from
    a hand-written approximation of it."""
    import html as html_mod

    select = html[html.index('<select id="wifi-ssid"') : html.index("</select>")]
    options = [
        {
            "value": html_mod.unescape(value),
            "selected": " selected" in attrs,
            "disabled": " disabled" in attrs,
            "manual": 'data-manual="1"' in attrs,
        }
        for value, attrs in re.findall(r'<option value="([^"]*)"([^>]*)>', select)
    ]
    assert _DETAILS_SHUT in html or _DETAILS_OPEN in html, "the <details> tag changed shape"
    field = re.search(r'<input type="text" id="wifi-ssid-manual".*?value="([^"]*)"', html, re.S)
    assert field is not None
    assert html.count("<script>") == 1, "the page grew a second <script>; the harness runs the first"
    script = html[html.index("<script>") + len("<script>") : html.index("</script>")]
    return {
        "options": options,
        "details_open": _DETAILS_OPEN in html,
        "input_value": html_mod.unescape(field.group(1)),
        "input_required": "required" in field.group(0),
    }, script


class TestManualSsidGate:
    """litclock-dev#848: the "type it myself" text box used to be fillable
    alongside a dropdown pick, and the two could disagree — the server's
    precedence rule (a picked network wins; see
    tests/test_wifi_retry_flow.py::TestManualSsidEntry) resolved it silently,
    which is the confusion the issue describes. Now the box is only usable
    while the manual option IS the dropdown's selection.

    Two layers, deliberately: the executed-DOM tests prove the behaviour;
    the source-text tests are the floor that still runs where node is
    absent, and pin the no-JS contract, which no script can prove.
    """

    @pytest.fixture(autouse=True)
    def _stub_scan(self, monkeypatch):
        import sys
        from unittest.mock import MagicMock

        monkeypatch.setattr(setup_server, "_WIFI_SCAN_NETWORKS", None)
        monkeypatch.setattr(setup_server, "_WIFI_SCAN_TIME", 0)
        monkeypatch.setattr(setup_server, "_WIFI_SCAN_SSIDS", frozenset())
        monkeypatch.setattr(setup_server, "WIFI_LAST_MANUAL_SSID", "")
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", False)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", None)
        monkeypatch.setattr(setup_server, "HOTSPOT_SSID", "LitClock-Setup")
        self.scan = [{"ssid": "FakeHomeNet", "signal": 75, "security": "WPA2"}]
        mock_wifi = MagicMock()
        mock_wifi.scan_wifi_networks = lambda: self.scan
        monkeypatch.setitem(sys.modules, "wifi_provision", mock_wifi)

    # ── executed against the page's real script ──

    def _run(self, tmp_path, html, steps, fetch=None):
        import json
        import os
        import shutil

        node = shutil.which("node")
        if node is None:
            # Skipping locally is a convenience; skipping in CI would mean the
            # only executed proof of the gate disappears the day the runner
            # image drops node, silently and with the suite green. The pytest
            # job does not run actions/setup-node — it relies on the image —
            # so the guard belongs here.
            if os.environ.get("CI"):
                pytest.fail("node is missing in CI — the executed-DOM gate checks cannot be skipped there")
            pytest.skip("node not installed — the executed-DOM gate check is dev/CI only")
        state, script = _page_state(html)
        state["steps"] = steps
        state["fetch"] = fetch
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "page.js").write_text(script, encoding="utf-8")
        (tmp_path / "scenario.json").write_text(json.dumps(state), encoding="utf-8")
        (tmp_path / "harness.js").write_text(_GATE_HARNESS, encoding="utf-8")
        result = subprocess.run(
            [node, str(tmp_path / "harness.js"), str(tmp_path / "page.js"), str(tmp_path / "scenario.json")],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"node harness failed:\n{result.stderr}"
        return {snap["label"]: snap for snap in json.loads(result.stdout)}

    def test_clean_load_hides_the_field_and_the_option_reveals_it(self, tmp_path):
        """The 95% case: a scan with networks. The field is not on offer at
        load, appears (open, focused, required) the moment the manual option
        is picked, and the placeholder — the one selection the browser can't
        submit — never shows it."""
        manual = setup_server.MANUAL_SSID_VALUE
        snaps = self._run(
            tmp_path,
            setup_server._build_setup_html(),
            [["snapshot", "load"], ["select", manual], ["change"], ["snapshot", "manual"]],
        )
        assert snaps["load"]["select"] == ""
        assert snaps["load"]["hidden"] is True, "the field must be gated off until the manual option is picked"
        assert snaps["load"]["required"] is False
        assert snaps["load"]["focusCalls"] == 0
        assert snaps["manual"]["hidden"] is False
        assert snaps["manual"]["open"] is True
        assert snaps["manual"]["required"] is True
        assert snaps["manual"]["focusCalls"] == 1, "picking the option must focus the field the user now has to fill"

    def test_picking_a_real_network_hides_and_blanks_the_typed_name(self, tmp_path):
        """The state the issue is about: text in the box AND a network in the
        dropdown. A real pick must take the typed name out of play on the
        client too — a stale entry under a hidden field would POST silently
        and rely on the server's precedence rule alone."""
        manual = setup_server.MANUAL_SSID_VALUE
        snaps = self._run(
            tmp_path,
            setup_server._build_setup_html(),
            [
                ["select", manual],
                ["change"],
                ["type", "MyHiddenNet"],
                ["select", "FakeHomeNet"],
                ["change"],
                ["snapshot", "picked"],
            ],
        )
        assert snaps["picked"]["select"] == "FakeHomeNet"
        assert snaps["picked"]["hidden"] is True
        assert snaps["picked"]["input"] == "", "a real pick must blank the typed name, not just hide it"
        assert snaps["picked"]["required"] is False, "a required field inside a hidden <details> is unsubmittable"
        assert snaps["picked"]["focusCalls"] == 1, "only the manual pick focuses; a network pick must not"

    def test_retry_echo_arrives_usable_without_a_load_time_focus(self, tmp_path, monkeypatch):
        """A failed join of a hand-typed name re-renders with the name echoed
        (litclock-dev#580). The gate must not hide that echo — so the server pre-selects
        the manual option — and must not focus it at load either, which would
        open the keyboard over the form (litclock-dev#671)."""
        monkeypatch.setattr(setup_server, "WIFI_LAST_MANUAL_SSID", "MyHiddenNet")
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", "Incorrect WiFi password")
        snaps = self._run(tmp_path, setup_server._build_setup_html(), [["snapshot", "load"]])
        assert snaps["load"]["select"] == setup_server.MANUAL_SSID_VALUE
        assert snaps["load"]["selectedIsManual"] is True
        assert snaps["load"]["hidden"] is False
        assert snaps["load"]["open"] is True
        assert snaps["load"]["input"] == "MyHiddenNet"
        assert snaps["load"]["focusCalls"] == 0

    def test_empty_scan_arrives_usable_and_required(self, tmp_path):
        """No networks: typing the name is the only way forward (litclock-dev#554/litclock-dev#615),
        so the page must arrive with the manual option selected and the field
        showing — under the gate, one implies the other. `required` is the
        other half: on this page Submit with only a password would otherwise
        POST an empty name and come back as a full-page error that loses the
        password the user just typed."""
        self.scan = []
        snaps = self._run(tmp_path, setup_server._build_setup_html(), [["snapshot", "load"]])
        assert snaps["load"]["select"] == setup_server.MANUAL_SSID_VALUE
        assert snaps["load"]["hidden"] is False
        assert snaps["load"]["open"] is True
        assert snaps["load"]["required"] is True

    def test_refresh_into_an_empty_scan_selects_the_option_and_shows_the_field(self, tmp_path):
        """The AJAX twin of the empty-scan render (litclock-dev#615): a Refresh
        that returns [] must leave the user with the field they need, not a
        rebuilt dropdown on its placeholder and the field gated away."""
        snaps = self._run(
            tmp_path,
            setup_server._build_setup_html(),
            [["refresh"], ["settle"], ["snapshot", "after"]],
            fetch={"networks": []},
        )
        assert snaps["after"]["fetchCalls"] == 1
        assert snaps["after"]["select"] == setup_server.MANUAL_SSID_VALUE
        assert snaps["after"]["options"] == ["", setup_server.MANUAL_SSID_VALUE]
        assert snaps["after"]["hidden"] is False
        assert snaps["after"]["open"] is True
        assert snaps["after"]["required"] is True

    def test_refresh_keeps_a_selected_manual_option_and_its_typed_name(self, tmp_path):
        """A hidden-network owner who has already picked the option and typed
        the name, then taps Refresh to double-check, must not watch the name
        vanish: the rebuild re-selects the option and the gate keeps the
        field. The mirror case — Refresh with a real network picked — lands
        on the placeholder with the field gated off, and the name typed
        earlier is left alone (a Refresh is not a decision the user made
        about it)."""
        manual = setup_server.MANUAL_SSID_VALUE
        nets = {"networks": [{"ssid": "FakeHomeNet", "signal": 75, "security": "WPA2"}]}
        kept = self._run(
            tmp_path,
            setup_server._build_setup_html(),
            [
                ["select", manual],
                ["change"],
                ["type", "MyHiddenNet"],
                ["refresh"],
                ["snapshot", "scanning"],
                ["settle"],
                ["snapshot", "after"],
            ],
            fetch=nets,
        )
        assert kept["scanning"]["select"] == manual, "the option must stay selected while the scan runs"
        assert kept["scanning"]["hidden"] is False
        assert kept["after"]["options"] == ["", "FakeHomeNet", manual]
        assert kept["after"]["select"] == manual
        assert kept["after"]["hidden"] is False
        assert kept["after"]["input"] == "MyHiddenNet"

        dropped = self._run(
            tmp_path / "mirror",
            setup_server._build_setup_html(),
            [["select", "FakeHomeNet"], ["change"], ["refresh"], ["settle"], ["snapshot", "after"]],
            fetch=nets,
        )
        assert dropped["after"]["select"] == ""
        assert dropped["after"]["hidden"] is True

    def test_picking_manual_DURING_a_scan_survives_the_response(self, tmp_path):
        """A scan runs 2-20s on real hardware and the dropdown stays live the
        whole time. Sampling "was the manual option selected?" only at the
        START loses the user who taps Refresh, sees nothing useful, picks
        "type it myself" and begins typing — the response then lands, rebuilds
        the dropdown to its placeholder, and the gate hides the field they are
        typing into, text intact but invisible. All three review passes found
        this independently."""
        manual = setup_server.MANUAL_SSID_VALUE
        snaps = self._run(
            tmp_path,
            setup_server._build_setup_html(),
            [
                ["refresh"],
                ["select", manual],
                ["change"],
                ["type", "MyHiddenNet"],
                ["snapshot", "mid"],
                ["settle"],
                ["snapshot", "after"],
            ],
            fetch={"networks": [{"ssid": "FakeHomeNet", "signal": 75, "security": "WPA2"}]},
        )
        assert snaps["mid"]["hidden"] is False
        assert snaps["after"]["select"] == manual, "the pick made DURING the scan must survive the rebuild"
        assert snaps["after"]["hidden"] is False
        assert snaps["after"]["input"] == "MyHiddenNet"
        assert snaps["after"]["required"] is True

    def test_a_sentinel_named_ap_never_steals_the_manual_selection(self, tmp_path):
        """MANUAL_SSID_VALUE's own comment allows that an AP could broadcast
        the sentinel name. It renders among the scanned networks, AHEAD of the
        real manual option, so selecting by value would select the neighbour's
        AP: the submit still reaches the typed name (the server reads the
        submitted value as the sentinel either way) while the dropdown names a
        different network — the exact ambiguity litclock-dev#848 removes. Selection goes
        by the data-manual marker instead."""
        manual = setup_server.MANUAL_SSID_VALUE
        snaps = self._run(
            tmp_path,
            setup_server._build_setup_html(),
            [
                ["select", manual],
                ["change"],
                ["type", "MyHiddenNet"],
                ["refresh"],
                ["settle"],
                ["snapshot", "after"],
            ],
            fetch={"networks": [{"ssid": manual, "signal": 90, "security": "WPA2"}]},
        )
        # Two options now carry the sentinel VALUE; exactly one is the real
        # manual option, and it is the one that must end up selected.
        assert snaps["after"]["options"] == ["", manual, manual]
        assert snaps["after"]["manualOptions"] == 1
        assert snaps["after"]["selectedIsManual"] is True, "the scanned AP stole the selection"
        assert snaps["after"]["hidden"] is False
        assert snaps["after"]["input"] == "MyHiddenNet"

    def test_a_failed_refresh_still_runs_the_gate(self, tmp_path):
        """The catch branch rebuilds the dropdown too; a rebuild the gate
        never sees would leave the field's visibility describing the list
        from before the scan."""
        manual = setup_server.MANUAL_SSID_VALUE
        snaps = self._run(
            tmp_path,
            setup_server._build_setup_html(),
            [["select", manual], ["change"], ["refresh"], ["settle"], ["snapshot", "after"]],
            fetch={"reject": True},
        )
        assert snaps["after"]["fetchCalls"] == 1
        assert snaps["after"]["options"] == ["", manual]
        assert snaps["after"]["select"] == manual
        assert snaps["after"]["hidden"] is False

    def test_a_bfcache_restore_reruns_the_gate(self, tmp_path):
        """iOS Safari restores form state AFTER the load-time pass has run, so
        a back-navigation onto a page it puts back on the manual option would
        show the gate's verdict for the other selection: field hidden, name
        still in it. pageshow fires on both a fresh load and a restore."""
        manual = setup_server.MANUAL_SSID_VALUE
        snaps = self._run(
            tmp_path,
            setup_server._build_setup_html(),
            [
                ["snapshot", "load"],
                ["select", manual],
                ["snapshot", "restored"],
                ["fire", "pageshow"],
                ["snapshot", "after"],
            ],
        )
        assert snaps["load"]["listeners"] == ["pageshow"]
        assert snaps["load"]["hidden"] is True
        # The restore itself changes the selection without firing onchange —
        # that is the whole problem — so the page is momentarily inconsistent.
        assert snaps["restored"]["hidden"] is True
        assert snaps["after"]["hidden"] is False
        assert snaps["after"]["required"] is True
        assert snaps["after"]["focusCalls"] == 0, "a restore must not open the keyboard"

    # ── source-text floor, and the no-JS contract ──

    def test_the_server_never_renders_the_disclosure_hidden(self):
        """THE no-JS invariant. Captive-portal WebViews routinely run with
        JavaScript off, and the repo's rule is that the form still works
        there (CLAUDE.md, "No-JS form fallback"). The gate is therefore a
        load-time SCRIPT decision: the markup must arrive visible in every
        render state — including the two that PRE-SELECT the manual option,
        where a hidden disclosure would leave the no-JS user with a selected
        option and no field to fill.

        `required` is checked here too, and for the opposite reason: with JS
        off nothing can take it off again, and a required control inside a
        collapsed <details> makes the whole form silently unsubmittable.
        """
        # All four render states, including the two that PRE-SELECT the
        # manual option — those are the ones a hidden disclosure would strand.
        # The fixture already monkeypatched these globals, so assigning here
        # overwrites its value and its undo still restores the real one.
        states = [
            ("clean", "", None, self.scan),
            ("echo", "MyHiddenNet", "Incorrect WiFi password", self.scan),
            ("error-only", "", "Incorrect WiFi password", self.scan),
            ("empty-scan", "", None, []),
        ]
        for label, last_manual, error, scan in states:
            setup_server.WIFI_LAST_MANUAL_SSID = last_manual
            setup_server.WIFI_CONNECT_ERROR = error
            self.scan = scan
            # Cold the scan cache per state. Without this the earlier states'
            # successful scans stay cached for the TTL and the "empty-scan"
            # state silently renders the POPULATED page — which is how this
            # very state passed against a server that marked the field
            # `required` on exactly the empty-scan render (measured).
            setup_server._WIFI_SCAN_NETWORKS = None
            setup_server._WIFI_SCAN_TIME = 0
            html = setup_server._build_setup_html()
            # Scoped to the <select>: the empty-scan copy also appears in the
            # script's own Refresh branch, so `in html` is true on every page.
            picker = html[html.index('<select id="wifi-ssid"') : html.index("</select>")]
            empty_page = setup_server._wifi_placeholder_empty_text() in picker
            assert empty_page == (scan == []), f"[{label}] is not the render state it claims to be"
            tag = html[html.index("<details") : html.index(">", html.index("<details")) + 1]
            assert not re.search(r"\bhidden\b", tag), f"[{label}] the server must not hide the disclosure: {tag}"
            assert "display:none" not in tag and "display: none" not in tag
            assert _DETAILS_SHUT in html or _DETAILS_OPEN in html
            field = re.search(r'<input type="text" id="wifi-ssid-manual".*?>', html, re.S).group(0)
            assert not re.search(r"\brequired\b", field), f"[{label}] server-side required bricks the no-JS form"
        # And the hiding happens at LOAD, from script: the bare call and the
        # bfcache listener are the script's last two statements, after the
        # elements exist.
        self.scan = [{"ssid": "FakeHomeNet", "signal": 75, "security": "WPA2"}]
        html = setup_server._build_setup_html()
        script = html[html.index("<script>") : html.index("</script>")]
        statements = [
            line.strip()
            for line in re.sub(r"//.*", "", script).split("\n")
            if line.strip() and not line.strip().startswith("<")
        ]
        assert statements[-2:] == [
            "syncManualSsid();",
            "window.addEventListener('pageshow', syncManualSsid);",
        ], f"the load-time pass and the pageshow listener must close the script, got {statements[-2:]}"

    def test_the_gate_binds_to_the_rendered_ids(self):
        """A rename of any of the three ids on either side leaves the gate
        looking up nothing and silently returning — the field would then be
        visible with JS on, which is the pre-litclock-dev#848 page."""
        html = setup_server._build_setup_html()
        script = html[html.index("<script>") : html.index("</script>")]
        body = _js_function(script, "syncManualSsid")
        for element_id in ("wifi-ssid", "manual-ssid", "wifi-ssid-manual"):
            assert f'id="{element_id}"' in html, f"markup lost id={element_id}"
            assert f"getElementById('{element_id}')" in body, f"syncManualSsid stopped looking up {element_id}"
        assert "details.hidden = !manual" in body
        assert "input.required = manual" in body
        # Every path that changes the dropdown ends in the gate. Asserted
        # against comment-stripped function BODIES: the same claims made
        # against the whole page were satisfiable by the comments that
        # explain them.
        assert "syncManualSsid();" in _js_function(script, "onSsidChange")
        assert "syncManualSsid();" in _js_function(script, "finishRebuild")
        refresh = _js_function(script, "refreshNetworks")
        assert refresh.count("finishRebuild(select,") == 4, "interim, empty, success and failure rebuilds"
        assert 'onchange="onSsidChange()"' in html

    def test_the_manual_option_is_selected_by_marker_not_by_value(self):
        """The source half of the sentinel-named-AP case: both builders mark
        the option, and the selector reads the marker. A `select.value = ...`
        here would pick whichever option holds that value first."""
        html = setup_server._build_setup_html()
        script = html[html.index("<script>") : html.index("</script>")]
        assert 'data-manual="1"' in html, "the server-rendered option lost its marker"
        assert "opt.dataset.manual = '1';" in _js_function(script, "appendManualOption")
        finder = _js_function(script, "manualOption")
        assert "dataset.manual" in finder
        chooser = _js_function(script, "selectManualOption")
        assert "manualOption(select)" in chooser
        assert "select.value =" not in chooser

    def test_build_wifi_options_select_manual_moves_the_selection(self):
        """Exactly one option is ever selected, and select_manual moves that
        from the placeholder to the manual option — leaving the placeholder
        disabled, so the retry/empty-scan page still cannot submit it."""
        nets = [{"ssid": "HomeWiFi", "signal": 80, "security": "WPA2"}]
        default = setup_server._build_wifi_options(nets)
        moved = setup_server._build_wifi_options(nets, select_manual=True)
        assert default.count(" selected") == 1 and moved.count(" selected") == 1
        assert '<option value="" selected disabled>' in default
        assert f'<option value="{setup_server.MANUAL_SSID_VALUE}" data-manual="1">' in default
        assert '<option value="" disabled>' in moved
        assert f'<option value="{setup_server.MANUAL_SSID_VALUE}" data-manual="1" selected>' in moved
        assert 'value="HomeWiFi"' in moved

    def test_an_empty_list_selects_the_manual_option_whatever_the_caller_asked(self):
        """The rule lives at the site that knows: with no networks the manual
        option is the only one `required` accepts, so a caller that forgot the
        flag would render a page whose only usable control is gated off."""
        forced = setup_server._build_wifi_options([], select_manual=False)
        assert f'<option value="{setup_server.MANUAL_SSID_VALUE}" data-manual="1" selected>' in forced
        assert " selected disabled>" not in forced

    def test_retry_echo_preselects_the_manual_option_and_a_clean_render_does_not(self, monkeypatch):
        clean = setup_server._build_setup_html()
        assert f'<option value="{setup_server.MANUAL_SSID_VALUE}" data-manual="1">' in clean
        assert '<option value="" selected disabled>' in clean
        monkeypatch.setattr(setup_server, "WIFI_LAST_MANUAL_SSID", "MyHiddenNet")
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", "Incorrect WiFi password")
        echoed = setup_server._build_setup_html()
        assert f'<option value="{setup_server.MANUAL_SSID_VALUE}" data-manual="1" selected>' in echoed
        assert '<option value="" selected disabled>' not in echoed
        assert 'value="MyHiddenNet"' in echoed

    def test_the_echo_is_read_once_for_both_the_selection_and_the_value(self):
        """Two unlocked reads straddled _wifi_network_options, which can sit
        seconds inside an nmcli rescan: a connect thread storing or clearing
        the echo in that window rendered a dropdown and a text box that
        disagreed. One read, into a local."""
        src = pathlib.Path(setup_server.__file__).read_text()
        body = src[src.index("def _build_setup_html") : src.index("HTML_SUCCESS =")]
        assert body.count("WIFI_LAST_MANUAL_SSID") == 1, "the global must be read exactly once per render"
        assert "last_manual = WIFI_LAST_MANUAL_SSID" in body
        assert "select_manual=bool(last_manual)" in body
        assert "html.escape(last_manual)" in body

    def test_empty_scan_preselects_the_manual_option(self):
        """The cache-miss empty branch is its own return site in
        _wifi_network_options."""
        self.scan = []
        options, was_empty = setup_server._wifi_network_options()
        assert was_empty is True
        assert f'<option value="{setup_server.MANUAL_SSID_VALUE}" data-manual="1" selected>' in options
        assert setup_server._wifi_placeholder_empty_text() in options
        assert " selected disabled>" not in options

    def test_a_connect_error_alone_does_not_preselect_the_manual_option(self, monkeypatch):
        """A list-picked join that failed re-renders with the banner and the
        disclosure force-open (litclock-dev#580, kept for the no-JS page) — but nothing
        was typed, so the dropdown must NOT arrive on the manual option. With
        JS on, the gate then hides the field until the user asks for it,
        which is the litclock-dev#848 contract."""
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", "Incorrect WiFi password")
        html = setup_server._build_setup_html()
        assert '<option value="" selected disabled>' in html
        assert f'<option value="{setup_server.MANUAL_SSID_VALUE}" data-manual="1">' in html
        assert _DETAILS_OPEN in html

    def test_a_cleared_echo_renders_no_selection_and_no_value(self, monkeypatch):
        """The render consequence of the do_POST clear (pinned at its source
        in tests/test_wifi_retry_flow.py). A stale name left in the global
        would arrive SELECTED since litclock-dev#848, so a password-only resubmit on the
        retry page would join it — see that test for the full sequence."""
        monkeypatch.setattr(setup_server, "WIFI_LAST_MANUAL_SSID", "")
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", "Incorrect WiFi password")
        html = setup_server._build_setup_html()
        assert '<option value="" selected disabled>' in html
        assert 'id="wifi-ssid-manual"' in html and 'value=""' in html
        assert "OldHidden" not in html


# ── litclock-dev#844 item 3 — reset_state's re-snapshot loop is CAPPED ──


def test_reset_state_reports_a_deep_spawn_chain_instead_of_chasing_it():
    """``reset_state`` re-snapshots ``_BG_THREADS`` so a thread appended
    DURING the join is still joined (litclock-dev#786), and caps the passes
    at ``_RESET_JOIN_MAX_PASSES`` so a spawner that keeps appending cannot
    make the reset chase it. The sibling test in test_sigterm_absorber.py
    covers one nesting level; this is the cap: a chain deeper than the cap
    must be REPORTED in the escape tuple, not followed to its end.

    The chain is deterministic, not raced. Each link is itself tracked, so
    the reset is blocked joining link N at the moment N spawns N+1 and every
    link lands in a fresh pass; and the link the cap strands — the one whose
    name the escape tuple must carry — blocks on an Event instead of
    sleeping, so it is still alive at prune time no matter how the scheduler
    behaves. With a sleep there, a 150ms stall between the join loop and the
    prune let the tail finish and spawn ITS successor, and the reported name
    became probe-chain-5 (measured).

    ``4`` below is a deliberate literal, not ``_RESET_JOIN_MAX_PASSES``: a
    mutant that changes the cap changes the constant with it, and a test that
    derived its expectation from the constant would follow the mutant (``= 1``
    and ``= 1000`` both stay green that way; measured).
    """
    import time

    depth = setup_server._RESET_JOIN_MAX_PASSES + 2
    link_done = [threading.Event() for _ in range(depth)]
    release = threading.Event()
    started = []

    def _link(i):
        def run():
            started.append(i)
            # Links inside the cap hand over to the next one while the reset
            # is joining them; the first link the cap strands waits instead.
            if i < setup_server._RESET_JOIN_MAX_PASSES:
                time.sleep(0.1)
            else:
                release.wait(5.0)
            if i + 1 < depth:
                setup_server._spawn_bg(_link(i + 1), name=f"probe-chain-{i + 1}")
            link_done[i].set()

        return run

    previous_in_flight = setup_server.WIFI_CONNECT_IN_FLIGHT
    try:
        setup_server.WIFI_CONNECT_IN_FLIGHT = False
        setup_server._spawn_bg(_link(0), name="probe-chain-0")

        t0 = time.monotonic()
        escaped = setup_server.reset_state(wait_for_inflight=2.0)
        elapsed = time.monotonic() - t0

        assert elapsed < 2.0, f"reset_state ran past its own budget ({elapsed:.2f}s)"
        # Links 0..3 were joined — one per pass — so the injection point was
        # reached four times; link 4 is the one left running when the cap
        # stopped the loop, and it is the one the tuple must name.
        assert [link_done[i].is_set() for i in range(4)] == [True] * 4, started
        assert escaped == ("probe-chain-4",), f"the tail must be REPORTED, got {escaped!r} (started={started})"
        assert not link_done[depth - 1].is_set(), "reset_state followed the chain past the cap"
    finally:
        release.set()
        for ev in link_done:
            ev.wait(2.0)
        setup_server.WIFI_CONNECT_IN_FLIGHT = previous_in_flight
        setup_server.reset_state(wait_for_inflight=2.0)
