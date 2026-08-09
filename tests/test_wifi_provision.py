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

    def _patch(self, monkeypatch, returncode=0, stderr="", connected=True):
        """``connected`` drives is_wifi_connected: True short-circuits the
        post-connect IP wait (success paths); False is required by the
        timeout-delete tests, because the rescue check (litclock-dev#600 review) treats
        a live connection after the bound as success, not as a cleanup case."""
        calls: list[list[str]] = []
        timeouts: list = []

        def fake_run(args, check=True, sudo=False, timeout=None):
            calls.append(list(args))
            timeouts.append(timeout)
            # The connect call must carry the litclock-dev#598 activation bound
            # (hardware-measured ~107s hang on exists-but-hidden SSIDs).
            if "connect" in args:
                assert timeout == 30, "connect must be bounded (litclock-dev#598)"
            return SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)

        self.timeouts = timeouts

        monkeypatch.setattr(wifi_provision, "_run_nmcli", fake_run)
        monkeypatch.setattr(wifi_provision, "is_wifi_connected", lambda: connected)
        monkeypatch.setattr(wifi_provision, "get_wifi_ssid", lambda: "HiddenNet")
        monkeypatch.setattr(wifi_provision, "_clear_wifi_watchdog_counter", lambda: None)
        # Both the rescue window and the IP wait poll with 1s sleeps.
        monkeypatch.setattr(wifi_provision.time, "sleep", lambda _s: None)
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

    def test_not_found_on_typed_ssid_leads_with_hidden_causes(self, monkeypatch):
        """litclock-dev#598 (hardware-established): a typed name nmcli can't
        find is often NOT a typo — a hidden 5GHz-only network is invisible to
        the 2.4GHz radio, and a hidden 2.4GHz one can be until it enters the
        scan cache. Lead with those causes; keep the spelling hint, but last."""
        self._patch(monkeypatch, returncode=1, stderr="Error: No network with SSID 'Hiddennet' found.")
        ok, err = wifi_provision.connect_to_wifi("Hiddennet", "pw", hidden=True)
        assert ok is False
        assert "Hiddennet" in err
        assert "2.4GHz" in err  # the likely causes lead
        assert "case-sensitive" in err  # spelling hint retained
        assert err.index("2.4GHz") < err.index("case-sensitive")

    def test_connect_timeout_returns_honest_copy_and_deletes_profile(self, monkeypatch):
        """litclock-dev#598: the bounded connect (synthetic returncode 124)
        must (a) tell the user the network didn't answer — not blame their
        spelling — and (b) delete the half-created profile: the kill only
        reaches the sudo wrapper and NetworkManager's activation job keeps
        running, so this delete is the one thing that aborts it AND drops
        the armed profile (the litclock-dev#595 class)."""
        calls = self._patch(
            monkeypatch, returncode=wifi_provision.NMCLI_TIMEOUT_RC, stderr="nmcli timed out", connected=False
        )
        ok, err = wifi_provision.connect_to_wifi("HiddenNet", "pw", hidden=True)
        assert ok is False
        assert "didn't answer in time" in err
        assert "case-sensitive" not in err  # spelling is not the story here
        delete_idx = [i for i, c in enumerate(calls) if "delete" in c]
        assert delete_idx, "timeout must trigger the profile delete"
        delete = calls[delete_idx[0]]
        # Explicit "id" selector — bare `delete <ssid>` lets nmcli spec-guess,
        # and an SSID named "id"/"uuid"/"path" would break the cleanup.
        assert delete[delete.index("delete") + 1 :] == ["id", "HiddenNet"]
        # The delete itself must stay bounded — an unbounded delete against
        # the same wedged NetworkManager would recreate the very hang this
        # timeout exists to prevent, on the cleanup path.
        assert self.timeouts[delete_idx[0]] == 10

    def test_connect_timeout_on_scanned_network_also_cleans_up(self, monkeypatch):
        """The rc-124 branch fires before the hidden/typed split — a scanned
        pick that times out needs the same honest copy and the same profile
        cleanup, and must keep needing them if the branch ever moves."""
        calls = self._patch(
            monkeypatch, returncode=wifi_provision.NMCLI_TIMEOUT_RC, stderr="nmcli timed out", connected=False
        )
        ok, err = wifi_provision.connect_to_wifi("HomeWiFi", "pw")
        assert ok is False
        assert "didn't answer in time" in err
        assert any("delete" in c and "HomeWiFi" in c for c in calls)

    def test_connect_timeout_rescues_a_join_that_landed_late(self, monkeypatch):
        """litclock-dev#600 review: the kill never stops NM's activation, so a slow-but-
        genuine join (mesh/band-steering DHCP takes 30-45s) can land AFTER
        the bound. Deleting then would tear down a working connection and
        every retry would collide identically — the network becomes
        permanently unprovisionable. A landed join is a success, and the
        half-created-profile delete must NOT run."""
        calls = self._patch(
            monkeypatch, returncode=wifi_provision.NMCLI_TIMEOUT_RC, stderr="nmcli timed out", connected=True
        )
        ok, err = wifi_provision.connect_to_wifi("HiddenNet", "pw", hidden=True)
        assert (ok, err) == (True, None)
        assert not any("delete" in c for c in calls), "rescued join must not delete the live profile"

    def test_connect_timeout_rescue_requires_the_target_ssid(self, monkeypatch):
        """Connected-to-something isn't rescued — only the SSID this attempt
        targeted. NM autoconnecting to a leftover profile (the litclock-dev#595 class)
        must still be treated as a failed attempt with cleanup."""
        self._patch(monkeypatch, returncode=wifi_provision.NMCLI_TIMEOUT_RC, stderr="nmcli timed out", connected=True)
        # get_wifi_ssid (patched in _patch) reports "HiddenNet" — attempt a
        # different network, so the match fails and the timeout path holds.
        ok, err = wifi_provision.connect_to_wifi("OtherNet", "pw", hidden=True)
        assert ok is False
        assert "didn't answer in time" in err

    def test_connect_timeout_logs_when_cleanup_delete_fails(self, monkeypatch, caplog):
        """The delete is the only thing standing between a timeout and the
        litclock-dev#595 armed-profile class — its failure must not be
        silent (this repo has shipped that exact swallow before)."""
        calls: list[list[str]] = []

        def fake_run(args, check=True, sudo=False, timeout=None):
            calls.append(list(args))
            # Connect times out; the cleanup delete then fails too.
            return SimpleNamespace(returncode=wifi_provision.NMCLI_TIMEOUT_RC, stdout="", stderr="nmcli timed out")

        monkeypatch.setattr(wifi_provision, "_run_nmcli", fake_run)
        monkeypatch.setattr(wifi_provision, "is_wifi_connected", lambda: False)  # no rescue
        monkeypatch.setattr(wifi_provision.time, "sleep", lambda _s: None)
        with caplog.at_level("ERROR"):
            ok, err = wifi_provision.connect_to_wifi("HiddenNet", "pw", hidden=True)
        assert ok is False
        assert "Could not delete half-created profile 'HiddenNet'" in caplog.text
        assert "litclock-dev#595" in caplog.text

    @pytest.mark.parametrize(("flag", "expected"), [([], 30), (["--timeout", "0"], None), (["--timeout", "90"], 90)])
    def test_cli_connect_timeout_flag(self, monkeypatch, capsys, flag, expected):
        """litclock-dev#600 review: the CLI is also the manual/SSH recovery path, where
        the setup flow's 30s bound + delete-on-timeout can destroy the very
        profile an operator is relying on. --timeout 0 must map to an
        unbounded connect (None); the default stays the setup bound."""
        seen = {}

        def fake_connect(ssid, password, hidden=False, connect_timeout=30):
            seen["timeout"] = connect_timeout
            return True, None

        monkeypatch.setattr(wifi_provision, "connect_to_wifi", fake_connect)
        monkeypatch.setattr(
            wifi_provision.sys, "argv", ["wifi_provision.py", "connect", "--ssid", "Net", "--password", "pw", *flag]
        )
        with pytest.raises(SystemExit) as exc:
            wifi_provision.main()
        assert exc.value.code == 0
        assert seen["timeout"] == expected

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
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))
        with caplog.at_level("DEBUG"):
            wifi_provision._run_nmcli(["device", "wifi", "connect", "Home", "password", "s3cret"], sudo=True)
        assert "s3cret" not in caplog.text
        assert "***" in caplog.text


class TestRunNmcliTimeout:
    """The real subprocess.TimeoutExpired handler in _run_nmcli. Every
    connect_to_wifi test mocks _run_nmcli wholesale, so without these the
    actual except-path has zero coverage — a regression there (exception
    escaping, wrong sentinel, lost stderr contract) would crash on hardware
    while the whole suite stayed green (litclock-dev#598)."""

    @staticmethod
    def _raise_timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    def test_timeout_becomes_synthetic_completed_process(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", self._raise_timeout)
        result = wifi_provision._run_nmcli(
            ["device", "wifi", "connect", "Net", "password", "pw"], check=False, timeout=30
        )
        assert result.returncode == wifi_provision.NMCLI_TIMEOUT_RC
        assert result.stderr == "nmcli timed out"  # connect_to_wifi's contract
        assert result.stdout == ""

    def test_timeout_error_line_is_redacted(self, monkeypatch, caplog):
        """The timeout log line is ERROR-level — unlike the DEBUG line it
        reaches journald at the default WARNING log level, so a future edit
        dropping _redact_nmcli here would persist the PSK to disk (litclock-dev#580)."""
        monkeypatch.setattr(subprocess, "run", self._raise_timeout)
        with caplog.at_level("ERROR"):
            wifi_provision._run_nmcli(
                ["device", "wifi", "connect", "Home", "password", "s3cret"], sudo=True, timeout=30
            )
        assert "s3cret" not in caplog.text
        assert "***" in caplog.text

    def test_timeout_preserves_partial_output_in_the_log(self, monkeypatch, caplog):
        """Whatever nmcli wrote before the kill is the only evidence of WHY
        the activation stalled (slow DHCP vs hidden 5GHz vs wedged NM) —
        the handler must not discard it. TimeoutExpired carries the partial
        output as bytes even under text=True."""

        def raise_with_output(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"), output=b"Device activation was stalled")

        monkeypatch.setattr(subprocess, "run", raise_with_output)
        with caplog.at_level("ERROR"):
            wifi_provision._run_nmcli(["device", "wifi", "connect", "Net", "password", "pw"], timeout=30)
        assert "Device activation was stalled" in caplog.text
