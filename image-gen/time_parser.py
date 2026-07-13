#!/usr/bin/env python3
"""
Comprehensive time parser for extracting and validating time phrases from quotes.

This module can:
1. Parse a time phrase string and convert it to HH:MM format
2. Find time phrases within quote text
3. Validate that a time phrase matches an expected time

Supports formats:
- Digital: 7:30, 7.30, 07:30, 0730, 0730h, 7:30 AM, 7.30pm, etc.
- Text: seven thirty, half past seven, quarter to eight, etc.
- Approximate: about seven, around 7:30, almost eight, nearly noon, etc.
- Clock references: struck seven, clock struck seven, the bells chimed eight
"""

import re

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
}

# Also handle "and" forms like "five and twenty" = 25
NUMBER_WORDS_AND = {
    "one and twenty": 21,
    "two and twenty": 22,
    "three and twenty": 23,
    "four and twenty": 24,
    "five and twenty": 25,
    "six and twenty": 26,
    "seven and twenty": 27,
    "eight and twenty": 28,
    "nine and twenty": 29,
}

# Reverse mapping
DIGIT_TO_WORD = {v: k for k, v in NUMBER_WORDS.items() if isinstance(v, int)}

# Hour words (for "X o'clock" patterns)
HOUR_WORDS = {
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
}

# Special time words
SPECIAL_TIMES = {
    "midnight": (0, 0),
    "mid-night": (0, 0),
    "noon": (12, 0),
    "midday": (12, 0),
    "mid-day": (12, 0),
    "noonday": (12, 0),
}

# Common prefix words to strip
APPROX_PREFIXES = (
    # Approximations
    "about",
    "around",
    "almost",
    "nearly",
    "exactly",
    "precisely",
    "roughly",
    # Simple prepositions
    "just",
    "at",
    "by",
    "near",
    "near to",
    "towards",
    "before",
    "after",
    "past",
    # Compound approximations
    "shortly before",
    "shortly after",
    "just before",
    "just after",
    "just past",
    "just about",
    "a little after",
    "a little before",
    "a little past",
    "a few minutes after",
    "a few minutes before",
    "only",
    "close upon",
    "close to",
    "near on",
    "almost at",
    "nearly at",
    "morning at",  # "morning at five o'clock"
)


def word_to_number(word: str) -> int | None:
    """Convert a number word to integer."""
    if not word:
        return None
    word = word.lower().strip()

    # Direct lookup
    if word in NUMBER_WORDS:
        return NUMBER_WORDS[word]

    # Handle "and" forms
    if word in NUMBER_WORDS_AND:
        return NUMBER_WORDS_AND[word]

    # Handle hyphenated forms written with space
    word_hyphen = word.replace(" ", "-")
    if word_hyphen in NUMBER_WORDS:
        return NUMBER_WORDS[word_hyphen]

    # Try to parse as digit
    try:
        return int(word)
    except ValueError:
        return None


def normalize_phrase(phrase: str) -> str:
    """Normalize a time phrase for parsing."""
    # Lowercase
    s = phrase.lower().strip()
    # Normalize quotes and special characters (including curly apostrophes)
    s = s.replace("\u2018", "'").replace("\u2019", "'")  # Left/right single quotes
    s = s.replace("\u201c", '"').replace("\u201d", '"')  # Left/right double quotes
    s = s.replace("\u02bc", "'")  # Modifier letter apostrophe
    # Normalize dashes
    s = s.replace("–", "-").replace("—", "-")
    # Remove italic/styled Unicode characters (map to ASCII equivalents)
    # Mathematical italic letters (commonly used for styling)
    styled_map = str.maketrans(
        {
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
    )
    s = s.translate(styled_map)
    # Normalize whitespace
    s = " ".join(s.split())
    return s


def strip_approximations(s: str) -> str:
    """Remove common approximation prefixes from a normalized string."""
    # Sort by length descending to match longer phrases first
    for prefix in sorted(APPROX_PREFIXES, key=len, reverse=True):
        if s.startswith(prefix + " "):
            s = s[len(prefix) :].strip()
            break
    # Also strip "it's" and similar
    s = re.sub(r"^(it's|it is|its|'tis|tis)\s+", "", s)
    return s


def apply_ampm(hour: int, ampm: str | None) -> int:
    """Apply AM/PM modifier to hour."""
    if not ampm:
        return hour
    ampm = ampm.lower().replace(".", "").replace(" ", "")
    if ampm in ("pm", "p") and hour < 12:
        return hour + 12
    elif ampm in ("am", "a") and hour == 12:
        return 0
    return hour


def parse_digital_time(phrase: str) -> tuple[int, int, str | None] | None:
    """
    Parse digital time formats.
    Returns (hour_24, minute, ampm) or None if not matched.
    """
    s = normalize_phrase(phrase)
    s = strip_approximations(s)

    # Pattern: H AM/PM or HH AM/PM (single hour with AM/PM)
    match = re.match(r"^(\d{1,2})\s*(a\.?m\.?|p\.?m\.?)\.?$", s, re.IGNORECASE)
    if match:
        hour = int(match.group(1))
        ampm = match.group(2)
        if 1 <= hour <= 12:
            hour = apply_ampm(hour, ampm)
            return (hour, 0, ampm)

    # Pattern: HH:MM or HH.MM or HH MM with optional AM/PM and timezone/suffix
    match = re.match(
        r"^(\d{1,2})[\s:.](\d{2})\s*"
        r"(a\.?m\.?|p\.?m\.?)?\s*"
        r"\.?\s*(gmt|cet|est|pst|utc|hrs?|hours?)?\.?",
        s,
        re.IGNORECASE,
    )
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        ampm = match.group(3)

        hour = apply_ampm(hour, ampm)

        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return (hour, minute, ampm)

    # Pattern: HHMM or HHMMh (military time)
    match = re.match(r"^(\d{4})\s*h?\.?\s*(hours?|hrs?)?$", s, re.IGNORECASE)
    if match:
        time_str = match.group(1)
        hour = int(time_str[:2])
        minute = int(time_str[2:])
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return (hour, minute, None)

    # Pattern: H:MM:SS (with seconds, ignore seconds)
    match = re.match(r"^(\d{1,2}):(\d{2}):\d{2}", s)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return (hour, minute, None)

    return None


def parse_oclock_time(phrase: str) -> tuple[int, int, str | None] | None:
    """
    Parse "X o'clock" patterns including variations like:
    - seven o'clock, 7 o'clock
    - seven o'clock a.m., four o'clock p.m.
    - seven o'clock in the morning
    - seven-o'clock, five-o'clock
    - seven clock, four clock (archaic)
    """
    s = normalize_phrase(phrase)
    s = strip_approximations(s)

    # Pattern: X o'clock (various spellings) with optional period of day or AM/PM
    # Handles: o'clock, o clock, oclock, -o'clock, clock
    match = re.match(
        r"^(\w+)[\s-]*o'?\s*clock\s*"
        r"(in the (morning|afternoon|evening|night)|"
        r"at (night)|"
        r"on the (morning|afternoon|evening)|"
        r"(a\.?m\.?|p\.?m\.?))?",
        s,
        re.IGNORECASE,
    )
    if match:
        hour_word = match.group(1)
        period_in = match.group(3)
        period_at = match.group(4)
        period_on = match.group(5)
        ampm_direct = match.group(6)

        hour = word_to_number(hour_word)
        if hour is None or hour < 1 or hour > 12:
            return None

        # Determine AM/PM from period
        ampm = None
        period = period_in or period_at or period_on
        if period:
            period = period.lower()
            if period == "morning":
                ampm = "am"
                if hour == 12:
                    hour = 0
            elif period in ("afternoon", "evening", "night"):
                ampm = "pm"
                if hour < 12:
                    hour += 12
        elif ampm_direct:
            hour = apply_ampm(hour, ampm_direct)
            ampm = ampm_direct

        return (hour, 0, ampm)

    # Pattern: X clock (without "o'")
    match = re.match(r"^(\w+)\s+clock$", s, re.IGNORECASE)
    if match:
        hour_word = match.group(1)
        hour = word_to_number(hour_word)
        if hour is not None and 1 <= hour <= 12:
            return (hour, 0, None)

    return None


def parse_half_past(phrase: str) -> tuple[int, int, str | None] | None:
    """
    Parse "half past X" patterns including noon/midnight.
    """
    s = normalize_phrase(phrase)
    s = strip_approximations(s)

    # Pattern: half past X (including noon/midnight)
    match = re.match(r"^half[\s-]+past\s+(\w+)", s, re.IGNORECASE)
    if match:
        hour_word = match.group(1).lower()
        if hour_word == "midnight":
            return (0, 30, None)
        elif hour_word in ("noon", "midday"):
            return (12, 30, None)
        hour = word_to_number(hour_word)
        if hour is not None and 1 <= hour <= 12:
            return (hour, 30, None)

    # Pattern: half X (British style)
    match = re.match(r"^half\s+(\w+)$", s, re.IGNORECASE)
    if match:
        hour_word = match.group(1).lower()
        if hour_word == "midnight":
            return (0, 30, None)
        elif hour_word in ("noon", "midday"):
            return (12, 30, None)
        hour = word_to_number(hour_word)
        if hour is not None and 1 <= hour <= 12:
            return (hour, 30, None)

    return None


def parse_quarter_time(phrase: str) -> tuple[int, int, str | None] | None:
    """
    Parse "quarter past/to X" patterns including midnight/noon.
    Handles: quarter past, quarter-past, quarter to, quarter after, etc.
    """
    s = normalize_phrase(phrase)
    s = strip_approximations(s)
    # Remove article
    s = re.sub(r"^a\s+", "", s)

    # Pattern: quarter past X (including midnight/noon)
    # Handles: quarter past, quarter-past, quarter after
    match = re.match(r"^quarter[\s-]+(?:past|after)\s+(\w+)", s, re.IGNORECASE)
    if match:
        hour_word = match.group(1).lower()
        if hour_word == "midnight":
            return (0, 15, None)
        elif hour_word in ("noon", "midday"):
            return (12, 15, None)
        hour = word_to_number(hour_word)
        if hour is not None and 1 <= hour <= 12:
            return (hour, 15, None)

    # Pattern: quarter to/before/of/till X (including midnight/noon)
    match = re.match(r"^quarter[\s-]+(?:to|before|of|till)\s+(\w+)", s, re.IGNORECASE)
    if match:
        hour_word = match.group(1).lower()
        if hour_word == "midnight":
            return (23, 45, None)  # Quarter to midnight = 23:45
        elif hour_word in ("noon", "midday"):
            return (11, 45, None)  # Quarter to noon = 11:45
        hour = word_to_number(hour_word)
        if hour is not None and 1 <= hour <= 12:
            prev_hour = 12 if hour == 1 else hour - 1
            return (prev_hour, 45, None)

    return None


def parse_minutes_past_to(phrase: str) -> tuple[int, int, str | None] | None:
    """
    Parse "X minutes past/to Y" patterns.
    Handles hyphenated numbers, "and" forms, midnight/noon, and singular "minute".
    """
    s = normalize_phrase(phrase)
    s = strip_approximations(s)

    # Handle "a minute" = 1 minute
    s = re.sub(r"^a\s+minute\b", "one minute", s)
    s = re.sub(r"^one\s+minute\b", "one minutes", s)  # Normalize to plural for pattern

    # Pattern: X (minutes) past/after Y (handles hyphenated and "and" forms)
    match = re.match(r"^([\w-]+(?:\s+and\s+\w+)?)\s*(?:minutes?)?\s+(?:past|after)\s+(\w+)", s, re.IGNORECASE)
    if match:
        min_word = match.group(1)
        hour_word = match.group(2).lower()

        minute = word_to_number(min_word)
        if minute is None:
            return None

        # Handle midnight/noon
        if hour_word == "midnight":
            hour = 0
        elif hour_word in ("noon", "midday"):
            hour = 12
        else:
            hour = word_to_number(hour_word)

        if hour is not None and 0 <= minute <= 59:
            # For regular hours, validate 1-12 range
            if hour_word not in ("midnight", "noon", "midday") and (hour < 1 or hour > 12):
                return None
            return (hour, minute, None)

    # Pattern: X (minutes) to/before/of/till Y
    match = re.match(
        r"^([\w-]+(?:\s+and\s+\w+)?)\s*(?:minutes?)?\s+(?:to|before|of|till|until)\s+(\w+)", s, re.IGNORECASE
    )
    if match:
        min_word = match.group(1)
        hour_word = match.group(2).lower()

        minutes_to = word_to_number(min_word)
        if minutes_to is None or minutes_to <= 0 or minutes_to > 59:
            return None

        # Handle midnight/noon
        if hour_word == "midnight":
            hour = 0
        elif hour_word in ("noon", "midday"):
            hour = 12
        else:
            hour = word_to_number(hour_word)

        if hour is not None:
            # For regular hours, validate 1-12 range
            if hour_word not in ("midnight", "noon", "midday") and (hour < 1 or hour > 12):
                return None
            # X minutes to Y means (Y-1):(60-X)
            prev_hour = (hour - 1) % 24
            minute = 60 - minutes_to
            return (prev_hour, minute, None)

    # Pattern: X to Y (short form with digits)
    match = re.match(r"^(\d+)\s+to\s+(\w+)$", s, re.IGNORECASE)
    if match:
        minutes_to = int(match.group(1))
        hour_word = match.group(2).lower()

        if hour_word == "midnight":
            hour = 0
        elif hour_word in ("noon", "midday"):
            hour = 12
        else:
            hour = word_to_number(hour_word)

        if hour is not None and 0 < minutes_to <= 30:
            prev_hour = (hour - 1) % 24
            minute = 60 - minutes_to
            return (prev_hour, minute, None)

    return None


def parse_compound_time(phrase: str) -> tuple[int, int, str | None] | None:
    """
    Parse compound times like "seven-thirty", "six thirty", "seven forty-five".
    Also handles:
    - "X AM/PM" patterns
    - "ten-forty in the morning" patterns
    - "X-past Y" patterns like "ten-past three"
    """
    s = normalize_phrase(phrase)
    s = strip_approximations(s)

    # Pattern: X-past Y (e.g., "ten-past three", "five-past eight")
    match = re.match(r"^(\w+)[\s-]+past\s+(\w+)$", s, re.IGNORECASE)
    if match:
        min_word = match.group(1)
        hour_word = match.group(2)

        minute = word_to_number(min_word)
        hour = word_to_number(hour_word)

        if minute is not None and hour is not None and 1 <= hour <= 12 and 0 <= minute <= 59:
            return (hour, minute, None)

    # Pattern: word hour + word minute + period of day
    match = re.match(r"^(\w+)[\s-]+([\w-]+)\s+(?:in the\s+|at\s+)(morning|afternoon|evening|night)$", s, re.IGNORECASE)
    if match:
        hour_word = match.group(1)
        min_word = match.group(2)
        period = match.group(3).lower()

        hour = word_to_number(hour_word)
        minute = word_to_number(min_word)

        if hour is not None and minute is not None and 1 <= hour <= 12 and 0 <= minute <= 59:
            if period == "morning":
                if hour == 12:
                    hour = 0
            elif period in ("afternoon", "evening", "night"):
                if hour < 12:
                    hour += 12
            return (hour, minute, period)

    # Pattern: word hour + word minute (with optional AM/PM)
    match = re.match(r"^(\w+)[\s-]+([\w-]+)\s*(a\.?m\.?|p\.?m\.?)?$", s, re.IGNORECASE)
    if match:
        hour_word = match.group(1)
        min_word = match.group(2)
        ampm = match.group(3)

        hour = word_to_number(hour_word)
        minute = word_to_number(min_word)

        if hour is not None and minute is not None and 1 <= hour <= 12 and 0 <= minute <= 59:
            hour = apply_ampm(hour, ampm)
            return (hour, minute, ampm)

    return None


def parse_hours_relative(phrase: str) -> tuple[int, int, str | None] | None:
    """
    Parse "X hours past/before/after/ere noon/midnight" patterns.
    E.g., "two hours ere noon" = 10:00, "two hours past midday" = 14:00
    """
    s = normalize_phrase(phrase)
    s = strip_approximations(s)

    # Pattern: X hours past/after noon/midnight
    match = re.match(r"^(\w+)\s+hours?\s+(?:past|after)\s+(noon|midday|midnight)", s, re.IGNORECASE)
    if match:
        hours_word = match.group(1)
        base = match.group(2).lower()

        hours = word_to_number(hours_word)
        if hours is not None and 1 <= hours <= 12:
            base_hour = 12 if base in ("noon", "midday") else 0
            return ((base_hour + hours) % 24, 0, None)

    # Pattern: X hours before/ere/to noon/midnight
    match = re.match(r"^(\w+)\s+hours?\s+(?:before|ere|to)\s+(noon|midday|midnight)", s, re.IGNORECASE)
    if match:
        hours_word = match.group(1)
        base = match.group(2).lower()

        hours = word_to_number(hours_word)
        if hours is not None and 1 <= hours <= 12:
            base_hour = 12 if base in ("noon", "midday") else 0
            return ((base_hour - hours) % 24, 0, None)

    return None


def parse_special_time(phrase: str) -> tuple[int, int, str | None] | None:
    """
    Parse special times: midnight, noon, midday.
    Also handles patterns like "five past midnight".
    """
    s = normalize_phrase(phrase)
    s = strip_approximations(s)

    # Check for special times as whole words (not substrings like "afternoon")
    for word, (hour, minute) in SPECIAL_TIMES.items():
        if re.search(rf"\b{re.escape(word)}\b", s):
            return (hour, minute, None)

    return None


def parse_period_time(phrase: str) -> tuple[int, int, str | None] | None:
    """
    Parse times with period of day: "seven in the morning", "three in the afternoon",
    "eight at night".
    """
    s = normalize_phrase(phrase)
    s = strip_approximations(s)

    # Pattern: X in the morning/afternoon/evening OR X at night
    match = re.match(r"^(\w+(?:-\w+)?)\s+(?:in the\s+|at\s+)(morning|afternoon|evening|night)", s, re.IGNORECASE)
    if match:
        hour_word = match.group(1)
        period = match.group(2).lower()

        hour = word_to_number(hour_word)
        if hour is not None and 1 <= hour <= 12:
            if period == "morning":
                if hour == 12:
                    hour = 0
            elif period in ("afternoon", "evening", "night"):
                if hour < 12:
                    hour += 12
            return (hour, 0, period)

    return None


def parse_hour_ampm(phrase: str) -> tuple[int, int, str | None] | None:
    """
    Parse simple hour with AM/PM: "six AM", "three A.M.", "two p.m."
    """
    s = normalize_phrase(phrase)
    s = strip_approximations(s)

    # Pattern: word hour + AM/PM
    match = re.match(r"^(\w+)\s+(a\.?m\.?|p\.?m\.?)\.?$", s, re.IGNORECASE)
    if match:
        hour_word = match.group(1)
        ampm = match.group(2)

        hour = word_to_number(hour_word)
        if hour is not None and 1 <= hour <= 12:
            hour = apply_ampm(hour, ampm)
            return (hour, 0, ampm)

    return None


def parse_struck_time(phrase: str) -> tuple[int, int, str | None] | None:
    """
    Parse clock-striking patterns: "struck seven", "clock struck ten".
    """
    s = normalize_phrase(phrase)

    # Pattern: struck X, clock struck X
    match = re.search(r"struck\s+(\w+)", s, re.IGNORECASE)
    if match:
        hour_word = match.group(1)
        if hour_word in ("noon", "midday"):
            return (12, 0, None)
        if hour_word == "midnight":
            return (0, 0, None)
        hour = word_to_number(hour_word)
        if hour is not None and 1 <= hour <= 12:
            return (hour, 0, None)

    return None


def parse_oh_time(phrase: str) -> tuple[int, int, str | None] | None:
    """
    Parse X-oh-Y patterns like "five-oh-eight", "three-oh-five", "eight oh two".
    The "oh" represents a zero in the minutes.
    """
    s = normalize_phrase(phrase)
    s = strip_approximations(s)

    # Pattern: X oh Y or X-oh-Y with optional AM/PM
    match = re.match(r"^(\w+)[\s-]+oh[\s-]+(\w+)\s*(a\.?m\.?|p\.?m\.?|eh\s*em)?\.?$", s, re.IGNORECASE)
    if match:
        hour_word = match.group(1)
        min_word = match.group(2)
        ampm = match.group(3)

        hour = word_to_number(hour_word)
        minute = word_to_number(min_word)

        if hour is not None and minute is not None and 1 <= hour <= 12 and 0 <= minute <= 9:
            hour = apply_ampm(hour, ampm)
            return (hour, minute, ampm)

    return None


def parse_sharp_time(phrase: str) -> tuple[int, int, str | None] | None:
    """
    Parse "X sharp" patterns like "seven sharp", "eight sharp".
    """
    s = normalize_phrase(phrase)
    s = strip_approximations(s)

    # Pattern: X sharp
    match = re.match(r"^(\w+)\s+sharp$", s, re.IGNORECASE)
    if match:
        hour_word = match.group(1)
        hour = word_to_number(hour_word)
        if hour is not None and 1 <= hour <= 12:
            return (hour, 0, None)

    return None


def parse_simple_hour(phrase: str) -> tuple[int, int, str | None] | None:
    """
    Parse simple hour references: "eight", "almost eight", "about ten".
    This is a fallback parser for simple hour words without any qualifier.
    """
    s = normalize_phrase(phrase)
    s = strip_approximations(s)

    # Pattern: just a number word (hour only)
    if s in HOUR_WORDS:
        hour = HOUR_WORDS[s]
        return (hour, 0, None)

    # Pattern: digit hour (1-12)
    match = re.match(r"^(\d{1,2})$", s)
    if match:
        hour = int(match.group(1))
        if 1 <= hour <= 12:
            return (hour, 0, None)

    return None


def parse_time_phrase(phrase: str) -> tuple[int, int] | None:
    """
    Parse a time phrase and return (hour_24, minute).
    Returns None if the phrase cannot be parsed.

    This is the main entry point for parsing time phrases.
    Tries multiple parsing strategies in order of specificity.
    """
    if not phrase:
        return None

    # Try each parser in order of specificity
    # Note: more specific patterns (quarter/minutes past/to) must come before
    # special times (midnight/noon) to correctly parse "ten past midnight" etc.
    parsers = [
        parse_digital_time,  # 7:30, 07:30, 0730h, 5 a.m.
        parse_half_past,  # half past seven
        parse_quarter_time,  # quarter past/to (including midnight/noon)
        parse_minutes_past_to,  # X minutes past/to Y (including midnight/noon)
        parse_hours_relative,  # two hours past noon, three hours ere midnight
        parse_oclock_time,  # seven o'clock, four clock
        parse_oh_time,  # five-oh-eight, three-oh-five
        parse_compound_time,  # seven-thirty, ten-forty in the morning
        parse_period_time,  # seven in the morning
        parse_hour_ampm,  # six AM, three P.M.
        parse_struck_time,  # struck seven
        parse_sharp_time,  # seven sharp
        parse_special_time,  # midnight, noon (must be after patterns that use them)
        parse_simple_hour,  # eight, almost ten
    ]

    for parser in parsers:
        result = parser(phrase)
        if result:
            hour, minute, _ = result
            # Normalize hour to 0-23 range
            hour = hour % 24
            return (hour, minute)

    return None


def time_to_str(hour: int, minute: int) -> str:
    """Convert hour and minute to HH:MM string."""
    return f"{hour:02d}:{minute:02d}"


def validate_time_phrase(phrase: str, expected_time: str) -> tuple[bool, str | None]:
    """
    Validate that a time phrase matches the expected time.

    Args:
        phrase: The time phrase to parse
        expected_time: Expected time in HH:MM format

    Returns:
        (is_valid, parsed_time_str) where parsed_time_str is the parsed time or None
    """
    result = parse_time_phrase(phrase)
    if result is None:
        return (False, None)

    hour, minute = result
    parsed_time = time_to_str(hour, minute)

    # Direct match
    if parsed_time == expected_time:
        return (True, parsed_time)

    # Check 12-hour ambiguity (when no AM/PM specified)
    expected_hour = int(expected_time[:2])
    expected_minute = int(expected_time[3:])

    if minute == expected_minute:
        if hour == expected_hour:
            return (True, parsed_time)
        # 12-hour ambiguity
        if abs(hour - expected_hour) == 12:
            return (True, expected_time)
        if hour == 0 and expected_hour == 12:
            return (True, expected_time)
        if hour == 12 and expected_hour == 0:
            return (True, expected_time)

    return (False, parsed_time)


def find_time_in_quote(quote: str, expected_time: str) -> str | None:
    """
    Find a time phrase in the quote that matches the expected time.

    Args:
        quote: The quote text to search
        expected_time: Expected time in HH:MM format

    Returns:
        The matched time phrase substring, or None if not found
    """
    from prepare_review_csv import find_best_time_match

    result, _ = find_best_time_match(expected_time, quote)
    return result


if __name__ == "__main__":
    # Run comprehensive tests
    test_cases = [
        # Digital times
        ("7:30", (7, 30)),
        ("07:30", (7, 30)),
        ("7.30", (7, 30)),
        ("7:30 AM", (7, 30)),
        ("7:30 pm", (19, 30)),
        ("7.30pm", (19, 30)),
        ("7:30 a.m.", (7, 30)),
        ("0730", (7, 30)),
        ("0730h", (7, 30)),
        ("07:30 hours", (7, 30)),
        ("19:30", (19, 30)),
        ("00:00", (0, 0)),
        ("12:00", (12, 0)),
        ("5 a.m.", (5, 0)),
        ("2 p.m.", (14, 0)),
        ("10.53 hrs", (10, 53)),
        # O'clock times
        ("seven o'clock", (7, 0)),
        ("7 o'clock", (7, 0)),
        ("12 o'clock", (12, 0)),
        ("twelve o'clock", (12, 0)),
        ("seven o'clock in the morning", (7, 0)),
        ("seven o'clock in the evening", (19, 0)),
        ("five o'clock in the morning", (5, 0)),
        ("eight o'clock at night", (20, 0)),
        # Half past
        ("half past seven", (7, 30)),
        ("half-past seven", (7, 30)),
        ("half past 7", (7, 30)),
        # Quarter times
        ("quarter past seven", (7, 15)),
        ("a quarter past seven", (7, 15)),
        ("quarter to eight", (7, 45)),
        ("a quarter to eight", (7, 45)),
        ("quarter to midnight", (23, 45)),
        ("quarter past noon", (12, 15)),
        ("quarter to noon", (11, 45)),
        # Minutes past/to
        ("ten minutes past seven", (7, 10)),
        ("ten past seven", (7, 10)),
        ("ten minutes to eight", (7, 50)),
        ("twenty-five to eight", (7, 35)),
        ("25 to eight", (7, 35)),
        ("twenty-six minutes past eight", (8, 26)),
        ("five and twenty to nine", (8, 35)),
        ("ten past noon", (12, 10)),
        ("twenty past midnight", (0, 20)),
        ("ten to midnight", (23, 50)),
        # Compound times
        ("seven thirty", (7, 30)),
        ("seven-thirty", (7, 30)),
        ("six forty-five", (6, 45)),
        # Special times
        ("midnight", (0, 0)),
        ("noon", (12, 0)),
        ("midday", (12, 0)),
        ("noonday", (12, 0)),
        # Period times
        ("seven in the morning", (7, 0)),
        ("three in the afternoon", (15, 0)),
        ("four in the morning", (4, 0)),
        ("eight at night", (20, 0)),
        # Hour with AM/PM
        ("six AM", (6, 0)),
        ("three A.M.", (3, 0)),
        ("six a.m.", (6, 0)),
        ("two p.m.", (14, 0)),
        ("three PM", (15, 0)),
        # Struck times
        ("struck seven", (7, 0)),
        ("clock struck ten", (10, 0)),
        ("struck noon", (12, 0)),
        # With approximations
        ("about seven o'clock", (7, 0)),
        ("around 7:30", (7, 30)),
        ("almost eight", (8, 0)),
        ("nearly nine o'clock", (9, 0)),
        ("just after 4am", (4, 0)),
    ]

    print("Running time parser tests...\n")
    passed = 0
    failed = 0

    for phrase, expected in test_cases:
        result = parse_time_phrase(phrase)
        if result == expected:
            passed += 1
            print(f"✓ '{phrase}' -> {result}")
        else:
            failed += 1
            print(f"✗ '{phrase}' -> {result} (expected {expected})")

    print(f"\n{passed} passed, {failed} failed")
