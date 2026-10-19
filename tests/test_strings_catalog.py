"""litclock-dev#532 Stage 3 — the language registry, the catalog loader's
never-raise contract, and the CI completeness gate.

The gate's job (per the approved design + the 2026-08-12 scope audit):
- `languages.json` is the single source of truth; its shape is validated.
- The English catalog is the CANONICAL key set; every other language is
  diffed against it (as dicts, no thresholds — the parity-file lesson).
- Coverage is computed from the REAL corpus, SFW-only (audit item 8: the
  default device filters NSFW rows, so a language must clear the floor on
  the rows a default clock actually shows).
- Plural keys come in complete category sets per the registry's
  plural_forms — a missing `.other` would serve a raw key at runtime.
- aria.* is a reserved key CLASS (audit item 12): when aria keys exist
  they participate in the same canonical diff; the class is pinned here
  so its first use can't be silently namespace-squatted.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import strings_catalog  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_catalog(monkeypatch, tmp_path):
    monkeypatch.delenv("LITCLOCK_LANGUAGE", raising=False)
    # Isolate from the dev box's ambient env.sh channels: point the file
    # channel at a nonexistent path so only what a test sets applies.
    monkeypatch.setenv("LITCLOCK_ENV_FILE", str(tmp_path / "no-env.sh"))
    monkeypatch.delenv("LITCLOCK_DIR", raising=False)
    strings_catalog.reset_cache()
    yield
    strings_catalog.reset_cache()


def _registry() -> dict:
    return json.loads((REPO_ROOT / "languages.json").read_text(encoding="utf-8"))


def _languages() -> dict:
    return _registry()["languages"]


def _catalog_keys(entry: dict) -> set[str]:
    data = json.loads((REPO_ROOT / entry["strings"]).read_text(encoding="utf-8"))
    return {k for k in data if not k.startswith("_")}


class TestRegistryShape:
    REQUIRED = ("code", "native_name", "status", "curator", "corpus", "strings", "nsfw_tagged", "plural_forms")

    def test_registry_parses_and_has_english_active(self):
        reg = _registry()
        assert reg["fleet_default"] == "en"
        en = reg["languages"]["en"]
        assert en["status"] == "active"

    def test_every_entry_carries_required_fields(self):
        for code, entry in _languages().items():
            for field in self.REQUIRED:
                assert field in entry, f"{code} missing registry field {field!r}"
            assert entry["code"] == code
            assert entry["status"] in ("active", "incubating"), (
                f"{code}: status {entry['status']!r} — 'beta' and friends are "
                "deliberately not a thing (incomplete languages are unselectable)"
            )

    def test_every_translated_corpus_lives_under_its_language_directory(self):
        """The render-invariants workflow re-checks corpora on
        `languages/**/*.csv` only (litclock-dev#532 T4, litclock-dev#914 review): a
        translation registered anywhere else would change without the gate
        running. English's corpus is the shipped PNG source, on its own path."""
        for code, entry in _languages().items():
            if code == "en":
                continue
            rel = entry["corpus"]["path"]
            parts = Path(rel).parts
            assert parts[:2] == ("languages", code) and ".." not in parts and rel.endswith(".csv"), (
                f"{code}: corpus.path {rel!r} must be languages/{code}/<name>.csv"
            )

    def test_strings_paths_resolve_and_parse(self):
        for code, entry in _languages().items():
            path = REPO_ROOT / entry["strings"]
            assert path.is_file(), f"{code}: strings file missing at {entry['strings']}"
            json.loads(path.read_text(encoding="utf-8"))


class TestCanonicalKeySet:
    def test_every_language_matches_the_english_keys_exactly(self):
        en_keys = _catalog_keys(_languages()["en"])
        assert en_keys, "the English catalog is empty — nothing is canonical"
        for code, entry in _languages().items():
            if code == "en":
                continue
            keys = _catalog_keys(entry)
            # The numbered farewell lines are the one optional class (litclock-dev#532
            # T7): a language supplies 1..N of its own per screen.
            missing = {k for k in en_keys - keys if not catalog_lint.is_optional_key(k)}
            extra = {k for k in keys - en_keys if not catalog_lint.is_farewell_key(k)}
            assert not missing and not extra, (
                f"{code} diverges from the canonical English key set — "
                f"missing={sorted(missing)[:5]} extra={sorted(extra)[:5]}"
            )

    def test_plural_categories_are_complete_per_language(self):
        for code, entry in _languages().items():
            forms = entry["plural_forms"]
            keys = _catalog_keys(entry)
            bases = {k.rsplit(".", 1)[0] for k in keys if k.rsplit(".", 1)[-1] in forms}
            for base in bases:
                for form in forms:
                    assert f"{base}.{form}" in keys, (
                        f"{code}: plural base {base!r} missing category {form!r} — "
                        "runtime would serve a raw key"
                    )

    def test_values_are_nonempty_strings_with_no_positional_slots(self):
        # Named slots only: a translator can reorder {n}; nobody can reorder {}.
        for code, entry in _languages().items():
            data = json.loads((REPO_ROOT / entry["strings"]).read_text(encoding="utf-8"))
            for key, value in data.items():
                if key.startswith("_"):
                    continue
                if isinstance(value, str) and not value.strip() and catalog_lint.is_optional_key(key):
                    continue  # an optional farewell left blank is not supplied (litclock-dev#532 T7)
                assert isinstance(value, str) and value.strip(), f"{code}:{key} empty"
                assert "{}" not in value, f"{code}:{key} uses a positional slot"
                import re as _re  # noqa: PLC0415

                assert not _re.search(r"\{\d+\}", value), f"{code}:{key} uses a numbered slot"

    def test_aria_key_class_is_reserved(self):
        # Audit item 12: aria.* is the screen-reader key class. Pin the
        # namespace so the first aria key lands inside the canonical diff
        # (it automatically does — this documents the reservation and fails
        # if someone squats the prefix with a non-string).
        for code, entry in _languages().items():
            data = json.loads((REPO_ROOT / entry["strings"]).read_text(encoding="utf-8"))
            for key, value in data.items():
                if key.startswith("aria."):
                    assert isinstance(value, str), f"{code}:{key} aria key must be a string"


class TestCoverageGate:
    def test_active_languages_clear_the_sfw_coverage_floor(self):
        # PRODUCTION semantics, not a mirror (/review litclock-dev#738 F5: the first
        # draft's yes/true/1 predicate and row-shape threshold diverged
        # from quote_corpus in three ways) — the gate builds the same index
        # the clock reads, so it measures the corpus the clock shows.
        import quote_corpus  # noqa: PLC0415

        for code, entry in _languages().items():
            if entry["status"] != "active":
                continue
            floor = entry.get("min_coverage_pct", 80)
            corpus = REPO_ROOT / entry["corpus"]["path"]
            assert corpus.is_file(), f"{code}: corpus missing at {entry['corpus']['path']}"
            # litclock-dev#870 (litclock-dev#874 red team): measure the file the DEVICE
            # would read, through the same resolver — a path that escapes the
            # checkout, or an empty file, is refused on the device and must
            # not pass here as a healthy translation.
            assert quote_corpus._registry_corpus(code) == corpus, (
                f"{code}: the device's resolver refuses {entry['corpus']['path']} (outside the checkout, or empty)"
            )
            quote_corpus.reset_cache()
            index = quote_corpus._index(corpus)
            sfw_minutes = {
                hhmm
                for hhmm, rows_in_bucket in index.items()
                if any(not r["is_nsfw"] for r in rows_in_bucket)
            }
            rows = sum(len(v) for v in index.values())
            coverage = len(sfw_minutes) / 1440 * 100
            assert coverage >= floor, (
                f"{code}: SFW coverage {coverage:.1f}% below the {floor}% activation floor "
                "(audit item 8 — NSFW rows must not count toward activation)"
            )
            assert rows == entry["corpus"]["rows"], (
                f"{code}: registry says {entry['corpus']['rows']} corpus rows, production "
                f"ingests {rows} — the registry is the single source of truth; keep it true"
            )
            recorded = entry["corpus"].get("sfw_coverage_pct")
            assert recorded is not None and abs(coverage - recorded) < 0.1, (
                f"{code}: registry records sfw_coverage_pct={recorded}, measured {coverage:.2f} — "
                "decorative numbers rot; keep the recorded value true"
            )


class TestLoaderContract:
    def test_known_key_resolves(self):
        assert strings_catalog.get("status.relative.just_now") == "just now"

    def test_plural_selects_whole_sentences(self):
        assert strings_catalog.plural("status.relative.minutes", 1) == "1 minute ago"
        assert strings_catalog.plural("status.relative.minutes", 5) == "5 minutes ago"

    def test_missing_key_serves_the_key_never_raises(self):
        assert strings_catalog.get("no.such.key") == "no.such.key"

    def test_unknown_language_degrades_to_english(self, monkeypatch):
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "zz")
        strings_catalog.reset_cache()
        assert strings_catalog.plural("status.relative.hours", 2) == "2 hours ago"

    def test_corrupt_catalog_never_raises(self, tmp_path, monkeypatch):
        # Point the loader at a broken tree: registry unreadable → empty →
        # every lookup degrades to the key. The screen stays up (audit item 7).
        monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", tmp_path / "nope.json")
        strings_catalog.reset_cache()
        assert strings_catalog.get("status.relative.just_now") == "status.relative.just_now"

    @pytest.mark.parametrize("languages", ['["en", "es"]', '"es"', "7"], ids=["list", "string", "int"])
    def test_a_wrong_shaped_registry_degrades_never_raises(self, tmp_path, monkeypatch, languages):
        """litclock-dev#532 review: valid JSON whose `languages` is not a
        mapping used to raise AttributeError from every reader — and the
        painter resolves the language every minute."""
        reg = tmp_path / "languages.json"
        reg.write_text(f'{{"fleet_default": "en", "languages": {languages}}}')
        monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", reg)
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "es")
        strings_catalog.reset_cache()
        assert strings_catalog.active_language() == "en"
        assert strings_catalog.active_codes() == {"en"}
        assert strings_catalog.corpus_relpath("es") is None
        assert strings_catalog.registry_readable() is False
        assert strings_catalog.quote_language() is None

    def test_an_unreadable_registry_does_not_log_using_english(self, tmp_path, monkeypatch, caplog):
        """Round-2 review: the journal said "using English" just before the
        painter refused English."""
        import logging

        monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", tmp_path / "nope.json")
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "es")
        strings_catalog.reset_cache()
        with caplog.at_level(logging.WARNING, logger="strings_catalog"):
            assert strings_catalog.quote_language() is None
            strings_catalog.get_own("clock.fallback.failure")  # what the failure frame does next
        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "using English" not in messages and "painting no quotes" in messages

    def test_quote_language_trusts_a_readable_registry(self, monkeypatch):
        """The control: a readable registry rejecting the code is the
        device-level rule (English), not the damaged case."""
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "zz")
        strings_catalog.reset_cache()
        assert strings_catalog.quote_language() == "en"

    def test_get_own_never_serves_another_language(self, tmp_path, monkeypatch):
        _multilang_registry(tmp_path, monkeypatch)
        monkeypatch.setattr(strings_catalog, "_REPO_ROOT", tmp_path)
        (tmp_path / "languages" / "en").mkdir(parents=True)
        (tmp_path / "languages" / "de").mkdir(parents=True)
        (tmp_path / "languages" / "en" / "strings.json").write_text('{"a": "English A", "b": "English B"}')
        (tmp_path / "languages" / "de" / "strings.json").write_text('{"a": "Deutsch A"}')
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "de")
        strings_catalog.reset_cache()
        # Incomplete (lacks "b"): unusable WHOLE since RT1 — get() serves the
        # fleet default for every key, never a mix, and get_own() nothing.
        assert strings_catalog.get("a") == "English A"
        assert strings_catalog.get("b") == "English B"
        assert strings_catalog.get_own("a") is None
        (tmp_path / "languages" / "de" / "strings.json").write_text('{"a": "Deutsch A", "b": "Deutsch B"}')
        strings_catalog.reset_cache()
        assert strings_catalog.get_own("a") == "Deutsch A"
        assert strings_catalog.get("b") == "Deutsch B"

    def test_slot_fill_is_inert_to_braces_in_values(self):
        # The dumb-replace contract: a slot value containing braces must not
        # recurse or raise (the reason format() is not used).
        out = strings_catalog.get("status.relative.minutes.other", n="{n}{weird}")
        assert out == "{n}{weird} minutes ago"

    def test_a_value_reading_like_a_later_slot_is_not_filled_again(self):
        # litclock-dev#532 T6 review: slot-by-slot replacement re-filled a
        # value that read like a slot filled AFTER it, so a setup SSID
        # "My{ip_url}Net" became a different network name in the steps.
        assert strings_catalog._fill("{ssid} / {ip_url}", {"ssid": "My{ip_url}Net", "ip_url": "http://x"}) == (
            "My{ip_url}Net / http://x"
        )
        # Either order: a value reading like an EARLIER slot is safe too.
        assert strings_catalog._fill("{a} {b}", {"b": "{a}", "a": "A"}) == "A {a}"
        # A name with no value stays as written.
        assert strings_catalog._fill("{b}x{/b} {gone}", {"n": 1}) == "{b}x{/b} {gone}"

    def test_stray_braces_in_template_never_raise(self):
        # A translated template with a literal unmatched brace must pass
        # through untouched rather than raising (str.format would).
        assert strings_catalog._fill("100% {n} done {", {"n": 3}) == "100% 3 done {"


class TestShellSurface:
    """The bash string-lookup path (audit's missed FIFTH hosting context):
    scripts paint panel text via eink_display.py subprocess calls, so
    `catalog-get` is how shell reaches the catalog without parsing JSON."""

    def _run(self, *argv, env=None):
        import os
        import subprocess

        merged = dict(os.environ)
        merged.pop("LANGUAGE", None)
        if env:
            merged.update(env)
        return subprocess.run(
            [sys.executable, str(_SRC / "eink_display.py"), "catalog-get", *argv],
            capture_output=True,
            text=True,
            timeout=30,
            env=merged,
        )

    def test_resolves_key_with_slot(self):
        result = self._run("status.relative.minutes.other", "--slot", "n=7")
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "7 minutes ago"

    def test_missing_key_prints_key_and_exits_zero(self):
        # Shell callers must never die on a catalog gap — the key itself is
        # the visible-but-safe degradation.
        result = self._run("no.such.key")
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "no.such.key"


class TestCatalogGetContractHoldsWhenTheModuleItselfIsBroken:
    """litclock-dev#766 item 3 — `scripts/update.sh` documents "catalog-get
    always exits 0 by contract", and the class above proves that for a missing
    KEY. It was false for a missing or broken MODULE: the subcommand's own
    `import strings_catalog` was unguarded while its twin
    `_resolve_status_parts` wraps the same import in a broad `except Exception`,
    with a docstring explaining why (a half-deployed venv can carry a module
    that IMPORTS but is stale or truncated).

    A device whose checkout lost or truncated `strings_catalog.py` therefore got
    a traceback, exit 1, and EMPTY stdout — no way for a caller to tell "broken"
    from "resolved to nothing". No live impact, because the smoke gate compares
    stdout rather than trusting the exit code, but it is a trap for the next
    caller that believes the documented contract.

    Scope of the claim, stated honestly: the contract is "exits 0 with a value
    whenever stdout is writable". A closed pipe or a failing write still exits
    non-zero, as it did before — the resolution is guarded, the emission is not,
    and conflating them would mislabel a stdout failure as a catalog failure.

    Driven through a real checkout copy so the failure is the real one:
    monkeypatching an ImportError would prove the `except` clause catches
    ImportError, not that a device in this state gets a usable answer.
    """

    KEY = "status.relative.just_now"

    def _checkout(self, tmp_path, catalog_source: str | None):
        import shutil

        root = tmp_path / "checkout"
        (root / "src").mkdir(parents=True)
        for module in sorted(REPO_ROOT.glob("src/*.py")):
            shutil.copy2(module, root / "src" / module.name)
        shutil.copytree(REPO_ROOT / "languages", root / "languages")
        shutil.copy2(REPO_ROOT / "languages.json", root / "languages.json")
        target = root / "src" / "strings_catalog.py"
        if catalog_source is None:
            target.unlink()
        else:
            target.write_text(catalog_source, encoding="utf-8")
        return root

    def _run(self, root):
        import os
        import subprocess

        # PYTHONPATH is dropped along with LITCLOCK_*, and `-E` makes that
        # stick against any other channel. A CI or dev shell exporting
        # `PYTHONPATH=<repo>/src` would otherwise resolve the REAL
        # strings_catalog for the "missing" case and fail this for an
        # environment reason (measured: 2 failed under that env).
        #
        # `-E`, not `-I`: isolated mode also drops the user site directory,
        # where this box's Pillow lives, so eink_display.py could not import at
        # all and every case failed on `No module named 'PIL'`.
        return subprocess.run(
            [sys.executable, "-E", "src/eink_display.py", "catalog-get", self.KEY],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
            env={k: v for k, v in os.environ.items() if not k.startswith(("LITCLOCK_", "PYTHON"))},
        )

    def test_a_healthy_checkout_resolves_it(self, tmp_path):
        """The baseline the cases below are measured against. Without it a
        harness that silently failed to build a usable checkout would make every
        assertion pass for the wrong reason."""
        r = self._run(self._checkout(tmp_path, (REPO_ROOT / "src" / "strings_catalog.py").read_text()))
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "just now"

    @pytest.mark.parametrize(
        "label,source",
        [
            ("missing", None),
            ("syntactically broken", "def broken(:\n"),
            # IMPORTS fine, then fails at the call — the case the twin's
            # docstring singles out, and the one a plain `except ImportError`
            # would let through.
            ("truncated to a stub", "# nothing here\n"),
        ],
    )
    def test_the_contract_holds(self, tmp_path, label, source):
        r = self._run(self._checkout(tmp_path, source))
        assert r.returncode == 0, (
            f"catalog-get must exit 0 with a {label} strings_catalog — update.sh "
            f"documents that as its contract\n{r.stdout}\n{r.stderr}"
        )
        assert r.stdout.strip() == self.KEY, (
            "it must print the KEY, not nothing. An empty stdout gives the caller no "
            f"way to tell 'broken' from 'resolved to an empty string'\n{r.stderr}"
        )
        assert "catalog resolution failed" in r.stderr, (
            "the degradation must be visible on stderr — silent is how it stays broken"
        )

    @staticmethod
    def _fd2_closed():
        import os

        os.close(2)

    @staticmethod
    def _fd2_broken_pipe():
        import os

        r, w = os.pipe()
        os.close(r)
        os.dup2(w, 2)

    @pytest.mark.parametrize("stderr_state", ["closed-at-startup", "broken-pipe"])
    @pytest.mark.parametrize("subcommand,fallback", [("catalog-get", KEY), ("catalog-count", "0")])
    def test_the_fallback_note_cannot_cost_the_value(self, tmp_path, stderr_state, subcommand, fallback):
        """PR litclock-dev#851 review (Codex 4): the fallback arm's stderr note was a bare
        `print(..., file=sys.stderr)`. On a broken stderr it raised BEFORE the
        fallback value was assigned — empty stdout, exit 1, on the exact path
        whose contract is "always exits 0 with a value". And with fd 2 closed
        at startup `sys.stderr` is None, where `print(file=None)` means STDOUT:
        the note landed on the stream the OTA gate compares.

        Both states, both subcommands, for real: `preexec_fn` rearranges fd 2
        in the child before the interpreter starts.
        """
        import os
        import subprocess

        root = self._checkout(tmp_path, None)  # strings_catalog.py missing -> the except arm fires
        argv = [subcommand, self.KEY] if subcommand == "catalog-get" else [subcommand]
        r = subprocess.run(
            [sys.executable, "-E", "src/eink_display.py", *argv],
            cwd=root,
            stdout=subprocess.PIPE,
            text=True,
            timeout=60,
            env={k: v for k, v in os.environ.items() if not k.startswith(("LITCLOCK_", "PYTHON"))},
            preexec_fn=self._fd2_closed if stderr_state == "closed-at-startup" else self._fd2_broken_pipe,
        )
        assert r.returncode == 0, f"{subcommand} with stderr {stderr_state} exited {r.returncode}"
        assert r.stdout.strip() == fallback, (
            f"{subcommand} with stderr {stderr_state} printed {r.stdout!r} — the note leaked onto "
            "stdout or the value was lost"
        )

    def test_the_smoke_gate_still_catches_it(self, tmp_path):
        """The contract must not turn the OTA gate off. Printing the key is
        exactly what that gate compares against, so a broken module still
        mismatches 'just now' and still reverts."""
        r = self._run(self._checkout(tmp_path, None))
        assert r.stdout.strip() != "just now"

    @pytest.mark.parametrize(
        "subcommand,resolver",
        [
            ("catalog-get", "strings_catalog.get"),
            # litclock-dev#773 item 2 — catalog-count carries the SAME contract
            # (update.sh compares its stdout too), so it gets the same check
            # rather than a weaker one of its own.
            # litclock-dev#840: through the PUBLIC catalog_size — the gate
            # used to reach into `_catalog`, and a rename there would have
            # printed 0 and failed every update closed.
            ("catalog-count", "strings_catalog.catalog_size"),
        ],
    )
    def test_only_resolution_is_guarded_not_emission(self, subcommand, resolver):
        """The `print` must sit OUTSIDE the try.

        With it inside, a stdout write failure (a closed pipe, an encoding error
        under a restrictive PYTHONIOENCODING) is caught and mislabelled a
        catalog failure, and can emit a partial line and then the fallback on
        top of it. Source-level because the failure needs a hostile stdout that
        a subprocess test cannot arrange without becoming a test of pipes.
        """
        import ast

        src = (REPO_ROOT / "src" / "eink_display.py").read_text(encoding="utf-8")
        start = src.index(f'elif args.command == "{subcommand}":')
        # End at whatever the NEXT dispatch arm is, rather than naming one.
        # Naming "clear" meant inserting an arm between them silently widened
        # the slice, and the widened text failed to re-indent — which is how
        # litclock-dev#773's catalog-count arm broke this test.
        end = src.index("elif args.command ==", start + len("elif args.command =="))
        block = ast.unparse(ast.parse("if True:\n" + "\n".join(
            "    " + ln for ln in src[start:end].splitlines()[1:]
        )))
        assert resolver in block, f"the {subcommand} block no longer resolves anything"
        try_at = block.index("try:")
        except_at = block.index("except Exception")
        resolve_at = block.index(resolver)
        assert try_at < resolve_at < except_at, "resolution must be inside the guard"
        assert block.rindex("print(") > except_at, (
            "the final print must be OUTSIDE the guard — guarding the emission mislabels a "
            "stdout failure as a catalog failure and can double-write"
        )


class TestCatalogSize:
    """litclock-dev#840 — `catalog_size(code)` is the public face of the count
    the OTA smoke gate depends on (`eink_display.py catalog-count`,
    litclock-dev#773 item 2). Its contract: the LOADER's view, never raises,
    0 for anything that cannot be served."""

    @staticmethod
    def _loaded_count(entry: dict) -> int:
        data = json.loads((REPO_ROOT / entry["strings"]).read_text(encoding="utf-8"))
        return len([k for k, v in data.items() if isinstance(v, str) and not k.startswith("_")])

    def test_english_counts_what_the_loader_serves(self):
        expected = self._loaded_count(_languages()["en"])
        assert expected > 0
        assert strings_catalog.catalog_size("en") == expected

    @pytest.mark.parametrize("code", ["nope", "", "EN"])
    def test_a_code_the_registry_does_not_list_is_zero_not_an_error(self, code):
        """0 is the honest answer AND below any sane floor, so a gate reading
        it fails closed. It must not raise: `catalog-count` guards the call,
        but the contract belongs to the function, not to one caller."""
        assert strings_catalog.catalog_size(code) == 0

    def test_it_measures_after_the_loader_filters(self, monkeypatch, tmp_path):
        """`_catalog` drops `_`-prefixed keys and non-string values. A count
        off the raw JSON would be wrong by exactly those, and the gate has to
        measure what `get()` can actually resolve."""
        bundle = tmp_path / "languages" / "zz" / "strings.json"
        bundle.parent.mkdir(parents=True)
        bundle.write_text(
            json.dumps({"_meta": "dropped", "a.one": "1", "a.two": "2", "a.count": 3}),
            encoding="utf-8",
        )
        registry = tmp_path / "languages.json"
        registry.write_text(
            json.dumps({"languages": {"zz": {"status": "active", "strings": "languages/zz/strings.json"}}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", registry)
        monkeypatch.setattr(strings_catalog, "_REPO_ROOT", tmp_path)
        strings_catalog.reset_cache()
        assert strings_catalog.catalog_size("zz") == 2

    def test_a_registered_language_whose_bundle_is_unloadable_is_zero(self, monkeypatch, tmp_path):
        registry = tmp_path / "languages.json"
        registry.write_text(
            json.dumps({"languages": {"zz": {"status": "active", "strings": "languages/zz/missing.json"}}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", registry)
        monkeypatch.setattr(strings_catalog, "_REPO_ROOT", tmp_path)
        strings_catalog.reset_cache()
        assert strings_catalog.catalog_size("zz") == 0


class TestEnvFileChannel:
    """/review litclock-dev#738 F3: litclock-control.service never sources env.sh — the
    loader must read the FILE, and re-read it on mtime change so a PWA
    language save reaches a running server without a restart."""

    def _env_file(self, tmp_path, monkeypatch, content: str):
        env = tmp_path / "env.sh"
        env.write_text(content)
        monkeypatch.setenv("LITCLOCK_ENV_FILE", str(env))
        strings_catalog.reset_cache()
        return env

    def test_language_read_from_env_file(self, tmp_path, monkeypatch):
        self._env_file(tmp_path, monkeypatch, "export LITCLOCK_LANGUAGE=en\n")
        assert strings_catalog.active_language() == "en"

    def test_process_env_overrides_file(self, tmp_path, monkeypatch):
        self._env_file(tmp_path, monkeypatch, "export LITCLOCK_LANGUAGE=zz\n")
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "en")
        assert strings_catalog.active_language() == "en"

    def test_mtime_change_is_picked_up_without_reset(self, tmp_path, monkeypatch):
        import os as _os

        env = self._env_file(tmp_path, monkeypatch, "export LITCLOCK_LANGUAGE=zz\n")
        assert strings_catalog.active_language() == "en"  # zz inactive → en
        env.write_text("export LITCLOCK_LANGUAGE=en\n")
        _os.utime(env, (env.stat().st_atime, env.stat().st_mtime + 10))
        # No reset_cache(): the mtime check alone must pick it up.
        assert strings_catalog.active_language() == "en"

    def test_posix_language_variable_is_ignored(self, monkeypatch):
        # The glibc locale-priority variable an SSH session forwards must
        # not reach language selection at all (/review litclock-dev#738 F2).
        monkeypatch.setenv("LANGUAGE", "de_DE:de")
        strings_catalog.reset_cache()
        assert strings_catalog.active_language() == "en"


class TestDegradationHygiene:
    def test_warning_fires_once_per_missing_key(self, caplog):
        import logging as logging_mod

        with caplog.at_level(logging_mod.WARNING, logger="strings_catalog"):
            strings_catalog.get("gone.key")
            strings_catalog.get("gone.key")
            strings_catalog.get("gone.key")
        hits = [r for r in caplog.records if "gone.key" in str(r.args)]
        assert len(hits) == 1, (
            f"missing-key warning must fire once per key, not per lookup: {len(hits)} "
            "(/review litclock-dev#738 — status polls every 30s forever)"
        )

    def test_transient_load_failure_heals_after_ttl(self, tmp_path, monkeypatch):
        # /review litclock-dev#738 F4: a corrupt read during the OTA window must not
        # wedge the process on raw keys for its lifetime.
        reg = tmp_path / "languages.json"
        strings_dir = tmp_path / "languages" / "xx"
        strings_dir.mkdir(parents=True)
        strings_file = strings_dir / "strings.json"
        strings_file.write_text("{ corrupt")
        reg.write_text(json.dumps({
            "languages": {
                "xx": {"code": "xx", "status": "active", "strings": "languages/xx/strings.json"},
                "en": {"code": "en", "status": "active", "strings": "languages/xx/strings.json"},
            }
        }))
        monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", reg)
        monkeypatch.setattr(strings_catalog, "_REPO_ROOT", tmp_path)
        monkeypatch.setattr(strings_catalog, "NEGATIVE_CACHE_TTL_S", 0.0)
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "xx")
        strings_catalog.reset_cache()
        assert strings_catalog.get("k") == "k"  # corrupt → degrade
        strings_file.write_text(json.dumps({"k": "healed"}))
        assert strings_catalog.get("k") == "healed", (
            "a healed catalog must be picked up after the negative-cache TTL"
        )

    def test_incubating_language_is_unselectable(self, tmp_path, monkeypatch):
        # Mutant-killable status gate (/review litclock-dev#738 F6): a registry entry
        # that EXISTS with a real catalog but status=incubating must refuse.
        reg = tmp_path / "languages.json"
        d = tmp_path / "languages" / "xx"
        d.mkdir(parents=True)
        (d / "strings.json").write_text(json.dumps({"k": "xx-value"}))
        reg.write_text(json.dumps({
            "languages": {
                "xx": {"code": "xx", "status": "incubating", "strings": "languages/xx/strings.json"},
            }
        }))
        monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", reg)
        monkeypatch.setattr(strings_catalog, "_REPO_ROOT", tmp_path)
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "xx")
        strings_catalog.reset_cache()
        assert strings_catalog.active_language() == "en", (
            "incubating languages are unselectable by contract — an incomplete "
            "bundle must never be served"
        )


class TestOtaSmokeCoversTheCatalog:
    def test_update_sh_smoke_asserts_a_resolved_string(self):
        # /review litclock-dev#738 (codex): a half-applied OTA missing languages/ passed
        # smoke and shipped raw keys. The smoke must ask the catalog for a
        # known key and require a NON-degraded answer.
        body = (REPO_ROOT / "scripts" / "update.sh").read_text()
        code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
        assert "catalog-get status.relative.just_now" in code, (
            "update.sh smoke lost the catalog probe (litclock-dev#532 /review litclock-dev#738)"
        )
        assert '= "just now"' in code, (
            "the smoke must compare against the RESOLVED value — exit codes can't "
            "discriminate (catalog-get always exits 0)"
        )
        # And the comparison must guard the SAME probe: both live between the
        # dry-run smoke and the smoke_rc dispatch (ordering, comments stripped).
        probe_idx = code.index("catalog-get status.relative.just_now")
        cmp_idx = code.index('= "just now"')
        assert 0 < cmp_idx - probe_idx < 400, "probe and comparison drifted apart"


def _multilang_registry(tmp_path, monkeypatch, extra_status="active"):
    reg = tmp_path / "languages.json"
    reg.write_text(json.dumps({
        "fleet_default": "en",
        "languages": {
            "en": {"code": "en", "native_name": "English", "status": "active",
                   "strings": "languages/en/strings.json"},
            "de": {"code": "de", "native_name": "Deutsch", "status": extra_status,
                   "strings": "languages/de/strings.json"},
        },
    }))
    monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", reg)
    strings_catalog.reset_cache()


class TestNegotiate:
    """litclock-dev#532 pickers: Accept-Language → best ACTIVE language."""

    def test_q_ordering_picks_the_active_match(self, tmp_path, monkeypatch):
        _multilang_registry(tmp_path, monkeypatch)
        assert strings_catalog.negotiate("de-DE,de;q=0.9,en;q=0.5") == "de"
        assert strings_catalog.negotiate("en;q=0.9,de;q=0.5") == "en"

    def test_primary_subtag_matching(self, tmp_path, monkeypatch):
        _multilang_registry(tmp_path, monkeypatch)
        assert strings_catalog.negotiate("de-AT") == "de"

    def test_inactive_language_never_negotiates(self, tmp_path, monkeypatch):
        _multilang_registry(tmp_path, monkeypatch, extra_status="incubating")
        assert strings_catalog.negotiate("de-DE,de;q=0.9") == "en"

    def test_unknown_empty_and_wildcard_default_to_english(self):
        assert strings_catalog.negotiate("zz-ZZ;q=1.0") == "en"
        assert strings_catalog.negotiate("") == "en"
        assert strings_catalog.negotiate(None) == "en"
        assert strings_catalog.negotiate("*") == "en"

    def test_malformed_q_is_treated_as_full_weight(self, tmp_path, monkeypatch):
        # Pinned (/review litclock-dev#742): an unparseable q falls back to the implicit
        # 1.0 — generous beats dropping the user's first-listed language.
        # Both unparseable shapes behave identically (follow-up F7).
        _multilang_registry(tmp_path, monkeypatch)
        assert strings_catalog.negotiate("de;q=banana,en") == "de"
        assert strings_catalog.negotiate("de;q=1.2.3,en") == "de"

    def test_q_zero_means_not_acceptable(self, tmp_path, monkeypatch):
        # RFC 9110: q=0 excludes the language (follow-up F7).
        _multilang_registry(tmp_path, monkeypatch)
        assert strings_catalog.negotiate("de;q=0,en;q=0.5") == "en"
        assert strings_catalog.negotiate("de;q=0") == "en"

    def test_active_codes_always_contains_english(self, tmp_path, monkeypatch):
        reg = tmp_path / "languages.json"
        reg.write_text(json.dumps({"languages": {}}))
        monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", reg)
        strings_catalog.reset_cache()
        assert "en" in strings_catalog.active_codes()


class TestLanguageValidator:
    def test_active_code_accepted_inactive_rejected(self, tmp_path, monkeypatch):
        _multilang_registry(tmp_path, monkeypatch, extra_status="incubating")
        import config  # noqa: PLC0415

        ok, _ = config.validate_setting("LITCLOCK_LANGUAGE", "en")
        assert ok
        ok, msg = config.validate_setting("LITCLOCK_LANGUAGE", "de")
        assert not ok and "active" in msg
        ok, _ = config.validate_setting("LITCLOCK_LANGUAGE", "")
        assert not ok


class TestGetTriplet:
    """litclock-dev#532 bulk extraction: the bash splash surfaces resolve
    (title, message, submessage) triplets in one process."""

    def test_resolves_all_three_parts(self):
        t, m, sub = strings_catalog.get_triplet("firstboot.splash.setup_failed")
        assert t == "Setup Failed"
        assert m == "Unplug power for 10 seconds"
        assert sub == "Then plug back in"

    def test_absent_part_key_means_intentionally_blank(self):
        """The canonical gate forbids empty VALUES, so blank parts are
        expressed by key absence — resolves to '', never the key."""
        t, m, sub = strings_catalog.get_triplet("firstboot.splash.setup_complete")
        assert t == "Setup Complete!"
        assert m == "Starting your clock..."
        assert sub == ""

    def test_slots_fill_in_every_part(self):
        t, m, sub = strings_catalog.get_triplet(
            "firstboot.splash.wifi_retry", attempt=2, max=5
        )
        assert m == "Retrying... (2/5)"
        assert t == "Setup WiFi Failed"

    def test_en_load_failure_serves_keys_not_a_blank_splash(self, monkeypatch, tmp_path):
        """Absence proves 'intentionally blank' only when en LOADED. A
        corrupt bundle must degrade to visible keys, not an empty panel."""
        monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", tmp_path / "nope.json")
        strings_catalog.reset_cache()
        t, m, sub = strings_catalog.get_triplet("firstboot.splash.preparing")
        assert t == "firstboot.splash.preparing.title"
        assert sub == "firstboot.splash.preparing.submessage"


class TestBashTripletParity:
    """The extraction must not change a single painted byte: every catalog
    value equals the literal it replaced in scripts/first-boot.sh and
    scripts/boot-splash.sh (pre-litclock-dev#532-bulk-extraction copy)."""

    FORMER_LITERALS = {
        "boot.splash.starting": ("LitClock", "Starting...", None),
        "firstboot.splash.preparing": ("Setup", "LitClock", "Preparing setup..."),
        "firstboot.splash.wifi_connected": ("WiFi Connected", "Network: {ssid}", None),
        "firstboot.splash.ntp_sync": ("Syncing Time", "Setting clock via NTP...", None),
        "firstboot.splash.detecting_location": (
            "Setting Up",
            "Detecting your location...",
            "No action needed",
        ),
        "firstboot.splash.wifi_retry": (
            "Setup WiFi Failed",
            "Retrying... ({attempt}/{max})",
            "Please wait",
        ),
        "firstboot.splash.setup_failed": (
            "Setup Failed",
            "Unplug power for 10 seconds",
            "Then plug back in",
        ),
        "firstboot.splash.setup_complete": ("Setup Complete!", "Starting your clock...", None),
        "firstboot.splash.setup_incomplete": (
            "Setup Incomplete",
            "Restart to try again",
            "Unplug, then plug back in",
        ),
        "firstboot.splash.recovering": (
            "Setup Problem",
            "Trying to recover...",
            "If this persists: unplug, then plug back in",
        ),
        # slice 2 — the shutdown/doom splashes. None = the part is a
        # runtime literal (curated quote / custom gift title) or absent.
        "shutdown.splash.welcome": (
            "Welcome to LitClock",
            "1. Plug in power\n2. Connect to LitClock-Setup WiFi when prompted\n"
            "3. Be patient — first boot takes a moment :)",
            None,
        ),
        "shutdown.splash.reboot": ("LitClock", "Restarting...", None),
        "shutdown.splash.poweroff": ("Powered Off", None, "LitClock"),
        "bootcheck.splash.gave_up": (
            "Recovery failed",
            "The clock couldn't repair itself after an update.",
            "Please re-flash the SD card — see the LitClock docs.",
        ),
        "reset.splash.failed": (
            "Reset did not finish",
            "This clock may still hold its previous owner's settings.",
            "Do NOT pass it on. Power it off and on, then try Factory reset again.",
        ),
    }

    def test_catalog_values_match_the_former_literals(self):
        catalog = _catalog_json()
        for prefix, (title, message, submessage) in self.FORMER_LITERALS.items():
            assert catalog.get(prefix + ".title") == title, prefix
            if message is None:
                assert prefix + ".message" not in catalog, (
                    f"{prefix}: runtime-literal part must be ABSENT from the catalog"
                )
            else:
                assert catalog.get(prefix + ".message") == message, prefix
            if submessage is None:
                assert prefix + ".submessage" not in catalog, (
                    f"{prefix}: blank part must be ABSENT (the gate forbids empty values)"
                )
            else:
                assert catalog.get(prefix + ".submessage") == submessage, prefix

    def test_every_painted_prefix_is_in_this_table(self):
        """A new display_message site must land here too — the table IS the
        byte-parity guarantee's coverage."""
        import re as _re

        for script in (
            "first-boot.sh",
            "boot-splash.sh",
            "shutdown-splash.sh",
            "litclock-bootcheck-giveup-splash.sh",
            "litclock-reset-failed-splash.sh",
        ):
            body = (REPO_ROOT / "scripts" / script).read_text(encoding="utf-8")
            code = "\n".join(
                ln for ln in body.splitlines() if not ln.lstrip().startswith("#")
            )
            for m in _re.finditer(
                # Prefixes always contain dots — this keeps prose out of the
                # capture — and the lookahead refuses a trailing dot, so a
                # call site like "recovering." FAILS the sweep instead of
                # matching its clean prefix while runtime serves dotted keys
                # (testing /review: the sweep must observe the exact token).
                # Third alternation: shutdown-splash passes its prefix via
                # a PREFIX="..." variable — capture the assignment too.
                r"(?:display_message(?:_strict)?\s+|--catalog-prefix\s+|PREFIX=\")"
                r"([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)(?![.\w])",
                code,
            ):
                assert m.group(1) in self.FORMER_LITERALS, (
                    f"{script} paints {m.group(1)} — add it to the parity table"
                )
            if script == "shutdown-splash.sh":
                # Anti-vacuity: this file's prefixes ride the PREFIX= form —
                # the sweep must actually see all three arms.
                found = _re.findall(r"PREFIX=\"([a-z0-9_.]+)\"", code)
                assert sorted(found) == [
                    "shutdown.splash.poweroff",
                    "shutdown.splash.reboot",
                    "shutdown.splash.welcome",
                ], found
            # Companion sweep: EVERY display_message first argument must be a
            # dotted prefix (or a $variable) — a dotless typo matches no
            # regex above and would otherwise evade the table entirely.
            for m in _re.finditer(r"display_message(?:_strict)?\s+(\S+)", code):
                # Shell statement punctuation is not part of the token
                # (`if display_message_strict x; then`); a trailing DOT
                # stays — that's the malformed shape this sweep exists for.
                arg = m.group(1).rstrip(";")
                if arg.startswith("$") or arg.startswith('"$'):
                    continue
                assert _re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+", arg), (
                    f"{script}: display_message argument {arg!r} is not a well-formed catalog prefix"
                )


def _catalog_json() -> dict:
    import json

    return json.loads((REPO_ROOT / "languages" / "en" / "strings.json").read_text(encoding="utf-8"))


class TestGetTripletCallerMistakes:
    def test_trailing_dot_prefix_serves_keys_not_blank(self):
        t, m, sub = strings_catalog.get_triplet("firstboot.splash.recovering.")
        assert t == "firstboot.splash.recovering..title"  # visible, diagnosable

    def test_dotless_prefix_serves_keys_not_blank(self):
        t, _m, _s = strings_catalog.get_triplet("firstboot_splash_recovering")
        assert t == "firstboot_splash_recovering.title"

    def test_slot_named_prefix_or_key_never_raises(self):
        """Positional-only params: a --slot literally named 'prefix'/'key'
        must fill (or no-op), never TypeError through the never-raise
        contract."""
        t, _m, _s = strings_catalog.get_triplet("firstboot.splash.preparing", prefix="x", key="y")
        assert t == "Setup"
        assert strings_catalog.get("status.relative.just_now", key="z") == "just now"


class TestGetTripletUnknownPrefix:
    def test_unknown_prefix_serves_keys_not_a_blank_frame(self, tmp_path, monkeypatch):
        """Codex slice-1 /review: a LOADED en catalog missing an entire
        prefix is a typo/partial OTA, not a design choice — all three
        parts must degrade to visible keys, never ('', '', '') (a blank
        strict recovery splash would latch as success)."""
        import json as _json

        es_free = tmp_path / "strings.json"
        es_free.write_text(_json.dumps({"some.other.key": "x"}), encoding="utf-8")
        reg = tmp_path / "languages.json"
        reg.write_text(
            _json.dumps(
                {
                    "languages": {
                        "en": {"code": "en", "status": "active", "strings": str(es_free)}
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", reg)
        strings_catalog.reset_cache()
        t, m, sub = strings_catalog.get_triplet("firstboot.splash.setup_incomplete")
        assert t == "firstboot.splash.setup_incomplete.title"
        assert m == "firstboot.splash.setup_incomplete.message"
        assert sub == "firstboot.splash.setup_incomplete.submessage"


class TestDoomPathImportDegradation:
    def test_missing_strings_catalog_module_paints_key_names(self, monkeypatch):
        """The bootcheck/reset-failed splashes share a venv that may be
        half-deployed: a missing strings_catalog MODULE must degrade to
        painting the visible key names, never kill the one paint that
        outlives a frozen PWA (litclock-dev#532 slice 2)."""
        import argparse
        import sys as _sys

        _eink_dir = str(REPO_ROOT / "src")
        if _eink_dir not in _sys.path:
            _sys.path.insert(0, _eink_dir)
        import eink_display

        # sys.modules[name] = None makes `import name` raise ImportError.
        monkeypatch.setitem(_sys.modules, "strings_catalog", None)
        args = argparse.Namespace(
            catalog_prefix="reset.splash.failed",
            title=None,
            message=None,
            submessage=None,
            slot=[],
        )
        t, m, sub = eink_display._resolve_status_parts(args)
        assert t == "reset.splash.failed.title"
        assert m == "reset.splash.failed.message"
        assert sub == "reset.splash.failed.submessage"

    def test_stale_module_without_get_triplet_paints_key_names(self, monkeypatch):
        """A half-deployed venv can carry a strings_catalog that IMPORTS
        but is stale (no get_triplet) — AttributeError must degrade to key
        names identically, not kill the doom-path paint."""
        import argparse
        import sys as _sys
        import types

        _eink_dir = str(REPO_ROOT / "src")
        if _eink_dir not in _sys.path:
            _sys.path.insert(0, _eink_dir)
        import eink_display

        stale = types.ModuleType("strings_catalog")  # no attributes at all
        monkeypatch.setitem(_sys.modules, "strings_catalog", stale)
        args = argparse.Namespace(
            catalog_prefix="bootcheck.splash.gave_up",
            title=None,
            message=None,
            submessage=None,
            slot=[],
        )
        t, m, sub = eink_display._resolve_status_parts(args)
        assert t == "bootcheck.splash.gave_up.title"
        assert sub == "bootcheck.splash.gave_up.submessage"


# ── litclock-dev#532 Stage-4 content gates ─────────────────────────────────
#
# The Stage-3 gates prove key-set parity; these prove VALUE conformance, so
# the first translated bundle (es) is linted the moment it lands in the
# registry — not on activation day. Recorded needs on litclock-dev#532: placeholder-set
# parity, rich-token balance, and the content lint for the setup-page
# brace/quote family (the litclock-dev#756 review's future-translation traps: the
# mechanism is structurally safe, but a translation carrying stray braces,
# typo'd slots, raw markup, or tokens a resolver won't convert still renders
# literal garbage to the user).
#
# Every check runs over EVERY registry language including en — en's own
# conformance is what keeps the gates honest between now and the es bundle.
#
# Accepted false positive (this gate's /review, on record): the global
# '<'/'>' ban rejects legitimate plain-text uses like "Settings > WiFi".
# That is deliberate — catalog values are plain text across surfaces whose
# escape posture differs (Jinja autoescape, _page_text, json.dumps'd inline
# JS, RAW splices in the CNA bridge), and the gate cannot know per-key
# routing. A blocked translation is rephrased; a missed '</script>' breaks
# the captive portal. Strict wins.

# litclock-dev#532: the Stage-4 content-lint RULES live in the shippable
# src/catalog_lint.py so the CI gate (here), the standalone validator, and
# the translator-kit generator share ONE implementation. This test file
# keeps the anti-vacuity pins (source scan is live, vocab matches the
# production resolvers, wiring is exercised end-to-end) and the red-case
# proofs — the module is the code under test, these are the tests.
import catalog_lint  # noqa: E402

_RICH_TOKEN_NAMES = catalog_lint.RICH_TOKEN_NAMES
_RICH_VOCAB_FULL = catalog_lint.RICH_VOCAB_FULL
_RICH_VOCAB_SETUP = catalog_lint.RICH_VOCAB_SETUP
_TOKEN_RE = catalog_lint.TOKEN_RE
_slot_counts = catalog_lint.slot_counts
_plural_sibling_slots = catalog_lint.plural_sibling_slots


def _rich_capable_keys():
    return catalog_lint.rich_capable_keys(REPO_ROOT)


def _stage4_value_errors(value, **kw):
    return catalog_lint.value_errors(value, **kw)


def _all_stage4_errors(root: Path = REPO_ROOT) -> list[str]:
    # Bundle from `root`; rich-token vocabulary always from the real source
    # tree (the tmp-registry end-to-end test lints a synthetic es bundle
    # against the production resolvers).
    return catalog_lint.registry_errors(root, source_root=REPO_ROOT)


class TestStage4ContentGates:
    def test_every_catalog_value_passes_the_content_lint(self):
        findings = _all_stage4_errors()
        assert not findings, "Stage-4 content lint:\n  " + "\n  ".join(findings[:20])

    def test_rich_scan_is_live_and_covers_every_token_carrying_key(self):
        """Anti-vacuity for the source scan: if the scan regex rots, every
        en key that CARRIES a token would trip the vocabulary check above —
        and this pin fails first with a direct message."""
        capable = _rich_capable_keys()
        assert capable, "rich-capable source scan found nothing — the scan regex is dead"
        assert any(v == _RICH_VOCAB_SETUP for v in capable.values()), "setup_server._rich scan found nothing"
        assert any(v == _RICH_VOCAB_FULL for v in capable.values()), "template t_rich scan found nothing"
        en = _catalog_json()
        carrying = {k for k, v in en.items() if not k.startswith("_") and isinstance(v, str) and _TOKEN_RE.search(v)}
        assert carrying, "no en value carries a rich token — the balance gate has no live subject"
        orphans = carrying - set(capable)
        assert not orphans, f"en values carry rich tokens in keys no resolver routes: {sorted(orphans)}"
        missing_keys = set(capable) - {k for k in en if not k.startswith("_")}
        assert not missing_keys, f"rich call sites reference keys absent from the en catalog: {sorted(missing_keys)}"

    def test_vocab_matches_production_resolvers(self):
        """The vocab tuples are pinned against the PRODUCTION resolver
        sources, so a resolver-side vocab change — especially a SHRINK,
        which every other rule passes silently — goes red here."""
        cs_src = (REPO_ROOT / "src" / "control_server" / "__init__.py").read_text(encoding="utf-8")
        block = re.search(r"_RICH_TOKENS\s*=\s*\((.*?)\n\s*\)", cs_src, re.S)
        assert block, "control_server _RICH_TOKENS table not found — resolver moved; re-pin this test"
        full = set(re.findall(r'\(\s*"\{([a-z]+)\}"\s*,', block.group(1)))
        assert full == set(_RICH_VOCAB_FULL), f"t_rich converts {sorted(full)}, gate allows {sorted(_RICH_VOCAB_FULL)}"
        import inspect  # noqa: PLC0415

        import setup_server  # noqa: PLC0415

        setup = set(re.findall(r'\(\s*"\{([a-z]+)\}"\s*,', inspect.getsource(setup_server._rich)))
        assert setup == set(_RICH_VOCAB_SETUP), (
            f"setup_server._rich converts {sorted(setup)}, gate allows {sorted(_RICH_VOCAB_SETUP)}"
        )

    def test_slot_extraction_is_live(self):
        """Anti-vacuity for the slot extractor against the REAL catalog: a
        known slot-carrying key must yield its slot, or every parity
        assertion above is vacuously green."""
        en = _catalog_json()
        assert _slot_counts(en["setup.banner.error_lead"]) == {"error": 1}
        assert _slot_counts(en["system.sheet.factory_reset.body"]) == {"network": 2}
        inventory = {s for k, v in en.items() if not k.startswith("_") for s in _slot_counts(v)}
        assert len(inventory) >= 10, f"en slot inventory implausibly small: {sorted(inventory)}"

    def test_plural_category_suffix_is_reserved(self):
        """A non-plural key ending in .one/.other would silently gain the
        {n} allowance (and Stage 3 would demand its full category set), so
        the suffix is a reserved namespace: genuinely-plural bases extend
        this list; anything else renames."""
        en = _catalog_json()
        forms = set(_languages()["en"]["plural_forms"])
        bases = {k.rsplit(".", 1)[0] for k in en if not k.startswith("_") and k.rsplit(".", 1)[-1] in forms}
        assert bases == {
            "status.relative.minutes",
            "status.relative.hours",
            "status.relative.days",
        }, f"plural-suffixed bases changed: {sorted(bases)} — extend if genuinely plural, else rename the key"

    def test_registry_field_shapes(self):
        """Stage-3 checks field PRESENCE; a defective es registry entry is
        as dangerous as a defective strings.json (adversarial F7:
        plural_forms as a STRING iterates as characters)."""
        for code, entry in _languages().items():
            forms = entry["plural_forms"]
            assert isinstance(forms, list) and forms and all(isinstance(f, str) for f in forms), (
                f"{code}: plural_forms must be a non-empty list of strings, got {forms!r}"
            )
            floor = entry.get("min_coverage_pct", 80)
            assert isinstance(floor, (int, float)) and not isinstance(floor, bool) and 0 <= floor <= 100, (
                f"{code}: min_coverage_pct must be a number in [0, 100], got {floor!r}"
            )


class TestStage4EndToEnd:
    """The wiring red-case (this gate's /review, testing specialist): with
    only en in the real registry, the parity/vocab/relaxation arms of
    _all_stage4_errors structurally cannot fire — so a synthetic registry
    with a defective es bundle drives every arm through the REAL gate
    path: en_value plumbing, per-key vocab plumbing, relax plumbing."""

    def _write_root(self, tmp_path, es_overrides: dict) -> Path:
        en = {
            "setup.banner.error_lead": "Couldn't join. {error}",
            # A REAL setup_server._rich-routed key so the source-scanned
            # vocab (setup: {b} only) applies through the real plumbing.
            "setup.banner.connecting": "Connecting to {b}{network}{/b}…",
            "shell.drawer.follow_pill": "Follow ({n})",
            "status.relative.minutes.one": "{n} minute ago",
            "status.relative.minutes.other": "{n} minutes ago",
        }
        es = dict(en)
        es.update(es_overrides)
        (tmp_path / "languages").mkdir()
        for code, data in (("en", en), ("es", es)):
            d = tmp_path / "languages" / code
            d.mkdir()
            (d / "strings.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        (tmp_path / "languages.json").write_text(
            json.dumps(
                {
                    "languages": {
                        code: {
                            "code": code,
                            "status": "active",
                            "strings": f"languages/{code}/strings.json",
                            "plural_forms": ["one", "other"],
                        }
                        for code in ("en", "es")
                    }
                }
            ),
            encoding="utf-8",
        )
        return tmp_path

    def test_defective_es_bundle_is_caught_through_the_real_gate(self, tmp_path):
        root = self._write_root(
            tmp_path,
            {
                "setup.banner.error_lead": "No se pudo conectar.",  # drops {error}
                "setup.banner.connecting": "Conectando a {em}{network}{/em}…",  # {em} beyond setup vocab
                "shell.drawer.follow_pill": "Seguir {n} de {n}",  # duplicate — split-rendered key truncates
                "status.relative.minutes.other": "hace minutos",  # drops {n} where n is unbounded
            },
        )
        findings = _all_stage4_errors(root=root)
        assert any(f.startswith("es:setup.banner.error_lead:") and "drops slot" in f for f in findings), findings
        assert any(
            f.startswith("es:setup.banner.connecting:") and "outside this key's resolver vocabulary" in f
            for f in findings
        ), findings
        assert any(
            f.startswith("es:shell.drawer.follow_pill:") and "exceed the English occurrence count" in f
            for f in findings
        ), findings
        assert any(f.startswith("es:status.relative.minutes.other:") and "drops slot" in f for f in findings), findings
        assert not [f for f in findings if f.startswith("en:")], f"clean en flagged: {findings}"

    def test_conforming_es_bundle_passes_clean(self, tmp_path):
        root = self._write_root(
            tmp_path,
            {
                "setup.banner.error_lead": "No se pudo conectar. {error}",
                "setup.banner.connecting": "Conectando a {b}{network}{/b}…",
                "status.relative.minutes.one": "hace un minuto",  # .one may spell the number
                "status.relative.minutes.other": "hace {n} minutos",
            },
        )
        assert _all_stage4_errors(root=root) == []


class TestStage4GatesFireOnDefects:
    """Red-case proofs: each defect class the gate exists for must produce a
    finding from the SAME function the real gate runs. Delete a rule from
    _stage4_value_errors and its proof here goes red."""

    def _errs(self, value, *, en_value="Ready {name}.", vocab=(), **kw):
        return _stage4_value_errors(value, en_value=en_value, vocab=vocab, **kw)

    def test_typoed_slot_is_caught(self):
        errs = self._errs("Listo {nmae}.")
        assert any("exceed the English occurrence count" in e for e in errs)
        assert any("drops slot" in e for e in errs)

    def test_duplicate_slot_is_caught(self):
        # Four templates split-render a slot en uses once; a duplicate
        # silently truncates the tail after the second occurrence.
        errs = self._errs("Antes {name} medio {name} después.")
        assert any("exceed the English occurrence count" in e for e in errs)

    def test_stray_brace_is_caught(self):
        errs = self._errs("Listo {name}. {")
        assert any("stray brace" in e for e in errs)

    def test_dropped_slot_is_caught(self):
        assert any("drops slot" in e for e in self._errs("Listo."))

    def test_unbalanced_token_is_caught(self):
        errs = self._errs("{b}Listo {name}.", vocab=_RICH_VOCAB_FULL)
        assert any("unclosed" in e for e in errs)

    def test_crossed_nesting_is_caught(self):
        errs = self._errs("{b}Listo {em}ya{/b} {name}{/em}.", vocab=_RICH_VOCAB_FULL)
        assert any("closes nothing or crosses" in e for e in errs)

    def test_token_outside_vocabulary_is_caught(self):
        # {em} in a setup_server._rich key would render literally — its
        # resolver converts only {b}.
        errs = self._errs("{em}Listo{/em} {name}.", vocab=_RICH_VOCAB_SETUP)
        assert any("outside this key's resolver vocabulary" in e for e in errs)
        assert any("stray brace" in e for e in errs)

    def test_token_in_plain_key_is_caught(self):
        assert any("vocabulary" in e for e in self._errs("{b}Listo{/b} {name}."))

    def test_raw_markup_is_caught(self):
        errs = self._errs("Listo <strong>{name}</strong>.")
        assert any("markup" in e for e in errs)

    def test_script_close_in_js_literal_key_is_caught(self):
        # json.dumps escapes quotes but not '/', so '</script>' inside an
        # inline-JS literal terminates the script element — the markup rule
        # is what stands between a translation and that.
        assert any("markup" in e for e in self._errs("Listo </script>."))

    def test_html_entity_is_caught(self):
        assert any("HTML entity" in e for e in self._errs("Listo &lt;ya&gt; {name}."))
        assert any("HTML entity" in e for e in self._errs("Listo &#123;b&#125; {name}."))

    def test_confusable_brace_is_caught(self):
        # Fullwidth ｛b｝ matches no resolver and no ASCII-brace rule —
        # visually indistinguishable from a real token in most fonts.
        assert any("brace-confusable" in e for e in self._errs("｛b｝Listo｛/b｝ {name}."))

    def test_bidi_override_is_caught(self):
        assert any("bidi control" in e for e in self._errs("Contrase‮ña {name}."))

    def test_control_character_is_caught(self):
        assert any("control character" in e for e in self._errs("Listo\r {name}."))

    def test_newline_is_allowed(self):
        # shutdown.splash.welcome.message legitimately carries \n.
        assert self._errs("Listo,\n{name}.") == []

    def test_invisible_only_value_is_caught(self):
        # ZWSP survives value.strip() — a blank splash title through both
        # Stage-3 and a naive Stage-4.
        assert any("no visible content" in e for e in self._errs("​", en_value="Ready."))
        assert any("no visible content" in e for e in self._errs("⠀", en_value="Ready."))

    def test_plural_relaxation_is_per_category(self):
        en_one = "{n} minute ago"
        sibling = {"n"}
        # .one may spell the number ("hace un minuto") — {n} relaxed.
        assert (
            _stage4_value_errors(
                "hace un minuto", en_value=en_one, extra_allowed_slots=sibling, relax_slots={"n"}
            )
            == []
        )
        # A named non-{n} slot stays REQUIRED even under relaxation.
        errs = _stage4_value_errors(
            "quedan {n}", en_value="{n} of {total} left", extra_allowed_slots=sibling, relax_slots={"n"}
        )
        assert any("drops slot" in e and "total" in e for e in errs)
        # .other gets NO relaxation (unbounded n cannot be spelled) — the
        # end-to-end test drives this through the real wiring too.
        errs = _stage4_value_errors("hace minutos", en_value="{n} minutes ago", extra_allowed_slots=sibling)
        assert any("drops slot" in e for e in errs)

    def test_conforming_translation_passes_clean(self):
        assert self._errs("Listo, {name} — ¡ya está!") == []
        assert self._errs("{b}Listo{/b}, {name}.", vocab=_RICH_VOCAB_SETUP) == []


# ---------- litclock-dev#532 RT1 (R12/R15, O3/O4): one resolver ----------


@pytest.fixture
def xx_fleet(tmp_path, monkeypatch):
    """English (the real bundle) plus an ACTIVE `xx` whose bundle the test
    writes: complete by default (every canonical key, value prefixed XX)."""
    en = json.loads((REPO_ROOT / "languages" / "en" / "strings.json").read_text(encoding="utf-8"))
    canonical = {k: v for k, v in en.items() if not k.startswith("_")}
    (tmp_path / "languages" / "en").mkdir(parents=True)
    (tmp_path / "languages" / "xx").mkdir(parents=True)
    (tmp_path / "languages" / "en" / "strings.json").write_text(json.dumps(en), encoding="utf-8")
    bundle = tmp_path / "languages" / "xx" / "strings.json"

    def write(data):
        bundle.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")

    write({k: f"XX {v}" for k, v in canonical.items()})
    reg = tmp_path / "languages.json"
    reg.write_text(
        json.dumps(
            {
                "fleet_default": "en",
                "languages": {
                    "en": {"code": "en", "status": "active", "strings": "languages/en/strings.json"},
                    "xx": {"code": "xx", "status": "active", "strings": "languages/xx/strings.json"},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", reg)
    monkeypatch.setattr(strings_catalog, "_REPO_ROOT", tmp_path)
    monkeypatch.setenv("LITCLOCK_LANGUAGE", "xx")
    strings_catalog.reset_cache()
    yield {"write": write, "canonical": canonical}
    strings_catalog.reset_cache()


class TestOneResolver:
    KEY = "status.relative.just_now"

    def test_an_intact_bundle_serves_itself(self, xx_fleet):
        snap = strings_catalog.snapshot()
        assert (snap.requested, snap.language, snap.usable) == ("xx", "xx", True)
        assert strings_catalog.get(self.KEY) == "XX just now"

    @pytest.mark.parametrize("broken", ["{not json", "[]", "{}"], ids=["unparseable", "list", "empty"])
    def test_an_unloadable_bundle_serves_the_fleet_default_whole(self, xx_fleet, broken):
        xx_fleet["write"](broken)
        strings_catalog.reset_cache()
        snap = strings_catalog.snapshot()
        assert (snap.requested, snap.language, snap.usable) == ("xx", "en", False)
        assert strings_catalog.get(self.KEY) == "just now"

    def test_one_missing_key_makes_the_whole_bundle_unusable_never_mixed(self, xx_fleet):
        """R12: the old per-key fallback served this page half in xx, half
        in English. Now every key comes from ONE language."""
        data = {k: f"XX {v}" for k, v in xx_fleet["canonical"].items()}
        del data["status.relative.never"]
        xx_fleet["write"](data)
        strings_catalog.reset_cache()
        page = strings_catalog.get_many([self.KEY, "status.relative.never"])
        assert page == {self.KEY: "just now", "status.relative.never": "—"}
        assert strings_catalog.snapshot().language == "en"

    @staticmethod
    def _clock(monkeypatch):
        clock = [1e9]
        monkeypatch.setattr(strings_catalog.time, "monotonic", lambda: clock[0])
        return clock

    def test_an_incomplete_bundle_heals_in_process_after_the_ttl(self, xx_fleet, monkeypatch):
        """O3: incomplete is a RETRYABLE failure (the module's own contract:
        a transient OTA window must heal itself), not cached for life."""
        clock = self._clock(monkeypatch)
        data = {k: f"XX {v}" for k, v in xx_fleet["canonical"].items()}
        missing = data.pop("status.relative.never")
        xx_fleet["write"](data)
        strings_catalog.reset_cache()
        assert strings_catalog.snapshot().language == "en"
        data["status.relative.never"] = missing
        xx_fleet["write"](data)
        assert strings_catalog.snapshot().language == "en", "still inside the TTL"
        clock[0] += strings_catalog.NEGATIVE_CACHE_TTL_S + 1
        assert strings_catalog.snapshot().language == "xx"
        assert strings_catalog.get(self.KEY) == "XX just now"

    def test_completeness_is_rejudged_after_a_canonical_load_failure(self, xx_fleet, monkeypatch):
        """litclock-dev#913 review (three reviewers): an incomplete bundle first loaded
        while English was unreadable was cached as COMPLETE for life and
        served raw keys once English healed."""
        clock = self._clock(monkeypatch)
        en_path = strings_catalog._REPO_ROOT / "languages" / "en" / "strings.json"
        good_en = en_path.read_text(encoding="utf-8")
        en_path.write_text("{broken", encoding="utf-8")
        data = {k: f"XX {v}" for k, v in xx_fleet["canonical"].items()}
        del data["status.relative.never"]
        xx_fleet["write"](data)
        strings_catalog.reset_cache()
        snap = strings_catalog.snapshot()
        assert not snap.usable
        assert snap.get(self.KEY) == "XX just now", "with no fallback loadable, the raw bundle beats keys"
        assert strings_catalog.get_own("clock.fallback.gap") is None
        en_path.write_text(good_en, encoding="utf-8")
        clock[0] += strings_catalog.NEGATIVE_CACHE_TTL_S + 1
        snap = strings_catalog.snapshot()
        assert (snap.language, snap.usable) == ("en", False)
        assert snap.get("status.relative.never") == "—", "never the raw key"

    def test_an_unjudged_complete_bundle_is_judged_as_soon_as_english_is_back(self, xx_fleet, monkeypatch):
        """litclock-dev#913 round 2: read while English was unreadable, a COMPLETE bundle
        was held for its full TTL after English recovered, showing the
        fallback (and logging a false 'unusable') in between."""
        clock = self._clock(monkeypatch)
        en_path = strings_catalog._REPO_ROOT / "languages" / "en" / "strings.json"
        good_en = en_path.read_text(encoding="utf-8")
        en_path.write_text("{broken", encoding="utf-8")
        strings_catalog.reset_cache()
        strings_catalog._catalog("en")  # English fails at t0
        clock[0] += 20
        strings_catalog._catalog("xx")  # xx read at t0+20: unjudged, not complete
        en_path.write_text(good_en, encoding="utf-8")
        clock[0] += 11  # English's retry is due; xx's own hold is only 11s old
        strings_catalog._catalog("en")
        assert strings_catalog.snapshot().language == "xx"

    def test_a_blank_value_counts_as_missing(self, xx_fleet):
        """A translator-kit export writes every key, filled or not."""
        data = {k: f"XX {v}" for k, v in xx_fleet["canonical"].items()}
        data["status.relative.never"] = "  "
        xx_fleet["write"](data)
        strings_catalog.reset_cache()
        assert strings_catalog.snapshot().language == "en"

    @pytest.mark.parametrize("status", ["incubating", "active-but-incomplete"])
    def test_a_fleet_default_that_cannot_serve_is_passed_over(self, xx_fleet, monkeypatch, status):
        reg = json.loads(strings_catalog.REGISTRY_PATH.read_text(encoding="utf-8"))
        reg["fleet_default"] = "xx"
        reg["languages"]["xx"]["status"] = "incubating" if status == "incubating" else "active"
        strings_catalog.REGISTRY_PATH.write_text(json.dumps(reg), encoding="utf-8")
        if status != "incubating":
            xx_fleet["write"]({"a": "b"})
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "en")
        strings_catalog.reset_cache()
        assert strings_catalog.snapshot("zz").language == "en"

    def test_lang_is_keyword_only(self, xx_fleet):
        with pytest.raises(TypeError):
            strings_catalog.get(self.KEY, "en")
        with pytest.raises(TypeError):
            strings_catalog.get_many([self.KEY], "en")

    def test_no_canonical_value_uses_lang_as_a_slot(self):
        """`lang` is the lookup keyword, so it can never be a slot name."""
        en = json.loads((REPO_ROOT / "languages" / "en" / "strings.json").read_text(encoding="utf-8"))
        assert not [k for k, v in en.items() if "{lang}" in str(v)]

    def test_a_blank_part_stays_blank_when_the_canonical_bundle_is_broken(self, xx_fleet):
        en_path = strings_catalog._REPO_ROOT / "languages" / "en" / "strings.json"
        en_path.write_text("{broken", encoding="utf-8")
        strings_catalog.reset_cache()
        prefix = "boot.splash.starting"
        assert prefix + ".submessage" not in xx_fleet["canonical"]
        assert strings_catalog.get_triplet(prefix)[2] == ""

    def test_catalog_size_stays_raw(self, xx_fleet):
        """Section 2 #2: the OTA count probe must never see the fallback's
        count for a broken bundle, and an incomplete one reports its own."""
        full = len(xx_fleet["canonical"])
        assert strings_catalog.catalog_size("xx") == full
        xx_fleet["write"]({"a": "b", "c": "d"})
        strings_catalog.reset_cache()
        assert strings_catalog.catalog_size("xx") == 2
        xx_fleet["write"]("{not json")
        strings_catalog.reset_cache()
        assert strings_catalog.catalog_size("xx") == 0

    def test_an_explicit_language_overrides_the_device(self, xx_fleet):
        assert strings_catalog.get(self.KEY, lang="en") == "just now"
        monkeypatch_free = strings_catalog.get(self.KEY)  # the device language is untouched
        assert monkeypatch_free == "XX just now"
        assert strings_catalog.plural("status.relative.minutes", 2, lang="en") == "2 minutes ago"

    @pytest.mark.parametrize("code", ["zz", "", "../en"])
    def test_an_explicit_unknown_language_is_the_fleet_default(self, xx_fleet, code):
        assert strings_catalog.snapshot(code).language == "en"

    def test_an_explicit_incubating_language_is_the_fleet_default(self, xx_fleet, monkeypatch):
        """A complete bundle is not enough: an incubating language is
        unselectable by contract, explicitly asked for or not."""
        reg = json.loads(strings_catalog.REGISTRY_PATH.read_text(encoding="utf-8"))
        reg["languages"]["xx"]["status"] = "incubating"
        strings_catalog.REGISTRY_PATH.write_text(json.dumps(reg), encoding="utf-8")
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "en")
        strings_catalog.reset_cache()
        assert strings_catalog.snapshot("xx").language == "en"
        assert strings_catalog.get(self.KEY, lang="xx") == "just now"

    def test_a_splash_is_wholly_one_language(self, xx_fleet):
        """R15: a broken xx bundle paints the restart splash wholly in the
        fleet default."""
        assert strings_catalog.get_triplet("shutdown.splash.reboot")[1].startswith("XX ")
        data = {k: f"XX {v}" for k, v in xx_fleet["canonical"].items()}
        del data["shutdown.splash.reboot.title"]
        xx_fleet["write"](data)
        strings_catalog.reset_cache()
        title, message, _ = strings_catalog.get_triplet("shutdown.splash.reboot")
        assert (title, message) == ("LitClock", "Restarting...")

    def test_an_optional_part_is_dropped_on_an_unusable_bundle(self, xx_fleet):
        """R15/D9: the plain-time frame's line, read through get_own, is
        omitted rather than served in the fleet default."""
        assert strings_catalog.get_own("clock.fallback.gap") == "XX No book quote for this minute yet."
        data = {k: f"XX {v}" for k, v in xx_fleet["canonical"].items()}
        del data["status.relative.never"]
        xx_fleet["write"](data)
        strings_catalog.reset_cache()
        assert strings_catalog.get_own("clock.fallback.gap") is None

    def test_a_key_missing_everywhere_serves_the_key(self, xx_fleet):
        assert strings_catalog.get("no.such.key") == "no.such.key"


class TestThePwaReadsOneSnapshotPerPage:
    def test_a_language_switch_mid_render_does_not_mix_the_page(self, xx_fleet, monkeypatch):
        """O4: t() resolved the device language on EVERY call, so a save
        landing mid-render could split one page across two languages."""
        sys.path.insert(0, str(REPO_ROOT / "src"))
        from control_server import create_app  # noqa: PLC0415

        app = create_app(test_config={"VERSION_OVERRIDE": "v0.0.0-test"})
        t = app.jinja_env.globals["t"]
        with app.test_request_context("/"):
            first = t(TestOneResolver.KEY)
            monkeypatch.setenv("LITCLOCK_LANGUAGE", "en")  # the save lands
            second = t(TestOneResolver.KEY)
        assert first == second == "XX just now"
        with app.test_request_context("/"):
            assert t(TestOneResolver.KEY) == "just now", "the next request sees the new language"

    def test_the_page_declares_the_language_it_is_written_in(self, xx_fleet):
        sys.path.insert(0, str(REPO_ROOT / "src"))
        from control_server import create_app  # noqa: PLC0415

        app = create_app(test_config={"VERSION_OVERRIDE": "v0.0.0-test"})
        assert '<html lang="xx">' in app.test_client().get("/").get_data(as_text=True)
        xx_fleet["write"]("{not json")
        strings_catalog.reset_cache()
        assert '<html lang="en">' in app.test_client().get("/").get_data(as_text=True)
