"""Deterministic read-only replay subsystem (Layer C).

Replay reconstructs recorded research state from immutable persisted evidence.
No writes, no external calls, no model execution.
"""

from .result import ReplayResult
from .reader import ReplayReader

__all__ = ["ReplayResult", "ReplayReader"]
