"""Cached subprocess helper for control_server consumers (#416 / design C2=A).

Factored out of ``routes/status.py:_cached_subprocess`` so /api/status,
/api/diagnostics, and any future route that shells out for a cheap fact
(``nmcli``, ``timedatectl``, ``uname``, journalctl, etc.) share one
implementation. Status keeps its existing 5-second TTL via the default;
Diagnostics passes ``ttl=20`` for steady-state cache warmth on the cold-
cache 30s-poll cycle (per /plan-eng-review C2=A).

Two return-type contracts (#428 PR1a):

- :func:`cached_subprocess` — returns ``str`` on success (rc=0), ``""``
  on non-zero exit (a SUCCESS that produced no stdout), and ``None`` on
  subprocess failure (timeout / missing binary / SubprocessError). The
  ``None`` distinction lets classifier callers (anomaly logic) tell
  "binary ran fine, produced nothing" from "binary couldn't run."
- Failure cache window (#428 PR1b /plan-eng-review P-2): success entries
  use the caller's ``ttl``; failure entries (``None``) use
  ``min(ttl, FAILURE_TTL_CAP_S)`` (5s). A wedged binary that timed
  out yesterday doesn't keep poisoning a 20s diagnostics cache; recovery
  is within ~5s once the binary unwedges.
- :func:`cached_subprocess_or_empty` — convenience wrapper for display
  callers (``_network.py:read_ssid``/``read_default_route``/
  ``read_signal_dbm``, ``_collectors.py``'s readers) that immediately
  ``.splitlines()``/``.strip()`` the result. Coerces ``None`` to ``""``
  so the existing string-method-then-truthy-guard idiom keeps working.

Fixed argv (no shell). Caller passes the full command. ``timeout`` is
the per-call subprocess timeout (default 2s — short enough that a wedged
nmcli on a degraded WiFi can't stall the request handler). ``ttl`` is
the cache TTL in seconds. Same ``key`` shares its entry regardless of
``argv`` — callers are responsible for picking unique keys per logical
call site.

The module-level cache is intentional. Waitress is threaded; each thread
shares the dict (Python's GIL serializes dict mutations enough for our
use). Test setup can call :func:`clear_cache` to start with a clean slate.
"""

from __future__ import annotations

import subprocess
import threading
import time as _time
from collections import OrderedDict

# Default TTL preserves the pre-extraction behavior. Status callers don't
# need to opt in; diagnostics passes ttl=20 explicitly.
DEFAULT_TTL_S = 5.0
DEFAULT_TIMEOUT_S = 2.0

# Short cache window for subprocess failures (#428 PR1b /plan-eng-review P-2).
# A wedged binary that times out at ``timeout`` seconds would otherwise pin
# the failure (cached as ``None``) for the caller's full ``ttl`` (20s for
# diagnostics), keeping the next 6-7 poll cycles from re-trying. 5s strikes
# the trade: long enough to debounce a single hammer (~6 hits/min worst
# case if the binary stays wedged), short enough that the user sees fresh
# state within one or two polls after the binary recovers. Applied as
# ``min(ttl, FAILURE_TTL_CAP_S)`` so a caller that wanted instant
# refresh (ttl < 5s) keeps their tighter cadence on failures too.
FAILURE_TTL_CAP_S = 5.0

# Module-level cache, shared across threads. Bounded with LRU eviction
# (#416 PR1 /review ASK-5=A) so a future caller varying the key per call
# (e.g. embedding a hostname) can't leak entries across the process
# lifetime. Today's known callers use 3-4 fixed string literals, well
# under the cap; the cap is a hard invariant, not a tuning parameter.
#
# Mutations (PR1 adversarial pass): the dict-level GIL atomicity that
# protects single ``__setitem__`` calls does NOT protect the compound
# get-then-check-then-popitem-then-assign sequence below. Two threads
# racing on different new keys could over-evict (each pops a different
# oldest), and a hit racing with a popitem could KeyError on move_to_end.
# Hold ``_cache_lock`` for the entire compound mutation. The subprocess
# call stays OUTSIDE the lock so a slow shell-out doesn't serialise the
# whole cache.
#
# Entry value is ``str | None`` (#428 PR1a): ``None`` is the cached
# representation of a subprocess failure, distinct from ``""`` (the
# cached representation of a successful run that produced empty stdout).
MAX_CACHE_ENTRIES = 64
_cache: OrderedDict[str, tuple[float, str | None]] = OrderedDict()
_cache_lock = threading.Lock()


def cached_subprocess(
    key: str,
    argv: list[str],
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    ttl: float = DEFAULT_TTL_S,
) -> str | None:
    """Run ``argv`` with a TTL cache.

    Returns:
        - ``stdout.strip()`` on rc=0 (success)
        - ``""`` on non-zero exit (the binary ran but signaled non-success)
        - ``None`` on subprocess failure (``TimeoutExpired``, missing
          binary, ``SubprocessError``). Cached for
          ``min(ttl, FAILURE_TTL_CAP_S)`` so a transient failure
          rotates out of the cache within ~5s on diagnostics (which
          passes ``ttl=20``) and at the caller's natural ``ttl`` on
          shorter-cadence routes.

    Callers that don't care about the failure distinction should use
    :func:`cached_subprocess_or_empty`, which coerces ``None`` to ``""``.
    The short failure-cache window still applies — display callers also
    benefit from quicker recovery once a wedged binary unwedges.

    The cache is bounded at :data:`MAX_CACHE_ENTRIES` (#416 PR1 /review
    ASK-5=A) with LRU eviction — a cache miss on a full cache evicts the
    least-recently-used entry before inserting. Cache hits on the warm
    path call ``move_to_end`` to refresh the LRU position.

    Thread-safety: the compound get/move/popitem/assign sequence runs
    under :data:`_cache_lock`. The subprocess call is OUTSIDE the lock so
    a slow ``nmcli``/``systemctl`` doesn't serialise every other lookup
    behind the in-flight call.
    """
    now = _time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            # #428 PR1b (P-2): failure entries (cached ``None``) expire at
            # ``min(ttl, FAILURE_TTL_CAP_S)`` instead of the caller's
            # ``ttl``. Diagnostics (ttl=20s) sees failures rotate every 5s
            # so a transient wedged binary stops poisoning the page for
            # 20s; Status (ttl=5s) is unchanged because min(5, 5) == 5;
            # a hypothetical fast-poll caller (ttl=1s) keeps their tighter
            # cadence because min(1, 5) == 1.
            cached_ttl = min(ttl, FAILURE_TTL_CAP_S) if hit[1] is None else ttl
            if (now - hit[0]) < cached_ttl:
                # Refresh LRU position so the warm entry isn't the next eviction.
                _cache.move_to_end(key)
                return hit[1]
    out: str | None
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        out = result.stdout.strip() if result.returncode == 0 else ""
    except (FileNotFoundError, subprocess.SubprocessError):
        # #428 PR1a: distinguish "binary couldn't run" (None) from
        # "binary ran but produced no stdout" ("") so classifier callers
        # can tell a transient failure from a steady-state empty result.
        out = None
    # #428 PR1b /review ADV-1 (cross-model Claude+Codex): capture the
    # write-time monotonic AFTER subprocess.run returns. The pre-call
    # ``now`` (line 120) is correct for the cache-hit window check (it
    # represents the caller's request arrival), but using it for the
    # write timestamp would make the entry "born stale" whenever the
    # subprocess takes longer than the failure-TTL cap. journalctl's
    # 8s timeout (>5s FAILURE_TTL_CAP_S) is the concrete production
    # case: pre-fix, a wedged journalctl would cache (T=0, None) at
    # T=8 → every subsequent caller saw (now - 0) > 5s → cache miss
    # → re-fork the 8s timeout → potential waitress thread exhaustion.
    # Capturing write-time monotonic means the entry's clock starts
    # from when the result was actually observed.
    written_at = _time.monotonic()
    with _cache_lock:
        # Cap with LRU eviction: drop the oldest entry on overflow. A stale
        # entry for `key` already in the cache is overwritten in place (the
        # ``_cache[key] = ...`` below); only a NEW key triggers eviction.
        if key not in _cache and len(_cache) >= MAX_CACHE_ENTRIES:
            _cache.popitem(last=False)
        _cache[key] = (written_at, out)
        _cache.move_to_end(key)
    return out


def cached_subprocess_or_empty(
    key: str,
    argv: list[str],
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    ttl: float = DEFAULT_TTL_S,
) -> str:
    """Convenience wrapper for display callers that treat subprocess
    failure as 'binary produced no stdout'.

    Coerces :func:`cached_subprocess`'s ``None`` (failure) to ``""``.
    Use at the 8+ display call sites (``_network.py:read_ssid`` etc.,
    ``_collectors.py``'s readers that immediately ``.splitlines()``/
    ``.strip()`` the result). Use raw :func:`cached_subprocess` in
    classifier callers where the ``None`` vs ``""`` distinction matters
    (#428 PR1b will branch the anomaly classifier on that distinction).

    Per #428 PR1a /plan-eng-review CQ-1: one helper at the boundary is
    DRY-positive vs ``cached_subprocess(...) or ""`` repeated at every
    site, and the name reads as a contract ("I accept failure as empty").
    """
    result = cached_subprocess(key, argv, timeout=timeout, ttl=ttl)
    return result if result is not None else ""


def clear_cache() -> None:
    """Drop all cached entries. Tests call this between cases so a stale
    entry from one test doesn't bleed into another."""
    with _cache_lock:
        _cache.clear()
