"""Public tests for the utils.text_utils helpers (unrelated to slugify)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils import text_utils  # noqa: E402


def test_count_words_simple():
    assert text_utils.count_words("one two three") == 3


def test_count_words_collapses_whitespace():
    assert text_utils.count_words("  a\t b\n c ") == 3


def test_count_words_empty():
    assert text_utils.count_words("") == 0


def test_is_blank():
    assert text_utils.is_blank("   ")
    assert not text_utils.is_blank("x")


def test_first_line():
    assert text_utils.first_line("a\nb") == "a"
    assert text_utils.first_line("") == ""
