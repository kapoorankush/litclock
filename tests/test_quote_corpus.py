"""Tests for src/quote_corpus.py — runtime quote-metadata lookup.

The PHP image generator bakes quote text + attribution into PNGs and
names them ``quote_{HHMM}_{idx}_credits.png``. The control_server's
hero-card renderer needs the inverse: filename → metadata. This module
implements the inverse via lazy CSV indexing; these tests pin the
indexing contract.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make src/ importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import quote_corpus  # noqa: E402


def _write_corpus(path: Path, rows: list[tuple[str, str, str, str, str, str]]) -> None:
    """Write a synthetic litclock_annotated.csv with `|` delimiter at
    `path`. Rows: (time, timestring, quote, title, author, classification)."""
    with path.open("w", encoding="utf-8", newline="") as f:
        for r in rows:
            time_str, timestring, quote, title, author, cls = r
            # Quote field is wrapped in `"` per PHP's CSV style; embedded
            # `"` is doubled `""` per CSV convention.
            quote_field = '"' + quote.replace('"', '""') + '"'
            f.write(f"{time_str}|{timestring}|{quote_field}|{title}|{author}|{cls}\n")


@pytest.fixture
def synthetic_corpus(tmp_path, monkeypatch):
    """Point the corpus path at a tmp file with predictable rows."""
    csv = tmp_path / "litclock_annotated.csv"
    _write_corpus(
        csv,
        [
            ("00:00", "midnight", "first quote at 00:00", "Title A", "Author A", "NO"),
            ("00:00", "midnight", "second quote at 00:00", "Title B", "Author B", "NO"),
            ("00:00", "midnight", 'third with embedded "quotes"', "Title C", "Author C", "NO"),
            ("00:01", "one past", "rolls to 00:01 — idx resets", "Title D", "Author D", "NO"),
            ("00:01", "one past", "second quote at 00:01", "Title E", "Author E", "YES"),
        ],
    )
    monkeypatch.setattr(quote_corpus, "_CORPUS_PATH", csv)
    quote_corpus.reset_cache()
    yield csv
    quote_corpus.reset_cache()


def test_lookup_first_row_in_bucket(synthetic_corpus) -> None:
    """imagenumber resets to 0 on time change, increments per row otherwise."""
    meta = quote_corpus.lookup_by_filename("quote_0000_0_credits.png")
    assert meta is not None
    assert meta["author"] == "Author A"
    assert meta["title"] == "Title A"
    assert meta["time"] == "00:00"


def test_lookup_second_row_in_bucket(synthetic_corpus) -> None:
    meta = quote_corpus.lookup_by_filename("quote_0000_1_credits.png")
    assert meta is not None
    assert meta["author"] == "Author B"


def test_idx_resets_on_time_change(synthetic_corpus) -> None:
    """0001_0 must be the FIRST 00:01 row, not the 4th overall row."""
    meta = quote_corpus.lookup_by_filename("quote_0001_0_credits.png")
    assert meta is not None
    assert meta["author"] == "Author D"
    assert meta["time"] == "00:01"


def test_lookup_nsfw_filename_uses_same_bucket(synthetic_corpus) -> None:
    """NSFW filenames have a `_nsfw` suffix but share the per-time bucket
    counter with safe rows. CSV row 5 is NSFW at 00:01 idx=1 — its
    filename is `quote_0001_1_nsfw_credits.png`."""
    meta = quote_corpus.lookup_by_filename("quote_0001_1_nsfw_credits.png")
    assert meta is not None
    assert meta["author"] == "Author E"


def test_lookup_handles_image_filename_without_credits(synthetic_corpus) -> None:
    meta = quote_corpus.lookup_by_filename("quote_0000_2.png")
    assert meta is not None
    assert meta["author"] == "Author C"


def test_lookup_returns_none_for_unknown_idx(synthetic_corpus) -> None:
    assert quote_corpus.lookup_by_filename("quote_0000_99_credits.png") is None


def test_lookup_returns_none_for_unknown_time(synthetic_corpus) -> None:
    assert quote_corpus.lookup_by_filename("quote_2300_0_credits.png") is None


def test_lookup_returns_none_for_malformed_filename(synthetic_corpus) -> None:
    assert quote_corpus.lookup_by_filename("not-a-quote-file.png") is None


def test_lookup_strips_outer_csv_quotes(synthetic_corpus) -> None:
    """CSV writes wrap each quote in `"..."`; on read we should see the
    inner text only, with embedded `""` collapsed to single `"`."""
    meta = quote_corpus.lookup_by_filename("quote_0000_0_credits.png")
    assert meta is not None
    assert meta["quote"] == "first quote at 00:00"


def test_lookup_handles_embedded_quote_chars(synthetic_corpus) -> None:
    meta = quote_corpus.lookup_by_filename("quote_0000_2_credits.png")
    assert meta is not None
    # Embedded `"` was written as `""` and should reduce back to `"`.
    assert 'embedded "quotes"' in meta["quote"]


def test_lookup_real_corpus_first_row_smoke() -> None:
    """Smoke test against the real bundled corpus — pins that the
    CSV-vs-filename contract holds with the actual production data,
    catching any drift between PHP-side numbering and our Python-side
    index. ``quote_0000_0`` was Towles' "A Gentleman in Moscow" at the
    time M2 shipped; if PR #218 (or later) renumbers, this needs an
    update."""
    quote_corpus.reset_cache()
    # Reset to the project-bundled CSV path.
    repo_root = Path(__file__).resolve().parents[1]
    real_csv = repo_root / "image-gen" / "litclock_annotated.csv"
    if not real_csv.exists():
        pytest.skip("bundled corpus CSV not present in this checkout")
    os.environ.pop("LITCLOCK_CORPUS_CSV", None)
    # Direct attribute swap because the module reads the env var only at
    # import time. monkeypatch.setenv won't help post-import.
    quote_corpus._CORPUS_PATH = real_csv  # type: ignore[attr-defined]
    quote_corpus.reset_cache()
    meta = quote_corpus.lookup_by_filename("quote_0000_0_credits.png")
    assert meta is not None
    assert meta["time"] == "00:00"
    # Title + author from the first 00:00 row in the bundled CSV.
    assert "Towles" in meta["author"]
