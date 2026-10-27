"""Public (builder-visible) tests for the slugify canonical fixture.

These tests are intentionally simple and shipped to the Builder. They are visible
in builder context and can be read by the Builder's bounded-context glob.

At least two assertions fail against the V0 stub, ensuring the factory line has
to produce real source changes to pass.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from slugify import slugify  # noqa: E402


def test_separator_simple():
    assert slugify("Hello World") == "hello-world"


def test_trailing_leading_separator_trimmed():
    assert slugify("-Hello-World-") == "hello-world"


def test_returns_lowercase():
    assert slugify("ABCD") == "abcd"


def test_maxlen_is_honored_for_short_input():
    # Input shorter than maxlen → simply slugified.
    assert slugify("ab", maxlen=5) == "ab"
