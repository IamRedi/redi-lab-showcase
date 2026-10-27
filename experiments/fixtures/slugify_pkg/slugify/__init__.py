"""Stub slugify package — intentionally wrong baseline for REDI-03 canonical fixture.

The stub deliberately fails public tests on at least two cases so the factory line
must actually modify source to pass.
"""

from __future__ import annotations

from typing import Optional


def slugify(text: str, *, maxlen: Optional[int] = None, separator: str = "-") -> str:
    """Return a URL-friendly version of `text` (STUB — intentionally incomplete).

    V0 Stub behavior (intentionally wrong):
    * Returns text unchanged (no normalization, no separator collapsing, no trim).
    * If `maxlen` is provided, simply slice (can leave trailing separator).
    * If `text` is `None` or not a string → returns text unchanged (no type check).

    Correct behavior (what the Builder must implement):
    * Normalize unicode to ASCII (e.g., café → cafe).
    * Lowercase.
    * Replace any sequence of non-alphanumeric chars with a single separator.
    * Strip leading/trailing separators.
    * Respect `maxlen` by trimming cleanly at separator boundary if possible.
    """
    if not isinstance(text, str):
        return text
    if maxlen is not None and isinstance(maxlen, int) and maxlen > 0:
        return text[:maxlen]
    return text
