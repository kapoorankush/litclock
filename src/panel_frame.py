"""The frame the per-minute painter last put on the panel (litclock-dev#963).

A partial refresh redraws only what changed, so it is safe only when the
panel still shows the painter's previous frame. The HAT is write-only (no
MISO, only BUSY is readable), so nothing can ask the panel what it shows.
This file is the substitute: the painter saves each frame right after
``epd.display()`` returns, and EVERY panel write forgets it first —
display_driver wraps the driver's init, display and clear methods, so a
splash, ``clear.py`` or any future writer invalidates it without knowing it
exists. The file therefore exists only while the painter's own frame is
the last thing written to the glass, and anything else (a crash mid-paint,
a reboot, since /run is tmpfs) leaves no file and the next paint is a full
refresh, which is today's behaviour.

No hardware import here, so the painter and the tests can use it freely.
"""

import functools
import logging
import os
import tempfile

from PIL import Image

PANEL_FRAME_FILE = os.environ.get("LITCLOCK_PANEL_FRAME_FILE", "/run/litclock/panel-frame.png")

# Every EPD method that starts or performs a write to the glass. display_driver
# wraps each with forgetting(); tests/test_partial_refresh.py checks the list
# against the vendored driver, so a renamed method fails there, not silently.
PANEL_WRITE_METHODS = (
    "init",
    "init_fast",
    "init_part",
    "init_4Gray",
    "display",
    "display_Partial",
    "display_4Gray",
    "Clear",
)


def forget() -> None:
    """Remove the saved frame before a write to the panel. Never raises:
    a splash must paint even if this fails (it is logged, and the painter's
    comparison against a frame that no longer matches the glass is the
    residual risk)."""
    try:
        os.unlink(PANEL_FRAME_FILE)
    except FileNotFoundError:
        pass
    except OSError as e:
        logging.warning(f"panel frame: could not remove {PANEL_FRAME_FILE}: {e}")


def forgetting(method):
    """Wrap a driver method so it forgets the saved frame before it runs."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        forget()
        return method(self, *args, **kwargs)

    return wrapper


def load(size: tuple[int, int]) -> Image.Image | None:
    """The saved frame, or None when there is none or it is unusable."""
    try:
        with Image.open(PANEL_FRAME_FILE) as img:
            img.load()
            if img.size != size or img.mode != "1":
                return None
            return img.copy()
    except FileNotFoundError:
        return None
    except Exception as e:
        logging.warning(f"panel frame: {PANEL_FRAME_FILE} unreadable ({e}); full refresh")
        return None


def save(image: Image.Image) -> None:
    """Record ``image`` as what the panel now shows. Best-effort and atomic:
    a failure only costs the next minute its partial refresh."""
    directory = os.path.dirname(PANEL_FRAME_FILE) or "."
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".panel-frame.tmp.", suffix=".png")
        with os.fdopen(fd, "wb") as f:
            image.convert("1").save(f, format="PNG")
        os.replace(tmp_path, PANEL_FRAME_FILE)
        tmp_path = None
    except Exception as e:
        logging.warning(f"panel frame: save failed (non-fatal): {e}")
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
