"""REDI-07A deterministic cold-start exploration foundation.

This module is deliberately rule based.  It does not perform recipe lookup,
learned routing, model planning, promotion, or network/provider calls.  It
turns an explicitly recipe-less ProductSpec into a conservative ProductDAG,
executes bounded fake-worker exploration through the REDI-06 executor, and
induces exactly one immutable ProductRecipe candidate after PRODUCT_VERIFIED.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .dag_scheduler import (
    NodeRunState,
    canonical_replay_view,
    dag_snapshot,
    product_dag_hash,
    replay_dag_run,
)
from .domain import (
    AcceptanceCriterion,
    ArtifactRef,
    Budget,
    DagDependency,
    DagNode,
    EvidenceFieldRef,
    EvidenceId,
    EventId,
    FactoryRun,
    FactoryRunId,
    FailureCategory,
    FailureId,
    FailureRecord,
    GateResult,
    GateResultId,
    GateStatus,
    GateType,
    LifecycleEvent,
    LifecycleEventType,
    Mode,
    PoolEntry,
    ProductClass,
    ProductClassId,
    ProductDAG,
    ProductId,
    ProductRecipe,
    ProductSpec,
    RecipeId,
    RecipeRung,
    Requirement,
    RiskTier,
    RunEvidence,
    RunStatus,
    Severity,
    StationContract,
    StationId,
    TaskContract,
    TaskId,
    Timing,
    TokenUsage,
    ValidityCondition,
    WorkerId,
    WorkerPool,
    WorkerProfile,
    domain_from_json,
    domain_to_json,
)
from .factory.isolation import IsolatedWorkspace, apply_patches, baseline_file_hashes
from .gates import DeterministicGateEngine, GateSpec
from .parallel_dag_executor import (
    NodeWorkspaceHandle,
    ParallelDagExecutor,
    ParallelDagRunResult,
    WorkerExecutionResult,
)
from .persistence import EvidenceLedger, FactoryRunRepository, bootstrap_schema
from .replay import ReplayReader
from .runtime.contracts import PatchEdit, ReviewerVerdict, WorkerRunner
from .runtime.fake_worker import FakeScriptedWorker
from .worker_selection import pool_snapshot, pool_snapshot_hash, resolve_worker


COLD_START_UNKNOWN_PRODUCT = "COLD_START_UNKNOWN_PRODUCT"
COLD_START_REASON_NO_RECIPE_SUPPLIED = "no_recipe_supplied_by_caller"
PLANNER_POLICY_ID = "cold_start_conservative_planner_v0"
PLANNER_POLICY_VERSION = 1
GATE_POLICY_ID = "cold_start_strict_gates_v0"
GATE_POLICY_VERSION = 1
EXPLORATION_POLICY_ID = "cold_start_deterministic_exploration_v0"
EXPLORATION_POLICY_VERSION = 1
RECIPE_CANDIDATE_POLICY_ID = "verified_run_recipe_induction_v0"
RECIPE_CANDIDATE_POLICY_VERSION = 1
CONTEXT_STRATEGY_ID = "selected_v0"
CONTEXT_BUDGET_BYTES = 64 * 1024

_REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROLLED_FIXTURE_ROOT = _REPO_ROOT / "experiments" / "fixtures" / "cold_start_inventory"
CONTROLLED_HIDDEN_ROOT = _REPO_ROOT / "experiments" / "fixtures" / "cold_start_inventory_hidden_tests"


class ColdStartSelectionError(ValueError):
    """Cold-start was requested without satisfying its explicit trigger."""


class ColdStartPlanningError(ValueError):
    """ProductSpec/station inputs cannot produce a bounded V0 plan."""


class CandidateGenerationError(ValueError):
    """The source run is not complete, verified, or evidence-valid."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "value"):
        return _canonical(value.value)
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_canonical(item) for item in value]
    return str(value)


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        _canonical(value), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _requirement_value(requirement: Any) -> dict[str, Any]:
    if isinstance(requirement, Requirement):
        return {
            "text": requirement.text,
            "kind": requirement.kind,
            "reference": requirement.reference,
            "known": requirement.known,
            "metadata": _canonical(requirement.metadata),
        }
    return {"text": str(requirement), "kind": "constraint", "known": True}


def _criterion_value(criterion: Any) -> dict[str, Any]:
    if isinstance(criterion, AcceptanceCriterion):
        return {
            "description": criterion.description,
            "criterion_type": criterion.criterion_type,
            "reference": criterion.reference,
            "evidence_needed": criterion.evidence_needed,
            "metadata": _canonical(criterion.metadata),
        }
    return {"description": str(criterion), "criterion_type": "deterministic"}


@dataclass(frozen=True)
class ProductSignature:
    body: Mapping[str, Any]
    sha256: str
    policy_id: str = "canonical_product_signature_v0"
    policy_version: int = 1

    def to_evidence(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "body": _canonical(self.body),
            "sha256": self.sha256,
        }


def derive_product_signature(
    product: ProductSpec,
    product_class: ProductClass | None = None,
) -> ProductSignature:
    """Derive a bounded semantic signature without opaque run/product ids."""

    allowed_metadata = (
        "product_type",
        "required_capabilities",
        "interface_shape",
        "technology_constraints",
        "runtime_constraints",
        "verification_requirements",
        "cold_start_components",
        "integration",
    )
    metadata = {
        key: _canonical(product.metadata[key])
        for key in allowed_metadata
        if key in (product.metadata or {})
    }
    class_value: dict[str, Any]
    if product_class is not None:
        class_value = {
            "id": str(product_class.id),
            "name": product_class.name,
            "version": int(product_class.version),
            "signature": _canonical(product_class.signature),
        }
    else:
        class_value = {
            "id": str(product.product_class_id) if product.product_class_id else None,
            "type": metadata.get("product_type"),
        }
    body = {
        "requested_outcome": product.requested_outcome.strip(),
        "requirements": [_requirement_value(item) for item in product.requirements],
        "constraints": [_requirement_value(item) for item in product.constraints],
        "acceptance_criteria": [
            _criterion_value(item) for item in product.acceptance_criteria
        ],
        "unresolved_requirements": sorted(str(item) for item in product.unresolved_requirements),
        "risk_tier": product.risk_tier.value if product.risk_tier else None,
        "requested_artifacts": sorted(str(item) for item in product.requested_artifacts),
        "product_class": class_value,
        "bounded_contract_metadata": metadata,
    }
    return ProductSignature(body=body, sha256=_canonical_hash(body))


def derive_product_class(product: ProductSpec, signature: ProductSignature) -> ProductClass:
    product_type = str((product.metadata or {}).get("product_type") or "unknown_product")
    return ProductClass(
        id=ProductClassId(f"product_class_cold_{signature.sha256[:24]}"),
        name=product_type,
        description="Deterministically derived cold-start product class.",
        signature=dict(signature.body),
        compatible_recipe_ids=(),
        classification_evidence=(),
        version=1,
        metadata={
            "derivation_policy_id": signature.policy_id,
            "derivation_policy_version": signature.policy_version,
            "signature_sha256": signature.sha256,
        },
    )


@dataclass(frozen=True)
class ColdStartSelection:
    mode: str
    factory_mode: Mode
    reason: str
    recipe_lookup_result: str
    policy_id: str
    policy_version: int

    def to_evidence(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "factory_mode": self.factory_mode.value,
            "reason": self.reason,
            "recipe_lookup_result": self.recipe_lookup_result,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
        }


def select_cold_start_mode(
    *,
    known_recipe: ProductRecipe | None,
    reason: str,
) -> ColdStartSelection:
    """Select cold-start only from an explicit recipe-less caller decision."""

    if known_recipe is not None:
        raise ColdStartSelectionError(
            "COLD_START_UNKNOWN_PRODUCT cannot be selected when a recipe is supplied"
        )
    if reason != COLD_START_REASON_NO_RECIPE_SUPPLIED:
        raise ColdStartSelectionError("an explicit governed cold-start reason is required")
    return ColdStartSelection(
        mode=COLD_START_UNKNOWN_PRODUCT,
        factory_mode=Mode.EXPLORATION,
        reason=reason,
        recipe_lookup_result="none",
        policy_id=EXPLORATION_POLICY_ID,
        policy_version=EXPLORATION_POLICY_VERSION,
    )


@dataclass(frozen=True)
class ExplorationBudget:
    max_candidate_workers_per_node: int = 2
    max_attempts_per_worker: int = 1
    max_total_attempts_per_node: int = 2
    max_total_attempts_per_run: int = 5
    max_tokens_per_run: int | None = None
    max_cost_minor_units_per_run: int | None = None

    def __post_init__(self) -> None:
        for name in (
            "max_candidate_workers_per_node",
            "max_attempts_per_worker",
            "max_total_attempts_per_node",
            "max_total_attempts_per_run",
        ):
            if int(getattr(self, name)) < 1:
                raise ValueError(f"{name} must be >= 1")

    def to_evidence(self) -> dict[str, Any]:
        return {
            "max_candidate_workers_per_node": self.max_candidate_workers_per_node,
            "max_attempts_per_worker": self.max_attempts_per_worker,
            "max_total_attempts_per_node": self.max_total_attempts_per_node,
            "max_total_attempts_per_run": self.max_total_attempts_per_run,
            "max_tokens_per_run": self.max_tokens_per_run,
            "max_cost_minor_units_per_run": self.max_cost_minor_units_per_run,
        }


@dataclass(frozen=True)
class ColdStartPolicy:
    planner_policy_id: str = PLANNER_POLICY_ID
    planner_policy_version: int = PLANNER_POLICY_VERSION
    gate_policy_id: str = GATE_POLICY_ID
    gate_policy_version: int = GATE_POLICY_VERSION
    exploration_policy_id: str = EXPLORATION_POLICY_ID
    exploration_policy_version: int = EXPLORATION_POLICY_VERSION
    context_strategy_id: str = CONTEXT_STRATEGY_ID
    context_budget_bytes: int = CONTEXT_BUDGET_BYTES
    max_components: int = 4
    budget: ExplorationBudget = field(default_factory=ExplorationBudget)

    def gate_policy_evidence(self) -> dict[str, Any]:
        return {
            "policy_id": self.gate_policy_id,
            "policy_version": self.gate_policy_version,
            "mandatory_gates": [
                "allowed_file_change_scope",
                "python_syntax_compile",
                "public_test_execution",
                "hidden_test_execution",
                "integration_verification",
                "independent_final_reviewer",
            ],
            "weakening_allowed_for_worker_capability": False,
        }

    def exploration_evidence(self) -> dict[str, Any]:
        return {
            "policy_id": self.exploration_policy_id,
            "policy_version": self.exploration_policy_version,
            "selection": "ordered_eligible_candidates_then_failure_specific_action",
            "learned_routing": False,
            "budgets": self.budget.to_evidence(),
            "failure_actions": failure_action_table(),
        }


@dataclass(frozen=True)
class ColdStartPlan:
    dag: ProductDAG
    tasks_by_node: Mapping[str, TaskContract]
    stations_by_node: Mapping[str, StationContract]
    required_handoffs_by_node: Mapping[str, Sequence[str]]
    produced_handoffs_by_node: Mapping[str, Sequence[str]]
    produced_handoff_types_by_id: Mapping[str, Sequence[str]]
    components_by_node: Mapping[str, Mapping[str, Any]]
    integration_node_id: str
    policy_evidence: Mapping[str, Any]


def _station_for_role(
    stations: Sequence[StationContract], *, reviewer: bool
) -> StationContract:
    candidates = []
    for station in stations:
        role = station.role.lower()
        is_reviewer = "review" in role or "integration" in role
        if is_reviewer == reviewer:
            candidates.append(station)
    if not candidates:
        label = "integration/reviewer" if reviewer else "implementation"
        raise ColdStartPlanningError(f"no {label} StationContract available")
    return sorted(candidates, key=lambda station: str(station.id))[0]


def plan_cold_start_v0(
    *,
    product: ProductSpec,
    product_class: ProductClass,
    signature: ProductSignature,
    stations: Sequence[StationContract],
    policy: ColdStartPolicy,
) -> ColdStartPlan:
    """Compile a conservative deterministic A+B→review graph from ProductSpec."""

    raw_components = list((product.metadata or {}).get("cold_start_components") or ())
    if not raw_components or len(raw_components) > policy.max_components:
        raise ColdStartPlanningError(
            f"cold_start_components must contain 1..{policy.max_components} bounded entries"
        )
    normalized: list[dict[str, Any]] = []
    keys: set[str] = set()
    for raw in raw_components:
        if not isinstance(raw, Mapping):
            raise ColdStartPlanningError("each cold-start component must be a mapping")
        key = str(raw.get("key") or "").strip().lower()
        if not key or not key.replace("_", "").isalnum() or key in keys:
            raise ColdStartPlanningError(f"invalid or duplicate component key {key!r}")
        keys.add(key)
        selected_paths = tuple(sorted(str(path) for path in (raw.get("selected_paths") or ())))
        allowed_scope = tuple(sorted(str(path) for path in (raw.get("allowed_scope") or ())))
        if not selected_paths or not allowed_scope:
            raise ColdStartPlanningError(f"component {key!r} requires selected_paths and allowed_scope")
        normalized.append(
            {
                "key": key,
                "goal": str(raw.get("goal") or f"Implement component {key}"),
                "selected_paths": selected_paths,
                "allowed_scope": allowed_scope,
                "public_test": str(raw.get("public_test") or ""),
                "hidden_test": str(raw.get("hidden_test") or ""),
                "handoff_id": str(raw.get("handoff_id") or f"handoff_{key}"),
                "handoff_type": str(raw.get("handoff_type") or f"{key}.implementation.v1"),
            }
        )
    normalized.sort(key=lambda item: item["key"])
    implementation_station = _station_for_role(stations, reviewer=False)
    review_station = _station_for_role(stations, reviewer=True)
    nodes: list[DagNode] = []
    tasks: dict[str, TaskContract] = {}
    station_map: dict[str, StationContract] = {}
    produced: dict[str, Sequence[str]] = {}
    produced_types: dict[str, Sequence[str]] = {}
    components_by_node: dict[str, Mapping[str, Any]] = {}
    prefix = signature.sha256[:16]
    for item in normalized:
        node_id = f"implement_{item['key']}"
        task = TaskContract(
            id=TaskId(f"task_cold_{prefix}_{item['key']}"),
            goal=item["goal"],
            station_id=implementation_station.id,
            role_requirement=implementation_station.role,
            allowed_scope=item["allowed_scope"],
            forbidden_scope=(".git", "secrets", "hidden_tests"),
            acceptance_criteria=tuple(product.acceptance_criteria),
            verification_requirements=(
                "scope",
                "syntax",
                "public_deterministic_tests",
                "hidden_deterministic_tests",
            ),
            budget=Budget(
                retries=policy.budget.max_total_attempts_per_node - 1,
                tokens_total=policy.budget.max_tokens_per_run,
                metadata={"exploration_budget": policy.budget.to_evidence()},
            ),
            metadata={"task_class": "cold_start_component", "component_key": item["key"]},
        )
        nodes.append(
            DagNode(
                node_id=node_id,
                task_id=str(task.id),
                station_id=str(implementation_station.id),
                role_hint="implementation",
                parallel_group="cold_start_components",
                metadata={
                    "selected_paths": list(item["selected_paths"]),
                    "produces_handoff_ids": [item["handoff_id"]],
                    "produces_handoff_types": [item["handoff_type"]],
                    "planner_policy_id": policy.planner_policy_id,
                    "planner_policy_version": policy.planner_policy_version,
                    "gate_policy_id": policy.gate_policy_id,
                    "gate_policy_version": policy.gate_policy_version,
                },
            )
        )
        tasks[node_id] = task
        station_map[node_id] = implementation_station
        produced[node_id] = (item["handoff_id"],)
        produced_types[item["handoff_id"]] = (item["handoff_type"],)
        components_by_node[node_id] = item

    integration = dict((product.metadata or {}).get("integration") or {})
    integration_node = "integration_review"
    integration_task = TaskContract(
        id=TaskId(f"task_cold_{prefix}_integration_review"),
        goal=str(integration.get("goal") or "Integrate all component handoffs and independently verify"),
        station_id=review_station.id,
        role_requirement=review_station.role,
        allowed_scope=tuple(sorted(str(path) for path in (integration.get("allowed_scope") or ("inventory_math", "tests")))),
        forbidden_scope=(".git", "secrets", "hidden_tests"),
        acceptance_criteria=tuple(product.acceptance_criteria),
        prerequisites=tuple(tasks[node].id for node in sorted(tasks)),
        verification_requirements=(
            "integration_verification",
            "independent_final_reviewer",
        ),
        metadata={"task_class": "cold_start_integration_review"},
    )
    handoff_ids = tuple(item["handoff_id"] for item in normalized)
    handoff_types = tuple(item["handoff_type"] for item in normalized)
    nodes.append(
        DagNode(
            node_id=integration_node,
            task_id=str(integration_task.id),
            station_id=str(review_station.id),
            role_hint="integration_reviewer",
            metadata={
                "selected_paths": sorted(str(path) for path in (integration.get("selected_paths") or ("inventory_math/service.py", "tests/test_integration_public.py"))),
                "requires_handoff_ids": list(handoff_ids),
                "requires_handoff_types": list(handoff_types),
                "planner_policy_id": policy.planner_policy_id,
                "planner_policy_version": policy.planner_policy_version,
                "gate_policy_id": policy.gate_policy_id,
                "gate_policy_version": policy.gate_policy_version,
                "independent_reviewer_required": True,
            },
        )
    )
    tasks[integration_node] = integration_task
    station_map[integration_node] = review_station
    required = {integration_node: handoff_ids}
    dependencies = tuple(
        DagDependency(upstream=node_id, downstream=integration_node, kind="hard")
        for node_id in sorted(components_by_node)
    )
    graph_id = f"run_cold_v0_{signature.sha256[:32]}"
    dag = ProductDAG(
        id=graph_id,
        product_id=product.id,
        revision=1,
        nodes=tuple(nodes),
        dependencies=dependencies,
        metadata={
            "planner_policy_id": policy.planner_policy_id,
            "planner_policy_version": policy.planner_policy_version,
            "product_signature_sha256": signature.sha256,
            "product_class_id": str(product_class.id),
            "conservative": True,
            "independent_review_required": True,
        },
    )
    policy_evidence = {
        "planner_policy_id": policy.planner_policy_id,
        "planner_policy_version": policy.planner_policy_version,
        "gate_policy": policy.gate_policy_evidence(),
        "exploration_policy": policy.exploration_evidence(),
        "no_recipe_dependency": True,
        "no_provider_or_model_identity": True,
        "rule_based": True,
    }
    return ColdStartPlan(
        dag=dag,
        tasks_by_node=tasks,
        stations_by_node=station_map,
        required_handoffs_by_node=required,
        produced_handoffs_by_node=produced,
        produced_handoff_types_by_id=produced_types,
        components_by_node=components_by_node,
        integration_node_id=integration_node,
        policy_evidence=policy_evidence,
    )


_FAILURE_ACTIONS: dict[FailureCategory, str] = {
    FailureCategory.DETERMINISTIC_TEST_FAILURE: "worker_escalation",
    FailureCategory.CONTEXT_INSUFFICIENCY: "context_rebuild_then_same_or_next_worker",
    FailureCategory.CAPABILITY_INSUFFICIENCY: "worker_escalation",
    FailureCategory.TOOL_FAILURE: "orchestrator_tool_retry_no_capability_penalty",
    FailureCategory.ENVIRONMENT_FAILURE: "orchestrator_environment_retry_no_capability_penalty",
    FailureCategory.SCOPE_VIOLATION: "fail_closed_scope_violation",
    FailureCategory.INTEGRATION_CONFLICT: "integration_orchestrator_escalation",
    FailureCategory.TIMEOUT_OR_BUDGET_EXHAUSTION: "stop_budget_exhausted",
}


def failure_action_table() -> dict[str, str]:
    return {category.value: action for category, action in _FAILURE_ACTIONS.items()}


def escalation_action(category: FailureCategory) -> str:
    return _FAILURE_ACTIONS.get(category, "worker_escalation")


def classify_gate_failure(gate: GateResult) -> FailureCategory:
    metadata_category = (gate.metadata or {}).get("failure_category")
    if metadata_category:
        try:
            return FailureCategory(str(metadata_category))
        except ValueError:
            pass
    if gate.gate_identity == "allowed_file_change_scope":
        return FailureCategory.SCOPE_VIOLATION
    if gate.gate_identity in {
        "python_syntax_compile",
        "public_test_execution",
        "hidden_test_execution",
    }:
        return FailureCategory.DETERMINISTIC_TEST_FAILURE
    if gate.gate_identity == "integration_verification":
        return FailureCategory.INTEGRATION_CONFLICT
    if gate.gate_identity == "independent_final_reviewer":
        return FailureCategory.REVIEWER_REJECTION
    return FailureCategory.TOOL_FAILURE


class _RunAttemptCounter:
    def __init__(self, limit: int) -> None:
        self.limit = int(limit)
        self.total = 0
        self._lock = threading.Lock()

    def claim(self) -> bool:
        with self._lock:
            if self.total >= self.limit:
                return False
            self.total += 1
            return True


def _gate_summary(gate: GateResult) -> dict[str, Any]:
    public_identity = (
        "private_deterministic_verification"
        if gate.gate_identity == "hidden_test_execution"
        else gate.gate_identity
    )
    return {
        "gate_id": str(gate.id),
        "gate_identity": public_identity,
        "status": gate.status.value,
        "failure_reasons_count": len(gate.failure_reasons),
        "policy_id": (gate.metadata or {}).get("gate_policy_id"),
        "policy_version": (gate.metadata or {}).get("gate_policy_version"),
    }


def _profile_snapshot(profile: WorkerProfile, rank: int) -> dict[str, Any]:
    return {
        "worker_id": str(profile.id),
        "display_name": profile.display_name,
        "profile_version": profile.version,
        "preference_rank": int(rank),
        "capability_tags": sorted(str(tag) for tag in profile.capability_tags),
        "availability_status": profile.availability_status,
    }


def _candidate_inventory(
    *,
    pool: WorkerPool,
    station: StationContract,
    task: TaskContract,
    runners: Mapping[str, WorkerRunner],
    budget: ExplorationBudget,
) -> list[dict[str, Any]]:
    ordered = sorted(pool.entries, key=lambda entry: (entry.preference_rank, str(entry.profile.id)))
    inventory: list[dict[str, Any]] = []
    for index, entry in enumerate(ordered):
        profile = entry.profile
        reasons: list[str] = []
        if index >= budget.max_candidate_workers_per_node:
            reasons.append("rejected_by_max_candidate_workers_per_node")
        if profile.availability_status not in (None, "available"):
            reasons.append(f"availability_status={profile.availability_status}")
        if profile.station_eligibility and str(station.id) not in {
            str(item) for item in profile.station_eligibility
        }:
            reasons.append("not_eligible_for_station")
        task_class = (task.metadata or {}).get("task_class")
        if station.applicable_task_classes and task_class not in station.applicable_task_classes:
            reasons.append("task_class_mismatch")
        if str(profile.id) not in runners:
            reasons.append("no_runner_binding")
        inventory.append(
            _profile_snapshot(profile, entry.preference_rank)
            | {
                "considered": index < budget.max_candidate_workers_per_node,
                "eligible": not reasons,
                "rejected_or_ineligible_reasons": reasons,
            }
        )
    return inventory


def _with_gate_policy(gate: GateResult, policy: ColdStartPolicy) -> GateResult:
    gate.metadata = dict(gate.metadata or {}) | {
        "gate_policy_id": policy.gate_policy_id,
        "gate_policy_version": policy.gate_policy_version,
    }
    gate.verifier_identity = f"{policy.gate_policy_id}@{policy.gate_policy_version}"
    return gate


def _scope_failure_gate(
    *, node_id: str, task: TaskContract, reasons: Sequence[str], policy: ColdStartPolicy
) -> GateResult:
    return GateResult(
        gate_identity="allowed_file_change_scope",
        gate_type=GateType.DETERMINISTIC,
        target_node_id=node_id,
        target_task_id=task.id,
        criteria=(AcceptanceCriterion(description="all edits remain in task scope"),),
        status=GateStatus.FAIL,
        verifier_type="deterministic_code",
        verifier_identity=f"{policy.gate_policy_id}@{policy.gate_policy_version}",
        failure_reasons=tuple(str(reason) for reason in reasons),
        metadata={
            "mandatory": True,
            "gate_policy_id": policy.gate_policy_id,
            "gate_policy_version": policy.gate_policy_version,
        },
    )


@dataclass
class _ExploringComponentNode:
    node_id: str
    product: ProductSpec
    task: TaskContract
    station: StationContract
    component: Mapping[str, Any]
    pool: WorkerPool
    runners: Mapping[str, WorkerRunner]
    policy: ColdStartPolicy
    fixture_root: Path
    hidden_root: Path
    attempt_counter: _RunAttemptCounter
    barrier: threading.Barrier | None = None

    def run_node(self, *, handle: NodeWorkspaceHandle) -> WorkerExecutionResult:
        if self.barrier is not None:
            self.barrier.wait(timeout=10)
        inventory = _candidate_inventory(
            pool=self.pool,
            station=self.station,
            task=self.task,
            runners=self.runners,
            budget=self.policy.budget,
        )
        bounded_entries = tuple(
            sorted(self.pool.entries, key=lambda entry: (entry.preference_rank, str(entry.profile.id)))[
                : self.policy.budget.max_candidate_workers_per_node
            ]
        )
        bounded_pool = WorkerPool(
            id=self.pool.id,
            station_id=self.pool.station_id,
            role=self.pool.role,
            entries=bounded_entries,
            selection_policy_id=self.pool.selection_policy_id,
            metadata=dict(self.pool.metadata),
            created_at=self.pool.created_at,
        )
        snapshot = pool_snapshot(pool=bounded_pool, station=self.station, task=self.task)
        snapshot_hash = pool_snapshot_hash(snapshot)
        per_worker_attempts: dict[str, int] = {}
        excluded: set[str] = set()
        attempt_records: list[RunEvidence] = []
        failure_records: list[FailureRecord] = []
        gate_records: list[GateResult] = []
        trajectory: list[dict[str, Any]] = []
        previous_worker_id: str | None = None
        last_category: FailureCategory | None = None
        node_attempt = 0
        while node_attempt < self.policy.budget.max_total_attempts_per_node:
            if not self.attempt_counter.claim():
                return self._budget_failure(
                    handle=handle,
                    inventory=inventory,
                    snapshot=snapshot,
                    snapshot_hash=snapshot_hash,
                    trajectory=trajectory,
                    attempts=attempt_records,
                    gates=gate_records,
                    failures=failure_records,
                    reason="max_total_attempts_per_run exhausted",
                )
            for worker_id, count in per_worker_attempts.items():
                if count >= self.policy.budget.max_attempts_per_worker:
                    excluded.add(worker_id)
            selection = resolve_worker(
                pool=bounded_pool,
                runners=self.runners,
                station=self.station,
                task=self.task,
                exclude_worker_ids=tuple(sorted(excluded)),
                attempt_index=node_attempt,
                previous_worker_id=previous_worker_id,
            )
            if selection is None:
                return self._budget_failure(
                    handle=handle,
                    inventory=inventory,
                    snapshot=snapshot,
                    snapshot_hash=snapshot_hash,
                    trajectory=trajectory,
                    attempts=attempt_records,
                    gates=gate_records,
                    failures=failure_records,
                    reason="candidate/attempt budget exhausted before success",
                )
            worker_id = str(selection.profile.id)
            per_worker_attempts[worker_id] = per_worker_attempts.get(worker_id, 0) + 1
            selection_evidence = selection.to_evidence() | {
                "pool_id": str(bounded_pool.id),
                "pool_snapshot_hash": snapshot_hash,
                "failure_trigger_category": last_category.value if last_category else None,
                "failure_specific_reason": (
                    escalation_action(last_category) if last_category else "initial_ordered_candidate"
                ),
            }
            started = time.perf_counter()
            result = selection.runner.run_node(
                task_id=self.task.id,
                station_id=self.station.id,
                worker_id=selection.profile.id,
                context={
                    "attempt_index": per_worker_attempts[worker_id] - 1,
                    "case_id": self.node_id,
                    "context_package_hash": handle.context_package.package_hash,
                    "cold_start_mode": COLD_START_UNKNOWN_PRODUCT,
                },
                workspace_root=handle.root,
                station_role=self.station.role,
            )
            workspace = IsolatedWorkspace(
                root=handle.root,
                fixture_root=self.fixture_root,
                allowed_scope_rel_prefixes=tuple(str(item) for item in self.task.allowed_scope),
                baseline_hashes=baseline_file_hashes(self.fixture_root),
            )
            applied, violations = apply_patches(
                workspace,
                result.patches,
                allowed_scope=self.task.allowed_scope,
            )
            if not applied:
                gates = [
                    _scope_failure_gate(
                        node_id=self.node_id,
                        task=self.task,
                        reasons=[str(item) for item in violations],
                        policy=self.policy,
                    )
                ]
            else:
                gate_spec = GateSpec(
                    public_test_cmd=[
                        sys.executable,
                        "-m",
                        "pytest",
                        "-q",
                        "--no-header",
                        "-p",
                        "no:cacheprovider",
                        str(self.component["public_test"]),
                    ],
                    hidden_test_cmd=[
                        sys.executable,
                        "-m",
                        "pytest",
                        "-q",
                        "--no-header",
                        "-p",
                        "no:cacheprovider",
                        str(self.hidden_root / self.component["hidden_test"]),
                    ],
                    hidden_test_cwd=self.hidden_root,
                    allowed_scope_prefixes=tuple(str(item) for item in self.task.allowed_scope),
                    baseline_hashes=baseline_file_hashes(self.fixture_root),
                    per_gate_timeout_s=20.0,
                )
                gates = DeterministicGateEngine(gate_spec).run_all(
                    handle.root,
                    target_node_id=self.node_id,
                    target_task_id=self.task.id,
                )
                gates = [_with_gate_policy(gate, self.policy) for gate in gates]
            elapsed = round(time.perf_counter() - started, 6)
            gate_records.extend(gates)
            failed_gate = next((gate for gate in gates if gate.status != GateStatus.PASS), None)
            category = classify_gate_failure(failed_gate) if failed_gate else None
            action = escalation_action(category) if category else "accept_verified_attempt"
            attempt_evidence = RunEvidence(
                factory_run_id=FactoryRunId(handle.factory_run_id) if handle.factory_run_id else None,
                product_id=self.product.id,
                product_class_id=self.product.product_class_id,
                node_id=self.node_id,
                station_id=self.station.id,
                task_class="cold_start_worker_attempt",
                worker_id=selection.profile.id,
                worker_version=selection.profile.version,
                token_usage=result.token_usage,
                timing=Timing(wall_clock_seconds=elapsed, compute_seconds=elapsed),
                artifacts_changed=tuple(
                    ArtifactRef(
                        ref_id=f"attempt_{self.node_id}_{node_attempt}_{index}",
                        kind="patch",
                        location=patch.rel_path,
                        sha256=patch.content_sha256,
                        source="output",
                    )
                    for index, patch in enumerate(result.patches)
                ),
                tests_executed=tuple(gate.gate_identity for gate in gates),
                gate_results=tuple(gate.id for gate in gates),
                retry_count=node_attempt,
                escalation=category is not None,
                final_status=RunStatus.FAILED if failed_gate else RunStatus.COMPLETED,
                failure_category=category,
                metadata={
                    "evidence_kind": "cold_start_attempt",
                    "attempt_index": node_attempt,
                    "worker_selection": selection_evidence,
                    "worker_profile": _profile_snapshot(selection.profile, selection.preference_rank),
                    "context_package_hash": handle.context_package.package_hash,
                    "context_strategy_id": handle.context_package.strategy_id,
                    "context_strategy_version": handle.context_package.strategy_version,
                    "gate_boundary": _gate_summary(failed_gate) if failed_gate else None,
                    "gate_summaries": [_gate_summary(gate) for gate in gates],
                    "failure_action": action,
                    "exploration_policy_id": self.policy.exploration_policy_id,
                    "exploration_policy_version": self.policy.exploration_policy_version,
                    "budget": self.policy.budget.to_evidence(),
                    "provider_calls": 0,
                    "network_calls": 0,
                },
            )
            attempt_records.append(attempt_evidence)
            trace = {
                "attempt_index": node_attempt,
                "worker_id": worker_id,
                "worker_profile_version": selection.profile.version,
                "selection": selection_evidence,
                "context_package_hash": handle.context_package.package_hash,
                "gate_summaries": [_gate_summary(gate) for gate in gates],
                "outcome": "failure" if failed_gate else "success",
                "failure_category": category.value if category else None,
                "action": action,
                "attempt_evidence_id": str(attempt_evidence.id),
            }
            trajectory.append(trace)
            if failed_gate is None:
                target_path = handle.root / str(self.component["selected_paths"][0])
                contract = dict(handle.handoff_output_contracts[self.component["handoff_id"]])
                descriptor = {
                    "handoff_id": self.component["handoff_id"],
                    "artifact_id": f"artifact::{self.node_id}::{self.component['handoff_id']}",
                    "path": target_path.relative_to(handle.root).as_posix(),
                    "sha256": hashlib.sha256(target_path.read_bytes()).hexdigest(),
                    "kind": "file",
                    "size_bytes": target_path.stat().st_size,
                    "producer_node_id": self.node_id,
                    "producer_station_id": contract["producer_station_id"],
                    "consumer_scope": contract["consumer_scope"],
                    "metadata": {"purpose": "cold-start verified component handoff"},
                }
                return WorkerExecutionResult(
                    node_id=self.node_id,
                    workspace_id=handle.workspace_id,
                    success=True,
                    produced_handoff_ids=(self.component["handoff_id"],),
                    produced_handoff_types_by_id={
                        self.component["handoff_id"]: (self.component["handoff_type"],)
                    },
                    artifacts=(descriptor,),
                    execution_evidence={
                        "cold_start_mode": COLD_START_UNKNOWN_PRODUCT,
                        "exploration_policy": self.policy.exploration_evidence(),
                        "candidate_inventory": inventory,
                        "pool_snapshot": snapshot,
                        "pool_snapshot_hash": snapshot_hash,
                        "attempt_trajectory": trajectory,
                        "selected_worker_id": worker_id,
                        "gates": [_gate_summary(gate) for gate in gates],
                        "tokens": (
                            {
                                "input": result.token_usage.input,
                                "output": result.token_usage.output,
                                "cached_input": result.token_usage.cached_input,
                            }
                            if result.token_usage else None
                        ),
                        "provider_calls": 0,
                        "network_calls": 0,
                    },
                    attempt_evidence=tuple(attempt_records),
                    gate_results=tuple(gate_records),
                    failure_records=tuple(failure_records),
                )
            failure = FailureRecord(
                category=category or FailureCategory.TOOL_FAILURE,
                origin="cold_start_worker_attempt",
                node_id=self.node_id,
                task_id=self.task.id,
                factory_run_id=FactoryRunId(handle.factory_run_id) if handle.factory_run_id else None,
                details={
                    "failed_gate_id": str(failed_gate.id),
                    "failed_gate_identity": failed_gate.gate_identity,
                    "failure_reasons_count": len(failed_gate.failure_reasons),
                    "attempt_index": node_attempt,
                },
                severity=Severity.ERROR,
                recoverable=action in {
                    "worker_escalation",
                    "context_rebuild_then_same_or_next_worker",
                    "orchestrator_tool_retry_no_capability_penalty",
                    "orchestrator_environment_retry_no_capability_penalty",
                },
                escalation_target=action,
                evidence_id=attempt_evidence.id,
                metadata={
                    "worker_id": worker_id,
                    "worker_profile_version": selection.profile.version,
                    "context_package_hash": handle.context_package.package_hash,
                    "gate_boundary": _gate_summary(failed_gate),
                    "retry_escalation_decision": action,
                    "policy_id": self.policy.exploration_policy_id,
                    "policy_version": self.policy.exploration_policy_version,
                    "not_capability_failure": category in {
                        FailureCategory.TOOL_FAILURE,
                        FailureCategory.ENVIRONMENT_FAILURE,
                    },
                },
            )
            failure_records.append(failure)
            trajectory[-1]["failure_record_id"] = str(failure.id)
            if action == "fail_closed_scope_violation":
                break
            if action == "worker_escalation":
                excluded.add(worker_id)
            previous_worker_id = worker_id
            last_category = category
            node_attempt += 1
        return self._budget_failure(
            handle=handle,
            inventory=inventory,
            snapshot=snapshot,
            snapshot_hash=snapshot_hash,
            trajectory=trajectory,
            attempts=attempt_records,
            gates=gate_records,
            failures=failure_records,
            reason="max_total_attempts_per_node exhausted",
        )

    def _budget_failure(
        self,
        *,
        handle: NodeWorkspaceHandle,
        inventory: list[dict[str, Any]],
        snapshot: Mapping[str, Any],
        snapshot_hash: str,
        trajectory: list[dict[str, Any]],
        attempts: list[RunEvidence],
        gates: list[GateResult],
        failures: list[FailureRecord],
        reason: str,
    ) -> WorkerExecutionResult:
        failure = FailureRecord(
            id=FailureId(f"failure_budget_{_canonical_hash([handle.factory_run_id, self.node_id, reason])[:24]}"),
            category=FailureCategory.TIMEOUT_OR_BUDGET_EXHAUSTION,
            origin="cold_start_exploration_budget",
            node_id=self.node_id,
            task_id=self.task.id,
            factory_run_id=FactoryRunId(handle.factory_run_id) if handle.factory_run_id else None,
            details={"reason": reason, "attempts_used": len(trajectory)},
            severity=Severity.ERROR,
            recoverable=False,
            escalation_target="stop_budget_exhausted",
            metadata={
                "context_package_hash": handle.context_package.package_hash,
                "budget": self.policy.budget.to_evidence(),
                "policy_id": self.policy.exploration_policy_id,
                "policy_version": self.policy.exploration_policy_version,
            },
        )
        failures.append(failure)
        trajectory.append(
            {
                "outcome": "failure",
                "failure_category": failure.category.value,
                "action": "stop_budget_exhausted",
                "failure_record_id": str(failure.id),
                "reason": reason,
            }
        )
        return WorkerExecutionResult(
            node_id=self.node_id,
            workspace_id=handle.workspace_id,
            success=False,
            error_type="ExplorationBudgetExhausted",
            error_message=reason,
            execution_evidence={
                "cold_start_mode": COLD_START_UNKNOWN_PRODUCT,
                "exploration_policy": self.policy.exploration_evidence(),
                "candidate_inventory": inventory,
                "pool_snapshot": dict(snapshot),
                "pool_snapshot_hash": snapshot_hash,
                "attempt_trajectory": trajectory,
                "provider_calls": 0,
                "network_calls": 0,
            },
            attempt_evidence=tuple(attempts),
            gate_results=tuple(gates),
            failure_records=tuple(failures),
        )


@dataclass
class _IndependentIntegrationReviewer:
    node_id: str
    product: ProductSpec
    task: TaskContract
    station: StationContract
    policy: ColdStartPolicy
    fixture_root: Path
    hidden_root: Path
    worker_id: WorkerId = WorkerId("worker_independent_reviewer_v0")
    calls: int = 0

    def run_node(self, *, handle: NodeWorkspaceHandle) -> WorkerExecutionResult:
        self.calls += 1
        started = time.perf_counter()
        merge_failures: list[str] = []
        for manifest in sorted(handle.upstream_handoffs, key=lambda item: str(item.id)):
            artifacts = list(manifest.produced_artifacts or ())
            if len(artifacts) != 1 or handle.retained_artifact_root is None:
                merge_failures.append(f"handoff {manifest.id} missing retained artifact")
                continue
            artifact = artifacts[0]
            retained_rel = (artifact.metadata or {}).get("retained_artifact_path")
            if not retained_rel:
                merge_failures.append(f"handoff {manifest.id} missing retained path")
                continue
            source = handle.retained_artifact_root / str(retained_rel)
            if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != artifact.sha256:
                merge_failures.append(f"handoff {manifest.id} retained artifact verification failed")
                continue
            target = handle.root / str(artifact.location)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

        gate_spec = GateSpec(
            public_test_cmd=[
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--no-header",
                "-p",
                "no:cacheprovider",
                "tests/",
            ],
            hidden_test_cmd=[
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--no-header",
                "-p",
                "no:cacheprovider",
                str(self.hidden_root),
            ],
            hidden_test_cwd=self.hidden_root,
            allowed_scope_prefixes=tuple(str(item) for item in self.task.allowed_scope),
            baseline_hashes=baseline_file_hashes(self.fixture_root),
            per_gate_timeout_s=20.0,
        )
        gates = [
            _with_gate_policy(gate, self.policy)
            for gate in DeterministicGateEngine(gate_spec).run_all(
                handle.root,
                target_node_id=self.node_id,
                target_task_id=self.task.id,
            )
        ]
        deterministic_pass = not merge_failures and all(gate.status == GateStatus.PASS for gate in gates)
        integration_gate = GateResult(
            gate_identity="integration_verification",
            gate_type=GateType.DETERMINISTIC,
            target_node_id=self.node_id,
            target_task_id=self.task.id,
            criteria=(AcceptanceCriterion(description="all typed component handoffs integrate and full tests pass"),),
            status=GateStatus.PASS if deterministic_pass else GateStatus.FAIL,
            verifier_type="deterministic_code",
            verifier_identity=f"{self.policy.gate_policy_id}@{self.policy.gate_policy_version}",
            failure_reasons=tuple(merge_failures or (() if deterministic_pass else ("mandatory deterministic gate failed",))),
            metadata={
                "mandatory": True,
                "gate_policy_id": self.policy.gate_policy_id,
                "gate_policy_version": self.policy.gate_policy_version,
                "handoff_ids": sorted(str(item.id) for item in handle.upstream_handoffs),
            },
        )
        gates.append(integration_gate)
        independent_pass = deterministic_pass and len(handle.upstream_handoffs) >= 2
        reviewer_gate = GateResult(
            gate_identity="independent_final_reviewer",
            gate_type=GateType.DETERMINISTIC,
            target_node_id=self.node_id,
            target_task_id=self.task.id,
            criteria=(AcceptanceCriterion(description="independent reviewer confirms integrated product"),),
            status=GateStatus.PASS if independent_pass else GateStatus.FAIL,
            verifier_type="deterministic_code",
            verifier_identity=str(self.worker_id),
            failure_reasons=() if independent_pass else ("independent review prerequisites not satisfied",),
            metadata={
                "mandatory": True,
                "independent_from_implementation_workers": True,
                "gate_policy_id": self.policy.gate_policy_id,
                "gate_policy_version": self.policy.gate_policy_version,
            },
        )
        gates.append(reviewer_gate)
        elapsed = round(time.perf_counter() - started, 6)
        failed_gate = next((gate for gate in gates if gate.status != GateStatus.PASS), None)
        category = classify_gate_failure(failed_gate) if failed_gate else None
        attempt = RunEvidence(
            factory_run_id=FactoryRunId(handle.factory_run_id) if handle.factory_run_id else None,
            product_id=self.product.id,
            product_class_id=self.product.product_class_id,
            node_id=self.node_id,
            station_id=self.station.id,
            task_class="cold_start_independent_integration_review",
            worker_id=self.worker_id,
            worker_version="1",
            timing=Timing(wall_clock_seconds=elapsed, compute_seconds=elapsed),
            tests_executed=tuple(gate.gate_identity for gate in gates),
            gate_results=tuple(gate.id for gate in gates),
            retry_count=0,
            escalation=failed_gate is not None,
            final_status=RunStatus.FAILED if failed_gate else RunStatus.COMPLETED,
            failure_category=category,
            metadata={
                "evidence_kind": "cold_start_independent_review",
                "context_package_hash": handle.context_package.package_hash,
                "gate_summaries": [_gate_summary(gate) for gate in gates],
                "final_verdict": ReviewerVerdict.PASS.value if independent_pass else ReviewerVerdict.FAIL.value,
                "provider_calls": 0,
                "network_calls": 0,
            },
        )
        failures: tuple[FailureRecord, ...] = ()
        if failed_gate is not None:
            failures = (
                FailureRecord(
                    category=category or FailureCategory.REVIEWER_REJECTION,
                    origin="cold_start_independent_reviewer",
                    node_id=self.node_id,
                    task_id=self.task.id,
                    factory_run_id=FactoryRunId(handle.factory_run_id) if handle.factory_run_id else None,
                    details={"failed_gate_id": str(failed_gate.id), "failed_gate_identity": failed_gate.gate_identity},
                    recoverable=False,
                    escalation_target=escalation_action(category or FailureCategory.REVIEWER_REJECTION),
                    evidence_id=attempt.id,
                    metadata={"context_package_hash": handle.context_package.package_hash},
                ),
            )
        return WorkerExecutionResult(
            node_id=self.node_id,
            workspace_id=handle.workspace_id,
            success=independent_pass,
            execution_evidence={
                "worker_selection": {
                    "policy_id": "fixed_independent_reviewer_v0",
                    "policy_version": 1,
                    "worker_id": str(self.worker_id),
                    "reason": "non-exploratory mandatory independent final reviewer",
                },
                "gates": [_gate_summary(gate) for gate in gates],
                "final_verdict": ReviewerVerdict.PASS.value if independent_pass else ReviewerVerdict.FAIL.value,
                "upstream_handoff_ids": sorted(str(item.id) for item in handle.upstream_handoffs),
                "provider_calls": 0,
                "network_calls": 0,
            },
            attempt_evidence=(attempt,),
            gate_results=tuple(gates),
            failure_records=failures,
            error_type=None if independent_pass else "IndependentReviewFailed",
            error_message=None if independent_pass else "integration/reviewer gates failed",
        )


_BAD_NORMALIZE = '''"""Weak candidate: intentionally incomplete normalization."""

def normalize_sku(value):
    return value.strip().upper()
'''

_GOOD_NORMALIZE = '''"""Deterministic inventory SKU normalization."""

from __future__ import annotations

import re


def normalize_sku(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("value must be str")
    normalized = re.sub(r"[\\s_]+", "-", value.strip().upper())
    normalized = re.sub(r"-+", "-", normalized).strip("-")
    if not normalized:
        raise ValueError("value must contain a SKU")
    return normalized
'''

_GOOD_REORDER = '''"""Deterministic replenishment quantity calculation."""

from __future__ import annotations


def reorder_quantity(on_hand: int, target: int) -> int:
    if isinstance(on_hand, bool) or isinstance(target, bool):
        raise TypeError("inventory values must be integers")
    if not isinstance(on_hand, int) or not isinstance(target, int):
        raise TypeError("inventory values must be integers")
    if on_hand < 0 or target < 0:
        raise ValueError("inventory values must be non-negative")
    return max(target - on_hand, 0)
'''


def controlled_product_spec(*, material_variant: str = "v1") -> ProductSpec:
    return ProductSpec(
        requested_outcome="Build a deterministic inventory replenishment package from two independent components.",
        requirements=(
            Requirement(
                text=(
                    "Normalize external SKU labels into a stable interface: require a string, "
                    "trim surrounding whitespace, uppercase letters, collapse each run of "
                    "whitespace, underscores, or hyphens to one hyphen, remove leading and "
                    "trailing hyphens, and reject labels that normalize to empty."
                ),
                kind="functional",
            ),
            Requirement(text="Calculate a non-negative reorder quantity.", kind="functional"),
            Requirement(text="Integrate both capabilities behind replenishment_order.", kind="functional"),
        ),
        constraints=(
            Requirement(text="Use the Python standard library only.", kind="constraint"),
            Requirement(text=f"Product contract material variant {material_variant}.", kind="constraint"),
        ),
        acceptance_criteria=(
            AcceptanceCriterion(description="Public deterministic tests pass."),
            AcceptanceCriterion(description="Hidden deterministic verification passes."),
            AcceptanceCriterion(description="Independent integration review passes."),
        ),
        unresolved_requirements=(),
        risk_tier=RiskTier.SYNTHETIC,
        requested_artifacts=("inventory_math package", "verification evidence"),
        metadata={
            "product_type": "python_inventory_package",
            "required_capabilities": ["sku_normalization", "reorder_calculation", "integration"],
            "interface_shape": {
                "normalize_sku": "str -> str",
                "reorder_quantity": "(int, int) -> int",
                "replenishment_order": "(str, int, int) -> mapping",
            },
            "technology_constraints": ["python", "stdlib_only"],
            "runtime_constraints": ["python>=3.11", "offline"],
            "verification_requirements": ["scope", "syntax", "public", "hidden", "integration", "independent_review"],
            "cold_start_components": [
                {
                    "key": "normalize",
                    "goal": "Implement deterministic SKU normalization.",
                    "selected_paths": ["inventory_math/normalize.py", "tests/test_normalize_public.py"],
                    "allowed_scope": ["inventory_math"],
                    "public_test": "tests/test_normalize_public.py",
                    "hidden_test": "test_normalize_hidden.py",
                    "handoff_id": "handoff_normalize",
                    "handoff_type": "inventory.normalize.py.v1",
                },
                {
                    "key": "reorder",
                    "goal": "Implement deterministic reorder quantity calculation.",
                    "selected_paths": ["inventory_math/reorder.py", "tests/test_reorder_public.py"],
                    "allowed_scope": ["inventory_math"],
                    "public_test": "tests/test_reorder_public.py",
                    "hidden_test": "test_reorder_hidden.py",
                    "handoff_id": "handoff_reorder",
                    "handoff_type": "inventory.reorder.py.v1",
                },
            ],
            "integration": {
                "goal": "Merge typed component handoffs and independently verify replenishment_order.",
                "selected_paths": ["inventory_math/service.py", "tests/test_integration_public.py"],
                "allowed_scope": ["inventory_math", "tests"],
            },
        },
    )


def controlled_stations() -> tuple[StationContract, StationContract]:
    implementation = StationContract(
        id=StationId("station_cold_start_implementation_v0"),
        role="cold_start_implementation",
        description="Bounded implementation station for deterministic component work.",
        accepted_input_kinds=("product_spec", "context_package"),
        required_output_kinds=("patch", "typed_handoff"),
        owned_scope=("inventory_math",),
        forbidden_scope=(".git", "hidden_tests", "secrets"),
        allowed_tool_classes=("filesystem_patch", "deterministic_test"),
        applicable_task_classes=("cold_start_component",),
        gate_requirements={"policy_id": GATE_POLICY_ID, "policy_version": GATE_POLICY_VERSION},
        version=1,
    )
    reviewer = StationContract(
        id=StationId("station_cold_start_integration_review_v0"),
        role="cold_start_integration_reviewer",
        description="Independent deterministic integration and final review station.",
        accepted_input_kinds=("typed_handoff", "context_package"),
        required_output_kinds=("verified_verdict",),
        owned_scope=("inventory_math", "tests"),
        forbidden_scope=(".git", "hidden_tests", "secrets"),
        allowed_tool_classes=("artifact_merge", "deterministic_test", "independent_review"),
        applicable_task_classes=("cold_start_integration_review",),
        gate_requirements={"policy_id": GATE_POLICY_ID, "policy_version": GATE_POLICY_VERSION},
        version=1,
    )
    return implementation, reviewer


@dataclass(frozen=True)
class RecipeCandidateArtifact:
    recipe: ProductRecipe
    identity_hash: str
    canonical_recipe_sha256: str
    evidence_id: str


@dataclass
class ColdStartRunOutcome:
    db_path: Path
    factory_run_id: str
    product: ProductSpec
    product_class: ProductClass
    signature: ProductSignature
    selection: ColdStartSelection
    plan: ColdStartPlan
    execution: ParallelDagRunResult
    verified: bool
    candidate: RecipeCandidateArtifact | None
    provider_calls: int
    network_calls: int
    worker_call_counts: Mapping[str, int]
    independent_reviewer_calls: int


def _new_event(
    event_type: LifecycleEventType,
    *,
    factory_run_id: str,
    product_id: str,
    payload: Mapping[str, Any],
) -> LifecycleEvent:
    return LifecycleEvent(
        event_type=event_type,
        entity_ids={"factory_run_id": factory_run_id, "product_id": product_id},
        payload=dict(payload),
        timestamp=_utcnow(),
    )


def _controlled_worker_configuration(
    implementation_station: StationContract,
) -> tuple[WorkerPool, WorkerPool, dict[str, WorkerRunner], dict[str, dict[tuple[str, int], int]]]:
    weak = WorkerProfile(
        id=WorkerId("worker_cold_cheap_weak_v0"),
        display_name="Cold-start cheap/weak fake candidate",
        version="1",
        capability_tags=("python_patch", "low_cost"),
        station_eligibility=(implementation_station.id,),
        availability_status="available",
        metadata={"relative_strength": "weaker", "fake": True},
    )
    strong = WorkerProfile(
        id=WorkerId("worker_cold_strong_fallback_v0"),
        display_name="Cold-start stronger fake fallback",
        version="1",
        capability_tags=("python_patch", "strict_tests"),
        station_eligibility=(implementation_station.id,),
        availability_status="available",
        metadata={"relative_strength": "stronger", "fake": True},
    )
    unavailable = WorkerProfile(
        id=WorkerId("worker_cold_unavailable_v0"),
        display_name="Unavailable cold-start candidate",
        version="1",
        capability_tags=("python_patch",),
        station_eligibility=(implementation_station.id,),
        availability_status="unavailable",
        metadata={"fake": True},
    )
    weak_runner = FakeScriptedWorker(
        outputs_by_case={
            "implement_normalize": {
                "patches": (PatchEdit(rel_path="inventory_math/normalize.py", action="write", content=_BAD_NORMALIZE),),
                "tokens": (30, 20),
            }
        }
    )
    strong_runner = FakeScriptedWorker(
        outputs_by_case={
            "implement_normalize": {
                "patches": (PatchEdit(rel_path="inventory_math/normalize.py", action="write", content=_GOOD_NORMALIZE),),
                "tokens": (45, 35),
            },
            "implement_reorder": {
                "patches": (PatchEdit(rel_path="inventory_math/reorder.py", action="write", content=_GOOD_REORDER),),
                "tokens": (40, 30),
            },
        }
    )
    pool_a = WorkerPool(
        id="worker_pool_cold_normalize_v0",
        station_id=implementation_station.id,
        role=implementation_station.role,
        entries=(PoolEntry(weak, 0), PoolEntry(strong, 1), PoolEntry(unavailable, 2)),
        selection_policy_id=EXPLORATION_POLICY_ID,
    )
    pool_b = WorkerPool(
        id="worker_pool_cold_reorder_v0",
        station_id=implementation_station.id,
        role=implementation_station.role,
        entries=(PoolEntry(strong, 0),),
        selection_policy_id=EXPLORATION_POLICY_ID,
    )
    runners: dict[str, WorkerRunner] = {
        str(weak.id): weak_runner,
        str(strong.id): strong_runner,
    }
    counters = {
        "weak": weak_runner.call_counter or {},
        "strong": strong_runner.call_counter or {},
    }
    return pool_a, pool_b, runners, counters


def run_controlled_cold_start(
    *,
    output_root: Path | str,
    known_recipe: ProductRecipe | None,
    policy: ColdStartPolicy | None = None,
    material_variant: str = "v1",
    force_unverified: bool = False,
    create_candidate: bool = True,
) -> ColdStartRunOutcome:
    """Run the offline controlled REDI-07A proof (zero provider/network calls)."""

    policy = policy or ColdStartPolicy()
    selection = select_cold_start_mode(
        known_recipe=known_recipe,
        reason=COLD_START_REASON_NO_RECIPE_SUPPLIED,
    )
    product = controlled_product_spec(material_variant=material_variant)
    signature = derive_product_signature(product)
    product_class = derive_product_class(product, signature)
    product.product_class_id = product_class.id
    stations = controlled_stations()
    plan = plan_cold_start_v0(
        product=product,
        product_class=product_class,
        signature=signature,
        stations=stations,
        policy=policy,
    )
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    db_path = root / "evidence.sqlite"
    artifact_root = root / "retained"
    conn = sqlite3.connect(db_path)
    bootstrap_schema(conn)
    ledger = EvidenceLedger(conn)
    runs = FactoryRunRepository(conn, ledger)
    factory_run = FactoryRun(
        product_spec_id=product.id,
        dag_id=str(plan.dag.id),
        recipe_id=None,
        mode=selection.factory_mode,
        final_status=RunStatus.RUNNING,
        verified=False,
        metadata={
            "execution_mode": selection.mode,
            "cold_start_selection": selection.to_evidence(),
            "product_spec_id": str(product.id),
            "product_class_id": str(product_class.id),
            "product_signature_sha256": signature.sha256,
            "recipe_lookup": {"implemented": False, "result": "none", "known_recipe_supplied": False},
            "exploration_policy_id": policy.exploration_policy_id,
            "exploration_policy_version": policy.exploration_policy_version,
        },
    )
    factory_run_id = runs.store(factory_run)
    ledger.append(
        _new_event(
            LifecycleEventType.PRODUCT_ACCEPTED,
            factory_run_id=factory_run_id,
            product_id=str(product.id),
            payload={
                "cold_start_selection": selection.to_evidence(),
                "product_signature": signature.to_evidence(),
                "product_class_id": str(product_class.id),
                "known_recipe": None,
            },
        ),
        factory_run_id=factory_run_id,
    )
    graph_hash = product_dag_hash(plan.dag)
    ledger.append(
        _new_event(
            LifecycleEventType.PLAN_COMPILED,
            factory_run_id=factory_run_id,
            product_id=str(product.id),
            payload={
                "graph_id": str(plan.dag.id),
                "graph_revision": plan.dag.revision,
                "graph_hash": graph_hash,
                "planner_policy_id": policy.planner_policy_id,
                "planner_policy_version": policy.planner_policy_version,
                "recipe_dependency": None,
            },
        ),
        factory_run_id=factory_run_id,
    )
    plan_evidence = RunEvidence(
        factory_run_id=factory_run.id,
        product_id=product.id,
        product_class_id=product_class.id,
        node_id="COLD_START_PLAN",
        task_class="cold_start_plan",
        final_status=RunStatus.COMPLETED,
        metadata={
            "evidence_kind": "cold_start_plan",
            "selection": selection.to_evidence(),
            "product_signature": signature.to_evidence(),
            "product_class": json.loads(domain_to_json(product_class)),
            "graph_id": str(plan.dag.id),
            "graph_revision": plan.dag.revision,
            "graph_hash": graph_hash,
            "dag_snapshot": dag_snapshot(plan.dag),
            "policies": dict(plan.policy_evidence),
        },
    )
    ledger.append(plan_evidence, factory_run_id=factory_run_id)

    implementation_station, review_station = stations
    pool_a, pool_b, runners, _ = _controlled_worker_configuration(implementation_station)
    attempt_counter = _RunAttemptCounter(policy.budget.max_total_attempts_per_run)
    barrier = threading.Barrier(2, timeout=10)
    node_workers: dict[str, Any] = {}
    for node_id in sorted(plan.components_by_node):
        component = plan.components_by_node[node_id]
        node_workers[node_id] = _ExploringComponentNode(
            node_id=node_id,
            product=product,
            task=plan.tasks_by_node[node_id],
            station=implementation_station,
            component=component,
            pool=pool_a if node_id == "implement_normalize" else pool_b,
            runners=runners,
            policy=policy,
            fixture_root=CONTROLLED_FIXTURE_ROOT,
            hidden_root=CONTROLLED_HIDDEN_ROOT,
            attempt_counter=attempt_counter,
            barrier=barrier,
        )
    reviewer = _IndependentIntegrationReviewer(
        node_id=plan.integration_node_id,
        product=product,
        task=plan.tasks_by_node[plan.integration_node_id],
        station=review_station,
        policy=policy,
        fixture_root=CONTROLLED_FIXTURE_ROOT,
        hidden_root=CONTROLLED_HIDDEN_ROOT,
    )
    node_workers[plan.integration_node_id] = reviewer
    executor = ParallelDagExecutor(
        dag=plan.dag,
        station_by_node=plan.stations_by_node,
        task_by_node=plan.tasks_by_node,
        node_workers=node_workers,
        required_handoff_ids_by_node=plan.required_handoffs_by_node,
        produced_handoff_ids_by_node=plan.produced_handoffs_by_node,
        produced_handoff_types_by_id=plan.produced_handoff_types_by_id,
        fixture_root=CONTROLLED_FIXTURE_ROOT,
        product=product,
        max_parallelism=2,
        context_budget_bytes=policy.context_budget_bytes,
        ledger=ledger,
        factory_run_id=factory_run_id,
        artifact_root=artifact_root,
        workspace_allowed_scope_prefixes=("inventory_math", "tests"),
    )
    started = time.perf_counter()
    execution = executor.run()
    all_completed = bool(execution.final_states) and all(
        state == NodeRunState.COMPLETED for state in execution.final_states.values()
    )
    verified = all_completed and not force_unverified
    if verified:
        ledger.append(
            _new_event(
                LifecycleEventType.PRODUCT_VERIFIED,
                factory_run_id=factory_run_id,
                product_id=str(product.id),
                payload={
                    "graph_id": str(plan.dag.id),
                    "graph_revision": plan.dag.revision,
                    "graph_hash": graph_hash,
                    "gate_policy_id": policy.gate_policy_id,
                    "gate_policy_version": policy.gate_policy_version,
                    "independent_reviewer": str(reviewer.worker_id),
                    "time_to_first_verified_product_seconds": round(time.perf_counter() - started, 6),
                },
            ),
            factory_run_id=factory_run_id,
        )
    final_status = RunStatus.COMPLETED if all_completed else RunStatus.FAILED
    runs.close_run(
        factory_run_id,
        final_status=final_status,
        verified=verified,
        timing=Timing(
            started_at=factory_run.created_at,
            ended_at=_utcnow(),
            wall_clock_seconds=execution.timing.get("graph_wall_clock_seconds"),
            compute_seconds=execution.timing.get("aggregate_node_compute_seconds"),
        ),
    )
    conn.close()
    candidate = None
    if create_candidate and verified:
        candidate = generate_recipe_candidate(db_path=db_path, factory_run_id=factory_run_id)
    counts: dict[str, int] = {}
    for worker_id, runner in runners.items():
        counts[worker_id] = sum((runner.call_counter or {}).values())  # type: ignore[attr-defined]
    return ColdStartRunOutcome(
        db_path=db_path,
        factory_run_id=factory_run_id,
        product=product,
        product_class=product_class,
        signature=signature,
        selection=selection,
        plan=plan,
        execution=execution,
        verified=verified,
        candidate=candidate,
        provider_calls=0,
        network_calls=0,
        worker_call_counts=counts,
        independent_reviewer_calls=reviewer.calls,
    )


def _load_run_material(
    conn: sqlite3.Connection, factory_run_id: str
) -> tuple[sqlite3.Row, FactoryRun, list[tuple[int, Any]]]:
    conn.row_factory = sqlite3.Row
    run_row = conn.execute(
        "SELECT * FROM factory_runs WHERE id = ? LIMIT 1", (factory_run_id,)
    ).fetchone()
    if run_row is None:
        raise CandidateGenerationError(f"FactoryRun {factory_run_id!r} does not exist")
    run_obj = domain_from_json(run_row["payload_json"])
    if not isinstance(run_obj, FactoryRun):
        raise CandidateGenerationError("stored source run is not a FactoryRun")
    rows = conn.execute(
        "SELECT global_sequence, payload_json FROM evidence_entries "
        "WHERE factory_run_id = ? ORDER BY global_sequence", (factory_run_id,)
    ).fetchall()
    decoded = [(int(row["global_sequence"]), domain_from_json(row["payload_json"])) for row in rows]
    return run_row, run_obj, decoded


def _existing_candidate(decoded: Sequence[tuple[int, Any]]) -> RecipeCandidateArtifact | None:
    found: list[RunEvidence] = []
    for _, entity in decoded:
        if isinstance(entity, RunEvidence) and (entity.metadata or {}).get("evidence_kind") == "cold_start_recipe_candidate":
            found.append(entity)
    if len(found) > 1:
        raise CandidateGenerationError("more than one recipe candidate exists for one FactoryRun")
    if not found:
        return None
    evidence = found[0]
    recipe_json = str(evidence.metadata["canonical_recipe_json"])
    recipe = domain_from_json(recipe_json)
    if not isinstance(recipe, ProductRecipe):
        raise CandidateGenerationError("candidate evidence does not decode to ProductRecipe")
    actual = hashlib.sha256(recipe_json.encode("utf-8")).hexdigest()
    expected = str(evidence.metadata["canonical_recipe_sha256"])
    if actual != expected:
        raise CandidateGenerationError("stored candidate canonical hash mismatch")
    return RecipeCandidateArtifact(
        recipe=recipe,
        identity_hash=str(evidence.metadata["candidate_identity_hash"]),
        canonical_recipe_sha256=actual,
        evidence_id=str(evidence.id),
    )


def generate_recipe_candidate(
    *, db_path: Path | str, factory_run_id: str
) -> RecipeCandidateArtifact:
    """Idempotently induce one candidate only from an intact verified run."""

    path = Path(db_path).resolve()
    reader = ReplayReader(path)
    try:
        integrity = reader.verify_integrity_all()
    finally:
        reader.close()
    if integrity["tampered_count"] or integrity["broken_chain_count"] or integrity["malformed_count"]:
        raise CandidateGenerationError(f"source evidence integrity is invalid: {integrity}")
    conn = sqlite3.connect(path)
    try:
        run_row, run_obj, decoded = _load_run_material(conn, factory_run_id)
        existing = _existing_candidate(decoded)
        if existing is not None:
            return existing
        if run_row["closed_at"] is None:
            raise CandidateGenerationError("partial/interrupted FactoryRun cannot create a candidate")
        if run_row["final_status"] != RunStatus.COMPLETED.value or not bool(run_row["verified"]):
            raise CandidateGenerationError("unverified or failed FactoryRun cannot create a candidate")
        verified_events = [
            entity for _, entity in decoded
            if isinstance(entity, LifecycleEvent) and entity.event_type == LifecycleEventType.PRODUCT_VERIFIED
        ]
        if len(verified_events) != 1:
            raise CandidateGenerationError("exactly one PRODUCT_VERIFIED event is required")
        promoted = [
            entity for _, entity in decoded
            if isinstance(entity, LifecycleEvent) and entity.event_type == LifecycleEventType.RECIPE_PROMOTED
        ]
        if promoted:
            raise CandidateGenerationError("source run already contains forbidden RECIPE_PROMOTED")
        plan_evidence = [
            entity for _, entity in decoded
            if isinstance(entity, RunEvidence) and (entity.metadata or {}).get("evidence_kind") == "cold_start_plan"
        ]
        if len(plan_evidence) != 1:
            raise CandidateGenerationError("exactly one cold-start plan evidence record is required")
        plan_record = plan_evidence[0]
        dag_rows = [
            dict(entity.metadata) for _, entity in decoded
            if isinstance(entity, RunEvidence) and entity.task_class == "dag_orchestration"
        ]
        replay = replay_dag_run(
            dag_rows,
            expected_graph_hash=str(plan_record.metadata["graph_hash"]),
            expected_revision=int(plan_record.metadata["graph_revision"]),
            retained_artifact_root=path.parent / "retained",
        )
        if not (replay.get("final_record") or {}).get("graph_verified"):
            raise CandidateGenerationError("source DAG is not graph-verified")
        attempts = [
            entity for _, entity in decoded
            if isinstance(entity, RunEvidence) and entity.task_class in {
                "cold_start_worker_attempt", "cold_start_independent_integration_review"
            }
        ]
        if not attempts:
            raise CandidateGenerationError("source run has no attempt evidence")
        gates = [entity for _, entity in decoded if isinstance(entity, GateResult)]
        failures = [entity for _, entity in decoded if isinstance(entity, FailureRecord)]
        mandatory_gate_names = set(
            plan_record.metadata["policies"]["gate_policy"]["mandatory_gates"]
        )
        observed_gate_names = {gate.gate_identity for gate in gates if gate.status == GateStatus.PASS}
        if not mandatory_gate_names.issubset(observed_gate_names):
            missing = sorted(mandatory_gate_names - observed_gate_names)
            raise CandidateGenerationError(f"missing mandatory passing gate evidence: {missing}")
        ordered_attempts = sorted(
            attempts,
            key=lambda item: (
                str(item.node_id),
                int((item.metadata or {}).get("attempt_index", 0)),
            ),
        )
        attempt_view = [
            {
                "evidence_id": str(item.id),
                "node_id": item.node_id,
                "station_id": str(item.station_id) if item.station_id else None,
                "worker_id": str(item.worker_id) if item.worker_id else None,
                "worker_version": item.worker_version,
                "attempt_index": (item.metadata or {}).get("attempt_index", 0),
                "status": item.final_status.value if item.final_status else None,
                "failure_category": item.failure_category.value if item.failure_category else None,
                "failure_action": (item.metadata or {}).get("failure_action"),
                "context_package_hash": (item.metadata or {}).get("context_package_hash"),
                "gate_result_ids": [str(value) for value in item.gate_results],
                "token_usage": _canonical(item.token_usage),
                "timing": _canonical(item.timing),
            }
            for item in ordered_attempts
        ]
        failure_view = [
            {
                "failure_id": str(item.id),
                "category": item.category.value,
                "node_id": item.node_id,
                "evidence_id": str(item.evidence_id) if item.evidence_id else None,
                "escalation_target": item.escalation_target,
            }
            for item in sorted(failures, key=lambda item: str(item.id))
        ]
        graph = replay["graph"]
        required_stations = tuple(
            StationId(value) for value in sorted({str(node.station_id) for node in graph.nodes if node.station_id})
        )
        successful_by_station: dict[StationId, list[WorkerId]] = {}
        observed_by_station: dict[StationId, list[WorkerId]] = {}
        for item in ordered_attempts:
            if not item.station_id or not item.worker_id:
                continue
            station_id = StationId(str(item.station_id))
            observed_by_station.setdefault(station_id, [])
            if WorkerId(str(item.worker_id)) not in observed_by_station[station_id]:
                observed_by_station[station_id].append(WorkerId(str(item.worker_id)))
            if item.final_status == RunStatus.COMPLETED:
                successful_by_station.setdefault(station_id, [])
                if WorkerId(str(item.worker_id)) not in successful_by_station[station_id]:
                    successful_by_station[station_id].append(WorkerId(str(item.worker_id)))
        evidence_refs = tuple(
            EvidenceFieldRef(
                evidence_id=str(entity.id),
                kind="gate" if isinstance(entity, GateResult) else "run",
                note=type(entity).__name__,
            )
            for _, entity in decoded
            if isinstance(entity, (RunEvidence, FailureRecord, GateResult))
        )
        source_material = {
            "policy_id": RECIPE_CANDIDATE_POLICY_ID,
            "policy_version": RECIPE_CANDIDATE_POLICY_VERSION,
            "source_factory_run_id": factory_run_id,
            "product_class_id": str(plan_record.product_class_id),
            "product_signature": plan_record.metadata["product_signature"],
            "graph_id": replay["graph_id"],
            "graph_revision": replay["graph_revision"],
            "graph_hash": replay["graph_hash"],
            "dag_snapshot": dag_snapshot(graph),
            "attempt_trajectory": attempt_view,
            "failure_trajectory": failure_view,
            "gate_results": [
                {
                    "gate_id": str(gate.id),
                    "identity": gate.gate_identity,
                    "status": gate.status.value,
                    "policy_id": (gate.metadata or {}).get("gate_policy_id"),
                    "policy_version": (gate.metadata or {}).get("gate_policy_version"),
                }
                for gate in gates
            ],
            "verified_event_id": str(verified_events[0].id),
            "evidence_ids": [ref.evidence_id for ref in evidence_refs],
            "observed_timing": _canonical(replay.get("timing") or {}),
        }
        identity_hash = _canonical_hash(source_material)
        created_at = datetime.fromisoformat(str(run_row["created_at"]))
        policies = plan_record.metadata["policies"]
        recipe = ProductRecipe(
            id=RecipeId(f"recipe_cold_{identity_hash[:32]}"),
            product_class_id=ProductClassId(str(plan_record.product_class_id)),
            dag_template_id=str(replay["graph_id"]),
            required_stations=required_stations,
            worker_preferences={key: tuple(value) for key, value in successful_by_station.items()},
            worker_fallbacks={key: tuple(value) for key, value in observed_by_station.items()},
            context_budgets={
                CONTEXT_STRATEGY_ID: Budget(
                    retries=0,
                    metadata={"budget_bytes": CONTEXT_BUDGET_BYTES, "observed_only": True},
                )
            },
            gate_policies=dict(policies["gate_policy"]),
            retry_policies={
                "policy_id": policies["exploration_policy"]["policy_id"],
                "policy_version": policies["exploration_policy"]["policy_version"],
                "budgets": policies["exploration_policy"]["budgets"],
            },
            escalation_policies={"failure_actions": policies["exploration_policy"]["failure_actions"]},
            parallelization_rules={
                "independent_root_nodes": sorted(
                    str(node.node_id) for node in graph.nodes
                    if not any(str(dep.downstream) == str(node.node_id) for dep in graph.dependencies)
                ),
                "max_parallelism_observed": 2,
                "integration_node": "integration_review",
            },
            evidence=evidence_refs,
            provenance={
                "source_factory_run_id": factory_run_id,
                "source_product_verified_event_id": str(verified_events[0].id),
                "source_graph_id": replay["graph_id"],
                "source_graph_revision": replay["graph_revision"],
                "source_graph_hash": replay["graph_hash"],
                "relevant_run_evidence_ids": [str(item.id) for item in attempts],
                "relevant_failure_record_ids": [str(item.id) for item in failures],
                "relevant_gate_result_ids": [str(item.id) for item in gates],
                "observed_trajectory": attempt_view,
                "observed_failures": failure_view,
                "claim_boundary": "observed trajectory only; no global worker superiority claim",
            },
            rung=RecipeRung.CANDIDATE,
            validity_conditions=(
                ValidityCondition(
                    description=f"Product signature remains {plan_record.metadata['product_signature']['sha256']}",
                    scope="data",
                    checked_via="hash",
                ),
                ValidityCondition(
                    description=f"Gate policy remains {policies['gate_policy']['policy_id']}@{policies['gate_policy']['policy_version']}",
                    scope="tool",
                    checked_via="version",
                ),
                ValidityCondition(
                    description="Historical evidence replays with intact hashes and retained artifacts.",
                    scope="environment",
                    checked_via="replay",
                ),
            ),
            version=1,
            metadata={
                "lifecycle_status": "CANDIDATE",
                "promotion_status": "NOT_PROMOTED",
                "candidate_identity_hash": identity_hash,
                "induction_policy_id": RECIPE_CANDIDATE_POLICY_ID,
                "induction_policy_version": RECIPE_CANDIDATE_POLICY_VERSION,
                "observed_configuration_only": True,
                "stable_or_best_claimed": False,
            },
            created_at=created_at,
        )
        recipe_json = domain_to_json(recipe)
        recipe_sha = hashlib.sha256(recipe_json.encode("utf-8")).hexdigest()
        candidate_evidence = RunEvidence(
            id=EvidenceId(f"evidence_recipe_{identity_hash[:24]}"),
            factory_run_id=FactoryRunId(factory_run_id),
            product_id=run_obj.product_spec_id,
            product_class_id=recipe.product_class_id,
            recipe_id=recipe.id,
            recipe_version=recipe.version,
            node_id="RECIPE_CANDIDATE",
            task_class="recipe_candidate_induction",
            final_status=RunStatus.COMPLETED,
            metadata={
                "evidence_kind": "cold_start_recipe_candidate",
                "candidate_status": "CANDIDATE",
                "promotion_status": "NOT_PROMOTED",
                "candidate_identity_hash": identity_hash,
                "canonical_recipe_sha256": recipe_sha,
                "canonical_recipe_json": recipe_json,
                "source_factory_run_id": factory_run_id,
                "source_graph_id": replay["graph_id"],
                "source_graph_revision": replay["graph_revision"],
                "source_graph_hash": replay["graph_hash"],
            },
            created_at=created_at,
        )
        candidate_event = LifecycleEvent(
            id=EventId(f"event_recipe_{identity_hash[:24]}"),
            event_type=LifecycleEventType.RECIPE_CANDIDATE_CREATED,
            entity_ids={
                "factory_run_id": factory_run_id,
                "recipe_id": str(recipe.id),
                "product_id": str(run_obj.product_spec_id),
            },
            payload={
                "candidate_identity_hash": identity_hash,
                "canonical_recipe_sha256": recipe_sha,
                "rung": RecipeRung.CANDIDATE.value,
                "promotion_status": "NOT_PROMOTED",
                "source_factory_run_id": factory_run_id,
            },
            timestamp=created_at,
        )
        ledger = EvidenceLedger(conn)
        ledger.append_batch(
            [
                {"entity": candidate_evidence, "factory_run_id": factory_run_id},
                {"entity": candidate_event, "factory_run_id": factory_run_id},
            ]
        )
        return RecipeCandidateArtifact(
            recipe=recipe,
            identity_hash=identity_hash,
            canonical_recipe_sha256=recipe_sha,
            evidence_id=str(candidate_evidence.id),
        )
    finally:
        conn.close()


def replay_cold_start(
    *, db_path: Path | str, factory_run_id: str
) -> dict[str, Any]:
    """Read-only reconstruction of cold-start, exploration, and candidate evidence."""

    path = Path(db_path).resolve()
    reader = ReplayReader(path)
    try:
        base = reader.replay_factory_run(FactoryRunId(factory_run_id))
    finally:
        reader.close()
    if base.integrity_status != "OK":
        raise CandidateGenerationError(f"cold-start replay integrity is {base.integrity_status}")
    plan_records = [
        item for item in base.run_evidences
        if (item.metadata or {}).get("evidence_kind") == "cold_start_plan"
    ]
    if len(plan_records) != 1:
        raise CandidateGenerationError("replay requires exactly one cold-start plan record")
    plan = plan_records[0]
    dag_rows = [
        dict(item.metadata) for item in base.run_evidences
        if item.task_class == "dag_orchestration"
    ]
    dag_replay = replay_dag_run(
        dag_rows,
        expected_graph_hash=str(plan.metadata["graph_hash"]),
        expected_revision=int(plan.metadata["graph_revision"]),
        retained_artifact_root=path.parent / "retained",
    )
    attempts = [
        item for item in base.run_evidences
        if item.task_class in {"cold_start_worker_attempt", "cold_start_independent_integration_review"}
    ]
    candidates = [
        item for item in base.run_evidences
        if (item.metadata or {}).get("evidence_kind") == "cold_start_recipe_candidate"
    ]
    if len(candidates) > 1:
        raise CandidateGenerationError("historical replay found duplicate recipe candidates")
    candidate = candidates[0] if candidates else None
    event_types = [event.event_type.value for event in base.ordered_events]
    node_exploration = {
        str(row.get("node_id")): {
            "candidate_inventory": (row.get("execution_evidence") or {}).get("candidate_inventory", []),
            "attempt_trajectory": (row.get("execution_evidence") or {}).get("attempt_trajectory", []),
            "pool_snapshot_hash": (row.get("execution_evidence") or {}).get("pool_snapshot_hash"),
            "exploration_policy": (row.get("execution_evidence") or {}).get("exploration_policy"),
        }
        for row in dag_replay.get("node_results") or []
        if (row.get("execution_evidence") or {}).get("candidate_inventory") is not None
    }
    return {
        "factory_run_id": factory_run_id,
        "integrity_status": base.integrity_status,
        "observed_run_status": base.observed_run_status,
        "final_verification_state": base.final_verification_state,
        "cold_start_mode": plan.metadata["selection"]["mode"],
        "cold_start_reason": plan.metadata["selection"]["reason"],
        "recipe_lookup_result": plan.metadata["selection"]["recipe_lookup_result"],
        "product_signature": plan.metadata["product_signature"],
        "product_class": plan.metadata["product_class"],
        "planner_policy": plan.metadata["policies"],
        "dag": canonical_replay_view(dag_replay),
        "node_exploration": node_exploration,
        "attempt_trajectory": [
            {
                "evidence_id": str(item.id),
                "node_id": item.node_id,
                "station_id": str(item.station_id) if item.station_id else None,
                "worker_id": str(item.worker_id) if item.worker_id else None,
                "attempt_index": (item.metadata or {}).get("attempt_index", 0),
                "status": item.final_status.value if item.final_status else None,
                "failure_category": item.failure_category.value if item.failure_category else None,
                "failure_action": (item.metadata or {}).get("failure_action"),
                "context_package_hash": (item.metadata or {}).get("context_package_hash"),
                "gate_result_ids": [str(value) for value in item.gate_results],
            }
            for item in sorted(attempts, key=lambda item: (str(item.node_id), int((item.metadata or {}).get("attempt_index", 0))))
        ],
        "failures": [
            {
                "failure_id": str(item.id),
                "category": item.category.value,
                "node_id": item.node_id,
                "worker_id": (item.metadata or {}).get("worker_id"),
                "context_package_hash": (item.metadata or {}).get("context_package_hash"),
                "gate_boundary": (item.metadata or {}).get("gate_boundary"),
                "retry_escalation_decision": (item.metadata or {}).get("retry_escalation_decision") or item.escalation_target,
            }
            for item in base.failures
        ],
        "gates": [
            {
                "gate_id": str(item.id),
                "identity": item.gate_identity,
                "status": item.status.value,
                "policy_id": (item.metadata or {}).get("gate_policy_id"),
                "policy_version": (item.metadata or {}).get("gate_policy_version"),
            }
            for item in base.gate_results
        ],
        "event_types": event_types,
        "product_verified_event": LifecycleEventType.PRODUCT_VERIFIED.value in event_types,
        "recipe_promoted_event": LifecycleEventType.RECIPE_PROMOTED.value in event_types,
        "recipe_candidate": None if candidate is None else {
            "evidence_id": str(candidate.id),
            "recipe_id": str(candidate.recipe_id),
            "candidate_identity_hash": candidate.metadata["candidate_identity_hash"],
            "canonical_recipe_sha256": candidate.metadata["canonical_recipe_sha256"],
            "canonical_recipe_json": candidate.metadata["canonical_recipe_json"],
            "source_factory_run_id": candidate.metadata["source_factory_run_id"],
            "source_graph_id": candidate.metadata["source_graph_id"],
            "source_graph_revision": candidate.metadata["source_graph_revision"],
            "source_graph_hash": candidate.metadata["source_graph_hash"],
            "promotion_status": candidate.metadata["promotion_status"],
        },
    }


def canonical_cold_start_replay_view(replay: Mapping[str, Any]) -> dict[str, Any]:
    return _canonical(replay)


__all__ = [
    "COLD_START_UNKNOWN_PRODUCT",
    "COLD_START_REASON_NO_RECIPE_SUPPLIED",
    "PLANNER_POLICY_ID",
    "PLANNER_POLICY_VERSION",
    "GATE_POLICY_ID",
    "GATE_POLICY_VERSION",
    "EXPLORATION_POLICY_ID",
    "EXPLORATION_POLICY_VERSION",
    "CandidateGenerationError",
    "ColdStartPlan",
    "ColdStartPlanningError",
    "ColdStartPolicy",
    "ColdStartRunOutcome",
    "ColdStartSelection",
    "ColdStartSelectionError",
    "ExplorationBudget",
    "ProductSignature",
    "RecipeCandidateArtifact",
    "canonical_cold_start_replay_view",
    "classify_gate_failure",
    "controlled_product_spec",
    "controlled_stations",
    "derive_product_class",
    "derive_product_signature",
    "escalation_action",
    "failure_action_table",
    "generate_recipe_candidate",
    "plan_cold_start_v0",
    "replay_cold_start",
    "run_controlled_cold_start",
    "select_cold_start_mode",
]
