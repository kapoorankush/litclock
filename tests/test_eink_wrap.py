"""Renderer tests for the e-ink status splash word-wrap helper (#319).

These run in CI — unlike ``test_eink_display.py``, the wrap helper has no
waveshare-driver dependency. It only needs PIL + the bundled Literata
font, both already required by the control-server tests.

Background: a 36-char personalized welcome ("This is a test message! Love,
Alexis") rendered at the 48pt title font measures ~900px wide, which is
wider than the 800px e-ink canvas. The pre-#319 renderer computed
``title_x = (800 - width) // 2`` → negative → text fell off both edges.
The fix word-wraps to at most 2 lines centered with 40px gutters, with
explicit ``\\n`` honored as a hard break and an ellipsis truncation when
the message still overflows.
"""

from __future__ import annotations

from PIL import ImageDraw, ImageFont

from eink_display import (
    DISPLAY_SIZE,
    FONT_PATH_BOLD,
    MAX_TITLE_LINES,
    TITLE_FIT_TIERS,
    TITLE_SIDE_MARGIN,
    _fit_title,
    _wrap_title,
    create_status_image,
)

TITLE_MAX_WIDTH = DISPLAY_SIZE[0] - 2 * TITLE_SIDE_MARGIN


def _widths(lines, font):
    from PIL import Image

    d = ImageDraw.Draw(Image.new("1", (1, 1)))
    return [d.textbbox((0, 0), ln, font=font)[2] - d.textbbox((0, 0), ln, font=font)[0] for ln in lines]


def _title_font() -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_PATH_BOLD, 48)


def _line_widths_px(lines: list[str], font: ImageFont.FreeTypeFont) -> list[int]:
    from PIL import Image

    draw = ImageDraw.Draw(Image.new("1", (1, 1)))
    return [draw.textbbox((0, 0), line, font=font)[2] - draw.textbbox((0, 0), line, font=font)[0] for line in lines]


class TestWrapTitle:
    def test_short_title_stays_one_line(self):
        font = _title_font()
        max_width = DISPLAY_SIZE[0] - 2 * TITLE_SIDE_MARGIN
        lines = _wrap_title("Welcome to LitClock", font, max_width, MAX_TITLE_LINES)
        assert lines == ["Welcome to LitClock"]

    def test_bug_case_wraps_to_two_lines_within_canvas(self):
        """The exact message from hardware QA on 2026-05-10 that rendered
        with leading 'Th' and trailing 'h' clipped off both edges. After
        #319, it must wrap to ≤ MAX_TITLE_LINES whose widths fit inside
        DISPLAY_SIZE - 2 * TITLE_SIDE_MARGIN."""
        font = _title_font()
        max_width = DISPLAY_SIZE[0] - 2 * TITLE_SIDE_MARGIN
        title = "This is a test message! Love, Alexis"
        lines = _wrap_title(title, font, max_width, MAX_TITLE_LINES)
        assert 1 <= len(lines) <= MAX_TITLE_LINES
        for width in _line_widths_px(lines, font):
            assert width <= max_width, (
                f"line wider than canvas-minus-gutter ({width} > {max_width}) — would clip on the e-ink"
            )
        # Sanity: the joined output preserves every word from the input.
        assert "Alexis" in lines[-1]

    def test_overflow_truncates_with_ellipsis(self):
        font = _title_font()
        max_width = DISPLAY_SIZE[0] - 2 * TITLE_SIDE_MARGIN
        title = "A really long message that would definitely overflow even a generous limit"
        lines = _wrap_title(title, font, max_width, MAX_TITLE_LINES)
        assert len(lines) == MAX_TITLE_LINES
        assert lines[-1].endswith("…"), (
            "overflowed wrap must mark truncation with an ellipsis — silent "
            "truncation would hide that more text was intended"
        )

    def test_hard_newline_is_honored_as_line_break(self):
        font = _title_font()
        max_width = DISPLAY_SIZE[0] - 2 * TITLE_SIDE_MARGIN
        lines = _wrap_title("Happy Birthday\nMom!", font, max_width, MAX_TITLE_LINES)
        assert lines == ["Happy Birthday", "Mom!"]

    def test_oversized_single_word_breaks_at_chars(self):
        """A single word wider than the canvas must be char-broken so the
        rest of the wrap budget still has somewhere to land."""
        font = _title_font()
        max_width = DISPLAY_SIZE[0] - 2 * TITLE_SIDE_MARGIN
        lines = _wrap_title("Supercalifragilisticexpialidocious", font, max_width, MAX_TITLE_LINES)
        assert all(width <= max_width for width in _line_widths_px(lines, font))

    def test_empty_string_returns_empty(self):
        font = _title_font()
        lines = _wrap_title("", font, 720, MAX_TITLE_LINES)
        assert lines == []

    def test_leading_empty_paragraphs_do_not_eat_budget(self):
        """Adversarial /review HIGH fix: ``"\\n\\nMom!"`` previously
        ate both max_lines slots with blank lines then ellipsis-truncated
        "Mom!" away entirely. Leading and trailing empty paragraphs must
        be stripped before line budgeting."""
        font = _title_font()
        max_width = DISPLAY_SIZE[0] - 2 * TITLE_SIDE_MARGIN
        assert _wrap_title("\n\nMom!", font, max_width, MAX_TITLE_LINES) == ["Mom!"]
        assert _wrap_title("Mom!\n\n", font, max_width, MAX_TITLE_LINES) == ["Mom!"]
        # Internal blank lines between real paragraphs still count (rare,
        # but if a user intentionally types "A\n\nB" we honor it within
        # the line budget).
        result = _wrap_title("A\n\nB", font, max_width, MAX_TITLE_LINES)
        assert result[0] == "A"
        assert any("B" in line for line in result)


class TestCreateStatusImage:
    """End-to-end: the rendered image is the right size + mode and the bug
    case doesn't crash. Pixel-level layout assertions live in the wrap
    helper tests above; this only verifies the renderer wires through."""

    def test_renders_bug_case_without_clipping(self):
        img = create_status_image(
            "This is a test message! Love, Alexis",
            "1. Plug in power\n2. Connect to LitClock-Setup WiFi when prompted\n3. Be patient — first boot",
            "LitClock",
        )
        assert img.size == DISPLAY_SIZE
        assert img.mode == "1"

    def test_renders_multi_line_hard_break(self):
        img = create_status_image("Happy Birthday\nMom!", None, "LitClock")
        assert img.size == DISPLAY_SIZE

    def test_renders_default_welcome(self):
        img = create_status_image("Welcome to LitClock", None, None)
        assert img.size == DISPLAY_SIZE

    def test_renders_empty_title(self):
        """Edge case: missing welcome message should not crash."""
        img = create_status_image("", None, None)
        assert img.size == DISPLAY_SIZE


# ── #399 handoff splash SSID caveat ─────────────────────────────────


class TestFitSsidToBand:
    """Issue #399: the handoff splash paints an SSID caveat under the QR
    so a phone on cellular / a different network knows where to switch
    first. The wrap helper is testable in isolation here so we don't
    need pixel-OCR on the rendered splash."""

    def _font_and_draw(self, size: int = 18):
        from PIL import Image

        from eink_display import FONT_PATH

        font = ImageFont.truetype(FONT_PATH, size)
        draw = ImageDraw.Draw(Image.new("1", (800, 480), 255))
        return font, draw

    def test_short_ssid_returns_single_line(self):
        from eink_display import _fit_ssid_to_band

        font, draw = self._font_and_draw(18)
        assert _fit_ssid_to_band("MyHomeWiFi", font, draw, max_w=200) == ["MyHomeWiFi"]

    def test_medium_ssid_fits_one_line(self):
        from eink_display import _fit_ssid_to_band

        font, draw = self._font_and_draw(18)
        result = _fit_ssid_to_band("MyHomeWiFi-5GHz", font, draw, max_w=200)
        assert result == ["MyHomeWiFi-5GHz"], (
            "a medium-width SSID must NOT trigger a second line — "
            "a 1-line value reads as 'definitive', 2-line reads as 'wrapping'"
        )

    def test_long_ssid_wraps_to_two_lines_without_truncation(self):
        """A 24-char SSID at 18pt fits cleanly on 2 lines of 200px each;
        the full SSID must be preserved, no ellipsis."""
        from eink_display import _fit_ssid_to_band

        font, draw = self._font_and_draw(18)
        ssid = "MyHomeWiFi-5GHz-Extended"
        result = _fit_ssid_to_band(ssid, font, draw, max_w=200)
        assert 1 <= len(result) <= 2
        assert "".join(result) == ssid, f"full SSID must be preserved, got {result!r}"
        assert not any(line.endswith("…") for line in result), (
            "wrap-without-truncation must NOT emit an ellipsis — only the overflow path adds the marker"
        )

    def test_overflowing_ssid_truncates_last_line_with_ellipsis(self):
        from eink_display import _fit_ssid_to_band

        font, draw = self._font_and_draw(18)
        ssid = "VeryVeryLongHomeWifiNetworkName2024SuperExtended"
        result = _fit_ssid_to_band(ssid, font, draw, max_w=200)
        assert len(result) <= 2
        assert result[-1].endswith("…"), (
            "overflow MUST emit the ellipsis marker — silent truncation would "
            "hide that the SSID continues beyond what's shown"
        )
        # The prefix (recognizable brand) must be preserved on the first line.
        assert result[0].startswith("VeryVery"), "truncation must keep the SSID prefix; user recognizes the start"

    def test_empty_ssid_returns_empty_list(self):
        from eink_display import _fit_ssid_to_band

        font, draw = self._font_and_draw(18)
        assert _fit_ssid_to_band("", font, draw, max_w=200) == []

    def test_max_lines_one_truncates_when_overflowing(self):
        """If a future caller passes max_lines=1, an overflowing SSID must
        still be truncated with the ellipsis on that single line — not
        silently dropped or wrapped beyond the budget."""
        from eink_display import _fit_ssid_to_band

        font, draw = self._font_and_draw(18)
        ssid = "MyHomeWiFi-5GHz-Extended-Network"
        result = _fit_ssid_to_band(ssid, font, draw, max_w=200, max_lines=1)
        assert len(result) == 1
        assert result[0].endswith("…")


class TestHandoffSplashSsidCaveat:
    """End-to-end on the rendered splash: the caveat shows when the SSID
    is provided and is suppressed otherwise. Layout-region pixel sweep
    rather than OCR so the test is robust to font rasterizer drift."""

    def _make_settings(self, ssid: str = "MyHomeWiFi", **overrides) -> dict:
        base = {
            "has_location": True,
            "location_name": "San Francisco, CA",
            "timezone": "America/Los_Angeles",
            "units_label": "Imperial (°F)",
            "mature_enabled": False,
            "connected_ssid": ssid,
        }
        base.update(overrides)
        return base

    def _caveat_band_bounds(self):
        """Compute the caveat sample band from production constants so a
        layout edit (e.g. QR size or position change) moves the test in
        lockstep. The band covers the QR's full x-extent (so a re-centered
        caveat is still captured) and the y-range from caveat label
        through the wrapped SSID lines."""
        from eink_display import (
            DISPLAY_SIZE,
            HANDOFF_CAVEAT_SSID_GAP,
            HANDOFF_CAVEAT_TOP_GAP,
            HANDOFF_LEFT_MARGIN,
            HANDOFF_SSID_LINE_HEIGHT_LARGE,
            HANDOFF_SSID_MAX_LINES,
        )

        qr_size = 200  # locked-geometry literal in create_handoff_splash_image
        qr_x = DISPLAY_SIZE[0] - qr_size - HANDOFF_LEFT_MARGIN
        qr_y = 40
        url_y = qr_y + qr_size + 6
        caveat_y = url_y + HANDOFF_CAVEAT_TOP_GAP
        ssid_y = caveat_y + HANDOFF_CAVEAT_SSID_GAP
        # Bottom of caveat zone after up to HANDOFF_SSID_MAX_LINES rows.
        caveat_bottom = ssid_y + HANDOFF_SSID_MAX_LINES * HANDOFF_SSID_LINE_HEIGHT_LARGE
        return (qr_x, qr_x + qr_size, caveat_y, caveat_bottom)

    def _caveat_band(self, image):
        """Return (any_dark, dark_count) summarizing whether the caveat
        painted anything in its expected zone. Coordinates derive from
        production constants via `_caveat_band_bounds`."""
        x0, x1, y0, y1 = self._caveat_band_bounds()
        any_dark = False
        dark_count = 0
        for x in range(x0, x1):
            for y in range(y0, y1):
                if image.getpixel((x, y)) == 0:
                    any_dark = True
                    dark_count += 1
        return any_dark, dark_count

    def test_caveat_paints_when_ssid_present(self):
        from eink_display import create_handoff_splash_image

        image = create_handoff_splash_image(self._make_settings("MyHomeWiFi"), "http://192.168.2.132:8443")
        any_dark, count = self._caveat_band(image)
        assert any_dark, "caveat must paint dark pixels in the right-column band when SSID is present"
        assert count > 50, f"caveat must paint a substantial number of glyph pixels (got {count})"

    def test_caveat_suppressed_when_ssid_empty(self):
        from eink_display import create_handoff_splash_image

        image = create_handoff_splash_image(self._make_settings(""), "http://192.168.2.132:8443")
        any_dark, count = self._caveat_band(image)
        assert not any_dark, (
            f"caveat band must be all-white when SSID is empty (no '(unknown)' fallback) — "
            f"found {count} dark pixels in {(800 - 250, 800 - 50)}×(274, 340)"
        )

    def test_caveat_suppressed_when_ssid_missing_from_settings(self):
        """A caller that doesn't set connected_ssid at all (older code
        path) must still render without crashing, with the caveat
        suppressed. Backward-compatible with pre-#399 callers."""
        from eink_display import create_handoff_splash_image

        settings = self._make_settings()
        del settings["connected_ssid"]
        image = create_handoff_splash_image(settings, "http://192.168.2.132:8443")
        any_dark, _ = self._caveat_band(image)
        assert not any_dark

    def test_caveat_suppressed_when_ssid_is_whitespace_only(self):
        """Whitespace-only SSID is functionally empty — strip + suppress."""
        from eink_display import create_handoff_splash_image

        image = create_handoff_splash_image(self._make_settings("   "), "http://192.168.2.132:8443")
        any_dark, _ = self._caveat_band(image)
        assert not any_dark

    def test_caveat_does_not_overflow_caveat_zone(self):
        """Even with the longest realistic SSID + wrap, no dark pixel
        from the caveat may appear BELOW the caveat's expected bottom
        edge (caveat_bottom from `_caveat_band_bounds`). The previous
        revision of this test sampled `x > DISPLAY_SIZE[0] - 70` which
        only audited a 19-px rightmost sliver of the caveat column —
        a caveat that wrapped down 100px would have escaped detection.
        This sweeps the full caveat x-band."""
        from eink_display import DISPLAY_SIZE, create_handoff_splash_image

        image = create_handoff_splash_image(
            self._make_settings("VeryVeryLongHomeWifiNetworkName2024SuperExtended"),
            "http://192.168.2.132:8443",
        )
        x0, x1, _y0, caveat_bottom = self._caveat_band_bounds()
        # Sweep the FULL caveat x-band from the expected bottom edge
        # down to the bottom-status line. The bottom-status text is
        # centered around x≈400 (string ≈190px wide on an 800px canvas)
        # and never touches the caveat x-band (x0=550), so no
        # false-positive filtering is needed.
        for x in range(x0, x1):
            for y in range(caveat_bottom, DISPLAY_SIZE[1] - 50):
                assert image.getpixel((x, y)) == 255, (
                    f"caveat glyph leaked below its zone at ({x}, {y}); expected white below y={caveat_bottom}"
                )

    def test_caveat_does_not_crash_on_newline_in_ssid(self):
        """A `\\n` in the SSID would crash PIL's draw.textlength with
        `ValueError: can't measure length of multiline text`. The
        outer render_eink_splash swallows the crash but the splash
        would silently fail to paint for up to ~10 minutes (handoff
        fallback timer). Pin that the renderer sanitizes the SSID
        before measuring."""
        from eink_display import create_handoff_splash_image

        # Direct render path: a newline-bearing SSID must NOT crash. The
        # production sanitization happens in handoff.connected_ssid(),
        # but a defensive splash should also tolerate it — both layers
        # together kill the entire P1 class.
        settings = self._make_settings("foo\nbar")
        image = create_handoff_splash_image(settings, "http://192.168.2.132:8443")
        # The image must still render successfully (size + mode invariants).
        from eink_display import DISPLAY_SIZE

        assert image.size == DISPLAY_SIZE
        assert image.mode == "1"


class TestFitTitleAutoShrink:
    """#280 gift-message fix: `_fit_title` shrinks the font (and grows the line
    budget) so a personalized welcome renders in FULL instead of losing its
    tail to an ellipsis. Truncating "…a good time to read!" off someone's gift
    was the field bug (hardware photo, 2026-07-15)."""

    def test_short_greeting_unchanged_at_48pt_2_lines(self):
        """Regression: a short greeting must land on the top tier (48pt, ≤2
        lines) exactly as the pre-fix code did — no silent restyle of the
        common case."""
        lines, font = _fit_title("Happy Birthday Mom! Love, Alexis", FONT_PATH_BOLD, TITLE_MAX_WIDTH)
        assert font.size == TITLE_FIT_TIERS[0][0] == 48
        assert 1 <= len(lines) <= MAX_TITLE_LINES
        assert "…" not in "".join(lines)

    def test_the_field_bug_message_renders_in_full(self):
        """The exact failing shape from the hardware photo (three-name
        salutation + the pun) must render with NO ellipsis at a shrunk font."""
        msg = "Alex, Blair & Cameron: May it always be a good time to read!"
        lines, font = _fit_title(msg, FONT_PATH_BOLD, TITLE_MAX_WIDTH)
        joined = " ".join(lines)
        assert "…" not in joined, "gift message must not be truncated"
        assert font.size < 48, "a message this long must shrink below the top tier"
        # Every word of the original survives (order-preserving).
        assert joined.split() == msg.split()
        # Every line fits the canvas gutter.
        assert all(w <= TITLE_MAX_WIDTH for w in _widths(lines, font))

    def test_descending_tiers_never_increase_line_count(self):
        """Core invariant that makes 'largest font that fits' correct: at a
        smaller font, more words fit per line, so the natural (untruncated)
        line count is monotonically non-increasing as tiers shrink."""
        msg = "A moderately long personalized welcome message for the recipient"
        prev = None
        for size, _ in TITLE_FIT_TIERS:
            font = ImageFont.truetype(FONT_PATH_BOLD, size)
            natural = _wrap_title(msg, font, TITLE_MAX_WIDTH, len(msg) + 1)
            if prev is not None:
                assert len(natural) <= prev, f"line count rose from {prev} at smaller font {size}"
            prev = len(natural)

    def test_pathological_overflow_truncates_within_canvas(self):
        """A message far past any real gift greeting (and the 280-char input
        cap) degrades to the smallest tier WITH ellipsis — but every line must
        still fit the canvas, never run off the edge."""
        absurd = "supercalifragilistic " * 40
        lines, font = _fit_title(absurd, FONT_PATH_BOLD, TITLE_MAX_WIDTH)
        assert font.size == TITLE_FIT_TIERS[-1][0]
        assert len(lines) <= TITLE_FIT_TIERS[-1][1]
        assert "…" in "".join(lines)
        assert all(w <= TITLE_MAX_WIDTH for w in _widths(lines, font))

    def test_empty_title_is_safe(self):
        lines, font = _fit_title("", FONT_PATH_BOLD, TITLE_MAX_WIDTH)
        assert lines == []
        assert font.size == 48

    def test_status_image_renders_long_gift_without_crash(self):
        """End-to-end: the full gift splash (long title + multi-line steps +
        Orwell footer) renders to a valid 1-bit canvas."""
        img = create_status_image(
            "Alex, Blair & Cameron: May it always be a good time to read!",
            message="1. Plug in power\n2. Connect to LitClock-Setup WiFi when prompted\n3. Be patient",
            submessage='"It was a bright cold day in April." —Orwell',
        )
        assert img.size == DISPLAY_SIZE
        assert img.mode == "1"
