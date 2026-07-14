#!/usr/bin/env python3
"""
Prepare a clean CSV for manual review of scraped quotes.
Adds intelligent NEEDS_REVIEW flagging based on extraction quality.
"""

import csv
import re
from pathlib import Path

# placeholder — set to your source's file name before running
INPUT_FILE = Path(__file__).parent / "scraped_quotes.csv"
OUTPUT_FILE = Path(__file__).parent / "quotes_for_review.csv"

# Number words to digits mapping
NUMBER_WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "twenty-one": 21,
    "twenty-two": 22,
    "twenty-three": 23,
    "twenty-four": 24,
    "twenty-five": 25,
    "twenty-six": 26,
    "twenty-seven": 27,
    "twenty-eight": 28,
    "twenty-nine": 29,
    "thirty": 30,
    "thirty-one": 31,
    "thirty-two": 32,
    "thirty-three": 33,
    "thirty-four": 34,
    "thirty-five": 35,
    "thirty-six": 36,
    "thirty-seven": 37,
    "thirty-eight": 38,
    "thirty-nine": 39,
    "forty": 40,
    "forty-one": 41,
    "forty-two": 42,
    "forty-three": 43,
    "forty-four": 44,
    "forty-five": 45,
    "forty-six": 46,
    "forty-seven": 47,
    "forty-eight": 48,
    "forty-nine": 49,
    "fifty": 50,
    "fifty-one": 51,
    "fifty-two": 52,
    "fifty-three": 53,
    "fifty-four": 54,
    "fifty-five": 55,
    "fifty-six": 56,
    "fifty-seven": 57,
    "fifty-eight": 58,
    "fifty-nine": 59,
    "noon": "noon",
    "midnight": "midnight",
}

# Reverse mapping: digit to word
DIGIT_TO_WORD = {v: k for k, v in NUMBER_WORDS.items() if isinstance(v, int)}

# Suspicious single words that are likely false positives (when alone)
SUSPICIOUS_WORDS = set(DIGIT_TO_WORD.values())

# Stats for tracking improvements
IMPROVEMENT_STATS = {
    "digital_match": 0,
    "text_pattern_match": 0,
    "ampm_added": 0,
    "ampm_validated": 0,
    "total_improved": 0,
}


def validate_ampm(hour_24: int, ampm_text: str | None) -> bool:
    """
    Validate that AM/PM matches the 24-hour time.
    Returns True if valid (or no AM/PM present), False if mismatch.
    """
    if not ampm_text:
        return True

    ampm_lower = ampm_text.lower().replace(".", "").replace(" ", "")
    is_am = "am" in ampm_lower
    is_pm = "pm" in ampm_lower

    if not is_am and not is_pm:
        return True

    # 12:xx AM = 00:xx (midnight hour)
    # 12:xx PM = 12:xx (noon hour)
    # 1-11 AM = 01:xx - 11:xx
    # 1-11 PM = 13:xx - 23:xx

    if is_am:
        if hour_24 == 0:
            return True  # 12:xx AM = midnight
        return 1 <= hour_24 <= 11
    else:  # PM
        if hour_24 == 12:
            return True  # 12:xx PM = noon
        return 13 <= hour_24 <= 23


def get_hour_12(hour_24: int) -> int:
    """Convert 24-hour to 12-hour format."""
    if hour_24 == 0:
        return 12
    elif hour_24 > 12:
        return hour_24 - 12
    return hour_24


def build_digital_patterns(hour: int, minute: int) -> list[tuple[str, str, str | None]]:
    """
    Build digital time patterns (e.g., 7:30, 7.30am).
    Returns list of (regex_pattern, description, expected_ampm) tuples.
    expected_ampm is 'am', 'pm', or None (for patterns without AM/PM).
    """
    patterns = []

    hour_12 = get_hour_12(hour)

    # Hour formats for both 24-hour and 12-hour
    hour_formats_24 = [str(hour)]
    if hour < 10:
        hour_formats_24.append(f"{hour:02d}")

    hour_formats_12 = [str(hour_12)]
    if hour_12 < 10:
        hour_formats_12.append(f"{hour_12:02d}")

    # Minute formats
    minute_formats = [f"{minute:02d}"]
    if minute % 10 == 0 and minute > 0:
        minute_formats.append(str(minute // 10))

    # Separators
    separators = [".", ":"]

    # AM/PM patterns - order by specificity (longer first)
    ampm_patterns = [
        (r"[\s.]*[Aa]\.?[Mm]\.?", "am"),
        (r"[\s.]*[Pp]\.?[Mm]\.?", "pm"),
        ("", None),
    ]

    # Build patterns - prioritize those with matching AM/PM
    for ampm_pattern, ampm_type in ampm_patterns:
        for sep in separators:
            sep_escaped = "\\." if sep == "." else sep

            # Use 12-hour format when AM/PM is present
            if ampm_type:
                for h in hour_formats_12:
                    for m in minute_formats:
                        pattern = f"(?<!\\d){h}{sep_escaped}{m}{ampm_pattern}"
                        desc = f"{h}{sep}{m} {ampm_type}"
                        patterns.append((pattern, desc, ampm_type))
            else:
                # No AM/PM - use 24-hour format
                for h in hour_formats_24:
                    for m in minute_formats:
                        pattern = f"(?<!\\d){h}{sep_escaped}{m}"
                        desc = f"{h}{sep}{m}"
                        patterns.append((pattern, desc, None))

    return patterns


def _flex(word: str) -> str:
    """Make a word form matchable with an optional hyphen or space at each dash."""
    return word.replace("-", "[-\\s]?")


def build_text_patterns(hour: int, minute: int) -> list[tuple[str, str]]:
    """
    Build text-based time patterns.
    Returns list of (regex_pattern, matched_group_description) tuples.

    Patterns include:
    - "X minutes past Y" / "X minutes to Y"
    - "half past X" / "quarter past X" / "quarter to X"
    - "X o'clock" (word, digit, and hyphenated forms)
    - Compound "{hour} {minute}" word forms ("twelve-thirty-five", "eight forty-three")
    - Word-hour + AM/PM ("Three A.M.")
    - Strike/chime/toll patterns ("clocks were striking thirteen", "clock struck eleven")
    - "X minutes after midnight" / "X minutes to midnight" / same for noon
    - "X minutes of Y o'clock [noon]" ("nine minutes of twelve o'clock noon")
    - Approximation prefixes ("nearly four", "about five")
    - "noon" / "midnight"
    """
    patterns = []
    hour_12 = get_hour_12(hour)

    # Get word forms for hours (12-hour, 24-hour, and next-hour for "to" patterns)
    hour_word = DIGIT_TO_WORD.get(hour_12, str(hour_12))
    hour_24_word = DIGIT_TO_WORD.get(hour, str(hour))
    next_hour = (hour_12 % 12) + 1
    next_hour_word = DIGIT_TO_WORD.get(next_hour, str(next_hour))

    hour_word_flex = _flex(hour_word)
    hour_24_word_flex = _flex(hour_24_word)
    next_hour_word_flex = _flex(next_hour_word)

    # Special cases for noon and midnight
    if hour == 12 and minute == 0:
        patterns.append((r"\bnoon\b", "noon"))
        patterns.append((r"\btwelve\s+(?:o\'?clock\s+)?noon\b", "twelve noon"))
    if hour == 0 and minute == 0:
        patterns.append((r"\bmidnight\b", "midnight"))
        patterns.append((r"\btwelve\s+(?:o\'?clock\s+)?midnight\b", "twelve midnight"))

    # "X o'clock" patterns (only for :00 times) — allow hyphen in "three-o'clock"
    if minute == 0:
        # Word form: "twelve o'clock" / "twelve-o'clock"
        patterns.append((rf"\b{hour_word_flex}[-\s]+o'?clock\b", f"{hour_word} o'clock"))
        # Digit form: "12 o'clock" / "12-o'clock"
        patterns.append((rf"\b{hour_12}[-\s]+o'?clock\b", f"{hour_12} o'clock"))
        # 24-hour word form — matches "striking thirteen" context in passive lookup.
        if hour_24_word != hour_word:
            patterns.append((rf"\b{hour_24_word_flex}[-\s]+o'?clock\b", f"{hour_24_word} o'clock"))

        # Word hour + AM/PM ("Three A.M."); only emit if AM/PM matches the 24h target.
        for ampm_text in ("am", "pm"):
            if validate_ampm(hour, ampm_text):
                ampm_re = r"[Aa]\.?[Mm]\.?" if ampm_text == "am" else r"[Pp]\.?[Mm]\.?"
                patterns.append((rf"\b{hour_word_flex}\s*[.,]?\s*{ampm_re}", f"{hour_word} {ampm_text.upper()}"))

        # Strike / chime / toll + hour word (matches "clocks were striking thirteen",
        # "clock struck eleven", "bells tolled three").
        strike_verbs = r"(?:struck|strik(?:e|es|ing)|chim(?:e|ed|es|ing)|toll(?:s|ed|ing))"
        subject = r"(?:clocks?|bells?|watch(?:es)?)\s+(?:were\s+|was\s+|had\s+)?"
        patterns.append((rf"\b{subject}{strike_verbs}\s+{hour_word_flex}\b", f"clock striking {hour_word}"))
        patterns.append((rf"\b{subject}{strike_verbs}\s+{hour_12}\b", f"clock striking {hour_12}"))
        if hour_24_word != hour_word:
            patterns.append((rf"\b{subject}{strike_verbs}\s+{hour_24_word_flex}\b", f"clock striking {hour_24_word}"))
            patterns.append((rf"\b{subject}{strike_verbs}\s+{hour}\b", f"clock striking {hour}"))

        # Approximation prefix ("nearly four", "about five") — only for :00 times.
        # Require trailing word boundary followed by punctuation / end of clause to
        # reduce false positives on phrases like "nearly four miles".
        approx = r"(?:about|around|nearly|almost|approximately|roughly|just\s+before|just\s+after)\s+"
        patterns.append((rf"\b{approx}{hour_word_flex}\b(?=\s*[,.;:!?\"'\)\]]|\s*$)", f"~{hour_word}"))

    # "half past X" (for :30) — allow hyphen in "half-past"
    if minute == 30:
        patterns.append((rf"\bhalf[-\s]+past[-\s]+{hour_word_flex}\b", f"half past {hour_word}"))
        patterns.append((rf"\bhalf[-\s]+past[-\s]+{hour_12}\b", f"half past {hour_12}"))
        # Also "half X" (British)
        patterns.append((rf"\bhalf[-\s]+{hour_word_flex}\b", f"half {hour_word}"))

    # "quarter past X" (for :15) — allow hyphen in "quarter-past"
    if minute == 15:
        patterns.append((rf"\bquarter[-\s]+past[-\s]+{hour_word_flex}\b", f"quarter past {hour_word}"))
        patterns.append((rf"\bquarter[-\s]+past[-\s]+{hour_12}\b", f"quarter past {hour_12}"))
        patterns.append((rf"\ba\s+quarter[-\s]+past[-\s]+{hour_word_flex}\b", f"a quarter past {hour_word}"))

    # "quarter to X" (for :45, references next hour)
    if minute == 45:
        patterns.append((rf"\bquarter[-\s]+to[-\s]+{next_hour_word_flex}\b", f"quarter to {next_hour_word}"))
        patterns.append((rf"\bquarter[-\s]+to[-\s]+{next_hour}\b", f"quarter to {next_hour}"))
        patterns.append((rf"\ba\s+quarter[-\s]+to[-\s]+{next_hour_word_flex}\b", f"a quarter to {next_hour_word}"))

    # "X minutes past Y" patterns (0 < minute < 30)
    if minute > 0 and minute < 30:
        min_word = DIGIT_TO_WORD.get(minute, str(minute))
        min_word_flex = _flex(min_word)
        # Word minute, word hour
        patterns.append(
            (
                rf"\b{min_word_flex}[-\s]+minutes?[-\s]+(?:past|after)[-\s]+{hour_word_flex}\b",
                f"{min_word} minutes past {hour_word}",
            )
        )
        # Digit minute, word hour
        patterns.append(
            (
                rf"\b{minute}[-\s]+minutes?[-\s]+(?:past|after)[-\s]+{hour_word_flex}\b",
                f"{minute} minutes past {hour_word}",
            )
        )
        # Word minute, digit hour
        patterns.append(
            (
                rf"\b{min_word_flex}[-\s]+minutes?[-\s]+(?:past|after)[-\s]+{hour_12}\b",
                f"{min_word} minutes past {hour_12}",
            )
        )
        # Digit minute, digit hour
        patterns.append(
            (
                rf"\b{minute}[-\s]+minutes?[-\s]+(?:past|after)[-\s]+{hour_12}\b",
                f"{minute} minutes past {hour_12}",
            )
        )

        # "X minutes past/after midnight" / "X minutes past noon"
        if hour == 0:
            patterns.append(
                (
                    rf"\b{min_word_flex}[-\s]+minutes?[-\s]+(?:past|after)[-\s]+midnight\b",
                    f"{min_word} minutes past midnight",
                )
            )
            patterns.append(
                (rf"\b{minute}[-\s]+minutes?[-\s]+(?:past|after)[-\s]+midnight\b", f"{minute} minutes past midnight")
            )
        if hour == 12:
            patterns.append(
                (
                    rf"\b{min_word_flex}[-\s]+minutes?[-\s]+(?:past|after)[-\s]+noon\b",
                    f"{min_word} minutes past noon",
                )
            )
            patterns.append(
                (rf"\b{minute}[-\s]+minutes?[-\s]+(?:past|after)[-\s]+noon\b", f"{minute} minutes past noon")
            )

    # "X minutes to Y" patterns (minute > 30)
    if minute > 30:
        minutes_to = 60 - minute
        min_to_word = DIGIT_TO_WORD.get(minutes_to, str(minutes_to))
        min_to_word_flex = _flex(min_to_word)
        # Word minute, word hour
        patterns.append(
            (
                rf"\b{min_to_word_flex}[-\s]+minutes?[-\s]+(?:to|before)[-\s]+{next_hour_word_flex}\b",
                f"{min_to_word} minutes to {next_hour_word}",
            )
        )
        # Digit forms
        patterns.append(
            (
                rf"\b{minutes_to}[-\s]+minutes?[-\s]+(?:to|before)[-\s]+{next_hour_word_flex}\b",
                f"{minutes_to} minutes to {next_hour_word}",
            )
        )
        patterns.append(
            (
                rf"\b{minutes_to}[-\s]+minutes?[-\s]+(?:to|before)[-\s]+{next_hour}\b",
                f"{minutes_to} minutes to {next_hour}",
            )
        )

        # "X minutes to midnight" / "X minutes to noon"
        if hour == 23:
            patterns.append(
                (
                    rf"\b{min_to_word_flex}[-\s]+minutes?[-\s]+(?:to|before)[-\s]+midnight\b",
                    f"{min_to_word} minutes to midnight",
                )
            )
            patterns.append(
                (
                    rf"\b{minutes_to}[-\s]+minutes?[-\s]+(?:to|before)[-\s]+midnight\b",
                    f"{minutes_to} minutes to midnight",
                )
            )
        if hour == 11:
            patterns.append(
                (
                    rf"\b{min_to_word_flex}[-\s]+minutes?[-\s]+(?:to|before)[-\s]+noon\b",
                    f"{min_to_word} minutes to noon",
                )
            )
            # "nine minutes of twelve o'clock noon" / "nine minutes of twelve o'clock"
            patterns.append(
                (
                    rf"\b{min_to_word_flex}[-\s]+minutes?[-\s]+of[-\s]+twelve[-\s]+o'?clock(?:[-\s]+noon)?\b",
                    f"{min_to_word} minutes of twelve o'clock",
                )
            )

    # Compound word forms: "{hour_word} {minute_word}" (e.g. "twelve-thirty-five",
    # "eight forty-three", "five twenty-three", "eleven fifty-seven"). Only emit for
    # minute > 0 because minute == 0 is already covered by the o'clock patterns.
    if minute > 0:
        min_word = DIGIT_TO_WORD.get(minute, str(minute))
        min_word_flex = _flex(min_word)
        patterns.append(
            (
                rf"\b{hour_word_flex}[-\s]+{min_word_flex}\b(?!\s*(?:minutes?|past|to|o'?clock))",
                f"{hour_word} {min_word}",
            )
        )
        # Compound with "and": "eleven o'clock and twenty-five minutes"
        patterns.append(
            (
                rf"\b{hour_word_flex}[-\s]+o'?clock\s+and\s+{min_word_flex}(?:\s+minutes?)?\b",
                f"{hour_word} o'clock and {min_word}",
            )
        )
        # 24-hour compound: "thirteen thirty-two" for 13:32
        if hour_24_word != hour_word:
            patterns.append(
                (
                    rf"\b{hour_24_word_flex}[-\s]+{min_word_flex}\b(?!\s*(?:minutes?|past|to|o'?clock))",
                    f"{hour_24_word} {min_word}",
                )
            )

    return patterns


def find_best_time_match(time_str: str, quote: str) -> tuple[str | None, dict]:
    """
    Find the best matching time phrase in the quote.
    Returns (matched_text, match_info) or (None, {}) if not found.

    Tries digital patterns first, then text patterns.
    Validates AM/PM alignment with target time.
    """
    hour, minute = int(time_str[:2]), int(time_str[3:5])

    # Try digital patterns first (more specific with AM/PM)
    digital_patterns = build_digital_patterns(hour, minute)

    for pattern, _desc, _expected_ampm in digital_patterns:
        regex = re.compile(pattern, re.IGNORECASE)
        match = regex.search(quote)

        if match:
            matched_text = match.group(0)

            # Extract AM/PM from match if present
            ampm_match = re.search(r"[AaPp]\.?[Mm]\.?", matched_text)
            found_ampm = ampm_match.group(0) if ampm_match else None

            # Validate AM/PM alignment
            if not validate_ampm(hour, found_ampm):
                continue  # Skip this match, AM/PM doesn't align

            match_info = {
                "type": "digital",
                "has_ampm": found_ampm is not None,
                "ampm_validated": found_ampm is not None,
            }

            return matched_text, match_info

    # Try text-based patterns
    text_patterns = build_text_patterns(hour, minute)

    for pattern, desc in text_patterns:
        regex = re.compile(pattern, re.IGNORECASE)
        match = regex.search(quote)

        if match:
            matched_text = match.group(0)
            match_info = {
                "type": "text",
                "pattern": desc,
            }
            return matched_text, match_info

    return None, {}


def improve_time_phrase(time_str: str, original_phrase: str, quote: str) -> tuple[str, list[str]]:
    """
    Try to find the best time phrase in the quote.
    Handles:
    - Digital times with various formats (7:30, 7.30, 07:30)
    - AM/PM in various formats with validation against 24-hour time
    - Text patterns (half past ten, quarter to twelve, etc.)

    Returns (improved_phrase, list of improvements made)
    """
    improvements = []

    # Try to find the best match in the quote
    found_time, match_info = find_best_time_match(time_str, quote)

    if found_time:
        # Check if this is different/better than the original
        if found_time.lower().strip() != original_phrase.lower().strip():
            match_type = match_info.get("type", "unknown")

            if match_type == "digital":
                improvements.append("digital")
                IMPROVEMENT_STATS["digital_match"] += 1
                if match_info.get("has_ampm"):
                    improvements.append("ampm")
                    IMPROVEMENT_STATS["ampm_added"] += 1
                if match_info.get("ampm_validated"):
                    IMPROVEMENT_STATS["ampm_validated"] += 1
            elif match_type == "text":
                improvements.append("text_pattern")
                IMPROVEMENT_STATS["text_pattern_match"] += 1

            if improvements:
                IMPROVEMENT_STATS["total_improved"] += 1

            return found_time, improvements

        # Same match found - still good
        return found_time, []

    # If no match found via patterns, check if original is in quote
    if original_phrase.lower() in quote.lower():
        # Original is fine, but try to extend with AM/PM
        idx = quote.lower().find(original_phrase.lower())
        actual_phrase = quote[idx : idx + len(original_phrase)]

        # Look for AM/PM after the phrase
        after = quote[idx + len(original_phrase) : idx + len(original_phrase) + 10]
        ampm_match = re.match(r"^([\s.]*[AaPp]\.?[Mm]\.?)", after)
        if ampm_match:
            hour = int(time_str[:2])
            found_ampm = ampm_match.group(1)
            # Validate AM/PM
            if validate_ampm(hour, found_ampm):
                extended = actual_phrase + found_ampm
                improvements.append("ampm_extended")
                IMPROVEMENT_STATS["ampm_added"] += 1
                IMPROVEMENT_STATS["total_improved"] += 1
                return extended, improvements

        return actual_phrase, []

    # Original not in quote - use found_time if we have it
    if found_time:
        return found_time, ["fallback"]

    return original_phrase, []


def has_time_context(time_phrase: str, quote: str) -> bool:
    """
    Check if a single word time phrase has time-related context in the quote.
    """
    phrase_lower = time_phrase.lower().strip()
    quote_lower = quote.lower()

    # Direct time contexts
    time_contexts = [
        f"{phrase_lower} o'clock",
        f"{phrase_lower} minutes",
        f"{phrase_lower} minute",
        f"{phrase_lower} past",
        f"{phrase_lower} to ",
        f"{phrase_lower} after",
        f"{phrase_lower} before",
        f"at {phrase_lower}",
        f"struck {phrase_lower}",
        f"strike {phrase_lower}",
        f"striking {phrase_lower}",
        f"past {phrase_lower}",
        f"to {phrase_lower}",
        f"half past {phrase_lower}",
        f"quarter past {phrase_lower}",
        f"quarter to {phrase_lower}",
        f"until {phrase_lower}",
        f"by {phrase_lower}",
    ]

    return any(ctx in quote_lower for ctx in time_contexts)


# Word-bounded regex for time-related vocabulary. Used by
# has_any_time_context() to auto-reject contributor padding where the quote
# has no time reference at all (Bridget Jones's Diary pattern). Using
# word boundaries instead of substring search avoids false matches like
# "Newsnight" containing "night" or "by mistake" matching " by ". Generic
# connectives ("by", "to", "until", "after", "before") are deliberately
# excluded because they have too much non-time usage in prose.
_TIME_CONTEXT_RE = re.compile(
    r"\b(?:"
    r"clocks?|watches?|watch|bells?|"
    r"struck|strikes?|striking|"
    r"chimed?|chimes?|chiming|"
    r"tolls?|tolled|tolling|"
    r"o'?clock|"
    r"noon|midnight|midday|noonday|"
    r"minutes?|hours?|"
    r"mornings?|afternoons?|evenings?|nights?|nighttime|daybreak|"
    r"dawn|dusk|twilight|sunrise|sunset|"
    r"strokes?|past"
    r")\b",
    re.IGNORECASE,
)
_AMPM_RE = re.compile(r"\b[ap]\.?m\.?\b", re.IGNORECASE)


def has_any_time_context(quote: str) -> bool:
    """
    Return True if the quote contains ANY time-related keyword.
    Used to distinguish real time-referential quotes from contributor padding
    where the quote text never actually mentions time.
    """
    return bool(_TIME_CONTEXT_RE.search(quote) or _AMPM_RE.search(quote))


def needs_review(time_str: str, time_phrase: str, quote: str, confident: str) -> tuple[bool, str]:
    """
    Determine if a quote needs review and why.
    Returns (needs_review: bool, reason: str)
    """
    reasons = []

    # Check if phrase is in quote
    phrase_in_quote = time_phrase.lower() in quote.lower()

    # Already marked as not confident
    if confident != "YES":
        # Check if it's a suspicious single word
        if time_phrase.lower().strip() in SUSPICIOUS_WORDS:
            if not has_time_context(time_phrase, quote):
                reasons.append(f"Single word '{time_phrase}' may not be time reference")

        # Check if extracted phrase appears in quote
        if not phrase_in_quote:
            reasons.append("Extracted phrase not found in quote")

        # If no specific issue found but still not confident
        if not reasons:
            reasons.append("Time phrase extraction uncertain")

    # Check for HTML tags that need cleaning
    if "<br>" in quote or "<" in quote:
        reasons.append("Contains HTML tags")

    # Check for very short quotes (might be truncated)
    if len(quote) < 50:
        reasons.append("Very short quote")

    # Check if time_phrase is just the digital time (fallback extraction)
    if re.match(r"^\d{1,2}:\d{2}(?::\d{2})?$", time_phrase) and confident != "YES":
        reasons.append("No time phrase found in quote text")
        # Stronger signal: the quote itself has no time-related vocabulary at all.
        # This catches the Bridget Jones's Diary / contributor-padding pattern where
        # someone posts sequential diary-style excerpts with made-up timestamps.
        if not has_any_time_context(quote):
            reasons.append("No time context in quote (contributor padding)")

    return (len(reasons) > 0, "; ".join(reasons) if reasons else "")


def main():
    print(f"Reading from {INPUT_FILE}...")

    rows = []
    with open(INPUT_FILE, encoding="utf-8") as f:
        content = f.read().replace("\r\n", "\n").replace("\r", "\n")
        reader = csv.reader(content.splitlines(), delimiter="|")
        next(reader)  # Skip header

        for row in reader:
            if len(row) >= 8:
                rows.append(row)

    print(f"Processing {len(rows)} quotes...")

    # Process and flag
    output_rows = []
    stats = {"total": 0, "needs_review": 0, "duplicates": 0, "new": 0}
    improved_phrases = []

    for row in rows:
        time_str, time_phrase, quote, title, author, asin, confident, duplicate = row[:8]

        stats["total"] += 1
        if duplicate == "YES":
            stats["duplicates"] += 1
        else:
            stats["new"] += 1

        # Try to improve the time phrase
        improved_phrase, improvements = improve_time_phrase(time_str, time_phrase, quote)
        if improvements:
            improved_phrases.append(
                {
                    "time": time_str,
                    "original": time_phrase,
                    "improved": improved_phrase,
                    "improvements": improvements,
                    "quote_snippet": quote[:80] + "..." if len(quote) > 80 else quote,
                }
            )

        # Use improved phrase for review check
        review_needed, review_reason = needs_review(time_str, improved_phrase, quote, confident)

        if review_needed:
            stats["needs_review"] += 1

        # Clean up quote (remove HTML tags for display, but keep original)
        clean_quote = quote.replace("<br>", " ").replace("  ", " ")

        output_rows.append(
            {
                "TIME": time_str,
                "TIME_PHRASE": improved_phrase,  # Use improved phrase
                "QUOTE": clean_quote,
                "TITLE": title,
                "AUTHOR": author,
                "ASIN": asin,
                "NEEDS_REVIEW": "YES" if review_needed else "NO",
                "REVIEW_REASON": review_reason,
                "IS_DUPLICATE": duplicate,
            }
        )

    # Sort by: needs_review (YES first), then by time
    output_rows.sort(key=lambda x: (x["NEEDS_REVIEW"] == "NO", x["TIME"]))

    # Write output
    print(f"Writing to {OUTPUT_FILE}...")

    with open(OUTPUT_FILE, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "TIME",
                "TIME_PHRASE",
                "QUOTE",
                "TITLE",
                "AUTHOR",
                "ASIN",
                "NEEDS_REVIEW",
                "REVIEW_REASON",
                "IS_DUPLICATE",
            ],
            delimiter="|",
        )
        writer.writeheader()
        writer.writerows(output_rows)

    # Print summary
    print("\n" + "=" * 60)
    print("REVIEW CSV PREPARED")
    print("=" * 60)
    print(f"Total quotes:              {stats['total']}")
    print(f"New quotes (not dupes):    {stats['new']}")
    print(f"Duplicates:                {stats['duplicates']}")
    print(f"Flagged for review:        {stats['needs_review']}")
    print("=" * 60)

    # Print improvement stats
    print("\nTIME PHRASE IMPROVEMENTS:")
    print(f"  Total phrases improved:                 {IMPROVEMENT_STATS['total_improved']}")
    print(f"  Digital format matches:                 {IMPROVEMENT_STATS['digital_match']}")
    print(f"  Text pattern matches:                   {IMPROVEMENT_STATS['text_pattern_match']}")
    print(f"  AM/PM added:                            {IMPROVEMENT_STATS['ampm_added']}")
    print(f"  AM/PM validated (correct alignment):    {IMPROVEMENT_STATS['ampm_validated']}")

    if improved_phrases:
        print(f"\nSample improvements ({min(10, len(improved_phrases))} of {len(improved_phrases)}):")
        for item in improved_phrases[:10]:
            imp_types = ", ".join(item["improvements"])
            print(f"  [{item['time']}] '{item['original']}' -> '{item['improved']}' ({imp_types})")

    print("=" * 60)
    print(f"\nOutput: {OUTPUT_FILE}")
    print("\nColumns:")
    print("  TIME         - The minute this quote is for (HH:MM)")
    print("  TIME_PHRASE  - Extracted time reference (EDIT THIS if wrong)")
    print("  QUOTE        - The quote text")
    print("  TITLE        - Book title")
    print("  AUTHOR       - Author name")
    print("  ASIN         - Amazon ID (for reference)")
    print("  NEEDS_REVIEW - YES if flagged for review, NO if looks OK")
    print("  REVIEW_REASON- Why it was flagged")
    print("  IS_DUPLICATE - YES if already in your database")
    print("\nTips:")
    print("  - Filter NEEDS_REVIEW=YES to see quotes needing attention")
    print("  - Filter IS_DUPLICATE=NO to see only new quotes")
    print("  - Edit TIME_PHRASE column to fix incorrect extractions")
    print("  - Delete rows you don't want to import")


if __name__ == "__main__":
    main()
