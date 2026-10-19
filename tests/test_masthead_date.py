"""litclock-dev#532 D3/DT2: the masthead date is the clock's language.

The painter drew ``now.strftime("%a, %B %d")`` — English names, and only
because the service runs in the C locale. The format and the day and month
names now come from the catalog (``clock.masthead.date``, ``clock.weekday.*``,
``clock.month.*``, ``clock.day.*``), and every registered language is held to the space the
masthead has: every weekday x month x day name at the painter's 48pt must INK
within ``MASTHEAD_DATE_BUDGET`` (443px: to 8px short of the QR's quiet-zone
carve) and clear the rule below it, English included. A bundle that skipped CI
is shrunk, then cut, at runtime rather than drawn into the QR.

R8: English is unchanged — the same text for every day of a leap year, drawn
by the same call.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont, ImageOps

import literary_clock as lc
import strings_catalog

REPO = Path(__file__).resolve().parent.parent
EN = json.loads((REPO / "languages" / "en" / "strings.json").read_text(encoding="utf-8"))
EN_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
EN_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip


@pytest.fixture(autouse=True)
def _english(monkeypatch, tmp_path):
    monkeypatch.setenv("LITCLOCK_ENV_FILE", str(tmp_path / "no-env.sh"))
    monkeypatch.setenv("LITCLOCK_LANGUAGE", "en")
    strings_catalog.reset_cache()
    yield
    strings_catalog.reset_cache()


def _registry(tmp_path, monkeypatch, xx: dict[str, str]):
    for code, data in (("en", EN), ("xx", xx)):
        target = tmp_path / "languages" / code / "strings.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    registry = json.loads((REPO / "languages.json").read_text(encoding="utf-8"))
    registry["languages"]["en"]["strings"] = "languages/en/strings.json"
    registry["languages"]["xx"] = dict(
        registry["languages"]["en"], code="xx", native_name="Xx", strings="languages/xx/strings.json"
    )
    (tmp_path / "languages.json").write_text(json.dumps(registry), encoding="utf-8")
    monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", tmp_path / "languages.json")
    monkeypatch.setattr(strings_catalog, "_REPO_ROOT", tmp_path)
    monkeypatch.setenv("LITCLOCK_LANGUAGE", "xx")
    strings_catalog.reset_cache()


def _spanish_like() -> dict[str, str]:
    """D3's approved Spanish shape: "mié., 30 de sept." — unpadded day."""
    bundle = dict(EN)
    bundle["clock.masthead.date"] = "{weekday}, {day} de {month}"
    for key, day in zip(lc._DAY_KEYS, range(1, 32), strict=True):
        bundle[key] = str(day)
    for key, name in zip(lc._WEEKDAY_KEYS, ("lun.", "mar.", "mié.", "jue.", "vie.", "sáb.", "dom."), strict=True):
        bundle[key] = name
    months = ("ene.", "feb.", "mar.", "abr.", "may.", "jun.", "jul.", "ago.", "sept.", "oct.", "nov.", "dic.")
    for key, name in zip(lc._MONTH_KEYS, months, strict=True):
        bundle[key] = name
    return bundle


# ── R8: English, every day of a leap year ──────────────────────────────────


def test_english_is_the_old_strftime_text_every_day():
    day = datetime(2028, 1, 1, 12, 0)
    for _ in range(366):
        want = f"{EN_WEEKDAYS[day.weekday()]}, {EN_MONTHS[day.month - 1]} {day.day:02d}"
        assert lc.masthead_date(day) == want
        day += timedelta(days=1)
    # And it is what strftime said in the locale the service runs in.
    assert lc.masthead_date(datetime(2026, 9, 30)) == "Wed, September 30"
    assert lc.masthead_date(datetime(2026, 3, 5)) == "Thu, March 05"


def test_english_is_drawn_by_the_same_call_as_before(monkeypatch):
    """Fits at 48pt, so the original top-left draw — not the backstop."""
    calls = []
    real = ImageDraw.ImageDraw.text

    def spy(self, xy, text, *a, **k):
        calls.append((xy, text, k.get("anchor")))
        return real(self, xy, text, *a, **k)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", spy)
    image = Image.new("1", lc.DISPLAY_SIZE, 255)
    lc._compose_masthead(image, ImageDraw.Draw(image), "Wed, September 30", None, None, None)
    mh = lc._masthead_metrics()
    assert calls == [((lc.DATE_X, mh["date_y"]), "Wed, September 30", None)]


# ── D3's CI gate: every language fits, registered or not ───────────────────────────


def _overflows(bundle: dict[str, str]) -> list[tuple[str, float]]:
    """Every (date, ink width) that inks past the budget, or (date, -depth)
    that inks onto the rule, over weekday x month x day. Ink, not advance
    width: the quiet-zone carve erases ink, and a final 'c' or 'w' inks past
    its advance (litclock-dev#919 review)."""
    font = ImageFont.truetype(lc.FONT_PATH, lc.DATE_FONT_SIZE)
    draw = ImageDraw.Draw(Image.new("1", (4, 4)))
    baseline = lc._masthead_metrics()["baseline"]
    fmt = bundle["clock.masthead.date"]
    widest: dict[str, float] = {}
    for weekday in lc._WEEKDAY_KEYS:
        for month in lc._MONTH_KEYS:
            for day in lc._DAY_KEYS:
                text = strings_catalog._fill(
                    fmt, {"weekday": bundle[weekday], "month": bundle[month], "day": bundle[day]}
                )
                left, _top, right, bottom = draw.textbbox((0, baseline), text, font=font, anchor="ls")
                widest[text] = (right, bottom)
    over = [(t, r) for t, (r, _b) in widest.items() if r > lc.MASTHEAD_DATE_BUDGET]
    over += [(t, -b) for t, (_r, b) in widest.items() if b > lc.RULE_TOP - 1]
    return sorted(over, key=lambda tw: -abs(tw[1]))


def _registered_bundles():
    registry = json.loads((REPO / "languages.json").read_text(encoding="utf-8"))
    for code, entry in sorted(registry["languages"].items()):
        path = REPO / entry.get("strings", f"languages/{code}/strings.json")
        # A registered language with no bundle is a failure, not a skip: a
        # skip would let it ship unmeasured (litclock-dev#919 review).
        yield code, json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    # And every bundle in the tree the registry does not list yet (litclock-dev#532 T8,
    # litclock-dev#926 review): the validator promises "green here = green there", and an
    # unregistered bundle would otherwise merge unmeasured.
    for path in sorted((REPO / "languages").glob("*/strings.json")):
        if path.parent.name not in registry["languages"]:
            yield path.parent.name, json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(("code", "bundle"), list(_registered_bundles()), ids=lambda v: v if isinstance(v, str) else "")
def test_every_language_fits_the_masthead(code, bundle):
    if "clock.masthead.date" not in bundle:
        pytest.fail(f"{code}: no clock.masthead.date — a language must define its date format")
    over = _overflows(bundle)
    assert not over, (
        f"{code}: {len(over)} dates ink past {lc.MASTHEAD_DATE_BUDGET}px or onto the rule, e.g. {over[:3]}"
    )


def test_the_gate_sees_a_registry_with_english_in_it():
    assert "en" in dict(_registered_bundles())


def test_the_gate_also_measures_a_bundle_the_registry_does_not_list(tmp_path, monkeypatch):
    """litclock-dev#532 T8 (litclock-dev#926 review): an unregistered bundle must not merge unmeasured."""
    (tmp_path / "languages" / "en").mkdir(parents=True)
    (tmp_path / "languages" / "en" / "strings.json").write_text("{}", encoding="utf-8")
    (tmp_path / "languages" / "es").mkdir()
    (tmp_path / "languages" / "es" / "strings.json").write_text('{"clock.masthead.date": "x"}', encoding="utf-8")
    (tmp_path / "languages.json").write_text(
        json.dumps({"languages": {"en": {"strings": "languages/en/strings.json"}}}), encoding="utf-8"
    )
    monkeypatch.setattr(sys.modules[__name__], "REPO", tmp_path)
    assert dict(_registered_bundles()) == {"en": {}, "es": {"clock.masthead.date": "x"}}


def test_the_budget_ends_short_of_the_quiet_zone_carve():
    """The carve _composite_settings_qr whites out starts at the QR's left
    edge less its quiet zone (701), not at the QR (713)."""
    assert lc.MASTHEAD_DATE_RIGHT == lc.QR_POSITION[0] - lc.QR_QUIET_ZONE - 8 == 693
    assert lc.MASTHEAD_DATE_BUDGET == lc.MASTHEAD_DATE_RIGHT - lc.DATE_X == 443


def test_english_widest_date_inks_438px():
    """The measured margin (5px), so a font or size change that eats it is
    a visible change here before it is an overflow. D3 quoted "Wed, September
    30" at 432px of advance; over the whole weekday x month x day product the
    widest INK is "Mon, September 0x" at 438px (digits differ in width)."""
    font = ImageFont.truetype(lc.FONT_PATH, lc.DATE_FONT_SIZE)
    draw = ImageDraw.Draw(Image.new("1", (4, 4)))
    widest = max(
        draw.textbbox((0, 0), f"{w}, {m} {d:02d}", font=font)[2]
        for w in EN_WEEKDAYS
        for m in EN_MONTHS
        for d in range(1, 32)
    )
    assert widest == 438


def test_the_gate_measures_ink_not_advance():
    """A string whose advance fits but whose final Q inks past the budget."""
    font = ImageFont.truetype(lc.FONT_PATH, lc.DATE_FONT_SIZE)
    draw = ImageDraw.Draw(Image.new("1", (4, 4)))
    text = "Mercredi 30 iiiiiiiiQ"
    assert draw.textlength(text, font=font) <= lc.MASTHEAD_DATE_BUDGET < draw.textbbox((0, 0), text, font=font)[2]
    bundle = dict(EN, **{"clock.masthead.date": text})
    assert _overflows(bundle)


def test_the_gate_fails_a_glyph_that_reaches_the_rule(monkeypatch):
    """Depth is checked as well as width. No Literata glyph reaches the rule
    at 48pt today ("ļ" stops exactly at the last allowed row), so the rule is
    raised 2px here to prove the check can fail."""
    bundle = dict(EN, **{"clock.masthead.date": "{weekday} ļ"})
    assert not _overflows(bundle)
    monkeypatch.setattr(lc, "RULE_TOP", lc.RULE_TOP - 2)
    over = _overflows(bundle)
    assert over and all(depth < 0 for _t, depth in over)


def test_the_gate_fails_a_planted_overlong_format():
    bundle = dict(EN, **{"clock.masthead.date": "{weekday}, the {day} of {month}, in the year of our Lord"})
    assert _overflows(bundle)
    long_names = dict(EN, **{key: "Septembertember" for key in lc._MONTH_KEYS})
    assert _overflows(long_names)


def test_the_spanish_example_fits_and_reads_as_approved():
    bundle = _spanish_like()
    assert not _overflows(bundle)


# ── Another language, and the degraded paths ───────────────────────────────


def test_a_device_language_writes_its_own_date(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, _spanish_like())
    assert lc.masthead_date(datetime(2026, 9, 30)) == "mié., 30 de sept."
    assert lc.masthead_date(datetime(2026, 3, 5)) == "jue., 5 de mar."


def test_a_truncated_english_bundle_gets_the_iso_date_not_a_key(tmp_path, monkeypatch):
    """The canonical bundle is "usable" by definition even when it has lost
    keys; the date must not paint "clock.month.sep" (litclock-dev#919 review)."""
    for missing in ("clock.month.sep", "clock.masthead.date", "clock.day.30", "clock.weekday.wed", "blank"):
        bundle = dict(EN)
        if missing == "blank":
            bundle["clock.month.sep"] = "  "
        else:
            del bundle[missing]
        target = tmp_path / missing / "languages" / "en" / "strings.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps(bundle), encoding="utf-8")
        registry = json.loads((REPO / "languages.json").read_text(encoding="utf-8"))
        registry["languages"]["en"]["strings"] = "languages/en/strings.json"
        (tmp_path / missing / "languages.json").write_text(json.dumps(registry), encoding="utf-8")
        monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", tmp_path / missing / "languages.json")
        monkeypatch.setattr(strings_catalog, "_REPO_ROOT", tmp_path / missing)
        strings_catalog.reset_cache()
        assert lc.masthead_date(datetime(2026, 9, 30)) == "2026-09-30", missing


def test_an_unusable_bundle_gets_the_iso_date_never_english(tmp_path, monkeypatch):
    bundle = _spanish_like()
    del bundle["clock.month.sep"]
    _registry(tmp_path, monkeypatch, bundle)
    assert lc.masthead_date(datetime(2026, 9, 30)) == "2026-09-30"


def test_a_catalog_that_raises_gets_the_iso_date(monkeypatch):
    def boom(_lang=None):
        raise RuntimeError("catalog exploded")

    monkeypatch.setattr(strings_catalog, "snapshot", boom)
    assert lc.masthead_date(datetime(2026, 9, 30)) == "2026-09-30"


def test_a_translated_line_break_is_folded(tmp_path, monkeypatch):
    bundle = dict(EN, **{"clock.masthead.date": "{weekday},\n{month} {day}"})
    _registry(tmp_path, monkeypatch, bundle)
    assert lc.masthead_date(datetime(2026, 9, 30)) == "Wed, September 30"


def test_an_overlong_date_never_reaches_the_qr():
    """The runtime backstop: shrink, then cut — no ink right of the budget."""
    # The last one's ADVANCE fits (442.2px) while its final Q inks 4px past
    # the budget (447px): only an ink check sends it to the backstop.
    for text in ("Wednesday, the thirtieth of September", "W" * 80, "Mié 04 de Novembre", "Mercredi 30 iiiiiiiiQ"):
        image = Image.new("1", lc.DISPLAY_SIZE, 255)
        lc._compose_masthead(image, ImageDraw.Draw(image), text, None, None, None)
        band = image.crop((lc.MASTHEAD_DATE_RIGHT, 0, lc.QR_POSITION[0], lc.DIVIDER_Y - 2))
        assert ImageOps.invert(band.convert("L")).getbbox() is None, text
        left = image.crop((lc.DATE_X, 0, lc.DATE_X + lc.MASTHEAD_DATE_BUDGET, lc.DIVIDER_Y - 2))
        assert ImageOps.invert(left.convert("L")).getbbox() is not None, "the date vanished"


def test_the_backstop_shrinks_before_it_cuts(monkeypatch):
    draw = ImageDraw.Draw(Image.new("1", (4, 4)))
    text = "Wednesday, September 30th"  # wider than the budget at 48pt, fits smaller
    assert lc._date_ink_width(draw, text, ImageFont.truetype(lc.FONT_PATH, lc.DATE_FONT_SIZE)) > lc.MASTHEAD_DATE_BUDGET
    fitted, font = lc._fit_masthead_date(draw, text)
    assert fitted == text and lc.MASTHEAD_DATE_FLOOR <= font.size < lc.DATE_FONT_SIZE
    fitted, font = lc._fit_masthead_date(draw, "W" * 80)
    assert fitted.endswith("…") and font.size == lc.MASTHEAD_DATE_FLOOR
    assert lc._date_ink_width(draw, fitted, font) <= lc.MASTHEAD_DATE_BUDGET
    # A pathological bundle is capped where the date is made, so nothing after
    # it measures more than the cap.
    assert len(lc._fit_masthead_date(draw, "W" * 100_000)[0]) <= lc.MASTHEAD_DATE_MAX_CHARS + 1


def test_a_pathological_pattern_is_capped_where_the_date_is_made(tmp_path, monkeypatch):
    bundle = dict(EN, **{"clock.masthead.date": "{weekday} " + "W" * 2_000_000})
    _registry(tmp_path, monkeypatch, bundle)
    text = lc.masthead_date(datetime(2026, 9, 30))
    assert len(text) == lc.MASTHEAD_DATE_MAX_CHARS and text.startswith("Wed W")
    image = Image.new("1", lc.DISPLAY_SIZE, 255)
    lc._compose_masthead(image, ImageDraw.Draw(image), text, None, None, None)  # Pillow refuses >1M chars
