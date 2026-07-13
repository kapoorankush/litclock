"""Tests for eink_display module — QR code generation (no hardware needed)."""

import sys

import pytest
from PIL import Image

# eink_display imports qrcode at module level and calls setup_logging(),
# which is fine. But waveshare_epd is only imported inside get_display().
import eink_display
from eink_display import create_qr_display_image, generate_qr_image


class TestHandoffSplashCliExitCode:
    """#388/#484 (/review): the `handoff-splash` CLI must exit NONZERO when the
    paint fails. control_server paints the handoff splash via this subprocess and
    keys off its returncode — an exit 0 on a failed paint would report a silent
    success (the whole point of the subprocess split is to surface the failure)."""

    def _invoke(self, monkeypatch, paint_result):
        # Mock the paint so no hardware is touched; only the exit-code wiring runs.
        monkeypatch.setattr(eink_display, "display_handoff_splash", lambda settings, url: paint_result)
        monkeypatch.setattr(
            sys, "argv", ["eink_display.py", "handoff-splash", "http://x:8443", "--settings-json", "{}"]
        )
        eink_display.main()

    def test_exits_nonzero_when_paint_fails(self, monkeypatch):
        with pytest.raises(SystemExit) as exc_info:
            self._invoke(monkeypatch, paint_result=False)
        assert exc_info.value.code == 1

    def test_no_exit_when_paint_succeeds(self, monkeypatch):
        # A successful paint returns None from main() (exit 0) — no SystemExit.
        self._invoke(monkeypatch, paint_result=True)


def _count_black_pixels(img: Image.Image) -> int:
    """Count black pixels (value 0) in a binary image."""
    get_pixels = getattr(img, "get_flattened_data", None) or img.getdata
    return sum(1 for px in get_pixels() if px == 0)


class TestGenerateQrImage:
    def test_returns_pil_image(self):
        img = generate_qr_image("https://example.com")
        assert img.mode == "1"

    def test_nonzero_size(self):
        img = generate_qr_image("test data")
        w, h = img.size
        assert w > 0 and h > 0

    def test_different_data_different_images(self):
        img1 = generate_qr_image("aaa")
        img2 = generate_qr_image("bbb")
        assert img1.tobytes() != img2.tobytes()

    def test_contains_black_pixels(self):
        """QR code must have black modules, not be a blank white image."""
        img = generate_qr_image("https://example.com")
        assert _count_black_pixels(img) > 0


class TestCreateQrDisplayImage:
    def test_returns_correct_size(self):
        img = create_qr_display_image("https://example.com")
        assert img.size == (800, 480)

    def test_mode_is_binary(self):
        img = create_qr_display_image("https://example.com")
        assert img.mode == "1"

    def test_url_truncation_renders_differently(self):
        """Long URLs get truncated for display text, producing different pixel output."""
        short_url = "https://a.co"
        long_url = "https://example.com/" + "a" * 100
        img_short = create_qr_display_image(short_url)
        img_long = create_qr_display_image(long_url)
        # Different QR data → different images (proves the URL is actually encoded)
        assert img_short.tobytes() != img_long.tobytes()

    def test_with_title_renders_content(self):
        """Title text should add black pixels compared to no title."""
        img_no_title = create_qr_display_image("https://example.com")
        img_with_title = create_qr_display_image("https://example.com", title="Setup")
        # Title adds text, so pixel content differs
        assert img_no_title.tobytes() != img_with_title.tobytes()

    def test_with_caption_renders_content(self):
        """Caption text should add black pixels compared to no caption."""
        img_no_caption = create_qr_display_image("https://example.com")
        img_with_caption = create_qr_display_image("https://example.com", caption="Scan me")
        assert img_no_caption.tobytes() != img_with_caption.tobytes()

    def test_contains_qr_code(self):
        """The display image must contain a QR code (significant black pixels)."""
        img = create_qr_display_image("https://example.com")
        black_pixels = _count_black_pixels(img)
        # A QR code at 280x280 has many black modules; expect at least a few hundred
        assert black_pixels > 500
