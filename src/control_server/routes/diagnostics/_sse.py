"""HTTP routes + SSE machinery for the diagnostics surface.

Split out of the pre-#419 monolithic ``routes/diagnostics.py`` (M1). This
module owns:

- The Flask :data:`bp` blueprint that all diagnostics routes attach to.
- The SSE subscriber registry + supersession lifecycle
  (:func:`_register_sse`, :func:`_unregister_sse`, :data:`_sse_registry`).
- The four route handlers: ``GET /api/diagnostics``, ``GET /diagnostics``,
  ``GET /api/logs``, ``GET /api/logs/stream``.
- The ``format_log_ts`` Jinja template filter consumed by
  ``diagnostics.html.j2``.
- The post-:func:`collect_diagnostics` envelope shaping
  (:func:`_redact_values_for_envelope`, :func:`_check_schema_match`,
  :func:`_parse_reveal_groups`).

Test patching note (D8): tests that monkey-patch ``collect_diagnostics``,
``_read_journal_tail`` (the ``/api/diagnostics/journal`` reader, #436),
``_compute_uncollected``, or ``_compute_section_states`` MUST patch them on
THIS module (where the route's name lookup happens), not on the package
``__init__.py``. The package re-exports the names for plain imports, but
Python binds names at import time in each module — the routes call
``collect_diagnostics()`` / ``_compute_section_states()`` /
``_read_journal_tail()`` which are looked up in THIS module's namespace.
"""

from __future__ import annotations

import json
import queue as _queue
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from flask import (
    Blueprint,
    Response,
    current_app,
    jsonify,
    render_template,
    request,
    stream_with_context,
)

from ... import log_buffer
from ..._diagnostics_privacy import PRIVACY_POLICY, redact
from ..._redaction import redact_text
from ...errors import envelope
from ._anomalies import (
    _compute_anomalies,  # noqa: F401 — kept as a test-binding seam (D8)
    _compute_section_states,
    _compute_uncollected,  # noqa: F401 — kept as a test-binding seam (D8)
)
from ._collectors import (
    DIAG_JOURNAL_LINES_MAX,
    DIAG_JOURNAL_LINES_PER_UNIT,
    DIAG_SUPPORT_JOURNAL_LINES,
    DIAG_SUPPORT_LOGS_BUDGET_S,
    DIAG_UNITS,
    SECTION_IDS,
    _read_journal_tail,
    collect_diagnostics,
    schema_keys,
)
from ._copy_payload import build_copy_payload, build_support_logs_bundle

bp = Blueprint("diagnostics", __name__)

__all__ = [
    "SSE_CONNECTION_TIMEOUT_S",
    "SSE_HEARTBEAT_INTERVAL_S",
    "SSE_INNER_POLL_S",
    "SSE_MAX_CONCURRENT_STREAMS",
    "_SseSession",
    "_check_schema_match",
    "_format_log_ts",
    "_generate_sse",
    "_parse_reveal_groups",
    "_redact_values_for_envelope",
    "_register_sse",
    "_serialize_log_entries",
    "_sse_format",
    "_sse_registry",
    "_unregister_sse",
    "api_diagnostics",
    "api_diagnostics_journal",
    "api_diagnostics_support_logs",
    "api_logs",
    "api_logs_stream",
    "bp",
    "page_diagnostics",
]


# --- SSE config -----------------------------------------------------------

SSE_MAX_CONCURRENT_STREAMS = 6
SSE_CONNECTION_TIMEOUT_S = 5 * 60
SSE_HEARTBEAT_INTERVAL_S = 15
# Cap the inner queue.get() so a sid-churn DoS can't pin waitress workers
# for 15 s each. The wire heartbeat still fires every
# ``SSE_HEARTBEAT_INTERVAL_S`` seconds; the inner poll just makes
# close_event observation snappier.
SSE_INNER_POLL_S = 1.0


# Accepted ``?level=`` values on /api/logs. Pre-#416 PR2 the route silently
# returned 0 entries for any unrecognised level — strict allowlist matches
# the rest of the route's validation.
_VALID_LOG_LEVELS: frozenset[str] = frozenset({"DEBUG", "INFO", "WARNING", "WARN", "ERROR", "CRITICAL"})

# The valid reveal-group values accepted on ``?reveal=`` for /api/diagnostics.
_VALID_REVEAL_GROUPS: frozenset[str] = frozenset({"location"})


@dataclass
class _SseSession:
    """One live SSE writer + the metadata the registry needs.

    ``close_reason`` is set by the registry BEFORE signalling close_event
    so the writer's exit frame can distinguish a same-sid replace
    (``"superseded"``) from a capacity-driven eviction
    (``"capacity-exceeded"``).
    """

    sid: str
    queue: Any  # queue.Queue[log_buffer.LogEntry]
    close_event: threading.Event
    started_at: float
    close_reason: str = ""


# OrderedDict so LRU eviction at cap is cheap. Keyed by sid. Initialised
# eagerly at module load (the pre-#416 PR2 lazy-init was racy on the first
# two concurrent SSE connections — Security specialist finding F-LAZY-RACE).
_sse_registry: OrderedDict[str, _SseSession] = OrderedDict()
_sse_registry_lock = threading.Lock()


def _register_sse(sid: str, q: Any) -> tuple[_SseSession, _SseSession | None]:
    """Add a new session to the registry, returning (session, superseded).

    If a session with the same ``sid`` already exists, the old one is
    returned as ``superseded`` and the caller is responsible for signalling
    its close. If the registry is at capacity, the LRU session is evicted
    and returned with ``close_reason='capacity-exceeded'``.
    """
    new_session = _SseSession(
        sid=sid,
        queue=q,
        close_event=threading.Event(),
        started_at=time.time(),
    )
    superseded: _SseSession | None = None
    with _sse_registry_lock:
        if sid in _sse_registry:
            superseded = _sse_registry.pop(sid)
            superseded.close_reason = "superseded"
        elif len(_sse_registry) >= SSE_MAX_CONCURRENT_STREAMS:
            _, evicted = _sse_registry.popitem(last=False)
            superseded = evicted
            superseded.close_reason = "capacity-exceeded"
        _sse_registry[sid] = new_session
    return new_session, superseded


def _unregister_sse(sid: str, session: _SseSession | None = None) -> None:
    """Remove ``sid`` from the registry IFF the current registry entry IS
    ``session``. Without the identity check, an old generator's ``finally``
    can pop a newer session that already re-registered the same sid —
    orphaning the live session and breaking the cap accounting.
    """
    with _sse_registry_lock:
        current = _sse_registry.get(sid)
        if session is None or current is session:
            _sse_registry.pop(sid, None)


# --- Envelope shaping ------------------------------------------------------


def _parse_reveal_groups(raw: str | None) -> frozenset[str]:
    """Validate + collect ``?reveal=`` groups. Unknown values are ignored
    silently so a future client-server skew doesn't 400 the surface."""
    if not raw:
        return frozenset()
    return frozenset({g for g in raw.split(",") if g in _VALID_REVEAL_GROUPS})


def _redact_values_for_envelope(values: dict[str, Any], revealed_groups: frozenset[str]) -> dict[str, Any]:
    """Apply PRIVACY_POLICY redaction to the JSON ``values`` dict.

    Pre-#416 PR2 /review the route emitted ``values`` raw — ssid, 6dp
    lat/lon, lan_ip, gateway, weather_location_name all visible to any
    LAN client. For safe-clear fields, the native value is preserved.
    For redacted/rounded fields the rendered string is substituted unless
    the reveal group is on.
    """
    out: dict[str, Any] = {}
    for field, value in values.items():
        policy = PRIVACY_POLICY.get(field)
        if policy is None:
            # Unknown field — leave raw and let _check_schema_match log it.
            out[field] = value
            continue
        if policy.copy == "safe-clear":
            out[field] = value
            continue
        if value is None:
            out[field] = None
            continue
        out[field] = redact(field, value, kind="copy", revealed_groups=revealed_groups)
    return out


# --- Schema validity self-check -------------------------------------------
# Defensive smoke check that fires only in DEBUG-equivalent environments.
# The real CI keystone lives in the test file.
_SCHEMA_KEYS = schema_keys()


def _check_schema_match(values: dict[str, Any]) -> None:
    missing = _SCHEMA_KEYS - values.keys()
    extra = values.keys() - _SCHEMA_KEYS
    if missing or extra:
        current_app.logger.warning(
            "DIAGNOSTICS_SCHEMA_DRIFT: missing=%s extra=%s",
            sorted(missing),
            sorted(extra),
        )


# --- Routes ----------------------------------------------------------------


@bp.route("/api/diagnostics")
def api_diagnostics() -> tuple[Any, int]:
    """JSON envelope with structured diagnostics values + anomaly flags.

    Response shape (#419 A4 — kept WRAPPED, not flattened):

    ```json
    {
      "ok": true,
      "values": { ...all schema_keys() fields, redacted per PRIVACY_POLICY... },
      "anomalies": ["network", "last-quote"],
      "copy_payload": "```markdown\\n# LitClock diagnostics\\n...",
      "section_order": ["build-version", "system", "network", ...],
      "revealed_groups": ["location"]
    }
    ```

    Wrapped (rather than flat) because the server-side schema gate
    (:func:`_check_schema_match`) compares ``values.keys()`` against
    :func:`schema_keys` to catch new fields that landed in
    ``collect_diagnostics`` without a :data:`PRIVACY_POLICY` entry — and
    that gate needs the full dict as a single addressable field.
    Flattening would split the schema-keyset across the top-level keys
    of the response and lose the invariant. Sibling routes like
    ``/api/status`` use a flat shape because they expose a small fixed
    surface; diagnostics is the project's wrapped-envelope pattern. See
    :mod:`control_server.errors` for the cross-route envelope rules.

    Invariants (#419 A2):

    - ``section_order`` is the CANONICAL render order for the PWA's
      diagnostics sections. The current ``diagnostics.html.j2`` template
      hardcodes the order to match :data:`SECTION_IDS`; the field is
      emitted on the wire as a forward-compat / advisory signal so a
      future client can adopt server-driven ordering without a server
      change.
    - ``anomalies`` is ALWAYS a subset of ``section_order``. A section ID
      appearing in ``anomalies`` but not ``section_order`` is a bug.
    - ``section_order`` is PROCESS-STABLE (same across requests on one
      process) but MAY CHANGE across releases. Clients caching the
      ordering across deploys must re-fetch.

    Error path (#419 T7 covers this): on any exception during the
    collect-and-shape pipeline, the route returns 500 + the standard
    JSON envelope ``{ok: false, error: {code, message}}`` via
    :func:`control_server.errors.envelope`. Error code is
    ``diagnostics_unavailable``. Same JSON Content-Type as the success
    path (never flips to HTML 500).
    """
    try:
        values = collect_diagnostics()
        _check_schema_match(values)
        # #432 — _compute_section_states applies uncollected-wins
        # precedence over BOTH _compute_anomalies + _compute_uncollected
        # in one place. The route never calls those predicates directly —
        # the helper IS the single source of truth for precedence. See the
        # helper's docstring for why uncollected-wins (it's what actually
        # closes the user-reported fresh-flash bug — anomaly-wins on
        # overlap would leave the orange pills #432 was opened to remove).
        anomalies, uncollected = _compute_section_states(values)
        revealed = _parse_reveal_groups(request.args.get("reveal"))
        copy_payload = build_copy_payload(values, revealed_groups=revealed)
        envelope_values = _redact_values_for_envelope(values, revealed)
    except Exception as exc:  # noqa: BLE001 — final-bailout error envelope
        current_app.logger.error("collect_diagnostics raised: %s", exc, exc_info=True)
        return envelope("diagnostics_unavailable", "Diagnostics unavailable.", 500)
    return (
        jsonify(
            {
                "ok": True,
                "values": envelope_values,
                "anomalies": anomalies,
                "uncollected": uncollected,
                "copy_payload": copy_payload,
                "section_order": list(SECTION_IDS),
                "revealed_groups": sorted(revealed),
            }
        ),
        200,
    )


@bp.route("/api/diagnostics/journal")
def api_diagnostics_journal() -> tuple[Any, int]:
    """Per-unit journal tail — the async hydration source for the Services
    section (#436).

    Split OUT of ``collect_diagnostics`` (and therefore off both the SSR and the
    30 s poll critical paths) so a cold ``journalctl`` (~5-7 s on a Pi Zero 2W)
    never blocks first paint. The PWA fires ONE request per non-healthy unit, so
    a slow unit can't stall another's tail (the multi-failure case that
    otherwise blew the client's 10 s budget — #433 OV-7 / #436 T4).

    SECURITY: ``unit`` feeds ``journalctl -u <unit>`` via
    :func:`_read_journal_tail`. It MUST be a member of the fixed
    :data:`DIAG_UNITS` allowlist or we'd expose arbitrary unit logs. Anything
    else is a 400 ``invalid_unit`` with NO subprocess fork.

    Tails are redacted with the same :func:`redact_text` the ``safe-clear``
    ``service_states`` field uses (journald content lives outside
    control_server's RedactingFilter — other services log raw SSIDs / weather
    keys). ``no-store`` because a tail can contain secrets pre-redaction-audit.
    """
    unit = request.args.get("unit", "")
    if unit not in DIAG_UNITS:
        return envelope("invalid_unit", "Unknown unit.", 400)
    lines = _parse_lines_param(request.args.get("lines"))
    try:
        if lines == DIAG_JOURNAL_LINES_PER_UNIT:
            # Default depth shares the page-preview cache (warm from the poll).
            tail = [redact_text(line) for line in _read_journal_tail(unit)]
        else:
            # Deeper read: ONE cache key per unit (up to MAX), sliced to the
            # requested depth. Keying per lines-value would let a LAN peer rotate
            # ?lines=4..200 to force cold journalctl forks (cache-miss DoS,
            # /review) — this collapses the fan-out to a single warm key per unit,
            # shared with the support-logs export. Distinct from the 3-line
            # preview key so it never serves or poisons the page cache.
            deep = _read_journal_tail(unit, n=DIAG_JOURNAL_LINES_MAX, cache_key=f"diag-journal-{unit}-deep")
            tail = [redact_text(line) for line in deep[-lines:]]
    except Exception as exc:  # noqa: BLE001 — final-bailout error envelope
        current_app.logger.error("journal tail for %s raised: %s", unit, exc, exc_info=True)
        return envelope("journal_unavailable", "Journal unavailable.", 500)
    response = jsonify({"ok": True, "unit": unit, "journal_tail": tail, "lines": lines})
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response, 200


def _parse_lines_param(raw: str | None) -> int:
    """Clamp the ``?lines=`` param to [1, DIAG_JOURNAL_LINES_MAX]; default to the
    3-line page depth. Non-numeric → default (never a 500 on a stray query)."""
    if raw is None or raw == "":
        return DIAG_JOURNAL_LINES_PER_UNIT
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DIAG_JOURNAL_LINES_PER_UNIT
    return max(1, min(n, DIAG_JOURNAL_LINES_MAX))


@bp.route("/api/diagnostics/support-logs")
def api_diagnostics_support_logs() -> Any:
    """On-demand 'deep logs for support' export (#416 follow-up).

    A single downloadable text bundle: the standard copy payload (system state,
    default-redacted) + a deeper per-unit journal tail (``DIAG_SUPPORT_JOURNAL_LINES``
    vs the 3-line page preview) across the fixed ``DIAG_UNITS`` allowlist, so a
    non-technical user can hand support one file/paste that actually carries
    enough to debug — no SSH.

    Off the page/poll critical path (its own route). Deep reads use a distinct,
    line-count-scoped cache key so they never disturb the page preview cache, and
    a wall-clock budget (``DIAG_SUPPORT_LOGS_BUDGET_S``) bounds the serial
    journalctl loop with an explicit truncation note. ``no-store`` + redacted;
    served as a ``text/plain`` attachment so the no-JS path just downloads it.
    """
    try:
        values = collect_diagnostics()
        system_payload = build_copy_payload(values)  # default reveal → redacted

        def _deep_tail(unit: str) -> list[str]:
            # Same single deep cache key per unit as the ?lines= path, so a page
            # deep-read and a support-logs download share one warm journalctl
            # result (read up to MAX, show the last DIAG_SUPPORT_JOURNAL_LINES).
            deep = _read_journal_tail(unit, n=DIAG_JOURNAL_LINES_MAX, cache_key=f"diag-journal-{unit}-deep")
            return [redact_text(line) for line in deep[-DIAG_SUPPORT_JOURNAL_LINES:]]

        bundle = build_support_logs_bundle(system_payload, DIAG_UNITS, _deep_tail, budget_s=DIAG_SUPPORT_LOGS_BUDGET_S)
    except Exception as exc:  # noqa: BLE001 — text endpoint: return a plain 500 body
        current_app.logger.error("support-logs bundle raised: %s", exc, exc_info=True)
        return Response("Support logs unavailable.\n", status=500, mimetype="text/plain")
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    # mimetype (not a charset-bearing content_type): werkzeug appends the
    # charset once → "text/plain; charset=utf-8" (passing it inline doubled it).
    response = Response(bundle, mimetype="text/plain")
    response.headers["Content-Disposition"] = f'attachment; filename="litclock-support-logs-{ts}.txt"'
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


@bp.app_template_filter("format_log_ts")
def _format_log_ts(timestamp: Any) -> str:
    """Jinja filter — turn a unix-float timestamp into ``HH:MM:SS``.

    Used by ``diagnostics.html.j2``'s recent-log-entries section so each
    snapshot row renders the same wall-clock shape the live drawer uses.
    Falls back to ``""`` for malformed input rather than raising in the
    template render path.
    """
    try:
        ts = float(timestamp)
    except (TypeError, ValueError):
        return ""
    try:
        return datetime.fromtimestamp(ts, tz=UTC).strftime("%H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return ""


@bp.route("/diagnostics")
def page_diagnostics() -> Any:
    # On error, let Flask render its default HTML 500 — the route's
    # advertised Content-Type is text/html, not JSON.
    values = collect_diagnostics()
    _check_schema_match(values)
    # #432 — single source of truth for tri-state precedence; matches the JSON
    # route's shape so SSR first-paint and the 30s poll never disagree on
    # STATE / verdict. (Since #436 neither path carries journal tails — those
    # hydrate per-unit via /api/diagnostics/journal after first paint — so the
    # "never disagree" contract is about state/anomalies, not tail text.)
    anomalies, uncollected = _compute_section_states(values)
    revealed = _parse_reveal_groups(request.args.get("reveal"))
    copy_payload = build_copy_payload(values, revealed_groups=revealed)
    envelope_values = _redact_values_for_envelope(values, revealed)
    body = render_template(
        "diagnostics.html.j2",
        values=envelope_values,
        anomalies=anomalies,
        uncollected=uncollected,
        copy_payload=copy_payload,
        section_order=list(SECTION_IDS),
        revealed_groups=sorted(revealed),
        active_tab=None,
    )
    response = current_app.make_response(body)
    # F-SW-CACHE: the response can contain unredacted SSID + city + 6dp
    # coords if ``?reveal=location`` was set. Without this header the PWA
    # service worker (sw.js.j2 navigateNetworkFirst) would cache the
    # rendered HTML and serve it later — past the sessionStorage-scoped
    # Reveal window.
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


# --- /api/logs (paginated read) -------------------------------------------


def _serialize_log_entries(entries: list[Any]) -> list[dict[str, Any]]:
    """LogEntry → dict for JSON. The handler returns dataclass instances;
    the to_dict() shape is the documented SSE wire format too."""
    return [e.to_dict() for e in entries]


@bp.route("/api/logs", methods=["GET"])
def api_logs() -> tuple[Any, int]:
    """Return a paginated slice of the in-memory buffer.

    Query string:
    - ``limit`` — int, clamped to [1, 500]. Default 100.
    - ``level`` — optional level filter.
    - ``since_seq`` — optional int. Only return entries with seq > this
      value. The SSE client uses this on reconnect to backfill any gap.

    There is intentionally **no DELETE endpoint** — /plan-eng-review
    OV-3=A removed it because clearing in-memory state is a write.
    """
    handler = log_buffer.get_memory_handler()
    if handler is None:
        return envelope(
            "log_buffer_unavailable",
            "Log buffer not initialised on this process.",
            503,
        )
    try:
        limit_raw = request.args.get("limit", "100")
        limit = max(1, min(int(limit_raw), log_buffer.MAX_ENTRIES))
    except ValueError:
        return envelope("bad_limit", "limit must be an integer.", 400)
    level = request.args.get("level") or None
    if level is not None and level.upper() not in _VALID_LOG_LEVELS:
        return envelope(
            "bad_level",
            "level must be one of DEBUG/INFO/WARNING/ERROR/CRITICAL.",
            400,
        )
    since_seq: int | None = None
    raw_since = request.args.get("since_seq")
    if raw_since is not None:
        try:
            since_seq = int(raw_since)
        except ValueError:
            return envelope("bad_since_seq", "since_seq must be an integer.", 400)
    # #419 PR2 P3: snapshot() takes ``_lock`` once for all three reads so
    # the badge counter (``total``) and the resume-seq (``latest_seq``)
    # can't drift across an in-flight ``emit()``.
    entries, total, latest_seq = handler.snapshot(limit=limit, level=level, since_seq=since_seq)
    return (
        jsonify(
            {
                "ok": True,
                "entries": _serialize_log_entries(entries),
                "total": total,
                "latest_seq": latest_seq,
                "limit": limit,
                "level": level,
                "since_seq": since_seq,
            }
        ),
        200,
    )


# --- /api/logs/stream (SSE) -----------------------------------------------


def _sse_format(event: str | None, data: dict[str, Any]) -> str:
    """Format a Server-Sent Events frame. Two newlines terminate the frame
    per the EventSource spec; missing event= falls back to the client's
    default 'message' handler."""
    payload = json.dumps(data, default=str)
    if event:
        return f"event: {event}\ndata: {payload}\n\n"
    return f"data: {payload}\n\n"


def _generate_sse(sid: str, since_seq: int | None) -> Any:
    """Generator that pumps entries from the subscriber's queue to the wire.

    Lifecycle:
    1. Subscribe to the buffer; register the session (LRU evict any
       over-cap; supersede any prior session with the same sid).
    2. Backfill if since_seq is provided.
    3. Loop: get(timeout=SSE_INNER_POLL_S). On entry → yield it. On Empty
       → check close_event + bookkeep heartbeat. On close → emit the
       reason + return. On overall timeout → break.
    4. ``finally`` → unsubscribe + unregister (identity-safe).
    """
    handler = log_buffer.get_memory_handler()
    if handler is None:
        yield _sse_format("error", {"code": "log_buffer_unavailable"})
        return
    q = handler.subscribe()
    session, superseded = _register_sse(sid, q)
    if superseded is not None:
        superseded.close_event.set()
    try:
        if since_seq is not None and since_seq >= 0:
            # #419 PR2 P4 (D10): get_logs(order='asc') returns the newest-N
            # slice in chronological order — same set the old
            # ``reversed(get_logs(...))`` produced, but one pass instead of
            # two. Limit STILL means "newest N"; order is post-sort.
            backfill = handler.get_logs(
                since_seq=since_seq,
                limit=log_buffer.MAX_ENTRIES,
                order="asc",
            )
            for entry in backfill:
                yield _sse_format("entry", entry.to_dict())
        yield _sse_format("hello", {"sid": sid, "latest_seq": handler.latest_seq()})
        start = time.monotonic()
        last_heartbeat = start
        while True:
            if session.close_event.is_set():
                event = session.close_reason or "superseded"
                yield _sse_format(event, {"sid": sid})
                return
            if time.monotonic() - start > SSE_CONNECTION_TIMEOUT_S:
                yield _sse_format("timeout", {"sid": sid})
                return
            try:
                entry = q.get(timeout=SSE_INNER_POLL_S)
            except _queue.Empty:
                if time.monotonic() - last_heartbeat >= SSE_HEARTBEAT_INTERVAL_S:
                    yield _sse_format("heartbeat", {"t": time.time()})
                    last_heartbeat = time.monotonic()
                continue
            yield _sse_format("entry", entry.to_dict())
            last_heartbeat = time.monotonic()
    finally:
        handler.unsubscribe(q)
        _unregister_sse(sid, session)


@bp.route("/api/logs/stream", methods=["GET"])
def api_logs_stream() -> Any:
    """SSE endpoint for the live drawer.

    Client supplies ``?sid=<uuid>`` (OV-2=A). Optional ``since_seq`` lets
    a reconnecting client backfill the gap (replayed via
    :func:`MemoryLogHandler.get_logs` ``order='asc'`` so the client sees
    entries in chronological order — see #419 D10).

    ## Wire-event contract (#419 A1 + A3)

    The stream emits SSE frames using the standard ``event:`` + ``data:``
    shape. Two frame families with DIFFERENT data shapes:

    **Normal (JSON object on ``data:``):**

    - ``event: entry`` — ``data: {seq, timestamp, level, logger, message}``
    - ``event: hello`` — ``data: {sid, latest_seq}``. Sent once after
      backfill replays so the client can pin its resume baseline.
    - ``event: heartbeat`` — ``data: {t}``. IDLE keep-alive: fires only
      when the inner poll has timed out :data:`SSE_HEARTBEAT_INTERVAL_S`
      seconds without an ``entry`` frame (real entries reset the
      heartbeat clock). A continuously-active stream may never emit a
      heartbeat. No client action required.

    **Lifecycle close frames (single object, then stream ends):**

    - ``event: superseded`` — ``data: {sid}``. Another EventSource with
      the same ``sid`` connected (typically a same-browser tab swap).
      Client MUST stop reading; the new tab owns the slot. Drawer's
      ``drawer.js:444`` matches this.
    - ``event: capacity-exceeded`` — ``data: {sid}``. Server is at
      :data:`SSE_MAX_CONCURRENT_STREAMS` and this connection lost the
      LRU race. **Client MUST back off** (longer than the normal
      reconnect interval) so we don't churn other clients out of their
      slots. Drawer enforces this at ``drawer.js:452`` via
      ``CAPACITY_BACKOFF_MS``. Without backoff, a misbehaving client
      can DoS its own slot.
    - ``event: timeout`` — ``data: {sid}``. Server hit
      :data:`SSE_CONNECTION_TIMEOUT_S` (5 min). Client may reconnect
      immediately.

    **Error frames (DIFFERENT data shape — minimal code-only object):**

    - ``event: error`` — ``data: {"code": "log_buffer_unavailable"}``.
      Emitted only when ``_generate_sse`` discovers the log handler is
      missing on its first check (before subscribing). The single
      ``code`` field differs from the standard HTTP error envelope
      (``{ok: false, error: {code, message}}``). Clients must handle
      BOTH shapes: HTTP-envelope JSON on the initial pre-stream GET
      (404/400/503), AND the in-stream ``event: error`` frame which
      fires at handler-missing boot before the stream opens for real.
      Drawer handles the SSE event at ``drawer.js:465`` plus the
      EventSource ``onerror`` transport callback for network-level
      failures. Note: today's code emits ``event: error`` only on the
      one handler-missing path; a future contributor adding new
      in-stream error scenarios should preserve the ``{"code": "..."}``
      shape.

    ## HTTP error envelopes (before stream opens)

    Pre-stream validation can still 400 / 503 with the standard JSON
    envelope:

    - 400 ``missing_sid`` — no ``?sid=`` query arg
    - 400 ``bad_sid`` — sid outside [4, 128] chars or not URL-safe
    - 400 ``bad_since_seq`` — non-integer ``?since_seq=``
    - 503 ``log_buffer_unavailable`` — buffer not initialised
    """
    handler = log_buffer.get_memory_handler()
    if handler is None:
        return envelope(
            "log_buffer_unavailable",
            "Log buffer not initialised on this process.",
            503,
        )
    sid = request.args.get("sid")
    if not sid:
        return envelope("missing_sid", "sid query parameter is required.", 400)
    # Soft sid shape gate — permissive (length 4-128 with URL-safe chars).
    if not (4 <= len(sid) <= 128) or not all(c.isalnum() or c in "-_" for c in sid):
        return envelope("bad_sid", "sid must be 4-128 url-safe chars.", 400)
    since_seq: int | None = None
    raw_since = request.args.get("since_seq")
    if raw_since is not None:
        try:
            since_seq = int(raw_since)
        except ValueError:
            return envelope("bad_since_seq", "since_seq must be an integer.", 400)
    # WSGI apps MUST NOT set hop-by-hop response headers (PEP 3333 §
    # "Other HTTP Features"). ``Connection`` is hop-by-hop, so listing
    # ``Connection: keep-alive`` here causes waitress (the production
    # WSGI server) to ``AssertionError`` on start_response → 500 → no
    # streaming. Flask's test_client uses a bypass path that doesn't
    # enforce PEP 3333, so unit tests pass but production crashes.
    # Hardware QA on authorclock surfaced this — caught after v0.214.0
    # shipped because the entire SSE drawer surface was non-functional
    # on real waitress. Cutting v0.214.1 hotfix.
    #
    # WSGI/the server already manages connection lifetime correctly
    # without an app-level Connection header. The browser EventSource
    # client doesn't need it either — text/event-stream + the absence
    # of Content-Length implicitly tells it to hold the connection open.
    headers = {
        "Content-Type": "text/event-stream",
        "X-Accel-Buffering": "no",
        "Cache-Control": "no-cache, no-transform",
    }
    return Response(
        stream_with_context(_generate_sse(sid, since_seq)),
        status=200,
        headers=headers,
    )
