"""litclock-dev#532 T4 — `tools/render_invariants.py --language CODE`.

Every registered language's corpus, English included, through the device's
own registry resolution, with three checks on top of I1-I5 (L1 mature flag,
L2 time field, L3 glyph coverage) and the D19 coverage report.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "tools"))

try:
    import freetype  # noqa: F401

    _HAVE_FREETYPE = True
except ImportError:
    _HAVE_FREETYPE = False

pytestmark = pytest.mark.skipif(not _HAVE_FREETYPE, reason="freetype-py required (render_invariants renders rows)")

GOOD = "00:00|midnight|It was midnight already in the quiet house.|Title|Author|NO\n"


@pytest.fixture
def lang(tmp_path, monkeypatch):
    """A checkout under tmp_path with English (the real corpus is NOT copied
    — tests name their own corpora) and a registered `xx` whose corpus each
    test writes. Returns a runner: run(corpus_text, code="xx") -> (rc, report)."""
    import quote_corpus
    import strings_catalog

    (tmp_path / "corpora").mkdir()

    def register(entries):
        (tmp_path / "languages.json").write_text(
            json.dumps({"fleet_default": "en", "languages": entries}), encoding="utf-8"
        )
        strings_catalog.reset_cache()

    def entry(code, rel, status="active"):
        return {"code": code, "status": status, "corpus": {"path": rel}, "strings": f"languages/{code}/strings.json"}

    register({"en": entry("en", "corpora/en.csv"), "xx": entry("xx", "corpora/xx.csv", "incubating")})
    monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", tmp_path / "languages.json")
    monkeypatch.setattr(strings_catalog, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(quote_corpus, "_PROJECT_ROOT", tmp_path)
    strings_catalog.reset_cache()
    import render_invariants

    def run(text, code="xx", *, register_as=None):
        if register_as is not None:
            register(register_as)
        (tmp_path / "corpora" / f"{code}.csv").write_text(text, encoding="utf-8")
        report = tmp_path / f"report-{code}.json"
        monkeypatch.setattr(sys, "argv", ["render_invariants.py", "--language", code, "check", "--report", str(report)])
        rc = render_invariants.main()
        return rc, (json.loads(report.read_text(encoding="utf-8")) if report.exists() else None)

    run.entry = entry
    yield run
    strings_catalog.reset_cache()


class TestLanguageChecks:
    def test_a_clean_corpus_passes(self, lang):
        rc, report = lang(GOOD)
        assert rc == 0, report["failures"]
        assert report["summary"]["language"] == "xx"

    @pytest.mark.parametrize("flag", ["SÍ", "si", "yes", "YES ", "oui", "No"])
    def test_a_mature_flag_the_clock_would_misread_fails(self, lang, flag):
        """R6: anything but YES/NO/empty reads as SAFE on the clock."""
        rc, report = lang(GOOD.replace("|NO\n", f"|{flag}\n"))
        assert rc == 1
        assert any(f.startswith("L1 row 1") for f in report["failures"]), report["failures"]

    def test_a_byte_order_mark_is_named_not_reported_as_a_bad_time(self, lang):
        """litclock-dev#926 pass 3: Excel's "CSV UTF-8" adds a BOM, which reached the
        volunteer as an unexplained `'\\ufeff07:30' is not an HH:MM minute`."""
        rc, report = lang("\ufeff" + GOOD)
        assert rc == 1
        assert any("byte-order mark" in f for f in report["failures"]), report["failures"]

    @pytest.mark.parametrize(
        "quote",
        ['"It was midnight," she said.', '"It was midnight'],
        ids=["dialogue-opening", "unterminated"],
    )
    def test_quotation_marks_the_clock_would_misread_fail(self, lang, quote, capsys):
        """litclock-dev#926 review (red team): csv.reader drops the marks from
        `"It was midnight," she said.` and every other check passes, so the
        clock shows `It was midnight, she said.`. An unterminated quote
        swallows the following lines into one field."""
        rc, report = lang(f"00:00|midnight|{quote}|Title|Author|NO\n")
        assert rc == 1 and report is None  # stops before rendering
        assert "L6 line 1: quotation marks the clock would misread" in capsys.readouterr().out

    def test_an_unterminated_quote_in_a_large_corpus_is_a_report_not_a_traceback(self, lang, capsys):
        """litclock-dev#926 re-review: past csv's 128 KB field limit the lenient walk
        raised csv.Error and the L6 line number never reached the volunteer."""
        rest = GOOD * 3000  # ~200 KB the open quote would swallow
        rc, _report = lang(f'00:00|midnight|"It was midnight|Title|Author|NO\n{rest}')
        assert rc == 1
        assert "L6 line 1" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "quote",
        [
            '"""It was midnight,"" she said."',
            'She said "midnight" twice.',
            'It was midnight "already',  # a lone mid-field mark is literal to csv.reader
            "“It was midnight,” she said.",
        ],
        ids=["csv-escaped", "mid-field", "lone-mid-field", "typographic"],
    )
    def test_quotation_marks_the_clock_reads_as_written_pass(self, lang, quote):
        rc, report = lang(f"00:00|midnight|{quote}|Title|Author|NO\n")
        assert rc == 0, report["failures"]

    @pytest.mark.parametrize("flag", ["YES", "NO", ""])
    def test_the_three_flags_pass(self, lang, flag):
        rc, report = lang(GOOD.replace("|NO\n", f"|{flag}\n"))
        assert rc == 0, report["failures"]

    @pytest.mark.parametrize("time", ["0:00", "24:00", "12:60", "1200", "noon"])
    def test_a_time_that_names_no_minute_fails(self, lang, time):
        rc, report = lang(GOOD.replace("00:00|", f"{time}|", 1))
        assert rc == 1
        assert any(f.startswith("L2 row 1") for f in report["failures"]), report["failures"]

    @pytest.mark.parametrize(
        ("field", "char"),
        [("quote", "א"), ("quote", "時"), ("quote", "✿"), ("title", "א"), ("timestring", "時")],
        ids=["hebrew-quote", "cjk-quote", "dingbat-quote", "hebrew-title", "cjk-timestring"],
    )
    def test_a_character_no_font_draws_fails(self, lang, field, char):
        """R11: an unsupported character renders as an empty box."""
        row = GOOD
        if field == "quote":
            row = row.replace("quiet house", f"quiet {char} house")
        elif field == "title":
            row = row.replace("|Title|", f"|Title {char}|")
        else:
            row = row.replace("midnight|It was midnight", f"midnight{char}|It was midnight{char}")
        rc, report = lang(row)
        assert rc == 1
        assert any(f.startswith("L3 row 1") and f"U+{ord(char):04X}" in f for f in report["failures"]), report[
            "failures"
        ]

    @pytest.mark.parametrize(
        "text", ["Él dijo: «¿qué?» — ¡ñ!", "Ζεύς", "Москва", "Łódź ß ẞ"], ids=["spanish", "greek", "cyrillic", "polish"]
    )
    def test_scripts_the_fonts_cover_pass(self, lang, text):
        rc, report = lang(GOOD.replace("in the quiet house", f"in {text}"))
        assert not [f for f in report["failures"] if f.startswith("L3")], report["failures"]

    @pytest.mark.parametrize("face", ["bold", "credits"])
    def test_each_face_is_checked_for_what_it_draws(self, lang, monkeypatch, face):
        """The shipped faces share a character set, so only a face that
        differs shows each check is wired to its own field: the timestring is
        drawn bold, title and author in the credits face."""
        import render_invariants

        import quote_renderer as qr

        real = render_invariants._faces()
        target = {"bold": qr.FONT_BOLD, "credits": qr.FONT_CREDITS}[face]

        class NoE:
            def get_char_index(self, cp):
                return 0 if cp == ord("é") else real[target].get_char_index(cp)

        monkeypatch.setattr(render_invariants, "_faces", lambda: {**real, target: NoE()})
        row = (
            GOOD.replace("midnight|It was midnight", "midnighté|It was midnighté")
            if face == "bold"
            else (GOOD.replace("|Title|", "|Titleé|"))
        )
        rc, report = lang(row)
        assert rc == 1
        assert any(f.startswith("L3") and target in f for f in report["failures"]), report["failures"]

    def test_a_missing_timestring_still_fails_i1(self, lang):
        rc, report = lang("00:00|eleven|It was midnight already.|T|A|NO\n")
        assert rc == 1 and any(f.startswith("I1") for f in report["failures"])


class TestReviewFindings:
    """litclock-dev#914 review."""

    @pytest.mark.parametrize("cp", [0x202F, 0x2028, 0x3000, 0x1680], ids=["nnbsp", "line-sep", "ideographic", "ogham"])
    def test_a_space_the_fonts_cannot_draw_fails(self, lang, cp):
        """P1: whitespace was exempt from L3, but only ASCII whitespace is
        collapsed — these are DRAWN, as an empty box or a wrong gap."""
        rc, report = lang(GOOD.replace("quiet house", f"quiet{chr(cp)}house"))
        assert rc == 1
        assert any(f.startswith("L3 row 1") and f"U+{cp:04X}" in f for f in report["failures"]), report["failures"]

    def test_characters_the_renderer_adds_are_checked_too(self, lang, monkeypatch):
        """The credits line is DASH + "title, author": a credits face without
        the dash would box it on every frame although no field contains it."""
        import render_invariants

        import quote_renderer as qr

        real = render_invariants._faces()

        class NoDash:
            def get_char_index(self, cp):
                return 0 if cp == ord(qr.DASH) else real[qr.FONT_CREDITS].get_char_index(cp)

        monkeypatch.setattr(render_invariants, "_faces", lambda: {**real, qr.FONT_CREDITS: NoDash()})
        rc, report = lang(GOOD)
        assert rc == 1
        assert any(f.startswith("L3") and "U+2014" in f for f in report["failures"]), report["failures"]

    @pytest.mark.parametrize("phrase", ["", "   "], ids=["empty", "blank"])
    def test_an_empty_time_phrase_fails(self, lang, phrase):
        rc, report = lang(GOOD.replace("|midnight|", f"|{phrase}|", 1))
        assert rc == 1 and any(f.startswith("L4 row 1") for f in report["failures"]), report["failures"]

    @pytest.mark.parametrize(
        "line",
        ["00:01|one|It was one.|T", "00:01|one|It was one.|T|A|NO|extra"],
        ids=["four-fields", "seven-fields"],
    )
    def test_a_row_of_the_wrong_shape_fails(self, lang, line):
        rc, report = lang(GOOD + line + "\n")
        assert rc == 1 and any(f.startswith("L5 line 2") for f in report["failures"]), report["failures"]

    def test_a_line_of_empty_fields_is_a_row_and_fails(self, lang):
        """`||||` is a row to the clock's walk (key "", basename quote__0)."""
        rc, report = lang(GOOD + "||||\n")
        assert rc == 1 and any(f.startswith("L2 row 2") for f in report["failures"]), report["failures"]

    def test_a_corpus_that_is_not_utf8_is_a_rejection_not_a_traceback(self, lang, tmp_path, monkeypatch):
        import render_invariants

        (tmp_path / "corpora" / "xx.csv").write_bytes(GOOD.replace("quiet", "caf\u00e9").encode("latin-1"))
        monkeypatch.setattr(
            sys, "argv", ["render_invariants.py", "check", "--language", "xx", "--report", str(tmp_path / "r.json")]
        )
        assert render_invariants.main() == 1

    def test_a_backslash_guard_failure_is_a_rejection_not_a_traceback(self, lang, tmp_path, monkeypatch, capsys):
        import render_invariants

        (tmp_path / "corpora" / "xx.csv").write_text(GOOD.replace("quiet house", "quiet \\ house"), encoding="utf-8")
        monkeypatch.setattr(
            sys, "argv", ["render_invariants.py", "check", "--language", "xx", "--report", str(tmp_path / "r.json")]
        )
        assert render_invariants.main() == 1

    def test_a_corpus_of_only_blank_lines_fails(self, lang):
        rc, report = lang("\n\n\n")
        assert rc == 1 and any("no rows" in f for f in report["failures"]), report

    def test_blank_lines_are_not_rows(self, lang):
        rc, report = lang(GOOD + "\n\n")
        assert rc == 0, report["failures"]

    @pytest.mark.parametrize("time", ["0\u0666:3\u0660", "0\uff16:0\uff10"], ids=["arabic-indic", "fullwidth"])
    def test_non_ascii_digits_fail_and_never_count_as_covered(self, lang, time):
        rc, report = lang(GOOD.replace("00:00|", f"{time}|", 1))
        assert rc == 1 and any(f.startswith("L2 row 1") for f in report["failures"])
        assert report["summary"]["sfw_minutes"] == 0

    def test_coverage_follows_the_clocks_own_mature_reading(self, lang):
        """A `yes` row is rejected by L1 AND, as the clock reads it, mature:
        it must not count as a covered safe minute."""
        _rc, report = lang(GOOD.replace("|NO\n", "|yes\n"))
        assert report["summary"]["sfw_minutes"] == 0

    def test_language_belongs_to_check_not_probe(self, lang, monkeypatch, tmp_path):
        import render_invariants

        (tmp_path / "corpora" / "xx.csv").write_text(GOOD, encoding="utf-8")
        monkeypatch.setattr(
            sys, "argv", ["render_invariants.py", "check", "--language", "xx", "--report", str(tmp_path / "r.json")]
        )
        assert render_invariants.main() == 0
        monkeypatch.setattr(sys, "argv", ["render_invariants.py", "--language", "xx", "probe", "5", "1"])
        with pytest.raises(SystemExit):
            render_invariants.main()


class TestResolution:
    @pytest.mark.parametrize("rel", ["../outside.csv", "/etc/passwd"])
    def test_a_corpus_outside_the_checkout_is_refused(self, lang, tmp_path, rel):
        """The same containment the device applies: a path the clock would
        refuse cannot pass here as a healthy translation."""
        (tmp_path.parent / "outside.csv").write_text(GOOD, encoding="utf-8")
        rc, report = lang(GOOD, register_as={"en": lang.entry("en", "corpora/en.csv"), "xx": lang.entry("xx", rel)})
        assert rc == 2 and report is None

    def test_an_unregistered_code_is_refused(self, lang):
        rc, report = lang(GOOD, code="zz")
        assert rc == 2 and report is None

    def test_an_unregistered_corpus_at_the_conventional_path_is_checked(self, lang, tmp_path, monkeypatch, capsys):
        """T8: a volunteer checks a corpus before the language is registered.
        It gets the corpus checks (a bad flag still fails) and a NOTE. (The
        `lang` fixture is used only for the tmp checkout it roots.)"""
        (tmp_path / "languages" / "zz").mkdir(parents=True)
        (tmp_path / "languages" / "zz" / "quotes.csv").write_text(GOOD.replace("|NO", "|SÍ"), encoding="utf-8")
        import render_invariants

        assert render_invariants._unregistered_corpus("zz") == ((tmp_path / "languages/zz/quotes.csv").resolve(), "")
        argv = ["render_invariants.py", "--language", "zz", "check", "--report", str(tmp_path / "r.json")]
        monkeypatch.setattr(sys, "argv", argv)
        assert render_invariants.main() == 1
        out = capsys.readouterr().out
        assert "zz is not in languages.json yet" in out and "L1 row 1" in out
        (tmp_path / "languages" / "zz" / "quotes.csv").write_text(GOOD, encoding="utf-8")
        assert render_invariants.main() == 0

    @pytest.mark.parametrize("code", ["xx", "en"], ids=["incubating", "active"])
    def test_a_registered_code_never_falls_back_to_quotes_csv(self, lang, tmp_path, code):
        """The device resolves a registered language through the registry
        only; a stray quotes.csv beside it must not stand in for a missing
        registered corpus (or the gate would pass a corpus the clock refuses)."""
        (tmp_path / "languages" / code).mkdir(parents=True)
        (tmp_path / "languages" / code / "quotes.csv").write_text(GOOD, encoding="utf-8")
        import render_invariants

        path, why = render_invariants._unregistered_corpus(code)
        assert path is None and "registered" in why

    @pytest.mark.parametrize(("code", "planted"), [("ZZ", "ZZ"), ("z", "z"), ("zz/../yy", "yy"), ("pt_BR", "pt_BR")])
    def test_a_code_that_is_not_a_language_tag_is_refused(self, lang, tmp_path, code, planted):
        """A corpus IS at the path each code would resolve to, so only the
        tag check can refuse it (litclock-dev#926 review: without the file the test
        passed on is_file() alone and could not fail)."""
        (tmp_path / "languages" / planted).mkdir(parents=True)
        (tmp_path / "languages" / planted / "quotes.csv").write_text(GOOD, encoding="utf-8")
        import render_invariants

        path, why = render_invariants._unregistered_corpus(code)
        assert path is None and "is not a language code" in why

    def test_a_symlinked_corpus_is_refused(self, lang, tmp_path):
        """A link to English rows would pass on English data (litclock-dev#926 review)."""
        (tmp_path / "corpora" / "en.csv").write_text(GOOD, encoding="utf-8")
        (tmp_path / "languages" / "zz").mkdir(parents=True)
        (tmp_path / "languages" / "zz" / "quotes.csv").symlink_to(tmp_path / "corpora" / "en.csv")
        import render_invariants

        path, why = render_invariants._unregistered_corpus("zz")
        assert path is None and "not a link" in why

    @pytest.mark.parametrize(("content", "why"), [(None, "no languages/zz/quotes.csv"), ("", "is empty")])
    def test_the_refusal_says_which_condition_failed(self, lang, tmp_path, monkeypatch, capsys, content, why):
        """litclock-dev#926 review: the message used to tell a volunteer to put the file
        exactly where it already was."""
        if content is not None:
            (tmp_path / "languages" / "zz").mkdir(parents=True)
            (tmp_path / "languages" / "zz" / "quotes.csv").write_text(content, encoding="utf-8")
        import render_invariants

        argv = ["render_invariants.py", "--language", "zz", "check", "--report", str(tmp_path / "r.json")]
        monkeypatch.setattr(sys, "argv", argv)
        assert render_invariants.main() == 2
        assert why in capsys.readouterr().out

    def test_the_strings_checks_run_for_registered_languages_only(self, lang, tmp_path, monkeypatch):
        """L7/L8 read the strings bundle the maintainer writes at
        registration; a volunteer's corpus has none yet."""
        import render_invariants

        calls = []
        monkeypatch.setattr(render_invariants, "catalog_checks", lambda code: calls.append(code) or [])
        (tmp_path / "languages" / "zz").mkdir(parents=True)
        (tmp_path / "languages" / "zz" / "quotes.csv").write_text(GOOD, encoding="utf-8")
        argv = ["render_invariants.py", "--language", "zz", "check", "--report", str(tmp_path / "r.json")]
        monkeypatch.setattr(sys, "argv", argv)
        assert render_invariants.main() == 0 and calls == []
        rc, _report = lang(GOOD)  # the registered `xx`
        assert rc == 0 and calls == ["xx"]

    def test_an_empty_corpus_is_refused(self, lang):
        rc, _report = lang("")
        assert rc == 2


class TestCoverageReport:
    def test_coverage_numbers(self, lang):
        """Two SFW minutes (00:00, 12:00) and one mature-only (13:00)."""
        rows = (
            GOOD
            + GOOD.replace("00:00|", "12:00|", 1).replace("midnight", "noon")
            + GOOD.replace("00:00|", "13:00|", 1).replace("midnight", "one").replace("|NO\n", "|YES\n")
        )
        rc, report = lang(rows)
        assert rc == 0, report["failures"]
        s = report["summary"]
        assert s["sfw_minutes"] == 2
        assert s["sfw_coverage_pct"] == round(200 / 1440, 1)
        assert s["daytime_sfw_coverage_pct"] == round(100 / 960, 1)
        # 12:01 .. 23:59 then 00:00 is covered: 719 minutes; 00:01 .. 11:59: 719.
        assert s["longest_uncovered_run_minutes"] == 719

    def test_the_longest_run_wraps_past_midnight(self, lang):
        rows = GOOD.replace("00:00|", "12:00|", 1).replace("midnight", "noon")
        _rc, report = lang(rows)
        assert report["summary"]["longest_uncovered_run_minutes"] == 1439


def _language_step_script() -> str:
    import yaml

    wf = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "render-invariants.yml").read_text(encoding="utf-8"))
    steps = [st for job in wf["jobs"].values() for st in job["steps"]]
    (step,) = [st for st in steps if st.get("name", "").startswith("Render invariants (every language")]
    return step["run"]


def _run_step(tmp_path, registry_text, fail_codes=(), unregistered=(), files=()):
    """EXECUTE the shipped step — under `-eo pipefail`, stricter than the
    `bash -e` GitHub uses for a step with no `shell:` — against a
    stub render_invariants.py that records each call and fails for
    `fail_codes`, and the REAL tools/language_corpora.py that decides the
    list. `files` are extra CSVs to plant, relative to the checkout."""
    import shutil
    import subprocess

    (tmp_path / "tools").mkdir()
    shutil.copy(REPO_ROOT / "tools" / "language_corpora.py", tmp_path / "tools")
    log = tmp_path / "calls"
    (tmp_path / "tools" / "render_invariants.py").write_text(
        "import sys\n"
        f"open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        f"sys.exit(1 if sys.argv[2] in {list(fail_codes)!r} else 0)\n",
        encoding="utf-8",
    )
    (tmp_path / "languages.json").write_text(registry_text, encoding="utf-8")
    for rel in [f"languages/{code}/quotes.csv" for code in unregistered] + list(files):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(GOOD, encoding="utf-8")
    r = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", _language_step_script()],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return r.returncode, calls


class TestTheWorkflowStep:
    REG = json.dumps({"languages": {"en": {}, "es": {}}})

    def test_every_registered_language_is_checked(self, tmp_path):
        rc, calls = _run_step(tmp_path, self.REG)
        assert rc == 0
        assert [c.split()[:3] for c in calls] == [["--language", "en", "check"], ["--language", "es", "check"]]

    def test_one_failing_language_fails_the_step_after_all_run(self, tmp_path):
        rc, calls = _run_step(tmp_path, self.REG, fail_codes=("en",))
        assert rc != 0 and len(calls) == 2

    @pytest.mark.parametrize(
        "registry",
        ["{broken", json.dumps({"languages": {}}), json.dumps({"languages": {"es": {}}}), json.dumps({})],
        ids=["unparseable", "empty", "no-english", "no-languages"],
    )
    def test_a_registry_that_names_nothing_fails_closed(self, tmp_path, registry):
        """litclock-dev#914 review: the loop ran zero times and the step exited 0."""
        rc, calls = _run_step(tmp_path, registry)
        assert rc != 0 and calls == []

    def test_an_unregistered_corpus_is_checked_too(self, tmp_path):
        """T8: a volunteer's PR adds only languages/<code>/quotes.csv; a
        registry-only loop would render nothing new on exactly that PR."""
        rc, calls = _run_step(tmp_path, self.REG, unregistered=("fr", "de"))
        assert rc == 0
        assert [c.split()[1] for c in calls] == ["en", "es", "de", "fr"]

    @pytest.mark.parametrize(
        "rel",
        ["languages/de/de.csv", "languages/es/quotes.csv", "languages/en /quotes.csv", "languages/ /quotes.csv"],
        ids=["other-name", "beside-registered", "trailing-space", "blank-name"],
    )
    def test_a_csv_the_loop_would_not_render_fails_the_step_before_any_render(self, tmp_path, rel):
        """litclock-dev#926 review: each of these triggered the step and passed it green
        without the file being read (the space cases word-split to `en`, or
        to nothing)."""
        rc, calls = _run_step(tmp_path, self.REG, files=(rel,))
        assert rc != 0 and calls == []

    def test_a_failing_unregistered_corpus_fails_the_step(self, tmp_path):
        rc, calls = _run_step(tmp_path, self.REG, fail_codes=("fr",), unregistered=("fr",))
        assert rc != 0 and len(calls) == 3

    def test_corpus_files_trigger_the_workflow(self):
        wf = (REPO_ROOT / ".github" / "workflows" / "render-invariants.yml").read_text(encoding="utf-8")
        assert "'languages/**/*.csv'" in wf


def test_a_phrase_matched_only_by_unicode_folding_renders(lang):
    """litclock-dev#532 T5: 'Única' in the row, 'única' in the quote — the
    ASCII-only stristr pass misses it; the gate must render it like the clock."""
    rc, report = lang("00:00|Única|Era la única hora de la noche.|Título|Autora|NO\n")
    assert rc == 0, report["failures"]
