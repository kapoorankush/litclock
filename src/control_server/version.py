"""Resolve the version string surfaced by /api/health.

Resolution order:
1. ``LITCLOCK_VERSION_OVERRIDE`` env var / app.config (tests pin a value).
2. ``git describe --tags --always`` from the repo (fast, accurate, requires
   ``.git`` on disk — which the Pi has, since update.sh uses ``git pull``).
3. ``.images-version`` file (image-release pin, fallback when ``.git`` is
   absent — e.g., after a tarball-based DIY install).
4. Literal ``"unknown"``.

Cached at module import so the value is stable across requests; the
``litclock-control.service`` unit restarts on each ``update.sh`` run, which
naturally refreshes the cache.
"""

from __future__ import annotations

import functools
import subprocess
from pathlib import Path

# Repo root is two parents up: src/control_server/version.py -> src -> repo.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_IMAGES_VERSION_FILE = _REPO_ROOT / ".images-version"


@functools.lru_cache(maxsize=1)
def get_version(override: str | None = None) -> str:
    if override:
        return override
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--always"],
            capture_output=True,
            text=True,
            cwd=_REPO_ROOT,
            timeout=2,
        )
        if result.returncode == 0:
            value = result.stdout.strip()
            if value:
                return value
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    if _IMAGES_VERSION_FILE.exists():
        try:
            return _IMAGES_VERSION_FILE.read_text().strip() or "unknown"
        except OSError:
            pass
    return "unknown"


def reset_cache() -> None:
    """Test hook — clears the lru_cache so each test sees a fresh resolution."""
    get_version.cache_clear()
