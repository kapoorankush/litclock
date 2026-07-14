#!/usr/bin/env python3
"""
NSFW Content Detection Script for Literary Clock Quotes

This script scans quotes for potentially NSFW content using:
1. Keyword matching (Tier 1) - Fast, catches obvious cases
2. LLM classification (Tier 2) - Optional, for nuanced detection

Output: CSV file with flagged quotes for human review
"""

import argparse
import csv
import json
import os
import re
from pathlib import Path

# Optional: Anthropic API for LLM classification
try:
    import anthropic

    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

SCRIPT_DIR = Path(__file__).parent
KEYWORDS_FILE = SCRIPT_DIR / "nsfw_keywords.txt"
CSV_FILE = SCRIPT_DIR / "litclock_annotated.csv"
OUTPUT_FILE = SCRIPT_DIR / "nsfw_flagged_for_review.csv"


def load_keywords(keywords_file: Path) -> dict[str, list[str]]:
    """Load keywords from file, organized by category."""
    keywords = {}
    current_category = "UNCATEGORIZED"

    with open(keywords_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                if line.startswith("## "):
                    current_category = line[3:].strip()
                    keywords[current_category] = []
                continue

            if current_category not in keywords:
                keywords[current_category] = []
            keywords[current_category].append(line.lower())

    return keywords


def check_keywords(text: str, keywords: dict[str, list[str]]) -> list[tuple[str, str]]:
    """
    Check text for keyword matches.
    Returns list of (category, matched_keyword) tuples.
    """
    text_lower = text.lower()
    matches = []

    for category, words in keywords.items():
        for word in words:
            # Use word boundary matching for better accuracy
            pattern = r"\b" + re.escape(word)
            if re.search(pattern, text_lower):
                matches.append((category, word))

    return matches


def classify_with_llm(quotes: list[dict], api_key: str, batch_size: int = 20) -> dict[int, dict]:
    """
    Use Claude API to classify quotes for NSFW content.
    Returns dict mapping quote index to classification result.
    """
    if not HAS_ANTHROPIC:
        print("Warning: anthropic package not installed, skipping LLM classification")
        return {}

    client = anthropic.Anthropic(api_key=api_key)
    results = {}

    for i in range(0, len(quotes), batch_size):
        batch = quotes[i : i + batch_size]

        # Build prompt with numbered quotes
        quotes_text = "\n\n".join(
            [
                f'[{j + 1}] "{q["quote"][:500]}..." - {q["title"]} by {q["author"]}'
                if len(q["quote"]) > 500
                else f'[{j + 1}] "{q["quote"]}" - {q["title"]} by {q["author"]}'
                for j, q in enumerate(batch)
            ]
        )

        prompt = f"""Analyze these literary quotes for content that may be unsuitable for general audiences \
(including children). For each quote, classify as:
- NSFW: Contains explicit sexual content, graphic violence, strong profanity, slurs, or explicit drug use
- SFW: Appropriate for general audiences
- REVIEW: Uncertain, needs human review (mild innuendo, contextual violence, etc.)

Return a JSON array with objects containing:
- "number": the quote number
- "classification": "NSFW", "SFW", or "REVIEW"
- "reason": brief explanation (10 words max)

Quotes to analyze:

{quotes_text}

Return ONLY the JSON array, no other text."""

        try:
            response = client.messages.create(
                model="claude-sonnet-4-20250514", max_tokens=2000, messages=[{"role": "user", "content": prompt}]
            )

            # Parse response
            response_text = response.content[0].text.strip()
            # Handle potential markdown code blocks
            if response_text.startswith("```"):
                response_text = re.sub(r"^```\w*\n?", "", response_text)
                response_text = re.sub(r"\n?```$", "", response_text)

            classifications = json.loads(response_text)

            for item in classifications:
                quote_idx = i + item["number"] - 1
                results[quote_idx] = {"classification": item["classification"], "reason": item.get("reason", "")}

        except Exception as e:
            print(f"Warning: LLM classification failed for batch {i // batch_size + 1}: {e}")
            continue

        # Progress indicator
        print(f"  LLM processed {min(i + batch_size, len(quotes))}/{len(quotes)} quotes...")

    return results


def load_quotes(csv_file: Path) -> list[dict]:
    """Load quotes from CSV file."""
    quotes = []

    with open(csv_file, encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f, delimiter="|")
        for row_num, row in enumerate(reader):
            if len(row) < 5:
                continue

            quotes.append(
                {
                    "row_num": row_num,
                    "time": row[0],
                    "time_phrase": row[1],
                    "quote": row[2],
                    "title": row[3],
                    "author": row[4],
                    "current_nsfw": row[5].strip().upper() if len(row) > 5 else "NO",
                }
            )

    return quotes


def main():
    parser = argparse.ArgumentParser(description="Detect NSFW content in literary quotes")
    parser.add_argument(
        "--use-llm", action="store_true", help="Use LLM for additional classification (requires ANTHROPIC_API_KEY)"
    )
    parser.add_argument(
        "--keywords-only", action="store_true", help="Only use keyword detection (skip LLM even if --use-llm is set)"
    )
    parser.add_argument(
        "--output", type=Path, default=OUTPUT_FILE, help=f"Output file for flagged quotes (default: {OUTPUT_FILE.name})"
    )
    parser.add_argument("--csv", type=Path, default=CSV_FILE, help=f"Input CSV file (default: {CSV_FILE.name})")
    args = parser.parse_args()

    print("=" * 60)
    print("NSFW Content Detection Script")
    print("=" * 60)

    # Load keywords
    print(f"\nLoading keywords from {KEYWORDS_FILE.name}...")
    keywords = load_keywords(KEYWORDS_FILE)
    total_keywords = sum(len(words) for words in keywords.values())
    print(f"  Loaded {total_keywords} keywords in {len(keywords)} categories")

    # Load quotes
    print(f"\nLoading quotes from {args.csv.name}...")
    quotes = load_quotes(args.csv)
    print(f"  Loaded {len(quotes)} quotes")

    # Tier 1: Keyword detection
    print("\n[Tier 1] Running keyword detection...")
    flagged = []
    keyword_flagged_indices = set()

    for idx, quote in enumerate(quotes):
        matches = check_keywords(quote["quote"], keywords)
        if matches:
            categories = list(set(m[0] for m in matches))
            matched_words = list(set(m[1] for m in matches))
            flagged.append(
                {
                    **quote,
                    "flag_source": "KEYWORD",
                    "flag_categories": ", ".join(categories),
                    "flag_details": ", ".join(matched_words[:5]),  # Limit to 5 words
                    "suggested_nsfw": "YES",
                }
            )
            keyword_flagged_indices.add(idx)

    print(f"  Keyword detection flagged {len(flagged)} quotes")

    # Tier 2: LLM classification (optional)
    if args.use_llm and not args.keywords_only:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            print("\nWarning: ANTHROPIC_API_KEY not set, skipping LLM classification")
        elif not HAS_ANTHROPIC:
            print("\nWarning: anthropic package not installed, skipping LLM classification")
        else:
            print("\n[Tier 2] Running LLM classification on non-flagged quotes...")

            # Only classify quotes not already flagged by keywords
            unflagged_quotes = [(idx, q) for idx, q in enumerate(quotes) if idx not in keyword_flagged_indices]

            print(f"  Analyzing {len(unflagged_quotes)} quotes...")

            # Prepare quotes for LLM
            llm_input = [q for _, q in unflagged_quotes]
            llm_results = classify_with_llm(llm_input, api_key)

            # Process LLM results
            llm_flagged = 0
            for batch_idx, result in llm_results.items():
                if result["classification"] in ("NSFW", "REVIEW"):
                    original_idx, quote = unflagged_quotes[batch_idx]
                    flagged.append(
                        {
                            **quote,
                            "flag_source": "LLM",
                            "flag_categories": result["classification"],
                            "flag_details": result.get("reason", ""),
                            "suggested_nsfw": "YES" if result["classification"] == "NSFW" else "REVIEW",
                        }
                    )
                    llm_flagged += 1

            print(f"  LLM classification flagged {llm_flagged} additional quotes")

    # Write output
    print(f"\nWriting {len(flagged)} flagged quotes to {args.output.name}...")

    with open(args.output, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter="|")
        writer.writerow(
            [
                "ROW_NUM",
                "TIME",
                "QUOTE_PREVIEW",
                "TITLE",
                "AUTHOR",
                "FLAG_SOURCE",
                "FLAG_CATEGORIES",
                "FLAG_DETAILS",
                "SUGGESTED_NSFW",
                "FINAL_DECISION",
            ]
        )

        for item in flagged:
            # Truncate quote for preview
            quote_preview = item["quote"][:150] + "..." if len(item["quote"]) > 150 else item["quote"]

            writer.writerow(
                [
                    item["row_num"],
                    item["time"],
                    quote_preview,
                    item["title"],
                    item["author"],
                    item["flag_source"],
                    item["flag_categories"],
                    item["flag_details"],
                    item["suggested_nsfw"],
                    "",  # FINAL_DECISION - to be filled by human reviewer
                ]
            )

    # Summary
    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"Total quotes scanned: {len(quotes)}")
    print(f"Flagged for review: {len(flagged)}")
    print(f"  - By keywords: {len(keyword_flagged_indices)}")
    if args.use_llm and not args.keywords_only:
        print(f"  - By LLM: {len(flagged) - len(keyword_flagged_indices)}")
    print(f"\nOutput written to: {args.output}")
    print("\nNext steps:")
    print("  1. Review the flagged quotes in the output file")
    print("  2. Fill in FINAL_DECISION column (YES/NO)")
    print("  3. Run review_nsfw.py to merge decisions back to CSV")


if __name__ == "__main__":
    main()
