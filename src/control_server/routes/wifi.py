"""POST /api/wifi/reset — drop the user back into firstboot AP-mode (#245 M5).

Per locked D11: dispatches via ``sudo systemctl start --no-block
litclock-wifi-reset.service``. The unit's ``Conflicts=litclock-update.service``
directive gives systemd-native job-level interlock against running a reset
mid-update; this route also pre-checks the same gate (D5/F7) so the user
sees a 409 with a clear error message instead of an opaque systemd refusal.

The actual reset work (stop control_server, wipe ALL wifi-type connections
by UUID, rm /etc/litclock/.setup-complete, restart litclock-firstboot.service)
runs inside the unit's ExecStart — see scripts/litclock-wifi-reset.sh.
This route just validates the request and kicks off the unit.

env.sh is preserved verbatim across the reset (D1 — location, weather,
gift-mode all stay set). The user only re-enters WiFi credentials.

Routes:
- POST /api/wifi/reset — confirm-token gated, rate-limited, blocks during
                         active update.
"""

from __future__ import annotations

import subprocess
from typing import Final

from flask import Blueprint, current_app, jsonify, request

from .. import update_state
from ..confirm_tokens import ConfirmTokenStore, envelope_for_consume_outcome
from ..errors import envelope
from ..rate_limit import RateLimiter

bp = Blueprint("wifi", __name__)

SYSTEMCTL: Final[str] = update_state.SYSTEMCTL_BIN
SYSTEMCTL_TIMEOUT_S: Final[int] = 5
RESET_UNIT: Final[str] = "litclock-wifi-reset.service"


def _store() -> ConfirmTokenStore:
    return current_app.extensions["confirm_tokens"]


def _rate_limiter() -> RateLimiter:
    return current_app.extensions["system_rate_limiter"]


def _client_ip() -> str:
    return request.remote_addr or f"unknown-{id(request)}"


def _check_rate_limit() -> tuple[object, int] | None:
    allowed, retry_after_s = _rate_limiter().take(_client_ip())
    if allowed:
        return None
    body, status = envelope(
        "rate_limited",
        "Too many destructive actions. Try again shortly.",
        429,
        retry_after_s=retry_after_s,
    )
    body.headers["Retry-After"] = str(retry_after_s)
    return body, status


def _extract_token() -> str | None:
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        token = body.get("token")
        if isinstance(token, str):
            return token
    form_token = request.form.get("token")
    return form_token if isinstance(form_token, str) and form_token else None


@bp.route("/api/wifi/reset", methods=["POST"])
def reset() -> tuple[object, int]:
    """Trigger litclock-wifi-reset.service after standard gates pass.

    Order:
    1. Rate-limit (shared 5/min bucket).
    2. Confirm token (action='wifi_reset', single-use, 300s TTL).
    3. Update-busy gate (D5/F7) — return 409 update_in_progress if a
       weekly fire or PWA-triggered apply is currently running. The
       systemd unit has Conflicts=litclock-update.service as the
       deterministic backstop, but checking here gives the PWA a
       human-readable message instead of an opaque systemd error.
    4. Dispatch via `sudo systemctl start --no-block`.

    On success: 200 with a "switching to setup mode" message so the PWA
    can render handoff copy ("Connect your phone to LitClock-Setup
    hotspot") before its LAN connection drops in ~2s.
    """
    rate_limit_response = _check_rate_limit()
    if rate_limit_response is not None:
        return rate_limit_response

    token = _extract_token()
    if token is None:
        return envelope(
            "confirm_token_invalid",
            "Confirm token is missing.",
            401,
        )
    result = _store().consume_classified("wifi_reset", token)
    if result.outcome != "ok":
        return envelope_for_consume_outcome(result.outcome)
    expiry = result.expiry

    if update_state.update_is_busy():
        # Issue #328 — pre-side-effect failure: no systemctl dispatched.
        # Restore the token so the user can retry once the update finishes
        # without re-opening the page (this was the live #327-adjacent case
        # caught in M8 hardware QA when masked units made the destructive
        # endpoint look "expired" instead of showing the real error).
        _store().restore("wifi_reset", token, expiry)
        return envelope(
            "update_in_progress",
            "An update is in progress. Try again in a few minutes.",
            409,
        )

    try:
        subprocess.run(
            ["sudo", SYSTEMCTL, "start", "--no-block", RESET_UNIT],
            check=True,
            timeout=SYSTEMCTL_TIMEOUT_S,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = getattr(exc, "stderr", b"") or b""
        current_app.logger.error(
            "systemctl start %s failed: %s",
            RESET_UNIT,
            stderr.decode(errors="replace").strip(),
        )
        # Issue #328 — pre-side-effect failure: systemctl returned non-zero
        # BEFORE the wifi-reset unit started (typical case: unit masked or
        # missing, which is the exact M8 hardware-QA #327 scenario this
        # entire restore-on-failure expansion was filed to address).
        # Restore the token so retry surfaces the real error.
        #
        # Review D1 caveat: a non-zero exit from `systemctl --no-block`
        # does NOT strictly prove "no side effect" — narrow theoretical
        # window where systemd queues the job then a dbus reply glitch
        # causes the wrapper to exit non-zero. In that case, restoring the
        # token lets the user double-fire wifi_reset. The unit's ExecStart
        # is litclock-wifi-reset.sh, which wipes WiFi connections by UUID
        # and re-arms litclock-firstboot.service — both idempotent (a
        # second wipe targets already-removed UUIDs and no-ops; firstboot
        # re-arming is unit-state-based). So double-fire today is
        # harmless. If a future non-idempotent step is added to the
        # wifi-reset ExecStart, this restore must be narrowed to specific
        # pre-dispatch stderr patterns or removed.
        _store().restore("wifi_reset", token, expiry)
        return envelope(
            "wifi_reset_failed",
            "The WiFi reset could not be started.",
            500,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = getattr(exc, "stderr", b"") or b""
        current_app.logger.error(
            "systemctl start %s timed out: %s",
            RESET_UNIT,
            stderr.decode(errors="replace").strip(),
        )
        # Issue #328 — DON'T restore on timeout. systemctl --no-block
        # returns immediately on success; a timeout means the unit may
        # actually have dispatched. Paranoid: keep the token consumed
        # so we don't double-fire the WiFi wipe.
        return envelope(
            "wifi_reset_failed",
            "The WiFi reset could not be started.",
            500,
        )

    return (
        jsonify(
            {
                "ok": True,
                "message": ("Switching to setup mode... Connect your phone to the LitClock-Setup hotspot."),
            }
        ),
        200,
    )
