"""REDI-06C live-provider parallel acceptance (owner-executed live call).

Default policy is non-live.  ``--dry-run`` exercises the exact DAG/context/
WorkerPool/evidence path with two deterministic fake provider slots.  A live
run requires REDI_LIVE_PROVIDER=1 and REDI_OPENAI_API_KEY and makes exactly two
provider calls: node A and node B.  Node C is always deterministic and local.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from redi_lab.context_compiler import DEFAULT_BUDGET_BYTES
from redi_lab.dag_scheduler import (
    DagValidationError,
    NodeRunState,
    canonical_replay_view,
    replay_dag_run,
)
from redi_lab.domain import (
    AcceptanceCriterion,
    DagDependency,
    DagNode,
    FactoryRun,
    FactoryRunId,
    Mode,
    PoolEntry,
    ProductDAG,
    ProductSpec,
    Requirement,
    RiskTier,
    RunStatus,
    StationContract,
    TaskContract,
    Timing,
    TokenUsage,
    WorkerId,
    WorkerPool,
    WorkerProfile,
    domain_from_json,
    new_id,
)
from redi_lab.factory.isolation import IsolatedWorkspace, apply_patches, baseline_file_hashes
from redi_lab.gates import DeterministicGateEngine, GateSpec
from redi_lab.parallel_dag_executor import (
    NodeWorkspaceHandle,
    ParallelDagExecutor,
    ParallelDagRunResult,
    WorkerExecutionResult,
)
from redi_lab.persistence import (
    EvidenceLedger,
    FactoryRunRepository,
    IncompleteEvidenceError,
    bootstrap_schema,
    payload_hash_from_json,
    verify_chain,
)
from redi_lab.runtime.contracts import NodeRunResult, PatchEdit, WorkerRunner
from redi_lab.runtime.fake_worker import fake_token_usage
from redi_lab.runtime.live_provider_adapter import LiveProviderAdapter, LiveProviderConfig
from redi_lab.worker_selection import pool_snapshot, pool_snapshot_hash, resolve_worker


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "experiments" / "fixtures" / "dag_pair_pkg"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "artifacts" / "redi06c"
MODEL = "gpt-4o-mini-2024-07-18"
MAX_OUTPUT_TOKENS = 256
EXPECTED_PROVIDER_CALLS = 2
MAX_PATCH_BYTES = 4096

ALPHA_IMPL = (
    '"""Small uppercase helper."""\n\n'
    "from __future__ import annotations\n\n\n"
    "def alpha(text: str) -> str:\n"
    "    return text.upper()\n"
)
BETA_IMPL = (
    '"""Small reverse helper."""\n\n'
    "from __future__ import annotations\n\n\n"
    "def beta(text: str) -> str:\n"
    "    return text[::-1]\n"
)


def _json_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _safe_error(value: Any) -> str:
    text = str(value)
    key = os.environ.get("REDI_OPENAI_API_KEY", "")
    if key:
        text = text.replace(key, "[REDACTED]")
    text = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "[REDACTED]", text)
    return text[:500]


@dataclass
class CallObservation:
    """Per-node observation only; no response state is shared between workers."""

    node_id: str
    values: dict[str, int] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, event: str, timestamp_ns: int) -> None:
        if event not in {"provider_call_started", "provider_call_finished"}:
            raise ValueError(f"unknown provider observation {event!r}")
        with self.lock:
            if event in self.values:
                raise RuntimeError(f"duplicate {event} for {self.node_id}")
            self.values[event] = int(timestamp_ns)

    def snapshot(self) -> dict[str, int]:
        with self.lock:
            return dict(self.values)


@dataclass
class DryProviderRunner(WorkerRunner):
    """Network-free stand-in at the same WorkerRunner boundary as live."""

    patch: PatchEdit
    barrier: threading.Barrier
    observation: CallObservation

    def run_node(self, *, task_id, station_id, worker_id, context, workspace_root,
                 station_role="builder") -> NodeRunResult:
        self.barrier.wait(timeout=10)
        self.observation.record("provider_call_started", time.perf_counter_ns())
        try:
            # Dry-run-only rendezvous proves both would-be provider slots are
            # concurrently inside the call boundary. Live calls have no
            # artificial delay and use only the pre-call barrier.
            self.barrier.wait(timeout=10)
            return NodeRunResult(
                worker_id=worker_id,
                task_id=task_id,
                station_id=station_id,
                patches=(self.patch,),
                token_usage=fake_token_usage(prompt=64, completion=24),
                opaque_provider_ref="fake:dry-run",
                elapsed_seconds=0.001,
            )
        finally:
            self.observation.record("provider_call_finished", time.perf_counter_ns())


@dataclass
class PoolBoundLiveNode:
    node_id: str
    station: StationContract
    task: TaskContract
    pool: WorkerPool
    runners: dict[str, WorkerRunner]
    target_path: str
    public_test_path: str
    handoff_id: str
    handoff_type: str
    observation: CallObservation
    live: bool = False
    fixture_root: Path = FIXTURE_ROOT

    def _base_evidence(self, selection) -> dict[str, Any]:
        snapshot = pool_snapshot(pool=self.pool, station=self.station, task=self.task)
        return {
            "worker_selection": selection.to_evidence(),
            "worker_pool_hash": pool_snapshot_hash(snapshot),
            "worker_pool_snapshot": snapshot,
            "retry_count": 0,
            "automatic_retry": False,
            "provider_call_mode": "live" if self.live else "dry_slot",
        }

    def run_node(self, *, handle: NodeWorkspaceHandle) -> WorkerExecutionResult:
        selection = resolve_worker(
            pool=self.pool,
            runners=self.runners,
            station=self.station,
            task=self.task,
            attempt_index=0,
        )
        if selection is None:
            return WorkerExecutionResult(
                node_id=self.node_id,
                workspace_id=handle.workspace_id,
                success=False,
                error_type="WorkerPoolExhausted",
                error_message="no eligible worker",
                execution_evidence={"retry_count": 0, "automatic_retry": False},
            )

        evidence = self._base_evidence(selection)
        evidence["adapter_entered_ns"] = time.perf_counter_ns()
        try:
            node_result = selection.runner.run_node(
                task_id=self.task.id,
                station_id=self.station.id,
                worker_id=selection.profile.id,
                context={
                    "role": "builder",
                    "instructions": handle.context_package.instructions,
                    "context_body": handle.context_package.context_body,
                    "attempt_index": 0,
                },
                workspace_root=handle.root,
                station_role="builder",
            )
        except Exception as exc:  # one attempt only; preserve bounded public failure evidence
            evidence["provider_timing"] = self.observation.snapshot()
            return WorkerExecutionResult(
                node_id=self.node_id,
                workspace_id=handle.workspace_id,
                success=False,
                error_type=type(exc).__name__,
                error_message=_safe_error(exc),
                execution_evidence=evidence,
            )

        tokens = node_result.token_usage
        evidence["provider_timing"] = self.observation.snapshot()
        evidence["provider_ref"] = str(node_result.opaque_provider_ref or "")
        evidence["tokens"] = {
            "input": int(tokens.input or 0) if tokens else 0,
            "output": int(tokens.output or 0) if tokens else 0,
            "cached_input": int(tokens.cached_input or 0) if tokens else 0,
        }
        if node_result.error_message:
            return WorkerExecutionResult(
                node_id=self.node_id,
                workspace_id=handle.workspace_id,
                success=False,
                error_type="ProviderOutputError",
                error_message=node_result.error_message[:500],
                execution_evidence=evidence,
            )

        patches = tuple(node_result.patches or ())
        patch_bytes = sum(len((p.content or "").encode("utf-8")) for p in patches)
        if (
            len(patches) != 1
            or patches[0].rel_path != self.target_path
            or patches[0].action != "write"
            or patch_bytes > MAX_PATCH_BYTES
        ):
            evidence["patch_validation"] = {
                "passed": False,
                "patch_count": len(patches),
                "patch_bytes": patch_bytes,
            }
            return WorkerExecutionResult(
                node_id=self.node_id,
                workspace_id=handle.workspace_id,
                success=False,
                error_type="BoundedPatchContractFailed",
                error_message="provider patch violated exact path/count/size contract",
                execution_evidence=evidence,
            )

        workspace = IsolatedWorkspace(
            root=handle.root,
            fixture_root=self.fixture_root,
            allowed_scope_rel_prefixes=(self.target_path.split("/", 1)[0],),
            baseline_hashes=baseline_file_hashes(self.fixture_root),
        )
        applied, violations = apply_patches(
            workspace, patches, allowed_scope=(self.target_path.split("/", 1)[0],)
        )
        if not applied:
            evidence["patch_validation"] = {
                "passed": False,
                "violations": [v.offending_path or "" for v in violations],
            }
            return WorkerExecutionResult(
                node_id=self.node_id,
                workspace_id=handle.workspace_id,
                success=False,
                error_type="ScopeViolation",
                error_message="patch rejected by scope gate",
                execution_evidence=evidence,
            )

        gates = DeterministicGateEngine(GateSpec(
            public_test_cmd=[
                sys.executable, "-m", "pytest", "-q", "--no-header",
                "-p", "no:cacheprovider", self.public_test_path,
            ],
            hidden_test_cmd=[],
            hidden_test_cwd=None,
            allowed_scope_prefixes=(self.target_path.split("/", 1)[0],),
            baseline_hashes=baseline_file_hashes(self.fixture_root),
        )).run_all(handle.root, target_node_id=self.node_id, target_task_id=self.task.id)
        evidence["patch_validation"] = {
            "passed": True,
            "patch_count": len(patches),
            "patch_bytes": patch_bytes,
        }
        evidence["gates"] = [
            {
                "gate_identity": str(g.gate_identity),
                "status": str(g.status.value),
                "exit_code": (g.metadata or {}).get("exit_code"),
                "changed_files": list((g.metadata or {}).get("changed_files") or ()),
            }
            for g in gates
            if str(g.gate_identity) != "hidden_test_execution"
        ]
        gates_pass = bool(gates) and all(str(g.status.value) == "pass" for g in gates)
        if not gates_pass:
            return WorkerExecutionResult(
                node_id=self.node_id,
                workspace_id=handle.workspace_id,
                success=False,
                error_type="DeterministicGateFailed",
                error_message="scope, syntax, or public verification failed",
                execution_evidence=evidence,
            )

        target = handle.root / self.target_path
        contract = dict(handle.handoff_output_contracts[self.handoff_id])
        descriptor = {
            "handoff_id": self.handoff_id,
            "artifact_id": f"artifact::{self.node_id}::{self.handoff_id}",
            "path": self.target_path,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "kind": "file",
            "size_bytes": target.stat().st_size,
            "producer_node_id": self.node_id,
            "producer_station_id": contract["producer_station_id"],
            "consumer_scope": contract["consumer_scope"],
            "metadata": {"verification": "scope+syntax+public"},
        }
        return WorkerExecutionResult(
            node_id=self.node_id,
            workspace_id=handle.workspace_id,
            success=True,
            produced_handoff_ids=(self.handoff_id,),
            produced_handoff_types_by_id={self.handoff_id: (self.handoff_type,)},
            artifacts=(descriptor,),
            execution_evidence=evidence,
        )


@dataclass
class DeterministicIntegrationNode:
    expected: dict[str, dict[str, str]]

    def run_node(self, *, handle: NodeWorkspaceHandle) -> WorkerExecutionResult:
        started_ns = time.perf_counter_ns()
        handoffs = {str(h["handoff_id"]): h for h in handle.context_package.upstream_handoffs}
        checks: list[str] = []
        try:
            if set(handoffs) != set(self.expected):
                raise ValueError("required governed handoff set mismatch")
            for hid, expected in sorted(self.expected.items()):
                item = handoffs[hid]
                refs = list(item.get("artifact_refs") or ())
                if item.get("producer_node_id") != expected["producer_node_id"]:
                    raise ValueError(f"{hid}: producer mismatch")
                if item.get("public_interfaces") != [expected["handoff_type"]]:
                    raise ValueError(f"{hid}: interface mismatch")
                if len(refs) != 1 or not re.fullmatch(r"[0-9a-f]{64}", str(refs[0].get("sha256") or "")):
                    raise ValueError(f"{hid}: artifact hash invalid")
                if refs[0].get("location") != expected["location"] or int(refs[0].get("size_bytes") or 0) <= 0:
                    raise ValueError(f"{hid}: artifact descriptor invalid")
                checks.append(hid)
        except Exception as exc:
            return WorkerExecutionResult(
                node_id=handle.node_id,
                workspace_id=handle.workspace_id,
                success=False,
                error_type=type(exc).__name__,
                error_message=str(exc)[:500],
                execution_evidence={
                    "integration_started_ns": started_ns,
                    "governed_handoffs_checked": checks,
                    "local_only": True,
                },
            )
        return WorkerExecutionResult(
            node_id=handle.node_id,
            workspace_id=handle.workspace_id,
            success=True,
            execution_evidence={
                "integration_started_ns": started_ns,
                "integration_finished_ns": time.perf_counter_ns(),
                "governed_handoffs_checked": checks,
                "local_only": True,
                "final_verdict": "VERIFIED",
            },
        )


@dataclass
class AcceptanceBundle:
    product: ProductSpec
    dag: ProductDAG
    stations: dict[str, StationContract]
    tasks: dict[str, TaskContract]
    pools: dict[str, WorkerPool]
    workers: dict[str, Any]
    observations: dict[str, CallObservation]


def build_acceptance(*, live: bool) -> AcceptanceBundle:
    if EXPECTED_PROVIDER_CALLS != 2:
        raise RuntimeError("REDI-06C policy requires exactly two planned provider calls")

    product = ProductSpec(
        requested_outcome="Implement two independent tiny pure functions and verify their handoffs.",
        requirements=(
            Requirement(text="alpha uppercases text", kind="functional"),
            Requirement(text="beta reverses text", kind="functional"),
        ),
        constraints=(Requirement(text="One bounded Python patch per live node", kind="constraint"),),
        acceptance_criteria=(AcceptanceCriterion(description="Both narrow public tests pass"),),
        unresolved_requirements=(),
        risk_tier=RiskTier.LOW,
    )
    station_a = StationContract(
        role="builder", required_output_kinds=("patch_edit",), owned_scope=("alpha",),
        allowed_tool_classes=(), applicable_task_classes=("tiny_python_function",),
    )
    station_b = StationContract(
        role="builder", required_output_kinds=("patch_edit",), owned_scope=("beta",),
        allowed_tool_classes=(), applicable_task_classes=("tiny_python_function",),
    )
    station_c = StationContract(
        role="integration", accepted_input_kinds=("alpha.impl.v1", "beta.impl.v1"),
        required_output_kinds=("verified_graph_outcome",), allowed_tool_classes=(),
    )
    task_a = TaskContract(
        goal="Implement alpha(text) by returning text.upper(); edit only alpha/__init__.py.",
        station_id=station_a.id,
        allowed_scope=("alpha",),
        acceptance_criteria=(AcceptanceCriterion(description="tests/test_alpha_public.py passes"),),
        verification_requirements=("scope", "syntax", "public"),
        metadata={"task_class": "tiny_python_function"},
    )
    task_b = TaskContract(
        goal="Implement beta(text) by returning text[::-1]; edit only beta/__init__.py.",
        station_id=station_b.id,
        allowed_scope=("beta",),
        acceptance_criteria=(AcceptanceCriterion(description="tests/test_beta_public.py passes"),),
        verification_requirements=("scope", "syntax", "public"),
        metadata={"task_class": "tiny_python_function"},
    )
    task_c = TaskContract(
        goal="Deterministically validate both governed typed handoff descriptors.",
        station_id=station_c.id,
        allowed_scope=(),
        verification_requirements=("handoff identity", "hash", "type", "consumer scope"),
        metadata={"task_class": "deterministic_integration"},
    )
    dag = ProductDAG(
        id="redi06c-live-parallel-abc",
        product_id=product.id,
        nodes=(
            DagNode(
                node_id="node_a", task_id=str(task_a.id), station_id=str(station_a.id),
                parallel_group="live_pair",
                metadata={
                    "selected_paths": ["alpha/__init__.py", "tests/test_alpha_public.py"],
                    "produces_handoff_ids": ["handoff_alpha"],
                    "produces_handoff_types": ["alpha.impl.v1"],
                },
            ),
            DagNode(
                node_id="node_b", task_id=str(task_b.id), station_id=str(station_b.id),
                parallel_group="live_pair",
                metadata={
                    "selected_paths": ["beta/__init__.py", "tests/test_beta_public.py"],
                    "produces_handoff_ids": ["handoff_beta"],
                    "produces_handoff_types": ["beta.impl.v1"],
                },
            ),
            DagNode(
                node_id="node_c", task_id=str(task_c.id), station_id=str(station_c.id),
                metadata={
                    "requires_handoff_ids": ["handoff_alpha", "handoff_beta"],
                    "requires_handoff_types": ["alpha.impl.v1", "beta.impl.v1"],
                },
            ),
        ),
        dependencies=(
            DagDependency(upstream="node_a", downstream="node_c", kind="hard"),
            DagDependency(upstream="node_b", downstream="node_c", kind="hard"),
        ),
        metadata={"acceptance": "REDI-06C", "max_parallelism": 2},
    )

    barrier = threading.Barrier(2, timeout=30)
    observations = {nid: CallObservation(nid) for nid in ("node_a", "node_b")}
    profiles: dict[str, WorkerProfile] = {}
    pools: dict[str, WorkerPool] = {}
    runners_by_node: dict[str, dict[str, WorkerRunner]] = {}
    for node_id, station, patch in (
        ("node_a", station_a, PatchEdit("alpha/__init__.py", "write", ALPHA_IMPL)),
        ("node_b", station_b, PatchEdit("beta/__init__.py", "write", BETA_IMPL)),
    ):
        wid = WorkerId(f"worker_openai_06c_{node_id[-1]}")
        profile = WorkerProfile(
            id=wid,
            display_name="REDI-06C bounded OpenAI worker",
            version="1",
            capability_tags=("python", "structured_output"),
            structured_output_capable=True,
            station_eligibility=(station.id,),
            availability_status="available",
            provider_opaque_reference=f"openai:{MODEL}",
        )
        profiles[node_id] = profile
        pools[node_id] = WorkerPool(
            station_id=station.id,
            role="builder",
            entries=(PoolEntry(profile=profile, preference_rank=0),),
            selection_policy_id="deterministic_v0",
        )
        if live:
            runner: WorkerRunner = LiveProviderAdapter(
                config=LiveProviderConfig(
                    env_key_name="REDI_OPENAI_API_KEY",
                    model=MODEL,
                    timeout_s=60.0,
                    max_output_tokens=MAX_OUTPUT_TOKENS,
                    temperature=0.0,
                    provider_name="openai",
                ),
                before_call=lambda b=barrier: b.wait(timeout=30),
                call_observer=observations[node_id].record,
            )
        else:
            runner = DryProviderRunner(patch, barrier, observations[node_id])
        runners_by_node[node_id] = {str(wid): runner}

    workers = {
        "node_a": PoolBoundLiveNode(
            "node_a", station_a, task_a, pools["node_a"], runners_by_node["node_a"],
            "alpha/__init__.py", "tests/test_alpha_public.py", "handoff_alpha",
            "alpha.impl.v1", observations["node_a"], live,
        ),
        "node_b": PoolBoundLiveNode(
            "node_b", station_b, task_b, pools["node_b"], runners_by_node["node_b"],
            "beta/__init__.py", "tests/test_beta_public.py", "handoff_beta",
            "beta.impl.v1", observations["node_b"], live,
        ),
        "node_c": DeterministicIntegrationNode({
            "handoff_alpha": {
                "producer_node_id": "node_a", "handoff_type": "alpha.impl.v1",
                "location": "alpha/__init__.py",
            },
            "handoff_beta": {
                "producer_node_id": "node_b", "handoff_type": "beta.impl.v1",
                "location": "beta/__init__.py",
            },
        }),
    }
    return AcceptanceBundle(
        product=product,
        dag=dag,
        stations={"node_a": station_a, "node_b": station_b, "node_c": station_c},
        tasks={"node_a": task_a, "node_b": task_b, "node_c": task_c},
        pools=pools,
        workers=workers,
        observations=observations,
    )


def _as_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _load_bound_acceptance_rows(
    db_path: Path, factory_run_id: str | None = None,
) -> tuple[str, sqlite3.Row, list[dict[str, Any]], list[sqlite3.Row]]:
    """Resolve one acceptance run from canonical FactoryRun identity, read-only."""
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        factory_rows = list(conn.execute("SELECT * FROM factory_runs ORDER BY created_at, id"))
        if factory_run_id is None:
            if len(factory_rows) != 1:
                raise IncompleteEvidenceError(
                    "acceptance audit requires exactly one persisted FactoryRun authority"
                )
            run_row = factory_rows[0]
            factory_run_id = str(run_row["id"])
        else:
            matches = [row for row in factory_rows if str(row["id"]) == str(factory_run_id)]
            if len(matches) != 1:
                raise IncompleteEvidenceError(
                    f"FactoryRun {factory_run_id!r} is missing or ambiguous"
                )
            run_row = matches[0]

        run_obj = domain_from_json(str(run_row["payload_json"]))
        if not isinstance(run_obj, FactoryRun) or str(run_obj.id) != str(factory_run_id):
            raise IncompleteEvidenceError("FactoryRun payload identity mismatch")

        known_run_ids = {str(row["id"]) for row in factory_rows}
        entries = list(conn.execute(
            "SELECT * FROM evidence_entries ORDER BY global_sequence"
        ))
        target_rows: list[dict[str, Any]] = []
        target_entries: list[sqlite3.Row] = []
        for entry in entries:
            payload = domain_from_json(str(entry["payload_json"]))
            metadata = dict(getattr(payload, "metadata", {}) or {})
            if "row_kind" not in metadata:
                continue
            column_run_id = entry["factory_run_id"]
            payload_run_id = getattr(payload, "factory_run_id", None)
            if column_run_id is None or str(column_run_id) not in known_run_ids:
                raise IncompleteEvidenceError("orphan DAG evidence: missing FactoryRun binding")
            if payload_run_id is None or str(payload_run_id) != str(column_run_id):
                raise IncompleteEvidenceError(
                    "DAG evidence payload points to another FactoryRun"
                )
            if str(column_run_id) == str(factory_run_id):
                target_rows.append(metadata)
                target_entries.append(entry)
        if not target_rows:
            raise IncompleteEvidenceError(
                f"FactoryRun {factory_run_id!r} has no bound DAG evidence"
            )
        if _as_utc(str(run_row["created_at"])) > _as_utc(str(target_entries[0]["recorded_at"])):
            raise IncompleteEvidenceError("FactoryRun was not persisted before first DAG evidence")
        snapshot = next((row for row in target_rows if row.get("row_kind") == "dag_snapshot"), None)
        if snapshot is None or str(run_row["dag_id"] or "") != str(snapshot.get("graph_id") or ""):
            raise IncompleteEvidenceError("FactoryRun DAG identity does not match persisted snapshot")
        return str(factory_run_id), run_row, target_rows, entries
    finally:
        conn.close()


def audit_evidence(
    db_path: Path, *, factory_run_id: str | None = None,
) -> dict[str, Any]:
    """Full read-only acceptance audit resolved through persisted FactoryRun."""
    stat_before = db_path.stat()
    before = (hashlib.sha256(db_path.read_bytes()).hexdigest(), stat_before.st_size, stat_before.st_mtime_ns)
    authority_id, run_row, rows, all_entries = _load_bound_acceptance_rows(
        db_path, factory_run_id=factory_run_id,
    )
    replay_1 = replay_dag_run(rows, retained_artifact_root=db_path.parent)
    replay_2 = replay_dag_run(rows, retained_artifact_root=db_path.parent)
    canonical_1 = canonical_replay_view(replay_1)
    canonical_2 = canonical_replay_view(replay_2)
    stat_after = db_path.stat()
    after = (hashlib.sha256(db_path.read_bytes()).hexdigest(), stat_after.st_size, stat_after.st_mtime_ns)
    completed = {r["node_id"]: r for r in rows if r.get("row_kind") == "dag_node_completed"}
    launches = {r["node_id"]: r for r in rows if r.get("row_kind") == "dag_node_launch"}
    timing = {
        nid: (completed.get(nid, {}).get("execution_evidence") or {}).get("provider_timing") or {}
        for nid in ("node_a", "node_b")
    }
    modes = {
        nid: (completed.get(nid, {}).get("execution_evidence") or {}).get("provider_call_mode")
        for nid in ("node_a", "node_b")
    }
    observed_slots = sum(1 for t in timing.values() if "provider_call_started" in t)
    live_calls = sum(
        1 for nid, t in timing.items()
        if modes.get(nid) == "live" and "provider_call_started" in t
    )
    live_run = any(mode == "live" for mode in modes.values())
    overlap = bool(
        timing["node_a"].get("provider_call_started")
        < timing["node_b"].get("provider_call_finished", -1)
        and timing["node_b"].get("provider_call_started")
        < timing["node_a"].get("provider_call_finished", -1)
    )
    seq = {id(row): i for i, row in enumerate(rows)}
    c_after_ab = bool(
        "node_c" in launches
        and "node_a" in completed
        and "node_b" in completed
        and seq[id(launches["node_c"])] > seq[id(completed["node_a"])]
        and seq[id(launches["node_c"])] > seq[id(completed["node_b"])]
    )
    contexts = {
        nid: launches.get(nid, {}).get("context_evidence") or {}
        for nid in ("node_a", "node_b", "node_c")
    }
    gate_pass = all(
        evidence
        and evidence.get("gates")
        and all(g.get("status") == "pass" for g in evidence["gates"])
        for evidence in (
            (completed.get("node_a", {}).get("execution_evidence") or {}),
            (completed.get("node_b", {}).get("execution_evidence") or {}),
        )
    )
    handoffs = [r for r in rows if r.get("row_kind") == "dag_handoff_manifest"]
    final = next((r for r in reversed(rows) if r.get("row_kind") == "dag_final"), {})

    chain_ok = True
    prev_seq = None
    prev_hash = None
    for entry in all_entries:
        cur_seq = int(entry["global_sequence"])
        payload = str(entry["payload_json"])
        stored_hash = str(entry["payload_sha256"])
        cur_prev = entry["prev_hash"]
        chain_ok = chain_ok and stored_hash == payload_hash_from_json(payload)
        chain_ok = chain_ok and verify_chain(prev_seq, prev_hash, cur_seq, stored_hash, cur_prev)
        prev_seq, prev_hash = cur_seq, stored_hash

    graph_completed = final.get("final_states") == {
        "node_a": "COMPLETED", "node_b": "COMPLETED", "node_c": "COMPLETED"
    }
    expected_factory_status = RunStatus.COMPLETED.value if graph_completed else RunStatus.FAILED.value
    factory_terminal_matches = bool(
        run_row["closed_at"] is not None
        and str(run_row["final_status"]) == expected_factory_status
        and bool(int(run_row["verified"])) == bool(graph_completed)
    )
    all_bound = all(
        entry["factory_run_id"] is not None
        and str(entry["factory_run_id"]) == authority_id
        for entry in all_entries
    )

    checks = {
        "factory_run_bound": all_bound,
        "factory_run_terminal_matches": factory_terminal_matches,
        "provider_call_policy": (
            live_calls == EXPECTED_PROVIDER_CALLS
            if live_run
            else live_calls == 0 and observed_slots == EXPECTED_PROVIDER_CALLS
        ),
        "real_call_overlap": overlap,
        "selected_workers": all(
            (completed.get(n, {}).get("execution_evidence") or {}).get("worker_selection")
            for n in ("node_a", "node_b")
        ),
        "contexts_bound_and_distinct": bool(
            contexts["node_a"].get("package_hash")
            and contexts["node_b"].get("package_hash")
            and contexts["node_a"]["package_hash"] != contexts["node_b"]["package_hash"]
        ),
        "tokens_persisted": all(
            "tokens" in (completed.get(n, {}).get("execution_evidence") or {})
            for n in ("node_a", "node_b")
        ),
        "deterministic_gates_pass": gate_pass,
        "two_valid_handoffs": len(handoffs) == 2,
        "retained_artifacts_verified": len(replay_1.get("retained_artifacts") or ()) == 2,
        "c_after_a_and_b_terminal": c_after_ab,
        "final_graph_verified": graph_completed and final.get("graph_verified") is True,
        "evidence_chain_valid": chain_ok,
        "replay_x2_equal": canonical_1 == canonical_2,
        "replay_zero_writes": before == after,
    }
    return {
        "verified": all(checks.values()),
        "checks": checks,
        "factory_run_id": authority_id,
        "acceptance_run_id": (getattr(domain_from_json(str(run_row["payload_json"])), "metadata", {}) or {}).get("acceptance_run_id"),
        "live_run": live_run,
        "provider_calls": live_calls,
        "observed_worker_slots": observed_slots,
        "timing": timing,
        "overlap": overlap,
        "contexts": contexts,
        "final_states": final.get("final_states") or {},
        "retained_artifacts": replay_1.get("retained_artifacts") or [],
        "canonical_replay_equal": canonical_1 == canonical_2,
        "ledger_fingerprint_before": before,
        "ledger_fingerprint_after": after,
    }


def _dry_estimate(bundle: AcceptanceBundle) -> dict[str, Any]:
    """Compile contexts without launching workers or touching the provider."""
    from redi_lab.context_compiler import compile_context

    estimates: dict[str, Any] = {}
    for nid in ("node_a", "node_b"):
        task = bundle.tasks[nid]
        station = bundle.stations[nid]
        node = next(n for n in bundle.dag.nodes if n.node_id == nid)
        package = compile_context(
            strategy_id="selected_v0",
            station_role="builder",
            task=task,
            station=station,
            product=bundle.product,
            workspace_root=FIXTURE_ROOT,
            selected_paths=list(node.metadata["selected_paths"]),
            allowed_scope=list(task.allowed_scope),
            budget_bytes=DEFAULT_BUDGET_BYTES,
        )
        input_estimate = int(package.counts["estimated_tokens"]) + 200
        estimates[nid] = {
            "context_bytes": int(package.counts["bytes"]),
            "estimated_input_tokens": input_estimate,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "upper_bound_tokens": input_estimate + MAX_OUTPUT_TOKENS,
        }
    estimates["planned_provider_calls"] = EXPECTED_PROVIDER_CALLS
    estimates["expected_upper_bound_tokens"] = sum(
        estimates[n]["upper_bound_tokens"] for n in ("node_a", "node_b")
    )
    return estimates


def _aggregate_tokens(result: ParallelDagRunResult) -> TokenUsage:
    totals = {"input": 0, "output": 0, "cached_input": 0}
    for row in result.evidence_rows:
        if row.get("row_kind") != "dag_node_completed":
            continue
        tokens = (row.get("execution_evidence") or {}).get("tokens") or {}
        for key in totals:
            totals[key] += int(tokens.get(key) or 0)
    return TokenUsage(
        input=totals["input"], output=totals["output"],
        cached_input=totals["cached_input"], reasoning=0,
    )


def execute_acceptance(*, live: bool, output_root: Path = DEFAULT_OUTPUT_ROOT) -> tuple[Path, ParallelDagRunResult, dict[str, Any]]:
    bundle = build_acceptance(live=live)
    estimate = _dry_estimate(bundle)
    if estimate["planned_provider_calls"] != 2:
        raise RuntimeError("refusing run: provider-call plan is not exactly two")
    run_id = f"redi06c-{'live' if live else 'dry'}-{uuid.uuid4().hex[:12]}"
    factory_run_id = str(FactoryRunId(new_id("factory_run")))
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    db_path = run_dir / "evidence.sqlite"
    print("REDI-06C PRE-EXECUTION ESTIMATE")
    print(f"acceptance_run_id={run_id}")
    print(f"factory_run_id={factory_run_id}")
    print(f"node_ledger={db_path}")
    print(f"provider_model=openai:{MODEL}")
    print(f"expected_provider_calls={EXPECTED_PROVIDER_CALLS if live else 0}")
    print(f"would_be_live_worker_slots={EXPECTED_PROVIDER_CALLS}")
    for nid in ("node_a", "node_b"):
        item = estimate[nid]
        print(
            f"preflight[{nid}]=context_bytes:{item['context_bytes']} "
            f"estimated_input_tokens:{item['estimated_input_tokens']} "
            f"max_output_tokens:{item['max_output_tokens']}"
        )
    print(f"expected_upper_bound_tokens={estimate['expected_upper_bound_tokens']}")
    conn = sqlite3.connect(db_path)
    bootstrap_schema(conn)
    ledger = EvidenceLedger(conn)
    run_repo = FactoryRunRepository(conn, ledger)
    try:
        run_repo.store(FactoryRun(
            id=FactoryRunId(factory_run_id),
            product_spec_id=bundle.product.id,
            dag_id=str(bundle.dag.id),
            mode=Mode.EXPLORATION,
            final_status=RunStatus.PENDING,
            verified=False,
            metadata={
                "acceptance": "REDI-06C",
                "acceptance_run_id": run_id,
                "provider_call_mode": "live" if live else "dry_slot",
                "pricing_evidence": "not_recorded_no_versioned_schedule",
            },
        ))
        executor = ParallelDagExecutor(
            dag=bundle.dag,
            station_by_node=bundle.stations,
            task_by_node=bundle.tasks,
            node_workers=bundle.workers,
            required_handoff_ids_by_node={"node_c": ("handoff_alpha", "handoff_beta")},
            produced_handoff_ids_by_node={
                "node_a": ("handoff_alpha",), "node_b": ("handoff_beta",),
            },
            produced_handoff_types_by_id={
                "handoff_alpha": ("alpha.impl.v1",), "handoff_beta": ("beta.impl.v1",),
            },
            fixture_root=FIXTURE_ROOT,
            product=bundle.product,
            max_parallelism=2,
            context_budget_bytes=DEFAULT_BUDGET_BYTES,
            ledger=ledger,
            factory_run_id=factory_run_id,
            artifact_root=run_dir,
        )
        try:
            result = executor.run()
        except Exception:
            run_repo.close_run(factory_run_id, RunStatus.FAILED, False)
            raise
        completed = bool(result.final_states) and all(
            state == NodeRunState.COMPLETED for state in result.final_states.values()
        )
        run_repo.close_run(
            factory_run_id,
            RunStatus.COMPLETED if completed else RunStatus.FAILED,
            completed,
            total_tokens=_aggregate_tokens(result),
            timing=Timing(
                wall_clock_seconds=float(result.timing["graph_wall_clock_seconds"]),
                compute_seconds=float(result.timing["aggregate_node_compute_seconds"]),
            ),
        )
    finally:
        conn.close()
    audit = audit_evidence(db_path, factory_run_id=factory_run_id)
    return db_path, result, {
        "estimate": estimate, "audit": audit, "run_id": run_id,
        "factory_run_id": factory_run_id,
    }


def _print_run(db_path: Path, result: ParallelDagRunResult, report: dict[str, Any], *, live: bool) -> None:
    print("=" * 72)
    print("REDI-06C LIVE PROVIDER PARALLEL ACCEPTANCE")
    print("=" * 72)
    print(f"mode={'LIVE' if live else 'DRY-RUN (ZERO NETWORK)'}")
    print(f"acceptance_run_id={report['run_id']}")
    print(f"factory_run_id={report['factory_run_id']}")
    print(f"node_ledger={db_path}")
    print(f"provider_model=openai:{MODEL}")
    print(f"planned_provider_calls={EXPECTED_PROVIDER_CALLS}")
    for nid in ("node_a", "node_b"):
        est = report["estimate"][nid]
        print(
            f"estimate[{nid}]=context_bytes:{est['context_bytes']} "
            f"input_tokens:{est['estimated_input_tokens']} "
            f"max_output_tokens:{est['max_output_tokens']} "
            f"upper_bound:{est['upper_bound_tokens']}"
        )
    print(f"expected_upper_bound_tokens={report['estimate']['expected_upper_bound_tokens']}")
    audit = report["audit"]
    print(f"actual_live_provider_calls={audit['provider_calls']}")
    print(f"observed_worker_slots={audit['observed_worker_slots']}")
    for nid in ("node_a", "node_b"):
        timing = audit["timing"][nid]
        context = audit["contexts"][nid]
        execution = next((
            r.get("execution_evidence") or {}
            for r in result.evidence_rows
            if r.get("row_kind") in {"dag_node_completed", "dag_node_failed"}
            and r.get("node_id") == nid
        ), {})
        selection = execution.get("worker_selection") or {}
        tokens = execution.get("tokens") or {}
        print(
            f"worker[{nid}]={selection.get('worker_id')} policy:{selection.get('policy_id')} "
            f"tokens_input:{tokens.get('input')} tokens_output:{tokens.get('output')}"
        )
        print(
            f"timing[{nid}]={timing.get('provider_call_started')}.."
            f"{timing.get('provider_call_finished')}"
        )
        print(
            f"context[{nid}]=package:{context.get('package_hash')} "
            f"rendered:{context.get('rendered_hash')} "
            f"bytes:{(context.get('counts') or {}).get('bytes')} "
            f"estimated_tokens:{(context.get('counts') or {}).get('estimated_tokens')}"
        )
    print(f"measured_provider_overlap={audit['overlap']}")
    print(f"workspace_ids={json.dumps(result.workspace_ids, sort_keys=True)}")
    for hid, manifest in sorted(result.handoff_manifests.items()):
        artifact = manifest.produced_artifacts[0]
        print(f"handoff[{hid}]=artifact:{artifact.ref_id} sha256:{artifact.sha256}")
    print(f"final_graph_status={audit['final_states']}")
    print(f"audit_verified={audit['verified']}")
    print("audit_checks=" + json.dumps(audit["checks"], sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="use deterministic fake workers; zero network")
    parser.add_argument("--audit", type=Path, help="read-only audit of an existing evidence.sqlite")
    args = parser.parse_args(argv)
    if args.audit is not None:
        report = audit_evidence(args.audit)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["verified"] else 1
    if not args.dry_run:
        if os.environ.get("REDI_LIVE_PROVIDER") != "1":
            print("REDI-06C LIVE BLOCKED: REDI_LIVE_PROVIDER=1 is required; no provider call made.")
            return 2
        if not os.environ.get("REDI_OPENAI_API_KEY"):
            print("REDI-06C LIVE BLOCKED: REDI_OPENAI_API_KEY is required; no provider call made.")
            return 2
    live = not args.dry_run
    db_path, result, report = execute_acceptance(live=live)
    _print_run(db_path, result, report, live=live)
    return 0 if report["audit"]["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
