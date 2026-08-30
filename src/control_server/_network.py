"""Shared network helpers for control_server routes.

Extracted from ``routes/status.py`` and ``routes/diagnostics.py`` per litclock-dev#419
follow-up M3 — both modules were running the same ``nmcli``/``ip``/``iw``
shell-outs, with subtly diverged return-type contracts (status used ``""``
for missing, diagnostics used ``None``). Canonical surface now returns
``str | None`` / ``int | None``; the status alias maps ``None`` back to
``""`` at the render boundary so the Jinja template's ``{{ ssid or "—" }}``
pattern keeps working without per-call site changes (D5).

Each reader takes explicit ``cache_key``, ``ttl``, and ``timeout`` params
(per litclock-dev#419 F6) so status (short steady-state, 5 s, 2 s timeout) and
diagnostics (longer poll cadence, 20 s, 3 s timeout) can use the underlying
:func:`control_server._subprocess.cached_subprocess` WITHOUT poisoning each
other's cache entries — a 2 s-timeout failure cached for 5 s won't block
a diagnostics caller that would have waited 3 s. The diagnostics-side keys
keep their pre-litclock-dev#419 ``diag-`` prefix.

litclock-dev#428 PR1a (CQ-1): the readers below go through
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
# :func:`cached_subprocess_or_empty` (litclock-dev#428 PR1a CQ-1 — helper at the
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
    pre-litclock-dev#419 contract) wrap as ``read_ssid() or ""``. Diagnostics keeps
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

    Replaces the pre-litclock-dev#419 pattern in diagnostics.py that paired
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


# Addresses that must never count as "this clock has a working LAN address",
# mirroring what nm-dispatcher 99-litclock-ip-change refuses to record. Kept
# here so the live read and the marker agree on what is believable — recording
# or reporting one of these would MUTE a real "never acquired an IP" fault
# instead of surfacing it.
UNUSABLE_IP_PREFIXES = ("127.", "169.254.")
HOTSPOT_GATEWAY_IP = "10.42.0.1"


def read_lan_ip_live(
    *,
    iface: str | None = None,
    cache_key: str = "lan-ip-live",
    ttl: float = STATUS_SUBPROC_TTL_S,
    timeout: float = STATUS_SUBPROC_TIMEOUT_S,
) -> tuple[str | None, bool]:
    """The interface's CURRENT IPv4, as ``(address, determined)``.

    ``determined`` is the whole point. ``(None, True)`` means "asked the kernel,
    this box has no usable IPv4" — an authoritative negative that must trip the
    missing-IP anomaly. ``(None, False)`` means the question could not be asked
    (``ip`` missing, timed out), and the caller should fall back to the marker
    rather than invent a fault. :func:`~control_server._subprocess.cached_subprocess`
    already distinguishes those: ``None`` on subprocess failure, ``""`` on a
    command that ran and said nothing.

    Why this exists (litclock-dev#672): ``/run/litclock/last-rendered-ip`` is
    written by nm-dispatcher and NOTHING clears it, so a clock that loses DHCP
    without rebooting keeps serving its pre-outage address and /diagnostics
    reports a healthy network for the whole outage. Confirmed on hardware: four
    minutes with ``wlan0=[]`` while the marker — and therefore the page — still
    read ``192.168.2.99``. That is the exact user-visible failure litclock-dev#645 was
    opened to fix, reached from the other direction: the fault is muted by the
    previous GOOD value rather than by a bogus new one.

    A fix routed through the dispatcher cannot work here. On a single-stack IPv4
    network a DHCP failure produces ZERO dispatcher invocations — NM goes
    ``ip-config -> failed -> disconnected -> prepare`` and never reaches
    ``activated``, so no ``up`` fires and ``dhcp4-change`` needs a lease that
    never exists. The marker goes stale precisely because nothing is running to
    update it.

    ``ip addr`` rather than the connect-trick in ``literary_clock._resolve_lan_ip``:
    that opens a UDP socket to a routable address and reads back the egress IP,
    so it returns None on a box that HAS an address but no default route. For
    the QR that is right (an unreachable clock should fall back to mDNS); here
    it would report "no IP" for a device that is serving the very page making
    the claim — the false-positive class ``_compute_anomalies`` already warns
    about. The interface address is the question this row is asking.

    SCOPED TO ONE INTERFACE, and that is load-bearing (/review, red team +
    Codex, independently). An un-scoped read takes the first global IPv4 by
    interface index, so a second global-scope interface — docker0, wg0,
    tailscale0, a usb0 gadget, a USB-ethernet dongle — re-opens the exact muting
    this function exists to close: demonstrated with wlan0 holding no IPv4 and
    docker0 up, /diagnostics reported ``172.17.0.1`` and the network anomaly did
    not fire. It also defeats the hotspot exclusion, because skipping
    ``10.42.0.1`` on wlan0 just falls through to the next interface. The
    dispatcher that writes the marker is interface-scoped for the same reason
    and says so (``[ "$INTERFACE" = "wlan0" ] || exit 0``, with a comment about
    a later USB dongle grabbing a different name), so scoping here is also what
    keeps the two agreeing.

    A NON-ZERO EXIT reads as authoritative "no address", deliberately.
    ``cached_subprocess`` returns ``""`` for any non-zero exit — it does not
    surface the code — so this cannot distinguish "ran and printed nothing" from
    "ran and failed". Of the two ways to be wrong, this one surfaces a fault
    that may not exist, and the other hides a fault that does; the anomaly logic
    in ``_anomalies`` makes the same call everywhere else ("every non-sane
    uptime fails SAFE"). A genuinely broken ``ip`` is also a real problem worth
    reporting. Only a subprocess that could not run AT ALL (missing binary,
    timeout — ``None``, not ``""``) falls back to the marker.
    """
    iface = iface or "wlan0"
    raw = cached_subprocess(
        cache_key,
        ["ip", "-4", "-o", "addr", "show", "dev", iface, "scope", "global"],
        timeout=timeout,
        ttl=ttl,
    )
    if raw is None:
        return None, False

    for line in raw.splitlines():
        parts = line.split()
        # `2: wlan0    inet 192.168.2.99/24 brd 192.168.2.255 scope global ...`
        if "inet" not in parts:
            continue
        inet_at = parts.index("inet")
        if inet_at + 1 >= len(parts):
            continue
        line_iface = parts[1] if len(parts) > 1 else ""
        address = parts[inet_at + 1].split("/", 1)[0]
        if line_iface == "lo" or not address:
            continue
        if address.startswith(UNUSABLE_IP_PREFIXES) or address == HOTSPOT_GATEWAY_IP:
            continue
        return address, True

    # The command ran and reported no usable global IPv4 on this interface.
    return None, True


def read_lan_ip(path: str | None = None) -> str | None:
    """LAN IP last RECORDED by nm-dispatcher.

    Since litclock-dev#672 this is the FALLBACK, not the primary source:
    :func:`read_lan_ip_live` is consulted first and is authoritative when it can
    answer, because nothing ever clears this marker and a stale address mutes
    the missing-IP anomaly for a whole outage. This still serves
    :func:`read_last_dhcp_iso`, and answers when the live read cannot run.

    Reads ``/run/litclock/last-rendered-ip`` (override via ``path``). The
    dispatcher writes this on actual IP change only — so freshness reflects
    DHCP-renew + lease-change cadence, not request cadence.

    The filename is historical. Since litclock-dev#645 the dispatcher records the
    address on every qualifying event, including the pre-handoff ``up`` where
    no render follows at all — so "recorded", not "rendered", and NOT
    necessarily the value the e-ink QR encodes (both QR producers resolve the
    address live instead: ``literary_clock._resolve_lan_ip`` and
    ``handoff``). The path itself is load-bearing across this module,
    ``routes/diagnostics/_collectors``, and the ``LITCLOCK_DIAG_LAST_IP_PATH``
    override, so it is deliberately not renamed;
    ``tests/test_nm_dispatcher_behavior.py`` asserts the writer and both
    readers still name the same file.

    The dispatcher refuses to record an address that would lie: our own setup
    hotspot's gateway, a DHCP-failure ``169.254.x`` link-local, or loopback.
    :func:`~control_server.routes.diagnostics._anomalies._compute_anomalies`
    treats any non-empty value here as a healthy network, so an unbelievable
    address would mute a real "never acquired an IP" fault.

    Distinguishes ``None`` from ``""``: passing the empty string is treated
    as an intentional "disable" — the read attempts ``Path("")`` which
    raises ``OSError`` and degrades to ``None`` (matches the pre-litclock-dev#419
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
