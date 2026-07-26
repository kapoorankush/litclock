import argparse
import json
import logging
import os
import signal
import socket
import sys
import tempfile
import time as _time
from datetime import datetime
from glob import glob
from pathlib import Path
from random import randrange

from PIL import Image, ImageDraw, ImageFont, ImageOps

import quote_corpus
from control_url import control_base_url  # QR target — single source of truth (#343)
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
    sys.exit(1)


# Configure logging
setup_logging()

# Constants
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT_PATH = os.path.join(PROJECT_ROOT, "fonts", "Literata72pt-Regular.ttf")
DISPLAY_SIZE = (800, 480)

# Status file for the Control PWA (#245 M2 / OV3). literary_clock.py writes
# this after each render so /api/status can mirror exactly what's on the
# e-ink. Lives under /run/litclock (tmpfs, pi:pi-owned via the #241 tmpfiles.d
# entry) — /var/run is root-owned and would Permission-denied the per-minute
# write under `User=pi`. Codex /review caught this on M2 (PR #245). tmpfs
# means zero SD wear from per-minute writes; override via env so dev boxes
# / CI can point it elsewhere.
STATUS_FILE = os.environ.get("LITCLOCK_STATUS_FILE", "/run/litclock/current-quote.json")

# Persistent QR code on the e-ink top strip (#245 A6). 75x75 px at x=713,y=0,
# encodes the PWA URL so non-tech users can scan-to-open instead of typing.
# Geometry locked in M0 (validated via tools/control-pwa/validate_qr_layout.py);
# nudged (716,2)→(713,0) for the quiet zone (see QR_QUIET_ZONE).
#
# URL is plain HTTP — locked decision #257 dropped self-signed TLS for
# control_server (iOS PWAs reject self-signed every launch + suppress AtHS icon
# + suppress Dynamic Type). #343 moved control_server to port 80 so the URL a
# user scans or types carries NO port (bare `http://<ip>` / `http://litclock.local`)
# — the port is built by control_url.control_base_url, which omits `:80`. The
# clock and control_server share that helper so the QR target and the actual
# listen port can never drift.
#
# QR_URL is the FALLBACK — used when _resolve_lan_ip() can't determine the
# Pi's LAN IP (no network, no default route). The runtime path prefers the IP
# because mDNS (litclock.local) is unreliable on Android Chrome and many
# home/guest WiFi networks (issue #306). Resolved IP changes propagate within
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
# That yields 4 modules right (x=788..799), 4+ modules left (date text ends
# ≤ x=682) and 4 modules below — the notch extends past the divider to
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

# ---- Masthead geometry (dev#538 V8-G2, owner-approved on hardware) ----
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
# (subtracting it shifted the approved layout 1px left — dev#538 pixel-diff).
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

_masthead_cache: dict = {}


def _reset_masthead_cache() -> None:
    """Test hook — the cache assumes FONT_PATH and the size constants are
    immutable for the process lifetime (true in production: fresh process
    per minute). Tests that monkeypatch them must clear it."""
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
    """Bound the weather temp domain (dev#538 review): finite numeric within
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


def _compose_masthead(image, draw, date_text: str, temp_high: str | None, temp_low: str | None, icon_path: str | None):
    """Draw the top strip: date, weather lockup (when temps present), rules.
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
                # is an OSError subclass) — dev#538 review: the old narrow
                # except let a corrupt icon kill the whole render.
                logging.error(f"Weather icon unusable ({icon_path}): {e}; leaving icon area blank")
        for line_text, line_baseline in (
            (temp_high, mh["baseline"] - TEMP_LINE_PITCH),
            (temp_low, mh["baseline"]),
        ):
            w = draw.textbbox((0, 0), line_text, font=mh["temp_font"])[2]
            draw.text((mh["slot_right"] - w, line_baseline - mh["temp_asc"]), line_text, font=mh["temp_font"], fill=0)
    draw.text((DATE_X, mh["date_y"]), date_text, font=mh["date_font"], fill=0)
    draw.line([(0, DIVIDER_Y), (DISPLAY_SIZE[0], DIVIDER_Y)], fill=0, width=DIVIDER_WIDTH)
    if have_weather:
        draw.line([(WEATHER_DIVIDER_X, 0), (WEATHER_DIVIDER_X, DIVIDER_Y)], fill=0, width=DIVIDER_WIDTH)


# Runtime quote rendering (dev#531 Stage 2). When LITCLOCK_RUNTIME_RENDER is
# true, the clock renders the quote from the corpus CSV via
# src/quote_renderer.py instead of opening a pre-rendered PNG. Fallback
# order on ANY failure: pre-rendered PNG for this minute (right time, right
# content) -> the existing 144pt time draw (right time, no quote). There is
# deliberately NO "last-good frame" tier: a stale frame shows the WRONG
# minute's timestring, and on a clock a wrong time is worse than a plain
# one. The rendered frame is persisted to tmpfs (best-effort, atomic) so
# support bundles and the status file can reference what was painted.
RUNTIME_RENDER_DIR = os.environ.get("LITCLOCK_RUNTIME_RENDER_DIR", "/run/litclock")
CORPUS_CSV = os.path.join(PROJECT_ROOT, "image-gen", "litclock_annotated.csv")
# The flag alone is not enough: this device's freetype-py wheel must first
# be PROVEN to reproduce GD metrics (tools/validate_measurement.py check
# --stamp writes this marker only on a 100% pass). Guards against enabling
# runtime rendering in an unvalidated environment, where subtly-wrong
# hinted metrics would render ugly-but-sane frames no per-minute sanity
# check can catch (dev#537 review). Delete the marker after any freetype-py
# or font change and re-run the check.
RUNTIME_VALIDATED_MARKER = os.environ.get(
    "LITCLOCK_RUNTIME_VALIDATED_MARKER", os.path.join(PROJECT_ROOT, ".runtime-render-validated")
)

# display_driver binds to hardware GPIO/SPI on import. Keep it lazy so
# --dry-run (smoke test) can render an image without touching /dev/spidev*.
from weather_providers import open_meteo, openweathermap  # noqa: E402


def main():
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

    Returns: (image, quote_meta, now) — quote_meta is None when no quote
    PNG matched the current minute (caller already drew time-as-text)."""
    logging.info("Starting main function")

    openweathermap_apikey = os.getenv("OPENWEATHERMAP_APIKEY")
    location_lat = os.getenv("WEATHER_LATITUDE")
    location_long = os.getenv("WEATHER_LONGITUDE")
    units = os.getenv("WEATHER_UNITS", "imperial")
    allow_nsfw = os.getenv("ALLOW_NSFW_QUOTES", "false").lower() == "true"
    # WEATHER_ENABLED master toggle (Control PWA M3, #245). Default true
    # to preserve pre-M3 behavior on Pis that don't have the key yet —
    # update.sh's env.sh.sample merge will add it on the next update.
    weather_enabled = os.getenv("WEATHER_ENABLED", "true").lower() == "true"

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
            # garbage into the masthead (dev#538 review).
            logging.warning(
                f"weather temps unusable (max={weather.get('temperatureMax')!r} "
                f"min={weather.get('temperatureMin')!r}); suppressing weather"
            )
            weather = None
            temp_high = temp_low = None
        else:
            icon = weather.get("icon")
            # icon may be absent/garbage without costing the temps —
            # _compose_masthead handles icon_path=None (dev#541 review)
            icon_path = os.path.join(PROJECT_ROOT, "icons", f"{icon}.xbm") if isinstance(icon, str) and icon else None
            logging.debug(f"Weather: {temp_high} / {temp_low}, icon: {icon_path}")

    now = datetime.now()
    quote_meta = None
    if _runtime_render_enabled():
        quote_meta = get_current_quote_runtime(now=now, allow_nsfw=allow_nsfw)
    if quote_meta is None:
        quote_meta = get_current_quote(now=now, allow_nsfw=allow_nsfw)

    draw = ImageDraw.Draw(image)

    if quote_meta is None:
        # No quote image for this minute — fall back to drawing the time
        # in 144pt Literata. Existing behavior since v0.1; preserved for
        # corpus gaps (e.g., minutes without a literary match). Also the
        # final tier of the runtime-render fallback chain: right time, no
        # quote — never a stale quote.
        time_font = ImageFont.truetype(FONT_PATH, 144)
        draw.text((220, 150), now.strftime("%H:%M"), font=time_font, fill=0)
        logging.info("Time drawn on image")
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
            # time draw, not kill the whole per-minute render (dev#537
            # review) — this tier is also the runtime-render fallback, so
            # an exception here would void the "never crash-loop" contract.
            logging.error(f"quote PNG unusable ({quote_meta['image_path']}): {e}; drawing time")
            quote_meta = None
            time_font = ImageFont.truetype(FONT_PATH, 144)
            draw.text((220, 150), now.strftime("%H:%M"), font=time_font, fill=0)

    _compose_masthead(image, draw, now.strftime("%a, %B %d"), temp_high, temp_low, icon_path)

    _stamp_update_failed_glyph(image, draw)
    _composite_settings_qr(image)

    logging.info("Image drawing completed")
    # Status-file publication is __main__'s job, after the hardware
    # confirms the new frame reached the panel (codex /review M2 catch).
    return image, quote_meta, now


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
    # The marker records which FreeType it validated. A freetype-py bump
    # (OTA venv rebuild) must invalidate it — hinted metrics are version-
    # sensitive and a stale marker would keep the flag honored in a
    # now-unproven environment (dev#537 review, finding 2).
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
    return True


def _runtime_frame_ok(frame) -> str | None:
    """Sanity-check a rendered mode-"L" quote frame BEFORE it may replace a
    pre-rendered PNG on the panel. Returns a rejection reason or None.

    Catches the "rendered but garbage" class that raises nothing: wrong
    canvas, an all-white frame, or ink inside the settings-QR quiet-zone
    notch (which _composite_settings_qr would silently erase — #530).
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

    Ordering note (dev#537 review, finding 7): this runs at selection
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
) -> dict | None:
    """Render the current-minute quote from the corpus (dev#531 Stage 2).

    Returns the same metadata shape as get_current_quote() plus an
    ``image`` key holding the ready-to-paste mode-"1" frame (converted
    with dither=NONE — the renderer emits grey AA fringes, and default
    Floyd-Steinberg would speckle them into black dots). Returns ``None``
    on ANY failure — missing freetype-py, corpus read error, RenderError,
    sanity rejection — and the caller falls back to the pre-rendered PNG
    path, then the plain time draw. One bad row must degrade one minute,
    never crash-loop the timer."""
    now = now or datetime.now()
    try:
        import quote_renderer
    except ImportError as e:
        logging.warning(f"runtime render enabled but renderer unavailable ({e}); using images")
        return None
    try:
        rows = quote_renderer.rows_for_time(CORPUS_CSV, now.strftime("%H%M"))
    except Exception as e:
        logging.error(f"runtime render: corpus read failed: {e}")
        return None
    if not allow_nsfw:
        rows = [r for r in rows if not r.is_nsfw]
    if not rows:
        return None  # corpus gap — identical outcome to the PNG-glob miss
    row = rows[randrange(len(rows))]
    # ONE guard around render+validate+convert+persist: the "returns None
    # on ANY failure" contract must hold for unexpected shapes too, not
    # just RenderError (dev#537 review — a sanity-check or convert crash
    # previously escaped and would have killed the whole render).
    try:
        _, credits_img, font_size, _ = quote_renderer.render_row(row)
        reason = _runtime_frame_ok(credits_img)
        if reason is not None:
            logging.error(f"runtime render rejected for {row.basename}: {reason}; falling back")
            return None
        frame = credits_img.convert("1", dither=Image.Dither.NONE)
    except Exception as e:
        logging.error(f"runtime render failed for {row.basename}: {e}; falling back")
        return None
    return {
        "quote": row.quote,
        "author": row.author,
        "title": row.title,
        "time": row.time,
        "image_path": _persist_runtime_frame(frame) or "",
        "picked_at": _time.time(),
        "image": frame,
        "font_size": font_size,
    }


def get_current_quote(
    now: datetime | None = None,
    allow_nsfw: bool = False,
) -> dict | None:
    """Return current-minute quote metadata, or ``None`` if no quote is
    available for this HH:MM (caller falls back to drawing the time in
    144pt Literata).

    Pure function (#245 A7) — same selection logic feeds both the e-ink
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
    meta = quote_corpus.lookup_by_filename(chosen) or {}
    return {
        "quote": meta.get("quote", ""),
        "author": meta.get("author", ""),
        "title": meta.get("title", ""),
        "time": meta.get("time", now.strftime("%H:%M")),
        "image_path": chosen,
        "picked_at": _time.time(),
    }


def _write_status_file(quote_meta: dict | None, now: datetime) -> None:
    """Atomically publish what's on the e-ink so /api/status can mirror it.

    Uses the same tempfile + os.replace pattern as src/config.py for
    crash-safety. Best-effort — a missing or unwritable target directory
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

    payload: dict = {
        "time": now.strftime("%H:%M"),
        "picked_at": _time.time(),
    }
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

    Issue #306: mDNS (`litclock.local`) is unreliable on Android Chrome and
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
    e-ink top strip (#245 A6). Encodes ``http://<ip>`` (port omitted at 80,
    #343) when the LAN IP resolves, else falls back to ``QR_URL`` (mDNS
    hostname). Geometry
    locked in M0; the runtime composite mirrors what
    tools/control-pwa/validate_qr_layout.py proves scans on a real phone
    at ~30cm. White-outs the strip corner first for the ISO 18004 quiet
    zone (see QR_QUIET_ZONE) — this notches the y=78 divider under the QR.

    Issue #306: prefer IP over mDNS because Android Chrome and many home
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

    Placement note: pre-#245-M2 the glyph was top-right at x=784. M2 moves
    the QR to the top-right (#245 A6), so the glyph relocates to x=4 (the
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
    args = parser.parse_args()

    if args.dry_run:
        # Smoke test path: exercise image composition end-to-end (fonts, corpus,
        # weather fallbacks) without importing display_driver (which binds GPIO
        # and opens /dev/spidev* on import). Any exception becomes a non-zero
        # exit with a traceback — update.sh reads that and reverts. main()
        # returns (image, quote_meta, now) but doesn't publish the status
        # file (that's __main__'s post-hardware job), so the smoke render
        # never touches /run/litclock/current-quote.json — OV3 lockstep
        # preserved (codex /review M2 catch).
        try:
            result = main()
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
        logging.info("dry-run OK: rendered %sx%s image", image.size[0], image.size[1])
        sys.exit(0)

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Hardware import is lazy — deferred until we actually need to talk to the display.
    from display_driver import epd7in5  # noqa: E402

    image, quote_meta, now = main()
    epd = None

    try:
        logging.info("Initializing e-Paper display...")
        epd = epd7in5.EPD()
        _epd = epd  # Set global for signal handler
        logging.info("EPD object created.")

        epd.init()
        logging.info("EPD initialized.")

        display_clear_hour = int(os.getenv("DISPLAY_CLEAR_HOUR", 2))
        if datetime.now().minute == 0 and datetime.now().hour == display_clear_hour:
            epd.Clear()

        epd.display(epd.getbuffer(image))
        # Hardware confirmed the new frame is on the panel — only NOW is
        # it correct to publish the OV3 status file. If epd.display() above
        # raised, the status file stays stale and the PWA renders the
        # stale-quote banner — which is the right signal (codex /review
        # caught the M2 draft publishing pre-hardware).
        _write_status_file(quote_meta, now)
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
