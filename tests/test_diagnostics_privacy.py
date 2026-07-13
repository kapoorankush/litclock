"""Tests for control_server/_diagnostics_privacy.py (#416 T3 + OV-6=A).

Covers:
- safe-clear / redacted / rounded behavior for each policy class
- reveal-group toggling
- unknown field → fail-closed policy gap WITH log warning (never raises)
- ``schema_keys()`` exposes the locked field set for the CI keystone test
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from control_server import _diagnostics_privacy as priv  # noqa: E402


class TestRedact:
    """The collapsed redact(field, value, kind, revealed_groups) function
    is the canonical entry point; redact_for_copy / redact_for_display
    are 1-line wrappers (PR1 /review ASK-1=A)."""

    def test_kind_copy_matches_wrapper(self):
        # Both call paths must agree for every policy class.
        for field, value in [
            ("app_version", "v0.213.0"),
            ("ssid", "MyWiFi"),
            ("weather_lat", 37.7749),
            ("unknown_x", "whatever"),
        ]:
            assert priv.redact(field, value, kind="copy") == priv.redact_for_copy(field, value)

    def test_kind_display_matches_wrapper(self):
        for field, value in [
            ("app_version", "v0.213.0"),
            ("ssid", "MyWiFi"),
            ("weather_lat", 37.7749),
            ("unknown_x", "whatever"),
        ]:
            assert priv.redact(field, value, kind="display") == priv.redact_for_display(field, value)

    def test_default_kind_is_copy(self):
        # The collapsed signature defaults kind="copy" because copy is
        # the public surface (clipboard payload).
        assert priv.redact("app_version", "v1") == priv.redact_for_copy("app_version", "v1")

    def test_kind_selects_correct_policy_field(self):
        # Build a synthetic divergence and assert kind=copy vs kind=display
        # pick different sensitivities. (Today no policy entry has
        # copy != display; this is the future-divergence story.)
        original = priv.PRIVACY_POLICY["app_version"]
        priv.PRIVACY_POLICY["__test_divergent__"] = priv.RowPolicy(
            display="safe-clear",
            copy="redacted",
            reveal_group="location",  # not revealed → redacts
        )
        try:
            assert priv.redact("__test_divergent__", "secret", kind="copy") == priv.REDACTED_VALUE
            assert priv.redact("__test_divergent__", "secret", kind="display") == "secret"
        finally:
            del priv.PRIVACY_POLICY["__test_divergent__"]
            # sanity: didn't touch other entries
            assert priv.PRIVACY_POLICY["app_version"] is original


class TestRedactForCopy:
    def test_safe_clear_passes_value_through(self):
        assert priv.redact_for_copy("app_version", "v0.213.0") == "v0.213.0"

    def test_safe_clear_renders_none_as_empty(self):
        assert priv.redact_for_copy("app_version", None) == ""

    def test_redacted_default_off_returns_marker(self):
        assert priv.redact_for_copy("ssid", "MyWiFi") == priv.REDACTED_VALUE

    def test_redacted_with_reveal_returns_value(self):
        out = priv.redact_for_copy("ssid", "MyWiFi", frozenset({"location"}))
        assert out == "MyWiFi"

    def test_redacted_wrong_reveal_group_stays_redacted(self):
        # A different reveal group (made up) doesn't un-redact ssid.
        out = priv.redact_for_copy("ssid", "MyWiFi", frozenset({"some-other-group"}))
        assert out == priv.REDACTED_VALUE

    def test_rounded_default_2dp(self):
        out = priv.redact_for_copy("weather_lat", 37.7749)
        assert out == "37.77"

    def test_rounded_revealed_full_precision(self):
        out = priv.redact_for_copy("weather_lat", 37.7749, frozenset({"location"}))
        assert out == "37.774900"

    def test_rounded_handles_bad_value_gracefully(self):
        # Garbage in -> redacted marker; never raises.
        assert priv.redact_for_copy("weather_lat", "abc") == priv.REDACTED_VALUE

    def test_unknown_field_returns_policy_gap_and_logs(self, caplog):
        with caplog.at_level(logging.WARNING, logger="control_server._diagnostics_privacy"):
            out = priv.redact_for_copy("definitely_not_a_real_field", "secret-value")
        assert out == priv.POLICY_GAP_VALUE
        assert any("DIAGNOSTICS_POLICY_GAP" in rec.getMessage() for rec in caplog.records), (
            "policy gap must log a warning per OV-6=A"
        )

    def test_unknown_field_returns_marker_not_just_no_raise(self):
        # OV-6=A: fail-closed renders the marker AND the call returns —
        # the contract is "never 500 the page," but also "never silently
        # return the raw value either." Assert the marker explicitly so a
        # regression that emits the original value would fail.
        assert priv.redact_for_copy("unknown_x", "whatever") == priv.POLICY_GAP_VALUE
        # The policy lookup happens BEFORE the None check, so an unknown
        # field with a None value still returns the gap marker (not "").
        # A known field with None returns "" — see test_safe_clear_renders_none_as_empty.
        assert priv.redact_for_copy("unknown_y", None) == priv.POLICY_GAP_VALUE
        assert priv.redact_for_copy("", None) == priv.POLICY_GAP_VALUE
        # A whitespace key is still an unknown key.
        assert priv.redact_for_copy("\n\n", "ignored") == priv.POLICY_GAP_VALUE


class TestRedactForDisplay:
    """Display path mirrors copy today (see redact_for_display docstring).
    Re-running the same matrix protects against accidental divergence."""

    def test_safe_clear_passes_value_through(self):
        assert priv.redact_for_display("app_version", "v0.213.0") == "v0.213.0"

    def test_redacted_default_off_returns_marker(self):
        assert priv.redact_for_display("ssid", "MyWiFi") == priv.REDACTED_VALUE

    def test_redacted_revealed_returns_value(self):
        assert priv.redact_for_display("ssid", "MyWiFi", frozenset({"location"})) == "MyWiFi"

    def test_rounded_default_2dp(self):
        assert priv.redact_for_display("weather_lat", 37.7749) == "37.77"


class TestSchemaContract:
    """The CI keystone: PRIVACY_POLICY's key set IS the contract for what
    fields ``collect_diagnostics()`` may emit. A future PR adding a new
    field WITHOUT a privacy entry must fail this test before the policy-gap
    runtime path fires.

    The collect_diagnostics() route is built in PR2; for now we lock the
    expected shape (the keys that ship in PR2). When PR2 lands, the
    integration test will assert
    ``set(collect_diagnostics().keys()) == schema_keys()``.
    """

    def test_schema_keys_is_frozenset(self):
        assert isinstance(priv.schema_keys(), frozenset)

    def test_schema_keys_matches_policy_keys(self):
        # Tautological assertion of an implementation detail (schema_keys()
        # is defined as `frozenset(PRIVACY_POLICY.keys())`). The REAL
        # keystone test — `set(collect_diagnostics().keys()) ==
        # schema_keys()` — lands with collect_diagnostics() in PR2.
        # Keeping this here as the grep target so PR2's eng review surfaces
        # the obligation.
        assert priv.schema_keys() == frozenset(priv.PRIVACY_POLICY.keys())

    def test_collect_diagnostics_payload_matches_schema(self, tmp_path):
        """Build-time gate against the OV-6=A failure mode (a new diag
        field added without a privacy entry, or vice versa). Unblocks
        in PR2 when ``collect_diagnostics()`` ships."""
        from control_server import create_app  # noqa: PLC0415
        from control_server.routes.diagnostics import collect_diagnostics  # noqa: PLC0415

        env = tmp_path / "env.sh"
        env.write_text("WEATHER_ENABLED=false\n")
        app = create_app({"ENV_FILE": str(env)})
        with app.app_context():
            values = collect_diagnostics()
        assert set(values.keys()) == priv.schema_keys()

    def test_required_field_groups_present(self):
        # Spot-check that the canonical sections from /plan-design-review
        # all have at least one field.
        keys = priv.schema_keys()
        assert "app_version" in keys  # Build & version
        assert "kernel" in keys  # System
        assert "ssid" in keys and "lan_ip" in keys  # Network
        assert "timezone" in keys  # Time & location
        assert "service_states" in keys  # Services
        assert "quote" in keys  # Last quote
        assert "setup_complete" in keys  # Setup markers
        assert "recent_log_entries" in keys  # Recent log entries

    def test_user_typed_fields_are_redacted_by_default(self):
        # The two fields the user types verbatim (city, ssid) MUST default
        # to redacted. Coordinates MUST default to rounded. This locks the
        # privacy posture against a future PR that flips a single line.
        assert priv.PRIVACY_POLICY["ssid"].copy == "redacted"
        assert priv.PRIVACY_POLICY["weather_location_name"].copy == "redacted"
        assert priv.PRIVACY_POLICY["weather_lat"].copy == "rounded"
        assert priv.PRIVACY_POLICY["weather_lon"].copy == "rounded"

    def test_reveal_groups_are_consistent(self):
        # Every field with copy="redacted" or copy="rounded" should belong
        # to a reveal group — otherwise the toggle can't reach it.
        for field, p in priv.PRIVACY_POLICY.items():
            if p.copy in ("redacted", "rounded"):
                assert p.reveal_group is not None, f"field {field!r} is redacted/rounded but has no reveal_group"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
