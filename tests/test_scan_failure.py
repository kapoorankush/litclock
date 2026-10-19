"""litclock-dev#908 gap 1: a scan that FAILS is not a scan that found nothing.

Before this, scan_wifi_networks() returned [] when nmcli's list step errored
or timed out, so the setup page told an owner "No networks found - tap
Refresh" — and opened the type-it-yourself box — when the radio had simply
not answered and their network was almost certainly in range. A failure now
raises WifiScanError; the page says "Scan failed - tap Refresh" (copy that
already existed for the in-page Refresh), /scan-wifi answers 503, and a
genuinely empty scan keeps "No networks found".
"""

from __future__ import annotations

import subprocess
import time

import pytest

import setup_server
import strings_catalog
import wifi_provision
from tests.test_setup_portal_catalog import _rescan
from tests.test_wifi_retry_flow import FakeRequest, make_handler


def _cp(rc, stdout=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr="")


def _nmcli(monkeypatch, *, rescan_rc=0, list_rc=0, list_out=""):
    def fake(args, **_kw):
        return _cp(rescan_rc) if "rescan" in args else _cp(list_rc, list_out)

    monkeypatch.setattr(wifi_provision, "_run_nmcli", fake)
    monkeypatch.setattr(wifi_provision.time, "sleep", lambda _s: None)


# ── wifi_provision: the scan reports failure as failure ────────────────────


@pytest.mark.parametrize("rc", [1, 10, 124])  # 124 = _run_nmcli's own timeout bound
def test_a_failed_list_raises(monkeypatch, rc):
    _nmcli(monkeypatch, list_rc=rc)
    with pytest.raises(wifi_provision.WifiScanError):
        wifi_provision.scan_wifi_networks()


def test_an_empty_list_is_an_empty_result_not_a_failure(monkeypatch):
    _nmcli(monkeypatch, list_out="")
    assert wifi_provision.scan_wifi_networks() == []


def test_a_refused_rescan_still_lists(monkeypatch):
    """nmcli can refuse a rescan while the radio hosts the setup hotspot; the
    list then reports what the radio last saw. That is a result, not a
    failure, or the setup page would say "Scan failed" on every load."""
    _nmcli(monkeypatch, rescan_rc=1, list_out=":HomeNet:80:WPA2\n")
    assert [n["ssid"] for n in wifi_provision.scan_wifi_networks()] == ["HomeNet"]


# ── The setup server: page and /scan-wifi ──────────────────────────────────


@pytest.fixture
def portal(monkeypatch, tmp_path):
    monkeypatch.setenv("LITCLOCK_ENV_FILE", str(tmp_path / "no-env.sh"))
    monkeypatch.setenv("LITCLOCK_LANGUAGE", "en")
    monkeypatch.setenv("LITCLOCK_GIFT_LANGUAGE_MARKER", str(tmp_path / "no-marker"))
    monkeypatch.setattr(setup_server, "PROVISIONING_MODE", True)
    monkeypatch.setattr(setup_server, "WIFI_CONNECT_IN_FLIGHT", False)
    monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", None)
    monkeypatch.setattr(setup_server, "WIFI_LAST_MANUAL_SSID", "")
    monkeypatch.setattr(setup_server, "HOTSPOT_SSID", "LitClock-Setup")
    monkeypatch.setattr(setup_server, "_WIFI_SCAN_NETWORKS", None)
    monkeypatch.setattr(setup_server, "_WIFI_SCAN_TIME", 0)
    monkeypatch.setattr(setup_server, "_WIFI_SCAN_SSIDS", frozenset({"homenet"}))
    monkeypatch.setattr(setup_server, "_WIFI_SCAN_LAST_EMPTY", False)
    monkeypatch.setattr(setup_server, "_WIFI_SCAN_ATTEMPT_TIME", 0)
    monkeypatch.setattr(setup_server, "_WIFI_SCAN_LAST_FAILED", False)
    strings_catalog.reset_cache()
    yield monkeypatch
    strings_catalog.reset_cache()


def _failing(monkeypatch):
    def boom():
        raise wifi_provision.WifiScanError("nmcli device wifi list exited 10")

    monkeypatch.setattr(wifi_provision, "scan_wifi_networks", boom)


def _get(path):
    request = FakeRequest("GET", path)
    make_handler(request).do_GET()
    raw = request.wfile.getvalue().decode("utf-8", "replace")
    head, _, body = raw.partition("\r\n\r\n")
    return head.split("\r\n")[0], body


def test_the_page_says_scan_failed_and_does_not_push_typing(portal):
    _failing(portal)
    _status, page = _get("/setup")
    markup = page.split("<script>")[0]  # the script carries both strings for its own Refresh
    assert "Scan failed - tap Refresh" in markup
    assert "No networks found" not in markup
    # The disclosure is NOT force-opened, and the manual option is NOT
    # pre-selected: the next step is Refresh, not typing a name by hand.
    assert '<details id="manual-ssid">' in page
    assert 'data-manual="1" selected' not in page


def test_a_failed_scan_touches_no_scan_state(portal):
    _failing(portal)
    _get("/setup")
    assert setup_server._WIFI_SCAN_NETWORKS is None
    assert setup_server._WIFI_SCAN_SSIDS == frozenset({"homenet"})
    assert setup_server._WIFI_SCAN_LAST_EMPTY is False
    assert setup_server._WIFI_SCAN_ATTEMPT_TIME == 0, "a queued Refresh must try the radio again"


def test_a_genuinely_empty_scan_still_says_no_networks(portal):
    portal.setattr(wifi_provision, "scan_wifi_networks", lambda: [])
    _status, page = _get("/setup")
    assert "No networks found - tap Refresh" in page
    assert "Scan failed" not in page.split("<script>")[0]


def test_scan_wifi_answers_503_on_failure(portal):
    _failing(portal)
    status, body = _get("/scan-wifi")
    assert " 503 " in status + " "
    assert '"scan_failed"' in body
    assert setup_server._WIFI_SCAN_ATTEMPT_TIME == 0


def test_scan_wifi_still_answers_200_with_an_empty_list(portal):
    portal.setattr(wifi_provision, "scan_wifi_networks", lambda: [])
    status, body = _get("/scan-wifi")
    assert " 200 " in status + " " and body.strip() == "[]"


# ── The page's own Refresh: a 503 lands on "Scan failed" ───────────────────


def test_js_refresh_shows_scan_failed_on_a_503(portal, tmp_path):
    portal.setattr(setup_server, "_WIFI_SCAN_NETWORKS", [{"ssid": "HomeNet", "signal": 80, "security": "WPA2"}])
    portal.setattr(setup_server, "_WIFI_SCAN_TIME", time.monotonic())
    steps = [["refresh", None], ["settle", None], ["snapshot", "after"]]
    snaps = _rescan(tmp_path, steps, {"status": 503, "body": {"error": "scan_failed"}})
    assert snaps["after"]["labels"][0] == "Scan failed - tap Refresh"
    # Not routed into the networks handler, whose empty branch would have
    # force-selected the manual option.
    assert not snaps["after"]["selectedIsManual"]


def test_js_refresh_decides_on_the_status_not_the_body(portal, tmp_path):
    """A 503 must read as a failure whatever its body says. Without the
    `r.ok` check the {"error": ...} body above only lands on "Scan failed" by
    accident (its .sort throws into the catch); a 503 carrying [] would read
    as "No networks found" — the exact confusion this gap is about."""
    steps = [["refresh", None], ["settle", None], ["snapshot", "after"]]
    snaps = _rescan(tmp_path, steps, {"status": 503, "body": []})
    assert snaps["after"]["labels"][0] == "Scan failed - tap Refresh"


def test_a_language_switch_after_a_failure_re_renders_it_without_scanning(portal):
    """litclock-dev#532 D10: a language switch never waits on the radio. After a failed
    scan it must show "Scan failed" again — not rescan, and not a stale
    "No networks found" from an older empty scan."""
    _failing(portal)
    _get("/setup")
    portal.setattr(setup_server, "_WIFI_SCAN_LAST_EMPTY", True)  # an older empty scan
    portal.setattr(wifi_provision, "scan_wifi_networks", lambda: pytest.fail("a language switch rescanned"))
    options, empty = setup_server._wifi_network_options(stale_ok=True)
    assert "Scan failed - tap Refresh" in options and empty is False


def test_a_successful_scan_clears_the_failure(portal):
    _failing(portal)
    _get("/setup")
    nets = [{"ssid": "HomeNet", "signal": 80, "security": "WPA2"}]
    portal.setattr(wifi_provision, "scan_wifi_networks", lambda: nets)
    _get("/setup?rescan=1")
    assert setup_server._WIFI_SCAN_LAST_FAILED is False


def test_reset_state_clears_every_scan_global():
    """reset_state() must reset the WHOLE _WIFI_SCAN_* family. A name missing
    from its `global` line silently binds a local instead — the attempt stamp
    shipped that way in litclock-dev#932 and survived every reset."""
    names = [n for n in vars(setup_server) if n.startswith("_WIFI_SCAN_") and n != "_WIFI_SCAN_TTL"]
    assert "_WIFI_SCAN_ATTEMPT_TIME" in names and "_WIFI_SCAN_LAST_FAILED" in names
    sentinel = object()
    saved = {n: getattr(setup_server, n) for n in names}
    try:
        for n in names:
            setattr(setup_server, n, sentinel)
        setup_server.reset_state(wait_for_inflight=0)
        left = [n for n in names if getattr(setup_server, n) is sentinel]
        assert not left, f"reset_state() left {left} untouched"
    finally:
        for n, v in saved.items():
            setattr(setup_server, n, v)
