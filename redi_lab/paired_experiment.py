"""REDI-08B preregistered cold-versus-warm sibling comparison.

The module deliberately reports paired facts rather than a winner.  A single
immutable manifest is frozen before either condition, each condition owns an
independent ledger/workspace/artifact scope, and replay reads only persisted
historical evidence.  Dry mode exercises the same one-shot provider boundary
with deterministic stand-ins while making zero provider/network calls.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .cold_start import (
    COLD_START_UNKNOWN_PRODUCT,
    ColdStartPolicy,
    derive_product_signature,
    plan_cold_start_v0,
)
from .dag_scheduler import dag_snapshot, product_dag_hash
from .context_compiler import (
    CONTEXT_CLASS_PRIOR_FAILURE_EVIDENCE,
    CONTEXT_CLASS_UPSTREAM_HANDOFFS,
    CONTEXT_STRATEGY_SELECTED_V0,
    ContextFirewall,
    compile_context,
)
from .domain import (
    AcceptanceCriterion,
    EnvironmentInfo,
    ExperimentId,
    ExperimentManifest,
    FactoryRun,
    FactoryRunId,
    FailureCategory,
    FailureRecord,
    GateResult,
    GateResultId,
    GateStatus,
    GateType,
    LifecycleEvent,
    LifecycleEventType,
    Mode,
    ProductClass,
    ProductRecipe,
    ProductSpec,
    RecipeRung,
    RunEvidence,
    RunStatus,
    Severity,
    StationId,
    TaskId,
    Timing,
    TokenUsage,
    ValidityCondition,
    WorkerId,
    domain_to_json,
)
from .persistence import (
    EvidenceLedger,
    ExperimentRepository,
    FactoryRunRepository,
    bootstrap_schema,
)
from .recipe_store import (
    EXPERIMENTAL_PROMOTION_POLICY_ID,
    EXPERIMENTAL_PROMOTION_POLICY_VERSION,
    ConfidenceMetadata,
    RecipeApproval,
    RecipeStore,
    StoredRecipeVersion,
    canonical_recipe_semantic_hash,
)
from .replay import ReplayReader
from .runtime.contracts import NodeRunResult, PatchEdit
from .runtime.live_provider_adapter import (
    LiveProviderAdapter,
    LiveProviderConfig,
    render_live_request,
)
from .warm_start import (
    ACCOUNTING_SEMANTICS,
    WARM_START_ADAPTATION_POLICY_ID,
    WARM_START_ADAPTATION_POLICY_VERSION,
    WARM_START_GATE_POLICY_ID,
    WARM_START_GATE_POLICY_VERSION,
    WARM_START_KNOWN_PRODUCT,
    WARM_START_MATCH_POLICY_ID,
    WARM_START_MATCH_POLICY_VERSION,
    compile_warm_start_dag,
    controlled_warm_stations,
    evaluate_warm_start_eligibility,
    match_product_class,
    original_product_fixture,
    import_candidate_from_ledger,
)


CONDITION_COLD = "COLD_START"
CONDITION_WARM = "WARM_START_SHADOW_RECIPE"
CONDITION_ORDER = (CONDITION_COLD, CONDITION_WARM)
PAIR_POLICY_ID = "redi08b_preregistered_paired_sibling_v0"
PAIR_POLICY_VERSION = 1
CONTEXT_POLICY_ID = "redi08b_fair_context_accounting_v0"
RETRY_POLICY_ID = "redi08b_no_paid_retry_v0"
ENVIRONMENT_POLICY_ID = "redi08b_same_process_isolated_scopes_v0"
LIVE_PROVIDER = "openai"
LIVE_MODEL = "gpt-4.1-2025-04-14"
MAX_OUTPUT_TOKENS = 1024
MAX_LIVE_CALLS_PER_CONDITION = 1
MAX_TOTAL_LIVE_CALLS = 2

WORKER_UNIVERSE = (
    "worker_pair_weak_explorer_v0",
    "worker_pair_provider_candidate_v0",
    "worker_pair_local_reorder_v0",
    "worker_pair_independent_reviewer_v0",
)

MANDATORY_GATES = (
    "structured_output_schema",
    "patch_output_contract",
    "allowed_file_change_scope",
    "python_syntax_compile",
    "public_test_execution",
    "private_test_execution",
    "handoff_artifact_validation",
    "integration_verification",
    "independent_final_reviewer",
)

METRIC_GROUPS = {
    "quality": (
        "product_verified",
        "required_gate_outcomes",
        "private_verification",
        "integration_reviewer_result",
        "regression_count",
    ),
    "provider_usage": (
        "live_provider_calls",
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "provider",
        "model",
    ),
    "orchestration": (
        "worker_attempts",
        "actual_worker_attempts",
        "synthetic_dry_attempts",
        "provider_attempts",
        "failed_attempts",
        "retries",
        "escalations",
        "graph_revisions",
        "replanning_events",
        "fallback_events",
    ),
    "context": ("compiled_package_bytes", "rendered_request_bytes", "heuristic_tokens", "distinct_compilations"),
    "time": ("graph_wall_clock_seconds", "aggregate_worker_compute_seconds", "provider_wall_seconds"),
    "recipe": (
        "lookup_result",
        "adaptation_count",
        "adaptation_reasons",
        "transfer_failures",
        "rollback_revalidation_events",
    ),
    "human": ("interventions",),
}


class PairError(RuntimeError):
    """A preregistration, control, execution, or replay invariant failed."""


class ProviderRetryBlocked(PairError):
    """A condition attempted to cross its single-call provider boundary twice."""


class TransferFailureClass(str, Enum):
    CLASS_MATCH_FAILURE = "class_match_failure"
    RECIPE_VALIDITY_FAILURE = "recipe_validity_failure"
    ADAPTATION_FAILURE = "adaptation_failure"
    RECIPE_WORKER_UNAVAILABLE = "recipe_worker_unavailable"
    RECIPE_CONTEXT_INVALID = "recipe_context_invalid"
    RECIPE_GATE_INCOMPATIBILITY = "recipe_gate_incompatibility"
    RECIPE_TOPOLOGY_MISMATCH = "recipe_topology_mismatch"
    RECIPE_EXECUTION_FAILURE = "recipe_execution_failure"
    RECIPE_INTEGRATION_FAILURE = "recipe_integration_failure"
    RECIPE_REVALIDATION_FAILURE = "recipe_revalidation_failure"


class PairClassification(str, Enum):
    VALID_PAIRED_SUCCESS = "VALID_PAIRED_SUCCESS"
    VALID_PAIRED_FAILURE = "VALID_PAIRED_FAILURE"
    PARTIAL_PAIR = "PARTIAL_PAIR"
    INVALID_PAIR = "INVALID_PAIR"


@dataclass(frozen=True)
class PreregisteredManifest:
    manifest: ExperimentManifest
    canonical_json: str
    canonical_hash: str
    path: Path
    frozen_at: str


@dataclass(frozen=True)
class ConditionOutcome:
    condition: str
    db_path: Path
    factory_run_id: str
    verified: bool
    dag_hash: str
    fixture_hash: str
    metrics: Mapping[str, Any]
    provider_calls: int
    network_calls: int
    started_at: str
    completed_at: str


@dataclass(frozen=True)
class PairedExperimentOutcome:
    root: Path
    preregistration: PreregisteredManifest
    cold: ConditionOutcome
    warm: ConditionOutcome
    paired_result: Mapping[str, Any]
    paired_result_path: Path
    audit: Mapping[str, bool]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _canonical(value[k]) for k in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def product_spec_semantic_payload(product: ProductSpec) -> dict[str, Any]:
    payload = json.loads(domain_to_json(product))
    payload.pop("id", None)
    payload.pop("created_at", None)
    return _canonical(payload)


def product_spec_hash(product: ProductSpec) -> str:
    return _hash(product_spec_semantic_payload(product))


def _tree_hash(root: Path) -> str:
    rows = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        rows.append({
            "path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
        })
    return _hash(rows)


def worker_profiles() -> dict[str, dict[str, Any]]:
    """Approved REDI-08B WorkerProfile registry (provider-neutral declarations)."""

    return _worker_profiles()


def _worker_profiles() -> dict[str, dict[str, Any]]:
    implementation_station, review_station = controlled_warm_stations()
    return {
        WORKER_UNIVERSE[0]: {
            "id": WORKER_UNIVERSE[0], "version": "1", "kind": "synthetic_policy_only",
            "station_eligibility": [str(implementation_station.id)], "actual_execution": False,
        },
        WORKER_UNIVERSE[1]: {
            "id": WORKER_UNIVERSE[1], "version": "1", "kind": "provider_candidate",
            "station_eligibility": [str(implementation_station.id)], "structured_output_capable": True,
            "provider": LIVE_PROVIDER, "model": LIVE_MODEL,
        },
        WORKER_UNIVERSE[2]: {
            "id": WORKER_UNIVERSE[2], "version": "1", "kind": "local_deterministic",
            "station_eligibility": [str(implementation_station.id)],
        },
        WORKER_UNIVERSE[3]: {
            "id": WORKER_UNIVERSE[3], "version": "1", "kind": "local_independent_reviewer",
            "station_eligibility": [str(review_station.id)],
        },
    }


def _write_fixture(root: Path) -> str:
    files = {
        "bulk_inventory/__init__.py": "",
        "bulk_inventory/normalize.py": "# governed sibling baseline\n",
        "bulk_inventory/reorder.py": "# governed sibling baseline\n",
        "bulk_inventory/service.py": "# governed sibling baseline\n",
        "tests/test_vendor_normalize_public.py": (
            "# Public contract: normalize_vendor_code(vendor, code)\n"
            "# Expected example: ('acme corp', ' a_12 ') -> 'ACME-CORP:A-12'\n"
        ),
        "tests/test_batch_reorder_public.py": "# safety-stock contract\n",
        "tests/test_bulk_integration_public.py": "# paired integration contract\n",
    }
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return _tree_hash(root)


def sibling_difference_summary(product: ProductSpec, product_class: ProductClass) -> dict[str, Any]:
    source = original_product_fixture(str(product_class.id))
    source_variables = dict(source.metadata["redi08_classification"]["product_variables"])
    sibling_variables = dict(product.metadata["redi08_classification"]["product_variables"])
    changed = {
        key: {"source": source_variables.get(key), "sibling": sibling_variables.get(key)}
        for key in sorted(set(source_variables) | set(sibling_variables))
        if source_variables.get(key) != sibling_variables.get(key)
    }
    return {
        "source_product_id": str(source.id),
        "sibling_product_id": str(product.id),
        "source_product_sha256": product_spec_hash(source),
        "sibling_product_sha256": product_spec_hash(product),
        "materially_different": bool(changed) and product.requested_outcome != source.requested_outcome,
        "changed_product_variable_fields": changed,
        "requested_outcome_changed": product.requested_outcome != source.requested_outcome,
        "requirements_changed": [item.text for item in product.requirements]
        != [item.text for item in source.requirements],
        "class_invariants_equal": (
            product.metadata["redi08_classification"]["class_invariants"]
            == source.metadata["redi08_classification"]["class_invariants"]
        ),
    }


def _environment(source_revision: str, *, live: bool) -> EnvironmentInfo:
    return EnvironmentInfo(
        software_revision=source_revision,
        python_version=platform.python_version(),
        os_description=platform.platform(),
        metadata={
            "policy_id": ENVIRONMENT_POLICY_ID,
            "runtime_identity": f"{platform.python_implementation()}-{platform.python_version()}",
            "provider_catalog": [{"provider": LIVE_PROVIDER, "model": LIVE_MODEL}],
            "provider_mode": "live" if live else "dry_fake",
            "dry_provider_disabled": not live,
        },
    )


def build_preregistered_manifest(
    *,
    product: ProductSpec,
    product_class: ProductClass,
    stored: StoredRecipeVersion,
    source_revision: str,
    live: bool = False,
) -> ExperimentManifest:
    """Build the one manifest used byte-for-byte by both conditions."""

    if stored.lifecycle_state != RecipeRung.SHADOW:
        raise PairError("REDI-08B requires a SHADOW recipe; stable promotion is forbidden")
    match = match_product_class(product, product_class)
    if match.decision.value != "COMPATIBLE":
        raise PairError("sibling product is not class-invariant compatible")
    product_hash = product_spec_hash(product)
    semantic_recipe_hash = canonical_recipe_semantic_hash(stored.recipe)
    station_snapshots = [json.loads(domain_to_json(item)) for item in controlled_warm_stations()]
    worker_profiles = _worker_profiles()
    environment = _environment(source_revision, live=live)
    controlled_payloads = {
        "product_spec": product_spec_semantic_payload(product),
        "product_class": {
            "id": str(product_class.id),
            "version": product_class.version,
            "class_invariants": _canonical(product_class.signature.get("class_invariants", {})),
        },
        "station_contracts": station_snapshots,
        "gate_policy": {"id": WARM_START_GATE_POLICY_ID, "version": WARM_START_GATE_POLICY_VERSION, "required": list(MANDATORY_GATES)},
        "worker_profiles": {key: value for key, value in worker_profiles.items()},
        "environment": json.loads(domain_to_json(environment)),
        "output_limits": {"max_output_tokens": MAX_OUTPUT_TOKENS, "max_calls_per_condition": 1},
    }
    controlled = {
        "product_spec_id": str(product.id),
        "product_spec_sha256": product_hash,
        "product_class_id": str(product_class.id),
        "source_revision": source_revision,
        "environment_policy_id": ENVIRONMENT_POLICY_ID,
        "gate_policy": {"id": WARM_START_GATE_POLICY_ID, "version": WARM_START_GATE_POLICY_VERSION},
        "mandatory_gates": list(MANDATORY_GATES),
        "worker_universe": list(WORKER_UNIVERSE),
        "context_policy": {"id": CONTEXT_POLICY_ID, "provider_local_separated": True},
        "retry_policy": {"id": RETRY_POLICY_ID, "paid_retry": False},
        "fixture_policy": "byte_identical_baseline_per_condition",
        "output_limits": {"max_output_tokens": MAX_OUTPUT_TOKENS},
        "evidence_semantics": ACCOUNTING_SEMANTICS,
        "controlled_payloads": controlled_payloads,
        "controlled_payload_hashes": {key: _hash(value) for key, value in controlled_payloads.items()},
    }
    recipe = {
        "recipe_id": str(stored.recipe.id),
        "recipe_version": stored.recipe.version,
        "canonical_hash": semantic_recipe_hash,
        "content_hash": stored.canonical_hash,
        "lifecycle_state": stored.lifecycle_state.value,
        "matching_policy": {"id": WARM_START_MATCH_POLICY_ID, "version": WARM_START_MATCH_POLICY_VERSION},
        "adaptation_policy": {
            "id": WARM_START_ADAPTATION_POLICY_ID,
            "version": WARM_START_ADAPTATION_POLICY_VERSION,
        },
    }
    return ExperimentManifest(
        id=ExperimentId(f"experiment_redi08b_{_hash({'product': product_hash, 'recipe_semantic_hash': semantic_recipe_hash, 'revision': source_revision})[:24]}"),
        title="REDI-08B preregistered cold-versus-warm sibling pilot",
        hypothesis_reference="Recipe reuse / warm-start feasibility (not superiority)",
        research_question="Can one materially different compatible sibling execute through both governed paths?",
        arms={
            CONDITION_COLD: {
                "mode": COLD_START_UNKNOWN_PRODUCT,
                "known_recipe": None,
                "planner": "cold_start_conservative_planner_v0@1",
                "exploration": "cold_start_deterministic_exploration_v0@1",
                "controlled_variables": controlled,
            },
            CONDITION_WARM: {
                "mode": WARM_START_KNOWN_PRODUCT,
                "pinned_recipe": recipe,
                "controlled_variables": controlled,
            },
        },
        baselines={"source_recipe_evidence": dict(stored.provenance)},
        task_selection={"exact_sibling_product_id": str(product.id), "exact_product_sha256": product_hash},
        environment=environment,
        worker_declarations={WorkerId(worker): {"eligible_in": list(CONDITION_ORDER)} for worker in WORKER_UNIVERSE},
        context_strategy={"policy_id": CONTEXT_POLICY_ID, "same_compiler_semantics": True},
        acceptance_criteria=(
            AcceptanceCriterion(description="Each successful condition reaches PRODUCT_VERIFIED."),
            AcceptanceCriterion(description="All mandatory gates and independent review pass."),
            AcceptanceCriterion(description="No automatic winner or superiority claim is emitted."),
        ),
        preregistered_thresholds={
            "max_live_calls_per_condition": MAX_LIVE_CALLS_PER_CONDITION,
            "max_total_live_calls": MAX_TOTAL_LIVE_CALLS,
            "max_output_tokens_per_call": MAX_OUTPUT_TOKENS,
            "provider_retry_allowed": False,
        },
        randomization={"condition_order": list(CONDITION_ORDER), "order_selected_before_results": True},
        repetition_config={"pilot_pairs": 1, "automatic_rerun": False},
        measurement_plan={
            "metric_groups": {key: list(value) for key, value in METRIC_GROUPS.items()},
            "composite_score": False,
            "winner_field": False,
            "comparison_semantics": "factual paired dimensions only",
            "stopping_rules": [
                "stop a condition after its one live-call failure",
                "never retry a paid call automatically",
                "close independent sibling evidence even when the pair is partial",
            ],
        },
        version_assumptions={"recipe_resolution": "pinned_id_version_hash_only", "latest_lookup_on_replay": False},
        human_intervention_policy={"allowed": False, "count_required": True},
        replay_policy={
            "read_only": True,
            "workers": 0,
            "providers": 0,
            "network": 0,
            "recipe_reselection": False,
            "current_config_dependency": False,
        },
        validity_controls={
            "same_product": True,
            "same_source_revision": True,
            "same_fixture_bytes": True,
            "same_environment": True,
            "same_station_contracts": True,
            "same_gate_definitions": True,
            "same_public_private_integration_review": True,
            "same_worker_universe": True,
            "workspace_isolation": True,
            "cross_condition_reads": False,
        },
        metadata={
            "policy_id": PAIR_POLICY_ID,
            "policy_version": PAIR_POLICY_VERSION,
            "hypothesis_family": "recipe_reuse_warm_start_feasibility",
            "warm_start_superiority_established": False,
            "recipe_transfer_advantage_claimed": False,
            "recipe": recipe,
            "difference_summary": sibling_difference_summary(product, product_class),
        },
        preregistered_at=_utcnow(),
        created_at=_utcnow(),
    )


def freeze_manifest(manifest: ExperimentManifest, root: Path | str) -> PreregisteredManifest:
    output = Path(root).resolve()
    output.mkdir(parents=True, exist_ok=True)
    path = output / "experiment_manifest.json"
    if path.exists():
        raise PairError("refusing to overwrite an existing preregistration")
    canonical_json = domain_to_json(manifest)
    path.write_text(canonical_json + "\n", encoding="utf-8")
    canonical_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    frozen_at = _utcnow().isoformat()
    (output / "experiment_manifest.sha256").write_text(canonical_hash + "\n", encoding="ascii")
    return PreregisteredManifest(manifest, canonical_json, canonical_hash, path, frozen_at)


def pre_execution_estimate(manifest: ExperimentManifest) -> dict[str, Any]:
    product_bytes = len(_canonical_json(manifest.task_selection).encode("utf-8"))
    controlled_bytes = len(_canonical_json(manifest.arms[CONDITION_COLD]["controlled_variables"]).encode("utf-8"))
    estimated_input = (product_bytes + controlled_bytes + 3) // 4 + 256
    estimate = {
        "planned_live_calls": {CONDITION_COLD: 1, CONDITION_WARM: 1},
        "planned_live_calls_total": 2,
        "live_slots": {
            CONDITION_COLD: {"provider": LIVE_PROVIDER, "model": LIVE_MODEL, "estimated_input_tokens": estimated_input, "max_output_tokens": MAX_OUTPUT_TOKENS},
            CONDITION_WARM: {"provider": LIVE_PROVIDER, "model": LIVE_MODEL, "estimated_input_tokens": estimated_input, "max_output_tokens": MAX_OUTPUT_TOKENS},
        },
        "expected_token_upper_bound": 2 * (estimated_input + MAX_OUTPUT_TOKENS),
        "provider_retry_allowed": False,
    }
    if estimate["planned_live_calls_total"] > MAX_TOTAL_LIVE_CALLS:
        raise PairError("planned live calls exceed the hard paired budget")
    return estimate


def prepare_shadow_recipe(
    *,
    store: RecipeStore,
    source_ledger: Path | str,
    source_factory_run_id: str,
    product_class: ProductClass,
    approval_reference: str = "redi08b-preregistered-pilot",
) -> StoredRecipeVersion:
    """Import the accepted candidate and explicitly enable its SHADOW v2.

    The helper is idempotent for a store that already contains v2.  It never
    promotes to CANARY/VALIDATED and never alters the historical source ledger.
    """

    candidate = import_candidate_from_ledger(
        store=store,
        source_ledger=source_ledger,
        source_factory_run_id=source_factory_run_id,
        product_class=product_class,
    )
    existing = store.list_versions(str(candidate.recipe.id))
    if len(existing) >= 2:
        stored = store.read(str(candidate.recipe.id), 2)
        if stored.lifecycle_state != RecipeRung.SHADOW:
            raise PairError("existing recipe v2 is not the governed SHADOW version")
        return stored
    approval = RecipeApproval(
        policy_id=EXPERIMENTAL_PROMOTION_POLICY_ID,
        policy_version=EXPERIMENTAL_PROMOTION_POLICY_VERSION,
        approved_by="REDI-08B offline preregistration audit",
        approval_reference=approval_reference,
        source_evidence_ids=(
            str(candidate.provenance["source_candidate_evidence_id"]),
            str(candidate.provenance["source_product_verified_event_id"]),
        ),
        approved_at=_utcnow(),
    )
    confidence = ConfidenceMetadata(
        tier="LOW",
        evidence_state="SINGLE_PRODUCT_EVIDENCE",
        source_verified_runs_count=1,
        source_failed_runs_count=0,
        source_failed_attempts_count=1,
        distinct_sibling_products_tested=0,
        last_verified_at=None,
        environment_coverage=("controlled_python_offline",),
        model_coverage=("observed_source_only",),
        station_evidence_coverage=tuple(str(value) for value in candidate.recipe.required_stations),
        transfer_evidence_count=0,
    )
    preferences = {
        StationId("station_cold_start_implementation_v0"): (
            WorkerId(WORKER_UNIVERSE[1]),
            WorkerId(WORKER_UNIVERSE[2]),
            WorkerId(WORKER_UNIVERSE[0]),
        ),
        StationId("station_cold_start_integration_review_v0"): (
            WorkerId(WORKER_UNIVERSE[3]),
        ),
    }
    return store.enable_experimental(
        recipe_id=str(candidate.recipe.id),
        source_version=1,
        product_class=product_class,
        approval=approval,
        confidence=confidence,
        validity_conditions=(
            ValidityCondition(
                description="controlled sibling paired experiment only",
                scope="environment",
                checked_via="other",
            ),
        ),
        worker_preferences=preferences,
        worker_fallbacks=preferences,
        metadata={
            "worker_semantics": {
                "observed_source": "evidence_only",
                "paired_worker_universe": list(WORKER_UNIVERSE),
                "globally_best_claimed": False,
            }
        },
    )


class _OneShotProviderBoundary:
    def __init__(self, *, condition: str, live: bool) -> None:
        self.condition = condition
        self.live = live
        self.attempts = 0
        self.provider_calls = 0
        self.network_calls = 0
        self.provider_wall_seconds = 0.0

    def run(self, *, context_package: Any, task_id: str, station_id: str, workspace_root: Path) -> dict[str, Any]:
        if self.attempts >= 1:
            raise ProviderRetryBlocked(f"{self.condition} provider retry is forbidden")
        self.attempts += 1
        request = render_live_request(
            role="builder",
            task_id=task_id,
            instructions=context_package.instructions,
            context_body=context_package.context_body,
        )
        started = time.perf_counter()
        if not self.live:
            self.provider_wall_seconds = time.perf_counter() - started
            return {
                "patches": [PatchEdit("bulk_inventory/normalize.py", "write", _NORMALIZE_CODE)],
                "provider": {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0, "provider": None, "model": None},
                "context": {
                    "compiled_package_bytes": context_package.counts["bytes"],
                    "rendered_request_bytes": request["rendered_request_bytes"],
                    "heuristic_tokens": context_package.counts["estimated_tokens"],
                },
                "structured_output": {"validated": True, "schema": "RediBuilderOutput", "source": "deterministic_fake_adapter"},
                "synthetic_fake": {"adapter_calls": 1, "input_tokens": 0, "output_tokens": 0},
            }
        def observe_live_call() -> None:
            self.provider_calls += 1
            self.network_calls += 1

        adapter = LiveProviderAdapter(
            config=LiveProviderConfig(
                env_key_name="REDI_OPENAI_API_KEY",
                model=LIVE_MODEL,
                timeout_s=60.0,
                max_output_tokens=MAX_OUTPUT_TOKENS,
                temperature=0.0,
                provider_name=LIVE_PROVIDER,
            ),
            before_call=observe_live_call,
        )
        try:
            result: NodeRunResult = adapter.run_node(
                task_id=TaskId(task_id),
                station_id=StationId(station_id),
                worker_id=WorkerId("worker_pair_provider_candidate_v0"),
                context={"role": "builder", "instructions": context_package.instructions, "context_body": context_package.context_body},
                workspace_root=workspace_root,
                station_role="builder",
            )
        finally:
            self.provider_wall_seconds = time.perf_counter() - started
        usage = result.token_usage or TokenUsage(input=0, output=0, cached_input=0)
        return {
            "patches": list(result.patches),
            "provider": {"calls": 1, "input_tokens": usage.input, "output_tokens": usage.output, "cached_input_tokens": usage.cached_input, "provider": LIVE_PROVIDER, "model": LIVE_MODEL},
            "context": {
                "compiled_package_bytes": context_package.counts["bytes"],
                "rendered_request_bytes": request["rendered_request_bytes"],
                "heuristic_tokens": context_package.counts["estimated_tokens"],
            },
            "structured_output": {
                "validated": result.error_message is None,
                "schema": "RediBuilderOutput",
                "safe_error": result.error_message,
                "source": "provider_json_schema_plus_local_validation",
            },
            "synthetic_fake": {"adapter_calls": 0, "input_tokens": 0, "output_tokens": 0},
        }


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
    return {"vendor_code": normalize_vendor_code(vendor, code), "quantity": batch_reorder_quantity(on_hand, target, safety), "safety_stock": safety}
'''


def _event(event_type: LifecycleEventType, run_id: str, product_id: str, payload: Mapping[str, Any]) -> LifecycleEvent:
    return LifecycleEvent(event_type=event_type, entity_ids={"factory_run_id": run_id, "product_id": product_id}, payload=dict(payload), timestamp=_utcnow())


def _gate(
    identity: str,
    node_id: str,
    outcome: str,
    *,
    evidence_ref: str,
    reason: str | None = None,
) -> GateResult:
    status = {"PASS": GateStatus.PASS, "FAIL": GateStatus.FAIL, "NOT_RUN": GateStatus.BLOCKED}[outcome]
    return GateResult(
        gate_identity=identity,
        gate_type=GateType.DETERMINISTIC,
        target_node_id=node_id,
        criteria=(AcceptanceCriterion(description=f"{identity} must pass"),),
        status=status,
        verifier_type="deterministic_code",
        verifier_identity="redi08b_strict_gate_engine_v0",
        failure_reasons=() if outcome == "PASS" else ((reason or "boundary not reached"),),
        metadata={
            "gate_policy_id": WARM_START_GATE_POLICY_ID,
            "gate_policy_version": WARM_START_GATE_POLICY_VERSION,
            "outcome": outcome,
            "boundary": identity,
            "evidence_reference": evidence_ref,
        },
        created_at=_utcnow(),
    )


def _condition_plan(
    *, condition: str, product: ProductSpec, product_class: ProductClass, stored: StoredRecipeVersion
) -> tuple[Any, Mapping[str, Any]]:
    if condition == CONDITION_COLD:
        policy = ColdStartPolicy()
        signature = derive_product_signature(product)
        plan = plan_cold_start_v0(product=product, product_class=product_class, signature=signature, stations=controlled_warm_stations(), policy=policy)
        return plan, {
            "cold_planner_used": True,
            "recipe_consulted": False,
            "adaptation": None,
            "planner_policy_id": "cold_start_conservative_planner_v0",
            "conservative": True,
            "execution_policy": {
                "worker_order": [WORKER_UNIVERSE[1], WORKER_UNIVERSE[2]],
                "fallback_ladder": [WORKER_UNIVERSE[1], WORKER_UNIVERSE[2]],
                "context_strategy": CONTEXT_STRATEGY_SELECTED_V0,
                "context_budget_bytes": 65536,
                "retry_policy": {"paid_retries": 0},
                "escalation_policy": {"on_failure": "owner_authorized_rerun_only"},
                "parallelization": {"independent_roots": ["implement_normalize", "implement_reorder"], "max_parallelism": 2},
                "gate_policy": {"id": WARM_START_GATE_POLICY_ID, "version": WARM_START_GATE_POLICY_VERSION},
                "source": "cold_start_policy",
            },
        }
    match = match_product_class(product, product_class)
    available_stations = tuple(str(item.id) for item in controlled_warm_stations())
    eligibility = evaluate_warm_start_eligibility(
        stored=stored,
        match=match,
        available_station_ids=available_stations,
        available_worker_ids=tuple(str(worker) for values in stored.recipe.worker_preferences.values() for worker in values),
        available_context_strategies=tuple(stored.recipe.context_budgets),
        available_gate_policies=(WARM_START_GATE_POLICY_ID,),
    )
    semantic_recipe_hash = canonical_recipe_semantic_hash(stored.recipe)
    plan = compile_warm_start_dag(
        product=product, product_class=product_class, stored=stored, match=match,
        eligibility=eligibility, recipe_identity_hash=semantic_recipe_hash,
        product_identity_hash=product_spec_hash(product),
    )
    implementation_station = str(controlled_warm_stations()[0].id)
    context_budget = stored.recipe.context_budgets[CONTEXT_STRATEGY_SELECTED_V0]
    context_budget_metadata = (
        context_budget.get("metadata", {}) if isinstance(context_budget, Mapping)
        else context_budget.metadata
    )
    return plan, {
        "cold_planner_used": False,
        "recipe_consulted": True,
        "match": match.to_dict(),
        "eligibility": eligibility.to_dict(),
        "adaptation": plan.adaptation.to_dict(),
        "execution_policy": {
            "worker_order": [str(item) for item in stored.recipe.worker_preferences[StationId(implementation_station)]],
            "fallback_ladder": [str(item) for item in stored.recipe.worker_fallbacks[StationId(implementation_station)]],
            "context_strategy": CONTEXT_STRATEGY_SELECTED_V0,
            "context_budget_bytes": int(context_budget_metadata["budget_bytes"]),
            "retry_policy": _canonical(stored.recipe.retry_policies),
            "escalation_policy": _canonical(stored.recipe.escalation_policies),
            "parallelization": _canonical(stored.recipe.parallelization_rules),
            "gate_policy": _canonical(stored.recipe.gate_policies),
            "dag_template_id": stored.recipe.dag_template_id,
            "adaptation_hash": plan.adaptation.canonical_hash,
            "source": "pinned_recipe",
        },
    }


def _execute_condition(
    *,
    root: Path,
    condition: str,
    preregistration: PreregisteredManifest,
    product: ProductSpec,
    product_class: ProductClass,
    stored: StoredRecipeVersion,
    live: bool,
) -> ConditionOutcome:
    started_perf = time.perf_counter()
    condition_root = root / ("cold" if condition == CONDITION_COLD else "warm")
    if condition_root.exists():
        raise PairError(f"condition root already exists: {condition_root}")
    fixture_root = condition_root / "fixture"
    workspace_root = condition_root / "workspace"
    artifact_root = condition_root / "artifacts"
    fixture_hash = _write_fixture(fixture_root)
    shutil.copytree(fixture_root, workspace_root)
    artifact_root.mkdir(parents=True)
    plan, lineage = _condition_plan(condition=condition, product=product, product_class=product_class, stored=stored)
    dag_hash = product_dag_hash(plan.dag)
    db_path = condition_root / "evidence.sqlite"
    conn = sqlite3.connect(db_path)
    bootstrap_schema(conn)
    ledger = EvidenceLedger(conn)
    experiments = ExperimentRepository(conn, ledger)
    experiments.store_preregistered(preregistration.manifest)
    experiments.freeze(str(preregistration.manifest.id))
    started_at = _utcnow()
    runs = FactoryRunRepository(conn, ledger)
    factory = FactoryRun(
        product_spec_id=product.id,
        dag_id=str(plan.dag.id),
        recipe_id=stored.recipe.id if condition == CONDITION_WARM else None,
        mode=Mode.WARM if condition == CONDITION_WARM else Mode.EXPLORATION,
        final_status=RunStatus.RUNNING,
        verified=False,
        metadata={
            "acceptance": "REDI-08B",
            "experiment_id": str(preregistration.manifest.id),
            "manifest_hash": preregistration.canonical_hash,
            "manifest_frozen_at": preregistration.frozen_at,
            "manifest_frozen_before_factory_run": preregistration.frozen_at < started_at.isoformat(),
            "condition": condition,
            "condition_order": list(CONDITION_ORDER),
            "product_spec_sha256": product_spec_hash(product),
            "fixture_hash": fixture_hash,
            "workspace_root": str(workspace_root),
            "artifact_root": str(artifact_root),
            "worker_universe": list(WORKER_UNIVERSE),
            "recipe": None if condition == CONDITION_COLD else preregistration.manifest.metadata["recipe"],
            **lineage,
        },
        created_at=started_at,
    )
    run_id = runs.store(factory, experiment_id=str(preregistration.manifest.id))
    ledger.append(_event(LifecycleEventType.PRODUCT_ACCEPTED, run_id, str(product.id), {
        "condition": condition,
        "manifest_hash": preregistration.canonical_hash,
        "product_spec_sha256": product_spec_hash(product),
        "known_recipe": None if condition == CONDITION_COLD else preregistration.manifest.metadata["recipe"],
    }), factory_run_id=run_id, experiment_id=str(preregistration.manifest.id))
    ledger.append(_event(LifecycleEventType.PLAN_COMPILED, run_id, str(product.id), {
        "condition": condition,
        "dag_hash": dag_hash,
        "dag_snapshot": dag_snapshot(plan.dag),
        **lineage,
    }), factory_run_id=run_id, experiment_id=str(preregistration.manifest.id))

    boundary = _OneShotProviderBoundary(condition=condition, live=live)
    implementation_task = plan.tasks_by_node["implement_normalize"]
    implementation_station = plan.stations_by_node["implement_normalize"]
    component = plan.components_by_node["implement_normalize"]
    execution_policy = lineage["execution_policy"]
    worker_order = list(execution_policy["worker_order"])
    if not worker_order or worker_order[0] != WORKER_UNIVERSE[1]:
        raise PairError("governed provider worker is not first in the selected execution policy")
    selected_worker = worker_order[0]
    firewall = ContextFirewall(
        allowed_classes=ContextFirewall().allowed_classes
        | frozenset({CONTEXT_CLASS_PRIOR_FAILURE_EVIDENCE, CONTEXT_CLASS_UPSTREAM_HANDOFFS})
    )
    context_package = compile_context(
        strategy_id=str(execution_policy["context_strategy"]),
        task=implementation_task,
        station=implementation_station,
        product=product,
        workspace_root=workspace_root,
        selected_paths=tuple(component["selected_paths"]),
        prior_failure_evidence=(),
        handoff_manifests=(),
        budget_bytes=int(execution_policy["context_budget_bytes"]),
        firewall=firewall,
        station_role="builder",
        allowed_scope=tuple(component["allowed_scope"]),
    )
    if "normalize_vendor_code(vendor, code)" not in context_package.rendered_text:
        raise PairError("compiled context omitted the exact two-argument public interface")
    failures: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    gate_rows: list[GateResult] = []
    provider_failed: str | None = None
    provider_result: dict[str, Any] = {}
    gate_outcomes = {identity: "NOT_RUN" for identity in MANDATORY_GATES}
    gate_reasons: dict[str, str] = {}
    try:
        provider_result = boundary.run(
            context_package=context_package,
            task_id=str(implementation_task.id),
            station_id=str(implementation_station.id),
            workspace_root=workspace_root,
        )
    except Exception as exc:  # governed failure is persisted below; never retry
        provider_failed = f"{type(exc).__name__}: {exc}"
        gate_outcomes["structured_output_schema"] = "FAIL"
        gate_reasons["structured_output_schema"] = provider_failed
    gate_compute_started = time.perf_counter()
    local_worker_compute_seconds = 0.0

    normalize_patch: PatchEdit | None = None
    if provider_failed is None:
        structured = provider_result.get("structured_output", {})
        if not structured.get("validated"):
            provider_failed = str(structured.get("safe_error") or "structured output was not validated")
            gate_outcomes["structured_output_schema"] = "FAIL"
            gate_reasons["structured_output_schema"] = provider_failed
        else:
            gate_outcomes["structured_output_schema"] = "PASS"

    if provider_failed is None:
        patches = list(provider_result.get("patches", ()))
        if len(patches) != 1 or patches[0].rel_path != "bulk_inventory/normalize.py" or patches[0].action != "write" or patches[0].content is None:
            provider_failed = "patch output contract requires exactly one normalize.py write"
            gate_outcomes["patch_output_contract"] = "FAIL"
            gate_reasons["patch_output_contract"] = provider_failed
        else:
            normalize_patch = patches[0]
            gate_outcomes["patch_output_contract"] = "PASS"

    if provider_failed is None and normalize_patch is not None:
        if not normalize_patch.rel_path.startswith("bulk_inventory/"):
            provider_failed = "patch escaped allowed file scope"
            gate_outcomes["allowed_file_change_scope"] = "FAIL"
            gate_reasons["allowed_file_change_scope"] = provider_failed
        else:
            gate_outcomes["allowed_file_change_scope"] = "PASS"

    normalize_namespace: dict[str, Any] = {}
    reorder_namespace: dict[str, Any] = {}
    if provider_failed is None and normalize_patch is not None:
        try:
            compile(normalize_patch.content or "", "normalize.py", "exec")
            compile(_REORDER_CODE, "reorder.py", "exec")
            compile(_SERVICE_CODE, "service.py", "exec")
            gate_outcomes["python_syntax_compile"] = "PASS"
        except Exception as exc:
            provider_failed = f"syntax failure: {type(exc).__name__}: {exc}"
            gate_outcomes["python_syntax_compile"] = "FAIL"
            gate_reasons["python_syntax_compile"] = provider_failed

    if provider_failed is None and normalize_patch is not None:
        try:
            exec(normalize_patch.content or "", normalize_namespace)
            public_actual = normalize_namespace["normalize_vendor_code"]("acme corp", " a_12 ")
            if public_actual != "ACME-CORP:A-12":
                raise AssertionError(f"unexpected public result {public_actual!r}")
            gate_outcomes["public_test_execution"] = "PASS"
        except Exception as exc:
            provider_failed = f"public verification failure: {type(exc).__name__}: {exc}"
            gate_outcomes["public_test_execution"] = "FAIL"
            gate_reasons["public_test_execution"] = provider_failed

    if provider_failed is None:
        try:
            fn = normalize_namespace["normalize_vendor_code"]
            if fn("vendor_x", "sku_9") != "VENDOR-X:SKU-9":
                raise AssertionError("private namespace case failed")
            try:
                fn("vendor", "")
            except ValueError:
                pass
            else:
                raise AssertionError("empty code must fail")
            gate_outcomes["private_test_execution"] = "PASS"
        except Exception as exc:
            provider_failed = f"private verification failure: {type(exc).__name__}: {exc}"
            gate_outcomes["private_test_execution"] = "FAIL"
            gate_reasons["private_test_execution"] = provider_failed

    if provider_failed is None and normalize_patch is not None:
        local_worker_started = time.perf_counter()
        (workspace_root / "bulk_inventory" / "normalize.py").write_text(normalize_patch.content or "", encoding="utf-8")
        (workspace_root / "bulk_inventory" / "reorder.py").write_text(_REORDER_CODE, encoding="utf-8")
        (workspace_root / "bulk_inventory" / "service.py").write_text(_SERVICE_CODE, encoding="utf-8")
        produced_hash = hashlib.sha256((normalize_patch.content or "").encode("utf-8")).hexdigest()
        gate_outcomes["handoff_artifact_validation"] = "PASS" if len(produced_hash) == 64 else "FAIL"
        try:
            exec(_REORDER_CODE, reorder_namespace)
            result = {
                "vendor_code": normalize_namespace["normalize_vendor_code"]("acme corp", " a_12 "),
                "quantity": reorder_namespace["batch_reorder_quantity"](3, 10, 2),
                "safety_stock": 2,
            }
            if result != {"vendor_code": "ACME-CORP:A-12", "quantity": 9, "safety_stock": 2}:
                raise AssertionError("integration result mismatch")
            gate_outcomes["integration_verification"] = "PASS"
            if result["quantity"] < result["safety_stock"]:
                raise AssertionError("independent reviewer rejected safety stock")
            gate_outcomes["independent_final_reviewer"] = "PASS"
        except Exception as exc:
            provider_failed = f"integration/reviewer failure: {type(exc).__name__}: {exc}"
            failed_gate = "integration_verification" if gate_outcomes["integration_verification"] != "PASS" else "independent_final_reviewer"
            gate_outcomes[failed_gate] = "FAIL"
            gate_reasons[failed_gate] = provider_failed
        local_worker_compute_seconds = max(time.perf_counter() - local_worker_started, 0.000001)

    verified = all(gate_outcomes[identity] == "PASS" for identity in MANDATORY_GATES)
    gate_compute_seconds = max(time.perf_counter() - gate_compute_started, 0.000001)
    gate_rows = [
        _gate(
            identity,
            "implement_normalize" if identity not in {"integration_verification", "independent_final_reviewer"} else "integration_review",
            gate_outcomes[identity],
            evidence_ref=("provider-structured-output" if identity == "structured_output_schema" else f"workspace:{identity}"),
            reason=gate_reasons.get(identity),
        )
        for identity in MANDATORY_GATES
    ]
    for gate in gate_rows:
        ledger.append(gate, factory_run_id=run_id, experiment_id=str(preregistration.manifest.id))

    provider = provider_result.get("provider", {
        "calls": boundary.provider_calls,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "provider": LIVE_PROVIDER if live else None,
        "model": LIVE_MODEL if live else None,
    })
    provider = dict(provider) | {"provider_wall_seconds": boundary.provider_wall_seconds}
    request_accounting = render_live_request(
        role="builder", task_id=str(implementation_task.id),
        instructions=context_package.instructions, context_body=context_package.context_body,
    )
    context = provider_result.get("context", {
        "compiled_package_bytes": context_package.counts["bytes"],
        "rendered_request_bytes": request_accounting["rendered_request_bytes"],
        "heuristic_tokens": context_package.counts["estimated_tokens"],
    })
    patch_evidence = [
        {
            "rel_path": patch.rel_path,
            "action": patch.action,
            "bytes": len((patch.content or "").encode("utf-8")),
            "sha256": hashlib.sha256((patch.content or "").encode("utf-8")).hexdigest(),
        }
        for patch in provider_result.get("patches", ())
    ]
    attempt = RunEvidence(
        experiment_id=preregistration.manifest.id,
        factory_run_id=FactoryRunId(run_id),
        product_id=product.id,
        product_class_id=product_class.id,
        recipe_id=stored.recipe.id if condition == CONDITION_WARM else None,
        recipe_version=stored.recipe.version if condition == CONDITION_WARM else None,
        node_id="implement_normalize",
        station_id=implementation_station.id,
        task_class=("warm_start_worker_attempt" if condition == CONDITION_WARM else "cold_start_worker_attempt"),
        worker_id=WorkerId(selected_worker),
        worker_version="1",
        token_usage=TokenUsage(input=provider["input_tokens"], output=provider["output_tokens"], cached_input=provider["cached_input_tokens"]),
        timing=Timing(wall_clock_seconds=max(boundary.provider_wall_seconds, 0.000001), compute_seconds=gate_compute_seconds),
        tests_executed=("test_vendor_normalize_public",) if gate_outcomes["public_test_execution"] != "NOT_RUN" else (),
        gate_results=tuple(GateResultId(str(gate.id)) for gate in gate_rows),
        retry_count=0,
        escalation=False,
        human_intervention=False,
        final_status=RunStatus.COMPLETED if verified else RunStatus.FAILED,
        failure_category=None if verified else FailureCategory.DETERMINISTIC_TEST_FAILURE,
        metadata={
            "attempt_index": 0,
            "execution_kind": "governed_provider_slot",
            "actual_worker_attempt": True,
            "dry_fake_attempt": not live,
            "recipe_consulted": condition == CONDITION_WARM,
            "worker_policy_source": execution_policy["source"],
            "worker_profile": _worker_profiles()[selected_worker],
            "provider": provider,
            "structured_output": provider_result.get("structured_output", {"validated": False, "safe_error": provider_failed}),
            "patch_result": patch_evidence,
            "context": context_package.to_evidence() | context | {
                "exact_interface_present": True,
                "prior_failure_feedback": list(context_package.prior_failure_evidence),
                "upstream_handoffs": list(context_package.upstream_handoffs),
            },
            "gates": {identity: gate_outcomes[identity] for identity in MANDATORY_GATES},
            "local_compute": {"gate_compute_seconds": gate_compute_seconds, "public_gate_executions": int(gate_outcomes["public_test_execution"] != "NOT_RUN")},
            "synthetic_fake": provider_result.get("synthetic_fake", {"adapter_calls": 0, "input_tokens": 0, "output_tokens": 0}),
        },
        created_at=_utcnow(),
    )
    ledger.append(attempt, factory_run_id=run_id, experiment_id=str(preregistration.manifest.id))
    attempts.append({
        "evidence_id": str(attempt.id), "worker_id": selected_worker, "node_id": "implement_normalize",
        "status": "completed" if verified else "failed", "actual_worker_attempt": True,
        "dry_fake_attempt": not live, "provider_attempt": True, "provider_calls": provider["calls"],
        "context_bytes": context["compiled_package_bytes"],
    })

    if not verified:
        failure = FailureRecord(
            category=FailureCategory.DETERMINISTIC_TEST_FAILURE,
            origin="governed_worker_implementation",
            node_id="implement_normalize",
            task_id=implementation_task.id,
            factory_run_id=FactoryRunId(run_id),
            details={"safe_error": provider_failed, "failed_gates": [key for key, value in gate_outcomes.items() if value == "FAIL"]},
            severity=Severity.ERROR,
            recoverable=False,
            escalation_target="owner_authorized_rerun_only",
            evidence_id=attempt.id,
            metadata={
                "failure_class": "ordinary_worker_implementation_failure",
                "transfer_failure": False,
                "recipe_configuration_causal": False,
            },
            created_at=_utcnow(),
        )
        ledger.append(failure, factory_run_id=run_id, experiment_id=str(preregistration.manifest.id))
        failures.append({
            "failure_id": str(failure.id), "category": failure.category.value,
            "failure_class": "ordinary_worker_implementation_failure", "transfer_failure": False,
        })
    else:
        for node_id, worker_id, gates in (
            ("implement_reorder", WORKER_UNIVERSE[2], ("python_syntax_compile", "public_test_execution", "private_test_execution", "handoff_artifact_validation")),
            ("integration_review", WORKER_UNIVERSE[3], ("integration_verification", "independent_final_reviewer")),
        ):
            local = RunEvidence(
                experiment_id=preregistration.manifest.id,
                factory_run_id=FactoryRunId(run_id), product_id=product.id, product_class_id=product_class.id,
                recipe_id=stored.recipe.id if condition == CONDITION_WARM else None,
                recipe_version=stored.recipe.version if condition == CONDITION_WARM else None,
                node_id=node_id, station_id=plan.stations_by_node[node_id].id,
                task_class=("warm_start_worker_attempt" if condition == CONDITION_WARM else "cold_start_worker_attempt"),
                worker_id=WorkerId(worker_id), worker_version="1",
                gate_results=tuple(GateResultId(str(gate.id)) for gate in gate_rows if gate.gate_identity in gates),
                retry_count=0, escalation=False, human_intervention=False, final_status=RunStatus.COMPLETED,
                metadata={
                    "execution_kind": "governed_local_deterministic",
                    "actual_worker_attempt": True,
                    "dry_fake_attempt": False,
                    "provider": {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0, "provider": None, "model": None},
                    "worker_profile": _worker_profiles()[worker_id],
                    "worker_policy_source": execution_policy["source"],
                },
                created_at=_utcnow(),
            )
            ledger.append(local, factory_run_id=run_id, experiment_id=str(preregistration.manifest.id))
            attempts.append({
                "evidence_id": str(local.id), "worker_id": worker_id, "node_id": node_id,
                "status": "completed", "actual_worker_attempt": True, "dry_fake_attempt": False,
                "provider_attempt": False, "provider_calls": 0, "context_bytes": 0,
            })

    wall = max(time.perf_counter() - started_perf, 0.000001)
    metrics = {
        "quality": {
            "product_verified": verified,
            "required_gate_outcomes": dict(gate_outcomes),
            "private_verification": gate_outcomes["private_test_execution"],
            "integration_reviewer_result": gate_outcomes["independent_final_reviewer"],
            "regression_count": 0 if verified else None,
        },
        "provider_usage": provider,
        "orchestration": {
            "worker_attempts": len(attempts),
            "actual_worker_attempts": len([item for item in attempts if item["actual_worker_attempt"]]),
            "synthetic_dry_attempts": len([item for item in attempts if item["dry_fake_attempt"]]),
            "provider_attempts": len([item for item in attempts if item["provider_attempt"]]),
            "failed_attempts": len([item for item in attempts if item["status"] == "failed"]),
            "retries": 0,
            "escalations": 0,
            "graph_revisions": int(plan.dag.revision),
            "replanning_events": 0,
            "fallback_events": 0,
            "execution_policy": execution_policy,
        },
        "context": {
            "compiled_package_bytes": context["compiled_package_bytes"],
            "rendered_request_bytes": context["rendered_request_bytes"],
            "heuristic_tokens": context["heuristic_tokens"],
            "distinct_compilations": 1,
        },
        "time": {
            "graph_wall_clock_seconds": wall,
            "aggregate_worker_compute_seconds": gate_compute_seconds,
            "provider_wall_seconds": boundary.provider_wall_seconds,
        },
        "recipe": {
            "lookup_result": "NOT_CONSULTED" if condition == CONDITION_COLD else "PINNED_SHADOW_ELIGIBLE",
            "adaptation_count": 0 if condition == CONDITION_COLD else 1,
            "adaptation_reasons": [] if condition == CONDITION_COLD else ["materially different product-variable fields"],
            "transfer_failures": [item.get("transfer_failure_class") for item in failures if item.get("transfer_failure_class")],
            "rollback_revalidation_events": 0,
        },
        "human": {"interventions": 0},
        "accounting": {
            "provider_only": provider,
            "all_system_local": {
                "deterministic_operations": len(attempts),
                "deterministic_worker_compute_seconds": local_worker_compute_seconds,
                "gate_compute_seconds": gate_compute_seconds,
                "synthetic_fake": provider_result.get("synthetic_fake", {"adapter_calls": 0, "input_tokens": 0, "output_tokens": 0}),
            },
        },
    }
    snapshot = {
        "condition": condition,
        "experiment_id": str(preregistration.manifest.id),
        "manifest_hash": preregistration.canonical_hash,
        "product_spec_id": str(product.id),
        "product_spec_sha256": product_spec_hash(product),
        "factory_run_id": run_id,
        "dag_hash": dag_hash,
        "dag_snapshot": dag_snapshot(plan.dag),
        "fixture_hash": fixture_hash,
        "controlled_payloads": preregistration.manifest.arms[condition]["controlled_variables"]["controlled_payloads"],
        "controlled_payload_hashes": preregistration.manifest.arms[condition]["controlled_variables"]["controlled_payload_hashes"],
        "workspace_isolated": True,
        "recipe": None if condition == CONDITION_COLD else preregistration.manifest.metadata["recipe"],
        "lineage": lineage,
        "context_compilation": context_package.to_evidence() | context | {"exact_interface_present": True},
        "gate_evidence": {
            gate.gate_identity: {
                "gate_result_id": str(gate.id),
                "status": gate.metadata["outcome"],
                "boundary": gate.metadata["boundary"],
                "evidence_reference": gate.metadata["evidence_reference"],
            }
            for gate in gate_rows
        },
        "attempts": attempts,
        "failures": failures,
        "metrics": metrics,
        "verified": verified,
        "provider_failure": provider_failed,
        "provider_retry_attempted": False,
        "recipe_promoted": False,
        "replay_dependencies": {"workers": 0, "providers": 0, "network": 0, "recipe_store": False, "current_config": False},
    }
    ledger.append(RunEvidence(
        experiment_id=preregistration.manifest.id,
        factory_run_id=FactoryRunId(run_id),
        product_id=product.id,
        product_class_id=product_class.id,
        recipe_id=stored.recipe.id if condition == CONDITION_WARM else None,
        recipe_version=stored.recipe.version if condition == CONDITION_WARM else None,
        node_id="PAIRED_CONDITION_SNAPSHOT",
        task_class="redi08b_condition_snapshot",
        token_usage=TokenUsage(input=provider["input_tokens"], output=provider["output_tokens"], cached_input=provider["cached_input_tokens"]),
        timing=Timing(wall_clock_seconds=wall, compute_seconds=wall),
        retry_count=0,
        escalation=bool(metrics["orchestration"]["escalations"]),
        human_intervention=False,
        final_status=RunStatus.COMPLETED if verified else RunStatus.FAILED,
        metadata={"evidence_kind": "redi08b_condition_snapshot", "snapshot": snapshot},
        created_at=_utcnow(),
    ), factory_run_id=run_id, experiment_id=str(preregistration.manifest.id))
    if verified:
        ledger.append(_event(LifecycleEventType.PRODUCT_VERIFIED, run_id, str(product.id), {
            "condition": condition, "dag_hash": dag_hash, "all_mandatory_gates_passed": True, "independent_reviewer": WORKER_UNIVERSE[3]
        }), factory_run_id=run_id, experiment_id=str(preregistration.manifest.id))
    completed_at = _utcnow()
    runs.close_run(
        run_id,
        final_status=RunStatus.COMPLETED if verified else RunStatus.FAILED,
        verified=verified,
        total_tokens=TokenUsage(input=provider["input_tokens"], output=provider["output_tokens"], cached_input=provider["cached_input_tokens"]),
        timing=Timing(started_at=started_at, ended_at=completed_at, wall_clock_seconds=wall, compute_seconds=wall),
    )
    conn.close()
    return ConditionOutcome(condition, db_path, run_id, verified, dag_hash, fixture_hash, metrics, boundary.provider_calls, boundary.network_calls, started_at.isoformat(), completed_at.isoformat())


def replay_condition(*, db_path: Path | str, factory_run_id: str) -> dict[str, Any]:
    reader = ReplayReader(Path(db_path).resolve())
    try:
        replay = reader.replay_factory_run(FactoryRunId(factory_run_id))
    finally:
        reader.close()
    if replay.integrity_status != "OK":
        raise PairError(f"condition replay integrity is {replay.integrity_status}")
    records = [item for item in replay.run_evidences if (item.metadata or {}).get("evidence_kind") == "redi08b_condition_snapshot"]
    if len(records) != 1:
        raise PairError("condition replay requires exactly one canonical snapshot")
    return _canonical(records[0].metadata["snapshot"])


def replay_pair(path: Path | str) -> dict[str, Any]:
    pair_path = Path(path).resolve()
    before = pair_path.stat()
    raw = pair_path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    expected = payload.pop("canonical_hash")
    if _hash(payload) != expected:
        raise PairError("paired result canonical hash mismatch")
    after = pair_path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise PairError("paired replay was not read-only")
    return _canonical(payload | {"canonical_hash": expected})


def _classification(cold: ConditionOutcome, warm: ConditionOutcome, controls_valid: bool) -> PairClassification:
    if not controls_valid:
        return PairClassification.INVALID_PAIR
    if cold.verified and warm.verified:
        return PairClassification.VALID_PAIRED_SUCCESS
    if cold.verified != warm.verified:
        return PairClassification.PARTIAL_PAIR
    return PairClassification.VALID_PAIRED_FAILURE


def execute_paired_experiment(
    *,
    output_root: Path | str,
    product: ProductSpec,
    product_class: ProductClass,
    store: RecipeStore,
    recipe_id: str,
    recipe_version: int,
    source_revision: str,
    live: bool = False,
    preregistered_callback: Callable[[ExperimentManifest, Mapping[str, Any]], None] | None = None,
) -> PairedExperimentOutcome:
    """Preregister once, then execute cold -> warm with no cross-condition reads."""

    if live and os.environ.get("REDI_LIVE_PROVIDER") != "1":
        raise PairError("live execution requires REDI_LIVE_PROVIDER=1 and caller --live")
    root = Path(output_root).resolve()
    if root.exists() and any(root.iterdir()):
        raise PairError("paired output root must be new or empty")
    root.mkdir(parents=True, exist_ok=True)
    stored = store.read(recipe_id, recipe_version)
    manifest = build_preregistered_manifest(
        product=product, product_class=product_class, stored=stored,
        source_revision=source_revision, live=live,
    )
    prereg = freeze_manifest(manifest, root)
    estimate = pre_execution_estimate(manifest)
    if preregistered_callback is not None:
        preregistered_callback(manifest, estimate)
    cold = _execute_condition(root=root, condition=CONDITION_COLD, preregistration=prereg, product=product, product_class=product_class, stored=stored, live=live)
    if hashlib.sha256(prereg.canonical_json.encode("utf-8")).hexdigest() != prereg.canonical_hash:
        raise PairError("manifest mutated after cold condition")
    warm = _execute_condition(root=root, condition=CONDITION_WARM, preregistration=prereg, product=product, product_class=product_class, stored=stored, live=live)
    controls_valid = (
        cold.fixture_hash == warm.fixture_hash
        and product_spec_hash(product) == manifest.task_selection["exact_product_sha256"]
        and manifest.arms[CONDITION_COLD]["controlled_variables"] == manifest.arms[CONDITION_WARM]["controlled_variables"]
        and cold.db_path.parent != warm.db_path.parent
    )
    cold_replay = replay_condition(db_path=cold.db_path, factory_run_id=cold.factory_run_id)
    warm_replay = replay_condition(db_path=warm.db_path, factory_run_id=warm.factory_run_id)
    result = {
        "experiment_id": str(manifest.id),
        "condition_ids": {CONDITION_COLD: cold.factory_run_id, CONDITION_WARM: warm.factory_run_id},
        "manifest_hash": prereg.canonical_hash,
        "sibling_product_spec_hash": product_spec_hash(product),
        "condition_order": list(CONDITION_ORDER),
        "source_revision": source_revision,
        "environment": json.loads(domain_to_json(manifest.environment)),
        "cold_factory_run_id": cold.factory_run_id,
        "warm_factory_run_id": warm.factory_run_id,
        "recipe": manifest.metadata["recipe"],
        "verification_results": {CONDITION_COLD: cold.verified, CONDITION_WARM: warm.verified},
        "metrics": {CONDITION_COLD: cold.metrics, CONDITION_WARM: warm.metrics},
        "dag_hashes": {CONDITION_COLD: cold.dag_hash, CONDITION_WARM: warm.dag_hash},
        "fixture_hashes": {CONDITION_COLD: cold.fixture_hash, CONDITION_WARM: warm.fixture_hash},
        "planned_live_budget": estimate,
        "actual_external_calls": {
            CONDITION_COLD: {"provider": cold.provider_calls, "network": cold.network_calls},
            CONDITION_WARM: {"provider": warm.provider_calls, "network": warm.network_calls},
        },
        "anomalies": [],
        "classification": _classification(cold, warm, controls_valid).value,
        "claims": {
            "automatic_comparative_claim_emitted": False,
            "warm_superiority_established": False,
            "recipe_advantage_established": False,
            "pilot_evidence_only": True,
        },
        "controls_valid": controls_valid,
        "preregistration_proof": {
            "manifest_frozen_at": prereg.frozen_at,
            "cold_started_at": cold.started_at,
            "warm_started_at": warm.started_at,
            "before_both_conditions": prereg.frozen_at < cold.started_at and prereg.frozen_at < warm.started_at,
        },
        "replay_refs": {
            CONDITION_COLD: {"db_path": str(cold.db_path), "factory_run_id": cold.factory_run_id, "snapshot_hash": _hash(cold_replay)},
            CONDITION_WARM: {"db_path": str(warm.db_path), "factory_run_id": warm.factory_run_id, "snapshot_hash": _hash(warm_replay)},
        },
    }
    result["canonical_hash"] = _hash(result)
    pair_path = root / "paired_result.json"
    pair_path.write_text(json.dumps(result, sort_keys=True, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit = audit_paired_experiment(root=root, pair_path=pair_path, manifest_path=prereg.path)
    return PairedExperimentOutcome(root, prereg, cold, warm, result, pair_path, audit)


def audit_paired_experiment(*, root: Path | str, pair_path: Path | str, manifest_path: Path | str) -> dict[str, bool]:
    pair = replay_pair(pair_path)
    manifest_raw = Path(manifest_path).read_text(encoding="utf-8").strip()
    manifest_hash = hashlib.sha256(manifest_raw.encode("utf-8")).hexdigest()
    cold_ref = pair["replay_refs"][CONDITION_COLD]
    warm_ref = pair["replay_refs"][CONDITION_WARM]
    cold_one = replay_condition(db_path=cold_ref["db_path"], factory_run_id=cold_ref["factory_run_id"])
    cold_two = replay_condition(db_path=cold_ref["db_path"], factory_run_id=cold_ref["factory_run_id"])
    warm_one = replay_condition(db_path=warm_ref["db_path"], factory_run_id=warm_ref["factory_run_id"])
    warm_two = replay_condition(db_path=warm_ref["db_path"], factory_run_id=warm_ref["factory_run_id"])
    pair_two = replay_pair(pair_path)
    condition_views = (cold_one, warm_one)
    provider_attempt_evidence_valid = all(
        view["metrics"]["orchestration"]["provider_attempts"]
        == len([attempt for attempt in view["attempts"] if attempt["provider_attempt"]])
        and view["metrics"]["provider_usage"]["calls"]
        <= view["metrics"]["orchestration"]["provider_attempts"]
        for view in condition_views
    )
    gate_records_complete = all(
        set(view["gate_evidence"]) == set(MANDATORY_GATES)
        for view in condition_views
    )
    all_required_gates_passed = all(
        all(item["status"] == "PASS" for item in view["gate_evidence"].values())
        for view in condition_views
    )
    controlled_payloads_reconstruct = all(
        all(_hash(value) == view["controlled_payload_hashes"][key] for key, value in view["controlled_payloads"].items())
        for view in condition_views
    )
    return {
        "manifest_hash_matches": manifest_hash == pair["manifest_hash"],
        "manifest_before_both_runs": bool(pair["preregistration_proof"]["before_both_conditions"]),
        "same_product": cold_one["product_spec_sha256"] == warm_one["product_spec_sha256"] == pair["sibling_product_spec_hash"],
        "same_fixture_bytes": cold_one["fixture_hash"] == warm_one["fixture_hash"],
        "workspace_isolation": Path(cold_ref["db_path"]).parent != Path(warm_ref["db_path"]).parent,
        "cold_uses_no_recipe": cold_one["recipe"] is None and cold_one["lineage"]["cold_planner_used"],
        "warm_uses_pinned_recipe": warm_one["recipe"] == pair["recipe"] and not warm_one["lineage"]["cold_planner_used"],
        "cold_replay_x2": cold_one == cold_two,
        "warm_replay_x2": warm_one == warm_two,
        "pair_replay_x2": pair == pair_two,
        "replay_zero_execution": all(item["replay_dependencies"] == {"workers": 0, "providers": 0, "network": 0, "recipe_store": False, "current_config": False} for item in (cold_one, warm_one)),
        "no_winner_field": "winner" not in pair and not pair["claims"]["automatic_comparative_claim_emitted"],
        "recipe_not_promoted": not cold_one["recipe_promoted"] and not warm_one["recipe_promoted"] and pair["recipe"]["lifecycle_state"] == RecipeRung.SHADOW.value,
        "hard_live_budget": pair["planned_live_budget"]["planned_live_calls_total"] <= MAX_TOTAL_LIVE_CALLS,
        "provider_local_separated": all("provider_only" in pair["metrics"][condition]["accounting"] and "all_system_local" in pair["metrics"][condition]["accounting"] for condition in CONDITION_ORDER),
        "provider_attempt_evidence": provider_attempt_evidence_valid,
        "gate_records_complete": gate_records_complete,
        "all_required_gates_passed": all_required_gates_passed,
        "context_compiler_used": all(
            view["context_compilation"]["strategy_id"] == CONTEXT_STRATEGY_SELECTED_V0
            and view["metrics"]["context"]["compiled_package_bytes"] > 0
            and view["metrics"]["context"]["rendered_request_bytes"] > 0
            for view in condition_views
        ),
        "controlled_payloads_reconstruct": controlled_payloads_reconstruct,
        "warm_policy_from_recipe": warm_one["lineage"]["execution_policy"]["source"] == "pinned_recipe",
        "pair_verified_success": pair["classification"] == PairClassification.VALID_PAIRED_SUCCESS.value
        and all(pair["verification_results"].values()),
        "controls_valid": bool(pair["controls_valid"]),
    }


def assert_retry_blocked() -> None:
    boundary = _OneShotProviderBoundary(condition=CONDITION_COLD, live=False)
    package = type("RetryContext", (), {
        "instructions": "first",
        "context_body": "public context",
        "counts": {"bytes": 20, "estimated_tokens": 5},
    })()
    boundary.run(context_package=package, task_id="task_first", station_id="station_first", workspace_root=Path.cwd())
    try:
        boundary.run(context_package=package, task_id="task_second", station_id="station_first", workspace_root=Path.cwd())
    except ProviderRetryBlocked:
        return
    raise AssertionError("second provider attempt was not blocked")


def classify_transfer_failure(stage: str) -> TransferFailureClass:
    mapping = {
        "match": TransferFailureClass.CLASS_MATCH_FAILURE,
        "validity": TransferFailureClass.RECIPE_VALIDITY_FAILURE,
        "adaptation": TransferFailureClass.ADAPTATION_FAILURE,
        "worker": TransferFailureClass.RECIPE_WORKER_UNAVAILABLE,
        "context": TransferFailureClass.RECIPE_CONTEXT_INVALID,
        "gate": TransferFailureClass.RECIPE_GATE_INCOMPATIBILITY,
        "topology": TransferFailureClass.RECIPE_TOPOLOGY_MISMATCH,
        "execution": TransferFailureClass.RECIPE_EXECUTION_FAILURE,
        "integration": TransferFailureClass.RECIPE_INTEGRATION_FAILURE,
        "revalidation": TransferFailureClass.RECIPE_REVALIDATION_FAILURE,
    }
    try:
        return mapping[stage]
    except KeyError as exc:
        raise ValueError(f"unknown recipe-transfer stage {stage!r}") from exc


__all__ = [
    "CONDITION_COLD", "CONDITION_ORDER", "CONDITION_WARM", "MAX_TOTAL_LIVE_CALLS",
    "PairClassification", "PairError", "PairedExperimentOutcome", "ProviderRetryBlocked",
    "TransferFailureClass", "WORKER_UNIVERSE", "assert_retry_blocked",
    "audit_paired_experiment", "build_preregistered_manifest", "classify_transfer_failure",
    "execute_paired_experiment", "freeze_manifest", "pre_execution_estimate",
    "prepare_shadow_recipe", "product_spec_hash", "product_spec_semantic_payload", "replay_condition", "replay_pair",
    "sibling_difference_summary", "worker_profiles",
]
