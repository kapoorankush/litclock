"""Tests for image-gen/build_gold_set.py.

The gold set gates the $4-5 full-corpus audit run, so a silent bug here
(regex drift, non-deterministic seed, off-by-one on TOTAL) would poison
calibration with no alarm.
"""

import csv

import build_gold_set
import pytest


def _mk_corpus(tmp_path, rows):
    """Write a 6-col pipe corpus at tmp_path/corpus.csv."""
    p = tmp_path / "corpus.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter="|")
        w.writerows(rows)
    return p


def _synthetic_corpus(n, duration_count=0):
    """Generate a synthetic corpus with `duration_count` rows whose match starts with a
    duration prefix, and n-duration_count plain rows.
    """
    rows = []
    for i in range(duration_count):
        rows.append([f"{i:02d}:00", "in twenty minutes", f"q{i}", "B", "A", "NO"])
    for i in range(duration_count, n):
        rows.append([f"{i:02d}:00", "eight", f"q{i}", "B", "A", "NO"])
    return rows


# ── DURATION_PREFIX regex ───────────────────────────────────────────


class TestDurationPrefix:
    @pytest.mark.parametrize(
        "s,expected",
        [
            ("In twenty minutes", True),
            ("After a while", True),
            ("for five seconds", True),
            ("Within the hour", True),
            ("another moment", True),
            ("IN TWENTY", True),  # case-insensitive
            ("information age", False),  # must be a word, not a prefix
            ("afternoon", False),
            ("forever", False),
            ("quarter past", False),
            ("eight o'clock", False),
            ("", False),
        ],
    )
    def test_word_boundary(self, s, expected):
        assert bool(build_gold_set.DURATION_PREFIX.match(s)) is expected


# ── pick() ──────────────────────────────────────────────────────────


class TestPick:
    def test_returns_exactly_total_rows(self, tmp_path):
        corpus = build_gold_set.load_corpus(_mk_corpus(tmp_path, _synthetic_corpus(200, 10)))
        picked = build_gold_set.pick(corpus)
        assert len(picked) == build_gold_set.TOTAL

    def test_duration_suspects_first_in_idx_order(self, tmp_path):
        """The 16 duration suspects must land at the start, sorted by idx."""
        corpus = build_gold_set.load_corpus(_mk_corpus(tmp_path, _synthetic_corpus(200, 16)))
        picked = build_gold_set.pick(corpus)
        # First 16 picked rows should all match the duration prefix
        for r in picked[:16]:
            assert build_gold_set.DURATION_PREFIX.match(r["match"])
        # And they should be in ascending idx order
        idx_order = [r["idx"] for r in picked[:16]]
        assert idx_order == sorted(idx_order)

    def test_random_slice_deterministic_across_runs(self, tmp_path):
        """Same corpus → same pick, every time. Seeded `random.Random(RANDOM_SEED)`."""
        corpus = build_gold_set.load_corpus(_mk_corpus(tmp_path, _synthetic_corpus(200, 5)))
        a = build_gold_set.pick(corpus)
        b = build_gold_set.pick(corpus)
        assert [r["idx"] for r in a] == [r["idx"] for r in b]

    def test_no_overlap_between_suspects_and_random_slice(self, tmp_path):
        corpus = build_gold_set.load_corpus(_mk_corpus(tmp_path, _synthetic_corpus(200, 5)))
        picked = build_gold_set.pick(corpus)
        idxs = [r["idx"] for r in picked]
        assert len(idxs) == len(set(idxs))  # no duplicates


# ── write_gold ──────────────────────────────────────────────────────


class TestWriteGold:
    def test_header_is_seven_columns_with_empty_label(self, tmp_path):
        out = tmp_path / "gold.csv"
        rows = [{"idx": 7, "time": "22:20", "match": "in twenty minutes", "quote": "q", "title": "T", "author": "A"}]
        build_gold_set.write_gold(out, rows)
        with open(out, encoding="utf-8") as f:
            reader = csv.reader(f, delimiter="|")
            header = next(reader)
            assert header == ["idx", "time", "match", "quote", "title", "author", "label"]
            row = next(reader)
            assert row == ["7", "22:20", "in twenty minutes", "q", "T", "A", ""]

    def test_empty_rows_still_writes_header(self, tmp_path):
        out = tmp_path / "gold.csv"
        build_gold_set.write_gold(out, [])
        assert out.exists()
        with open(out, encoding="utf-8") as f:
            assert f.readline().strip() == "idx|time|match|quote|title|author|label"


# ── main() ──────────────────────────────────────────────────────────


class TestMain:
    def test_refuses_overwrite_without_force(self, tmp_path, capsys):
        corpus = _mk_corpus(tmp_path, _synthetic_corpus(200, 5))
        out = tmp_path / "gold.csv"
        out.write_text("pre-existing content")
        rc = build_gold_set.main(["--corpus", str(corpus), "--out", str(out)])
        assert rc == 2
        assert "exists" in capsys.readouterr().err
        # And the file's original content must be preserved.
        assert out.read_text() == "pre-existing content"

    def test_force_overwrites(self, tmp_path):
        corpus = _mk_corpus(tmp_path, _synthetic_corpus(200, 5))
        out = tmp_path / "gold.csv"
        out.write_text("old")
        rc = build_gold_set.main(["--corpus", str(corpus), "--out", str(out), "--force"])
        assert rc == 0
        assert out.read_text() != "old"
        assert out.read_text().startswith("idx|time|match|quote|title|author|label")

    def test_writes_total_rows(self, tmp_path):
        corpus = _mk_corpus(tmp_path, _synthetic_corpus(200, 5))
        out = tmp_path / "gold.csv"
        build_gold_set.main(["--corpus", str(corpus), "--out", str(out)])
        with open(out, encoding="utf-8") as f:
            line_count = sum(1 for _ in f)
        # header + TOTAL data rows
        assert line_count == build_gold_set.TOTAL + 1


# ── load_corpus ─────────────────────────────────────────────────────


class TestLoadCorpus:
    def test_skips_short_rows(self, tmp_path):
        p = _mk_corpus(
            tmp_path,
            [
                ["22:20", "m", "q", "T", "A", "NO"],
                ["bad"],  # malformed — <5 cols
                ["13:42", "m", "q", "T", "A", "NO"],
            ],
        )
        rows = build_gold_set.load_corpus(p)
        assert len(rows) == 2
        # idx reflects source-line ordinal, so the second valid row is idx=2
        assert [r["idx"] for r in rows] == [0, 2]
