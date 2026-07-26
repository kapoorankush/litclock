"""Runtime quote renderer — Python/PIL port of image-gen/quote_to_image.php.

Produces the same layout as the GD pipeline (dev#531 Stage-0 evidence:
layout parity 4,809/4,809 fs + line breaks on the full EN corpus, hardware
A/B pass on the owner clock, calibrated blind machine judge clean):

  - measurement is GD-exact via ``gd_measure`` (freetype-py replicating
    gdft.c brect math), never Pillow's own metrics;
  - glyphs are drawn ONE AT A TIME at GD's integer pen positions
    (``gd_pen_positions``) so intra-word geometry matches GD ink — whole-word
    PIL drawing drifts px-narrower at large fs and reads as double-spaces;
  - the layout algorithm mirrors fitText byte-for-byte: greedy wrap on
    measured advance widths over the space-split quote, line height
    ``php_round(fs*1.618)``, grow-until-fit from 18 while the paragraph
    height stays under ``H-100``, bold = EXACTLY the matched timestring
    span (dev#540 — the #503/#504 boundary-extension cases were deleted;
    bolding errors are corpus data fixes, see timestring_midword_edge);
  - credits use ``gd_bbox`` (measureSizeOfTextbox port) verbatim, including
    the two-line balance loop and the single-line ``left``-based x.

Deliberate divergences from the PHP (each fails loud instead of rendering
garbage; the render-invariants CI job proves the corpus never hits them):
  - a credits line that cannot be two-line balanced raises
    ``CreditsBalanceError`` (PHP reads an unset array key and paints a bare
    dash);
  - the two-line balance loop compares UTF-8 BYTE lengths like PHP strlen
    (the Stage-0 spike compared character counts — same result on the EN
    corpus, kept exact here for #532);
  - a corpus row whose quote still contains a backslash after the escape
    chain raises ``CorpusGuardError`` in strict mode (PHP fgetcsv's escape
    handling diverges from csv.reader there).

Rendering timestamps/fonts: PIL renders at 96 DPI like GD, so fonts load at
``ptsize * 4/3`` px (PIL sizes are px @72). Output is a mode-"L" GREYSCALE
image with anti-aliased fringes — unlike the shipped GD PNGs, which are
palette-bilevel. STAGE-2 CONTRACT: the panel conversion MUST be
``convert("1", dither=Image.Dither.NONE)`` (threshold at 128, matching
GD's ~50% AA rounding). literary_clock.py's current ``convert("1")``
defaults to Floyd-Steinberg, which is a no-op on bilevel PNGs today but
would stochastically speckle this renderer's grey fringes.

Not thread-safe: gd_measure re-sizes shared FT_Face objects per call
(single-threaded clock process only — see gd_measure docstring).
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from gd_measure import gd_bbox, gd_pen_positions, gd_text_width, php_round

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
FONTS_DIR = Path(os.environ.get("LITCLOCK_FONTS_DIR", str(_PROJECT_ROOT / "fonts")))

FONT_REGULAR = "Literata72pt-ExtraLight.ttf"
FONT_BOLD = "Literata72pt-Black.ttf"
FONT_CREDITS = "Literata72pt-SemiBoldItalic.ttf"

WIDTH, HEIGHT, MARGIN = 800, 400, 10
START_FONT_SIZE = 18
CREDITS_FONT_SIZE = 18
CREDITS_MAX_WIDTH = 500
LINE_HEIGHT_RATIO = 1.618
DASH = "—"

# PHP defaults the renderer must mirror exactly:
# trim() strips " \t\n\r\0\x0B"; preg_replace('/\s+/') without /u is the
# ASCII class [ \t\n\x0B\f\r] (Python's \s would be Unicode-wide).
PHP_TRIM_CHARS = " \t\n\r\0\x0b"
_PHP_WS_RE = re.compile(r"[ \t\n\x0b\f\r]+")

# The escape-sequence cleanup chain from quote_to_image.php, in order.
# Literal backslash counts (4/2/1 before n, 5/3/1 before ") are built
# programmatically — they were verified by eval'ing the PHP source literals,
# and the PHP comments themselves miscount one of them.
_BS = "\\"
_ESCAPE_CHAIN = (
    (_BS * 4 + "n", " "),
    (_BS * 2 + "n", " "),
    (_BS + "n", " "),
    (_BS * 5 + '"', '"'),
    (_BS * 3 + '"', '"'),
    (_BS + '"', '"'),
)

# \p{L}\p{N} equivalent for the corpus-quality mid-word probe. [^\W_] = word
# chars minus underscore. Python \w and PCRE \p{L}\p{N} agree on the EN
# corpus (Stage-0 parity); the corpus guards below stay armed for #532.
_WORD_RE = re.compile(r"^[^\W_]", re.UNICODE)

_ENTITY_RE = re.compile(r"&#?\w+;")
_EXOTIC_WS = set(
    "\u0085\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
)


class RenderError(Exception):
    """Quote cannot be rendered. ``reason`` mirrors the PHP counters:
    'nostring' (timestring not found) or 'nofit' (font range exhausted).

    STAGE-2 CONTRACT: the invariant CI proves these absent on the shipped
    corpus, but user-edited corpora bypass that gate — the on-device caller
    must catch RenderError/CreditsBalanceError per render and fall back
    (render -> last-good image -> numeral clock), never crash-loop the
    per-minute timer on one bad row."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


class CreditsBalanceError(Exception):
    """Two-line credits balance loop produced no split (PHP would render a
    bare dash from an unset array key). Never happens on the shipped corpus
    — enforced by the render-invariants CI job."""


class CorpusGuardError(Exception):
    """Corpus row would be parsed differently by PHP fgetcsv and Python
    csv.reader (or by production's escape chain) — parity can't be trusted."""


def _font_path(name: str) -> str:
    return str(FONTS_DIR / name)


@lru_cache(maxsize=8)
def _pil_font(name: str, ptsize: int) -> ImageFont.FreeTypeFont:
    # GD renders at 96 DPI: pixel size = ptsize * 4/3 (PIL sizes are px @72).
    # maxsize 8, not larger: only the final fit size + the credits size are
    # ever DRAWN per render, and each cached font pins a ~230KB face on a
    # 512MB Pi.
    return ImageFont.truetype(_font_path(name), ptsize * 4 / 3)


def _width(name: str, ptsize: int, seg: bytes) -> int:
    """GD-exact advance width of a byte segment. A UnicodeDecodeError here
    means a bold-span offset cut a multibyte char — a real bug; let it
    raise (stristr alignment guarantees char-boundary offsets)."""
    return gd_text_width(_font_path(name), ptsize, seg.decode("utf-8"))


def preprocess_quote(raw: str) -> str:
    """The production escape collapse: quote_to_image.php's six-replace
    chain, then ASCII whitespace collapse, then PHP trim."""
    for src, dst in _ESCAPE_CHAIN:
        raw = raw.replace(src, dst)
    return _PHP_WS_RE.sub(" ", raw).strip(PHP_TRIM_CHARS)


def guard_corpus_row(raw_quote: str, warn: Callable[[str], None] | None = None) -> None:
    """Reject/flag divergence classes that are dormant on the EN corpus but
    would silently break parity (ported from the Stage-0 harness; extended
    to strip all six production escape forms before the backslash check)."""
    stripped = raw_quote
    for src, _ in _ESCAPE_CHAIN:
        stripped = stripped.replace(src, "")
    if _BS in stripped:
        raise CorpusGuardError(
            "quote contains a backslash outside the production escape forms; "
            "fgetcsv and csv.reader parse backslash-quote differently — "
            "extend the escape chain before trusting parity"
        )
    if warn is None:
        warn = logging.getLogger(__name__).warning
    if _ENTITY_RE.search(raw_quote):
        warn("quote contains an HTML-entity-shaped substring; GD interprets entities, this renderer does not")
    if _EXOTIC_WS & set(raw_quote):
        warn("quote contains non-ASCII whitespace; PHP \\s+ is ASCII-only — collapse differs")
    nbytes = len(raw_quote.encode("utf-8"))
    if nbytes > 4000:
        warn(
            f"row is {nbytes} bytes — approaching fgetcsv's 5000-byte line cap; "
            "PHP would split an over-cap row into two, csv.reader would not"
        )


def find_timestring(quote_bytes: bytes, timestring_bytes: bytes) -> int:
    """PHP stristr mirror: ASCII-case-insensitive byte search (bytes.lower
    is ASCII-only, exactly like stristr's C locale folding)."""
    return quote_bytes.lower().find(timestring_bytes.lower())


# ---- Corpus-quality helpers (NOT part of rendering — dev#540) ----
# The renderer bolds EXACTLY the matched timestring span (owner decision
# 2026-07-26, dev#540): the CSV row IS the bold spec, so bolding errors are
# data fixes contributors can make per-language — no renderer heuristics.
# The #503/#504 boundary-extension case machinery (mid-word extension,
# trailing-punctuation bolding, hyphen-join guard) was deleted here; the
# word-char predicate survives ONLY to flag questionable data at edit time.


def _word_char_at(qb: bytes, i: int) -> bool:
    # One UTF-8 char starting at byte i (i is always at a char boundary);
    # decode-with-ignore drops any trailing partial char in the 4-byte
    # window (#27 semantics). Corpus-quality use only.
    if i >= len(qb):
        return False
    ch = qb[i : i + 4].decode("utf-8", "ignore")[:1]
    return bool(ch) and bool(_WORD_RE.match(ch))


def timestring_midword_edge(quote: str, timestring: str) -> str | None:
    """Data-quality probe for corpus tooling: does the timestring's match
    start or end in the middle of a word? Returns 'start', 'end', 'both',
    or None. A mid-word edge renders a half-bold word — almost always a
    corpus bug (missing space, or the timestring should include the whole
    word, e.g. 'noon' vs 'noonday', '230' vs '0230'). Fix the row, never
    the renderer."""
    qb = quote.encode("utf-8")
    tsb = timestring.encode("utf-8")
    idx = find_timestring(qb, tsb)
    if idx < 0:
        return None
    prev = idx - 1
    while prev > 0 and 0x80 <= qb[prev] < 0xC0:  # back up over UTF-8 continuation bytes
        prev -= 1
    start_mid = idx > 0 and _word_char_at(qb, idx) and _word_char_at(qb, prev)
    end_mid = _word_char_at(qb, idx + len(tsb))
    if start_mid and end_mid:
        return "both"
    if start_mid:
        return "start"
    if end_mid:
        return "end"
    return None


@dataclass(frozen=True)
class Layout:
    """One fitText pass. ``ops`` are draw operations (x, baseline_y,
    utf-8 segment, bold); ``breaks`` are word indices that started a new
    line (the Stage-0 parity fingerprint, alongside the font size)."""

    ops: tuple[tuple[int, int, bytes, bool], ...]
    paragraph_height: int
    breaks: tuple[int, ...]


def layout_pass(words: list[bytes], font_size: int, ts_start: int, ts_end: int) -> Layout | None:
    """Single source of truth for the fitText layout algorithm (the Stage-0
    spike kept two implementations cross-checked at render time; the port
    keeps one, and the invariant CI diffs it against the PHP probe).
    Returns None when any word exceeds the drawable width — fitText's
    stop-enlarging signal."""
    x, y = MARGIN, MARGIN + font_size
    off = 0
    ops: list[tuple[int, int, bytes, bool]] = []
    breaks: list[int] = []
    for k, wb in enumerate(words):
        wlen = len(wb)
        b0 = max(ts_start, off) - off
        b1 = min(ts_end, off + wlen) - off
        has_bold = b1 > b0 and b0 < wlen and b1 > 0
        fully_bold = has_bold and b0 <= 0 and b1 >= wlen
        if not has_bold or fully_bold:
            segs = [(wb, fully_bold)]
            tw = _width(FONT_BOLD if fully_bold else FONT_REGULAR, font_size, wb + b" ")
        else:
            b0, b1 = max(0, b0), min(wlen, b1)
            segs = []
            if b0 > 0:
                segs.append((wb[:b0], False))
            segs.append((wb[b0:b1], True))
            if b1 < wlen:
                segs.append((wb[b1:], False))
            tw = sum(_width(FONT_BOLD if bold else FONT_REGULAR, font_size, sb) for sb, bold in segs)
            tw += _width(FONT_REGULAR, font_size, b" ")
        if tw > WIDTH - MARGIN:
            return None
        if x + tw >= WIDTH - MARGIN:
            x = MARGIN
            y += php_round(font_size * LINE_HEIGHT_RATIO)
            breaks.append(k)
        sx = x
        for sb, bold in segs:
            ops.append((sx, y, sb, bold))
            sx += _width(FONT_BOLD if bold else FONT_REGULAR, font_size, sb)
        x += tw
        off += wlen + 1
    return Layout(tuple(ops), y, tuple(breaks))


def fit(words: list[bytes], ts_start: int, ts_end: int) -> tuple[Layout, int] | None:
    """Largest font size whose paragraph height stays under HEIGHT-100
    (room for the credits), growing from START_FONT_SIZE like the PHP
    recursion. Returns (layout, font_size) or None when even the start
    size doesn't fit."""
    best = None
    font_size = START_FONT_SIZE
    while True:
        result = layout_pass(words, font_size, ts_start, ts_end)
        if result is None or result.paragraph_height >= HEIGHT - 100:
            break
        best = (result, font_size)
        font_size += 1
    return best


def _draw_segment(draw: ImageDraw.ImageDraw, font_name: str, font_size: int, x: int, y: int, text: str) -> None:
    # imagettftext's y is the baseline; PIL anchor "ls" = left-baseline.
    # Each glyph lands at GD's own integer pen position.
    pil_font = _pil_font(font_name, font_size)
    for ch, off in zip(text, gd_pen_positions(_font_path(font_name), font_size, text), strict=True):
        draw.text((x + off, y), ch, font=pil_font, fill=0, anchor="ls")


def render_quote(quote: str, timestring: str) -> tuple[Image.Image, int, Layout]:
    """Render the quote-only image (the PHP's first imagepng). ``quote``
    must already be preprocessed; ``timestring`` already trimmed.
    Returns (image, font_size, layout); raises RenderError on the two
    production failure classes."""
    qb = quote.encode("utf-8")
    tsb = timestring.encode("utf-8")
    idx = find_timestring(qb, tsb)
    if idx < 0:
        raise RenderError("nostring", f"timestring {timestring!r} not in quote")
    # Exact-span bolding (dev#540): the matched CSV timestring, nothing more.
    ts_start, ts_end = idx, idx + len(tsb)

    fitted = fit(qb.split(b" "), ts_start, ts_end)
    if fitted is None:
        raise RenderError("nofit", f"no font size fits {quote[:60]!r}...")
    lay, font_size = fitted

    if __debug__:
        # Span-slicing self-check: segments must reassemble to the quote
        # minus its spaces — a mismatch means a bold offset sliced wrong.
        assert b"".join(sb for _, _, sb, _ in lay.ops) == qb.replace(b" ", b""), (
            "layout segments do not reassemble to the quote"
        )

    img = Image.new("L", (WIDTH, HEIGHT), 255)
    draw = ImageDraw.Draw(img)
    for x, y, sb, bold in lay.ops:
        _draw_segment(draw, FONT_BOLD if bold else FONT_REGULAR, font_size, x, y, sb.decode("utf-8"))
    return img, font_size, lay


def add_credits(img: Image.Image, title: str, author: str) -> None:
    """Draw the attribution block in place (the PHP draws onto the same GD
    image and saves it a second time as the _credits variant). Port of
    quote_to_image.php lines 294-356, using gd_bbox for every measurement.
    Two-line leading is the measured bbox height * 1.1 (an 18px
    approximation sat ~6px low — caught by the blind machine judge on the
    fs-110 pair); the PHP passes a float y which truncates toward zero."""
    draw = ImageDraw.Draw(img)
    cred_path = _font_path(FONT_CREDITS)
    credits = f"{title}, {author}"
    meta_w, _meta_h, meta_left, _meta_top = gd_bbox(cred_path, CREDITS_FONT_SIZE, DASH + credits)
    if meta_w > CREDITS_MAX_WIDTH:
        parts = credits.split(" ")
        best = None
        i = 1
        while True:
            line0 = " ".join(parts[: len(parts) - i])
            line1 = " ".join(parts[len(parts) - i :])
            # PHP strlen compares BYTE lengths — keep that for non-ASCII
            # credits (#532); identical on the EN corpus.
            if len(line1.encode("utf-8")) + 5 > len(line0.encode("utf-8")):
                break
            best = (line0, line1)
            i += 1
        if best is None:
            raise CreditsBalanceError(f"no two-line balance for {credits!r}")
        line0, line1 = best
        w1, h1, _, _ = gd_bbox(cred_path, CREDITS_FONT_SIZE, DASH + line0)
        w2, _, _, _ = gd_bbox(cred_path, CREDITS_FONT_SIZE, line1)
        base_y = HEIGHT - MARGIN
        _draw_segment(
            draw, FONT_CREDITS, CREDITS_FONT_SIZE, WIDTH - (w1 + MARGIN), int(base_y - h1 * 1.1), DASH + line0
        )
        _draw_segment(draw, FONT_CREDITS, CREDITS_FONT_SIZE, WIDTH - (w2 + MARGIN), base_y, line1)
    else:
        _draw_segment(
            draw, FONT_CREDITS, CREDITS_FONT_SIZE, (WIDTH - meta_left) - MARGIN, HEIGHT - MARGIN, DASH + credits
        )


@dataclass(frozen=True)
class CorpusRow:
    """One renderable corpus row with its PHP-derived identity.
    ``basename`` is the PHP filename stem: quote_{HHMM}_{n}[_nsfw]
    (append .png / _credits.png)."""

    ordinal: int  # 1-based data-row count, matches the PHP $row-1
    time: str
    hhmm: str
    image_number: int
    is_nsfw: bool
    basename: str
    timestring: str
    quote: str  # preprocessed
    title: str
    author: str


def iter_corpus(
    csv_path: str | os.PathLike, strict: bool = True, warn: Callable[[str], None] | None = None
) -> Iterator[CorpusRow]:
    """Yield renderable rows exactly as the PHP main loop walks the CSV:
    rows with <5 fields skipped, image_number incremented while the
    consecutive time key repeats, NSFW = field 6 == 'YES' (case-folded).
    strict=True raises CorpusGuardError on fgetcsv-divergent rows."""
    import csv as _csv

    previous_key: str | None = None
    image_number = 0
    ordinal = 0
    with open(csv_path, encoding="utf-8", newline="") as fh:
        for row in _csv.reader(fh, delimiter="|"):
            if len(row) < 5:
                continue
            ordinal += 1
            if strict:
                # Guard the WHOLE row, not just the quote field: an
                # fgetcsv-vs-csv.reader escape divergence can column-shift
                # the parse, landing the offending backslash in a field
                # other than row[2] (dev#536 review, codex).
                guard_corpus_row("|".join(row), warn=warn)
            time = row[0]
            key = time[:2] + time[3:5]
            if key == previous_key:
                image_number += 1
            else:
                image_number = 0
            previous_key = key
            is_nsfw = len(row) > 5 and row[5].strip(PHP_TRIM_CHARS).upper() == "YES"
            yield CorpusRow(
                ordinal=ordinal,
                time=time,
                hhmm=key,
                image_number=image_number,
                is_nsfw=is_nsfw,
                basename=f"quote_{key}_{image_number}{'_nsfw' if is_nsfw else ''}",
                timestring=row[1].strip(PHP_TRIM_CHARS),
                quote=preprocess_quote(row[2]),
                title=row[3].strip(PHP_TRIM_CHARS),
                author=row[4].strip(PHP_TRIM_CHARS),
            )


def rows_for_time(csv_path: str | os.PathLike, hhmm: str) -> list[CorpusRow]:
    """All corpus rows for one HHMM bucket, in file order (the runtime
    selection pool — mirrors the clock's PNG glob semantics). strict=False:
    at runtime the Python renderer IS the renderer, so PHP-parity guards
    are a CI concern, and one odd row elsewhere in the CSV must not take
    down selection for this bucket."""
    return [r for r in iter_corpus(csv_path, strict=False) if r.hhmm == hhmm]


def render_row(row: CorpusRow) -> tuple[Image.Image, Image.Image, int, Layout]:
    """Render both production variants for a corpus row:
    (quote_image, credits_image, font_size, layout). The credits image is a
    copy — callers keep both, unlike the PHP which mutates and saves twice.
    The layout is exposed so invariant checks can assert on paragraph
    height, not just the pixels (dev#536 review)."""
    img, font_size, layout = render_quote(row.quote, row.timestring)
    credits_img = img.copy()
    add_credits(credits_img, row.title, row.author)
    return img, credits_img, font_size, layout


def reset_caches() -> None:
    """Test hook — clear PIL font cache (gd_measure has its own)."""
    _pil_font.cache_clear()
