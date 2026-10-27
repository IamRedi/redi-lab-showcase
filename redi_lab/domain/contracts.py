"""All 14 canonical top-level domain contracts.

Every dataclass here satisfies the :class:`VersionedContract` structural protocol:
    - ``id`` = a provider-independent opaque NewType id
    - ``schema_version`` = an int defaulting to ``SCHEMA_VERSION``

Fields are drawn directly from the six governance-baseline documents.  Their names
match the REDI_LAB_RESEARCH_PLAN.md §23 contract inventory.  No provider-specific
types are imported or referenced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Mapping, Sequence

from .enums import (
    FailureCategory,
    GateStatus,
    GateType,
    Mode,
    RecipeRung,
    RiskTier,
    RunStatus,
    Severity,
)
from .identity import (
    SCHEMA_VERSION,
    ContextManifestId,
    EvidenceId,
    ExperimentId,
    FactoryRunId,
    FailureId,
    GateResultId,
    HandoffManifestId,
    ProductClassId,
    ProductId,
    RecipeId,
    StationId,
    TaskId,
    WorkerId,
    WorkerPoolId,
    new_id,
)
from .value_objects import (
    AcceptanceCriterion,
    ArtifactRef,
    Budget,
    DagDependency,
    DagNode,
    EnvironmentInfo,
    EvidenceFieldRef,
    MonetaryCost,
    Requirement,
    Timing,
    TokenUsage,
    ValidityCondition,
)


# ---------------------------------------------------------------------------
# 1. ProductSpec
# ---------------------------------------------------------------------------


@dataclass
class ProductSpec:
    id: ProductId = field(default_factory=lambda: ProductId(new_id("product")))
    schema_version: int = SCHEMA_VERSION
    requested_outcome: str = ""
    requirements: Sequence[Requirement] = field(default_factory=tuple)
    constraints: Sequence[Requirement] = field(default_factory=tuple)
    acceptance_criteria: Sequence[AcceptanceCriterion] = field(default_factory=tuple)
    unresolved_requirements: Sequence[str] = field(default_factory=tuple)
    risk_tier: RiskTier | None = None
    requested_artifacts: Sequence[str] = field(default_factory=tuple)
    product_class_id: ProductClassId | None = None
    product_class_candidates: Sequence[ProductClassId] = field(default_factory=tuple)
    budget: Budget | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)

    def __post_init__(self) -> None:
        # Late local import avoids circular contracts↔validation at module top.
        from .validation import validate_contract
        validate_contract(self)


# ---------------------------------------------------------------------------
# 2. ProductClass
# ---------------------------------------------------------------------------


@dataclass
class ProductClass:
    id: ProductClassId = field(default_factory=lambda: ProductClassId(new_id("product_class")))
    schema_version: int = SCHEMA_VERSION
    name: str = ""
    description: str = ""
    signature: Mapping[str, Any] = field(default_factory=dict)
    compatible_recipe_ids: Sequence[RecipeId] = field(default_factory=tuple)
    classification_evidence: Sequence[EvidenceFieldRef] = field(default_factory=tuple)
    version: int = 1
    superseded_by: ProductClassId | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)

    def __post_init__(self) -> None:
        from .validation import validate_contract
        validate_contract(self)


# ---------------------------------------------------------------------------
# 3. ProductRecipe
# ---------------------------------------------------------------------------


@dataclass
class ProductRecipe:
    id: RecipeId = field(default_factory=lambda: RecipeId(new_id("recipe")))
    schema_version: int = SCHEMA_VERSION
    product_class_id: ProductClassId | None = None
    dag_template_id: str | None = None  # refers to a ProductDAG.id
    required_stations: Sequence[StationId] = field(default_factory=tuple)
    worker_preferences: Mapping[StationId, Sequence[WorkerId]] = field(default_factory=dict)
    worker_fallbacks: Mapping[StationId, Sequence[WorkerId]] = field(default_factory=dict)
    context_budgets: Mapping[str, Budget] = field(default_factory=dict)
    gate_policies: Mapping[str, Any] = field(default_factory=dict)
    retry_policies: Mapping[str, Any] = field(default_factory=dict)
    escalation_policies: Mapping[str, Any] = field(default_factory=dict)
    parallelization_rules: Mapping[str, Any] = field(default_factory=dict)
    evidence: Sequence[EvidenceFieldRef] = field(default_factory=tuple)
    provenance: Mapping[str, Any] = field(default_factory=dict)
    rung: RecipeRung = RecipeRung.CANDIDATE
    validity_conditions: Sequence[ValidityCondition] = field(default_factory=tuple)
    version: int = 1
    last_revalidated_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# 4. ProductDAG
# ---------------------------------------------------------------------------


@dataclass
class ProductDAG:
    id: str = field(default_factory=lambda: new_id("run"))  # stable versioned graph id
    schema_version: int = SCHEMA_VERSION
    product_id: ProductId | None = None
    revision: int = 1
    nodes: Sequence[DagNode] = field(default_factory=tuple)
    dependencies: Sequence[DagDependency] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)

    # Structural validation lives in redi_lab.domain.validation: validate_dag


# ---------------------------------------------------------------------------
# 5. StationContract
# ---------------------------------------------------------------------------


@dataclass
class StationContract:
    id: StationId = field(default_factory=lambda: StationId(new_id("station")))
    schema_version: int = SCHEMA_VERSION
    role: str = ""
    description: str = ""
    accepted_input_kinds: Sequence[str] = field(default_factory=tuple)
    required_output_kinds: Sequence[str] = field(default_factory=tuple)
    owned_scope: Sequence[str] = field(default_factory=tuple)
    forbidden_scope: Sequence[str] = field(default_factory=tuple)
    allowed_tool_classes: Sequence[str] = field(default_factory=tuple)
    applicable_task_classes: Sequence[str] = field(default_factory=tuple)
    gate_requirements: Mapping[str, Any] = field(default_factory=dict)
    handoff_contracts: Mapping[str, Any] = field(default_factory=dict)
    worker_eligibility: Mapping[str, Any] = field(default_factory=dict)
    version: int = 1
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)

    def __post_init__(self) -> None:
        from .validation import validate_contract
        validate_contract(self)


# ---------------------------------------------------------------------------
# 6. WorkerProfile
# ---------------------------------------------------------------------------


@dataclass
class WorkerProfile:
    id: WorkerId = field(default_factory=lambda: WorkerId(new_id("worker")))
    schema_version: int = SCHEMA_VERSION
    display_name: str = ""
    version: str = ""
    capability_tags: Sequence[str] = field(default_factory=tuple)
    context_capabilities: Mapping[str, Any] = field(default_factory=dict)
    tool_capabilities: Mapping[str, Any] = field(default_factory=dict)
    structured_output_capable: bool | None = None
    station_eligibility: Sequence[StationId] = field(default_factory=tuple)
    cost_reference: MonetaryCost | None = None
    availability_status: Literal["available", "unavailable", "deprecated"] | None = None
    provider_opaque_reference: str | None = None  # provider-specific data stays opaque
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)

    def __post_init__(self) -> None:
        from .validation import validate_contract
        validate_contract(self)


# ---------------------------------------------------------------------------
# 6b. WorkerPool (REDI-04A) — ordered eligible-worker ladder for one station.
#
# Configuration type, NOT a ledger entity and intentionally NOT a member of
# CANONICAL_CONTRACTS (that registry stays the frozen 14-contract governance
# baseline).  Provider/model neutral: entries reference WorkerProfiles, whose
# provider data stays opaque in WorkerProfile.provider_opaque_reference.
# Ordering of `entries` IS the deterministic preference order (rank 0 first).
# ---------------------------------------------------------------------------


@dataclass
class PoolEntry:
    """One eligible worker in a station's pool, with its preference rank."""

    profile: WorkerProfile
    preference_rank: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class WorkerPool:
    id: WorkerPoolId = field(default_factory=lambda: WorkerPoolId(new_id("worker_pool")))
    schema_version: int = SCHEMA_VERSION
    station_id: StationId | None = None
    role: str = ""
    entries: Sequence[PoolEntry] = field(default_factory=tuple)
    selection_policy_id: str = "deterministic_v0"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)

    def __post_init__(self) -> None:
        from .validation import validate_contract
        validate_contract(self)


# ---------------------------------------------------------------------------
# 7. TaskContract
# ---------------------------------------------------------------------------


@dataclass
class TaskContract:
    id: TaskId = field(default_factory=lambda: TaskId(new_id("task")))
    schema_version: int = SCHEMA_VERSION
    goal: str = ""
    station_id: StationId | None = None
    role_requirement: str | None = None
    allowed_scope: Sequence[str] = field(default_factory=tuple)
    forbidden_scope: Sequence[str] = field(default_factory=tuple)
    inputs: Sequence[ArtifactRef] = field(default_factory=tuple)
    expected_outputs: Sequence[ArtifactRef] = field(default_factory=tuple)
    acceptance_criteria: Sequence[AcceptanceCriterion] = field(default_factory=tuple)
    prerequisites: Sequence[TaskId] = field(default_factory=tuple)
    constraints: Sequence[Requirement] = field(default_factory=tuple)
    verification_requirements: Sequence[str] = field(default_factory=tuple)
    budget: Budget | None = None
    risk_metadata: Mapping[str, Any] = field(default_factory=dict)
    escalation_conditions: Sequence[Requirement] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)

    def __post_init__(self) -> None:
        from .validation import validate_contract
        validate_contract(self)


# ---------------------------------------------------------------------------
# 8. ContextManifest
# ---------------------------------------------------------------------------


@dataclass
class ContextManifest:
    id: ContextManifestId = field(
        default_factory=lambda: ContextManifestId(new_id("context_manifest"))
    )
    schema_version: int = SCHEMA_VERSION
    task_id: TaskId | None = None
    station_id: StationId | None = None
    product_id: ProductId | None = None
    project_inputs: Sequence[ArtifactRef] = field(default_factory=tuple)
    source_references: Sequence[ArtifactRef] = field(default_factory=tuple)
    upstream_handoffs: Sequence[HandoffManifestId] = field(default_factory=tuple)
    previous_decisions: Sequence[EvidenceFieldRef] = field(default_factory=tuple)
    failure_evidence_included: Sequence[FailureId] = field(default_factory=tuple)
    explicit_exclusions: Sequence[ArtifactRef] = field(default_factory=tuple)
    context_budget: Budget | None = None
    deterministic_hash: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# 9. HandoffManifest
# ---------------------------------------------------------------------------


@dataclass
class HandoffManifest:
    id: HandoffManifestId = field(
        default_factory=lambda: HandoffManifestId(new_id("handoff_manifest"))
    )
    schema_version: int = SCHEMA_VERSION
    producer_station_id: StationId | None = None
    consumer_scope: str | None = None  # downstream station id / or "*" for broadcast
    node_id: str | None = None  # ProductDAG node reference
    task_id: TaskId | None = None
    produced_artifacts: Sequence[ArtifactRef] = field(default_factory=tuple)
    public_interfaces: Sequence[str] = field(default_factory=tuple)
    guarantees: Sequence[str] = field(default_factory=tuple)
    validation_already_performed: Sequence[AcceptanceCriterion] = field(default_factory=tuple)
    known_risks: Sequence[str] = field(default_factory=tuple)
    open_issues: Sequence[str] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# 10. GateResult
# ---------------------------------------------------------------------------


@dataclass
class GateResult:
    id: GateResultId = field(default_factory=lambda: GateResultId(new_id("gate_result")))
    schema_version: int = SCHEMA_VERSION
    gate_identity: str = ""
    gate_type: GateType = GateType.DETERMINISTIC
    target_node_id: str | None = None
    target_artifact: ArtifactRef | None = None
    target_task_id: TaskId | None = None
    criteria: Sequence[AcceptanceCriterion] = field(default_factory=tuple)
    status: GateStatus = GateStatus.BLOCKED
    evidence_refs: Sequence[EvidenceFieldRef] = field(default_factory=tuple)
    verifier_type: Literal["deterministic_code", "model", "human", "mixed"] | None = None
    verifier_identity: str | None = None
    failure_reasons: Sequence[str] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)

    def __post_init__(self) -> None:
        from .validation import validate_contract
        validate_contract(self)


# ---------------------------------------------------------------------------
# 11. RunEvidence
# ---------------------------------------------------------------------------


@dataclass
class RunEvidence:
    id: EvidenceId = field(default_factory=lambda: EvidenceId(new_id("evidence")))
    schema_version: int = SCHEMA_VERSION
    experiment_id: ExperimentId | None = None
    factory_run_id: FactoryRunId | None = None
    product_id: ProductId | None = None
    product_class_id: ProductClassId | None = None
    recipe_id: RecipeId | None = None
    recipe_version: int | None = None
    node_id: str | None = None
    station_id: StationId | None = None
    task_class: str | None = None
    worker_id: WorkerId | None = None
    worker_version: str | None = None
    context_manifest_id: ContextManifestId | None = None
    token_usage: TokenUsage | None = None
    timing: Timing | None = None
    artifacts_read: Sequence[ArtifactRef] = field(default_factory=tuple)
    artifacts_changed: Sequence[ArtifactRef] = field(default_factory=tuple)
    tests_executed: Sequence[str] = field(default_factory=tuple)
    gate_results: Sequence[GateResultId] = field(default_factory=tuple)
    retry_count: int | None = None
    escalation: bool | None = None
    human_intervention: bool | None = None
    cost: MonetaryCost | None = None
    final_status: RunStatus | None = None
    failure_category: FailureCategory | None = None
    environment: EnvironmentInfo | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# 12. FailureRecord
# ---------------------------------------------------------------------------


@dataclass
class FailureRecord:
    id: FailureId = field(default_factory=lambda: FailureId(new_id("failure")))
    schema_version: int = SCHEMA_VERSION
    category: FailureCategory = FailureCategory.SPECIFICATION_AMBIGUITY
    origin: str = ""
    node_id: str | None = None
    task_id: TaskId | None = None
    factory_run_id: FactoryRunId | None = None
    details: Mapping[str, Any] = field(default_factory=dict)
    severity: Severity = Severity.ERROR
    recoverable: bool | None = None
    escalation_target: str | None = None
    evidence_id: EvidenceId | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# 13. ExperimentManifest
# ---------------------------------------------------------------------------


@dataclass
class ExperimentManifest:
    id: ExperimentId = field(default_factory=lambda: ExperimentId(new_id("experiment")))
    schema_version: int = SCHEMA_VERSION
    title: str = ""
    hypothesis_reference: str = ""  # pointer to H1..H9 / RQ / charter question
    research_question: str | None = None
    arms: Mapping[str, Any] = field(default_factory=dict)
    baselines: Mapping[str, Any] = field(default_factory=dict)
    task_selection: Mapping[str, Any] = field(default_factory=dict)
    environment: EnvironmentInfo | None = None
    worker_declarations: Mapping[WorkerId, Mapping[str, Any]] = field(default_factory=dict)
    context_strategy: Mapping[str, Any] = field(default_factory=dict)
    budgets: Mapping[str, Budget] = field(default_factory=dict)
    acceptance_criteria: Sequence[AcceptanceCriterion] = field(default_factory=tuple)
    preregistered_thresholds: Mapping[str, Any] = field(default_factory=dict)
    randomization: Mapping[str, Any] = field(default_factory=dict)
    repetition_config: Mapping[str, Any] = field(default_factory=dict)
    measurement_plan: Mapping[str, Any] = field(default_factory=dict)
    pricing_assumptions: Mapping[str, Any] = field(default_factory=dict)
    version_assumptions: Mapping[str, Any] = field(default_factory=dict)
    human_intervention_policy: Mapping[str, Any] = field(default_factory=dict)
    replay_policy: Mapping[str, Any] = field(default_factory=dict)
    validity_controls: Mapping[str, Any] = field(default_factory=dict)
    preregistered_at: datetime = field(default_factory=datetime.utcnow)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)

    def __post_init__(self) -> None:
        from .validation import validate_contract
        validate_contract(self)


# ---------------------------------------------------------------------------
# 14. FactoryRun
# ---------------------------------------------------------------------------


@dataclass
class FactoryRun:
    id: FactoryRunId = field(default_factory=lambda: FactoryRunId(new_id("factory_run")))
    schema_version: int = SCHEMA_VERSION
    product_spec_id: ProductId | None = None
    dag_id: str | None = None  # ProductDAG.id
    recipe_id: RecipeId | None = None
    mode: Mode = Mode.EXPLORATION
    final_status: RunStatus = RunStatus.PENDING
    verified: bool = False
    timing: Timing | None = None
    total_cost: MonetaryCost | None = None
    total_tokens: TokenUsage | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Canonical inventory — ordering matches §23 of REDI_LAB_RESEARCH_PLAN.md v0.2
# ---------------------------------------------------------------------------

CANONICAL_CONTRACTS: tuple[type[Any], ...] = (
    ProductSpec,
    ProductClass,
    ProductRecipe,
    ProductDAG,
    StationContract,
    WorkerProfile,
    TaskContract,
    ContextManifest,
    HandoffManifest,
    GateResult,
    RunEvidence,
    FailureRecord,
    ExperimentManifest,
    FactoryRun,
)


__all__ = [
    "CANONICAL_CONTRACTS",
    "ProductSpec",
    "ProductClass",
    "ProductRecipe",
    "ProductDAG",
    "StationContract",
    "WorkerProfile",
    "PoolEntry",
    "WorkerPool",
    "TaskContract",
    "ContextManifest",
    "HandoffManifest",
    "GateResult",
    "RunEvidence",
    "FailureRecord",
    "ExperimentManifest",
    "FactoryRun",
]
