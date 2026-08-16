#!/usr/bin/env python3
"""
E-ink Display Utility for LitClock

Provides functions to display QR codes and status messages on the e-paper display.
Used during setup and provisioning.
"""

import argparse
import logging
import os
import sys

from PIL import Image, ImageDraw, ImageFont

from captive_portal import SETUP_HOSTNAME
from log import setup_logging

# Try to import qrcode, provide helpful message if not installed
try:
    import qrcode
except ImportError:
    print("Error: qrcode library not installed")
    print("Install with: pip install qrcode[pil]")
    sys.exit(1)

# Configure logging
setup_logging()

# Constants
DISPLAY_SIZE = (800, 480)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT_PATH = os.path.join(PROJECT_ROOT, "fonts", "Literata72pt-Regular.ttf")
FONT_PATH_BOLD = os.path.join(PROJECT_ROOT, "fonts", "Literata72pt-Black.ttf")

# Side gutter for ANY text centred across the full panel, and the single text
# budget derived from it. Two surfaces share this: the status splash
# (_clamp_block_to_panel) and the QR splash (create_qr_display_image). Both
# centre a single line across the whole 800px canvas, so both need the same
# bound — and there must be exactly ONE derivation of it, or the two surfaces
# silently disagree after a tweak to either.
#
# It lives in the shared Constants block because it now belongs to neither
# consumer. It does NOT live here for an import-order reason: a module-level
# name read from inside a function body resolves at CALL time, so the old
# placement further down the module was never a NameError. Only the derived
# assignment below has to follow its operand.
#
# STATUS_SIDE_MARGIN keeps its name despite no longer being status-specific;
# renaming it would churn five status tests that name the symbol for reasons
# unrelated to the QR splash.
STATUS_SIDE_MARGIN = 20
PANEL_TEXT_BUDGET = DISPLAY_SIZE[0] - 2 * STATUS_SIDE_MARGIN

# Point sizes the QR splash draws at. Hoisted for the same reason as
# SETUP_SMALL_FONT_PT below: a hardcoded 36/24/18 in a test silently validates
# the wrong font after a size tweak here (litclock-dev#629 review).
QR_TITLE_FONT_PT = 36
QR_CAPTION_FONT_PT = 24
QR_URL_FONT_PT = 18

# Hotspot info screen layout (normal + retry variants). Both variants stack
# their instruction block from the bottom up with these constants so the
# 3-line retry screen and 4-line normal screen sit flush at the same
# baseline. Promoted from function-local vars so future layout tweaks have
# one place to change.
HOTSPOT_INFO_LINE_HEIGHT = 28
# Point size of the setup splash's small text (framing line + numbered steps).
# Hoisted so the step-line clamp tests measure with the SAME size production
# draws at — a hardcoded 18 in the test would silently validate the wrong
# font after a size tweak here (litclock-dev#629 review).
SETUP_SMALL_FONT_PT = 18
HOTSPOT_INFO_BOTTOM_PADDING = 20

# QR block geometry for the setup splash. Module level for the same reason
# as the two above, plus one specific to the QR: a test that re-hardcodes
# these ends up asserting a blank region of the panel and keeps passing
# after the QR moves, which is exactly the regression a quiet-zone guard
# exists to catch.
SETUP_QR_X = 40
SETUP_QR_Y = 80
SETUP_QR_SIZE = 220

# ISO 18004 wants 4 modules of blank around the symbol. generate_qr_image
# bakes 2 of them in via border=2, so the panel has to supply the other 2.
# Module size falls out of the QR version, which falls out of how long the
# credentials are — and the WORST case is the SHORTEST payload, because
# fewer modules across a fixed 220px block makes each one bigger. Empty
# ssid and password give 25 modules (29 with border) at 7.59px each, so
# 2 modules is 15.2px. 16 covers every credential length we can generate.
#
# Do not replace this with a measured margin taken from one set of
# credentials: dev#... cb9d9781 already made the settings QR's quiet zone
# structural for this reason, and a point-in-time number reintroduces the
# bug it fixed.
SETUP_QR_QUIET_ZONE_PX = 16

# The framing line sits in the band under the QR, clear of its quiet zone.
# Derived, not measured, so moving or resizing the QR moves it too.
SETUP_FRAMING_Y = SETUP_QR_Y + SETUP_QR_SIZE + SETUP_QR_QUIET_ZONE_PX


def instruction_block_top(line_count: int) -> int:
    """Y of the TOPMOST instruction line for a block of ``line_count`` lines.

    The block stacks bottom-up from the panel edge, so adding a line moves
    this value UP, toward the framing line and the QR. Shared with the tests
    so the clearance assertion cannot drift away from what the renderer
    actually does.
    """
    return DISPLAY_SIZE[1] - HOTSPOT_INFO_BOTTOM_PADDING - line_count * HOTSPOT_INFO_LINE_HEIGHT


def get_display():
    """Get the e-paper display object. Returns None if not available."""
    try:
        from display_driver import epd7in5

        epd = epd7in5.EPD()
        return epd
    except Exception as e:
        logging.warning(f"Could not initialize display: {e}")
        return None


def generate_qr_image(data: str, box_size: int = 10, border: int = 2) -> Image.Image:
    """Generate a QR code image from data."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=box_size,
        border=border,
    )
    qr.add_data(data)
    qr.make(fit=True)

    # Create QR code image (black on white)
    qr_image = qr.make_image(fill_color="black", back_color="white")
    return qr_image.convert("1")


def create_qr_display_image(url: str, title: str = None, caption: str = None, qr_size: int = 280) -> Image.Image:
    """
    Create a full display image with QR code, title, and caption.

    Args:
        url: URL or data to encode in QR code
        title: Large text above QR code (optional)
        caption: Smaller text below QR code (optional)
        qr_size: Size of QR code in pixels

    Returns:
        PIL Image ready for display
    """
    # Create white background
    image = Image.new("1", DISPLAY_SIZE, 255)
    draw = ImageDraw.Draw(image)

    # Load fonts
    try:
        title_font = ImageFont.truetype(FONT_PATH_BOLD, QR_TITLE_FONT_PT)
        caption_font = ImageFont.truetype(FONT_PATH, QR_CAPTION_FONT_PT)
        small_font = ImageFont.truetype(FONT_PATH, QR_URL_FONT_PT)
    except Exception as e:
        # size= + log, never a silent 10px collapse (litclock-dev#589 item 3).
        logging.error("fonts unavailable (%s); using scaled default", e)
        title_font = ImageFont.load_default(size=QR_TITLE_FONT_PT)
        caption_font = ImageFont.load_default(size=QR_CAPTION_FONT_PT)
        small_font = ImageFont.load_default(size=QR_URL_FONT_PT)

    # Calculate positions
    qr_x = (DISPLAY_SIZE[0] - qr_size) // 2
    qr_y = 100  # Starting Y position for QR code

    # Draw title if provided. Sanitize first, for the same reason the setup
    # splash does (litclock-dev#589): this is a CLI surface
    # (`eink_display.py qr <url> --title T --caption C`), so a control
    # character in an argument would otherwise reach draw.text. The sibling
    # splash has stripped these since #589; this one never did.
    if title:
        title = _sanitize_render_text(title, "title", surface="qr splash")
        title = _clamp_to_width(title, title_font, draw, PANEL_TEXT_BUDGET, "qr title", surface="qr splash")
        bbox = draw.textbbox((0, 0), title, font=title_font)
        title_width = bbox[2] - bbox[0]
        title_x = (DISPLAY_SIZE[0] - title_width) // 2
        draw.text((title_x, 30), title, font=title_font, fill=0)
        qr_y = 90

    # Generate and paste QR code
    qr_image = generate_qr_image(url)
    qr_image = qr_image.resize((qr_size, qr_size), Image.Resampling.NEAREST)
    image.paste(qr_image, (qr_x, qr_y))

    # Draw caption if provided
    if caption:
        caption = _sanitize_render_text(caption, "caption", surface="qr splash")
        caption = _clamp_to_width(caption, caption_font, draw, PANEL_TEXT_BUDGET, "qr caption", surface="qr splash")
        bbox = draw.textbbox((0, 0), caption, font=caption_font)
        caption_width = bbox[2] - bbox[0]
        caption_x = (DISPLAY_SIZE[0] - caption_width) // 2
        caption_y = qr_y + qr_size + 20
        draw.text((caption_x, caption_y), caption, font=caption_font, fill=0)

    # Draw URL in small text at bottom. The old guard truncated at 60
    # CHARACTERS, which is a pixel guard in disguise and does not hold. Every
    # figure below is measured at QR_URL_FONT_PT against PANEL_TEXT_BUDGET
    # (760px), which is the bound that actually applies — not the 800px panel:
    #   'W' * 57 + '…'                  1023.4px   (the old limit, 4.3x over)
    #   'http://' + 'W' * 38 + '.local'  768.6px   (51 chars, under the old
    #                                               character limit, still 8.6px
    #                                               over the pixel budget)
    # Measure instead. tests/test_eink_wrap.py pins both numbers.
    url_display = _clamp_to_width(url, small_font, draw, PANEL_TEXT_BUDGET, "qr url", surface="qr splash")
    bbox = draw.textbbox((0, 0), url_display, font=small_font)
    url_width = bbox[2] - bbox[0]
    url_x = (DISPLAY_SIZE[0] - url_width) // 2
    draw.text((url_x, DISPLAY_SIZE[1] - 40), url_display, font=small_font, fill=0)

    return image


# Gift-mode title layout (litclock-dev#319). Two lines max so a personalized welcome
# can read naturally across the 800×480 canvas without falling off either
# edge; a 1-line single-word title still centers fine because the wrapper
# returns it unchanged. Horizontal margin keeps the wrapped text away from
# the bezel; 40px each side gives 720px of usable width. Ellipsis suffix
# is appended when the message cannot fit in MAX_TITLE_LINES at the title
# font size — better than mid-word truncation because the recipient can
# tell at a glance that more text was intended.
TITLE_SIDE_MARGIN = 40
MAX_TITLE_LINES = 2
TITLE_LINE_SPACING = 4
ELLIPSIS = "…"

# Auto-fit ladder for the welcome/status title (gift-message litclock-dev#280 truncation
# fix). A personalized gift message must SHRINK to fit, never lose its tail to
# an ellipsis — silently cutting "…a good time to read!" off someone's present
# is the one place truncation is unacceptable. Each tier is (font_size,
# max_lines); tried largest-first, the first tier whose natural wrap fits the
# line budget wins. The top tier is the historical 48pt/2-line look, so short
# greetings render byte-identically to before. Only the final tier permits an
# ellipsis, for a message longer than ~4 lines at 28pt (well past the 280-char
# input cap in practice). Envelope check: 4 lines @ 28pt ≈ 150px, which clears
# the setup-steps block below even at the gift layout's title_y=60.
TITLE_FIT_TIERS = ((48, 2), (44, 3), (38, 3), (32, 4), (28, 4))


def _wrap_title(text: str, font: ImageFont.FreeTypeFont, max_width: int, max_lines: int) -> list[str]:
    """Word-wrap ``text`` into at most ``max_lines`` lines whose pixel width
    fits within ``max_width`` when rendered with ``font``. Explicit ``\n``
    breaks are honored as hard line boundaries. If a single word is wider
    than ``max_width`` it is force-broken at the character level so the
    rest of the title still has somewhere to land. When the result would
    overflow ``max_lines``, the last kept line is shortened with an
    ellipsis suffix so the truncation reads intentionally.

    Returns an empty list for empty input.
    """
    if not text:
        return []
    measure = ImageDraw.Draw(Image.new("1", (1, 1))).textbbox
    # Drop empty paragraphs — leading, trailing, OR internal — so a user
    # typing "\n\nMom!" doesn't burn the max_lines budget on blank lines
    # and then get "Mom!" ellipsis-truncated away (adversarial /review).
    # The e-ink layout reserves the full title area; honoring blank-line
    # spacing inside the title block would just truncate real text on
    # the recipient end.
    paragraphs = [p for p in text.split("\n") if p]
    lines: list[str] = []

    def fits(s: str) -> bool:
        bbox = measure((0, 0), s, font=font)
        return (bbox[2] - bbox[0]) <= max_width

    for paragraph in paragraphs:
        current = ""
        for word in paragraph.split(" "):
            candidate = word if not current else f"{current} {word}"
            if fits(candidate):
                current = candidate
                continue
            if current:
                lines.append(current)
                current = ""
            # Word alone exceeds max_width — char-break it.
            if not fits(word):
                buf = ""
                for ch in word:
                    if fits(buf + ch):
                        buf += ch
                    else:
                        if buf:
                            lines.append(buf)
                        buf = ch
                current = buf
            else:
                current = word
        if current:
            lines.append(current)

    if len(lines) <= max_lines:
        return lines

    kept = lines[:max_lines]
    overflow_tail = kept[-1]
    # Append ellipsis; trim characters off the tail until it fits again.
    candidate = overflow_tail.rstrip() + ELLIPSIS
    while candidate and not fits(candidate):
        # Drop one character (before the ellipsis) and retry. Stop if we
        # ever bottom out at a lone ellipsis — better that than infinite.
        trimmed = candidate[:-2].rstrip()
        if not trimmed:
            candidate = ELLIPSIS
            break
        candidate = trimmed + ELLIPSIS
    kept[-1] = candidate
    return kept


def _fit_title(text: str, font_path: str, max_width: int) -> tuple[list[str], "ImageFont.ImageFont"]:
    """Choose the largest TITLE_FIT_TIERS font at which ``text`` word-wraps
    within that tier's line budget WITHOUT ellipsis truncation, and return
    ``(lines, font)``.

    Iterates tiers largest-first: at a smaller font more words fit per line,
    so the natural (untruncated) line count only shrinks as we descend — the
    first tier whose natural wrap fits is the biggest font that shows the whole
    message. If even the smallest tier overflows (a message far past the input
    cap), that tier is used WITH ellipsis as a last resort. Short titles hit
    the first tier and render exactly as the pre-fix 48pt/2-line code did.

    Falls back to Pillow's default font if ``font_path`` can't be loaded (keeps
    the hardware path from crashing on a missing font, matching the caller's
    prior try/except contract).
    """
    if not text:
        try:
            return [], ImageFont.truetype(font_path, TITLE_FIT_TIERS[0][0])
        except Exception as e:
            # size=, never a silent 10px collapse (litclock-dev#589 item 3).
            logging.error("title font unavailable (%s); using scaled default", e)
            return [], ImageFont.load_default(size=TITLE_FIT_TIERS[0][0])

    last: tuple[list[str], ImageFont.FreeTypeFont, int] | None = None
    for size, max_lines in TITLE_FIT_TIERS:
        try:
            font = ImageFont.truetype(font_path, size)
        except Exception as e:
            # size=, matching this tier, never the silent 10px (litclock-dev#589 item 3).
            logging.error("title font unavailable at %dpt (%s); using scaled default", size, e)
            font = ImageFont.load_default(size=size)
        # Natural wrap: max_lines=len(text)+1 can never truncate, so this is the
        # untruncated line count at this font size.
        natural = _wrap_title(text, font, max_width, len(text) + 1)
        if len(natural) <= max_lines:
            return natural, font
        last = (natural, font, max_lines)
    # Overflowed every tier — truncate at the smallest (last) tier.
    natural, font, max_lines = last
    return _wrap_title(text, font, max_width, max_lines), font


def create_status_image(title: str, message: str = None, submessage: str = None) -> Image.Image:
    """
    Create a status/message display image.

    Args:
        title: Main title text
        message: Secondary message (optional)
        submessage: Smaller tertiary message (optional)

    Returns:
        PIL Image ready for display
    """
    # Create white background
    image = Image.new("1", DISPLAY_SIZE, 255)
    draw = ImageDraw.Draw(image)

    # Load the secondary fonts (fixed sizes). The TITLE font is chosen
    # per-message by _fit_title below, not fixed here.
    try:
        message_font = ImageFont.truetype(FONT_PATH, 28)
        small_font = ImageFont.truetype(FONT_PATH, 20)
    except Exception as e:
        # size= + log, never a silent 10px collapse (litclock-dev#589 item 3).
        logging.error("fonts unavailable (%s); using scaled default", e)
        message_font = ImageFont.load_default(size=28)
        small_font = ImageFont.load_default(size=20)

    # litclock-dev#319 + litclock-dev#280 gift-message fix: word-wrap the title at the TITLE_SIDE_MARGIN
    # gutter, AUTO-FITTING the font so a long personalized welcome shrinks to
    # fit instead of losing its tail to an ellipsis. Old code centered a
    # single-line draw.text (clipped both edges); the litclock-dev#319 wrap fixed the
    # clipping but capped at 48pt/2 lines and ellipsis-truncated anything
    # longer ("May it always be a good ti…" on a real gift). _fit_title returns
    # both the wrapped lines and the font size it settled on; short greetings
    # still land at 48pt/2 lines, unchanged.
    title_max_width = DISPLAY_SIZE[0] - 2 * TITLE_SIDE_MARGIN
    title_lines, title_font = _fit_title(title, FONT_PATH_BOLD, title_max_width)
    if title_lines:
        title_block = "\n".join(title_lines)
        title_bbox = draw.multiline_textbbox(
            (0, 0), title_block, font=title_font, spacing=TITLE_LINE_SPACING, align="center"
        )
        title_block_height = title_bbox[3] - title_bbox[1]
    else:
        title_block = ""
        title_block_height = 0

    # Vertical placement: when a multi-line message follows (e.g. gift-mode
    # setup steps) push the title block toward the top so the page reads
    # top-down; otherwise center the title vertically in the upper half.
    if message and "\n" in message:
        title_y = 60
    elif message:
        title_y = 150
    else:
        title_y = 200

    if title_block:
        draw.multiline_text(
            (DISPLAY_SIZE[0] // 2, title_y),
            title_block,
            font=title_font,
            fill=0,
            spacing=TITLE_LINE_SPACING,
            align="center",
            anchor="ma",  # middle-ascender: x is the center, y is the top
        )

    # Draw message if provided — gap below the title scales with how many
    # lines the title occupied so a 2-line gift welcome doesn't crash into
    # the steps list (litclock-dev#319 follow-up to the wrap fix).
    if message:
        message_y = title_y + title_block_height + 30
        message = _clamp_block_to_panel(message, message_font, draw, "status message")
        bbox = draw.textbbox((0, 0), message, font=message_font)
        msg_width = bbox[2] - bbox[0]
        msg_x = (DISPLAY_SIZE[0] - msg_width) // 2
        draw.text((msg_x, message_y), message, font=message_font, fill=0)

    # Draw submessage if provided
    if submessage:
        submessage = _clamp_block_to_panel(submessage, small_font, draw, "status submessage")
        bbox = draw.textbbox((0, 0), submessage, font=small_font)
        sub_width = bbox[2] - bbox[0]
        sub_x = (DISPLAY_SIZE[0] - sub_width) // 2
        draw.text((sub_x, DISPLAY_SIZE[1] - 60), submessage, font=small_font, fill=0)

    return image


def display_image(image: Image.Image, epd=None):
    """Display an image on the e-paper display."""
    if epd is None:
        epd = get_display()

    if epd is None:
        logging.error("No display available")
        return False

    try:
        logging.info("Initializing display...")
        epd.init()

        logging.info("Displaying image...")
        epd.display(epd.getbuffer(image))

        logging.info("Putting display to sleep...")
        epd.sleep()

        return True
    except Exception as e:
        logging.error(f"Failed to display image: {e}")
        return False


HOTSPOT_RETRY_WIFI_PASSWORD = "wifi_password"
# litclock-dev#603 — the retry splash for every NON-password failure class
# (timeout, network not found, anything else). The old behavior painted the
# wrong-password variant for all of them, and its "select your network from
# the list" step is exactly wrong for a hidden network that failed on
# reachability. Same class-neutral title; the steps differ.
HOTSPOT_RETRY_CONNECT_FAILED = "connect_failed"


# ── Setup-splash copy ────────────────────────────────────────────────
#
# Module-level so it is assertable without inspecting pixels. This is the
# highest-stakes copy in the product: it is the recipient's first contact,
# on a device with no keyboard, where the panel is the only instruction
# surface. Getting it wrong doesn't degrade setup, it blocks it.
#
# Both labels name LitClock's own WiFi, because the reader has to separate
# it from their own. The retry screen shows these values directly above a
# form asking for the user's home network and password, so an unqualified
# "Network:" / "Password:" is ambiguous exactly where it costs most.
#
# NOT "Hotspot" (litclock-dev#555): the one place a phone owner has already
# met that word is iOS Settings > Personal Hotspot, or Android's "Hotspot &
# tethering" — a thing THEY switch on to share THEIR connection. That is
# close to the opposite of what this screen means, so it sends them hunting
# through their own settings instead of their WiFi list.
#
# "Clock's Password:" (the litclock-dev#555 wording) turned out to be the worse of
# the two. Read plainly it means "the password belonging to the clock" — a
# credential to keep, like a device PIN. Two independent non-technical
# readers walked this panel and both STORED it (one wrote it on an envelope
# "so nobody messes with my clock"); neither connected it to joining
# anything, because nothing on the panel said to type it. Naming the network
# it belongs to is what makes it an input rather than a possession.
#
# Both labels keep the word "WiFi": the printed booklet the recipient is
# holding calls it "the clock's own Wi-Fi", and the panel and the booklet
# are the only two artifacts in their hands during setup. "network" and
# "password" are then the nouns that say which of the two is meant.
#
# Measured, not assumed — at Literata72pt-Black 22 these render 285px and
# 298px against the 510px budget (the panel is 800px wide and the label
# column starts at x=290, right of the 220px QR), so the fixed-position
# layout cannot have moved.
SETUP_LABEL_NETWORK = "LitClock's WiFi network:"
SETUP_LABEL_PASSWORD = "LitClock's WiFi password:"

# The concept the whole flow rests on and that nothing on the panel supplied.
# Neither reader knew a device can broadcast its own network — "I didn't buy
# the clock a WiFi." One sentence, rendered at SETUP_FRAMING_Y in the band
# under the QR, as the lead-in to the numbered steps. First run only: by the
# time the retry screen appears the reader has joined this network once, so
# the concept has landed and the headline is carrying different news.
SETUP_FRAMING_LINE = "The clock makes its own WiFi for setup."


def _sanitize_render_text(value: str | None, field: str, surface: str = "setup splash") -> str:
    """Strip control characters (including newlines) from a credential before
    it is drawn on the setup splash (litclock-dev#589). This renderer treats
    its args as constants, but wifi_provision honours ``--ssid`` and a newline
    in the SSID would render multiline and collide with the password label.
    Logs when it changes the value so a misconfigured credential is never
    silently mangled.

    ``surface`` names the caller in the warning, for the same reason
    _clamp_to_width takes one: the QR splash now calls this too, and journald
    is the only diagnostic channel on this device, so a log line naming the
    wrong screen costs real time."""
    if not value:
        return ""
    text = str(value)
    # str.isprintable() keeps ASCII space and every genuinely-printable glyph
    # (accented letters, emoji) while dropping ALL Unicode Other/Separator code
    # points — C0 AND C1 controls, DEL, NEL, the line/paragraph separators
    # U+2028/U+2029, zero-width and bidi format chars. A plain `32 <= ord < 127`
    # range would miss the C1 + Unicode-separator class (/review).
    cleaned = "".join(ch for ch in text if ch == " " or ch.isprintable())
    if cleaned != text:
        logging.warning("%s %s contained control characters; stripped for rendering", surface, field)
    return cleaned


def _wifi_qr_escape(value: str) -> str:
    r"""Backslash-escape the ``WIFI:`` QR payload's reserved characters
    ``\ ; , : "`` per the de-facto WIFI-QR format (litclock-dev#589).
    Unreachable with the alnum password generator and the constant SSID, but a
    varying input would otherwise silently encode the WRONG network onto the
    phone that scans it."""
    return "".join("\\" + ch if ch in '\\;,:"' else ch for ch in value)


def _collapse_newlines(text: str) -> str:
    """Flatten multiline text to one line.

    ``draw.textlength`` raises ValueError on any "\n", which would take down a
    whole splash render. Both the single-line clamp and the row shrink ladder
    measure before drawing, so both need this, and it lives here so the reason
    is recorded once. Reachable with arbitrary content via
    ``eink_display.py handoff-splash --settings-json``, which json.loads its
    input; the sibling SSID field is defended the same way.
    """
    if "\n" in text or "\r" in text:
        return " ".join(text.split())
    return text


def _clamp_to_width(text: str, font, draw, max_w: int, field: str, surface: str = "setup splash") -> str:
    """Single-line fit-with-ellipsis at ``max_w`` px, logging when it truncates
    so a clipped credential is never silent (litclock-dev#589). Reuses the
    handoff splash's fit logic (_fit_ssid_to_band).

    ``surface`` names the caller in the warning. It used to be hardcoded to
    "setup splash", which misattributed every truncation once the handoff and
    status paths started calling this — journald is the only diagnostic channel
    on this device, so a log line that names the wrong screen costs real time.

    Newlines are collapsed first: ``draw.textlength`` raises ValueError on
    multiline text, and before this helper was wired into _draw_dotted_row that
    path only called ``draw.text``, which renders multiline harmlessly. Not
    reachable from env.sh (config.load_config splits on newlines) but IS
    reachable via ``eink_display.py handoff-splash --settings-json``, which
    json.loads arbitrary input. create_handoff_splash_image already defends the
    sibling SSID field against exactly this.
    """
    # Compare against the COLLAPSED text for the width warning, but report the
    # collapse separately. Reassigning `text` before a single `result != text`
    # check made newline collapse invisible: a title of "\n" collapses to "",
    # the fit returns "", the two match, and the string vanished from the panel
    # with nothing in journald. Two causes, two messages — silence is the one
    # outcome this device cannot afford.
    collapsed = _collapse_newlines(text)
    if collapsed != text:
        logging.warning("%s %s contained line breaks; collapsed to one line", surface, field)
    fitted = _fit_ssid_to_band(collapsed, font, draw, max_w, max_lines=1)
    result = fitted[0] if fitted else ""
    if result != collapsed:
        logging.warning("%s %s too wide for the panel; truncated to fit", surface, field)
    return result


def _clamp_block_to_panel(text: str, font, draw, field: str) -> str:
    """Clamp every line of a status message to the panel width.

    ``create_status_image`` draws its message (28pt) and submessage (20pt) with
    a single unbounded ``draw.text``: only the TITLE has the fit-and-wrap
    ladder. Unclamped, ``msg_x = (800 - width) // 2`` goes NEGATIVE for a long
    string, so the text bleeds off BOTH edges and loses its head as well as its
    tail.

    Reachable, but only at the edge — state it precisely, because this docstring
    has been wrong twice already. ``scripts/first-boot.sh`` renders
    ``display_message "WiFi Connected" "Network: $ssid"`` with the joined
    network's name from ``iwgetid``, and SSIDs are user-controlled up to 32
    bytes. WIDTH depends on the glyphs: the rendered message "Network: " + 32
    literal W's measures 1005.9px at the 28pt message font against a 760px
    budget (the 32 W's alone are 880px — the figure is for the whole drawn
    string, which is what actually has to fit). Every realistic 32-byte SSID
    surveyed FITS, though: the widest plausible one found was 697px (92% of
    budget). So this is a guard against a
    narrow wide-glyph case plus translated copy, NOT an everyday overflow.
    (Two earlier versions claimed the opposite in both directions: first that
    every shipped string fit with bootcheck's 599px as the widest, then that any
    32-byte SSID overflowed. Neither was true.)

    Headroom on the fixed copy is also thinner than it looks: the widest
    shipped string is a poweroff farewell quote at 738.9px, 21px under the
    limit. One added comma would ellipsise it, on the one splash that persists
    on the glass while the device sits powered off on a shelf.

    Deliberately a clamp rather than a re-flow so shipped layouts stay
    byte-identical. Line-wise so gift mode's embedded newlines survive.

    KNOWN LIMIT for #532: it truncates character-wise and drops the TAIL, and
    every shipped submessage puts the actionable part last ("... see the
    LitClock docs.", "Then plug back in"). For translated instruction copy the
    right primitive is the font-shrinking ladder ``_fit_title`` uses, on the
    stated reasoning that silently cutting text is unacceptable there. This is
    strictly better than the current bleed-off-both-edges behaviour, but it is
    not the finished answer for translated strings.
    """
    max_w = PANEL_TEXT_BUDGET
    # No `if line` guard: _clamp_to_width already returns "" for falsy input,
    # and a mutation confirmed the conditional was a no-op branch.
    return "\n".join(
        _clamp_to_width(line, font, draw, max_w, field, surface="status splash") for line in text.split("\n")
    )


def setup_instruction_lines(
    ip: str, ssid: str = "LitClock-Setup", is_retry: bool = False, retry_reason: str | None = None
) -> list[str]:
    """Bottom instruction block for the setup splash.

    ``ssid`` names the network in the numbered steps. It MUST be the same value
    create_hotspot_display_image paints in the credential block — a step that
    says "join LitClock-Setup" while the credential block shows a different
    network name is unreadable to the very audience (litclock-dev#588) that cannot tell two
    networks apart (litclock-dev#589 item 2). Defaults to the hotspot's own
    DEFAULT_SSID for any direct caller.

    dnsmasq's wildcard on the setup network resolves every hostname to `ip`,
    and nftables redirects 80->8080, so SETUP_HOSTNAME lands on the real
    setup form without a port number. The raw gateway IP is printed
    alongside the hostname as an absolute fallback.

    Rules for this block, all learned from watching non-technical readers:

    - No OS-specific gestures. The old step 3 said "Swipe down (top-right) -
      tap WiFi", which is iOS Control Centre; an Android reader either finds
      nothing there or lands somewhere unrelated. Name the destination
      ("in your WiFi settings") and let them find it their own way.
    - No punctuation that could be mistaken for something to type. The old
      fallback separated two addresses with " | ". A pipe sitting between two
      URL-shaped strings does not read as "or" to someone who has never seen
      one; it reads as part of the address, and they type it. The separator
      is now the word "or".
    - Addresses carry an explicit http:// scheme. ".setup" is not a public
      TLD, so a bare "litclock.setup" is not confidently a hostname to a
      phone browser: Chrome's omnibox ranks a Google search above it, that
      is the biggest tap target, and the search then SUCCEEDS over cellular
      and returns junk. A scheme makes it a navigation instead of a guess.
    - The page is not named. The panel used to quote its title, "LitClock
      Setup", three lines under the network name "LitClock-Setup" — two
      near-identical strings for different things. One reader went hunting
      their WiFi list for the page title. The reader needs to know a page
      should appear, not what it is called.
    - No "~". Read aloud by a non-technical reader the tilde is a stray mark,
      and on an e-ink panel a stray mark is entirely plausible.
    """
    if is_retry:
        if retry_reason == HOTSPOT_RETRY_CONNECT_FAILED:
            # litclock-dev#603 — the join failed for a reason that is NOT
            # the password: a timeout, an unreachable/hidden network, or an
            # unclassified nmcli error. Password advice here would send the
            # reader retyping a password that was never the problem; the
            # likely fixes are radio-physical (band, range). The setup page
            # shows the specific cause once they're back on it.
            return [
                f"1. Rescan the QR code to rejoin {ssid}",
                "2. Check your WiFi is 2.4GHz and in range of the clock",
                "3. On the setup page, pick your network and try again",
                f"4. No page? Open a browser: http://{SETUP_HOSTNAME} or http://{ip}",
            ]
        return [
            f"1. Rescan the QR code to rejoin {ssid}",
            "2. Select your internet WiFi network, type your WiFi password",
            f"3. No page? Open a browser: http://{SETUP_HOSTNAME} or http://{ip}",
        ]
    return [
        f"1. Scan the QR code to join {ssid}",
        "2. Wait about 20 seconds for a setup page",
        f"3. No page? Join {ssid} in your WiFi settings",
        f"4. Then open a browser: http://{SETUP_HOSTNAME} or http://{ip}",
    ]


def create_hotspot_display_image(ssid: str, password: str, ip: str, retry_reason: str = None) -> Image.Image:
    """
    Create a display image for WiFi hotspot setup.

    Shows a QR code that auto-joins the hotspot (WIFI: format), plus the
    SSID, password, and setup URL as text.

    Args:
        ssid: Hotspot network name
        password: Hotspot password
        ip: Hotspot gateway IP (shown as absolute-fallback URL)
        retry_reason: If set, renders a retry-specific variant:
            HOTSPOT_RETRY_WIFI_PASSWORD ("wifi_password") when the user
            submitted a wrong WiFi password, HOTSPOT_RETRY_CONNECT_FAILED
            ("connect_failed") for every other failure class — timeout,
            network not found, unclassified (litclock-dev#603). The user
            needs distinct signal on the e-ink (not just the browser
            banner) because phones auto-disconnect from the hotspot during
            the failed connection attempt and may not see the banner until
            they've rescanned the QR. Both variants share the class-neutral
            title; the numbered steps differ.

    Returns:
        PIL Image ready for display
    """
    is_retry = retry_reason in (HOTSPOT_RETRY_WIFI_PASSWORD, HOTSPOT_RETRY_CONNECT_FAILED)

    # Validate the credentials at the boundary (litclock-dev#589): strip
    # control chars / newlines that would break the single-line layout. Logs
    # loudly on any change; the render still proceeds best-effort because a
    # blank splash on THE setup screen is a worse failure than a cleaned one.
    ssid = _sanitize_render_text(ssid, "network name")
    password = _sanitize_render_text(password, "password")

    # Create white background
    image = Image.new("1", DISPLAY_SIZE, 255)
    draw = ImageDraw.Draw(image)

    # Load fonts. On failure (missing fonts/ dir) DON'T fall to a bare
    # load_default(): on Pillow >= 12 that is a 10px bitmap, which collapses the
    # 36/22/24/18px hierarchy to ~1.4mm cap height — a fully-painted but
    # unreadable panel with a clean journal (litclock-dev#589 item 3). Pass
    # size= (Pillow >= 10.1 returns scalable Aileron) and log an ERROR so a
    # broken fonts/ dir is never silent.
    try:
        title_font = ImageFont.truetype(FONT_PATH_BOLD, 36)
        label_font = ImageFont.truetype(FONT_PATH_BOLD, 22)
        value_font = ImageFont.truetype(FONT_PATH, 24)
        small_font = ImageFont.truetype(FONT_PATH, SETUP_SMALL_FONT_PT)
    except Exception as e:
        logging.error("setup splash fonts unavailable (%s); using scaled default — legible but off-brand", e)
        title_font = ImageFont.load_default(size=36)
        label_font = ImageFont.load_default(size=22)
        value_font = ImageFont.load_default(size=24)
        small_font = ImageFont.load_default(size=SETUP_SMALL_FONT_PT)

    # Title — swap to a distinct retry title so the user's eye immediately
    # registers "something changed, read this." E-ink is monochrome so we
    # can't use color to distinguish states; the title text is the signal.
    # Retry title avoids the word "password" on purpose: the hotspot
    # password is visible right below the title, and a title saying "Wrong
    # Password" would prime a naive user to type THAT password into their
    # home-WiFi-password field. "Couldn't Join Your WiFi" makes it
    # unambiguous that the failure was about the user's own WiFi, not the
    # hotspot the phone is currently connected to.
    title = "Couldn't Join Your WiFi" if is_retry else "WiFi Setup"
    bbox = draw.textbbox((0, 0), title, font=title_font)
    title_width = bbox[2] - bbox[0]
    draw.text(((DISPLAY_SIZE[0] - title_width) // 2, 12), title, font=title_font, fill=0)

    # Framing line placement is NOT free space. Directly under the title it
    # left a 2px gap to the title's descenders and, being centred on an 800px
    # panel, its left end reached x=237 — 23px INSIDE the QR's 220px block,
    # putting 17 ink pixels in the quiet zone at the top-right corner. A
    # quiet zone with ink in it is a scan-reliability bug, not a cosmetic
    # one: if the QR will not scan, setup cannot start at all.
    #
    # It sits instead in the band under the QR, at SETUP_QR_Y + SETUP_QR_SIZE
    # + SETUP_QR_QUIET_ZONE_PX — derived from the QR's own geometry rather
    # than measured once, so moving or resizing the QR moves it too. It reads
    # as the lead-in to the numbered steps it introduces.
    if not is_retry:
        fb = draw.textbbox((0, 0), SETUP_FRAMING_LINE, font=small_font)
        draw.text(
            ((DISPLAY_SIZE[0] - (fb[2] - fb[0])) // 2, SETUP_FRAMING_Y),
            SETUP_FRAMING_LINE,
            font=small_font,
            fill=0,
        )

    # WiFi QR code (standard format that phones auto-recognize). Same QR in
    # the retry state — the hotspot credentials are unchanged, only the
    # user-facing instructions differ.
    wifi_qr_data = f"WIFI:T:WPA;S:{_wifi_qr_escape(ssid)};P:{_wifi_qr_escape(password)};;"
    qr_size = SETUP_QR_SIZE
    qr_image = generate_qr_image(wifi_qr_data)
    qr_image = qr_image.resize((qr_size, qr_size), Image.Resampling.NEAREST)

    # Place QR on left side
    qr_x = SETUP_QR_X
    qr_y = SETUP_QR_Y
    image.paste(qr_image, (qr_x, qr_y))

    # Text info on right side
    text_x = qr_x + qr_size + 30
    text_y = qr_y + 10

    # Clamp the values to the right-column budget so a long SSID/password can't
    # clip silently at x=800 with no ellipsis (litclock-dev#589 item 1). The
    # budget is the panel width minus the value's left edge.
    value_budget = DISPLAY_SIZE[0] - text_x
    ssid_line = _clamp_to_width(ssid, value_font, draw, value_budget, "network name")
    password_line = _clamp_to_width(password, value_font, draw, value_budget, "password")

    draw.text((text_x, text_y), SETUP_LABEL_NETWORK, font=label_font, fill=0)
    draw.text((text_x, text_y + 30), ssid_line, font=value_font, fill=0)

    draw.text((text_x, text_y + 80), SETUP_LABEL_PASSWORD, font=label_font, fill=0)
    draw.text((text_x, text_y + 110), password_line, font=value_font, fill=0)

    lines = setup_instruction_lines(ip, ssid=ssid, is_retry=is_retry, retry_reason=retry_reason)

    # Per-line clamp (litclock-dev#626, the litclock-dev#589-review Q4 gap): the credential
    # block clamps its values, but the STEP lines interpolate the same ssid
    # (and ip) with no width guard of their own. Clamping each line to the
    # panel width means an over-long value ellipsizes here the same way it
    # does in the credential block, instead of clipping at x=800 — and it
    # keeps `widest` <= panel so the block_x centring below never degrades.
    lines = [
        _clamp_to_width(line, small_font, draw, DISPLAY_SIZE[0], f"setup step {i + 1}") for i, line in enumerate(lines)
    ]

    # Stack the lines bottom-up so both 3-line and 4-line layouts sit flush
    # with the bottom edge at a consistent padding.
    #
    # The BLOCK is centred; the lines inside it are left-aligned to a shared
    # x, so the step numbers stack in a column. Centring each line
    # individually gave every line a different left edge, and a reader with
    # presbyopic eyes at arm's length loses their place descending a ragged
    # list. Costs nothing and changes no copy.
    #
    # Clamped at 0: block_x is (panel - widest line) // 2 and goes NEGATIVE
    # if any line outgrows the panel, which clips the left edge silently —
    # losing the leading "4." and the trailing digits of an address the
    # reader is being asked to type. `ip` reaches here from a JSON parse in
    # first-boot.sh, so it is not a constant from this function's point of
    # view. Clamping degrades to a right-clip instead of clipping both ends.
    widest = max(draw.textbbox((0, 0), ln, font=small_font)[2] for ln in lines)
    block_x = max(0, (DISPLAY_SIZE[0] - widest) // 2)
    top = instruction_block_top(len(lines))
    for i, line in enumerate(lines):
        draw.text((block_x, top + i * HOTSPOT_INFO_LINE_HEIGHT), line, font=small_font, fill=0)

    return image


# Handoff splash layout (EPIC litclock-dev#383 PR2, litclock-dev#388). Settings summary block on the
# left, PWA QR top-right. Column where the dotted-leader values start.
HANDOFF_LEFT_MARGIN = 50
# Gutter kept clear before a column boundary, so a clamped cell never sits flush
# against the thing it was clamped away from. One name because four sites need
# the same number: both budgets below and both ends of the dotted leader.
HANDOFF_COLUMN_GUTTER = 8
# Moved left from 330 so the value column keeps a usable budget once it is
# bounded by the right column instead of the panel edge (see
# HANDOFF_VALUE_BUDGET). At 330 the safe budget was 192px, which truncated
# ordinary IP-geo cities ("Frankfurt am Main, Hesse", 253px); at 240 it is
# HANDOFF_VALUE_BUDGET px, enough that every realistic location/timezone
# surveyed survives INTACT — the widest take a point or two off the shrink
# ladder rather than losing characters. The labels still clear the column: the
# widest, "Mature quotes", is 148px and ends at x=198, leaving 34px of leader.
HANDOFF_VALUE_COLUMN = 240
# QR geometry, hoisted out of create_handoff_splash_image because the settings
# block has to be budgeted against it. The QR is pasted BEFORE the rows are
# drawn, so any row that reaches this far paints on top of it.
HANDOFF_QR_SIZE = 200
HANDOFF_QR_X = DISPLAY_SIZE[0] - HANDOFF_QR_SIZE - HANDOFF_LEFT_MARGIN  # 550
HANDOFF_QR_Y = 40
# Room a row value has before it hits the RIGHT COLUMN — not before it leaves
# the panel. The panel edge is the wrong bound: the QR starts at x=550 and the
# URL / "scan with your phone on:" / SSID text sit under it, so right-column ink
# spans y≈40..364 while all four rows sit at y 200..324. Budgeting against
# DISPLAY_SIZE[0] let a value reach x=750, straight across the QR.
#
# It needed no typing to reach — plain IP-geo output did it. At the old 330
# column, "Buenos Aires, Argentina" (246px) ended at x=576, and 109 of 498 IANA
# zones (22%), America/Los_Angeles among them, crossed into the right column.
# The Location row lands on the QR modules; the Timezone row lands on the IP
# fallback text printed under it. Between them they degrade both routes to the
# PWA on the one splash whose only job is getting a phone there.
#
# The bound is the RIGHT COLUMN's leftmost ink, which is NOT the QR: the caveat
# label is centred under the QR and is WIDER than it. "Scan with your phone on:"
# measures 212px against the QR's 200px, so it starts at x=543 — 7px inboard of
# the QR — and sits at y 274..292, overlapping the Units row at y 268..290.
# Budgeting to HANDOFF_QR_X alone left exactly 1px of clearance there, so the
# lane is declared explicitly instead, with room for the caveat to grow.
# test_the_shipped_caveat_label_stays_in_its_lane pins that measurement.
HANDOFF_RIGHT_COLUMN_X = 530
# Widest a CENTRED right-column caption may be before its own left edge crosses
# the lane boundary: centring at HANDOFF_QR_X means left_edge = QR_X + (QR_SIZE
# - w)/2, so w <= QR_SIZE + 2*(QR_X - RIGHT_COLUMN_X). The rows are not the only
# thing that can cross this line — the URL caption under the QR was drawn
# unclamped and, for a long enough qr_url, ran the full width of the panel
# straight through the settings block. Same bleed this PR fixes for the rows,
# on the sibling line.
HANDOFF_RIGHT_TEXT_BUDGET = HANDOFF_QR_SIZE + 2 * (HANDOFF_QR_X - HANDOFF_RIGHT_COLUMN_X)
HANDOFF_VALUE_BUDGET = HANDOFF_RIGHT_COLUMN_X - HANDOFF_COLUMN_GUTTER - HANDOFF_VALUE_COLUMN
# The LABEL column is tighter still (182px), and moving the value column left
# tightened it further — a deliberate trade made when this fix was chosen: it
# shifts the #532 translation pressure from the values onto the labels, where
# the shrink ladder below absorbs it. English labels are unaffected ("Mature
# quotes", the widest, is 148px of 182). German "Nicht jugendfreie Zitate" is
# 242px and now shrinks rather than rendering at full size.
HANDOFF_LABEL_BUDGET = HANDOFF_VALUE_COLUMN - HANDOFF_COLUMN_GUTTER - HANDOFF_LEFT_MARGIN
# Floor for the row shrink ladder. Below this the value stops being readable
# across a room, which is the whole point of the splash, so past the floor we
# truncate instead of shrinking further.
HANDOFF_ROW_FONT_FLOOR = 15
HANDOFF_ROW_HEIGHT = 34
# Kept short so it reads as a value rather than a sentence; the QR clearance
# it used to rely on is now enforced by HANDOFF_VALUE_BUDGET above.
HANDOFF_NOT_DETECTED = "Not detected"

# SSID caveat layout (litclock-dev#399). Painted right-column under the URL text.
# Max lines: two is the budget — wider would push past the bottom-status
# line; less and a 24-char realistic SSID would truncate too aggressively.
HANDOFF_SSID_MAX_LINES = 2
# Vertical offsets from the URL text baseline down to the caveat label,
# and from the label down to the first SSID line. Named so a future
# spacing tweak doesn't have to grep pixel arithmetic.
HANDOFF_CAVEAT_TOP_GAP = 28  # url_y → caveat_label_y
HANDOFF_CAVEAT_SSID_GAP = 24  # caveat_label_y → first ssid line
# Per-line vertical spacing for the SSID value. Two values because the
# bold label_font (22pt) is taller than the small_font (18pt); a wrapped
# SSID at small_font packs tighter.
HANDOFF_SSID_LINE_HEIGHT_LARGE = 24  # label_font (22pt bold)
HANDOFF_SSID_LINE_HEIGHT_SMALL = 22  # small_font (18pt regular)
# Caveat label copy — hoisted alongside HANDOFF_NOT_DETECTED so future
# copy iterations have ONE intercept point (matches the splash's other
# user-visible strings).
HANDOFF_CAVEAT_LABEL = "Scan with your phone on:"


def _halve_until_close(text: str, suffix: str, font, draw, max_w: int) -> str:
    """Cheap prefix pre-cut that keeps the fit loops below bounded.

    Those loops delete ONE character and re-measure the WHOLE string, so they
    are O(n²) in the input length: 8.6s for an 8000-char argument measured on
    a dev box, and a Pi Zero 2W is 10-20x slower than that, against the
    ``timeout 20`` wrapper in scripts/first-boot.sh. Not reachable from the
    shipped call (``SETUP_URL`` is built from an IPv4-only grep, ≤28 chars) —
    but the character guard this replaced was O(1), and measuring instead of
    counting must not make pathological input worse than counting was.

    Halves only while the halved prefix is STILL over budget, so it can never
    cut past the answer: if ``half`` does not fit, the longest fitting prefix
    is strictly shorter than ``half``, and everything after it is dead weight.
    The caller's loop therefore returns exactly what it would have returned
    unaided, after O(log n) extra measurements instead of O(n).
    """
    while len(text) > 64:
        half = text[: len(text) // 2]
        if draw.textlength(half + suffix, font=font) <= max_w:
            break
        text = half
    return text


def _fit_ssid_to_band(ssid: str, font, draw, max_w: int, max_lines: int = HANDOFF_SSID_MAX_LINES) -> list[str]:
    """Wrap an SSID string into at most ``max_lines`` lines at ``font``,
    each fitting within ``max_w`` pixels. The last line is truncated with
    an ellipsis (… U+2026) when the full SSID overflows. Truncation keeps
    the SSID PREFIX — the human-recognizable brand portion is the part a
    glancing user reaches for.

    Returns an empty list for empty input. Character-level wrap (no word
    breaks) because SSIDs don't have spaces in any meaningful sense; the
    user reads them as opaque labels."""
    if not ssid:
        return []
    lines: list[str] = []
    remaining = ssid
    for line_idx in range(max_lines):
        if not remaining:
            break
        is_last = line_idx == max_lines - 1
        # Trial: does the whole remainder fit on this line?
        if draw.textlength(remaining, font=font) <= max_w:
            lines.append(remaining)
            return lines
        # Doesn't fit. If this is the last line, fit-with-ellipsis.
        if is_last:
            ellipsis = "…"
            line = _halve_until_close(remaining, ellipsis, font, draw, max_w)
            while line and draw.textlength(line + ellipsis, font=font) > max_w:
                line = line[:-1]
            lines.append((line + ellipsis) if line else ellipsis)
            return lines
        # Otherwise, peel off the longest prefix that fits and continue.
        line = _halve_until_close(remaining, "", font, draw, max_w)
        while line and draw.textlength(line, font=font) > max_w:
            line = line[:-1]
        if not line:
            # Even a single char doesn't fit — degenerate; bail with ellipsis.
            return lines + ["…"]
        lines.append(line)
        remaining = remaining[len(line) :]
    return lines


def _as_bool(value) -> bool:
    """Interpret a settings flag that may have arrived as JSON rather than a bool.

    ``bool("false")`` is True, so bare truthiness inverts a flag delivered as a
    string. Only the conventional affirmatives count as True.
    """
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _sanitize_row_value(value) -> str:
    """Strip non-printables and collapse whitespace in an untrusted row value.

    Mirrors the connected_ssid filter in create_handoff_splash_image. Keeps a
    plain space (str.isprintable() rejects it) so multi-word city names survive.
    Non-str input returns "" so the caller's `or HANDOFF_NOT_DETECTED` fires.
    """
    if not isinstance(value, str):
        return ""
    # Whitespace becomes a space rather than vanishing: str.isprintable() is
    # False for TAB, so simply dropping non-printables welds the words either
    # side of one together ("Aires,\tArgentina" -> "Aires,Argentina"). Truly
    # invisible characters (zero-width space, bidi overrides) are not
    # whitespace and ARE dropped, which is the point.
    cleaned = "".join(" " if ch.isspace() else ch for ch in value if ch.isspace() or ch.isprintable())
    return " ".join(cleaned.split())


def _row_baseline(y: int, base_font) -> int:
    """The baseline every cell in a row sits on.

    Mixed font sizes in one row have to share a BASELINE, not a top edge. PIL's
    default anchor is top-left, so drawing a shrunk value at the same y floats
    it upward by the ascent difference — 7px at the 15pt floor against a 22pt
    row, which reads as a broken line. (A first attempt nudged by half the
    point-size delta; measured against real renders that under-corrected in
    both directions, because ascent does not scale linearly with declared
    size.) Callers pass anchor="ls" so this is exact rather than approximate.
    """
    try:
        ascent, _ = base_font.getmetrics()
    except Exception as e:
        # journald is the only diagnostic channel on this device, so a silent
        # fallback here would shift the whole settings block with no trail.
        # The declared size is NOT the ascent (Literata is 26 at 22pt), so
        # approximate rather than using `size` and sitting 4px high.
        logging.error("row baseline: font reports no metrics (%s); approximating ascent", e)
        return y + int(getattr(base_font, "size", 22) * 1.18)
    return y + ascent


def _fit_row_text(text: str, font, draw, budget: int, field: str):
    """Fit ``text`` into ``budget`` px, preferring a smaller font over a
    shorter string. Returns ``(text, font)``.

    Shrinks one point at a time from the row's base size down to
    ``HANDOFF_ROW_FONT_FLOOR``, and only truncates if it still doesn't fit at
    the floor. This is the ``_fit_title`` primitive applied to the settings
    rows: values here are things the reader VERIFIES (their city, their
    timezone), so losing the tail is worse than losing a couple of points of
    size. See HANDOFF_VALUE_BUDGET for why the budget is what it is.

    Falls back to plain truncation when the font has no loadable path (the
    ``load_default`` path taken when Literata is missing), because there is no
    size ladder to walk in that case.
    """
    if not text:
        return "", font
    # BEFORE the first measurement: this ladder measures ahead of
    # _clamp_to_width, which is where the collapse used to happen, so without it
    # a single "\n" in a row value takes down the whole splash render.
    text = _collapse_newlines(text)
    if draw.textlength(text, font=font) <= budget:
        return text, font

    path = getattr(font, "path", None)
    base_size = getattr(font, "size", None)
    fitted = font
    if path and base_size:
        for trial in range(int(base_size) - 1, HANDOFF_ROW_FONT_FLOOR - 1, -1):
            try:
                candidate = ImageFont.truetype(path, trial)
            except Exception as e:  # unreadable font file mid-ladder
                logging.error("row shrink ladder could not load %s at %dpt (%s)", path, trial, e)
                break
            if draw.textlength(text, font=candidate) <= budget:
                return text, candidate
            fitted = candidate

    # Still too wide at the floor — truncate at whatever size we reached.
    return _clamp_to_width(text, fitted, draw, budget, field, surface="handoff splash"), fitted


def _draw_dotted_row(draw, y, label, value, font):
    """Draw 'Label ........... Value' with a dotted leader filling the gap
    between the label and the fixed value column. Monochrome e-ink has no
    color to lean on, so the leader is what visually ties label to value.

    Both the label and the value are clamped (litclock-dev#620 /review). They
    were drawn unbounded from fixed columns, so two separate things went wrong:

    1. A long value ran off the edge of the glass. ``WEATHER_LOCATION_NAME_MAX_LEN``
       accepts 120 characters and the PWA lets the owner TYPE a place in
       Location > Specific; that value survives reboots by design (#337
       MODE=specific) and a WiFi reset clears .handoff-complete so this splash
       repaints, so a long typed name genuinely reaches the panel.
    2. Long before that, it collided with the right column — see
       HANDOFF_VALUE_BUDGET. This one needs no typing at all: plain IP-geo
       output reaches it.

    Both columns go through ``_fit_row_text``: shrink to fit, truncate only at
    the floor. A plain clamp was tried first and rejected on rendered evidence —
    at the QR-safe budget it truncated ordinary IP-geo cities, and a mangled
    city name is a worse answer than a slightly smaller one. Shrinking keeps
    every realistic value complete and reserves truncation for input no
    geolocation service produces.
    """
    baseline = _row_baseline(y, font)
    label, label_font = _fit_row_text(label, font, draw, HANDOFF_LABEL_BUDGET, "row label")
    draw.text((HANDOFF_LEFT_MARGIN, baseline), label, font=label_font, fill=0, anchor="ls")
    label_w = draw.textlength(label, font=label_font)
    # The leader stays at the row's base font so a shrunk label doesn't drag the
    # dots down with it — the leader is a horizontal rule, not part of the text.
    dot_start = HANDOFF_LEFT_MARGIN + label_w + HANDOFF_COLUMN_GUTTER
    dot_end = HANDOFF_VALUE_COLUMN - HANDOFF_COLUMN_GUTTER
    dot_w = draw.textlength(".", font=font) or 1
    n_dots = max(0, int((dot_end - dot_start) // dot_w))
    if n_dots:
        draw.text((dot_start, baseline), "." * n_dots, font=font, fill=0, anchor="ls")
    value, value_font = _fit_row_text(value, font, draw, HANDOFF_VALUE_BUDGET, "row value")
    draw.text((HANDOFF_VALUE_COLUMN, baseline), value, font=value_font, fill=0, anchor="ls")


def create_handoff_splash_image(settings: dict, qr_url: str) -> Image.Image:
    """Render the post-WiFi handoff splash (EPIC litclock-dev#383 PR2, litclock-dev#388).

    Painted by control_server on the first launch since setup, in the gap
    between "WiFi connected + location auto-detected" and "quotes start." Shows
    the auto-detected settings the user can fine-tune, plus the PWA QR.

    ``settings`` is ``handoff.handoff_context`` output; the relevant keys are
    ``has_location`` (success vs failure variant), ``location_name``,
    ``timezone``, ``units_label``, ``mature_enabled``. ``connected_ssid``
    (litclock-dev#399) is optional — when present, the splash paints a "phone must
    be on this WiFi" caveat under the QR so a phone on cellular / a
    different network doesn't silently fail the scan (the QR encodes a
    LAN-only IP). When empty, the caveat is suppressed rather than
    displayed as "(unknown)" — better to omit than mislead.
    ``qr_url`` is the IP-based PWA URL the QR encodes (A5).

    Success (IP-geo detected a location): "Ready to read." + filled settings.
    Failure (no location, tz unknown): "Almost ready." + "Not detected" rows +
    a "scan the QR to set your timezone" call to action, because a clock that
    paints quotes at the wrong time is worse than no clock (design-review A2).
    """
    has_location = bool(settings.get("has_location"))

    image = Image.new("1", DISPLAY_SIZE, 255)
    draw = ImageDraw.Draw(image)

    try:
        brand_font = ImageFont.truetype(FONT_PATH_BOLD, 26)
        heading_font = ImageFont.truetype(FONT_PATH_BOLD, 40)
        label_font = ImageFont.truetype(FONT_PATH_BOLD, 22)
        row_font = ImageFont.truetype(FONT_PATH, 22)
        small_font = ImageFont.truetype(FONT_PATH, 18)
    except Exception as e:
        # size= + log, never a silent 10px collapse (litclock-dev#589 item 3).
        logging.error("handoff splash fonts unavailable (%s); using scaled default", e)
        brand_font = ImageFont.load_default(size=26)
        heading_font = ImageFont.load_default(size=40)
        label_font = ImageFont.load_default(size=22)
        row_font = ImageFont.load_default(size=22)
        small_font = ImageFont.load_default(size=18)

    # Brand wordmark + hairline rule, top-left.
    draw.text((HANDOFF_LEFT_MARGIN, 28), "LITCLOCK", font=brand_font, fill=0)
    draw.line((HANDOFF_LEFT_MARGIN, 66, HANDOFF_LEFT_MARGIN + 150, 66), fill=0, width=2)

    # PWA QR, top-right. A5: encode the just-acquired IP (100% scan success vs
    # flaky Android mDNS). URL printed under it as the human-readable fallback.
    qr_size = HANDOFF_QR_SIZE
    qr_x = HANDOFF_QR_X
    qr_y = HANDOFF_QR_Y
    qr_image = generate_qr_image(qr_url).resize((qr_size, qr_size), Image.Resampling.NEAREST)
    image.paste(qr_image, (qr_x, qr_y))
    url_text = _clamp_to_width(
        _sanitize_row_value(qr_url).replace("http://", ""),
        small_font,
        draw,
        HANDOFF_RIGHT_TEXT_BUDGET,
        "QR url caption",
        surface="handoff splash",
    )
    url_w = draw.textlength(url_text, font=small_font)
    url_y = qr_y + qr_size + 6
    draw.text((qr_x + (qr_size - url_w) // 2, url_y), url_text, font=small_font, fill=0)

    # Cross-network caveat (litclock-dev#399). The QR encodes a LAN-only IP, so a phone
    # on cellular or a different WiFi gets a silent dead link. Surface the
    # SSID the clock is on so the user knows where to put their phone first.
    # Right-column under the URL text. Suppressed when connected_ssid is
    # empty rather than rendering "(unknown)" — better to omit a misleading
    # hint than display one.
    #
    # Defense-in-depth: strip control chars / non-printables / multi-line
    # content before any measurement. PIL's draw.textlength raises
    # ValueError on any `\n` in the input, which would silently fail the
    # whole splash render via the outer try/except in render_eink_splash.
    # handoff.connected_ssid() also sanitizes upstream, but the renderer
    # cannot trust ALL future callers (tests, dev stubs, third-party
    # callers) to do so.
    _ssid_raw = settings.get("connected_ssid") or ""
    connected_ssid = "".join(ch for ch in _ssid_raw if ch.isprintable())
    connected_ssid = " ".join(connected_ssid.split())
    if connected_ssid:
        caveat_y = url_y + HANDOFF_CAVEAT_TOP_GAP
        # Center the label under the QR (matches URL-text alignment).
        caveat_label = _clamp_to_width(
            HANDOFF_CAVEAT_LABEL, small_font, draw, HANDOFF_RIGHT_TEXT_BUDGET, "caveat label", surface="handoff splash"
        )
        label_w = draw.textlength(caveat_label, font=small_font)
        draw.text(
            (qr_x + (qr_size - label_w) // 2, caveat_y),
            caveat_label,
            font=small_font,
            fill=0,
        )
        # The SSID value uses the bold label_font when it fits on one line
        # (~12 chars at 22pt — typical home WiFi), so it pops as the
        # actionable value. For longer SSIDs we fall back to small_font
        # (18pt) and wrap onto up to HANDOFF_SSID_MAX_LINES lines via
        # `_fit_ssid_to_band` (testable in isolation).
        ssid_y = caveat_y + HANDOFF_CAVEAT_SSID_GAP
        max_w = qr_size  # match the QR's width as the natural visual bound

        if draw.textlength(connected_ssid, font=label_font) <= max_w:
            ssid_font = label_font
            ssid_lines = [connected_ssid]
            line_height = HANDOFF_SSID_LINE_HEIGHT_LARGE
        else:
            ssid_font = small_font
            ssid_lines = _fit_ssid_to_band(connected_ssid, small_font, draw, max_w)
            line_height = HANDOFF_SSID_LINE_HEIGHT_SMALL

        for i, line in enumerate(ssid_lines):
            if not line:
                continue
            w = draw.textlength(line, font=ssid_font)
            draw.text(
                (qr_x + (qr_size - w) // 2, ssid_y + i * line_height),
                line,
                font=ssid_font,
                fill=0,
            )

    # Heading.
    heading = "Ready to read." if has_location else "Almost ready."
    draw.text((HANDOFF_LEFT_MARGIN, 92), heading, font=heading_font, fill=0)

    # Settings summary block.
    draw.text((HANDOFF_LEFT_MARGIN, 158), "Your settings — auto-detected:", font=label_font, fill=0)
    # Same defence the connected_ssid field above already gets, for the same
    # reason: these are not trusted strings. location_name and timezone come
    # from the ip-api.com response, which is fetched over plain HTTP
    # (geocoding.py), so anyone able to answer that request controls them; the
    # PWA's typed-place path has its own allowlist but the IP-geo write path
    # does not go through it. Unfiltered, a bidi override or zero-width run
    # could reorder or hide part of the very rows the splash exists to let the
    # user verify before trusting the clock.
    location_value = _sanitize_row_value(settings.get("location_name")) or HANDOFF_NOT_DETECTED
    timezone_value = _sanitize_row_value(settings.get("timezone")) or HANDOFF_NOT_DETECTED
    if not has_location:
        location_value = timezone_value = HANDOFF_NOT_DETECTED
    rows = [
        ("Location", location_value),
        ("Timezone", timezone_value),
        # Sanitized like the rows above, and for the same reason: this arrives
        # from the same --settings-json payload. Unfiltered it accepted a bidi
        # override, and a NON-STRING value (json.loads happily yields an int or
        # a dict) raised TypeError out of _collapse_newlines and took the whole
        # splash down. `or` restores the default for empty/invalid input.
        ("Units", _sanitize_row_value(settings.get("units_label")) or "Imperial (°F)"),
        # Not bare truthiness: the string "false" is truthy, which would invert
        # the mature-content indicator. handoff_context() always passes a real
        # bool, but --settings-json does not have to.
        ("Mature quotes", "On" if _as_bool(settings.get("mature_enabled")) else "Off"),
    ]
    row_y = 200
    for label, value in rows:
        _draw_dotted_row(draw, row_y, label, value, row_font)
        row_y += HANDOFF_ROW_HEIGHT

    # Call to action + educational note, lower-left.
    cta = "To change anything, scan the QR." if has_location else "Scan the QR to set your timezone."
    draw.text((HANDOFF_LEFT_MARGIN, row_y + 14), cta, font=row_font, fill=0)
    tip_lines = [
        "Tip: this QR lives in the corner of every",
        "quote — scan it any time to return to settings.",
    ]
    tip_y = row_y + 52
    for line in tip_lines:
        draw.text((HANDOFF_LEFT_MARGIN, tip_y), line, font=small_font, fill=0)
        tip_y += 24

    # Bottom status line, centered.
    bottom = "Quotes start shortly." if has_location else "Quotes start once your timezone is set."
    bottom_w = draw.textlength(bottom, font=row_font)
    draw.text(((DISPLAY_SIZE[0] - bottom_w) // 2, DISPLAY_SIZE[1] - 44), bottom, font=row_font, fill=0)

    return image


def display_handoff_splash(settings: dict, qr_url: str):
    """Render + push the handoff splash to the e-paper display."""
    return display_image(create_handoff_splash_image(settings, qr_url))


def display_hotspot_info(ssid: str, password: str, ip: str, retry_reason: str = None):
    """
    Display hotspot connection info on e-paper display.

    Args:
        ssid: Hotspot network name
        password: Hotspot password
        ip: Hotspot gateway IP
        retry_reason: If set, renders the retry-specific variant. See
            create_hotspot_display_image() for supported values.
    """
    image = create_hotspot_display_image(ssid, password, ip, retry_reason=retry_reason)
    return display_image(image)


def display_qr(url: str, title: str = None, caption: str = None):
    """
    Display a QR code on the e-paper display.

    Args:
        url: URL or data to encode
        title: Title above QR code
        caption: Caption below QR code
    """
    image = create_qr_display_image(url, title, caption)
    return display_image(image)


def display_status(title: str, message: str = None, submessage: str = None):
    """
    Display a status message on the e-paper display.

    Args:
        title: Main title
        message: Secondary message
        submessage: Smaller message at bottom
    """
    image = create_status_image(title, message, submessage)
    return display_image(image)


def save_image(image: Image.Image, path: str):
    """Save image to file (for testing without display)."""
    image.save(path)
    logging.info(f"Image saved to {path}")


def main():
    parser = argparse.ArgumentParser(description="E-ink Display Utility")
    subparsers = parser.add_subparsers(dest="command", help="Command")

    # QR command
    qr_parser = subparsers.add_parser("qr", help="Display QR code")
    qr_parser.add_argument("url", help="URL or data to encode")
    qr_parser.add_argument("--title", "-t", help="Title above QR code")
    qr_parser.add_argument("--caption", "-c", help="Caption below QR code")
    qr_parser.add_argument("--save", "-s", help="Save to file instead of displaying")

    # Status command
    status_parser = subparsers.add_parser("status", help="Display status message")
    status_parser.add_argument("title", help="Main title")
    status_parser.add_argument("--message", "-m", help="Secondary message")
    status_parser.add_argument("--submessage", "-sub", help="Small message at bottom")
    status_parser.add_argument("--save", "-s", help="Save to file instead of displaying")

    # Hotspot command
    hotspot_parser = subparsers.add_parser("hotspot", help="Display hotspot setup info with QR")
    hotspot_parser.add_argument("ssid", help="Hotspot SSID")
    hotspot_parser.add_argument("password", help="Hotspot password")
    hotspot_parser.add_argument("ip", help="Hotspot gateway IP")
    hotspot_parser.add_argument("--save", "-s", help="Save to file instead of displaying")

    # Clear command
    clear_parser = subparsers.add_parser("clear", help="Clear display (white screen)")
    clear_parser.add_argument("--save", "-s", help="Save to file instead of displaying")

    # Handoff-splash command (litclock-dev#388). Invoked as a SHORT-LIVED subprocess by the
    # long-lived control_server so it never holds the e-ink GPIO — module_exit /
    # gpiozero-close does NOT free lgpio line claims; only process exit does, so a
    # long-lived in-process painter would leave litclock.service stuck on
    # 'GPIO busy' (fresh-flash test-Pi QA 2026-07-06). The settings dict is passed
    # as JSON so the subprocess rebuilds the splash without re-deriving context.
    handoff_parser = subparsers.add_parser("handoff-splash", help="Display the post-WiFi handoff splash")
    handoff_parser.add_argument("qr_url", help="PWA QR URL encoded on the splash")
    handoff_parser.add_argument("--settings-json", required=True, help="handoff_context dict as JSON")
    handoff_parser.add_argument("--save", "-s", help="Save to file instead of displaying")

    args = parser.parse_args()

    if args.command == "qr":
        image = create_qr_display_image(args.url, args.title, args.caption)
        if args.save:
            save_image(image, args.save)
        else:
            display_image(image)

    elif args.command == "status":
        image = create_status_image(args.title, args.message, args.submessage)
        if args.save:
            save_image(image, args.save)
        else:
            display_image(image)

    elif args.command == "hotspot":
        image = create_hotspot_display_image(args.ssid, args.password, args.ip)
        if args.save:
            save_image(image, args.save)
        else:
            display_image(image)

    elif args.command == "clear":
        image = Image.new("1", DISPLAY_SIZE, 255)
        if args.save:
            save_image(image, args.save)
        else:
            display_image(image)

    elif args.command == "handoff-splash":
        import json  # noqa: PLC0415

        settings = json.loads(args.settings_json)
        if args.save:
            save_image(create_handoff_splash_image(settings, args.qr_url), args.save)
        else:
            # Propagate the paint result as the EXIT CODE (/review): display_image
            # returns False (not raises) on "No display available" / a caught
            # hardware fault, so without this the process would exit 0 and the
            # calling control_server would believe the splash painted when the
            # e-ink shows nothing — defeating the point of this subprocess split.
            if not display_handoff_splash(settings, args.qr_url):
                sys.exit(1)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
