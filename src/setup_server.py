#!/usr/bin/env python3
"""
Web-based Setup Server for LitClock — WiFi-only provisioning (EPIC #383).

The hotspot form collects only WiFi credentials. After WiFi connects,
``_resolve_location_from_ip`` auto-populates ``WEATHER_LATITUDE``,
``WEATHER_LONGITUDE``, ``WEATHER_LOCATION_NAME``, ``WEATHER_UNITS``, and the
system timezone from a single ``ip-api.com`` call (with 3 retries on the
DNS-race-post-handshake failure mode). Users override anything wrong from
the Control PWA Settings tab after first-boot.

All env.sh writes route through ``config.atomic_update``; timezone is set
via ``timedatectl`` and runs BEFORE the env write so the worst failure
case is "tz set, env stale" instead of "wrong-time clock with working
weather" (see ``_update_env_location`` docstring for the A2-revised
ordering rationale).

Usage:
    # Normal mode (HTTPS, no WiFi selection — pre-connected ethernet/wpa):
    python setup_server.py <env_file> <signal_file>

    # Provisioning mode (HTTP on hotspot, with WiFi selection):
    python setup_server.py <env_file> <signal_file> --provisioning
"""

import html
import http.server
import json
import logging
import os
import signal
import socketserver
import ssl
import sys
import threading
import urllib.parse
from pathlib import Path

# Captive-portal primitives — see captive_portal.py for the rationale on
# why SETUP_HOSTNAME lives there (avoids a circular dep with eink_display).
# APPLE_CAPTIVE_PORTAL_HOSTS is the subset of CAPTIVE_PORTAL_HOSTS that
# gets the iOS-shaped bridge HTML; the rest get a 302 redirect.
from captive_portal import (
    APPLE_CAPTIVE_PORTAL_HOSTS,
    CAPTIVE_PORTAL_HOSTS,
    SETUP_HOSTNAME,
)

PORT = 8080
HTTPS_PORT = 8443
# Hotspot gateway IP shown as the absolute-fallback URL on the bridge page
# and e-ink display. The canonical copy of this constant lives in
# wifi_provision.py (which also writes the NetworkManager shared-connection
# config); this name is kept here purely for local readability in bridge
# text formatting.
HOTSPOT_GATEWAY_IP = "10.42.0.1"

ENV_FILE = None
SIGNAL_FILE = None
PROVISIONING_MODE = False
WIFI_CONNECT_ERROR = None  # Set by background thread on WiFi failure
WIFI_CONNECT_IN_FLIGHT = False  # Guard against concurrent connect attempts
HOTSPOT_SSID = None  # Original hotspot SSID, used to restore after failed WiFi
HOTSPOT_PASSWORD = None  # Original hotspot password, used to restore after failed WiFi
_WIFI_SCAN_CACHE = None  # Cached HTML <option> tags from last WiFi scan
_WIFI_SCAN_TIME = 0  # time.monotonic() when cache was populated
_WIFI_SCAN_TTL = 30  # seconds before cache expires

# Thread safety: the server runs in threaded mode so a stuck handler can't
# block new connections. These locks guard state mutated from both request
# threads and the background WiFi connect thread.
_WIFI_CONNECT_LOCK = threading.Lock()
_SCAN_CACHE_LOCK = threading.Lock()

# Registry of background daemon threads this module spawns (WiFi connect /
# teardown, location-resolve, delayed SIGTERM). Production never reaps these —
# they're daemons that die at process exit. It exists SOLELY so the #355
# test-isolation helper :func:`reset_state` can JOIN them between tests
# (#478): the old flag-only drain let a prior test's thread outlive its case
# and fire its (now next-test-monkeypatched) os.kill / retry function, polluting
# the next test's counters. Tracking + joining makes isolation deterministic.
_BG_THREADS: list[threading.Thread] = []
_BG_THREADS_LOCK = threading.Lock()
# Set by :func:`reset_state` (test-only) to WAKE a sleeping ``_delayed`` SIGTERM
# timer so it exits WITHOUT firing ``os.kill``. Without this, joining a
# ``_delayed`` thread would wait out its ``sleep(delay)`` and then fire a
# SIGTERM — but by test-teardown time ``monkeypatch`` has already reverted
# ``os.kill`` to the real one (conftest tears monkeypatch down BEFORE this
# fixture), so the SIGTERM would kill the test runner. Never set in production
# (``reset_state`` isn't a production path); ``Event.wait(delay)`` is otherwise
# identical to ``time.sleep(delay)``.
_BG_CANCEL = threading.Event()


def _spawn_bg(target, name: str) -> threading.Thread:
    """Start a daemon thread and register it so ``reset_state`` can join it.

    Prunes already-finished threads on each spawn so the registry stays bounded
    to the (tiny) set of live background threads.
    """
    t = threading.Thread(target=target, name=name, daemon=True)
    with _BG_THREADS_LOCK:
        _BG_THREADS[:] = [x for x in _BG_THREADS if x.is_alive()]
        _BG_THREADS.append(t)
        # start() INSIDE the lock (/review): reset_state snapshots the registry
        # under the same lock, so it can never observe an appended-but-not-yet-
        # started thread and hit ``RuntimeError: cannot join thread before it is
        # started``. start() returns as soon as the thread is launched, so the
        # child (e.g. _resolve_and_signal spawning _delayed via this helper)
        # only blocks on the lock momentarily — no deadlock.
        t.start()
    return t


# Per-connection read timeout. Captive portal probes from phones can stall
# mid-request (phone drops off the AP, enters a different network, etc.).
# Without this timeout, rfile.read() blocks forever and the handler thread
# leaks until the server is restarted.
HANDLER_TIMEOUT = 15

# Cap POST body size so a misbehaving (or hostile) client can't make the
# server allocate unbounded memory. Setup form posts are ~1KB.
MAX_POST_BODY = 32 * 1024


def reset_state(wait_for_inflight: float = 2.0) -> None:
    """Reset all module-level connect-flow state to defaults.

    Test-isolation helper (#355). The WiFi connect handler spawns a daemon
    thread that writes ``WIFI_CONNECT_ERROR`` and ``WIFI_CONNECT_IN_FLIGHT``
    asynchronously; without an explicit reset between tests, late writes
    from a prior test's thread leak into the next test's assertions and
    cause order-dependent flakes.

    Waits up to ``wait_for_inflight`` seconds for any in-flight WiFi connect
    thread to finish (so we don't race a still-running thread that's about
    to clobber the values we just reset), then zeroes the connect-flow
    globals and clears the WiFi scan cache. Idempotent and cheap when there
    is no in-flight work — a no-op in the common case.

    Not used by production code paths; safe to call from any test fixture.
    """
    global WIFI_CONNECT_ERROR, WIFI_CONNECT_IN_FLIGHT
    global _WIFI_SCAN_CACHE, _WIFI_SCAN_TIME

    import time

    deadline = time.monotonic() + max(0.0, wait_for_inflight)
    while WIFI_CONNECT_IN_FLIGHT and time.monotonic() < deadline:
        time.sleep(0.01)

    # #478 — the flag-drain above only covers threads that set
    # WIFI_CONNECT_IN_FLIGHT (the connect thread). _delayed (SIGTERM timer) and
    # _resolve_and_signal don't touch it, so join EVERY tracked background
    # thread against the SAME deadline. This is what actually stops a prior
    # test's thread from firing its (now next-test-monkeypatched) os.kill /
    # retry function into the next test's counters. _BG_CANCEL wakes any
    # sleeping _delayed timer so it exits WITHOUT firing a (now-real) SIGTERM;
    # a thread that still outlives the budget stays a daemon (dies at process
    # exit) — bounded, never hangs.
    _BG_CANCEL.set()
    try:
        with _BG_THREADS_LOCK:
            threads = list(_BG_THREADS)
        for t in threads:
            t.join(timeout=max(0.0, deadline - time.monotonic()))
        # Drop only the threads that actually finished; KEEP any that outran the
        # join budget so a later reset can retry cancelling/joining them
        # (/review). Clearing the whole list up-front would forget a still-live
        # daemon — it could then write the globals after we clear them below, and
        # no future reset could join it. is_alive() also drops any new threads
        # spawned during the join once they finish.
        with _BG_THREADS_LOCK:
            _BG_THREADS[:] = [t for t in _BG_THREADS if t.is_alive()]
    finally:
        _BG_CANCEL.clear()

    with _WIFI_CONNECT_LOCK:
        WIFI_CONNECT_ERROR = None
        WIFI_CONNECT_IN_FLIGHT = False
    with _SCAN_CACHE_LOCK:
        _WIFI_SCAN_CACHE = None
        _WIFI_SCAN_TIME = 0


def _schedule_self_terminate(delay: float = 0.0) -> None:
    """Send SIGTERM to the current process, optionally after ``delay`` seconds.

    When ``delay == 0``, signals synchronously from the calling thread:
    ``os.kill`` queues SIGTERM in the kernel; the default SIGTERM handler
    terminates the Python interpreter (there is NO graceful handler in
    setup_server — only ``except KeyboardInterrupt`` around serve_forever at
    lines 1612-1644). The 1s/2s sleep before this call gives the HTTP
    response time to flush; after termination, the firstboot.sh wrapper
    script polls ``/tmp/litclock-setup-done`` (written by
    ``signal_completion()``) to detect successful handoff.

    When ``delay > 0``, spawns a daemon thread that sleeps then signals —
    used when the HTTP response must flush before the process dies (e.g.,
    the no-WiFi-form-data branch where ``do_POST`` returns immediately
    after spawning the timer).

    Sequencing invariant (#364):

    Callers that interact with ``reset_state()``'s drain barrier (today
    only ``_connect_and_teardown``'s success path) MUST invoke this
    helper BEFORE clearing ``WIFI_CONNECT_IN_FLIGHT``. The drain barrier
    polls IN_FLIGHT and exits when it sees False; if IN_FLIGHT is
    cleared first, drain returns claiming the thread is quiescent while
    a SIGTERM is still pending in the kernel queue. That gives
    misleading state to test fixtures.

    Note that A2 only fixes the drain-barrier accuracy — it does NOT
    prevent SIGTERM from killing an unmocked-os.kill test runner.
    Test authors exercising any code path that invokes this helper
    MUST monkeypatch os.kill. See ``tests/test_wifi_retry_flow.py``
    for 8 canonical examples.
    """
    if delay > 0:

        def _delayed():
            # Cancellable wait: in production the event is never set, so this is
            # exactly ``time.sleep(delay)`` then SIGTERM. In tests, reset_state
            # sets _BG_CANCEL so we return WITHOUT killing the runner (#478).
            if _BG_CANCEL.wait(delay):
                return
            os.kill(os.getpid(), signal.SIGTERM)

        _spawn_bg(_delayed, name="setup-delayed-sigterm")
    else:
        os.kill(os.getpid(), signal.SIGTERM)


def _build_wifi_options(networks):
    """Convert a list of network dicts into HTML <option> tags."""
    options = []
    for net in networks:
        ssid = html.escape(net["ssid"])
        signal = net["signal"]
        if signal >= 70:
            bars = "Strong"
        elif signal >= 40:
            bars = "Medium"
        else:
            bars = "Weak"
        security = net.get("security", "")
        lock = " [Open]" if not security or security == "--" else ""
        options.append(f'<option value="{ssid}">{ssid} ({bars}{lock})</option>')
    return "\n                    ".join(options)


def _filter_own_hotspot(networks):
    """Drop the clock's own setup hotspot from a scanned network list.

    Connecting the Pi to its own AP is nonsensical, and the hotspot SSID can
    surface in a scan of its own radio. A non-technical tester picked
    "LitClock-Setup" off the dropdown thinking that was the network to choose
    (first-boot QA). No-op outside provisioning mode (HOTSPOT_SSID is None).

    Applied at BOTH scan entry points — the server-rendered dropdown
    (_wifi_network_options) and the /scan-wifi JSON the client uses to rebuild
    the dropdown on Refresh — because they share _WIFI_SCAN_CACHE; filtering
    only one path lets the hotspot leak back via the cache or the JSON.
    """
    if not HOTSPOT_SSID:
        return networks
    return [n for n in networks if n.get("ssid") != HOTSPOT_SSID]


def _wifi_network_options():
    """Generate <option> tags for scanned WiFi networks, with 30s caching."""
    global _WIFI_SCAN_CACHE, _WIFI_SCAN_TIME
    import time

    now = time.monotonic()
    if _WIFI_SCAN_CACHE is not None and (now - _WIFI_SCAN_TIME) < _WIFI_SCAN_TTL:
        return _WIFI_SCAN_CACHE

    try:
        from wifi_provision import scan_wifi_networks

        networks = scan_wifi_networks()
    except Exception:
        networks = []

    networks = _filter_own_hotspot(networks)

    if not networks:
        # Don't cache empty results — let the next call retry
        return '<option value="">No networks found - try refreshing</option>'

    result = _build_wifi_options(networks)
    _WIFI_SCAN_CACHE = result
    _WIFI_SCAN_TIME = now
    return result


# WISPr 1.0 smart-client block (litclock-dev#526). iOS's captive detector
# identifies itself as a WISPr client ("CaptiveNetworkSupport-<ver> wispr")
# and the WISPr protocol carries the portal handoff as an XML island inside
# an HTML comment: MessageType 100 / ResponseCode 0 = "captive network,
# login page is at LoginURL". Browsers ignore the comment entirely, so this
# is embedded in every captive response we serve to Apple clients. LoginURL
# uses the raw gateway IP, not SETUP_HOSTNAME — the point is to give iOS a
# target it can reach without trusting our wildcard DNS.
_WISPR_XML_COMMENT = (
    "<!--\n"
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<WISPAccessGatewayParam"
    ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
    ' xsi:noNamespaceSchemaLocation="http://www.wballiance.net/wispr_2_0.xsd">\n'
    "<Redirect>\n"
    "<MessageType>100</MessageType>\n"
    "<ResponseCode>0</ResponseCode>\n"
    "<AccessProcedure>1.0</AccessProcedure>\n"
    "<LocationName>LitClock Setup</LocationName>\n"
    f"<LoginURL>http://{HOTSPOT_GATEWAY_IP}/cna</LoginURL>\n"
    "</Redirect>\n"
    "</WISPAccessGatewayParam>\n"
    "-->"
)

# Precomputed at import time — everything in the bridge page is static
# except SETUP_HOSTNAME and HOTSPOT_GATEWAY_IP, both module-level constants
# known at import. iOS CNA can fire probes in quick bursts during first-boot
# and we'd otherwise rebuild this ~1 KB string per request for zero benefit.
#
# iOS CNA opens probe responses in a sandboxed WebView sheet with a short
# timeout and poor JS support. Returning the full setup page (large, inline
# CSS, scan-wifi JS) makes iOS silently give up and bury the captive portal
# in Control Center. This page is ~1 KB, has no JavaScript, and bridges the
# user into the real setup form via a single tap — which reliably pops the
# sheet. The small-text fallback also prints the raw gateway IP so the user
# has a recovery path if the hostname ever fails to resolve.
_CNA_BRIDGE_HTML = (
    "<!DOCTYPE html>" + _WISPR_XML_COMMENT + '<html lang="en"><head>'
    '<meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    "<title>LitClock Setup</title>"
    "<style>"
    "body{font-family:-apple-system,system-ui,sans-serif;"
    "margin:0;padding:32px 24px;background:#f8fafc;color:#1e293b;"
    "text-align:center;}"
    "h1{font-size:26px;margin:8px 0 12px;}"
    "p{font-size:16px;line-height:1.5;color:#475569;margin:0 0 24px;}"
    "a.btn{display:inline-block;background:#2563eb;color:#fff;"
    "font-size:18px;font-weight:600;padding:16px 36px;border-radius:10px;"
    "text-decoration:none;}"
    "small{display:block;margin-top:24px;color:#94a3b8;font-size:13px;}"
    "</style>"
    "</head><body>"
    "<h1>LitClock Setup</h1>"
    "<p>Tap below to continue setting up your clock.</p>"
    f'<a class="btn" href="http://{SETUP_HOSTNAME}/setup">Open Setup</a>'
    "<small>If the page does not load, open it in your browser using the "
    f"option at the top of the screen, or go to {SETUP_HOSTNAME} (or {HOTSPOT_GATEWAY_IP}).</small>"
    "</body></html>"
)


def _build_cna_bridge_html():
    """Return the precomputed iOS CNA bridge HTML. Kept as a thin wrapper so
    existing tests and call sites can continue to use the function name;
    actual construction happens once at module import."""
    return _CNA_BRIDGE_HTML


def _build_setup_html():
    """Build the setup page HTML.

    Branches on three pieces of module state:

    - ``PROVISIONING_MODE`` — gates the entire WiFi-picker section AND
      picks the page subtitle (#398). In provisioning the subtitle
      orients the user on joining their own WiFi; in the pre-connected
      path (boot with WiFi already configured via ethernet/wpa_supplicant)
      the form is essentially a confirmation button and the subtitle
      drops the WiFi framing to avoid reading wrong.
    - ``WIFI_CONNECT_IN_FLIGHT`` — paints the blue "Connecting…" banner
      with an auto-refresh meta tag. Also shifts the subtitle to a
      "joining your network" register so it doesn't read as a stale
      instruction while the user is already waiting.
    - ``WIFI_CONNECT_ERROR`` — paints the red error banner with the
      hotspot-vs-WiFi password disambiguation copy.

    The hotspot SSID name in the picker-section explainer is sourced
    from ``HOTSPOT_SSID`` (set via the ``--hotspot-ssid`` CLI flag,
    defaults to ``wifi_provision.DEFAULT_SSID`` = "LitClock-Setup") so
    branded / customized builds don't display copy that lies to the
    user about which network is the temporary one.
    """
    wifi_error_banner = ""
    if WIFI_CONNECT_IN_FLIGHT:
        wifi_error_banner = (
            '<div style="background:#eff6ff; border:2px solid #3b82f6; border-radius:8px;'
            ' padding:12px 16px; margin-bottom:16px; color:#1e40af;">'
            "<strong>Connecting to WiFi...</strong> This page will refresh automatically."
            "</div>"
            '<meta http-equiv="refresh" content="10;url=/setup">'
        )
    elif WIFI_CONNECT_ERROR:
        # HTML-escape the raw error. The banner is interpolated into the
        # f-string template via `{wifi_error_banner}`, NOT .format(), so we
        # must NOT double braces — f-string interpolation is non-recursive
        # and doubled braces would leak into the rendered HTML as literal
        # `{{` `}}`. (Earlier copies of this code doubled braces defensively
        # for a .format() call site that no longer exists.)
        safe_error = html.escape(WIFI_CONNECT_ERROR)
        wifi_error_banner = (
            '<div style="background:#fef2f2; border:2px solid #ef4444; border-radius:8px;'
            ' padding:12px 16px; margin-bottom:16px; color:#991b1b;">'
            f"<strong>Couldn&rsquo;t join your WiFi:</strong> {safe_error}<br>"
            "Double-check your <strong>WiFi password</strong> and try "
            "again (not the hotspot password shown on the clock). If this "
            "page doesn&rsquo;t reload, rescan the QR code on the display "
            "&mdash; the hotspot has restarted.</div>"
        )

    # Subtitle copy depends on three states:
    #   - PROVISIONING_MODE + in-flight: user already submitted, page is
    #     auto-refreshing — subtitle should reflect "joining", not "go
    #     pick a network" (avoids reading as a stale instruction).
    #   - PROVISIONING_MODE (default / error retry): orient on joining
    #     their own WiFi.
    #   - else (pre-connected): generic literary subtitle.
    # All three branches use hardcoded literals — no user input flows in,
    # so the f-string interpolation at the bottom of this function does
    # not need html.escape.
    if PROVISIONING_MODE and WIFI_CONNECT_IN_FLIGHT:
        subtitle_text = "Joining your WiFi &mdash; hang tight."
    elif PROVISIONING_MODE:
        subtitle_text = "Connect your clock to the WiFi your phone normally uses."
    else:
        subtitle_text = "Finish setting up your literary clock."

    wifi_section = ""
    if PROVISIONING_MODE:
        network_options = _wifi_network_options()
        # Source the hotspot SSID from the runtime constant rather than
        # hardcoding "LitClock-Setup" — branded builds set this via the
        # --hotspot-ssid CLI flag, and the disambiguating cue ("Not the
        # X hotspot") fails exactly when it's most needed if X is wrong.
        # html.escape because a custom SSID could in theory contain HTML
        # special chars; wifi_provision.DEFAULT_SSID is plain ASCII but
        # we don't enforce that on the CLI override.
        hotspot_name = html.escape(HOTSPOT_SSID or "LitClock-Setup")
        wifi_section = f"""
            <!-- WiFi Section -->
            <div class="section">
                <div class="section-title">
                    <span class="icon">WiFi</span>
                </div>
                <p class="help-text" style="margin:0 0 14px 0; font-size:13px; color:#555;">
                    Pick the WiFi your phone normally uses &mdash; at home, the
                    office, wherever the clock will live. Not the {hotspot_name}
                    hotspot you joined to see this page.
                </p>

                <label>Pick your WiFi network</label>
                <div style="display:flex; gap:8px; margin-bottom:10px;">
                    <select id="wifi-ssid" name="wifi_ssid"
                        style="flex:1; padding:12px; font-size:16px; border:2px solid #ddd; border-radius:8px;">
                        {network_options}
                    </select>
                    <button type="button" class="btn btn-secondary"
                        style="width:auto; margin-bottom:0; padding:12px 16px;"
                        onclick="refreshNetworks()">Refresh</button>
                </div>

                <label>Your WiFi Password</label>
                <input type="password" id="wifi-password" name="wifi_password"
                    placeholder="Enter your WiFi password" autocomplete="off" autofocus>
                <p class="help-text">
                    Use the password for the WiFi you just picked above &mdash; not the
                    hotspot password shown on the clock. Leave blank for open networks.
                </p>
            </div>
"""

    return f"""<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta charset="UTF-8">
    <title>LitClock Setup</title>
    <style>
        * {{
            box-sizing: border-box;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 500px;
            margin: 0 auto;
            padding: 20px;
            background: #f5f5f5;
            color: #333;
        }}
        .container {{
            background: white;
            padding: 25px;
            border-radius: 12px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}
        h1 {{
            font-size: 24px;
            margin: 0 0 5px 0;
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        .subtitle {{
            color: #666;
            font-size: 14px;
            margin-bottom: 25px;
        }}
        .section {{
            margin-bottom: 25px;
            padding-bottom: 20px;
            border-bottom: 1px solid #eee;
        }}
        .section:last-of-type {{
            border-bottom: none;
            margin-bottom: 15px;
        }}
        .section-title {{
            font-size: 16px;
            font-weight: 600;
            margin-bottom: 12px;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .section-title .icon {{
            font-size: 20px;
        }}
        label {{
            display: block;
            font-size: 14px;
            color: #555;
            margin-bottom: 6px;
        }}
        input[type="text"], input[type="number"], input[type="password"] {{
            width: 100%;
            padding: 12px;
            font-size: 16px;
            border: 2px solid #ddd;
            border-radius: 8px;
            margin-bottom: 10px;
        }}
        input:focus {{
            border-color: #4CAF50;
            outline: none;
        }}
        .btn {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            padding: 12px 20px;
            font-size: 16px;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            text-decoration: none;
            transition: background 0.2s;
        }}
        .btn-primary {{
            width: 100%;
            background: #4CAF50;
            color: white;
            font-size: 18px;
            padding: 15px;
        }}
        .btn-primary:hover {{
            background: #45a049;
        }}
        .btn-secondary {{
            background: #e3f2fd;
            color: #1565c0;
            width: 100%;
            margin-bottom: 10px;
        }}
        .btn-secondary:hover {{
            background: #bbdefb;
        }}
        .help-text {{
            font-size: 12px;
            color: #666;
            margin-top: 5px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>LitClock Setup</h1>
        <p class="subtitle">{subtitle_text}</p>

{wifi_error_banner}
        <form id="setup-form" method="POST" action="/setup">
{wifi_section}
            <!-- Submit -->
            <button type="submit" class="btn btn-primary" id="submit-btn">
                Complete Setup
            </button>
            <p class="help-text" style="text-align:center; margin-top:12px;">
                Location, timezone, and units are auto-detected after WiFi connects.
                Adjust anything in Settings once the clock is online.
            </p>
        </form>
    </div>

    <script>
        function refreshNetworks() {{
            var select = document.getElementById('wifi-ssid');
            if (!select) return;
            select.innerHTML = '<option value="">Scanning...</option>';
            select.disabled = true;

            fetch('/scan-wifi')
                .then(function(r) {{ return r.json(); }})
                .then(function(networks) {{
                    select.innerHTML = '';
                    if (networks.length === 0) {{
                        select.innerHTML = '<option value="">No networks found</option>';
                    }} else {{
                        networks.forEach(function(net) {{
                            var opt = document.createElement('option');
                            opt.value = net.ssid;
                            var strength = net.signal >= 70 ? 'Strong' : net.signal >= 40 ? 'Medium' : 'Weak';
                            var lock = (!net.security || net.security === '--') ? ' [Open]' : '';
                            opt.textContent = net.ssid + ' (' + strength + lock + ')';
                            select.appendChild(opt);
                        }});
                    }}
                    select.disabled = false;
                }})
                .catch(function() {{
                    select.innerHTML = '<option value="">Scan failed - try again</option>';
                    select.disabled = false;
                }});
        }}
    </script>
</body>
</html>
"""


HTML_SUCCESS = """<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    {meta_refresh}<meta charset="UTF-8">
    <title>{heading}</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 500px;
            margin: 0 auto;
            padding: 20px;
            background: #f5f5f5;
        }}
        .container {{
            background: white;
            padding: 40px 30px;
            border-radius: 12px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            text-align: center;
        }}
        .checkmark {{
            width: 80px;
            height: 80px;
            background: #4CAF50;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            margin: 0 auto 20px;
            font-size: 40px;
            color: white;
        }}
        h1 {{
            color: #333;
            font-size: 24px;
            margin-bottom: 10px;
        }}
        p {{
            color: #666;
            line-height: 1.6;
            margin-bottom: 20px;
        }}
        .summary {{
            background: #f8f9fa;
            padding: 15px;
            border-radius: 8px;
            text-align: left;
            font-size: 14px;
        }}
        .summary-item {{
            display: flex;
            justify-content: space-between;
            padding: 8px 0;
            border-bottom: 1px solid #eee;
        }}
        .summary-item:last-child {{
            border-bottom: none;
        }}
        .summary-label {{
            color: #666;
        }}
        .summary-value {{
            font-weight: 500;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="checkmark">OK</div>
        <h1>{heading}</h1>
        <p>{subtitle}</p>

        <div class="summary">
            <div class="summary-item">
                <span class="summary-label">WiFi</span>
                <span class="summary-value">{wifi}</span>
            </div>
        </div>

        <p style="margin-top: 25px; font-size: 14px; color: #999;">
            {footer}
        </p>
    </div>
</body>
</html>
"""


HTML_ERROR = """<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Setup Error</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 500px;
            margin: 40px auto;
            padding: 20px;
            background: #f5f5f5;
        }}
        .container {{
            background: white;
            padding: 30px;
            border-radius: 12px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            text-align: center;
        }}
        h1 {{
            color: #c62828;
            font-size: 22px;
        }}
        p {{
            color: #666;
        }}
        a {{
            color: #1565c0;
        }}
        a.loading {{
            pointer-events: none;
            color: #999;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Setup Error</h1>
        <p>{error}</p>
        <p><a href="/" id="retry-link">Try again</a></p>
    </div>
    <script>
    (function() {{
        var link = document.getElementById('retry-link');
        if (link) {{
            link.addEventListener('click', function() {{
                link.textContent = 'Loading...';
                link.className = 'loading';
                setTimeout(function() {{
                    link.textContent = 'Try again';
                    link.className = '';
                }}, 10000);
            }});
        }}
    }})();
    </script>
</body>
</html>
"""


# ── Re-exports ─────────────────────────────────────────────────────────────
#
# #337 A4 — _IP_GEO_RETRY_DELAYS re-export.
# Re-exported from location_resolver so existing tests that read
# ``setup_server._IP_GEO_RETRY_DELAYS`` continue to see the canonical value.
# Functionally identical to ``location_resolver._IP_GEO_RETRY_DELAYS``. Bound
# via attribute access rather than ``from ... import`` so ruff's auto-fix
# doesn't strip it as an unused import — this constant is read externally
# (test_setup_server.py:495 sleep-schedule parity test) but not within
# this module, which would otherwise trip F401.
import location_resolver as _location_resolver_mod  # noqa: E402

_IP_GEO_RETRY_DELAYS = _location_resolver_mod._IP_GEO_RETRY_DELAYS

# #414 maintainability item #5 — set_system_timezone re-export.
# Canonical implementation moved to ``geocoding.set_system_timezone`` so the
# resolver + control_server routes can import it without dragging
# setup_server's captive-portal/http.server imports. This re-export keeps
# existing call sites + tests working without rewrites — anyone doing
# ``from setup_server import set_system_timezone`` or
# ``setup_server.set_system_timezone(...)`` still gets the same callable.
# Use ``geocoding.set_system_timezone`` for new code paths.
from geocoding import set_system_timezone  # noqa: E402,F401


def _update_env_location(lat, lon, *, location_name=None, units=None, timezone=None, **kwargs):
    """Backwards-compat shim for the legacy 5-kwarg writer (#337 A4).

    The canonical implementation now lives in ``location_resolver.update_env_location``;
    this shim exists because (a) existing tests monkeypatch this name to capture
    resolver-writer calls, and (b) first-boot has historically called this function
    by name. New callers should prefer ``location_resolver.update_env_location``
    directly so they can use the new ``mode`` / ``ip_country`` kwargs without
    going through the shim.

    Extra kwargs (``mode``, ``ip_country``) introduced by #337 A1/A6.1 are
    forwarded transparently via ``**kwargs`` so this shim doesn't need a
    signature change every time the canonical writer grows a kwarg.

    **env_file forwarding (#337 /review P0 fix):** the legacy first-boot caller
    has no ENV_FILE param and relies on the module-level constant — fine,
    because first-boot's ``setup_server.main()`` sets it. The new contexts
    (PWA sync-quick + on-boot oneshot) DO have ENV_FILE but reach this shim
    through ``location_resolver.resolve_location_from_ip``, which passes
    ``env_file=`` as a kwarg. Without honouring that kwarg here the shim
    would force the canonical writer to fall back to ``setup_server.ENV_FILE``
    (None in those contexts) and silently no-op — the actual bug Codex caught
    in /review. Pop the override before forwarding so the kwarg doesn't
    duplicate the explicit ``env_file=`` arg below.
    """
    from location_resolver import update_env_location as _impl

    env_file_override = kwargs.pop("env_file", None)
    return _impl(
        lat,
        lon,
        location_name=location_name,
        units=units,
        timezone=timezone,
        env_file=env_file_override if env_file_override else ENV_FILE,
        **kwargs,
    )


def _resolve_location_from_ip():
    """Backwards-compat shim for the first-boot post-WiFi caller (#337 A4).

    Delegates to ``location_resolver.resolve_location_from_ip`` with the full
    retry budget. The on-boot reresolve oneshot calls the canonical function
    directly; the PWA Save Specific→Auto path calls it with ``retries=False``
    (A7 sync-quick) — neither goes through this shim.

    The shim is preserved because (a) first-boot do_POST handlers call by
    name (lines ~1206 / ~1265) and (b) existing tests monkeypatch this name.
    """
    from location_resolver import resolve_location_from_ip as _impl

    _impl(retries=True, env_file=ENV_FILE)


def signal_completion() -> bool:
    """Signal to the installer that we're done. Best-effort; returns True on success.

    The signal file at /tmp/litclock-setup-done is the handoff primitive
    that first-boot.sh's wait loop polls. If touch() fails (tmpfs full,
    permission error, etc.), this returns False so callers can decide whether
    to proceed to SIGTERM — without the signal file, firstboot.sh's wait loop
    would never observe completion.

    Why bool-return instead of raising (#364 codex post-review fix): the two
    call sites in do_POST have load-bearing ordering invariants that a raise
    breaks:

      * No-WiFi branch — signal_completion runs AFTER the HTTP success
        response has been written. BaseHTTPRequestHandler cannot turn a raise
        from this point into a 500 (headers/body already flushed). A raise
        there would skip _schedule_self_terminate, leaving the setup server
        running while firstboot.sh waits for a signal file that will never
        appear.

      * WiFi-success branch — signal_completion runs AFTER teardown_hotspot.
        A raise from here landing in the existing except branch would call
        _restore_hotspot on the same wlan0 we just joined to the user's WiFi,
        destroying the working connection.

    Bool return + explicit check at each call site avoids both regressions.
    If SIGNAL_FILE is not configured (test mode), returns True so test
    callers proceed as if signal succeeded.
    """
    if not SIGNAL_FILE:
        return True
    try:
        Path(SIGNAL_FILE).touch()
        return True
    except Exception as e:
        print(f"Warning: Could not create signal file: {e}")
        return False


class SetupHandler(http.server.BaseHTTPRequestHandler):
    # Per-connection read timeout. StreamRequestHandler.setup() calls
    # self.request.settimeout(self.timeout), which makes rfile.read() raise
    # socket.timeout after HANDLER_TIMEOUT seconds instead of blocking forever.
    timeout = HANDLER_TIMEOUT

    def log_message(self, fmt, *args):
        if PROVISIONING_MODE:
            # flush=True: stdout is block-buffered when piped to journald, so
            # without an explicit flush, captive-portal probe access logs sit
            # in the buffer until the server process exits and may be lost on
            # SIGTERM. We need these logs visible in real time to debug iOS
            # CNA popup behavior. (issue #178)
            print(f"HTTP: {fmt % args}", flush=True)
        # Suppress in normal mode

    def send_html(self, content, status=200):
        self.send_response(status)
        self.send_header("Content-type", "text/html; charset=utf-8")
        # Never let phones or CNA WebViews cache anything we serve. Everything
        # coming out of this server is one-shot provisioning state or dynamic
        # settings — stale copies would re-trigger the exact iOS CNA bug this
        # PR is fixing (old 20 KB setup HTML persisting across boots).
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(content.encode("utf-8"))

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _redirect_to_setup(self):
        """Send a 302 redirect to the setup page (used for captive portal probes)."""
        self.send_response(302)
        self.send_header("Location", f"http://{SETUP_HOSTNAME}/setup")
        self.end_headers()

    def _is_cna_detection_probe(self):
        """True when the request is Apple's WISPr detection client
        (UA "CaptiveNetworkSupport-<ver> wispr") — the probe that decides
        whether the CNA sheet rises. The sheet's WebView and real browsers
        send a Safari-family UA instead and must NOT match."""
        return "CaptiveNetworkSupport" in self.headers.get("User-Agent", "")

    def _redirect_cna_probe(self):
        """Answer the Apple detection probe the way commercial hotspots do:
        302 off the Apple hostname to our own captive page, with the WISPr
        XML block as the response body for smart clients that read it
        instead of following the redirect. Returns the body byte count for
        the probe log.

        litclock-dev#526: iOS 26.5.x stopped promoting a 200-HTML answer
        served under an Apple probe hostname to the CNA sheet (the sheet
        would render spoofed content on an apple.com URL — a phishing
        surface for the iOS 26 Captive Assist credential sync). Portals
        that answer the probe with a redirect to their own host kept
        auto-popping, so the detection probe gets this 302; the sheet's
        WebView and manual browsers still get the 200 bridge. The Location
        uses the raw gateway IP so reaching it needs no DNS at all."""
        body = _WISPR_XML_COMMENT.encode()
        self.send_response(302)
        self.send_header("Location", f"http://{HOTSPOT_GATEWAY_IP}/cna")
        self.send_header("Content-type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return len(body)

    def _handle_captive_portal_probe(self, path, host):
        """Respond to captive portal probe requests to trigger the portal sheet.

        Different OSes detect captive portals differently:
        - iOS: Fetches /hotspot-detect.html, expects exact "Success" text.
               Any non-"Success" 200 response opens the CNA sheet — but iOS
               silently gives up on large or JS-heavy responses and buries
               the portal in Control Center, so we serve a purpose-built
               ~1 KB JS-free bridge (see _CNA_BRIDGE_HTML) to reliably pop
               the CNA sheet. Bug history: issue #175.
        - Android: Fetches /generate_204, expects HTTP 204.
                   A 302 redirect triggers the "Sign in to network" notification.
        - Windows: Fetches /connecttest.txt, expects "Microsoft Connect Test".
        - Firefox: Fetches /canonical.html, expects specific content.

        iOS gets the JS-free bridge HTML; Android / Windows / Firefox get a
        302 redirect to the real setup form.
        """

        # #483 diagnostic: the base access log (log_message) records only the
        # request line + status — NOT the Host header or User-Agent. Those are
        # exactly what a "portal didn't auto-open" repro needs: which host iOS
        # probed (is it one we recognize?), which UA/iOS version, and what we
        # returned. Pair this with dnsmasq's log-queries (already enabled) to
        # see the full DNS→HTTP probe path a phone takes on join.
        # The RESPONSE side (branch + status + bytes) is logged too: the
        # litclock-dev#526 repro established that receipt-only logging cannot
        # distinguish "sheet never opened" from "sheet opened and died" —
        # and the 80-char UA truncation cut off exactly before the Safari/
        # token that separates a manual browser open from the CNA WebView.
        def _probe_log(branch, status, nbytes):
            if PROVISIONING_MODE:
                ua = self.headers.get("User-Agent", "")
                print(
                    f"CAPTIVE-PROBE: host={host!r} path={path!r} -> {branch} "
                    f"status={status} bytes={nbytes} ua={ua[:200]!r}",
                    flush=True,
                )

        # iOS probes. The WISPr detection client gets a 302 to /cna on the
        # gateway IP (iOS 26.5.x no longer reliably promotes 200-HTML served
        # under an Apple hostname to the CNA sheet — see _redirect_cna_probe).
        # Everything else hitting these paths (the CNA sheet's WebView, a
        # manual browser) gets the tiny JS-free bridge: a non-"Success" 200,
        # kept small so the sheet renders it instead of burying the portal
        # in Control Center. The bridge links into the real setup form.
        if path in ("/hotspot-detect.html", "/library/test/success.html"):
            if self._is_cna_detection_probe():
                nbytes = self._redirect_cna_probe()
                _probe_log("cna-302", 302, nbytes)
            else:
                body = _build_cna_bridge_html()
                self.send_html(body)
                _probe_log("cna-bridge", 200, len(body.encode()))
            return True
        # Android probes — 302 redirect triggers "Sign in to network"
        if path in ("/generate_204", "/gen_204"):
            self._redirect_to_setup()
            _probe_log("redirect-setup", 302, 0)
            return True
        # Windows/Firefox probes — 302 redirect
        if path in ("/connecttest.txt", "/ncsi.txt", "/canonical.html", "/success.txt"):
            self._redirect_to_setup()
            _probe_log("redirect-setup", 302, 0)
            return True
        # Host-based fallback for probes that hit an unexpected path (e.g.
        # some iOS versions probe captive.apple.com/ with path "/"). Only
        # Apple hosts get the iOS-shaped bridge HTML — Google/Microsoft/
        # Firefox probe hosts expect a 302, and serving them HTML shaped for
        # iOS would be a regression for Android captive portal detection.
        host_clean = host.split(":")[0].lower()
        if host_clean in CAPTIVE_PORTAL_HOSTS:
            if host_clean in APPLE_CAPTIVE_PORTAL_HOSTS:
                if self._is_cna_detection_probe():
                    nbytes = self._redirect_cna_probe()
                    _probe_log("cna-302-hostmatch", 302, nbytes)
                else:
                    body = _build_cna_bridge_html()
                    self.send_html(body)
                    _probe_log("cna-bridge-hostmatch", 200, len(body.encode()))
            else:
                self._redirect_to_setup()
                _probe_log("redirect-setup-hostmatch", 302, 0)
            return True
        _probe_log("no-match-fallthrough", 0, 0)
        return False

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        host = self.headers.get("Host", "")

        # In provisioning mode, intercept captive portal probes. A request is
        # a probe if EITHER (a) the path isn't one of our real app paths, OR
        # (b) the Host header is a known captive-portal detection host — some
        # phones just hit the root path on captive.apple.com / etc., which
        # would otherwise fall through to _build_setup_html() and re-trigger
        # the exact iOS CNA bug we're trying to fix.
        if PROVISIONING_MODE:
            is_app_path = parsed.path in ("/", "/setup", "/scan-wifi", "/cna")
            is_probe_host = host.split(":")[0].lower() in CAPTIVE_PORTAL_HOSTS
            if is_probe_host or not is_app_path:
                if self._handle_captive_portal_probe(parsed.path, host):
                    return

        if parsed.path == "/" or parsed.path == "/setup":
            self.send_html(_build_setup_html())

        elif parsed.path == "/cna":
            # Target of the detection-probe 302 (and the WISPr LoginURL):
            # the tiny JS-free bridge on our own host, so the CNA sheet
            # never has to render portal content under an Apple hostname.
            self.send_html(_build_cna_bridge_html())

        elif parsed.path == "/scan-wifi":
            global _WIFI_SCAN_CACHE, _WIFI_SCAN_TIME
            import time

            # Serialize scans — multiple phones hitting /scan-wifi concurrently
            # would otherwise each trigger their own nmcli rescan, fighting
            # over the radio and taking much longer than a single rescan.
            with _SCAN_CACHE_LOCK:
                try:
                    from wifi_provision import scan_wifi_networks

                    networks = scan_wifi_networks()
                except Exception:
                    networks = []
                # Filter before BOTH caching and send_json — the client rebuilds
                # the dropdown from this JSON, and the cache feeds the
                # server-rendered dropdown. Leaving either unfiltered re-exposes
                # the clock's own hotspot SSID.
                networks = _filter_own_hotspot(networks)
                if networks:
                    _WIFI_SCAN_CACHE = _build_wifi_options(networks)
                    _WIFI_SCAN_TIME = time.monotonic()
            self.send_json(networks)

        elif PROVISIONING_MODE:
            # In provisioning mode, redirect any unknown path to setup.
            # Catches captive portal probes we don't explicitly handle.
            self._redirect_to_setup()

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        global WIFI_CONNECT_ERROR, WIFI_CONNECT_IN_FLIGHT

        if self.path != "/setup":
            self.send_response(404)
            self.end_headers()
            return

        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            self.send_response(400)
            self.end_headers()
            return
        if content_length < 0 or content_length > MAX_POST_BODY:
            self.send_response(413)
            self.end_headers()
            return
        post_data = self.rfile.read(content_length).decode()
        params = urllib.parse.parse_qs(post_data)

        # EPIC #383: the hotspot form collects WiFi credentials only.
        # Location, timezone, and units are auto-populated post-WiFi by
        # _resolve_location_from_ip() from ip-api.com; ALLOW_NSFW_QUOTES
        # stays at the env.sh.sample default until the user toggles it in
        # the PWA Settings tab. The handler itself writes nothing
        # synchronously — env.sh is written by the background thread
        # spawned below, after the resolver returns.
        wifi_ssid = params.get("wifi_ssid", [""])[0].strip()

        if PROVISIONING_MODE and not wifi_ssid:
            self.send_html(HTML_ERROR.format(error="Please select a WiFi network"))
            return

        # Never let the clock try to join its own setup hotspot. The picker
        # filters HOTSPOT_SSID out (see _filter_own_hotspot), but a stale page
        # or scripted POST could still submit it. Connecting would tear down the
        # AP and fail (single-radio chip can't join the network it's hosting),
        # dumping the user into the connect-fail + hotspot-restart retry loop.
        # Reject up front with a clear message instead.
        if PROVISIONING_MODE and HOTSPOT_SSID and wifi_ssid == HOTSPOT_SSID:
            self.send_html(
                HTML_ERROR.format(error="That's the clock's own setup network. Pick your home or office WiFi instead.")
            )
            return

        # Send success response BEFORE connecting to WiFi. On the Pi Zero 2W,
        # connecting to WiFi tears down the hotspot (single-radio chip can't
        # do AP+Station simultaneously), so the phone loses connection. We
        # must deliver the confirmation page while the hotspot is still up.
        # All the user-visible values are "Auto-detecting..." because IP-geo
        # runs after WiFi connects — PR2's handoff splash + PWA banner shows
        # the resolved values once they're known.
        if PROVISIONING_MODE and wifi_ssid:
            wifi_display = wifi_ssid
            heading = "Settings Saved!"
            subtitle = f"Connecting to <strong>{html.escape(wifi_ssid)}</strong>..."
            meta_refresh = '<meta http-equiv="refresh" content="15;url=/setup">\n    '
            footer = "This page will automatically check the connection status."
        else:
            wifi_display = "Already connected"
            heading = "Setup Complete!"
            subtitle = "Your LitClock is configured and ready to display literary quotes."
            meta_refresh = ""
            footer = "You can close this page now. The clock will start displaying shortly."

        self.send_html(
            HTML_SUCCESS.format(
                wifi=html.escape(wifi_display),
                heading=heading,
                subtitle=subtitle,
                meta_refresh=meta_refresh,
                footer=footer,
            )
        )

        # Connect to WiFi (provisioning only) + resolve location from IP +
        # signal completion + teardown. Runs in a background thread AFTER the
        # response has been sent so the phone sees the success page before
        # the hotspot tears down.
        if PROVISIONING_MODE and wifi_ssid:
            # Password is only used in this branch — sourced here so a normal-
            # mode POST never reads or holds the sensitive value in memory.
            wifi_password = params.get("wifi_password", [""])[0]

            # Atomically check-and-set the in-flight flag. With the threaded
            # server, two POSTs can arrive simultaneously and both pass the
            # check without the lock, spawning two conflicting connect attempts.
            with _WIFI_CONNECT_LOCK:
                if WIFI_CONNECT_IN_FLIGHT:
                    return
                WIFI_CONNECT_IN_FLIGHT = True
                WIFI_CONNECT_ERROR = None  # Clear stale error from previous attempt

            def _connect_and_teardown():
                global WIFI_CONNECT_ERROR, WIFI_CONNECT_IN_FLIGHT
                import time

                time.sleep(1)  # Let response flush to phone
                success_completed = False
                try:
                    from wifi_provision import connect_to_wifi, create_hotspot, teardown_hotspot

                    # P2.C: replace the stale hotspot QR with a progress splash
                    # while WiFi connects + IP-geo resolves (~30s).
                    _show_connecting_splash(wifi_ssid)
                    success, error = connect_to_wifi(wifi_ssid, wifi_password)
                    if not success:
                        # WiFi failed — on single-radio Pi, nmcli killed the hotspot
                        # when it tried to connect. Restore it so the phone can
                        # reconnect and the user can retry.
                        WIFI_CONNECT_ERROR = error
                        print(f"WiFi connection failed: {error}")
                        _restore_hotspot(create_hotspot)
                        return  # Keep server alive for retry

                    # WiFi connected — resolve location from IP, then clean up.
                    # Wrap the resolver in its own try/except so a future bug
                    # in env-write logic can't unwind to the outer except (which
                    # would call _restore_hotspot on the same wlan0 we just
                    # joined to the user's WiFi and destroy the connection).
                    teardown_hotspot()
                    try:
                        _resolve_location_from_ip()
                    except Exception as resolver_exc:
                        print(f"Resolver raised post-WiFi: {resolver_exc} — continuing without location")
                    if signal_completion():
                        success_completed = True
                    else:
                        # Signal file write failed. WiFi is up and the hotspot
                        # is down — DO NOT fall into the except branch, which
                        # would call _restore_hotspot() on the same wlan0 and
                        # destroy the working WiFi connection we just joined.
                        # Leave success_completed=False; the outer finally
                        # clears IN_FLIGHT and does NOT SIGTERM. firstboot.sh
                        # will time out; on next reboot it re-enters AP mode
                        # for re-provisioning. User can power-cycle to recover.
                        WIFI_CONNECT_ERROR = "Setup completed but signal file write failed"
                        print("Warning: signal_completion failed; setup server staying up, no SIGTERM scheduled")
                except Exception as e:
                    WIFI_CONNECT_ERROR = f"Setup error: {e}"
                    print(f"Post-setup error: {e}")
                    try:
                        from wifi_provision import create_hotspot as _create_hp

                        _restore_hotspot(_create_hp)
                    except Exception:
                        pass
                    return  # Keep server alive
                finally:
                    # Sequencing invariant — issue #364.
                    # Queue SIGTERM BEFORE clearing IN_FLIGHT so reset_state()'s drain
                    # barrier (which polls IN_FLIGHT and exits on False) cannot return
                    # claiming "thread quiescent" while a SIGTERM is still pending in
                    # the kernel queue. The kernel may deliver the signal microseconds
                    # after this line, but the drain barrier observed IN_FLIGHT=True
                    # at the moment SIGTERM was queued. See _schedule_self_terminate's
                    # docstring for the full story.
                    #
                    # Nested try/finally guarantees IN_FLIGHT clears even if the
                    # helper raises — without it, a failed helper would leave the
                    # flag stuck True forever and block subsequent setup attempts.
                    try:
                        if success_completed:
                            _schedule_self_terminate()
                    finally:
                        WIFI_CONNECT_IN_FLIGHT = False

            _spawn_bg(_connect_and_teardown, name="setup-wifi-connect")
        else:
            # Normal mode (already on WiFi) — resolve location from IP-geo,
            # then signal + SIGTERM. Run in a daemon thread so the HTTP
            # response flushes immediately rather than waiting up to ~13s
            # for the resolver's retry budget. Only schedule SIGTERM if
            # signal_completion lands — otherwise firstboot.sh's wait loop
            # would never observe handoff and the user would be stuck.

            def _resolve_and_signal():
                import time

                time.sleep(1)  # Let response flush to client
                try:
                    _resolve_location_from_ip()
                except Exception as e:
                    print(f"Normal-mode resolver raised: {e}")
                if signal_completion():
                    _schedule_self_terminate(delay=2.0)
                else:
                    print("Warning: signal_completion failed in normal-mode branch; not scheduling SIGTERM")

            _spawn_bg(_resolve_and_signal, name="setup-resolve-signal")


def generate_self_signed_cert(cert_dir):
    """Self-signed cert generator — delegates to the shared `https_cert` module
    (extracted in M1 so control_server reuses the same code path)."""
    from https_cert import generate_self_signed_cert as _shared

    return _shared(cert_dir)


class ThreadedHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Multi-threaded HTTP server.

    Each request runs on its own daemon thread so a stuck client (captive
    portal probe that stalls mid-request, phone that drops off the AP
    without closing its TCP connection, etc.) cannot block new requests.
    The per-handler socket timeout (SetupHandler.timeout) bounds how long
    any single handler can block.
    """

    allow_reuse_address = True
    daemon_threads = True
    # Larger accept queue so a burst of captive portal probes (iOS/Android
    # can send 3–5 within the first second after joining the AP) doesn't
    # fill the default backlog of 5 and cause kernel SYN drops.
    request_queue_size = 64


class HTTPSServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Multi-threaded HTTPS server with SSL support."""

    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, server_address, handler_class, cert_file, key_file):
        super().__init__(server_address, handler_class)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_file, key_file)
        self.socket = context.wrap_socket(self.socket, server_side=True)


def _restore_hotspot(create_hotspot_fn):
    """Restore the hotspot after a failed WiFi connection attempt.

    On single-radio Pi hardware, nmcli tears down the hotspot when attempting
    a station connection. If the connection fails, we must recreate the hotspot
    so the user can reconnect and retry — AND refresh the e-ink display with a
    distinct "Wrong Password — Retry" variant, because phones auto-disconnect
    from the hotspot during the failed attempt and may not see the red banner
    on the setup form until they've rescanned the QR. The e-ink is the only
    surface the user is looking at during that gap.
    """
    if not HOTSPOT_SSID or not HOTSPOT_PASSWORD:
        print("No hotspot credentials available — cannot restore hotspot")
        return
    import time

    print("Restoring hotspot for retry...")
    restored = False
    for attempt in range(3):
        result = create_hotspot_fn(ssid=HOTSPOT_SSID, password=HOTSPOT_PASSWORD)
        if result:
            print("Hotspot restored successfully")
            restored = True
            break
        print(f"Hotspot restore attempt {attempt + 1}/3 failed")
        time.sleep(2)

    if not restored:
        print("WARNING: Could not restore hotspot after 3 attempts")
        return

    # Refresh the e-ink display with the retry variant. Best-effort — if the
    # display module fails to import (dev machine) or the hardware refresh
    # errors, we still want the hotspot itself to stay up. Same-credential QR
    # code so users who auto-rejoined the hotspot keep working.
    #
    # Narrow except list: ImportError covers missing PIL / waveshare drivers
    # on a dev machine; OSError / RuntimeError cover SPI failures, kernel
    # driver issues, and the waveshare driver's own RuntimeError on re-init.
    # Everything else propagates — a TypeError from a bad signature change
    # should fail loud, not silently degrade the retry UX.
    try:
        from eink_display import HOTSPOT_RETRY_WIFI_PASSWORD, display_hotspot_info

        display_hotspot_info(
            HOTSPOT_SSID,
            HOTSPOT_PASSWORD,
            HOTSPOT_GATEWAY_IP,
            retry_reason=HOTSPOT_RETRY_WIFI_PASSWORD,
        )
        logging.info("E-ink refreshed with retry instructions")
    except (ImportError, OSError, RuntimeError):
        logging.warning("Could not refresh e-ink with retry state", exc_info=True)


def _show_connecting_splash(ssid):
    """Best-effort 'Connecting to {SSID}…' splash (EPIC #383 PR2, design P2.C).

    Bridges the ~30s gap between hotspot teardown and the control_server handoff
    splash so the e-ink isn't stuck on the stale hotspot QR while WiFi connects
    and IP-geo runs. Same narrow except list + rationale as the retry refresh
    above — a dev-machine import miss or an SPI hiccup must never abort the
    connect flow that's already underway."""
    try:
        from eink_display import display_status

        display_status(f"Connecting to {ssid}…", "This can take up to a minute.")
        logging.info("E-ink refreshed with connecting splash")
    except (ImportError, OSError, RuntimeError):
        logging.warning("Could not refresh e-ink with connecting state", exc_info=True)


def main():
    global ENV_FILE, SIGNAL_FILE, PROVISIONING_MODE, HOTSPOT_SSID, HOTSPOT_PASSWORD

    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <env_file> <signal_file> [--provisioning]")
        sys.exit(1)

    ENV_FILE = sys.argv[1]
    SIGNAL_FILE = sys.argv[2]
    PROVISIONING_MODE = "--provisioning" in sys.argv

    # Parse optional hotspot credentials (used to restore hotspot after failed WiFi)
    for i, arg in enumerate(sys.argv):
        if arg == "--hotspot-ssid" and i + 1 < len(sys.argv):
            HOTSPOT_SSID = sys.argv[i + 1]
        elif arg == "--hotspot-password" and i + 1 < len(sys.argv):
            HOTSPOT_PASSWORD = sys.argv[i + 1]

    if not os.path.exists(ENV_FILE):
        print(f"Error: {ENV_FILE} not found")
        sys.exit(1)

    if PROVISIONING_MODE:
        # Provisioning mode: HTTP only on all interfaces.
        # DHCP Option 114 (RFC 8908) was considered but requires a CA-signed TLS
        # cert, which is impossible on a local hotspot IP. iOS also ignores
        # Option 114 on first connection. Instead we rely on legacy captive portal
        # detection: DNS-intercept all domains → hotspot IP, then serve
        # OS-specific probe responses to trigger the captive portal sheet.
        with ThreadedHTTPServer(("", PORT), SetupHandler) as httpd:
            print(f"Setup server (provisioning) running on HTTP port {PORT}")
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                httpd.shutdown()
    else:
        # Normal mode: try HTTPS, fall back to HTTP
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cert_dir = os.path.join(project_root, ".certs")
        os.makedirs(cert_dir, exist_ok=True)

        cert_file, key_file = generate_self_signed_cert(cert_dir)

        if cert_file and key_file:
            try:
                httpd = HTTPSServer(("", HTTPS_PORT), SetupHandler, cert_file, key_file)
                print(f"Setup server running on HTTPS port {HTTPS_PORT}")
                try:
                    httpd.serve_forever()
                except KeyboardInterrupt:
                    pass
                finally:
                    httpd.shutdown()
            except Exception as e:
                print(f"HTTPS failed: {e}, falling back to HTTP")
                cert_file = None

        if not cert_file:
            with ThreadedHTTPServer(("", PORT), SetupHandler) as httpd:
                print(f"Setup server running on HTTP port {PORT}")
                try:
                    httpd.serve_forever()
                except KeyboardInterrupt:
                    pass
                finally:
                    httpd.shutdown()


if __name__ == "__main__":
    main()
