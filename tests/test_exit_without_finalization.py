"""literary_clock exits without interpreter finalization (litclock-dev#531).

`import lgpio` — pulled in by gpiozero's LGPIOFactory, which the display driver
uses — unconditionally spawns a daemon thread named `Thread-1`. Verified on
hardware: it exists before any chip is opened or pin claimed, and nothing
removes it (not closing the BUSY Button, not closing the pin factory). Its run
loop parks in a blocking read and its `stop()` only sets a flag.

So it is a daemon thread inside a syscall when Py_FinalizeEx begins, and ~1 run
in 4300 it unwinds against half-cleared globals and raises. journald records
`Exception in thread Thread-1:` and one _bootstrap_inner frame.

These tests do not need lgpio or hardware. The first pair reproduces the failure
shape with a stand-in daemon thread and proves os._exit suppresses it; the rest
pin the shipped exit path so the fix cannot be removed or defanged.
"""

import ast
import logging
import os
import subprocess
import sys
import textwrap
import types
from datetime import datetime

import pytest

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
LITERARY_CLOCK = os.path.join(SRC, "literary_clock.py")


# A process with an atexit handler and a parked daemon thread. Whether
# finalization ran is directly observable, and that is the switch the fix
# flips -- so these tests do not depend on hitting the rare race.
_PROGRAM = """
import atexit, os, sys, threading, time

atexit.register(lambda: print("ATEXIT-RAN", flush=True))

stop = threading.Event()

def _worker():
    # Parked in a wait, as lgpio's _callback_thread is parked in a blocking
    # read. Never joined; the interpreter must deal with it at teardown.
    stop.wait(30)

threading.Thread(target=_worker, daemon=True).start()
time.sleep(0.05)
{exit_stmt}
"""


def _run(exit_stmt):
    src = _PROGRAM.replace("{exit_stmt}", exit_stmt)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(src)],
        capture_output=True,
        text=True,
        timeout=40,
    )


class TestFinalizationIsSkipped:
    """What the fix actually turns on, stated so it cannot pass vacuously.

    The litclock-dev#531 race is ~1 in 4300 runs and is NOT reliably reproducible in a
    unit test. An earlier draft of this file asserted "no `Exception in thread`
    on stderr" -- which passed 12/12 for a plain `pass` exit as well, and so
    proved nothing. The evidence that the fix works on real hardware is the
    three-run trial recorded on litclock-dev#531.

    What IS directly observable is whether interpreter finalization ran at all,
    and that is exactly what the fix changes. Measured: atexit runs 6/6 under a
    normal exit and under sys.exit, 0/6 under os._exit.
    """

    def test_normal_exit_runs_finalization(self):
        """The control. Without this passing, the os._exit test is vacuous."""
        r = _run("pass")
        assert r.returncode == 0
        assert "ATEXIT-RAN" in r.stdout, "control failed: finalization did not run on a normal exit"

    def test_sys_exit_also_runs_finalization(self):
        """sys.exit raises SystemExit, which unwinds normally and still runs
        finalization -- so swapping os._exit for sys.exit silently reopens
        litclock-dev#531. That is the most likely regression here."""
        r = _run("sys.exit(0)")
        assert r.returncode == 0
        assert "ATEXIT-RAN" in r.stdout, "sys.exit unexpectedly skipped finalization"

    def test_os_exit_skips_finalization(self):
        """The property the fix rests on: no finalization means no
        daemon-thread teardown, so lgpio's parked Thread-1 is never unwound."""
        r = _run("os._exit(0)")
        assert r.returncode == 0
        assert "ATEXIT-RAN" not in r.stdout, (
            f"os._exit ran finalization; the fix would not suppress litclock-dev#531. stdout={r.stdout!r}"
        )

    def test_os_exit_does_not_swallow_the_exit_code(self):
        """A non-zero code must still propagate -- this is an exit path, not a
        way to force success."""
        assert _run("os._exit(3)").returncode == 3


class TestMainBlockActuallyTerminatesViaOsExit:
    """Execute the __main__ block and observe how it terminates.

    Every AST-shape assertion in this file approximates the property instead of
    checking it, and each round of review found a new way around the
    approximation. Round 1: a top-level `sys.exit(0)` above the final exit.
    Round 2's fix only inspected TOP-LEVEL statements, so ten mutations one
    indentation level down still passed — `if True: sys.exit(0)`,
    `with open(os.devnull): sys.exit(0)`, `for _ in range(1): sys.exit(0)`,
    `try: sys.exit(0) finally: pass`, `raise SystemExit(0)` from a helper,
    `_h = sys.exit; _h(0)`, `os.abort()`, `os.execv(...)`. All verified to
    reopen litclock-dev#531 at runtime while the suite stayed green.

    The block already contains `if args.dry_run: ... sys.exit(0)` at statement
    3, which is exactly why a shape check cannot work here: a conditional exit
    at this level is normal, correct code.

    So: run it. Stub the world, make os._exit raise a private sentinel, and
    assert the block terminates by raising that sentinel with code 0 — not via
    SystemExit, and not by falling off the end.
    """

    class _HardExit(BaseException):
        """Stands in for os._exit. BaseException so `except Exception` in the
        block cannot swallow it, exactly as a real os._exit cannot be caught."""

        def __init__(self, code):
            self.code = code
            super().__init__(code)

    def _exec_main_block(self, source=None, dry_run=False):
        """Returns ('os._exit', code) | ('SystemExit', code) | ('fell-through', None)."""
        src = source if source is not None else open(LITERARY_CLOCK).read()
        tree = ast.parse(src)
        main_if = next(
            n for n in tree.body if isinstance(n, ast.If) and ast.unparse(n.test).startswith("__name__")
        )
        body = "\n".join(ast.unparse(st) for st in main_if.body)

        hard = self._HardExit

        class _Args:
            def __init__(self):
                self.dry_run = dry_run

        class _Parser:
            def __init__(self, *a, **k):
                pass

            def add_argument(self, *a, **k):
                pass

            def parse_args(self, *a, **k):
                return _Args()

        class _EPD:
            def init(self):
                pass

            def getbuffer(self, img):
                return img

            def display(self, buf):
                pass

            def sleep(self):
                pass

            def Clear(self):
                pass

        fake_os = types.SimpleNamespace(
            _exit=lambda code: (_ for _ in ()).throw(hard(code)),
            path=os.path,
            environ=os.environ,
            replace=os.replace,
            abort=lambda: (_ for _ in ()).throw(RuntimeError("os.abort() reached")),
            execv=lambda *a: (_ for _ in ()).throw(RuntimeError("os.execv() reached")),
        )

        driver = types.ModuleType("display_driver")
        driver.epd7in5 = types.SimpleNamespace(EPD=_EPD)

        ns = {
            "__name__": "__main__",
            "os": fake_os,
            "sys": sys,
            "logging": logging,
            "argparse": types.SimpleNamespace(ArgumentParser=_Parser),
            "signal": types.SimpleNamespace(
                signal=lambda *a: None, SIGTERM=15, SIGINT=2
            ),
            "signal_handler": lambda *a: None,
            "main": lambda: (types.SimpleNamespace(size=(800, 480)), {"time": "00:00"}, None),
            "_write_status_file": lambda *a, **k: None,
            "_write_heartbeat": lambda *a, **k: None,
            "display_clear_hour": lambda *a, **k: False,
            "datetime": datetime,
            "_epd": None,
        }

        saved = sys.modules.get("display_driver")
        sys.modules["display_driver"] = driver
        try:
            exec(compile(body, "<main-block>", "exec"), ns)
        except hard as h:
            return ("os._exit", h.code)
        except SystemExit as e:
            return ("SystemExit", e.code)
        finally:
            if saved is None:
                sys.modules.pop("display_driver", None)
            else:
                sys.modules["display_driver"] = saved
        return ("fell-through", None)

    def test_shipped_block_terminates_via_os_exit_zero(self):
        how, code = self._exec_main_block()
        assert how == "os._exit", (
            f"__main__ terminated via {how!r}, not os._exit — interpreter finalization would run "
            f"and litclock-dev#531 is reopened"
        )
        assert code == 0, f"exit code {code}, expected 0"

    def test_dry_run_still_exits_normally(self):
        """--dry-run must keep using sys.exit: it never imports display_driver,
        so there is no Thread-1, and update.sh's smoke test reads that code."""
        how, code = self._exec_main_block(dry_run=True)
        assert how == "SystemExit", f"--dry-run terminated via {how!r}, expected SystemExit"
        assert code == 0, f"--dry-run exit code {code}, expected 0"

    @pytest.mark.parametrize(
        "mutation",
        [
            "if True:\n    sys.exit(0)",
            "with open(os.path.devnull if hasattr(os.path, 'devnull') else '/dev/null'):\n    sys.exit(0)",
            "for _ in range(1):\n    sys.exit(0)",
            "if True:\n    raise SystemExit(0)",
            "_h = sys.exit\n_h(0)",
        ],
    )
    def test_a_hidden_exit_before_the_final_one_is_detected(self, mutation):
        """Each of these previously passed the whole suite while making the
        fix dead code. The executing test sees them because it observes HOW the
        block terminates rather than what it looks like."""
        src = open(LITERARY_CLOCK).read()
        needle = "    logging.shutdown()\n    os._exit(0)"
        assert needle in src, "shipped exit tail not found; update this fixture"
        indented = "\n".join("    " + ln for ln in mutation.split("\n"))
        how, _ = self._exec_main_block(source=src.replace(needle, indented + "\n" + needle, 1))
        assert how != "os._exit", (
            f"mutation {mutation!r} was NOT detected — it terminated via os._exit anyway"
        )

    def test_signal_handler_terminates_via_os_exit(self):
        """Exercised, not grepped. The previous test asserted `'os._exit(1)' in
        src` and `'sys.exit' not in src`, which `raise SystemExit(1)` satisfies
        — byte-equivalent to the bug it was meant to prevent."""
        import literary_clock as lc

        hard = self._HardExit
        saved = lc.os._exit
        lc.os._exit = lambda code: (_ for _ in ()).throw(hard(code))
        try:
            with pytest.raises(hard) as caught:
                lc.signal_handler(15, None)
            assert caught.value.code == 1
        finally:
            lc.os._exit = saved


class TestShippedExitPath:
    """Pin the fix in literary_clock.py itself."""

    def _main_block(self):
        tree = ast.parse(open(LITERARY_CLOCK).read())
        for node in tree.body:
            if isinstance(node, ast.If) and ast.unparse(node.test).startswith("__name__"):
                return node
        raise AssertionError("no `if __name__ == '__main__':` block found")

    def test_exit_is_the_last_statement_of_main(self):
        """It must be LAST. Anything after it is unreachable, and a fix that
        sits mid-block leaves the finalization path live on the branches below
        it — which is where the exception actually fires."""
        body = self._main_block().body
        last = body[-1]
        assert isinstance(last, ast.Expr), f"last statement is {ast.dump(last)[:80]}"
        assert ast.unparse(last) == "os._exit(0)", (
            f"__main__ no longer ends with os._exit(0); ends with {ast.unparse(last)!r}. "
            f"Without it the lgpio daemon thread is torn down by Py_FinalizeEx (litclock-dev#531)."
        )

    def test_logging_is_flushed_before_the_exit(self):
        """os._exit skips atexit, so logging handlers are never flushed unless
        we do it. Without this a crash-adjacent log line can be lost."""
        body = self._main_block().body
        assert ast.unparse(body[-2]) == "logging.shutdown()", (
            f"statement before os._exit is {ast.unparse(body[-2])!r}, expected logging.shutdown(). "
            f"os._exit skips handler flushing."
        )

    def test_the_paint_try_does_not_swallow_the_final_exit(self):
        """The paint's own try/except must NOT contain an os._exit.

        The final exit has to be the single unconditional one for that path — if
        it moved inside, it would run on some branches only, and the daemon
        thread exists regardless of whether the paint succeeded. The pre-paint
        guard is a deliberate exception: it exits 1 from its handler because its
        failures used to escape __main__ entirely.
        """
        # Identified by CONTENT, not by index. `tries[-1]` silently retargets the
        # assertion at any try added after the paint block, and would also fail on
        # correct code if the pre-paint guard moved below it.
        tries = [n for n in self._main_block().body if isinstance(n, ast.Try)]
        assert tries, "no try block in __main__"
        paint = [t for t in tries if "epd.display" in ast.unparse(t)]
        assert paint, "could not identify the paint try by content"
        paint_try = paint[0]
        assert "os._exit" not in ast.unparse(paint_try), (
            "os._exit moved inside the paint try/except; it must stay unconditional at the end"
        )

    def test_no_unconditional_exit_precedes_the_final_one(self):
        """The demonstrated defeat, closed.

        Verified before this test existed: inserting a single `sys.exit(0)` one
        line above `logging.shutdown(); os._exit(0)` makes the fix statically
        unreachable dead code — and the whole gate stayed green at byte-identical
        counts (2751 passed, ruff clean, since ruff has no unreachable-code rule
        enabled here). The other tests only assert what the block ENDS with, and
        never that those statements can be reached.
        """
        body = self._main_block().body
        for i, node in enumerate(body[:-2]):
            dumped = ast.dump(node)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                fn = ast.unparse(node.value.func)
                assert fn not in ("sys.exit", "os._exit", "exit", "quit"), (
                    f"unconditional {fn}() at top level of __main__ (statement {i}) makes the "
                    f"final os._exit unreachable; litclock-dev#531 would be fully reopened"
                )
            assert "SystemExit" not in dumped or not isinstance(node, ast.Raise), (
                f"unconditional `raise SystemExit` at statement {i} bypasses the final os._exit"
            )

    def test_os_exit_is_not_rebound(self):
        """`os._exit = sys.exit` anywhere in the module would satisfy every
        AST-shape assertion while restoring finalization."""
        tree = ast.parse(open(LITERARY_CLOCK).read())
        for node in ast.walk(tree):
            # Direct rebinding, including annotated and augmented forms.
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    name = ast.unparse(t)
                    assert name not in ("os._exit", "sys.exit"), (
                        f"{name} is reassigned at line {node.lineno}"
                    )
                    # Shadowing the MODULE also defeats it: `os = SimpleNamespace(...)`.
                    assert name not in ("os", "sys") or node.lineno < 20, (
                        f"the {name} module is rebound at line {node.lineno}"
                    )
            # setattr(os, "_exit", ...) sidesteps assignment entirely.
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "setattr" and node.args:
                target = ast.unparse(node.args[0])
                assert target not in ("os", "sys"), (
                    f"setattr on the {target} module at line {node.lineno} can rebind _exit"
                )

    def test_signal_handler_exits_without_finalization(self):
        """SIGTERM fires on every reboot, `systemctl stop`, and the unit's
        TimeoutStopSec kill. `sys.exit` there raises SystemExit, which is a
        BaseException — not caught by the paint's `except Exception` — so it
        unwinds out of __main__ and runs the finalization this fix avoids."""
        tree = ast.parse(open(LITERARY_CLOCK).read())
        fn = next(
            n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "signal_handler"
        )
        # Structural, not substring: `raise SystemExit(1)` satisfied both of the
        # old assertions while being byte-equivalent to the sys.exit(1) they
        # existed to prevent. Verified defeat.
        last = fn.body[-1]
        assert isinstance(last, ast.Expr) and isinstance(last.value, ast.Call), (
            f"signal_handler's last statement is {ast.unparse(last)!r}, expected an os._exit call"
        )
        assert ast.unparse(last.value.func) == "os._exit", (
            f"signal_handler terminates with {ast.unparse(last.value.func)!r}, not os._exit"
        )
        for node in ast.walk(fn):
            if isinstance(node, ast.Raise) and node.exc is not None:
                assert "SystemExit" not in ast.unparse(node.exc), (
                    "signal_handler raises SystemExit, which runs interpreter finalization"
                )
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "sys.exit":
                raise AssertionError("signal_handler still calls sys.exit")

    def test_pre_paint_failures_also_skip_finalization(self):
        """The display_driver import CONSTRUCTS the gpiozero objects and spawns
        Thread-1, so a failure there is the one place litclock-dev#531 is most likely —
        and it sits before the paint's try/except. Both it and main() must be
        wrapped by a handler that exits via os._exit."""
        body = self._main_block().body
        wrappers = [n for n in body if isinstance(n, ast.Try)]
        assert wrappers, "no try block in __main__"
        guarded = [t for t in wrappers if "display_driver" in ast.unparse(t)]
        assert guarded, (
            "the display_driver import / main() call are not covered by a try; an exception "
            "there runs finalization (litclock-dev#531)"
        )
        # The handler must TERMINATE via os._exit, not merely mention it.
        # `if False: os._exit(1); sys.exit(1)` satisfied the old substring check.
        for handler in guarded[0].handlers:
            last = handler.body[-1]
            assert isinstance(last, ast.Expr) and isinstance(last.value, ast.Call), (
                f"pre-paint handler ends with {ast.unparse(last)!r}, expected an os._exit call"
            )
            assert ast.unparse(last.value.func) == "os._exit", (
                f"pre-paint handler terminates with {ast.unparse(last.value.func)!r}, not os._exit"
            )
            assert "sys.exit" not in ast.unparse(handler), "pre-paint handler still uses sys.exit"

    def test_sys_exit_is_not_used_as_the_final_exit(self):
        """sys.exit raises SystemExit, which unwinds normally and RUNS
        finalization — it does not fix litclock-dev#531. Swapping one for the other is
        the most likely silent regression here."""
        body = self._main_block().body
        assert "sys.exit" not in ast.unparse(body[-1]), (
            "final exit uses sys.exit, which still runs interpreter finalization"
        )
