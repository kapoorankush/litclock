"""Per-IP token-bucket rate limiter for /api/system/* (#245 M4).

5 actions/minute per ``request.remote_addr`` covering /confirm-token,
/reboot, and /poweroff together — so spamming the cheap issuance endpoint
can't bypass the cap on the destructive ones.

We're behind the LAN — DDoS isn't the threat. The defended scenario is an
automation bug or a stuck retry loop firing reboot in a tight loop. 5/min
gives a human plenty of headroom for "did that work? let me try again"
while turning a runaway loop into a 429 within the first second.

Per-app instance lives in ``flask.current_app.extensions["system_rate_limiter"]``,
so each ``create_app()`` call (test or production) gets its own state.
``X-Forwarded-For`` is intentionally NOT honored — there's no proxy in front
of waitress on the Pi, and trusting client-supplied headers in the LAN-trust
threat model would let a single attacker rotate IPs trivially.
"""

from __future__ import annotations

import math
import time
from typing import Final

DEFAULT_CAPACITY: Final[int] = 5
DEFAULT_PER_SECONDS: Final[int] = 60
# Drop bucket entries that haven't been touched in this many windows. Keeps
# the dict from growing unbounded if anyone ever puts the server behind a
# public proxy — under the LAN-trust threat model the bound matters less,
# but the eviction is cheap and removes a future footgun.
EVICTION_AGE_WINDOWS: Final[int] = 10


class RateLimiter:
    """Token-bucket: ``capacity`` tokens that refill at
    ``capacity / per_seconds`` tokens per second per IP.

    ``take(ip)`` returns ``(allowed, retry_after_s)``. ``retry_after_s`` is 0
    when ``allowed`` is True; otherwise it's the integer seconds until the
    bucket refills enough for the next request (always ≥ 1).
    """

    def __init__(
        self,
        capacity: int = DEFAULT_CAPACITY,
        per_seconds: int = DEFAULT_PER_SECONDS,
    ) -> None:
        self.capacity = float(capacity)
        self.refill_per_second = capacity / per_seconds
        # ip -> (tokens_remaining, last_refill_monotonic)
        self._buckets: dict[str, tuple[float, float]] = {}

    def take(self, ip: str) -> tuple[bool, int]:
        now = time.monotonic()
        self._evict(now)
        tokens, last = self._buckets.get(ip, (self.capacity, now))

        # Refill since last touch — capped at capacity (no over-fill).
        tokens = min(self.capacity, tokens + (now - last) * self.refill_per_second)

        if tokens >= 1.0:
            self._buckets[ip] = (tokens - 1.0, now)
            return True, 0

        # Not enough — caller is rate limited. Compute time-until-1-token.
        deficit = 1.0 - tokens
        retry_after_s = max(1, math.ceil(deficit / self.refill_per_second))
        self._buckets[ip] = (tokens, now)
        return False, retry_after_s

    def _evict(self, now: float) -> None:
        """Drop buckets that haven't been touched in EVICTION_AGE_WINDOWS *
        per_seconds. A bucket that's been silent that long is at full
        capacity anyway — re-creating it on next access produces the same
        result as keeping the stale entry. Keeps the dict bounded by the
        number of currently-active clients, not lifetime-distinct ones.
        """
        # Reuse per_seconds by deriving from refill rate.
        per_seconds = self.capacity / self.refill_per_second
        cutoff = now - per_seconds * EVICTION_AGE_WINDOWS
        stale = [ip for ip, (_, last) in self._buckets.items() if last < cutoff]
        for ip in stale:
            del self._buckets[ip]
