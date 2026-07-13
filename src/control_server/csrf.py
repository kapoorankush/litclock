"""CSRF guard for the M3 Settings tab.

Per M3 locked decisions D4 + D5 (PLAN-LitClock-Control-PWA.md):

- **D4 — multi-use synchronizer token, action=`settings`, TTL 30 min.**
  A long TTL is fine because Settings is a low-stakes, high-frequency surface
  (every save mints + consumes; the user might leave the tab open while they
  edit gift-mode copy). Single-use would force a full-page reload between
  edits. Reuse is bounded by the 30-min expiry window plus the Origin/Referer
  reflexive-host check, which is the actual CSRF defense; the token is the
  defense-in-depth synchronizer that makes a missing-Origin attacker's life
  harder still.

- **D5 — reflexive Origin/Referer match against `request.host`.**
  No static allowlist: the Pi is reachable as `litclock.local`, the IP, or
  whatever name the LAN's mDNS resolver returns first. Hard-coding any one
  fingerprints the appliance. Reflexive check passes iff the browser's
  Origin (or Referer host fallback) matches the host the request landed on.
  Fails closed when both Origin and Referer headers are missing — RFC 6454
  recommends fail-closed for state-changing requests when origin can't be
  established.

The token store mirrors ``confirm_tokens.ConfirmTokenStore``'s API on
purpose so callers can swap the two with minimal re-wiring; the differences
are TTL (30 min vs 5 min), action namespace (`settings` only), and
single-vs-multi consume semantics. We don't share the store class because
single-use semantics live on the M4 confirm-token side and would be a
footgun to mix in.
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Final
from urllib.parse import urlparse

from flask import Request

CSRF_ACTION: Final[str] = "settings"
# 30-min window — long enough that a user editing gift-mode copy across two
# saves doesn't have to reload, short enough that a stolen token from a
# closed tab is gone before the next attacker can replay it.
TTL_SECONDS: Final[int] = 30 * 60


class CsrfTokenStore:
    """Multi-use, action-bound, TTL-bounded synchronizer token store.

    Validates tokens on every state-changing request and re-issues only
    when the caller hits the legitimate render route. Lazy GC on every
    issue/validate call.
    """

    def __init__(self, ttl_seconds: int = TTL_SECONDS) -> None:
        self._ttl_seconds = ttl_seconds
        # token -> (action, expires_at_monotonic)
        self._tokens: dict[str, tuple[str, float]] = {}
        # Waitress runs `threads=4` by default (see src/control_server/app.py).
        # Without a lock, concurrent `issue()` + `_sweep()` calls would race on
        # `self._tokens` and raise `RuntimeError: dictionary changed size during
        # iteration` mid-request, 500-ing a save under load.
        self._lock = threading.Lock()

    def issue(self, action: str = CSRF_ACTION) -> tuple[str, int]:
        """Mint a token and bind it to ``action``. Returns
        ``(token, expires_at_unix_seconds)``."""
        if not action:
            raise ValueError("action must be a non-empty string")
        token = secrets.token_urlsafe(32)
        now_monotonic = time.monotonic()
        now_wall = int(time.time())
        with self._lock:
            self._sweep_locked(now_monotonic)
            self._tokens[token] = (action, now_monotonic + self._ttl_seconds)
        return token, now_wall + self._ttl_seconds

    def validate(self, action: str, token: str) -> bool:
        """Return True iff ``token`` is fresh, bound to ``action``, and not
        expired. Tokens are NOT consumed — the same token can validate many
        saves within the TTL window (multi-use semantics, D4). Always
        returns False (without raising) for unknown/missing tokens."""
        now = time.monotonic()
        with self._lock:
            self._sweep_locked(now)
            record = self._tokens.get(token)
            if record is None:
                return False
            bound_action, expires_at = record
            if bound_action != action:
                return False
            if expires_at < now:
                self._tokens.pop(token, None)
                return False
            return True

    def _sweep_locked(self, now: float) -> None:
        """Caller must hold ``self._lock``."""
        expired = [t for t, (_, exp) in self._tokens.items() if exp < now]
        for t in expired:
            del self._tokens[t]


def _host_of(url: str) -> str | None:
    """Extract host[:port] from an absolute URL. Returns None for malformed
    or relative URLs."""
    try:
        parsed = urlparse(url)
    except (ValueError, AttributeError):
        return None
    if not parsed.netloc:
        return None
    return parsed.netloc


def origin_matches_host(req: Request) -> bool:
    """Reflexive same-origin check (D5).

    Compares the browser-supplied ``Origin`` (or ``Referer`` as fallback) to
    ``request.host``. Passes iff they match exactly (case-insensitive on
    hostname; ports must match if present). Fails closed when both headers
    are absent.

    Why reflexive vs static allowlist: the Pi is reachable as
    ``litclock.local``, raw IP, or whatever name a LAN's mDNS picks. A
    static allowlist would either be too permissive (accept anything) or
    too restrictive (break the IP fallback). Reflexive lets the browser's
    own same-origin claim drive the decision — the attacker can't forge
    Origin from a cross-site form submission (browsers strip / lock that
    header to the actual page origin), so this is sufficient for the
    LAN-trust model PLAN A4 documents.
    """
    request_host = (req.host or "").lower()
    if not request_host:
        return False

    origin = req.headers.get("Origin")
    referer = req.headers.get("Referer")

    # Origin is the authoritative signal — browsers won't let JS forge it
    # for cross-site state-changing fetches/forms. If Origin is present but
    # mismatched, that's a hard fail regardless of Referer.
    if origin is not None:
        # The literal string "null" is what some browsers send for sandboxed
        # iframes / opaque origins; treat it as a definite mismatch.
        if origin == "null":
            return False
        origin_host = _host_of(origin)
        if origin_host is None:
            return False
        return origin_host.lower() == request_host

    if referer is not None:
        referer_host = _host_of(referer)
        if referer_host is None:
            return False
        return referer_host.lower() == request_host

    # Both missing — fail closed.
    return False
