"""litclock-dev#532 DT8 (design D16): the control app's web fonts.

The app's four woff2 fonts are Latin-1 subsets; the clock face's Literata is
not the constraint. Every LETTER a language's UI strings use must be in all
four (render_invariants L7, and the translator validator's FONT finding), or
that language's app is drawn with fallback-font letters mid-word. Symbols
fall back per glyph harmlessly and are not checked — English already uses
"→". Corpus letters are a reported count, not a gate (an occasional
proper name such as Čapek is fine).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))

import render_invariants as ri  # noqa: E402

import strings_catalog  # noqa: E402

EN = json.loads((REPO / "languages" / "en" / "strings.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def cmaps():
    return ri.pwa_cmaps()


def test_the_four_app_fonts_are_read(cmaps):
    assert sorted(cmaps) == [
        "fraunces-wght-italic.woff2",
        "fraunces-wght-normal.woff2",
        "geist-mono-wght-normal.woff2",
        "instrument-sans-wght-normal.woff2",
    ]
    assert all(len(cmap) > 150 for cmap in cmaps.values())


@pytest.mark.parametrize(("text", "missing"), [
    ("Configuración — año, über, Ærø", set()),       # Spanish, German, Danish: Latin-1
    ("Zażółć gęślą", {"ż", "ł", "ć", "ę", "ś", "ą"}),  # Polish
    ("Ωμέγα", {"Ω", "μ", "έ", "γ", "α"}),               # Greek
    ("Жизнь", {"Ж", "и", "з", "н", "ь"}),               # Cyrillic
    ("e\u0328", {"ę"}),                        # decomposed ę is judged as drawn: composed, still missing
    ("q\u0328", {"\u0328"}),                   # a mark that cannot compose is checked on its own
    ("System → Reset · 20°C", set()),                    # symbols fall back per glyph: not checked
])  # fmt: skip
def test_letters_are_checked_and_symbols_are_not(cmaps, text, missing):
    gaps = ri.pwa_missing_letters(text, cmaps)
    assert {ch for chars in gaps.values() for ch in chars} == missing


def test_the_english_ui_passes():
    assert ri.catalog_checks("en") == []


def _language(tmp_path, monkeypatch, value):
    bundle = dict(EN, **{"setup.page.title": value})
    for code, data in (("en", EN), ("xx", bundle)):
        target = tmp_path / "languages" / code / "strings.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    registry = json.loads((REPO / "languages.json").read_text(encoding="utf-8"))
    registry["languages"]["en"]["strings"] = "languages/en/strings.json"
    registry["languages"]["xx"] = dict(registry["languages"]["en"], code="xx", strings="languages/xx/strings.json")
    (tmp_path / "languages.json").write_text(json.dumps(registry), encoding="utf-8")
    monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", tmp_path / "languages.json")
    monkeypatch.setattr(strings_catalog, "_REPO_ROOT", tmp_path)
    strings_catalog.reset_cache()
    return bundle


@pytest.mark.parametrize("planted", ["Konfiguracja zegara — ą", "Ρύθμιση", "Настройка"])
def test_a_planted_polish_greek_or_cyrillic_letter_fails_the_gate(tmp_path, monkeypatch, planted):
    _language(tmp_path, monkeypatch, planted)
    try:
        failures = ri.catalog_checks("xx")
    finally:
        strings_catalog.reset_cache()
    assert len(failures) == 1 and failures[0].startswith("L7 setup.page.title:"), failures


def test_a_spanish_ui_passes_the_gate(tmp_path, monkeypatch):
    _language(tmp_path, monkeypatch, "Configuración del reloj")
    try:
        assert ri.catalog_checks("xx") == []
    finally:
        strings_catalog.reset_cache()


def test_the_validator_reports_it_to_the_translator(tmp_path):
    spec = importlib.util.spec_from_file_location("validate_translation", REPO / "scripts" / "validate_translation.py")
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    for value, expect in (("Konfiguracja — ł", True), ("Configuración", False)):
        path = tmp_path / "xx.json"
        path.write_text(json.dumps(dict(EN, **{"setup.page.title": value}), ensure_ascii=False), encoding="utf-8")
        font = [f for f in validator.validate("es", str(path)) if f.startswith("FONT")]
        assert bool(font) is expect, (value, font)


def test_corpus_letters_are_counted_not_gated(tmp_path):
    csv = tmp_path / "xx.csv"
    csv.write_text(
        "00:00|midnight|It was midnight in Prague.|Válka s mloky|Karel Čapek\n"
        "00:01|one minute past|It was one minute past midnight.|A Book|An Author\n",
        encoding="utf-8",
    )
    failures, coverage = ri.language_checks(csv)
    assert not [f for f in failures if "fonts" in f]
    assert coverage["pwa_fallback_rows"] == 1


def test_the_check_command_fails_a_language_whose_ui_the_fonts_cannot_draw(tmp_path, monkeypatch, capsys):
    """The gate is wired into `check --language`, not just available."""
    import argparse

    import quote_corpus

    _language(tmp_path, monkeypatch, "Ustawienia — ł")
    corpus = tmp_path / "languages" / "xx" / "corpus.csv"
    corpus.write_text("00:00|midnight|It was midnight.|A Book|An Author\n", encoding="utf-8")
    registry = json.loads((tmp_path / "languages.json").read_text(encoding="utf-8"))
    registry["languages"]["xx"]["corpus"] = {"path": "languages/xx/corpus.csv", "rows": 1}
    (tmp_path / "languages.json").write_text(json.dumps(registry), encoding="utf-8")
    monkeypatch.setattr(quote_corpus, "_PROJECT_ROOT", tmp_path)
    strings_catalog.reset_cache()
    args = argparse.Namespace(language="xx", csv="", report=str(tmp_path / "r.json"), limit=0)
    try:
        rc = ri.cmd_check(args)
    finally:
        strings_catalog.reset_cache()
    assert rc != 0
    assert "L7 setup.page.title" in capsys.readouterr().out


def test_the_font_helpers_import_without_the_rendering_stack():
    """A translator with only fonttools[woff] installed (no Pillow, maybe on
    Windows, where `fcntl` does not exist) must still get the check (litclock-dev#925
    review): the helpers live in a module of their own."""
    import subprocess

    code = (
        "import sys; sys.path.insert(0, 'tools'); import pwa_glyphs; "
        "heavy = {'PIL', 'literary_clock', 'quote_renderer', 'config', 'fcntl', 'freetype'} & set(sys.modules); "
        "assert not heavy, heavy; print(len(pwa_glyphs.pwa_cmaps()))"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "4"


def test_a_skipped_font_check_does_not_fail_a_conformant_bundle(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("validate_translation", REPO / "scripts" / "validate_translation.py")
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    monkeypatch.setattr(validator, "validate", lambda code, file: ["NOTE  font check skipped: pip install ..."])
    assert validator.main(["en"]) == 0
    assert "NOTE  font check skipped" in capsys.readouterr().out


# ── Round-1 review hardening (litclock-dev#925) ────────────────────────────────────────


@pytest.mark.parametrize("code", sorted(json.loads((REPO / "languages.json").read_text("utf-8"))["languages"]))
def test_every_registered_language_passes_the_font_gates(code):
    """On every push — the render-invariants workflow does not run on a
    strings-only change, which is how a translation usually changes."""
    assert ri.catalog_checks(code) == []


@pytest.mark.parametrize("word", ["français", "garçon", "forêt", "Århus"])
def test_a_decomposed_file_is_judged_as_drawn(cmaps, word):
    import unicodedata

    assert ri.pwa_missing_letters(unicodedata.normalize("NFD", word), cmaps) == {}


def test_a_panel_string_with_a_character_the_panel_lacks_fails_l8(tmp_path, monkeypatch):
    """French puts a narrow no-break space (U+202F) before ':' — Literata has
    none, and the panel draws a missing character as an empty box."""
    bundle = _language(tmp_path, monkeypatch, "Réglages")
    bundle["firstboot.splash.preparing.title"] = "Réglages :"
    (tmp_path / "languages" / "xx" / "strings.json").write_text(json.dumps(bundle, ensure_ascii=False), "utf-8")
    strings_catalog.reset_cache()
    try:
        failures = ri.catalog_checks("xx")
    finally:
        strings_catalog.reset_cache()
    assert [f for f in failures if f.startswith("L8 firstboot.splash.preparing.title")], failures
    # The same character in an APP-only string is not a panel matter.
    assert not [f for f in failures if f.startswith("L8 setup.")]


def test_the_validator_skips_with_a_note_when_fonttools_cannot_load(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("validate_translation", REPO / "scripts" / "validate_translation.py")
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    import pwa_glyphs

    def no_brotli():
        raise ImportError("No module named brotli")

    monkeypatch.setattr(pwa_glyphs, "pwa_cmaps", no_brotli)
    findings = validator.validate("en", None)
    assert [f for f in findings if f.startswith("NOTE  font check skipped")]
    assert validator.main(["en"]) == 0


def test_a_missing_fonts_directory_does_not_pass_silently(tmp_path, monkeypatch):
    import pwa_glyphs

    monkeypatch.setattr(pwa_glyphs, "PWA_FONTS_DIR", tmp_path)
    with pytest.raises(FileNotFoundError):
        pwa_glyphs.pwa_cmaps()
