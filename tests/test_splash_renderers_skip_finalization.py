"""The splash renderers exit without interpreter finalization (litclock-dev#815).

`d4574a2b` (litclock-dev#556) fixed the litclock-dev#531 `Thread-1` teardown race by terminating via
`os._exit`, but **only in `src/literary_clock.py`**. `src/eink_display.py` and
`src/clear.py` both import `display_driver` — which pulls in lgpio via gpiozero
and spawns the daemon thread — and exited normally, running the race on every
invocation. `eink_display.py` is the boot, hotspot, QR, handoff, shutdown and
reset-failed splash, so that is every boot and every shutdown.

The dangerous half of this fix is the FLUSH, not the exit. `os._exit` discards
buffered writes, and `catalog-get` / `catalog-count` print to stdout which
`scripts/update.sh`'s OTA smoke gate compares. Python buffers stdout when it is
a pipe — exactly how the gate invokes it — so an unflushed exit would hand the
gate an empty string and revert every update on every device forever. These
tests exist mostly to pin that.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"

RENDERERS = ("eink_display.py", "clear.py")


class TestStdoutSurvivesTheHardExit:
    """The regression that would break the OTA gate. Runs through a PIPE on
    purpose — on a tty stdout is line-buffered and the bug is invisible."""

    @pytest.mark.parametrize(
        "argv,expected",
        [
            (["catalog-get", "status.relative.just_now"], "just now"),
            (["catalog-get", "boot.splash.starting.title"], "LitClock"),
        ],
    )
    def test_catalog_get_still_prints_through_a_pipe(self, argv, expected):
        r = subprocess.run(
            [sys.executable, "src/eink_display.py", *argv],
            cwd=REPO_ROOT,
            capture_output=True,  # => stdout is a PIPE, i.e. block-buffered
            text=True,
            timeout=60,
            env={**dict(__import__("os").environ), "LITCLOCK_LANGUAGE": "en"},
        )
        assert r.returncode == 0, f"exit {r.returncode}\n{r.stderr}"
        assert r.stdout.strip() == expected, (
            f"stdout was {r.stdout!r} — os._exit discarded the buffer. "
            "update.sh's smoke gate compares this exact string."
        )

    def test_catalog_count_still_prints_through_a_pipe(self):
        r = subprocess.run(
            [sys.executable, "src/eink_display.py", "catalog-count"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            env={**dict(__import__("os").environ), "LITCLOCK_LANGUAGE": "en"},
        )
        assert r.returncode == 0, f"exit {r.returncode}\n{r.stderr}"
        assert r.stdout.strip().isdigit() and int(r.stdout.strip()) > 0, r.stdout


class TestExitCodesArePreserved:
    """`os._exit` takes an int and ignores everything else, so the SystemExit
    translation has to be exact — the splash scripts branch on these."""

    def test_help_exits_zero_and_prints(self):
        r = subprocess.run(
            [sys.executable, "src/eink_display.py", "--help"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
        )
        assert r.returncode == 0, r.stderr
        assert "usage:" in r.stdout.lower()

    def test_bad_flag_exits_two(self):
        r = subprocess.run(
            [sys.executable, "src/eink_display.py", "--definitely-not-a-flag"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
        )
        assert r.returncode == 2, f"argparse's code must survive; got {r.returncode}"

    def test_no_subcommand_prints_help_and_exits_zero(self):
        r = subprocess.run(
            [sys.executable, "src/eink_display.py"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
        )
        assert r.returncode == 0, r.stderr


class TestBothRenderersTerminateWithoutFinalization:
    @pytest.mark.parametrize("name", RENDERERS)
    def test_main_block_terminates_via_os_exit(self, name):
        """Structural, and deliberately paired with the executed tests above —
        this alone would be the litclock-dev#813 mistake (an AST check that
        cannot see whether the statements before the exit can raise)."""
        tree = ast.parse((SRC / name).read_text())
        main_if = next(
            n for n in tree.body
            if isinstance(n, ast.If) and ast.unparse(n.test).startswith("__name__")
        )
        last = main_if.body[-1]
        assert isinstance(last, ast.Expr) and isinstance(last.value, ast.Call), (
            f"{name}: __main__ ends with {ast.unparse(last)!r}, expected os._exit"
        )
        assert ast.unparse(last.value.func) == "os._exit", (
            f"{name}: __main__ terminates via {ast.unparse(last.value.func)!r}, not os._exit"
        )

    @pytest.mark.parametrize("name", RENDERERS)
    def test_the_flush_precedes_the_exit(self, name):
        """The flush is what keeps update.sh's gate working; assert it is
        present AND ahead of the exit, not merely mentioned."""
        src = (SRC / name).read_text()
        block = src[src.index('if __name__ == "__main__":'):]
        assert ".flush()" in block, f"{name}: no flush before os._exit — see the module comment"
        assert block.index(".flush()") < block.rindex("os._exit"), (
            f"{name}: the flush must come BEFORE os._exit, or the buffer is discarded"
        )

    def test_the_two_shims_cannot_drift(self):
        """The ~40-line exit shim is duplicated verbatim between the two files.
        Nothing tied them together, which is exactly how litclock-dev#556 fixed the
        painter and left BOTH of these behind for a whole release.

        Compared as ASTs, so COMMENTS are free to differ — and they must: the
        flush is load-bearing for the OTA gate in eink_display.py and ordinary
        correctness in clear.py, and saying otherwise in clear.py was itself a
        finding (a verbatim copy claiming argparse and catalog-get in a file
        that has neither).
        """
        import ast

        def shim(name):
            tree = ast.parse((SRC / name).read_text())
            main_if = next(
                n for n in tree.body
                if isinstance(n, ast.If) and ast.unparse(n.test).startswith("__name__")
            )
            return ast.unparse(ast.Module(body=main_if.body, type_ignores=[]))

        a, b = shim("eink_display.py"), shim("clear.py")
        assert a == b, (
            "the two __main__ exit shims have drifted. Keep them identical (comments may "
            f"differ), or split them deliberately and delete this test.\n--- eink_display\n{a}\n--- clear\n{b}"
        )

    @pytest.mark.parametrize("name", RENDERERS)
    def test_every_renderer_that_imports_display_driver_is_covered(self, name):
        """The premise. If a renderer stops importing display_driver this test
        should be revisited rather than silently kept."""
        assert "display_driver" in (SRC / name).read_text()
