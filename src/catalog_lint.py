"""Catalog content-lint rules — the single source for the Stage-4 gates.

litclock-dev#532: these functions are the AUTHORITATIVE conformance rules
for a translation bundle. They live here (not inside the test) so three
consumers share ONE implementation and can never drift:

  - ``tests/test_strings_catalog.py`` — the CI gate (mutation-verified).
  - ``scripts/validate_translation.py`` — the standalone validator a
    volunteer runs on their own bundle before opening a PR.
  - ``scripts/build_translator_kit.py`` — derives the worksheet, the es
    skeleton, and the HTML tool's embedded data from the SAME metadata.

The Stage-3 gate (key-set parity, plural completeness, coverage floor)
stays in the test — it is a property of the registry as a whole, checked
once in CI, not a per-value rule a translator needs at their fingertips.

Stdlib only, no side effects at import: this is tooling, importable from
the Pi venv, a bare python3, or a script with no repo on sys.path.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Final

# Single source for the vocabulary: the tuples AND the token regex derive
# from this one name list (the gate /review: three restatements let a
# partial update pass an unbalanced token silently). The test pins these
# against the PRODUCTION resolver sources, so a vocab shrink there — the
# one silent drift quadrant — goes red.
RICH_TOKEN_NAMES: Final[tuple[str, ...]] = ("b", "em", "code")
RICH_VOCAB_FULL: Final[tuple[str, ...]] = RICH_TOKEN_NAMES
# setup_server._rich converts ONLY {b}; a token outside the key's resolver
# vocabulary renders literally.
RICH_VOCAB_SETUP: Final[tuple[str, ...]] = ("b",)
# Slot alphabet: lowercase + digits, letter-first ({0}-style numbered slots
# stay rejected by the Stage-3 gate). Out-of-alphabet names ({Name}, { b })
# fall to the stray-brace rule — loud, though the message points at braces.
SLOT_RE: Final[re.Pattern[str]] = re.compile(r"\{([a-z][a-z0-9_]*)\}")
TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"\{(/?)(" + "|".join(RICH_TOKEN_NAMES) + r")\}")

# Character-class lint (the gate's adversarial /review, F1-F3: all three
# shipped a defective bundle through the first cut with zero findings).
CONFUSABLE_BRACES: Final[str] = "｛｝❴❵⦃⦄"
BIDI_CONTROLS: Final[str] = "".join(chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A)))
# Invisible-but-not-isspace codepoints that defeat value.strip() checks.
INVISIBLE_EXTRAS: Final[str] = "​⠀ㅤﾠ"
ENTITY_RE: Final[re.Pattern[str]] = re.compile(r"&(?:[a-zA-Z][a-zA-Z0-9]*|#[0-9]+|#[xX][0-9a-fA-F]+);")
# Plural categories whose CLDR range is a single integer — only there can a
# translation spell the number ("hace un minuto") and legitimately drop {n}.
# .other/.few/.many serve unbounded n and must keep every en slot.
SINGLE_VALUED_CATEGORIES: Final[frozenset[str]] = frozenset({"one", "two"})


def slot_counts(value: str) -> dict[str, int]:
    """Per-slot occurrence COUNTS (not a set: four templates split-render a
    slot exactly once — base/settings/diagnostics/_handoff-banner `.split(
    '{x}')` rendering [0]/[1] — so a translation repeating a slot en uses
    once would silently truncate its tail; Codex, the gate /review)."""
    counts: dict[str, int] = {}
    for m in SLOT_RE.findall(value):
        if m not in RICH_TOKEN_NAMES:
            counts[m] = counts.get(m, 0) + 1
    return counts


def rich_capable_keys(repo_root: Path) -> dict[str, tuple[str, ...]]:
    """Source-scanned map of key → allowed rich-token vocabulary.

    Derived from the CALL SITES, not from which en values happen to carry
    tokens today — a resolver-routed key with a plain en value (e.g.
    setup.banner.advice_neutral) may legitimately gain {b} in translation,
    while a token in any OTHER key would reach the page unconverted.

    Comments are stripped before scanning (the gate /review: a retired
    call site surviving in a {# jinja comment #} or a # python comment
    would keep granting its key a vocabulary). The scan has no eyes on the
    JS side — if a JS token resolver ever appears, add its scan here.
    """
    capable: dict[str, tuple[str, ...]] = {}

    def _grant(key: str, vocab: tuple[str, ...]) -> None:
        if key in capable:
            # A dual-routed key gets the INTERSECTION — every resolver that
            # serves it must convert every token it carries.
            vocab = tuple(t for t in capable[key] if t in vocab)
        capable[key] = vocab

    for path in (repo_root / "src" / "control_server" / "templates").rglob("*.j2"):
        text = re.sub(r"\{#.*?#\}", "", path.read_text(encoding="utf-8"), flags=re.S)
        for key in re.findall(r"""t_rich\(\s*['"]([A-Za-z0-9_.]+)['"]""", text):
            _grant(key, RICH_VOCAB_FULL)
    setup_src = (repo_root / "src" / "setup_server.py").read_text(encoding="utf-8")
    setup_src = "\n".join(ln for ln in setup_src.splitlines() if not ln.lstrip().startswith("#"))
    for key in re.findall(r"""(?<![A-Za-z0-9_])_rich\(\s*['"]([A-Za-z0-9_.]+)['"]""", setup_src):
        _grant(key, RICH_VOCAB_SETUP)
    return capable


def plural_sibling_slots(key: str, forms: set[str], en_data: dict) -> set[str] | None:
    """For a plural-category key, the extra slot names a translation may use.

    ``plural()`` always supplies {n}, and a language may need a slot in a
    category where English spells the word instead — so for plural keys
    every sibling category's slots (plus {n}) are ALLOWED. Whether {n} is
    REQUIRED is per-category (see SINGLE_VALUED_CATEGORIES). None means
    the key is not a plural-category key.
    """
    base, _, last = key.rpartition(".")
    if last not in forms or not base:
        return None
    slots = {"n"}
    for k, v in en_data.items():
        b, _, cat = k.rpartition(".")
        if b == base and cat in forms:
            slots |= set(slot_counts(v))
    return slots


def value_errors(
    value: str,
    *,
    en_value: str | None,
    extra_allowed_slots: set[str] | None = None,
    relax_slots: set[str] | None = None,
    vocab: tuple[str, ...] = (),
) -> list[str]:
    """Every Stage-4 content-lint finding for ONE catalog value.

    ``relax_slots`` names slots whose occurrence count is unconstrained
    (the {n} of a single-valued plural category); ``extra_allowed_slots``
    may appear any number of times but are never required (plural sibling
    slots). All other slots must match the English value's counts exactly.
    """
    errors: list[str] = []
    if "<" in value or ">" in value:
        # Kills raw tags, </script> inside json.dumps'd inline-JS literals,
        # and markup in the setup page's raw-splice element contexts (CNA
        # bridge, language section) in one rule: catalog values are plain
        # text; markup is expressed only via the whitelisted tokens.
        errors.append("raw '<' or '>' — markup is expressed only via rich tokens")
    if ENTITY_RE.search(value):
        errors.append("HTML entity — it renders literally (escaped) or as fake markup; write the character itself")
    bad_braces = sorted({ch for ch in value if ch in CONFUSABLE_BRACES})
    if bad_braces:
        errors.append(f"brace-confusable character(s) {bad_braces} — no resolver converts these; use ASCII {{ }}")
    bad_bidi = sorted({f"U+{ord(ch):04X}" for ch in value if ch in BIDI_CONTROLS})
    if bad_bidi:
        errors.append(f"bidi control character(s) {bad_bidi} — an unpaired override garbles or spoofs the page/panel")
    bad_ctl = sorted({f"U+{ord(ch):04X}" for ch in value if (ord(ch) < 0x20 and ch != "\n") or ch == "\x7f"})
    if bad_ctl:
        errors.append(f"control character(s) {bad_ctl} — only \\n is allowed")

    if value and all(
        unicodedata.category(ch) in ("Cf", "Zs", "Cc", "Zl", "Zp") or ch in INVISIBLE_EXTRAS for ch in value
    ):
        errors.append("no visible content — blank parts are expressed by key ABSENCE, never an invisible value")

    en_counts = dict(slot_counts(en_value or ""))
    counts = dict(slot_counts(value))
    allowed_names = set(en_counts) | set(extra_allowed_slots or ()) | set(relax_slots or ())
    for name in relax_slots or ():
        en_counts.pop(name, None)
        counts.pop(name, None)
    for name in extra_allowed_slots or ():
        if name not in en_counts:
            counts.pop(name, None)
    missing = {s: c for s, c in en_counts.items() if counts.get(s, 0) < c}
    extra = {s: c for s, c in counts.items() if c > en_counts.get(s, 0)}
    if missing:
        errors.append(f"drops slot(s) {sorted(missing)} that the English value carries")
    if extra:
        errors.append(
            f"slot(s) {sorted(extra)} exceed the English occurrence count — "
            "unknown slots never fill; duplicates truncate split-rendered copy"
        )

    stack: list[str] = []
    for m in TOKEN_RE.finditer(value):
        closing, name = m.group(1), m.group(2)
        if name not in vocab:
            shown = ("{/" if closing else "{") + name + "}"
            errors.append(f"rich token {shown} outside this key's resolver vocabulary {vocab or '(none)'}")
            continue
        if not closing:
            stack.append(name)
        elif not stack or stack.pop() != name:
            errors.append(f"rich token {{/{name}}} closes nothing or crosses another token's span")
    if stack:
        errors.append(f"unclosed rich token(s) {stack}")

    residue = value
    for name in vocab:
        residue = residue.replace("{" + name + "}", "").replace("{/" + name + "}", "")
    for name in sorted(allowed_names):
        residue = residue.replace("{" + name + "}", "")
    if "{" in residue or "}" in residue:
        errors.append("stray brace(s) — would render literally (typo'd slot, or a token this key can't carry)")
    return errors


def registry_errors(strings_root: Path, source_root: Path | None = None) -> list[str]:
    """Run the value lint over every language in the registry at ``strings_root``.

    Returns a flat ``code:key: message`` list — empty means every bundle
    conforms. ``strings_root`` is where ``languages.json`` and the bundles
    live (the CI repo, or a volunteer's work-in-progress checkout).
    ``source_root`` is where the rich-token vocabulary is scanned from —
    the PRODUCTION resolver call sites, always the real source tree, never
    the translation bundle. It defaults to ``strings_root`` (the normal
    case); a test that lints a synthetic bundle against the real resolvers
    passes the repo as ``source_root``.
    """
    capable = rich_capable_keys(source_root if source_root is not None else strings_root)
    registry = json.loads((strings_root / "languages.json").read_text(encoding="utf-8"))["languages"]

    def _strings(entry: dict) -> dict:
        rel = Path(entry["strings"])
        return json.loads((rel if rel.is_absolute() else strings_root / rel).read_text(encoding="utf-8"))

    en_data = {k: v for k, v in _strings(registry["en"]).items() if not k.startswith("_")}
    findings: list[str] = []
    for code, entry in registry.items():
        data = _strings(entry)
        forms = set(entry.get("plural_forms") or ())
        for key, value in data.items():
            if key.startswith("_") or not isinstance(value, str):
                continue
            sibling = plural_sibling_slots(key, forms, en_data)
            category = key.rpartition(".")[2]
            relax = {"n"} if sibling is not None and category in SINGLE_VALUED_CATEGORIES else None
            for err in value_errors(
                value,
                en_value=en_data.get(key),
                extra_allowed_slots=sibling,
                relax_slots=relax,
                vocab=capable.get(key, ()),
            ):
                findings.append(f"{code}:{key}: {err}")
    return findings
