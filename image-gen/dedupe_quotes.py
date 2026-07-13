#!/usr/bin/env python3
"""
Deduplicate quotes in litclock_annotated.csv.

Subcommands:
  stats   — Print duplicate statistics without writing files
  detect  — Generate a review CSV (litclock_duplicates_for_review.csv)
  apply   — Apply reviewed decisions back to the main CSV
"""

import argparse
import csv
import re
import shutil
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
INPUT_FILE = SCRIPT_DIR / "litclock_annotated.csv"
REVIEW_FILE = SCRIPT_DIR / "litclock_duplicates_for_review.csv"
BACKUP_FILE = SCRIPT_DIR / "litclock_annotated.csv.pre_dedupe_backup"

FIELDNAMES = ["TIME", "TIME_PHRASE", "QUOTE", "TITLE", "AUTHOR", "IS_NSFW"]
REVIEW_FIELDNAMES = ["GROUP_ID", "KEEP"] + FIELDNAMES


def normalize_for_comparison(text):
    """Normalize text for duplicate detection (from clean_csv.py)."""
    normalized = re.sub(r"[^\w\s]", "", text.lower())
    return " ".join(normalized.split())


def parse_line(line):
    """Parse a pipe-delimited line into a dict. Returns None for invalid lines."""
    parts = line.split("|")
    if len(parts) < 5:
        return None
    return {
        "TIME": parts[0].strip(),
        "TIME_PHRASE": parts[1].strip(),
        "QUOTE": parts[2].strip(),
        "TITLE": parts[3].strip(),
        "AUTHOR": parts[4].strip() if len(parts) > 4 else "",
        "IS_NSFW": parts[5].strip() if len(parts) > 5 else "",
    }


def read_csv(path):
    """Read the pipe-delimited CSV into a list of dicts."""
    rows = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = parse_line(line)
            if row is not None:
                rows.append(row)
    return rows


def read_raw_lines(path):
    """Read CSV as raw lines, preserving original formatting including line endings."""
    with open(path, "rb") as f:
        content = f.read()
    # Detect line ending style
    if b"\r\n" in content:
        line_ending = "\r\n"
    else:
        line_ending = "\n"
    lines = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            stripped = line.rstrip("\n").rstrip("\r")
            if stripped:
                lines.append(stripped)
    return lines, line_ending


def find_duplicates(rows):
    """Group rows by normalized key, return groups with >1 row."""
    groups = defaultdict(list)
    for i, row in enumerate(rows):
        key = (
            row["TIME"],
            normalize_for_comparison(row["QUOTE"]),
            normalize_for_comparison(row["AUTHOR"]),
        )
        groups[key].append((i, row))

    duplicates = {}
    group_id = 0
    for _key, members in groups.items():
        if len(members) > 1:
            group_id += 1
            duplicates[group_id] = members
    return duplicates


def score_row(row):
    """Score a row to determine which duplicate to keep.

    Higher score = prefer to keep.
    """
    score = 0
    # Prefer rows without double-escaped quotes
    if '""' not in row["QUOTE"]:
        score += 2
    # Prefer longer author names (fuller attribution)
    score += len(row["AUTHOR"]) / 100
    # Prefer rows with explicit NSFW field
    if row["IS_NSFW"] in ("YES", "NO"):
        score += 1
    return score


def cmd_stats(args):
    """Print duplicate statistics."""
    rows = read_csv(INPUT_FILE)
    duplicates = find_duplicates(rows)

    total_dupes = sum(len(members) - 1 for members in duplicates.values())
    print(f"Total rows: {len(rows)}")
    print(f"Duplicate groups: {len(duplicates)}")
    print(f"Duplicate rows to remove: {total_dupes}")
    print(f"Rows after dedup: {len(rows) - total_dupes}")

    if args.verbose:
        print("\n--- Duplicate groups ---")
        for gid, members in duplicates.items():
            print(f"\nGroup {gid} (time={members[0][1]['TIME']}):")
            for _idx, row in members:
                print(f"  QUOTE: {row['QUOTE'][:80]}...")
                print(f"  AUTHOR: {row['AUTHOR']}")
                print()


def cmd_detect(args):
    """Generate review CSV with auto-selected KEEP flags."""
    rows = read_csv(INPUT_FILE)
    duplicates = find_duplicates(rows)

    if not duplicates:
        print("No duplicates found.")
        return

    total_dupes = sum(len(members) - 1 for members in duplicates.values())

    review_rows = []
    for gid, members in duplicates.items():
        # Score each member and pick the best
        scored = [(score_row(row), idx, row) for idx, row in members]
        scored.sort(key=lambda x: x[0], reverse=True)
        best_idx = scored[0][1]

        for idx, row in members:
            review_row = {
                "GROUP_ID": str(gid),
                "KEEP": "YES" if idx == best_idx else "NO",
            }
            review_row.update(row)
            review_rows.append(review_row)

    with open(REVIEW_FILE, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=REVIEW_FIELDNAMES, delimiter="|")
        writer.writeheader()
        writer.writerows(review_rows)

    print(f"Duplicate groups: {len(duplicates)}")
    print(f"Duplicate rows to remove: {total_dupes}")
    print(f"Review file written to: {REVIEW_FILE}")
    print("Edit the KEEP column (YES/NO) then run: dedupe_quotes.py apply")


def cmd_apply(args):
    """Apply reviewed decisions from the review CSV."""
    if not REVIEW_FILE.exists():
        print(f"Error: {REVIEW_FILE} not found. Run 'detect' first.")
        return

    rows = read_csv(INPUT_FILE)
    raw_lines, line_ending = read_raw_lines(INPUT_FILE)
    duplicates = find_duplicates(rows)

    with open(REVIEW_FILE, encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="|")
        review_rows = list(reader)

    # Validate: every group must have at least one KEEP=YES
    groups_from_review = defaultdict(list)
    for rrow in review_rows:
        groups_from_review[rrow["GROUP_ID"]].append(rrow)

    empty_groups = []
    for gid_str, members in groups_from_review.items():
        if not any(m["KEEP"] == "YES" for m in members):
            time = members[0]["TIME"]
            quote_preview = members[0]["QUOTE"][:60]
            empty_groups.append(f"  Group {gid_str} (time={time}): {quote_preview}...")

    if empty_groups:
        print("Error: these groups have no KEEP=YES row (would lose all versions):")
        for line in empty_groups:
            print(line)
        print("Fix the review CSV and re-run.")
        return

    # Match review rows to original rows to find indices to remove
    remove_indices = set()
    for gid_str, review_members in groups_from_review.items():
        gid = int(gid_str)
        if gid not in duplicates:
            continue

        original_members = duplicates[gid]

        for rrow in review_members:
            if rrow["KEEP"] == "YES":
                continue
            for orig_idx, orig_row in original_members:
                if (
                    orig_row["QUOTE"] == rrow["QUOTE"]
                    and orig_row["AUTHOR"] == rrow["AUTHOR"]
                    and orig_idx not in remove_indices
                ):
                    remove_indices.add(orig_idx)
                    break

    if not remove_indices:
        print("No rows to remove.")
        return

    # Backup original
    shutil.copy2(INPUT_FILE, BACKUP_FILE)
    print(f"Backup saved to: {BACKUP_FILE}")

    # Write back raw lines, preserving original order and formatting
    with open(INPUT_FILE, "w", encoding="utf-8", newline="") as f:
        for i, line in enumerate(raw_lines):
            if i not in remove_indices:
                f.write(line + line_ending)

    print(f"Rows before: {len(rows)}")
    print(f"Rows removed: {len(remove_indices)}")
    print(f"Rows after: {len(rows) - len(remove_indices)}")
    print(f"Updated: {INPUT_FILE}")


def main():
    parser = argparse.ArgumentParser(description="Deduplicate litclock_annotated.csv")
    subparsers = parser.add_subparsers(dest="command")

    stats_parser = subparsers.add_parser("stats", help="Print duplicate statistics")
    stats_parser.add_argument("-v", "--verbose", action="store_true", help="Show duplicate details")

    subparsers.add_parser("detect", help="Generate review CSV")
    subparsers.add_parser("apply", help="Apply reviewed dedup decisions")

    args = parser.parse_args()

    if args.command is None:
        args.command = "stats"
        args.verbose = False

    commands = {
        "stats": cmd_stats,
        "detect": cmd_detect,
        "apply": cmd_apply,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
