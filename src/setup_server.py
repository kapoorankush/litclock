#!/usr/bin/env python3
"""
Web-based Setup Server for LitClock — WiFi-only provisioning (EPIC litclock-dev#383).

The hotspot form collects only WiFi credentials. After WiFi connects,
``_resolve_location_from_ip`` auto-populates ``WEATHER_LATITUDE``,
``WEATHER_LONGITUDE``, ``WEATHER_LOCATION_NAME``, ``WEATHER_UNITS``, and the
system timezone from a single ``ip-api.com`` call (with 3 retries on the
DNS-race-post-handshake failure mode). Users override anything wrong from
the Control PWA Settings tab after first-boot.

All env.sh writes route through ``config.atomic_update``; timezone is set
via ``timedatectl`` and runs BEFORE the env write so the worst failure
case is "tz set, env stale" instead of "wrong-time clock with working
weather" (see ``_update_env_location`` docstring for the A2-revised
ordering rationale).

Usage:
    # Provisioning mode (HTTP on hotspot, with WiFi selection) — the only
    # serving mode since litclock-dev#715:
    python setup_server.py <env_file> <signal_file> --provisioning

    # No-flag invocations exit 1 loudly. first-boot.sh's missing-env
    # fallback relies on exactly that (restart-loop into the 1800s
    # Setup-Incomplete ceiling).
"""

import html
import http.server
import json
import logging
import os
import signal
import socketserver
import sys
import threading
import urllib.parse
from pathlib import Path

# Captive-portal primitives — see captive_portal.py for the rationale on
# why SETUP_HOSTNAME lives there (avoids a circular dep with eink_display).
# APPLE_CAPTIVE_PORTAL_HOSTS is the subset of CAPTIVE_PORTAL_HOSTS that
# gets the iOS-shaped bridge HTML; the rest get a 302 redirect.
from captive_portal import (
    APPLE_CAPTIVE_PORTAL_HOSTS,
    CAPTIVE_PORTAL_HOSTS,
    SETUP_HOSTNAME,
)

PORT = 8080
# Hotspot gateway IP shown as the absolute-fallback URL on the bridge page
# and e-ink display. The canonical copy of this constant lives in
# wifi_provision.py (which also writes the NetworkManager shared-connection
# config); this name is kept here purely for local readability in bridge
# text formatting.
HOTSPOT_GATEWAY_IP = "10.42.0.1"

ENV_FILE = None
SIGNAL_FILE = None
PROVISIONING_MODE = False
WIFI_CONNECT_ERROR = None  # Set by background thread on WiFi failure
WIFI_CONNECT_IN_FLIGHT = False  # Guard against concurrent connect attempts
HOTSPOT_SSID = None  # Original hotspot SSID, used to restore after failed WiFi
HOTSPOT_PASSWORD = None  # Original hotspot password, used to restore after failed WiFi
# Raw (hotspot-filtered) network list from the last non-empty scan. The DATA
# is cached, never the rendered HTML: the <option> list carries localized
# copy (placeholder, manual entry), and a cached render served the OLD
# language for up to a TTL after a mid-session language switch (Stage-4
# gates /review on litclock-dev#532). Rendering per request is trivial;
# the TTL exists to serialize radio SCANS, not renders.
_WIFI_SCAN_NETWORKS = None
_WIFI_SCAN_TIME = 0  # time.monotonic() when cache was populated
_WIFI_SCAN_TTL = 30  # seconds before cache expires
# The last scan the reader was SHOWN found nothing (litclock-dev#532 RT3
# review) — a page scan or the Refresh button's /scan-wifi. An empty scan is
# deliberately NOT cached (the next ordinary load retries, and an older list
# may still sit in the cache), but a language switch re-renders what the
# reader last saw: neither 2-20s of radio for the same empty list, nor an
# older list resurrected in its place.
_WIFI_SCAN_LAST_EMPTY = False
# time.monotonic() when the last scan FINISHED, whatever it found — unlike
# _WIFI_SCAN_TIME, which only a non-empty scan moves. A request that asks for
# a scan newer than itself (the no-JS Refresh, /scan-wifi) and waited on
# _SCAN_CACHE_LOCK behind one takes that scan's result, empty included, rather
# than holding the radio for another 2-27s (litclock-dev#908 review).
_WIFI_SCAN_ATTEMPT_TIME = 0
# True when the LAST scan failed outright (litclock-dev#908 gap 1). Kept apart
# from the cache on purpose — a failure must not overwrite what earlier scans
# found — but a language switch (stale_ok) has to re-render the "Scan failed"
# the reader is looking at without a 2-27s rescan (litclock-dev#532 D10), and must not
# swap it for a stale "No networks found" from an older empty scan.
_WIFI_SCAN_LAST_FAILED = False
# SSIDs from the same scan that produced _WIFI_SCAN_NETWORKS. Kept alongside
# so a hand-typed name can be checked against what the radio actually saw —
# see the `hidden` decision in do_POST (litclock-dev#554).
_WIFI_SCAN_SSIDS = frozenset()

# litclock-dev#532: user-visible setup copy resolves from the language
# catalog PER REQUEST (the final-slice /review overturned the original
# import-time posture: the picker on this very page can change the language
# mid-session, and a retry re-render must follow it). The loader's
# never-raise contract means a broken catalog degrades this page to visible
# dotted keys, not a dead hotspot. MANUAL_SSID_VALUE stays a literal: it is
# a POST sentinel, not copy.
import strings_catalog as _strings_catalog  # noqa: E402

# litclock-dev#532 RT3: ONE catalog resolution per page (the PWA's per-request
# snapshot, control_server/_strings.py, done for this server). A request sets
# it for its thread in the page's language (the setup session's, D10); every
# lookup below reads it, so a page cannot mix two languages and the
# eighty-odd lookups need no language argument each. Outside a request — the
# connect thread, a test calling a builder directly — a lookup resolves the
# device language afresh, as it always did.
_RENDER = threading.local()


def _catalog():
    snap = getattr(_RENDER, "snap", None)
    return snap if snap is not None else _strings_catalog.snapshot()


def _catalog_get(key, /, **slots):
    return _catalog().get(key, **slots)


class _page:
    """``with _page(accept_language):`` — resolve the catalog once for one page
    in the setup session's language. Re-entrant: a builder called inside a
    request's scope reuses the request's resolution."""

    def __init__(self, accept_language=None):
        self.accept_language = accept_language
        self.owner = False

    def __enter__(self):
        if getattr(_RENDER, "snap", None) is None:
            _RENDER.snap = _strings_catalog.snapshot(_default_language(self.accept_language))
            self.owner = True
        return _RENDER.snap

    def __exit__(self, *_exc):
        if self.owner:
            _RENDER.snap = None
        return False

# litclock-dev#532 pickers: a gifted clock carries the gifter's language
# choice across the reset via this root-owned marker (the .welcome-message
# pattern). Read-precedence lives here; the WRITE side (PWA gift flow +
# reset-setup preservation) lands with the PWA picker arc. Env override is
# the test seam.
GIFT_LANGUAGE_MARKER = os.environ.get("LITCLOCK_GIFT_LANGUAGE_MARKER", "/etc/litclock/.gift-language")


# The setup session's language (litclock-dev#532 D10/R13): the last EXPLICIT
# choice this process saw — a language link (GET ?lang=xx) or the WiFi POST.
# Highest precedence, so a link click re-renders every later page in that
# language and a failed WiFi join never silently resets it to Accept-Language
# (/review litclock-dev#742). Only the POST writes env.sh; a link never does. Process-wide
# on purpose: setup is one phone at a time. Cleared by reset_state().
SESSION_LANGUAGE = ""


def _read_marker_code():
    """Safe-read the gift marker: O_NOFOLLOW+O_NONBLOCK+regular-file+size cap
    (the repo's update_state safe-read posture — /review litclock-dev#742; /etc/litclock
    is root-owned, but uniformity beats reasoning about writers per path)."""
    try:
        fd = os.open(GIFT_LANGUAGE_MARKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        return ""
    try:
        import stat as _stat  # noqa: PLC0415

        if not _stat.S_ISREG(os.fstat(fd).st_mode):
            return ""
        return os.read(fd, 64).decode("utf-8", errors="replace").strip()
    except OSError:
        return ""
    finally:
        os.close(fd)


def _persisted_language():
    """The env.sh LITCLOCK_LANGUAGE line, if any (the POST writes it).

    Reuses strings_catalog's line regex with LAST-match semantics — bash
    sourcing is last-wins, and three parsers disagreeing on a degenerate
    file (duplicates, trailing comments) is exactly the drift class this
    workstream removes (/review litclock-dev#742 follow-up F4).
    """
    # The process environment first — strings_catalog's own precedence, so
    # the page and the device agree on what "saved" means (litclock-dev#532
    # RT3; first-boot does not export it, so on a device this is env.sh).
    explicit = os.environ.get("LITCLOCK_LANGUAGE", "").strip()
    if explicit:
        return explicit
    if not ENV_FILE:
        return ""
    import strings_catalog  # noqa: PLC0415

    value = ""
    try:
        with open(ENV_FILE, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = strings_catalog._ENV_LINE_RE.match(line)
                if m:
                    value = m.group(1)
    except OSError:
        return ""
    return value


def _default_language(accept_language):
    """This session's choice (link or POST) > persisted env value > gift marker >
    Accept-Language negotiation > English (litclock-dev#532, /review litclock-dev#742:
    without the session/env steps, a failed-WiFi retry silently reset a
    picked language back to the phone's locale).

    Persisted env OUTRANKS the marker (5b adversarial /review): a
    non-empty persisted value is always someone's explicit choice — the
    recipient's setup POST, a PWA save, or the gift arm's own env seed
    (which equals the marker code, making the order moot on the normal
    gift path). The marker ranking above it created a losing state: a
    kept inactive marker (trap b) + a recipient's later re-provision
    after an OTA activated that code silently reverted THEIR explicit
    pick to the gifter's. The marker now only decides when env carries
    no language — exactly the fresh-gift window it exists for."""
    import strings_catalog  # noqa: PLC0415

    active = strings_catalog.active_codes()
    if SESSION_LANGUAGE and SESSION_LANGUAGE in active:
        return SESSION_LANGUAGE
    persisted = _persisted_language()
    if persisted and persisted in active:
        return persisted
    code = _read_marker_code()
    if code and code in active:
        return code
    return strings_catalog.negotiate(accept_language)


def _language_links():
    """D6: a row of links at the very top, one per active language, each in
    its own language and tagged ``lang``/``hreflang``; the page's language
    unlinked. Clicking one reloads the page in that language (GET ?lang=xx)
    without saving anything. Rendered ONLY on a multi-language fleet — a row
    with one entry is pure friction (the owner's zero-friction rule)."""
    active = _strings_catalog.active_languages()
    if len(active) < 2:
        return ""
    current = _catalog().requested
    items = []
    for code in sorted(active):
        attr = html.escape(code, quote=True)
        native = html.escape(str(active[code].get("native_name") or code))
        if code == current:
            items.append(f'<span lang="{attr}" aria-current="true">{native}</span>')
        else:
            href = f"/setup?lang={urllib.parse.quote(code)}"
            items.append(f'<a href="{href}" lang="{attr}" hreflang="{attr}">{native}</a>')
    label = _page_text("setup.language.title")
    return f'        <nav class="lang-links" aria-label="{label}">{" ".join(items)}</nav>\n'


def _rescan_request(query):
    """The no-JS Refresh (litclock-dev#908): ``?rescan=1`` asks for a scan
    newer than this request. Returns that request's monotonic stamp, or None.

    Ignored while a join is in flight: the radio is busy joining the owner's
    network, a rescan there contends with the join it is waiting on, and the
    page in that state is a progress banner, not a picker anyone is using."""
    import time  # noqa: PLC0415

    if WIFI_CONNECT_IN_FLIGHT or urllib.parse.parse_qs(query).get("rescan", [""])[0] != "1":
        return None
    return time.monotonic()


def _apply_language_link(query, accept_language=None):
    """A language link (GET ?lang=xx, D6/D10): make an ACTIVE code the
    session's language, and report whether that SWITCHED the page's language
    — only a switch may reuse a stale scan, so a no-JS reader reloading the
    same link still gets fresh networks (RT3 review). Anything else — no
    parameter, an unknown or inactive code — keeps the current language.
    Never writes env.sh: only the WiFi POST saves the device language (R13)."""
    global SESSION_LANGUAGE
    code = urllib.parse.parse_qs(query).get("lang", [""])[0].strip()
    if code and code in _strings_catalog.active_codes():
        switched = code != _default_language(accept_language)
        SESSION_LANGUAGE = code
        return switched
    return False


def _adopt_posted_language(params, accept_language=None):
    """The WiFi POST's language becomes the session's — the hidden field the
    page carried, if it is an active code; else the same default the page
    was rendered in (a tampered or stale field never fails a join). The
    device setting is written later in do_POST, best-effort (R13); the
    session follows the submit whether or not that write lands."""
    global SESSION_LANGUAGE
    code = params.get("litclock_language", [""])[0].strip()
    if code not in _strings_catalog.active_codes():
        code = _default_language(accept_language)
    SESSION_LANGUAGE = code
    return code


def _language_field():
    """The page's language rides into the WiFi POST, which is what saves it
    (R13: on submit, never on a link click)."""
    if len(_strings_catalog.active_languages()) < 2:
        return ""
    value = html.escape(_catalog().requested, quote=True)
    return f'            <input type="hidden" name="litclock_language" value="{value}">'


# Sentinel <option> value meaning "I'll type the network name myself"
# (litclock-dev#554). Deliberately not a plausible SSID: an access point could
# in principle broadcast this exact name, in which case picking it from the
# list would route the user through the manual text input — which they can
# then use to type that same name. Degrades to one extra step, never to a
# wrong network.
MANUAL_SSID_VALUE = "__litclock_type_it_myself__"
MANUAL_SSID_FIELD = "wifi_ssid_manual"
# litclock-dev#532 final slice — the rest of the page copy. Values are
# repo-trusted plain text interpolated into the HTML f-strings (the 4c
# pattern); the error banners go through _rich below instead, because they
# compose USER-derived text.
# (litclock-dev#532 final-slice /review: page copy resolves PER REQUEST via
# _page_text below, not at module import — a language persisted mid-session
# or carried by the gift marker must reach the retry re-render; frozen
# constants made those renders mixed-language.)


def _page_text(key, /, **slots):
    """Request-time copy lookup, HTML-escaped for safe interpolation into
    both element and attribute contexts (quote=True). English values are
    unchanged by the escape; a future translation carrying quotes or angle
    brackets cannot break the markup."""
    return html.escape(_catalog_get(key, **slots), quote=True)


def _rich(key, /, **slots):
    """Escape-then-compose for the banner copy (mirrors the PWA's t_rich):
    the catalog value is plain text with {b}/{/b} tokens; the value is
    HTML-escaped FIRST, tokens become <strong> tags, then slots fill with
    ESCAPED values — so the user-derived {error} text can never inject
    markup, and a translation can move the emphasis."""
    text = html.escape(_catalog_get(key))
    for token, tag in (("{b}", "<strong>"), ("{/b}", "</strong>")):
        text = text.replace(token, tag)
    for name, value in slots.items():
        text = text.replace("{" + name + "}", html.escape(str(value)))
    return text


# Request-time WiFi-picker copy (litclock-dev#532 follow-up, from the
# Stage-4 gates /review): these three survived the final-slice per-request
# conversion as import-time constants, freezing the picker's manual option
# and placeholders — a language persisted or gift-marker-carried
# mid-session must reach the retry re-render like every other page string.
def _manual_ssid_text():
    return _catalog_get("setup.wifi.manual_option")


def _wifi_placeholder_text():
    return _catalog_get("setup.wifi.placeholder")


def _wifi_placeholder_empty_text():
    return _catalog_get("setup.wifi.placeholder_empty")


def _page_language():
    """The language the setup page is actually written in, for ``<html
    lang>`` (litclock-dev#532 T6): the page's resolution — the session's
    language, or the fleet default when that bundle is unusable — what screen
    readers and translation prompts should be told, never just "en"."""
    try:
        return html.escape(_catalog().language, quote=True)
    except Exception:
        return "en"


def _strength_text(signal):
    """The picker's signal word, from the catalog (litclock-dev#532 T6). The
    JS rescan path applies the same thresholds to the same three keys."""
    if signal >= 70:
        return _catalog_get("setup.wifi.strength_strong")
    if signal >= 40:
        return _catalog_get("setup.wifi.strength_medium")
    return _catalog_get("setup.wifi.strength_weak")


class _ServerFailure(str):
    """A connect-flow failure raised by THIS server rather than by
    wifi_provision, shaped like wifi_provision.WifiFailure: the str is the
    catalog's English for the journal, ``key``/``slots`` what the page shows.
    Its own type, not an import: the retry-flow tests replace wifi_provision
    with a bare fake."""

    __slots__ = ("key", "slots")
    failure_class = "other"

    def __new__(cls, key, **slots):
        # Built inside the connect thread's except arms, so it must not raise
        # itself: an exception here would skip the hotspot restore after it.
        try:
            import strings_catalog  # noqa: PLC0415

            message = strings_catalog.get(key, lang=strings_catalog.CANONICAL_LANGUAGE, **slots)
        except Exception:
            message = key
        if message == key:
            # The catalog's never-raise floor is the bare key; the values are
            # what the journal needs to diagnose the failure, so keep them.
            message = " ".join([key, *(str(value) for value in slots.values())])
        obj = super().__new__(cls, message)
        obj.key = key
        obj.slots = slots
        return obj

    def __getnewargs_ex__(self):
        return (self.key,), self.slots


def _connect_error_text(error):
    """The connect failure in the page's language: its catalog sentence when
    it names one (a WifiFailure from wifi_provision or this server), else the
    text as given. The str form stays English for the journal."""
    key = getattr(error, "key", None)
    if key:
        return _catalog_get(key, **getattr(error, "slots", {}))
    return str(error)


# Last SSID typed into the manual field, echoed back on a retry render. Only
# ever a name the user chose; never a password.
WIFI_LAST_MANUAL_SSID = ""
# 802.11 caps the SSID at 32 BYTES, not characters. Checked before nmcli sees
# it so an over-long paste gets a sentence a recipient can act on rather than
# nmcli's. The user-facing message says "bytes" deliberately: DE and RU are
# top-two referrer geos, and a Cyrillic name is two bytes per character, so
# "32 characters" would be a factually wrong instruction in the one place
# where the user has no keyboard and no way to check.
#
# The manual-SSID input also interpolates this as its maxlength
# (litclock-dev#605 item 12). maxlength counts CHARACTERS (UTF-16 code
# units), so for multi-byte names the client guard is looser than the
# byte budget — that's fine: it exists to stop the all-ASCII 99% case
# before a captive-portal round trip, and the byte check in do_POST is
# the real gate either way.
SSID_MAX_BYTES = 32
# Control characters are rejected outright. An interior newline in a
# hand-typed SSID forges lines in the persistent journal (journald ships
# Storage=persistent on the flashed image, and the support bundle collects
# it) and reaches the e-ink connecting splash. Same class as the env.sh
# splitlines round-trip: reject at the entry point, do not sanitise downstream.
# U+2028/U+2029 ride along with the C0/C1 controls: Python's isprintable()
# treats them as unprintable (the renderer already strips them) but they are
# not in the C1 range, so without this the reject-before-journal comment
# below would overclaim (litclock-dev#636).
SSID_FORBIDDEN = (
    frozenset(chr(c) for c in range(0x20))
    | {chr(0x7F), "\u2028", "\u2029"}
    | frozenset(chr(c) for c in range(0x80, 0xA0))
)

# Thread safety: the server runs in threaded mode so a stuck handler can't
# block new connections. These locks guard state mutated from both request
# threads and the background WiFi connect thread.
_WIFI_CONNECT_LOCK = threading.Lock()
_SCAN_CACHE_LOCK = threading.Lock()

# Registry of background daemon threads this module spawns (WiFi connect /
# teardown, location-resolve, delayed SIGTERM). Production never reaps these —
# they're daemons that die at process exit. It exists SOLELY so the litclock-dev#355
# test-isolation helper :func:`reset_state` can JOIN them between tests
# (litclock-dev#478): the old flag-only drain let a prior test's thread outlive its case
# and fire its (now next-test-monkeypatched) os.kill / retry function, polluting
# the next test's counters. Tracking + joining makes isolation deterministic.
_BG_THREADS: list[threading.Thread] = []
_BG_THREADS_LOCK = threading.Lock()
# litclock-dev#786: how many times ``reset_state`` re-snapshots the registry
# looking for threads appended DURING the join. Bounded so a pathological
# spawner cannot spin the loop; the join_deadline already bounds wall-clock, so
# this only bounds iteration count once the budget is spent and every join
# returns instantly. Four is generous — the registry holds one thread kind.
_RESET_JOIN_MAX_PASSES = 4
# There is deliberately NO cancellation Event here — litclock-dev#785 removed
# one, and this note exists so it does not come back.
#
# ``_BG_CANCEL`` was set by :func:`reset_state` to wake a sleeping ``_delayed``
# SIGTERM timer so it exited WITHOUT firing ``os.kill``. ``_delayed`` was
# DELETED by litclock-dev#715 and nothing waited on the Event afterwards, but
# four comments went on describing it as live — and they actively misled three
# readers in one sitting during the litclock-dev#781 review (one of whom
# proposed ADDING a test that waited on the Event purely so the flag could be
# observed being set, which would have pinned dead machinery against the test's
# own construct).
#
# Reviving it was the other option on the table and was rejected on evidence,
# not taste. Making the tracked thread cooperative means threading cancellation
# checks through ``_connect_and_teardown`` — the first-boot provisioning path,
# whose every branch is commented with which early exit must NOT be taken
# because it would tear down the hotspot or destroy the WiFi link just joined.
# And the only caller that would ever set the flag is :func:`reset_state`,
# which is TEST-ONLY (full-repo grep: no shell script, systemd unit or
# control_server caller). So reviving would add early-exit paths to
# provisioning code that nothing but a test could trigger, to save test
# teardown wall-clock. That is the wrong direction.
#
# The consequence for reading :func:`reset_state`: the one thread kind tracked
# today (``_connect_and_teardown``, the sole ``_spawn_bg`` call site) is
# UNCOOPERATIVE — ``time.sleep(1)`` plus blocking nmcli — so a join budget is
# spent as real wall-clock and cannot be short-circuited. Do not write comments
# here that assume otherwise.


def _spawn_bg(target, name: str) -> threading.Thread:
    """Start a daemon thread and register it so ``reset_state`` can join it.

    Prunes already-finished threads on each spawn so the registry stays bounded
    to the (tiny) set of live background threads.
    """
    t = threading.Thread(target=target, name=name, daemon=True)
    with _BG_THREADS_LOCK:
        _BG_THREADS[:] = [x for x in _BG_THREADS if x.is_alive()]
        _BG_THREADS.append(t)
        # start() INSIDE the lock (/review): reset_state snapshots the registry
        # under the same lock, so it can never observe an appended-but-not-yet-
        # started thread and hit ``RuntimeError: cannot join thread before it is
        # started``. start() returns as soon as the thread is launched, so the
        # child (e.g. _resolve_and_signal spawning _delayed via this helper)
        # only blocks on the lock momentarily — no deadlock.
        t.start()
    return t


# Per-connection read timeout. Captive portal probes from phones can stall
# mid-request (phone drops off the AP, enters a different network, etc.).
# Without this timeout, rfile.read() blocks forever and the handler thread
# leaks until the server is restarted.
HANDLER_TIMEOUT = 15

# Cap POST body size so a misbehaving (or hostile) client can't make the
# server allocate unbounded memory. Setup form posts are ~1KB.
MAX_POST_BODY = 32 * 1024


def reset_state(wait_for_inflight: float = 2.0) -> tuple[str, ...]:
    """Reset all module-level connect-flow state to defaults.

    Returns the NAMES of tracked threads that outran the join budget — empty
    on the normal path. litclock-dev#786 finding 1: this used to return
    ``None``, so the only trace of an escape was a non-empty ``_BG_THREADS``
    and nothing looked. That is exactly backwards, because an escape is the
    one event this machinery exists to prevent: the escapee later fires the
    real ``os.kill`` and kills the runner, and the consequence lands in a
    DIFFERENT test as ``Terminated`` / exit 143 with nothing pointing back
    (the litclock-dev#769 diagnosis, which cost a week). ``tests/conftest.py``
    consumes this and reports it through ``pytest_terminal_summary``.

    Test-isolation helper (litclock-dev#355). The WiFi connect handler spawns a daemon
    thread that writes ``WIFI_CONNECT_ERROR`` and ``WIFI_CONNECT_IN_FLIGHT``
    asynchronously; without an explicit reset between tests, late writes
    from a prior test's thread leak into the next test's assertions and
    cause order-dependent flakes.

    Runs TWO bounded waits, each capped at ``wait_for_inflight`` (litclock-dev#781):

    1. drains ``WIFI_CONNECT_IN_FLIGHT``, so we don't race a still-running
       connect thread that's about to clobber the values we just reset; then
    2. joins every thread in the ``_BG_THREADS`` registry (litclock-dev#478), sharing one
       fresh budget among them — each thread is offered an equal slice of
       whatever remains, so no thread is ever handed ``timeout=0`` while
       budget is left, and the total stays bounded.

    ...then zeroes the connect-flow globals and clears the WiFi scan cache.

    So the WORST case is ``2 * wait_for_inflight``, not ``wait_for_inflight``.
    In the common case neither budget is spent: the drain returns immediately
    and the joins find nothing live, making this a cheap no-op. Idempotent.

    A thread that outruns its slice stays a daemon (dies at process exit) and
    stays REGISTERED so a later reset can retry it — this never hangs.

    Not used by production code paths; safe to call from any test fixture.
    """
    global WIFI_CONNECT_ERROR, WIFI_CONNECT_IN_FLIGHT, WIFI_LAST_MANUAL_SSID, SESSION_LANGUAGE

    SESSION_LANGUAGE = ""
    # EVERY _WIFI_SCAN_* global must be named here, or the reset below binds a
    # local and the module keeps its value — _WIFI_SCAN_ATTEMPT_TIME shipped
    # exactly that way (litclock-dev#932). test_reset_state_clears_every_scan_global
    # checks the whole family by name.
    global _WIFI_SCAN_NETWORKS, _WIFI_SCAN_TIME, _WIFI_SCAN_SSIDS, _WIFI_SCAN_LAST_EMPTY
    global _WIFI_SCAN_ATTEMPT_TIME, _WIFI_SCAN_LAST_FAILED

    import time

    # litclock-dev#781 — the drain and the join below get SEPARATE budgets. They
    # used to share one `deadline`: with WIFI_CONNECT_IN_FLIGHT stuck, the drain
    # burned the whole thing and every join was handed timeout=0, so a tracked
    # thread outlived the reset and later fired its (now un-monkeypatched)
    # os.kill into a subsequent test. Each wait is independently bounded by
    # wait_for_inflight, so the worst case is 2x that -- still bounded, never
    # unbounded, and unchanged in the common case where the drain returns
    # immediately and neither budget is spent.
    drain_deadline = time.monotonic() + max(0.0, wait_for_inflight)
    while WIFI_CONNECT_IN_FLIGHT and time.monotonic() < drain_deadline:
        time.sleep(0.01)

    # litclock-dev#478 — the flag-drain above only covers the connect thread, the one kind
    # that sets WIFI_CONNECT_IN_FLIGHT, so join EVERY tracked background thread
    # too. This is what actually stops a prior test's thread from firing its
    # (now next-test-monkeypatched) os.kill / retry function into the next
    # test's counters.
    #
    # The budget is FRESH, not the drain's leftovers, and it is shared among the
    # threads as an EQUAL SLICE of what remains rather than first-come-first-
    # served (litclock-dev#781). Both halves are load-bearing and each was a
    # separate bug:
    #
    #   * one deadline for drain AND joins meant a stuck drain left every join
    #     with timeout=0 — the original litclock-dev#781 defect;
    #   * a fresh but first-come deadline RELOCATED it: the first live thread
    #     absorbed the whole budget and every later thread still got timeout=0,
    #     reachable with the drain not stuck at all (found by the /review
    #     adversarial pass, reproduced with WIFI_CONNECT_IN_FLIGHT False).
    #     The retention filter below deliberately keeps a live thread across
    #     resets, so a single permanently-stuck thread starved the tail FOREVER.
    #
    # An equal slice fixes both while keeping the SAME total bound: the divisor
    # is the number of threads not yet joined, so a thread that returns early
    # hands what it did not use to the ones behind it, and the loop as a whole
    # still cannot run past join_deadline.
    #
    # NOTE: there is no cancellation signal (litclock-dev#785 removed the inert
    # one). Every slice below is spent as real wall-clock. Do not reason about
    # this loop as though a sleeping thread can be woken out of its join.
    #
    # litclock-dev#786 finding 2: the snapshot is RE-TAKEN. The previous version
    # snapshotted once under the lock and joined only that list, so a request
    # thread that set WIFI_CONNECT_IN_FLIGHT and appended a worker after the
    # drain and after the snapshot was never joined — and the globals were then
    # cleared out from under it, potentially including that worker's own
    # freshly-set in-flight flag. That reopens the duplicate-connect and
    # late-os.kill shapes this helper exists to close.
    #
    # The litclock-dev#781 equal-slice invariant is preserved INSIDE each pass (the
    # divisor is the number not yet joined in that pass, so a thread that
    # returns early hands its remainder to the ones behind it), and the total
    # bound is preserved ACROSS passes because every slice is measured against
    # the one ``join_deadline``. A later pass whose budget is already spent
    # hands out ``timeout=0``, which is the same treatment the tail of a single
    # over-budget pass has always had.
    join_deadline = time.monotonic() + max(0.0, wait_for_inflight)
    joined: set[threading.Thread] = set()  # holds references, so identity is stable
    for _pass in range(_RESET_JOIN_MAX_PASSES):
        with _BG_THREADS_LOCK:
            pending = [t for t in _BG_THREADS if t not in joined]
        if not pending:
            break
        for _i, t in enumerate(pending):
            _unjoined = len(pending) - _i
            t.join(timeout=max(0.0, (join_deadline - time.monotonic()) / _unjoined))
            joined.add(t)

    # Drop only the threads that actually finished; KEEP any that outran the
    # join budget so a later reset can retry joining them (/review). Clearing
    # the whole list up-front would forget a still-live daemon — it could then
    # write the globals after we clear them below, and no future reset could
    # join it. is_alive() also drops any new threads spawned during the join
    # once they finish.
    #
    # The try/finally that used to wrap this block existed ONLY to clear the
    # inert Event, so it went with it (litclock-dev#785). Nothing here needs
    # unwind protection: a join() that raises leaves the registry unpruned,
    # which is the same conservative state a timed-out join leaves.
    with _BG_THREADS_LOCK:
        _BG_THREADS[:] = [t for t in _BG_THREADS if t.is_alive()]
        escaped = tuple(t.name for t in _BG_THREADS)

    with _WIFI_CONNECT_LOCK:
        WIFI_CONNECT_ERROR = None
        WIFI_CONNECT_IN_FLIGHT = False
        WIFI_LAST_MANUAL_SSID = ""
    with _SCAN_CACHE_LOCK:
        _WIFI_SCAN_NETWORKS = None
        _WIFI_SCAN_TIME = 0
        _WIFI_SCAN_SSIDS = frozenset()
        _WIFI_SCAN_LAST_EMPTY = False
        _WIFI_SCAN_ATTEMPT_TIME = 0
        _WIFI_SCAN_LAST_FAILED = False

    # Reported, not raised. A raise here would fail whichever test happened to
    # be finishing rather than the one that leaked, and would turn a diagnostic
    # into a second, differently-misattributed failure.
    return escaped


def _schedule_self_terminate() -> None:
    """Send SIGTERM to the current process, synchronously.

    ``os.kill`` queues SIGTERM in the kernel; the default SIGTERM handler
    terminates the Python interpreter (there is NO graceful handler in
    setup_server — only ``except KeyboardInterrupt`` around serve_forever
    in ``main()``). The caller's 1s sleep before this gives the HTTP
    response time to flush; after termination, first-boot.sh polls
    ``/tmp/litclock-setup-done`` (written by ``signal_completion()``) to
    detect successful handoff.

    litclock-dev#715 removed the ``delay > 0`` daemon-thread arm — its only
    production caller was the retired normal-mode ``_resolve_and_signal``.

    Sequencing invariant (litclock-dev#364): callers that interact with
    ``reset_state()``'s drain barrier (today only
    ``_connect_and_teardown``'s success path) MUST invoke this helper
    BEFORE clearing ``WIFI_CONNECT_IN_FLIGHT``. The drain barrier polls
    IN_FLIGHT and exits when it sees False; if IN_FLIGHT is cleared
    first, drain returns claiming the thread is quiescent while a
    SIGTERM is still pending in the kernel queue.

    Test authors exercising any code path that invokes this helper MUST
    monkeypatch os.kill. See ``tests/test_wifi_retry_flow.py``.
    """
    os.kill(os.getpid(), signal.SIGTERM)


def _scanned_ssids(networks):
    """Case-folded SSIDs from a scan, for "did the radio actually see this?".

    Case-folded because the question being asked is "is the name this person
    typed the same network as one we can see", and a human retyping a name
    off a router label gets the capitalisation wrong constantly.
    """
    return frozenset(n["ssid"].casefold() for n in networks if n.get("ssid"))


def _net_signal(net):
    """Signal strength as an int, treating absent/None/non-numeric as 0.

    Cheap insurance, not a defence against a live threat: today the only
    caller is fed by scan_wifi_networks(), which always emits an int. (An
    earlier version of this docstring claimed the list round-trips through
    /scan-wifi JSON — it does not; that JSON goes to the browser and never
    comes back.) The reason to keep the coercion is that both the sort and
    the Strong/Medium/Weak comparison would raise on a malformed entry, and
    an exception here takes down the whole setup page, which on a captive
    portal leaves the user with no path forward at all.

    Note the deliberate asymmetry with `net["ssid"]` below, which is a bare
    subscript: a network with no name is not renderable at all, so there is
    nothing to degrade to.
    """
    signal = net.get("signal")
    try:
        return int(signal)
    except (TypeError, ValueError):
        return 0


def _selected_attr(flag):
    """``" selected"`` when ``flag``, else nothing — the <option> attribute.

    A function rather than a conditional at each site so the two callers in
    :func:`_build_wifi_options` cannot drift into both selecting, or neither.
    """
    if flag:
        return " selected"
    return ""


def _build_wifi_options(networks, placeholder=None, select_manual=False, scan_failed=False):
    """Convert a list of network dicts into HTML <option> tags.

    Three things beyond the obvious (litclock-dev#554):

    1. A **disabled placeholder leads the list**. A <select> with no `selected`
       option displays its first option, so whatever happened to sort first
       looked like a decision the user had already made — someone tapping
       Submit without opening the dropdown submitted a neighbour's SSID they
       never chose, then hit a WiFi-join failure they had no way to diagnose.
       Paired with `required` on the <select> so the no-JS path refuses an
       empty submit client-side, before a slow captive-portal round trip.
    2. **Sorted here, by signal descending.** nmcli happens to sort that way,
       but that is inherited behaviour we never asked for and never asserted;
       a driver quirk or scan race silently reorders the list. Python's sort
       is stable, so equal-signal networks keep scan order.
    3. A **manual-entry option trails the list**. A hidden SSID never appears
       in a scan, so before this there was no path to join one through setup
       at all. Its non-empty value is what satisfies `required` when the user
       is going to type the name into the paired text input instead.

    ``select_manual`` (litclock-dev#848) pre-selects that manual-entry option
    INSTEAD of the placeholder. Since litclock-dev#848 the text input is only usable
    while the manual option is the dropdown's selection, so any render that
    wants the input usable on arrival — the retry echo of a hand-typed name,
    and the empty scan where typing is the only way forward — has to select
    the option too, or the page would show a field the dropdown says is not
    in play. Exactly one option ever carries ``selected``: the placeholder
    stays ``disabled`` either way so it can never be re-picked or submitted.

    An EMPTY list forces it on regardless of the argument: with no networks
    the manual option is the only one ``required`` accepts, so every caller
    would have to pass the flag and the one that forgot would render a page
    whose only usable control is gated off. One rule, at the site that knows.

    Except when ``scan_failed`` (litclock-dev#908 gap 1): the list is empty
    because the radio never answered, not because nothing is there, so the
    owner's network is almost certainly in range and the right next step is
    Refresh — not a type-it-yourself box opened as if typing were the only
    way forward. The manual option still trails the list for a hidden network.

    The option is marked ``data-manual`` as well. The script selects it by
    that attribute rather than by value, because a scanned AP can broadcast
    the sentinel name and would sort ahead of it (see MANUAL_SSID_VALUE).
    """
    select_manual = select_manual or (not networks and not scan_failed)
    placeholder = html.escape(placeholder or _wifi_placeholder_text())
    # Through the helper, not two inline conditionals. The natural shape for
    # the first of them — an empty-string branch written before the `if` —
    # is a banned substring in tests/test_cross_file_string_parity.py's
    # in-code-grammar guard (litclock-dev#532 item 11), which matches raw
    # text and cannot tell an HTML attribute from a spliced plural suffix.
    # The helper also states the invariant once: the flag picks exactly one
    # of the two options to carry `selected`.
    placeholder_selected = _selected_attr(not select_manual)
    manual_selected = _selected_attr(select_manual)
    options = [f'<option value=""{placeholder_selected} disabled>{placeholder}</option>']
    for net in sorted(networks, key=_net_signal, reverse=True):
        ssid = html.escape(net["ssid"])
        security = net.get("security", "")
        is_open = not security or security == "--"
        # The whole label is a catalog template (litclock-dev#532 T6), so a
        # translation orders name and strength as its language needs; the
        # SSID is escaped above and filled in one pass.
        # Filled with the RAW values and escaped WHOLE, so a translation's
        # own "<" or "&" is escaped like every other page string (litclock-dev#918
        # review); the one-pass fill means a value is never re-read.
        label = html.escape(
            _catalog_get(
                "setup.wifi.option_open" if is_open else "setup.wifi.option",
                ssid=net["ssid"],
                strength=_strength_text(_net_signal(net)),
            )
        )
        options.append(f'<option value="{ssid}">{label}</option>')
    options.append(
        f'<option value="{MANUAL_SSID_VALUE}" data-manual="1"{manual_selected}>'
        f"{html.escape(_manual_ssid_text())}</option>"
    )
    return "\n                    ".join(options)


def _filter_own_hotspot(networks):
    """Drop the clock's own setup hotspot from a scanned network list.

    Connecting the Pi to its own AP is nonsensical, and the hotspot SSID can
    surface in a scan of its own radio. A non-technical tester picked
    "LitClock-Setup" off the dropdown thinking that was the network to choose
    (first-boot QA). No-op outside provisioning mode (HOTSPOT_SSID is None).

    Applied at BOTH scan entry points — the server-rendered dropdown
    (_wifi_network_options) and the /scan-wifi JSON the client uses to rebuild
    the dropdown on Refresh — because they share _WIFI_SCAN_NETWORKS; filtering
    only one path lets the hotspot leak back via the cache or the JSON.
    """
    if not HOTSPOT_SSID:
        return networks
    return [n for n in networks if n.get("ssid") != HOTSPOT_SSID]


def _scan_failed_options(select_manual):
    """The picker after a failed scan (litclock-dev#908 gap 1)."""
    return _build_wifi_options(
        [], placeholder=_catalog_get("setup.wifi.scan_failed"), select_manual=select_manual, scan_failed=True
    )


def _wifi_network_options(select_manual=False, stale_ok=False, fresh_after=None):
    """Generate <option> tags for scanned WiFi networks, with 30s SCAN caching.

    The scan RESULT is cached; the <option> HTML renders per call so the
    localized picker copy tracks the active language (litclock-dev#532).

    Returns ``(options_html, scan_was_empty)``. The flag is explicit
    because the page build used to infer emptiness by sniffing the
    rendered HTML for the placeholder copy (litclock-dev#605 item 11) —
    a check that breaks silently the day the copy gains an escapable
    character.

    ``select_manual`` is forwarded to :func:`_build_wifi_options`, which
    forces it on for an empty scan regardless (litclock-dev#848): on that
    page the manual option is the only one ``required`` will accept, and the
    text input is gated on it being selected.

    ``fresh_after`` (a ``time.monotonic()`` stamp) is the no-JS Refresh
    (litclock-dev#908): the cache counts only if a scan COMPLETED after that
    instant. A stamp rather than a plain "force" flag, so a second Refresh
    that queued on the lock behind the first one's scan takes that scan's
    result — empty included, via _WIFI_SCAN_ATTEMPT_TIME — instead of keeping
    the radio busy for another 2-27s."""
    global _WIFI_SCAN_NETWORKS, _WIFI_SCAN_TIME, _WIFI_SCAN_SSIDS, _WIFI_SCAN_LAST_EMPTY, _WIFI_SCAN_ATTEMPT_TIME
    global _WIFI_SCAN_LAST_FAILED
    import time

    # Take _SCAN_CACHE_LOCK across the whole cache-miss path, exactly like
    # /scan-wifi does. This is the second scan entry point that shares
    # _WIFI_SCAN_NETWORKS/_SSIDS/_TIME, and it was previously unlocked
    # (litclock-dev#615). Consequences of the gap on a threaded server:
    #   - two concurrent GET / builds at a cold/expired cache each fired their
    #     own nmcli rescan on the single hotspot radio — the exact contention
    #     the lock exists to prevent (an iOS CNA sheet + the real browser is a
    #     realistic pair within the 30s TTL);
    #   - interleaving with /scan-wifi could leave _WIFI_SCAN_NETWORKS from one
    #     scan and _WIFI_SCAN_SSIDS from another, so wifi_hidden in do_POST is
    #     computed against evidence that doesn't match the dropdown the user
    #     saw — which can flip a VISIBLE typed SSID to `hidden yes`, the
    #     permanent probe-request leak litclock-dev#580's policy is meant to prevent;
    #   - the cache-hit branch double-read _WIFI_SCAN_NETWORKS unlocked, so a
    #     concurrent reset to None could interpolate "None" into the <select>.
    # A waiter that blocks here through another thread's scan finds the fresh
    # cache on the recheck and skips its own scan entirely.
    with _SCAN_CACHE_LOCK:
        now = time.monotonic()
        if fresh_after is not None and _WIFI_SCAN_ATTEMPT_TIME >= fresh_after:
            # A scan finished after this request asked: show what it found.
            if _WIFI_SCAN_LAST_EMPTY or _WIFI_SCAN_NETWORKS is None:
                return _build_wifi_options([], placeholder=_wifi_placeholder_empty_text()), True
            return _build_wifi_options(_WIFI_SCAN_NETWORKS, select_manual=select_manual), False
        if stale_ok and _WIFI_SCAN_LAST_FAILED:
            return _scan_failed_options(select_manual), False
        if stale_ok and _WIFI_SCAN_LAST_EMPTY:
            return _build_wifi_options([], placeholder=_wifi_placeholder_empty_text()), True
        # stale_ok: a language switch re-renders the networks already on
        # the reader's screen rather than block 2-20s on the radio
        # (litclock-dev#532 D10, eng re-review Section 1 #1).
        fresh = fresh_after is None and (
            _WIFI_SCAN_NETWORKS is not None and (stale_ok or (now - _WIFI_SCAN_TIME) < _WIFI_SCAN_TTL)
        )
        if fresh:
            # Render fresh from the cached scan — no radio contention, and
            # the picker copy follows a language switched since the scan.
            return _build_wifi_options(_WIFI_SCAN_NETWORKS, select_manual=select_manual), False

        try:
            from wifi_provision import scan_wifi_networks

            networks = scan_wifi_networks()
        except Exception as exc:
            # A FAILED scan, not an empty one (litclock-dev#908 gap 1): say so,
            # and touch none of the scan state. Nothing is cached, the
            # evidence set and LAST_EMPTY keep what earlier scans found, and
            # the attempt stamp does not move, so a request queued behind
            # this one tries the radio again rather than inheriting a failure.
            print(f"WiFi scan failed: {exc}", flush=True)
            _WIFI_SCAN_LAST_FAILED = True
            return _scan_failed_options(select_manual), False

        networks = _filter_own_hotspot(networks)
        _WIFI_SCAN_ATTEMPT_TIME = time.monotonic()
        _WIFI_SCAN_LAST_FAILED = False

        if not networks:
            # Don't cache empty results — let the next call retry. Still built via
            # _build_wifi_options so the manual-entry option survives: a user whose
            # only network is hidden sees an empty scan every time, and dropping
            # their one path to type it in would strand them (litclock-dev#554).
            # _WIFI_SCAN_SSIDS deliberately keeps its previous contents: an empty
            # rescan never clears the evidence, and the thing it guards against —
            # writing `hidden yes` into the saved profile — is permanent while
            # the staleness is transient (see the wifi_hidden decision in
            # do_POST; litclock-dev#605 item 7, refined to UNION by litclock-dev#615).
            # select_manual is not passed: _build_wifi_options forces it on
            # for an empty list, which is exactly this branch.
            _WIFI_SCAN_LAST_EMPTY = True
            return _build_wifi_options([], placeholder=_wifi_placeholder_empty_text()), True

        _WIFI_SCAN_LAST_EMPTY = False
        _WIFI_SCAN_NETWORKS = networks
        # UNION, not replace (litclock-dev#615): accumulate every SSID seen this
        # provisioning session. Scans flicker — a network that is genuinely
        # visible can be absent from one scan — and a replace would drop it,
        # flipping a typed, previously-seen name to a permanent `hidden yes`
        # probe-request leak. Union can only ever SUPPRESS `hidden yes`, never
        # add one, so it fails in the no-leak direction. reset_state() clears the
        # set at the session boundary; SSIDs in radio range are bounded, so the
        # accumulation cannot grow without limit.
        _WIFI_SCAN_SSIDS = _WIFI_SCAN_SSIDS | _scanned_ssids(networks)
        # Stamp AFTER the scan, like /scan-wifi does. `now` was measured before a
        # scan that can take seconds (rescan + 2s settle + list); stamping it
        # would make the cache born up to that much closer to expiry — a slow
        # scan near the TTL could yield an already-expired cache that the next
        # caller immediately rescans, defeating the serialization (litclock-dev#615).
        _WIFI_SCAN_TIME = time.monotonic()
        return _build_wifi_options(networks, select_manual=select_manual), False


# Path serving the CNA bridge on our own host — the target of the Apple
# detection-probe 302 Location (litclock-dev#526). Deliberately NOT
# accompanied by a WISPr XML block: MessageType 100 / ResponseCode 0 is an
# active auto-login solicitation, and a WISPr smart client POSTing stored
# credentials at this server would get a spec-invalid answer that a naive
# client can read as "auth succeeded" — suppressing the very sheet the 302
# is meant to raise. The plain redirect-to-own-host is the pattern
# commercial portals use and the only one confirmed still auto-popping on
# iOS 26.x.
CNA_PATH = "/cna"
CNA_URL = f"http://{HOTSPOT_GATEWAY_IP}{CNA_PATH}"

# ── Page styling (litclock-dev#909) ─────────────────────────────────────────
#
# The setup pages are the first thing a new owner or gift recipient sees, so
# they wear the PWA's design system (DESIGN.md): paper, ink, foxed amber,
# Fraunces headings, Instrument Sans body, no cards or shadows. The hotspot
# has no internet, so the fonts are the PWA's own self-hosted woff2 files,
# served by this server from SETUP_FONT_DIR under /fonts/.
#
# The CSS is INLINED into every page rather than linked: a render-blocking
# stylesheet request is one more fetch a captive-portal WebView can stall on,
# and the bridge must stay JS-free and tiny (test_bridge_is_small). Fonts are
# the one external fetch, and font-display:swap means the page paints at once
# in the fallback stack and never waits on them.
#
# The colour values duplicate src/control_server/static/css/tokens.css by
# necessity (that file is not served here); test_setup_page_style pins them
# to it so a palette change cannot leave the setup pages behind.
SETUP_FONT_DIR = Path(__file__).resolve().parent / "control_server" / "static" / "fonts"
# Allowlist, not a directory listing: /fonts/<name> serves exactly these.
SETUP_FONTS = (
    "fraunces-wght-normal.woff2",
    "fraunces-wght-italic.woff2",
    "instrument-sans-wght-normal.woff2",
)
SETUP_FONT_PATH = "/fonts/"


def _font_name(path):
    """The allowlisted font a /fonts/ request names, or "" — an exact match
    against SETUP_FONTS, so no path the client sends reaches the filesystem."""
    if path.startswith(SETUP_FONT_PATH):
        name = path[len(SETUP_FONT_PATH):]
        if name in SETUP_FONTS:
            return name
    return ""


def _font_face(family, filename, weight, style="normal"):
    return (
        f'@font-face{{font-family:"{family}";src:url({SETUP_FONT_PATH}{filename}) format("woff2");'
        f"font-style:{style};font-weight:{weight};font-display:swap}}"
    )


# DESIGN.md colour tokens, light and dark (dark is designed, not inverted).
# One table so the bridge can take a subset and the parity test can read it.
SETUP_TOKENS = {
    "bg": ("#FBF6EC", "#14110D"),
    "surface": ("#F4ECDC", "#1F1A14"),
    "ink": ("#1A1410", "#F0E6D2"),
    "ink-muted": ("#6B5C4A", "#9A8B73"),
    "hairline": ("rgba(26,20,16,.08)", "rgba(240,230,210,.1)"),
    "accent": ("#B85C20", "#D88150"),
    "accent-muted": ("#8C4318", "#B85C20"),
    "success": ("#4F6E3A", "#8FAB6B"),
    "error": ("#9A2E22", "#C26E5E"),
}
_SERIF_STACK = '"Fraunces","Iowan Old Style","Apple Garamond",Georgia,serif'
_SANS_STACK = '"Instrument Sans",-apple-system,BlinkMacSystemFont,system-ui,sans-serif'


def _tokens_css(names):
    light = ";".join(f"--{n}:{SETUP_TOKENS[n][0]}" for n in names)
    dark = ";".join(f"--{n}:{SETUP_TOKENS[n][1]}" for n in names)
    return (
        f"html{{color-scheme:light dark}}:root{{{light}}}"
        f"@media(prefers-color-scheme:dark){{:root{{{dark}}}}}"
    )


# The body/heading base both the bridge and the full pages use — minified,
# because the bridge carries it inside its size budget.
_BASE_RULES_CSS = (
    "*{box-sizing:border-box}"
    f"body{{margin:0;background:var(--bg);color:var(--ink);font-family:{_SANS_STACK};"
    "font-size:clamp(15px,1rem,20px);line-height:1.5;-webkit-text-size-adjust:100%}"
    f"h1{{font-family:{_SERIF_STACK};font-weight:400;font-size:clamp(26px,1.9375rem,38px);"
    "line-height:1.15;margin:0 0 8px}"
)

# Everything the full pages share: all three faces, every token, the base.
_SETUP_BASE_CSS = (
    _font_face("Fraunces", SETUP_FONTS[0], "100 900")
    + _font_face("Fraunces", SETUP_FONTS[1], "100 900", "italic")
    + _font_face("Instrument Sans", SETUP_FONTS[2], "400 700")
    + _tokens_css(SETUP_TOKENS)
    + _BASE_RULES_CSS
    + "a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,"
    "summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}"
)

# The bridge's subset: only the tokens it paints with, and NO @font-face.
# The bridge is also served under captive.apple.com (_serve_apple_probe), where
# a relative /fonts/ URL resolves to Apple's host and lands back in the probe
# handler — two extra bridge renders per sheet open, logged as if the sheet had
# loaded three times. It has been subresource-free since litclock-dev#175 for exactly this
# kind of reason; the bridge is iOS-only, so the stacks' "Iowan Old Style" and
# system fallbacks are what it shows, and the families are named first so an
# installed copy still wins. Also keeps translated copy inside the 2 KB budget.
_BRIDGE_BASE_CSS = _tokens_css(("bg", "ink", "ink-muted", "accent")) + _BASE_RULES_CSS

# The full pages' rules (setup, success, error). Readable, not minified:
# these pages have no size budget, and this is the CSS people will edit.
_SETUP_PAGE_CSS = """
        main {
            max-width: 480px;
            margin: 0 auto;
            padding: 48px 16px 32px;
        }
        .lang-links {
            font-size: clamp(13px, 0.875rem, 17px);
            color: var(--ink-muted);
            margin: -24px 0 32px;
        }
        .lang-links a { color: var(--ink); }
        .lang-links [aria-current] { font-weight: 600; }
        .subtitle {
            color: var(--ink-muted);
            margin: 0 0 32px;
        }
        .banner {
            background: var(--surface);
            border-top: 2px solid var(--accent);
            border-radius: 10px;
            padding: 12px 16px;
            margin: 0 0 24px;
            font-size: clamp(13px, 0.875rem, 17px);
        }
        .banner--error { border-top-color: var(--error); }
        .section { margin-bottom: 24px; }
        .eyebrow {
            font-size: clamp(11px, 0.75rem, 18px);
            font-weight: 600;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            color: var(--ink-muted);
            margin: 0 0 8px;
        }
        label {
            display: block;
            font-size: clamp(13px, 0.875rem, 17px);
            font-weight: 500;
            color: var(--ink-muted);
            margin: 0 0 4px;
        }
        input[type="text"], input[type="password"], select {
            font: inherit;
            font-size: max(16px, 1rem);
            color: var(--ink);
            background: var(--surface);
            /* --ink-muted, not the PWA's --hairline: an 8% edge on a fill
               1.09:1 off the page fails WCAG's 3:1 for a control outline, and
               this is the one form a first-time owner fills in unaided
               (litclock-dev#909 review). */
            border: 1px solid var(--ink-muted);
            border-radius: 4px;
            padding: 12px;
            min-height: 48px;
            width: 100%;
            margin: 0 0 16px;
        }
        select {
            appearance: none;
            -webkit-appearance: none;
            padding-right: 36px;
            background-image: linear-gradient(45deg, transparent 50%, var(--ink-muted) 50%),
                linear-gradient(135deg, var(--ink-muted) 50%, transparent 50%);
            background-position: calc(100% - 20px) 50%, calc(100% - 15px) 50%;
            background-size: 5px 5px;
            background-repeat: no-repeat;
        }
        input::placeholder { color: var(--ink-muted); opacity: 0.7; }
        .picker {
            display: flex;
            gap: 8px;
            margin: 0 0 16px;
        }
        .picker select { flex: 1; min-width: 0; margin: 0; }
        .btn {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            font: inherit;
            font-weight: 600;
            min-height: 48px;
            padding: 8px 16px;
            border-radius: 10px;
            border: 1px solid transparent;
            cursor: pointer;
            text-decoration: none;
            transition: background 100ms ease-out, color 100ms ease-out;
        }
        .btn-primary {
            width: 100%;
            min-height: 52px;
            font-size: clamp(18px, 1.25rem, 25px);
            background: var(--accent);
            color: var(--bg);
        }
        .btn-primary:hover, .btn-primary:active { background: var(--accent-muted); }
        /* Text in --accent-muted on light paper: --accent at this size is
           4.25:1, under AA's 4.5:1. Dark mode's --accent already clears it
           (and its --accent-muted would not), so it keeps --accent. */
        .btn-secondary {
            background: transparent;
            color: var(--accent-muted);
            border-color: var(--accent);
        }
        .btn-secondary:hover, .btn-secondary:active { color: var(--ink); }
        @media (prefers-color-scheme: dark) {
            .btn-secondary { color: var(--accent); }
        }
        .help-text {
            font-size: clamp(13px, 0.875rem, 17px);
            color: var(--ink-muted);
            margin: 0 0 16px;
        }
        .help-text--after { text-align: center; margin: 12px 0 0; }
        details { margin: 0 0 16px; }
        summary {
            cursor: pointer;
            font-size: clamp(13px, 0.875rem, 17px);
            color: var(--ink-muted);
            /* display stays list-item: Chromium and Gecko draw the
               disclosure triangle only on a list-item summary, and with JS
               off that triangle is the only cue the line can be tapped. */
            padding: 12px 0;
        }
        details .help-text { margin: 0 0 8px; }
        .rows { margin: 0 0 24px; border-top: 1px solid var(--hairline); }
        /* The success page's one instruction — what to do once the phone
           loses this page — so it reads at body size in ink, not as help. */
        .next-step { margin: 0; }
        .row {
            display: flex;
            justify-content: space-between;
            gap: 16px;
            padding: 12px 0;
            border-bottom: 1px solid var(--hairline);
            font-size: clamp(13px, 0.875rem, 17px);
        }
        .row__label { color: var(--ink-muted); }
        .row__value { overflow-wrap: anywhere; text-align: end; }
        .rule-error {
            border: 0;
            border-top: 2px solid var(--error);
            width: 48px;
            margin: 0 0 16px;
        }
        h1.h1--quiet { font-style: italic; }
        .is-loading { pointer-events: none; opacity: 0.45; }
        @media (prefers-reduced-motion: reduce) {
            .btn { transition: none; }
        }
"""

def _setup_head_css():
    """The <style> element the full setup pages share."""
    return "<style>" + _SETUP_BASE_CSS + _SETUP_PAGE_CSS + "    </style>"


# theme-color tints the phone's status bar to --bg in either scheme.
_SETUP_HEAD_META = (
    '<meta name="theme-color" content="#FBF6EC" media="(prefers-color-scheme: light)">\n'
    '    <meta name="theme-color" content="#14110D" media="(prefers-color-scheme: dark)">\n'
    f'    <link rel="preload" href="{SETUP_FONT_PATH}{SETUP_FONTS[0]}" as="font" type="font/woff2" crossorigin>\n'
    f'    <link rel="preload" href="{SETUP_FONT_PATH}{SETUP_FONTS[2]}" as="font" type="font/woff2" crossorigin>'
)


# Built PER REQUEST since the litclock-dev#532 final-slice review — the copy
# must follow a mid-session language switch, and the ~1 KB rebuild is cheap
# even in iOS CNA probe bursts.
#
# iOS CNA opens probe responses in a sandboxed WebView sheet with a short
# timeout and poor JS support. Returning the full setup page (large, inline
# CSS, scan-wifi JS) makes iOS silently give up and bury the captive portal
# in Control Center. This page is ~1 KB, has no JavaScript, and bridges the
# user into the real setup form via a single tap. (Since litclock-dev#526
# the sheet is popped by the detection-probe 302, not by this page — the
# bridge renders INSIDE the risen sheet, where the small/JS-free constraints
# still apply.) The small-text fallback also prints the raw gateway IP so
# the user has a recovery path if the hostname ever fails to resolve.
def _cna_bridge_html():
    """Build the CNA bridge PER REQUEST (litclock-dev#532 final-slice
    /review): copy resolves at render time, so a language persisted or
    marker-carried mid-session reaches the sheet — import-time constants
    froze the language and made retry renders mixed-language."""
    title_text = _catalog_get("setup.page.title")
    body_text = _catalog_get("setup.cna.body")
    cta_text = _catalog_get("setup.cna.cta")
    return (

    "<!DOCTYPE html>"
    '<html lang="' + _page_language() + '"><head>'
    '<meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    f"<title>{title_text}</title>"
    # The shared tokens and faces, plus the bridge's own few rules — all
    # inside the ~2 KB budget (litclock-dev#909).
    "<style>" + _BRIDGE_BASE_CSS +
    "body{padding:48px 16px}"
    "p{color:var(--ink-muted);margin:0 0 24px}"
    "a.btn{display:block;background:var(--accent);color:var(--bg);font-size:18px;"
    "font-weight:600;padding:14px 16px;border-radius:10px;text-align:center;text-decoration:none}"
    "small{display:block;margin-top:24px;color:var(--ink-muted);font-size:13px}"
    "</style>"
    "</head><body>"
    f"<h1>{title_text}</h1>"
    f"<p>{body_text}</p>"
    # The CTA targets the raw gateway IP, not SETUP_HOSTNAME — same
    # DNS-distrust rationale as the /cna redirect (litclock-dev#526): a tap
    # inside the CNA sheet must work even if the sheet's fetches bypass the
    # hotspot's wildcard DNS. The hostname stays in the small print for
    # humans typing a URL into a browser.
    f'<a class="btn" href="http://{HOTSPOT_GATEWAY_IP}/setup">{cta_text}</a>'
    "<small>"
    + _catalog_get("setup.cna.small", host=SETUP_HOSTNAME, ip=HOTSPOT_GATEWAY_IP)
    + "</small>"
    "</body></html>"
    )


def _build_cna_bridge_html():
    """Return the iOS CNA bridge HTML (built per request since the
    litclock-dev#532 final-slice review — see _cna_bridge_html), in the
    session's language (RT3)."""
    with _page():
        return _cna_bridge_html()


def _build_setup_html(accept_language=None, *, stale_scan_ok=False, rescan_after=None):
    """The setup page, resolved once in the setup session's language
    (litclock-dev#532 RT3). ``stale_scan_ok`` renders from the cached scan
    whatever its age — a language switch must cost one render, not a 2-20s
    radio scan (eng re-review Section 1 #1)."""
    with _page(accept_language):
        return _setup_html_body(accept_language, stale_scan_ok=stale_scan_ok, rescan_after=rescan_after)


def _setup_html_body(accept_language, *, stale_scan_ok=False, rescan_after=None):
    """Build the setup page HTML.

    Every state this page (and the flow around it) can be in is specified in
    docs/setup-portal-states.md (litclock-dev#908) — update it with any change
    to what the owner sees.

    Branches on three pieces of module state:

    - ``PROVISIONING_MODE`` — gates the entire WiFi-picker section AND
      picks the page subtitle (litclock-dev#398). In provisioning the subtitle
      orients the user on joining their own WiFi; in the pre-connected
      path (boot with WiFi already configured via ethernet/wpa_supplicant)
      the form is essentially a confirmation button and the subtitle
      drops the WiFi framing to avoid reading wrong.
    - ``WIFI_CONNECT_IN_FLIGHT`` — paints the blue "Connecting…" banner
      with an auto-refresh meta tag. Also shifts the subtitle to a
      "joining your network" register so it doesn't read as a stale
      instruction while the user is already waiting.
    - ``WIFI_CONNECT_ERROR`` — paints the red error banner with the
      hotspot-vs-WiFi password disambiguation copy.

    The hotspot SSID name in the picker-section explainer is sourced
    from ``HOTSPOT_SSID`` (set via the ``--hotspot-ssid`` CLI flag,
    defaults to ``wifi_provision.DEFAULT_SSID`` = "LitClock-Setup") so
    branded / customized builds don't display copy that lies to the
    user about which network is the temporary one.
    """
    # Request-time copy (litclock-dev#532 final-slice /review). Keys the
    # page f-strings consume as {c[...]}; all attribute-safe via _page_text.
    c = {
        "title": _page_text("setup.page.title"),
        "wifi_icon": _page_text("setup.wifi.icon_label"),
        "pick_label": _page_text("setup.wifi.pick_label"),
        "refresh": _page_text("setup.wifi.refresh"),
        "manual_placeholder": _page_text("setup.wifi.manual_placeholder"),
        "password_label": _page_text("setup.wifi.password_label"),
        "password_placeholder": _page_text("setup.wifi.password_placeholder"),
        "password_help": _page_text("setup.wifi.password_help"),
        "submit": _page_text("setup.submit"),
        "submit_help": _page_text("setup.submit_help"),
    }
    wifi_error_banner = ""
    if WIFI_CONNECT_IN_FLIGHT:
        wifi_error_banner = (
            '<div class="banner" role="status">'
            + _rich("setup.banner.connecting") +
            "</div>"
            '<meta http-equiv="refresh" content="10;url=/setup">'
        )
    elif WIFI_CONNECT_ERROR:
        # HTML-escape the raw error. The banner is interpolated into the
        # f-string template via `{wifi_error_banner}`, NOT .format(), so we
        # must NOT double braces — f-string interpolation is non-recursive
        # and doubled braces would leak into the rendered HTML as literal
        # `{{` `}}`. (Earlier copies of this code doubled braces defensively
        # for a .format() call site that no longer exists.)
        # Snapshot the global once (litclock-dev#610 review): the connect
        # thread writes it, and reading it twice — once for the message,
        # once for the class — could pair an old message with new-class
        # advice across a mid-render write.
        connect_error = WIFI_CONNECT_ERROR
        # litclock-dev#603 — the advice line must not contradict the honest
        # cause {safe_error} just gave. Password advice only when the failure
        # actually WAS the password (the class rides on the WifiFailure
        # error string); a timeout or not-found gets a neutral retry line,
        # because "double-check your password" after "the network didn't
        # answer in time" reads as the page arguing with itself.
        if getattr(connect_error, "failure_class", None) == "bad_password":
            advice = _rich("setup.banner.advice_password")
        else:
            advice = _rich("setup.banner.advice_neutral")
        wifi_error_banner = (
            '<div class="banner banner--error" role="alert">'
            + _rich("setup.banner.error_lead", error=_connect_error_text(connect_error)) + "<br>"
            + advice
            + _rich("setup.banner.error_tail") + "</div>"
        )

    # Subtitle copy depends on two states (litclock-dev#715 removed the
    # pre-connected normal-mode page — the server only serves provisioning):
    #   - in-flight: user already submitted, page is auto-refreshing —
    #     subtitle should reflect "joining", not "go pick a network".
    #   - default / error retry: orient on joining their own WiFi.
    # Both branches use hardcoded literals — no user input flows in, so the
    # f-string interpolation at the bottom of this function does not need
    # html.escape.
    if WIFI_CONNECT_IN_FLIGHT:
        subtitle_text = _page_text("setup.subtitle.joining")
    else:
        subtitle_text = _page_text("setup.subtitle.default")

    # The no-JS Refresh's own form (litclock-dev#908). Empty of fields on
    # purpose: a GET carries everything in the URL, so it must never be the
    # form holding the password. The session language rides on the server
    # side (SESSION_LANGUAGE), so it needs no hidden field either.
    rescan_form = (
        '        <form id="rescan-form" method="get" action="/setup">'
        '<input type="hidden" name="rescan" value="1"></form>\n'
        if PROVISIONING_MODE
        else ""
    )
    language_links = _language_links() if PROVISIONING_MODE else ""
    language_field = _language_field() if PROVISIONING_MODE else ""
    wifi_section = ""
    if PROVISIONING_MODE:
        # A retry that echoes a hand-typed name also pre-selects the manual
        # option (litclock-dev#848): the text input is only usable while that
        # option is the dropdown's selection, so an echoed value under a
        # dropdown still on its placeholder would be a field the page then
        # hides — and the no-JS user would be asked to re-pick an option
        # they had already picked once.
        # ONE read, into a local. The two reads this replaced straddled
        # _wifi_network_options — which can spend seconds inside an nmcli
        # rescan — so a connect thread storing or clearing the echo in that
        # window produced a page whose dropdown and text box disagreed: the
        # manual option selected over an empty box, or a filled box under a
        # placeholder. Same reasoning as the connect_error snapshot above.
        with _WIFI_CONNECT_LOCK:
            last_manual = WIFI_LAST_MANUAL_SSID
        network_options, scan_was_empty = _wifi_network_options(
            select_manual=bool(last_manual), stale_ok=stale_scan_ok, fresh_after=rescan_after
        )
        # Source the hotspot SSID from the runtime constant rather than
        # hardcoding "LitClock-Setup" — branded builds set this via the
        # --hotspot-ssid CLI flag, and the disambiguating cue ("Not the
        # X hotspot") fails exactly when it's most needed if X is wrong.
        # html.escape because a custom SSID could in theory contain HTML
        # special chars; wifi_provision.DEFAULT_SSID is plain ASCII but
        # we don't enforce that on the CLI override.

        # Echo the last hand-typed SSID back, and open the disclosure, when a
        # retry is showing or the scan came back empty. Without this the retry
        # page is blank and re-collapsed: the hidden-network user has to
        # remember what they typed while a red banner tells them to check their
        # PASSWORD, when the likelier fault on that path is the SSID spelling
        # (/review, litclock-dev#580). The echo covers JOIN failures only: a value the
        # server rejected outright (malformed, the clock's own hotspot name,
        # or a POST that lost the in-flight race) is deliberately never
        # stored, so those paths re-render blank (litclock-dev#605 item 10 — see the
        # store site in do_POST).
        manual_value = html.escape(last_manual)
        manual_open = " open" if (manual_value or scan_was_empty or WIFI_CONNECT_ERROR) else ""
        manual_summary = html.escape(_manual_ssid_text())
        # Composed rich help lines: {network}/{summary} slots escape-filled.
        wifi_help_html = _rich("setup.wifi.help", network=HOTSPOT_SSID or "LitClock-Setup")
        manual_help_html = _rich("setup.wifi.manual_help", summary=_manual_ssid_text())
        wifi_section = f"""
            <!-- WiFi Section -->
            <div class="section">
                <p class="eyebrow">{c['wifi_icon']}</p>
                <p class="help-text">
                    {wifi_help_html}
                </p>

                <label for="wifi-ssid">{c['pick_label']}</label>
                <div class="picker">
                    <select id="wifi-ssid" name="wifi_ssid" required
                        onchange="onSsidChange()">
                        {network_options}
                    </select>
                    <!-- A real submit for #rescan-form (outside this form,
                         so no password ever rides a GET): with JS off it
                         reloads /setup?rescan=1, which rescans server-side
                         (litclock-dev#908). With JS on, refreshNetworks()
                         rescans in place and returns false to cancel it. -->
                    <button type="submit" form="rescan-form" class="btn btn-secondary"
                        onclick="return refreshNetworks()">{c['refresh']}</button>
                </div>

                <!-- Hidden-SSID path. A <details> rather than a JS-revealed
                     field so it still opens with JavaScript disabled, which is
                     the norm inside captive-portal WebViews.

                     litclock-dev#848: with JavaScript ON, syncManualSsid()
                     below hides this whole disclosure unless the dropdown's
                     selection is the manual option, and blanks the input the
                     moment a real network is picked — so the two can never be
                     filled at once. The server NEVER renders it hidden: that
                     is the no-JS fallback, where the field stays visible as it
                     always was and the server-side precedence rule in do_POST
                     (a picked network wins over typed text) is the guard.

                     Force-opened when a retry is showing or the scan came back
                     empty: in both states typing the name is the likeliest (in
                     the empty-scan case, the ONLY) way forward, and leaving the
                     instructions folded inside a disclosure the user has to
                     discover for themselves is how a hidden-network owner gets
                     stuck in a loop (/review, litclock-dev#580). -->
                <details id="manual-ssid"{manual_open}>
                    <summary>
                        {manual_summary}
                    </summary>
                    <p class="help-text">
                        {manual_help_html}
                    </p>
                    <input type="text" id="wifi-ssid-manual" name="{MANUAL_SSID_FIELD}"
                        aria-label="{c['manual_placeholder']}"
                        placeholder="{c['manual_placeholder']}" autocomplete="off"
                        autocapitalize="none" autocorrect="off" spellcheck="false"
                        value="{manual_value}" maxlength="{SSID_MAX_BYTES}">
                </details>

                <label for="wifi-password">{c['password_label']}</label>
                <input type="password" id="wifi-password" name="wifi_password"
                    placeholder="{c['password_placeholder']}" autocomplete="off">
                <p class="help-text">
                    {c['password_help']}
                </p>
            </div>
"""

    # json.dumps, not a bare quoted literal: these strings end up inside a
    # <script> block, and it emits correctly escaped JS string literals for
    # the apostrophes and any future non-ASCII in the placeholder copy.
    manual_value_js = json.dumps(MANUAL_SSID_VALUE)
    placeholder_js = json.dumps(_wifi_placeholder_text())
    placeholder_empty_js = json.dumps(_wifi_placeholder_empty_text())
    manual_text_js = json.dumps(_manual_ssid_text())
    # litclock-dev#532 T6: the rescan path's copy, in the page's language. The
    # JS fills an option template as the server does — {strength} first and
    # the SSID LAST, so a network name that reads like a slot is never filled
    # again (the one-pass rule strings_catalog._fill keeps).
    scanning_js = json.dumps(_catalog_get("setup.wifi.scanning"))
    scan_failed_js = json.dumps(_catalog_get("setup.wifi.scan_failed"))
    option_js = json.dumps(_catalog_get("setup.wifi.option"))
    option_open_js = json.dumps(_catalog_get("setup.wifi.option_open"))
    strength_js = json.dumps(
        [
            _catalog_get("setup.wifi.strength_strong"),
            _catalog_get("setup.wifi.strength_medium"),
            _catalog_get("setup.wifi.strength_weak"),
        ]
    )

    return f"""<!DOCTYPE html>
<html lang="{_page_language()}">
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta charset="UTF-8">
    <title>{c['title']}</title>
    {_SETUP_HEAD_META}
    {_setup_head_css()}
</head>
<body>
    <main>
{language_links}        <h1>{c['title']}</h1>
        <p class="subtitle">{subtitle_text}</p>

{wifi_error_banner}
        <form id="setup-form" method="POST" action="/setup">
{language_field}
{wifi_section}
            <!-- Submit -->
            <button type="submit" class="btn btn-primary" id="submit-btn">
                {c['submit']}
            </button>
            <p class="help-text help-text--after">
                {c['submit_help']}
            </p>
        </form>
{rescan_form}    </main>

    <script>
        var MANUAL_SSID_VALUE = {manual_value_js};

        // Rebuild the dropdown to the same shape the server renders: disabled
        // placeholder first so nothing is pre-selected, manual-entry option
        // last so a hidden network stays reachable after a Refresh
        // (litclock-dev#554). Dropping either on this path would have let the
        // client silently undo the server's fix.
        function resetSsidOptions(select, placeholderText) {{
            select.innerHTML = '';
            var ph = document.createElement('option');
            ph.value = '';
            ph.selected = true;
            ph.disabled = true;
            ph.textContent = placeholderText;
            select.appendChild(ph);
        }}

        function appendManualOption(select) {{
            var opt = document.createElement('option');
            opt.value = MANUAL_SSID_VALUE;
            // Mirrors the server-rendered option's data-manual: the marker is
            // how selectManualOption finds it, and both builders have to set
            // it or a Refresh would lose the identity mid-session.
            opt.dataset.manual = '1';
            opt.textContent = {manual_text_js};
            select.appendChild(opt);
        }}

        // Find the manual option by its MARKER, not by its value. An AP can
        // broadcast the sentinel name (MANUAL_SSID_VALUE says so), and it
        // renders among the scanned networks — ahead of the real manual
        // option — so `select.value = MANUAL_SSID_VALUE` would select the
        // NEIGHBOUR'S AP. The submit still reaches the typed name, because the
        // server reads that submitted value as the sentinel either way, so the
        // damage is a dropdown naming a network other than the one being
        // joined: the exact "which of these two did I choose?" confusion
        // litclock-dev#848 exists to remove.
        function manualOption(select) {{
            var opts = select.options;
            for (var i = 0; i < opts.length; i++) {{
                if (opts[i].dataset && opts[i].dataset.manual) return opts[i];
            }}
            return null;
        }}

        // The manual-SSID field is usable ONLY while the dropdown's selection
        // is the manual option (litclock-dev#848). Anything else hides the
        // whole disclosure, and a real network pick also blanks the typed
        // name — a stale entry under a hidden field is exactly the "both
        // filled at once" state the issue is about. The server-side rule in
        // do_POST (a picked network wins over typed text) stays as the
        // backstop; this makes the page agree with it. Called at load, on
        // every dropdown change, and after every Refresh rebuild.
        //
        // The placeholder case ('' — nothing picked yet) hides without
        // blanking: a Refresh lands there, and a name typed before the
        // Refresh is not a decision the user reversed.
        function syncManualSsid() {{
            var select = document.getElementById('wifi-ssid');
            var details = document.getElementById('manual-ssid');
            var input = document.getElementById('wifi-ssid-manual');
            if (!select || !details) return;
            // VALUE, not the data-manual marker: this asks "will the server
            // treat this submit as manual entry?", and do_POST keys on the
            // submitted value. So an AP broadcasting the sentinel name shows
            // the field too — which is the documented degradation for that
            // case (one extra step: type the name), not a bug. Selecting an
            // option is the other question, and manualOption answers it.
            var manual = select.value === MANUAL_SSID_VALUE;
            details.hidden = !manual;
            if (input) {{
                // `required` only while the field is reachable. Server-side
                // this attribute would be a brick: a required control inside
                // a hidden <details> makes the form silently unsubmittable in
                // every browser, and the no-JS page NEEDS the plain field. On
                // the empty-scan page — manual pre-selected, nothing else to
                // pick — it turns a blank submit from a full-page error that
                // loses the typed password into the browser's own "fill this
                // in" bubble.
                input.required = manual;
            }}
            if (manual) {{
                details.open = true;
            }} else if (select.value && input) {{
                input.value = '';
            }}
        }}

        // Focus the input the moment the manual option is picked, so the
        // field the user now has to fill isn't one they have to find. Focus
        // lives HERE and not in syncManualSsid on purpose: that runs at load
        // too, and a load-time focus opens the keyboard over the form
        // (litclock-dev#671).
        function onSsidChange() {{
            syncManualSsid();
            var select = document.getElementById('wifi-ssid');
            var input = document.getElementById('wifi-ssid-manual');
            if (select && input && select.value === MANUAL_SSID_VALUE) input.focus();
        }}

        // NEVER set select.disabled here. A disabled control is excluded from
        // the form data set AND skipped by constraint validation, so a submit
        // landing mid-refresh would POST no wifi_ssid at all — silently
        // bypassing `required` and handing the server an empty pick. Swapping
        // the options is enough to show the scan is running (/review, litclock-dev#580).
        //
        // The fetch is aborted after SCAN_TIMEOUT_MS because the phone-to-clock
        // link is exactly the link that drops: with no timeout a hung scan
        // leaves the picker reading "Scanning..." forever, inside a captive
        // portal WebView that often has no visible reload control.
        //
        // 30s, not 20s (litclock-dev#908): the server's own worst case is
        // rescan (SCAN_RESCAN_TIMEOUT 10s) + a 2s settle + list
        // (SCAN_LIST_TIMEOUT 15s) = 27s, so a 20s abort reported "Scan failed"
        // on slow scans that were about to succeed — and the retry then queued
        // on the lock behind the scan still running. test_setup_rescan pins it.
        var SCAN_TIMEOUT_MS = 30000;

        // Keep the manual option selected across a Refresh if it was selected
        // before (litclock-dev#848): the rebuild resets the dropdown to its
        // placeholder, and without this a hidden-network owner who tapped
        // Refresh to double-check would watch their typed name vanish.
        function selectManualOption(select) {{
            var opt = manualOption(select);
            if (opt) opt.selected = true;
        }}

        // The tail every rebuild shares: the manual option goes back on the
        // end, the selection is restored if it should be, and the gate runs
        // over the result. Three call sites had this inline and drifted
        // (the empty branch forgot the re-append once).
        function finishRebuild(select, selectManual) {{
            appendManualOption(select);
            if (selectManual) selectManualOption(select);
            syncManualSsid();
        }}

        function isManualSelected(select) {{
            return select.value === MANUAL_SSID_VALUE;
        }}

        // Returns false so the Refresh button's no-JS fallback (a GET to
        // /setup?rescan=1) does not ALSO fire; with no picker on the page it
        // returns true and lets that server-side rescan happen instead.
        function refreshNetworks() {{
            var select = document.getElementById('wifi-ssid');
            if (!select) return true;
            // Sampled twice on purpose. A scan runs 2-20s, the dropdown stays
            // live throughout, and a user who taps Refresh from the
            // placeholder and then picks "type it myself" and starts typing
            // would — on the start sample alone — have the field vanish under
            // their fingers when the response landed, text kept but invisible
            // and the dropdown back on the placeholder. So the rebuild keeps
            // the manual selection if it held EITHER when the scan started or
            // when it finished.
            var wasManual = isManualSelected(select);
            resetSsidOptions(select, {scanning_js});
            finishRebuild(select, wasManual);

            var ctrl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
            var timer = setTimeout(function() {{
                if (ctrl) ctrl.abort();
            }}, SCAN_TIMEOUT_MS);

            fetch('/scan-wifi', ctrl ? {{signal: ctrl.signal}} : undefined)
                // A failed scan is a 503 (litclock-dev#908 gap 1): route it to
                // the "Scan failed" branch below, never into the networks
                // handler, where its JSON body would read as a result.
                .then(function(r) {{
                    if (!r.ok) throw new Error('scan failed: ' + r.status);
                    return r.json();
                }})
                .then(function(networks) {{
                    var keepManual = wasManual || isManualSelected(select);
                    if (networks.length === 0) {{
                        resetSsidOptions(select, {placeholder_empty_js});
                        // Match the server-rendered empty-scan path, which
                        // pre-selects the manual option and so force-opens
                        // this disclosure: a hidden-network user who taps
                        // Refresh into an empty scan needs the "type it in"
                        // field visible, not folded away (litclock-dev#615),
                        // and since litclock-dev#848 selecting the option IS what shows it.
                        finishRebuild(select, true);
                    }} else {{
                        resetSsidOptions(select, {placeholder_js});
                        networks.sort(function(a, b) {{
                            return (b.signal || 0) - (a.signal || 0);
                        }});
                        networks.forEach(function(net) {{
                            var opt = document.createElement('option');
                            opt.value = net.ssid;
                            var strengths = {strength_js};
                            var strength = net.signal >= 70 ? strengths[0]
                                : net.signal >= 40 ? strengths[1] : strengths[2];
                            var open = !net.security || net.security === '--';
                            opt.textContent = (open ? {option_open_js} : {option_js})
                                .split('{{strength}}').join(strength)
                                .split('{{ssid}}').join(net.ssid);
                            select.appendChild(opt);
                        }});
                        finishRebuild(select, keepManual);
                    }}
                }})
                .catch(function() {{
                    var keepManual = wasManual || isManualSelected(select);
                    resetSsidOptions(select, {scan_failed_js});
                    finishRebuild(select, keepManual);
                }})
                .then(function() {{
                    clearTimeout(timer);
                }});
            return false;
        }}

        // Load-time pass (litclock-dev#848): the server always renders the
        // disclosure visible so the no-JS page keeps its field; with JS on,
        // hide it unless the dropdown arrived with the manual option selected
        // (the retry echo and the empty scan). The script sits after the
        // form, so the elements exist.
        syncManualSsid();
        // ...and again on bfcache restore. iOS Safari restores form state
        // AFTER the load-time pass has run, so a back-navigation onto a page
        // whose dropdown it puts back on the manual option would show the
        // gate's verdict for the OTHER selection. pageshow is the one event
        // that fires on both a fresh load and a restore.
        window.addEventListener('pageshow', syncManualSsid);
    </script>
</body>
</html>
"""


HTML_SUCCESS = """<!DOCTYPE html>
<html lang="{lang}">
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    {meta_refresh}<meta charset="UTF-8">
    <title>{heading}</title>
    {head_meta}
    {style}
</head>
<body>
    <main>
        <h1>{heading}</h1>
        <p class="subtitle">{subtitle}</p>

        <div class="rows">
            <div class="row">
                <span class="row__label">{wifi_label}</span>
                <span class="row__value">{wifi}</span>
            </div>
        </div>

        <p class="next-step">
            {footer}
        </p>
    </main>
</body>
</html>
"""


HTML_ERROR = """<!DOCTYPE html>
<html lang="{ERROR_LANG}">
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta charset="UTF-8">
    <title>{ERROR_TITLE_TEXT}</title>
    {ERROR_HEAD}
</head>
<body>
    <main>
        <hr class="rule-error">
        <h1 class="h1--quiet">{ERROR_TITLE_TEXT}</h1>
        <p class="subtitle">{error}</p>
        <a href="/" id="retry-link" class="btn btn-primary">{ERROR_RETRY_TEXT}</a>
    </main>
    <script>
    (function() {
        var link = document.getElementById('retry-link');
        if (link) {
            link.addEventListener('click', function() {
                link.textContent = {loading_js};
                link.classList.add('is-loading');
                setTimeout(function() {
                    link.textContent = {retry_js};
                    link.classList.remove('is-loading');
                }, 10000);
            });
        }
    })();
    </script>
</body>
</html>
"""
# litclock-dev#532 final-slice /review: the error page builds PER REQUEST
# and fills every field via .replace — never .format, so a translation
# carrying stray braces can neither KeyError the render nor splice the
# runtime error into the wrong field. The CSS braces in the template are
# untouched by construction — which is why the template's CSS and JS are
# written with SINGLE braces: before litclock-dev#909 they were still doubled
# from the .format era and shipped verbatim, so the page's styles and its
# retry script were both dead. The two inline-JS literals go through
# json.dumps so apostrophes/non-ASCII stay valid JS.
_HTML_ERROR_TEMPLATE = HTML_ERROR
del HTML_ERROR


def _success_page(wifi_ssid, accept_language=None):
    """The confirmation page a provisioning POST returns while the hotspot is
    still up (see do_POST). Copy from the catalog (litclock-dev#532 T6), in
    the session's language (RT3); the network name is escaped into the
    subtitle by _rich."""
    with _page(accept_language):
        return _success_page_body(wifi_ssid)


def _success_page_body(wifi_ssid):
    return HTML_SUCCESS.format(
        lang=_page_language(),
        head_meta=_SETUP_HEAD_META,
        style=_setup_head_css(),
        wifi_label=_page_text("setup.success.wifi_label"),
        wifi=html.escape(wifi_ssid),
        heading=_page_text("setup.success.heading"),
        subtitle=_rich("setup.success.subtitle", ssid=wifi_ssid),
        meta_refresh='<meta http-equiv="refresh" content="15;url=/setup">\n    ',
        # The owner's own network, named where they must switch back to it;
        # _page_text escapes after filling, so the SSID cannot inject markup.
        footer=_page_text("setup.success.footer", ssid=wifi_ssid),
    )


def _error_page(error, accept_language=None):
    with _page(accept_language):
        return _error_page_body(error)


def _error_page_key(key, accept_language=None, /, **slots):
    """An error page whose MESSAGE is a catalog key, resolved in the same
    page scope as the rest of the page — a message translated before the
    scope opened came out in the device language on a session-language page
    (RT3 review)."""
    with _page(accept_language):
        return _error_page_body(_catalog_get(key, **slots))


def _error_page_body(error):
    retry_text = _catalog_get("setup.error.retry")
    return (
        _HTML_ERROR_TEMPLATE.replace("{ERROR_HEAD}", _SETUP_HEAD_META + "\n    " + _setup_head_css())
        .replace("{ERROR_LANG}", _page_language())
        .replace("{ERROR_TITLE_TEXT}", _page_text("setup.error.title"))
        .replace("{ERROR_RETRY_TEXT}", html.escape(retry_text, quote=True))
        .replace("{loading_js}", json.dumps(_catalog_get("setup.error.loading")))
        .replace("{retry_js}", json.dumps(retry_text))
        # quote=False keeps ASCII apostrophes literal inside the <p> —
        # byte-parity with the old raw insertion for our own copy.
        .replace("{error}", html.escape(str(error), quote=False))
    )


# ── Re-exports ─────────────────────────────────────────────────────────────
#
# litclock-dev#337 A4 — _IP_GEO_RETRY_DELAYS re-export.
# Re-exported from location_resolver so existing tests that read
# ``setup_server._IP_GEO_RETRY_DELAYS`` continue to see the canonical value.
# Functionally identical to ``location_resolver._IP_GEO_RETRY_DELAYS``. Bound
# via attribute access rather than ``from ... import`` so ruff's auto-fix
# doesn't strip it as an unused import — this constant is read externally
# (test_setup_server.py:495 sleep-schedule parity test) but not within
# this module, which would otherwise trip F401.
import location_resolver as _location_resolver_mod  # noqa: E402

_IP_GEO_RETRY_DELAYS = _location_resolver_mod._IP_GEO_RETRY_DELAYS

# litclock-dev#414 maintainability item #5 — set_system_timezone re-export.
# Canonical implementation moved to ``geocoding.set_system_timezone`` so the
# resolver + control_server routes can import it without dragging
# setup_server's captive-portal/http.server imports. This re-export keeps
# existing call sites + tests working without rewrites — anyone doing
# ``from setup_server import set_system_timezone`` or
# ``setup_server.set_system_timezone(...)`` still gets the same callable.
# Use ``geocoding.set_system_timezone`` for new code paths.
from geocoding import set_system_timezone  # noqa: E402,F401


def _update_env_location(lat, lon, *, location_name=None, units=None, timezone=None, **kwargs):
    """Backwards-compat shim for the legacy 5-kwarg writer (litclock-dev#337 A4).

    The canonical implementation now lives in ``location_resolver.update_env_location``;
    this shim exists because (a) existing tests monkeypatch this name to capture
    resolver-writer calls, and (b) first-boot has historically called this function
    by name. New callers should prefer ``location_resolver.update_env_location``
    directly so they can use the new ``mode`` / ``ip_country`` kwargs without
    going through the shim.

    Extra kwargs (``mode``, ``ip_country``) introduced by litclock-dev#337 A1/A6.1 are
    forwarded transparently via ``**kwargs`` so this shim doesn't need a
    signature change every time the canonical writer grows a kwarg.

    **env_file forwarding (litclock-dev#337 /review P0 fix):** the legacy first-boot caller
    has no ENV_FILE param and relies on the module-level constant — fine,
    because first-boot's ``setup_server.main()`` sets it. The new contexts
    (PWA sync-quick + on-boot oneshot) DO have ENV_FILE but reach this shim
    through ``location_resolver.resolve_location_from_ip``, which passes
    ``env_file=`` as a kwarg. Without honouring that kwarg here the shim
    would force the canonical writer to fall back to ``setup_server.ENV_FILE``
    (None in those contexts) and silently no-op — the actual bug Codex caught
    in /review. Pop the override before forwarding so the kwarg doesn't
    duplicate the explicit ``env_file=`` arg below.
    """
    from location_resolver import update_env_location as _impl

    env_file_override = kwargs.pop("env_file", None)
    return _impl(
        lat,
        lon,
        location_name=location_name,
        units=units,
        timezone=timezone,
        env_file=env_file_override if env_file_override else ENV_FILE,
        **kwargs,
    )


def _resolve_location_from_ip():
    """Backwards-compat shim for the first-boot post-WiFi caller (litclock-dev#337 A4).

    Delegates to ``location_resolver.resolve_location_from_ip`` with the full
    retry budget. The on-boot reresolve oneshot calls the canonical function
    directly; the PWA Save Specific→Auto path calls it with ``retries=False``
    (A7 sync-quick) — neither goes through this shim.

    The shim is preserved because (a) first-boot do_POST handlers call by
    name (lines ~1206 / ~1265) and (b) existing tests monkeypatch this name.
    """
    from location_resolver import resolve_location_from_ip as _impl

    _impl(retries=True, env_file=ENV_FILE)


def signal_completion() -> bool:
    """Signal to the installer that we're done. Best-effort; returns True on success.

    The signal file at /tmp/litclock-setup-done is the handoff primitive
    that first-boot.sh's wait loop polls. If touch() fails (tmpfs full,
    permission error, etc.), this returns False so callers can decide whether
    to proceed to SIGTERM — without the signal file, firstboot.sh's wait loop
    would never observe completion.

    Why bool-return instead of raising (litclock-dev#364 codex post-review fix; the
    retired normal-mode call site is gone since litclock-dev#715 — one
    call site remains, with a load-bearing ordering invariant a raise
    breaks):

      * WiFi-success branch — signal_completion runs AFTER teardown_hotspot.
        A raise from here landing in the existing except branch would call
        _restore_hotspot on the same wlan0 we just joined to the user's WiFi,
        destroying the working connection.

    Bool return + explicit check at each call site avoids both regressions.
    If SIGNAL_FILE is not configured (test mode), returns True so test
    callers proceed as if signal succeeded.
    """
    if not SIGNAL_FILE:
        return True
    try:
        Path(SIGNAL_FILE).touch()
        return True
    except Exception as e:
        print(f"Warning: Could not create signal file: {e}")
        return False


class SetupHandler(http.server.BaseHTTPRequestHandler):
    # Per-connection read timeout. StreamRequestHandler.setup() calls
    # self.request.settimeout(self.timeout), which makes rfile.read() raise
    # socket.timeout after HANDLER_TIMEOUT seconds instead of blocking forever.
    timeout = HANDLER_TIMEOUT

    def log_message(self, fmt, *args):
        if PROVISIONING_MODE:
            # flush=True: stdout is block-buffered when piped to journald, so
            # without an explicit flush, captive-portal probe access logs sit
            # in the buffer until the server process exits and may be lost on
            # SIGTERM. We need these logs visible in real time to debug iOS
            # CNA popup behavior. (issue litclock-dev#178)
            print(f"HTTP: {fmt % args}", flush=True)
        # Suppress in normal mode

    def send_html(self, content, status=200):
        self.send_response(status)
        self.send_header("Content-type", "text/html; charset=utf-8")
        # Never let phones or CNA WebViews cache anything we serve. Everything
        # coming out of this server is one-shot provisioning state or dynamic
        # settings — stale copies would re-trigger the exact iOS CNA bug this
        # PR is fixing (old 20 KB setup HTML persisting across boots).
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(content.encode("utf-8"))

    def send_font(self, name):
        """Serve one allowlisted woff2 (litclock-dev#909). Unlike the pages,
        a font is static and identical every boot, so it may be cached —
        the no-store rule above is about provisioning STATE, and a re-fetch
        per page would cost every render ~110 KB over the hotspot."""
        try:
            data = (SETUP_FONT_DIR / name).read_bytes()
        except OSError:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-type", "font/woff2")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _redirect_to_setup(self):
        """Send a 302 redirect to the setup page (used for captive portal probes)."""
        self.send_response(302)
        self.send_header("Location", f"http://{SETUP_HOSTNAME}/setup")
        self.end_headers()

    def _is_cna_detection_probe(self):
        """True when the request is Apple's WISPr detection client
        (UA "CaptiveNetworkSupport-<ver> wispr") — the probe that decides
        whether the CNA sheet rises. The sheet's WebView and real browsers
        send a Safari-family UA instead and must NOT match."""
        return "captivenetworksupport" in self.headers.get("User-Agent", "").lower()

    def _redirect_cna_probe(self):
        """Answer the Apple detection probe the way commercial hotspots do:
        a bodyless 302 off the Apple hostname to our own captive page.

        litclock-dev#526: iOS 26.5.x stopped promoting a 200-HTML answer
        served under an Apple probe hostname to the CNA sheet (the sheet
        would render spoofed content on an apple.com URL — a phishing
        surface for the iOS 26 Captive Assist credential sync). Portals
        that answer the probe with a redirect to their own host kept
        auto-popping, so the detection probe gets this 302; the sheet's
        WebView and manual browsers still get the 200 bridge. The Location
        uses the raw gateway IP so reaching it needs no DNS at all. See
        the CNA_PATH comment for why there is deliberately no WISPr XML
        body here."""
        self.send_response(302)
        self.send_header("Location", CNA_URL)
        self.send_header("Content-Length", "0")
        # Same no-cache posture as send_html — a cached captive decision
        # persisting across provisioning runs is the failure mode this
        # server spends the most effort avoiding.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()

    def _handle_captive_portal_probe(self, path, host):
        """Respond to captive portal probe requests to trigger the portal sheet.

        Different OSes detect captive portals differently:
        - iOS: Fetches /hotspot-detect.html with the CaptiveNetworkSupport
               (wispr) UA, expects exact "Success" text. Any non-"Success"
               answer marks the network captive. The detection probe gets a
               302 to /cna on the gateway IP (litclock-dev#526 — iOS 26.5.x
               stopped promoting 200-HTML served under an Apple hostname to
               the CNA sheet); the sheet's WebView / manual browsers get the
               ~1 KB JS-free bridge (see _cna_bridge_html) because large or
               JS-heavy responses bury the portal in Control Center (litclock-dev#175).
        - Android: Fetches /generate_204, expects HTTP 204.
                   A 302 redirect triggers the "Sign in to network" notification.
        - Windows: Fetches /connecttest.txt, expects "Microsoft Connect Test".
        - Firefox: Fetches /canonical.html, expects specific content.

        The iOS wispr probe gets a 302 to /cna; other iOS/browser hits on
        Apple paths get the bridge HTML; Android / Windows / Firefox get a
        302 redirect to the real setup form.
        """

        # litclock-dev#483 diagnostic: the base access log (log_message) records only the
        # request line + status — NOT the Host header or User-Agent. Those are
        # exactly what a "portal didn't auto-open" repro needs: which host iOS
        # probed (is it one we recognize?), which UA/iOS version, and what we
        # returned. Pair this with dnsmasq's log-queries (already enabled) to
        # see the full DNS→HTTP probe path a phone takes on join.
        # The RESPONSE side (branch + status + bytes) is logged too: the
        # litclock-dev#526 repro established that receipt-only logging cannot
        # distinguish "sheet never opened" from "sheet opened and died" —
        # and the 80-char UA truncation cut off exactly before the Safari/
        # token that separates a manual browser open from the CNA WebView.
        def _probe_log(branch, status, nbytes):
            if PROVISIONING_MODE:
                ua = self.headers.get("User-Agent", "")
                print(
                    f"CAPTIVE-PROBE: host={host!r} path={path!r} -> {branch} "
                    f"status={status} bytes={nbytes} ua={ua[:200]!r}",
                    flush=True,
                )

        # Apple probe hits get a UA split (litclock-dev#526): the WISPr
        # detection client gets a 302 to /cna on the gateway IP (iOS 26.5.x
        # no longer reliably promotes 200-HTML served under an Apple
        # hostname to the CNA sheet — see _redirect_cna_probe). Everything
        # else hitting these paths (the CNA sheet's WebView, a manual
        # browser) gets the tiny JS-free bridge: a non-"Success" 200, kept
        # small so the sheet renders it instead of burying the portal in
        # Control Center. The bridge links into the real setup form.
        # Shared by the Apple path branch and the Apple host fallback so
        # the two can't drift; `suffix` only differentiates the log label.
        def _serve_apple_probe(suffix=""):
            if self._is_cna_detection_probe():
                self._redirect_cna_probe()
                _probe_log("cna-302" + suffix, 302, 0)
            else:
                with _page(self.headers.get("Accept-Language")):
                    body = _build_cna_bridge_html()
                self.send_html(body)
                _probe_log("cna-bridge" + suffix, 200, len(body.encode()))

        if path in ("/hotspot-detect.html", "/library/test/success.html"):
            _serve_apple_probe()
            return True
        # Android probes — 302 redirect triggers "Sign in to network"
        if path in ("/generate_204", "/gen_204"):
            self._redirect_to_setup()
            _probe_log("redirect-setup", 302, 0)
            return True
        # Windows/Firefox probes — 302 redirect
        if path in ("/connecttest.txt", "/ncsi.txt", "/canonical.html", "/success.txt"):
            self._redirect_to_setup()
            _probe_log("redirect-setup", 302, 0)
            return True
        # Host-based fallback for probes that hit an unexpected path (e.g.
        # some iOS versions probe captive.apple.com/ with path "/"). Only
        # Apple hosts get the iOS-shaped bridge HTML — Google/Microsoft/
        # Firefox probe hosts expect a 302, and serving them HTML shaped for
        # iOS would be a regression for Android captive portal detection.
        host_clean = host.split(":")[0].lower()
        if host_clean in CAPTIVE_PORTAL_HOSTS:
            if host_clean in APPLE_CAPTIVE_PORTAL_HOSTS:
                _serve_apple_probe("-hostmatch")
            else:
                self._redirect_to_setup()
                _probe_log("redirect-setup-hostmatch", 302, 0)
            return True
        _probe_log("no-match-fallthrough", 0, 0)
        return False

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        host = self.headers.get("Host", "")

        # In provisioning mode, intercept captive portal probes. A request is
        # a probe if EITHER (a) the path isn't one of our real app paths, OR
        # (b) the Host header is a known captive-portal detection host — some
        # phones just hit the root path on captive.apple.com / etc., which
        # would otherwise fall through to _build_setup_html() and re-trigger
        # the exact iOS CNA bug we're trying to fix.
        if PROVISIONING_MODE:
            is_app_path = parsed.path in ("/", "/setup", "/scan-wifi", CNA_PATH) or _font_name(parsed.path)
            is_probe_host = host.split(":")[0].lower() in CAPTIVE_PORTAL_HOSTS
            if is_probe_host or not is_app_path:
                if self._handle_captive_portal_probe(parsed.path, host):
                    return

        if parsed.path == "/" or parsed.path == "/setup":
            switched = _apply_language_link(parsed.query, self.headers.get("Accept-Language"))
            self.send_html(
                _build_setup_html(
                    self.headers.get("Accept-Language"),
                    stale_scan_ok=switched,
                    rescan_after=_rescan_request(parsed.query),
                )
            )

        elif parsed.path == CNA_PATH and PROVISIONING_MODE:
            # Target of the detection-probe 302 (and the WISPr LoginURL):
            # the tiny JS-free bridge on our own host, so the CNA sheet
            # never has to render portal content under an Apple hostname.
            # Provisioning-only — outside provisioning there is no
            # hotspot and litclock.setup doesn't resolve, so the page
            # would be a dead end; it 404s like any other unknown path.
            with _page(self.headers.get("Accept-Language")):
                self.send_html(_build_cna_bridge_html())

        elif _font_name(parsed.path):
            self.send_font(_font_name(parsed.path))

        elif parsed.path == "/scan-wifi":
            global _WIFI_SCAN_NETWORKS, _WIFI_SCAN_TIME, _WIFI_SCAN_SSIDS, _WIFI_SCAN_LAST_EMPTY
            global _WIFI_SCAN_ATTEMPT_TIME, _WIFI_SCAN_LAST_FAILED
            import time

            # Stamped BEFORE the lock: a scan that finishes while this request
            # waits is newer than the Refresh tap, so it is the answer — a
            # second back-to-back scan would push the page past its own 30s
            # abort and read as "Scan failed" (litclock-dev#908 review).
            asked = time.monotonic()
            # Serialize scans — multiple phones hitting /scan-wifi concurrently
            # would otherwise each trigger their own nmcli rescan, fighting
            # over the radio and taking much longer than a single rescan.
            with _SCAN_CACHE_LOCK:
                if _WIFI_SCAN_ATTEMPT_TIME >= asked:
                    networks = [] if _WIFI_SCAN_LAST_EMPTY else list(_WIFI_SCAN_NETWORKS or [])
                    self.send_json(networks)
                    return
                try:
                    from wifi_provision import scan_wifi_networks

                    networks = scan_wifi_networks()
                except Exception as exc:
                    # 503, not an empty list (litclock-dev#908 gap 1): the
                    # page's Refresh shows "Scan failed - tap Refresh" for a
                    # non-OK response and "No networks found" only for a real
                    # []. Scan state is left untouched, as in
                    # _wifi_network_options.
                    print(f"WiFi scan failed: {exc}", flush=True)
                    _WIFI_SCAN_LAST_FAILED = True
                    self.send_json({"error": "scan_failed"}, status=503)
                    return
                # Filter before BOTH caching and send_json — the client rebuilds
                # the dropdown from this JSON, and the cache feeds the
                # server-rendered dropdown. Leaving either unfiltered re-exposes
                # the clock's own hotspot SSID.
                networks = _filter_own_hotspot(networks)
                _WIFI_SCAN_ATTEMPT_TIME = time.monotonic()
                _WIFI_SCAN_LAST_FAILED = False
                # An empty rescan updates NOTHING — same policy as
                # _wifi_network_options: no caching of empties (next call
                # retries), and _WIFI_SCAN_SSIDS keeps the evidence an
                # earlier scan gathered (litclock-dev#605 item 7 — see the
                # wifi_hidden decision in do_POST for why stale visibility
                # evidence beats none).
                _WIFI_SCAN_LAST_EMPTY = not networks
                if networks:
                    _WIFI_SCAN_NETWORKS = networks
                    # UNION, not replace — accumulate seen SSIDs so a flickered
                    # scan can't drop a genuinely-visible name and flip it to a
                    # permanent `hidden yes` leak (litclock-dev#615; see the
                    # matching write in _wifi_network_options).
                    _WIFI_SCAN_SSIDS = _WIFI_SCAN_SSIDS | _scanned_ssids(networks)
                    _WIFI_SCAN_TIME = time.monotonic()
            self.send_json(networks)

        elif PROVISIONING_MODE:
            # In provisioning mode, redirect any unknown path to setup.
            # Catches captive portal probes we don't explicitly handle.
            # Apple detection probes from hosts/paths outside the known
            # sets get the DNS-free /cna redirect — a litclock.setup
            # Location would hand the exact DNS-dependent shape iOS 26
            # distrusts back to the one client the litclock-dev#526 fix targets.
            if self._is_cna_detection_probe():
                self._redirect_cna_probe()
            else:
                self._redirect_to_setup()

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        global WIFI_CONNECT_ERROR, WIFI_CONNECT_IN_FLIGHT, WIFI_LAST_MANUAL_SSID

        if self.path != "/setup":
            self.send_response(404)
            self.end_headers()
            return

        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            self.send_response(400)
            self.end_headers()
            return
        if content_length < 0 or content_length > MAX_POST_BODY:
            self.send_response(413)
            self.end_headers()
            return
        post_data = self.rfile.read(content_length).decode()
        params = urllib.parse.parse_qs(post_data)

        # EPIC litclock-dev#383: the hotspot form collects WiFi credentials only.
        # Location, timezone, and units are auto-populated post-WiFi by
        # _resolve_location_from_ip() from ip-api.com; ALLOW_NSFW_QUOTES
        # stays at the env.sh.sample default until the user toggles it in
        # the PWA Settings tab. The handler itself writes nothing
        # synchronously — env.sh is written by the background thread
        # spawned below, after the resolver returns.
        wifi_ssid = params.get("wifi_ssid", [""])[0].strip()

        # Hidden networks never appear in a scan, so the picker offers a
        # sentinel option paired with a free-text field (litclock-dev#554).
        # Resolved here, ahead of every check below, so the typed SSID gets
        # the same own-network rejection, the same emptiness check and the
        # same character validation as one picked from the list.
        #
        # .strip() is deliberate even though leading/trailing spaces are legal
        # SSID bytes: a phone keyboard appends a space far more often than an
        # AP is named with one, and the accidental case fails in a way the
        # recipient cannot diagnose.
        wifi_ssid_manual = params.get(MANUAL_SSID_FIELD, [""])[0].strip()
        wifi_typed = False
        if wifi_ssid == MANUAL_SSID_VALUE or (not wifi_ssid and wifi_ssid_manual):
            # Arm 2 is NOT the no-JS user, despite what an earlier version of
            # this comment claimed: `required` is HTML constraint validation,
            # which browsers enforce with JavaScript off, so that user is
            # blocked before the POST. What arm 2 actually catches is a submit
            # landing while the dropdown is absent from the form data set —
            # and taking the typed name there is strictly better than joining
            # nothing. (The refresh path no longer disables the select, which
            # was how that state used to arise. See the JS comment.)
            wifi_ssid = wifi_ssid_manual
            wifi_typed = True

        accept_language = self.headers.get("Accept-Language")
        if PROVISIONING_MODE:
            # The session follows the submit BEFORE anything can answer with
            # a page, so every page this POST returns — a validation error
            # included — is in the language the reader chose (RT3 review).
            posted_language = _adopt_posted_language(params, accept_language)

        if PROVISIONING_MODE and not wifi_ssid:
            key = "setup.error.type_or_pick" if wifi_typed else "setup.error.pick_network"
            self.send_html(_error_page_key(key, accept_language))
            return

        # litclock-dev#532: persist the device language BEFORE the connect
        # thread (a failed-WiFi retry keeps the choice; the hotspot may tear
        # down moments later). The select only offers active codes, so an
        # invalid value here is a tampered POST — fall back to the same
        # default the GET rendered, never fail the join over a language.
        # Best-effort by contract: a language that doesn't persist costs a
        # Settings visit later; a join that fails costs the whole setup.
        if PROVISIONING_MODE:
            import config as _config  # noqa: PLC0415

            try:
                # The local, not the shared global: another phone's link
                # landing between adopt and here must not decide what THIS
                # submit saves (RT3 round-2 review).
                _config.atomic_update({"LITCLOCK_LANGUAGE": posted_language}, ENV_FILE)
            except (ValueError, OSError) as exc:
                print(f"Warning: could not persist LITCLOCK_LANGUAGE: {exc}")

        # Control characters are rejected before the value reaches the journal,
        # the e-ink splash or nmcli. An interior newline in a hand-typed SSID
        # forges lines in a journal that ships Storage=persistent and gets
        # collected into support bundles.
        if PROVISIONING_MODE and SSID_FORBIDDEN.intersection(wifi_ssid):
            self.send_html(_error_page_key("setup.error.bad_ssid_chars", accept_language))
            return

        # 802.11 caps the SSID at 32 bytes. Rejected here so an over-long
        # value gets a sentence the recipient can act on, instead of nmcli's.
        # "bytes", not "characters" — see SSID_MAX_BYTES.
        if PROVISIONING_MODE and len(wifi_ssid.encode("utf-8")) > SSID_MAX_BYTES:
            self.send_html(
                _error_page_key("setup.error.ssid_too_long", accept_language, n=SSID_MAX_BYTES)
            )
            return

        # Never let the clock try to join its own setup hotspot. The picker
        # filters HOTSPOT_SSID out (see _filter_own_hotspot), but a stale page
        # or scripted POST could still submit it. Connecting would tear down the
        # AP and fail (single-radio chip can't join the network it's hosting),
        # dumping the user into the connect-fail + hotspot-restart retry loop.
        # Reject up front with a clear message instead.
        #
        # casefold, not ==, because the free-text field is a HUMAN typing entry
        # point straight past the dropdown filter, and the page directly above
        # that field displays the hotspot name to them. A recipient who can't
        # find their network has been handed exactly one network name and a
        # text box; "litclock-setup" must not slip through (/review, litclock-dev#580).
        if PROVISIONING_MODE and HOTSPOT_SSID and wifi_ssid.casefold() == HOTSPOT_SSID.casefold():
            self.send_html(
                _error_page_key("setup.error.own_network", accept_language)
            )
            return

        # `hidden yes` is NOT a per-attempt flag: nmcli writes
        # 802-11-wireless.hidden=yes into the saved profile, so the clock then
        # announces that SSID in cleartext probe requests on every scan, for
        # the life of the install. Only ask for it when the radio genuinely
        # cannot see the network — a name typed by hand that IS in the current
        # scan is just a user who typed instead of scrolling, and does not
        # deserve a permanent probe-request leak of their home SSID
        # (/review, litclock-dev#580). When the evidence set doesn't hold the name we
        # err toward `hidden`: a needless probe leak is a smaller harm than
        # a hidden network that cannot be joined at all. The evidence set is
        # the UNION of every non-empty scan this provisioning session — each
        # non-empty scan accumulates into it and an empty rescan leaves it
        # untouched (litclock-dev#605 item 7, refined to union by litclock-dev#615): a
        # network seen even once stays "visible" evidence across later scan
        # flicker, because the `hidden yes` it suppresses would be written into
        # the saved profile permanently while any staleness is transient.
        #
        # This read of _WIFI_SCAN_SSIDS is deliberately OUTSIDE _SCAN_CACHE_LOCK
        # (litclock-dev#615): it is a single atomic reference-read of an
        # immutable frozenset that the scan writers only ever rebind wholesale
        # under the lock, so it always resolves to one complete evidence set.
        # Do NOT pair it with a second lock-guarded global at this site without
        # taking the lock — that would reintroduce the torn read litclock-dev#615 closed.
        wifi_hidden = wifi_typed and wifi_ssid.casefold() not in _WIFI_SCAN_SSIDS

        # Send success response BEFORE connecting to WiFi. On the Pi Zero 2W,
        # connecting to WiFi tears down the hotspot (single-radio chip can't
        # do AP+Station simultaneously), so the phone loses connection. We
        # must deliver the confirmation page while the hotspot is still up.
        # All the user-visible values are "Auto-detecting..." because IP-geo
        # runs after WiFi connects — PR2's handoff splash + PWA banner shows
        # the resolved values once they're known.
        # litclock-dev#715: this response has exactly one shape now — the
        # validations above return early on every empty/invalid SSID, so a
        # POST reaching here is a provisioning join. (The retired
        # normal-mode arm rendered "Setup Complete!" here.)
        self.send_html(_success_page(wifi_ssid, accept_language))

        # Connect to WiFi (provisioning only) + resolve location from IP +
        # signal completion + teardown. Runs in a background thread AFTER the
        # response has been sent so the phone sees the success page before
        # the hotspot tears down.
        if PROVISIONING_MODE and wifi_ssid:
            # Password is only used in this branch — sourced here so a normal-
            # mode POST never reads or holds the sensitive value in memory.
            wifi_password = params.get("wifi_password", [""])[0]

            # Atomically check-and-set the in-flight flag. With the threaded
            # server, two POSTs can arrive simultaneously and both pass the
            # check without the lock, spawning two conflicting connect attempts.
            with _WIFI_CONNECT_LOCK:
                if WIFI_CONNECT_IN_FLIGHT:
                    return
                WIFI_CONNECT_IN_FLIGHT = True
                WIFI_CONNECT_ERROR = None  # Clear stale error from previous attempt
                # Remember a typed name so the retry render can echo it back
                # (litclock-dev#605 review item 10). Stored HERE — after every rejection
                # (empty/control-chars/over-long, the own-hotspot refusal),
                # after the PROVISIONING_MODE gate (a normal-mode POST skips
                # the validations entirely, so nothing it carries may land in
                # this global), and after winning the in-flight CAS (a POST
                # that races a live attempt never joins, so its name must not
                # become the retry echo) — the global only ever holds the
                # <=32-byte validated SSID of a join actually launched.
                # Same lock as reset_state, the other writer.
                #
                # The else-arm CLEARS it, and that is load-bearing since
                # litclock-dev#848 (found by two independent adversarial
                # passes on PR litclock-dev#853). Before litclock-dev#848 a stale echo was inert:
                # the retry page put it in the text box but left the
                # dropdown on its disabled placeholder, so a password-only
                # resubmit sent no SSID and `required` stopped it. Now the
                # echo PRE-SELECTS the manual option, so a leftover name
                # from an earlier hand-typed attempt would arrive selected:
                # type "OldHidden", fail, pick "HomeNet" from the list, get
                # the password wrong, and the retry page offers to join
                # OldHidden — as `hidden yes`, with the same one tap it
                # takes to retry HomeNet. Identical with JavaScript off,
                # where nothing re-picks for the user.
                WIFI_LAST_MANUAL_SSID = wifi_ssid if wifi_typed else ""

            def _connect_and_teardown():
                global WIFI_CONNECT_ERROR, WIFI_CONNECT_IN_FLIGHT
                import time

                time.sleep(1)  # Let response flush to phone
                success_completed = False
                try:
                    from wifi_provision import connect_to_wifi, create_hotspot, teardown_hotspot

                    # P2.C: replace the stale hotspot QR with a progress splash
                    # while WiFi connects + IP-geo resolves (~30s).
                    _show_connecting_splash(wifi_ssid)
                    success, error = connect_to_wifi(wifi_ssid, wifi_password, hidden=wifi_hidden)
                    if not success:
                        # WiFi failed — on single-radio Pi, nmcli killed the hotspot
                        # when it tried to connect. Restore it so the phone can
                        # reconnect and the user can retry.
                        WIFI_CONNECT_ERROR = error
                        print(f"WiFi connection failed: {error}")
                        # litclock-dev#603 — thread the failure class through
                        # so the e-ink retry variant matches the actual cause
                        # (a timeout painted wrong-password guidance before).
                        _restore_hotspot(
                            create_hotspot,
                            failure_class=getattr(error, "failure_class", None),
                        )
                        return  # Keep server alive for retry

                    # WiFi connected — resolve location from IP, then clean up.
                    # Wrap the resolver in its own try/except so a future bug
                    # in env-write logic can't unwind to the outer except (which
                    # would call _restore_hotspot on the same wlan0 we just
                    # joined to the user's WiFi and destroy the connection).
                    teardown_hotspot()
                    try:
                        _resolve_location_from_ip()
                    except Exception as resolver_exc:
                        print(f"Resolver raised post-WiFi: {resolver_exc} — continuing without location")
                    if signal_completion():
                        success_completed = True
                    else:
                        # Signal file write failed. WiFi is up and the hotspot
                        # is down — DO NOT fall into the except branch, which
                        # would call _restore_hotspot() on the same wlan0 and
                        # destroy the working WiFi connection we just joined.
                        # Leave success_completed=False; the outer finally
                        # clears IN_FLIGHT and does NOT SIGTERM. firstboot.sh
                        # will time out; on next reboot it re-enters AP mode
                        # for re-provisioning. User can power-cycle to recover.
                        WIFI_CONNECT_ERROR = _ServerFailure("setup.cause.signal_failed")
                        print("Warning: signal_completion failed; setup server staying up, no SIGTERM scheduled")
                except Exception as e:
                    # str(e), never the exception: a held exception keeps its
                    # traceback, whose frames hold the submitted WiFi password
                    # for as long as the error is on show (litclock-dev#918 review).
                    WIFI_CONNECT_ERROR = _ServerFailure("setup.cause.setup_error", detail=str(e))
                    print(f"Post-setup error: {e}")
                    try:
                        from wifi_provision import create_hotspot as _create_hp

                        _restore_hotspot(_create_hp)
                    except Exception:
                        pass
                    return  # Keep server alive
                finally:
                    # Sequencing invariant — issue litclock-dev#364.
                    # Queue SIGTERM BEFORE clearing IN_FLIGHT so reset_state()'s drain
                    # barrier (which polls IN_FLIGHT and exits on False) cannot return
                    # claiming "thread quiescent" while a SIGTERM is still pending in
                    # the kernel queue. The kernel may deliver the signal microseconds
                    # after this line, but the drain barrier observed IN_FLIGHT=True
                    # at the moment SIGTERM was queued. See _schedule_self_terminate's
                    # docstring for the full story.
                    #
                    # Nested try/finally guarantees IN_FLIGHT clears even if the
                    # helper raises — without it, a failed helper would leave the
                    # flag stuck True forever and block subsequent setup attempts.
                    try:
                        if success_completed:
                            _schedule_self_terminate()
                    finally:
                        WIFI_CONNECT_IN_FLIGHT = False

            _spawn_bg(_connect_and_teardown, name="setup-wifi-connect")
        # litclock-dev#715: the normal-mode (pre-connected) completion arm
        # lived here — resolve-from-IP + signal + SIGTERM on an else branch.
        # The pre-connected path resolves inline in first-boot.sh since
        # litclock-dev#647, so the arm was reachable only from a page this
        # server no longer serves. In provisioning mode the early
        # empty-SSID validation guarantees wifi_ssid is set by this point.


class ThreadedHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Multi-threaded HTTP server.

    Each request runs on its own daemon thread so a stuck client (captive
    portal probe that stalls mid-request, phone that drops off the AP
    without closing its TCP connection, etc.) cannot block new requests.
    The per-handler socket timeout (SetupHandler.timeout) bounds how long
    any single handler can block.
    """

    allow_reuse_address = True
    daemon_threads = True
    # Larger accept queue so a burst of captive portal probes (iOS/Android
    # can send 3–5 within the first second after joining the AP) doesn't
    # fill the default backlog of 5 and cause kernel SYN drops.
    request_queue_size = 64

def _restore_hotspot(create_hotspot_fn, failure_class=None):
    """Restore the hotspot after a failed WiFi connection attempt.

    On single-radio Pi hardware, nmcli tears down the hotspot when attempting
    a station connection. If the connection fails, we must recreate the hotspot
    so the user can reconnect and retry — AND refresh the e-ink display with a
    retry variant, because phones auto-disconnect from the hotspot during the
    failed attempt and may not see the red banner on the setup form until
    they've rescanned the QR. The e-ink is the only surface the user is
    looking at during that gap.

    ``failure_class`` (litclock-dev#603) selects the variant: the
    wrong-password steps only when the failure actually WAS the password;
    everything else — timeout, not-found, unknown (None included: with no
    evidence, telling the user to retype their password sends them fixing
    the one thing known-least-likely to be broken) — gets the class-neutral
    connect-failed steps.
    """
    if not HOTSPOT_SSID or not HOTSPOT_PASSWORD:
        print("No hotspot credentials available — cannot restore hotspot")
        return
    import time

    print("Restoring hotspot for retry...")
    restored = False
    for attempt in range(3):
        result = create_hotspot_fn(ssid=HOTSPOT_SSID, password=HOTSPOT_PASSWORD)
        if result:
            print("Hotspot restored successfully")
            restored = True
            break
        print(f"Hotspot restore attempt {attempt + 1}/3 failed")
        time.sleep(2)

    if not restored:
        print("WARNING: Could not restore hotspot after 3 attempts")
        return

    # Refresh the e-ink display with the retry variant. Best-effort — if the
    # display module fails to import (dev machine) or the hardware refresh
    # errors, we still want the hotspot itself to stay up. Same-credential QR
    # code so users who auto-rejoined the hotspot keep working.
    #
    # Narrow except list: ImportError covers missing PIL / waveshare drivers
    # on a dev machine; OSError / RuntimeError cover SPI failures, kernel
    # driver issues, and the waveshare driver's own RuntimeError on re-init.
    # Everything else propagates — a TypeError from a bad signature change
    # should fail loud, not silently degrade the retry UX.
    try:
        from eink_display import HOTSPOT_RETRY_CONNECT_FAILED, HOTSPOT_RETRY_WIFI_PASSWORD, display_hotspot_info

        # Literal, not `from wifi_provision import WIFI_FAIL_BAD_PASSWORD`:
        # the retry-flow tests replace wifi_provision in sys.modules with a
        # bare fake, and a missing-name import here would be swallowed by
        # the ImportError arm below — silently degrading the variant
        # selection in exactly the tests meant to pin it. A lockstep test
        # asserts the literal matches wifi_provision.WIFI_FAIL_BAD_PASSWORD.
        retry_reason = HOTSPOT_RETRY_WIFI_PASSWORD if failure_class == "bad_password" else HOTSPOT_RETRY_CONNECT_FAILED
        display_hotspot_info(
            HOTSPOT_SSID,
            HOTSPOT_PASSWORD,
            HOTSPOT_GATEWAY_IP,
            retry_reason=retry_reason,
            lang=_default_language(None),  # the session's (RT3)
        )
        logging.info("E-ink refreshed with retry instructions")
    except (ImportError, OSError, RuntimeError):
        logging.warning("Could not refresh e-ink with retry state", exc_info=True)


def _show_connecting_splash(ssid):
    """Best-effort 'Connecting to {SSID}…' splash (EPIC litclock-dev#383 PR2, design P2.C).

    Bridges the ~30s gap between hotspot teardown and the control_server handoff
    splash so the e-ink isn't stuck on the stale hotspot QR while WiFi connects
    and IP-geo runs. Same narrow except list + rationale as the retry refresh
    above — a dev-machine import miss or an SPI hiccup must never abort the
    connect flow that's already underway."""
    try:
        from eink_display import _splash_catalog, display_status

        # litclock-dev#532 T6: one resolution, so title and message are one
        # language — the device's, which the WiFi submit just saved (R13).
        # The painter's own helper, so a catalog that will not even load
        # shows key names here too instead of escaping this narrow except
        # into the connect flow. Explicitly the session's language (RT3).
        catalog = _splash_catalog(_default_language(None))
        display_status(
            catalog.get("firstboot.splash.connecting.title", ssid=ssid),
            catalog.get("firstboot.splash.connecting.message"),
        )
        logging.info("E-ink refreshed with connecting splash")
    except (ImportError, OSError, RuntimeError):
        logging.warning("Could not refresh e-ink with connecting state", exc_info=True)


def main():
    global ENV_FILE, SIGNAL_FILE, PROVISIONING_MODE, HOTSPOT_SSID, HOTSPOT_PASSWORD

    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <env_file> <signal_file> [--provisioning]")
        sys.exit(1)

    ENV_FILE = sys.argv[1]
    SIGNAL_FILE = sys.argv[2]
    PROVISIONING_MODE = "--provisioning" in sys.argv

    # Parse optional hotspot credentials (used to restore hotspot after failed WiFi)
    for i, arg in enumerate(sys.argv):
        if arg == "--hotspot-ssid" and i + 1 < len(sys.argv):
            HOTSPOT_SSID = sys.argv[i + 1]
        elif arg == "--hotspot-password" and i + 1 < len(sys.argv):
            HOTSPOT_PASSWORD = sys.argv[i + 1]

    if not os.path.exists(ENV_FILE):
        print(f"Error: {ENV_FILE} not found")
        sys.exit(1)

    # Point the catalog loader at the SAME env.sh this server persists to.
    # Without this the render language comes from strings_catalog's own path
    # chain (LITCLOCK_ENV_FILE → LITCLOCK_DIR/env.sh → repo root), which
    # coincides with our argv only by convergent construction on the Pi — a
    # bench run with a different env-file argument would persist the picker's
    # choice to one file and render from another, mixed-language forever
    # (Stage-4 gates /review). setdefault: an explicit override still wins.
    os.environ.setdefault("LITCLOCK_ENV_FILE", ENV_FILE)

    if PROVISIONING_MODE:
        # Provisioning mode: HTTP only on all interfaces.
        # DHCP Option 114 (RFC 8908) was considered but requires a CA-signed TLS
        # cert, which is impossible on a local hotspot IP. iOS also ignores
        # Option 114 on first connection. Instead we rely on legacy captive portal
        # detection: DNS-intercept all domains → hotspot IP, then serve
        # OS-specific probe responses to trigger the captive portal sheet.
        with ThreadedHTTPServer(("", PORT), SetupHandler) as httpd:
            print(f"Setup server (provisioning) running on HTTP port {PORT}")
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                httpd.shutdown()
    else:
        # litclock-dev#715: normal mode (self-signed HTTPS on 8443, the
        # input-free pre-connected page) is retired — the pre-connected
        # first-boot path resolves location inline since litclock-dev#647
        # and never serves a page. The ONE remaining no-flag caller is
        # first-boot.sh's missing-env.sh fallback arm, whose contract is
        # exactly this: exit 1 loudly, wait_for_setup restart-loops the
        # exit, and the 1800s Setup-Incomplete ceiling paints recovery
        # instructions and powers off — a retry on the next boot.
        print(
            "Error: normal mode was retired (litclock-dev#715); "
            "this server only runs with --provisioning"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
