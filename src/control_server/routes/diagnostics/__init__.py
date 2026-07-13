"""``/api/diagnostics`` + ``/diagnostics`` + ``/api/logs*`` route package.

Split out of the pre-#419 monolithic ``routes/diagnostics.py`` (M1, see
the GitHub issue body for the full follow-up bucket). Submodules:

- :mod:`._collectors` — per-row readers + :func:`collect_diagnostics` +
  subprocess cache helpers + ``DIAG_*`` constants + the data ``SECTION_IDS``
  ordering.
- :mod:`._anomalies` — :func:`_compute_anomalies` thresholds + helpers.
- :mod:`._copy_payload` — :func:`build_copy_payload` markdown assembler.
- :mod:`._sse` — :data:`bp` Flask blueprint + the 4 route handlers + SSE
  subscriber registry + supersession lifecycle + template filter.

Config precedence (M4): every reader that touches an external path
consults ``current_app.config[<KEY>]`` first, falling back to the
module-level ``DEFAULT_*`` constants. The Flask-config layer wins
implicitly because readers consult it first; env vars provide the
build-time defaults.

Test patching note (D8): re-export here is for plain ``from
control_server.routes.diagnostics import collect_diagnostics`` style
imports. It does NOT make ``monkeypatch.setattr(diagnostics,
"collect_diagnostics", fake)`` redirect the route's call site — the route
inside ``_sse.py`` looks up the name in its own module namespace where it
was bound at import time. Tests that mock readers or the assembler MUST
patch the actual binding site, e.g.
``monkeypatch.setattr("control_server.routes.diagnostics._sse.collect_diagnostics", fake)``.
"""

from __future__ import annotations

from ._anomalies import (
    ANOMALY_CPU_TEMP_C,
    ANOMALY_DHCP_AGE_S,
    ANOMALY_DISK_FREE_PCT,
    ANOMALY_LAST_IPGEO_AGE_S,
    ANOMALY_MEMORY_FREE_MB,
    ANOMALY_QUOTE_AGE_S,
    ANOMALY_RECENT_LOG_LOOKBACK,
    ANOMALY_SIGNAL_DBM,
    _compute_anomalies,
    _compute_section_states,
    _compute_uncollected,
    _is_numeric,
    _recent_logs_contain_error,
)
from ._collectors import (
    DIAG_JOURNAL_LINES_PER_UNIT,
    DIAG_JOURNAL_TIMEOUT_S,
    DIAG_ONESHOT_UNITS,
    DIAG_SUBPROC_TIMEOUT_S,
    DIAG_SUBPROC_TTL_S,
    DIAG_UNITS,
    PRIVACY_POLICY,
    REDACTED_VALUE,
    SECTION_IDS,
    _batched_is_active,
    _batched_journal_tails,
    _build_service_states,
    _coerce_float,
    _lazy_cache,
    _lazy_cache_lock,
    _read_app_version,
    _read_appliance_uptime_s,
    _read_cpu_temp_c,
    _read_current_quote,
    _read_default_route,
    _read_disk_free_pct,
    _read_gateway,
    _read_git_head,
    _read_iface,
    _read_images_version,
    _read_journal_tail,
    _read_kernel_release,
    _read_lan_ip,
    _read_last_dhcp_iso,
    _read_last_update,
    _read_memory_free_mb,
    _read_os_release_pretty,
    _read_recent_log_entries,
    _read_signal_dbm,
    _read_ssid,
    _read_text_once,
    _read_timezone,
    _setup_marker_present,
    cached_subprocess,
    cached_subprocess_or_empty,
    collect_diagnostics,
    format_uptime,
    redact,
    redact_text,
    schema_keys,
)
from ._copy_payload import build_copy_payload
from ._sse import (
    SSE_CONNECTION_TIMEOUT_S,
    SSE_HEARTBEAT_INTERVAL_S,
    SSE_INNER_POLL_S,
    SSE_MAX_CONCURRENT_STREAMS,
    _check_schema_match,
    _format_log_ts,
    _generate_sse,
    _parse_reveal_groups,
    _redact_values_for_envelope,
    _register_sse,
    _serialize_log_entries,
    _sse_format,
    _sse_registry,
    _SseSession,
    _unregister_sse,
    api_diagnostics,
    api_logs,
    api_logs_stream,
    bp,
    page_diagnostics,
)

__all__ = [
    # --- Anomaly thresholds + helpers ---
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
    "_recent_logs_contain_error",
    # --- Collectors / readers + constants ---
    "DIAG_JOURNAL_LINES_PER_UNIT",
    "DIAG_ONESHOT_UNITS",
    "DIAG_JOURNAL_TIMEOUT_S",
    "DIAG_SUBPROC_TIMEOUT_S",
    "DIAG_SUBPROC_TTL_S",
    "DIAG_UNITS",
    "PRIVACY_POLICY",
    "REDACTED_VALUE",
    "SECTION_IDS",
    "_batched_is_active",
    "_batched_journal_tails",
    "_build_service_states",
    "_coerce_float",
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
    "cached_subprocess",
    "cached_subprocess_or_empty",
    "collect_diagnostics",
    "format_uptime",
    "redact",
    "redact_text",
    "schema_keys",
    # --- Copy payload assembler ---
    "build_copy_payload",
    # --- SSE machinery + routes ---
    "SSE_CONNECTION_TIMEOUT_S",
    "SSE_HEARTBEAT_INTERVAL_S",
    "SSE_INNER_POLL_S",
    "SSE_MAX_CONCURRENT_STREAMS",
    "_SseSession",
    "_check_schema_match",
    "_format_log_ts",
    "_generate_sse",
    "_parse_reveal_groups",
    "_redact_values_for_envelope",
    "_register_sse",
    "_serialize_log_entries",
    "_sse_format",
    "_sse_registry",
    "_unregister_sse",
    "api_diagnostics",
    "api_logs",
    "api_logs_stream",
    "bp",
    "page_diagnostics",
]
