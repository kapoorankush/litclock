"""GET /api/update/check, POST /api/update/apply, GET /api/update/status (#245 M5).

The Updates tab calls all three. The /api/update/check route returns the
6h-cached "is there a newer release" answer. /api/update/apply triggers
``sudo systemctl start --no-block litclock-update.service`` (D1 — systemd's
job queue serializes against the existing Sunday 03:00 + 7d-jitter timer).
/api/update/status mirrors the JSON written by update.sh's _write_status_json
helper at /run/litclock/update.status (D2/D9), or returns ``{state:'idle'}``
when no update has run yet.

Error envelopes follow the project-wide #254 contract (control_server.errors).
Two new slugs land here: ``update_in_progress`` (409 — applies during a busy
unit) and ``already_up_to_date`` (409 — applies when cache says we're current).
Both are deterministic via D10's threading.Lock around the gate-check + start
(F10 in plan).

Routes:
- GET /updates                       — Updates tab page (server-rendered card)
- GET /api/update/check              — release-tag + changelog (6h-cached)
- POST /api/update/apply             — kicks off litclock-update.service
- GET /api/update/status             — current phase or terminal state
"""

from __future__ import annotations

import subprocess
import threading
from typing import Final

from flask import Blueprint, current_app, jsonify, render_template, request

from .. import update_state
from ..confirm_tokens import ConfirmTokenStore, envelope_for_consume_outcome
from ..errors import envelope
from ..rate_limit import RateLimiter
from ..version import get_version

bp = Blueprint("updates", __name__)

SYSTEMCTL: Final[str] = update_state.SYSTEMCTL_BIN
SYSTEMCTL_TIMEOUT_S: Final[int] = 5

# F10 — gate-check + systemctl start are not atomic. Two simultaneous POSTs
# could each call update_is_busy() (both see "no") and each kick `systemctl
# start --no-block`. systemd would deduplicate the job (Type=oneshot), so
# nothing actually runs twice — but the SECOND caller would see a 202 here
# instead of the 409 D10 promises. Module-level Lock makes the gate +
# dispatch deterministic across all worker threads.
_apply_lock = threading.Lock()


# ─── Helpers (mirroring routes/system.py patterns) ─────────────────────────


def _store() -> ConfirmTokenStore:
    return current_app.extensions["confirm_tokens"]


def _rate_limiter() -> RateLimiter:
    return current_app.extensions["system_rate_limiter"]


def _client_ip() -> str:
    # Same constraint as routes/system.py — X-Forwarded-For ignored under
    # the LAN-trust threat model.
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


# ─── /updates HTML page ────────────────────────────────────────────────────


@bp.route("/updates")
def updates_tab() -> str:
    """Server-render the Updates tab card.

    Reads the current version + cache (best-effort) so the no-JS path
    sees the up-to-date pill state on first paint. JS overlays the
    phase reading-list when /api/update/status reports state=running.
    """
    store = _store()
    token = store.issue("update_apply")[0]
    current_version = get_version(current_app.config.get("VERSION_OVERRIDE"))
    cached = update_state.read_cache()
    # M7 design-review F-004 — server-render the relative time on the
    # `Last checked` row so no-JS users see "12 minutes ago" instead of a
    # raw Unix epoch like `1777673436`. The original M5 design comment
    # referred to a JS replacement that was never wired up; rendering on
    # the server side is the cleaner approach (one path, no flicker).
    #
    # Guard against bad cache values: read_cache() only validates the
    # shape is dict; a corrupted SD card or future-bug write could leave
    # NaN, Infinity, or out-of-range numbers on disk. isinstance() is
    # True for those, but datetime.fromtimestamp() raises ValueError on
    # NaN and OverflowError on values outside [year 1, year 9999]. Catch
    # both to avoid 500-ing /updates on disk corruption.
    if cached and isinstance(cached.get("fetched_at_unix"), (int, float)):
        from datetime import UTC, datetime  # noqa: PLC0415

        from .status import _format_relative  # noqa: PLC0415

        try:
            ts_iso = datetime.fromtimestamp(cached["fetched_at_unix"], tz=UTC).isoformat()
        except (ValueError, OverflowError, OSError):
            cached = {**cached, "fetched_at_relative": None}
        else:
            cached = {**cached, "fetched_at_relative": _format_relative(ts_iso, _time_now())}
    return render_template(
        "updates.html.j2",
        active_tab="updates",
        token=token,
        current_version=current_version,
        cached_check=cached,
    )


def _time_now() -> float:
    """Indirection so tests can monkeypatch the wall clock."""
    import time as _time  # noqa: PLC0415

    return _time.time()


# ─── /api/update/check ─────────────────────────────────────────────────────


@bp.route("/api/update/check", methods=["GET"])
def check() -> tuple[object, int]:
    """Cached release-tag resolver + changelog (D6, D13, F11, F13).

    Reads the cache; if fresh, returns it. Otherwise refetches via the
    GH /tags endpoint and the raw.githubusercontent.com CHANGELOG path,
    writes the result, and returns it.

    Never 5xx on network failure — a missing tag with available=null is
    a graceful-degraded response the PWA renders as "couldn't check".
    """
    current_version = get_version(current_app.config.get("VERSION_OVERRIDE"))
    cached = update_state.read_cache()
    if cached and update_state.cache_is_fresh(cached):
        # Re-stamp current_version in case it changed since last write
        # (post-update, pre-cache-invalidation window).
        cached["current_version"] = current_version
        return jsonify({"ok": True, **cached}), 200

    payload = update_state.build_check_payload(current_version)
    update_state.write_cache(payload)
    return jsonify({"ok": True, **payload}), 200


# ─── /api/update/apply ─────────────────────────────────────────────────────


@bp.route("/api/update/apply", methods=["POST"])
def apply_update() -> tuple[object, int]:
    """Kick off the update via systemd's job queue.

    Order of checks (any one failing aborts):
    1. Rate-limit (shared 5/min bucket with reboot/poweroff/wifi-reset).
    2. Confirm token (action='update_apply', single-use, 300s TTL).
    3. Busy gate under _apply_lock (F10): if litclock-update.service is
       active OR queued, return 409 update_in_progress.
    4. Already-up-to-date gate: if the cache (or fresh fetch) says we
       have no available update, return 409 already_up_to_date.
    5. Dispatch via `sudo systemctl start --no-block litclock-update.service`.

    On success: 202 Accepted with started_at_unix so the PWA can render
    the phase reading-list timer.
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
    result = _store().consume_classified("update_apply", token)
    if result.outcome != "ok":
        return envelope_for_consume_outcome(result.outcome)
    expiry = result.expiry

    current_version = get_version(current_app.config.get("VERSION_OVERRIDE"))

    with _apply_lock:
        # Busy gate (D5/F7): is-active OR has a queued job.
        if update_state.update_is_busy():
            # Issue #328 — pre-side-effect failure: no systemctl dispatched.
            # Restore the token so two PWA tabs racing on the same token
            # can sequentially succeed (tab A wins, tab B sees 409, then
            # tab B can retry with the same token once tab A's run completes).
            _store().restore("update_apply", token, expiry)
            return envelope(
                "update_in_progress",
                "An update is already running. Watch progress on the Updates tab.",
                409,
            )

        # Already-up-to-date gate (D10).
        cached = update_state.read_cache()
        if cached and update_state.cache_is_fresh(cached):
            available = cached.get("available")
            if available is False:
                # Issue #328 — pre-side-effect failure: no dispatch happened.
                # Restore so the user can re-tap after a new release lands
                # without a page reload.
                _store().restore("update_apply", token, expiry)
                return envelope(
                    "already_up_to_date",
                    f"You're already on {current_version} — nothing to do.",
                    409,
                    current_version=current_version,
                )
        # No fresh cache → fetch on the fly so a no-JS form POST doesn't
        # blindly fire systemd. Network failure here is treated as
        # "permission to try" — the worst case is update.sh exits cleanly
        # with the graceful-offline path.
        else:
            payload = update_state.build_check_payload(current_version)
            update_state.write_cache(payload)
            if payload.get("available") is False:
                # Issue #328 — pre-side-effect failure: same as the cached
                # branch above. Restore the token.
                _store().restore("update_apply", token, expiry)
                return envelope(
                    "already_up_to_date",
                    f"You're already on {current_version} — nothing to do.",
                    409,
                    current_version=current_version,
                )

        # Dispatch. --no-block so the HTTP response flushes before
        # systemd's job activation latency.
        try:
            subprocess.run(
                [
                    "sudo",
                    SYSTEMCTL,
                    "start",
                    "--no-block",
                    update_state.UPDATE_UNIT,
                ],
                check=True,
                timeout=SYSTEMCTL_TIMEOUT_S,
                capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            stderr = getattr(exc, "stderr", b"") or b""
            current_app.logger.error(
                "systemctl start litclock-update.service failed: %s",
                stderr.decode(errors="replace").strip(),
            )
            # Issue #328 — pre-side-effect failure: systemctl returned non-zero
            # BEFORE the unit started, so the box is still up. Restore the
            # token so the user's retry surfaces the underlying error
            # instead of a spurious "token already used" 401. The #327-style
            # missing-unit / sudoers-misconfig case is the live motivation.
            #
            # Review D1 caveat: a non-zero exit from `systemctl --no-block`
            # does NOT strictly prove "no side effect" — narrow theoretical
            # window where systemd queues the job then a dbus reply glitch
            # causes the wrapper to exit non-zero. In that case, restoring
            # the token lets the user double-fire update_apply. The
            # litclock-update.service unit is Type=oneshot with systemd-level
            # job-queue deduplication: a second `systemctl start` while a
            # run is queued or active is coalesced into the in-flight run
            # (and the explicit _apply_lock + update_is_busy() gate above
            # belts-and-braces against re-entry). So double-fire today is
            # harmless. If a future change makes the update flow
            # non-idempotent at the systemd level, this restore must be
            # narrowed to specific pre-dispatch stderr patterns or removed.
            _store().restore("update_apply", token, expiry)
            return envelope(
                "update_dispatch_failed",
                "The update could not be started.",
                500,
            )
        except subprocess.TimeoutExpired as exc:
            stderr = getattr(exc, "stderr", b"") or b""
            current_app.logger.error(
                "systemctl start litclock-update.service timed out: %s",
                stderr.decode(errors="replace").strip(),
            )
            # Issue #328 — DON'T restore on timeout. systemctl --no-block
            # returns immediately on success; a timeout means the unit may
            # have actually dispatched. Paranoid: keep the token consumed
            # so we don't double-fire the update flow.
            return envelope(
                "update_dispatch_failed",
                "The update could not be started.",
                500,
            )

    # Lock released; response flushes outside the critical section.
    import time as _time

    return jsonify({"ok": True, "started_at_unix": int(_time.time())}), 202


# ─── /api/update/status ────────────────────────────────────────────────────


@bp.route("/api/update/status", methods=["GET"])
def status() -> tuple[object, int]:
    """Mirror /run/litclock/update.status JSON to the PWA (D2/D9).

    Always 200. Caller branches on the ``state`` field:
        idle                — no update has ever run; render Updates tab card
        running             — render the phase reading-list (phase_index 1..7)
        complete            — version-mismatch reload via A8
        failed_reverted     — render "rolled back, clock fine" copy
        failed_unrecovered  — render "manual recovery needed" copy
        stale               — file existed but couldn't be parsed
    """
    payload = update_state.read_status_file()
    return jsonify({"ok": True, **payload}), 200
