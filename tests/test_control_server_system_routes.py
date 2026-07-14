"""Tests for src/control_server/routes/system.py + rate_limit.py.

Covers the destructive M4 surface:

- POST /api/system/reboot + /api/system/poweroff happy paths (subprocess
  mocked; we never actually fork systemctl).
- Confirm token consumed exactly once (replay rejected).
- Rate limit: 6th call within a minute returns 429 with retry_after_s.
- Different remote_addr → separate buckets (a single IP can't starve others).
- subprocess CalledProcessError + TimeoutExpired both return 500 without
  leaking stderr into the response body.
- HTTP `Retry-After` header set on 429.
- /api/system/confirm-token shares the rate-limit bucket (so spamming the
  cheap endpoint can't bypass the cap).
- GET /system still renders (route moved off the index stub onto the system
  blueprint — Story 2.1 fills the template).
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from control_server import create_app  # noqa: E402
from control_server.rate_limit import RateLimiter  # noqa: E402

# ─── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def app():
    return create_app({"VERSION_OVERRIDE": "v0.test"})


@pytest.fixture
def client(app):
    return app.test_client()


def _issue_token(client, action: str) -> str:
    response = client.post("/api/system/confirm-token", json={"action": action})
    assert response.status_code == 200, response.json
    return response.json["token"]


@pytest.fixture
def csrf_token(app) -> str:
    """Mint a CSRF token for the settings/gift writer endpoint. Mirrors
    the fixture in test_control_server_settings.py — duplicated locally so
    the #317 item 7 gift-card tests can POST to /settings (section=gift)
    without pulling in the whole settings test module."""
    from control_server.csrf import CSRF_ACTION  # noqa: PLC0415

    token, _ = app.extensions["csrf_tokens"].issue(CSRF_ACTION)
    return token


@pytest.fixture(autouse=True)
def _reset_shutdown_imminent_flag():
    """#362 D7 — clear the module-level ``_SHUTDOWN_IMMINENT`` flag between
    tests. ``_execute_action`` sets this flag once per shutdown and there
    is no production "unset" path (it's intentionally one-way: a shutdown
    is happening, abort ad-hoc work). Tests that fire reboot/poweroff would
    bleed the flag into the next test in file order, causing spooky
    settings.py ad-hoc-tick aborts in unrelated tests.

    Reset BEFORE each test so the test sees a clean flag; we don't need to
    reset on teardown because the next test's setup will reset again."""
    from control_server.routes import system as system_mod  # noqa: PLC0415

    with system_mod._SHUTDOWN_IMMINENT_LOCK:
        system_mod._SHUTDOWN_IMMINENT = False
    yield


@pytest.fixture(autouse=True)
def _gift_unit_always_loadable():
    """#393 — prepare_for_gift now runs a read-only `systemctl show -p LoadState`
    pre-flight before its destructive location clear. On dev/CI hosts the unit
    isn't installed, so the real probe would return not-found and abort every
    gift test. Patch it to "loadable" module-wide; the dedicated not-loadable
    test overrides this with its own inner patch."""
    with patch("control_server.routes.system._gift_unit_loadable", return_value=True):
        yield


@pytest.fixture
def _bypass_update_busy_for_execute_action():
    """#362 D8 — ``_execute_action`` now checks ``update_state.update_is_busy()``
    before pre-stop. In CI / dev hosts that don't have systemd available,
    the probe returns whatever the test machine reports — bypass it
    explicitly for happy-path tests on the reboot/poweroff routes. The
    "update in progress" branch has its own dedicated tests below."""
    with patch("control_server.routes.system.update_state.update_is_busy", return_value=False):
        yield


# ─── Rate limiter unit tests ────────────────────────────────────────────────


class TestRateLimiter:
    def test_capacity_5_allows_5_immediate_calls(self):
        limiter = RateLimiter(capacity=5, per_seconds=60)
        for _ in range(5):
            allowed, retry_after = limiter.take("1.2.3.4")
            assert allowed is True
            assert retry_after == 0

    def test_sixth_call_in_window_is_rate_limited(self):
        limiter = RateLimiter(capacity=5, per_seconds=60)
        for _ in range(5):
            limiter.take("1.2.3.4")
        allowed, retry_after = limiter.take("1.2.3.4")
        assert allowed is False
        assert retry_after >= 1, (
            "Retry-after must be at least 1 second so the client backs off rather than tight-looping on the boundary."
        )

    def test_separate_ips_have_separate_buckets(self):
        limiter = RateLimiter(capacity=5, per_seconds=60)
        for _ in range(5):
            limiter.take("1.1.1.1")
        # Second IP starts fresh — defends against a single noisy automation
        # bug locking out other clients on the LAN.
        allowed, _ = limiter.take("2.2.2.2")
        assert allowed is True

    def test_eviction_drops_idle_buckets(self):
        """Caught in /review on PR #267: bucket dict was unbounded.
        Idle entries get evicted after EVICTION_AGE_WINDOWS * per_seconds
        of silence. Re-creating on next access produces the same result
        as keeping the stale full-capacity entry, so the eviction is a
        pure space win.
        """
        # Microscopic per_seconds so the test doesn't sleep long. A bucket
        # at capacity is evicted EVICTION_AGE_WINDOWS * 0.01s = 0.1s after
        # last touch.
        limiter = RateLimiter(capacity=5, per_seconds=0.01)
        limiter.take("1.1.1.1")
        limiter.take("2.2.2.2")
        assert len(limiter._buckets) == 2

        time.sleep(0.15)  # past the eviction cutoff
        # Touching any IP triggers the eviction sweep at the top of take().
        limiter.take("3.3.3.3")
        assert "1.1.1.1" not in limiter._buckets
        assert "2.2.2.2" not in limiter._buckets
        assert "3.3.3.3" in limiter._buckets


# ─── /api/system/reboot ─────────────────────────────────────────────────────


class TestRebootRoute:
    @pytest.fixture(autouse=True)
    def _bypass_update_busy_gate(self, _bypass_update_busy_for_execute_action):
        """#362 D8 — pre-stop gate added; bypass for the happy-path tests so
        the test host's systemctl probe doesn't randomly 409. The 'update
        in progress' branch gets its own dedicated test class."""
        yield

    def test_happy_path_invokes_systemctl_reboot_no_block(self, client):
        token = _issue_token(client, "reboot")
        with patch("control_server.routes.system.subprocess.run") as mock_run:
            response = client.post("/api/system/reboot", json={"token": token})

        assert response.status_code == 200, response.json
        assert response.json == {"ok": True, "action": "reboot"}

        # #362 — _execute_action now fires two subprocess.run calls per
        # successful reboot: the pre-stop and then the destructive reboot.
        assert mock_run.call_count == 2, (
            "expected exactly two subprocess.run calls (pre-stop + destructive); "
            f"got {mock_run.call_count}: {mock_run.call_args_list}"
        )
        # Last call must be the destructive reboot with the sudoers-exact argv.
        destructive_args = mock_run.call_args_list[-1].args[0]
        assert destructive_args == ["sudo", "/usr/bin/systemctl", "reboot", "--no-block"], (
            "Argv must match the sudoers entry verbatim — sudoers matches "
            "binary path + arguments exactly. --no-block lets the HTTP "
            "response flush before systemd takes the box down."
        )

    def test_token_is_single_use(self, client):
        """#317 item 1 codex P2: duplicate POST on a consumed token now
        returns 409 ``confirm_token_consumed`` (was 401
        ``confirm_token_invalid``). The single-use guard remains in
        force — the second POST is rejected — but the more-specific
        slug lets the client distinguish "already submitted" from
        "TTL expired" and refuse the refresh-and-retry path that
        would otherwise silently double-fire the destructive action."""
        token = _issue_token(client, "reboot")
        with patch("control_server.routes.system.subprocess.run"):
            first = client.post("/api/system/reboot", json={"token": token})
            second = client.post("/api/system/reboot", json={"token": token})
        assert first.status_code == 200
        assert second.status_code == 409
        assert second.json["error"]["code"] == "confirm_token_consumed"

    def test_missing_token_rejected(self, client):
        with patch("control_server.routes.system.subprocess.run") as mock_run:
            response = client.post("/api/system/reboot", json={})
        assert response.status_code == 401
        assert response.json["error"]["code"] == "confirm_token_invalid"
        mock_run.assert_not_called()

    def test_token_for_other_action_rejected(self, client):
        # poweroff token must not unlock reboot — modal lies otherwise.
        poweroff_token = _issue_token(client, "poweroff")
        with patch("control_server.routes.system.subprocess.run") as mock_run:
            response = client.post("/api/system/reboot", json={"token": poweroff_token})
        assert response.status_code == 401
        mock_run.assert_not_called()


# ─── /api/system/poweroff ───────────────────────────────────────────────────


class TestPoweroffRoute:
    @pytest.fixture(autouse=True)
    def _bypass_update_busy_gate(self, _bypass_update_busy_for_execute_action):
        """#362 D8 — see TestRebootRoute for rationale."""
        yield

    def test_happy_path_invokes_systemctl_poweroff_no_block(self, client):
        token = _issue_token(client, "poweroff")
        with patch("control_server.routes.system.subprocess.run") as mock_run:
            response = client.post("/api/system/poweroff", json={"token": token})

        assert response.status_code == 200, response.json
        assert response.json == {"ok": True, "action": "poweroff"}
        # #362 — pre-stop + destructive. Last call is the destructive one.
        assert mock_run.call_count == 2, mock_run.call_args_list
        destructive_args = mock_run.call_args_list[-1].args[0]
        assert destructive_args == ["sudo", "/usr/bin/systemctl", "poweroff", "--no-block"]


# ─── #362 — Pre-shutdown stop ordering + flag + update-busy + rollback ──────


class TestPreShutdownStop:
    """#362 — verify the locked plan-eng-review decisions D1, D2, D3, D7, D8,
    D9 in the shared ``_execute_action`` dispatcher (reboot + poweroff).

    The race that prompted these tests: ``litclock.timer`` fires every
    minute and enqueues a ``litclock.service`` start job. If the PWA's
    Power off / Restart lands in that ~1s/min window, the queued
    service start lands AFTER our ``litclock-shutdown.service`` ExecStop
    runs — repainting a literary quote over the "Powered Off" splash.
    The fix: synchronously stop both units BEFORE invoking the
    destructive ``systemctl --no-block`` call.
    """

    @pytest.fixture(autouse=True)
    def _bypass_update_busy_gate(self, _bypass_update_busy_for_execute_action):
        """Default: update isn't busy so the dispatcher reaches the pre-stop
        and destructive calls. Tests that exercise the update-busy branch
        override this gate per-call."""
        yield

    # ─── D1 + D2 — stop-before-destructive ordering ─────────────────────────

    def test_reboot_stops_clock_units_before_invoking_systemctl_reboot(self, client):
        """D1 — stop litclock.timer + litclock.service synchronously BEFORE
        invoking ``systemctl reboot --no-block``. Stopping after would
        leave the timer-queued-job race open."""
        token = _issue_token(client, "reboot")
        with patch("control_server.routes.system.subprocess.run") as mock_run:
            response = client.post("/api/system/reboot", json={"token": token})
        assert response.status_code == 200, response.json
        assert mock_run.call_count == 2, mock_run.call_args_list
        # Index 0 is the pre-stop, index 1 is the destructive.
        assert mock_run.call_args_list[0].args[0] == [
            "sudo",
            "/usr/bin/systemctl",
            "stop",
            "litclock.timer",
            "litclock.service",
        ], "pre-stop must fire FIRST and stop BOTH units in one call"
        assert mock_run.call_args_list[1].args[0] == [
            "sudo",
            "/usr/bin/systemctl",
            "reboot",
            "--no-block",
        ]

    def test_poweroff_stops_clock_units_before_invoking_systemctl_poweroff(self, client):
        """D1 — same shape as reboot but for poweroff. Both paths share
        ``_execute_action`` so the dispatcher's behavior must be identical."""
        token = _issue_token(client, "poweroff")
        with patch("control_server.routes.system.subprocess.run") as mock_run:
            response = client.post("/api/system/poweroff", json={"token": token})
        assert response.status_code == 200, response.json
        assert mock_run.call_count == 2, mock_run.call_args_list
        assert mock_run.call_args_list[0].args[0] == [
            "sudo",
            "/usr/bin/systemctl",
            "stop",
            "litclock.timer",
            "litclock.service",
        ]
        assert mock_run.call_args_list[1].args[0] == [
            "sudo",
            "/usr/bin/systemctl",
            "poweroff",
            "--no-block",
        ]

    # ─── D3 — separate, longer timeout on the stop call ────────────────────

    def test_stop_call_uses_stop_timeout_not_action_timeout(self, client):
        """D3 — the pre-stop call uses ``SYSTEMCTL_STOP_TIMEOUT_S`` (15s,
        leaves slack above ``litclock.service``'s ``TimeoutStopSec=10s``);
        the destructive call keeps the existing ``SYSTEMCTL_TIMEOUT_S``
        (5s). Wrong assignment here would either let the dispatcher hang
        15s on a clean reboot or timeout the stop mid-render."""
        from control_server.routes import system as system_mod

        token = _issue_token(client, "reboot")
        with patch("control_server.routes.system.subprocess.run") as mock_run:
            response = client.post("/api/system/reboot", json={"token": token})
        assert response.status_code == 200, response.json
        assert mock_run.call_count == 2
        # The first call (pre-stop) uses the longer stop timeout.
        assert mock_run.call_args_list[0].kwargs["timeout"] == system_mod.SYSTEMCTL_STOP_TIMEOUT_S
        # The second call (destructive) uses the action timeout.
        assert mock_run.call_args_list[1].kwargs["timeout"] == system_mod.SYSTEMCTL_TIMEOUT_S

    def test_systemctl_stop_timeout_constant_is_15(self):
        """C16 — bumped from 12 to 15 to leave 5s slack above
        ``TimeoutStopSec=10s`` for Pi Zero sudo + D-Bus overhead. Pinned
        here so a future trim back below the safety margin trips CI."""
        from control_server.routes import system as system_mod

        assert system_mod.SYSTEMCTL_STOP_TIMEOUT_S == 15, (
            "SYSTEMCTL_STOP_TIMEOUT_S must be 15 (TimeoutStopSec=10s + 5s slack on Pi Zero)"
        )

    # ─── D4 — log + proceed on stop failure ────────────────────────────────

    def test_stop_failure_logs_and_proceeds_to_destructive_action(self, client, caplog):
        """D4 — stop call returns non-zero (typical case: timer was already
        inactive; less typical: sudoers rejection). Must log + proceed to
        the destructive call. Denying the user's Power off because a best-
        effort stop failed is worse UX than the cosmetic race we're
        trying to close."""
        import logging

        token = _issue_token(client, "reboot")

        call_count = {"n": 0}

        def fake_run(argv, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First call: the pre-stop. Make it fail.
                raise subprocess.CalledProcessError(returncode=1, cmd=argv, stderr=b"failed to stop")
            # Second call: the destructive. Let it succeed.
            from subprocess import CompletedProcess

            return CompletedProcess(argv, 0, b"", b"")

        with (
            caplog.at_level(logging.WARNING, logger="control_server"),
            patch("control_server.routes.system.subprocess.run", side_effect=fake_run),
        ):
            response = client.post("/api/system/reboot", json={"token": token})

        # Despite the stop failure, the destructive call still fired and
        # succeeded — user's request is honored.
        assert response.status_code == 200, response.json
        assert call_count["n"] == 2, "destructive call must still fire after stop failure"
        # The warning is logged.
        assert any("pre-shutdown stop" in r.message for r in caplog.records), (
            f"expected pre-shutdown-stop warning in caplog; saw: {[r.message for r in caplog.records]}"
        )

    def test_stop_timeout_logs_and_proceeds_to_destructive_action(self, client, caplog):
        """D4 — stop call times out (wedged render >15s). Same handling as
        non-zero return: log + proceed."""
        import logging

        token = _issue_token(client, "reboot")

        call_count = {"n": 0}

        def fake_run(argv, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise subprocess.TimeoutExpired(cmd=argv, timeout=15)
            from subprocess import CompletedProcess

            return CompletedProcess(argv, 0, b"", b"")

        with (
            caplog.at_level(logging.WARNING, logger="control_server"),
            patch("control_server.routes.system.subprocess.run", side_effect=fake_run),
        ):
            response = client.post("/api/system/reboot", json={"token": token})

        assert response.status_code == 200, response.json
        assert call_count["n"] == 2
        assert any("timed out" in r.message for r in caplog.records), "expected timeout warning in caplog"

    # ─── D7 — _SHUTDOWN_IMMINENT flag set before pre-stop ──────────────────

    def test_reboot_marks_shutdown_imminent_before_pre_stop(self, client):
        """D7 — process-local flag must be set BEFORE the pre-stop call so
        any settings.py ad-hoc thread that wakes up during the stop
        window sees ``True`` and aborts instead of re-firing a render
        past our pre-stop."""
        from control_server.routes import system as system_mod

        token = _issue_token(client, "reboot")
        # Sanity: flag starts clean (the autouse reset fixture).
        assert system_mod.is_shutdown_imminent() is False

        observed = {"flag_at_stop": None}

        def fake_run(argv, *args, **kwargs):
            # First call is the pre-stop; record the flag state at that
            # moment so we can prove the mark ran before the stop.
            # Codex post-review TOCTOU fix: mark + stop now happen under
            # one lock acquisition, so this side_effect runs while THIS
            # thread holds _SHUTDOWN_IMMINENT_LOCK. Read the flag DIRECTLY
            # instead of via is_shutdown_imminent() — the latter tries to
            # re-acquire the same Lock and deadlocks.
            if observed["flag_at_stop"] is None:
                observed["flag_at_stop"] = system_mod._SHUTDOWN_IMMINENT
            from subprocess import CompletedProcess

            return CompletedProcess(argv, 0, b"", b"")

        with patch("control_server.routes.system.subprocess.run", side_effect=fake_run):
            response = client.post("/api/system/reboot", json={"token": token})

        assert response.status_code == 200, response.json
        assert observed["flag_at_stop"] is True, (
            "shutdown-imminent flag must be set BEFORE pre-stop subprocess.run fires"
        )
        # Codex post-review Finding 2 fix: destructive succeeded so no
        # rollback fires, but successful destructive doesn't clear the
        # flag either — the process is shutting down, the flag dies with
        # it. Test passes by checking the post-call state matches.
        assert system_mod.is_shutdown_imminent() is True

    def test_poweroff_marks_shutdown_imminent_before_pre_stop(self, client):
        """D7 — same ordering for poweroff."""
        from control_server.routes import system as system_mod

        token = _issue_token(client, "poweroff")
        assert system_mod.is_shutdown_imminent() is False

        observed = {"flag_at_stop": None}

        def fake_run(argv, *args, **kwargs):
            # See codex post-review fix in test_reboot_marks_shutdown_imminent_before_pre_stop:
            # mark + stop hold _SHUTDOWN_IMMINENT_LOCK; reading the flag via
            # is_shutdown_imminent() would re-acquire the same Lock and
            # deadlock this thread. Read the flag directly.
            if observed["flag_at_stop"] is None:
                observed["flag_at_stop"] = system_mod._SHUTDOWN_IMMINENT
            from subprocess import CompletedProcess

            return CompletedProcess(argv, 0, b"", b"")

        with patch("control_server.routes.system.subprocess.run", side_effect=fake_run):
            response = client.post("/api/system/poweroff", json={"token": token})
        assert response.status_code == 200, response.json
        assert observed["flag_at_stop"] is True

    # ─── D8 — update_is_busy() gating ──────────────────────────────────────

    def test_reboot_returns_409_when_update_busy(self, client):
        """D8 — pre-check that an update isn't running before pre-stop +
        destructive call fire. update.sh's Phase 7 fires
        ``systemctl start litclock.service`` + ``systemctl start
        litclock.timer``; if we pre-stop while update.sh is mid-flight,
        the race re-opens via that path. Mirror the existing wifi-reset /
        prepare-for-gift 409 gate."""
        token = _issue_token(client, "reboot")
        with (
            patch("control_server.routes.system.update_state.update_is_busy", return_value=True),
            patch("control_server.routes.system.subprocess.run") as mock_run,
        ):
            response = client.post("/api/system/reboot", json={"token": token})
        assert response.status_code == 409, response.json
        assert response.json["error"]["code"] == "update_in_progress"
        # NEITHER pre-stop NOR destructive must fire.
        mock_run.assert_not_called()

    def test_poweroff_returns_409_when_update_busy(self, client):
        """D8 — same gate for poweroff."""
        token = _issue_token(client, "poweroff")
        with (
            patch("control_server.routes.system.update_state.update_is_busy", return_value=True),
            patch("control_server.routes.system.subprocess.run") as mock_run,
        ):
            response = client.post("/api/system/poweroff", json={"token": token})
        assert response.status_code == 409
        assert response.json["error"]["code"] == "update_in_progress"
        mock_run.assert_not_called()

    def test_update_busy_restores_token_for_retry(self, client):
        """D8 (#328 parity) — a 409 update_in_progress is pre-side-effect.
        Restore the token so the user's retry after the update finishes
        works with the same open page."""
        token = _issue_token(client, "reboot")

        with (
            patch("control_server.routes.system.update_state.update_is_busy", return_value=True),
            patch("control_server.routes.system.subprocess.run"),
        ):
            first = client.post("/api/system/reboot", json={"token": token})
        assert first.status_code == 409

        # Retry with the SAME token once the update has finished.
        with (
            patch("control_server.routes.system.update_state.update_is_busy", return_value=False),
            patch("control_server.routes.system.subprocess.run") as mock_run,
        ):
            second = client.post("/api/system/reboot", json={"token": token})
        assert second.status_code == 200, second.json
        # Two calls: pre-stop + destructive.
        assert mock_run.call_count == 2

    def test_update_busy_does_NOT_set_shutdown_imminent_flag(self, client):
        """D7 + D8 interaction — when 409 fires we MUST NOT have marked
        shutdown imminent. Otherwise an in-flight settings.py ad-hoc
        thread would abort even though the user's reboot was refused."""
        from control_server.routes import system as system_mod

        token = _issue_token(client, "reboot")
        assert system_mod.is_shutdown_imminent() is False
        with (
            patch("control_server.routes.system.update_state.update_is_busy", return_value=True),
            patch("control_server.routes.system.subprocess.run"),
        ):
            response = client.post("/api/system/reboot", json={"token": token})
        assert response.status_code == 409
        # Flag stays False — D8 gate fired before D7's _mark_shutdown_imminent.
        assert system_mod.is_shutdown_imminent() is False, (
            "_mark_shutdown_imminent must NOT fire when D8 gate refuses the request"
        )

    # ─── D9 — destructive CalledProcessError triggers rollback restart-timer

    def test_destructive_failure_triggers_timer_restart_rollback(self, client):
        """D9 — destructive ``systemctl reboot`` returns non-zero (typical
        cases: unit not found, sudoers misconfig, masked). The pre-stop
        already stopped litclock.timer + litclock.service; without the
        rollback the clock is silently stopped until the next boot. Roll
        back via ``systemctl restart litclock.timer`` (existing sudoers
        grant — no new privilege)."""
        token = _issue_token(client, "reboot")

        call_log: list[list[str]] = []

        def fake_run(argv, *args, **kwargs):
            call_log.append(list(argv))
            # Pre-stop: succeed. Destructive: fail. Rollback: succeed.
            if argv[2:5] == ["stop", "litclock.timer", "litclock.service"]:
                from subprocess import CompletedProcess

                return CompletedProcess(argv, 0, b"", b"")
            if "reboot" in argv:
                raise subprocess.CalledProcessError(returncode=1, cmd=argv, stderr=b"x")
            # The rollback restart.
            from subprocess import CompletedProcess

            return CompletedProcess(argv, 0, b"", b"")

        with patch("control_server.routes.system.subprocess.run", side_effect=fake_run):
            response = client.post("/api/system/reboot", json={"token": token})

        assert response.status_code == 500
        assert response.json["error"]["code"] == "systemctl_failed"
        # Three subprocess.run calls: pre-stop, destructive (failed), rollback.
        assert len(call_log) == 3, f"expected pre-stop + destructive + rollback; got {call_log}"
        # The rollback call uses the existing `restart litclock.timer` sudoers grant.
        assert call_log[2] == ["sudo", "/usr/bin/systemctl", "restart", "litclock.timer"], (
            f"rollback must restart litclock.timer; got {call_log[2]}"
        )

    # Removed: test_destructive_timeout_does_NOT_trigger_rollback — codex
    # post-review Finding 4 inverted the plan's original "no rollback on
    # timeout" decision. The new behavior IS to rollback on timeout (the
    # destructive may have wedged pre-dispatch, leaving the clock stopped).
    # The new behavior is pinned by:
    # TestShutdownPostReviewFixes::test_destructive_timeout_triggers_rollback_restart_timer

    def test_destructive_failure_rollback_swallows_its_own_errors(self, client, caplog):
        """D9 — rollback is best-effort. If the rollback ``systemctl
        restart litclock.timer`` itself fails (e.g., timer is masked),
        the request must STILL return 500 (the destructive failed) and
        the rollback failure must be logged at warning level — not
        re-raised, which would surface as a 500 with a misleading code."""
        import logging

        token = _issue_token(client, "reboot")

        def fake_run(argv, *args, **kwargs):
            if argv[2] == "stop":
                from subprocess import CompletedProcess

                return CompletedProcess(argv, 0, b"", b"")
            if argv[2] == "reboot":
                raise subprocess.CalledProcessError(returncode=1, cmd=argv, stderr=b"x")
            # The rollback also fails.
            raise subprocess.CalledProcessError(returncode=1, cmd=argv, stderr=b"rollback failed")

        with (
            caplog.at_level(logging.WARNING, logger="control_server"),
            patch("control_server.routes.system.subprocess.run", side_effect=fake_run),
        ):
            response = client.post("/api/system/reboot", json={"token": token})

        # Response is still the destructive-failure 500 — rollback errors
        # don't surface to the client.
        assert response.status_code == 500
        assert response.json["error"]["code"] == "systemctl_failed"
        # But the rollback failure IS logged so an operator can find it.
        assert any("rollback restart litclock.timer failed" in r.message for r in caplog.records), (
            f"expected rollback-failure warning in caplog; got {[r.message for r in caplog.records]}"
        )

    # ─── §3 — helper unit tests on _stop_clock_units_for_shutdown ─────────

    def test_helper_calls_systemctl_stop_with_both_units(self, app):
        """Direct invocation: argv must be exactly `sudo /usr/bin/systemctl
        stop litclock.timer litclock.service`. Sudoers matches binary +
        args verbatim, so this is the load-bearing contract."""
        from control_server.routes import system as system_mod

        with app.app_context():
            with patch("control_server.routes.system.subprocess.run") as mock_run:
                system_mod._stop_clock_units_for_shutdown()
        mock_run.assert_called_once()
        assert mock_run.call_args.args[0] == [
            "sudo",
            "/usr/bin/systemctl",
            "stop",
            "litclock.timer",
            "litclock.service",
        ]

    def test_helper_uses_check_true_and_capture_output(self, app):
        """Match the dispatcher pattern: check=True so CalledProcessError
        fires on non-zero; capture_output=True so stderr is grabbed
        without leaking into the response body."""
        from control_server.routes import system as system_mod

        with app.app_context():
            with patch("control_server.routes.system.subprocess.run") as mock_run:
                system_mod._stop_clock_units_for_shutdown()
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("check") is True
        assert kwargs.get("capture_output") is True

    def test_helper_uses_stop_timeout(self, app):
        from control_server.routes import system as system_mod

        with app.app_context():
            with patch("control_server.routes.system.subprocess.run") as mock_run:
                system_mod._stop_clock_units_for_shutdown()
        assert mock_run.call_args.kwargs.get("timeout") == system_mod.SYSTEMCTL_STOP_TIMEOUT_S

    def test_helper_swallows_called_process_error_with_warning(self, app, caplog):
        """Best-effort: a non-zero return must NOT re-raise — caller will
        proceed to the destructive subprocess.run. Pin via direct
        invocation so a future refactor that re-raises here trips CI."""
        import logging

        from control_server.routes import system as system_mod

        with (
            app.app_context(),
            caplog.at_level(logging.WARNING, logger="control_server"),
            patch(
                "control_server.routes.system.subprocess.run",
                side_effect=subprocess.CalledProcessError(returncode=5, cmd=["sudo"], stderr=b"masked"),
            ),
        ):
            # Must not raise.
            system_mod._stop_clock_units_for_shutdown()
        assert any("pre-shutdown stop returned non-zero" in r.message for r in caplog.records)

    def test_helper_swallows_timeout_with_warning(self, app, caplog):
        """Same swallow contract for TimeoutExpired (wedged-render path)."""
        import logging

        from control_server.routes import system as system_mod

        with (
            app.app_context(),
            caplog.at_level(logging.WARNING, logger="control_server"),
            patch(
                "control_server.routes.system.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd=["sudo"], timeout=15),
            ),
        ):
            system_mod._stop_clock_units_for_shutdown()
        assert any("pre-shutdown stop timed out" in r.message for r in caplog.records)

    # ─── §1.7 / §1.8 — pre-stop NOT called for non-shutdown routes ─────────

    def test_stop_NOT_called_for_prepare_for_gift(self, client, tmp_path):
        """``prepare_for_gift`` lives on a different handler and routes
        through ``reset-setup.sh --gift-mode``, which already stops both
        units in its Step 1 (no race). The /api/system/prepare-for-gift
        endpoint must NOT call ``_stop_clock_units_for_shutdown``."""
        token = _issue_token(client, "prepare_for_gift")
        msg_path = tmp_path / "gift-message"
        with (
            patch("control_server.routes.system.update_state.update_is_busy", return_value=False),
            patch("control_server.routes.system.GIFT_MESSAGE_PATH", str(msg_path)),
            patch("control_server.routes.system.subprocess.run") as mock_run,
        ):
            response = client.post(
                "/api/system/prepare-for-gift",
                json={"token": token, "message": "hi"},
            )
        assert response.status_code == 200, response.json
        # The gift route fires exactly ONE subprocess.run call — the
        # `systemctl start litclock-prepare-for-gift.service`. No pre-stop.
        for call in mock_run.call_args_list:
            argv = call.args[0]
            assert argv[2:5] != ["stop", "litclock.timer", "litclock.service"], (
                f"prepare_for_gift must NOT invoke the #362 pre-stop; saw {argv}"
            )

    def test_stop_NOT_called_for_wifi_reset(self, client):
        """``wifi_reset`` doesn't shut down the device — it wipes WiFi and
        restarts firstboot. The clock keeps running. Calling
        ``_stop_clock_units_for_shutdown`` here would needlessly blank
        the e-ink for ~10s during a setup retry."""
        token = _issue_token(client, "wifi_reset")
        with (
            patch("control_server.routes.wifi.update_state.update_is_busy", return_value=False),
            patch("control_server.routes.wifi.subprocess.run") as mock_run,
        ):
            response = client.post("/api/wifi/reset", json={"token": token})
        # The wifi.reset route should succeed without ever touching the
        # #362 pre-stop. The route lives on the wifi blueprint and uses
        # control_server.routes.wifi.subprocess.run (not system.subprocess.run).
        assert response.status_code == 200, response.json
        for call in mock_run.call_args_list:
            argv = call.args[0]
            assert argv[2:5] != ["stop", "litclock.timer", "litclock.service"], (
                f"wifi.reset must NOT invoke the #362 pre-stop; saw {argv}"
            )


# ─── Codex post-review regression tests (#362 round 2) ─────────────────────


class TestShutdownPostReviewFixes:
    """Regression tests for the four HIGH findings codex caught on the
    initial #362 PR (TOCTOU + flag-stuck-True + silent rollback non-zero +
    missing timeout rollback). Each test pins the specific failure mode so
    a future refactor can't silently reintroduce it.
    """

    @pytest.fixture(autouse=True)
    def _bypass_update_busy_gate(self, _bypass_update_busy_for_execute_action):
        """Default: update isn't busy so the dispatcher reaches the pre-stop
        and destructive calls. Matches the pattern in TestPreShutdownStop.
        """
        yield

    def test_mark_and_stop_holds_lock_atomically_during_subprocess(self, monkeypatch):
        """Codex Finding 1 (TOCTOU) regression.

        ``_mark_shutdown_imminent_and_stop_units`` MUST hold
        ``_SHUTDOWN_IMMINENT_LOCK`` for the duration of BOTH the flag mark
        AND the pre-stop subprocess.run. If the lock is released between
        them, a settings.py ad-hoc tick using ``shutdown_imminent_check()``
        could check the flag, get False, and fire ``systemctl start
        litclock.service`` AFTER our mark + stop, defeating the race
        closure the PR is meant to achieve.
        """
        from control_server.routes import system as system_mod  # noqa: PLC0415

        lock_held_during_subprocess = []
        flag_during_subprocess = []

        def fake_subprocess_run(*args, **kwargs):
            # Capture lock state at the moment subprocess.run executes.
            # If the lock is NOT held here, the TOCTOU window is open.
            lock_held_during_subprocess.append(system_mod._SHUTDOWN_IMMINENT_LOCK.locked())
            # Flag must be True at this point (mark ran before stop).
            # Read directly (not via is_shutdown_imminent) to avoid
            # deadlocking on the held lock.
            flag_during_subprocess.append(system_mod._SHUTDOWN_IMMINENT)
            return subprocess.CompletedProcess(args=args[0], returncode=0, stdout=b"", stderr=b"")

        monkeypatch.setattr(system_mod.subprocess, "run", fake_subprocess_run)

        system_mod._mark_shutdown_imminent_and_stop_units()

        assert lock_held_during_subprocess == [True], (
            "_SHUTDOWN_IMMINENT_LOCK must be held during pre-stop subprocess.run "
            "(codex Finding 1 TOCTOU regression — concurrent ad-hoc ticks would "
            "race a fresh litclock.service start past our pre-stop)"
        )
        assert flag_during_subprocess == [True], (
            "_SHUTDOWN_IMMINENT must be set BEFORE the pre-stop subprocess.run runs"
        )

    def test_shutdown_imminent_check_blocks_concurrent_pre_stop(self, monkeypatch):
        """Codex Finding 1 (TOCTOU) regression — consumer side.

        While a settings.py-style consumer holds the lock via
        ``shutdown_imminent_check()``, a concurrent call to
        ``_mark_shutdown_imminent_and_stop_units()`` MUST block on the
        lock until the consumer's critical section exits.
        """
        import threading  # noqa: PLC0415
        import time  # noqa: PLC0415

        from control_server.routes import system as system_mod  # noqa: PLC0415

        consumer_released = threading.Event()
        mark_completed = threading.Event()

        def consumer():
            with system_mod.shutdown_imminent_check() as imminent:
                assert imminent is False  # initial state
                # Simulate a slow critical section (fires the subprocess.run
                # for systemctl start). Hold the lock for ~200ms; the mark
                # thread must block during this window.
                time.sleep(0.2)
                consumer_released.set()

        consumer_t = threading.Thread(target=consumer, daemon=True)
        consumer_t.start()

        # Give the consumer time to acquire the lock.
        time.sleep(0.05)

        # Mock subprocess so pre-stop returns immediately.
        monkeypatch.setattr(
            system_mod.subprocess,
            "run",
            lambda *a, **kw: subprocess.CompletedProcess(args=a[0], returncode=0, stdout=b"", stderr=b""),
        )

        def marker():
            system_mod._mark_shutdown_imminent_and_stop_units()
            mark_completed.set()

        marker_t = threading.Thread(target=marker, daemon=True)
        marker_t.start()

        # Marker must NOT complete while consumer holds the lock.
        assert not mark_completed.wait(timeout=0.1), (
            "_mark_shutdown_imminent_and_stop_units completed while a consumer "
            "held the lock — TOCTOU race re-opened (codex Finding 1)"
        )

        # Consumer eventually releases.
        assert consumer_released.wait(timeout=1.0)
        consumer_t.join(timeout=1.0)

        # Now marker can complete.
        assert mark_completed.wait(timeout=1.0), "marker never completed after consumer released the lock"
        marker_t.join(timeout=1.0)

    def test_destructive_failure_clears_shutdown_imminent_flag(self, client):
        """Codex Finding 2 regression.

        After ``_execute_action``'s destructive systemctl returns non-zero,
        the rollback path MUST clear ``_SHUTDOWN_IMMINENT`` so future
        settings ad-hoc ticks resume firing. The original D7 implementation
        left the flag stuck True forever — every settings save would
        silently skip its immediate render until process restart.
        """
        from control_server.routes import system as system_mod  # noqa: PLC0415

        with patch("control_server.routes.system.subprocess.run") as mock_run:
            # Sequence: pre-stop OK, destructive fails, rollback OK.
            mock_run.side_effect = [
                subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b""),
                subprocess.CalledProcessError(1, "systemctl", stderr=b"unit not found"),
                subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b""),
            ]

            token = _issue_token(client, "reboot")
            resp = client.post("/api/system/reboot", json={"token": token})
            assert resp.status_code == 500

        # Critical assertion: flag is False after destructive failure +
        # rollback. Without the codex Finding 2 fix, this would be True.
        assert system_mod.is_shutdown_imminent() is False, (
            "_SHUTDOWN_IMMINENT must be cleared after destructive failure "
            "(codex Finding 2 — flag-stuck-True regression)"
        )

    def test_rollback_non_zero_returncode_logs_warning(self, client, caplog):
        """Codex Finding 3 regression.

        ``_rollback_failed_shutdown_attempt`` uses ``subprocess.run(check=False)``
        for the rollback restart. A non-zero returncode (sudoers mismatch,
        masked unit, etc.) does NOT raise CalledProcessError, so the
        original ``except Exception`` clause never fires. The helper must
        inspect ``result.returncode`` explicitly and log a warning.
        """
        import logging  # noqa: PLC0415

        caplog.set_level(logging.WARNING)

        with patch("control_server.routes.system.subprocess.run") as mock_run:
            # Sequence: pre-stop OK, destructive fails, rollback non-zero.
            mock_run.side_effect = [
                subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b""),
                subprocess.CalledProcessError(1, "systemctl", stderr=b"destructive failed"),
                subprocess.CompletedProcess(
                    args=["sudo", "/usr/bin/systemctl", "restart", "litclock.timer"],
                    returncode=4,
                    stdout=b"",
                    stderr=b"Unit not loaded.",
                ),
            ]

            token = _issue_token(client, "poweroff")
            resp = client.post("/api/system/poweroff", json={"token": token})
            assert resp.status_code == 500

        # Critical assertion: the rollback non-zero returncode produced a
        # warning log. Without the codex Finding 3 fix, the silent failure
        # would leave operators with no signal.
        warning_messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("rollback restart litclock.timer returned non-zero" in m for m in warning_messages), (
            f"expected rollback non-zero warning; got: {warning_messages}"
        )

    def test_destructive_timeout_triggers_rollback_restart_timer(self, client):
        """Codex Finding 4 regression.

        The original implementation skipped the rollback in the
        TimeoutExpired branch on the reasoning "destructive may have
        dispatched, don't double-fire." But a timeout can ALSO be a
        pre-dispatch wedge (sudo / D-Bus / systemd issue), in which case
        the box stays up with timer + service stopped and ad-hoc ticks
        suppressed forever. The fix: rollback on timeout too.
        """
        from control_server.routes import system as system_mod  # noqa: PLC0415

        with patch("control_server.routes.system.subprocess.run") as mock_run:
            # Sequence: pre-stop OK, destructive times out, rollback OK.
            mock_run.side_effect = [
                subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b""),
                subprocess.TimeoutExpired(cmd="systemctl", timeout=5),
                subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b""),
            ]

            token = _issue_token(client, "reboot")
            resp = client.post("/api/system/reboot", json={"token": token})
            assert resp.status_code == 500

            # Three subprocess.run calls: pre-stop + destructive (timeout) + rollback
            assert mock_run.call_count == 3, (
                f"expected 3 subprocess.run calls (pre-stop + destructive + rollback); "
                f"got {mock_run.call_count}: {mock_run.call_args_list}"
            )
            rollback_args = mock_run.call_args_list[2].args[0]
            assert rollback_args == ["sudo", "/usr/bin/systemctl", "restart", "litclock.timer"], (
                f"expected rollback call to be `systemctl restart litclock.timer`; got {rollback_args}"
            )

        # Flag cleared after timeout rollback (same as CalledProcessError path).
        assert system_mod.is_shutdown_imminent() is False, (
            "_SHUTDOWN_IMMINENT must be cleared after destructive timeout + rollback (codex Finding 4 regression)"
        )

    def test_pre_stop_oserror_does_not_propagate_and_destructive_still_fires(self, client):
        """Codex final-pass Finding 1 (pre-stop side) regression.

        ``_stop_clock_units_for_shutdown`` must catch OSError (e.g.,
        FileNotFoundError if sudo binary missing, fork failures under
        process-table pressure). Without this catch, the OSError
        propagates out of ``_mark_shutdown_imminent_and_stop_units`` with
        ``_SHUTDOWN_IMMINENT`` already True. ``_execute_action``'s try
        block is for the destructive call only; an OSError raised BEFORE
        the try block becomes an unhandled 500 with the flag stuck True
        and no rollback fired.

        Fixed by catching OSError in the pre-stop helper: log + proceed.
        The destructive call still fires, the request succeeds with 200.
        """
        with patch("control_server.routes.system.subprocess.run") as mock_run:
            # Pre-stop raises OSError; destructive succeeds.
            mock_run.side_effect = [
                OSError("[Errno 12] Cannot allocate memory"),
                subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b""),
            ]

            token = _issue_token(client, "reboot")
            resp = client.post("/api/system/reboot", json={"token": token})

            # Request succeeds: pre-stop logged + skipped, destructive fired.
            assert resp.status_code == 200, resp.json
            assert mock_run.call_count == 2, f"expected pre-stop attempt + destructive; got {mock_run.call_count} calls"

    def test_destructive_oserror_triggers_rollback_and_clears_flag(self, client):
        """Codex final-pass Finding 1 (destructive side) — symmetric with
        the pre-stop OSError fix.

        If the destructive ``systemctl reboot/poweroff --no-block`` raises
        OSError (FileNotFoundError, fork failure, etc.), the request must:
          1. Restore the consumed token (pre-side-effect failure)
          2. Rollback (restart litclock.timer + clear _SHUTDOWN_IMMINENT)
          3. Return 500

        Without this, the OSError propagates as an unhandled 500 with the
        flag stuck True — same regression class as the pre-stop case.
        """
        from control_server.routes import system as system_mod  # noqa: PLC0415

        with patch("control_server.routes.system.subprocess.run") as mock_run:
            # Sequence: pre-stop OK, destructive raises OSError, rollback OK.
            mock_run.side_effect = [
                subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b""),
                OSError("[Errno 2] No such file or directory: '/usr/bin/systemctl'"),
                subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b""),
            ]

            token = _issue_token(client, "reboot")
            resp = client.post("/api/system/reboot", json={"token": token})

            assert resp.status_code == 500, resp.json
            # Three subprocess.run calls: pre-stop + destructive (OSError) + rollback
            assert mock_run.call_count == 3, (
                f"expected 3 subprocess.run calls (pre-stop + destructive + rollback); "
                f"got {mock_run.call_count}: {mock_run.call_args_list}"
            )
            rollback_args = mock_run.call_args_list[2].args[0]
            assert rollback_args == ["sudo", "/usr/bin/systemctl", "restart", "litclock.timer"], (
                f"expected rollback to restart litclock.timer; got {rollback_args}"
            )

        # Flag cleared by the rollback path.
        assert system_mod.is_shutdown_imminent() is False, (
            "_SHUTDOWN_IMMINENT must be cleared after destructive OSError + rollback"
        )


# ─── /api/system/prepare-for-gift (#280) ────────────────────────────────────


class TestPrepareForGiftRoute:
    """#280: PWA-triggered "Prepare for Gifting" flow. Writes the optional
    welcome message to /run/litclock/gift-message, then invokes the
    litclock-prepare-for-gift.service systemd unit (which runs reset-setup.sh
    --gift-mode, wipes WiFi, paints the welcome on the e-ink, powers off).
    """

    @pytest.fixture(autouse=True)
    def _bypass_update_busy_gate(self):
        """#316 /review fix added an update-busy pre-check that mirrors
        wifi.reset. In the test env, update_state's systemctl probe runs
        against the dev host and may return any state — bypass it for the
        happy-path tests. The 'update in progress' branch gets its own
        dedicated test below."""
        with patch("control_server.routes.system.update_state.update_is_busy", return_value=False):
            yield

    @pytest.fixture(autouse=True)
    def _noop_tz_reset(self):
        """#396 added a best-effort `sudo timedatectl set-timezone UTC` to
        prepare_for_gift, before the systemctl dispatch. It uses the same
        module-level subprocess.run these tests patch + count, so leaving it
        live would turn every `mock_run.assert_called_once()` (which means "the
        systemctl dispatch fired exactly once") into a 2-call failure. No-op it
        here so the dispatch/token/message tests stay focused; the tz-reset
        behavior gets its own coverage in TestPrepareForGiftTimezoneReset."""
        with patch("control_server.routes.system._gift_reset_timezone_to_utc"):
            yield

    @pytest.fixture(autouse=True)
    def gift_env_file(self, app, tmp_path):
        """#393: prepare_for_gift now clears the gifter's location from env.sh
        synchronously before dispatching the teardown unit. Point ENV_FILE at a
        throwaway env.sh (seeded with a location) so tests exercise that write
        without clobbering the real /home/pi/litclock/env.sh create_app default.
        Returned so tests can assert on the post-clear contents."""
        env_file = tmp_path / "env.sh"
        env_file.write_text(
            "export OPENWEATHERMAP_APIKEY=secret123\n"
            "export WEATHER_LATITUDE=30.27\n"
            "export WEATHER_LONGITUDE=-97.74\n"
            'export WEATHER_LOCATION_NAME="Austin, TX"\n'
            "export WEATHER_UNITS=imperial\n"
        )
        app.config["ENV_FILE"] = str(env_file)
        return env_file

    def test_clears_location_from_env_before_dispatch(self, client, tmp_path, gift_env_file):
        """#393: the gifter's coordinates + city must be wiped from env.sh
        BEFORE the teardown unit is dispatched — that's the only point the PWA
        connection is still alive to report a failure, and it makes the leak
        impossible regardless of the script's later wipe. The non-allowlisted
        API key is left for the script's shell writer."""
        token = _issue_token(client, "prepare_for_gift")
        msg_path = tmp_path / "gift-message"
        with (
            patch("control_server.routes.system.GIFT_MESSAGE_PATH", str(msg_path)),
            patch("control_server.routes.system.subprocess.run") as mock_run,
        ):
            response = client.post("/api/system/prepare-for-gift", json={"token": token})

        assert response.status_code == 200, response.json
        # The three location keys must be empty post-clear.
        from config import load_config

        cfg = load_config(str(gift_env_file))
        assert cfg.get("WEATHER_LATITUDE", "") == ""
        assert cfg.get("WEATHER_LONGITUDE", "") == ""
        assert cfg.get("WEATHER_LOCATION_NAME", "") == ""
        # Dispatch still happened after the successful clear.
        mock_run.assert_called_once()

    def test_lock_timeout_aborts_and_reports_to_pwa(self, client, tmp_path):
        """#393 core: if the synchronous location clear can't get the env.sh
        flock, abort BEFORE dispatching the teardown — return 504 so the PWA
        (still connected) shows an error instead of a false 'pack and ship',
        stage no message, dispatch nothing, and restore the token for retry."""
        token = _issue_token(client, "prepare_for_gift")
        msg_path = tmp_path / "gift-message"

        def raise_timeout(updates, path):
            raise TimeoutError("env.sh lock held")

        with (
            patch("control_server.routes.system.GIFT_MESSAGE_PATH", str(msg_path)),
            patch("control_server.routes.system.subprocess.run") as mock_run,
            patch("config.atomic_update", side_effect=raise_timeout),
        ):
            response = client.post("/api/system/prepare-for-gift", json={"token": token})

        assert response.status_code == 504, response.json
        assert response.json["error"]["code"] == "env_lock_timeout"
        mock_run.assert_not_called()
        assert not msg_path.exists(), "no message must be staged when the location clear fails"

    def test_write_error_aborts_and_reports_to_pwa(self, client, tmp_path):
        """#393: a disk/write error clearing location must also abort before
        dispatch with a 500 the PWA can show — never ship a stale device."""
        token = _issue_token(client, "prepare_for_gift")
        msg_path = tmp_path / "gift-message"

        def raise_oserror(updates, path):
            raise OSError("disk full")

        with (
            patch("control_server.routes.system.GIFT_MESSAGE_PATH", str(msg_path)),
            patch("control_server.routes.system.subprocess.run") as mock_run,
            patch("config.atomic_update", side_effect=raise_oserror),
        ):
            response = client.post("/api/system/prepare-for-gift", json={"token": token})

        assert response.status_code == 500, response.json
        assert response.json["error"]["code"] == "prepare_for_gift_failed"
        mock_run.assert_not_called()
        assert not msg_path.exists()

    def test_unit_not_loadable_aborts_before_clearing_location(self, client, tmp_path, gift_env_file):
        """#393/#327: if the teardown unit is missing/masked, the route must bail
        BEFORE the destructive location clear — never wipe the owner's location
        for a gift that can't start. Asserts: location SURVIVES in env.sh, no
        message staged, no systemctl start, 500, and the token is restored so a
        retry (once the unit is installed) works."""
        token = _issue_token(client, "prepare_for_gift")
        msg_path = tmp_path / "gift-message"
        with (
            patch("control_server.routes.system._gift_unit_loadable", return_value=False),
            patch("control_server.routes.system.GIFT_MESSAGE_PATH", str(msg_path)),
            patch("control_server.routes.system.subprocess.run") as mock_run,
        ):
            response = client.post("/api/system/prepare-for-gift", json={"token": token})

        assert response.status_code == 500, response.json
        assert response.json["error"]["code"] == "prepare_for_gift_failed"
        mock_run.assert_not_called()
        assert not msg_path.exists(), "no message must be staged when the unit can't dispatch"
        # Location must be untouched — the whole point of the pre-flight.
        from config import load_config

        cfg = load_config(str(gift_env_file))
        assert cfg.get("WEATHER_LATITUDE") == "30.27", "location must NOT be cleared when the unit can't start"
        assert cfg.get("WEATHER_LONGITUDE") == "-97.74"

    def test_missing_env_file_still_dispatches(self, client, tmp_path, app):
        """#393: a missing env.sh means there's no stored location to leak, so
        the clear is a no-op and the flow proceeds (mirrors the script's
        `[[ -f env.sh ]]` guard). Pointing ENV_FILE at a nonexistent path must
        NOT block gifting."""
        app.config["ENV_FILE"] = str(tmp_path / "does-not-exist-env.sh")
        token = _issue_token(client, "prepare_for_gift")
        msg_path = tmp_path / "gift-message"
        with (
            patch("control_server.routes.system.GIFT_MESSAGE_PATH", str(msg_path)),
            patch("control_server.routes.system.subprocess.run") as mock_run,
        ):
            response = client.post("/api/system/prepare-for-gift", json={"token": token})

        assert response.status_code == 200, response.json
        mock_run.assert_called_once()

    def test_token_restored_after_lock_timeout_allows_retry(self, client, tmp_path, gift_env_file):
        """#393 + #328 pattern: a pre-dispatch failure (lock timeout clearing
        location) must restore the confirm token so the operator's retry on the
        same open page works. First call 504s; second call (token still valid)
        succeeds once the lock is free."""
        token = _issue_token(client, "prepare_for_gift")
        msg_path = tmp_path / "gift-message"

        with (
            patch("control_server.routes.system.GIFT_MESSAGE_PATH", str(msg_path)),
            patch("control_server.routes.system.subprocess.run") as mock_run,
        ):
            with patch("config.atomic_update", side_effect=TimeoutError("busy")):
                first = client.post("/api/system/prepare-for-gift", json={"token": token})
            assert first.status_code == 504, first.json
            # Same token retried after the contending writer released the lock.
            second = client.post("/api/system/prepare-for-gift", json={"token": token})

        assert second.status_code == 200, second.json
        mock_run.assert_called_once()

    def test_happy_path_with_personal_message(self, client, tmp_path):
        token = _issue_token(client, "prepare_for_gift")
        msg_path = tmp_path / "gift-message"
        with (
            patch("control_server.routes.system.GIFT_MESSAGE_PATH", str(msg_path)),
            patch("control_server.routes.system.subprocess.run") as mock_run,
        ):
            response = client.post(
                "/api/system/prepare-for-gift",
                json={"token": token, "message": "For Mom — read me when you miss me."},
            )

        assert response.status_code == 200, response.json
        assert response.json["ok"] is True
        # Message was atomically staged for reset-setup.sh to consume.
        assert msg_path.read_text() == "For Mom — read me when you miss me."
        # Systemd unit was kicked off via the sudoers-allowed argv.
        args = mock_run.call_args.args[0]
        assert args == [
            "sudo",
            "/usr/bin/systemctl",
            "start",
            "--no-block",
            "litclock-prepare-for-gift.service",
        ]

    def test_happy_path_with_empty_message_writes_empty_file(self, client, tmp_path):
        """An empty message is valid — reset-setup.sh + shutdown-splash.sh
        fall back to the default 'Welcome to LitClock' greeting. The file
        is still written (empty) so the script's --message-file path is
        always meaningful."""
        token = _issue_token(client, "prepare_for_gift")
        msg_path = tmp_path / "gift-message"
        with (
            patch("control_server.routes.system.GIFT_MESSAGE_PATH", str(msg_path)),
            patch("control_server.routes.system.subprocess.run"),
        ):
            response = client.post("/api/system/prepare-for-gift", json={"token": token})
        assert response.status_code == 200, response.json
        assert msg_path.read_text() == ""

    def test_message_too_long_rejected(self, client, tmp_path):
        """80-char ceiling (#319 lowered from 280 once the renderer learned
        to wrap) enforced at the endpoint via the shared validator —
        defense-in-depth, the script also enforces head -c 80."""
        token = _issue_token(client, "prepare_for_gift")
        long_message = "x" * 81
        msg_path = tmp_path / "gift-message"
        with (
            patch("control_server.routes.system.GIFT_MESSAGE_PATH", str(msg_path)),
            patch("control_server.routes.system.subprocess.run") as mock_run,
        ):
            response = client.post(
                "/api/system/prepare-for-gift",
                json={"token": token, "message": long_message},
            )
        assert response.status_code == 400
        # #316 /review fix: API path now routes through the same validator
        # as the PRG path (config._validate_gift_mode_message), returning
        # `invalid_message` with the validator's specific reason.
        assert response.json["error"]["code"] == "invalid_message"
        assert "80" in response.json["error"]["message"]
        # Must NOT have invoked systemctl (and confirm token was already
        # consumed — the user has to mint a new one to retry, which is fine).
        mock_run.assert_not_called()
        # Must NOT have written the staging file (avoids leaving a stale
        # message that a hand-triggered reset-setup.sh would pick up).
        assert not msg_path.exists()

    @pytest.mark.parametrize(
        "bad_message,reason_substr",
        [
            ("hi $(whoami)", "may not contain"),
            ("`whoami`", "may not contain"),
            ("nul\x00byte", "NUL"),
            # #319 dropped the newline-rejection case — embedded `\n` is now
            # allowed end-to-end and rendered as a hard line break.
        ],
    )
    def test_message_content_validation_parity_with_settings(
        self, client, tmp_path, bad_message: str, reason_substr: str
    ) -> None:
        """#316 /review CRITICAL fix: the API endpoint must apply the same
        content validator as the PRG path. Previously only length was
        checked, so backticks / $ / NUL bytes could land in the gift-message
        file — $ / backticks violated the defense-in-depth ban documented
        in src/config.py. (#319 narrowed the contract: newlines are now
        allowed because the renderer word-wraps + honors them as hard breaks.)"""
        token = _issue_token(client, "prepare_for_gift")
        msg_path = tmp_path / "gift-message"
        with (
            patch("control_server.routes.system.GIFT_MESSAGE_PATH", str(msg_path)),
            patch("control_server.routes.system.subprocess.run") as mock_run,
        ):
            response = client.post(
                "/api/system/prepare-for-gift",
                json={"token": token, "message": bad_message},
            )
        assert response.status_code == 400, f"validator should reject {bad_message!r}; got {response.json}"
        assert response.json["error"]["code"] == "invalid_message"
        assert reason_substr in response.json["error"]["message"]
        mock_run.assert_not_called()
        assert not msg_path.exists(), "rejected message must not leave a staging file behind"

    def test_message_with_newline_accepted_and_written_verbatim(self, client, tmp_path):
        """#319 contract pin: /api/system/prepare-for-gift must ACCEPT
        embedded newlines and write the literal ``\\n`` byte to the staging
        file (no re-encoding). Without this test, a regression that
        re-banned newlines at the endpoint — or that quietly normalised
        ``\\n`` to a space before writing — would slip past CI because the
        old rejection-case parametrize was removed when #319 widened the
        validator (line 273 comment).

        Pins the end-to-end contract that shutdown-splash.sh depends on:
        the message file shutdown-splash reads must contain real newline
        bytes so ``_wrap_title`` can honor them as hard line breaks."""
        token = _issue_token(client, "prepare_for_gift")
        msg_path = tmp_path / "gift-message"
        with (
            patch("control_server.routes.system.GIFT_MESSAGE_PATH", str(msg_path)),
            patch("control_server.routes.system.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            response = client.post(
                "/api/system/prepare-for-gift",
                json={"token": token, "message": "Happy Birthday\nMom!"},
            )
        assert response.status_code == 200, response.json
        assert msg_path.read_bytes() == b"Happy Birthday\nMom!", (
            "literal \\n byte must reach the staging file so shutdown-splash.sh can render it"
        )
        mock_run.assert_called_once()

    def test_concurrent_calls_use_unique_tmp_paths(self, client, tmp_path):
        """#316 /review fix: two simultaneous calls must not race on a
        shared `.tmp` path. Each call uses tempfile.mkstemp for a unique
        inode; without that, thread B's open(..., 'w') would truncate
        thread A's pending bytes and one of them would surface an ENOENT
        500. Patch at the module level (not per-thread) since
        unittest.mock.patch is not thread-safe."""
        import threading

        msg_path = tmp_path / "gift-message"
        tokens = [_issue_token(client, "prepare_for_gift") for _ in range(2)]
        results: list[int] = []
        results_lock = threading.Lock()

        # Patch ONCE for both threads — unittest.mock.patch's context
        # manager isn't thread-safe; nested per-thread `with patch(...)`
        # blocks can race on enter/exit and leave the global unpatched
        # while a sibling thread's subprocess.run() call is still in
        # flight.
        with (
            patch("control_server.routes.system.GIFT_MESSAGE_PATH", str(msg_path)),
            patch("control_server.routes.system.subprocess.run"),
        ):

            def fire(token: str) -> None:
                response = client.post(
                    "/api/system/prepare-for-gift",
                    json={"token": token, "message": "concurrent"},
                )
                with results_lock:
                    results.append(response.status_code)

            threads = [threading.Thread(target=fire, args=(t,)) for t in tokens]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)
        # Both must succeed — the pre-fix race on the shared tmp path
        # would cause at least one to return 500 with ENOENT.
        assert all(r == 200 for r in results), f"concurrent calls produced mixed results: {results}"
        # No stale tmp files left behind.
        leftover = list(tmp_path.glob(".gift-message.*"))
        assert not leftover, f"tmp files leaked: {leftover}"

    def test_missing_token_rejected(self, client):
        with patch("control_server.routes.system.subprocess.run") as mock_run:
            response = client.post("/api/system/prepare-for-gift", json={"message": "hi"})
        assert response.status_code == 401
        assert response.json["error"]["code"] == "confirm_token_invalid"
        mock_run.assert_not_called()

    def test_token_for_other_action_rejected(self, client):
        """A reboot token cannot trigger prepare-for-gift — different blast
        radius. Confirm-modal copy lies otherwise."""
        reboot_token = _issue_token(client, "reboot")
        with patch("control_server.routes.system.subprocess.run") as mock_run:
            response = client.post(
                "/api/system/prepare-for-gift",
                json={"token": reboot_token, "message": "hi"},
            )
        assert response.status_code == 401
        mock_run.assert_not_called()

    def test_token_is_single_use(self, client, tmp_path):
        """#317 item 1 codex P2: duplicate POST on a consumed
        prepare_for_gift token now returns 409 ``confirm_token_consumed``
        (was 401 ``confirm_token_invalid``). The JS path gates its
        refresh-and-retry on ``confirm_token_expired`` ONLY, so this
        distinct slug is what prevents a double-click or bfcached
        resubmit from silently bypassing the single-use guard."""
        token = _issue_token(client, "prepare_for_gift")
        msg_path = tmp_path / "gift-message"
        with (
            patch("control_server.routes.system.GIFT_MESSAGE_PATH", str(msg_path)),
            patch("control_server.routes.system.subprocess.run"),
        ):
            first = client.post(
                "/api/system/prepare-for-gift",
                json={"token": token, "message": "first"},
            )
            second = client.post(
                "/api/system/prepare-for-gift",
                json={"token": token, "message": "replay"},
            )
        assert first.status_code == 200
        assert second.status_code == 409
        assert second.json["error"]["code"] == "confirm_token_consumed"

    def test_form_post_no_js_fallback(self, client, tmp_path):
        """Progressive enhancement: the PWA Settings form posts with
        application/x-www-form-urlencoded body when JS is off. The endpoint
        must accept both shapes for the captive-portal / hardened-browser
        case."""
        token = _issue_token(client, "prepare_for_gift")
        msg_path = tmp_path / "gift-message"
        with (
            patch("control_server.routes.system.GIFT_MESSAGE_PATH", str(msg_path)),
            patch("control_server.routes.system.subprocess.run"),
        ):
            response = client.post(
                "/api/system/prepare-for-gift",
                data={"token": token, "message": "form-fallback"},
            )
        assert response.status_code == 200
        assert msg_path.read_text() == "form-fallback"

    def test_systemctl_failure_returns_500_without_leaking_stderr(self, client, tmp_path):
        """sudoers / systemd / NM error stderr can name internal paths and
        unit names — never echo to the client. Same envelope as the reboot
        failure path."""
        token = _issue_token(client, "prepare_for_gift")
        msg_path = tmp_path / "gift-message"
        sentinel = b"systemd: Refused to start: AccessDenied on /etc/litclock/.welcome-mode"
        with (
            patch("control_server.routes.system.GIFT_MESSAGE_PATH", str(msg_path)),
            patch(
                "control_server.routes.system.subprocess.run",
                side_effect=subprocess.CalledProcessError(
                    returncode=1,
                    cmd=["sudo", "systemctl", "start", "litclock-prepare-for-gift.service"],
                    stderr=sentinel,
                ),
            ),
        ):
            response = client.post(
                "/api/system/prepare-for-gift",
                json={"token": token, "message": "fine"},
            )
        assert response.status_code == 500
        assert response.json["error"]["code"] == "prepare_for_gift_failed"
        assert "AccessDenied" not in response.data.decode()
        assert ".welcome-mode" not in response.data.decode()

    def test_systemctl_unit_not_found_unlinks_staged_message(self, client, tmp_path):
        """#342 I9 — when systemctl returncode is 4 (unit not found, the
        #327 install-gap motivation), the unit provably did not dispatch.
        Unlink the staged message so a later retry with an empty body
        doesn't pick up a stale draft from a prior failed attempt."""
        token = _issue_token(client, "prepare_for_gift")
        msg_path = tmp_path / "gift-message"
        with (
            patch("control_server.routes.system.GIFT_MESSAGE_PATH", str(msg_path)),
            patch(
                "control_server.routes.system.subprocess.run",
                side_effect=subprocess.CalledProcessError(
                    returncode=4,  # systemctl: unit not found
                    cmd=["sudo", "systemctl", "start", "litclock-prepare-for-gift.service"],
                    stderr=b"Unit litclock-prepare-for-gift.service not found.",
                ),
            ),
        ):
            response = client.post(
                "/api/system/prepare-for-gift",
                json={"token": token, "message": "stale draft"},
            )
        assert response.status_code == 500
        # Returncode 4 proves pre-dispatch — unlink is safe.
        assert not msg_path.exists(), "staged gift-message should be unlinked when returncode proves pre-dispatch"

    def test_systemctl_unit_masked_unlinks_staged_message(self, client, tmp_path):
        """#342 I9 — returncode 5 (unit not loaded / masked) is the other
        provably-pre-dispatch case."""
        token = _issue_token(client, "prepare_for_gift")
        msg_path = tmp_path / "gift-message"
        with (
            patch("control_server.routes.system.GIFT_MESSAGE_PATH", str(msg_path)),
            patch(
                "control_server.routes.system.subprocess.run",
                side_effect=subprocess.CalledProcessError(
                    returncode=5,
                    cmd=["sudo", "systemctl", "start", "litclock-prepare-for-gift.service"],
                    stderr=b"Unit litclock-prepare-for-gift.service is masked.",
                ),
            ),
        ):
            response = client.post(
                "/api/system/prepare-for-gift",
                json={"token": token, "message": "stale draft"},
            )
        assert response.status_code == 500
        assert not msg_path.exists()

    def test_systemctl_ambiguous_failure_preserves_staged_message(self, client, tmp_path):
        """#342 I9 / codex adversarial /review F3: returncode 1 (generic
        failure) does NOT prove pre-dispatch — a narrow dbus-glitch window
        could leave the unit dispatched while the wrapper exits non-zero.
        The unit's ExecStart will then read GIFT_MESSAGE_PATH and render
        the welcome message. Unlinking in this branch would replace the
        user's personalized message with the default splash — data loss.
        Preserve the staged message; let the unit consume it if it fires."""
        token = _issue_token(client, "prepare_for_gift")
        msg_path = tmp_path / "gift-message"
        with (
            patch("control_server.routes.system.GIFT_MESSAGE_PATH", str(msg_path)),
            patch(
                "control_server.routes.system.subprocess.run",
                side_effect=subprocess.CalledProcessError(
                    returncode=1,  # ambiguous — could be pre- or post-dispatch
                    cmd=["sudo", "systemctl", "start", "litclock-prepare-for-gift.service"],
                    stderr=b"Failed to start: some ambiguous error",
                ),
            ),
        ):
            response = client.post(
                "/api/system/prepare-for-gift",
                json={"token": token, "message": "user-typed-welcome"},
            )
        assert response.status_code == 500
        # The staged message MUST survive — the unit may still pick it up.
        assert msg_path.exists(), "ambiguous returncode must preserve the staged message"
        assert msg_path.read_text() == "user-typed-welcome"

    def test_returns_409_when_update_is_busy(self, client, tmp_path):
        """#316 /review fix: pre-check that an update isn't running before
        triggering the gift-prep unit. The unit's Conflicts=
        litclock-update.service is bidirectional — if we kick off
        prepare-for-gift while the weekly update is in flight, systemd
        SIGTERMs the script and leaves the device in partial state. Mirror
        the wifi.reset 409 update_in_progress gate so the user gets a
        friendly retry message instead of an opaque post-hoc failure."""
        token = _issue_token(client, "prepare_for_gift")
        msg_path = tmp_path / "gift-message"
        # Override the class-level autouse fixture so this test can simulate
        # the update-busy state.
        with (
            patch("control_server.routes.system.update_state.update_is_busy", return_value=True),
            patch("control_server.routes.system.GIFT_MESSAGE_PATH", str(msg_path)),
            patch("control_server.routes.system.subprocess.run") as mock_run,
        ):
            response = client.post(
                "/api/system/prepare-for-gift",
                json={"token": token, "message": "hi"},
            )
        assert response.status_code == 409
        assert response.json["error"]["code"] == "update_in_progress"
        # Must NOT have written the message file or invoked systemctl.
        mock_run.assert_not_called()
        assert not msg_path.exists()

    # ─── #328 — restore-on-failure regressions ──────────────────────────────

    def test_update_busy_restores_token_for_retry(self, client, tmp_path):
        """#328: a 409 update_in_progress on prepare-for-gift is a pre-side-
        effect failure. The user opened the confirm modal, tapped the
        destructive button, and got told to retry — but pre-fix the token
        was already consumed, so the retry hit 401 ("expired") instead of
        the gate. Post-fix: token restored, retry after the update
        finishes can use the same open page."""
        token = _issue_token(client, "prepare_for_gift")
        msg_path = tmp_path / "gift-message"

        # First call: update is busy.
        with (
            patch("control_server.routes.system.update_state.update_is_busy", return_value=True),
            patch("control_server.routes.system.GIFT_MESSAGE_PATH", str(msg_path)),
            patch("control_server.routes.system.subprocess.run"),
        ):
            first = client.post("/api/system/prepare-for-gift", json={"token": token, "message": "hi"})
        assert first.status_code == 409

        # Second call with same token after the update finishes — must
        # reach subprocess.run (token was restored).
        with (
            patch("control_server.routes.system.update_state.update_is_busy", return_value=False),
            patch("control_server.routes.system.GIFT_MESSAGE_PATH", str(msg_path)),
            patch("control_server.routes.system.subprocess.run") as mock_run,
        ):
            second = client.post("/api/system/prepare-for-gift", json={"token": token, "message": "hi"})
        assert second.status_code == 200, second.json
        mock_run.assert_called_once()

    def test_validation_failure_restores_token_for_retry(self, client, tmp_path):
        """#328: a 400 invalid_message rejection is pre-side-effect — no
        file staged, no systemctl dispatched. Restoring the token lets
        the user fix the message and retry with the same open page
        instead of being told the token is expired."""
        token = _issue_token(client, "prepare_for_gift")
        msg_path = tmp_path / "gift-message"

        # First call: invalid message (backtick).
        with (
            patch("control_server.routes.system.GIFT_MESSAGE_PATH", str(msg_path)),
            patch("control_server.routes.system.subprocess.run") as mock_run,
        ):
            first = client.post(
                "/api/system/prepare-for-gift",
                json={"token": token, "message": "`whoami`"},
            )
        assert first.status_code == 400
        assert first.json["error"]["code"] == "invalid_message"
        mock_run.assert_not_called()

        # Second call with the SAME token + a clean message must succeed.
        with (
            patch("control_server.routes.system.GIFT_MESSAGE_PATH", str(msg_path)),
            patch("control_server.routes.system.subprocess.run") as mock_run,
        ):
            second = client.post(
                "/api/system/prepare-for-gift",
                json={"token": token, "message": "Hello!"},
            )
        assert second.status_code == 200, second.json
        mock_run.assert_called_once()

    def test_called_process_error_restores_token_for_retry(self, client, tmp_path):
        """#328: when systemctl returns non-zero on the gift-prep unit,
        restoring the token IS safe even though the message file is
        already staged — the staging path is tmpfs, re-stage on retry
        overwrites atomically via os.replace (idempotent), and the unit
        only reads the file when it actually fires. Pin this so the
        documented exception to "restore is safe" doesn't drift back
        into "we should keep the token consumed because state was
        partially written."""
        token = _issue_token(client, "prepare_for_gift")
        msg_path = tmp_path / "gift-message"

        with (
            patch("control_server.routes.system.GIFT_MESSAGE_PATH", str(msg_path)),
            patch(
                "control_server.routes.system.subprocess.run",
                side_effect=subprocess.CalledProcessError(
                    returncode=1, cmd=["sudo", "systemctl", "start"], stderr=b"x"
                ),
            ),
        ):
            first = client.post(
                "/api/system/prepare-for-gift",
                json={"token": token, "message": "hi"},
            )
        assert first.status_code == 500
        assert first.json["error"]["code"] == "prepare_for_gift_failed"

        # Retry with same token — token restored, subprocess called again
        # (and now succeeds).
        with (
            patch("control_server.routes.system.GIFT_MESSAGE_PATH", str(msg_path)),
            patch("control_server.routes.system.subprocess.run") as mock_run,
        ):
            second = client.post(
                "/api/system/prepare-for-gift",
                json={"token": token, "message": "hi"},
            )
        assert second.status_code == 200, second.json
        mock_run.assert_called_once()


# ─── #396 gift-flow timezone reset ──────────────────────────────────────────


_TZ_RESET_ARGV = ["sudo", "/usr/bin/timedatectl", "set-timezone", "UTC"]


def _is_gift_dispatch(argv: list[str]) -> bool:
    return argv[:2] == ["sudo", "/usr/bin/systemctl"] and "litclock-prepare-for-gift.service" in argv


class TestPrepareForGiftTimezoneReset:
    """#396: prepare_for_gift resets the system tz to UTC synchronously — AFTER
    the load-bearing coords clear, BEFORE the teardown dispatch — so the gifter's
    tz doesn't linger in /etc/localtime. Best-effort by design: a tz-reset
    failure must NOT abort the gift. A stale tz can't drive a wrong-time clock
    for the recipient (the cleared coords gate quotes until their first-boot
    resolves a tz), so blocking a gift over a timedatectl quirk would be
    backwards on the severity ledger.

    These tests deliberately do NOT no-op _gift_reset_timezone_to_utc (unlike
    TestPrepareForGiftRoute), so the same module-level subprocess.run mock sees
    BOTH the tz reset and the systemctl dispatch."""

    @pytest.fixture(autouse=True)
    def _bypass_update_busy_gate(self):
        with patch("control_server.routes.system.update_state.update_is_busy", return_value=False):
            yield

    @pytest.fixture(autouse=True)
    def gift_env_file(self, app, tmp_path):
        env_file = tmp_path / "env.sh"
        env_file.write_text(
            "export WEATHER_LATITUDE=30.27\n"
            "export WEATHER_LONGITUDE=-97.74\n"
            'export WEATHER_LOCATION_NAME="Austin, TX"\n'
        )
        app.config["ENV_FILE"] = str(env_file)
        return env_file

    def test_resets_tz_to_utc_before_dispatch(self, client, tmp_path):
        """The tz reset fires with the exact sudoers-matched argv, and ordering
        is tz-reset → systemctl dispatch (tz gone before any teardown)."""
        token = _issue_token(client, "prepare_for_gift")
        msg_path = tmp_path / "gift-message"
        with (
            patch("control_server.routes.system.GIFT_MESSAGE_PATH", str(msg_path)),
            patch("control_server.routes.system.subprocess.run") as mock_run,
        ):
            response = client.post("/api/system/prepare-for-gift", json={"token": token})

        assert response.status_code == 200, response.json
        argv_list = [call.args[0] for call in mock_run.call_args_list]
        assert _TZ_RESET_ARGV in argv_list, argv_list
        tz_idx = argv_list.index(_TZ_RESET_ARGV)
        dispatch_idx = next(i for i, a in enumerate(argv_list) if _is_gift_dispatch(a))
        assert tz_idx < dispatch_idx, f"tz reset must precede dispatch: {argv_list}"

    def test_tz_reset_failure_does_not_abort_gift(self, client, tmp_path):
        """A timedatectl failure is swallowed: the gift still dispatches + 200.
        This is the whole point of #396's best-effort choice."""
        token = _issue_token(client, "prepare_for_gift")
        msg_path = tmp_path / "gift-message"

        def run_side_effect(argv, *a, **k):
            if argv[:2] == ["sudo", "/usr/bin/timedatectl"]:
                raise subprocess.CalledProcessError(returncode=1, cmd=argv, stderr=b"nope")
            return MagicMock(returncode=0, stderr=b"")

        with (
            patch("control_server.routes.system.GIFT_MESSAGE_PATH", str(msg_path)),
            patch("control_server.routes.system.subprocess.run", side_effect=run_side_effect) as mock_run,
        ):
            response = client.post("/api/system/prepare-for-gift", json={"token": token})

        assert response.status_code == 200, response.json
        assert any(_is_gift_dispatch(call.args[0]) for call in mock_run.call_args_list), (
            "the gift must still dispatch when the tz reset fails"
        )

    def test_helper_swallows_subprocess_error(self, app):
        """_gift_reset_timezone_to_utc logs, never raises — a propagated error
        would bubble up and abort the gift, defeating the best-effort contract."""
        from control_server.routes.system import _gift_reset_timezone_to_utc

        with (
            app.app_context(),
            patch(
                "control_server.routes.system.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="timedatectl", timeout=5),
            ),
        ):
            _gift_reset_timezone_to_utc()  # must not raise

    def test_helper_argv_matches_sudoers(self):
        """The argv exposed for the parity check is exactly what sudo matches
        (sudo strips argv[0] → `timedatectl set-timezone UTC`)."""
        from control_server.routes.system import _gift_reset_argv

        assert _gift_reset_argv() == _TZ_RESET_ARGV

    # ── Negative paths: the tz reset must NOT fire on any abort, so a gift that
    # never happens can't silently put the OWNER's own clock on UTC. These pin
    # the "clear → stage → tz-reset → dispatch" ordering — the tz reset is the
    # last mutation before the point of no return.

    def test_tz_reset_not_fired_when_location_clear_aborts(self, client, app, tmp_path):
        """Clear fails (flock timeout → 504 abort) → gift never happens, owner
        keeps the device, so their tz must be PRESERVED (UTC reset must NOT
        fire)."""
        token = _issue_token(client, "prepare_for_gift")
        msg_path = tmp_path / "gift-message"
        with (
            patch("control_server.routes.system.GIFT_MESSAGE_PATH", str(msg_path)),
            patch("control_server.routes.system.subprocess.run") as mock_run,
            patch("config.atomic_update", side_effect=TimeoutError("busy")),
        ):
            r = client.post("/api/system/prepare-for-gift", json={"token": token})
        assert r.status_code == 504, r.json
        assert not any(c.args[0] == _TZ_RESET_ARGV for c in mock_run.call_args_list), (
            "tz reset must NOT fire when the gift aborts on a clear failure"
        )

    def test_tz_reset_not_fired_when_unit_not_loadable(self, client, tmp_path):
        """Unit not dispatchable → bail before any mutation → tz preserved."""
        token = _issue_token(client, "prepare_for_gift")
        msg_path = tmp_path / "gift-message"
        with (
            patch("control_server.routes.system._gift_unit_loadable", return_value=False),
            patch("control_server.routes.system.GIFT_MESSAGE_PATH", str(msg_path)),
            patch("control_server.routes.system.subprocess.run") as mock_run,
        ):
            r = client.post("/api/system/prepare-for-gift", json={"token": token})
        assert r.status_code == 500, r.json
        assert not any(c.args[0] == _TZ_RESET_ARGV for c in mock_run.call_args_list)

    def test_tz_reset_not_fired_when_message_staging_fails(self, client, tmp_path):
        """#396 Codex /review: the tz reset is placed AFTER message staging so a
        staging failure (gift aborts, owner keeps the device) can't leave the
        owner's clock on UTC. Pin it: staging OSError → 500 → tz NOT reset."""
        token = _issue_token(client, "prepare_for_gift")
        msg_path = tmp_path / "gift-message"
        with (
            patch("control_server.routes.system.GIFT_MESSAGE_PATH", str(msg_path)),
            patch("control_server.routes.system.subprocess.run") as mock_run,
            patch("control_server.routes.system.os.replace", side_effect=OSError("disk full")),
        ):
            r = client.post("/api/system/prepare-for-gift", json={"token": token})
        assert r.status_code == 500, r.json
        assert r.json["error"]["code"] == "prepare_for_gift_failed"
        assert not any(c.args[0] == _TZ_RESET_ARGV for c in mock_run.call_args_list), (
            "tz reset must NOT fire when message staging fails before dispatch"
        )


# ─── Subprocess failure modes ───────────────────────────────────────────────


class TestSubprocessFailure:
    @pytest.fixture(autouse=True)
    def _bypass_update_busy_gate(self, _bypass_update_busy_for_execute_action):
        """#362 D8 — bypass the update-busy gate so we reach the destructive
        subprocess.run call where these tests are exercising failure modes."""
        yield

    def test_called_process_error_returns_500_without_leaking_stderr(self, client):
        token = _issue_token(client, "reboot")
        sentinel = b"systemd internal: /run/systemd/private bind failed"
        with patch(
            "control_server.routes.system.subprocess.run",
            side_effect=subprocess.CalledProcessError(
                returncode=1, cmd=["sudo", "systemctl", "reboot"], stderr=sentinel
            ),
        ):
            response = client.post("/api/system/reboot", json={"token": token})

        assert response.status_code == 500
        body = response.json
        assert body["ok"] is False
        assert body["error"]["code"] == "systemctl_failed"
        # stderr can name internal paths / unit names — never echo to client.
        assert b"systemd internal" not in response.data
        assert "private" not in body["error"]["message"]

    def test_timeout_returns_500(self, client):
        token = _issue_token(client, "poweroff")
        with patch(
            "control_server.routes.system.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["sudo", "systemctl", "poweroff"], timeout=5),
        ):
            response = client.post("/api/system/poweroff", json={"token": token})

        assert response.status_code == 500
        assert response.json["error"]["code"] == "systemctl_failed"

    # ─── #328 — restore-on-failure regressions ──────────────────────────────

    def test_called_process_error_restores_token_for_retry(self, client):
        """#328: when systemctl returns non-zero BEFORE dispatching the
        unit (e.g. #327-style missing unit), the box is still up and the
        user's retry should surface the real error message — not a
        spurious "token already used" 401 that masks it.

        Repro the live M8-hardware-QA scenario: first call fails (mocked
        CalledProcessError), then the second call with the SAME token
        must reach subprocess.run again. We swap the mock between calls
        to assert the second one ACTUALLY tries (and now succeeds).
        """
        token = _issue_token(client, "reboot")
        # First call: subprocess fails.
        with patch(
            "control_server.routes.system.subprocess.run",
            side_effect=subprocess.CalledProcessError(returncode=1, cmd=["sudo", "systemctl", "reboot"], stderr=b"x"),
        ):
            first = client.post("/api/system/reboot", json={"token": token})
        assert first.status_code == 500, first.json
        assert first.json["error"]["code"] == "systemctl_failed"

        # Second call with the SAME token — pre-fix this 401'd ("token
        # already used"). Post-fix the token was restored, so subprocess.run
        # is invoked again. Mock it to succeed this time.
        #
        # #362 — successful _execute_action now fires TWO subprocess.run
        # calls (pre-stop + destructive). The destructive call is the last
        # one; assert by call_args_list rather than the old
        # ``assert_called_once``.
        with patch("control_server.routes.system.subprocess.run") as mock_run:
            second = client.post("/api/system/reboot", json={"token": token})
        assert second.status_code == 200, second.json
        assert second.json == {"ok": True, "action": "reboot"}
        assert mock_run.call_count == 2, mock_run.call_args_list
        assert mock_run.call_args_list[-1].args[0] == ["sudo", "/usr/bin/systemctl", "reboot", "--no-block"]

    def test_timeout_does_not_restore_token(self, client):
        """#328: TimeoutExpired is the paranoid path — systemctl --no-block
        returns immediately on success, so a timeout means the unit may
        have actually dispatched. Keep the token consumed so we don't
        double-fire poweroff. Pin this behavior so a future "always
        restore" refactor doesn't enable double-fire on the rare timeout.
        """
        token = _issue_token(client, "poweroff")
        with patch(
            "control_server.routes.system.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["sudo", "systemctl", "poweroff"], timeout=5),
        ):
            first = client.post("/api/system/poweroff", json={"token": token})
        assert first.status_code == 500

        # Token must NOT be restored on timeout — second call sees the
        # tombstone hit, classified as 409 ``confirm_token_consumed``
        # (#317 item 1 codex P2 — was 401 ``confirm_token_invalid``).
        # The JS path will surface the "already submitted" copy and
        # refuse to refresh-and-retry, which is exactly right: a real
        # poweroff may have dispatched.
        with patch("control_server.routes.system.subprocess.run") as mock_run:
            second = client.post("/api/system/poweroff", json={"token": token})
        assert second.status_code == 409, second.json
        assert second.json["error"]["code"] == "confirm_token_consumed"
        mock_run.assert_not_called()

    def test_called_process_error_handlers_document_double_fire_caveat(self):
        """Review D1: BOTH CalledProcessError restore branches in system.py
        (the shared _execute_action dispatcher AND the prepare_for_gift
        handler) must carry the residual-dispatch-window warning so a
        future engineer adding a non-idempotent destructive action sees
        the escape hatch.

        Pin 'double-fire' AND 'non-idempotent' as load-bearing phrases
        appearing in the system route source. A refactor that removes
        the caveat blocks trips this test."""
        system_src = (
            Path(__file__).resolve().parents[1] / "src" / "control_server" / "routes" / "system.py"
        ).read_text()
        # Both phrases must appear at least twice — once for _execute_action
        # (reboot / poweroff) and once for prepare_for_gift. The exact count
        # is a soft pin; what matters is that the caveat lives next to both
        # restore call sites.
        assert system_src.count("double-fire") >= 2, (
            "routes/system.py must document the double-fire caveat near BOTH "
            "CalledProcessError restore call sites (review D1) — _execute_action "
            "and prepare_for_gift"
        )
        assert system_src.count("non-idempotent") >= 2, (
            "routes/system.py must spell out the non-idempotent action escape "
            "hatch near BOTH CalledProcessError restore call sites (review D1)"
        )


# ─── Rate limit at the route layer ──────────────────────────────────────────


class TestRouteRateLimit:
    @pytest.fixture(autouse=True)
    def _bypass_update_busy_gate(self, _bypass_update_busy_for_execute_action):
        """#362 D8 — bypass so the 200 spends actually land on the rate-limit
        bucket instead of being shunted aside as 409 update_in_progress."""
        yield

    def test_sixth_action_within_window_returns_429(self, client):
        # /confirm-token + /reboot each consume one bucket token (they share
        # the same bucket per AC). Issue 2 tokens up front, fire the action
        # twice (4 spends), then attempt a 6th call: that 6th call exhausts
        # and 429s on call #6 (confirm-token + reboot + confirm-token +
        # reboot + confirm-token = 5 spends, the 6th is the 429 trigger).
        token_a = _issue_token(client, "reboot")  # spend 1
        token_b = _issue_token(client, "reboot")  # spend 2
        with patch("control_server.routes.system.subprocess.run"):
            assert (
                client.post("/api/system/reboot", json={"token": token_a}).status_code == 200  # spend 3
            )
            assert (
                client.post("/api/system/reboot", json={"token": token_b}).status_code == 200  # spend 4
            )
        _issue_token(client, "reboot")  # spend 5

        # The 6th call to ANYTHING under /api/system/* should 429.
        response = client.post("/api/system/confirm-token", json={"action": "reboot"})
        assert response.status_code == 429
        body = response.json
        assert body["error"]["code"] == "rate_limited"
        assert body["error"]["retry_after_s"] >= 1
        # Standard HTTP signal for proxies + browser dev tools.
        assert response.headers.get("Retry-After") == str(body["error"]["retry_after_s"])

    def test_rate_limit_is_per_remote_addr(self, client):
        # Burn IP A's bucket all the way to 429.
        for _ in range(5):
            client.post(
                "/api/system/confirm-token",
                json={"action": "reboot"},
                environ_overrides={"REMOTE_ADDR": "10.0.0.1"},
            )
        # IP A is exhausted.
        a_429 = client.post(
            "/api/system/confirm-token",
            json={"action": "reboot"},
            environ_overrides={"REMOTE_ADDR": "10.0.0.1"},
        )
        assert a_429.status_code == 429

        # IP B starts fresh — defends LAN siblings from a single bad actor.
        b_ok = client.post(
            "/api/system/confirm-token",
            json={"action": "reboot"},
            environ_overrides={"REMOTE_ADDR": "10.0.0.2"},
        )
        assert b_ok.status_code == 200

    def test_confirm_token_endpoint_is_rate_limited(self, client):
        """Regression guard: /confirm-token must share the bucket with the
        action endpoints — otherwise an attacker could spam the cheap endpoint
        with no cap, then fire reboot once via a stockpiled token.
        """
        for _ in range(5):
            response = client.post("/api/system/confirm-token", json={"action": "reboot"})
            assert response.status_code == 200
        sixth = client.post("/api/system/confirm-token", json={"action": "reboot"})
        assert sixth.status_code == 429


# ─── /system tab page ───────────────────────────────────────────────────────


class TestSystemTabPage:
    def test_get_system_returns_200(self, client):
        response = client.get("/system")
        assert response.status_code == 200

    def test_index_no_longer_owns_system_route(self, app):
        """The /system route must be served by the system blueprint, not the
        index stub — otherwise both M4 and M2 try to register /system and
        Flask raises AssertionError at app creation. (Implicit: create_app
        didn't raise.) This test pins the contract for future grep'ers."""
        rules = [r for r in app.url_map.iter_rules() if r.rule == "/system"]
        assert len(rules) == 1
        assert rules[0].endpoint == "system.system_tab"

    def test_renders_action_cards(self, client):
        """Story 2.1 + M5 + #317 item 7: Restart + Power off + Reset WiFi +
        Prepare for Gifting cards all present on /system."""
        body = client.get("/system").data
        assert b'data-action="reboot"' in body
        assert b'data-action="poweroff"' in body
        assert b'data-action="wifi_reset"' in body
        assert b'data-action="prepare_for_gift"' in body
        assert b">Restart</h2>" in body
        assert b">Power off</h2>" in body
        assert b">Reset WiFi</h2>" in body
        assert b">Prepare for Gifting</h2>" in body

    def test_each_card_has_form_with_hidden_token(self, client):
        """No-JS fallback: each card is a form with a server-issued token in
        a hidden input. Story 2.2's JS will intercept submit; without JS the
        form POSTs straight through and the destructive action still gates
        on the confirm-token TTL. M5 added Reset-WiFi as the 3rd card;
        #317 item 7 added Prepare-for-Gifting as the 4th.
        """
        body = client.get("/system").data.decode()
        # Reboot + poweroff hit /api/system/{action}; Reset-WiFi hits
        # /api/wifi/reset on the wifi blueprint; Prepare-for-Gifting hits
        # /api/system/prepare-for-gift (#317 item 7 moved this card here).
        for action in ("reboot", "poweroff"):
            assert f'action="/api/system/{action}"' in body
            assert f'data-confirm-action="{action}"' in body
        assert 'action="/api/wifi/reset"' in body
        assert 'data-confirm-action="wifi_reset"' in body
        assert 'action="/api/system/prepare-for-gift"' in body
        assert 'data-confirm-action="prepare_for_gift"' in body
        # #510 — Factory reset hits /api/system/reset.
        assert 'action="/api/system/reset"' in body
        assert 'data-confirm-action="factory_reset"' in body
        # Tokens must be non-trivial strings (secrets.token_urlsafe(32) → ~43 chars).
        import re

        token_inputs = re.findall(r'name="token" value="([^"]+)"', body)
        assert len(token_inputs) == 5
        for token in token_inputs:
            assert len(token) >= 32
        # All five tokens must differ — otherwise consuming one would invalidate
        # the others.
        assert len(set(token_inputs)) == 5

    def test_form_token_consumes_against_correct_action(self, client):
        """End-to-end: render the page, scrape the reboot token from the
        hidden input, POST it to /api/system/reboot. This is the no-JS
        smoke test — guards against a regression where the page renders
        a token bound to the wrong action.
        """
        import re

        page = client.get("/system").data.decode()
        # Scrape the reboot card's token: form action="/api/system/reboot"
        # block + the hidden token within it.
        match = re.search(
            r'action="/api/system/reboot".*?value="([^"]+)"',
            page,
            re.DOTALL,
        )
        assert match, "couldn't find reboot card's confirm token in rendered HTML"
        reboot_token = match.group(1)

        with (
            patch("control_server.routes.system.update_state.update_is_busy", return_value=False),
            patch("control_server.routes.system.subprocess.run") as mock_run,
        ):
            response = client.post(
                "/api/system/reboot",
                data={"token": reboot_token},
                content_type="application/x-www-form-urlencoded",
            )
        assert response.status_code == 200, response.json
        assert response.json == {"ok": True, "action": "reboot"}
        # #362 — _execute_action now fires two subprocess.run calls per
        # successful reboot (pre-stop + destructive).
        assert mock_run.call_count == 2, mock_run.call_args_list
        assert mock_run.call_args_list[-1].args[0] == ["sudo", "/usr/bin/systemctl", "reboot", "--no-block"]

    def test_renders_action_icons(self, client):
        """All four cards (Restart, Power off, Reset WiFi, Prepare for
        Gifting per #317 item 7) have an inline SVG icon — test the
        structural marker (`action-card__icon` class) not the path data,
        so a future Phosphor swap (#255) doesn't break this test.
        """
        body = client.get("/system").data
        assert body.count(b'class="action-card__icon"') == 5

    def test_loads_system_css_only_on_system_tab(self, client):
        """system.css ships only when active_tab=='system' — every byte
        counts on Pi Zero 2W per design rationale in tokens.css comments.
        """
        system_body = client.get("/system").data
        status_body = client.get("/").data
        assert b"css/system.css" in system_body
        assert b"css/system.css" not in status_body

    def test_loads_system_js_only_on_system_tab(self, client):
        """system.js follows the same per-tab scoping rule (Story 2.2).

        #317 item 7 moved Prepare-for-Gifting from /settings to /system, so
        /settings no longer needs system.js — that exception is gone."""
        system_body = client.get("/system").data
        status_body = client.get("/").data
        settings_body = client.get("/settings").data
        assert b"js/system.js" in system_body
        assert b"js/system.js" not in status_body
        assert b"js/system.js" not in settings_body, (
            "#317 item 7: settings.html.j2 must NOT load system.js anymore — "
            "the Prepare-for-Gifting card lives on /system now, and pulling "
            "system.js onto /settings would lazy-cache an unused script on "
            "every Settings visit."
        )

    def test_system_js_includes_all_hidden_inputs_in_post_body(self, client):
        """#316 /review CRITICAL fix: fireAction must include every hidden
        field (not just `token`) in the JSON body. The Prepare-for-Gifting
        form carries a hidden `message` field; without this loop the JS
        path silently sent an empty message and the e-ink fell back to
        the default greeting regardless of what the user typed and saved.

        #319 hardware-QA regression catch: the message field changed from
        `<input type="hidden">` to `<textarea hidden>` (to preserve
        newlines on the no-JS path). The selector MUST match both element
        types — caught by firing the actual destructive flow on the test
        Pi and seeing the e-ink paint a default 'Welcome to LitClock'
        instead of the saved multi-line draft."""
        system_js = client.get("/static/js/system.js").data.decode()
        # The fix selector must catch BOTH hidden inputs (sibling tokens)
        # AND hidden textareas (the message field after #319's no-JS-newline fix).
        assert 'input[type="hidden"], textarea[hidden]' in system_js, (
            "fireAction must collect both hidden inputs AND hidden textareas; "
            "the #319 message field is a <textarea hidden> so newlines survive "
            "the no-JS form serializer"
        )
        assert "payload[field.name] = field.value" in system_js or "payload[field.name]=field.value" in system_js, (
            "fireAction must merge hidden field values into the JSON payload"
        )
        # Single-token shape (the pre-fix bug) MUST NOT be the body anymore.
        assert "JSON.stringify({ token: tokenInput.value })" not in system_js, (
            "fireAction must not ship the pre-fix single-token body shape — message field dropped"
        )


# ─── Sheet-style confirm modal (Story 2.2) ──────────────────────────────────


class TestConfirmSheetModal:
    """Markup-level tests for the sheet-style modal scaffolding.

    Behavior tests (showModal() flow, backdrop dismiss, focus trap) live in
    Phase 5 QA — they need a real browser engine, not Flask's test client.
    """

    def test_dialogs_render_one_per_action(self, client):
        body = client.get("/system").data
        # #317 item 7 added Prepare-for-Gifting as the 4th action-card +
        # dialog. Each card AND each dialog carries data-action so the
        # count includes both occurrences.
        assert b'data-action="reboot"' in body
        assert b'data-action="poweroff"' in body
        assert b'data-action="wifi_reset"' in body
        assert b'data-action="prepare_for_gift"' in body
        assert b'data-action="factory_reset"' in body  # #510
        # All five should be <dialog> elements with role=alertdialog per
        # DESIGN.md a11y line 290.
        assert body.count(b'role="alertdialog"') == 5
        assert body.count(b'class="confirm-sheet"') == 5

    def test_dialogs_have_aria_labelledby_and_describedby(self, client):
        """ARIA pairing per DESIGN.md a11y spec — labelledby points at the
        title element, describedby at the consequence body."""
        body = client.get("/system").data.decode()
        assert 'aria-labelledby="confirm-reboot-title"' in body
        assert 'id="confirm-reboot-title"' in body
        assert 'aria-describedby="confirm-reboot-body"' in body
        assert 'id="confirm-reboot-body"' in body
        assert 'aria-labelledby="confirm-poweroff-title"' in body
        assert 'aria-describedby="confirm-poweroff-body"' in body

    def test_restart_modal_uses_locked_copy_verbatim(self, client):
        """DESIGN.md §"Confirm modals — copy library" — Restart row.
        Locked. Any drift from these strings means a design-system review
        was bypassed. Test guards against silent edits."""
        body = client.get("/system").data
        assert b"Restart LitClock?" in body
        assert b"The display will go blank for about 30 seconds, then your quote will return." in body

    def test_poweroff_modal_uses_locked_copy_verbatim(self, client):
        """DESIGN.md §"Confirm modals — copy library" — Power off row.
        Bold "unplug and re-plug" per the markdown emphasis in the library."""
        body = client.get("/system").data
        assert b"Power off LitClock?" in body
        assert b"<strong>unplug and re-plug</strong>" in body
        assert b"There&#39;s no remote on switch." in body or b"There's no remote on switch." in body

    def test_buttons_present_per_dialog(self, client):
        body = client.get("/system").data
        # #510: 5 dialogs (reboot, poweroff, wifi_reset, factory_reset,
        # prepare_for_gift) × (1 cancel + 1 confirm) = 10 control buttons.
        assert body.count(b"data-modal-cancel") == 5
        assert body.count(b"data-modal-confirm") == 5

    def test_prepare_for_gift_modal_uses_locked_copy_verbatim(self, client):
        """DESIGN.md §"Confirm modals — copy library" — Prepare for Gifting
        row. Pinned post-#317 item 7 move so a future refactor can't drift
        the destructive copy."""
        body = client.get("/system").data
        assert b"Prepare for Gifting?" in body
        assert b"Your WiFi will be wiped and the clock will power off." in body

    def test_reconnect_helpers_present_in_js(self, client):
        """system.js exposes the reconnect state machines for both reboot
        and poweroff. Browser-engine tests live in Phase 5 QA — these
        are structural smoke checks only.
        """
        js = client.get("/static/js/system.js").data.decode()
        # The state machine entry point.
        assert "enterReconnectState" in js
        # Polls the M1 health endpoint.
        assert "/api/health" in js
        # 90s deadline before falling back to retry state (per AC).
        assert "90000" in js or "RECONNECT_DEADLINE_MS" in js
        # Restart path's lede mirrors DESIGN.md "Hero: clock service starting".
        assert "Restarting" in js
        # Power off path: 3-phase shutdown (caught in hardware QA — original
        # immediate "safe to unplug" let users yank the plug mid-fsync).
        assert "Shutting down" in js, "phase 1 (services stopping) must be present"
        assert "Almost done" in js, "phase 2 (sync countdown) must be present"
        assert "Safe to unplug" in js, "phase 3 (terminal safe state) must be present"
        assert "POWEROFF_SAFETY_COUNTDOWN_S" in js, (
            "the post-network sync window constant must be a named symbol so future tuning is grep-able"
        )
        assert "20" in js  # the default countdown value

    def test_reconnect_state_styled_in_css(self, client):
        css = client.get("/static/css/system.css").data.decode()
        # Both core states styled.
        assert ".reconnect-state" in css
        assert ".reconnect-state__title" in css
        # Error/retry variant for the 90s-deadline path.
        assert ".reconnect-state--error" in css
        assert ".reconnect-state__retry" in css

    def test_fetch_replaces_native_form_submit(self, client):
        """Story 3.1 swapped the confirm-button native form.submit() for a
        fetch() POST so JS can read the response and switch to the reconnect
        state. Guards against accidental revert.
        """
        js = client.get("/static/js/system.js").data.decode()
        assert "fireAction" in js
        assert "fetch(" in js
        # Form-submit should no longer be the active path on confirm —
        # the fireAction() call took over. Guard: no plain `form.submit()`
        # invocation in the click handler block.
        assert "form.submit()" not in js

    def test_reduced_motion_disables_animation(self, client):
        # M7 OV-2 — confirm-sheet styles (including the reduced-motion block)
        # moved out of system.css into the shared confirm-sheet.css partial.
        # /system + /updates both link it.
        css = client.get("/static/css/confirm-sheet.css").data.decode()
        assert "@media (prefers-reduced-motion: reduce)" in css
        # The reduced-motion block must reset animation on the dialog.
        # We grep for the substring — the block can be formatted multiple ways.
        rm_block_start = css.find("@media (prefers-reduced-motion: reduce)")
        # Find the matching closing brace of the @media block.
        # Crude bracket-balance walk is sufficient here.
        depth = 0
        end = rm_block_start
        seen_open = False
        for i, char in enumerate(css[rm_block_start:], start=rm_block_start):
            if char == "{":
                depth += 1
                seen_open = True
            elif char == "}":
                depth -= 1
                if seen_open and depth == 0:
                    end = i
                    break
        block = css[rm_block_start:end]
        assert "animation: none" in block, (
            "reduced-motion block must set `animation: none` on the dialog "
            "so the slide-up doesn't fire for users who opt out."
        )


# ─── Prepare-for-Gifting card (#317 item 7) ─────────────────────────────────


class TestPrepareForGiftCard:
    """#317 item 7 — Prepare-for-Gifting card markup pins. The card moved
    here from /settings; these tests catch a regression where the card,
    its textarea, its counter, or the dual-form pattern silently drifts."""

    def test_gift_card_renders_on_system_tab(self, client):
        body = client.get("/system").data
        assert b'data-action="prepare_for_gift"' in body
        assert b'name="GIFT_MODE_MESSAGE"' in body
        assert b"data-gift-message-source" in body
        assert b"data-gift-message-sync" in body
        assert b"data-gift-counter" in body
        # Textarea has the maxlength matching GIFT_MODE_MESSAGE_MAX_LEN (#319).
        assert b'maxlength="80"' in body

    def test_gift_card_has_two_forms(self, client):
        """Draft form posts to /settings (centralised persistence); the
        destructive form posts to /api/system/prepare-for-gift. Pin the
        dual-form pattern so a future refactor doesn't accidentally
        collapse them into one. #317 item 7."""
        body = client.get("/system").data.decode()
        assert 'action="/settings"' in body
        assert 'action="/api/system/prepare-for-gift"' in body

    def test_gift_card_pre_fills_message_from_env(self, app, client, tmp_path):
        """Pre-fill source: load_config(env_file) → GIFT_MODE_MESSAGE
        renders into BOTH the textarea body AND the hidden message field
        on the destructive form. Without the second mirror, the no-JS
        destructive POST would send an empty message regardless of what
        the gifter saved."""
        env = tmp_path / "env.sh"
        env.write_text("export GIFT_MODE_MESSAGE='Hello there'\n")
        with app.app_context():
            app.config["ENV_FILE"] = str(env)
            body = client.get("/system").data.decode()
        # Draft textarea body carries the persisted draft.
        assert ">Hello there</textarea>" in body
        # Destructive form's hidden textarea (post-adversarial /review fix —
        # was a hidden <input>; switched to <textarea hidden> so newlines
        # round-trip on the no-JS Prepare path) also carries the draft.
        assert 'name="message" hidden data-gift-message-sync>Hello there</textarea>' in body

    def test_save_draft_failure_renders_on_system_with_field_error(self, client, csrf_token):
        """#317 item 7: a gift-section POST validation failure must
        re-render the System tab (where the textarea lives) with the
        per-field error inline. Otherwise the error message lands on a
        page that doesn't show the gift form."""
        resp = client.post(
            "/settings",
            data={
                "csrf_token": csrf_token,
                "section": "gift",
                "GIFT_MODE_MESSAGE": "hi $(whoami)",
            },
            base_url="http://litclock.local",
            headers={"Origin": "http://litclock.local"},
        )
        assert resp.status_code == 422
        body = resp.data
        assert b'data-action="prepare_for_gift"' in body, "validation failure must re-render /system, not /settings"
        assert b"may not contain" in body

    def test_save_draft_failure_preserves_rejected_input(self, app, client, csrf_token, tmp_path):
        """Adversarial /review CRITICAL fix: when a gift POST fails validation,
        the textarea must reflect the value the user actually typed, NOT
        the last-saved good draft. Otherwise the inline error message
        ('must be at most 80 characters') is meaningless because the
        rejected input has silently vanished from the screen."""
        env = tmp_path / "env.sh"
        env.write_text("export GIFT_MODE_MESSAGE='old saved draft'\n")
        rejected = "x" * 200  # over the 80-char cap → fails validation
        with app.app_context():
            app.config["ENV_FILE"] = str(env)
            resp = client.post(
                "/settings",
                data={
                    "csrf_token": csrf_token,
                    "section": "gift",
                    "GIFT_MODE_MESSAGE": rejected,
                },
                base_url="http://litclock.local",
                headers={"Origin": "http://litclock.local"},
            )
        assert resp.status_code == 422
        body = resp.data.decode()
        assert f">{rejected}</textarea>" in body, (
            "rejected gift-message input must be preserved in the textarea so the user can see what failed and edit it"
        )
        assert ">old saved draft</textarea>" not in body

    def test_save_draft_success_redirects_to_system_tab(self, app, client, csrf_token, tmp_path):
        """PRG destination for section=gift is /system?saved=gift, not
        /settings?saved=gift. Lands the user back on the tab they
        submitted from (#317 item 7)."""
        env = tmp_path / "env.sh"
        env.write_text("GIFT_MODE_MESSAGE=\n")
        with app.app_context():
            app.config["ENV_FILE"] = str(env)
            resp = client.post(
                "/settings",
                data={
                    "csrf_token": csrf_token,
                    "section": "gift",
                    "GIFT_MODE_MESSAGE": "Hi",
                },
                base_url="http://litclock.local",
                headers={"Origin": "http://litclock.local"},
            )
        assert resp.status_code == 303, resp.data
        assert "/system?saved=gift" in resp.headers["Location"]


# ─── #317 item 1: TTL-expiry-mid-typing refresh-and-retry ───────────────────


class TestPrepareForGiftTokenRefreshAndRetry:
    """#317 item 1 (TTL-expiry-mid-typing half): the slow-drafter trap.

    A user opens /system, drafts a welcome message in the textarea, and
    waits 5+ minutes before tapping "Prepare for Gifting". The hidden
    confirm token's 300s TTL expires while they type, so the JS POST
    surfaces a 401 ``confirm_token_invalid`` — the consume-on-failure half
    is already closed (#341), but a TTL-expired token can't be restored.

    Fix (LOCKED — Option 1): on a 401 ``confirm_token_invalid`` for the
    prepare_for_gift action, the JS path mints a fresh token via
    /api/system/confirm-token, swaps it into the hidden field, and
    replays the action POST exactly once. No infinite-401 loops; any
    failure on the retry / refresh-endpoint path falls through to the
    existing alert.

    No browser engine on the dev host → these tests pin the JS source
    contract (the retry branch must exist in the shipped bundle) AND
    exercise the underlying server-side endpoint behavior the JS depends
    on. Mirrors the pattern of test_system_js_includes_all_hidden_inputs_in_post_body
    (#316 source-pin) and test_destructive_button_class_styled_in_settings_css
    (markup pin)."""

    def test_js_source_contains_refresh_and_retry_branch(self, client):
        """Pin the JS retry branch in system.js source so a future refactor
        can't silently revert the TTL-expiry-mid-typing fix.

        Source-pinning (not behavior) because the dev host has no JS
        runtime. The browser-level happy path is exercised in hardware QA;
        the server-side contract is exercised in
        test_confirm_token_can_be_re_minted_after_consume below.

        #317 item 1 codex P2 update: the retry branch must now gate on
        the SPECIFIC ``confirm_token_expired`` slug — gating on
        ``confirm_token_invalid`` (the legacy ambiguous slug) would let
        a double-click / bfcached resubmit silently bypass the single-use
        guard. The ``confirm_token_consumed`` branch must surface a
        specific message and NOT retry."""
        system_js = client.get("/static/js/system.js").data.decode()
        # The retry branch must call out the issue number for grepability.
        assert "#317 item 1" in system_js, (
            "system.js must reference #317 item 1 next to the refresh-and-retry "
            "branch so a future refactor sees the load-bearing context"
        )
        # The refresh endpoint must be called from system.js.
        assert "/api/system/confirm-token" in system_js, (
            "system.js must POST to /api/system/confirm-token on TTL-expired 401"
        )
        # The branch must be scoped to prepare_for_gift — reboot/poweroff/
        # wifi_reset all confirm in seconds, so a stale token there is a
        # real staleness signal worth surfacing rather than papering over.
        assert "action === 'prepare_for_gift'" in system_js, "refresh-and-retry must be scoped to prepare_for_gift only"
        # The retry branch must gate on `confirm_token_expired` ONLY —
        # NOT `confirm_token_invalid` (legacy ambiguous slug that conflated
        # expired-vs-consumed) and NOT `confirm_token_consumed` (would
        # bypass single-use guard on double-submit / bfcache).
        assert "confirm_token_expired" in system_js, (
            "refresh-and-retry must gate on error.code === 'confirm_token_expired' (#317 item 1 codex P2)"
        )
        # One-retry-only enforced via a flag — pin the symbol so an
        # accidental infinite-loop regression trips this test.
        assert "retried" in system_js, (
            "system.js must carry a retried flag enforcing one-retry-only on the refresh path"
        )
        # #317 item 1 codex P2 — explicit `confirm_token_consumed` branch
        # must exist and surface a distinct message. Without this branch,
        # a duplicate POST would fall through to the generic alert path
        # AND (worse) the legacy `confirm_token_invalid` gate would have
        # triggered a refresh-and-retry, bypassing single-use.
        assert "confirm_token_consumed" in system_js, (
            "system.js must surface a specific message on `confirm_token_consumed` "
            "(must NOT refresh-and-retry — that bypasses the single-use guard)"
        )

    def test_js_does_not_retry_on_confirm_token_consumed(self, client):
        """#317 item 1 codex P2 source-pin: the `confirm_token_consumed`
        path must NEVER call `refreshTokenAndRetry`. If a refactor reflows
        the branch order so the retry trigger fires before the consumed
        check, a duplicate destructive action could double-fire.

        Static-analysis pin via the source ordering: the
        `confirm_token_expired` retry trigger must check
        ``code === 'confirm_token_expired'`` (not the ambiguous old slug)
        AND the `confirm_token_consumed` handler must explicitly return
        without entering `refreshTokenAndRetry`. We grep for the literal
        sequence that proves this contract."""
        system_js = client.get("/static/js/system.js").data.decode()
        # The retry call exists, but its gate must be the specific
        # `confirm_token_expired` slug, not the legacy ambiguous one.
        retry_call_site = system_js.find("refreshTokenAndRetry(form, action, tokenInput")
        assert retry_call_site != -1, "refreshTokenAndRetry call site must still exist for the TTL-expiry path"
        # Locate the gate IF condition immediately preceding the call.
        gate_window = system_js[max(0, retry_call_site - 400) : retry_call_site]
        assert "confirm_token_expired" in gate_window, (
            "the IF gate preceding refreshTokenAndRetry must check "
            "'confirm_token_expired' (not 'confirm_token_invalid')"
        )

    def test_confirm_token_can_be_re_minted_after_expired(self, client, tmp_path):
        """Server-side contract underpinning the JS refresh-and-retry: when
        the prepare_for_gift token TTL has passed, the route returns 401
        ``confirm_token_expired`` and the user can mint a fresh one via
        /api/system/confirm-token and replay the action.

        #317 item 1 codex P2: the JS retry path keys off the
        ``confirm_token_expired`` slug (NOT the legacy
        ``confirm_token_invalid`` slug, which would now indicate a
        malformed token — not a TTL expiry — and would also fire on
        ``confirm_token_consumed`` which the JS MUST NOT retry).
        """
        from flask import current_app

        from control_server.confirm_tokens import ConfirmTokenStore

        msg_path = tmp_path / "gift-message"
        # Inject a microscopic-TTL store so issued tokens expire immediately.
        with client.application.app_context():
            current_app.extensions["confirm_tokens"] = ConfirmTokenStore(ttl_seconds=0)

        stale_token = _issue_token(client, "prepare_for_gift")
        time.sleep(0.01)  # let monotonic clock pass the zero-TTL expiry

        with (
            patch("control_server.routes.system.GIFT_MESSAGE_PATH", str(msg_path)),
            patch("control_server.routes.system.subprocess.run"),
            patch("control_server.routes.system.update_state.update_is_busy", return_value=False),
        ):
            # Stale (TTL-expired) token: server returns 401 confirm_token_expired.
            expired_response = client.post(
                "/api/system/prepare-for-gift",
                json={"token": stale_token, "message": "Hi"},
            )
            assert expired_response.status_code == 401
            assert expired_response.json["error"]["code"] == "confirm_token_expired"

            # JS now mints a fresh token via /api/system/confirm-token.
            refresh = client.post(
                "/api/system/confirm-token",
                json={"action": "prepare_for_gift"},
            )
            assert refresh.status_code == 200, refresh.json
            fresh_token = refresh.json["token"]
            assert isinstance(fresh_token, str) and fresh_token
            assert fresh_token != stale_token, "refresh must mint a NEW token"

            # Retry the action POST with the fresh token — succeeds.
            # But the fresh token must also not have a zero-TTL inheritance —
            # restore the normal TTL for the retry to be valid in this test.
            with client.application.app_context():
                from control_server.confirm_tokens import ConfirmTokenStore as _CTS

                current_app.extensions["confirm_tokens"] = _CTS()
            # Re-mint via the now-normal store; the in-test-only TTL reset
            # is a quirk of microscopic-TTL injection, not a property of
            # the route — production never resets the store this way.
            refresh2 = client.post(
                "/api/system/confirm-token",
                json={"action": "prepare_for_gift"},
            )
            assert refresh2.status_code == 200
            fresh_token2 = refresh2.json["token"]
            retry = client.post(
                "/api/system/prepare-for-gift",
                json={"token": fresh_token2, "message": "Hi"},
            )
            assert retry.status_code == 200, retry.json
            assert retry.json["ok"] is True

    def test_consumed_token_returns_409_not_401(self, client, tmp_path):
        """#317 item 1 codex P2: the single-use guard now reports 409
        ``confirm_token_consumed`` (was 401 ``confirm_token_invalid``)
        on a duplicate POST. This is the critical-path discriminator —
        the JS gates its refresh-and-retry on ``confirm_token_expired``
        ONLY, so this distinct slug is what makes the double-submit /
        bfcache window safe to ship."""
        msg_path = tmp_path / "gift-message"
        token = _issue_token(client, "prepare_for_gift")
        with (
            patch("control_server.routes.system.GIFT_MESSAGE_PATH", str(msg_path)),
            patch("control_server.routes.system.subprocess.run"),
            patch("control_server.routes.system.update_state.update_is_busy", return_value=False),
        ):
            first = client.post(
                "/api/system/prepare-for-gift",
                json={"token": token, "message": "Hi"},
            )
            assert first.status_code == 200, first.json
            # Replay (simulates double-click / bfcached resubmit):
            replay = client.post(
                "/api/system/prepare-for-gift",
                json={"token": token, "message": "Hi"},
            )
            assert replay.status_code == 409, replay.json
            assert replay.json["error"]["code"] == "confirm_token_consumed"
            # The user-facing message should hint that this was already submitted.
            assert "already" in replay.json["error"]["message"].lower()

    def test_refresh_endpoint_rejects_unknown_action(self, client):
        """Defense pin: the refresh endpoint already rejects unknown
        actions with 400 ``invalid_action``. The JS refresh-and-retry path
        only ever sends ``prepare_for_gift`` (scoped in source), but if a
        future refactor widens it accidentally, the server still gates."""
        response = client.post(
            "/api/system/confirm-token",
            json={"action": "make-coffee"},
        )
        assert response.status_code == 400
        assert response.json["error"]["code"] == "invalid_action"


# ─── /api/system/reset — Factory reset (#510) ───────────────────────────────


class TestFactoryResetRoute:
    """#510: PWA-triggered Factory reset. Full-wipe sibling of /api/wifi/reset —
    dispatches litclock-reset.service (reset-setup.sh --wipe-wifi --reboot --yes),
    which wipes config + WiFi and reboots into first-boot setup. Same confirm-token
    + rate-limit + update-busy gates as wifi_reset."""

    EXPECTED_ARGV = ["sudo", "/usr/bin/systemctl", "start", "--no-block", "litclock-reset.service"]

    @pytest.fixture(autouse=True)
    def _bypass_update_busy_gate(self):
        with patch("control_server.routes.system.update_state.update_is_busy", return_value=False):
            yield

    def test_success_dispatches_reset_unit(self, client):
        token = _issue_token(client, "factory_reset")
        with patch("control_server.routes.system.subprocess.run") as mock_run:
            response = client.post("/api/system/reset", json={"token": token})
        assert response.status_code == 200, response.json
        assert response.json["ok"] is True
        mock_run.assert_called_once()
        assert mock_run.call_args.args[0] == self.EXPECTED_ARGV

    def test_missing_token_returns_401(self, client):
        with patch("control_server.routes.system.subprocess.run") as mock_run:
            response = client.post("/api/system/reset", json={})
        assert response.status_code == 401
        assert response.json["error"]["code"] == "confirm_token_invalid"
        mock_run.assert_not_called()

    def test_token_is_single_use(self, client):
        token = _issue_token(client, "factory_reset")
        with patch("control_server.routes.system.subprocess.run"):
            first = client.post("/api/system/reset", json={"token": token})
        assert first.status_code == 200
        # Replay the consumed token → not ok.
        with patch("control_server.routes.system.subprocess.run") as mock_run:
            second = client.post("/api/system/reset", json={"token": token})
        assert second.status_code != 200
        mock_run.assert_not_called()

    def test_wrong_action_token_rejected(self, client):
        # A wifi_reset token must not be replayable against factory_reset.
        token = _issue_token(client, "wifi_reset")
        with patch("control_server.routes.system.subprocess.run") as mock_run:
            response = client.post("/api/system/reset", json={"token": token})
        assert response.status_code != 200
        mock_run.assert_not_called()

    def test_update_in_progress_returns_409_no_dispatch(self, client):
        token = _issue_token(client, "factory_reset")
        with (
            patch("control_server.routes.system.update_state.update_is_busy", return_value=True),
            patch("control_server.routes.system.subprocess.run") as mock_run,
        ):
            response = client.post("/api/system/reset", json={"token": token})
        assert response.status_code == 409
        assert response.json["error"]["code"] == "update_in_progress"
        mock_run.assert_not_called()

    def test_failed_start_restores_token_and_500(self, client):
        """A non-zero systemctl (masked/missing unit, the #327 class) → 500 and the
        token is restored so a retry works on the same page (idempotent unit)."""
        token = _issue_token(client, "factory_reset")
        err = subprocess.CalledProcessError(4, self.EXPECTED_ARGV, stderr=b"Unit not found")
        with patch("control_server.routes.system.subprocess.run", side_effect=err):
            first = client.post("/api/system/reset", json={"token": token})
        assert first.status_code == 500
        assert first.json["error"]["code"] == "factory_reset_failed"
        # Token restored → the same token now succeeds on retry.
        with patch("control_server.routes.system.subprocess.run") as mock_run:
            retry = client.post("/api/system/reset", json={"token": token})
        assert retry.status_code == 200, retry.json
        mock_run.assert_called_once()

    def test_timeout_does_not_restore_token(self, client):
        """On timeout the unit may have dispatched — token stays consumed to avoid
        double-firing the wipe."""
        token = _issue_token(client, "factory_reset")
        err = subprocess.TimeoutExpired(self.EXPECTED_ARGV, 5)
        with patch("control_server.routes.system.subprocess.run", side_effect=err):
            first = client.post("/api/system/reset", json={"token": token})
        assert first.status_code == 500
        # Retry with the same (consumed, not restored) token → not ok, no dispatch.
        with patch("control_server.routes.system.subprocess.run") as mock_run:
            retry = client.post("/api/system/reset", json={"token": token})
        assert retry.status_code != 200
        mock_run.assert_not_called()
