"""Tests for prepare_review_csv module."""

import re

from prepare_review_csv import (
    build_digital_patterns,
    build_text_patterns,
    get_hour_12,
    has_time_context,
    needs_review,
    validate_ampm,
)

# ── validate_ampm ───────────────────────────────────────────────────


class TestValidateAmpm:
    def test_am_morning_hour(self):
        assert validate_ampm(7, "am") is True

    def test_pm_morning_hour(self):
        assert validate_ampm(7, "pm") is False

    def test_pm_afternoon_hour(self):
        assert validate_ampm(15, "pm") is True

    def test_am_midnight(self):
        assert validate_ampm(0, "am") is True

    def test_pm_noon(self):
        assert validate_ampm(12, "pm") is True

    def test_am_noon_invalid(self):
        assert validate_ampm(12, "am") is False

    def test_pm_midnight_invalid(self):
        assert validate_ampm(0, "pm") is False

    def test_none_always_valid(self):
        assert validate_ampm(7, None) is True
        assert validate_ampm(15, None) is True

    def test_dotted_format(self):
        assert validate_ampm(7, "a.m.") is True
        assert validate_ampm(15, "p.m.") is True


# ── get_hour_12 ─────────────────────────────────────────────────────


class TestGetHour12:
    def test_midnight(self):
        assert get_hour_12(0) == 12

    def test_afternoon(self):
        assert get_hour_12(13) == 1

    def test_noon(self):
        assert get_hour_12(12) == 12

    def test_morning(self):
        assert get_hour_12(6) == 6

    def test_late_night(self):
        assert get_hour_12(23) == 11

    def test_one_am(self):
        assert get_hour_12(1) == 1


# ── has_time_context ────────────────────────────────────────────────


class TestHasTimeContext:
    def test_struck_context(self):
        assert has_time_context("seven", "The clock struck seven") is True

    def test_no_context(self):
        assert has_time_context("seven", "seven dwarfs went to the mine") is False

    def test_oclock_context(self):
        assert has_time_context("four", "It was four o'clock") is True

    def test_past_context(self):
        assert has_time_context("eight", "ten past eight") is True

    def test_at_context(self):
        assert has_time_context("nine", "Meet me at nine") is True

    def test_half_past_context(self):
        assert has_time_context("ten", "half past ten in the evening") is True

    def test_partial_word_false_positive(self):
        """Known limitation: substring matching means 'past eight' in non-time context matches.

        If this starts failing, the matching has been improved to use word boundaries —
        update the assertion to `is False`.
        """
        assert has_time_context("eight", "We drove past eight houses on the road") is True


# ── needs_review ────────────────────────────────────────────────────


class TestNeedsReview:
    def test_confident_no_issues(self):
        # Quote must be >50 chars to avoid "Very short quote" flag
        quote = "It was 7:30 in the morning when the alarm went off and woke the whole house."
        review, reason = needs_review("07:30", "7:30", quote, "YES")
        assert review is False
        assert reason == ""

    def test_html_tags(self):
        review, reason = needs_review("07:30", "7:30", "It was <br>7:30", "YES")
        assert review is True
        assert "HTML" in reason

    def test_short_quote(self):
        review, reason = needs_review("07:30", "7:30", "At 7:30.", "YES")
        assert review is True
        assert "short" in reason.lower()

    def test_not_confident_suspicious_word(self):
        review, reason = needs_review("07:00", "seven", "seven dwarfs", "NO")
        assert review is True

    def test_not_confident_phrase_missing(self):
        review, reason = needs_review("07:30", "seven thirty", "The clock said 7:30", "NO")
        assert review is True
        assert "not found" in reason.lower()

    def test_digital_fallback(self):
        # Quote >50 chars so only the digital-fallback and not-confident flags trigger
        quote = "There was no time reference anywhere in this long passage of fictional text at all."
        review, reason = needs_review("07:30", "07:30", quote, "NO")
        assert review is True
        assert "No time phrase found" in reason


# ── build_digital_patterns ──────────────────────────────────────────


class TestBuildDigitalPatterns:
    def test_returns_nonempty(self):
        patterns = build_digital_patterns(7, 30)
        assert len(patterns) > 0

    def test_patterns_are_valid_regex(self):
        patterns = build_digital_patterns(14, 15)
        for pattern, _desc, _ampm in patterns:
            re.compile(pattern)  # Should not raise

    def test_contains_separator_variants(self):
        patterns = build_digital_patterns(7, 30)
        descs = [d for _, d, _ in patterns]
        assert any(":" in d for d in descs)
        assert any("." in d for d in descs)


# ── build_text_patterns ─────────────────────────────────────────────


class TestBuildTextPatterns:
    def test_returns_nonempty_for_oclock(self):
        patterns = build_text_patterns(7, 0)
        assert len(patterns) > 0

    def test_noon_pattern(self):
        patterns = build_text_patterns(12, 0)
        descs = [d for _, d in patterns]
        assert any("noon" in d for d in descs)

    def test_half_past_pattern(self):
        patterns = build_text_patterns(7, 30)
        descs = [d for _, d in patterns]
        assert any("half past" in d for d in descs)

    def test_quarter_to_pattern(self):
        patterns = build_text_patterns(7, 45)
        descs = [d for _, d in patterns]
        assert any("quarter to" in d for d in descs)

    def test_minutes_past_pattern(self):
        patterns = build_text_patterns(7, 10)
        descs = [d for _, d in patterns]
        assert any("past" in d for d in descs)

    def test_valid_regex(self):
        patterns = build_text_patterns(12, 0)
        for pattern, _ in patterns:
            re.compile(pattern)

    def test_midnight_patterns(self):
        patterns = build_text_patterns(0, 0)
        descs = [d for _, d in patterns]
        assert "midnight" in descs
        assert "twelve midnight" in descs
        assert len(patterns) >= 2
