#!/usr/bin/env python3
"""
Captive Portal for LitClock WiFi Setup

Provides two services that make the setup page auto-open on phones:

1. DNS server (port 53) — resolves ALL domains to the hotspot gateway IP.
   This triggers the phone's captive portal detection.

2. HTTP handler integration — responds to known captive portal probe URLs
   with a redirect to the setup page.

Captive portal detection URLs by platform:
- Apple iOS/macOS: http://captive.apple.com/hotspot-detect.html
                   http://www.apple.com/library/test/success.html
- Android:         http://connectivitycheck.gstatic.com/generate_204
                   http://clients3.google.com/generate_204
                   http://www.google.com/gen_204
- Windows:         http://www.msftconnecttest.com/connecttest.txt
                   http://www.msftncsi.com/ncsi.txt
- Firefox:         http://detectportal.firefox.com/canonical.html

When a phone gets a "wrong" response to these probes (anything other than
the expected content), it shows a captive portal popup with the response.
We redirect to the setup page.
"""

import logging
import socketserver
import struct
import threading

from log import setup_logging

setup_logging()

# User-facing hostname for the setup server. dnsmasq's wildcard
# (address=/#/10.42.0.1) resolves this to the hotspot gateway, and nftables
# redirects 80→8080, so `http://litclock.setup` lands on the real setup form
# without a port number. Uses a fake `.setup` TLD so Safari treats it as a
# URL instead of a search query. Imported by setup_server.py (redirect
# Location header, bridge href) and eink_display.py (printed instructions),
# so there's exactly one place to change it.
SETUP_HOSTNAME = "litclock.setup"

# Known captive portal detection paths
CAPTIVE_PORTAL_PATHS = {
    "/hotspot-detect.html",
    "/library/test/success.html",
    "/generate_204",
    "/gen_204",
    "/connecttest.txt",
    "/ncsi.txt",
    "/canonical.html",
    # Some phones just hit the root
    "/success.txt",
}

# Apple probe hosts specifically — these get the iOS CNA bridge HTML
# response. Other captive-portal hosts (Google, Microsoft, Firefox) get a
# 302 redirect instead because Android / Windows / Firefox detectors
# expect a redirect, not HTML. Kept as a separate set so the host-based
# fallback in setup_server can gate bridge vs redirect correctly.
APPLE_CAPTIVE_PORTAL_HOSTS = {
    "captive.apple.com",
    "www.apple.com",
}

# All known captive portal detection hosts. Used by setup_server.do_GET to
# decide whether an incoming request is a probe that should be intercepted
# regardless of path (some phones probe the root path on these hosts).
CAPTIVE_PORTAL_HOSTS = APPLE_CAPTIVE_PORTAL_HOSTS | {
    "connectivitycheck.gstatic.com",
    "clients3.google.com",
    "www.google.com",
    "www.msftconnecttest.com",
    "www.msftncsi.com",
    "detectportal.firefox.com",
}


def is_captive_portal_request(path, host=""):
    """Check if an HTTP request is a captive portal probe."""
    # Check by path
    if path in CAPTIVE_PORTAL_PATHS:
        return True
    # Check by host
    host_clean = host.split(":")[0].lower()
    if host_clean in CAPTIVE_PORTAL_HOSTS:
        return True
    return False


class CaptiveDNSHandler(socketserver.BaseRequestHandler):
    """
    DNS handler that resolves ALL queries to the hotspot IP.

    This is a minimal DNS responder — it parses just enough of the query
    to build a valid response pointing to the gateway IP. It doesn't
    implement full DNS; it only needs to fool captive portal detectors.
    """

    gateway_ip = "10.42.0.1"

    def handle(self):
        data = self.request[0]
        socket = self.request[1]

        try:
            # Parse the DNS query minimally
            # DNS header: ID(2) + Flags(2) + QDCOUNT(2) + ANCOUNT(2) + NSCOUNT(2) + ARCOUNT(2)
            if len(data) < 12:
                return

            transaction_id = data[:2]
            # Extract the question section (skip header)
            question = data[12:]

            # Find end of QNAME (null-terminated labels)
            qname_end = 0
            while qname_end < len(question) and question[qname_end] != 0:
                qname_end += question[qname_end] + 1
            qname_end += 1  # Include the null byte

            # QTYPE(2) + QCLASS(2) follow QNAME
            if qname_end + 4 > len(question):
                return

            qname = question[:qname_end]

            # Build response
            # Header: same ID, response flags, 1 question, 1 answer
            flags = struct.pack("!H", 0x8180)  # Response, no error
            counts = struct.pack("!HHHH", 1, 1, 0, 0)  # QD=1, AN=1, NS=0, AR=0
            header = transaction_id + flags + counts

            # Question section (echo back)
            question_section = qname + question[qname_end : qname_end + 4]

            # Answer section: pointer to qname, type A, class IN, TTL 60, rdlength 4, IP
            answer = (
                b"\xc0\x0c"  # Pointer to qname in question section
                + struct.pack("!HHI", 1, 1, 60)  # Type A, Class IN, TTL 60s
                + struct.pack("!H", 4)  # RDLENGTH = 4 bytes
                + bytes(int(x) for x in self.gateway_ip.split("."))
            )

            response = header + question_section + answer
            socket.sendto(response, self.client_address)

        except Exception as e:
            logging.debug(f"DNS handler error: {e}")


class CaptiveDNSServer(socketserver.ThreadingUDPServer):
    """Threaded UDP DNS server."""

    allow_reuse_address = True


def start_dns_server(gateway_ip="10.42.0.1", port=53):
    """
    Start the captive portal DNS server in a background thread.

    Args:
        gateway_ip: IP address to resolve all queries to
        port: DNS port (default 53, requires root)

    Returns:
        The server instance (call .shutdown() to stop)
    """
    CaptiveDNSHandler.gateway_ip = gateway_ip

    try:
        server = CaptiveDNSServer(("", port), CaptiveDNSHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logging.info(f"Captive portal DNS server started on port {port} (resolving to {gateway_ip})")
        return server
    except PermissionError:
        logging.error(f"Cannot bind to port {port} — requires root. Run with sudo.")
        return None
    except Exception as e:
        logging.error(f"Failed to start DNS server: {e}")
        return None


def main():
    """Run the DNS server standalone (for testing)."""
    import sys

    ip = sys.argv[1] if len(sys.argv) > 1 else "10.42.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 53

    print(f"Starting captive portal DNS on port {port}, resolving to {ip}")
    print("Press Ctrl+C to stop")

    server = start_dns_server(ip, port)
    if server is None:
        sys.exit(1)

    try:
        threading.Event().wait()  # Block forever
    except KeyboardInterrupt:
        server.shutdown()
        print("\nStopped")


if __name__ == "__main__":
    main()
