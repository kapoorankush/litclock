"""Copy-payload assembler for the /api/diagnostics surface.

Split out of the pre-#419 monolithic ``routes/diagnostics.py`` (M1).
:func:`build_copy_payload` is the markdown block the user pastes into a
GitHub issue / Slack thread / email. The trailing ``_captured: …_``
timestamp is request-time so a stale paste is easy to spot.

Default-redacts SSID / city / coords via :func:`redact` with
``kind="copy"``; the JS-enabled PWA composes its own payload client-side
based on the live Reveal state.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from ..._diagnostics_privacy import redact


def build_copy_payload(
    values: dict[str, Any],
    revealed_groups: frozenset[str] = frozenset(),
) -> str:
    """Assemble the markdown block the user pastes into a GitHub issue.

    The payload renders each known field via :func:`redact` with
    ``kind="copy"`` so SSID + city stay redacted by default. Coordinates
    are rounded to 2 dp regardless of the reveal state.

    Format is intentionally a fenced ``markdown`` block so the helper can
    drop it into an issue, a Slack code-block, an email, etc.
    """
    sections: list[tuple[str, list[tuple[str, str]]]] = [
        (
            "Build & version",
            [
                ("App version", "app_version"),
                ("git HEAD", "git_head"),
                ("Images version", "images_version"),
                ("Last update", "last_update_at"),
                ("Last update version", "last_update_version"),
            ],
        ),
        (
            "System",
            [
                ("Kernel", "kernel"),
                ("OS", "os_release"),
                ("Uptime", "uptime_human"),
                ("CPU temp °C", "cpu_temp_c"),
                ("Free memory MB", "memory_free_mb"),
                ("Free disk %", "disk_free_pct"),
            ],
        ),
        (
            "Network",
            [
                ("Interface", "iface"),
                ("SSID", "ssid"),
                ("LAN IP", "lan_ip"),
                ("Gateway", "gateway"),
                ("Signal dBm", "signal_dbm"),
                ("Last DHCP", "last_dhcp_at"),
            ],
        ),
        (
            "Time & location",
            [
                ("Timezone", "timezone"),
                ("City", "weather_location_name"),
                ("Lat", "weather_lat"),
                ("Lon", "weather_lon"),
                ("Mode", "weather_location_mode"),
                ("IP country", "weather_ip_country"),
                ("Units", "weather_units"),
                ("Weather on?", "weather_enabled"),
                ("Last IP-geo", "last_ip_geo_at"),
            ],
        ),
        (
            "Setup markers",
            [
                (".setup-complete", "setup_complete"),
                (".handoff-complete", "handoff_complete"),
                ("Gift mode", "gift_mode_active"),
                ("Allow NSFW", "allow_nsfw_quotes"),
            ],
        ),
    ]

    lines: list[str] = []
    lines.append("```markdown")
    lines.append("# LitClock diagnostics")
    captured = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines.append(f"_captured: {captured}_")
    lines.append("")
    for heading, rows in sections:
        lines.append(f"## {heading}")
        for label, field in rows:
            value = values.get(field)
            rendered = redact(field, value, kind="copy", revealed_groups=revealed_groups)
            if rendered == "":
                rendered = "—"
            lines.append(f"- **{label}:** {rendered}")
        lines.append("")
    # Services block — the per-unit nested shape doesn't go through
    # redact() because the field is "service_states" (a dict, safe-clear
    # in the policy), but rendering it inline as JSON in the copy block
    # would be unreadable. Flatten to one row per unit.
    services = values.get("service_states") or {}
    if isinstance(services, dict) and services:
        lines.append("## Services")
        for unit, info in services.items():
            if not isinstance(info, dict):
                continue
            state = info.get("state", "unknown")
            tail = info.get("journal_tail") or []
            lines.append(f"- **{unit}:** {state}")
            for ln in tail:
                lines.append(f"    {ln}")
        lines.append("")
    # Last quote
    quote = values.get("quote")
    if quote:
        lines.append("## Last quote")
        lines.append(f"> {quote}")
        attr_parts: list[str] = []
        author = values.get("author")
        title = values.get("title")
        when = values.get("time")
        if author:
            attr_parts.append(str(author))
        if title:
            attr_parts.append(f"_{title}_")
        if when:
            attr_parts.append(str(when))
        if attr_parts:
            lines.append(f"— {' · '.join(attr_parts)}")
        lines.append("")
    # Recent log entries (snapshot, max 4)
    recent = values.get("recent_log_entries") or []
    if isinstance(recent, list) and recent:
        lines.append("## Recent log entries (snapshot)")
        for entry in recent:
            if not isinstance(entry, dict):
                continue
            ts = entry.get("timestamp")
            level = entry.get("level", "")
            msg = entry.get("message", "")
            if isinstance(ts, (int, float)):
                ts_str = datetime.fromtimestamp(ts, tz=UTC).strftime("%H:%M:%S")
            else:
                ts_str = ""
            lines.append(f"- `{ts_str}` **{level}** {msg}")
        lines.append("")
    lines.append("```")
    return "\n".join(lines)


def build_support_logs_bundle(
    system_payload: str,
    units: Sequence[str],
    deep_tail_fn: Callable[[str], list[str]],
    *,
    budget_s: float,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Assemble the on-demand 'deep logs for support' bundle (#416 follow-up).

    A SINGLE pasteable/downloadable text blob: the standard copy payload (system
    state, default-redacted) followed by a deeper per-unit journal tail than the
    3-line page preview, so one paste actually carries enough to debug.

    ``deep_tail_fn(unit)`` returns the already-redacted tail lines for a unit
    (the caller injects redaction + the distinct-cache-key deep read). Reads are
    serial and each journalctl can be slow on a Pi Zero 2W, so a wall-clock
    ``budget_s`` bounds the whole assembly: on overrun we STOP and append an
    explicit truncation note naming the skipped units — never a silent cap.
    ``clock`` is injectable for deterministic tests.
    """
    start = clock()
    parts: list[str] = [system_payload, "", "## Logs (deep tail per unit)", ""]
    skipped: list[str] = []
    for i, unit in enumerate(units):
        if clock() - start > budget_s:
            skipped = list(units[i:])
            break
        parts.append(f"### {unit}")
        parts.append("```")
        tail = deep_tail_fn(unit)
        parts.extend(tail if tail else ["(no journal entries)"])
        parts.append("```")
        parts.append("")
    if skipped:
        parts.append(
            f"_[truncated: {len(skipped)} unit(s) not read after the {budget_s:.0f}s budget — {', '.join(skipped)}]_"
        )
    return "\n".join(parts)
