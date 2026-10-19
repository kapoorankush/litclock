"""litclock-dev#532 T6 (slice 2): the captive portal's last English literals.

The WiFi picker's signal words and its "[Open]" mark (server-rendered AND
rebuilt by the page's own rescan script), the rescan's "Scanning..." and
"Scan failed" placeholders, the confirmation page, and the connect-failure
cause in the red banner now come from the catalog; ``<html lang>`` names the
language the page is written in.

R8: English is unchanged. A one-off render of nine page states before and
after was identical outside the ``<html>`` tag (now ``lang="en"``, the one
intentional change) and the inline script's source, whose output the
executed tests below pin.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

import setup_server
import strings_catalog
import wifi_provision
from tests.test_setup_server import _GATE_HARNESS, _page_state

REPO = Path(__file__).resolve().parent.parent
EN = json.loads((REPO / "languages" / "en" / "strings.json").read_text(encoding="utf-8"))
NETS = [
    {"ssid": "HomeNet", "signal": 80, "security": "WPA2"},
    {"ssid": "Cafe", "signal": 50, "security": "--"},
    {"ssid": "{strength}Net", "signal": 20, "security": "WPA"},
]


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


def _xx(tmp_path, monkeypatch, *, drop: str | None = None):
    """Make the device language `xx`: every English value wrapped in «»."""
    bundle = {k: v if k.startswith("_") else f"«{v}»" for k, v in EN.items()}
    if drop:
        del bundle[drop]
    for code, data in (("en", EN), ("xx", bundle)):
        target = tmp_path / "languages" / code / "strings.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    registry = json.loads((REPO / "languages.json").read_text(encoding="utf-8"))
    registry["languages"]["en"]["strings"] = "languages/en/strings.json"
    registry["languages"]["xx"] = dict(
        registry["languages"]["en"], code="xx", native_name="Xx", strings="languages/xx/strings.json"
    )
    (tmp_path / "languages.json").write_text(json.dumps(registry), encoding="utf-8")
    monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", tmp_path / "languages.json")
    monkeypatch.setattr(strings_catalog, "_REPO_ROOT", tmp_path)
    monkeypatch.setenv("LITCLOCK_LANGUAGE", "xx")
    strings_catalog.reset_cache()


def _rescan(tmp_path, steps, fetch):
    """Run the page's real script under node and drive a rescan."""
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("node is missing in CI — the executed rescan checks cannot be skipped there")
        pytest.skip("node not installed")
    state, script = _page_state(setup_server._build_setup_html())
    state.update(steps=steps, fetch=fetch)
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "page.js").write_text(script, encoding="utf-8")
    (tmp_path / "scenario.json").write_text(json.dumps(state), encoding="utf-8")
    (tmp_path / "harness.js").write_text(_GATE_HARNESS, encoding="utf-8")
    result = subprocess.run(
        [node, str(tmp_path / "harness.js"), str(tmp_path / "page.js"), str(tmp_path / "scenario.json")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return {snap["label"]: snap for snap in json.loads(result.stdout)}


RESCAN = [["refresh", None], ["snapshot", "scanning"], ["settle", None], ["snapshot", "done"]]


# ── English: today's copy ──────────────────────────────────────────────────


def test_english_server_options_are_todays_labels():
    page = setup_server._build_setup_html()
    assert ">HomeNet (Strong)</option>" in page
    assert ">Cafe (Medium [Open])</option>" in page
    assert ">{strength}Net (Weak)</option>" in page
    assert '<html lang="en">' in page


def test_english_rescan_rebuilds_the_same_labels(tmp_path):
    snaps = _rescan(tmp_path, RESCAN, {"networks": NETS})
    assert snaps["scanning"]["labels"][0] == "Scanning..."
    labels = snaps["done"]["labels"]
    assert "HomeNet (Strong)" in labels and "Cafe (Medium [Open])" in labels
    # The SSID is filled LAST, so a name reading like a slot stays as named.
    assert "{strength}Net (Weak)" in labels


def test_english_rescan_failure_placeholder(tmp_path):
    snaps = _rescan(tmp_path, [["refresh", None], ["settle", None], ["snapshot", "failed"]], {"reject": True})
    assert snaps["failed"]["labels"][0] == "Scan failed - tap Refresh"


@pytest.mark.parametrize(
    ("make", "want"),
    [
        (lambda: wifi_provision._failure("bad_password", "setup.cause.bad_password"), "Incorrect WiFi password"),
        (
            lambda: wifi_provision._failure("timeout", "setup.cause.timeout", ssid="Home<Net>"),
            "Couldn&#x27;t reach &#x27;Home&lt;Net&gt;&#x27; — the network didn&#x27;t answer in time.",
        ),
        (lambda: setup_server._ServerFailure("setup.cause.setup_error", detail="boom"), "Setup error: boom"),
        (lambda: "a plain string error", "a plain string error"),
    ],
)
def test_english_banner_shows_the_cause(monkeypatch, make, want):
    monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", make())
    assert want in setup_server._build_setup_html()


def test_wifi_failure_english_is_the_catalog_value():
    """One source: the journal's English IS the catalog's English."""
    failure = wifi_provision._failure("not_found", "setup.cause.not_found_hidden", ssid="Attic")
    assert str(failure) == EN["setup.cause.not_found_hidden"].replace("{ssid}", "Attic")
    assert (failure.failure_class, failure.key, failure.slots) == (
        "not_found",
        "setup.cause.not_found_hidden",
        {"ssid": "Attic"},
    )
    import copy

    clone = copy.deepcopy(failure)
    assert (str(clone), clone.failure_class, clone.key, clone.slots) == (
        str(failure),
        failure.failure_class,
        failure.key,
        failure.slots,
    )


def test_english_success_page(monkeypatch):
    page = setup_server._success_page("Home <Net>")
    assert '<html lang="en">' in page
    assert "Your clock is connecting to <strong>Home &lt;Net&gt;</strong>." in page
    assert "Settings Saved" not in page, "the page must not claim success before the join"


# ── Another language ───────────────────────────────────────────────────────


def test_a_device_language_writes_the_picker_and_the_rescan_in_it(tmp_path, monkeypatch):
    _xx(tmp_path, monkeypatch)
    page = setup_server._build_setup_html()
    assert '<html lang="xx">' in page
    assert ">«HomeNet («Strong»)»</option>" in page
    assert ">«Cafe («Medium» [Open])»</option>" in page
    snaps = _rescan(tmp_path / "run", RESCAN, {"networks": NETS})
    assert snaps["scanning"]["labels"][0] == "«Scanning...»"
    assert "«HomeNet («Strong»)»" in snaps["done"]["labels"]
    assert "«{strength}Net («Weak»)»" in snaps["done"]["labels"]
    failed = _rescan(tmp_path / "fail", [["refresh", None], ["settle", None], ["snapshot", "f"]], {"reject": True})
    assert failed["f"]["labels"][0] == "«Scan failed - tap Refresh»"


def test_the_banner_cause_is_in_the_page_language(tmp_path, monkeypatch):
    _xx(tmp_path, monkeypatch)
    failure = wifi_provision._failure("timeout", "setup.cause.timeout", ssid="HomeNet")
    assert str(failure).startswith("Couldn't reach 'HomeNet'"), "the journal text must stay English"
    monkeypatch.setattr(setup_server, "WIFI_CONNECT_ERROR", failure)
    page = setup_server._build_setup_html()
    assert "«Couldn&#x27;t reach &#x27;HomeNet&#x27;" in page
    assert "Couldn&#x27;t reach &#x27;HomeNet&#x27; — the network" not in page.replace("«Couldn", "")


def test_the_success_error_and_cna_pages_are_in_the_page_language(tmp_path, monkeypatch):
    _xx(tmp_path, monkeypatch)
    success = setup_server._success_page("HomeNet")
    assert '<html lang="xx">' in success
    for want in ("«Joining your WiFi»", "«Your clock is connecting to <strong>HomeNet</strong>.", "«WiFi»"):
        assert want in success, want
    assert '<html lang="xx">' in setup_server._error_page("x")
    assert '<html lang="xx">' in setup_server._build_cna_bridge_html()


def test_an_unusable_bundle_names_the_fleet_default_in_html_lang(tmp_path, monkeypatch):
    _xx(tmp_path, monkeypatch, drop="setup.wifi.scanning")
    page = setup_server._build_setup_html()
    assert '<html lang="en">' in page
    assert ">HomeNet (Strong)</option>" in page


def test_a_server_failure_never_raises_while_being_built(monkeypatch):
    """It is built inside the connect thread's except arms: raising there
    would skip the hotspot restore that follows."""

    def boom(*_a, **_k):
        raise RuntimeError("catalog exploded")

    monkeypatch.setattr(strings_catalog, "get", boom)
    failure = setup_server._ServerFailure("setup.cause.setup_error", detail="disk full")
    assert str(failure) == "setup.cause.setup_error disk full"
    assert failure.key == "setup.cause.setup_error"


# ── Review hardening (PR litclock-dev#918) ─────────────────────────────────────────────


def test_a_setup_error_keeps_text_not_the_exception(monkeypatch, tmp_path, tmp_env_file):
    """The error on show must not hold the exception: its traceback frames
    hold the submitted WiFi password for as long as the banner is up."""
    import sys

    from tests.test_wifi_retry_flow import (
        FakeRequest,
        TestConnectAndTeardownOrdering,
        _wait_for_connect_thread,
        make_handler,
        post_setup,
    )

    monkeypatch.setattr(setup_server, "ENV_FILE", tmp_env_file)
    monkeypatch.setattr(setup_server, "SIGNAL_FILE", str(tmp_path / "signal-done"))
    monkeypatch.setattr(setup_server, "_restore_hotspot", lambda *a, **k: None)
    # Put the REAL module back afterwards: the helper swaps in a fake, and a
    # bare pop would leave no wifi_provision at all for later test files.
    monkeypatch.setitem(sys.modules, "wifi_provision", wifi_provision)
    TestConnectAndTeardownOrdering._install_fake_wp(monkeypatch, connect_result=(True, None), teardown_raises=True)
    body = "wifi_ssid=TestNetwork&wifi_password=secret123"
    post_setup(make_handler(FakeRequest("POST", "/setup", body)))
    _wait_for_connect_thread()
    error = setup_server.WIFI_CONNECT_ERROR
    assert str(error) == "Setup error: nmcli timeout"
    assert error.key == "setup.cause.setup_error"
    assert error.slots == {"detail": "nmcli timeout"}
    assert all(isinstance(value, str) for value in error.slots.values())


def test_with_no_catalog_the_journal_keeps_the_failure_detail(tmp_path, monkeypatch):
    """The catalog's floor is the bare key; the journal line must still carry
    the SSID and nmcli's own words, the only diagnosis it offers."""
    monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(strings_catalog, "_REPO_ROOT", tmp_path)
    strings_catalog.reset_cache()
    failure = wifi_provision._failure("other", "setup.cause.connect_failed", detail="Secrets were required")
    assert str(failure) == "setup.cause.connect_failed Secrets were required"
    server = setup_server._ServerFailure("setup.cause.setup_error", detail="disk full")
    assert str(server) == "setup.cause.setup_error disk full"


def test_a_translations_own_markup_is_escaped_in_the_picker(tmp_path, monkeypatch):
    _xx(tmp_path, monkeypatch)
    bundle_path = tmp_path / "languages" / "xx" / "strings.json"
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle["setup.wifi.option"] = "{ssid} <{strength}> & co"
    bundle_path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    strings_catalog.reset_cache()
    page = setup_server._build_setup_html()
    assert ">HomeNet &lt;«Strong»&gt; &amp; co</option>" in page


def test_a_server_failure_copies_without_a_catalog_lookup(monkeypatch):
    """Without __getnewargs_ex__ a copy rebuilt the object from its MESSAGE
    as if it were a key — a catalog miss and a bogus journal warning on
    every copy (the slot state then restored the fields regardless)."""
    import copy

    failure = setup_server._ServerFailure("setup.cause.setup_error", detail="boom")
    looked_up = []
    real = strings_catalog.get
    monkeypatch.setattr(strings_catalog, "get", lambda key, *a, **k: (looked_up.append(key), real(key, *a, **k))[1])
    clone = copy.deepcopy(failure)
    assert all(key.startswith("setup.cause.") for key in looked_up), looked_up
    assert (str(clone), clone.key, clone.slots, clone.failure_class) == (
        "Setup error: boom",
        "setup.cause.setup_error",
        {"detail": "boom"},
        "other",
    )
