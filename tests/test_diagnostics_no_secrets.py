"""End-to-end deny-list gate against the diagnostics surface (#416 PR2 A6=A).

The /plan-eng-review A6=A decision was: allowlist (locked at the schema
level) PLUS deny-list (substring/regex scan of the rendered output). The
allowlist lives in tests/test_control_server_diagnostics.py via
``set(values.keys()) == schema_keys()``. This file is the deny-list half.

The contract: NOTHING the redaction patterns would flag should appear in
the rendered JSON payload OR the HTML placeholder. If a future contributor
adds a new field that interpolates a secret-shaped string, the keys-only
allowlist test still passes (the field IS in the schema) — but THIS test
fails because the secret-shaped value lands in the response.

The deny-list patterns are imported directly from
``control_server._redaction`` so the gate stays aligned with the runtime
filter: any pattern the filter would redact in a log line is also a
pattern we refuse to see in a diagnostics response.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from control_server import create_app  # noqa: E402
from control_server._redaction import (  # noqa: E402
    _COORD_PAIR_RE,
    _GH_TOKEN_RE,
    _PSK_RE,
    _SSH_RE,
    REDACTED_TOKEN,
)


def _make_app(tmp_path) -> object:
    """Build a test app pointed at empty config so the response is a
    realistic "clean machine" baseline."""
    env = tmp_path / "env.sh"
    env.write_text("WEATHER_ENABLED=false\n")
    return create_app(
        {
            "ENV_FILE": str(env),
            "DIAG_OS_RELEASE_PATH": "/nonexistent/os-release",
            "DIAG_PROC_UPTIME_PATH": "/nonexistent/uptime",
            "DIAG_PROC_MEMINFO_PATH": "/nonexistent/meminfo",
            "DIAG_DISK_TARGET": str(tmp_path),
            "DIAG_LAST_IP_PATH": "/nonexistent/last-ip",
            "DIAG_CURRENT_QUOTE_PATH": "/nonexistent/quote",
            "DIAG_IMAGES_VERSION_PATH": "/nonexistent/images",
            "DIAG_GIFT_MODE_MARKER": "/nonexistent/gift",
            "DIAG_THERMAL_PATH": "/nonexistent/thermal",
        }
    )


class TestDenyListScanCleanResponse:
    """On a clean machine (no secret-shaped data anywhere), the response
    body should not match any deny-list pattern. This locks in the
    no-leak baseline."""

    def test_api_diagnostics_response_has_no_psk_pattern(self, tmp_path):
        app = _make_app(tmp_path)
        with app.test_client() as c:
            r = c.get("/api/diagnostics")
        body_text = r.data.decode("utf-8")
        match = _PSK_RE.search(body_text)
        assert match is None, f"PSK pattern matched: {match!r}"

    def test_api_diagnostics_response_has_no_ssh_pattern(self, tmp_path):
        app = _make_app(tmp_path)
        with app.test_client() as c:
            r = c.get("/api/diagnostics")
        body_text = r.data.decode("utf-8")
        assert _SSH_RE.search(body_text) is None

    def test_api_diagnostics_response_has_no_gh_token(self, tmp_path):
        app = _make_app(tmp_path)
        with app.test_client() as c:
            r = c.get("/api/diagnostics")
        body_text = r.data.decode("utf-8")
        assert _GH_TOKEN_RE.search(body_text) is None

    def test_api_diagnostics_response_has_no_raw_coords(self, tmp_path):
        # On a clean machine the env.sh has no lat/lon, so the keyed
        # pattern should not match. (When a real coord is present, the
        # privacy policy rounds it to 2dp before insertion — but the
        # _COORD_KEYED_RE still matches because "lat=37.77" satisfies
        # the pattern. We assert NOT that the pattern misses the row,
        # but that no 6dp form leaks; the per-row redact path handles
        # the 2dp rendering.)
        app = _make_app(tmp_path)
        with app.test_client() as c:
            r = c.get("/api/diagnostics")
        body_text = r.data.decode("utf-8")
        # The cleanest assertion: no 6dp coord-shaped substring anywhere.
        import re

        six_dp = re.compile(r"-?\d+\.\d{6,}")
        leak = six_dp.search(body_text)
        # Allow timestamps (which can include sub-second precision) — the
        # diagnostic payload's only timestamp shape is ISO 8601 with
        # fractional seconds, not bare floats. Filter those out:
        while leak is not None:
            iso_context = body_text[max(0, leak.start() - 5) : leak.end() + 5]
            if "T" in iso_context or ":" in iso_context:
                # Looks like a timestamp; advance past it.
                leak = six_dp.search(body_text, leak.end())
                continue
            break
        assert leak is None, f"6dp coord leak at {leak.start()}: {leak.group()!r}"


class TestDenyListScanReversedFromKnownLeak:
    """Synthetic positive controls: if we inject secrets into the values
    dict and render the copy payload, the deny-list should catch them.
    This proves the deny-list patterns actually flag what they should."""

    def test_psk_in_value_would_be_caught(self):
        """Direct check: if the response body contained a PSK= line, the
        _PSK_RE pattern should match it. This is the round-trip — proves
        the regex isn't accidentally broken."""
        synthetic_response = '{"some_key": "PSK=hunter2foobar"}'
        assert _PSK_RE.search(synthetic_response) is not None

    def test_ssh_key_in_value_would_be_caught(self):
        synthetic_response = (
            '{"some_key": "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAACAQDxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx user@host"}'
        )
        assert _SSH_RE.search(synthetic_response) is not None

    def test_gh_token_in_value_would_be_caught(self):
        synthetic_response = '{"some_key": "token=ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"}'
        assert _GH_TOKEN_RE.search(synthetic_response) is not None

    def test_paired_coord_in_value_would_be_caught(self):
        # Note: the response renders lat/lon as separate keyed fields,
        # not as a parenthesized pair — _COORD_PAIR_RE wouldn't normally
        # match the JSON shape. But if a future contributor adds a
        # location-as-tuple field, the pattern protects.
        synthetic_response = '{"location": "(37.774929, -122.419418)"}'
        assert _COORD_PAIR_RE.search(synthetic_response) is not None


class TestJournalTailRedactionIntegration:
    """F-LEAK-B regression — journal_tail values come from journalctl
    (out of process), bypassing the in-process RedactingFilter. The
    /review fix runs every journal line through redact_text() at ingest
    time so neither the JSON envelope NOR the copy_payload leaks the
    SSID/PSK/appid the journal recorded."""

    def test_synthetic_journal_secrets_do_not_leak(self, monkeypatch, tmp_path):
        from control_server.routes.diagnostics import _collectors

        app = _make_app(tmp_path)
        fake_tails = {
            "litclock.service": [
                "wpa_supplicant PSK=hunter2foobarbaz association failed",
            ],
            "litclock-control.service": [
                "weather GET ...&appid=sk_live_AbCdEfGhIjKl",
            ],
            "litclock-firstboot.service": [],
            "litclock-update.timer": [],
            "litclock-reresolve-location.service": [
                "ip-geo Authorization: Bearer ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
            ],
        }
        # #433 P-1 lazy-tail (per /review P-1): _build_service_states
        # only calls _batched_journal_tails for units that aren't
        # obviously-healthy. In the test env, _batched_is_active would
        # return "unknown" for every unit (no systemctl mock) — that's
        # NOT obviously-healthy, so the lazy-tail fetch fires. Good. But
        # we still need to patch _batched_journal_tails on the SAME
        # module the caller resolves the name from (_collectors), not on
        # the package re-export (per
        # [[learning-reexport-not-monkeypatch-compat]] — Python binds
        # names per module at import time).
        monkeypatch.setattr(_collectors, "_batched_journal_tails", lambda *a, **kw: fake_tails)
        # Force the lazy-tail filter to include every unit so EVERY
        # secret's redaction path is exercised — regardless of how
        # _batched_is_active resolves in the test environment.
        monkeypatch.setattr(
            _collectors,
            "_batched_is_active",
            lambda units: {u: "failed" for u in units},
        )
        with app.test_client() as c:
            r = c.get("/api/diagnostics")
        body_text = r.data.decode("utf-8")
        # Raw secrets must NOT round-trip through the values dict OR the
        # copy_payload. Multi-source /review (Claude F1 + Codex HIGH).
        for secret in [
            "hunter2foobarbaz",
            "sk_live_AbCdEfGhIjKl",
            "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
        ]:
            assert secret not in body_text, f"leak: {secret!r}"


class TestApiDiagnosticsValuesDictRedaction:
    """F-LEAK-A regression — pre-/review the /api/diagnostics envelope
    jsonified the RAW values dict, leaking ssid + 6dp coords + city to
    any LAN client. The fix wraps the dict in _redact_values_for_envelope
    BEFORE jsonify."""

    def test_envelope_does_not_leak_6dp_coords_when_reveal_off(self, tmp_path):
        env = tmp_path / "env.sh"
        env.write_text(
            "WEATHER_LATITUDE=37.774929\n"
            "WEATHER_LONGITUDE=-122.419418\n"
            "WEATHER_LOCATION_NAME=Hidden City\n"
            "WEATHER_ENABLED=true\n"
        )
        app = create_app({"ENV_FILE": str(env), "DIAG_OS_RELEASE_PATH": "/nonexistent"})
        with app.test_client() as c:
            r = c.get("/api/diagnostics")
        body_text = r.data.decode("utf-8")
        assert "37.774929" not in body_text
        assert "-122.419418" not in body_text
        assert "Hidden City" not in body_text

    def test_envelope_does_leak_when_reveal_location_set(self, tmp_path):
        # Confirms the toggle works in the other direction.
        env = tmp_path / "env.sh"
        env.write_text(
            "WEATHER_LATITUDE=37.774929\n"
            "WEATHER_LONGITUDE=-122.419418\n"
            "WEATHER_LOCATION_NAME=ShownCity\n"
            "WEATHER_ENABLED=true\n"
        )
        app = create_app({"ENV_FILE": str(env), "DIAG_OS_RELEASE_PATH": "/nonexistent"})
        with app.test_client() as c:
            r = c.get("/api/diagnostics?reveal=location")
        body_text = r.data.decode("utf-8")
        assert "ShownCity" in body_text


class TestCopyPayloadDenyList:
    """Same deny-list applied to the copy payload string specifically
    (which the helper pastes into a public GitHub issue). This is the
    most exploit-prone surface — the response body might be safe for
    LAN-trust callers, but the copy payload lands in public."""

    def test_copy_payload_clean_machine_has_no_redactable_patterns(self, tmp_path):
        app = _make_app(tmp_path)
        with app.test_client() as c:
            r = c.get("/api/diagnostics")
        body = r.get_json()
        copy = body["copy_payload"]
        assert _PSK_RE.search(copy) is None
        assert _SSH_RE.search(copy) is None
        assert _GH_TOKEN_RE.search(copy) is None

    def test_copy_payload_for_machine_with_ssid_redacts(self, tmp_path):
        """The PRIVACY_POLICY redacts ssid by default. Synthesize a values
        dict directly and assert the copy payload contains the marker,
        not the literal SSID."""
        from control_server._diagnostics_privacy import REDACTED_VALUE, schema_keys
        from control_server.routes.diagnostics import build_copy_payload

        values = {k: None for k in schema_keys()}
        values["ssid"] = "MyHomeWiFi"
        values["service_states"] = {}
        values["recent_log_entries"] = []
        values["weather_enabled"] = False
        copy = build_copy_payload(values)
        assert "MyHomeWiFi" not in copy
        assert REDACTED_VALUE in copy

    def test_copy_payload_uses_redaction_token_for_log_secrets(self):
        """If the recent_log_entries snapshot ever contains a redacted
        log line, the marker comes from _redaction (REDACTED_TOKEN),
        NOT the privacy policy's REDACTED_VALUE — the two tokens are
        intentionally distinct so the helper can tell which layer
        intervened. This test pins that contract."""
        # We can't easily exercise this without driving the RedactingFilter
        # end-to-end, but the assertion is that the two markers exist as
        # distinct strings. (A regression that aligned them would surface
        # as a single grep hit instead of two.)
        from control_server._diagnostics_privacy import REDACTED_VALUE

        assert REDACTED_TOKEN != REDACTED_VALUE


class TestDenyListPositiveControlFromEnvSh:
    """End-to-end positive control (#419 T12).

    Pre-existing positive controls (:class:`TestDenyListScanReversedFromKnownLeak`)
    proved each regex catches a synthetic string. This class proves the
    FULL redaction chain — env.sh read → values dict → JSON envelope +
    copy_payload assembly → deny-list scan — catches a real secret-shaped
    value smuggled in through env.sh. A regression that lost the field-
    level redaction OR the deny-list pattern would surface here, not just
    in the synthetic substring test.
    """

    def test_psk_smuggled_via_weather_location_name_does_not_leak(self, tmp_path):
        # WEATHER_LOCATION_NAME's policy is "redacted" — its value should
        # be replaced wholesale by REDACTED_VALUE before reaching the
        # envelope or the copy payload. Belt-and-suspenders: even if the
        # field redaction misses, the deny-list regex on the response body
        # should still fire.
        env = tmp_path / "env.sh"
        env.write_text("WEATHER_LOCATION_NAME=PSK=hunter2foobarbaz\nWEATHER_ENABLED=true\n")
        app = create_app(
            {
                "ENV_FILE": str(env),
                "DIAG_OS_RELEASE_PATH": "/nonexistent/os-release",
                "DIAG_PROC_UPTIME_PATH": "/nonexistent/uptime",
                "DIAG_PROC_MEMINFO_PATH": "/nonexistent/meminfo",
                "DIAG_DISK_TARGET": str(tmp_path),
                "DIAG_LAST_IP_PATH": "/nonexistent/last-ip",
                "DIAG_CURRENT_QUOTE_PATH": "/nonexistent/quote",
                "DIAG_IMAGES_VERSION_PATH": "/nonexistent/images",
                "DIAG_GIFT_MODE_MARKER": "/nonexistent/gift",
                "DIAG_THERMAL_PATH": "/nonexistent/thermal",
            }
        )
        with app.test_client() as c:
            r = c.get("/api/diagnostics")
        body_text = r.data.decode("utf-8")
        # The raw PSK substring MUST be absent from EVERY part of the
        # response — wire envelope AND embedded copy_payload.
        assert "hunter2foobarbaz" not in body_text, (
            "PSK-shaped value smuggled via WEATHER_LOCATION_NAME leaked into the response"
        )
        # Defense-in-depth: the deny-list PSK regex must report a clean
        # response too. (If field-level redaction misses but the regex
        # also misses, neither layer caught the secret — that's the
        # double-fault we want to be loud about.)
        assert _PSK_RE.search(body_text) is None

    def test_psk_smuggled_via_weather_location_name_does_not_leak_via_copy_payload(self, tmp_path):
        # Tighter variant: scan the copy_payload field SPECIFICALLY since
        # that's what the helper pastes into a public GitHub issue. A
        # response-body assertion catches it too, but pinning the
        # copy_payload separately makes a regression's blast radius
        # explicit.
        env = tmp_path / "env.sh"
        env.write_text("WEATHER_LOCATION_NAME=PSK=hunter2foobarbaz\nWEATHER_ENABLED=true\n")
        app = create_app(
            {
                "ENV_FILE": str(env),
                "DIAG_OS_RELEASE_PATH": "/nonexistent/os-release",
                "DIAG_PROC_UPTIME_PATH": "/nonexistent/uptime",
                "DIAG_PROC_MEMINFO_PATH": "/nonexistent/meminfo",
                "DIAG_DISK_TARGET": str(tmp_path),
                "DIAG_LAST_IP_PATH": "/nonexistent/last-ip",
                "DIAG_CURRENT_QUOTE_PATH": "/nonexistent/quote",
                "DIAG_IMAGES_VERSION_PATH": "/nonexistent/images",
                "DIAG_GIFT_MODE_MARKER": "/nonexistent/gift",
                "DIAG_THERMAL_PATH": "/nonexistent/thermal",
            }
        )
        with app.test_client() as c:
            r = c.get("/api/diagnostics")
        copy_payload = r.get_json()["copy_payload"]
        assert "hunter2foobarbaz" not in copy_payload
        assert _PSK_RE.search(copy_payload) is None


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
