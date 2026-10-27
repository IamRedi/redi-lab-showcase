"""REDI-06A — Product DAG & deterministic scheduler foundation.

Pure Layer C.  Builds on the canonical ProductDAG / DagNode / DagDependency
domain contracts (no new canonical contracts, no provider/model information
anywhere in the graph).

FOUNDATION ONLY:  valid versioned graphs, deterministic ready-node
computation, dependency blocking, handoff identity checks, immutable
revision mechanics, separate timing metrics.  No parallel execution, no
learned adaptation, no recipe-driven DAGs.

Scheduling invariants
---------------------
* a node becomes READY only when ALL mandatory (hard) prerequisites are
  COMPLETED and its required handoff identities are available;
* a failed/blocked hard prerequisite BLOCKS the dependent node;
* COMPLETED / RUNNING nodes never become READY again;
* unknown nodes / dependencies fail closed;
* cycles fail graph validation;
* identical graph + identical node states ⇒ identical ready set and order
  (ordering is by node_id and implies no execution dependency).

Timing model:  aggregate node compute time and graph wall-clock time are
recorded as SEPARATE metrics; they are never collapsed.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from .domain import DagDependency, DagNode, ProductDAG
from .domain.validation import validate_dag
from .evidence.artifacts import (
    ArtifactRetentionError,
    retained_relative_path,
    verify_retained_artifact,
)


class DagValidationError(Exception):
    """Fail-closed graph validation error (structure, lineage, compatibility)."""


class DagStateError(Exception):
    """Illegal node state transition."""


class NodeRunState(str, Enum):
    """Deterministic runtime states for a DAG node.

    Values reuse the canonical lifecycle vocabulary where an event exists
    (NODE_READY / NODE_COMPLETED); PENDING / RUNNING / FAILED / BLOCKED /
    SKIPPED are the execution states behind those events.
    """

    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    SKIPPED = "SKIPPED"


LEGAL_TRANSITIONS: dict[NodeRunState, frozenset[NodeRunState]] = {
    NodeRunState.PENDING: frozenset({NodeRunState.READY, NodeRunState.BLOCKED, NodeRunState.SKIPPED}),
    NodeRunState.READY: frozenset({NodeRunState.RUNNING, NodeRunState.BLOCKED, NodeRunState.SKIPPED}),
    NodeRunState.RUNNING: frozenset({NodeRunState.COMPLETED, NodeRunState.FAILED}),
    NodeRunState.COMPLETED: frozenset(),
    NodeRunState.FAILED: frozenset({NodeRunState.READY}),  # explicit retry/reassignment only
    NodeRunState.BLOCKED: frozenset({NodeRunState.READY}),  # unblocking is explicit
    NodeRunState.SKIPPED: frozenset(),
}


def assert_transition(current: NodeRunState, new: NodeRunState) -> None:
    if new not in LEGAL_TRANSITIONS[current]:
        raise DagStateError(f"illegal node transition {current.value} -> {new.value}")


# --- graph identity ----------------------------------------------------------


def product_dag_hash(dag: ProductDAG) -> str:
    """Deterministic sha256 over the canonical graph structure.

    Covers graph id, revision, ordered node identities/fields and ordered
    dependencies (incl. edge kind).  Worker bindings, context strategy and
    scheduler state are deliberately NOT part of the graph identity.
    """
    payload = {
        "graph_id": str(dag.id),
        "revision": int(dag.revision),
        "nodes": [
            {
                "node_id": str(n.node_id),
                "task_id": None if n.task_id is None else str(n.task_id),
                "station_id": None if n.station_id is None else str(n.station_id),
                "role_hint": n.role_hint,
                "parallel_group": n.parallel_group,
                "metadata": _primitive(n.metadata),
            }
            for n in sorted(dag.nodes, key=lambda n: str(n.node_id))
        ],
        "dependencies": [
            {"upstream": str(d.upstream), "downstream": str(d.downstream), "kind": str(d.kind)}
            for d in sorted(dag.dependencies, key=lambda d: (str(d.upstream), str(d.downstream)))
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _primitive(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _primitive(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_primitive(v) for v in value]
    return str(value)


# --- revision mechanics ------------------------------------------------------


@dataclass(frozen=True)
class DagRevision:
    """An immutable successor graph with explicit lineage."""

    dag: ProductDAG
    parent_revision: int
    parent_hash: str
    reason: str
    changed_nodes: tuple[str, ...]
    changed_dependencies: tuple[tuple[str, str], ...]


def revise_product_dag(
    parent: ProductDAG,
    *,
    reason: str,
    nodes: Sequence[DagNode] | None = None,
    dependencies: Sequence[DagDependency] | None = None,
    metadata_merge: Mapping[str, Any] | None = None,
) -> DagRevision:
    """Create revision N+1 from revision N.  The parent is never mutated.

    ``nodes``/``dependencies`` default to the parent's own structure; pass
    modified sequences to change topology.  The child carries explicit
    lineage metadata (parent revision + parent hash + reason) and must pass
    full graph validation before it is returned.
    """
    import dataclasses

    parent_hash = product_dag_hash(parent)
    child_nodes = list(parent.nodes if nodes is None else nodes)
    child_deps = list(parent.dependencies if dependencies is None else dependencies)
    child_meta = dict(parent.metadata)
    if metadata_merge:
        child_meta.update({str(k): _primitive(v) for k, v in metadata_merge.items()})
    child_meta["parent_revision"] = int(parent.revision)
    child_meta["parent_revision_hash"] = parent_hash
    child_meta["revision_reason"] = str(reason)
    child = ProductDAG(
        id=parent.id,  # same versioned graph identity, new revision
        schema_version=parent.schema_version,
        product_id=parent.product_id,
        revision=int(parent.revision) + 1,
        nodes=child_nodes,
        dependencies=child_deps,
        metadata=child_meta,
        created_at=parent.created_at,
    )
    validate_dag(child)  # fail closed on invalid topology
    changed_nodes = tuple(sorted(
        {str(n.node_id) for n in child_nodes} ^ {str(n.node_id) for n in parent.nodes}
    ))
    parent_deps = {(str(d.upstream), str(d.downstream)) for d in parent.dependencies}
    child_dep_set = {(str(d.upstream), str(d.downstream)) for d in child_deps}
    changed_deps = tuple(sorted(parent_deps ^ child_dep_set))
    return DagRevision(
        dag=child,
        parent_revision=int(parent.revision),
        parent_hash=parent_hash,
        reason=str(reason),
        changed_nodes=changed_nodes,
        changed_dependencies=changed_deps,
    )


def validate_revision_lineage(parent: ProductDAG, child: ProductDAG) -> None:
    """Fail closed unless child is the immediate, hash-linked successor."""
    if int(child.revision) != int(parent.revision) + 1:
        raise DagValidationError(
            f"revision lineage broken: child revision {child.revision} does not "
            f"immediately follow parent revision {parent.revision}"
        )
    declared = child.metadata.get("parent_revision_hash")
    if declared != product_dag_hash(parent):
        raise DagValidationError("child parent_revision_hash does not match parent graph hash")
    if child.metadata.get("parent_revision") != int(parent.revision):
        raise DagValidationError("child parent_revision metadata does not match parent revision")


# --- scheduling-time validation ----------------------------------------------


def _node_meta(node: DagNode, key: str) -> tuple:
    meta = node.metadata.get(key) if isinstance(node.metadata, Mapping) else None
    if meta is None:
        return ()
    if isinstance(meta, (str, int)):
        return (str(meta),)
    return tuple(str(x) for x in meta)


def _node_produces(node: DagNode) -> tuple:
    return _node_meta(node, "produces_handoff_ids")


def _node_requires(node: DagNode) -> tuple:
    """Raw requirement entries: legacy strings or typed dicts.  The raw form
    is preserved so typed (id + type) requirements survive parsing."""
    meta = node.metadata.get("requires_handoff_ids") if isinstance(node.metadata, Mapping) else None
    if meta is None:
        return ()
    if isinstance(meta, (str, int)):
        return (str(meta),)
    return tuple(meta)


def validate_product_dag_for_scheduling(
    dag: ProductDAG,
    *,
    task_by_node: Mapping[str, TaskContract] | None = None,
    station_by_node: Mapping[str, StationContract] | None = None,
) -> list[str]:
    """Full scheduling-time validation.  Raises DagValidationError fail-closed.

    Extends the canonical structural validation with handoff-reference
    compatibility and task/station compatibility where canonical contracts
    define it.  Returns the deterministic topological node order.
    """
    order = validate_dag(dag)  # duplicate ids / missing refs / self-loops / cycles
    nodes = {str(n.node_id): n for n in dag.nodes}

    # handoff reference compatibility: every required handoff identity of a
    # node must be produced by one of its declared hard prerequisites.
    producers: dict[str, set[str]] = {}
    producer_types: dict[str, dict[str, set[str]]] = {}
    deps_by_down: dict[str, list[DagDependency]] = {}
    for d in dag.dependencies:
        deps_by_down.setdefault(str(d.downstream), []).append(d)
        upstream_node = nodes.get(str(d.upstream))
        if upstream_node is not None:
            up_types = _node_meta(upstream_node, "produces_handoff_types")
            for hid in _node_produces(upstream_node):
                producers.setdefault(str(d.downstream), set()).add(hid)
                if up_types:
                    producer_types.setdefault(str(d.downstream), {}).setdefault(hid, set()).update(
                        str(t) for t in up_types
                    )
    for node_id, node in nodes.items():
        for required in _node_requires(node):
            if isinstance(required, Mapping):
                hid = str(required.get("handoff_id", ""))
                htype = required.get("handoff_type")
            else:
                hid, htype = str(required), None
            upstreams = {str(d.upstream) for d in deps_by_down.get(node_id, [])}
            offered = producers.get(node_id, set())
            if not upstreams or hid not in offered:
                raise DagValidationError(
                    f"node {node_id!r} requires handoff {hid!r} that no "
                    "declared prerequisite produces"
                )
            if htype is not None:
                declared = producer_types.get(node_id, {}).get(hid, set())
                if declared and str(htype) not in {str(t) for t in declared}:
                    raise DagValidationError(
                        f"node {node_id!r}: required handoff {hid!r} type {htype!r} "
                        "is not declared by any prerequisite"
                    )

    # task/station compatibility where canonical contracts define it
    task_by_node = task_by_node or {}
    station_by_node = station_by_node or {}
    for node_id, node in nodes.items():
        task = task_by_node.get(node_id)
        st = station_by_node.get(node_id)
        if task is None or st is None:
            continue
        applicable = tuple(st.applicable_task_classes or ())
        task_class = (task.metadata or {}).get("task_class") if isinstance(task.metadata, Mapping) else None
        if applicable and task_class is not None and str(task_class) not in {str(a) for a in applicable}:
            raise DagValidationError(
                f"node {node_id!r}: task class {task_class!r} is not accepted by "
                f"station {str(st.id)!r}"
            )
    return order


# --- ready-node computation --------------------------------------------------


@dataclass(frozen=True)
class ReadyDecision:
    """Deterministic outcome of one ready-node computation."""

    ready: tuple[str, ...]
    blocked: tuple[str, ...]
    pending: tuple[str, ...]
    ordering: str = "node_id"




def _handoff_requirements_satisfied(
    requirements: tuple,
    available: set,
    available_types: Mapping[str, Iterable[str]],
) -> bool:
    """Typed handoff requirement matching.

    Legacy string entries match on identity only.  Mapping entries
    ({"handoff_id", "handoff_type"}) require the same identity AND the
    declared type to be offered — correct id with incompatible type does
    NOT satisfy the dependency.
    """
    for req in requirements:
        if isinstance(req, Mapping):
            hid = str(req.get("handoff_id", ""))
            htype = req.get("handoff_type")
            if hid not in available:
                return False
            if htype is not None:
                offered = {str(t) for t in (available_types or {}).get(hid, ())}
                if str(htype) not in offered:
                    return False
        elif str(req) not in available:
            return False
    return True

def compute_ready_nodes(
    dag: ProductDAG,
    states: Mapping[str, NodeRunState],
    *,
    available_handoff_ids: Iterable[str] = (),
    available_handoff_types: Mapping[str, Iterable[str]] | None = None,
) -> ReadyDecision:
    """Pure deterministic function: graph + node states → ready-node set.

    ``states`` may cover any subset of nodes (missing entries default to
    PENDING).  Entries naming unknown nodes fail closed.  Ready order is by
    node_id and implies no execution dependency between independent nodes.
    """
    validate_dag(dag)
    known = {str(n.node_id): n for n in dag.nodes}
    for node_id in states:
        if str(node_id) not in known:
            raise DagValidationError(f"state given for unknown node {node_id!r}")
    normalized: dict[str, NodeRunState] = {
        str(k): (v if isinstance(v, NodeRunState) else NodeRunState(str(v)))
        for k, v in states.items()
    }
    available = {str(h) for h in available_handoff_ids}

    hard_prereqs: dict[str, list[str]] = {nid: [] for nid in known}
    for d in dag.dependencies:
        if str(d.kind) == "hard":
            hard_prereqs[str(d.downstream)].append(str(d.upstream))

    ready: list[str] = []
    blocked: list[str] = []
    pending: list[str] = []
    for node_id in sorted(known):
        current = normalized.get(node_id, NodeRunState.PENDING)
        if current in (NodeRunState.COMPLETED, NodeRunState.RUNNING,
                       NodeRunState.FAILED, NodeRunState.BLOCKED, NodeRunState.SKIPPED):
            continue  # terminal/in-flight nodes never become READY again
        node = known[node_id]
        prereq_states = [
            normalized.get(up, NodeRunState.PENDING) for up in hard_prereqs[node_id]
        ]
        if any(s in (NodeRunState.FAILED, NodeRunState.BLOCKED) for s in prereq_states):
            blocked.append(node_id)
            continue
        if any(s is not NodeRunState.COMPLETED for s in prereq_states):
            pending.append(node_id)
            continue
        required = _node_requires(node)
        if required and not _handoff_requirements_satisfied(
            required, available, available_handoff_types or {}
        ):
            pending.append(node_id)
            continue
        ready.append(node_id)
    return ReadyDecision(ready=tuple(ready), blocked=tuple(blocked), pending=tuple(pending))


# --- timing model ------------------------------------------------------------


def dag_timing_summary(
    node_compute_seconds: Mapping[str, float],
    graph_wall_clock_seconds: float,
) -> dict[str, float]:
    """Aggregate node compute time and graph wall-clock time, kept SEPARATE.

    Invariant going forward: once real parallel execution arrives, aggregate
    node compute may exceed graph wall-clock.  Never collapse the two.
    """
    return {
        "aggregate_node_compute_seconds": round(sum(float(v) for v in node_compute_seconds.values()), 6),
        "graph_wall_clock_seconds": round(float(graph_wall_clock_seconds), 6),
        "per_node_seconds": {str(k): float(v) for k, v in sorted(node_compute_seconds.items())},
    }


# --- scheduling evidence -----------------------------------------------------


def dag_scheduling_evidence(
    dag: ProductDAG,
    states: Mapping[str, NodeRunState],
    *,
    decision: ReadyDecision | None = None,
    graph_hash: str | None = None,
) -> dict[str, Any]:
    """Primitive evidence dict: graph identity, revision, node states and the
    ready-node decision — reconstructable from persisted metadata."""
    normalized = {str(k): (v.value if isinstance(v, NodeRunState) else str(v))
                  for k, v in sorted(states.items())}
    return {
        "graph_id": str(dag.id),
        "graph_revision": int(dag.revision),
        "graph_hash": graph_hash or product_dag_hash(dag),
        "node_states": normalized,
        "ready_nodes": list(decision.ready) if decision else [],
        "blocked_nodes": list(decision.blocked) if decision else [],
        "pending_nodes": list(decision.pending) if decision else [],
    }


# --- deterministic execution simulation (06A foundation; sequential only) ----


@dataclass
class DagNodeOutcome:
    node_id: str
    final_state: NodeRunState
    produced_handoff_ids: tuple[str, ...]
    compute_seconds: float


@dataclass
class DagExecutionResult:
    final_states: dict[str, NodeRunState]
    transitions: dict[str, tuple[str, ...]]
    node_outcomes: tuple[DagNodeOutcome, ...]
    ready_decisions: tuple[dict[str, Any], ...]
    produced_handoff_ids: frozenset[str]
    timing: dict[str, float]


def simulate_dag_execution(
    dag: ProductDAG,
    *,
    node_success: Mapping[str, bool],
    node_compute_seconds: Mapping[str, float] | None = None,
    graph_wall_clock_seconds: float = 0.0,
    produced_handoff_ids_by_node: Mapping[str, Sequence[str]] | None = None,
    produced_handoff_types_by_node: Mapping[str, Sequence[str]] | None = None,
    required_handoff_ids_by_node: Mapping[str, Sequence[str]] | None = None,
) -> DagExecutionResult:
    """Deterministic sequential simulation of one graph execution pass.

    NO provider calls, NO parallelism, NO threads: nodes run one at a time in
    the deterministic ready order using injected outcomes and injected
    compute seconds (non-flaky tests).  Validates the graph first — an
    invalid graph produces zero worker invocations.
    """
    validate_product_dag_for_scheduling(dag)
    compute = dict(node_compute_seconds or {})
    produced_by_node = dict(produced_handoff_ids_by_node or {})
    produced_types_by_node = {
        str(k): tuple(str(t) for t in (v or ()))
        for k, v in (produced_handoff_types_by_node or {}).items()
    }
    required_by_node = dict(required_handoff_ids_by_node or {})
    states: dict[str, NodeRunState] = {}
    transitions: dict[str, tuple[str, ...]] = {}
    available_handoffs: set[str] = set()
    available_handoff_types: dict[str, tuple[str, ...]] = {}
    node_outcomes: list[DagNodeOutcome] = []
    ready_log: list[dict[str, Any]] = []
    executed_compute: dict[str, float] = {}

    def set_state(node_id: str, new: NodeRunState) -> None:
        cur = states.get(node_id, NodeRunState.PENDING)
        assert_transition(cur, new)
        states[node_id] = new
        transitions.setdefault(node_id, tuple(transitions.get(node_id, ())) + (new.value,))

    passes = 0
    while True:
        passes += 1
        if passes > len(dag.nodes) + 3:
            raise DagStateError("scheduler did not converge; possible state bug")
        decision = compute_ready_nodes(
            dag, states,
            available_handoff_ids=available_handoffs,
            available_handoff_types=available_handoff_types,
        )
        ready_log.append(dag_scheduling_evidence(dag, states, decision=decision))

        # newly BLOCKED nodes are recorded first (dependents of failed/blocked)
        newly_blocked = [n for n in decision.blocked if states.get(n) != NodeRunState.BLOCKED]
        for node_id in newly_blocked:
            set_state(node_id, NodeRunState.BLOCKED)

        if decision.ready:
            for node_id in decision.ready:
                set_state(node_id, NodeRunState.READY)
                set_state(node_id, NodeRunState.RUNNING)
                success = bool(node_success.get(node_id, False))
                if success:
                    set_state(node_id, NodeRunState.COMPLETED)
                    available_handoffs |= set(produced_by_node.get(node_id, ()))
                    for hid in produced_by_node.get(node_id, ()):
                        types = produced_types_by_node.get(hid, ())
                        existing = available_handoff_types.get(hid)
                        if existing is not None and tuple(sorted(existing)) != tuple(sorted(types)):
                            raise DagValidationError(
                                f"conflicting handoff identity {hid!r} produced by {node_id!r}"
                            )
                        available_handoff_types[hid] = tuple(types)
                else:
                    set_state(node_id, NodeRunState.FAILED)
                sec = float(compute.get(node_id, 0.0))
                executed_compute[node_id] = sec
                node_outcomes.append(DagNodeOutcome(
                    node_id=node_id,
                    final_state=states[node_id],
                    produced_handoff_ids=tuple(produced_by_node.get(node_id, ())),
                    compute_seconds=sec,
                ))
            continue

        # nothing READY: terminate gracefully when no state can change further
        # (e.g. a required handoff never becomes available -> node stays PENDING)
        break

    final_states = {nid: states.get(nid, NodeRunState.PENDING) for nid in known_ids(dag)}
    return DagExecutionResult(
        final_states=final_states,
        transitions={k: v for k, v in transitions.items()},
        node_outcomes=tuple(node_outcomes),
        ready_decisions=tuple(ready_log),
        produced_handoff_ids=frozenset(available_handoffs),
        timing=dag_timing_summary(executed_compute, graph_wall_clock_seconds),
    )


def known_ids(dag: ProductDAG) -> list[str]:
    return sorted(str(n.node_id) for n in dag.nodes)

# ---------------------------------------------------------------------------
# REDI-06A1 — canonical graph snapshot, typed HandoffManifest closure,
# persisted DAG evidence and fail-closed replay.
# ---------------------------------------------------------------------------


def dag_snapshot(dag: ProductDAG) -> dict[str, Any]:
    """Canonical primitive snapshot: enough to reconstruct topology/revision
    and to recompute the graph hash.  No provider/model/workers inside."""
    return {
        "graph_id": str(dag.id),
        "revision": int(dag.revision),
        "product_id": None if dag.product_id is None else str(dag.product_id),
        "nodes": [
            {
                "node_id": str(n.node_id),
                "task_id": None if n.task_id is None else str(n.task_id),
                "station_id": None if n.station_id is None else str(n.station_id),
                "role_hint": n.role_hint,
                "parallel_group": n.parallel_group,
                "metadata": _primitive(n.metadata),
            }
            for n in sorted(dag.nodes, key=lambda n: str(n.node_id))
        ],
        "dependencies": [
            {"upstream": str(d.upstream), "downstream": str(d.downstream), "kind": str(d.kind)}
            for d in sorted(dag.dependencies, key=lambda d: (str(d.upstream), str(d.downstream)))
        ],
    }


def dag_from_snapshot(snapshot: Mapping[str, Any]) -> ProductDAG:
    """Rebuild a ProductDAG from a persisted canonical snapshot."""
    nodes = [
        DagNode(
            node_id=n["node_id"],
            task_id=n.get("task_id"),
            station_id=n.get("station_id"),
            role_hint=n.get("role_hint"),
            parallel_group=n.get("parallel_group"),
            metadata=n.get("metadata") or {},
        )
        for n in (snapshot.get("nodes") or [])
    ]
    deps = [
        DagDependency(upstream=d["upstream"], downstream=d["downstream"],
                      kind=d.get("kind") or "hard")
        for d in (snapshot.get("dependencies") or [])
    ]
    from .domain import ProductId

    return ProductDAG(
        id=str(snapshot["graph_id"]),
        revision=int(snapshot["revision"]),
        product_id=ProductId(snapshot["product_id"]) if snapshot.get("product_id") else None,
        nodes=nodes,
        dependencies=deps,
        metadata=snapshot.get("metadata") or {},
    )


def handoff_types_from_manifests(
    manifests: Iterable[Any],
) -> dict[str, tuple[str, ...]]:
    """Canonical HandoffManifest binding: handoff identity = str(manifest.id);
    offered types = the manifest's declared public_interfaces (type/schema
    information already supported by the canonical contract)."""
    out: dict[str, tuple[str, ...]] = {}
    for m in manifests or ():
        out[str(m.id)] = tuple(str(i) for i in (m.public_interfaces or ()))
    return out


def validate_handoff_offers(
    offers: Mapping[str, Sequence[str]],
) -> None:
    """Fail closed on duplicate/conflicting handoff identities: the same
    handoff id offered with conflicting type sets is ambiguous."""
    seen: dict[str, tuple[str, ...]] = {}
    for hid, types in offers.items():
        norm = tuple(sorted(str(t) for t in (types or ())))
        if hid in seen and seen[hid] != norm:
            raise DagValidationError(
                f"conflicting handoff offers for identity {hid!r}: "
                f"{seen[hid]} vs {norm}"
            )
        seen[hid] = norm


def dag_run_evidence_rows(
    dag: ProductDAG,
    execution: "DagExecutionResult",
    *,
    required_handoff_ids_by_node: Mapping[str, Sequence[str]] | None = None,
    produced_handoff_types_by_id: Mapping[str, Sequence[str]] | None = None,
) -> list[dict[str, Any]]:
    """Primitive evidence rows for ONE DAG run (persist as RunEvidence
    metadata; one canonical graph snapshot row, then per-node transition and
    final-outcome rows — no graph body duplication in every row)."""
    snapshot = dag_snapshot(dag)
    graph_hash = product_dag_hash(dag)
    required_by_node: dict[str, list[Any]] = {}
    for k, v in (required_handoff_ids_by_node or {}).items():
        entries: list[Any] = []
        for x in (v or ()):
            if isinstance(x, Mapping):
                entries.append({"handoff_id": str(x.get("handoff_id")),
                                "handoff_type": str(x.get("handoff_type"))})
            else:
                entries.append(str(x))
        required_by_node[str(k)] = entries
    produced_types = {
        str(k): tuple(str(t) for t in (v or ()))
        for k, v in (produced_handoff_types_by_id or {}).items()
    }
    rows: list[dict[str, Any]] = [
        {
            "row_kind": "dag_snapshot",
            "graph_id": str(dag.id),
            "graph_revision": int(dag.revision),
            "graph_hash": graph_hash,
            "dag_snapshot": snapshot,
        }
    ]
    node_index = {str(n.node_id): n for n in dag.nodes}
    for outcome in execution.node_outcomes:
        node = node_index[outcome.node_id]
        final_state = (outcome.final_state.value if hasattr(outcome.final_state, "value")
                       else str(outcome.final_state))
        transition_chain = (("PENDING", "READY"), ("READY", "RUNNING"),
                            ("RUNNING", final_state))
        for frm, to in transition_chain:
            row = {
                "row_kind": "dag_node_transition",
                "graph_id": str(dag.id),
                "graph_revision": int(dag.revision),
                "graph_hash": graph_hash,
                "node_id": outcome.node_id,
                "from_state": frm,
                "to_state": to,
                "compute_seconds": outcome.compute_seconds if to == final_state else 0.0,
            }
            if to == final_state:
                # requirement/production identity binds to the terminal step
                row["required_handoffs"] = [
                    ({"handoff_id": str(x.get("handoff_id", "")),
                      "handoff_type": (None if x.get("handoff_type") is None
                                       else str(x.get("handoff_type")))}
                     if isinstance(x, Mapping)
                     else {"handoff_id": str(x), "handoff_type": None})
                    for x in _node_requires(node)
                ]
                row["produced_handoffs"] = [
                    {"handoff_id": hid,
                     "handoff_types": list(produced_types.get(hid, ()))}
                    for hid in outcome.produced_handoff_ids
                ]
            rows.append(row)
    rows.append({
        "row_kind": "dag_final",
        "graph_id": str(dag.id),
        "graph_revision": int(dag.revision),
        "graph_hash": graph_hash,
        "final_states": {
            k: (v.value if hasattr(v, "value") else str(v))
            for k, v in execution.final_states.items()
        },
        "timing": dict(execution.timing),
    })
    return rows


def replay_dag_run(
    rows: Sequence[Mapping[str, Any]],
    *,
    expected_graph_hash: str | None = None,
    expected_revision: int | None = None,
    retained_artifact_root: Path | str | None = None,
) -> dict[str, Any]:
    """Fail-closed replay of a persisted DAG run from its evidence rows.

    Zero provider calls, zero writes.  Validates: snapshot present, hash
    consistent, revision consistent, legal state transitions, prerequisite
    edges consistent with the snapshot, ready decisions consistent with the
    persisted states, handoff identity/type satisfaction.  Returns the
    reconstruction (graph, revision, hash, states, transitions, decisions,
    handoff identities/types, timing)."""
    if not rows:
        raise DagValidationError("replay evidence is empty: missing graph snapshot")
    rows = [dict(r) for r in rows]
    snap_rows = [r for r in rows if r.get("row_kind") == "dag_snapshot"]
    if not snap_rows:
        raise DagValidationError("missing graph snapshot in replay evidence")
    snapshot = snap_rows[0]["dag_snapshot"]
    graph = dag_from_snapshot(snapshot)
    if expected_revision is not None and int(snapshot["revision"]) != int(expected_revision):
        raise DagValidationError(
            f"graph revision mismatch: snapshot {snapshot['revision']} != expected {expected_revision}"
        )
    graph_hash = product_dag_hash(graph)
    declared_hash = snap_rows[0].get("graph_hash")
    if declared_hash is not None and declared_hash != graph_hash:
        raise DagValidationError(
            f"graph hash mismatch: snapshot-derived {graph_hash} != recorded {declared_hash}"
        )
    if expected_graph_hash is not None and expected_graph_hash != graph_hash:
        raise DagValidationError(
            f"graph hash mismatch vs expectation: {graph_hash} != {expected_graph_hash}"
        )
    validate_dag(graph)

    node_ids = {str(n.node_id) for n in graph.nodes}
    hard_prereqs: dict[str, list[str]] = {nid: [] for nid in node_ids}
    for d in graph.dependencies:
        if str(d.kind) == "hard":
            hard_prereqs[str(d.downstream)].append(str(d.upstream))
    node_requires = {str(n.node_id): _node_requires(n) for n in graph.nodes}

    states: dict[str, NodeRunState] = {nid: NodeRunState.PENDING for nid in sorted(node_ids)}
    available_handoff_types: dict[str, tuple[str, ...]] = {}
    handoff_descriptors: dict[str, dict[str, Any]] = {}
    launch_order: list[str] = []
    completion_order: list[str] = []
    replayed_decisions: list[dict[str, Any]] = []
    replayed_executions: list[dict[str, Any]] = []
    launch_records: list[dict[str, Any]] = []
    node_results: list[dict[str, Any]] = []
    retained_artifacts: list[dict[str, Any]] = []
    final_record: dict[str, Any] = {}
    timing: dict[str, Any] | None = None
    for row in rows:
        kind = row.get("row_kind")
        g_hash = row.get("graph_hash")
        if g_hash is not None and g_hash != graph_hash:
            raise DagValidationError(
                f"evidence row references foreign graph hash {g_hash!r}"
            )
        rev = row.get("graph_revision")
        if rev is not None and int(rev) != int(snapshot["revision"]):
            raise DagValidationError("evidence row references foreign graph revision")
        if kind == "dag_snapshot":
            continue  # already validated as the canonical snapshot
        if kind == "dag_node_launch":
            nid = str(row.get("node_id") or "")
            if nid not in node_ids:
                raise DagValidationError(f"launch references unknown node {nid!r}")
            if states.get(nid) not in (NodeRunState.READY, NodeRunState.RUNNING):
                raise DagValidationError(
                    f"launch inconsistent for {nid!r}: state {states.get(nid)}"
                )
            launch_order.append(nid)
            launch_records.append(dict(row))
            continue
        if kind == "dag_node_blocked":
            nid = str(row.get("node_id") or "")
            if nid not in node_ids:
                raise DagValidationError(f"blocked row references unknown node {nid!r}")
            states[nid] = NodeRunState.BLOCKED
            continue
        if kind == "dag_node_completed":
            nid = str(row.get("node_id") or "")
            if nid not in node_ids:
                raise DagValidationError(f"completion references unknown node {nid!r}")
            states[nid] = NodeRunState.COMPLETED
            completion_order.append(nid)
            node_results.append(dict(row))
            continue
        if kind == "dag_node_failed":
            nid = str(row.get("node_id") or "")
            if nid not in node_ids:
                raise DagValidationError(f"failure references unknown node {nid!r}")
            states[nid] = NodeRunState.FAILED
            node_results.append(dict(row))
            continue
        if kind == "dag_handoff_manifest":
            for produced in row.get("produced_handoffs") or []:
                hid = str(produced.get("handoff_id"))
                types = tuple(str(t) for t in (produced.get("handoff_types") or ()))
                required_fields = {
                    "handoff_id", "producer_node_id", "producer_station_id",
                    "artifact_id", "artifact_hash", "artifact_kind",
                    "artifact_location", "artifact_size_bytes", "handoff_types",
                    "consumer_scope",
                }
                missing = required_fields - set(produced)
                if missing:
                    raise DagValidationError(
                        f"handoff {hid!r} replay descriptor missing {sorted(missing)}"
                    )
                producer_node = str(produced.get("producer_node_id") or "")
                if producer_node not in node_ids or producer_node != str(row.get("node_id") or ""):
                    raise DagValidationError(
                        f"handoff {hid!r} replay producer node mismatch"
                    )
                producer_graph_node = next(
                    n for n in graph.nodes if str(n.node_id) == producer_node
                )
                expected_station = str(producer_graph_node.station_id or "")
                if str(produced.get("producer_station_id") or "") != expected_station:
                    raise DagValidationError(
                        f"handoff {hid!r} replay producer station mismatch"
                    )
                artifact_hash = str(produced.get("artifact_hash") or "")
                if not re.fullmatch(r"[0-9a-f]{64}", artifact_hash):
                    raise DagValidationError(
                        f"handoff {hid!r} replay artifact hash is invalid"
                    )
                try:
                    artifact_size = int(produced.get("artifact_size_bytes"))
                except (TypeError, ValueError) as exc:
                    raise DagValidationError(
                        f"handoff {hid!r} replay artifact size is invalid"
                    ) from exc
                if artifact_size < 0 or artifact_size > 1024 * 1024:
                    raise DagValidationError(
                        f"handoff {hid!r} replay artifact size is outside bounds"
                    )
                retained_path = produced.get("retained_artifact_path")
                if retained_path is not None:
                    try:
                        expected_retained_path = retained_relative_path(
                            str(produced.get("artifact_id") or "")
                        )
                    except ArtifactRetentionError as exc:
                        raise DagValidationError(
                            f"handoff {hid!r} retained artifact identity is invalid"
                        ) from exc
                    if str(retained_path) != expected_retained_path:
                        raise DagValidationError(
                            f"handoff {hid!r} retained artifact identity/path mismatch"
                        )
                if retained_artifact_root is not None:
                    if retained_path is None:
                        raise DagValidationError(
                            f"handoff {hid!r} missing retained artifact path"
                        )
                    try:
                        verified_artifact = verify_retained_artifact(
                            run_artifact_root=retained_artifact_root,
                            retained_path=str(retained_path),
                            artifact_id=str(produced.get("artifact_id") or ""),
                            expected_sha256=artifact_hash,
                            expected_size=artifact_size,
                        )
                    except ArtifactRetentionError as exc:
                        raise DagValidationError(
                            f"handoff {hid!r} retained artifact verification failed: {exc}"
                        ) from exc
                    retained_artifacts.append(dict(verified_artifact))
                expected_consumers = sorted({
                    str(n.station_id)
                    for n in graph.nodes
                    if hid in {
                        str(req.get("handoff_id", "")) if isinstance(req, Mapping) else str(req)
                        for req in node_requires.get(str(n.node_id), ())
                    }
                })
                recorded_consumers = sorted(
                    part for part in str(produced.get("consumer_scope") or "").split(",") if part
                )
                if recorded_consumers != expected_consumers:
                    raise DagValidationError(
                        f"handoff {hid!r} replay consumer_scope mismatch"
                    )
                existing = available_handoff_types.get(hid)
                if existing is not None and tuple(sorted(existing)) != tuple(sorted(types)):
                    raise DagValidationError(
                        f"conflicting handoff identity {hid!r} during replay"
                    )
                normalized = dict(produced)
                prior_descriptor = handoff_descriptors.get(hid)
                if prior_descriptor is not None and prior_descriptor != normalized:
                    raise DagValidationError(
                        f"conflicting handoff material for identity {hid!r} during replay"
                    )
                available_handoff_types[hid] = types
                handoff_descriptors[hid] = normalized
            continue
        if kind == "dag_completion_order":
            recorded_order = [str(x) for x in (row.get("completion_order") or [])]
            terminal_via_worker_results = [
                d["node_id"] for d in replayed_executions
                if d.get("to_state") in {"COMPLETED", "FAILED"}
            ]
            if sorted(recorded_order) != sorted(terminal_via_worker_results):
                raise DagValidationError(
                    "completion order inconsistent with terminal worker transitions"
                )
            completion_order = recorded_order
            continue
        if kind == "dag_ready_decision":
            recorded_states = row.get("node_states") or {}
            for nid in recorded_states:
                if nid not in node_ids:
                    raise DagValidationError(f"ready decision references unknown node {nid!r}")
            recomputed = compute_ready_nodes(
                graph,
                {k: NodeRunState(v) for k, v in recorded_states.items()},
                available_handoff_ids=set(available_handoff_types),
                available_handoff_types=available_handoff_types,
            )
            expected_ready = list(recomputed.ready)
            if list(row.get("ready_nodes") or []) != expected_ready:
                raise DagValidationError(
                    f"ready decision inconsistent: recorded {row.get('ready_nodes')} "
                    f"!= recomputed {expected_ready}"
                )
            replayed_decisions.append(dict(row))
        elif kind in ("dag_node_execution", "dag_node_transition"):
            node_id = str(row.get("node_id") or "")
            if node_id not in node_ids:
                raise DagValidationError(f"transition references unknown node {node_id!r}")
            frm = NodeRunState(row["from_state"])
            to = NodeRunState(row["to_state"])
            cur = states.get(node_id, NodeRunState.PENDING)
            if frm != cur:
                raise DagValidationError(
                    f"transition for {node_id!r} starts from {frm.value} but "
                    f"persisted state is {cur.value}"
                )
            assert_transition(cur, to)
            # handoff identity/type satisfaction binds to the terminal step
            for req in node_requires.get(node_id, ()):
                if isinstance(req, Mapping):
                    hid = str(req.get("handoff_id", ""))
                    htype = str(req.get("handoff_type")) if req.get("handoff_type") is not None else None
                else:
                    hid, htype = str(req), None
                if htype is not None and to.value == "COMPLETED":
                    offered = available_handoff_types.get(hid)
                    if offered is None:
                        raise DagValidationError(
                            f"node {node_id!r} completed without required handoff "
                            f"{hid!r} (type {htype!r})"
                        )
                    if htype not in {str(t) for t in offered}:
                        raise DagValidationError(
                            f"node {node_id!r} completed with incompatible handoff type "
                            f"for {hid!r}: {htype!r} not in {sorted(map(str, offered))}"
                        )
            recorded_states = row.get("node_states") or {}
            if recorded_states:
                recorded = NodeRunState(recorded_states.get(node_id))
                if recorded != to:
                    raise DagValidationError(
                        f"executed node state inconsistent for {node_id!r}: "
                        f"{recorded} != {to}"
                    )
            for produced in row.get("produced_handoffs") or []:
                hid = str(produced.get("handoff_id"))
                types = tuple(str(t) for t in (produced.get("handoff_types") or ()))
                existing = available_handoff_types.get(hid)
                if existing is not None and tuple(sorted(existing)) != tuple(sorted(types)):
                    raise DagValidationError(
                        f"conflicting handoff identity {hid!r} during replay"
                    )
                available_handoff_types[hid] = types
            states[node_id] = to
            replayed_executions.append(dict(row))
        elif kind == "dag_final":
            for nid, st in (row.get("final_states") or {}).items():
                if nid not in node_ids:
                    raise DagValidationError(f"final state references unknown node {nid!r}")
                if NodeRunState(st) != states.get(nid, NodeRunState.PENDING):
                    raise DagValidationError(
                        f"final state mismatch for {nid!r}: "
                        f"{states.get(nid)} != {NodeRunState(st)}"
                    )
            if row.get("timing"):
                timing = dict(row["timing"])
            final_record = dict(row)
        else:
            raise DagValidationError(f"unknown replay evidence row_kind: {kind!r}")

    return {
        "graph": graph,
        "graph_id": str(graph.id),
        "graph_revision": int(graph.revision),
        "graph_hash": graph_hash,
        "final_states": dict(states),
        "transitions": replayed_executions,
        "ready_decisions": replayed_decisions,
        "launch_records": launch_records,
        "node_results": node_results,
        "available_handoff_types": dict(available_handoff_types),
        "handoff_descriptors": dict(handoff_descriptors),
        "retained_artifacts": retained_artifacts,
        "launch_order": list(launch_order),
        "completion_order": list(completion_order),
        "timing": timing or {},
        "final_record": final_record,
    }


def _canonical_replay_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(k): _canonical_replay_value(v)
            for k, v in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_replay_value(v) for v in value]
    return value


def canonical_replay_view(replay: Mapping[str, Any]) -> dict[str, Any]:
    """Canonical structural comparison view for a persisted DAG replay.

    The domain ``ProductDAG`` instance itself is intentionally excluded from
    equality.  Its canonical snapshot and hash are included instead, along
    with the complete persisted execution semantics needed by REDI-06C.
    """
    graph = replay.get("graph")
    if not isinstance(graph, ProductDAG):
        raise DagValidationError("replay result is missing canonical ProductDAG")
    launches = list(replay.get("launch_records") or ())
    results = list(replay.get("node_results") or ())
    selected_workers: dict[str, Any] = {}
    provider_timing: dict[str, Any] = {}
    token_evidence: dict[str, Any] = {}
    gate_outcomes: dict[str, Any] = {}
    for row in results:
        node_id = str(row.get("node_id") or "")
        execution = row.get("execution_evidence") or {}
        if execution.get("worker_selection") is not None:
            selected_workers[node_id] = execution.get("worker_selection")
        if execution.get("provider_timing") is not None:
            provider_timing[node_id] = execution.get("provider_timing")
        if execution.get("tokens") is not None:
            token_evidence[node_id] = execution.get("tokens")
        if execution.get("gates") is not None:
            gate_outcomes[node_id] = execution.get("gates")
    context_hashes = {
        str(row.get("node_id") or ""): {
            "context_package_hash": row.get("context_package_hash"),
            "rendered_hash": row.get("rendered_hash"),
        }
        for row in launches
    }
    final_record = dict(replay.get("final_record") or {})
    final_verdicts = {
        str(row.get("node_id") or ""): (row.get("execution_evidence") or {}).get(
            "final_verdict"
        )
        for row in results
        if (row.get("execution_evidence") or {}).get("final_verdict") is not None
    }
    view = {
        "dag_snapshot": dag_snapshot(graph),
        "graph_id": replay.get("graph_id"),
        "graph_revision": replay.get("graph_revision"),
        "graph_hash": replay.get("graph_hash"),
        "launch_order": replay.get("launch_order") or [],
        "completion_order": replay.get("completion_order") or [],
        "node_transitions": replay.get("transitions") or [],
        "ready_decisions": replay.get("ready_decisions") or [],
        "final_states": replay.get("final_states") or {},
        "selected_workers": selected_workers,
        "provider_timing": provider_timing,
        "token_evidence": token_evidence,
        "context_hashes": context_hashes,
        "gate_outcomes": gate_outcomes,
        "handoff_descriptors": replay.get("handoff_descriptors") or {},
        "retained_artifacts": replay.get("retained_artifacts") or [],
        "timing": replay.get("timing") or {},
        "final_graph_verification": {
            "graph_verified": final_record.get("graph_verified"),
            "final_verdicts": final_verdicts,
        },
    }
    return _canonical_replay_value(view)
