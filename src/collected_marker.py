"""Persistent "data has been collected" marker for the /diagnostics tiers (#445).

The diagnostics "Not yet collected" grey tier (#432) originally keyed off the
tmpfs file ``/run/litclock/last-rendered-ip`` — present iff the NM dispatcher
had fired since boot. That file is wiped at every reboot, so a healthy clock
flashed grey for ~5-10s after each boot until the dispatcher re-fired. This
module maintains the honest replacement: a PERSISTENT JSON marker at
``/var/lib/litclock/.last-collected-marker.json`` answering "has this section
EVER been collected on this Pi" (one ISO-8601 UTC timestamp per section).

Two writers exist by design, in two languages:

  * ``scripts/litclock-mark-collected.sh`` — the NM dispatcher writes
    ``network`` as root via ``/bin/sh`` (it must not depend on the venv).
  * this module — the IP-geo resolvers (``location_resolver.py`` +
    ``setup_server._resolve_location_from_ip``) write ``time-location`` as pi.

Both target the SAME file + sidecar lock + format. ``flock`` interlocks them
across processes/languages; ``tests/test_collected_marker.py`` pins the parity
so the two implementations can't drift. The read side
(``control_server.routes.diagnostics._anomalies``) reads the JSON directly and
treats a torn/absent file as "fall back to the legacy tmpfs check".

Every public call here is BEST-EFFORT: a marker write must never fail a
location resolve or an e-ink render. Failures are logged at debug and
swallowed.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
import time
from datetime import UTC, datetime

log = logging.getLogger(__name__)

# Keep in lockstep with scripts/litclock-mark-collected.sh (MARKER default).
DEFAULT_COLLECTED_MARKER_PATH = "/var/lib/litclock/.last-collected-marker.json"

# Only these two sections have a collection lifecycle. A typo'd key would
# create a junk entry the read side ignores, so reject unknown keys (no-op).
VALID_SECTIONS = ("network", "time-location")

# flock acquisition budget. Contention is rare (network + time-location
# writers seldom fire in the same instant), so a short wait is plenty; on
# timeout we skip the write rather than block a boot oneshot.
_LOCK_WAIT_S = 5.0


def _marker_path(marker_path: str | None) -> str:
    if marker_path:
        return marker_path
    return os.environ.get("LITCLOCK_COLLECTED_MARKER", DEFAULT_COLLECTED_MARKER_PATH)


def _read_existing(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def mark_collected(section: str, marker_path: str | None = None) -> bool:
    """Record ``section`` as collected (now, UTC) in the persistent marker.

    Read-modify-write under ``flock`` so the concurrent shell writer (or a
    second Python writer) can't clobber the other section's key. Returns
    ``True`` on a successful write, ``False`` on any skip/failure. Never
    raises — callers treat the marker as advisory.
    """
    if section not in VALID_SECTIONS:
        return False
    path = _marker_path(marker_path)
    directory = os.path.dirname(path) or "."
    if not os.path.isdir(directory):
        # Sandbox / CI / pre-tmpfiles: nothing durable to write to.
        return False

    lock_path = path + ".lock"
    ts = datetime.now(UTC).isoformat(timespec="seconds")

    lock_fd = None
    try:
        # Open the lock READ-ONLY (O_CREAT) so a root-owned lock never blocks
        # this pi writer — read-open needs no write permission, and flock
        # interlocks on the inode regardless of open mode.
        lock_fd = os.open(lock_path, os.O_RDONLY | os.O_CREAT, 0o644)
        if not _acquire(lock_fd):
            log.debug("collected-marker: lock busy after %ss, skipping %s", _LOCK_WAIT_S, section)
            return False
        data = _read_existing(path)
        data[section] = ts
        _atomic_write(path, directory, data)
        return True
    except OSError as exc:
        log.debug("collected-marker: write failed for %s: %s", section, exc)
        return False
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(lock_fd)


def _acquire(lock_fd: int) -> bool:
    """Non-blocking flock poll loop with a deadline (mirrors the env.sh writer
    pattern, #274). Returns False if the lock can't be taken within the
    budget. ``time.monotonic`` is immune to NTP steps."""
    deadline = time.monotonic() + _LOCK_WAIT_S
    while True:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


def _atomic_write(path: str, directory: str, data: dict) -> None:
    """Write ``data`` as JSON via a temp file + rename so the unlocked reader
    never sees a torn file. chmod 0644 so control_server (pi) can read a
    marker written by root."""
    fd, tmp = tempfile.mkstemp(prefix=".last-collected-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
