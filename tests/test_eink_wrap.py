"""Renderer tests for the e-ink status splash word-wrap helper (litclock-dev#319).

These run in CI — unlike ``test_eink_display.py``, the wrap helper has no
waveshare-driver dependency. It only needs PIL + the bundled Literata
font, both already required by the control-server tests.

Background: a 36-char personalized welcome ("This is a test message! Love,
Alexis") rendered at the 48pt title font measures ~900px wide, which is
wider than the 800px e-ink canvas. The pre-litclock-dev#319 renderer computed
``title_x = (800 - width) // 2`` → negative → text fell off both edges.
The fix word-wraps to at most 2 lines centered with 40px gutters, with
explicit ``\\n`` honored as a hard break and an ellipsis truncation when
the message still overflows.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

import eink_display
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

REPO_ROOT = Path(__file__).resolve().parent.parent

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
        litclock-dev#319, it must wrap to ≤ MAX_TITLE_LINES whose widths fit inside
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


# ── litclock-dev#399 handoff splash SSID caveat ─────────────────────────────────


class TestFitSsidToBand:
    """Issue litclock-dev#399: the handoff splash paints an SSID caveat under the QR
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
        suppressed. Backward-compatible with pre-litclock-dev#399 callers."""
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
    """litclock-dev#280 gift-message fix: `_fit_title` shrinks the font (and grows the line
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


class TestPanelTextClamps:
    """litclock-dev#620 /review — every panel string except the TITLE was drawn
    with an unbounded single-line draw.text from a fixed origin.

    The handoff row values are a live bug: WEATHER_LOCATION_NAME_MAX_LEN admits
    120 characters and the value column leaves ~37, so a place name typed in
    PWA Location > Specific (preserved across reboots by litclock-dev#337, and repainted
    because a WiFi reset clears .handoff-complete) runs off the glass.

    The status message/submessage are a guard, not a bug: every shipped English
    string fits today. They are in scope for the litclock-dev#532 catalogs, where a
    translation ~30% longer is the normal case.
    """

    def _draw(self):
        img = Image.new("1", eink_display.DISPLAY_SIZE, 255)
        return img, ImageDraw.Draw(img)

    def _row_font(self):
        return ImageFont.truetype(eink_display.FONT_PATH, 22)

    def test_realistic_location_names_are_untouched(self):
        """Nothing IP-geo actually returns may lose characters.

        Goes through _fit_row_text, not _clamp_to_width, because the ladder is
        the path a row value takes: a value that needs a point or two of shrink
        is fine, a value that loses its tail is not. Timezones included — 77 of
        498 IANA zones exceed the budget at full size, so the ladder carries
        them.
        """
        _, draw = self._draw()
        font = self._row_font()
        realistic = (
            "Austin, Texas",
            "Frankfurt am Main, Hesse",
            "Charleville-Mezieres, Grand Est",
            "Buenos Aires, Argentina",
            "America/Los_Angeles",
            "America/Argentina/Buenos_Aires",
            "America/Indiana/Indianapolis",
        )
        for name in realistic:
            out, used = eink_display._fit_row_text(name, font, draw, eink_display.HANDOFF_VALUE_BUDGET, "t")
            assert out == name, f"lost characters from a realistic value: {name!r} -> {out!r}"
            assert draw.textlength(out, font=used) <= eink_display.HANDOFF_VALUE_BUDGET
            assert used.size >= eink_display.HANDOFF_ROW_FONT_FLOOR

    def test_long_typed_place_name_is_clamped_inside_the_panel(self):
        _, draw = self._draw()
        font = self._row_font()
        name = "Llanfairpwllgwyngyllgogerychwyrndrobwllllantysiliogogogoch, Wales"
        out = eink_display._clamp_to_width(name, font, draw, eink_display.HANDOFF_VALUE_BUDGET, "t")
        assert out != name
        assert draw.textlength(out, font=font) <= eink_display.HANDOFF_VALUE_BUDGET

    def test_max_length_location_never_leaves_the_panel(self):
        """The validator's ceiling must be renderable, not just accepted."""
        from config import WEATHER_LOCATION_NAME_MAX_LEN

        _, draw = self._draw()
        font = self._row_font()
        out = eink_display._clamp_to_width(
            "A" * WEATHER_LOCATION_NAME_MAX_LEN, font, draw, eink_display.HANDOFF_VALUE_BUDGET, "t"
        )
        right_edge = eink_display.HANDOFF_VALUE_COLUMN + draw.textlength(out, font=font)
        assert right_edge <= eink_display.DISPLAY_SIZE[0], f"row value runs {right_edge} past an 800px panel"

    def test_handoff_row_ink_stays_on_the_panel(self):
        """End-to-end through the real drawing helper.

        Drawn on an OVERSIZED canvas on purpose: PIL clips at the image edge, so
        asserting "ink <= 800" on an 800px image can never fail and the test
        would pass with the clamp deleted. The wide canvas lets the overflow
        actually appear, which is what makes this a guard.
        """
        wide = Image.new("1", (2400, 200), 255)
        draw = ImageDraw.Draw(wide)
        eink_display._draw_dotted_row(draw, 100, "Location", "B" * 120, self._row_font())
        ink = ImageOps.invert(wide.convert("L")).getbbox()
        assert ink is not None, "nothing was drawn"
        assert ink[2] <= eink_display.DISPLAY_SIZE[0], (
            f"row value reaches x={ink[2]} on an 800px panel — it would run off the glass"
        )

    def test_status_message_and_submessage_are_clamped(self):
        _, draw = self._draw()
        font = ImageFont.truetype(eink_display.FONT_PATH, 28)
        out = eink_display._clamp_block_to_panel("X" * 400, font, draw, "t")
        assert draw.textlength(out, font=font) <= eink_display.DISPLAY_SIZE[0]

    def test_status_image_keeps_side_margins_on_an_overlong_message(self):
        """Integration guard on create_status_image itself.

        Unclamped, `msg_x = (800 - width) // 2` goes NEGATIVE for a long string,
        so the text starts off-canvas and bleeds to both edges. Asserting the
        rendered ink keeps a margin on BOTH sides fails the moment the clamp is
        removed — unlike a width check, which PIL's edge-clipping makes vacuous.
        """
        img = create_status_image("Status", "X" * 400, "Y" * 400)
        ink = ImageOps.invert(img.convert("L")).getbbox()
        assert ink is not None
        left, right = ink[0], ink[2]
        assert left >= 2, f"ink starts at x={left} — text bled off the left edge"
        assert right <= eink_display.DISPLAY_SIZE[0] - 2, f"ink reaches x={right} — text bled off the right edge"

    def test_multiline_status_message_keeps_its_line_structure(self):
        """Gift mode's welcome text carries embedded newlines — clamping must be
        per line, or the three numbered steps collapse into one."""
        _, draw = self._draw()
        font = ImageFont.truetype(eink_display.FONT_PATH, 28)
        text = "1. Plug in power\n2. Connect to LitClock-Setup WiFi when prompted\n3. Be patient"
        out = eink_display._clamp_block_to_panel(text, font, draw, "t")
        assert out.count("\n") == 2
        assert out == text, "shipped gift copy fits and must render byte-identical"

    def test_every_shipped_status_string_still_fits_unclamped(self):
        """Regression guard on the LAYOUTS, not the clamp.

        Reads the strings out of the shell scripts that actually paint them
        rather than a hand-copied list. The first version of this test copied
        four literals and missed all 15 shutdown-splash quotes — including the
        widest shipped string on the device, which has 21px of headroom — so it
        could only ever confirm the same wrong survey the docstring was built
        on (the guard-derived-from-its-own-constant shape).
        """
        _, draw = self._draw()
        msg = ImageFont.truetype(eink_display.FONT_PATH, 28)
        sub = ImageFont.truetype(eink_display.FONT_PATH, 20)
        budget = eink_display.DISPLAY_SIZE[0] - 2 * eink_display.STATUS_SIDE_MARGIN

        shutdown = (REPO_ROOT / "scripts" / "shutdown-splash.sh").read_text()
        # Parse the QUOTES=( ... ) arrays positionally: welcome, reboot,
        # poweroff, in file order. Splitting on the case labels does NOT work —
        # "reboot)" and "poweroff)" both appear first in the flag parser at the
        # top of the script.
        blocks = re.findall(r"QUOTES=\(\s*\n(.*?)^\s*\)", shutdown, re.MULTILINE | re.DOTALL)
        assert len(blocks) == 3, f"expected 3 QUOTES arrays (welcome/reboot/poweroff), parsed {len(blocks)}"
        # Poweroff quotes render as the 28pt MESSAGE; welcome and reboot as the
        # 20pt SUBMESSAGE. Measure each at the font its own branch uses.
        fonts = (sub, sub, msg)
        worst = (0.0, "")
        total = 0
        for block, font in zip(blocks, fonts, strict=True):
            quotes = re.findall(r"^\s*'(.+)'\s*$", block, re.MULTILINE)
            assert quotes, "parsed a QUOTES array with no quotes in it"
            total += len(quotes)
            for q in quotes:
                w = draw.textlength(q, font=font)
                if w > worst[0]:
                    worst = (w, q)
                assert w <= budget, f"shipped quote no longer fits ({w:.0f}px > {budget}px): {q!r}"
        assert total >= 15, f"only parsed {total} shutdown quotes — the parser drifted, not the copy"
        assert worst[0] > 700, (
            f"expected the widest shipped quote near the limit, got {worst[0]:.0f}px — parser probably missed lines"
        )

        for font, text in (
            (msg, "The clock couldn't repair itself after an update."),
            (sub, "Please re-flash the SD card — see the LitClock docs."),
            (msg, "2. Connect to LitClock-Setup WiFi when prompted"),
            (msg, "3. Be patient — first boot takes a moment :)"),
            (sub, "Or SSH in to configure manually"),
        ):
            assert eink_display._clamp_block_to_panel(text, font, draw, "t") == text, f"now truncates: {text!r}"

    def test_long_ssid_status_message_is_a_live_overflow(self):
        """first-boot.sh renders `display_message firstboot.splash.wifi_connected
        --slot "ssid=$ssid"` (catalog message "Network: {ssid}") with the
        joined network name. 32 bytes is the 802.11 maximum, and at
        28pt that is ~1006px against a 760px budget — a real, user-controlled
        overflow. (Production now SHRINKS this string via the litclock-dev#532
        ladder rather than clamping it; this remains a unit pin of the FLOOR
        helper the ladder falls back to — /review litclock-dev#734.)"""
        _, draw = self._draw()
        msg = ImageFont.truetype(eink_display.FONT_PATH, 28)
        raw = "Network: " + "W" * 32
        budget = eink_display.DISPLAY_SIZE[0] - 2 * eink_display.STATUS_SIDE_MARGIN
        assert draw.textlength(raw, font=msg) > budget, "premise: a wide 32-char SSID must exceed the budget"
        out = eink_display._clamp_block_to_panel(raw, msg, draw, "t")
        assert draw.textlength(out, font=msg) <= budget

    def test_overlong_line_in_a_LATER_position_is_clamped(self):
        """Guards the per-line semantics. A mutation that clamped only line[0]
        and passed the rest through survived the whole suite, because the only
        multiline case used a block whose every line already fit — while gift
        mode's real message has its longest line SECOND."""
        _, draw = self._draw()
        font = ImageFont.truetype(eink_display.FONT_PATH, 28)
        budget = eink_display.DISPLAY_SIZE[0] - 2 * eink_display.STATUS_SIDE_MARGIN
        out = eink_display._clamp_block_to_panel("short\n" + "X" * 400 + "\nshort", font, draw, "t")
        lines = out.split("\n")
        assert len(lines) == 3
        assert lines[0] == "short" and lines[2] == "short"
        for i, line in enumerate(lines):
            assert draw.textlength(line, font=font) <= budget, f"line {i} unclamped"

    def test_status_margins_are_asserted_against_the_declared_budget(self):
        """A margin assertion, not a not-clipped assertion. Setting
        STATUS_SIDE_MARGIN to 0 previously left the whole suite green."""
        img = create_status_image("Status", "X" * 400, "Y" * 400)
        ink = ImageOps.invert(img.convert("L")).getbbox()
        assert ink is not None
        m = eink_display.STATUS_SIDE_MARGIN
        assert ink[0] >= m - 1, f"ink starts at x={ink[0]}, inside the {m}px margin"
        assert ink[2] <= eink_display.DISPLAY_SIZE[0] - m + 1, f"ink reaches x={ink[2]}, inside the {m}px margin"

    def test_status_side_margin_is_a_real_margin(self):
        """Validated against intent, separately from the code that uses it, so
        the margin tests cannot be satisfied by shrinking the constant."""
        assert eink_display.STATUS_SIDE_MARGIN >= 16

    def test_handoff_LABEL_is_fitted_too(self):
        """The label column is the tighter of the two (182px vs 302px), and
        moving the value column left tightened it deliberately — the trade made
        when this fix was chosen. So the labels lean on the ladder harder than
        the values do: English fits at full size, de 'Nicht jugendfreie Zitate'
        (242px) shrinks rather than truncating."""
        _, draw = self._draw()
        font = self._row_font()
        for real in ("Location", "Timezone", "Units", "Mature quotes", "Nicht jugendfreie Zitate"):
            out, used = eink_display._fit_row_text(real, font, draw, eink_display.HANDOFF_LABEL_BUDGET, "t")
            assert out == real, f"lost characters from a realistic label: {real!r} -> {out!r}"
            assert draw.textlength(out, font=used) <= eink_display.HANDOFF_LABEL_BUDGET

        # English labels must not shrink at all — if they start shrinking, the
        # column moved too far and the block is about to look inconsistent.
        for english in ("Location", "Timezone", "Units", "Mature quotes"):
            _, used = eink_display._fit_row_text(english, font, draw, eink_display.HANDOFF_LABEL_BUDGET, "t")
            assert used.size == font.size, f"{english!r} shrank; the label column is too narrow"

        # Past the floor, truncation is still the backstop.
        out, used = eink_display._fit_row_text("L" * 80, font, draw, eink_display.HANDOFF_LABEL_BUDGET, "t")
        assert draw.textlength(out, font=used) <= eink_display.HANDOFF_LABEL_BUDGET
        assert out.endswith("…"), "past the floor the label must truncate, not overflow"

    def test_label_overflow_cannot_overprint_the_value_column(self):
        """End-to-end on an oversized canvas: an unclamped label runs past the
        dotted leader and prints on top of the value."""
        wide = Image.new("1", (2400, 200), 255)
        draw = ImageDraw.Draw(wide)
        eink_display._draw_dotted_row(draw, 100, "L" * 80, "Austin, Texas", self._row_font())
        ink = ImageOps.invert(wide.convert("L")).getbbox()
        assert ink[2] <= eink_display.DISPLAY_SIZE[0]

    def test_newline_in_a_row_value_does_not_raise(self):
        """draw.textlength raises ValueError on multiline text. Before the
        clamp, _draw_dotted_row only called draw.text, which renders multiline
        harmlessly — so wiring in the clamp created a new crash path. Reachable
        via `eink_display.py handoff-splash --settings-json`, which json.loads
        arbitrary input. The sibling SSID field is already defended this way.

        Goes through _draw_dotted_row, NOT _clamp_to_width. The earlier version
        called the helper directly and so kept passing when the shrink ladder
        was added in front of it and reintroduced the crash — the test no longer
        covered the path it was written to protect. Both cells are exercised:
        the label measures on the same ladder as the value.
        """
        _, draw = self._draw()
        font = self._row_font()
        for label, value in (
            ("Location", "Austin\nTexas"),
            ("Loc\nation", "Austin"),
            ("Location", "Austin\r\nTexas"),
            ("Location", "L" * 60 + "\n" + "L" * 60),
        ):
            eink_display._draw_dotted_row(draw, 100, label, value, font)

        out, _ = eink_display._fit_row_text("Austin\nTexas", font, draw, 420, "t")
        assert "\n" not in out and out.startswith("Austin")

    def test_budget_boundary_is_not_off_by_one(self):
        """Nothing previously sat near either budget, so the ellipsis fitter's
        boundary comparison was never exercised at the edge."""
        _, draw = self._draw()
        font = self._row_font()
        budget = eink_display.HANDOFF_VALUE_BUDGET
        s = "W"
        while draw.textlength(s + "W", font=font) <= budget:
            s += "W"
        assert eink_display._clamp_to_width(s, font, draw, budget, "t") == s, "a string exactly at budget must pass"
        longer = eink_display._clamp_to_width(s + "W", font, draw, budget, "t")
        assert longer != s + "W" and draw.textlength(longer, font=font) <= budget


class TestHandoffRowsClearTheQrBlock:
    """The settings rows share the panel with the PWA QR (x 550..750, y 40..240)
    and its URL / caveat / SSID text below it. The QR is pasted BEFORE the rows
    are drawn, so a row that reaches that far paints ON TOP of it.

    Budgeting the rows against the panel edge (the first version of this PR)
    left 200px of overprint. These render the REAL splash and assert the QR
    region is untouched, which is the only assertion that would have caught it:
    the pre-existing panel-edge test passes at 750px on an 800px panel.
    """

    BASE = {
        "has_location": True,
        "location_name": "Austin, Texas",
        "timezone": "America/Chicago",
        "units_label": "Imperial (°F)",
        "mature_enabled": False,
        "connected_ssid": "cooper-iot",
    }
    URL = "http://192.168.2.134"

    def _qr_region(self, settings):
        """Render the splash and return the QR rectangle's pixels."""
        img = eink_display.create_handoff_splash_image(settings, self.URL)
        x, y, s = eink_display.HANDOFF_QR_X, eink_display.HANDOFF_QR_Y, eink_display.HANDOFF_QR_SIZE
        return img.crop((x, y, x + s, y + s)).tobytes()

    def _long(self, **over):
        return {**self.BASE, **over}

    def test_a_typed_120_char_place_name_never_touches_the_qr(self):
        """The PWA accepts 120 chars in Location > Specific and litclock-dev#337 preserves
        it across reboots, so it genuinely reaches this splash."""
        baseline = self._qr_region(self.BASE)
        assert self._qr_region(self._long(location_name="L" * 120)) == baseline, (
            "a long typed place name painted into the QR block — it would degrade or "
            "break the scan, and the QR is the primary route to the PWA here"
        )

    def test_realistic_values_never_reach_the_right_column(self):
        """Geometric, per-row, on an oversized canvas.

        The pixel-crop tests above only see the QR RECTANGLE (y 40..240), and
        only the Location row (ink y 215..228) falls inside it — the Timezone,
        Units and Mature rows draw BELOW y=240, so a crop-based assertion can
        never fail for them. Two earlier versions of this test were green
        against the exact regression they named for that reason. Measuring the
        row's own ink on a wide canvas is y-independent, so it covers every row.
        """
        font = ImageFont.truetype(eink_display.FONT_PATH, 22)
        realistic = [
            ("Location", "Buenos Aires, Argentina"),
            ("Location", "Llanfairpwllgwyngyllgogerychwyrndrobwllllantysiliogogogoch, Anglesey"),
            ("Timezone", "America/Argentina/Buenos_Aires"),
            ("Timezone", "America/North_Dakota/New_Salem"),
            ("Timezone", "America/Los_Angeles"),
            ("Units", "Imperial (\u00b0F)"),
        ]
        for label, value in realistic:
            wide = Image.new("1", (2400, 200), 255)
            draw = ImageDraw.Draw(wide)
            eink_display._draw_dotted_row(draw, 100, label, value, font)
            ink = ImageOps.invert(wide.convert("L")).getbbox()
            assert ink is not None, f"nothing drawn for {value!r}"
            assert ink[2] <= eink_display.HANDOFF_RIGHT_COLUMN_X, (
                f"row {label}={value!r} reaches x={ink[2]}, past the right-column "
                f"boundary {eink_display.HANDOFF_RIGHT_COLUMN_X}"
            )

    def test_the_value_budget_is_bounded_by_the_qr_not_the_panel(self):
        """States the invariant the two renders above enforce. Deliberately NOT
        the only guard: a constant-vs-constant check stays green if the whole
        layout shifts together, which is why the render assertions exist."""
        reach = eink_display.HANDOFF_VALUE_COLUMN + eink_display.HANDOFF_VALUE_BUDGET
        assert reach <= eink_display.HANDOFF_RIGHT_COLUMN_X, (
            f"row values can reach x={reach}, past the right column at {eink_display.HANDOFF_RIGHT_COLUMN_X}"
        )
        # The above alone is near-tautological while the budget is DEFINED from
        # HANDOFF_RIGHT_COLUMN_X. These two are not: they pin the anchor against
        # the wrong bound this PR exists to replace, and pin the QR's literal
        # position, so swapping the anchor back to the panel edge fails here.
        panel_edge_budget = eink_display.DISPLAY_SIZE[0] - 8 - eink_display.HANDOFF_VALUE_COLUMN
        assert eink_display.HANDOFF_VALUE_BUDGET < panel_edge_budget, (
            "budget is no narrower than the panel-edge formula — the anchor regressed"
        )
        assert eink_display.HANDOFF_QR_X == 550 and eink_display.HANDOFF_RIGHT_COLUMN_X <= 550

    def test_the_not_detected_variant_renders_and_stays_in_its_lane(self):
        """The failure splash uses the other branch of every row."""
        nd = {"has_location": False, "units_label": "Imperial (°F)", "mature_enabled": False}
        img = eink_display.create_handoff_splash_image(nd, self.URL)
        # NOTE: an earlier version asserted `region.tobytes() == ... or True`,
        # which always passes. Every value on this branch is the fixed
        # HANDOFF_NOT_DETECTED constant or a short label, so this branch can
        # only ever show that it renders and stays in its lane — it is
        # coverage, not a stress case. The stress lives in the tests above.
        strip = img.crop((eink_display.HANDOFF_RIGHT_COLUMN_X, 200, eink_display.HANDOFF_QR_X, 340))
        assert ImageOps.invert(strip.convert("L")).getbbox() is None, (
            "row ink entered the gutter between the settings block and the QR column"
        )

    def test_a_shrunk_value_shares_the_row_baseline(self):
        """Mixed sizes in one row must share a BASELINE. PIL anchors text
        top-left by default, so a shrunk value drawn at the same y floats up by
        the ascent difference — 7px at the floor against a 22pt row.

        Asserts on the ink BOTTOM, which IS the baseline for strings with no
        descenders — so this is exact rather than tolerance-fudged. The strings
        are deliberately all-caps with no comma or slash: both of those glyphs
        descend in Literata, which silently broke a first version of this test.
        """
        font = ImageFont.truetype(eink_display.FONT_PATH, 22)
        probe = ImageDraw.Draw(Image.new("1", (10, 10)))

        def ink_bottom(value):
            canvas = Image.new("1", (2400, 160), 255)
            draw = ImageDraw.Draw(canvas)
            eink_display._draw_dotted_row(draw, 40, "Location", value, font)
            strip = canvas.crop((eink_display.HANDOFF_VALUE_COLUMN, 0, 2400, 160))
            box = ImageOps.invert(strip.convert("L")).getbbox()
            assert box is not None, f"nothing drawn for {value!r}"
            return box[3]

        full = ink_bottom("AUSTIN")
        for value in ("MASSACHUSETTS INSTITUTE BOSTON", "NEWCASTLE UPON TYNE ENGLAND UK", "L" * 120):
            _, used = eink_display._fit_row_text(value, font, probe, eink_display.HANDOFF_VALUE_BUDGET, "t")
            assert used.size < font.size, f"premise: {value[:20]!r} must take the ladder"
            assert ink_bottom(value) == full, (
                f"a {used.size}pt value sits off the row baseline "
                f"({ink_bottom(value)} vs {full}) — the row reads as broken"
            )

    def test_a_shrunk_label_shares_the_row_baseline(self):
        """Same invariant on the label column, which is the one litclock-dev#532 will push
        hardest: the value column moved left, so translated labels take the
        ladder before values do. All-caps, descender-free, so ink bottom is the
        baseline exactly.
        """
        font = ImageFont.truetype(eink_display.FONT_PATH, 22)
        probe = ImageDraw.Draw(Image.new("1", (10, 10)))
        long_label = "NICHT JUGENDFREIE ZITATE"
        _, used = eink_display._fit_row_text(long_label, font, probe, eink_display.HANDOFF_LABEL_BUDGET, "t")
        assert used.size < font.size, "premise: this label must take the ladder"

        canvas = Image.new("1", (2400, 160), 255)
        draw = ImageDraw.Draw(canvas)
        eink_display._draw_dotted_row(draw, 40, long_label, "AUSTIN", font)

        label_box = ImageOps.invert(
            canvas.crop((0, 0, eink_display.HANDOFF_VALUE_COLUMN - 8, 160)).convert("L")
        ).getbbox()
        value_box = ImageOps.invert(
            canvas.crop((eink_display.HANDOFF_VALUE_COLUMN, 0, 2400, 160)).convert("L")
        ).getbbox()
        assert label_box is not None and value_box is not None
        assert label_box[3] == value_box[3], (
            f"a {used.size}pt label sits off the row baseline ({label_box[3]} vs value {value_box[3]})"
        )

    def test_row_ink_never_enters_the_right_column_lane(self):
        """The general form of this PR's bug, asserted on the rendered panel.

        The first fix budgeted rows against the QR at x=550 and looked correct.
        But the caveat label is CENTRED under the QR and is wider than it
        ("Scan with your phone on:" is 212px vs 200px), so it starts at x=543
        and overlaps the Units row vertically — 1px of clearance. Asserting
        against the QR rectangle alone could not see that. This measures the
        left column's actual ink against the declared lane boundary, so any
        future right-column element is covered by construction.
        """
        worst = {
            "has_location": True,
            "location_name": "Llanfairpwllgwyngyllgogerychwyrndrobwllllantysiliogogogoch, Anglesey",
            "timezone": "America/Argentina/Buenos_Aires",
            "units_label": "Imperial (°F)",
            "mature_enabled": True,
            "connected_ssid": "cooper-iot",
        }
        img = eink_display.create_handoff_splash_image(worst, "http://192.168.2.134")

        # Left column: everything above the CTA, cropped at the lane boundary.
        rows_band = img.crop((0, 190, eink_display.HANDOFF_RIGHT_COLUMN_X, 340))
        box = ImageOps.invert(rows_band.convert("L")).getbbox()
        assert box is not None, "no row ink rendered — the test would pass vacuously"
        assert box[2] <= eink_display.HANDOFF_RIGHT_COLUMN_X, (
            f"row ink reaches x={box[2]}, past the lane boundary {eink_display.HANDOFF_RIGHT_COLUMN_X}"
        )

        # The boundary is a dividing line, not a reserved empty gap: the caveat
        # label legitimately starts at x=543, right of the line. What must never
        # happen is the two lanes' ink meeting. Re-render with SHORT values and
        # confirm the right column's ink is unchanged — if a long row value had
        # bled across, these would differ.
        short = {**worst, "location_name": "Austin, Texas", "timezone": "America/Chicago"}
        right = (eink_display.HANDOFF_RIGHT_COLUMN_X, 190, eink_display.DISPLAY_SIZE[0], 340)
        assert img.crop(right).tobytes() == (
            eink_display.create_handoff_splash_image(short, "http://192.168.2.134").crop(right).tobytes()
        ), "long row values changed the right column's pixels — the lanes are touching"

    def test_the_shipped_caveat_label_stays_in_its_lane(self):
        """Pins the measurement the lane boundary was derived from. If the copy
        changes and grows, this fails HERE with a clear reason rather than
        silently eating the gutter. litclock-dev#532: translating this string is the likely
        trigger — de is 282px, which crosses the boundary.
        """
        draw = ImageDraw.Draw(Image.new("1", eink_display.DISPLAY_SIZE, 255))
        small = ImageFont.truetype(eink_display.FONT_PATH, 18)
        width = draw.textlength(eink_display.HANDOFF_CAVEAT_LABEL, font=small)
        left_edge = eink_display.HANDOFF_QR_X + (eink_display.HANDOFF_QR_SIZE - width) // 2
        assert left_edge >= eink_display.HANDOFF_RIGHT_COLUMN_X, (
            f"caveat label {eink_display.HANDOFF_CAVEAT_LABEL!r} is {width:.0f}px and starts at "
            f"x={left_edge:.0f}, left of the lane boundary {eink_display.HANDOFF_RIGHT_COLUMN_X} — "
            f"widen the lane or shorten the copy"
        )

    def test_the_ladder_survives_a_font_with_no_usable_path(self):
        """create_handoff_splash_image falls back to ImageFont.load_default()
        when Literata fails to load. That font's `.path` is a BytesIO, so the
        ladder ENTERS its loop and ImageFont.truetype() raises on the consumed
        buffer — the except/break branch. Untested until now, and it is the
        branch that runs on a device with a broken font install.
        """
        draw = ImageDraw.Draw(Image.new("1", eink_display.DISPLAY_SIZE, 255))
        default_font = ImageFont.load_default(size=22)
        out, used = eink_display._fit_row_text("L" * 120, default_font, draw, eink_display.HANDOFF_VALUE_BUDGET, "t")
        assert draw.textlength(out, font=used) <= eink_display.HANDOFF_VALUE_BUDGET
        assert out != "L" * 120, "must fall back to truncation when the ladder cannot run"

    def test_the_whole_splash_renders_with_fonts_missing(self):
        """End-to-end on the same fallback: anchor='ls' needs a FreeType font,
        so a bitmap fallback would raise inside every row. Guards the degraded
        path rather than only the happy one."""
        real_path, real_bold = eink_display.FONT_PATH, eink_display.FONT_PATH_BOLD
        try:
            eink_display.FONT_PATH = "/nonexistent/Missing.ttf"
            eink_display.FONT_PATH_BOLD = "/nonexistent/MissingBold.ttf"
            img = eink_display.create_handoff_splash_image(
                {
                    "has_location": True,
                    "location_name": "Buenos Aires, Argentina",
                    "timezone": "America/Argentina/Buenos_Aires",
                    "units_label": "Metric (°C)",
                    "mature_enabled": False,
                    "connected_ssid": "cooper-iot",
                },
                "http://192.168.2.134",
            )
            assert img.size == eink_display.DISPLAY_SIZE
        finally:
            eink_display.FONT_PATH, eink_display.FONT_PATH_BOLD = real_path, real_bold

    def test_untrusted_row_values_are_stripped_of_control_characters(self):
        """location_name and timezone come from the ip-api.com response, fetched
        over plain HTTP, so they are not trusted. The sibling connected_ssid
        field was already filtered; these were not. A bidi override or
        zero-width run could reorder or hide part of the rows the splash exists
        to let the user verify."""
        assert eink_display._sanitize_row_value("A‮BCD") == "ABCD"
        assert eink_display._sanitize_row_value("Aus​tin") == "Austin"
        assert eink_display._sanitize_row_value("Austin\x1b[31m, TX") == "Austin[31m, TX"
        # TAB must become a space, not vanish — dropping it welds the words
        # either side together, which is a silent corruption of a city name.
        assert eink_display._sanitize_row_value("Aires,\tArgentina") == "Aires, Argentina"
        assert eink_display._sanitize_row_value("Sao\xa0Paulo") == "Sao Paulo"
        assert eink_display._sanitize_row_value("Buenos  Aires,\tArgentina") == "Buenos Aires, Argentina"
        assert eink_display._sanitize_row_value("Austin, Texas") == "Austin, Texas"
        assert eink_display._sanitize_row_value(None) == ""
        assert eink_display._sanitize_row_value(12345) == ""

        # and end-to-end: nothing hostile survives into the rendered panel
        hostile = {
            "has_location": True,
            "location_name": "A‮BCD​\x00",
            "timezone": "Europe/‮London",
            "units_label": "Metric (°C)",
            "mature_enabled": False,
            "connected_ssid": "cooper-iot",
        }
        assert eink_display.create_handoff_splash_image(hostile, "http://192.168.2.134").size == (
            eink_display.DISPLAY_SIZE
        )

    def test_a_hostile_or_non_string_units_label_cannot_break_the_splash(self):
        """units_label reaches the rows from the same --settings-json payload as
        location_name, but was the one row value left unsanitized. A non-string
        (json.loads yields ints and dicts happily) raised TypeError out of
        _collapse_newlines and killed the render; a bidi override rendered as-is.
        """
        base = {
            "has_location": True,
            "location_name": "Austin, Texas",
            "timezone": "America/Chicago",
            "mature_enabled": False,
            "connected_ssid": "cooper-iot",
        }
        for hostile in (123, {"a": 1}, ["x"], None, "", "Imperial‮ (°F)", "Imperial\x00 (°F)"):
            img = eink_display.create_handoff_splash_image({**base, "units_label": hostile}, "http://192.168.2.134")
            assert img.size == eink_display.DISPLAY_SIZE, f"splash died on units_label={hostile!r}"

        assert eink_display._sanitize_row_value("Imperial‮ (°F)") == "Imperial (°F)"

    def test_a_long_qr_url_cannot_bleed_into_the_settings_column(self):
        """The URL caption under the QR was drawn with an unclamped centred
        draw.text, so `qr_x + (qr_size - w) // 2` went negative for a wide
        string and the caption ran the full width of the panel, straight
        through the settings rows. Same bleed this PR fixes for the row values,
        on the sibling line — and qr_url is a raw positional CLI argument, not
        even wrapped in the settings JSON.
        """
        settings = {
            "has_location": True,
            "location_name": "Austin, Texas",
            "timezone": "America/Chicago",
            "units_label": "Metric (°C)",
            "mature_enabled": False,
            "connected_ssid": "cooper-iot",
        }
        rows_band = (0, 190, eink_display.HANDOFF_RIGHT_COLUMN_X, 340)
        normal = eink_display.create_handoff_splash_image(settings, "http://192.168.2.134")
        normal_ink = ImageOps.invert(normal.crop(rows_band).convert("L")).getbbox()

        for hostile_url in (
            "http://" + "8" * 200,
            "http://litclock-" + "x" * 120 + ".local",
            "http://" + ".".join(["255"] * 40),
        ):
            img = eink_display.create_handoff_splash_image(settings, hostile_url)
            ink = ImageOps.invert(img.crop(rows_band).convert("L")).getbbox()
            assert ink == normal_ink, (
                f"a long qr_url changed the settings-column pixels ({ink} vs {normal_ink}) — "
                f"the caption bled out of the right column"
            )

    def test_mature_quotes_is_not_inverted_by_a_json_string(self):
        """bool("false") is True, so bare truthiness would render "On" for a
        flag delivered as a JSON string — silently inverting the one row that
        tells the user whether mature content is enabled."""
        assert eink_display._as_bool("false") is False
        assert eink_display._as_bool("False") is False
        assert eink_display._as_bool("0") is False
        assert eink_display._as_bool("") is False
        assert eink_display._as_bool("true") is True
        assert eink_display._as_bool(True) is True
        assert eink_display._as_bool(False) is False
        assert eink_display._as_bool(None) is False

        # End-to-end, or the helper can be right while the call site is not:
        # the rendered "false" splash must match the real-False splash and
        # differ from the real-True one.
        base = {
            "has_location": True,
            "location_name": "Austin, Texas",
            "timezone": "America/Chicago",
            "units_label": "Metric (°C)",
            "connected_ssid": "cooper-iot",
        }

        def render(flag):
            return eink_display.create_handoff_splash_image(
                {**base, "mature_enabled": flag}, "http://192.168.2.134"
            ).tobytes()

        assert render("false") == render(False), 'mature_enabled="false" rendered as On'
        assert render("true") == render(True)
        assert render(False) != render(True), "premise: the two states must render differently"

    def test_a_font_without_metrics_logs_rather_than_shifting_silently(self, caplog):
        """journald is the only diagnostic channel on this device, so the
        baseline fallback must leave a trail. It must also approximate the
        ASCENT — the declared size is 22 where the real ascent is 26, so using
        `size` sits the whole settings block 4px high."""

        class NoMetrics:
            size = 22

            def getmetrics(self):
                raise OSError("no metrics")

        with caplog.at_level("ERROR"):
            out = eink_display._row_baseline(100, NoMetrics())
        assert any("metrics" in r.message or "metrics" in r.getMessage() for r in caplog.records), (
            "silent fallback — no diagnostic reached the log"
        )
        real = eink_display._row_baseline(100, ImageFont.truetype(eink_display.FONT_PATH, 22))
        assert abs(out - real) <= 2, f"fallback baseline {out} is far from the real one {real}"


class TestQrScreenTextClamps:
    """create_qr_display_image centres three strings across the full panel with
    no bound — the same shape as the handoff splash's URL caption fixed in
    litclock-dev#640, found by sweeping every draw site for that pattern.

    Not a live overflow: the only shipped caller is first-boot.sh's
    `display_qr "$SETUP_URL" "Scan to Setup" "Open on your phone"`, and both
    text arguments are English literals. It is a CLI surface
    (`eink_display.py qr <url> --title T --caption C`) and litclock-dev#532
    exposure — every one of those literals gets longer when translated.

    The URL was the real defect: it was guarded at 60 CHARACTERS, which is a
    pixel guard in disguise. 57 wide glyphs plus an ellipsis measure 1023px at
    18pt against a 760px budget, and a plausible long hostname
    ("http://" + "W" * 38 + ".local", 51 chars — under the old character
    limit) measures 768.6px, already 8.6px over. Both figures are asserted
    below rather than asserted in prose.
    """

    URL = "https://192.168.2.134:8443"

    # Y-bands the QR block cannot reach, so each assertion's observation window
    # contains its own subject and nothing else.
    #
    # The first version of this class measured the WHOLE image. That window
    # contains the 280px QR block, whose ink at x 260..540 satisfies the gutter
    # assertion all by itself — so every test here passed with all three clamps
    # replaced by "". A splash that silently drops "Scan to Setup" and the IP
    # the user has to type is a worse outcome than the overflow this class was
    # written to catch, and the guard could not see it.
    #
    # test_the_bands_exclude_the_qr_block pins the premise; do not widen these
    # without re-running it.
    TITLE_BAND = (0, 0, 800, 88)
    CAPTION_BAND = (0, 384, 800, 436)
    URL_BAND = (0, 438, 800, 480)

    def _ink(self, img, band=None):
        if band is not None:
            img = img.crop(band)
        ink = ImageOps.invert(img.convert("L")).getbbox()
        if ink is None or band is None:
            return ink
        return (ink[0] + band[0], ink[1] + band[1], ink[2] + band[0], ink[3] + band[1])

    def _assert_drawn_inside_the_gutters(self, img, band, what):
        """Assert the text IS there, and that it clears both gutters.

        Presence first: a clamp that over-truncates to "" is invisible to any
        bounds-only assertion, and is the failure mode this surface can least
        afford.

        Then bounds — and NOT `ink[2] <= 800`. PIL clips at the image edge, so
        that can never fail on an 800px canvas and passes with the clamp
        deleted, the trap test_handoff_row_ink_stays_on_the_panel documents.
        Unclamped centred text has a NEGATIVE start x and bleeds off BOTH
        edges, so its clipped ink runs 0..800 exactly. Requiring a real margin
        on each side is what distinguishes the two.

        The 1px tolerance matches test_status_margins_are_asserted_against_the
        _declared_budget, and is not slack for its own sake: the clamp measures
        with textlength (advance width) while placement centres on textbbox
        (ink extents), so a glyph with a negative left bearing lands one pixel
        further left than the budget accounts for. Measured: a title of "y"*200
        clamps correctly and still inks at x=19. "W" has no negative bearing,
        which is the only reason a strict bound passed here before.
        """
        ink = self._ink(img, band)
        assert ink is not None, f"{what}: nothing drawn in its band — the clamp emptied the string"
        margin = eink_display.STATUS_SIDE_MARGIN
        assert ink[0] >= margin - 1, f"{what} ink starts at x={ink[0]}, inside the {margin}px gutter: {ink}"
        assert ink[2] <= eink_display.DISPLAY_SIZE[0] - margin + 1, (
            f"{what} ink reaches x={ink[2]}, inside the {margin}px right gutter: {ink}"
        )

    def test_the_bands_exclude_the_qr_block(self):
        """Premise for every band assertion in this class.

        Rendering with no title and no caption leaves only the QR and the URL.
        If the title and caption bands are empty in THAT image, the QR reaches
        neither — and since CAPTION_BAND ends at 436, below URL_BAND's 438, it
        cannot reach the URL band either. Without this, the bands are three
        magic numbers and a hope.
        """
        img = eink_display.create_qr_display_image(self.URL)
        assert self.CAPTION_BAND[3] <= self.URL_BAND[1], "the bands overlap; the proof below does not carry"
        for name, band in (("title", self.TITLE_BAND), ("caption", self.CAPTION_BAND)):
            assert self._ink(img, band) is None, (
                f"the {name} band contains QR ink — this class's observation window "
                f"no longer excludes its non-subject: {self._ink(img, band)}"
            )

    def test_the_budget_is_pinned_to_the_panel(self):
        """Guards the WIDE direction's own constant.

        Every other assertion here is written in terms of PANEL_TEXT_BUDGET, so
        shrinking that constant satisfies all of them while ellipsising real
        copy on a shipped surface. Verified: PANEL_TEXT_BUDGET = 300 left the
        whole eink suite green. An earlier comment in this class claimed
        test_status_side_margin_is_a_real_margin closed that hole — it does
        not; it pins a LOWER bound on the MARGIN, which constrains the budget
        in the wrong direction entirely.
        """
        assert eink_display.PANEL_TEXT_BUDGET == (eink_display.DISPLAY_SIZE[0] - 2 * eink_display.STATUS_SIDE_MARGIN), (
            "the budget is no longer the panel minus its two gutters"
        )
        assert eink_display.PANEL_TEXT_BUDGET >= 700, (
            f"budget shrank to {eink_display.PANEL_TEXT_BUDGET}px; shipped copy would start truncating"
        )

    def test_a_wide_title_stays_inside_the_gutters(self):
        self._assert_drawn_inside_the_gutters(
            eink_display.create_qr_display_image(self.URL, title="W" * 60), self.TITLE_BAND, "title"
        )

    def test_a_wide_caption_stays_inside_the_gutters(self):
        self._assert_drawn_inside_the_gutters(
            eink_display.create_qr_display_image(self.URL, caption="W" * 60), self.CAPTION_BAND, "caption"
        )

    def test_a_negative_left_bearing_still_clears_the_gutter(self):
        """'y' at 36pt bold has a negative left bearing, so it inks one pixel
        left of where textlength says it starts. Pins the tolerance above to a
        real glyph rather than leaving it as unexplained slack."""
        self._assert_drawn_inside_the_gutters(
            eink_display.create_qr_display_image(self.URL, title="y" * 200), self.TITLE_BAND, "title"
        )

    def test_a_long_url_is_measured_not_counted(self):
        """The character-count guard passed 60 chars regardless of glyph width.
        Uses wide glyphs so a count-based guard cannot save it."""
        draw = ImageDraw.Draw(Image.new("1", eink_display.DISPLAY_SIZE, 255))
        small = ImageFont.truetype(eink_display.FONT_PATH, eink_display.QR_URL_FONT_PT)
        hostile = "http://" + "W" * 38 + ".local"
        assert len(hostile) < 60, "premise: must be under the OLD character limit"
        width = draw.textlength(hostile, font=small)
        assert width > eink_display.PANEL_TEXT_BUDGET, (
            f"premise: {width:.1f}px must exceed the {eink_display.PANEL_TEXT_BUDGET}px budget, or this proves nothing"
        )
        assert 765 < width < 772, f"the 768.6px figure in the docstring and the source comment drifted to {width:.1f}"
        self._assert_drawn_inside_the_gutters(eink_display.create_qr_display_image(hostile), self.URL_BAND, "url")

    def test_the_old_character_limit_measures_far_over(self):
        """Pins the 1023px figure the source comment cites for 57 wide glyphs
        plus the ellipsis — the number that makes 60-characters indefensible."""
        draw = ImageDraw.Draw(Image.new("1", eink_display.DISPLAY_SIZE, 255))
        small = ImageFont.truetype(eink_display.FONT_PATH, eink_display.QR_URL_FONT_PT)
        width = draw.textlength("W" * 57 + "…", font=small)
        assert 1020 < width < 1027, f"the 1023px figure in the source comment drifted to {width:.1f}"

    def test_the_shipped_strings_never_reach_the_clamp(self):
        """first-boot.sh's actual call must pass through untouched.

        Asserts the clamp is a no-op on all THREE shipped strings, the URL
        included — the previous version measured only the two text arguments
        and skipped 'http://litclock.setup', which is the one first-boot.sh
        actually passes. It also claimed to prove byte-identical rendering
        while asserting only `img.size == DISPLAY_SIZE`, which is unfalsifiable
        because the function's first statement is Image.new("1", DISPLAY_SIZE).
        """
        draw = ImageDraw.Draw(Image.new("1", eink_display.DISPLAY_SIZE, 255))
        shipped = (
            ("Scan to Setup", eink_display.FONT_PATH_BOLD, eink_display.QR_TITLE_FONT_PT),
            ("Open on your phone", eink_display.FONT_PATH, eink_display.QR_CAPTION_FONT_PT),
            ("http://litclock.setup", eink_display.FONT_PATH, eink_display.QR_URL_FONT_PT),
        )
        for text, path, size in shipped:
            font = ImageFont.truetype(path, size)
            out = eink_display._clamp_to_width(text, font, draw, eink_display.PANEL_TEXT_BUDGET, "t", "qr splash")
            assert out == text, f"the clamp now fires on shipped copy: {text!r} -> {out!r}"

        # And the real render still puts all three on the panel.
        img = eink_display.create_qr_display_image(
            "http://litclock.setup", title="Scan to Setup", caption="Open on your phone"
        )
        for name, band in (("title", self.TITLE_BAND), ("caption", self.CAPTION_BAND), ("url", self.URL_BAND)):
            self._assert_drawn_inside_the_gutters(img, band, name)

    def test_truncation_is_announced_on_the_right_surface(self, caplog):
        """journald is the only diagnostic channel on this device, and
        _clamp_to_width takes a `surface` argument precisely because a warning
        naming the wrong screen costs real time. Nothing asserted that these
        three call sites pass 'qr splash', so swapping it back to the default
        'setup splash' was a silent, green mutation."""
        with caplog.at_level(logging.WARNING):
            eink_display.create_qr_display_image(self.URL, title="W" * 60, caption="W" * 60)
        assert "qr splash qr title too wide" in caplog.text, caplog.text
        assert "qr splash qr caption too wide" in caplog.text, caplog.text

    def test_a_title_that_collapses_to_nothing_is_not_silent(self, caplog):
        """A newline-only title reduces to "" and draws nothing. That is the
        right render; being silent about it was not. On this surface the
        sanitize pass now reports it (newlines are control characters), so the
        title vanishes WITH a journald line rather than without one."""
        with caplog.at_level(logging.WARNING):
            img = eink_display.create_qr_display_image(self.URL, title="\n\n")
        assert "qr splash title contained control characters" in caplog.text, caplog.text
        assert self._ink(img, self.TITLE_BAND) is None

    def test_clamp_announces_a_newline_collapse_on_unsanitized_surfaces(self, caplog):
        """_clamp_to_width reassigned `text` to the collapsed value before its
        `result != text` check, so a collapse could never be reported: a "\\n"
        argument returned "" with nothing logged. The QR splash no longer
        reaches that path (it sanitizes first), but the handoff splash does —
        `eink_display.py handoff-splash --settings-json` json.loads arbitrary
        input straight into these fields."""
        draw = ImageDraw.Draw(Image.new("1", eink_display.DISPLAY_SIZE, 255))
        font = ImageFont.truetype(eink_display.FONT_PATH, eink_display.QR_URL_FONT_PT)
        with caplog.at_level(logging.WARNING):
            out = eink_display._clamp_to_width("a\nb", font, draw, eink_display.PANEL_TEXT_BUDGET, "f", "handoff")
        assert out == "a b"
        assert "handoff f contained line breaks" in caplog.text, caplog.text

    def test_control_characters_are_stripped_like_the_sibling_splash(self, caplog):
        """_sanitize_render_text has guarded the setup splash since litclock-dev#589; this
        surface takes the same CLI-supplied strings and did not call it."""
        with caplog.at_level(logging.WARNING):
            eink_display.create_qr_display_image(self.URL, title="Scan\x07\x00 Me")
        assert "qr splash title contained control characters" in caplog.text, caplog.text

    def test_a_pathological_argument_does_not_hang(self):
        """The clamp replaced an O(1) character cut with a measure-and-delete
        loop that is O(n²) unaided: 8.6s for 8000 chars on a dev box, and a Pi
        Zero 2W is 10-20x slower against first-boot.sh's `timeout 20`. Pins
        _halve_until_close, and pins that it does not change the answer."""
        draw = ImageDraw.Draw(Image.new("1", eink_display.DISPLAY_SIZE, 255))
        small = ImageFont.truetype(eink_display.FONT_PATH, eink_display.QR_URL_FONT_PT)
        budget = eink_display.PANEL_TEXT_BUDGET

        start = time.monotonic()
        out = eink_display._clamp_to_width("x" * 20000, small, draw, budget, "u", "qr splash")
        elapsed = time.monotonic() - start
        assert elapsed < 2.0, f"clamping 20k chars took {elapsed:.1f}s — the pre-cut is not working"
        assert draw.textlength(out, font=small) <= budget

        # Identical result to the unaided loop, on a length where running the
        # unaided loop is still cheap.
        for n in (100, 300, 900):
            fast = eink_display._clamp_to_width("x" * n, small, draw, budget, "u", "qr splash")
            slow = "x" * n
            while slow and draw.textlength(slow + "…", font=small) > budget:
                slow = slow[:-1]
            assert fast == slow + "…", f"the pre-cut changed the answer at n={n}: {fast!r} != {slow + '…'!r}"

    def test_the_clamps_hold_on_the_fallback_font(self, monkeypatch, caplog):
        """The `except` branch in create_qr_display_image swaps all three fonts
        for ImageFont.load_default(size=...), and nothing exercised it — so
        every clamp assertion in this class only ever ran against Literata.

        It matters that this branch is measured, not assumed: the clamp is a
        pixel bound, so it is only as good as the metrics of whatever font is
        actually in play. load_default(size=) returns an embedded Aileron as a
        real FreeTypeFont, so textlength works and the clamps do hold — but
        Aileron is NARROWER than Literata ('W'*60 is 2040px against 2297.8px),
        which is exactly the kind of difference that would move ink relative to
        a fixed band if the bands had been chosen against one font by accident.

        Note the handler is not freetype-free: load_default(size=) reaches
        truetype() internally, the same call that failed to get here. A Pillow
        built without freetype would raise inside the except block rather than
        degrade. Not guarded, because the primary path has the same hard
        dependency and requirements.txt pins pillow==12.3.0, which bundles it.
        """
        monkeypatch.setattr(eink_display, "FONT_PATH", "/nonexistent/Missing.ttf")
        monkeypatch.setattr(eink_display, "FONT_PATH_BOLD", "/nonexistent/MissingBold.ttf")
        with caplog.at_level(logging.ERROR):
            img = eink_display.create_qr_display_image(
                "http://" + "W" * 200 + ".local", title="W" * 200, caption="W" * 200
            )
        assert "fonts unavailable" in caplog.text, "the fallback branch was not taken; this test proves nothing"
        for name, band in (("title", self.TITLE_BAND), ("caption", self.CAPTION_BAND), ("url", self.URL_BAND)):
            self._assert_drawn_inside_the_gutters(img, band, f"{name} (fallback font)")


class TestStatusBlockFitLadder:
    """litclock-dev#532 Stage 3 (scope-audit blocker, second half): the
    message/submessage blocks prefer a smaller font over a shorter string —
    the _fit_row_text primitive applied to status blocks. The per-line
    ellipsis clamp survives only as the floor for pathological input."""

    def _draw(self):
        img = Image.new("1", (1, 1))
        return ImageDraw.Draw(img)

    def _font(self, pt):
        return ImageFont.truetype(eink_display.FONT_PATH, pt)

    def test_fitting_text_returns_byte_identical_at_base_font(self):
        draw = self._draw()
        font = self._font(28)
        out, out_font = eink_display._fit_block_to_panel(
            "Detecting your location...", font, draw, "t", eink_display.STATUS_MESSAGE_FONT_FLOOR
        )
        assert out == "Detecting your location..."
        assert out_font is font, "a fitting string must keep the caller's font object"

    def test_shrinkable_text_stays_complete_at_smaller_font(self):
        # THE behavioral change: pre-ladder this string lost its tail to an
        # ellipsis; now it shrinks and stays complete. Built to overflow 28pt
        # but fit within the floor — locale-length copy, not pathology.
        draw = self._draw()
        font = self._font(28)
        text = "Verbindung zum WLAN wird hergestellt und der Standort erkannt..."
        assert draw.textlength(text, font=font) > eink_display.PANEL_TEXT_BUDGET, (
            "fixture must overflow at 28pt or this test pins nothing"
        )
        out, out_font = eink_display._fit_block_to_panel(
            text, font, draw, "t", eink_display.STATUS_MESSAGE_FONT_FLOOR
        )
        assert out == text, "a shrinkable block must not be truncated"
        assert out_font.size < 28
        assert out_font.size >= eink_display.STATUS_MESSAGE_FONT_FLOOR
        assert draw.textlength(text, font=out_font) <= eink_display.PANEL_TEXT_BUDGET

    def test_pathological_text_clamps_at_the_floor(self):
        draw = self._draw()
        font = self._font(28)
        out, out_font = eink_display._fit_block_to_panel(
            "X" * 400, font, draw, "t", eink_display.STATUS_MESSAGE_FONT_FLOOR
        )
        assert out_font.size == eink_display.STATUS_MESSAGE_FONT_FLOOR
        assert out.endswith("…")
        assert draw.textlength(out, font=out_font) <= eink_display.PANEL_TEXT_BUDGET

    def test_multiline_widest_line_governs_and_newlines_survive(self):
        draw = self._draw()
        font = self._font(20)
        wide = "W" * 60
        text = f"short line\n{wide}\nanother short line"
        out, out_font = eink_display._fit_block_to_panel(
            text, font, draw, "t", eink_display.STATUS_SUBMESSAGE_FONT_FLOOR
        )
        assert out.count("\n") == 2, "embedded newlines must survive the ladder"
        for line in out.split("\n"):
            assert draw.textlength(line, font=out_font) <= eink_display.PANEL_TEXT_BUDGET

    def test_wiring_message_shrinks_instead_of_truncating(self, caplog):
        """/review litclock-dev#734 Finding 1: the rendered-margin test alone is satisfied
        by the CLAMP too (it also keeps ink inside margins — that's its job),
        so the integration wiring was mutation-survivable. The discriminator:
        the clamp LOGS "too wide for the panel; truncated to fit"; the ladder
        is silent. Reverting the wiring turns this red (mutant-verified)."""
        import logging as logging_mod

        text = "Verbindung zum WLAN wird hergestellt und der Standort erkannt..."
        draw = self._draw()
        assert draw.textlength(text, font=self._font(28)) > eink_display.PANEL_TEXT_BUDGET
        floor_font = self._font(eink_display.STATUS_MESSAGE_FONT_FLOOR)
        assert draw.textlength(text, font=floor_font) <= eink_display.PANEL_TEXT_BUDGET
        with caplog.at_level(logging_mod.WARNING):
            img = eink_display.create_status_image("", message=text)
        joined = " ".join(str(rec.msg) for rec in caplog.records)
        assert "truncated to fit" not in joined, (
            "the message block was clamped at base size — the fit-ladder wiring is gone"
        )
        assert "collapsed to one line" not in joined
        # And the ink's ROW extent must match a shrunk font: at 28pt this
        # string cannot fit, so a silent base-size render is also caught.
        import numpy as np

        arr = np.array(img)
        ink_cols = np.where((arr == 0).any(axis=0))[0]
        assert len(ink_cols) > 0

    def test_wiring_submessage_shrinks_instead_of_truncating(self, caplog):
        # The submessage half had no shrink-path coverage at any level
        # (/review litclock-dev#734): its only prior integration fixtures were
        # pathological clamp cases.
        import logging as logging_mod

        draw = self._draw()
        base = self._font(20)
        text = "Bitte stecken Sie das Netzkabel aus und danach wieder ein, um die Einrichtung fortzusetzen"
        assert draw.textlength(text, font=base) > eink_display.PANEL_TEXT_BUDGET, (
            "fixture must overflow at 20pt or this pins nothing"
        )
        floor_font = self._font(eink_display.STATUS_SUBMESSAGE_FONT_FLOOR)
        assert draw.textlength(text, font=floor_font) <= eink_display.PANEL_TEXT_BUDGET, (
            "fixture must FIT at the floor or the clamp fires legitimately"
        )
        with caplog.at_level(logging_mod.WARNING):
            eink_display.create_status_image("", submessage=text)
        joined = " ".join(str(rec.msg) for rec in caplog.records)
        assert "truncated to fit" not in joined, (
            "the submessage block was clamped — the fit-ladder wiring is gone"
        )

    def test_rendered_status_image_keeps_margins_with_long_message(self):
        # Guard-observation-window lesson: assert on the RENDERED image, and
        # on an image whose only ink is the text under test — a whole-image
        # bbox on a busier layout can be satisfied by other elements.
        import numpy as np

        text = "Verbindung zum WLAN wird hergestellt und der Standort erkannt..."
        img = eink_display.create_status_image("", message=text)
        arr = np.array(img)
        ink_cols = np.where((arr == 0).any(axis=0))[0]
        assert len(ink_cols) > 0, "no ink rendered — the message vanished"
        assert ink_cols.min() >= eink_display.STATUS_SIDE_MARGIN - 2
        assert ink_cols.max() < eink_display.DISPLAY_SIZE[0] - eink_display.STATUS_SIDE_MARGIN + 2
