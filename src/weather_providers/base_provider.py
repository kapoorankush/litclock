import datetime
import glob
import json
import logging
import os
import re
import tempfile
import time
from abc import ABC, abstractmethod

import requests

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
# Weather responses are a purely derived cache (WEATHER_TTL, default 1h, then
# re-fetched on demand), so they belong on tmpfs rather than the SD-backed
# project root. Writing them under _PROJECT_ROOT cost ~8,760 SD writes/yr when
# weather is enabled — the largest recurring app-layer flash writer (#434).
# /run/litclock is the pi-owned tmpfs dir created every boot by
# systemd/tmpfiles.d/litclock.conf (same dir as the heartbeat + status files);
# it's cleared on reboot, which is fine — a cold cache just triggers one fetch
# on the next tick. Overridable for tests + non-Pi hosts.
_CACHE_DIR = os.environ.get("LITCLOCK_WEATHER_CACHE_DIR", "/run/litclock")
# Sweeping the legacy SD-backed project root for stale caches is a one-time
# #434 migration, not a steady-state concern — record the (root, prefix) pairs
# already swept in this process so subsequent hourly fetches don't keep
# readdir-ing the flash-backed repo root for the life of the install. Reset on
# reboot (fresh process), which re-sweeps once — cheap.
_legacy_roots_swept: set = set()
# Units are interpolated into the cache filename; anything outside this
# pattern is a path-traversal vector. config.SETTINGS_ALLOWLIST also validates
# at write-ingress via atomic_update; this is defense-in-depth.
_UNITS_ALLOWED = re.compile(r"^[a-z]+$")
# Lat/lon are also interpolated into the cache filename (so a coord change
# from the M3 Settings tab gets a fresh cache miss instead of serving the
# old location's cached payload until WEATHER_TTL expires). Anything
# outside this pattern is a path-traversal vector. config.atomic_update
# already validates the env-side coord; this is the last guard before the
# filesystem write.
_COORD_ALLOWED = re.compile(r"^-?\d+(\.\d+)?$")


def _is_stale(filepath, ttl):
    """Return True if `filepath` is older than `ttl` seconds or doesn't exist."""
    verdict = True
    if os.path.isfile(filepath):
        verdict = time.time() - os.path.getmtime(filepath) > ttl
    logging.debug("_is_stale(%s) - %s", filepath, verdict)
    return verdict


class BaseWeatherProvider(ABC):
    ttl = float(os.getenv("WEATHER_TTL", 1 * 60 * 60))

    # Filename stem used to derive this provider's cache file. Subclasses
    # override to distinguish provider responses (e.g. Open-Meteo and
    # OpenWeatherMap have different schemas and must never share a cache).
    # The active `units` value is appended so WEATHER_UNITS changes write to
    # a fresh file and never serve wrong-unit values under a new-unit label
    # (bug caught during issue #175 QA, 2026-04-11: the cache layer was
    # units-agnostic and a celsius-populated cache was being read back and
    # displayed with a °F label after the user changed units).
    _cache_prefix = "weather-cache"
    # Pre-refactor cache filenames this provider used. Listed explicitly so
    # the orphan sweep can also clean up legacy files whose names don't fit
    # the new `{prefix}-{units}.json` pattern (e.g. OpenWeatherMap used to
    # write to plain `weather-cache.json` before the prefix changed).
    _legacy_cache_filenames = ()

    @abstractmethod
    def get_weather(self):
        """
        Implement this method.
        Return a dictionary in this format:
        {"temperatureMin": "2.0", "temperatureMax": "15.1", "icon": "mostly_cloudy", ...}
        """
        pass

    def c_to_f(self, celsius):
        """
        Return the Fahrenheit value from a given Celsius
        """
        return (float(celsius) * 9 / 5) + 32

    def is_daytime(self, location_lat, location_long):
        """Return whether it's daytime at the given lat/long RIGHT NOW.

        History of this function's bugs (you're welcome, future us):

        1. The original code did `LocationInfo(location_lat, location_long)`.
           `astral.LocationInfo.__init__` signature is
           `(name='Greenwich', region='England', timezone='Europe/London',
             latitude=51.4733, longitude=-0.0008328)`, so the positional args
           landed as `name` and `region`, and the observer silently defaulted
           to Greenwich. Every is_daytime check for the last N months was
           asking "is it currently daytime in *London*?" regardless of where
           the user actually was. That's why the LitClock in Austin TX shows
           a day icon at 1 AM local.

        2. The obvious fix ("use kwargs") has a subtler bug: astral's
           `sun(observer, date=dt)` takes `dt.date()` — a UTC date — and
           returns the sunrise/sunset that occur during that UTC day at the
           observer. For a location far from Greenwich, "the sunset during
           UTC April 11" can be the PREVIOUS local day's sunset. Comparing
           `sunrise <= dt <= sunset` then fails in the middle of the local
           daytime because `sunset` is from yesterday local. Tested: at 12 PM
           CDT noon in Austin, the kwargs fix still returns is_day=False.

        We avoid both bugs by using `astral.sun.elevation(observer, dt)` —
        the sun's angular elevation above the horizon at the exact moment
        in question. Positive elevation = sun is up = daytime. No date
        arithmetic, no sunrise/sunset edge cases, no timezone guesswork.

        astral is imported lazily so the weather test suite can still run
        in CI environments where astral isn't installed — only this one
        function needs it, and everything else in the module (including
        the unit-aware cache tests) works without it.
        """
        from astral import Observer
        from astral.sun import elevation

        observer = Observer(latitude=float(location_lat), longitude=float(location_long))
        now_utc = datetime.datetime.now(datetime.UTC)
        sun_elevation = elevation(observer, now_utc)
        is_day = sun_elevation > 0
        logging.debug(f"is_daytime({location_lat}, {location_long}) elev={sun_elevation:.1f}° → {is_day}")
        return is_day

    def _cache_file_path(self):
        """Return the unit + location-aware cache file path for this provider.

        Filename shape: ``{prefix}-{units}-{lat}-{lon}.json``. Including
        coordinates in the cache key means changing location from the
        Control PWA Settings tab (M3) gets a cache miss on the very next
        tick instead of serving the old location's payload until
        WEATHER_TTL expires (caught during M3 hardware QA: user moved
        from Austin TX to Dublin CA, lat/lon updated in env.sh, but
        the e-ink kept rendering Austin's temperature for an hour).

        Defense-in-depth: assert units AND coords match the allowlist
        before interpolating into a filesystem path. The setup form +
        ``config.atomic_update`` validate at ingress, but a future bug
        could bypass that, and this is the last line before ``open(w)``.

        Empty/None coords (location unconfigured) bypass the path entirely
        — callers gate on `location_lat and location_long` upstream and
        don't reach the cache path in that case. We still produce a
        deterministic filename if invoked, so subclasses that override
        ``get_weather`` can rely on it.
        """
        if not _UNITS_ALLOWED.match(self.units):
            raise ValueError(f"Invalid WEATHER_UNITS {self.units!r} — refusing to build cache path")
        lat = str(self.location_lat or "")
        lon = str(self.location_long or "")
        if not _COORD_ALLOWED.match(lat) or not _COORD_ALLOWED.match(lon):
            raise ValueError(f"Invalid coordinates lat={lat!r} lon={lon!r} — refusing to build cache path")
        return os.path.join(_CACHE_DIR, f"{self._cache_prefix}-{self.units}-{lat}-{lon}.json")

    def _sweep_orphan_caches(self, active=None):
        """Remove cache files left over from a prior `units`/location config or
        from the pre-refactor unit-less filename. Runs at the start of every
        cache read/write — it's cheap (a glob), a no-op when there are no
        orphans, and only writes logs when it actually deletes something. This
        is the defense-in-depth layer behind `_cache_file_path`: even if
        something still looks up the legacy filename, the orphan gets swept
        before it can be read.

        Always sweeps the active tmpfs cache dir (`_CACHE_DIR`) — units/coords
        can change on any tick. The legacy SD-backed project root
        (`_PROJECT_ROOT`) is swept only ONCE per process (via
        `_legacy_roots_swept`): #434 moved the cache onto tmpfs, so scanning
        the old root is a one-time migration that drops any stale flash-
        resident file on the first fetch — no need to readdir the SD card on
        every hourly fetch thereafter.

        `active` is the current cache path; pass it in to avoid recomputing it
        (the caller already has it), falling back to computing it if omitted.
        """
        if active is None:
            active = self._cache_file_path()
        roots = [_CACHE_DIR]
        legacy_key = (_PROJECT_ROOT, self._cache_prefix)
        if legacy_key not in _legacy_roots_swept:
            _legacy_roots_swept.add(legacy_key)
            roots.append(_PROJECT_ROOT)
        candidates = set()
        # dict.fromkeys dedups when _CACHE_DIR == _PROJECT_ROOT (dev/override
        # hosts pointing both at one dir) so we don't glob the same dir twice.
        for base in dict.fromkeys(roots):
            # Files matching "weather-cache-openmeteo-*.json" etc. (prior units)
            candidates.update(glob.glob(os.path.join(base, f"{self._cache_prefix}-*.json")))
            # Pre-refactor unit-less filename derived from the current prefix
            # (e.g. "weather-cache-openmeteo.json" for Open-Meteo)
            legacy = os.path.join(base, f"{self._cache_prefix}.json")
            if os.path.exists(legacy):
                candidates.add(legacy)
            # Additional subclass-declared legacy filenames (e.g. OpenWeatherMap
            # used to write to plain "weather-cache.json")
            for name in self._legacy_cache_filenames:
                p = os.path.join(base, name)
                if os.path.exists(p):
                    candidates.add(p)
        for f in candidates:
            if f == active:
                continue
            try:
                os.unlink(f)
                logging.info(f"Removed orphan weather cache: {os.path.basename(f)}")
            except OSError as e:
                logging.debug(f"Could not remove orphan cache {f}: {e}")

    def _write_cache(self, cache_file_name, payload):
        """Persist a fetched response to the cache, best-effort + atomic.

        Best-effort: the tmpfs cache dir (#434) is normally present (created at
        boot by systemd/tmpfiles.d and kept warm by the heartbeat writer), but
        if it's missing/unwritable we log and return rather than propagate —
        the caller already holds the live data and must still render.

        Atomic: write to a temp file in the same dir then ``os.replace`` so a
        process crash mid-write can't leave a truncated JSON that the next
        cache-hit read would choke on (matching ``literary_clock``'s
        ``_write_status_file`` and ``collected_marker._atomic_write``).
        """
        cache_dir = os.path.dirname(cache_file_name)
        tmp_path = None
        try:
            if cache_dir:
                os.makedirs(cache_dir, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=cache_dir or None, prefix=".weather-cache.tmp.")
            with os.fdopen(fd, "w") as text_file:
                json.dump(payload, text_file, indent=4)
            os.replace(tmp_path, cache_file_name)
            tmp_path = None
        except OSError as e:
            logging.warning(f"Weather cache write skipped ({cache_file_name}): {e}")
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def get_response_data(self, url, headers=None, cache_file_name=None):
        """
        Perform an HTTP GET for a `url` with optional `headers`.
        Caches the response in `cache_file_name` for WEATHER_TTL seconds.
        If `cache_file_name` is None, uses the provider's unit-aware cache
        file. Returns the response as JSON.
        """
        if headers is None:
            headers = {}
        if cache_file_name is None:
            cache_file_name = self._cache_file_path()
            self._sweep_orphan_caches(active=cache_file_name)
        response_json = False

        if _is_stale(cache_file_name, self.ttl):
            logging.info("Cache file is stale. Fetching from source.")
            try:
                timeout = int(os.getenv("WEATHER_API_TIMEOUT", 15))
                response = requests.get(url, headers=headers, timeout=timeout)
                response.raise_for_status()
                response_data = response.text
                response_json = json.loads(response_data)
            except requests.exceptions.Timeout as error:
                logging.error(f"Request timed out: {error}")
                raise
            except Exception as error:
                logging.error(f"Request failed: {error}")
                if "response" in dir() and response is not None:
                    logging.error(f"Response text: {response.text}")
                    logging.error(f"Response headers: {response.headers}")
                raise
            # Persisting the response is best-effort and happens AFTER the fetch
            # try-block so a cache-write failure can never sink the render — we
            # already hold the freshly-fetched data (#434 review). On a real Pi
            # the pi user can't create /run/litclock if it's somehow missing
            # (root-owned /run parent), so a hard failure here would drop
            # weather entirely; instead we log and return the live data.
            self._write_cache(cache_file_name, response_json)
        else:
            logging.info("Found in cache.")
            with open(cache_file_name) as file:
                return json.loads(file.read())
        return response_json
