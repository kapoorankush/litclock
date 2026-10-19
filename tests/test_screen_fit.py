"""litclock-dev#532 RT4 (design D7, eng R16, Codex O6): the screen-fit matrix.

Every e-ink surface whose copy comes from the catalog, rendered in every
registered language AND a test-only pseudo-locale, must show all of its copy:

- no line cut or ellipsised, and no translated line break folded away — the
  renderers report these through ``eink_display.recording_fits`` (shrinking a
  line, or wrapping the caveat, is a fit, not a loss, and is allowed);
- every piece of text on the panel;
- no two pieces of text overlapping each other (O6);
- nothing inside the setup QR or its quiet zone, and nothing from the handoff
  splash's left lane reaching its QR column.

English emits no fit event at all — its frames are unchanged by RT4.

The pseudo-locale is English with every word 35% longer and accented
(Latin-1 only, so it renders in Literata), slots and rich tokens kept
intact. It is never shipped or registered: it stands in for the first real
translation, so a layout that cannot take a longer language fails HERE
rather than on a recipient's panel (R16). English must pass unchanged.
"""

from __future__ import annotations

import json
import math
import re
from itertools import combinations
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

import eink_display as e
import literary_clock as lc
import strings_catalog
import wifi_provision

REPO = Path(__file__).resolve().parent.parent
EN = json.loads((REPO / "languages" / "en" / "strings.json").read_text(encoding="utf-8"))
REGISTRY = json.loads((REPO / "languages.json").read_text(encoding="utf-8"))

PSEUDO_GROWTH = 0.35
_ACCENTED = str.maketrans("aeiouAEIOUcnyCNY", "áéíóúÁÉÍÓÚçñýÇÑÝ")
_TOKEN = re.compile(r"(\{/?[a-z][a-z0-9_]*\})")


def pseudo(value: str) -> str:
    """English, 35% longer per word and accented; slots and tokens intact."""
    out = []
    for part in _TOKEN.split(value):
        if _TOKEN.fullmatch(part):
            out.append(part)
            continue
        words = []
        for word in part.split(" "):
            grown = word.translate(_ACCENTED)
            words.append(grown + "ö" * math.ceil(len(word) * PSEUDO_GROWTH) if word else word)
        out.append(" ".join(words))
    return "".join(out)


def _pseudo_bundle() -> dict[str, str]:
    return {k: v if k.startswith("_") else pseudo(v) for k, v in EN.items()}


LANGUAGES = sorted(REGISTRY["languages"]) + ["pseudo"]


@pytest.fixture(params=LANGUAGES)
def language(request, tmp_path, monkeypatch):
    """The device set to one row of the matrix."""
    code = request.param
    monkeypatch.setenv("LITCLOCK_ENV_FILE", str(tmp_path / "no-env.sh"))
    if code == "pseudo":
        for lang, data in (("en", EN), ("xx", _pseudo_bundle())):
            target = tmp_path / "languages" / lang / "strings.json"
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        registry = json.loads(json.dumps(REGISTRY))
        registry["languages"]["en"]["strings"] = "languages/en/strings.json"
        registry["languages"]["xx"] = dict(
            registry["languages"]["en"], code="xx", native_name="Pseudo", strings="languages/xx/strings.json"
        )
        (tmp_path / "languages.json").write_text(json.dumps(registry), encoding="utf-8")
        monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", tmp_path / "languages.json")
        monkeypatch.setattr(strings_catalog, "_REPO_ROOT", tmp_path)
        code = "xx"
    monkeypatch.setenv("LITCLOCK_LANGUAGE", code)
    strings_catalog.reset_cache()
    snap = strings_catalog.snapshot()
    assert snap.usable and snap.language == code, f"{code}'s bundle is not usable — the matrix would test English"
    yield snap
    strings_catalog.reset_cache()


@pytest.fixture
def drawn(monkeypatch):
    """(text, bbox) for every string drawn, measured with the draw's own args."""
    boxes: list[tuple[str, tuple[int, int, int, int]]] = []
    real_text, real_multi = ImageDraw.ImageDraw.text, ImageDraw.ImageDraw.multiline_text
    inside = []  # Pillow's multiline_text draws through text(); count it once

    def text(self, xy, s, fill=None, font=None, anchor=None, *a, **k):
        if s and not inside:
            boxes.append((s, self.textbbox(xy, s, font=font, anchor=anchor)))
        return real_text(self, xy, s, fill, font, anchor, *a, **k)

    def multiline_text(self, xy, s, fill=None, font=None, anchor=None, spacing=4, align="left", *a, **k):
        if s:
            boxes.append((s, self.multiline_textbbox(xy, s, font=font, anchor=anchor, spacing=spacing, align=align)))
        inside.append(1)
        try:
            return real_multi(self, xy, s, fill, font, anchor, spacing, align, *a, **k)
        finally:
            inside.pop()

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", text)
    monkeypatch.setattr(ImageDraw.ImageDraw, "multiline_text", multiline_text)
    return boxes


def _overlaps(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _check_frame(name, events, boxes, keep_out=()):
    lost = [ev for ev in events if ev[2] in ("truncated", "collapsed")]
    assert not lost, f"{name}: copy lost — {lost}"
    w, h = e.DISPLAY_SIZE
    off = [(t, b) for t, b in boxes if b[0] < 0 or b[1] < 0 or b[2] > w or b[3] > h]
    assert not off, f"{name}: text off the panel — {off}"
    # Dotted leaders run between a row's label and value by design.
    texts = [(t, b) for t, b in boxes if set(t) != {"."}]
    clashes = [(s, t) for (s, a), (t, b) in combinations(texts, 2) if _overlaps(a, b)]
    assert not clashes, f"{name}: text overlapping text — {clashes}"
    for region in keep_out:
        inside = [t for t, b in boxes if _overlaps(b, region)]
        assert not inside, f"{name}: text inside a QR or its quiet zone — {inside}"


# ── Status splashes: every catalog triplet ─────────────────────────────────

_SLOT_SAMPLES = {"ssid": "HomeNet", "attempt": "2", "max": "5"}
_PREFIXES = sorted({k.rsplit(".", 1)[0] for k in EN if ".splash." in k and k.endswith(".title")})


def test_the_matrix_covers_the_splash_prefixes():
    assert len(_PREFIXES) >= 15, _PREFIXES
    assert "firstboot.splash.connecting" in _PREFIXES


@pytest.mark.parametrize("prefix", _PREFIXES)
def test_status_splashes_fit(language, drawn, prefix):
    title, message, submessage = language.get_triplet(prefix, **_SLOT_SAMPLES)
    with e.recording_fits() as events:
        e.create_status_image(title, message or None, submessage or None)
    _check_frame(f"{language.language}:{prefix}", events, drawn)
    if language.language == "en":
        assert events == []


# ── The farewell lines (T7): every line, on its own screen ─────────────────

_FAREWELL_PART = {"welcome": "submessage", "reboot": "submessage", "poweroff": "message"}


@pytest.mark.parametrize("screen", sorted(_FAREWELL_PART))
def test_every_farewell_line_fits_its_screen(language, drawn, screen):
    title, message, submessage = language.get_triplet(f"shutdown.splash.{screen}")
    lines = language.numbered(f"shutdown.farewell.{screen}")
    assert lines, f"{language.language} has no {screen} farewell"
    for line in lines:
        drawn.clear()
        parts = {"message": message, "submessage": submessage, _FAREWELL_PART[screen]: line}
        with e.recording_fits() as events:
            e.create_status_image(title, parts["message"] or None, parts["submessage"] or None)
        _check_frame(f"{language.language}:{screen} farewell {line!r}", events, drawn)
        if language.language == "en":
            assert events == []


# ── The setup (hotspot) splash ──────────────────────────────────────────────

_PASSWORD = "Ab3xYz9qKmNpQrTu"[: wifi_provision.HOTSPOT_PASSWORD_LENGTH]
_SETUP_QR = (
    e.SETUP_QR_X - e.SETUP_QR_QUIET_ZONE_PX,
    e.SETUP_QR_Y - e.SETUP_QR_QUIET_ZONE_PX,
    e.SETUP_QR_X + e.SETUP_QR_SIZE + e.SETUP_QR_QUIET_ZONE_PX,
    e.SETUP_QR_Y + e.SETUP_QR_SIZE + e.SETUP_QR_QUIET_ZONE_PX,
)


@pytest.mark.parametrize("reason", [None, e.HOTSPOT_RETRY_WIFI_PASSWORD, e.HOTSPOT_RETRY_CONNECT_FAILED])
def test_the_setup_splash_fits(language, drawn, reason):
    with e.recording_fits() as events:
        e.create_hotspot_display_image(wifi_provision.DEFAULT_SSID, _PASSWORD, "10.42.0.1", retry_reason=reason)
    _check_frame(f"{language.language}:hotspot/{reason}", events, drawn, keep_out=[_SETUP_QR])
    if language.language == "en":
        assert events == []


# ── The handoff splash ──────────────────────────────────────────────────────

_HANDOFF_QR = (
    e.HANDOFF_RIGHT_COLUMN_X,
    e.HANDOFF_QR_Y,
    e.DISPLAY_SIZE[0],
    e.HANDOFF_QR_Y + e.HANDOFF_QR_SIZE,
)


@pytest.mark.parametrize("has_location", [True, False])
@pytest.mark.parametrize("units", ["imperial", "metric"])
def test_the_handoff_splash_fits(language, drawn, has_location, units):
    settings = {
        "has_location": has_location,
        "location_name": "Austin, Texas",
        "timezone": "America/Chicago",
        "units": units,
        "mature_enabled": units == "metric",
        "connected_ssid": "HomeNet",
    }
    with e.recording_fits() as events:
        e.create_handoff_splash_image(settings, "http://192.168.1.50")
    # The QR's own column is allowed its caption, caveat and SSID; nothing
    # from the left lane may reach it.
    left = [(t, b) for t, b in drawn if b[0] < e.HANDOFF_RIGHT_COLUMN_X]
    _check_frame(f"{language.language}:handoff/{has_location}/{units}", events, drawn)
    if language.language == "en":
        assert events == []
    reach = [t for t, b in left if _overlaps(b, _HANDOFF_QR)]
    assert not reach, f"{language.language}: left-lane text reaching the QR column — {reach}"


# ── The plain-time frame's line ─────────────────────────────────────────────


@pytest.mark.parametrize("reason", [lc.FALLBACK_GAP, lc.FALLBACK_FAILURE])
def test_the_plain_time_line_is_drawn_not_omitted(language, drawn, reason):
    image = Image.new("1", lc.DISPLAY_SIZE, 255)
    lc._draw_fallback_line(ImageDraw.Draw(image), lc.DISPLAY_SIZE[0] // 2, reason)
    want = language.get(lc.FALLBACK_LINE_KEYS[reason])
    assert [t for t, _b in drawn] == [want], f"{language.language}: the {reason} line was omitted"
    _check_frame(f"{language.language}:plain-time/{reason}", [], drawn)


# ── The pseudo-locale itself ────────────────────────────────────────────────


def test_the_pseudo_locale_is_longer_and_keeps_every_slot():
    import catalog_lint

    bundle = _pseudo_bundle()
    english_chars = {ch for v in EN.values() for ch in v}
    for key, value in EN.items():
        if key.startswith("_"):
            continue
        assert catalog_lint.slot_counts(bundle[key]) == catalog_lint.slot_counts(value), key
        assert _TOKEN.findall(bundle[key]) == _TOKEN.findall(value), key
        text, grown = _TOKEN.sub("", value), _TOKEN.sub("", bundle[key])
        if len(text.replace(" ", "")) >= 10:
            assert len(grown) >= len(text) * 1.3, key
    added = {ch for v in bundle.values() for ch in v} - english_chars
    assert added and all(ord(ch) < 256 for ch in added), added  # Literata draws Latin-1
    assert pseudo("Ready to read.") == "Réádýöö tóö réád.öö"


# ── The fit primitives ──────────────────────────────────────────────────────


def _draw():
    return ImageDraw.Draw(Image.new("1", e.DISPLAY_SIZE, 255))


def test_fit_line_returns_the_same_font_when_the_line_fits():
    """Byte-identical English: a line that fits gets the caller's own font
    object back and no event."""
    from PIL import ImageFont

    font = ImageFont.truetype(e.FONT_PATH_BOLD, 22)
    with e.recording_fits() as events:
        text, fitted = e._fit_line("Ready", font, _draw(), 400, "f", "s", 16)
    assert (text, fitted, events) == ("Ready", font, [])


def test_fit_line_shrinks_before_it_cuts():
    from PIL import ImageFont

    font = ImageFont.truetype(e.FONT_PATH_BOLD, 22)
    long = "Your settings — auto-detected and double-checked:"
    with e.recording_fits() as events:
        text, fitted = e._fit_line(long, font, _draw(), 480, "settings label", "handoff splash", 16)
    assert text == long and 16 <= fitted.size < 22
    assert [ev[2] for ev in events] == ["shrunk"]
    with e.recording_fits() as events:
        text, fitted = e._fit_line(long * 3, font, _draw(), 480, "settings label", "handoff splash", 16)
    assert text.endswith("…") and fitted.size == 16
    assert [ev[2] for ev in events] == ["truncated"]


def test_fit_line_reports_a_folded_line_break():
    from PIL import ImageFont

    font = ImageFont.truetype(e.FONT_PATH, 18)
    with e.recording_fits() as events:
        text, _ = e._fit_line("one\ntwo", font, _draw(), 400, "tip", "handoff splash", 14)
    assert text == "one two" and [ev[2] for ev in events] == ["collapsed"]


def test_recording_is_off_outside_the_block():
    assert e._FIT_EVENTS is None
    with e.recording_fits() as outer:
        e._fit_event("s", "f", "truncated", "x")
        with e.recording_fits() as inner:
            e._fit_event("s", "f", "shrunk", "y")
        e._fit_event("s", "f", "collapsed", "z")
    assert [ev[2] for ev in outer] == ["truncated", "collapsed"] and [ev[2] for ev in inner] == ["shrunk"]
    assert e._FIT_EVENTS is None


def test_a_long_setup_title_shrinks_instead_of_being_cut(tmp_path, monkeypatch, drawn):
    """The hotspot title is on the fit ladder (RT4): a translation half again
    longer than the panel shrinks to fit, never loses its tail."""
    bundle = _pseudo_bundle()
    bundle["firstboot.hotspot.title_retry"] = "Wé côuldn't jóin thé WiFi nétwörk yóu chösé fór thís clöck"
    for lang, data in (("en", EN), ("xx", bundle)):
        target = tmp_path / "languages" / lang / "strings.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    registry = json.loads(json.dumps(REGISTRY))
    registry["languages"]["en"]["strings"] = "languages/en/strings.json"
    registry["languages"]["xx"] = dict(registry["languages"]["en"], code="xx", strings="languages/xx/strings.json")
    (tmp_path / "languages.json").write_text(json.dumps(registry), encoding="utf-8")
    monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", tmp_path / "languages.json")
    monkeypatch.setattr(strings_catalog, "_REPO_ROOT", tmp_path)
    monkeypatch.setenv("LITCLOCK_ENV_FILE", str(tmp_path / "no-env.sh"))
    monkeypatch.setenv("LITCLOCK_LANGUAGE", "xx")
    strings_catalog.reset_cache()
    try:
        with e.recording_fits() as events:
            e.create_hotspot_display_image(
                wifi_provision.DEFAULT_SSID, _PASSWORD, "10.42.0.1", retry_reason="wifi_password"
            )
        assert ("setup splash", "title", "shrunk", bundle["firstboot.hotspot.title_retry"]) in events
        _check_frame("long title", events, drawn, keep_out=[_SETUP_QR])
    finally:
        strings_catalog.reset_cache()


def test_every_loss_point_reports():
    """The primitives every surface funnels through each report what they
    gave up — the matrix is only as good as these."""
    from PIL import ImageFont

    font = ImageFont.truetype(e.FONT_PATH, 18)
    with e.recording_fits() as events:
        e._clamp_to_width("a\nb", font, _draw(), 400, "field", surface="s")
        e._clamp_to_width("W" * 200, font, _draw(), 400, "field", surface="s")
        e._wrap_title("one two three four five six seven", font, 60, 2)
        e._fit_ssid_to_band("W" * 80, font, _draw(), 100)
    assert [ev[2] for ev in events] == ["collapsed", "truncated", "truncated", "truncated"], events


def _caveat_frame(monkeypatch, caveat, drawn):
    """The handoff splash with the caveat replaced, every other string as in
    English."""
    real = e._splash_catalog

    class _Patched:
        def __init__(self, inner):
            self.inner = inner

        def get(self, key, /, **slots):
            return caveat if key == "firstboot.handoff.caveat" else self.inner.get(key, **slots)

    monkeypatch.setattr(e, "_splash_catalog", lambda lang=None: _Patched(real(lang)))
    settings = {
        "has_location": True,
        "location_name": "Austin, Texas",
        "timezone": "America/Chicago",
        "units": "imperial",
        "mature_enabled": False,
        "connected_ssid": "A-Thirty-Two-Character-SSID-Name",
    }
    with e.recording_fits() as events:
        e.create_handoff_splash_image(settings, "http://192.168.1.50")
    return events


def test_a_translated_caveat_break_is_honoured_not_folded(monkeypatch, drawn):
    events = _caveat_frame(monkeypatch, "Scan with your\nphone on:", drawn)
    assert "Scan with your" in [t for t, _ in drawn] and "phone on:" in [t for t, _ in drawn]
    assert [ev[2] for ev in events] == ["wrapped"], events
    _check_frame("caveat break", events, drawn)


def test_a_two_line_caveat_with_accents_does_not_collide(monkeypatch, drawn):
    """An accented capital under a descender: the caveat's pitch is the
    font's own line height, not the SSID's tighter one."""
    events = _caveat_frame(monkeypatch, "Skanna med din telefon på nätverket Åbo:", drawn)
    _check_frame("accented caveat", events, drawn)


def test_a_caveat_cut_is_reported_against_the_caveat(monkeypatch, drawn):
    events = _caveat_frame(monkeypatch, "one\ntwo\nthree", drawn)
    assert ("handoff splash", "caveat label", "truncated", "one\ntwo\nthree") in events
