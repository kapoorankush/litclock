"""Shared Waveshare e-Paper driver setup.

Adds the vendored waveshare_epd library to sys.path and re-exports
the display driver so consumers can simply:

    from display_driver import epd7in5

Patches ReadBusy() with a timeout to prevent infinite hangs when the
display hardware is unresponsive (upstream has no timeout).
"""

import logging
import os
import sys
import time

import panel_frame

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIBDIR = os.path.join(PROJECT_ROOT, "lib", "e-Paper", "RaspberryPi_JetsonNano", "python", "lib")

if os.path.exists(LIBDIR):
    sys.path.append(LIBDIR)

from waveshare_epd import epd7in5_V2 as epd7in5  # noqa: E402
from waveshare_epd import epdconfig  # noqa: E402

logger = logging.getLogger(__name__)

# Monkey-patch ReadBusy with a timeout. The upstream driver polls BUSY
# in an infinite loop — if the display doesn't respond (hardware fault,
# loose cable, etc.), the process hangs forever.
_BUSY_TIMEOUT_S = 15


def _ReadBusy_with_timeout(self):
    logger.debug("e-Paper busy")
    self.send_command(0x71)
    start = time.monotonic()
    while epdconfig.digital_read(self.busy_pin) == 0:
        self.send_command(0x71)
        epdconfig.delay_ms(100)
        if time.monotonic() - start > _BUSY_TIMEOUT_S:
            raise TimeoutError(
                f"e-Paper busy timeout after {_BUSY_TIMEOUT_S}s — display not responding (check cable/hardware)"
            )
    epdconfig.delay_ms(20)
    logger.debug("e-Paper busy release")


epd7in5.EPD.ReadBusy = _ReadBusy_with_timeout


# litclock-dev#963 — every write to the panel forgets the painter's saved
# frame first (see panel_frame.py). Wrapping the driver here, rather than
# asking each writer to remember, is what makes the partial refresh safe:
# a splash, clear.py or a QA script cannot paint without invalidating it.
# The painter saves its frame only AFTER its own display call returns.
for _name in panel_frame.PANEL_WRITE_METHODS:
    setattr(epd7in5.EPD, _name, panel_frame.forgetting(getattr(epd7in5.EPD, _name)))
