"""Tests for src/location_resolver.py (#337 A4 extraction + A1/A6/A6.1/A7/A12/A15 behaviors).

The most important test in this file is ``test_main_no_ops_when_mode_specific``:
the on-boot reresolve oneshot MUST NEVER overwrite env.sh when the user has
chosen Specific mode in the PWA. A silent overwrite on every reboot would
defeat the entire reason ``WEATHER_LOCATION_MODE`` exists. Per the skill's
iron rule, this regression test ships as P1 CRITICAL alongside the extraction
itself.

The atomicity tests (``test_failed_geocode_leaves_env_byte_identical``) pin
the A15 contract: a save that fails partway through (geocode error, junk
coords) must leave env.sh untouched on disk. The expensive way to break this
is to half-write env.sh and corrupt the next reboot's state.
"""

from __future__ import annotations

import os

import pytest

import config
import geocoding
import location_resolver
import setup_server

# ── helpers ─────────────────────────────────────────────────────────────────


def _write_env(env_file: str, values: dict[str, str]) -> None:
    """Write env.sh in the conventional `export KEY=VAL` form for the test."""
    lines = [f"export {k}={v}" for k, v in values.items()]
    with open(env_file, "w") as f:
        f.write("\n".join(lines) + "\n")


def _stub_ip_geo(monkeypatch, response):
    """Replace ``geocoding.ip_geolocate`` with one that returns ``response``."""
    import geocoding

    monkeypatch.setattr(geocoding, "ip_geolocate", lambda: response)


def _stub_set_system_timezone(monkeypatch, ok=True, err=None):
    """Stub ``geocoding.set_system_timezone`` so tests don't shell out
    (moved here from setup_server in #414 item #5)."""
    monkeypatch.setattr(geocoding, "set_system_timezone", lambda tz: (ok, err))


def _stub_sleep(monkeypatch):
    """Stub time.sleep so retry loops don't waste wall-clock during tests."""
    import time

    monkeypatch.setattr(time, "sleep", lambda _s: None)


# ── CRITICAL regression: silent-corruption guard (A1 + A15) ─────────────────


class TestMainModeSpecificGate:
    """The whole point of ``WEATHER_LOCATION_MODE``. If main() ever overwrites
    a user's typed Specific location on reboot, this is the test that catches
    it. Pin firmly — any future refactor that breaks the gate MUST update this
    test consciously (not accidentally)."""

    def test_main_no_ops_when_mode_specific(self, tmp_env_file, monkeypatch):
        """CRITICAL (#337 A1): the on-boot oneshot must NEVER overwrite env.sh
        when ``WEATHER_LOCATION_MODE=specific``. The user picked Specific in
        the PWA; their typed city is sacred. Silent overwrite would be the
        worst class of corruption this feature could introduce."""
        _write_env(
            tmp_env_file,
            {
                "WEATHER_LOCATION_MODE": "specific",
                "WEATHER_LATITUDE": "51.5",
                "WEATHER_LONGITUDE": "-0.1",
                "WEATHER_LOCATION_NAME": "London, England",
                "WEATHER_IP_COUNTRY": "GB",
            },
        )
        monkeypatch.setenv("LITCLOCK_ENV_FILE", tmp_env_file)

        # If the gate fails, the resolver would call ip_geolocate and try to
        # write env.sh. We make ip_geolocate explode so a regression here
        # produces a loud AssertionError instead of a silent overwrite.
        import geocoding

        monkeypatch.setattr(
            geocoding,
            "ip_geolocate",
            lambda: pytest.fail("main() must NOT call ip_geolocate when MODE=specific"),
        )

        before = open(tmp_env_file).read()
        rc = location_resolver.main()
        after = open(tmp_env_file).read()

        assert rc == 0, "main() should exit 0 (clean no-op) when MODE=specific"
        assert before == after, "env.sh must be byte-identical after a MODE=specific no-op"

    def test_main_runs_when_mode_auto(self, tmp_env_file, monkeypatch):
        """Counterpart: when MODE=auto, the oneshot runs the resolver."""
        _write_env(tmp_env_file, {"WEATHER_LOCATION_MODE": "auto"})
        monkeypatch.setenv("LITCLOCK_ENV_FILE", tmp_env_file)
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        _stub_sleep(monkeypatch)
        _stub_set_system_timezone(monkeypatch)
        called = []
        _stub_ip_geo(
            monkeypatch,
            {
                "lat": "30.27",
                "lon": "-97.74",
                "city": "Austin, TX",
                "country_code": "US",
                "timezone": "America/Chicago",
            },
        )
        import geocoding

        original = geocoding.ip_geolocate

        def watched():
            called.append(1)
            return original()

        monkeypatch.setattr(geocoding, "ip_geolocate", watched)

        rc = location_resolver.main()
        assert rc == 0
        assert called == [1], "MODE=auto must trigger exactly one IP-geo call"

    def test_main_treats_empty_mode_as_auto(self, tmp_env_file, monkeypatch):
        """Migration guard (#337 A1): pre-S2 envs with empty MODE must run
        the resolver (auto is the migration default)."""
        _write_env(tmp_env_file, {"WEATHER_LOCATION_MODE": ""})
        monkeypatch.setenv("LITCLOCK_ENV_FILE", tmp_env_file)
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        _stub_sleep(monkeypatch)
        _stub_set_system_timezone(monkeypatch)
        called = []
        import geocoding

        def stub():
            called.append(1)
            return {
                "lat": "30.27",
                "lon": "-97.74",
                "city": "Austin, TX",
                "country_code": "US",
                "timezone": "America/Chicago",
            }

        monkeypatch.setattr(geocoding, "ip_geolocate", stub)

        location_resolver.main()
        assert called == [1], "empty MODE must default to auto and trigger resolve"

    def test_main_treats_absent_mode_as_auto(self, tmp_env_file, monkeypatch):
        """Migration guard: pre-S2 envs entirely missing the MODE key must
        run the resolver."""
        _write_env(tmp_env_file, {"WEATHER_UNITS": "imperial"})
        monkeypatch.setenv("LITCLOCK_ENV_FILE", tmp_env_file)
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        _stub_sleep(monkeypatch)
        _stub_set_system_timezone(monkeypatch)
        called = []
        import geocoding

        def stub():
            called.append(1)
            return {
                "lat": "30.27",
                "lon": "-97.74",
                "city": "Austin, TX",
                "country_code": "US",
                "timezone": "America/Chicago",
            }

        monkeypatch.setattr(geocoding, "ip_geolocate", stub)

        location_resolver.main()
        assert called == [1]

    def test_main_exits_cleanly_on_missing_env(self, tmp_path, monkeypatch):
        """No env.sh → no panic, just clean exit 0. systemd shouldn't retry-loop."""
        monkeypatch.setenv("LITCLOCK_ENV_FILE", str(tmp_path / "does-not-exist.sh"))
        # If the gate fails, ip_geolocate would be called — make it explode.
        import geocoding

        monkeypatch.setattr(
            geocoding,
            "ip_geolocate",
            lambda: pytest.fail("must not call ip_geolocate when env.sh is missing"),
        )

        rc = location_resolver.main()
        assert rc == 0


# ── A6 + A6.1: country-change UNITS rule ────────────────────────────────────


class TestUnitsCountryChangeRule:
    """A6: UNITS is overwritten only when the resolved country differs from
    the persisted ``WEATHER_IP_COUNTRY``. A6.1: that persisted country is
    written on every successful resolve."""

    @pytest.fixture(autouse=True)
    def _no_sleep_and_tz_ok(self, monkeypatch):
        _stub_sleep(monkeypatch)
        _stub_set_system_timezone(monkeypatch)

    def test_same_country_preserves_units_override(self, tmp_env_file, monkeypatch):
        """User in US picked Celsius manually. On-boot reresolve detects same
        country (US). UNITS must NOT be overwritten — Celsius pick survives."""
        _write_env(
            tmp_env_file,
            {
                "WEATHER_LOCATION_MODE": "auto",
                "WEATHER_UNITS": "metric",  # user's manual override
                "WEATHER_IP_COUNTRY": "US",  # persisted from previous resolve
            },
        )
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        _stub_ip_geo(
            monkeypatch,
            {
                "lat": "30.27",
                "lon": "-97.74",
                "city": "Austin, TX",
                "country_code": "US",  # same as persisted
                "timezone": "America/Chicago",
            },
        )

        location_resolver.resolve_location_from_ip(retries=False, env_file=tmp_env_file)

        env = config.load_config(tmp_env_file)
        assert env["WEATHER_UNITS"] == "metric", "manual override must survive same-country resolve"
        assert env["WEATHER_IP_COUNTRY"] == "US", "IP_COUNTRY refreshed even when no change"

    def test_country_change_flips_units(self, tmp_env_file, monkeypatch):
        """Pi moved US→UK. On-boot reresolve detects country change; UNITS
        flips to metric (the new country's default)."""
        _write_env(
            tmp_env_file,
            {
                "WEATHER_LOCATION_MODE": "auto",
                "WEATHER_UNITS": "imperial",
                "WEATHER_IP_COUNTRY": "US",  # previous trip
            },
        )
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        _stub_ip_geo(
            monkeypatch,
            {
                "lat": "51.5",
                "lon": "-0.1",
                "city": "London, England",
                "country_code": "GB",  # DIFFERENT from persisted
                "timezone": "Europe/London",
            },
        )

        location_resolver.resolve_location_from_ip(retries=False, env_file=tmp_env_file)

        env = config.load_config(tmp_env_file)
        assert env["WEATHER_UNITS"] == "metric", "country change must flip UNITS to new default"
        assert env["WEATHER_IP_COUNTRY"] == "GB", "IP_COUNTRY updated to new country"

    def test_empty_persisted_country_writes_default(self, tmp_env_file, monkeypatch):
        """First resolve (pre-S2 migration, fresh install, post-gift-reset):
        persisted IP_COUNTRY is empty → write the default. No prior state to
        preserve."""
        _write_env(
            tmp_env_file,
            {
                "WEATHER_LOCATION_MODE": "auto",
                "WEATHER_UNITS": "imperial",
                "WEATHER_IP_COUNTRY": "",
            },
        )
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        _stub_ip_geo(
            monkeypatch,
            {
                "lat": "51.5",
                "lon": "-0.1",
                "city": "London, England",
                "country_code": "GB",
                "timezone": "Europe/London",
            },
        )

        location_resolver.resolve_location_from_ip(retries=False, env_file=tmp_env_file)

        env = config.load_config(tmp_env_file)
        # Empty persisted → write the default. Without this, UK-bound first-boots
        # would silently fall through to imperial.
        assert env["WEATHER_UNITS"] == "metric"
        assert env["WEATHER_IP_COUNTRY"] == "GB"


# ── A7: sync-quick retry budget ─────────────────────────────────────────────


class TestSyncQuickRetries:
    """A7: PWA Save Specific→Auto switch uses retries=False (single attempt,
    no backoff). Boot path uses retries=True (full 4-attempt budget)."""

    @pytest.fixture(autouse=True)
    def _no_sleep_and_tz_ok(self, monkeypatch):
        _stub_sleep(monkeypatch)
        _stub_set_system_timezone(monkeypatch)

    def test_retries_false_makes_one_attempt(self, tmp_env_file, monkeypatch):
        """Sync-quick path: one attempt, no retries on failure."""
        _write_env(tmp_env_file, {"WEATHER_LOCATION_MODE": "auto"})
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        attempts = []

        import geocoding

        def always_none():
            attempts.append(1)
            return None

        monkeypatch.setattr(geocoding, "ip_geolocate", always_none)

        location_resolver.resolve_location_from_ip(retries=False, env_file=tmp_env_file)
        assert len(attempts) == 1, "retries=False must make exactly 1 attempt"

    def test_retries_true_makes_full_budget_attempts(self, tmp_env_file, monkeypatch):
        """Full-retry path: 1 + len(delays) attempts."""
        _write_env(tmp_env_file, {"WEATHER_LOCATION_MODE": "auto"})
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        attempts = []
        import geocoding

        def always_none():
            attempts.append(1)
            return None

        monkeypatch.setattr(geocoding, "ip_geolocate", always_none)

        location_resolver.resolve_location_from_ip(retries=True, env_file=tmp_env_file)
        assert len(attempts) == len(location_resolver._IP_GEO_RETRY_DELAYS) + 1


# ── A1: MODE=auto is written on every successful resolve ────────────────────


class TestModeWrite:
    @pytest.fixture(autouse=True)
    def _no_sleep_and_tz_ok(self, monkeypatch):
        _stub_sleep(monkeypatch)
        _stub_set_system_timezone(monkeypatch)

    def test_successful_resolve_writes_mode_auto(self, tmp_env_file, monkeypatch):
        """Whether MODE was previously absent, empty, or already 'auto',
        a successful resolve writes 'auto' explicitly so env.sh always
        carries the field (A3 defensive-default invariant)."""
        _write_env(tmp_env_file, {})  # no MODE at all
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        _stub_ip_geo(
            monkeypatch,
            {
                "lat": "30.27",
                "lon": "-97.74",
                "city": "Austin, TX",
                "country_code": "US",
                "timezone": "America/Chicago",
            },
        )

        location_resolver.resolve_location_from_ip(retries=False, env_file=tmp_env_file)

        env = config.load_config(tmp_env_file)
        assert env["WEATHER_LOCATION_MODE"] == "auto"


# ── A15: atomicity contract ─────────────────────────────────────────────────


class TestAtomicityContract:
    """A15 (locked 2026-06-01 by user during /plan-design-review): a failed
    save MUST leave env.sh byte-identical. The "Moon" example was the
    motivating case — user types non-resolvable Specific location, taps Save,
    sees error, closes PWA, reopens, sees the LAST PERSISTED state (not the
    failed Moon attempt). Pinned here at the resolver level; the routes-level
    counterpart lives in tests/test_control_server_settings.py."""

    @pytest.fixture(autouse=True)
    def _no_sleep_and_tz_ok(self, monkeypatch):
        _stub_sleep(monkeypatch)
        _stub_set_system_timezone(monkeypatch)

    def test_failed_ip_geo_leaves_env_byte_identical(self, tmp_env_file, monkeypatch):
        """resolve_location_from_ip with always-failing IP-geo → zero env writes."""
        _write_env(
            tmp_env_file,
            {
                "WEATHER_LOCATION_MODE": "auto",
                "WEATHER_LATITUDE": "30.27",
                "WEATHER_LONGITUDE": "-97.74",
                "WEATHER_UNITS": "imperial",
                "WEATHER_IP_COUNTRY": "US",
            },
        )
        monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
        import geocoding

        monkeypatch.setattr(geocoding, "ip_geolocate", lambda: None)

        before = open(tmp_env_file).read()
        location_resolver.resolve_location_from_ip(retries=False, env_file=tmp_env_file)
        after = open(tmp_env_file).read()

        assert before == after, "failed IP-geo must leave env.sh byte-identical"

    def test_incomplete_coords_skip_write(self, tmp_env_file, monkeypatch):
        """update_env_location refuses partial coord pairs (#393) — no env
        write happens. Test that a tz-less coord pair also skips."""
        _write_env(
            tmp_env_file,
            {
                "WEATHER_LATITUDE": "0",
                "WEATHER_LONGITUDE": "0",
                "WEATHER_UNITS": "imperial",
            },
        )
        before = open(tmp_env_file).read()
        # Lat-only (partial). update_env_location should refuse.
        location_resolver.update_env_location(
            "30.27",
            None,
            location_name="Austin, TX",
            timezone="America/Chicago",
            env_file=tmp_env_file,
        )
        after = open(tmp_env_file).read()
        assert before == after, "partial coord pair must skip the write entirely"

    def test_tz_set_failure_skips_env_write(self, tmp_env_file, monkeypatch):
        """A15 + #393: if set_system_timezone fails, env.sh must not be
        partially written. Worst failure case = tz set + env stale, never
        env populated + tz stale (wrong-time clock)."""
        _write_env(tmp_env_file, {"WEATHER_UNITS": "imperial"})
        _stub_set_system_timezone(monkeypatch, ok=False, err="timedatectl unavailable")

        before = open(tmp_env_file).read()
        location_resolver.update_env_location(
            "30.27",
            "-97.74",
            location_name="Austin, TX",
            timezone="America/Chicago",  # tz set fails
            env_file=tmp_env_file,
        )
        after = open(tmp_env_file).read()
        assert before == after, "tz failure must short-circuit before env write"


# ── update_env_location: new kwarg surface ──────────────────────────────────


class TestUpdateEnvLocationNewKwargs:
    """Direct tests for the public ``update_env_location`` surface — exercises
    the new ``mode`` and ``ip_country`` kwargs added by #337 A1/A6.1."""

    @pytest.fixture(autouse=True)
    def _tz_ok(self, monkeypatch):
        _stub_set_system_timezone(monkeypatch)

    @pytest.fixture(autouse=True)
    def _isolate_collected_marker(self, tmp_path, monkeypatch):
        """#445 wiring: update_env_location now best-effort writes the
        time-location collected-marker. Redirect it into tmp_path so these
        tests stay hermetic (otherwise a successful resolve writes a real
        /var/lib/litclock/.last-collected-marker.json on a Pi/CI box where
        that dir exists). Exposes the path so the wiring test can read it."""
        marker = tmp_path / ".last-collected-marker.json"
        monkeypatch.setenv("LITCLOCK_COLLECTED_MARKER", str(marker))
        self._marker_path = marker

    def test_records_time_location_collected(self, tmp_env_file):
        """#445: a successful resolve+write records the time-location key in
        the persistent collected-marker. Pins the integration seam so a
        regression in the mark_collected call (wrong key, broken import, the
        broad except swallowing it) fails CI instead of shipping silently."""
        import json as _json

        location_resolver.update_env_location(
            "30.27",
            "-97.74",
            location_name="Austin, TX",
            timezone="America/Chicago",
            mode="auto",
            env_file=tmp_env_file,
        )
        assert self._marker_path.exists(), "successful resolve should write the collected-marker"
        assert "time-location" in _json.loads(self._marker_path.read_text())

    def test_no_collected_marker_when_write_refused(self, tmp_env_file):
        """The marker must NOT be recorded when the writer refuses (no usable
        coords → update_env_location returns False before any write)."""
        result = location_resolver.update_env_location(
            None,
            None,
            location_name="",
            timezone="America/Chicago",
            env_file=tmp_env_file,
        )
        assert result is False
        assert not self._marker_path.exists()

    def test_writes_mode_when_given(self, tmp_env_file):
        location_resolver.update_env_location(
            "30.27",
            "-97.74",
            location_name="Austin, TX",
            timezone="America/Chicago",
            mode="auto",
            env_file=tmp_env_file,
        )
        env = config.load_config(tmp_env_file)
        assert env["WEATHER_LOCATION_MODE"] == "auto"

    def test_writes_ip_country_uppercased(self, tmp_env_file):
        """A6.1: IP_COUNTRY canonicalised to upper at the writer so reads
        always see consistent shape."""
        location_resolver.update_env_location(
            "30.27",
            "-97.74",
            location_name="Austin, TX",
            timezone="America/Chicago",
            ip_country="us",  # lowercase input
            env_file=tmp_env_file,
        )
        env = config.load_config(tmp_env_file)
        assert env["WEATHER_IP_COUNTRY"] == "US"

    def test_omits_mode_and_country_when_none(self, tmp_env_file):
        """Backwards-compat: legacy callers don't pass mode/ip_country; the
        writer must not invent values for them."""
        _write_env(tmp_env_file, {})  # ensure no preexisting keys
        location_resolver.update_env_location(
            "30.27",
            "-97.74",
            location_name="Austin, TX",
            timezone="America/Chicago",
            env_file=tmp_env_file,
        )
        env = config.load_config(tmp_env_file)
        assert "WEATHER_LOCATION_MODE" not in env or env["WEATHER_LOCATION_MODE"] == ""
        assert "WEATHER_IP_COUNTRY" not in env or env["WEATHER_IP_COUNTRY"] == ""


# ── country_default_units helper ───────────────────────────────────────────


class TestCountryDefaultUnits:
    """A6 default-derivation rule. Pinned so the constants don't drift between
    the resolver and reset-setup.sh defaults."""

    def test_us_imperial(self):
        assert location_resolver.country_default_units("US") == "imperial"

    def test_empty_imperial(self):
        """Unknown country falls back to imperial (matches reset-setup.sh)."""
        assert location_resolver.country_default_units("") == "imperial"

    def test_uk_metric(self):
        assert location_resolver.country_default_units("GB") == "metric"

    def test_india_metric(self):
        assert location_resolver.country_default_units("IN") == "metric"

    def test_anything_non_us_metric(self):
        for cc in ("FR", "JP", "AU", "CA", "DE", "BR", "ZA"):
            assert location_resolver.country_default_units(cc) == "metric"


# ── _persisted_country helper ───────────────────────────────────────────────


class TestEnvFileForwarding:
    """#337 /review P0 regression. Codex caught that `resolve_location_from_ip`
    was passing `env_file` to itself but the inner call to the
    `setup_server._update_env_location` shim hardcoded ENV_FILE from the
    setup_server module — which is None in PWA + on-boot oneshot contexts.
    Result: the resolver read from the right file but WROTE NOWHERE (the
    canonical writer's `if not env_file or not os.path.exists(env_file)`
    guard short-circuited). Tests masked this by monkeypatching
    `setup_server.ENV_FILE`. The fix forwards `env_file` through the shim;
    these tests pin that the forwarding actually happens.

    If these tests start failing, the silent-no-op P0 has regressed. Do NOT
    just monkeypatch ENV_FILE to make them pass — that's what concealed the
    bug originally."""

    @pytest.fixture(autouse=True)
    def _tz_ok(self, monkeypatch):
        _stub_set_system_timezone(monkeypatch)

    def test_resolver_passes_env_file_to_shim(self, tmp_env_file, monkeypatch):
        """resolve_location_from_ip(env_file=X) must reach the shim with
        env_file=X, NOT setup_server.ENV_FILE. Pin via the shim itself,
        intercepting the kwargs the resolver passes."""
        # Set setup_server.ENV_FILE to a DIFFERENT, deliberately-bogus path
        # so a regression that forwards the module constant (instead of the
        # caller's env_file) writes nowhere and the assertion catches it.
        monkeypatch.setattr(setup_server, "ENV_FILE", "/tmp/should-not-be-touched.sh")

        seen_kwargs = []

        def spy(lat, lon, *, location_name=None, units=None, timezone=None, **kwargs):
            seen_kwargs.append(kwargs)
            # Forward to real implementation so the env actually gets written
            # (proves the value isn't just observed but also used).
            return location_resolver.update_env_location(
                lat, lon, location_name=location_name, units=units, timezone=timezone, **kwargs
            )

        monkeypatch.setattr(setup_server, "_update_env_location", spy)
        _stub_sleep(monkeypatch)
        _stub_ip_geo(
            monkeypatch,
            {
                "lat": "30.27",
                "lon": "-97.74",
                "city": "Austin, TX",
                "country_code": "US",
                "timezone": "America/Chicago",
            },
        )

        result = location_resolver.resolve_location_from_ip(retries=False, env_file=tmp_env_file)
        assert result is True, "resolver must return True on successful resolve + write"
        assert len(seen_kwargs) == 1
        assert seen_kwargs[0].get("env_file") == tmp_env_file, (
            "P0 REGRESSION: resolver must forward env_file to the shim. "
            "If env_file is missing or wrong here, the on-boot oneshot + PWA "
            "sync-quick paths silently no-op because setup_server.ENV_FILE is "
            "None in those contexts."
        )
        # Verify the write actually landed in the tmp file, not the bogus one.
        env = config.load_config(tmp_env_file)
        assert env["WEATHER_LATITUDE"] == "30.27"
        # And NOT in the bogus path.
        assert not os.path.exists("/tmp/should-not-be-touched.sh")

    def test_shim_pops_env_file_kwarg_before_forwarding(self, tmp_env_file, monkeypatch):
        """The shim's `env_file` override path (popping from kwargs to avoid
        duplicate-kwarg errors when forwarding to the canonical writer) is
        load-bearing — without the pop, Python raises TypeError on the
        explicit env_file= + **kwargs collision and the resolver's success
        path silently turns into an exception swallowed by `except Exception`
        in routes/settings.py."""
        monkeypatch.setattr(setup_server, "ENV_FILE", "/tmp/nope.sh")
        # This call mimics what resolve_location_from_ip does.
        result = setup_server._update_env_location(
            "30.27",
            "-97.74",
            location_name="Austin, TX",
            timezone="America/Chicago",
            env_file=tmp_env_file,  # explicit override
        )
        assert result is True
        env = config.load_config(tmp_env_file)
        assert env["WEATHER_LATITUDE"] == "30.27"


class TestPersistedCountry:
    def test_empty_when_file_missing(self, tmp_path):
        assert location_resolver._persisted_country(str(tmp_path / "nope.sh")) == ""

    def test_empty_when_key_absent(self, tmp_env_file):
        _write_env(tmp_env_file, {})
        assert location_resolver._persisted_country(tmp_env_file) == ""

    def test_returns_persisted_value_uppercased(self, tmp_env_file):
        _write_env(tmp_env_file, {"WEATHER_IP_COUNTRY": "gb"})
        assert location_resolver._persisted_country(tmp_env_file) == "GB"

    def test_handles_empty_env_file(self, tmp_path):
        f = tmp_path / "empty.sh"
        f.write_text("")
        assert location_resolver._persisted_country(str(f)) == ""
