"""Tests for the project-wide JSON error envelope (issue #254).

Pins the locked decisions:

- ``ok: bool`` is the project-wide success/error signal — every endpoint
  that returns an envelope sets it (true on 2xx, false on non-2xx).
- ``/api/*`` paths return JSON envelopes for HTTPException + uncaught
  exceptions; non-API paths keep Flask's default HTML error pages.
- The envelope shape is ``{"ok": false, "error": {"code", "message",
  [extras]}}`` — extras like ``retry_after_s`` slot in alongside ``code``
  and ``message`` rather than at the top level.

Anti-regression tests: M2-M5 land more endpoints over time. If any future
edit drops ``ok`` from a 2xx response or hand-rolls a non-canonical error
shape, these tests catch it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from flask import Blueprint, abort

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from control_server import create_app  # noqa: E402
from control_server.errors import envelope  # noqa: E402


@pytest.fixture
def app():
    return create_app({"VERSION_OVERRIDE": "v0.test"})


@pytest.fixture
def client(app):
    return app.test_client()


# ---------- envelope() helper shape ----------


class TestEnvelopeHelper:
    def test_minimal_shape(self, app) -> None:
        with app.test_request_context():
            response, status = envelope("bad_request", "no good", 400)
        assert status == 400
        body = response.get_json()
        assert body == {"ok": False, "error": {"code": "bad_request", "message": "no good"}}

    def test_extras_nest_inside_error_object(self, app) -> None:
        """``retry_after_s`` (and future extras like ``fields``) live INSIDE
        the ``error`` object, not at the top level. Pin the full nested object
        so a refactor that accidentally flattens, drops ``code``/``message``,
        or shifts the layout breaks the test loudly.
        """
        with app.test_request_context():
            response, _ = envelope("rate_limited", "slow down", 429, retry_after_s=12)
        body = response.get_json()
        assert body == {
            "ok": False,
            "error": {"code": "rate_limited", "message": "slow down", "retry_after_s": 12},
        }

    def test_status_passed_through(self, app) -> None:
        with app.test_request_context():
            for status in (400, 401, 403, 404, 409, 429, 500):
                response, returned = envelope("x", "y", status)
                assert returned == status
                assert response.get_json() == {"ok": False, "error": {"code": "x", "message": "y"}}


# ---------- HTTPException → envelope on /api/* ----------


class TestHttpExceptionEnvelope:
    """Werkzeug raises HTTPException for unknown paths, wrong methods,
    and ``abort(...)`` calls. The global handler converts them to JSON
    on /api/* so M2-M5's POST routes don't have to hand-roll error
    responses for every malformed request."""

    def test_unknown_api_path_returns_json_envelope(self, client) -> None:
        response = client.get("/api/this/does/not/exist")
        assert response.status_code == 404
        assert response.is_json
        body = response.get_json()
        assert body["ok"] is False
        assert body["error"]["code"] == "not_found"
        assert isinstance(body["error"]["message"], str)
        assert body["error"]["message"]  # non-empty

    def test_wrong_method_on_api_path_returns_json_envelope(self, client) -> None:
        # /api/health is GET-only; POSTing should land in 405.
        response = client.post("/api/health")
        assert response.status_code == 405
        assert response.is_json
        body = response.get_json()
        assert body["ok"] is False
        assert body["error"]["code"] == "method_not_allowed"

    def test_unknown_html_path_keeps_default_html_error(self, client) -> None:
        """The PWA shell's HTML pages don't consume JSON. A 404 in the
        browser address bar should still render Flask's HTML error page,
        not bleed JSON into a <pre> tag."""
        response = client.get("/this/does/not/exist")
        assert response.status_code == 404
        assert response.mimetype == "text/html"

    def test_abort_with_description_passes_through(self, app) -> None:
        """``abort(400, description=...)`` lets a route hand the global
        handler a custom message. Pin so the description doesn't get
        replaced by the default."""
        bp = Blueprint("test_abort", __name__)

        @bp.route("/api/test/abort")
        def trigger():  # pragma: no cover - registered for the test only
            abort(400, description="missing field: city")

        app.register_blueprint(bp)
        response = app.test_client().get("/api/test/abort")
        assert response.status_code == 400
        body = response.get_json()
        assert body["error"]["code"] == "bad_request"
        assert body["error"]["message"] == "missing field: city"

    @pytest.mark.parametrize(
        "status,expected_code",
        [
            (400, "bad_request"),
            (401, "unauthorized"),
            (403, "forbidden"),
            (404, "not_found"),
            (405, "method_not_allowed"),
            (409, "conflict"),
            (413, "payload_too_large"),
            (415, "unsupported_media_type"),
            (429, "rate_limited"),
            (500, "server_error"),
        ],
    )
    def test_every_default_status_round_trips(self, app, status: int, expected_code: str) -> None:
        """Every entry in ``_DEFAULTS`` must round-trip through ``abort(status)``
        with the locked slug. Pin so a future edit can't drop, rename, or
        re-order entries silently."""
        bp = Blueprint(f"test_abort_{status}", __name__)

        @bp.route(f"/api/test/abort-{status}")
        def trigger():  # pragma: no cover - registered for the test only
            abort(status)

        app.register_blueprint(bp)
        response = app.test_client().get(f"/api/test/abort-{status}")
        assert response.status_code == status
        body = response.get_json()
        assert body["ok"] is False
        assert body["error"]["code"] == expected_code
        # Curated default message must be non-empty (specific text is the
        # fallback path's contract; pin existence here, not literal string).
        assert isinstance(body["error"]["message"], str)
        assert body["error"]["message"]

    def test_unmapped_status_derives_slug_from_exc_name(self, app) -> None:
        """Statuses outside ``_DEFAULTS`` derive their slug from
        ``exc.name``. Pinning the M3-relevant case: 422 Unprocessable Entity
        (the canonical validation status) returns ``code: unprocessable_entity``,
        not the generic ``server_error`` it would land on with a curated-only
        table. Same logic gives ``service_unavailable`` for 503, etc."""
        bp = Blueprint("test_abort_422", __name__)

        @bp.route("/api/test/abort-422")
        def trigger():  # pragma: no cover - registered for the test only
            abort(422)

        app.register_blueprint(bp)
        response = app.test_client().get("/api/test/abort-422")
        assert response.status_code == 422
        body = response.get_json()
        assert body["error"]["code"] == "unprocessable_entity"
        # Default message for unmapped statuses is the exc.name (one-line
        # summary), not the verbose Werkzeug description that would otherwise
        # leak into the response.
        assert body["error"]["message"]
        assert "Unprocessable Entity" in body["error"]["message"] or body["error"]["message"]

    def test_bare_abort_uses_curated_default_not_werkzeug_verbose(self, app) -> None:
        """`abort(401)` with no description must return the curated
        ``_DEFAULTS[401][1]`` ('Authentication required.'), NOT Werkzeug's
        verbose internal class-level description ('The server could not
        verify that you are authorized... You either supplied the wrong
        credentials...'). The verbose text is Werkzeug-version-coupled and
        not human-PWA-friendly."""
        bp = Blueprint("test_abort_bare_401", __name__)

        @bp.route("/api/test/abort-bare-401")
        def trigger():  # pragma: no cover - registered for the test only
            abort(401)

        app.register_blueprint(bp)
        response = app.test_client().get("/api/test/abort-bare-401")
        assert response.status_code == 401
        body = response.get_json()
        assert body["error"]["code"] == "unauthorized"
        assert body["error"]["message"] == "Authentication required."
        # Anti-regression: the Werkzeug verbose description must not bleed
        # into the response.
        assert "supplied the wrong credentials" not in body["error"]["message"]


# ---------- Uncaught exceptions on /api/* ----------


class TestUncaughtExceptionEnvelope:
    """An /api/* route that raises an unexpected exception must land as
    a generic 500 envelope — never as Flask's default HTML stack trace
    (which would leak host paths + Python tracebacks to whoever pokes
    the LAN). Stack traces go to the logger, not the response body."""

    def test_uncaught_exception_returns_500_envelope(self, app) -> None:
        bp = Blueprint("test_boom", __name__)

        @bp.route("/api/test/boom")
        def boom():  # pragma: no cover - registered for the test only
            raise RuntimeError("internal-detail-do-not-leak")

        app.register_blueprint(bp)
        response = app.test_client().get("/api/test/boom")
        assert response.status_code == 500
        assert response.is_json
        body = response.get_json()
        assert body["ok"] is False
        assert body["error"]["code"] == "server_error"
        # The exception message must NOT bleed into the response — it
        # could carry host paths, secrets, or stack data.
        assert "internal-detail-do-not-leak" not in response.get_data(as_text=True)


# ---------- ok=True invariance for /api/health and /api/status ----------
# The issue text (#254) recommends pinning a test asserting `ok` stays
# True under all current health code paths. /api/status went canonical
# in M2 with the same posture; cover both.


class TestOkTrueInvariance:
    def test_health_ok_true_with_version_override(self, client) -> None:
        body = client.get("/api/health").get_json()
        assert body["ok"] is True

    def test_health_has_app_identity_marker_and_cors_header(self, client) -> None:
        """#487: /api/health carries an ``app`` identity marker and an
        ``Access-Control-Allow-Origin`` header so the cross-origin mDNS bookmark
        probe (status.js) can READ the body and confirm it is talking to a real
        LitClock — not some other ``.local`` device answering on :8443."""
        resp = client.get("/api/health")
        assert resp.get_json()["app"] == "litclock"
        assert resp.headers.get("Access-Control-Allow-Origin") == "*"

    def test_health_ok_true_without_version_override(self) -> None:
        from control_server.version import reset_cache

        reset_cache()
        app = create_app({"VERSION_OVERRIDE": None})
        body = app.test_client().get("/api/health").get_json()
        assert body["ok"] is True

    def test_health_ok_true_when_git_describe_fails(self, monkeypatch) -> None:
        """Force the git-describe path to fail and confirm /api/health still
        returns ok=True (version falls back to '.images-version' or 'unknown').
        ``get_version`` is exception-safe by contract; this pins it."""
        import subprocess as _sp

        from control_server.version import reset_cache

        reset_cache()

        def fail_run(*args, **kwargs):
            raise FileNotFoundError("no git here")

        monkeypatch.setattr(_sp, "run", fail_run)
        # Disable override so the fallback chain runs.
        app = create_app({"VERSION_OVERRIDE": None})
        body = app.test_client().get("/api/health").get_json()
        assert body["ok"] is True
        assert isinstance(body["version"], str) and body["version"]

    def test_status_ok_true_when_status_file_missing(self, tmp_path) -> None:
        """/api/status returns ok=True even when the producer-side status
        file is missing — ``stale: true`` carries the real signal. Pinning
        the contract so a future edit can't flip ok=false on stale and
        confuse PWA error UI for a clock-paused signal."""
        app = create_app({"VERSION_OVERRIDE": "v0.test", "STATUS_FILE": str(tmp_path / "no-such-file.json")})
        body = app.test_client().get("/api/status").get_json()
        assert body["ok"] is True
        assert body["stale"] is True


# ---------- Cache-Control on /api/* (PWA reconnect-probe safety) ----------


class TestApiCacheControl:
    """PWA polls /api/health every 3s while reconnecting. Without
    ``Cache-Control: no-store`` an intermediate cache (browser HTTP cache,
    future cloud relay, M6 service worker) could serve a stale 500 envelope
    from a mid-update window as 'last known good' — defeating the
    version-mismatch reload trigger. Pin the header on every /api/* path."""

    def test_api_health_carries_no_store(self, client) -> None:
        response = client.get("/api/health")
        assert response.headers.get("Cache-Control") == "no-store"

    def test_api_status_carries_no_store(self, tmp_path) -> None:
        app = create_app({"VERSION_OVERRIDE": "v0.test", "STATUS_FILE": str(tmp_path / "no.json")})
        response = app.test_client().get("/api/status")
        assert response.headers.get("Cache-Control") == "no-store"

    def test_api_404_envelope_carries_no_store(self, client) -> None:
        response = client.get("/api/this/does/not/exist")
        assert response.status_code == 404
        assert response.headers.get("Cache-Control") == "no-store"

    def test_html_shell_does_not_force_no_store(self, client) -> None:
        """Non-API routes (the PWA shell HTML) keep Flask's default cache
        semantics. The shell is fingerprinted by version; M6's service
        worker handles cache invalidation. Forcing no-store on HTML pages
        would defeat that strategy."""
        response = client.get("/")
        assert response.headers.get("Cache-Control") != "no-store"


# ---------- Security hardening headers (SAST/DAST 2026-07, F1) ----------


class TestSecurityHeaders:
    """Every control_server response must carry nosniff + X-Frame-Options DENY.
    The PWA is unauthenticated on the LAN, so DENY closes a clickjacking vector
    (a malicious same-LAN page iframing the PWA to trigger a state-changing
    control). nosniff is universal defense-in-depth. Applies to API + HTML +
    error responses alike."""

    def test_api_route_has_hardening_headers(self, client) -> None:
        response = client.get("/api/health")
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "DENY"

    def test_html_shell_has_hardening_headers(self, client) -> None:
        response = client.get("/")
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "DENY"

    def test_error_response_still_has_hardening_headers(self, client) -> None:
        # A 404 must not skip the after_request hook — hardening is not
        # conditional on a 2xx.
        response = client.get("/api/this/does/not/exist")
        assert response.status_code == 404
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "DENY"


# ---------- URL versioning lock (PLAN A10 decision 2) ----------


class TestUrlVersioningLock:
    """PLAN A10 decision 2 locked unversioned URLs (``/api/health``, no
    ``/api/v1/``). A future contributor unaware of the lock could prefix a
    blueprint with ``url_prefix='/api/v1'`` and the contract would silently
    drift. The url_map test below catches that at CI time."""

    def test_no_versioned_api_paths_in_url_map(self, app) -> None:
        import re

        versioned = re.compile(r"^/api/v\d+/")
        offenders = [rule.rule for rule in app.url_map.iter_rules() if versioned.match(rule.rule)]
        assert offenders == [], (
            f"PLAN A10 decision 2 locks unversioned /api/* URLs — these rules violate it: {offenders}"
        )
