"""ReplayResult: reconstructed read-only state from immutable ledger evidence.

All 9 reconstruction fields are evidence-backed. No inference of missing facts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence


@dataclass(frozen=True)
class ReplayResult:
    experiment_id: str | None
    factory_run_id: str
    ordered_events: tuple[Any, ...]
    observed_run_status: str
    completed_task_ids: tuple[str, ...]
    gate_outcomes: tuple[tuple[str, str, str | None], ...]
    failures: tuple[Any, ...]
    evidence_refs: tuple[tuple[str, str, int], ...]
    final_verification_state: bool
    integrity_status: str
    integrity_flags: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    run_evidences: tuple[Any, ...] = field(default_factory=tuple)
    gate_results: tuple[Any, ...] = field(default_factory=tuple)


__all__ = ["ReplayResult"]
