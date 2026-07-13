"""Tests for image-gen/apply_audit.py — critical path, mutates source of truth."""

import csv

import apply_audit
import pytest

# ── fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def corpus_file(tmp_path):
    """Small 6-col pipe corpus. idx 0,3,5 at 22:20; idx 1 at 13:42; others filler."""
    p = tmp_path / "corpus.csv"
    rows = [
        [
            "22:20",
            "in twenty minutes",
            "In twenty minutes the ground was white",
            "The Silver Chair",
            "C.S. Lewis",
            "NO",
        ],
        ["13:42", "1.42pm", "At 1.42pm she arrived", "Book B", "Author B", "NO"],
        ["08:00", "eight", "At eight o'clock he left", "Book C", "Author C", "NO"],
        ["22:20", "in twenty minutes", "Another duration quote", "Book D", "Author D", "NO"],
        ["09:15", "quarter past nine", "A quarter past nine", "Book E", "Author E", "NO"],
        ["22:20", "twenty minutes", "Yet another duration", "Book F", "Author F", "NO"],
    ]
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter="|")
        w.writerows(rows)
    return p


def _reviewed(tmp_path, rows: list[dict]) -> "object":
    """Write a comma-delimited reviewed audit CSV."""
    p = tmp_path / "reviewed.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["idx", "time", "match", "quote", "decision"])
        w.writeheader()
        w.writerows(rows)
    return p


def _read_out(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.reader(f, delimiter="|"))


# ── happy path ──────────────────────────────────────────────────────


class TestApplyHappyPath:
    def test_drops_flagged_rows_preserves_order(self, tmp_path, corpus_file):
        out = tmp_path / "out.csv"
        reviewed = _reviewed(
            tmp_path,
            [
                {
                    "idx": 0,
                    "time": "22:20",
                    "match": "in twenty minutes",
                    "quote": "In twenty minutes the ground was white",
                    "decision": "DROP",
                },
                {
                    "idx": 3,
                    "time": "22:20",
                    "match": "in twenty minutes",
                    "quote": "Another duration quote",
                    "decision": "KEEP",
                },
                {
                    "idx": 5,
                    "time": "22:20",
                    "match": "twenty minutes",
                    "quote": "Yet another duration",
                    "decision": "DROP",
                },
            ],
        )
        rc = apply_audit.main(["--reviewed", str(reviewed), "--corpus", str(corpus_file), "--out", str(out)])
        assert rc == 0

        result = _read_out(out)
        # 6 input rows - 2 dropped = 4 rows; order preserved.
        assert len(result) == 4
        assert [r[2] for r in result] == [
            "At 1.42pm she arrived",  # idx 1
            "At eight o'clock he left",  # idx 2
            "Another duration quote",  # idx 3 (KEEP)
            "A quarter past nine",  # idx 4
        ]

    def test_preserves_six_column_format(self, tmp_path, corpus_file):
        out = tmp_path / "out.csv"
        reviewed = _reviewed(
            tmp_path,
            [
                {
                    "idx": 0,
                    "time": "22:20",
                    "match": "in twenty minutes",
                    "quote": "In twenty minutes the ground was white",
                    "decision": "DROP",
                },
            ],
        )
        apply_audit.main(["--reviewed", str(reviewed), "--corpus", str(corpus_file), "--out", str(out)])
        result = _read_out(out)
        assert all(len(r) == 6 for r in result), "All output rows must have 6 columns"
        # NSFW column (col 5) preserved
        assert all(r[5] == "NO" for r in result)

    def test_keep_only_writes_all_rows(self, tmp_path, corpus_file):
        out = tmp_path / "out.csv"
        reviewed = _reviewed(
            tmp_path,
            [
                {
                    "idx": 0,
                    "time": "22:20",
                    "match": "in twenty minutes",
                    "quote": "In twenty minutes the ground was white",
                    "decision": "KEEP",
                },
            ],
        )
        rc = apply_audit.main(["--reviewed", str(reviewed), "--corpus", str(corpus_file), "--out", str(out)])
        assert rc == 0
        assert len(_read_out(out)) == 6

    def test_case_insensitive_decision(self, tmp_path, corpus_file):
        out = tmp_path / "out.csv"
        reviewed = _reviewed(
            tmp_path,
            [
                {
                    "idx": 0,
                    "time": "22:20",
                    "match": "in twenty minutes",
                    "quote": "In twenty minutes the ground was white",
                    "decision": "drop",
                },
                {"idx": 1, "time": "13:42", "match": "1.42pm", "quote": "At 1.42pm she arrived", "decision": " Keep "},
            ],
        )
        rc = apply_audit.main(["--reviewed", str(reviewed), "--corpus", str(corpus_file), "--out", str(out)])
        assert rc == 0
        assert len(_read_out(out)) == 5


# ── abort paths (exit 2) ────────────────────────────────────────────


class TestValidationAborts:
    def test_idx_mismatch_time_aborts(self, tmp_path, corpus_file, capsys):
        out = tmp_path / "out.csv"
        reviewed = _reviewed(
            tmp_path,
            [
                {
                    "idx": 0,
                    "time": "99:99",
                    "match": "in twenty minutes",
                    "quote": "In twenty minutes the ground was white",
                    "decision": "DROP",
                },
            ],
        )
        with pytest.raises(SystemExit) as exc:
            apply_audit.main(["--reviewed", str(reviewed), "--corpus", str(corpus_file), "--out", str(out)])
        assert exc.value.code == 2
        assert "drifted" in capsys.readouterr().err
        assert not out.exists(), "Output must not be written when validation fails"

    def test_idx_mismatch_quote_aborts(self, tmp_path, corpus_file):
        out = tmp_path / "out.csv"
        reviewed = _reviewed(
            tmp_path,
            [
                {
                    "idx": 0,
                    "time": "22:20",
                    "match": "in twenty minutes",
                    "quote": "Completely different quote",
                    "decision": "DROP",
                },
            ],
        )
        with pytest.raises(SystemExit) as exc:
            apply_audit.main(["--reviewed", str(reviewed), "--corpus", str(corpus_file), "--out", str(out)])
        assert exc.value.code == 2

    def test_invalid_decision_aborts(self, tmp_path, corpus_file, capsys):
        out = tmp_path / "out.csv"
        reviewed = _reviewed(
            tmp_path,
            [
                {
                    "idx": 0,
                    "time": "22:20",
                    "match": "in twenty minutes",
                    "quote": "In twenty minutes the ground was white",
                    "decision": "MAYBE",
                },
            ],
        )
        with pytest.raises(SystemExit) as exc:
            apply_audit.main(["--reviewed", str(reviewed), "--corpus", str(corpus_file), "--out", str(out)])
        assert exc.value.code == 2
        assert "invalid decision" in capsys.readouterr().err

    def test_missing_decision_column_aborts(self, tmp_path, corpus_file):
        p = tmp_path / "bad.csv"
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["idx", "time", "match", "quote"])
            w.writeheader()
            w.writerow({"idx": 0, "time": "22:20", "match": "m", "quote": "q"})
        with pytest.raises(SystemExit) as exc:
            apply_audit.main(["--reviewed", str(p), "--corpus", str(corpus_file), "--out", str(tmp_path / "out.csv")])
        assert exc.value.code == 2

    def test_out_of_range_idx_aborts(self, tmp_path, corpus_file):
        out = tmp_path / "out.csv"
        reviewed = _reviewed(
            tmp_path,
            [{"idx": 9999, "time": "22:20", "match": "m", "quote": "q", "decision": "DROP"}],
        )
        with pytest.raises(SystemExit) as exc:
            apply_audit.main(["--reviewed", str(reviewed), "--corpus", str(corpus_file), "--out", str(out)])
        assert exc.value.code == 2

    def test_non_integer_idx_aborts(self, tmp_path, corpus_file):
        out = tmp_path / "out.csv"
        reviewed = _reviewed(
            tmp_path,
            [{"idx": "abc", "time": "22:20", "match": "m", "quote": "q", "decision": "DROP"}],
        )
        with pytest.raises(SystemExit) as exc:
            apply_audit.main(["--reviewed", str(reviewed), "--corpus", str(corpus_file), "--out", str(out)])
        assert exc.value.code == 2


# ── empty-slot gate (exit 3) ────────────────────────────────────────


class TestEmptySlotGate:
    def test_drops_that_empty_a_slot_aborts(self, tmp_path, corpus_file, capsys):
        # idx 2 is the only 08:00 and idx 4 is the only 09:15 — dropping both empties two slots.
        out = tmp_path / "out.csv"
        reviewed = _reviewed(
            tmp_path,
            [
                {"idx": 2, "time": "08:00", "match": "eight", "quote": "At eight o'clock he left", "decision": "DROP"},
                {
                    "idx": 4,
                    "time": "09:15",
                    "match": "quarter past nine",
                    "quote": "A quarter past nine",
                    "decision": "DROP",
                },
            ],
        )
        rc = apply_audit.main(["--reviewed", str(reviewed), "--corpus", str(corpus_file), "--out", str(out)])
        assert rc == 3
        err = capsys.readouterr().err
        assert "08:00" in err
        assert "09:15" in err
        assert not out.exists()

    def test_allow_empty_slots_bypasses_gate(self, tmp_path, corpus_file, capsys):
        out = tmp_path / "out.csv"
        reviewed = _reviewed(
            tmp_path,
            [
                {"idx": 2, "time": "08:00", "match": "eight", "quote": "At eight o'clock he left", "decision": "DROP"},
            ],
        )
        rc = apply_audit.main(
            [
                "--reviewed",
                str(reviewed),
                "--corpus",
                str(corpus_file),
                "--out",
                str(out),
                "--allow-empty-slots",
            ]
        )
        assert rc == 0
        assert "08:00" in capsys.readouterr().err  # slot still printed for visibility
        result = _read_out(out)
        assert len(result) == 5
        assert not any(r[0] == "08:00" for r in result)

    def test_multi_per_slot_drop_does_not_trip_gate(self, tmp_path, corpus_file):
        # 22:20 has 3 rows (idx 0,3,5). Drop two — slot still has one.
        out = tmp_path / "out.csv"
        reviewed = _reviewed(
            tmp_path,
            [
                {
                    "idx": 0,
                    "time": "22:20",
                    "match": "in twenty minutes",
                    "quote": "In twenty minutes the ground was white",
                    "decision": "DROP",
                },
                {
                    "idx": 5,
                    "time": "22:20",
                    "match": "twenty minutes",
                    "quote": "Yet another duration",
                    "decision": "DROP",
                },
            ],
        )
        rc = apply_audit.main(["--reviewed", str(reviewed), "--corpus", str(corpus_file), "--out", str(out)])
        assert rc == 0


# ── coverage helper unit tests ──────────────────────────────────────


class TestCoverage:
    def test_coverage_counts_and_skips_dropped(self, corpus_file):
        corpus = apply_audit.load_corpus(corpus_file)
        before = apply_audit.coverage(corpus, set())
        after = apply_audit.coverage(corpus, {0, 3, 5})
        assert before["22:20"] == 3
        assert after["22:20"] == 0
        assert after["13:42"] == 1

    def test_empty_slot_regressions(self):
        before = __import__("collections").Counter({"22:20": 3, "13:42": 1, "09:00": 1})
        after = __import__("collections").Counter({"22:20": 1, "13:42": 1, "09:00": 0})
        assert apply_audit.empty_slot_regressions(before, after) == ["09:00"]

    def test_no_regressions_when_slot_was_already_zero(self):
        before = __import__("collections").Counter({"22:20": 0})
        after = __import__("collections").Counter({"22:20": 0})
        assert apply_audit.empty_slot_regressions(before, after) == []


# ── auto-fix regression guards (review findings) ────────────────────


class TestAutoFixGuards:
    def test_same_path_guard_aborts(self, tmp_path, corpus_file, capsys):
        """--out == --corpus would silently overwrite the source-of-truth corpus."""
        reviewed = _reviewed(
            tmp_path,
            [
                {
                    "idx": 0,
                    "time": "22:20",
                    "match": "in twenty minutes",
                    "quote": "In twenty minutes the ground was white",
                    "decision": "DROP",
                }
            ],
        )
        rc = apply_audit.main(["--reviewed", str(reviewed), "--corpus", str(corpus_file), "--out", str(corpus_file)])
        assert rc == 2
        assert "same file" in capsys.readouterr().err

    def test_bom_in_reviewed_csv_tolerated(self, tmp_path, corpus_file):
        """Excel/Numbers often prepend a BOM; utf-8-sig transparently strips it."""
        out = tmp_path / "out.csv"
        bom_reviewed = tmp_path / "reviewed.csv"
        with open(bom_reviewed, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["idx", "time", "match", "quote", "decision"])
            w.writeheader()
            w.writerow(
                {
                    "idx": 0,
                    "time": "22:20",
                    "match": "in twenty minutes",
                    "quote": "In twenty minutes the ground was white",
                    "decision": "DROP",
                }
            )
        rc = apply_audit.main(["--reviewed", str(bom_reviewed), "--corpus", str(corpus_file), "--out", str(out)])
        assert rc == 0

    def test_title_drift_aborts_when_reviewed_carries_title(self, tmp_path, corpus_file, capsys):
        """Extended drift check: if reviewed CSV has `title`, it must also match."""
        p = tmp_path / "reviewed.csv"
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["idx", "time", "match", "quote", "title", "author", "decision"])
            w.writeheader()
            w.writerow(
                {
                    "idx": 0,
                    "time": "22:20",
                    "match": "in twenty minutes",
                    "quote": "In twenty minutes the ground was white",
                    "title": "Wrong Title",
                    "author": "C.S. Lewis",
                    "decision": "DROP",
                }
            )
        with pytest.raises(SystemExit) as exc:
            apply_audit.main(["--reviewed", str(p), "--corpus", str(corpus_file), "--out", str(tmp_path / "o.csv")])
        assert exc.value.code == 2
        assert "title drifted" in capsys.readouterr().err

    def test_coverage_skips_malformed_rows(self, tmp_path):
        """coverage() must agree with audit_quotes.load_rows (which skips rows <5 cols)
        so a 'safe' slot count can't include rows the auditor never evaluated.
        """
        p = tmp_path / "c.csv"
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter="|")
            w.writerow(["09:00", "m", "q", "T", "A", "NO"])
            w.writerow(["09:00", "short"])  # malformed — only 2 cols
            w.writerow(["09:00", "m", "q", "T", "A", "NO"])
        corpus = apply_audit.load_corpus(p)
        before = apply_audit.coverage(corpus, set())
        assert before["09:00"] == 2  # malformed row excluded

    def test_flag_decision_accepted_and_kept_in_corpus(self, tmp_path, corpus_file, capsys):
        """FLAG rows stay in the corpus (so the clock keeps serving them) but get
        counted separately in the summary so the magnitude is visible."""
        out = tmp_path / "out.csv"
        reviewed = _reviewed(
            tmp_path,
            [
                {
                    "idx": 0,
                    "time": "22:20",
                    "match": "in twenty minutes",
                    "quote": "In twenty minutes the ground was white",
                    "decision": "FLAG",
                },
                {
                    "idx": 3,
                    "time": "22:20",
                    "match": "in twenty minutes",
                    "quote": "Another duration quote",
                    "decision": "DROP",
                },
            ],
        )
        rc = apply_audit.main(["--reviewed", str(reviewed), "--corpus", str(corpus_file), "--out", str(out)])
        assert rc == 0
        err = capsys.readouterr().err
        assert "FLAG: 1" in err
        assert "DROP: 1" in err
        result = _read_out(out)
        # idx 0 (FLAG) kept, idx 3 (DROP) removed: 5 rows written.
        assert len(result) == 5
        assert any("In twenty minutes the ground was white" == r[2] for r in result)

    def test_flag_writes_sidecar_csv(self, tmp_path, corpus_file):
        out = tmp_path / "out.csv"
        reviewed = _reviewed(
            tmp_path,
            [
                {
                    "idx": 2,
                    "time": "08:00",
                    "match": "eight",
                    "quote": "At eight o'clock he left",
                    "decision": "FLAG",
                },
            ],
        )
        apply_audit.main(["--reviewed", str(reviewed), "--corpus", str(corpus_file), "--out", str(out)])
        flagged_path = out.with_name(out.stem + ".flagged.csv")
        assert flagged_path.exists()
        with open(flagged_path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["idx"] == "2"
        assert rows[0]["decision"] == "FLAG"

    def test_no_flagged_file_when_no_flags(self, tmp_path, corpus_file):
        out = tmp_path / "out.csv"
        reviewed = _reviewed(
            tmp_path,
            [
                {
                    "idx": 0,
                    "time": "22:20",
                    "match": "in twenty minutes",
                    "quote": "In twenty minutes the ground was white",
                    "decision": "DROP",
                },
            ],
        )
        apply_audit.main(["--reviewed", str(reviewed), "--corpus", str(corpus_file), "--out", str(out)])
        assert not out.with_name(out.stem + ".flagged.csv").exists()

    def test_invalid_decision_message_mentions_flag(self, tmp_path, corpus_file, capsys):
        reviewed = _reviewed(
            tmp_path,
            [
                {
                    "idx": 0,
                    "time": "22:20",
                    "match": "in twenty minutes",
                    "quote": "In twenty minutes the ground was white",
                    "decision": "MAYBE",
                },
            ],
        )
        with pytest.raises(SystemExit):
            apply_audit.main(
                ["--reviewed", str(reviewed), "--corpus", str(corpus_file), "--out", str(tmp_path / "o.csv")]
            )
        err = capsys.readouterr().err
        assert "FLAG" in err  # error message updated

    def test_duplicate_idx_with_matching_decision_is_idempotent(self, tmp_path, corpus_file):
        """A reviewed CSV that lists idx=0 twice with the same decision is OK.
        Re-running apply after a merge or manual re-save shouldn't become surprising."""
        out = tmp_path / "out.csv"
        reviewed = _reviewed(
            tmp_path,
            [
                {
                    "idx": 0,
                    "time": "22:20",
                    "match": "in twenty minutes",
                    "quote": "In twenty minutes the ground was white",
                    "decision": "DROP",
                },
                {
                    "idx": 0,
                    "time": "22:20",
                    "match": "in twenty minutes",
                    "quote": "In twenty minutes the ground was white",
                    "decision": "DROP",
                },
            ],
        )
        rc = apply_audit.main(["--reviewed", str(reviewed), "--corpus", str(corpus_file), "--out", str(out)])
        assert rc == 0
        assert len(_read_out(out)) == 5  # 6 corpus - 1 unique DROP

    def test_duplicate_idx_with_conflicting_decisions_aborts(self, tmp_path, corpus_file, capsys):
        """The real footgun: same idx, divergent decisions. Must abort (exit 2)
        rather than silently pick one."""
        reviewed = _reviewed(
            tmp_path,
            [
                {
                    "idx": 0,
                    "time": "22:20",
                    "match": "in twenty minutes",
                    "quote": "In twenty minutes the ground was white",
                    "decision": "DROP",
                },
                {
                    "idx": 0,
                    "time": "22:20",
                    "match": "in twenty minutes",
                    "quote": "In twenty minutes the ground was white",
                    "decision": "KEEP",
                },
            ],
        )
        with pytest.raises(SystemExit) as exc:
            apply_audit.main(
                [
                    "--reviewed",
                    str(reviewed),
                    "--corpus",
                    str(corpus_file),
                    "--out",
                    str(tmp_path / "o.csv"),
                ]
            )
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "conflicting" in err
        assert "idx=0" in err

    def test_duplicate_idx_drop_vs_flag_aborts(self, tmp_path, corpus_file):
        """DROP vs FLAG is the two-reviewer collision case FLAG was added to catch."""
        reviewed = _reviewed(
            tmp_path,
            [
                {
                    "idx": 0,
                    "time": "22:20",
                    "match": "in twenty minutes",
                    "quote": "In twenty minutes the ground was white",
                    "decision": "DROP",
                },
                {
                    "idx": 0,
                    "time": "22:20",
                    "match": "in twenty minutes",
                    "quote": "In twenty minutes the ground was white",
                    "decision": "FLAG",
                },
            ],
        )
        with pytest.raises(SystemExit) as exc:
            apply_audit.main(
                [
                    "--reviewed",
                    str(reviewed),
                    "--corpus",
                    str(corpus_file),
                    "--out",
                    str(tmp_path / "o.csv"),
                ]
            )
        assert exc.value.code == 2

    def test_atomic_write_leaves_no_tmp_file(self, tmp_path, corpus_file):
        """Successful write must rename tmp onto the target — no stale .tmp sibling."""
        out = tmp_path / "out.csv"
        reviewed = _reviewed(
            tmp_path,
            [
                {
                    "idx": 0,
                    "time": "22:20",
                    "match": "in twenty minutes",
                    "quote": "In twenty minutes the ground was white",
                    "decision": "DROP",
                }
            ],
        )
        rc = apply_audit.main(["--reviewed", str(reviewed), "--corpus", str(corpus_file), "--out", str(out)])
        assert rc == 0
        assert out.exists()
        assert not (out.parent / (out.name + ".tmp")).exists()
