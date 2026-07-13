"""Per-row readers, subprocess cache, and the :func:`collect_diagnostics`
builder for the /api/diagnostics surface.

Split out of the pre-#419 monolithic ``routes/diagnostics.py`` (M1). Three
flavors of reader live here:

- ``_read_text_once`` lazy cache for one-shot reads (kernel, os-release) —
  populated on first request, cached for process lifetime.
- Direct file readers (``_read_appliance_uptime_s``, ``_read_cpu_temp_c``,
  ``_read_lan_ip`` via :mod:`control_server._network`, ``_read_current_quote``,
  setup marker presence checks).
- Subprocess-backed readers via :func:`control_server._subprocess.cached_subprocess_or_empty`
  (the CQ-1 helper added in #428 PR1a — coerces ``None`` failure to ``""``
  so ``.splitlines()`` / ``.strip()`` idioms keep working). A longer 20 s
  TTL than status's 5 s default avoids paying a cold-cache fork on every
  30 s PWA poll. Classifier callers (PR1b anomaly logic) will use raw
  :func:`cached_subprocess` to branch on the ``None`` distinction.

The :func:`collect_diagnostics` builder composes all of these into the
schema-conformant dict that :func:`api_diagnostics` returns. Per the
diagnostics docstring: a new field added here without a corresponding
:data:`control_server._diagnostics_privacy.PRIVACY_POLICY` entry is a
build-time failure (caught by the schema CI keystone).

Config-precedence note (M4): every reader that touches an external path
consults ``current_app.config[<KEY>]`` first, falling back to the
module-level ``DEFAULT_*`` constants. Tests override via config; production
uses the defaults. The Flask-config layer is consulted FIRST; env vars
provide the build-time defaults.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from flask import current_app

from ... import handoff
from ..._diagnostics_privacy import PRIVACY_POLICY, REDACTED_VALUE, redact, schema_keys
from ..._env import read_env_settings
from ..._format import format_uptime
from ..._network import (
    read_default_route as _network_read_default_route,
)
from ..._network import (
    read_lan_ip as _network_read_lan_ip,
)
from ..._network import (
    read_last_dhcp_iso as _network_read_last_dhcp_iso,
)
from ..._network import (
    read_signal_dbm as _network_read_signal_dbm,
)
from ..._network import (
    read_ssid as _network_read_ssid,
)
from ..._redaction import redact_text

# #428 PR1a (CQ-1): readers below go through ``cached_subprocess_or_empty``
# so they keep their ``.splitlines()`` / ``.strip()`` idioms without each
# call site writing ``or ""``. The raw ``cached_subprocess`` import stays
# bound so tests that monkeypatch ``_collectors.cached_subprocess`` keep
# working (e.g. test_control_server_diagnostics_readers.py:68 — Python
# binds names in each module namespace at import time).
from ..._subprocess import (
    cached_subprocess,
    cached_subprocess_or_empty,
)
from ...version import get_version

# Re-exported so tests that monkeypatch via the package namespace continue
# to work. The actual binding sites tests should patch are documented in
# ``__init__.py``'s ``__all__``.
__all__ = [
    "DIAG_JOURNAL_LINES_PER_UNIT",
    "DEFAULT_COLLECTED_MARKER_PATH",
    "DIAG_ONESHOT_NONANOMALY_STATES",
    "DIAG_ONESHOT_UNITS",
    "DIAG_JOURNAL_TIMEOUT_S",
    "DIAG_JOURNAL_TTL_S",
    "DIAG_SUBPROC_TIMEOUT_S",
    "DIAG_SUBPROC_TTL_S",
    "DIAG_UNITS",
    "PRIVACY_POLICY",
    "REDACTED_VALUE",
    "SECTION_IDS",
    "_batched_is_active",
    "_batched_journal_tails",
    "_coerce_float",
    "_is_obviously_healthy",
    "_is_oneshot_nonanomaly",
    "_lazy_cache",
    "_lazy_cache_lock",
    "_read_app_version",
    "_read_appliance_uptime_s",
    "_read_cpu_temp_c",
    "_read_current_quote",
    "_read_default_route",
    "_read_disk_free_pct",
    "_read_gateway",
    "_read_git_head",
    "_read_iface",
    "_read_images_version",
    "_read_journal_tail",
    "_read_kernel_release",
    "_read_lan_ip",
    "_read_last_dhcp_iso",
    "_read_last_update",
    "_read_memory_free_mb",
    "_read_os_release_pretty",
    "_read_recent_log_entries",
    "_read_signal_dbm",
    "_read_ssid",
    "_read_text_once",
    "_read_timezone",
    "_setup_marker_present",
    "_build_service_states",
    "cached_subprocess",
    "cached_subprocess_or_empty",
    "collect_diagnostics",
    "format_uptime",
    "redact",
    "redact_text",
    "schema_keys",
]


# --- Configuration / constants ----------------------------------------------

# Diagnostics-only TTL on the shared subprocess cache. Longer than status's
# 5 s so the 30 s poll cadence on the page doesn't pay a cold-cache
# subprocess fork every refresh. See /plan-eng-review C2=A.
DIAG_SUBPROC_TTL_S = 20.0

# Per-call subprocess timeouts. Fast tools (uname / nmcli / ip / iw /
# systemctl is-active / timedatectl / git rev-parse) all finish in
# under a second on a Pi Zero 2W; 3 s is a generous-enough budget
# without making degraded states (e.g. nmcli hanging on a wedged WiFi)
# block a page render too long.
DIAG_SUBPROC_TIMEOUT_S = 3.0  # shared "fast" base; the per-call seeds below

# Per-call "fast" budgets (#430). Historically every fast diagnostics call
# shared the single DIAG_SUBPROC_TIMEOUT_S above. That coupling is a trap: the
# journalctl story (#427) showed a "fast" call can blow 3 s under SD-IO/CPU
# contention, but bumping the SHARED budget to cover the slow one would also
# loosen the cheap kernel calls that don't need it (and a too-loose budget lets
# a wedged call stall a render; a too-tight one false-positives an anomaly on a
# healthy clock — the #430 bug). Each fast call now reads its OWN constant so
# the budgets tune independently.
#
# HONESTY NOTE: these are SEEDED at the shared base — behaviour-preserving
# today, NOT yet tuned. Real per-call values need observed worst-case latency
# from Pi Zero 2W hardware (authorclock + test Pi) under idle / paint
# contention / memory pressure / degraded SD / wedged WiFi. Run
# ``scripts/diag-subprocess-timing.py`` on the Pi and size each budget at the
# worst-case p99 + headroom (the journalctl precedent used ~1.5x). Following
# #444's call, we ship the per-call STRUCTURE + invariant tests now and tune
# from data, never from guessed p99s. The risk-class on each line is the
# hypothesis the measurement confirms or refutes; the seed is pinned by
# ``TestFastCallBudgets.test_seeded_at_shared_base_until_measured`` so any tune
# is a deliberate, data-cited edit. See #430.
#
# These per-call constants are intentionally module-private (NOT added to the
# diagnostics package __all__, unlike DIAG_SUBPROC_TIMEOUT_S / DIAG_JOURNAL_
# TIMEOUT_S which predate them). Nothing outside this module needs them; the
# budget tests reach them via ``_collectors.DIAG_*`` like the perf suite already
# does for the journal constant. Re-export only if a cross-package consumer
# appears.
#
# D-Bus / network-stack clients — "fast" is not a pure local-kernel guarantee
# (systemd/NetworkManager can stall under contention). nmcli is the known hang
# risk on a wedged or dead WiFi, so it is the highest-risk of the group.
DIAG_NMCLI_TIMEOUT_S = DIAG_SUBPROC_TIMEOUT_S  # nmcli connection show (SSID)
DIAG_IW_LINK_TIMEOUT_S = DIAG_SUBPROC_TIMEOUT_S  # iw dev <iface> link (slow on weak signal)
DIAG_SYSTEMCTL_TIMEOUT_S = DIAG_SUBPROC_TIMEOUT_S  # systemctl is-active (D-Bus)
DIAG_TIMEDATECTL_TIMEOUT_S = DIAG_SUBPROC_TIMEOUT_S  # timedatectl show Timezone (D-Bus)
# IO-bound — slow only on a degraded SD card.
DIAG_GIT_HEAD_TIMEOUT_S = DIAG_SUBPROC_TIMEOUT_S  # git rev-parse --short HEAD
# Pure-kernel, cheap — own constants so NO fast reader is left on the shared
# base (keeps the "every call site reads its own budget" invariant total, and
# lets measurement TIGHTEN these if the data supports it).
DIAG_IP_ROUTE_TIMEOUT_S = DIAG_SUBPROC_TIMEOUT_S  # ip -4 route show default
DIAG_UNAME_TIMEOUT_S = DIAG_SUBPROC_TIMEOUT_S  # uname -r

# Journalctl is the outlier. v0.214.2 hardware QA on authorclock clocked a
# single ``journalctl --no-pager -n 3 -u <unit>`` query at 3.95 s on a Pi
# Zero 2W with a few weeks of journal storage; under SD-card IO contention
# the prior 4-worker pool could push p99 past 15 s (#433). 8 s per call gives
# headroom for the worst observed (~4 s) plus transient CPU contention from
# concurrent paint cycles. PR1b's failure-TTL cap (5 s) means a journalctl
# that times out gets cached as ``None`` for 5 s, so the next caller re-runs
# within recovery cadence rather than pinning the failure for 20 s (#428).
#
# Total budget on the route: serial calls (#433 A-2) cap at
# ``len(units_needing_tails) * DIAG_JOURNAL_TIMEOUT_S``. Lazy-tail (#433 P-1)
# in ``_build_service_states`` keeps ``units_needing_tails`` empty on a
# healthy clock and bounded at the number of failed/inactive-non-oneshot
# units otherwise. Worst case is bounded by the count of non-healthy units
# times this timeout: e.g., 4 simultaneously-unhealthy units would cap at
# ~32 s (litclock-control.service must be active when /api/diagnostics is
# being served, so it's never one of the unhealthy ones). Per /review
# C-1: the serial budget IS strictly worse than the pre-#433 4-worker
# parallel design's ~16 s ceiling under IO contention. We accept the
# regression because the steady-state production case has 0 or 1
# unhealthy units (almost always 0); the 32 s tail is a multi-failure
# anomaly where the JS client aborts at its 10 s budget and the user
# sees "couldn't refresh" — strictly better UX than the v0.214.x oxblood
# false-positive that #433 was opened to close.
DIAG_JOURNAL_TIMEOUT_S = 8.0

# Journal tails get their OWN cache TTL, decoupled from the shared 20 s
# DIAG_SUBPROC_TTL_S (#436). Reason: since #436 the tails are no longer fetched
# on the SSR/poll critical path — they hydrate per-unit via
# ``GET /api/diagnostics/journal`` after first paint. The PWA re-hydrates a
# still-unhealthy unit on every 30 s poll (``POLL_INTERVAL_MS`` in
# diagnostics.js). A 20 s TTL expires between polls, so a stuck-failed unit
# would re-fork the ~5-7 s cold journalctl EVERY poll. 45 s > 30 s means
# consecutive polls reuse the cached tail instead of re-forking. Safe to raise
# past the timeout because the born-stale write-timestamp fix (#438) stamps the
# cache entry AFTER subprocess.run returns.
DIAG_JOURNAL_TTL_S = 45.0

# The set of systemd units the helper-paste block cares about. Order is the
# render order. ``litclock.service`` first because that's the user-visible
# "is the clock running" check.
DIAG_UNITS: tuple[str, ...] = (
    "litclock.service",
    "litclock-control.service",
    "litclock-firstboot.service",
    "litclock-update.timer",
    "litclock-reresolve-location.service",
)

# Units that are systemd Type=oneshot — they paint/work once and settle
# into ``inactive (dead)`` by design. The anomaly detector MUST NOT flag
# these as "services" anomalies just because they're inactive.
DIAG_ONESHOT_UNITS: frozenset[str] = frozenset(
    {
        "litclock.service",
        "litclock-firstboot.service",
        "litclock-reresolve-location.service",
    }
)

# systemd states that are NOT a services anomaly for a DIAG_ONESHOT_UNITS
# member: the settled post-paint ``inactive`` AND the transient
# ``activating``/``deactivating`` window a oneshot cycles through every
# minute during the quote paint (#443). SINGLE source of truth shared by
# ``_compute_anomalies`` (anomaly verdict) and ``_is_obviously_healthy``
# (lazy-tail journal-fetch decision, #433) so the two can't drift — a unit
# flagged anomalous but denied its journal tail would lose the debug context
# #433's P-1 filter exists to preserve. ``failed`` is deliberately absent: a
# failed oneshot is a real failure that must trip + keep its tail.
DIAG_ONESHOT_NONANOMALY_STATES: frozenset[str] = frozenset({"inactive", "activating", "deactivating"})

# Last-N journal lines fetched per unit, displayed in the "Services" section.
DIAG_JOURNAL_LINES_PER_UNIT = 3
# Deeper tail for the on-demand support-logs export (#416 follow-up): enough
# context to actually debug a failing unit, vs the 3-line page preview.
DIAG_SUPPORT_JOURNAL_LINES = 50
# Hard cap on the journal endpoint's `?lines=` param so a client can't ask for
# an unbounded (slow, huge) journalctl read.
DIAG_JOURNAL_LINES_MAX = 200
# Wall-clock budget (seconds) for assembling the whole support-logs bundle. If
# the serial per-unit reads exceed it, we stop and append an explicit
# truncation note rather than let the request run away (or silently drop units).
DIAG_SUPPORT_LOGS_BUDGET_S = 20.0

# Sensor + sys-file probes. Caller may override via Flask config for tests.
DEFAULT_THERMAL_PATH = os.environ.get("LITCLOCK_DIAG_THERMAL_PATH", "")
DEFAULT_OS_RELEASE_PATH = os.environ.get("LITCLOCK_DIAG_OS_RELEASE_PATH", "/etc/os-release")
DEFAULT_PROC_UPTIME_PATH = os.environ.get("LITCLOCK_DIAG_PROC_UPTIME_PATH", "/proc/uptime")
DEFAULT_PROC_MEMINFO_PATH = os.environ.get("LITCLOCK_DIAG_PROC_MEMINFO_PATH", "/proc/meminfo")
DEFAULT_DISK_TARGET = os.environ.get("LITCLOCK_DIAG_DISK_TARGET", "/")
DEFAULT_LAST_RENDERED_IP_PATH = os.environ.get("LITCLOCK_DIAG_LAST_IP_PATH", "/run/litclock/last-rendered-ip")
# #445 — persistent "has this section ever been collected" marker. Replaces
# the reboot-wiped DEFAULT_LAST_RENDERED_IP_PATH check in _compute_uncollected
# (the read side falls back to that tmpfs path when this marker is absent, for
# one-release backward compat). Written by scripts/litclock-mark-collected.sh
# (network, dispatcher) + src/collected_marker.py (time-location, resolvers).
DEFAULT_COLLECTED_MARKER_PATH = os.environ.get(
    "LITCLOCK_DIAG_COLLECTED_MARKER", "/var/lib/litclock/.last-collected-marker.json"
)
DEFAULT_CURRENT_QUOTE_PATH = os.environ.get("LITCLOCK_DIAG_CURRENT_QUOTE_PATH", "/run/litclock/current-quote.json")
DEFAULT_IMAGES_VERSION_PATH = os.environ.get("LITCLOCK_DIAG_IMAGES_VERSION_PATH", "/home/pi/litclock/.images-version")
DEFAULT_GIFT_MODE_MARKER = os.environ.get("LITCLOCK_DIAG_GIFT_MARKER", "/etc/litclock/.gift-mode")

# The anomaly-aware section identifiers. Match the render order locked in
# /plan-design-review. The list defines BOTH the section ordering and the
# set valid for the "anomalies" array in the response — a future change to
# section IDs must update both ends in one diff.
SECTION_IDS: tuple[str, ...] = (
    "build-version",
    "system",
    "network",
    "time-location",
    "services",
    "last-quote",
    "setup-markers",
    "recent-log-entries",
)


# --- Lazy module-level caches -----------------------------------------------
# These are read once per process — kernel doesn't change without a reboot,
# os-release doesn't change without an OS upgrade.

_lazy_cache: dict[str, str] = {}
_lazy_cache_lock = threading.Lock()


def _read_text_once(key: str, reader: Callable[[], str | None]) -> str | None:
    """Return cached result if any, else compute via ``reader`` and cache it.

    The cache holds strings only (a missing file results in ``None`` not
    being inserted, so the reader is retried on the next request — which
    is the desired behavior when the file becomes available later).

    Concurrency: guarded by ``_lazy_cache_lock``. Two waitress threads racing
    on the first miss would otherwise both fork the same ``uname -r`` /
    ``cat /etc/os-release`` subprocess.
    """
    if key in _lazy_cache:
        return _lazy_cache[key]
    with _lazy_cache_lock:
        if key in _lazy_cache:
            return _lazy_cache[key]
        value = reader()
        if value is not None:
            _lazy_cache[key] = value
    return value


def _read_kernel_release() -> str | None:
    return (
        cached_subprocess_or_empty(
            "diag-uname-r",
            ["uname", "-r"],
            timeout=DIAG_UNAME_TIMEOUT_S,
            ttl=24 * 3600,  # effectively a process-lifetime cache
        )
        or None
    )


def _read_os_release_pretty() -> str | None:
    """Pull PRETTY_NAME from /etc/os-release. Returns the literal string
    (already unquoted by the parser) or None when the file is missing."""
    path = current_app.config.get("DIAG_OS_RELEASE_PATH", DEFAULT_OS_RELEASE_PATH)
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not line.startswith("PRETTY_NAME="):
                    continue
                value = line.split("=", 1)[1].strip()
                if len(value) >= 2 and value[0] in ('"', "'") and value[0] == value[-1]:
                    value = value[1:-1]
                return value
    except OSError:
        return None
    return None


# --- Per-row readers --------------------------------------------------------


def _read_app_version() -> str:
    return get_version(current_app.config.get("VERSION_OVERRIDE")) or ""


def _read_git_head() -> str | None:
    return (
        cached_subprocess_or_empty(
            "diag-git-head",
            ["git", "rev-parse", "--short", "HEAD"],
            timeout=DIAG_GIT_HEAD_TIMEOUT_S,
            ttl=DIAG_SUBPROC_TTL_S,
        )
        or None
    )


def _read_images_version() -> str | None:
    path = current_app.config.get("DIAG_IMAGES_VERSION_PATH", DEFAULT_IMAGES_VERSION_PATH)
    try:
        return Path(path).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _read_last_update(env_settings: dict[str, str]) -> tuple[str | None, str | None]:
    # Canonical resolver lives in routes.status._resolve_last_update.
    # Re-use it via lazy import so diagnostics + status stay in lockstep.
    try:
        from ..status import _resolve_last_update  # noqa: PLC0415
    except ImportError:
        return None, None
    return _resolve_last_update()


def _read_appliance_uptime_s() -> int | None:
    path = current_app.config.get("DIAG_PROC_UPTIME_PATH", DEFAULT_PROC_UPTIME_PATH)
    try:
        with open(path) as f:
            return int(float(f.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def _read_cpu_temp_c() -> float | None:
    """Read CPU temperature from /sys/class/thermal/thermal_zone*/temp."""
    candidates: list[str] = []
    configured = current_app.config.get("DIAG_THERMAL_PATH", DEFAULT_THERMAL_PATH)
    if configured:
        candidates.append(configured)
    candidates.extend(
        [
            "/sys/class/thermal/thermal_zone0/temp",
            "/sys/class/thermal/thermal_zone1/temp",
        ]
    )
    for path in candidates:
        try:
            raw = Path(path).read_text().strip()
            milli = int(raw)
        except (OSError, ValueError):
            continue
        return milli / 1000.0
    return None


def _read_memory_free_mb() -> int | None:
    path = current_app.config.get("DIAG_PROC_MEMINFO_PATH", DEFAULT_PROC_MEMINFO_PATH)
    try:
        with open(path) as f:
            for line in f:
                if not line.startswith("MemAvailable:"):
                    continue
                kb = int(line.split()[1])
                return kb // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _read_disk_free_pct() -> float | None:
    target = current_app.config.get("DIAG_DISK_TARGET", DEFAULT_DISK_TARGET)
    try:
        usage = shutil.disk_usage(target)
    except OSError:
        return None
    if usage.total <= 0:
        return None
    return round(usage.free / usage.total * 100, 1)


# Network ------------------------------------------------------------------
# Each reader is a thin wrapper over control_server._network with a
# diag-prefixed cache key + DIAG_SUBPROC_{TTL,TIMEOUT}_S so the diagnostics
# cache window stays distinct from status's 5 s window. The wrapper layer
# matters because tests monkeypatch _read_ssid/_read_lan_ip/etc. on this
# module (D8 — patch where it's looked up).


def _read_default_route() -> tuple[str | None, str | None]:
    """Run ``ip -4 route show default`` once and parse both iface + gateway.

    Pre-#419 the iface + gateway readers BOTH went through this helper so a
    monkey-patch of ``_read_default_route`` reliably reshaped both readers
    in lock-step. The package split now delegates to
    :func:`control_server._network.read_default_route` for the parse, but
    :func:`_read_iface` and :func:`_read_gateway` continue to flow through
    THIS function (not directly to ``_network``) so the seam stays where
    every existing patch site expects it. Codex /review F4 caught the
    earlier shape that bypassed the seam.
    """
    return _network_read_default_route(
        cache_key="diag-default-route",
        ttl=DIAG_SUBPROC_TTL_S,
        timeout=DIAG_IP_ROUTE_TIMEOUT_S,
    )


def _read_iface() -> str | None:
    return _read_default_route()[0]


def _read_ssid() -> str | None:
    return _network_read_ssid(
        cache_key="diag-wifi-ssid",
        ttl=DIAG_SUBPROC_TTL_S,
        timeout=DIAG_NMCLI_TIMEOUT_S,
    )


def _read_lan_ip() -> str | None:
    path = current_app.config.get("DIAG_LAST_IP_PATH", DEFAULT_LAST_RENDERED_IP_PATH)
    return _network_read_lan_ip(path=path)


def _read_gateway() -> str | None:
    return _read_default_route()[1]


def _read_signal_dbm() -> int | None:
    iface = _read_iface() or "wlan0"
    return _network_read_signal_dbm(
        iface=iface,
        cache_key_prefix="diag-iw-signal-",
        ttl=DIAG_SUBPROC_TTL_S,
        timeout=DIAG_IW_LINK_TIMEOUT_S,
    )


def _read_last_dhcp_iso() -> str | None:
    path = current_app.config.get("DIAG_LAST_IP_PATH", DEFAULT_LAST_RENDERED_IP_PATH)
    return _network_read_last_dhcp_iso(path=path)


# Time + location ----------------------------------------------------------


def _read_timezone() -> str | None:
    raw = cached_subprocess_or_empty(
        "diag-timezone",
        ["timedatectl", "show", "-p", "Timezone", "--value"],
        timeout=DIAG_TIMEDATECTL_TIMEOUT_S,
        ttl=DIAG_SUBPROC_TTL_S,
    )
    return raw or None


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# Services -----------------------------------------------------------------


def _batched_is_active(units: tuple[str, ...]) -> dict[str, str]:
    """Run ``systemctl is-active u1 u2 …`` once and parse the per-unit
    states. Per OV-7=A: collapsed from N forks to 1.
    """
    raw = cached_subprocess_or_empty(
        "diag-systemctl-is-active-" + "+".join(units),
        ["systemctl", "is-active", *units],
        timeout=DIAG_SYSTEMCTL_TIMEOUT_S,
        ttl=DIAG_SUBPROC_TTL_S,
    )
    lines = raw.splitlines() if raw else []
    out: dict[str, str] = {}
    for i, unit in enumerate(units):
        out[unit] = lines[i].strip() if i < len(lines) and lines[i].strip() else "unknown"
    return out


def _read_journal_tail(unit: str, n: int = DIAG_JOURNAL_LINES_PER_UNIT, cache_key: str | None = None) -> list[str]:
    # cache_key defaults to the shared per-unit key used by the 3-line page
    # preview. A DEEPER read (support-logs export, #416 follow-up) MUST pass a
    # distinct key: the cache is keyed on the label, NOT the line count, so a
    # deep read sharing the default key would serve the stale 3-line result (or
    # poison the page's cache with a 50-line blob).
    raw = cached_subprocess_or_empty(
        cache_key or f"diag-journal-{unit}",
        ["journalctl", "--no-pager", "-n", str(n), "-u", unit, "-o", "short-iso"],
        timeout=DIAG_JOURNAL_TIMEOUT_S,  # bumped from DIAG_SUBPROC_TIMEOUT_S in v0.214.2 (#427)
        ttl=DIAG_JOURNAL_TTL_S,  # decoupled + raised above the 30s poll interval (#436)
    )
    if not raw:
        return []
    return [line for line in raw.splitlines() if line and not line.startswith("--")]


def _batched_journal_tails(units: tuple[str, ...], n: int = DIAG_JOURNAL_LINES_PER_UNIT) -> dict[str, list[str]]:
    """Fetch the last N journal lines for each unit, serially.

    NOTE (#436): this has NO production caller anymore. Before #436,
    :func:`_build_service_states` invoked it (the P-1 lazy-tail path); since
    #436 tails hydrate per-unit off the render path via
    ``/api/diagnostics/journal`` (:func:`_read_journal_tail` directly), so
    ``_build_service_states`` no longer fetches tails at all. This wrapper is
    retained only as a thin serial batch helper exercised by
    ``test_control_server_perf.py`` (it pins the serial, one-call-per-unit,
    per-unit-failure-isolation contract) — kept over deletion to avoid churn
    across ``__all__`` / ``__init__.py`` re-exports and to preserve a batch
    helper a future consumer could reuse. The single-unit
    :func:`_read_journal_tail` is the live path.

    The serial loop (over a pre-#433 ``ThreadPoolExecutor``) was #433's fix for
    SD-IO saturation pushing 4 concurrent ``journalctl`` p99 past the 10 s
    client budget; per-unit isolation is now provided at the HTTP layer instead
    (one request per row).
    """
    if not units:
        return {}
    return {unit: _read_journal_tail(unit, n) for unit in units}


def _is_oneshot_nonanomaly(unit: str, state: str) -> bool:
    """True iff ``unit`` is a oneshot sitting in a benign lifecycle state.

    A ``DIAG_ONESHOT_UNITS`` member in any ``DIAG_ONESHOT_NONANOMALY_STATES``
    state — the settled post-paint ``inactive`` or the transient
    ``activating``/``deactivating`` window it cycles through every minute
    during the quote paint (#443). NOT ``failed``: a failed oneshot is a
    real failure.

    Single source of truth for the three places that must agree on the
    oneshot carve-out so they can't drift: the anomaly verdict
    (``_compute_anomalies``), the lazy-tail fetch decision
    (:func:`_is_obviously_healthy`), and the per-row chip tone
    (:func:`_build_service_states`, #449).
    """
    return unit in DIAG_ONESHOT_UNITS and state in DIAG_ONESHOT_NONANOMALY_STATES


def _is_obviously_healthy(state: str, unit: str) -> bool:
    """True iff a unit's systemctl state is "nothing to debug here".

    The P-1 lazy-tail filter (#433) in :func:`_build_service_states`
    fetches journal tails for every unit EXCEPT the ones this returns
    True for. Two healthy categories:

    1. ``active`` — the canonical happy state. Rendering an empty tail
       at the top of the services section is the at-a-glance "OK" view.
    2. A oneshot unit in a non-failure lifecycle state (settled
       ``inactive`` or mid-paint ``activating``/``deactivating``), so it
       needs no journal tail. See :func:`_is_oneshot_nonanomaly`.

    Every other state (notably ``failed`` for any unit, and
    ``activating``/``deactivating``/``inactive`` for a NON-oneshot) gets a
    tail because each can trigger the services anomaly downstream — and
    when the banner fires the user needs the log context to debug. Per
    /review P-1 (cross-specialist consensus from testing + perf).
    """
    if state == "active":
        return True
    return _is_oneshot_nonanomaly(unit, state)


def _row_state_modifier(unit: str, state: str) -> str:
    """CSS modifier suffix for the Services row chip COLOR.

    The chip TEXT always stays the literal systemd ``state``; only the tone
    follows this modifier. A ``DIAG_ONESHOT_UNITS`` member in a benign
    lifecycle state gets a tone that matches the OK section pill instead of
    its literal state's tint:

    * settled ``inactive`` (the by-design resting state) -> ``settled-ok``,
      the same botanical-green ``--success`` tone as ``active`` so a non-tech
      gift recipient reads "healthy" at a glance instead of misreading the
      calm idle row as a fault (#463). ``litclock-firstboot.service`` is the
      sharpest case: permanently ``inactive`` on a provisioned clock.
    * transient ``activating``/``deactivating`` (the per-minute paint window)
      -> ``transient-ok``, neutral graphite, so the row doesn't flash ochre
      while the section pill reads OK (#443 fixed the verdict, #449 the tone).

    Everything else — non-oneshot units, and any ``failed`` — keeps its
    literal modifier so the existing CSS still tints it (green ``--active``,
    ochre ``--failed``/``--activating``/``--deactivating``). Shares the
    :func:`_is_oneshot_nonanomaly` predicate so the row tone can't drift from
    the anomaly verdict.
    """
    if _is_oneshot_nonanomaly(unit, state):
        return "settled-ok" if state == "inactive" else "transient-ok"
    return state


def _build_service_states() -> dict[str, dict[str, Any]]:
    """Build the per-unit map of ``{state, state_modifier, healthy, journal_tail}``.

    Since #436 this NO LONGER fetches journal tails. ``journal_tail`` is always
    ``[]`` here; every row's tail hydrates per-unit via
    ``GET /api/diagnostics/journal?unit=<unit>`` AFTER first paint (see
    :func:`_sse.api_diagnostics_journal` + ``patchServicesSection`` in
    diagnostics.js). Rationale: a single cold ``journalctl`` costs ~5-7 s on a
    Pi Zero 2W (measured on authorclock), and it was on the synchronous SSR
    render path — so ``/diagnostics`` blocked first paint by that much whenever
    a unit was unhealthy, i.e. exactly when the user opened it to debug. Moving
    the fetch off the render path (AND off the poll path) removes ``journalctl``
    from every page/poll request; only the dedicated per-unit endpoint forks it.

    ``healthy`` is :func:`_is_obviously_healthy` for the unit — emitted so the
    template can stamp ``data-diag-healthy`` on each row and the client can gate
    hydration (and the boot fetch) on "does any row actually need a tail?"
    WITHOUT re-implementing the predicate in JS (which would drift on oneshot
    ``inactive`` / transient rows). It is the SAME predicate the pre-#436
    lazy-tail filter used to decide which units got a tail.

    The anomaly verdict is unaffected: :func:`_anomalies._compute_anomalies`
    reads ``state`` only, never ``journal_tail`` — so dropping tails here cannot
    change any section state (guarded by a regression test).
    """
    active = _batched_is_active(DIAG_UNITS)
    out: dict[str, dict[str, Any]] = {}
    for unit in DIAG_UNITS:
        state = active.get(unit, "unknown")
        out[unit] = {
            "state": state,
            # CSS modifier suffix for the row chip COLOR (the chip TEXT stays
            # the literal ``state``). See :func:`_row_state_modifier`: oneshot
            # mid-paint -> neutral ``transient-ok`` (#443/#449), oneshot at its
            # settled ``inactive`` resting state -> green ``settled-ok`` (#463).
            "state_modifier": _row_state_modifier(unit, state),
            # Server-computed so JS/template can gate hydration without drift
            # (#436). Tails hydrate per-unit for the NON-healthy rows only.
            "healthy": _is_obviously_healthy(state, unit),
            # Always empty here — hydrated client-side via the per-unit endpoint.
            "journal_tail": [],
        }
    return out


# Last quote ---------------------------------------------------------------


def _read_current_quote() -> dict[str, Any]:
    path = current_app.config.get("DIAG_CURRENT_QUOTE_PATH", DEFAULT_CURRENT_QUOTE_PATH)
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


# Setup markers ------------------------------------------------------------


def _setup_marker_present(app_key: str, default_path: str) -> bool:
    path = current_app.config.get(app_key, default_path)
    try:
        return os.path.exists(path)
    except OSError:
        return False


# Recent log entries -------------------------------------------------------


def _read_recent_log_entries(n: int = 4) -> list[dict[str, Any]]:
    """Snapshot of the last ``n`` entries from the in-memory buffer.

    Per /plan-design-review F28 — the on-page "Recent log entries" section
    is a STATIC snapshot taken at request time. The live drawer (via
    /api/logs/stream) is the streaming surface. They share the same buffer.
    """
    try:
        from ...log_buffer import get_memory_handler  # noqa: PLC0415
    except ImportError:
        return []
    handler = get_memory_handler()
    if handler is None:
        return []
    return [e.to_dict() for e in handler.get_logs(limit=n)]


# --- The main builder ------------------------------------------------------


def collect_diagnostics() -> dict[str, Any]:
    """Build the diagnostics value dict.

    The returned dict's keys MUST be EXACTLY ``schema_keys()`` — see the
    test_collect_diagnostics_payload_matches_schema CI keystone. A field
    added here without a PRIVACY_POLICY entry (or vice versa) is a
    build-time fail.
    """
    env_settings = read_env_settings(current_app.config.get("ENV_FILE"))
    last_update_at, last_update_version = _read_last_update(env_settings)
    quote_payload = _read_current_quote()
    uptime_s = _read_appliance_uptime_s()
    setup_complete = _setup_marker_present(
        "SETUP_COMPLETE_FILE",
        handoff.SETUP_COMPLETE_FILE_DEFAULT,
    )
    handoff_complete = _setup_marker_present(
        "HANDOFF_COMPLETE_FILE",
        handoff.HANDOFF_COMPLETE_FILE_DEFAULT,
    )
    gift_mode_active = _setup_marker_present(
        "DIAG_GIFT_MODE_MARKER",
        DEFAULT_GIFT_MODE_MARKER,
    )
    services = _build_service_states()
    recent_logs = _read_recent_log_entries()

    return {
        # Build & version
        "app_version": _read_app_version(),
        "git_head": _read_git_head(),
        "images_version": _read_images_version(),
        "last_update_at": last_update_at,
        "last_update_version": last_update_version,
        # System
        "kernel": _read_text_once("kernel", _read_kernel_release),
        "os_release": _read_text_once("os_release", _read_os_release_pretty),
        "uptime_s": uptime_s,
        "uptime_human": format_uptime(uptime_s) if uptime_s is not None else None,
        "cpu_temp_c": _read_cpu_temp_c(),
        "memory_free_mb": _read_memory_free_mb(),
        "disk_free_pct": _read_disk_free_pct(),
        # Network
        "iface": _read_iface(),
        "ssid": _read_ssid(),
        "lan_ip": _read_lan_ip(),
        "gateway": _read_gateway(),
        "signal_dbm": _read_signal_dbm(),
        "last_dhcp_at": _read_last_dhcp_iso(),
        # Time & location
        "timezone": _read_timezone(),
        "weather_location_name": env_settings.get("WEATHER_LOCATION_NAME") or None,
        "weather_lat": _coerce_float(env_settings.get("WEATHER_LATITUDE")),
        "weather_lon": _coerce_float(env_settings.get("WEATHER_LONGITUDE")),
        "weather_location_mode": env_settings.get("WEATHER_LOCATION_MODE") or None,
        "weather_ip_country": env_settings.get("WEATHER_IP_COUNTRY") or None,
        "weather_units": env_settings.get("WEATHER_UNITS") or None,
        "weather_enabled": (env_settings.get("WEATHER_ENABLED") or "").lower() in ("true", "1", "yes"),
        "last_ip_geo_at": env_settings.get("WEATHER_LAST_IP_GEO_AT") or None,
        # Services
        "service_states": services,
        # Last quote
        "quote": quote_payload.get("quote") or None,
        "author": quote_payload.get("author") or None,
        "title": quote_payload.get("title") or None,
        "time": quote_payload.get("time") or None,
        "picked_at": quote_payload.get("picked_at"),
        # Setup markers
        "setup_complete": setup_complete,
        "handoff_complete": handoff_complete,
        "gift_mode_active": gift_mode_active,
        # Allowlist flags
        "allow_nsfw_quotes": (env_settings.get("ALLOW_NSFW_QUOTES") or "").lower() in ("true", "1", "yes"),
        # Recent log entries (snapshot)
        "recent_log_entries": recent_logs,
    }
