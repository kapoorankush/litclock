"""LitClock Control PWA — Flask app factory.

Per PRD-LitClock-Control-PWA.md and PLAN-LitClock-Control-PWA.md (M1).
Always-on management surface that takes over from the first-boot setup_server
once `/etc/litclock/.setup-complete` exists. Routes land progressively:

- M1 (this milestone): PWA shell + /api/health.
- M2: /api/status (current quote + read-only system facts).
- M3: /api/settings/* (atomic env.sh writes via src/config.py).
- M4: /api/system/{reboot,poweroff} (privileged via /etc/sudoers.d/020).
- M5: /api/update/*, /api/wifi/reset.
- M6: PWA shell — manifest.json, sw.js, iOS splash matrix.

The factory pattern keeps tests isolated from the waitress entry point
(`src/control_server/app.py`) — tests instantiate via `create_app()` and use
Flask's built-in test client.
"""

from __future__ import annotations

import os
from datetime import timedelta

from flask import Flask


def create_app(test_config: dict | None = None) -> Flask:
    """Build and return the Flask app. ``test_config`` is merged on top of the
    default config and is the way pytest fixtures inject overrides without
    touching the environment."""
    app = Flask(
        __name__,
        static_folder="static",
        static_url_path="/static",
        template_folder="templates",
    )

    # PWA load perf (litclock-dev#436). Flask's static handler defaults to NO ``max-age``
    # — it ships an ETag and relies on conditional 304 revalidation. On a Pi
    # Zero 2W that means every PWA navigation (Status → Settings → Diagnostics →
    # …) re-validates ~9 CSS + ~9 JS + the woff2 faces against the device, a
    # burst of round-trips that reads as the "erratic / slow / feels broken"
    # symptom in litclock-dev#436. On iOS Safari at our plain-HTTP private-IP origin the
    # service worker never registers (sw-register.js gates on isSecureContext),
    # so the HTTP cache is the ONLY cache layer there — making this header the
    # single biggest, fully hardware-independent win for the primary platform.
    #
    # 15 minutes — not far-future, and deliberately not an hour: static URLs
    # are NOT fingerprinted (``/static/css/tokens.css`` is stable across
    # releases), so a long max-age serves stale CSS/JS after an ``update.sh``
    # that changed a file in place — fresh HTML against stale sub-resources,
    # worst exactly when a user opens /diagnostics right after an OTA. 15 min
    # is the shortest TTL that still exceeds a browsing session (the litclock-dev#436
    # revalidation storm is intra-session, seconds-to-minutes), so it kills the
    # storm while bounding post-update staleness on iOS to <=15 min instead of
    # an hour. Fingerprinting the URLs (far-future cache + instant bust) would
    # erase the window entirely, but it isn't worth the work for a single-user
    # LAN appliance where a 15-min revalidation is a cheap localhost 304 (litclock-dev#467,
    # considered + declined). The service worker keeps its own per-version
    # CACHE_NAME busting on Android (immune regardless); this is
    # the layer iOS relies on. ``/api/*`` is unaffected (errors.py forces
    # ``no-store``); ``/sw.js`` and ``/manifest.webmanifest`` set their own
    # explicit headers.
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = timedelta(minutes=15)

    # Defaults. Anything that comes from the environment lives here so tests
    # can override via test_config.
    app.config.from_mapping(
        ENV_FILE=os.environ.get("LITCLOCK_ENV_FILE", "/home/pi/litclock/env.sh"),
        # /api/health version source — see src/control_server/version.py for
        # the resolution order. Pinning here lets tests assert a fixed value.
        VERSION_OVERRIDE=os.environ.get("LITCLOCK_VERSION_OVERRIDE"),
        # Status hero "Last update" file paths (litclock-dev#330 + litclock-dev#334 + review I5).
        # Mirrors the ENV_FILE pattern: tests setting LITCLOCK_*_FILE in the
        # environment before create_app() should see those values land in
        # app.config so app.config["..."] = tmp_path overrides keep
        # working. routes/status.py reads app.config[...] in preference to
        # its module-level DEFAULT_* constants, so plumbing here is the
        # one missing link.
        UPDATE_STATUS_FILE=os.environ.get("LITCLOCK_UPDATE_STATUS_FILE"),
        LAST_UPDATE_FILE=os.environ.get("LITCLOCK_LAST_UPDATE_FILE"),
        LKG_SHA_FILE=os.environ.get("LITCLOCK_LKG_SHA_FILE"),
        PHASE3_SKIPPED_FILE=os.environ.get("LITCLOCK_PHASE3_SKIPPED_FILE"),
        # EPIC litclock-dev#383 PR2 handoff markers. Same env-override pattern as above so
        # tests point these at a tmp dir (and a direct marker write succeeds
        # there without sudo). See control_server/handoff.py for the lifecycle.
        SETUP_COMPLETE_FILE=os.environ.get("LITCLOCK_SETUP_COMPLETE_FILE", "/etc/litclock/.setup-complete"),
        HANDOFF_COMPLETE_FILE=os.environ.get("LITCLOCK_HANDOFF_COMPLETE_FILE", "/etc/litclock/.handoff-complete"),
        HANDOFF_TIMEOUT_S=float(os.environ.get("LITCLOCK_HANDOFF_TIMEOUT_S", "120")),
    )
    if test_config:
        app.config.update(test_config)

    # Tighten Jinja autoescape — Flask defaults autoescape *.html only.
    # We use the .html.j2 extension so M3's gift-mode message render path
    # gets free XSS protection per locked decision C3.
    # Setting jinja_options after the env is built is a no-op (the env
    # freezes its options on first access); jinja_env.autoescape is the
    # one that actually matters.
    app.jinja_env.autoescape = True
    # litclock-dev#532 Stage 3: templates resolve user-facing strings through
    # the language catalog. `t` renders one key; `catalog_subset` builds the
    # dict a page injects for its client-side JS. The JS keeps English
    # fallback literals for the STALE-JS window (/review litclock-dev#739: static assets
    # are unversioned — a client can hold diagnostics.js for up to 15 min /
    # one SW activation after an update, and old JS ignores the blob), for a
    # JSON-parse failure, and for test DOMs; CI pins those fallbacks against
    # the catalog so they are verified copies, not a second source.
    # NOTE: this import is a HARD dependency on the litclock-dev#532 file
    # chain (strings_catalog + languages.json + languages/) — a port that
    # carries this line without the chain kills create_app at boot.
    import strings_catalog as _strings_catalog  # noqa: PLC0415

    app.jinja_env.globals["t"] = _strings_catalog.get
    app.jinja_env.globals["catalog_subset"] = _strings_catalog.get_many
    # litclock-dev#532 pickers 5b: the Settings Language section (and,
    # once the gift-language write path lands, the System tab's gift
    # field) renders ONLY on a multi-language fleet. Registered as
    # callables, not values: env.sh is mtime-re-read per request, so a PWA
    # language save is visible on the next render without a restart. The
    # REGISTRY, by contrast, is positive-cached for process lifetime by
    # design (/review litclock-dev#739) — a language ACTIVATION arrives via OTA, and
    # update.sh restarts litclock-control.service, so the fresh registry
    # loads exactly when it can legitimately change.
    app.jinja_env.globals["active_language_choices"] = _strings_catalog.active_languages
    app.jinja_env.globals["current_language"] = _strings_catalog.active_language

    # litclock-dev#532 slice 6 — rich copy. Catalog values stay PLAIN TEXT;
    # mid-sentence emphasis is expressed with a tiny whitelisted token
    # vocabulary ({b}…{/b}, {em}…{/em}, {code}…{/code}) so translators can
    # move the emphasis where their language needs it. t_rich resolves the
    # key (slots filled as plain text), ESCAPES the whole result, and only
    # then converts the tokens to tags — the same escape-then-splice order
    # as system.js's card builders: catalog text can never smuggle markup;
    # only the whitelist reintroduces it.
    from markupsafe import Markup
    from markupsafe import escape as _escape

    _RICH_TOKENS = (
        ("{b}", "<strong>"),
        ("{/b}", "</strong>"),
        ("{em}", "<em>"),
        ("{/em}", "</em>"),
        ("{code}", "<code>"),
        ("{/code}", "</code>"),
    )

    def _t_rich(key, /, **slots):
        # Slot values fill LAST — after escaping AND after token
        # conversion — so a slot value can never assemble a whitelist
        # token or markup (the tokens are already spent, the value is
        # escaped). Mirrors system.js's esc-then-splice exactly.
        text = str(_escape(_strings_catalog.get(key)))
        for token, tag in _RICH_TOKENS:
            text = text.replace(token, tag)
        for name, value in slots.items():
            text = text.replace("{" + name + "}", str(_escape(value)))
        return Markup(text)

    app.jinja_env.globals["t_rich"] = _t_rich

    from .confirm_tokens import ConfirmTokenStore
    from .csrf import CsrfTokenStore
    from .errors import register_error_handlers
    from .rate_limit import RateLimiter
    from .routes import diagnostics, handoff, health, index, settings, status, sw, system, updates, wifi
    from .version import get_version

    # M4 destructive-action gates. Per-app instances so test create_app() calls
    # don't share token / rate-limit state across test cases.
    app.extensions["confirm_tokens"] = ConfirmTokenStore()
    app.extensions["system_rate_limiter"] = RateLimiter()
    # M3 Settings tab CSRF (D4). Multi-use, action="settings", TTL 30 min.
    app.extensions["csrf_tokens"] = CsrfTokenStore()

    # Project-wide JSON error envelope (issue litclock-dev#254). Installs handlers for
    # HTTPException + uncaught Exception so M2-M5 endpoints inherit the
    # convention without each route hand-rolling its own error shape.
    register_error_handlers(app)

    app.register_blueprint(health.bp)
    app.register_blueprint(index.bp)
    app.register_blueprint(settings.bp)
    app.register_blueprint(status.bp)
    app.register_blueprint(system.bp)
    # M5 (litclock-dev#245) — Updates tab + Reset-WiFi.
    app.register_blueprint(updates.bp)
    app.register_blueprint(wifi.bp)
    # M6 — /sw.js (templated) + /manifest.webmanifest (proper Content-Type/Cache-Control).
    app.register_blueprint(sw.bp)
    # EPIC litclock-dev#383 PR2 (litclock-dev#388) — post-WiFi PWA handoff (Done / browser-tz endpoints).
    app.register_blueprint(handoff.bp)
    # litclock-dev#416 PR2 — /api/diagnostics + /diagnostics placeholder. The
    # templated page + drawer markup land in PR3.
    app.register_blueprint(diagnostics.bp)

    # Inject handoff state into every template so base.html.j2 can render the
    # handoff banner (and aths-hint.js can suppress itself) when the device is
    # in the post-setup handoff window. Cheap when inactive (two stat() calls);
    # only reads env.sh + timedatectl when the banner is actually showing.
    from . import handoff as handoff_mod  # noqa: PLC0415

    @app.context_processor
    def _inject_handoff() -> dict:
        try:
            if handoff_mod.is_handoff_active(app):
                return {"handoff": handoff_mod.handoff_context(app)}
        except Exception:  # noqa: BLE001 — a context failure must not 500 every page
            app.logger.exception("handoff context injection failed")
        return {"handoff": {"active": False}}

    # litclock-dev#416 PR3c (F31) — inject `diag_shortcut_expanded` so base.html.j2
    # can set body[data-diag-ribbon-expanded]. Cheap (one env read per
    # render); the value is the per-process snapshot of the env, so a
    # toggle save that flushes via reload reflects on next paint.
    from ._env import read_env_settings  # noqa: PLC0415 — lazy

    @app.context_processor
    def _inject_diag_shortcut() -> dict:
        try:
            env = read_env_settings(app.config.get("ENV_FILE"))
            expanded = (env.get("SHOW_DIAGNOSTICS_SHORTCUT") or "false").strip().lower() == "true"
        except Exception:  # noqa: BLE001 — never 500 a page over a settings read
            expanded = False
        return {"diag_shortcut_expanded": expanded}

    # Prime the version cache at factory time so the first /api/health
    # post-restart doesn't pay the 50-200ms `git describe` subprocess fork
    # cost. The PWA's reconnect probe (PLAN A8) uses a 1s timeout; a cold
    # health response that hits a slow git could exceed it.
    get_version(app.config.get("VERSION_OVERRIDE"))

    return app
