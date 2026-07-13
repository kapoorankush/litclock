"""Single source of truth for env.sh reads + atomic writes.

Captures the atomic-write + ownership-preserve + tempfile-cleanup pattern that
previously lived inline in `src/setup_server.py`. Both setup_server (first-boot
resolver via `_update_env_location`) and control_server (post-boot Settings
PATCH route) call ``atomic_update`` here so the env.sh write contract stays
identical across all settings edits.

Locked behaviors:
- Atomic via `os.replace(tmp, target)` — no torn writes on power loss.
- Ownership + mode of the existing file are preserved (fchown/fchmod on the
  tmp fd before replace).
- Updates are merged into existing content; lines for un-managed keys
  (comments, blank lines, complex shell) are preserved verbatim.
- All values pass `validate_setting()` before any write — if any value fails,
  no write happens (no partial writes).
- Keys outside `SETTINGS_ALLOWLIST` are rejected. M3+ adds new keys here.
- Concurrent writers are serialized via `fcntl.flock` on a sidecar `.lock`
  file alongside the target. The sidecar (vs. locking the target itself)
  survives `os.replace`, which would otherwise unlink the inode the lock is
  attached to and let a second writer race in mid-update. See `atomic_update`
  for the full rationale.

The validators are deliberately tight and ASCII-safelisted. env.sh is sourced
by bash; values with shell metacharacters could execute on `source env.sh`.
Most keys (numeric coords / fixed enums / booleans) keep every value in
`[A-Za-z0-9.-]+` so no quoting is needed. The free-form ``GIFT_MODE_MESSAGE``
key passes through ``shlex.quote()`` at write time *and* a content allowlist
that rejects backtick + ``$`` (defense-in-depth — `shlex.quote` already
neutralises both, but the explicit deny means a future writer that forgets to
re-quote can't regress us). The validator also caps it at GIFT_MODE_MESSAGE_MAX_LEN
characters (post-#319: 80).
"""

from __future__ import annotations

import fcntl
import os
import re
import shlex
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path


def _parse_env_lock_wait_default() -> float:
    """Parse `LITCLOCK_ENV_LOCK_WAIT` from the environment. Defaults to 30s,
    matching `scripts/lib/state.sh:ENV_LOCK_WAIT_DEFAULT` so both writers
    honor the same env-var (`tests/test_envsh_shell_flock.py::
    test_python_and_shell_share_lock_wait_env_var` pins symmetry). Malformed
    values fall back to the default so a typo can't crash module import on
    a real Pi."""
    raw = os.environ.get("LITCLOCK_ENV_LOCK_WAIT", "30")
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return 30.0
    return parsed if parsed >= 0 else 30.0


# #274 follow-up #4 — bounded wait for the Python writer's env.sh sidecar
# flock. Mirrors `scripts/lib/state.sh`'s 30s default + `LITCLOCK_ENV_LOCK_WAIT`
# override so both writers timeout symmetrically. Before this, `_exclusive_lock`
# called `fcntl.flock(LOCK_EX)` with no timeout — a stuck shell writer (mv
# wedged on a degraded SD card, strace left attached, kernel oops mid-rename)
# would block the Flask request thread forever, accumulating stuck threads on
# every Save until OOM.
ENV_LOCK_WAIT_DEFAULT = _parse_env_lock_wait_default()
# Poll cadence for the LOCK_EX|LOCK_NB loop. 10ms gives ~100 syscalls/sec
# under contention — negligible CPU vs. the typical stuck-rename(2) window.
ENV_LOCK_POLL_INTERVAL_S = 0.01

ENV_FILE_DEFAULT = os.environ.get("LITCLOCK_ENV_FILE", "/home/pi/litclock/env.sh")

_KV_PATTERN = re.compile(r"^(\s*(?:export\s+)?)([A-Z_][A-Z0-9_]*)=(.*)$")


# ---------- Validators ----------

Validator = Callable[[str], "tuple[bool, str | None]"]


def _validate_latitude(value: str) -> tuple[bool, str | None]:
    # #325: empty string is a valid "unset" state — env.sh.sample ships
    # with `WEATHER_LOCATION_NAME=` empty by design, and literary_clock.py
    # handles empty coords as "no location" (the elif location_lat and
    # location_long: branch in main()). The PWA's Clear weather location
    # affordance writes "" for all three weather keys; without accepting
    # empty here, atomic_update would 422 the explicit-clear payload.
    if value == "":
        return True, None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return False, "must be numeric"
    if not -90.0 <= f <= 90.0:
        return False, "must be between -90 and 90"
    return True, None


def _validate_longitude(value: str) -> tuple[bool, str | None]:
    # See _validate_latitude — same "" -> unset contract (#325).
    if value == "":
        return True, None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return False, "must be numeric"
    if not -180.0 <= f <= 180.0:
        return False, "must be between -180 and 180"
    return True, None


def _validate_units(value: str) -> tuple[bool, str | None]:
    # Tight allowlist — units flows downstream into the weather-cache filename
    # (see weather_providers/base_provider.py) so any deviation is a
    # path-traversal vector. config.atomic_update is the single writer.
    if value not in ("imperial", "metric"):
        return False, "must be 'imperial' or 'metric'"
    return True, None


def _validate_weather_location_mode(value: str) -> tuple[bool, str | None]:
    # #337 A1: two-state enum. WRITER accepts only "auto" or "specific" —
    # empty string is REJECTED to close the downgrade hole flagged in
    # /review (an empty-string write reads as "auto" via the read-side
    # default, so accepting it at write time would let a confused or
    # hostile client silently downgrade a Specific user back to Auto
    # without going through the explicit mode pill). Empty is only valid
    # as a read-side default for legacy / pre-S2 env.sh files where the
    # key is genuinely absent.
    if value not in ("auto", "specific"):
        return False, "must be 'auto' or 'specific'"
    return True, None


def _validate_weather_ip_country(value: str) -> tuple[bool, str | None]:
    # #337 A6.1: ISO 3166-1 alpha-2 country code (uppercase) OR empty
    # (pre-S2 envs + first-resolve cases where IP-geo hasn't run yet).
    # Used by the on-boot reresolve service to detect country changes for
    # the UNITS-flip rule (A6). The value is compared as a case-sensitive
    # sentinel against `_resolve_location_from_ip`'s output (uppercase),
    # so the validator REJECTS lowercase to prevent silent miscompare
    # bugs — flagged in /review where an external JSON client posting
    # "gb" would persist as "gb" and break the country-change detector.
    # Internal callers (location_resolver, routes/settings.py country-
    # change block) all uppercase before write, so this validator only
    # fires defensively against the JSON PATCH boundary.
    if value == "":
        return True, None
    if len(value) != 2 or not value.isascii() or not value.isalpha() or not value.isupper():
        return False, "must be a 2-letter UPPERCASE ISO country code (e.g. 'US', 'GB', 'IN')"
    return True, None


def _validate_bool(value: str) -> tuple[bool, str | None]:
    if value.lower() not in ("true", "false"):
        return False, "must be 'true' or 'false'"
    return True, None


# #319: lowered from 280 → 80 once the e-ink renderer started word-wrapping
# the title (eink_display._wrap_title). 80 characters covers natural gifter
# messages ("Happy Birthday Mom! Love, Alexis" = 32c) in 1-2 wrapped lines
# at the 48pt title font; the renderer ellipsis-truncates anything that
# still overflows two lines so a wider-than-expected glyph mix degrades
# gracefully instead of falling off the canvas.
GIFT_MODE_MESSAGE_MAX_LEN = 80
WEATHER_LOCATION_NAME_MAX_LEN = 120

# Defense-in-depth content allowlist on top of `shlex.quote` at the writer.
# `$` enables variable expansion + command substitution `$(...)` and `${...}`;
# backtick enables legacy command substitution. `shlex.quote` already wraps
# values in single quotes (which neutralise both inside the quoted span), but
# rejecting them outright means a future writer that forgets to re-quote
# can't regress us into shell-injection territory.
_FREE_FORM_FORBIDDEN_CHARS = ("`", "$")

# #319 follow-up — characters that ``str.splitlines()`` splits on in
# addition to ``\n`` / ``\r``. ``load_config`` uses ``.read_text().splitlines()``,
# so any of these in a stored free-form value would split the env.sh line in
# half and corrupt the round-trip. Pages.app and Word emit U+2028 for soft
# returns, so a paste from either silently bricks the value. Adversarial
# /review caught all eight after the U+2424 sentinel encoding landed —
# reject them at the writer so the corruption can't be persisted.
#
# Also reject our internal sentinels (U+2424 / U+240D) so a user typing
# them literally doesn't get them silently rewritten to ``\n`` / ``\r`` on
# the next ``load_config`` (sentinel-collision bug, also adversarial /review).
_LINE_TERMINATOR_LIKE_CHARS = frozenset(
    [
        "\x0b",  # U+000B VERTICAL TAB
        "\x0c",  # U+000C FORM FEED
        "\x1c",  # U+001C FILE SEPARATOR
        "\x1d",  # U+001D GROUP SEPARATOR
        "\x1e",  # U+001E RECORD SEPARATOR
        "\x85",  # U+0085 NEXT LINE (NEL)
        " ",  # U+2028 LINE SEPARATOR  (the Pages.app soft-return char)
        " ",  # U+2029 PARAGRAPH SEPARATOR
        "␤",  # our \n sentinel (see _LINE_SEP below)
        "␍",  # our \r sentinel (see _PARA_SEP below)
    ]
)


def _make_free_form_validator(
    max_codepoints: int,
    *,
    allow_newlines: bool = False,
    max_bytes: int | None = None,
) -> Validator:
    """Build a validator for a free-form text setting.

    ``max_codepoints`` caps the number of Unicode codepoints (what users
    perceive as "characters"). ``max_bytes``, when not ``None``, ADDITIONALLY
    caps the UTF-8 byte length. The byte cap is opt-in because not every
    free-form field has a byte-bound downstream consumer — see the per-key
    instantiations below.
    """

    def _validate(value: str) -> tuple[bool, str | None]:
        # User-facing codepoint cap. "80 characters" in the UI label maps to
        # 80 codepoints here (Python's ``len(str)`` is codepoint count).
        if len(value) > max_codepoints:
            return False, f"must be at most {max_codepoints} characters"
        # #317 item 3 (parameterized in adversarial /review follow-up): some
        # free-form fields ALSO have a byte-bound consumer downstream and
        # need a parity-critical UTF-8 byte cap on top of the codepoint
        # cap. ``GIFT_MODE_MESSAGE`` is the canonical case:
        # ``scripts/reset-setup.sh`` (the ``--message-file`` consumer fired
        # by ``litclock-prepare-for-gift.service``) reads at most
        # GIFT_MODE_MESSAGE_MAX_LEN BYTES via ``os.read(fd, N)``. Without
        # the byte cap here, an emoji-heavy message at the codepoint limit
        # can be up to 4× the byte limit; the consumer's byte read then
        # cuts mid-codepoint → invalid UTF-8 → tofu glyphs on the e-ink
        # welcome splash.
        #
        # Other free-form fields (``WEATHER_LOCATION_NAME``) have no
        # byte-bound consumer — their downstream is shlex-quoted into
        # env.sh and read back as a Unicode string. Applying the byte cap
        # there would silently reject legitimate non-ASCII geocoded names
        # like "São Paulo" near the cap that ``_short_location_name``
        # truncates by CODEPOINTS, not bytes.
        #
        # ``UnicodeEncodeError`` guard: a JSON payload can deliver a valid
        # Python ``str`` containing lone surrogates (e.g. ``"\ud800"``)
        # that ``.encode("utf-8")`` refuses. Without this catch the
        # validator raises 500 on the API path AND, on
        # ``/api/system/prepare-for-gift``, the confirm token has already
        # been consumed → user loses the token to a 500. Same trap class
        # #328 was supposed to close. Convert to a clean validation error.
        if max_bytes is not None:
            try:
                byte_len = len(value.encode("utf-8"))
            except UnicodeEncodeError:
                return False, "must be valid UTF-8 (no unpaired surrogates)"
            if byte_len > max_bytes:
                return False, (
                    f"must be at most {max_bytes} bytes (emoji and accented characters take more than one byte each)"
                )
        for ch in _FREE_FORM_FORBIDDEN_CHARS:
            if ch in value:
                return False, f"may not contain {ch!r}"
        if not allow_newlines and ("\n" in value or "\r" in value):
            return False, "may not contain line breaks"
        if "\x00" in value:
            return False, "may not contain NUL"
        for ch in _LINE_TERMINATOR_LIKE_CHARS:
            if ch in value:
                return False, "may not contain unprintable line-terminator characters"
        return True, None

    return _validate


# #319: GIFT_MODE_MESSAGE allows embedded newlines so the gifter can write
# a multi-line welcome ("Happy Birthday\nMom!"). The renderer's word-wrap
# treats `\n` as a hard line break. WEATHER_LOCATION_NAME stays single-line
# — it's a search input, not free-form prose.
#
# Only GIFT_MODE_MESSAGE passes ``max_bytes`` — see _make_free_form_validator
# for why the byte cap is scoped narrowly (parity with the byte-bound
# reset-setup.sh consumer). WEATHER_LOCATION_NAME stays codepoint-only:
# its only consumer is env.sh round-trip via shlex.quote, which is bytes-
# agnostic, and ``_short_location_name`` already truncates by codepoints.
_validate_gift_mode_message = _make_free_form_validator(
    GIFT_MODE_MESSAGE_MAX_LEN,
    allow_newlines=True,
    max_bytes=GIFT_MODE_MESSAGE_MAX_LEN,
)
_validate_weather_location_name = _make_free_form_validator(WEATHER_LOCATION_NAME_MAX_LEN)


SETTINGS_ALLOWLIST: dict[str, Validator] = {
    "WEATHER_LATITUDE": _validate_latitude,
    "WEATHER_LONGITUDE": _validate_longitude,
    "WEATHER_UNITS": _validate_units,
    "WEATHER_LOCATION_NAME": _validate_weather_location_name,
    # #337 A1 / A6.1 — location-mode provenance + last-detected country.
    # MODE drives the on-boot reresolve gate (skip when 'specific') and the
    # PWA radio default render. IP_COUNTRY is the country-change detector
    # for the UNITS-flip rule (A6) shared by on-boot + PWA Auto/Specific
    # save paths.
    "WEATHER_LOCATION_MODE": _validate_weather_location_mode,
    "WEATHER_IP_COUNTRY": _validate_weather_ip_country,
    "WEATHER_ENABLED": _validate_bool,
    "ALLOW_NSFW_QUOTES": _validate_bool,
    # #416 PR3c (F31) — opt-in toggle for the diagnostics shortcut ribbon's
    # full-label state. Default unset / "false" → dots-three icon only
    # (owner-persona protection per OV-D-C). True → 'Live diagnostics' label
    # expanded. Read by base.html.j2 which sets body[data-diag-ribbon-expanded].
    "SHOW_DIAGNOSTICS_SHORTCUT": _validate_bool,
    # #280: GIFT_MODE_ENABLED dropped — the M3 toggle had no runtime
    # semantics (literary_clock.py never read it) and the new design treats
    # gift mode as a one-shot pre-ship action via /api/system/prepare-for-gift,
    # not a persistent state. GIFT_MODE_MESSAGE persists as a transient draft
    # so the gifter can compose over multiple visits.
    "GIFT_MODE_MESSAGE": _validate_gift_mode_message,
}

# Keys whose values are free-form text and must be wrapped via `shlex.quote()`
# before being written to env.sh. All other keys in SETTINGS_ALLOWLIST are
# constrained to ASCII tokens that don't need quoting.
_SHELL_QUOTED_KEYS = frozenset(["GIFT_MODE_MESSAGE", "WEATHER_LOCATION_NAME"])

# #319 hardware-QA fix: env.sh is a line-oriented file but ``GIFT_MODE_MESSAGE``
# now allows embedded newlines. Writing a multi-line shlex-quoted value
# would span multiple lines, and ``load_config`` parses line-by-line — so
# the reload path would only see the first line and the textarea pre-fill
# would render a broken stray-quote artifact. The destructive Prepare form's
# hidden mirror would then ship the broken value too.
#
# Fix: encode `\n` / `\r` as U+2424 (SYMBOL FOR NEWLINE) / U+240D (SYMBOL
# FOR CARRIAGE RETURN) before shlex.quote. These are visible Unicode glyphs
# that splitlines() and shlex.split() both treat as ordinary characters
# (unlike U+2028/U+2029, which ARE Unicode line separators and break the
# round-trip — caught in hardware QA on the test Pi).
# Welcome-message → e-ink path is untouched: ``/api/system/prepare-for-gift``
# writes the form-submitted bytes (with real ``\n``) directly to
# ``/run/litclock/gift-message``; this encoding only governs env.sh round-trip.
_LINE_SEP = "␤"  # U+2424 SYMBOL FOR NEWLINE
_PARA_SEP = "␍"  # U+240D SYMBOL FOR CARRIAGE RETURN


def _encode_newlines_for_envsh(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\n", _LINE_SEP).replace("\r", _PARA_SEP)


def _decode_newlines_from_envsh(value: str) -> str:
    return value.replace(_LINE_SEP, "\n").replace(_PARA_SEP, "\r")


def _serialize_value(key: str, value: str) -> str:
    """Render a validated value for the env.sh line. Free-form text keys
    pass through `shlex.quote`; tightly-validated keys (numbers, enums,
    booleans) are emitted verbatim to keep the file diff-friendly.

    Free-form values get newlines encoded as U+2424 first so the env.sh
    line never spans multiple physical lines (see _LINE_SEP comment)."""
    if key in _SHELL_QUOTED_KEYS:
        return shlex.quote(_encode_newlines_for_envsh(value))
    return value


def _export_prefix(captured: str) -> str:
    """Canonicalize the line prefix to ``export ``.

    `_KV_PATTERN`'s group(1) is whatever indentation + optional ``export``
    the existing line had. Bash-sourced env.sh requires ``export`` for
    values to reach the Python child process — without it the assignment
    is shell-local and ``os.getenv`` returns None. We rewrite the prefix
    on every save so:

    - Lines that already had ``export`` keep it.
    - Lines that lacked ``export`` GAIN it on the next save (one-time
      backfill for env.sh files written before this fix).
    - Leading whitespace from the captured prefix is dropped — managed
      keys are flush-left for diff sanity.
    """
    if "export" in captured:
        return captured.lstrip()
    return "export "


def validate_setting(key: str, value: str) -> tuple[bool, str | None]:
    """Validate a single key/value pair against `SETTINGS_ALLOWLIST`.

    Returns (True, None) on pass, (False, error_message) on fail. Unknown keys
    are rejected — the allowlist is the contract.
    """
    if key not in SETTINGS_ALLOWLIST:
        return False, f"unknown setting: {key}"
    return SETTINGS_ALLOWLIST[key](value)


# ---------- Reader ----------


def load_config(path: str | os.PathLike[str] = ENV_FILE_DEFAULT) -> dict[str, str]:
    """Parse env.sh into a key/value dict.

    Recognises `KEY=value` and `export KEY=value`. Strips a single layer of
    surrounding single or double quotes. Lines that don't match (comments,
    blanks, multi-line shell) are silently skipped — `atomic_update()` will
    preserve them verbatim on write.

    Returns an empty dict if the file does not exist (matches setup_server's
    behavior of "no config = nothing to read", not an error).
    """
    config: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return config
    for line in p.read_text().splitlines():
        m = _KV_PATTERN.match(line)
        if not m:
            continue
        _prefix, key, raw = m.group(1), m.group(2), m.group(3)
        value = _unquote(raw)
        # Reverse the writer's newline-encoding for free-form keys so
        # ``\n``/``\r`` survive the env.sh round-trip (#319 hardware-QA).
        if key in _SHELL_QUOTED_KEYS:
            value = _decode_newlines_from_envsh(value)
        config[key] = value
    return config


def _unquote(raw: str) -> str:
    """Reverse the writer's quoting. Round-trips ``shlex.quote()``-emitted
    values for free-form text keys (M3's GIFT_MODE_MESSAGE / WEATHER_LOCATION_NAME)
    while still handling the legacy "single layer of ' or \\" wrap" pattern that
    pre-M3 setup_server writes used.
    """
    if not raw:
        return raw
    # shlex-quoted values always start with `'` (or are bare single tokens for
    # shell-safe inputs). Try shlex first; on failure (e.g. a value with an
    # unbalanced quote written by some other tool), fall back to the legacy
    # single-layer-strip and then to the raw bytes.
    if raw[0] in ('"', "'"):
        try:
            tokens = shlex.split(raw, posix=True)
            if len(tokens) == 1:
                return tokens[0]
        except ValueError:
            pass
        if len(raw) >= 2 and raw[0] == raw[-1]:
            return raw[1:-1]
    return raw


# ---------- Writer ----------


@contextmanager
def _exclusive_lock(target: Path, timeout: float | None = None) -> Iterator[None]:
    """Hold an exclusive flock on a sidecar `<target>.lock` for the block,
    bounded by ``timeout`` seconds. ``None`` (default) resolves to the
    current ``ENV_LOCK_WAIT_DEFAULT`` at call time so a test or a future
    runtime override of the module constant takes effect on subsequent
    acquires without re-defining the function.

    The lock lives on a sibling file (e.g. `env.sh.lock`), not on `target`
    itself. `atomic_update` finishes by `os.replace`-ing a tempfile over
    `target`, which swaps the inode — any flock held on the old inode
    becomes invisible to a second writer that opens the new inode. The
    sidecar's inode is stable across replaces, so all writers contend on
    the same lock file regardless of when they opened it.

    Cross-process safe: flock semantics extend across processes, so
    setup_server (first-boot) and control_server (post-boot) can both
    serialize against this lock without coordinating in-process. Inherited
    by `os.fork` but NOT by `os.exec*` — fine for our single-process Python
    writers.

    #274 follow-up #4 — bounded wait. The shell-side helper in
    `scripts/lib/state.sh` honors a 30s `LITCLOCK_ENV_LOCK_WAIT` budget; the
    Python side now does the same so a stuck shell writer (wedged mv on a
    degraded SD card, strace left attached, kernel oops mid-rename) can't
    block the Flask request thread forever. Without this, a stuck shell
    writer + steady stream of PWA Saves would pile up stuck threads in
    waitress until OOM. On timeout, raises ``TimeoutError`` — the
    `routes/settings.py` handler catches this and returns HTTP 504 with a
    structured envelope so the user gets a real error instead of a hanging
    spinner.

    ``timeout`` semantics:
      * ``timeout > 0``: poll `LOCK_EX | LOCK_NB` every 10ms until acquired
        or until ``time.monotonic()`` exceeds the deadline.
      * ``timeout <= 0``: try once non-blocking, raise immediately on
        contention. Useful for tests and CSP probe-like callers.

    Lock is released on context exit even if the body raises.
    """
    # Resolve the sentinel to the module-level default at call time. This
    # lets a test (or a future runtime hook) override
    # `config.ENV_LOCK_WAIT_DEFAULT` and have the next acquire pick it up
    # without re-importing the module or re-defining this function.
    effective_timeout = ENV_LOCK_WAIT_DEFAULT if timeout is None else timeout
    # NaN-safe clamp (adversarial-review P2): `max(0.0, nan)` returns nan
    # in CPython; `time.monotonic() >= nan` is always False (IEEE rules),
    # which would make the poll loop spin forever — exactly the OOM /
    # thread-pile-up failure this PR was meant to prevent. The negated
    # comparison `not (x > 0)` IS NaN-safe: `nan > 0` is False, so the
    # branch fires and we clamp to 0 (non-blocking single attempt).
    # Negative timeouts route through the same clamp.
    if not (effective_timeout > 0):
        effective_timeout = 0.0
    lock_path = target.with_name(target.name + ".lock")
    # `os.open` with O_CREAT so the first writer creates the sidecar.
    # Mode 0o644 is fine — the lock has no secrets, and writers need to
    # both read (lock) and the file's existence is what matters, not its
    # contents. We never write to the lock file's body.
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + effective_timeout
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                # Another writer holds the lock. Check the deadline; if
                # exceeded, surface the timeout. Otherwise sleep + retry.
                # Note: time.monotonic() before sleep so a timeout=0 caller
                # (non-blocking) raises immediately on the first contention
                # without an unnecessary sleep.
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"env.sh lock held >{effective_timeout}s — another writer "
                        f"(update.sh / reset-setup / prepare-for-cloning?) "
                        f"is stuck. Try again or check journalctl for a "
                        f"hanging script."
                    ) from None
                time.sleep(ENV_LOCK_POLL_INTERVAL_S)
        yield
    finally:
        # flock is released automatically on close, but unlock first so
        # any error in close doesn't leave the lock implicitly dangling
        # in less common kernels.
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def atomic_update(
    updates: Mapping[str, str],
    path: str | os.PathLike[str] = ENV_FILE_DEFAULT,
) -> None:
    """Atomically merge ``updates`` into the env.sh at ``path``.

    Behavior:
    - All keys are validated *first*. Any failure raises ``ValueError`` and
      no write happens.
    - Lines for keys present in ``updates`` are replaced in place, preserving
      the original ``export`` prefix if any.
    - Keys present in ``updates`` but absent from the file are appended.
    - All other lines (comments, blanks, complex shell) are preserved.
    - File ownership + mode are preserved via fchown/fchmod on the tmp fd.
    - Atomic via ``os.replace`` — readers see either pre- or post-state, never
      a torn intermediate.
    - Concurrent writers are serialized: the read+modify+write+replace sequence
      is wrapped in an exclusive ``fcntl.flock`` on ``<path>.lock`` (sidecar).
      Without this, two writers each holding pre-update content would last-
      write-wins on ``os.replace`` and silently drop one update. See
      ``_exclusive_lock`` for why the lock is on a sidecar rather than on
      ``path`` itself.

    Raises ``FileNotFoundError`` if ``path`` does not exist; ``ValueError`` on
    validation failure; ``OSError`` on filesystem failures (with tmpfile
    cleanup attempted).
    """
    if not updates:
        return

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"env file not found: {p}")

    # Fail fast before touching the filesystem — no partial writes.
    # Validation runs outside the lock; it's pure CPU and shouldn't
    # serialize callers behind each other for input errors.
    for key, value in updates.items():
        ok, err = validate_setting(key, value)
        if not ok:
            raise ValueError(f"invalid {key}={value!r}: {err}")

    with _exclusive_lock(p):
        stat = p.stat()
        content = p.read_text()
        trailing_newline = content.endswith("\n")

        remaining = dict(updates)
        new_lines: list[str] = []
        for line in content.splitlines():
            m = _KV_PATTERN.match(line)
            if m and m.group(2) in remaining:
                key = m.group(2)
                value = remaining.pop(key)
                new_lines.append(f"{_export_prefix(m.group(1))}{key}={_serialize_value(key, value)}")
            else:
                new_lines.append(line)

        # Append any keys that weren't already in the file. Always with
        # `export` so the bash-sourced child process (literary_clock.py
        # via runtheclock.sh) actually sees them — bare `KEY=value` is a
        # shell variable, not an env var. Caught on test Pi 2026-04-29:
        # M3 toggled WEATHER_ENABLED=false but appended without `export`,
        # so os.getenv returned None and the toggle was silently no-op.
        for key, value in remaining.items():
            new_lines.append(f"export {key}={_serialize_value(key, value)}")

        new_content = "\n".join(new_lines)
        if trailing_newline:
            new_content += "\n"

        env_dir = str(p.parent)
        fd, tmp_path = tempfile.mkstemp(dir=env_dir, prefix=".env.sh.tmp.")
        try:
            os.fchown(fd, stat.st_uid, stat.st_gid)
            os.fchmod(fd, stat.st_mode)
            with os.fdopen(fd, "w") as f:
                f.write(new_content)
            os.replace(tmp_path, p)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
