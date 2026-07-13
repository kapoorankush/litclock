"""Post-WiFi PWA handoff phase (EPIC #383 PR2, issue #388).

After first-boot provisioning completes, the e-ink display shows a "Setup
complete — scan to fine-tune" splash with the PWA QR before quotes start.
control_server (not setup_server) owns this phase: by the time the user scans
the QR, setup_server is gone and control_server is the server the QR points at.

Lifecycle (locked plan A4/A6, design-review A2):

    setup_server writes /etc/litclock/.setup-complete  → litclock-control starts
    control_server first launch (this module's kickoff):
        - .setup-complete exists AND .handoff-complete missing → handoff active
        - render handoff splash to e-ink (settings summary + IP-based QR)
        - serve the PWA with the handoff banner over the Status tab
        - start a HANDOFF_TIMEOUT_S background timer
    .handoff-complete is written by ANY of (all converge, idempotent):
        - POST /api/handoff/done            (explicit "Done" tap, success state)
        - POST /api/handoff/set-timezone    (browser-tz fallback, failure state)
        - a successful PWA Settings save during the handoff phase (implicit)
        - the background timer (auto)
    litclock.service is gated ConditionPathExists=/etc/litclock/.handoff-complete
    → first quote overwrites the splash on the next minute tick.

Critical correctness gate (design-review A2, revised): a clock that paints
quotes at the WRONG TIME is worse than no clock. So .handoff-complete is NOT
written automatically (timer / implicit save) until the timezone is known.
"Timezone known" is proxied by ``WEATHER_LATITUDE`` being set in env.sh — the
IP-geo resolver writes lat/lon and sets the system tz together (or not at all),
so a populated latitude means the resolver succeeded and the tz is correct. The
browser-tz fallback (set-timezone endpoint) sets the tz explicitly and is the
one path that may complete the handoff with latitude still empty.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

from control_url import control_base_url  # single source of truth for the PWA URL (#343)

log = logging.getLogger(__name__)

# Wall-clock ceiling for the short-lived splash painter subprocess (#388). The
# paint is ~5s on a Pi Zero 2W; 20s covers a slow/contended paint while bounding
# how long a WEDGED display can stall control_server startup (kickoff runs the
# paint synchronously before serve()). Referenced in the timeout log so the two
# can't drift.
_SPLASH_PAINT_TIMEOUT_S = 20

# Marker paths. Overridable via env so tests point at a tmp dir (and so a
# direct write succeeds there without sudo). Mirror the LITCLOCK_*_FILE config
# pattern in __init__.py; create_app() copies these into app.config so a test
# fixture can override per-app.
SETUP_COMPLETE_FILE_DEFAULT = os.environ.get("LITCLOCK_SETUP_COMPLETE_FILE", "/etc/litclock/.setup-complete")
HANDOFF_COMPLETE_FILE_DEFAULT = os.environ.get("LITCLOCK_HANDOFF_COMPLETE_FILE", "/etc/litclock/.handoff-complete")

# Seconds the splash stays up before the timer auto-completes the handoff.
# 120s per locked plan. Overridable so tests don't actually sleep two minutes.
HANDOFF_TIMEOUT_S_DEFAULT = float(os.environ.get("LITCLOCK_HANDOFF_TIMEOUT_S", "120"))

# Hardcoded so the sudo fallback below matches sudoers/020_litclock-control
# verbatim (sudoers matches binary path + args). Bookworm: /usr/bin/touch.
_TOUCH = "/usr/bin/touch"
_SUDO_TIMEOUT_S = 5


# ---------- path / env accessors ----------


def _setup_complete_path(app) -> str:
    return app.config.get("SETUP_COMPLETE_FILE", SETUP_COMPLETE_FILE_DEFAULT)


def _handoff_complete_path(app) -> str:
    return app.config.get("HANDOFF_COMPLETE_FILE", HANDOFF_COMPLETE_FILE_DEFAULT)


def _timeout_seconds(app) -> float:
    return float(app.config.get("HANDOFF_TIMEOUT_S", HANDOFF_TIMEOUT_S_DEFAULT))


def _load_env(app) -> dict[str, str]:
    """Read env.sh into a dict. Lazy import keeps create_app light and avoids
    a module-level dependency on the sibling src/ layout."""
    import config as _config  # noqa: PLC0415

    return _config.load_config(app.config.get("ENV_FILE", _config.ENV_FILE_DEFAULT))


# ---------- state predicates ----------


def is_handoff_active(app) -> bool:
    """True iff setup has completed but the handoff hasn't. This is the gate
    that turns the PWA banner on and suppresses the AtHS hint."""
    return os.path.exists(_setup_complete_path(app)) and not os.path.exists(_handoff_complete_path(app))


def _has_location(env: dict[str, str]) -> bool:
    """True iff env.sh carries usable coordinates. Mirrors literary_clock.py's
    ``elif location_lat and location_long`` weather gate so "location set" means
    the same thing on both sides of the handoff."""
    return bool(env.get("WEATHER_LATITUDE", "").strip()) and bool(env.get("WEATHER_LONGITUDE", "").strip())


def timezone_known(app) -> bool:
    """Proxy for "the system timezone is correct." The IP-geo resolver sets the
    tz and writes lat/lon together, so a populated latitude means the tz was
    resolved. Used to block auto/implicit handoff completion (design-review A2:
    never start a wrong-time clock)."""
    return _has_location(_load_env(app))


# ---------- completion ----------


def mark_handoff_complete(app) -> bool:
    """Idempotently create the .handoff-complete marker. Returns True iff the
    marker exists afterward.

    Tries a direct write first — that succeeds in tests (tmp path) and anywhere
    the parent dir is writable. The production path lives in root-owned
    /etc/litclock, so the direct write raises PermissionError there and we fall
    back to ``sudo touch`` (scoped in sudoers/020_litclock-control). Idempotent:
    an existing marker short-circuits to success, so the three concurrent
    completion triggers can't race destructively."""
    path = _handoff_complete_path(app)
    if os.path.exists(path):
        return True
    try:
        Path(path).touch()
        return True
    except OSError:
        # PermissionError (root-owned /etc/litclock) is the expected production
        # path; FileNotFoundError (missing dir) falls through to sudo too and
        # then to the warning if that also fails.
        pass
    try:
        # sudo because /etc/litclock is root-owned and control_server runs as
        # pi. Fixed argv, no shell; matches sudoers/020_litclock-control which
        # authorizes exactly `/usr/bin/touch /etc/litclock/.handoff-complete`.
        subprocess.run(["sudo", _TOUCH, path], check=True, timeout=_SUDO_TIMEOUT_S)  # noqa: S603,S607
    except (subprocess.SubprocessError, OSError) as exc:
        # Direct + sudo both failed. Don't crash the request/timer — the
        # litclock-handoff-fallback.timer is the last-resort writer.
        log.warning("could not write handoff marker %s: %s", path, exc)
        return False
    return os.path.exists(path)


def complete_if_timezone_known(app) -> bool:
    """Complete the handoff only when the timezone is known. Used by the
    automatic (timer) and implicit (settings-save) triggers, which must not
    start a wrong-time clock. Returns True if the handoff is complete after the
    call (including the already-complete and not-our-job cases)."""
    if not is_handoff_active(app):
        return True
    if not timezone_known(app):
        log.info("handoff: timezone not yet known — leaving splash up (no location set)")
        return False
    return mark_handoff_complete(app)


def _sudo_touch_argv(app) -> list[str]:
    """The exact argv used for the sudo fallback — exposed for the sudoers
    parity test so a path/binary drift is caught in CI. sudo strips argv[0],
    so the command sudoers matches is `_TOUCH <handoff_complete_path>`."""
    return ["sudo", _TOUCH, _handoff_complete_path(app)]


# ---------- background timer ----------


def start_auto_timer(app, delay: float | None = None) -> threading.Timer:
    """Schedule the auto-completion timer. Fires once after ``delay`` seconds
    (default HANDOFF_TIMEOUT_S) and completes the handoff iff the timezone is
    known. Daemon thread so it never blocks process shutdown. Returns the Timer
    so callers/tests can cancel or join it."""
    if delay is None:
        delay = _timeout_seconds(app)

    def _fire() -> None:
        try:
            if complete_if_timezone_known(app):
                log.info("handoff: auto-completed after %.0fs timeout", delay)
        except Exception:  # noqa: BLE001 — never let a timer thread crash silently
            log.exception("handoff: auto-complete timer failed")

    timer = threading.Timer(delay, _fire)
    timer.daemon = True
    timer.name = "litclock-handoff-timeout"
    timer.start()
    return timer


# ---------- browser-tz fallback ----------


def set_timezone_and_complete(app, timezone: str) -> tuple[bool, str | None]:
    """Set the system timezone (browser-tz fallback) and, on success, complete
    the handoff. Returns (ok, error). The set-timezone path is the one completer
    allowed to run with latitude empty — the user has explicitly confirmed the
    tz, which is all quote rendering needs (weather stays off until a location
    is set)."""
    # #414 maintainability item #5: tz setter moved to geocoding (lighter
    # imports than setup_server). Original "pulls hardware deps; import
    # lazily" rationale no longer applies — geocoding has no hardware deps —
    # but the lazy import is preserved so create_app() stays cheap.
    from geocoding import set_system_timezone  # noqa: PLC0415

    ok, err = set_system_timezone(timezone)
    if not ok:
        return False, err
    mark_handoff_complete(app)
    return True, None


# ---------- display helpers ----------


def resolve_lan_ip() -> str | None:
    """Primary LAN IPv4 via the stdlib connect-trick (no packet leaves the
    box). Returns None for loopback / link-local / no-route. Canonical
    implementation + rationale: literary_clock._resolve_lan_ip (#306). Inlined
    here to keep the hardware-free control_server import graph clean."""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.5)
        sock.connect(("1.1.1.1", 80))
        ip = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    if not ip or ip.startswith(("127.", "169.254.")):
        return None
    return ip


def qr_url(app) -> str:
    """The PWA URL the handoff QR encodes. Prefers the just-acquired DHCP IP
    (A5: IP-in-QR has 100% scan success vs flaky Android mDNS); falls back to
    the mDNS hostname only when no usable IP is available."""
    ip = resolve_lan_ip()
    host = ip if ip else "litclock.local"
    return control_base_url(host)


def _sanitize_ssid_for_render(ssid: str) -> str:
    """Strip control chars + collapse internal whitespace so PIL's
    ``draw.textlength`` can measure the SSID without raising. PIL rejects
    multiline strings with ``ValueError: can't measure length of multiline
    text``, which would silently nuke the entire handoff splash render
    if any source (nmcli mode change, dev stub, future refactor) ever
    returns an SSID containing ``\\n``. The 802.11 spec allows arbitrary
    bytes 0-255 in an SSID, so we cannot trust the source to be clean.

    Keeps printable chars (str.isprintable, which includes spaces by
    design); collapses internal whitespace to single spaces; strips ends.
    Returns the empty string if nothing survives."""
    if not ssid:
        return ""
    cleaned = "".join(ch for ch in ssid if ch.isprintable())
    return " ".join(cleaned.split())


def connected_ssid() -> str:
    """Best-effort read of the SSID the clock is currently connected to.
    Used by the e-ink handoff splash (#399) to tell the user which WiFi
    their phone must be on for the QR to scan-and-load — the QR encodes
    a LAN-only IP, so a phone on cellular / a different WiFi gets a
    silent dead link.

    Returns the empty string on any failure (no WiFi yet, nmcli missing,
    permissions etc.) so callers can use the truthy/falsy distinction
    instead of None-handling. The splash suppresses the caveat line
    when this returns empty — better to omit than display "(unknown)".

    Control chars + multiline content are stripped via
    ``_sanitize_ssid_for_render`` before returning so downstream PIL
    measurement cannot crash on a `\\n`-bearing SSID."""
    try:
        # wifi_provision is a sibling-of-control_server module; import lazily
        # to avoid pulling nmcli/subprocess imports on a pure-PWA test path.
        import wifi_provision  # noqa: PLC0415
    except ImportError:
        # Expected off-Pi (dev box, pure-PWA test path). Silent — no signal.
        return ""
    try:
        return _sanitize_ssid_for_render(wifi_provision.get_wifi_ssid() or "")
    except Exception as exc:  # noqa: BLE001 — runtime nmcli failure
        # Runtime failure on a Pi (nmcli missing, permissions, network
        # stack wedged). Surface at warning — this is interesting in
        # field debugging vs. the expected-off-Pi ImportError above.
        log.warning("handoff: SSID read failed (non-fatal): %s", exc)
        return ""


def current_timezone() -> str | None:
    """Best-effort read of the current system timezone for the e-ink splash.
    Non-privileged: timedatectl show, falling back to /etc/timezone."""
    try:
        result = subprocess.run(
            ["timedatectl", "show", "--property=Timezone", "--value"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=_SUDO_TIMEOUT_S,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        pass
    try:
        return Path("/etc/timezone").read_text().strip() or None
    except OSError:
        return None


def _units_label(units: str) -> str:
    return "Metric (°C)" if units == "metric" else "Imperial (°F)"


def handoff_context(app) -> dict:
    """Banner data derived from env.sh. ``state`` is "success" when a location
    was detected (tz known) and "failure" when it wasn't — the two banner
    variants the template renders.

    Deliberately env-only (cheap): this runs in the per-request context
    processor during the handoff window. It does NOT fork ``timedatectl`` or
    open a socket — the timezone + QR-url fields are only needed by the e-ink
    splash (computed once in ``render_eink_splash``), not the PWA banner. On a
    Pi Zero 2W a subprocess + socket per page render is real latency."""
    env = _load_env(app)
    has_location = _has_location(env)
    units = env.get("WEATHER_UNITS", "imperial") or "imperial"
    return {
        "active": is_handoff_active(app),
        "state": "success" if has_location else "failure",
        "has_location": has_location,
        "location_name": env.get("WEATHER_LOCATION_NAME", "").strip(),
        "units": units,
        "units_label": _units_label(units),
        "mature_enabled": env.get("ALLOW_NSFW_QUOTES", "false").strip().lower() == "true",
    }


def render_eink_splash(app) -> bool:
    """Render the handoff splash to the e-ink display. Best-effort: the e-ink
    stack (Pillow + waveshare drivers) is hardware-only, so the import is lazy
    and every failure is swallowed — a dev box or a headless test never paints,
    and a render failure on the Pi must not crash control_server startup.
    Returns True iff a frame was pushed.

    #388 fresh-flash fix (test-Pi QA 2026-07-06): control_server is LONG-LIVED, so
    it must NOT paint the e-ink in its own process. The lgpio line claims a paint
    makes are held for the process lifetime — ``epd.sleep()`` only deep-sleeps the
    panel and ``module_exit``/gpiozero-close does NOT free the claims; only PROCESS
    EXIT does. An in-process paint therefore left the always-on PWA holding the GPIO
    forever, and ``litclock.service`` (the per-minute quote painter) died with lgpio
    ``'GPIO busy'`` so the clock never left the splash. We instead paint via a
    SHORT-LIVED SUBPROCESS (same pattern as first-boot.sh's splashes) that claims,
    paints, and frees the lines on exit. control_server never imports the hardware
    stack here."""
    try:
        # The splash needs tz + QR url + SSID the banner doesn't — compute them
        # here (once, at kickoff), all hardware-free. connected_ssid is a #399
        # addition: the splash paints a "phone must be on this WiFi" caveat next to
        # the QR so a phone on cellular / a different network doesn't silently fail.
        ctx = handoff_context(app)
        ctx["timezone"] = current_timezone() or ""
        ctx["connected_ssid"] = connected_ssid()
        url = qr_url(app)

        install_dir = os.environ.get("LITCLOCK_DIR") or str(Path(__file__).resolve().parents[2])
        script = os.path.join(install_dir, "src", "eink_display.py")
        result = subprocess.run(
            [sys.executable, script, "handoff-splash", url, "--settings-json", json.dumps(ctx)],
            cwd=install_dir,
            timeout=_SPLASH_PAINT_TIMEOUT_S,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            log.warning(
                "handoff: splash painter exited %s (non-fatal): %s",
                result.returncode,
                (result.stderr or result.stdout or "").strip()[:200],
            )
            return False
        return True
    except subprocess.TimeoutExpired:
        # NOT `exc` in the message (/review): str(TimeoutExpired) embeds the full
        # argv, which carries the settings JSON (SSID + location name). redact_text
        # doesn't scrub those, so it would leak PII into the #416 diagnostics log
        # buffer + journald — exactly when a wedged display gets debugged/shared.
        log.warning("handoff: splash painter timed out after %ss (non-fatal)", _SPLASH_PAINT_TIMEOUT_S)
        return False
    except Exception as exc:  # noqa: BLE001 — hardware/subprocess failure is non-fatal
        # Safe to format: spawn errors (FileNotFoundError etc.) carry the executable
        # / script path, not the settings argv — only TimeoutExpired embeds that.
        log.warning("handoff: could not render e-ink splash (non-fatal): %s", exc)
        return False


# ---------- one-shot launch hook ----------


def kickoff(app) -> None:
    """Run once at real process launch (from app.py main, NOT create_app, so
    tests never paint e-ink or start timers). If the handoff phase is active,
    paint the splash and arm the auto-completion timer."""
    if not is_handoff_active(app):
        return
    log.info("handoff: first launch since setup — entering handoff phase")
    render_eink_splash(app)
    start_auto_timer(app)
