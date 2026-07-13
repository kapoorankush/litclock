"""Tests for setup_server module — IP-geo resolver, timezone helper, captive portal API."""

import subprocess

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


# ── _update_env_location (EPIC #383 kwargs API) ───────────────────────


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
        """T3 / #380 regression: WEATHER_LOCATION_NAME must be written when
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

        A valid timezone is passed so the call clears the #393 "coords without
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
        """#393: coordinates must NOT be persisted without a resolved timezone.
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
        """#393 (review follow-up): a single-axis coord (lat present, lon
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
        """#393 guard must NOT block the PATCH-semantics tz-only write. The
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


# ── _resolve_location_from_ip (EPIC #383 IP-geo retry pipeline) ──────


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

        #337 A4 extracted the resolver to location_resolver.py; the writer
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
        """#393: when ip-api returns coords with no timezone AND the offline
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


# ── _build_setup_html regression (T9 — EPIC #383 hotspot HTML shape) ──


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
        assert "/geocode" not in src, "/geocode endpoint should be removed in EPIC #383"


# ── WiFi scan caching ─────────────────────────────────────────────


class TestWifiScanCaching:
    def setup_method(self):
        # Reset cache state before each test
        setup_server._WIFI_SCAN_CACHE = None
        setup_server._WIFI_SCAN_TIME = 0

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

        result1 = setup_server._wifi_network_options()
        result2 = setup_server._wifi_network_options()

        assert result1 == result2
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

        result = setup_server._wifi_network_options()
        assert "No networks found" in result
        assert setup_server._WIFI_SCAN_CACHE is None

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

        result = setup_server._wifi_network_options()
        assert "LitClock-Setup" not in result
        assert "HomeWiFi" in result

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

        result = setup_server._wifi_network_options()
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

        result = setup_server._wifi_network_options()
        assert "No networks found" in result
        assert "LitClock-Setup" not in result
        assert setup_server._WIFI_SCAN_CACHE is None

    def test_scan_wifi_endpoint_filters_own_hotspot(self, monkeypatch):
        """The /scan-wifi handler must filter the hotspot from BOTH the JSON it
        returns (the client rebuilds the dropdown from it on Refresh) AND the
        shared _WIFI_SCAN_CACHE it populates (feeds the server-rendered
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
        assert setup_server._WIFI_SCAN_CACHE is not None
        assert "LitClock-Setup" not in setup_server._WIFI_SCAN_CACHE
        assert "HomeWiFi" in setup_server._WIFI_SCAN_CACHE


# ── HTML_ERROR template ────────────────────────────────────────────


class TestHtmlError:
    def test_retry_link_present(self):
        """Error page contains a retry link that works without JS."""
        rendered = setup_server.HTML_ERROR.format(error="Test error")
        assert 'href="/"' in rendered
        assert 'id="retry-link"' in rendered
        assert "Try again" in rendered

    def test_loading_feedback_script(self):
        """Error page includes JS for loading feedback (progressive enhancement)."""
        rendered = setup_server.HTML_ERROR.format(error="Test error")
        assert "Loading..." in rendered
        assert "addEventListener" in rendered


# ── Captive portal bridge (iOS CNA) ────────────────────────────────


class TestCnaBridge:
    def test_bridge_contains_setup_link(self):
        """Bridge HTML links into the real setup form via SETUP_HOSTNAME."""
        html = setup_server._build_cna_bridge_html()
        assert f'href="http://{setup_server.SETUP_HOSTNAME}/setup"' in html
        assert "Open Setup" in html

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
        """#482: on-screen setup copy must not name Safari (wrong on Android /
        any non-iOS phone) — it should point at the generic 'browser'. Also
        must not quote a specific button label (/review: iOS's actual CNA
        escape button says "Open in Safari", so a quoted "Open in Browser"
        would misdescribe it — keep the instruction generic instead)."""
        html = setup_server._build_cna_bridge_html()
        assert "Safari" not in html
        assert "browser" in html.lower()

    def test_setup_hostname_uses_fake_tld(self):
        """Hostname must have a TLD so Safari treats it as a URL, not a search query."""
        assert "." in setup_server.SETUP_HOSTNAME
        assert setup_server.SETUP_HOSTNAME == "litclock.setup"


# ── WiFi error banner ─────────────────────────────────────────────


class TestWifiErrorBanner:
    def test_banner_mentions_rescan_fallback(self, monkeypatch):
        """The red error banner must tell the user what to do if the setup
        page doesn't auto-reload — phones auto-disconnect from the hotspot
        during the failed WiFi attempt, so the browser won't see the error
        until they rescan the QR code and rejoin. Without this instruction
        the user is stuck staring at a stale loading page."""
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", False)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", "wrong password")
        html = setup_server._build_setup_html()
        assert "rescan" in html.lower()
        assert "qr" in html.lower()
        assert "hotspot has restarted" in html.lower()

    def test_banner_distinguishes_wifi_from_hotspot_password(self, monkeypatch):
        """The banner must explicitly call out the WiFi password vs the
        hotspot password so a user staring at the visible hotspot password
        on the clock doesn't type that one into the WiFi password field.

        EPIC #383 dropped the 'home' qualifier (which was scary jargon for
        non-tech users — issue #384) but the WiFi-vs-hotspot disambiguation
        is still load-bearing copy."""
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", False)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", "wrong password")
        html = setup_server._build_setup_html()
        assert "wifi password" in html.lower()
        assert "hotspot password shown on the clock" in html.lower()
        # Regression: the dropped "home" qualifier must not creep back in.
        assert "home wifi" not in html.lower()

    def test_wifi_form_field_label_no_home_qualifier(self, monkeypatch):
        """The WiFi password input label must say 'Your WiFi Password' (no
        'Home' qualifier per #384). The hotspot-vs-WiFi cue lives in the
        error banner, not the field label."""
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", False)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", None)
        html = setup_server._build_setup_html()
        assert "Your WiFi Password" in html
        assert "Home WiFi" not in html


class TestSetupPagePickerCopy:
    """Issue #398: hardware QA with a non-tech user showed they did not
    realize they were supposed to pick their OWN home/office WiFi from
    the dropdown — the page gave no guidance, and "LitClock-Setup" looked
    like a selectable option. The code half (hotspot filter) shipped in
    #397; this class pins the copy half so future polish doesn't regress
    the orienting cues a first-time non-tech user depends on."""

    @pytest.fixture(autouse=True)
    def _clean_wifi_scan_state(self, monkeypatch):
        """Reset module-level WiFi scan state + stub the scanner. Without
        this, `_wifi_network_options()` (called by `_build_setup_html` in
        provisioning mode) reads the shared `_WIFI_SCAN_CACHE` global —
        which earlier tests may have populated with stale options — and
        on a cache miss falls through to the real `wifi_provision.
        scan_wifi_networks()`, which calls nmcli (slow / flaky on a dev
        box). Mirrors the pattern in `TestWifiNetworkScan`."""
        import sys
        from unittest.mock import MagicMock

        monkeypatch.setattr(setup_server, "_WIFI_SCAN_CACHE", None)
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

    def test_subtitle_drops_wifi_framing_in_pre_connected_path(self, monkeypatch):
        """The pre-connected path (boot with WiFi already configured via
        ethernet/wpa_supplicant) renders the same page with no WiFi section
        — just a Complete Setup button. The subtitle must drop the WiFi
        framing here, otherwise it reads wrong ("Connect your clock to
        WiFi" with no WiFi picker visible)."""
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", False)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", False)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", None)
        html = setup_server._build_setup_html()
        assert "phone normally uses" not in html.lower()
        # Pin the FULL pre-connected subtitle string. The previous
        # `"literary clock" in html.lower()` was too loose — that
        # substring also appears in the <title> and <h1>, so the
        # assertion would pass even if the subtitle vanished entirely.
        assert "Finish setting up your literary clock." in html

    def test_picker_section_explains_which_wifi(self, monkeypatch):
        """An explainer next to the dropdown must tell the user which
        network this is — explicitly NOT the LitClock-Setup hotspot. The
        hotspot SSID is filtered from the dropdown (#397), but a user
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
        # The explainer must call out the LitClock-Setup hotspot by name,
        # not just "the hotspot" (which is hand-wavy for a first-time user).
        # Scope the assertion to the WiFi section using the surrounding
        # HTML comments as boundaries (the section has nested <div>s, so
        # a non-greedy </div> regex stops at the wrong close tag).
        start = html.find("<!-- WiFi Section -->")
        end = html.find("<!-- Submit -->", start)
        assert start != -1 and end != -1, "WiFi/Submit markers missing — boundaries unclear"
        section = re.sub(r"\s+", " ", html[start:end])
        assert "LitClock-Setup hotspot" in section, "picker explainer must name the hotspot inside the WiFi section"
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
        assert "Brand &amp; Co Setup hotspot" in section, "custom SSID must appear in the explainer, HTML-escaped"
        # And the raw (unescaped) ampersand must NOT leak — that would
        # signal a missing escape.
        assert "Brand & Co Setup hotspot" not in section, "raw & in SSID must be HTML-escaped before interpolation"

    def test_password_helper_calls_out_hotspot_disambiguation(self, monkeypatch):
        """The helper under the password input must call out the
        hotspot-vs-WiFi password distinction up-front, not just in the
        error banner. The visible hotspot password is on the e-ink in
        front of the user; typing it in is the natural first attempt.
        Wording must align with the error banner's "hotspot password
        shown on the clock" so the two surfaces don't drift."""
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", False)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", None)
        html = setup_server._build_setup_html()
        # Aligned with the error banner's exact phrasing — both surfaces
        # describe the SAME password to the SAME user; divergent wording
        # ("temporary" vs "hotspot") would confuse a non-tech user who
        # sees both on a failed-then-retry sequence.
        assert "hotspot password shown on the clock" in html.lower()
        # And it must keep the open-network escape hatch (a fraction of
        # users do have unsecured WiFi).
        assert "open networks" in html.lower()


# ── Captive portal routing & response headers ─────────────────────


def _make_handler():
    """Build a SetupHandler without going through BaseHTTPRequestHandler.__init__
    (which requires a real socket). Tests mock the wire-level send_* methods."""
    from unittest.mock import MagicMock

    handler = setup_server.SetupHandler.__new__(setup_server.SetupHandler)
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
        assert f"http://{setup_server.SETUP_HOSTNAME}/setup" in sent_html

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

    def test_probe_logs_host_ua_path_diagnostic_in_provisioning(self, capsys, monkeypatch):
        """#483: the access log records only the request line + status, so a
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

    def test_probe_diagnostic_silent_outside_provisioning(self, capsys, monkeypatch):
        """The diagnostic is provisioning-only — normal-mode HTTPS setup must
        not spam stdout (and must not touch self.headers when not logging)."""
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

    def test_provisioning_mode_off_skips_probe_dispatch(self, monkeypatch):
        """Normal (post-setup) HTTPS server mode must never enter probe
        dispatch. Cross-verifying the PROVISIONING_MODE guard."""
        monkeypatch.setattr(setup_server, "PROVISIONING_MODE", False)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", False)
        monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", None)
        handler = _make_do_get_handler("/", "captive.apple.com")
        handler.do_GET()
        handler.send_html.assert_called_once()
        sent = handler.send_html.call_args[0][0]
        # Normal mode serves the setup form; bridge must NOT appear
        assert "Open Setup" not in sent, "normal mode must not serve the bridge even to probe hosts"


# ── _restore_hotspot retry display hook ────────────────────────────


class TestRestoreHotspot:
    """The retry e-ink display refresh is the user's primary signal during
    a failed-WiFi-password retry. These tests pin the behavior so a future
    refactor doesn't silently regress the UX."""

    def test_restore_hotspot_refreshes_display_with_retry_reason(self, monkeypatch):
        """Happy path: hotspot restore succeeds, display_hotspot_info is
        called with retry_reason=HOTSPOT_RETRY_WIFI_PASSWORD."""
        import sys
        from unittest.mock import MagicMock

        monkeypatch.setattr(setup_server, "HOTSPOT_SSID", "LitClock-Setup")
        monkeypatch.setattr(setup_server, "HOTSPOT_PASSWORD", "abc12345")

        create_hotspot = MagicMock(return_value={"ssid": "x", "password": "y", "ip": "z"})

        mock_eink = MagicMock()
        mock_eink.HOTSPOT_RETRY_WIFI_PASSWORD = "wifi_password"
        monkeypatch.setitem(sys.modules, "eink_display", mock_eink)

        setup_server._restore_hotspot(create_hotspot)

        create_hotspot.assert_called_once_with(ssid="LitClock-Setup", password="abc12345")
        mock_eink.display_hotspot_info.assert_called_once()
        kwargs = mock_eink.display_hotspot_info.call_args.kwargs
        assert kwargs.get("retry_reason") == "wifi_password"

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


# ── _schedule_self_terminate helper (#364) ──────────────────────────


class TestScheduleSelfTerminate:
    """Unit tests for the SIGTERM-scheduling helper introduced in #364.

    Centralizes the daemon-thread + os.kill pattern so the no-WiFi branch
    and the WiFi-connect success path share one tested implementation
    instead of each carrying its own bare ``os.kill`` call.
    """

    def test_delay_zero_calls_os_kill_synchronously(self, monkeypatch):
        """delay=0 path signals from the calling thread — no new thread spawn.

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
            setup_server._schedule_self_terminate(delay=0.0)
            mock_thread.assert_not_called()

        assert kill_calls == [(os.getpid(), signal_mod.SIGTERM)]

    def test_delay_nonzero_spawns_daemon_thread(self, monkeypatch):
        """delay>0 path spawns a daemon thread and returns immediately."""
        from unittest.mock import patch

        monkeypatch.setattr("os.kill", lambda pid, sig: None)

        with patch("setup_server.threading.Thread") as mock_thread:
            setup_server._schedule_self_terminate(delay=0.1)
            mock_thread.assert_called_once()
            _, kwargs = mock_thread.call_args
            assert kwargs.get("daemon") is True
            # Thread.start() was called on the returned instance
            mock_thread.return_value.start.assert_called_once()

    def test_delay_nonzero_calls_os_kill_after_sleep(self, monkeypatch):
        """End-to-end: delay path eventually invokes os.kill with the right args."""
        import os
        import signal as signal_mod
        import threading
        import time

        kill_event = threading.Event()
        kill_calls = []

        def mock_kill(pid, sig):
            kill_calls.append((pid, sig))
            kill_event.set()

        monkeypatch.setattr("os.kill", mock_kill)

        setup_server._schedule_self_terminate(delay=0.05)

        # Helper returned immediately; the sleep happens in the daemon thread.
        assert not kill_event.is_set(), "os.kill should not have fired synchronously"
        # Wait for the daemon thread's sleep to complete (with slack for CI jitter).
        assert kill_event.wait(timeout=2.0), "os.kill never fired after delay"
        # Sanity: helper returns before the sleep completes.
        # (Already proved by the sequence above — the assertion is the wait timeout.)
        del time  # silence unused-import lint
        assert kill_calls == [(os.getpid(), signal_mod.SIGTERM)]

    def test_signals_current_pid_and_sigterm(self, monkeypatch):
        """Regardless of delay path, signal target is (getpid(), SIGTERM)."""
        import os
        import signal as signal_mod
        import threading

        kill_event = threading.Event()
        kill_calls = []

        def mock_kill(pid, sig):
            kill_calls.append((pid, sig))
            kill_event.set()

        monkeypatch.setattr("os.kill", mock_kill)

        # delay=0 path
        setup_server._schedule_self_terminate(delay=0.0)
        assert kill_calls[-1] == (os.getpid(), signal_mod.SIGTERM)

        # delay>0 path
        kill_event.clear()
        setup_server._schedule_self_terminate(delay=0.01)
        assert kill_event.wait(timeout=2.0)
        assert kill_calls[-1] == (os.getpid(), signal_mod.SIGTERM)


# ── signal_completion bool return (#364 D4, codex post-review fix) ──


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


# ── Structural / regression-prevention pins (#364) ──────────────────


class TestSetupServerStructuralInvariants:
    """Pins that future refactors cannot silently re-introduce the bug
    class fixed in #364. These run against the on-disk source — they
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
            f"invariant (#364)."
        )

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
        assert "#364" in doc, "helper docstring must reference issue #364"
        assert "reset_state" in doc, "helper docstring must reference reset_state"
        assert "IN_FLIGHT" in doc, "helper docstring must reference IN_FLIGHT"
