#!/usr/bin/env python3
"""Validate a LitClock strings bundle (litclock-dev#532).

The maintainer writes every language's UI strings (design D12); this is the
check they run on ``languages/<code>/strings.json``, and the one a volunteer
reviewing those strings can run too — WITHOUT installing pytest or knowing the
repo internals. It is the SAME rules the CI gate runs: both import
``src/catalog_lint.py``, so a green run here means a green gate there.

    python3 scripts/validate_translation.py es
    python3 scripts/validate_translation.py --file /path/to/es/strings.json es
    python3 scripts/validate_translation.py --all

``--all`` checks every ``languages/*/strings.json`` in the checkout,
registered or not (T8) — by hand what tests/test_translator_kit.py's
TestEveryBundleInTheTree runs in CI through ``validate_all()``, so a bundle
cannot dodge the gate by not being listed in ``languages.json`` yet.

Its OUTPUT is read by people outside this repository, so it names no
private issue: point at translations/README.md instead.

Exit code 0 = conformant, 1 = findings, 2 = usage/IO error. Checks:
  - key-set parity with English (missing keys, unknown keys)
  - plural-category completeness (es needs .one AND .other)
  - per-value content lint (slots, rich tokens, braces, markup, char classes)
It does NOT check corpus coverage — that is a registry-level CI gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
# At import, from the script's own checkout — not inside validate(), where
# _REPO_ROOT may name a bundle tree with no tools/ (the tests repoint it) and
# the font check would quietly skip itself.
sys.path.insert(0, str(_REPO_ROOT / "tools"))

import catalog_lint  # noqa: E402


def _has_bom(path: Path) -> bool:
    with open(path, "rb") as f:
        return f.read(3) == b"\xef\xbb\xbf"


def _load(path: Path) -> dict:
    # Parse tolerantly (utf-8-sig strips a BOM) so the volunteer still gets
    # per-key feedback — but a BOM is reported as a finding by validate(),
    # because the runtime + CI read plain utf-8 and a BOM makes json.load
    # reject the file. Silently accepting it here would false-green vs CI.
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def _en_data() -> dict[str, str]:
    reg = _load(_REPO_ROOT / "languages.json")["languages"]["en"]
    data = _load(_REPO_ROOT / reg["strings"])
    return {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, str)}


def _registry_entry(code: str) -> dict | None:
    reg = _load(_REPO_ROOT / "languages.json")["languages"]
    return reg.get(code)


def _bundle_path(code: str, override: str | None) -> Path:
    if override:
        return Path(override)
    entry = _registry_entry(code)
    if entry and isinstance(entry.get("strings"), str):
        rel = Path(entry["strings"])
        return rel if rel.is_absolute() else _REPO_ROOT / rel
    return _REPO_ROOT / "languages" / code / "strings.json"


# CLDR plural categories for languages likely to be contributed whose systems
# EXCEED English's one/other. The kit's skeleton + worksheet only carry en's
# categories, so until litclock-dev#532's registry-schema decision (plural-base
# equivalence / synthetic en mirrors), these languages cannot be completed from
# this kit alone — the validator says so instead of falsely passing.
_CLDR_EXTRA_CATEGORIES: dict[str, list[str]] = {
    "ru": ["few", "many"], "uk": ["few", "many"], "be": ["few", "many"],
    "pl": ["few", "many"], "cs": ["few"], "sk": ["few"], "hr": ["few"],
    "sr": ["few"], "bs": ["few"], "lt": ["few"], "lv": ["zero"],
    "ro": ["few"], "sl": ["two", "few"], "ar": ["zero", "two", "few", "many"],
    "ga": ["two", "few", "many"], "cy": ["two", "few", "many"],
}


def _plural_forms(code: str) -> list[str]:
    entry = _registry_entry(code)
    forms = entry.get("plural_forms") if entry else None
    return forms if isinstance(forms, list) and forms else ["one", "other"]


def validate(code: str, bundle_file: str | None) -> list[str]:
    """Return a flat list of human-readable findings (empty = conformant)."""
    en = _en_data()
    path = _bundle_path(code, bundle_file)
    if not path.is_file():
        return [f"bundle not found: {path}"]
    try:
        raw = _load(path)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return [f"{path.name} is not valid JSON: {exc}"]
    if not isinstance(raw, dict):
        return [f"{path.name} must be a JSON object (curly braces at the top), not a {type(raw).__name__}"]

    data = {k: v for k, v in raw.items() if not k.startswith("_")}
    findings: list[str] = []

    if _has_bom(path):
        findings.append(
            "BOM  your file starts with a byte-order mark (Windows Notepad adds one). "
            "Save it as UTF-8 WITHOUT a BOM, or CI will reject it as invalid JSON."
        )

    extra_cats = _CLDR_EXTRA_CATEGORIES.get(code)
    if extra_cats and not _registry_entry(code):
        findings.append(
            f"NOTE  {code} needs plural forms {extra_cats} that the catalog does not support yet "
            "(English's one/other only), so its count strings cannot be completed — see "
            "translations/README.md."
        )

    # Key-set parity (Stage 3).
    # The numbered farewell lines are optional past the first, and a language
    # may supply more than English's five (litclock-dev#532 T7).
    missing = {k for k in set(en) - set(data) if not catalog_lint.is_optional_key(k)}
    unknown = {k for k in set(data) - set(en) if not catalog_lint.is_farewell_key(k)}
    for k in sorted(missing):
        findings.append(f"MISSING KEY  {k}  (English: {en[k]!r})")
    for k in sorted(unknown):
        findings.append(f"UNKNOWN KEY  {k}  (not in the English catalog — remove it)")

    # Non-string / empty values.
    for k, v in data.items():
        if not isinstance(v, str):
            findings.append(f"NOT A STRING {k}  (must be a text value)")
        elif not v.strip() and catalog_lint.is_optional_key(k):
            continue  # an optional farewell line left blank is simply not supplied (litclock-dev#532 T7)
        elif not v.strip():
            findings.append(f"EMPTY        {k}  (translate it, or drop the key if English has no such part)")

    # Plural completeness (Stage 3).
    forms = _plural_forms(code)
    en_bases = {k.rsplit(".", 1)[0] for k in en if k.rsplit(".", 1)[-1] in forms}
    for base in sorted(en_bases):
        for form in forms:
            key = f"{base}.{form}"
            if key not in data:
                findings.append(f"MISSING PLURAL {key}  ({code} needs every category: {', '.join(forms)})")

    # The control app's web fonts are Latin-1 subsets (design D16): a letter
    # they lack is drawn mid-word in a fallback font. fontTools is a dev
    # dependency; without it the check is skipped with a note, not failed.
    try:
        import pwa_glyphs  # noqa: PLC0415

        cmaps = pwa_glyphs.pwa_cmaps()
        faces = pwa_glyphs.eink_cmaps()
    except Exception as exc:  # noqa: BLE001 — no fontTools/brotli, or an unreadable font: skip, say so
        findings.append(
            f"NOTE  font check skipped ({type(exc).__name__}: {exc}); "
            "pip install 'fonttools[woff]' to check your text against the clock's and the app's fonts"
        )
        cmaps = None
    if cmaps is not None:
        for key in sorted(data):
            value = data[key]
            if key.startswith("_") or not isinstance(value, str):
                continue
            gaps = pwa_glyphs.pwa_missing_letters(value, cmaps)
            if gaps:
                chars = sorted({ch for chars in gaps.values() for ch in chars})
                findings.append(
                    f"FONT         {key}  the control app's fonts can't draw {''.join(chars)!r} — "
                    "your language needs a font change first (see translations/README.md)"
                )
            if key.split(".", 1)[0] in pwa_glyphs.EINK_NAMESPACES:
                missing = pwa_glyphs.eink_missing_chars(value, faces)
                if missing:
                    chars = sorted({ch for found in missing.values() for ch in found})
                    findings.append(
                        f"FONT         {key}  the clock's screen can't draw {''.join(chars)!r} — "
                        "it would show an empty box; use a plain character instead"
                    )

    # Per-value content lint (Stage 4) — the shared rules.
    capable = catalog_lint.rich_capable_keys(_REPO_ROOT)
    forms_set = set(forms)
    # Extra farewell lines past English's count get the same content lint
    # (their English value is absent, so they may carry no slot): litclock-dev#923 review.
    for key in sorted((set(data) & set(en)) | {k for k in data if catalog_lint.is_farewell_key(k)}):
        value = data[key]
        if not isinstance(value, str) or not value.strip():
            # Non-strings and empties are already reported above; running the
            # content lint on a blank work-in-progress value only adds noise.
            continue
        sibling = catalog_lint.plural_sibling_slots(key, forms_set, en)
        category = key.rpartition(".")[2]
        relax = {"n"} if sibling is not None and category in catalog_lint.SINGLE_VALUED_CATEGORIES else None
        for err in catalog_lint.value_errors(
            value,
            en_value=en.get(key),
            extra_allowed_slots=sibling,
            relax_slots=relax,
            vocab=capable.get(key, ()),
        ):
            findings.append(f"{key}: {err}")
    return findings


def _is_note(finding: str) -> bool:
    # Classified on the bare finding, BEFORE validate_all() adds its code
    # prefix: a prefix such as `NOTE: ` (a directory named NOTE) would
    # otherwise read as a note and hide that bundle's failures (litclock-dev#926 review).
    return finding.startswith("NOTE")


def bundle_files() -> dict[str, Path]:
    """Every ``languages/<dir>/strings.json`` in the checkout, by directory
    name — from the TREE, not the registry, so an unregistered bundle is
    found too."""
    return {p.parent.name: p for p in sorted((_REPO_ROOT / "languages").glob("*/strings.json"))}


def validate_all() -> list[tuple[str, str]]:
    """``(code, finding)`` for every bundle in the tree, NOTE lines included
    (the caller decides what fails). A directory whose name is not a language
    code is a finding of its own. Fails closed on a tree with no English: a
    gate that checked nothing must not read as a pass."""
    import language_corpora  # noqa: PLC0415 — tools/, on sys.path above

    bundles = bundle_files()
    if "en" not in bundles:
        return [("en", f"no languages/en/strings.json under {_REPO_ROOT} — nothing was checked")]
    findings: list[tuple[str, str]] = []
    for code, path in bundles.items():
        if not language_corpora.CODE.fullmatch(code):
            findings.append((code, f"languages/{code}/ is not a language code (lower-case, like es or pt-BR)"))
            continue
        findings.extend((code, f) for f in validate(code, str(path)))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a LitClock strings bundle.")
    parser.add_argument("code", nargs="?", help="language code, e.g. es")
    parser.add_argument("--file", help="path to the strings.json (default: the registry path for <code>)")
    parser.add_argument("--all", action="store_true", help="check every languages/*/strings.json, registered or not")
    args = parser.parse_args(argv)
    if args.all == bool(args.code) or (args.all and args.file):
        parser.error("give a language code, or --all on its own")

    try:
        if args.all:
            tagged = validate_all()
        else:
            tagged = [(args.code, f) for f in validate(args.code, args.file)]
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    label = "every bundle" if args.all else args.code

    # NOTE lines are information, not failures: a skipped optional check must
    # not fail a conformant bundle (litclock-dev#925 review).
    def shown(code: str, f: str) -> str:
        return f"{code}: {f}" if args.all else f

    notes = [shown(c, f) for c, f in tagged if _is_note(f)]
    findings = [shown(c, f) for c, f in tagged if not _is_note(f)]
    for n in notes:
        print(f"  {n}")
    if not findings:
        print(f"✓ {label}: conformant — every check the CI gate runs passes.")
        return 0
    print(f"✗ {label}: {len(findings)} finding(s) — fix these before opening a PR:\n")
    for f in findings:
        print(f"  {f}")
    print("\n(This is the same lint CI runs. Green here = green there.)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
