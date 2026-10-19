"""litclock-dev#909: the captive-portal setup pages wear the PWA's design system.

The pages inline their CSS (a captive-portal WebView should not have to make a
render-blocking fetch) and the setup server serves the PWA's own woff2 files
under /fonts/, because the hotspot has no internet. These tests pin the three
things a restyle can quietly break: the colour values drifting from the PWA's
tokens.css, a page asking for a font the server will not serve, and template
braces leaking into the output (the error page shipped with dead CSS and a
dead retry script for that reason until this change).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

import setup_server
import strings_catalog
from tests.test_wifi_retry_flow import FakeRequest, make_handler

REPO = Path(__file__).resolve().parent.parent
TOKENS_CSS = (REPO / "src" / "control_server" / "static" / "css" / "tokens.css").read_text(encoding="utf-8")
NETS = [{"ssid": "HomeNet", "signal": 80, "security": "WPA2"}]


@pytest.fixture(autouse=True)
def _page(monkeypatch, tmp_path):
    monkeypatch.setenv("LITCLOCK_ENV_FILE", str(tmp_path / "no-env.sh"))
    monkeypatch.setenv("LITCLOCK_LANGUAGE", "en")
    monkeypatch.setenv("LITCLOCK_GIFT_LANGUAGE_MARKER", str(tmp_path / "no-marker"))
    monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
    monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", False)
    monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", None)
    monkeypatch.setattr(setup_server, "HOTSPOT_SSID", "LitClock-Setup")
    monkeypatch.setattr(setup_server, "_WIFI_SCAN_NETWORKS", NETS)
    monkeypatch.setattr(setup_server, "_WIFI_SCAN_SSIDS", {n["ssid"] for n in NETS})
    monkeypatch.setattr(setup_server, "_WIFI_SCAN_TIME", time.monotonic())
    strings_catalog.reset_cache()
    yield
    strings_catalog.reset_cache()


def _pages():
    return {
        "setup": setup_server._build_setup_html(),
        "cna": setup_server._build_cna_bridge_html(),
        "success": setup_server._success_page("HomeNet"),
        "error": setup_server._error_page("Network 'HomeNet' not found"),
    }


def _normalize(value):
    """`rgba(26, 20, 16, 0.08)` and `rgba(26,20,16,.08)` are the same colour."""
    value = value.strip().lower().replace(" ", "")
    return re.sub(r"\d*\.\d+", lambda m: repr(float(m.group())), value)


def _pwa_tokens(block):
    return {name: _normalize(val) for name, val in re.findall(r"--([a-z-]+):\s*([^;]+);", block)}


def test_setup_tokens_match_the_pwa():
    """The setup pages cannot link tokens.css (this server does not serve the
    PWA's static tree), so they carry a copy — which this pins, both schemes,
    so a palette change in the PWA fails here instead of leaving setup behind."""
    light_block = TOKENS_CSS[TOKENS_CSS.index(":root {") : TOKENS_CSS.index("}", TOKENS_CSS.index(":root {"))]
    dark_start = TOKENS_CSS.index("@media (prefers-color-scheme: dark)")
    dark_block = TOKENS_CSS[dark_start : TOKENS_CSS.index("}", dark_start)]
    light, dark = _pwa_tokens(light_block), _pwa_tokens(dark_block)
    for name, (light_value, dark_value) in setup_server.SETUP_TOKENS.items():
        assert _normalize(light_value) == light[name], f"--{name} (light) drifted from tokens.css"
        assert _normalize(dark_value) == dark[name], f"--{name} (dark) drifted from tokens.css"


def test_every_page_asks_only_for_fonts_the_server_serves():
    for name, page in _pages().items():
        if name == "cna":
            continue  # pinned subresource-free below
        urls = re.findall(r"url\(([^)]+)\)", page) + re.findall(r'rel="preload" href="([^"]+)"', page)
        assert urls, f"{name}: no font references at all — the faces were dropped"
        for url in urls:
            assert url.startswith(setup_server.SETUP_FONT_PATH), f"{name}: {url} is not under /fonts/"
            assert url[len(setup_server.SETUP_FONT_PATH) :] in setup_server.SETUP_FONTS, f"{name}: {url}"


def test_every_allowlisted_font_ships():
    for font in setup_server.SETUP_FONTS:
        assert (setup_server.SETUP_FONT_DIR / font).is_file(), font


def test_pages_use_the_design_fonts_not_a_system_stack():
    """DESIGN.md bans system-ui / -apple-system as a PRIMARY face; it may only
    trail the stack as a fallback."""
    for name, page in _pages().items():
        assert '"Instrument Sans"' in page and '"Fraunces"' in page, name
        for decl in re.findall(r"font-family:\s*([^;}]+)", page):
            first = decl.split(",")[0].strip()
            assert first not in ("-apple-system", "system-ui", "BlinkMacSystemFont"), f"{name}: {decl}"


def test_pages_carry_none_of_the_old_router_admin_styling():
    for name, page in _pages().items():
        for old in ("#2563eb", "#4CAF50", "#f5f5f5", "#f8fafc", "#1565c0", "box-shadow", "background: white"):
            assert old not in page, f"{name} still carries {old}"


def test_no_template_braces_leak_into_any_page():
    """HTML_ERROR is filled with .replace, so doubled braces left over from
    the .format era were emitted verbatim — `body {{` — killing the page's
    CSS and making its retry script a syntax error."""
    for name, page in _pages().items():
        assert "{{" not in page, f"{name} emits a doubled brace"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_error_page_script_parses(tmp_path):
    page = _pages()["error"]
    script = page[page.index("<script>") + len("<script>") : page.index("</script>")]
    path = tmp_path / "error.js"
    path.write_text(script, encoding="utf-8")
    result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_bridge_fetches_nothing():
    """The bridge is also served under captive.apple.com, where a relative
    /fonts/ URL would resolve to Apple's host and come back as another bridge
    page — so it carries no @font-face, no preload, no subresource at all."""
    page = _pages()["cna"]
    assert "url(" not in page and "@font-face" not in page and "<link" not in page


def test_bridge_keeps_its_size_budget_with_the_styling():
    """test_bridge_is_small enforces 2048 bytes on the English bridge; keep
    real headroom for longer translated copy too."""
    assert len(_pages()["cna"].encode("utf-8")) < 1600


def _get(path, host="10.42.0.1"):
    request = FakeRequest("GET", path)
    handler = make_handler(request)
    handler.headers.replace_header("Host", host)
    handler.do_GET()
    raw = request.wfile.getvalue()
    head, _, body = raw.partition(b"\r\n\r\n")
    return head.decode("latin-1"), body


def test_font_route_serves_an_allowlisted_woff2_in_provisioning_mode():
    font = setup_server.SETUP_FONTS[0]
    head, body = _get(setup_server.SETUP_FONT_PATH + font)
    assert " 200 " in head.split("\r\n")[0], head
    assert "Content-type: font/woff2" in head
    assert body == (setup_server.SETUP_FONT_DIR / font).read_bytes()


@pytest.mark.parametrize(
    "path",
    [
        "/fonts/geist-mono-wght-normal.woff2",  # real file, not on the allowlist
        # Resolves to src/setup_server.py, a REAL file: only the allowlist
        # stands between this and serving the module's source.
        "/fonts/../../../setup_server.py",
        "/fonts/",
    ],
)
def test_font_route_serves_nothing_off_the_allowlist(path):
    head, body = _get(path)
    assert " 200 " not in head.split("\r\n")[0], head
    assert b"import" not in body and not body.startswith(b"wOF2")
