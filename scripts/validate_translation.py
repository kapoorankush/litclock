#!/usr/bin/env python3
"""Validate a LitClock translation bundle before opening a PR (litclock-dev#532).

A volunteer translator runs this on their own ``languages/<code>/strings.json``
and sees, per key, exactly what CI will check — WITHOUT installing pytest or
knowing the repo internals. It is the SAME rules the CI gate runs: both import
``src/catalog_lint.py``, so a green run here means a green gate there.

    python3 scripts/validate_translation.py es
    python3 scripts/validate_translation.py --file /path/to/es/strings.json es

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
            f"NOTE  {code} needs plural forms {extra_cats} that this kit can't generate yet "
            "(it only has English's one/other). Finish the other strings, but coordinate the "
            "plural keys with the maintainer — litclock-dev#532."
        )

    # Key-set parity (Stage 3).
    missing = set(en) - set(data)
    unknown = set(data) - set(en)
    for k in sorted(missing):
        findings.append(f"MISSING KEY  {k}  (English: {en[k]!r})")
    for k in sorted(unknown):
        findings.append(f"UNKNOWN KEY  {k}  (not in the English catalog — remove it)")

    # Non-string / empty values.
    for k, v in data.items():
        if not isinstance(v, str):
            findings.append(f"NOT A STRING {k}  (must be a text value)")
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

    # Per-value content lint (Stage 4) — the shared rules.
    capable = catalog_lint.rich_capable_keys(_REPO_ROOT)
    forms_set = set(forms)
    for key in sorted(set(data) & set(en)):
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a LitClock translation bundle.")
    parser.add_argument("code", help="language code, e.g. es")
    parser.add_argument("--file", help="path to the strings.json (default: the registry path for <code>)")
    args = parser.parse_args(argv)

    try:
        findings = validate(args.code, args.file)
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not findings:
        print(f"✓ {args.code}: conformant — every check the CI gate runs passes.")
        return 0
    print(f"✗ {args.code}: {len(findings)} finding(s) — fix these before opening a PR:\n")
    for f in findings:
        print(f"  {f}")
    print("\n(This is the same lint CI runs. Green here = green there.)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
