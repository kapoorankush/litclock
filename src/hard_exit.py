"""Terminate a display-touching entry point WITHOUT interpreter finalization.

One helper for the exit shim that `src/clear.py` and `src/eink_display.py`
used to carry as two AST-identical 18-line copies, held together by an
AST-equivalence test (litclock-dev#815, litclock-dev#840). Both files import
`display_driver`, which pulls in lgpio via gpiozero and spawns the daemon
thread `Thread-1`; it parks in a blocking read and nothing removes it, so at
Py_FinalizeEx it can unwind against half-cleared globals and raise. `os._exit`
skips finalization entirely. (litclock-dev#531 is the shorthand for that
teardown crash — see the NOTE in `src/literary_clock.py` and the CHANGELOG.)

Stdlib only, on purpose: the helper must be importable on a dev box, in the
fake checkouts the OTA-gate tests build, and before either caller has decided
whether it will touch the panel.

`src/literary_clock.py`'s three exit arms are deliberately NOT callers, so do
not "fix" that: the painter always exits 0 from its paint block whatever
failed, exits 1 from inside its pre-paint guard, keeps `sys.exit` on
`--dry-run` (no `display_driver` import, and update.sh reads that code) and
has no stdout to flush — a different contract from "run main, translate
SystemExit, flush, exit", pinned statement-by-statement by
`tests/test_exit_without_finalization.py`.

Why every statement is guarded and the exit sits in a ``finally``
(litclock-dev#837, found by the Codex adversarial pass in the v0.227.0 port
review): the previous shims ran ``traceback.print_exc()`` bare in their
exception arm. On a broken stderr — reader gone, pipe closed — that write
raises ``BrokenPipeError``, which escaped the arm, skipped the flushes AND the
``os._exit``, and ran the very interpreter finalization the shim exists to
prevent, with `Thread-1` alive. That is litclock-dev#813's lesson repeated a
third time: anything unguarded ahead of the exit can cost you the exit.
Diagnostics here are best-effort; terminating without finalization is not.

FLUSHING IS LOAD-BEARING, not tidiness. ``os._exit`` discards buffered
writes, and ``catalog-get`` / ``catalog-count`` print to stdout which
``scripts/update.sh``'s OTA smoke gate COMPARES. Python block-buffers stdout
when it is a pipe — exactly how the gate invokes ``eink_display.py`` — so
exiting without an explicit flush would hand the gate an empty string and
revert every update on every device, forever (the false-RED direction
litclock-dev#773 exists to prevent). ``logging.shutdown()`` flushes the log
handlers ``os._exit`` would otherwise skip; it re-raises non-OSError
exceptions while ``logging.raiseExceptions`` is true, so it is guarded too.
"""

from __future__ import annotations

import logging
import os
import sys
import traceback
from collections.abc import Callable


def exit_code_for(exc: SystemExit) -> int:
    """Translate a ``SystemExit`` into the int ``os._exit`` needs.

    ``sys.exit()`` / ``sys.exit(None)`` mean 0; an int in 0..255 is itself
    (argparse's 2, the explicit ``sys.exit(1)`` on a handoff-splash paint
    failure; bool is an int subclass and passes through as 0/1); any other
    payload — ``sys.exit("message")`` — is 1, matching the interpreter.

    CLAMPED, because ``os._exit`` is not ``sys.exit`` (PR litclock-dev#851 review, Codex
    adversarial, reproduced with an atexit probe): ``os._exit(2**31)`` and
    ``os._exit(-2**31 - 1)`` raise ``OverflowError`` from inside the
    ``finally`` that is supposed to be unconditional, the exception escapes
    ``run_and_exit``, and interpreter finalization runs after all. Anything
    outside 0..255 — including negatives, which the interpreter would wrap
    to ``code & 0xFF`` — becomes 1: it is a failure code either way, and 1 is
    the one every caller already handles.
    """
    code = exc.code
    if code is None:
        return 0
    if isinstance(code, int):
        return code if 0 <= code <= 255 else 1
    return 1


def _report(text: str) -> None:
    """Best-effort stderr write: never raises, never lands on stdout."""
    stream = sys.stderr
    if stream is None:
        return
    try:
        stream.write(text)
    except BaseException:
        pass


def run_and_exit(main: Callable[[], object]) -> None:
    """Run ``main()`` and terminate the process via ``os._exit`` — always.

    Exit code: 0 when ``main`` returns; its ``SystemExit`` code when it raises
    one (see :func:`exit_code_for`); 1 for any other exception, whose traceback
    is reported on stderr on a best-effort basis. The ``finally`` is the
    contract: no failure in reporting, flushing or logging shutdown can reach
    the caller, and none can skip the exit.
    """
    # 1 until proven otherwise: if something unforeseen unwound past every
    # guard below, the finally still exits, and it exits as a FAILURE.
    code = 1
    try:
        try:
            main()
            code = 0
        except SystemExit as exc:
            # The attribute read in exit_code_for is the ONE expression here
            # outside a guard, deliberately: a SystemExit whose `.code` raises
            # is how the tests prove the finally exits 1 on an escape past
            # every guard. Do not wrap it.
            code = exit_code_for(exc)
            if exc.code is not None and not isinstance(exc.code, int):
                # sys.exit("message"): the interpreter prints the payload to
                # stderr before exiting 1. Same, best-effort.
                _report(str(exc.code) + "\n")
        except BaseException:
            # Inside its own guard (litclock-dev#837): print_exc writes to
            # stderr and raises BrokenPipeError when nobody is reading it.
            try:
                if sys.stderr is not None:
                    # Explicit file= and the None check (PR litclock-dev#851 review): with
                    # fd 2 closed at startup Python sets sys.stderr to None, and
                    # traceback.print_exc() then prints to STDOUT — on the
                    # catalog-count path that pollutes the integer the OTA
                    # gate compares.
                    traceback.print_exc(file=sys.stderr)
            except BaseException:
                pass
            code = 1
        for stream in (sys.stdout, sys.stderr):
            if stream is None:
                continue
            try:
                stream.flush()
            except BaseException:
                pass
        try:
            logging.shutdown()
        except BaseException:
            pass
    finally:
        os._exit(code)
