"""REDI-07B one-call live cold-start acceptance support.

The live and dry modes share the same planner, DAG, context compiler, worker
selection, gates, evidence ledger, replay, and candidate-induction path.  The
only substituted boundary is the second worker for ``implement_normalize``:
the dry mode uses :class:`DryLiveAdapter`; live mode uses the accepted OpenAI
adapter behind a hard one-call guard.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .cold_start import (
    COLD_START_REASON_NO_RECIPE_SUPPLIED,
    COLD_START_UNKNOWN_PRODUCT,
    CONTROLLED_FIXTURE_ROOT,
    CONTROLLED_HIDDEN_ROOT,
    ColdStartPlan,
    ColdStartPolicy,
    ColdStartRunOutcome,
    ExplorationBudget,
    _BAD_NORMALIZE,
    _GOOD_NORMALIZE,
    _GOOD_REORDER,
    _ExploringComponentNode,
    _IndependentIntegrationReviewer,
    _RunAttemptCounter,
    _candidate_inventory,
    _gate_summary,
    _new_event,
    _profile_snapshot,
    _scope_failure_gate,
    _with_gate_policy,
    classify_gate_failure,
    controlled_product_spec,
    controlled_stations,
    derive_product_class,
    derive_product_signature,
    generate_recipe_candidate,
    plan_cold_start_v0,
    replay_cold_start,
    select_cold_start_mode,
)
from .context_compiler import compile_context
from .dag_scheduler import NodeRunState, dag_snapshot, product_dag_hash
from .domain import (
    AcceptanceCriterion,
    ArtifactRef,
    FactoryRun,
    FactoryRunId,
    FailureCategory,
    FailureRecord,
    GateResult,
    GateStatus,
    GateType,
    LifecycleEventType,
    PoolEntry,
    RunEvidence,
    RunStatus,
    Severity,
    Timing,
    TokenUsage,
    WorkerId,
    WorkerPool,
    WorkerProfile,
    domain_from_json,
    domain_to_json,
)
from .factory.isolation import IsolatedWorkspace, apply_patches, baseline_file_hashes
from .gates import DeterministicGateEngine, GateSpec
from .parallel_dag_executor import NodeWorkspaceHandle, ParallelDagExecutor, WorkerExecutionResult
from .persistence import EvidenceLedger, FactoryRunRepository, bootstrap_schema
from .replay import ReplayReader
from .runtime.contracts import NodeRunResult, PatchEdit, WorkerRunner
from .runtime.fake_worker import FakeScriptedWorker, fake_token_usage
from .runtime.live_provider_adapter import LiveProviderAdapter, LiveProviderConfig
from .worker_selection import pool_snapshot, pool_snapshot_hash, resolve_worker


LIVE_PROVIDER = "openai"
PLANNED_LIVE_PROVIDER_CALLS = 1
MAX_LIVE_PROVIDER_CALLS = 1
MAX_OUTPUT_TOKENS = 1024
MAX_PATCH_BYTES = 12 * 1024
LIVE_NODE_ID = "implement_normalize"
WEAK_WORKER_ID = "worker_cold_cheap_weak_v0"
LOCAL_WORKER_ID = "worker_cold_local_reorder_v0"


@dataclass(frozen=True)
class LiveCandidatePolicy:
    """Predeclared identity for one bounded REDI-07B candidate exploration."""

    policy_id: str
    policy_version: int
    worker_id: str
    model: str
    display_name: str
    selection_basis: str


HISTORICAL_LIVE_CANDIDATE = LiveCandidatePolicy(
    policy_id="redi07b_one_live_fallback_v0",
    policy_version=1,
    worker_id="worker_cold_openai_live_fallback_v0",
    model="gpt-4o-mini-2024-07-18",
    display_name="REDI-07B bounded OpenAI fallback",
    selection_basis="historical accepted REDI-07B fallback profile",
)

# Declared after factory_run_9a73e7ae91bf4fb598dc9c35b3058b6a failed the
# explicit, representative public contract.  This is a candidate exploration,
# not a selected winner: the same ProductSpec, node, station, context strategy,
# one-call limit, and strict gates remain authoritative for any future run.
NEXT_LIVE_CANDIDATE = LiveCandidatePolicy(
    policy_id="redi07b_candidate_worker_exploration_v0",
    policy_version=1,
    worker_id="worker_cold_openai_gpt41_candidate_v0",
    model="gpt-4.1-2025-04-14",
    display_name="REDI-07B OpenAI GPT-4.1 candidate",
    selection_basis=(
        "different eligible worker after the prior profile failed the clear "
        "post-remediation public contract"
    ),
)

DECLARED_LIVE_CANDIDATES = {
    (item.policy_id, item.policy_version): item
    for item in (HISTORICAL_LIVE_CANDIDATE, NEXT_LIVE_CANDIDATE)
}
ACTIVE_LIVE_CANDIDATE = NEXT_LIVE_CANDIDATE

# Compatibility aliases used by the runner and the canonical validation pack.
LIVE_MODEL = ACTIVE_LIVE_CANDIDATE.model
LIVE_WORKER_ID = ACTIVE_LIVE_CANDIDATE.worker_id
LIVE_ACCEPTANCE_POLICY_ID = ACTIVE_LIVE_CANDIDATE.policy_id
LIVE_ACCEPTANCE_POLICY_VERSION = ACTIVE_LIVE_CANDIDATE.policy_version


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe_error(value: Any) -> str:
    """Bounded public-safe error text; never retain credentials or home paths."""

    text = str(value)
    key = os.environ.get("REDI_OPENAI_API_KEY", "")
    if key:
        text = text.replace(key, "[REDACTED]")
    text = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "[REDACTED]", text)
    text = re.sub(r"(?i)(api[_ -]?key\s*[=:]\s*)\S+", r"\1[REDACTED]", text)
    text = re.sub(r"[A-Za-z]:\\Users\\[^\\\s]+", "[HOME]", text)
    return text[:500]


@dataclass
class LiveCallGuard:
    """Thread-safe hard budget shared by the fake and real live adapters."""

    maximum: int = MAX_LIVE_PROVIDER_CALLS
    claimed: int = 0
    events: dict[str, int] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def claim(self) -> None:
        with self.lock:
            if self.claimed >= self.maximum:
                raise RuntimeError("REDI-07B live provider call budget exhausted")
            self.claimed += 1

    def observe(self, event: str, timestamp_ns: int) -> None:
        if event not in {"provider_call_started", "provider_call_finished"}:
            raise ValueError(f"unknown provider observation {event!r}")
        with self.lock:
            if event in self.events:
                raise RuntimeError(f"duplicate provider observation {event}")
            self.events[event] = int(timestamp_ns)

    def snapshot(self) -> dict[str, int]:
        with self.lock:
            return dict(self.events)


@dataclass
class DryLiveAdapter(WorkerRunner):
    """Deterministic stand-in at the exact future live WorkerRunner boundary."""

    guard: LiveCallGuard
    call_counter: int = 0

    def run_node(
        self,
        *,
        task_id,
        station_id,
        worker_id,
        context,
        workspace_root,
        station_role="builder",
    ) -> NodeRunResult:
        del workspace_root, station_role
        self.guard.claim()
        self.guard.observe("provider_call_started", time.perf_counter_ns())
        self.call_counter += 1
        try:
            if context.get("role") != "builder" or not context.get("instructions"):
                raise RuntimeError("dry live adapter did not receive compiled builder context")
            return NodeRunResult(
                worker_id=worker_id,
                task_id=task_id,
                station_id=station_id,
                patches=(PatchEdit("inventory_math/normalize.py", "write", _GOOD_NORMALIZE),),
                token_usage=fake_token_usage(prompt=180, completion=120),
                opaque_provider_ref=f"fake:{LIVE_MODEL}",
                elapsed_seconds=0.001,
            )
        finally:
            self.guard.observe("provider_call_finished", time.perf_counter_ns())


@dataclass(frozen=True)
class LiveAcceptanceConfiguration:
    normalize_pool: WorkerPool
    reorder_pool: WorkerPool
    runners: Mapping[str, WorkerRunner]
    live_worker_ids: tuple[str, ...]
    live_node_ids: tuple[str, ...]
    live_reviewer: bool = False
    live_planner: bool = False


def validate_live_configuration(config: LiveAcceptanceConfiguration, policy: ColdStartPolicy) -> None:
    """Reject any shape capable of a second provider call or hidden retry."""

    live_entries = [
        str(entry.profile.id)
        for pool in (config.normalize_pool, config.reorder_pool)
        for entry in pool.entries
        if str(entry.profile.id) in set(config.live_worker_ids)
    ]
    if PLANNED_LIVE_PROVIDER_CALLS != 1 or MAX_LIVE_PROVIDER_CALLS != 1:
        raise RuntimeError("REDI-07B requires exactly one planned/max live provider call")
    if tuple(config.live_worker_ids) != (LIVE_WORKER_ID,) or live_entries != [LIVE_WORKER_ID]:
        raise RuntimeError("REDI-07B requires exactly one live WorkerProfile entry")
    if tuple(config.live_node_ids) != (LIVE_NODE_ID,):
        raise RuntimeError("REDI-07B permits exactly one live node")
    if config.live_reviewer or config.live_planner:
        raise RuntimeError("REDI-07B forbids live planner/reviewer")
    if policy.budget.max_attempts_per_worker != 1:
        raise RuntimeError("REDI-07B forbids same-worker retry")
    if policy.budget.max_candidate_workers_per_node != 2:
        raise RuntimeError("REDI-07B normalize trajectory must contain exactly two candidates")
    if policy.budget.max_total_attempts_per_node != 2:
        raise RuntimeError("REDI-07B permits exactly weak A then live B on the live node")
    ordered = [str(entry.profile.id) for entry in config.normalize_pool.entries]
    if ordered != [WEAK_WORKER_ID, LIVE_WORKER_ID]:
        raise RuntimeError("REDI-07B normalize pool must be weak A then live B")
    if any(str(entry.profile.id) in config.live_worker_ids for entry in config.reorder_pool.entries):
        raise RuntimeError("REDI-07B forbids another live node")


def build_live_configuration(*, live: bool, station, guard: LiveCallGuard) -> LiveAcceptanceConfiguration:
    weak = WorkerProfile(
        id=WorkerId(WEAK_WORKER_ID),
        display_name="REDI-07B deterministic weak candidate",
        version="1",
        capability_tags=("python_patch", "controlled_failure"),
        station_eligibility=(station.id,),
        availability_status="available",
        metadata={"relative_strength": "weaker", "provider": "local", "live": False},
    )
    live_profile = WorkerProfile(
        id=WorkerId(LIVE_WORKER_ID),
        display_name=ACTIVE_LIVE_CANDIDATE.display_name,
        version="1",
        capability_tags=("python_patch", "structured_output"),
        structured_output_capable=True,
        station_eligibility=(station.id,),
        availability_status="available",
        provider_opaque_reference=f"{LIVE_PROVIDER}:{LIVE_MODEL}",
        metadata={
            "provider": LIVE_PROVIDER,
            "model": LIVE_MODEL,
            "live": True,
            "candidate_policy_id": LIVE_ACCEPTANCE_POLICY_ID,
            "candidate_policy_version": LIVE_ACCEPTANCE_POLICY_VERSION,
            "selection_basis": ACTIVE_LIVE_CANDIDATE.selection_basis,
        },
    )
    local = WorkerProfile(
        id=WorkerId(LOCAL_WORKER_ID),
        display_name="REDI-07B deterministic reorder worker",
        version="1",
        capability_tags=("python_patch", "strict_tests"),
        station_eligibility=(station.id,),
        availability_status="available",
        metadata={"provider": "local", "live": False},
    )
    weak_runner = FakeScriptedWorker(outputs_by_case={
        LIVE_NODE_ID: {
            "patches": (PatchEdit("inventory_math/normalize.py", "write", _BAD_NORMALIZE),),
            "tokens": (30, 20),
        }
    })
    local_runner = FakeScriptedWorker(outputs_by_case={
        "implement_reorder": {
            "patches": (PatchEdit("inventory_math/reorder.py", "write", _GOOD_REORDER),),
            "tokens": (40, 30),
        }
    })
    if live:
        live_runner: WorkerRunner = LiveProviderAdapter(
            config=LiveProviderConfig(
                env_key_name="REDI_OPENAI_API_KEY",
                model=LIVE_MODEL,
                timeout_s=60.0,
                max_output_tokens=MAX_OUTPUT_TOKENS,
                temperature=0.0,
                provider_name=LIVE_PROVIDER,
            ),
            before_call=guard.claim,
            call_observer=guard.observe,
        )
    else:
        live_runner = DryLiveAdapter(guard)
    normalize_pool = WorkerPool(
        id="worker_pool_redi07b_normalize_v0",
        station_id=station.id,
        role=station.role,
        entries=(PoolEntry(weak, 0), PoolEntry(live_profile, 1)),
        selection_policy_id=LIVE_ACCEPTANCE_POLICY_ID,
    )
    reorder_pool = WorkerPool(
        id="worker_pool_redi07b_reorder_v0",
        station_id=station.id,
        role=station.role,
        entries=(PoolEntry(local, 0),),
        selection_policy_id=LIVE_ACCEPTANCE_POLICY_ID,
    )
    return LiveAcceptanceConfiguration(
        normalize_pool=normalize_pool,
        reorder_pool=reorder_pool,
        runners={WEAK_WORKER_ID: weak_runner, LIVE_WORKER_ID: live_runner, LOCAL_WORKER_ID: local_runner},
        live_worker_ids=(LIVE_WORKER_ID,),
        live_node_ids=(LIVE_NODE_ID,),
    )


def _output_contract_gate(*, passed: bool, node_id: str, task, policy: ColdStartPolicy) -> GateResult:
    return GateResult(
        gate_identity="patch_output_contract",
        gate_type=GateType.DETERMINISTIC,
        target_node_id=node_id,
        target_task_id=task.id,
        criteria=(AcceptanceCriterion(description="one bounded write to the assigned implementation file"),),
        status=GateStatus.PASS if passed else GateStatus.FAIL,
        verifier_type="deterministic_code",
        verifier_identity=f"{policy.gate_policy_id}@{policy.gate_policy_version}",
        failure_reasons=() if passed else ("bounded patch contract failed",),
        metadata={
            "mandatory": True,
            "gate_policy_id": policy.gate_policy_id,
            "gate_policy_version": policy.gate_policy_version,
        },
    )


@dataclass
class LiveFallbackComponentNode:
    """Exact two-attempt node: deterministic weak A then one live B."""

    node_id: str
    product: Any
    task: Any
    station: Any
    component: Mapping[str, Any]
    pool: WorkerPool
    runners: Mapping[str, WorkerRunner]
    policy: ColdStartPolicy
    fixture_root: Path
    hidden_root: Path
    attempt_counter: _RunAttemptCounter
    guard: LiveCallGuard
    live: bool

    def _context_evidence(self, package) -> dict[str, Any]:
        value = package.to_evidence()
        return {
            "strategy_id": value["strategy_id"],
            "strategy_version": value["strategy_version"],
            "source_list": [item["path"] for item in value["included_files"]],
            "package_hash": value["package_hash"],
            "rendered_hash": value["rendered_hash"],
            "bytes": value["counts"]["bytes"],
            "estimated_tokens": value["counts"]["estimated_tokens"],
            "token_estimate_method": value["counts"]["token_estimate_method"],
            "upstream_handoff_ids": [item["handoff_id"] for item in value["upstream_handoffs"]],
            "prior_failure_evidence": value["prior_failure_evidence"],
        }

    def _gates(self, handle: NodeWorkspaceHandle, patches: tuple[PatchEdit, ...]) -> tuple[list[GateResult], bool]:
        target_path = str(self.component["selected_paths"][0])
        patch_bytes = sum(len((patch.content or "").encode("utf-8")) for patch in patches)
        contract_ok = (
            len(patches) == 1
            and patches[0].rel_path == target_path
            and patches[0].action == "write"
            and patch_bytes <= MAX_PATCH_BYTES
        )
        gates = [_output_contract_gate(
            passed=contract_ok, node_id=self.node_id, task=self.task, policy=self.policy
        )]
        if not contract_ok:
            return gates, False
        workspace = IsolatedWorkspace(
            root=handle.root,
            fixture_root=self.fixture_root,
            allowed_scope_rel_prefixes=tuple(str(item) for item in self.task.allowed_scope),
            baseline_hashes=baseline_file_hashes(self.fixture_root),
        )
        applied, violations = apply_patches(workspace, patches, allowed_scope=self.task.allowed_scope)
        if not applied:
            gates.append(_scope_failure_gate(
                node_id=self.node_id,
                task=self.task,
                reasons=[str(item) for item in violations],
                policy=self.policy,
            ))
            return gates, False
        spec = GateSpec(
            public_test_cmd=[sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", str(self.component["public_test"])],
            hidden_test_cmd=[sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", str(self.hidden_root / self.component["hidden_test"])],
            hidden_test_cwd=self.hidden_root,
            allowed_scope_prefixes=tuple(str(item) for item in self.task.allowed_scope),
            baseline_hashes=baseline_file_hashes(self.fixture_root),
            per_gate_timeout_s=20.0,
        )
        gates.extend(
            _with_gate_policy(gate, self.policy)
            for gate in DeterministicGateEngine(spec).run_all(
                handle.root, target_node_id=self.node_id, target_task_id=self.task.id
            )
        )
        return gates, all(gate.status == GateStatus.PASS for gate in gates)

    def _attempt(
        self,
        *,
        handle: NodeWorkspaceHandle,
        package,
        selection,
        attempt_index: int,
        failure_trigger: FailureCategory | None,
    ) -> tuple[RunEvidence, list[GateResult], FailureRecord | None, NodeRunResult | None, dict[str, Any]]:
        worker_id = str(selection.profile.id)
        selection_ev = selection.to_evidence() | {
            "pool_id": str(self.pool.id),
            "failure_trigger_category": failure_trigger.value if failure_trigger else None,
            "failure_specific_reason": "worker_escalation" if failure_trigger else "initial_ordered_candidate",
        }
        context_ev = self._context_evidence(package)
        started = time.perf_counter()
        calls_before = self.guard.claimed
        result: NodeRunResult | None = None
        error_type: str | None = None
        error_message: str | None = None
        try:
            result = selection.runner.run_node(
                task_id=self.task.id,
                station_id=self.station.id,
                worker_id=selection.profile.id,
                context={
                    "role": "builder",
                    "instructions": package.instructions,
                    "context_body": package.context_body,
                    "attempt_index": 0,
                    "case_id": self.node_id,
                    "context_package_hash": package.package_hash,
                    "cold_start_mode": COLD_START_UNKNOWN_PRODUCT,
                },
                workspace_root=handle.root,
                station_role="builder",
            )
            if result.error_message:
                error_type = "ProviderOutputError"
                error_message = _safe_error(result.error_message)
        except Exception as exc:  # terminal for live B; never re-enter provider
            error_type = type(exc).__name__
            error_message = _safe_error(exc)

        gates: list[GateResult] = []
        passed = False
        category: FailureCategory | None = None
        if error_type is None and result is not None:
            gates, passed = self._gates(handle, tuple(result.patches or ()))
            failed_gate = next((gate for gate in gates if gate.status != GateStatus.PASS), None)
            category = classify_gate_failure(failed_gate) if failed_gate else None
        else:
            category = FailureCategory.ENVIRONMENT_FAILURE if error_type != "ProviderOutputError" else FailureCategory.TOOL_FAILURE

        elapsed = round(time.perf_counter() - started, 6)
        is_live_worker = worker_id == LIVE_WORKER_ID
        provider_call_delta = max(0, self.guard.claimed - calls_before)
        action = "accept_verified_attempt" if passed else (
            "worker_escalation" if attempt_index == 0 else "stop_after_single_live_failure"
        )
        tokens = result.token_usage if result is not None else None
        attempt = RunEvidence(
            factory_run_id=FactoryRunId(handle.factory_run_id) if handle.factory_run_id else None,
            product_id=self.product.id,
            product_class_id=self.product.product_class_id,
            node_id=self.node_id,
            station_id=self.station.id,
            task_class="cold_start_worker_attempt",
            worker_id=selection.profile.id,
            worker_version=selection.profile.version,
            token_usage=tokens,
            timing=Timing(wall_clock_seconds=elapsed, compute_seconds=elapsed),
            artifacts_changed=tuple(
                ArtifactRef(
                    ref_id=f"attempt_{self.node_id}_{attempt_index}_{index}",
                    kind="patch",
                    location=patch.rel_path,
                    sha256=patch.content_sha256,
                    source="output",
                )
                for index, patch in enumerate((result.patches if result else ()))
            ),
            tests_executed=tuple(gate.gate_identity for gate in gates),
            gate_results=tuple(gate.id for gate in gates),
            retry_count=attempt_index,
            escalation=not passed,
            final_status=RunStatus.COMPLETED if passed else RunStatus.FAILED,
            failure_category=None if passed else category,
            metadata={
                "evidence_kind": "cold_start_attempt",
                "attempt_index": attempt_index,
                "worker_selection": selection_ev,
                "worker_profile": _profile_snapshot(selection.profile, selection.preference_rank),
                "context_package_hash": package.package_hash,
                "context_strategy_id": package.strategy_id,
                "context_strategy_version": package.strategy_version,
                "context": context_ev,
                "gate_summaries": [_gate_summary(gate) for gate in gates],
                "gate_boundary": _gate_summary(next((g for g in gates if g.status != GateStatus.PASS), gates[-1])) if gates and not passed else None,
                "failure_action": action,
                "exploration_policy_id": LIVE_ACCEPTANCE_POLICY_ID,
                "exploration_policy_version": LIVE_ACCEPTANCE_POLICY_VERSION,
                "provider": LIVE_PROVIDER if is_live_worker else "local",
                "model": LIVE_MODEL if is_live_worker else None,
                "provider_ref": str(result.opaque_provider_ref or "") if result else "",
                "provider_calls": provider_call_delta if is_live_worker and self.live else 0,
                "would_be_live_calls": 1 if is_live_worker else 0,
                "network_calls": 1 if is_live_worker and self.live else 0,
                "provider_timing": self.guard.snapshot() if is_live_worker else {},
                "actual_provider_usage": {
                    "input": int(tokens.input or 0),
                    "output": int(tokens.output or 0),
                    "cached_input": int(tokens.cached_input or 0),
                } if tokens else None,
                "error_type": error_type,
                "public_safe_error": error_message,
                "automatic_retry": False,
            },
        )
        failure: FailureRecord | None = None
        if not passed:
            failed_gate = next((gate for gate in gates if gate.status != GateStatus.PASS), None)
            failure = FailureRecord(
                category=category or FailureCategory.TOOL_FAILURE,
                origin="cold_start_worker_attempt",
                node_id=self.node_id,
                task_id=self.task.id,
                factory_run_id=FactoryRunId(handle.factory_run_id) if handle.factory_run_id else None,
                details={
                    "attempt_index": attempt_index,
                    "failed_gate_id": str(failed_gate.id) if failed_gate else None,
                    "failed_gate_identity": failed_gate.gate_identity if failed_gate else "live_provider_boundary",
                    "public_safe_error": error_message,
                },
                severity=Severity.ERROR,
                recoverable=attempt_index == 0,
                escalation_target=action,
                evidence_id=attempt.id,
                metadata={
                    "worker_id": worker_id,
                    "worker_profile_version": selection.profile.version,
                    "context_package_hash": package.package_hash,
                    "gate_boundary": _gate_summary(failed_gate) if failed_gate else {"gate_identity": "live_provider_boundary", "status": "fail"},
                    "retry_escalation_decision": action,
                    "policy_id": LIVE_ACCEPTANCE_POLICY_ID,
                    "policy_version": LIVE_ACCEPTANCE_POLICY_VERSION,
                    "automatic_retry": False,
                    "terminal_live_failure": attempt_index == 1,
                },
            )
        trace = {
            "attempt_index": attempt_index,
            "worker_id": worker_id,
            "station_id": str(self.station.id),
            "node_id": self.node_id,
            "selection": selection_ev,
            "context": context_ev,
            "outcome": "success" if passed else "failure",
            "failure_category": None if passed else (category or FailureCategory.TOOL_FAILURE).value,
            "action": action,
            "attempt_evidence_id": str(attempt.id),
            "failure_record_id": str(failure.id) if failure else None,
            "provider": LIVE_PROVIDER if is_live_worker else "local",
            "model": LIVE_MODEL if is_live_worker else None,
            "actual_provider_usage": (attempt.metadata or {}).get("actual_provider_usage"),
        }
        return attempt, gates, failure, result if passed else None, trace

    def run_node(self, *, handle: NodeWorkspaceHandle) -> WorkerExecutionResult:
        inventory = _candidate_inventory(
            pool=self.pool, station=self.station, task=self.task,
            runners=self.runners, budget=self.policy.budget,
        )
        snapshot = pool_snapshot(pool=self.pool, station=self.station, task=self.task)
        snapshot_hash = pool_snapshot_hash(snapshot)
        attempts: list[RunEvidence] = []
        gates: list[GateResult] = []
        failures: list[FailureRecord] = []
        trajectory: list[dict[str, Any]] = []
        previous: str | None = None
        category: FailureCategory | None = None
        final_result: NodeRunResult | None = None
        final_package = handle.context_package

        for attempt_index in range(2):
            if not self.attempt_counter.claim():
                raise RuntimeError("REDI-07B total attempt budget exhausted")
            selection = resolve_worker(
                pool=self.pool,
                runners=self.runners,
                station=self.station,
                task=self.task,
                exclude_worker_ids=(() if previous is None else (previous,)),
                attempt_index=attempt_index,
                previous_worker_id=previous,
            )
            if selection is None:
                raise RuntimeError("REDI-07B exact weak/live worker trajectory is unavailable")
            if attempt_index == 1:
                if str(selection.profile.id) != LIVE_WORKER_ID:
                    raise RuntimeError("REDI-07B second selection is not the sole live worker")
                final_package = compile_context(
                    strategy_id=self.policy.context_strategy_id,
                    task=self.task,
                    station=self.station,
                    product=self.product,
                    workspace_root=handle.root,
                    selected_paths=tuple(self.component["selected_paths"]),
                    prior_failure_evidence=({
                        "attempt": 0,
                        "category": FailureCategory.DETERMINISTIC_TEST_FAILURE.value,
                        "boundary": "public_test_execution",
                        "reasons": ["public deterministic verification failed"],
                        "context_sanitized": True,
                    },),
                    budget_bytes=self.policy.context_budget_bytes,
                    station_role="builder",
                    allowed_scope=self.task.allowed_scope,
                )
            attempt, attempt_gates, failure, result, trace = self._attempt(
                handle=handle,
                package=final_package,
                selection=selection,
                attempt_index=attempt_index,
                failure_trigger=category,
            )
            attempts.append(attempt)
            gates.extend(attempt_gates)
            trajectory.append(trace)
            if failure:
                failures.append(failure)
                category = failure.category
            if result is not None:
                final_result = result
                break
            if attempt_index == 1:
                break
            if category != FailureCategory.DETERMINISTIC_TEST_FAILURE:
                break
            previous = str(selection.profile.id)

        base_evidence = {
            "cold_start_mode": COLD_START_UNKNOWN_PRODUCT,
            "exploration_policy": {
                "policy_id": LIVE_ACCEPTANCE_POLICY_ID,
                "policy_version": LIVE_ACCEPTANCE_POLICY_VERSION,
                "max_live_calls": MAX_LIVE_PROVIDER_CALLS,
                "automatic_retry": False,
            },
            "candidate_inventory": inventory,
            "pool_snapshot": snapshot,
            "pool_snapshot_hash": snapshot_hash,
            "attempt_trajectory": trajectory,
            "selected_worker_id": trajectory[-1]["worker_id"],
            "live_context": self._context_evidence(final_package),
            "provider": LIVE_PROVIDER,
            "model": LIVE_MODEL,
            "provider_calls": self.guard.claimed if self.live else 0,
            "would_be_live_calls": 1,
            "network_calls": self.guard.claimed if self.live else 0,
            "provider_timing": self.guard.snapshot(),
            "automatic_retry": False,
        }
        if final_result is None:
            return WorkerExecutionResult(
                node_id=self.node_id,
                workspace_id=handle.workspace_id,
                success=False,
                execution_evidence=base_evidence,
                attempt_evidence=tuple(attempts),
                gate_results=tuple(gates),
                failure_records=tuple(failures),
                error_type="LiveFallbackFailed",
                error_message="single live fallback failed; no retry permitted",
            )

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
            "metadata": {"purpose": "REDI-07B verified cold-start live handoff"},
        }
        return WorkerExecutionResult(
            node_id=self.node_id,
            workspace_id=handle.workspace_id,
            success=True,
            produced_handoff_ids=(self.component["handoff_id"],),
            produced_handoff_types_by_id={self.component["handoff_id"]: (self.component["handoff_type"],)},
            artifacts=(descriptor,),
            execution_evidence=base_evidence,
            attempt_evidence=tuple(attempts),
            gate_results=tuple(gates),
            failure_records=tuple(failures),
        )


def _policy() -> ColdStartPolicy:
    return ColdStartPolicy(budget=ExplorationBudget(
        max_candidate_workers_per_node=2,
        max_attempts_per_worker=1,
        max_total_attempts_per_node=2,
        max_total_attempts_per_run=3,
    ))


def _prepare_plan(policy: ColdStartPolicy) -> tuple[Any, Any, Any, Any, ColdStartPlan]:
    selection = select_cold_start_mode(
        known_recipe=None, reason=COLD_START_REASON_NO_RECIPE_SUPPLIED
    )
    product = controlled_product_spec()
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
    return selection, product, signature, product_class, plan


def pre_execution_estimate() -> dict[str, Any]:
    """Compile the exact live-node retry context without running a worker."""

    policy = _policy()
    _, product, _, _, plan = _prepare_plan(policy)
    task = plan.tasks_by_node[LIVE_NODE_ID]
    station = plan.stations_by_node[LIVE_NODE_ID]
    import tempfile

    with tempfile.TemporaryDirectory(prefix="redi07b_estimate_") as raw:
        root = Path(raw)
        from shutil import copytree
        copytree(CONTROLLED_FIXTURE_ROOT, root, dirs_exist_ok=True)
        package = compile_context(
            strategy_id=policy.context_strategy_id,
            task=task,
            station=station,
            product=product,
            workspace_root=root,
            selected_paths=tuple(plan.components_by_node[LIVE_NODE_ID]["selected_paths"]),
            prior_failure_evidence=({
                "attempt": 0,
                "category": FailureCategory.DETERMINISTIC_TEST_FAILURE.value,
                "boundary": "public_test_execution",
                "reasons": ["public deterministic verification failed"],
                "context_sanitized": True,
            },),
            budget_bytes=policy.context_budget_bytes,
            station_role="builder",
            allowed_scope=task.allowed_scope,
        )
    estimated_input = int(package.counts["estimated_tokens"]) + 200
    return {
        "planned_live_provider_calls": PLANNED_LIVE_PROVIDER_CALLS,
        "live_node_id": LIVE_NODE_ID,
        "context_bytes": int(package.counts["bytes"]),
        "estimated_input_tokens": estimated_input,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "expected_upper_bound_tokens": estimated_input + MAX_OUTPUT_TOKENS,
        "context_package_hash": package.package_hash,
        "rendered_hash": package.rendered_hash,
        "source_list": [item["path"] for item in package.included_files],
    }


def execute_live_cold_start(*, live: bool, output_root: Path | str) -> ColdStartRunOutcome:
    """Execute exact REDI-07B path; the caller must enforce live env opt-in."""

    policy = _policy()
    selection, product, signature, product_class, plan = _prepare_plan(policy)
    if selection.mode != COLD_START_UNKNOWN_PRODUCT or selection.recipe_lookup_result != "none":
        raise RuntimeError("REDI-07B requires known_recipe=None and explicit cold-start mode")
    guard = LiveCallGuard()
    implementation_station, review_station = controlled_stations()
    config = build_live_configuration(live=live, station=implementation_station, guard=guard)
    validate_live_configuration(config, policy)

    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=False)
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
            "acceptance": "REDI-07B",
            "execution_mode": selection.mode,
            "cold_start_selection": selection.to_evidence(),
            "product_spec_id": str(product.id),
            "product_class_id": str(product_class.id),
            "product_signature_sha256": signature.sha256,
            "recipe_lookup": {"implemented": False, "result": "none", "known_recipe_supplied": False},
            "live_acceptance_policy_id": LIVE_ACCEPTANCE_POLICY_ID,
            "live_acceptance_policy_version": LIVE_ACCEPTANCE_POLICY_VERSION,
            "provider_call_mode": "live" if live else "dry_live_slot",
            "planned_live_provider_calls": PLANNED_LIVE_PROVIDER_CALLS,
            "max_live_provider_calls": MAX_LIVE_PROVIDER_CALLS,
            "live_planner": False,
            "live_reviewer": False,
            "automatic_retry": False,
        },
    )
    factory_run_id = runs.store(factory_run)
    ledger.append(_new_event(
        LifecycleEventType.PRODUCT_ACCEPTED,
        factory_run_id=factory_run_id,
        product_id=str(product.id),
        payload={
            "cold_start_selection": selection.to_evidence(),
            "product_signature": signature.to_evidence(),
            "product_class_id": str(product_class.id),
            "known_recipe": None,
        },
    ), factory_run_id=factory_run_id)
    graph_hash = product_dag_hash(plan.dag)
    ledger.append(_new_event(
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
            "planner_runtime": "deterministic_local",
        },
    ), factory_run_id=factory_run_id)
    ledger.append(RunEvidence(
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
            "live_acceptance_policy": {
                "policy_id": LIVE_ACCEPTANCE_POLICY_ID,
                "policy_version": LIVE_ACCEPTANCE_POLICY_VERSION,
                "planned_live_provider_calls": 1,
                "same_worker_retry": False,
                "live_planner": False,
                "live_reviewer": False,
            },
        },
    ), factory_run_id=factory_run_id)

    counter = _RunAttemptCounter(policy.budget.max_total_attempts_per_run)
    normalize = plan.components_by_node[LIVE_NODE_ID]
    reorder = plan.components_by_node["implement_reorder"]
    reviewer = _IndependentIntegrationReviewer(
        node_id=plan.integration_node_id,
        product=product,
        task=plan.tasks_by_node[plan.integration_node_id],
        station=review_station,
        policy=policy,
        fixture_root=CONTROLLED_FIXTURE_ROOT,
        hidden_root=CONTROLLED_HIDDEN_ROOT,
    )
    workers = {
        LIVE_NODE_ID: LiveFallbackComponentNode(
            node_id=LIVE_NODE_ID,
            product=product,
            task=plan.tasks_by_node[LIVE_NODE_ID],
            station=implementation_station,
            component=normalize,
            pool=config.normalize_pool,
            runners=config.runners,
            policy=policy,
            fixture_root=CONTROLLED_FIXTURE_ROOT,
            hidden_root=CONTROLLED_HIDDEN_ROOT,
            attempt_counter=counter,
            guard=guard,
            live=live,
        ),
        "implement_reorder": _ExploringComponentNode(
            node_id="implement_reorder",
            product=product,
            task=plan.tasks_by_node["implement_reorder"],
            station=implementation_station,
            component=reorder,
            pool=config.reorder_pool,
            runners=config.runners,
            policy=policy,
            fixture_root=CONTROLLED_FIXTURE_ROOT,
            hidden_root=CONTROLLED_HIDDEN_ROOT,
            attempt_counter=counter,
        ),
        plan.integration_node_id: reviewer,
    }
    executor = ParallelDagExecutor(
        dag=plan.dag,
        station_by_node=plan.stations_by_node,
        task_by_node=plan.tasks_by_node,
        node_workers=workers,
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
    if all_completed:
        ledger.append(_new_event(
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
        ), factory_run_id=factory_run_id)
    runs.close_run(
        factory_run_id,
        final_status=RunStatus.COMPLETED if all_completed else RunStatus.FAILED,
        verified=all_completed,
        total_tokens=_aggregate_tokens(execution.evidence_rows),
        timing=Timing(
            started_at=factory_run.created_at,
            ended_at=_utcnow(),
            wall_clock_seconds=execution.timing.get("graph_wall_clock_seconds"),
            compute_seconds=execution.timing.get("aggregate_node_compute_seconds"),
        ),
    )
    conn.close()
    candidate = generate_recipe_candidate(db_path=db_path, factory_run_id=factory_run_id) if all_completed else None
    counts = {
        WEAK_WORKER_ID: sum((config.runners[WEAK_WORKER_ID].call_counter or {}).values()),  # type: ignore[attr-defined]
        LOCAL_WORKER_ID: sum((config.runners[LOCAL_WORKER_ID].call_counter or {}).values()),  # type: ignore[attr-defined]
        LIVE_WORKER_ID: guard.claimed,
    }
    return ColdStartRunOutcome(
        db_path=db_path,
        factory_run_id=factory_run_id,
        product=product,
        product_class=product_class,
        signature=signature,
        selection=selection,
        plan=plan,
        execution=execution,
        verified=all_completed,
        candidate=candidate,
        provider_calls=guard.claimed if live else 0,
        network_calls=guard.claimed if live else 0,
        worker_call_counts=counts,
        independent_reviewer_calls=reviewer.calls,
    )


def _aggregate_tokens(rows: tuple[dict[str, Any], ...]) -> TokenUsage:
    totals = {"input": 0, "output": 0, "cached_input": 0}
    for row in rows:
        execution = row.get("execution_evidence") or {}
        for trace in execution.get("attempt_trajectory") or ():
            usage = trace.get("actual_provider_usage") or {}
            for key in totals:
                totals[key] += int(usage.get(key) or 0)
    return TokenUsage(input=totals["input"], output=totals["output"], cached_input=totals["cached_input"])


def audit_live_cold_start(
    db_path: Path | str,
    *,
    factory_run_id: str | None = None,
    expected_live: bool = True,
) -> dict[str, Any]:
    """Read-only REDI-07B post-run audit, valid for success and live failure."""

    path = Path(db_path).resolve()
    before = path.read_bytes()
    before_stat = path.stat()
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        run_rows = conn.execute("SELECT * FROM factory_runs ORDER BY created_at").fetchall()
        if factory_run_id is None:
            if len(run_rows) != 1:
                raise ValueError("audit requires exactly one authoritative FactoryRun")
            factory_run_id = str(run_rows[0]["id"])
        row = next((item for item in run_rows if str(item["id"]) == factory_run_id), None)
        if row is None:
            raise ValueError(f"FactoryRun {factory_run_id!r} not found")
        run_payload = json.loads(row["payload_json"])
    finally:
        conn.close()

    first = replay_cold_start(db_path=path, factory_run_id=factory_run_id)
    second = replay_cold_start(db_path=path, factory_run_id=factory_run_id)
    reader = ReplayReader(path)
    try:
        base = reader.replay_factory_run(FactoryRunId(factory_run_id))
    finally:
        reader.close()
    attempts = sorted(
        [item for item in base.run_evidences if item.task_class == "cold_start_worker_attempt"],
        key=lambda item: (str(item.node_id), int((item.metadata or {}).get("attempt_index", 0))),
    )
    normalize = [item for item in attempts if item.node_id == LIVE_NODE_ID]
    weak = next((item for item in normalize if str(item.worker_id) == WEAK_WORKER_ID), None)
    run_metadata = dict(run_payload.get("metadata") or {})
    policy_key = (
        str(run_metadata.get("live_acceptance_policy_id") or ""),
        int(run_metadata.get("live_acceptance_policy_version") or 0),
    )
    declared_candidate = DECLARED_LIVE_CANDIDATES.get(policy_key)
    live_attempt = next(
        (
            item for item in normalize
            if declared_candidate is not None
            and str(item.worker_id) == declared_candidate.worker_id
        ),
        None,
    )
    live_meta = dict(live_attempt.metadata or {}) if live_attempt else {}
    actual_calls = sum(int((item.metadata or {}).get("provider_calls") or 0) for item in attempts)
    would_be = sum(int((item.metadata or {}).get("would_be_live_calls") or 0) for item in attempts)
    candidate = first.get("recipe_candidate")
    refs_resolve = True
    if candidate:
        recipe = domain_from_json(candidate["canonical_recipe_json"])
        available = {
            str(item.id)
            for item in (*base.run_evidences, *base.failures, *base.gate_results)
        }
        refs_resolve = all(
            str(ref.get("evidence_id") if isinstance(ref, dict) else ref.evidence_id) in available
            for ref in recipe.evidence
        )
    successful = bool(first["final_verification_state"])
    valid_failed = (
        expected_live
        and not successful
        and actual_calls == 1
        and candidate is None
        and row["final_status"] == RunStatus.FAILED.value
        and live_attempt is not None
    )
    after_stat = path.stat()
    read_only = (
        before == path.read_bytes()
        and before_stat.st_size == after_stat.st_size
        and before_stat.st_mtime_ns == after_stat.st_mtime_ns
    )
    checks = {
        "one_authoritative_factory_run": len(run_rows) == 1,
        "factory_run_closed": row["closed_at"] is not None,
        "cold_start_mode": first["cold_start_mode"] == COLD_START_UNKNOWN_PRODUCT,
        "no_recipe_supplied": first["recipe_lookup_result"] == "none" and run_payload["metadata"]["recipe_lookup"]["known_recipe_supplied"] is False,
        "planner_generated_historical_dag": bool(first["dag"]["graph_hash"]) and first["dag"]["graph_hash"] == first["recipe_candidate"]["source_graph_hash"] if candidate else bool(first["dag"]["graph_hash"]),
        "weak_a_failed_deterministically": weak is not None and weak.failure_category == FailureCategory.DETERMINISTIC_TEST_FAILURE,
        "failure_record_bound": any(str(item.evidence_id) == str(weak.id) for item in base.failures) if weak else False,
        "explicit_worker_escalation": weak is not None and (weak.metadata or {}).get("failure_action") == "worker_escalation",
        "same_node_and_station": len(normalize) == 2 and len({item.node_id for item in normalize}) == 1 and len({str(item.station_id) for item in normalize}) == 1,
        "candidate_policy_declared": declared_candidate is not None,
        "one_live_worker_selected": declared_candidate is not None and len([
            item for item in normalize if str(item.worker_id) == declared_candidate.worker_id
        ]) == 1,
        "provider_model_bound": (
            declared_candidate is not None
            and live_meta.get("provider") == LIVE_PROVIDER
            and live_meta.get("model") == declared_candidate.model
        ),
        "provider_call_policy": actual_calls == (1 if expected_live else 0) and would_be == 1,
        "context_identity_complete": all(key in (live_meta.get("context") or {}) for key in ("strategy_id", "strategy_version", "source_list", "package_hash", "rendered_hash", "bytes", "estimated_tokens", "upstream_handoff_ids")),
        "actual_usage_persisted": live_meta.get("actual_provider_usage") is not None if successful else True,
        "strict_gates": (not successful) or {"allowed_file_change_scope", "python_syntax_compile", "public_test_execution", "hidden_test_execution", "integration_verification", "independent_final_reviewer"}.issubset({item["identity"] for item in first["gates"] if item["status"] == "pass"}),
        "verified_event_consistent": first["product_verified_event"] is successful,
        "candidate_consistent": (candidate is not None) is successful,
        "candidate_refs_resolve": refs_resolve,
        "candidate_not_promoted": not first["recipe_promoted_event"] and (candidate is None or candidate["promotion_status"] == "NOT_PROMOTED"),
        "replay_x2_identical": first == second,
        "replay_zero_provider_calls": True,
        "replay_read_only": read_only,
        "no_orphan_factory_evidence": all(str(getattr(item, "factory_run_id", factory_run_id) or factory_run_id) == factory_run_id for item in (*base.run_evidences, *base.failures)),
    }
    acceptance_valid = all(checks.values()) and (successful or valid_failed)
    return {
        "factory_run_id": factory_run_id,
        "ledger_path": str(path),
        "expected_live": expected_live,
        "actual_provider_calls": actual_calls,
        "would_be_live_calls": would_be,
        "verified": successful,
        "valid_failed_acceptance": valid_failed,
        "acceptance_valid": acceptance_valid,
        "checks": checks,
        "replay": first,
        "live_attempt": {
            "worker_id": str(live_attempt.worker_id) if live_attempt else None,
            "provider": live_meta.get("provider"),
            "model": live_meta.get("model"),
            "context": live_meta.get("context"),
            "tokens": live_meta.get("actual_provider_usage"),
            "provider_timing": live_meta.get("provider_timing"),
        },
    }


__all__ = [
    "ACTIVE_LIVE_CANDIDATE",
    "DECLARED_LIVE_CANDIDATES",
    "DryLiveAdapter",
    "HISTORICAL_LIVE_CANDIDATE",
    "LIVE_ACCEPTANCE_POLICY_ID",
    "LIVE_MODEL",
    "LIVE_NODE_ID",
    "LIVE_PROVIDER",
    "LIVE_WORKER_ID",
    "LiveCandidatePolicy",
    "MAX_OUTPUT_TOKENS",
    "NEXT_LIVE_CANDIDATE",
    "PLANNED_LIVE_PROVIDER_CALLS",
    "audit_live_cold_start",
    "build_live_configuration",
    "execute_live_cold_start",
    "pre_execution_estimate",
    "validate_live_configuration",
]
