#!/usr/bin/env python3
"""Render the proposed e-ink top-strip layout (PLAN A6) for hardware validation.

The runtime composite ships in M2 (`src/literary_clock.py` and friends).
This script does NOT modify runtime code. It generates a stand-alone
800x480 preview PNG that mirrors the planned top strip:

    +-----------------------------------------------------------+ y=0
    | (4,4) [!]    [WEATHER 64x64]  Mon, April, 27       [QR]   |
    |              (20,5)           (250,10)             (713,0)|
    +-----------------------------------------------------------+ y=78  <-- divider
    |                                                           |
    | (quote area placeholder — y=80..480)                      |
    |                                                           |
    +-----------------------------------------------------------+ y=480

The QR encodes `https://litclock.local`. PLAN A6 spec:
  - qrcode version 2 (25 modules)
  - error correction level M
  - 3 px / module, 0 border  -> 75x75 px output
  - composited at x=713, y=0 (nudged from (716, 2) for the quiet zone)
  - 4-module (12px) ISO 18004 quiet zone carved from the surroundings:
    the composite white-outs the strip corner, notching the y=78 divider

The relocated update-failed glyph moves from x=784 to x=4 (top-left of weather
area), reusing the exact 12x12 "!" geometry from
src/literary_clock.py:_stamp_update_failed_glyph (lines 147-170).

Output: /tmp/litclock-qr-layout-preview.png (800x480, mode "1" greyscale to
match the e-ink). Open the PNG, display it at native resolution on a 7.5"
screen (or print at ~9.7" wide), and scan from ~30 cm with both iPhone
Camera and Android Camera. Both must decode `https://litclock.local`.

Usage:
    python3 tools/control-pwa/validate_qr_layout.py [--out PATH]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import qrcode
import qrcode.constants
from PIL import Image, ImageDraw, ImageFont, ImageOps

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = Path("/tmp/litclock-qr-layout-preview.png")
DISPLAY_SIZE = (800, 480)

# Locked from PLAN A6 + DESIGN.md. #343: port-less (control_server on 80), matching
# what literary_clock.py actually paints via control_url.control_base_url.
QR_URL = "http://litclock.local"
QR_VERSION = 2
QR_BOX_SIZE = 3
QR_BORDER = 0
QR_POSITION = (713, 0)
QR_EXPECTED_SIZE = (75, 75)
# ISO 18004 quiet zone (4 modules = 12px). Mirrors literary_clock.py:
# the composite white-outs the strip's top-right corner (notching the
# y=78 divider under the QR) instead of baking a border into the QR image.
QR_QUIET_ZONE = 4 * QR_BOX_SIZE

# Locked from literary_clock.py:166-170 (relocation: x=784 -> x=4).
GLYPH_POSITION = (4, 4)

# Existing top-strip features that the QR must not collide with.
WEATHER_ICON_POSITION = (20, 5)
WEATHER_ICON_SIZE = (64, 64)
DATE_TEXT_POSITION = (250, 10)
TOP_STRIP_DIVIDER_Y = 78

# Project assets (best-effort — script still runs if absent).
PROJECT_FONT = REPO_ROOT / "fonts" / "Literata72pt-Regular.ttf"
SUN_ICON_XBM = REPO_ROOT / "icons" / "sun.xbm"


def build_qr() -> Image.Image:
    """Build the planned 75x75 QR. Mirrors `src/eink_display.py` usage pattern
    but with the higher-density / better-EC settings PLAN A6 locked in."""
    qr = qrcode.QRCode(
        version=QR_VERSION,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=QR_BOX_SIZE,
        border=QR_BORDER,
    )
    qr.add_data(QR_URL)
    # fit=False because PLAN A6 locked the version. If `QR_URL` ever grows past
    # what fits in version 2/EC-M, qrcode raises — that's the right signal,
    # not silent re-fitting to a larger QR that breaks the 75x75 layout.
    qr.make(fit=False)
    img = qr.make_image(fill_color="black", back_color="white").convert("1")
    if img.size != QR_EXPECTED_SIZE:
        raise RuntimeError(
            f"QR output size {img.size} != expected {QR_EXPECTED_SIZE}; "
            "PLAN A6 layout assumes 75x75. Inspect qrcode lib version."
        )
    return img


def stamp_update_failed_glyph(draw: ImageDraw.ImageDraw, position: tuple[int, int]) -> None:
    """Replicates src/literary_clock.py:_stamp_update_failed_glyph (line 147-170)
    pixel-for-pixel, but at an arbitrary `position` instead of hard-coded
    top-right. Used here to confirm the relocated x=4,y=4 placement is visible
    and does not collide with the weather icon at x=20."""
    x0, y0 = position
    # "!" — vertical bar + dot. Same offsets as the runtime function.
    draw.rectangle([(x0 + 5, y0 + 1), (x0 + 6, y0 + 7)], fill=0)
    draw.rectangle([(x0 + 5, y0 + 9), (x0 + 6, y0 + 10)], fill=0)


def paste_weather_placeholder(image: Image.Image) -> bool:
    """Paste a real sun.xbm icon if available; otherwise an outlined 64x64 box.
    Returns True if the real asset was used."""
    if SUN_ICON_XBM.exists():
        try:
            with Image.open(SUN_ICON_XBM) as raw:
                icon = ImageOps.invert(raw.resize(WEATHER_ICON_SIZE).convert("L"))
            image.paste(icon, WEATHER_ICON_POSITION)
            return True
        except (OSError, ValueError) as exc:
            print(f"warn: could not load {SUN_ICON_XBM}: {exc}", file=sys.stderr)
    # Fallback placeholder: outlined 64x64 box.
    draw = ImageDraw.Draw(image)
    x, y = WEATHER_ICON_POSITION
    w, h = WEATHER_ICON_SIZE
    draw.rectangle([(x, y), (x + w - 1, y + h - 1)], outline=0, width=2)
    draw.text((x + 6, y + 24), "WEATHER", fill=0)
    return False


def draw_date_placeholder(draw: ImageDraw.ImageDraw) -> None:
    """Mirror `literary_clock.py:108-109` — date text at (250, 10), 48pt
    Literata. Uses fixed copy so the preview is reproducible."""
    text = "Mon, April, 27"
    if PROJECT_FONT.exists():
        try:
            font = ImageFont.truetype(str(PROJECT_FONT), 48)
        except OSError:
            font = ImageFont.load_default()
    else:
        font = ImageFont.load_default()
    draw.text(DATE_TEXT_POSITION, text, font=font, fill=0)


def render_preview() -> Image.Image:
    image = Image.new(mode="1", size=DISPLAY_SIZE, color=255)
    draw = ImageDraw.Draw(image)

    paste_weather_placeholder(image)
    draw_date_placeholder(draw)

    # The relocated update-failed glyph at x=4, y=4 (was x=784).
    stamp_update_failed_glyph(draw, GLYPH_POSITION)

    # Top-strip divider at y=78 — same as runtime literary_clock.py:115.
    draw.line([(0, TOP_STRIP_DIVIDER_Y), (DISPLAY_SIZE[0], TOP_STRIP_DIVIDER_Y)], fill=0, width=4)
    # Vertical divider after weather block — same as runtime literary_clock.py:117.
    draw.line([(225, 0), (225, TOP_STRIP_DIVIDER_Y)], fill=0, width=4)

    # Quote-area placeholder so the preview reads as a recognizable layout.
    draw.rectangle([(0, 80), (DISPLAY_SIZE[0] - 1, DISPLAY_SIZE[1] - 1)], outline=0, width=1)
    if PROJECT_FONT.exists():
        try:
            quote_font = ImageFont.truetype(str(PROJECT_FONT), 32)
            draw.text((40, 220), "(quote area — y=80..480, unchanged from current layout)", font=quote_font, fill=0)
        except OSError:
            pass

    # The QR at x=713, y=0 — same order as the runtime: quiet-zone white-out
    # (notches the divider under the QR) then paste.
    qr_image = build_qr()
    # Through row 80: PIL's width=4 line at y=78 paints rows 77..80 (mirrors
    # the runtime notch in literary_clock.py).
    draw.rectangle(
        [(QR_POSITION[0] - QR_QUIET_ZONE, 0), (DISPLAY_SIZE[0] - 1, 80)],
        fill=255,
    )
    # Emulate the quote images' blank 10px top margin (fitText margin=10 in
    # image-gen/quote_to_image.php) — in the runtime that margin is what
    # extends the bottom quiet zone below the notch. The placeholder outline
    # drawn above would otherwise fake a black edge the real layout lacks.
    draw.rectangle(
        [(QR_POSITION[0] - QR_QUIET_ZONE, 80), (DISPLAY_SIZE[0] - 1, 89)],
        fill=255,
    )
    image.paste(qr_image, QR_POSITION)

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

    # Approx date-text bounding box (48pt across ~14 chars). Conservative width.
    date_rect = (DATE_TEXT_POSITION[0], DATE_TEXT_POSITION[1], DATE_TEXT_POSITION[0] + 460, DATE_TEXT_POSITION[1] + 56)

    pairs = [
        ("QR", qr_rect, "weather icon", weather_rect),
        ("QR", qr_rect, "date text", date_rect),
        ("QR", qr_rect, "update-failed glyph", glyph_rect),
        ("update-failed glyph", glyph_rect, "weather icon", weather_rect),
        ("update-failed glyph", glyph_rect, "date text", date_rect),
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
    print(f"  source: PLAN A6 layout (QR @ {QR_POSITION}, glyph @ {GLYPH_POSITION})")
    print()
    print("HARDWARE VALIDATION (gates the merge per PLAN A6):")
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
