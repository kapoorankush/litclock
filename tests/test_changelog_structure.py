"""Structural guards on CHANGELOG.md (litclock-dev#697).

This repo's `## [Unreleased]` carried two `### Changed` headings (introduced
2026-08-11 in df01118e, #48) — the same defect litclock-dev#697 found and
litclock-dev#698 fixed in the development repo, never ported here. Running
this guard on arrival also flagged `[v0.212.0]`: the RELEASED section had
carried duplicate `### Fixed` and `### For contributors` headings unnoticed
the whole time. In both cases the file asserted two different groups for one
release, and a reader had no way to tell which of them shipped. That is a
defect in the record itself, which is the changelog's only job.

**It is not a rendering fix, and the first version of this file claimed it was.**
The device's update card is byte-identical before and after the merge, for two
independent reasons, both measured rather than assumed:

  1. `fetch_release_notes` is only ever called with a tag from
     `fetch_latest_release_tag()` (`^v\\d+\\.\\d+\\.\\d+$`), so `[Unreleased]`
     is never fetched for any owner, under either heading.
  2. `_extract_changelog_section` caps at the first 10 non-empty lines of the
     WHOLE section, not per subheading. The first `### Changed` block's bullets
     already exhaust that budget, so everything after them is off the card
     whether it sits under one heading or two -- including after the section is
     promoted to a release tag.

What hides content from an owner is that cap. This guard does not touch it and
must not be read as covering it. `scripts/check-changelog-section.py` guards the
adjacent failure (a tag cut while its content is still under `[Unreleased]`
ships a blank card); a budget guard on the first ten lines would be a third,
separate thing, and does not exist.

Deliberately NOT enforced at release-cut time: the release script refuses two
`## [Unreleased]` headings, because promoting the wrong one releases the wrong
notes. A repeated `###` is formatting, and a formatting rule enforced at tag
time is one you meet at the worst possible moment. Here it fails on the commit
that introduces it.
"""

import collections
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

# CommonMark: an ATX heading may be indented up to three spaces (a fourth makes
# it an indented code block), and the delimiter after the `#`s may be a space or
# a TAB. Matching bare `startswith("### ")` missed both, and each miss is a
# fail-open: a duplicate hidden behind one space or one tab is a duplicate this
# guard reports as absent.
_H2_RE = re.compile(r"^ {0,3}##[ \t]")
_H3_RE = re.compile(r"^ {0,3}###[ \t]")
_FENCE_RE = re.compile(r"^ {0,3}(```|~~~)")
# An ATX closing sequence: `### Fixed ###` renders as the heading "Fixed". It
# must be preceded by whitespace to count, so `### Fixed#` is the heading
# "Fixed#" and is left alone.
_CLOSING_SEQ_RE = re.compile(r"\s#+\s*$")


class UnterminatedFence(Exception):
    """Raised rather than returning a clean result -- see the note in the scanner."""


def _heading_text(line: str) -> str:
    """The rendered text of an ATX heading, normalized for comparison.

    Trailing whitespace and a closing `#` sequence are removed, so
    `### Fixed`, `###\tFixed  ` and `### Fixed ###` are one heading rather than
    three. GitHub renders them identically; a comparison that does not is a
    comparison a duplicate can hide from.
    """
    text = line.strip().lstrip("#")
    text = _CLOSING_SEQ_RE.sub("", text)
    return " ".join(text.split())


def find_duplicate_subheadings(text: str) -> list[tuple[str, str]]:
    """Return `(section heading, repeated subheading)` for every `###` that
    appears twice inside one `##` section, in file order.

    A list of pairs rather than a dict keyed by section title, because two
    sections can carry the SAME title -- `## [Unreleased]` twice is exactly the
    bad-merge state the release script exists to refuse -- and a dict would
    bucket them together and report a duplicate that exists in neither.

    Fenced code blocks are skipped. Both directions of that matter and both are
    pinned below: a fenced `### Changed` example must not be reported (a false
    alarm on a correct file, landing as red CI on an unrelated docs commit), and
    a fenced `## ` example must not end the enclosing section, which would
    silently disarm the guard for the rest of that release.

    An UNTERMINATED fence raises instead of returning. Everything after the last
    unmatched fence marker is unscanned, so returning `[]` there is the guard
    reporting success over a region it never looked at -- the one failure
    direction that must never be quiet. The fence tracking is a simple toggle
    and does not implement CommonMark's rule that a closing fence must be at
    least as long as its opener; a ```` ```` ```` block closed by ``` therefore
    mis-pairs, which shows up as either a false alarm or this exception, never
    as a silent pass.

    `#### ` is not a match, and the required delimiter after the third `#` is
    the only reason: `#### x` has a fourth `#` where the pattern wants a space
    or tab. A negative lookahead was added on top of that and removed again once
    a mutation probe showed deleting it left every test green -- a guard that
    cannot fail is not a guard.
    """
    section: tuple[int, str] | None = None
    seen: dict[tuple[int, str], list[str]] = collections.defaultdict(list)
    order: list[tuple[int, str]] = []
    # Compare on normalized text, REPORT the line as written -- a failure
    # message that says `### Fixed` can be grepped for; one that says `Fixed`
    # cannot.
    as_written: dict[str, str] = {}
    in_fence = False
    for index, line in enumerate(text.splitlines()):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _H2_RE.match(line):
            section = (index, _heading_text(line))
            as_written.setdefault(f"##:{index}", line.strip())
            continue
        if section is not None and _H3_RE.match(line):
            if section not in seen:
                order.append(section)
            key = _heading_text(line)
            seen[section].append(key)
            as_written.setdefault(f"###:{section[0]}:{key}", line.strip())
    if in_fence:
        raise UnterminatedFence(
            "unterminated code fence -- everything after it went unscanned, so a duplicate could hide there"
        )

    dupes: list[tuple[str, str]] = []
    for section_key in order:
        index = section_key[0]
        title = as_written[f"##:{index}"]
        counts = collections.Counter(seen[section_key])
        for head in sorted(h for h, n in counts.items() if n > 1):
            dupes.append((title, as_written[f"###:{index}:{head}"]))
    return dupes


def test_no_release_section_repeats_a_subheading():
    """The real file, not a fixture.

    A fixture-only version of this would have passed against the very file that
    prompted it -- see the lesson about running a tool against the real repo.
    """
    dupes = find_duplicate_subheadings(CHANGELOG.read_text(encoding="utf-8"))
    assert dupes == [], f"duplicate ### headings within one ## section (litclock-dev#697): {dupes}"


def test_detector_catches_a_duplicate_it_is_shown():
    """Mutate the input and watch the guard go red, or it is not a guard."""
    text = "# Changelog\n\n## [Unreleased]\n\n### Changed\n- one\n\n### Fixed\n- two\n\n### Changed\n- three\n"
    assert find_duplicate_subheadings(text) == [("## [Unreleased]", "### Changed")]


def test_detector_does_not_leak_across_release_sections():
    """Two releases may each have a `### Fixed`; that is the normal shape."""
    text = "## [Unreleased]\n\n### Fixed\n- one\n\n## [v0.1.0] - 2026-01-01\n\n### Fixed\n- two\n"
    assert find_duplicate_subheadings(text) == []


def test_two_sections_sharing_a_title_do_not_cross_contaminate():
    """Keyed by position, not by title.

    Two `## [Unreleased]` headings is a real state (a bad merge), and keying on
    the title would report a phantom `### Fixed` duplicate that exists in
    neither section while the actual defect goes unmentioned.
    """
    text = "## [Unreleased]\n\n### Fixed\n- one\n\n## [Unreleased]\n\n### Fixed\n- two\n"
    assert find_duplicate_subheadings(text) == []


def test_detector_ignores_h4_subheadings():
    """h4s are outside this rule; only `###` headings are compared.

    v0.211.0 nests `#### Auto-update -- Added` / `-- Changed` under an `###`
    parent, so a matcher that treated `####` as `###` would put an unrelated
    layout under a rule that has nothing to say about it. This FIXTURE is the
    only thing holding that line: the file's two h4s are distinct strings, so
    deleting the delimiter from `_H3_RE` leaves the real-file assertion green
    and reddens only this test. Named because a reader could otherwise assume
    the real file is exerting pressure it is not.
    """
    text = "## [v0.1.0] - 2026-01-01\n\n### Changed\n#### Auto-update\n- one\n#### Auto-update\n- two\n"
    assert find_duplicate_subheadings(text) == []


def test_detector_ignores_headings_before_any_release_section():
    """A `###` above the first `##` belongs to no release, so it is not compared."""
    assert find_duplicate_subheadings("### Fixed\n- a\n\n### Fixed\n- b\n") == []


def test_detector_handles_empty_and_headingless_input():
    assert find_duplicate_subheadings("") == []
    assert find_duplicate_subheadings("just prose\n") == []


def test_detector_reports_every_repeated_heading_in_sorted_order():
    """Two distinct repeats in one section -- pins the sort.

    Without a fixture like this the ordering is dict-insertion-dependent and
    the failure message drifts between runs of a Counter.
    """
    text = "## [Unreleased]\n\n### Fixed\n- a\n\n### Changed\n- b\n\n### Fixed\n- c\n\n### Changed\n- d\n"
    assert find_duplicate_subheadings(text) == [
        ("## [Unreleased]", "### Changed"),
        ("## [Unreleased]", "### Fixed"),
    ]


def test_detector_ignores_headings_inside_a_code_fence():
    """A documented example of the changelog format is not a duplicate."""
    text = "## [Unreleased]\n\n### Changed\n- one\n\n```md\n### Changed\n### Changed\n```\n"
    assert find_duplicate_subheadings(text) == []


def test_a_fenced_h2_does_not_end_the_section():
    """The false NEGATIVE, and the more dangerous direction.

    A `## ` inside a fence used to reset the section, so every heading after it
    was attributed to a section that does not exist and the real duplicate went
    unreported -- the guard disarmed by one entry quoting a changelog snippet.
    """
    text = "## [v0.1.0] - 2026-01-01\n\n### Fixed\n- one\n\n```\n## [v9.9.9]\n```\n\n### Fixed\n- two\n"
    assert find_duplicate_subheadings(text) == [("## [v0.1.0] - 2026-01-01", "### Fixed")]


def test_tilde_fences_count_too():
    text = "## [Unreleased]\n\n### Fixed\n- one\n\n~~~\n### Fixed\n### Fixed\n~~~\n"
    assert find_duplicate_subheadings(text) == []


def test_indented_headings_are_still_headings():
    """Up to three spaces is a heading; the fourth makes it a code block."""
    indented = "## [Unreleased]\n\n### Fixed\n- a\n\n   ### Fixed\n- b\n"
    assert find_duplicate_subheadings(indented) == [("## [Unreleased]", "### Fixed")]
    code_block = "## [Unreleased]\n\n### Fixed\n- a\n\n    ### Fixed\n- b\n"
    assert find_duplicate_subheadings(code_block) == []


def test_tab_delimited_headings_are_headings():
    """CommonMark allows a tab after the `#`s, and GitHub renders it.

    A duplicate could otherwise hide behind one tab, exactly as it could behind
    one leading space.
    """
    text = "## [Unreleased]\n\n### Fixed\n- a\n\n###\tFixed\n- b\n"
    assert find_duplicate_subheadings(text) == [("## [Unreleased]", "### Fixed")]


def test_a_closing_hash_sequence_is_the_same_heading():
    """`### Fixed ###` renders as "Fixed"; comparing raw lines would miss it."""
    text = "## [Unreleased]\n\n### Fixed\n- a\n\n### Fixed ###\n- b\n"
    assert find_duplicate_subheadings(text) == [("## [Unreleased]", "### Fixed")]


def test_a_hash_not_preceded_by_space_is_part_of_the_heading():
    """The closing sequence must be preceded by whitespace to count."""
    text = "## [Unreleased]\n\n### Fixed\n- a\n\n### Fixed#\n- b\n"
    assert find_duplicate_subheadings(text) == []


def test_an_unterminated_fence_raises_instead_of_reporting_clean():
    """The one failure direction that must never be quiet.

    Everything after the last unmatched fence marker is unscanned. Returning
    `[]` there is the guard reporting success over a region it never read --
    verified: a real duplicate placed after an unclosed ```bash used to come
    back as no findings.
    """
    text = "## [Unreleased]\n\n```bash\necho hi\n\n### Fixed\n- a\n\n### Fixed\n- b\n"
    with pytest.raises(UnterminatedFence):
        find_duplicate_subheadings(text)


def test_the_real_file_has_balanced_fences():
    """The guard above is only reachable if this ever stops holding."""
    find_duplicate_subheadings(CHANGELOG.read_text(encoding="utf-8"))


def test_a_tab_delimited_section_heading_still_opens_a_section():
    """`_H2_RE` needs the same tab tolerance as `_H3_RE`.

    Without it a tab-delimited `##` is invisible, `section` keeps pointing at
    whatever came before, and the `###`s underneath are attributed to the wrong
    release -- fail-open in one direction and false-alarm in the other. A
    mutation probe caught this: dropping the tab from `_H2_RE` alone left every
    other test green.
    """
    text = "## [v0.1.0] - 2026-01-01\n\n### Fixed\n- a\n\n##\t[Unreleased]\n\n### Fixed\n- b\n\n### Fixed\n- c\n"
    assert find_duplicate_subheadings(text) == [("##\t[Unreleased]", "### Fixed")]
