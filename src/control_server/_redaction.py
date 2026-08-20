"""Redaction patterns for both the diagnostic snapshot deny-list test and
the in-memory log buffer's filter (litclock-dev#416 / OV-1=A).

The /diagnostics snapshot already redacts user-typed PII via the per-row
PRIVACY_POLICY in ``_diagnostics_privacy.py``. The live log buffer surfaces
the *raw* Python ``logging`` calls of control_server itself — if any
``log.info(...)`` interpolates a secret-shaped string (PSK, ssh key, token,
exact coords), the buffer would leak it.

This module gives us:

1. :func:`redact_text` — substring/regex/entropy redaction of a single
   string. Used by:
   a. The :class:`RedactingFilter` (installed alongside the MemoryLogHandler
      so EVERY log entry is sanitized before it lands in the buffer — see
      ``log_buffer.py``).
   b. The deny-list test in ``tests/test_diagnostics_no_secrets.py``, which
      asserts no rendered HTML / JSON payload contains anything this function
      would have redacted.

2. :class:`RedactingFilter` — a :class:`logging.Filter` subclass that
   replaces ``record.msg`` and any ``record.args`` with their redacted
   equivalents before the formatter sees them.

The patterns are intentionally conservative. False positives here are a
minor UX cost (a real git SHA in a log line might get over-redacted); false
negatives are a privacy bug. We accept the tradeoff.
"""

from __future__ import annotations

import logging
import re
from typing import Final

# --- Patterns ---------------------------------------------------------------

# Credential-keyword leaks in env-style strings — covers common shell-export
# forms emitted by setup_server logs and any future logger that hand-formats
# a config dump. Includes generic SECRET / TOKEN / API_KEY / BEARER / AUTH
# variants (PR1 /review extension) so future log calls don't need the
# 40+ char catch-all to fire.
#
# Boundary handling (PR1 adversarial pass):
# - Compound keys like ``GH_AUTH_TOKEN=foo`` need the keyword to match even
#   when an underscore (a word char) precedes it. ``\b`` would NOT match
#   between two word chars, so we use a non-word-or-start lookbehind
#   ``(?:^|[\W_])`` (matches start-of-string, whitespace, punctuation, OR
#   underscore as a separator).
# - Quoted values like ``WIFI_PASSWORD="my secret pass"`` would only match
#   ``"my`` under ``\S+`` and leak the rest of the password. The value
#   group now accepts an optional surrounding quote and matches through
#   the closing quote when present.
_PSK_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)(?:^|[\W_])("
    r"(?:PSK|PASSWORD|PASSWD|WIFI_PASS|WIFI_PASSWORD|"
    r"SECRET|TOKEN|API[_-]?KEY|AUTH|AUTHORIZATION|BEARER|CLIENT_SECRET|"
    # PR2 /review extension — openweathermap.py:91 emits
    # ``…&appid=$KEY`` in URL traces; the redaction filter at PR1 missed
    # this. SSID is intentionally NOT added here because the copy-payload
    # row label ``**SSID:**`` would false-match; SSID protection lives at
    # the field-level (PRIVACY_POLICY["ssid"] = redacted).
    r"APPID|OPENWEATHERMAP_APIKEY|OWM_KEY)"
    r"\s*[:=]\s*)"
    r'(?:"([^"]*)"|\'([^\']*)\'|(\S+))'
)


# nmcli takes credentials as SPACE-separated argv tokens (`… password <v>`,
# `… psk <v>`), never KEY=VALUE — so _PSK_RE's mandatory `[:=]` misses them
# completely. That matters because `pi` has NOPASSWD sudo on the image, sudo
# writes the FULL argv to its command-audit line, journald ships persistent,
# and the Diagnostics support bundle exports that journal for the firstboot
# unit. An 8-char hotspot PSK also sits far below _LONG_TOKEN_RE's 40-char
# floor, so nothing else caught it either: verified empirically that a real
# sudo audit line came back from redact_text() with the password intact.
#
# This was tolerable while the hotspot password was regenerated every cycle
# (the leaked value was dead on arrival). litclock-dev#620 makes it the
# device's PERSISTENT setup-network key, which turns a transient leak into a
# durable one in a file users attach to GitHub issues — so the filter has to
# learn the argv form.
#
# Deliberately NOT solved by relaxing _PSK_RE's `[:=]` to also accept
# whitespace: that would redact the next word after any prose mention of
# "password" ("the password is wrong" -> "the password <REDACTED> wrong").
# Scoped instead to lines that actually invoke nmcli.
_NMCLI_SECRET_ARG_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)(\b(?:password|psk|wifi-sec\.psk|802-11-wireless-security\.psk)\s+)(\S+)"
)

# Line-scope guard: an nmcli credential argument only ever appears on a line
# that is invoking nmcli against wifi/connection objects. Requiring one of
# these keeps narrative log lines that merely mention nmcli out of scope.
_NMCLI_ARGV_CONTEXT: Final[frozenset[str]] = frozenset({"wifi", "connection", "con-name", "hotspot"})

# Value-shape guard for the residual false positive: on an in-scope line, a
# sentence like "nmcli wifi connect failed, the password was rejected" would
# otherwise redact the auxiliary verb and make the log unreadable — which
# defeats the point of shipping a support bundle at all. A PSK is never one
# of these words (WPA2 requires 8+ chars, and none of these reach 8).
_NOT_A_SECRET: Final[frozenset[str]] = frozenset(
    {"was", "is", "were", "are", "will", "would", "has", "had", "must", "should", "may", "might", "does", "did", "can"}
)


def _nmcli_arg_replace(m: re.Match[str]) -> str:
    if m.group(2).strip(".,;:").lower() in _NOT_A_SECRET:
        return m.group(0)
    return f"{m.group(1)}{REDACTED_TOKEN}"


def _redact_nmcli_argv(text: str) -> str:
    """Redact space-separated nmcli credential arguments.

    Scoped per line, and only on lines that actually invoke nmcli against a
    wifi/connection object — see the guards above for why both narrowings
    exist.
    """
    if "nmcli" not in text.lower():
        return text
    out = []
    for line in text.split("\n"):
        low = line.lower()
        if "nmcli" in low and any(ctx in low for ctx in _NMCLI_ARGV_CONTEXT):
            line = _NMCLI_SECRET_ARG_RE.sub(_nmcli_arg_replace, line)
        out.append(line)
    return "\n".join(out)


def _psk_replace(m: re.Match[str]) -> str:
    """Replacement helper: keep the ``KEY=`` prefix, drop the value, keep
    surrounding quotes if present so the redacted form is still valid
    shell syntax."""
    prefix = m.group(1)
    if m.group(2) is not None:
        return f'{prefix}"{REDACTED_TOKEN}"'
    if m.group(3) is not None:
        return f"{prefix}'{REDACTED_TOKEN}'"
    return f"{prefix}{REDACTED_TOKEN}"


# SSH key fragments. Block both armored headers and unrolled base64 chunks
# that look like ssh-rsa public keys.
_SSH_RE: Final[re.Pattern[str]] = re.compile(
    r"(-----BEGIN [A-Z ]+-----[\s\S]*?-----END [A-Z ]+-----|ssh-(?:rsa|ed25519|dss)\s+[A-Za-z0-9+/=]{40,})"
)

# GitHub tokens — both the legacy 40-char hex form and the modern
# `ghp_` / `github_pat_` / `ghs_` prefixes.
_GH_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"\b(?:gh[ps]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{82})\b")

# Generic long high-entropy ASCII tokens. The 40-char floor avoids tagging
# normal git short SHAs (7-12 chars) and version strings while still catching
# typical 40-byte hex secrets / base64-shaped credentials. Restrict to a
# closed character set so we don't gobble normal English words. Anchored on
# word boundaries.
#
# NOTE: this is the noisiest pattern — see "false positives" in the test
# plan. The carve-out for git SHAs is implicit, not an explicit allowlist:
# the `(?=.*[A-Z])` lookahead requires at least one uppercase letter, so
# pure-lowercase 40-char hex strings (the git short-SHA shape) never
# match. The compromise: a 40+ char run of *mixed* case + digits gets
# redacted; a 40-char run of *pure* hex is left alone (still has a
# git-SHA shape).
_LONG_TOKEN_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?=[A-Za-z0-9/+=_-]{40,})(?=.*[A-Z])(?=.*[a-z])(?=.*\d)[A-Za-z0-9/+=_-]{40,}\b"
)

# Exact lat/lon as they appear inside log messages: ``lat=37.7749 lon=-122.4194``
# or ``(37.774929, -122.419418)`` or ``lat='37.7749'`` (the %r form that
# ``location_resolver.py`` emits for refused/incomplete coordinates). The
# structured /api/diagnostics surface rounds these via the privacy policy —
# but a logger that interpolates floats or quoted strings directly would
# leak the raw value into the live drawer.
#
# Lead-in is a ZERO-WIDTH negative lookbehind ``(?<![A-Za-z])`` — not the ``\b``
# it started with, nor the captured ``(^|[\W_])`` of the litclock-dev#497 fix:
#   * ``\b`` failed on compound keys — ``WEATHER_LATITUDE=`` has ``_`` (a word
#     char) before ``LATITUDE``, so no boundary (litclock-dev#497: full-precision home
#     coords leaked through the support-logs / journal export).
#   * A *consuming* lead-in ``(^|[\W_])`` fixed compound keys but still leaked
#     ADJACENT coords ``lat=11.1lon=22.2`` — consuming the char before ``lon``
#     leaves nothing for its own lead-in to match (litclock-dev#498).
# A lookbehind is zero-width, fixing BOTH: it permits a digit/underscore/start
# before the keyword (compound keys AND back-to-back coords match) while still
# blocking a LETTER before it, so ``belong=``, ``along=``, ``flat=``,
# ``translation=``, ``collation=`` never match. No separator group to re-emit.
#
# The value group accepts an optional sign and scientific notation — both a
# dotted mantissa (``.331494e2``) and a bare-integer mantissa with an exponent
# (``331494e-4``, which ``config`` accepts via ``float()`` and a weather-error
# log could echo). The ``['\"]?`` after the keyword also catches JSON quoted-key
# forms (``{"lat": "33.1234"}``). These extra shapes were all non-reachable
# in-tree (geo APIs + ``str(float)`` emit plain dotted decimals), so they are
# defense-in-depth for the "redaction is safe to share" contract (litclock-dev#498).
# Separators/quotes are normalized away in the output — lossy, but the precision
# (the sensitive part) never survives.
#
# Comma-decimals (EU-locale ``33,1494``) are DELIBERATELY not matched: a comma is
# ambiguous with a list separator (``lat=1,2,3`` must not be fabricated into a
# coordinate), and a comma coordinate is impossible to reach anyway — the
# ``_validate_latitude`` writer does ``float(value)``, which rejects a comma, so
# it can never be stored in env.sh or logged. Matching it would only risk
# corrupting a legitimate list for zero real coverage (litclock-dev#498 /review, both models).
#
# A bare integer with no fractional part or exponent (``lat=33``) is left alone —
# it carries no sub-degree precision to leak.
_COORD_KEYED_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)(?<![A-Za-z])(lat(?:itude)?|lon(?:gitude)?|long)"
    r"['\"]?\s*[:=]\s*['\"]?"
    r"([-+]?\d*\.\d+(?:[eE][-+]?\d+)?|[-+]?\d+[eE][-+]?\d+)['\"]?"
)
# Decimal-count floor removed in PR1 adversarial pass — a coord already
# rounded to 2dp upstream (37.77, -122.42) should still pass through the
# rounding sub so the output shape is consistent regardless of who
# pre-rounded. ``[-+]?`` accepts a leading sign for parity with the keyed form.
_COORD_PAIR_RE: Final[re.Pattern[str]] = re.compile(r"\(\s*([-+]?\d{1,2}\.\d+)\s*,\s*([-+]?\d{1,3}\.\d+)\s*\)")

# Replacement marker. Keep distinct from PRIVACY_POLICY's REDACTED_VALUE so
# the two surfaces don't look identical when both are visible — helper
# pasting the log block sees a different glyph than the snapshot row.
REDACTED_TOKEN: Final[str] = "***REDACTED***"


def _coord_keyed_replace(m: re.Match[str]) -> str:
    """Round a keyed coordinate to 2dp, normalizing the separator to ``=``. The
    original separator and any surrounding quotes are dropped — lossy, but the
    precision (the sensitive part) never survives. Group 1 is the ``lat``/``lon``
    keyword, group 2 the numeric value."""
    return f"{m.group(1)}={_round2(m.group(2))}"


def redact_text(text: str) -> str:
    """Apply every pattern to ``text`` and return the redacted result.

    Order matters: SSH blocks first (the longest match), then keyed
    credentials (PSK/PASSWORD), then GitHub tokens, then coordinates, then
    the generic long-token catch-all. This minimizes the chance of the
    high-entropy catch-all eating part of an already-matched secret and
    leaving the surrounding context unredacted.
    """
    if not text:
        return text

    out = _SSH_RE.sub(REDACTED_TOKEN, text)
    out = _PSK_RE.sub(_psk_replace, out)
    # nmcli's space-separated argv form (litclock-dev#620 /review) — must run
    # while the value is still whole, i.e. before the coordinate and
    # long-token passes rewrite anything inside it.
    out = _redact_nmcli_argv(out)
    out = _GH_TOKEN_RE.sub(REDACTED_TOKEN, out)
    # Round coordinate matches to 2dp inline instead of redacting outright —
    # the helper still wants to see "user is in Texas" without leaking the
    # exact street.
    out = _COORD_KEYED_RE.sub(_coord_keyed_replace, out)
    out = _COORD_PAIR_RE.sub(
        lambda m: f"({_round2(m.group(1))}, {_round2(m.group(2))})",
        out,
    )
    out = _LONG_TOKEN_RE.sub(REDACTED_TOKEN, out)
    return out


def _round2(s: str) -> str:
    """Round a numeric string to 2dp. Falls back to the original string on
    parse error so a malformed coordinate doesn't crash the filter."""
    try:
        return f"{float(s):.2f}"
    except ValueError:
        return s


class RedactingFilter(logging.Filter):
    """A logging.Filter that rewrites the record's message via :func:`redact_text`.

    Installed on the root logger by ``log_buffer.init_memory_handler()`` (see
    that module's docstring + litclock-dev#416 OV-1=A rationale). The filter applies
    BEFORE the buffer's append, so the in-memory deque never contains a
    secret-shaped substring even if a future ``log.info("PSK=hunter2")`` call
    lands somewhere in the codebase.

    The filter rewrites the formatted message — i.e. the result of
    ``record.getMessage()``. We do that by clearing ``args`` (so the
    formatter doesn't try to %-format again) and replacing ``msg`` with the
    redacted, fully-formatted string. This matches Python's
    ``logging.LogRecord.getMessage()`` contract: if ``args`` is falsy the
    raw ``msg`` is returned as-is.
    """

    # Always allow the record through — we're only rewriting, never dropping.
    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            formatted = record.getMessage()
        except Exception:  # noqa: BLE001 — a misformatted record is the caller's bug; we still want it in the buffer
            return True
        redacted = redact_text(formatted)
        if redacted != formatted:
            record.msg = redacted
            record.args = None
        return True
