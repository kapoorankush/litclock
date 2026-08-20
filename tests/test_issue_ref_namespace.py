"""Bare `#NNN` in this repo must not silently resolve to the wrong issue.

litclock-dev#658. Most of this codebase was written in the development repo,
where `#82` correctly means development issue 82. Ported here it means
*whatever this repo's issue 82 turns out to be* — and GitHub renders a wrong
link exactly like a right one, so the damage is invisible. Two qualification
passes had already been run by hand and neither stuck, because nothing stopped
new ones arriving: requalification happens on the porting path, and anything
authored directly here skips it.

The rule this enforces: a bare `#NNN` is allowed only while N is small enough
to plausibly BE an issue in this repo. Anything above the ceiling below has to
be written `litclock-dev#NNN`.

Bumping `PUBLIC_NUMBER_CEILING` is a deliberate act, not a chore. Raising it to
N says "this repo now has an issue N, so a bare `#N` is ambiguous and I have
checked the ones in the tree." That is the judgement a regex cannot make, which
is why the ceiling is a constant a human moves rather than a live API call.
"""

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Highest issue-or-PR number in THIS repo as of the litclock-dev#658 audit
# (2026-08-20: highest issue 57, highest PR 59).
PUBLIC_NUMBER_CEILING = 59

# EVERY tracked text file, not an extension allowlist. The first version of
# this listed ten extensions, inherited from the audit's own scan command, and
# review found 40 genuine references sitting in the file types it did not name:
# .php, .conf, .json, .in, .txt, .sample and extensionless scripts. An
# allowlist of file types is a list of the places you remembered.

# The three shapes a reference is written in here. They are separate patterns
# on purpose: a single widened one would also match URL fragments and the tail
# of hex colours.
_SHAPES = (
    re.compile(r"(?<![-A-Za-z0-9/#])#(\d{2,4})"),   # plain: `(#337)`
    re.compile(r"(?<=[A-Za-z])-#(\d{2,4})"),        # idiom: `pre-#337`, `post-#209`
    re.compile(r"(?<=\d)/#(\d{2,4})"),              # chain: `#309/#362/#364`
)

# A CSS declaration value. EVERY three-digit issue number is also a valid CSS
# hex colour, and an earlier attempt at this requalification turned
# `color:#555` into `color:litclock-dev#555` and shipped invalid CSS to every
# first-boot user. Two boundary details are load-bearing: the `--custom-prop`
# arm needs a real token boundary or `<!-- Backfill:` in an HTML comment
# matches it, and a quote counts as one or `style="color:#555"` slips past.
_CSS_VALUE = re.compile(
    r"(?:(?:^|[\s{;(\"'])(?:color|background|background-color|fill|stroke|outline|box-shadow|text-shadow)"
    r"|(?:^|[\s{;\"'])--[A-Za-z0-9-]+|(?:^|[\s{;\"'])border[a-z-]*)\s*:\s*[^;{]*$"
)


def _is_reference(line, start, end):
    """False for the things that merely LOOK like a reference."""
    if start > 0 and line[start - 1] == "&":
        return False                                   # `&#8209;` — an HTML entity
    if end < len(line) and line[end] in "0123456789abcdefABCDEF":
        return False                                   # `#14110D`, and `#431b` sub-refs
    return not _CSS_VALUE.search(line[:start])         # `color: #333`


def find_unqualified_refs():
    """(path, lineno, ref, line) for every bare ref above the ceiling."""
    files = subprocess.run(
        ["git", "ls-files"],
        capture_output=True, text=True, cwd=REPO_ROOT, check=True,
    ).stdout.split()
    hits = []
    for rel in files:
        # This file is EXEMPT, and it has to be: its docstrings explain the
        # problem and its fixtures are literal bare references, so scanning it
        # would report its own test data forever. A checker does not lint its
        # own fixtures. The exemption is narrow — one path, named here — rather
        # than a directory or a magic comment, so it cannot quietly grow.
        if rel == "tests/test_issue_ref_namespace.py":
            continue
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        try:
            with open(path, encoding="utf-8", newline="") as fh:
                content = fh.read()
        except (UnicodeDecodeError, OSError):
            continue                                   # binary or unreadable
        if "\0" in content[:4000]:
            continue                                   # binary despite decoding
        for lineno, line in enumerate(content.split("\n"), 1):
            for shape in _SHAPES:
                for m in shape.finditer(line):
                    n = int(m.group(1))
                    if n <= PUBLIC_NUMBER_CEILING:
                        continue
                    if not _is_reference(line, m.start(1) - 1, m.end(1)):
                        continue
                    hits.append((rel, lineno, f"#{n}", line.strip()[:120]))
    return hits


def test_no_unqualified_development_issue_refs():
    hits = find_unqualified_refs()
    if hits:
        listing = "\n".join(f"  {p}:{ln}  {ref}\n      {text}" for p, ln, ref, text in hits[:25])
        more = f"\n  … and {len(hits) - 25} more" if len(hits) > 25 else ""
        raise AssertionError(
            f"{len(hits)} bare issue reference(s) above #{PUBLIC_NUMBER_CEILING}.\n"
            "A bare `#NNN` renders as a link to THIS repo's issue NNN. Development-repo "
            "references must be written `litclock-dev#NNN` so they resolve, and so they "
            "cannot silently start pointing at an unrelated issue as this repo's numbering "
            f"grows (litclock-dev#658).\n{listing}{more}"
        )


def test_the_detector_would_catch_a_reintroduction():
    """Mutate the input and watch it fire, or it is not a guard.

    Covers all three shapes, because each was found in the tree and each needs
    its own pattern.
    """
    for line in ("see #337 for detail", "the pre-#337 layout", "the #309/#362 wave"):
        assert any(
            _is_reference(line, m.start(1) - 1, m.end(1)) and int(m.group(1)) > PUBLIC_NUMBER_CEILING
            for shape in _SHAPES
            for m in shape.finditer(line)
        ), f"a reintroduced bare reference would not be caught: {line!r}"


def test_the_detector_leaves_alone_what_only_looks_like_a_reference():
    """The false-positive classes that made the first attempt at this ship
    broken CSS."""
    for line in ("            color: #333;", 'style="color:#555;"', "Wi&#8209;Fi", "  --bg: #14110D;"):
        assert not any(
            _is_reference(line, m.start(1) - 1, m.end(1))
            for shape in _SHAPES
            for m in shape.finditer(line)
        ), f"would have rewritten a non-reference: {line!r}"


def test_references_at_or_below_the_ceiling_are_left_alone():
    """`#25` plausibly IS an issue in this repo, so a regex must not touch it."""
    hits = [h for h in find_unqualified_refs() if int(h[2][1:]) <= PUBLIC_NUMBER_CEILING]
    assert hits == []
