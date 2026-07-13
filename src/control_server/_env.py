"""env.sh reader for control_server consumers (#416 / design D22).

Factored out of ``routes/status.py:_read_env_file_settings`` (M2 PR #245) so
diagnostic, status, and any future route that needs env.sh values reads
through one helper. The original name was misleading — the function never
filtered to weather; it always returned every parsed KEY=value pair. The new
name reflects the actual behavior, and the function lives in a module that
is not tied to the Status blueprint.

Reading is delegated to ``src/config.py:load_config`` (the canonical parser
used by both writers, the literary_clock runtime, and tests). Returns ``{}``
on any read error so callers can gracefully render an em-dash row instead
of bubbling the exception up to the request handler.

Lookup order for ``env_file``:
  1. The explicit ``env_file`` argument (typically plumbed from
     ``app.config['ENV_FILE']`` via the route helpers).
  2. The ``LITCLOCK_ENV_FILE`` environment variable.
  3. Returns ``{}`` when neither is set.

Preferring the explicit arg over the env var lets tests using
``create_app({'ENV_FILE': ...})`` override without monkey-patching os.environ
(adversarial /review on M2 caught this gap; the PR #245 wrapper carried the
same plumbing and this extraction preserves it verbatim).
"""

from __future__ import annotations

import os


def read_env_settings(env_file: str | None = None) -> dict[str, str]:
    """Parse ``env_file`` into a dict of KEY=value pairs.

    Returns the full parsed dict (no filtering). Consumers pick the keys they
    care about; per-row privacy decisions live in
    ``control_server/_diagnostics_privacy.py``.
    """
    if env_file is None:
        env_file = os.environ.get("LITCLOCK_ENV_FILE")
    if not env_file:
        return {}
    try:
        # Lazy import so test plumbing that stubs callers (e.g. status.py's
        # _weather_city) doesn't pay an import-time cost. config.load_config
        # returns {} for missing files — we treat ANY read failure the same.
        import config as _config  # noqa: PLC0415

        return _config.load_config(env_file)
    except Exception:
        return {}
