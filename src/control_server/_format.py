"""Shared formatting helpers for control_server routes.

Extracted from ``routes/status.py`` and ``routes/diagnostics.py`` per #419
follow-up M2 — both modules had byte-identical copies of ``format_uptime``.
Single source of truth means a future fix (e.g., sub-minute support) reaches
both surfaces at once.
"""

from __future__ import annotations


def format_uptime(seconds: int) -> str:
    """Compact uptime ``Nd Nh Nm``. Drops zero-prefix units so ``3m`` stays
    ``3m`` rather than ``0d 0h 3m``. Negative seconds (clock skew, mtime
    in the future) render as ``—``.

    DESIGN.md row spec line 380 (status): ``Uptime: Geist Mono 14px ("4d 12h 3m")``.
    Diagnostics renders the same shape via the System section.
    """
    if seconds < 0:
        return "—"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)
