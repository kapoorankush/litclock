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

import re
import string
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


class TestValidateHotspotCredentials:
    """litclock-dev#626: out-of-spec credentials are rejected where the AP is
    created, so the live AP, the e-ink QR, the /etc/issue banner, and the
    splash step lines can never disagree about what the network is."""

    def test_shipped_defaults_are_valid(self):
        # The production path: DEFAULT_SSID + a generated 8-char password.
        pw = wifi_provision._generate_password()
        assert wifi_provision.validate_hotspot_credentials(wifi_provision.DEFAULT_SSID, pw) is None

    def test_generated_password_invariants_are_pinned(self):
        # Don't just sample one random draw: pin the generator's contract so a
        # future alphabet change that could emit a validator-rejected char
        # fails deterministically (/review, Codex). Length 8, alnum ASCII, and
        # every char of the drawing alphabet is itself validator-clean.
        pw = wifi_provision._generate_password()
        assert len(pw) == 8 and pw.isalnum() and pw.isascii()
        alphabet = string.ascii_letters + string.digits
        assert set(pw) <= set(alphabet)
        for ch in alphabet:
            assert wifi_provision.validate_hotspot_credentials(wifi_provision.DEFAULT_SSID, ch * 8) is None

    def test_boundary_lengths_are_valid(self):
        assert wifi_provision.validate_hotspot_credentials("S" * 32, "p" * 8) is None
        assert wifi_provision.validate_hotspot_credentials("S", "p" * 63) is None

    def test_ssid_over_32_chars_rejected(self):
        error = wifi_provision.validate_hotspot_credentials("S" * 33, "p" * 8)
        assert error and "33" in error

    def test_empty_or_blank_ssid_rejected(self):
        # All-spaces is as bad as empty: invisible on every surface, and phone
        # QR parsers whitespace-trim the payload (/review adversarial pass).
        for bad in ("", "   "):
            assert "blank" in wifi_provision.validate_hotspot_credentials(bad, "p" * 8)

    def test_control_chars_in_ssid_rejected(self):
        # A newline here is the exact QR-vs-AP divergence litclock-dev#589 renders
        # around: nmcli would create the AP with the raw value while the QR
        # encodes the sanitized one.
        for bad in ("Lit\nClock", "Lit\rClock", "Lit\x00Clock", "Lit\u2028Clock"):
            error = wifi_provision.validate_hotspot_credentials(bad, "p" * 8)
            assert error and "ASCII" in error

    def test_non_ascii_ssid_rejected(self):
        # Deliberately tighter than 802.11 (/review, three passes converged):
        # Aileron has no CJK/emoji glyphs (tofu on the e-ink), bash pads the
        # /etc/issue box by bytes while counting chars, and phone QR parsers
        # mangle non-ASCII -- ASCII is the one alphabet where every surface
        # renders the same name.
        for bad in ("Caf\u00e9 WiFi 5G", "\u65e5" * 10 + "ab"):
            error = wifi_provision.validate_hotspot_credentials(bad, "p" * 8)
            assert error and "ASCII" in error

    def test_ssid_with_spaces_is_valid(self):
        assert wifi_provision.validate_hotspot_credentials("My Home 5G", "p" * 8) is None

    def test_missing_password_returns_error_not_raise(self):
        # The validator reads as public API: None/empty must yield an error
        # string, never a TypeError (/review adversarial pass).
        assert "missing" in wifi_provision.validate_hotspot_credentials("LitClock-Setup", None)
        assert "missing" in wifi_provision.validate_hotspot_credentials("LitClock-Setup", "")

    def test_password_length_bounds_rejected(self):
        # WPA2-PSK passphrases are 8-63 chars; the error must not echo the value.
        for bad in ("p" * 7, "p" * 64):
            error = wifi_provision.validate_hotspot_credentials("LitClock-Setup", bad)
            assert error and "8-63" in error
            assert bad not in error

    def test_password_outside_printable_ascii_rejected(self):
        for bad in ("pässword", "pass\tword1", "password\x00"):
            error = wifi_provision.validate_hotspot_credentials("LitClock-Setup", bad)
            assert error and "ASCII" in error
            assert bad not in error


def test_create_hotspot_rejects_invalid_credentials_before_any_side_effect(monkeypatch):
    """litclock-dev#626: a rejected pair must leave the system exactly as it
    was — no hotspot teardown, no captive-portal config, no nmcli, not even
    the wifi-ready probe."""
    called = []

    def should_not_run(*args, **kwargs):
        called.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(wifi_provision, "ensure_wifi_ready", should_not_run)
    monkeypatch.setattr(wifi_provision, "teardown_hotspot", should_not_run)
    monkeypatch.setattr(wifi_provision, "_setup_captive_portal", should_not_run)
    monkeypatch.setattr(wifi_provision, "_run_nmcli", should_not_run)

    assert wifi_provision.create_hotspot(ssid="Lit\nClock", password="p" * 8) is None
    assert wifi_provision.create_hotspot(ssid="LitClock-Setup", password="short") is None
    assert called == [], "create_hotspot must reject invalid credentials before any side effect"


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

    def _patch(
        self,
        monkeypatch,
        returncode=0,
        stderr="",
        connected=True,
        uuids_before=(),
        uuids_created=(),
        uuids_show_rc=0,
        delete_rc=0,
        disconnect_rc=0,
        expected_connect_timeout=30,
    ):
        """``connected`` drives is_wifi_connected: True short-circuits the
        post-connect IP wait (success paths); False is required by the
        timeout-delete tests, because the rescue check (litclock-dev#600 review) treats
        a live connection after the bound as success, not as a cleanup case.

        litclock-dev#595 per-command scripting: the UUID listing reports
        ``uuids_before`` until the CONNECT call has been observed, and
        ``uuids_before + uuids_created`` after it — keyed on the connect,
        not on call count, so a snapshot taken on the wrong side of the
        connect reads the wrong set and fails (a call-count key let the
        snapshot-after-connect mutation pass the whole suite — litclock-dev#609
        testing-specialist finding). ``uuids_show_rc`` != 0 fails the
        listing (snapshot/diff unavailable); ``delete_rc`` / ``disconnect_rc``
        script those commands' outcomes."""
        calls: list[list[str]] = []
        timeouts: list = []
        connect_seen = {"v": False}

        def fake_run(args, check=True, sudo=False, timeout=None, **_kw):
            calls.append(list(args))
            timeouts.append(timeout)
            if args == ["-t", "-f", "UUID", "connection", "show"]:
                if uuids_show_rc:
                    return SimpleNamespace(returncode=uuids_show_rc, stdout="", stderr="cannot list")
                uuids = list(uuids_before) + (list(uuids_created) if connect_seen["v"] else [])
                return SimpleNamespace(returncode=0, stdout="".join(f"{u}\n" for u in uuids), stderr="")
            if "delete" in args:
                return SimpleNamespace(returncode=delete_rc, stdout="", stderr="")
            if args == ["device", "disconnect", "wlan0"]:
                return SimpleNamespace(returncode=disconnect_rc, stdout="", stderr="")
            # The connect call must carry the litclock-dev#598 activation bound
            # (hardware-measured ~107s hang on exists-but-hidden SSIDs).
            if "connect" in args:
                connect_seen["v"] = True
                assert timeout == expected_connect_timeout, "connect must carry the expected bound (litclock-dev#598)"
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

    def test_connect_timeout_returns_honest_copy_aborts_and_deletes_created(self, monkeypatch):
        """litclock-dev#598 + litclock-dev#609 review: the bounded connect (synthetic
        returncode 124) must (a) tell the user the network didn't answer —
        not blame their spelling — (b) abort the still-running activation at
        the DEVICE (the kill only reaches the sudo wrapper; NM keeps trying,
        and a delete only doubles as abort when the activated profile
        happens to be named the SSID), and (c) delete the profile this
        attempt created, identified by UUID diff — which also covers NM
        uniquifying a colliding name ("MyNet 1")."""
        calls = self._patch(
            monkeypatch,
            returncode=wifi_provision.NMCLI_TIMEOUT_RC,
            stderr="nmcli timed out",
            connected=False,
            uuids_before=("u-old",),
            uuids_created=("u-new",),
        )
        ok, err = wifi_provision.connect_to_wifi("HiddenNet", "pw", hidden=True)
        assert ok is False
        assert "didn't answer in time" in err
        assert "case-sensitive" not in err  # spelling is not the story here
        disconnect_idx = [i for i, c in enumerate(calls) if c == ["device", "disconnect", "wlan0"]]
        assert disconnect_idx, "timeout must abort the activation at the device"
        # The abort must stay bounded — an unbounded call against the same
        # wedged NetworkManager would recreate the very hang this timeout
        # exists to prevent, on the cleanup path.
        assert self.timeouts[disconnect_idx[0]] == 10
        delete_idx = [i for i, c in enumerate(calls) if "delete" in c]
        assert delete_idx, "timeout must delete the created profile"
        delete = calls[delete_idx[0]]
        assert delete[delete.index("delete") + 1 :] == ["uuid", "u-new"]
        assert self.timeouts[delete_idx[0]] == 10
        # And never the pre-existing profile's UUID.
        assert not any("u-old" in c for c in calls if "delete" in c)

    def test_connect_timeout_on_scanned_network_also_cleans_up(self, monkeypatch):
        """The rc-124 branch fires before the hidden/typed split — a scanned
        pick that times out needs the same honest copy and the same profile
        cleanup, and must keep needing them if the branch ever moves."""
        calls = self._patch(
            monkeypatch,
            returncode=wifi_provision.NMCLI_TIMEOUT_RC,
            stderr="nmcli timed out",
            connected=False,
            uuids_created=("u-x",),
        )
        ok, err = wifi_provision.connect_to_wifi("HomeWiFi", "pw")
        assert ok is False
        assert "didn't answer in time" in err
        assert any("delete" in c and "u-x" in c for c in calls)

    def test_connect_timeout_rescues_a_join_that_landed_late(self, monkeypatch):
        """litclock-dev#600 review: the kill never stops NM's activation, so a slow-but-
        genuine join (mesh/band-steering DHCP takes 30-45s) can land AFTER
        the bound. Deleting then would tear down a working connection and
        every retry would collide identically — the network becomes
        permanently unprovisionable. A landed join is a success: no delete,
        and no device disconnect either (it would tear down the join)."""
        calls = self._patch(
            monkeypatch,
            returncode=wifi_provision.NMCLI_TIMEOUT_RC,
            stderr="nmcli timed out",
            connected=True,
            uuids_created=("u-live",),
        )
        ok, err = wifi_provision.connect_to_wifi("HiddenNet", "pw", hidden=True)
        assert (ok, err) == (True, None)
        assert not any("delete" in c for c in calls), "rescued join must not delete the live profile"
        assert ["device", "disconnect", "wlan0"] not in calls, "rescued join must not be disconnected"

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

        def fake_run(args, check=True, sudo=False, timeout=None, **_kw):
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

    # ── litclock-dev#595: failed attempts must not leave armed profiles ──
    # (in this class rather than a subclass so pytest doesn't re-run the
    # whole fixture surface twice — the litclock-dev#605 item-8 lesson.)

    def test_wrong_password_deletes_the_profile_this_attempt_created(self, monkeypatch):
        """The hardware repro (2026-08-08 20:12): a wrong PSK fails via
        nmcli's own WRONG_KEY exit in ~24s and leaves the profile saved with
        autoconnect=yes and the bad password. On a fielded device that
        profile autoconnect-loops failed auth whenever the real network
        drops — every retry on the single radio is time NOT rejoining the
        good network. UUID-identified, so it holds even when NM uniquified
        the name ("MyNet 1") because a same-named profile already existed."""
        calls = self._patch(
            monkeypatch,
            returncode=1,
            stderr="Error: Connection activation failed: Secrets were required, but not provided.",
            connected=False,
            uuids_before=("u-preexisting",),
            uuids_created=("u-new",),
        )
        ok, err = wifi_provision.connect_to_wifi("HomeWiFi", "badpw")
        assert (ok, err) == (False, "Incorrect WiFi password")
        delete = next(c for c in calls if "delete" in c)
        assert delete[delete.index("delete") + 1 :] == ["uuid", "u-new"]
        # Snapshot ORDER is load-bearing (litclock-dev#609 testing specialist): the first
        # UUID listing must precede the connect, or the snapshot would see
        # the just-created profile and classify it pre-existing, disabling
        # the entire cleanup.
        first_show = next(i for i, c in enumerate(calls) if c == ["-t", "-f", "UUID", "connection", "show"])
        connect = next(i for i, c in enumerate(calls) if "connect" in c)
        assert first_show < connect, "pre-connect snapshot must be taken BEFORE the connect"

    def test_wrong_password_with_a_reused_profile_deletes_nothing(self, monkeypatch):
        """`nmcli device wifi connect` REUSES an existing profile for the
        same SSID (under ANY name — the UUID diff makes the profile's name
        irrelevant). Deleting on auth failure would destroy a previously-
        working saved network — the hazard the owner declined to risk in
        litclock-dev#600. Nothing new appeared, so nothing is deleted."""
        calls = self._patch(
            monkeypatch,
            returncode=1,
            stderr="Error: Connection activation failed: Secrets were required, but not provided.",
            connected=False,
            uuids_before=("u-home",),
            uuids_created=(),
        )
        ok, _err = wifi_provision.connect_to_wifi("HomeWiFi", "badpw")
        assert ok is False
        assert not any("delete" in c for c in calls)

    def test_success_never_deletes(self, monkeypatch):
        """Mutation-hunting pin (litclock-dev#609 testing specialist): an unconditional
        cleanup on the success path passed the whole suite before this test.
        A successful join created a profile with a VERIFIED password —
        deleting it would drop WiFi on the next disconnect or reboot."""
        calls = self._patch(
            monkeypatch,
            returncode=0,
            connected=True,
            uuids_before=("u-old",),
            uuids_created=("u-good",),
        )
        ok, err = wifi_provision.connect_to_wifi("HiddenNet", "pw", hidden=True)
        assert (ok, err) == (True, None)
        assert not any("delete" in c for c in calls)
        assert ["device", "disconnect", "wlan0"] not in calls

    def test_not_found_failure_also_cleans_up_a_created_profile(self, monkeypatch):
        """The cleanup is failure-class-agnostic: any nmcli self-exit that
        left a profile behind gets the same diff-based delete."""
        calls = self._patch(
            monkeypatch,
            returncode=1,
            stderr="Error: No network with SSID 'GhostNet' found.",
            connected=False,
            uuids_created=("u-ghost",),
        )
        ok, _err = wifi_provision.connect_to_wifi("GhostNet", "pw", hidden=True)
        assert ok is False
        assert any("delete" in c and "u-ghost" in c for c in calls)

    def test_generic_failure_also_cleans_up_a_created_profile(self, monkeypatch):
        """litclock-dev#609 testing specialist: hoisting the cleanup into only the auth
        and not-found branches passed the whole suite — the fall-through
        'Connection failed:' class (device busy, activation failed with any
        other reason) must keep the same cleanup."""
        calls = self._patch(
            monkeypatch,
            returncode=1,
            stderr="Error: Connection activation failed: (53) The device is busy.",
            connected=False,
            uuids_created=("u-busy",),
        )
        ok, err = wifi_provision.connect_to_wifi("Net", "pw")
        assert ok is False
        assert err.startswith("Connection failed:")
        assert any("delete" in c and "u-busy" in c for c in calls)

    def test_wrong_password_delete_failure_is_logged(self, monkeypatch, caplog):
        """The delete is the only thing standing between an auth failure and
        the armed-profile class — its failure must not be silent, and the
        user must still get the honest password copy."""
        self._patch(
            monkeypatch,
            returncode=1,
            stderr="Error: Connection activation failed: Secrets were required, but not provided.",
            connected=False,
            uuids_created=("u-n",),
            delete_rc=1,
        )
        with caplog.at_level("ERROR"):
            ok, err = wifi_provision.connect_to_wifi("HomeWiFi", "badpw")
        assert (ok, err) == (False, "Incorrect WiFi password")
        assert "Could not delete profile u-n" in caplog.text
        assert "litclock-dev#595" in caplog.text

    def test_wrong_password_with_unavailable_listing_skips_delete_and_warns(self, monkeypatch, caplog):
        """litclock-dev#609 Codex: with the diff unavailable on a SELF-EXIT failure there
        is no activation to abort, so a name-guessed delete has no upside to
        weigh against deleting a good pre-existing profile. Skip, loudly."""
        calls = self._patch(
            monkeypatch,
            returncode=1,
            stderr="Error: Connection activation failed: Secrets were required, but not provided.",
            connected=False,
            uuids_show_rc=1,
        )
        with caplog.at_level("WARNING"):
            ok, _err = wifi_provision.connect_to_wifi("HomeWiFi", "badpw")
        assert ok is False
        assert not any("delete" in c for c in calls)
        assert "leaving profiles untouched" in caplog.text

    def test_unbounded_cli_timeout_opts_out_of_cleanup(self, monkeypatch):
        """litclock-dev#600 decision d: --timeout 0 is the SSH-recovery path, which must
        never delete profiles — litclock-dev#595's cleanup keeps that
        contract on every failure class, not just timeouts."""
        calls = self._patch(
            monkeypatch,
            returncode=1,
            stderr="Error: Connection activation failed: Secrets were required, but not provided.",
            connected=False,
            uuids_created=("u-n",),
            expected_connect_timeout=None,
        )
        ok, _err = wifi_provision.connect_to_wifi("Net", "pw", connect_timeout=None)
        assert ok is False
        assert not any("delete" in c for c in calls)

    def test_timeout_with_a_reused_profile_disconnects_and_deletes_nothing(self, monkeypatch):
        """rc-124 with a reused profile: the activation must still be
        aborted (the radio is needed for the hotspot restore) but nothing
        new was created, so nothing is deleted — regardless of what the
        reused profile is NAMED (litclock-dev#609: nmcli reuses by SSID match, and an
        operator can rename a profile over SSH)."""
        calls = self._patch(
            monkeypatch,
            returncode=wifi_provision.NMCLI_TIMEOUT_RC,
            stderr="nmcli timed out",
            connected=False,
            uuids_before=("u-renamed",),
            uuids_created=(),
        )
        ok, err = wifi_provision.connect_to_wifi("HiddenNet", "pw", hidden=True)
        assert ok is False
        assert "didn't answer in time" in err
        assert not any("delete" in c for c in calls)
        assert ["device", "disconnect", "wlan0"] in calls

    def test_timeout_disconnect_failure_is_logged_and_delete_still_runs(self, monkeypatch, caplog):
        """The abort and the cleanup are independent halves — a failed
        disconnect must be loud (the activation is racing the hotspot
        restore) and must not skip the armed-profile delete."""
        calls = self._patch(
            monkeypatch,
            returncode=wifi_provision.NMCLI_TIMEOUT_RC,
            stderr="nmcli timed out",
            connected=False,
            uuids_created=("u-n",),
            disconnect_rc=1,
        )
        with caplog.at_level("ERROR"):
            ok, _err = wifi_provision.connect_to_wifi("HiddenNet", "pw", hidden=True)
        assert ok is False
        assert "may still be racing the hotspot restore" in caplog.text
        assert any("delete" in c and "u-n" in c for c in calls)

    def test_timeout_falls_back_to_name_delete_when_the_diff_is_unavailable(self, monkeypatch):
        """A listing hiccup must not leave the armed profile: on the timeout
        path the attempt got far enough to activate, so a leftover is
        near-certain — fall back to litclock-dev#600's shipped name-targeted delete
        (explicit `id` selector). The device abort above already handled
        the racing-activation half."""
        calls = self._patch(
            monkeypatch,
            returncode=wifi_provision.NMCLI_TIMEOUT_RC,
            stderr="nmcli timed out",
            connected=False,
            uuids_show_rc=1,
        )
        ok, _err = wifi_provision.connect_to_wifi("HiddenNet", "pw", hidden=True)
        assert ok is False
        assert ["device", "disconnect", "wlan0"] in calls
        delete = next(c for c in calls if "delete" in c)
        assert delete[delete.index("delete") + 1 :] == ["id", "HiddenNet"]


class TestPskRestore:
    """litclock-dev#613 (hardware-verified 2026-08-09, NM 1.42.4): a failed
    ``nmcli device wifi connect`` on an SSID with a PRE-EXISTING saved
    profile REUSES that profile and persists the attempted (wrong) password
    into it — same UUID, no new profile, so the litclock-dev#609 UUID set-diff cleanup
    is structurally blind. A previously-working saved network is left armed
    with a password that can never authenticate. connect_to_wifi now
    snapshots the stored PSK of matching pre-existing profiles before the
    attempt and restores it on the failure paths where nmcli reported the
    connect itself failed (NOT the rc=0 no-IP path — association there proves
    the submitted password was right).

    The fake mirrors the hardware behavior: the connect call rewrites the
    matching profile's stored PSK to the attempted password. It also serves
    the ``UUID,TYPE`` snapshot listing + per-profile ssid reads, terse-mode
    escaping the ssid field so the escaped-colon path is exercised."""

    GOOD = "correct-horse"
    SNAPSHOT_LISTING = ["-t", "-f", "UUID,TYPE", "connection", "show"]

    @staticmethod
    def _terse(value):
        """nmcli -t escapes ``\\`` and the ``:`` separator in value fields."""
        return value.replace("\\", "\\\\").replace(":", "\\:")

    def _patch(
        self,
        monkeypatch,
        *,
        profiles=None,
        connect_rc=1,
        connect_stderr="Error: Connection activation failed: Secrets were required, but not provided.",
        connected=False,
        listing_rc=0,
        connect_rewrites=True,
        connect_deletes=False,
        modify_rc=0,
        psk_read_rc=0,
    ):
        """``profiles``: uuid -> {"ssid": ..., "psk": ...} saved wifi profiles
        (a non-wifi ``u-eth`` entry is always present in the listing so the
        type filter is exercised). The connect call mutates the matching
        profile's psk (``connect_rewrites``) or removes the profile entirely
        (``connect_deletes`` — the vanished-before-restore race). ``psk_read_rc``
        != 0 fails every secret read (snapshot degradation path)."""
        profiles = {} if profiles is None else {u: dict(p) for u, p in profiles.items()}
        calls: list[list[str]] = []

        def fake_run(args, check=True, sudo=False, timeout=None, secret_output=False, input_text=None):
            calls.append(list(args))
            # The litclock-dev#599 invariant, enforced at the fake on EVERY
            # dispatch: no secret VALUE may appear in any argv token (sudo
            # audits argv to persistent journald), and the write-form
            # `password` key must never be present. Property NAMES in read
            # argvs (-s -g 802-11-wireless-security.psk) are fine — they
            # introduce no value.
            for _sec in [prof["psk"] for prof in profiles.values()]:
                assert not (_sec and any(_sec in tok for tok in args)), f"secret value in argv: {args}"
            assert "password" not in args, f"password key in argv: {args}"
            if args == ["-t", "-f", "UUID", "connection", "show"]:
                return SimpleNamespace(returncode=0, stdout="".join(f"{u}\n" for u in profiles), stderr="")
            if args[:2] == ["-t", "-f"] and args[3:] == ["connection", "show"]:
                # Real nmcli 1.42.4: the LIST form accepts only profile
                # COLUMNS; a setting property (contains a dot) is rc=2
                # "invalid field". The old fake accepted the dotted field,
                # which hid that the litclock-dev#616 one-listing snapshot NEVER worked
                # on hardware (litclock-dev#630 retest).
                if "." in args[2]:
                    return SimpleNamespace(returncode=2, stdout="", stderr="Error: invalid field")
                if listing_rc:
                    return SimpleNamespace(returncode=listing_rc, stdout="", stderr="cannot list")
                body = "".join(f"{u}:802-11-wireless\n" for u in profiles)
                return SimpleNamespace(returncode=0, stdout=body + "u-eth:802-3-ethernet\n", stderr="")
            if args[:3] == ["-t", "-g", "802-11-wireless.ssid"]:
                uuid = args[-1]
                if uuid not in profiles:
                    return SimpleNamespace(returncode=10, stdout="", stderr="unknown")
                return SimpleNamespace(returncode=0, stdout=self._terse(profiles[uuid]["ssid"]) + "\n", stderr="")
            if args[:3] == ["-s", "-g", "802-11-wireless-security.psk"]:
                uuid = args[-1]
                if psk_read_rc or uuid not in profiles:
                    return SimpleNamespace(returncode=psk_read_rc or 10, stdout="", stderr="unreadable")
                # -g output is terse-ESCAPED even for one field (verified on
                # NM 1.42.4) — the fake mirrors that so the production
                # unescape is exercised, not optional (litclock-dev#599 F1).
                return SimpleNamespace(returncode=0, stdout=self._terse(profiles[uuid]["psk"]) + "\n", stderr="")
            if args[:3] == ["connection", "edit", "uuid"]:
                # litclock-dev#599: the restore write arrives as editor
                # commands on stdin, never as argv. Mirror the editor: `set`
                # writes the value, `remove` clears it; a failing editor
                # (modify_rc) changes nothing — the read-back verify is what
                # must catch that.
                uuid = args[3]
                assert input_text and input_text.endswith("quit\n"), "editor script must end with quit"
                if modify_rc == 0 and uuid in profiles:
                    m = re.search(r"^set 802-11-wireless-security\.psk (.*)$", input_text, re.M)
                    if m:
                        profiles[uuid]["psk"] = m.group(1)
                    elif "remove 802-11-wireless-security.psk" in input_text:
                        profiles[uuid]["psk"] = ""
                return SimpleNamespace(returncode=modify_rc, stdout="", stderr="")
            if "connect" in args:
                target = args[args.index("connect") + 1]
                # The attempted password arrives as the first stdin line
                # under --ask (litclock-dev#599) — never via argv or a file.
                if "--ask" in args:
                    assert input_text and input_text.endswith("\n"), "--ask needs a newline-terminated stdin secret"
                    attempted = input_text.splitlines()[0]
                else:
                    attempted = ""  # open network — no secrets channel at all
                for uuid in list(profiles):
                    if profiles[uuid]["ssid"] == target:
                        if connect_deletes:
                            del profiles[uuid]
                        elif connect_rewrites and attempted:
                            profiles[uuid]["psk"] = attempted
                return SimpleNamespace(returncode=connect_rc, stdout="", stderr=connect_stderr)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(wifi_provision, "_run_nmcli", fake_run)
        monkeypatch.setattr(wifi_provision, "is_wifi_connected", lambda: connected)
        monkeypatch.setattr(wifi_provision, "get_wifi_ssid", lambda: "")
        monkeypatch.setattr(wifi_provision, "_clear_wifi_watchdog_counter", lambda: None)
        monkeypatch.setattr(wifi_provision.time, "sleep", lambda _s: None)
        return calls, profiles

    def _modifies(self, calls):
        return [c for c in calls if c[:3] == ["connection", "edit", "uuid"]]

    def test_wrong_password_restores_the_saved_psk(self, monkeypatch):
        """The hardware repro, end-to-end: reused profile's stored PSK is
        the wrong password after the failed connect; the restore puts the
        good one back, by UUID, via editor commands on stdin — never a
        wifi-sec.psk argv token (litclock-dev#599)."""
        calls, profiles = self._patch(
            monkeypatch,
            profiles={
                "u-good": {"ssid": "HomeWiFi", "psk": self.GOOD},
                "u-other": {"ssid": "SomewhereElse", "psk": "unrelated"},
            },
        )
        ok, err = wifi_provision.connect_to_wifi("HomeWiFi", "WRONGWRONG")
        assert ok is False
        assert "Incorrect WiFi password" in err
        assert profiles["u-good"]["psk"] == self.GOOD, "the failed attempt's password must be un-written"
        assert profiles["u-other"]["psk"] == "unrelated"
        modifies = self._modifies(calls)
        assert modifies == [["connection", "edit", "uuid", "u-good"]]

    def test_unchanged_psk_is_left_alone(self, monkeypatch):
        """If NM didn't rewrite the profile, no modify is issued — the
        restore must be conditional, not unconditional churn."""
        calls, profiles = self._patch(
            monkeypatch,
            profiles={"u-good": {"ssid": "HomeWiFi", "psk": self.GOOD}},
            connect_rewrites=False,
        )
        ok, _err = wifi_provision.connect_to_wifi("HomeWiFi", "WRONGWRONG")
        assert ok is False
        assert self._modifies(calls) == []

    def test_timeout_path_restores_after_abort(self, monkeypatch):
        """The bounded-connect branch must restore too — and only after the
        activation abort, so the modify can't race the in-flight join."""
        calls, profiles = self._patch(
            monkeypatch,
            profiles={"u-good": {"ssid": "HomeWiFi", "psk": self.GOOD}},
            connect_rc=wifi_provision.NMCLI_TIMEOUT_RC,
            connect_stderr="nmcli timed out",
        )
        ok, err = wifi_provision.connect_to_wifi("HomeWiFi", "WRONGWRONG")
        assert ok is False
        assert "didn't answer in time" in err
        assert profiles["u-good"]["psk"] == self.GOOD
        modifies = self._modifies(calls)
        assert len(modifies) == 1
        assert calls.index(["device", "disconnect", "wlan0"]) < calls.index(modifies[0])

    def test_manual_timeout_none_never_snapshots_or_restores(self, monkeypatch):
        """litclock-dev#600 decision d: the SSH-recovery path (--timeout 0 →
        connect_timeout=None) must never touch profiles — that now includes
        never READING their secrets either."""
        calls, _profiles = self._patch(
            monkeypatch,
            profiles={"u-good": {"ssid": "HomeWiFi", "psk": self.GOOD}},
        )
        ok, _err = wifi_provision.connect_to_wifi("HomeWiFi", "WRONGWRONG", connect_timeout=None)
        assert ok is False
        assert self.SNAPSHOT_LISTING not in calls
        assert self._modifies(calls) == []

    def test_listing_failure_degrades_to_noop(self, monkeypatch):
        """Snapshot unavailable → restore is a no-op, the failure still
        returns normally — degraded observability, never a new crash."""
        calls, _profiles = self._patch(
            monkeypatch,
            profiles={"u-good": {"ssid": "HomeWiFi", "psk": self.GOOD}},
            listing_rc=1,
        )
        ok, err = wifi_provision.connect_to_wifi("HomeWiFi", "WRONGWRONG")
        assert ok is False
        assert "Incorrect WiFi password" in err
        assert self._modifies(calls) == []

    def test_success_keeps_the_newly_written_psk(self, monkeypatch):
        """On success NM stored the password that actually authenticated —
        restoring the old one would be wrong. No modify on the happy path."""
        calls, profiles = self._patch(
            monkeypatch,
            profiles={"u-good": {"ssid": "HomeWiFi", "psk": "old-rotated-away"}},
            connect_rc=0,
            connect_stderr="",
            connected=True,
        )
        ok, err = wifi_provision.connect_to_wifi("HomeWiFi", self.GOOD)
        assert (ok, err) == (True, None)
        assert profiles["u-good"]["psk"] == self.GOOD
        assert self._modifies(calls) == []

    def test_profile_vanished_before_restore_is_skipped(self, monkeypatch):
        """A profile deleted between snapshot and restore (e.g. by cleanup
        or an operator) is skipped — unreadable current PSK must not crash
        or spray modifies at dead UUIDs."""
        calls, _profiles = self._patch(
            monkeypatch,
            profiles={"u-good": {"ssid": "HomeWiFi", "psk": self.GOOD}},
            connect_deletes=True,
        )
        ok, _err = wifi_provision.connect_to_wifi("HomeWiFi", "WRONGWRONG")
        assert ok is False
        assert self._modifies(calls) == []

    def test_modify_failure_is_logged_loudly(self, monkeypatch, caplog):
        """A failed restore leaves the device unable to rejoin — that must
        reach the journal as an ERROR naming the consequence, never pass
        silently (the silent-swallow class)."""
        _calls, _profiles = self._patch(
            monkeypatch,
            profiles={"u-good": {"ssid": "HomeWiFi", "psk": self.GOOD}},
            modify_rc=4,
        )
        with caplog.at_level("ERROR"):
            ok, _err = wifi_provision.connect_to_wifi("HomeWiFi", "WRONGWRONG")
        assert ok is False
        assert "Could not restore the saved password" in caplog.text
        assert self.GOOD not in caplog.text, "the secret must never reach logs"

    def test_third_party_change_between_snapshot_and_failure_is_not_clobbered(self, monkeypatch):
        """litclock-dev#616 review (Codex): restore keys on ``current == attempted``, not
        ``current != good``. If something other than this attempt set the PSK
        (SSH, a concurrent NM edit) to a THIRD value, the restore must leave
        it alone rather than roll it back to the stale snapshot."""
        calls, profiles = self._patch(
            monkeypatch,
            profiles={"u-good": {"ssid": "HomeWiFi", "psk": self.GOOD}},
            connect_rewrites=False,  # this attempt did NOT write the profile
        )
        profiles["u-good"]["psk"] = "changed-by-someone-else"  # a third value
        ok, _err = wifi_provision.connect_to_wifi("HomeWiFi", "WRONGWRONG")
        assert ok is False
        assert self._modifies(calls) == []
        assert profiles["u-good"]["psk"] == "changed-by-someone-else"

    def test_open_network_empty_psk_is_never_modified(self, monkeypatch):
        """An open network has an empty stored PSK; the attempt password is
        also empty, so there is nothing this attempt could have corrupted —
        no modify (attempted == good == '')."""
        calls, _profiles = self._patch(
            monkeypatch,
            profiles={"u-open": {"ssid": "CafeWiFi", "psk": ""}},
        )
        ok, _err = wifi_provision.connect_to_wifi("CafeWiFi", "")
        assert ok is False
        assert self._modifies(calls) == []

    def test_colon_and_backslash_psk_round_trips_through_escaping(self, monkeypatch):
        """litclock-dev#599 review F1 (hardware-verified on NM 1.42.4): -g
        terse-escapes the VALUE even for one field, and the editor's `set`
        takes the raw value literally. Without unescaping the three psk
        reads, a good PSK containing ':' or a backslash is (a) never matched
        for restore when the ATTEMPT contains one, and (b) written back in
        escaped form — corrupted to a third value."""
        tricky_good = "pa:ss\\word99"
        calls, profiles = self._patch(
            monkeypatch,
            profiles={"u-good": {"ssid": "HomeWiFi", "psk": tricky_good}},
        )
        ok, _err = wifi_provision.connect_to_wifi("HomeWiFi", "also:wro\\ng1")
        assert ok is False
        assert profiles["u-good"]["psk"] == tricky_good, "raw value must round-trip unmangled"
        assert self._modifies(calls) == [["connection", "edit", "uuid", "u-good"]]

    def test_control_char_snapshot_psk_skips_restore_loudly(self, monkeypatch, caplog):
        """A snapshot value with a newline would execute as editor commands
        under sudo; the restore must refuse it with an ERROR, never feed it
        to the editor (litclock-dev#599 review F3)."""
        calls, profiles = self._patch(
            monkeypatch,
            profiles={"u-good": {"ssid": "HomeWiFi", "psk": "bad\npsk99"}},
        )
        with caplog.at_level("ERROR"):
            ok, _err = wifi_provision.connect_to_wifi("HomeWiFi", "WRONGWRONG")
        assert ok is False
        assert self._modifies(calls) == []
        assert "cannot carry safely" in caplog.text
        assert "bad" + chr(10) + "psk99" not in caplog.text, "the secret must never reach logs"

    def test_colon_in_ssid_still_matches_and_restores(self, monkeypatch):
        """litclock-dev#616 review F8: nmcli terse-mode escapes ':' in the ssid field
        (``My\\:Net``). The single-listing parse must un-escape it or a
        colon-SSID profile silently misses the snapshot and never restores."""
        calls, profiles = self._patch(
            monkeypatch,
            profiles={"u-good": {"ssid": "My:Net", "psk": self.GOOD}},
        )
        ok, _err = wifi_provision.connect_to_wifi("My:Net", "WRONGWRONG")
        assert ok is False
        assert profiles["u-good"]["psk"] == self.GOOD
        assert self._modifies(calls) == [["connection", "edit", "uuid", "u-good"]]

    def test_snapshot_uses_one_listing_not_per_profile_queries(self, monkeypatch):
        """litclock-dev#616 review F1: the snapshot must be O(1) nmcli listings + one
        secret read per match, NOT a per-profile ssid query (the N+1 that
        could serialize many 10s timeouts before the connect started)."""
        calls, _profiles = self._patch(
            monkeypatch,
            profiles={
                "u-a": {"ssid": "A", "psk": "pa"},
                "u-b": {"ssid": "B", "psk": "pb"},
                "u-c": {"ssid": "HomeWiFi", "psk": self.GOOD},
            },
        )
        wifi_provision.connect_to_wifi("HomeWiFi", "WRONGWRONG")
        # Exactly one UUID,TYPE listing. The ssid comes from per-WIFI-profile
        # unprivileged reads (the litclock-dev#616 one-listing dotted field is rc=2 on
        # real nmcli — the fake now rejects it too), and the SUDO SECRET
        # reads stay per-MATCH: that is the litclock-dev#616-F1 contract that matters.
        assert calls.count(self.SNAPSHOT_LISTING) == 1
        ssid_reads = [c for c in calls if c[:3] == ["-t", "-g", "802-11-wireless.ssid"]]
        assert {c[-1] for c in ssid_reads} == {"u-a", "u-b", "u-c"}, "one ssid read per WIFI profile"
        # Secret reads only for the match (u-c): snapshot + restore pre-write
        # re-read + post-edit verify read-back (litclock-dev#599) = u-c only.
        psk_reads = [c for c in calls if c[:3] == ["-s", "-g", "802-11-wireless-security.psk"]]
        assert psk_reads and all(c[-1] == "u-c" for c in psk_reads), psk_reads

    def test_snapshot_listing_failure_logs_a_warning(self, monkeypatch, caplog):
        """litclock-dev#616 review F6: a snapshot that can't list must WARN, not fail
        silently — a silent miss leaves the litclock-dev#613 brick state with no trace."""
        self._patch(
            monkeypatch,
            profiles={"u-good": {"ssid": "HomeWiFi", "psk": self.GOOD}},
            listing_rc=1,
        )
        with caplog.at_level("WARNING"):
            wifi_provision.connect_to_wifi("HomeWiFi", "WRONGWRONG")
        assert "will NOT be auto-restored" in caplog.text

    def test_snapshot_secret_read_failure_logs_a_warning(self, monkeypatch, caplog):
        """litclock-dev#616 review F6: an unreadable secret at snapshot time also WARNs
        (UUID only, never the value)."""
        self._patch(
            monkeypatch,
            profiles={"u-good": {"ssid": "HomeWiFi", "psk": self.GOOD}},
            psk_read_rc=5,
        )
        with caplog.at_level("WARNING"):
            wifi_provision.connect_to_wifi("HomeWiFi", "WRONGWRONG")
        assert "Could not read the saved password" in caplog.text
        assert self.GOOD not in caplog.text


class TestNoSecretInArgv:
    """The litclock-dev#599 end-to-end invariant, enforced at the subprocess
    boundary: across a full failed connect INCLUDING the litclock-dev#613 PSK restore,
    neither the attempted password nor the restored good PSK may appear in any
    argv — sudo's command audit writes argv to persistent journald, which is
    exactly how both leaked before this redesign (hardware-verified, including
    a real home PSK)."""

    GOOD = "good-secret-psk"
    ATTEMPTED = "attempted-secret"

    def test_connect_and_restore_argv_never_carry_secrets(self, monkeypatch):
        argvs = []
        stored = {"psk": self.GOOD}

        def fake_subprocess_run(cmd, capture_output=True, text=True, timeout=None, input=None):
            argvs.append(list(cmd))
            args = list(cmd[cmd.index("nmcli") + 1 :])
            if args == ["-t", "-f", "UUID", "connection", "show"]:
                return subprocess.CompletedProcess(cmd, 0, "u-1\n", "")
            if args == ["-t", "-f", "UUID,TYPE", "connection", "show"]:
                return subprocess.CompletedProcess(cmd, 0, "u-1:802-11-wireless\n", "")
            if args[:3] == ["-t", "-g", "802-11-wireless.ssid"]:
                return subprocess.CompletedProcess(cmd, 0, "HomeWiFi\n", "")
            if args[:3] == ["-s", "-g", "802-11-wireless-security.psk"]:
                escaped = stored["psk"].replace("\\", "\\\\").replace(":", "\\:")
                return subprocess.CompletedProcess(cmd, 0, escaped + "\n", "")
            if "connect" in args:
                assert "--ask" in args and input == self.ATTEMPTED + "\n", "secret must arrive via --ask stdin"
                stored["psk"] = self.ATTEMPTED  # NM persists the attempt (litclock-dev#613)
                return subprocess.CompletedProcess(cmd, 1, "", "Secrets were required, but not provided.")
            if args[:3] == ["connection", "edit", "uuid"]:
                if input and f"set 802-11-wireless-security.psk {self.GOOD}" in input:
                    stored["psk"] = self.GOOD
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(wifi_provision.subprocess, "run", fake_subprocess_run)
        monkeypatch.setattr(wifi_provision, "is_wifi_connected", lambda: False)
        monkeypatch.setattr(wifi_provision, "get_wifi_ssid", lambda: "")
        monkeypatch.setattr(wifi_provision, "_clear_wifi_watchdog_counter", lambda: None)
        monkeypatch.setattr(wifi_provision.time, "sleep", lambda _s: None)

        ok, err = wifi_provision.connect_to_wifi("HomeWiFi", self.ATTEMPTED)
        assert ok is False and "Incorrect WiFi password" in err
        for secret in (self.GOOD, self.ATTEMPTED):
            leaked = [v for v in argvs if any(secret in token for token in v)]
            assert not leaked, f"secret reached argv: {leaked}"
        # Anti-vacuity: the mechanisms this invariant protects actually ran.
        assert any("--ask" in v for v in argvs), "connect must use --ask"
        assert stored["psk"] == self.GOOD, "the restore must have completed via the editor stdin"

    def test_open_network_connect_omits_the_passwd_file(self, monkeypatch):
        argvs = []

        def fake_subprocess_run(cmd, capture_output=True, text=True, timeout=None, input=None):
            argvs.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(wifi_provision.subprocess, "run", fake_subprocess_run)
        monkeypatch.setattr(wifi_provision, "is_wifi_connected", lambda: True)
        monkeypatch.setattr(wifi_provision, "get_wifi_ssid", lambda: "CafeWiFi")
        monkeypatch.setattr(wifi_provision, "_clear_wifi_watchdog_counter", lambda: None)
        monkeypatch.setattr(wifi_provision.time, "sleep", lambda _s: None)

        ok, _err = wifi_provision.connect_to_wifi("CafeWiFi", "")
        assert ok is True
        connect = next(v for v in argvs if "connect" in v)
        assert "--ask" not in connect and "password" not in connect

    def test_control_char_password_rejected_before_any_nmcli(self, monkeypatch):
        # A newline in the password would be consumed as an extra --ask prompt
        # line (and can't exist in a real WPA passphrase); it must fail fast
        # with the honest class and ZERO subprocess activity.
        def must_not_run(*a, **k):
            raise AssertionError("no subprocess may run for a control-char password")

        monkeypatch.setattr(wifi_provision.subprocess, "run", must_not_run)
        ok, err = wifi_provision.connect_to_wifi("HomeWiFi", "pass\nword1")
        assert ok is False
        assert err.failure_class == wifi_provision.WIFI_FAIL_BAD_PASSWORD
        assert "control characters" in err


class TestUnescapeTerse:
    """nmcli -t escaping round-trip (litclock-dev#613/litclock-dev#616 F8)."""

    def test_plain_value_untouched(self):
        assert wifi_provision._unescape_terse("HomeWiFi") == "HomeWiFi"

    def test_escaped_colon(self):
        assert wifi_provision._unescape_terse("My\\:Net") == "My:Net"

    def test_escaped_backslash(self):
        assert wifi_provision._unescape_terse("A\\\\B") == "A\\B"

    def test_trailing_lone_backslash_kept(self):
        assert wifi_provision._unescape_terse("odd\\") == "odd\\"


class TestRunNmcliSecretOutput:
    """litclock-dev#613/litclock-dev#616: a `-s -g …psk` read's STDOUT is the secret;
    argv redaction does not cover output, so the TimeoutExpired branch must
    not journal the partial pre-kill bytes for a secret_output command."""

    def test_timeout_redacts_secret_output(self, monkeypatch, caplog):
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="nmcli", timeout=10, output="hunter2-the-psk", stderr="")

        monkeypatch.setattr(subprocess, "run", boom)
        with caplog.at_level("ERROR"):
            wifi_provision._run_nmcli(
                ["-s", "-g", "802-11-wireless-security.psk", "connection", "show", "uuid", "u"],
                check=False,
                sudo=True,
                timeout=10,
                secret_output=True,
            )
        assert "hunter2-the-psk" not in caplog.text
        assert "bytes redacted" in caplog.text

    def test_timeout_keeps_output_for_non_secret_commands(self, monkeypatch, caplog):
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="nmcli", timeout=30, output="Connecting (prepare)", stderr="")

        monkeypatch.setattr(subprocess, "run", boom)
        with caplog.at_level("ERROR"):
            wifi_provision._run_nmcli(["device", "wifi", "connect", "Home"], check=False, sudo=True, timeout=30)
        assert "Connecting (prepare)" in caplog.text


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

    def test_returned_result_argv_is_redacted_on_the_common_path(self, monkeypatch):
        """The non-timeout result comes straight from subprocess.run, which
        stores the RAW argv in .args — without the overwrite in _run_nmcli
        the safe-to-log property would hold only for the rare timeout
        sentinel, not the shape every caller actually receives (/review on
        litclock-dev#606)."""
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr=""),
        )
        result = wifi_provision._run_nmcli(["device", "wifi", "connect", "Home", "password", "s3cret"])
        assert "s3cret" not in " ".join(result.args)
        assert "***" in result.args

    def test_the_debug_line_itself_is_redacted(self, monkeypatch, caplog):
        """The unit above is only useful if _run_nmcli actually calls it."""
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))
        with caplog.at_level("DEBUG"):
            wifi_provision._run_nmcli(["device", "wifi", "connect", "Home", "password", "s3cret"], sudo=True)
        assert "s3cret" not in caplog.text
        assert "***" in caplog.text


class TestProfileUuids:
    """litclock-dev#595 — the snapshot half of snapshot-and-diff."""

    def test_uuids_are_parsed_and_stripped(self, monkeypatch):
        monkeypatch.setattr(
            wifi_provision,
            "_run_nmcli",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout="u-aaa\nu-bbb\n\n", stderr=""),
        )
        assert wifi_provision._profile_uuids() == {"u-aaa", "u-bbb"}

    def test_listing_failure_returns_none_not_empty(self, monkeypatch):
        """None (unknown) and set() (known-empty) drive different cleanup
        decisions — a failed listing must not masquerade as an empty one."""
        monkeypatch.setattr(
            wifi_provision,
            "_run_nmcli",
            lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
        )
        assert wifi_provision._profile_uuids() is None

    def test_created_diff_is_none_when_either_side_is_unknown(self, monkeypatch):
        monkeypatch.setattr(
            wifi_provision,
            "_run_nmcli",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout="u-a\n", stderr=""),
        )
        assert wifi_provision._created_profile_uuids(None) is None
        monkeypatch.setattr(
            wifi_provision,
            "_run_nmcli",
            lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
        )
        assert wifi_provision._created_profile_uuids({"u-a"}) is None


class TestRunNmcliTimeout:
    """The real subprocess.TimeoutExpired handler in _run_nmcli. Every
    connect_to_wifi test mocks _run_nmcli wholesale, so without these the
    actual except-path has zero coverage — a regression there (exception
    escaping, wrong sentinel, lost stderr contract) would crash on hardware
    while the whole suite stayed green (litclock-dev#598)."""

    @staticmethod
    def _raise_timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    def test_synthetic_result_argv_is_redacted(self, monkeypatch):
        """litclock-dev#605 item 9: the sentinel CompletedProcess embeds the
        argv in .args — with the raw command that includes the PSK. Nothing
        logs it today; make it safe to log by construction."""
        monkeypatch.setattr(subprocess, "run", self._raise_timeout)
        result = wifi_provision._run_nmcli(
            ["device", "wifi", "connect", "Home", "password", "s3cret"], check=False, timeout=30
        )
        assert "s3cret" not in " ".join(result.args)
        assert "***" in result.args

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


class TestWifiFailureClasses:
    """litclock-dev#603 — connect_to_wifi's error carries its failure class
    so the retry surfaces (e-ink variant, banner advice) branch on data
    instead of re-deriving the cause from user-facing copy (the antipattern
    litclock-dev#605 item 11 flags)."""

    def _connect(self, monkeypatch, returncode, stderr, connected=False):
        def fake_run(args, check=True, sudo=False, timeout=None, **_kw):
            return SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)

        monkeypatch.setattr(wifi_provision, "_run_nmcli", fake_run)
        monkeypatch.setattr(wifi_provision, "is_wifi_connected", lambda: connected)
        monkeypatch.setattr(wifi_provision, "get_wifi_ssid", lambda: "Net")
        monkeypatch.setattr(wifi_provision, "_clear_wifi_watchdog_counter", lambda: None)
        monkeypatch.setattr(wifi_provision.time, "sleep", lambda _s: None)
        return wifi_provision.connect_to_wifi("Net", "pw", hidden=True)

    @pytest.mark.parametrize(
        ("rc", "stderr", "expected"),
        [
            (wifi_provision.NMCLI_TIMEOUT_RC, "nmcli timed out", wifi_provision.WIFI_FAIL_TIMEOUT),
            (
                1,
                "Error: Connection activation failed: Secrets were required, but not provided.",
                wifi_provision.WIFI_FAIL_BAD_PASSWORD,
            ),
            (1, "Error: No network with SSID 'Net' found.", wifi_provision.WIFI_FAIL_NOT_FOUND),
            (
                1,
                "Error: Connection activation failed: (53) The device is busy.",
                wifi_provision.WIFI_FAIL_OTHER,
            ),
        ],
        ids=["timeout", "bad-password", "not-found", "other"],
    )
    def test_failure_class_per_branch(self, monkeypatch, rc, stderr, expected):
        ok, err = self._connect(monkeypatch, rc, stderr)
        assert ok is False
        assert err.failure_class == expected

    def test_no_ip_failure_class(self, monkeypatch):
        # rc 0 (nmcli succeeded) but the IP wait never sees a connection.
        ok, err = self._connect(monkeypatch, 0, "", connected=False)
        assert ok is False
        assert err.failure_class == wifi_provision.WIFI_FAIL_NO_IP

    def test_scanned_not_found_shares_the_not_found_class(self, monkeypatch):
        def fake_run(args, check=True, sudo=False, timeout=None, **_kw):
            return SimpleNamespace(returncode=1, stdout="", stderr="Error: No network with SSID 'Home' found.")

        monkeypatch.setattr(wifi_provision, "_run_nmcli", fake_run)
        monkeypatch.setattr(wifi_provision, "is_wifi_connected", lambda: False)
        monkeypatch.setattr(wifi_provision.time, "sleep", lambda _s: None)
        ok, err = wifi_provision.connect_to_wifi("Home", "pw", hidden=False)
        assert ok is False
        assert err.failure_class == wifi_provision.WIFI_FAIL_NOT_FOUND

    def test_error_is_still_a_plain_string_to_every_consumer(self, monkeypatch):
        """Every existing consumer treats the error as a str — html.escape
        at the banner, equality asserts, substring checks, journald
        interpolation. The class must ride along invisibly."""
        import html as _html

        ok, err = self._connect(
            monkeypatch, 1, "Error: Connection activation failed: Secrets were required, but not provided."
        )
        assert ok is False
        assert err == "Incorrect WiFi password"
        assert isinstance(err, str)
        assert _html.escape(err) == "Incorrect WiFi password"
        assert f"{err}" == "Incorrect WiFi password"
