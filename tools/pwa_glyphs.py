"""The control app's web fonts, as a glyph check (litclock-dev#532 DT8, D16).

Kept free of the clock's rendering stack — fontTools and the standard
library only — so scripts/validate_translation.py can run it for a
translator who has installed nothing but ``fonttools[woff]`` (litclock-dev#925 review).
tools/render_invariants.py re-exports it for the CI language check.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
PWA_FONTS_DIR = _REPO / "src" / "control_server" / "static" / "fonts"
# The panel's faces: status/setup/handoff copy (Regular), titles and labels
# (Black), the plain-time frame's line (SemiBoldItalic). eink_display.py and
# literary_clock.py name the same files.
EINK_FONTS = ("Literata72pt-Regular.ttf", "Literata72pt-Black.ttf", "Literata72pt-SemiBoldItalic.ttf")
# The catalog namespaces drawn on the panel (the translator kit's "E-ink"
# surfaces).
EINK_NAMESPACES = ("boot", "firstboot", "shutdown", "bootcheck", "reset", "clock")


def pwa_cmaps() -> dict[str, set[int]]:
    """Each control-app web font's code points. fontTools (requirements-dev)
    because the woff2 files need brotli, which freetype-py's bundled
    FreeType lacks. All four are required for every UI string, which is a
    little stricter than the CSS needs (Geist Mono shows only diagnostics
    values; the setup page uses system fonts) — moot today, the four cmaps
    are near-identical Latin-1 subsets. Raises when none is found, so a
    missing fonts directory cannot pass silently (litclock-dev#925 review)."""
    from fontTools.ttLib import TTFont  # noqa: PLC0415

    cmaps = {p.name: set(TTFont(p).getBestCmap()) for p in sorted(PWA_FONTS_DIR.glob("*.woff2"))}
    if not cmaps:
        raise FileNotFoundError(f"no woff2 fonts in {PWA_FONTS_DIR}")
    return cmaps


def eink_cmaps() -> dict[str, set[int]]:
    """The panel faces' code points (plain TTFs, no brotli needed)."""
    from fontTools.ttLib import TTFont  # noqa: PLC0415

    return {name: set(TTFont(_REPO / "fonts" / name).getBestCmap()) for name in EINK_FONTS}


def eink_missing_chars(text: str, cmaps: dict[str, set[int]]) -> dict[str, list[str]]:
    """{face: [characters it lacks]} — EVERY character but the space and
    line break the renderers handle themselves: the panel has no per-glyph
    fallback, so a missing symbol or space (U+202F, French typography's
    narrow no-break space) is an empty box on the glass (D16/R11)."""
    chars = {ch for ch in unicodedata.normalize("NFC", text) if ch not in " \n"}
    gaps = {name: sorted(ch for ch in chars if ord(ch) not in cmap) for name, cmap in cmaps.items()}
    return {name: found for name, found in gaps.items() if found}


def pwa_missing_letters(text: str, cmaps: dict[str, set[int]]) -> dict[str, list[str]]:
    """{font: [letters it lacks]} for the letters and combining marks in
    ``text`` (Unicode categories L* and M*) — see render_invariants L7."""
    # NFC first: a file saved decomposed (e + U+0302) is drawn precomposed by
    # the browser when the font has the composed letter (litclock-dev#925 review).
    letters = {ch for ch in unicodedata.normalize("NFC", text) if unicodedata.category(ch)[0] in "LM"}
    gaps = {name: sorted(ch for ch in letters if ord(ch) not in cmap) for name, cmap in cmaps.items()}
    return {name: chars for name, chars in gaps.items() if chars}


