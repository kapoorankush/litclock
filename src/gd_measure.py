"""GD-exact text measurement for the runtime quote renderer (litclock-dev#531 Stage 1).

Replicates libgd 2.3.3 ``gdImageStringFTEx`` metrics math for angle=0,
byte-for-byte (Stage-0 validation: 66,582/66,582 measurements exact vs PHP
``imagettfbbox``, full corpus x 27 font sizes x 3 Literata faces).

The algorithm (gdft.c):
  - metrics face size: ``FT_Set_Char_Size(0, ptsize*64, METRIC_RES=300, 300)``
    -> ppem = round(ptsize * 300/72), NOT the 96-DPI render size. This is why
    Pillow-only approximations (getlength/getbbox at fs*4/3) sit +-2px off:
    hinting runs at a different ppem.
  - per glyph, FT_LOAD_DEFAULT (hinted) metrics:
      min.x = pen + horiBearingX
      max.x = pen + horiAdvance      # full advance: trailing spaces DO count
      pen  += horiAdvance            # (+ legacy kern-table delta, if any)
  - brect coords = C ``(int)`` cast of total * 96/(300*64)  [truncation]
  - imagettfbbox x-extent = trunc(max) - trunc(min)

Environment sensitivity: pip wheels of freetype-py BUNDLE their own
libfreetype rather than binding the system library GD links against. Hinted
metrics are interpreter-version-sensitive, so parity must be re-validated
per environment (the development repo's render-invariants CI proves it on
every PR; on-device, ``tools/validate_measurement.py check --stamp`` is the
gate). Stage-0 held on FreeType 2.13.2 == 2.13.2.

freetype-py is imported lazily so pure helpers (``php_round``) stay
importable without it (e.g. on a Pi that hasn't flipped to runtime
rendering yet).
"""

from __future__ import annotations

from functools import lru_cache

METRIC_RES = 300
GD_RESOLUTION = 96
_SCALE = GD_RESOLUTION / (64 * METRIC_RES)

# Faces are cached per font PATH (the repo ships 3 Literata faces) and
# re-sized in place via set_char_size when the requested ptsize changes —
# a (path, ptsize) cache would grow with every grow-until-fit pass
# (~90 sizes x faces) and hold that many open FT_Face objects forever in
# the long-lived clock process. dict[str, [Face, int|None]]
# NOT THREAD-SAFE: two threads interleaving different ptsizes on the same
# face would return silently wrong widths (and lru_cache would pin them).
# The clock renders single-threaded; add a lock before any threaded use.
_faces: dict[str, list] = {}


def php_round(x: float) -> int:
    """PHP round(): half away from zero (Python round() is banker's)."""
    return int(x + 0.5) if x >= 0 else -int(-x + 0.5)


def _face(font_path: str, ptsize: int):
    import freetype

    entry = _faces.get(font_path)
    if entry is None:
        entry = [freetype.Face(font_path), None]
        _faces[font_path] = entry
    if entry[1] != ptsize:
        entry[0].set_char_size(0, ptsize * 64, METRIC_RES, METRIC_RES)
        entry[1] = ptsize
    return entry[0]


@lru_cache(maxsize=4096)
def gd_text_width(font_path: str, ptsize: int, s: str) -> int:
    """Integer pixel width of ``s``, exactly as GD's imagettfbbox x-extent.

    lru_cache bounds the long-lived clock process (the unbounded spike dict
    was a Stage-1 must-fix); 4096 entries comfortably cover one render's
    grow-until-fit word set with room for cross-render reuse.
    """
    import freetype

    face = _face(font_path, ptsize)
    has_kern = face.has_kerning
    pen = 0
    tmin = tmax = None
    prev = 0
    for ch in s:
        gi = face.get_char_index(ch)
        if has_kern and prev and gi:
            pen += face.get_kerning(prev, gi, freetype.FT_KERNING_DEFAULT).x
        face.load_glyph(gi, freetype.FT_LOAD_DEFAULT)
        m = face.glyph.metrics
        gmin = pen + m.horiBearingX
        gmax = pen + m.horiAdvance
        if tmin is None:
            tmin, tmax = gmin, gmax
        else:
            if gmin < tmin:
                tmin = gmin
            if gmax > tmax:
                tmax = gmax
        pen += m.horiAdvance
        prev = gi
    if tmin is None:
        return 0
    return int(tmax * _SCALE) - int(tmin * _SCALE)  # C (int) casts: truncate


def gd_pen_positions(font_path: str, ptsize: int, s: str) -> list[int]:
    """Per-char draw offsets (int 96-DPI px), exactly as gdft.c places glyph
    bitmaps: pen accumulated in METRIC_RES=300 DPI 26.6 space, each glyph
    blitted at ``(int)(pen * hdpi/(METRIC_RES*64))``. Drawing each char at
    x + offset reproduces GD's intra-word geometry (litclock-dev#531 attempt-2 A/B:
    word-at-a-time PIL drawing let hinted PIL advances drift px-narrower
    than GD ink at large fs, reading as double-spaces)."""
    import freetype

    face = _face(font_path, ptsize)
    has_kern = face.has_kerning
    pen = 0
    prev = 0
    offsets = []
    for ch in s:
        gi = face.get_char_index(ch)
        if has_kern and prev and gi:
            pen += face.get_kerning(prev, gi, freetype.FT_KERNING_DEFAULT).x
        offsets.append(int(pen * GD_RESOLUTION / (METRIC_RES * 64)))
        face.load_glyph(gi, freetype.FT_LOAD_DEFAULT)
        pen += face.glyph.metrics.horiAdvance
        prev = gi
    return offsets


@lru_cache(maxsize=512)
def gd_bbox(font_path: str, ptsize: int, s: str) -> tuple[int, int, int, int]:
    """``(width, height, left, top)`` exactly as quote_to_image.php's
    measureSizeOfTextbox: GD brect extents with left = abs(min_x)+width,
    top = abs(min_y)+height. Vertical math per gdft.c: glyph_min.y =
    -horiBearingY, glyph_max.y = glyph_min.y + metrics.height, scaled by
    96/(300*64) with C truncation-toward-zero per corner. Stage-0
    validation: 100/100 vs PHP on corpus credits strings."""
    import freetype

    face = _face(font_path, ptsize)
    has_kern = face.has_kerning
    pen = 0
    txmin = txmax = tymin = tymax = None
    prev = 0
    for ch in s:
        gi = face.get_char_index(ch)
        if has_kern and prev and gi:
            pen += face.get_kerning(prev, gi, freetype.FT_KERNING_DEFAULT).x
        face.load_glyph(gi, freetype.FT_LOAD_DEFAULT)
        m = face.glyph.metrics
        gxmin = pen + m.horiBearingX
        gxmax = pen + m.horiAdvance
        gymin = -m.horiBearingY
        gymax = gymin + m.height
        if txmin is None:
            txmin, txmax, tymin, tymax = gxmin, gxmax, gymin, gymax
        else:
            txmin = min(txmin, gxmin)
            txmax = max(txmax, gxmax)
            tymin = min(tymin, gymin)
            tymax = max(tymax, gymax)
        pen += m.horiAdvance
        prev = gi
    if txmin is None:
        return 0, 0, 0, 0
    xmin, xmax = int(txmin * _SCALE), int(txmax * _SCALE)
    ymin, ymax = int(tymin * _SCALE), int(tymax * _SCALE)
    w, h = xmax - xmin, ymax - ymin
    return w, h, abs(xmin) + w, abs(ymin) + h


def reset_caches() -> None:
    """Test hook — drop cached faces and measurements."""
    _faces.clear()
    gd_text_width.cache_clear()
    gd_bbox.cache_clear()
