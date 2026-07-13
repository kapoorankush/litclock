"""Tests for weather provider modules."""

import time as time_mod

import pytest

from weather_providers.base_provider import _is_stale
from weather_providers.openweathermap import OpenWeatherMap

# ── get_icon_from_openweathermap_weathercode ────────────────────────


class TestGetIconByWeathercode:
    @pytest.fixture
    def provider(self):
        return OpenWeatherMap("fake-key", "0", "0", "metric")

    def test_clear_day(self, provider):
        assert provider.get_icon_from_openweathermap_weathercode(800, True) == "sun"

    def test_clear_night(self, provider):
        assert provider.get_icon_from_openweathermap_weathercode(800, False) == "moon"

    def test_few_clouds_day(self, provider):
        assert provider.get_icon_from_openweathermap_weathercode(801, True) == "cloud_sun"

    def test_few_clouds_night(self, provider):
        assert provider.get_icon_from_openweathermap_weathercode(801, False) == "cloud_moon"

    def test_thunderstorm_fixed(self, provider):
        assert provider.get_icon_from_openweathermap_weathercode(200, True) == "lightning"
        assert provider.get_icon_from_openweathermap_weathercode(200, False) == "lightning"

    def test_heavy_rain(self, provider):
        assert provider.get_icon_from_openweathermap_weathercode(502, True) == "rain2"

    def test_snow_day(self, provider):
        assert provider.get_icon_from_openweathermap_weathercode(600, True) == "snow_sun"

    def test_snow_night(self, provider):
        assert provider.get_icon_from_openweathermap_weathercode(600, False) == "snow_moon"

    def test_unknown_code_raises(self, provider):
        with pytest.raises(KeyError):
            provider.get_icon_from_openweathermap_weathercode(999, True)


# ── c_to_f ──────────────────────────────────────────────────────────


class TestCToF:
    @pytest.fixture
    def provider(self):
        return OpenWeatherMap("fake-key", "0", "0", "metric")

    def test_freezing(self, provider):
        assert provider.c_to_f(0) == 32

    def test_boiling(self, provider):
        assert provider.c_to_f(100) == 212

    def test_negative_forty(self, provider):
        assert provider.c_to_f(-40) == -40

    def test_body_temp(self, provider):
        assert provider.c_to_f(37) == pytest.approx(98.6)

    def test_float_input(self, provider):
        assert provider.c_to_f(0.5) == pytest.approx(32.9)


# ── _is_stale ───────────────────────────────────────────────────────


class TestIsStale:
    def test_nonexistent_file(self, tmp_path):
        assert _is_stale(str(tmp_path / "nope.json"), 60) is True

    def test_fresh_file(self, tmp_path):
        f = tmp_path / "cache.json"
        f.write_text("{}")
        assert _is_stale(str(f), 3600) is False

    def test_expired_file(self, tmp_path, mocker):
        f = tmp_path / "cache.json"
        f.write_text("{}")
        # Pretend the file was modified 2 hours ago — patch where it's used
        mocker.patch("weather_providers.base_provider.time.time", return_value=time_mod.time() + 7200)
        assert _is_stale(str(f), 3600) is True


# ── Unit-aware cache (bug caught during issue #175 QA, 2026-04-11) ─
#
# The cache layer used to key on filename only. A celsius cache written in
# one session was served under a °F label in a later session after the user
# changed WEATHER_UNITS. These tests pin the fix: cache filenames include
# units, and the orphan sweep removes wrong-unit files on read.


class TestUnitAwareCache:
    @pytest.fixture(autouse=True)
    def _reset_legacy_sweep_state(self, monkeypatch):
        """The one-shot legacy-root sweep gate (#434 review) is module-level
        state that would otherwise leak across tests. Reset it to empty before
        each test so a test relying on the legacy _PROJECT_ROOT scan firing
        isn't suppressed by an earlier test having already marked that root
        swept."""
        from weather_providers import base_provider

        monkeypatch.setattr(base_provider, "_legacy_roots_swept", set())

    def test_cache_file_path_includes_units(self):
        from weather_providers.openweathermap import OpenWeatherMap

        metric = OpenWeatherMap("k", "0", "0", "metric")
        imperial = OpenWeatherMap("k", "0", "0", "imperial")
        assert metric._cache_file_path() != imperial._cache_file_path()
        # Filename shape: {prefix}-{units}-{lat}-{lon}.json (M3 #245).
        assert "-metric-" in metric._cache_file_path()
        assert "-imperial-" in imperial._cache_file_path()

    def test_cache_file_path_includes_coords(self):
        """M3 #245 hardware-QA fix: changing location must invalidate the
        cache. Austin TX (30.27, -97.74) and Dublin CA (37.7, -121.9) must
        write to different cache files so a coord change gets a clean miss
        on the next tick instead of serving the old location's payload
        until WEATHER_TTL expires."""
        from weather_providers.open_meteo import OpenMeteo

        austin = OpenMeteo("30.27", "-97.74", "imperial")
        dublin = OpenMeteo("37.7", "-121.9", "imperial")
        assert austin._cache_file_path() != dublin._cache_file_path()
        assert austin._cache_file_path().endswith("-imperial-30.27--97.74.json")
        assert dublin._cache_file_path().endswith("-imperial-37.7--121.9.json")

    def test_cache_file_path_rejects_path_traversal_in_coords(self):
        """Defense-in-depth: lat/lon are interpolated into a filesystem
        path. config.atomic_update validates at ingress, but if a future
        bug ever lets a non-numeric value through, the cache builder must
        refuse to interpolate it."""
        import pytest

        from weather_providers.open_meteo import OpenMeteo

        bad = OpenMeteo("../../tmp/pwn", "0", "imperial")
        with pytest.raises(ValueError, match="Invalid coordinates"):
            bad._cache_file_path()

    def test_openmeteo_and_owm_do_not_share_cache(self):
        from weather_providers.open_meteo import OpenMeteo
        from weather_providers.openweathermap import OpenWeatherMap

        om = OpenMeteo("0", "0", "imperial")
        owm = OpenWeatherMap("k", "0", "0", "imperial")
        assert om._cache_file_path() != owm._cache_file_path()
        # Sanity: their prefixes differ so a sweep can't cross-contaminate
        assert om._cache_prefix != owm._cache_prefix

    def test_orphan_sweep_removes_stale_unit_cache(self, tmp_path, monkeypatch):
        """A cache written under metric units must be removed when the
        provider is next constructed under imperial units — otherwise we
        repeat the #175 bug (celsius numbers rendered with a °F label)."""
        from weather_providers import base_provider
        from weather_providers.open_meteo import OpenMeteo

        monkeypatch.setattr(base_provider, "_CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(base_provider, "_PROJECT_ROOT", str(tmp_path))

        stale = tmp_path / "weather-cache-openmeteo-metric-0-0.json"
        stale.write_text('{"current_units": {"temperature_2m": "\\u00b0C"}}')
        assert stale.exists()

        imperial = OpenMeteo("0", "0", "imperial")
        imperial._sweep_orphan_caches()

        assert not stale.exists(), "metric-unit cache must be swept when provider is imperial"
        # The imperial cache (which doesn't exist yet) must not have been created
        assert not (tmp_path / "weather-cache-openmeteo-imperial-0-0.json").exists()

    def test_orphan_sweep_removes_stale_coord_cache(self, tmp_path, monkeypatch):
        """M3 #245 hardware-QA fix: when the user changes city, the prior
        location's cache must be swept on the next render so the new
        coords get a fresh fetch instead of serving the old payload."""
        from weather_providers import base_provider
        from weather_providers.open_meteo import OpenMeteo

        monkeypatch.setattr(base_provider, "_CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(base_provider, "_PROJECT_ROOT", str(tmp_path))

        # Austin TX cache from the user's prior location.
        old_loc = tmp_path / "weather-cache-openmeteo-imperial-30.27--97.74.json"
        old_loc.write_text('{"current": {"temperature_2m": 75}}')
        assert old_loc.exists()

        # User changes city to Dublin CA — provider constructed with new coords.
        dublin = OpenMeteo("37.7", "-121.9", "imperial")
        dublin._sweep_orphan_caches()

        assert not old_loc.exists(), "old-location cache must be swept on coord change"

    def test_orphan_sweep_removes_pre_refactor_legacy_file(self, tmp_path, monkeypatch):
        """Upgraders had plain `weather-cache-openmeteo.json` on disk (no
        units suffix). Sweep must delete it too."""
        from weather_providers import base_provider
        from weather_providers.open_meteo import OpenMeteo

        monkeypatch.setattr(base_provider, "_CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(base_provider, "_PROJECT_ROOT", str(tmp_path))
        legacy = tmp_path / "weather-cache-openmeteo.json"
        legacy.write_text("{}")

        OpenMeteo("0", "0", "imperial")._sweep_orphan_caches()

        assert not legacy.exists()

    def test_orphan_sweep_removes_owm_pre_refactor_legacy_file(self, tmp_path, monkeypatch):
        """OpenWeatherMap used to write to plain `weather-cache.json` (the
        old module-level _DEFAULT_CACHE_FILE). Listed in _legacy_cache_filenames
        on the subclass so the sweep cleans it up after the prefix changed
        to weather-cache-owm."""
        from weather_providers import base_provider
        from weather_providers.openweathermap import OpenWeatherMap

        monkeypatch.setattr(base_provider, "_CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(base_provider, "_PROJECT_ROOT", str(tmp_path))
        legacy = tmp_path / "weather-cache.json"
        legacy.write_text("{}")

        OpenWeatherMap("k", "0", "0", "imperial")._sweep_orphan_caches()

        assert not legacy.exists()

    def test_cache_lives_on_tmpfs_by_default(self, monkeypatch):
        """#434: the weather cache is a purely derived 1h-TTL blob, so it must
        default to the /run/litclock tmpfs dir — NOT the SD-backed project
        root — to keep ~8,760 writes/yr off the flash card. Unset the override
        + reload so the assertion holds even when a dev/CI runner has exported
        LITCLOCK_WEATHER_CACHE_DIR (the very override this change advertises)."""
        import importlib

        from weather_providers import base_provider

        monkeypatch.delenv("LITCLOCK_WEATHER_CACHE_DIR", raising=False)
        importlib.reload(base_provider)
        try:
            from weather_providers.open_meteo import OpenMeteo

            path = OpenMeteo("0", "0", "imperial")._cache_file_path()
            assert path.startswith("/run/litclock/"), f"cache must live on tmpfs, got {path}"
        finally:
            importlib.reload(base_provider)

    def test_cache_dir_env_override(self, tmp_path, monkeypatch):
        """The cache dir is overridable via LITCLOCK_WEATHER_CACHE_DIR so dev
        boxes + non-Pi hosts (no /run/litclock) can relocate it. Reload the
        module so the module-level default re-reads the env var."""
        import importlib

        from weather_providers import base_provider

        monkeypatch.setenv("LITCLOCK_WEATHER_CACHE_DIR", str(tmp_path))
        try:
            importlib.reload(base_provider)
            from weather_providers.open_meteo import OpenMeteo

            path = OpenMeteo("0", "0", "imperial")._cache_file_path()
            assert path.startswith(str(tmp_path))
        finally:
            monkeypatch.delenv("LITCLOCK_WEATHER_CACHE_DIR", raising=False)
            importlib.reload(base_provider)

    def test_orphan_sweep_removes_legacy_sd_cache_on_upgrade(self, tmp_path, monkeypatch):
        """#434 migration: after the cache moves to tmpfs, a pre-upgrade file
        left in the SD-backed project root must be swept on the first fetch so
        it doesn't rot on flash forever. Cache dir and project root are
        DISTINCT dirs here (unlike the other sweep tests) to model the split."""
        from weather_providers import base_provider
        from weather_providers.open_meteo import OpenMeteo

        cache_dir = tmp_path / "run"
        project_root = tmp_path / "sd"
        cache_dir.mkdir()
        project_root.mkdir()
        monkeypatch.setattr(base_provider, "_CACHE_DIR", str(cache_dir))
        monkeypatch.setattr(base_provider, "_PROJECT_ROOT", str(project_root))

        # Stale cache left behind on the SD card by a pre-#434 install.
        sd_leftover = project_root / "weather-cache-openmeteo-imperial-0-0.json"
        sd_leftover.write_text("{}")
        assert sd_leftover.exists()

        OpenMeteo("0", "0", "imperial")._sweep_orphan_caches()

        assert not sd_leftover.exists(), "stale SD-resident cache must be swept post-upgrade"

    def test_legacy_root_swept_only_once_per_process(self, tmp_path, monkeypatch):
        """#434 review: the legacy SD project-root sweep is a one-time
        migration. After the first fetch marks it swept, a stale file that
        later reappears in the project root must NOT be re-swept — we don't
        readdir the flash-backed repo root on every hourly fetch forever."""
        from weather_providers import base_provider
        from weather_providers.open_meteo import OpenMeteo

        cache_dir = tmp_path / "run"
        project_root = tmp_path / "sd"
        cache_dir.mkdir()
        project_root.mkdir()
        monkeypatch.setattr(base_provider, "_CACHE_DIR", str(cache_dir))
        monkeypatch.setattr(base_provider, "_PROJECT_ROOT", str(project_root))

        provider = OpenMeteo("0", "0", "imperial")
        provider._sweep_orphan_caches()  # first sweep marks the legacy root done

        # A stale file appears in the SD root AFTER the one-shot fired.
        late = project_root / "weather-cache-openmeteo-metric-0-0.json"
        late.write_text("{}")
        provider._sweep_orphan_caches()

        assert late.exists(), "legacy root must not be re-scanned after the one-shot migration"

    def test_write_cache_is_best_effort_on_failure(self, tmp_path, monkeypatch):
        """#434 review headline: a cache-write failure (e.g. the pi user can't
        create a root-owned /run/litclock) must NOT raise — the caller already
        holds the live fetched data and must still render. It logs and moves
        on, leaving no target file behind."""
        from weather_providers import base_provider
        from weather_providers.open_meteo import OpenMeteo

        def boom(*args, **kwargs):
            raise PermissionError("simulated root-owned /run parent")

        monkeypatch.setattr(base_provider.tempfile, "mkstemp", boom)
        target = tmp_path / "weather-cache-openmeteo-imperial-0-0.json"

        OpenMeteo("0", "0", "imperial")._write_cache(str(target), {"t": 1})  # must not raise

        assert not target.exists()

    def test_write_cache_is_atomic_and_leaves_no_temp(self, tmp_path):
        """The write goes through tempfile + os.replace (like _write_status_file)
        so a crash mid-write can't leave a truncated JSON the next cache-hit
        read would choke on. Success path leaves the target valid and no
        .weather-cache.tmp.* leftovers."""
        import json

        from weather_providers.open_meteo import OpenMeteo

        target = tmp_path / "weather-cache-openmeteo-imperial-0-0.json"
        OpenMeteo("0", "0", "imperial")._write_cache(str(target), {"t": 42})

        assert json.loads(target.read_text()) == {"t": 42}
        leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".weather-cache.tmp.")]
        assert leftovers == [], f"temp files must be cleaned up, found {leftovers}"

    def test_get_response_data_writes_then_reads_cache(self, tmp_path, monkeypatch):
        """End-to-end round-trip through get_response_data: a cold fetch writes
        the tmpfs cache, and the next call within the TTL is served from that
        file WITHOUT re-fetching. Pins that the #434 relocation didn't break
        the write→read cache contract (the piece unit tests exercised only in
        halves)."""
        import json
        import os

        from weather_providers import base_provider
        from weather_providers.open_meteo import OpenMeteo

        monkeypatch.setattr(base_provider, "_CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(base_provider, "_PROJECT_ROOT", str(tmp_path))

        payload = {"current": {"temperature_2m": 21.5}}
        calls = {"n": 0}

        class FakeResponse:
            text = json.dumps(payload)

            def raise_for_status(self):
                pass

        def fake_get(url, headers=None, timeout=None):
            calls["n"] += 1
            return FakeResponse()

        monkeypatch.setattr(base_provider.requests, "get", fake_get)

        provider = OpenMeteo("0", "0", "imperial")
        cache_path = provider._cache_file_path()

        # 1) Cold: fetch from source + persist to the tmpfs cache.
        first = provider.get_response_data("https://example.test/wx")
        assert first == payload
        assert calls["n"] == 1
        assert os.path.exists(cache_path), "cold fetch must persist the cache to tmpfs"
        with open(cache_path) as f:
            assert json.load(f) == payload

        # 2) Warm: served from the cache file, no second network call.
        second = provider.get_response_data("https://example.test/wx")
        assert second == payload
        assert calls["n"] == 1, "a call within TTL must NOT re-fetch"

    def test_orphan_sweep_preserves_active_cache(self, tmp_path, monkeypatch):
        """Defensive — the sweep must NEVER delete the currently-active
        cache file, or the provider would re-fetch on every call."""
        from weather_providers import base_provider
        from weather_providers.open_meteo import OpenMeteo

        monkeypatch.setattr(base_provider, "_CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(base_provider, "_PROJECT_ROOT", str(tmp_path))
        active = tmp_path / "weather-cache-openmeteo-imperial-0-0.json"
        active.write_text("{}")

        OpenMeteo("0", "0", "imperial")._sweep_orphan_caches()

        assert active.exists()


# ── is_daytime (regression: Austin TX day icon at night) ───────────
#
# Real user report on 2026-04-11: e-ink showed a cloud-with-sun icon
# at 1 AM local time in Austin, TX. Two bugs stacked:
#
# 1. LocationInfo(lat, lon) was passing coordinates as name/region,
#    not latitude/longitude. Observer silently defaulted to Greenwich.
# 2. The obvious "pass as kwargs" fix still broke for daytime queries
#    because astral.sun(observer, date=dt) uses UTC dates, and sunrise
#    during UTC April 11 at Austin is 12 PM UTC while sunset is 00:54 UTC
#    of the SAME UTC date (that's actually April 10 evening local). The
#    comparison `sunrise <= now <= sunset` then fails for any moment
#    after sunrise during local daytime.
#
# These tests pin the elevation()-based fix so neither bug can regress.


class TestIsDaytime:
    """Exercise is_daytime at known times for known locations. Uses
    monkeypatch to freeze `datetime.now(timezone.utc)` to a specific moment."""

    @pytest.fixture(autouse=True)
    def _real_astral(self, monkeypatch):
        """test_open_meteo.py stubs astral + astral.sun with MagicMocks at
        module import (before any other test runs). That's fine for tests
        that mock provider.is_daytime directly, but is_daytime itself needs
        the real astral to compute solar elevation. Force-load the real
        module here and restore it for the duration of each test."""
        import importlib
        import sys

        for mod_name in ("astral", "astral.sun"):
            if mod_name in sys.modules:
                monkeypatch.delitem(sys.modules, mod_name, raising=False)
        importlib.import_module("astral")
        importlib.import_module("astral.sun")

    def _freeze_utc(self, monkeypatch, frozen):
        import datetime as real_datetime

        from weather_providers import base_provider

        class FrozenDatetime(real_datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    return frozen.replace(tzinfo=None)
                return frozen.astimezone(tz)

        # Delegating wrapper: replaces `datetime.datetime` with our frozen
        # subclass, but forwards any other attribute access (timezone, UTC,
        # timedelta, date, etc.) to the real datetime module. ruff's pyupgrade
        # rewrites `datetime.timezone.utc` to `datetime.UTC` on recent Python,
        # so the wrapper needs to forward both.
        class FrozenModule:
            datetime = FrozenDatetime

            def __getattr__(self, name):
                return getattr(real_datetime, name)

        monkeypatch.setattr(base_provider, "datetime", FrozenModule())

    def test_austin_tx_night_returns_false(self, monkeypatch):
        """Austin TX at 1:50 AM local (CDT, UTC-5) on April 11 — clearly
        night. Pre-fix this returned True because of the Greenwich
        Observer default."""
        import datetime as _dt

        from weather_providers.open_meteo import OpenMeteo

        austin_night_utc = _dt.datetime(2026, 4, 11, 6, 50, 0, tzinfo=_dt.UTC)
        self._freeze_utc(monkeypatch, austin_night_utc)

        p = OpenMeteo("30.27", "-97.74", "imperial")
        assert p.is_daytime("30.27", "-97.74") is False

    def test_austin_tx_noon_returns_true(self, monkeypatch):
        """Austin TX at 12 PM local (CDT) on April 11 — clearly day.
        Pre-fix the naive kwargs fix returned False here because astral's
        sunset was from the previous local day."""
        import datetime as _dt

        from weather_providers.open_meteo import OpenMeteo

        austin_noon_utc = _dt.datetime(2026, 4, 11, 17, 0, 0, tzinfo=_dt.UTC)
        self._freeze_utc(monkeypatch, austin_noon_utc)

        p = OpenMeteo("30.27", "-97.74", "imperial")
        assert p.is_daytime("30.27", "-97.74") is True

    def test_austin_tx_8am_morning_returns_true(self, monkeypatch):
        """Catches the edge case where astral's sunset is from the prior
        local day — morning in Austin should be unambiguously day."""
        import datetime as _dt

        from weather_providers.open_meteo import OpenMeteo

        austin_morning_utc = _dt.datetime(2026, 4, 11, 13, 0, 0, tzinfo=_dt.UTC)
        self._freeze_utc(monkeypatch, austin_morning_utc)

        p = OpenMeteo("30.27", "-97.74", "imperial")
        assert p.is_daytime("30.27", "-97.74") is True

    def test_austin_tx_evening_returns_true(self, monkeypatch):
        """6 PM local in Austin — still well before sunset (~7:50 PM)."""
        import datetime as _dt

        from weather_providers.open_meteo import OpenMeteo

        austin_evening_utc = _dt.datetime(2026, 4, 11, 23, 0, 0, tzinfo=_dt.UTC)
        self._freeze_utc(monkeypatch, austin_evening_utc)

        p = OpenMeteo("30.27", "-97.74", "imperial")
        assert p.is_daytime("30.27", "-97.74") is True

    def test_austin_tx_late_night_returns_false(self, monkeypatch):
        """11:59 PM local in Austin — deep night."""
        import datetime as _dt

        from weather_providers.open_meteo import OpenMeteo

        austin_latenight_utc = _dt.datetime(2026, 4, 12, 4, 59, 0, tzinfo=_dt.UTC)
        self._freeze_utc(monkeypatch, austin_latenight_utc)

        p = OpenMeteo("30.27", "-97.74", "imperial")
        assert p.is_daytime("30.27", "-97.74") is False

    def test_greenwich_at_noon_returns_true(self, monkeypatch):
        """Sanity check: noon in Greenwich itself is daytime. Catches a
        future regression where Observer coordinates get swapped or zeroed."""
        import datetime as _dt

        from weather_providers.open_meteo import OpenMeteo

        # Noon BST on April 11 = 11:00 UTC
        greenwich_noon_utc = _dt.datetime(2026, 4, 11, 11, 0, 0, tzinfo=_dt.UTC)
        self._freeze_utc(monkeypatch, greenwich_noon_utc)

        p = OpenMeteo("51.4733", "-0.0008", "metric")
        assert p.is_daytime("51.4733", "-0.0008") is True

    def test_location_info_positional_regression(self, monkeypatch):
        """Explicit regression test for the original bug: passing lat/lon
        positionally to the buggy `LocationInfo(lat, lon)` pattern would
        default the observer to Greenwich. Pin the FIX by asserting that
        a Texas night and a Greenwich daytime give opposite answers from
        the SAME UTC moment. Before the fix they agreed (both Greenwich)."""
        import datetime as _dt

        from weather_providers.open_meteo import OpenMeteo

        # 06:50 UTC on April 11: 1:50 AM CDT Austin (night) and 07:50 BST London (day)
        shared_utc = _dt.datetime(2026, 4, 11, 6, 50, 0, tzinfo=_dt.UTC)
        self._freeze_utc(monkeypatch, shared_utc)

        p = OpenMeteo("0", "0", "imperial")  # lat/lon passed explicitly below
        assert p.is_daytime("30.27", "-97.74") is False, "Austin should be night"
        assert p.is_daytime("51.4733", "-0.0008") is True, "London should be day"
