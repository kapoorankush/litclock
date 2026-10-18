"""litclock-dev#908: the setup page's Refresh works with JavaScript off.

Refresh used to be a ``type="button"`` that only ran ``refreshNetworks()``,
so a captive-portal WebView with JS disabled had no way to rescan at all. It
is now a real submit for a separate, field-less GET form (``#rescan-form``,
outside the setup form so the password can never ride a GET URL); the server
reads ``?rescan=1`` as "a scan newer than this request". With JS on, the
handler rescans in place and returns false to cancel the navigation.
"""

from __future__ import annotations

import re
import time

import pytest

import setup_server
import strings_catalog
import wifi_provision
from tests.test_setup_portal_catalog import _rescan
from tests.test_wifi_retry_flow import FakeRequest, make_handler

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
    # A FRESH cache: anything that scans below did so because it was asked to.
    monkeypatch.setattr(setup_server, "_WIFI_SCAN_NETWORKS", NETS)
    monkeypatch.setattr(setup_server, "_WIFI_SCAN_SSIDS", frozenset({"HomeNet"}))
    monkeypatch.setattr(setup_server, "_WIFI_SCAN_TIME", time.monotonic())
    monkeypatch.setattr(setup_server, "_WIFI_SCAN_LAST_EMPTY", False)
    strings_catalog.reset_cache()
    yield
    strings_catalog.reset_cache()


@pytest.fixture
def scans(monkeypatch):
    calls = []

    def fake_scan():
        calls.append(1)
        return [{"ssid": "FreshNet", "signal": 70, "security": "WPA2"}]

    monkeypatch.setattr(wifi_provision, "scan_wifi_networks", fake_scan)
    return calls


def _get(path):
    request = FakeRequest("GET", path)
    make_handler(request).do_GET()
    return request.wfile.getvalue().decode("utf-8", "replace")


def _form(page, form_id):
    match = re.search(rf'<form id="{form_id}"[^>]*>(.*?)</form>', page, re.S)
    assert match, f"no #{form_id}"
    return match.group(0)


# ── Markup: the no-JS path exists and cannot leak the password ─────────────


def test_refresh_submits_its_own_get_form():
    page = setup_server._build_setup_html()
    button = re.search(r"<button[^>]*>\s*Refresh\s*</button>", page, re.S).group(0)
    assert 'type="submit"' in button and 'form="rescan-form"' in button
    assert 'onclick="return refreshNetworks()"' in button, "JS must be able to cancel the GET"
    rescan = _form(page, "rescan-form")
    assert 'method="get"' in rescan and 'action="/setup"' in rescan
    assert '<input type="hidden" name="rescan" value="1">' in rescan


def test_the_rescan_form_carries_no_setup_field():
    """A GET puts every field in the URL — and so in history and any proxy
    log. The rescan form must hold nothing but its own marker, and must not
    be nested in (or nest) the setup form."""
    page = setup_server._build_setup_html()
    rescan = _form(page, "rescan-form")
    assert re.findall(r"<(?:input|select|textarea)\b", rescan) == ["<input"]
    setup_start = page.index('<form id="setup-form"')
    setup_end = page.index("</form>", setup_start)
    assert page[setup_start:setup_end].count("<form") == 1, "a form is nested in the setup form"
    assert page.index('<form id="rescan-form"') > setup_end, "the rescan form must sit after it"


def test_enter_in_the_password_field_still_submits_setup():
    """Enter submits a form's FIRST submit button. Refresh comes first in the
    markup, so it must belong to the other form or Enter would rescan and
    throw the typed password away."""
    setup_form = _form(setup_server._build_setup_html(), "setup-form")
    submits = re.findall(r'<button[^>]*type="submit"[^>]*>', setup_form)
    assert submits[0].count('form="rescan-form"') == 1
    assert 'id="submit-btn"' in [s for s in submits if "rescan-form" not in s][0]


# ── Route: ?rescan=1 scans even with a fresh cache ─────────────────────────


def test_plain_get_serves_the_fresh_cache(scans):
    page = _get("/setup")
    assert scans == [] and "HomeNet" in page


def test_rescan_get_scans_despite_a_fresh_cache(scans):
    page = _get("/setup?rescan=1")
    assert scans == [1]
    assert "FreshNet" in page and ">HomeNet" not in page


def test_rescan_is_ignored_while_a_join_is_in_flight(scans, monkeypatch):
    monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", True)
    _get("/setup?rescan=1")
    assert scans == []


@pytest.mark.parametrize("query", ["rescan=0", "rescan=", "rescan=yes", "rescanx=1"])
def test_only_rescan_1_asks(scans, query):
    _get(f"/setup?{query}")
    assert scans == []


def test_a_queued_rescan_after_an_EMPTY_scan_takes_it_too(scans, monkeypatch):
    """An empty scan never moves _WIFI_SCAN_TIME, so freshness keys on when
    the last scan FINISHED, whatever it found."""
    asked = time.monotonic()
    monkeypatch.setattr(setup_server, "_WIFI_SCAN_ATTEMPT_TIME", asked + 1)
    monkeypatch.setattr(setup_server, "_WIFI_SCAN_LAST_EMPTY", True)
    options, empty = setup_server._wifi_network_options(fresh_after=asked)
    assert scans == [] and empty is True


def test_scan_wifi_reuses_a_scan_that_finished_while_it_waited(scans, monkeypatch):
    """The JS Refresh's endpoint: a scan finishing after the tap is the
    answer. A second back-to-back scan would overrun the page's 30s abort."""
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(setup_server, "_WIFI_SCAN_ATTEMPT_TIME", 100.0)
    body = _get("/scan-wifi")
    assert scans == [] and "HomeNet" in body
    monkeypatch.setattr(setup_server, "_WIFI_SCAN_ATTEMPT_TIME", 99.0)
    body = _get("/scan-wifi")
    assert scans == [1] and "FreshNet" in body


def test_a_queued_rescan_takes_the_scan_that_finished_after_it_asked(scans, monkeypatch):
    """Two no-JS Refreshes: the second waits on the lock through the first's
    scan. That scan completed after the second ASKED, so it is fresh enough —
    a second 2-20s radio scan would only delay the page."""
    asked = time.monotonic()
    monkeypatch.setattr(setup_server, "_WIFI_SCAN_ATTEMPT_TIME", asked + 1)
    setup_server._wifi_network_options(fresh_after=asked)
    assert scans == []
    setup_server._wifi_network_options(fresh_after=asked + 2)
    assert scans == [1]


# ── JS on: the in-page rescan cancels the navigation ───────────────────────


def test_js_refresh_returns_false_so_the_get_does_not_also_fire(tmp_path):
    snaps = _rescan(tmp_path, [["refresh", None], ["settle", None], ["snapshot", "after"]], {"networks": NETS})
    assert snaps["after"]["refreshReturned"] is False
    assert snaps["after"]["fetchCalls"] == 1


def test_the_page_waits_out_the_servers_worst_case_scan():
    """The page aborts /scan-wifi after SCAN_TIMEOUT_MS. If that is shorter
    than the server's own bounded scan, a slow scan that would succeed shows
    "Scan failed" while the server is still scanning."""
    page = setup_server._build_setup_html()
    client_ms = int(re.search(r"var SCAN_TIMEOUT_MS = (\d+);", page).group(1))
    worst_s = wifi_provision.SCAN_RESCAN_TIMEOUT + wifi_provision.SCAN_SETTLE_S + wifi_provision.SCAN_LIST_TIMEOUT
    assert client_ms > worst_s * 1000
