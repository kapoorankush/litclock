#!/usr/bin/env python3
"""Generate the LitClock Control PWA icon set + iOS splash matrix from logo.png.

Produces icon variants per DESIGN.md D5, into src/control_server/static/icons/:

- icon-192.png            192x192, transparent bg (PWA standard)
- icon-512.png            512x512, transparent bg (PWA standard)
- icon-maskable-512.png   512x512, logo at 80% scale on solid --bg #FBF6EC
                          (Android adaptive-icon safe zone; outer 10% on each
                          side is solid --bg so any shape mask still shows brand)
- icon-bg-baked-512.png   512x512, logo at 100% scale on solid --bg #FBF6EC
                          (iOS splash bg surrogate — splash background can use
                          --bg directly without a rendering hop)

And per M6 D4/D8 + DD3/DD6 the iOS apple-touch-startup-image splash matrix
into src/control_server/static/splash/ (`--splash` mode):

- 17 light-mode PNGs: solid --bg #FBF6EC canvas, logo at 50% of the canvas
  shorter side, vertically + horizontally centered.
- 17 dark-mode PNGs: solid --bg dark #14110D canvas, color-inverted logo
  pasted via the same 50%-of-shorter-side rule.

= 34 PNGs total. base.html.j2 ships 34 `<link rel="apple-touch-startup-image">`
tags with `(orientation: portrait) and (prefers-color-scheme: light|dark)`
media queries so iOS Safari picks the right splash before the first paint.

Idempotent: re-running with an unchanged logo.png produces byte-identical PNGs.
We strip optional metadata (no text chunks, no timestamps) and pin compression
parameters so the diff stays clean on rebuild.

Usage:
    python3 tools/control-pwa/generate_pwa_icons.py            # icons only
    python3 tools/control-pwa/generate_pwa_icons.py --splash   # +splash matrix
    python3 tools/control-pwa/generate_pwa_icons.py --check    # exit nonzero if
                                                                # output would change
"""

from __future__ import annotations

import argparse
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image

# DESIGN.md --bg (light mode): "Antique paper warm off-white"
BG_COLOR = (0xFB, 0xF6, 0xEC, 0xFF)
# DESIGN.md --bg (dark mode): deep ink (M6 DD6 dark splash canvas).
BG_COLOR_DARK = (0x14, 0x11, 0x0D, 0xFF)

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_LOGO = REPO_ROOT / "logo.png"
OUT_DIR = REPO_ROOT / "src" / "control_server" / "static" / "icons"
SPLASH_DIR = REPO_ROOT / "src" / "control_server" / "static" / "splash"

# iOS apple-touch-startup-image splash matrix (M6 D4/D8). Portrait sizes
# covering every iPhone + iPad shipped in the last 5 years. Each (W, H) pair
# is the device's physical pixel resolution — Safari matches against the
# `device-width × device-pixel-ratio` media query.
#
# Ordered roughly oldest → newest for the change diff to stay readable; the
# generator is order-independent at runtime (each entry stands alone).
SPLASH_SIZES: tuple[tuple[int, int], ...] = (
    (640, 1136),  # iPhone SE (1st gen) / iPhone 5/5s/5c
    (750, 1334),  # iPhone SE (2nd/3rd gen) / 6/7/8
    (828, 1792),  # iPhone XR / 11
    (1080, 2340),  # iPhone 12 mini / 13 mini (CSS 360×780 @ DPR 3) — codex /review M6
    (1125, 2436),  # iPhone X / Xs / 11 Pro
    (1170, 2532),  # iPhone 12/13/14 + Pro
    (1179, 2556),  # iPhone 15 / 15 Pro
    (1206, 2622),  # iPhone 16 Pro (D8)
    (1242, 2208),  # iPhone 6+/7+/8+ — downsampled from 1080×1920 panel
    (1242, 2688),  # iPhone Xs Max / 11 Pro Max
    (1260, 2736),  # iPhone Air (D8)
    (1284, 2778),  # iPhone 12/13/14 Pro Max
    (1290, 2796),  # iPhone 15 Pro Max
    (1320, 2868),  # iPhone 16/17 Pro Max (D8)
    (1536, 2048),  # iPad mini / iPad (10.2") @2x
    (1620, 2160),  # iPad 10.9" / Air 4 / mini 6
    (2048, 2732),  # iPad Pro 12.9"
)
# Sanity check at import time — D4 locks 17 sizes; if SPLASH_SIZES drifts the
# tests would catch it but a self-check here surfaces the bug at generator
# invocation.
assert len(SPLASH_SIZES) == 17, f"M6 D4/D8 locks 17 splash sizes; got {len(SPLASH_SIZES)}"


def _encode_png_deterministic(img: Image.Image) -> bytes:
    """Encode a PIL image to PNG bytes with no metadata and pinned compression.

    Re-encoding the same PIL image on the same Pillow version yields the same
    bytes — that's what makes the generator idempotent. ``optimize=False``
    keeps the compressor's choices deterministic across runs; we accept a
    slightly larger file for reproducibility.
    """
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=False, compress_level=9)
    return buf.getvalue()


def _icon_192(source: Image.Image) -> Image.Image:
    return source.resize((192, 192), Image.LANCZOS)


def _icon_512(source: Image.Image) -> Image.Image:
    if source.size == (512, 512):
        return source.copy()
    return source.resize((512, 512), Image.LANCZOS)


def _icon_maskable_512(source: Image.Image) -> Image.Image:
    """Logo at 80% scale (410x410) centered on a solid --bg 512x512 canvas.

    Outer 51px on each side is solid --bg so Android adaptive-icon shape masks
    (square / circle / squircle / teardrop) all show brand color in the corners
    rather than letting the system bg leak through.
    """
    canvas = Image.new("RGBA", (512, 512), BG_COLOR)
    inner = source.resize((410, 410), Image.LANCZOS)
    canvas.alpha_composite(inner, dest=(51, 51))
    return canvas


def _icon_bg_baked_512(source: Image.Image) -> Image.Image:
    """Logo at 100% scale on solid --bg. Used by iOS splash matrix in M6."""
    canvas = Image.new("RGBA", (512, 512), BG_COLOR)
    full = _icon_512(source)
    canvas.alpha_composite(full)
    return canvas


def _icon_bg_baked(source: Image.Image, size: int) -> Image.Image:
    """Generic bg-baked variant at arbitrary size. Solid --bg fill, logo at
    100% scale. Used for the iOS apple-touch-icon size matrix (180/152/167)
    so iOS doesn't fall back to a generated initial-letter placeholder when
    it can't find a matching size — caught by hardware QA on PR #252."""
    canvas = Image.new("RGBA", (size, size), BG_COLOR)
    inner = source.resize((size, size), Image.LANCZOS)
    canvas.alpha_composite(inner)
    return canvas


def _icon_bg_baked_180(source: Image.Image) -> Image.Image:
    return _icon_bg_baked(source, 180)


def _icon_bg_baked_167(source: Image.Image) -> Image.Image:
    return _icon_bg_baked(source, 167)


def _icon_bg_baked_152(source: Image.Image) -> Image.Image:
    return _icon_bg_baked(source, 152)


VARIANTS = (
    ("icon-192.png", _icon_192),
    ("icon-512.png", _icon_512),
    ("icon-maskable-512.png", _icon_maskable_512),
    ("icon-bg-baked-512.png", _icon_bg_baked_512),
    # Apple touch icon size matrix. iOS 17 generates a letter-initial
    # placeholder when no apple-touch-icon matches the device's pixel
    # density. 180 = iPhone @3x; 152 = iPad @2x; 167 = iPad Pro.
    ("icon-bg-baked-180.png", _icon_bg_baked_180),
    ("icon-bg-baked-167.png", _icon_bg_baked_167),
    ("icon-bg-baked-152.png", _icon_bg_baked_152),
)


def _invert_logo(source: Image.Image) -> Image.Image:
    """Color-invert RGB while preserving alpha — produces a cream-on-dark
    variant of the logo for dark-mode splashes (DD6).

    DD6 says: "Verifies dark-variant logo asset exists; if logo.png lacks a
    dark variant, generator produces a color-inverted version." We don't have
    a separate logo-dark.png; the inverted RGB of the existing logo is the
    locked dark variant. Alpha is preserved so anti-aliased edges still
    composite correctly on the dark canvas.
    """
    rgba = source.convert("RGBA")
    r, g, b, a = rgba.split()
    from PIL import ImageOps

    inv_rgb = Image.merge("RGB", (r, g, b))
    inv_rgb = ImageOps.invert(inv_rgb)
    ir, ig, ib = inv_rgb.split()
    return Image.merge("RGBA", (ir, ig, ib, a))


def _splash(source: Image.Image, w: int, h: int, dark: bool) -> Image.Image:
    """One splash frame at (w, h). Solid bg fill + logo at 50% of the canvas
    SHORTER side, vertically + horizontally centered (DD3).

    The shorter-side rule keeps the logo's visual weight consistent across
    portrait phones, tall phones, and ~4:3 iPads — any single canvas axis
    would either oversize on iPad or undersize on iPhone.
    """
    bg = BG_COLOR_DARK if dark else BG_COLOR
    canvas = Image.new("RGBA", (w, h), bg)
    logo_size = int(min(w, h) * 0.50)
    logo = (_invert_logo(source) if dark else source).resize((logo_size, logo_size), Image.LANCZOS)
    dest_x = (w - logo_size) // 2
    dest_y = (h - logo_size) // 2
    canvas.alpha_composite(logo, dest=(dest_x, dest_y))
    return canvas


def _splash_variants() -> list[tuple[str, int, int, bool]]:
    """Materialise the 17×2 = 34 splash entries.

    Filename format: ``splash-{W}x{H}-{light|dark}.png``. The W/H pair is
    the iOS device pixel resolution; the suffix lets the manifest media-query
    logic pick the right file unambiguously (no parsing the path inside
    base.html.j2).
    """
    out: list[tuple[str, int, int, bool]] = []
    for w, h in SPLASH_SIZES:
        out.append((f"splash-{w}x{h}-light.png", w, h, False))
        out.append((f"splash-{w}x{h}-dark.png", w, h, True))
    return out


def generate(check_only: bool = False, splash: bool = False) -> int:
    if not SOURCE_LOGO.exists():
        print(f"ERROR: source logo not found at {SOURCE_LOGO}", file=sys.stderr)
        return 2

    with Image.open(SOURCE_LOGO) as raw:
        source = raw.convert("RGBA")
    if source.size != (512, 512):
        print(
            f"WARN: expected logo.png 512x512, got {source.size}; output will still be normalized to spec sizes",
            file=sys.stderr,
        )

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    drift = 0
    for name, build in VARIANTS:
        dest = OUT_DIR / name
        new_bytes = _encode_png_deterministic(build(source))

        if check_only:
            existing = dest.read_bytes() if dest.exists() else b""
            if existing != new_bytes:
                print(f"DRIFT: {dest.relative_to(REPO_ROOT)}")
                drift += 1
            else:
                print(f"OK:    {dest.relative_to(REPO_ROOT)}")
        else:
            dest.write_bytes(new_bytes)
            print(f"wrote  {dest.relative_to(REPO_ROOT)} ({len(new_bytes):,} bytes)")

    if splash:
        SPLASH_DIR.mkdir(parents=True, exist_ok=True)
        for name, w, h, dark in _splash_variants():
            dest = SPLASH_DIR / name
            new_bytes = _encode_png_deterministic(_splash(source, w, h, dark))

            if check_only:
                existing = dest.read_bytes() if dest.exists() else b""
                if existing != new_bytes:
                    print(f"DRIFT: {dest.relative_to(REPO_ROOT)}")
                    drift += 1
                else:
                    print(f"OK:    {dest.relative_to(REPO_ROOT)}")
            else:
                dest.write_bytes(new_bytes)
                print(f"wrote  {dest.relative_to(REPO_ROOT)} ({len(new_bytes):,} bytes)")

    if check_only and drift:
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit nonzero if any output file would change. No files written.",
    )
    parser.add_argument(
        "--splash",
        action="store_true",
        help="Also generate the 34-entry iOS apple-touch-startup-image splash matrix (M6 D4/D8/DD3/DD6).",
    )
    args = parser.parse_args()
    return generate(check_only=args.check, splash=args.splash)


if __name__ == "__main__":
    sys.exit(main())
