"""litclock-dev#532 T6: the setup splashes speak the clock's language.

The hotspot / retry / connecting / handoff splashes painted hardcoded
English. Their copy now lives in the catalog, read through ONE resolution per
frame (RT1/O4), so a frame is wholly one language: the device's, an explicit
``lang=`` (the setup page's session language, RT3), or the fleet default
WHOLE when the requested bundle is unusable (R15).

R8: English output is unchanged. These tests capture every string a renderer
draws, so "English draws exactly today's copy" is checked at the draw call —
with geometry untouched, the same strings are the same pixels. (A one-off
render of all thirteen variants before and after was byte-identical; pixel
hashes are not pinned here because they move with the FreeType build.)
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from PIL import ImageDraw

import eink_display
import setup_server
import strings_catalog

REPO = Path(__file__).resolve().parent.parent
EN = json.loads((REPO / "languages" / "en" / "strings.json").read_text(encoding="utf-8"))

SSID, PASSWORD, IP = "LitClock-Setup", "abcd-efgh-1234", "10.42.0.1"
SETTINGS = {
    "has_location": True,
    "location_name": "Austin, Texas",
    "timezone": "America/Chicago",
    "units": "imperial",
    "mature_enabled": False,
    "connected_ssid": "HomeNet",
}
QR_URL = "http://192.168.1.50"
# Values drawn as they are, never translated: credentials, addresses, the
# detected settings, the brand wordmark.
VALUES = {SSID, PASSWORD, "LITCLOCK", "192.168.1.50", "HomeNet", "Austin, Texas", "America/Chicago"}


@pytest.fixture
def drawn(monkeypatch):
    """Every string drawn with ImageDraw.text, in order."""
    texts: list[str] = []
    real = ImageDraw.ImageDraw.text

    def spy(self, xy, text, *args, **kwargs):
        texts.append(text)
        return real(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", spy)
    return texts


def _registry(tmp_path, monkeypatch, *, device: str, xx: dict[str, str]):
    """A registry with English and a test language `xx`, the device set to
    `device`. `xx` is the xx bundle."""
    for code, bundle in (("en", EN), ("xx", xx)):
        target = tmp_path / "languages" / code / "strings.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    shutil.copy(REPO / "languages.json", tmp_path / "base-registry.json")
    registry = json.loads((tmp_path / "base-registry.json").read_text(encoding="utf-8"))
    xx_entry = dict(registry["languages"]["en"], code="xx", native_name="Xx", strings="languages/xx/strings.json")
    registry["languages"]["en"]["strings"] = "languages/en/strings.json"
    registry["languages"]["xx"] = xx_entry
    (tmp_path / "languages.json").write_text(json.dumps(registry), encoding="utf-8")
    monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", tmp_path / "languages.json")
    monkeypatch.setattr(strings_catalog, "_REPO_ROOT", tmp_path)
    monkeypatch.setenv("LITCLOCK_ENV_FILE", str(tmp_path / "no-env.sh"))
    monkeypatch.setenv("LITCLOCK_LANGUAGE", device)
    strings_catalog.reset_cache()


def _xx_bundle() -> dict[str, str]:
    """Every English value wrapped in «», slots and tokens intact."""
    return {k: v if k.startswith("_") else f"«{v}»" for k, v in EN.items()}


@pytest.fixture(autouse=True)
def _fresh_catalog(monkeypatch, tmp_path):
    monkeypatch.setenv("LITCLOCK_ENV_FILE", str(tmp_path / "no-env.sh"))
    monkeypatch.setenv("LITCLOCK_LANGUAGE", "en")
    strings_catalog.reset_cache()
    yield
    strings_catalog.reset_cache()


def _render_all(lang=None):
    for reason in (None, eink_display.HOTSPOT_RETRY_WIFI_PASSWORD, eink_display.HOTSPOT_RETRY_CONNECT_FAILED):
        eink_display.create_hotspot_display_image(SSID, PASSWORD, IP, retry_reason=reason, lang=lang)
    for has_location in (True, False):
        eink_display.create_handoff_splash_image({**SETTINGS, "has_location": has_location}, QR_URL, lang=lang)


def _copy(texts):
    """The drawn strings that are copy rather than values (or the dotted
    leaders between a row's label and its value)."""
    return [t for t in texts if t not in VALUES and set(t) != {"."}]


# ── R8: English is today's copy, exactly ───────────────────────────────────


HOTSPOT_FIRST = [
    "WiFi Setup",
    "The clock makes its own WiFi for setup.",
    "LitClock's WiFi network:",
    "LitClock's WiFi password:",
    "1. Scan the QR code to join LitClock-Setup",
    "2. Wait about 20 seconds for a setup page",
    "3. No page? Join LitClock-Setup in your WiFi settings",
    "4. Then open a browser: http://litclock.setup or http://10.42.0.1",
]
HOTSPOT_PASSWORD = [
    "Couldn't Join Your WiFi",
    "LitClock's WiFi network:",
    "LitClock's WiFi password:",
    "1. Rescan the QR code to rejoin LitClock-Setup",
    "2. Select your internet WiFi network, type your WiFi password",
    "3. No page? Open a browser: http://litclock.setup or http://10.42.0.1",
]
HOTSPOT_CONNECT = [
    "Couldn't Join Your WiFi",
    "LitClock's WiFi network:",
    "LitClock's WiFi password:",
    "1. Rescan the QR code to rejoin LitClock-Setup",
    "2. Check your WiFi is 2.4GHz and in range of the clock",
    "3. On the setup page, pick your network and try again",
    "4. No page? Open a browser: http://litclock.setup or http://10.42.0.1",
]


@pytest.mark.parametrize(
    ("reason", "want"),
    [
        (None, HOTSPOT_FIRST),
        (eink_display.HOTSPOT_RETRY_WIFI_PASSWORD, HOTSPOT_PASSWORD),
        (eink_display.HOTSPOT_RETRY_CONNECT_FAILED, HOTSPOT_CONNECT),
    ],
)
def test_english_hotspot_draws_exactly_todays_copy(drawn, reason, want):
    assert eink_display.SETUP_HOSTNAME == "litclock.setup"
    eink_display.create_hotspot_display_image(SSID, PASSWORD, IP, retry_reason=reason)
    assert _copy(drawn) == want


def test_english_handoff_draws_exactly_todays_copy(drawn):
    eink_display.create_handoff_splash_image(SETTINGS, QR_URL)
    assert _copy(drawn) == [
        "Scan with your phone on:",
        "Ready to read.",
        "Your settings — auto-detected:",
        "Location",
        "Timezone",
        "Units",
        "Imperial (°F)",
        "Mature quotes",
        "Off",
        "To change anything, scan the QR.",
        "Tip: this QR lives in the corner of every",
        "quote — scan it any time to return to settings.",
        "Quotes start shortly.",
    ]


def test_english_handoff_failure_and_metric_mature_variants(drawn):
    eink_display.create_handoff_splash_image(
        {**SETTINGS, "has_location": False, "units": "metric", "mature_enabled": True}, QR_URL
    )
    copy = _copy(drawn)
    for want in (
        "Almost ready.",
        "Not detected",
        "Metric (°C)",
        "On",
        "Scan the QR to set your timezone.",
        "Quotes start once your timezone is set.",
    ):
        assert want in copy, want
    assert copy.count("Not detected") == 2
    assert "Austin, Texas" not in drawn


def test_english_connecting_splash_is_todays_copy(monkeypatch):
    painted = []
    monkeypatch.setattr(eink_display, "display_status", lambda *a: painted.append(a))
    setup_server._show_connecting_splash("HomeNet")
    assert painted == [("Connecting to HomeNet…", "This can take up to a minute.")]


# ── A language other than English, whole ──────────────────────────────────


def test_a_device_language_paints_every_setup_splash_in_it(drawn, tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, device="xx", xx=_xx_bundle())
    _render_all()
    copy = _copy(drawn)
    assert len(copy) > 40
    untranslated = [t for t in copy if not t.startswith("«")]
    assert not untranslated, f"English on an xx device's splash: {untranslated}"
    # The slots still carry the values, untouched.
    assert "«1. Scan the QR code to join LitClock-Setup»" in copy
    assert "«4. Then open a browser: http://litclock.setup or http://10.42.0.1»" in copy


def test_an_explicit_language_overrides_the_device(drawn, tmp_path, monkeypatch):
    """RT3 hands the setup page's session language to the retry splash."""
    _registry(tmp_path, monkeypatch, device="en", xx=_xx_bundle())
    _render_all(lang="xx")
    assert all(t.startswith("«") for t in _copy(drawn))


def test_an_unusable_bundle_paints_the_fleet_default_whole(drawn, tmp_path, monkeypatch):
    """R15: one missing key makes the bundle unusable, and the WHOLE frame is
    the fleet default — never a frame that is part xx, part English."""
    bundle = _xx_bundle()
    del bundle["firstboot.handoff.tip_2"]
    _registry(tmp_path, monkeypatch, device="xx", xx=bundle)
    _render_all()
    copy = _copy(drawn)
    assert not [t for t in copy if t.startswith("«")], "a mixed frame"
    assert HOTSPOT_FIRST[0] in copy and "Ready to read." in copy


def test_the_connecting_splash_is_one_language(monkeypatch, tmp_path):
    _registry(tmp_path, monkeypatch, device="xx", xx=_xx_bundle())
    painted = []
    monkeypatch.setattr(eink_display, "display_status", lambda *a: painted.append(a))
    setup_server._show_connecting_splash("HomeNet")
    assert painted == [("«Connecting to HomeNet…»", "«This can take up to a minute.»")]


def test_a_catalog_that_cannot_import_paints_key_names_not_nothing(drawn, monkeypatch):
    """The setup screen must still paint its QR and credentials; the copy
    shows its keys, visibly, rather than the frame dying."""

    def boom(_lang=None):
        raise RuntimeError("catalog exploded")

    monkeypatch.setattr(strings_catalog, "snapshot", boom)
    img = eink_display.create_hotspot_display_image(SSID, PASSWORD, IP)
    assert img.size == eink_display.DISPLAY_SIZE
    assert "firstboot.hotspot.title" in drawn and SSID in drawn and PASSWORD in drawn
    # The key-name floor keeps the slot values: the step lines are the only
    # place the typed-address fallback is printed.
    assert any(IP in t and "firstboot.hotspot.first_4" in t for t in drawn), drawn


def test_control_server_no_longer_sends_an_english_units_label():
    """The splash labels the units itself, in its language. A label made in
    control_server's process would be English on every clock."""
    from control_server import handoff

    assert not hasattr(handoff, "_units_label")
    source = (REPO / "src" / "control_server" / "handoff.py").read_text(encoding="utf-8")
    assert "Metric (°C)" not in source and "Imperial (°F)" not in source


# ── Review hardening (PR litclock-dev#917) ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "render",
    [
        lambda: eink_display.create_hotspot_display_image(SSID, PASSWORD, IP),
        lambda: eink_display.create_hotspot_display_image(SSID, PASSWORD, IP, retry_reason="connect_failed"),
        lambda: eink_display.create_handoff_splash_image(SETTINGS, QR_URL),
        lambda: eink_display.create_handoff_splash_image({**SETTINGS, "has_location": False}, QR_URL),
    ],
)
def test_one_catalog_resolution_per_frame(monkeypatch, render):
    """O4: a language save or a bundle recovering mid-render cannot split a
    frame, because the frame reads ONE snapshot. Resolving per string would
    pass every static-language test above; counting resolutions does not."""
    calls = []
    real = strings_catalog.snapshot

    def counting(lang=None):
        calls.append(lang)
        return real(lang)

    monkeypatch.setattr(strings_catalog, "snapshot", counting)
    monkeypatch.setattr(strings_catalog, "get", lambda *a, **k: pytest.fail("a per-string catalog lookup"))
    render()
    assert len(calls) == 1, calls


def test_a_translated_line_break_never_crashes_a_splash(drawn, tmp_path, monkeypatch):
    """catalog_lint allows a line break in a value. textlength raises on one,
    and the handoff bottom line was measured raw — a crash the panel shows as
    a setup that never reaches the PWA QR."""
    bundle = {k: v if k.startswith("_") else f"«{v}\nzweite»" for k, v in EN.items()}
    _registry(tmp_path, monkeypatch, device="xx", xx=bundle)
    _render_all()
    copy = _copy(drawn)
    assert copy and all("\n" not in t for t in copy), [t for t in copy if "\n" in t]


def _qr_band(image, x0, y0, size):
    return image.crop((x0, y0, x0 + size, y0 + size)).tobytes()


def test_a_long_translation_never_draws_into_either_qr(tmp_path, monkeypatch):
    """The handoff QR is the only way from that screen to the PWA, the setup
    QR the only easy way onto the clock's network. A translation half again
    longer than English must leave both untouched (clamped to its lane)."""
    # No SSID caveat: its label is copy too, so it would differ by design.
    # What stays is the QR and the URL caption — values, never translated.
    no_caveat = {**SETTINGS, "connected_ssid": ""}
    english_hotspot = eink_display.create_hotspot_display_image(SSID, PASSWORD, IP)
    english_handoff = eink_display.create_handoff_splash_image(no_caveat, QR_URL)
    longer = {k: v if k.startswith("_") else f"«{v} {v}»" for k, v in EN.items()}
    _registry(tmp_path, monkeypatch, device="xx", xx=longer)
    hotspot = eink_display.create_hotspot_display_image(SSID, PASSWORD, IP)
    handoff = eink_display.create_handoff_splash_image(no_caveat, QR_URL)

    e = eink_display
    assert _qr_band(hotspot, e.SETUP_QR_X, e.SETUP_QR_Y, e.SETUP_QR_SIZE) == _qr_band(
        english_hotspot, e.SETUP_QR_X, e.SETUP_QR_Y, e.SETUP_QR_SIZE
    )
    # The handoff check spans the whole right column: from the lane boundary
    # to the panel edge, down to the bottom of the right column's ink
    # (y≈364, per the HANDOFF_VALUE_BUDGET notes) — the quiet zone and the
    # call-to-action line's level included.
    band = (e.HANDOFF_RIGHT_COLUMN_X, e.HANDOFF_QR_Y, e.DISPLAY_SIZE[0], 364)
    assert handoff.crop(band).tobytes() == english_handoff.crop(band).tobytes()


def test_a_slot_value_that_reads_like_a_slot_is_painted_as_written():
    """A custom setup SSID "My{ip_url}Net" must be the same name in the steps
    as in the QR and credentials — slot filling is one pass."""
    lines = eink_display.setup_instruction_lines(IP, ssid="My{ip_url}Net")
    assert lines[0] == "1. Scan the QR code to join My{ip_url}Net"
    assert lines[3] == "4. Then open a browser: http://litclock.setup or http://10.42.0.1"


def test_a_catalog_that_raises_still_paints_the_connecting_splash(monkeypatch):
    """The connect flow's except is narrow on purpose (ImportError, OSError,
    RuntimeError). A catalog failure of any other kind must be absorbed by
    the painter's helper — key names on the panel — not escape into the
    connect thread."""

    def boom(_lang=None):
        raise ValueError("catalog exploded")

    monkeypatch.setattr(strings_catalog, "snapshot", boom)
    painted = []
    monkeypatch.setattr(eink_display, "display_status", lambda *a: painted.append(a))
    setup_server._show_connecting_splash("HomeNet")
    assert painted == [("firstboot.splash.connecting.title HomeNet", "firstboot.splash.connecting.message")]
