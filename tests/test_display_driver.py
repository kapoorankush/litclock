"""Unit tests for src/display_driver.py — the any-panel driver abstraction.

The vendored waveshare_epd library is hardware-bound and absent on dev
boxes/CI, so these tests inject fake driver modules into sys.modules and
exercise the selection, geometry, and adapter contracts:

- model selection: env → env.sh → default, with validation
- display_geometry(): env override → KNOWN_GEOMETRY → driver constants →
  default, always landscape-normalized, never touching hardware
- EinkPanel: vendor-shaped delegation (init/getbuffer/display/Clear/sleep),
  Clear() signature papering, init failure surfacing, watchdog timeout,
  EINK_ROTATE=180
- renderer integration: eink_display / literary_clock geometry follows the
  configured panel on reload
"""

from __future__ import annotations

import importlib
import os
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import display_driver  # noqa: E402

_HAS_PIL = True
try:
    from PIL import Image
except ImportError:
    _HAS_PIL = False


def _make_epd_class(width: int, height: int, *, init_ret=0, clear_needs_arg=False, init_hang_s: float = 0):
    class FakeEPD:
        instances: list = []

        def __init__(self):
            self.width = width
            self.height = height
            self.calls: list = []
            self.buffers: list = []
            FakeEPD.instances.append(self)

        def init(self):
            if init_hang_s:
                time.sleep(init_hang_s)
            self.calls.append("init")
            return init_ret

        def getbuffer(self, image):
            self.calls.append("getbuffer")
            self.buffers.append(image)
            return b"buf"

        def display(self, buffer):
            self.calls.append(("display", buffer))

        if clear_needs_arg:

            def Clear(self, color):  # noqa: N802 — vendor casing
                self.calls.append(("clear", color))
        else:

            def Clear(self):  # noqa: N802 — vendor casing
                self.calls.append("clear")

        def sleep(self):
            self.calls.append("sleep")

    return FakeEPD


@pytest.fixture
def fake_driver(monkeypatch):
    """Install a fake waveshare_epd package; returns a registrar function."""
    pkg = types.ModuleType("waveshare_epd")
    pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "waveshare_epd", pkg)

    def register(name: str, width: int, height: int, **epd_kwargs):
        mod = types.ModuleType(f"waveshare_epd.{name}")
        mod.EPD_WIDTH = width
        mod.EPD_HEIGHT = height
        mod.EPD = _make_epd_class(width, height, **epd_kwargs)
        monkeypatch.setitem(sys.modules, f"waveshare_epd.{name}", mod)
        return mod

    return register


@pytest.fixture(autouse=True)
def _clean_eink_env(monkeypatch):
    for key in ("EINK_MODEL", "EINK_ROTATE", "EINK_WIDTH", "EINK_HEIGHT", "EINK_OP_TIMEOUT_S"):
        monkeypatch.delenv(key, raising=False)
    # Point env.sh reads at a nonexistent file so a developer's real env.sh
    # can't leak into assertions.
    monkeypatch.setenv("LITCLOCK_ENV_FILE", "/nonexistent/litclock-test-env.sh")


class TestModelSelection:
    def test_default_model(self):
        assert display_driver.model_name() == "epd7in5_V2"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("EINK_MODEL", "epd2in7_V2")
        assert display_driver.model_name() == "epd2in7_V2"

    def test_invalid_name_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("EINK_MODEL", "epd2in7; rm -rf /")
        assert display_driver.model_name() == "epd7in5_V2"

    def test_env_sh_fallback(self, monkeypatch, tmp_path):
        env_sh = tmp_path / "env.sh"
        env_sh.write_text('# comment\nexport WEATHER_UNITS=imperial\nexport EINK_MODEL="epd4in2"\n')
        monkeypatch.setenv("LITCLOCK_ENV_FILE", str(env_sh))
        assert display_driver.model_name() == "epd4in2"

    def test_process_env_wins_over_env_sh(self, monkeypatch, tmp_path):
        env_sh = tmp_path / "env.sh"
        env_sh.write_text("export EINK_MODEL=epd4in2\n")
        monkeypatch.setenv("LITCLOCK_ENV_FILE", str(env_sh))
        monkeypatch.setenv("EINK_MODEL", "epd2in9_V2")
        assert display_driver.model_name() == "epd2in9_V2"

    def test_unreadable_env_sh_is_harmless(self, monkeypatch):
        monkeypatch.setenv("LITCLOCK_ENV_FILE", "/nonexistent/nope.sh")
        assert display_driver.model_name() == "epd7in5_V2"


class TestDisplayGeometry:
    def test_default(self):
        assert display_driver.display_geometry() == (800, 480)

    def test_known_model_no_driver_needed(self, monkeypatch):
        # No waveshare_epd is installed in the test env — the table answers.
        monkeypatch.setenv("EINK_MODEL", "epd2in7_V2")
        assert display_driver.display_geometry() == (264, 176)

    def test_env_size_override_and_portrait_normalization(self, monkeypatch):
        monkeypatch.setenv("EINK_WIDTH", "176")
        monkeypatch.setenv("EINK_HEIGHT", "264")
        assert display_driver.display_geometry() == (264, 176)

    def test_invalid_env_size_ignored(self, monkeypatch):
        monkeypatch.setenv("EINK_WIDTH", "abc")
        monkeypatch.setenv("EINK_HEIGHT", "264")
        assert display_driver.display_geometry() == (800, 480)

    def test_unknown_model_reads_driver_constants(self, monkeypatch, fake_driver):
        fake_driver("epd_custom", 128, 296)
        monkeypatch.setenv("EINK_MODEL", "epd_custom")
        # Portrait constants are landscape-normalized.
        assert display_driver.display_geometry() == (296, 128)

    def test_unknown_model_without_driver_falls_back(self, monkeypatch):
        monkeypatch.setenv("EINK_MODEL", "epd_not_a_panel")
        assert display_driver.display_geometry() == (800, 480)


class TestEinkPanel:
    def test_vendor_shaped_delegation(self, monkeypatch, fake_driver):
        fake_driver("epd_fake", 250, 122)
        monkeypatch.setenv("EINK_MODEL", "epd_fake")
        panel = display_driver.get_panel()
        assert (panel.width, panel.height) == (250, 122)
        assert panel.init() == 0
        buf = panel.getbuffer(Image.new("1", (250, 122), 255)) if _HAS_PIL else b"buf"
        if not _HAS_PIL:
            pytest.skip("PIL required for getbuffer")
        panel.display(buf)
        panel.Clear()
        panel.sleep()
        epd = panel.epd
        assert "init" in epd.calls
        assert ("display", b"buf") in epd.calls
        assert "clear" in epd.calls
        assert "sleep" in epd.calls

    def test_portrait_panel_reports_landscape_logical_size(self, monkeypatch, fake_driver):
        fake_driver("epd_portrait", 122, 250)
        monkeypatch.setenv("EINK_MODEL", "epd_portrait")
        panel = display_driver.get_panel()
        assert (panel.width, panel.height) == (250, 122)

    def test_clear_signature_fallback(self, monkeypatch, fake_driver):
        fake_driver("epd_argclear", 250, 122, clear_needs_arg=True)
        monkeypatch.setenv("EINK_MODEL", "epd_argclear")
        panel = display_driver.get_panel()
        panel.Clear()
        assert ("clear", 0xFF) in panel.epd.calls

    def test_init_failure_raises(self, monkeypatch, fake_driver):
        fake_driver("epd_dead", 250, 122, init_ret=-1)
        monkeypatch.setenv("EINK_MODEL", "epd_dead")
        panel = display_driver.get_panel()
        with pytest.raises(RuntimeError, match="init"):
            panel.init()

    def test_watchdog_timeout(self, monkeypatch, fake_driver):
        fake_driver("epd_hung", 250, 122, init_hang_s=5)
        monkeypatch.setenv("EINK_MODEL", "epd_hung")
        monkeypatch.setenv("EINK_OP_TIMEOUT_S", "0.2")
        panel = display_driver.get_panel()
        start = time.monotonic()
        with pytest.raises(TimeoutError, match="not responding"):
            panel.init()
        assert time.monotonic() - start < 2, "timeout must fire well before the hung call returns"

    @pytest.mark.skipif(not _HAS_PIL, reason="PIL required")
    def test_rotate_180(self, monkeypatch, fake_driver):
        fake_driver("epd_fake", 250, 122)
        monkeypatch.setenv("EINK_MODEL", "epd_fake")
        monkeypatch.setenv("EINK_ROTATE", "180")
        panel = display_driver.get_panel()
        img = Image.new("1", (250, 122), 255)
        img.putpixel((0, 0), 0)
        panel.getbuffer(img)
        seen = panel.epd.buffers[0]
        assert seen.getpixel((249, 121)) == 0, "180° rotation must move the (0,0) mark to the far corner"
        assert seen.getpixel((0, 0)) != 0

    def test_missing_driver_raises_for_get_panel(self, monkeypatch):
        monkeypatch.setenv("EINK_MODEL", "epd_not_a_panel")
        with pytest.raises(ModuleNotFoundError):
            display_driver.get_panel()


@pytest.mark.skipif(not _HAS_PIL, reason="PIL required")
class TestRendererGeometryIntegration:
    """eink_display / literary_clock capture DISPLAY_SIZE at import — verify
    it follows the configured panel across a reload, then restore."""

    def _reload_with_size(self, module_name: str, width: str, height: str):
        os.environ["EINK_WIDTH"] = width
        os.environ["EINK_HEIGHT"] = height
        module = importlib.import_module(module_name)
        return importlib.reload(module)

    def _restore(self, module_name: str):
        os.environ.pop("EINK_WIDTH", None)
        os.environ.pop("EINK_HEIGHT", None)
        importlib.reload(importlib.import_module(module_name))

    def test_eink_display_follows_configured_geometry(self):
        try:
            mod = self._reload_with_size("eink_display", "400", "300")
            assert mod.DISPLAY_SIZE == (400, 300)
            img = mod.create_status_image("LitClock", message="Starting...")
            assert img.size == (400, 300)
            # 400×300 is below the compact threshold — the handoff splash
            # must take the collision-free compact layout and still render.
            splash = mod.create_handoff_splash_image({"has_location": True}, "http://192.168.1.2")
            assert splash.size == (400, 300)
        finally:
            self._restore("eink_display")

    def test_literary_clock_corner_qr_gate(self):
        try:
            mod = self._reload_with_size("literary_clock", "264", "176")
            assert mod.DISPLAY_SIZE == (264, 176)
            assert mod.QR_CORNER_FITS is False
        finally:
            self._restore("literary_clock")
            mod = importlib.import_module("literary_clock")
            # Locked 7.5" geometry restored (matches test_literary_clock.py).
            assert mod.QR_CORNER_FITS is True
            assert mod.QR_POSITION == (713, 0)
