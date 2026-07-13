"""Shared network helpers for control_server routes.

Extracted from ``routes/status.py`` and ``routes/diagnostics.py`` per #419
follow-up M3 — both modules were running the same ``nmcli``/``ip``/``iw``
shell-outs, with subtly diverged return-type contracts (status used ``""``
for missing, diagnostics used ``None``). Canonical surface now returns
``str | None`` / ``int | None``; the status alias maps ``None`` back to
``""`` at the render boundary so the Jinja template's ``{{ ssid or "—" }}``
pattern keeps working without per-call site changes (D5).

Each reader takes explicit ``cache_key``, ``ttl``, and ``timeout`` params
(per #419 F6) so status (short steady-state, 5 s, 2 s timeout) and
diagnostics (longer poll cadence, 20 s, 3 s timeout) can use the underlying
:func:`control_server._subprocess.cached_subprocess` WITHOUT poisoning each
other's cache entries — a 2 s-timeout failure cached for 5 s won't block
a diagnostics caller that would have waited 3 s. The diagnostics-side keys
keep their pre-#419 ``diag-`` prefix.

#428 PR1a (CQ-1): the readers below go through
:func:`cached_subprocess_or_empty` so they can keep treating
"subprocess failed" as "binary produced no stdout" without each call site
writing ``or ""``. The classifier callers (anomaly logic in PR1b) will
use raw :func:`cached_subprocess` to branch on the ``None`` distinction.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

# Backwards-compat shim: tests monkeypatch ``_network.cached_subprocess``
# (e.g. tests/test_control_server_diagnostics_readers.py). Keep the name
# bound in this module's namespace so existing monkeypatches still hit
# something, even though the readers below now go through
# :func:`cached_subprocess_or_empty` (#428 PR1a CQ-1 — helper at the
# boundary, contract-loud at call site).
from ._subprocess import (
    cached_subprocess,  # noqa: F401
    cached_subprocess_or_empty,
)

# Default LAN IP file — written by nm-dispatcher 99-litclock-ip-change on
# IP change (which fires at DHCP renew + lease-change). Same value the
# e-ink QR encodes, so the diagnostics row matches what the owner sees.
# Callers may override via the ``path`` argument.
DEFAULT_LAST_RENDERED_IP_PATH = "/run/litclock/last-rendered-ip"

# Status's pre-extraction defaults. Diagnostics callers pass DIAG_*
# constants from routes.diagnostics._collectors.
STATUS_SUBPROC_TTL_S = 5.0
STATUS_SUBPROC_TIMEOUT_S = 2.0


def read_ssid(
    *,
    cache_key: str = "wifi-ssid",
    ttl: float = STATUS_SUBPROC_TTL_S,
    timeout: float = STATUS_SUBPROC_TIMEOUT_S,
) -> str | None:
    """Active WiFi SSID via nmcli. Returns None when not connected.

    Callers that want the legacy ``""``-on-missing surface (status.py's
    pre-#419 contract) wrap as ``read_ssid() or ""``. Diagnostics keeps
    None as a true sentinel so the anomaly detector can distinguish
    "WiFi down" from "SSID is the empty string."
    """
    raw = cached_subprocess_or_empty(
        cache_key,
        ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show", "--active"],
        timeout=timeout,
        ttl=ttl,
    )
    for line in raw.splitlines():
        # nmcli -t output: NAME:TYPE — find the first wifi connection.
        if ":" in line:
            name, typ = line.split(":", 1)
            if "wireless" in typ or typ == "wifi":
                return name
    return None


def read_default_route(
    *,
    cache_key: str = "default-route",
    ttl: float = STATUS_SUBPROC_TTL_S,
    timeout: float = STATUS_SUBPROC_TIMEOUT_S,
) -> tuple[str | None, str | None]:
    """Run ``ip -4 route show default`` once and parse both iface + gateway.

    Replaces the pre-#419 pattern in diagnostics.py that paired
    ``_read_iface`` + ``_read_gateway``, each forking the SAME command
    under different cache keys. One key here serves both readers.
    """
    raw = cached_subprocess_or_empty(
        cache_key,
        ["ip", "-4", "route", "show", "default"],
        timeout=timeout,
        ttl=ttl,
    )
    iface: str | None = None
    gateway: str | None = None
    for line in raw.splitlines():
        parts = line.split()
        if iface is None and "dev" in parts:
            dev_idx = parts.index("dev")
            if dev_idx + 1 < len(parts):
                iface = parts[dev_idx + 1]
        if gateway is None and "via" in parts:
            via_idx = parts.index("via")
            if via_idx + 1 < len(parts):
                gateway = parts[via_idx + 1]
        if iface is not None and gateway is not None:
            break
    return iface, gateway


def read_iface(
    *,
    cache_key: str = "default-route",
    ttl: float = STATUS_SUBPROC_TTL_S,
    timeout: float = STATUS_SUBPROC_TIMEOUT_S,
) -> str | None:
    """Default-route egress interface (typically ``wlan0`` on a Pi)."""
    return read_default_route(cache_key=cache_key, ttl=ttl, timeout=timeout)[0]


def read_gateway(
    *,
    cache_key: str = "default-route",
    ttl: float = STATUS_SUBPROC_TTL_S,
    timeout: float = STATUS_SUBPROC_TIMEOUT_S,
) -> str | None:
    """Default-route gateway IP."""
    return read_default_route(cache_key=cache_key, ttl=ttl, timeout=timeout)[1]


def read_signal_dbm(
    iface: str | None = None,
    *,
    cache_key_prefix: str = "iw-signal-",
    ttl: float = STATUS_SUBPROC_TTL_S,
    timeout: float = STATUS_SUBPROC_TIMEOUT_S,
) -> int | None:
    """Wireless signal strength in dBm via ``iw dev <iface> link``.

    Returns None when ``iw`` isn't installed or the iface has no signal
    line (e.g. Ethernet). When ``iface`` is None, falls back to ``wlan0``.
    The cache key is composed as ``cache_key_prefix + iface`` so per-iface
    entries stay distinct.
    """
    iface = iface or "wlan0"
    raw = cached_subprocess_or_empty(
        f"{cache_key_prefix}{iface}",
        ["iw", "dev", iface, "link"],
        timeout=timeout,
        ttl=ttl,
    )
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("signal:"):
            try:
                value = line.split(":", 1)[1].strip().split()[0]
                return int(value)
            except (ValueError, IndexError):
                return None
    return None


def read_lan_ip(path: str | None = None) -> str | None:
    """LAN IP last rendered by nm-dispatcher.

    Reads ``/run/litclock/last-rendered-ip`` (override via ``path``). The
    dispatcher writes this on actual IP change only — so freshness reflects
    DHCP-renew + lease-change cadence, not request cadence. Same value the
    e-ink QR encodes.

    Distinguishes ``None`` from ``""``: passing the empty string is treated
    as an intentional "disable" — the read attempts ``Path("")`` which
    raises ``OSError`` and degrades to ``None`` (matches the pre-#419
    monolith's behavior where an empty Flask config value would similarly
    degrade). Without this distinction, a staging override of
    ``DIAG_LAST_IP_PATH=""`` would silently fall back to the production
    path and leak the real LAN IP.
    """
    target = DEFAULT_LAST_RENDERED_IP_PATH if path is None else path
    try:
        return Path(target).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def read_last_dhcp_iso(path: str | None = None) -> str | None:
    """ISO-8601 timestamp of the most recent DHCP-relevant event.

    Approximates "last DHCP renew" via the mtime of the last-rendered-ip
    marker (which nm-dispatcher rewrites only on IP change). Cheap, bounded,
    and matches the same source as :func:`read_lan_ip` so a "stale LAN IP"
    anomaly and a "stale DHCP" anomaly track the same underlying signal.

    Same None-vs-empty-string distinction as :func:`read_lan_ip`: an empty
    ``path`` is "disable", not "fall back to default."
    """
    target = DEFAULT_LAST_RENDERED_IP_PATH if path is None else path
    try:
        st = os.lstat(target)
    except OSError:
        return None
    try:
        return datetime.fromtimestamp(st.st_mtime, tz=UTC).isoformat()
    except (OSError, OverflowError, ValueError):
        return None
