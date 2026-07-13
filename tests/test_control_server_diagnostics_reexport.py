"""Re-export contract for ``control_server.routes.diagnostics``.

Pins the symbols ``__init__.py`` MUST re-export so plain imports like
``from control_server.routes.diagnostics import collect_diagnostics``
keep working after the #419 PR1 package split. The contract was derived
from grepping every existing test + production import site at split time;
see the issue body's D3 enumeration for the rationale.

NOTE (#419 D8): this test verifies PLAIN-IMPORT resolution. It does NOT
test that ``monkeypatch.setattr(diagnostics, "X", fake)`` redirects call
sites inside submodules — that requires patching the actual binding site
(e.g. ``_sse.collect_diagnostics`` or ``_collectors._read_cpu_temp_c``).
The package docstring documents the patch-where-it's-looked-up rule.
"""

from __future__ import annotations

import pytest

from control_server.routes import diagnostics

# Symbols every existing test or production importer reaches into via the
# package namespace. Derived from grep across master pre-#419 PR1.
EXPECTED_PUBLIC = {
    # Route + blueprint
    "bp",
    "api_diagnostics",
    "api_logs",
    "api_logs_stream",
    "page_diagnostics",
    # Public assemblers
    "collect_diagnostics",
    "build_copy_payload",
    # Anomaly engine
    "_compute_anomalies",
    "_compute_section_states",
    "_compute_uncollected",
    "_recent_logs_contain_error",
    "_is_numeric",
    # Per-row readers (tests reach in via diagnostics._read_*)
    "_read_iface",
    "_read_ssid",
    "_read_lan_ip",
    "_read_gateway",
    "_read_signal_dbm",
    "_read_timezone",
    "_read_kernel_release",
    "_read_cpu_temp_c",
    "_read_memory_free_mb",
    "_read_disk_free_pct",
    "_read_appliance_uptime_s",
    "_read_app_version",
    "_read_git_head",
    "_read_images_version",
    "_read_current_quote",
    "_read_recent_log_entries",
    "_read_default_route",
    "_read_last_dhcp_iso",
    "_read_last_update",
    "_read_os_release_pretty",
    # Service-state readers
    "_batched_is_active",
    "_batched_journal_tails",
    "_read_journal_tail",
    "_build_service_states",
    # Subprocess cache + lazy cache (tests clear these between cases)
    "cached_subprocess",
    "cached_subprocess_or_empty",  # #428 PR1a CQ-1: display-caller helper
    "_lazy_cache",
    "_lazy_cache_lock",
    "_read_text_once",
    # Schema + privacy helpers
    "schema_keys",
    "PRIVACY_POLICY",
    "REDACTED_VALUE",
    "_check_schema_match",
    # SSE machinery (tests at tests/test_control_server_logs_routes.py
    # reach into these directly)
    "_sse_registry",
    "_register_sse",
    "_unregister_sse",
    "_sse_format",
    "_generate_sse",
    "_SseSession",
    "SSE_MAX_CONCURRENT_STREAMS",
    "SSE_CONNECTION_TIMEOUT_S",
    "SSE_HEARTBEAT_INTERVAL_S",
    "SSE_INNER_POLL_S",
    # Envelope shaping
    "_redact_values_for_envelope",
    "_parse_reveal_groups",
    "_serialize_log_entries",
    # Constants
    "DIAG_UNITS",
    "DIAG_ONESHOT_UNITS",
    "DIAG_JOURNAL_LINES_PER_UNIT",
    "DIAG_SUBPROC_TTL_S",
    "DIAG_JOURNAL_TIMEOUT_S",
    "DIAG_SUBPROC_TIMEOUT_S",
    "SECTION_IDS",
    "ANOMALY_CPU_TEMP_C",
    "ANOMALY_DISK_FREE_PCT",
    "ANOMALY_MEMORY_FREE_MB",
    "ANOMALY_SIGNAL_DBM",
    "ANOMALY_DHCP_AGE_S",
    "ANOMALY_LAST_IPGEO_AGE_S",
    "ANOMALY_QUOTE_AGE_S",
    "ANOMALY_RECENT_LOG_LOOKBACK",
    # Template filter
    "_format_log_ts",
    # _coerce_float is used by anomaly logic + tests may reach in
    "_coerce_float",
    # Format helper (canonical name; pre-#419's leading-underscore alias
    # was dropped in PR1 because grep across master found zero callers
    # and the alias was misleading — calls inside _collectors.py use
    # format_uptime directly, so monkey-patching ``diagnostics._format_uptime``
    # wouldn't have redirected the route anyway).
    "format_uptime",
    # Redaction helpers re-exported for backward compat
    "redact",
    "redact_text",
    # Setup marker presence check
    "_setup_marker_present",
}


class TestReexportContract:
    """Every name in EXPECTED_PUBLIC must be importable from the package.

    A new internal helper that ends up being reached by a test should be
    added here so the contract stays honest. A removed symbol that drops
    from EXPECTED_PUBLIC needs a matching delete from ``__all__``.
    """

    @pytest.mark.parametrize("name", sorted(EXPECTED_PUBLIC))
    def test_symbol_resolves(self, name: str) -> None:
        assert hasattr(diagnostics, name), (
            f"control_server.routes.diagnostics.{name} is missing — "
            "either re-export it in __init__.py or remove it from "
            "tests/test_control_server_diagnostics_reexport.EXPECTED_PUBLIC."
        )

    def test_all_matches_expected(self) -> None:
        # __all__ is the canonical public list. Drift between EXPECTED_PUBLIC
        # and __all__ catches both directions of regression:
        # - symbol removed from __all__ but kept in expected → fails above
        # - symbol added to __all__ but not in expected → fails here, prompts
        #   the reviewer to either add the new symbol to EXPECTED_PUBLIC or
        #   ask why it leaked into the public surface.
        actual = set(diagnostics.__all__)
        unexpected = actual - EXPECTED_PUBLIC
        missing = EXPECTED_PUBLIC - actual
        assert not missing, f"__all__ missing required symbols: {sorted(missing)}"
        assert not unexpected, (
            f"__all__ contains symbols not in EXPECTED_PUBLIC: "
            f"{sorted(unexpected)}. Either add them to EXPECTED_PUBLIC or "
            "drop from __all__."
        )

    def test_bp_is_flask_blueprint(self) -> None:
        # The blueprint registration in ``control_server/__init__.py`` does
        # ``app.register_blueprint(diagnostics.bp)``; mistaking it for a
        # function would silently break route registration.
        from flask import Blueprint

        assert isinstance(diagnostics.bp, Blueprint)
        assert diagnostics.bp.name == "diagnostics"

    def test_collect_diagnostics_is_callable(self) -> None:
        # The route looks up `collect_diagnostics` inside `_sse.py` —
        # ensure the package-level alias still references the same
        # callable identity (so a test that grabs `diagnostics.collect_diagnostics`
        # AND a test that imports it from `_collectors` see the same function).
        from control_server.routes.diagnostics import _collectors, _sse

        assert callable(diagnostics.collect_diagnostics)
        assert diagnostics.collect_diagnostics is _collectors.collect_diagnostics
        # _sse imports the name; verify it points to the same function
        # (the route's call site).
        assert _sse.collect_diagnostics is _collectors.collect_diagnostics

    def test_sse_call_site_identities(self) -> None:
        """Codex F3: the D8 patch-where-it's-looked-up trap applies to
        every symbol the route binds at import time, not just
        collect_diagnostics. A package-level re-binding must NOT
        diverge from the _sse.py binding silently.

        If this test fails, a tests file is monkeypatching one of these
        symbols on the package namespace expecting the route to see the
        change — and it WON'T. Either update the test to patch
        ``_sse.<name>``, OR add the missing identity check here so the
        regression surfaces.
        """
        from control_server.routes.diagnostics import (
            _anomalies,
            _collectors,
            _copy_payload,
            _sse,
        )

        # _sse calls collect_diagnostics, schema_keys (from _collectors),
        # _compute_anomalies (from _anomalies), build_copy_payload
        # (from _copy_payload). Each must be the SAME object as the
        # submodule definition so package-level rebinding can't drift.
        assert _sse.collect_diagnostics is _collectors.collect_diagnostics
        assert _sse.schema_keys is _collectors.schema_keys
        assert _sse.SECTION_IDS is _collectors.SECTION_IDS
        assert _sse._compute_anomalies is _anomalies._compute_anomalies
        assert _sse.build_copy_payload is _copy_payload.build_copy_payload
        # And the package alias mirrors them too.
        assert diagnostics._compute_anomalies is _anomalies._compute_anomalies
        assert diagnostics.build_copy_payload is _copy_payload.build_copy_payload
        assert diagnostics.schema_keys is _collectors.schema_keys

    def test_sse_registry_is_shared_object(self) -> None:
        """Codex adversarial #8: tests reach into ``diagnostics._sse_registry``
        and call ``.clear()`` expecting it to affect the live registry.
        That only works if the package alias IS the same OrderedDict
        object as the one inside ``_sse.py``. A future refactor that
        wraps the registry in a property/factory would break tests
        silently — pin the identity here.
        """
        from control_server.routes.diagnostics import _sse

        assert diagnostics._sse_registry is _sse._sse_registry
