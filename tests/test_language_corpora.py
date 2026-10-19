"""litclock-dev#532 T8 — tools/language_corpora.py, the list of corpora CI renders.

The render-invariants workflow triggers on any `languages/**/*.csv`, so every
CSV there must either be rendered or fail the step. Before this module a file
under another name, a stray `quotes.csv` beside a registered language, or a
directory name the shell word-split each passed the step green without a row
of it read (litclock-dev#926 review). The repo-tree test below is the every-push twin: a
GitHub path filter is case-sensitive, so `Quotes.CSV` would not even start
the workflow.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import language_corpora  # noqa: E402

ROW = "00:00|midnight|It was midnight.|Title|Author|\n"


@pytest.fixture
def tree(tmp_path):
    """A checkout with English registered off `languages/` (as the real one
    is) and an `xx` registered at `languages/xx/xx.csv`. Returns add(rel)."""
    registry = {
        "languages": {
            "en": {"corpus": {"path": "image-gen/litclock_annotated.csv"}},
            "xx": {"corpus": {"path": "languages/xx/xx.csv"}},
        }
    }
    (tmp_path / "languages.json").write_text(json.dumps(registry), encoding="utf-8")

    def add(rel: str) -> None:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(ROW, encoding="utf-8")

    add("languages/xx/xx.csv")
    add.root = tmp_path
    return add


def test_the_real_tree_has_no_corpus_ci_would_skip():
    codes, problems = language_corpora.plan(REPO_ROOT)
    assert problems == [], problems
    assert "en" in codes


def test_registered_codes_come_first_then_unregistered_quotes_csv(tree):
    tree("languages/fr/quotes.csv")
    tree("languages/de/quotes.csv")
    tree("languages/pt-BR/quotes.csv")
    assert language_corpora.plan(tree.root) == (["en", "xx", "de", "fr", "pt-BR"], [])


@pytest.mark.parametrize(
    "rel",
    [
        "languages/de/de.csv",  # another file name
        "languages/de/Quotes.csv",  # case
        "languages/de/quotes.CSV",  # case of the suffix
        "languages/de/v1/quotes.csv",  # nested
        "languages/quotes.csv",  # no language directory at all
        "languages/xx/quotes.csv",  # beside a registered language whose corpus is xx.csv
        "languages/en/quotes.csv",  # beside English, whose corpus is not under languages/
    ],
)
def test_a_csv_ci_would_not_render_is_a_problem(tree, rel):
    tree(rel)
    codes, problems = language_corpora.plan(tree.root)
    assert len(problems) == 1 and problems[0].startswith(rel), problems
    assert codes == ["en", "xx"]


@pytest.mark.parametrize("name", ["en ", " ", "ES", "pt_BR", "NOTE", "-x", "*", "de\nfr"])
def test_a_directory_that_is_not_a_language_code_is_a_problem(tree, name):
    """Codex (litclock-dev#926 review): `languages/en /quotes.csv` word-split to `en` and
    rendered English twice; a whitespace-only name vanished from the loop."""
    tree(f"languages/{name}/quotes.csv")
    codes, problems = language_corpora.plan(tree.root)
    assert problems and "is not a language code" in problems[0], problems
    assert codes == ["en", "xx"]


def test_a_symlinked_language_directory_is_a_problem(tree):
    """litclock-dev#926 re-review: rglob does not descend a linked directory, so a
    corpus behind one was invisible here AND to the workflow's path filter."""
    tree("corpora/es/quotes.csv")
    (tree.root / "languages" / "es").symlink_to(tree.root / "corpora" / "es")
    codes, problems = language_corpora.plan(tree.root)
    assert problems == ["languages/es: a symbolic link — languages/ must hold real files and directories"]
    assert codes == ["en", "xx"]


@pytest.mark.parametrize("target", ["corpora/fr.csv", "nowhere.csv"], ids=["live", "dangling"])
def test_a_symlinked_corpus_file_is_a_problem(tree, target):
    if target != "nowhere.csv":
        tree(target)
    (tree.root / "languages" / "fr").mkdir()
    (tree.root / "languages" / "fr" / "quotes.csv").symlink_to(tree.root / target)
    codes, problems = language_corpora.plan(tree.root)
    assert problems == ["languages/fr/quotes.csv: a symbolic link — languages/ must hold real files and directories"]
    assert codes == ["en", "xx"]


def test_a_registry_without_english_is_a_problem(tree):
    (tree.root / "languages.json").write_text(json.dumps({"languages": {}}), encoding="utf-8")
    assert "languages.json does not list English" in language_corpora.plan(tree.root)[1]


def test_main_prints_nothing_on_stdout_when_it_fails(tree, monkeypatch, capsys):
    """The workflow assigns stdout to `codes`: a partial list on failure
    would be rendered as if complete."""
    tree("languages/de/de.csv")
    monkeypatch.setattr(language_corpora, "_REPO", tree.root)
    assert language_corpora.main() == 1
    out = capsys.readouterr()
    assert out.out == "" and "::error::languages/de/de.csv" in out.err


def test_main_prints_one_code_per_line(tree, monkeypatch, capsys):
    tree("languages/fr/quotes.csv")
    monkeypatch.setattr(language_corpora, "_REPO", tree.root)
    assert language_corpora.main() == 0
    assert capsys.readouterr().out == "en\nxx\nfr\n"


def test_an_unreadable_registry_fails(tree, monkeypatch, capsys):
    (tree.root / "languages.json").write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(language_corpora, "_REPO", tree.root)
    assert language_corpora.main() == 1 and capsys.readouterr().out == ""
