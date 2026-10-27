"""REDI-06B — isolated parallel DAG execution foundation.

Bounded concurrent execution of independent READY nodes inside ONE local
orchestrator process that owns ONE FactoryRun.  Workers execute in isolated
workspaces; a single coordinator owns all scheduler state transitions and
all canonical evidence writes.

Architecture invariants
-----------------------
* workers execute work; the COORDINATOR alone commits state transitions and
  ledger evidence (workers never touch SQLite or scheduler state);
* max_parallelism bounds in-flight nodes (no unbounded thread creation);
* every node receives only its own governed workspace capability, copied from
  the same immutable baseline; output declarations are rejected when they
  escape that workspace boundary;
* each node compiles its own REDI-05 context package (firewall, strategy,
  budget enforced per node);
* completed typed handoffs cross node boundaries only as governed
  HandoffManifest descriptors (producer/station/type/artifact hash/scope
  validated);
* launch order is deterministic (node_id); actual completion order is
  recorded and may vary;
* worker exceptions become typed node failures; the coordinator never dies
  from worker code;
* aggregate node compute seconds and graph wall-clock seconds are recorded
  as SEPARATE metrics.

Crash/interruption rule (06B, bounded): a node persisted RUNNING without a
terminal outcome after process interruption is marked FAILED with reason
"interrupted" — it is never silently treated as COMPLETED.  Retry remains a
separate explicit policy decision.  No distributed leases or cross-process
locking: REDI-06B supports exactly one local orchestrator process owning
one FactoryRun.

REDI-06B provides orchestrator/capability workspace isolation, not an OS
security sandbox. Worker code runs in-process and is not contained by a
kernel filesystem sandbox.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .context_compiler import (
    CONTEXT_CLASS_UPSTREAM_HANDOFFS,
    DEFAULT_ALLOWED_CLASSES,
    ContextFirewall,
    compile_context,
)
from .dag_scheduler import (
    validate_handoff_offers,
    DagValidationError,
    NodeRunState,
    assert_transition,
    compute_ready_nodes,
    dag_run_evidence_rows,
    dag_snapshot,
    dag_timing_summary,
    product_dag_hash,
    validate_product_dag_for_scheduling,
)
from .domain import (
    ArtifactRef,
    FactoryRunId,
    FailureRecord,
    GateResult,
    HandoffManifest,
    ProductDAG,
    ProductSpec,
    RunEvidence,
    StationContract,
    TaskContract,
)
from .evidence.artifacts import (
    MAX_RETAINED_ARTIFACT_BYTES,
    retain_validated_artifact,
)
from .factory.isolation import IsolatedWorkspace, ScopeViolation, assert_in_workspace
from .persistence import EvidenceLedger


class DuplicateNodeResultError(Exception):
    """The coordinator received more than one result for the same node."""


class UnknownNodeResultError(Exception):
    """The coordinator received a result for a node it never launched."""


MAX_HANDOFF_ARTIFACT_BYTES = MAX_RETAINED_ARTIFACT_BYTES
MAX_HANDOFF_METADATA_BYTES = 4096
_HANDOFF_DESCRIPTOR_KEYS = frozenset({
    "handoff_id",
    "artifact_id",
    "path",
    "sha256",
    "kind",
    "size_bytes",
    "producer_node_id",
    "producer_station_id",
    "consumer_scope",
    "metadata",
})
_HANDOFF_REQUIRED_KEYS = _HANDOFF_DESCRIPTOR_KEYS - {"metadata"}
_FORBIDDEN_HANDOFF_TERMS = (
    "reasoning",
    "chain_of_thought",
    "chain-of-thought",
    "hidden_test",
    "hidden-test",
    "hidden test",
)


def _reject_forbidden_handoff_material(value: Any, *, path: str = "descriptor") -> None:
    """Fail closed on raw reasoning or hidden-test material in public handoffs."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key).lower()
            if any(term in key_text for term in _FORBIDDEN_HANDOFF_TERMS):
                raise DagValidationError(f"forbidden handoff field at {path}.{key}")
            _reject_forbidden_handoff_material(item, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_forbidden_handoff_material(item, path=f"{path}[{index}]")
        return
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, str):
        lowered = value.lower()
        if any(term in lowered for term in _FORBIDDEN_HANDOFF_TERMS):
            raise DagValidationError(f"forbidden handoff material at {path}")
        return
    raise DagValidationError(
        f"handoff metadata must contain JSON primitives, got {type(value).__name__} at {path}"
    )


def recover_orphaned_running(
    states: Mapping[str, str],
) -> dict[str, dict[str, str]]:
    """06B restart rule: RUNNING without a terminal outcome after process
    interruption → FAILED with reason "interrupted" (never silently
    COMPLETED).  Returns only the corrected entries."""
    corrected: dict[str, dict[str, str]] = {}
    for node_id, state in states.items():
        if str(state) == NodeRunState.RUNNING.value:
            corrected[str(node_id)] = {
                "state": NodeRunState.FAILED.value,
                "reason": "interrupted",
            }
    return corrected


@dataclass(frozen=True)
class NodeWorkspaceHandle:
    """Governed capability containing only this node's root and identity.

    This is an orchestrator boundary, not an OS filesystem sandbox.
    """

    node_id: str
    workspace_id: str
    root: Path
    context_package: Any
    handoff_output_contracts: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    factory_run_id: str | None = None
    upstream_handoffs: tuple[HandoffManifest, ...] = ()
    retained_artifact_root: Path | None = None


@dataclass
class WorkerExecutionResult:
    """Structured result returned by a worker boundary (never raw worker
    reasoning).  Exceptions are captured by the executor as typed failures."""

    node_id: str
    workspace_id: str
    success: bool
    compute_seconds: float = 0.0
    produced_handoff_ids: tuple[str, ...] = ()
    produced_handoff_types_by_id: Mapping[str, Sequence[str]] = field(default_factory=dict)
    artifacts: tuple[dict[str, Any], ...] = ()  # small identity entries only
    execution_evidence: Mapping[str, Any] = field(default_factory=dict)
    attempt_evidence: tuple[RunEvidence, ...] = ()
    gate_results: tuple[GateResult, ...] = ()
    failure_records: tuple[FailureRecord, ...] = ()
    error_type: str | None = None
    error_message: str | None = None


@dataclass
class ParallelDagRunResult:
    final_states: dict[str, NodeRunState]
    launch_order: tuple[str, ...]
    completion_order: tuple[str, ...]
    transitions: dict[str, tuple[str, ...]]
    context_packages: dict[str, dict[str, Any]]
    handoff_manifests: dict[str, HandoffManifest]
    workspace_ids: dict[str, str]
    evidence_rows: tuple[dict[str, Any], ...]
    timing: dict[str, float]
    graph_hash: str
    graph_revision: int


class ParallelDagExecutor:
    """Bounded concurrent executor for a ProductDAG (single coordinator).

    One local orchestrator process owns one FactoryRun.  Worker threads
    execute node work inside isolated workspaces; the coordinator thread
    alone performs compute_ready_nodes, state transitions, HandoffManifest
    construction/validation and every EvidenceLedger write.
    """

    def __init__(
        self,
        dag: ProductDAG,
        *,
        station_by_node: Mapping[str, StationContract],
        task_by_node: Mapping[str, TaskContract],
        node_workers: Mapping[str, Callable[[NodeWorkspaceHandle], WorkerExecutionResult]],
        required_handoff_ids_by_node: Mapping[str, Sequence[str]] | None = None,
        produced_handoff_ids_by_node: Mapping[str, Sequence[str]] | None = None,
        produced_handoff_types_by_id: Mapping[str, Sequence[str]] | None = None,
        consumer_scope_by_handoff_id: Mapping[str, Sequence[str]] | None = None,
        fixture_root: Path | str,
        product: ProductSpec,
        max_parallelism: int = 2,
        context_budget_bytes: int | None = None,
        ledger: EvidenceLedger | None = None,
        factory_run_id: str | None = None,
        artifact_root: Path | str | None = None,
        workspace_allowed_scope_prefixes: Sequence[str] = ("slugify", "tests"),
    ):
        validate_product_dag_for_scheduling(dag)  # fail closed before any worker
        self.dag = dag
        self.graph_hash = product_dag_hash(dag)
        self.station_by_node = dict(station_by_node)
        self.task_by_node = dict(task_by_node)
        self.node_workers = dict(node_workers)
        self.required_handoff_ids_by_node = {
            str(k): [str(x) for x in (v or ())] for k, v in (required_handoff_ids_by_node or {}).items()
        }
        self.produced_handoff_ids_by_node = {
            str(k): [str(x) for x in (v or ())] for k, v in (produced_handoff_ids_by_node or {}).items()
        }
        self.produced_handoff_types_by_id = {
            str(k): tuple(str(t) for t in (v or ()))
            for k, v in (produced_handoff_types_by_id or {}).items()
        }
        self.consumer_scope_by_handoff_id = {
            str(k): tuple(sorted({str(station_id) for station_id in (v or ())}))
            for k, v in (consumer_scope_by_handoff_id or {}).items()
        }
        self.fixture_root = Path(fixture_root)
        self.product = product
        if max_parallelism < 1:
            raise ValueError("max_parallelism must be >= 1")
        self.max_parallelism = int(max_parallelism)
        self.context_budget_bytes = context_budget_bytes
        self.context_strategy = "selected_v0"
        self._per_node_compute: dict[str, float] = {}
        self.ledger = ledger  # canonical evidence writer (coordinator-owned)
        self.factory_run_id = str(factory_run_id) if factory_run_id is not None else None
        self.artifact_root = Path(artifact_root) if artifact_root is not None else None
        self.workspace_allowed_scope_prefixes = tuple(
            str(prefix) for prefix in workspace_allowed_scope_prefixes
        )
        self._states: dict[str, NodeRunState] = {}
        self._launch_order: list[str] = []
        self._completion_order: list[str] = []
        self._transitions: dict[str, list[str]] = {}
        self._context_packages: dict[str, dict[str, Any]] = {}
        self._handoff_manifests: dict[str, HandoffManifest] = {}
        self._workspace_ids: dict[str, str] = {}
        self._workspace_roots: dict[str, Path] = {}
        self._evidence_rows: list[dict[str, Any]] = []
        self._available_handoff_types: dict[str, tuple[str, ...]] = {}
        self._commit_lock = threading.Lock()

    # -- coordinator-owned helpers ------------------------------------------

    def _transition(self, node_id: str, new: NodeRunState) -> None:
        cur = self._states.get(node_id, NodeRunState.PENDING)
        assert_transition(cur, new)
        self._states[node_id] = new
        self._transitions.setdefault(node_id, []).append(new.value)
        self._append_evidence_row({
            "row_kind": "dag_node_transition",
            "graph_id": str(self.dag.id),
            "graph_revision": int(self.dag.revision),
            "graph_hash": self.graph_hash,
            "node_id": node_id,
            "from_state": cur.value,
            "to_state": new.value,
        })

    def _append_evidence_row(self, row: dict[str, Any]) -> None:
        """Single evidence writer: coordinator thread only, serially."""
        with self._commit_lock:
            self._evidence_rows.append(row)
            if self.ledger is not None:
                from .domain import RunEvidence

                self.ledger.append(
                    RunEvidence(
                        factory_run_id=(
                            FactoryRunId(self.factory_run_id)
                            if self.factory_run_id is not None else None
                        ),
                        node_id="DAG_" + row["row_kind"].upper(),
                        task_class="dag_orchestration",
                        metadata=row,
                    ),
                    factory_run_id=self.factory_run_id,
                )

    def _build_handoff_manifest(
        self, *, node_id: str, hid: str, result: WorkerExecutionResult,
    ) -> HandoffManifest:
        """Validate and bind one worker descriptor to the canonical manifest."""
        station = self.station_by_node.get(node_id)
        if station is None:
            raise DagValidationError(f"node {node_id!r} has no bound producer station")
        declared_types = tuple(self.produced_handoff_types_by_id.get(hid, ()))
        offered_types = tuple(str(t) for t in result.produced_handoff_types_by_id.get(hid, ()))
        if not offered_types:
            raise DagValidationError(
                f"node {node_id!r} produced handoff {hid!r} without an interface/type"
            )
        if declared_types and set(offered_types) != set(declared_types):
            raise DagValidationError(
                f"node {node_id!r} produced handoff {hid!r} with types "
                f"{sorted(offered_types)} that contradict the declared "
                f"{sorted(declared_types)}"
            )
        artifact = self._validated_artifact_ref(node_id=node_id, hid=hid, result=result)
        return HandoffManifest(
            id=hid,
            producer_station_id=station.id,
            consumer_scope=self._consumer_scope_for(hid),
            node_id=node_id,
            task_id=(self.task_by_node[node_id].id if node_id in self.task_by_node else None),
            produced_artifacts=[artifact],
            public_interfaces=list(offered_types),
            metadata={
                "handoff_id": hid,
                "artifact_identity_sha256": artifact.sha256,
                "artifact_id": artifact.ref_id,
                "producing_node": node_id,
                "workspace_id": result.workspace_id,
                "artifact_size_bytes": artifact.metadata["size_bytes"],
            },
        )

    def _consumer_station_ids_for(self, hid: str) -> tuple[str, ...]:
        if hid in self.consumer_scope_by_handoff_id:
            return self.consumer_scope_by_handoff_id[hid]
        consumers: set[str] = set()
        for node_id, required in self.required_handoff_ids_by_node.items():
            if hid not in required:
                continue
            station = self.station_by_node.get(node_id)
            if station is None:
                raise DagValidationError(
                    f"handoff {hid!r} consumer node {node_id!r} has no bound station"
                )
            consumers.add(str(station.id))
        return tuple(sorted(consumers))

    def _consumer_scope_for(self, hid: str) -> str:
        """Canonical bounded scope: sorted comma-separated station identities."""
        return ",".join(self._consumer_station_ids_for(hid))

    @staticmethod
    def _scope_members(scope: str | None) -> set[str]:
        return {part for part in str(scope or "").split(",") if part}

    def _handoffs_for_node(self, node_id: str) -> tuple[HandoffManifest, ...]:
        required = self.required_handoff_ids_by_node.get(node_id, ())
        if not required:
            return ()
        station = self.station_by_node.get(node_id)
        if station is None:
            raise DagValidationError(f"consumer node {node_id!r} has no bound station")
        station_id = str(station.id)
        manifests: list[HandoffManifest] = []
        for hid in required:
            manifest = self._handoff_manifests.get(hid)
            if manifest is None:
                raise DagValidationError(
                    f"consumer node {node_id!r} launched without handoff {hid!r}"
                )
            if station_id not in self._scope_members(manifest.consumer_scope):
                raise DagValidationError(
                    f"consumer station {station_id!r} is outside consumer_scope "
                    f"for handoff {hid!r}"
                )
            manifests.append(manifest)
        return tuple(manifests)

    def _validated_artifact_ref(
        self, *, node_id: str, hid: str, result: WorkerExecutionResult,
    ) -> ArtifactRef:
        matches = [
            artifact for artifact in (result.artifacts or ())
            if isinstance(artifact, Mapping) and str(artifact.get("handoff_id", "")) == hid
        ]
        if len(matches) != 1:
            raise DagValidationError(
                f"handoff {hid!r} must declare exactly one artifact descriptor; got {len(matches)}"
            )
        descriptor = matches[0]
        unknown = set(descriptor) - _HANDOFF_DESCRIPTOR_KEYS
        missing = _HANDOFF_REQUIRED_KEYS - set(descriptor)
        if unknown or missing:
            raise DagValidationError(
                f"handoff {hid!r} artifact descriptor fields invalid: "
                f"missing={sorted(missing)} unknown={sorted(unknown)}"
            )
        _reject_forbidden_handoff_material(descriptor)
        try:
            descriptor_bytes = json.dumps(
                descriptor, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise DagValidationError(f"handoff {hid!r} descriptor is not JSON-safe") from exc
        if len(descriptor_bytes) > MAX_HANDOFF_METADATA_BYTES * 2:
            raise DagValidationError(f"handoff {hid!r} artifact descriptor is too large")

        expected_station = str(self.station_by_node[node_id].id)
        expected_scope = self._consumer_scope_for(hid)
        exact_fields = {
            "handoff_id": hid,
            "producer_node_id": node_id,
            "producer_station_id": expected_station,
            "consumer_scope": expected_scope,
        }
        for field_name, expected in exact_fields.items():
            if str(descriptor[field_name]) != expected:
                raise DagValidationError(
                    f"handoff {hid!r} {field_name} mismatch: "
                    f"{descriptor[field_name]!r} != {expected!r}"
                )

        artifact_id = str(descriptor["artifact_id"])
        rel_path = str(descriptor["path"])
        kind = str(descriptor["kind"])
        if not artifact_id.strip():
            raise DagValidationError(f"handoff {hid!r} artifact_id must be non-empty")
        if kind not in {"file", "patch", "directory", "artifact", "trace", "report", "other"}:
            raise DagValidationError(f"handoff {hid!r} has unsupported artifact kind {kind!r}")
        path_violation = ContextFirewall().check_file(rel_path)
        if path_violation is not None:
            raise DagValidationError(
                f"handoff {hid!r} artifact path rejected: {path_violation}"
            )
        workspace_root = self._workspace_roots.get(node_id)
        if workspace_root is None:
            raise DagValidationError(f"node {node_id!r} has no active workspace boundary")
        try:
            artifact_path = assert_in_workspace(workspace_root, rel_path)
        except ScopeViolation as exc:
            raise DagValidationError(
                f"handoff {hid!r} artifact path escapes node workspace"
            ) from exc
        if not artifact_path.is_file():
            raise DagValidationError(
                f"handoff {hid!r} artifact path is not a file: {rel_path!r}"
            )
        actual_size = artifact_path.stat().st_size
        try:
            declared_size = int(descriptor["size_bytes"])
        except (TypeError, ValueError) as exc:
            raise DagValidationError(f"handoff {hid!r} size_bytes must be an integer") from exc
        if declared_size != actual_size:
            raise DagValidationError(
                f"handoff {hid!r} artifact size mismatch: {declared_size} != {actual_size}"
            )
        if actual_size > MAX_HANDOFF_ARTIFACT_BYTES:
            raise DagValidationError(
                f"handoff {hid!r} artifact exceeds {MAX_HANDOFF_ARTIFACT_BYTES} bytes"
            )
        actual_hash = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        declared_hash = str(descriptor["sha256"]).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", declared_hash) or declared_hash != actual_hash:
            raise DagValidationError(
                f"handoff {hid!r} artifact hash mismatch for {artifact_id!r}"
            )
        retained_path: str | None = None
        if self.artifact_root is not None:
            retained_path = retain_validated_artifact(
                run_artifact_root=self.artifact_root,
                source_path=artifact_path,
                artifact_id=artifact_id,
                expected_sha256=actual_hash,
                expected_size=actual_size,
                maximum_bytes=MAX_HANDOFF_ARTIFACT_BYTES,
            )
        metadata = descriptor.get("metadata") or {}
        if not isinstance(metadata, Mapping):
            raise DagValidationError(f"handoff {hid!r} metadata must be a mapping")
        metadata_bytes = json.dumps(
            metadata, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(metadata_bytes) > MAX_HANDOFF_METADATA_BYTES:
            raise DagValidationError(
                f"handoff {hid!r} metadata exceeds {MAX_HANDOFF_METADATA_BYTES} bytes"
            )
        return ArtifactRef(
            ref_id=artifact_id,
            kind=kind,
            location=rel_path,
            sha256=actual_hash,
            source="handoff",
            metadata={
                "size_bytes": actual_size,
                "handoff_id": hid,
                "producer_node_id": node_id,
                "producer_station_id": expected_station,
                "consumer_scope": expected_scope,
                "descriptor_metadata": dict(metadata),
                **({"retained_artifact_path": retained_path} if retained_path else {}),
            },
        )

    def _validate_result(self, node_id: str, result: WorkerExecutionResult) -> None:
        # the COMMIT identity (the coordinator's in-flight key) is authoritative
        if node_id not in {str(n.node_id) for n in self.dag.nodes}:
            raise UnknownNodeResultError(f"result for unknown node {node_id!r}")
        if str(result.node_id) != node_id:
            raise DagValidationError(
                f"result identity mismatch: result names {result.node_id!r} "
                f"but the committed node is {node_id!r}"
            )
        expected_workspace_id = self._workspace_ids.get(node_id)
        if expected_workspace_id is None or str(result.workspace_id) != expected_workspace_id:
            raise DagValidationError(
                f"result workspace mismatch for {node_id!r}: "
                f"{result.workspace_id!r} != {expected_workspace_id!r}"
            )
        if self._states.get(node_id) != NodeRunState.RUNNING:
            raise DuplicateNodeResultError(
                f"duplicate or unsolicited result for {node_id!r} "
                f"(state {self._states.get(node_id)})"
            )
        if result.produced_handoff_ids:
            validate_handoff_offers(dict(result.produced_handoff_types_by_id))
        delivered = [str(h) for h in (result.produced_handoff_ids or ())]
        if len(delivered) != len(set(delivered)):
            raise DagValidationError(f"duplicate conflicting handoff id from node {node_id!r}")
        descriptor_ids = [
            str(a.get("handoff_id", "")) if isinstance(a, Mapping) else ""
            for a in (result.artifacts or ())
        ]
        if sorted(descriptor_ids) != sorted(delivered):
            raise DagValidationError(
                f"artifact descriptors do not match delivered handoffs for node {node_id!r}"
            )
        evidence = result.execution_evidence or {}
        if not isinstance(evidence, Mapping):
            raise DagValidationError("execution_evidence must be a mapping")
        _reject_forbidden_handoff_material(evidence, path="execution_evidence")
        try:
            evidence_bytes = json.dumps(
                evidence, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise DagValidationError("execution_evidence must be JSON-safe") from exc
        if len(evidence_bytes) > 32 * 1024:
            raise DagValidationError("execution_evidence exceeds 32768 bytes")
        if not all(isinstance(item, RunEvidence) for item in result.attempt_evidence):
            raise DagValidationError("attempt_evidence must contain RunEvidence records")
        if not all(isinstance(item, GateResult) for item in result.gate_results):
            raise DagValidationError("gate_results must contain GateResult records")
        if not all(isinstance(item, FailureRecord) for item in result.failure_records):
            raise DagValidationError("failure_records must contain FailureRecord records")

    # -- main loop -----------------------------------------------------------

    def run(self) -> ParallelDagRunResult:
        """Execute the whole graph with bounded parallelism, then clean up."""
        graph_wall_start = time.perf_counter()
        pool = ThreadPoolExecutor(max_workers=self.max_parallelism,
                                  thread_name_prefix="redi-dag-worker")
        workspaces: list[IsolatedWorkspace] = []
        in_flight: dict[Future, str] = {}
        try:
            # one canonical graph snapshot row — the replay anchor
            self._append_evidence_row({
                "row_kind": "dag_snapshot",
                "graph_id": str(self.dag.id),
                "graph_revision": int(self.dag.revision),
                "graph_hash": self.graph_hash,
                "dag_snapshot": dag_snapshot(self.dag),
            })
            while True:
                decision = compute_ready_nodes(
                    self.dag, self._states,
                    available_handoff_ids=set(self._available_handoff_types),
                    available_handoff_types=self._available_handoff_types,
                )
                self._append_evidence_row({
                    "row_kind": "dag_ready_decision",
                    "graph_id": str(self.dag.id),
                    "graph_revision": int(self.dag.revision),
                    "graph_hash": self.graph_hash,
                    "node_states": {k: v.value for k, v in sorted(self._states.items())},
                    "ready_nodes": list(decision.ready),
                    "blocked_nodes": list(decision.blocked),
                    "pending_nodes": list(decision.pending),
                })

                for node_id in decision.blocked:
                    if self._states.get(node_id) != NodeRunState.BLOCKED:
                        self._transition(node_id, NodeRunState.BLOCKED)
                        self._append_evidence_row({
                            "row_kind": "dag_node_blocked",
                            "graph_id": str(self.dag.id),
                            "graph_revision": int(self.dag.revision),
                            "graph_hash": self.graph_hash,
                            "node_id": node_id,
                        })

                # bounded launch of independent READY nodes (deterministic order)
                slots = self.max_parallelism - len(in_flight)
                launchable = [n for n in decision.ready
                              if n not in in_flight][:max(0, slots)]
                for node_id in launchable:
                    self._launch(node_id, pool, workspaces, in_flight)

                if not in_flight:
                    if not decision.ready and not decision.blocked:
                        break  # quiescent: nothing ready, running or newly blocked
                    # blocked nodes were just recorded; recompute once more
                    if not launchable:
                        continue

                # coordinator blocks on the FIRST completion; workers keep running
                done, _ = wait(list(in_flight), return_when=FIRST_COMPLETED)
                for future in done:
                    node_id = in_flight.pop(future)
                    result: WorkerExecutionResult = future.result()  # typed failure already captured
                    # SERIAL coordinator commit of one node result
                    self._commit_result(node_id, result)

                if all(
                    self._states.get(str(n.node_id)) in
                    (NodeRunState.COMPLETED, NodeRunState.FAILED,
                     NodeRunState.BLOCKED, NodeRunState.SKIPPED)
                    for n in self.dag.nodes
                ) and not in_flight:
                    break
        finally:
            pool.shutdown(wait=True)  # no background tasks survive the run
            for ws in workspaces:
                ws.destroy()  # explicit cleanup

        graph_wall = time.perf_counter() - graph_wall_start
        timing = dag_timing_summary(self._per_node_compute, graph_wall)
        for row in self._finalize_evidence_rows(timing):
            self._append_evidence_row(row)
        return ParallelDagRunResult(
            final_states=dict(self._states),
            launch_order=tuple(self._launch_order),
            completion_order=tuple(self._completion_order),
            transitions={k: tuple(v) for k, v in self._transitions.items()},
            context_packages=dict(self._context_packages),
            handoff_manifests=dict(self._handoff_manifests),
            workspace_ids=dict(self._workspace_ids),
            evidence_rows=tuple(self._evidence_rows),
            timing=timing,
            graph_hash=self.graph_hash,
            graph_revision=int(self.dag.revision),
        )

    # -- per-node launch / commit --------------------------------------------

    def _launch(self, node_id: str, pool: ThreadPoolExecutor,
                workspaces: list[IsolatedWorkspace],
                in_flight: dict[Future, str]) -> None:
        station = self.station_by_node.get(node_id)
        task = self.task_by_node.get(node_id)
        # per-node isolated workspace from the SAME immutable baseline
        ws = IsolatedWorkspace.create(fixture_root=self.fixture_root,
                                      allowed_scope_prefixes=self.workspace_allowed_scope_prefixes,
                                      dir=self.fixture_root.parent)
        workspaces.append(ws)
        workspace_id = f"ws::{self.dag.id}::{self.dag.revision}::{node_id}"
        self._workspace_ids[node_id] = workspace_id
        self._workspace_roots[node_id] = ws.root

        # per-node REDI-05 context package (firewall/strategy/budget per node)
        product = self._product_for_node(node_id, task)
        raw_role = str(station.role) if station else "builder"
        # compile_context pranon vetëm "builder"/"reviewer"
        compile_role = "reviewer" if ("review" in raw_role or "integration" in raw_role) else "builder"
        upstream_handoffs = self._handoffs_for_node(node_id)
        firewall = None
        if upstream_handoffs:
            firewall = ContextFirewall(allowed_classes=frozenset(
                set(DEFAULT_ALLOWED_CLASSES) | {CONTEXT_CLASS_UPSTREAM_HANDOFFS}
            ))
        context_package = compile_context(
            strategy_id=self.context_strategy,
            station_role=compile_role,
            task=task, station=station, product=product,
            workspace_root=ws.root,
            selected_paths=self._selected_paths_for(node_id),
            allowed_scope=[str(s) for s in (task.allowed_scope or ())] if task else [],
            handoff_manifests=upstream_handoffs,
            firewall=firewall,
            budget_bytes=self.context_budget_bytes or 64 * 1024,
        )
        self._context_packages[node_id] = context_package.to_evidence()

        handle = NodeWorkspaceHandle(
            node_id=node_id, workspace_id=workspace_id,
            root=ws.root, context_package=context_package,
            handoff_output_contracts={
                hid: {
                    "handoff_id": hid,
                    "producer_node_id": node_id,
                    "producer_station_id": str(station.id),
                    "consumer_scope": self._consumer_scope_for(hid),
                    "handoff_types": list(self.produced_handoff_types_by_id.get(hid, ())),
                }
                for hid in self.produced_handoff_ids_by_node.get(node_id, ())
            },
            factory_run_id=self.factory_run_id,
            upstream_handoffs=tuple(upstream_handoffs),
            retained_artifact_root=self.artifact_root,
        )
        worker = self.node_workers.get(node_id)
        if worker is None:
            raise DagValidationError(f"no worker bound for node {node_id!r}")

        self._transition(node_id, NodeRunState.READY)
        self._transition(node_id, NodeRunState.RUNNING)
        self._launch_order.append(node_id)
        self._append_evidence_row({
            "row_kind": "dag_node_launch",
            "graph_id": str(self.dag.id),
            "graph_revision": int(self.dag.revision),
            "graph_hash": self.graph_hash,
            "node_id": node_id,
            "workspace_id": workspace_id,
            "context_package_hash": context_package.package_hash,
            "rendered_hash": context_package.rendered_hash,
            "context_evidence": context_package.to_evidence(),
        })

        def _boundary(handle: NodeWorkspaceHandle = handle,
                      worker: Callable[[NodeWorkspaceHandle], WorkerExecutionResult] = worker) -> WorkerExecutionResult:
            start = time.perf_counter()
            try:
                result = worker.run_node(handle=handle)
                result.compute_seconds = round(time.perf_counter() - start, 6)
                return result
            except Exception as exc:  # typed node failure — coordinator never dies
                return WorkerExecutionResult(
                    node_id=handle.node_id,
                    workspace_id=handle.workspace_id,
                    success=False,
                    compute_seconds=round(time.perf_counter() - start, 6),
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:500],
                )

        future = pool.submit(_boundary)
        in_flight[future] = node_id

    def _commit_result(self, node_id: str, result: WorkerExecutionResult) -> None:
        """SERIAL coordinator commit: validate, transition, handoffs, evidence."""
        self._validate_result(node_id, result)  # duplicate/unknown → fail closed
        delivered = [str(h) for h in (result.produced_handoff_ids or ())]
        declared_produced = [str(h) for h in self.produced_handoff_ids_by_node.get(node_id, ())]
        stray = [h for h in delivered if h not in declared_produced]
        if stray:
            raise DagValidationError(
                f"node {node_id!r} delivered handoff(s) {sorted(stray)} it does "
                f"not declare as producer (declared: {sorted(declared_produced)})"
            )
        if not result.success and delivered:
            raise DagValidationError(
                f"failed node {node_id!r} cannot deliver handoff(s) {sorted(delivered)}"
            )
        # validate + build ALL manifests BEFORE any state transition (fail
        # closed without leaving half-committed state behind)
        built = [(hid, self._build_handoff_manifest(node_id=node_id, hid=hid, result=result))
                 for hid in delivered]
        duplicate = [hid for hid, _ in built if hid in self._handoff_manifests]
        if duplicate:
            raise DagValidationError(
                f"duplicate conflicting handoff id(s): {sorted(duplicate)}"
            )
        if self.ledger is not None:
            for evidence in result.attempt_evidence:
                self.ledger.append(
                    evidence,
                    factory_run_id=self.factory_run_id,
                )
            for gate in result.gate_results:
                self.ledger.append(
                    gate,
                    factory_run_id=self.factory_run_id,
                )
            for failure in result.failure_records:
                self.ledger.append(
                    failure,
                    factory_run_id=self.factory_run_id,
                )
        self._completion_order.append(node_id)
        self._per_node_compute[node_id] = float(result.compute_seconds)
        if result.success:
            self._transition(node_id, NodeRunState.COMPLETED)
            self._append_evidence_row({
                "row_kind": "dag_node_completed",
                "graph_id": str(self.dag.id),
                "graph_revision": int(self.dag.revision),
                "graph_hash": self.graph_hash,
                "node_id": node_id,
                "workspace_id": result.workspace_id,
                "completion_index": len(self._completion_order),
                "execution_evidence": dict(result.execution_evidence or {}),
            })
            for hid, manifest in built:
                self._handoff_manifests[str(manifest.id)] = manifest
                types = tuple(str(t) for t in (manifest.public_interfaces or ()))
                # availability is keyed by the DECLARED handoff identity —
                # the identity downstream nodes gate on — never by the
                # manifest instance id.
                self._available_handoff_types[hid] = types
                self._append_evidence_row({
                    "row_kind": "dag_handoff_manifest",
                    "graph_id": str(self.dag.id),
                    "graph_revision": int(self.dag.revision),
                    "graph_hash": self.graph_hash,
                    "node_id": node_id,
                    "handoff_id": hid,
                    "handoff_types": list(types),
                    "produced_handoffs": [self._manifest_descriptor(manifest)],
                    "producer_station": (str(self.station_by_node[node_id].id)
                                         if node_id in self.station_by_node else None),
                    "manifest_id": str(manifest.id),
                })
        else:
            self._transition(node_id, NodeRunState.FAILED)
            self._append_evidence_row({
                "row_kind": "dag_node_failed",
                "graph_id": str(self.dag.id),
                "graph_revision": int(self.dag.revision),
                "graph_hash": self.graph_hash,
                "node_id": node_id,
                "error_type": result.error_type,
                "error_message": (result.error_message or "")[:500],
                "execution_evidence": dict(result.execution_evidence or {}),
            })

    # -- node material (product/task/selection per node) ----------------------

    def _product_for_node(self, node_id: str, task: TaskContract | None):
        return self.product  # one canonical ProductSpec for the whole run

    def _selected_paths_for(self, node_id: str) -> list[str]:
        meta = next((n.metadata for n in self.dag.nodes
                     if str(n.node_id) == node_id), {}) or {}
        sel = meta.get("selected_paths")
        return [str(p) for p in sel] if sel else []

    @staticmethod
    def _manifest_descriptor(manifest: HandoffManifest) -> dict[str, Any]:
        artifact = list(manifest.produced_artifacts or ())[0]
        return {
            "handoff_id": str(manifest.id),
            "producer_node_id": str(manifest.node_id),
            "producer_station_id": str(manifest.producer_station_id),
            "artifact_id": artifact.ref_id,
            "artifact_hash": artifact.sha256,
            "artifact_kind": artifact.kind,
            "artifact_location": artifact.location,
            "artifact_size_bytes": artifact.metadata.get("size_bytes"),
            "handoff_types": [str(t) for t in (manifest.public_interfaces or ())],
            "consumer_scope": manifest.consumer_scope,
            **(
                {"retained_artifact_path": artifact.metadata["retained_artifact_path"]}
                if artifact.metadata.get("retained_artifact_path") else {}
            ),
        }

    def _finalize_evidence_rows(self, timing: dict[str, float]) -> tuple[dict[str, Any], ...]:
        return (
            {
                "row_kind": "dag_completion_order",
                "graph_id": str(self.dag.id),
                "graph_revision": int(self.dag.revision),
                "graph_hash": self.graph_hash,
                "completion_order": list(self._completion_order),
            },
            {
                "row_kind": "dag_final",
                "graph_id": str(self.dag.id),
                "graph_revision": int(self.dag.revision),
                "graph_hash": self.graph_hash,
                "final_states": {k: v.value for k, v in sorted(self._states.items())},
                "timing": dict(timing),
                "graph_verified": bool(self._states) and all(
                    state == NodeRunState.COMPLETED for state in self._states.values()
                ),
            },
        )
