"""litclock-dev#532 Phase A — what the painter shows when it cannot show a quote.

Pins the decisions from the litclock-dev#532 eng and design reviews:

- R7: every language but the PNG set's renders text, whatever the
  LITCLOCK_RUNTIME_RENDER flag and the validation marker say; a minute it
  cannot render paints the plain time, never the English PNG set.
- R9: an unusable corpus for that language is a failed minute, never a
  quietly borrowed English corpus.
- D8: the pick has THREE outcomes (quote / gap / failure), and the plain-time
  frame carries a reason line from the catalog.
- D2: the plain-time frame's layout (centred 160pt time, one 30pt line).
- R14: the status file counts consecutive failure frames; gaps never count.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

_HAS_DEPS = True
try:
    import PIL  # noqa: F401
    import qrcode  # noqa: F401
except ImportError:
    _HAS_DEPS = False

pytestmark = pytest.mark.skipif(not _HAS_DEPS, reason="literary_clock deps (PIL / qrcode) not in this interpreter")

try:
    import freetype  # noqa: F401

    _HAVE_FREETYPE = True
except ImportError:
    _HAVE_FREETYPE = False

if _HAS_DEPS:
    from PIL import Image, ImageDraw

    import literary_clock
    import quote_corpus
    import strings_catalog


XX_GAP_LINE = "Ningún libro para este minuto."
XX_FAILURE_LINE = "Los libros no están disponibles."


def _ink(img, box):
    """Bounding box of the black pixels inside ``box`` (panel coordinates), or None."""
    region = img.convert("L").crop(box).point(lambda v: 255 if v < 128 else 0)
    bb = region.getbbox()
    return None if bb is None else (bb[0] + box[0], bb[1] + box[1], bb[2] + box[0], bb[3] + box[1])


def _midnight(monkeypatch, hour=0, minute=0):
    class FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 1, hour, minute)

    monkeypatch.setattr(literary_clock, "datetime", FixedDT)


@pytest.fixture
def device(tmp_path, monkeypatch):
    """A checkout under tmp_path with English plus an ACTIVE second language
    `xx`: registry, both string bundles (xx = English's keys with its own two
    fallback lines) and an xx corpus with one row at 00:00. The language comes
    from the process env; the flag, the marker and the PNG set are absent."""
    (tmp_path / "languages" / "en").mkdir(parents=True)
    (tmp_path / "languages" / "xx").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "languages" / "en" / "strings.json", tmp_path / "languages" / "en" / "strings.json")
    en = json.loads((REPO_ROOT / "languages" / "en" / "strings.json").read_text(encoding="utf-8"))
    xx = {k: v for k, v in en.items() if not k.startswith("_")}
    xx["clock.fallback.gap"] = XX_GAP_LINE
    xx["clock.fallback.failure"] = XX_FAILURE_LINE
    (tmp_path / "languages" / "xx" / "strings.json").write_text(json.dumps(xx, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "corpora").mkdir()
    (tmp_path / "corpora" / "xx.csv").write_text(
        "00:00|medianoche|Era la medianoche en la casa tranquila.|Título|Autora\n", encoding="utf-8"
    )
    shutil.copy(REPO_ROOT / "image-gen" / "litclock_annotated.csv", tmp_path / "corpora" / "en.csv")

    def entry(code, corpus):
        return {
            "code": code,
            "native_name": code,
            "status": "active",
            "corpus": {"path": corpus, "rows": 1, "sfw_coverage_pct": 0.1},
            "strings": f"languages/{code}/strings.json",
            "plural_forms": ["one", "other"],
        }

    (tmp_path / "languages.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "fleet_default": "en",
                "languages": {"en": entry("en", "corpora/en.csv"), "xx": entry("xx", "corpora/xx.csv")},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", tmp_path / "languages.json")
    monkeypatch.setattr(strings_catalog, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(quote_corpus, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(quote_corpus, "_DEFAULT_CORPUS_PATH", tmp_path / "corpora" / "en.csv")
    monkeypatch.setattr(quote_corpus, "_CORPUS_PATH", None)
    # The PNG set lives under PROJECT_ROOT/images — absent here.
    monkeypatch.setattr(literary_clock, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(literary_clock, "RUNTIME_RENDER_DIR", str(tmp_path))
    monkeypatch.setattr(literary_clock, "RUNTIME_VALIDATED_MARKER", str(tmp_path / "no-marker"))
    monkeypatch.setattr(literary_clock, "STATUS_FILE", str(tmp_path / "current-quote.json"))
    monkeypatch.setattr(literary_clock, "UPDATE_FAILED_MARKER", str(tmp_path / "no-update-failed"))
    monkeypatch.setenv("LITCLOCK_ENV_FILE", str(tmp_path / "no-env.sh"))
    monkeypatch.setenv("LITCLOCK_LANGUAGE", "xx")
    monkeypatch.setenv("WEATHER_ENABLED", "false")
    monkeypatch.delenv("LITCLOCK_RUNTIME_RENDER", raising=False)
    monkeypatch.delenv("LITCLOCK_CORPUS_CSV", raising=False)
    strings_catalog.reset_cache()
    quote_corpus.reset_cache()
    _midnight(monkeypatch)
    yield tmp_path
    strings_catalog.reset_cache()
    quote_corpus.reset_cache()


def _png_tier_must_not_run(monkeypatch):
    def boom(**_kw):
        raise AssertionError("the English PNG tier was consulted on a non-English device")

    monkeypatch.setattr(literary_clock, "get_current_quote", boom)


def _runtime_meta(text="Era la medianoche."):
    frame = Image.new("1", (800, literary_clock.QUOTE_AREA_H), 1)
    ImageDraw.Draw(frame).rectangle((100, 100, 300, 200), fill=0)
    return {
        "quote": text,
        "author": "Autora",
        "title": "Título",
        "time": "00:00",
        "image_path": "",
        "picked_at": 0.0,
        "image": frame,
        "font_size": 30,
        "render_mode": "runtime",
    }


# ---------- R7: the tier rule ----------


class TestNonEnglishAlwaysRendersText:
    def test_it_renders_text_with_no_flag_and_no_marker(self, device, monkeypatch):
        calls = []

        def runtime(**kw):
            calls.append(kw)
            return _runtime_meta(), None

        monkeypatch.setattr(literary_clock, "get_current_quote_runtime", runtime)
        _png_tier_must_not_run(monkeypatch)
        _image, meta, _now, reason = literary_clock.main()
        assert len(calls) == 1, "the text renderer must be attempted on a non-English device"
        assert meta["render_mode"] == "runtime" and reason is None

    def test_the_flag_and_marker_are_not_even_consulted(self, device, monkeypatch):
        """A marker revoked by a proof-input release must not touch a
        non-English clock (R1/R7) — so the gate that reads it is not called."""

        def gate():
            raise AssertionError("_runtime_render_enabled consulted on a non-English device")

        monkeypatch.setattr(literary_clock, "_runtime_render_enabled", gate)
        monkeypatch.setattr(literary_clock, "get_current_quote_runtime", lambda **kw: (_runtime_meta(), None))
        _png_tier_must_not_run(monkeypatch)
        literary_clock.main()

    @pytest.mark.parametrize("reason", ["failure", "gap"])
    def test_a_miss_paints_the_plain_time_never_png(self, device, monkeypatch, reason):
        monkeypatch.setattr(literary_clock, "get_current_quote_runtime", lambda **kw: (None, reason))
        _png_tier_must_not_run(monkeypatch)
        image, meta, _now, got = literary_clock.main()
        assert meta is None and got == reason
        assert _ink(image, (0, 180, 800, 320)) is not None, "the time must be drawn"

    def test_english_still_uses_the_png_tier_after_a_runtime_miss(self, device, monkeypatch):
        """The control: the PNG set's own language keeps its PNG rung."""
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "en")
        monkeypatch.setattr(literary_clock, "_runtime_render_enabled", lambda: True)
        monkeypatch.setattr(literary_clock, "get_current_quote_runtime", lambda **kw: (None, "failure"))
        consulted = []
        monkeypatch.setattr(literary_clock, "get_current_quote", lambda **kw: consulted.append(1))
        _image, meta, _now, reason = literary_clock.main()
        assert consulted == [1]
        assert meta is None and reason == "failure", "a PNG miss after a runtime failure is still a failure"


class TestEnglishPngTierReasons:
    """With text rendering off, an empty glob is a gap only if the PNG set
    exists at all — a device whose images/ is gone is failing, not uncovered."""

    def _english(self, monkeypatch):
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "en")

    def test_a_glob_miss_with_the_set_on_disk_is_a_gap(self, device, monkeypatch):
        self._english(monkeypatch)
        (device / "images" / "metadata").mkdir(parents=True)
        (device / "images" / "metadata" / "quote_1200_0_credits.png").write_bytes(b"x")
        _image, meta, _now, reason = literary_clock.main()
        assert meta is None and reason == "gap"

    @pytest.mark.parametrize("state", ["absent", "empty"])
    def test_a_missing_png_set_is_a_failure(self, device, monkeypatch, state):
        self._english(monkeypatch)
        if state == "empty":
            (device / "images" / "metadata").mkdir(parents=True)
        _image, meta, _now, reason = literary_clock.main()
        assert meta is None and reason == "failure"

    def test_debris_in_the_png_directory_is_not_a_png_set(self, device, monkeypatch):
        """Review: any entry used to count as "the set is present"."""
        self._english(monkeypatch)
        (device / "images" / "metadata").mkdir(parents=True)
        (device / "images" / "metadata" / ".DS_Store").write_bytes(b"x")
        (device / "images" / "metadata" / "tmp123").write_bytes(b"x")
        _image, meta, _now, reason = literary_clock.main()
        assert meta is None and reason == "failure"

    @pytest.mark.parametrize("runtime_reason", ["gap", "failure"])
    def test_the_text_tier_keeps_its_own_reason_past_a_png_miss(self, device, monkeypatch, runtime_reason):
        """The fleet's common path (English on the text tier since Stage B):
        a runtime miss, then a PNG glob miss WITH the set on disk. The
        runtime's reason must survive — a re-derivation would turn every
        uncovered minute into a failure, or hide real failures as gaps."""
        self._english(monkeypatch)
        (device / "images" / "metadata").mkdir(parents=True)
        (device / "images" / "metadata" / "quote_1200_0_credits.png").write_bytes(b"x")
        monkeypatch.setattr(literary_clock, "_runtime_render_enabled", lambda: True)
        monkeypatch.setattr(literary_clock, "get_current_quote_runtime", lambda **kw: (None, runtime_reason))
        monkeypatch.setattr(literary_clock, "get_current_quote", lambda **kw: None)
        _image, meta, _now, reason = literary_clock.main()
        assert meta is None and reason == runtime_reason

    def test_a_png_hit_after_a_runtime_failure_carries_no_reason(self, device, monkeypatch):
        self._english(monkeypatch)
        png = device / "fallback.png"
        Image.new("1", (800, 400), 1).save(png)
        monkeypatch.setattr(literary_clock, "_runtime_render_enabled", lambda: True)
        monkeypatch.setattr(literary_clock, "get_current_quote_runtime", lambda **kw: (None, "failure"))
        monkeypatch.setattr(
            literary_clock,
            "get_current_quote",
            lambda **kw: {"quote": "q", "author": "a", "title": "t", "time": "00:00", "image_path": str(png),
                          "render_mode": "image"},
        )
        _image, meta, _now, reason = literary_clock.main()
        assert meta["render_mode"] == "image-fallback" and reason is None

    def test_a_corrupt_png_is_a_failure(self, device, monkeypatch):
        self._english(monkeypatch)
        bad = device / "bad.png"
        bad.write_bytes(b"not a png")
        monkeypatch.setattr(
            literary_clock,
            "get_current_quote",
            lambda **kw: {"quote": "q", "author": "a", "title": "t", "time": "00:00", "image_path": str(bad)},
        )
        _image, meta, _now, reason = literary_clock.main()
        assert meta is None and reason == "failure"


@pytest.mark.skipif(not _HAVE_FREETYPE, reason="freetype-py required for the real renderer")
class TestNonEnglishEndToEnd:
    """The real renderer and the real corpus resolution, no stubs on the pick."""

    def test_the_dry_run_line_says_why_there_is_no_quote(self, device, monkeypatch, capsys):
        """The self-test reads this line; a gap and a failure must differ."""
        _midnight(monkeypatch, 3, 7)
        monkeypatch.setattr(sys, "argv", ["literary_clock.py", "--dry-run"])
        monkeypatch.setattr(literary_clock, "main", lambda: (Image.new("1", (800, 480), 1), None, None, "gap"))
        with pytest.raises(SystemExit) as exc:
            exec(compile(_main_block_source(), "<main>", "exec"), vars(literary_clock) | {"__name__": "__main__"})
        assert exc.value.code == 0
        assert "render_mode=time-only (gap)" in capsys.readouterr().out

    def test_its_own_corpus_renders(self, device, monkeypatch):
        _png_tier_must_not_run(monkeypatch)
        _image, meta, _now, reason = literary_clock.main()
        assert reason is None
        assert meta["render_mode"] == "runtime"
        assert meta["quote"] == "Era la medianoche en la casa tranquila."

    def test_an_uncovered_minute_is_a_gap(self, device, monkeypatch):
        _midnight(monkeypatch, 3, 7)
        _png_tier_must_not_run(monkeypatch)
        _image, meta, _now, reason = literary_clock.main()
        assert meta is None and reason == "gap"

    @pytest.mark.parametrize("damage", ["missing", "empty", "junk", "header-only", "unpadded-times"])
    def test_an_unusable_corpus_is_a_failure_never_english(self, device, monkeypatch, damage):
        """R9: pre-fix every one of these read the English corpus (a real row
        exists at 00:00 in it) and painted an English quote."""
        corpus = device / "corpora" / "xx.csv"
        if damage == "missing":
            corpus.unlink()
        elif damage == "empty":
            corpus.write_text("", encoding="utf-8")
        elif damage == "junk":
            corpus.write_text("junk\n", encoding="utf-8")
        elif damage == "header-only":
            corpus.write_text("time|timestring|quote|title|author\n", encoding="utf-8")
        else:  # rows present, but "0:00" keys name no real minute
            corpus.write_text("0:00|medianoche|Era la medianoche.|T|A\n", encoding="utf-8")
        _png_tier_must_not_run(monkeypatch)
        _image, meta, _now, reason = literary_clock.main()
        assert meta is None and reason == "failure"


def _main_block_source():
    import ast

    tree = ast.parse((REPO_ROOT / "src" / "literary_clock.py").read_text(encoding="utf-8"))
    block = next(n for n in tree.body if isinstance(n, ast.If) and ast.unparse(n.test).startswith("__name__"))
    return "\n".join(ast.unparse(st) for st in block.body)


# ---------- D2/D8: the plain-time frame ----------


class TestPlainTimeFrame:
    TIME_BAND = (0, 150, 800, 340)
    LINE_BAND = (0, 350, 800, 420)

    def _frame(self, reason, now=datetime(2026, 9, 30, 12, 4)):
        img = Image.new("1", (800, 480), 1)
        literary_clock._draw_time_fallback(ImageDraw.Draw(img), now, reason)
        return img

    def test_the_time_matches_the_approved_mockup(self, device):
        """The approved plain-time mockup (litclock-dev#532 D2): "12:04" ink spans
        x 211..590, y 189..310 — centred on the panel's middle column."""
        assert _ink(self._frame("gap"), self.TIME_BAND) == (211, 189, 590, 310)

    def test_the_time_stays_put_across_minutes(self, device):
        """Baseline anchoring: the digits' row does not jump with the glyphs."""
        a = _ink(self._frame("gap", datetime(2026, 1, 1, 11, 11)), self.TIME_BAND)
        b = _ink(self._frame("gap", datetime(2026, 1, 1, 20, 58)), self.TIME_BAND)
        # Round glyphs overshoot the baseline by a pixel; an ink-centred
        # layout would move by tens.
        assert abs(a[3] - 310) <= 1 and abs(b[3] - 310) <= 1

    @pytest.mark.parametrize(
        ("reason", "line"), [("gap", XX_GAP_LINE), ("failure", XX_FAILURE_LINE)], ids=["gap", "failure"]
    )
    def test_each_reason_draws_its_own_line_in_the_device_language(self, device, monkeypatch, reason, line):
        drawn = []
        real_text = ImageDraw.ImageDraw.text

        def spy(self, xy, text, *a, **k):
            drawn.append(text)
            return real_text(self, xy, text, *a, **k)

        monkeypatch.setattr(ImageDraw.ImageDraw, "text", spy)
        img = self._frame(reason)
        assert drawn[-1] == line
        box = _ink(img, self.LINE_BAND)
        assert box is not None and box[1] >= 360 and box[3] <= 410
        assert abs((box[0] + box[2]) / 2 - 400) <= 3, "the line is centred"

    def test_the_english_lines_are_the_approved_copy(self, device, monkeypatch):
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "en")
        assert literary_clock._fallback_line("gap") == "No book quote for this minute yet."
        assert literary_clock._fallback_line("failure") == "Book quotes are unavailable right now."
        # Mockup ink rows for the English gap line: 370..400.
        box = _ink(self._frame("gap"), self.LINE_BAND)
        assert (box[1], box[3]) == (370, 400)

    def test_a_missing_key_omits_the_line_never_draws_the_key(self, device, monkeypatch):
        monkeypatch.setattr(literary_clock.strings_catalog, "get_own", lambda key, **s: None)
        img = self._frame("gap")
        assert _ink(img, self.TIME_BAND) is not None
        assert _ink(img, self.LINE_BAND) is None

    def test_a_bundle_without_the_key_never_borrows_the_english_line(self, device, monkeypatch):
        """D9 + review: the line must come from the device language's own
        bundle or not at all — get() serves the fleet default WHOLE for an
        incomplete bundle (RT1), which must not reach the glass as this
        language's line. Real catalog, no stub."""
        bundle = device / "languages" / "xx" / "strings.json"
        data = json.loads(bundle.read_text(encoding="utf-8"))
        del data["clock.fallback.gap"]
        bundle.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        strings_catalog.reset_cache()
        assert strings_catalog.get("clock.fallback.gap") == "No book quote for this minute yet."  # the trap
        assert literary_clock._fallback_line("gap") is None
        assert _ink(self._frame("gap"), self.LINE_BAND) is None
        # Since RT1 the missing key makes the WHOLE bundle unusable, so every
        # optional line is dropped (R15), not just the one that is missing.
        assert literary_clock._fallback_line("failure") is None
        # Control: repaired, the same frame draws its line again.
        data["clock.fallback.gap"] = XX_GAP_LINE
        bundle.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        strings_catalog.reset_cache()
        assert literary_clock._fallback_line("gap") == XX_GAP_LINE

    def test_a_raising_catalog_omits_the_line_and_keeps_the_time(self, device, monkeypatch):
        def boom(key, **s):
            raise RuntimeError("bundle exploded")

        monkeypatch.setattr(literary_clock.strings_catalog, "get_own", boom)
        img = self._frame("failure")
        assert _ink(img, self.TIME_BAND) is not None
        assert _ink(img, self.LINE_BAND) is None

    def test_a_hand_wrapped_translation_is_drawn_on_one_line(self, device, monkeypatch):
        """Review: the catalog lint allows newlines and Pillow cannot measure
        multiline text (ValueError) — that used to escape main()."""
        wrapped = "Ningún libro\npara este minuto."
        monkeypatch.setattr(literary_clock.strings_catalog, "get_own", lambda key, **s: wrapped)
        box = _ink(self._frame("gap"), self.LINE_BAND)
        assert box is not None and box[3] - box[1] < 40, "one line, not two"

    def test_french_spacing_survives_the_one_line_rule(self, device, monkeypatch):
        """Round-2 review: only line breakers are folded, not NBSP/U+202F."""
        text = "Pas de citation\u202f! \r\n Vraiment"
        monkeypatch.setattr(literary_clock.strings_catalog, "get_own", lambda key, **s: text)
        assert literary_clock._fallback_line("gap") == "Pas de citation\u202f! Vraiment"

    def test_a_missing_line_font_costs_the_line_not_the_time(self, device, monkeypatch, caplog):
        """Review: the credits font is also what the runtime tier needs, so
        its loss is exactly when this frame is painted."""
        monkeypatch.setattr(literary_clock, "FALLBACK_LINE_FONT_PATH", str(device / "no-such-font.ttf"))
        with caplog.at_level(logging.WARNING):
            img = self._frame("failure")
        assert _ink(img, self.TIME_BAND) is not None
        assert _ink(img, self.LINE_BAND) is None
        assert any("line not drawn" in r.getMessage() for r in caplog.records)

    def test_a_long_line_shrinks_to_fit(self, device, monkeypatch):
        long = "Ningún libro tiene una cita para este minuto, vuelve más tarde."  # 880px at 30pt
        monkeypatch.setattr(literary_clock.strings_catalog, "get_own", lambda key, **s: long)
        box = _ink(self._frame("gap"), self.LINE_BAND)
        assert box is not None
        assert box[0] >= literary_clock.FALLBACK_LINE_MARGIN_X - 2
        assert box[2] <= 800 - literary_clock.FALLBACK_LINE_MARGIN_X + 2
        assert box[2] - box[0] > 600, "shrunk only as far as needed, not to the floor"

    def test_a_line_too_long_even_at_the_floor_is_omitted_with_a_warning(self, device, monkeypatch, caplog):
        monkeypatch.setattr(literary_clock.strings_catalog, "get_own", lambda key, **s: "palabra " * 40)
        with caplog.at_level(logging.WARNING):
            img = self._frame("gap")
        assert _ink(img, self.LINE_BAND) is None
        assert any("too wide for the panel" in r.getMessage() for r in caplog.records)


# ---------- review: the language boundary under a damaged registry ----------


class TestDamagedRegistry:
    def _registry(self, device, text):
        (device / "languages.json").write_text(text, encoding="utf-8")
        strings_catalog.reset_cache()

    @pytest.mark.parametrize("text", ["{not json", '{"languages": ["en", "xx"]}', '{"languages": "xx"}'],
                             ids=["unparseable", "list", "string"])
    def test_a_non_english_device_never_falls_back_to_english(self, device, monkeypatch, text):
        """Pre-review: an unreadable registry made active_language() say "en"
        for this xx device — English PNGs and the English corpus again — and
        a wrong-shaped one raised AttributeError out of main()."""
        self._registry(device, text)
        _png_tier_must_not_run(monkeypatch)
        seen = []
        monkeypatch.setattr(
            literary_clock, "get_current_quote_runtime", lambda **kw: seen.append(1) or (None, "failure")
        )
        _image, meta, _now, reason = literary_clock.main()
        assert seen == [1] and meta is None and reason == "failure"
        with pytest.raises(quote_corpus.CorpusUnavailable):
            quote_corpus.corpus_path()

    def test_an_english_device_keeps_its_png_tier(self, device, monkeypatch):
        """The control: the same damage on the fleet default's own device."""
        self._registry(device, "{not json")
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "en")
        consulted = []
        monkeypatch.setattr(literary_clock, "get_current_quote", lambda **kw: consulted.append(1))
        literary_clock.main()
        assert consulted == [1]

    def test_a_readable_registry_that_rejects_the_code_is_english(self, device, monkeypatch):
        """An inactive code with the registry READABLE is the device-level
        rule (UI and panel English) — not the damaged case above."""
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "zz")
        consulted = []
        monkeypatch.setattr(literary_clock, "get_current_quote", lambda **kw: consulted.append(1))
        literary_clock.main()
        assert consulted == [1]

    def test_a_language_resolution_that_raises_paints_a_failure(self, device, monkeypatch):
        """Neither tier: not the English PNGs, and not the text renderer with
        English's validation gate skipped (round-2 review)."""

        def boom():
            raise RuntimeError("registry exploded")

        def no_runtime(**kw):
            raise AssertionError("the text tier was attempted without a known language")

        monkeypatch.setattr(literary_clock.strings_catalog, "quote_language", boom)
        monkeypatch.setattr(literary_clock, "get_current_quote_runtime", no_runtime)
        _png_tier_must_not_run(monkeypatch)
        _image, meta, _now, reason = literary_clock.main()
        assert meta is None and reason == "failure"


@pytest.mark.skipif(not _HAVE_FREETYPE, reason="freetype-py required for the real renderer")
def test_a_renderer_that_fails_to_import_is_a_failure_not_a_crash(device, monkeypatch):
    """Review: only ImportError was caught; a truncated module raises
    SyntaxError at import and used to escape main() on a non-English device."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "quote_renderer":
            raise SyntaxError("truncated module")
        return real_import(name, *a, **k)

    monkeypatch.delitem(sys.modules, "quote_renderer", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert literary_clock.get_current_quote_runtime(now=datetime(2026, 1, 1, 0, 0)) == (None, "failure")


# ---------- R14: the failure count in the status file ----------


class TestConsecutiveFailures:
    QUOTE = {"quote": "q", "author": "a", "title": "t", "image_path": "/x.png", "render_mode": "runtime"}

    @pytest.fixture(autouse=True)
    def _clock(self):
        self.minute = 0

    def _write(self, meta, reason=None, *, same_minute=False):
        """One published frame per call, each a NEW minute unless asked."""
        if not same_minute:
            self.minute += 1
        literary_clock._write_status_file(meta, datetime(2026, 1, 1, 12, self.minute), reason)
        return json.loads(Path(literary_clock.STATUS_FILE).read_text(encoding="utf-8"))

    def test_failures_count_up(self, device):
        got = [self._write(None, "failure") for _ in range(3)]
        assert [p["consecutive_failures"] for p in got] == [1, 2, 3]
        assert all(p["fallback_reason"] == "failure" and p["render_mode"] == "time-only" for p in got)

    def test_a_gap_neither_counts_nor_resets(self, device):
        """Review (red team + adversarial): resetting on a gap let a partly
        covered language with a broken renderer keep the warning off."""
        self._write(None, "failure")
        p = self._write(None, "gap")
        assert p["consecutive_failures"] == 1 and p["fallback_reason"] == "gap"
        assert self._write(None, "gap")["consecutive_failures"] == 1
        assert self._write(None, "failure")["consecutive_failures"] == 2
        assert self._write(None, "failure")["consecutive_failures"] == 3

    def test_gaps_alone_never_count(self, device):
        assert [self._write(None, "gap")["consecutive_failures"] for _ in range(5)] == [0] * 5

    def test_a_repaint_of_the_same_minute_does_not_count_again(self, device):
        """An update's timer restart or a Settings save repaints the current
        minute; three frames must mean three minutes."""
        self._write(None, "failure")
        assert self._write(None, "failure", same_minute=True)["consecutive_failures"] == 1
        assert self._write(None, "failure")["consecutive_failures"] == 2

    def test_a_quote_frame_resets_and_carries_no_reason(self, device):
        for _ in range(4):
            self._write(None, "failure")
        p = self._write(self.QUOTE, "failure")  # a reason beside a quote is ignored
        assert p["consecutive_failures"] == 0
        assert "fallback_reason" not in p
        assert p["render_mode"] == "runtime"
        assert self._write(None, "failure")["consecutive_failures"] == 1

    def test_an_unstated_reason_is_a_failure_not_a_claimed_gap(self, device):
        p = self._write(None)
        assert p["fallback_reason"] == "failure" and p["consecutive_failures"] == 1

    @pytest.mark.parametrize(
        "previous",
        [
            "{not json",
            "[]",
            '"x"',
            '{"consecutive_failures": "7"}',
            '{"consecutive_failures": true}',
            '{"consecutive_failures": -4}',
            '{"consecutive_failures": 2.5}',
        ],
        ids=["corrupt", "list", "string", "str-count", "bool-count", "negative", "float"],
    )
    def test_a_bad_previous_file_counts_from_zero(self, device, previous):
        Path(literary_clock.STATUS_FILE).write_text(previous, encoding="utf-8")
        assert self._write(None, "failure")["consecutive_failures"] == 1

    def test_a_missing_previous_file_counts_from_zero(self, device):
        assert not Path(literary_clock.STATUS_FILE).exists()
        assert self._write(None, "failure")["consecutive_failures"] == 1


def test_the_pwa_threshold_matches_the_painter():
    """The control server does not import the painter; the two constants are
    one number."""
    from control_server.routes import status

    assert status.QUOTE_FAILURE_WARN_AT == literary_clock.CONSECUTIVE_FAILURES_WARN_AT == 3


# ---------- R10: the weekly self-test samples a covered minute ----------


class TestSampleMinute:
    def _corpus(self, device, body):
        (device / "corpora" / "xx.csv").write_text(body, encoding="utf-8")
        quote_corpus.reset_cache()

    def test_the_next_covered_minute_wrapping_past_midnight(self, device):
        """The fixture's only row is 00:00; from 23:30 the sample is tomorrow's."""
        got = literary_clock._sample_minute(datetime(2026, 1, 1, 23, 30, 56))
        assert got == datetime(2026, 1, 2, 0, 0)

    def test_the_current_minute_when_it_is_covered(self, device):
        assert literary_clock._sample_minute(datetime(2026, 1, 1, 0, 0, 30)) == datetime(2026, 1, 1, 0, 0)

    def test_a_mature_only_minute_is_skipped(self, device):
        self._corpus(device, "00:00|m|Mature only.|T|A|YES\n00:05|m|Safe one.|T|A\n")
        assert literary_clock._sample_minute(datetime(2026, 1, 1, 0, 0)) == datetime(2026, 1, 1, 0, 5)

    @pytest.mark.parametrize("body", ["", "00:00|m|Mature only.|T|A|YES\n"], ids=["empty", "all-mature"])
    def test_nothing_to_sample_is_none(self, device, body):
        self._corpus(device, body)
        assert literary_clock._sample_minute(datetime(2026, 1, 1, 12, 0)) is None

    def test_an_unreadable_registry_is_none_never_english(self, device):
        (device / "languages.json").write_text("{nope", encoding="utf-8")
        strings_catalog.reset_cache()
        assert literary_clock._sample_minute(datetime(2026, 1, 1, 12, 0)) is None

    def test_an_indexer_bug_is_not_reported_as_no_corpus(self, device, monkeypatch):
        """Review: only corpus-shaped errors mean "nothing to sample"; anything
        else must reach the dry-run's exit 1 (selftest-failed)."""

        def boom():
            raise KeyError("indexer bug")

        monkeypatch.setattr(quote_corpus, "device_index", boom)
        with pytest.raises(KeyError):
            literary_clock._sample_minute(datetime(2026, 1, 1, 12, 0))

    def test_english_samples_the_current_minute(self, device, monkeypatch):
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "en")
        assert literary_clock._sample_minute(datetime(2026, 1, 1, 14, 37)) == datetime(2026, 1, 1, 14, 37)


class TestSampleQuoteDryRun:
    def _run_block(self, monkeypatch, argv, main):
        monkeypatch.setattr(sys, "argv", ["literary_clock.py", *argv])
        monkeypatch.setattr(literary_clock, "main", main)
        with pytest.raises(SystemExit) as exc:
            exec(compile(_main_block_source(), "<main>", "exec"), vars(literary_clock) | {"__name__": "__main__"})
        return exc.value.code

    def test_it_renders_the_sampled_minute(self, device, monkeypatch, capsys):
        _midnight(monkeypatch, 23, 30)
        seen = []

        def main(at=None):
            seen.append(at)
            return Image.new("1", (800, 480), 1), {"render_mode": "runtime"}, at, None

        code = self._run_block(monkeypatch, ["--dry-run", "--require-runtime-render", "--sample-quote"], main)
        assert code == 0
        assert seen == [datetime(2026, 1, 2, 0, 0)]
        assert "sampling 00:00" in capsys.readouterr().out

    def test_nothing_to_sample_exits_4_without_rendering(self, device, monkeypatch, capsys):
        (device / "corpora" / "xx.csv").write_text("", encoding="utf-8")

        def main(at=None):
            raise AssertionError("rendered although there was nothing to sample")

        code = self._run_block(monkeypatch, ["--dry-run", "--sample-quote"], main)
        assert code == literary_clock.EXIT_NO_SAMPLE == 4
        assert "no quote to sample" in capsys.readouterr().out

    def test_without_the_flag_the_current_minute_is_rendered(self, device, monkeypatch):
        seen = []

        def main(at=None):
            seen.append(at)
            return Image.new("1", (800, 480), 1), {"render_mode": "runtime"}, None, None

        assert self._run_block(monkeypatch, ["--dry-run"], main) == 0
        assert seen == [None]

    @pytest.mark.skipif(not _HAVE_FREETYPE, reason="freetype-py required for the real renderer")
    def test_end_to_end_an_uncovered_minute_still_passes(self, device, monkeypatch, capsys):
        """The point of R10: at 03:07 the xx corpus has nothing, the current
        minute would be a plain-time frame and exit 3; the sample renders."""
        _midnight(monkeypatch, 3, 7)
        argv = ["literary_clock.py", "--dry-run", "--require-runtime-render", "--sample-quote"]
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit) as exc:
            exec(compile(_main_block_source(), "<main>", "exec"), vars(literary_clock) | {"__name__": "__main__"})
        out = capsys.readouterr().out
        assert exc.value.code == 0, out
        assert "render_mode=runtime" in out and "sampling 00:00" in out


def test_the_painted_masthead_date_comes_from_the_device_language(device, monkeypatch):
    """litclock-dev#532 D3: the frame's date is the catalog's, not strftime's
    — on an xx device it is xx's pattern and names."""
    bundle_path = device / "languages" / "xx" / "strings.json"
    xx = json.loads(bundle_path.read_text(encoding="utf-8"))
    xx["clock.masthead.date"] = "{day} {month} ({weekday})"
    xx["clock.month.jan"] = "enero"
    xx["clock.weekday.thu"] = "jue."
    xx["clock.day.1"] = "1"
    bundle_path.write_text(json.dumps(xx, ensure_ascii=False), encoding="utf-8")
    strings_catalog.reset_cache()
    drawn = []
    real = literary_clock._compose_masthead
    def spy(image, draw, date_text, *rest):
        drawn.append(date_text)
        return real(image, draw, date_text, *rest)

    monkeypatch.setattr(literary_clock, "_compose_masthead", spy)
    literary_clock.main()
    assert drawn == ["1 enero (jue.)"]  # 2026-01-01 was a Thursday
