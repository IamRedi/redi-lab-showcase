"""Provider-independent supporting value objects shared by multiple contracts.

These are immutable (or explicitly documented as mutable) dataclasses that reduce
duplication inside the top-level contracts.  Value objects are schema-versioned
indirectly via the top-level contract that owns them; they don't need a standalone
``schema_version`` field.

All monetary values use :class:`decimal.Decimal` explicitly so rounding behaviour
survives serialization round trips.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Mapping


# ---------------------------------------------------------------------------
# Requirement / acceptance criteria (ProductSpec, TaskContract)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Requirement:
    """A single explicit statement or constraint.

    Unknown requirements are modelled by ``known=False`` instead of guessed fields.
    """

    text: str
    kind: Literal["functional", "non_functional", "constraint", "acceptance", "unknown"]
    reference: str | None = None
    known: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("Requirement.text must be a non-empty string")


@dataclass(frozen=True)
class AcceptanceCriterion:
    """A concrete, independently verifiable acceptance statement."""

    description: str
    criterion_type: Literal["deterministic", "model_based", "manual", "hybrid"] = "deterministic"
    reference: str | None = None
    evidence_needed: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("AcceptanceCriterion.description must be non-empty")


# ---------------------------------------------------------------------------
# Monetary / token / timing / budget observations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MonetaryCost:
    """A tagged monetary amount in a specific currency.

    ``amount`` is a :class:`Decimal` to avoid float rounding during JSON round-trips.
    Negative amounts mean refunds/credits only; contracts reject negative amounts
    as "primary cost" via :mod:`redi_lab.domain.validation`.
    """

    amount: Decimal
    currency: str = "USD"  # ISO 4217 3-letter code
    basis: Literal["estimated", "billed", "observed", "reported"] = "observed"
    schedule_version: str | None = None  # pricing-version traceability

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            raise TypeError("MonetaryCost.amount must be a Decimal")
        if not isinstance(self.currency, str) or len(self.currency) not in (3,):
            raise ValueError("MonetaryCost.currency must be a 3-letter ISO code")


@dataclass(frozen=True)
class TokenUsage:
    """Structured input/output/caching token counts (experiment-policy §10)."""

    input: int | None = None
    output: int | None = None
    cached_input: int | None = None
    reasoning: int | None = None
    other: Mapping[str, int] = field(default_factory=dict)

    def total(self) -> int | None:
        known: list[int] = []
        for v in (self.input, self.output, self.cached_input, self.reasoning):
            if v is not None:
                known.append(v)
        known.extend(self.other.values())
        if not known:
            return None
        return sum(known)

    def __post_init__(self) -> None:
        for name, value in (
            ("input", self.input),
            ("output", self.output),
            ("cached_input", self.cached_input),
            ("reasoning", self.reasoning),
        ):
            if value is not None and value < 0:
                raise ValueError(f"TokenUsage.{name} must be non-negative if set")
        for k, v in self.other.items():
            if not isinstance(k, str) or not isinstance(v, int) or v < 0:
                raise ValueError(f"TokenUsage.other key {k!r} must map to non-negative int")


@dataclass(frozen=True)
class Timing:
    """Wall-clock vs aggregate compute timing (experiment-policy §9 and §10)."""

    started_at: datetime | None = None
    ended_at: datetime | None = None
    wall_clock_seconds: float | None = None
    compute_seconds: float | None = None
    queue_seconds: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in (
            ("wall_clock_seconds", self.wall_clock_seconds),
            ("compute_seconds", self.compute_seconds),
            ("queue_seconds", self.queue_seconds),
        ):
            if value is not None and value < 0:
                raise ValueError(f"Timing.{name} must be non-negative if set")


@dataclass(frozen=True)
class Budget:
    """Time / cost / token / retry budget attached to a run or experiment."""

    wall_clock_seconds: float | None = None
    tokens_total: int | None = None
    cost: MonetaryCost | None = None
    retries: int | None = None
    interventions: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in (
            ("wall_clock_seconds", self.wall_clock_seconds),
        ):
            if value is not None and value <= 0:
                raise ValueError(f"Budget.{name} must be positive if set")
        for name, value in (
            ("tokens_total", self.tokens_total),
            ("retries", self.retries),
            ("interventions", self.interventions),
        ):
            if value is not None and value < 0:
                raise ValueError(f"Budget.{name} must be non-negative if set")


@dataclass(frozen=True)
class ResourceUsage:
    """Generic resource observation — CPU, RAM, etc. — normalized provider-independent."""

    resource: str
    value: float
    unit: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.resource, str) or not self.resource.strip():
            raise ValueError("ResourceUsage.resource must be non-empty")
        if not isinstance(self.unit, str) or not self.unit.strip():
            raise ValueError("ResourceUsage.unit must be non-empty")


# ---------------------------------------------------------------------------
# Artifact references (context, handoff, evidence)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactRef:
    """Provider-independent reference to a produced, consumed, or read artifact.

    Never embed full file contents here; content lives in storage and is looked up
    by the ``location`` URI pattern outside the domain layer.
    """

    ref_id: str
    kind: Literal["file", "patch", "directory", "artifact", "trace", "report", "other"]
    location: str | None = None
    sha256: str | None = None
    source: Literal["input", "output", "handoff", "context", "generated"] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.ref_id, str) or not self.ref_id.strip():
            raise ValueError("ArtifactRef.ref_id must be non-empty")


# ---------------------------------------------------------------------------
# DAG structural value objects — owned by ProductDAG
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DagDependency:
    """A single upstream-prerequisite dependency between two DAG nodes."""

    upstream: str
    downstream: str
    kind: Literal["hard", "soft", "side_effect"] = "hard"

    def __post_init__(self) -> None:
        if not isinstance(self.upstream, str) or not self.upstream.strip():
            raise ValueError("DagDependency.upstream must be non-empty")
        if not isinstance(self.downstream, str) or not self.downstream.strip():
            raise ValueError("DagDependency.downstream must be non-empty")
        if self.upstream == self.downstream:
            raise ValueError("DagDependency upstream and downstream must differ")


@dataclass(frozen=True)
class DagNode:
    """A single structural node in a ProductDAG.

    The only validation performed at domain level is structural uniqueness /
    dependency resolution.  Runtime assignment and scheduling live in the
    orchestrator layer (REDI-02+), never here.
    """

    node_id: str
    task_id: str | None = None  # foreign key into TaskContract.id after PLAN_COMPILED
    station_id: str | None = None
    role_hint: str | None = None
    parallel_group: str | None = None  # nodes with same group run concurrently if ready
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.node_id, str) or not self.node_id.strip():
            raise ValueError("DagNode.node_id must be non-empty")


# ---------------------------------------------------------------------------
# Supporting evidence / validity / environment value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceFieldRef:
    """A weak pointer to a supporting evidence record, used by recipe provenance."""

    evidence_id: str
    kind: Literal[
        "run",
        "gate",
        "replay",
        "experiment",
        "external",
        "manual",
        "audit",
    ]
    note: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_id, str) or not self.evidence_id.strip():
            raise ValueError("EvidenceFieldRef.evidence_id must be non-empty")


@dataclass(frozen=True)
class ValidityCondition:
    """A single precondition a recipe / experiment relies on."""

    description: str
    scope: Literal["worker", "model", "environment", "tool", "pricing", "data", "other"]
    checked_via: Literal["hash", "version", "calibration", "replay", "external", "other"]
    expires_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("ValidityCondition.description must be non-empty")


@dataclass(frozen=True)
class EnvironmentInfo:
    """Normalized version/hash capture — experiment-policy §6, §7."""

    software_revision: str | None = None
    dependency_hash: str | None = None
    python_version: str | None = None
    os_description: str | None = None
    sandbox_version: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


__all__ = [
    "Requirement",
    "AcceptanceCriterion",
    "MonetaryCost",
    "TokenUsage",
    "Timing",
    "Budget",
    "ResourceUsage",
    "ArtifactRef",
    "DagDependency",
    "DagNode",
    "EvidenceFieldRef",
    "ValidityCondition",
    "EnvironmentInfo",
]
