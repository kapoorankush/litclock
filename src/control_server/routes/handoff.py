"""Post-WiFi PWA handoff endpoints (EPIC #383 PR2, issue #388).

Two POST endpoints that complete the handoff phase and let quotes start:

- POST /api/handoff/done           — explicit "Done" tap (success-state banner,
                                      i.e. IP-geo detected a location). Writes
                                      .handoff-complete iff the timezone is
                                      already known; refuses with 409 otherwise
                                      so a wrong-time clock never starts.
- POST /api/handoff/set-timezone   — browser-tz fallback (failure-state banner,
                                      IP-geo couldn't detect a location). The
                                      PWA posts Intl.DateTimeFormat().
                                      resolvedOptions().timeZone; the server
                                      sets the system tz and completes.

CSRF: deliberately NOT token-guarded (locked plan A6). These are non-destructive
and idempotent — ``done`` only flips a one-way "handoff done" marker, and
``set-timezone`` sets a value the user is explicitly confirming. They are
reachable only during the ~2-minute handoff window on a freshly provisioned
device, on the user's own post-WPA2 home WiFi (the locked LAN-trust threat
model, PLAN A4). The persistent management surface (Settings) keeps its CSRF
token; this transient setup surface does not need one.
"""

from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify, request

from .. import handoff
from ..errors import envelope

bp = Blueprint("handoff", __name__)

log = logging.getLogger(__name__)


@bp.route("/api/handoff/done", methods=["POST"])
def done() -> tuple[object, int]:
    """Complete the handoff on an explicit "Done" tap.

    Idempotent: if the handoff already completed (or never started), return 200
    so a double-tap or a stale tab can't error. If the handoff is active but the
    timezone isn't known yet, refuse with 409 — the UI only shows this button in
    the success state, so reaching here without a tz means the client is in the
    failure state and must use /api/handoff/set-timezone instead."""
    if not handoff.is_handoff_active(current_app):
        return jsonify({"ok": True, "complete": True}), 200

    if not handoff.timezone_known(current_app):
        return envelope(
            "timezone_required",
            "Set your timezone first so quotes show at the right time.",
            409,
        )

    if not handoff.mark_handoff_complete(current_app):
        return envelope(
            "handoff_write_failed",
            "Could not finish setup. The clock will start on its own shortly.",
            500,
        )
    return jsonify({"ok": True, "complete": True}), 200


@bp.route("/api/handoff/set-timezone", methods=["POST"])
def set_timezone() -> tuple[object, int]:
    """Browser-tz fallback: set the system timezone the phone reports, then
    complete the handoff. This is the one completion path allowed to run with
    no location set — the user has explicitly confirmed the tz, which is all
    quote rendering needs (weather stays off until a location is added).

    Guarded by is_handoff_active so this is NOT a permanent, CSRF-less,
    LAN-reachable system-timezone setter: outside the handoff window, the
    CSRF-guarded Settings tab owns timezone. After handoff it's a 200 no-op."""
    if not handoff.is_handoff_active(current_app):
        return jsonify({"ok": True, "complete": True}), 200

    body = request.get_json(silent=True)
    timezone = body.get("timezone") if isinstance(body, dict) else None
    if not isinstance(timezone, str) or not timezone.strip():
        return envelope(
            "timezone_required",
            "A timezone is required.",
            422,
        )

    ok, err = handoff.set_timezone_and_complete(current_app, timezone.strip())
    if not ok:
        # set_system_timezone validates against `timedatectl list-timezones`,
        # so a bad value here is a client/IANA-name problem, not a server fault.
        log.warning("handoff set-timezone failed for %r: %s", timezone, err)
        return envelope(
            "invalid_timezone",
            "That timezone isn't recognized. Pick one from Settings instead.",
            422,
        )
    return jsonify({"ok": True, "complete": True, "timezone": timezone.strip()}), 200
