"""Tests for time_parser module — the core time-phrase parsing logic."""

import pytest
from time_parser import (
    apply_ampm,
    normalize_phrase,
    parse_time_phrase,
    strip_approximations,
    time_to_str,
    validate_time_phrase,
    word_to_number,
)

# ── word_to_number ──────────────────────────────────────────────────


class TestWordToNumber:
    def test_basic_words(self):
        assert word_to_number("seven") == 7
        assert word_to_number("twelve") == 12
        assert word_to_number("zero") == 0

    def test_hyphenated(self):
        assert word_to_number("twenty-five") == 25
        assert word_to_number("forty-two") == 42

    def test_and_forms(self):
        assert word_to_number("five and twenty") == 25
        assert word_to_number("nine and twenty") == 29

    def test_digit_string(self):
        assert word_to_number("42") == 42
        assert word_to_number("7") == 7

    def test_unknown(self):
        assert word_to_number("banana") is None
        assert word_to_number("") is None

    def test_case_insensitive(self):
        assert word_to_number("Seven") == 7
        assert word_to_number("TWELVE") == 12

    def test_space_separated_hyphenated(self):
        assert word_to_number("twenty five") == 25


# ── normalize_phrase ────────────────────────────────────────────────


class TestNormalizePhrase:
    def test_lowercase_and_strip(self):
        assert normalize_phrase("  Seven O'Clock  ") == "seven o'clock"

    def test_collapse_whitespace(self):
        assert normalize_phrase("half   past   seven") == "half past seven"

    def test_curly_apostrophe(self):
        assert normalize_phrase("seven o\u2019clock") == "seven o'clock"

    def test_em_dash(self):
        assert normalize_phrase("seven\u2014thirty") == "seven-thirty"

    def test_en_dash(self):
        assert normalize_phrase("seven\u2013thirty") == "seven-thirty"

    def test_curly_double_quotes(self):
        assert normalize_phrase("\u201chello\u201d") == '"hello"'

    def test_unicode_styled_letters(self):
        # Mathematical italic letters: 𝘢 = a, 𝘭 = l
        assert normalize_phrase("\U0001d622\U0001d62d") == "al"


# ── strip_approximations ───────────────────────────────────────────


class TestStripApproximations:
    def test_about(self):
        assert strip_approximations("about seven o'clock") == "seven o'clock"

    def test_nearly(self):
        assert strip_approximations("nearly nine") == "nine"

    def test_compound_prefix(self):
        assert strip_approximations("shortly after seven") == "seven"

    def test_its(self):
        assert strip_approximations("it's seven o'clock") == "seven o'clock"

    def test_no_prefix(self):
        assert strip_approximations("seven o'clock") == "seven o'clock"

    def test_just_past(self):
        assert strip_approximations("just past four") == "four"


# ── apply_ampm ──────────────────────────────────────────────────────


class TestApplyAmpm:
    def test_pm_converts(self):
        assert apply_ampm(3, "pm") == 15

    def test_am_noon_to_midnight(self):
        assert apply_ampm(12, "am") == 0

    def test_pm_noon_stays(self):
        assert apply_ampm(12, "pm") == 12

    def test_none_no_change(self):
        assert apply_ampm(7, None) == 7

    def test_am_normal(self):
        assert apply_ampm(7, "am") == 7

    def test_pm_already_afternoon(self):
        assert apply_ampm(12, "p.m.") == 12

    def test_dotted_am(self):
        assert apply_ampm(6, "a.m.") == 6


# ── time_to_str ─────────────────────────────────────────────────────


class TestTimeToStr:
    def test_normal(self):
        assert time_to_str(7, 30) == "07:30"

    def test_midnight(self):
        assert time_to_str(0, 0) == "00:00"

    def test_noon(self):
        assert time_to_str(12, 0) == "12:00"

    def test_single_digit_minute(self):
        assert time_to_str(9, 5) == "09:05"

    def test_late_night(self):
        assert time_to_str(23, 59) == "23:59"


# ── parse_time_phrase (parametrized with all 71 ad-hoc cases) ───────


class TestParseTimePhrase:
    @pytest.mark.parametrize(
        "phrase, expected",
        [
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
        ],
        ids=lambda x: str(x) if isinstance(x, tuple) else x,
    )
    def test_digital(self, phrase, expected):
        assert parse_time_phrase(phrase) == expected

    @pytest.mark.parametrize(
        "phrase, expected",
        [
            ("seven o'clock", (7, 0)),
            ("7 o'clock", (7, 0)),
            ("12 o'clock", (12, 0)),
            ("twelve o'clock", (12, 0)),
            ("seven o'clock in the morning", (7, 0)),
            ("seven o'clock in the evening", (19, 0)),
            ("five o'clock in the morning", (5, 0)),
            ("eight o'clock at night", (20, 0)),
        ],
        ids=lambda x: str(x) if isinstance(x, tuple) else x,
    )
    def test_oclock(self, phrase, expected):
        assert parse_time_phrase(phrase) == expected

    @pytest.mark.parametrize(
        "phrase, expected",
        [
            ("half past seven", (7, 30)),
            ("half-past seven", (7, 30)),
            ("half past 7", (7, 30)),
        ],
        ids=lambda x: str(x) if isinstance(x, tuple) else x,
    )
    def test_half_past(self, phrase, expected):
        assert parse_time_phrase(phrase) == expected

    @pytest.mark.parametrize(
        "phrase, expected",
        [
            ("quarter past seven", (7, 15)),
            ("a quarter past seven", (7, 15)),
            ("quarter to eight", (7, 45)),
            ("a quarter to eight", (7, 45)),
            ("quarter to midnight", (23, 45)),
            ("quarter past noon", (12, 15)),
            ("quarter to noon", (11, 45)),
        ],
        ids=lambda x: str(x) if isinstance(x, tuple) else x,
    )
    def test_quarter(self, phrase, expected):
        assert parse_time_phrase(phrase) == expected

    @pytest.mark.parametrize(
        "phrase, expected",
        [
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
        ],
        ids=lambda x: str(x) if isinstance(x, tuple) else x,
    )
    def test_minutes_past_to(self, phrase, expected):
        assert parse_time_phrase(phrase) == expected

    @pytest.mark.parametrize(
        "phrase, expected",
        [
            ("seven thirty", (7, 30)),
            ("seven-thirty", (7, 30)),
            ("six forty-five", (6, 45)),
        ],
        ids=lambda x: str(x) if isinstance(x, tuple) else x,
    )
    def test_compound(self, phrase, expected):
        assert parse_time_phrase(phrase) == expected

    @pytest.mark.parametrize(
        "phrase, expected",
        [
            ("midnight", (0, 0)),
            ("noon", (12, 0)),
            ("midday", (12, 0)),
            ("noonday", (12, 0)),
        ],
        ids=lambda x: str(x) if isinstance(x, tuple) else x,
    )
    def test_special(self, phrase, expected):
        assert parse_time_phrase(phrase) == expected

    @pytest.mark.parametrize(
        "phrase, expected",
        [
            ("seven in the morning", (7, 0)),
            ("three in the afternoon", (15, 0)),
            ("four in the morning", (4, 0)),
            ("eight at night", (20, 0)),
        ],
        ids=lambda x: str(x) if isinstance(x, tuple) else x,
    )
    def test_period(self, phrase, expected):
        assert parse_time_phrase(phrase) == expected

    @pytest.mark.parametrize(
        "phrase, expected",
        [
            ("six AM", (6, 0)),
            ("three A.M.", (3, 0)),
            ("six a.m.", (6, 0)),
            ("two p.m.", (14, 0)),
            ("three PM", (15, 0)),
        ],
        ids=lambda x: str(x) if isinstance(x, tuple) else x,
    )
    def test_hour_ampm(self, phrase, expected):
        assert parse_time_phrase(phrase) == expected

    @pytest.mark.parametrize(
        "phrase, expected",
        [
            ("struck seven", (7, 0)),
            ("clock struck ten", (10, 0)),
            ("struck noon", (12, 0)),
        ],
        ids=lambda x: str(x) if isinstance(x, tuple) else x,
    )
    def test_struck(self, phrase, expected):
        assert parse_time_phrase(phrase) == expected

    @pytest.mark.parametrize(
        "phrase, expected",
        [
            ("about seven o'clock", (7, 0)),
            ("around 7:30", (7, 30)),
            ("almost eight", (8, 0)),
            ("nearly nine o'clock", (9, 0)),
            ("just after 4am", (4, 0)),
        ],
        ids=lambda x: str(x) if isinstance(x, tuple) else x,
    )
    def test_approximations(self, phrase, expected):
        assert parse_time_phrase(phrase) == expected

    def test_empty_string(self):
        assert parse_time_phrase("") is None

    def test_none(self):
        assert parse_time_phrase(None) is None

    def test_unparseable(self):
        assert parse_time_phrase("banana") is None

    def test_hour_out_of_range(self):
        assert parse_time_phrase("25:00") is None

    def test_minute_out_of_range(self):
        assert parse_time_phrase("12:60") is None

    def test_thirteen_oclock_rejected(self):
        """13 is outside the 1-12 range for o'clock patterns."""
        assert parse_time_phrase("thirteen o'clock") is None


# ── validate_time_phrase ────────────────────────────────────────────


class TestValidateTimePhrase:
    def test_exact_match(self):
        valid, parsed = validate_time_phrase("7:30", "07:30")
        assert valid is True

    def test_twelve_hour_ambiguity(self):
        valid, parsed = validate_time_phrase("seven o'clock", "19:00")
        assert valid is True

    def test_mismatch(self):
        valid, parsed = validate_time_phrase("seven o'clock", "08:00")
        assert valid is False

    def test_midnight_matches_twelve_am(self):
        """hour==0 should match expected==12 (12-hour ambiguity)."""
        valid, parsed = validate_time_phrase("midnight", "12:00")
        assert valid is True

    def test_noon_matches_zero(self):
        """hour==12 should match expected==0 (12-hour ambiguity)."""
        valid, parsed = validate_time_phrase("noon", "00:00")
        assert valid is True

    def test_unparseable(self):
        valid, parsed = validate_time_phrase("banana", "07:00")
        assert valid is False
        assert parsed is None
