"""Language-catalog loader (litclock-dev#532 Stage 3).

One ``LITCLOCK_LANGUAGE`` setting drives every user-facing string surface;
the catalogs live in ``languages/<code>/strings.json`` and the registry in
``languages.json`` at the repo root. English is the CANONICAL key set —
CI diffs every other catalog against it.

Naming (/review litclock-dev#738): the setting is ``LITCLOCK_LANGUAGE``, NOT the
POSIX ``LANGUAGE`` — that name is glibc/gettext's locale-priority list,
which SSH sessions forward inward with values like ``en_US:en`` and which
env.sh (sourced by five scripts) must not override for subprocesses.

Resolution order (/review litclock-dev#738 F3 — the control server never sources
env.sh, it reads the FILE):

1. ``LITCLOCK_LANGUAGE`` in the process environment — set by the
   env.sh-sourcing contexts (runtheclock.sh) and by tests.
2. The ``LITCLOCK_LANGUAGE`` line of env.sh itself, located via
   ``LITCLOCK_ENV_FILE`` / ``LITCLOCK_DIR`` / the repo root. The file is
   re-read when its mtime changes, so a PWA language save reaches a
   running control server WITHOUT a restart (the design doc's
   "switchable anytime").
3. English.

Contract (scope-audit item 7): a missing or corrupt bundle must never
take a screen down. Every lookup falls back to the English catalog, then
to the key itself — it NEVER raises. Failed catalog loads are
negative-cached for a short TTL, not for process lifetime (/review litclock-dev#738
F4: a transient read error during an OTA window must heal itself), and
every warning fires once per distinct subject, not per lookup.

Slots are filled with plain substring replacement of ``{name}`` — the
same deliberately dumb mechanism as the JS side's split/join: no
format() machinery, so a stray brace in translated copy cannot raise and
a slot value containing braces cannot recurse.

Plural selection is English-shaped for now (``one``/``other``); the
registry's ``plural_forms`` declares each language's categories and the
CI gate enforces catalog completeness against them, but a real
category-selection function lands with the first language that needs one
(Stage 4).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger(__name__)

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
REGISTRY_PATH: Final[Path] = _REPO_ROOT / "languages.json"
CANONICAL_LANGUAGE: Final[str] = "en"
ENV_KEY: Final[str] = "LITCLOCK_LANGUAGE"
# Failed loads retry after this many seconds instead of sticking for
# process lifetime (an OTA's git reset window is seconds long).
NEGATIVE_CACHE_TTL_S: Final[float] = 30.0

_ENV_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:export\s+)?" + ENV_KEY + r"\s*=\s*[\"\']?([A-Za-z0-9_-]*)"
)

_lock = threading.Lock()
_cache: dict[str, dict[str, str]] = {}
_cache_failed_at: dict[str, float] = {}
_registry_cache: dict[str, Any] | None = None
_registry_failed_at: float | None = None
_env_file_state: tuple[str, float, str] | None = None  # (path, mtime, value)
_warned: set[str] = set()


def _warn_once(subject: str, fmt: str, *args: Any) -> None:
    with _lock:
        if subject in _warned:
            return
        _warned.add(subject)
    logger.warning(fmt, *args)


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        _warn_once(f"load:{path}", "strings catalog unreadable at %s: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def _registry() -> dict[str, Any]:
    global _registry_cache, _registry_failed_at
    now = time.monotonic()
    with _lock:
        if _registry_cache is not None:
            return _registry_cache
        if _registry_failed_at is not None and now - _registry_failed_at < NEGATIVE_CACHE_TTL_S:
            return {}
    loaded = _load_json(REGISTRY_PATH)
    with _lock:
        if loaded:
            # Only a SUCCESSFUL load is cached for process lifetime; a failed
            # one retries after the TTL (/review litclock-dev#739: the first version
            # positive-cached {} forever, defeating the very mid-OTA-heal
            # semantics the catalog negative cache exists for).
            if _registry_cache is None:
                _registry_cache = loaded
            _registry_failed_at = None
            return _registry_cache
        _registry_failed_at = now
        return {}


def _env_file_path() -> Path | None:
    explicit = os.environ.get("LITCLOCK_ENV_FILE")
    if explicit:
        return Path(explicit)
    install = os.environ.get("LITCLOCK_DIR")
    if install:
        return Path(install) / "env.sh"
    candidate = _REPO_ROOT / "env.sh"
    return candidate if candidate.exists() else None


def _language_from_env_file() -> str:
    """The durable source: env.sh's LITCLOCK_LANGUAGE line, mtime-cached."""
    global _env_file_state
    path = _env_file_path()
    if path is None:
        return ""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ""
    with _lock:
        if _env_file_state and _env_file_state[0] == str(path) and _env_file_state[1] == mtime:
            return _env_file_state[2]
    value = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _ENV_LINE_RE.match(line)
                if m:
                    value = m.group(1)
    except OSError:
        return ""
    with _lock:
        _env_file_state = (str(path), mtime, value)
    return value


def active_language() -> str:
    """The device language; unknown / inactive codes degrade to English.

    The registry's ``status`` gate makes ``incubating`` languages
    unselectable by contract: an incomplete bundle must not be servable.
    """
    code = (os.environ.get(ENV_KEY) or "").strip() or _language_from_env_file() or CANONICAL_LANGUAGE
    if code == CANONICAL_LANGUAGE:
        return code
    entry = (_registry().get("languages") or {}).get(code)
    if not isinstance(entry, dict) or entry.get("status") != "active":
        _warn_once(
            f"lang:{code}",
            "%s=%r is not an active registry language; using English",
            ENV_KEY,
            code,
        )
        return CANONICAL_LANGUAGE
    return code


def _catalog(code: str) -> dict[str, str]:
    now = time.monotonic()
    with _lock:
        if code in _cache:
            return _cache[code]
        failed_at = _cache_failed_at.get(code)
        if failed_at is not None and now - failed_at < NEGATIVE_CACHE_TTL_S:
            return {}
    entry = (_registry().get("languages") or {}).get(code)
    rel = entry.get("strings") if isinstance(entry, dict) else None
    data: dict[str, str] | None = None
    if isinstance(rel, str):
        loaded = _load_json(_REPO_ROOT / rel)
        if loaded:
            data = {k: v for k, v in loaded.items() if isinstance(v, str) and not k.startswith("_")}
    with _lock:
        if data:
            _cache[code] = data
            _cache_failed_at.pop(code, None)
            return data
        # Negative-cache with TTL — retry after the window, never wedge
        # for process lifetime (/review litclock-dev#738 F4).
        _cache_failed_at[code] = now
        return {}


def _fill(template: str, slots: dict[str, Any]) -> str:
    for name, value in slots.items():
        template = template.replace("{" + name + "}", str(value))
    return template


def get(key: str, /, **slots: Any) -> str:
    """Resolve ``key`` in the active language; fall back en → key. Never raises."""
    code = active_language()
    template = _catalog(code).get(key)
    if template is None and code != CANONICAL_LANGUAGE:
        template = _catalog(CANONICAL_LANGUAGE).get(key)
        if template is not None:
            _warn_once(f"miss:{code}:{key}", "catalog key %r missing for %r; served English", key, code)
    if template is None:
        _warn_once(f"miss:en:{key}", "catalog key %r missing entirely; serving the key", key)
        template = key
    return _fill(template, slots)


def active_languages() -> dict[str, dict[str, Any]]:
    """Registry entries with ``status == "active"`` (litclock-dev#532 pickers)."""
    langs = _registry().get("languages") or {}
    return {
        code: entry
        for code, entry in langs.items()
        if isinstance(entry, dict) and entry.get("status") == "active"
    }


def active_codes() -> set[str]:
    codes = set(active_languages())
    # English is the fleet default and the canonical catalog — selectable
    # even if a hand-edited registry drops its entry.
    codes.add(CANONICAL_LANGUAGE)
    return codes


def negotiate(accept_language: str | None) -> str:
    """Best ACTIVE language for an Accept-Language header, else English.

    Deliberately small: split on commas, honor q= ordering, match on the
    PRIMARY subtag only (es-MX → es). The hotspot picker uses this for its
    DEFAULT selection — the user can always override, so mis-negotiation
    costs one tap, never a wrong persisted setting.
    """
    if not accept_language:
        return CANONICAL_LANGUAGE
    candidates: list[tuple[float, int, str]] = []
    for i, part in enumerate(accept_language.split(",")):
        piece = part.strip()
        if not piece:
            continue
        lang, _, params = piece.partition(";")
        # Unparseable q (absent, or shapes like q=banana / q=1.2.3) is
        # treated as the implicit 1.0 — generous beats dropping the user's
        # first-listed language (/review litclock-dev#742 pinned; the follow-up made
        # the two unparseable shapes consistent). q=0 means "not
        # acceptable" per RFC 9110 and is excluded.
        q = 1.0
        m = re.search(r"q\s*=\s*([0-9.]+)", params)
        if m:
            try:
                q = float(m.group(1))
            except ValueError:
                q = 1.0
        if q <= 0:
            continue
        primary = lang.strip().lower().split("-")[0]
        if primary and primary != "*":
            # i as tiebreak keeps header order stable for equal q.
            candidates.append((-q, i, primary))
    active = active_codes()
    for _q, _i, primary in sorted(candidates):
        if primary in active:
            return primary
    return CANONICAL_LANGUAGE


def get_triplet(prefix: str, /, **slots: Any) -> tuple[str, str, str]:
    """Resolve a splash triplet ``prefix.title/.message/.submessage``.

    The bash panel surfaces paint (title, message, submessage) triplets
    through ONE eink_display.py invocation; resolving all three inside
    that same process (``status --catalog-prefix``) keeps first-boot at
    one python spawn per splash — three ``catalog-get`` subprocesses per
    paint would add seconds on a Pi Zero 2W.

    Blank parts: the canonical-catalog gate forbids empty VALUES, so an
    intentionally blank part is expressed by the key's ABSENCE from the
    English catalog — absent-in-en means "this splash has no such part"
    and resolves to ``""`` (CI diffs every language against en, so a
    translation can't accidentally drop a part en carries). If the en
    catalog itself failed to load (corrupt bundle mid-OTA), absence
    proves nothing — fall back to ``get`` for every part, which serves
    keys (ugly-but-visible) rather than a blank splash."""
    en = _catalog(CANONICAL_LANGUAGE)
    keys = [f"{prefix}.{part}" for part in ("title", "message", "submessage")]
    # A prefix with NO part present in a loaded en catalog is an unknown
    # prefix (typo, partial OTA), not a design choice — serve the keys so
    # the panel shows something diagnosable instead of a BLANK frame that
    # a strict paint would latch as success (Codex slice-1 /review).
    known = any(k in en for k in keys)
    parts = []
    for key in keys:
        if not en or not known or key in en:
            parts.append(get(key, **slots))
        else:
            parts.append("")
    return (parts[0], parts[1], parts[2])


def get_many(keys: list[str] | tuple[str, ...]) -> dict[str, str]:
    """Resolve many keys with ONE language resolution and catalog fetch.

    The per-page injected-strings blob calls this (/review litclock-dev#739: the
    per-key ``get()`` path stats env.sh once per lookup — fine for four
    keys, wrong shape for the ~500-string extraction to come).
    """
    code = active_language()
    catalog = _catalog(code)
    fallback = _catalog(CANONICAL_LANGUAGE) if code != CANONICAL_LANGUAGE else catalog
    out: dict[str, str] = {}
    for key in keys:
        template = catalog.get(key)
        if template is None:
            template = fallback.get(key)
        if template is None:
            _warn_once(f"miss:en:{key}", "catalog key %r missing entirely; serving the key", key)
            template = key
        out[key] = template
    return out


def plural(base: str, n: int, **slots: Any) -> str:
    """Resolve ``<base>.<category>`` for ``n`` with ``n`` available as {n}.

    English-shaped selection (one/other); see the module docstring for the
    Stage-4 boundary.
    """
    category = "one" if n == 1 else "other"
    slots.setdefault("n", n)
    return get(f"{base}.{category}", **slots)


def reset_cache() -> None:
    """Test seam — drop memoized registry, catalogs, env-file state, warns."""
    global _registry_cache, _env_file_state, _registry_failed_at
    with _lock:
        _registry_cache = None
        _registry_failed_at = None
        _env_file_state = None
        _cache.clear()
        _cache_failed_at.clear()
        _warned.clear()
