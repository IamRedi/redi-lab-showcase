"""Public tests for the alpha module (DAG node A's deliverable)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpha import alpha  # noqa: E402


def test_alpha_uppercases():
    assert alpha("abc") == "ABC"


def test_alpha_preserves_digits():
    assert alpha("a1b2") == "A1B2"
