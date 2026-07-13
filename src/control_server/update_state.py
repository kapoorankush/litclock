"""Shared state helpers for /api/update/* (#245 M5).

This module owns three plumbing concerns the route handlers in
``routes/updates.py`` share:

1. **GH-API + CHANGELOG cache** at ``/run/litclock/update-check.json``
   (D6, F11, F13). 6h TTL. Cache shape: ``{tag, fetched_at_unix, etag,
   release_notes}``. Atomic mv-tmp writes; corrupt-JSON readers refetch.
   update.sh Phase 7 invalidates by deleting the file (D6). Lives on tmpfs
   (#434): it's a purely derived 6h-TTL cache, so keeping it off the SD card
   costs nothing but a single refetch after a reboot clears it.
2. **`systemctl is-active` + `systemctl list-jobs` busy gate** for
   litclock-update.service (D5, F7). Returns True if the unit is in
   active|activating|deactivating|reloading OR has a queued job — covers
   the corner case where a Sunday timer fired 1ms before the user tapped
   Apply and systemd has the job queued but not yet running.
3. **Status file reader** for ``/run/litclock/update.status`` (D2, D9).
   Returns the parsed JSON or ``{"state": "idle"}`` if the file is
   missing / invalid.

Everything in this module is process-side helpers; the network-facing
contract lives in ``routes/updates.py``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger(__name__)

# Defaults — every constant overridable via env so tests can isolate.
# #434 — the update-check cache is a purely derived 6h-TTL blob, so it lives on
# the /run/litclock tmpfs (pi-owned, created at boot by tmpfiles.d) rather than
# the SD-backed /var/lib/litclock. A reboot clears it; the next /api/update/check
# just refetches once. The must-persist state (lkg-sha, update-failed, grace,
# last-update mirror) stays in /var/lib/litclock.
DEFAULT_CACHE_FILE: Final[Path] = Path("/run/litclock/update-check.json")
DEFAULT_STATUS_FILE: Final[Path] = Path("/run/litclock/update.status")
DEFAULT_CACHE_TTL_S: Final[int] = 6 * 60 * 60  # 6 hours
SYSTEMCTL_BIN: Final[str] = os.environ.get("LITCLOCK_SYSTEMCTL", "/usr/bin/systemctl")
UPDATE_UNIT: Final[str] = "litclock-update.service"
TAGS_URL_TEMPLATE: Final[str] = "https://api.github.com/repos/{owner}/{repo}/tags?per_page=100"
CHANGELOG_URL_TEMPLATE: Final[str] = "https://raw.githubusercontent.com/{owner}/{repo}/{tag}/CHANGELOG.md"
DEFAULT_OWNER: Final[str] = "kapoorankush"
DEFAULT_REPO: Final[str] = "litclock"
GH_API_TIMEOUT_S: Final[int] = 10
# GitHub's API requires every request to send a User-Agent header per
# https://docs.github.com/en/rest/overview/resources-in-the-rest-api#user-agent-required.
# Python's default `Python-urllib/3.x` works for /tags today but GH has
# historically returned 403 to unidentified clients. Pin our own value.
HTTP_USER_AGENT: Final[str] = "litclock-control-server (+https://github.com/kapoorankush/litclock)"
RELEASE_TAG_RE: Final[re.Pattern[str]] = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
# `git config credential.helper=store` writes one URL-per-line to this path
# in the form `https://user:token@github.com`. The bash side already reads
# this for the auto-update flow; the Python /api/update/check route mirrors
# it so private-repo Pis don't show "checking…" forever (caught in hardware
# QA on test Pi 2026-04-30 — public repos work without auth, private ones
# return 404 on /tags without it).
DEFAULT_GIT_CREDENTIALS: Final[Path] = Path.home() / ".git-credentials"
_GH_HOST_RE: Final[re.Pattern[str]] = re.compile(r"^https://[^:/\s]+:([^@\s]+)@github\.com(?:/.*)?$")

# #336 — DoS / hardening byte caps for status-file readers.
# These files are produced by jq on the bash side and are typically a few
# hundred bytes; a 1MB junk file appearing at any of these paths must NOT
# be loaded into memory or fed to json.load. The caps below are 1-2 orders
# of magnitude above the expected size — enough headroom for future field
# additions, but tight enough to reject pathological inputs.
MAX_STATUS_FILE_BYTES: Final[int] = 8192
MAX_LAST_UPDATE_FILE_BYTES: Final[int] = 8192
MAX_LKG_SHA_FILE_BYTES: Final[int] = 64
MAX_GH_API_CACHE_BYTES: Final[int] = 8192
# #342 I1 — release_notes byte cap applied BEFORE write_cache. Keeps the
# cached payload comfortably under MAX_GH_API_CACHE_BYTES (the read-side
# cap) regardless of how verbose a future CHANGELOG entry gets. Without
# this, a release with a long-form notes section (>~7KB after the rest
# of the payload's overhead) would write fine but read-side reject as
# oversize → /api/update/check refetches GitHub on every request →
# burns the PAT rate budget → "update unavailable" surfaces intermittently
# in the PWA. 4KB is ~10x the largest CHANGELOG entry shipped to date
# (v0.211.1's ~600 chars), well under the 8KB ceiling.
MAX_RELEASE_NOTES_BYTES: Final[int] = 4096


# ─── #336 shared bounded readers ───────────────────────────────────────────


# Open-flag bundle for safe_read_*. Kept at module scope so the rationale
# lives in one place and tests can reuse without re-deriving the bitmask.
# - O_RDONLY: read-only (we never mutate the path).
# - O_NOFOLLOW: rejects symlinks at open() — defeats symlink swap.
# - O_CLOEXEC: prevents fd leak to subprocesses spawned mid-read.
# - O_NONBLOCK: critical for FIFO safety. open(FIFO, O_RDONLY) without
#   O_NONBLOCK blocks indefinitely waiting for a writer. With O_NONBLOCK,
#   open returns immediately and the subsequent fstat S_ISREG check
#   rejects the FIFO. On regular files O_NONBLOCK is a no-op (no read
#   semantic change) — safe to leave set throughout the read.
_SAFE_READ_OPEN_FLAGS: Final[int] = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


def safe_read_json(path: Path, max_bytes: int) -> dict[str, Any] | None:
    """Bounded, type-safe JSON loader for tmpfs / persisted status files.

    Atomic open-then-fstat (review C1 / TOCTOU close). The earlier
    lstat-then-open pattern had a same-user race window: between
    ``os.lstat(path)`` returning a regular-file mode and ``open(path)``
    actually opening, a malicious / buggy process could swap the path for
    a FIFO. The subsequent ``open()`` would then block forever on the
    FIFO waiting for a writer, hanging waitress workers indefinitely.
    Closing that hole means the syscalls must inspect the SAME file
    descriptor — never re-resolve the path:

    1. ``os.open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK)``
       opens the path atomically. ``O_NOFOLLOW`` rejects symlinks at open
       time (no symlink-to-FIFO swap). ``O_CLOEXEC`` prevents fd leaks to
       subprocesses. ``O_NONBLOCK`` ensures the open returns immediately
       even if the path resolves to a FIFO (otherwise read-only FIFO
       open() blocks waiting for a writer — the exact hang we're closing).
    2. ``os.fstat(fd)`` — inspects the *opened* inode, not the path.
       ``S_ISREG`` rejects FIFOs / devices / dirs that opened ok past
       ``O_NOFOLLOW`` (FIFO opens succeed under O_NONBLOCK; the gate
       fires here). Size cap refuses oversize before any bytes are read.
    3. ``fh.read(max_bytes + 1)`` — read one extra byte. If the file grew
       between fstat and read (legitimate atomic mv-tmp from a producer
       writing a larger payload over the top), reject — the cap is a
       hard ceiling regardless of growth window.
    4. Any ``OSError`` or ``ValueError`` returns ``None`` so callers
       degrade gracefully — never raises.

    The fd is owned by ``os.fdopen`` once that call succeeds (the with-
    block closes it). The pre-fdopen path explicitly closes on error.
    """
    try:
        # os.open accepts both Path and str; str-coerce for portability.
        fd = os.open(str(path), _SAFE_READ_OPEN_FLAGS)
    except OSError:
        return None
    # From this point on the fd is owned by us. Any return path that does
    # NOT hand the fd to os.fdopen must close it explicitly.
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            return None
        if st.st_size > max_bytes:
            os.close(fd)
            return None
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        return None
    # os.fdopen takes ownership of the fd; the with-block closes it.
    # Do NOT os.close(fd) after this point — that would double-close.
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            # Read max_bytes+1 to defensively detect a grow-between-fstat-
            # and-read. UTF-8 length in bytes can exceed codepoint count, so
            # measure the bytes-encoded length against the cap, not len().
            data = fh.read(max_bytes + 1)
    except (OSError, ValueError):
        return None
    if len(data.encode("utf-8")) > max_bytes:
        return None
    try:
        parsed = json.loads(data)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def safe_read_text(path: Path, max_bytes: int) -> str | None:
    """Bounded text reader — sibling to ``safe_read_json`` for non-JSON files
    like ``/var/lib/litclock/lkg-sha`` (a single-line SHA). Same atomic
    open-then-fstat pattern (review C1) — see ``safe_read_json`` for the
    full TOCTOU rationale. Returns ``None`` on any failure.
    """
    try:
        fd = os.open(str(path), _SAFE_READ_OPEN_FLAGS)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            return None
        if st.st_size > max_bytes:
            os.close(fd)
            return None
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        return None
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            data = fh.read(max_bytes + 1)
    except (OSError, ValueError):
        return None
    if len(data.encode("utf-8")) > max_bytes:
        return None
    return data


# ─── GH-API auth (private-repo support) ────────────────────────────────────


def _gh_token_from_credentials(creds_file: Path | None = None) -> str | None:
    """Read a github.com auth token from `~/.git-credentials`.

    Mirrors the bash helper at scripts/lib/github_api.sh::_litclock_token_from_git_credentials.
    Returns the first token found for `github.com`, or None if the file is
    missing/unreadable/empty/has-no-github-line. Never raises — this is a
    best-effort path; auth failure should fall through to unauthenticated
    fetches (which work for public repos).
    """
    target = creds_file or Path(os.environ.get("LITCLOCK_GIT_CREDENTIALS", str(DEFAULT_GIT_CREDENTIALS)))
    try:
        with open(target, encoding="utf-8") as fh:
            for line in fh:
                m = _GH_HOST_RE.match(line.strip())
                if m:
                    return m.group(1)
    except (FileNotFoundError, PermissionError, OSError) as exc:
        logger.debug("git-credentials unreadable (%s); GH calls will be unauthenticated", exc)
    return None


def _gh_auth_header() -> dict[str, str]:
    """Build the Authorization header dict for GH API calls.

    Returns ``{"Authorization": "Bearer <token>"}`` when a token is
    available via env (GH_TOKEN, GITHUB_TOKEN) or `~/.git-credentials`,
    else returns an empty dict so callers can `.update()` it
    unconditionally.
    """
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        token = _gh_token_from_credentials()
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


# ─── GH-API cache (D6, F11, F13) ───────────────────────────────────────────


def cache_path() -> Path:
    """Resolve the cache path with env override for tests."""
    override = os.environ.get("LITCLOCK_UPDATE_CHECK_CACHE")
    return Path(override) if override else DEFAULT_CACHE_FILE


def status_path() -> Path:
    """Resolve the status-file path with env override for tests."""
    override = os.environ.get("LITCLOCK_UPDATE_STATUS_FILE")
    return Path(override) if override else DEFAULT_STATUS_FILE


def read_cache(cache_file: Path | None = None) -> dict[str, Any] | None:
    """Read + parse the cache file. Returns ``None`` if missing or corrupt.

    F13 — corrupt JSON tolerated: log a warning, return None so caller
    refetches and overwrites. NEVER raises.

    #336 — bounded via ``safe_read_json`` (8KB cap, lstat-rejects symlinks /
    FIFOs / dirs). A pathological 1MB cache file or a symlink swap can no
    longer pull garbage into memory or hang the route handler.
    """
    target = cache_file or cache_path()
    data = safe_read_json(target, MAX_GH_API_CACHE_BYTES)
    if data is None:
        # safe_read_json swallows the specific reason (file-not-found vs
        # parse error vs oversize); log at info to keep journal noise low —
        # the route handler will refetch + overwrite either way.
        logger.info("update-check cache not loadable; will refetch")
        return None
    return data


def cache_is_fresh(payload: dict[str, Any], ttl_s: int = DEFAULT_CACHE_TTL_S) -> bool:
    """True iff ``payload`` was fetched within the last ``ttl_s`` seconds."""
    fetched = payload.get("fetched_at_unix")
    if not isinstance(fetched, (int, float)):
        return False
    return (time.time() - fetched) < ttl_s


def write_cache(payload: dict[str, Any], cache_file: Path | None = None) -> bool:
    """Atomically write ``payload`` to the cache via mv-tmp (F11).

    Returns True on success, False on any IO error. Errors logged, never
    raised — a stale cache is acceptable degradation; an unhandled
    exception in the route handler is not.
    """
    target = cache_file or cache_path()
    parent = target.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("could not create cache parent dir %s: %s", parent, exc)
        return False
    # waitress runs threads=4 in a single process, so os.getpid() alone
    # would collide between worker threads racing to refresh the cache.
    # Suffix with thread id too so each refresh writes to its own tmp;
    # os.replace() at the end is atomic, so the final file is one of the
    # racing payloads (whichever wins the rename), never a torn mix.
    # /review caught this; see PR #284 review notes.
    tmp = target.with_suffix(target.suffix + f".tmp.{os.getpid()}.{threading.get_ident()}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            # #342 I1 follow-up (codex adversarial /review): ensure_ascii=False
            # so the on-disk JSON byte length matches the UTF-8 byte length we
            # capped release_notes against. The default ensure_ascii=True
            # inflates each non-ASCII codepoint to a \uXXXX escape (6 bytes for
            # BMP, 12 for surrogate pair), so a 4KB emoji-heavy notes section
            # serialises to ~12KB and blows past MAX_GH_API_CACHE_BYTES.
            # safe_read_json reads UTF-8, so non-ASCII chars survive the
            # round-trip unchanged.
            json.dump(payload, fh, separators=(",", ":"), ensure_ascii=False)
        os.replace(tmp, target)
        return True
    except OSError as exc:
        logger.warning("could not write update-check cache: %s", exc)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def fetch_latest_release_tag(
    owner: str = DEFAULT_OWNER,
    repo: str = DEFAULT_REPO,
    timeout_s: int = GH_API_TIMEOUT_S,
) -> str | None:
    """Resolve the highest-semver vX.Y.Z tag via /repos/{owner}/{repo}/tags.

    Returns the tag string on success, ``None`` on any failure (network,
    HTTP, parse, no candidates). Mirrors scripts/lib/github_api.sh's
    resolver — uses /tags rather than /releases/latest because of the
    long-standing GH PAT-vs-private-repo bug (issue #247).

    Auth resolution order (mirrors the bash helper): GH_TOKEN env var,
    GITHUB_TOKEN env var, then ``~/.git-credentials``. Public repos
    work with no auth at all; private repos need a token with
    Contents:Read. Caught in hardware QA on test Pi 2026-04-30 — the
    /api/update/check route was returning ``available=null`` forever
    because /tags 404'd without auth on a private repo.
    """
    url = TAGS_URL_TEMPLATE.format(owner=owner, repo=repo)
    headers = {
        "User-Agent": HTTP_USER_AGENT,
        "Accept": "application/vnd.github+json",
    }
    headers.update(_gh_auth_header())
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
            data = json.load(resp)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        logger.warning("GH tags fetch failed: %s", exc)
        return None
    if not isinstance(data, list):
        return None
    candidates: list[tuple[tuple[int, int, int], str]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name", "")
        if not isinstance(name, str):
            continue
        m = RELEASE_TAG_RE.match(name)
        if m:
            candidates.append((tuple(int(p) for p in m.groups()), name))  # type: ignore[arg-type]
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def fetch_release_notes(
    tag: str,
    owner: str = DEFAULT_OWNER,
    repo: str = DEFAULT_REPO,
    timeout_s: int = GH_API_TIMEOUT_S,
) -> str | None:
    """D13 — fetch CHANGELOG.md at ``tag`` from raw.githubusercontent.com.

    Sidesteps the /releases/* PAT-404 issue (#247) by reading the file
    directly from the public raw host. Returns the first 10 non-empty
    lines under the section heading for ``tag``, or None if no section
    matches / fetch fails.

    The CHANGELOG.md format is "Keep a Changelog"-shaped:
        ## [Unreleased]
        ## [v0.211.0] - 2026-04-30
        ### Added
        - thing
    We extract the first section header that matches the tag (allowing
    optional brackets and a trailing date).
    """
    url = CHANGELOG_URL_TEMPLATE.format(owner=owner, repo=repo, tag=tag)
    # raw.githubusercontent.com requires the same auth as api.github.com
    # for private repos — without it, the response is 404 (mirroring the
    # bash fetch behavior).
    headers = {"User-Agent": HTTP_USER_AGENT}
    headers.update(_gh_auth_header())
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
            body = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as exc:
        logger.info("CHANGELOG fetch for %s failed: %s", tag, exc)
        return None
    return _extract_changelog_section(body, tag)


def _extract_changelog_section(body: str, tag: str) -> str | None:
    """Extract bullet list under the heading matching ``tag``.

    Robust to the common 'Keep a Changelog' shapes:
        ## v0.211.0
        ## [v0.211.0]
        ## [v0.211.0] - 2026-04-30

    Returns up to the first 10 non-empty lines following the heading,
    stopping at the next ``## `` heading. Stripped of leading whitespace.
    """
    # Match the heading line for `tag`, optionally bracketed.
    pattern = re.compile(rf"^##\s+\[?{re.escape(tag)}\]?(\s+.*)?$", re.MULTILINE)
    match = pattern.search(body)
    if not match:
        return None
    after = body[match.end() :]
    lines: list[str] = []
    for raw in after.splitlines():
        if raw.startswith("## "):
            break
        stripped = raw.rstrip()
        if stripped:
            lines.append(stripped)
        if len(lines) >= 10:
            break
    if not lines:
        return None
    return "\n".join(lines)


def build_check_payload(current_version: str) -> dict[str, Any]:
    """Build a fresh /api/update/check payload by hitting GH + CHANGELOG.

    Returns a dict with the cache-friendly shape; caller writes it to
    the cache file. On total network failure, returns a minimal payload
    with ``available=None`` (unknown) so the PWA can render a graceful
    degraded state ("couldn't check — try again later") instead of a
    misleading "up to date" — /review caught this regressing the PWA's
    offline-detection contract: cache writes a fresh fetched_at_unix
    even when the fetch failed, so available=False would mask a 6-hour
    "we're offline" window as "we're up to date".
    """
    tag = fetch_latest_release_tag()
    notes = fetch_release_notes(tag) if tag else None
    # #342 I1 — cap release_notes byte length so write_cache always
    # produces a payload readable by the bounded read side. Truncate at
    # the last newline before the cap so we don't slice mid-bullet, then
    # fall back to a hard byte slice if no newline is within range.
    if notes is not None:
        encoded = notes.encode("utf-8")
        if len(encoded) > MAX_RELEASE_NOTES_BYTES:
            truncated_bytes = encoded[:MAX_RELEASE_NOTES_BYTES]
            try:
                truncated = truncated_bytes.decode("utf-8")
            except UnicodeDecodeError:
                # Slice landed mid-multibyte codepoint — back off until valid.
                truncated = truncated_bytes.decode("utf-8", errors="ignore")
            last_newline = truncated.rfind("\n")
            if last_newline > 0:
                truncated = truncated[:last_newline]
            notes = truncated.rstrip() + "\n…"
    if tag is None:
        # Network failure — distinct from "we're current". Caller (route
        # handler at routes/updates.py:190,204) treats `available is False`
        # as the only block; `None` falls through to letting the user try.
        available: bool | None = None
    else:
        available = not _version_matches(current_version, tag)
    return {
        "fetched_at_unix": int(time.time()),
        "current_version": current_version,
        "latest_tag": tag,
        "available": available,
        "release_notes": notes,
    }


def _version_matches(current: str, tag: str) -> bool:
    """Compare a `git describe`-style current version to a vX.Y.Z tag.

    The version source for /api/health is `git describe` which yields
    'v0.210.0' on a tagged commit and 'v0.210.0-3-gabc1234' otherwise.
    On an exact tag match we want available=False; the '-N-g<sha>'
    suffix indicates we're past the tag (which we treat as "still up to
    date" since auto-update only ever installs blessed tags).
    """
    if not current or not tag:
        return False
    # Exact match.
    if current == tag:
        return True
    # `git describe`-style suffix.
    if current.startswith(f"{tag}-"):
        return True
    return False


# ─── Update-busy gate (D5, F7) ─────────────────────────────────────────────


_BUSY_STATES: Final[frozenset[str]] = frozenset({"active", "activating", "deactivating", "reloading"})


def update_is_busy() -> bool:
    """Authoritative busy gate for litclock-update.service.

    Combines two signals (D5 + F7):

    1. ``systemctl is-active litclock-update.service`` — covers the running
       case. Parses stdout (NOT exit code) because Bookworm's
       `is-active --quiet` returns 3 for the 'activating' state, which
       would let a polling caller fire mid-render and have its `start`
       coalesced with the in-flight oneshot run (M3 50b51dbb precedent).
    2. ``systemctl list-jobs`` — covers the queued case. If the Sunday
       timer fired 1ms before the user tapped Apply and the unit's job
       is queued but not yet activated, is-active returns 'inactive' but
       starting the unit again would queue a second redundant job.

    Returns True if EITHER signal reports activity. False only if both
    say the unit is idle.

    Subprocess errors (path missing, sudo denied) → False with a warning.
    Better to allow the apply attempt + see systemd's own error than
    block the user on a broken local check.
    """
    if _is_active_busy():
        return True
    if _has_queued_job():
        return True
    return False


def _is_active_busy() -> bool:
    try:
        result = subprocess.run(
            [SYSTEMCTL_BIN, "is-active", UPDATE_UNIT],
            capture_output=True,
            timeout=5,
            text=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("systemctl is-active probe failed: %s", exc)
        return False
    state = (result.stdout or "").strip()
    return state in _BUSY_STATES


def _has_queued_job() -> bool:
    try:
        result = subprocess.run(
            [SYSTEMCTL_BIN, "list-jobs", "--no-legend", UPDATE_UNIT],
            capture_output=True,
            timeout=5,
            text=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("systemctl list-jobs probe failed: %s", exc)
        return False
    # list-jobs with a unit filter returns rows like:
    #   123 litclock-update.service start running
    # Empty stdout = no queued job. Non-empty = at least one job in the queue.
    return bool((result.stdout or "").strip())


# ─── Status file reader (D2, D9) ───────────────────────────────────────────


def read_status_file(status_file: Path | None = None) -> dict[str, Any]:
    """Read + parse /run/litclock/update.status. Always returns a dict.

    File missing → ``{"state": "idle"}``. Caller routes idle through
    /api/update/status as a normal 200 response so the PWA renders the
    Updates tab card without a phase reading-list.

    File present but parse-fails / oversized / non-regular → ``{"state":
    "stale"}``. The atomic mv-tmp write in update.sh's _write_status_json
    (D9) makes torn reads vanishingly unlikely; if we do see one, or if a
    1MB junk file or symlink lands at the path, mark it stale so the PWA
    can render a degraded state instead of a 500 (or, worse, OOM).

    #336 — bounded via ``safe_read_json`` (8KB cap, lstat-rejects symlinks /
    FIFOs / dirs). Distinguishes "file absent" (idle) from "file present
    but unreadable / oversize" (stale) by an explicit lstat check up front.
    """
    target = status_file or status_path()
    try:
        os.lstat(target)
    except FileNotFoundError:
        return {"state": "idle"}
    except OSError as exc:
        logger.warning("status file unreadable (%s); reporting stale", exc)
        return {"state": "stale", "error": str(exc)}
    data = safe_read_json(target, MAX_STATUS_FILE_BYTES)
    if data is None:
        return {"state": "stale", "error": "status payload was not loadable"}
    return data
