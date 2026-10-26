"""Location resolver: shared IP-geolocation + atomic env.sh writer (litclock-dev#337 A4).

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

Atomicity contract (A15, inherited from litclock-dev#393):
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
import tempfile
import time
from datetime import UTC, datetime
from typing import Any

# litclock-dev#414 maintainability item #4: prefer logging over print() so journald can
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

# litclock-dev#958 — two records of a successful IP-geo, for two questions.
#
# "Did THIS boot resolve?" is what the Diagnostics "Location stale" check asks,
# and it must not depend on the wall clock: on a fresh flash the clock is the
# image's build day, on any boot it is fake-hwclock's last-shutdown time until
# NTP lands, and NTP can take longer than any wait this unit can afford (the
# bench missed first-boot's 30s window). So every resolve writes
# "<boot_id> running" to a tmpfs file, then "ok" or "failed"; a Specific save
# writes "superseded" (the location on disk is no longer IP-geo data). The check
# compares the id to the current boot_id: no clock is involved, and the file
# dies with the boot. An ABSENT file means no resolve has STARTED this boot — a
# Specific-mode boot, or this release arrived by OTA (which does not reboot) —
# and Diagnostics treats it as unknown, never stale (owner decision, litclock-dev#958
# review: the old 7-day age rule was the bug).
#
# WEATHER_LAST_IP_GEO_AT stays, for the "Last IP-geo" row a human reads — but
# it is written only when the clock is already synchronized (no wait: a wait
# blocked PWA saves and ate the boot unit's 60s budget, /review + Codex). An
# unsynced success leaves the previous value; a wrong date is never written.
IPGEO_BOOT_MARKER_DEFAULT = "/run/litclock/ipgeo-boot-id"
BOOT_ID_PATH_DEFAULT = "/proc/sys/kernel/random/boot_id"
TIMESYNC_MARKER_DEFAULT = "/run/systemd/timesync/synchronized"


def _clock_synchronized() -> bool:
    """True when systemd-timesyncd has synchronized the clock."""
    return os.path.exists(os.environ.get("LITCLOCK_TIMESYNC_MARKER") or TIMESYNC_MARKER_DEFAULT)


EXIT_TEMPFAIL = 75
BOOT_STATES = ("running", "ok", "failed", "superseded")
# Bounds the wait for the marker's sidecar lock; every holder is a few
# file operations, so this only ever bites a wedged writer.
_MARKER_LOCK_WAIT_S = 2.0


def _paths(marker: str | None = None, boot_id_path: str | None = None) -> tuple[str, str]:
    """(marker, boot_id) paths, resolved at CALL time so tests and the
    diagnostics reader see the same overrides as the writer."""
    return (
        marker or os.environ.get("LITCLOCK_IPGEO_BOOT_MARKER") or IPGEO_BOOT_MARKER_DEFAULT,
        boot_id_path or os.environ.get("LITCLOCK_BOOT_ID_PATH") or BOOT_ID_PATH_DEFAULT,
    )


def read_boot_outcome(marker: str | None = None, boot_id_path: str | None = None) -> str | None:
    """This boot's state from the marker — "running", "ok" or "failed" — or
    None: no marker, a marker from another boot, or anything unreadable. The
    ONE parser of the format, shared with the diagnostics collector."""
    marker, boot_id_path = _paths(marker, boot_id_path)
    try:
        with open(boot_id_path, encoding="ascii") as f:
            boot_id = f.read().strip()
        with open(marker, encoding="ascii") as f:
            parts = f.read().split()
    except (OSError, ValueError):
        return None
    if not boot_id or len(parts) != 2 or parts[0] != boot_id or parts[1] not in BOOT_STATES:
        return None
    return parts[1]


def _record_boot_outcome(state: str) -> None:
    """Write "<boot_id> <state>" to the IP-geo boot marker. Best-effort: a
    failure here costs at worst a wrong Diagnostics verdict, never the resolve.

    A same-boot "ok" is never demoted by "running" or "failed": once this boot
    has resolved, a later single-attempt PWA refresh that times out must not
    turn a healthy clock "stale" until the next reboot (review) — the location
    on disk is still this boot's good answer. Only "superseded" (a Specific
    save replaced it) clears it. The check and the replace run under one
    sidecar flock: the boot unit and PWA threads write concurrently, and an
    unlocked check-then-replace let a failing writer overwrite a fresh "ok"
    (Codex + review)."""
    import fcntl  # noqa: PLC0415

    marker, boot_id_path = _paths()
    tmp = None
    lock_fd = None
    try:
        lock_fd = os.open(f"{marker}.lock", os.O_CREAT | os.O_RDWR, 0o644)
        deadline = time.monotonic() + _MARKER_LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    log.warning("IP-geo boot marker lock busy; not recording %r", state)
                    return
                time.sleep(0.05)
        with open(boot_id_path, encoding="ascii") as f:
            boot_id = f.read().strip()
        if not boot_id:
            return
        if state in ("running", "failed") and read_boot_outcome(marker, boot_id_path) == "ok":
            return
        # A unique file per call: the PWA resolves on several threads of ONE
        # process, and a pid-named temp file let two writers interleave into
        # a corrupt marker (Codex).
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(marker) or ".", prefix=".ipgeo-boot-id.")
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(f"{boot_id} {state}\n")
        os.replace(tmp, marker)
        tmp = None
    except Exception as e:
        log.warning("could not record the IP-geo boot marker %s: %s", marker, e)
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        if lock_fd is not None:
            os.close(lock_fd)  # releases the flock


def mark_location_superseded() -> None:
    """A Specific location (typed city or raw coordinates) has replaced the
    IP-geo data on disk: this boot's "ok" no longer vouches for it
    (litclock-dev#958). Called by every Specific writer; best-effort."""
    _record_boot_outcome("superseded")


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
    """A6 default rule (locked under EPIC litclock-dev#383): US (or unknown) → imperial,
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
    ip_geo_at: str | None = None,
    env_file: str | None = None,
) -> bool:
    """Persist a resolved location to env.sh + system timezone.

    Atomic-write ordering (T5 / codex outside-voice, litclock-dev#393):
      0. Refuse incomplete location. Coordinates are persisted only as a
         complete pair (both lat AND lon) backed by a resolved timezone;
         partial or tz-less coords skip the whole write.
      1. ``set_system_timezone(timezone)`` FIRST. If timedatectl is missing
         or rejects the value, abort and skip the env write entirely.
      2. Only on tz success do we ``config.atomic_update`` the keys.

    The worst failure case is "tz set, env stale" instead of "env populated,
    tz stale" (wrong-time clock — the design-review A2 hard-block).

    New kwargs (litclock-dev#337):
      * ``mode``: writes ``WEATHER_LOCATION_MODE`` when not None.
      * ``ip_country``: writes ``WEATHER_IP_COUNTRY`` (uppercased) when not None.
      * ``ip_geo_at`` (litclock-dev#791): writes ``WEATHER_LAST_IP_GEO_AT``.
        Deliberately a kwarg rather than a stamp taken inside this function:
        this writer also serves the PWA's Specific-mode save, which is not an
        IP-geo event, and stamping there would make the diagnostics row — and
        the 7-day staleness anomaly that reads it — lie in the other direction.
        Only ``resolve_location_from_ip`` passes it.

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

    # litclock-dev#393: coordinates are only safe to persist as a COMPLETE, tz-backed pair.
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
        # litclock-dev#414 maintainability item #5: import directly from geocoding (where
        # set_system_timezone lives post-extraction). Pre-litclock-dev#414 this lazy-imported
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
    if ip_geo_at:
        updates["WEATHER_LAST_IP_GEO_AT"] = str(ip_geo_at)
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
        # litclock-dev#958: a Specific save replaces IP-geo data, so this
        # boot's "ok" no longer vouches for what is on disk. A later Auto
        # attempt then records its own verdict, failure included.
        if mode == "specific":
            mark_location_superseded()
        # litclock-dev#445: record that time-location data has been collected on this Pi
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
    """Resolve and persist the location from IP-geo; see
    :func:`_resolve_location_from_ip` for the contract.

    litclock-dev#958: brackets the attempt in the per-boot marker — "running"
    first, so a resolver killed by its unit's timeout leaves evidence rather
    than nothing, then "ok"/"failed" from the body. An exception records
    "failed" before it propagates: a crash is not a success."""
    _record_boot_outcome("running")
    try:
        return _resolve_location_from_ip(retries=retries, env_file=env_file)
    except BaseException:
        _record_boot_outcome("failed")
        raise


def _resolve_location_from_ip(retries: bool = True, env_file: str | None = None) -> bool:
    """Run IP-geolocation and persist the result via ``update_env_location``.

    Returns ``True`` iff IP-geo returned usable coordinates AND the write
    succeeded (so callers — specifically the PWA Save sync-quick path — can
    surface a reliable success/failure hint without relying on a fragile
    env.sh before/after snapshot). Returns ``False`` on hard failure OR when
    the writer refused (incomplete coords, tz failure, etc.).

    ``retries=True`` (default): full retry budget — 4 attempts with 1/3/9s
    backoff. Used by first-boot post-WiFi and the on-boot reresolve oneshot
    where we can afford ~33s wall clock to recover from DNS races.

    ``retries=False`` (litclock-dev#337 A7, sync-quick): single attempt with no backoff.
    Used by the PWA Save Specific→Auto switch where the user is waiting on
    a Save button tap (must return in <5s on happy path). Hard-fail still
    writes no env keys (it records "failed" in the boot marker); the next
    reboot's oneshot will retry under full budget.

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
    happen. PR2's handoff splash + the PWA browser-tz fallback (litclock-dev#337 A18)
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
        _record_boot_outcome("failed")
        return False

    # A str, or nothing: a JSON answer with a list or a number here used to
    # raise AttributeError outside the retry loop (Codex).
    raw_country = ip_geo.get("country_code")
    country = raw_country.upper() if isinstance(raw_country, str) else ""
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

    ip_geo_at: str | None = datetime.now(UTC).isoformat(timespec="seconds") if _clock_synchronized() else None
    if ip_geo_at is None:
        log.info("clock not NTP-synchronized yet; leaving WEATHER_LAST_IP_GEO_AT as it was (litclock-dev#958)")

    # Route the write through setup_server._update_env_location so existing
    # tests that monkeypatch the shim continue to work. The shim (post-litclock-dev#337
    # A4) is a thin delegate back into this module's update_env_location.
    # CRITICAL (litclock-dev#337 /review P0): pass env_file explicitly. The shim's
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
        # litclock-dev#791 — stamp the SUCCESSFUL IP-geo. Taken here, not
        # inside the writer, because the writer also serves the PWA Specific
        # save. Seconds resolution: this feeds the "Last IP-geo" row a human
        # reads and, since litclock-dev#958, only the NO-VERDICT fallback of the
        # staleness check (the per-boot marker is the primary test).
        # litclock-dev#958: only from a synchronized clock (see the boot
        # marker note at the top of this module).
        ip_geo_at=ip_geo_at,
        env_file=env_file,
    )
    # The shim returns whatever update_env_location returns (bool after this
    # commit, None pre-this-commit for legacy callers that didn't update).
    # `is False` distinguishes a refused write from a raise; falsy-default
    # (None) is treated as success because the legacy contract was "no
    # exception = success."
    # litclock-dev#958: only a PERSISTED resolve counts as "this boot resolved".
    _record_boot_outcome("ok" if result is not False else "failed")
    return result is not False


def main() -> int:
    """Entry point for ``litclock-reresolve-location.service`` (litclock-dev#337 A2/A8).

    Reads env.sh. CRITICAL: if ``WEATHER_LOCATION_MODE != "auto"``, exits
    cleanly (silent no-op) — this is the silent-corruption guard pinned by
    the T7 regression test. Without this gate, a user's typed Specific city
    would get clobbered by IP-geo on every reboot.

    Otherwise runs the full-retry IP-geo path.

    Exit codes:
      * 0 — success (resolved + wrote, OR no-op because MODE=specific, OR
            no-op because env.sh missing — all benign for systemd).
      * 75 (EX_TEMPFAIL) — MODE=auto and the lookup failed. Since
        litclock-dev#958 the unit retries on it (``Restart=on-failure``,
        bounded by its StartLimit): a clock that boots before its router
        after a power cut must not carry "failed" for its whole uptime, and
        nothing else re-resolves. Still best-effort per A8 — nothing is
        ordered after this unit, so a retry delays no paint.
    """
    # litclock-dev#414 maintainability item #4: configure logging when run as a script
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

    mode = _config.weather_location_mode(env.get("WEATHER_LOCATION_MODE"))
    if mode != "auto":
        # The whole point of this gate: a user in Specific mode picked their
        # location intentionally; the on-boot reresolve must NEVER overwrite it.
        # Tested by tests/test_location_resolver.py::test_main_no_ops_when_mode_specific.
        log.info("WEATHER_LOCATION_MODE=%r — preserving user choice, no IP-geo", mode)
        return 0

    log.info("MODE=auto — running IP-geo against env_file=%s", env_file)
    try:
        ok = resolve_location_from_ip(retries=True, env_file=env_file)
    except Exception as exc:
        # Best-effort per A8: never crash the systemd unit. Failures already
        # log inside resolve_location_from_ip; this catch is the last line
        # of defense.
        log.warning("resolve raised non-fatally: %r", exc)
        ok = False
    return 0 if ok else EXIT_TEMPFAIL


if __name__ == "__main__":
    sys.exit(main())
