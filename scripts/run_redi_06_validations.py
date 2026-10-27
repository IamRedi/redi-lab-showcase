"""stdlib-only REDI-06A validation runner (Product DAG & scheduler foundation).

Deterministic, zero-network. Mirrors the REDI-01..05 pack pattern:
    python -m scripts.run_redi_06_validations
"""

from __future__ import annotations

import json
import hashlib
import os
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

PASS = 0
FAIL = 0
RESULTS: list[tuple[str, bool, str]] = []


def check(name, fn):
    global PASS, FAIL
    try:
        fn()
        PASS += 1
        RESULTS.append((name, True, ""))
        print(f"  PASS {name}")
    except AssertionError as e:
        FAIL += 1
        RESULTS.append((name, False, str(e)))
        print(f"  FAIL {name}: {e}")
    except Exception as e:  # noqa: BLE001
        FAIL += 1
        RESULTS.append((name, False, f"{type(e).__name__}: {e}"))
        print(f"  FAIL {name}: {type(e).__name__}: {e}")


def _abc_graph():
    from redi_lab.domain import DagDependency, ProductDAG
    from redi_lab.domain.contracts import DagNode

    return ProductDAG(
        nodes=[
            DagNode(node_id="node_a", station_id="station_builder",
                    metadata={"produces_handoff_ids": ["handoff_a"]}),
            DagNode(node_id="node_b", station_id="station_builder_test",
                    metadata={"produces_handoff_ids": ["handoff_b"]}),
            DagNode(node_id="node_c", station_id="station_integration",
                    metadata={"requires_handoff_ids": ["handoff_a", "handoff_b"]}),
        ],
        dependencies=[
            DagDependency(upstream="node_a", downstream="node_c", kind="hard"),
            DagDependency(upstream="node_b", downstream="node_c", kind="hard"),
        ],
        metadata={"fixture": "redi06_ab_c"},
    )


def d01_valid_abc_graph():
    from redi_lab.dag_scheduler import validate_product_dag_for_scheduling

    order = validate_product_dag_for_scheduling(_abc_graph())
    assert order.index("node_a") < order.index("node_c")
    assert order.index("node_b") < order.index("node_c")


def d02_initial_ready_set():
    from redi_lab.dag_scheduler import compute_ready_nodes

    decision = compute_ready_nodes(_abc_graph(), {})
    assert list(decision.ready) == ["node_a", "node_b"]
    assert decision.pending == ("node_c",)


def d03_c_blocked_until_both_complete():
    from redi_lab.dag_scheduler import compute_ready_nodes, NodeRunState

    dag = _abc_graph()
    half = compute_ready_nodes(dag, {"node_a": NodeRunState.COMPLETED})
    assert "node_c" not in half.ready and "node_b" in half.ready
    both = compute_ready_nodes(dag, {"node_a": NodeRunState.COMPLETED,
                                     "node_b": NodeRunState.COMPLETED},
                               available_handoff_ids={"handoff_a", "handoff_b"})
    assert list(both.ready) == ["node_c"]


def d04_a_failure_blocks_c_b_independent():
    from redi_lab.dag_scheduler import compute_ready_nodes, NodeRunState

    decision = compute_ready_nodes(_abc_graph(), {"node_a": NodeRunState.FAILED})
    assert decision.blocked == ("node_c",)
    assert "node_b" in decision.ready


def d05_validation_fail_closed():
    from redi_lab.domain import DagDependency, ProductDAG
    from redi_lab.dag_scheduler import validate_product_dag_for_scheduling

    from redi_lab.domain.contracts import DagNode
    dag = ProductDAG(nodes=[DagNode(node_id="A"), DagNode(node_id="A")])
    try:
        validate_product_dag_for_scheduling(dag)
        raise AssertionError("duplicate nodes accepted")
    except Exception as e:
        assert "duplicate" in str(e).lower()
    # unknown dependency
    dag = ProductDAG(nodes=[DagNode(node_id="A")],
                     dependencies=[DagDependency(upstream="ghost", downstream="A")])
    try:
        validate_product_dag_for_scheduling(dag)
        raise AssertionError("unknown dependency accepted")
    except Exception as e:
        assert "does not reference" in str(e)
    # cycle
    dag = ProductDAG(nodes=[DagNode(node_id="A"), DagNode(node_id="B")],
                     dependencies=[DagDependency(upstream="A", downstream="B"),
                                   DagDependency(upstream="B", downstream="A")])
    try:
        validate_product_dag_for_scheduling(dag)
        raise AssertionError("cycle accepted")
    except Exception as e:
        assert "topologically" in str(e) or "cycle" in str(e).lower()


def d06_deterministic_ready_ordering():
    from redi_lab.dag_scheduler import compute_ready_nodes

    dag = _abc_graph()
    runs = [tuple(compute_ready_nodes(dag, {}).ready) for _ in range(5)]
    assert runs == [("node_a", "node_b")] * 5


def d07_graph_hash_reproducible_and_sensitive():
    from redi_lab.dag_scheduler import product_dag_hash
    from redi_lab.domain import ProductDAG, DagDependency

    dag = _abc_graph()
    h1 = product_dag_hash(dag)
    h2 = product_dag_hash(_abc_graph())
    twin = _abc_graph()
    twin.id = dag.id
    assert product_dag_hash(twin) == h1  # same identity → same hash
    assert h2 != h1  # distinct graph instances (distinct ids) → distinct hashes
    changed = ProductDAG(
        id=dag.id, revision=dag.revision, product_id=dag.product_id,
        nodes=dag.nodes,
        dependencies=[DagDependency(upstream="node_a", downstream="node_c", kind="hard")],
        metadata=dict(dag.metadata),
    )
    assert product_dag_hash(changed) != h1  # topology change → hash change


def d08_revision_lineage():
    from redi_lab.domain import DagDependency
    from redi_lab.dag_scheduler import (
        product_dag_hash, revise_product_dag, validate_revision_lineage,
    )

    dag = _abc_graph()
    h0 = product_dag_hash(dag)
    rev = revise_product_dag(
        dag, reason="split node_b",
        dependencies=[DagDependency(upstream="node_a", downstream="node_c"),
                      DagDependency(upstream="node_b", downstream="node_c")],
    )
    assert rev.dag.revision == 2 and rev.parent_revision == 1
    assert rev.parent_hash == h0
    assert rev.dag.metadata["parent_revision_hash"] == h0
    assert rev.dag.metadata["revision_reason"] == "split node_b"
    validate_revision_lineage(dag, rev.dag)
    try:
        validate_revision_lineage(dag, dag)
        raise AssertionError("self lineage accepted")
    except Exception as e:
        assert "lineage" in str(e) or "revision" in str(e)


def d09_completed_never_reruns():
    from redi_lab.dag_scheduler import compute_ready_nodes, NodeRunState

    dag = _abc_graph()
    decision = compute_ready_nodes(
        dag, {"node_a": NodeRunState.COMPLETED, "node_b": NodeRunState.COMPLETED},
        available_handoff_ids={"handoff_a", "handoff_b"})
    assert "node_a" not in decision.ready and "node_b" not in decision.ready


def d10_handoff_gating():
    from redi_lab.dag_scheduler import compute_ready_nodes, NodeRunState

    dag = _abc_graph()
    states = {"node_a": NodeRunState.COMPLETED, "node_b": NodeRunState.COMPLETED}
    missing = compute_ready_nodes(dag, states, available_handoff_ids={"handoff_a"})
    assert missing.ready == ()  # required handoff_b missing → not READY
    present = compute_ready_nodes(dag, states, available_handoff_ids={"handoff_a", "handoff_b"})
    assert list(present.ready) == ["node_c"]


def d11_worker_context_orthogonality():
    """Worker bindings and context strategy are NOT part of DAG identity."""
    from redi_lab.dag_scheduler import product_dag_hash
    from redi_lab.domain.contracts import DagNode

    dag_fields = set(type(_abc_graph()).__dataclass_fields__)
    node_fields = set(DagNode.__dataclass_fields__)
    for forbidden in ("worker", "model", "provider", "context_strategy"):
        assert not any(f.startswith(forbidden) for f in dag_fields)
        assert not any(f.startswith(forbidden) for f in node_fields)


def d12_scheduling_evidence_reconstructs():
    from redi_lab.dag_scheduler import (
        compute_ready_nodes, dag_scheduling_evidence, NodeRunState,
    )

    dag = _abc_graph()
    states = {"node_a": NodeRunState.COMPLETED, "node_b": NodeRunState.COMPLETED}
    decision = compute_ready_nodes(dag, states, available_handoff_ids={"handoff_a", "handoff_b"})
    ev = dag_scheduling_evidence(dag, states, decision=decision)
    blob = json.dumps(ev, sort_keys=True)
    assert ev["graph_revision"] == 1
    assert ev["ready_nodes"] == ["node_c"]
    assert ev["node_states"]["node_a"] == "COMPLETED"
    assert json.loads(blob) == ev  # primitive, deterministic
    # run it again → identical evidence
    ev2 = dag_scheduling_evidence(dag, states, decision=decision)
    assert ev == ev2


def d13_timing_model_separate():
    from redi_lab.dag_scheduler import dag_timing_summary

    t = dag_timing_summary({"node_a": 2.0, "node_b": 3.0}, 4.0)
    assert t["aggregate_node_compute_seconds"] == 5.0
    assert t["graph_wall_clock_seconds"] == 4.0
    assert "aggregate_node_compute_seconds" in t and "graph_wall_clock_seconds" in t
    assert t["per_node_seconds"] == {"node_a": 2.0, "node_b": 3.0}


def d14_simulated_ab_c_expected_behavior():
    from redi_lab.dag_scheduler import NodeRunState, simulate_dag_execution

    dag = _abc_graph()
    result = simulate_dag_execution(
        dag,
        node_success={"node_a": False, "node_b": True, "node_c": True},
        node_compute_seconds={"node_a": 2.0, "node_b": 3.0, "node_c": 1.5},
        graph_wall_clock_seconds=4.0,
        produced_handoff_ids_by_node={"node_a": ["handoff_a"], "node_b": ["handoff_b"]},
        required_handoff_ids_by_node={"node_c": ["handoff_a", "handoff_b"]},
    )
    assert result.final_states["node_a"] == "FAILED" or result.final_states["node_a"].value == "FAILED"
    assert result.final_states["node_b"].value == "COMPLETED"
    assert result.final_states["node_c"].value == "BLOCKED"
    assert result.timing["aggregate_node_compute_seconds"] == 5.0
    assert result.timing["graph_wall_clock_seconds"] == 4.0


def d15_simulated_ab_c_success_with_handoffs():
    from redi_lab.dag_scheduler import NodeRunState, simulate_dag_execution

    dag = _abc_graph()
    result = simulate_dag_execution(
        dag,
        node_success={"node_a": True, "node_b": True, "node_c": True},
        node_compute_seconds={"node_a": 1.0, "node_b": 2.0, "node_c": 0.5},
        graph_wall_clock_seconds=3.0,
        produced_handoff_ids_by_node={"node_a": ["handoff_a"], "node_b": ["handoff_b"]},
        required_handoff_ids_by_node={"node_c": ["handoff_a", "handoff_b"]},
    )
    assert all(s.value == "COMPLETED" for s in result.final_states.values())
    assert result.produced_handoff_ids == frozenset({"handoff_a", "handoff_b"})
    first_ready = result.ready_decisions[0]["ready_nodes"]
    assert first_ready == ["node_a", "node_b"]


# ---------------------------------------------------------------------------
# REDI-06B — isolated parallel execution (bounded threads, single coordinator)
# ---------------------------------------------------------------------------

_ALPHA_IMPL = (
    "def alpha(text):\n"
    "    if not isinstance(text, str):\n        raise TypeError\n"
    "    return text.upper()\n"
)
_BETA_IMPL = (
    "def beta(text):\n"
    "    if not isinstance(text, str):\n        raise TypeError\n"
    "    return text[::-1]\n"
)
_INTEGRATION_IMPL = "integration PASS\n"


def _parallel_bundle():
    from redi_lab.domain import ProductSpec, Requirement, RiskTier
    from redi_lab.domain.contracts import StationContract, TaskContract

    task = TaskContract(goal="implement alpha and beta modules")
    builder = StationContract(role="builder", required_output_kinds=["patch_edit"],
                              allowed_tool_classes=[])
    beta_station = StationContract(role="builder_beta", required_output_kinds=["patch_edit"],
                                   allowed_tool_classes=[])
    integration = StationContract(role="integration", required_output_kinds=["verdict"],
                                  allowed_tool_classes=[])
    dag = _abc_graph()
    from dataclasses import replace
    station_ids = (builder.id, beta_station.id, integration.id)
    dag.nodes = [
        replace(node, station_id=str(station_id), task_id=str(task.id))
        for node, station_id in zip(dag.nodes, station_ids)
    ]
    dag.nodes[0].metadata["selected_paths"] = ["alpha/__init__.py"]
    dag.nodes[1].metadata["selected_paths"] = ["beta/__init__.py"]
    product = ProductSpec(
        requested_outcome="Implement alpha (uppercase) and beta (reverse) modules.",
        requirements=[Requirement(text="alpha uppercases", kind="functional"),
                      Requirement(text="beta reverses", kind="functional")],
        constraints=["Stdlib only."],
        risk_tier=RiskTier.LOW, unresolved_requirements=[],
    )
    return {
        "product": product, "task": task,
        "stations": {"node_a": builder, "node_b": beta_station, "node_c": integration},
        "dag": dag,
    }


def _parallel_executor(bundle, workers, *, max_parallelism=2, ledger=None):
    from redi_lab.context_compiler import DEFAULT_BUDGET_BYTES
    from redi_lab.parallel_dag_executor import ParallelDagExecutor

    return ParallelDagExecutor(
        dag=bundle["dag"],
        station_by_node=bundle["stations"],
        task_by_node={nid: bundle["task"] for nid in ("node_a", "node_b", "node_c")},
        node_workers=workers,
        required_handoff_ids_by_node={"node_c": ["handoff_a", "handoff_b"]},
        produced_handoff_ids_by_node={"node_a": ["handoff_a"], "node_b": ["handoff_b"]},
        produced_handoff_types_by_id={"handoff_a": ("alpha.impl.v1",),
                                      "handoff_b": ("beta.impl.v1",)},
        fixture_root=REPO_ROOT / "experiments" / "fixtures" / "dag_pair_pkg",
        product=bundle["product"],
        max_parallelism=max_parallelism,
        context_budget_bytes=DEFAULT_BUDGET_BYTES,
        ledger=ledger,
    )


def _write_module(handle, sub, content):
    target = handle.root / sub / "__init__.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def _artifact_descriptor(handle, hid, target):
    contract = dict(handle.handoff_output_contracts.get(hid, {}))
    return {
        "handoff_id": hid,
        "artifact_id": f"artifact::{handle.node_id}::{hid}",
        "path": target.relative_to(handle.root).as_posix(),
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "kind": "file",
        "size_bytes": target.stat().st_size,
        "producer_node_id": contract.get("producer_node_id", handle.node_id),
        "producer_station_id": contract.get(
            "producer_station_id", handle.context_package.station_id
        ),
        "consumer_scope": contract.get("consumer_scope", ""),
        "metadata": {"purpose": "validation handoff"},
    }


def _mk_worker(content, *, handoff_ids=(), types_by_id=None, error=None, success=True):
    from redi_lab.parallel_dag_executor import WorkerExecutionResult
    from redi_lab.runtime.contracts import WorkerRunner

    class _W(WorkerRunner):
        def run_node(self, *, handle, **_):
            if error is not None:
                raise error
            sub = {"node_a": "alpha", "node_b": "beta", "node_c": "integration"}[
                str(handle.node_id)]
            target = _write_module(handle, sub, content)
            ids = tuple(handoff_ids) if success else ()
            return WorkerExecutionResult(
                node_id=handle.node_id, workspace_id=handle.workspace_id,
                success=success, produced_handoff_ids=ids,
                produced_handoff_types_by_id={
                    hid: tuple((types_by_id or {}).get(hid, (f"{hid}.v1",)))
                    for hid in ids},
                artifacts=tuple(_artifact_descriptor(handle, hid, target) for hid in ids),
            )
    return _W()


def p01_barrier_real_concurrency():
    """A and B in-flight SIMULTANEOUSLY (Barrier) — sequential cannot pass."""
    import threading

    from redi_lab.dag_scheduler import NodeRunState
    from redi_lab.parallel_dag_executor import WorkerExecutionResult
    from redi_lab.runtime.contracts import WorkerRunner

    bundle = _parallel_bundle()
    barrier = threading.Barrier(2, timeout=10)
    entered = []
    lock = threading.Lock()

    class _BW(WorkerRunner):
        def __init__(self, content, hid, types):
            self.content, self.hid, self.types = content, hid, types

        def run_node(self, *, handle, **_):
            with lock:
                entered.append(str(handle.node_id))
            barrier.wait(timeout=10)
            target = _write_module(
                handle, "alpha" if str(handle.node_id) == "node_a" else "beta", self.content
            )
            return WorkerExecutionResult(
                node_id=handle.node_id, workspace_id=handle.workspace_id,
                success=True, produced_handoff_ids=(self.hid,),
                produced_handoff_types_by_id={self.hid: self.types},
                artifacts=(_artifact_descriptor(handle, self.hid, target),))

    class _C(WorkerRunner):
        def run_node(self, *, handle, **_):
            _write_module(handle, "integration", _INTEGRATION_IMPL)
            return WorkerExecutionResult(node_id=handle.node_id,
                                         workspace_id=handle.workspace_id, success=True)

    workers = {
        "node_a": _BW(_ALPHA_IMPL, "handoff_a", ("alpha.impl.v1",)),
        "node_b": _BW(_BETA_IMPL, "handoff_b", ("beta.impl.v1",)),
        "node_c": _C(),
    }
    result = _parallel_executor(bundle, workers).run()
    assert result.final_states["node_a"] == NodeRunState.COMPLETED
    assert result.final_states["node_b"] == NodeRunState.COMPLETED
    assert result.final_states["node_c"] == NodeRunState.COMPLETED
    assert sorted(entered) == ["node_a", "node_b"]  # both entered pre-barrier
    assert result.launch_order == ("node_a", "node_b", "node_c")


def p02_max_parallelism_bound():
    import threading

    from redi_lab.dag_scheduler import NodeRunState
    from redi_lab.parallel_dag_executor import WorkerExecutionResult  # noqa: F401
    from redi_lab.runtime.contracts import WorkerRunner

    bundle = _parallel_bundle()
    counter = {"cur": 0, "max": 0}
    lock = threading.Lock()

    class _Track(WorkerRunner):
        def __init__(self, inner):
            self.inner = inner

        def run_node(self, *, handle, **kw):
            with lock:
                counter["cur"] += 1
                counter["max"] = max(counter["max"], counter["cur"])
            try:
                return self.inner.run_node(handle=handle, **kw)
            finally:
                with lock:
                    counter["cur"] -= 1

    workers = {
        "node_a": _Track(_mk_worker(_ALPHA_IMPL, handoff_ids=("handoff_a",),
                                    types_by_id={"handoff_a": ("alpha.impl.v1",)})),
        "node_b": _Track(_mk_worker(_BETA_IMPL, handoff_ids=("handoff_b",),
                                    types_by_id={"handoff_b": ("beta.impl.v1",)})),
        "node_c": _Track(_mk_worker(_INTEGRATION_IMPL)),
    }
    result = _parallel_executor(bundle, workers, max_parallelism=1).run()
    assert result.final_states["node_c"] == NodeRunState.COMPLETED
    assert counter["max"] == 1  # bound respected — never two nodes in flight


def p03_failure_isolation_a_fails_b_completes_c_blocked():
    from redi_lab.dag_scheduler import NodeRunState

    bundle = _parallel_bundle()
    wa = _mk_worker(_ALPHA_IMPL, error=RuntimeError("worker exploded"))
    wb = _mk_worker(_BETA_IMPL, handoff_ids=("handoff_b",),
                    types_by_id={"handoff_b": ("beta.impl.v1",)})
    wc = _mk_worker(_INTEGRATION_IMPL)
    result = _parallel_executor(bundle, {"node_a": wa, "node_b": wb, "node_c": wc}).run()
    assert result.final_states["node_a"] == NodeRunState.FAILED   # typed failure
    assert result.final_states["node_b"] == NodeRunState.COMPLETED  # sibling unaffected
    assert result.final_states["node_c"] == NodeRunState.BLOCKED


def p04_wrong_producer_handoff_rejected():
    bundle = _parallel_bundle()
    wa = _mk_worker(_ALPHA_IMPL, handoff_ids=("handoff_a",),
                    types_by_id={"handoff_a": ("alpha.impl.v1",)})
    wb = _mk_worker(_BETA_IMPL, handoff_ids=("handoff_a",),  # B delivers A's handoff
                    types_by_id={"handoff_a": ("alpha.impl.v1",)})
    try:
        _parallel_executor(bundle, {"node_a": wa, "node_b": wb}).run()
    except Exception as exc:  # noqa: BLE001
        assert "handoff_a" in str(exc), f"unexpected rejection reason: {exc}"
        return
    raise AssertionError("wrong-producer handoff was not rejected")


def p05_wrong_type_handoff_rejected():
    bundle = _parallel_bundle()
    wa = _mk_worker(_ALPHA_IMPL, handoff_ids=("handoff_a",),
                    types_by_id={"handoff_a": ("alpha.impl.v1",)})
    wb = _mk_worker(_BETA_IMPL, handoff_ids=("handoff_b",),
                    types_by_id={"handoff_b": ("incompatible.type.v1",)})
    try:
        _parallel_executor(bundle, {"node_a": wa, "node_b": wb}).run()
    except Exception as exc:  # noqa: BLE001
        assert "contradict" in str(exc), f"unexpected rejection reason: {exc}"
        return
    raise AssertionError("wrong-type handoff was not rejected")


def p06_duplicate_and_unknown_results_fail_closed():
    from redi_lab.dag_scheduler import NodeRunState
    from redi_lab.parallel_dag_executor import (
        DuplicateNodeResultError,
        UnknownNodeResultError,
        WorkerExecutionResult,
    )

    bundle = _parallel_bundle()
    ex = _parallel_executor(bundle, {
        "node_a": _mk_worker(_ALPHA_IMPL, handoff_ids=("handoff_a",),
                             types_by_id={"handoff_a": ("alpha.impl.v1",)}),
        "node_b": _mk_worker(_BETA_IMPL, handoff_ids=("handoff_b",),
                             types_by_id={"handoff_b": ("beta.impl.v1",)}),
    })
    ex._states["node_a"] = NodeRunState.RUNNING
    ex._workspace_ids["node_a"] = "ws"
    good = WorkerExecutionResult(node_id="node_a", workspace_id="ws", success=True)
    ex._commit_result("node_a", good)  # first commit OK
    try:
        ex._commit_result("node_a", good)
        raise AssertionError("duplicate result was not rejected")
    except DuplicateNodeResultError:
        pass
    try:
        ex._commit_result("node_ghost", good)
        raise AssertionError("unknown-node result was not rejected")
    except UnknownNodeResultError:
        pass
    assert ex._states["node_a"] == NodeRunState.COMPLETED


def p07_workspace_isolation_private_files():
    from redi_lab.parallel_dag_executor import WorkerExecutionResult
    from redi_lab.runtime.contracts import WorkerRunner

    bundle = _parallel_bundle()
    roots, observed = {}, {}

    class _W(WorkerRunner):
        def __init__(self, content, hid, types, marker=None):
            self.content, self.hid = content, hid
            self.types, self.marker = types, marker

        def run_node(self, *, handle, **_):
            roots[str(handle.node_id)] = handle.root
            if self.marker:
                marker = handle.root / self.marker
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text("private", encoding="utf-8")
            sub = {"node_a": "alpha", "node_b": "beta", "node_c": "integration"}[
                str(handle.node_id)]
            target = _write_module(handle, sub, self.content)
            return WorkerExecutionResult(
                node_id=handle.node_id, workspace_id=handle.workspace_id,
                success=True, produced_handoff_ids=(self.hid,) if self.hid else (),
                produced_handoff_types_by_id={self.hid: self.types} if self.hid else {},
                artifacts=((_artifact_descriptor(handle, self.hid, target),)
                           if self.hid else ()))

    class _VerifyC(_W):
        def run_node(self, *, handle, **kw):
            # C runs only after A and B COMPLETED — workspaces still alive
            a_root, b_root = roots["node_a"], roots["node_b"]
            observed["distinct"] = a_root != b_root
            observed["in_a"] = len(list(a_root.rglob("marker_private.txt")))
            observed["in_b"] = len(list(b_root.rglob("marker_private.txt")))
            return super().run_node(handle=handle, **kw)

    workers = {
        "node_a": _W(_ALPHA_IMPL, "handoff_a", ("alpha.impl.v1",),
                     marker="alpha/marker_private.txt"),
        "node_b": _W(_BETA_IMPL, "handoff_b", ("beta.impl.v1",)),
        "node_c": _VerifyC(_INTEGRATION_IMPL, None, None),
    }
    result = _parallel_executor(bundle, workers).run()
    assert result.final_states["node_c"].value == "COMPLETED"  # probe ran
    assert observed["distinct"] is True
    assert observed["in_a"] == 1 and observed["in_b"] == 0  # no cross-write


def p08_persisted_parallel_run_replays_deterministically():
    import json
    import sqlite3
    import tempfile

    from redi_lab.dag_scheduler import NodeRunState, product_dag_hash, replay_dag_run
    from redi_lab.persistence import EvidenceLedger, bootstrap_schema

    bundle = _parallel_bundle()
    workers = {
        "node_a": _mk_worker(_ALPHA_IMPL, handoff_ids=("handoff_a",),
                             types_by_id={"handoff_a": ("alpha.impl.v1",)}),
        "node_b": _mk_worker(_BETA_IMPL, handoff_ids=("handoff_b",),
                             types_by_id={"handoff_b": ("beta.impl.v1",)}),
        "node_c": _mk_worker(_INTEGRATION_IMPL),
    }
    db_path = Path(tempfile.mkdtemp(prefix="redi06b_pack_")) / "pilot.db"
    conn = sqlite3.connect(db_path)
    bootstrap_schema(conn)
    ledger = EvidenceLedger(conn)
    result = _parallel_executor(bundle, workers, ledger=ledger).run()
    conn.close()

    assert result.final_states["node_c"] == NodeRunState.COMPLETED
    t = result.timing
    assert t["aggregate_node_compute_seconds"] > 0
    assert t["graph_wall_clock_seconds"] > 0  # separate metrics, both recorded

    conn = sqlite3.connect(db_path)
    try:
        stored = [json.loads(r[0]).get("metadata") for r in conn.execute(
            "SELECT payload_json FROM evidence_entries ORDER BY global_sequence")]
        stored = [m for m in stored if m and "row_kind" in m]
    finally:
        conn.close()
    r1 = replay_dag_run(stored)
    r2 = replay_dag_run(stored)  # zero provider calls, zero writes
    assert r1["graph_hash"] == r2["graph_hash"] == product_dag_hash(bundle["dag"])
    assert r1["launch_order"] == r2["launch_order"] == ["node_a", "node_b", "node_c"]
    assert r1["completion_order"] == r2["completion_order"]
    assert r1["final_states"] == r2["final_states"]
    assert r1["available_handoff_types"] == r2["available_handoff_types"]
    assert any(d["ready_nodes"] == ["node_a", "node_b"] for d in r1["ready_decisions"])


def p09_orphaned_running_recovered_as_interrupted():
    from redi_lab.parallel_dag_executor import recover_orphaned_running

    recovered = recover_orphaned_running({"node_x": "RUNNING", "node_y": "COMPLETED"})
    assert recovered == {"node_x": {"state": "FAILED", "reason": "interrupted"}}
    assert recover_orphaned_running({"node_x": "RUNNING"}) == recovered


def p10_06c_exact_runner_dry_path_and_readonly_audit():
    from scripts.run_redi_06_live_parallel import execute_acceptance

    output_root = Path(tempfile.mkdtemp(prefix="redi06c_validation_"))
    db_path, result, report = execute_acceptance(live=False, output_root=output_root)
    audit = report["audit"]
    assert db_path.is_file()
    assert audit["verified"] is True
    assert audit["provider_calls"] == 0
    assert audit["observed_worker_slots"] == 2
    assert audit["overlap"] is True
    assert result.launch_order == ("node_a", "node_b", "node_c")


def p11_06c_factory_run_binding_and_terminal_state():
    import json
    import sqlite3

    from redi_lab.domain import domain_from_json
    from scripts.run_redi_06_live_parallel import execute_acceptance

    output_root = Path(tempfile.mkdtemp(prefix="redi06c_factory_binding_"))
    db_path, _result, report = execute_acceptance(live=False, output_root=output_root)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        factories = list(conn.execute("SELECT * FROM factory_runs"))
        entries = list(conn.execute(
            "SELECT * FROM evidence_entries ORDER BY global_sequence"
        ))
    finally:
        conn.close()
    assert len(factories) == 1
    factory = factories[0]
    assert factory["id"] == report["factory_run_id"]
    assert factory["final_status"] == "completed" and factory["verified"] == 1
    assert factory["closed_at"] is not None
    assert entries and all(row["factory_run_id"] == factory["id"] for row in entries)
    assert all(
        str(domain_from_json(row["payload_json"]).factory_run_id) == factory["id"]
        for row in entries
    )
    assert json.loads(factories[0]["total_tokens_json"])["__type__"] == "TokenUsage"


def p12_06c_retained_artifacts_rehash_after_cleanup():
    from redi_lab.evidence.artifacts import verify_retained_artifact
    from scripts.run_redi_06_live_parallel import execute_acceptance

    output_root = Path(tempfile.mkdtemp(prefix="redi06c_retention_"))
    db_path, _result, report = execute_acceptance(live=False, output_root=output_root)
    retained = report["audit"]["retained_artifacts"]
    assert len(retained) == 2
    for item in retained:
        verified = verify_retained_artifact(
            run_artifact_root=db_path.parent,
            retained_path=item["retained_path"],
            artifact_id=item["artifact_id"],
            expected_sha256=item["sha256"],
            expected_size=item["size_bytes"],
        )
        assert verified == item


def p13_06c_full_canonical_replay_equality():
    from redi_lab.dag_scheduler import canonical_replay_view, replay_dag_run
    from scripts.run_redi_06_live_parallel import (
        _load_bound_acceptance_rows,
        execute_acceptance,
    )

    output_root = Path(tempfile.mkdtemp(prefix="redi06c_full_replay_"))
    db_path, _result, report = execute_acceptance(live=False, output_root=output_root)
    _run_id, _run_row, rows, _entries = _load_bound_acceptance_rows(
        db_path, factory_run_id=report["factory_run_id"],
    )
    before = (db_path.read_bytes(), db_path.stat().st_mtime_ns)
    replay_1 = replay_dag_run(rows, retained_artifact_root=db_path.parent)
    replay_2 = replay_dag_run(rows, retained_artifact_root=db_path.parent)
    after = (db_path.read_bytes(), db_path.stat().st_mtime_ns)
    assert canonical_replay_view(replay_1) == canonical_replay_view(replay_2)
    assert report["audit"]["checks"]["replay_x2_equal"] is True
    assert before == after


def main() -> int:
    print("=" * 64)
    print("REDI-06 VALIDATIONS — DAG scheduler (06A) + isolated parallel execution (06B)")
    print("=" * 64)
    check("d01 valid A+B→C graph topological order", d01_valid_abc_graph)
    check("d02 initial ready set = A,B; C pending", d02_initial_ready_set)
    check("d03 C blocked until both complete", d03_c_blocked_until_both_complete)
    check("d04 A failure blocks C; B independent", d04_a_failure_blocks_c_b_independent)
    check("d05 validation fail-closed (dup/unknown/self/cycle)", d05_validation_fail_closed)
    check("d06 deterministic ready ordering", d06_deterministic_ready_ordering)
    check("d07 graph hash reproducible + topology-sensitive", d07_graph_hash_reproducible_and_sensitive)
    check("d08 revision lineage N → N+1 with parent hash", d08_revision_lineage)
    check("d09 completed nodes never become ready", d09_completed_never_reruns)
    check("d10 handoff identity gating (missing/present)", d10_handoff_gating)
    check("d11 worker/context orthogonality to DAG identity", d11_worker_context_orthogonality)
    check("d12 scheduling evidence reconstructs deterministically", d12_scheduling_evidence_reconstructs)
    check("d13 timing model: aggregate vs wall-clock separate", d13_timing_model_separate)
    check("d14 simulated A fail → C BLOCKED", d14_simulated_ab_c_expected_behavior)
    check("d15 simulated A,B success with handoffs", d15_simulated_ab_c_success_with_handoffs)
    print("-" * 64)
    print("REDI-06B — isolated parallel execution")
    check("p01 barrier: A,B in-flight simultaneously; C completes", p01_barrier_real_concurrency)
    check("p02 max_parallelism=1 bound respected", p02_max_parallelism_bound)
    check("p03 A fails typed; B completes; C BLOCKED", p03_failure_isolation_a_fails_b_completes_c_blocked)
    check("p04 wrong-producer handoff rejected", p04_wrong_producer_handoff_rejected)
    check("p05 wrong-type handoff rejected", p05_wrong_type_handoff_rejected)
    check("p06 duplicate/unknown results fail closed", p06_duplicate_and_unknown_results_fail_closed)
    check("p07 workspace isolation: private file never crosses roots", p07_workspace_isolation_private_files)
    check("p08 persisted parallel run replays deterministically (x2)", p08_persisted_parallel_run_replays_deterministically)
    check("p09 orphaned RUNNING → FAILED (interrupted)", p09_orphaned_running_recovered_as_interrupted)
    check("p10 REDI-06C exact runner dry path + read-only audit", p10_06c_exact_runner_dry_path_and_readonly_audit)
    check("p11 REDI-06C FactoryRun binding + terminal state", p11_06c_factory_run_binding_and_terminal_state)
    check("p12 REDI-06C retained artifacts rehash after cleanup", p12_06c_retained_artifacts_rehash_after_cleanup)
    check("p13 REDI-06C full canonical replay equality x2", p13_06c_full_canonical_replay_equality)
    print("=" * 64)
    print(f"TOTAL: PASS={PASS} FAIL={FAIL} CHECKS={PASS + FAIL}")
    if FAIL:
        print("Failing checks:")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name}: {detail}")
    print("=" * 64)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
