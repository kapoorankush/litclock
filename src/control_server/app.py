"""LitClock Control PWA — process entry point.

Plain HTTP on port 80 (#343 — so a user never types a port; bound by the
``pi`` service account via the ip_unprivileged_port_start sysctl, not a
capability). The locked PLAN A2/A4 + PRD §7.3 originally specified
self-signed TLS to "match the first-boot cert UX." Hardware QA on PR #252
invalidated that assumption: iOS standalone PWAs reject self-signed certs
on every launch (UX deal-breaker), the iOS apple-touch-icon fetch is
suppressed for untrusted-cert origins (AtHS shows letter-initial fallback),
and iOS Dynamic Type appears to be suppressed for the same reason.

Per re-decided issue #257 (option C), control_server drops TLS entirely
and serves plain HTTP. Justification:

- The locked threat model (PLAN A4) explicitly accepts LAN-trust:
  "Assumes no malicious actor on home WiFi (matches Hue, Sonos, Nest
  thermostat posture)." Self-signed TLS adds no security value on a
  LAN-trust threat model — only the UX cost.
- Cloud relay (PRD v2) will introduce real TLS via Let's Encrypt at
  that point. Until then, no cleartext-on-WAN exposure.
- setup_server (first-boot WiFi credential POST) keeps its self-signed
  TLS — credentials must not transit cleartext even on a LAN-trust
  fabric, since first-boot can run on an open hotspot. control_server
  ships post-setup, post-WPA2, on the user's home WiFi.

waitress is the locked WSGI choice (PLAN P1) — production-grade, threaded,
~30MB RSS budget. It handles signal teardown, request timeouts, thread-
pool sizing internally; the hand-rolled TLS terminator + slow-loris
guards from the prior commit are no longer needed without TLS to defend.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make sibling modules importable when invoked as a script.
_SRC = Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from control_server import create_app  # noqa: E402
from control_url import CONTROL_PORT as PORT  # noqa: E402  # single source of truth (#343)

THREADS = int(os.environ.get("LITCLOCK_CONTROL_THREADS", "4"))
# Bind address override. Production default 0.0.0.0 (reachable on LAN);
# tests set LITCLOCK_CONTROL_BIND=127.0.0.1 so the spawned subprocess is
# not transiently exposed to the LAN during the memory-sentinel run.
BIND = os.environ.get("LITCLOCK_CONTROL_BIND", "0.0.0.0")


def main() -> int:
    import logging  # noqa: PLC0415

    from waitress import serve

    from control_server import handoff  # noqa: PLC0415

    # #415 /review: configure logging so `log.info()` from sibling modules
    # surfaces in journald. Specifically restores observability for
    # location_resolver's "Resolved location: lat=... mode=..." diagnostic
    # on the PWA sync-quick path, which regressed in #414 item #4 when bare
    # print() was migrated to log.info() — Python's root logger defaults to
    # WARNING and silently drops INFO without a configured handler. Default
    # to INFO here (the PWA is the user-facing surface where Save diagnostics
    # matter); LOG_LEVEL env var still overrides. The on-boot reresolve
    # oneshot calls its own basicConfig in location_resolver.main() and is
    # unaffected.
    log_level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level_name, logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
    )

    # #416 (eng-review C3=A + OV-1=A): install the in-memory log buffer +
    # RedactingFilter on the ROOT logger so /api/logs and the SSE drawer
    # see every log call made by control_server's sibling modules. Lives
    # here, NOT in create_app(), so tests that build apps via the factory
    # don't accumulate global handler state across cases. The redaction
    # filter sanitizes records BEFORE they land in the buffer — the
    # buffer is privacy-clean by construction.
    from control_server.log_buffer import init_memory_handler  # noqa: PLC0415

    init_memory_handler()

    app = create_app()
    # EPIC #383 PR2 (#388): on the first launch since setup completed, paint the
    # handoff splash to e-ink and arm the auto-completion timer. Lives here (not
    # create_app) so the test client never paints hardware or starts a timer.
    handoff.kickoff(app)
    print(f"control_server: HTTP on {BIND}:{PORT} (threads={THREADS})")
    try:
        serve(app, host=BIND, port=PORT, threads=THREADS)
    except PermissionError:
        # #343: binding a privileged port (80) needs the
        # net.ipv4.ip_unprivileged_port_start=80 sysctl. If it hasn't applied
        # (an OTA where `sysctl -w` errored, or a kernel < 4.11), fail with an
        # actionable message instead of a bare traceback. The unit
        # (StartLimitIntervalSec=0) keeps retrying; a reboot applies the
        # installed sysctl drop-in and the bind then succeeds.
        logging.error(
            "control_server could not bind port %s as a non-root user. The "
            "'net.ipv4.ip_unprivileged_port_start=80' sysctl is not applied — "
            "reboot to apply /etc/sysctl.d/30-litclock-unprivileged-ports.conf, "
            "or run: sudo sysctl -w net.ipv4.ip_unprivileged_port_start=80",
            PORT,
        )
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
