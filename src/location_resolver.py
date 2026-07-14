"""Location resolver: shared IP-geolocation + atomic env.sh writer (#337 A4).

Extracted from ``setup_server.py`` so the new ``litclock-reresolve-location``
systemd oneshot can reuse it without dragging captive-portal / http.server
imports. Two public callers:

  * First-boot post-WiFi (``setup_server._resolve_location_from_ip`` shim).
  * On-boot reresolve oneshot (``main()`` entry point, gated on
    ``WEATHER_LOCATION_MODE=auto``).

Plus a sync-quick variant for the PWA Save Specific→Auto path (A7):
``resolve_location_from_ip(retries=False)`` runs one attempt with a tight
budget so the user's Save tap returns in under ~5s instead of the full
~33s the boot resolver tolerates.

Atomicity contract (A15, inherited from #393):
  1. ``set_system_timezone(tz)`` FIRST. If timedatectl is missing or rejects
     the value, abort — no env writes happen.
  2. Only on tz success do we ``config.atomic_update`` the location keys.

UNITS handling (A6 + A6.1 + A16):
  * The resolver always derives a country-default UNITS from
    ``country_code`` (US → imperial, everything else → metric).
  * UNITS is written ONLY when the resolved country differs from the
    persisted ``WEATHER_IP_COUNTRY``. This preserves the user's manual
    Temperature override within the same country across reboots and PWA
    Auto saves. ``WEATHER_IP_COUNTRY`` itself is written on every
    successful resolve (so the next comparison has the latest baseline).

The MODE write contract (A1):
  * Every successful resolve writes ``WEATHER_LOCATION_MODE=auto``. Both
    first-boot (where MODE was absent or empty) and PWA Auto-Save use
    this path; both want the post-write state to be "auto" explicitly.
  * The oneshot ``main()`` gates on ``MODE=auto`` BEFORE running — if a
    user has manually picked Specific in the PWA, the oneshot no-ops.
    This is the CRITICAL silent-corruption guard pinned by the T7
    regression test.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any

# #414 maintainability item #4: prefer logging over print() so journald can
# filter by level. The oneshot's `StandardOutput=journal` directive routes
# stdout to the journal regardless, but bare prints land as `notice` with no
# severity info — `journalctl -p warning -u litclock-reresolve-location`
# can't distinguish a routine "MODE=specific — skipping" line from a real
# warning about a failed env write. Logger uses module name so the unit + the
# PWA sync-quick paths share the same handler hierarchy.
log = logging.getLogger(__name__)

# IP-geo retry budget. Delays are seconds between attempts; the loop tries
# ``len(_IP_GEO_RETRY_DELAYS)+1`` times total. Backoff alone sums to
# 1+3+9 = 13s; combined with ``geocoding.ip_geolocate``'s 5s socket timeout
# per attempt, true worst-case wall clock is ~33s. Inside PR2's 120s handoff
# splash window.
_IP_GEO_RETRY_DELAYS = (1, 3, 9)


def _persisted_country(env_file: str) -> str:
    """Read the persisted WEATHER_IP_COUNTRY from env.sh, normalised to upper.
    Returns empty string when the file is missing or the key is unset."""
    if not env_file or not os.path.exists(env_file):
        return ""
    try:
        import config as _config  # noqa: PLC0415

        env = _config.load_config(env_file)
    except Exception:
        return ""
    return (env.get("WEATHER_IP_COUNTRY") or "").upper().strip()


def country_default_units(country: str) -> str:
    """A6 default rule (locked under EPIC #383): US (or unknown) → imperial,
    everything else → metric. Mirrors ``reset-setup.sh`` defaults."""
    return "imperial" if country in ("", "US") else "metric"


def update_env_location(
    lat: Any,
    lon: Any,
    *,
    location_name: str | None = None,
    units: str | None = None,
    timezone: str | None = None,
    mode: str | None = None,
    ip_country: str | None = None,
    env_file: str | None = None,
) -> bool:
    """Persist a resolved location to env.sh + system timezone.

    Atomic-write ordering (T5 / codex outside-voice, #393):
      0. Refuse incomplete location. Coordinates are persisted only as a
         complete pair (both lat AND lon) backed by a resolved timezone;
         partial or tz-less coords skip the whole write.
      1. ``set_system_timezone(timezone)`` FIRST. If timedatectl is missing
         or rejects the value, abort and skip the env write entirely.
      2. Only on tz success do we ``config.atomic_update`` the keys.

    The worst failure case is "tz set, env stale" instead of "env populated,
    tz stale" (wrong-time clock — the design-review A2 hard-block).

    New kwargs (#337):
      * ``mode``: writes ``WEATHER_LOCATION_MODE`` when not None.
      * ``ip_country``: writes ``WEATHER_IP_COUNTRY`` (uppercased) when not None.

    Caller responsibility: the country-change-only UNITS rule (A6) is
    enforced by ``resolve_location_from_ip``; this function writes
    whatever ``units`` it's given. Don't pass ``units`` when the country
    is unchanged.
    """
    if env_file is None:
        # Lazy import to avoid a circular dep — setup_server holds the
        # canonical ENV_FILE module-level constant.
        import setup_server as _ss  # noqa: PLC0415

        env_file = _ss.ENV_FILE
    if not env_file or not os.path.exists(env_file):
        return False

    # #393: coordinates are only safe to persist as a COMPLETE, tz-backed pair.
    # Two consumers gate on them and they disagree on shape:
    #   * control_server/handoff.py:_has_location requires BOTH lat AND lon;
    #   * scripts/litclock-handoff-fallback.sh checks WEATHER_LATITUDE alone.
    # Both treat a populated latitude as "timezone known" (the resolver writes
    # lat/lon and sets the system tz together). So writing a partial coord (only
    # one axis) or a tz-less coord would leave the gates inconsistent and could
    # complete the handoff with the wrong/UTC time — exactly the wrong-time clock
    # design-review A2 hard-blocks. ``.strip()`` + the "None" guard mirror
    # _has_location so " " / "None" count as absent.
    has_lat = lat is not None and str(lat).strip() not in ("", "None")
    has_lon = lon is not None and str(lon).strip() not in ("", "None")
    if (has_lat or has_lon) and not (has_lat and has_lon and timezone):
        log.warning(
            "Refusing to persist incomplete location (lat=%r, lon=%r, tz=%r) — "
            "coordinates need both axes and a resolved timezone (handoff stays gated)",
            lat,
            lon,
            timezone,
        )
        return False

    if timezone:
        # #414 maintainability item #5: import directly from geocoding (where
        # set_system_timezone lives post-extraction). Pre-#414 this lazy-imported
        # from setup_server, which dragged the captive-portal / http.server /
        # NetworkManager helpers onto the boot-critical reresolve oneshot's
        # startup path. The single remaining `import setup_server` (for ENV_FILE
        # default) is gated by `env_file is None` and only fires when callers
        # don't pass an explicit env_file.
        from geocoding import set_system_timezone  # noqa: PLC0415

        tz_ok, tz_err = set_system_timezone(timezone)
        if not tz_ok:
            log.warning("set_system_timezone(%r) failed: %s — skipping env write", timezone, tz_err)
            return False

    updates: dict[str, str] = {}
    if lat is not None and str(lat) != "":
        updates["WEATHER_LATITUDE"] = str(lat)
    if lon is not None and str(lon) != "":
        updates["WEATHER_LONGITUDE"] = str(lon)
    if location_name:
        updates["WEATHER_LOCATION_NAME"] = str(location_name)
    if units:
        updates["WEATHER_UNITS"] = str(units)
    if mode:
        updates["WEATHER_LOCATION_MODE"] = str(mode)
    if ip_country is not None:
        # Normalise to upper canonically (A6.1). Empty string is a valid
        # value (clearing the field — e.g., when IP-geo failed and we
        # explicitly want to mark "no last-detected country").
        updates["WEATHER_IP_COUNTRY"] = str(ip_country).upper().strip()
    if not updates:
        return False

    try:
        from config import atomic_update as _config_atomic_update  # noqa: PLC0415

        _config_atomic_update(updates, env_file)
        log.info(
            "Resolved location: lat=%s lon=%s name=%r units=%s tz=%s mode=%s ip_country=%s",
            lat,
            lon,
            location_name,
            units,
            timezone,
            mode,
            ip_country,
        )
        # #445: record that time-location data has been collected on this Pi
        # so /diagnostics stops flashing the grey "Not yet collected" tier
        # post-reboot. Best-effort, never fails the resolve.
        try:
            from collected_marker import mark_collected  # noqa: PLC0415

            mark_collected("time-location")
        except Exception as exc:  # pragma: no cover - defensive only
            log.debug("collected-marker write skipped: %s", exc)
        return True
    except ValueError as e:
        log.warning("invalid resolved coordinates (lat=%s, lon=%s): %s", lat, lon, e)
        return False
    except Exception as e:
        log.warning("failed to write resolved location to env: %s", e)
        return False


def resolve_location_from_ip(retries: bool = True, env_file: str | None = None) -> bool:
    """Run IP-geolocation and persist the result via ``update_env_location``.

    Returns ``True`` iff IP-geo returned usable coordinates AND the write
    succeeded (so callers — specifically the PWA Save sync-quick path — can
    surface a reliable success/failure hint without relying on a fragile
    env.sh before/after snapshot). Returns ``False`` on hard failure OR when
    the writer refused (incomplete coords, tz failure, etc.).

    ``retries=True`` (default): full retry budget — 4 attempts with 1/3/9s
    backoff. Used by first-boot post-WiFi and the on-boot reresolve oneshot
    where we can afford ~33s wall clock to recover from DNS races.

    ``retries=False`` (#337 A7, sync-quick): single attempt with no backoff.
    Used by the PWA Save Specific→Auto switch where the user is waiting on
    a Save button tap (must return in <5s on happy path). Hard-fail still
    writes nothing; the next reboot's oneshot will retry under full budget.

    Country-change UNITS rule (A6 + A6.1 + A16):
      * Resolve a new country from the IP-geo response.
      * Read the persisted ``WEATHER_IP_COUNTRY`` from env.sh.
      * If the new country differs (or persisted is empty — first resolve):
        include ``units`` in the write so it flips to the new country's default.
      * Else: omit ``units`` from the write so the user's manual Temperature
        override survives.
      * ``WEATHER_IP_COUNTRY`` itself is always written on success, so the
        next resolve's comparison has the latest baseline.

    On hard failure (all attempts return None / raise), no env writes
    happen. PR2's handoff splash + the PWA browser-tz fallback (#337 A18)
    cover the user-recovery path.
    """
    if env_file is None:
        import setup_server as _ss  # noqa: PLC0415

        env_file = _ss.ENV_FILE

    delays: tuple[int, ...] = _IP_GEO_RETRY_DELAYS if retries else ()
    attempts = len(delays) + 1

    ip_geo = None
    last_error: str | None = None
    for attempt in range(1, attempts + 1):
        try:
            from geocoding import ip_geolocate  # noqa: PLC0415

            candidate = ip_geolocate()
            # Treat all-empty responses as a soft failure and retry.
            # Without this guard, a degenerate ip-api response (cloud
            # egress, some VPN exits) would break out of the loop with
            # lat=None/lon=None and the writer would silently no-op.
            if candidate and (candidate.get("lat") or candidate.get("lon")):
                ip_geo = candidate
                break
            last_error = "ip_geolocate returned no usable coordinates"
        except Exception as e:
            last_error = repr(e)
        if attempt < attempts:
            delay = delays[attempt - 1]
            log.info("IP geolocation attempt %d/%d failed (%s); retrying in %ds", attempt, attempts, last_error, delay)
            time.sleep(delay)

    if not ip_geo:
        log.warning("IP geolocation failed after %d attempts (%s) — no location written", attempts, last_error)
        return False

    country = (ip_geo.get("country_code") or "").upper()
    default_units = country_default_units(country)

    # A6 country-change-only UNITS rule. Read persisted country; only SKIP
    # the units write when persisted matches new — that's the "user has been
    # in this country, manual Temperature override should survive" case.
    # When persisted is empty (first resolve, fresh install, post-gift-reset),
    # always write the default — there's no prior state to preserve. When
    # persisted differs from new (country change, e.g., US→UK move), also
    # write — the new country's default replaces the old.
    persisted_country = _persisted_country(env_file)
    units_to_write: str | None
    if persisted_country and persisted_country == country:
        units_to_write = None  # preserve user's manual Temperature override
    else:
        units_to_write = default_units

    # Tz fallback: if ip-api returned coords but no timezone (rare, observed
    # with some VPN exits and IPs missing geo metadata), derive tz offline
    # from lat/lon via the `timezonefinder` polygon DB.
    tz = ip_geo.get("timezone")
    lat = ip_geo.get("lat")
    lon = ip_geo.get("lon")
    if not tz and lat and lon:
        try:
            from geocoding import timezone_from_coords  # noqa: PLC0415

            tz = timezone_from_coords(lat, lon)
            if tz:
                log.info("IP-geo returned no timezone; derived %s from coords (%s, %s)", tz, lat, lon)
        except Exception as e:
            log.warning("timezone_from_coords fallback raised: %s", e)

    # Route the write through setup_server._update_env_location so existing
    # tests that monkeypatch the shim continue to work. The shim (post-#337
    # A4) is a thin delegate back into this module's update_env_location.
    # CRITICAL (#337 /review P0): pass env_file explicitly. The shim's
    # default forwards setup_server.ENV_FILE, which is None in the PWA
    # sync-quick and on-boot oneshot contexts — Codex caught the silent
    # no-op caused by the missing forward. The shim now pops env_file
    # from kwargs and overrides its default with this value.
    import setup_server as _ss  # noqa: PLC0415

    result = _ss._update_env_location(
        lat,
        lon,
        location_name=ip_geo.get("city"),
        units=units_to_write,
        timezone=tz,
        mode="auto",
        ip_country=country,
        env_file=env_file,
    )
    # The shim returns whatever update_env_location returns (bool after this
    # commit, None pre-this-commit for legacy callers that didn't update).
    # `is False` distinguishes a refused write from a raise; falsy-default
    # (None) is treated as success because the legacy contract was "no
    # exception = success."
    return result is not False


def main() -> int:
    """Entry point for ``litclock-reresolve-location.service`` (#337 A2/A8).

    Reads env.sh. CRITICAL: if ``WEATHER_LOCATION_MODE != "auto"``, exits
    cleanly (silent no-op) — this is the silent-corruption guard pinned by
    the T7 regression test. Without this gate, a user's typed Specific city
    would get clobbered by IP-geo on every reboot.

    Otherwise runs the full-retry IP-geo path.

    Exit codes:
      * 0 — success (resolved + wrote, OR no-op because MODE=specific, OR
            no-op because env.sh missing — all benign for systemd).
      * non-zero never returned: this is a best-effort oneshot per A8
        (``Before=litclock.service`` was deliberately dropped). Any
        failure logs to journal and exits 0 so systemd doesn't retry-loop.
    """
    # #414 maintainability item #4: configure logging when run as a script
    # so the levels survive into the journal. systemd's StandardOutput=journal
    # captures stderr (where logging writes by default) with metadata; plain
    # print() lands as `notice` level uniformly. Use a minimal format since
    # journald already adds timestamp + unit metadata.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    env_file = os.environ.get("LITCLOCK_ENV_FILE")
    if not env_file or not os.path.exists(env_file):
        log.warning("env.sh not found at %r — exiting cleanly", env_file)
        return 0
    try:
        import config as _config  # noqa: PLC0415

        env = _config.load_config(env_file)
    except Exception as exc:
        log.warning("could not load env.sh: %r — exiting cleanly", exc)
        return 0

    mode = (env.get("WEATHER_LOCATION_MODE") or "auto").strip() or "auto"
    if mode != "auto":
        # The whole point of this gate: a user in Specific mode picked their
        # location intentionally; the on-boot reresolve must NEVER overwrite it.
        # Tested by tests/test_location_resolver.py::test_main_no_ops_when_mode_specific.
        log.info("WEATHER_LOCATION_MODE=%r — preserving user choice, no IP-geo", mode)
        return 0

    log.info("MODE=auto — running IP-geo against env_file=%s", env_file)
    try:
        resolve_location_from_ip(retries=True, env_file=env_file)
    except Exception as exc:
        # Best-effort per A8: never crash the systemd unit. Failures already
        # log inside resolve_location_from_ip; this catch is the last line
        # of defense.
        log.warning("resolve raised non-fatally: %r", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
