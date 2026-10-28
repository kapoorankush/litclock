import argparse
import functools
import json
import logging
import os
import re
import signal
import socket
import sys
import tempfile
import time as _time
import traceback
from datetime import datetime, timedelta
from glob import glob
from pathlib import Path
from random import randrange

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps, features

import config
import panel_frame
import quote_corpus
import strings_catalog
from control_url import control_base_url  # QR target — single source of truth (litclock-dev#343)
from log import setup_logging

# Global reference for signal handler cleanup
_epd = None


def signal_handler(signum, frame):
    """Handle termination signals to ensure display is put to sleep."""
    global _epd
    logging.warning(f"Received signal {signum}, cleaning up...")
    if _epd is not None:
        try:
            _epd.sleep()
            logging.info("Display put to sleep via signal handler.")
        except Exception as e:
            logging.error(f"Failed to sleep display in signal handler: {e}")
    # os._exit, not sys.exit — litclock-dev#531. sys.exit raises SystemExit, which is a
    # BaseException, so it is not caught by the paint's `except Exception`; it
    # unwinds out of __main__ entirely and runs full interpreter finalization,
    # tearing down lgpio's parked Thread-1. This path fires on every reboot,
    # every `systemctl stop`, and the unit's TimeoutStopSec kill — so leaving it
    # would have made the traceback rarer AND correlated with reboots, which
    # reads as "the fix mostly worked": the worst possible diagnostic signal.
    # litclock-dev#813 applied consistently (/review): `logging.shutdown()`
    # re-raises non-OSError/ValueError while `raiseExceptions` is True, so an
    # unguarded call here can cost the exit and drop the process into the very
    # finalization this line exists to skip. Same rule as the pre-paint handler:
    # everything that can fail is INSIDE the guard, os._exit is outside and last.
    try:
        logging.shutdown()
    except BaseException:
        pass
    os._exit(1)


# Configure logging
setup_logging()

# Constants
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT_PATH = os.path.join(PROJECT_ROOT, "fonts", "Literata72pt-Regular.ttf")
DISPLAY_SIZE = (800, 480)

# Status file for the Control PWA (litclock-dev#245 M2 / OV3). literary_clock.py writes
# this after each render so /api/status can mirror exactly what's on the
# e-ink. Lives under /run/litclock (tmpfs, pi:pi-owned via the litclock-dev#241 tmpfiles.d
# entry) — /var/run is root-owned and would Permission-denied the per-minute
# write under `User=pi`. Codex /review caught this on M2 (PR litclock-dev#245). tmpfs
# means zero SD wear from per-minute writes; override via env so dev boxes
# / CI can point it elsewhere.
STATUS_FILE = os.environ.get("LITCLOCK_STATUS_FILE", "/run/litclock/current-quote.json")

# litclock-dev#762 — the timer fires ~4s BEFORE the minute boundary and we
# render the UPCOMING minute, so the panel's unavoidable blackout falls at the
# boundary instead of after it.
#
# Measured on-device over 6942 runs (litclock-dev#590): refresh p50 10.45s, of which
# ~2.6s is prep and ~2.4s is trigger-to-legible on the glass. Firing at :00
# landed the new frame around :06.6 — so for ~4s of every minute the panel
# showed the PREVIOUS minute's timestring. A quote reading 11:59 still on screen
# at 12:00:03 reads as a broken clock.
#
# This offset is MANDATORY, not cosmetic: with the timer at :56 and no offset,
# datetime.now() still reports the old minute and the clock would render the
# previous minute's quote permanently. The timer and this constant are one
# change (tests/test_timer_lead.py pins them together).
#
# 4s suits the shipped PNG path. Runtime-render mode costs ~1.3s more prep;
# override per-device rather than editing this default.
def _render_lead_seconds() -> float:
    """The lead, parsed defensively and bounded.

    A bare `float(os.getenv(...))` at import is a brick: an empty or malformed
    value raises ValueError BEFORE the litclock-dev#531 `except BaseException`
    guard exists (that guard lives inside `if __name__ == "__main__"`, far
    below), so the painter dies at import every minute and the panel freezes on
    the last quote — the same PANEL signature this repo documents as confusable
    with the litclock-dev#531 lgpio wedge.

    The journal, however, does tell them apart, and an earlier version of this
    docstring wrongly claimed it did not (/review). `litclock.service` sets
    `StandardError=journal`, so an uncaught module-level ValueError prints a
    full traceback naming LITCLOCK_RENDER_LEAD_S and its line number — verified.
    The frozen panel is genuinely ambiguous; the journal is the one thing that
    resolves it at 1am, so do not tell a reader it is absent.

    Empty is not hypothetical: `export KEY=` is env.sh.sample's own idiom for
    an unset key, and update.sh merges sample keys into every device's env.sh.
    The OTA smoke gate does not backstop it either — Phase 4.5 runs the dry-run
    without sourcing env.sh, so a bad value passes smoke and Phase 7 then starts
    a service that fails forever.

    Bounded as well as parsed, and the FLOOR is not zero. The timer fires at
    :56, so the lead is what carries the target into the next minute: any value
    below ~4 lands in the minute that is ENDING and the clock renders the
    previous minute's quote permanently — the failure this file calls mandatory
    to avoid. Zero is the value an operator reaching for "turn this off" would
    set, and it produces a worse clock than before the change, silently
    (/review). A negative lead renders into the past; a lead past a minute skips
    one. Out-of-range and unparseable both fall back to the default, loudly.

    The floor cannot be exact here, because the correct value depends on the
    TIMER's second and this module does not read the unit file. `lead ~= 60 -
    second` is the real invariant, and tests/test_timer_lead.py pins the shipped
    pair. This bound only rules out the values that are wrong for ANY plausible
    timer.
    """
    raw = os.getenv("LITCLOCK_RENDER_LEAD_S")
    if raw is None or not raw.strip():
        return RENDER_LEAD_DEFAULT_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logging.warning("LITCLOCK_RENDER_LEAD_S=%r is not a number; using %s", raw, RENDER_LEAD_DEFAULT_S)
        return RENDER_LEAD_DEFAULT_S
    if not RENDER_LEAD_MIN_S <= value <= RENDER_LEAD_MAX_S:
        logging.warning(
            "LITCLOCK_RENDER_LEAD_S=%r is outside [%s, %s]; using %s. Below the floor the "
            "target lands in the minute that is ENDING and the clock paints the previous "
            "minute's quote every tick.",
            raw,
            RENDER_LEAD_MIN_S,
            RENDER_LEAD_MAX_S,
            RENDER_LEAD_DEFAULT_S,
        )
        return RENDER_LEAD_DEFAULT_S
    return value


RENDER_LEAD_DEFAULT_S = 4.0

# The FLOOR is 4.0, matching the SHIPPED timer, not a permissive band (/review).
#
# It was 3.0, which `_render_lead_seconds`' docstring calls broken: "any value
# below ~4 lands in the minute that is ENDING and the clock renders the
# previous minute's quote permanently". With `OnCalendar=*-*-* *:*:56`, a lead
# of 3.0-3.9 targets :59 of the minute that is ending — verified — so the
# validator ACCEPTED the exact failure it exists to reject, and
# tests/test_timer_lead.py asserted 3.0 was honoured, locking it in.
#
# The real invariant is `lead >= 60 - timer_second`, and this module cannot read
# the unit file. So the floor is pinned to the shipped pair instead: change
# litclock.timer's second and this constant must change with it —
# tests/test_timer_lead.py already pins the two together and will go red.
RENDER_LEAD_MIN_S = 4.0
RENDER_LEAD_MAX_S = 30.0
RENDER_LEAD_S = _render_lead_seconds()

# litclock-dev#838 — the hour of the nightly full `epd.Clear()`, parsed the
# same way as the lead above and for the same reason. It used to be a bare
# `int(os.getenv("DISPLAY_CLEAR_HOUR", 2))` INSIDE the paint try, after
# `epd.init()` and before `epd.display()`: an empty or malformed value raised
# there every minute, the paint's `except Exception` swallowed it, and the
# panel froze on the last quote with the process exiting 0 — the litclock-dev#531
# lookalike signature, and one the Phase 4.5 dry-run cannot see because it
# never sources env.sh. Empty is one uncomment away: since litclock-dev#783
# all four env.sh writers seed the key, commented, in the sample's
# `export KEY=` unset idiom.
DISPLAY_CLEAR_HOUR_DEFAULT = 2


def _warn_never_raises(fmt: str, *args) -> None:
    """`logging.warning`, guaranteed not to raise.

    PR litclock-dev#851 review (Codex 2, downgraded by the Claude pass): `logging.warning`
    does NOT raise on a broken pipe or a closed fd — `Handler.handleError`
    swallows OSError — only on a Python-level closed TextIOWrapper, which
    nothing in this tree does. The guard exists so `_display_clear_hour`'s
    "never raises" is literally true rather than true-by-current-handlers.
    Design note: `_render_lead_seconds` above has the same bare-warning shape
    at IMPORT and is deliberately left as is — a raising warning there is a
    raising import, which the litclock-dev#762 tests already cover for the value path,
    and refactoring it was out of scope for the review that added this.
    """
    try:
        logging.warning(fmt, *args)
    except BaseException:
        pass


def _display_clear_hour() -> int:
    """`DISPLAY_CLEAR_HOUR`, parsed defensively and bounded to a clock hour.

    Unset, empty or whitespace is the default, SILENTLY — that is the sample's
    own unset idiom, merged onto every device by update.sh, and a warning there
    would fire every minute on every clock. Anything else that is not an
    integer in 0..23 falls back to the default LOUDLY, naming the variable and
    the raw value, because the journal is the only thing that separates a
    rejected knob from a wedged panel. Never raises.
    """
    raw = os.getenv("DISPLAY_CLEAR_HOUR")
    if raw is None or not raw.strip():
        return DISPLAY_CLEAR_HOUR_DEFAULT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        _warn_never_raises("DISPLAY_CLEAR_HOUR=%r is not an integer; using %s", raw, DISPLAY_CLEAR_HOUR_DEFAULT)
        return DISPLAY_CLEAR_HOUR_DEFAULT
    if not 0 <= value <= 23:
        _warn_never_raises(
            "DISPLAY_CLEAR_HOUR=%r is not an hour in 0..23; using %s", raw, DISPLAY_CLEAR_HOUR_DEFAULT
        )
        return DISPLAY_CLEAR_HOUR_DEFAULT
    return value

# Persistent QR code on the e-ink top strip (litclock-dev#245 A6). 75x75 px at x=713,y=0,
# encodes the PWA URL so non-tech users can scan-to-open instead of typing.
# Geometry locked in M0 (validated via tools/control-pwa/validate_qr_layout.py);
# nudged (716,2)→(713,0) for the quiet zone (see QR_QUIET_ZONE).
#
# URL is plain HTTP — locked decision litclock-dev#257 dropped self-signed TLS for
# control_server (iOS PWAs reject self-signed every launch + suppress AtHS icon
# + suppress Dynamic Type). litclock-dev#343 moved control_server to port 80 so the URL a
# user scans or types carries NO port (bare `http://<ip>` / `http://litclock.local`)
# — the port is built by control_url.control_base_url, which omits `:80`. The
# clock and control_server share that helper so the QR target and the actual
# listen port can never drift.
#
# QR_URL is the FALLBACK — used when _resolve_lan_ip() can't determine the
# Pi's LAN IP (no network, no default route). The runtime path prefers the IP
# because mDNS (litclock.local) is unreliable on Android Chrome and many
# home/guest WiFi networks (issue litclock-dev#306). Resolved IP changes propagate within
# ~60s — the e-ink re-renders every minute tick.
QR_URL = control_base_url("litclock.local")
QR_POSITION = (713, 0)
QR_VERSION = 2
QR_BOX_SIZE = 3
QR_BORDER = 0
QR_MODULES = 25  # version 2
QR_SIZE = QR_MODULES * QR_BOX_SIZE  # 75px
# Top-strip divider geometry, shared by compose() and the quiet-zone notch
# below so they can never drift apart. PIL paints a width=4 horizontal line
# at y=78 across rows 77..80 (centerline convention).
DIVIDER_Y = 78
DIVIDER_WIDTH = 4
# ISO/IEC 18004 quiet zone: 4 modules of white on every side = 12px at
# box_size 3. The 78px strip can't hold (25 + 2*4) * 3 = 99px, so the border
# isn't baked into the QR image (QR_BORDER stays 0) — _composite_settings_qr
# carves it out of the surroundings instead: it white-outs the strip's
# top-right corner (which notches the divider under the QR) before pasting.
# That yields 4 modules right (x=788..799), 4+ modules left (date ink ends
# ≤ x=MASTHEAD_DATE_RIGHT, 693, held there by CI) and 4 modules below — the notch extends past the divider to
# QR bottom + 12px (row 86), so the bottom quiet zone is STRUCTURAL, not
# corpus-dependent. Today that carve is a no-op below row 80: the tallest
# corpus glyphs (brackets above cap height) start at display y=87, measured
# across all 4,809 corpus PNGs. If a future regen inks rows 81..86 under
# the QR, the notch clips those pixels rather than breaking the QR scan
# (and tests/test_literary_clock.py's corpus-clearance test flags it).
# Top is 0px in the framebuffer — physically backed by the panel's ~2-3mm
# inactive white margin inside the bezel, which scanners see as quiet zone.
QR_QUIET_ZONE = 4 * QR_BOX_SIZE
QR_NOTCH_BOTTOM = max(DIVIDER_Y + DIVIDER_WIDTH // 2, QR_POSITION[1] + QR_SIZE + QR_QUIET_ZONE - 1)

# The 800x400 quote area is pasted at this panel row (below the top strip).
QUOTE_AREA_Y = 80
QUOTE_AREA_H = 400

# ---- Masthead geometry (litclock-dev#538 V8-G2, owner-approved on hardware) ----
# The top strip reads as one typeset dateline: the date and the LOW temp
# line stand on a shared baseline placed so worst-case descenders clear
# the horizontal rule by RULE_CLEARANCE — mirroring how the quote's
# ascenders sit just below the rule. The weather cell is a centered
# lockup (icon + fixed-width two-line temp slot), so its position never
# shifts with the day's digits. All vertical placement derives from font
# metrics at the PINNED Pillow/FreeType versions (dev + Pi ship the same
# lock); geometry margins are small, so an unpinned Pillow bump must
# re-run the masthead tests. NOTE: textbbox() bottoms are EXCLUSIVE
# (first row below the ink) — every derivation here relies on that.
RULE_CLEARANCE = 4
# Top ink row of the width-4 divider PIL paints at DIVIDER_Y
# (centerline convention: rows DIVIDER_Y-1 .. DIVIDER_Y+2).
RULE_TOP = DIVIDER_Y - DIVIDER_WIDTH // 2 + 1
WEATHER_DIVIDER_X = 225  # ink columns 224..227
# Last white column left of the vertical rule's ink — the weather cell the
# lockup centers in is columns 0..WEATHER_CELL_RIGHT. RULE_CLEARANCE is a
# bound the tests assert near the rule, NOT space removed from centering
# (subtracting it shifted the approved layout 1px left — litclock-dev#538 pixel-diff).
WEATHER_CELL_RIGHT = WEATHER_DIVIDER_X - DIVIDER_WIDTH // 2 - 1
DATE_X = 250
DATE_FONT_SIZE = 48
TEMP_FONT_SIZE = 24
TEMP_LINE_PITCH = 28  # baseline-to-baseline for the two temp lines (8px min ink gap — reviewed)
ICON_SIZE = 64  # icons are native 32x32 -> exact 2x integer scale (NEAREST)
LOCKUP_GAP = 12
LOCKUP_MIN_X = 18  # stay clear of the update-failed glyph zone (ink x<=16)
# The temp slot is sized over EVERY string _format_temp can emit
# (the full accepted band x both units), so any accepted value fits by
# construction. Hand-picked reference strings lost this game twice —
# "-40°C" tied the slot, then "100°C" beat it ("0" is wider than "9" in
# Literata and °C wider than °F). ~600 textbbox probes, once per process.
TEMP_BAND = (-99, 199)  # also the _format_temp acceptance band
# litclock-dev#532 D3: the date's INK may end 8px short of the quiet-zone
# carve (x=701, which _composite_settings_qr whites out AFTER the masthead is
# drawn — the QR's own edge is 12px further right, and measuring to it let a
# date that passed lose its last letter: litclock-dev#919 review). Measured with
# textbbox, the WIDER of ink and advance, for the same reason: a final 'Q' or
# 'c' inks past its advance. Every language's format is held to it at
# DATE_FONT_SIZE by CI (tests/test_masthead_date.py, every weekday x month x
# day); English's widest over that product, "Mon, September 0x", measures
# 438px of the 443.
MASTHEAD_DATE_RIGHT = QR_POSITION[0] - QR_QUIET_ZONE - 8
MASTHEAD_DATE_BUDGET = MASTHEAD_DATE_RIGHT - DATE_X
# The runtime backstop for a bundle CI never saw: shrink to this, then cut.
MASTHEAD_DATE_FLOOR = 32
# Nothing near this long fits the strip at any size; capping where the date is
# made bounds every measure after it (and the log lines) for a pathological
# bundle — Pillow is O(n) per measure and refuses a string past ~1M chars.
MASTHEAD_DATE_MAX_CHARS = 120
# Fortune mode's masthead (litclock-dev#533, owner decision 2026-08-12): the
# time leads at the size the date has always used, the date steps down beside
# it, and the pair is centred between the weather rule and the QR quiet zone.
# Measured on the shipped font: English's widest lockup ("00:00" + "Mon,
# September 0x") ends at x=668, against the same MASTHEAD_DATE_RIGHT the
# quote masthead is held to.
LOCKUP_TIME_FONT_SIZE = DATE_FONT_SIZE
LOCKUP_DATE_FONT_SIZE = 28
LOCKUP_DATE_FLOOR = 20
LOCKUP_TIME_GAP = 26
LOCKUP_BAND = (WEATHER_DIVIDER_X + DIVIDER_WIDTH // 2 + 1, QR_POSITION[0] - QR_QUIET_ZONE)  # x=228..701
# datetime.weekday() order (Monday first) and month order: the catalog names.
_WEEKDAY_KEYS = (
    "clock.weekday.mon",
    "clock.weekday.tue",
    "clock.weekday.wed",
    "clock.weekday.thu",
    "clock.weekday.fri",
    "clock.weekday.sat",
    "clock.weekday.sun",
)
_MONTH_KEYS = (
    "clock.month.jan",
    "clock.month.feb",
    "clock.month.mar",
    "clock.month.apr",
    "clock.month.may",
    "clock.month.jun",
    "clock.month.jul",
    "clock.month.aug",
    "clock.month.sep",
    "clock.month.oct",
    "clock.month.nov",
    "clock.month.dec",
)
# The day is a name too: "05" in English, "5" in Spanish, "1er" for a French
# first. A list rather than a padding switch keeps every slot fixed.
_DAY_KEYS = tuple(f"clock.day.{n}" for n in range(1, 32))

_masthead_cache: dict = {}


def _reset_masthead_cache() -> None:
    """Reset hook — the cache assumes FONT_PATH and the size constants are
    immutable for the process lifetime (true in production: fresh process
    per minute). Tests that monkeypatch them must clear it.

    No callers today (litclock-dev#605 item 14): the masthead tests all run
    against the real fonts and constants, so the cache never goes stale.
    Kept as the documented reset point for the first test that does
    monkeypatch FONT_PATH or the size constants."""
    _masthead_cache.clear()


def _masthead_metrics() -> dict:
    """Derived masthead placement (cached: fonts + metrics are static)."""
    if _masthead_cache:
        return _masthead_cache
    date_font = ImageFont.truetype(FONT_PATH, DATE_FONT_SIZE)
    temp_font = ImageFont.truetype(FONT_PATH, TEMP_FONT_SIZE)
    probe = ImageDraw.Draw(Image.new("1", (4, 4)))
    asc_date = date_font.getmetrics()[0]
    # descender depth below the baseline for the deepest date glyphs
    desc_depth = probe.textbbox((0, 0), "Jy", font=date_font)[3] - asc_date
    baseline = RULE_TOP - RULE_CLEARANCE - desc_depth
    slot = max(
        probe.textbbox((0, 0), f"{n}{deg}", font=temp_font)[2]
        for n in range(TEMP_BAND[0], TEMP_BAND[1] + 1)
        for deg in ("°F", "°C")
    )
    group = ICON_SIZE + LOCKUP_GAP + slot
    icon_x = max((WEATHER_CELL_RIGHT - group) // 2, LOCKUP_MIN_X)  # centers in 0..WEATHER_CELL_RIGHT
    _masthead_cache.update(
        date_font=date_font,
        temp_font=temp_font,
        temp_asc=temp_font.getmetrics()[0],
        baseline=baseline,
        date_y=baseline - asc_date,
        icon_x=icon_x,
        icon_y=RULE_TOP - RULE_CLEARANCE - ICON_SIZE,
        slot_right=icon_x + group,
    )
    return _masthead_cache


def _format_temp(value, degrees: str) -> str | None:
    """Bound the weather temp domain (litclock-dev#538 review): finite numeric within
    a physical sanity band -> 'N°F'/'N°C'; anything else returns None and the
    caller suppresses weather for this render. Prevents API garbage from
    crashing the per-minute render (round(None)) or drawing junk into the
    fixed-width lockup slot."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if not (TEMP_BAND[0] <= n <= TEMP_BAND[1]):  # NaN-safe; band == slot sizing domain
        return None
    return f"{round(n):d}{degrees}"


def masthead_date(now: datetime, catalog=None) -> str:
    """The masthead's date line in the device language (litclock-dev#532 D3).

    The format is the catalog's ``clock.masthead.date`` with ``{weekday}``,
    ``{month}`` and ``{day}`` filled from the catalog's name lists (the day
    is a list too: "05" in English, "5" or "1er" elsewhere). English is
    ``{weekday}, {month} {day}`` over "Wed" / "September" / "05" — exactly
    the ``%a, %B %d`` it used to strftime, which also tied the date to the
    process locale. ``catalog`` is a strings_catalog snapshot; None takes
    the device's.

    A bundle that is not usable, or a catalog that fails outright, gets the
    language-neutral ISO date: a quote frame is never another language (R7),
    so the fleet default's names are not an option here, and the date is a
    REQUIRED part of the frame (D9), so it is not omitted either.
    """
    try:
        snap = catalog if catalog is not None else strings_catalog.snapshot()
        if snap.usable:
            # get_present, not get: a missing or blank name must cost the
            # whole localized date, never paint a raw key (litclock-dev#919 review — a
            # truncated canonical bundle still counts as usable).
            names = {
                "weekday": snap.get_present(_WEEKDAY_KEYS[now.weekday()]),
                "month": snap.get_present(_MONTH_KEYS[now.month - 1]),
                "day": snap.get_present(_DAY_KEYS[now.day - 1]),
            }
            text = snap.get_present("clock.masthead.date", **names) if all(names.values()) else None
            # One line: Pillow cannot measure a line break (see _fallback_line).
            text = _LINE_BREAKS_RE.sub(" ", text).strip()[:MASTHEAD_DATE_MAX_CHARS] if text else ""
            if text:
                return text
    except Exception as e:  # never raises by contract; the frame must not depend on that
        logging.warning(f"masthead date: catalog lookup failed ({e}); using the ISO date")
    return now.strftime("%Y-%m-%d")


def _date_ink_width(draw, text: str, font) -> int:
    """How far right of its origin the text reaches — the wider of its ink
    and its advance (textbbox), what the budget limits."""
    return draw.textbbox((0, 0), text, font=font)[2] if text else 0


def _fit_masthead_date(draw, text: str):
    """(text, font) for a date wider than MASTHEAD_DATE_BUDGET: the largest
    size down to MASTHEAD_DATE_FLOOR that fits, else the floor size with the
    text cut and an ellipsis. Only a bundle that skipped CI reaches this."""
    for size in range(DATE_FONT_SIZE - 2, MASTHEAD_DATE_FLOOR - 1, -2):
        font = ImageFont.truetype(FONT_PATH, size)
        if _date_ink_width(draw, text, font) <= MASTHEAD_DATE_BUDGET:
            logging.warning(f"masthead date {text!r} too wide at {DATE_FONT_SIZE}pt; drawn at {size}pt")
            return text, font
    font = ImageFont.truetype(FONT_PATH, MASTHEAD_DATE_FLOOR)
    cut = text[:MASTHEAD_DATE_MAX_CHARS]
    while cut and _date_ink_width(draw, cut + "…", font) > MASTHEAD_DATE_BUDGET:
        cut = cut[:-1]
    logging.warning(f"masthead date {text!r} too wide even at {MASTHEAD_DATE_FLOOR}pt; cut to fit")
    return cut.rstrip() + "…", font


def _clock_text(now: datetime) -> str:
    """The time as the panel shows it: 12-hour, no leading zero, no AM/PM
    ("9:44"; both 12:05 and 00:05 read "12:05"). The owner's call on
    litclock-dev#961: 12-hour only, no setting. Display only — the status
    file and every comparison keep the 24-hour %H:%M."""
    return f"{now.hour % 12 or 12}:{now.minute:02d}"


def _time_slot_metrics() -> tuple[list[str] | None, int]:
    """(features, slot width) for the lockup's time — fortune mode only, so a
    quote-mode minute never pays for it.

    Literata's figures are proportional ("1" is 9px narrower than "0" at
    48pt), so a centred lockup would slide every minute. Tabular figures
    where raqm can shape them — then every time is one width, which
    tests/test_fortune.py checks over all 1,440 — and otherwise a slot sized
    over all 1,440 times, the TEMP_BAND discipline."""
    if "time_slot" not in _masthead_cache:
        font = _masthead_metrics()["date_font"]
        probe = ImageDraw.Draw(Image.new("1", (4, 4)))
        # Tabular only if the shaper actually applied it: every digit one
        # advance. Ten measures prove that; then "00:00" is every time's width.
        # A font or build that ignores tnum takes the full sweep instead of
        # trusting a slot measured on one string (review).
        tabular = features.check("raqm") and (
            len({probe.textlength(d, font=font, features=["tnum"]) for d in "0123456789"}) == 1
        )
        if tabular:
            feats = ["tnum"]
            slot = probe.textbbox((0, 0), "00:00", font=font, features=feats)[2]
        else:
            feats = None
            slot = max(
                probe.textbbox((0, 0), _clock_text(datetime(2000, 1, 1, h, m)), font=font)[2]
                for h in range(24)
                for m in range(60)
            )
        _masthead_cache.update(time_features=feats, time_slot=slot)
    return _masthead_cache["time_features"], _masthead_cache["time_slot"]


@functools.lru_cache(maxsize=8)
def _lockup_date_font(size: int):
    return ImageFont.truetype(FONT_PATH, size)


def _fit_lockup_date(draw, date_text: str, budget: int):
    """(text, font) for the lockup's date in ``budget`` px: the design size,
    else smaller down to LOCKUP_DATE_FLOOR, else the floor with the text cut.
    A translated date CI never saw lands here; English never does."""
    text = date_text
    for size in range(LOCKUP_DATE_FONT_SIZE, LOCKUP_DATE_FLOOR - 1, -2):
        font = _lockup_date_font(size)
        if _date_ink_width(draw, text, font) <= budget:
            if size != LOCKUP_DATE_FONT_SIZE:
                logging.warning(f"masthead lockup date {text!r} drawn at {size}pt")
            return text, font
    while text and _date_ink_width(draw, text + "…", font) > budget:
        text = text[:-1]
    logging.warning(f"masthead lockup date {date_text!r} too wide even at {LOCKUP_DATE_FLOOR}pt; cut to fit")
    return (text.rstrip() + "…") if text else "", font


def _draw_time_lockup(draw, mh: dict, time_text: str, date_text: str) -> tuple[int, int]:
    """Fortune mode's dateline: the time in its fixed slot, the date beside
    it, both on the masthead baseline, the pair centred in LOCKUP_BAND and
    never past MASTHEAD_DATE_RIGHT. Returns the lockup's (left, right) ink
    span for the invariant tests."""
    band_left, band_right = LOCKUP_BAND
    time_features, slot = _time_slot_metrics()
    budget = MASTHEAD_DATE_RIGHT - band_left - slot - LOCKUP_TIME_GAP
    date_text, date_font = _fit_lockup_date(draw, date_text, budget)
    width = slot + LOCKUP_TIME_GAP + _date_ink_width(draw, date_text, date_font)
    left = band_left + max(0, (band_right - band_left - width) // 2)
    left = min(left, MASTHEAD_DATE_RIGHT - width)
    tw = draw.textbbox((0, 0), time_text, font=mh["date_font"], features=time_features)[2]
    draw.text(
        (left + (slot - tw) // 2, mh["baseline"]),
        time_text,
        font=mh["date_font"],
        anchor="ls",
        features=time_features,
        fill=0,
    )
    draw.text((left + slot + LOCKUP_TIME_GAP, mh["baseline"]), date_text, font=date_font, anchor="ls", fill=0)
    return left, left + width


def _compose_masthead(
    image,
    draw,
    date_text: str,
    temp_high: str | None,
    temp_low: str | None,
    icon_path: str | None,
    time_text: str | None = None,
):
    """Draw the top strip: date, weather lockup (when temps present), rules.
    With ``time_text`` (fortune mode, litclock-dev#533) the dateline becomes
    the time-and-date lockup instead; nothing else in the strip moves.
    Call AFTER the quote paste — the rule's bottom ink row (DIVIDER_Y+2 ==
    QUOTE_AREA_Y) must overwrite the quote image's top margin row, as it
    always has. A missing/corrupt icon leaves the icon area blank but never
    moves the temps (fixed slot geometry) and never raises."""
    mh = _masthead_metrics()
    have_weather = temp_high is not None and temp_low is not None
    if have_weather:
        if icon_path:
            try:
                icon_image = ImageOps.invert(
                    Image.open(icon_path).resize((ICON_SIZE, ICON_SIZE), Image.NEAREST).convert("L")
                )
                image.paste(icon_image, (mh["icon_x"], mh["icon_y"]))
            except OSError as e:
                # FileNotFoundError AND corrupt-file (UnidentifiedImageError
                # is an OSError subclass) — litclock-dev#538 review: the old narrow
                # except let a corrupt icon kill the whole render.
                logging.error(f"Weather icon unusable ({icon_path}): {e}; leaving icon area blank")
        for line_text, line_baseline in (
            (temp_high, mh["baseline"] - TEMP_LINE_PITCH),
            (temp_low, mh["baseline"]),
        ):
            w = draw.textbbox((0, 0), line_text, font=mh["temp_font"])[2]
            draw.text((mh["slot_right"] - w, line_baseline - mh["temp_asc"]), line_text, font=mh["temp_font"], fill=0)
    if time_text is not None:
        _draw_time_lockup(draw, mh, time_text, date_text)
    elif _date_ink_width(draw, date_text, mh["date_font"]) <= MASTHEAD_DATE_BUDGET:
        draw.text((DATE_X, mh["date_y"]), date_text, font=mh["date_font"], fill=0)
    else:
        # A smaller size sits on the same baseline, so the date still clears
        # the rule by the measured margin.
        fitted, font = _fit_masthead_date(draw, date_text)
        draw.text((DATE_X, mh["baseline"]), fitted, font=font, anchor="ls", fill=0)
    draw.line([(0, DIVIDER_Y), (DISPLAY_SIZE[0], DIVIDER_Y)], fill=0, width=DIVIDER_WIDTH)
    if have_weather:
        draw.line([(WEATHER_DIVIDER_X, 0), (WEATHER_DIVIDER_X, DIVIDER_Y)], fill=0, width=DIVIDER_WIDTH)


# Runtime quote rendering (litclock-dev#531 Stage 2). The clock renders the quote from
# the corpus CSV via src/quote_renderer.py. For the PNG set's language
# (English) it does so only when LITCLOCK_RUNTIME_RENDER is true and the
# validation marker holds, with the fallback order on ANY failure: the
# pre-rendered PNG for this minute (right time, right content) -> the
# plain-time frame (right time, no quote). Every other language always
# renders text and goes straight to the plain-time frame, never to English
# PNGs (litclock-dev#532 R7 — see main()). There is
# deliberately NO "last-good frame" tier: a stale frame shows the WRONG
# minute's timestring, and on a clock a wrong time is worse than a plain
# one. The rendered frame is persisted to tmpfs (best-effort, atomic) so
# support bundles and the status file can reference what was painted.
RUNTIME_RENDER_DIR = os.environ.get("LITCLOCK_RUNTIME_RENDER_DIR", "/run/litclock")
# NOTE: no CORPUS_CSV constant here — corpus path resolution lives in
# quote_corpus (env-aware, litclock-dev#594 review). A local constant was a dead knob
# that tests patched to no effect (litclock-dev#601).
# The flag alone is not enough: this device's freetype-py wheel must first
# be PROVEN to reproduce GD metrics (tools/validate_measurement.py check
# --stamp writes this marker only on a 100% pass). Guards against enabling
# runtime rendering in an unvalidated environment, where subtly-wrong
# hinted metrics would render ugly-but-sane frames no per-minute sanity
# check can catch (litclock-dev#537 review). Delete the marker after any freetype-py
# or font change and re-run the check.
RUNTIME_VALIDATED_MARKER = os.environ.get(
    "LITCLOCK_RUNTIME_VALIDATED_MARKER", os.path.join(PROJECT_ROOT, ".runtime-render-validated")
)

# display_driver binds to hardware GPIO/SPI on import. Keep it lazy so
# --dry-run (smoke test) can render an image without touching /dev/spidev*.
from weather_providers import open_meteo, openweathermap  # noqa: E402

# litclock-dev#532 D8 — why a frame carries no quote. The two are told apart
# because they mean different things to the owner: a GAP is the corpus having
# nothing for this minute (normal; a partly covered language has many), a
# FAILURE is the clock being unable to show a quote it should have (a render
# error, a corpus that will not load, a PNG that will not open). Only failures
# count toward the PWA warning (R14).
FALLBACK_GAP = "gap"
FALLBACK_FAILURE = "failure"
# Catalog keys for the plain-time frame's one line, per reason (D8).
FALLBACK_LINE_KEYS = {
    FALLBACK_GAP: "clock.fallback.gap",
    FALLBACK_FAILURE: "clock.fallback.failure",
}
# The PWA warns after this many consecutive FAILURE frames (R14).
CONSECUTIVE_FAILURES_WARN_AT = 3

# The plain-time frame (litclock-dev#532 D2, as in the approved mockup): the
# time centred in Literata Regular 160pt, then one Literata SemiBold-italic
# 30pt line in the device language. Baselines, not ink boxes, so the layout
# does not jump between minutes whose digits differ; they reproduce the
# mockup's ink rows (time 189..310, line 370..400) exactly. 24-hour for every
# language (D18).
FALLBACK_TIME_FONT_SIZE = 160
FALLBACK_TIME_BASELINE_Y = 308
FALLBACK_LINE_FONT_PATH = os.path.join(PROJECT_ROOT, "fonts", "Literata72pt-SemiBoldItalic.ttf")
FALLBACK_LINE_FONT_SIZE = 30
FALLBACK_LINE_MIN_FONT_SIZE = 22
FALLBACK_LINE_BASELINE_Y = 393
FALLBACK_LINE_MARGIN_X = 40


_LINE_BREAKS_RE = re.compile(r"[ \t]*[\r\n\v\f\x1c-\x1e\x85\u2028\u2029]+[ \t]*")


def _fallback_line(reason: str) -> str | None:
    """The plain-time frame's line for ``reason`` in the device language, or
    ``None`` to omit it. The line is the frame's one OPTIONAL part (D9): a
    catalog that cannot supply it costs the line, never the time, and never
    puts a raw key on the glass."""
    key = FALLBACK_LINE_KEYS.get(reason)
    if key is None:
        return None
    try:
        # The device language's OWN bundle: get() would serve English for a
        # bundle that lacks the key, and this line must never be another
        # language (D9 — a missing translation costs the line).
        text = strings_catalog.get_own(key)
    except Exception as e:  # never raises by contract; the frame must not depend on that
        logging.warning(f"plain-time frame: catalog lookup for {key} failed ({e}); omitting the line")
        return None
    if not isinstance(text, str):
        return None
    # One line, always: the catalog lint allows newlines, and Pillow cannot
    # measure multiline text (ValueError) — a hand-wrapped translation must
    # not be able to stop the painter (litclock-dev#532 review).
    # Only the line breakers Pillow refuses: str.split() would also fold NBSP
    # and U+202F, which French typography puts before ':;?!' (round-2 review).
    text = _LINE_BREAKS_RE.sub(" ", text).strip()
    return text or None


def _draw_time_fallback(draw, now, reason: str) -> None:
    """The plain-time frame: the last tier of every degradation chain —
    right time, no quote, never a stale quote, and never another language's
    quote (litclock-dev#532 R1/R7). One body for every cause (litclock-dev#605
    item 13), so the failure paths cannot drift apart; ``reason`` only picks
    the line under the time (D8)."""
    centre_x = DISPLAY_SIZE[0] // 2
    time_font = ImageFont.truetype(FONT_PATH, FALLBACK_TIME_FONT_SIZE)
    draw.text(
        (centre_x, FALLBACK_TIME_BASELINE_Y), _clock_text(now), font=time_font, anchor="ms", fill=0
    )
    # Everything about the line is optional and guarded as ONE unit: its own
    # font (the credits font, whose loss is also a reason the runtime tier
    # failed), measuring it and drawing it. Whatever goes wrong costs the
    # line, never the time already drawn (litclock-dev#532 review: an
    # unguarded font load or a multiline translation used to escape main()
    # and leave the previous minute on the panel).
    try:
        _draw_fallback_line(draw, centre_x, reason)
    except Exception as e:
        logging.warning(f"plain-time frame: {reason} line not drawn ({e}); time only")


def _draw_fallback_line(draw, centre_x: int, reason: str) -> None:
    line = _fallback_line(reason)
    if line is None:
        return
    # Shrink to fit the panel width; a line that does not fit even at the
    # floor is omitted rather than clipped (D7: never clip translated copy).
    # Width scales with size, so one measurement at the design size predicts
    # the fitting size and at most a couple of loads confirm it (instead of
    # up to nine font loads on the per-minute path).
    budget = DISPLAY_SIZE[0] - 2 * FALLBACK_LINE_MARGIN_X
    font = ImageFont.truetype(FALLBACK_LINE_FONT_PATH, FALLBACK_LINE_FONT_SIZE)
    width = draw.textlength(line, font=font)
    size = FALLBACK_LINE_FONT_SIZE if width <= budget else int(FALLBACK_LINE_FONT_SIZE * budget / width)
    while size >= FALLBACK_LINE_MIN_FONT_SIZE:
        if size != FALLBACK_LINE_FONT_SIZE:
            font = ImageFont.truetype(FALLBACK_LINE_FONT_PATH, size)
        if draw.textlength(line, font=font) <= budget:
            draw.text((centre_x, FALLBACK_LINE_BASELINE_Y), line, font=font, anchor="ms", fill=0)
            return
        size -= 1
    logging.warning("plain-time frame %s line too wide for the panel; omitted", reason)


def _png_set_present() -> bool:
    """Whether the English PNG set is on disk at all. Tells an uncovered
    minute (a GAP) from a device whose images/ is missing or quarantined
    (a FAILURE) on the PNG tier, where both look like an empty glob."""
    # A real quote PNG, not merely a non-empty directory: a leftover temp
    # file or a .DS_Store must not make a missing set read as healthy.
    try:
        with os.scandir(os.path.join(PROJECT_ROOT, "images", "metadata")) as it:
            return any(e.name.startswith("quote_") and e.name.endswith("_credits.png") for e in it)
    except OSError:
        return False


# The dry-run's exit statuses beyond 0/1, which scripts/update.sh's self-test
# branches on. 3: `--require-runtime-render` and the text renderer did not
# produce the frame (litclock-dev#871). 4: `--sample-quote` and the device
# language's corpus has no quote to sample at all (litclock-dev#532 R10) — a
# corpus problem, not a renderer one, memoed under its own token.
EXIT_NOT_RUNTIME = 3
EXIT_NO_SAMPLE = 4


def _render_target() -> datetime:
    """The minute this run paints: now plus the render lead (litclock-dev#762).

    AWARE arithmetic, not naive addition. `datetime.now() + timedelta` would
    fabricate a minute that does not exist across a DST spring-forward: at
    01:59:56 CST naive +4s gives 02:00, while the real local time four
    seconds later is 03:00 CDT — and that fake minute would reach the panel,
    the masthead and /api/status. Attaching the offset, adding, then
    re-normalising resolves the zone at the TARGET instant and lands on 03:00.

    It stays routed through datetime.now() ON PURPOSE. Every time-freezing
    test in this repo subclasses datetime and overrides now(); an
    implementation reading _time.time() instead escapes all of them, and
    because the escape yields the REAL clock the resulting failure is
    time-dependent — green locally, red in CI, or the reverse (/review)."""
    return (datetime.now().astimezone() + timedelta(seconds=RENDER_LEAD_S)).astimezone().replace(tzinfo=None)


# litclock-dev#963 — partial refresh. When the new frame differs from the one
# on the glass only inside the masthead (fortune mode's time, mostly), redraw
# it with the panel's partial waveform: ~0.8s and no black-white flash,
# against ~3s of flashing for a full refresh. Measured on the bench
# 2026-10-26 (V2 panel): a partial works straight out of the painter's deep
# sleep, leaves the rest of the frame intact, and ten in a row left no
# ghosting. Every frame whose body changed (a new quote, a new fortune at the
# end of a dwell slot, the nightly clear) is still a full refresh, which also
# bounds how many partials can stack up — so no forced periodic full refresh
# (owner, 2026-10-26).
#
# There is no way to ask the panel whether it can do this: the HAT is
# write-only. Older V2 panels (before 2023-10-24, per the vendor) are not
# supported by decision; LITCLOCK_PARTIAL_REFRESH=false puts any clock back on
# a full refresh every minute, exactly as before.
PARTIAL_REFRESH_S = 0.8  # display_Partial, measured 0.80-0.83s on the bench


def _partial_refresh_enabled() -> bool:
    """On unless LITCLOCK_PARTIAL_REFRESH says false/0/no/off. Empty is the
    env.sh.sample unset idiom and means the default."""
    return (os.getenv("LITCLOCK_PARTIAL_REFRESH") or "").strip().lower() not in ("false", "0", "no", "off")


def _masthead_only_change(previous: Image.Image, image: Image.Image) -> bool:
    """True when every pixel that differs between the two frames lies above
    the quote area (an identical frame counts)."""
    box = ImageChops.difference(previous.convert("L"), image.convert("L")).getbbox()
    return box is None or box[3] <= QUOTE_AREA_Y


def _wait_for_partial() -> None:
    """Hold a partial refresh until it will land on the minute. The timer
    fires at :56 so a ~3s full refresh settles near :00; a 0.8s partial fired
    at once would show the new time about three seconds early. Wait until the
    nearest minute boundary minus the partial's own duration.

    Epoch arithmetic, not the naive target: every zone offset is a whole
    number of minutes, so local minute boundaries are multiples of 60s in
    epoch time, and a DST jump cannot turn the wait into an hour or into
    nothing (Codex review). Still read through datetime.now(), so the
    frozen-clock tests reach it (see _render_target). Bounded by the lead,
    and no wait at all when the run is already past the boundary."""
    now_ts = datetime.now().astimezone().timestamp()
    delay = round(now_ts / 60) * 60 - now_ts - PARTIAL_REFRESH_S
    if delay > 0:
        _time.sleep(min(delay, RENDER_LEAD_S))


def _sample_minute(start: datetime) -> datetime | None:
    """The first minute at or after ``start`` (wrapping past 23:59) whose
    device-language corpus has a non-mature row, or ``None`` when there is
    none — a corpus that is unavailable, empty, or all mature.

    litclock-dev#532 R10: the weekly self-test renders THIS minute rather than
    the current one, so it checks rendering and never corpus coverage. On a
    partly covered language the current minute is uncovered up to 20% of the
    time at the 80% floor, and a plain-time frame there is the correct answer
    — which would read as a failed self-test. English's corpus covers every
    minute, so for English this is the current minute and nothing changes.
    Non-mature regardless of ALLOW_NSFW_QUOTES: the sample must be renderable
    on every device, and a mature-only minute would be a gap on most."""
    try:
        # Resolved once, not per minute (review: 1440 resolutions cost ~0.4s
        # here and several seconds on a Pi Zero). Only corpus-shaped errors
        # mean "nothing to sample": a bug in the indexer must surface as the
        # dry-run's exit 1 (selftest-failed), not as a missing corpus.
        index = quote_corpus.device_index()
    except (quote_corpus.CorpusUnavailable, OSError, ValueError) as err:  # ValueError: UnicodeDecodeError
        logging.warning(f"self-test sample: the device corpus is unavailable ({err})")
        return None
    base = start.replace(second=0, microsecond=0)
    for offset in range(24 * 60):
        minute = base + timedelta(minutes=offset)
        if any(not row["is_nsfw"] for row in index.get(minute.strftime("%H%M"), ())):
            return minute
    return None


def _pick_quote(now: datetime, allow_nsfw: bool) -> tuple[dict | None, str | None]:
    """The literary-quote tiers for ``now``: runtime text, then the English
    PNG set. Returns (quote_meta, fallback_reason) with the same contract as
    main()'s — exactly one of the two is None. Lifted out of main() for
    litclock-dev#533, which puts the fortune tier in front of it."""
    quote_meta = None
    fallback_reason: str | None = None
    # litclock-dev#532 R7: the pre-rendered PNG set is ENGLISH-ONLY — the one
    # standing English asymmetry, kept because those PNGs predate runtime
    # render. Every other language always renders text: the flag, the
    # validation marker, the self-test and Stage B decide text-vs-PNG for the
    # PNG set's language only, and a non-English minute that cannot render
    # paints the plain time in its own language (R1), never English PNGs.
    #
    # quote_language(), not active_language(): with the registry unreadable the
    # latter calls every device English, and a Spanish clock would be back on
    # English PNGs (None then — the text tier reports the failure). Guarded:
    # resolving the language reads two files, and nothing here may stop the
    # minute's paint (litclock-dev#532 review).
    language_known = True
    try:
        png_tier = strings_catalog.quote_language() == strings_catalog.CANONICAL_LANGUAGE
    except Exception as e:
        # Neither tier: the PNGs are English-only, and the text tier without a
        # known language would also skip English's validation gate. A failed
        # minute, said plainly (round-2 review).
        logging.error(f"could not resolve the device language ({e}); painting the plain time")
        language_known = png_tier = False
        fallback_reason = FALLBACK_FAILURE
    runtime_attempted = language_known and ((not png_tier) or _runtime_render_enabled())
    if runtime_attempted:
        quote_meta, fallback_reason = get_current_quote_runtime(now=now, allow_nsfw=allow_nsfw)
    if quote_meta is None and png_tier:
        png_meta = get_current_quote(now=now, allow_nsfw=allow_nsfw)
        if png_meta is not None:
            if runtime_attempted:
                # "image" alone can't tell "runtime rendering is off on this
                # device" from "runtime rendering is failing every minute" —
                # and with the flag defaulting off, EVERY device reports the
                # former today, which would make the fleet evidence useless
                # (litclock-dev#543 review F3). Distinguish the alarm condition.
                png_meta["render_mode"] = "image-fallback"
            quote_meta, fallback_reason = png_meta, None
        elif not runtime_attempted:
            fallback_reason = FALLBACK_GAP if _png_set_present() else FALLBACK_FAILURE
        # else: the runtime tier's own reason stands — a PNG miss after a
        # runtime failure is still a failure, after a runtime gap still a gap.

    return quote_meta, fallback_reason


def main(at: datetime | None = None):
    """Compose and return the current-minute image plus the metadata the
    __main__ block needs to publish the OV3 status file AFTER the e-ink
    hardware update confirms the new frame is on the panel.

    Pure-ish: never publishes the status file or touches the heartbeat —
    those are __main__'s job. Codex /review on M2 caught the M2 draft
    publishing the status file inside main(), before the hardware update.
    If `epd.display()` fails (GPIO contention, SPI timeout), the panel
    keeps showing the OLD frame while /api/status reports a FRESH
    `picked_at` for the new quote — defeats the stale signal. Publishing
    after `epd.display()` returns successfully is the right contract.

    ``at`` overrides the render target — only the self-test's
    ``--sample-quote`` dry-run passes it (litclock-dev#532 R10); the painter
    always renders _render_target().

    Returns: (image, quote_meta, now, fallback_reason) — quote_meta is None
    when no quote could be shown this minute (the plain-time frame is
    already drawn), and fallback_reason then says why: FALLBACK_GAP or
    FALLBACK_FAILURE (litclock-dev#532 D8). fallback_reason is None whenever
    quote_meta is not."""
    logging.info("Starting main function")

    openweathermap_apikey = os.getenv("OPENWEATHERMAP_APIKEY")
    location_lat = os.getenv("WEATHER_LATITUDE")
    location_long = os.getenv("WEATHER_LONGITUDE")
    units = os.getenv("WEATHER_UNITS", "imperial")
    allow_nsfw = os.getenv("ALLOW_NSFW_QUOTES", "false").lower() == "true"
    # WEATHER_ENABLED master toggle (Control PWA M3, litclock-dev#245). Default true
    # to preserve pre-M3 behavior on Pis that don't have the key yet —
    # update.sh's env.sh.sample merge will add it on the next update.
    #
    # litclock-dev#790: resolved through config.weather_enabled() rather than
    # inline, because diagnostics answered the same question with the opposite
    # default and the panel and the support bundle disagreed. One function, so
    # they cannot drift again. No argument = read os.environ, which is what
    # runtheclock.sh has already sourced env.sh into.
    weather_enabled = config.weather_enabled()

    weather = None
    if not weather_enabled:
        logging.info("WEATHER_ENABLED=false, skipping weather")
    elif location_lat and location_long:
        try:
            if openweathermap_apikey:
                weather_provider = openweathermap.OpenWeatherMap(
                    openweathermap_apikey, location_lat, location_long, units
                )
            else:
                weather_provider = open_meteo.OpenMeteo(location_lat, location_long, units)
            weather = weather_provider.get_weather()
            logging.debug(f"Weather data retrieved: {weather}")
        except ImportError:
            raise  # Don't mask broken dependencies
        except Exception as e:
            logging.warning(f"Weather unavailable, continuing without weather data: {e}")
    else:
        logging.info("No location configured, skipping weather")

    image = Image.new(mode="1", size=DISPLAY_SIZE, color=255)

    temp_high = temp_low = icon_path = None
    if weather is not None and not isinstance(weather, dict):
        logging.warning(f"weather payload is not a mapping ({type(weather).__name__}); suppressing weather")
        weather = None
    if weather is not None:
        degrees = "°F" if units == "imperial" else "°C"
        temp_high = _format_temp(weather.get("temperatureMax"), degrees)
        temp_low = _format_temp(weather.get("temperatureMin"), degrees)
        if temp_high is None or temp_low is None:
            # Malformed provider payload (None/NaN/non-numeric/absurd) —
            # suppress weather for this render rather than crash or draw
            # garbage into the masthead (litclock-dev#538 review).
            logging.warning(
                f"weather temps unusable (max={weather.get('temperatureMax')!r} "
                f"min={weather.get('temperatureMin')!r}); suppressing weather"
            )
            weather = None
            temp_high = temp_low = None
        else:
            icon = weather.get("icon")
            # icon may be absent/garbage without costing the temps —
            # _compose_masthead handles icon_path=None (litclock-dev#541 review)
            icon_path = os.path.join(PROJECT_ROOT, "icons", f"{icon}.xbm") if isinstance(icon, str) and icon else None
            logging.debug(f"Weather: {temp_high} / {temp_low}, icon: {icon_path}")

    # ONE offset target, threaded through every downstream time site: the quote
    # pick, the masthead date, the time-fallback draw, the status file and the
    # nightly clear gate. Independent datetime.now() calls would disagree with
    # it — at 23:59:56 the masthead date must already be tomorrow's.
    #
    # (Why aware arithmetic, and why datetime.now(): _render_target.)
    now = at if at is not None else _render_target()
    mode = _content_mode()
    quote_meta = get_current_fortune(now) if mode == "fortune" else None
    fallback_reason: str | None = None
    if quote_meta is None:
        # Quotes mode, or fortune mode with nothing to show (package missing,
        # an empty category pick, a render failure): the literary quote is the
        # fallback, never a blank body. The masthead still leads with the time.
        quote_meta, fallback_reason = _pick_quote(now, allow_nsfw)

    draw = ImageDraw.Draw(image)

    if quote_meta is None:
        # No quote for this minute — the plain-time frame, with the line that
        # says why (D8). Right time, no quote — never a stale quote.
        if fallback_reason is None:  # every path above sets one; never paint a reason-less frame silently
            fallback_reason = FALLBACK_FAILURE
        _draw_time_fallback(draw, now, fallback_reason)
        logging.info(f"Time drawn on image ({fallback_reason})")
    elif "image" in quote_meta:
        image.paste(quote_meta["image"], (0, QUOTE_AREA_Y))
        logging.info(f"Runtime-rendered quote pasted (fs={quote_meta.get('font_size')})")
    else:
        try:
            quote_image = Image.open(quote_meta["image_path"]).convert("1")
            image.paste(quote_image, (0, QUOTE_AREA_Y))
            logging.info(f"Quote image pasted from {quote_meta['image_path']}")
        except Exception as e:
            # Corrupt/unreadable/race-deleted PNG must degrade to the plain
            # time draw, not kill the whole per-minute render (litclock-dev#537
            # review) — this tier is also the runtime-render fallback, so
            # an exception here would void the "never crash-loop" contract.
            logging.error(f"quote PNG unusable ({quote_meta['image_path']}): {e}; drawing time")
            quote_meta = None
            fallback_reason = FALLBACK_FAILURE
            _draw_time_fallback(draw, now, fallback_reason)

    # One layout per mode (litclock-dev#533 design, 2026-08-12): fortune mode leads with
    # the time on EVERY frame, including a quote fallback, so the panel never
    # reconfigures itself minute to minute.
    time_text = _clock_text(now) if mode == "fortune" else None
    _compose_masthead(image, draw, masthead_date(now), temp_high, temp_low, icon_path, time_text=time_text)

    _stamp_update_failed_glyph(image, draw)
    _composite_settings_qr(image)

    logging.info("Image drawing completed")
    # Status-file publication is __main__'s job, after the hardware
    # confirms the new frame reached the panel (codex /review M2 catch).
    return image, quote_meta, now, fallback_reason


def _runtime_render_enabled() -> bool:
    if os.getenv("LITCLOCK_RUNTIME_RENDER", "false").lower() not in ("1", "true"):
        return False
    try:
        with open(RUNTIME_VALIDATED_MARKER, encoding="utf-8") as fh:
            marker = fh.read()
    except OSError:
        logging.warning(
            "LITCLOCK_RUNTIME_RENDER is set but this device is not validated "
            f"({RUNTIME_VALIDATED_MARKER} missing) — run "
            "`venv/bin/python3 tools/validate_measurement.py check --stamp` "
            "first; using pre-rendered images"
        )
        return False
    except UnicodeDecodeError:
        # A CORRUPT marker, which is not the same as a missing one and used to
        # be the one input to this function that could not degrade: `open(...,
        # encoding="utf-8")` defers decoding to `.read()`, and a
        # UnicodeDecodeError is a ValueError, so `except OSError` did not catch
        # it. It propagated out of the guard whose entire contract is to return
        # False, and the painter died instead of falling back to the PNG tier.
        #
        # NOT latent: this line is unreachable only where
        # `LITCLOCK_RUNTIME_RENDER` is false, and the fielded clock is not one
        # of those — checked directly on 2026-09-21, flag true and
        # `render_mode: runtime` in its live status file. The gifted clocks and
        # the internet flashers are on images and are not exposed yet;
        # litclock-dev#871 Stage B would have exposed all of them in one weekly
        # tick, which is how this was found (in that PR's review, before it
        # shipped).
        #
        # It matters more than a stray traceback because nothing catches the
        # aftermath: `litclock-bootcheck` asks whether the heartbeat exists at
        # all since boot, not whether it is RECENT, so a clock that painted all
        # week and stopped after a Sunday update still looks healthy to it.
        #
        # Rejected rather than salvaged with errors="replace": a marker is a
        # proof, and a proof that did not survive the disk is not one. One
        # re-stamp re-earns it, and until then the device paints PNGs.
        logging.warning(
            f"runtime-render validation marker at {RUNTIME_VALIDATED_MARKER} is not valid "
            "UTF-8 — treating this device as unvalidated and using pre-rendered images. "
            "Re-run `venv/bin/python3 tools/validate_measurement.py check --stamp` to "
            "replace it (litclock-dev#871 review)"
        )
        return False
    # The marker records which FreeType it validated. A freetype-py bump
    # (OTA venv rebuild) must invalidate it — hinted metrics are version-
    # sensitive and a stale marker would keep the flag honored in a
    # now-unproven environment (litclock-dev#537 review, finding 2).
    try:
        import freetype

        current = ".".join(map(str, freetype.version()))
    except Exception as e:
        logging.warning(f"LITCLOCK_RUNTIME_RENDER set but freetype-py unusable ({e}); using pre-rendered images")
        return False
    stamped = next((t.split("=", 1)[1] for t in marker.split() if t.startswith("freetype=")), None)
    if stamped != current:
        logging.warning(
            f"runtime-render validation marker is for FreeType {stamped}, installed is "
            f"{current} — re-run `tools/validate_measurement.py check --stamp`; "
            "using pre-rendered images"
        )
        return False
    # litclock-dev#604 — the freetype version alone is not the proof. The
    # stamp binds to a digest of (freetype version, font bytes, expected-dump
    # bytes); a font swap or dump regen with the SAME wheel previously left a
    # stale marker in force and the device kept runtime-rendering against an
    # invalid validation. Markers from before the digest existed carry no
    # digest= token and are rejected the same way — one re-stamp re-earns it.
    stamped_digest = next((t.split("=", 1)[1] for t in marker.split() if t.startswith("digest=")), None)
    try:
        from quote_renderer import runtime_validation_digest

        current_digest = runtime_validation_digest()
    except Exception as e:
        logging.warning(
            f"LITCLOCK_RUNTIME_RENDER set but the validation digest could not be computed ({e}); "
            "using pre-rendered images"
        )
        return False
    if stamped_digest != current_digest:
        logging.warning(
            "runtime-render validation marker no longer matches this device's proof inputs "
            f"(fonts / expected-measurements dump / FreeType) — stamped "
            f"{(stamped_digest or 'none')[:12]}, current {current_digest[:12]}. Re-run "
            "`tools/validate_measurement.py check --stamp`; using pre-rendered images "
            "(litclock-dev#604)"
        )
        return False
    return True


def _runtime_frame_ok(frame) -> str | None:
    """Sanity-check a rendered mode-"L" quote frame BEFORE it may replace a
    pre-rendered PNG on the panel. Returns a rejection reason or None.

    Catches the "rendered but garbage" class that raises nothing: wrong
    canvas, an all-white frame, or ink inside the settings-QR quiet-zone
    notch (which _composite_settings_qr would silently erase — litclock-dev#530).
    Ink = pixel < 128, matching both GD's AA rounding and the dither=NONE
    conversion this frame is about to get. Subtle wrongness (bad break,
    off-by-one font size) is NOT detectable here — that correctness is
    proven per-PR by the render-invariants CI, not per-minute on the Pi."""
    if frame.size != (DISPLAY_SIZE[0], QUOTE_AREA_H):
        return f"unexpected frame size {frame.size}"
    ink = frame.point(lambda v: 255 if v < 128 else 0)
    if ink.getbbox() is None:
        return "frame has no ink"
    notch_rows = QR_NOTCH_BOTTOM - QUOTE_AREA_Y + 1  # panel rows 80..86 -> frame rows 0..6
    notch = ink.crop((QR_POSITION[0] - QR_QUIET_ZONE, 0, frame.size[0], notch_rows))
    if notch.getbbox() is not None:
        return "ink inside the settings-QR quiet-zone notch"
    return None


def _persist_runtime_frame(frame) -> str | None:
    """Atomically write the painted frame to tmpfs (same tempfile+replace
    pattern as the status file). Best-effort: the render must not fail
    because /run/litclock is missing on a dev box.

    Ordering note (litclock-dev#537 review, finding 7): this runs at selection
    time, BEFORE epd.display() confirms — unlike the status file, which
    __main__ writes only post-display (OV3). If the panel write then
    fails, this PNG shows a frame the panel never painted. Accepted: no
    consumer dereferences image_path today (verified control_server +
    diagnostics); it exists for support bundles, where
    what-the-clock-TRIED-to-paint is the useful artifact."""
    target = os.path.join(RUNTIME_RENDER_DIR, "current-quote.png")
    try:
        fd, tmp = tempfile.mkstemp(dir=RUNTIME_RENDER_DIR, suffix=".png")
        try:
            with os.fdopen(fd, "wb") as fh:
                frame.save(fh, format="PNG")
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return target
    except Exception as e:
        logging.warning(f"runtime frame not persisted (non-fatal): {e}")
        return None


def get_current_quote_runtime(
    now: datetime | None = None,
    allow_nsfw: bool = False,
) -> tuple[dict | None, str | None]:
    """Render the current-minute quote from the corpus (litclock-dev#531 Stage 2).

    Returns ``(meta, None)`` on success — the same metadata shape as
    get_current_quote() plus an ``image`` key holding the ready-to-paste
    mode-"1" frame (converted with dither=NONE — the renderer emits grey AA
    fringes, and default Floyd-Steinberg would speckle them into black
    dots) — or ``(None, reason)`` (litclock-dev#532 D8): FALLBACK_GAP when
    the corpus has no usable row for this minute, FALLBACK_FAILURE on
    anything else — missing freetype-py, a corpus that will not load
    (``quote_corpus.CorpusUnavailable`` included), RenderError, sanity
    rejection. Never raises: one bad row must degrade one minute, never
    crash-loop the timer. What the caller does with a miss depends on the
    language — only the PNG set's language has a PNG tier to try next."""
    now = now or datetime.now()
    try:
        import quote_renderer
    except Exception as e:  # not just ImportError: a truncated module raises SyntaxError at import
        logging.warning(f"runtime render wanted but renderer unavailable ({e!r})")
        return None, FALLBACK_FAILURE
    try:
        # csv_path=None: quote_corpus.corpus_path() — the active language's
        # registry corpus (litclock-dev#870); LITCLOCK_CORPUS_CSV still
        # overrides it, and a local constant here was the litclock-dev#594 bug
        # (it ignored that override and keyed a second cache entry for the same
        # file). The PNG lookup below reads quote_corpus.image_corpus_path() on
        # purpose — the corpus the images were baked from.
        rows = quote_renderer.rows_for_time(None, now.strftime("%H%M"))
    except Exception as e:
        logging.error(f"runtime render: corpus read failed: {e}")
        return None, FALLBACK_FAILURE
    if not allow_nsfw:
        rows = [r for r in rows if not r.is_nsfw]
    if not rows:
        # Corpus gap. SAID, since litclock-dev#871: the self-test reads a
        # fallback as "did not render text", and without this line the journal
        # could not tell a gap at this minute from a renderer that cannot
        # render at all.
        logging.warning(f"runtime render: no rows for {now.strftime('%H:%M')} after the NSFW filter")
        return None, FALLBACK_GAP
    row = rows[randrange(len(rows))]
    # ONE guard around render+validate+convert+persist: the "never raises"
    # contract must hold for unexpected shapes too, not just RenderError
    # (litclock-dev#537 review — a sanity-check or convert crash previously escaped
    # and would have killed the whole render).
    try:
        _, credits_img, font_size, _ = quote_renderer.render_row(row)
        reason = _runtime_frame_ok(credits_img)
        if reason is not None:
            logging.error(f"runtime render rejected for {row.basename}: {reason}; falling back")
            return None, FALLBACK_FAILURE
        frame = credits_img.convert("1", dither=Image.Dither.NONE)
    except Exception as e:
        logging.error(f"runtime render failed for {row.basename}: {e}; falling back")
        return None, FALLBACK_FAILURE
    return {
        "quote": row.quote,
        "author": row.author,
        "title": row.title,
        "time": row.time,
        "image_path": _persist_runtime_frame(frame) or "",
        "picked_at": _time.time(),
        "image": frame,
        "font_size": font_size,
        "render_mode": "runtime",
    }, None


def _content_mode() -> str:
    """LITCLOCK_CONTENT_MODE from the environment runtheclock.sh sourced env.sh into
    (litclock-dev#533). Never raises: a broken fortune module means quotes."""
    try:
        import fortune  # noqa: PLC0415

        return fortune.content_mode(os.environ.get("LITCLOCK_CONTENT_MODE"))
    except Exception as e:
        logging.error(f"content mode unreadable ({e!r}); painting quotes")
        return "quotes"


def get_current_fortune(now: datetime) -> dict | None:
    """Fortune mode's body (litclock-dev#533): the dwell slot's fortune as a
    ready-to-paste mode-"1" frame plus status metadata, or None — the caller
    then paints a literary quote. Never raises."""
    try:
        import fortune  # noqa: PLC0415

        # The frame check runs inside the pick, so a rejected frame costs one
        # candidate rather than the whole dwell slot (review).
        picked = fortune.current_fortune(now, frame_ok=_runtime_frame_ok)
        if picked is None:
            return None
        item, body = picked
        frame = body.convert("1", dither=Image.Dither.NONE)
    except Exception as e:
        logging.error(f"fortune unavailable ({e!r}); painting a quote")
        return None
    return {
        "quote": item.text,
        "author": item.attribution,
        "title": "",
        "time": now.strftime("%H:%M"),
        "image_path": _persist_runtime_frame(frame) or "",
        "picked_at": _time.time(),
        "image": frame,
        "render_mode": "fortune",
    }


def get_current_quote(
    now: datetime | None = None,
    allow_nsfw: bool = False,
) -> dict | None:
    """Return current-minute quote metadata from the English PNG set, or
    ``None`` if no PNG matches this HH:MM (the caller paints the plain-time
    frame, a gap or a failure by ``_png_set_present()``).

    Pure function (litclock-dev#245 A7) — same selection logic feeds both the e-ink
    render path in main() and the /api/status response in
    src/control_server/routes/status.py. Random pick within the bucket
    matches existing per-minute rotation behavior.

    Returns:
        ``{quote, author, title, time, image_path, picked_at}`` where
        time is ``HH:MM``, image_path is the absolute PNG path, and
        picked_at is float epoch seconds at selection time.
    """
    now = now or datetime.now()
    hour_minute = now.strftime("%H%M")
    quotes = glob(os.path.join(PROJECT_ROOT, "images", "metadata", f"quote_{hour_minute}_*_credits.png"))
    if not allow_nsfw:
        quotes = [q for q in quotes if "_nsfw_" not in q]
    if not quotes:
        return None
    chosen = quotes[randrange(len(quotes))]
    # litclock-dev#594 review: since quote_corpus became the ONE corpus walk, a
    # torn/corrupt CSV throws in BOTH render tiers — runtime already
    # returns None for it, and an unguarded raise here would kill the PNG
    # tier too, stalling the panel every minute until the file heals. The
    # PNG needs no CSV to paint: degrade to empty meta (right image, no
    # PWA metadata; _write_status_file skips the empty case by design).
    try:
        meta = quote_corpus.lookup_by_filename(chosen) or {}
    except Exception as e:
        logging.error(f"corpus lookup failed for {os.path.basename(chosen)}: {e}; painting PNG without metadata")
        meta = {}
    return {
        "quote": meta.get("quote", ""),
        "author": meta.get("author", ""),
        "title": meta.get("title", ""),
        "time": meta.get("time", now.strftime("%H:%M")),
        "image_path": chosen,
        "picked_at": _time.time(),
        "render_mode": "image",
    }


def _previous_status() -> tuple[int, str | None]:
    """The failure count and minute ("HH:MM") the LAST published frame
    carried, or (0, None).

    The painter is a fresh process every minute, so the run of failures
    lives in the status file it already publishes (litclock-dev#532 R14 —
    tmpfs, no SD writes, no new memo). A missing, unreadable, corrupt or
    foreign-shaped file counts from 0: the warning is at worst delayed a few
    minutes, and the painter must never fail over its own status file."""
    try:
        with open(STATUS_FILE, encoding="utf-8") as f:
            payload = json.load(f)
        value = payload.get("consecutive_failures", 0)
        minute = payload.get("time")
    except Exception:
        return 0, None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        value = 0
    return value, minute if isinstance(minute, str) else None


def _write_status_file(
    quote_meta: dict | None, now: datetime, fallback_reason: str | None = None, *, refresh: str | None = None
) -> None:
    """Atomically publish what's on the e-ink so /api/status can mirror it.

    tempfile + os.replace so a reader never sees a torn file — deliberately
    WITHOUT config.py's fsync barriers (litclock-dev#893): the target is on
    tmpfs, so there is nothing to make durable. Best-effort — a missing or unwritable target directory
    logs at WARN and the render proceeds. Path overrideable via
    ``LITCLOCK_STATUS_FILE`` for tests / dev boxes without /var/run.

    Refuses to write when ``quote_meta`` is non-empty but the corpus
    lookup returned no text (PNG exists on disk but the CSV row is
    missing — an out-of-sync corpus). Rationale (adversarial /review on
    M2): writing a fresh ``picked_at`` with empty quote/author/title
    would make the PWA hero render the "Starting up…" empty-state copy
    while the e-ink is showing a real quote — gaslighting the user
    looking at the device. Better to leave the file stale and let the
    PWA show the stale-quote banner (D2) — that's an honest signal that
    something is broken."""
    if quote_meta is not None and not quote_meta.get("quote"):
        logging.warning(
            "corpus lookup empty for chosen image (corpus/images out of sync); "
            "skipping status-file write — PWA will render stale banner"
        )
        return

    # litclock-dev#532 D8/R14: a quote frame resets the run and a failure
    # frame extends it. A GAP neither counts nor resets: no row means the
    # renderer never ran, so the frame says nothing either way — and resetting
    # on it let a partly covered language with a broken renderer (every
    # covered minute a failure, every uncovered one a gap) keep the warning
    # off forever (litclock-dev#532 review, red team + adversarial). A repaint
    # of the minute already published (an update's timer restart, a Settings
    # save's ad-hoc tick) does not count again either, so "3 in a row" means
    # three minutes. A no-quote frame with no stated reason is a failure:
    # claiming a gap we cannot prove would hide a broken clock.
    if quote_meta is not None:
        fallback_reason = None
    elif fallback_reason not in (FALLBACK_GAP, FALLBACK_FAILURE):
        fallback_reason = FALLBACK_FAILURE
    if fallback_reason is None:
        consecutive_failures = 0
    else:
        previous, previous_minute = _previous_status()
        repaint = previous_minute == now.strftime("%H:%M")
        consecutive_failures = previous + (fallback_reason == FALLBACK_FAILURE and not repaint)

    payload: dict = {
        "time": now.strftime("%H:%M"),
        "picked_at": _time.time(),
        # Which tier of the render chain actually painted this frame
        # (litclock-dev#531). The chain is invisible to the viewer, so this is the
        # ONLY evidence distinguishing a healthy runtime-render fleet from
        # one silently falling back — what the images-retirement decision
        # rests on. Values:
        #   "runtime"        rendered on-device from the corpus
        #   "image-fallback" runtime was ATTEMPTED and lost -> the alarm
        #   "image"          runtime not enabled here; PNG by design
        #   "time-only"      no quote this minute -> plain-time frame drawn
        #   "fortune"        fortune mode painted a fortune (litclock-dev#533);
        #                    a fortune-mode miss paints a quote and reports
        #                    that quote's own tier
        #   None             unknown (a meta without the field; never
        #                    claim a tier we can't prove — litclock-dev#543 F4)
        # Rendered in the diagnostics page + support bundle.
        "render_mode": "time-only" if quote_meta is None else quote_meta.get("render_mode"),
        # Why a time-only frame has no quote: "gap" or "failure" (D8), absent
        # on a quote frame. render_mode stays "time-only" for both, so the
        # self-test, diagnostics and the support bundle are untouched.
        # consecutive_failures is the run of failure frames ending with this
        # one, gaps carried through (R14); the PWA warns at CONSECUTIVE_FAILURES_WARN_AT.
        "consecutive_failures": consecutive_failures,
    }
    # "full" or "partial" (litclock-dev#963): which waveform painted this
    # frame, so a partial refresh is checkable over SSH. Absent when unknown.
    if refresh is not None:
        payload["refresh"] = refresh
    if fallback_reason is not None:
        payload["fallback_reason"] = fallback_reason
    if quote_meta:
        payload.update(
            {
                "quote": quote_meta.get("quote", ""),
                "author": quote_meta.get("author", ""),
                "title": quote_meta.get("title", ""),
                "image_path": quote_meta.get("image_path", ""),
            }
        )

    target = Path(STATUS_FILE)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logging.warning(f"status file: parent mkdir failed: {e}")
        return

    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), prefix=".litclock-status.tmp.")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp_path, target)
            tmp_path = None
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
    except OSError as e:
        logging.warning(f"status file write failed (non-fatal): {e}")


def _resolve_lan_ip() -> str | None:
    """Return the Pi's primary LAN IPv4 via the stdlib connect-trick, or None
    if no usable address is available.

    The trick: open a UDP socket and call ``connect()`` to a routable address.
    The kernel picks the egress interface and binds the socket to its IP
    without sending any packet — ``getsockname()`` returns that IP. We use
    ``1.1.1.1`` because it's stable and externally routable; the choice is
    arbitrary, no traffic actually leaves the box.

    Returns None on:
        - any OSError (no network, no default route, DNS-unconfigured box)
        - a loopback address (127.x — happens on a Pi with only `lo` up)
        - a link-local address (169.254.x — DHCP failed, APIPA self-assigned;
          phones on the same WiFi rarely land on the matching link-local
          segment, so the IP is effectively unreachable from a scanner)

    Issue litclock-dev#306: mDNS (`litclock.local`) is unreliable on Android Chrome and
    many home/guest WiFi networks. Encoding the IP makes the scan path
    bulletproof; the IP refreshes every minute tick so DHCP renewals
    propagate within ~60s.
    """
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.5)
        sock.connect(("1.1.1.1", 80))
        ip = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    if not ip or ip.startswith(("127.", "169.254.")):
        return None
    return ip


def _composite_settings_qr(image: Image.Image) -> None:
    """Paste the persistent settings QR (75x75) at the top-right of the
    e-ink top strip (litclock-dev#245 A6). Encodes ``http://<ip>`` (port omitted at 80,
    litclock-dev#343) when the LAN IP resolves, else falls back to ``QR_URL`` (mDNS
    hostname). Geometry
    locked in M0; the runtime composite mirrors what
    tools/control-pwa/validate_qr_layout.py proves scans on a real phone
    at ~30cm. White-outs the strip corner first for the ISO 18004 quiet
    zone (see QR_QUIET_ZONE) — this notches the y=78 divider under the QR.

    Issue litclock-dev#306: prefer IP over mDNS because Android Chrome and many home
    networks don't resolve `.local` reliably — encoding the IP bypasses
    mDNS for the scan path. The friendly hostname is still typeable for
    users on supportive networks.

    Best-effort — qrcode lib is already a project dep (used by
    src/eink_display.py for the first-boot hotspot QR), but if anything
    goes sideways at runtime we log and skip rather than fail the render."""
    try:
        import qrcode  # noqa: PLC0415 — local import keeps test monkeypatching trivial
        import qrcode.constants  # noqa: PLC0415

        ip = _resolve_lan_ip()
        url = control_base_url(ip) if ip else QR_URL

        qr = qrcode.QRCode(
            version=QR_VERSION,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=QR_BOX_SIZE,
            border=QR_BORDER,
        )
        qr.add_data(url)
        # fit=False because the version is locked at 2 (25 modules @ 3px =
        # 75px). If the URL ever grows past that capacity, qrcode raises —
        # which is the right signal, not silent re-fitting to a larger QR
        # that breaks the top-strip layout. V2-M comfortably fits any
        # IPv4 URL up to 255.255.255.255 and the mDNS hostname fallback.
        qr.make(fit=False)
        qr_img = qr.make_image(fill_color="black", back_color="white").convert("1")
        # Quiet zone (see QR_QUIET_ZONE comment): white-out the strip's
        # top-right corner — including the y=78 divider's rightmost segment,
        # drawn earlier in compose — so the QR gets its 4 modules of white on
        # the right/left/bottom. Done here (not at the divider draw site) so
        # a failed QR build leaves the divider intact end-to-end.
        # Clear through QR_NOTCH_BOTTOM (row 86): covers every row the
        # divider paints (77..80 — PIL centers a width-4 line on DIVIDER_Y)
        # plus the full 4-module quiet zone below the QR, so the bottom
        # clearance holds by construction regardless of divider geometry,
        # draw ordering, or future corpus content. Rows 81..86 are the
        # quote images' top margin today (worst corpus ink starts at 87),
        # so this is currently a no-op there.
        notch = ImageDraw.Draw(image)
        notch.rectangle(
            [(QR_POSITION[0] - QR_QUIET_ZONE, 0), (DISPLAY_SIZE[0] - 1, QR_NOTCH_BOTTOM)],
            fill=255,
        )
        image.paste(qr_img, QR_POSITION)
    except Exception as e:
        logging.warning(f"QR composite skipped (non-fatal): {e}")


# Location of the marker update.sh writes on smoke-test failure.
UPDATE_FAILED_MARKER = os.environ.get("LITCLOCK_UPDATE_FAILED_MARKER", "/var/lib/litclock/update-failed")

# tmpfs heartbeat read by litclock-lkg-record.sh. mtime is the signal —
# the LKG writer skips promotion if the heartbeat is older than ~3 minutes,
# proving litclock.service has rendered a frame this cycle. Living in
# /run/litclock keeps it off the SD card (zero wear from ~525k writes/yr)
# and makes it reboot-fresh, which is exactly what the LKG gate wants.
HEARTBEAT_FILE = os.environ.get("LITCLOCK_HEARTBEAT_FILE", "/run/litclock/heartbeat")


def _write_heartbeat():
    """Touch the LKG heartbeat. Best-effort: tmpfs may be missing on a dev
    box and the LKG writer is observability-only, so we never fail the
    render over it."""
    try:
        with open(HEARTBEAT_FILE, "w"):
            pass
    except OSError as e:
        logging.debug(f"heartbeat touch failed (non-fatal): {e}")


def _stamp_update_failed_glyph(image, draw):
    """Paint a subtle 12x12 "!" glyph at the top-LEFT corner when the last
    auto-update failed. Cleared on next successful update.sh run.

    The glyph is intentionally small and corner-placed — non-technical users
    shouldn't notice it; technically-curious users will see something has
    changed and know to ``journalctl -u litclock-update.service``. Best-effort
    read (os.path.exists); no locking. Marker is transient; a stale read in
    either direction is harmless for the next render cycle.

    Placement note: pre-litclock-dev#245-M2 the glyph was top-right at x=784. M2 moves
    the QR to the top-right (litclock-dev#245 A6), so the glyph relocates to x=4 (the
    weather block's vertical divider sits at x=225, leaving the 0..16
    margin clear for the glyph).
    """
    try:
        if not os.path.exists(UPDATE_FAILED_MARKER):
            return
    except OSError:
        return
    # 12x12 at the top-left. Tiny filled rectangles as the glyph body.
    # Pixel offsets unchanged from the legacy top-right placement so the
    # visual silhouette is identical.
    x0 = 4
    y0 = 4
    # "!" — vertical bar + dot
    draw.rectangle([(x0 + 5, y0 + 1), (x0 + 6, y0 + 7)], fill=0)
    draw.rectangle([(x0 + 5, y0 + 9), (x0 + 6, y0 + 10)], fill=0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LitClock — render the current-minute quote to the e-Paper display.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Render the image to an in-memory buffer and exit without touching hardware. "
            "Used by scripts/update.sh as a post-update smoke test — failure here reverts "
            "the update so the clock never gets bricked by a bad release."
        ),
    )
    parser.add_argument(
        "--require-runtime-render",
        action="store_true",
        help=(
            "With --dry-run: exit 3 unless the frame came from the on-device text renderer "
            "(render_mode == 'runtime'). The litclock-dev#871 Stage A self-test — a dry-run that "
            "silently fell back to a pre-rendered PNG must not count as 'this device renders text'."
        ),
    )
    parser.add_argument(
        "--sample-quote",
        action="store_true",
        help=(
            "With --dry-run: render the first minute from now that the device language's corpus has a "
            "non-mature quote for, instead of the current minute, so the check is about rendering and "
            f"never about coverage. Exits {EXIT_NO_SAMPLE} when the corpus has nothing to sample "
            "(litclock-dev#532 R10, the weekly self-test)."
        ),
    )
    args = parser.parse_args()
    if args.require_runtime_render and not args.dry_run:
        parser.error("--require-runtime-render only means something with --dry-run")
    if args.sample_quote and not args.dry_run:
        parser.error("--sample-quote only means something with --dry-run")

    if args.dry_run:
        # Smoke test path: exercise image composition end-to-end (fonts, corpus,
        # weather fallbacks) without importing display_driver (which binds GPIO
        # and opens /dev/spidev* on import). Any exception becomes a non-zero
        # exit with a traceback — update.sh reads that and reverts. main()
        # returns (image, quote_meta, now, fallback_reason) but doesn't publish the status
        # file (that's __main__'s post-hardware job), so the smoke render
        # never touches /run/litclock/current-quote.json — OV3 lockstep
        # preserved (codex /review M2 catch).
        sample = None
        if args.sample_quote:
            sample = _sample_minute(_render_target())
            if sample is None:
                print("dry-run: no quote to sample — the device language's corpus has none", flush=True)
                sys.exit(EXIT_NO_SAMPLE)
            print(f"dry-run: sampling {sample.strftime('%H:%M')}", flush=True)
        try:
            # The self-test (--require-runtime-render / --sample-quote)
            # certifies the QUOTE renderer, so it pins quotes mode
            # (litclock-dev#533) — a fortune-mode device must still be able to
            # earn the pass Stage B gates on. The plain smoke gate honours
            # LITCLOCK_CONTENT_MODE, and so tests the tier the device actually paints.
            if args.require_runtime_render or args.sample_quote:
                os.environ["LITCLOCK_CONTENT_MODE"] = "quotes"
            elif _content_mode() == "fortune":
                # The quote tiers are a fortune-mode device's fallback, so the
                # smoke gate still runs them there (review). Exactly the bar a
                # quotes-mode device's gate holds them to: an exception exits 1
                # and reverts; a degraded frame (time-only, a failure reason)
                # is printed but passes, as it does in quotes mode — the
                # weekly self-test, pinned to quotes, is what catches a text
                # renderer that degrades. Stricter here would revert releases
                # on fortune devices that every other device keeps (Codex).
                os.environ["LITCLOCK_CONTENT_MODE"] = "quotes"
                fallback = main()
                fallback_mode = fallback[1].get("render_mode") if fallback[1] else "time-only"
                print(f"dry-run: fortune-mode fallback tier render_mode={fallback_mode}", flush=True)
                os.environ["LITCLOCK_CONTENT_MODE"] = "fortune"
            result = main() if sample is None else main(at=sample)
        except Exception:
            logging.exception("dry-run render failed")
            sys.exit(1)
        if result is None:
            logging.error("dry-run: main() returned None")
            sys.exit(1)
        image = result[0]
        if image is None:
            logging.error("dry-run: main() returned None image")
            sys.exit(1)
        # litclock-dev#871 Stage A: say WHICH tier produced the frame, and let
        # the self-test insist on the text renderer. main() degrades on every
        # runtime failure (marker missing, freetype unusable, digest stale,
        # corpus gap, render error) — to the PNG tier for English, to the
        # plain-time frame for every other language — and returns a perfectly
        # good image, so exit 0 alone cannot tell "this device renders text"
        # from "this device fell back", which is the whole question the
        # self-test asks. `time-only` is the plain-time frame (quote_meta
        # None), and the line then says whether it was a gap or a failure
        # (litclock-dev#532 D8): only a failure is evidence against the device.
        render_mode = result[1].get("render_mode") if result[1] else "time-only"
        reason = result[3] if len(result) > 3 and result[1] is None else None
        # stdout, not the log: setup_logging() defaults to WARNING unless
        # LOG_LEVEL is set, so an INFO line is normally invisible — and the
        # self-test's verdict is the one line of a dry-run anyone reads
        # (`[selftest] dry-run: render_mode=runtime` in the update journal).
        suffix = f" ({reason})" if reason else ""
        print(
            f"dry-run: rendered {image.size[0]}x{image.size[1]} image, render_mode={render_mode}{suffix}", flush=True
        )
        if args.require_runtime_render and render_mode != "runtime":
            logging.error(
                "dry-run: --require-runtime-render but the frame came from render_mode=%s — "
                "the text renderer did not produce it (see the warning above for why)",
                render_mode,
            )
            sys.exit(EXIT_NOT_RUNTIME)
        sys.exit(0)

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    epd = None

    # These run BEFORE the paint's try/except, and the first two sit downstream
    # of `import lgpio` — the display_driver import is what constructs the
    # gpiozero objects and spawns Thread-1. So an exception here used to
    # propagate out of __main__ entirely, skipping the os._exit below and
    # running the very finalization this fix exists to avoid. Worse, the most
    # likely failure here is a GPIO-busy import error from the previous
    # minute's process, i.e. litclock-dev#531 firing on precisely the path the fix
    # targets.
    #
    # Exits 1, matching the previous behaviour of letting the exception escape.
    # BaseException, not Exception: a SystemExit or KeyboardInterrupt raised in
    # here must not slip past to normal finalization either.
    #
    # The DISPLAY_CLEAR_HOUR parse is here too (litclock-dev#838). It never
    # raises by design, and it is read BEFORE the panel is touched so that if
    # it ever did, the run would exit 1 with a traceback in the journal rather
    # than freeze the panel from inside the paint try with the process
    # reporting success — which is what the bare int() it replaces did.
    try:
        # Hardware import is lazy — deferred until we actually need to talk to the display.
        from display_driver import epd7in5  # noqa: E402

        image, quote_meta, now, fallback_reason = main()
        display_clear_hour = _display_clear_hour()
    except BaseException:
        # traceback.print_exc() as well as logging: logging.exception is
        # level-gated, and this guard exists to instrument the one failure most
        # likely to BE litclock-dev#531 (a GPIO-busy import from the previous minute).
        # Under LOG_LEVEL=CRITICAL the process would otherwise exit 1 with
        # nothing in journald at all. stderr goes to the journal for this unit.
        #
        # litclock-dev#813 — the diagnostics are wrapped and the exit is not.
        # These three statements used to sit bare, ahead of `os._exit(1)`, and
        # `traceback.print_exc()` RAISES on a broken stderr (BrokenPipeError).
        # That escaped this handler, skipped the exit, and ran the very
        # interpreter finalization litclock-dev#531 exists to avoid — with Thread-1
        # alive. Demonstrated end to end. `logging.exception` would have
        # survived (logging swallows OSError); `traceback.print_exc()` does not,
        # and `logging.shutdown()` re-raises non-OSError/ValueError while
        # `raiseExceptions` is True, so it is no safer.
        #
        # The ordering is the contract: everything that can fail is INSIDE the
        # guard, and `os._exit(1)` is outside it and last. Diagnostics are
        # best-effort; terminating without finalization is not.
        try:
            traceback.print_exc()
            logging.exception("Failed before the display could be initialised")
            logging.shutdown()
        except BaseException:
            pass
        os._exit(1)

    try:
        logging.info("Initializing e-Paper display...")
        epd = epd7in5.EPD()
        _epd = epd  # Set global for signal handler
        logging.info("EPD object created.")

        # litclock-dev#762 Trap 2: this MUST read the offset target, not
        # datetime.now(). With the timer at :56 a live now().minute is never 0,
        # so the hourly full clear would simply stop firing and ghosting would
        # accumulate over weeks with nothing in the logs. At 01:59:56 the target
        # is 02:00:00 and the clear correctly fires on the 02:00 render.
        clear = now.minute == 0 and now.hour == display_clear_hour

        # litclock-dev#963: read the saved frame only NOW, with the pins held
        # (EPD() above claims them), so no other writer can paint between this
        # read and the partial. Any panel write elsewhere has already removed
        # the file (display_driver), and the init below removes it again until
        # panel_frame.save() records this frame.
        previous = panel_frame.load(DISPLAY_SIZE) if _partial_refresh_enabled() and not clear else None
        refresh = "partial" if previous is not None and _masthead_only_change(previous, image) else "full"

        if refresh == "partial":
            epd.init_part()
            logging.info("EPD initialized (partial).")
            _wait_for_partial()
            epd.display_Partial(epd.getbuffer(image), 0, 0, *DISPLAY_SIZE)
        else:
            epd.init()
            logging.info("EPD initialized.")
            if clear:
                epd.Clear()
            epd.display(epd.getbuffer(image))
        panel_frame.save(image)
        # Hardware confirmed the new frame is on the panel — only NOW is
        # it correct to publish the OV3 status file. If epd.display() above
        # raised, the status file stays stale and the PWA renders the
        # stale-quote banner — which is the right signal (codex /review
        # caught the M2 draft publishing pre-hardware).
        _write_status_file(quote_meta, now, fallback_reason, refresh=refresh)
        epd.sleep()
        _write_heartbeat()

        logging.info("Display updated and put to sleep.")

    except FileNotFoundError as e:
        logging.error(f"FileNotFoundError: {e}")
        logging.error("The e-Paper device is not connected. Please connect the device and try again.")

    except OSError as e:
        logging.error(f"IOError: {e}")

    except KeyboardInterrupt:
        logging.warning("Program interrupted.")
        if epd is not None:
            try:
                epd.sleep()
                logging.info("Display put to sleep.")
            except Exception as e:
                logging.error(f"Failed to sleep display: {e}")

    except Exception as e:
        logging.error(f"Unexpected error: {e}")
        if epd is not None:
            try:
                epd.sleep()
            except Exception:
                pass

    # litclock-dev#814 — BaseException, matching the pre-paint guard above,
    # whose comment already argues the rule this block did not follow: "a
    # SystemExit or KeyboardInterrupt raised in here must not slip past to
    # normal finalization either." Without this arm a SystemExit — or any
    # non-Exception BaseException — raised anywhere in the paint block escapes
    # to normal finalization with Thread-1 alive.
    #
    # No reachable trigger was found (the vendored waveshare driver contains no
    # sys.exit, and _write_status_file / _write_heartbeat raise nothing), so
    # this closes a gap rather than a live bug. It is worth closing because the
    # reason it was safe is a property of vendored third-party code that this
    # repo does not control and no test asserts.
    #
    # KeyboardInterrupt keeps its own arm above and never reaches here.
    except BaseException as e:
        logging.error(f"Unexpected non-Exception error: {e!r}")
        if epd is not None:
            try:
                epd.sleep()
            except BaseException:
                pass

    # litclock-dev#531 — exit WITHOUT running interpreter finalization.
    #
    # NOTE (litclock-dev#816): "litclock-dev#531" here is SHORTHAND for the lgpio
    # teardown crash, not a real issue reference — litclock-dev#531 is the
    # runtime-render epic and no issue was ever opened for this bug. The
    # behaviour change is written up in CHANGELOG.md; litclock-dev#815 covers
    # the splash renderers, which ran the same race until it landed.
    #
    # `import lgpio` (pulled in by gpiozero's LGPIOFactory, which the display
    # driver uses) unconditionally spawns a daemon thread, `_callback_thread`.
    # Verified on hardware: it exists before any chip is opened or pin claimed,
    # it is named `Thread-1`, and NOTHING removes it — not closing the BUSY
    # Button, not closing the pin factory. Its run loop parks in a blocking
    # `self._file.read()` on the notification pipe, and its `stop()` only sets
    # a flag, never interrupting that read.
    #
    # So it is a daemon thread sitting inside a syscall when Py_FinalizeEx
    # begins. Roughly once every 4300 runs it unwinds against half-cleared
    # module globals and raises, which journald records as
    # `Exception in thread Thread-1:` plus a single _bootstrap_inner frame and
    # nothing more. That is litclock-dev#531. It is an upstream property of the lgpio
    # binding, not something this code can clean up.
    #
    # Since the thread cannot be prevented or joined, the fix is to never run
    # finalization: os._exit terminates immediately, skipping atexit handlers
    # and daemon-thread teardown. Safe here specifically because this is a
    # Type=oneshot that is about to exit anyway, and everything durable is
    # already committed above — the panel (epd.display), the OV3 status file,
    # epd.sleep(), and the heartbeat. logging.shutdown() flushes the handlers
    # that os._exit would otherwise skip.
    #
    # Hardware-verified 2026-07-30: three consecutive runs under this exit all
    # reported success with the heartbeat and status file advancing, and zero
    # `GPIO busy` / lgpio errors — the kernel releases the chardev handles on
    # process death regardless of whether Python ran its finalizers, so the
    # next minute's run re-acquires cleanly.
    #
    # Exit code stays 0 in every branch above, matching the prior behaviour of
    # falling off the end of this block.
    # litclock-dev#813 applied consistently (/review): `logging.shutdown()`
    # re-raises non-OSError/ValueError while `raiseExceptions` is True, so an
    # unguarded call here can cost the exit and drop the process into the very
    # finalization this line exists to skip. Same rule as the pre-paint handler:
    # everything that can fail is INSIDE the guard, os._exit is outside and last.
    try:
        logging.shutdown()
    except BaseException:
        pass
    os._exit(0)
