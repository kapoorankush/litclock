#!/usr/bin/env python3
"""
NSFW Review Helper Script for Literary Clock Quotes

This script helps with the human review workflow:
1. Shows flagged quotes for review
2. Merges reviewed decisions back into the main CSV

Usage:
  # Merge reviewed decisions into main CSV
  python review_nsfw.py merge --reviewed nsfw_flagged_for_review.csv

  # Show statistics about the review file
  python review_nsfw.py stats --reviewed nsfw_flagged_for_review.csv

  # Interactive review mode (for terminal-based review)
  python review_nsfw.py interactive --reviewed nsfw_flagged_for_review.csv
"""

import argparse
import csv
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
CSV_FILE = SCRIPT_DIR / "litclock_annotated.csv"
DEFAULT_REVIEW_FILE = SCRIPT_DIR / "nsfw_flagged_for_review.csv"


def load_review_file(review_file: Path) -> list[dict]:
    """Load the review file with flagged quotes."""
    reviews = []

    with open(review_file, encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f, delimiter="|")
        for row in reader:
            reviews.append(row)

    return reviews


def show_stats(review_file: Path):
    """Show statistics about the review file."""
    reviews = load_review_file(review_file)

    total = len(reviews)
    reviewed = sum(1 for r in reviews if r.get("FINAL_DECISION", "").strip())
    pending = total - reviewed

    yes_count = sum(1 for r in reviews if r.get("FINAL_DECISION", "").strip().upper() == "YES")
    no_count = sum(1 for r in reviews if r.get("FINAL_DECISION", "").strip().upper() == "NO")

    # Count by flag source
    keyword_flagged = sum(1 for r in reviews if r.get("FLAG_SOURCE") == "KEYWORD")
    llm_flagged = sum(1 for r in reviews if r.get("FLAG_SOURCE") == "LLM")

    # Count by category
    categories = {}
    for r in reviews:
        for cat in r.get("FLAG_CATEGORIES", "").split(", "):
            cat = cat.strip()
            if cat:
                categories[cat] = categories.get(cat, 0) + 1

    print("=" * 60)
    print("Review File Statistics")
    print("=" * 60)
    print(f"\nFile: {review_file}")
    print(f"\nTotal flagged: {total}")
    print(f"  - By keywords: {keyword_flagged}")
    print(f"  - By LLM: {llm_flagged}")
    print("\nReview progress:")
    print(f"  - Reviewed: {reviewed}")
    print(f"  - Pending: {pending}")
    print("\nDecisions made:")
    print(f"  - Marked NSFW (YES): {yes_count}")
    print(f"  - Marked SFW (NO): {no_count}")

    if categories:
        print("\nFlag categories:")
        for cat, count in sorted(categories.items(), key=lambda x: -x[1]):
            print(f"  - {cat}: {count}")


def merge_decisions(review_file: Path, csv_file: Path, output_file: Path = None):
    """Merge reviewed decisions back into the main CSV."""
    reviews = load_review_file(review_file)

    # Build lookup of decisions by row number
    decisions = {}
    for r in reviews:
        row_num = int(r["ROW_NUM"])
        final_decision = r.get("FINAL_DECISION", "").strip().upper()
        if final_decision in ("YES", "NO"):
            decisions[row_num] = final_decision

    if not decisions:
        print("Error: No reviewed decisions found (FINAL_DECISION column is empty)")
        print("Please review the flagged quotes and fill in YES or NO for each.")
        sys.exit(1)

    print(f"Found {len(decisions)} reviewed decisions to merge")

    # Read and update the main CSV
    output_file = output_file or csv_file
    rows = []
    updated = 0

    with open(csv_file, encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f, delimiter="|")
        for row_num, row in enumerate(reader):
            if len(row) < 5:
                rows.append(row)
                continue

            # Ensure IS_NSFW column exists
            while len(row) < 6:
                row.append("NO")

            # Update if we have a decision for this row
            if row_num in decisions:
                old_value = row[5]
                row[5] = decisions[row_num]
                if old_value != row[5]:
                    updated += 1

            rows.append(row)

    # Write updated CSV
    with open(output_file, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter="|")
        for row in rows:
            writer.writerow(row)

    print(f"Updated {updated} quotes in {output_file.name}")
    print(f"  - Marked as NSFW: {sum(1 for d in decisions.values() if d == 'YES')}")
    print(f"  - Marked as SFW: {sum(1 for d in decisions.values() if d == 'NO')}")


# ANSI color codes
RED = "\033[91m"
BOLD = "\033[1m"
RESET = "\033[0m"


def load_full_quotes(csv_file: Path) -> dict[int, str]:
    """Load full quotes from main CSV, indexed by row number."""
    quotes = {}
    with open(csv_file, encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f, delimiter="|")
        for row_num, row in enumerate(reader):
            if len(row) >= 3:
                quotes[row_num] = row[2]
    return quotes


def highlight_words(text: str, words: str) -> str:
    """Highlight flagged words in text with red color."""
    # Split the comma-separated words
    word_list = [w.strip() for w in words.split(",")]

    result = text
    for word in word_list:
        if word:
            # Case-insensitive replacement with color highlighting
            pattern = re.compile(re.escape(word), re.IGNORECASE)
            result = pattern.sub(f"{RED}{BOLD}\\g<0>{RESET}", result)

    return result


def interactive_review(review_file: Path):
    """Interactive terminal-based review mode."""
    reviews = load_review_file(review_file)

    # Load full quotes from main CSV
    full_quotes = load_full_quotes(CSV_FILE)

    # Find unreviewed items
    pending = [(i, r) for i, r in enumerate(reviews) if not r.get("FINAL_DECISION", "").strip()]

    if not pending:
        print("All quotes have been reviewed!")
        return

    print(f"\n{len(pending)} quotes pending review. Press Enter to start, Ctrl+C to quit.\n")
    input()

    try:
        for idx, (review_idx, review) in enumerate(pending):
            print("\n" + "=" * 60)
            print(f"Quote {idx + 1}/{len(pending)} (Row {review['ROW_NUM']})")
            print("=" * 60)
            print(f"\nTime: {review['TIME']}")
            print(f"Title: {review['TITLE']}")
            print(f"Author: {review['AUTHOR']}")

            # Show full quote with flagged words highlighted
            row_num = int(review["ROW_NUM"])
            full_quote = full_quotes.get(row_num, review["QUOTE_PREVIEW"])
            flagged_words = review["FLAG_DETAILS"]
            highlighted_quote = highlight_words(full_quote, flagged_words)
            print(f'\nFull quote:\n  "{highlighted_quote}"')

            print(f"\nFlagged for: {RED}{BOLD}{flagged_words}{RESET}")

            while True:
                decision = input("\nIs this NSFW? [Y/n/s(kip)/q(uit)] (Enter=Yes): ").strip().lower()
                if decision in ("", "y", "yes"):
                    reviews[review_idx]["FINAL_DECISION"] = "YES"
                    print("  -> Marked as NSFW")
                    break
                elif decision in ("n", "no"):
                    reviews[review_idx]["FINAL_DECISION"] = "NO"
                    print("  -> Marked as SFW")
                    break
                elif decision in ("s", "skip"):
                    print("  -> Skipped")
                    break
                elif decision in ("q", "quit"):
                    raise KeyboardInterrupt
                else:
                    print("  Please enter y, n, s, or q")

    except KeyboardInterrupt:
        print("\n\nReview interrupted.")

    # Save progress
    print(f"\nSaving progress to {review_file.name}...")

    with open(review_file, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=reviews[0].keys(), delimiter="|")
        writer.writeheader()
        writer.writerows(reviews)

    # Show updated stats
    reviewed = sum(1 for r in reviews if r.get("FINAL_DECISION", "").strip())
    print(f"Progress: {reviewed}/{len(reviews)} reviewed")


def main():
    parser = argparse.ArgumentParser(description="NSFW Review Helper for Literary Clock Quotes")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Stats command
    stats_parser = subparsers.add_parser("stats", help="Show review file statistics")
    stats_parser.add_argument(
        "--reviewed", type=Path, default=DEFAULT_REVIEW_FILE, help=f"Review file (default: {DEFAULT_REVIEW_FILE.name})"
    )

    # Merge command
    merge_parser = subparsers.add_parser("merge", help="Merge decisions into main CSV")
    merge_parser.add_argument(
        "--reviewed", type=Path, default=DEFAULT_REVIEW_FILE, help=f"Review file (default: {DEFAULT_REVIEW_FILE.name})"
    )
    merge_parser.add_argument("--csv", type=Path, default=CSV_FILE, help=f"Main CSV file (default: {CSV_FILE.name})")
    merge_parser.add_argument("--output", type=Path, help="Output file (default: overwrite input CSV)")

    # Interactive command
    interactive_parser = subparsers.add_parser("interactive", help="Interactive review mode")
    interactive_parser.add_argument(
        "--reviewed", type=Path, default=DEFAULT_REVIEW_FILE, help=f"Review file (default: {DEFAULT_REVIEW_FILE.name})"
    )

    args = parser.parse_args()

    if args.command == "stats":
        show_stats(args.reviewed)
    elif args.command == "merge":
        merge_decisions(args.reviewed, args.csv, args.output)
    elif args.command == "interactive":
        interactive_review(args.reviewed)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
