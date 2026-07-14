"""Per-row privacy policy for the /diagnostics surface (#416 / design C1=A, OV-6=A).

The diagnostic payload is designed to be pasted into a public GitHub issue,
which means three classes of data need different treatment:

1. **PII** — fields the user typed (city name, SSID) that may inadvertently
   contain street addresses, network names, etc. Default-redacted, optionally
   un-revealed via the per-session "Reveal SSID & city" toggle on the page.
2. **Quasi-PII** — exact coordinates. We render at 2dp precision (city-block
   accuracy, not address-level) so the helper can still see "user is in
   Texas" without leaking the front porch.
3. **Safe-clear** — booleans, enums, version SHAs, uptime numbers. No privacy
   concern; always rendered verbatim.

Per OV-6=A: the PRIVACY_POLICY is the **fail-closed contract**. At runtime
an unknown field renders ``(redacted by policy gap, see logs)`` and a
journald log entry surfaces the gap — never a 500. The page keeps working;
the gap surfaces itself.

The PR-time build-time enforcement (``set(collect_diagnostics().keys()) ==
schema_keys()``) lands with PR2 alongside the ``collect_diagnostics()``
route — see ``tests/test_diagnostics_privacy.py::
test_collect_diagnostics_payload_matches_schema``, currently marked skip
until PR2 ships.

The keys in this module match the keys in the JSON payload returned by
``GET /api/diagnostics``, not the names of any underlying env.sh variable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final, Literal

log = logging.getLogger(__name__)


Sensitivity = Literal["safe-clear", "redacted", "rounded"]


@dataclass(frozen=True)
class RowPolicy:
    """How a single diagnostic row treats its value.

    ``display`` controls what renders on the on-screen page.
    ``copy`` controls what lands in the copy-payload markdown block.
    ``reveal_group`` ties multiple rows to one toggle; rows sharing a group
    flip together (e.g. SSID + city + coords are all in ``"location"``).
    """

    display: Sensitivity
    copy: Sensitivity
    reveal_group: str | None = None


# The canonical schema. Every key emitted by collect_diagnostics() MUST appear
# here. The CI keystone test asserts equality on (payload keys) vs
# (PRIVACY_POLICY keys); see tests/test_diagnostics_privacy.py.
#
# Keys are grouped by section to match the page layout. Comments cite the
# /plan-design-review decision and any owner-persona rationale.
PRIVACY_POLICY: Final[dict[str, RowPolicy]] = {
    # --- Build & version (info-only, never an anomaly) ---
    "app_version": RowPolicy("safe-clear", "safe-clear"),
    "git_head": RowPolicy("safe-clear", "safe-clear"),
    "images_version": RowPolicy("safe-clear", "safe-clear"),
    "last_update_at": RowPolicy("safe-clear", "safe-clear"),
    "last_update_version": RowPolicy("safe-clear", "safe-clear"),
    # --- System ---
    "kernel": RowPolicy("safe-clear", "safe-clear"),
    "os_release": RowPolicy("safe-clear", "safe-clear"),
    "uptime_s": RowPolicy("safe-clear", "safe-clear"),
    "uptime_human": RowPolicy("safe-clear", "safe-clear"),
    "cpu_temp_c": RowPolicy("safe-clear", "safe-clear"),
    "memory_free_mb": RowPolicy("safe-clear", "safe-clear"),
    "disk_free_pct": RowPolicy("safe-clear", "safe-clear"),
    # --- Network. SSID is owner-typed-adjacent (their WiFi name); LAN IP +
    # gateway leak the home subnet topology if pasted publicly. Default-redact
    # both; reveal as a group. ---
    "iface": RowPolicy("safe-clear", "safe-clear"),
    "ssid": RowPolicy("redacted", "redacted", reveal_group="location"),
    "lan_ip": RowPolicy("redacted", "redacted", reveal_group="location"),
    "gateway": RowPolicy("redacted", "redacted", reveal_group="location"),
    "signal_dbm": RowPolicy("safe-clear", "safe-clear"),
    "last_dhcp_at": RowPolicy("safe-clear", "safe-clear"),
    # --- Time & location ---
    "timezone": RowPolicy("safe-clear", "safe-clear"),
    "weather_location_name": RowPolicy("redacted", "redacted", reveal_group="location"),
    # Exact lat/lon rounded to 2dp (city-block precision). Display + copy
    # always show the rounded value; revealing un-rounds.
    "weather_lat": RowPolicy("rounded", "rounded", reveal_group="location"),
    "weather_lon": RowPolicy("rounded", "rounded", reveal_group="location"),
    "weather_location_mode": RowPolicy("safe-clear", "safe-clear"),
    "weather_ip_country": RowPolicy("safe-clear", "safe-clear"),
    "weather_units": RowPolicy("safe-clear", "safe-clear"),
    "weather_enabled": RowPolicy("safe-clear", "safe-clear"),
    "last_ip_geo_at": RowPolicy("safe-clear", "safe-clear"),
    # --- Services. systemd unit state is operational, not PII. ---
    "service_states": RowPolicy("safe-clear", "safe-clear"),
    # --- Last quote (corpus content, not user PII) ---
    "quote": RowPolicy("safe-clear", "safe-clear"),
    "author": RowPolicy("safe-clear", "safe-clear"),
    "title": RowPolicy("safe-clear", "safe-clear"),
    "time": RowPolicy("safe-clear", "safe-clear"),
    "picked_at": RowPolicy("safe-clear", "safe-clear"),
    # --- Setup markers (operational booleans / paths) ---
    "setup_complete": RowPolicy("safe-clear", "safe-clear"),
    "handoff_complete": RowPolicy("safe-clear", "safe-clear"),
    "gift_mode_active": RowPolicy("safe-clear", "safe-clear"),
    # --- Allow-or-disallow flags ---
    "allow_nsfw_quotes": RowPolicy("safe-clear", "safe-clear"),
    # --- Recent log entries (each line is already redacted by the
    # RedactingFilter before it lands in the buffer — see _redaction.py). ---
    "recent_log_entries": RowPolicy("safe-clear", "safe-clear"),
}

# Sentinel emitted for any unknown field so the helper-paste block still
# tells the reader something happened (per OV-6=A — never 500 the page).
POLICY_GAP_VALUE: Final[str] = "(redacted by policy gap, see logs)"

# Sentinel emitted for redacted fields when the reveal group is OFF. Chosen
# to be visually obvious in the paste block (no chance of being mistaken
# for real data) and short enough not to break narrow phone rows.
REDACTED_VALUE: Final[str] = "•••••••"


Kind = Literal["copy", "display"]


def redact(
    field: str,
    value: object,
    kind: Kind = "copy",
    revealed_groups: frozenset[str] = frozenset(),
) -> str:
    """Return the redacted string for ``field`` under the given reveal state.

    ``kind`` selects which RowPolicy field to consult:
    - ``"copy"`` — the markdown payload pasted into GitHub issues
    - ``"display"`` — what the owner sees on screen

    Both kinds share the same Sensitivity matrix today; the parameter
    exists so a future PR can diverge them (e.g. display always shows
    the un-redacted value behind a tap-to-reveal microinteraction) without
    re-plumbing every caller.

    Semantics:
    - Unknown field → ``POLICY_GAP_VALUE`` + a warning log entry.
    - ``safe-clear`` → ``str(value)`` (or ``""`` for ``None``).
    - ``redacted`` + group not revealed → ``REDACTED_VALUE``.
    - ``redacted`` + group revealed → ``str(value)`` (or ``""``).
    - ``rounded`` (lat/lon) + group not revealed → 2dp string.
    - ``rounded`` + group revealed → full-precision ``str(value)``.

    Per OV-6=A this NEVER raises — fail-closed for unknown fields means
    "render a visible gap row," not 500 the request handler.
    """
    policy = PRIVACY_POLICY.get(field)
    if policy is None:
        # Fail-closed for the runtime UI; CI test catches at PR time.
        log.warning("DIAGNOSTICS_POLICY_GAP: field=%s", field)
        return POLICY_GAP_VALUE

    if value is None:
        return ""

    sensitivity = policy.copy if kind == "copy" else policy.display
    revealed = policy.reveal_group is not None and policy.reveal_group in revealed_groups

    if sensitivity == "safe-clear":
        return str(value)
    if sensitivity == "redacted":
        return str(value) if revealed else REDACTED_VALUE
    if sensitivity == "rounded":
        try:
            num = float(value)
        except (TypeError, ValueError):
            return REDACTED_VALUE
        return f"{num:.6f}" if revealed else f"{num:.2f}"
    # Defensive fallback. The Sensitivity Literal alias is documentation only
    # — no static checker runs against this codebase, so a future contributor
    # extending the alias without updating this branch would silently hit
    # "unknown" semantics. The runtime warning is the actual enforcement.
    log.warning("DIAGNOSTICS_POLICY_UNKNOWN_KIND: field=%s kind=%s", field, sensitivity)
    return POLICY_GAP_VALUE


# Backwards-compatible wrappers. Existing call sites + tests use the
# explicit redact_for_copy / redact_for_display names; new call sites
# should prefer redact(kind=...) directly.
def redact_for_copy(
    field: str,
    value: object,
    revealed_groups: frozenset[str] = frozenset(),
) -> str:
    """Thin wrapper over :func:`redact` with ``kind="copy"``."""
    return redact(field, value, kind="copy", revealed_groups=revealed_groups)


def redact_for_display(
    field: str,
    value: object,
    revealed_groups: frozenset[str] = frozenset(),
) -> str:
    """Thin wrapper over :func:`redact` with ``kind="display"``."""
    return redact(field, value, kind="display", revealed_groups=revealed_groups)


def schema_keys() -> frozenset[str]:
    """Frozen view of the policy's covered field set. Used by the CI keystone
    test ``test_diagnostics_privacy_schema`` to assert
    ``set(collect_diagnostics().keys()) == schema_keys()``."""
    return frozenset(PRIVACY_POLICY.keys())
