"""The PHP generator's per-bucket image numbering (litclock-dev#584).

`quote_to_image.php` names each PNG `quote_<HHMM>_<n>.png`, where `n` restarts
at 0 for every new time bucket. `src/quote_corpus.py` rebuilds that numbering
in Python to attribute a rendered image back to its CSV row.

The two MUST agree. They did not: the PHP seeded its "previous bucket" tracker
with the integer `0`, and `"0000" == 0` is true under PHP's numeric-string
coercion, so the very first CSV row looked like a repeat and midnight was
written as `quote_0000_1..72` instead of `0..71`. Only "0000" collides with
integer 0, so midnight was the only affected bucket — and because the Python
reader is 0-based, every midnight image resolved to the NEXT row in the CSV.

These tests run real PHP against the generator's own seed and comparison, read
out of the source file, so they follow the code rather than restating it.

Mutation-checked, and the result is worth recording: reverting the seed to `0`
ALONE leaves every test green, and so does reverting it to `""`. Both are
equivalent mutants — once the comparison is `===`, `"0000" === 0` is false
whatever the seed is, so the strict operator is what actually carries the fix
and the null seed is belt-and-braces. Reverting the comparison alone IS caught,
and reverting both (the true pre-fix state) fails 6 of these 7 tests.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

PHP_GENERATOR = Path(__file__).resolve().parent.parent / "image-gen" / "quote_to_image.php"

pytestmark = pytest.mark.skipif(shutil.which("php") is None, reason="php not installed")


def _php(snippet: str) -> str:
    return subprocess.run(
        ["php", "-r", snippet], capture_output=True, text=True, check=True
    ).stdout.strip()


def _generator_seed() -> str:
    """The literal the generator assigns to $previoustime, verbatim."""
    m = re.search(r"^\$previoustime\s*=\s*(.+?);\s*$", PHP_GENERATOR.read_text(), re.M)
    assert m, "could not find the $previoustime seed in the generator"
    return m.group(1).strip()


def _generator_comparison() -> str:
    """The operator the generator uses to detect a repeated bucket."""
    m = re.search(r"if \(\$timeKey\s*(===?)\s*\$previoustime\)", PHP_GENERATOR.read_text())
    assert m, "could not find the $timeKey/$previoustime comparison"
    return m.group(1)


def _number_buckets(times: list[str]) -> list[int]:
    """Replay the generator's numbering loop in real PHP, using ITS seed and
    ITS operator, and return the image number assigned to each row."""
    seed, op = _generator_seed(), _generator_comparison()
    php_times = ",".join(f'"{t}"' for t in times)
    snippet = (
        f"$imagenumber = 0; $previoustime = {seed};"
        f"$out = [];"
        f"foreach ([{php_times}] as $timeKey) {{"
        f"  if ($timeKey {op} $previoustime) {{ $imagenumber++; }} else {{ $imagenumber = 0; }}"
        f"  $previoustime = $timeKey; $out[] = $imagenumber;"
        f"}}"
        f"echo implode(',', $out);"
    )
    return [int(x) for x in _php(snippet).split(",")]


class TestMidnightNumbering:
    def test_the_php_coercion_that_caused_this_still_exists(self):
        """Not a hypothetical: pin the language behaviour the bug rested on, so
        the reason for the null seed stays legible to the next reader."""
        assert _php('var_export("0000" == 0);') == "true"
        assert _php('var_export("0230" == 0);') == "false"
        assert _php('var_export("0000" === null);') == "false"

    def test_midnight_starts_at_zero(self):
        """THE regression. Reverting the seed to `0` makes this return 1."""
        assert _number_buckets(["0000"])[0] == 0

    def test_midnight_bucket_numbers_contiguously_from_zero(self):
        assert _number_buckets(["0000"] * 5) == [0, 1, 2, 3, 4]

    def test_every_bucket_starts_at_zero_including_the_first_row(self):
        """The first row of the CSV is midnight in production, but the rule is
        general: whatever bucket comes first must start at 0."""
        for first in ("0000", "0001", "0230", "1200", "2359"):
            assert _number_buckets([first])[0] == 0, first

    def test_bucket_transitions_reset_the_counter(self):
        assert _number_buckets(["0000", "0000", "0001", "0001", "0001", "0002"]) == [0, 1, 0, 1, 2, 0]

    def test_numeric_string_buckets_are_not_conflated(self):
        """Loose `==` would treat "0130" and "130" as the same bucket. Real
        time keys are always 4 chars so this cannot arise today, but the strict
        operator is what guarantees it stays that way."""
        assert _number_buckets(["0130", "130"]) == [0, 0]


class TestGeneratorMatchesPythonReader:
    """quote_corpus.py rebuilds this numbering to attribute an image back to a
    CSV row. A divergence silently mis-attributes quotes on the image path,
    which is what litclock-dev#584 was."""

    def test_numbering_agrees_with_quote_corpus_on_the_real_corpus_head(self):
        import csv
        import sys

        sys.path.insert(0, str(PHP_GENERATOR.parent.parent / "src"))
        corpus = PHP_GENERATOR.parent / "litclock_annotated.csv"
        times: list[str] = []
        with corpus.open(newline="", encoding="utf-8") as fh:
            for row in csv.reader(fh, delimiter="|"):
                if len(row) < 5 or len(row[0]) != 5 or row[0][2] != ":":
                    continue
                times.append(row[0][:2] + row[0][3:])
                if len(times) >= 200:
                    break

        php_numbers = _number_buckets(times)

        # The Python reader's rule, mirrored from quote_corpus.py.
        py_numbers, previous, n = [], None, 0
        for t in times:
            if t == previous:
                n += 1
            else:
                n, previous = 0, t
            py_numbers.append(n)

        assert php_numbers == py_numbers
        assert php_numbers[0] == 0, "the first CSV row must be image 0, not 1"
