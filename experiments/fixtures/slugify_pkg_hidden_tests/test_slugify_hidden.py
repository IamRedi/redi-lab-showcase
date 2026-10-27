"""HIDDEN acceptance tests for canonical slugify fixture.

These tests live OUTSIDE the builder-visible fixture root.  The gate engine runs
them via a separate subprocess with PYTHONPATH set so hidden-test source is NEVER
copied into the builder workspace.  The Reviewer only sees the RESULT SUMMARY,
never this source code.

Cases exercise four distinct edge behaviors:
  1. Unicode normalization (accent stripping).
  2. Multiple internal separators collapse to one dash.
  3. Leading/trailing dashes trimmed after normalization.
  4. maxlen trims cleanly at boundary.
"""

from __future__ import annotations

from slugify import slugify  # noqa: E402


def test_unicode_accent_stripped():
    assert slugify("Café résumé") == "cafe-resume"


def test_multiple_separators_collapsed():
    assert slugify("a--b///c___d  e") == "a-b-c-d-e"


def test_leading_trailing_trimmed_after_normalize():
    assert slugify("  ---  Hello World !!  ---") == "hello-world"


def test_maxlen_trims_cleanly_at_separator():
    result = slugify("hello-world-foo", maxlen=10)
    assert result == "hello-world" or result == "hello-worl", (
        f"expected trim-friendly output, got {result!r}"
    )
    assert len(result) <= 10


def test_empty_input_returns_empty():
    assert slugify("") == ""
    assert slugify("!!!") == ""
