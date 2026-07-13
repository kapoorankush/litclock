"""GET /api/health — post-restart reconnect probe.

Per PLAN A8: the PWA polls this endpoint every 3s with a 1s timeout while
reconnecting after a Restart / Apply Update / Reset WiFi action. A version
mismatch (post-update reload) triggers ``window.location.reload()`` on the
client; uptime is informational. Keep the response shape stable — the M4/M5
client code reads ``version`` and ``uptime_s`` by name.
"""

from __future__ import annotations

import time

from flask import Blueprint, current_app, jsonify

from ..version import get_version

bp = Blueprint("health", __name__)

# Service-uptime baseline. Captured at import (i.e., process start) so a
# systemctl restart resets the counter. That is exactly the signal the PWA
# needs to detect "the service restarted while I wasn't looking."
_APP_START_MONOTONIC = time.monotonic()


@bp.route("/api/health")
def health() -> tuple[object, int]:
    resp = jsonify(
        {
            "ok": True,
            # Identity marker for the cross-origin mDNS bookmark probe (#487).
            # The probe reads this to confirm the responder is actually THIS
            # LitClock and not some other `.local` device answering on the port.
            "app": "litclock",
            "version": get_version(current_app.config.get("VERSION_OVERRIDE")),
            "uptime_s": int(time.monotonic() - _APP_START_MONOTONIC),
        }
    )
    # CORS (#487): the mDNS bookmark probe in status.js fetches this endpoint
    # cross-origin (the PWA is loaded from the IP origin, the probe targets the
    # `litclock.local` origin) and must READ the body to verify identity. Before
    # this, the probe used an opaque `mode:'no-cors'` fetch that could only tell
    # "something answered", not "the LitClock answered" — so a different `.local`
    # device on the same port would false-positive the "Switch to litclock.local" offer.
    # `*` is safe on this endpoint: read-only, no secrets, no credentials, and
    # scoped to `/api/health` only (not the whole app).
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200
