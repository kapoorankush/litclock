"""Tests for clean_csv module."""

from clean_csv import clean_text, normalize_for_comparison

# ── clean_text ──────────────────────────────────────────────────────


class TestCleanText:
    def test_escaped_quotes(self):
        assert clean_text(r"He said \"hello\"") == 'He said "hello"'

    def test_multiple_backslash_quotes(self):
        assert clean_text(r"She said \\\"hi\\\"") == 'She said "hi"'

    def test_escaped_newlines(self):
        assert clean_text(r"line one\nline two") == "line one line two"

    def test_trailing_backslashes(self):
        assert clean_text("text\\\\") == "text"

    def test_whitespace_normalization(self):
        assert clean_text("  too   much   space  ") == "too much space"

    def test_clean_text_already_clean(self):
        assert clean_text("already clean") == "already clean"

    def test_empty_string(self):
        assert clean_text("") == ""


# ── normalize_for_comparison ────────────────────────────────────────


class TestNormalizeForComparison:
    def test_punctuation_removal(self):
        result = normalize_for_comparison("Hello, World!")
        assert result == "hello world"

    def test_case_folding(self):
        result = normalize_for_comparison("HELLO")
        assert result == "hello"

    def test_whitespace_collapse(self):
        result = normalize_for_comparison("  too   much   space  ")
        assert result == "too much space"

    def test_combined(self):
        result = normalize_for_comparison("It's a test, isn't it?")
        assert result == "its a test isnt it"
