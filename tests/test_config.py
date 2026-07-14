"""Tests for src/config.py — env.sh single-source-of-truth.

Covers:
- load_config: parse, missing file, comments, blank lines, quoted values, export prefix.
- validate_setting: each known key, invalid values, unknown key.
- atomic_update: write, ownership preserved, fail-fast on bad value, tempfile
  cleanup on error, comment/export-prefix preservation, append for missing keys.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make src/ importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config  # noqa: E402

# ---------- load_config ----------


def test_load_config_basic(tmp_path: Path) -> None:
    env = tmp_path / "env.sh"
    env.write_text("WEATHER_LATITUDE=30.27\nWEATHER_LONGITUDE=-97.74\nWEATHER_UNITS=imperial\n")
    cfg = config.load_config(env)
    assert cfg == {
        "WEATHER_LATITUDE": "30.27",
        "WEATHER_LONGITUDE": "-97.74",
        "WEATHER_UNITS": "imperial",
    }


def test_load_config_missing_file_returns_empty(tmp_path: Path) -> None:
    assert config.load_config(tmp_path / "absent.sh") == {}


def test_load_config_skips_comments_and_blanks(tmp_path: Path) -> None:
    env = tmp_path / "env.sh"
    env.write_text("# leading comment\n\nWEATHER_UNITS=metric\n  # indented comment\nALLOW_NSFW_QUOTES=false\n")
    assert config.load_config(env) == {
        "WEATHER_UNITS": "metric",
        "ALLOW_NSFW_QUOTES": "false",
    }


def test_load_config_strips_quotes(tmp_path: Path) -> None:
    env = tmp_path / "env.sh"
    env.write_text("WEATHER_UNITS=\"imperial\"\nALLOW_NSFW_QUOTES='false'\n")
    cfg = config.load_config(env)
    assert cfg["WEATHER_UNITS"] == "imperial"
    assert cfg["ALLOW_NSFW_QUOTES"] == "false"


def test_load_config_handles_export_prefix(tmp_path: Path) -> None:
    env = tmp_path / "env.sh"
    env.write_text("export WEATHER_LATITUDE=12.34\nWEATHER_LONGITUDE=56.78\n")
    cfg = config.load_config(env)
    assert cfg["WEATHER_LATITUDE"] == "12.34"
    assert cfg["WEATHER_LONGITUDE"] == "56.78"


def test_load_config_skips_unrecognized_lines(tmp_path: Path) -> None:
    env = tmp_path / "env.sh"
    env.write_text(
        "WEATHER_UNITS=imperial\n"
        'if [ -n "$FOO" ]; then\n'  # multi-line shell, ignored
        "  echo hi\n"
        "fi\n"
        "ALLOW_NSFW_QUOTES=true\n"
    )
    assert config.load_config(env) == {
        "WEATHER_UNITS": "imperial",
        "ALLOW_NSFW_QUOTES": "true",
    }


# ---------- validate_setting ----------


@pytest.mark.parametrize(
    "key,value",
    [
        ("WEATHER_LATITUDE", "30.27"),
        ("WEATHER_LATITUDE", "-89.999"),
        ("WEATHER_LATITUDE", "0"),
        # #325 — empty string is a valid "unset" state for lat/lon (matches
        # env.sh.sample's shipped empty WEATHER_LOCATION_NAME=). The PWA's
        # Clear weather location affordance relies on this.
        ("WEATHER_LATITUDE", ""),
        ("WEATHER_LONGITUDE", "-180"),
        ("WEATHER_LONGITUDE", "180"),
        ("WEATHER_LONGITUDE", "-97.74"),
        ("WEATHER_LONGITUDE", ""),
        ("WEATHER_UNITS", "imperial"),
        ("WEATHER_UNITS", "metric"),
        ("ALLOW_NSFW_QUOTES", "true"),
        ("ALLOW_NSFW_QUOTES", "false"),
        ("ALLOW_NSFW_QUOTES", "TRUE"),  # case-insensitive
    ],
)
def test_validate_setting_passes_valid(key: str, value: str) -> None:
    ok, err = config.validate_setting(key, value)
    assert ok is True, err
    assert err is None


@pytest.mark.parametrize(
    "key,value,expected_err_substr",
    [
        ("WEATHER_LATITUDE", "abc", "numeric"),
        ("WEATHER_LATITUDE", "91", "between"),
        ("WEATHER_LATITUDE", "-91", "between"),
        ("WEATHER_LONGITUDE", "abc", "numeric"),
        ("WEATHER_LONGITUDE", "181", "between"),
        ("WEATHER_LONGITUDE", "-181", "between"),
        ("WEATHER_UNITS", "fahrenheit", "imperial"),
        ("WEATHER_UNITS", "../../tmp/pwn", "imperial"),  # path traversal
        ("WEATHER_UNITS", "", "imperial"),
        ("ALLOW_NSFW_QUOTES", "yes", "true"),
        ("ALLOW_NSFW_QUOTES", "1", "true"),
    ],
)
def test_validate_setting_rejects_invalid(key: str, value: str, expected_err_substr: str) -> None:
    ok, err = config.validate_setting(key, value)
    assert ok is False
    assert err is not None and expected_err_substr in err


def test_validate_setting_rejects_unknown_key() -> None:
    ok, err = config.validate_setting("ARBITRARY_KEY", "anything")
    assert ok is False
    assert "unknown" in (err or "")


# ---------- M3 new validators (D7) ----------


@pytest.mark.parametrize(
    "value",
    [
        "",
        "Hello",
        "Happy birthday, Mum.",
        "O'Brien said hi",
        # #319: newlines now accepted so multi-line welcome messages survive
        # end-to-end (validator → env.sh → .welcome-message → renderer).
        "Happy Birthday\nMom!",
        "Line one\nLine two\nLine three",
    ],
)
def test_validate_gift_mode_message_passes_valid(value: str) -> None:
    ok, err = config.validate_setting("GIFT_MODE_MESSAGE", value)
    assert ok is True, err


@pytest.mark.parametrize(
    "value,err_substr",
    [
        # #319: cap dropped from 280 → 80 once the renderer learned to wrap.
        ("a" * 81, "80 characters"),
        ("hi $(whoami)", "may not contain"),
        ("`whoami`", "may not contain"),
        ("nul\x00byte", "NUL"),
    ],
)
def test_validate_gift_mode_message_rejects_bad(value: str, err_substr: str) -> None:
    ok, err = config.validate_setting("GIFT_MODE_MESSAGE", value)
    assert ok is False
    assert err is not None and err_substr in err


def test_gift_mode_message_rejects_emoji_at_codepoint_limit_over_byte_limit() -> None:
    """#317 item 3 (UTF-8 byte/codepoint parity).

    The validator caps GIFT_MODE_MESSAGE at 80 CODEPOINTS, but the consumer
    in ``scripts/reset-setup.sh`` reads 80 BYTES via ``os.read(fd, 80)``.
    A 4-byte emoji repeated 80 times sits at the codepoint cap (passes the
    old check) but balloons to 320 bytes — the byte read would slice
    mid-codepoint and ship invalid UTF-8 to the e-ink welcome splash.
    The new byte cap must reject it at the validator so the parity gap is
    closed at the source for both the API path and the form path."""
    emoji = "\U0001f381"  # gift-box, U+1F381, 4 bytes in UTF-8
    msg = emoji * config.GIFT_MODE_MESSAGE_MAX_LEN  # 80 codepoints, 320 bytes
    assert len(msg) == config.GIFT_MODE_MESSAGE_MAX_LEN
    assert len(msg.encode("utf-8")) == 4 * config.GIFT_MODE_MESSAGE_MAX_LEN
    ok, err = config.validate_setting("GIFT_MODE_MESSAGE", msg)
    assert ok is False, "emoji-heavy 80-codepoint message must be rejected (320 bytes > 80 byte cap)"
    assert err is not None and "bytes" in err


def test_gift_mode_message_accepts_ascii_at_byte_limit() -> None:
    """Regression net for #317 item 3: the byte cap must not reject
    well-formed ASCII messages that sit at the byte limit. ASCII users
    were the entire happy path pre-fix and must stay accepted."""
    msg = "a" * config.GIFT_MODE_MESSAGE_MAX_LEN  # 80 codepoints, 80 bytes
    ok, err = config.validate_setting("GIFT_MODE_MESSAGE", msg)
    assert ok is True, err


def test_gift_mode_message_accepts_single_emoji() -> None:
    """Regression net for #317 item 3: a single emoji (1 codepoint, up to
    4 bytes) is well under both caps and must still pass. Guards against
    over-correcting the fix into "reject all non-ASCII"."""
    ok, err = config.validate_setting("GIFT_MODE_MESSAGE", "\U0001f381")
    assert ok is True, err


def test_gift_mode_message_accepts_short_mixed_unicode() -> None:
    """A realistic gifter message with a few emoji and accented characters
    must still be accepted — its byte length is small relative to the cap."""
    # 17 codepoints; "é" is 2 bytes, emoji is 4 bytes → 17 + 1 + 3 = 21 bytes.
    msg = "Bonne fête, Papa \U0001f381"
    assert len(msg.encode("utf-8")) <= config.GIFT_MODE_MESSAGE_MAX_LEN
    ok, err = config.validate_setting("GIFT_MODE_MESSAGE", msg)
    assert ok is True, err


def test_gift_mode_message_rejects_message_just_over_byte_limit() -> None:
    """Edge: a message whose codepoint count is at-or-under the cap but
    whose byte count is exactly one byte over must be rejected with the
    byte-cap error message (not the character-cap error message — the
    codepoint check passes here, so we're exercising the second guard)."""
    # 79 ASCII chars + one 2-byte char → 80 codepoints, 81 bytes.
    msg = ("a" * (config.GIFT_MODE_MESSAGE_MAX_LEN - 1)) + "é"
    assert len(msg) == config.GIFT_MODE_MESSAGE_MAX_LEN  # codepoint check would pass
    assert len(msg.encode("utf-8")) == config.GIFT_MODE_MESSAGE_MAX_LEN + 1
    ok, err = config.validate_setting("GIFT_MODE_MESSAGE", msg)
    assert ok is False
    assert err is not None and "bytes" in err


def test_gift_mode_message_rejects_lone_surrogate_cleanly() -> None:
    """#317 item 3 codex follow-up (P2): a JSON payload like
    ``{"message": "\\ud800"}`` is a valid Python ``str`` (Flask's JSON
    decoder produces lone surrogates from such input on at least some
    paths). The validator's ``.encode("utf-8")`` then raises
    ``UnicodeEncodeError`` — without an explicit catch, this propagates
    out of ``validate_setting`` as a 500 from the route. On
    ``/api/system/prepare-for-gift`` that's the same trap class #328 was
    supposed to close: the confirm token is consumed BEFORE the validator
    runs, so a 500 here costs the user the token too. The validator must
    convert the encode error to a clean (False, "...") result instead."""
    msg = "\ud800"  # lone high surrogate — not encodable to UTF-8
    ok, err = config.validate_setting("GIFT_MODE_MESSAGE", msg)
    assert ok is False, "lone-surrogate message must be rejected cleanly (no UnicodeEncodeError)"
    assert err is not None and "UTF-8" in err


def test_weather_location_name_accepts_codepoint_cap_with_non_ascii() -> None:
    """#317 item 3 codex follow-up (P3 — scope): the byte cap is scoped to
    ``GIFT_MODE_MESSAGE`` because that key has a byte-bound consumer
    (``reset-setup.sh`` reads bytes). ``WEATHER_LOCATION_NAME`` does NOT —
    its only downstream is env.sh round-trip via shlex.quote, which is
    bytes-agnostic, and ``_short_location_name`` truncates by codepoints,
    not bytes. Applying the gift-mode byte cap project-wide would
    silently reject legitimate non-ASCII geocoded names that sit at the
    codepoint cap but exceed it in bytes (e.g. a Nominatim cascade like
    ``"São Paulo"`` repeated near the 120-codepoint cap).

    This test pins the scoping: a 120-codepoint non-ASCII weather
    location whose byte length exceeds 120 bytes must still be ACCEPTED.
    Would have failed under the over-broad PR #350 v1 implementation."""
    # 120 × "é" = 120 codepoints, 240 bytes. Sits exactly at the codepoint
    # cap; byte length is double — would have been rejected if the byte cap
    # were applied (the bug this scoping fix prevents).
    msg = "é" * config.WEATHER_LOCATION_NAME_MAX_LEN
    assert len(msg) == config.WEATHER_LOCATION_NAME_MAX_LEN
    assert len(msg.encode("utf-8")) > config.WEATHER_LOCATION_NAME_MAX_LEN
    ok, err = config.validate_setting("WEATHER_LOCATION_NAME", msg)
    assert ok is True, f"non-ASCII weather location at codepoint cap must pass: {err}"


def test_weather_location_name_rejects_codepoints_over_cap() -> None:
    """Regression net for #317 item 3 scoping: weather location is still
    capped by CODEPOINTS at WEATHER_LOCATION_NAME_MAX_LEN. Dropping the
    byte cap from this validator must not regress the codepoint cap."""
    msg = "X" * (config.WEATHER_LOCATION_NAME_MAX_LEN + 1)
    ok, err = config.validate_setting("WEATHER_LOCATION_NAME", msg)
    assert ok is False
    assert err is not None and "characters" in err


def test_weather_location_name_lone_surrogate_still_handled_cleanly() -> None:
    """The lone-surrogate guard for GIFT_MODE_MESSAGE is gated on the
    byte-cap branch (which only fires when ``max_bytes`` is not None).
    WEATHER_LOCATION_NAME doesn't pass through that branch, so a lone
    surrogate there must still be handled — not by the new UTF-8 guard,
    but by the existing free-form forbidden-char / line-terminator checks
    OR by being silently accepted (since none of those checks raise on
    surrogates). Both outcomes are non-crashing; this test just pins
    that the validator does NOT raise."""
    # Should return cleanly — either accepted (no surrogate-specific
    # rejection rule applies to weather names) or rejected by an existing
    # rule. Either way: no UnicodeEncodeError escapes the validator.
    ok, err = config.validate_setting("WEATHER_LOCATION_NAME", "\ud800")
    # Either pass or fail is fine — what matters is no exception escaped.
    assert ok in (True, False)
    if not ok:
        assert err is not None


@pytest.mark.parametrize("key", ["WEATHER_ENABLED"])
def test_validate_new_bool_keys(key: str) -> None:
    """GIFT_MODE_ENABLED dropped in #280 — gift mode is a one-shot action,
    not a persistent toggle. WEATHER_ENABLED stays."""
    ok, err = config.validate_setting(key, "true")
    assert ok is True, err
    ok, err = config.validate_setting(key, "garbage")
    assert ok is False


def test_gift_mode_enabled_no_longer_in_allowlist() -> None:
    """#280: the M3 GIFT_MODE_ENABLED toggle is gone. Attempting to save
    via the settings allowlist must reject it as unknown so a stale PWA
    form (cached navigation HTML) can't reintroduce the persistent state."""
    ok, _ = config.validate_setting("GIFT_MODE_ENABLED", "true")
    assert ok is False, "GIFT_MODE_ENABLED should be rejected by the allowlist post-#280"


def test_validate_weather_location_name_caps_at_120() -> None:
    ok, _ = config.validate_setting("WEATHER_LOCATION_NAME", "Austin, TX")
    assert ok is True
    ok, err = config.validate_setting("WEATHER_LOCATION_NAME", "X" * 121)
    assert ok is False
    assert err is not None and "120" in err


@pytest.mark.parametrize(
    "ch_name,ch",
    [
        ("VT (U+000B)", "\x0b"),
        ("FF (U+000C)", "\x0c"),
        ("FS (U+001C)", "\x1c"),
        ("GS (U+001D)", "\x1d"),
        ("RS (U+001E)", "\x1e"),
        ("NEL (U+0085)", "\x85"),
        ("LS (U+2028)", " "),
        ("PS (U+2029)", " "),
        ("U+2424 sentinel", "␤"),
        ("U+240D sentinel", "␍"),
    ],
)
def test_gift_mode_message_rejects_line_terminator_like_chars(ch_name: str, ch: str) -> None:
    """#319 adversarial /review: ``load_config`` parses env.sh with
    ``read_text().splitlines()``, which splits on more than just ``\\n``/``\\r``.
    A user pasting prose from Pages.app or Word (both emit U+2028 for soft
    returns) silently bricks GIFT_MODE_MESSAGE — the env.sh line splits in
    half, the second half doesn't match the KV pattern, and the reload
    returns a stray-quote artifact.

    Also pins the U+2424 / U+240D sentinel-collision case: those are our
    own internal encoding sentinels for ``\\n`` / ``\\r``, so a user typing
    them literally would have them silently rewritten on reload. Reject
    at the writer so the corruption can't be persisted."""
    ok, err = config.validate_setting("GIFT_MODE_MESSAGE", f"hi{ch}there")
    assert ok is False, f"{ch_name} must be rejected but was accepted"
    assert err is not None and "line-terminator" in err


@pytest.mark.parametrize(
    "ch_name,ch",
    [
        ("LS (U+2028)", " "),
        ("PS (U+2029)", " "),
        ("U+2424 sentinel", "␤"),
        ("U+240D sentinel", "␍"),
    ],
)
def test_weather_location_name_rejects_line_terminator_like_chars(ch_name: str, ch: str) -> None:
    """WEATHER_LOCATION_NAME is also in ``_SHELL_QUOTED_KEYS`` so it goes
    through the same env.sh encode/decode path. Same corruption risk if a
    user pastes a U+2028 from a word processor, or types a literal
    sentinel — pin parity with the GIFT_MODE_MESSAGE rejection."""
    ok, err = config.validate_setting("WEATHER_LOCATION_NAME", f"Austin{ch}TX")
    assert ok is False, f"{ch_name} must be rejected for weather location but was accepted"
    assert err is not None


def test_weather_location_name_still_rejects_newlines() -> None:
    """Regression net for #319: ``_make_free_form_validator`` gained an
    ``allow_newlines`` parameter (default ``False``). If a future refactor
    accidentally constructs the weather validator with ``allow_newlines=True``
    (or flips the default), this test catches it before silent regression."""
    ok, err = config.validate_setting("WEATHER_LOCATION_NAME", "Austin\nTX")
    assert ok is False
    assert err is not None and "line breaks" in err


# ---------- M3 shell-quoted writer (D7) ----------


def test_gift_mode_message_round_trips_newlines(tmp_path: Path) -> None:
    """#319 hardware-QA fix: env.sh is line-oriented but GIFT_MODE_MESSAGE
    allows embedded newlines post-#319. The naive shlex.quote-only writer
    produced a multi-line single-quoted bash value that ``load_config``
    (which iterates with ``splitlines()``) couldn't reassemble — it read
    only the first line and the textarea pre-fill rendered a stray-quote
    artifact like ``'Happy Birthday`` instead of the full message.

    The encoder swaps ``\\n`` → U+2424 and ``\\r`` → U+240D (visible
    symbol glyphs; not Unicode line separators like U+2028/U+2029, which
    splitlines() would split on and re-break the round-trip) before
    shlex.quote so the env.sh line never wraps; the loader reverses on
    read. This pins the round-trip end-to-end."""
    env = tmp_path / "env.sh"
    env.write_text("GIFT_MODE_MESSAGE=\n")
    multi_line = "Happy Birthday\nMom!"
    config.atomic_update({"GIFT_MODE_MESSAGE": multi_line}, env)

    # On-disk env.sh must be a single physical line so a future
    # ``load_config`` reads the value in one shot.
    raw = env.read_text().splitlines()
    gift_lines = [line for line in raw if "GIFT_MODE_MESSAGE" in line]
    assert len(gift_lines) == 1, f"GIFT_MODE_MESSAGE must serialise to one line; got {gift_lines!r}"
    assert "\n" not in gift_lines[0], "the stored value must not contain a raw newline"

    # Round-trip must recover the original message exactly.
    cfg = config.load_config(env)
    assert cfg["GIFT_MODE_MESSAGE"] == multi_line


def test_gift_mode_message_round_trips_crlf(tmp_path: Path) -> None:
    """Browser textarea POSTs use CRLF per RFC; we normalize on write so
    the round-trip value uses a single ``\\n`` separator."""
    env = tmp_path / "env.sh"
    env.write_text("GIFT_MODE_MESSAGE=\n")
    config.atomic_update({"GIFT_MODE_MESSAGE": "Line1\r\nLine2"}, env)
    cfg = config.load_config(env)
    assert cfg["GIFT_MODE_MESSAGE"] == "Line1\nLine2"


def test_atomic_update_shlex_quotes_gift_mode_message(tmp_path: Path) -> None:
    """Free-form text values must be wrapped via shlex.quote so that
    re-sourcing env.sh in bash gets the original string back, even when it
    contains spaces / single quotes / shell metacharacters."""
    env = tmp_path / "env.sh"
    # #280: env.sh no longer carries GIFT_MODE_ENABLED — gift mode is a
    # one-shot action via /api/system/prepare-for-gift, not a persistent
    # toggle. Only GIFT_MODE_MESSAGE persists as a draft.
    env.write_text("GIFT_MODE_MESSAGE=\n")
    msg = 'O\'Brien said "hi"; back later'
    config.atomic_update({"GIFT_MODE_MESSAGE": msg}, env)
    raw = env.read_text()
    # Round-trip via load_config must recover the exact input string.
    cfg = config.load_config(env)
    assert cfg["GIFT_MODE_MESSAGE"] == msg
    # The on-disk line must be safe to source via bash (single-quote wrapped
    # with embedded `'\''` for the apostrophe — `shlex.quote`'s canonical form).
    assert "'" in raw
    # The raw line must NOT carry an unbalanced literal apostrophe. Lines
    # are written `export KEY=value` after the export-canonicalization fix.
    line = next(line for line in raw.splitlines() if "GIFT_MODE_MESSAGE=" in line and "EXPORT" not in line.upper()[:1])
    # Verify the value parses back through shlex.split as a single token.
    import shlex

    tokens = shlex.split(line.split("=", 1)[1])
    assert tokens == [msg]


def test_atomic_update_does_not_quote_simple_keys(tmp_path: Path) -> None:
    """Tightly-validated keys (numbers, enums, booleans) stay unquoted on
    disk so the diff stays human-friendly."""
    env = tmp_path / "env.sh"
    env.write_text("WEATHER_UNITS=imperial\n")
    config.atomic_update({"WEATHER_UNITS": "metric"}, env)
    assert "WEATHER_UNITS=metric" in env.read_text()
    # No surrounding quotes added.
    assert "WEATHER_UNITS='metric'" not in env.read_text()
    assert 'WEATHER_UNITS="metric"' not in env.read_text()


def test_atomic_update_round_trip_message_with_quotes(tmp_path: Path) -> None:
    """Round-trip via env -> bash -> env (simulated by load_config) must
    preserve embedded quotes / spaces verbatim."""
    env = tmp_path / "env.sh"
    env.write_text("GIFT_MODE_MESSAGE=\n")
    msg = 'She said "don\'t"'
    config.atomic_update({"GIFT_MODE_MESSAGE": msg}, env)
    assert config.load_config(env)["GIFT_MODE_MESSAGE"] == msg


# ---------- M3 section/key contract (cross-module guard) ----------


def test_section_keys_are_subset_of_allowlist() -> None:
    """Every key in routes/settings.py:SECTION_KEYS must exist in
    SETTINGS_ALLOWLIST. If a section adds a new key, the allowlist must
    grow with it — otherwise atomic_update will reject the value at
    runtime with no UI surface to debug it."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from control_server.routes.settings import SECTION_KEYS

    all_section_keys = set().union(*SECTION_KEYS.values())
    missing = all_section_keys - set(config.SETTINGS_ALLOWLIST.keys())
    assert not missing, f"keys not in allowlist: {missing}"


# ---------- atomic_update ----------


def test_atomic_update_replaces_existing_keys(tmp_path: Path) -> None:
    env = tmp_path / "env.sh"
    env.write_text("WEATHER_LATITUDE=0\nWEATHER_LONGITUDE=0\nWEATHER_UNITS=imperial\n")
    config.atomic_update({"WEATHER_LATITUDE": "30.27", "WEATHER_LONGITUDE": "-97.74"}, env)
    cfg = config.load_config(env)
    assert cfg["WEATHER_LATITUDE"] == "30.27"
    assert cfg["WEATHER_LONGITUDE"] == "-97.74"
    assert cfg["WEATHER_UNITS"] == "imperial"  # untouched


def test_atomic_update_preserves_comments_and_blanks(tmp_path: Path) -> None:
    env = tmp_path / "env.sh"
    original = "# header comment\n\nWEATHER_LATITUDE=0\n# inline section\nWEATHER_UNITS=imperial\n"
    env.write_text(original)
    config.atomic_update({"WEATHER_LATITUDE": "30.27"}, env)
    out = env.read_text()
    assert "# header comment" in out
    assert "# inline section" in out
    assert "WEATHER_LATITUDE=30.27" in out
    assert "WEATHER_UNITS=imperial" in out


def test_atomic_update_preserves_export_prefix(tmp_path: Path) -> None:
    env = tmp_path / "env.sh"
    env.write_text("export WEATHER_LATITUDE=0\n")
    config.atomic_update({"WEATHER_LATITUDE": "12.34"}, env)
    assert env.read_text() == "export WEATHER_LATITUDE=12.34\n"


def test_atomic_update_appends_missing_keys(tmp_path: Path) -> None:
    env = tmp_path / "env.sh"
    env.write_text("WEATHER_LATITUDE=0\n")
    config.atomic_update({"WEATHER_UNITS": "metric"}, env)
    cfg = config.load_config(env)
    assert cfg["WEATHER_UNITS"] == "metric"
    assert cfg["WEATHER_LATITUDE"] == "0"


def test_atomic_update_appends_with_export_prefix(tmp_path: Path) -> None:
    """Hardware-QA fix 2026-04-29: appended keys MUST get `export` so
    runtheclock.sh's `source env.sh` exposes them to the Python child.
    Without `export`, the bash assignment is shell-local and `os.getenv`
    returns None — caught on test Pi when WEATHER_ENABLED=false had no
    runtime effect."""
    env = tmp_path / "env.sh"
    env.write_text("export WEATHER_UNITS=imperial\n")
    config.atomic_update({"WEATHER_ENABLED": "false"}, env)
    out = env.read_text()
    assert "export WEATHER_ENABLED=false" in out


def test_atomic_update_backfills_export_on_existing_unexported_lines(tmp_path: Path) -> None:
    """One-time backfill for env.sh files written before the export fix.
    A bare `WEATHER_ENABLED=true` from a prior /ship must gain `export`
    on the next save so the runtime starts seeing the value."""
    env = tmp_path / "env.sh"
    env.write_text("WEATHER_ENABLED=true\nexport WEATHER_UNITS=imperial\n")
    config.atomic_update({"WEATHER_ENABLED": "false"}, env)
    out = env.read_text()
    assert "export WEATHER_ENABLED=false" in out
    # The already-exported sibling stays exported.
    assert "export WEATHER_UNITS=imperial" in out


def test_atomic_update_preserves_trailing_newline(tmp_path: Path) -> None:
    env = tmp_path / "env.sh"
    env.write_text("WEATHER_UNITS=imperial\n")
    config.atomic_update({"WEATHER_UNITS": "metric"}, env)
    assert env.read_text().endswith("\n")


def test_atomic_update_no_trailing_newline_preserved(tmp_path: Path) -> None:
    env = tmp_path / "env.sh"
    env.write_text("WEATHER_UNITS=imperial")  # no trailing \n
    config.atomic_update({"WEATHER_UNITS": "metric"}, env)
    assert not env.read_text().endswith("\n")


def test_atomic_update_fails_fast_on_invalid_value(tmp_path: Path) -> None:
    env = tmp_path / "env.sh"
    env.write_text("WEATHER_LATITUDE=0\nWEATHER_UNITS=imperial\n")
    with pytest.raises(ValueError, match="invalid WEATHER_UNITS"):
        config.atomic_update(
            {"WEATHER_LATITUDE": "30.27", "WEATHER_UNITS": "../../tmp/pwn"},
            env,
        )
    # File must be unchanged — no partial writes.
    assert env.read_text() == "WEATHER_LATITUDE=0\nWEATHER_UNITS=imperial\n"


def test_atomic_update_rejects_unknown_key(tmp_path: Path) -> None:
    env = tmp_path / "env.sh"
    env.write_text("WEATHER_UNITS=imperial\n")
    with pytest.raises(ValueError, match="unknown setting"):
        config.atomic_update({"SOMETHING_ELSE": "x"}, env)


def test_atomic_update_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        config.atomic_update({"WEATHER_UNITS": "metric"}, tmp_path / "absent.sh")


def test_atomic_update_empty_updates_is_noop(tmp_path: Path) -> None:
    env = tmp_path / "env.sh"
    env.write_text("WEATHER_UNITS=imperial\n")
    mtime_before = env.stat().st_mtime_ns
    config.atomic_update({}, env)
    # File should be byte-identical and untouched on disk.
    assert env.read_text() == "WEATHER_UNITS=imperial\n"
    assert env.stat().st_mtime_ns == mtime_before


def test_atomic_update_preserves_mode(tmp_path: Path) -> None:
    env = tmp_path / "env.sh"
    env.write_text("WEATHER_UNITS=imperial\n")
    os.chmod(env, 0o640)
    mode_before = env.stat().st_mode & 0o777
    config.atomic_update({"WEATHER_UNITS": "metric"}, env)
    assert env.stat().st_mode & 0o777 == mode_before


def test_atomic_update_no_temp_files_left_on_success(tmp_path: Path) -> None:
    env = tmp_path / "env.sh"
    env.write_text("WEATHER_UNITS=imperial\n")
    config.atomic_update({"WEATHER_UNITS": "metric"}, env)
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".env.sh.tmp.")]
    assert leftovers == []


def test_atomic_update_no_temp_files_left_on_failure(tmp_path: Path, monkeypatch) -> None:
    env = tmp_path / "env.sh"
    env.write_text("WEATHER_UNITS=imperial\n")
    # Monkey-patch os.replace to raise — exercises the cleanup path.
    real_replace = os.replace

    def boom(src, dst, *a, **kw):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="simulated"):
        config.atomic_update({"WEATHER_UNITS": "metric"}, env)
    monkeypatch.setattr(os, "replace", real_replace)
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".env.sh.tmp.")]
    assert leftovers == [], f"tempfile leaked: {leftovers}"


def test_atomic_update_rejects_path_traversal_in_units(tmp_path: Path) -> None:
    """Path-traversal regression: an attacker submitting `units=../../tmp/pwn`
    must be rejected by atomic_update's validator before any write. Units
    flows into the weather-cache filename downstream."""
    env = tmp_path / "env.sh"
    original = "WEATHER_UNITS=imperial\n"
    env.write_text(original)
    with pytest.raises(ValueError):
        config.atomic_update({"WEATHER_UNITS": "../../tmp/pwn"}, env)
    assert env.read_text() == original


# ---------- atomic_update concurrency (issue #253) ----------


def test_atomic_update_concurrent_writers_dont_lose_updates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """10 threads, 10 distinct keys — without the flock, the read-modify-write
    inside atomic_update is racy and at least one update is silently dropped
    (last os.replace wins). With the lock, all 10 keys land.

    Uses monkeypatch to add 10 throwaway test-only keys to SETTINGS_ALLOWLIST
    so the test exercises the contention case the M3 Settings tab will hit
    without polluting the production allowlist with fake keys.
    """
    import threading

    test_keys = [f"TEST_KEY_{i}" for i in range(10)]
    extended_allowlist = dict(config.SETTINGS_ALLOWLIST)
    for k in test_keys:
        # Validator that accepts any digit string — keeps shell-safety guarantees.
        extended_allowlist[k] = lambda v: (v.isdigit(), None if v.isdigit() else "must be digits")
    monkeypatch.setattr(config, "SETTINGS_ALLOWLIST", extended_allowlist)

    env = tmp_path / "env.sh"
    env.write_text("# initial\n")

    barrier = threading.Barrier(len(test_keys))
    errors: list[BaseException] = []

    def writer(key: str, value: str) -> None:
        try:
            barrier.wait()  # release all threads at once to maximise contention
            config.atomic_update({key: value}, env)
        except BaseException as e:  # noqa: BLE001 — capture for assertion
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(k, str(i))) for i, k in enumerate(test_keys)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"writers raised: {errors}"
    cfg = config.load_config(env)
    for i, k in enumerate(test_keys):
        assert cfg.get(k) == str(i), f"missing/wrong {k}: {cfg.get(k)!r} (full cfg: {cfg})"


def test_atomic_update_lock_released_on_validation_failure(tmp_path: Path) -> None:
    """A ValueError on bad input must not leak a held lock — a subsequent
    successful call to atomic_update must complete without blocking.

    Validation runs *before* the lock is acquired, so this is mostly a
    documentation test for that ordering. If validation is ever moved
    inside the lock, this test guards against forgetting to release it.
    """
    env = tmp_path / "env.sh"
    env.write_text("WEATHER_UNITS=imperial\n")
    with pytest.raises(ValueError):
        config.atomic_update({"WEATHER_UNITS": "../../tmp/pwn"}, env)
    # If the lock leaked, this call would deadlock — the test would time out.
    config.atomic_update({"WEATHER_UNITS": "metric"}, env)
    assert config.load_config(env)["WEATHER_UNITS"] == "metric"


def test_atomic_update_lock_released_on_replace_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A filesystem error mid-write must release the lock so the next caller
    can proceed. Without `with _exclusive_lock(...)`, an exception inside
    the body would leak the lock fd indefinitely.
    """
    env = tmp_path / "env.sh"
    env.write_text("WEATHER_UNITS=imperial\n")

    real_replace = os.replace
    fail_count = {"n": 0}

    def boom(src, dst, *a, **kw):
        fail_count["n"] += 1
        if fail_count["n"] == 1:
            raise OSError("simulated rename failure")
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="simulated"):
        config.atomic_update({"WEATHER_UNITS": "metric"}, env)
    # Second call must not block — lock was released by the contextmanager.
    config.atomic_update({"WEATHER_UNITS": "metric"}, env)
    assert config.load_config(env)["WEATHER_UNITS"] == "metric"


# ─── #274 follow-up #4: bounded-wait timeout on _exclusive_lock ───────


def test_exclusive_lock_default_timeout_from_env() -> None:
    """`ENV_LOCK_WAIT_DEFAULT` is parsed at import time from
    `LITCLOCK_ENV_LOCK_WAIT`. Defaults to 30s; bad values fall back to 30s.

    Asserts the parse helper directly so the default-budget contract is
    pinned independently of the runtime acquire path."""
    assert config._parse_env_lock_wait_default() == 30.0
    # Sanity — the module-level constant reads the same default.
    assert config.ENV_LOCK_WAIT_DEFAULT == 30.0


def test_exclusive_lock_parses_env_var_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """`LITCLOCK_ENV_LOCK_WAIT=5` → 5.0. Mirrors the shell helper's env-var
    override so both sides honor `LITCLOCK_ENV_LOCK_WAIT`."""
    monkeypatch.setenv("LITCLOCK_ENV_LOCK_WAIT", "5")
    assert config._parse_env_lock_wait_default() == 5.0


def test_exclusive_lock_rejects_malformed_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Garbage in `LITCLOCK_ENV_LOCK_WAIT` falls back to 30s — never
    crashes module import on a real Pi with a typo'd env var."""
    monkeypatch.setenv("LITCLOCK_ENV_LOCK_WAIT", "not-a-number")
    assert config._parse_env_lock_wait_default() == 30.0


def test_exclusive_lock_negative_env_var_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative `LITCLOCK_ENV_LOCK_WAIT` is invalid — fall back to 30s
    rather than treating the lock as instantly stale."""
    monkeypatch.setenv("LITCLOCK_ENV_LOCK_WAIT", "-5")
    assert config._parse_env_lock_wait_default() == 30.0


def test_exclusive_lock_times_out_when_held(tmp_path: Path) -> None:
    """Adversarial threat model: a stuck shell writer holds the sidecar
    flock indefinitely. Before #274-followup-#4, the Python writer would
    block forever on `fcntl.flock(LOCK_EX)`. Now it must raise
    TimeoutError within ~timeout seconds.

    Verified via an in-process background thread that holds the flock on
    the sidecar; the foreground `atomic_update` must raise TimeoutError
    within `timeout + small fudge`.
    """
    import fcntl
    import threading
    import time as _time

    env = tmp_path / "env.sh"
    env.write_text("WEATHER_UNITS=imperial\n")
    lock_path = env.with_name(env.name + ".lock")
    lock_path.touch()

    release_event = threading.Event()
    holder_acquired = threading.Event()

    def hold_lock() -> None:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            holder_acquired.set()
            release_event.wait(timeout=5.0)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    holder = threading.Thread(target=hold_lock, daemon=True)
    holder.start()
    try:
        assert holder_acquired.wait(timeout=2.0), "background holder failed to acquire lock"
        # Use a short timeout so the test finishes quickly. The contract
        # under test is "raises TimeoutError within budget"; the exact
        # latency tolerance below is generous to absorb CI host jitter.
        start = _time.monotonic()
        with pytest.raises(TimeoutError, match=r"env\.sh lock held"):
            # Patch the default temporarily via the timeout kwarg path
            # — _exclusive_lock's timeout parameter is what production
            # callers ride.
            with config._exclusive_lock(env, timeout=0.3):
                pass
        elapsed = _time.monotonic() - start
        assert 0.25 <= elapsed <= 1.5, f"_exclusive_lock should raise TimeoutError near 0.3s, took {elapsed:.3f}s"
    finally:
        release_event.set()
        holder.join(timeout=2.0)


def test_exclusive_lock_acquires_quickly_when_lock_released(tmp_path: Path) -> None:
    """Mirror: if the holder releases mid-wait, the poll loop should pick
    up the lock within ~one poll interval (10ms + jitter), NOT wait the
    full timeout. Catches a regression where a future refactor introduces
    a long sleep between LOCK_NB attempts."""
    import fcntl
    import threading
    import time as _time

    env = tmp_path / "env.sh"
    env.write_text("WEATHER_UNITS=imperial\n")
    lock_path = env.with_name(env.name + ".lock")
    lock_path.touch()

    release_event = threading.Event()

    def hold_briefly() -> None:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            release_event.wait(timeout=5.0)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    holder = threading.Thread(target=hold_briefly, daemon=True)
    holder.start()
    _time.sleep(0.05)  # Let the holder acquire.

    # Schedule the release ~50ms from now.
    def release_after_50ms() -> None:
        _time.sleep(0.05)
        release_event.set()

    releaser = threading.Thread(target=release_after_50ms, daemon=True)
    releaser.start()

    start = _time.monotonic()
    # Generous 2s budget — we expect to acquire in ~50ms.
    with config._exclusive_lock(env, timeout=2.0):
        pass
    elapsed = _time.monotonic() - start
    holder.join(timeout=1.0)
    releaser.join(timeout=1.0)
    # Must acquire near the 50ms release point, NOT wait the full budget.
    assert elapsed < 0.5, f"_exclusive_lock should acquire shortly after release, took {elapsed:.3f}s"


def test_exclusive_lock_timeout_nan_does_not_loop_forever(tmp_path: Path) -> None:
    """Adversarial-review P2 — `float('nan')` for `timeout` previously
    fell through `max(0.0, nan) = nan`, leaving `deadline = monotonic() +
    nan = nan`. `monotonic() >= nan` is always False (IEEE), so the poll
    loop spun forever — reintroducing the OOM / Flask-thread-pile-up
    failure mode the PR was meant to fix.

    With the NaN-safe clamp (`not (x > 0)` rejects NaN per IEEE),
    timeout=NaN behaves as `timeout=0`: try once non-blocking, raise
    immediately on contention.
    """
    import fcntl
    import threading
    import time as _time

    env = tmp_path / "env.sh"
    env.write_text("WEATHER_UNITS=imperial\n")
    lock_path = env.with_name(env.name + ".lock")
    lock_path.touch()

    release_event = threading.Event()
    holder_acquired = threading.Event()

    def hold_lock() -> None:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            holder_acquired.set()
            release_event.wait(timeout=5.0)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    holder = threading.Thread(target=hold_lock, daemon=True)
    holder.start()
    try:
        assert holder_acquired.wait(timeout=2.0)
        start = _time.monotonic()
        with pytest.raises(TimeoutError):
            with config._exclusive_lock(env, timeout=float("nan")):
                pass
        elapsed = _time.monotonic() - start
        # Must NOT loop forever. Should behave like timeout=0 — near-instant.
        # A regression here would hang the test indefinitely; cap at 0.5s
        # to fail fast (poll cadence is 10ms, so a "loops a few times"
        # regression would still finish well under 0.5s — but a real
        # infinite-loop regression would never return at all and pytest
        # would time out at the suite level, which is the right signal).
        assert elapsed < 0.5, f"NaN timeout should clamp to non-blocking, took {elapsed:.3f}s"
    finally:
        release_event.set()
        holder.join(timeout=2.0)


def test_exclusive_lock_timeout_zero_is_non_blocking(tmp_path: Path) -> None:
    """`timeout=0` should try once and raise immediately on contention.
    Useful for tests + probe-like callers that don't want to spin."""
    import fcntl
    import threading
    import time as _time

    env = tmp_path / "env.sh"
    env.write_text("WEATHER_UNITS=imperial\n")
    lock_path = env.with_name(env.name + ".lock")
    lock_path.touch()

    release_event = threading.Event()
    holder_acquired = threading.Event()

    def hold_lock() -> None:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            holder_acquired.set()
            release_event.wait(timeout=5.0)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    holder = threading.Thread(target=hold_lock, daemon=True)
    holder.start()
    try:
        assert holder_acquired.wait(timeout=2.0)
        start = _time.monotonic()
        with pytest.raises(TimeoutError):
            with config._exclusive_lock(env, timeout=0):
                pass
        elapsed = _time.monotonic() - start
        # Non-blocking: must be much faster than even one poll interval.
        assert elapsed < 0.05, f"timeout=0 should be near-instant, took {elapsed:.3f}s"
    finally:
        release_event.set()
        holder.join(timeout=2.0)


def test_atomic_update_propagates_timeout_error(tmp_path: Path) -> None:
    """Integration: a held lock surfaces as TimeoutError from
    `atomic_update`, not as a hang. The control_server settings route
    catches TimeoutError to emit the 504 ENV_LOCK_TIMEOUT envelope —
    if this propagation regresses, the route falls back to 500 (route's
    catch-all) and the user sees a generic error instead of the actionable
    "settings file is busy, try again" message."""
    import fcntl
    import threading

    env = tmp_path / "env.sh"
    env.write_text("WEATHER_UNITS=imperial\n")
    lock_path = env.with_name(env.name + ".lock")
    lock_path.touch()

    release_event = threading.Event()
    holder_acquired = threading.Event()

    def hold_lock() -> None:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            holder_acquired.set()
            release_event.wait(timeout=5.0)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    holder = threading.Thread(target=hold_lock, daemon=True)
    holder.start()
    try:
        assert holder_acquired.wait(timeout=2.0)
        # Monkeypatch the default via the env var so production callers
        # of atomic_update (which don't pass timeout=) hit the budget.
        # This also confirms the env-var override flows end-to-end.
        original_default = config.ENV_LOCK_WAIT_DEFAULT
        config.ENV_LOCK_WAIT_DEFAULT = 0.3
        try:
            with pytest.raises(TimeoutError, match=r"env\.sh lock held"):
                config.atomic_update({"WEATHER_UNITS": "metric"}, env)
        finally:
            config.ENV_LOCK_WAIT_DEFAULT = original_default
    finally:
        release_event.set()
        holder.join(timeout=2.0)


# ─── #337 A1/A6.1 new validators (post /review tightening) ─────────────────


@pytest.mark.parametrize("value", ["auto", "specific"])
def test_weather_location_mode_accepts_valid(value: str) -> None:
    """#337 A1 + /review: validator accepts exactly the two enum values."""
    ok, err = config.validate_setting("WEATHER_LOCATION_MODE", value)
    assert ok, err


@pytest.mark.parametrize(
    "value",
    [
        "",          # /review tightening: empty is REJECTED at write time
                     # to close the downgrade-to-auto hole (empty reads as
                     # auto via the read-side default, but accepting empty
                     # at write would let a confused client silently flip
                     # MODE=specific back to auto)
        "AUTO",      # case-sensitive
        "Specific",
        "manual",
        "off",
        "ON",
        "true",
        " auto",     # leading whitespace
        "auto ",
    ],
)
def test_weather_location_mode_rejects_invalid(value: str) -> None:
    ok, err = config.validate_setting("WEATHER_LOCATION_MODE", value)
    assert not ok, f"validator must reject {value!r}"
    assert err and "auto" in err


@pytest.mark.parametrize("value", ["", "US", "GB", "IN", "FR", "JP"])
def test_weather_ip_country_accepts_valid(value: str) -> None:
    """#337 A6.1: ISO 3166-1 alpha-2 UPPERCASE or empty."""
    ok, err = config.validate_setting("WEATHER_IP_COUNTRY", value)
    assert ok, err


@pytest.mark.parametrize(
    "value",
    [
        "us",       # /review tightening: lowercase REJECTED so the case-
                    # sensitive country-change-detector comparison in
                    # location_resolver._persisted_country can't silently
                    # miscompare against the uppercase resolver output
        "gb",
        "USA",      # 3 chars
        "U",        # 1 char
        "12",       # digits
        "U1",       # mixed
        "U S",      # space
        "中国",     # non-ASCII
        " US",      # leading whitespace
        "US ",
    ],
)
def test_weather_ip_country_rejects_invalid(value: str) -> None:
    ok, err = config.validate_setting("WEATHER_IP_COUNTRY", value)
    assert not ok, f"validator must reject {value!r}"
