"""Tests for image-gen/audit_quotes.py (the #192 corpus audit judge)."""

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import audit_quotes
import pytest


def _block(kind: str, **kw):
    """Mimic an anthropic content block (TextBlock / ToolUseBlock)."""
    return SimpleNamespace(type=kind, **kw)


def _message(blocks: list) -> SimpleNamespace:
    return SimpleNamespace(content=blocks)


# ── parse_tool_response ─────────────────────────────────────────────


class TestParseToolResponse:
    def test_pass(self):
        msg = _message(
            [
                _block(
                    "tool_use",
                    name="record_verdict",
                    input={"verdict": "PASS", "rationale": "scene matches"},
                )
            ]
        )
        verdict, rationale = audit_quotes.parse_tool_response(msg)
        assert verdict == "PASS"
        assert rationale == "scene matches"

    def test_fail(self):
        msg = _message(
            [
                _block(
                    "tool_use",
                    name="record_verdict",
                    input={"verdict": "FAIL", "rationale": "duration"},
                )
            ]
        )
        assert audit_quotes.parse_tool_response(msg)[0] == "FAIL"

    def test_lowercase_coerced(self):
        msg = _message(
            [
                _block(
                    "tool_use",
                    name="record_verdict",
                    input={"verdict": "pass", "rationale": ""},
                )
            ]
        )
        assert audit_quotes.parse_tool_response(msg)[0] == "PASS"

    def test_missing_tool_block_raises(self):
        msg = _message([_block("text", text="PASS\nrationale...")])
        with pytest.raises(ValueError, match="missing record_verdict"):
            audit_quotes.parse_tool_response(msg)

    def test_invalid_verdict_raises(self):
        msg = _message(
            [
                _block(
                    "tool_use",
                    name="record_verdict",
                    input={"verdict": "MAYBE", "rationale": ""},
                )
            ]
        )
        with pytest.raises(ValueError, match="Invalid verdict"):
            audit_quotes.parse_tool_response(msg)

    def test_wrong_tool_name_skipped(self):
        msg = _message([_block("tool_use", name="other_tool", input={"verdict": "PASS", "rationale": ""})])
        with pytest.raises(ValueError, match="missing record_verdict"):
            audit_quotes.parse_tool_response(msg)

    def test_rationale_truncated(self):
        msg = _message(
            [
                _block(
                    "tool_use",
                    name="record_verdict",
                    input={"verdict": "PASS", "rationale": "x" * 500},
                )
            ]
        )
        _, rationale = audit_quotes.parse_tool_response(msg)
        assert len(rationale) == 300


# ── load_rows ───────────────────────────────────────────────────────


class TestLoadRows:
    def test_loads_six_col_corpus(self, tmp_path):
        p = tmp_path / "corpus.csv"
        p.write_text("00:00|midnight|The bell|Book|Author|NO\n13:42|1.42pm|The scene|Book|Author|NO\n")
        rows = list(audit_quotes.load_rows(p))
        assert len(rows) == 2
        assert rows[0]["idx"] == 0
        assert rows[0]["time"] == "00:00"
        assert rows[1]["time"] == "13:42"

    def test_skips_short_rows(self, tmp_path):
        p = tmp_path / "corpus.csv"
        p.write_text("00:00|midnight|The bell\n13:42|1.42pm|scene|Book|Author|NO\n")
        rows = list(audit_quotes.load_rows(p))
        assert len(rows) == 1
        assert rows[0]["time"] == "13:42"

    def test_preserves_row_index(self, tmp_path):
        p = tmp_path / "corpus.csv"
        p.write_text("a|b|c\n00:00|midnight|scene|Book|Author|NO\n")
        rows = list(audit_quotes.load_rows(p))
        assert rows[0]["idx"] == 1  # row 0 was short and skipped, but idx reflects source line


# ── load_gold_set ───────────────────────────────────────────────────


def _write_gold(path, rows_with_label):
    lines = ["idx|time|match|quote|title|author|label"]
    for r in rows_with_label:
        lines.append("|".join(str(c) for c in r))
    path.write_text("\n".join(lines) + "\n")


class TestLoadGoldSet:
    def test_valid_gold(self, tmp_path):
        p = tmp_path / "gold.csv"
        _write_gold(
            p,
            [
                [0, "22:20", "In twenty minutes", "q1", "Book", "Author", "FAIL"],
                [1, "13:42", "1.42pm", "q2", "Book", "Author", "PASS"],
            ],
        )
        rows = audit_quotes.load_gold_set(p)
        assert len(rows) == 2
        assert rows[0]["label"] == "FAIL"
        assert rows[0]["idx"] == 0
        assert rows[1]["label"] == "PASS"

    def test_lowercase_label_normalized(self, tmp_path):
        p = tmp_path / "gold.csv"
        _write_gold(p, [[0, "13:42", "m", "q", "Book", "Author", "pass"]])
        assert audit_quotes.load_gold_set(p)[0]["label"] == "PASS"

    def test_empty_label_rejected(self, tmp_path):
        p = tmp_path / "gold.csv"
        _write_gold(p, [[0, "13:42", "m", "q", "Book", "Author", ""]])
        with pytest.raises(ValueError, match="unlabeled"):
            audit_quotes.load_gold_set(p)

    def test_invalid_label_rejected(self, tmp_path):
        p = tmp_path / "gold.csv"
        _write_gold(p, [[0, "13:42", "m", "q", "Book", "Author", "MAYBE"]])
        with pytest.raises(ValueError, match="unlabeled|invalid"):
            audit_quotes.load_gold_set(p)

    def test_missing_header_rejected(self, tmp_path):
        p = tmp_path / "gold.csv"
        p.write_text("")
        with pytest.raises(ValueError, match="missing header"):
            audit_quotes.load_gold_set(p)


# ── score_gold ──────────────────────────────────────────────────────


class TestScoreGold:
    def _pair(self, idx, gold_label, predicted):
        return (
            {"idx": idx, "label": gold_label},
            {"idx": idx, "verdict": predicted, "time": "00:00", "match": "", "quote": ""},
        )

    def test_perfect(self):
        gold, pred = [], []
        for i, (g, p) in enumerate(
            [
                ("FAIL", "FAIL"),
                ("FAIL", "FAIL"),
                ("PASS", "PASS"),
                ("PASS", "PASS"),
            ]
        ):
            gr, pr = self._pair(i, g, p)
            gold.append(gr)
            pred.append(pr)
        s = audit_quotes.score_gold(pred, gold)
        assert s["precision"] == 1.0
        assert s["recall"] == 1.0
        assert s["tp"] == 2 and s["fp"] == 0 and s["fn"] == 0 and s["tn"] == 2

    def test_mixed(self):
        # 4 FAILs (3 caught, 1 missed), 4 PASSes (1 false-positive)
        gold = [
            {"idx": 0, "label": "FAIL"},
            {"idx": 1, "label": "FAIL"},
            {"idx": 2, "label": "FAIL"},
            {"idx": 3, "label": "FAIL"},
            {"idx": 4, "label": "PASS"},
            {"idx": 5, "label": "PASS"},
            {"idx": 6, "label": "PASS"},
            {"idx": 7, "label": "PASS"},
        ]
        preds = [
            {"idx": 0, "verdict": "FAIL"},
            {"idx": 1, "verdict": "FAIL"},
            {"idx": 2, "verdict": "FAIL"},
            {"idx": 3, "verdict": "PASS"},  # missed
            {"idx": 4, "verdict": "FAIL"},  # false positive
            {"idx": 5, "verdict": "PASS"},
            {"idx": 6, "verdict": "PASS"},
            {"idx": 7, "verdict": "PASS"},
        ]
        s = audit_quotes.score_gold(preds, gold)
        # TP=3, FP=1, FN=1 → precision=3/4=0.75, recall=3/4=0.75
        assert s["tp"] == 3
        assert s["fp"] == 1
        assert s["fn"] == 1
        assert s["tn"] == 3
        assert s["precision"] == pytest.approx(0.75)
        assert s["recall"] == pytest.approx(0.75)

    def test_error_excluded(self):
        gold = [{"idx": 0, "label": "FAIL"}, {"idx": 1, "label": "PASS"}]
        preds = [
            {"idx": 0, "verdict": "ERROR"},
            {"idx": 1, "verdict": "PASS"},
        ]
        s = audit_quotes.score_gold(preds, gold)
        assert s["errors"] == 1
        assert s["tp"] == 0 and s["fp"] == 0

    def test_empty_denominator_safe(self):
        # All PASS in gold → no positives → recall denominator is 0
        gold = [{"idx": 0, "label": "PASS"}]
        preds = [{"idx": 0, "verdict": "PASS"}]
        s = audit_quotes.score_gold(preds, gold)
        assert s["precision"] == 0.0
        assert s["recall"] == 0.0


# ── judge_one (async, mocked client) ────────────────────────────────


class TestJudgeOne:
    def _row(self):
        return {
            "idx": 5,
            "time": "22:20",
            "match": "in twenty minutes",
            "quote": "In twenty minutes the ground was white",
            "title": "The Silver Chair",
            "author": "C.S. Lewis",
        }

    def test_pass_verdict(self):
        client = SimpleNamespace(messages=SimpleNamespace())
        client.messages.create = AsyncMock(
            return_value=_message(
                [_block("tool_use", name="record_verdict", input={"verdict": "FAIL", "rationale": "duration"})]
            )
        )
        sem = asyncio.Semaphore(1)

        async def go():
            return await audit_quotes.judge_one(client, sem, self._row())

        res = asyncio.run(go())
        assert res["verdict"] == "FAIL"
        assert res["rationale"] == "duration"
        assert res["idx"] == 5
        assert "elapsed" in res
        # Verify the call used prompt caching + tool forcing
        call = client.messages.create.await_args
        assert call.kwargs["tool_choice"] == {"type": "tool", "name": "record_verdict"}
        assert call.kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
        assert call.kwargs["model"] == audit_quotes.MODEL
        assert call.kwargs["max_tokens"] == audit_quotes.MAX_TOKENS

    def test_api_error_becomes_error_row(self):
        client = SimpleNamespace(messages=SimpleNamespace())
        client.messages.create = AsyncMock(side_effect=RuntimeError("boom"))
        sem = asyncio.Semaphore(1)

        async def go():
            return await audit_quotes.judge_one(client, sem, self._row())

        res = asyncio.run(go())
        assert res["verdict"] == "ERROR"
        assert "RuntimeError" in res["rationale"]
        assert "boom" in res["rationale"]

    def test_missing_tool_becomes_error_row(self):
        client = SimpleNamespace(messages=SimpleNamespace())
        client.messages.create = AsyncMock(return_value=_message([_block("text", text="PASS")]))
        sem = asyncio.Semaphore(1)

        async def go():
            return await audit_quotes.judge_one(client, sem, self._row())

        res = asyncio.run(go())
        assert res["verdict"] == "ERROR"
        assert "record_verdict" in res["rationale"]


# ── main fail-fast ──────────────────────────────────────────────────


class TestMain:
    def test_missing_api_key_exits_2(self, monkeypatch, capsys):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        rc = audit_quotes.main(["--sample", "1"])
        assert rc == 2
        assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


# ── --sample --seed determinism ─────────────────────────────────────


class TestSampleDeterminism:
    def test_same_seed_same_rows(self, tmp_path, monkeypatch):
        # Build a tiny corpus we can load.
        corpus = tmp_path / "corpus.csv"
        lines = [f"{h:02d}:{m:02d}|m|quote {h * 60 + m}|Book|Author|NO" for h in range(5) for m in range(5)]
        corpus.write_text("\n".join(lines) + "\n")
        monkeypatch.setattr(audit_quotes, "CSV_PATH", corpus)

        import random as _random

        # Reproduce the exact same logic as run_audit_mode's sampling step.
        rows1 = list(audit_quotes.load_rows(corpus))
        _random.seed(42)
        s1 = _random.sample(rows1, 5)

        rows2 = list(audit_quotes.load_rows(corpus))
        _random.seed(42)
        s2 = _random.sample(rows2, 5)

        assert [r["idx"] for r in s1] == [r["idx"] for r in s2]

        # And different seeds give different samples (with high probability).
        _random.seed(99)
        s3 = _random.sample(rows1, 5)
        assert [r["idx"] for r in s1] != [r["idx"] for r in s3]


# ── sidecar meta ────────────────────────────────────────────────────


class TestWriteMeta:
    def test_meta_contains_required_fields(self, tmp_path):
        path = tmp_path / "out.meta.json"
        results = [
            {"verdict": "PASS"},
            {"verdict": "FAIL"},
            {"verdict": "FAIL"},
            {"verdict": "ERROR"},
        ]
        audit_quotes.write_meta(path, results, None, {"sample": 4})
        meta = json.loads(path.read_text())
        assert meta["model"] == audit_quotes.MODEL
        assert len(meta["prompt_hash"]) == 16
        assert meta["pass"] == 1
        assert meta["fail"] == 2
        assert meta["error"] == 1
        assert meta["args"] == {"sample": 4}
        assert "timestamp_utc" in meta
        assert "git_sha" in meta

    def test_meta_includes_gold_scores_when_given(self, tmp_path):
        path = tmp_path / "out.meta.json"
        scores = {"tune": {"precision": 0.9}, "holdout": {"precision": 0.85}, "gate_passed": True}
        audit_quotes.write_meta(path, [], scores, {})
        meta = json.loads(path.read_text())
        assert meta["gold"] == scores


# ── prompt_hash stable ──────────────────────────────────────────────


def test_prompt_hash_stable():
    h1 = audit_quotes.prompt_hash()
    h2 = audit_quotes.prompt_hash()
    assert h1 == h2
    assert len(h1) == 16


# ── ensure test file does not require ANTHROPIC_API_KEY ─────────────


def test_env_does_not_require_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Module import and all above tests ran without the key; spot-check:
    assert os.environ.get("ANTHROPIC_API_KEY") is None


# ── auto-fix regression guards (review findings) ────────────────────


class TestBuildUserMessage:
    """Guards the .format() -> f-string fix. Literary quotes often contain
    literal { and } characters; .format(**row) would raise KeyError."""

    def test_braces_in_quote_do_not_raise(self):
        row = {"time": "13:42", "match": "1.42pm", "quote": "She muttered {something} quietly"}
        msg = audit_quotes._build_user_message(row)
        assert "{something}" in msg
        assert "13:42" in msg

    def test_braces_in_match_do_not_raise(self):
        row = {"time": "09:00", "match": "{x}", "quote": "At nine"}
        msg = audit_quotes._build_user_message(row)
        assert "{x}" in msg

    def test_judge_one_survives_braced_quote(self):
        """End-to-end: a braced quote must not crash judge_one; it should round-trip
        the verdict cleanly through the mocked client."""
        import asyncio
        from unittest.mock import AsyncMock

        client = SimpleNamespace(messages=SimpleNamespace())
        client.messages.create = AsyncMock(
            return_value=_message(
                [
                    _block(
                        "tool_use",
                        name="record_verdict",
                        input={"verdict": "PASS", "rationale": "ok"},
                    )
                ]
            )
        )
        row = {
            "idx": 1,
            "time": "13:42",
            "match": "{1.42pm}",
            "quote": "Quote with {braces} and {more}",
            "title": "T",
            "author": "A",
        }
        sem = asyncio.Semaphore(1)
        res = asyncio.run(audit_quotes.judge_one(client, sem, row))
        assert res["verdict"] == "PASS"
        # And the braced content made it into the outgoing user message.
        call = client.messages.create.await_args
        assert "{braces}" in call.kwargs["messages"][0]["content"]


class TestCsvInjectionSanitize:
    def test_leading_formula_chars_prefixed(self):
        for bad in ("=CMD()", "+1+1", "-5", "@SUM()"):
            assert audit_quotes._sanitize_csv_cell(bad).startswith("'")

    def test_non_formula_text_unchanged(self):
        assert audit_quotes._sanitize_csv_cell("normal text") == "normal text"
        assert audit_quotes._sanitize_csv_cell("1.42pm") == "1.42pm"

    def test_non_string_unchanged(self):
        assert audit_quotes._sanitize_csv_cell(42) == 42
        assert audit_quotes._sanitize_csv_cell(None) is None

    def test_write_results_sanitizes_rationale(self, tmp_path):
        path = tmp_path / "out.csv"
        rows = [
            {
                "idx": 0,
                "time": "13:42",
                "match": "m",
                "verdict": "FAIL",
                "rationale": "=CMD('pwned')",
                "quote": "+load()",
                "title": "A Book",
                "author": "Author",
            }
        ]
        cols = ["idx", "time", "match", "verdict", "rationale", "quote", "title", "author"]
        audit_quotes.write_results(path, rows, cols)
        content = path.read_text()
        # Leading formula chars prefixed with a single quote.
        assert "'=CMD('pwned')" in content
        assert "'+load()" in content


class TestGitShaWarningOnFallback:
    def test_warning_printed_when_git_unavailable(self, monkeypatch, capsys):
        def boom(*a, **kw):
            raise FileNotFoundError("git not installed")

        monkeypatch.setattr(audit_quotes.subprocess, "check_output", boom)
        sha = audit_quotes.git_sha()
        assert sha == "unknown"
        assert "git_sha() unavailable" in capsys.readouterr().err


class TestJudgeOneErrorNarrowing:
    """Network/API/parse failures become ERROR rows; programming errors propagate."""

    def _row(self):
        return {"idx": 7, "time": "22:20", "match": "m", "quote": "q", "title": "T", "author": "A"}

    def test_valueerror_from_parse_becomes_error_row(self):
        """parse_tool_response raising ValueError (missing tool block, invalid verdict) should
        still be caught so one bad row doesn't abort the audit."""
        import asyncio
        from unittest.mock import AsyncMock

        client = SimpleNamespace(messages=SimpleNamespace())
        # Tool block missing — parse_tool_response raises ValueError
        client.messages.create = AsyncMock(return_value=_message([_block("text", text="PASS")]))
        sem = asyncio.Semaphore(1)
        res = asyncio.run(audit_quotes.judge_one(client, sem, self._row()))
        assert res["verdict"] == "ERROR"
        assert "record_verdict" in res["rationale"]

    def test_programming_error_propagates(self):
        """KeyError from our own code must surface, not be silently swallowed as ERROR."""
        import asyncio
        from unittest.mock import AsyncMock

        client = SimpleNamespace(messages=SimpleNamespace())
        client.messages.create = AsyncMock(side_effect=KeyError("real_bug"))
        sem = asyncio.Semaphore(1)
        with pytest.raises(KeyError):
            asyncio.run(audit_quotes.judge_one(client, sem, self._row()))

    def test_typeerror_propagates(self):
        import asyncio
        from unittest.mock import AsyncMock

        client = SimpleNamespace(messages=SimpleNamespace())
        client.messages.create = AsyncMock(side_effect=TypeError("bad call"))
        sem = asyncio.Semaphore(1)
        with pytest.raises(TypeError):
            asyncio.run(audit_quotes.judge_one(client, sem, self._row()))

    def test_generic_runtimeerror_becomes_error_row(self):
        """RuntimeError stands in for any network/API error class we don't explicitly
        know about; these should still turn into ERROR rows rather than crashing."""
        import asyncio
        from unittest.mock import AsyncMock

        client = SimpleNamespace(messages=SimpleNamespace())
        client.messages.create = AsyncMock(side_effect=RuntimeError("connection reset"))
        sem = asyncio.Semaphore(1)
        res = asyncio.run(audit_quotes.judge_one(client, sem, self._row()))
        assert res["verdict"] == "ERROR"
        assert "RuntimeError" in res["rationale"]


class TestProgressCheckpoint:
    """Streaming JSONL checkpoint: mid-run SIGINT/crash doesn't cost the whole $4-5."""

    def test_progress_path_derivation(self, tmp_path):
        p = audit_quotes._progress_path_for(tmp_path / "audit_fails.csv")
        assert p == tmp_path / "audit_fails.progress.jsonl"

    def test_load_progress_empty_when_missing(self, tmp_path):
        assert audit_quotes._load_progress(tmp_path / "nope.jsonl") == {}

    def test_load_progress_parses_valid_lines(self, tmp_path):
        p = tmp_path / "p.jsonl"
        p.write_text('{"idx":1,"verdict":"PASS","rationale":"a"}\n{"idx":3,"verdict":"FAIL","rationale":"b"}\n')
        loaded = audit_quotes._load_progress(p)
        assert set(loaded.keys()) == {1, 3}
        assert loaded[1]["verdict"] == "PASS"

    def test_load_progress_tolerates_partial_final_line(self, tmp_path):
        """A crash mid-flush can leave an incomplete JSON line; don't blow up on resume."""
        p = tmp_path / "p.jsonl"
        p.write_text(
            '{"idx":1,"verdict":"PASS","rationale":"a"}\n{"idx":2,"verdict":"FA'  # truncated
        )
        loaded = audit_quotes._load_progress(p)
        assert set(loaded.keys()) == {1}

    def test_load_progress_skips_missing_idx(self, tmp_path):
        p = tmp_path / "p.jsonl"
        p.write_text('{"verdict":"PASS"}\n{"idx":5,"verdict":"FAIL"}\n')
        loaded = audit_quotes._load_progress(p)
        assert set(loaded.keys()) == {5}

    def test_judge_all_skips_already_judged_idx(self, tmp_path, monkeypatch):
        """Rows whose idx is in the progress file must not hit the API again."""
        import asyncio

        # Seed a checkpoint with idx 1 and 2 already done
        pp = tmp_path / "x.progress.jsonl"
        pp.write_text(
            '{"idx":1,"verdict":"PASS","rationale":"cached","time":"01:00","match":"m",'
            '"quote":"q","title":"T","author":"A"}\n'
            '{"idx":2,"verdict":"FAIL","rationale":"cached","time":"02:00","match":"m",'
            '"quote":"q","title":"T","author":"A"}\n'
        )

        # Fake AsyncAnthropic so we can count how many times it was asked
        from unittest.mock import AsyncMock

        created = []

        class FakeAnthropic:
            def __init__(self, *a, **kw):
                self.messages = SimpleNamespace()
                self.messages.create = AsyncMock(
                    return_value=_message(
                        [
                            _block(
                                "tool_use",
                                name="record_verdict",
                                input={"verdict": "PASS", "rationale": "fresh"},
                            )
                        ]
                    )
                )
                self._wrapped_create = self.messages.create

                async def create_wrapped(**kw):
                    created.append(kw)
                    return await self._wrapped_create(**kw)

                self.messages.create = create_wrapped

        # Install a stub anthropic module so `from anthropic import AsyncAnthropic`
        # inside judge_all resolves to our fake without needing the real SDK.
        import sys as _sys
        import types as _types

        fake_mod = _types.ModuleType("anthropic")
        fake_mod.AsyncAnthropic = FakeAnthropic
        monkeypatch.setitem(_sys.modules, "anthropic", fake_mod)

        rows = [
            {"idx": i, "time": f"0{i}:00", "match": "m", "quote": "q", "title": "T", "author": "A"}
            for i in (1, 2, 3, 4)
        ]
        results = asyncio.run(audit_quotes.judge_all(rows, workers=2, progress_path=pp))

        # Only idx 3 and 4 should have hit the API; 1 and 2 came from the checkpoint.
        assert len(created) == 2
        # But the final results still cover all 4 rows
        by_idx = {r["idx"]: r for r in results}
        assert set(by_idx.keys()) == {1, 2, 3, 4}
        assert by_idx[1]["rationale"] == "cached"
        assert by_idx[2]["rationale"] == "cached"

        # And the new verdicts should have been appended to the progress file
        lines = pp.read_text().strip().splitlines()
        assert len(lines) == 4  # 2 seeded + 2 newly persisted


# ── end-to-end integration (patched judge_all) ──────────────────────


def _install_fake_judge_all(monkeypatch, verdict_by_idx=None, record=None):
    """Replace audit_quotes.judge_all with an in-process fake that records what it saw
    and returns synthetic verdicts. `record` is a list that gets appended with the rows
    passed in (order-preserved). `verdict_by_idx` maps idx -> 'PASS'|'FAIL'|'ERROR'."""

    async def fake(rows, workers, progress_path=None):
        if record is not None:
            record.append([r["idx"] for r in rows])
        out = []
        for r in rows:
            v = (verdict_by_idx or {}).get(r["idx"], "PASS")
            out.append({**r, "verdict": v, "rationale": "stub", "elapsed": 0.0})
        return out

    monkeypatch.setattr(audit_quotes, "judge_all", fake)


class TestGoldMode:
    """run_gold_mode integration — the gate that protects the $4-5 run."""

    def _write_gold(self, path, rows):
        import csv

        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter="|")
            w.writerow(["idx", "time", "match", "quote", "title", "author", "label"])
            for r in rows:
                w.writerow(r)

    def _gold_rows(self, n_fail, n_pass):
        """Build n_fail+n_pass gold rows. idx is simply 0..N-1."""
        rows = []
        i = 0
        for _ in range(n_fail):
            rows.append([i, f"{i % 24:02d}:00", "in twenty minutes", f"q{i}", "B", "A", "FAIL"])
            i += 1
        for _ in range(n_pass):
            rows.append([i, f"{i % 24:02d}:00", "eight", f"q{i}", "B", "A", "PASS"])
            i += 1
        return rows

    def test_gate_passes_when_holdout_perfect(self, tmp_path, monkeypatch):
        """21 gold rows: 20 tune + 1 holdout (FAIL). Judge labels match exactly → gate PASSED, rc=0."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        # 11 FAILs, 10 PASSes = 21 rows. tune[0:20] = 11 FAIL + 9 PASS; holdout[20] = last PASS.
        # We need >=1 positive (FAIL) in holdout for recall to be defined and == 1.
        # Rearrange: 10 FAIL + 10 PASS + 1 FAIL so holdout is FAIL.
        gold = (
            [[i, f"{i:02d}:00", "m", f"q{i}", "B", "A", "FAIL"] for i in range(10)]
            + [[i, f"{i:02d}:00", "m", f"q{i}", "B", "A", "PASS"] for i in range(10, 20)]
            + [[20, "20:00", "m", "q20", "B", "A", "FAIL"]]
        )
        gp = tmp_path / "gold.csv"
        self._write_gold(gp, gold)

        verdicts = {r[0]: r[-1] for r in gold}  # perfect judge
        _install_fake_judge_all(monkeypatch, verdict_by_idx=verdicts)

        rc = audit_quotes.main(["--gold", str(gp), "--out", str(tmp_path / "o.csv")])
        assert rc == 0  # gate passed

    def test_gate_fails_on_low_recall(self, tmp_path, monkeypatch, capsys):
        """Holdout has FAILs but the judge predicts PASS for all of them → recall=0 → gate FAILED, rc=4."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        gold = [[i, f"{i:02d}:00", "m", f"q{i}", "B", "A", "PASS"] for i in range(20)] + [
            [i + 20, f"{i:02d}:00", "m", f"q{i + 20}", "B", "A", "FAIL"] for i in range(5)
        ]
        gp = tmp_path / "gold.csv"
        self._write_gold(gp, gold)

        # Judge predicts PASS for EVERYTHING — misses all FAILs in holdout
        _install_fake_judge_all(monkeypatch, verdict_by_idx={r[0]: "PASS" for r in gold})

        rc = audit_quotes.main(["--gold", str(gp), "--out", str(tmp_path / "o.csv")])
        assert rc == 4
        assert "GATE FAILED" in capsys.readouterr().err

    def test_gate_fails_on_low_precision(self, tmp_path, monkeypatch, capsys):
        """Judge returns FAIL for everything → lots of false positives → precision below 0.80 → rc=4."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        gold = [[i, f"{i:02d}:00", "m", f"q{i}", "B", "A", "FAIL"] for i in range(5)] + [
            [i + 5, f"{i:02d}:00", "m", f"q{i + 5}", "B", "A", "PASS"] for i in range(20)
        ]
        gp = tmp_path / "gold.csv"
        self._write_gold(gp, gold)

        # Holdout (idx 20..24) is all PASS in gold but judge returns FAIL → 0 TP, 5 FP → precision=0
        _install_fake_judge_all(monkeypatch, verdict_by_idx={r[0]: "FAIL" for r in gold})
        rc = audit_quotes.main(["--gold", str(gp), "--out", str(tmp_path / "o.csv")])
        assert rc == 4
        assert "GATE FAILED" in capsys.readouterr().err

    def test_gold_set_too_small_returns_1(self, tmp_path, monkeypatch, capsys):
        """Gold set has <= GOLD_TUNE_SIZE rows → can't form a holdout → rc=1."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        gold = [[i, f"{i:02d}:00", "m", f"q{i}", "B", "A", "FAIL"] for i in range(10)]
        gp = tmp_path / "gold.csv"
        self._write_gold(gp, gold)
        _install_fake_judge_all(monkeypatch)
        rc = audit_quotes.main(["--gold", str(gp), "--out", str(tmp_path / "o.csv")])
        assert rc == 1
        assert "only 10" in capsys.readouterr().err

    def test_gate_pass_written_into_meta(self, tmp_path, monkeypatch):
        """The gate result must land in the meta sidecar so re-audits are diffable."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        gold = (
            [[i, f"{i:02d}:00", "m", f"q{i}", "B", "A", "FAIL"] for i in range(10)]
            + [[i + 10, f"{i:02d}:00", "m", f"q{i + 10}", "B", "A", "PASS"] for i in range(10)]
            + [[20, "20:00", "m", "q20", "B", "A", "FAIL"]]
        )
        gp = tmp_path / "gold.csv"
        self._write_gold(gp, gold)
        _install_fake_judge_all(monkeypatch, verdict_by_idx={r[0]: r[-1] for r in gold})

        out = tmp_path / "o.csv"
        audit_quotes.main(["--gold", str(gp), "--out", str(out)])
        meta = json.loads(out.with_suffix(".meta.json").read_text())
        assert meta["gold"]["gate_passed"] is True
        assert "tune" in meta["gold"]
        assert "holdout" in meta["gold"]


class TestSampleDeterminismEndToEnd:
    """End-to-end verification of --sample --seed determinism by exercising the real
    audit_quotes.main() path, not a parallel reimplementation in the test."""

    def test_same_seed_produces_same_row_order(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        corpus = tmp_path / "corpus.csv"
        lines = [f"{i:02d}:00|m|q{i}|B|A|NO" for i in range(30)]
        corpus.write_text("\n".join(lines) + "\n")
        monkeypatch.setattr(audit_quotes, "CSV_PATH", corpus)

        records = []
        _install_fake_judge_all(monkeypatch, record=records)

        rc1 = audit_quotes.main(["--sample", "5", "--seed", "42", "--out", str(tmp_path / "a.csv")])
        rc2 = audit_quotes.main(["--sample", "5", "--seed", "42", "--out", str(tmp_path / "b.csv")])
        rc3 = audit_quotes.main(["--sample", "5", "--seed", "99", "--out", str(tmp_path / "c.csv")])

        assert rc1 == rc2 == rc3 == 0
        # Three invocations → three records
        assert len(records) == 3
        # Same seed → identical row selection
        assert records[0] == records[1]
        # Different seed → different selection (statistically near-certain with 30 rows, 5 picks)
        assert records[0] != records[2]
