"""The translator kit: generated artifacts stay in sync, and the standalone
validator enforces the SAME rules as the CI gate (litclock-dev#532).

The kit (worksheet, skeleton, HTML tool, kit-data) is generated from the
English catalog by scripts/build_translator_kit.py; the standalone
validator (scripts/validate_translation.py) and the CI Stage-4 gate both
import src/catalog_lint.py. These tests pin that the generated files are
not stale and that the validator is a faithful front-end to the shared
rules — a green validator run must mean a green gate.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import catalog_lint  # noqa: E402


def _load_script(name: str):
    """Import a scripts/*.py module by path (scripts/ isn't a package)."""
    path = REPO_ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_kit = _load_script("build_translator_kit.py")
_validate = _load_script("validate_translation.py")


def _en_keys() -> set[str]:
    reg = json.loads((REPO_ROOT / "languages.json").read_text(encoding="utf-8"))["languages"]["en"]
    data = json.loads((REPO_ROOT / reg["strings"]).read_text(encoding="utf-8"))
    return {k for k in data if not k.startswith("_")}


class TestArtifactsInSync:
    def test_committed_artifacts_match_a_fresh_build(self):
        """--check must pass against the committed tree, or an en-catalog
        edit shipped without re-running the generator (stale worksheet /
        tool / skeleton). This is the same check CI runs."""
        assert _kit.main(["--check"]) == 0, "translator kit is stale — run scripts/build_translator_kit.py"

    def test_skeleton_key_set_is_exactly_english(self):
        skeleton = json.loads((REPO_ROOT / "translations" / "strings.template.json").read_text(encoding="utf-8"))
        keys = {k for k in skeleton if not k.startswith("_")}
        assert keys == _en_keys(), "skeleton keys drifted from the English catalog"

    def test_kit_data_slots_match_catalog_lint(self):
        """The worksheet/tool tell translators which slots each value needs;
        that metadata must come from the SAME slot_counts the gate uses, or
        the kit teaches a rule the gate doesn't enforce (or vice versa)."""
        data = _kit.build_data()
        en = {e["key"]: e["english"] for e in data["keys"]}
        for entry in data["keys"]:
            expected = catalog_lint.slot_counts(en[entry["key"]])
            got = {s["name"]: s["count"] for s in entry["slots"]}
            assert got == expected, f"{entry['key']}: kit slots {got} != catalog_lint {expected}"

    def test_kit_data_vocab_matches_catalog_lint(self):
        data = _kit.build_data()
        capable = catalog_lint.rich_capable_keys(REPO_ROOT)
        for entry in data["keys"]:
            assert tuple(entry["rich"]["allowed"]) == capable.get(entry["key"], ()), (
                f"{entry['key']}: kit rich vocab drifted from catalog_lint"
            )

    def test_html_tool_embeds_the_real_data(self):
        html = (REPO_ROOT / "translations" / "tool" / "index.html").read_text(encoding="utf-8")
        assert "/*__KIT_DATA__*/null" not in html, "HTML tool still has the data placeholder — generator didn't fill it"
        # The embedded blob must carry the real keys, not an empty stub.
        for key in ("setup.page.title", "status.relative.minutes.one"):
            assert key in html, f"HTML tool is missing catalog key {key}"


_JS_HARNESS = r'''
import fs from "fs";
const html = fs.readFileSync(process.argv[2], "utf8");
const dataM = html.match(/const DATA = (\{.*?\});\nconst values/s);
const DATA = JSON.parse(dataM[1]);
const jsSrc = html.slice(html.indexOf("const TOKEN_RE"), html.indexOf("function refresh"));
const {lint} = new Function("DATA", jsSrc + "\nreturn {lint, slotCounts};")(DATA);
const byKey = Object.fromEntries(DATA.keys.map(e => [e.key, e]));
const cases = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const out = cases.map(([k, v]) => lint(byKey[k], v).length > 0);
process.stdout.write(JSON.stringify(out));
'''

# Same defect battery both engines must agree on — the previously-divergent
# cases (dup {n}, control chars, invisible-only, stray brace) plus clean ones.
_PARITY_CASES = [
    ("status.relative.minutes.other", "hace {n} {n} minutos"),
    ("status.relative.minutes.one", "hace un minuto"),
    ("status.relative.minutes.other", "hace minutos"),
    ("setup.page.title", "Ahora {"),
    ("setup.page.title", "{Name}"),
    ("setup.page.title", "Listo\treboot"),
    ("setup.page.title", "Listo\rreboot"),
    ("setup.page.title", "\u200b"),
    ("setup.page.title", "\u2800"),
    ("setup.page.title", "\u061c"),
    ("setup.page.title", "\u2028"),
    ("setup.page.title", "\u00a0"),
    ("setup.page.title", "Hola\u061cmundo"),
    ("setup.banner.error_lead", "No pudo <b>x</b> {error}"),
    ("setup.banner.connecting", "Conectando a {em}{network}{/em}"),
    ("setup.banner.error_lead", "Listo {error}"),
    ("setup.page.title", "Configuración normal"),
]


class TestHtmlToolMatchesPython:
    """The HTML tool's JS lint is a reimplementation; it must agree with the
    Python gate on every case, or a volunteer gets a false green (the /review
    found four such divergences). This runs BOTH engines over the same
    battery. Skipped only when node is unavailable (dev/CI convenience)."""

    def test_js_and_python_agree(self, tmp_path):
        import shutil
        import subprocess

        node = shutil.which("node")
        if node is None:
            pytest.skip("node not installed — JS-parity check is dev/CI only")
        data = _kit.build_data()
        bykey = {e["key"]: e for e in data["keys"]}
        en = {e["key"]: e["english"] for e in data["keys"]}
        forms = {"one", "other"}

        py_verdicts = []
        for key, value in _PARITY_CASES:
            entry = bykey[key]
            sib = catalog_lint.plural_sibling_slots(key, forms, en) if entry.get("plural") else None
            cat = key.rpartition(".")[2]
            relax = {"n"} if (sib is not None and cat in catalog_lint.SINGLE_VALUED_CATEGORIES) else None
            # Mirror the VALIDATOR's real path, not raw value_errors: it
            # skips whitespace-only values (they're reported EMPTY, not
            # content-linted), exactly as the JS early-returns on trim-empty.
            # This is the true "green here = green there" comparison.
            if not value.strip():
                py_verdicts.append(False)
                continue
            errs = catalog_lint.value_errors(
                value, en_value=en.get(key), extra_allowed_slots=sib, relax_slots=relax,
                vocab=tuple(entry["rich"]["allowed"]),
            )
            py_verdicts.append(len(errs) > 0)

        harness = tmp_path / "h.mjs"
        harness.write_text(_JS_HARNESS, encoding="utf-8")
        cases_file = tmp_path / "cases.json"
        cases_file.write_text(json.dumps(_PARITY_CASES), encoding="utf-8")
        html = REPO_ROOT / "translations" / "tool" / "index.html"
        result = subprocess.run(
            [node, str(harness), str(html), str(cases_file)],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, f"node harness failed: {result.stderr}"
        js_verdicts = json.loads(result.stdout)
        mismatches = [
            (_PARITY_CASES[i], py_verdicts[i], js_verdicts[i])
            for i in range(len(_PARITY_CASES))
            if py_verdicts[i] != js_verdicts[i]
        ]
        assert not mismatches, f"JS lint disagrees with the Python gate: {mismatches}"


class TestGeneratorGuards:
    """The generator must fail loud when the catalog grows past its maps
    (the /review: a new namespace silently mislabeled "Control app" loses
    its e-ink length flag; a new rich token silently vanishes from the
    worksheet while still permitted)."""

    def test_unmapped_namespace_raises(self, monkeypatch):
        real = _kit._en_catalog

        def _with_alien():
            d = dict(real())
            d["zzznew.thing"] = "x"
            return d

        monkeypatch.setattr(_kit, "_en_catalog", _with_alien)
        with pytest.raises(SystemExit, match="_SURFACES"):
            _kit.build_data()

    def test_unmapped_rich_token_raises(self, monkeypatch):
        monkeypatch.setattr(
            _kit.catalog_lint, "rich_capable_keys", lambda _root: {"setup.page.title": ("b", "u")}
        )
        with pytest.raises(SystemExit, match="_TOKEN_HELP"):
            _kit.build_data()


class TestValidatorMatchesTheGate:
    """The validator is a faithful front-end to the shared rules: what it
    accepts, the CI gate accepts; what it rejects, the gate rejects."""

    def _write_es(self, tmp_path: Path, overrides: dict) -> Path:
        en = {
            "setup.banner.error_lead": "Couldn't join. {error}",
            "setup.banner.connecting": "Connecting to {b}{network}{/b}…",
            "status.relative.minutes.one": "{n} minute ago",
            "status.relative.minutes.other": "{n} minutes ago",
        }
        # A minimal registry rooted in tmp so the validator reads en from here.
        (tmp_path / "languages" / "en").mkdir(parents=True)
        (tmp_path / "languages" / "en" / "strings.json").write_text(json.dumps(en), encoding="utf-8")
        (tmp_path / "languages.json").write_text(
            json.dumps(
                {"languages": {"en": {"code": "en", "status": "active", "strings": "languages/en/strings.json",
                                      "plural_forms": ["one", "other"]},
                               "es": {"code": "es", "status": "active", "strings": "languages/es/strings.json",
                                      "plural_forms": ["one", "other"]}}}
            ),
            encoding="utf-8",
        )
        es = dict(en)
        es.update(overrides)
        es_path = tmp_path / "es-strings.json"
        es_path.write_text(json.dumps(es, ensure_ascii=False), encoding="utf-8")
        return es_path

    @pytest.fixture(autouse=True)
    def _repoint(self, tmp_path, monkeypatch):
        # Point the validator's repo-root helpers at the tmp registry.
        monkeypatch.setattr(_validate, "_REPO_ROOT", tmp_path)
        # rich_capable_keys still scans the REAL source tree (the resolvers
        # live in the repo, not the tmp bundle) — patch the validator's call.
        # Bind the ORIGINAL before patching, or the lambda recurses into
        # itself (the module object is shared).
        _orig = catalog_lint.rich_capable_keys
        monkeypatch.setattr(
            _validate.catalog_lint,
            "rich_capable_keys",
            lambda _root, _fn=_orig, _rr=REPO_ROOT: _fn(_rr),
        )

    def test_conforming_bundle_reports_no_findings(self, tmp_path):
        es = self._write_es(
            tmp_path,
            {
                "setup.banner.error_lead": "No se pudo conectar. {error}",
                "setup.banner.connecting": "Conectando a {b}{network}{/b}…",
                "status.relative.minutes.one": "hace un minuto",
                "status.relative.minutes.other": "hace {n} minutos",
            },
        )
        assert _validate.validate("es", str(es)) == []

    def test_defects_are_reported(self, tmp_path):
        es = self._write_es(
            tmp_path,
            {
                "setup.banner.error_lead": "No se pudo <b>conectar</b>. {error}",  # raw markup
                "setup.banner.connecting": "Conectando a {em}{network}{/em}…",  # {em} beyond vocab
                "status.relative.minutes.other": "hace minutos",  # drops {n}
            },
        )
        findings = _validate.validate("es", str(es))
        joined = "\n".join(findings)
        # One defect per rule class, so dropping any single rule in the shared
        # module turns a line of this test red (not only the Stage-4 gate).
        assert "setup.banner.error_lead" in joined and "markup" in joined
        assert "setup.banner.connecting" in joined and "vocabulary" in joined
        assert "status.relative.minutes.other" in joined and "drops slot" in joined

    def test_missing_and_unknown_keys_are_reported(self, tmp_path):
        es = self._write_es(tmp_path, {})
        raw = json.loads(es.read_text(encoding="utf-8"))
        del raw["status.relative.minutes.one"]  # missing
        raw["bogus.key"] = "x"  # unknown
        es.write_text(json.dumps(raw), encoding="utf-8")
        joined = "\n".join(_validate.validate("es", str(es)))
        assert "MISSING KEY  status.relative.minutes.one" in joined
        assert "UNKNOWN KEY  bogus.key" in joined

    def test_empty_value_is_reported_without_content_noise(self, tmp_path):
        es = self._write_es(tmp_path, {"setup.banner.error_lead": "   "})
        findings = _validate.validate("es", str(es))
        error_lead = [f for f in findings if "setup.banner.error_lead" in f]
        assert error_lead == [f for f in error_lead if "EMPTY" in f], (
            f"an empty value should report EMPTY only, no content-lint noise: {error_lead}"
        )

    def test_default_registry_path_is_used_when_no_file_given(self, tmp_path):
        # The es registry entry points at languages/es/strings.json under the
        # tmp root; validate("es") with no --file must resolve THAT bundle.
        self._write_es(tmp_path, {})
        es_dir = tmp_path / "languages" / "es"
        es_dir.mkdir(parents=True)
        conforming = {
            "setup.banner.error_lead": "No se pudo conectar. {error}",
            "setup.banner.connecting": "Conectando a {b}{network}{/b}…",
            "status.relative.minutes.one": "hace un minuto",
            "status.relative.minutes.other": "hace {n} minutos",
        }
        (es_dir / "strings.json").write_text(json.dumps(conforming, ensure_ascii=False), encoding="utf-8")
        assert _validate.validate("es", None) == []

    def _unregistered_es(self, tmp_path: Path, overrides: dict) -> None:
        """An es bundle in the TREE that languages.json does not list."""
        es = self._write_es(tmp_path, overrides)
        reg = json.loads((tmp_path / "languages.json").read_text(encoding="utf-8"))
        del reg["languages"]["es"]
        (tmp_path / "languages.json").write_text(json.dumps(reg), encoding="utf-8")
        (tmp_path / "languages" / "es").mkdir()
        es.rename(tmp_path / "languages" / "es" / "strings.json")

    def test_all_finds_an_unregistered_bundle_and_fails_it(self, tmp_path, capsys):
        """T8's verify line: an unregistered broken es bundle fails CI. The
        registry-driven gate (catalog_lint.registry_errors) never sees it."""
        self._unregistered_es(tmp_path, {"setup.banner.error_lead": "No se pudo <b>conectar</b>. {error}"})
        assert catalog_lint.registry_errors(tmp_path, source_root=REPO_ROOT) == []  # the gap T8 closes
        findings = _validate.validate_all()
        assert any(c == "es" and f.startswith("setup.banner.error_lead") and "markup" in f for c, f in findings)
        assert _validate.main(["--all"]) == 1
        assert "es: setup.banner.error_lead" in capsys.readouterr().out

    def test_all_passes_a_conforming_unregistered_bundle(self, tmp_path):
        self._unregistered_es(tmp_path, {"status.relative.minutes.one": "hace un minuto"})
        assert set(_validate.bundle_files()) == {"en", "es"}
        assert [f for _c, f in _validate.validate_all() if not _validate._is_note(f)] == []

    def test_all_fails_closed_on_a_tree_with_no_english(self, tmp_path):
        (tmp_path / "languages").mkdir()
        assert _validate.validate_all() and _validate.main(["--all"]) == 1

    def test_a_directory_named_note_cannot_hide_its_failures(self, tmp_path, capsys):
        """Claude + Codex (litclock-dev#926 review): `NOTE: <finding>` read as a note,
        so a bundle under languages/NOTE/ passed whatever it held."""
        self._unregistered_es(tmp_path, {})
        (tmp_path / "languages" / "es").rename(tmp_path / "languages" / "NOTE")
        (tmp_path / "languages" / "NOTE" / "strings.json").write_text("[]", encoding="utf-8")
        assert _validate.main(["--all"]) == 1
        assert "NOTE: languages/NOTE/ is not a language code" in capsys.readouterr().out

    @pytest.mark.parametrize("name", ["pt_BR", "ES", "es "])
    def test_a_directory_that_is_not_a_language_code_fails(self, tmp_path, name):
        self._unregistered_es(tmp_path, {"status.relative.minutes.one": "hace un minuto"})
        (tmp_path / "languages" / "es").rename(tmp_path / "languages" / name)
        findings = [f for c, f in _validate.validate_all() if c == name]
        assert findings and "is not a language code" in findings[0]

    @pytest.mark.parametrize("argv", [[], ["es", "--all"], ["--all", "--file", "x.json"]])
    def test_all_and_a_code_are_exclusive(self, argv):
        with pytest.raises(SystemExit) as exc:
            _validate.main(argv)
        assert exc.value.code == 2

    def test_bom_and_non_object_root(self, tmp_path):
        es = self._write_es(tmp_path, {})
        # A BOM must be REPORTED (CI reads plain utf-8 and would reject it),
        # while the reader still parses the rest for per-key feedback.
        raw = es.read_text(encoding="utf-8")
        es.write_text("\ufeff" + raw, encoding="utf-8")
        bom_findings = _validate.validate("es", str(es))
        assert any(f.startswith("BOM") for f in bom_findings), bom_findings
        # A non-object root gets a clean shape error, not a crash.
        es.write_text("[]", encoding="utf-8")
        out = _validate.validate("es", str(es))
        assert out and "must be a JSON object" in out[0]


_ROUND_TRIP_HARNESS = r'''
import fs from "fs";
const html = fs.readFileSync(process.argv[2], "utf8");
const DATA = JSON.parse(html.match(/const DATA = (\{.*?\});\nconst values/s)[1]);
const jsSrc = html.slice(html.indexOf("const TOKEN_RE"), html.indexOf("function refresh"));
const {importBundle, exportBundle} = new Function("DATA", jsSrc + "\nreturn {importBundle, exportBundle};")(DATA);
const values = {}, extra = {};
importBundle(JSON.parse(fs.readFileSync(process.argv[3], "utf8")), values, extra);
values["setup.page.title"] = "Configuración";  // an edit in between
process.stdout.write(JSON.stringify(exportBundle("es", values, extra)));
'''


def test_the_tool_keeps_extra_farewell_lines_on_a_round_trip(tmp_path):
    """litclock-dev#923 review: a translator's sixth and seventh farewell lines have no
    row in the tool (English has five); import then export must keep them,
    and must still drop keys that are genuinely unknown."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    bundle = {"shutdown.farewell.reboot.1": "«uno»", "shutdown.farewell.reboot.6": "«seis»",
              "shutdown.farewell.poweroff.7": "«siete»", "not.a.real.key": "x"}  # fmt: skip
    (tmp_path / "in.json").write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "h.mjs").write_text(_ROUND_TRIP_HARNESS, encoding="utf-8")
    html = REPO_ROOT / "translations" / "tool" / "index.html"
    result = subprocess.run(
        [node, str(tmp_path / "h.mjs"), str(html), str(tmp_path / "in.json")],
        capture_output=True, text=True, timeout=30,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["shutdown.farewell.reboot.1"] == "«uno»"
    assert out["shutdown.farewell.reboot.6"] == "«seis»" and out["shutdown.farewell.poweroff.7"] == "«siete»"
    assert out["setup.page.title"] == "Configuración"
    assert "not.a.real.key" not in out


class TestEveryBundleInTheTree:
    """The CI gate (litclock-dev#532 T8): every ``languages/*/strings.json``
    in the checkout, registered or not, passes the validator — so a bundle
    cannot reach master unchecked by simply not being listed yet."""

    def test_every_bundle_passes(self):
        findings = _validate.validate_all()
        failures = [f"{c}: {f}" for c, f in findings if not _validate._is_note(f)]
        assert failures == [], "\n".join(failures)
        # The font check is part of the gate here: a skipped one is a NOTE to
        # a volunteer, but CI has fontTools (requirements-dev.txt) and must run it.
        assert not [f for _c, f in findings if "font check skipped" in f], findings
        assert _validate.main(["--all"]) == 0  # the hand-run form agrees

    def test_the_tree_scan_includes_english(self):
        assert "en" in _validate.bundle_files()

    def test_output_names_no_private_issue(self, tmp_path):
        """Volunteers outside this repository read the validator's output and
        the kit's README: neither may point at an issue they cannot open."""
        import re

        readme = (REPO_ROOT / "translations" / "README.md").read_text(encoding="utf-8")
        assert not re.findall(r"dev#|#\d{2,}|issue \d{2,}", readme)
        ru = tmp_path / "ru.json"
        ru.write_text('{"clock.title": "Часы"}', encoding="utf-8")
        findings = _validate.validate("ru", str(ru))
        assert any("plural forms" in f for f in findings), findings  # the NOTE that used to name one
        assert not [f for f in findings if "dev#" in f], [f for f in findings if "dev#" in f]
