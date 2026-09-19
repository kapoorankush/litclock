from display_driver import epd7in5
from hard_exit import run_and_exit


def main():
    try:
        epd = epd7in5.EPD()
        epd.init()
        epd.Clear()
        epd.sleep()
    except OSError as e:
        print(e)


if __name__ == "__main__":
    # litclock-dev#815 — terminate WITHOUT interpreter finalization: the
    # module-level `from display_driver import epd7in5` above pulls in lgpio
    # via gpiozero and spawns the daemon thread `Thread-1`, which parks in a
    # blocking read and can raise at Py_FinalizeEx (the crash tracked as
    # litclock-dev#531 — see src/literary_clock.py's NOTE and the CHANGELOG). litclock-dev#556
    # fixed that in the per-minute painter only, so this file ran the race on
    # every invocation until litclock-dev#815.
    #
    # The shim lives in src/hard_exit.py, shared with src/eink_display.py
    # (litclock-dev#837 / litclock-dev#840): the two used to carry AST-identical copies
    # held together by an AST test, and the BrokenPipe escape found in the
    # v0.227.0 port review had to be fixed in both. Here the only stdout write
    # is main()'s `print(e)` on an OSError, so the helper's flush is ordinary
    # correctness rather than the OTA-gate dependency it is in the twin.
    run_and_exit(main)
