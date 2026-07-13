#!/usr/bin/env python3
"""
Validate the time parser against all quotes in litclock_annotated.csv.

This script tests that the parser can correctly parse every TIME_PHRASE
and match it to its expected TIME.
"""

import csv
from collections import defaultdict
from pathlib import Path

from time_parser import parse_time_phrase, time_to_str, validate_time_phrase

ANNOTATED_CSV = Path(__file__).parent / "litclock_annotated.csv"


def main():
    print(f"Loading quotes from {ANNOTATED_CSV}...")

    quotes = []
    with open(ANNOTATED_CSV, encoding="utf-8") as f:
        reader = csv.reader(f, delimiter="|")
        for row in reader:
            if len(row) >= 5:
                quotes.append(
                    {
                        "time": row[0],
                        "phrase": row[1],
                        "quote": row[2],
                        "title": row[3],
                        "author": row[4],
                    }
                )

    print(f"Loaded {len(quotes)} quotes\n")

    # Test parsing
    passed = 0
    failed = 0
    ambiguous = 0  # Parsed but could be AM or PM
    failures = []
    failure_patterns = defaultdict(list)

    for q in quotes:
        expected_time = q["time"]
        phrase = q["phrase"]

        is_valid, parsed_time = validate_time_phrase(phrase, expected_time)

        if is_valid:
            passed += 1
        else:
            result = parse_time_phrase(phrase)
            if result:
                parsed_hour, parsed_min = result
                expected_hour = int(expected_time[:2])
                expected_min = int(expected_time[3:])

                # Check if it's a 12-hour ambiguity
                if parsed_min == expected_min and abs(parsed_hour - expected_hour) == 12:
                    ambiguous += 1
                    passed += 1  # Count as passed since it's an AM/PM ambiguity
                else:
                    failed += 1
                    failures.append(
                        {
                            "time": expected_time,
                            "phrase": phrase,
                            "parsed": time_to_str(parsed_hour, parsed_min),
                            "quote_snippet": q["quote"][:60] + "..." if len(q["quote"]) > 60 else q["quote"],
                        }
                    )
                    # Categorize failure pattern
                    pattern = categorize_pattern(phrase)
                    failure_patterns[pattern].append(phrase)
            else:
                failed += 1
                failures.append(
                    {
                        "time": expected_time,
                        "phrase": phrase,
                        "parsed": None,
                        "quote_snippet": q["quote"][:60] + "..." if len(q["quote"]) > 60 else q["quote"],
                    }
                )
                pattern = categorize_pattern(phrase)
                failure_patterns[pattern].append(phrase)

    # Print results
    print("=" * 70)
    print("VALIDATION RESULTS")
    print("=" * 70)
    print(f"Total quotes:     {len(quotes)}")
    print(f"Passed:           {passed} ({100 * passed / len(quotes):.1f}%)")
    print(f"  (12hr ambiguous: {ambiguous})")
    print(f"Failed:           {failed} ({100 * failed / len(quotes):.1f}%)")
    print("=" * 70)

    if failure_patterns:
        print("\nFAILURE PATTERNS (by category):")
        print("-" * 70)
        for pattern, phrases in sorted(failure_patterns.items(), key=lambda x: -len(x[1])):
            print(f"\n{pattern}: {len(phrases)} failures")
            # Show unique examples
            unique_phrases = list(set(phrases))[:5]
            for p in unique_phrases:
                print(f"  - '{p}'")

    if failures and len(failures) <= 50:
        print("\n\nALL FAILURES:")
        print("-" * 70)
        for f in failures:
            parsed_str = f["parsed"] if f["parsed"] else "UNPARSED"
            print(f"[{f['time']}] '{f['phrase']}' -> {parsed_str}")

    # Save detailed failures to file
    if failures:
        failure_file = Path(__file__).parent / "parser_failures.txt"
        with open(failure_file, "w", encoding="utf-8") as f:
            f.write("Time Parser Validation Failures\n")
            f.write(f"{'=' * 70}\n")
            f.write(f"Total: {len(failures)} failures out of {len(quotes)} quotes\n\n")

            for pattern, phrases in sorted(failure_patterns.items(), key=lambda x: -len(x[1])):
                f.write(f"\n{pattern}: {len(phrases)} failures\n")
                for p in set(phrases):
                    f.write(f"  - '{p}'\n")

            f.write(f"\n\n{'=' * 70}\n")
            f.write("DETAILED FAILURES:\n")
            f.write(f"{'=' * 70}\n\n")

            for failure in failures:
                f.write(f"Expected: {failure['time']}\n")
                f.write(f"Phrase:   '{failure['phrase']}'\n")
                f.write(f"Parsed:   {failure['parsed'] if failure['parsed'] else 'UNPARSED'}\n")
                f.write(f"Quote:    {failure['quote_snippet']}\n")
                f.write("-" * 40 + "\n")

        print(f"\nDetailed failures written to: {failure_file}")


def categorize_pattern(phrase: str) -> str:
    """Categorize a phrase pattern for grouping failures."""
    p = phrase.lower()

    # Digital patterns
    if any(c.isdigit() for c in p):
        if ":" in p or "." in p:
            if "am" in p or "pm" in p or "a.m" in p or "p.m" in p:
                return "Digital with AM/PM"
            return "Digital (colon/period)"
        if "h" in p:
            return "Military time with h"
        return "Digital (other)"

    # Text patterns
    if "o'clock" in p or "oclock" in p:
        return "O'clock"
    if "half" in p:
        return "Half past"
    if "quarter" in p:
        return "Quarter"
    if "minutes" in p or "minute" in p:
        return "Minutes to/past"
    if " past " in p:
        return "Past (without 'minutes')"
    if " to " in p:
        return "To (without 'minutes')"
    if "morning" in p or "afternoon" in p or "evening" in p or "night" in p:
        return "Period of day"
    if "struck" in p:
        return "Clock struck"
    if "about" in p or "around" in p or "almost" in p or "nearly" in p:
        return "Approximate time"
    if "after" in p or "before" in p:
        return "After/before"

    # Check if it's just a number word
    number_words = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve"]
    if any(w in p for w in number_words):
        return "Number word (other)"

    return "Unknown pattern"


if __name__ == "__main__":
    main()
