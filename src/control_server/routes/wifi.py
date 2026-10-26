"""POST /api/wifi/reset — drop the user back into firstboot AP-mode (litclock-dev#245 M5).

Per locked D11: dispatches via ``sudo systemctl start --no-block
litclock-wifi-reset.service``. The unit's ``Conflicts=litclock-update.service``
directive gives systemd-native job-level interlock against running a reset
mid-update; this route also pre-checks the same gate (D5/F7) so the user
sees a 409 with a clear error message instead of an opaque systemd refusal.

The actual reset work (skip if first-boot is already provisioning; refuse if
nmcli is missing; enable litclock-firstboot.service, refusing the reset
if that fails — litclock-dev#938; stop control_server; rm
/etc/litclock/.setup-complete and .handoff-complete; wipe ALL wifi-type
connections by UUID; restart litclock-firstboot.service) runs inside the
unit's ExecStart — see scripts/litclock-wifi-reset.sh.
This route just validates the request and kicks off the unit.

env.sh is preserved verbatim across the reset (D1 — location, weather,
gift-mode all stay set). The user only re-enters WiFi credentials.

Routes:
- POST /api/wifi/reset — confirm-token gated, rate-limited, blocks during
                         active update.
- GET  /api/wifi/reset/status — what the reset job did since the last
                         dispatch, for the PWA's refused card (litclock-dev#939).
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Final

from flask import Blueprint, current_app, jsonify, request

import strings_catalog  # noqa: E402 — src/ on sys.path; hard dep since litclock-dev#532

from .. import update_state
from ..confirm_tokens import ConfirmTokenStore, envelope_for_consume_outcome
from ..errors import envelope
from ..handoff import _setup_complete_path
from ..rate_limit import RateLimiter

bp = Blueprint("wifi", __name__)

SYSTEMCTL: Final[str] = update_state.SYSTEMCTL_BIN
SYSTEMCTL_TIMEOUT_S: Final[int] = 5
RESET_UNIT: Final[str] = "litclock-wifi-reset.service"
# litclock-dev#939 — when THIS server last queued the reset: CLOCK_MONOTONIC
# (the clock systemd stamps unit state changes with) and wall time (the clock
# file mtimes use). tmpfs, so it survives the ExecStopPost failsafe restarting
# this server and resets with a reboot, exactly as the monotonic clock does.
DEFAULT_DISPATCH_FILE: Final[Path] = Path("/run/litclock/wifi-reset-dispatch")


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
        strings_catalog.get("common.rate_limited.destructive"),
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
    can render handoff copy ("Join LitClock-Setup from your phone's WiFi
    list") before its LAN connection drops in ~2s. The 200 means the job
    was QUEUED, not that the reset happened (litclock-dev#939). After it:
    the script may refuse before changing anything (this server never goes
    away); abort before the setup marker is removed (the unit's
    ExecStopPost= failsafe brings this server back); or abort after that,
    e.g. a WiFi-wipe timeout (the failsafe restarts first-boot: with no
    WiFi profile left the device comes up as the LitClock-Setup hotspot;
    if a profile survived a partial wipe, first-boot's pre-connected path
    rejoins that network and this server returns on the handoff splash,
    usually at the same origin).
    litclock-dev#938.
    """
    rate_limit_response = _check_rate_limit()
    if rate_limit_response is not None:
        return rate_limit_response

    token = _extract_token()
    if token is None:
        return envelope(
            "confirm_token_invalid",
            strings_catalog.get("common.alert.token_invalid"),
            401,
        )
    result = _store().consume_classified("wifi_reset", token)
    if result.outcome != "ok":
        return envelope_for_consume_outcome(result.outcome)
    expiry = result.expiry

    if update_state.update_is_busy():
        # Issue litclock-dev#328 — pre-side-effect failure: no systemctl dispatched.
        # Restore the token so the user can retry once the update finishes
        # without re-opening the page (this was the live litclock-dev#327-adjacent case
        # caught in M8 hardware QA when masked units made the destructive
        # endpoint look "expired" instead of showing the real error).
        _store().restore("wifi_reset", token, expiry)
        return envelope(
            "update_in_progress",
            strings_catalog.get("common.update_in_progress"),
            409,
        )

    _record_dispatch()
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
        # Issue litclock-dev#328 — pre-side-effect failure: systemctl returned non-zero
        # BEFORE the wifi-reset unit started (typical case: unit masked or
        # missing, which is the exact M8 hardware-QA litclock-dev#327 scenario this
        # entire restore-on-failure expansion was filed to address).
        # Restore the token so retry surfaces the real error.
        #
        # Review D1 caveat: a non-zero exit from `systemctl --no-block`
        # does NOT strictly prove "no side effect" — narrow theoretical
        # window where systemd queues the job then a dbus reply glitch
        # causes the wrapper to exit non-zero. In that case, restoring the
        # token lets the user double-fire wifi_reset. A second run is
        # harmless by design: once first-boot is running a setup (marker
        # gone, unit activating), litclock-wifi-reset.sh exits 0 before
        # touching anything, so it cannot delete the live LitClock-Setup
        # hotspot or restart a running setup (litclock-dev#938 review). A
        # second run that lands before that repeats every step (enable, stop
        # control, rm -f, delete-by-UUID, restart first-boot) on a device that
        # has not started provisioning yet. The guard starts at "activating",
        # not at the moment Step 4 queues the job; a run in that gap is the
        # same repeat. So double-fire today is harmless. If a future non-idempotent step is added to the
        # wifi-reset ExecStart, this restore must be narrowed to specific
        # pre-dispatch stderr patterns or removed.
        _store().restore("wifi_reset", token, expiry)
        return envelope(
            "wifi_reset_failed",
            strings_catalog.get("api.wifi.reset_failed"),
            500,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = getattr(exc, "stderr", b"") or b""
        current_app.logger.error(
            "systemctl start %s timed out: %s",
            RESET_UNIT,
            stderr.decode(errors="replace").strip(),
        )
        # Issue litclock-dev#328 — DON'T restore on timeout. systemctl --no-block
        # returns immediately on success; a timeout means the unit may
        # actually have dispatched. Paranoid: keep the token consumed
        # so we don't double-fire the WiFi wipe.
        return envelope(
            "wifi_reset_failed",
            strings_catalog.get("api.wifi.reset_failed"),
            500,
        )

    return (
        jsonify(
            {
                "ok": True,
                "message": ("Switching to setup mode... Join LitClock-Setup from your phone's WiFi list."),
            }
        ),
        200,
    )


def _dispatch_file() -> Path:
    return Path(os.environ.get("LITCLOCK_WIFI_RESET_DISPATCH_FILE", str(DEFAULT_DISPATCH_FILE)))


def _record_dispatch() -> None:
    """Stamp the dispatch BEFORE queueing, so any unit state change the
    status endpoint later sees is provably newer. Best effort: without the
    stamp the status reads ``unknown`` and the PWA keeps its handoff card,
    which is the behaviour before litclock-dev#939."""
    try:
        _dispatch_file().write_text(f"{time.monotonic():.6f} {time.time():.6f}\n", encoding="utf-8")
    except OSError as exc:
        current_app.logger.warning("could not record the WiFi reset dispatch: %s", exc)


@bp.route("/api/wifi/reset/status", methods=["GET"])
def reset_status() -> tuple[object, int]:
    """What the reset job did since this server last queued it (litclock-dev#939).

    ``POST /api/wifi/reset`` answers 200 once the job is QUEUED; the script
    can still refuse before its Step 1 stops this server. The PWA used to
    infer a refusal from this server still answering, which is wrong after a
    reset that worked and was re-provisioned onto the same network. This
    reads the unit instead. Read-only ``systemctl show`` needs no sudo.

    ``state``:
      * ``refused`` — the unit ended FAILED after our dispatch AND
        ``.setup-complete`` is older than the dispatch, i.e. the script
        stopped before its Step 2 removed it (the nmcli pre-flight, the litclock-dev#938
        enable guard, a failed ``rm``). The clock is still on its WiFi and no
        LitClock-Setup network is coming. A failure AFTER Step 2 is not a
        refusal: the failsafe restarts first-boot, which can re-provision on
        the same network and recreate the marker, so the reset did run
        (litclock-dev#949 re-review: a Step 3 timeout read as "didn't start").
      * ``pending`` — no state change since the dispatch yet (queued, e.g.
        behind ``Conflicts=litclock-update.service``'s stop) while an
        earlier state change is on record; a first reset of the boot reads
        ``unknown`` here instead, which the PWA treats the same.
      * ``running`` / ``done`` — started, or finished cleanly.
      * ``unknown`` — no dispatch on record, systemd did not answer, the unit
        was unloaded (systemd then reports no timestamp), or it failed after
        the setup marker was removed or recreated.
    """
    try:
        mono, wall = _dispatch_file().read_text(encoding="utf-8").split()
        dispatched, dispatched_wall = float(mono), float(wall)
    except (OSError, ValueError):
        return jsonify({"state": "unknown"}), 200
    try:
        out = subprocess.run(
            [
                SYSTEMCTL,
                "show",
                RESET_UNIT,
                "-p",
                "ActiveState",
                "-p",
                "StateChangeTimestampMonotonic",
            ],
            check=True,
            timeout=SYSTEMCTL_TIMEOUT_S,
            capture_output=True,
            text=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return jsonify({"state": "unknown"}), 200
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    try:
        changed = int(props.get("StateChangeTimestampMonotonic", "0")) / 1_000_000
    except ValueError:
        changed = 0.0
    active = props.get("ActiveState", "")
    if changed == 0:
        # systemd unloads an inactive oneshot nobody references, and then
        # reports a zero timestamp: the record is gone, not "not started yet"
        # (measured on the bench after a clean run).
        state = "unknown"
    elif changed <= dispatched:
        state = "pending"
    elif active == "failed":
        state = "refused" if _setup_marker_untouched_since(dispatched_wall) else "unknown"
    elif active in ("activating", "active", "deactivating", "reloading"):
        state = "running"
    elif active == "inactive":
        state = "done"
    else:
        state = "unknown"
    return jsonify({"state": state}), 200


def _setup_marker_untouched_since(wall: float) -> bool:
    """True when ``.setup-complete`` predates the dispatch: the reset never
    reached its Step 2 (which removes it) or first-boot never recreated it.
    Missing, or touched since, is not proof of a refusal."""
    try:
        return os.stat(_setup_complete_path(current_app)).st_mtime < wall
    except OSError:
        return False
