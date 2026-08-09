"""Tests for wifi_provision.ensure_wifi_ready (#172).

The strict check exists because on Pi Zero 2W the BCM43436 SDIO chip
can be left in a stuck state by a rapid reboot. When that happens,
`nmcli -t -f DEVICE,TYPE,STATE device` reports wlan0 as either missing,
`unavailable`, or `unmanaged` — and running `nmcli device wifi hotspot`
in any of those states fails with "Device 'wlan0' is not a Wi-Fi device".
The prior lenient check ("any state that isn't unavailable") would
wave these cases through and surface the misleading nmcli error.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

import wifi_provision


@pytest.fixture
def patch_subprocess(monkeypatch):
    """Patch subprocess.run so nothing actually shells out."""
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


@pytest.fixture
def fake_nmcli(monkeypatch):
    """Patch _run_nmcli to return a scripted sequence of device states.

    Each call returns the next scripted state (or the last one if exhausted).
    """

    scripted: list[str] = []

    def push(state_line: str):
        scripted.append(state_line)

    def fake(args, check=False, sudo=False):
        if args[:3] == ["-t", "-f", "DEVICE,TYPE,STATE"]:
            state = scripted[0] if len(scripted) == 1 else scripted.pop(0) if scripted else ""
            return SimpleNamespace(returncode=0, stdout=state, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(wifi_provision, "_run_nmcli", fake)
    monkeypatch.setattr(wifi_provision.time, "sleep", lambda _s: None)
    return SimpleNamespace(push=push)


def test_ready_when_disconnected(patch_subprocess, fake_nmcli):
    fake_nmcli.push("wlan0:wifi:disconnected")
    assert wifi_provision.ensure_wifi_ready(timeout=3) is True


def test_ready_when_connected(patch_subprocess, fake_nmcli):
    fake_nmcli.push("wlan0:wifi:connected")
    assert wifi_provision.ensure_wifi_ready(timeout=3) is True


def test_ready_when_connecting(patch_subprocess, fake_nmcli):
    fake_nmcli.push("wlan0:wifi:connecting")
    assert wifi_provision.ensure_wifi_ready(timeout=3) is True


def test_rejects_unmanaged(patch_subprocess, fake_nmcli):
    """Prior bug: the lenient check accepted `unmanaged` as ready."""
    fake_nmcli.push("wlan0:wifi:unmanaged")
    assert wifi_provision.ensure_wifi_ready(timeout=2) is False


def test_rejects_unavailable(patch_subprocess, fake_nmcli):
    fake_nmcli.push("wlan0:wifi:unavailable")
    assert wifi_provision.ensure_wifi_ready(timeout=2) is False


def test_rejects_missing_wlan0(patch_subprocess, fake_nmcli):
    """If wlan0 doesn't appear in nmcli output at all (driver hang)."""
    fake_nmcli.push("lo:loopback:connected (externally)")
    assert wifi_provision.ensure_wifi_ready(timeout=2) is False


def test_rejects_non_wifi_type(patch_subprocess, fake_nmcli):
    """The exact failure from the 2026-04-10 stuck-chip boot: wlan0 exists
    but NM has not recognized it as a wifi device."""
    fake_nmcli.push("wlan0:generic:disconnected")
    assert wifi_provision.ensure_wifi_ready(timeout=2) is False


def test_transitions_from_unavailable_to_disconnected(patch_subprocess, monkeypatch):
    """Normal cold-boot path: wlan0 starts unavailable, becomes disconnected."""
    states = iter(
        [
            "wlan0:wifi:unavailable",
            "wlan0:wifi:unavailable",
            "wlan0:wifi:disconnected",
        ]
    )

    def fake(args, check=False, sudo=False):
        if args[:3] == ["-t", "-f", "DEVICE,TYPE,STATE"]:
            return SimpleNamespace(returncode=0, stdout=next(states, "wlan0:wifi:disconnected"), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(wifi_provision, "_run_nmcli", fake)
    monkeypatch.setattr(wifi_provision.time, "sleep", lambda _s: None)
    assert wifi_provision.ensure_wifi_ready(timeout=10) is True


def test_create_hotspot_bails_when_not_ready(monkeypatch):
    """create_hotspot must refuse to shell out to nmcli when wlan0 isn't ready.

    Previously it logged a warning and marched on, surfacing a confusing
    "Device 'wlan0' is not a Wi-Fi device" error to the operator.
    """
    monkeypatch.setattr(wifi_provision, "ensure_wifi_ready", lambda: False)

    called = []

    def should_not_run(*args, **kwargs):
        called.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(wifi_provision, "teardown_hotspot", should_not_run)
    monkeypatch.setattr(wifi_provision, "_setup_captive_portal", should_not_run)
    monkeypatch.setattr(wifi_provision, "_run_nmcli", should_not_run)

    result = wifi_provision.create_hotspot(ssid="test", password="testpass")
    assert result is None
    assert called == [], "create_hotspot should not run any side effects when wlan0 is not ready"


def test_captive_portal_dnsmasq_config_has_no_resolv(monkeypatch):
    """#483: the captive dnsmasq config MUST include `no-resolv`.

    Without it, NM's shared-mode dnsmasq reads /etc/resolv.conf and inherits a
    public upstream (e.g. 8.8.8.8), then forwards iOS's HTTPS-RR (type 65)
    captive probe there. On the isolated hotspot that upstream is unreachable, so
    the forward returns `REFUSED (EDE: network error)` — which iOS reads as
    hostile DNS and silently demotes the captive-portal sheet. `no-resolv` drops
    the upstream entirely so dnsmasq answers non-A types authoritatively (NODATA)
    and the popup fires. `local=/#/` alone does NOT stop the forward (verified on
    dnsmasq 2.90), which is why this regressed on the newer image.
    """
    writes: list[dict] = []

    def fake_run(cmd, *args, **kwargs):
        writes.append({"cmd": list(cmd), "input": kwargs.get("input")})
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    wifi_provision._setup_captive_portal()

    tee = next(w for w in writes if "tee" in w["cmd"] and w["input"])
    conf = tee["input"]
    assert "no-resolv" in conf, "no-resolv missing — iOS captive HTTPS-RR probe will REFUSE"
    assert f"address=/#/{wifi_provision.HOTSPOT_GATEWAY}" in conf
    assert "local=/#/" in conf


def test_captive_portal_dnsmasq_nxdomains_private_relay_hosts(monkeypatch):
    """litclock-dev#526 pcap: on join, iOS 26 tries iCloud Private Relay
    (mask*.icloud.com); the /#/ wildcard spoofed it to the gateway which
    then refused the connection — part of the spoof-then-refuse pattern
    that suppresses the CNA sheet. Apple documents NXDOMAIN as the correct
    answer on networks where relay is unavailable: `address=/name/` with
    no IP. The specific entries must not carry the gateway IP."""
    writes: list[dict] = []

    def fake_run(cmd, *args, **kwargs):
        writes.append({"cmd": list(cmd), "input": kwargs.get("input")})
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    wifi_provision._setup_captive_portal()

    tee = next(w for w in writes if "tee" in w["cmd"] and w["input"])
    conf = tee["input"]
    for host in ("mask.icloud.com", "mask-h2.icloud.com", "mask-api.icloud.com"):
        assert f"address=/{host}/\n" in conf, f"{host} must be NXDOMAINed (bare address=, no IP)"
        assert f"address=/{host}/{wifi_provision.HOTSPOT_GATEWAY}" not in conf


def test_captive_portal_nft_drops_443_silently(monkeypatch):
    """litclock-dev#526 pcap: the kernel's RST on spoofed 443/5223
    connections (plus ICMP-unreachable on QUIC) is what iOS 26 reads as a
    broken network ('network connection was lost') — the sheet stays down
    even though the port-80 probe is answered. The nft table must contain
    a walled-garden filter chain that DROPs tcp 443+5223 and udp 443
    (silent, like commercial gateways) alongside the 80→8080 redirect."""
    runs: list[dict] = []

    def fake_run(cmd, *args, **kwargs):
        runs.append({"cmd": list(cmd), "input": kwargs.get("input")})
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    wifi_provision._setup_captive_portal()

    nft = next(w for w in runs if "/usr/sbin/nft" in w["cmd"])
    rules = nft["input"]
    assert "tcp dport 80 redirect to :8080" in rules
    assert "type filter hook prerouting" in rules
    assert "tcp dport { 443, 5223 } drop" in rules
    assert "udp dport 443 drop" in rules
    # Single named table — teardown deletes it whole, chains included.
    assert rules.count("table ip litclock_captive") == 1


class TestTeardownCaptivePortal:
    """#343 (/review F3): the captive nft table holds a port-80→8080 redirect,
    and control_server now binds 80. Teardown must VERIFY the table is gone (not
    ignore the delete result), retrying and logging loudly if it survives, or a
    failed teardown on the no-reboot success path would make the PWA unreachable."""

    def _patch(self, monkeypatch, list_returncodes):
        """list_returncodes: the returncode the `nft list table` probe yields on
        each call (0 = table still present)."""
        seq = iter(list_returncodes)
        calls = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(list(cmd))
            rc = 0
            if "list" in cmd and "table" in cmd:
                rc = next(seq)
            return SimpleNamespace(returncode=rc, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        return calls

    def test_success_when_table_gone_after_first_delete(self, monkeypatch, caplog):
        self._patch(monkeypatch, [1])  # absent after first delete
        with caplog.at_level("INFO"):
            wifi_provision._teardown_captive_portal()
        assert "Captive portal config removed" in caplog.text
        assert "survived teardown" not in caplog.text

    def test_retries_then_errors_when_table_persists(self, monkeypatch, caplog):
        calls = self._patch(monkeypatch, [0, 0])  # present after both deletes
        with caplog.at_level("ERROR"):
            wifi_provision._teardown_captive_portal()
        # Deleted twice (retry), still present → loud error naming the risk.
        assert sum(1 for c in calls if "delete" in c and "table" in c) == 2
        assert "survived teardown" in caplog.text
        assert "unreachable" in caplog.text


class TestConnectToWifiHidden:
    """litclock-dev#554: a hidden network never appears in a scan, so setup
    grew a free-text SSID field. Reaching such a network needs
    `hidden yes` — nmcli otherwise waits for a beacon the AP never sends.
    It stays OFF for scanned networks: it makes the device broadcast the
    SSID in probe requests, which is a fair trade to reach a network you
    otherwise cannot, but not one to impose on the ordinary path.
    """

    def _patch(self, monkeypatch, returncode=0, stderr=""):
        calls: list[list[str]] = []

        def fake_run(args, check=True, sudo=False):
            calls.append(list(args))
            return SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)

        monkeypatch.setattr(wifi_provision, "_run_nmcli", fake_run)
        # Short-circuit the post-connect IP wait so the test doesn't sleep.
        monkeypatch.setattr(wifi_provision, "is_wifi_connected", lambda: True)
        monkeypatch.setattr(wifi_provision, "get_wifi_ssid", lambda: "HiddenNet")
        monkeypatch.setattr(wifi_provision, "_clear_wifi_watchdog_counter", lambda: None)
        return calls

    def test_hidden_true_appends_hidden_yes(self, monkeypatch):
        calls = self._patch(monkeypatch)
        ok, err = wifi_provision.connect_to_wifi("HiddenNet", "pw", hidden=True)
        assert (ok, err) == (True, None)
        connect = next(c for c in calls if "connect" in c)
        # Adjacent pair, not just both present — nmcli parses `hidden` and its
        # value positionally, so a split would change what it means.
        assert connect[connect.index("hidden") + 1] == "yes"

    def test_hidden_defaults_off_for_scanned_networks(self, monkeypatch):
        calls = self._patch(monkeypatch)
        wifi_provision.connect_to_wifi("HomeWiFi", "pw")
        connect = next(c for c in calls if "connect" in c)
        assert "hidden" not in connect

    def test_hidden_false_is_explicitly_off(self, monkeypatch):
        calls = self._patch(monkeypatch)
        wifi_provision.connect_to_wifi("HomeWiFi", "pw", hidden=False)
        connect = next(c for c in calls if "connect" in c)
        assert "hidden" not in connect

    def test_not_found_on_typed_ssid_blames_the_spelling(self, monkeypatch):
        """A hand-typed name nmcli can't find is nearly always a typo or a
        case mismatch. The generic "not found" sends the user hunting for a
        fault that isn't there; say what to check instead."""
        self._patch(monkeypatch, returncode=1, stderr="Error: No network with SSID 'Hiddennet' found.")
        ok, err = wifi_provision.connect_to_wifi("Hiddennet", "pw", hidden=True)
        assert ok is False
        assert "case-sensitive" in err
        assert "Hiddennet" in err

    def test_not_found_on_scanned_ssid_keeps_the_plain_message(self, monkeypatch):
        """The scanned path has no spelling to doubt — the user picked from a
        list — so it must NOT inherit the typo advice."""
        self._patch(monkeypatch, returncode=1, stderr="Error: No network with SSID 'HomeWiFi' found.")
        ok, err = wifi_provision.connect_to_wifi("HomeWiFi", "pw")
        assert ok is False
        assert "case-sensitive" not in err
        assert "'HomeWiFi' not found" in err


class TestNmcliSecretRedaction:
    """The connect argv carries the recipient's home WiFi PSK in the clear.
    journald ships Storage=persistent on the flashed image and the support
    bundle collects it, so anyone who raises the log level once to debug a
    first-boot failure would write that password to disk permanently, where
    it then travels off the device (/review, litclock-dev#580)."""

    def test_password_value_is_replaced(self):
        cmd = ["sudo", "nmcli", "device", "wifi", "connect", "Home", "password", "s3cret", "ifname", "wlan0"]
        assert wifi_provision._redact_nmcli(cmd) == [
            "sudo",
            "nmcli",
            "device",
            "wifi",
            "connect",
            "Home",
            "password",
            "***",
            "ifname",
            "wlan0",
        ]

    def test_psk_property_form_is_also_redacted(self):
        """`nmcli connection modify` takes the secret as a dotted property."""
        assert wifi_provision._redact_nmcli(["nmcli", "c", "modify", "x", "wifi-sec.psk", "s3cret"])[-1] == "***"

    def test_ssid_and_flags_survive(self):
        """Redaction must not eat the parts an operator needs to debug with."""
        out = wifi_provision._redact_nmcli(
            ["nmcli", "device", "wifi", "connect", "MyNet", "password", "pw", "hidden", "yes"]
        )
        assert "MyNet" in out and "hidden" in out and "yes" in out

    def test_trailing_password_key_does_not_crash(self):
        assert wifi_provision._redact_nmcli(["nmcli", "password"]) == ["nmcli", "password"]

    def test_the_debug_line_itself_is_redacted(self, monkeypatch, caplog):
        """The unit above is only useful if _run_nmcli actually calls it."""
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr="")
        )
        with caplog.at_level("DEBUG"):
            wifi_provision._run_nmcli(["device", "wifi", "connect", "Home", "password", "s3cret"], sudo=True)
        assert "s3cret" not in caplog.text
        assert "***" in caplog.text
