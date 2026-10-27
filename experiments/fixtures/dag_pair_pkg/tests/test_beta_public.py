"""Public tests for the beta module (DAG node B's deliverable)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from beta import beta  # noqa: E402


def test_beta_reverse_simple():
    assert beta("abc") == "cba"


def test_beta_palindrome_is_stable():
    assert beta("aba") == "aba"
