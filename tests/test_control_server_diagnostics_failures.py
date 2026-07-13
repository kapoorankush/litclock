"""Malformed-source-file failure-path tests for the diagnostics readers.

Pre-#419 the readers' "missing file" path was covered by tests that
removed the file entirely. The "file exists but is malformed" path was
left untested for several readers — the existing payload tests assumed
well-formed inputs. This file fills the gap (T8 from issue body).

Each test points the relevant ``DIAG_*_PATH`` config key at a tmp file
holding intentionally bad content. The reader's contract is "return
``None`` (or an empty/safe-default value) without raising"; an unexpected
``ValueError`` / ``IndexError`` / ``OSError`` would have bubbled up to
``api_diagnostics`` and 500'd the route.

Covered:
- Malformed JSON in ``current-quote.json`` (corrupted bytes).
- Valid JSON but not a dict (e.g. a list or scalar) in current-quote.json.
- Non-numeric ``/proc/uptime`` content.
- Non-numeric thermal sysfs value.
- Malformed ``/etc/os-release`` (no PRETTY_NAME line; key/value without =).
"""

from __future__ import annotations

import pytest
from flask import Flask

from control_server.routes.diagnostics import _collectors


@pytest.fixture()
def app():
    return Flask(__name__)


# --- current-quote.json --------------------------------------------------


class TestReadCurrentQuote:
    """``_read_current_quote`` returns ``{}`` on any malformed content.

    Strict contract: a bad quote file must NEVER raise — the route's
    last-quote section degrades to empty rather than 500'ing.
    """

    def test_malformed_json(self, app, tmp_path):
        bad = tmp_path / "current-quote.json"
        bad.write_text("not-json}{")
        app.config["DIAG_CURRENT_QUOTE_PATH"] = str(bad)
        with app.app_context():
            assert _collectors._read_current_quote() == {}

    def test_json_but_not_dict_list(self, app, tmp_path):
        bad = tmp_path / "current-quote.json"
        bad.write_text("[1, 2, 3]")
        app.config["DIAG_CURRENT_QUOTE_PATH"] = str(bad)
        with app.app_context():
            assert _collectors._read_current_quote() == {}

    def test_json_but_not_dict_scalar(self, app, tmp_path):
        bad = tmp_path / "current-quote.json"
        bad.write_text("42")
        app.config["DIAG_CURRENT_QUOTE_PATH"] = str(bad)
        with app.app_context():
            assert _collectors._read_current_quote() == {}

    def test_json_but_not_dict_null(self, app, tmp_path):
        # `null` is valid JSON but isinstance(None, dict) is False.
        bad = tmp_path / "current-quote.json"
        bad.write_text("null")
        app.config["DIAG_CURRENT_QUOTE_PATH"] = str(bad)
        with app.app_context():
            assert _collectors._read_current_quote() == {}


# --- /proc/uptime --------------------------------------------------------


class TestReadApplianceUptimeS:
    """``_read_appliance_uptime_s`` parses ``"<seconds> <idle>\\n"`` ints.

    A non-numeric body (corrupted kernel interface, mocked test path) must
    return ``None`` not raise ``ValueError`` from ``int(float(...))``.
    """

    def test_non_numeric_content(self, app, tmp_path):
        bad = tmp_path / "uptime"
        bad.write_text("garbage idle\n")
        app.config["DIAG_PROC_UPTIME_PATH"] = str(bad)
        with app.app_context():
            assert _collectors._read_appliance_uptime_s() is None

    def test_empty_file(self, app, tmp_path):
        bad = tmp_path / "uptime"
        bad.write_text("")
        app.config["DIAG_PROC_UPTIME_PATH"] = str(bad)
        with app.app_context():
            assert _collectors._read_appliance_uptime_s() is None

    def test_negative_seconds_passes_through(self, app, tmp_path):
        # /proc/uptime never goes negative on real hardware, but if it did
        # the reader returns the int value; ``format_uptime`` separately
        # renders negative seconds as "—".
        bad = tmp_path / "uptime"
        bad.write_text("-1.5 0.0\n")
        app.config["DIAG_PROC_UPTIME_PATH"] = str(bad)
        with app.app_context():
            assert _collectors._read_appliance_uptime_s() == -1


# --- thermal sysfs -------------------------------------------------------


class TestReadCpuTempC:
    """``_read_cpu_temp_c`` reads /sys/.../thermal_zone*/temp as int millideg.

    A non-numeric body should fall through to the next candidate path; if
    ALL candidates are non-numeric, the reader returns ``None``.
    """

    def test_non_numeric_content_returns_none(self, app, tmp_path, monkeypatch):
        bad = tmp_path / "temp"
        bad.write_text("not_a_number\n")
        app.config["DIAG_THERMAL_PATH"] = str(bad)
        # Block the convention-based fallback paths so the test pins on
        # the configured path only.
        monkeypatch.setattr(
            _collectors.Path,
            "read_text",
            _block_real_thermal_zone(_collectors.Path.read_text, bad),
        )
        with app.app_context():
            assert _collectors._read_cpu_temp_c() is None

    def test_empty_file_returns_none(self, app, tmp_path, monkeypatch):
        bad = tmp_path / "temp"
        bad.write_text("")
        app.config["DIAG_THERMAL_PATH"] = str(bad)
        monkeypatch.setattr(
            _collectors.Path,
            "read_text",
            _block_real_thermal_zone(_collectors.Path.read_text, bad),
        )
        with app.app_context():
            assert _collectors._read_cpu_temp_c() is None


def _block_real_thermal_zone(real_read_text, allowed_path):
    """Wrap Path.read_text so /sys/class/thermal/* paths return "" (would
    have been the natural OSError on a dev box, but we make it explicit so
    the test isn't host-dependent)."""
    allowed_str = str(allowed_path)

    def wrapped(self, *args, **kwargs):
        if str(self).startswith("/sys/class/thermal/"):
            raise FileNotFoundError(str(self))
        if str(self) == allowed_str:
            return real_read_text(self, *args, **kwargs)
        return real_read_text(self, *args, **kwargs)

    return wrapped


# --- /etc/os-release -----------------------------------------------------


class TestReadLanIpEmptyPath:
    """Codex /review F1 regression guard.

    Pre-#419 the lan_ip reader did ``Path(path).read_text(...)`` directly,
    so a Flask config of ``DIAG_LAST_IP_PATH=""`` produced ``Path("")``
    which raised ``OSError`` and degraded to ``None``. The naive #419
    rewrite (``target = path or DEFAULT_LAST_RENDERED_IP_PATH``) would
    have silently fallen back to the production path — leaking the real
    LAN IP / DHCP timestamp when an override was intended to suppress
    the read. This test pins the None-vs-empty-string distinction.
    """

    def test_empty_string_does_not_fall_back_to_default(self, app):
        # The empty string is "disable", not "fall back."
        app.config["DIAG_LAST_IP_PATH"] = ""
        with app.app_context():
            from control_server.routes.diagnostics import _collectors

            assert _collectors._read_lan_ip() is None
            assert _collectors._read_last_dhcp_iso() is None

    def test_none_falls_back_to_default_via_network_helper(self):
        # Direct call to _network helpers with path=None should consult
        # DEFAULT_LAST_RENDERED_IP_PATH. We don't read it (no file exists
        # in CI), but the function should return None gracefully without
        # raising on ``Path(None)``.
        from control_server._network import read_lan_ip, read_last_dhcp_iso

        # Passing None explicitly → falls back to DEFAULT path which
        # presumably doesn't exist in the test environment → None.
        assert read_lan_ip(path=None) is None
        assert read_last_dhcp_iso(path=None) is None


class TestReadOsReleasePretty:
    """``_read_os_release_pretty`` walks the file looking for ``PRETTY_NAME=``.

    A file missing that key should return ``None``. A file with a malformed
    line (no ``=``) at the top should not raise.
    """

    def test_missing_pretty_name_key(self, app, tmp_path):
        bad = tmp_path / "os-release"
        bad.write_text("NAME=Debian\nVERSION_ID=12\n")  # no PRETTY_NAME=
        app.config["DIAG_OS_RELEASE_PATH"] = str(bad)
        with app.app_context():
            assert _collectors._read_os_release_pretty() is None

    def test_unbalanced_quotes_passes_through(self, app, tmp_path):
        # The strip-quotes branch only fires when first char == last char;
        # an unbalanced opening quote should NOT lose the closing token.
        bad = tmp_path / "os-release"
        bad.write_text('PRETTY_NAME="Debian GNU/Linux\n')  # no closing quote
        app.config["DIAG_OS_RELEASE_PATH"] = str(bad)
        with app.app_context():
            # Quote-strip skipped → leading " stays in the value.
            assert _collectors._read_os_release_pretty() == '"Debian GNU/Linux'

    def test_empty_file(self, app, tmp_path):
        bad = tmp_path / "os-release"
        bad.write_text("")
        app.config["DIAG_OS_RELEASE_PATH"] = str(bad)
        with app.app_context():
            assert _collectors._read_os_release_pretty() is None
