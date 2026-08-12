#!/usr/bin/env python3
"""Render the SHIPPED e-ink top-strip layout for hardware (re)validation.

Written pre-M2 to validate the PLAN A6 proposal; the composite has since
shipped in `src/literary_clock.py`, and this preview now mirrors it by
importing the production geometry (masthead metrics, QR constants, divider,
notch — litclock-dev#538/litclock-dev#605 killed the drifting-copy class twice). The tool never
modifies runtime code; it exists so a layout change can be phone-scanned
on a printout BEFORE it reaches a panel:

    +-----------------------------------------------------------+ y=0
    | [!]    [WEATHER]  Mon, September 04              [QR]     |
    +-----------------------------------------------------------+ divider
    | (quote area placeholder — y=80..480)                      |
    +-----------------------------------------------------------+ y=480

Geometry sourced from literary_clock: QR version/box/border/position,
quiet zone + notch expression, divider row/width, masthead lockup. Only
preview-specific values (worst-case ink row, glyph preview position) are
defined here.

Output: /tmp/litclock-qr-layout-preview.png (800x480, mode "1" to match
the e-ink). Display at native resolution on a 7.5" screen (or print at
~9.7" wide) and scan from ~30 cm with both iPhone and Android Camera —
both must decode the QR URL printed by this script.

Usage:
    python3 tools/control-pwa/validate_qr_layout.py [--out PATH]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import qrcode
import qrcode.constants
from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parents[2]
# litclock-dev#538: masthead geometry comes from the PRODUCTION module — this tool
# carried a drifting third copy of the strip layout twice (pre-litclock-dev#530 QR,
# pre-litclock-dev#538 masthead); importing kills that failure class.
sys.path.insert(0, str(REPO_ROOT / "src"))
import literary_clock as _lc  # noqa: E402

DEFAULT_OUT = Path("/tmp/litclock-qr-layout-preview.png")
DISPLAY_SIZE = _lc.DISPLAY_SIZE

# QR + divider geometry: production constants, aliased (litclock-dev#605
# item 15 — these were re-typed here while the litclock-dev#538 comment above claimed
# the drifting-copy class was dead; QR_URL is the mDNS fallback the clock
# paints when no LAN IP resolves, port-less per litclock-dev#343).
QR_URL = _lc.QR_URL
QR_VERSION = _lc.QR_VERSION
QR_BOX_SIZE = _lc.QR_BOX_SIZE
QR_BORDER = _lc.QR_BORDER
QR_POSITION = _lc.QR_POSITION
QR_EXPECTED_SIZE = (_lc.QR_SIZE, _lc.QR_SIZE)
# ISO 18004 quiet zone (4 modules): the composite white-outs the strip's
# top-right corner (notching the divider under the QR) instead of baking a
# border into the QR image; the notch reaches QR bottom + quiet zone so the
# bottom quiet zone is structural.
QR_QUIET_ZONE = _lc.QR_QUIET_ZONE

# Preview-only: where this tool paints the 12x12 "!" — matches the x0/y0
# literary_clock._stamp_update_failed_glyph hardcodes (relocated x=784 ->
# x=4 in litclock-dev#245 M2; no production constant to alias).
GLYPH_POSITION = (4, 4)

# Existing top-strip features that the QR must not collide with —
# sourced from production (litclock-dev#538 V8-G2 lockup geometry).
_MH = _lc._masthead_metrics()
WEATHER_ICON_POSITION = (_MH["icon_x"], _MH["icon_y"])
WEATHER_ICON_SIZE = (_lc.ICON_SIZE, _lc.ICON_SIZE)
DATE_TEXT_POSITION = (_lc.DATE_X, _MH["date_y"])
TOP_STRIP_DIVIDER_Y = _lc.DIVIDER_Y
DIVIDER_WIDTH = _lc.DIVIDER_WIDTH
QR_NOTCH_BOTTOM = _lc.QR_NOTCH_BOTTOM
# Worst-case corpus ink starts one row below the notch (display y=87,
# measured across all 4,809 quote PNGs — litclock-dev#530). The preview
# paints a solid bar there so the phone-scan validation runs against the
# TIGHTEST legal frame, not an optimistic all-white bottom.
WORST_CASE_INK_Y = 87

# Project assets (best-effort — script still runs if absent).
PROJECT_FONT = REPO_ROOT / "fonts" / "Literata72pt-Regular.ttf"
SUN_ICON_XBM = REPO_ROOT / "icons" / "sun.xbm"


def build_qr() -> Image.Image:
    """Build the QR exactly as the clock composites it (constants aliased
    from literary_clock above)."""
    qr = qrcode.QRCode(
        version=QR_VERSION,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=QR_BOX_SIZE,
        border=QR_BORDER,
    )
    qr.add_data(QR_URL)
    # fit=False because the shipped layout locks the version. If `QR_URL` ever grows past
    # what fits in version 2/EC-M, qrcode raises — that's the right signal,
    # not silent re-fitting to a larger QR that breaks the 75x75 layout.
    qr.make(fit=False)
    img = qr.make_image(fill_color="black", back_color="white").convert("1")
    if img.size != QR_EXPECTED_SIZE:
        raise RuntimeError(
            f"QR output size {img.size} != expected {QR_EXPECTED_SIZE}; "
            f"the shipped layout assumes {QR_EXPECTED_SIZE[0]}x{QR_EXPECTED_SIZE[1]}. "
            "Inspect qrcode lib version."
        )
    return img


def stamp_update_failed_glyph(draw: ImageDraw.ImageDraw, position: tuple[int, int]) -> None:
    """Replicates src/literary_clock.py:_stamp_update_failed_glyph
    pixel-for-pixel, but at an arbitrary `position` and unconditionally
    (production gates on the update-failed marker file). Used here to
    confirm the x=4,y=4 placement is visible and does not collide with the
    weather icon."""
    x0, y0 = position
    # "!" — vertical bar + dot. Same offsets as the runtime function.
    draw.rectangle([(x0 + 5, y0 + 1), (x0 + 6, y0 + 7)], fill=0)
    draw.rectangle([(x0 + 5, y0 + 9), (x0 + 6, y0 + 10)], fill=0)


def render_preview() -> Image.Image:
    image = Image.new(mode="1", size=DISPLAY_SIZE, color=255)
    draw = ImageDraw.Draw(image)

    # Production masthead (litclock-dev#538): date + weather lockup + rules drawn by
    # the same code the clock runs. Fixed strings keep the preview stable.
    icon_path = str(SUN_ICON_XBM) if SUN_ICON_XBM.exists() else None
    _lc._compose_masthead(image, draw, "Mon, September 04", "100°F", "82°F", icon_path)

    # The relocated update-failed glyph at x=4, y=4 (was x=784).
    stamp_update_failed_glyph(draw, GLYPH_POSITION)

    # Quote-area placeholder so the preview reads as a recognizable layout.
    draw.rectangle([(0, 80), (DISPLAY_SIZE[0] - 1, DISPLAY_SIZE[1] - 1)], outline=0, width=1)
    if PROJECT_FONT.exists():
        try:
            quote_font = ImageFont.truetype(str(PROJECT_FONT), 32)
            draw.text((40, 220), "(quote area — y=80..480, unchanged from current layout)", font=quote_font, fill=0)
        except OSError:
            pass

    # The QR at x=713, y=0 — same order as the runtime: quiet-zone white-out
    # (notches the divider under the QR, reaching QR bottom + 12px) then
    # paste. Single rectangle so preview and runtime geometry share one
    # expression (QR_NOTCH_BOTTOM).
    qr_image = build_qr()
    draw.rectangle(
        [(QR_POSITION[0] - QR_QUIET_ZONE, 0), (DISPLAY_SIZE[0] - 1, QR_NOTCH_BOTTOM)],
        fill=255,
    )
    image.paste(qr_image, QR_POSITION)

    # Adversarial bottom edge: solid ink bar at the worst-case corpus row so
    # the phone-scan check validates the TIGHTEST real frame (a solid bar is
    # harsher than the thin bracket glyphs that actually live there).
    draw.rectangle(
        [(QR_POSITION[0], WORST_CASE_INK_Y), (QR_POSITION[0] + QR_EXPECTED_SIZE[0] - 1, WORST_CASE_INK_Y + 2)],
        fill=0,
    )

    return image


def collision_report() -> list[str]:
    """Surface any rectangle overlap between the new top-strip features. Used
    as a sanity check before scanning. Returns a list of human-readable issues."""
    issues = []

    # New QR rectangle.
    qx, qy = QR_POSITION
    qw, qh = QR_EXPECTED_SIZE
    qr_rect = (qx, qy, qx + qw, qy + qh)

    # Relocated glyph 12x12.
    gx, gy = GLYPH_POSITION
    glyph_rect = (gx, gy, gx + 12, gy + 12)

    # Existing weather icon.
    wx, wy = WEATHER_ICON_POSITION
    ww, wh = WEATHER_ICON_SIZE
    weather_rect = (wx, wy, wx + ww, wy + wh)

    # Approx date-text bounding box. Longest real date string ("Wed,
    # September 30" at 48pt Literata) measures 433px from x=250 → ends at
    # x≈683; 440 keeps ~7px of conservatism without falsely colliding with
    # the quiet-zone erase rectangle that starts at x=701.
    date_rect = (DATE_TEXT_POSITION[0], DATE_TEXT_POSITION[1], DATE_TEXT_POSITION[0] + 440, DATE_TEXT_POSITION[1] + 56)

    # The quiet-zone erase rectangle is destructive at runtime — anything
    # inside it gets white-outed every minute tick. Model it so a future
    # layout/copy change (e.g., localized month names, litclock-dev#19) that drifts into
    # x>=701 is flagged here instead of showing up as a truncated date on
    # the e-ink.
    erase_rect = (QR_POSITION[0] - QR_QUIET_ZONE, 0, DISPLAY_SIZE[0], QR_NOTCH_BOTTOM + 1)

    pairs = [
        ("QR", qr_rect, "weather icon", weather_rect),
        ("QR", qr_rect, "date text", date_rect),
        ("QR", qr_rect, "update-failed glyph", glyph_rect),
        ("update-failed glyph", glyph_rect, "weather icon", weather_rect),
        ("update-failed glyph", glyph_rect, "date text", date_rect),
        ("quiet-zone erase rect", erase_rect, "date text", date_rect),
        ("quiet-zone erase rect", erase_rect, "weather icon", weather_rect),
        ("quiet-zone erase rect", erase_rect, "update-failed glyph", glyph_rect),
    ]
    for a_name, a, b_name, b in pairs:
        if a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]:
            issues.append(f"COLLISION: {a_name} {a} overlaps {b_name} {b}")
    if QR_POSITION[1] + QR_EXPECTED_SIZE[1] > TOP_STRIP_DIVIDER_Y:
        issues.append(
            f"QR bottom ({QR_POSITION[1] + QR_EXPECTED_SIZE[1]}) extends past top-strip divider y={TOP_STRIP_DIVIDER_Y}"
        )
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"Where to write the preview PNG (default: {DEFAULT_OUT})",
    )
    args = parser.parse_args()

    issues = collision_report()
    if issues:
        print("LAYOUT ISSUES:")
        for issue in issues:
            print(f"  - {issue}")
        print()
    else:
        print("Layout collision check: no overlaps among QR / glyph / weather / date.")
        print()

    image = render_preview()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    image.save(args.out, format="PNG")
    print(f"Preview written: {args.out}")
    print(f"  size: {image.size}, mode: {image.mode}")
    print(f"  source: production layout constants (QR @ {QR_POSITION}, glyph @ {GLYPH_POSITION})")
    print()
    print("HARDWARE VALIDATION (required before any layout change ships):")
    print("  1. Display the preview PNG at 800x480 native resolution OR print at")
    print('     ~9.7 inches wide (matches the 7.5" Waveshare panel diagonal).')
    print("  2. Scan from ~30 cm with iPhone Camera. URL must decode exactly:")
    print(f"       {QR_URL}")
    print("  3. Repeat with Android Camera. Both OSes must decode.")
    print("  4. Visually confirm: QR does not touch the date text, the update-failed")
    print("     glyph at x=4 is visible above-left of the weather block, the")
    print(f"     horizontal divider at y={TOP_STRIP_DIVIDER_Y} is intact.")
    print("  5. Record evidence in docs/control-pwa-m0-validation.md.")

    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
