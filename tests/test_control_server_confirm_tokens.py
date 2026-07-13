"""Tests for src/control_server/confirm_tokens.py + POST /api/system/confirm-token.

The store is the destructive-action gate for #245 M4 reboot/poweroff. These
tests pin its security-critical properties:

- Tokens are single-use (consume removes — second consume returns False).
- Tokens expire (60s TTL by default; tests inject a short TTL).
- Token-action binding is strict (a poweroff token does not consume against
  reboot, and vice versa).
- The HTTP route never accepts a non-{reboot, poweroff} action.
- The HTTP route never accepts non-JSON, missing, or wrong-type bodies.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from control_server import create_app  # noqa: E402
from control_server.confirm_tokens import (  # noqa: E402
    TOMBSTONE_TTL_SECONDS,
    TTL_SECONDS,
    VALID_ACTIONS,
    ConfirmTokenStore,
    envelope_for_consume_outcome,
)

# ─── Store unit tests ───────────────────────────────────────────────────────


class TestConfirmTokenStore:
    def test_default_ttl_is_300s(self):
        assert TTL_SECONDS == 300, (
            "TTL covers open-page + scroll + read-modal + tap. 60s was too "
            "tight (careful users hit 401 mid-read with no recovery path); "
            "300s is still well below the 'stale tab replays hours later' "
            "threat the single-use property defends against."
        )

    def test_valid_actions_locked(self):
        assert VALID_ACTIONS == (
            "reboot",
            "poweroff",
            "update_apply",
            "wifi_reset",
            "prepare_for_gift",
            "factory_reset",
        ), (
            "M4 shipped reboot + poweroff. M5 added update_apply + wifi_reset. "
            "Issue #280 added prepare_for_gift. Issue #510 added factory_reset. "
            "Each new action requires: a sudoers entry (sudoers/020_litclock-control), "
            "a route handler that consume()s with the matching action string, and "
            "DESIGN.md confirm-modal copy. Adding one without those leaks an "
            "undocumented destructive endpoint."
        )

    def test_issue_returns_token_and_expiry(self):
        store = ConfirmTokenStore()
        token, expires_at = store.issue("reboot")
        assert isinstance(token, str)
        assert len(token) >= 32  # secrets.token_urlsafe(32) → ~43 chars
        assert isinstance(expires_at, int)
        # Wall-clock expiry is roughly now + TTL.
        assert abs(expires_at - (int(time.time()) + TTL_SECONDS)) <= 2

    def test_issue_rejects_invalid_action(self):
        store = ConfirmTokenStore()
        with pytest.raises(ValueError):
            store.issue("delete-everything")

    def test_consume_happy_path(self):
        store = ConfirmTokenStore()
        token, _ = store.issue("reboot")
        # #328 — consume() now returns the monotonic expiry on success
        # (a float, so callers can pass it back to restore() if a
        # pre-side-effect failure path needs to un-consume).
        result = store.consume("reboot", token)
        assert isinstance(result, float)
        assert result > time.monotonic()

    def test_consume_is_single_use(self):
        store = ConfirmTokenStore()
        token, _ = store.issue("reboot")
        assert store.consume("reboot", token) is not None
        assert store.consume("reboot", token) is None, (
            "Single-use is the core security property — a successful consume "
            "must remove the token so a replay (e.g., user double-taps the "
            "primary button) can't fire reboot twice."
        )

    def test_consume_rejects_wrong_action(self):
        store = ConfirmTokenStore()
        token, _ = store.issue("reboot")
        # A token issued for reboot must not consume against poweroff —
        # otherwise the modal could lie about what's about to happen.
        assert store.consume("poweroff", token) is None
        # Subsequent consume against the right action also fails (the
        # token was popped on the wrong-action attempt — fail closed).
        assert store.consume("reboot", token) is None

    def test_consume_rejects_unknown_token(self):
        store = ConfirmTokenStore()
        assert store.consume("reboot", "definitely-not-a-real-token") is None

    def test_token_expires(self):
        # Inject a microscopic TTL so the test doesn't sleep 60s.
        store = ConfirmTokenStore(ttl_seconds=0)
        token, _ = store.issue("reboot")
        # ttl_seconds=0 means expiry is now — by the time consume runs,
        # monotonic clock has advanced past it.
        time.sleep(0.01)
        assert store.consume("reboot", token) is None

    def test_sweep_drops_expired_tokens(self):
        store = ConfirmTokenStore(ttl_seconds=0)
        store.issue("reboot")
        store.issue("poweroff")
        time.sleep(0.01)
        # Issuing a fresh token triggers _sweep() — old ones should be gone.
        store.issue("reboot")
        # No public size accessor; reach in for the regression check.
        assert len(store._tokens) == 1

    # ─── M5 codex F10 — threading.Lock concurrent-consume safety ──────

    def test_concurrent_consume_only_one_succeeds(self):
        """Under waitress threads=4, two POSTs sharing a token race the
        consume() path. Without the Lock, both could pop+check before the
        other deletes — both would see "fresh + bound" and both would
        return True, double-firing the destructive action. With the Lock,
        exactly one wins.
        """
        import threading

        store = ConfirmTokenStore()
        token, _ = store.issue("update_apply")

        results: list[float | None] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            ok = store.consume("update_apply", token)
            with results_lock:
                results.append(ok)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # #328 — consume() now returns the monotonic expiry (float) on
        # success and None on failure. Exactly one must win under contention.
        successes = [r for r in results if r is not None]
        failures = [r for r in results if r is None]
        assert len(successes) == 1, f"exactly one consume must succeed under contention; got {results}"
        assert len(failures) == 7
        assert all(isinstance(s, float) for s in successes)

    def test_concurrent_issue_creates_distinct_tokens(self):
        """No reason for two threads to ever produce the same token —
        secrets.token_urlsafe is collision-resistant — but the lock-
        wrapped issue path must not deadlock under contention.
        """
        import threading

        store = ConfirmTokenStore()
        produced: list[str] = []
        produced_lock = threading.Lock()

        def worker():
            tok, _ = store.issue("wifi_reset")
            with produced_lock:
                produced.append(tok)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(produced) == 16
        assert len(set(produced)) == 16, "all minted tokens must be distinct"


# ─── #328 — consume signature change + restore() round-trip ─────────────────


class TestConfirmTokenRestore:
    """Issue #328: destructive routes consume the confirm token at
    validation time, BEFORE subprocess.run. If a pre-side-effect failure
    path (busy gate, validation error, systemctl returned non-zero before
    dispatch) fires, the token is gone but no side effect happened. User
    retry hits "Confirm token is missing, expired, or already used" —
    masking the real underlying error.

    Fix: consume() returns the original monotonic expiry; restore() lets
    the failing route atomically re-add the token at that expiry so the
    retry surfaces the real error instead of a spurious 401.
    """

    def test_consume_returns_original_monotonic_expiry(self):
        """consume() success returns the same monotonic expiry that issue()
        stored. Pinned because routes pass this back to restore() and a
        drift here would mean the restored token expires at the wrong
        time (either too short — surprise 401 — or too long — TTL
        violation)."""
        store = ConfirmTokenStore(ttl_seconds=300)
        before_issue = time.monotonic()
        token, _expires_at_unix = store.issue("update_apply")
        after_issue = time.monotonic()
        result = store.consume("update_apply", token)
        assert isinstance(result, float)
        # Expiry is roughly now+TTL, bracketed by the issue() call window.
        assert (before_issue + 300) <= result <= (after_issue + 300) + 0.05

    def test_consume_returns_none_on_failure_paths(self):
        """All three failure modes (unknown / wrong-action / expired)
        return None so the route's `expiry is None` gate works."""
        store = ConfirmTokenStore()
        token, _ = store.issue("reboot")

        # Unknown token.
        assert store.consume("reboot", "bogus") is None

        # Wrong action — token consumed (fail-closed) but result is None.
        assert store.consume("poweroff", token) is None

        # Expired token: re-issue with TTL=0.
        expired_store = ConfirmTokenStore(ttl_seconds=0)
        expired_token, _ = expired_store.issue("reboot")
        time.sleep(0.01)
        assert expired_store.consume("reboot", expired_token) is None

    def test_restore_makes_token_consumable_again_with_same_expiry(self):
        """consume → restore → consume cycle yields the same expiry on
        each successful consume — the restored token's TTL is preserved
        from the original issue() call."""
        store = ConfirmTokenStore(ttl_seconds=300)
        token, _ = store.issue("wifi_reset")
        first_expiry = store.consume("wifi_reset", token)
        assert first_expiry is not None
        # Round-trip through restore.
        store.restore("wifi_reset", token, first_expiry)
        second_expiry = store.consume("wifi_reset", token)
        assert second_expiry == first_expiry, "restored token preserves the original expiry"
        # And after the second consume the token is gone again.
        assert store.consume("wifi_reset", token) is None

    def test_restore_rejects_invalid_action(self):
        """Mirrors issue()'s ValueError contract — fail loud on a typo so
        the bug surfaces at the call site instead of silently leaking a
        token under an unmapped action key."""
        store = ConfirmTokenStore()
        with pytest.raises(ValueError):
            store.restore("delete-everything", "anything", time.monotonic() + 60)

    def test_restore_clamps_oversized_expiry_to_ttl(self):
        """Issue #342 I8 — defense-in-depth: a caller passing an expiry
        beyond ``now + TTL_SECONDS`` (e.g. a future refactor that
        hardcodes a stale literal instead of round-tripping consume's
        return) must NOT mint a long-lived token. The clamp caps any
        oversized value at the current TTL ceiling."""
        store = ConfirmTokenStore(ttl_seconds=300)
        token, _ = store.issue("reboot")
        # Consume so the slot is free.
        assert store.consume("reboot", token) is not None
        # Restore with an absurdly far-future expiry — must be clamped.
        far_future = time.monotonic() + 86400  # 24 hours
        store.restore("reboot", token, far_future)
        consumed = store.consume("reboot", token)
        assert consumed is not None, "clamped restore still produces a consumable token"
        # The consumed expiry must be at most TTL_SECONDS ahead of now;
        # use a generous slack on the floor to absorb test-runner jitter.
        assert consumed <= time.monotonic() + 300, "expiry must be clamped to <= now + TTL"
        assert consumed < far_future, "expiry must NOT preserve the oversized value"

    def test_restore_preserves_in_range_expiry(self):
        """Issue #342 I8 — the clamp is a no-op on the happy path. A
        restore with an in-range expiry (the canonical case: round-trip
        from ``consume``) must preserve it exactly."""
        store = ConfirmTokenStore(ttl_seconds=300)
        token, _ = store.issue("wifi_reset")
        first_expiry = store.consume("wifi_reset", token)
        assert first_expiry is not None
        # Round-trip via restore. The expiry is in-range (consume just returned it).
        store.restore("wifi_reset", token, first_expiry)
        second_expiry = store.consume("wifi_reset", token)
        # Exact preservation — the clamp must not perturb a valid expiry.
        assert second_expiry == first_expiry

    def test_restore_is_noop_when_token_already_present(self):
        """Concurrent-restore race: two route handlers might both observe
        a pre-side-effect failure and both try to restore at near-identical
        times. The second restore must NOT overwrite the live token (which
        could swap a fresh issue's expiry with a stale one)."""
        store = ConfirmTokenStore(ttl_seconds=300)
        token, _ = store.issue("prepare_for_gift")
        original_expiry = store.consume("prepare_for_gift", token)
        assert original_expiry is not None

        # First restore lands.
        store.restore("prepare_for_gift", token, original_expiry)
        # Concurrent re-issue races (in production this can't happen with
        # the same token string, but pretend a different caller restored
        # at a DIFFERENT expiry — the late call must be a no-op).
        store.restore("prepare_for_gift", token, original_expiry + 9999.0)

        # The live token retains the first restore's expiry, not the
        # second's. Consume should succeed and return the original.
        consumed = store.consume("prepare_for_gift", token)
        assert consumed == original_expiry, "concurrent restore at a different expiry must NOT clobber the live token"

    def test_consume_restore_consume_cycle_under_threading_contention(self):
        """Mirrors test_concurrent_consume_only_one_succeeds — but the
        winner restores the token under contention and a SECOND wave of
        consumers race for it. Pins that consume/restore play nicely
        under the threading.Lock without deadlock or double-fire.
        """
        import threading

        store = ConfirmTokenStore(ttl_seconds=300)
        token, _ = store.issue("update_apply")

        # Wave 1: 8 concurrent consumers; exactly one wins.
        wave1_results: list[float | None] = []
        wave1_lock = threading.Lock()
        wave1_barrier = threading.Barrier(8)

        def wave1_worker():
            wave1_barrier.wait()
            res = store.consume("update_apply", token)
            with wave1_lock:
                wave1_results.append(res)

        threads = [threading.Thread(target=wave1_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes_w1 = [r for r in wave1_results if r is not None]
        assert len(successes_w1) == 1, f"wave 1: one consume must win; got {wave1_results}"
        winner_expiry = successes_w1[0]

        # The winner restores (simulating a route's pre-side-effect failure).
        store.restore("update_apply", token, winner_expiry)

        # Wave 2: 8 more consumers race for the restored token; exactly one wins.
        wave2_results: list[float | None] = []
        wave2_lock = threading.Lock()
        wave2_barrier = threading.Barrier(8)

        def wave2_worker():
            wave2_barrier.wait()
            res = store.consume("update_apply", token)
            with wave2_lock:
                wave2_results.append(res)

        threads = [threading.Thread(target=wave2_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes_w2 = [r for r in wave2_results if r is not None]
        assert len(successes_w2) == 1, f"wave 2: one consume must win after restore; got {wave2_results}"
        # Restored token must preserve the original expiry.
        assert successes_w2[0] == winner_expiry


# ─── HTTP route tests ───────────────────────────────────────────────────────


@pytest.fixture
def app():
    return create_app({"VERSION_OVERRIDE": "v0.test"})


@pytest.fixture
def client(app):
    return app.test_client()


class TestConfirmTokenRoute:
    def test_happy_path_reboot(self, client):
        response = client.post(
            "/api/system/confirm-token",
            json={"action": "reboot"},
        )
        assert response.status_code == 200
        body = response.json
        assert body["ok"] is True
        assert isinstance(body["token"], str)
        assert isinstance(body["expires_at"], int)

    def test_happy_path_poweroff(self, client):
        response = client.post(
            "/api/system/confirm-token",
            json={"action": "poweroff"},
        )
        assert response.status_code == 200
        assert response.json["ok"] is True

    def test_rejects_invalid_action(self, client):
        response = client.post(
            "/api/system/confirm-token",
            json={"action": "delete-everything"},
        )
        assert response.status_code == 400
        body = response.json
        assert body["ok"] is False
        assert body["error"]["code"] == "invalid_action"

    def test_rejects_missing_action(self, client):
        response = client.post(
            "/api/system/confirm-token",
            json={},
        )
        assert response.status_code == 400
        assert response.json["error"]["code"] == "invalid_action"

    def test_rejects_action_with_wrong_type(self, client):
        response = client.post(
            "/api/system/confirm-token",
            json={"action": ["reboot"]},
        )
        assert response.status_code == 400
        assert response.json["error"]["code"] == "invalid_action"

    def test_rejects_non_object_body(self, client):
        response = client.post(
            "/api/system/confirm-token",
            json=["reboot"],
        )
        assert response.status_code == 400
        assert response.json["error"]["code"] == "invalid_request"

    def test_rejects_non_json_body(self, client):
        response = client.post(
            "/api/system/confirm-token",
            data="action=reboot",
            content_type="application/x-www-form-urlencoded",
        )
        assert response.status_code == 400
        assert response.json["error"]["code"] == "invalid_request"

    def test_error_envelope_shape(self, client):
        """Pin the M4-local envelope shape — story 1.3 routes reuse it.
        Future #254 ratification may drop `ok` from non-2xx; if so, the
        migration is mechanical (`del response['ok']`)."""
        response = client.post(
            "/api/system/confirm-token",
            json={"action": "bogus"},
        )
        body = response.json
        assert set(body.keys()) == {"ok", "error"}
        assert set(body["error"].keys()) == {"code", "message"}
        assert isinstance(body["error"]["message"], str)
        assert body["error"]["message"]  # non-empty

    def test_each_app_has_isolated_store(self):
        """create_app() builds a fresh ConfirmTokenStore — token issued in
        app A must not validate in app B (matters for the test suite + any
        future per-tenant deployment).
        """
        app_a = create_app({"VERSION_OVERRIDE": "v0.test-a"})
        app_b = create_app({"VERSION_OVERRIDE": "v0.test-b"})
        client_a = app_a.test_client()

        token = client_a.post("/api/system/confirm-token", json={"action": "reboot"}).json["token"]

        # Reach into app_b's store and confirm the token isn't there.
        with app_b.app_context():
            from flask import current_app

            store_b = current_app.extensions["confirm_tokens"]
            assert store_b.consume("reboot", token) is None


# ─── #317 item 1 codex P2 — distinct expired vs consumed outcomes ──────────


class TestConsumeClassified:
    """The legacy ``consume()`` collapses all failure modes to ``None``.
    ``consume_classified()`` discriminates ``expired`` (TTL passed) from
    ``consumed`` (single-use guard tombstone hit) from ``invalid`` (unknown
    / wrong-action / malformed). This lets the route handler return a
    distinct HTTP status + slug for each, so the JS refresh-and-retry
    path can gate strictly on ``expired`` — a double-click or bfcached
    resubmit hits ``consumed`` instead of being silently re-fired.
    """

    def test_default_tombstone_ttl_is_600s(self):
        assert TOMBSTONE_TTL_SECONDS == 600, (
            "10 min tombstone covers realistic double-submit / bfcache windows "
            "without growing memory unbounded. Beyond this window the duplicate "
            "POST falls back to 'invalid' — still rejected, just less descriptive."
        )

    def test_consume_classified_ok_on_fresh_token(self):
        store = ConfirmTokenStore()
        token, _ = store.issue("reboot")
        result = store.consume_classified("reboot", token)
        assert result.outcome == "ok"
        assert isinstance(result.expiry, float)
        assert result.expiry > time.monotonic()

    def test_consume_classified_consumed_on_replay(self):
        """Single-use guard: after a successful consume, the SAME token
        replayed reports `consumed` (not `invalid`). This is the new
        contract — the JS uses it to refuse the refresh-and-retry path
        and protect the destructive one-shot action from double-fire."""
        store = ConfirmTokenStore()
        token, _ = store.issue("prepare_for_gift")
        first = store.consume_classified("prepare_for_gift", token)
        assert first.outcome == "ok"
        second = store.consume_classified("prepare_for_gift", token)
        assert second.outcome == "consumed"
        assert second.expiry is None

    def test_consume_classified_expired_on_ttl_passed(self):
        """A token whose TTL has passed (but was never consumed) reports
        `expired` so the JS can refresh-and-retry. Distinct from
        `consumed` (replay) and `invalid` (unknown).

        Implementation note: ``consume_classified()`` looks up the record
        BEFORE running ``_sweep_locked()`` so an expired-but-not-swept
        token is classified as "expired" deterministically (without this
        ordering the sweep would drop the record and we'd report "invalid"
        on the route, which 401s but doesn't trigger the JS retry path
        — the slow-drafter trap would re-emerge).
        """
        store = ConfirmTokenStore(ttl_seconds=0)
        token, _ = store.issue("prepare_for_gift")
        time.sleep(0.01)  # let monotonic clock pass the zero-TTL expiry
        result = store.consume_classified("prepare_for_gift", token)
        assert result.outcome == "expired", (
            f"a TTL-passed unconsumed token must report 'expired' so the JS can refresh-and-retry; got {result.outcome}"
        )
        assert result.expiry is None

    def test_consume_classified_invalid_on_unknown_token(self):
        store = ConfirmTokenStore()
        result = store.consume_classified("reboot", "definitely-not-real")
        assert result.outcome == "invalid"
        assert result.expiry is None

    def test_consume_classified_wrong_action_is_invalid(self):
        """A token bound to one action consumed against another reports
        `invalid` (NOT `consumed`) — the modal copy would lie otherwise.
        The fail-closed pop still happens so a follow-up consume against
        the right action sees a fresh tombstone."""
        store = ConfirmTokenStore()
        token, _ = store.issue("reboot")
        wrong = store.consume_classified("poweroff", token)
        assert wrong.outcome == "invalid"
        # Follow-up consume on the RIGHT action now sees the tombstone
        # (fail-closed pop happened) so it reports `consumed`.
        right_followup = store.consume_classified("reboot", token)
        assert right_followup.outcome == "consumed"

    def test_consume_returns_expiry_when_classified_ok(self):
        """Backward compat: the legacy ``consume()`` wrapper returns the
        expiry on the ``ok`` branch and ``None`` on every other outcome.
        Existing route code that hasn't migrated to consume_classified
        keeps working."""
        store = ConfirmTokenStore()
        token, _ = store.issue("update_apply")
        expiry = store.consume("update_apply", token)
        assert isinstance(expiry, float)
        # Replay returns None (consumed via the wrapper).
        assert store.consume("update_apply", token) is None

    def test_restore_clears_tombstone(self):
        """After restore(), the token is consumable again — the tombstone
        must NOT short-circuit the consume to `consumed`. Otherwise the
        route's restore-on-pre-side-effect-failure path (#328) would 409
        the user's retry instead of running the action."""
        store = ConfirmTokenStore()
        token, _ = store.issue("wifi_reset")
        first = store.consume_classified("wifi_reset", token)
        assert first.outcome == "ok"
        store.restore("wifi_reset", token, first.expiry)
        second = store.consume_classified("wifi_reset", token)
        assert second.outcome == "ok", (
            "restore() must clear the tombstone for this token so the "
            "retry succeeds. Without this, #328's pre-side-effect retry "
            "loop would 409 instead of running."
        )

    def test_tombstone_stores_hash_not_raw_token(self):
        """A tombstone-only memory disclosure cannot replay consumed
        tokens against the live store. The tombstone keys are SHA-256
        hashes (hex digest), not the raw urlsafe-base64 token strings."""
        import hashlib

        store = ConfirmTokenStore()
        token, _ = store.issue("reboot")
        store.consume_classified("reboot", token)
        # Raw token is NOT in the tombstone keys.
        assert token not in store._consumed, "tombstone must hash tokens; raw token leak would enable replay"
        # The HASH of the raw token IS in the tombstone keys.
        expected_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        assert expected_hash in store._consumed

    def test_tombstone_is_swept_after_ttl(self):
        """Lazy GC: a tombstone that has aged past TOMBSTONE_TTL_SECONDS
        is dropped on the next consume/issue. After that, a duplicate
        POST falls back to `invalid` (still rejected — that branch also
        blocks the JS refresh-and-retry, since the retry only triggers
        on `expired`)."""
        store = ConfirmTokenStore(ttl_seconds=300, tombstone_ttl_seconds=0)
        token, _ = store.issue("prepare_for_gift")
        first = store.consume_classified("prepare_for_gift", token)
        assert first.outcome == "ok"
        # Replay IMMEDIATELY — tombstone alive, reports consumed.
        replay_immediate = store.consume_classified("prepare_for_gift", token)
        # tombstone_ttl_seconds=0 may sweep before this consume; accept
        # both branches because production uses 600s and the sweep race
        # never closes there. The critical property pinned below is that
        # eventually the tombstone DOES sweep and we fall back to invalid.
        assert replay_immediate.outcome in ("consumed", "invalid")
        # Force a sweep by issuing a fresh token (any mutator calls _sweep_locked).
        time.sleep(0.01)
        store.issue("reboot")
        # Replay AFTER the tombstone TTL — tombstone gone, falls back to invalid.
        replay_late = store.consume_classified("prepare_for_gift", token)
        assert replay_late.outcome == "invalid", (
            "after the tombstone TTL, a duplicate POST falls back to 'invalid' "
            "(still rejected — the refresh-and-retry path gates on 'expired' only)"
        )


class TestEnvelopeForConsumeOutcome:
    """Pin the HTTP envelope mapping for each consume outcome — the
    route handlers depend on this contract to give the JS distinguishable
    responses for the three failure modes."""

    def test_expired_maps_to_401_expired(self):
        app = create_app({"VERSION_OVERRIDE": "v0.test"})
        with app.app_context():
            response, status = envelope_for_consume_outcome("expired")
        assert status == 401
        body = response.get_json()
        assert body["ok"] is False
        assert body["error"]["code"] == "confirm_token_expired"

    def test_consumed_maps_to_409_consumed(self):
        """409 (not 401) so the JS can branch cleanly on status code AND
        the slug — preventing a future client-side bug where a single
        check on `status === 401` silently re-fires the destructive
        action via the refresh-and-retry path."""
        app = create_app({"VERSION_OVERRIDE": "v0.test"})
        with app.app_context():
            response, status = envelope_for_consume_outcome("consumed")
        assert status == 409
        body = response.get_json()
        assert body["error"]["code"] == "confirm_token_consumed"
        assert "already" in body["error"]["message"].lower()

    def test_invalid_maps_to_401_invalid(self):
        app = create_app({"VERSION_OVERRIDE": "v0.test"})
        with app.app_context():
            response, status = envelope_for_consume_outcome("invalid")
        assert status == 401
        body = response.get_json()
        assert body["error"]["code"] == "confirm_token_invalid"
