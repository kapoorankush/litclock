"""Waveshare e-Paper driver abstraction.

Historically this module hard-imported ``epd7in5_V2`` and every consumer was
welded to the 7.5" V2 panel. It now selects the driver by name at runtime so
any panel in the vendored ``lib/e-Paper`` library can drive the clock:

    import display_driver
    epd = display_driver.get_panel()      # EinkPanel or raises
    epd.init(); epd.display(epd.getbuffer(img)); epd.sleep()

Model selection (first match wins):
    1. ``EINK_MODEL`` in the process environment
    2. ``export EINK_MODEL=...`` in env.sh (several shell entry points invoke
       the renderers WITHOUT sourcing env.sh — boot-splash.sh, the bootcheck
       give-up splash — so the driver layer reads the file itself rather than
       trusting every caller to plumb the variable)
    3. default ``epd7in5_V2`` (the original LitClock panel)

``EinkPanel`` mirrors the vendor EPD surface (init/getbuffer/display/Clear/
sleep) so call sites keep the shape they always had, with three additions:
    * every hardware call runs under a watchdog timeout (the vendor drivers
      poll BUSY in infinite loops; a wedged panel would otherwise hang the
      render process forever)
    * ``Clear()`` papers over the vendor API split — some drivers take a
      fill argument, some take none
    * ``EINK_ROTATE=180`` flips the frame for upside-down mounts

Geometry: ``display_geometry()`` returns the logical landscape (w, h) canvas
renderers should compose at, WITHOUT touching hardware — from the env
override, a table of known panels, or the driver module's constants. Panels
that are physically portrait are normalized to landscape here; the vendor
``getbuffer()`` implementations accept the rotated image and re-orient it.
"""

import importlib
import logging
import os
import re
import sys
import threading
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIBDIR = os.path.join(PROJECT_ROOT, "lib", "e-Paper", "RaspberryPi_JetsonNano", "python", "lib")

if os.path.exists(LIBDIR):
    sys.path.append(LIBDIR)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "epd7in5_V2"
DEFAULT_GEOMETRY = (800, 480)

# Landscape-normalized (w, h) for common MONO panels, so geometry never
# requires importing the vendor driver (which on a Pi may claim GPIO as an
# import side effect — the --dry-run smoke test must stay hardware-free).
# Unknown models fall through to reading the driver module's EPD_WIDTH/
# EPD_HEIGHT constants, and EINK_WIDTH/EINK_HEIGHT overrides both.
KNOWN_GEOMETRY = {
    "epd1in54": (200, 200),
    "epd1in54_V2": (200, 200),
    "epd2in7": (264, 176),
    "epd2in7_V2": (264, 176),
    "epd2in9": (296, 128),
    "epd2in9_V2": (296, 128),
    "epd2in13_V2": (250, 122),
    "epd2in13_V3": (250, 122),
    "epd2in13_V4": (250, 122),
    "epd3in7": (480, 280),
    "epd4in2": (400, 300),
    "epd4in2_V2": (400, 300),
    "epd5in83_V2": (648, 480),
    "epd7in5": (640, 384),
    "epd7in5_V2": (800, 480),
}

# Watchdog ceiling for a single hardware call. A 7.5" full refresh takes ~5s
# and a full Clear() ~10s; 60s is generous for every panel in the library.
DEFAULT_OP_TIMEOUT_S = 60

# ReadBusy poll timeout for the epd7in5_V2 fast-fail patch (pre-abstraction
# behavior, preserved for the default panel).
_BUSY_TIMEOUT_S = 15

# Only these keys are ever read from env.sh here. Values are ASCII-tight by
# the config.py write validators; the parse below is deliberately dumb.
_ENV_LINE_RE = re.compile(r"""^\s*(?:export\s+)?(EINK_[A-Z0-9_]+)=["']?([A-Za-z0-9_.-]*)["']?\s*$""")

_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _env_sh_path() -> str:
    return os.environ.get("LITCLOCK_ENV_FILE") or os.path.join(PROJECT_ROOT, "env.sh")


def _env_sh_settings() -> dict:
    """Best-effort parse of EINK_* lines from env.sh. Missing/unreadable
    file → empty dict; renderers must never fail over configuration."""
    settings: dict = {}
    try:
        with open(_env_sh_path(), encoding="utf-8") as f:
            for line in f:
                m = _ENV_LINE_RE.match(line)
                if m:
                    settings[m.group(1)] = m.group(2)
    except OSError:
        pass
    return settings


def get_setting(name: str, default: str = "") -> str:
    """EINK_* setting: process env first, env.sh second, default last."""
    value = os.environ.get(name)
    if value is not None and value != "":
        return value
    value = _env_sh_settings().get(name, "")
    return value if value != "" else default


def model_name() -> str:
    """The configured panel model (a ``waveshare_epd`` module name)."""
    name = get_setting("EINK_MODEL", DEFAULT_MODEL)
    if not _MODEL_NAME_RE.match(name):
        logger.warning("EINK_MODEL %r is not a valid driver name — using %s", name, DEFAULT_MODEL)
        return DEFAULT_MODEL
    return name


def _import_driver(model: str):
    return importlib.import_module(f"waveshare_epd.{model}")


def _normalize_landscape(width: int, height: int) -> tuple[int, int]:
    return (width, height) if width >= height else (height, width)


def display_geometry() -> tuple[int, int]:
    """Logical landscape (width, height) the renderers should compose at.

    Never touches hardware. Resolution order: EINK_WIDTH/EINK_HEIGHT env
    override → KNOWN_GEOMETRY table → driver module constants → the 7.5" V2
    default (with a warning when a non-default model couldn't be resolved,
    so a typo'd EINK_MODEL is diagnosable from the journal).
    """
    w_raw, h_raw = get_setting("EINK_WIDTH"), get_setting("EINK_HEIGHT")
    if w_raw and h_raw:
        try:
            w, h = int(w_raw), int(h_raw)
            if w > 0 and h > 0:
                return _normalize_landscape(w, h)
        except ValueError:
            pass
        logger.warning("Ignoring invalid EINK_WIDTH/EINK_HEIGHT %r/%r", w_raw, h_raw)

    model = model_name()
    if model in KNOWN_GEOMETRY:
        return KNOWN_GEOMETRY[model]

    try:
        mod = _import_driver(model)
        w = int(mod.EPD_WIDTH)
        h = int(mod.EPD_HEIGHT)
        return _normalize_landscape(w, h)
    except Exception as e:
        level = logger.warning if model != DEFAULT_MODEL else logger.debug
        level("Could not resolve geometry for EINK_MODEL=%s (%s) — assuming %sx%s", model, e, *DEFAULT_GEOMETRY)
        return DEFAULT_GEOMETRY


def _call_with_timeout(fn, timeout_s: float, desc: str):
    """Run ``fn()`` on a daemon thread, raising TimeoutError if it doesn't
    return in ``timeout_s``. The vendor drivers poll the BUSY pin in infinite
    loops; a wedged panel must fail the render (systemd retries next tick)
    instead of hanging the process forever. The abandoned thread may still
    hold GPIO — acceptable because the process exits shortly after (see the
    handoff-splash subprocess note in eink_display.py: only process exit
    reliably frees lgpio line claims anyway)."""
    result: dict = {}

    def runner():
        try:
            result["value"] = fn()
        except BaseException as e:  # noqa: BLE001 — propagated to caller below
            result["error"] = e

    t = threading.Thread(target=runner, daemon=True, name=f"eink-{desc}")
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        raise TimeoutError(f"e-Paper {desc} timed out after {timeout_s:.0f}s — display not responding")
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _patch_epd7in5_readbusy(mod) -> None:
    """Fast-fail ReadBusy for the 7.5" V2 (pre-abstraction behavior). The
    upstream driver polls BUSY forever; this raises after _BUSY_TIMEOUT_S.
    Applied only to epd7in5_V2 — the 0x71 poll command and busy polarity are
    model-specific, so other panels rely on the generic call watchdog in
    _call_with_timeout instead."""
    try:
        from waveshare_epd import epdconfig  # noqa: PLC0415
    except ImportError:
        return

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

    mod.EPD.ReadBusy = _ReadBusy_with_timeout


class EinkPanel:
    """Model-agnostic wrapper exposing the vendor EPD call surface
    (init/getbuffer/display/Clear/sleep) plus logical geometry."""

    def __init__(self, model: str | None = None):
        self.model = model or model_name()
        self._mod = _import_driver(self.model)
        if self.model == DEFAULT_MODEL:
            _patch_epd7in5_readbusy(self._mod)
        self.epd = self._mod.EPD()
        physical_w = int(getattr(self.epd, "width", 0)) or int(getattr(self._mod, "EPD_WIDTH", DEFAULT_GEOMETRY[0]))
        physical_h = int(getattr(self.epd, "height", 0)) or int(getattr(self._mod, "EPD_HEIGHT", DEFAULT_GEOMETRY[1]))
        self.width, self.height = _normalize_landscape(physical_w, physical_h)
        try:
            self._timeout_s = float(get_setting("EINK_OP_TIMEOUT_S", str(DEFAULT_OP_TIMEOUT_S)))
        except ValueError:
            self._timeout_s = float(DEFAULT_OP_TIMEOUT_S)
        self._rotate_180 = get_setting("EINK_ROTATE", "0").strip() == "180"

    # -- vendor-shaped API -------------------------------------------------

    def init(self):
        ret = _call_with_timeout(self.epd.init, self._timeout_s, f"{self.model} init")
        # Vendor init() returns 0 on success and -1 on failure (or None on
        # drivers that don't report). Surface the failure instead of letting
        # a dead panel look initialized.
        if ret not in (None, 0):
            raise RuntimeError(f"e-Paper {self.model} init() failed (returned {ret})")
        return ret

    def getbuffer(self, image):
        if self._rotate_180:
            image = image.rotate(180)
        # Portrait panels: the vendor getbuffer() accepts a landscape image
        # (imwidth == panel height) and rotates internally — every driver in
        # the vendored library implements both orientations.
        return self.epd.getbuffer(image)

    def display(self, buffer):
        return _call_with_timeout(lambda: self.epd.display(buffer), self._timeout_s, f"{self.model} display")

    def Clear(self):  # noqa: N802 — mirrors the vendor method name
        def _clear():
            try:
                return self.epd.Clear()
            except TypeError:
                # Some drivers (e.g. the 2.13" family) require a fill byte.
                return self.epd.Clear(0xFF)

        return _call_with_timeout(_clear, self._timeout_s, f"{self.model} clear")

    def sleep(self):
        return _call_with_timeout(self.epd.sleep, self._timeout_s, f"{self.model} sleep")


def get_panel(model: str | None = None) -> EinkPanel:
    """Instantiate the configured panel. Raises on missing driver/hardware —
    callers that want None-on-failure wrap this (see eink_display.get_display)."""
    return EinkPanel(model)


def __getattr__(name: str):
    # Legacy alias: ``from display_driver import epd7in5`` predates the
    # abstraction. Kept import-compatible for out-of-tree scripts.
    if name == "epd7in5":
        mod = _import_driver(DEFAULT_MODEL)
        _patch_epd7in5_readbusy(mod)
        return mod
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
