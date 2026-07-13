#!/usr/bin/env python3
"""
Clean up the litclock_annotated.csv file in place:
1. Remove various levels of escape sequences
2. Remove duplicate entries

The original master is backed up to litclock_annotated.csv.pre_clean_csv_backup
before being overwritten.
"""

import re
import shutil
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
INPUT_FILE = SCRIPT_DIR / "litclock_annotated.csv"
BACKUP_FILE = SCRIPT_DIR / "litclock_annotated.csv.pre_clean_csv_backup"
OUTPUT_FILE = INPUT_FILE


def clean_text(text):
    """Clean up escape sequences from text."""
    # Remove all backslash escaping before quotes (any number of backslashes)
    # This handles \\\\\\", \\", \" etc.
    text = re.sub(r'\\+(")', r"\1", text)
    # Clean escaped newlines (any number of backslashes before n)
    text = re.sub(r"\\+n", " ", text)
    # Remove trailing backslashes
    text = re.sub(r"\\+$", "", text)
    # Remove any remaining standalone backslashes
    text = re.sub(r"\\+", "", text)
    # Normalize whitespace
    text = " ".join(text.split())
    return text.strip()


def normalize_for_comparison(text):
    """Normalize text for duplicate detection."""
    # Remove all punctuation and extra spaces for comparison
    normalized = re.sub(r"[^\w\s]", "", text.lower())
    normalized = " ".join(normalized.split())
    return normalized


def main():
    rows = []
    seen = set()
    duplicates_removed = 0

    with open(INPUT_FILE, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            parts = line.split("|")
            if len(parts) < 5:
                continue

            # Clean each field
            time_str = parts[0].strip()
            timestring = clean_text(parts[1])
            quote = clean_text(parts[2])
            title = clean_text(parts[3])
            author = clean_text(parts[4] if len(parts) > 4 else "")

            # Create a key for duplicate detection
            # Use time + normalized quote + author to identify duplicates
            key = (time_str, normalize_for_comparison(quote), normalize_for_comparison(author))

            if key in seen:
                duplicates_removed += 1
                continue

            seen.add(key)
            rows.append(f"{time_str}|{timestring}|{quote}|{title}|{author}")

    # Back up the master before overwriting in place
    shutil.copy2(INPUT_FILE, BACKUP_FILE)

    # Write cleaned CSV
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(row + "\n")

    print(f"Original entries: {len(rows) + duplicates_removed}")
    print(f"Duplicates removed: {duplicates_removed}")
    print(f"Clean entries: {len(rows)}")
    print(f"Output written to: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
