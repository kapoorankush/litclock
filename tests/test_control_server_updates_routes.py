"""Tests for /api/update/{check,apply,status} + /updates page (litclock-dev#245 M5).

Covers:
- D1 — apply triggers `sudo systemctl start --no-block litclock-update.service`
- D5/F7 — busy gate (is-active OR list-jobs)
- D6 — shared GH cache + 6h TTL + atomic mv-tmp + corrupt-JSON tolerance
- D9 — status-file mirroring (idle / running / complete / failed_*)
- D10 — 409 envelopes for update_in_progress + already_up_to_date + threading.Lock
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from control_server import create_app  # noqa: E402


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("LITCLOCK_UPDATE_CHECK_CACHE", str(tmp_path / "update-check.json"))
    monkeypatch.setenv("LITCLOCK_UPDATE_STATUS_FILE", str(tmp_path / "update.status"))
    monkeypatch.setenv("LITCLOCK_VERSION_OVERRIDE", "v0.210.0")
    # litclock-dev#607 — /updates and /api/update/status now consult the unit-busy gate
    # (conftest.py additionally points LITCLOCK_SYSTEMCTL at a nonexistent
    # path so no test can shell the host's systemd). Default the gate to
    # idle here for determinism; tests that exercise the busy inference
    # patch it back to True explicitly. The routes memoize the gate for 2s
    # (module state), so reset the memo per test or a prior test's answer
    # leaks into this one.
    from control_server import update_state  # noqa: PLC0415
    from control_server.routes import updates as updates_routes  # noqa: PLC0415

    monkeypatch.setattr(update_state, "update_is_busy", lambda: False)
    updates_routes._reset_busy_memo()
    app = create_app(test_config={"VERSION_OVERRIDE": "v0.210.0"})
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def _scrape_apply_token(client) -> str:
    page = client.get("/updates").data.decode()
    match = re.search(r'name="token" value="([^"]+)"', page)
    if match:
        return match.group(1)
    # When `available` is False the form is suppressed; mint via the
    # ConfirmTokenStore directly for tests that need to call /api/update/apply.
    store = client.application.extensions["confirm_tokens"]
    return store.issue("update_apply")[0]


# ───────────────────── /updates page ───────────────────────────────────────


class TestUpdatesPage:
    def test_renders_with_active_tab_marker(self, client):
        response = client.get("/updates")
        assert response.status_code == 200
        decoded = response.data.decode()
        # Active tab marker.
        label_pos = decoded.find(">Updates<")
        assert label_pos > 0
        anchor_start = decoded.rfind("<a", 0, label_pos)
        assert "aria-current" in decoded[anchor_start:label_pos]

    def test_renders_pill_when_no_cache(self, client):
        # No cache file → unknown pill state with the in-flight "checking…"
        # label (JS is about to fire /api/update/check momentarily).
        body = client.get("/updates").data
        assert b"updates-pill" in body
        assert b'data-state="unknown"' in body or b"updates-pill--unknown" in body
        # litclock-dev#381 — initial-load pill is "checking…" (JS is firing the check)
        assert b"checking" in body, body[:500]
        # And NOT "couldn't check" (that's the terminal-failed label)
        assert b"couldn't check" not in body

    def test_renders_couldnt_check_pill_when_cache_says_available_is_null(self, client):
        """litclock-dev#381 regression — when the cached check landed with
        ``available: null`` (the graceful-degraded payload that
        ``build_check_payload`` writes after a GH /tags failure — typical
        on a private-repo Pi without a PAT), the pill MUST say
        "couldn't check", NOT "checking…". The old label left users
        staring at a spinner that wasn't spinning.
        """
        cache = Path(os.environ.get("LITCLOCK_UPDATE_CHECK_CACHE"))
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps(
                {
                    "fetched_at_unix": 9999999999,
                    "current_version": "v0.212.2",
                    "latest_tag": None,
                    "available": None,
                    "release_notes": None,
                }
            )
        )
        body = client.get("/updates").data
        assert b"updates-pill--unknown" in body
        assert b"couldn't check" in body, (
            f"expected 'couldn't check' label for terminal-unknown state; got: {body[:800]!r}"
        )
        # And NOT the in-flight "checking…" label.
        # (use a marker that only appears in the checking branch to avoid
        # false positives from comments/docs containing "checking")
        assert b'updates-pill--unknown" role="status">checking' not in body

    def test_renders_phase_reading_list_skeleton(self, client):
        body = client.get("/updates").data.decode()
        # All 7 D3 phase names must appear in the skeleton.
        for phase in [
            "Checking for updates",
            "Pulling new code",
            "Syncing quote images",
            "Updating Python packages",
            "Verifying clock starts",
            "Installing services",
            "Restarting",
        ]:
            assert phase in body
        assert 'id="phase-reading-list"' in body

    def test_loads_updates_css_and_js(self, client):
        body = client.get("/updates").data
        assert b"css/updates.css" in body
        assert b"js/updates.js" in body

    @pytest.mark.parametrize(
        "bad_value",
        [
            float("nan"),
            float("inf"),
            float("-inf"),
            1e308 * 10,  # OverflowError on fromtimestamp
            -1e308 * 10,
        ],
    )
    def test_renders_with_pathological_fetched_at_without_500(self, client, bad_value):
        """Codex /review M7: a corrupted cache containing NaN/Infinity/out-
        of-range floats must not 500 the page. isinstance() is True for
        these but datetime.fromtimestamp() raises ValueError/OverflowError;
        the F-004 server-side relative-time render now catches and falls
        back to "—"."""
        cache = Path(os.environ.get("LITCLOCK_UPDATE_CHECK_CACHE"))
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps(
                {
                    "fetched_at_unix": bad_value,
                    "current_version": "v0.210.0",
                    "latest_tag": "v0.210.0",
                    "available": False,
                },
                # NaN/Inf are not strict-JSON; allow_nan=True is Python's default.
            )
        )
        response = client.get("/updates")
        assert response.status_code == 200, response.data[:200]


# ───────────────────── /api/update/check ───────────────────────────────────


class TestApiUpdateCheck:
    def test_returns_cached_payload_when_fresh(self, client, tmp_path, monkeypatch):
        cache = Path(os.environ.get("LITCLOCK_UPDATE_CHECK_CACHE"))
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps(
                {
                    "fetched_at_unix": 9999999999,
                    "current_version": "v0.210.0",
                    "latest_tag": "v0.211.0",
                    "available": True,
                    "release_notes": "## v0.211.0\n- M5 shipped",
                }
            )
        )
        response = client.get("/api/update/check")
        assert response.status_code == 200
        body = response.json
        assert body["ok"] is True
        assert body["latest_tag"] == "v0.211.0"
        assert body["available"] is True

    def test_refetches_when_cache_stale(self, client, monkeypatch):
        cache = Path(os.environ.get("LITCLOCK_UPDATE_CHECK_CACHE"))
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps(
                {
                    "fetched_at_unix": 0,  # 1970 → very stale
                    "current_version": "v0.210.0",
                    "latest_tag": "v0.210.0",
                    "available": False,
                }
            )
        )
        with (
            patch("control_server.update_state.fetch_latest_release_tag", return_value="v0.211.0"),
            patch("control_server.update_state.fetch_release_notes", return_value="notes"),
        ):
            response = client.get("/api/update/check")
        assert response.status_code == 200
        body = response.json
        assert body["latest_tag"] == "v0.211.0"
        assert body["available"] is True

    def test_corrupt_cache_refetches_without_500(self, client, monkeypatch):
        # F13 — corrupt JSON in the cache file is tolerated (refetch + overwrite).
        cache = Path(os.environ.get("LITCLOCK_UPDATE_CHECK_CACHE"))
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text("{ this is not valid json")
        with (
            patch("control_server.update_state.fetch_latest_release_tag", return_value="v0.210.0"),
            patch("control_server.update_state.fetch_release_notes", return_value=None),
        ):
            response = client.get("/api/update/check")
        assert response.status_code == 200
        # Cache is now well-formed.
        re_read = json.loads(cache.read_text())
        assert re_read["latest_tag"] == "v0.210.0"

    def test_network_failure_returns_graceful_payload(self, client):
        with patch("control_server.update_state.fetch_latest_release_tag", return_value=None):
            response = client.get("/api/update/check")
        assert response.status_code == 200
        body = response.json
        assert body["ok"] is True
        assert body["latest_tag"] is None
        assert body["available"] in (False, None)


# ───────────────────── /api/update/apply ───────────────────────────────────


class TestApiUpdateApply:
    def test_happy_path_dispatches_systemctl(self, client, monkeypatch):
        # Seed a cache that says an update IS available so we don't hit the
        # already_up_to_date gate.
        cache = Path(os.environ.get("LITCLOCK_UPDATE_CHECK_CACHE"))
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps(
                {
                    "fetched_at_unix": 9999999999,
                    "current_version": "v0.210.0",
                    "latest_tag": "v0.211.0",
                    "available": True,
                }
            )
        )
        token = _scrape_apply_token(client)
        with (
            patch("control_server.routes.updates.update_state.update_is_busy", return_value=False),
            patch("control_server.routes.updates.subprocess.run") as mock_run,
        ):
            response = client.post(
                "/api/update/apply",
                json={"token": token},
            )
        assert response.status_code == 202, response.json
        body = response.json
        assert body["ok"] is True
        assert "started_at_unix" in body
        # Dispatched via sudo systemctl start --no-block litclock-update.service
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "sudo"
        assert args[-3:] == ["start", "--no-block", "litclock-update.service"]

    def test_busy_returns_409_update_in_progress(self, client, monkeypatch):
        cache = Path(os.environ.get("LITCLOCK_UPDATE_CHECK_CACHE"))
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps(
                {
                    "fetched_at_unix": 9999999999,
                    "current_version": "v0.210.0",
                    "latest_tag": "v0.211.0",
                    "available": True,
                }
            )
        )
        token = _scrape_apply_token(client)
        with (
            patch("control_server.routes.updates.update_state.update_is_busy", return_value=True),
            patch("control_server.routes.updates.subprocess.run") as mock_run,
        ):
            response = client.post("/api/update/apply", json={"token": token})
        assert response.status_code == 409
        body = response.json
        assert body["ok"] is False
        assert body["error"]["code"] == "update_in_progress"
        mock_run.assert_not_called()

    def test_already_up_to_date_returns_409(self, client, monkeypatch):
        cache = Path(os.environ.get("LITCLOCK_UPDATE_CHECK_CACHE"))
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps(
                {
                    "fetched_at_unix": 9999999999,
                    "current_version": "v0.210.0",
                    "latest_tag": "v0.210.0",
                    "available": False,
                }
            )
        )
        # /updates page suppresses the apply button when not available, so
        # mint a token directly to drive the route.
        store = client.application.extensions["confirm_tokens"]
        token = store.issue("update_apply")[0]
        with (
            patch("control_server.routes.updates.update_state.update_is_busy", return_value=False),
            patch("control_server.routes.updates.subprocess.run") as mock_run,
        ):
            response = client.post("/api/update/apply", json={"token": token})
        assert response.status_code == 409
        body = response.json
        assert body["error"]["code"] == "already_up_to_date"
        assert body["error"]["current_version"] == "v0.210.0"
        mock_run.assert_not_called()

    def test_missing_token_returns_401(self, client):
        response = client.post("/api/update/apply", json={})
        assert response.status_code == 401
        body = response.json
        assert body["error"]["code"] == "confirm_token_invalid"

    def test_replayed_token_returns_409_consumed(self, client, monkeypatch):
        """litclock-dev#317 item 1 codex P2: a replay of a consumed update_apply token
        now returns 409 ``confirm_token_consumed`` (was 401
        ``confirm_token_invalid``). Distinct slug so the client can refuse
        the refresh-and-retry path that would otherwise double-fire the
        destructive action."""
        cache = Path(os.environ.get("LITCLOCK_UPDATE_CHECK_CACHE"))
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps(
                {
                    "fetched_at_unix": 9999999999,
                    "current_version": "v0.210.0",
                    "latest_tag": "v0.211.0",
                    "available": True,
                }
            )
        )
        store = client.application.extensions["confirm_tokens"]
        token = store.issue("update_apply")[0]
        with (
            patch("control_server.routes.updates.update_state.update_is_busy", return_value=False),
            patch("control_server.routes.updates.subprocess.run"),
        ):
            first = client.post("/api/update/apply", json={"token": token})
        assert first.status_code == 202
        # Second use of the same token must fail with consumed (single-use guard).
        second = client.post("/api/update/apply", json={"token": token})
        assert second.status_code == 409
        assert second.json["error"]["code"] == "confirm_token_consumed"

    def test_systemctl_failure_returns_500_envelope(self, client, monkeypatch):
        import subprocess

        cache = Path(os.environ.get("LITCLOCK_UPDATE_CHECK_CACHE"))
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps(
                {
                    "fetched_at_unix": 9999999999,
                    "current_version": "v0.210.0",
                    "latest_tag": "v0.211.0",
                    "available": True,
                }
            )
        )
        store = client.application.extensions["confirm_tokens"]
        token = store.issue("update_apply")[0]
        with (
            patch("control_server.routes.updates.update_state.update_is_busy", return_value=False),
            patch(
                "control_server.routes.updates.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "systemctl", stderr=b"boom"),
            ),
        ):
            response = client.post("/api/update/apply", json={"token": token})
        assert response.status_code == 500
        assert response.json["error"]["code"] == "update_dispatch_failed"

    # ─── litclock-dev#328 — restore-on-failure regressions ──────────────────────────────

    def test_called_process_error_restores_token_for_retry(self, client):
        """litclock-dev#328: systemctl returned non-zero BEFORE the update unit
        started — the box is still up and the user's retry should hit
        the real error again, not a spurious "token already used" 401."""
        import subprocess

        cache = Path(os.environ.get("LITCLOCK_UPDATE_CHECK_CACHE"))
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps(
                {
                    "fetched_at_unix": 9999999999,
                    "current_version": "v0.210.0",
                    "latest_tag": "v0.211.0",
                    "available": True,
                }
            )
        )
        store = client.application.extensions["confirm_tokens"]
        token = store.issue("update_apply")[0]

        with (
            patch("control_server.routes.updates.update_state.update_is_busy", return_value=False),
            patch(
                "control_server.routes.updates.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "systemctl", stderr=b"boom"),
            ),
        ):
            first = client.post("/api/update/apply", json={"token": token})
        assert first.status_code == 500
        assert first.json["error"]["code"] == "update_dispatch_failed"

        # Retry with the SAME token — token was restored, dispatch fires.
        with (
            patch("control_server.routes.updates.update_state.update_is_busy", return_value=False),
            patch("control_server.routes.updates.subprocess.run") as mock_run,
        ):
            second = client.post("/api/update/apply", json={"token": token})
        assert second.status_code == 202, second.json
        mock_run.assert_called_once()

    def test_update_busy_restores_token_for_retry(self, client):
        """litclock-dev#328: 409 update_in_progress is pre-side-effect — restore so the
        second tab racing on the same token can retry after the first
        finishes. Acceptance-criteria mapping: two PWA tabs both tap
        Apply; first wins (202), second sees 409, second can retry after
        first completes (gate-restore worked)."""
        cache = Path(os.environ.get("LITCLOCK_UPDATE_CHECK_CACHE"))
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps(
                {
                    "fetched_at_unix": 9999999999,
                    "current_version": "v0.210.0",
                    "latest_tag": "v0.211.0",
                    "available": True,
                }
            )
        )
        store = client.application.extensions["confirm_tokens"]
        token = store.issue("update_apply")[0]

        with (
            patch("control_server.routes.updates.update_state.update_is_busy", return_value=True),
            patch("control_server.routes.updates.subprocess.run") as mock_run,
        ):
            first = client.post("/api/update/apply", json={"token": token})
        assert first.status_code == 409
        assert first.json["error"]["code"] == "update_in_progress"
        mock_run.assert_not_called()

        # Same token, gate now clear — must dispatch.
        with (
            patch("control_server.routes.updates.update_state.update_is_busy", return_value=False),
            patch("control_server.routes.updates.subprocess.run") as mock_run,
        ):
            second = client.post("/api/update/apply", json={"token": token})
        assert second.status_code == 202, second.json
        mock_run.assert_called_once()

    def test_already_up_to_date_restores_token_for_retry(self, client):
        """litclock-dev#328: 409 already_up_to_date is pre-side-effect — no dispatch.
        Restore so the user can re-tap after a new release lands
        without a page reload (rare but explicit per the plan's expanded
        scope of "ALL pre-side-effect failure paths")."""
        cache = Path(os.environ.get("LITCLOCK_UPDATE_CHECK_CACHE"))
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps(
                {
                    "fetched_at_unix": 9999999999,
                    "current_version": "v0.210.0",
                    "latest_tag": "v0.210.0",
                    "available": False,
                }
            )
        )
        store = client.application.extensions["confirm_tokens"]
        token = store.issue("update_apply")[0]

        with (
            patch("control_server.routes.updates.update_state.update_is_busy", return_value=False),
            patch("control_server.routes.updates.subprocess.run") as mock_run,
        ):
            first = client.post("/api/update/apply", json={"token": token})
        assert first.status_code == 409
        assert first.json["error"]["code"] == "already_up_to_date"
        mock_run.assert_not_called()

        # Operator pushed a new release → flip cache.
        cache.write_text(
            json.dumps(
                {
                    "fetched_at_unix": 9999999999,
                    "current_version": "v0.210.0",
                    "latest_tag": "v0.211.0",
                    "available": True,
                }
            )
        )
        with (
            patch("control_server.routes.updates.update_state.update_is_busy", return_value=False),
            patch("control_server.routes.updates.subprocess.run") as mock_run,
        ):
            second = client.post("/api/update/apply", json={"token": token})
        assert second.status_code == 202, second.json
        mock_run.assert_called_once()


# ───────────────────── /api/update/status ──────────────────────────────────


class TestApiUpdateStatus:
    def test_idle_when_file_missing(self, client):
        response = client.get("/api/update/status")
        assert response.status_code == 200
        assert response.json == {"ok": True, "state": "idle"}

    def test_mirrors_running_payload(self, client, monkeypatch):
        status_file = Path(os.environ.get("LITCLOCK_UPDATE_STATUS_FILE"))
        status_file.parent.mkdir(parents=True, exist_ok=True)
        status_file.write_text(
            json.dumps(
                {
                    "state": "running",
                    "phase_index": 4,
                    "phase_name": "Updating Python packages",
                    "started_at_unix": 1700000000,
                    "finished_at_unix": None,
                    "from_version": "v0.210.0",
                    "to_version": "v0.211.0",
                    "error": None,
                }
            )
        )
        response = client.get("/api/update/status")
        assert response.status_code == 200
        body = response.json
        assert body["state"] == "running"
        assert body["phase_index"] == 4
        assert body["phase_name"] == "Updating Python packages"

    def test_corrupt_status_returns_stale(self, client, monkeypatch):
        status_file = Path(os.environ.get("LITCLOCK_UPDATE_STATUS_FILE"))
        status_file.parent.mkdir(parents=True, exist_ok=True)
        status_file.write_text("{ corrupt")
        response = client.get("/api/update/status")
        assert response.status_code == 200
        assert response.json["state"] == "stale"

    def test_failed_reverted_passthrough(self, client, monkeypatch):
        status_file = Path(os.environ.get("LITCLOCK_UPDATE_STATUS_FILE"))
        status_file.parent.mkdir(parents=True, exist_ok=True)
        status_file.write_text(
            json.dumps(
                {
                    "state": "failed_reverted",
                    "phase_index": 5,
                    "phase_name": "Verifying clock starts",
                    "started_at_unix": 1700000000,
                    "finished_at_unix": 1700000060,
                    "from_version": "v0.210.0",
                    "to_version": "v0.211.0",
                    "error": "Smoke test failed; reverted to v0.210.0.",
                }
            )
        )
        body = client.get("/api/update/status").json
        assert body["state"] == "failed_reverted"
        assert "reverted" in body["error"]

    def test_idle_file_with_busy_unit_reports_running(self, client):
        """litclock-dev#607 — the dispatch race: Apply fired systemctl --no-block but
        Phase 1 hasn't written the status file yet. A poll in that window
        used to see idle, and the PWA read idle as 'nothing running' and
        fell back to the stale Apply card mid-update. The unit is the
        authority: busy + no file = running."""
        with patch("control_server.routes.updates.update_state.update_is_busy", return_value=True):
            body = client.get("/api/update/status").json
        assert body["state"] == "running"
        assert body["phase_index"] == 1

    def test_stale_file_is_not_inferred_running_even_when_busy(self, client):
        """litclock-dev#607 review — a torn/corrupt read self-corrects within one atomic-
        write tick, and inventing phase 1 for it would visually regress a
        mid-run reading list. Stale passes through untouched; the JS stale
        branch just re-polls."""
        status_file = Path(os.environ.get("LITCLOCK_UPDATE_STATUS_FILE"))
        status_file.parent.mkdir(parents=True, exist_ok=True)
        status_file.write_text("{ corrupt")
        with patch("control_server.routes.updates.update_state.update_is_busy", return_value=True):
            body = client.get("/api/update/status").json
        assert body["state"] == "stale"

    def test_terminal_states_are_never_overridden_by_busy(self, client):
        """litclock-dev#607 — complete/failed_* + a still-busy unit is the updater's
        normal tail (terminal state written, services restarting). The file's
        verdict must pass through untouched or the PWA would bounce a
        finished update back to 'running'."""
        status_file = Path(os.environ.get("LITCLOCK_UPDATE_STATUS_FILE"))
        status_file.parent.mkdir(parents=True, exist_ok=True)
        for terminal in ("complete", "failed_reverted", "failed_unrecovered"):
            status_file.write_text(json.dumps({"state": terminal, "phase_index": 7}))
            with patch("control_server.routes.updates.update_state.update_is_busy", return_value=True) as mock_busy:
                body = client.get("/api/update/status").json
            assert body["state"] == terminal, terminal
            mock_busy.assert_not_called()

    def test_idle_with_idle_unit_stays_idle(self, client):
        """The fixture defaults update_is_busy to False — pin that idle
        really means idle when the unit agrees (no inference misfire)."""
        body = client.get("/api/update/status").json
        assert body["state"] == "idle"

    def _write_running_file(self):
        status_file = Path(os.environ.get("LITCLOCK_UPDATE_STATUS_FILE"))
        status_file.parent.mkdir(parents=True, exist_ok=True)
        status_file.write_text(json.dumps({"state": "running", "phase_index": 3}))

    def test_running_with_busy_unit_reports_unit_busy_true(self, client):
        """litclock-dev#636 — a live run carries unit_busy=True so the poll loop's
        dead-updater counter stays at zero for the whole run."""
        self._write_running_file()
        with patch("control_server.routes.updates.update_state.update_is_busy", return_value=True):
            body = client.get("/api/update/status").json
        assert body["state"] == "running"
        assert body["unit_busy"] is True

    def test_running_with_idle_unit_keeps_state_but_reports_unit_busy_false(self, client):
        """litclock-dev#636 — the dead-updater case (SIGKILL/OOM skipped the EXIT trap;
        the file lies until reboot clears tmpfs). The route must NOT flip
        the state itself — one sample can be the 2s memo lagging the
        dispatch window, and a false 'dead' mid-run would resurrect the
        litclock-dev#607 stale-card bug. It reports the evidence and the JS requires
        several consecutive idle readings before acting."""
        self._write_running_file()
        body = client.get("/api/update/status").json
        assert body["state"] == "running"
        assert body["phase_index"] == 3
        assert body["unit_busy"] is False

    def test_inferred_running_reports_unit_busy_true(self, client):
        """litclock-dev#636 — the litclock-dev#607 inferred-busy payload exists only because the
        unit IS busy, so its unit_busy must be exactly True (a False here
        would mean the inference and the evidence contradict each other)."""
        with patch("control_server.routes.updates.update_state.update_is_busy", return_value=True):
            body = client.get("/api/update/status").json
        assert body["state"] == "running"
        assert body["inferred"] == "unit-busy"
        assert body["unit_busy"] is True

    def test_non_running_states_carry_no_unit_busy_key(self, client):
        """litclock-dev#636 — the evidence field is scoped to the one state whose
        truthfulness it qualifies; terminal and idle payloads stay
        byte-compatible with pre-litclock-dev#636 clients."""
        status_file = Path(os.environ.get("LITCLOCK_UPDATE_STATUS_FILE"))
        status_file.parent.mkdir(parents=True, exist_ok=True)
        for state in ("complete", "failed_reverted", "failed_unrecovered"):
            status_file.write_text(json.dumps({"state": state, "phase_index": 7}))
            body = client.get("/api/update/status").json
            assert "unit_busy" not in body, state
        status_file.unlink()
        body = client.get("/api/update/status").json
        assert body["state"] == "idle"
        assert "unit_busy" not in body

    def test_stale_state_carries_no_unit_busy_key(self, client):
        """litclock-dev#636 — the unit_busy field is scoped to `running`; a torn
        or oversize file (state=stale) must not carry it either, even when the
        unit is busy. The JS stale branch just re-polls; a stray unit_busy
        there would be meaningless."""
        status_file = Path(os.environ.get("LITCLOCK_UPDATE_STATUS_FILE"))
        status_file.parent.mkdir(parents=True, exist_ok=True)
        status_file.write_text("{ corrupt")
        with patch("control_server.routes.updates.update_state.update_is_busy", return_value=True):
            body = client.get("/api/update/status").json
        assert body["state"] == "stale"
        assert "unit_busy" not in body

    def test_oversize_status_file_returns_stale(self, client):
        """litclock-dev#336 — 1MB junk at update.status (above 8KB cap) must be rejected
        by the shared bounded reader and reported as state=stale, NOT a 500
        and NOT an OOM. Mirrors what update_state.read_status_file does for
        the corrupt-JSON case."""
        status_file = Path(os.environ.get("LITCLOCK_UPDATE_STATUS_FILE"))
        status_file.parent.mkdir(parents=True, exist_ok=True)
        status_file.write_text("X" * (1024 * 1024))
        response = client.get("/api/update/status")
        assert response.status_code == 200
        assert response.json["state"] == "stale"

    def test_fifo_at_status_file_does_not_hang(self, client):
        """litclock-dev#336 — FIFO at the status path would block forever on open() if
        the bounded reader didn't gate via lstat + S_ISREG. With the gate,
        read_status_file returns stale (file exists per lstat but isn't
        a regular file, so safe_read_json refuses)."""
        import os as _os

        status_file = Path(os.environ.get("LITCLOCK_UPDATE_STATUS_FILE"))
        status_file.parent.mkdir(parents=True, exist_ok=True)
        if status_file.exists():
            status_file.unlink()
        _os.mkfifo(status_file)
        response = client.get("/api/update/status")
        assert response.status_code == 200
        assert response.json["state"] == "stale"

    def test_symlink_at_status_file_is_rejected_as_stale(self, client, tmp_path):
        """litclock-dev#336 — a symlink-to-regular-file at the status path must be
        rejected by lstat + S_ISREG. Without lstat (i.e. with Path.stat),
        the symlink would be followed and a planted file could spoof the
        status. The bounded reader rejects → state=stale."""
        import json as _json
        import os as _os

        target = tmp_path / "real-status.json"
        target.write_text(_json.dumps({"state": "complete", "to_version": "spoof01"}))
        status_file = Path(os.environ.get("LITCLOCK_UPDATE_STATUS_FILE"))
        status_file.parent.mkdir(parents=True, exist_ok=True)
        if status_file.exists():
            status_file.unlink()
        _os.symlink(target, status_file)
        response = client.get("/api/update/status")
        assert response.status_code == 200
        # Symlink rejected — caller sees stale (lstat existed, S_ISREG false).
        assert response.json["state"] == "stale"


class TestReleaseNotesCap:
    """litclock-dev#342 I1 — build_check_payload caps release_notes before write_cache
    so the cached JSON stays below MAX_GH_API_CACHE_BYTES. Without the
    cap a future verbose CHANGELOG entry would write fine then read-side
    reject as oversize → refetch on every /api/update/check → burns the
    GitHub PAT rate budget."""

    def test_long_release_notes_are_truncated(self):
        from control_server import update_state

        oversized = "\n".join([f"- bullet line {i} " + "x" * 80 for i in range(200)])
        assert len(oversized.encode("utf-8")) > update_state.MAX_RELEASE_NOTES_BYTES
        with (
            patch("control_server.update_state.fetch_latest_release_tag", return_value="v0.210.0"),
            patch("control_server.update_state.fetch_release_notes", return_value=oversized),
        ):
            payload = update_state.build_check_payload("v0.209.0")
        notes = payload["release_notes"]
        assert notes is not None
        assert len(notes.encode("utf-8")) <= update_state.MAX_RELEASE_NOTES_BYTES + 16  # +16 for the "…" tail marker
        # Truncation should mark itself so consumers can show "see CHANGELOG for full notes".
        assert notes.endswith("…")

    def test_short_release_notes_are_unchanged(self):
        from control_server import update_state

        short = "### Fixed\n- one thing\n- another thing"
        with (
            patch("control_server.update_state.fetch_latest_release_tag", return_value="v0.210.0"),
            patch("control_server.update_state.fetch_release_notes", return_value=short),
        ):
            payload = update_state.build_check_payload("v0.209.0")
        assert payload["release_notes"] == short

    def test_capped_payload_stays_under_cache_byte_limit(self):
        """End-to-end: build_check_payload's output, when JSON-serialised
        the way write_cache serialises, must fit under MAX_GH_API_CACHE_BYTES.
        Without the cap on release_notes, an oversized notes section
        would slip through and read-side would reject the cache on every
        read."""
        from control_server import update_state

        oversized = "\n".join([f"- bullet line {i} " + "x" * 80 for i in range(200)])
        with (
            patch("control_server.update_state.fetch_latest_release_tag", return_value="v0.210.0"),
            patch("control_server.update_state.fetch_release_notes", return_value=oversized),
        ):
            payload = update_state.build_check_payload("v0.209.0")
        # Mirror write_cache's serialisation (ensure_ascii=False per the codex
        # adversarial follow-up — ASCII-escaped UTF-8 would inflate non-ASCII
        # chars far past the byte cap).
        serialised = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        assert len(serialised.encode("utf-8")) <= update_state.MAX_GH_API_CACHE_BYTES

    def test_capped_emoji_payload_stays_under_cache_byte_limit(self):
        """litclock-dev#342 I1 follow-up (codex adversarial /review): ASCII-escaping
        non-ASCII content inflates 4 UTF-8 bytes per emoji to ~12 escaped
        bytes. With the default ensure_ascii=True, a 4KB-of-emoji notes
        section serialises to ~12KB and trips the read-side oversize gate.
        write_cache must use ensure_ascii=False; this test pins that."""
        from control_server import update_state

        # 1000 emoji ≈ 4000 UTF-8 bytes (under MAX_RELEASE_NOTES_BYTES=4096).
        emoji_notes = "🎉" * 1000
        assert len(emoji_notes.encode("utf-8")) <= update_state.MAX_RELEASE_NOTES_BYTES
        with (
            patch("control_server.update_state.fetch_latest_release_tag", return_value="v0.210.0"),
            patch("control_server.update_state.fetch_release_notes", return_value=emoji_notes),
        ):
            payload = update_state.build_check_payload("v0.209.0")
        # Serialise as write_cache does — must fit under cache cap.
        serialised = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        assert len(serialised.encode("utf-8")) <= update_state.MAX_GH_API_CACHE_BYTES, (
            "ensure_ascii=False is required — ensure_ascii=True would inflate emoji notes past the cache cap"
        )

    def test_write_cache_round_trips_unicode(self, tmp_path, monkeypatch):
        """litclock-dev#342 I1 follow-up: write_cache → read_cache must preserve non-ASCII
        content. Pins ensure_ascii=False on the writer and the UTF-8 read side."""
        from control_server import update_state

        cache_file = tmp_path / "update-check.json"
        monkeypatch.setenv("LITCLOCK_UPDATE_CHECK_CACHE", str(cache_file))
        payload = {
            "fetched_at_unix": 1700000000,
            "current_version": "v0.210.0",
            "latest_tag": "v0.211.0",
            "available": True,
            "release_notes": "### Fixed\n- 🎉 Unicode notes work\n- naïve geocoding bug",
        }
        assert update_state.write_cache(payload, cache_file=cache_file)
        # Cache file size must stay below the read-side cap.
        assert cache_file.stat().st_size <= update_state.MAX_GH_API_CACHE_BYTES
        re_read = update_state.read_cache(cache_file=cache_file)
        assert re_read is not None
        assert re_read["release_notes"] == payload["release_notes"]


class TestUpdateCheckCacheBoundedReads:
    """litclock-dev#336 — read_cache must apply the same bounded-read gates as
    read_status_file. 1MB junk → return None (refetch). FIFO/symlink →
    return None. The /api/update/check route handler must NOT 500."""

    def test_oversize_cache_refetches_without_500(self, client):
        cache = Path(os.environ.get("LITCLOCK_UPDATE_CHECK_CACHE"))
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text("J" * (1024 * 1024))  # 1MB
        with (
            patch("control_server.update_state.fetch_latest_release_tag", return_value="v0.210.0"),
            patch("control_server.update_state.fetch_release_notes", return_value=None),
        ):
            response = client.get("/api/update/check")
        assert response.status_code == 200
        # Cache was refetched + overwritten with a well-formed payload.
        re_read = json.loads(cache.read_text())
        assert re_read["latest_tag"] == "v0.210.0"

    def test_symlink_cache_is_rejected_and_refetches(self, client, tmp_path):
        import json as _json
        import os as _os

        target = tmp_path / "evil-cache.json"
        target.write_text(
            _json.dumps(
                {
                    "fetched_at_unix": 9999999999,
                    "current_version": "v0.210.0",
                    "latest_tag": "v9.9.9",
                    "available": True,
                }
            )
        )
        cache = Path(os.environ.get("LITCLOCK_UPDATE_CHECK_CACHE"))
        cache.parent.mkdir(parents=True, exist_ok=True)
        if cache.exists():
            cache.unlink()
        _os.symlink(target, cache)
        with (
            patch("control_server.update_state.fetch_latest_release_tag", return_value="v0.210.0"),
            patch("control_server.update_state.fetch_release_notes", return_value=None),
        ):
            response = client.get("/api/update/check")
        assert response.status_code == 200
        # The planted symlink target's "v9.9.9" must NOT have leaked through.
        assert response.json["latest_tag"] == "v0.210.0"


# ───────────────────── busy gate (D5/F7) ───────────────────────────────────


class TestUpdateIsBusy:
    def test_active_state_is_busy(self):
        from unittest.mock import MagicMock

        from control_server import update_state

        with patch("control_server.update_state.subprocess.run") as mock_run:
            # is-active returns "active"
            mock_run.return_value = MagicMock(stdout="active\n")
            assert update_state._is_active_busy() is True

    def test_activating_is_busy(self):
        from unittest.mock import MagicMock

        from control_server import update_state

        with patch("control_server.update_state.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="activating\n")
            assert update_state._is_active_busy() is True

    def test_inactive_is_idle(self):
        from unittest.mock import MagicMock

        from control_server import update_state

        with patch("control_server.update_state.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="inactive\n")
            assert update_state._is_active_busy() is False

    def test_queued_job_counts_as_busy(self):
        from unittest.mock import MagicMock

        from control_server import update_state

        with patch("control_server.update_state.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="123 litclock-update.service start running\n")
            assert update_state._has_queued_job() is True

    def test_no_queued_job_is_idle(self):
        from unittest.mock import MagicMock

        from control_server import update_state

        with patch("control_server.update_state.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="\n")
            assert update_state._has_queued_job() is False

    def test_combined_gate_returns_true_if_either_busy(self):
        from control_server import update_state

        with (
            patch("control_server.update_state._is_active_busy", return_value=False),
            patch("control_server.update_state._has_queued_job", return_value=True),
        ):
            assert update_state.update_is_busy() is True

        with (
            patch("control_server.update_state._is_active_busy", return_value=True),
            patch("control_server.update_state._has_queued_job", return_value=False),
        ):
            assert update_state.update_is_busy() is True

        with (
            patch("control_server.update_state._is_active_busy", return_value=False),
            patch("control_server.update_state._has_queued_job", return_value=False),
        ):
            assert update_state.update_is_busy() is False


# ───────────────────── changelog parser (D13) ──────────────────────────────


class TestChangelogParser:
    def test_extracts_section_for_tag(self):
        from control_server.update_state import _extract_changelog_section

        body = (
            "# Changelog\n"
            "## [Unreleased]\n"
            "- nothing\n"
            "## v0.211.0\n"
            "### Added\n"
            "- M5 shipped\n"
            "- another bullet\n"
            "## v0.210.0\n"
            "- earlier release\n"
        )
        notes = _extract_changelog_section(body, "v0.211.0")
        assert notes is not None
        assert "M5 shipped" in notes
        assert "earlier release" not in notes
        assert "## v0.210.0" not in notes

    def test_returns_none_for_unknown_tag(self):
        from control_server.update_state import _extract_changelog_section

        body = "# Changelog\n## v0.210.0\n- nothing\n"
        assert _extract_changelog_section(body, "v0.999.0") is None

    def test_handles_bracketed_heading_with_date(self):
        from control_server.update_state import _extract_changelog_section

        body = "## [v0.211.0] - 2026-04-30\n- bullet\n"
        notes = _extract_changelog_section(body, "v0.211.0")
        assert notes is not None
        assert "bullet" in notes


# ───────────────────── GH auth (private-repo support) ──────────────────────


class TestGhAuth:
    """Verify update_state's GH auth helper finds tokens via the same
    resolution order as scripts/lib/github_api.sh: GH_TOKEN env, then
    GITHUB_TOKEN env, then ~/.git-credentials. Hardware QA on test Pi
    2026-04-30 caught the missing-auth case — /api/update/check returned
    available=null forever because /tags 404'd without auth on a private
    repo, leaving the PWA stuck on the 'checking…' pill.
    """

    def test_gh_token_env_takes_priority(self, tmp_path, monkeypatch):
        from control_server.update_state import _gh_auth_header

        # Even with a credentials file present, GH_TOKEN env wins.
        creds = tmp_path / ".git-credentials"
        creds.write_text("https://user:from-file@github.com\n")
        monkeypatch.setenv("LITCLOCK_GIT_CREDENTIALS", str(creds))
        monkeypatch.setenv("GH_TOKEN", "from-env")
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        assert _gh_auth_header() == {"Authorization": "Bearer from-env"}

    def test_github_token_env_used_when_no_gh_token(self, tmp_path, monkeypatch):
        from control_server.update_state import _gh_auth_header

        creds = tmp_path / ".git-credentials"
        creds.write_text("https://user:from-file@github.com\n")
        monkeypatch.setenv("LITCLOCK_GIT_CREDENTIALS", str(creds))
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "from-actions")
        assert _gh_auth_header() == {"Authorization": "Bearer from-actions"}

    def test_falls_back_to_git_credentials(self, tmp_path, monkeypatch):
        from control_server.update_state import _gh_auth_header

        creds = tmp_path / ".git-credentials"
        creds.write_text("https://other:irrelevant@gitlab.com\nhttps://user:abc123@github.com\n")
        monkeypatch.setenv("LITCLOCK_GIT_CREDENTIALS", str(creds))
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        assert _gh_auth_header() == {"Authorization": "Bearer abc123"}

    def test_returns_empty_when_no_token_anywhere(self, tmp_path, monkeypatch):
        from control_server.update_state import _gh_auth_header

        monkeypatch.setenv("LITCLOCK_GIT_CREDENTIALS", str(tmp_path / "missing"))
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        # Empty dict so callers can `.update()` it unconditionally —
        # public-repo path stays unauthenticated.
        assert _gh_auth_header() == {}

    def test_credentials_file_unreadable_returns_empty(self, tmp_path, monkeypatch):
        from control_server.update_state import _gh_token_from_credentials

        # Path that exists but isn't a regular file (a directory).
        not_a_file = tmp_path / "notafile"
        not_a_file.mkdir()
        monkeypatch.setenv("LITCLOCK_GIT_CREDENTIALS", str(not_a_file))
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        assert _gh_token_from_credentials() is None

    def test_skips_non_github_lines(self, tmp_path, monkeypatch):
        from control_server.update_state import _gh_token_from_credentials

        creds = tmp_path / ".git-credentials"
        creds.write_text(
            "https://user:gitlab-token@gitlab.com\nhttps://user:bitbucket-token@bitbucket.org\n"
            # No github.com line at all
        )
        monkeypatch.setenv("LITCLOCK_GIT_CREDENTIALS", str(creds))
        assert _gh_token_from_credentials() is None


class TestCacheOnTmpfs:
    """litclock-dev#434 — the update-check GH-API cache is a purely derived 6h-TTL blob,
    so it lives on the /run/litclock tmpfs (kept off the SD card), not the
    persistent /var/lib/litclock state dir."""

    def test_default_cache_file_is_on_tmpfs(self):
        from control_server import update_state

        assert str(update_state.DEFAULT_CACHE_FILE).startswith("/run/litclock/"), (
            f"update-check cache must live on tmpfs, got {update_state.DEFAULT_CACHE_FILE}"
        )

    def test_cache_path_resolves_default_when_unset(self, monkeypatch):
        from control_server import update_state

        monkeypatch.delenv("LITCLOCK_UPDATE_CHECK_CACHE", raising=False)
        assert update_state.cache_path() == update_state.DEFAULT_CACHE_FILE

    def test_cache_path_env_override_still_wins(self, tmp_path, monkeypatch):
        from control_server import update_state

        override = tmp_path / "update-check.json"
        monkeypatch.setenv("LITCLOCK_UPDATE_CHECK_CACHE", str(override))
        assert update_state.cache_path() == override

    def test_write_then_read_round_trip(self, tmp_path, monkeypatch):
        """The relocation doesn't break the write->read contract: an atomic
        write lands a readable cache and read_cache returns it."""
        from control_server import update_state

        override = tmp_path / "update-check.json"
        monkeypatch.setenv("LITCLOCK_UPDATE_CHECK_CACHE", str(override))
        payload = {"tag": "v0.215.0", "fetched_at_unix": 123.0, "release_notes": "hi"}

        assert update_state.write_cache(payload) is True
        assert override.exists()
        assert update_state.read_cache() == payload

    def test_write_cache_creates_missing_parent_dir(self, tmp_path, monkeypatch):
        """The tmpfs default relies on write_cache creating its parent dir when
        absent (e.g. an early-boot write before systemd-tmpfiles has created
        /run/litclock). Pin that branch — the round-trip test above uses an
        already-existing tmp_path and never exercises it (litclock-dev#434 review)."""
        from control_server import update_state

        nested = tmp_path / "run" / "litclock" / "update-check.json"
        assert not nested.parent.exists()
        monkeypatch.setenv("LITCLOCK_UPDATE_CHECK_CACHE", str(nested))

        assert update_state.write_cache({"tag": "v1", "fetched_at_unix": 1.0}) is True
        assert nested.exists()

    def test_update_sh_default_path_in_lockstep_with_python(self):
        """Lockstep guard (litclock-dev#434 review): scripts/update.sh must reference the
        SAME default cache path as update_state.DEFAULT_CACHE_FILE. If the two
        diverge, post-update the PWA reads one path while update.sh invalidates
        another → spurious 'update available' banner for up to 6h. Mirrors the
        MEMORY.md 'filter at one consumer leaks via siblings' lesson applied to
        these two path consumers."""
        from pathlib import Path

        from control_server import update_state

        update_sh = Path(__file__).resolve().parents[1] / "scripts" / "update.sh"
        text = update_sh.read_text()
        default = str(update_state.DEFAULT_CACHE_FILE)
        assert default in text, (
            f"scripts/update.sh must reference {default!r} (update_state.DEFAULT_CACHE_FILE); "
            "the reader and the invalidator must never disagree on the cache path"
        )


# ───────────────────── litclock-dev#607 — /updates in-progress render ──────────────────


class TestUpdatesTabInProgressRender:
    """litclock-dev#607 — a page load during a live run must serve the reading list,
    not the Apply card. The card invites a double-tap at the exact moment
    one is most tempting (the updater just restarted litclock-control and
    the PWA reloaded); the reading list is the truthful surface and works
    for no-JS users as a static snapshot."""

    def _write_status(self, payload: dict) -> None:
        status_file = Path(os.environ.get("LITCLOCK_UPDATE_STATUS_FILE"))
        status_file.parent.mkdir(parents=True, exist_ok=True)
        status_file.write_text(json.dumps(payload))

    @staticmethod
    def _card_hidden(page: str) -> bool:
        match = re.search(r'<section id="updates-card"[^>]*>', page)
        assert match, "updates-card section missing"
        return "hidden" in match.group(0)

    @staticmethod
    def _reading_list_hidden(page: str) -> bool:
        match = re.search(r'<section class="phase-reading-list"[^>]*>', page)
        assert match, "phase-reading-list section missing"
        return "hidden" in match.group(0)

    @staticmethod
    def _row_state(page: str, idx: int) -> str:
        match = re.search(rf'data-phase-index="{idx}" data-state="([a-z]+)"', page)
        assert match, f"phase row {idx} missing"
        return match.group(1)

    def test_running_status_renders_reading_list_open_with_snapshot(self, client):
        self._write_status({"state": "running", "phase_index": 4})
        with patch("control_server.routes.updates.update_state.update_is_busy", return_value=True):
            page = client.get("/updates").data.decode()
        assert self._card_hidden(page)
        assert not self._reading_list_hidden(page)
        assert self._row_state(page, 3) == "completed"
        assert self._row_state(page, 4) == "active"
        assert self._row_state(page, 5) == "upcoming"
        # litclock-dev#607 — the no-JS live-ness fallback rides the running render only.
        assert '<noscript><meta http-equiv="refresh"' in page

    def test_running_file_with_dead_unit_renders_the_card(self, client):
        """litclock-dev#607 review F2 — a SIGKILL/OOM'd updater skips the EXIT trap and
        leaves state=running on tmpfs until reboot. The unit is the
        authority in BOTH directions: a running file with an idle unit must
        not strand every page load on a frozen in-progress view (with
        refreshCheck suppressed, that lie would be permanent)."""
        self._write_status({"state": "running", "phase_index": 4})
        page = client.get("/updates").data.decode()  # fixture: busy=False
        assert not self._card_hidden(page)
        assert self._reading_list_hidden(page)

    def test_stale_file_with_busy_unit_renders_the_card(self, client):
        """Stale is inferred in NEITHER surface — mirror of the status-route
        pin, on the page render's own copy of the logic."""
        status_file = Path(os.environ.get("LITCLOCK_UPDATE_STATUS_FILE"))
        status_file.parent.mkdir(parents=True, exist_ok=True)
        status_file.write_text("{ corrupt")
        with patch("control_server.routes.updates.update_state.update_is_busy", return_value=True):
            page = client.get("/updates").data.decode()
        assert not self._card_hidden(page)
        assert self._reading_list_hidden(page)

    def test_idle_file_but_busy_unit_renders_reading_list_at_phase_1(self, client):
        """The dispatch window: unit active/queued, Phase 1 not yet written."""
        with patch("control_server.routes.updates.update_state.update_is_busy", return_value=True):
            page = client.get("/updates").data.decode()
        assert self._card_hidden(page)
        assert not self._reading_list_hidden(page)
        assert self._row_state(page, 1) == "active"
        assert self._row_state(page, 2) == "upcoming"

    def test_idle_and_idle_unit_renders_the_card(self, client):
        page = client.get("/updates").data.decode()
        assert not self._card_hidden(page)
        assert self._reading_list_hidden(page)
        for idx in range(1, 8):
            assert self._row_state(page, idx) == "upcoming"

    def test_terminal_status_renders_the_card_not_the_snapshot(self, client):
        """complete/failed_* on a page load is the PWA probe's job to
        surface (it renders the terminal copy); the server keeps the card
        so the no-JS user isn't stranded on a dead-end reading list.
        Pinned with the unit BUSY too: the updater's tail legitimately has
        a terminal file + a still-active unit, and the page must not bounce
        a finished update back to the reading list (review item 6)."""
        for busy in (False, True):
            self._write_status({"state": "complete", "phase_index": 7})
            with patch("control_server.routes.updates.update_state.update_is_busy", return_value=busy):
                from control_server.routes import updates as updates_routes  # noqa: PLC0415

                updates_routes._reset_busy_memo()
                page = client.get("/updates").data.decode()
            assert not self._card_hidden(page), f"busy={busy}"
            assert self._reading_list_hidden(page), f"busy={busy}"

    def test_boolean_phase_index_renders_sanely(self, client):
        """A corrupted boolean phase_index must not 500 or garble the
        render. NOTE (review): this cannot distinguish the litclock-dev#372 bool guard
        from its absence — `1 <= True <= 7` also yields a row-1-active
        render, so the guard is unobservable through this surface. The test
        pins doesn't-crash + sane output only; the guard itself is
        documentation-grade defense."""
        self._write_status({"state": "running", "phase_index": True})
        with patch("control_server.routes.updates.update_state.update_is_busy", return_value=True):
            page = client.get("/updates").data.decode()
        assert not self._reading_list_hidden(page)
        assert self._row_state(page, 1) == "active"

    def test_out_of_range_phase_index_falls_back_to_phase_1(self, client):
        self._write_status({"state": "running", "phase_index": 99})
        with patch("control_server.routes.updates.update_state.update_is_busy", return_value=True):
            page = client.get("/updates").data.decode()
        assert not self._reading_list_hidden(page)
        assert self._row_state(page, 1) == "active"
        assert self._row_state(page, 7) == "upcoming"

    def test_apply_dispatch_seeds_the_status_file(self, client):
        """litclock-dev#607 review F1 — a successful dispatch immediately overwrites the
        PREVIOUS run's terminal verdict, so a poll in the systemd-activation
        window can never read stale `complete` and reload onto the Apply
        card mid-update (second Apply in the same boot)."""
        self._write_status({"state": "complete", "phase_index": 7})
        cache = Path(os.environ.get("LITCLOCK_UPDATE_CHECK_CACHE"))
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps(
                {
                    "fetched_at_unix": 9999999999,
                    "current_version": "v0.210.0",
                    "latest_tag": "v0.211.0",
                    "available": True,
                }
            )
        )
        token = _scrape_apply_token(client)
        with (
            patch("control_server.routes.updates.update_state.update_is_busy", return_value=False),
            patch("control_server.routes.updates.subprocess.run"),
        ):
            response = client.post("/api/update/apply", json={"token": token})
        assert response.status_code == 202, response.json
        status_file = Path(os.environ.get("LITCLOCK_UPDATE_STATUS_FILE"))
        seeded = json.loads(status_file.read_text())
        assert seeded["state"] == "running"
        assert seeded["phase_index"] == 1
        assert seeded["inferred"] == "dispatch"

    def test_failed_dispatch_does_not_seed(self, client):
        """A failed systemctl start must NOT plant a running file that
        nothing will ever update — the previous verdict stays."""
        self._write_status({"state": "complete", "phase_index": 7})
        cache = Path(os.environ.get("LITCLOCK_UPDATE_CHECK_CACHE"))
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps(
                {
                    "fetched_at_unix": 9999999999,
                    "current_version": "v0.210.0",
                    "latest_tag": "v0.211.0",
                    "available": True,
                }
            )
        )
        token = _scrape_apply_token(client)
        import subprocess as _subprocess  # noqa: PLC0415

        with (
            patch("control_server.routes.updates.update_state.update_is_busy", return_value=False),
            patch(
                "control_server.routes.updates.subprocess.run",
                side_effect=_subprocess.CalledProcessError(1, ["systemctl"], stderr=b"boom"),
            ),
        ):
            response = client.post("/api/update/apply", json={"token": token})
        assert response.status_code == 500
        status_file = Path(os.environ.get("LITCLOCK_UPDATE_STATUS_FILE"))
        assert json.loads(status_file.read_text())["state"] == "complete"
