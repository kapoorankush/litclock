"""POST /api/system/* — destructive system actions (#245 M4, #280).

Routes:

- GET  /system                       — System tab page (Story 2.1 fills the
                                       template; this commit registers the
                                       route on the blueprint and renders a
                                       milestone placeholder).
- POST /api/system/confirm-token     — mint a per-action single-use token.
- POST /api/system/reboot            — `sudo systemctl reboot --no-block`
                                       after consuming a confirm token.
- POST /api/system/poweroff          — `sudo systemctl poweroff --no-block`
                                       after consuming a confirm token.
- POST /api/system/prepare-for-gift  — write welcome message to
                                       /run/litclock/gift-message, then
                                       `sudo systemctl start --no-block
                                       litclock-prepare-for-gift.service`,
                                       which invokes reset-setup.sh
                                       --gift-mode (wipes WiFi, paints
                                       welcome splash, powers off). #280.

All POST routes share a per-IP token-bucket rate limiter
(5/min, see ``rate_limit.py``) — the cap on the destructive endpoints
can't be bypassed by spamming the cheap issuance endpoint.

Error responses use the project-wide envelope from
``control_server.errors`` (locked by issue #254):
``{"ok": false, "error": {"code": <slug>, "message": <human-readable>,
[extras]}}``, with ``retry_after_s`` extra on 429s.
"""

from __future__ import annotations

import os
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from flask import Blueprint, current_app, jsonify, render_template, request

from .. import update_state
from ..confirm_tokens import VALID_ACTIONS, ConfirmTokenStore, envelope_for_consume_outcome
from ..errors import envelope
from ..rate_limit import RateLimiter

bp = Blueprint("system", __name__)

# Sudoers entry uses /usr/bin/systemctl verbatim — invoking via PATH would
# require sudo's secure_path resolution to land on the same binary. Hardcode
# to keep the audit trail trivial.
SYSTEMCTL: Final[str] = "/usr/bin/systemctl"
SYSTEMCTL_TIMEOUT_S: Final[int] = 5

# #396 — gift-flow system-timezone reset. Absolute path so the scoped
# sudoers entry (sudoers/020_litclock-control: `timedatectl set-timezone UTC`)
# matches verbatim. Today the call also works via the broad 010_pi-nopasswd
# grant; the scoped entry becomes load-bearing once that grant is dropped
# (#387, not yet shipped). UTC is the neutral default; the recipient's
# first-boot IP-geo overwrites it.
TIMEDATECTL: Final[str] = "/usr/bin/timedatectl"
# Own timeout (not SYSTEMCTL_TIMEOUT_S): `timedatectl set-timezone` talks to
# systemd-timedated over D-Bus, a different call shape than systemctl. 5s is
# ample for set-timezone; failure is swallowed + Step 3.5 retries, so this is
# self-healing rather than load-bearing.
TIMEDATECTL_TIMEOUT_S: Final[int] = 5

# #362 — separate, longer timeout for the pre-shutdown stop call. The
# litclock.service unit declares TimeoutStopSec=10s; on Pi Zero-class
# hardware add sudo + D-Bus + transaction-scheduling overhead and a tight
# 12s budget runs out at the worst possible moment (a wedged render).
# 15s leaves 5s slack above litclock.service's SIGKILL ceiling — matches
# the "TimeoutStopSec + safety margin" pattern.
SYSTEMCTL_STOP_TIMEOUT_S: Final[int] = 15

# #362 — units we must stop synchronously BEFORE invoking a destructive
# systemctl action (reboot / poweroff). Stopping the timer prevents future
# minute-tick firings; stopping the service cancels any timer-queued start
# job that hasn't executed yet AND stops an in-flight render. Together
# these close the timer-queued-job race documented in issue #362: a
# literary quote repainting over the "Powered Off" splash because a
# timer-queued litclock.service start landed AFTER our shutdown unit's
# ExecStop ran.
_PRE_SHUTDOWN_STOP_UNITS: Final[tuple[str, ...]] = ("litclock.timer", "litclock.service")

# #362 D7 — process-local "shutdown is about to happen" flag. Set just
# before the pre-stop runs in _execute_action; read by in-process ad-hoc
# worker threads (settings.py's deferred `systemctl start litclock.service`
# poll thread) so they abort instead of re-firing a render past our
# pre-stop. Bool reads are GIL-atomic, but the lock keeps the read+act
# sequence well-defined for any future consumer that wants to do more than
# a single boolean check.
_SHUTDOWN_IMMINENT = False
_SHUTDOWN_IMMINENT_LOCK = threading.Lock()


def _mark_shutdown_imminent() -> None:
    """#362 D7 — signal to in-process ad-hoc workers that a shutdown
    transaction is starting.

    Once set, downstream callers (notably ``settings.py``'s
    ``_fire_ad_hoc_tick_blocking`` daemon thread) must abort before firing
    any new ``systemctl start litclock.service`` calls — otherwise an
    ad-hoc tick spawned seconds before the user tapped Power off can land
    after our pre-stop and re-open the timer-queued-job race the pre-stop
    was meant to close.

    Process-local only. A future cross-process variant (e.g., a sentinel
    file under ``/run/litclock/``) would also cover ``update.sh``'s Phase 7
    re-start, but the cross-process surface is already gated by the D8
    ``update_is_busy()`` check, so D7 sticks to in-process semantics.
    """
    global _SHUTDOWN_IMMINENT
    with _SHUTDOWN_IMMINENT_LOCK:
        _SHUTDOWN_IMMINENT = True


def is_shutdown_imminent() -> bool:
    """#362 D7 — read-only API for ad-hoc worker threads.

    Used by ``control_server.routes.settings._fire_ad_hoc_tick_blocking``
    to skip its deferred ``systemctl start litclock.service`` call once a
    shutdown is in flight. Cheap; lock-guarded read.
    """
    with _SHUTDOWN_IMMINENT_LOCK:
        return _SHUTDOWN_IMMINENT


def _stop_clock_units_for_shutdown() -> None:
    """Stop litclock.timer + litclock.service synchronously, best-effort (#362).

    Closes the #362 race: ``litclock.timer`` fires every minute and enqueues
    a ``litclock.service`` start job. If shutdown lands between enqueue and
    execution, the queued render paints over the "Powered Off" splash —
    visibly wrong for a user who just tapped Power off / Restart.

    Stopping both units BEFORE the shutdown transaction begins removes
    every possible queued start. ``stop`` cancels any pending start job
    AND, if the service is mid-render, waits up to ``TimeoutStopSec=10s``
    for a clean SIGTERM-and-exit before SIGKILL. The 15-second
    ``SYSTEMCTL_STOP_TIMEOUT_S`` here leaves 5s slack above that ceiling.

    enable/disable invariant: this only touches RUNTIME state. ``stop`` is
    not ``disable``. ``litclock.timer`` keeps its ``[Install]
    WantedBy=timers.target`` line. On the next power-on, ``timers.target``
    activates the timer normally → first minute tick → ``litclock.service``
    starts → clock renders. No migration; no boot-state change.

    Failure handling: log + return. If the stop times out or errors, we
    still proceed to the destructive action — the race re-opens for this
    one shutdown (same behavior as today) but the user's Power off /
    Restart request still completes. Denying the destructive action on a
    stop failure would be worse UX than the cosmetic race. Common harmless
    case: timer was already inactive (admin manually stopped it), which
    sudoers / systemctl treats as success anyway.

    Wedged-render budget: ``litclock.service`` declares ``TimeoutStopSec=10s``;
    we allow 15s here. Typical case is <500ms.
    """
    cmd = ["sudo", SYSTEMCTL, "stop", *_PRE_SHUTDOWN_STOP_UNITS]
    try:
        subprocess.run(
            cmd,
            check=True,
            timeout=SYSTEMCTL_STOP_TIMEOUT_S,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (getattr(exc, "stderr", b"") or b"").decode(errors="replace").strip()
        # Best-effort: log + proceed. Race-closure value is lost for this
        # one shutdown, same as today's pre-fix behavior. The destructive
        # action still proceeds — denying the user's Power off because a
        # stop failed would be worse UX than the cosmetic race.
        current_app.logger.warning(
            "pre-shutdown stop returned non-zero (%s): %s",
            getattr(exc, "returncode", "?"),
            stderr,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = (getattr(exc, "stderr", b"") or b"").decode(errors="replace").strip()
        current_app.logger.warning(
            "pre-shutdown stop timed out after %ds: %s",
            SYSTEMCTL_STOP_TIMEOUT_S,
            stderr,
        )
    except OSError as exc:
        # #362 codex final-pass Finding 1: covers FileNotFoundError (sudo
        # binary missing), fork failures (process-table pressure), and
        # any other OS-level exec failure that subprocess.run can raise.
        # Without this catch, the exception propagates out of the caller
        # _mark_shutdown_imminent_and_stop_units() with _SHUTDOWN_IMMINENT
        # already set to True, leaving every future settings ad-hoc tick
        # silently aborted until the process restarts. Log + proceed
        # keeps the same contract as the CalledProcessError/TimeoutExpired
        # branches above.
        current_app.logger.warning(
            "pre-shutdown stop could not exec (%s): %s",
            type(exc).__name__,
            exc,
        )


@contextmanager
def shutdown_imminent_check():
    """Atomic check+act helper for in-process consumers (#362 D7 codex
    post-review TOCTOU fix).

    Holds ``_SHUTDOWN_IMMINENT_LOCK`` for the duration of the with-block so
    any concurrent ``_execute_action`` pre-stop blocks until the caller's
    critical section exits. Yields the current value of
    ``_SHUTDOWN_IMMINENT``. Use:

        with shutdown_imminent_check() as imminent:
            if imminent:
                return  # abort — pre-stop is running or done
            # critical section: subprocess.run, etc., runs atomically
            # against any concurrent shutdown attempt.

    Without this helper, the original D7 shape had a TOCTOU race: an
    ad-hoc tick thread could check ``is_shutdown_imminent()`` → False,
    get preempted, then have ``_execute_action`` set the flag + run
    pre-stop, then resume and fire a fresh ``systemctl start
    litclock.service`` AFTER the pre-stop. Holding the lock across
    check + act closes that window.
    """
    with _SHUTDOWN_IMMINENT_LOCK:
        yield _SHUTDOWN_IMMINENT


def _mark_shutdown_imminent_and_stop_units() -> None:
    """Atomically set ``_SHUTDOWN_IMMINENT`` AND run the pre-stop, under a
    single lock acquisition (#362 D7 codex post-review TOCTOU fix).

    A naive ``_mark_shutdown_imminent()`` + ``_stop_clock_units_for_shutdown()``
    call pair has a race window between the two: a concurrent ad-hoc tick
    that acquired the lock between mark and stop could fire ``systemctl
    start litclock.service`` after we already marked but before we
    stopped. Combining them under one lock acquisition closes that.

    See ``shutdown_imminent_check()`` for the consumer side of this lock.
    """
    global _SHUTDOWN_IMMINENT
    with _SHUTDOWN_IMMINENT_LOCK:
        _SHUTDOWN_IMMINENT = True
        _stop_clock_units_for_shutdown()


def _rollback_failed_shutdown_attempt() -> None:
    """After a destructive ``systemctl`` call fails, restore the clock and
    clear ``_SHUTDOWN_IMMINENT`` so future settings ad-hoc ticks resume
    firing normally (#362 D9 + codex post-review Findings 2/3/4).

    Pre-stop already stopped ``litclock.timer`` + ``.service``. If the
    destructive call returned non-zero (sudoers mismatch, unit missing,
    masked) or timed out pre-dispatch (sudo/D-Bus/systemd wedge), the
    box is staying up and the clock is silently dead until the next
    reboot. Restart the timer best-effort so minute-tick renders resume.

    Returncode check: ``subprocess.run(check=False)`` doesn't raise on
    non-zero exit, so we inspect ``result.returncode`` explicitly and
    log a warning. Without this, a sudoers-rejected rollback would be
    completely silent (codex Finding 3).

    Flag clear: ``_SHUTDOWN_IMMINENT`` stays True from the original mark.
    If we leave it True, every future settings save silently skips its
    instant-tick render via ``shutdown_imminent_check()`` → False
    positive shutdown state. Clearing it lets ad-hoc ticks resume
    (codex Finding 2).

    Called from BOTH the ``CalledProcessError`` and ``TimeoutExpired``
    branches of ``_execute_action`` — codex Finding 4: a timeout doesn't
    prove the destructive dispatched, so we must rollback there too.
    Worst case (destructive DID dispatch on timeout): the rollback fires
    harmlessly during shutdown, systemd cancels the timer restart as
    part of the shutdown transaction.

    Best-effort throughout — failures in the rollback itself are logged
    and swallowed. Rolling back a failed rollback would be unbounded
    recursion.
    """
    try:
        result = subprocess.run(
            ["sudo", SYSTEMCTL, "restart", "litclock.timer"],
            check=False,  # we inspect returncode explicitly below
            timeout=SYSTEMCTL_TIMEOUT_S,
            capture_output=True,
        )
        if result.returncode != 0:
            stderr = (result.stderr or b"").decode(errors="replace").strip()
            current_app.logger.warning(
                "rollback restart litclock.timer returned non-zero (%s): %s",
                result.returncode,
                stderr,
            )
    except Exception as rollback_exc:  # noqa: BLE001 — best-effort rollback
        current_app.logger.warning("rollback restart litclock.timer failed: %s", rollback_exc)

    # Clear the shutdown-imminent flag — destructive didn't succeed (or
    # we can't tell), so the box is staying up. Future settings saves
    # should resume their ad-hoc ticks. If the destructive actually DID
    # dispatch (only possible on TimeoutExpired), the process will be
    # killed by the shutdown transaction before any settings save can
    # fire, so clearing the flag is harmless.
    global _SHUTDOWN_IMMINENT
    with _SHUTDOWN_IMMINENT_LOCK:
        _SHUTDOWN_IMMINENT = False


# #280: prepare-for-gift unit + message-file location. The endpoint writes
# the gifter's welcome text to GIFT_MESSAGE_PATH before invoking systemctl
# start on the unit; the unit's ExecStart passes that path to reset-setup.sh
# --gift-mode --message-file. /run/litclock/ is pi-owned tmpfs (cleared on
# reboot — fine, the welcome message is short-lived by design).
PREPARE_FOR_GIFT_UNIT: Final[str] = "litclock-prepare-for-gift.service"
# Issue #510 — Factory reset. Full-wipe sibling of wifi-reset: the unit runs
# reset-setup.sh --wipe-wifi --reboot --yes (root-owned copy) to wipe config +
# WiFi and reboot into first-boot setup. No message file (unlike gift), so the
# route is the simpler wifi-reset shape.
RESET_UNIT: Final[str] = "litclock-reset.service"
GIFT_MESSAGE_PATH: Final[str] = "/run/litclock/gift-message"
# Same ceiling as M3's GIFT_MODE_MESSAGE_MAX_LEN (#319 dropped 280→80 once
# the e-ink renderer started word-wrapping). Enforced at the endpoint so
# an attacker can't get the privileged shell script to write more than
# this even if the script's own ceiling is bypassed somehow.
GIFT_MESSAGE_MAX_LEN: Final[int] = 80


def _gift_unit_loadable() -> bool:
    """Read-only probe: is the teardown unit installed and not masked — i.e. can
    ``systemctl start`` actually dispatch it?

    Returns True only for ``LoadState=loaded``. Used to bail BEFORE
    prepare_for_gift's destructive location clear: that clear is irreversible
    (it wipes the owner's coordinates) and runs before the point of no return,
    so a missing/masked unit (#327 install gap) must be caught first or the
    owner loses their location for a gift that can't start. ``systemctl show``
    is read-only and needs no sudo (mirrors update_state.update_is_busy's
    is-active probe). Any probe error → False (fail safe: if we can't confirm
    the unit can run, don't wipe location)."""
    try:
        result = subprocess.run(
            [SYSTEMCTL, "show", "-p", "LoadState", "--value", PREPARE_FOR_GIFT_UNIT],
            check=False,
            timeout=SYSTEMCTL_TIMEOUT_S,
            capture_output=True,
            text=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        current_app.logger.warning("prepare_for_gift: LoadState probe failed: %s", exc)
        return False
    return result.stdout.strip() == "loaded"


def _gift_reset_argv() -> list[str]:
    """The exact argv used for the gift-flow tz reset — exposed for the sudoers
    parity test so a path/binary drift is caught in CI. sudo strips argv[0], so
    the command sudoers matches is `TIMEDATECTL set-timezone UTC`."""
    return ["sudo", TIMEDATECTL, "set-timezone", "UTC"]


def _gift_reset_timezone_to_utc() -> None:
    """Best-effort: reset the system timezone to UTC during gift prep (#396).

    Called from prepare_for_gift immediately BEFORE the teardown unit dispatch
    (and after message staging — see the call site for why ordering matters): the
    gifter's tz is gone before any teardown, closing the window while the PWA is
    still up. The tz lives in /etc/localtime (timedatectl), not env.sh, so the
    synchronous location clear doesn't touch it.

    Best-effort by DESIGN, unlike the coords clear (load-bearing → fatal): the
    only residual a stale tz can produce is inspection-only (it never drives a
    wrong-time clock for the recipient — the cleared coords gate quotes until
    their first-boot resolves a tz). Blocking a gift over a timedatectl quirk
    would be backwards on the severity ledger, so failure is logged, not raised.
    reset-setup.sh Step 3.5 also resets tz: it's the reset for the direct-CLI
    path (no control_server), and a backstop here. Note the two share the same
    timedatectl/D-Bus dependency, so a correlated environmental failure can leave
    the gifter's tz in place silently — an accepted residual given the severity."""
    try:
        subprocess.run(  # noqa: S603 — fixed argv, no shell; matches sudoers/020
            _gift_reset_argv(),
            check=True,
            timeout=TIMEDATECTL_TIMEOUT_S,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        # Log stderr (the useful part for sudoers mismatch / timedated failure),
        # not just the exception repr.
        stderr = (getattr(exc, "stderr", b"") or b"").decode(errors="replace").strip()
        current_app.logger.warning("prepare_for_gift: tz reset to UTC failed (non-fatal): %s", stderr or exc)
    except (subprocess.SubprocessError, OSError) as exc:
        current_app.logger.warning("prepare_for_gift: tz reset to UTC failed (non-fatal): %s", exc)


def _store() -> ConfirmTokenStore:
    return current_app.extensions["confirm_tokens"]


def _rate_limiter() -> RateLimiter:
    return current_app.extensions["system_rate_limiter"]


def _client_ip() -> str:
    # X-Forwarded-For is intentionally NOT honored — there's no proxy in
    # front of waitress on the Pi. Trusting the header in the LAN-trust
    # threat model would let a single attacker rotate IPs trivially.
    #
    # remote_addr is None only in pathological test contexts (Werkzeug's
    # test client with explicit None environ). Fall back to id(request) so
    # two such requests don't share a bucket and rate-limit each other out
    # — production behind waitress always has a real IP.
    return request.remote_addr or f"unknown-{id(request)}"


def _check_rate_limit() -> tuple[object, int] | None:
    allowed, retry_after_s = _rate_limiter().take(_client_ip())
    if allowed:
        return None
    body, status = envelope(
        "rate_limited",
        "Too many system actions. Try again shortly.",
        429,
        retry_after_s=retry_after_s,
    )
    # HTTP-level Retry-After header for clients (and any HTTP cache /
    # browser dev tool) that read the standard signal.
    body.headers["Retry-After"] = str(retry_after_s)
    return body, status


def _extract_token() -> str | None:
    """Pull `token` from JSON body OR form-encoded body.

    The PWA's JS path posts JSON; the no-JS form fallback (Story 2.1's
    action-card form submit) posts form-urlencoded. Routes accept both so
    a captive-portal or JS-disabled WebView still gates on the confirm token
    instead of silently bypassing the destructive-action guard.
    """
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        token = body.get("token")
        if isinstance(token, str):
            return token
    form_token = request.form.get("token")
    return form_token if isinstance(form_token, str) and form_token else None


def _execute_action(action: str) -> tuple[object, int]:
    """Validate confirm token, then run ``sudo systemctl <action> --no-block``.

    --no-block lets the HTTP response flush before systemd takes the box
    down — the user sees "reboot started" before the connection drops.
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
    result = _store().consume_classified(action, token)
    if result.outcome != "ok":
        return envelope_for_consume_outcome(result.outcome)
    expiry = result.expiry

    # #362 D8 — refuse cleanly if an auto-update is mid-flight. update.sh's
    # Phase 7 fires `systemctl start litclock.service` + `systemctl start
    # litclock.timer` after the new code is in place; if we pre-stop both
    # units while update.sh is between Phase 3 and Phase 7, the in-flight
    # update can race a fresh start past our pre-stop and re-open the #362
    # race. Mirror the existing /api/wifi/reset and /api/system/prepare-for-
    # gift gate so the user gets a friendly 409 + retry guidance instead of
    # a quietly-broken splash. Pre-side-effect failure — restore the token
    # so the user's retry after the update finishes works with the same
    # open page.
    if update_state.update_is_busy():
        _store().restore(action, token, expiry)
        return envelope(
            "update_in_progress",
            "An update is in progress. Try again in a few minutes.",
            409,
        )

    # #362 D7 + D1 (codex post-review TOCTOU fix) — atomically set the
    # shutdown-imminent flag AND run the pre-stop, under a single lock
    # acquisition. A naive mark-then-stop pair has a race window: a
    # concurrent settings.py ad-hoc tick that acquired the lock between
    # our mark and stop calls could fire `systemctl start litclock.service`
    # AFTER our mark but BEFORE our stop, defeating the race closure.
    # The combined helper holds _SHUTDOWN_IMMINENT_LOCK across both
    # operations; ad-hoc ticks using shutdown_imminent_check() block
    # until pre-stop completes.
    _mark_shutdown_imminent_and_stop_units()

    try:
        subprocess.run(
            ["sudo", SYSTEMCTL, action, "--no-block"],
            check=True,
            timeout=SYSTEMCTL_TIMEOUT_S,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        # Log stderr at ERROR but do NOT leak it in the response body —
        # subprocess output can include host paths, unit names, kernel
        # signals, etc. that aren't useful to the PWA and aren't ours to
        # share with whoever's poking the LAN.
        stderr = getattr(exc, "stderr", b"") or b""
        current_app.logger.error("systemctl %s failed: %s", action, stderr.decode(errors="replace").strip())
        # Issue #328 — pre-side-effect failure: systemctl returned non-zero
        # before dispatching the unit (typical case: unit not found, sudoers
        # misconfigured, unit masked — the M5/M8 #327 missing-unit case is
        # the live motivation). Restore the token at its original expiry so
        # the open page can retry without minting a new one; otherwise the
        # second tap surfaces a misleading "token already used" 401 that
        # hides the underlying systemd error.
        #
        # Review D1 caveat: a non-zero exit from `systemctl --no-block` does
        # NOT strictly prove "no side effect" — there is a narrow theoretical
        # window where systemd accepts the request, queues the job, then a
        # dbus reply glitch causes the wrapper to exit non-zero. In that
        # case, restoring the token lets the user double-fire the action.
        #
        # All current destructive actions reached here (reboot, poweroff)
        # are idempotent at the systemd level — a second reboot/poweroff
        # request while one is already queued is a no-op — so double-fire
        # is harmless today. If a future non-idempotent destructive action
        # is added to this dispatcher, this restore must be narrowed to
        # specific pre-dispatch stderr patterns or removed for that action.
        _store().restore(action, token, expiry)

        # #362 D9 + codex post-review Findings 2/3 — rollback restart-timer
        # AND clear _SHUTDOWN_IMMINENT. The helper inspects returncode (not
        # just exceptions, since check=False) so a sudoers-rejected rollback
        # logs a warning instead of failing silently. See helper docstring
        # for the full rationale.
        _rollback_failed_shutdown_attempt()

        return envelope(
            "systemctl_failed",
            "The system action could not be invoked.",
            500,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = getattr(exc, "stderr", b"") or b""
        current_app.logger.error("systemctl %s timed out: %s", action, stderr.decode(errors="replace").strip())
        # Issue #328 — DON'T restore on timeout. systemctl --no-block
        # returns immediately on success; a timeout here means the call
        # may have dispatched (and we just didn't get the response in
        # time) or it may not have. Paranoid: keep the token consumed so
        # a retry can't double-fire reboot/poweroff/etc. Rare in practice;
        # the user reloads to mint a new token.
        #
        # #362 D9 + codex post-review Finding 4 — DO rollback restart-timer
        # AND clear _SHUTDOWN_IMMINENT on timeout. Codex caught: a timeout
        # doesn't prove the destructive dispatched; it can also be sudo /
        # D-Bus / systemd wedging BEFORE dispatch, in which case the box
        # stays up with timer+service pre-stopped and ad-hoc ticks
        # suppressed forever. If the destructive DID dispatch and we
        # missed the response, the rollback fires harmlessly during
        # shutdown (systemd cancels the timer-start as part of the
        # shutdown transaction). Worst case is a noisy log line; best
        # case is the box recovers to a usable state.
        _rollback_failed_shutdown_attempt()
        return envelope(
            "systemctl_failed",
            "The system action could not be invoked.",
            500,
        )
    except OSError as exc:
        # #362 codex final-pass Finding 1 (symmetric with the pre-stop
        # helper's OSError catch above). Covers FileNotFoundError + fork
        # failures + permission errors at exec time. Pre-stop already
        # fired (or its own OSError was caught and proceeded), so the
        # clock units are stopped; without rollback here, the box stays
        # up with a stuck _SHUTDOWN_IMMINENT flag and silenced ad-hoc
        # ticks until next restart. Treat the same as CalledProcessError:
        # restore token, rollback, return 500.
        current_app.logger.error(
            "systemctl %s could not exec (%s): %s",
            action,
            type(exc).__name__,
            exc,
        )
        _store().restore(action, token, expiry)
        _rollback_failed_shutdown_attempt()
        return envelope(
            "systemctl_failed",
            "The system action could not be invoked.",
            500,
        )

    return jsonify({"ok": True, "action": action}), 200


# ─── Routes ─────────────────────────────────────────────────────────────────


@bp.route("/system")
def system_tab() -> str:
    """GET /system — System tab page (Story 2.1 + #245 M5 Reset-WiFi card
    + #317 item 7 Prepare-for-Gifting move).

    Renders four action cards (Restart, Power off, Reset WiFi, Prepare for
    Gifting) per DESIGN.md "Cards" spec. Each card carries a server-issued
    single-use confirm token in a hidden form field so the no-JS form-POST
    fallback works (Story 2.2's JS upgrades the cards to the sheet-style
    confirm modal).

    Tokens are minted via the store API, which bypasses the rate limiter
    intentionally — page renders shouldn't burn destructive-action budget,
    and the tokens are still single-use + 300s TTL on consume.

    The page is scoped to ``_SYSTEM_TAB_ACTIONS``, NOT VALID_ACTIONS — the
    update_apply token is minted on /updates and shouldn't burn a slot here.

    Also passes a fresh CSRF token + the current ``GIFT_MODE_MESSAGE`` draft
    so the Prepare-for-Gifting card can pre-fill its textarea (#317 item 7).
    Accepts ``?saved=gift`` so the success banner renders after the PRG
    redirect from settings_post (which still owns the section=gift writer).
    """
    saved_section = request.args.get("saved") or None
    if saved_section not in ("gift",):  # Only gift PRG-redirects here today.
        saved_section = None
    return _render_system_tab(saved_section=saved_section)


def _render_system_tab(
    *,
    saved_section: str | None = None,
    field_errors: dict[str, str] | None = None,
    form_error: str | None = None,
    submitted_gift_message: str | None = None,
    status_code: int = 200,
) -> tuple[str, int] | str:
    """Shared render for /system. Used both by GET /system and by the
    section=gift failure path in routes.settings.settings_post — the
    failure path must re-render the System tab (where the card lives)
    with per-field errors, otherwise validation feedback would land on a
    page that no longer shows the gift form (#317 item 7).

    ``submitted_gift_message`` overrides the env.sh-loaded draft so the
    user's rejected input survives the re-render — without it, an
    overlong (>80 char) submission would silently revert to the
    last-saved good value, hiding what the user actually typed and making
    the inline error message meaningless (adversarial /review finding)."""
    # Lazy import — keeps the control_server boot path light. config loads
    # the env.sh on every call; we only need it for the gift-message draft.
    import config as _config  # noqa: PLC0415

    from ..csrf import CSRF_ACTION  # noqa: PLC0415

    store = _store()
    tokens = {action: store.issue(action)[0] for action in _SYSTEM_TAB_ACTIONS}
    env_settings = _config.load_config(current_app.config["ENV_FILE"])
    csrf_token, _ = current_app.extensions["csrf_tokens"].issue(CSRF_ACTION)
    gift_mode_message = (
        submitted_gift_message if submitted_gift_message is not None else env_settings.get("GIFT_MODE_MESSAGE", "")
    )
    body = render_template(
        "system.html.j2",
        active_tab="system",
        tokens=tokens,
        gift_mode_message=gift_mode_message,
        csrf_token=csrf_token,
        saved_section=saved_section,
        field_errors=field_errors or {},
        form_error=form_error,
    )
    if status_code == 200:
        return body
    return body, status_code


# Actions whose UI lives on the System tab. update_apply lives on /updates,
# so it's scoped out here — VALID_ACTIONS additions don't silently burn a
# System-tab token slot. #317 item 7 moved prepare_for_gift here.
_SYSTEM_TAB_ACTIONS: Final[tuple[str, ...]] = (
    "reboot",
    "poweroff",
    "wifi_reset",
    "factory_reset",
    "prepare_for_gift",
)


@bp.route("/api/system/confirm-token", methods=["POST"])
def issue_confirm_token() -> tuple[object, int]:
    """Mint a single-use 60s-TTL token bound to one of {reboot, poweroff}.

    Body: ``{"action": "reboot" | "poweroff"}``. Returns
    ``{"ok": true, "token": "...", "expires_at": <unix-seconds>}`` on success.
    Rate-limited via the same per-IP bucket as /reboot and /poweroff.
    """
    rate_limit_response = _check_rate_limit()
    if rate_limit_response is not None:
        return rate_limit_response

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return envelope(
            "invalid_request",
            "Request body must be a JSON object with an `action` field.",
            400,
        )
    action = body.get("action")
    if not isinstance(action, str) or action not in VALID_ACTIONS:
        return envelope(
            "invalid_action",
            f"`action` must be one of {list(VALID_ACTIONS)}.",
            400,
        )
    token, expires_at = _store().issue(action)
    return jsonify({"ok": True, "token": token, "expires_at": expires_at}), 200


@bp.route("/api/system/reboot", methods=["POST"])
def reboot() -> tuple[object, int]:
    return _execute_action("reboot")


@bp.route("/api/system/poweroff", methods=["POST"])
def poweroff() -> tuple[object, int]:
    return _execute_action("poweroff")


@bp.route("/api/system/prepare-for-gift", methods=["POST"])
def prepare_for_gift() -> tuple[object, int]:
    """Trigger the Prepare-for-Gifting flow (#280).

    1. Rate-limit (shared 5/min bucket).
    2. Consume confirm token (action='prepare_for_gift', single-use, 300s).
    3. Validate optional `message` field (str, ≤GIFT_MESSAGE_MAX_LEN chars). Empty/missing =
       use the default welcome ("Welcome to LitClock" — handled by
       shutdown-splash.sh when .welcome-message is absent).
    4. Atomically write the message bytes to /run/litclock/gift-message.
       (The unit's ExecStart hands this path to reset-setup.sh via
       --message-file. Writing here, not on the command line, keeps the
       message out of the process list / journal and avoids quoting hazards
       across the sudo boundary.)
    5. `sudo systemctl start --no-block litclock-prepare-for-gift.service`,
       which executes reset-setup.sh --gift-mode (wipes WiFi, writes the
       welcome-mode marker + .welcome-message, powers off).

    Same blast-radius shape as /api/wifi/reset: destructive, one-way,
    confirm-token gated, rate-limited. The user is told the device is
    powering off — handoff message in the response lets the PWA render
    "Pack and ship" copy before the LAN connection drops.
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
    result = _store().consume_classified("prepare_for_gift", token)
    if result.outcome != "ok":
        return envelope_for_consume_outcome(result.outcome)
    expiry = result.expiry

    # #316 /review CRITICAL fix — update-busy pre-check (parity with
    # /api/wifi/reset). The litclock-prepare-for-gift.service unit
    # declares Conflicts=litclock-update.service, which is bidirectional:
    # if the weekly update timer fires mid-prepare, systemd SIGTERMs the
    # gift-prep script and leaves the device in partial state (.welcome-mode
    # written but env.sh not yet wiped, WiFi partially gone, NO poweroff).
    # Pre-checking gives the user a friendly 409 with retry guidance
    # instead of an opaque post-hoc systemd refusal.

    if update_state.update_is_busy():
        # Issue #328 — pre-side-effect failure path: no message file written,
        # no systemctl dispatched. Restore the token so the user's retry
        # after the update finishes works with the same open page.
        _store().restore("prepare_for_gift", token, expiry)
        return envelope(
            "update_in_progress",
            "An update is in progress. Try again in a few minutes.",
            409,
        )

    # Extract + validate message. JSON body's `message` field is canonical;
    # the no-JS form fallback (settings.html.j2 form POST) sets it too.
    message = ""
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        raw_message = body.get("message")
        if isinstance(raw_message, str):
            message = raw_message
    elif request.form.get("message") is not None:
        message = request.form.get("message", "")

    # #316 /review CRITICAL fix — content validation parity with the
    # persisted-draft path. POST /settings (gift section) routes through
    # config.validate_setting → _validate_gift_mode_message, which rejects
    # backticks, `$`, newlines, NUL, and overlength. This API path was
    # only checking length, accepting payloads the project-wide free-form
    # validator forbids. Re-use the same validator so both writers share
    # a single contract on what's allowed inside GIFT_MODE_MESSAGE.
    from config import validate_setting  # noqa: PLC0415 — lazy import keeps

    # the control_server boot path light; this endpoint is rarely hit.
    ok, validation_error = validate_setting("GIFT_MODE_MESSAGE", message)
    if not ok:
        # Issue #328 — pre-side-effect failure: validator rejected the
        # message before any file was staged or systemctl dispatched.
        # Restore the token so the user can fix the message and retry
        # with the same open page (otherwise the next attempt 401s with
        # "token already used" and hides the validation error they were
        # about to fix).
        _store().restore("prepare_for_gift", token, expiry)
        return envelope(
            "invalid_message",
            f"Message {validation_error}.",
            400,
        )

    # #393/#327 — pre-flight the teardown unit BEFORE the destructive location
    # clear below. The clear is irreversible and runs before the point of no
    # return; if the unit turns out to be missing/masked (the #327 install-gap
    # case) we'd have wiped the owner's location for a gift that can never start.
    # A read-only LoadState probe lets us bail here with ZERO side effects (token
    # restored for a clean retry once the unit is installed). The narrow TOCTOU
    # window — unit masked between this probe and the start below — is handled by
    # the dispatch-failure branch's honest "location was reset" message.
    if not _gift_unit_loadable():
        _store().restore("prepare_for_gift", token, expiry)
        current_app.logger.error(
            "prepare_for_gift: unit %s not dispatchable — aborting before location clear",
            PREPARE_FOR_GIFT_UNIT,
        )
        return envelope(
            "prepare_for_gift_failed",
            "Gift preparation is unavailable on this device — the gift service isn't installed. Nothing was changed.",
            500,
        )

    # #393 — clear the gifter's location from env.sh SYNCHRONOUSLY, here, before
    # the point of no return. The teardown unit (reset-setup.sh --gift-mode)
    # stops litclock-control.service in its first step, so once it's dispatched
    # the PWA has no server to report back to. A glued-in (cased) Pi gives the
    # operator no physical poweroff cue and the e-ink already shows the welcome
    # splash, so a silent env-wipe failure *inside* the script would ship a
    # device still carrying the gifter's coordinates — which PR2's handoff treats
    # as "timezone known" (the WEATHER_LATITUDE proxy) and starts quotes at the
    # gifter's old time for the recipient. Doing the wipe here means a flock
    # timeout / write error surfaces as a normal error response while the LAN is
    # still up, AND the location is gone before any teardown, so the leak is
    # prevented regardless of what the script does next. The script's own wipe
    # stays as belt-and-suspenders for the direct-CLI path (and clears the
    # non-allowlisted OPENWEATHERMAP_APIKEY, which we can't touch here).
    from config import atomic_update as _clear_location  # noqa: PLC0415

    try:
        _clear_location(
            {"WEATHER_LATITUDE": "", "WEATHER_LONGITUDE": "", "WEATHER_LOCATION_NAME": ""},
            current_app.config["ENV_FILE"],
        )
    except FileNotFoundError:
        # No env.sh → no location stored → nothing to leak. Proceed; the
        # script's `[[ -f env.sh ]]` guard mirrors this no-op.
        pass
    except TimeoutError:
        # env.sh sidecar flock held by another writer past the wait budget.
        # Pre-side-effect failure: restore the token so the same open page can
        # retry once the contending writer releases, and surface 504 — the same
        # env_lock_timeout contract the Settings save uses.
        _store().restore("prepare_for_gift", token, expiry)
        current_app.logger.warning("prepare_for_gift: env.sh lock timeout while clearing location")
        return envelope(
            "env_lock_timeout",
            "Settings file is busy — another update is in progress. Try again in a few seconds.",
            504,
        )
    except (OSError, ValueError) as exc:
        # Write/validation failure before any teardown. Restore the token and
        # report so the operator never unknowingly ships a stale-location device.
        _store().restore("prepare_for_gift", token, expiry)
        current_app.logger.error("prepare_for_gift: failed to clear location from env.sh: %s", exc)
        return envelope(
            "prepare_for_gift_failed",
            "Could not clear your location from the device. Nothing was changed — try again.",
            500,
        )

    # Atomic write into the pi-owned tmpfs so a partial write can't leak a
    # half-written message into the script. Use NamedTemporaryFile so
    # concurrent requests don't race on a shared tmp_path (#316 /review):
    # without a unique inode, thread B's `open(...,'w')` truncates thread
    # A's pending bytes, then both os.replace calls happen and one of them
    # raises ENOENT. O_NOFOLLOW on the tmp file too — defense against a
    # pi-process pre-placing a symlink at the predictable tmp path.
    msg_dir = Path(GIFT_MESSAGE_PATH).parent
    msg_dir.mkdir(parents=True, exist_ok=True)
    tmp_fd = None
    tmp_path = None
    try:
        import tempfile  # noqa: PLC0415

        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=".gift-message.",
            dir=str(msg_dir),
        )
        os.write(tmp_fd, message.encode("utf-8"))
        os.close(tmp_fd)
        tmp_fd = None
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, GIFT_MESSAGE_PATH)
        tmp_path = None  # ownership transferred; don't unlink on cleanup
    except OSError as exc:
        current_app.logger.error("prepare_for_gift: failed to write %s: %s", GIFT_MESSAGE_PATH, exc)
        # Clean up the orphan tmp file so /run/litclock doesn't accumulate
        # half-staged messages across failed requests.
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        # Issue #328 — pre-side-effect failure: the OSError happened before
        # the message landed at GIFT_MESSAGE_PATH (the os.replace either
        # never ran or itself failed atomically), so no systemctl was
        # dispatched. Restore the token so the user can retry without
        # re-opening the page.
        _store().restore("prepare_for_gift", token, expiry)
        return envelope(
            "prepare_for_gift_failed",
            "Could not stage the gift message. Try again.",
            500,
        )

    # #396 — reset the system tz to UTC, synchronously, so the gifter's tz
    # doesn't linger in /etc/localtime. Placed HERE (after staging, immediately
    # before dispatch) on purpose: an earlier placement would mutate /etc/localtime
    # before the staging step, so a staging failure (which aborts the gift and
    # leaves the owner holding the device) would silently put their clock on UTC —
    # a wrong-time clock, the exact failure this feature exists to prevent (Codex
    # /review). By the point of dispatch, the location was already cleared, so tz
    # and coords mutate as one committed step. Best-effort (non-fatal): a stale tz
    # can't drive a wrong-time clock for the recipient (cleared coords gate quotes
    # until first-boot resolves a tz), so a timedatectl quirk must not abort the
    # gift. See _gift_reset_timezone_to_utc.
    _gift_reset_timezone_to_utc()

    try:
        subprocess.run(
            ["sudo", SYSTEMCTL, "start", "--no-block", PREPARE_FOR_GIFT_UNIT],
            check=True,
            timeout=SYSTEMCTL_TIMEOUT_S,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = getattr(exc, "stderr", b"") or b""
        current_app.logger.error(
            "systemctl start %s failed: %s",
            PREPARE_FOR_GIFT_UNIT,
            stderr.decode(errors="replace").strip(),
        )
        # Issue #328 — restoring is SAFE despite the message file being
        # already staged at GIFT_MESSAGE_PATH: the staging path lives in
        # /run/litclock (tmpfs — cleared on reboot, so it can't accumulate
        # across boots) and the systemd unit ExecStart reads the file only
        # when it actually fires. A retry overwrites the staging file
        # atomically via os.replace (idempotent re-stage). So the failure
        # mode is purely "user sees real error, fixes, retries cleanly."
        # The #327-style missing-unit / masked-unit case (pi-gen install
        # gap that dropped litclock-prepare-for-gift.service) is the live
        # motivation here.
        #
        # Review D1 caveat: a non-zero exit from `systemctl --no-block`
        # does NOT strictly prove "no side effect" — narrow theoretical
        # window where systemd queues the job then a dbus reply glitch
        # causes the wrapper to exit non-zero. In that case, restoring the
        # token lets the user double-fire prepare_for_gift. The unit's
        # ExecStart is reset-setup.sh --gift-mode, which is idempotent
        # (wipes WiFi connections by UUID — re-runs are no-ops; writes
        # .welcome-mode marker idempotently; powers off — also idempotent
        # in the "already shutting down" sense). So double-fire today is
        # harmless. If a future non-idempotent step is added to the gift
        # unit's ExecStart, this restore must be narrowed to specific
        # pre-dispatch stderr patterns or removed.
        _store().restore("prepare_for_gift", token, expiry)
        # #342 I9 — unlink the staged message file ONLY when the systemctl
        # returncode proves the unit did not dispatch. Per systemctl(1):
        #   4 = unit not found (the #327 install-gap motivation)
        #   5 = unit not loaded / masked
        # Other non-zero codes (1=catch-all, etc.) can occur when systemd
        # accepted the request but a downstream signal (dbus reply glitch,
        # rare race) caused the wrapper to exit non-zero — in that case the
        # unit's ExecStart will eventually read GIFT_MESSAGE_PATH and
        # render the user's welcome message. Unlinking in that branch
        # would replace the personalized message with the default
        # "Welcome to LitClock" splash — user-visible data loss.
        # Narrow to returncodes 4 and 5 (codex adversarial /review F3).
        unit_didnt_start = getattr(exc, "returncode", None) in (4, 5)
        if unit_didnt_start:
            try:
                os.unlink(GIFT_MESSAGE_PATH)
            except OSError:
                pass
        # #393/#396 — when the unit definitively didn't start (4/5, a TOCTOU after
        # the LoadState pre-flight passed), the location clear AND the tz reset to
        # UTC already ran, so be honest that both were reset rather than implying
        # the device is untouched. Other returncodes (dbus glitch) may mean the
        # unit DID dispatch, so keep the neutral copy there.
        return envelope(
            "prepare_for_gift_failed",
            (
                "Gift prep couldn't start, and your saved location and timezone were reset — retry, or "
                "re-add your city and timezone in Settings if you're keeping this device."
                if unit_didnt_start
                else "The gift preparation could not be started."
            ),
            500,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = getattr(exc, "stderr", b"") or b""
        current_app.logger.error(
            "systemctl start %s timed out: %s",
            PREPARE_FOR_GIFT_UNIT,
            stderr.decode(errors="replace").strip(),
        )
        # Issue #328 — DON'T restore on timeout. systemctl start --no-block
        # returns immediately on success; a timeout means the unit may
        # have actually dispatched (and we just missed the response).
        # Paranoid: keep the token consumed so we don't double-trigger
        # a gift-mode wipe.
        return envelope(
            "prepare_for_gift_failed",
            "The gift preparation could not be started.",
            500,
        )

    return (
        jsonify(
            {
                "ok": True,
                "message": (
                    "Preparing clock for gifting. The screen will paint the welcome "
                    "message, then power off. Pack and ship the device."
                ),
            }
        ),
        200,
    )


@bp.route("/api/system/reset", methods=["POST"])
def reset() -> tuple[object, int]:
    """Trigger the Factory reset flow (#510) — full-wipe sibling of /api/wifi/reset.

    Order (identical gates to wifi_reset / prepare_for_gift):
    1. Rate-limit (shared 5/min bucket).
    2. Confirm token (action='factory_reset', single-use, 300s TTL).
    3. Update-busy gate — 409 update_in_progress if a weekly fire or PWA-triggered
       apply is running. The unit's Conflicts=litclock-update.service is the
       deterministic backstop; pre-checking gives a human-readable 409.
    4. `sudo systemctl start --no-block litclock-reset.service`, whose ExecStart is
       reset-setup.sh --wipe-wifi --reboot --yes (wipes config + WiFi, reboots into
       first-boot setup). No LoadState pre-probe (unlike gift): there's no
       irreversible pre-dispatch side effect to guard — the unit does everything.

    On success: 200 with a "rebooting into setup" message so the PWA can render
    handoff copy before the LAN connection drops.
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
    result = _store().consume_classified("factory_reset", token)
    if result.outcome != "ok":
        return envelope_for_consume_outcome(result.outcome)
    expiry = result.expiry

    if update_state.update_is_busy():
        # Issue #328 pattern — pre-side-effect failure: no systemctl dispatched.
        # Restore the token so retry after the update finishes works on the same page.
        _store().restore("factory_reset", token, expiry)
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
        # Restore the token so retry surfaces the real error (the #327 masked/missing
        # unit case). Safe against double-fire: reset-setup.sh --wipe-wifi --reboot is
        # idempotent (env.sh rewrite to defaults, WiFi delete by UUID, marker removal
        # all no-op on a second run) — mirrors the wifi-reset restore rationale. If a
        # future non-idempotent step lands in the reset ExecStart, narrow or remove this.
        _store().restore("factory_reset", token, expiry)
        return envelope(
            "factory_reset_failed",
            "The factory reset could not be started.",
            500,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = getattr(exc, "stderr", b"") or b""
        current_app.logger.error(
            "systemctl start %s timed out: %s",
            RESET_UNIT,
            stderr.decode(errors="replace").strip(),
        )
        # Issue #328 — DON'T restore on timeout: systemctl --no-block returns at once
        # on success, so a timeout means the unit may have dispatched. Keep the token
        # consumed to avoid double-firing the wipe.
        return envelope(
            "factory_reset_failed",
            "The factory reset could not be started.",
            500,
        )

    return (
        jsonify(
            {
                "ok": True,
                "message": (
                    "Factory reset started. The clock will wipe its settings and reboot "
                    "into setup — connect your phone to the LitClock-Setup hotspot."
                ),
            }
        ),
        200,
    )
