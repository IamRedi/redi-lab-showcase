"""Evidence layer: ledger-writer helpers (Layer C).

Thin wrappers around EvidenceLedger.append specialized for each entity type.
No business logic; only convenience helpers for lifecycle events / evidence / failures.
"""

from typing import Any, Mapping

from ..domain import (
    EvidenceId,
    ExperimentId,
    FactoryRunId,
    FailureRecord,
    GateResult,
    LifecycleEvent,
    RunEvidence,
)
from ..persistence import EvidenceLedger
from .artifacts import (
    ArtifactRetentionError,
    MAX_RETAINED_ARTIFACT_BYTES,
    retain_validated_artifact,
    retained_relative_path,
    verify_retained_artifact,
)


def append_event(
    ledger: EvidenceLedger,
    event: LifecycleEvent,
    *,
    factory_run_id: str | None = None,
    experiment_id: str | None = None,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    fr_id = factory_run_id
    if fr_id is None:
        fr_id = event.entity_ids.get("factory_run_id") or None
    exp_id = experiment_id
    if exp_id is None:
        exp_id = event.entity_ids.get("experiment_id") or None
    return ledger.append(
        event,
        factory_run_id=fr_id,
        experiment_id=exp_id,
        recorded_at=recorded_at,
    )


def append_run_evidence(
    ledger: EvidenceLedger,
    evidence: RunEvidence,
    *,
    factory_run_id: str | None = None,
    experiment_id: str | None = None,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    fr_id = factory_run_id if factory_run_id is not None else (str(evidence.factory_run_id) if evidence.factory_run_id else None)
    exp_id = experiment_id if experiment_id is not None else (str(evidence.experiment_id) if evidence.experiment_id else None)
    return ledger.append(
        evidence,
        factory_run_id=fr_id,
        experiment_id=exp_id,
        recorded_at=recorded_at,
    )


def append_failure_record(
    ledger: EvidenceLedger,
    failure: FailureRecord,
    *,
    factory_run_id: str | None = None,
    experiment_id: str | None = None,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    fr_id = factory_run_id if factory_run_id is not None else (str(failure.factory_run_id) if failure.factory_run_id else None)
    return ledger.append(
        failure,
        factory_run_id=fr_id,
        experiment_id=experiment_id,
        recorded_at=recorded_at,
    )


def append_gate_result(
    ledger: EvidenceLedger,
    gate: GateResult,
    *,
    factory_run_id: str | None = None,
    experiment_id: str | None = None,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    return ledger.append(
        gate,
        factory_run_id=factory_run_id,
        experiment_id=experiment_id,
        recorded_at=recorded_at,
    )


__all__ = [
    "ArtifactRetentionError",
    "MAX_RETAINED_ARTIFACT_BYTES",
    "retain_validated_artifact",
    "retained_relative_path",
    "verify_retained_artifact",
    "append_event",
    "append_run_evidence",
    "append_failure_record",
    "append_gate_result",
]
