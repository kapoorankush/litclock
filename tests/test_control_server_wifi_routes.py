"""Tests for /api/wifi/reset (#245 M5 D11/D12).

Covers:
- Token + rate-limit gating (mirrors M4 patterns)
- Update-busy gate (D5/F7) returns 409 update_in_progress
- Happy path dispatches `sudo systemctl start --no-block litclock-wifi-reset.service`
  (D11 — service unit owns the actual nmcli + .setup-complete + firstboot work)
- 200 response includes the locked handoff copy ("Switching to setup mode...")
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from control_server import create_app  # noqa: E402


@pytest.fixture
def app():
    app = create_app(test_config={"VERSION_OVERRIDE": "v0.210.0"})
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def _mint_token(client):
    store = client.application.extensions["confirm_tokens"]
    return store.issue("wifi_reset")[0]


class TestApiWifiReset:
    def test_happy_path_dispatches_service_unit(self, client):
        token = _mint_token(client)
        with (
            patch("control_server.routes.wifi.update_state.update_is_busy", return_value=False),
            patch("control_server.routes.wifi.subprocess.run") as mock_run,
        ):
            response = client.post("/api/wifi/reset", json={"token": token})
        assert response.status_code == 200, response.json
        body = response.json
        assert body["ok"] is True
        # Locked DESIGN.md handoff copy elements.
        assert "Switching to setup mode" in body["message"]
        assert "LitClock-Setup" in body["message"]
        # Dispatched via sudo systemctl start --no-block litclock-wifi-reset.service
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "sudo"
        assert args[-3:] == ["start", "--no-block", "litclock-wifi-reset.service"]

    def test_busy_returns_409_update_in_progress(self, client):
        token = _mint_token(client)
        with (
            patch("control_server.routes.wifi.update_state.update_is_busy", return_value=True),
            patch("control_server.routes.wifi.subprocess.run") as mock_run,
        ):
            response = client.post("/api/wifi/reset", json={"token": token})
        assert response.status_code == 409
        body = response.json
        assert body["ok"] is False
        assert body["error"]["code"] == "update_in_progress"
        mock_run.assert_not_called()

    def test_missing_token_returns_401(self, client):
        response = client.post("/api/wifi/reset", json={})
        assert response.status_code == 401
        body = response.json
        assert body["error"]["code"] == "confirm_token_invalid"

    def test_replayed_token_returns_409_consumed(self, client):
        """#317 item 1 codex P2: replay of a consumed wifi_reset token
        returns 409 ``confirm_token_consumed`` (was 401
        ``confirm_token_invalid``). The distinct slug protects against
        the JS refresh-and-retry path bypassing single-use on duplicate
        submits."""
        token = _mint_token(client)
        with (
            patch("control_server.routes.wifi.update_state.update_is_busy", return_value=False),
            patch("control_server.routes.wifi.subprocess.run"),
        ):
            first = client.post("/api/wifi/reset", json={"token": token})
        assert first.status_code == 200
        second = client.post("/api/wifi/reset", json={"token": token})
        assert second.status_code == 409
        assert second.json["error"]["code"] == "confirm_token_consumed"

    def test_systemctl_failure_returns_500_envelope(self, client):
        import subprocess

        token = _mint_token(client)
        with (
            patch("control_server.routes.wifi.update_state.update_is_busy", return_value=False),
            patch(
                "control_server.routes.wifi.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "systemctl", stderr=b"unit not found"),
            ),
        ):
            response = client.post("/api/wifi/reset", json={"token": token})
        assert response.status_code == 500
        assert response.json["error"]["code"] == "wifi_reset_failed"

    def test_form_post_accepts_form_encoded_token(self, client):
        # Mirrors M4's no-JS form fallback — captive-portal WebViews lacking JS
        # still post form-urlencoded.
        token = _mint_token(client)
        with (
            patch("control_server.routes.wifi.update_state.update_is_busy", return_value=False),
            patch("control_server.routes.wifi.subprocess.run"),
        ):
            response = client.post(
                "/api/wifi/reset",
                data={"token": token},
                content_type="application/x-www-form-urlencoded",
            )
        assert response.status_code == 200

    def test_rate_limit_kicks_in_after_burst(self, client):
        # Shared 5/min bucket with reboot/poweroff. After 5 rapid POSTs from
        # the same IP, the 6th returns 429 even with a fresh token.
        with (
            patch("control_server.routes.wifi.update_state.update_is_busy", return_value=False),
            patch("control_server.routes.wifi.subprocess.run"),
        ):
            for _ in range(5):
                token = _mint_token(client)
                client.post("/api/wifi/reset", json={"token": token})
            token = _mint_token(client)
            response = client.post("/api/wifi/reset", json={"token": token})
        assert response.status_code == 429
        body = response.json
        assert body["error"]["code"] == "rate_limited"
        assert "retry_after_s" in body["error"]

    # ─── #328 — restore-on-failure regressions ──────────────────────────────

    def test_called_process_error_restores_token_for_retry(self, client):
        """#328 (the live M8-hardware-QA #327 scenario): the
        litclock-wifi-reset.service unit was missing on fresh pi-gen
        images. Tapping Reset WiFi got "systemctl failed" — pre-fix
        the user's retry then hit "Confirm token is missing, expired,
        or already used", which masked the real error and made the
        underlying #327 fix harder to spot.

        Post-fix: token restored on CalledProcessError, retry surfaces
        the real systemctl error again."""
        import subprocess

        token = _mint_token(client)
        with (
            patch("control_server.routes.wifi.update_state.update_is_busy", return_value=False),
            patch(
                "control_server.routes.wifi.subprocess.run",
                side_effect=subprocess.CalledProcessError(
                    1, "systemctl", stderr=b"Unit litclock-wifi-reset.service not found."
                ),
            ),
        ):
            first = client.post("/api/wifi/reset", json={"token": token})
        assert first.status_code == 500
        assert first.json["error"]["code"] == "wifi_reset_failed"

        # Retry with same token — token restored, subprocess invoked again
        # (and now succeeds, simulating the operator unmasking the unit).
        with (
            patch("control_server.routes.wifi.update_state.update_is_busy", return_value=False),
            patch("control_server.routes.wifi.subprocess.run") as mock_run,
        ):
            second = client.post("/api/wifi/reset", json={"token": token})
        assert second.status_code == 200, second.json
        mock_run.assert_called_once()

    def test_update_busy_restores_token_for_retry(self, client):
        """#328: a 409 update_in_progress is pre-side-effect — no systemctl
        dispatched. Restoring lets the user retry after the update
        completes without re-opening the page."""
        token = _mint_token(client)
        with (
            patch("control_server.routes.wifi.update_state.update_is_busy", return_value=True),
            patch("control_server.routes.wifi.subprocess.run") as mock_run,
        ):
            first = client.post("/api/wifi/reset", json={"token": token})
        assert first.status_code == 409
        mock_run.assert_not_called()

        # Same token, gate now clear — must dispatch.
        with (
            patch("control_server.routes.wifi.update_state.update_is_busy", return_value=False),
            patch("control_server.routes.wifi.subprocess.run") as mock_run,
        ):
            second = client.post("/api/wifi/reset", json={"token": token})
        assert second.status_code == 200, second.json
        mock_run.assert_called_once()

    def test_called_process_error_handler_documents_double_fire_caveat(self):
        """Review D1: the CalledProcessError restore branch must carry the
        residual-dispatch-window warning so a future engineer adding a
        non-idempotent destructive action can't silently strip it.

        Pin both 'double-fire' and 'non-idempotent' as load-bearing phrases
        inside the wifi route source so a refactor that removes the caveat
        block trips this test."""
        wifi_src = (Path(__file__).resolve().parents[1] / "src" / "control_server" / "routes" / "wifi.py").read_text()
        assert "double-fire" in wifi_src, (
            "routes/wifi.py must document the double-fire caveat near the CalledProcessError restore (review D1)"
        )
        assert "non-idempotent" in wifi_src, (
            "routes/wifi.py must spell out the non-idempotent action escape hatch "
            "near the CalledProcessError restore (review D1)"
        )
