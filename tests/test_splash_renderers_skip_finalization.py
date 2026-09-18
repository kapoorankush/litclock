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
gate an empty string and revert every update on every device forever. The
subprocess tests exist mostly to pin that.

Since litclock-dev#837 / litclock-dev#840 the shim is ONE helper, `hard_exit.run_and_exit`,
instead of two AST-identical 18-line copies held together by an AST-equivalence test.
That test could see the copies drift; it could not see that both ran
`traceback.print_exc()` bare — so a broken stderr raised `BrokenPipeError`
past the flushes and the `os._exit`, reopening the exact race the shim exists
to close. The helper is EXECUTED here with injected failures at every step,
and both entry points are pinned to call it and nothing else.
"""

from __future__ import annotations

import ast
import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import hard_exit

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


def _main_block(name: str) -> ast.If:
    tree = ast.parse((SRC / name).read_text())
    return next(
        n for n in tree.body
        if isinstance(n, ast.If) and ast.unparse(n.test).startswith("__name__")
    )


class TestBothRenderersTerminateThroughTheHelper:
    """Structural, and deliberately paired with the executed tests below — a
    shape check alone was the litclock-dev#813 mistake (an AST check that
    cannot see whether the statements before the exit can raise). Here the
    shape is trivial by design: the whole `__main__` block is ONE call into
    the helper, so there is nothing ahead of the exit to get wrong."""

    @pytest.mark.parametrize("name", RENDERERS)
    def test_main_block_is_exactly_one_call_to_run_and_exit(self, name):
        body = _main_block(name).body
        assert len(body) == 1, (
            f"{name}: __main__ has {len(body)} statements, expected only run_and_exit(main). "
            f"Anything ahead of the helper can raise or exit before it — that was litclock-dev#837:\n"
            + "\n".join(ast.unparse(st) for st in body)
        )
        (only,) = body
        assert isinstance(only, ast.Expr) and isinstance(only.value, ast.Call), (
            f"{name}: __main__ is {ast.unparse(only)!r}, expected a call"
        )
        assert ast.unparse(only.value) == "run_and_exit(main)", (
            f"{name}: __main__ calls {ast.unparse(only.value)!r}, not run_and_exit(main)"
        )

    @pytest.mark.parametrize("name", RENDERERS)
    def test_run_and_exit_is_the_shared_helper_not_a_local_copy(self, name):
        """A local `def run_and_exit` would satisfy the call-shape test while
        bringing the duplication — and the drift — straight back."""
        tree = ast.parse((SRC / name).read_text())
        imported = [
            n for n in tree.body
            if isinstance(n, ast.ImportFrom) and n.module == "hard_exit"
            and any(a.name == "run_and_exit" for a in n.names)
        ]
        assert imported, f"{name}: run_and_exit is not imported from hard_exit"
        local = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run_and_exit"]
        assert not local, f"{name}: defines its own run_and_exit — the copies are back"

    @pytest.mark.parametrize("name", RENDERERS)
    def test_every_renderer_that_imports_display_driver_is_covered(self, name):
        """The premise. If a renderer stops importing display_driver this test
        should be revisited rather than silently kept."""
        assert "display_driver" in (SRC / name).read_text()

    def test_the_helper_is_import_safe_without_hardware(self):
        """It must be importable everywhere the renderers are — a dev box, the
        OTA-gate fake checkouts, CI — so it can pull in nothing but stdlib."""
        tree = ast.parse((SRC / "hard_exit.py").read_text())
        modules = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                modules.update(a.name.split(".")[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.module:
                modules.add(n.module.split(".")[0])
        assert modules <= {"__future__", "logging", "os", "sys", "traceback", "collections"}, (
            f"hard_exit imports {sorted(modules)}; keep it stdlib-only"
        )


class _HardExit(BaseException):
    """Stands in for os._exit. BaseException so no `except Exception` in the
    helper can swallow it, exactly as a real os._exit cannot be caught."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _Stream:
    """A stdout/stderr stand-in that RECORDS, so a test can prove its
    injection point was reached rather than assume it."""

    def __init__(self, name, log, write_raises=None, flush_raises=None):
        self.name = name
        self.log = log
        self.writes: list[str] = []
        self.flushes = 0
        self.write_raises = write_raises
        self.flush_raises = flush_raises

    def write(self, text):
        self.writes.append(text)
        self.log.append(f"write:{self.name}")
        if self.write_raises is not None:
            raise self.write_raises
        return len(text)

    def flush(self):
        self.flushes += 1
        self.log.append(f"flush:{self.name}")
        if self.flush_raises is not None:
            raise self.flush_raises


class TestRunAndExitIsExecuted:
    """The helper itself, driven with a failure injected at every step that
    can fail. Each test asserts the injection was REACHED — a stub that raises
    early would make every later injection vacuous."""

    @pytest.fixture
    def world(self, monkeypatch):
        """Fake os._exit (records + raises the sentinel), fake streams, and a
        shared ordered log of everything the helper touched.

        The streams are built here but INSTALLED by `_run`, from inside the
        test body: pytest re-installs its own capture objects on `sys.stdout`
        / `sys.stderr` when it resumes capture between fixture setup and the
        call phase, so a fixture-time swap is silently undone and every
        stream assertion goes vacuous (measured: 4 red, no flush logged).
        """
        log: list[str] = []

        def _exit(code):
            log.append(f"_exit:{code}")
            raise _HardExit(code)

        monkeypatch.setattr(hard_exit.os, "_exit", _exit)
        self._monkeypatch = monkeypatch
        return log, _Stream("stdout", log), _Stream("stderr", log)

    def _run(self, world, main) -> int:
        _log, out, err = world
        self._monkeypatch.setattr(sys, "stdout", out)
        self._monkeypatch.setattr(sys, "stderr", err)
        with pytest.raises(_HardExit) as caught:
            hard_exit.run_and_exit(main)
        return caught.value.code

    def test_the_happy_path_exits_zero_via_os_exit_not_sys_exit(self, world):
        """`pytest.raises(_HardExit)` is the whole assertion: a SystemExit (from
        a `sys.exit` swap, the likeliest silent regression) would escape it."""
        log, _, _ = world
        assert self._run(world, lambda: None) == 0
        assert log[-1] == "_exit:0"

    def test_a_failing_main_exits_one_and_reports_the_traceback(self, world):
        _, _, err = world

        def main():
            raise RuntimeError("panel fell off")

        assert self._run(world, main) == 1
        assert "RuntimeError: panel fell off" in "".join(err.writes), "the traceback must reach stderr"

    def test_a_broken_stderr_does_not_cost_the_exit_or_the_flush(self, world):
        """litclock-dev#837 — the Codex finding. `traceback.print_exc()` on a
        stderr whose reader is gone raises BrokenPipeError. Bare, that escaped
        the exception arm, skipped the flushes and the os._exit, and ran the
        interpreter finalization the shim exists to prevent.

        Two assertions on purpose: the exit is reached (the `finally` belt),
        AND stdout was still flushed (the guard belt). With only the exit
        asserted, removing the guard around print_exc stays green — the
        finally still exits 1 — while the flush that keeps the OTA gate alive
        is silently skipped.
        """
        log, out, err = world
        err.write_raises = BrokenPipeError(32, "Broken pipe")

        def main():
            raise RuntimeError("boom")

        assert self._run(world, main) == 1
        assert err.writes, "stderr was never written to — the BrokenPipe injection was not reached"
        assert out.flushes == 1, (
            f"stdout was not flushed after the broken-stderr traceback: {log}. "
            "catalog-get's answer to the OTA gate is in that buffer."
        )
        assert log[-1] == "_exit:1"

    @pytest.mark.parametrize("failing_main", [False, True], ids=["main-returns", "main-raises"])
    def test_a_raising_logging_shutdown_does_not_cost_the_exit(self, world, monkeypatch, failing_main):
        """litclock-dev#844 item 1, executed at this site. `logging.shutdown()`
        re-raises non-OSError/ValueError while `raiseExceptions` is true, so an
        unguarded call ahead of os._exit can cost the exit (litclock-dev#813)."""
        log, _, _ = world
        calls: list[str] = []

        def _shutdown():
            calls.append("shutdown")
            raise RuntimeError("handler flush failed")

        monkeypatch.setattr(logging, "shutdown", _shutdown)

        def main():
            if failing_main:
                raise RuntimeError("boom")

        assert self._run(world, main) == (1 if failing_main else 0)
        assert calls == ["shutdown"], "logging.shutdown was never called — the injection was not reached"
        assert log[-1].startswith("_exit:")

    def test_a_raising_flush_does_not_cost_the_exit_or_the_other_flush(self, world):
        """A flush can itself raise on a closed reader (litclock-dev#815's own note).
        stdout raising must not skip stderr's flush, nor the exit."""
        log, out, err = world
        out.flush_raises = BrokenPipeError(32, "Broken pipe")
        assert self._run(world, lambda: None) == 0
        # stderr may be flushed AGAIN by logging.shutdown() when a handler is
        # bound to it, so `>= 1` there; the helper's own flush of it is what
        # the stdout failure must not skip.
        assert out.flushes == 1 and err.flushes >= 1, log
        assert log[-1] == "_exit:0"

    def test_both_streams_are_flushed_and_logging_shut_down_before_the_exit(self, world, monkeypatch):
        """ORDER, executed: `os._exit` discards buffers, so the flush must
        precede it, not merely exist."""
        log, _, _ = world
        monkeypatch.setattr(logging, "shutdown", lambda: log.append("logging.shutdown"))
        self._run(world, lambda: None)
        assert log == ["flush:stdout", "flush:stderr", "logging.shutdown", "_exit:0"], log

    @pytest.mark.parametrize(
        "payload,expected",
        [
            (None, 0),          # sys.exit() — argparse --help
            (0, 0),
            (1, 1),             # the handoff-splash paint failure
            (2, 2),             # argparse usage error
            ("a message", 1),   # sys.exit("...") prints and exits 1
        ],
    )
    def test_system_exit_codes_are_translated_exactly(self, world, payload, expected):
        def main():
            raise SystemExit(payload)

        assert self._run(world, main) == expected

    @pytest.mark.parametrize("exc", [KeyboardInterrupt(), GeneratorExit()], ids=["KeyboardInterrupt", "GeneratorExit"])
    def test_a_non_exception_failure_in_main_still_exits_one(self, world, exc):
        """`except BaseException`, not `except Exception`: a KeyboardInterrupt
        mid-splash must not unwind into finalization either."""

        def main():
            raise exc

        assert self._run(world, main) == 1

    def test_an_escape_past_every_guard_still_exits_one(self, world):
        """The `finally` and the `code = 1` initialiser, pinned together (PR
        litclock-dev#851 review, testing pass: `code = 0` initial passed all 28 tests, and
        so did removing the finally — each belt was unobservable while the
        other stood).

        The ONE expression in the helper outside any guard is the attribute
        read in `exit_code_for`; a SystemExit whose `.code` property raises is
        the only way past every guard, so it is the probe. With the finally
        removed the RuntimeError escapes `pytest.raises(_HardExit)`; with
        `code = 0` initial the exit is recorded as `_exit:0`.
        """
        log, _, _ = world

        class _Hostile(SystemExit):
            @property
            def code(self):
                raise RuntimeError("code property exploded")

        def main():
            raise _Hostile()

        assert self._run(world, main) == 1
        assert log[-1] == "_exit:1", log

    def test_a_string_payload_is_reported_and_exits_one(self, world):
        """`sys.exit("message")`: the interpreter prints the payload to stderr
        and exits 1. The helper used to drop the text silently (PR litclock-dev#851
        review, Claude F1)."""
        _, _, err = world

        def main():
            raise SystemExit("no panel attached")

        assert self._run(world, main) == 1
        assert "no panel attached" in "".join(err.writes)

    @pytest.mark.parametrize(
        "payload,expected",
        [(2**31, 1), (-(2**31) - 1, 1), (-1, 1), (256, 1), (True, 1), (False, 0), (255, 255)],
    )
    def test_out_of_range_codes_are_clamped_to_one(self, world, payload, expected):
        """Unit half of the clamp; the subprocess class below proves the
        OverflowError with the REAL os._exit."""

        def main():
            raise SystemExit(payload)

        assert self._run(world, main) == expected

    def test_a_none_stderr_puts_nothing_on_stdout(self, world):
        """With fd 2 closed at startup Python sets sys.stderr to None, and a
        bare traceback.print_exc() then prints to STDOUT (PR litclock-dev#851 review,
        Claude F2) — on the catalog-count path that pollutes the integer the
        OTA gate compares."""
        log, out, _ = world
        self._monkeypatch.setattr(sys, "stderr", None)

        def main():
            raise RuntimeError("boom")

        # _run installs the fake stderr; re-None it after, then call directly.
        self._monkeypatch.setattr(sys, "stdout", out)
        with pytest.raises(_HardExit) as caught:
            hard_exit.run_and_exit(main)
        assert caught.value.code == 1
        assert out.writes == [], f"the traceback landed on stdout: {out.writes}"
        assert log[-1] == "_exit:1"

    def test_hostile_reporting_that_raises_a_base_exception_still_exits_one(self, world, monkeypatch):
        """The guard around the traceback is BaseException-wide too. A
        KeyboardInterrupt landing inside print_exc must not become the way
        out."""
        reached: list[str] = []

        def _print_exc(*a, **k):
            reached.append("print_exc")
            raise KeyboardInterrupt()

        monkeypatch.setattr(hard_exit.traceback, "print_exc", _print_exc)

        def main():
            raise RuntimeError("boom")

        assert self._run(world, main) == 1
        assert reached == ["print_exc"]


# A process with an atexit handler, run under the REAL os._exit. Whether
# finalization ran is directly observable (tests/test_exit_without_finalization
# .py's idiom): ATEXIT-RAN on stdout means the helper let the interpreter
# unwind, which is the litclock-dev#531 race reopened.
_PROGRAM = """
import atexit, sys
atexit.register(lambda: print("ATEXIT-RAN", flush=True))
sys.path.insert(0, "src")
import hard_exit


def main():
{body}


hard_exit.run_and_exit(main)
"""


class TestTheRealOsExitInASubprocess:
    """PR litclock-dev#851 review, Codex adversarial (reproduced by the Claude pass):
    `os._exit(2**31)` raises OverflowError from inside the `finally`, the
    exception escapes `run_and_exit`, and finalization runs — an atexit probe
    confirmed it. The unit tests above fake os._exit, so only a subprocess
    can see this. Also the only tests that drive the helper's
    `except BaseException` arm with the real exit."""

    @staticmethod
    def _run(body, *, preexec=None, capture_stderr=True):
        src = _PROGRAM.replace("{body}", textwrap.indent(textwrap.dedent(body).strip("\n"), "    "))
        return subprocess.run(
            [sys.executable, "-c", src],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if capture_stderr else None,
            text=True,
            timeout=60,
            preexec_fn=preexec,
        )

    def test_the_control_a_normal_return_skips_finalization(self):
        r = self._run("return None")
        assert r.returncode == 0, r.stderr
        assert "ATEXIT-RAN" not in r.stdout, "control failed: the helper ran finalization on the happy path"

    @pytest.mark.parametrize(
        "payload,expected",
        [
            ("2**31", 1),          # os._exit raises OverflowError on this
            ("-(2**31) - 1", 1),   # and on this
            ("-1", 1),             # out of 0..255 -> 1 (the interpreter would wrap it to 255)
            ("256", 1),
            ("3", 3),              # in range passes through
        ],
    )
    def test_out_of_range_exit_codes_still_skip_finalization(self, payload, expected):
        r = self._run(f"raise SystemExit({payload})")
        assert r.returncode == expected, f"SystemExit({payload}) exited {r.returncode}\n{r.stderr}"
        assert "ATEXIT-RAN" not in r.stdout, (
            f"SystemExit({payload}) ran interpreter finalization — os._exit raised inside the "
            f"finally and the escape reopened litclock-dev#531:\n{r.stderr}"
        )
        assert "OverflowError" not in r.stderr, r.stderr

    def test_a_closed_stderr_still_exits_one_with_stdout_flushed(self):
        """The litclock-dev#837 case with the REAL exit: main prints (block-buffered —
        stdout is a pipe), closes stderr, raises. The exit must be 1, no
        finalization, and the buffered stdout must still reach the pipe."""
        r = self._run(
            """
            print("OUT")
            sys.stderr.close()
            raise RuntimeError("boom")
            """
        )
        assert r.returncode == 1
        assert "OUT" in r.stdout, f"stdout was not flushed before the exit: {r.stdout!r}"
        assert "ATEXIT-RAN" not in r.stdout

    def test_a_string_payload_reaches_stderr(self):
        r = self._run('raise SystemExit("no panel attached")')
        assert r.returncode == 1
        assert "no panel attached" in r.stderr
        assert "ATEXIT-RAN" not in r.stdout

    def test_fd2_closed_at_startup_keeps_the_traceback_off_stdout(self):
        """`sys.stderr is None` for real: fd 2 closed before the interpreter
        starts. The traceback must not be printed to stdout."""
        r = self._run('raise RuntimeError("boom")', preexec=lambda: os.close(2), capture_stderr=False)
        assert r.returncode == 1
        assert "Traceback" not in r.stdout and "boom" not in r.stdout, (
            f"with sys.stderr None the traceback went to stdout: {r.stdout!r}"
        )
        assert "ATEXIT-RAN" not in r.stdout
