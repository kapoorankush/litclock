"""In-memory single-use confirm-token store.

The Control PWA's destructive system actions — reboot, poweroff (litclock-dev#245 M4),
update_apply, wifi_reset (litclock-dev#245 M5) — are gated by a per-action confirm
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
# first two; M5 (litclock-dev#245) added `update_apply` for /api/update/apply and
# `wifi_reset` for /api/wifi/reset. Issue litclock-dev#280 adds `prepare_for_gift` for
# /api/system/prepare-for-gift (wipes WiFi + paints welcome splash + powers
# off, similar blast radius to wifi_reset). Each action's route handler
# binds its consume() call to its own action string so a token issued for
# one action cannot be replayed against another. Issue litclock-dev#510 adds
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
# client (litclock-dev#317 item 1 codex /review P2). 600s covers realistic double-submit
# / bfcache windows; after the tombstone expires we fall back to the
# pre-litclock-dev#317-followup behavior (no tombstone → "invalid" rather than
# "consumed"), which is acceptable because that window is 2x the TTL and
# the consumed-token hash space is collision-resistant.
TOMBSTONE_TTL_SECONDS: Final[int] = 600

# Expired-token tombstone TTL (litclock-dev#597). When _sweep_locked() drops a
# token whose TTL passed WITHOUT being consumed, it parks the hash here so a
# later consume of that same token classifies as "expired" (client mints a
# fresh token and retries) instead of "invalid" (a dead-end "confirm token
# unrecognised" alert with no recovery). Without this, whether a sat-on token
# reports "expired" or "invalid" depended on sweep timing: consume_classified
# looks up before sweeping, but ANY intervening issue/consume (a second action
# card's modal, a re-mint) would already have swept the record, collapsing it to
# "invalid". A user who opens the System tab and taps a destructive action a few
# minutes later hit exactly that. Horizon is long and generous — the only cost
# of classifying a genuinely-issued-then-stale token as "expired" is a client
# remint (which still requires the user to re-confirm), and an attacker's guessed
# token was never issued so never lands here. Bounded by tokens issued per day on
# a single-user device; swept lazily like the other two dicts.
EXPIRED_TTL_SECONDS: Final[int] = 24 * 60 * 60

# Hard cap on the expired-tombstone dict (litclock-dev#597 /review). Unlike the
# live store (300s TTL) and the consumed tombstone (fed only by rate-limited
# POSTs), _expired is fed by token ISSUANCE — GET /system mints one token per
# action card per render, and page GETs are NOT rate-limited on this
# unauthenticated-on-LAN PWA. Without a cap, a polling dashboard or a hostile
# LAN client hammering /system would accrete hash entries for the full 24h
# horizon and could OOM a 512MB Pi Zero. The cap bounds memory regardless of
# request rate; eviction is oldest-first and only ever degrades a very stale tap
# back to the pre-litclock-dev#597 "invalid" dead-end — never to anything unsafe. 4096 is
# orders of magnitude above a real single-user device's daily issuance.
EXPIRED_MAX_ENTRIES: Final[int] = 4096

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

    # User-facing copy carries NO "confirm token" jargon (litclock-dev#597):
    # a non-technical owner should read what to do, not what broke internally.
    # The machine-readable `code` slugs are the stable contract the client
    # branches on and are unchanged.
    if outcome == "expired":
        return envelope(
            "confirm_token_expired",
            "This confirmation timed out for safety. Reload the page and try again.",
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
        "Couldn't verify that action. Reload the page and try again.",
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
    can't double-consume the same token (litclock-dev#245 M5 codex F10).
    """

    def __init__(
        self,
        ttl_seconds: int = TTL_SECONDS,
        tombstone_ttl_seconds: int = TOMBSTONE_TTL_SECONDS,
        expired_ttl_seconds: int = EXPIRED_TTL_SECONDS,
        expired_max_entries: int = EXPIRED_MAX_ENTRIES,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._tombstone_ttl_seconds = tombstone_ttl_seconds
        self._expired_ttl_seconds = expired_ttl_seconds
        self._expired_max_entries = expired_max_entries
        # token -> (action, expires_at_monotonic)
        self._tokens: dict[str, tuple[str, float]] = {}
        # litclock-dev#317 item 1 codex P2 — shadow dict of recently-consumed tokens
        # (hashed, so a tombstone-only memory disclosure cannot replay).
        # Lets consume_classified() distinguish "consumed" from "expired"
        # so the JS refresh-and-retry only fires on real TTL expiry — a
        # double-click / bfcached resubmit hits "consumed" instead of
        # silently bypassing the single-use guard. hashed_token -> tombstone_expiry_monotonic.
        self._consumed: dict[str, float] = {}
        # litclock-dev#597 — shadow dict of tokens that expired UNCONSUMED
        # (swept for TTL, not for use). Lets consume_classified() report
        # "expired" (→ client remint) rather than "invalid" (→ dead-end alert)
        # for a token the sweep already dropped. hashed_token -> expiry_monotonic.
        self._expired: dict[str, float] = {}
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

        Issue litclock-dev#328: returning the expiry instead of a bare bool lets the
        caller pass it back to ``restore()`` if a pre-side-effect failure
        path (gate, validation, subprocess error before dispatch) needs to
        un-consume the token so the user's retry doesn't hit a spurious
        "token already used" 401 that masks the real underlying error.

        litclock-dev#317 item 1 codex P2: prefer ``consume_classified()`` in route
        handlers — it distinguishes "expired" from "consumed" so the JS
        refresh-and-retry only fires on real TTL expiry. This method is
        retained as a backward-compatibility wrapper for tests / non-route
        callers that don't need the categorical breakdown.
        """
        result = self.consume_classified(action, token)
        return result.expiry

    def consume_classified(self, action: str, token: str) -> ConsumeResult:
        """Categorical variant of ``consume()``. litclock-dev#317 item 1 codex P2.

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
                #   (a) consumed recently (consumed tombstone) → "consumed"
                #   (b) expired unconsumed and swept (expired tombstone,
                #       litclock-dev#597) → "expired" so the client remints
                #       instead of dead-ending on "invalid"
                #   (c) never existed / malformed → "invalid"
                #
                # The two tombstones discriminate (a) and (b); (c) is the
                # residual "invalid". consumed takes precedence over expired
                # (a token is one or the other, never both, but check consumed
                # first so a real replay is never misread as a stale sit).
                h = _hash_token(token)
                if h in self._consumed:
                    outcome = "consumed"
                elif h in self._expired:
                    outcome = "expired"
                else:
                    outcome = "invalid"
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
                # TTL passed unconsumed — the user sat on it. Park an EXPIRED
                # tombstone (litclock-dev#597) so a retry on this same token
                # (record now popped) still classifies "expired" via the
                # `record is None` path above, instead of collapsing to
                # "invalid". NOT a consumed tombstone — the action never ran.
                self._park_expired_locked(_hash_token(token), now)
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

        Issue litclock-dev#328: when a destructive route consumes a token but then fails
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

        Issue litclock-dev#342 I8 — defense-in-depth: clamp ``expires_at_monotonic`` to
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
            # litclock-dev#317 item 1 codex P2 — drop any tombstone for this token so
            # the restored token can be consumed again. Without this, the
            # consume() that follows restore() would short-circuit to
            # "consumed" via the tombstone hit and the retry would 409
            # instead of running the action. Tombstone hashes the raw
            # token; pop by the same hash. Clear the expired tombstone too
            # (litclock-dev#597 /review): unreachable today (restore only
            # follows an "ok" consume, which never parks _expired), but keeps
            # the "a live token has no stale tombstone" invariant complete.
            h = _hash_token(token)
            self._consumed.pop(h, None)
            self._expired.pop(h, None)

    def _park_expired_locked(self, token_hash: str, now: float) -> None:
        # Caller MUST hold self._lock. Record an expired-token tombstone and
        # enforce the size cap (litclock-dev#597 /review). dict is
        # insertion-ordered, so the oldest live entry is first; evict it when
        # over the cap. Re-parking an existing hash keeps its original position
        # (dict assignment does not reorder), which is fine — a token is only
        # ever parked once (it leaves _tokens on the same call).
        self._expired[token_hash] = now + self._expired_ttl_seconds
        while len(self._expired) > self._expired_max_entries:
            oldest = next(iter(self._expired))
            del self._expired[oldest]

    def _sweep_locked(self) -> None:
        # Caller MUST hold self._lock. Method name keeps that contract loud.
        now = time.monotonic()
        expired = [t for t, (_, exp) in self._tokens.items() if exp < now]
        for t in expired:
            del self._tokens[t]
            # litclock-dev#597 — a token swept for TTL (never consumed) leaves
            # an expired tombstone so a later consume reports "expired" (client
            # remints) rather than "invalid" (dead-end alert). This is what
            # makes the classification independent of sweep timing.
            self._park_expired_locked(_hash_token(t), now)
        # litclock-dev#317 item 1 codex P2 — sweep stale tombstones. Same lazy-GC
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
        # litclock-dev#597 — GC expired tombstones on the same lazy pattern.
        # After EXPIRED_TTL_SECONDS a sat-on token finally falls back to
        # "invalid"; the horizon is a full day, far longer than any realistic
        # open-modal-then-tap window.
        stale_expired = [h for h, exp in self._expired.items() if exp < now]
        for h in stale_expired:
            del self._expired[h]
