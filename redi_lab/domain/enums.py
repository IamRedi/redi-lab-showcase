"""Stable provider-independent enums used across all REDI Lab domain contracts.

Every enum value here comes directly from the six governance-baseline documents.
Values must not be renamed or re-ordered without a versioned schema change.
"""

from __future__ import annotations

from enum import Enum, IntEnum, StrEnum, unique


@unique
class SchemaVersion(IntEnum):
    """Initial REDI-01 schema version for all top-level contracts.

    Governed by `SCHEMA_VERSION` in `redi_lab.domain.identity`.
    """

    V1 = 1


@unique
class FailureCategory(StrEnum):
    """Exact canonical 14-item failure taxonomy from the governance baseline.

    Source of truth (all three fully aligned):
        REDI_LAB_RESEARCH_PLAN.md §11 lines 859–885,
        research-charter.md terminology table row "Failure Taxonomy",
        architecture.md failure references.
    """

    SPECIFICATION_AMBIGUITY = "specification_ambiguity"
    CONTEXT_INSUFFICIENCY = "context_insufficiency"
    CONTEXT_CONTAMINATION = "context_contamination"
    CAPABILITY_INSUFFICIENCY = "capability_insufficiency"
    TOOL_FAILURE = "tool_failure"
    ENVIRONMENT_FAILURE = "environment_failure"
    DEPENDENCY_MISMATCH = "dependency_mismatch"
    SCOPE_VIOLATION = "scope_violation"
    DETERMINISTIC_TEST_FAILURE = "deterministic_test_failure"
    REVIEWER_REJECTION = "reviewer_rejection"
    INTEGRATION_CONFLICT = "integration_conflict"
    SECURITY_VIOLATION = "security_violation"
    TIMEOUT_OR_BUDGET_EXHAUSTION = "timeout_or_budget_exhaustion"
    ORCHESTRATOR_PLANNING_ERROR = "orchestrator_planning_error"

    @classmethod
    def _missing_(cls, value: object) -> "FailureCategory | None":  # pragma: no cover - safety
        return None


@unique
class GateStatus(StrEnum):
    """GateResult pass/fail/blocked/error semantics."""

    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"
    ERROR = "error"


@unique
class GateType(StrEnum):
    """Whether a GateResult is a deterministic verification or model-based judgment."""

    DETERMINISTIC = "deterministic"
    MODEL_BASED = "model_based"
    MIXED = "mixed"


@unique
class LifecycleEventType(StrEnum):
    """Exact 18-event canonical lifecycle vocabulary from architecture §5.1.

    Sources: architecture.md §5.1 event tables 4 × 18 = 72 cells enumerated;
        naming and exact count (18) must match byte-for-byte.
    """

    PRODUCT_ACCEPTED = "PRODUCT_ACCEPTED"
    PLAN_COMPILED = "PLAN_COMPILED"
    NODE_READY = "NODE_READY"
    WORKER_STARTED = "WORKER_STARTED"
    WORKER_COMPLETED = "WORKER_COMPLETED"
    GATE_PASSED = "GATE_PASSED"
    GATE_FAILED = "GATE_FAILED"
    RETRY_REQUESTED = "RETRY_REQUESTED"
    CONTEXT_REBUILD_REQUIRED = "CONTEXT_REBUILD_REQUIRED"
    WORKER_ESCALATION_REQUIRED = "WORKER_ESCALATION_REQUIRED"
    ARCHITECTURAL_DECISION_REQUIRED = "ARCHITECTURAL_DECISION_REQUIRED"
    NODE_COMPLETED = "NODE_COMPLETED"
    MILESTONE_COMPLETED = "MILESTONE_COMPLETED"
    INTEGRATION_FAILED = "INTEGRATION_FAILED"
    PRODUCT_VERIFIED = "PRODUCT_VERIFIED"
    RECIPE_CANDIDATE_CREATED = "RECIPE_CANDIDATE_CREATED"
    RECIPE_PROMOTED = "RECIPE_PROMOTED"
    DRIFT_DETECTED = "DRIFT_DETECTED"

    @classmethod
    def known_count(cls) -> int:  # pragma: no cover - trivial
        return len(cls)


@unique
class Mode(StrEnum):
    """Four-tier factory operating modes — research-charter terminology table."""

    PRODUCTION = "production"
    WARM = "warm"
    EXPLORATION = "exploration"
    DRIFT = "drift"


@unique
class RecipeRung(StrEnum):
    """4-rung recipe promotion ladder from experiment-policy §19.6."""

    CANDIDATE = "candidate"
    SHADOW = "shadow"
    CANARY = "canary"
    VALIDATED = "validated"


@unique
class RiskTier(StrEnum):
    """Risk / consequence classification used by ProductSpec, experiments, replay."""

    SYNTHETIC = "synthetic"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@unique
class RunStatus(StrEnum):
    """Normalized terminal + transient node / run statuses."""

    PENDING = "pending"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


@unique
class Severity(StrEnum):
    """Evidence / Failure severity classification."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@unique
class EscalationLevel(StrEnum):
    """Ordered retry → escalate ladder (research-charter terminology table)."""

    RETRY_SAME = "retry_same"
    CONTEXT_REBUILD = "context_rebuild"
    WORKER_ESCALATION = "worker_escalation"
    ARCHITECTURAL_DECISION = "architectural_decision"


# Backwards-compatible alias for callers that imported Status before REDI-01 rename.
# Intentionally undocumented; new code must use RunStatus.
# Note: Enum aliases are NOT separate members; use RunStatus instead.
Status = RunStatus  # type: ignore[assignment]


__all__ = [
    "Enum",
    "IntEnum",
    "StrEnum",
    "unique",
    "SchemaVersion",
    "FailureCategory",
    "GateStatus",
    "GateType",
    "LifecycleEventType",
    "Mode",
    "RecipeRung",
    "RiskTier",
    "RunStatus",
    "Severity",
    "EscalationLevel",
]
