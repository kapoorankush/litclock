"""GET /api/status — read-only system + current-quote facts.

Per PR #245 M2: the PWA Status tab (the "memorable thing" front-door
moment) reads ONE endpoint to populate both the literary hero card and
the 5-row system facts list under it. The hero quote MUST mirror what's
on the e-ink right now — `literary_clock.py` writes
`/var/run/litclock-current-quote.json` after each successful render, and
this route reads from that file. PWA never calls `get_current_quote()`
independently — that would let the e-ink and the PWA disagree by up to a
minute (locked decision OV3).

Subprocess calls (nmcli for SSID) are short-cached so the 3s reconnect-
probe + manual refresh cadence doesn't fork shell helpers excessively.
The "Last update" row reads on-disk files (update.status + lkg-sha) —
no subprocess, no cache, just a direct read on each request (#330).
Each helper is module-level so tests can monkeypatch them without
touching the real system.
"""

from __future__ import annotations

import json
import os
import stat
import time as _time
from datetime import UTC, datetime
from pathlib import Path

from flask import Blueprint, current_app, jsonify

from ..update_state import (
    MAX_LAST_UPDATE_FILE_BYTES,
    MAX_LKG_SHA_FILE_BYTES,
    MAX_STATUS_FILE_BYTES,
    safe_read_json,
    safe_read_text,
)
from ..version import get_version

bp = Blueprint("status", __name__)

# Default status-file path — must match the producer side in
# src/literary_clock.py:STATUS_FILE. Lives under /run/litclock (tmpfs,
# pi:pi-owned per the #241 tmpfiles.d entry) so the writer doesn't hit
# Permission-denied on /var/run. Override via env so tests can use a tmpfile.
DEFAULT_STATUS_FILE = os.environ.get("LITCLOCK_STATUS_FILE", "/run/litclock/current-quote.json")

# Last-update signal sources (#330 + #334). update.sh writes all three:
#   1. /run/litclock/update.status — phase reading-list, terminal state=complete
#      includes finished_at_unix + to_version (short SHA). tmpfs, cleared on reboot.
#   2. /var/lib/litclock/last-update.json — persistent mirror of (1) on terminal
#      state=complete. Survives reboot-during-15-min-LKG-soak (#334 Window 1)
#      and offline-graceful-exit (#334 Window 2). Same shape as (1).
#   3. /var/lib/litclock/lkg-sha — full HEAD SHA written after a healthy soak
#      by litclock-lkg-record.sh. Persistent across reboots.
# Status row prefers (1) for freshest signal, falls back to (2) for the
# narrow reboot/offline windows above, then to (3) so the row survives
# pre-#334 Pis where last-update.json was never written.
DEFAULT_UPDATE_STATUS_FILE = os.environ.get("LITCLOCK_UPDATE_STATUS_FILE", "/run/litclock/update.status")
DEFAULT_LAST_UPDATE_FILE = os.environ.get("LITCLOCK_LAST_UPDATE_FILE", "/var/lib/litclock/last-update.json")
DEFAULT_LKG_SHA_FILE = os.environ.get("LITCLOCK_LKG_SHA_FILE", "/var/lib/litclock/lkg-sha")
SHORT_SHA_LEN = 7

# #274 follow-up — Phase 3 skip marker (set by update.sh on rc=75 flock
# timeout, cleared on a clean Phase 3 run). mtime-only — no body. The
# Status row clamps "fresh" to within the last day so the banner self-
# clears even if the next update is never run.
DEFAULT_PHASE3_SKIPPED_FILE = os.environ.get("LITCLOCK_PHASE3_SKIPPED_FILE", "/var/lib/litclock/update-phase3-skipped")
# Treat the marker as stale after 1 day. Weekly cron tick retries Phase 3,
# so a 1-day window covers "missed Sunday's tick + might-miss-next-Sunday"
# without leaving an indefinitely-stale glyph if the marker never gets
# cleaned (e.g. update.sh disabled / cron stopped firing).
PHASE3_SKIP_FRESH_WINDOW_S = 86400

# #274 follow-up — adversarial-review P1: budget for treating a
# `state=running` update.status entry as fresh. Past this, assume update.sh
# died (SIGKILL / OOM / power loss) without writing the terminal
# state=complete or state=failed_* — /run is tmpfs and clears at reboot,
# but a user who only interacts via the web (no reboot) would otherwise
# see the Settings "Update in progress" banner stuck forever every 15s.
# Budget = systemd's TimeoutStartSec=600s + 90s SIGKILL grace + ~20min
# headroom for the slow Pi Zero 2W pip install on a contended SD card.
# Past this window, treat as stale and return None so the banner self-clears.
UPDATE_RUNNING_TIMEOUT_S = 1800

# Stale threshold (#245 D2). Banner appears in the PWA when picked_at_age_s
# exceeds this; corresponds to ~90 seconds of dead clock-tick service.
STALE_THRESHOLD_S = 90

# Service uptime baseline. Captured at module import = process start. systemd
# restart resets it, which is the signal the PWA reconnect probe needs.
_SERVICE_START_MONOTONIC = _time.monotonic()

# The cache and helper now live in control_server/_subprocess.py so
# /api/diagnostics can share them with a longer ttl (#416 / C2=A). Pre-
# extraction grep across master + tests found zero monkey-patches of the
# old `_subprocess_cache` or `_cached_subprocess` names — the rumored
# backwards-compat constituency turned out not to exist. Tests that need
# to clear cache state call ``_subprocess.clear_cache()`` directly.
from .._subprocess import cached_subprocess as _cached_subprocess_impl  # noqa: E402


def _cached_subprocess(key: str, argv: list[str], timeout: float = 2.0) -> str | None:
    """Status's pre-extraction call shape.

    ``timeout`` is the per-call subprocess timeout (default 2 s — unchanged).
    The cache TTL stays at the helper's default (5 s); diagnostics calls
    :func:`._subprocess.cached_subprocess` directly with ``ttl=20`` for its
    longer steady-state cache window.

    Return type widened to ``str | None`` in #428 PR1a: the underlying
    helper returns ``None`` on subprocess failure (timeout / missing
    binary / SubprocessError), distinct from ``""`` (the binary ran but
    produced no stdout). No production caller of this shim exists today;
    the test at ``tests/test_subprocess_helper.py`` is its only consumer,
    and it asserts a successful echo (returns ``str``).
    """
    return _cached_subprocess_impl(key, argv, timeout=timeout)


def _read_status_file(path: Path) -> dict | None:
    """Load the producer-side status JSON. Returns None on any read failure
    (file missing, malformed JSON, perms). The PWA's stale banner takes
    over when this returns None."""
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _wifi_ssid() -> str:
    """Active WiFi SSID via nmcli. Returns empty string when not connected.

    Thin alias over :func:`control_server._network.read_ssid` since #419
    M3. Kept as a 1-line wrapper so existing test monkey-patches against
    ``routes.status._wifi_ssid`` (notably ``tests/test_control_server.py``
    L2525, which stubs a 30-char SSID to verify Jinja truncation) continue
    to work without source changes.
    """
    from .._network import read_ssid  # noqa: PLC0415 — keep _network import scoped

    return read_ssid() or ""


def _read_env_file_settings(env_file: str | None = None) -> dict[str, str]:
    """Thin wrapper around :func:`control_server._env.read_env_settings`.

    Kept for backwards compatibility — tests and ``_weather_city`` import this
    name. The actual logic lives in ``control_server/_env.py`` so the
    Diagnostics route (#416) and Status share one parser.
    """
    from .._env import read_env_settings  # noqa: PLC0415

    return read_env_settings(env_file)


def _weather_city(env_file: str | None = None) -> str:
    """User-facing weather location label. Reads `WEATHER_LOCATION_NAME`
    from env.sh when set (M3 Settings tab will populate this on save).
    Falls back to formatted lat/lon. Returns empty string when weather is
    disabled (no settings present)."""
    settings = _read_env_file_settings(env_file)
    name = settings.get("WEATHER_LOCATION_NAME", "").strip()
    if name:
        return name
    lat = settings.get("WEATHER_LATITUDE", "").strip()
    lon = settings.get("WEATHER_LONGITUDE", "").strip()
    if lat and lon:
        try:
            return f"{float(lat):.2f}, {float(lon):.2f}"
        except ValueError:
            return ""
    return ""


def _resolve_phase3_skipped_at(phase3_skipped_file: Path | None = None) -> float | None:
    """Return the mtime (unix epoch) of the Phase 3 skip marker if it
    exists AND is fresh (mtime within ``PHASE3_SKIP_FRESH_WINDOW_S``).
    Otherwise ``None``.

    The marker is written by update.sh on Phase 3 rc=75 (flock timeout)
    and cleared on a clean Phase 3 run. Reader-side staleness clamp
    means the Status banner self-clears after a day even if the next
    update.sh never runs to clear the file (cron stopped, unit
    disabled, etc.) — same pattern as #241 D2's post-update-grace.

    Symlinks / FIFOs / directories return ``None`` (use lstat so a
    symlink doesn't follow). Mtime-in-the-future (clock drift pre-NTP
    on Pi Zero 2W) clamps to ``None`` rather than reporting a negative
    age.
    """
    path = phase3_skipped_file or Path(DEFAULT_PHASE3_SKIPPED_FILE)
    try:
        st = os.lstat(path)
    except OSError:
        return None
    # Refuse anything that isn't a regular file. Defense-in-depth against
    # a planted symlink/FIFO that an os.stat would follow.
    if not stat.S_ISREG(st.st_mode):
        return None
    now = _time.time()
    age_s = now - st.st_mtime
    if age_s < 0 or age_s >= PHASE3_SKIP_FRESH_WINDOW_S:
        return None
    return float(st.st_mtime)


def _resolve_update_progress(
    update_status_file: Path | None = None,
) -> tuple[str | None, int | None]:
    """Return ``(state, phase_index)`` from /run/litclock/update.status.

    Used by the Settings tab banner (#274 follow-up #2) so the PWA can
    surface "Update in progress — settings briefly locked" when an
    update.sh run is mid-flight. The Save button stays enabled; the
    banner is purely advisory. Phase 3 (env.sh merge) is when the
    sidecar flock is actually held; Phase 4 (pip install) is the long
    phase where users are most likely to see the banner.

    Returns ``(None, None)`` when the file is missing / unreadable /
    not a regular file / has no ``state`` field. Caller treats ``None``
    as "no update in flight, hide banner."

    Adversarial-review P1 staleness clamp: if ``state == "running"`` but
    ``started_at_unix`` is older than ``UPDATE_RUNNING_TIMEOUT_S``,
    treat as stale and return ``(None, None)``. Covers the case where
    update.sh died from SIGKILL / OOM / power loss without writing a
    terminal state — /run is tmpfs and clears at reboot, but a user
    who never reboots would otherwise see the Settings banner stuck
    on indefinitely.
    """
    status_path = update_status_file or Path(DEFAULT_UPDATE_STATUS_FILE)
    data = safe_read_json(status_path, MAX_STATUS_FILE_BYTES)
    if not isinstance(data, dict):
        return None, None
    state_raw = data.get("state")
    state = state_raw if isinstance(state_raw, str) else None
    # `isinstance(True, int) is True` in Python — exclude booleans
    # explicitly so a hand-edited or buggy writer emitting
    # `"phase_index": true` doesn't round-trip as the integer 1.
    phase_raw = data.get("phase_index")
    if isinstance(phase_raw, int) and not isinstance(phase_raw, bool):
        phase = phase_raw
    else:
        phase = None
    # Staleness clamp on state=running. Other terminal states
    # (complete / failed_*) are point-in-time and don't need clamping.
    if state == "running":
        started_raw = data.get("started_at_unix")
        if isinstance(started_raw, (int, float)) and not isinstance(started_raw, bool):
            try:
                age_s = _time.time() - float(started_raw)
            except (OSError, OverflowError, ValueError):
                age_s = None
            if age_s is not None and age_s > UPDATE_RUNNING_TIMEOUT_S:
                return None, None
    return state, phase


def _payload_to_last_update(data: dict | None) -> tuple[str | None, str | None] | None:
    """Common shape extractor for sources 1 + 2 — both /run/litclock/update.status
    (state=complete) and /var/lib/litclock/last-update.json carry the same
    finished_at_unix + to_version fields. Returns ``(iso, version)`` on a
    successful extraction, or ``None`` if the payload is missing /
    non-complete / has no usable timestamp."""
    if not isinstance(data, dict) or data.get("state") != "complete":
        return None
    finished = data.get("finished_at_unix")
    to_version = data.get("to_version")
    if not isinstance(finished, (int, float)):
        return None
    try:
        iso = datetime.fromtimestamp(float(finished), tz=UTC).isoformat()
    except (OSError, OverflowError, ValueError):
        return None
    version = to_version if isinstance(to_version, str) and to_version else None
    return iso, version


def _resolve_last_update(
    update_status_file: Path | None = None,
    last_update_file: Path | None = None,
    lkg_sha_file: Path | None = None,
) -> tuple[str | None, str | None]:
    """Return ``(iso_timestamp, short_version)`` for the Status hero "Last
    update" row (#330 + #334).

    Order:
      1. /run/litclock/update.status with state=complete → ``finished_at_unix``
         + ``to_version`` (already a short SHA). The freshest signal,
         written by update.sh's update_status_complete().
      2. /var/lib/litclock/last-update.json — persistent mirror of (1)
         written by update.sh after update_status_complete validates
         (#334). Survives the tmpfs clear of (1) at reboot during the
         15-min LKG soak window AND the offline-graceful-exit window
         where Phase 1 already cleared lkg-sha but no new LKG was
         recorded yet.
      3. /var/lib/litclock/lkg-sha mtime + first 7 chars of contents.
         Persistent across reboots; covers pre-#334 Pis whose first
         post-upgrade reboot happens before any update.sh has written
         last-update.json.
      4. Neither file usable → ``(None, None)``. Caller renders em-dash.

    Previously this read ``systemctl show -p ActiveEnterTimestamp
    litclock-update.service`` — but PWA-triggered "Apply update" doesn't
    always leave a queryable ActiveEnterTimestamp on the unit (oneshot +
    RemainAfterExit semantics + service restart-in-place all interfere),
    so the row showed em-dash right after a successful update. The
    on-disk files are the durable source of truth (#330).

    All three reads go through the bounded helpers in update_state
    (#336): ``safe_read_json`` for sources 1 + 2 (8KB cap), ``safe_read_text``
    for source 3 (64-byte cap). lstat-based gates reject symlinks /
    FIFOs / directories so a planted bad file can't OOM or hang the
    request handler."""
    status_path = update_status_file or Path(DEFAULT_UPDATE_STATUS_FILE)
    resolved = _payload_to_last_update(safe_read_json(status_path, MAX_STATUS_FILE_BYTES))
    if resolved is not None:
        return resolved

    last_update_path = last_update_file or Path(DEFAULT_LAST_UPDATE_FILE)
    resolved = _payload_to_last_update(safe_read_json(last_update_path, MAX_LAST_UPDATE_FILE_BYTES))
    if resolved is not None:
        return resolved

    lkg_path = lkg_sha_file or Path(DEFAULT_LKG_SHA_FILE)
    sha_text = safe_read_text(lkg_path, MAX_LKG_SHA_FILE_BYTES)
    if sha_text is None:
        return None, None
    try:
        st = os.lstat(lkg_path)
    except OSError:
        return None, None
    try:
        iso = datetime.fromtimestamp(st.st_mtime, tz=UTC).isoformat()
    except (OSError, OverflowError, ValueError):
        return None, None
    sha = sha_text.strip()
    version = sha[:SHORT_SHA_LEN] if sha else None
    return iso, version


def _service_uptime_s() -> int:
    """Time since this control_server process started. Used for the
    /api/health uptime field (M1 reconnect probe — version-mismatch +
    uptime-reset together signal "restarted while you weren't looking")."""
    return int(_time.monotonic() - _SERVICE_START_MONOTONIC)


def _appliance_uptime_s() -> int:
    """System uptime via /proc/uptime — what the user reads as "Uptime"
    on the Status tab. Adversarial /review on M2 caught the M2 draft
    that surfaced control_server's own uptime here, which jumped to 3m
    after every update.sh restart while the appliance had been ticking
    for days. The user-facing "Uptime" row should reflect appliance
    health, not whichever Flask process happens to back the page.

    Falls back to control_server uptime when /proc/uptime is missing
    (macOS dev box, certain test setups). The fallback's still wrong-
    semantic for the user, but at least it's monotonic."""
    try:
        with open("/proc/uptime") as f:
            return int(float(f.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return _service_uptime_s()


# _format_uptime lives in control_server/_format.py since #419 (M2). The
# module-level re-export here keeps the status-internal call site one-name
# and lets any test that patched ``routes.status._format_uptime`` keep
# working without rewriting (grep across master found no such patches today,
# but the alias is a one-liner and the cost is nil).
from .._format import format_uptime as _format_uptime  # noqa: E402


def _format_relative(ts_iso: str | None, now_epoch: float) -> str:
    """Render an ISO-8601 timestamp as "2 days ago" / "3 hours ago".
    Fallback labels for empty / future / unparseable inputs follow
    DESIGN.md "Empty / loading / error" conventions."""
    if not ts_iso:
        return "—"
    try:
        dt = datetime.fromisoformat(ts_iso)
    except ValueError:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta_s = now_epoch - dt.timestamp()
    if delta_s < 0:
        return "just now"
    if delta_s < 60:
        return "just now"
    if delta_s < 3600:
        m = int(delta_s // 60)
        return f"{m} minute{'s' if m != 1 else ''} ago"
    if delta_s < 86400:
        h = int(delta_s // 3600)
        return f"{h} hour{'s' if h != 1 else ''} ago"
    days = int(delta_s // 86400)
    return f"{days} day{'s' if days != 1 else ''} ago"


def collect_status(
    status_file: Path | None = None,
    version_override: str | None = None,
    env_file: str | None = None,
    update_status_file: Path | None = None,
    last_update_file: Path | None = None,
    lkg_sha_file: Path | None = None,
    phase3_skipped_file: Path | None = None,
) -> dict:
    """Build the status payload — used by both `/api/status` (jsonified)
    and the `/` Status-tab template render (server-side first paint per
    PRD §7.5 progressive-enhancement requirement). Pulling this out of
    the route keeps the two consumers in lockstep with one source of truth.

    `env_file` plumbed through so app.config overrides win over process
    env (adversarial /review on M2 caught the bypass).

    `update_status_file` + `last_update_file` + `lkg_sha_file` plumbed
    through so tests can point the "Last update" row resolver at tmp
    paths (#330 + #334).

    `phase3_skipped_file` plumbed through for the same reason — the
    Status hero Phase-3-skip banner (#274 follow-up #5) needs a tmp
    path in tests."""
    status_path = status_file or Path(DEFAULT_STATUS_FILE)
    quote_payload = _read_status_file(status_path)

    now_epoch = _time.time()
    picked_at = (quote_payload or {}).get("picked_at")
    picked_at_age_s: float | None
    if isinstance(picked_at, (int, float)):
        picked_at_age_s = max(0.0, now_epoch - float(picked_at))
    else:
        picked_at_age_s = None

    # Stale signal: file missing OR picked_at older than threshold. PWA
    # uses this to render the ochre warning banner (DESIGN.md D2).
    stale = quote_payload is None or (picked_at_age_s is not None and picked_at_age_s >= STALE_THRESHOLD_S)

    appliance_uptime_s = _appliance_uptime_s()
    service_uptime_s = _service_uptime_s()
    last_update_at, last_update_version = _resolve_last_update(
        update_status_file=update_status_file,
        last_update_file=last_update_file,
        lkg_sha_file=lkg_sha_file,
    )
    # #274 follow-up #5: Phase 3 skip marker for the Status hero banner.
    phase3_skipped_at_unix = _resolve_phase3_skipped_at(phase3_skipped_file=phase3_skipped_file)
    # #274 follow-up #2: in-flight update state for the Settings banner.
    # Both fields read the same /run/litclock/update.status file — one
    # read, two consumers. None when no update.status file is present
    # (the common steady-state case).
    update_state, update_phase = _resolve_update_progress(update_status_file=update_status_file)
    return {
        "ok": True,
        "stale": stale,
        "quote": (quote_payload or {}).get("quote", ""),
        "author": (quote_payload or {}).get("author", ""),
        "title": (quote_payload or {}).get("title", ""),
        "time": (quote_payload or {}).get("time", ""),
        "picked_at": picked_at,
        "picked_at_age_s": picked_at_age_s,
        "version": get_version(version_override),
        # `uptime_s` / `uptime_human` are appliance uptime (read from
        # /proc/uptime) — that's what the Status row labelled "Uptime"
        # should display. service_uptime_s is the control_server's own
        # process uptime, exposed separately for the M4 reconnect-probe
        # version-mismatch detector.
        "uptime_s": appliance_uptime_s,
        "uptime_human": _format_uptime(appliance_uptime_s),
        "service_uptime_s": service_uptime_s,
        "wifi_ssid": _wifi_ssid(),
        "weather_city": _weather_city(env_file),
        "last_update_at": last_update_at,
        "last_update_at_relative": _format_relative(last_update_at, now_epoch),
        "last_update_version": last_update_version,
        # #274 follow-up #5: epoch seconds (float) when the marker is
        # fresh, else null. PWA Status-hero shows a banner when this
        # is non-null. Self-clears after PHASE3_SKIP_FRESH_WINDOW_S.
        "phase3_skipped_at_unix": phase3_skipped_at_unix,
        # #274 follow-up #2: in-flight update progress so the Settings
        # tab can show "Update in progress — Save may briefly block"
        # while update.sh holds the env.sh flock during Phase 3/4.
        # Both null in the common no-update-running state.
        "update_state": update_state,
        "update_phase_index": update_phase,
    }


@bp.route("/api/status")
def status() -> tuple[object, int]:
    status_file_cfg = current_app.config.get("STATUS_FILE")
    status_path = Path(status_file_cfg) if status_file_cfg else None
    update_status_cfg = current_app.config.get("UPDATE_STATUS_FILE")
    update_status_path = Path(update_status_cfg) if update_status_cfg else None
    last_update_cfg = current_app.config.get("LAST_UPDATE_FILE")
    last_update_path = Path(last_update_cfg) if last_update_cfg else None
    lkg_sha_cfg = current_app.config.get("LKG_SHA_FILE")
    lkg_sha_path = Path(lkg_sha_cfg) if lkg_sha_cfg else None
    phase3_skipped_cfg = current_app.config.get("PHASE3_SKIPPED_FILE")
    phase3_skipped_path = Path(phase3_skipped_cfg) if phase3_skipped_cfg else None
    body = collect_status(
        status_file=status_path,
        version_override=current_app.config.get("VERSION_OVERRIDE"),
        env_file=current_app.config.get("ENV_FILE"),
        update_status_file=update_status_path,
        last_update_file=last_update_path,
        lkg_sha_file=lkg_sha_path,
        phase3_skipped_file=phase3_skipped_path,
    )
    return jsonify(body), 200
