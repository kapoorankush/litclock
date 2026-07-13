#!/usr/bin/env python3
"""
Merge reviewed quotes from a reviewed-quotes CSV into litclock_annotated.csv
Only adds non-duplicate quotes.
"""

import csv
from pathlib import Path

# placeholder — set to your source's file name before running
REVIEW_FILE = Path(__file__).parent / "quotes_for_review.csv"
MAIN_CSV = Path(__file__).parent / "litclock_annotated.csv"
BACKUP_FILE = Path(__file__).parent / "litclock_annotated.csv.pre_merge_backup"


def main():
    print(f"Reading review file: {REVIEW_FILE}")

    # Read review file
    new_quotes = []
    with open(REVIEW_FILE, encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="|")
        for row in reader:
            # Skip duplicates
            if row.get("IS_DUPLICATE", "NO") == "YES":
                continue

            new_quotes.append(
                {
                    "TIME": row["TIME"],
                    "TIME_PHRASE": row["TIME_PHRASE"],
                    "QUOTE": row["QUOTE"],
                    "TITLE": row["TITLE"],
                    "AUTHOR": row["AUTHOR"],
                }
            )

    print(f"Found {len(new_quotes)} new quotes to add")

    if not new_quotes:
        print("No new quotes to merge.")
        return

    # Backup existing file
    print(f"Creating backup: {BACKUP_FILE}")
    with open(MAIN_CSV, encoding="utf-8") as f:
        backup_content = f.read()
    with open(BACKUP_FILE, "w", encoding="utf-8") as f:
        f.write(backup_content)

    # Read existing quotes
    existing_quotes = []
    with open(MAIN_CSV, encoding="utf-8") as f:
        reader = csv.reader(f, delimiter="|")
        for row in reader:
            if len(row) >= 5:
                existing_quotes.append(row)

    print(f"Existing quotes: {len(existing_quotes)}")

    # Add new quotes (with IS_NSFW defaulting to NO)
    for q in new_quotes:
        existing_quotes.append(
            [
                q["TIME"],
                q["TIME_PHRASE"],
                q["QUOTE"],
                q["TITLE"],
                q["AUTHOR"],
                "NO",  # IS_NSFW - will be reviewed by detect_nsfw.py
            ]
        )

    # Sort by time, then by quote
    existing_quotes.sort(key=lambda x: (x[0], x[2][:50] if len(x) > 2 else ""))

    # Write merged file
    print(f"Writing merged file with {len(existing_quotes)} total quotes")
    with open(MAIN_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter="|")
        for row in existing_quotes:
            writer.writerow(row)

    print("\nMerge complete!")
    print(f"  Added: {len(new_quotes)} new quotes")
    print(f"  Total: {len(existing_quotes)} quotes")
    print(f"  Backup: {BACKUP_FILE}")
    print()
    print("IMPORTANT: Run NSFW detection on the new quotes:")
    print("  python detect_nsfw.py --keywords-only")
    print("  python review_nsfw.py interactive")
    print("  python review_nsfw.py merge")


if __name__ == "__main__":
    main()
