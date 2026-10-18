"""One catalog resolution per request (litclock-dev#532 RT1, Codex O4).

Every reader of copy during a request — the Jinja ``t``/``catalog_subset``/
``t_rich`` globals, the diagnostics collector's "Language shown" row, the
copy payload — goes through :func:`request_snapshot`, so a language save or a
bundle recovering mid-request cannot make one response describe or render two
languages. The first reader creates the snapshot and stores it on ``flask.g``;
outside a request each call resolves on its own.
"""

from __future__ import annotations

from flask import g, has_request_context


def request_snapshot():
    import strings_catalog  # noqa: PLC0415 — the src/ chain, imported lazily like create_app's

    if not has_request_context():
        return strings_catalog.snapshot()
    snap = getattr(g, "litclock_strings", None)
    if snap is None:
        snap = g.litclock_strings = strings_catalog.snapshot()
    return snap
