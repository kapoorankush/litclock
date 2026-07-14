"""Project-wide JSON error envelope (issue #254).

Locked decisions for the control_server API surface, made before M3 lands
``/api/settings/*``:

1. **Every response carries ``ok: bool``.** ``true`` for 2xx, ``false``
   for non-2xx. M2-M5 endpoints inherit this convention; PWA can branch
   on a single field instead of memorising per-endpoint shapes.
2. **URLs stay unversioned.** ``/api/health``, ``/api/status``, etc. The
   appliance ships PWA shell + server in lockstep (update.sh restarts
   both), so URL versioning adds nothing. The cloud relay (PRD v2) is a
   separate transport boundary; it'll introduce its own versioning when
   it lands.
3. **Error envelope shape:**

       {"ok": false, "error": {"code": "<slug>", "message": "<human>"}}

   Optional extras live alongside ``code``/``message`` inside ``error``
   (e.g., ``retry_after_s`` for 429s, ``fields`` for 400 validation).
   ``register_error_handlers`` wires this into Flask so any unhandled
   ``HTTPException`` or 500 on an ``/api/*`` route emerges as the
   envelope, instead of Flask's default text/html debug page.

4. **Success envelope shapes — flat vs wrapped (#419 A4).**

   Two project patterns exist for SUCCESS responses:

   - **Flat** (default for small surfaces): ``{"ok": true, ...payload}``.
     Used by ``/api/status``, ``/api/health``, most of ``/api/settings/*``.
     The client treats every non-``ok`` field as a top-level value.
   - **Wrapped** (for routes carrying structured sub-payloads):
     ``{"ok": true, "values": {...}, "anomalies": [...], "section_order": [...]}``.
     Used by ``/api/diagnostics`` (#416 PR2). The ``values`` wrapper is
     intentional — the SERVER-side schema gate
     (:func:`control_server.routes.diagnostics._sse._check_schema_match`)
     compares ``values.keys()`` against
     :func:`control_server._diagnostics_privacy.schema_keys`, so the
     full dict must stay a single addressable field. Flattening would
     either lose the schema-gate semantic or require renaming every
     diagnostics key with a prefix.

   Both shapes are first-class. Pick wrapped when:
   - the route carries multiple structurally distinct sub-payloads
     (data + metadata + ordering), OR
   - a server-side schema invariant operates on the full payload as a
     unit (e.g., diagnostics' ``schema_keys`` gate).

   Pick flat when:
   - the payload is a single small fixed surface (~5 keys), AND
   - no schema gate or PRIVACY_POLICY needs to see the full dict.

   Routes documenting wrapped responses inline their shape in the
   route's docstring (see ``routes/diagnostics/_sse.py:api_diagnostics``).

Non-API paths (the PWA shell at ``/``, ``/system``, etc.) keep Flask's
default HTML error pages — those routes are HTML consumers, not JSON
consumers.

Slug resolution: a curated entry in ``_DEFAULTS`` wins so we get stable
project slugs (``rate_limited`` instead of Werkzeug's ``too_many_requests``).
For statuses outside the table — including 422 validation, 503 service
unavailable, and the long tail of Werkzeug HTTPException subclasses — the
slug is derived from ``exc.name`` so M3-M5 don't have to expand the table
to get a sensible code on the wire.

``/api/*`` responses also carry ``Cache-Control: no-store`` (post-restart
reconnect probe polls /api/health every 3s — a cached 500 from a
mid-update window must not be served as 'last known good').
"""

from __future__ import annotations

import re
from typing import Any

from flask import Flask, Response, jsonify, request
from werkzeug.exceptions import HTTPException

# Curated (code-slug, message) for the statuses M0-M5 emit by name.
# Anything outside this table derives its slug from ``exc.name`` — the
# table only holds project-stable overrides where Werkzeug's default name
# would drift or where we want a shorter slug.
_DEFAULTS: dict[int, tuple[str, str]] = {
    400: ("bad_request", "The request could not be understood."),
    401: ("unauthorized", "Authentication required."),
    403: ("forbidden", "Access denied."),
    404: ("not_found", "The requested resource does not exist."),
    405: ("method_not_allowed", "The HTTP method is not allowed for this endpoint."),
    409: ("conflict", "The request conflicts with the current state."),
    413: ("payload_too_large", "The request body is too large."),
    415: ("unsupported_media_type", "The request body media type is not supported."),
    429: ("rate_limited", "Too many requests. Try again shortly."),
    500: ("server_error", "An unexpected error occurred."),
}

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug_from_name(name: str | None) -> str:
    """Convert a Werkzeug ``exc.name`` ('Bad Request', 'Unprocessable Entity',
    'Service Unavailable') into a snake_case slug. Empty / missing names fall
    back to the 500 default so the response always carries a non-empty slug."""
    if not name:
        return _DEFAULTS[500][0]
    slug = _SLUG_RE.sub("_", name.lower()).strip("_")
    return slug or _DEFAULTS[500][0]


def envelope(code: str, message: str, status: int, **extras: Any) -> tuple[Response, int]:
    """Build the canonical error response.

    Routes that already have detail (``invalid_action``, ``rate_limited``
    with ``retry_after_s``) pass their own slug + message + extras instead
    of leaning on the global handler's defaults.
    """
    error_body: dict[str, Any] = {"code": code, "message": message}
    error_body.update(extras)
    return jsonify({"ok": False, "error": error_body}), status


def _is_api_request() -> bool:
    return request.path.startswith("/api/")


def register_error_handlers(app: Flask) -> None:
    """Install global error handlers on ``app``. Called from ``create_app``.

    Two handlers + one ``after_request`` hook:

    - ``HTTPException`` covers ``abort(...)`` and Werkzeug's automatic
      404/405 dispatch. Returns the JSON envelope for ``/api/*``;
      otherwise re-raises so Flask renders its default HTML error page.
      Statuses outside ``_DEFAULTS`` derive their slug from ``exc.name``.
    - ``Exception`` catches anything else uncaught on ``/api/*`` paths
      and converts it to a generic 500 envelope. Stack traces are NOT
      leaked into the response body — same posture as ``system.py``'s
      subprocess error path.
    - ``after_request`` injects ``Cache-Control: no-store`` on ``/api/*``
      so the PWA's reconnect-probe (3s cadence) never serves a cached
      response from an intermediate proxy or service worker.
    """

    @app.errorhandler(HTTPException)
    def handle_http_exception(exc: HTTPException):
        if not _is_api_request():
            return exc
        status = exc.code or 500
        # Curated entry wins; otherwise derive from exc.name. Caller
        # override (abort(..., description=...)) wins over both — detected
        # by comparing the instance's description to the class default.
        if status in _DEFAULTS:
            code, default_message = _DEFAULTS[status]
        else:
            code = _slug_from_name(getattr(exc, "name", None))
            default_message = exc.name or _DEFAULTS[500][1]
        class_default = type(exc).description
        if exc.description and exc.description != class_default:
            message = exc.description
        else:
            message = default_message
        return envelope(code, message, status)

    @app.errorhandler(Exception)
    def handle_unhandled_exception(exc: Exception):
        # Flask's dispatch already routes HTTPException subclasses to the
        # more specific handler above; if we get here, exc is genuinely
        # uncaught application code. Non-API paths re-raise so Flask
        # renders its default HTML 500.
        if not _is_api_request():
            raise exc
        app.logger.exception("Unhandled exception on %s", request.path)
        code, message = _DEFAULTS[500]
        return envelope(code, message, 500)

    @app.after_request
    def add_response_hardening(response: Response) -> Response:
        # PWA reconnect probe polls /api/health every 3s; a cached 500 from
        # a mid-update window must not be served as 'last known good'.
        # ``setdefault`` lets a route opt out by setting Cache-Control
        # explicitly (none currently do).
        if _is_api_request():
            response.headers.setdefault("Cache-Control", "no-store")
        # Security hardening headers on EVERY response (SAST/DAST 2026-07, F1):
        # - nosniff: never let a browser MIME-sniff a response into something
        #   executable (defense-in-depth around the JSON + static surfaces).
        # - X-Frame-Options DENY: the control PWA is unauthenticated on the LAN,
        #   so a malicious same-LAN page could otherwise iframe it and clickjack
        #   the owner into a state-changing control (Reset WiFi / power off).
        #   NOTE: deliberately NOT applied to setup_server — the first-boot
        #   captive portal may legitimately render inside an OS captive-portal
        #   WebView, and DENY would break that.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response
