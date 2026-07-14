#!/usr/bin/env python3
"""
Gather literary quotes from multiple open-source projects to expand the quote database.
Tracks progress in progress.json for safe resume after interruptions.
"""

import csv
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Configuration
SCRIPT_DIR = Path(__file__).parent
DOWNLOADS_DIR = SCRIPT_DIR / "downloads"
PROGRESS_FILE = SCRIPT_DIR / "progress.json"
CSV_FILE = SCRIPT_DIR / "litclock_annotated.csv"
BACKUP_FILE = SCRIPT_DIR / "litclock_annotated.csv.backup"
REPORT_FILE = SCRIPT_DIR / "coverage_report.txt"

# Data sources
SOURCES = {
    "cdmoro": {
        "url": "https://raw.githubusercontent.com/cdmoro/literature-clock/main/quotes/quotes.en-US.csv",
        "format": "csv",
        "delimiter": "|",
    },
    "johannesne": {
        "url": "https://raw.githubusercontent.com/JohannesNE/literature-clock/master/litclock_annotated.csv",
        "format": "csv",
        "delimiter": "|",
    },
    "arthurgassner": {
        "url": "https://raw.githubusercontent.com/arthurgassner/timeteller/main/data/litclock_annotated.csv",
        "format": "csv",
        "delimiter": "|",
    },
}


def load_progress():
    """Load progress from JSON file or return empty state."""
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {
        "step": "start",
        "sources_completed": [],
        "quotes_added": 0,
        "last_updated": None,
    }


def save_progress(progress):
    """Save progress to JSON file."""
    progress["last_updated"] = datetime.now().isoformat()
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)
    print(f"Progress saved: {progress['step']}")


def download_file(url, dest_path):
    """Download a file from URL to destination path."""
    print(f"Downloading: {url}")
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=30) as response:
            content = response.read()
            with open(dest_path, "wb") as f:
                f.write(content)
        print(f"Saved to: {dest_path}")
        return True
    except (URLError, HTTPError) as e:
        print(f"Error downloading {url}: {e}")
        return False


def normalize_time(time_str):
    """Normalize time to HH:MM format."""
    time_str = time_str.strip()
    # Handle various formats
    match = re.match(r"(\d{1,2}):(\d{2})", time_str)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
    return None


def clean_html(text):
    """Remove HTML tags from text."""
    if not text:
        return ""
    # Remove common HTML tags
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    # Clean up whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def validate_time_phrase(quote, time_phrase):
    """Check if time_phrase exists within quote (case-insensitive)."""
    if not quote or not time_phrase:
        return False
    return time_phrase.lower() in quote.lower()


def parse_csv_source(filepath, delimiter="|"):
    """Parse a CSV file and return normalized quotes."""
    quotes = []
    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            reader = csv.reader(f, delimiter=delimiter)
            for row in reader:
                # Detect format based on number of columns
                # cdmoro format: Time|Id|Quote time|Quote|Title|Author|SFW (7 columns)
                # standard format: time|time_phrase|quote|title|author (5 columns)
                if len(row) >= 7 and row[1] and "-" in row[1]:
                    # cdmoro format (has ID like "0000-000")
                    time_str = row[0]
                    time_phrase = row[2]
                    quote = row[3]
                    title = row[4]
                    author = row[5]
                elif len(row) >= 5:
                    time_str, time_phrase, quote, title, author = row[:5]
                else:
                    continue

                time_norm = normalize_time(time_str)
                if time_norm:
                    quote_clean = clean_html(quote)
                    time_phrase_clean = clean_html(time_phrase)
                    if validate_time_phrase(quote_clean, time_phrase_clean):
                        quotes.append(
                            {
                                "time": time_norm,
                                "time_phrase": time_phrase_clean,
                                "quote": quote_clean,
                                "title": clean_html(title).strip(),
                                "author": clean_html(author).strip(),
                            }
                        )
    except Exception as e:
        print(f"Error parsing {filepath}: {e}")
    return quotes


def parse_json_source(filepath):
    """Parse ambercaravalho JSON format and return normalized quotes."""
    quotes = []
    try:
        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)

        # Handle the nested structure: {"00:00": [{"quote": ..., "title": ..., "author": ..., "time": ...}, ...]}
        for time_key, entries in data.items():
            time_norm = normalize_time(time_key)
            if time_norm and isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict):
                        quote = clean_html(entry.get("quote", ""))
                        time_phrase = clean_html(entry.get("time", ""))
                        title = clean_html(entry.get("title", ""))
                        author = clean_html(entry.get("author", ""))

                        if quote and time_phrase and validate_time_phrase(quote, time_phrase):
                            quotes.append(
                                {
                                    "time": time_norm,
                                    "time_phrase": time_phrase,
                                    "quote": quote,
                                    "title": title,
                                    "author": author,
                                }
                            )
    except Exception as e:
        print(f"Error parsing {filepath}: {e}")
    return quotes


def load_existing_quotes():
    """Load existing quotes from the CSV file."""
    quotes = []
    if CSV_FILE.exists():
        with open(CSV_FILE, encoding="utf-8", errors="replace") as f:
            reader = csv.reader(f, delimiter="|")
            for row in reader:
                if len(row) >= 5:
                    quotes.append(
                        {
                            "time": row[0],
                            "time_phrase": row[1],
                            "quote": row[2],
                            "title": row[3],
                            "author": row[4],
                        }
                    )
    return quotes


def dedupe_key(quote):
    """Generate a deduplication key for a quote."""
    # Use time + first 100 chars of quote + title
    quote_prefix = quote["quote"][:100].lower().strip() if quote["quote"] else ""
    title = quote["title"].lower().strip() if quote["title"] else ""
    return (quote["time"], quote_prefix, title)


def merge_quotes(existing, new_quotes):
    """Merge new quotes with existing, removing duplicates."""
    seen = set()
    merged = []

    # Add existing quotes first
    for q in existing:
        key = dedupe_key(q)
        if key not in seen:
            seen.add(key)
            merged.append(q)

    # Add new quotes
    added = 0
    for q in new_quotes:
        key = dedupe_key(q)
        if key not in seen:
            seen.add(key)
            merged.append(q)
            added += 1

    return merged, added


def write_csv(quotes, filepath):
    """Write quotes to CSV file."""
    # Sort by time
    quotes_sorted = sorted(quotes, key=lambda q: q["time"])

    with open(filepath, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter="|", quoting=csv.QUOTE_NONE, escapechar="\\")
        for q in quotes_sorted:
            writer.writerow([q["time"], q["time_phrase"], q["quote"], q["title"], q["author"]])

    print(f"Wrote {len(quotes_sorted)} quotes to {filepath}")


def generate_report(quotes):
    """Generate coverage statistics report."""
    # Count quotes per time
    times = {}
    for q in quotes:
        t = q["time"]
        times[t] = times.get(t, 0) + 1

    # Find missing times
    all_times = set()
    for h in range(24):
        for m in range(60):
            all_times.add(f"{h:02d}:{m:02d}")

    covered = set(times.keys())
    missing = sorted(all_times - covered)

    # Stats per hour
    hour_stats = {}
    for h in range(24):
        hour_times = [f"{h:02d}:{m:02d}" for m in range(60)]
        covered_in_hour = sum(1 for t in hour_times if t in covered)
        hour_stats[h] = {"covered": covered_in_hour, "missing": 60 - covered_in_hour}

    # Generate report
    report = []
    report.append("=" * 60)
    report.append("LITERARY CLOCK QUOTE COVERAGE REPORT")
    report.append(f"Generated: {datetime.now().isoformat()}")
    report.append("=" * 60)
    report.append("")
    report.append(f"Total quotes: {len(quotes)}")
    report.append(f"Unique times covered: {len(covered)}/1440 ({100 * len(covered) / 1440:.2f}%)")
    report.append(f"Missing times: {len(missing)}")
    report.append("")
    report.append("-" * 60)
    report.append("COVERAGE BY HOUR")
    report.append("-" * 60)

    for h in range(24):
        stats = hour_stats[h]
        bar = "#" * (stats["covered"] // 2) + "." * ((60 - stats["covered"]) // 2)
        report.append(f"{h:02d}:00  [{bar:30}] {stats['covered']}/60 ({stats['missing']} missing)")

    report.append("")
    report.append("-" * 60)
    report.append("MISSING TIMES")
    report.append("-" * 60)

    if missing:
        # Group by hour
        for h in range(24):
            hour_missing = [t for t in missing if t.startswith(f"{h:02d}:")]
            if hour_missing:
                report.append(f"{h:02d}:xx - {', '.join(t.split(':')[1] for t in hour_missing)}")
    else:
        report.append("None! Full coverage achieved!")

    report.append("")
    report.append("=" * 60)

    return "\n".join(report)


def main():
    """Main execution flow."""
    print("=" * 60)
    print("Literary Clock Quote Gatherer")
    print("=" * 60)

    # Ensure downloads directory exists
    DOWNLOADS_DIR.mkdir(exist_ok=True)

    # Load progress
    progress = load_progress()
    print(f"Resuming from step: {progress['step']}")

    all_new_quotes = []

    # Step 1: Download sources
    for source_name, source_config in SOURCES.items():
        if source_name in progress["sources_completed"]:
            print(f"Skipping {source_name} (already completed)")
            # Load cached quotes
            ext = "json" if source_config["format"] == "json" else "csv"
            cached_file = DOWNLOADS_DIR / f"{source_name}.{ext}"
            if cached_file.exists():
                if source_config["format"] == "json":
                    quotes = parse_json_source(cached_file)
                else:
                    quotes = parse_csv_source(cached_file, source_config.get("delimiter", "|"))
                all_new_quotes.extend(quotes)
                print(f"  Loaded {len(quotes)} quotes from cache")
            continue

        print(f"\nProcessing source: {source_name}")
        ext = "json" if source_config["format"] == "json" else "csv"
        dest_file = DOWNLOADS_DIR / f"{source_name}.{ext}"

        if not dest_file.exists():
            if not download_file(source_config["url"], dest_file):
                print(f"Failed to download {source_name}, skipping...")
                continue

        # Parse the source
        if source_config["format"] == "json":
            quotes = parse_json_source(dest_file)
        else:
            quotes = parse_csv_source(dest_file, source_config.get("delimiter", "|"))

        print(f"  Parsed {len(quotes)} valid quotes")
        all_new_quotes.extend(quotes)

        # Mark source as completed
        progress["sources_completed"].append(source_name)
        save_progress(progress)

    print(f"\nTotal new quotes from all sources: {len(all_new_quotes)}")

    # Step 2: Backup existing CSV (once)
    if not BACKUP_FILE.exists() and CSV_FILE.exists():
        import shutil

        shutil.copy(CSV_FILE, BACKUP_FILE)
        print(f"Created backup: {BACKUP_FILE}")

    # Step 3: Load existing and merge
    existing = load_existing_quotes()
    print(f"Existing quotes: {len(existing)}")

    merged, added = merge_quotes(existing, all_new_quotes)
    print(f"After merge: {len(merged)} total quotes ({added} new)")

    progress["quotes_added"] = added
    progress["step"] = "merged"
    save_progress(progress)

    # Step 4: Write updated CSV
    write_csv(merged, CSV_FILE)
    progress["step"] = "csv_written"
    save_progress(progress)

    # Step 5: Generate report
    report = generate_report(merged)
    with open(REPORT_FILE, "w") as f:
        f.write(report)
    print(f"\nCoverage report written to: {REPORT_FILE}")
    print("\n" + report)

    progress["step"] = "complete"
    save_progress(progress)

    print("\n" + "=" * 60)
    print("COMPLETE!")
    print("=" * 60)


if __name__ == "__main__":
    main()
