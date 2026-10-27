"""Stub alpha module — intentionally incomplete baseline for the DAG pair fixture.

The DAG node bound to this module must implement `alpha(text)` so the
public tests pass.  Behavior is intentionally wrong at baseline.
"""

from __future__ import annotations


def alpha(text: str) -> str:
    """STUB — must be replaced: uppercase the text (baseline returns it unchanged)."""
    return text
