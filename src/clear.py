import logging
import os
import sys
import traceback

from display_driver import epd7in5


def main():
    try:
        epd = epd7in5.EPD()
        epd.init()
        epd.Clear()
        epd.sleep()
    except OSError as e:
        print(e)


if __name__ == "__main__":
    # litclock-dev#815 — terminate WITHOUT interpreter finalization, for the
    # same reason src/literary_clock.py does. "litclock-dev#531" there is SHORTHAND for
    # the lgpio teardown crash, not a real issue reference (litclock-dev#531 is
    # the runtime-render epic); the NOTE in src/literary_clock.py and the
    # CHANGELOG carry the full story.
    #
    # The mechanism: the module-level `from display_driver import epd7in5` above
    # pulls in lgpio via gpiozero and spawns the daemon thread `Thread-1`. It
    # parks in a blocking read and nothing removes it, so at Py_FinalizeEx it can
    # unwind against half-cleared globals and raise. `os._exit` skips
    # finalization entirely. litclock-dev#556 fixed this in the per-minute painter only,
    # so this file ran the race on every invocation until litclock-dev#815.
    #
    # The exit shim below is deliberately IDENTICAL to the one in
    # src/eink_display.py — tests/test_splash_renderers_skip_finalization.py
    # asserts the two are AST-equivalent, so a fix to one cannot silently miss
    # the other. That is the gap that let litclock-dev#556 leave BOTH files behind for a
    # whole release. This comment is the part that differs, because the two
    # files differ: eink_display.py's flush is load-bearing for the OTA smoke
    # gate (it compares `catalog-get` stdout through a pipe); here the only
    # stdout write is `main()`'s `print(e)` on an OSError, so the flush is
    # ordinary correctness rather than a gate dependency.
    #
    # Every step is individually guarded anyway: a flush can itself raise
    # (BrokenPipeError on a closed reader), and litclock-dev#813 is the lesson
    # that anything unguarded ahead of the exit can cost you the exit. The
    # `except SystemExit` arm is defensive — nothing in this file raises it —
    # and is kept so the shim stays identical to its twin.
    _exit_code = 0
    try:
        main()
    except SystemExit as _e:  # defensive here; load-bearing in the eink_display.py twin
        _exit_code = 0 if _e.code is None else (_e.code if isinstance(_e.code, int) else 1)
    except BaseException:
        traceback.print_exc()
        _exit_code = 1
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.flush()
        except BaseException:
            pass
    try:
        logging.shutdown()
    except BaseException:
        pass
    os._exit(_exit_code)
