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


def test_lookup_real_corpus_first_row_smoke(monkeypatch) -> None:
    """Smoke test against the real bundled corpus — pins that the
    CSV-vs-filename contract holds with the actual production data,
    catching any drift between PHP-side numbering and our Python-side
    index. ``quote_0000_0`` was Towles' "A Gentleman in Moscow" at the
    time M2 shipped; if PR litclock-dev#218 (or later) renumbers, this needs an
    update."""
    quote_corpus.reset_cache()
    # Reset to the project-bundled CSV path.
    repo_root = Path(__file__).resolve().parents[1]
    real_csv = repo_root / "image-gen" / "litclock_annotated.csv"
    if not real_csv.exists():
        pytest.skip("bundled corpus CSV not present in this checkout")
    # Attribute swap, RESTORED by monkeypatch (litclock-dev#874 red team): since
    # litclock-dev#870 `_CORPUS_PATH` carries a mode — None means "resolve
    # through the registry" — and a bare assignment here pinned the override
    # rung for every later test in the session.
    monkeypatch.setattr(quote_corpus, "_CORPUS_PATH", real_csv)
    quote_corpus.reset_cache()
    meta = quote_corpus.lookup_by_filename("quote_0000_0_credits.png")
    assert meta is not None
    assert meta["time"] == "00:00"
    # Title + author from the first 00:00 row in the bundled CSV.
    assert "Towles" in meta["author"]


# ---------------------------------------------------------------------------
# litclock-dev#590 — one corpus walk, one text pipeline
# ---------------------------------------------------------------------------


class TestUnifiedTextPipeline:
    """The PWA lookup and the runtime renderer must show IDENTICAL text.

    Pre-litclock-dev#590, quote_corpus stripped outer quote marks while the renderer's
    preprocess_quote kept them — 135 of 4,808 shipped rows rendered
    differently on the glass than in the app. The renderer's semantics won
    (eng-review decision D4); these tests pin the unification.
    """

    def test_outer_quote_marks_are_kept(self, tmp_path, monkeypatch):
        """A quote whose text genuinely starts with a quotation mark keeps
        it in the PWA — the old _strip_outer_quotes behavior is gone."""
        csv = tmp_path / "corpus.csv"
        # Raw CSV line, NOT via _write_corpus: the interesting case is a
        # field whose parsed value starts AND ends with a literal " — the
        # shape the old _strip_outer_quotes mangled on 135 shipped rows.
        # The leading space keeps csv.reader out of quoted-field mode (the
        # shipped corpus rows aren't csv-quoted either); preprocess_quote
        # trims it, exactly as the renderer does.
        csv.write_text('00:00|midnight| "Hello," she said, "at midnight."|T|A|NO\n', encoding="utf-8")
        monkeypatch.setattr(quote_corpus, "_CORPUS_PATH", csv)
        quote_corpus.reset_cache()
        meta = quote_corpus.lookup_by_filename("quote_0000_0_credits.png")
        assert meta is not None
        # preprocess_quote keeps the quote marks exactly as the renderer
        # draws them; the old pipeline stripped the outer pair.
        assert meta["quote"] == '"Hello," she said, "at midnight."'

    def test_escape_chain_and_whitespace_collapse_applied(self, tmp_path, monkeypatch):
        r"""The PHP escape chain (\n -> space) and ASCII whitespace collapse
        run on the lookup path, exactly as on the render path."""
        csv = tmp_path / "corpus.csv"
        csv.write_text("00:01|one|line\\none  two\ttabbed|T|A|NO\n", encoding="utf-8")
        monkeypatch.setattr(quote_corpus, "_CORPUS_PATH", csv)
        quote_corpus.reset_cache()
        meta = quote_corpus.lookup_by_filename("quote_0001_0.png")
        assert meta is not None
        assert meta["quote"] == "line one two tabbed"

    def test_full_corpus_lookup_matches_renderer_text(self, monkeypatch):
        """Every shipped row: lookup_by_filename text == CorpusRow.quote.

        This is the 135-row divergence, proven dead corpus-wide. Also pins
        timestring/title/author equality (PHP_TRIM_CHARS unification)."""
        repo_root = Path(__file__).resolve().parents[1]
        real_csv = repo_root / "image-gen" / "litclock_annotated.csv"
        if not real_csv.exists():
            pytest.skip("bundled corpus CSV not present in this checkout")
        import quote_renderer  # sys.path already set at module top

        monkeypatch.setattr(quote_corpus, "_CORPUS_PATH", real_csv)
        quote_corpus.reset_cache()
        mismatches = []
        for row in quote_renderer.iter_corpus(real_csv, strict=False):
            meta = quote_corpus.lookup_by_filename(f"{row.basename}_credits.png")
            assert meta is not None, row.basename
            if (meta["quote"], meta["timestring"], meta["title"], meta["author"]) != (
                row.quote,
                row.timestring,
                row.title,
                row.author,
            ):
                mismatches.append(row.basename)
        assert mismatches == []


class TestBucketEntriesParity:
    """bucket_entries must mirror the PHP namer exactly — including the
    cases that are dormant on the shipped corpus."""

    def test_malformed_time_row_counts_toward_bucket(self, tmp_path, monkeypatch):
        """PHP derives the key by substr with no shape check. A malformed
        time row must occupy an idx slot (the pre-litclock-dev#590 index skipped it,
        desyncing every later idx in the bucket from the PNG filenames)."""
        csv = tmp_path / "corpus.csv"
        csv.write_text(
            "0000x|zero|first|T|A|NO\n"  # malformed: no colon
            "0000x|zero|second|T|A|NO\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(quote_corpus, "_CORPUS_PATH", csv)
        quote_corpus.reset_cache()
        entries = quote_corpus.bucket_entries("000x")  # time[:2]+time[3:5] of "0000x"
        assert [e["idx"] for e in entries] == [0, 1]

    def test_noncontiguous_key_resets_counter_and_lookup_is_last_wins(self, tmp_path, monkeypatch):
        """A bucket key reappearing after a different key resets the PHP
        counter; the later quote_0000_0 overwrites the earlier file, so
        lookup must return the LAST idx match."""
        csv = tmp_path / "corpus.csv"
        csv.write_text(
            "00:00|midnight|early row|T|A|NO\n00:01|one|other bucket|T|A|NO\n00:00|midnight|late row|T|A|NO\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(quote_corpus, "_CORPUS_PATH", csv)
        quote_corpus.reset_cache()
        entries = quote_corpus.bucket_entries("0000")
        assert [e["idx"] for e in entries] == [0, 0]  # counter reset, both idx 0
        meta = quote_corpus.lookup_by_filename("quote_0000_0.png")
        assert meta is not None
        assert meta["quote"] == "late row"  # PHP overwrote the early file

    def test_nsfw_flag_and_counter_sharing(self, tmp_path, monkeypatch):
        """NSFW rows carry is_nsfw AND consume a bucket idx (the PHP
        counter does not skip them)."""
        csv = tmp_path / "corpus.csv"
        csv.write_text(
            "02:00|two|tame|T|A|NO\n02:00|two|racy|T|A|YES\n02:00|two|tame again|T|A|NO\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(quote_corpus, "_CORPUS_PATH", csv)
        quote_corpus.reset_cache()
        entries = quote_corpus.bucket_entries("0200")
        assert [(e["idx"], e["is_nsfw"]) for e in entries] == [(0, False), (1, True), (2, False)]


class TestCacheInvalidation:
    """The load-bearing behavior of the litclock-dev#590 index: a corpus
    rewrite must be served fresh WITHOUT reset_cache() (that is the whole
    'corpus_edit ship auto-invalidates' contract), and the contract's
    boundary — an mtime-and-size-preserving in-place rewrite — must stay
    pinned so nobody mistakes it for a bug fix opportunity or a regression.
    Review of litclock-dev#594 found this had zero coverage: every other test uses a
    fresh tmp path, so dropping mtime from the cache key passed the suite.
    """

    def _write(self, path: Path, quote: str) -> None:
        path.write_text(f"00:00|midnight|{quote}|T|A|NO\n", encoding="utf-8")

    def test_rewrite_with_new_stat_serves_fresh_without_reset(self, tmp_path, monkeypatch):
        csv = tmp_path / "corpus.csv"
        self._write(csv, "old text")
        monkeypatch.setattr(quote_corpus, "_CORPUS_PATH", csv)
        quote_corpus.reset_cache()
        assert quote_corpus.lookup_by_filename("quote_0000_0.png")["quote"] == "old text"
        self._write(csv, "new text longer")  # different size AND mtime
        # NO reset_cache() — the (path, mtime, size) key must invalidate alone.
        assert quote_corpus.lookup_by_filename("quote_0000_0.png")["quote"] == "new text longer"

    def test_same_mtime_and_size_rewrite_is_served_stale(self, tmp_path, monkeypatch):
        """Contract boundary: identical (mtime_ns, size) after a rewrite is
        indistinguishable from no change — the cache serves the old index.
        corpus_edit ship's normal write path never produces this shape;
        reset_cache() is the escape hatch (asserted below)."""
        csv = tmp_path / "corpus.csv"
        self._write(csv, "old text")
        st = csv.stat()
        monkeypatch.setattr(quote_corpus, "_CORPUS_PATH", csv)
        quote_corpus.reset_cache()
        assert quote_corpus.lookup_by_filename("quote_0000_0.png")["quote"] == "old text"
        self._write(csv, "new text")  # same length as "old text"? no — force size match:
        self._write(csv, "old texx")  # same byte length as "old text"
        os.utime(csv, ns=(st.st_atime_ns, st.st_mtime_ns))  # restore mtime exactly
        assert quote_corpus.lookup_by_filename("quote_0000_0.png")["quote"] == "old text"  # stale, by contract
        quote_corpus.reset_cache()
        assert quote_corpus.lookup_by_filename("quote_0000_0.png")["quote"] == "old texx"  # escape hatch works

    def test_missing_file_recovers_when_created(self, tmp_path, monkeypatch):
        """A control-server-shaped consumer starting before the corpus
        exists must recover once the file appears (the (0,0) stat sentinel
        must not pin the empty index)."""
        csv = tmp_path / "corpus.csv"
        monkeypatch.setattr(quote_corpus, "_CORPUS_PATH", csv)
        quote_corpus.reset_cache()
        assert quote_corpus.bucket_entries("0000") == ()
        assert quote_corpus.lookup_by_filename("quote_0000_0.png") is None
        self._write(csv, "born late")
        assert quote_corpus.lookup_by_filename("quote_0000_0.png")["quote"] == "born late"

    def test_bucket_entries_returns_immutable_container(self, tmp_path, monkeypatch):
        """The cached bucket cannot be reordered/extended by callers — a
        future in-place filter must not poison the process-wide cache."""
        csv = tmp_path / "corpus.csv"
        self._write(csv, "only row")
        monkeypatch.setattr(quote_corpus, "_CORPUS_PATH", csv)
        quote_corpus.reset_cache()
        entries = quote_corpus.bucket_entries("0000")
        assert isinstance(entries, tuple)
        assert quote_corpus.bucket_entries("0000") == entries


class TestGroundTruthBasenames:
    """Absolute pins against the SHIPPED corpus, beyond the Towles
    quote_0000_0 smoke row. These catch shared-constant regressions that
    move both sides of the parity tests together (litclock-dev#594 review): a bad edit
    to the walk shifts iter_corpus and the index identically, so only a
    hardcoded expectation trips. If a corpus edit renumbers these buckets,
    update the pins."""

    @pytest.fixture(autouse=True)
    def _real_corpus(self, monkeypatch):
        repo_root = Path(__file__).resolve().parents[1]
        real_csv = repo_root / "image-gen" / "litclock_annotated.csv"
        if not real_csv.exists():
            pytest.skip("bundled corpus CSV not present in this checkout")
        monkeypatch.setattr(quote_corpus, "_CORPUS_PATH", real_csv)
        quote_corpus.reset_cache()

    def test_mid_bucket_nsfw_row(self):
        meta = quote_corpus.lookup_by_filename("quote_0005_1_nsfw_credits.png")
        assert meta is not None
        assert meta["author"] == "David Foster Wallace"
        assert meta["title"] == "Infinite Jest"

    def test_tame_row_adjacent_to_nsfw(self):
        # idx 0 of the same 00:05 bucket is a tame row — the NSFW neighbor
        # at idx 1 must not shift it.
        meta = quote_corpus.lookup_by_filename("quote_0005_0_credits.png")
        assert meta is not None
        assert meta["time"] == "00:05"


class TestNsfwBasenameIdentity:
    """litclock-dev#594 review (Codex + security convergence): basename identity is
    (hhmm, idx, nsfw-suffix) — ALL three. A mismatch means the images on
    disk and the corpus disagree (desync window); refuse rather than serve
    possibly-mature text to a filtered device's PWA."""

    @pytest.fixture(autouse=True)
    def _corpus(self, tmp_path, monkeypatch):
        csv = tmp_path / "corpus.csv"
        csv.write_text(
            "03:00|three|tame row|T|A|NO\n03:00|three|racy row|T|A|YES\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(quote_corpus, "_CORPUS_PATH", csv)
        quote_corpus.reset_cache()

    def test_matching_suffix_resolves(self):
        assert quote_corpus.lookup_by_filename("quote_0300_0_credits.png")["quote"] == "tame row"
        assert quote_corpus.lookup_by_filename("quote_0300_1_nsfw_credits.png")["quote"] == "racy row"

    def test_tame_filename_refuses_nsfw_row(self):
        """A desynced corpus that turned idx 1 mature must NOT surface its
        text under the tame filename — None -> PWA stale banner."""
        assert quote_corpus.lookup_by_filename("quote_0300_1_credits.png") is None

    def test_nsfw_filename_refuses_tame_row(self):
        assert quote_corpus.lookup_by_filename("quote_0300_0_nsfw_credits.png") is None


# ── litclock-dev#870: the corpus follows the active language via the registry ──


def _write_registry(root: Path, languages: dict[str, dict]) -> None:
    import json

    (root / "languages.json").write_text(
        json.dumps({"schema_version": 1, "fleet_default": "en", "languages": languages}), encoding="utf-8"
    )


def _lang_entry(code: str, corpus_rel: str, *, status: str = "active") -> dict:
    return {
        "code": code,
        "native_name": code,
        "status": status,
        "corpus": {"path": corpus_rel, "rows": 1, "sfw_coverage_pct": 0.1},
        "strings": f"languages/{code}/strings.json",
        "plural_forms": ["one", "other"],
    }


def _one_row(path: Path, author: str, quote: str = "quote") -> None:
    _write_corpus(path, [("00:00", "midnight", quote, f"Title {author}", author, "NO")])


@pytest.fixture
def two_language_registry(tmp_path, monkeypatch):
    """A repo root with an English and an `xx` corpus, each one row at 00:00,
    registered in a tmp languages.json. Both modules are pointed at the tmp
    root, the shipped-default constant at the tmp English file (so the image
    tier is observable), and LITCLOCK_LANGUAGE is read from the process env."""
    import strings_catalog

    (tmp_path / "corpora").mkdir()
    en = tmp_path / "corpora" / "en.csv"
    xx = tmp_path / "corpora" / "xx.csv"
    _one_row(en, "Author EN", "english quote")
    _one_row(xx, "Author XX", "xx quote")
    _write_registry(tmp_path, {"en": _lang_entry("en", "corpora/en.csv"), "xx": _lang_entry("xx", "corpora/xx.csv")})
    monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", tmp_path / "languages.json")
    monkeypatch.setattr(strings_catalog, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(quote_corpus, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(quote_corpus, "_DEFAULT_CORPUS_PATH", en)
    monkeypatch.setattr(quote_corpus, "_CORPUS_PATH", None)
    monkeypatch.delenv("LITCLOCK_LANGUAGE", raising=False)
    monkeypatch.setenv("LITCLOCK_ENV_FILE", str(tmp_path / "no-env.sh"))  # no env.sh channel
    strings_catalog.reset_cache()
    quote_corpus.reset_cache()
    yield {"en": en, "xx": xx, "root": tmp_path}
    strings_catalog.reset_cache()
    quote_corpus.reset_cache()


def _rewrite_registry(reg, languages):
    import strings_catalog

    _write_registry(reg["root"], languages)
    strings_catalog.reset_cache()


def _author(hhmm="0000"):
    return quote_corpus.bucket_entries(hhmm)[0]["author"]


class TestCorpusFollowsTheActiveLanguage:
    def test_default_is_the_english_registry_corpus(self, two_language_registry):
        assert quote_corpus.corpus_path() == two_language_registry["en"]
        assert [e["quote_raw"] for e in quote_corpus.bucket_entries("0000")] == ["english quote"]

    def test_an_active_language_reads_its_own_corpus(self, two_language_registry, monkeypatch):
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "xx")
        assert quote_corpus.corpus_path() == two_language_registry["xx"]
        assert _author() == "Author XX"

    def test_switching_language_within_one_process_switches_the_corpus(self, two_language_registry, monkeypatch):
        """The lru_cache trap named on the issue: a naive resolution pins the
        FIRST language's corpus for the life of the process. Three lookups,
        no reset_cache() between them, in a single process."""
        assert _author() == "Author EN"
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "xx")
        assert _author() == "Author XX"
        monkeypatch.delenv("LITCLOCK_LANGUAGE")
        assert _author() == "Author EN"

    def test_the_renderer_selection_pool_follows_too(self, two_language_registry, monkeypatch):
        """rows_for_time(None, …) is what the clock's runtime path calls."""
        import quote_renderer

        monkeypatch.setenv("LITCLOCK_LANGUAGE", "xx")
        assert [r.quote for r in quote_renderer.rows_for_time(None, "0000")] == ["xx quote"]

    def test_an_unknown_code_degrades_to_english(self, two_language_registry, monkeypatch):
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "zz")
        assert quote_corpus.corpus_path() == two_language_registry["en"]

    def test_an_incubating_code_degrades_to_english(self, two_language_registry, monkeypatch):
        _rewrite_registry(
            two_language_registry,
            {"en": _lang_entry("en", "corpora/en.csv"), "xx": _lang_entry("xx", "corpora/xx.csv", status="incubating")},
        )
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "xx")
        assert quote_corpus.corpus_path() == two_language_registry["en"]

    def test_a_registry_corpus_missing_on_disk_falls_back_to_english_with_one_warning(
        self, two_language_registry, monkeypatch, caplog
    ):
        import logging

        _rewrite_registry(
            two_language_registry,
            {"en": _lang_entry("en", "corpora/en.csv"), "xx": _lang_entry("xx", "corpora/gone.csv")},
        )
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "xx")
        with caplog.at_level(logging.WARNING, logger="strings_catalog"):
            assert quote_corpus.corpus_path() == two_language_registry["en"]
            assert _author() == "Author EN"
            quote_corpus.corpus_path()
        hits = [r for r in caplog.records if "corpora/gone.csv" in r.getMessage()]
        assert len(hits) == 1, "the missing-corpus warning must fire once, not per lookup"

    def test_an_empty_corpus_does_not_shadow_english(self, two_language_registry, monkeypatch):
        """litclock-dev#874 review: existence alone let a zero-byte translation win over a
        healthy English corpus and paint nothing."""
        two_language_registry["xx"].write_text("", encoding="utf-8")
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "xx")
        assert quote_corpus.corpus_path() == two_language_registry["en"]

    @pytest.mark.parametrize("rel", ["../outside.csv", "/etc/passwd"])
    def test_a_corpus_path_outside_the_checkout_is_refused(self, two_language_registry, monkeypatch, caplog, rel):
        """litclock-dev#874 review (Codex + security): `root / rel` discards the root for an
        absolute rel and follows `..`, so a registry typo could serve any
        pi-readable file's lines as quote text."""
        import logging

        outside = two_language_registry["root"].parent / "outside.csv"
        _one_row(outside, "Author OUTSIDE")
        _rewrite_registry(
            two_language_registry, {"en": _lang_entry("en", "corpora/en.csv"), "xx": _lang_entry("xx", rel)}
        )
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "xx")
        with caplog.at_level(logging.WARNING, logger="strings_catalog"):
            assert quote_corpus.corpus_path() == two_language_registry["en"]
        assert any("outside the checkout" in r.getMessage() for r in caplog.records)

    def test_a_symlink_loop_is_unusable_not_fatal(self, two_language_registry, monkeypatch):
        """litclock-dev#874 red team: resolve() raises RuntimeError on a loop, which the
        first guard (OSError only) let escape — and the runtime tier's broad
        except would then have dropped to PNGs every minute instead of
        falling through to English."""
        loop = two_language_registry["root"] / "corpora" / "loop.csv"
        loop.symlink_to(loop)
        _rewrite_registry(
            two_language_registry,
            {"en": _lang_entry("en", "corpora/en.csv"), "xx": _lang_entry("xx", "corpora/loop.csv")},
        )
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "xx")
        assert quote_corpus.corpus_path() == two_language_registry["en"]

    def test_a_symlink_out_of_the_checkout_is_refused(self, two_language_registry, monkeypatch):
        outside = two_language_registry["root"].parent / "outside2.csv"
        _one_row(outside, "Author OUTSIDE")
        link = two_language_registry["root"] / "corpora" / "link.csv"
        link.symlink_to(outside)
        _rewrite_registry(
            two_language_registry,
            {"en": _lang_entry("en", "corpora/en.csv"), "xx": _lang_entry("xx", "corpora/link.csv")},
        )
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "xx")
        assert quote_corpus.corpus_path() == two_language_registry["en"]

    def test_both_registry_corpora_missing_lands_on_the_shipped_default_with_two_warnings(
        self, two_language_registry, monkeypatch, caplog
    ):
        import logging

        shipped = two_language_registry["root"] / "shipped.csv"
        _one_row(shipped, "Author SHIPPED")
        monkeypatch.setattr(quote_corpus, "_DEFAULT_CORPUS_PATH", shipped)
        _rewrite_registry(
            two_language_registry,
            {"en": _lang_entry("en", "corpora/gone-en.csv"), "xx": _lang_entry("xx", "corpora/gone-xx.csv")},
        )
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "xx")
        with caplog.at_level(logging.WARNING, logger="strings_catalog"):
            assert quote_corpus.corpus_path() == shipped
            assert _author() == "Author SHIPPED"
            quote_corpus.corpus_path()
        hits = [r for r in caplog.records if "gone-" in r.getMessage()]
        assert len(hits) == 2, "one warning per missing rung, each once"

    def test_the_env_override_wins_over_the_registry_for_both_tiers(self, two_language_registry, monkeypatch):
        override = two_language_registry["root"] / "override.csv"
        _one_row(override, "Author OV")
        monkeypatch.setattr(quote_corpus, "_CORPUS_PATH", override)
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "xx")
        assert quote_corpus.corpus_path() == override
        assert quote_corpus.image_corpus_path() == override
        assert _author() == "Author OV"
        assert quote_corpus.lookup_by_filename("quote_0000_0_credits.png")["author"] == "Author OV"

    def test_the_image_lookup_reads_the_shipped_corpus_whatever_the_language_or_registry_says(
        self, two_language_registry, monkeypatch
    ):
        """See quote_corpus.image_corpus_path: the PNGs' provenance is the
        file the generator opens by name, not the device language and not
        even the registry's English entry."""
        other = two_language_registry["root"] / "corpora" / "other.csv"
        _one_row(other, "Author OTHER")
        _rewrite_registry(
            two_language_registry,
            {"en": _lang_entry("en", "corpora/other.csv"), "xx": _lang_entry("xx", "corpora/xx.csv")},
        )
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "xx")
        assert quote_corpus.image_corpus_path() == two_language_registry["en"]
        assert quote_corpus.lookup_by_filename("quote_0000_0_credits.png")["author"] == "Author EN"
        # ...while the renderer's pool, in the same process, is the xx corpus,
        # and an English device's runtime pool follows the registry's English.
        assert _author() == "Author XX"
        monkeypatch.delenv("LITCLOCK_LANGUAGE")
        assert _author() == "Author OTHER"


class TestRegistryCorpusAccessors:
    def test_english_points_at_the_shipped_corpus(self, monkeypatch):
        import strings_catalog

        monkeypatch.setattr(quote_corpus, "_CORPUS_PATH", None)
        monkeypatch.delenv("LITCLOCK_LANGUAGE", raising=False)
        monkeypatch.setenv("LITCLOCK_ENV_FILE", "/nonexistent/env.sh")
        strings_catalog.reset_cache()
        assert strings_catalog.corpus_relpath("en") == "image-gen/litclock_annotated.csv"
        assert quote_corpus.corpus_path() == quote_corpus._DEFAULT_CORPUS_PATH
        assert quote_corpus.image_corpus_path() == quote_corpus._DEFAULT_CORPUS_PATH

    def test_the_image_corpus_is_the_file_the_generator_opens(self):
        """litclock-dev#874 review: nothing else pins the PNGs' provenance to the resolver.
        The PHP generator, the release verifier and the render tooling all
        name this file; image_corpus_path() must name the same one."""
        repo = Path(__file__).resolve().parents[1]
        assert quote_corpus._DEFAULT_CORPUS_PATH == repo / "image-gen" / "litclock_annotated.csv"
        assert "litclock_annotated.csv" in (repo / "image-gen" / "quote_to_image.php").read_text(encoding="utf-8")
        assert 'CORPUS_FILE="$REPO_ROOT/image-gen/litclock_annotated.csv"' in (
            repo / "scripts" / "download_images.sh"
        ).read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        "entry",
        [
            {"status": "active"},
            {"status": "active", "corpus": None},
            {"status": "active", "corpus": {}},
            {"status": "active", "corpus": {"path": ""}},
            {"status": "active", "corpus": {"path": 7}},
        ],
        ids=["no-corpus", "corpus-none", "corpus-empty", "path-empty", "path-int"],
    )
    def test_a_malformed_entry_has_no_corpus_and_the_clock_still_resolves(
        self, two_language_registry, monkeypatch, entry
    ):
        import strings_catalog

        xx = {**_lang_entry("xx", "x"), **entry}
        if "corpus" not in entry:
            del xx["corpus"]
        _rewrite_registry(two_language_registry, {"en": _lang_entry("en", "corpora/en.csv"), "xx": xx})
        assert strings_catalog.corpus_relpath("xx") is None
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "xx")
        assert quote_corpus.corpus_path() == two_language_registry["en"]

    def test_unknown_code_has_no_corpus(self):
        import strings_catalog

        strings_catalog.reset_cache()
        assert strings_catalog.corpus_relpath("zz") is None
