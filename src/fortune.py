"""Fortune mode (litclock-dev#533) — Debian fortune databases as the body text.

An alternative to the literary quotes, switched on by
``LITCLOCK_CONTENT_MODE=fortune`` from the PWA's Settings → Experimental section.
The clock stays a clock: the masthead carries the time in this mode
(literary_clock._compose_masthead), so the body never has to.

Sourcing (owner decision, 2026-10-22): the text is Debian's ``fortunes``
package, installed on the device by the image and by update.sh, and read
RAW from ``/usr/share/games/fortunes`` at paint time. The repo never
redistributes a fortune — it holds this reader and a denylist of entry
hashes (``fortune_denylist.txt``). Every installed database is offered;
the offensive ones (an ``off/`` subdirectory, Debian's ``fortunes-off``)
join the pool only when ticked by name AND ``ALLOW_NSFW_QUOTES`` is true.
Raspberry Pi OS bookworm's archive does not carry ``fortunes-off``
(measured 2026-10-23), so nothing installs it; the reader supports the
tree for a device where someone did.

Selection is deterministic per dwell slot (``LITCLOCK_FORTUNE_DWELL_MINUTES``,
default 1): the painter is a fresh process every minute, so the fortune
for a slot is a pure function of (slot, pool), and a reboot or a repaint
mid-slot shows the same one. Unusable entries — ASCII art, tables, text
outside the font, anything that does not fit at the legibility floor —
are skipped by drawing the next candidate from the same seeded stream,
so the skip is deterministic too.

This module deliberately does NOT live in quote_renderer.py: that file is
a runtime-render proof input, and editing it revokes every device's
validation marker (a ~3 minute re-validation fleet-wide). It reads that
module's constants and fonts, never changes them.

Never raises from ``current_fortune``: a broken database or a render
failure returns None and the painter falls back to the quote frame.
"""

from __future__ import annotations

import codecs
import hashlib
import logging
import os
import random
import re
import struct
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import Image, ImageFont

# Pillow is imported where it draws, not here: the control server imports this
# module to LIST the databases for the lab section, and a module-scope PIL
# import would pin Pillow in that long-running process for nothing (review).

FORTUNE_DIR = Path(os.environ.get("LITCLOCK_FORTUNE_DIR", "/usr/share/games/fortunes"))
OFFENSIVE_SUBDIR = "off"
DENYLIST_PATH = Path(__file__).resolve().parent / "fortune_denylist.txt"

CONTENT_MODES = ("quotes", "fortune")
CONTENT_MODE_DEFAULT = "quotes"
DWELL_DEFAULT = 1  # owner decision 2026-10-24 (was 5)
DWELL_MIN, DWELL_MAX = 1, 60
DWELL_CHOICES = (1, 5, 10, 15)  # what the lab section offers; the reader accepts DWELL_MIN..DWELL_MAX

# A database name as the PWA writes it into LITCLOCK_FORTUNE_CATEGORIES.
CATEGORY_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
# An offensive database's selection key: "off-<name>". Its OWN identity, never
# the bare name — Debian ships the same basenames in both trees (wisdom and
# off/wisdom), and a shared key let ticking the ordinary box pull in the
# offensive half (Codex review).
OFFENSIVE_PREFIX = "off-"
# A strfile index is a few KB; refuse anything absurd rather than read it on
# the per-minute path.
MAX_DAT_BYTES = 4 * 1024 * 1024
CATEGORIES_MAX = 128

# Canvas: the quote area under the masthead, same as a quote frame.
WIDTH, HEIGHT = 800, 400
SIDE_MARGIN = 40
TOP_MARGIN = 14
BOTTOM_MARGIN = 10
# The poster cap (owner decision 2026-08-12): short fortunes grow large and
# centred, but stop here. The quote renderer has no ceiling — a five-word
# fortune would otherwise land near 110pt. Sizes are GD points (px = pt*4/3),
# the unit the quote renderer and its FS_HARD_FLOOR speak.
FS_MAX = 56
FS_FLOOR = 22  # == quote_renderer.FS_HARD_FLOOR; asserted equal in tests
LINE_HEIGHT_RATIO = 1.618  # == quote_renderer.LINE_HEIGHT_RATIO
ATTR_FONT_PT = 18
ATTR_MAX_WIDTH = 500
ATTR_MAX_LINES = 2
ATTR_GAP = 14
ATTR_LINE_SPACING = 1.1  # the credits two-line leading quote_renderer.add_credits uses

# Cheap pre-filters before any measurement. A fortune longer than this cannot
# fit at FS_FLOOR (measured: ~420 chars is the practical ceiling for prose).
MAX_BODY_CHARS = 420
MAX_LINES = 9
MAX_ATTEMPTS = 60
# Wall-clock cap on one pick. The painter fires 4s before the minute; a run of
# entries that normalise but never fit (a long URL, say) costs a binary search
# each, so draws alone do not bound the minute (review). Past the budget the
# painter shows a quote, never a late frame.
PICK_BUDGET_S = 1.5

# Characters the shipped Literata covers and that read correctly on the panel.
# Anything else (box drawing, CJK, control codes, overstrike) rejects the entry
# rather than painting tofu.
_ALLOWED = frozenset(
    [chr(c) for c in range(0x20, 0x7F)]
    + [chr(c) for c in range(0xA0, 0x100)]
    # Typographic punctuation, listed one by one: the U+2010 block has holes in
    # the shipped Literata (U+2011, U+201B, U+201F, U+2023-2025 among others),
    # and a range would paint those as tofu. tests/test_fortune.py checks every
    # member against both fonts' cmaps.
    + [chr(c) for c in (0x2010, 0x2012, 0x2013, 0x2014, 0x2015, 0x2018, 0x2019, 0x201A, 0x201C, 0x201D, 0x201E)]
    + [chr(c) for c in (0x2020, 0x2021, 0x2022, 0x2026, 0x2030, 0x2032, 0x2033, 0x2039, 0x203A, 0x20AC, 0x2122)]
)
_ART_CHARS = set("|/\\_#*=<>~^{}[]@+")
_DIALOGUE_RE = re.compile(r"^[A-Z][A-Za-z.' ]{0,24}:\s")
_LABEL_TAB_RE = re.compile(r"^(\s*[A-Z][A-Za-z.' ]{0,24}:)\t+")
_INNER_GAP_RE = re.compile(r"\S {4,}\S|\t")
_WS_RE = re.compile(r"\s+")

# Words that keep a fortune off the panel while ALLOW_NSFW_QUOTES is off
# (litclock-dev#971). "Offensive" elsewhere in this module means Debian's off/
# tree, which bookworm does not ship, so the NSFW switch gated nothing that is
# installed, and the ordinary databases carry ~150 explicit or crude lines (the
# fielded clock showed one on 2026-10-28). Case-insensitive, matched as whole
# words: a trailing * takes any suffix, a leading * any prefix (only on stems
# no innocent word contains; a free prefix on a short stem would take English
# place names). An entry with a space matches across a wrapped line.
# Conservative on purpose: a false positive costs one draw, a miss paints the
# line on someone's shelf. A single miss goes in fortune_denylist.txt instead.
#
# Stored rot13, the way Debian stores its own offensive fortunes, so this
# public source carries none of the words in plain text (owner, 2026-10-28).
# Read one with: python3 -c "import codecs; print(codecs.decode('...', 'rot13'))"
_MATURE_WORDS_ROT13 = (
    # explicit terms
    "frk", "frkl", "frkrq", "frkvre", "frkvrfg", "frkyrff", "*frkhny*", "vagrepbhefr", "sbercynl", "xvaxl",
    "cravf*", "intvan*", "pyvgbevf*", "betnfz*", "betl", "betvrf", "znfgheong*", "rerpgvba*", "rwnphyng*",
    "pbaqbz*", "qvyqb*", "cbea*", "cebfgvghg*", "juber*", "fyhg*", "ubeal", "ubeavre", "ubeavrfg", "alzcub*",
    "sryyngvb", "phaavyvathf", "oybjwbo*", "oybj wbo*", "travgny*", "grfgvpyr*", "fpebghz*", "obbof", "obbovrf",
    "oernfgf", "gvgf", "gvggvrf", "rebgvp*", "ivnten", "encr", "encrq", "encrf", "encvat", "encvfg*",
    # profanity: two stems list their forms rather than take a free suffix,
    # which hit a mushroom and a painter in review; one is also a donkey
    "*shpx*", "s*px*", "*fuvg", "*fuvgf", "*fuvggvat", "*fuvggre*", "fuvggl", "fuvgr", "fuvgyrff", "fuvgurnq*",
    "fuvgybnq*", "cvff", "cvffrf", "cvffrq", "cvffvat", "cvffl", "ovgpu*", "phag*", "pbpxfhpx*", "nffubyr*",
    "nff-ubyr*", "onqnff*", "ovtnff", "ynzrnff", "qhzonff*", "wnpxnff*", "fznegnff*", "zl nff", "lbhe nff",
    "uvf nff", "ure nff", "bhe nff", "gurve nff", "vgf nff", "'f nff", "zl nffrf", "lbhe nffrf", "bhe nffrf",
    "gurve nffrf", "fbzr nff", "xvpx nff", "xvpxf nff", "xvpxrq nff", "xvpxvat nff", "unhy nff", "unhyrq nff",
    "cnva va gur nff", "nefr", "nefrf", "fbqbzvm*", "fbqbzvf*",
    # slurs (two more were left out: each is also an everyday phrase)
    "avttre*", "avttn*", "snttbg*", "snt", "sntf", "xvxr*", "jrgonpx*", "genaal",
)
MATURE_WORDS = tuple(codecs.decode(w, "rot13") for w in _MATURE_WORDS_ROT13)

# Innocent phrases that contain a listed word, removed before matching: the
# word used for gender (the corpus's commonest false positive, its horoscope
# lines among them), a Jefferson line and a Shakespeare title in an
# attribution (final litclock-dev#972 review). rot13 for the same reason as the list.
_INNOCENT_ROT13 = (
    "(?:bccbfvgr|bja|fnzr|bgure|znyr|srznyr|evtug|artyvtvoyr|hapbafvqrerq|snve|tragyr|jrnxre|fgebatre"
    "|jubyr znyr|jubyr srznyr)~frk",
    "frk~rdhnyvgl",
    "fbpvny~vagrepbhefr",
    "encr~bs~yhperpr",
)
_INNOCENT_RE = re.compile(
    r"(?<![^\W_])(?:"
    # "~" is the word separator: rot13 would turn a written \s into \f.
    + "|".join(codecs.decode(x, "rot13").replace("~", r"[\s_-]+") for x in _INNOCENT_ROT13)
    + r")(?![^\W_])",
    re.IGNORECASE,
)


def _word_pattern(word: str) -> str:
    """One MATURE_WORDS entry as a regex. The edges are \\b with the underscore
    counted as a separator, so fortune-style _emphasis_ cannot hide a word; an
    entry that starts with punctuation (a possessive) takes no left edge. A
    leading * still starts at a word edge, so a long token is scanned once,
    not retried from every position (Codex: 1.8s on an 8KB token)."""
    stem = word.strip("*")
    if stem.startswith("f*"):  # the corpus's own self-censored spelling
        body = "f\\*+" + re.escape(stem[2:])
    else:
        body = re.escape(stem).replace("\\ ", "[\\s_-]+")
    edge = "(?<![^\\W_])" if stem[0].isalnum() else ""
    lead = edge + ("[^\\W_]*" if word.startswith("*") else "")
    tail = "\\w*" if word.endswith("*") else ""
    return lead + body + tail + "(?![^\\W_])"


_MATURE_RE = re.compile("|".join(_word_pattern(w) for w in MATURE_WORDS), re.IGNORECASE)

STR_ROTATED = 0x4  # strfile(8) flag: the text is rot13


@dataclass(frozen=True)
class Database:
    name: str
    path: Path
    offensive: bool
    offsets: tuple[int, ...]  # entry start offsets; one per fortune
    rotated: bool

    @property
    def count(self) -> int:
        return len(self.offsets)

    @property
    def key(self) -> str:
        """The name LITCLOCK_FORTUNE_CATEGORIES and the lab list use."""
        return OFFENSIVE_PREFIX + self.name if self.offensive else self.name


@dataclass(frozen=True)
class Fortune:
    database: str
    offensive: bool
    raw: str
    paragraphs: tuple[str, ...]
    attribution: str

    @property
    def text(self) -> str:
        return " ".join(self.paragraphs)


# ─── settings parsing (lenient: env.sh can be hand-edited) ──────────────────


def content_mode(raw: object) -> str:
    """The effective LITCLOCK_CONTENT_MODE. Empty is the sample's unset idiom and is
    silent; anything else unknown warns and paints quotes — the product's
    default is never an experimental feature."""
    value = (raw or "").strip().lower() if isinstance(raw, str) else ""
    if not value:
        return CONTENT_MODE_DEFAULT
    if value not in CONTENT_MODES:
        logging.warning(f"LITCLOCK_CONTENT_MODE={raw!r} is not one of {CONTENT_MODES}; painting quotes")
        return CONTENT_MODE_DEFAULT
    return value


def dwell_minutes(raw: object) -> int:
    value = raw.strip() if isinstance(raw, str) else ""
    if not value:
        return DWELL_DEFAULT
    try:
        n = int(value)
    except ValueError:
        n = 0
    if not DWELL_MIN <= n <= DWELL_MAX:
        logging.warning(
            f"LITCLOCK_FORTUNE_DWELL_MINUTES={raw!r} is not {DWELL_MIN}..{DWELL_MAX}; using {DWELL_DEFAULT}"
        )
        return DWELL_DEFAULT
    return n


def parse_categories(raw: object) -> tuple[str, ...]:
    """LITCLOCK_FORTUNE_CATEGORIES as a sorted tuple; empty means every database.
    Malformed names are dropped, not fatal."""
    if not isinstance(raw, str):
        return ()
    names = {part.strip().lower() for part in raw.split(",") if part.strip()}
    return tuple(sorted(n for n in names if CATEGORY_RE.fullmatch(n)))[:CATEGORIES_MAX]


# ─── databases ──────────────────────────────────────────────────────────────


def _read_dat(path: Path) -> tuple[tuple[int, ...], bool] | None:
    """Parse a strfile(8) index: a 24-byte big-endian header (version,
    numstr, longlen, shortlen, flags, delim) and numstr+1 offsets. Returns
    (entry offsets, rotated) or None when the index is unusable — without it
    there is no way to know whether the text is rot13, so the database is
    skipped rather than guessed at."""
    try:
        if path.stat().st_size > MAX_DAT_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) < 24:
        return None
    version, numstr, _longlen, _shortlen, flags = struct.unpack(">IIIII", data[:20])
    if version not in (1, 2) or numstr == 0 or len(data) < 24 + 4 * (numstr + 1):
        return None
    offsets = struct.unpack(f">{numstr + 1}I", data[24 : 24 + 4 * (numstr + 1)])
    # The last offset is the end-of-file sentinel. STR_RANDOM/ORDERED shuffle
    # the order, never the set, so a sort recovers file order.
    return tuple(sorted(offsets[:numstr])), bool(flags & STR_ROTATED)


def list_databases(root: Path | None = None, *, include_offensive: bool = True) -> list[Database]:
    """Every usable database under ``root``: a text file with a valid
    ``.dat`` beside it. ``.u8`` aliases and the indexes themselves are not
    databases. Offensive databases come from the ``off/`` subdirectory."""
    root = FORTUNE_DIR if root is None else Path(root)
    found: list[Database] = []
    dirs = [(root, False)]
    if include_offensive:
        dirs.append((root / OFFENSIVE_SUBDIR, True))
    for directory, offensive in dirs:
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            if name.endswith((".dat", ".u8")) or name.startswith(".") or not CATEGORY_RE.fullmatch(name):
                continue
            path = directory / name
            if not path.is_file():
                continue
            parsed = _read_dat(directory / f"{name}.dat")
            if parsed is None:
                continue
            offsets, rotated = parsed
            found.append(Database(name, path, offensive, offsets, rotated))
    return found


# has_showable() verdicts by text file version, parsed index and denylist. Settings
# renders call it per database; a database that has nothing to show is read
# to its end, so without this a huge or corrupt one would be read whole on
# every page (Codex review). A changed file, index or denylist is a new key. A cap
# on the scan was tried first and hid a database whose first showable entry
# came after it (second Codex pass). A scan cut short by a read error is not
# cached and counts as showable: cached, one transient failure hid a healthy
# database until the process restarted; hidden even once, a Settings save from
# that page dropped it from the stored pick (final Codex passes). Offering an
# unreadable database is what Settings did before any of this.
_SHOWABLE_CACHE: dict[tuple, bool] = {}


def has_showable(db: Database, denylist: frozenset[str] | None = None, *, allow_nsfw: bool = True) -> bool:
    """Whether any entry survives the denylist and ``normalise`` — stopping at
    the first that does, so an ordinary database costs one read. Debian's
    ascii-art passes none of its 10, and offering it as a choice let an owner
    pick a fortune mode that only ever painted quotes. ``fit`` is not run:
    it needs Pillow, which the control server must not load, and
    ``normalise`` already applies the length and line caps that reject
    nearly every entry ``fit`` would.

    ``allow_nsfw=False`` also applies the MATURE_WORDS filter. The Settings
    LIST leaves it at True on purpose (litclock-dev#971): a list that changed
    with the NSFW switch would let a save drop a database from the pick. The
    save check passes the real switch, so it still refuses a pick that can
    only ever paint quotes."""
    deny = load_denylist() if denylist is None else denylist
    try:
        st = db.path.stat()
    except OSError:
        return False
    key = (str(db.path), st.st_mtime_ns, st.st_size, db.offensive, db.rotated, db.offsets, deny, allow_nsfw)
    if key not in _SHOWABLE_CACHE:
        verdict = _scan_showable(db, deny, allow_nsfw)
        if verdict is None:
            return True
        _SHOWABLE_CACHE[key] = verdict
    return _SHOWABLE_CACHE[key]


def _scan_showable(db: Database, deny: frozenset[str], allow_nsfw: bool = True) -> bool | None:
    """None when a read failed before anything showable turned up."""
    for i in range(db.count):
        try:
            raw = _entry_text(db, i)
        except OSError:
            return None
        if entry_hash(raw) in deny or normalise(raw, db.name, db.offensive) is None:
            continue
        if allow_nsfw or not is_mature(raw):
            return True
    return False


def installed_keys(root: Path | None = None) -> set[str]:
    """Every installed database's key, showable or not. "Not installed" is
    judged against this, not against ``categories()``, which hides some."""
    return {db.key for db in list_databases(root)}


def categories(root: Path | None = None) -> list[tuple[str, bool]]:
    """(key, offensive) for every installed database with something to show —
    what the lab section lists: the ordinary ones sorted, then the offensive
    ones under their own ``off-`` keys, so ticking an ordinary box can never
    select one."""
    deny = load_denylist()
    dbs = [db for db in list_databases(root) if has_showable(db, deny)]
    main = sorted({db.key for db in dbs if not db.offensive})
    off = sorted({db.key for db in dbs if db.offensive})
    return [(k, False) for k in main] + [(k, True) for k in off]


def _entry_text(db: Database, index: int) -> str:
    start = db.offsets[index]
    with open(db.path, "rb") as fh:
        fh.seek(start)
        chunk = fh.read(8192)
    # An entry ends at a line holding only the delimiter.
    lines = []
    for line in chunk.decode("utf-8", errors="replace").split("\n"):
        if line.rstrip("\r") == "%":
            break
        lines.append(line.rstrip("\r"))
    text = "\n".join(lines)
    return codecs.decode(text, "rot13") if db.rotated else text


# ─── normalisation + filtering ──────────────────────────────────────────────


def entry_hash(raw: str) -> str:
    """The denylist key: 16 hex chars of sha256 over the stripped entry."""
    return hashlib.sha256(raw.strip().encode("utf-8")).hexdigest()[:16]


def is_mature(raw: str) -> bool:
    """Whether an entry uses a MATURE_WORDS word, so stays off the panel
    while ALLOW_NSFW_QUOTES is off (litclock-dev#971)."""
    return any(_MATURE_RE.search(text) for text in _match_texts(raw))


def _match_texts(raw: str) -> tuple[str, str]:
    """The entry as the panel will show it, twice: once as written and once
    with a word hyphenated across a line break joined. Both, because joining
    a wrapped hyphenated compound can make a word the list does not hold,
    erasing a match the first half had (third Codex pass).
    Carriage returns, soft hyphens and line-end spaces are gone (normalise
    drops them, the font draws nothing for a soft hyphen), the Unicode hyphen
    is a plain one, and a curly apostrophe is straight."""
    text = raw.replace("\r", "").replace("\u00ad", "").replace("\u2010", "-").replace("\u2019", "'")
    text = _INNOCENT_RE.sub(" ", re.sub(r"[ \t]+\n", "\n", text))
    return text, _INNOCENT_RE.sub(" ", re.sub(r"(\w)-\n\s*(\w)", r"\1\2", text))


def load_denylist(path: Path | None = None) -> frozenset[str]:
    try:
        lines = Path(path or DENYLIST_PATH).read_text(encoding="utf-8").splitlines()
    except OSError:
        return frozenset()
    return frozenset(line.split("#", 1)[0].strip().lower() for line in lines if line.split("#", 1)[0].strip())


def normalise(raw: str, database: str = "", offensive: bool = False) -> Fortune | None:
    """Split an entry into display paragraphs and an attribution, or None
    when it is not prose the panel can show (art, tables, code, overlong).

    Fortunes are hard-wrapped at ~70 columns, so wrapped lines are joined
    back into paragraphs. A new paragraph starts at a blank line, an
    indented line, or a dialogue/Q-and-A label; verse (several short lines)
    keeps every line. The attribution is a trailing indented block that
    opens with ``--``."""
    text = raw.replace("\r", "").strip("\n")
    if not text.strip() or "�" in text:
        return None
    lines = text.split("\n")
    attribution = ""
    tail = len(lines)
    while tail > 1 and lines[tail - 1][:1] in (" ", "\t") and lines[tail - 1].strip():
        tail -= 1
    if tail < len(lines) and lines[tail].strip().startswith("--"):
        attribution = _WS_RE.sub(" ", " ".join(ln.strip() for ln in lines[tail:]))[2:].strip()
        lines = lines[:tail]
    while lines and not lines[-1].strip():
        lines.pop()
    # "Q:<tab>What..." — the riddles format aligns its answers with a tab.
    body_lines = [_LABEL_TAB_RE.sub(r"\1 ", ln.rstrip()) for ln in lines]
    if not body_lines or len(body_lines) > MAX_LINES:
        return None
    joined = "".join(body_lines)
    visible = [c for c in joined if not c.isspace()]
    if not visible:
        return None
    # ASCII art, diagrams, tables, code: punctuation-dense, or aligned in
    # columns. An indented line is a paragraph (or a knock-knock reply), not
    # art on its own.
    if sum(c in _ART_CHARS for c in visible) > max(2, len(visible) // 12):
        return None
    if any(_INNER_GAP_RE.search(ln.strip()) for ln in body_lines):
        return None
    if not set(" ".join(ln.strip() for ln in body_lines) + attribution) <= _ALLOWED:  # leading tabs are layout
        return None
    nonblank = [ln for ln in body_lines if ln.strip()]
    verse = len(nonblank) >= 2 and max(len(ln) for ln in nonblank) < 48
    paragraphs: list[str] = []
    current: list[str] = []
    for ln in body_lines:
        s = ln.strip()
        if not s:
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        if current and (verse or ln[:1] in (" ", "\t") or _DIALOGUE_RE.match(s)):
            paragraphs.append(" ".join(current))
            current = []
        current.append(s)
    if current:
        paragraphs.append(" ".join(current))
    paragraphs = [_WS_RE.sub(" ", p).strip() for p in paragraphs if p.strip()]
    if sum(len(p) for p in paragraphs) > MAX_BODY_CHARS:
        return None
    return Fortune(database, offensive, raw.strip(), tuple(paragraphs), attribution)


# ─── layout + render ────────────────────────────────────────────────────────


def _font(name: str, pt: int) -> ImageFont.FreeTypeFont:
    import quote_renderer  # noqa: PLC0415 — the fonts and their path resolution live there

    return quote_renderer._pil_font(name, pt)


def _attr_geometry(n_lines: int) -> tuple[int, int]:
    """(line pitch, body region height) for an attribution of ``n_lines``.
    ONE copy, read by fit() to decide a size fits and by render() to place
    it — two copies could let render() lay out a block fit() never checked."""
    import quote_renderer  # noqa: PLC0415

    pitch = round(sum(_font(quote_renderer.FONT_CREDITS, ATTR_FONT_PT).getmetrics()) * ATTR_LINE_SPACING)
    attr_h = (n_lines * pitch + ATTR_GAP) if n_lines else 0
    return pitch, HEIGHT - TOP_MARGIN - BOTTOM_MARGIN - attr_h


def _wrap(paragraphs: Iterable[str], font: ImageFont.FreeTypeFont, max_width: int) -> list[str] | None:
    """Greedy wrap on summed word advances (one measure per distinct word,
    not one per growing line — the per-minute path runs on a Pi Zero). The
    drawn line is the joined string, whose kerning across spaces can differ
    from the sum by a pixel; SIDE_MARGIN absorbs that."""
    space = font.getlength(" ")
    widths: dict[str, float] = {}
    lines: list[str] = []
    for para in paragraphs:
        current: list[str] = []
        width = 0.0
        for word in para.split(" "):
            w = widths.get(word)
            if w is None:
                w = widths[word] = font.getlength(word)
            if w > max_width:
                return None  # a single word wider than the canvas
            if current and width + space + w > max_width:
                lines.append(" ".join(current))
                current, width = [], 0.0
            width = width + space + w if current else w
            current.append(word)
        if current:
            lines.append(" ".join(current))
    return lines


def _attribution_lines(attribution: str) -> list[str]:
    import quote_renderer  # noqa: PLC0415

    if not attribution:
        return []
    font = _font(quote_renderer.FONT_CREDITS, ATTR_FONT_PT)
    lines = _wrap([quote_renderer.DASH + attribution], font, ATTR_MAX_WIDTH)
    if lines is None or len(lines) > ATTR_MAX_LINES:
        return []  # drop an unwieldy attribution rather than the fortune
    return lines


@dataclass(frozen=True)
class FortuneLayout:
    font_size: int
    lines: tuple[str, ...]
    attribution: tuple[str, ...]


def fit(fortune: Fortune) -> FortuneLayout | None:
    """The largest size in FS_FLOOR..FS_MAX at which the body fits the canvas
    above the attribution, or None. Bounded above by the poster cap, unlike
    the quote renderer, which grows without limit."""
    import quote_renderer  # noqa: PLC0415

    attr = _attribution_lines(fortune.attribution)
    _, avail_h = _attr_geometry(len(attr))

    def attempt(pt: int) -> FortuneLayout | None:
        font = _font(quote_renderer.FONT_REGULAR, pt)
        lines = _wrap(fortune.paragraphs, font, WIDTH - 2 * SIDE_MARGIN)
        if lines is None:
            return None
        asc, desc = font.getmetrics()
        block = (len(lines) - 1) * round(pt * LINE_HEIGHT_RATIO) + asc + desc
        return FortuneLayout(pt, tuple(lines), tuple(attr)) if block <= avail_h else None

    # Binary search: fit is monotonic in size except at rare wrap boundaries,
    # where this may settle a point or two small. It only ever returns a size
    # it has checked, so the result always fits. Seven font loads, not 35 —
    # and within quote_renderer._pil_font's cache of 8.
    best = None
    lo, hi = FS_FLOOR, FS_MAX
    while lo <= hi:
        mid = (lo + hi) // 2
        layout = attempt(mid)
        if layout is not None:
            best, lo = layout, mid + 1
        else:
            hi = mid - 1
    return best


def render(fortune: Fortune, layout: FortuneLayout | None = None) -> Image.Image:
    """The 800x400 mode-"L" body frame: lines centred, the block centred
    vertically above the attribution, which sits bottom-right like a
    quote's credits."""
    from PIL import Image, ImageDraw  # noqa: PLC0415

    import quote_renderer  # noqa: PLC0415

    layout = layout or fit(fortune)
    if layout is None:
        raise ValueError("fortune does not fit")
    img = Image.new("L", (WIDTH, HEIGHT), 255)
    draw = ImageDraw.Draw(img)
    font = _font(quote_renderer.FONT_REGULAR, layout.font_size)
    asc, desc = font.getmetrics()
    pitch = round(layout.font_size * LINE_HEIGHT_RATIO)
    attr_font = _font(quote_renderer.FONT_CREDITS, ATTR_FONT_PT)
    attr_pitch, region_h = _attr_geometry(len(layout.attribution))
    block = (len(layout.lines) - 1) * pitch + asc + desc
    y = TOP_MARGIN + (region_h - block) // 2 + asc
    for line in layout.lines:
        draw.text((WIDTH // 2, y), line, font=font, fill=0, anchor="ms")
        y += pitch
    base = HEIGHT - BOTTOM_MARGIN - attr_font.getmetrics()[1]
    for i, line in enumerate(reversed(layout.attribution)):
        draw.text((WIDTH - SIDE_MARGIN, base - i * attr_pitch), line, font=attr_font, fill=0, anchor="rs")
    return img


# ─── selection ──────────────────────────────────────────────────────────────


def slot_index(now: datetime, dwell: int) -> int:
    """Wall-clock minutes since the epoch, bucketed by dwell. Local wall time
    on purpose: slots line up with the minutes the panel shows (:00, :05…)."""
    naive = now.replace(tzinfo=None)
    return int((naive - datetime(1970, 1, 1)).total_seconds() // 60) // dwell


def pool(selected: tuple[str, ...], *, allow_nsfw: bool, root: Path | None = None) -> list[Database]:
    """The databases a pick draws from. Ordinary ones: the selection, or all
    of them when nothing is selected. Offensive ones: ONLY when ticked by name
    AND ALLOW_NSFW_QUOTES is on — never through the empty "all" (owner
    decision on the litclock-dev#533 review: the NSFW switch is labelled for frank novels,
    and Debian's offensive tree holds hate-joke files; a default plus one
    unrelated toggle must not reach them)."""
    return [
        db
        for db in list_databases(root, include_offensive=allow_nsfw and bool(selected))
        if ((db.key in selected) if selected else not db.offensive)
    ]


def pick(
    now: datetime,
    *,
    dwell: int = DWELL_DEFAULT,
    selected: tuple[str, ...] = (),
    allow_nsfw: bool = False,
    root: Path | None = None,
    denylist: frozenset[str] | None = None,
    accept: Callable[[Fortune, FortuneLayout], bool] | None = None,
) -> tuple[Fortune, FortuneLayout] | None:
    """The fortune for ``now``'s dwell slot, already fitted, or None when the
    pool is empty, MAX_ATTEMPTS draws found nothing usable, or PICK_BUDGET_S
    ran out. ``accept`` is a last veto (the painter's frame check): a vetoed
    candidate costs one draw, not the slot.

    The budget covers the database scan too (Codex review). It is checked
    before each draw, not after a successful one: a fortune already fitted and
    rendered is cheaper to paint than a quote rendered from scratch."""
    deadline = time.monotonic() + PICK_BUDGET_S
    dbs = pool(selected, allow_nsfw=allow_nsfw, root=root)
    total = sum(db.count for db in dbs)
    if not total:
        # Said, because the painter then shows quotes and nothing else would
        # tell a fortune-mode clock from a quotes one (review).
        logging.warning(
            f"fortune: no databases to draw from (selected={list(selected) or 'all'}, nsfw={allow_nsfw}); "
            "painting a quote"
        )
        return None
    deny = load_denylist() if denylist is None else denylist
    key = f"{slot_index(now, dwell)}|{','.join(d.key for d in dbs)}"
    rng = random.Random(int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big"))
    for _ in range(MAX_ATTEMPTS):
        if time.monotonic() > deadline:
            logging.warning(f"fortune: pick budget {PICK_BUDGET_S}s spent; painting a quote")
            return None
        n = rng.randrange(total)
        for db in dbs:
            if n < db.count:
                break
            n -= db.count
        try:
            raw = _entry_text(db, n)
        except OSError as e:
            logging.warning(f"fortune: {db.path} unreadable ({e})")
            continue
        if entry_hash(raw) in deny:
            continue
        fortune = normalise(raw, db.name, db.offensive)
        # After normalise, which rejects anything long cheaply first.
        if fortune is None or (not allow_nsfw and is_mature(raw)):
            continue
        layout = fit(fortune)
        if layout is not None and (accept is None or accept(fortune, layout)):
            return fortune, layout
    logging.warning(f"fortune: no usable entry in {MAX_ATTEMPTS} draws from {len(dbs)} database(s)")
    return None


def current_fortune(
    now: datetime,
    env: dict | None = None,
    frame_ok: Callable[[Image.Image], str | None] | None = None,
) -> tuple[Fortune, Image.Image] | None:
    """The painter's entry point: settings from the environment, a fitted
    fortune and its mode-"L" frame, or None. ``frame_ok`` returns a rejection
    reason for a rendered frame; a rejected frame moves on to the next
    candidate. Never raises."""
    env = os.environ if env is None else env
    try:
        allow_nsfw = (env.get("ALLOW_NSFW_QUOTES") or "false").strip().lower() == "true"
        frames: dict[int, Image.Image] = {}

        def accept(fortune: Fortune, layout: FortuneLayout) -> bool:
            frame = render(fortune, layout)
            reason = frame_ok(frame) if frame_ok else None
            if reason is not None:
                logging.warning(f"fortune frame rejected ({fortune.database}): {reason}; drawing another")
                return False
            frames[id(fortune)] = frame
            return True

        picked = pick(
            now,
            dwell=dwell_minutes(env.get("LITCLOCK_FORTUNE_DWELL_MINUTES")),
            selected=parse_categories(env.get("LITCLOCK_FORTUNE_CATEGORIES")),
            allow_nsfw=allow_nsfw,
            accept=accept,
        )
        if picked is None:
            return None
        fortune, _ = picked
        return fortune, frames[id(fortune)]
    except Exception as e:
        logging.error(f"fortune: render failed ({e!r})")
        return None


# ─── CLI: curation helpers ──────────────────────────────────────────────────


def _cli(argv: list[str]) -> int:
    """``fortune.py hash`` reads an entry on stdin and prints its denylist
    key. ``fortune.py sweep [--nsfw]`` walks every installed entry and prints
    how many normalise and fit — the curation view of the corpus.
    ``fortune.py blocked`` prints every entry the NSFW-off word filter skips;
    ``fortune.py check [slots]`` picks over a day with NSFW off and on."""
    if argv[:1] == ["hash"]:
        print(entry_hash(sys.stdin.read()))
        return 0
    if argv[:1] == ["sweep"]:
        allow_nsfw = "--nsfw" in argv
        dbs = list_databases(include_offensive=allow_nsfw)
        shown = 0
        total = 0
        for db in dbs:
            ok = 0
            for i in range(db.count):
                raw = _entry_text(db, i)
                f = normalise(raw, db.name, db.offensive)
                f = None if f is None or (not allow_nsfw and is_mature(raw)) else f
                ok += f is not None and fit(f) is not None
            print(f"{'off/' if db.offensive else ''}{db.name}: {ok}/{db.count}")
            shown += ok
            total += db.count
        print(f"total: {shown}/{total} usable")
        return 0 if shown else 1
    if argv[:1] == ["blocked"]:
        # Every entry the NSFW-off word filter keeps off the panel, for review.
        counts: dict[str, int] = {}
        for db in list_databases(include_offensive=False):
            for i in range(db.count):
                raw = _entry_text(db, i)
                if is_mature(raw):
                    counts[db.name] = counts.get(db.name, 0) + 1
                    hits = {m.lower() for text in _match_texts(raw) for m in _MATURE_RE.findall(text)}
                    print(f"=== {db.name} [{', '.join(sorted(hits))}]")
                    print(raw.strip())
        for name, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"{n:5d} {name}")
        print(f"total: {sum(counts.values())} blocked")
        return 0
    if argv[:1] == ["check"]:
        # pick() over a day of slots with NSFW off, then on: the NSFW-off line
        # must show 0 mature, and the NSFW-on line is the control proving the
        # check can fail (litclock-dev#971; CLAUDE.md, Fortune mode).
        slots = int(argv[1]) if len(argv) > 1 else 1440
        t0 = datetime(2026, 1, 1)
        result = {}
        for allow in (False, True):
            picks = mature = 0
            for k in range(slots):
                got = pick(t0 + timedelta(minutes=k), allow_nsfw=allow)
                picks += got is not None
                mature += got is not None and is_mature(got[0].raw)
            print(f"nsfw={'on ' if allow else 'off'}: {picks}/{slots} picks, {mature} mature")
            result[allow] = (picks, mature)
        if result[False][1]:
            return 1
        # Nothing picked, or a control that never fired: the check proved nothing.
        if not result[False][0] or not result[True][1]:
            print("inconclusive: no NSFW-off picks, or the NSFW-on control showed nothing", file=sys.stderr)
            return 2
        return 0
    print(
        "usage: fortune.py hash < entry | fortune.py sweep [--nsfw] | fortune.py blocked | fortune.py check [slots]",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(_cli(sys.argv[1:]))
