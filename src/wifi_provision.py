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


def _run_nmcli(args, check=True, sudo=False):
    """Run an nmcli command and return the result."""
    cmd = (["sudo"] if sudo else []) + ["nmcli"] + args
    logging.debug(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        logging.error(f"nmcli failed: {result.stderr.strip()}")
    return result


def _generate_password(length=8):
    """Generate a random password for the hotspot."""
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


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
    subprocess.run(
        ["sudo", "mkdir", "-p", "/etc/NetworkManager/dnsmasq-shared.d"],
        capture_output=True,
    )
    subprocess.run(
        ["sudo", "tee", DNSMASQ_CAPTIVE_CONF],
        input=f"address=/#/{HOTSPOT_GATEWAY}\nlocal=/#/\nno-resolv\nlog-queries\n",
        capture_output=True,
        text=True,
    )
    logging.info("Captive portal DNS config written")

    # Redirect port 80 → setup server port 8080. Captive portal probes are
    # plain HTTP, so port 80 is all we need.
    #
    # Port 443 is intentionally NOT redirected. The plain HTTP server on
    # 8080 cannot speak TLS, so a 443 redirect makes iOS's HTTPS captive
    # probe see a corrupt TLS handshake (the HTTP server reads the
    # ClientHello bytes as garbage and responds "HTTP/1.1 400 Bad request
    # version"). iOS 26.4.1 silently demotes the captive portal CNA popup
    # when its HTTPS probe gets a hostile-looking response. With no
    # listener on 443, the kernel sends a clean RST and iOS interprets
    # that as "this network blocks HTTPS, fall back to the HTTP probe
    # result" — which is exactly what we want. (issue #178)
    #
    # Raspberry Pi OS Bookworm uses nftables (no iptables binary). Create a
    # named table so we can cleanly delete it on teardown.
    nft_rules = (
        "table ip litclock_captive {\n"
        "  chain prerouting {\n"
        "    type nat hook prerouting priority dstnat; policy accept;\n"
        "    tcp dport 80 redirect to :8080\n"
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
    # Trigger a rescan — sudo for consistency with other nmcli calls from systemd
    _run_nmcli(["device", "wifi", "rescan"], check=False, sudo=True)
    time.sleep(2)

    # Get results
    result = _run_nmcli(
        ["-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list"],
        check=False,
        sudo=True,
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


def connect_to_wifi(ssid, password):
    """
    Connect to a WiFi network.

    Returns:
        (success: bool, error_message: str or None)
    """
    logging.info(f"Connecting to WiFi: {ssid}")

    # Use nmcli to connect — sudo needed when run from systemd (no polkit session)
    result = _run_nmcli(
        [
            "device",
            "wifi",
            "connect",
            ssid,
            "password",
            password,
            "ifname",
            "wlan0",
        ],
        check=False,
        sudo=True,
    )

    if result.returncode != 0:
        error = result.stderr.strip()
        # Parse common error messages for user-friendly messages
        if "Secrets were required" in error or "password" in error.lower():
            return False, "Incorrect WiFi password"
        if "No network with SSID" in error:
            return False, f"Network '{ssid}' not found"
        return False, f"Connection failed: {error}"

    # Verify connection - wait for IP address
    for _ in range(15):
        if is_wifi_connected():
            connected_ssid = get_wifi_ssid()
            logging.info(f"Connected to: {connected_ssid}")
            _clear_wifi_watchdog_counter()
            return True, None
        time.sleep(1)

    return False, "Connected but could not obtain IP address"


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
        success, error = connect_to_wifi(args.ssid, args.password)
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
