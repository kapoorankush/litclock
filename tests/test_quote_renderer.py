"""Unit tests for src/quote_renderer.py (litclock-dev#531 Stage 1).

Pure-function tests (preprocess, exact-span bolding, corpus iteration) run
everywhere. Tests that measure or render text require freetype-py and the
repo fonts; they skip cleanly where freetype-py isn't installed (same
pattern as the hardware-gated eink tests). The full-corpus layout-parity
gate lives in tools/render_invariants.py + CI, not here.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

import quote_renderer as qr

BS = "\\"


# ---------------------------------------------------------------- preprocess


def test_preprocess_all_six_escape_forms():
    # 4/2/1 backslashes before n -> space; 5/3/1 before " -> "
    raw = f'a {BS * 4}n b {BS * 2}n c {BS}n d {BS * 5}" e {BS * 3}" f {BS}" g'
    assert qr.preprocess_quote(raw) == 'a b c d " e " f " g'


def test_preprocess_chain_order_longest_first():
    # 4bs+n must be consumed as one token, not as 2bs + 2bs+n leaving debris
    assert qr.preprocess_quote(f"x{BS * 4}ny") == "x y"
    # 3bs+quote consumed before 1bs+quote
    assert qr.preprocess_quote(f'x{BS * 3}"y') == 'x"y'


def test_preprocess_whitespace_collapse_is_ascii_only():
    # PHP preg_replace('/\\s+/') without /u: NBSP survives, \t\n collapse
    assert qr.preprocess_quote("a\t\n b") == "a b"
    assert qr.preprocess_quote("a\u00a0\u00a0b") == "a\u00a0\u00a0b"


def test_preprocess_trim_matches_php_trim_set():
    assert qr.preprocess_quote("\x0b\x00 mid \x00\x0b") == "mid"
    # PHP trim does NOT strip NBSP; the ASCII collapse doesn't touch it either
    assert qr.preprocess_quote("\u00a0mid") == "\u00a0mid"


# ------------------------------------------------------------- corpus guards


def test_guard_accepts_production_escape_forms():
    qr.guard_corpus_row(f'ok {BS}n and {BS}" here')


def test_guard_rejects_stray_backslash():
    with pytest.raises(qr.CorpusGuardError):
        qr.guard_corpus_row(f"a{BS}b")


def test_guard_warns_on_entities_and_exotic_whitespace():
    warnings: list[str] = []
    qr.guard_corpus_row("five &amp; six", warn=warnings.append)
    qr.guard_corpus_row("thin\u2009space", warn=warnings.append)
    assert len(warnings) == 2
    assert "entity" in warnings[0]
    assert "whitespace" in warnings[1]


# ------------------------------------------------- timestring + bold boundary


def test_find_timestring_is_ascii_case_insensitive():
    qb = b"It was Midnight, then."
    assert qr.find_timestring(qb, b"midnight") == 7
    # No Unicode case folding (stristr is byte/ASCII-ci): İ != i̇
    assert qr.find_timestring("İstanbul at noon".encode(), b"istanbul") == -1


def _span(quote: str, ts: str) -> str:
    """The bolded text for a timestring: with exact-span bolding (litclock-dev#540)
    this is ALWAYS the matched substring itself."""
    qb = quote.encode()
    idx = qr.find_timestring(qb, ts.encode())
    assert idx >= 0
    return qb[idx : idx + len(ts.encode())].decode()


def test_exact_span_never_extends():
    # litclock-dev#540: the CSV timestring IS the bold spec — no case machinery.
    assert _span("in the tenth hour", "ten") == "ten"  # mid-word: half-bold = data bug, flagged by probe
    assert _span("struck midnight. Then", "midnight") == "midnight"  # trailing punct stays regular
    assert _span("at midnight", "midnight") == "midnight"
    assert _span("four minutes to ten-four they said", "ten") == "ten"
    assert _span("at ten\u2014\u6771\u4eac station", "ten") == "ten"
    assert _span("at ten\u2026 then", "ten") == "ten"


def test_interior_punctuation_stays_bold():
    # punctuation INSIDE the CSV phrase is part of the matched span
    assert _span("it was eleven o'clock at night", "eleven o'clock") == "eleven o'clock"
    assert _span("by half-past ten they left", "half-past ten") == "half-past ten"


def test_midword_edge_probe():
    # the corpus-quality probe flags what exact-span will render half-bold
    assert qr.timestring_midword_edge("The noonday siren blew", "noon") == "end"
    assert qr.timestring_midword_edge("at 0230 hours sharp", "230") == "start"
    assert qr.timestring_midword_edge("it was 11:37 A.M.local time", "11:37 A.M.") == "end"
    assert qr.timestring_midword_edge("struck midnight. Then", "midnight") is None
    # em-dash join is punctuation, not a word char -> not mid-word
    assert qr.timestring_midword_edge("at ten\u2014\u6771\u4eac station", "ten") is None
    assert qr.timestring_midword_edge("caf\u00e9ten time", "ten") == "start"  # multibyte word char before match
    assert qr.timestring_midword_edge("no match here", "midnight") is None


# ------------------------------------------------------------- corpus reader


def _write_corpus(tmp_path, body: str):
    p = tmp_path / "corpus.csv"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def test_iter_corpus_image_number_and_nsfw(tmp_path):
    p = _write_corpus(
        tmp_path,
        """\
        00:00|midnight|It was midnight already.|T1|A1
        00:00|midnight|Still midnight there.|T2|A2|yes
        00:01|midnight|Past midnight now.|T3|A3
        short|row
        00:00|midnight|Back at midnight again.|T4|A4
        """,
    )
    rows = list(qr.iter_corpus(p))
    assert [r.basename for r in rows] == [
        "quote_0000_0",
        "quote_0000_1_nsfw",
        "quote_0001_0",
        # PHP quirk mirrored: a non-consecutive repeat of a time key RESETS
        # the counter (the shipped corpus is sorted, so this never collides)
        "quote_0000_0",
    ]
    assert [r.ordinal for r in rows] == [1, 2, 3, 4]
    assert rows[1].is_nsfw and not rows[0].is_nsfw
    # the clock's NSFW glob filter matches on "_nsfw_" in the credits name
    assert "_nsfw_" in rows[1].basename + "_credits.png"


def test_iter_corpus_strict_guard(tmp_path):
    p = _write_corpus(tmp_path, f"00:00|midnight|bad {BS}x quote|T|A\n")
    with pytest.raises(qr.CorpusGuardError):
        list(qr.iter_corpus(p))
    assert len(list(qr.iter_corpus(p, strict=False))) == 1


# ------------------------------------------------------------------ php_round


def test_php_round_half_away_from_zero():
    from gd_measure import php_round

    assert php_round(0.5) == 1
    assert php_round(1.5) == 2
    assert php_round(2.5) == 3  # Python's round(2.5) == 2 (banker's)
    assert php_round(-0.5) == -1
    assert php_round(29.124) == 29


# ----------------------------------------------- measurement + render (fonts)

try:
    import freetype  # noqa: F401

    _HAVE_FREETYPE = True
except ImportError:
    _HAVE_FREETYPE = False

needs_freetype = pytest.mark.skipif(not _HAVE_FREETYPE, reason="freetype-py required for GD-exact measurement")


@needs_freetype
def test_fit_returns_largest_fitting_size():
    quote = "It was almost midnight when the church bells finally stopped ringing across the empty square."
    qb = qr.preprocess_quote(quote).encode()
    idx = qr.find_timestring(qb, b"midnight")
    s, e = idx, idx + len(b"midnight")
    fitted = qr.fit(qb.split(b" "), s, e)
    assert fitted is not None
    lay, fs = fitted
    assert fs >= qr.START_FONT_SIZE
    assert lay.paragraph_height < qr.HEIGHT - 100
    # fs+1 must NOT fit (either rejected or too tall) — largest-fit property
    bigger = qr.layout_pass(qb.split(b" "), fs + 1, s, e)
    assert bigger is None or bigger.paragraph_height >= qr.HEIGHT - 100


@needs_freetype
def test_layout_ops_reassemble_to_quote():
    quote = "It was ten—東京 o'clock. Nobody knew what the ten-four signal meant at midnight."
    qb = quote.encode()
    idx = qr.find_timestring(qb, b"ten")
    s, e = idx, idx + 3
    lay = qr.layout_pass(qb.split(b" "), 20, s, e)
    assert lay is not None
    assert b"".join(sb for _, _, sb, _ in lay.ops) == qb.replace(b" ", b"")
    assert any(bold for _, _, _, bold in lay.ops)


@needs_freetype
def test_render_quote_nostring_raises():
    with pytest.raises(qr.RenderError) as exc:
        qr.render_quote("no time words here", "midnight")
    assert exc.value.reason == "nostring"


@needs_freetype
def test_render_sentinel_font_size():
    # Golden sentinel pinned to the committed fonts + GD-exact measurement:
    # the first corpus quote (A Gentleman in Moscow) fits at exactly fs 32.
    # If this moves, measurement or fonts changed — investigate before
    # trusting any parity claim.
    quote = (
        "\"And almost exactly at midnight, the Count's patience was rewarded. "
        "For in accordance with the instructions he'd written to Richard, "
        'every telephone on the first floor of the Metropol began to ring."'
    )
    img, fs, _ = qr.render_quote(quote, "midnight")
    assert fs == 32
    assert img.size == (qr.WIDTH, qr.HEIGHT)


@needs_freetype
def test_add_credits_single_and_two_line():
    from PIL import Image, ImageOps

    short = Image.new("L", (qr.WIDTH, qr.HEIGHT), 255)
    qr.add_credits(short, "Dune", "Frank Herbert")
    short_bbox = ImageOps.invert(short).getbbox()
    assert short_bbox is not None
    long = Image.new("L", (qr.WIDTH, qr.HEIGHT), 255)
    qr.add_credits(long, "The Curious Incident of the Dog in the Night-Time and Other Long Titles", "Mark Haddon")
    long_bbox = ImageOps.invert(long).getbbox()
    # two-line block starts higher and is wider than the single-line one
    assert long_bbox[1] < short_bbox[1]
    # right-aligned: rightmost ink within a few px of WIDTH-MARGIN (glyph
    # side bearings); baseline sits at HEIGHT-MARGIN so the lowest ink
    # (italic descenders) lands within a few px below it
    assert qr.WIDTH - qr.MARGIN - 6 <= short_bbox[2] <= qr.WIDTH - qr.MARGIN + 1
    assert qr.HEIGHT - qr.MARGIN - 2 <= short_bbox[3] <= qr.HEIGHT - qr.MARGIN + 8


@needs_freetype
def test_credits_balance_failure_raises():
    from PIL import Image

    img = Image.new("L", (qr.WIDTH, qr.HEIGHT), 255)
    # wide (>500px) credits whose last word is nearly the whole string:
    # the PHP would read an unset array key and paint a bare dash — the
    # port fails loud instead (deliberate divergence, CI-enforced absent)
    with pytest.raises(qr.CreditsBalanceError):
        qr.add_credits(img, "X,", "W" * 60)


@needs_freetype
def test_caches_are_bounded():
    import gd_measure

    assert gd_measure.gd_text_width.cache_info().maxsize == 4096
    assert gd_measure.gd_bbox.cache_info().maxsize == 512
    # faces cache is keyed per font PATH: at most the 3 shipped faces
    qr.render_quote("It was midnight again somewhere in the world tonight.", "midnight")
    assert len(gd_measure._faces) <= 3


@needs_freetype
def test_render_row_exposes_layout(tmp_path):
    p = _write_corpus(tmp_path, "00:00|midnight|It was midnight already.|T1|A1\n")
    (row,) = list(qr.iter_corpus(p))
    quote_img, credits_img, font_size, layout = qr.render_row(row)
    assert layout.paragraph_height < qr.HEIGHT - 100
    assert font_size >= qr.START_FONT_SIZE
    assert quote_img.size == credits_img.size == (qr.WIDTH, qr.HEIGHT)


def test_iter_corpus_guard_scans_all_fields(tmp_path):
    # column-shift scenario: the backslash lands in the TITLE field — the
    # guard must still catch it (fgetcsv escape divergence, litclock-dev#536 review)
    p = _write_corpus(tmp_path, f"00:00|midnight|clean midnight quote|bad{BS}title|A\n")
    with pytest.raises(qr.CorpusGuardError):
        list(qr.iter_corpus(p))


def test_midword_edge_both():
    # match glued on BOTH sides -> 'both' (litclock-dev#542 review: uncovered branch)
    assert qr.timestring_midword_edge("xten9 o'clock", "ten") == "both"


needs_freetype_2 = needs_freetype  # alias for readability below


@needs_freetype_2
def test_layout_ops_bold_is_exact_span():
    """The bold ops must cover EXACTLY the matched bytes — asserted at the
    layout level, not just span slicing (litclock-dev#542 review)."""
    for quote, ts in [
        ("struck midnight. Then all was calm again", "midnight"),
        ("in the tenth hour of the long day", "ten"),
    ]:
        qb = quote.encode()
        idx = qr.find_timestring(qb, ts.encode())
        lay = qr.layout_pass(qb.split(b" "), 20, idx, idx + len(ts.encode()))
        bold_bytes = b"".join(sb for _, _, sb, bold in lay.ops if bold)
        assert bold_bytes == ts.encode(), (quote, bold_bytes)

class TestRowsForTimeParity:
    """litclock-dev#590: rows_for_time is served from quote_corpus's index, not
    its own iter_corpus walk. These pin that the rewiring changed the COST
    and nothing else."""

    def test_full_corpus_parity_with_iter_corpus(self):
        """Every one of the 1,440 buckets: rows_for_time == the iter_corpus
        filter it replaced, field-for-field (CorpusRow dataclass eq)."""
        csv_path = Path(__file__).resolve().parents[1] / "image-gen" / "litclock_annotated.csv"
        if not csv_path.exists():
            pytest.skip("bundled corpus CSV not present in this checkout")
        by_bucket: dict[str, list[qr.CorpusRow]] = {}
        for row in qr.iter_corpus(csv_path, strict=False):
            by_bucket.setdefault(row.hhmm, []).append(row)
        assert len(by_bucket) == 1440  # every minute of the day covered
        for hhmm, expected in by_bucket.items():
            assert qr.rows_for_time(csv_path, hhmm) == expected, hhmm

    def test_parity_on_hostile_synthetic_corpus(self, tmp_path):
        """Parity must hold on the shapes that are dormant in the shipped
        corpus: short rows (skipped), malformed times (no colon), NSFW
        counter sharing, escape sequences, and a non-contiguous bucket key
        (counter reset)."""
        csv_path = tmp_path / "hostile.csv"
        csv_path.write_text(
            "00:00|midnight|first|T|A|NO\n"
            "short|row\n"  # <5 fields: skipped, no ordinal
            "00:00|midnight|second \\n escaped|T|A|YES\n"
            "0001x|one|malformed time|T|A|NO\n"
            "00:02|two|other bucket|T|A|NO\n"
            "00:00|midnight|counter resets here|T|A|NO\n",
            encoding="utf-8",
        )
        buckets = {r.hhmm for r in qr.iter_corpus(csv_path, strict=False)}
        for hhmm in sorted(buckets):
            expected = [r for r in qr.iter_corpus(csv_path, strict=False) if r.hhmm == hhmm]
            assert qr.rows_for_time(csv_path, hhmm) == expected, hhmm

    def test_rows_for_time_misses_return_empty(self, tmp_path):
        """Empty bucket and missing file both yield [] (the PNG-glob-miss
        contract get_current_quote_runtime depends on)."""
        csv_path = tmp_path / "tiny.csv"
        csv_path.write_text("00:00|midnight|only row|T|A|NO\n", encoding="utf-8")
        assert qr.rows_for_time(csv_path, "1234") == []
        assert qr.rows_for_time(tmp_path / "absent.csv", "0000") == []
