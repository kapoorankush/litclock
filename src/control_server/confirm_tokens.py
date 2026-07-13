"""In-memory single-use confirm-token store.

The Control PWA's destructive system actions — reboot, poweroff (#245 M4),
update_apply, wifi_reset (#245 M5) — are gated by a per-action confirm
token. A token is issued when the user opens the confirm modal and
consumed when they tap the primary button. Single-use + 300s TTL means
a stale tab can't replay the action hours later (or a refresh-on-action
can't re-fire it).

This is NOT site-wide CSRF protection. The broader CSRF/Origin/Referer
contract is gated on the M3 unblocker tracked in TODOS.md and may layer on
top of this — token consumption stays orthogonal to whichever mechanism
M3 picks.

Design choice (logged in .gstack/build/decisions.md): in-memory dict, not
HMAC-signed. waitress runs single-process by default in this deployment;
multi-process serving isn't on the v1 roadmap. The store is held in
``flask.current_app.extensions["confirm_tokens"]`` so each ``create_app()``
call (production or test) gets its own isolated instance.

Concurrency (M5 codex F10): every issue / consume / sweep operation runs
under ``self._lock``. waitress runs threads=4 by default, so concurrent
POSTs to /api/system/* + /api/update/apply + /api/wifi/reset can hit the
same dict from different worker threads. CPython's GIL makes most dict
ops atomic at the bytecode level, but the sweep+pop+check sequence in
consume() is NOT atomic — two threads could both pop the same token,
both see "fresh + bound", both return True. The lock makes the action
deterministically single-use even under contention.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from typing import Final, Literal, NamedTuple

# All destructive actions that can mint + consume a token. M4 shipped the
# first two; M5 (#245) added `update_apply` for /api/update/apply and
# `wifi_reset` for /api/wifi/reset. Issue #280 adds `prepare_for_gift` for
# /api/system/prepare-for-gift (wipes WiFi + paints welcome splash + powers
# off, similar blast radius to wifi_reset). Each action's route handler
# binds its consume() call to its own action string so a token issued for
# one action cannot be replayed against another. Issue #510 adds
# `factory_reset` for /api/system/reset (wipes config + WiFi, reboots into
# setup — full-wipe sibling of wifi_reset, which is WiFi-only).
VALID_ACTIONS: Final[tuple[str, ...]] = (
    "reboot",
    "poweroff",
    "update_apply",
    "wifi_reset",
    "prepare_for_gift",
    "factory_reset",
)
# 300s window covers the realistic read-and-decide path: open /system,
# scroll to the action card, watch the modal slide up, read the locked
# DESIGN.md consequence copy ("display will go blank for about 30
# seconds…"), tap the destructive button. 60s was too tight — careful
# users hit the 401 confirm_token_invalid alert with no recovery path.
# Single-use property still defends against the "stale tab replays
# reboot hours later" threat; 5 minutes is plenty short for that.
TTL_SECONDS: Final[int] = 300

# Tombstone TTL — how long a consumed-token hash sticks around in the
# `_consumed` shadow dict so a duplicate POST (double-click, bfcached
# reload, re-submit-on-back) can be classified as "already used" instead
# of being misread as "expired" and silently re-fired by a refresh-and-retry
# client (#317 item 1 codex /review P2). 600s covers realistic double-submit
# / bfcache windows; after the tombstone expires we fall back to the
# pre-#317-followup behavior (no tombstone → "invalid" rather than
# "consumed"), which is acceptable because that window is 2x the TTL and
# the consumed-token hash space is collision-resistant.
TOMBSTONE_TTL_SECONDS: Final[int] = 600

# Outcome of consume_classified(). The route handler maps each outcome to
# a distinct HTTP response so the client can distinguish them:
#   - "ok"       → 200 / action proceeds
#   - "expired"  → 401 confirm_token_expired (TTL passed; client may mint
#                  a fresh token and retry exactly once)
#   - "consumed" → 409 confirm_token_consumed (token was already used;
#                  client must NOT retry — this is the single-use guard)
#   - "invalid"  → 401 confirm_token_invalid (unknown / wrong-action /
#                  malformed token — likely a buggy client or attack)
ConsumeOutcome = Literal["ok", "expired", "consumed", "invalid"]


class ConsumeResult(NamedTuple):
    """Result of consume_classified().

    ``outcome`` carries the categorical result; ``expiry`` is the original
    monotonic expiry on the ``"ok"`` branch (so the caller can pass it
    back to ``restore()`` on a pre-side-effect failure) and ``None`` on
    every other branch.
    """

    outcome: ConsumeOutcome
    expiry: float | None


def envelope_for_consume_outcome(outcome: ConsumeOutcome):
    """Map a non-ok ``consume_classified()`` outcome to a JSON envelope.

    Imported lazily by the route modules to keep the ``errors`` import
    out of the confirm_tokens unit-test surface. Centralised here so the
    four destructive routes (reboot, poweroff, prepare_for_gift,
    wifi_reset, update_apply) share one mapping — a future tightening
    (e.g., adding a Retry-After hint to ``consumed``) lands once.

    Outcomes:
      - "expired"  → 401 confirm_token_expired  (JS may refresh-and-retry)
      - "consumed" → 409 confirm_token_consumed (JS must NOT retry)
      - "invalid"  → 401 confirm_token_invalid  (existing slug; legacy
                                                  unknown / wrong-action path)
    """
    from .errors import envelope  # noqa: PLC0415 — lazy to keep test surface light

    if outcome == "expired":
        return envelope(
            "confirm_token_expired",
            "Confirm token has expired. Reload and try again.",
            401,
        )
    if outcome == "consumed":
        return envelope(
            "confirm_token_consumed",
            "This action was already submitted. Reload the page if you need to retry.",
            409,
        )
    # outcome == "invalid"
    return envelope(
        "confirm_token_invalid",
        "Confirm token is missing or unrecognised.",
        401,
    )


def _hash_token(token: str) -> str:
    """Hash the raw token before parking it in the tombstone dict.

    The active store keeps raw tokens (it needs them as dict keys to
    look up on POST). The tombstone only ever answers "is this hash
    present?" — storing hashes (not raw tokens) means a memory disclosure
    of the tombstone alone cannot replay consumed tokens against a fresh
    store instance. SHA-256 is overkill for the threat but cheap; the
    tombstone is sized in the low hundreds at most under realistic load.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ConfirmTokenStore:
    """Single-use confirm tokens bound to one of VALID_ACTIONS, TTL 300s.

    Sweeps expired entries on every issue/consume call — no background
    thread (waitress single-process makes lazy GC sufficient). All
    mutations run under ``self._lock`` so concurrent worker threads
    can't double-consume the same token (#245 M5 codex F10).
    """

    def __init__(
        self,
        ttl_seconds: int = TTL_SECONDS,
        tombstone_ttl_seconds: int = TOMBSTONE_TTL_SECONDS,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._tombstone_ttl_seconds = tombstone_ttl_seconds
        # token -> (action, expires_at_monotonic)
        self._tokens: dict[str, tuple[str, float]] = {}
        # #317 item 1 codex P2 — shadow dict of recently-consumed tokens
        # (hashed, so a tombstone-only memory disclosure cannot replay).
        # Lets consume_classified() distinguish "consumed" from "expired"
        # so the JS refresh-and-retry only fires on real TTL expiry — a
        # double-click / bfcached resubmit hits "consumed" instead of
        # silently bypassing the single-use guard. hashed_token -> tombstone_expiry_monotonic.
        self._consumed: dict[str, float] = {}
        self._lock = threading.Lock()

    def issue(self, action: str) -> tuple[str, int]:
        """Mint a fresh token bound to ``action``.

        Returns ``(token, expires_at_unix_seconds)``. Raises ValueError if
        ``action`` is not one of VALID_ACTIONS — callers (the route handler)
        should translate that into a 400 response.
        """
        if action not in VALID_ACTIONS:
            raise ValueError(f"invalid action: {action!r}")
        with self._lock:
            self._sweep_locked()
            token = secrets.token_urlsafe(32)
            # Two clocks: monotonic for internal expiry (immune to wall-clock
            # jumps from NTP correction) and wall-clock for the response (so
            # the client can render a relative countdown).
            now_monotonic = time.monotonic()
            now_wall = int(time.time())
            self._tokens[token] = (action, now_monotonic + self._ttl_seconds)
            return token, now_wall + self._ttl_seconds

    def consume(self, action: str, token: str) -> float | None:
        """Validate + remove a token. Returns the token's monotonic expiry
        timestamp (a float, immune to wall-clock NTP jumps) iff the token is
        fresh, bound to ``action``, and unused. Returns ``None`` (without
        raising) for unknown or invalid tokens — callers map ``None`` to a
        401 confirm_token_invalid response.

        Issue #328: returning the expiry instead of a bare bool lets the
        caller pass it back to ``restore()`` if a pre-side-effect failure
        path (gate, validation, subprocess error before dispatch) needs to
        un-consume the token so the user's retry doesn't hit a spurious
        "token already used" 401 that masks the real underlying error.

        #317 item 1 codex P2: prefer ``consume_classified()`` in route
        handlers — it distinguishes "expired" from "consumed" so the JS
        refresh-and-retry only fires on real TTL expiry. This method is
        retained as a backward-compatibility wrapper for tests / non-route
        callers that don't need the categorical breakdown.
        """
        result = self.consume_classified(action, token)
        return result.expiry

    def consume_classified(self, action: str, token: str) -> ConsumeResult:
        """Categorical variant of ``consume()``. #317 item 1 codex P2.

        Returns a :class:`ConsumeResult` with one of four outcomes:

        - ``"ok"``       — token was fresh, bound to ``action``, unused.
                           ``expiry`` is the monotonic deadline for restore.
        - ``"expired"``  — token existed but its TTL has passed.
                           ``expiry`` is ``None``. The route maps this to
                           HTTP 401 ``confirm_token_expired`` so the JS
                           refresh-and-retry path can mint a new token.
        - ``"consumed"`` — token was already consumed (single-use guard
                           tombstone hit). ``expiry`` is ``None``. The
                           route maps this to HTTP 409 ``confirm_token_consumed``
                           so the JS does NOT retry — protecting against
                           double-click / bfcached resubmit on destructive
                           one-shot actions.
        - ``"invalid"``  — token unknown / wrong-action / malformed.
                           ``expiry`` is ``None``. The route maps this to
                           HTTP 401 ``confirm_token_invalid`` (existing code).

        The tombstone is written for the ``"ok"`` branch (so a real replay
        is caught) AND for the wrong-action branch (which already pops the
        token under fail-closed semantics — record the consume to keep
        replay diagnostics honest). It is NOT written for genuinely
        unknown / expired tokens, since recording those would let an
        attacker poison the tombstone arbitrarily by guessing.
        """
        with self._lock:
            now = time.monotonic()
            # IMPORTANT: do the lookup BEFORE _sweep_locked() so an
            # already-expired record can be classified as "expired"
            # rather than collapsed into "invalid" by the sweep. The
            # sweep still runs at the end to garbage-collect stale
            # tombstones and any other expired records (lazy GC pattern
            # matching issue()).
            record = self._tokens.pop(token, None)
            if record is None:
                # Token not in live store. Could be:
                #   (a) consumed recently (tombstone hit) → "consumed"
                #   (b) expired and already swept → "invalid"
                #   (c) never existed / malformed → "invalid"
                #
                # The tombstone discriminates (a). For (b) vs (c) we can't
                # reconstruct the history (a previous sweep dropped any
                # forensic state), so we fold them both into "invalid" —
                # both branches map to a 401 client-side and the JS
                # refresh-and-retry path gates on "expired" only, so
                # neither outcome is critical-path security.
                outcome = "consumed" if _hash_token(token) in self._consumed else "invalid"
                self._sweep_locked()
                return ConsumeResult(outcome, None)
            bound_action, expires_at = record
            if bound_action != action:
                # Fail-closed: token has been popped, record the consume in
                # the tombstone so a retry under the right action sees
                # "consumed" instead of "invalid".
                self._consumed[_hash_token(token)] = now + self._tombstone_ttl_seconds
                self._sweep_locked()
                return ConsumeResult("invalid", None)
            if expires_at < now:
                # TTL passed. Do NOT add a tombstone — the user didn't
                # consume it, they just sat on it. A retry on the same
                # expired token will fall into the `record is None` path
                # above and report "invalid" (the consume just dropped the
                # record). Acceptable: the JS retry gate fires on "expired"
                # exactly once, then the retry uses a fresh token.
                self._sweep_locked()
                return ConsumeResult("expired", None)
            # Successful consume. Record in the tombstone so a duplicate
            # POST (double-click, bfcached reload, stale tab) sees the
            # "consumed" outcome instead of "invalid" or — worse — a
            # silent refresh-and-retry that bypasses the single-use guard.
            self._consumed[_hash_token(token)] = now + self._tombstone_ttl_seconds
            self._sweep_locked()
            return ConsumeResult("ok", expires_at)

    def restore(self, action: str, token: str, expires_at_monotonic: float) -> None:
        """Atomically re-add a token previously returned by ``consume``.

        Issue #328: when a destructive route consumes a token but then fails
        BEFORE any side effect (busy gate, validation, subprocess error
        pre-dispatch), restoring the token at the original expiry lets the
        user retry with the same token in their open page. Without this,
        every gate / dispatch failure 401s the next attempt with "Confirm
        token is missing, expired, or already used" — masking the real
        error message that should have been shown.

        Concurrency: same lock as issue/consume. If the action is invalid,
        raises ValueError (matches ``issue()`` behavior). If a token at the
        same key already exists (concurrent restore race), this is a no-op
        — the live token is preserved and the late-arriving restore is
        dropped silently. The expiry passed in MUST come from a prior
        ``consume()`` call on this store; passing arbitrary floats is not a
        supported use case.

        Issue #342 I8 — defense-in-depth: clamp ``expires_at_monotonic`` to
        at most ``time.monotonic() + self._ttl_seconds``. Today's callers
        always pass an expiry straight back from ``consume()`` under the
        same action, so the clamp is a no-op on the happy path. A future
        refactor that synthesises an expiry (e.g. hardcoding
        ``time.monotonic() + 86400``) would otherwise silently mint a
        long-lived token that bypasses the 300s TTL contract.
        """
        if action not in VALID_ACTIONS:
            raise ValueError(f"invalid action: {action!r}")
        with self._lock:
            if token in self._tokens:
                # Concurrent restore race or duplicate restore — drop the
                # late-arriving call silently. The live token wins.
                return
            clamped_expiry = min(expires_at_monotonic, time.monotonic() + self._ttl_seconds)
            self._tokens[token] = (action, clamped_expiry)
            # #317 item 1 codex P2 — drop any tombstone for this token so
            # the restored token can be consumed again. Without this, the
            # consume() that follows restore() would short-circuit to
            # "consumed" via the tombstone hit and the retry would 409
            # instead of running the action. Tombstone hashes the raw
            # token; pop by the same hash.
            self._consumed.pop(_hash_token(token), None)

    def _sweep_locked(self) -> None:
        # Caller MUST hold self._lock. Method name keeps that contract loud.
        now = time.monotonic()
        expired = [t for t, (_, exp) in self._tokens.items() if exp < now]
        for t in expired:
            del self._tokens[t]
        # #317 item 1 codex P2 — sweep stale tombstones. Same lazy-GC
        # pattern as the live token store. After the tombstone TTL elapses,
        # a duplicate POST on the same (now-collapsed) token will be
        # classified as "invalid" instead of "consumed" — that's the 11+
        # minute fallback window. Acceptable because the raw token hash
        # space is large enough that no realistic resubmit would still be
        # in flight that long after the original consume, and "invalid"
        # also blocks the refresh-and-retry path (only "expired" triggers).
        stale_tombstones = [h for h, exp in self._consumed.items() if exp < now]
        for h in stale_tombstones:
            del self._consumed[h]
