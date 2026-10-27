"""Small text helpers used by the examples/ CLI demo.

Unrelated to the slugify task: these helpers only count words and detect
blank input.  They exist so the workspace carries more than one module.
"""

from __future__ import annotations


def count_words(text: str) -> int:
    """Return the number of whitespace-separated words in *text*."""
    if not isinstance(text, str):
        raise TypeError("text must be str")
    return len(text.split())


def is_blank(text: str) -> bool:
    """Return True when *text* is empty or only whitespace."""
    return not (text or "").strip()


def first_line(text: str) -> str:
    """Return the first line of *text* ("" when there is none)."""
    if not isinstance(text, str):
        raise TypeError("text must be str")
    return text.split("\n", 1)[0]
