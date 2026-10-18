"""litclock-dev#532 RT3 (design D6/D10, eng R13): the setup page's language.

A row of language links at the top of the setup page switches the PAGE's
language for the setup session — the language every later setup page, and
the in-process connecting and retry splashes, are written in. A link never
saves anything: the device language is written by the WiFi POST, which
carries the session language in a hidden field (R13). A switch re-renders
from the cached scan, whatever its age, so it costs one render rather than a
2-20s radio scan. An unknown or inactive code keeps the current language.
Each page reads ONE catalog resolution (O4).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import setup_server
import strings_catalog

REPO = Path(__file__).resolve().parent.parent
EN = json.loads((REPO / "languages" / "en" / "strings.json").read_text(encoding="utf-8"))
NETS = [{"ssid": "HomeNet", "signal": 80, "security": "WPA2"}]


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """English and an active test language `xx` (every value wrapped in «»);
    the device has no language saved; a scan is cached, and STALE."""
    for code, data in (("en", EN), ("xx", {k: v if k.startswith("_") else f"«{v}»" for k, v in EN.items()})):
        target = tmp_path / "languages" / code / "strings.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    registry = json.loads((REPO / "languages.json").read_text(encoding="utf-8"))
    registry["languages"]["en"]["strings"] = "languages/en/strings.json"
    registry["languages"]["xx"] = dict(
        registry["languages"]["en"], code="xx", native_name="Xxish", strings="languages/xx/strings.json"
    )
    (tmp_path / "languages.json").write_text(json.dumps(registry), encoding="utf-8")
    env = tmp_path / "env.sh"
    env.write_text("export WEATHER_UNITS=metric\n", encoding="utf-8")
    monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", tmp_path / "languages.json")
    monkeypatch.setattr(strings_catalog, "_REPO_ROOT", tmp_path)
    monkeypatch.setenv("LITCLOCK_ENV_FILE", str(env))
    monkeypatch.delenv("LITCLOCK_LANGUAGE", raising=False)
    monkeypatch.setattr(setup_server, "ENV_FILE", str(env))
    monkeypatch.setattr(setup_server, "GIFT_LANGUAGE_MARKER", str(tmp_path / "no-marker"))
    monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
    monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", False)
    monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", None)
    monkeypatch.setattr(setup_server, "HOTSPOT_SSID", "LitClock-Setup")
    monkeypatch.setattr(setup_server, "SESSION_LANGUAGE", "")
    monkeypatch.setattr(setup_server, "_WIFI_SCAN_NETWORKS", NETS)
    monkeypatch.setattr(setup_server, "_WIFI_SCAN_SSIDS", {"HomeNet"})
    monkeypatch.setattr(setup_server, "_WIFI_SCAN_TIME", time.monotonic() - 10 * setup_server._WIFI_SCAN_TTL)
    strings_catalog.reset_cache()
    yield env
    strings_catalog.reset_cache()


class _Handler:
    """Just enough of SetupHandler to drive do_GET and capture the page."""

    def __init__(self, path, accept_language="en-US,en;q=0.9"):
        self.path = path
        self.headers = {"Host": "10.42.0.1", "Accept-Language": accept_language}
        self.sent = []

    def send_html(self, body):
        self.sent.append(body)

    def get(self):
        setup_server.SetupHandler.do_GET(self)
        return self.sent[-1]


def _no_scan(monkeypatch):
    import wifi_provision

    monkeypatch.setattr(wifi_provision, "scan_wifi_networks", lambda: pytest.fail("a language switch rescanned"))


def test_a_link_switches_the_page_and_saves_nothing(fleet, monkeypatch):
    _no_scan(monkeypatch)
    before = fleet.read_bytes()
    page = _Handler("/setup?lang=xx").get()
    assert setup_server.SESSION_LANGUAGE == "xx"
    assert '<html lang="xx">' in page
    assert f"<h1>«{EN['setup.page.title']}»</h1>" in page
    assert ">«HomeNet («Strong»)»</option>" in page, "the stale cached scan was not reused"
    assert '<span lang="xx" aria-current="true">Xxish</span>' in page
    assert '<a href="/setup?lang=en" lang="en" hreflang="en">English</a>' in page
    assert '<input type="hidden" name="litclock_language" value="xx">' in page
    assert fleet.read_bytes() == before, "a link click wrote env.sh"


def test_the_session_language_outlasts_the_link(fleet):
    """The next page — a refresh, the auto-refresh while joining — is in the
    session's language, whatever the phone's Accept-Language says."""
    _Handler("/setup?lang=xx").get()
    page = _Handler("/", accept_language="en-US,en").get()
    assert '<html lang="xx">' in page


@pytest.mark.parametrize("query", ["lang=zz", "lang=", "lang=xx%22%3E", "other=1"])
def test_an_unknown_code_keeps_the_current_language(fleet, monkeypatch, query):
    monkeypatch.setattr(setup_server, "SESSION_LANGUAGE", "xx")
    page = _Handler(f"/setup?{query}").get()
    assert setup_server.SESSION_LANGUAGE == "xx"
    assert '<html lang="xx">' in page


def test_without_a_link_a_stale_scan_is_refreshed(fleet, monkeypatch):
    """Only a switch reuses a stale scan; an ordinary load keeps the TTL."""
    import wifi_provision

    calls = []
    monkeypatch.setattr(wifi_provision, "scan_wifi_networks", lambda: calls.append(1) or NETS)
    _Handler("/setup").get()
    assert calls == [1]


def test_the_post_saves_the_session_language(fleet, monkeypatch):
    """R13: the WiFi submit is what writes the device language — the value
    the page carried in its hidden field."""
    from tests.test_wifi_retry_flow import FakeRequest, make_handler, post_setup

    monkeypatch.setattr(setup_server, "_spawn_bg", lambda target, name: None)
    _Handler("/setup?lang=xx").get()
    post_setup(
        make_handler(FakeRequest("POST", "/setup", "wifi_ssid=HomeNet&wifi_password=secret123&litclock_language=xx"))
    )
    assert "export LITCLOCK_LANGUAGE=xx" in fleet.read_text(encoding="utf-8")
    assert setup_server.SESSION_LANGUAGE == "xx"


def test_every_setup_page_honours_the_session(fleet):
    _Handler("/setup?lang=xx").get()
    assert '<html lang="xx">' in setup_server._build_cna_bridge_html()
    assert '<html lang="xx">' in setup_server._error_page("x")
    success = setup_server._success_page("HomeNet")
    assert '<html lang="xx">' in success and f"«{EN['setup.success.heading']}»" in success
    assert '<html lang="xx">' in _Handler(setup_server.CNA_PATH).get()


def test_the_connecting_and_retry_splashes_use_the_session_language(fleet, monkeypatch):
    import eink_display

    _Handler("/setup?lang=xx").get()
    langs = []
    real = eink_display._splash_catalog
    monkeypatch.setattr(eink_display, "_splash_catalog", lambda lang=None: (langs.append(lang), real(lang))[1])
    painted = []
    monkeypatch.setattr(eink_display, "display_status", lambda *a: painted.append(a))
    setup_server._show_connecting_splash("HomeNet")
    assert langs == ["xx"] and painted[0][0].startswith("«")

    retry = []
    monkeypatch.setattr(setup_server, "HOTSPOT_PASSWORD", "p")
    monkeypatch.setattr(eink_display, "display_hotspot_info", lambda *a, **k: retry.append(k.get("lang")))
    setup_server._restore_hotspot(lambda **_k: {"ssid": "LitClock-Setup", "password": "p", "ip": "10.42.0.1"})
    assert retry == ["xx"]


def test_one_catalog_resolution_per_page(fleet, monkeypatch):
    calls = []
    real = strings_catalog.snapshot
    monkeypatch.setattr(strings_catalog, "snapshot", lambda lang=None: (calls.append(lang), real(lang))[1])
    _Handler("/setup?lang=xx").get()
    assert calls == ["xx"], calls
    # And the page's resolution does not outlive the page.
    assert getattr(setup_server._RENDER, "snap", None) is None


def test_reset_clears_the_session_language(fleet, monkeypatch):
    monkeypatch.setattr(setup_server, "SESSION_LANGUAGE", "xx")
    monkeypatch.setattr(setup_server, "_WIFI_SCAN_LAST_EMPTY", True)
    setup_server.reset_state()
    assert setup_server.SESSION_LANGUAGE == ""
    assert setup_server._WIFI_SCAN_LAST_EMPTY is False


def test_a_single_language_fleet_shows_no_links_and_no_field(tmp_path, monkeypatch):
    """Pinned to an English-only registry of its own, so it keeps testing the
    single-language case after the real registry gains a language."""
    target = tmp_path / "languages" / "en" / "strings.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(EN), encoding="utf-8")
    registry = json.loads((REPO / "languages.json").read_text(encoding="utf-8"))
    registry["languages"] = {"en": dict(registry["languages"]["en"], strings="languages/en/strings.json")}
    (tmp_path / "languages.json").write_text(json.dumps(registry), encoding="utf-8")
    monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", tmp_path / "languages.json")
    monkeypatch.setattr(strings_catalog, "_REPO_ROOT", tmp_path)
    strings_catalog.reset_cache()
    monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
    page = setup_server._build_setup_html("de-DE")
    assert 'class="lang-links"' not in page and "litclock_language" not in page


def test_the_session_follows_the_submit_even_if_the_save_fails(fleet, monkeypatch):
    """The pages and splashes after a submit are in the language the reader
    chose, whether or not env.sh took the write (best-effort by contract)."""
    import config
    from tests.test_wifi_retry_flow import FakeRequest, make_handler, post_setup

    def refuse(*_a, **_k):
        raise OSError("read-only file system")

    monkeypatch.setattr(config, "atomic_update", refuse)
    monkeypatch.setattr(setup_server, "_spawn_bg", lambda target, name: None)
    post_setup(
        make_handler(FakeRequest("POST", "/setup", "wifi_ssid=HomeNet&wifi_password=secret123&litclock_language=xx"))
    )
    assert setup_server.SESSION_LANGUAGE == "xx"
    assert "LITCLOCK_LANGUAGE" not in fleet.read_text(encoding="utf-8")


# ── Review hardening (PR litclock-dev#920) ─────────────────────────────────────────────


def _post(body, accept_language=None):
    from tests.test_wifi_retry_flow import FakeRequest, make_handler, post_setup

    handler = make_handler(FakeRequest("POST", "/setup", body))
    if accept_language:
        handler.headers["Accept-Language"] = accept_language
    return post_setup(handler)


@pytest.mark.parametrize(
    ("body", "key"),
    [
        ("wifi_ssid=&wifi_password=x&litclock_language=xx", "setup.error.pick_network"),
        ("wifi_ssid=Bad%01Name&wifi_password=x&litclock_language=xx", "setup.error.bad_ssid_chars"),
        ("wifi_ssid=LitClock-Setup&wifi_password=x&litclock_language=xx", "setup.error.own_network"),
    ],
)
def test_a_validation_error_is_wholly_in_the_session_language(fleet, monkeypatch, body, key):
    """The message used to be resolved before the page's scope opened, in
    the device language, under an xx heading and button."""
    monkeypatch.setattr(setup_server, "_spawn_bg", lambda target, name: None)
    page = _post(body)
    assert '<html lang="xx">' in page
    message = EN[key]
    assert f"«{message}»" in page
    assert page.count(message) == page.count(f"«{message}»"), "the message appeared in English too"


def test_a_switch_after_an_empty_scan_does_not_rescan(fleet, monkeypatch):
    import wifi_provision

    monkeypatch.setattr(setup_server, "_WIFI_SCAN_NETWORKS", None)
    calls = []
    monkeypatch.setattr(wifi_provision, "scan_wifi_networks", lambda: calls.append(1) or [])
    _Handler("/setup").get()
    assert calls == [1]
    page = _Handler("/setup?lang=xx").get()
    assert calls == [1], "a language switch rescanned after an empty scan"
    assert f"«{EN['setup.wifi.placeholder_empty']}»" in page
    _Handler("/setup").get()
    assert calls == [1, 1], "an ordinary load must still retry an empty scan"


def test_reloading_the_current_language_link_still_refreshes_networks(fleet, monkeypatch):
    """Only a real switch reuses a stale scan: a no-JS reader's reload of the
    link they already followed is their only way to rescan."""
    import wifi_provision

    calls = []
    monkeypatch.setattr(wifi_provision, "scan_wifi_networks", lambda: calls.append(1) or NETS)
    _Handler("/setup?lang=xx").get()
    assert calls == []
    monkeypatch.setattr(setup_server, "_WIFI_SCAN_TIME", time.monotonic() - 10 * setup_server._WIFI_SCAN_TTL)
    _Handler("/setup?lang=xx").get()
    assert calls == [1]


def test_the_probe_path_bridge_follows_the_phone_language(fleet):
    """The iOS bridge served on a probe host honours Accept-Language like the
    /cna route does."""
    import io

    from tests.test_wifi_retry_flow import make_handler

    raw = (
        b"GET /hotspot-detect.html HTTP/1.1\r\nHost: captive.apple.com\r\n"
        b"User-Agent: Mozilla/5.0 Safari\r\nAccept-Language: xx\r\n\r\n"
    )

    class _Req:
        rfile = io.BytesIO(raw)
        wfile = io.BytesIO()

    handler = make_handler(_Req)
    handler.do_GET()
    body = _Req.wfile.getvalue().decode()
    assert "<html" in body and '<html lang="xx">' in body


def test_a_switch_never_resurrects_a_list_the_reader_no_longer_sees(fleet, monkeypatch):
    """A populated cache, then an expired-cache rescan that finds nothing:
    the reader sees the empty picker. A switch must show that, not the older
    list still sitting in the cache (round-2 review)."""
    import wifi_provision

    monkeypatch.setattr(wifi_provision, "scan_wifi_networks", lambda: [])
    page = _Handler("/setup").get()
    assert ">HomeNet (" not in page
    monkeypatch.setattr(wifi_provision, "scan_wifi_networks", lambda: pytest.fail("a switch rescanned"))
    page = _Handler("/setup?lang=xx").get()
    assert "HomeNet" not in page
    assert f"«{EN['setup.wifi.placeholder_empty']}»" in page


def _scan_wifi(monkeypatch, result):
    import wifi_provision

    monkeypatch.setattr(wifi_provision, "scan_wifi_networks", lambda: result)
    sent = []
    handler = _Handler("/scan-wifi")
    handler.send_json = sent.append
    setup_server.SetupHandler.do_GET(handler)
    return sent[-1]


def test_a_switch_shows_what_the_refresh_button_last_showed(fleet, monkeypatch):
    assert _scan_wifi(monkeypatch, []) == []
    import wifi_provision

    monkeypatch.setattr(wifi_provision, "scan_wifi_networks", lambda: pytest.fail("a switch rescanned"))
    assert "HomeNet" not in _Handler("/setup?lang=xx").get()
    other = [{"ssid": "Attic", "signal": 60, "security": "WPA2"}]
    assert _scan_wifi(monkeypatch, other) == other
    monkeypatch.setattr(wifi_provision, "scan_wifi_networks", lambda: pytest.fail("a switch rescanned"))
    assert ">Attic (Medium)</option>" in _Handler("/setup?lang=en").get()


def test_the_post_saves_its_own_language_whatever_another_phone_does(fleet, monkeypatch):
    """Another phone's link can change the SESSION between adopting this
    POST's language and writing it; the write must be this POST's."""
    from tests.test_wifi_retry_flow import FakeRequest, make_handler, post_setup

    real_adopt = setup_server._adopt_posted_language

    def adopt_then_interleave(params, accept_language=None):
        code = real_adopt(params, accept_language)
        setup_server.SESSION_LANGUAGE = "en"  # another phone's ?lang=en
        return code

    monkeypatch.setattr(setup_server, "_adopt_posted_language", adopt_then_interleave)
    monkeypatch.setattr(setup_server, "_spawn_bg", lambda target, name: None)
    post_setup(
        make_handler(FakeRequest("POST", "/setup", "wifi_ssid=HomeNet&wifi_password=secret123&litclock_language=xx"))
    )
    assert "export LITCLOCK_LANGUAGE=xx" in fleet.read_text(encoding="utf-8")
