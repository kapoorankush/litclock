"""Lazy in-memory index of the quote corpus — the ONE runtime corpus walk.

The PHP image generator (`image-gen/quote_to_image.php`) bakes quote text +
attribution into the PNGs and names them ``quote_{HHMM}_{idx}_credits.png``
where ``idx`` is the per-time-bucket position in the source CSV. This
module gives us the inverse — given an image filename, return
``{quote, author, title, time}`` so the PWA hero card can render the quote
in HTML rather than read text out of a baked PNG — and, since litclock-dev#590,
the forward direction too: ``bucket_entries()`` feeds the runtime
renderer's per-minute row selection (``quote_renderer.rows_for_time``).

Which CSV (litclock-dev#870): the forward direction reads the ACTIVE
LANGUAGE's corpus from ``languages.json`` (``corpus_path``); the inverse
reads the corpus the PNGs were baked from — the shipped default the PHP
generator opens by name (``image_corpus_path``). ``LITCLOCK_CORPUS_CSV``
overrides both.

Both directions share one walk, one cache, and ONE text pipeline
(``preprocess_quote``): the eng review of litclock-dev#590 found the previous
split — ``preprocess_quote`` on the e-ink path vs a local outer-quote
strip here — showed different text on the glass than in the PWA for 135
of 4,808 rows. The renderer's semantics won (decision D4): what the PWA
shows now matches the panel byte-for-byte.

Row-walk semantics mirror the PHP main loop exactly (and therefore
``quote_renderer.iter_corpus``): rows with <5 fields are skipped, the
bucket key is ``time[:2]+time[3:5]`` with NO shape validation, and
``idx`` resets on key change / increments on repeat. NSFW rows count
toward the same counter. Quote text stays RAW in the index and is
``preprocess_quote``-d at access time — eagerly transforming all ~4,800
rows would cost ~0.4s per fresh process to serve the 2-3 rows a lookup
actually touches.

Loaded once at first lookup; ~4MB resident per index (measured), so the
lru_cache's 4 slots bound a hypothetical long-lived importer at ~16MB.
Today there is NO long-lived consumer — the control_server reads the
status file literary_clock publishes, and the only importer is the
fresh per-minute clock process, whose cache dies with it. The cache key
still includes the CSV's (realpath, mtime, size) so any future
long-lived adopter inherits ``corpus_edit ship`` auto-invalidation for
free. Residual (documented, accepted): a stat-then-open race or an
mtime-and-size-preserving in-place rewrite can serve a stale index to
such an adopter until the next key change; the per-minute process is
immune (fresh cache each run).
"""

from __future__ import annotations

import csv
import logging
import os
import re
from functools import lru_cache
from pathlib import Path

import strings_catalog  # acyclic: strings_catalog imports nothing from this package

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
# The corpus the repo ships AND the one the PHP generator reads by name
# (`image-gen/quote_to_image.php` opens `litclock_annotated.csv` directly, and
# `scripts/download_images.sh` verifies the image set against the same file).
# It is therefore the provenance of every PNG under images/ — which is why
# image_corpus_path() returns it — and the LAST rung of corpus_path(), never
# the first (litclock-dev#870).
_DEFAULT_CORPUS_PATH = _PROJECT_ROOT / "image-gen" / "litclock_annotated.csv"
# Highest-precedence override for BOTH resolvers: tooling and tests point it at
# a synthetic CSV (`monkeypatch.setattr(quote_corpus, "_CORPUS_PATH", ...)` is
# the established seam). ``None`` means "resolve through the registry". It
# says "this file is the whole corpus, images included", so it is NOT the way
# to bench-test a translation — that redirects the PNG metadata too (litclock-dev#874
# review); register the translation in languages.json and set LITCLOCK_LANGUAGE.
_CORPUS_PATH: Path | None = Path(os.environ["LITCLOCK_CORPUS_CSV"]) if os.environ.get("LITCLOCK_CORPUS_CSV") else None


def _registry_corpus(code: str) -> Path | None:
    """``languages.json[code].corpus.path`` as a usable file inside the
    checkout, else ``None`` — with one warning per code per PROCESS for each
    way it can be unusable, because each is a packaging error the clock must
    paint through. (The painter is a fresh process every minute, so on a
    device with a broken registry entry that is one line per paint — the
    rate every other painter warning already has; the dedup is for
    long-lived importers.) Contained on purpose (litclock-dev#874 review): ``root / rel`` discards the
    root for an absolute ``rel`` and follows ``..`` and symlinks, so a
    registry typo could name any pi-readable file and serve its lines as
    quote text; a resolved path outside the checkout is refused. A zero-byte
    file is refused too, so an empty translation cannot shadow a healthy
    English corpus. A file that exists but does not parse is NOT caught here
    (that costs a full read per minute) — it falls to the PNG tier exactly
    as a corrupt English corpus always has.
    """
    rel = strings_catalog.corpus_relpath(code)
    if not rel:
        return None
    candidate = _PROJECT_ROOT / rel
    try:
        # Inside one guard: resolve() and is_file() raise on EACCES rather than
        # answering False, resolve() raises RuntimeError on a symlink loop and
        # ValueError on a NUL byte in the registry string (litclock-dev#874 review, red
        # team) — and the painter must never die here.
        root = _PROJECT_ROOT.resolve()
        real = candidate.resolve()
        if not real.is_relative_to(root):
            strings_catalog.warn_once(
                f"corpus-escape:{code}",
                "languages.json corpus %r for %r resolves outside the checkout; ignored",
                rel,
                code,
            )
            return None
        usable = real.is_file() and real.stat().st_size > 0
    except (OSError, RuntimeError, ValueError):
        usable = False
    if not usable:
        strings_catalog.warn_once(
            f"corpus-missing:{code}", "languages.json names corpus %r for %r but it is missing or empty", rel, code
        )
        return None
    # The plain join, not the resolved path: both accessors then spell a path
    # the same way (_index realpaths for the cache key regardless).
    return candidate


def corpus_path() -> Path:
    """The corpus the RUNTIME RENDERER reads: the active language's registry
    corpus (litclock-dev#870, unblocking litclock-dev#532 Stage 4).

    Precedence: the ``LITCLOCK_CORPUS_CSV`` override -> ``languages.json``'s
    ``corpus.path`` for ``strings_catalog.active_language()`` -> the English
    registry corpus -> the shipped default. Degrades the way the strings
    catalog does: an unknown or inactive code is already English by the time
    it reaches here, and a registry entry that is missing, empty or outside
    the checkout falls through with a warning (once per process — see
    ``_registry_corpus``). A clock must never stop painting over a registry
    typo.

    Resolved on EVERY call, on purpose: the index cache is keyed by path, so
    a language change reaches any long-lived importer on its next lookup —
    the ``lru_cache`` trap the issue named, where a module-level resolution
    would pin the first language's corpus for the life of the process. (No
    such importer exists today — see the module docstring — and the registry
    itself is cached for process lifetime by ``strings_catalog``, so a
    registry EDIT, as opposed to a language change, needs a restart there.)
    """
    if _CORPUS_PATH is not None:
        return _CORPUS_PATH
    code = strings_catalog.active_language()
    for candidate_code in dict.fromkeys((code, strings_catalog.CANONICAL_LANGUAGE)):
        resolved = _registry_corpus(candidate_code)
        if resolved is not None:
            return resolved
    return _DEFAULT_CORPUS_PATH


def image_corpus_path() -> Path:
    """The corpus the PRE-RENDERED PNGs were baked from: the shipped default,
    whatever the device language and whatever the registry says (litclock-dev#874 review).

    ``lookup_by_filename`` inverts the PHP namer, and the namer walked ONE
    file, by name — ``image-gen/litclock_annotated.csv`` — so that file is the
    provenance of every PNG under ``images/``, not any registry entry: a
    registry that repointed English's ``corpus.path`` must not move the PNG
    metadata off the images it describes. ``images/`` has no language
    dimension, so a device on a second language while still on the PNG tier
    paints these PNGs and gets their metadata from here, not from its own
    corpus. (That such a device paints the wrong language at all is why
    runtime render is the multilingual enabler, litclock-dev#871.)
    """
    return _CORPUS_PATH if _CORPUS_PATH is not None else _DEFAULT_CORPUS_PATH


# Image filename forms generated by quote_to_image.php:
#   quote_{HHMM}_{idx}.png                       (image)
#   quote_{HHMM}_{idx}_credits.png               (metadata variant)
#   quote_{HHMM}_{idx}_nsfw.png                  (NSFW image)
#   quote_{HHMM}_{idx}_nsfw_credits.png          (NSFW metadata variant)
_FILENAME_RE = re.compile(r"^quote_(?P<hhmm>\d{4})_(?P<idx>\d+)(?P<nsfw>_nsfw)?(?:_credits)?\.png$")


# PHP defaults the text pipeline must mirror exactly (moved here from
# quote_renderer in litclock-dev#590 so the corpus walk and its text semantics
# live in ONE module; quote_renderer re-exports them):
# trim() strips " \t\n\r\0\x0B"; preg_replace('/\s+/') without /u is the
# ASCII class [ \t\n\x0B\f\r] (Python's \s would be Unicode-wide).
PHP_TRIM_CHARS = " \t\n\r\0\x0b"
_PHP_WS_RE = re.compile(r"[ \t\n\x0b\f\r]+")

# The escape-sequence cleanup chain from quote_to_image.php, in order.
# Literal backslash counts (4/2/1 before n, 5/3/1 before ") are built
# programmatically — they were verified by eval'ing the PHP source literals,
# and the PHP comments themselves miscount one of them.
_BS = "\\"
_ESCAPE_CHAIN = (
    (_BS * 4 + "n", " "),
    (_BS * 2 + "n", " "),
    (_BS + "n", " "),
    (_BS * 5 + '"', '"'),
    (_BS * 3 + '"', '"'),
    (_BS + '"', '"'),
)


def preprocess_quote(raw: str) -> str:
    """The production escape collapse: quote_to_image.php's six-replace
    chain, then ASCII whitespace collapse, then PHP trim."""
    for src, dst in _ESCAPE_CHAIN:
        raw = raw.replace(src, dst)
    return _PHP_WS_RE.sub(" ", raw).strip(PHP_TRIM_CHARS)


@lru_cache(maxsize=4)
def _index_for(path_str: str, mtime_ns: int, size: int) -> dict[str, list[dict]]:
    """Build the corpus index: ``HHMM → [entry, ...]`` in bucket order.

    Mirrors PHP's per-time-bucket counter in `quote_to_image.php`: the
    `imagenumber` resets to 0 when the time field changes and increments
    otherwise. Each entry carries its filename ``idx`` explicitly — for a
    key that reappears NON-contiguously the counter resets mid-bucket
    (PHP would overwrite the earlier file), so list position alone would
    lie. Contiguous on the shipped corpus; the walk mirrors the namer
    regardless. NSFW rows count toward the same counter (they get a
    `_nsfw` suffix in the filename but share the bucket index).

    Each entry keeps ``quote_raw`` untransformed — callers apply
    ``preprocess_quote`` to the rows they actually use (see module
    docstring for why). ``ordinal`` is the 1-based renderable-row number,
    matching ``quote_renderer.iter_corpus``.

    Caching is keyed by (path, mtime_ns), not argument identity, so a
    `corpus_edit ship` (which rewrites the CSV) auto-invalidates the
    cache without restarting control_server (adversarial /review on M2
    caught this — long-running control_server would serve stale
    attribution after a corpus edit until restart). lru_cache(maxsize=4)
    keeps the last few (path, mtime) pairs so a brief mtime flap doesn't
    thrash the parser; the path in the key also lets tests point at tmp
    corpora without evicting the production index.
    """
    index: dict[str, list[dict]] = {}
    path = Path(path_str)
    if not path.exists():
        return index

    previous_hhmm: str | None = None
    image_number = 0
    ordinal = 0

    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f, delimiter="|")
        for row in reader:
            if len(row) < 5:
                continue
            ordinal += 1
            time_str = row[0]
            # PHP substr semantics: derive the key with NO shape check.
            # (The pre-litclock-dev#590 index skipped malformed times, silently
            # desyncing idx from the PHP filename counter for every later
            # row in that bucket. Dormant on the shipped corpus — every
            # row is well-formed — but the walk must mirror the namer.
            # Known residual: PHP substr slices BYTES, Python slices code
            # points, so a multibyte time field would diverge — dormant,
            # pre-existing, and identical in iter_corpus, so the two
            # Python walks can never disagree with each other over it.)
            hhmm = time_str[:2] + time_str[3:5]
            if hhmm == previous_hhmm:
                image_number += 1
            else:
                image_number = 0
                previous_hhmm = hhmm
            index.setdefault(hhmm, []).append(
                {
                    "ordinal": ordinal,
                    "idx": image_number,
                    "time": time_str,
                    "timestring": row[1].strip(PHP_TRIM_CHARS),
                    "quote_raw": row[2],
                    "title": row[3].strip(PHP_TRIM_CHARS),
                    "author": row[4].strip(PHP_TRIM_CHARS),
                    "is_nsfw": len(row) > 5 and row[5].strip(PHP_TRIM_CHARS).upper() == "YES",
                }
            )
    return index


def _current_stat(path: Path) -> tuple[int, int]:
    """Return the CSV's (mtime_ns, size), or (0, 0) if it doesn't exist.
    Both join the lru_cache key so the index auto-invalidates on corpus
    edit — size catches the mtime-granularity edge where a rewrite lands
    in the same timestamp tick. Per-call stat is microseconds — much
    cheaper than CSV re-parse (~100-300ms on Pi Zero)."""
    try:
        st = path.stat()
        return st.st_mtime_ns, st.st_size
    except OSError:
        return 0, 0


def _index(csv_path: str | os.PathLike | None = None) -> dict[str, list[dict]]:
    path = corpus_path() if csv_path is None else Path(csv_path)
    # realpath: symlinked installs and env-var overrides must key ONE
    # cache entry per underlying file, not one per spelling of its path.
    path = Path(os.path.realpath(path))
    mtime_ns, size = _current_stat(path)
    return _index_for(str(path), mtime_ns, size)


def bucket_entries(hhmm: str, csv_path: str | os.PathLike | None = None) -> tuple[dict, ...]:
    """All corpus entries for one HHMM bucket, in file order (the runtime
    selection pool — ``quote_renderer.rows_for_time`` builds its
    ``CorpusRow`` objects from this). Entries carry ``quote_raw``; apply
    ``preprocess_quote`` to the rows actually used. ``csv_path=None``
    means ``corpus_path()`` — the active language's corpus (litclock-dev#870).

    Returns a tuple so callers cannot reorder/extend the cached bucket;
    the entry dicts themselves are still the SHARED cached objects —
    treat them as immutable (mutating one poisons every later caller in
    this process until the cache key changes)."""
    return tuple(_index(csv_path).get(hhmm, []))


def lookup_by_filename(filename: str) -> dict[str, str] | None:
    """Look up quote metadata by image filename. Returns ``None`` if not
    found (corpus missing, malformed name, or out-of-range idx).

    ``quote`` is the SAME text the runtime renderer draws
    (``preprocess_quote`` — litclock-dev#590 unified the pipelines; the old
    outer-quote strip showed different text in the PWA than on the glass
    for 135 rows). Matching idx entries resolve last-wins, mirroring PHP
    overwriting the earlier file when a bucket key reappears after a
    counter reset (impossible on a contiguous corpus, exact anyway).

    Basename identity is (hhmm, idx, nsfw-suffix) — all three, exactly as
    the PHP namer writes files. The nsfw check (litclock-dev#594 review, Codex +
    security convergence) guards the corpus/images desync window: without
    it, a tame filename could resolve to a row that is NSFW at that idx in
    a NEWER corpus, publishing mature text to a filtered device's PWA.
    Mismatch returns None — same refuse-on-desync posture as
    ``_write_status_file``; the PWA shows the stale banner instead."""
    name = os.path.basename(filename)
    m = _FILENAME_RE.match(name)
    if not m:
        return None
    hhmm = m.group("hhmm")
    idx = int(m.group("idx"))
    filename_is_nsfw = m.group("nsfw") is not None
    entry = None
    # The corpus the PNGs were baked from, never the device language's
    # (see image_corpus_path).
    for e in _index(image_corpus_path()).get(hhmm, []):
        if e["idx"] == idx:
            entry = e
    if entry is None or entry["is_nsfw"] != filename_is_nsfw:
        return None
    return {
        "time": entry["time"],
        "timestring": entry["timestring"],
        "quote": preprocess_quote(entry["quote_raw"]),
        "title": entry["title"],
        "author": entry["author"],
    }


def reset_cache() -> None:
    """Test hook — clears the lru_cache so each test sees a fresh index.
    Without this, tests that monkeypatch the corpus path will see the
    stale index from the first call. (The warn-once memory lives in
    ``strings_catalog``; its ``reset_cache`` clears that.)"""
    _index_for.cache_clear()
