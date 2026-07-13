#!/usr/bin/env python3
"""
Clean up unusual Unicode characters in litclock_annotated.csv.
Normalizes:
- Mathematical italic letters to regular ASCII
- Various dash types to standard hyphen/dash
- Curly quotes to straight quotes
- Other typographic characters

Tracks changed rows for image regeneration.
"""

import csv
from pathlib import Path

INPUT_FILE = Path(__file__).parent / "litclock_annotated.csv"
OUTPUT_FILE = Path(__file__).parent / "litclock_annotated.csv"
BACKUP_FILE = Path(__file__).parent / "litclock_annotated.csv.pre_cleanup"
CHANGES_FILE = Path(__file__).parent / "quotes_changed_for_cleanup.txt"

# Mathematical italic letters (U+1D622 - U+1D63B for lowercase, etc.)
MATH_ITALIC_MAP = {
    # Uppercase
    "𝘈": "A",
    "𝘉": "B",
    "𝘊": "C",
    "𝘋": "D",
    "𝘌": "E",
    "𝘍": "F",
    "𝘎": "G",
    "𝘏": "H",
    "𝘐": "I",
    "𝘑": "J",
    "𝘒": "K",
    "𝘓": "L",
    "𝘔": "M",
    "𝘕": "N",
    "𝘖": "O",
    "𝘗": "P",
    "𝘘": "Q",
    "𝘙": "R",
    "𝘚": "S",
    "𝘛": "T",
    "𝘜": "U",
    "𝘝": "V",
    "𝘞": "W",
    "𝘟": "X",
    "𝘠": "Y",
    "𝘡": "Z",
    # Lowercase
    "𝘢": "a",
    "𝘣": "b",
    "𝘤": "c",
    "𝘥": "d",
    "𝘦": "e",
    "𝘧": "f",
    "𝘨": "g",
    "𝘩": "h",
    "𝘪": "i",
    "𝘫": "j",
    "𝘬": "k",
    "𝘭": "l",
    "𝘮": "m",
    "𝘯": "n",
    "𝘰": "o",
    "𝘱": "p",
    "𝘲": "q",
    "𝘳": "r",
    "𝘴": "s",
    "𝘵": "t",
    "𝘶": "u",
    "𝘷": "v",
    "𝘸": "w",
    "𝘹": "x",
    "𝘺": "y",
    "𝘻": "z",
}

# Other character replacements
OTHER_REPLACEMENTS = {
    # Quotes
    "\u2018": "'",  # Left single quote
    "\u2019": "'",  # Right single quote
    "\u201c": '"',  # Left double quote
    "\u201d": '"',  # Right double quote
    "\u02bc": "'",  # Modifier letter apostrophe
    "\u00b4": "'",  # Acute accent (sometimes used as apostrophe)
    # Dashes
    "\u2011": "-",  # Non-breaking hyphen
    "\u2013": "-",  # En-dash
    "\u2014": "-",  # Em-dash (consider keeping as ' - ' for readability)
    "\u2212": "-",  # Minus sign
    # Other
    "\u2026": "...",  # Ellipsis
    "\u2032": "'",  # Prime (used as apostrophe sometimes)
}


def normalize_text(text: str) -> str:
    """Normalize text by replacing unusual characters."""
    result = text

    # Replace mathematical italic letters
    for char, replacement in MATH_ITALIC_MAP.items():
        result = result.replace(char, replacement)

    # Replace other characters
    for char, replacement in OTHER_REPLACEMENTS.items():
        result = result.replace(char, replacement)

    return result


def main():
    print(f"Reading from {INPUT_FILE}...")

    # Read all rows
    rows = []
    with open(INPUT_FILE, encoding="utf-8") as f:
        reader = csv.reader(f, delimiter="|")
        for row in reader:
            rows.append(row)

    print(f"Read {len(rows)} rows")

    # Process and track changes
    changed_rows = []  # List of (row_num, time, old_phrase, new_phrase, old_quote_snippet, new_quote_snippet)
    new_rows = []

    for row_num, row in enumerate(rows, 1):
        if len(row) < 5:
            new_rows.append(row)
            continue

        time_str = row[0]
        phrase = row[1]
        quote = row[2]
        title = row[3]
        author = row[4]

        # Normalize
        new_phrase = normalize_text(phrase)
        new_quote = normalize_text(quote)
        new_title = normalize_text(title)
        new_author = normalize_text(author)

        # Check if anything changed
        if phrase != new_phrase or quote != new_quote or title != new_title or author != new_author:
            changed_rows.append(
                {
                    "row": row_num,
                    "time": time_str,
                    "old_phrase": phrase,
                    "new_phrase": new_phrase,
                    "old_quote": quote[:80] + "..." if len(quote) > 80 else quote,
                    "new_quote": new_quote[:80] + "..." if len(new_quote) > 80 else new_quote,
                    "title": title,
                }
            )

        new_rows.append([time_str, new_phrase, new_quote, new_title, new_author] + row[5:])

    print(f"Found {len(changed_rows)} rows with changes")

    if not changed_rows:
        print("No changes needed!")
        return

    # Create backup
    print(f"Creating backup at {BACKUP_FILE}...")
    with open(BACKUP_FILE, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter="|")
        for row in rows:
            writer.writerow(row)

    # Write cleaned file
    print(f"Writing cleaned file to {OUTPUT_FILE}...")
    with open(OUTPUT_FILE, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter="|")
        for row in new_rows:
            writer.writerow(row)

    # Write changes log
    print(f"Writing changes log to {CHANGES_FILE}...")
    with open(CHANGES_FILE, "w", encoding="utf-8") as f:
        f.write("# Quotes Changed During Cleanup\n")
        f.write("# These quotes need image regeneration\n")
        f.write(f"# Total changes: {len(changed_rows)}\n\n")

        for change in changed_rows:
            f.write(f"Row {change['row']}: {change['time']}\n")
            f.write(f"  Title: {change['title']}\n")
            if change["old_phrase"] != change["new_phrase"]:
                f.write(f"  Phrase: {change['old_phrase']} -> {change['new_phrase']}\n")
            if change["old_quote"] != change["new_quote"]:
                f.write("  Quote changed\n")
            f.write("\n")

        # Also write just the times for easy processing
        f.write("\n# Times needing regeneration (one per line):\n")
        times_changed = sorted(set(c["time"] for c in changed_rows))
        for t in times_changed:
            f.write(f"{t}\n")

    print("\nCleanup complete!")
    print(f"  Changed: {len(changed_rows)} rows")
    print(f"  Backup: {BACKUP_FILE}")
    print(f"  Changes: {CHANGES_FILE}")


if __name__ == "__main__":
    main()
