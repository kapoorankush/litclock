#!/usr/bin/env python3
"""
Process the partially reviewed CSV file:
1. Apply improved time matching to pending items
2. Merge approved items into the annotated list
3. Generate reduced review file for remaining uncertain quotes
"""

import csv
import re
from pathlib import Path

# Import matching functions from prepare_review_csv
from prepare_review_csv import (
    SUSPICIOUS_WORDS,
    find_best_time_match,
    has_time_context,
)

# placeholder — set to your source's file names before running
INPUT_FILE = Path(__file__).parent / "quotes_for_review_partly_reviewed.csv"
ANNOTATED_FILE = Path(__file__).parent / "litclock_annotated.csv"
OUTPUT_REVIEW_FILE = Path(__file__).parent / "quotes_needs_review.csv"
OUTPUT_MERGED_FILE = Path(__file__).parent / "litclock_annotated_merged.csv"


def load_existing_quotes(filepath: Path) -> set[tuple[str, str]]:
    """Load existing quotes as (time, quote_start) tuples to detect duplicates."""
    existing = set()
    if not filepath.exists():
        return existing

    with open(filepath, encoding="utf-8") as f:
        content = f.read().replace("\r\n", "\n").replace("\r", "\n")
        reader = csv.reader(content.splitlines(), delimiter="|")

        for row in reader:
            if len(row) >= 3:
                time_str = row[0]
                quote_start = row[2][:50].lower()  # First 50 chars for matching
                existing.add((time_str, quote_start))

    return existing


def is_confident_match(time_str: str, time_phrase: str, quote: str) -> tuple[bool, str]:
    """
    Determine if we're confident about this time phrase match.
    Returns (is_confident, reason).
    """
    # Check if phrase is in quote
    if time_phrase.lower() not in quote.lower():
        return False, "Phrase not in quote"

    # Check for suspicious single words without context
    if time_phrase.lower().strip() in SUSPICIOUS_WORDS:
        if not has_time_context(time_phrase, quote):
            return False, f"Single word '{time_phrase}' lacks time context"

    # Check for fallback digital time (HH:MM format matching the target)
    if re.match(r"^\d{2}:\d{2}$", time_phrase):
        if time_phrase == time_str:
            return False, "Only matched the target time itself"

    return True, "OK"


def main():
    print(f"Reading partially reviewed file: {INPUT_FILE}")

    # Load existing quotes to avoid re-adding duplicates
    existing_quotes = load_existing_quotes(ANNOTATED_FILE)
    print(f"Loaded {len(existing_quotes)} existing quotes from annotated file")

    # Read the partially reviewed file
    rows = []
    with open(INPUT_FILE, encoding="utf-8") as f:
        content = f.read().replace("\r\n", "\n").replace("\r", "\n")
        reader = csv.reader(content.splitlines(), delimiter="|")
        next(reader)

        for row in reader:
            if len(row) >= 9:
                rows.append(
                    {
                        "time": row[0],
                        "time_phrase": row[1],
                        "quote": row[2],
                        "title": row[3],
                        "author": row[4],
                        "asin": row[5],
                        "needs_review": row[6],
                        "review_reason": row[7],
                        "is_duplicate": row[8],
                    }
                )

    print(f"Loaded {len(rows)} rows from reviewed file")

    # Categorize rows
    stats = {
        "already_approved_no": 0,
        "already_approved_user": 0,
        "duplicates_skipped": 0,
        "auto_approved_by_matching": 0,
        "still_needs_review": 0,
        "already_in_database": 0,
    }

    to_merge = []
    to_review = []

    for row in rows:
        time_str = row["time"]
        quote = row["quote"]
        needs_review = row["needs_review"]
        is_duplicate = row["is_duplicate"]

        # Skip duplicates (already in our database)
        if is_duplicate == "YES":
            stats["duplicates_skipped"] += 1
            continue

        # Check if already in database (by time + quote start)
        quote_start = quote[:50].lower()
        if (time_str, quote_start) in existing_quotes:
            stats["already_in_database"] += 1
            continue

        # Already approved (NO = auto-approved, Approved = user approved)
        if needs_review == "NO":
            stats["already_approved_no"] += 1
            to_merge.append(row)
            continue

        if needs_review == "Approved":
            stats["already_approved_user"] += 1
            to_merge.append(row)
            continue

        # Needs review - try improved matching
        found_time, match_info = find_best_time_match(time_str, quote)

        if found_time:
            # Update time phrase with improved match
            row["time_phrase"] = found_time

            # Check if we're confident about this match
            is_confident, reason = is_confident_match(time_str, found_time, quote)

            if is_confident:
                stats["auto_approved_by_matching"] += 1
                to_merge.append(row)
                continue

        # Still needs review
        stats["still_needs_review"] += 1
        to_review.append(row)

    # Print stats
    print("\n" + "=" * 60)
    print("PROCESSING SUMMARY")
    print("=" * 60)
    print(f"Duplicates skipped:              {stats['duplicates_skipped']}")
    print(f"Already in database:             {stats['already_in_database']}")
    print(f"Auto-approved (NO):              {stats['already_approved_no']}")
    print(f"User approved (Approved):        {stats['already_approved_user']}")
    print(f"Auto-approved by matching:       {stats['auto_approved_by_matching']}")
    print(f"Still needs review:              {stats['still_needs_review']}")
    print("-" * 60)
    print(f"Total to merge:                  {len(to_merge)}")
    print(f"Total still needing review:      {len(to_review)}")
    print("=" * 60)

    # Load existing annotated file
    existing_rows = []
    if ANNOTATED_FILE.exists():
        with open(ANNOTATED_FILE, encoding="utf-8") as f:
            content = f.read().replace("\r\n", "\n").replace("\r", "\n")
            reader = csv.reader(content.splitlines(), delimiter="|")
            for row in reader:
                if len(row) >= 5:
                    existing_rows.append(row)

    print(f"\nExisting annotated file has {len(existing_rows)} rows")

    # Prepare new rows to add (with IS_NSFW defaulting to NO)
    new_rows = []
    for row in to_merge:
        new_row = [
            row["time"],
            row["time_phrase"],
            row["quote"],
            row["title"],
            row["author"],
            "NO",  # IS_NSFW - will be reviewed by detect_nsfw.py
        ]
        new_rows.append(new_row)

    print(f"Adding {len(new_rows)} new rows")

    # Combine and sort
    all_rows = existing_rows + new_rows
    all_rows.sort(key=lambda x: x[0])  # Sort by time

    # Write merged file
    print(f"\nWriting merged file: {OUTPUT_MERGED_FILE}")
    with open(OUTPUT_MERGED_FILE, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter="|")
        for row in all_rows:
            writer.writerow(row)

    print(f"Merged file has {len(all_rows)} total rows")

    # Write reduced review file
    if to_review:
        print(f"\nWriting review file: {OUTPUT_REVIEW_FILE}")
        with open(OUTPUT_REVIEW_FILE, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["TIME", "TIME_PHRASE", "QUOTE", "TITLE", "AUTHOR", "ASIN", "NEEDS_REVIEW", "REVIEW_REASON"],
                delimiter="|",
            )
            writer.writeheader()

            for row in to_review:
                writer.writerow(
                    {
                        "TIME": row["time"],
                        "TIME_PHRASE": row["time_phrase"],
                        "QUOTE": row["quote"],
                        "TITLE": row["title"],
                        "AUTHOR": row["author"],
                        "ASIN": row["asin"],
                        "NEEDS_REVIEW": "YES",
                        "REVIEW_REASON": row["review_reason"],
                    }
                )

        print(f"Review file has {len(to_review)} rows needing attention")
    else:
        print("\nNo items need review - all quotes processed!")

    print("\n" + "=" * 60)
    print("DONE")
    print("=" * 60)
    print("\nNext steps:")
    print(f"1. Review the merged file: {OUTPUT_MERGED_FILE}")
    print(f"2. If OK, replace {ANNOTATED_FILE} with the merged file")
    if to_review:
        print(f"3. Review remaining items in: {OUTPUT_REVIEW_FILE}")
    print()
    print("IMPORTANT: After merging, run NSFW detection on new quotes:")
    print("  python detect_nsfw.py --keywords-only")
    print("  python review_nsfw.py interactive")
    print("  python review_nsfw.py merge")


if __name__ == "__main__":
    main()
