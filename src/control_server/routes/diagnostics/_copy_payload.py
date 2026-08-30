"""Copy-payload assembler for the /api/diagnostics surface.

Split out of the pre-litclock-dev#419 monolithic ``routes/diagnostics.py`` (M1).
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

# litclock-dev#532 PR 4c: row labels resolve from the language catalog —
# the same source the diagnostics page's Jinja side reads via t(), which
# is what makes the pasted payload match the page the user is looking at.
# The table lives inside build_copy_payload, so labels resolve per call
# (a language switch reaches the next Copy without a restart).
# PORT NOTE: this module-level import makes the whole diagnostics package
# hard-depend on the litclock-dev#532 chain (strings_catalog + languages/) —
# same warning as control_server/__init__'s create_app note.
from strings_catalog import get as _t

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
                (_t("diag.row.app_version"), "app_version"),
                (_t("diag.row.git_head"), "git_head"),
                (_t("diag.row.images_version"), "images_version"),
                (_t("diag.row.last_update_at"), "last_update_at"),
                (_t("diag.row.last_update_version"), "last_update_version"),
            ],
        ),
        (
            "System",
            [
                (_t("diag.row.kernel"), "kernel"),
                (_t("diag.row.os_release"), "os_release"),
                (_t("diag.row.uptime_human"), "uptime_human"),
                (_t("diag.row.cpu_temp_c"), "cpu_temp_c"),
                (_t("diag.row.memory_free_mb"), "memory_free_mb"),
                (_t("diag.row.disk_free_pct"), "disk_free_pct"),
            ],
        ),
        (
            "Network",
            [
                (_t("diag.row.iface"), "iface"),
                (_t("diag.row.ssid"), "ssid"),
                (_t("diag.row.lan_ip"), "lan_ip"),
                (_t("diag.row.gateway"), "gateway"),
                (_t("diag.row.signal_dbm"), "signal_dbm"),
                (_t("diag.row.last_dhcp_at"), "last_dhcp_at"),
            ],
        ),
        (
            "Time & location",
            [
                (_t("diag.row.timezone"), "timezone"),
                (_t("diag.row.weather_location_name"), "weather_location_name"),
                (_t("diag.row.weather_lat"), "weather_lat"),
                (_t("diag.row.weather_lon"), "weather_lon"),
                (_t("diag.row.weather_location_mode"), "weather_location_mode"),
                (_t("diag.row.weather_ip_country"), "weather_ip_country"),
                (_t("diag.row.weather_units"), "weather_units"),
                (_t("diag.row.weather_enabled"), "weather_enabled"),
                (_t("diag.row.last_ip_geo_at"), "last_ip_geo_at"),
            ],
        ),
        (
            "Setup markers",
            [
                (_t("diag.row.setup_complete"), "setup_complete"),
                (_t("diag.row.handoff_complete"), "handoff_complete"),
                (_t("diag.row.gift_mode_active"), "gift_mode_active"),
                (_t("diag.row.allow_nsfw_quotes"), "allow_nsfw_quotes"),
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
        # Which render tier painted it (litclock-dev#531). Printed for EVERY quote:
        # "image-fallback" (runtime attempted and lost) is the exact
        # condition this bundle exists to reveal, and it is invisible on
        # the panel itself. picked_at rides along so a reader can tell
        # whether the tier describes THIS minute or a stale write
        # (litclock-dev#543 review F5).
        render_mode = values.get("render_mode")
        if render_mode:
            lines.append(f"Rendered by: {render_mode}")
        picked_at = values.get("picked_at")
        if picked_at:
            lines.append(f"Picked at: {picked_at}")
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
    """Assemble the on-demand 'deep logs for support' bundle (litclock-dev#416 follow-up).

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
