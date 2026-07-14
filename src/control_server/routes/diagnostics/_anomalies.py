"""Anomaly detection for the /api/diagnostics surface.

Split out of the pre-#419 monolithic ``routes/diagnostics.py`` (M1). The
``_compute_anomalies`` function is the server's authoritative answer to
"which sections does PR3's template open by default on first paint" —
its return list is intersected against :data:`SECTION_IDS` in the
``"anomalies"`` field of the route's JSON envelope.

Thresholds are locked by /plan-design-review P7.1=A. Mutating any
threshold here without updating both the design doc + the dashboard
documentation is a regression.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flask import current_app

from ._collectors import (
    DEFAULT_COLLECTED_MARKER_PATH,
    DEFAULT_LAST_RENDERED_IP_PATH,
    _coerce_float,
    _is_oneshot_nonanomaly,
)

# Anomaly thresholds locked by /plan-design-review P7.1=A.
ANOMALY_CPU_TEMP_C = 78.0
ANOMALY_DISK_FREE_PCT = 10.0
ANOMALY_MEMORY_FREE_MB = 50.0
ANOMALY_SIGNAL_DBM = -75
ANOMALY_DHCP_AGE_S = 24 * 3600
ANOMALY_LAST_IPGEO_AGE_S = 7 * 24 * 3600
ANOMALY_QUOTE_AGE_S = 90
ANOMALY_RECENT_LOG_LOOKBACK = 50  # last N entries scanned for ERROR-level

__all__ = [
    "ANOMALY_CPU_TEMP_C",
    "ANOMALY_DHCP_AGE_S",
    "ANOMALY_DISK_FREE_PCT",
    "ANOMALY_LAST_IPGEO_AGE_S",
    "ANOMALY_MEMORY_FREE_MB",
    "ANOMALY_QUOTE_AGE_S",
    "ANOMALY_RECENT_LOG_LOOKBACK",
    "ANOMALY_SIGNAL_DBM",
    "_compute_anomalies",
    "_compute_section_states",
    "_compute_uncollected",
    "_is_numeric",
    "_read_collected_sections",
    "_recent_logs_contain_error",
]


def _is_numeric(value: Any) -> bool:
    """``isinstance(True, int) is True`` in Python — booleans are ints. A
    JSON ``true`` round-tripping through a handcrafted writer would surface
    as ``1`` and pass the threshold checks (and ``True == True > 78.0`` is
    False but ``time.time() - 1.0`` makes ``picked_at`` look ancient).
    Per LitClock learning #372: filter booleans explicitly from numeric reads.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _compute_anomalies(values: dict[str, Any]) -> list[str]:
    """Return the list of section IDs whose data tripped an anomaly.

    Locked by /plan-design-review OV-D-I-revised + P7.1=A. The list is the
    server's authoritative answer to "which <details> sections open by
    default on first paint." PR3's template iterates it.

    Anomaly conditions per section:

    - ``build-version`` — NEVER (info-only).
    - ``system`` — CPU > 78 °C OR disk < 10 % free OR memory < 50 MB free.
    - ``network`` — signal < -75 dBm OR LAN IP missing OR last DHCP > 24 h.
    - ``time-location`` — weather enabled AND (city empty OR mode=specific
      with empty place OR last IP-geo > 7 days).
    - ``services`` — ANY non-oneshot unit non-active. ``DIAG_ONESHOT_UNITS``
      is the explicit allowlist of post-boot-inactive-by-design services;
      members also get a pass on the transient ``activating``/``deactivating``
      lifecycle states (#443 — litclock.service cycles through these every
      minute during the quote paint). ``failed`` still trips for oneshots.
      #433 dropped the prior ``has_journal_access`` trigger (per
      /plan-eng-review A-3 + CMT-2) — it false-positived on healthy clocks
      whenever Pi Zero 2W IO contention pushed journalctl over the 8 s
      budget, and the hint copy advised the wrong group on Bookworm.
    - ``last-quote`` — picked_at > 90 s OR quote empty.
    - ``setup-markers`` — .handoff-complete missing post-setup.
    - ``recent-log-entries`` — any ERROR-level entry in the last
      ``ANOMALY_RECENT_LOG_LOOKBACK`` buffer entries.
    """
    anomalies: list[str] = []
    now = datetime.now(tz=UTC)

    # System
    sys_anomaly = False
    cpu = _coerce_float(values.get("cpu_temp_c"))
    if cpu is not None and cpu > ANOMALY_CPU_TEMP_C:
        sys_anomaly = True
    disk = _coerce_float(values.get("disk_free_pct"))
    if disk is not None and disk < ANOMALY_DISK_FREE_PCT:
        sys_anomaly = True
    mem = values.get("memory_free_mb")
    if _is_numeric(mem) and mem < ANOMALY_MEMORY_FREE_MB:
        sys_anomaly = True
    if sys_anomaly:
        anomalies.append("system")

    # Network
    net_anomaly = False
    signal = values.get("signal_dbm")
    if _is_numeric(signal) and signal < ANOMALY_SIGNAL_DBM:
        net_anomaly = True
    if not values.get("lan_ip"):
        net_anomaly = True
    dhcp_iso = values.get("last_dhcp_at")
    if isinstance(dhcp_iso, str) and dhcp_iso:
        try:
            dhcp_dt = datetime.fromisoformat(dhcp_iso)
            if dhcp_dt.tzinfo is None:
                dhcp_dt = dhcp_dt.replace(tzinfo=UTC)
            age = (now - dhcp_dt).total_seconds()
            if age > ANOMALY_DHCP_AGE_S:
                net_anomaly = True
        except ValueError:
            pass
    if net_anomaly:
        anomalies.append("network")

    # Time & location
    if values.get("weather_enabled") in (True, "true", "1"):
        tl_anomaly = False
        if not values.get("weather_location_name"):
            tl_anomaly = True
        ipgeo_iso = values.get("last_ip_geo_at")
        if isinstance(ipgeo_iso, str) and ipgeo_iso:
            try:
                ipgeo_dt = datetime.fromisoformat(ipgeo_iso)
                if ipgeo_dt.tzinfo is None:
                    ipgeo_dt = ipgeo_dt.replace(tzinfo=UTC)
                age = (now - ipgeo_dt).total_seconds()
                if age > ANOMALY_LAST_IPGEO_AGE_S:
                    tl_anomaly = True
            except ValueError:
                pass
        if tl_anomaly:
            anomalies.append("time-location")

    # Services
    services = values.get("service_states") or {}
    if isinstance(services, dict):
        svc_anomaly = False
        for unit, info in services.items():
            if not isinstance(info, dict):
                continue
            state = info.get("state")
            # Oneshot units cycle inactive → activating → active → inactive
            # in 2-5 s during the per-minute quote paint (litclock.service).
            # A poll landing in the activating/deactivating window is the
            # NORMAL lifecycle, not a failure (#443) — skip it via the shared
            # _is_oneshot_nonanomaly predicate (kept in lockstep with the
            # lazy-tail filter in _is_obviously_healthy + the row chip tone in
            # _build_service_states). ``failed`` is excluded by that predicate:
            # a failed oneshot is a real failure and still falls through to the
            # anomaly branch below.
            if _is_oneshot_nonanomaly(unit, state):
                continue
            if state in ("failed", "activating", "deactivating"):
                svc_anomaly = True
                break
            if state == "inactive" and "timer" not in unit:
                svc_anomaly = True
                break
        if svc_anomaly:
            anomalies.append("services")

    # Last quote
    quote_anomaly = False
    if not values.get("quote"):
        quote_anomaly = True
    picked_at = values.get("picked_at")
    if _is_numeric(picked_at):
        age = time.time() - float(picked_at)
        if age > ANOMALY_QUOTE_AGE_S:
            quote_anomaly = True
    if quote_anomaly:
        anomalies.append("last-quote")

    # Setup markers
    if values.get("setup_complete") and not values.get("handoff_complete"):
        anomalies.append("setup-markers")

    # Recent log entries. The on-page snapshot is 4 entries (rendered in
    # values["recent_log_entries"]), but the docstring contract is "any
    # ERROR-level in the last ANOMALY_RECENT_LOG_LOOKBACK" — fetch a wider
    # slice from the handler so a stale ERROR 5-50 entries back still opens
    # the section.
    if _recent_logs_contain_error(values):
        anomalies.append("recent-log-entries")

    return anomalies


def _recent_logs_contain_error(values: dict[str, Any]) -> bool:
    """True iff any of the last ``ANOMALY_RECENT_LOG_LOOKBACK`` entries
    carries level == ERROR. Falls back to the snapshot in ``values`` when
    the handler is unavailable (test mode)."""
    try:
        from ...log_buffer import get_memory_handler  # noqa: PLC0415
    except ImportError:
        get_memory_handler = None  # type: ignore[assignment]
    handler = get_memory_handler() if get_memory_handler else None
    if handler is not None:
        entries = handler.get_logs(limit=ANOMALY_RECENT_LOG_LOOKBACK)
        return any(getattr(e, "level", None) == "ERROR" for e in entries)
    recent = values.get("recent_log_entries") or []
    if not isinstance(recent, list):
        return False
    return any(isinstance(e, dict) and e.get("level") == "ERROR" for e in recent)


def _read_collected_sections() -> set[str] | None:
    """Parsed section keys from the persistent collected-marker (#445), or
    ``None`` to signal "fall back to the legacy tmpfs check".

    The marker (``/var/lib/litclock/.last-collected-marker.json``) answers
    "has this section EVER been collected on this Pi" and survives reboot —
    unlike the tmpfs ``last-rendered-ip`` file the v0.214.4 predicate keyed
    off, which is wiped at boot and caused a ~5-10 s grey flicker on a
    healthy clock until the NM dispatcher re-fired.

    ``None`` covers BOTH cases where the persistent marker can't answer:
    the file is absent (an existing Pi that hasn't OTA'd, or a fresh Pi
    before its first network/IP-geo event) OR a read/parse error. In every
    such case the caller reverts to the exact v0.214.4 behavior, so the
    migration is seamless. A well-formed object returns the set of string
    keys present; an empty object returns ``set()`` (the Pi has a marker but
    this section was never recorded → still genuinely uncollected)."""
    try:
        path = current_app.config.get("DIAG_COLLECTED_MARKER_PATH", DEFAULT_COLLECTED_MARKER_PATH)
    except RuntimeError:
        # Outside an app context (unit test calls), fall back to the env
        # default. Stays pure-Python and never raises.
        path = DEFAULT_COLLECTED_MARKER_PATH
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        # Absent, unreadable, or torn mid-write → legacy fallback.
        return None
    if not isinstance(data, dict):
        return None
    return {k for k in data if isinstance(k, str)}


def _compute_uncollected(values: dict[str, Any]) -> list[str]:
    """Return the section IDs in the muted "Not yet collected" tier (#432).

    This is the RAW predicate output (pre-precedence). Callers MUST go
    through :func:`_compute_section_states` to apply the uncollected-wins
    truth table — see that helper's docstring for the full table + the
    rationale for inverting the plan's locked anomaly-wins direction.

    "Collected" is now sourced from the persistent marker (#445): a section
    is uncollected only if its key is NOT in
    ``/var/lib/litclock/.last-collected-marker.json``. When that marker is
    absent/unreadable (:func:`_read_collected_sections` returns ``None``),
    each section falls back to its v0.214.4 signal — the tmpfs
    ``last-rendered-ip`` existence check for ``network``, and the env-only
    gate for ``time-location`` — so existing Pis behave unchanged until the
    first dispatcher / IP-geo write lands the persistent marker.

    Per D3 (the rest of each gate is unchanged):

    - ``network`` — uncollected iff (1) network never collected, AND (2)
      ``values["lan_ip"]`` is empty, AND (3) ``values["ssid"]`` is empty
      (SSID-present sanity gate: an association without an IP is a real
      DHCP-failure anomaly, NOT an "unrecorded data" state).
    - ``time-location`` — uncollected iff (1) weather is enabled, AND (2)
      ``WEATHER_LOCATION_MODE`` is ``auto`` (a user-configured ``specific``
      mode with no resolved name stays a real anomaly), AND (3)
      time-location never collected, AND (4) ``weather_location_name`` is
      empty, AND (5) ``last_ip_geo_at`` is empty.
    """
    out: list[str] = []
    collected = _read_collected_sections()

    # network — gate per D3, PLUS an "independent anomaly" carve-out
    # (adversarial-review Fix A): the helper applies uncollected-wins
    # precedence over the WHOLE section, so if low-signal or stale-DHCP
    # independently trip the network anomaly on the same poll, we'd mask a
    # real failure as "Just settling in." Refuse to mark uncollected when any
    # non-empty-lan_ip anomaly is firing.
    if collected is not None:
        network_never_collected = "network" not in collected
    else:
        # Legacy fallback: the reboot-wiped tmpfs marker's existence.
        try:
            marker_path = current_app.config.get("DIAG_LAST_IP_PATH", DEFAULT_LAST_RENDERED_IP_PATH)
        except RuntimeError:
            marker_path = DEFAULT_LAST_RENDERED_IP_PATH
        try:
            marker_exists = bool(marker_path) and Path(marker_path).exists()
        except OSError:
            # Filesystem unmount or EACCES on /run/ — fail-conservative:
            # treat the marker as present so the section stays in its
            # last-known state instead of flapping to grey.
            marker_exists = True
        network_never_collected = not marker_exists
    if network_never_collected and not values.get("lan_ip") and not values.get("ssid"):
        # Carve-out 1: signal anomaly. If the radio is reporting a usable
        # signal value AND it's below the anomaly threshold, that's a real
        # signal-degradation problem — do NOT mute as uncollected.
        signal = values.get("signal_dbm")
        signal_is_anomalous = (
            isinstance(signal, (int, float)) and not isinstance(signal, bool) and signal < ANOMALY_SIGNAL_DBM
        )
        # Carve-out 2: stale DHCP age. If last_dhcp_at parses to a real
        # timestamp older than 24h, that's a real DHCP-renewal failure —
        # not "data was never collected."
        dhcp_is_anomalous = False
        dhcp_iso = values.get("last_dhcp_at")
        if isinstance(dhcp_iso, str) and dhcp_iso:
            try:
                dhcp_dt = datetime.fromisoformat(dhcp_iso)
                if dhcp_dt.tzinfo is None:
                    dhcp_dt = dhcp_dt.replace(tzinfo=UTC)
                age = (datetime.now(tz=UTC) - dhcp_dt).total_seconds()
                if age > ANOMALY_DHCP_AGE_S:
                    dhcp_is_anomalous = True
            except ValueError:
                pass
        if not signal_is_anomalous and not dhcp_is_anomalous:
            out.append("network")

    # time-location — gate per D3. Fix C: legacy / pre-#337 env files don't
    # set WEATHER_LOCATION_MODE; the rest of the app treats a missing mode as
    # `auto`. Accept None alongside "auto" so those Pis get the grey tier
    # instead of the orange false positive this change was meant to remove.
    # When the persistent marker is absent (collected is None), preserve the
    # v0.214.4 env-only behavior (no marker gate); otherwise require the
    # time-location key to be missing too.
    weather_enabled = values.get("weather_enabled")
    if weather_enabled in (True, "true", "1"):
        mode = values.get("weather_location_mode")
        tl_never_collected = True if collected is None else "time-location" not in collected
        if (
            mode in ("auto", None, "")
            and tl_never_collected
            and not values.get("weather_location_name")
            and not values.get("last_ip_geo_at")
        ):
            out.append("time-location")

    return out


def _compute_section_states(
    values: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """Return ``(anomalies, uncollected)`` with uncollected-wins precedence
    on overlap. SINGLE source of truth for the truth table (#432).

    +-----------------+-----------------------+--------------------+
    | S ∈ anom_raw    | S ∈ uncollected_raw   | Result             |
    +=================+=======================+====================+
    | true            | true                  | **uncollected**    |
    | true            | false                 | anomaly            |
    | false           | true                  | uncollected        |
    | false           | false                 | ok                 |
    +-----------------+-----------------------+--------------------+

    **Deviation from the plan's locked table (which said "anomaly wins"
    on overlap).** The plan author wrote: "Empirically, the only section
    that can satisfy BOTH is `network` (when SSID present + lan_ip empty
    + marker missing — but the SSID-present sanity gate at D3 already
    excludes this from `uncollected`, so the overlap row is unreachable
    in v0.214.4)." That overlap analysis is incomplete: the existing
    :func:`_compute_anomalies` network branch unconditionally trips on
    ``lan_ip=""`` regardless of SSID, and the time-location branch trips
    on ``weather_location_name=""`` regardless of mode — so in the
    user-reported fresh-flash case (marker absent + lan_ip empty + ssid
    empty + name empty), BOTH predicates fire for BOTH sections. Anomaly-
    wins precedence would leave the user seeing the same orange "Connection
    issue" + "Location stale" pills that #432 was opened to fix.

    Uncollected wins matches the plan's stated INTENT (gift recipient
    sees grey, not orange) and is the only precedence that actually closes
    the user-reported bug. Both predicates stay pure + independently
    testable; the helper is still the single place precedence is applied.
    """
    anomalies_raw = _compute_anomalies(values)
    uncollected = _compute_uncollected(values)
    uncollected_set = set(uncollected)
    anomalies = [s for s in anomalies_raw if s not in uncollected_set]
    return anomalies, uncollected
