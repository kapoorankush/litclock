#!/usr/bin/env python3
"""Which language corpora CI renders, and which CSVs it would miss (litclock-dev#532 T8).

    python3 tools/language_corpora.py      # one code per line; exit 1 listing every problem

Prints every REGISTERED code (registry order), then every unregistered code
with a corpus at the one conventional path, ``languages/<code>/quotes.csv``.
Exits 1 — printing nothing on stdout — when any CSV under ``languages/``
would not be rendered by that list: a file under another name, a
``quotes.csv`` beside a registered language whose corpus is a different
file, or a directory name that is not a language code. Each of those used to
start the render-invariants workflow and pass it green without a row of the
volunteer's file being read (litclock-dev#926 review).

Standalone on purpose (stdlib only): the workflow runs it before anything
else, and its tests run the real file beside a stub renderer.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]

UNREGISTERED_CORPUS = "quotes.csv"
# A BCP 47-shaped code as this project writes it: `es`, `pt-BR` — one optional
# subtag, so `zh-Hant-TW` is out until a language needs it (then widen this,
# with the workflow loop in mind). Also what keeps a directory name with a
# space, a glob character or a leading dash out of the workflow's shell loop.
CODE = re.compile(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})?")


def plan(root: Path | None = None) -> tuple[list[str], list[str]]:
    """``(codes, problems)``: the codes to render, and a line per CSV under
    ``languages/`` that those codes would not cover."""
    root = root if root is not None else _REPO
    registry = json.loads((root / "languages.json").read_text(encoding="utf-8"))["languages"]
    problems = [f"registered code {c!r} is not a language code" for c in registry if not CODE.fullmatch(c)]
    if "en" not in registry:
        problems.append("languages.json does not list English")
    claimed = {
        str(Path(e["corpus"]["path"]))
        for e in registry.values()
        if isinstance(e, dict) and isinstance(e.get("corpus"), dict) and isinstance(e["corpus"].get("path"), str)
    }
    new: set[str] = set()
    languages = root / "languages"
    for path in _walk(languages):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            # rglob does not descend a linked directory, and a linked corpus
            # is edited at a path the workflow filter never sees (litclock-dev#926
            # re-review): nothing under languages/ may be a link.
            problems.append(f"{rel}: a symbolic link — languages/ must hold real files and directories")
            continue
        if not path.is_file() or path.suffix.lower() != ".csv":
            continue
        if rel in claimed:
            continue
        parts = path.relative_to(languages).parts
        code = parts[0]
        if len(parts) != 2 or parts[1] != UNREGISTERED_CORPUS:
            problems.append(f"{rel}: not checked — a new corpus must be languages/<code>/{UNREGISTERED_CORPUS}")
        elif not CODE.fullmatch(code):
            problems.append(f"{rel}: {code!r} is not a language code (lower-case, like es or pt-BR)")
        elif code in registry:
            problems.append(
                f"{rel}: not checked — {code} is registered and its corpus is "
                f"{registry[code].get('corpus', {}).get('path')!r}"
            )
        else:
            new.add(code)
    return [*registry, *sorted(new)], problems


def _walk(top: Path) -> list[Path]:
    """Every entry under ``top``, links included but never followed."""
    if not top.is_dir() or top.is_symlink():
        return [top] if top.is_symlink() else []
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
        here = Path(dirpath)
        found += [here / d for d in dirnames if (here / d).is_symlink()]
        found += [here / f for f in filenames]
    return sorted(found)


def main() -> int:
    try:
        codes, problems = plan()
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"language_corpora: cannot read the registry: {exc}", file=sys.stderr)
        return 1
    if problems:
        for p in problems:
            print(f"::error::{p}", file=sys.stderr)
        return 1
    print("\n".join(codes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
