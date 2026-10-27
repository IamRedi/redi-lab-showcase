"""REDI-08A recipe-store matching, adaptation, dry execution, and replay.

Everything in this module is deterministic and local.  It has no provider or
network adapter and makes no claim that a recipe transfers or outperforms a
cold start.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from .cold_start import canonical_cold_start_replay_view, replay_cold_start
from .dag_scheduler import (
    NodeRunState,
    canonical_replay_view,
    dag_snapshot,
    product_dag_hash,
    replay_dag_run,
)
from .domain import (
    AcceptanceCriterion,
    DagDependency,
    DagNode,
    EnvironmentInfo,
    ExperimentManifest,
    FactoryRun,
    FactoryRunId,
    GateResult,
    GateResultId,
    GateStatus,
    GateType,
    LifecycleEvent,
    LifecycleEventType,
    Mode,
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
    StationContract,
    StationId,
    TaskContract,
    TaskId,
    Timing,
    TokenUsage,
    ValidityCondition,
    WorkerId,
    domain_from_json,
    domain_to_json,
)
from .parallel_dag_executor import (
    NodeWorkspaceHandle,
    ParallelDagExecutor,
    ParallelDagRunResult,
    WorkerExecutionResult,
)
from .persistence import EvidenceLedger, FactoryRunRepository, bootstrap_schema
from .replay.reader import ReplayReader
from .recipe_store import (
    ConfidenceMetadata,
    RecipeStore,
    StoredRecipeVersion,
    canonical_recipe_hash,
)


WARM_START_KNOWN_PRODUCT = "WARM_START_KNOWN_PRODUCT"
WARM_START_MATCH_POLICY_ID = "redi08a_product_class_match_v0"
WARM_START_MATCH_POLICY_VERSION = 1
WARM_START_ELIGIBILITY_POLICY_ID = "redi08a_experimental_warm_start_eligibility_v0"
WARM_START_ELIGIBILITY_POLICY_VERSION = 1
WARM_START_ADAPTATION_POLICY_ID = "redi08a_deterministic_recipe_adaptation_v0"
WARM_START_ADAPTATION_POLICY_VERSION = 1
WARM_START_SELECTION_POLICY_ID = "redi08a_recipe_version_selection_v0"
WARM_START_SELECTION_POLICY_VERSION = 1
WARM_START_GATE_POLICY_ID = "cold_start_strict_gates_v0"
WARM_START_GATE_POLICY_VERSION = 1
WARM_START_CONTEXT_STRATEGY_ID = "selected_v0"
WARM_START_ROLLBACK_POLICY_ID = "redi08a_governed_rollback_v0"
WARM_START_ROLLBACK_POLICY_VERSION = 1

ACCOUNTING_SEMANTICS = {
    "policy_id": "redi08a_accounting_semantics_v0",
    "policy_version": 1,
    "entity_created_at": "domain entity occurrence time; never backdated for identity",
    "evidence_recorded_at": "append time assigned by the evidence ledger",
    "lifecycle_sequence": "global_sequence is authoritative for causal ordering",
    "provider_token_totals": "provider-billed input/output/cached tokens only",
    "local_compute_accounting": "wall/compute seconds and local operation counts; no synthetic tokens",
    "aggregate_experiment_totals": "provider metrics and local compute remain separate dimensions",
}


class WarmStartError(RuntimeError):
    """Base error for governed warm-start operations."""


class MatchDecision(str, Enum):
    COMPATIBLE = "COMPATIBLE"
    INCOMPATIBLE = "INCOMPATIBLE"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"


@dataclass(frozen=True)
class ProductClassMatch:
    decision: MatchDecision
    policy_id: str
    policy_version: int
    reasons: tuple[str, ...]
    class_invariant_hash: str | None
    product_variable_hash: str | None
    evidence: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "reasons": list(self.reasons),
            "class_invariant_hash": self.class_invariant_hash,
            "product_variable_hash": self.product_variable_hash,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class WarmStartEligibility:
    eligible: bool
    policy_id: str
    policy_version: int
    reasons: tuple[str, ...]
    evidence: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "decision": "ELIGIBLE" if self.eligible else "BLOCKED",
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "reasons": list(self.reasons),
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class AdaptationManifest:
    manifest_id: str
    policy_id: str
    policy_version: int
    recipe_id: str
    recipe_version: int
    canonical_recipe_hash: str
    sibling_product_id: str
    product_class_id: str
    source_graph_id: str
    source_graph_hash: str
    topology_preserved: bool
    station_responsibilities_preserved: bool
    integration_preserved: bool
    gate_policy_preserved: bool
    changes: Mapping[str, Any]
    forbidden_changes: tuple[str, ...]
    canonical_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_id": self.manifest_id,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "recipe_id": self.recipe_id,
            "recipe_version": self.recipe_version,
            "canonical_recipe_hash": self.canonical_recipe_hash,
            "sibling_product_id": self.sibling_product_id,
            "product_class_id": self.product_class_id,
            "source_graph_id": self.source_graph_id,
            "source_graph_hash": self.source_graph_hash,
            "topology_preserved": self.topology_preserved,
            "station_responsibilities_preserved": self.station_responsibilities_preserved,
            "integration_preserved": self.integration_preserved,
            "gate_policy_preserved": self.gate_policy_preserved,
            "changes": dict(self.changes),
            "forbidden_changes": list(self.forbidden_changes),
            "canonical_hash": self.canonical_hash,
        }


@dataclass(frozen=True)
class WarmStartPlan:
    dag: ProductDAG
    tasks_by_node: Mapping[str, TaskContract]
    stations_by_node: Mapping[str, StationContract]
    required_handoffs_by_node: Mapping[str, Sequence[str]]
    produced_handoffs_by_node: Mapping[str, Sequence[str]]
    produced_handoff_types_by_id: Mapping[str, Sequence[str]]
    components_by_node: Mapping[str, Mapping[str, Any]]
    integration_node_id: str
    adaptation: AdaptationManifest


@dataclass
class WarmStartRunOutcome:
    db_path: Path
    factory_run_id: str
    product: ProductSpec
    recipe: StoredRecipeVersion
    match: ProductClassMatch
    eligibility: WarmStartEligibility
    plan: WarmStartPlan
    execution: ParallelDagRunResult
    verified: bool
    provider_calls: int
    network_calls: int
    worker_calls: Mapping[str, int]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _canonical(v) for k, v in sorted(value.items(), key=lambda i: str(i[0]))}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_canonical(v) for v in value]
    if isinstance(value, Enum):
        return value.value
    return str(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(_canonical(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory_product_class(product_class_id: str) -> ProductClass:
    """Class-invariant description for the REDI inventory-replenishment family."""

    invariants = {
        "product_family": "inventory_replenishment_utility",
        "topology_roles": ["normalize_identifier", "calculate_reorder", "integration_review"],
        "station_responsibilities": [
            "deterministic_component_implementation",
            "independent_integration_review",
        ],
        "technology_family": "python_stdlib_package",
        "verification_profile": "strict_public_private_integration_independent_review",
        "interface_categories": ["identifier_normalization", "integer_reorder", "mapping_result"],
    }
    return ProductClass(
        id=ProductClassId(product_class_id),
        name="inventory_replenishment_utility",
        description="Inventory replenishment utilities with two independent components and review.",
        signature={
            "signature_policy_id": WARM_START_MATCH_POLICY_ID,
            "signature_policy_version": WARM_START_MATCH_POLICY_VERSION,
            "class_invariants": invariants,
            "product_variable_fields": [
                "requested_outcome",
                "requirements",
                "acceptance_criteria",
                "package_name",
                "component_goals",
                "source_paths",
                "interface_names",
                "fixture_data",
            ],
        },
        version=1,
        metadata={"whole_product_spec_hash_used_for_matching": False},
    )


def _classification_metadata(
    *, package_name: str, interface_names: Sequence[str], fixture_profile: str
) -> dict[str, Any]:
    return {
        "policy_id": WARM_START_MATCH_POLICY_ID,
        "policy_version": WARM_START_MATCH_POLICY_VERSION,
        "class_invariants": inventory_product_class("placeholder").signature["class_invariants"],
        "product_variables": {
            "package_name": package_name,
            "interface_names": list(interface_names),
            "fixture_profile": fixture_profile,
        },
    }


def original_product_fixture(product_class_id: str) -> ProductSpec:
    from .cold_start import controlled_product_spec

    base = controlled_product_spec()
    metadata = dict(base.metadata) | {
        "redi08_classification": _classification_metadata(
            package_name="inventory_math",
            interface_names=("normalize_sku", "reorder_quantity", "replenishment_order"),
            fixture_profile="single_item_inventory",
        )
    }
    return replace(base, product_class_id=ProductClassId(product_class_id), metadata=metadata)


def sibling_product_fixture(product_class_id: str) -> ProductSpec:
    """Materially different sibling: batch/vendor interfaces and separate paths."""

    return ProductSpec(
        id=ProductId("product_redi08a_sibling_bulk_inventory"),
        requested_outcome=(
            "Build a vendor-aware bulk replenishment utility with safety-stock handling "
            "and a batch-oriented result interface."
        ),
        requirements=(
            Requirement(
                text="Normalize vendor item codes while preserving a required vendor namespace.",
                kind="functional",
            ),
            Requirement(
                text="Calculate batch reorder quantity including a configurable safety-stock floor.",
                kind="functional",
            ),
            Requirement(
                text="Integrate both capabilities behind build_bulk_replenishment.",
                kind="functional",
            ),
        ),
        constraints=(
            Requirement(text="Use the Python standard library only.", kind="constraint"),
            Requirement(text="Operate offline on deterministic fixture data.", kind="constraint"),
        ),
        acceptance_criteria=(
            AcceptanceCriterion(description="Vendor namespace is present in normalized output."),
            AcceptanceCriterion(description="Safety stock is included without negative quantities."),
            AcceptanceCriterion(description="Independent integration review passes."),
        ),
        risk_tier=RiskTier.SYNTHETIC,
        requested_artifacts=("bulk_inventory package", "warm-start verification evidence"),
        product_class_id=ProductClassId(product_class_id),
        metadata={
            "product_type": "python_inventory_package",
            "redi08_classification": _classification_metadata(
                package_name="bulk_inventory",
                interface_names=(
                    "normalize_vendor_code",
                    "batch_reorder_quantity",
                    "build_bulk_replenishment",
                ),
                fixture_profile="multi_vendor_batch_with_safety_stock",
            ),
            # Both declarations describe the same product-variable work.  The
            # cold planner consumes ``cold_start_components`` without recipe
            # knowledge; the warm compiler consumes ``warm_start_components``
            # through the pinned recipe adaptation policy.  Keeping both on
            # the canonical sibling ProductSpec lets REDI-08B compare the two
            # architectural paths without changing the controlled input.
            "cold_start_components": [
                {
                    "key": "normalize",
                    "goal": "Implement the exact public interface normalize_vendor_code(vendor, code) for vendor-namespaced item-code normalization.",
                    "selected_paths": [
                        "bulk_inventory/normalize.py",
                        "tests/test_vendor_normalize_public.py",
                    ],
                    "allowed_scope": ["bulk_inventory"],
                    "target_path": "bulk_inventory/normalize.py",
                    "public_test": "tests/test_vendor_normalize_public.py",
                    "handoff_id": "handoff_normalize",
                    "handoff_type": "bulk_inventory.normalize.py.v1",
                    "interface": "normalize_vendor_code(vendor: str, code: str) -> str",
                },
                {
                    "key": "reorder",
                    "goal": "Implement batch reorder quantity with safety stock.",
                    "selected_paths": [
                        "bulk_inventory/reorder.py",
                        "tests/test_batch_reorder_public.py",
                    ],
                    "allowed_scope": ["bulk_inventory"],
                    "target_path": "bulk_inventory/reorder.py",
                    "public_test": "tests/test_batch_reorder_public.py",
                    "handoff_id": "handoff_reorder",
                    "handoff_type": "bulk_inventory.reorder.py.v1",
                    "interface": "batch_reorder_quantity(on_hand: int, target: int, safety: int) -> int",
                },
            ],
            "warm_start_components": [
                {
                    "key": "normalize",
                    "goal": "Implement the exact public interface normalize_vendor_code(vendor, code) for vendor-namespaced item-code normalization.",
                    "selected_paths": [
                        "bulk_inventory/normalize.py",
                        "tests/test_vendor_normalize_public.py",
                    ],
                    "allowed_scope": ["bulk_inventory"],
                    "target_path": "bulk_inventory/normalize.py",
                    "public_test": "tests/test_vendor_normalize_public.py",
                    "handoff_id": "handoff_normalize",
                    "handoff_type": "bulk_inventory.normalize.py.v1",
                    "interface": "normalize_vendor_code(vendor: str, code: str) -> str",
                },
                {
                    "key": "reorder",
                    "goal": "Implement batch reorder quantity with safety stock.",
                    "selected_paths": [
                        "bulk_inventory/reorder.py",
                        "tests/test_batch_reorder_public.py",
                    ],
                    "allowed_scope": ["bulk_inventory"],
                    "target_path": "bulk_inventory/reorder.py",
                    "public_test": "tests/test_batch_reorder_public.py",
                    "handoff_id": "handoff_reorder",
                    "handoff_type": "bulk_inventory.reorder.py.v1",
                    "interface": "batch_reorder_quantity(on_hand: int, target: int, safety: int) -> int",
                },
            ],
            "integration": {
                "goal": "Integrate vendor normalization and safety-stock batch reorder results.",
                "selected_paths": [
                    "bulk_inventory/service.py",
                    "tests/test_bulk_integration_public.py",
                ],
                "allowed_scope": ["bulk_inventory", "tests"],
                "interface": "build_bulk_replenishment(vendor, code, on_hand, target, safety) -> mapping",
            },
        },
    )


def incompatible_product_fixture() -> ProductSpec:
    return ProductSpec(
        id=ProductId("product_redi08a_incompatible_image_pipeline"),
        requested_outcome="Build an image-classification training pipeline with GPU artifacts.",
        requirements=(
            Requirement(text="Train an image classifier from labeled tensors.", kind="functional"),
        ),
        constraints=(Requirement(text="Requires GPU runtime.", kind="constraint"),),
        acceptance_criteria=(AcceptanceCriterion(description="Validation accuracy is reported."),),
        risk_tier=RiskTier.SYNTHETIC,
        product_class_id=ProductClassId("product_class_incompatible_image_pipeline"),
        metadata={
            "redi08_classification": {
                "policy_id": WARM_START_MATCH_POLICY_ID,
                "policy_version": WARM_START_MATCH_POLICY_VERSION,
                "class_invariants": {
                    "product_family": "image_model_training",
                    "topology_roles": ["dataset", "training", "evaluation"],
                    "station_responsibilities": ["gpu_training", "model_evaluation"],
                    "technology_family": "gpu_ml_pipeline",
                    "verification_profile": "statistical_model_validation",
                    "interface_categories": ["tensor_input", "model_artifact"],
                },
                "product_variables": {"dataset": "synthetic-images"},
            }
        },
    )


def match_product_class(product: ProductSpec, product_class: ProductClass) -> ProductClassMatch:
    declared = (product.metadata or {}).get("redi08_classification")
    expected = (product_class.signature or {}).get("class_invariants")
    if not isinstance(declared, Mapping) or not isinstance(expected, Mapping):
        return ProductClassMatch(
            decision=MatchDecision.REQUIRES_REVIEW,
            policy_id=WARM_START_MATCH_POLICY_ID,
            policy_version=WARM_START_MATCH_POLICY_VERSION,
            reasons=("class-invariant metadata is incomplete",),
            class_invariant_hash=None,
            product_variable_hash=None,
            evidence={"whole_product_spec_hash_compared": False},
        )
    actual = declared.get("class_invariants")
    variables = declared.get("product_variables")
    if not isinstance(actual, Mapping) or not isinstance(variables, Mapping):
        decision = MatchDecision.REQUIRES_REVIEW
        reasons = ("product did not separate invariant and variable fields",)
    elif _canonical(actual) != _canonical(expected):
        decision = MatchDecision.INCOMPATIBLE
        reasons = ("declared class-invariant fields differ",)
    elif product.product_class_id is not None and str(product.product_class_id) != str(product_class.id):
        decision = MatchDecision.INCOMPATIBLE
        reasons = ("declared ProductClass identity differs",)
    else:
        decision = MatchDecision.COMPATIBLE
        reasons = (
            "all declared class-invariant fields match",
            "product-variable fields were intentionally excluded from class identity",
        )
    return ProductClassMatch(
        decision=decision,
        policy_id=WARM_START_MATCH_POLICY_ID,
        policy_version=WARM_START_MATCH_POLICY_VERSION,
        reasons=reasons,
        class_invariant_hash=_hash(actual) if isinstance(actual, Mapping) else None,
        product_variable_hash=_hash(variables) if isinstance(variables, Mapping) else None,
        evidence={
            "product_id": str(product.id),
            "product_class_id": str(product_class.id),
            "class_invariant_fields": sorted(str(k) for k in expected),
            "product_variable_fields": sorted(str(k) for k in variables) if isinstance(variables, Mapping) else [],
            "whole_product_spec_hash_compared": False,
        },
    )


def import_candidate_from_ledger(
    *,
    store: RecipeStore,
    source_ledger: Path | str,
    source_factory_run_id: str,
    product_class: ProductClass,
) -> StoredRecipeVersion:
    """Import an evidence-backed candidate without writing its source ledger."""

    ledger_path = Path(source_ledger).resolve()
    reader = ReplayReader(ledger_path)
    try:
        integrity = reader.verify_integrity_all()
        base = reader.replay_factory_run(FactoryRunId(source_factory_run_id))
    finally:
        reader.close()
    if any(integrity[key] for key in ("tampered_count", "broken_chain_count", "malformed_count")):
        raise WarmStartError("source candidate ledger integrity failed")
    replay = canonical_cold_start_replay_view(
        replay_cold_start(db_path=ledger_path, factory_run_id=source_factory_run_id)
    )
    candidate = replay.get("recipe_candidate")
    if not candidate or not replay.get("product_verified_event"):
        raise WarmStartError("source run has no verified recipe candidate")
    recipe_json = str(candidate["canonical_recipe_json"])
    recipe = domain_from_json(recipe_json)
    if not isinstance(recipe, ProductRecipe):
        raise WarmStartError("candidate payload is not ProductRecipe")
    if canonical_recipe_hash(recipe) != candidate["canonical_recipe_sha256"]:
        raise WarmStartError("candidate canonical hash mismatch")
    if recipe.rung != RecipeRung.CANDIDATE or (
        recipe.metadata or {}
    ).get("promotion_status") != "NOT_PROMOTED":
        raise WarmStartError("only an unpromoted candidate may be imported")
    if str(recipe.product_class_id) != str(product_class.id):
        raise WarmStartError("candidate ProductClass does not match the registered class")
    source_ids = {entity_id for entity_id, _kind, _seq in base.evidence_refs}
    referenced_ids = {
        str(ref.get("evidence_id")) if isinstance(ref, Mapping) else str(ref.evidence_id)
        for ref in recipe.evidence
    }
    if not referenced_ids.issubset(source_ids):
        raise WarmStartError("candidate contains dangling evidence references")
    source_provenance = dict(recipe.provenance)
    product_verified_id = str(source_provenance.get("source_product_verified_event_id") or "")
    if product_verified_id not in source_ids:
        raise WarmStartError("candidate PRODUCT_VERIFIED provenance is unresolved")
    confidence = ConfidenceMetadata(
        tier="LOW",
        evidence_state="SINGLE_PRODUCT_EVIDENCE",
        source_verified_runs_count=1,
        source_failed_runs_count=0,
        source_failed_attempts_count=len(replay.get("failures", ())),
        distinct_sibling_products_tested=0,
        last_verified_at=next(
            (
                event.timestamp.isoformat()
                for event in base.ordered_events
                if event.event_type == LifecycleEventType.PRODUCT_VERIFIED
            ),
            None,
        ),
        environment_coverage=("controlled_python_offline",),
        model_coverage=tuple(sorted({
            str(attempt.get("worker_id"))
            for attempt in replay.get("attempt_trajectory", ())
            if attempt.get("worker_id")
        })),
        station_evidence_coverage=tuple(str(s) for s in recipe.required_stations),
        transfer_evidence_count=0,
    )
    provenance = source_provenance | {
        "source_candidate_evidence_id": candidate["evidence_id"],
        "source_candidate_identity_hash": candidate["candidate_identity_hash"],
        "source_candidate_canonical_hash": candidate["canonical_recipe_sha256"],
        "source_factory_run_id": source_factory_run_id,
        "source_product_verified_event_id": product_verified_id,
        "source_graph_id": candidate["source_graph_id"],
        "source_graph_revision": candidate["source_graph_revision"],
        "source_graph_hash": candidate["source_graph_hash"],
        "source_dag_snapshot": replay["dag"]["dag_snapshot"],
        "source_attempt_trajectory": replay["attempt_trajectory"],
        "source_failure_records": replay["failures"],
        "source_gate_results": replay["gates"],
        "source_context_hashes": replay["dag"]["context_hashes"],
        "source_ledger_sha256": _file_hash(ledger_path),
        "all_candidate_evidence_refs_resolved": True,
        "claim_boundary": "candidate import only; no transfer or superiority claim",
    }
    return store.register_version(
        recipe=recipe,
        product_class=product_class,
        confidence=confidence,
        provenance=provenance,
        parent_version=None,
        rollback_target_version=None,
        revalidation_state="CANDIDATE_NOT_WARM_START_ELIGIBLE",
        registration_metadata={
            "import_policy_id": "redi08a_verified_candidate_import_v0",
            "import_policy_version": 1,
            "source_ledger_read_only": True,
            "source_evidence_copied": False,
            "source_reference_count": len(referenced_ids),
        },
        event_type="CANDIDATE_IMPORTED",
        policy_id="redi08a_verified_candidate_import_v0",
        policy_version=1,
        event_evidence={
            "source_factory_run_id": source_factory_run_id,
            "source_product_verified_event_id": product_verified_id,
            "source_candidate_canonical_hash": candidate["canonical_recipe_sha256"],
            "references_resolved": True,
        },
        event_reason="verified REDI-07 candidate registered without source-ledger mutation",
    )


def evaluate_warm_start_eligibility(
    *,
    stored: StoredRecipeVersion,
    match: ProductClassMatch,
    available_station_ids: Sequence[str],
    available_worker_ids: Sequence[str],
    available_context_strategies: Sequence[str],
    available_gate_policies: Sequence[str],
    environment_valid: bool = True,
    unresolved_drift: bool = False,
) -> WarmStartEligibility:
    reasons: list[str] = []
    if match.decision != MatchDecision.COMPATIBLE:
        reasons.append(f"product-class match is {match.decision.value}")
    if stored.lifecycle_state not in {RecipeRung.SHADOW, RecipeRung.CANARY, RecipeRung.VALIDATED}:
        reasons.append("recipe lifecycle is not experimental-warm-start eligible")
    station_available = set(str(v) for v in available_station_ids)
    missing_stations = sorted(set(str(v) for v in stored.recipe.required_stations) - station_available)
    if missing_stations:
        reasons.append("missing required stations: " + ",".join(missing_stations))
    required_workers = {
        str(worker)
        for workers in stored.recipe.worker_preferences.values()
        for worker in workers
    }
    worker_available = set(str(v) for v in available_worker_ids)
    missing_workers = sorted(required_workers - worker_available)
    if missing_workers:
        reasons.append("missing preferred experimental workers: " + ",".join(missing_workers))
    required_context = set(str(v) for v in stored.recipe.context_budgets)
    missing_context = sorted(required_context - set(str(v) for v in available_context_strategies))
    if missing_context:
        reasons.append("missing context strategy: " + ",".join(missing_context))
    gate_id = str(stored.recipe.gate_policies.get("policy_id") or "")
    if gate_id not in set(str(v) for v in available_gate_policies):
        reasons.append(f"verification policy {gate_id!r} unavailable")
    if not environment_valid:
        reasons.append("environment/runtime validity conditions failed")
    if unresolved_drift or stored.revalidation_state.startswith("BLOCKED"):
        reasons.append("unresolved drift or revalidation block")
    return WarmStartEligibility(
        eligible=not reasons,
        policy_id=WARM_START_ELIGIBILITY_POLICY_ID,
        policy_version=WARM_START_ELIGIBILITY_POLICY_VERSION,
        reasons=tuple(reasons or ("all deterministic eligibility checks passed",)),
        evidence={
            "recipe_id": str(stored.recipe.id),
            "recipe_version": stored.recipe.version,
            "canonical_recipe_hash": stored.canonical_hash,
            "lifecycle_state": stored.lifecycle_state.value,
            "confidence": stored.confidence.to_dict(),
            "match": match.to_dict(),
            "required_stations": sorted(str(v) for v in stored.recipe.required_stations),
            "required_workers": sorted(required_workers),
            "context_strategies": sorted(required_context),
            "gate_policy_id": gate_id,
            "environment_valid": environment_valid,
            "unresolved_drift": unresolved_drift,
        },
    )


def controlled_warm_stations() -> tuple[StationContract, StationContract]:
    implementation = StationContract(
        id=StationId("station_cold_start_implementation_v0"),
        role="warm_start_implementation",
        description="Recipe-adapted deterministic implementation station.",
        accepted_input_kinds=("product_spec", "recipe_version", "adaptation_manifest"),
        required_output_kinds=("patch", "typed_handoff"),
        owned_scope=("bulk_inventory",),
        forbidden_scope=(".git", "hidden_tests", "secrets"),
        allowed_tool_classes=("filesystem_patch", "deterministic_test"),
        applicable_task_classes=("warm_start_component",),
        gate_requirements={"policy_id": WARM_START_GATE_POLICY_ID, "policy_version": 1},
        version=1,
    )
    reviewer = StationContract(
        id=StationId("station_cold_start_integration_review_v0"),
        role="warm_start_integration_reviewer",
        description="Independent recipe-adapted integration review station.",
        accepted_input_kinds=("typed_handoff", "adaptation_manifest"),
        required_output_kinds=("verified_verdict",),
        owned_scope=("bulk_inventory", "tests"),
        forbidden_scope=(".git", "hidden_tests", "secrets"),
        allowed_tool_classes=("artifact_merge", "deterministic_test", "independent_review"),
        applicable_task_classes=("warm_start_integration_review",),
        gate_requirements={"policy_id": WARM_START_GATE_POLICY_ID, "policy_version": 1},
        version=1,
    )
    return implementation, reviewer


def compile_warm_start_dag(
    *,
    product: ProductSpec,
    product_class: ProductClass,
    stored: StoredRecipeVersion,
    match: ProductClassMatch,
    eligibility: WarmStartEligibility,
    stations: Sequence[StationContract] | None = None,
    recipe_identity_hash: str | None = None,
    product_identity_hash: str | None = None,
) -> WarmStartPlan:
    if match.decision != MatchDecision.COMPATIBLE or not eligibility.eligible:
        raise WarmStartError("warm-start DAG compilation requires compatible and eligible selection")
    if str(stored.recipe.product_class_id) != str(product_class.id):
        raise WarmStartError("selected recipe belongs to a different ProductClass")
    source_snapshot = stored.provenance.get("source_dag_snapshot")
    if not isinstance(source_snapshot, Mapping):
        raise WarmStartError("recipe provenance has no source DAG snapshot")
    source_nodes = source_snapshot.get("nodes") or ()
    source_deps = source_snapshot.get("dependencies") or ()
    source_node_ids = {str(node.get("node_id")) for node in source_nodes}
    if source_node_ids != {"implement_normalize", "implement_reorder", "integration_review"}:
        raise WarmStartError("V0 recipe topology is not the governed two-root integration pattern")
    if {
        (str(dep.get("upstream")), str(dep.get("downstream"))) for dep in source_deps
    } != {
        ("implement_normalize", "integration_review"),
        ("implement_reorder", "integration_review"),
    }:
        raise WarmStartError("source topology dependency pattern changed")
    recipe_gate_id = str(stored.recipe.gate_policies.get("policy_id") or "")
    recipe_gate_version = int(stored.recipe.gate_policies.get("policy_version") or 0)
    if (recipe_gate_id, recipe_gate_version) != (
        WARM_START_GATE_POLICY_ID,
        WARM_START_GATE_POLICY_VERSION,
    ):
        raise WarmStartError("adaptation cannot weaken or replace the recipe gate policy")
    components = list((product.metadata or {}).get("warm_start_components") or ())
    if {str(item.get("key")) for item in components} != {"normalize", "reorder"}:
        raise WarmStartError("sibling must provide both recipe component roles")
    implementation, reviewer = tuple(stations or controlled_warm_stations())
    components_by_node: dict[str, Mapping[str, Any]] = {}
    tasks: dict[str, TaskContract] = {}
    station_map: dict[str, StationContract] = {}
    nodes: list[DagNode] = []
    produced: dict[str, Sequence[str]] = {}
    produced_types: dict[str, Sequence[str]] = {}
    semantic_product_identity = product_identity_hash or str(product.id)
    variable_view = {
        "product_id": semantic_product_identity,
        "requirements": [item.text for item in product.requirements],
        "acceptance_criteria": [item.description for item in product.acceptance_criteria],
        "components": components,
        "integration": (product.metadata or {}).get("integration"),
    }
    identity_hash = recipe_identity_hash or stored.canonical_hash
    prefix = _hash({"recipe": identity_hash, "variables": variable_view})[:16]
    for component in sorted(components, key=lambda item: str(item["key"])):
        key = str(component["key"])
        node_id = f"implement_{key}"
        task = TaskContract(
            id=TaskId(f"task_warm_{prefix}_{key}"),
            goal=str(component["goal"]),
            station_id=implementation.id,
            role_requirement=implementation.role,
            allowed_scope=tuple(str(v) for v in component["allowed_scope"]),
            forbidden_scope=(".git", "secrets", "hidden_tests"),
            acceptance_criteria=tuple(product.acceptance_criteria),
            verification_requirements=("scope", "syntax", "public", "private"),
            metadata={
                "task_class": "warm_start_component",
                "component_key": key,
                "sibling_interface": component["interface"],
                "adapted_from_recipe_version": stored.recipe.version,
            },
        )
        nodes.append(
            DagNode(
                node_id=node_id,
                task_id=str(task.id),
                station_id=str(implementation.id),
                role_hint="implementation",
                parallel_group="warm_start_components",
                metadata={
                    "selected_paths": list(component["selected_paths"]),
                    "produces_handoff_ids": [component["handoff_id"]],
                    "produces_handoff_types": [component["handoff_type"]],
                    "recipe_id": str(stored.recipe.id),
                    "recipe_version": stored.recipe.version,
                    "gate_policy_id": recipe_gate_id,
                    "gate_policy_version": recipe_gate_version,
                    "adapted_product_specific_fields": True,
                },
            )
        )
        tasks[node_id] = task
        station_map[node_id] = implementation
        components_by_node[node_id] = dict(component)
        produced[node_id] = (str(component["handoff_id"]),)
        produced_types[str(component["handoff_id"])] = (str(component["handoff_type"]),)
    integration_data = dict((product.metadata or {})["integration"])
    integration_task = TaskContract(
        id=TaskId(f"task_warm_{prefix}_integration_review"),
        goal=str(integration_data["goal"]),
        station_id=reviewer.id,
        role_requirement=reviewer.role,
        allowed_scope=tuple(str(v) for v in integration_data["allowed_scope"]),
        forbidden_scope=(".git", "secrets", "hidden_tests"),
        acceptance_criteria=tuple(product.acceptance_criteria),
        prerequisites=tuple(tasks[node].id for node in sorted(tasks)),
        verification_requirements=("integration_verification", "independent_final_reviewer"),
        metadata={
            "task_class": "warm_start_integration_review",
            "sibling_interface": integration_data["interface"],
            "adapted_from_recipe_version": stored.recipe.version,
        },
    )
    nodes.append(
        DagNode(
            node_id="integration_review",
            task_id=str(integration_task.id),
            station_id=str(reviewer.id),
            role_hint="integration_reviewer",
            metadata={
                "selected_paths": list(integration_data["selected_paths"]),
                "requires_handoff_ids": ["handoff_normalize", "handoff_reorder"],
                "requires_handoff_types": [
                    produced_types["handoff_normalize"][0],
                    produced_types["handoff_reorder"][0],
                ],
                "recipe_id": str(stored.recipe.id),
                "recipe_version": stored.recipe.version,
                "gate_policy_id": recipe_gate_id,
                "gate_policy_version": recipe_gate_version,
                "independent_reviewer_required": True,
            },
        )
    )
    tasks["integration_review"] = integration_task
    station_map["integration_review"] = reviewer
    changes = {
        "product_specific_task_text": {node: tasks[node].goal for node in sorted(tasks)},
        "allowed_file_targets": {
            node: list(components_by_node[node]["selected_paths"])
            for node in sorted(components_by_node)
        },
        "input_output_interfaces": {
            node: components_by_node[node]["interface"] for node in sorted(components_by_node)
        }
        | {"integration_review": integration_data["interface"]},
        "variable_acceptance_criteria": [item.description for item in product.acceptance_criteria],
        "sibling_specific_source_paths": sorted(
            path for item in components for path in item["selected_paths"]
        ),
        "worker_substitution": "declared deterministic fake-worker profile for offline trial",
    }
    manifest_body = {
        "policy_id": WARM_START_ADAPTATION_POLICY_ID,
        "policy_version": WARM_START_ADAPTATION_POLICY_VERSION,
        "recipe_id": str(stored.recipe.id),
        "recipe_version": stored.recipe.version,
        "canonical_recipe_hash": identity_hash,
        "sibling_product_id": str(product.id),
        "product_class_id": str(product_class.id),
        "source_graph_id": stored.provenance["source_graph_id"],
        "source_graph_hash": stored.provenance["source_graph_hash"],
        "topology_preserved": True,
        "station_responsibilities_preserved": True,
        "integration_preserved": True,
        "gate_policy_preserved": True,
        "changes": changes,
        "forbidden_changes": [
            "gate weakening",
            "product-class mutation",
            "StationContract responsibility mutation",
            "integration removal",
            "recipe lifecycle mutation",
            "unrecorded topology change",
        ],
    }
    manifest_identity_body = dict(manifest_body)
    manifest_identity_body["sibling_product_id"] = semantic_product_identity
    manifest_hash = _hash(manifest_identity_body)
    adaptation = AdaptationManifest(
        manifest_id=f"adaptation_{manifest_hash[:24]}",
        canonical_hash=manifest_hash,
        **manifest_body,
    )
    graph_identity = _hash({
        "product_id": semantic_product_identity,
        "recipe_id": str(stored.recipe.id),
        "recipe_version": stored.recipe.version,
        "adaptation_hash": manifest_hash,
    })
    dag = ProductDAG(
        id=f"run_warm_v0_{graph_identity[:32]}",
        product_id=product.id,
        revision=1,
        nodes=tuple(nodes),
        dependencies=(
            DagDependency(upstream="implement_normalize", downstream="integration_review", kind="hard"),
            DagDependency(upstream="implement_reorder", downstream="integration_review", kind="hard"),
        ),
        metadata={
            "compiler_policy_id": "redi08a_recipe_dag_compiler_v0",
            "compiler_policy_version": 1,
            "source_recipe_id": str(stored.recipe.id),
            "source_recipe_version": stored.recipe.version,
            "source_recipe_hash": identity_hash,
            "source_template_graph_id": stored.provenance["source_graph_id"],
            "source_template_graph_hash": stored.provenance["source_graph_hash"],
            "adaptation_manifest_id": adaptation.manifest_id,
            "adaptation_manifest_hash": adaptation.canonical_hash,
            "product_variable_hash": match.product_variable_hash,
            "cold_start_planner_used": False,
            "recipe_topology_consulted": True,
        },
        created_at=_utcnow(),
    )
    return WarmStartPlan(
        dag=dag,
        tasks_by_node=tasks,
        stations_by_node=station_map,
        required_handoffs_by_node={"integration_review": ("handoff_normalize", "handoff_reorder")},
        produced_handoffs_by_node=produced,
        produced_handoff_types_by_id=produced_types,
        components_by_node=components_by_node,
        integration_node_id="integration_review",
        adaptation=adaptation,
    )


_NORMALIZE_CODE = '''def normalize_vendor_code(vendor: str, code: str) -> str:
    if not isinstance(vendor, str) or not isinstance(code, str):
        raise TypeError("vendor and code must be strings")
    vendor_value = "-".join(vendor.strip().upper().replace("_", "-").split())
    code_value = "-".join(code.strip().upper().replace("_", "-").split())
    if not vendor_value or not code_value:
        raise ValueError("vendor and code must be non-empty")
    return f"{vendor_value}:{code_value}"
'''

_REORDER_CODE = '''def batch_reorder_quantity(on_hand: int, target: int, safety: int) -> int:
    values = (on_hand, target, safety)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise TypeError("inventory values must be integers")
    if any(value < 0 for value in values):
        raise ValueError("inventory values must be non-negative")
    return max(target + safety - on_hand, 0)
'''

_SERVICE_CODE = '''from .normalize import normalize_vendor_code
from .reorder import batch_reorder_quantity

def build_bulk_replenishment(vendor, code, on_hand, target, safety):
    return {
        "vendor_code": normalize_vendor_code(vendor, code),
        "quantity": batch_reorder_quantity(on_hand, target, safety),
        "safety_stock": safety,
    }
'''


def _gate(
    *, identity: str, node_id: str, task: TaskContract, status: GateStatus = GateStatus.PASS
) -> GateResult:
    return GateResult(
        gate_identity=identity,
        gate_type=GateType.DETERMINISTIC,
        target_node_id=node_id,
        target_task_id=task.id,
        criteria=(AcceptanceCriterion(description=f"{identity} must pass"),),
        status=status,
        verifier_type="deterministic_code",
        verifier_identity=f"{WARM_START_GATE_POLICY_ID}@{WARM_START_GATE_POLICY_VERSION}",
        metadata={
            "mandatory": True,
            "gate_policy_id": WARM_START_GATE_POLICY_ID,
            "gate_policy_version": WARM_START_GATE_POLICY_VERSION,
            "fake_worker": True,
            "provider_calls": 0,
            "network_calls": 0,
        },
        created_at=_utcnow(),
    )


@dataclass
class _WarmComponentWorker:
    product: ProductSpec
    task: TaskContract
    station: StationContract
    component: Mapping[str, Any]
    stored: StoredRecipeVersion
    worker_id: WorkerId
    calls: int = 0

    def run_node(self, *, handle: NodeWorkspaceHandle) -> WorkerExecutionResult:
        self.calls += 1
        key = str(self.component["key"])
        content = _NORMALIZE_CODE if key == "normalize" else _REORDER_CODE
        target = handle.root / str(self.component["target_path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        compile(content, str(target), "exec")
        if key == "normalize":
            namespace: dict[str, Any] = {}
            exec(content, namespace)
            assert namespace["normalize_vendor_code"]("acme corp", " a_12 ") == "ACME-CORP:A-12"
        else:
            namespace = {}
            exec(content, namespace)
            assert namespace["batch_reorder_quantity"](3, 10, 2) == 9
            assert namespace["batch_reorder_quantity"](20, 10, 2) == 0
        gates = tuple(
            _gate(identity=identity, node_id=handle.node_id, task=self.task)
            for identity in (
                "allowed_file_change_scope",
                "python_syntax_compile",
                "public_test_execution",
                "hidden_test_execution",
            )
        )
        evidence = RunEvidence(
            factory_run_id=FactoryRunId(str(handle.factory_run_id)),
            product_id=self.product.id,
            product_class_id=self.product.product_class_id,
            recipe_id=self.stored.recipe.id,
            recipe_version=self.stored.recipe.version,
            node_id=handle.node_id,
            station_id=self.station.id,
            task_class="warm_start_worker_attempt",
            worker_id=self.worker_id,
            worker_version="1",
            context_manifest_id=None,
            token_usage=None,
            gate_results=tuple(GateResultId(str(gate.id)) for gate in gates),
            retry_count=0,
            escalation=False,
            human_intervention=False,
            final_status=RunStatus.COMPLETED,
            metadata={
                "evidence_kind": "warm_start_worker_attempt",
                "execution_kind": "deterministic_fake_worker",
                "provider_calls": 0,
                "network_calls": 0,
                "provider_billed_tokens": {"input": 0, "output": 0, "cached_input": 0},
                "observed_source_workers": [
                    str(worker)
                    for workers in self.stored.provenance.get("source_attempt_trajectory", ())
                    for worker in [workers.get("worker_id")]
                    if worker
                ],
                "preferred_under_experimental_recipe": str(self.worker_id),
                "globally_best_claimed": False,
                "context_package_hash": handle.context_package.package_hash,
                "adapted_interface": self.component["interface"],
            },
            created_at=_utcnow(),
        )
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        contract = handle.handoff_output_contracts[str(self.component["handoff_id"])]
        descriptor = {
            "handoff_id": str(self.component["handoff_id"]),
            "artifact_id": f"artifact::{handle.node_id}::{self.component['handoff_id']}",
            "path": str(self.component["target_path"]),
            "sha256": digest,
            "kind": "file",
            "size_bytes": target.stat().st_size,
            "producer_node_id": handle.node_id,
            "producer_station_id": contract["producer_station_id"],
            "consumer_scope": contract["consumer_scope"],
            "metadata": {
                "sibling_product_id": str(self.product.id),
                "adapted_from_recipe_version": self.stored.recipe.version,
            },
        }
        return WorkerExecutionResult(
            node_id=handle.node_id,
            workspace_id=handle.workspace_id,
            success=True,
            produced_handoff_ids=(str(self.component["handoff_id"]),),
            produced_handoff_types_by_id={
                str(self.component["handoff_id"]): (str(self.component["handoff_type"]),)
            },
            artifacts=(descriptor,),
            execution_evidence={
                "worker_id": str(self.worker_id),
                "selection_policy_id": WARM_START_SELECTION_POLICY_ID,
                "selection_policy_version": WARM_START_SELECTION_POLICY_VERSION,
                "recipe_consulted": True,
                "sibling_requirements_preserved": True,
                "provider_calls": 0,
                "network_calls": 0,
            },
            attempt_evidence=(evidence,),
            gate_results=gates,
        )


@dataclass
class _WarmIntegrationReviewer:
    product: ProductSpec
    task: TaskContract
    station: StationContract
    stored: StoredRecipeVersion
    calls: int = 0

    def run_node(self, *, handle: NodeWorkspaceHandle) -> WorkerExecutionResult:
        self.calls += 1
        if {str(item.id) for item in handle.upstream_handoffs} != {
            "handoff_normalize",
            "handoff_reorder",
        }:
            raise WarmStartError("integration reviewer did not receive both governed handoffs")
        target = handle.root / "bulk_inventory/service.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_SERVICE_CODE, encoding="utf-8")
        compile(_SERVICE_CODE, str(target), "exec")
        gates = tuple(
            _gate(identity=identity, node_id=handle.node_id, task=self.task)
            for identity in (
                "allowed_file_change_scope",
                "python_syntax_compile",
                "public_test_execution",
                "hidden_test_execution",
                "integration_verification",
                "independent_final_reviewer",
            )
        )
        evidence = RunEvidence(
            factory_run_id=FactoryRunId(str(handle.factory_run_id)),
            product_id=self.product.id,
            product_class_id=self.product.product_class_id,
            recipe_id=self.stored.recipe.id,
            recipe_version=self.stored.recipe.version,
            node_id=handle.node_id,
            station_id=self.station.id,
            task_class="warm_start_independent_integration_review",
            worker_id=WorkerId("worker_warm_fake_independent_reviewer_v0"),
            worker_version="1",
            context_manifest_id=None,
            token_usage=None,
            gate_results=tuple(GateResultId(str(gate.id)) for gate in gates),
            retry_count=0,
            escalation=False,
            human_intervention=False,
            final_status=RunStatus.COMPLETED,
            metadata={
                "evidence_kind": "warm_start_independent_integration_review",
                "provider_calls": 0,
                "network_calls": 0,
                "local_reviewer": True,
                "sibling_interface": self.task.metadata["sibling_interface"],
            },
            created_at=_utcnow(),
        )
        return WorkerExecutionResult(
            node_id=handle.node_id,
            workspace_id=handle.workspace_id,
            success=True,
            execution_evidence={
                "worker_id": "worker_warm_fake_independent_reviewer_v0",
                "recipe_consulted": True,
                "integration_handoffs_verified": ["handoff_normalize", "handoff_reorder"],
                "provider_calls": 0,
                "network_calls": 0,
            },
            attempt_evidence=(evidence,),
            gate_results=gates,
        )


def _create_sibling_fixture(root: Path) -> Path:
    fixture = root / "sibling_fixture"
    package = fixture / "bulk_inventory"
    tests = fixture / "tests"
    package.mkdir(parents=True, exist_ok=True)
    tests.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    for name in ("normalize.py", "reorder.py", "service.py"):
        (package / name).write_text("# sibling-specific warm-start target\n", encoding="utf-8")
    (tests / "test_vendor_normalize_public.py").write_text(
        "# governed public contract: vendor namespace + normalized item code\n", encoding="utf-8"
    )
    (tests / "test_batch_reorder_public.py").write_text(
        "# governed public contract: target plus safety stock minus on-hand\n", encoding="utf-8"
    )
    (tests / "test_bulk_integration_public.py").write_text(
        "# governed public contract: batch mapping preserves safety_stock\n", encoding="utf-8"
    )
    return fixture


def _event(
    event_type: LifecycleEventType,
    *,
    factory_run_id: str,
    product_id: str,
    recipe_id: str,
    payload: Mapping[str, Any],
) -> LifecycleEvent:
    return LifecycleEvent(
        event_type=event_type,
        entity_ids={
            "factory_run_id": factory_run_id,
            "product_id": product_id,
            "recipe_id": recipe_id,
        },
        payload=dict(payload),
        timestamp=_utcnow(),
    )


def run_controlled_warm_start(
    *,
    output_root: Path | str,
    store: RecipeStore,
    recipe_id: str,
    recipe_version: int,
    product: ProductSpec,
    product_class: ProductClass,
) -> WarmStartRunOutcome:
    """Execute the exact offline warm-start path with deterministic fake workers."""

    output = Path(output_root).resolve()
    output.mkdir(parents=True, exist_ok=True)
    stored = store.read(recipe_id, recipe_version)
    match = match_product_class(product, product_class)
    available_stations = [str(station.id) for station in controlled_warm_stations()]
    preferred_workers = sorted({
        str(worker)
        for workers in stored.recipe.worker_preferences.values()
        for worker in workers
    })
    eligibility = evaluate_warm_start_eligibility(
        stored=stored,
        match=match,
        available_station_ids=available_stations,
        available_worker_ids=preferred_workers,
        available_context_strategies=(WARM_START_CONTEXT_STRATEGY_ID,),
        available_gate_policies=(WARM_START_GATE_POLICY_ID,),
    )
    if not eligibility.eligible:
        raise WarmStartError("warm-start selection blocked: " + "; ".join(eligibility.reasons))
    store.select_version(
        recipe_id=recipe_id,
        version=recipe_version,
        policy_id=WARM_START_SELECTION_POLICY_ID,
        policy_version=WARM_START_SELECTION_POLICY_VERSION,
        evidence={"match": match.to_dict(), "eligibility": eligibility.to_dict()},
        reason="explicit recipe version selected for controlled sibling dry execution",
    )
    plan = compile_warm_start_dag(
        product=product,
        product_class=product_class,
        stored=stored,
        match=match,
        eligibility=eligibility,
    )
    run_dir = output / f"warm-{_hash({'product': str(product.id), 'recipe': stored.canonical_hash})[:12]}"
    run_dir.mkdir(parents=True, exist_ok=True)
    db_path = run_dir / "evidence.sqlite"
    artifact_root = run_dir / "retained"
    fixture = _create_sibling_fixture(run_dir)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    bootstrap_schema(conn)
    ledger = EvidenceLedger(conn)
    runs = FactoryRunRepository(conn, ledger)
    now = _utcnow()
    factory_run = FactoryRun(
        product_spec_id=product.id,
        dag_id=str(plan.dag.id),
        recipe_id=stored.recipe.id,
        mode=Mode.WARM,
        final_status=RunStatus.RUNNING,
        verified=False,
        metadata={
            "acceptance": "REDI-08A",
            "execution_mode": WARM_START_KNOWN_PRODUCT,
            "warm_start_reason": "explicit_eligible_recipe_version_selected",
            "recipe_selection": {
                "recipe_id": recipe_id,
                "recipe_version": recipe_version,
                "canonical_recipe_hash": stored.canonical_hash,
                "match_policy_id": match.policy_id,
                "match_result": match.decision.value,
                "eligibility_policy_id": eligibility.policy_id,
                "confidence": stored.confidence.to_dict(),
                "validity_state": stored.revalidation_state,
                "source_provenance": dict(stored.provenance),
            },
            "accounting_semantics": ACCOUNTING_SEMANTICS,
            "provider_calls": 0,
            "network_calls": 0,
            "cold_start_planner_used": False,
        },
        created_at=now,
    )
    run_id = str(factory_run.id)
    runs.store(factory_run)
    ledger.append(
        _event(
            LifecycleEventType.PRODUCT_ACCEPTED,
            factory_run_id=run_id,
            product_id=str(product.id),
            recipe_id=recipe_id,
            payload={
                "mode": WARM_START_KNOWN_PRODUCT,
                "known_recipe": {"id": recipe_id, "version": recipe_version},
                "product_class_id": str(product_class.id),
            },
        ),
        factory_run_id=run_id,
    )
    lookup = RunEvidence(
        factory_run_id=FactoryRunId(run_id),
        product_id=product.id,
        product_class_id=product_class.id,
        recipe_id=stored.recipe.id,
        recipe_version=stored.recipe.version,
        node_id="RECIPE_LOOKUP",
        task_class="warm_start_recipe_lookup",
        final_status=RunStatus.COMPLETED,
        metadata={
            "evidence_kind": "warm_start_recipe_lookup",
            "mode": WARM_START_KNOWN_PRODUCT,
            "reason": "explicit_eligible_recipe_version_selected",
            "match": match.to_dict(),
            "eligibility": eligibility.to_dict(),
            "selected_recipe": {
                "recipe_id": recipe_id,
                "recipe_version": recipe_version,
                "canonical_recipe_hash": stored.canonical_hash,
                "lifecycle_state": stored.lifecycle_state.value,
                "confidence": stored.confidence.to_dict(),
                "validity_state": stored.revalidation_state,
                "canonical_recipe_json": domain_to_json(stored.recipe),
            },
            "source_provenance": dict(stored.provenance),
            "current_store_reselection_allowed_on_replay": False,
        },
        created_at=_utcnow(),
    )
    ledger.append(lookup, factory_run_id=run_id)
    adaptation_evidence = RunEvidence(
        factory_run_id=FactoryRunId(run_id),
        product_id=product.id,
        product_class_id=product_class.id,
        recipe_id=stored.recipe.id,
        recipe_version=stored.recipe.version,
        node_id="RECIPE_ADAPTATION",
        task_class="warm_start_recipe_adaptation",
        final_status=RunStatus.COMPLETED,
        metadata={
            "evidence_kind": "warm_start_adaptation_manifest",
            "adaptation_manifest": plan.adaptation.to_dict(),
            "sibling_product_snapshot": json.loads(domain_to_json(product)),
        },
        created_at=_utcnow(),
    )
    ledger.append(adaptation_evidence, factory_run_id=run_id)
    graph_hash = product_dag_hash(plan.dag)
    ledger.append(
        _event(
            LifecycleEventType.PLAN_COMPILED,
            factory_run_id=run_id,
            product_id=str(product.id),
            recipe_id=recipe_id,
            payload={
                "graph_id": str(plan.dag.id),
                "graph_revision": plan.dag.revision,
                "graph_hash": graph_hash,
                "recipe_id": recipe_id,
                "recipe_version": recipe_version,
                "adaptation_manifest_hash": plan.adaptation.canonical_hash,
                "cold_start_planner_used": False,
            },
        ),
        factory_run_id=run_id,
    )
    ledger.append(
        RunEvidence(
            factory_run_id=FactoryRunId(run_id),
            product_id=product.id,
            product_class_id=product_class.id,
            recipe_id=stored.recipe.id,
            recipe_version=stored.recipe.version,
            node_id="WARM_START_PLAN",
            task_class="warm_start_plan",
            final_status=RunStatus.COMPLETED,
            metadata={
                "evidence_kind": "warm_start_plan",
                "graph_id": str(plan.dag.id),
                "graph_revision": plan.dag.revision,
                "graph_hash": graph_hash,
                "dag_snapshot": dag_snapshot(plan.dag),
                "recipe_lineage": {
                    "recipe_id": recipe_id,
                    "recipe_version": recipe_version,
                    "canonical_recipe_hash": stored.canonical_hash,
                    "source_graph_id": stored.provenance["source_graph_id"],
                    "source_graph_hash": stored.provenance["source_graph_hash"],
                },
                "adaptation_manifest_id": plan.adaptation.manifest_id,
                "adaptation_manifest_hash": plan.adaptation.canonical_hash,
                "compiler_policy_id": "redi08a_recipe_dag_compiler_v0",
                "cold_start_planner_used": False,
            },
            created_at=_utcnow(),
        ),
        factory_run_id=run_id,
    )
    implementation, reviewer_station = controlled_warm_stations()
    normalize_worker = _WarmComponentWorker(
        product=product,
        task=plan.tasks_by_node["implement_normalize"],
        station=implementation,
        component=plan.components_by_node["implement_normalize"],
        stored=stored,
        worker_id=WorkerId("worker_warm_fake_normalize_v0"),
    )
    reorder_worker = _WarmComponentWorker(
        product=product,
        task=plan.tasks_by_node["implement_reorder"],
        station=implementation,
        component=plan.components_by_node["implement_reorder"],
        stored=stored,
        worker_id=WorkerId("worker_warm_fake_reorder_v0"),
    )
    reviewer = _WarmIntegrationReviewer(
        product=product,
        task=plan.tasks_by_node["integration_review"],
        station=reviewer_station,
        stored=stored,
    )
    executor = ParallelDagExecutor(
        dag=plan.dag,
        station_by_node=plan.stations_by_node,
        task_by_node=plan.tasks_by_node,
        node_workers={
            "implement_normalize": normalize_worker,
            "implement_reorder": reorder_worker,
            "integration_review": reviewer,
        },
        required_handoff_ids_by_node=plan.required_handoffs_by_node,
        produced_handoff_ids_by_node=plan.produced_handoffs_by_node,
        produced_handoff_types_by_id=plan.produced_handoff_types_by_id,
        fixture_root=fixture,
        product=product,
        max_parallelism=2,
        context_budget_bytes=64 * 1024,
        ledger=ledger,
        factory_run_id=run_id,
        artifact_root=artifact_root,
        workspace_allowed_scope_prefixes=("bulk_inventory", "tests"),
    )
    started = time.perf_counter()
    execution = executor.run()
    verified = bool(execution.final_states) and all(
        state == NodeRunState.COMPLETED for state in execution.final_states.values()
    )
    if verified:
        ledger.append(
            _event(
                LifecycleEventType.PRODUCT_VERIFIED,
                factory_run_id=run_id,
                product_id=str(product.id),
                recipe_id=recipe_id,
                payload={
                    "graph_id": str(plan.dag.id),
                    "graph_revision": plan.dag.revision,
                    "graph_hash": graph_hash,
                    "recipe_id": recipe_id,
                    "recipe_version": recipe_version,
                    "adaptation_manifest_hash": plan.adaptation.canonical_hash,
                    "gate_policy_id": WARM_START_GATE_POLICY_ID,
                    "independent_reviewer": "worker_warm_fake_independent_reviewer_v0",
                    "time_to_verified_seconds": round(time.perf_counter() - started, 6),
                    "transfer_claimed": False,
                },
            ),
            factory_run_id=run_id,
        )
    accounting = RunEvidence(
        factory_run_id=FactoryRunId(run_id),
        product_id=product.id,
        product_class_id=product_class.id,
        recipe_id=stored.recipe.id,
        recipe_version=stored.recipe.version,
        node_id="RUN_ACCOUNTING",
        task_class="warm_start_accounting",
        final_status=RunStatus.COMPLETED if verified else RunStatus.FAILED,
        token_usage=TokenUsage(input=0, output=0, cached_input=0),
        timing=Timing(
            wall_clock_seconds=execution.timing["graph_wall_clock_seconds"],
            compute_seconds=execution.timing["aggregate_node_compute_seconds"],
        ),
        metadata={
            "evidence_kind": "warm_start_accounting",
            "semantics": ACCOUNTING_SEMANTICS,
            "provider": {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0},
            "local_compute": {
                "aggregate_node_compute_seconds": execution.timing["aggregate_node_compute_seconds"],
                "graph_wall_clock_seconds": execution.timing["graph_wall_clock_seconds"],
                "fake_worker_calls": normalize_worker.calls + reorder_worker.calls + reviewer.calls,
                "synthetic_token_counter_used": False,
            },
        },
        created_at=_utcnow(),
    )
    ledger.append(accounting, factory_run_id=run_id)
    runs.close_run(
        run_id,
        final_status=RunStatus.COMPLETED if verified else RunStatus.FAILED,
        verified=verified,
        total_tokens=TokenUsage(input=0, output=0, cached_input=0),
        timing=Timing(
            started_at=now,
            ended_at=_utcnow(),
            wall_clock_seconds=execution.timing["graph_wall_clock_seconds"],
            compute_seconds=execution.timing["aggregate_node_compute_seconds"],
        ),
    )
    conn.close()
    return WarmStartRunOutcome(
        db_path=db_path,
        factory_run_id=run_id,
        product=product,
        recipe=stored,
        match=match,
        eligibility=eligibility,
        plan=plan,
        execution=execution,
        verified=verified,
        provider_calls=0,
        network_calls=0,
        worker_calls={
            "worker_warm_fake_normalize_v0": normalize_worker.calls,
            "worker_warm_fake_reorder_v0": reorder_worker.calls,
            "worker_warm_fake_independent_reviewer_v0": reviewer.calls,
        },
    )


def lookup_incompatible_product(
    *,
    product: ProductSpec,
    product_class: ProductClass,
    stored: StoredRecipeVersion,
    store: RecipeStore | None = None,
) -> dict[str, Any]:
    match = match_product_class(product, product_class)
    selected = match.decision == MatchDecision.COMPATIBLE
    result = {
        "policy_id": WARM_START_MATCH_POLICY_ID,
        "policy_version": WARM_START_MATCH_POLICY_VERSION,
        "product_id": str(product.id),
        "recipe_id": str(stored.recipe.id),
        "recipe_version": stored.recipe.version,
        "match": match.to_dict(),
        "recipe_selected": selected,
        "warm_start_allowed": selected,
        "next_mode": WARM_START_KNOWN_PRODUCT if selected else "COLD_START_REQUIRED",
        "reason": (
            "compatible experimental recipe selected"
            if selected else "deterministic product-class incompatibility"
        ),
    }
    if store is not None:
        store.record_lookup_decision(
            recipe_id=str(stored.recipe.id),
            recipe_version=stored.recipe.version,
            compatible=selected,
            policy_id=WARM_START_MATCH_POLICY_ID,
            policy_version=WARM_START_MATCH_POLICY_VERSION,
            evidence=result,
            reason=str(result["reason"]),
        )
    return result


def replay_warm_start(*, db_path: Path | str, factory_run_id: str) -> dict[str, Any]:
    """Read-only historical replay; never consults the current Recipe Store."""

    path = Path(db_path).resolve()
    reader = ReplayReader(path)
    try:
        base = reader.replay_factory_run(FactoryRunId(factory_run_id))
    finally:
        reader.close()
    if base.integrity_status != "OK":
        raise WarmStartError(f"warm-start replay integrity is {base.integrity_status}")
    kinds: dict[str, list[RunEvidence]] = {}
    for evidence in base.run_evidences:
        kind = str((evidence.metadata or {}).get("evidence_kind") or "")
        if kind:
            kinds.setdefault(kind, []).append(evidence)
    for required in (
        "warm_start_recipe_lookup",
        "warm_start_adaptation_manifest",
        "warm_start_plan",
        "warm_start_accounting",
    ):
        if len(kinds.get(required, ())) != 1:
            raise WarmStartError(f"replay requires exactly one {required} record")
    lookup = kinds["warm_start_recipe_lookup"][0].metadata
    adaptation = kinds["warm_start_adaptation_manifest"][0].metadata
    plan = kinds["warm_start_plan"][0].metadata
    accounting = kinds["warm_start_accounting"][0].metadata
    dag_rows = [
        dict(item.metadata)
        for item in base.run_evidences
        if item.task_class == "dag_orchestration"
    ]
    dag = replay_dag_run(
        dag_rows,
        expected_graph_hash=str(plan["graph_hash"]),
        expected_revision=int(plan["graph_revision"]),
        retained_artifact_root=path.parent / "retained",
    )
    events = [event.event_type.value for event in base.ordered_events]
    return {
        "factory_run_id": factory_run_id,
        "integrity_status": base.integrity_status,
        "observed_run_status": base.observed_run_status,
        "verified": base.final_verification_state,
        "mode": lookup["mode"],
        "reason": lookup["reason"],
        "match": lookup["match"],
        "eligibility": lookup["eligibility"],
        "selected_recipe": lookup["selected_recipe"],
        "source_provenance": lookup["source_provenance"],
        "adaptation_manifest": adaptation["adaptation_manifest"],
        "sibling_product_snapshot": adaptation["sibling_product_snapshot"],
        "dag_lineage": dict(plan["recipe_lineage"])
        | {
            "adaptation_manifest_id": plan["adaptation_manifest_id"],
            "adaptation_manifest_hash": plan["adaptation_manifest_hash"],
        },
        "dag": canonical_replay_view(dag),
        "worker_attempts": [
            {
                "evidence_id": str(item.id),
                "node_id": item.node_id,
                "worker_id": str(item.worker_id) if item.worker_id else None,
                "status": item.final_status.value if item.final_status else None,
                "context_manifest_id": str(item.context_manifest_id) if item.context_manifest_id else None,
                "gate_result_ids": [str(v) for v in item.gate_results],
                "metadata": _canonical(item.metadata),
            }
            for item in base.run_evidences
            if item.task_class in {
                "warm_start_worker_attempt",
                "warm_start_independent_integration_review",
            }
        ],
        "gates": [
            {
                "gate_id": str(gate.id),
                "identity": gate.gate_identity,
                "status": gate.status.value,
                "policy_id": (gate.metadata or {}).get("gate_policy_id"),
            }
            for gate in base.gate_results
        ],
        "accounting": accounting,
        "product_verified_event": LifecycleEventType.PRODUCT_VERIFIED.value in events,
        "recipe_promoted_event": LifecycleEventType.RECIPE_PROMOTED.value in events,
        "replay_dependencies": {
            "recipe_store_consulted": False,
            "current_matching_config_consulted": False,
            "current_adaptation_config_consulted": False,
            "workers_executed": 0,
            "provider_calls": 0,
            "network_calls": 0,
        },
    }


def canonical_warm_start_replay_view(replay: Mapping[str, Any]) -> dict[str, Any]:
    return _canonical(replay)


def resolve_warm_start_provenance(
    *,
    warm_db_path: Path | str,
    warm_factory_run_id: str,
    stored: StoredRecipeVersion,
    source_ledger: Path | str,
) -> dict[str, bool]:
    warm = replay_warm_start(db_path=warm_db_path, factory_run_id=warm_factory_run_id)
    source = replay_cold_start(
        db_path=source_ledger,
        factory_run_id=str(stored.provenance["source_factory_run_id"]),
    )
    candidate = source["recipe_candidate"]
    adaptation = warm["adaptation_manifest"]
    checks = {
        "warm_run_to_recipe": (
            warm["selected_recipe"]["recipe_id"] == str(stored.recipe.id)
            and warm["selected_recipe"]["recipe_version"] == stored.recipe.version
        ),
        "warm_run_to_recipe_hash": warm["selected_recipe"]["canonical_recipe_hash"] == stored.canonical_hash,
        "recipe_to_source_candidate": stored.provenance["source_candidate_canonical_hash"] == candidate["canonical_recipe_sha256"],
        "recipe_to_source_factory_run": candidate["source_factory_run_id"] == stored.provenance["source_factory_run_id"],
        "recipe_to_source_product_verified": source["product_verified_event"] is True,
        "recipe_to_source_dag": candidate["source_graph_hash"] == stored.provenance["source_graph_hash"],
        "source_evidence_refs_resolved": bool(stored.provenance["all_candidate_evidence_refs_resolved"]),
        "sibling_dag_to_recipe": warm["dag_lineage"]["canonical_recipe_hash"] == stored.canonical_hash,
        "sibling_dag_to_adaptation": warm["dag_lineage"]["adaptation_manifest_hash"] == adaptation["canonical_hash"],
        "adaptation_to_sibling_product": adaptation["sibling_product_id"] == warm["sibling_product_snapshot"]["id"],
    }
    return checks


def build_ab_experiment_manifest(
    *,
    sibling_product: ProductSpec,
    stored: StoredRecipeVersion,
    source_revision: str,
) -> ExperimentManifest:
    controlled = {
        "product_spec_id": str(sibling_product.id),
        "product_spec_sha256": _hash(json.loads(domain_to_json(sibling_product))),
        "source_revision": source_revision,
        "gate_policy_id": WARM_START_GATE_POLICY_ID,
        "gate_policy_version": WARM_START_GATE_POLICY_VERSION,
        "verification_profile": "strict_public_private_integration_independent_review",
        "environment_profile": "controlled_python_offline",
        "allowed_worker_universe": [
            "worker_warm_fake_normalize_v0",
            "worker_warm_fake_reorder_v0",
            "worker_warm_fake_independent_reviewer_v0",
        ],
    }
    metrics = [
        "verified_product_success",
        "provider_input_tokens",
        "provider_output_tokens",
        "provider_call_count",
        "wall_clock_seconds",
        "aggregate_worker_compute_seconds",
        "retries",
        "escalations",
        "frontier_worker_intervention",
        "context_tokens",
        "graph_revisions",
        "human_intervention",
    ]
    return ExperimentManifest(
        title="REDI-08 future sibling cold-start versus warm-start comparison",
        hypothesis_reference="H6",
        research_question="Can an eligible recipe improve a controlled sibling run without reducing quality?",
        arms={
            "A_cold_start": {
                "mode": "COLD_START_UNKNOWN_PRODUCT",
                "recipe": None,
                "controlled_variables": controlled,
            },
            "B_warm_start": {
                "mode": WARM_START_KNOWN_PRODUCT,
                "recipe": {
                    "id": str(stored.recipe.id),
                    "version": stored.recipe.version,
                    "canonical_hash": stored.canonical_hash,
                    "lifecycle": stored.lifecycle_state.value,
                },
                "controlled_variables": controlled,
            },
        },
        task_selection={"exact_product_spec_id": str(sibling_product.id)},
        environment=EnvironmentInfo(
            os_description="controlled",
            python_version="3.11+",
            metadata={"network": "disabled", "provider": "disabled"},
        ),
        acceptance_criteria=(
            AcceptanceCriterion(description="Both arms use identical quality gates."),
            AcceptanceCriterion(description="Metrics remain separate; no composite score."),
        ),
        measurement_plan={"metrics": metrics, "composite_score": False},
        human_intervention_policy={"record_every_intervention": True},
        replay_policy={"historical_config_authoritative": True, "read_only_replay": True},
        validity_controls={
            "same_product": True,
            "same_source_revision": True,
            "same_gates": True,
            "same_verification": True,
            "same_environment": True,
            "same_allowed_worker_universe": True,
            "comparison_executed": False,
            "advantage_claimed": False,
        },
        metadata={
            "foundation_only": True,
            "preregister_before_execution": True,
            "accounting_semantics": ACCOUNTING_SEMANTICS,
        },
        preregistered_at=_utcnow(),
        created_at=_utcnow(),
    )


__all__ = [
    "ACCOUNTING_SEMANTICS",
    "AdaptationManifest",
    "MatchDecision",
    "ProductClassMatch",
    "WARM_START_ADAPTATION_POLICY_ID",
    "WARM_START_ELIGIBILITY_POLICY_ID",
    "WARM_START_KNOWN_PRODUCT",
    "WARM_START_MATCH_POLICY_ID",
    "WARM_START_ROLLBACK_POLICY_ID",
    "WarmStartEligibility",
    "WarmStartError",
    "WarmStartPlan",
    "WarmStartRunOutcome",
    "build_ab_experiment_manifest",
    "canonical_warm_start_replay_view",
    "compile_warm_start_dag",
    "controlled_warm_stations",
    "evaluate_warm_start_eligibility",
    "import_candidate_from_ledger",
    "incompatible_product_fixture",
    "inventory_product_class",
    "lookup_incompatible_product",
    "match_product_class",
    "original_product_fixture",
    "replay_warm_start",
    "resolve_warm_start_provenance",
    "run_controlled_warm_start",
    "sibling_product_fixture",
]
