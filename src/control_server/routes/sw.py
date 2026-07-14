"""GET /sw.js — service worker, served from a Jinja template (M6 D1).

Why a Flask route instead of a static file: ``CACHE_NAME = 'litclock-{version}'``
must change every release so that on update the new SW installs a fresh cache
and clears the old one. Templating lets us stamp the version at request time
without a build step or manual cache-name bumping.

Headers:
- ``Content-Type: text/javascript`` — required by browsers to register a SW.
- ``Cache-Control: max-age=0, must-revalidate`` — the SW spec already short-
  circuits cache for /sw.js, but pinning the headers keeps proxies honest.

Per D9 the matching ``static/js/sw-register.js`` short-circuits on iOS via an
``isSecureContext`` feature-detect — so this route only ever does real work
for Chromium-based browsers at our private-IP origin (M5 plain HTTP locked
in #257).

Plus ``GET /manifest.webmanifest`` — same blueprint, served from
``static/manifest.webmanifest`` but with the explicit MIME + Cache-Control
headers F3 locks (Flask's static handler defaults to ``application/json`` +
short max-age, neither of which matches the manifest spec).
"""

from __future__ import annotations

from flask import Blueprint, current_app, send_from_directory

from ..version import get_version

bp = Blueprint("sw", __name__)


@bp.route("/sw.js")
def service_worker() -> tuple[str, int, dict[str, str]]:
    version = get_version(current_app.config.get("VERSION_OVERRIDE"))
    body = current_app.jinja_env.get_template("sw.js.j2").render(version=version)
    return (
        body,
        200,
        {
            "Content-Type": "text/javascript; charset=utf-8",
            "Cache-Control": "max-age=0, must-revalidate",
        },
    )


@bp.route("/manifest.webmanifest")
def manifest() -> object:
    response = send_from_directory(
        current_app.static_folder,
        "manifest.webmanifest",
    )
    # F3: explicit MIME + cache headers. The manifest is largely immutable
    # between releases (icon paths, theme colors, name), so a 1-day cache
    # is fine and avoids re-fetching on every PWA cold launch.
    response.headers["Content-Type"] = "application/manifest+json"
    response.headers["Cache-Control"] = "max-age=86400"
    return response
