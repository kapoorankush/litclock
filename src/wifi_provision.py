#!/usr/bin/env python3
"""
WiFi Provisioning for LitClock

Uses NetworkManager (nmcli) to create a temporary hotspot for WiFi setup.
User connects phone to hotspot, opens setup page, selects WiFi.

Replaces the Balena wifi-connect binary which is incompatible with
NetworkManager on Raspberry Pi OS Bookworm.

Usage:
    python wifi_provision.py hotspot [--ssid NAME]
    python wifi_provision.py scan
    python wifi_provision.py connect --ssid NAME --password PASSWORD
    python wifi_provision.py teardown
    python wifi_provision.py status
"""

import argparse
import json
import logging
import os
import secrets
import string
import subprocess
import sys
import time

from log import setup_logging

# Configure logging
setup_logging()

DEFAULT_SSID = "LitClock-Setup"
HOTSPOT_CON_NAME = "litclock-hotspot"
HOTSPOT_GATEWAY = "10.42.0.1"
SETUP_SERVER_PORT = 8080
DNSMASQ_CAPTIVE_CONF = "/etc/NetworkManager/dnsmasq-shared.d/captive-portal.conf"


_NMCLI_SECRET_KEYS = frozenset({"password", "wifi-sec.psk", "802-11-wireless-security.psk"})

# Synthetic returncode _run_nmcli substitutes when its timeout= expires,
# mirroring coreutils timeout(1)'s convention. Safe sentinel: nmcli's own
# documented exit codes stay in single digits, and sudo relays the child's
# status, so no genuine nmcli failure can collide with it.
NMCLI_TIMEOUT_RC = 124

# Bounds on the two nmcli calls in scan_wifi_networks(). Both scan entry points
# (the /scan-wifi handler and the GET / page build) hold _SCAN_CACHE_LOCK across
# the whole scan to serialize radio access, so an UNBOUNDED nmcli here would let
# a single wedged NetworkManager (D-Bus stall, brcmfmac hang) hold the lock
# forever and freeze every setup-page render and rescan (litclock-dev#615). A
# timeout synthesizes rc=124 and the scan degrades to an empty result the
# callers already handle. Generous on a Pi Zero 2 W's weak 2.4GHz radio: a busy
# band can make a rescan genuinely slow, so these only trip on a true wedge.
SCAN_RESCAN_TIMEOUT = 10
SCAN_LIST_TIMEOUT = 15

# Failure classes carried on connect_to_wifi's error returns
# (litclock-dev#603). The retry surfaces — the e-ink retry variant and the
# web banner's advice line — branch on these instead of re-deriving the
# cause by sniffing user-facing copy (the antipattern litclock-dev#605 item 11 flags).
WIFI_FAIL_TIMEOUT = "timeout"
WIFI_FAIL_BAD_PASSWORD = "bad_password"
WIFI_FAIL_NOT_FOUND = "not_found"
WIFI_FAIL_NO_IP = "no_ip"
WIFI_FAIL_OTHER = "other"


class WifiFailure(str):
    """connect_to_wifi's error copy, carrying its failure class.

    A str subclass so every existing consumer — html.escape at the web
    banner, equality and substring asserts, journald interpolation — keeps
    working byte-for-byte unchanged; ``failure_class`` rides along for the
    retry surfaces (litclock-dev#603). Immutable like its base."""

    __slots__ = ("failure_class",)

    def __new__(cls, message, failure_class):
        obj = super().__new__(cls, message)
        obj.failure_class = failure_class
        return obj

    def __getnewargs__(self):
        # str's default returns one arg; our __new__ demands two, so without
        # this, copy.copy / deepcopy / pickle all raise TypeError (/review on
        # litclock-dev#610 — e.g. a future QueueHandler pickling log args).
        return (str(self), self.failure_class)


def _redact_nmcli(cmd):
    """Replace the token after any secret-bearing nmcli key with ***.

    The connect argv carries the recipient's home WiFi PSK in the clear.
    journald ships Storage=persistent on the flashed image and the support
    bundle collects it, so anyone who raises the log level once to debug a
    first-boot failure would write that password to disk permanently, where
    it then travels off the device (/review, litclock-dev#580).
    """
    out = []
    redact_next = False
    for token in cmd:
        out.append("***" if redact_next else token)
        redact_next = token in _NMCLI_SECRET_KEYS
    return out


def _run_nmcli(args, check=True, sudo=False, timeout=None, secret_output=False, input_text=None):
    """Run an nmcli command and return the result.

    ``input_text`` feeds the command's stdin (litclock-dev#599: the
    interactive-editor path carries a PSK via stdin so it never transits
    argv, where sudo's audit line would persist it to journald).

    ``timeout`` (seconds, None = unbounded) exists for the connect path
    (litclock-dev#598): on an exists-but-hidden SSID this NM version can
    block ~107s in activation — measured on hardware — while the hotspot
    is already torn down and the user's phone dangles with no feedback.
    On expiry the caller gets a synthetic result (returncode 124, stderr
    "nmcli timed out") instead of an exception, so every existing
    error-handling path keeps working.

    ``secret_output`` marks a command whose STDOUT is itself a secret
    (``-s -g …psk`` reads — litclock-dev#613). argv redaction does not cover
    OUTPUT, and the TimeoutExpired branch below otherwise logs the partial
    pre-kill output at ERROR: a wedged secret read could then land the PSK in
    persistent journald (litclock-dev#616 review). When set, the partial output is
    replaced with a byte count."""
    cmd = (["sudo"] if sudo else []) + ["nmcli"] + args
    logging.debug(f"Running: {' '.join(_redact_nmcli(cmd))}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input_text)
    except subprocess.TimeoutExpired as exc:
        # Keep whatever nmcli wrote before the kill — it's the only record of
        # what NM was doing (slow DHCP vs hidden 5GHz vs wedged daemon) and
        # this ERROR line is the only journald trace at the default log level.
        # TimeoutExpired output is bytes even under text=True (CPython quirk),
        # but don't bet on that across versions — normalize either way.
        parts = [p for p in (exc.stdout, exc.stderr) if p]
        partial = " ".join(p.decode("utf-8", "replace") if isinstance(p, bytes) else p for p in parts).strip()
        if secret_output and partial:
            # The output is the secret itself — never journal it (litclock-dev#613/litclock-dev#616).
            partial = f"<{len(partial)} bytes redacted>"
        logging.error(
            f"nmcli timed out after {timeout}s: {' '.join(_redact_nmcli(cmd))}"
            + (f" — output before kill: {partial}" if partial else "")
        )
        # Redacted argv in the synthetic result: .args embeds the command line,
        # and the connect argv carries the PSK. No current caller logs it, but
        # the whole redaction machinery exists because a future debug line
        # writing an argv persists the password to journald (litclock-dev#580) — make the
        # sentinel object safe to log by construction.
        return subprocess.CompletedProcess(
            _redact_nmcli(cmd), returncode=NMCLI_TIMEOUT_RC, stdout="", stderr="nmcli timed out"
        )
    # Same invariant on the common path: subprocess.run stored the RAW argv
    # in result.args, so without this line the "safe to log" property would
    # hold for exactly one of the two result shapes — the rare one. Nothing
    # re-executes or compares .args (verified across src/), so overwriting
    # it loses nothing.
    result.args = _redact_nmcli(cmd)
    if check and result.returncode != 0:
        logging.error(f"nmcli failed: {result.stderr.strip()}")
    return result


def _generate_password(length=8):
    """Generate a random password for the hotspot."""
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


# Hotspot credential bounds (litclock-dev#626). 802.11-2020 §9.4.2.2: an SSID
# element is 0-32 octets (zero-length is the wildcard SSID, meaningless for an
# AP we host). WPA2-PSK passphrases (802.11i Annex H, enforced by both
# wpa_supplicant and hostapd) are 8-63 printable-ASCII characters.
#
# The SSID rule is deliberately TIGHTER than 802.11 (which allows any 32
# octets): printable ASCII only. This AP is ours, and the name must render
# faithfully on every surface that shows it — the e-ink splash (Aileron has no
# CJK/emoji glyphs; a Unicode name paints as tofu boxes while the AP
# broadcasts the real bytes), the /etc/issue box (bash pads printf fields by
# BYTES while ${#var} counts characters, so any multibyte name misaligns the
# border), and WIFI: QR parsers on phones (several trim or re-encode
# non-ASCII). ASCII is the one alphabet where chars == bytes == terminal
# columns, which is what makes the four-surfaces-agree guarantee of this
# validator actually hold (/review on litclock-dev#629: three independent
# passes converged on this).
HOTSPOT_SSID_MAX_CHARS = 32
HOTSPOT_PASSWORD_MIN_CHARS = 8
HOTSPOT_PASSWORD_MAX_CHARS = 63


def _outside_printable_ascii(value):
    return any(not (0x20 <= ord(ch) <= 0x7E) for ch in value)


def validate_hotspot_credentials(ssid, password):
    """Validate an SSID/password pair BEFORE the AP exists (litclock-dev#626).

    create_hotspot() is the single chokepoint through which the live AP, the
    e-ink QR, the /etc/issue console banner, and the splash instruction steps
    all receive their credentials, so rejecting out-of-spec values here is what
    guarantees those four surfaces can never disagree about what the network
    is. The renderer's own sanitize/clamp (litclock-dev#589) stays as
    belt-and-suspenders, but after this check it can no longer be the only
    guard. Unreachable on the shipped path — first-boot.sh always uses the
    defaults — so this only ever fires on a hand-typed --ssid/--password.

    Returns None when the pair is valid, else a log-safe error string (never
    echoes the password; never echoes raw control characters).
    """
    if not ssid or not ssid.strip():
        # All-spaces is as bad as empty: an invisible name on every surface,
        # and phone QR parsers whitespace-trim the payload, so the phone would
        # seek a DIFFERENT SSID than the AP broadcasts (/review).
        return "SSID is empty or blank"
    if _outside_printable_ascii(ssid):
        return "SSID must be printable ASCII (0x20-0x7E) so every surface renders the same name"
    if len(ssid) > HOTSPOT_SSID_MAX_CHARS:
        return f"SSID is {len(ssid)} characters; 802.11 caps SSIDs at {HOTSPOT_SSID_MAX_CHARS} octets"
    if not password:
        # Guard None/empty explicitly — this reads as public API and must
        # return an error string, never raise, for a future direct caller.
        return "password is missing"
    if not (HOTSPOT_PASSWORD_MIN_CHARS <= len(password) <= HOTSPOT_PASSWORD_MAX_CHARS):
        return (
            f"password must be {HOTSPOT_PASSWORD_MIN_CHARS}-{HOTSPOT_PASSWORD_MAX_CHARS} "
            f"characters for WPA2-PSK (got {len(password)})"
        )
    if _outside_printable_ascii(password):
        return "password contains characters outside printable ASCII; WPA2-PSK passphrases are printable ASCII"
    return None


_READY_STATES = {"disconnected", "connected", "connecting"}


def ensure_wifi_ready(timeout=45):
    """Ensure WiFi hardware is unblocked, managed, and recognized as a Wi-Fi device.

    Returns True only when wlan0 reaches a state NetworkManager can act on
    (disconnected / connected / connecting). The states `unmanaged` and
    `unavailable` are rejected — they indicate the brcmfmac SDIO chip hasn't
    been claimed by NM yet, and proceeding to nmcli hotspot in those states
    fails with "Device 'wlan0' is not a Wi-Fi device".
    """
    # Unblock WiFi radio
    subprocess.run(["sudo", "rfkill", "unblock", "wifi"], capture_output=True)

    # Enable WiFi radio in NetworkManager
    _run_nmcli(["radio", "wifi", "on"], check=False, sudo=True)

    # Ensure wlan0 is managed
    _run_nmcli(["device", "set", "wlan0", "managed", "yes"], check=False, sudo=True)

    # Wait for wlan0 to reach a usable state — Pi Zero 2W brcmfmac can take
    # 20+ seconds on a cold boot, longer if the chip is recovering from a
    # stuck state left behind by a rapid reboot.
    last_state = "missing"
    for _ in range(timeout):
        result = _run_nmcli(["-t", "-f", "DEVICE,TYPE,STATE", "device"], check=False)
        for line in result.stdout.strip().split("\n"):
            parts = line.split(":")
            if len(parts) >= 3 and parts[0] == "wlan0":
                last_state = parts[2]
                if parts[1] == "wifi" and last_state in _READY_STATES:
                    logging.info(f"wlan0 is ready (state={last_state})")
                    return True
                break
        time.sleep(1)

    logging.error(f"wlan0 did not become ready within {timeout}s (last state: {last_state})")
    return False


def is_wifi_connected():
    """Check if WiFi is currently connected to a network (not hotspot)."""
    result = _run_nmcli(["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"], check=False)
    for line in result.stdout.strip().split("\n"):
        parts = line.split(":")
        if len(parts) >= 4 and parts[0] == "wlan0" and parts[1] == "wifi":
            if parts[2] == "connected" and parts[3] != HOTSPOT_CON_NAME:
                return True
    return False


def get_wifi_ssid():
    """Get the currently connected WiFi SSID."""
    result = _run_nmcli(["-t", "-f", "active,ssid", "dev", "wifi"], check=False)
    for line in result.stdout.strip().split("\n"):
        if line.startswith("yes:"):
            return line.split(":", 1)[1]
    return None


def _setup_captive_portal():
    """Configure captive portal so phones auto-open the setup page.

    Two pieces:
    1. Tell NM's dnsmasq to resolve ALL domains to the hotspot IP.
       NM starts dnsmasq in "shared" mode when the hotspot activates,
       so the config must exist before the hotspot is created.
    2. nftables redirect: captive portal probes hit port 80, but the
       setup server listens on 8080. Redirect 80→8080.
    """
    # Create dnsmasq config directory if needed, then write the wildcard rule.
    #
    # address=/#/IP — wildcard A answer for every name, points at the gateway.
    # no-resolv    — THE fix for the iOS captive-portal HTTPS-RR probe (#483,
    #                supersedes the local=/#/ theory of #178). iOS 17+ sends an
    #                HTTPS RR (type 65) query for `captive.apple.com` BEFORE the A
    #                query (RFC 9460 HTTPS-upgrade discovery). dnsmasq does NOT
    #                answer type 65 from `address=/#/` (that's an A record only),
    #                and — critically — `local=/#/` does NOT stop it forwarding
    #                the type-65 query upstream (verified on dnsmasq 2.90). NM's
    #                shared-mode dnsmasq reads /etc/resolv.conf and inherits a
    #                public upstream (e.g. 8.8.8.8), so it forwards the type-65
    #                query there — but the isolated hotspot has NO route to that
    #                upstream, so the forward fails and dnsmasq returns
    #                `REFUSED (EDE: network error)`. iOS reads that REFUSED as
    #                hostile DNS and SILENTLY DEMOTES the captive-portal sheet
    #                (the exact failure #178 was chasing; local=/#/ only masked it
    #                on client Pis whose inherited upstream happened to be
    #                reachable and answered NODATA). `no-resolv` makes dnsmasq
    #                keep NO upstream at all, so it answers every non-A type
    #                authoritatively as NODATA — iOS falls through to the A query
    #                and pops the sheet. An isolated captive portal never needs an
    #                upstream, so dropping it has no downside. (local=/#/ kept as
    #                belt-and-suspenders.) Reproduced + fix-verified against a
    #                dnsmasq with an unreachable upstream, 2026-07-07.
    # local=/#/    — declare every name local (authoritative); kept alongside
    #                no-resolv though no-resolv is what actually fixes type 65.
    # log-queries — NM doesn't pass --log-queries to shared-mode dnsmasq, so
    #                we add it here for captive-portal debugging.
    # address=/mask*.icloud.com/ (no IP → NXDOMAIN) — iCloud Private Relay
    #                ingress hosts. litclock-dev#526 pcap (2026-07-16): on
    #                join, iOS 26.5.2 immediately tries Private Relay via
    #                QUIC to mask.icloud.com; the wildcard spoofed it to the
    #                gateway, which refused the connection — part of the
    #                spoof-then-refuse pattern that makes iOS 26 treat the
    #                hotspot as broken and suppress the captive sheet. Apple
    #                documents that networks where relay is unavailable
    #                should answer these names with NXDOMAIN so iOS falls
    #                back to direct connections cleanly. A more-specific
    #                address= always beats the /#/ wildcard in dnsmasq.
    subprocess.run(
        ["sudo", "mkdir", "-p", "/etc/NetworkManager/dnsmasq-shared.d"],
        capture_output=True,
    )
    dnsmasq_conf = (
        f"address=/#/{HOTSPOT_GATEWAY}\n"
        "address=/mask.icloud.com/\n"
        "address=/mask-h2.icloud.com/\n"
        "address=/mask-api.icloud.com/\n"
        "local=/#/\n"
        "no-resolv\n"
        "log-queries\n"
    )
    subprocess.run(
        ["sudo", "tee", DNSMASQ_CAPTIVE_CONF],
        input=dnsmasq_conf,
        capture_output=True,
        text=True,
    )
    logging.info("Captive portal DNS config written")

    # Redirect port 80 → setup server port 8080. Captive portal probes are
    # plain HTTP, so port 80 is all we need to ANSWER.
    #
    # Port 443 is intentionally NOT redirected (the plain HTTP server can't
    # speak TLS — a redirected ClientHello would read as garbage and iOS
    # demotes the sheet on hostile-looking responses, issue #178). It is
    # now DROPPED rather than left to the kernel's RST. litclock-dev#526
    # pcap (2026-07-16, iOS 26.5.2): on join the phone's Apple services
    # (iCloud gateway, location, Private Relay QUIC, APNs 5223, cached
    # Apple IPs) all resolved to the spoofing gateway and got active
    # refusals — 14 RSTs on 443 plus ICMP unreachables — before/while the
    # captive check ran. iOS 26 reads that spoof-then-refuse pattern as a
    # broken network ("network connection was lost") and suppresses the
    # CNA sheet even though the port-80 probe was answered perfectly.
    # Commercial walled gardens filter silently; matching that (drop, no
    # RST, no ICMP) is the point of the filter chain below. The setup
    # flow itself never needs 443/5223 — probes are plain HTTP on 80.
    #
    # Raspberry Pi OS Bookworm uses nftables (no iptables binary). Create a
    # named table so we can cleanly delete it on teardown (both chains go
    # with the table).
    nft_rules = (
        "table ip litclock_captive {\n"
        "  chain prerouting {\n"
        "    type nat hook prerouting priority dstnat; policy accept;\n"
        "    tcp dport 80 redirect to :8080\n"
        "  }\n"
        "  chain walled_garden {\n"
        "    type filter hook prerouting priority filter; policy accept;\n"
        "    tcp dport { 443, 5223 } drop\n"
        "    udp dport 443 drop\n"
        "  }\n"
        "}\n"
    )
    result = subprocess.run(
        ["sudo", "/usr/sbin/nft", "-f", "-"],
        input=nft_rules,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logging.error(f"nft redirect rules failed: {result.stderr.strip()}")
    else:
        logging.info("nft port 80→8080 redirect added")


def _teardown_captive_portal():
    """Remove captive portal DNS config and nftables redirect rules.

    #343 made this teardown load-bearing: the captive table holds an
    ``ip daddr … tcp dport 80 redirect to :8080`` rule, and control_server now
    binds port 80. If the table survives teardown (the first-boot success path
    does NOT reboot — nftables is not cleared for us), inbound PWA traffic on
    :80 would be NAT'd to the now-dead setup_server port 8080, leaving
    control_server healthy-but-unreachable. Pre-#343 a surviving redirect was
    harmless (control_server was on 8443). So we now VERIFY the table is gone,
    retry once, and log loudly if it persists (rather than ignore the delete
    result). A reboot would also clear it, but we must not depend on one.
    """
    subprocess.run(["sudo", "rm", "-f", DNSMASQ_CAPTIVE_CONF], capture_output=True)

    def _table_present() -> bool:
        # `nft list table` exits non-zero when the table is absent.
        return (
            subprocess.run(
                ["sudo", "/usr/sbin/nft", "list", "table", "ip", "litclock_captive"],
                capture_output=True,
            ).returncode
            == 0
        )

    for _ in range(2):
        # Delete removes all the table's chains/rules; non-zero here just means
        # "already absent", which the verify below confirms.
        subprocess.run(
            ["sudo", "/usr/sbin/nft", "delete", "table", "ip", "litclock_captive"],
            capture_output=True,
        )
        if not _table_present():
            logging.info("Captive portal config removed")
            return

    logging.error(
        "Captive portal nft table 'litclock_captive' survived teardown — its "
        "port-80 redirect to 8080 would make the control PWA unreachable on "
        "port 80 (#343). Flush manually: sudo nft delete table ip litclock_captive"
    )


def create_hotspot(ssid=DEFAULT_SSID, password=None):
    """
    Create a WiFi hotspot using nmcli.

    Returns:
        dict with 'ssid', 'password', 'ip' on success, None on failure
    """
    if password is None:
        password = _generate_password()

    # Reject out-of-spec credentials before ANY side effect (no teardown, no
    # captive-portal config, no nmcli) — a rejected pair must leave the system
    # exactly as it was (litclock-dev#626).
    error = validate_hotspot_credentials(ssid, password)
    if error:
        logging.error(f"Refusing to create hotspot: {error}")
        return None

    logging.info(f"Creating hotspot: {ssid}")

    if not ensure_wifi_ready():
        logging.error("wlan0 not ready — refusing to attempt hotspot creation")
        return None

    # Remove any existing hotspot connection profile
    teardown_hotspot()

    # Set up captive portal DNS + iptables before hotspot starts
    # (NM reads dnsmasq-shared.d when activating the shared connection)
    _setup_captive_portal()

    # Create hotspot — sudo needed: no active polkit session when run from systemd
    result = _run_nmcli(
        [
            "device",
            "wifi",
            "hotspot",
            "ifname",
            "wlan0",
            "con-name",
            HOTSPOT_CON_NAME,
            "ssid",
            ssid,
            "password",
            password,
        ],
        check=False,
        sudo=True,
    )

    if result.returncode != 0:
        logging.error(f"Failed to create hotspot: {result.stderr.strip()}")
        _teardown_captive_portal()
        return None

    logging.info(f"Hotspot '{ssid}' created successfully")

    return {
        "ssid": ssid,
        "password": password,
        "ip": HOTSPOT_GATEWAY,
    }


def teardown_hotspot():
    """Deactivate and remove the hotspot connection profile."""
    # Deactivate
    _run_nmcli(["connection", "down", HOTSPOT_CON_NAME], check=False, sudo=True)
    # Delete the profile
    _run_nmcli(["connection", "delete", HOTSPOT_CON_NAME], check=False, sudo=True)
    # Clean up captive portal config
    _teardown_captive_portal()
    logging.info("Hotspot torn down")


def scan_wifi_networks():
    """
    Scan for available WiFi networks.

    Returns:
        list of dicts with 'ssid', 'signal', 'security', 'in_use'
    """
    # Trigger a rescan — sudo for consistency with other nmcli calls from systemd.
    # Bounded so a wedged NM can't hold _SCAN_CACHE_LOCK forever (litclock-dev#615).
    _run_nmcli(["device", "wifi", "rescan"], check=False, sudo=True, timeout=SCAN_RESCAN_TIMEOUT)
    time.sleep(2)

    # Get results
    result = _run_nmcli(
        ["-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list"],
        check=False,
        sudo=True,
        timeout=SCAN_LIST_TIMEOUT,
    )

    if result.returncode != 0:
        logging.error("WiFi scan failed")
        return []

    networks = []
    seen_ssids = set()

    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        # nmcli -t uses : as separator; fields may contain escaped colons
        parts = line.split(":")
        if len(parts) < 4:
            continue

        in_use = parts[0].strip() == "*"
        ssid = parts[1].strip()
        try:
            signal = int(parts[2].strip())
        except ValueError:
            signal = 0
        security = ":".join(parts[3:]).strip()

        # Skip empty SSIDs (hidden networks) and duplicates
        if not ssid or ssid in seen_ssids:
            continue

        seen_ssids.add(ssid)
        networks.append(
            {
                "ssid": ssid,
                "signal": signal,
                "security": security,
                "in_use": in_use,
            }
        )

    # Sort by signal strength (strongest first)
    networks.sort(key=lambda n: n["signal"], reverse=True)

    logging.info(f"Found {len(networks)} WiFi networks")
    return networks


def _profile_uuids():
    """Set of saved NM connection profile UUIDs, or None if unreadable.

    UUIDs, not NAMEs (litclock-dev#609 review, Codex + adversarial converged): NM
    uniquifies a colliding name ("MyNet 1"), an operator can rename a
    profile over SSH so its name no longer matches its SSID, and duplicate
    names collapse in a set — all three defeat name membership. UUIDs are
    stable, unique per profile, and escape-free in terse output.

    Bounded (~0.1s measured; 10s cap for a wedged daemon) and unprivileged —
    listing needs no polkit session."""
    result = _run_nmcli(["-t", "-f", "UUID", "connection", "show"], check=False, timeout=10)
    if result.returncode != 0:
        return None
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _created_profile_uuids(pre_uuids):
    """UUIDs of profiles created since the pre-connect snapshot.

    None when the diff cannot be computed (either listing failed) — callers
    decide per failure class how loud to be, because "unknown" and "known
    empty" drive different cleanup decisions."""
    if pre_uuids is None:
        return None
    post = _profile_uuids()
    if post is None:
        return None
    return post - pre_uuids


def _delete_created_profiles(created, ssid):
    """Delete the profile(s) THIS attempt created (litclock-dev#595).

    nmcli's own failure exits (wrong password via WRONG_KEY ~24s, SSID not
    found) leave the just-created profile saved with ``autoconnect=yes`` and
    the unverified PSK. On a fielded device that profile autoconnect-loops
    failed WPA auth whenever the real network drops — and the single radio
    spends every one of those retries NOT rejoining the good network.

    The UUID set-diff means a profile that pre-existed the attempt — under
    ANY name — is structurally untouchable here, so a previously-working
    saved network can never be deleted (the hazard the owner declined to
    risk in litclock-dev#600). Bounded deletes: an unbounded delete against a wedged
    NetworkManager would recreate the very hang the connect timeout exists
    to prevent, on the cleanup path.
    """
    ok = True
    for uuid in sorted(created):
        cleanup = _run_nmcli(["connection", "delete", "uuid", uuid], check=False, sudo=True, timeout=10)
        if cleanup.returncode != 0:
            logging.error(
                f"Could not delete profile {uuid} created by the failed "
                f"attempt on '{ssid}' (rc={cleanup.returncode}): it may keep "
                "autoconnecting with an unverified password "
                "(litclock-dev#595). See journalctl -u NetworkManager."
            )
            ok = False
    return ok


def _unescape_terse(field):
    """Undo nmcli ``-t`` terse-mode escaping (``\\:`` -> ``:``, ``\\\\`` ->
    ``\\``). nmcli escapes the field separator and backslash in VALUE fields;
    an SSID legitimately containing a colon arrives as ``My\\:Net`` and would
    otherwise never match the target (litclock-dev#616 review F8 — silent snapshot miss
    on colon SSIDs)."""
    out = []
    escaped = False
    for ch in field:
        if escaped:
            out.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        else:
            out.append(ch)
    if escaped:  # trailing lone backslash — keep it literally
        out.append("\\")
    return "".join(out)


def _snapshot_ssid_psks(ssid):
    """Map of pre-existing wifi profile UUID -> stored PSK for profiles whose
    802-11-wireless.ssid equals the connect target (litclock-dev#613).

    Hardware-verified 2026-08-09 (NM 1.42.4): ``nmcli device wifi connect``
    REUSES a pre-existing profile for the SSID and writes the attempted
    password into it even when the join fails — a previously-working saved
    network is left armed (autoconnect=yes) with a password that can never
    authenticate. The litclock-dev#609 UUID set-diff is structurally blind to this (no
    profile is *created*), so the failure paths restore from this snapshot
    instead.

    UUID-keyed for the same reasons as _profile_uuids (rename/uniquify).
    Shape: ONE UUID,TYPE listing + one unprivileged ssid read per WIFI
    profile + one sudo secret read per SSID MATCH. (litclock-dev#616 tried to fold the
    ssid into the listing — `-f UUID,TYPE,802-11-wireless.ssid` — but the
    list form rejects setting properties with rc=2 on real nmcli 1.42.4,
    which silently disabled the whole restore; hardware-caught in the litclock-dev#630
    retest. The litclock-dev#616-F1 N+1 concern was specifically per-profile SUDO SECRET
    reads; the ssid reads here are unprivileged and wifi-only.)

    The PSK values live in this dict IN MEMORY ONLY. The listing/ssid read is
    unprivileged; only the secret read is sudo. A listing failure or an
    unreadable secret drops that profile and logs a WARNING (never the value)
    — restore then degrades to the pre-#613 no-op, but LOUDLY, because a
    silent miss leaves the fielded device in exactly the litclock-dev#613 brick state with
    no journal trace (litclock-dev#616 review F6)."""
    # The LIST form of `connection show` accepts only profile COLUMNS
    # (NAME, UUID, TYPE, ...) — setting properties like 802-11-wireless.ssid
    # are rejected with rc=2 "invalid field". The litclock-dev#616 one-listing shape
    # (`-f UUID,TYPE,802-11-wireless.ssid`) therefore ALWAYS failed on real
    # nmcli 1.42.4, silently disabling the whole litclock-dev#613 restore; the unit-test
    # fake accepted the invalid field (hardware-caught during the litclock-dev#630
    # retest, 2026-08-10). The ssid must come from a per-profile query.
    # That reintroduces a per-profile read, but NOT the litclock-dev#616-F1 hazard: F1
    # was about per-profile SUDO SECRET reads serializing 10s timeouts —
    # these ssid reads are unprivileged, wifi-type-only, and the secret
    # reads below remain per-MATCH.
    listing = _run_nmcli(["-t", "-f", "UUID,TYPE", "connection", "show"], check=False, timeout=10)
    if listing.returncode != 0:
        logging.warning(
            f"Could not list connections to snapshot the saved password for "
            f"'{ssid}' (rc={listing.returncode}) — a failed attempt that "
            "overwrites a reused profile will NOT be auto-restored "
            "(litclock-dev#613)."
        )
        return {}
    snapshot = {}
    for line in listing.stdout.splitlines():
        parts = line.split(":", 1)
        if len(parts) < 2:
            continue
        uuid, ctype = parts
        if ctype != "802-11-wireless":
            continue
        ssid_q = _run_nmcli(
            ["-t", "-g", "802-11-wireless.ssid", "connection", "show", "uuid", uuid],
            check=False,
            timeout=10,
        )
        if ssid_q.returncode != 0:
            logging.warning(
                f"Could not read the ssid of wifi profile {uuid} while "
                f"snapshotting for '{ssid}' (rc={ssid_q.returncode}) — that "
                "profile will NOT be auto-restored (litclock-dev#613)."
            )
            continue
        if _unescape_terse(ssid_q.stdout.rstrip("\n")) != ssid:
            continue
        psk_q = _run_nmcli(
            ["-s", "-g", "802-11-wireless-security.psk", "connection", "show", "uuid", uuid],
            check=False,
            sudo=True,
            timeout=10,
            secret_output=True,
        )
        if psk_q.returncode != 0:
            logging.warning(
                f"Could not read the saved password on profile {uuid} ('{ssid}') "
                f"to snapshot it (rc={psk_q.returncode}) — a failed attempt that "
                "overwrites it will NOT be auto-restored (litclock-dev#613)."
            )
            continue
        # ``-g`` output is terse-ESCAPED even for a single field — verified on
        # NM 1.42.4: a stored ``pa:ss\word`` reads back as ``pa\:ss\\word``
        # (litclock-dev#599 review F1). Un-escape so every comparison and the
        # editor replay below operate on the RAW value; without this, a PSK
        # containing ``:`` or ``\`` silently defeats the whole litclock-dev#613 restore.
        snapshot[uuid] = _unescape_terse(psk_q.stdout.rstrip("\n"))
    return snapshot


def _restore_ssid_psks(snapshot, ssid, attempted):
    """Restore each snapshotted profile's PSK when the failed attempt is what
    overwrote it (litclock-dev#613). Called only on the paths where nmcli
    reported the connect itself failed — NOT on the rc=0 "connected but no
    IP" path, where association proves the submitted password was correct and
    NM's stored value is the right final state.

    Restore condition is ``current == attempted and attempted != good``, not
    merely ``current != good`` (litclock-dev#616 review, Codex): only undo the exact write
    THIS attempt made. A value that is neither the good snapshot nor this
    attempt's password was changed by something else (SSH, a concurrent NM
    edit) and must not be clobbered; an open network (good == attempted == "")
    is left alone. Per profile: re-read the current PSK (bounded, sudo —
    secrets read needs root), then modify only when the condition holds. A
    failed restore is logged loudly (the profile is left unable to rejoin);
    the secret never reaches logs OR argv — the write rides the editor's
    stdin (litclock-dev#599) and the reads use secret_output."""
    if not snapshot:
        return
    for uuid, good in snapshot.items():
        if attempted == good:
            continue  # nothing this attempt could have corrupted (e.g. open network)
        current = _run_nmcli(
            ["-s", "-g", "802-11-wireless-security.psk", "connection", "show", "uuid", uuid],
            check=False,
            sudo=True,
            timeout=10,
            secret_output=True,
        )
        # Skip unless the stored PSK is exactly what this attempt wrote: an
        # unreadable profile (deleted meanwhile), an unchanged one, or one a
        # third party changed are all left untouched. The read is un-escaped
        # (-g terse-escapes ``:`` and ``\`` — see _snapshot_ssid_psks) so the
        # comparison is raw-vs-raw.
        if current.returncode != 0 or _unescape_terse(current.stdout.rstrip("\n")) != attempted:
            continue
        # The good PSK goes back via `connection edit` with the commands fed
        # on STDIN — never `connection modify … wifi-sec.psk <value>`, whose
        # argv sudo audits to persistent journald; that leaked the RESTORED
        # (working!) password, the worst credential to leak (litclock-dev#599,
        # widened by litclock-dev#616). The editor's `set` takes the rest of the line
        # LITERALLY (verified on NM 1.42.4: colons and backslashes round-trip
        # unmodified) — but it cannot carry a value with control characters
        # (each newline would execute as another editor command under sudo)
        # or leading/trailing whitespace (the editor strips it). No real WPA
        # passphrase contains either; a snapshot value that does is corrupt,
        # so refuse loudly rather than write a mangled third value.
        if good and (any(ord(ch) < 0x20 or ch == "\x7f" for ch in good) or good != good.strip()):
            logging.error(
                f"Snapshot password for profile {uuid} ('{ssid}') contains "
                "characters the profile editor cannot carry safely — restore "
                "skipped; re-provision this network to fix it (litclock-dev#599)."
            )
            continue
        # An empty good value (open-network profile that somehow got a psk
        # written) clears the property instead of `set` with no value, which
        # would drop the editor into an interactive prompt and hang until
        # the timeout.
        if good:
            commands = f"set 802-11-wireless-security.psk {good}\nsave persistent\nquit\n"
        else:
            commands = "remove 802-11-wireless-security.psk\nsave persistent\nquit\n"
        fix = _run_nmcli(
            ["connection", "edit", "uuid", uuid],
            check=False,
            sudo=True,
            timeout=15,
            input_text=commands,
            secret_output=True,
        )
        # The editor's exit code is a weak signal (it exits 0 after `quit`
        # even when `save` printed an error), so the authority on success is
        # a read-back: the stored PSK must now equal the snapshot.
        verify = _run_nmcli(
            ["-s", "-g", "802-11-wireless-security.psk", "connection", "show", "uuid", uuid],
            check=False,
            sudo=True,
            timeout=10,
            secret_output=True,
        )
        restored = verify.returncode == 0 and _unescape_terse(verify.stdout.rstrip("\n")) == good
        if restored:
            logging.info(
                f"Restored the saved password on profile {uuid} ('{ssid}') — the "
                "failed attempt had overwritten it (litclock-dev#613)."
            )
        else:
            logging.error(
                f"Could not restore the saved password on profile {uuid} "
                f"('{ssid}', edit rc={fix.returncode}, verified=False): the "
                "profile is left with the failed attempt's password and cannot "
                "rejoin until re-provisioned (litclock-dev#613). "
                "See journalctl -u NetworkManager."
            )


def connect_to_wifi(ssid, password, hidden=False, connect_timeout=30):
    """
    Connect to a WiFi network.

    Args:
        hidden: the SSID was typed by hand rather than picked from a scan
            (litclock-dev#554). Adds `hidden yes`, which sets
            802-11-wireless.hidden on the profile so the client actively
            probes for the SSID instead of waiting for a beacon that a hidden
            AP never broadcasts. Off by default: it makes the device announce
            the SSID in probe requests, which is a fair trade to reach a
            network you otherwise cannot, but not something to impose on the
            ordinary visible-network path.
        connect_timeout: seconds before the nmcli connect is abandoned and
            the half-created profile deleted (litclock-dev#598). None =
            unbounded — the CLI's manual/SSH path passes it via --timeout 0,
            where "return the hotspot to the user" doesn't apply and NO
            profile cleanup runs on any failure path (litclock-dev#595
            keeps the setup-flow semantics off the recovery path).

    Returns:
        (success: bool, error_message: str or None)
    """
    logging.info(f"Connecting to WiFi: {ssid}" + (" (hidden)" if hidden else ""))

    # A control character in the password would be consumed as extra --ask
    # prompt lines below (and can never be part of a real WPA passphrase —
    # 802.11 Annex H is printable ASCII); reject it before ANY nmcli
    # activity with the honest failure class (litclock-dev#599).
    if password and any(ord(ch) < 0x20 or ch == "\x7f" for ch in password):
        return False, WifiFailure(
            "WiFi passwords can't contain line breaks or control characters — re-type the password and try again.",
            WIFI_FAIL_BAD_PASSWORD,
        )

    # litclock-dev#595 snapshot-and-diff: capture the profile UUID set BEFORE
    # the connect so every failure path below can tell what THIS attempt
    # created. UUIDs, not names — see _profile_uuids.
    pre_uuids = _profile_uuids()

    # litclock-dev#613: also snapshot the stored PSK of any PRE-EXISTING
    # profile for this SSID — nmcli reuses such a profile and persists the
    # attempted password into it even on failure (hardware-verified). Gated
    # like the cleanup below: the manual --timeout 0 SSH-recovery path must
    # never touch profiles (litclock-dev#600 decision d), so it skips the snapshot too.
    pre_psks = _snapshot_ssid_psks(ssid) if connect_timeout is not None else {}

    # Use nmcli to connect — sudo needed when run from systemd (no polkit
    # session). The PSK travels via `--ask` + stdin, NEVER as a `password
    # <value>` argv token: sudo's command-audit line writes the full argv to
    # persistent journald, so the old form logged every attempted password —
    # and after litclock-dev#616, restored good ones — to disk on every provisioning
    # (litclock-dev#599, hardware-verified on the QA Pi including a real home
    # PSK). NOT --passwd-file: that option does not exist on nmcli 1.42.4
    # (verified on-device — "Option '--passwd-file' is unknown"; it is a
    # `connection up` sub-argument only). With --ask, nmcli prompts for
    # whatever secret NM requests (psk for WPA, wep-key for WEP — no
    # secret-type hardcoding) and reads it from stdin when stdin is a pipe;
    # verified live on NM 1.42.4: the prompt consumed a piped password and
    # activation proceeded. On a wrong password NM re-asks, stdin is at EOF,
    # and nmcli fails with the same "Secrets were required" error string the
    # classification below already handles. An empty password (open network —
    # the setup form says "leave blank") omits --ask entirely. Control
    # characters in the password were already rejected at the top of this
    # function.
    args = [
        "device",
        "wifi",
        "connect",
        ssid,
        "ifname",
        "wlan0",
    ]
    if hidden:
        args += ["hidden", "yes"]
    if password:
        args = ["--ask"] + args

    # litclock-dev#598: bound the connect. Hardware-measured: an
    # exists-but-hidden SSID blocks ~107s inside NM activation before
    # failing — the whole time with the hotspot torn down (single radio)
    # and the user's phone off the setup network with zero feedback. 30s
    # is generous for a genuine slow hidden association (the successful
    # hidden join measured ~3s) and returns the hotspot to the user while
    # they still have context.
    result = _run_nmcli(
        args,
        check=False,
        sudo=True,
        timeout=connect_timeout,
        input_text=(password + "\n") if password else None,
    )

    if result.returncode != 0:
        error = result.stderr.strip()
        if result.returncode == NMCLI_TIMEOUT_RC:
            # The timeout SIGKILLed the sudo wrapper — sudo can't relay
            # SIGKILL, so nmcli survives briefly as an orphan, and either way
            # NetworkManager's activation job keeps running: nmcli is only a
            # D-Bus client (litclock-dev#600 review).
            #
            # Rescue check first: BECAUSE the activation keeps running, a
            # slow-but-genuine join (mesh/band-steering DHCP legitimately
            # takes 30-45s; NM's own ipv4.dhcp-timeout default is 45s) can
            # land moments after the bound. Deleting then would tear down a
            # working connection — and every retry would collide the same
            # way, making that network permanently unprovisionable. Give the
            # in-flight activation a short grace window; landing here counts
            # as success. 15s + the 30s bound covers NM's 45s DHCP tail.
            for _ in range(15):
                if is_wifi_connected() and get_wifi_ssid() == ssid:
                    logging.info(f"Join to '{ssid}' completed after the {connect_timeout}s bound — rescued")
                    _clear_wifi_watchdog_counter()
                    return True, None
                time.sleep(1)
            # No rescue: abort the in-flight activation FIRST, independent of
            # profile identity (litclock-dev#609 review — the sharpest hole): nmcli was
            # only a D-Bus client, NM keeps trying, and litclock-dev#600's delete-as-abort
            # only worked when the activated profile happened to be NAMED
            # exactly the SSID (nmcli reuses profiles by SSID match, and an
            # operator can rename one over SSH). `device disconnect` stops
            # whatever wlan0 is activating so the hotspot restore isn't
            # racing it for the single radio. create_hotspot recovers the
            # manual-disconnected state this leaves (ensure_wifi_ready
            # accepts `disconnected`; the hotspot activation is explicit).
            abort = _run_nmcli(["device", "disconnect", "wlan0"], check=False, sudo=True, timeout=10)
            if abort.returncode != 0:
                logging.error(
                    f"Could not disconnect wlan0 after the bound expired on "
                    f"'{ssid}' (rc={abort.returncode}): the activation may "
                    "still be racing the hotspot restore. "
                    "See journalctl -u NetworkManager."
                )
            # Then drop what this attempt created (the armed-profile half).
            created = _created_profile_uuids(pre_uuids)
            if created is None:
                # Listing broken — fall back to litclock-dev#600's shipped name-targeted
                # delete. On THIS path an armed leftover is near-certain (the
                # attempt got far enough to activate) and the abort above
                # already handled the racing-activation half; the residual
                # risk (a same-named pre-existing profile deleted while
                # listing is broken but delete works) is the documented
                # double-fault tradeoff. Explicit "id" selector — bare
                # `delete <ssid>` makes nmcli spec-guess.
                cleanup = _run_nmcli(["connection", "delete", "id", ssid], check=False, sudo=True, timeout=10)
                if cleanup.returncode != 0:
                    logging.error(
                        f"Could not delete half-created profile '{ssid}' "
                        f"(rc={cleanup.returncode}): it may keep autoconnecting "
                        "with an unverified password (litclock-dev#595). "
                        "See journalctl -u NetworkManager."
                    )
            else:
                _delete_created_profiles(created, ssid)
            # A reused pre-existing profile survives the delete above by
            # design — un-write the failed password from it (litclock-dev#613).
            _restore_ssid_psks(pre_psks, ssid, password)
            return False, WifiFailure(
                f"Couldn't reach '{ssid}' — the network didn't answer in time. "
                "Check that it's a 2.4GHz network and in range, then try again.",
                WIFI_FAIL_TIMEOUT,
            )
        # nmcli exited on its own — no activation left running, but the
        # profile this attempt created survives with autoconnect=yes and the
        # unverified password (litclock-dev#595 — hardware repro: a wrong PSK
        # fails via WRONG_KEY in ~24s and the armed profile remains; on a
        # fielded device it then autoconnect-loops failed auth whenever the
        # real network drops). Clean up what this attempt created before
        # returning. The manual --timeout 0 path opts out entirely (litclock-dev#600
        # decision d — SSH-recovery semantics must never delete profiles).
        # When the UUID diff is unavailable, SKIP the cleanup rather than
        # guess by name (litclock-dev#609 Codex): unlike the timeout branch there is no
        # activation to abort here, so a name-targeted delete has no upside
        # to weigh against deleting a good pre-existing profile.
        if connect_timeout is not None:
            created = _created_profile_uuids(pre_uuids)
            if created is None:
                logging.warning(
                    f"Could not determine whether the failed attempt on "
                    f"'{ssid}' left a profile behind (connection listing "
                    "unavailable) — leaving profiles untouched "
                    "(litclock-dev#595)."
                )
            else:
                _delete_created_profiles(created, ssid)
            # The wrong-password path on a REUSED profile creates nothing —
            # it corrupts the pre-existing profile's stored PSK instead
            # (hardware: 6s rc=4 "Secrets were required", stored PSK ==
            # the failed attempt's). Restore what this attempt overwrote
            # (litclock-dev#613).
            _restore_ssid_psks(pre_psks, ssid, password)

        # Parse common error messages for user-friendly messages
        if "Secrets were required" in error or "password" in error.lower():
            return False, WifiFailure("Incorrect WiFi password", WIFI_FAIL_BAD_PASSWORD)
        if "No network with SSID" in error:
            # litclock-dev#598 (hardware-established): for a TYPED name this
            # is usually NOT a typo — a hidden 5GHz-only network is invisible
            # to the Zero 2 W's 2.4GHz radio and fails exactly here, and a
            # hidden 2.4GHz one can too until it enters the scan cache. Lead
            # with the likely causes; keep the spelling hint last.
            if hidden:
                return False, WifiFailure(
                    f"Couldn't find '{ssid}'. If it's a hidden network, make sure "
                    "it's 2.4GHz and in range, then try again. Also double-check "
                    "the exact spelling — network names are case-sensitive.",
                    WIFI_FAIL_NOT_FOUND,
                )
            return False, WifiFailure(f"Network '{ssid}' not found", WIFI_FAIL_NOT_FOUND)
        return False, WifiFailure(f"Connection failed: {error}", WIFI_FAIL_OTHER)

    # Verify connection - wait for IP address
    for _ in range(15):
        if is_wifi_connected():
            connected_ssid = get_wifi_ssid()
            logging.info(f"Connected to: {connected_ssid}")
            _clear_wifi_watchdog_counter()
            return True, None
        time.sleep(1)

    return False, WifiFailure("Connected but could not obtain IP address", WIFI_FAIL_NO_IP)


def _clear_wifi_watchdog_counter():
    """Clear the wifi-watchdog reboot counter on successful (re-)provisioning.

    M5 OV1 (#245): wifi-watchdog clears its own counter at the START of every
    tick when the ping target responds, but ticks fire every 5 minutes —
    leaving up to a 5-min window after a successful re-provisioning where a
    stale count==3 could falsely re-trigger the firstboot fallback. Clearing
    here closes that window immediately on the user-facing connect success.

    Best-effort: missing file or permission error is silently ignored — the
    next watchdog tick after this clears it via the success path anyway.
    """
    counter_file = os.environ.get(
        "LITCLOCK_WIFI_WATCHDOG_COUNTER",
        "/var/lib/litclock/wifi-watchdog-reboots",
    )
    try:
        if os.path.exists(counter_file):
            os.remove(counter_file)
            logging.info(f"Cleared wifi-watchdog counter: {counter_file}")
    except OSError as exc:
        logging.debug(f"Could not clear wifi-watchdog counter: {exc}")


def get_hotspot_status():
    """Check if hotspot is currently active."""
    result = _run_nmcli(
        ["-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"],
        check=False,
    )
    for line in result.stdout.strip().split("\n"):
        if HOTSPOT_CON_NAME in line:
            return True
    return False


def show_hotspot_info(ssid, password, ip, display=True):
    """Show hotspot information on e-ink display with QR code."""
    if not display:
        return

    try:
        from eink_display import display_hotspot_info

        display_hotspot_info(ssid, password, ip)
        logging.info("Displayed hotspot info on e-ink")
    except ImportError:
        logging.warning("eink_display module not available")
    except Exception as e:
        logging.warning(f"Could not update display: {e}")


def main():
    parser = argparse.ArgumentParser(description="WiFi Provisioning for LitClock")
    subparsers = parser.add_subparsers(dest="command", help="Command")

    # hotspot command
    hotspot_parser = subparsers.add_parser("hotspot", help="Create WiFi hotspot")
    hotspot_parser.add_argument("--ssid", "-s", default=DEFAULT_SSID, help=f"Hotspot SSID (default: {DEFAULT_SSID})")
    hotspot_parser.add_argument("--password", "-p", help="Hotspot password (auto-generated if omitted)")

    # scan command
    subparsers.add_parser("scan", help="Scan for WiFi networks")

    # connect command
    connect_parser = subparsers.add_parser("connect", help="Connect to WiFi network")
    connect_parser.add_argument("--ssid", "-s", required=True, help="Network SSID")
    connect_parser.add_argument("--password", "-p", required=True, help="Network password")
    connect_parser.add_argument(
        "--hidden",
        action="store_true",
        help="Probe actively for the SSID instead of waiting for a beacon (litclock-dev#554)",
    )
    connect_parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="Seconds before the join is abandoned AND its half-created "
        "profile deleted (litclock-dev#598 setup-flow semantics). "
        "0 = wait indefinitely and never auto-delete — use for manual/SSH "
        "recovery on slow networks.",
    )

    # teardown command
    subparsers.add_parser("teardown", help="Tear down hotspot")

    # status command
    subparsers.add_parser("status", help="Check WiFi/hotspot status")

    args = parser.parse_args()

    if args.command == "hotspot":
        result = create_hotspot(ssid=args.ssid, password=args.password)
        if result:
            print(json.dumps(result))
            sys.exit(0)
        else:
            sys.exit(1)

    elif args.command == "scan":
        networks = scan_wifi_networks()
        print(json.dumps(networks, indent=2))

    elif args.command == "connect":
        success, error = connect_to_wifi(
            args.ssid,
            args.password,
            hidden=args.hidden,
            connect_timeout=args.timeout or None,  # 0 → unbounded
        )
        if success:
            print(f"Connected to {args.ssid}")
            sys.exit(0)
        else:
            print(f"Failed: {error}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "teardown":
        teardown_hotspot()

    elif args.command == "status":
        if is_wifi_connected():
            ssid = get_wifi_ssid()
            print(f"Connected to: {ssid}")
        elif get_hotspot_status():
            print("Hotspot active")
        else:
            print("Not connected")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
