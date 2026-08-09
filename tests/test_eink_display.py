"""Tests for eink_display module — QR code generation (no hardware needed)."""

import sys

import pytest
from PIL import Image

# eink_display imports qrcode at module level and calls setup_logging(),
# which is fine. But waveshare_epd is only imported inside get_display().
import eink_display
from eink_display import create_qr_display_image, generate_qr_image


class TestHandoffSplashCliExitCode:
    """litclock-dev#388/litclock-dev#484 (/review): the `handoff-splash` CLI must exit NONZERO when the
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


class TestSetupSplashCopy:
    """litclock-dev#555. The setup splash is the recipient's first contact
    with the product, on a device with no keyboard, where the e-ink panel is
    the only instruction surface. "Hotspot" reads there as an instruction
    about the user's OWN phone — iOS Settings > Personal Hotspot, Android's
    "Hotspot & tethering" — which is a thing they switch on to share their
    connection, close to the opposite of what this screen means. The failure
    isn't a degraded experience, it's the user hunting through their own
    settings while setup never starts.
    """

    def test_labels_do_not_say_hotspot(self):
        assert "hotspot" not in eink_display.SETUP_LABEL_NETWORK.lower()
        assert "hotspot" not in eink_display.SETUP_LABEL_PASSWORD.lower()

    def test_labels_say_the_words_they_are_supposed_to_say(self):
        """Adversarial review built `SETUP_LABEL_NETWORK = "Clock's Netwrok:"`
        — 16 chars, contains "clock", no "hotspot" — and every test passed.
        A typo in the most-read word on the panel has to be catchable."""
        assert eink_display.SETUP_LABEL_NETWORK == "LitClock's WiFi network:"
        assert eink_display.SETUP_LABEL_PASSWORD == "LitClock's WiFi password:"

    def test_labels_still_disambiguate_whose_network_this_is(self):
        """Dropping the jargon must not drop the distinction it carried. The
        retry screen shows these values directly above a form asking for the
        user's OWN WiFi name and password; unqualified "Network:" /
        "Password:" would be ambiguous exactly where it costs most.

        Both labels name the network out loud. Two independent non-technical
        readers walked this panel and neither realised two networks existed;
        the password label in particular ("Clock's Password:") was read as a
        PIN belonging to the clock and stored rather than typed. Tying both
        labels to "LitClock's WiFi" is what makes the password an input to
        that specific network instead of a possession of the device.
        """
        for label in (eink_display.SETUP_LABEL_NETWORK, eink_display.SETUP_LABEL_PASSWORD):
            assert "litclock" in label.lower(), label
            assert "wifi" in label.lower(), label
        assert "password" in eink_display.SETUP_LABEL_PASSWORD.lower()
        assert "network" in eink_display.SETUP_LABEL_NETWORK.lower()

    def test_labels_fit_the_panel_measured_not_counted(self):
        """Character count is NOT width in a proportional font — the earlier
        version of this test asserted len() and the CHANGELOG repeated the
        claim, which guaranteed nothing. Measure the real render.

        The labels are drawn left-aligned at text_x = qr_x + qr_size + 30 =
        290 on an 800px panel, so the budget is 510px.

        This asserts the budget only. An earlier version also required each
        label to be no wider than the "Hotspot ..." string it replaced, which
        was a proxy for "the layout cannot have moved" that only held while
        the labels kept getting shorter. The shipped labels are deliberately
        WIDER than what they replaced (285px and 298px against 208px and
        220px) because they now name the network, so the monotonic check
        would block a wording decision without measuring anything real. The
        510px budget is the actual constraint and both clear it.
        """
        from PIL import Image, ImageDraw, ImageFont

        draw = ImageDraw.Draw(Image.new("1", eink_display.DISPLAY_SIZE, 255))
        font = ImageFont.truetype(eink_display.FONT_PATH_BOLD, 22)
        budget = eink_display.DISPLAY_SIZE[0] - 290
        for label in (eink_display.SETUP_LABEL_NETWORK, eink_display.SETUP_LABEL_PASSWORD):
            width = draw.textbbox((0, 0), label, font=font)[2]
            assert width < budget, f"{label!r} is {width}px, over the {budget}px budget"

    def test_instruction_lines_fit_the_panel(self):
        """The block is centred on its widest line and the lines are
        left-aligned within it, so the whole 800px is the budget."""
        from PIL import Image, ImageDraw, ImageFont

        draw = ImageDraw.Draw(Image.new("1", eink_display.DISPLAY_SIZE, 255))
        font = ImageFont.truetype(eink_display.FONT_PATH, 18)
        for is_retry in (False, True):
            for line in eink_display.setup_instruction_lines("192.168.100.100", is_retry=is_retry):
                width = draw.textbbox((0, 0), line, font=font)[2]
                assert width < eink_display.DISPLAY_SIZE[0], f"{line!r} is {width}px"

    @pytest.mark.parametrize("is_retry", [False, True])
    def test_instruction_lines_do_not_say_hotspot(self, is_retry):
        lines = eink_display.setup_instruction_lines("10.42.0.1", is_retry=is_retry)
        assert not any("hotspot" in line.lower() for line in lines), lines

    @pytest.mark.parametrize("is_retry", [False, True])
    def test_no_punctuation_a_reader_could_mistake_for_the_address(self, is_retry):
        """The fallback used to read "litclock.setup  |  10.42.0.1". A pipe
        between two URL-shaped strings does not say "or" to someone who has
        never seen one used that way — it looks like part of what to type,
        and they type it. Alternatives are separated by the word.
        """
        lines = eink_display.setup_instruction_lines("10.42.0.1", is_retry=is_retry)
        assert not any("|" in line for line in lines), lines
        fallback = [ln for ln in lines if eink_display.SETUP_HOSTNAME in ln]
        assert fallback, lines
        assert all(" or " in ln for ln in fallback), fallback

    @pytest.mark.parametrize("is_retry", [False, True])
    def test_typable_addresses_carry_a_scheme(self, is_retry):
        """ ".setup" is not a public TLD, so a bare "litclock.setup" is not
        confidently a hostname to a phone browser — Chrome ranks a Google
        search above it, that is the biggest tap target, and the search then
        succeeds over cellular and returns junk. Worse than an error,
        because the phone looks fine and the clock looks broken. A scheme
        makes it a navigation.
        """
        lines = eink_display.setup_instruction_lines("10.42.0.1", is_retry=is_retry)
        blob = " ".join(lines)
        assert f"http://{eink_display.SETUP_HOSTNAME}" in blob, lines
        assert "http://10.42.0.1" in blob, lines

    @pytest.mark.parametrize("is_retry", [False, True])
    def test_no_tilde_and_no_page_title_collision(self, is_retry):
        """Two separate reader failures. The tilde in "~20s" reads as a stray
        mark, and on e-ink a stray mark is plausible. And the panel used to
        quote the setup page's title, "LitClock Setup", three lines under the
        network name "LitClock-Setup" — near-identical strings for different
        things, and a reader went hunting their WiFi list for the page title.
        """
        lines = eink_display.setup_instruction_lines("10.42.0.1", is_retry=is_retry)
        blob = " ".join(lines)
        assert "~" not in blob, lines
        # The hyphenated SSID may appear; the spaced page title must not.
        assert "LitClock Setup" not in blob, lines

    def test_framing_line_states_the_missing_concept(self):
        """Neither reader knew a device can broadcast its own network ("I
        didn't buy the clock a WiFi"). Retry omits it — by then they have
        joined the network once and the headline is carrying other news.
        """
        # Exact string, not a substring. The sibling test above exists
        # because "Clock's Netwrok:" passed every property check; the same
        # standard has to apply to new copy. Mutating this constant to
        # "The clcok makes its own WiFi for setpu." passed the whole file
        # when the only assertion here was `"own wifi" in ...lower()`.
        assert eink_display.SETUP_FRAMING_LINE == "The clock makes its own WiFi for setup."

        first = eink_display.create_hotspot_display_image("LitClock-Setup", "pw", "10.42.0.1")
        retry = eink_display.create_hotspot_display_image(
            "LitClock-Setup", "pw", "10.42.0.1", retry_reason=eink_display.HOTSPOT_RETRY_WIFI_PASSWORD
        )
        # Band derived from the renderer's own constant, not re-hardcoded.
        # Count black pixels explicitly: in mode "1" white is 1, so getbbox()
        # reports a BLANK band as full-size and would pass either way.
        band = (0, eink_display.SETUP_FRAMING_Y, eink_display.DISPLAY_SIZE[0], eink_display.SETUP_FRAMING_Y + 24)
        assert _count_black_pixels(first.crop(band)) > 0, "framing line missing on first run"
        assert _count_black_pixels(retry.crop(band)) == 0, "framing line should not paint on retry"

    def test_framing_line_clears_the_instruction_block(self):
        """The framing line is anchored under the QR and the instruction
        block grows UPWARD from the panel edge, so the two close on each
        other as lines are added. Nothing caught that: the band assertion
        above is satisfied by a colliding line just as well as a clear one.

        Assert the real clearance for every line count the renderer actually
        produces. Today that is 3 (retry) and 4 (first run), which clear by
        34px and 6px. A fifth line would put the block top at y=320 against
        framing ink ending at 342, so adding one makes THIS test fail rather
        than silently overlapping on a panel nobody looks at until a
        stranger is holding the device.
        """
        from PIL import Image, ImageDraw, ImageFont

        draw = ImageDraw.Draw(Image.new("1", eink_display.DISPLAY_SIZE, 255))
        font = ImageFont.truetype(eink_display.FONT_PATH, 18)
        framing_bottom = draw.textbbox((0, eink_display.SETUP_FRAMING_Y), eink_display.SETUP_FRAMING_LINE, font=font)[3]

        produced = {len(eink_display.setup_instruction_lines("10.42.0.1", is_retry=r)) for r in (False, True)}
        for count in sorted(produced):
            top = eink_display.instruction_block_top(count)
            assert framing_bottom < top, (
                f"framing line (ink to y={framing_bottom}) collides with a {count}-line "
                f"instruction block (top y={top}) — move SETUP_FRAMING_Y or shorten the block"
            )

    def test_quiet_zone_constant_actually_satisfies_the_spec(self):
        """The guard below derives its bands from SETUP_QR_QUIET_ZONE_PX, so
        the guard alone cannot tell whether that constant is big enough —
        shrinking it to 2 shrinks the bands and everything still passes.
        Check the constant against the geometry it is supposed to encode.

        ISO 18004 wants 4 modules of blank around the symbol.
        generate_qr_image bakes 2 in via border=2, so the panel supplies the
        other 2. Module size = SETUP_QR_SIZE / (modules + 2*border), and the
        SHORTEST payload gives the FEWEST modules, hence the biggest module,
        hence the strictest requirement.
        """
        import qrcode

        border = 2
        spec_modules = 4
        worst = 0.0
        for ssid, password in (("", ""), ("x", "y"), ("LitClock-Setup", "clockwis"), ("LitClock-Setup", "clockwise42")):
            qr = qrcode.QRCode(border=border)
            qr.add_data(f"WIFI:T:WPA;S:{ssid};P:{password};;")
            qr.make(fit=True)
            module_px = eink_display.SETUP_QR_SIZE / (qr.modules_count + 2 * border)
            worst = max(worst, (spec_modules - border) * module_px)
        assert eink_display.SETUP_QR_QUIET_ZONE_PX >= worst, (
            f"SETUP_QR_QUIET_ZONE_PX is {eink_display.SETUP_QR_QUIET_ZONE_PX}px but the "
            f"shortest credentials need {worst:.1f}px of blank outside the baked-in border"
        )

    def test_nothing_intrudes_on_the_qr_quiet_zone(self):
        """The framing line used to be centred directly under the title,
        where its left end reached 23px into the QR block and dropped 17 ink
        pixels in the quiet zone. A QR's quiet zone has to stay blank or
        scanning gets unreliable, which on this screen means setup cannot
        start at all.

        Guard all four sides, derived from the renderer's constants. The
        first version of this test hardcoded the geometry and an 8px margin:
        8px is less than half the real requirement, so it passed a genuine
        3.3px spec violation (SETUP_FRAMING_Y = 302), and re-hardcoding the
        QR position means it would keep passing while asserting a blank
        corner of the panel after the QR moved.

        Credentials are swept because the payload length sets the QR
        version, which sets the module size, which sets how much blank the
        spec wants. Shorter credentials mean BIGGER modules and a bigger
        requirement, so the empty case is the strict one.
        """
        qx, qy, qs = eink_display.SETUP_QR_X, eink_display.SETUP_QR_Y, eink_display.SETUP_QR_SIZE
        margin = eink_display.SETUP_QR_QUIET_ZONE_PX
        for ssid, password in (("", ""), ("LitClock-Setup", "clockwis"), ("LitClock-Setup", "clockwise42")):
            for retry_reason in (None, eink_display.HOTSPOT_RETRY_WIFI_PASSWORD):
                img = eink_display.create_hotspot_display_image(
                    ssid or "x", password or "y", "10.42.0.1", retry_reason=retry_reason
                )
                boxes = {
                    "above": (qx, qy - margin, qx + qs, qy),
                    "below": (qx, qy + qs, qx + qs, qy + qs + margin),
                    "left": (qx - margin, qy, qx, qy + qs),
                    "right": (qx + qs, qy, qx + qs + margin, qy + qs),
                }
                for name, box in boxes.items():
                    assert _count_black_pixels(img.crop(box)) == 0, (
                        f"ink in the QR quiet zone ({name}), retry={retry_reason}, ssid={ssid!r}"
                    )

    @pytest.mark.parametrize("is_retry", [False, True])
    def test_instructions_name_no_os_specific_gesture(self, is_retry):
        """Step 3 used to be "Swipe down (top-right) - tap WiFi", which is
        iOS Control Centre. An Android reader swiping there finds a
        different panel or nothing, on the one screen where being stuck
        means setup never starts. Name the destination, not the gesture:
        every phone has WiFi settings, none reach them the same way.
        """
        lines = eink_display.setup_instruction_lines("10.42.0.1", is_retry=is_retry)
        blob = " ".join(lines).lower()
        for gesture in ("swipe", "control cent", "top-right", "top right", "pull down"):
            assert gesture not in blob, f"{gesture!r} is OS-specific: {lines}"

    @pytest.mark.parametrize("is_retry", [False, True])
    def test_instructions_point_at_the_qr_and_a_typable_fallback(self, is_retry):
        """Both variants must keep the QR shortcut AND the browser fallback.
        The QR handles most people; the words are the fallback for when it
        doesn't fire, which is precisely the case where clarity matters.

        Adversarial review gutted this block to a single line and every
        assertion here still passed, because they only asked whether SOME
        line contained each token. Pin the shape and the hostname too —
        SETUP_HOSTNAME is what the function's own docstring calls the point
        of the block, and nothing asserted it survived."""
        lines = eink_display.setup_instruction_lines("10.42.0.1", is_retry=is_retry)
        assert len(lines) == (3 if is_retry else 4), lines
        assert any("QR" in line for line in lines)
        assert any("10.42.0.1" in line for line in lines)
        assert any(eink_display.SETUP_HOSTNAME in line for line in lines), lines
        # Numbered steps, in order, so a reordering or a dropped step shows up.
        #
        # Derive the filter from the data. It used to be a literal tuple
        # ("1.", "2.", "3."), written when the 4th first-run line was an
        # unnumbered continuation. Promoting that line to "4." left the
        # filter blind to it: renumbering step 4 to "9." passed, and so did
        # SWAPPING steps 3 and 4 — the exact reordering this assertion's own
        # comment promises to catch. Every line in both variants is numbered
        # now, so require that too.
        numbered = [ln for ln in lines if ln[:1].isdigit() and ln[1:2] == "."]
        assert len(numbered) == len(lines), f"every line should be a numbered step: {lines}"
        assert [ln[0] for ln in numbered] == [str(i + 1) for i in range(len(numbered))], lines

    def test_instruction_copy_is_pinned_exactly(self):
        """Property checks alone let typos ship. Every line in this block was
        rewritten and only properties were asserted (no pipe, no tilde, has
        http://, numbered, fits the panel) — mutating
        "2. Wait about 20 seconds for a setup page" to
        "2. Wiat abuot 20 secnods for a setpu paeg" passed the entire file.

        This is the highest-stakes copy in the product, so pin it verbatim.
        Changing it should be a deliberate edit here, not a silent one.
        """
        assert eink_display.setup_instruction_lines("10.42.0.1") == [
            "1. Scan the QR code to join LitClock-Setup",
            "2. Wait about 20 seconds for a setup page",
            "3. No page? Join LitClock-Setup in your WiFi settings",
            "4. Then open a browser: http://litclock.setup or http://10.42.0.1",
        ]
        assert eink_display.setup_instruction_lines("10.42.0.1", is_retry=True) == [
            "1. Rescan the QR code to rejoin LitClock-Setup",
            "2. Select your internet WiFi network, type your WiFi password",
            "3. No page? Open a browser: http://litclock.setup or http://10.42.0.1",
        ]

    def test_instruction_lines_share_one_left_edge(self):
        """The block is centred but the lines inside it are left-aligned, so
        the step numbers stack in a column — a reader at arm's length loses
        their place descending a ragged list. Reverting to per-line centring
        passed every other test in this file, so assert the rendered result.

        Tolerance, not equality: every line is drawn at the same x, but the
        left side bearing of the leading glyph differs slightly between
        digits, so the first INK column varies by a pixel. Per-line centring
        spreads it across ~90px ([227, 230, 180, 140] for the current copy),
        so 3px separates the two layouts with room to spare.
        """
        img = eink_display.create_hotspot_display_image("LitClock-Setup", "clockwis", "10.42.0.1")
        lines = eink_display.setup_instruction_lines("10.42.0.1")
        top = eink_display.instruction_block_top(len(lines))
        first_ink_columns = []
        for i in range(len(lines)):
            row_band = img.crop(
                (
                    0,
                    top + i * eink_display.HOTSPOT_INFO_LINE_HEIGHT,
                    eink_display.DISPLAY_SIZE[0],
                    top + (i + 1) * eink_display.HOTSPOT_INFO_LINE_HEIGHT,
                )
            )
            bbox = row_band.point(lambda px: 255 - px).getbbox()  # invert so ink is non-zero
            assert bbox is not None, f"instruction line {i + 1} painted nothing"
            first_ink_columns.append(bbox[0])
        spread = max(first_ink_columns) - min(first_ink_columns)
        assert spread <= 3, f"lines are not left-aligned: first ink at x={first_ink_columns} (spread {spread}px)"

    def test_first_run_instructions_name_the_network_to_join(self):
        """ "Join LitClock-Setup" is the whole instruction — the user has to
        match that string against their phone's WiFi list."""
        lines = eink_display.setup_instruction_lines("10.42.0.1")
        assert any("LitClock-Setup" in line for line in lines)

    def test_splash_still_renders_after_the_copy_extraction(self):
        """The copy moved to module level; the splash must still paint. A
        NameError here would leave a blank panel and no way to set up.

        Asserting image.size was a tautology — Image.new(DISPLAY_SIZE)
        guarantees it, so a splash that drew nothing at all passed. Count
        ink instead."""
        image = eink_display.create_hotspot_display_image("LitClock-Setup", "abcd1234", "10.42.0.1")
        assert image.size == eink_display.DISPLAY_SIZE
        assert _count_black_pixels(image) > 5000, "splash is blank or nearly blank"

    def test_retry_splash_still_renders(self):
        image = eink_display.create_hotspot_display_image(
            "LitClock-Setup", "abcd1234", "10.42.0.1", retry_reason=eink_display.HOTSPOT_RETRY_WIFI_PASSWORD
        )
        assert image.size == eink_display.DISPLAY_SIZE
        assert _count_black_pixels(image) > 5000, "retry splash is blank or nearly blank"

    def test_the_splash_actually_draws_the_constants(self):
        """Codex caught this: every other test here reads the constants and
        the helper directly, so create_hotspot_display_image could regress to
        drawing a literal "Hotspot Network:" — or omit the labels entirely —
        and the whole file would stay green. Capture what reaches the canvas."""
        from PIL import ImageDraw

        drawn = []
        original = ImageDraw.ImageDraw.text

        def spy(self, xy, text, *args, **kwargs):
            drawn.append(text)
            return original(self, xy, text, *args, **kwargs)

        ImageDraw.ImageDraw.text = spy
        try:
            eink_display.create_hotspot_display_image("LitClock-Setup", "abcd1234", "10.42.0.1")
        finally:
            ImageDraw.ImageDraw.text = original

        assert eink_display.SETUP_LABEL_NETWORK in drawn
        assert eink_display.SETUP_LABEL_PASSWORD in drawn
        for line in eink_display.setup_instruction_lines("10.42.0.1"):
            assert line in drawn
        assert not any("hotspot" in str(t).lower() for t in drawn), drawn

    def test_retry_splash_also_draws_only_the_new_copy(self):
        from PIL import ImageDraw

        drawn = []
        original = ImageDraw.ImageDraw.text

        def spy(self, xy, text, *args, **kwargs):
            drawn.append(text)
            return original(self, xy, text, *args, **kwargs)

        ImageDraw.ImageDraw.text = spy
        try:
            eink_display.create_hotspot_display_image(
                "LitClock-Setup", "abcd1234", "10.42.0.1", retry_reason=eink_display.HOTSPOT_RETRY_WIFI_PASSWORD
            )
        finally:
            ImageDraw.ImageDraw.text = original

        assert not any("hotspot" in str(t).lower() for t in drawn), drawn

    def test_retry_instructions_name_the_network_too(self):
        """The retry screen is the one where the phone has definitely been
        kicked off, so it is the one that most needs the string the user has
        to match in their WiFi list. It was the only variant naming nothing."""
        lines = eink_display.setup_instruction_lines("10.42.0.1", is_retry=True)
        assert any("LitClock-Setup" in line for line in lines), lines
