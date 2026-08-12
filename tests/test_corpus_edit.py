"""Tests for image-gen/corpus_edit.py — the one-command corpus-edit orchestrator (issue litclock-dev#211)."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import corpus_edit
import pytest

# ── fixtures ──────────────────────────────────────────────────────────


def _csv(rows: list[list[str]]) -> str:
    return "\n".join("|".join(r) for r in rows) + "\n"


HEAD_ROWS = [
    ["21:10", "10.10pm.", "10.10pm. When you turn your recorder on", "Bridget Jones", "Helen Fielding", "NO"],
    ["21:10", "9:10 p.m.", "9:10 p.m. adjust clock", "Bridget Jones", "Helen Fielding", "NO"],
    ["21:10", "ten past nine", "It was ten past nine", "A Taste for Death", "P.D. James", "NO"],
    [
        "22:10",
        "ten minutes past ten",
        "the kitchen clock says ten minutes past ten",
        "The Girl at Central",
        "Geraldine Bonner",
        "NO",
    ],
    ["08:00", "eight", "At eight o'clock", "Book X", "Author X", "NO"],
]


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Sandbox with patched module paths so nothing touches the real repo."""
    repo = tmp_path / "repo"
    images = repo / "images"
    metadata = images / "metadata"
    scripts = repo / "scripts"
    image_gen = repo / "image-gen"
    for d in (repo, images, metadata, scripts, image_gen):
        d.mkdir(parents=True, exist_ok=True)

    corpus = image_gen / "litclock_annotated.csv"
    version = repo / ".images-version"
    php = image_gen / "quote_to_image.php"
    release_script = scripts / "release_images.sh"
    manifest = images / "manifest.json"

    php.write_text("<?php // stub\n")
    release_script.write_text("#!/bin/bash\nexit 0\n")
    release_script.chmod(0o755)
    version.write_text("v1\n")

    monkeypatch.setattr(corpus_edit, "REPO_ROOT", repo)
    monkeypatch.setattr(corpus_edit, "CORPUS_PATH", corpus)
    monkeypatch.setattr(corpus_edit, "IMAGES_DIR", images)
    monkeypatch.setattr(corpus_edit, "METADATA_DIR", metadata)
    monkeypatch.setattr(corpus_edit, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(corpus_edit, "MANIFEST_BAK", images / "manifest.json.bak")
    monkeypatch.setattr(corpus_edit, "IMAGES_VERSION_FILE", version)
    monkeypatch.setattr(corpus_edit, "PHP_GENERATOR", php)
    monkeypatch.setattr(corpus_edit, "RELEASE_SCRIPT", release_script)

    @dataclass
    class S:
        repo: Path
        corpus: Path
        images: Path
        metadata: Path
        manifest: Path
        version: Path
        release_script: Path

        def write_corpus(self, rows: list[list[str]]) -> None:
            self.corpus.write_text(_csv(rows))

        def write_images(self, buckets_counts: dict[str, int]) -> None:
            for key, n in buckets_counts.items():
                for i in range(n):
                    (self.images / f"quote_{key}_{i}.png").write_bytes(b"x")
                    (self.metadata / f"quote_{key}_{i}_credits.png").write_bytes(b"x")

        def write_manifest(self, payload: dict) -> None:
            self.manifest.write_text(json.dumps(payload))

    return S(
        repo=repo,
        corpus=corpus,
        images=images,
        metadata=metadata,
        manifest=manifest,
        version=version,
        release_script=release_script,
    )


# ── parsing + fingerprint + bucket keys ───────────────────────────────


class TestParsing:
    def test_parse_corpus_handles_nsfw_column(self):
        rows = corpus_edit.parse_corpus(_csv([["22:10", "ten", "q", "t", "a", "YES"]]))
        assert len(rows) == 1
        assert rows[0].is_nsfw is True

    def test_parse_corpus_defaults_nsfw_false_when_missing(self):
        rows = corpus_edit.parse_corpus(_csv([["22:10", "ten", "q", "t", "a"]]))
        assert rows[0].is_nsfw is False

    def test_parse_corpus_skips_malformed_rows(self):
        rows = corpus_edit.parse_corpus(_csv([["only", "four", "cols", "here"]]))
        assert rows == []

    def test_fingerprint_is_deterministic(self):
        rows = corpus_edit.parse_corpus(_csv([["22:10", "ten", "q", "t", "a", "NO"]]))
        assert rows[0].fingerprint() == rows[0].fingerprint()

    def test_fingerprint_distinguishes_nsfw(self):
        a = corpus_edit.parse_corpus(_csv([["22:10", "ten", "q", "t", "a", "NO"]]))[0]
        b = corpus_edit.parse_corpus(_csv([["22:10", "ten", "q", "t", "a", "YES"]]))[0]
        assert a.fingerprint() != b.fingerprint()

    def test_bucket_key_strips_colon(self):
        assert corpus_edit.bucket_key("21:10") == "2110"
        assert corpus_edit.bucket_key("00:00") == "0000"


# ── dirty-bucket detection ────────────────────────────────────────────


class TestDirtyBuckets:
    def test_clean_tree_has_no_dirty_buckets(self):
        rows = corpus_edit.parse_corpus(_csv(HEAD_ROWS))
        assert corpus_edit.compute_dirty_buckets(rows, rows) == set()

    def test_retag_marks_both_source_and_destination(self):
        head = corpus_edit.parse_corpus(_csv(HEAD_ROWS))
        work_rows = [r[:] for r in HEAD_ROWS]
        work_rows[0][0] = "22:10"  # retag 21:10 -> 22:10
        work = corpus_edit.parse_corpus(_csv(work_rows))
        assert corpus_edit.compute_dirty_buckets(head, work) == {"2110", "2210"}

    def test_delete_marks_single_bucket(self):
        head = corpus_edit.parse_corpus(_csv(HEAD_ROWS))
        work = corpus_edit.parse_corpus(_csv([r for i, r in enumerate(HEAD_ROWS) if i != 1]))
        assert corpus_edit.compute_dirty_buckets(head, work) == {"2110"}

    def test_add_marks_single_bucket(self):
        head = corpus_edit.parse_corpus(_csv(HEAD_ROWS))
        work_rows = HEAD_ROWS + [["22:10", "ten-ten", "new quote at ten ten", "T", "A", "NO"]]
        work = corpus_edit.parse_corpus(_csv(work_rows))
        assert corpus_edit.compute_dirty_buckets(head, work) == {"2210"}

    def test_in_bucket_reorder_marks_bucket(self):
        head = corpus_edit.parse_corpus(_csv(HEAD_ROWS))
        reorder = [HEAD_ROWS[2], HEAD_ROWS[1], HEAD_ROWS[0]] + HEAD_ROWS[3:]
        work = corpus_edit.parse_corpus(_csv(reorder))
        assert corpus_edit.compute_dirty_buckets(head, work) == {"2110"}

    def test_diff_changed_rows_returns_new_fingerprints_only(self):
        head = corpus_edit.parse_corpus(_csv(HEAD_ROWS))
        work_rows = [r[:] for r in HEAD_ROWS]
        work_rows[0][0] = "22:10"
        work = corpus_edit.parse_corpus(_csv(work_rows))
        changed = corpus_edit.diff_changed_rows(head, work)
        assert len(changed) == 1
        assert changed[0].time == "22:10"
        assert changed[0].match == "10.10pm."


# ── time-tag validation ───────────────────────────────────────────────


class TestValidate:
    def test_ten_ten_pm_tagged_as_21_10_fails(self):
        """The exact bug that motivated issue litclock-dev#211."""
        rows = corpus_edit.parse_corpus(
            _csv([["21:10", "10.10pm.", "10.10pm. When you turn your recorder", "BJD", "HF", "NO"]])
        )
        errors = corpus_edit.validate_rows(rows)
        assert len(errors) == 1
        assert "10.10pm." in errors[0]

    def test_ten_ten_pm_tagged_as_22_10_passes(self):
        """After the retag, validation must pass."""
        rows = corpus_edit.parse_corpus(
            _csv([["22:10", "10.10pm.", "10.10pm. When you turn your recorder", "BJD", "HF", "NO"]])
        )
        assert corpus_edit.validate_rows(rows) == []

    def test_existing_clean_rows_pass(self):
        clean = [
            ["21:10", "9:10 p.m.", "9:10 p.m. something", "T", "A", "NO"],
            ["22:10", "ten minutes past ten", "ten minutes past ten", "T", "A", "NO"],
            ["08:00", "eight", "At eight o'clock", "T", "A", "NO"],
        ]
        rows = corpus_edit.parse_corpus(_csv(clean))
        assert corpus_edit.validate_rows(rows) == []


class TestMidwordEdges:
    """litclock-dev#540's WARN tier had zero coverage (litclock-dev#605 item 6) —
    notably, validate_midword_edges bootstraps sys.path and imports the
    production renderer at call time, so an ImportError there would crash
    `corpus_edit ship` mid-flow. These tests run that real import."""

    def test_midword_edge_is_flagged(self):
        """'noon' ending inside 'noonday' is the canonical half-bold bug."""
        rows = corpus_edit.parse_corpus(_csv([["12:00", "noon", "It was noonday", "T", "A", "NO"]]))
        warns = corpus_edit.validate_midword_edges(rows)
        assert len(warns) == 1
        assert "mid-word" in warns[0]

    def test_clean_edges_produce_no_warns(self):
        rows = corpus_edit.parse_corpus(_csv([["12:00", "noon", "It was noon when", "T", "A", "NO"]]))
        assert corpus_edit.validate_midword_edges(rows) == []

    def test_warn_helper_prints_to_stderr(self, capsys):
        rows = corpus_edit.parse_corpus(_csv([["12:00", "noon", "It was noonday", "T", "A", "NO"]]))
        corpus_edit.warn_midword_edges(rows)
        err = capsys.readouterr().err
        assert "WARN:" in err
        assert "mid-word" in err

    def test_warn_helper_silent_on_clean_rows(self, capsys):
        rows = corpus_edit.parse_corpus(_csv([["12:00", "noon", "It was noon when", "T", "A", "NO"]]))
        corpus_edit.warn_midword_edges(rows)
        assert capsys.readouterr().err == ""

    def test_both_command_paths_call_the_warn(self):
        """litclock-dev#540: a warn only in `validate` is bypassable by never running
        it — ship is the primary workflow. Pin that BOTH paths call the one
        shared helper (litclock-dev#605 item 16 collapsed the two copies).
        AST Call nodes, not source-substring (/review on litclock-dev#614): a substring
        match is satisfied by a comment containing the call text."""
        import ast
        import inspect
        import textwrap

        def calls_warn(fn):
            tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
            return any(
                isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "warn_midword_edges"
                for node in ast.walk(tree)
            )

        assert calls_warn(corpus_edit.cmd_validate)
        # cmd_ship only dispatches; the CSV-edit path is where changed rows
        # exist (generator ship regen-only mode has none by definition).
        assert calls_warn(corpus_edit._cmd_ship_csv)

    def test_cmd_validate_emits_the_warn_end_to_end(self, sandbox, monkeypatch, capsys):
        """Behavioral pin for the validate path (/review on litclock-dev#614): a mid-word
        row driven through the real cmd_validate must reach stderr as a WARN
        and stay non-fatal (rc 0) — litclock-dev#540's contract, previously pinned
        only structurally. The fs-floor stage is stubbed out so the test
        runs without freetype (the floor has its own gated tests)."""
        monkeypatch.setattr(corpus_edit, "read_head_corpus", lambda: "")
        monkeypatch.setattr(corpus_edit, "validate_fs_floor", lambda *a, **k: ([], []))
        sandbox.corpus.write_text(_csv([["12:00", "noon", "It was noonday", "T", "A", "NO"]]))
        rc = corpus_edit.cmd_validate(argparse.Namespace(allow_small=None))
        err = capsys.readouterr().err
        assert "mid-word" in err
        assert rc == 0


class TestBucketContiguity:
    def test_contiguous_buckets_pass(self):
        rows = corpus_edit.parse_corpus(_csv(HEAD_ROWS))
        assert corpus_edit.validate_bucket_contiguity(rows) == []

    def test_non_contiguous_bucket_flagged(self):
        # 21:10 → 22:10 → 21:10 — second 21:10 run would collide.
        bad = [
            ["21:10", "ten past nine", "It was ten past nine", "T", "A", "NO"],
            ["22:10", "ten minutes past ten", "ten minutes past ten", "T", "A", "NO"],
            ["21:10", "9:10 p.m.", "9:10 p.m. adjust clock", "T", "A", "NO"],
        ]
        rows = corpus_edit.parse_corpus(_csv(bad))
        errors = corpus_edit.validate_bucket_contiguity(rows)
        assert len(errors) == 1
        assert "21:10" in errors[0]
        assert "row 3" in errors[0]

    def test_multiple_non_contiguous_buckets_all_flagged(self):
        bad = [
            ["00:00", "midnight", "Q", "T", "A", "NO"],
            ["01:00", "one", "Q", "T", "A", "NO"],
            ["00:00", "midnight", "Q2", "T", "A", "NO"],
            ["02:00", "two", "Q", "T", "A", "NO"],
            ["01:00", "one", "Q2", "T", "A", "NO"],
        ]
        rows = corpus_edit.parse_corpus(_csv(bad))
        errors = corpus_edit.validate_bucket_contiguity(rows)
        assert len(errors) == 2

    def test_single_bucket_no_error(self):
        rows = corpus_edit.parse_corpus(_csv([["00:00", "midnight", "Q", "T", "A", "NO"]] * 5))
        assert corpus_edit.validate_bucket_contiguity(rows) == []

    def test_empty_rows_no_error(self):
        assert corpus_edit.validate_bucket_contiguity([]) == []


# ── wipe ──────────────────────────────────────────────────────────────


class TestWipe:
    def test_wipes_only_dirty_buckets(self, sandbox):
        sandbox.write_images({"2110": 3, "2210": 2, "0800": 1})
        removed = corpus_edit.wipe_buckets({"2110"}, dry_run=False)
        remaining = {p.name for p in sandbox.images.glob("quote_*.png")}
        assert remaining == {"quote_2210_0.png", "quote_2210_1.png", "quote_0800_0.png"}
        meta_remaining = {p.name for p in sandbox.metadata.glob("quote_*.png")}
        assert meta_remaining == {
            "quote_2210_0_credits.png",
            "quote_2210_1_credits.png",
            "quote_0800_0_credits.png",
        }
        # 3 images + 3 credits
        assert len(removed) == 6

    def test_dry_run_lists_but_does_not_delete(self, sandbox):
        sandbox.write_images({"2110": 2})
        removed = corpus_edit.wipe_buckets({"2110"}, dry_run=True)
        assert len(removed) == 4
        assert {p.name for p in sandbox.images.glob("quote_*.png")} == {"quote_2110_0.png", "quote_2110_1.png"}

    def test_empty_dirty_set_noop(self, sandbox):
        sandbox.write_images({"2110": 2})
        assert corpus_edit.wipe_buckets(set(), dry_run=False) == []
        assert len(list(sandbox.images.glob("quote_*.png"))) == 2


# ── version bump ──────────────────────────────────────────────────────


class TestVersion:
    def test_next_version_increments(self):
        assert corpus_edit.next_version("v1") == "v2"
        assert corpus_edit.next_version("v42") == "v43"

    def test_next_version_rejects_garbage(self):
        with pytest.raises(SystemExit):
            corpus_edit.next_version("latest")

    def test_read_version_trims_newline(self, sandbox):
        assert corpus_edit.read_version() == "v1"

    def test_write_version(self, sandbox):
        corpus_edit.write_version("v7", dry_run=False)
        assert sandbox.version.read_text().strip() == "v7"

    def test_write_version_dry_run_noop(self, sandbox):
        corpus_edit.write_version("v7", dry_run=True)
        assert sandbox.version.read_text().strip() == "v1"


# ── ship orchestration (mocked subprocess + git) ──────────────────────


@pytest.fixture
def shipping(sandbox, monkeypatch):
    """Layer subprocess + git diff mocking on top of the file sandbox."""
    sandbox.write_corpus(HEAD_ROWS)
    # Work tree: retag row 0 from 21:10 to 22:10, then re-sort by bucket so
    # the 22:10 rows stay contiguous (post-#299/E this is required — the
    # ship-time validator rejects non-contiguous buckets).
    work_rows = [r[:] for r in HEAD_ROWS]
    work_rows[0][0] = "22:10"
    work_rows.sort(key=lambda r: r[0])
    sandbox.write_corpus(work_rows)
    sandbox.write_images({"2110": 3, "2210": 1, "0800": 1})
    # Pre-existing manifest from the "last release": generator_hash matches the stub
    # php, so a pure CSV edit stays on the CSV path (renderer unchanged, not a
    # generator ship). corpus_hash is intentionally stale — the regen rewrites it.
    sandbox.write_manifest({"corpus_hash": "seed", "generator_hash": corpus_edit.generator_file_hash(), "files": {}})

    calls: list[list[str]] = []

    def fake_run(cmd, *, cwd=None, check=True, capture=False):
        calls.append(list(cmd))
        if cmd and cmd[0] == "php":
            # Mirror real PHP: (re)write a consistent manifest so _run_generator's
            # post-regen corpus_hash / generator_hash checks pass.
            sandbox.write_manifest(
                {
                    "corpus_hash": corpus_edit.corpus_file_hash(),
                    "generator_hash": corpus_edit.generator_file_hash(),
                    "files": {},
                }
            )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        stdout = ""
        if cmd[:3] == ["git", "show", f"HEAD:{corpus_edit.CORPUS_REL}"]:
            stdout = _csv(HEAD_ROWS)
        elif cmd[:4] == ["git", "diff", "--name-only", "HEAD"]:
            stdout = f"{corpus_edit.CORPUS_REL}\n"  # uncommitted CSV edit
        elif cmd[:3] == ["git", "diff", "--name-only"] and cmd[3:4] == ["master...HEAD"]:
            stdout = ""  # nothing committed vs base (CSV-edit-on-master flow)
        elif cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            stdout = "master\n"
        elif cmd[:3] == ["git", "rev-parse", "--short"]:
            stdout = "abc1234\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(corpus_edit, "run", fake_run)
    return sandbox, calls


class TestShip:
    @pytest.fixture(autouse=True)
    def _stub_fs_floor(self, monkeypatch):
        # These tests exercise ship-pipeline mechanics, not the fs gate
        # (which has its own tests and needs freetype-py). Stubbing keeps
        # them green on freetype-less interpreters — the documented
        # pre-commit pytest command runs under system python3 (review F1).
        # **kwargs so the stub survives signature growth (litclock-dev#559 added
        # head_rows); a positional stub silently breaks every ship test.
        monkeypatch.setattr(corpus_edit, "validate_fs_floor", lambda rows, **kwargs: ([], []))

    def test_ship_dry_run_has_no_side_effects(self, shipping):
        sandbox, calls = shipping
        before_images = {p.name for p in sandbox.images.glob("quote_*.png")}
        before_version = sandbox.version.read_text()
        args = _ship_args(message="fix(corpus): retag", dry_run=True)
        assert corpus_edit.cmd_ship(args) == 0
        assert {p.name for p in sandbox.images.glob("quote_*.png")} == before_images
        assert sandbox.version.read_text() == before_version
        # No checkout, commit, release, push, or PR creation.
        assert not any(c[:2] == ["git", "checkout"] for c in calls)
        assert not any(c[:2] == ["git", "commit"] for c in calls)
        assert not any(c[0].endswith("release_images.sh") for c in calls)
        assert not any(c[:2] == ["git", "push"] for c in calls)
        assert not any(c[0] == "gh" for c in calls)

    def test_ship_real_run_executes_steps_in_order(self, shipping):
        sandbox, calls = shipping
        args = _ship_args(message="fix(corpus): retag 10.10pm to 22:10")
        assert corpus_edit.cmd_ship(args) == 0
        # Version bumped.
        assert sandbox.version.read_text().strip() == "v2"
        # Dirty buckets wiped (21:10 and 22:10). 08:00 survives.
        assert not list(sandbox.images.glob("quote_2110_*.png"))
        assert not list(sandbox.images.glob("quote_2210_*.png"))
        assert list(sandbox.images.glob("quote_0800_*.png"))
        # Call order: checkout -> php -> git add -> git commit -> release -> push -> gh pr create.
        order = [_label(c) for c in calls]
        i_checkout = order.index("git checkout")
        i_php = order.index("php generate")
        i_add = order.index("git add")
        i_commit = order.index("git commit")
        i_release = order.index("release_images.sh")
        i_push = order.index("git push")
        i_pr = order.index("gh pr create")
        assert i_checkout < i_php < i_add < i_commit < i_release < i_push < i_pr

    def test_ship_refuses_if_unrelated_files_changed(self, shipping, monkeypatch):
        sandbox, _ = shipping

        def fake_run(cmd, *, cwd=None, check=True, capture=False):
            if cmd[:3] == ["git", "diff", "--name-only"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{corpus_edit.CORPUS_REL}\nREADME.md\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(corpus_edit, "run", fake_run)
        args = _ship_args(message="fix(corpus): retag")
        assert corpus_edit.cmd_ship(args) == 2

    def test_ship_refuses_if_validation_fails(self, sandbox, monkeypatch):
        sandbox.write_corpus(HEAD_ROWS)
        bad_rows = [r[:] for r in HEAD_ROWS]
        bad_rows[0] = ["21:10", "10.10pm.", "10.10pm. a quote", "BJD", "HF", "NO"]  # mistag
        sandbox.write_corpus(bad_rows)

        def fake_run(cmd, *, cwd=None, check=True, capture=False):
            stdout = ""
            if cmd[:3] == ["git", "show", f"HEAD:{corpus_edit.CORPUS_REL}"]:
                # HEAD has a legitimate 21:10 row for this slot.
                head_rows = [r[:] for r in HEAD_ROWS]
                head_rows[0] = ["21:10", "9:10 p.m.", "9:10 p.m. something", "BJD", "HF", "NO"]
                stdout = _csv(head_rows)
            elif cmd[:3] == ["git", "diff", "--name-only"]:
                stdout = f"{corpus_edit.CORPUS_REL}\n"
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

        monkeypatch.setattr(corpus_edit, "run", fake_run)
        args = _ship_args(message="fix(corpus): retag")
        assert corpus_edit.cmd_ship(args) == 2

    def test_ship_refuses_if_no_csv_changes(self, sandbox, monkeypatch):
        sandbox.write_corpus(HEAD_ROWS)

        def fake_run(cmd, *, cwd=None, check=True, capture=False):
            if cmd[:3] == ["git", "diff", "--name-only"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(corpus_edit, "run", fake_run)
        args = _ship_args(message="fix(corpus): retag")
        assert corpus_edit.cmd_ship(args) == 2

    def test_ship_refuses_if_not_on_master_or_derived_branch(self, shipping, monkeypatch):
        """Covers the branch-check error path (line 315-324)."""
        sandbox, calls = shipping

        def fake_run(cmd, *, cwd=None, check=True, capture=False):
            calls.append(list(cmd))
            stdout = ""
            if cmd[:3] == ["git", "show", f"HEAD:{corpus_edit.CORPUS_REL}"]:
                stdout = _csv(HEAD_ROWS)
            elif cmd[:3] == ["git", "diff", "--name-only"]:
                stdout = f"{corpus_edit.CORPUS_REL}\n"
            elif cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
                stdout = "some-other-branch\n"
            elif cmd[:3] == ["git", "rev-parse", "--short"]:
                stdout = "abc1234\n"
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

        monkeypatch.setattr(corpus_edit, "run", fake_run)
        args = _ship_args(message="fix(corpus): retag")
        assert corpus_edit.cmd_ship(args) == 2
        assert not any(c[:2] == ["git", "commit"] for c in calls)

    def test_ship_refuses_if_images_dir_missing(self, shipping, monkeypatch):
        sandbox, _ = shipping
        # Remove the sandbox's images dir to simulate a dev machine without download_images.sh run.
        import shutil

        shutil.rmtree(sandbox.images)
        args = _ship_args(message="fix(corpus): retag")
        assert corpus_edit.cmd_ship(args) == 2

    def test_ship_skip_validate_bypasses_bad_timestring(self, sandbox, monkeypatch):
        sandbox.write_corpus(HEAD_ROWS)
        bad_rows = [r[:] for r in HEAD_ROWS]
        bad_rows[0] = ["21:10", "10.10pm.", "10.10pm. a quote", "BJD", "HF", "NO"]
        sandbox.write_corpus(bad_rows)
        sandbox.write_images({"2110": 3, "2210": 1})
        sandbox.write_manifest(
            {"corpus_hash": "seed", "generator_hash": corpus_edit.generator_file_hash(), "files": {}}
        )
        calls: list[list[str]] = []

        def fake_run(cmd, *, cwd=None, check=True, capture=False):
            calls.append(list(cmd))
            if cmd and cmd[0] == "php":
                sandbox.write_manifest(
                    {
                        "corpus_hash": corpus_edit.corpus_file_hash(),
                        "generator_hash": corpus_edit.generator_file_hash(),
                        "files": {},
                    }
                )
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            stdout = ""
            if cmd[:3] == ["git", "show", f"HEAD:{corpus_edit.CORPUS_REL}"]:
                clean_head = [r[:] for r in HEAD_ROWS]
                clean_head[0] = ["21:10", "9:10 p.m.", "9:10 p.m. thing", "BJD", "HF", "NO"]
                stdout = _csv(clean_head)
            elif cmd[:4] == ["git", "diff", "--name-only", "HEAD"]:
                stdout = f"{corpus_edit.CORPUS_REL}\n"
            elif cmd[:3] == ["git", "diff", "--name-only"] and cmd[3:4] == ["master...HEAD"]:
                stdout = ""
            elif cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
                stdout = "master\n"
            elif cmd[:3] == ["git", "rev-parse", "--short"]:
                stdout = "abc1234\n"
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

        monkeypatch.setattr(corpus_edit, "run", fake_run)
        args = _ship_args(message="fix(corpus): override", skip_validate=True)
        assert corpus_edit.cmd_ship(args) == 0
        # Still reached the commit step despite the validator-failing row.
        assert any(c[:2] == ["git", "commit"] for c in calls)

    def test_ship_prints_recovery_on_post_commit_failure(self, shipping, monkeypatch, capsys):
        sandbox, _ = shipping
        original_run = corpus_edit.run

        def fake_run(cmd, *, cwd=None, check=True, capture=False):
            # Let everything up to release succeed, then make release fail.
            if cmd and str(cmd[0]).endswith("release_images.sh"):
                raise subprocess.CalledProcessError(1, cmd, "boom")
            return original_run(cmd, cwd=cwd, check=check, capture=capture)

        monkeypatch.setattr(corpus_edit, "run", fake_run)
        args = _ship_args(message="fix(corpus): retag")
        with pytest.raises(subprocess.CalledProcessError):
            corpus_edit.cmd_ship(args)
        err = capsys.readouterr().err
        assert "To finish manually" in err
        assert "release_images.sh" in err

    def test_read_head_corpus_returns_empty_when_file_not_in_head(self, monkeypatch):
        """Covers the CalledProcessError branch in read_head_corpus."""

        def fake_run(cmd, *, cwd=None, check=True, capture=False):
            raise subprocess.CalledProcessError(128, cmd, "", "fatal: path not in HEAD")

        monkeypatch.setattr(corpus_edit, "run", fake_run)
        assert corpus_edit.read_head_corpus() == ""


# ── generator-awareness (litclock-dev#502) ────────────────────────────────────────


def _cp(cmd, code=0, out="", err=""):
    return subprocess.CompletedProcess(cmd, code, stdout=out, stderr=err)


class TestGeneratorOutOfSync:
    def test_true_when_manifest_missing(self, sandbox):
        assert corpus_edit._generator_out_of_sync() is True

    def test_true_when_generator_hash_differs(self, sandbox):
        sandbox.write_manifest({"generator_hash": "0" * 40})
        assert corpus_edit._generator_out_of_sync() is True

    def test_false_when_generator_hash_matches(self, sandbox):
        sandbox.write_manifest({"generator_hash": corpus_edit.generator_file_hash()})
        assert corpus_edit._generator_out_of_sync() is False

    def test_true_when_manifest_malformed(self, sandbox):
        sandbox.manifest.write_text("{not json")
        assert corpus_edit._generator_out_of_sync() is True


class TestChangedVsBase:
    def test_true_when_committed_diff_nonempty(self, sandbox, monkeypatch):
        def fake_run(cmd, *, cwd=None, check=True, capture=False):
            if cmd[:2] == ["git", "rev-parse"] and "--verify" in cmd:
                return _cp(cmd, 0, "deadbeef\n")
            if cmd[:3] == ["git", "diff", "--name-only"]:
                return _cp(cmd, 0, f"{corpus_edit.PHP_GENERATOR_REL}\n")
            return _cp(cmd)

        monkeypatch.setattr(corpus_edit, "run", fake_run)
        assert corpus_edit._changed_vs_base(corpus_edit.PHP_GENERATOR_REL) is True

    def test_false_when_committed_diff_empty(self, sandbox, monkeypatch):
        def fake_run(cmd, *, cwd=None, check=True, capture=False):
            if cmd[:2] == ["git", "rev-parse"] and "--verify" in cmd:
                return _cp(cmd, 0, "deadbeef\n")
            return _cp(cmd, 0, "")

        monkeypatch.setattr(corpus_edit, "run", fake_run)
        assert corpus_edit._changed_vs_base(corpus_edit.PHP_GENERATOR_REL) is False

    def test_false_and_warns_when_base_ref_missing(self, sandbox, monkeypatch, capsys):
        def fake_run(cmd, *, cwd=None, check=True, capture=False):
            if cmd[:2] == ["git", "rev-parse"] and "--verify" in cmd:
                return _cp(cmd, 1, "")  # base ref not found
            raise AssertionError("must not run git diff when base is missing")

        monkeypatch.setattr(corpus_edit, "run", fake_run)
        assert corpus_edit._changed_vs_base(corpus_edit.PHP_GENERATOR_REL) is False
        assert "not found" in capsys.readouterr().err


class TestPrepareForRegen:
    def test_generator_drift_moves_manifest_aside(self, sandbox):
        sandbox.write_manifest({"generator_hash": "x"})
        corpus_edit._prepare_for_regen([], False, True, dry_run=False)
        assert not corpus_edit.MANIFEST_PATH.exists()
        assert corpus_edit.MANIFEST_BAK.exists()

    def test_generator_drift_dry_run_keeps_manifest(self, sandbox):
        sandbox.write_manifest({"generator_hash": "x"})
        corpus_edit._prepare_for_regen([], False, True, dry_run=True)
        assert corpus_edit.MANIFEST_PATH.exists()
        assert not corpus_edit.MANIFEST_BAK.exists()

    def test_generator_dominates_dirty(self, sandbox):
        # R1 + litclock-dev#503 review P1: a renderer change forces a full regen (manifest aside)
        # AND still wipes the dirty buckets — that clears orphaned PNGs from
        # removed/renumbered rows, which a content-hash full regen would otherwise
        # leave for the release tarball to ship.
        sandbox.write_images({"2110": 2})
        sandbox.write_manifest({"generator_hash": "x"})
        corpus_edit._prepare_for_regen(["2110"], False, True, dry_run=False)
        assert corpus_edit.MANIFEST_BAK.exists()  # manifest moved aside for full regen
        assert not list(sandbox.images.glob("quote_2110_*.png"))  # orphans cleared

    def test_dirty_only_wipes_buckets(self, sandbox):
        sandbox.write_images({"2110": 2})
        corpus_edit._prepare_for_regen(["2110"], False, False, dry_run=False)
        assert not list(sandbox.images.glob("quote_2110_*.png"))


class TestRunGenerator:
    def test_restores_backup_on_php_failure(self, sandbox, monkeypatch):
        sandbox.write_manifest({"generator_hash": corpus_edit.generator_file_hash()})
        corpus_edit._remove_manifest_for_regen(dry_run=False)  # -> .bak
        assert not corpus_edit.MANIFEST_PATH.exists()

        def fake_run(cmd, *, cwd=None, check=True, capture=False):
            raise subprocess.CalledProcessError(1, cmd, "boom")

        monkeypatch.setattr(corpus_edit, "run", fake_run)
        with pytest.raises(subprocess.CalledProcessError):
            corpus_edit._run_generator()
        assert corpus_edit.MANIFEST_PATH.exists()  # rolled back
        assert not corpus_edit.MANIFEST_BAK.exists()

    def test_restores_backup_when_php_writes_no_manifest(self, sandbox, monkeypatch):
        sandbox.write_manifest({"generator_hash": corpus_edit.generator_file_hash()})
        corpus_edit._remove_manifest_for_regen(dry_run=False)

        def fake_run(cmd, *, cwd=None, check=True, capture=False):
            return _cp(cmd)  # php "succeeds" but writes nothing

        monkeypatch.setattr(corpus_edit, "run", fake_run)
        with pytest.raises(SystemExit):
            corpus_edit._run_generator()
        assert corpus_edit.MANIFEST_PATH.exists()  # rolled back


class TestRegenerateGeneratorDrift:
    def test_regenerate_forces_full_regen_on_generator_change(self, sandbox, monkeypatch):
        sandbox.write_corpus(HEAD_ROWS)
        # Manifest matches the CSV but records a STALE generator_hash → pure renderer drift.
        sandbox.write_manifest({"corpus_hash": corpus_edit.corpus_file_hash(), "generator_hash": "0" * 40, "files": {}})
        calls: list[list[str]] = []

        def fake_run(cmd, *, cwd=None, check=True, capture=False):
            calls.append(list(cmd))
            if cmd[:3] == ["git", "show", f"HEAD:{corpus_edit.CORPUS_REL}"]:
                return _cp(cmd, 0, _csv(HEAD_ROWS))  # no CSV dirty
            if cmd and cmd[0] == "php":
                sandbox.write_manifest(
                    {
                        "corpus_hash": corpus_edit.corpus_file_hash(),
                        "generator_hash": corpus_edit.generator_file_hash(),
                        "files": {},
                    }
                )
                return _cp(cmd)
            return _cp(cmd)

        monkeypatch.setattr(corpus_edit, "run", fake_run)
        args = argparse.Namespace(dry_run=False)
        assert corpus_edit.cmd_regenerate(args) == 0
        assert any(c and c[0] == "php" for c in calls)
        assert not corpus_edit.MANIFEST_BAK.exists()  # discarded after success


class TestGeneratorShip:
    def _fake_run(
        self,
        sandbox,
        calls,
        *,
        branch="fix/renderer",
        renderer_changed=True,
        release_fail=False,
        version_bumped=False,
        csv_committed=False,
    ):
        def fake_run(cmd, *, cwd=None, check=True, capture=False):
            calls.append(list(cmd))
            if cmd[:4] == ["git", "diff", "--name-only", "HEAD"]:
                return _cp(cmd, 0, "")  # clean working tree (no uncommitted CSV)
            if cmd[:2] == ["git", "rev-parse"] and "--verify" in cmd:
                return _cp(cmd, 0, "deadbeef\n")  # base ref exists
            if cmd[:3] == ["git", "diff", "--name-only"] and len(cmd) > 3 and cmd[3] == "master...HEAD":
                # Scope the committed-vs-base diff to the queried path (cmd[5] after `--`).
                path = cmd[5] if len(cmd) > 5 else ""
                changed = (
                    (path == corpus_edit.PHP_GENERATOR_REL and renderer_changed)
                    or (path == corpus_edit.IMAGES_VERSION_REL and version_bumped)
                    or (path == corpus_edit.CORPUS_REL and csv_committed)
                )
                return _cp(cmd, 0, f"{path}\n" if changed else "")
            if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
                return _cp(cmd, 0, f"{branch}\n")
            if cmd and cmd[0] == "php":
                sandbox.write_manifest(
                    {
                        "corpus_hash": corpus_edit.corpus_file_hash(),
                        "generator_hash": corpus_edit.generator_file_hash(),
                        "files": {},
                    }
                )
                return _cp(cmd)
            if release_fail and str(cmd[0]).endswith("release_images.sh"):
                raise subprocess.CalledProcessError(1, cmd, "boom")
            return _cp(cmd)

        return fake_run

    def test_generator_ship_runs_in_place_and_commits_only_version(self, sandbox, monkeypatch):
        sandbox.write_corpus(HEAD_ROWS)
        sandbox.write_images({"2110": 1})
        calls: list[list[str]] = []
        monkeypatch.setattr(corpus_edit, "run", self._fake_run(sandbox, calls))
        args = _ship_args(message="fix(images): renderer")
        assert corpus_edit.cmd_ship(args) == 0
        assert sandbox.version.read_text().strip() == "v2"
        add_calls = [c for c in calls if c[:2] == ["git", "add"]]
        assert add_calls == [["git", "add", ".images-version"]]  # NOT the CSV
        assert not any(c[:2] == ["git", "checkout"] for c in calls)  # in place

    def test_generator_ship_rejected_on_master(self, sandbox, monkeypatch):
        sandbox.write_corpus(HEAD_ROWS)
        calls: list[list[str]] = []
        monkeypatch.setattr(corpus_edit, "run", self._fake_run(sandbox, calls, branch="master"))
        assert corpus_edit.cmd_ship(_ship_args(message="fix(images): renderer")) == 2
        assert not any(c[:2] == ["git", "commit"] for c in calls)

    def test_generator_ship_rejected_on_detached_head(self, sandbox, monkeypatch):
        sandbox.write_corpus(HEAD_ROWS)
        calls: list[list[str]] = []
        monkeypatch.setattr(corpus_edit, "run", self._fake_run(sandbox, calls, branch="HEAD"))
        assert corpus_edit.cmd_ship(_ship_args(message="fix(images): renderer")) == 2

    def test_generator_ship_rejected_when_version_already_bumped(self, sandbox, monkeypatch):
        # Rerun-safety (litclock-dev#503 review P1): .images-version already bumped vs base means a
        # prior ship cut the release — re-running must NOT double-bump.
        sandbox.write_corpus(HEAD_ROWS)
        calls: list[list[str]] = []
        monkeypatch.setattr(corpus_edit, "run", self._fake_run(sandbox, calls, version_bumped=True))
        assert corpus_edit.cmd_ship(_ship_args(message="fix(images): renderer")) == 2
        assert not any(c[:2] == ["git", "commit"] for c in calls)
        assert sandbox.version.read_text().strip() == "v1"  # not bumped again

    def test_generator_ship_rejected_on_mixed_committed_csv(self, sandbox, monkeypatch):
        # A branch with BOTH a committed renderer change and a committed CSV edit is
        # not supported by the in-place generator flow (litclock-dev#503 review P1) — fail loud
        # rather than skip CSV validation + orphan clearing.
        sandbox.write_corpus(HEAD_ROWS)
        calls: list[list[str]] = []
        monkeypatch.setattr(corpus_edit, "run", self._fake_run(sandbox, calls, csv_committed=True))
        assert corpus_edit.cmd_ship(_ship_args(message="fix(images): renderer")) == 2
        assert not any(c[:2] == ["git", "commit"] for c in calls)
        assert not any(c[:2] == ["git", "commit"] for c in calls)

    def test_nothing_to_ship_when_neither_csv_nor_renderer_changed(self, sandbox, monkeypatch):
        sandbox.write_corpus(HEAD_ROWS)
        calls: list[list[str]] = []
        monkeypatch.setattr(corpus_edit, "run", self._fake_run(sandbox, calls, renderer_changed=False))
        assert corpus_edit.cmd_ship(_ship_args(message="noop")) == 2

    def test_generator_ship_dry_run_has_no_side_effects(self, sandbox, monkeypatch):
        sandbox.write_corpus(HEAD_ROWS)
        before = sandbox.version.read_text()
        calls: list[list[str]] = []
        monkeypatch.setattr(corpus_edit, "run", self._fake_run(sandbox, calls))
        assert corpus_edit.cmd_ship(_ship_args(message="fix(images): renderer", dry_run=True)) == 0
        assert sandbox.version.read_text() == before
        assert not any(c[:2] == ["git", "commit"] for c in calls)
        assert not any(c and c[0] == "php" for c in calls)

    def test_generator_ship_step_aware_recovery_skips_release_line(self, sandbox, monkeypatch, capsys):
        # Release SUCCEEDS, push fails → recovery must NOT tell the user to re-cut the tag.
        sandbox.write_corpus(HEAD_ROWS)
        calls: list[list[str]] = []
        base = self._fake_run(sandbox, calls)

        def fake_run(cmd, *, cwd=None, check=True, capture=False):
            if cmd[:2] == ["git", "push"]:
                raise subprocess.CalledProcessError(1, cmd, "push boom")
            return base(cmd, cwd=cwd, check=check, capture=capture)

        monkeypatch.setattr(corpus_edit, "run", fake_run)
        with pytest.raises(subprocess.CalledProcessError):
            corpus_edit.cmd_ship(_ship_args(message="fix(images): renderer"))
        err = capsys.readouterr().err
        assert "To finish manually" in err
        assert "release_images.sh" not in err  # already cut
        assert "git push" in err


# ── helpers ───────────────────────────────────────────────────────────


def _ship_args(
    *,
    message: str,
    dry_run: bool = False,
    no_release: bool = False,
    no_push: bool = False,
    branch: str | None = None,
    skip_validate: bool = False,
):
    import argparse

    return argparse.Namespace(
        message=message,
        dry_run=dry_run,
        no_release=no_release,
        no_push=no_push,
        branch=branch,
        skip_validate=skip_validate,
        cmd="ship",
    )


def _label(cmd: list[str]) -> str:
    if cmd[:2] == ["git", "checkout"]:
        return "git checkout"
    if cmd[:2] == ["git", "add"]:
        return "git add"
    if cmd[:2] == ["git", "commit"]:
        return "git commit"
    if cmd[:2] == ["git", "push"]:
        return "git push"
    if cmd[:2] == ["git", "show"]:
        return "git show"
    if cmd[:2] == ["git", "diff"]:
        return "git diff"
    if cmd[:2] == ["git", "rev-parse"]:
        return "git rev-parse"
    if cmd[0] == "gh" and cmd[1:3] == ["pr", "create"]:
        return "gh pr create"
    if cmd[0] == "php":
        return "php generate"
    if cmd[0].endswith("release_images.sh"):
        return "release_images.sh"
    return " ".join(cmd[:2])


# ── manifest helpers (litclock-dev#299) ───────────────────────────────────────────


class TestImageContentHash:
    def test_matches_php_json_encoded_tuple(self):
        # Pre-computed via:
        #   php -r 'echo sha1(json_encode(["q","t","a","ts"],
        #     JSON_UNESCAPED_SLASHES|JSON_UNESCAPED_UNICODE));'
        # Must match for the manifest produced by quote_to_image.php to be readable here.
        assert corpus_edit.image_content_hash("q", "t", "a", "ts") == "493b0912a4e67ec285c2e6369a230ebff613334f"

    def test_handles_utf8(self):
        # café (with U+00E9) must hash identically to PHP. Verified via:
        #   php -r 'echo sha1(json_encode(["café","titlé","aut","ts"],
        #     JSON_UNESCAPED_SLASHES|JSON_UNESCAPED_UNICODE));'
        assert (
            corpus_edit.image_content_hash("café", "titlé", "aut", "ts") == "3d180ee586de2408dbec3a71bb75eee230ad595e"
        )

    def test_pipe_in_field_is_unambiguous(self):
        # litclock-dev#299/F: pipe-in-field used to collide under the old `|`-joined
        # preimage. With JSON-array encoding these two MUST hash differently.
        a = corpus_edit.image_content_hash("a|b", "c", "d", "e")
        b = corpus_edit.image_content_hash("a", "b|c", "d", "e")
        assert a != b

    def test_distinct_inputs_distinct_hashes(self):
        a = corpus_edit.image_content_hash("q1", "t", "a", "ts")
        b = corpus_edit.image_content_hash("q2", "t", "a", "ts")
        assert a != b


class TestCorpusFileHash:
    def test_hashes_csv_file_bytes(self, sandbox):
        sandbox.write_corpus(HEAD_ROWS)
        expected = hashlib.sha1(sandbox.corpus.read_bytes()).hexdigest()
        assert corpus_edit.corpus_file_hash() == expected

    def test_explicit_text_argument(self):
        # When passed CSV text directly, hashes its UTF-8 bytes.
        assert corpus_edit.corpus_file_hash("hello") == hashlib.sha1(b"hello").hexdigest()


class TestReadManifest:
    def test_returns_none_when_missing(self, sandbox):
        assert corpus_edit.read_manifest() is None

    def test_parses_valid_manifest(self, sandbox):
        payload = {"corpus_hash": "abc", "files": {"quote_2110_0.png": "deadbeef"}}
        sandbox.write_manifest(payload)
        assert corpus_edit.read_manifest() == payload

    def test_returns_none_on_invalid_json(self, sandbox):
        sandbox.manifest.write_text("{not json")
        assert corpus_edit.read_manifest() is None


class TestPerRowFilenames:
    def test_counter_resets_per_bucket(self):
        rows = corpus_edit.parse_corpus(_csv(HEAD_ROWS))
        names = [fn for _, fn in corpus_edit.per_row_filenames(rows)]
        # HEAD_ROWS: 3x21:10, 1x22:10, 1x08:00
        assert names == [
            "quote_2110_0.png",
            "quote_2110_1.png",
            "quote_2110_2.png",
            "quote_2210_0.png",
            "quote_0800_0.png",
        ]

    def test_nsfw_suffix_applied(self):
        rows = corpus_edit.parse_corpus(
            _csv([["00:00", "midnight", "Q", "T", "A", "YES"], ["00:00", "midnight", "Q2", "T", "A", "NO"]])
        )
        names = [fn for _, fn in corpus_edit.per_row_filenames(rows)]
        assert names == ["quote_0000_0_nsfw.png", "quote_0000_1.png"]

    def test_empty_rows_yields_empty(self):
        assert corpus_edit.per_row_filenames([]) == []


class TestManifestMismatches:
    def test_clean_match(self):
        rows = corpus_edit.parse_corpus(_csv(HEAD_ROWS))
        files = {
            fn: corpus_edit.image_content_hash(r.quote, r.title, r.author, r.match)
            for r, fn in corpus_edit.per_row_filenames(rows)
        }
        assert corpus_edit.manifest_mismatches(rows, files) == []

    def test_stale_hash_surfaces(self):
        rows = corpus_edit.parse_corpus(_csv(HEAD_ROWS))
        # All filenames map to a wrong hash.
        files = {fn: "0" * 40 for _, fn in corpus_edit.per_row_filenames(rows)}
        out = corpus_edit.manifest_mismatches(rows, files)
        assert len(out) == len(rows)
        for filename, manifest_hash, expected in out:
            assert manifest_hash == "0" * 40
            assert expected != "0" * 40
            assert filename.startswith("quote_")

    def test_missing_entry_treated_as_mismatch(self):
        rows = corpus_edit.parse_corpus(_csv(HEAD_ROWS))
        out = corpus_edit.manifest_mismatches(rows, {})
        assert len(out) == len(rows)
        assert all(mh is None for _, mh, _ in out)


class TestDiffWithManifest:
    def test_diff_no_manifest_prints_missing_marker(self, sandbox, capsys, monkeypatch):
        sandbox.write_corpus(HEAD_ROWS)

        def fake_run(cmd, *, cwd=None, check=True, capture=False):
            if cmd[:3] == ["git", "show", f"HEAD:{corpus_edit.CORPUS_REL}"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=_csv(HEAD_ROWS), stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(corpus_edit, "run", fake_run)
        import argparse

        assert corpus_edit.cmd_diff(argparse.Namespace()) == 0
        out = capsys.readouterr().out
        assert "manifest.json missing" in out

    def test_diff_manifest_corpus_hash_mismatch_reported(self, sandbox, capsys, monkeypatch):
        sandbox.write_corpus(HEAD_ROWS)
        sandbox.write_manifest({"corpus_hash": "deadbeef" * 5, "files": {}})

        def fake_run(cmd, *, cwd=None, check=True, capture=False):
            if cmd[:3] == ["git", "show", f"HEAD:{corpus_edit.CORPUS_REL}"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=_csv(HEAD_ROWS), stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(corpus_edit, "run", fake_run)
        import argparse

        assert corpus_edit.cmd_diff(argparse.Namespace()) == 0
        out = capsys.readouterr().out
        assert "MISMATCH" in out
        assert "deadbeef" in out

    def test_diff_manifest_corpus_hash_matches_no_mismatch_section(self, sandbox, capsys, monkeypatch):
        sandbox.write_corpus(HEAD_ROWS)
        rows = corpus_edit.parse_corpus(_csv(HEAD_ROWS))
        files = {
            fn: corpus_edit.image_content_hash(r.quote, r.title, r.author, r.match)
            for r, fn in corpus_edit.per_row_filenames(rows)
        }
        sandbox.write_manifest({"corpus_hash": corpus_edit.corpus_file_hash(), "files": files})

        def fake_run(cmd, *, cwd=None, check=True, capture=False):
            if cmd[:3] == ["git", "show", f"HEAD:{corpus_edit.CORPUS_REL}"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=_csv(HEAD_ROWS), stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(corpus_edit, "run", fake_run)
        import argparse

        assert corpus_edit.cmd_diff(argparse.Namespace()) == 0
        out = capsys.readouterr().out
        assert "matches current CSV" in out
        assert "MISMATCH" not in out
        assert "per-row content-hash mismatch" not in out


# ── fitted-fs floor validator (litclock-dev#539) ───────────────────────────────

try:
    import freetype  # noqa: F401

    _HAVE_FREETYPE = True
except ImportError:
    _HAVE_FREETYPE = False

needs_freetype = pytest.mark.skipif(not _HAVE_FREETYPE, reason="freetype-py required to measure fitted size")


def _row(quote: str, match: str, time: str = "12:00") -> corpus_edit.Row:
    return corpus_edit.Row(time=time, match=match, quote=quote, title="T", author="A", is_nsfw=False)


@needs_freetype
class TestFsFloor:
    SHORT = _row("It was noon and the bells rang out over the quiet town.", "noon")
    # Long enough to fit only below the hard floor; filler deliberately
    # avoids any substring of the match so find_timestring can't hit early.
    LONG = _row(
        (
            "The keeper counted his coins and muttered about the heat while dust settled over the crowded "
            "shelves of the shop, and outside the carts rolled by one after another, their drivers calling "
            "greetings to the women at the well, who answered without looking up from their buckets. " * 2
        )
        + "It was noon.",
        "noon",
    )

    def test_short_row_passes_clean(self):
        errors, warns = corpus_edit.validate_fs_floor([self.SHORT])
        assert errors == []
        assert warns == []

    def test_absent_timestring_is_error(self):
        errors, warns = corpus_edit.validate_fs_floor([_row("No time in this quote.", "midnight")])
        assert len(errors) == 1
        assert "won't render" in errors[0]

    def test_long_row_fails_hard_floor(self):
        errors, warns = corpus_edit.validate_fs_floor([self.LONG])
        assert len(errors) == 1
        assert "hard floor" in errors[0]

    def test_acknowledging_the_token_demotes_to_warn(self):
        token = corpus_edit.violation_token(self.LONG)
        errors, warns = corpus_edit.validate_fs_floor([self.LONG], acknowledged=[token])
        assert errors == []
        assert len(warns) == 1
        assert "hard floor" in warns[0]


@needs_freetype
class TestFsFloorAcknowledgement:
    """litclock-dev#559 — the floor is absolute; exactly one relaxation is automatic,
    and everything else is reported for an explicit, content-bound accept.

    An earlier version of this change tried to INFER intent (pair a row with
    its pre-edit self, pass anything no worse). Cross-model review found it
    unfixably bypassable: deleting a sub-floor row and adding a different one
    at the same identity produces exactly the diff an edit produces. The tool
    now stops guessing and asks.
    """

    BEFORE = TestFsFloor.LONG  # 562 chars, fits fs20, below the hard floor of 22

    def _edited(self, **kw):
        d = dict(
            time=self.BEFORE.time,
            match=self.BEFORE.match,
            quote=self.BEFORE.quote,
            title=self.BEFORE.title,
            author=self.BEFORE.author,
            is_nsfw=False,
        )
        d.update(kw)
        return corpus_edit.Row(**d)

    @staticmethod
    def _as_csv(row: corpus_edit.Row) -> str:
        return "|".join([row.time, row.match, row.quote, row.title, row.author, "YES" if row.is_nsfw else "NO"]) + "\n"

    # ── the automatic relaxation: provably-safe metadata edits ───────

    def test_retag_of_a_sub_floor_row_passes_with_no_flag(self):
        """Retagging is the workflow this tool was built for (litclock-dev#211)."""
        errors, warns = corpus_edit.validate_fs_floor([self._edited(time="13:00")], head_rows=[self.BEFORE])
        assert not errors, f"a retag changes no rendered text; got {errors}"
        assert any("unchanged at HEAD" in w for w in warns), warns

    def test_title_and_author_fixes_pass_with_no_flag(self):
        for field in ("title", "author"):
            row = self._edited(**{field: "Corrected"})
            errors, _ = corpus_edit.validate_fs_floor([row], head_rows=[self.BEFORE])
            assert not errors, f"a {field} fix changes no rendered text; got {errors}"

    def test_changing_the_match_is_NOT_metadata_only(self):
        """(quote, match) is the render pair — the shortcut must key on BOTH."""
        assert "It was noon" in self.BEFORE.quote
        errors, _ = corpus_edit.validate_fs_floor([self._edited(match="It was noon")], head_rows=[self.BEFORE])
        assert errors, "a changed match moves the bold span; it is not metadata-only"

    # ── everything else is reported, then explicitly accepted ────────

    def test_sub_floor_row_errors_and_names_its_token(self):
        errors, _ = corpus_edit.validate_fs_floor([self.BEFORE], head_rows=[])
        assert errors
        assert corpus_edit.violation_token(self.BEFORE) in errors[0], errors

    def test_acknowledging_the_token_lets_it_through(self):
        token = corpus_edit.violation_token(self.BEFORE)
        errors, warns = corpus_edit.validate_fs_floor([self.BEFORE], acknowledged=[token], head_rows=[])
        assert not errors, errors
        assert any(token in w for w in warns), warns

    def test_token_is_content_bound_so_a_further_edit_invalidates_it(self):
        """The key property. Acknowledge a row, then change it again — the old
        approval must stop applying rather than silently carry over."""
        token = corpus_edit.violation_token(self.BEFORE)
        changed_again = self._edited(quote=self.BEFORE.quote.replace("muttered", "murmured", 1))
        assert corpus_edit.violation_token(changed_again) != token
        errors, _ = corpus_edit.validate_fs_floor([changed_again], acknowledged=[token], head_rows=[])
        assert errors, "a stale token must not authorise the edited row"

    def test_two_rows_at_the_same_minute_get_different_tokens(self):
        """A minute holds many rows, so the time alone cannot be the key."""
        a = self.BEFORE
        b = self._edited(quote=self.BEFORE.quote + " A second quote at the same minute.")
        assert a.time == b.time
        assert corpus_edit.violation_token(a) != corpus_edit.violation_token(b)

    def test_stale_acknowledgement_is_an_error_not_a_no_op(self):
        """A list pasted from an old PR body must not look like it still
        authorises something."""
        ghost = "12:00#deadbeef"
        errors, _ = corpus_edit.validate_fs_floor(
            [self._edited(time="13:00")], acknowledged=[ghost], head_rows=[self.BEFORE]
        )
        assert any("stale acknowledgement" in e and ghost in e for e in errors), errors

    def test_no_acknowledgement_can_excuse_a_row_that_will_not_render(self):
        broken = self._edited(match="nowhere-in-this-text")
        token = corpus_edit.violation_token(broken)
        errors, _ = corpus_edit.validate_fs_floor([broken], acknowledged=[token], head_rows=[])
        assert errors, "a non-rendering row must never be acceptable"
        assert any("won't render" in e for e in errors), errors

    # ── the wiring: both entry points must honour it ─────────────────

    def test_cmd_validate_rejects_then_accepts_with_the_token(self, sandbox, monkeypatch):
        monkeypatch.setattr(corpus_edit, "read_head_corpus", lambda: "")
        sandbox.corpus.write_text(self._as_csv(self.BEFORE))
        assert corpus_edit.cmd_validate(argparse.Namespace(allow_small=None)) == 2
        token = corpus_edit.violation_token(self.BEFORE)
        assert corpus_edit.cmd_validate(argparse.Namespace(allow_small=[token])) == 0

    def test_cmd_validate_passes_a_metadata_edit_with_no_flag(self, sandbox, monkeypatch):
        """End-to-end cover for the automatic relaxation.

        Uses a byline fix rather than a retag: this fixture's match is "noon",
        which time-tag validation (correctly, and BEFORE the fs gate) only
        accepts at 12:00, so it cannot be retagged. The retag path is covered
        by the direct-call test above.
        """
        head_csv = self._as_csv(self.BEFORE)
        monkeypatch.setattr(corpus_edit, "read_head_corpus", lambda: head_csv)
        sandbox.corpus.write_text(self._as_csv(self._edited(author="Corrected Author")))
        assert corpus_edit.cmd_validate(argparse.Namespace(allow_small=None)) == 0

    def test_bare_flag_no_longer_blankets(self, sandbox, monkeypatch):
        """`--allow-small` with no tokens used to excuse everything."""
        monkeypatch.setattr(corpus_edit, "read_head_corpus", lambda: "")
        sandbox.corpus.write_text(self._as_csv(self.BEFORE))
        assert corpus_edit.cmd_validate(argparse.Namespace(allow_small=[])) == 2

    def test_cmd_ship_honours_the_same_semantics(self, sandbox, monkeypatch):
        """A gate only in validate is bypassable by never running it."""
        monkeypatch.setattr(corpus_edit, "read_head_corpus", lambda: "")
        sandbox.corpus.write_text(self._as_csv(self.BEFORE))
        sandbox.write_manifest(
            {"corpus_hash": "stale", "generator_hash": corpus_edit.generator_file_hash(), "files": {}}
        )
        monkeypatch.setattr(
            corpus_edit,
            "run",
            lambda cmd, **kw: subprocess.CompletedProcess(
                cmd,
                0,
                stdout=(f"{corpus_edit.CORPUS_REL}\n" if cmd[:3] == ["git", "diff", "--name-only"] else ""),
                stderr="",
            ),
        )
        args = argparse.Namespace(
            message="m",
            dry_run=True,
            no_release=True,
            no_push=True,
            branch=None,
            allow_small=None,
            skip_validate=False,
        )
        assert corpus_edit.cmd_ship(args) == 2, "ship must reject an unacknowledged sub-floor row"

    def test_hint_prints_a_copy_pasteable_command(self, capsys):
        token = corpus_edit.violation_token(self.BEFORE)
        errors, _ = corpus_edit.validate_fs_floor([self.BEFORE], head_rows=[])
        corpus_edit._print_allow_small_hint(errors)
        captured = capsys.readouterr().err
        assert f"--allow-small {token}" in captured, captured

    def test_a_token_accepts_only_its_own_row(self):
        """Two sub-floor rows, one acknowledged. The other must still fail.

        test_a_token_for_a_different_row_does_not_help does NOT cover this: its
        token belongs to no changed row, so the stale-acknowledgement check
        fires and the test passes without the per-row match ever being
        exercised. Caught by mutation — replacing `token in ack` with a bare
        `ack` truthiness test left the whole suite green.
        """
        row_a = self.BEFORE
        row_b = self._edited(quote=self.BEFORE.quote + " A second long quote at this minute.")
        errors, warns = corpus_edit.validate_fs_floor(
            [row_a, row_b], acknowledged=[corpus_edit.violation_token(row_a)], head_rows=[]
        )
        assert not any("stale" in e for e in errors), f"neither token should be stale: {errors}"
        assert any(corpus_edit.violation_token(row_a) in w for w in warns), warns
        assert errors, "row_b was never acknowledged and must still fail"
        assert corpus_edit.violation_token(row_b) in errors[0], errors

    # ── the exemption needs both sides, not set membership ───────────
    #
    # 235 (quote, match) pairs in the live corpus are carried by more than one
    # row, so "this text exists somewhere at HEAD" is not evidence that THIS
    # row is an edit of it. Found in self-review: the first version exempted a
    # brand-new row that merely duplicated existing text, and the warning even
    # claimed "this edit cannot have moved the fit" when there was no edit.

    def test_new_row_duplicating_existing_text_is_still_gated(self):
        newcomer = self._edited(title="A Different Book", author="Someone Else")
        errors, warns = corpus_edit.validate_fs_floor(
            [newcomer], head_rows=[self.BEFORE], work_rows=[self.BEFORE, newcomer]
        )
        assert errors, "a NEW sub-floor row must be gated even if its text already exists"
        assert not any("unchanged at HEAD" in w for w in warns), warns

    def test_metadata_edit_still_exempt_when_the_copy_count_holds(self):
        """The counterpart — a byline fix keeps the count level and stays exempt."""
        fixed = self._edited(author="Corrected Author")
        errors, warns = corpus_edit.validate_fs_floor([fixed], head_rows=[self.BEFORE], work_rows=[fixed])
        assert not errors, errors
        assert any("unchanged at HEAD" in w for w in warns), warns

    def test_deleting_one_of_two_copies_stays_exempt(self):
        """Count falling is fine — nothing new entered the corpus."""
        twin = self._edited(title="Other Book")
        edited = self._edited(author="Corrected")
        errors, _ = corpus_edit.validate_fs_floor([edited], head_rows=[self.BEFORE, twin], work_rows=[edited])
        assert not errors, errors

    def test_cmd_validate_gates_a_new_duplicate_text_row(self, sandbox, monkeypatch):
        """End-to-end: the call sites must pass work_rows or this is inert."""
        newcomer = self._edited(title="A Different Book", author="Someone Else")
        head_csv = self._as_csv(self.BEFORE)
        monkeypatch.setattr(corpus_edit, "read_head_corpus", lambda: head_csv)
        sandbox.corpus.write_text(head_csv + self._as_csv(newcomer))
        assert corpus_edit.cmd_validate(argparse.Namespace(allow_small=None)) == 2

    def test_hint_converges_when_a_stale_token_is_also_present(self, capsys):
        """The suggested command must never re-include a token just rejected.

        Before this, the hint regex-scraped every error line including the
        stale-acknowledgement line, so it suggested the stale token back. An
        operator pasting it verbatim would be rejected again on every run —
        the remediation advice could not converge.
        """
        errors, _ = corpus_edit.validate_fs_floor(
            [self.BEFORE], acknowledged=["12:00#deadbeef"], head_rows=[], work_rows=[self.BEFORE]
        )
        assert any("stale" in e for e in errors), errors
        corpus_edit._print_allow_small_hint(errors)
        hint = capsys.readouterr().err
        assert "deadbeef" not in hint, f"hint re-suggests the stale token: {hint}"
        assert corpus_edit.violation_token(self.BEFORE) in hint, hint
        assert "do not re-pass" in hint, hint

    def test_hint_says_something_useful_when_only_stale_tokens_remain(self, capsys):
        """No real failures, just a leftover token — don't print an empty command."""
        errors, _ = corpus_edit.validate_fs_floor(
            [self._edited(author="Corrected")],
            acknowledged=["12:00#deadbeef"],
            head_rows=[self.BEFORE],
            work_rows=[self._edited(author="Corrected")],
        )
        corpus_edit._print_allow_small_hint(errors)
        hint = capsys.readouterr().err
        assert "no longer match a failing row" in hint, hint
        assert "--allow-small 12:00#deadbeef" not in hint, hint

    def test_token_for_an_exempted_row_is_unnecessary_not_stale(self):
        """A correct token for a row the metadata rule already exempts must not
        block the run. It reports as unnecessary, not as a stale acknowledgement."""
        fixed = self._edited(author="Corrected Author")
        errors, warns = corpus_edit.validate_fs_floor(
            [fixed],
            acknowledged=[corpus_edit.violation_token(fixed)],
            head_rows=[self.BEFORE],
            work_rows=[fixed],
        )
        assert not errors, f"an exempted row's own token must not block: {errors}"
        assert any("was not needed" in w for w in warns), warns

    def test_token_naming_nothing_in_the_edit_is_still_stale(self):
        """The property that makes tokens more than decoration is kept."""
        errors, _ = corpus_edit.validate_fs_floor(
            [self.BEFORE], acknowledged=["23:59#deadbeef"], head_rows=[], work_rows=[self.BEFORE]
        )
        assert any("stale acknowledgement" in e for e in errors), errors

    # ── ship must be wired identically to validate ───────────────────
    #
    # The ship call site had NO coverage of its head_rows/work_rows wiring: the
    # only ship test stubbed HEAD to empty, so head_rows was always [] and
    # work_rows never load-bearing. Dropping both from the ship path left the
    # whole suite green — the third instance of that hole on this branch, and
    # ship is the primary workflow.

    def _ship_args(self, **kw):
        d = dict(
            message="m", dry_run=True, no_release=True, no_push=True, branch=None, allow_small=None, skip_validate=False
        )
        d.update(kw)
        return argparse.Namespace(**d)

    def _stub_git(self, monkeypatch, head_csv):
        def fake_run(cmd, **kw):
            out = ""
            if cmd[:3] == ["git", "show", f"HEAD:{corpus_edit.CORPUS_REL}"]:
                out = head_csv
            elif cmd[:3] == ["git", "diff", "--name-only"]:
                out = f"{corpus_edit.CORPUS_REL}\n"
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

        monkeypatch.setattr(corpus_edit, "run", fake_run)
        monkeypatch.setattr(corpus_edit, "read_head_corpus", lambda: head_csv)

    def test_cmd_ship_exempts_a_metadata_edit_like_validate_does(self, sandbox, monkeypatch):
        head_csv = self._as_csv(self.BEFORE)
        self._stub_git(monkeypatch, head_csv)
        sandbox.corpus.write_text(self._as_csv(self._edited(author="Corrected Author")))
        sandbox.write_manifest(
            {"corpus_hash": "stale", "generator_hash": corpus_edit.generator_file_hash(), "files": {}}
        )
        assert corpus_edit.cmd_ship(self._ship_args()) != 2, (
            "ship must honour the metadata-only exemption; head_rows is not reaching it"
        )

    def test_cmd_ship_gates_a_new_duplicate_text_row_like_validate_does(self, sandbox, monkeypatch):
        newcomer = self._edited(title="A Different Book", author="Someone Else")
        head_csv = self._as_csv(self.BEFORE)
        self._stub_git(monkeypatch, head_csv)
        sandbox.corpus.write_text(head_csv + self._as_csv(newcomer))
        sandbox.write_manifest(
            {"corpus_hash": "stale", "generator_hash": corpus_edit.generator_file_hash(), "files": {}}
        )
        assert corpus_edit.cmd_ship(self._ship_args()) == 2, (
            "ship must gate a new duplicate-text row; work_rows is not reaching it"
        )

    def test_unrenderable_row_is_not_told_its_token_was_unnecessary(self):
        """A row that will not render is a hard error. Telling the operator the
        token 'was not needed' is false and points away from the real problem."""
        broken = self._edited(match="nowhere-in-this-text")
        errors, warns = corpus_edit.validate_fs_floor(
            [broken],
            acknowledged=[corpus_edit.violation_token(broken)],
            head_rows=[],
            work_rows=[broken],
        )
        assert any("won't render" in e for e in errors), errors
        assert not any("was not needed" in w for w in warns), warns

    def test_hint_marker_is_anchored_to_the_line_start(self, capsys):
        """The marker is prose; an error line can quote a row's timestring
        verbatim. Anchoring means a quoted value cannot pose as a stale line."""
        evil = self._edited(match=corpus_edit._STALE_MARKER + "x")
        errors, _ = corpus_edit.validate_fs_floor([evil], head_rows=[], work_rows=[evil])
        assert any("won't render" in e for e in errors), errors
        corpus_edit._print_allow_small_hint(errors)
        hint = capsys.readouterr().err
        assert "no longer match a failing row" not in hint, f"a quoted timestring was mistaken for a stale line: {hint}"


class TestDuplicateAddIsAChangedRow:
    """litclock-dev#559 review / litclock-dev#565 — a second exact copy of an existing row must reach
    the per-row validators.

    With set semantics it produced no changed row at all, so the time-tag,
    mid-word and fitted-fs checks were all skipped for it, while
    compute_dirty_buckets still regenerated the bucket.
    """

    R = corpus_edit.Row(time="12:00", match="noon", quote="It was noon.", title="T", author="A", is_nsfw=False)

    def test_exact_duplicate_add_is_reported_as_changed(self):
        changed = corpus_edit.diff_changed_rows([self.R], [self.R, self.R])
        assert len(changed) == 1, f"the extra copy must be validated: {changed}"

    def test_unchanged_corpus_still_reports_nothing(self):
        assert corpus_edit.diff_changed_rows([self.R], [self.R]) == []

    def test_three_copies_where_head_had_two_reports_one(self):
        changed = corpus_edit.diff_changed_rows([self.R, self.R], [self.R, self.R, self.R])
        assert len(changed) == 1, changed

    def test_deleting_a_duplicate_reports_nothing(self):
        assert corpus_edit.diff_changed_rows([self.R, self.R], [self.R]) == []

    def test_retag_still_reported(self):
        moved = corpus_edit.Row(
            time="13:00",
            match=self.R.match,
            quote=self.R.quote,
            title=self.R.title,
            author=self.R.author,
            is_nsfw=False,
        )
        changed = corpus_edit.diff_changed_rows([self.R], [moved])
        assert len(changed) == 1 and changed[0].time == "13:00", changed


class TestSweepOrphans:
    """litclock-dev#584 review: `_prepare_for_regen` wipes only DIRTY buckets, but a
    GENERATOR change can renumber files in a bucket whose CSV rows never
    changed — litclock-dev#584 renumbers midnight from `quote_0000_1..72` to `0..71`,
    and `0000` is not dirty unless a midnight row is edited in the same batch.
    A full regen overwrites in place and never deletes, so the old top-index
    file survives.

    That orphan is not inert: release_images.sh only WARNS about unmanifested
    PNGs so it ships, literary_clock globs the bucket so it can be selected,
    and quote_corpus returns None for it so the status file loses its
    quote/author/title.
    """

    def _tree(self, tmp_path, monkeypatch, manifest_files):
        import json

        images = tmp_path / "images"
        metadata = images / "metadata"
        metadata.mkdir(parents=True)
        monkeypatch.setattr(corpus_edit, "IMAGES_DIR", images)
        monkeypatch.setattr(corpus_edit, "METADATA_DIR", metadata)
        monkeypatch.setattr(corpus_edit, "MANIFEST_PATH", images / "manifest.json")
        monkeypatch.setattr(corpus_edit, "REPO_ROOT", tmp_path)
        (images / "manifest.json").write_text(json.dumps({"files": manifest_files}))
        return images, metadata

    def test_removes_a_renumbered_orphan_and_its_credits_sidecar(self, tmp_path, monkeypatch):
        images, metadata = self._tree(tmp_path, monkeypatch, {"quote_0000_0.png": "h0"})
        (images / "quote_0000_0.png").write_bytes(b"keep")
        (metadata / "quote_0000_0_credits.png").write_bytes(b"keep")
        (images / "quote_0000_72.png").write_bytes(b"orphan")  # the litclock-dev#584 leftover
        (metadata / "quote_0000_72_credits.png").write_bytes(b"orphan")

        removed = corpus_edit._sweep_orphans()

        assert {p.name for p in removed} == {"quote_0000_72.png", "quote_0000_72_credits.png"}
        assert (images / "quote_0000_0.png").exists()
        assert (metadata / "quote_0000_0_credits.png").exists()
        assert not (images / "quote_0000_72.png").exists()
        assert not (metadata / "quote_0000_72_credits.png").exists()

    def test_nsfw_suffixed_files_are_matched_by_their_manifest_key(self):
        """The manifest keys the quote PNG; the credits sidecar shares its stem
        with `_credits` appended. An `_nsfw` file must not be mistaken for an
        orphan just because of the suffix."""
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            images = tmp / "images"
            metadata = images / "metadata"
            metadata.mkdir(parents=True)
            (images / "manifest.json").write_text(json.dumps({"files": {"quote_0230_5_nsfw.png": "h"}}))
            (images / "quote_0230_5_nsfw.png").write_bytes(b"keep")
            (metadata / "quote_0230_5_nsfw_credits.png").write_bytes(b"keep")
            orig = (corpus_edit.IMAGES_DIR, corpus_edit.METADATA_DIR, corpus_edit.MANIFEST_PATH, corpus_edit.REPO_ROOT)
            corpus_edit.IMAGES_DIR, corpus_edit.METADATA_DIR = images, metadata
            corpus_edit.MANIFEST_PATH, corpus_edit.REPO_ROOT = images / "manifest.json", tmp
            try:
                assert corpus_edit._sweep_orphans() == []
                assert (images / "quote_0230_5_nsfw.png").exists()
                assert (metadata / "quote_0230_5_nsfw_credits.png").exists()
            finally:
                (corpus_edit.IMAGES_DIR, corpus_edit.METADATA_DIR, corpus_edit.MANIFEST_PATH, corpus_edit.REPO_ROOT) = (
                    orig
                )

    def test_an_empty_or_missing_manifest_deletes_NOTHING(self, tmp_path, monkeypatch):
        """The safety property. If the manifest is absent or has no files, the
        sweep must be a no-op rather than treating every PNG as an orphan and
        emptying images/."""
        images, metadata = self._tree(tmp_path, monkeypatch, {})
        (images / "quote_0000_0.png").write_bytes(b"precious")
        (metadata / "quote_0000_0_credits.png").write_bytes(b"precious")

        assert corpus_edit._sweep_orphans() == []
        assert (images / "quote_0000_0.png").exists()

        (images / "manifest.json").unlink()
        assert corpus_edit._sweep_orphans() == []
        assert (images / "quote_0000_0.png").exists()

    def test_a_clean_tree_is_a_no_op(self, tmp_path, monkeypatch):
        images, metadata = self._tree(tmp_path, monkeypatch, {"quote_0000_0.png": "h"})
        (images / "quote_0000_0.png").write_bytes(b"x")
        (metadata / "quote_0000_0_credits.png").write_bytes(b"x")
        assert corpus_edit._sweep_orphans() == []
