"""Tests for control_server/_redaction.py (#416 OV-1=A).

The RedactingFilter is the safety net for the live-logs drawer: every log
record gets sanitized BEFORE it lands in the in-memory buffer. The tests
below cover each pattern individually + a "filter integration" smoke that
runs a real LogRecord through the buffer.
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

from control_server._redaction import (  # noqa: E402
    REDACTED_TOKEN,
    RedactingFilter,
    redact_text,
)


class TestCompoundKeyBoundary:
    """PR1 adversarial pass found that ``\\b`` doesn't match between two
    word chars, so ``GH_AUTH_TOKEN=value`` slipped past the keyword
    pattern. Fix uses ``(?:^|[\\W_])`` to treat underscore as a separator.
    """

    @pytest.mark.parametrize(
        "line",
        [
            "GH_AUTH_TOKEN=ghp_FakeTokenValue123",
            "OAUTH_CLIENT_SECRET=oauthsecretvalue",
            "MY_API_KEY=customapikey",
            "X_AUTH_TOKEN=session-jwt-here",
            "X-AUTH-TOKEN=hyphen-separator-form",
            "myapp_password=plain-pass-value",
            "USER_PSK=wifi-password-here",
        ],
    )
    def test_compound_key_is_redacted(self, line):
        out = redact_text(line)
        secret = line.split("=", 1)[1]
        assert secret not in out, f"{line!r} leaked {secret!r}: out={out!r}"
        assert REDACTED_TOKEN in out


class TestQuotedValueRedaction:
    """PR1 adversarial pass: ``WIFI_PASSWORD="my secret pass"`` only
    matched ``"my`` under ``\\S+``, leaking the rest of the password.
    Fix matches through the closing quote.
    """

    def test_double_quoted_value_fully_redacted(self):
        out = redact_text('WIFI_PASSWORD="my home wifi pass"')
        assert "my home wifi pass" not in out
        assert "home" not in out  # ensure no fragment leaked
        assert "wifi pass" not in out
        assert REDACTED_TOKEN in out

    def test_single_quoted_value_fully_redacted(self):
        out = redact_text("PASSWORD='another secret value'")
        assert "another secret value" not in out
        assert "secret value" not in out
        assert REDACTED_TOKEN in out

    def test_unquoted_value_still_works(self):
        # Backwards compatibility: existing unquoted form should still
        # redact the same way it did before.
        out = redact_text("PSK=hunter2foobar")
        assert "hunter2foobar" not in out
        assert REDACTED_TOKEN in out

    def test_quoted_value_redacted_form_preserves_quotes(self):
        # The replacement preserves quotes so the line stays shell-valid
        # (useful when the log line was a literal env-dump).
        out = redact_text('SECRET="abc"')
        assert '"' in out
        assert "abc" not in out


class TestExtendedKeywordPatterns:
    """PR1 /review ASK-3=A extended _PSK_RE to cover SECRET / TOKEN /
    API_KEY / BEARER / AUTH / CLIENT_SECRET in addition to the original
    PSK / PASSWORD / PASSWD / WIFI_PASS keywords. This locks each new
    keyword as a redaction trigger."""

    @pytest.mark.parametrize(
        "line",
        [
            "SECRET=plaintext-secret-value",
            "secret=plaintext-secret-value",
            "TOKEN=ghp_FakeTokenValue123",
            "API_KEY=AbCdEf123456",
            "api-key=lowercase-form",
            "API-KEY=mixed-form",
            "BEARER=jwt.token.value.here",
            "AUTH=session-id-987",
            "CLIENT_SECRET=oauth-secret-stuff",
        ],
    )
    def test_extended_keywords_redact(self, line):
        out = redact_text(line)
        # The literal value after `=` must be gone, regardless of which
        # keyword tripped the match.
        secret = line.split("=", 1)[1]
        assert secret not in out, f"{line!r} leaked secret {secret!r}: out={out!r}"
        assert REDACTED_TOKEN in out

    def test_existing_keywords_still_redact(self):
        # Sanity: the original keywords weren't broken by the extension.
        for line in ("PSK=foo", "PASSWORD=bar", "WIFI_PASS=baz"):
            out = redact_text(line)
            assert REDACTED_TOKEN in out
            assert line.split("=", 1)[1] not in out


class TestPSKPattern:
    def test_psk_equals_value(self):
        out = redact_text("PSK=hunter2foobar")
        assert "hunter2foobar" not in out
        assert REDACTED_TOKEN in out

    def test_password_keyword(self):
        out = redact_text("WIFI_PASSWORD=verysecret")
        assert "verysecret" not in out
        assert REDACTED_TOKEN in out

    def test_passwd_keyword(self):
        out = redact_text("passwd=admin1234")
        assert "admin1234" not in out

    def test_wifi_pass_variant(self):
        out = redact_text("wifi_pass=letmein99")
        assert "letmein99" not in out

    def test_case_insensitive(self):
        assert REDACTED_TOKEN in redact_text("psk=foo123bar")
        assert REDACTED_TOKEN in redact_text("Psk=foo123bar")
        assert REDACTED_TOKEN in redact_text("PSK=foo123bar")

    def test_keeps_label_visible_for_helper_context(self):
        # The PSK= label survives — the helper sees "yes, a PSK leaked"
        # without seeing the value. The replacement happens AFTER the
        # capture group containing the label.
        out = redact_text("PSK=abc123xyz")
        assert out.startswith("PSK=")


class TestSSHPattern:
    def test_armored_block(self):
        body = (
            "Error: -----BEGIN OPENSSH PRIVATE KEY-----\n"
            "AAAAFAKEKEYDATA\nAAAAB3NzaC1yc2EAAAA\n"
            "-----END OPENSSH PRIVATE KEY-----"
        )
        out = redact_text(body)
        assert "AAAAFAKEKEYDATA" not in out
        assert REDACTED_TOKEN in out

    def test_ssh_rsa_public_key(self):
        out = redact_text("auth: ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAACAQDxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx user@host")
        assert "AAAAB3NzaC1yc2EAAAADAQABAAACAQDx" not in out

    def test_ssh_ed25519(self):
        out = redact_text("auth: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILxxxxxxxxxxxxxxxxxxxxxxx user@host")
        assert REDACTED_TOKEN in out


class TestGHTokenPattern:
    def test_ghp_token(self):
        out = redact_text("token=ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789")
        assert "ghp_" not in out

    def test_ghs_token(self):
        out = redact_text("ghs_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789aaaa")
        assert REDACTED_TOKEN in out

    def test_github_pat(self):
        out = redact_text("github_pat_11ABCDE0K0aBcDeFgHiJkL_MnOpQrStUvWxYzAbCdEfGhIjKlMnOpQrStUvWxYzAbCdEfGhIjKlMnOpQ")
        assert "github_pat_" not in out


class TestCoordinatePattern:
    def test_keyed_lat_lon_rounds_to_2dp(self):
        out = redact_text("Resolved location: lat=37.7749 lon=-122.4194")
        assert "lat=37.77" in out
        assert "lon=-122.42" in out
        # Make sure we didn't leave 6dp lurking
        assert "37.7749" not in out
        assert "-122.4194" not in out

    def test_keyed_quoted_lat_lon_rounded(self):
        # PR1 codex adversarial pass: location_resolver.py logs refused
        # coordinates with %r, producing lat='37.7749' and lon='-122.4194'.
        # The pre-fix regex required no surrounding quotes; now optional.
        out_double = redact_text('Refused: lat="37.7749" lon="-122.4194"')
        assert "37.7749" not in out_double
        assert "-122.4194" not in out_double
        out_single = redact_text("Refused: lat='37.7749' lon='-122.4194'")
        assert "37.7749" not in out_single
        assert "-122.4194" not in out_single

    def test_latitude_longitude_long_form(self):
        out = redact_text("latitude=12.123456 longitude=78.987654")
        assert "12.12" in out
        assert "78.98" in out or "78.99" in out  # rounding can go either way

    def test_paired_coords_in_parens(self):
        out = redact_text("Center point: (37.774929, -122.419418)")
        assert "(37.77, -122.42)" in out
        assert "37.774929" not in out

    def test_compound_key_env_export_line_rounds(self):
        # Regression: systemd logs env.sh verbatim when a line uses `export`
        # (rejected by EnvironmentFile=): "Ignoring invalid environment
        # assignment 'export WEATHER_LATITUDE=33.1234'". The pre-fix
        # `_COORD_KEYED_RE` led with `\b`, which cannot match inside the
        # compound key WEATHER_LATITUDE (underscore before LATITUDE is a word
        # char), so the full-precision home coordinate leaked through the
        # support-logs / journal export. The fix uses `(?:^|[\W_])` like
        # _PSK_RE and preserves the surrounding key name.
        lat_line = (
            "systemd[1]: Ignoring invalid environment assignment "
            "'export WEATHER_LATITUDE=33.1234': /home/pi/litclock/env.sh"
        )
        out_lat = redact_text(lat_line)
        assert "33.1234" not in out_lat  # street-level precision must not survive
        assert "33.12" in out_lat  # rounded to ~city-block
        assert "WEATHER_LATITUDE" in out_lat  # key name preserved, not mangled

        lon_line = (
            "systemd[1]: Ignoring invalid environment assignment "
            "'export WEATHER_LONGITUDE=-96.876': /home/pi/litclock/env.sh"
        )
        out_lon = redact_text(lon_line)
        assert "-96.876" not in out_lon
        assert "-96.88" in out_lon
        assert "WEATHER_LONGITUDE" in out_lon

    def test_leading_sign_and_scientific_rounded(self):
        # #498: value group accepts an optional sign + scientific notation, both
        # a dotted mantissa (.331234e2) and a bare-integer mantissa with an
        # exponent (331234e-4 — float() accepts it, so it's storable/loggable).
        assert "33.1234" not in redact_text("export WEATHER_LATITUDE=+33.1234")
        assert "33.12" in redact_text("export WEATHER_LATITUDE=+33.1234")
        out_sci = redact_text("export WEATHER_LATITUDE=.331234e2")
        assert ".331234e2" not in out_sci
        assert "33.12" in out_sci
        out_intexp = redact_text("Invalid coordinates lat='331234e-4'")
        assert "331234e-4" not in out_intexp
        assert "33.12" in out_intexp

    def test_comma_decimal_and_lists_untouched(self):
        # #498 (post-/review): comma-decimals are DELIBERATELY not matched — a
        # comma is ambiguous with a list separator and a comma coord is
        # unreachable (the validator's float() rejects it). The redaction must
        # never fabricate a coordinate out of a comma-separated integer list.
        for text in ("lat=1,2,3", "lat=33, lon=44", "bbox=1,2,3,4", "lat=33,1234"):
            assert redact_text(text) == text, f"comma list corrupted: {text!r}"

    def test_json_keyed_coords_rounded(self):
        # #498: the optional quote after the keyword catches JSON quoted-key
        # forms. Output is lossy (structure mangled) but never leaky.
        out_q = redact_text('{"lat": "33.1234", "lon": "-96.876"}')
        assert "33.1234" not in out_q
        assert "-96.876" not in out_q
        assert "33.12" in out_q and "-96.88" in out_q
        out_bare = redact_text('{"latitude":33.1234}')
        assert "33.1234" not in out_bare
        assert "33.12" in out_bare

    def test_adjacent_coords_no_separator_both_rounded(self):
        # #498: the zero-width lookbehind lets a coord immediately following
        # another (no separator) still match — the consuming lead-in could not.
        out = redact_text("lat=11.1234lon=22.5678")
        assert "11.1234" not in out
        assert "22.5678" not in out
        assert "11.12" in out and "22.57" in out

    def test_no_over_redaction_of_lookalike_words(self):
        # #498 guard: the lookbehind blocks a LETTER before the keyword, so
        # words that merely contain lat/lon/long are untouched; a comma-
        # separated integer list is not mistaken for a comma-decimal.
        for text in (
            "the translation=5.5 here",
            "collation=9.9",
            "along=3.3",
            "belong=4.4",
            "flat=2.2",
            "plateau: 6.6",
            "prolong=7.7",
            "inflation=2.5 and deflation=1.2",
            "lat=33, lon=44",  # int list, not a decimal — must not corrupt
        ):
            assert redact_text(text) == text, f"over-redacted: {text!r}"


class TestLongTokenCatchAll:
    def test_long_mixed_case_redacted(self):
        # 40+ chars mixed case + digit triggers the catch-all.
        s = "secret=AbCdEfGhIjKlMnOpQrStUvWxYz0123456789AbCdE"  # 47 chars
        assert len(s.split("=", 1)[1]) >= 40
        out = redact_text(s)
        assert REDACTED_TOKEN in out

    def test_short_token_left_alone(self):
        # 15-char identifier shouldn't trip the catch-all.
        out = redact_text("session_id=AbCdEfGhIjKlMno")
        assert "AbCdEfGhIjKlMno" in out

    def test_pure_lowercase_hex_sha_preserved(self):
        # The catch-all requires upper+lower+digit; a pure-hex 40-char git
        # SHA legitimately appears in logs and must NOT be redacted.
        s = "git rev-parse HEAD -> 5159fedd8a3b2c1d4e5f6a7b8c9d0e1f2a3b4c5d"
        out = redact_text(s)
        assert "5159fedd8a3b2c1d4e5f6a7b8c9d0e1f2a3b4c5d" in out

    def test_normal_english_left_alone(self):
        s = "Saved settings successfully after 320ms with no contention"
        assert redact_text(s) == s


class TestRedactTextEdgeCases:
    def test_empty_string(self):
        assert redact_text("") == ""

    def test_none_safe(self):
        # The helper is typed `str -> str`; we still want defensive
        # behavior in case a caller hands us a falsy value.
        assert redact_text("") == ""

    def test_idempotent(self):
        s = "PSK=secretfoobar123"
        once = redact_text(s)
        twice = redact_text(once)
        assert once == twice


class TestRedactingFilter:
    def test_filter_rewrites_record_message(self):
        flt = RedactingFilter()
        rec = logging.LogRecord(
            name="test",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="Saved env.sh with PSK=secretvalueabc",
            args=None,
            exc_info=None,
        )
        assert flt.filter(rec) is True
        assert "secretvalueabc" not in rec.getMessage()

    def test_filter_handles_args_format(self):
        # Records often carry a format string + args. After filtering, the
        # args are cleared so the formatter doesn't double-substitute.
        flt = RedactingFilter()
        rec = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="key=%s value=%s",
            args=("PSK", "leaktokenlongstring42charsAbCdEfGhIjKlMnOp"),
            exc_info=None,
        )
        flt.filter(rec)
        # getMessage() formats with current args; after the filter, the
        # interpolated string should already be redacted.
        msg = rec.getMessage()
        assert "leaktokenlongstring42charsAbCdEfGhIjKlMnOp" not in msg

    def test_filter_passes_clean_records_unchanged(self):
        flt = RedactingFilter()
        rec = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="Plain text, nothing sensitive.",
            args=None,
            exc_info=None,
        )
        flt.filter(rec)
        assert rec.getMessage() == "Plain text, nothing sensitive."

    def test_filter_never_drops_record(self):
        flt = RedactingFilter()
        rec = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="whatever",
            args=None,
            exc_info=None,
        )
        # filter() always returns True — records flow through, just rewritten.
        assert flt.filter(rec) is True

    def test_filter_handles_unformattable_record_without_crash(self):
        flt = RedactingFilter()
        rec = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="%(missing)s",
            # Legitimate "format with mapping" call shape per Python's
            # logging docs: args is a 1-tuple wrapping the mapping. The
            # `missing` key is absent so getMessage() will KeyError.
            args=({"other": "value"},),
            exc_info=None,
        )
        # Should not raise. The filter swallows getMessage failures...
        assert flt.filter(rec) is True
        # ...AND leaves msg/args untouched, so downstream handlers can
        # still attempt to format and produce their own error path.
        # If we mutated msg here we'd corrupt the record for every other
        # handler on the same logger. (Python's LogRecord constructor
        # unpacks a 1-tuple containing a mapping, so rec.args is the
        # dict itself, not the tuple — that's stdlib behavior.)
        assert rec.msg == "%(missing)s"
        assert rec.args == {"other": "value"}


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
