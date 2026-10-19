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
take a screen down, and no lookup ever raises. Since litclock-dev#532
(R12/R15) a page or splash is never MIXED: a bundle is USABLE only if it
loads and holds every canonical (English) key, decided once per load. A
usable bundle serves every string; an unusable one is replaced WHOLE by the
registry's fleet default (English today) — one language per surface, never
the old per-key English fallback that produced half-translated pages. A key
missing from the language actually serving renders as the key itself.
Quote frames are not served from here and never fall back (R7/R1).

Failed and incomplete loads are negative-cached for a short TTL, not for
process lifetime (/review litclock-dev#738 F4: a transient read error during an OTA
window must heal itself — and an incomplete bundle is retried the same way,
litclock-dev#532 O3), and every warning fires once per distinct subject, not per lookup.

A surface resolves ONCE and reads every string from that resolution
(:func:`snapshot`), so a language save or a bundle recovering mid-render
cannot mix one page (litclock-dev#532 O4). The module-level :func:`get` and friends are
one-call snapshots. ``lang=`` asks for a specific language instead of the
device's (the setup page's session language, litclock-dev#532 D10).

Slots are filled in ONE pass that swaps each ``{name}`` for its value —
no format() machinery, so a stray brace in translated copy cannot raise,
and a slot value containing braces is never itself filled (a value is
inserted, never re-read).

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

import catalog_lint

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
_cache: dict[str, dict[str, str]] = {}  # COMPLETE bundles, process lifetime
# Loaded but not (yet) complete: (data, loaded_at, unjudged). Retried after the
# TTL — or at once, for an UNJUDGED one, as soon as the canonical bundle is in.
_incomplete: dict[str, tuple[dict[str, str], float, bool]] = {}
_cache_failed_at: dict[str, float] = {}
_registry_cache: dict[str, Any] | None = None
_registry_failed_at: float | None = None
_env_file_state: tuple[str, float, str] | None = None  # (path, mtime, value)
_warned: set[str] = set()


def warn_once(subject: str, fmt: str, *args: Any) -> None:
    """Log ``fmt`` once per distinct ``subject`` for the process (public since
    litclock-dev#870 — ``quote_corpus`` shares it; ``reset_cache`` clears the memory)."""
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
        warn_once(f"load:{path}", "strings catalog unreadable at %s: %s", path, exc)
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
    if loaded and not isinstance(loaded.get("languages"), dict):
        # Valid JSON of the wrong shape (`"languages": [...]`) is as unusable
        # as a parse error, and every reader below indexes it as a mapping —
        # treated as a failed load, it degrades instead of raising
        # (litclock-dev#532 review).
        warn_once(f"shape:{REGISTRY_PATH}", "language registry at %s has no languages mapping; ignored", REGISTRY_PATH)
        loaded = None
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
    code = configured_language()
    if code == CANONICAL_LANGUAGE:
        return code
    registry = _registry()
    entry = registry.get("languages", {}).get(code)
    if not isinstance(entry, dict) or entry.get("status") != "active":
        if not registry:
            # The registry failed to load, which _load_json already logged;
            # "not an active registry language" would misstate the cause, and
            # the painter refuses English in this case (quote_language).
            return CANONICAL_LANGUAGE
        warn_once(
            f"lang:{code}",
            "%s=%r is not an active registry language; using English",
            ENV_KEY,
            code,
        )
        return CANONICAL_LANGUAGE
    return code


def configured_language() -> str:
    """The code the device is SET to, before any degrading: the process env,
    then env.sh, then the fleet default. Unlike :func:`active_language` this
    does not consult the registry."""
    return (os.environ.get(ENV_KEY) or "").strip() or _language_from_env_file() or CANONICAL_LANGUAGE


def registry_readable() -> bool:
    """Whether ``languages.json`` loaded with a ``languages`` mapping."""
    return bool(_registry())


def quote_language() -> str | None:
    """The language whose QUOTES the panel may paint, or ``None`` when it
    cannot be known (litclock-dev#532 R7/R9).

    :func:`active_language` degrades an unknown or inactive code to English,
    which is right for the UI and right for the panel when a READABLE registry
    rejected the code. But when the registry itself cannot be read it degrades
    every code to English too, and a Spanish clock would then paint English
    quotes — the one thing R7 and R9 rule out. In that case there is no
    language to paint, and the painter shows the plain time as a failure.
    """
    # Checked BEFORE active_language(), whose "using English" warning would
    # otherwise land in the journal just ahead of the painter refusing English
    # — the opposite of what happened (round-2 review).
    if configured_language() != CANONICAL_LANGUAGE and not registry_readable():
        warn_once(
            "quote-lang:unreadable",
            "%s=%r but the language registry is unreadable; painting no quotes rather than English ones",
            ENV_KEY,
            configured_language(),
        )
        return None
    return active_language()


def _catalog(code: str) -> dict[str, str]:
    """The RAW bundle for ``code`` (``{}`` when it will not load), whether or
    not it is complete. Complete bundles are cached for the process; an
    incomplete one is re-read after the TTL (litclock-dev#532 O3), so a bundle repaired
    in place — an OTA landing, a hand fix — heals a running server."""
    now = time.monotonic()
    with _lock:
        if code in _cache:
            return _cache[code]
        held = _incomplete.get(code)
        if held is not None and now - held[1] < NEGATIVE_CACHE_TTL_S:
            # An unjudged bundle (read while the canonical one was unreadable)
            # is re-judged the moment the canonical bundle is cached, not 30s
            # later — until then the device would show the fallback for a
            # bundle that may be complete (litclock-dev#913 round-2 review).
            if not (held[2] and CANONICAL_LANGUAGE in _cache):
                return held[0]
        failed_at = _cache_failed_at.get(code)
        if failed_at is not None and now - failed_at < NEGATIVE_CACHE_TTL_S:
            return {}
    entry = _registry().get("languages", {}).get(code)
    rel = entry.get("strings") if isinstance(entry, dict) else None
    data: dict[str, str] | None = None
    if isinstance(rel, str):
        loaded = _load_json(_REPO_ROOT / rel)
        if loaded:
            data = {k: v for k, v in loaded.items() if isinstance(v, str) and not k.startswith("_")}
    if not data:
        with _lock:
            # Negative-cache with TTL — retry after the window, never wedge
            # for process lifetime (/review litclock-dev#738 F4).
            _cache_failed_at[code] = now
            _incomplete.pop(code, None)
        return {}
    # Completeness against the canonical key set, decided once per load. The
    # canonical bundle is complete by definition. A key whose value is blank
    # counts as missing: the translator kit exports every key, filled or not.
    # With NO canonical bundle loaded (an OTA window) completeness cannot be
    # judged, so the bundle is held for re-judging after the TTL rather than
    # cached as complete for the life of the process (litclock-dev#913 review — three
    # reviewers found it: an incomplete bundle loaded in that window was
    # served with raw keys until the next restart).
    missing: set[str] = set()
    unjudged = False
    if code != CANONICAL_LANGUAGE:
        canonical = _catalog(CANONICAL_LANGUAGE)
        unjudged = not canonical
        # Optional keys (the numbered farewell lines past the first, litclock-dev#532 T7)
        # are a language's own choice to supply, so never count as missing.
        required = {k for k in canonical if not catalog_lint.is_optional_key(k)}
        missing = required - {k for k, v in data.items() if v.strip()}
    with _lock:
        _cache_failed_at.pop(code, None)
        if not missing and not unjudged:
            _incomplete.pop(code, None)
            _cache[code] = data
            return data
        _incomplete[code] = (data, now, unjudged)
    if missing:
        warn_once(
            f"incomplete:{code}",
            "strings catalog for %r lacks %d canonical key(s) (e.g. %r); serving the fleet default whole",
            code,
            len(missing),
            sorted(missing)[0],
        )
    return data


def _usable(code: str) -> bool:
    """Loaded AND complete (see :func:`_catalog`)."""
    _catalog(code)
    with _lock:
        return code in _cache


def _fleet_default() -> str:
    """The registry's ``fleet_default`` when it names an ACTIVE language whose
    bundle is usable, else the canonical one — a fallback must not itself be
    an incubating or half-finished bundle (litclock-dev#913 review)."""
    fleet = _registry().get("fleet_default")
    if isinstance(fleet, str) and fleet != CANONICAL_LANGUAGE:
        entry = _registry().get("languages", {}).get(fleet)
        if isinstance(entry, dict) and entry.get("status") == "active" and _usable(fleet):
            return fleet
    return CANONICAL_LANGUAGE


class Snapshot:
    """One resolution of the catalog, to read a whole surface from.

    ``requested`` is the language asked for (the device's, or an explicit
    ``lang=``); ``language`` is the one actually serving — the same, or the
    fleet default when the requested bundle is unusable (litclock-dev#532 R12/R15)."""

    __slots__ = ("requested", "language", "usable", "_strings", "_schema")

    def __init__(self, requested: str, language: str, strings: dict[str, str], usable: bool) -> None:
        self.requested = requested
        self.language = language
        #: Whether the requested language's own bundle is serving AND is
        #: usable — False whenever a fallback (or a broken last resort) serves.
        self.usable = usable
        self._strings = strings
        # The triplet schema, captured WITH the strings so a cache transition
        # between resolution and use cannot pair two different states (litclock-dev#913
        # round-2 review). The canonical bundle when it is loaded; otherwise
        # the serving bundle stands in, so a deliberately absent part stays
        # blank rather than painting its raw key.
        self._schema = _catalog(CANONICAL_LANGUAGE) or strings

    def _template(self, key: str) -> str:
        template = self._strings.get(key)
        if template is None:
            lang = self.language
            warn_once(f"miss:{lang}:{key}", "catalog key %r missing for %r; serving the key", key, lang)
            return key
        return template

    def get(self, key: str, /, **slots: Any) -> str:
        return _fill(self._template(key), slots)

    def get_present(self, key: str, /, **slots: Any) -> str | None:
        """``key`` filled, or ``None`` when the serving bundle lacks it or
        holds it blank — for a caller with its own fallback, which must not
        paint a raw key. (The canonical bundle counts as usable by
        definition, so a TRUNCATED English bundle is "usable" and get()
        would serve the key: litclock-dev#919 review.)"""
        template = self._strings.get(key)
        if not isinstance(template, str) or not template.strip():
            return None
        return _fill(template, slots)

    def numbered(self, prefix: str, /) -> list[str]:
        """Every ``prefix.N`` this resolution holds, non-blank, in number
        order — a numbered optional set such as the farewell lines (litclock-dev#532 T7),
        which each language fills to its own count."""
        found = []
        for key, value in self._strings.items():
            head, _, tail = key.rpartition(".")
            if head == prefix and tail.isdigit() and isinstance(value, str) and value.strip():
                found.append((int(tail), value))
        return [value for _n, value in sorted(found)]

    def get_many(self, keys: list[str] | tuple[str, ...]) -> dict[str, str]:
        """Templates for ``keys``, UNFILLED — the injected-strings blob the
        PWA's JS fills itself."""
        return {key: self._template(key) for key in keys}

    def plural(self, base: str, n: int, **slots: Any) -> str:
        """``<base>.<category>`` for ``n``, with ``n`` available as {n}.
        English-shaped selection (one/other); see the module docstring."""
        category = "one" if n == 1 else "other"
        slots.setdefault("n", n)
        return self.get(f"{base}.{category}", **slots)

    def get_triplet(self, prefix: str, /, **slots: Any) -> tuple[str, str, str]:
        """A splash triplet ``prefix.title/.message/.submessage``.

        The bash panel surfaces paint (title, message, submessage) triplets
        through ONE eink_display.py invocation; resolving all three inside
        that same process (``status --catalog-prefix``) keeps first-boot at
        one python spawn per splash.

        Blank parts: the canonical-catalog gate forbids empty VALUES, so an
        intentionally blank part is expressed by the key's ABSENCE from the
        canonical catalog — absent-there means "this splash has no such part"
        and resolves to ``""``. If the canonical catalog itself failed to load
        (corrupt bundle mid-OTA), absence proves nothing — every part is
        served, which shows keys (ugly-but-visible) rather than a blank
        splash. A prefix with NO part present in a loaded canonical catalog
        is an unknown prefix (typo, partial OTA), not a design choice — the
        keys are served so the panel shows something diagnosable instead of
        a BLANK frame that a strict paint would latch as success (Codex
        slice-1 /review)."""
        schema = self._schema
        keys = [f"{prefix}.{part}" for part in ("title", "message", "submessage")]
        known = any(k in schema for k in keys)
        parts = [self.get(k, **slots) if (not schema or not known or k in schema) else "" for k in keys]
        return (parts[0], parts[1], parts[2])


def snapshot(lang: str | None = None) -> Snapshot:
    """Resolve once: the requested language's bundle when it is usable, else
    the fleet default's, WHOLE (litclock-dev#532 R12/R15). ``lang=None`` is the device
    language (:func:`active_language`); an explicit code must be an active
    one, anything else is treated as the fleet default."""
    requested = lang if lang is not None else active_language()
    if lang is not None and requested != CANONICAL_LANGUAGE and requested not in active_codes():
        requested = _fleet_default()
    if _usable(requested):
        return Snapshot(requested, requested, _catalog(requested), usable=True)
    fallback = _fleet_default()
    if fallback != requested and _catalog(fallback):
        warn_once(
            f"unusable:{requested}",
            "strings catalog for %r is unusable (missing or incomplete); serving %r whole",
            requested,
            fallback,
        )
        return Snapshot(requested, fallback, _catalog(fallback), usable=False)
    # No usable fallback either (the fleet default's own bundle is broken —
    # an OTA window): serve whatever the requested bundle loaded, which beats
    # every string rendering as its key; with nothing loaded, keys are the
    # documented never-raise floor.
    return Snapshot(requested, requested, _catalog(requested), usable=False)


def get_own(key: str, /, **slots: Any) -> str | None:
    """``key`` from the device language's OWN bundle, or ``None`` — never
    another language's. ``None`` too when that bundle is unusable: an
    optional screen part is omitted, not served in the fleet default or
    pulled from a half-complete bundle (D9, R15)."""
    snap = snapshot()
    if not snap.usable:
        return None
    template = snap._strings.get(key)
    return None if template is None else _fill(template, slots)


def catalog_size(code: str) -> int:
    """How many strings the loader serves for ``code`` — the app's view, not the file's.

    Public because ``scripts/update.sh``'s OTA smoke gate depends on it
    (through ``eink_display.py catalog-count``, litclock-dev#773 item 2): the
    count used to reach into ``_catalog()`` directly, and a rename of that
    private name would have printed ``0`` and failed every update closed
    (litclock-dev#840). Measured AFTER ``_catalog``'s filters — ``_``-prefixed
    keys and non-string values are dropped — so it counts what ``get()`` can
    actually resolve. ``0`` for a code the registry does not list, or whose
    bundle is missing or unparseable; never raises for those.
    Callers wanting the device's language resolve it with
    :func:`active_language` first, which is what ``catalog-count`` does.

    RAW on purpose (litclock-dev#532 Section 2 #2): never through :func:`snapshot`, or a
    broken bundle would report the fleet default's count and pass the OTA
    gate it exists to fail. An incomplete bundle reports its own count.
    """
    return len(_catalog(code))


_SLOT_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")


def _fill(template: str, slots: dict[str, Any]) -> str:
    # ONE pass over the template (litclock-dev#532 T6 review): replacing slot
    # by slot let a value that itself reads like a later slot be filled again
    # — a custom setup SSID "My{ip_url}Net" painted a different network name
    # in the steps than the QR and credentials carried. A name with no slot
    # value stays as written, as it always did.
    if not slots:
        return template
    return _SLOT_RE.sub(lambda m: str(slots[m.group(1)]) if m.group(1) in slots else m.group(0), template)


def get(key: str, /, *, lang: str | None = None, **slots: Any) -> str:
    """Resolve ``key`` (see :func:`snapshot`). Never raises. ``lang`` is
    keyword-only and therefore not a usable slot name."""
    return snapshot(lang).get(key, **slots)


def corpus_relpath(code: str) -> str | None:
    """The registry's ``corpus.path`` for ``code`` (relative to the repo root),
    or ``None`` when the code is unlisted or the entry has no usable path.
    Public because ``quote_corpus.corpus_path`` resolves the quote corpus
    through it (litclock-dev#870); the precedence and degrade order live there.
    """
    entry = _registry().get("languages", {}).get(code)
    if not isinstance(entry, dict):
        return None
    corpus = entry.get("corpus")
    rel = corpus.get("path") if isinstance(corpus, dict) else None
    return rel if isinstance(rel, str) and rel else None


def active_languages() -> dict[str, dict[str, Any]]:
    """Registry entries with ``status == "active"`` (litclock-dev#532 pickers)."""
    langs = _registry().get("languages", {})
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


def get_triplet(prefix: str, /, *, lang: str | None = None, **slots: Any) -> tuple[str, str, str]:
    """A splash triplet from ONE resolution (:meth:`Snapshot.get_triplet`)."""
    return snapshot(lang).get_triplet(prefix, **slots)


def get_many(keys: list[str] | tuple[str, ...], *, lang: str | None = None) -> dict[str, str]:
    """Many keys from ONE resolution — the per-page injected-strings blob
    (/review litclock-dev#739: the per-key path stats env.sh once per lookup)."""
    return snapshot(lang).get_many(keys)


def plural(base: str, n: int, *, lang: str | None = None, **slots: Any) -> str:
    """``<base>.<category>`` for ``n`` (:meth:`Snapshot.plural`)."""
    return snapshot(lang).plural(base, n, **slots)


def reset_cache() -> None:
    """Test seam — drop memoized registry, catalogs, env-file state, warns."""
    global _registry_cache, _env_file_state, _registry_failed_at
    with _lock:
        _registry_cache = None
        _registry_failed_at = None
        _env_file_state = None
        _cache.clear()
        _incomplete.clear()
        _cache_failed_at.clear()
        _warned.clear()
