"""Stub beta module — intentionally incomplete baseline for the DAG pair fixture.

The DAG node bound to this module must implement `beta(text)` so the
public tests pass.  Behavior is intentionally wrong at baseline.
"""

from __future__ import annotations


def beta(text: str) -> str:
    """STUB — must be replaced: reverse the text (baseline returns it unchanged)."""
    return text
