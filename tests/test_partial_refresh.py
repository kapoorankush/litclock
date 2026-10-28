"""litclock-dev#963 partial refresh and litclock-dev#961 12-hour time.

The panel itself is bench-only (CLAUDE.md, Fortune mode). What runs here:
the saved-frame store, the masthead-only rule, the off switch, the wait that
lands a partial on the minute, the driver wrapping that makes every other
writer invalidate the frame, and the painter's __main__ block executed
against a fake panel so the full/partial choice is observed, not read.
"""

from __future__ import annotations

import ast
import logging
import os
import sys
import types
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import literary_clock as lc  # noqa: E402
import panel_frame  # noqa: E402

PAINTER = REPO_ROOT / "src" / "literary_clock.py"
VENDORED = REPO_ROOT / "lib/e-Paper/RaspberryPi_JetsonNano/python/lib/waveshare_epd/epd7in5_V2.py"
# The lib/e-Paper commit PANEL_WRITE_METHODS was checked against. CI's unit job
# does not check out submodules, so the two scans of VENDORED below run only
# where it is present; this pin runs everywhere and fails a driver bump until
# someone re-runs those scans against the new driver and moves it.
VENDORED_COMMIT = "86aa9932f471a50157cf02fafadc2c1b4a965449"


def _vendored_epd_class():
    submodule = REPO_ROOT / "lib/e-Paper"
    if not submodule.is_dir() or not any(submodule.iterdir()):
        pytest.skip("lib/e-Paper submodule not checked out; test_vendored_driver_commit_is_pinned covers CI")
    # Checked out but the driver is not where display_driver imports it from:
    # fail here rather than skip, or a bump that moved it would pass silently.
    tree = ast.parse(VENDORED.read_text())
    return next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "EPD")


@pytest.fixture
def frame_file(tmp_path, monkeypatch):
    path = tmp_path / "panel-frame.png"
    monkeypatch.setattr(panel_frame, "PANEL_FRAME_FILE", str(path))
    return path


def _frame(time_text="9:44", body="A body line"):
    img = Image.new("1", lc.DISPLAY_SIZE, 255)
    d = ImageDraw.Draw(img)
    d.text((40, 20), time_text, fill=0)
    d.text((40, 200), body, fill=0)
    return img


# ── the saved frame ──────────────────────────────────────────────────


class TestPanelFrame:
    def test_round_trip(self, frame_file):
        img = _frame()
        panel_frame.save(img)
        back = panel_frame.load(lc.DISPLAY_SIZE)
        assert back is not None and back.tobytes() == img.tobytes()

    def test_absent_is_none(self, frame_file):
        assert panel_frame.load(lc.DISPLAY_SIZE) is None

    def test_wrong_size_is_none(self, frame_file):
        panel_frame.save(Image.new("1", (10, 10), 255))
        assert panel_frame.load(lc.DISPLAY_SIZE) is None

    def test_garbage_is_none(self, frame_file):
        frame_file.write_bytes(b"not a png")
        assert panel_frame.load(lc.DISPLAY_SIZE) is None

    def test_forget_removes_and_tolerates_absence(self, frame_file):
        panel_frame.save(_frame())
        panel_frame.forget()
        assert not frame_file.exists()
        panel_frame.forget()

    def test_save_failure_is_not_fatal(self, tmp_path, monkeypatch):
        monkeypatch.setattr(panel_frame, "PANEL_FRAME_FILE", str(tmp_path / "missing-dir" / "f.png"))
        panel_frame.save(_frame())


def _sends_refresh(fn: ast.FunctionDef) -> bool:
    """Does the method send DISPLAY_REFRESH (0x12)? Read from the AST, since
    ast.unparse prints the literal as 18 and a text match never fires."""
    return any(
        isinstance(c, ast.Call)
        and getattr(c.func, "attr", None) == "send_command"
        and c.args
        and isinstance(c.args[0], ast.Constant)
        and c.args[0].value == 0x12
        for c in ast.walk(fn)
    )


class TestDriverWrapping:
    def test_forgetting_forgets_before_the_write(self, frame_file):
        seen = []

        class EPD:
            def display(self, buf):
                seen.append(frame_file.exists())

        EPD.display = panel_frame.forgetting(EPD.display)
        panel_frame.save(_frame())
        EPD().display(None)
        assert seen == [False], "the frame must be gone before the panel is written"

    def test_every_listed_method_exists_on_the_vendored_driver(self):
        cls = _vendored_epd_class()
        methods = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
        assert set(panel_frame.PANEL_WRITE_METHODS) <= methods

    def test_every_write_method_of_the_vendored_driver_is_listed(self):
        """A method that sends 0x12 (refresh) or starts the panel and is not
        wrapped would paint without invalidating the frame."""
        cls = _vendored_epd_class()
        writers = {
            n.name
            for n in cls.body
            if isinstance(n, ast.FunctionDef) and (_sends_refresh(n) or n.name.startswith("init"))
        }
        assert {"display", "display_Partial", "Clear"} <= writers, "the 0x12 scan found nothing: vacuous"
        assert writers <= set(panel_frame.PANEL_WRITE_METHODS)

    def test_vendored_driver_commit_is_pinned(self):
        import subprocess

        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-tree", "HEAD", "lib/e-Paper"],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        ).stdout.split()
        assert out[:3] == ["160000", "commit", VENDORED_COMMIT], (
            "lib/e-Paper moved: re-run the two vendored-driver scans with the submodule checked out, "
            "update PANEL_WRITE_METHODS if needed, then VENDORED_COMMIT"
        )

    def test_display_driver_wraps_every_write_method(self, frame_file, monkeypatch):
        """Import the real display_driver against a fake vendored package and
        paint through each method: every one must forget the frame first."""
        import importlib.util

        seen = {}

        class EPD:
            pass

        for name in panel_frame.PANEL_WRITE_METHODS:
            setattr(EPD, name, lambda self, *a, _n=name: seen.__setitem__(_n, frame_file.exists()))
        pkg = types.ModuleType("waveshare_epd")
        epd_mod = types.ModuleType("waveshare_epd.epd7in5_V2")
        epd_mod.EPD = EPD
        cfg = types.ModuleType("waveshare_epd.epdconfig")
        pkg.epd7in5_V2, pkg.epdconfig = epd_mod, cfg
        fakes = {"waveshare_epd": pkg, "waveshare_epd.epd7in5_V2": epd_mod, "waveshare_epd.epdconfig": cfg}
        for key, mod in fakes.items():
            monkeypatch.setitem(sys.modules, key, mod)
        spec = importlib.util.spec_from_file_location("_dd_under_test", REPO_ROOT / "src" / "display_driver.py")
        dd = importlib.util.module_from_spec(spec)
        monkeypatch.setattr(sys, "path", list(sys.path))
        spec.loader.exec_module(dd)
        for name in panel_frame.PANEL_WRITE_METHODS:
            panel_frame.save(_frame())
            getattr(dd.epd7in5.EPD(), name)()
        assert seen == dict.fromkeys(panel_frame.PANEL_WRITE_METHODS, False)


# ── the decision ─────────────────────────────────────────────────────


class TestMastheadOnlyChange:
    def test_time_change_is_masthead_only(self):
        assert lc._masthead_only_change(_frame("9:44"), _frame("9:45"))

    def test_identical_counts(self):
        assert lc._masthead_only_change(_frame(), _frame())

    def test_body_change_is_not(self):
        assert not lc._masthead_only_change(_frame(), _frame(body="Another line"))

    def test_first_body_row_is_not_masthead(self):
        a = Image.new("1", lc.DISPLAY_SIZE, 255)
        b = a.copy()
        b.putpixel((400, lc.QUOTE_AREA_Y), 0)
        assert not lc._masthead_only_change(a, b)
        c = a.copy()
        c.putpixel((400, lc.QUOTE_AREA_Y - 1), 0)
        assert lc._masthead_only_change(a, c)


@pytest.mark.parametrize(
    "value,on",
    [
        (None, True),
        ("", True),
        ("true", True),
        ("1", True),
        ("false", False),
        ("0", False),
        (" OFF ", False),
        ("no", False),
    ],
)
def test_off_switch(monkeypatch, value, on):
    if value is None:
        monkeypatch.delenv("LITCLOCK_PARTIAL_REFRESH", raising=False)
    else:
        monkeypatch.setenv("LITCLOCK_PARTIAL_REFRESH", value)
    assert lc._partial_refresh_enabled() is on


class TestWaitForPartial:
    @staticmethod
    def _run(monkeypatch, wall):
        class FrozenDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return wall

        slept = []
        monkeypatch.setattr(lc, "datetime", FrozenDT)
        monkeypatch.setattr(lc._time, "sleep", slept.append)
        lc._wait_for_partial()
        return slept

    def test_waits_until_the_minute_less_the_paint(self, monkeypatch):
        slept = self._run(monkeypatch, datetime(2026, 10, 26, 12, 34, 57))
        assert slept == [pytest.approx(3.0 - lc.PARTIAL_REFRESH_S)]

    def test_spring_forward_waits_the_real_seconds(self, monkeypatch):
        """01:59:57 CST is three real seconds before 03:00 CDT. Naive
        arithmetic on the target would see an hour and sleep the full lead."""
        import time

        monkeypatch.setenv("TZ", "America/Chicago")
        time.tzset()
        try:
            slept = self._run(monkeypatch, datetime(2026, 3, 8, 1, 59, 57))
        finally:
            monkeypatch.undo()
            time.tzset()
        assert slept == [pytest.approx(3.0 - lc.PARTIAL_REFRESH_S)]

    def test_a_late_run_does_not_wait(self, monkeypatch):
        slept = self._run(monkeypatch, datetime(2026, 10, 26, 12, 35, 1))
        assert slept == []

    def test_bounded_by_the_lead(self, monkeypatch):
        slept = self._run(monkeypatch, datetime(2026, 10, 26, 12, 34, 40))
        assert slept == [lc.RENDER_LEAD_S]


# ── the painter's __main__ block, executed ──────────────────────────


class _HardExit(BaseException):
    pass


def _run_main_block(monkeypatch, image, now, *, clear_hour=2, display_raises=None):
    """Execute literary_clock's __main__ body against a fake panel; return
    (panel calls, refresh passed to the status write)."""
    tree = ast.parse(PAINTER.read_text())
    main_if = next(n for n in tree.body if isinstance(n, ast.If) and ast.unparse(n.test).startswith("__name__"))
    body = "\n".join(ast.unparse(st) for st in main_if.body)
    calls: list[str] = []

    class EPD:
        def init(self):
            calls.append("init")

        def init_part(self):
            calls.append("init_part")

        def getbuffer(self, img):
            return img

        def display(self, buf):
            calls.append("display")
            if display_raises:
                raise display_raises

        def display_Partial(self, buf, x0, y0, x1, y1):
            calls.append(f"display_Partial{(x0, y0, x1, y1)}")
            if display_raises:
                raise display_raises

        def Clear(self):
            calls.append("Clear")

        def sleep(self):
            calls.append("sleep")

    for name in ("init", "init_part", "display", "display_Partial", "Clear"):
        setattr(EPD, name, panel_frame.forgetting(getattr(EPD, name)))

    class _Args:
        dry_run = False
        require_runtime_render = False
        sample_quote = False

    class _Parser:
        def __init__(self, *a, **k):
            pass

        def add_argument(self, *a, **k):
            pass

        def parse_args(self, *a, **k):
            return _Args()

    class _Os:
        def __getattr__(self, name):
            return getattr(os, name)

        @staticmethod
        def _exit(code):
            raise _HardExit(code)

    status = {}
    ns = dict(vars(lc))
    ns.update(
        {
            "__name__": "__main__",
            "os": _Os(),
            "argparse": types.SimpleNamespace(ArgumentParser=_Parser),
            "signal": types.SimpleNamespace(signal=lambda *a: None, SIGTERM=15, SIGINT=2),
            "main": lambda: (image, {"time": now.strftime("%H:%M")}, now, None),
            "_display_clear_hour": lambda: clear_hour,
            "_write_status_file": lambda *a, **k: status.update(k),
            "_write_heartbeat": lambda: None,
            "_wait_for_partial": lambda: calls.append("wait"),
            "logging": logging,
        }
    )
    driver = types.ModuleType("display_driver")
    driver.epd7in5 = types.SimpleNamespace(EPD=EPD)
    monkeypatch.setitem(sys.modules, "display_driver", driver)
    with pytest.raises(_HardExit):
        exec(compile(body, "<main-block>", "exec"), ns)
    return calls, status.get("refresh")


NOW = datetime(2026, 10, 26, 21, 44, 0)


class TestPainterChoosesTheRefresh:
    def test_no_saved_frame_is_full_and_saves(self, monkeypatch, frame_file):
        calls, refresh = _run_main_block(monkeypatch, _frame("9:44"), NOW)
        assert calls == ["init", "display", "sleep"] and refresh == "full"
        assert panel_frame.load(lc.DISPLAY_SIZE).tobytes() == _frame("9:44").tobytes()

    def test_time_only_change_is_partial_and_waits(self, monkeypatch, frame_file):
        panel_frame.save(_frame("9:43"))
        calls, refresh = _run_main_block(monkeypatch, _frame("9:44"), NOW)
        assert calls == ["init_part", "wait", "display_Partial(0, 0, 800, 480)", "sleep"]
        assert refresh == "partial"
        assert panel_frame.load(lc.DISPLAY_SIZE).tobytes() == _frame("9:44").tobytes()

    def test_body_change_is_full(self, monkeypatch, frame_file):
        panel_frame.save(_frame("9:43", body="Old fortune"))
        calls, refresh = _run_main_block(monkeypatch, _frame("9:44", body="New fortune"), NOW)
        assert calls == ["init", "display", "sleep"] and refresh == "full"

    def test_off_switch_is_full(self, monkeypatch, frame_file):
        monkeypatch.setenv("LITCLOCK_PARTIAL_REFRESH", "false")
        panel_frame.save(_frame("9:43"))
        calls, refresh = _run_main_block(monkeypatch, _frame("9:44"), NOW)
        assert calls == ["init", "display", "sleep"] and refresh == "full"

    def test_nightly_clear_is_full(self, monkeypatch, frame_file):
        panel_frame.save(_frame("1:59"))
        calls, refresh = _run_main_block(monkeypatch, _frame("2:00"), datetime(2026, 10, 26, 2, 0), clear_hour=2)
        assert calls == ["init", "Clear", "display", "sleep"] and refresh == "full"

    def test_a_failed_paint_leaves_no_frame(self, monkeypatch, frame_file):
        """The init forgets the frame; a display that raises must not get it
        back, so the next minute is a full refresh."""
        panel_frame.save(_frame("9:43"))
        calls, refresh = _run_main_block(monkeypatch, _frame("9:44"), NOW, display_raises=OSError("SPI timeout"))
        assert "display_Partial(0, 0, 800, 480)" in calls and refresh is None
        assert not frame_file.exists()


# ── 12-hour time (litclock-dev#961) ────────────────────────────────


@pytest.mark.parametrize(
    "h,m,text",
    [
        (0, 0, "12:00"),
        (0, 5, "12:05"),
        (9, 44, "9:44"),
        (12, 0, "12:00"),
        (13, 1, "1:01"),
        (21, 44, "9:44"),
        (23, 59, "11:59"),
    ],
)
def test_clock_text(h, m, text):
    assert lc._clock_text(datetime(2026, 10, 26, h, m)) == text


def test_fortune_masthead_uses_twelve_hour(monkeypatch, tmp_path):
    seen = {}
    real = lc._compose_masthead

    def spy(*a, **k):
        seen.update(k)
        return real(*a, **k)

    monkeypatch.setenv("WEATHER_ENABLED", "false")
    monkeypatch.setenv("LITCLOCK_CONTENT_MODE", "fortune")
    monkeypatch.setattr(lc, "RUNTIME_RENDER_DIR", str(tmp_path))
    monkeypatch.setattr(lc, "_compose_masthead", spy)
    lc.main(at=datetime(2026, 10, 26, 21, 44))
    assert seen["time_text"] == "9:44"


def test_time_fallback_uses_twelve_hour(monkeypatch):
    drawn = []
    draw = types.SimpleNamespace(text=lambda xy, text, **k: drawn.append(text))
    monkeypatch.setattr(lc, "_draw_fallback_line", lambda *a: None)
    lc._draw_time_fallback(draw, datetime(2026, 10, 26, 21, 44), lc.FALLBACK_GAP)
    assert drawn == ["9:44"]
