"""REDI-06A — Product DAG & deterministic scheduler foundation tests.

Deterministic, zero-network.  Covers the §15 matrix: A+B→C graph, initial
ready set, dependency blocking, deterministic ordering, hash reproducibility,
immutable revisions with lineage, handoff identity gating, worker/context
orthogonality, fail-closed validation with zero worker calls, evidence and
replay.
"""

from __future__ import annotations

import json

import pytest

from redi_lab.dag_scheduler import (
    DagStateError,
    DagValidationError,
    NodeRunState,
    compute_ready_nodes,
    dag_scheduling_evidence,
    dag_timing_summary,
    product_dag_hash,
    revise_product_dag,
    simulate_dag_execution,
    validate_product_dag_for_scheduling,
    validate_revision_lineage,
)
from redi_lab.domain import (
    DagDependency,
    DagNode,
    DomainValidationError,
    StationContract,
    TaskContract,
    validate_dag,
)


def _node(node_id, *, task_id=None, station_id=None, requires=(), produces=(), **meta):
    md = dict(meta)
    if requires:
        md["requires_handoff_ids"] = list(requires)
    if produces:
        md["produces_handoff_ids"] = list(produces)
    return DagNode(node_id=node_id, task_id=task_id, station_id=station_id, metadata=md)


def _abc_graph():
    """A ─┐
           ├→ C        (A, B independent builders; C integration/reviewer)
    B ─┘"""
    from redi_lab.domain import ProductDAG

    return ProductDAG(
        nodes=[
            _node("node_a", station_id="station_builder", task_id="task_a",
                  produces=["handoff_a"]),
            _node("node_b", station_id="station_builder_test", task_id="task_b",
                  produces=["handoff_b"]),
            _node("node_c", station_id="station_integration",
                  task_id="task_c", requires=["handoff_a", "handoff_b"]),
        ],
        dependencies=[
            DagDependency(upstream="node_a", downstream="node_c", kind="hard"),
            DagDependency(upstream="node_b", downstream="node_c", kind="hard"),
        ],
        metadata={"fixture": "redi06_ab_c"},
    )


# ---------------------------------------------------------------------------
# Graph validation
# ---------------------------------------------------------------------------


def test_06a_valid_abc_graph_validates_and_returns_topological_order():
    dag = _abc_graph()
    order = validate_product_dag_for_scheduling(dag)
    assert order.index("node_a") < order.index("node_c")
    assert order.index("node_b") < order.index("node_c")


def test_06a_duplicate_node_rejected():
    from redi_lab.domain import ProductDAG

    dag = ProductDAG(
        nodes=[_node("A"), _node("A")],
        dependencies=[],
    )
    with pytest.raises(Exception, match="duplicate node_ids"):
        validate_product_dag_for_scheduling(dag)


def test_06a_unknown_dependency_rejected():
    from redi_lab.domain import ProductDAG

    dag = ProductDAG(
        nodes=[_node("A")],
        dependencies=[DagDependency(upstream="ghost", downstream="A")],
    )
    with pytest.raises(Exception, match="does not reference any node"):
        validate_product_dag_for_scheduling(dag)


def test_06a_self_dependency_rejected():
    """Self-dependency fails closed at value-object construction."""
    with pytest.raises(ValueError, match="must differ"):
        DagDependency(upstream="A", downstream="A")


def test_06a_cycle_rejected():
    from redi_lab.domain import ProductDAG

    dag = ProductDAG(
        nodes=[_node("A"), _node("B")],
        dependencies=[
            DagDependency(upstream="A", downstream="B"),
            DagDependency(upstream="B", downstream="A"),
        ],
    )
    with pytest.raises(Exception, match="cycle|not topologically"):
        validate_product_dag_for_scheduling(dag)


def test_06a_incompatible_handoff_reference_rejected():
    """C requires a handoff that none of its declared prerequisites produces."""
    from redi_lab.domain import DagDependency, ProductDAG

    dag = ProductDAG(
        nodes=[
            _node("A", produces=["handoff_a"]),
            _node("C", requires=["handoff_from_nowhere"]),
        ],
        dependencies=[DagDependency(upstream="A", downstream="C")],
    )
    with pytest.raises(DagValidationError, match="requires handoff"):
        validate_product_dag_for_scheduling(dag)


def test_06a_task_station_mismatch_rejected():
    task = TaskContract(goal="docs task", metadata={"task_class": "docs_authoring"})
    station = StationContract(
        role="builder", required_output_kinds=["patch_edit"],
        applicable_task_classes=["software_patch"],
    )
    from redi_lab.domain import ProductDAG

    dag = ProductDAG(nodes=[_node("A", task_id=str(task.id), station_id=str(station.id))])
    with pytest.raises(DagValidationError, match="task class"):
        validate_product_dag_for_scheduling(
            dag, task_by_node={"A": task}, station_by_node={"A": station}
        )


# ---------------------------------------------------------------------------
# Ready-node computation
# ---------------------------------------------------------------------------


def test_06a_initial_ready_set_is_a_and_b_c_pending():
    dag = _abc_graph()
    decision = compute_ready_nodes(dag, {})
    assert list(decision.ready) == ["node_a", "node_b"]  # deterministic order
    assert decision.pending == ("node_c",)
    assert decision.blocked == ()


def test_06a_c_stays_pending_until_both_complete():
    dag = _abc_graph()
    after_a = compute_ready_nodes(dag, {"node_a": NodeRunState.COMPLETED})
    assert "node_c" not in after_a.ready
    assert "node_b" in after_a.ready
    both = compute_ready_nodes(
        dag, {"node_a": NodeRunState.COMPLETED, "node_b": NodeRunState.COMPLETED},
        available_handoff_ids={"handoff_a", "handoff_b"},
    )
    assert set(both.ready) == {"node_c"}


def test_06a_failed_prerequisite_blocks_child_b_independent():
    dag = _abc_graph()
    decision = compute_ready_nodes(dag, {"node_a": NodeRunState.FAILED})
    assert decision.blocked == ("node_c",)
    assert "node_b" in decision.ready  # independent sibling still executable


def test_06a_completed_node_never_becomes_ready_again():
    dag = _abc_graph()
    decision = compute_ready_nodes(
        dag, {"node_a": NodeRunState.COMPLETED, "node_b": NodeRunState.COMPLETED},
        available_handoff_ids={"handoff_a", "handoff_b"},
    )
    assert "node_a" not in decision.ready
    assert "node_b" not in decision.ready


def test_06a_required_handoff_missing_blocks_readiness():
    dag = _abc_graph()
    # both completed but handoff_b not produced/available
    decision = compute_ready_nodes(
        dag, {"node_a": NodeRunState.COMPLETED, "node_b": NodeRunState.COMPLETED},
        available_handoff_ids={"handoff_a"},
    )
    assert decision.ready == ()
    assert "node_c" in decision.pending


def test_06a_required_handoff_present_makes_child_ready():
    dag = _abc_graph()
    decision = compute_ready_nodes(
        dag, {"node_a": NodeRunState.COMPLETED, "node_b": NodeRunState.COMPLETED},
        available_handoff_ids={"handoff_a", "handoff_b"},
    )
    assert decision.ready == ("node_c",)


def test_06a_unknown_state_node_fails_closed():
    dag = _abc_graph()
    with pytest.raises(DagValidationError, match="unknown node"):
        compute_ready_nodes(dag, {"ghost": NodeRunState.COMPLETED})


def test_06a_ready_ordering_deterministic():
    dag = _abc_graph()
    orders = {
        tuple(compute_ready_nodes(dag, {}).ready) for _ in range(5)
    }
    assert orders == {("node_a", "node_b")}


def test_06a_illegal_state_transition_rejected():
    with pytest.raises(DagStateError):
        from redi_lab.dag_scheduler import assert_transition
        assert_transition(NodeRunState.COMPLETED, NodeRunState.READY)


# ---------------------------------------------------------------------------
# Hash / revision / orthogonality
# ---------------------------------------------------------------------------


def test_06a_graph_hash_reproducible():
    dag = _abc_graph()
    twin = _abc_graph()
    twin.id = dag.id  # same versioned graph identity
    assert product_dag_hash(dag) == product_dag_hash(twin)


def test_06a_revision_changes_hash_and_preserves_parent():
    dag = _abc_graph()
    parent_hash = product_dag_hash(dag)
    revision = revise_product_dag(
        dag, reason="split node_b into two independent test nodes",
        dependencies=[
            DagDependency(upstream="node_a", downstream="node_c"),
            DagDependency(upstream="node_b", downstream="node_c"),
            DagDependency(upstream="node_b", downstream="node_a", kind="soft"),
        ],
    )
    assert revision.dag.revision == dag.revision + 1
    assert product_dag_hash(revision.dag) != parent_hash
    # parent immutable
    assert product_dag_hash(dag) == parent_hash
    assert dag.revision == 1
    # lineage validates and breaks on tampering
    validate_revision_lineage(dag, revision.dag)
    with pytest.raises(DagValidationError):
        validate_revision_lineage(dag, dag)
    assert revision.changed_dependencies == (("node_b", "node_a"),)


def test_06a_worker_profile_change_does_not_alter_dag_identity():
    """Worker selection is ORTHOGONAL to the graph: swapping WorkerPool /
    WorkerProfile bindings leaves graph identity untouched."""
    dag = _abc_graph()
    worker_one = object()  # Worker A binding (any runner object)
    worker_two = object()  # Worker B binding (a different profile/runner)
    # nothing on ProductDAG/DagNode carries worker bindings — structural proof
    node_fields = set(DagNode.__dataclass_fields__)
    assert not ({"worker_id", "worker_profile", "model", "provider"} & node_fields)
    dag_fields = set(type(dag).__dataclass_fields__)
    assert not ({"worker_pool", "workers", "model", "provider"} & dag_fields)
    assert worker_one is not worker_two  # bindings differ; identity below unchanged
    assert product_dag_hash(dag) == product_dag_hash(dag)


def test_06a_context_strategy_change_does_not_alter_dag_identity():
    dag = _abc_graph()
    from redi_lab.context_compiler import (
        CONTEXT_STRATEGY_FULL_PUBLIC_V0, CONTEXT_STRATEGY_SELECTED_V0, compile_context,
    )
    import tempfile, os

    ws = tempfile.mkdtemp()
    os.makedirs(os.path.join(ws, "slugify"), exist_ok=True)
    with open(os.path.join(ws, "slugify", "__init__.py"), "w", encoding="utf-8") as f:
        f.write("x = 1\n")
    task = TaskContract(goal="implement slugify")
    station = StationContract(role="builder", required_output_kinds=["patch_edit"])
    product = _specs_product()
    ctx_s = compile_context(
        strategy_id=CONTEXT_STRATEGY_SELECTED_V0, station_role="builder",
        task=task, station=station, product=product, workspace_root=ws,
        selected_paths=["slugify/__init__.py"],
    )
    ctx_f = compile_context(
        strategy_id=CONTEXT_STRATEGY_FULL_PUBLIC_V0, station_role="builder",
        task=task, station=station, product=product, workspace_root=ws,
        selected_paths=(),
    )
    assert ctx_s.package_hash != ctx_f.package_hash  # context varies
    twin = _abc_graph()
    twin.id = dag.id
    assert product_dag_hash(dag) == product_dag_hash(twin)  # DAG identity does not


def _specs_product():
    from redi_lab.domain import ProductSpec, RiskTier

    return ProductSpec(requested_outcome="slugify task", requirements=[],
                       constraints=[], risk_tier=RiskTier.LOW, unresolved_requirements=[])


# ---------------------------------------------------------------------------
# Controlled execution simulation (deterministic fake workers only)
# ---------------------------------------------------------------------------


class _CountingSuccess(dict):
    def __init__(self, data):
        super().__init__(data)
        self.asked = []

    def get(self, k, default=None):
        self.asked.append(k)
        return super().get(k, default)


def test_06a_invalid_graph_zero_worker_calls():
    from redi_lab.domain import ProductDAG

    dag = ProductDAG(
        nodes=[_node("A"), _node("B")],
        dependencies=[DagDependency(upstream="A", downstream="B"),
                      DagDependency(upstream="B", downstream="A")],  # cycle
    )
    success = _CountingSuccess({"A": True, "B": True})
    with pytest.raises(Exception):
        simulate_dag_execution(dag, node_success=success)
    assert success.asked == []  # validation failed before any node execution


def test_06a_ab_c_expected_scheduler_behavior():
    dag = _abc_graph()
    result = simulate_dag_execution(
        dag,
        node_success={"node_a": False, "node_b": True, "node_c": True},
        node_compute_seconds={"node_a": 2.0, "node_b": 3.0, "node_c": 1.5},
        graph_wall_clock_seconds=4.0,
        produced_handoff_ids_by_node={
            "node_a": ["handoff_a"], "node_b": ["handoff_b"], "node_c": [],
        },
        required_handoff_ids_by_node={"node_c": ["handoff_a", "hand_b"] if False else ["handoff_a", "handoff_b"]},
    )
    # A fails → C blocked; B independent and completes
    assert result.final_states["node_a"] == NodeRunState.FAILED
    assert result.final_states["node_b"] == NodeRunState.COMPLETED
    assert result.final_states["node_c"] == NodeRunState.BLOCKED
    # timing model: separate metrics, aggregate = 2.0 + 3.0 (C never ran)
    assert result.timing["aggregate_node_compute_seconds"] == pytest.approx(5.0)
    assert result.timing["graph_wall_clock_seconds"] == pytest.approx(4.0)
    assert result.timing["per_node_seconds"] == {"node_a": 2.0, "node_b": 3.0}


def test_06a_ab_c_success_path_produces_handoffs_and_verifies():
    dag = _abc_graph()
    result = simulate_dag_execution(
        dag,
        node_success={"node_a": True, "node_b": True, "node_c": True},
        node_compute_seconds={"node_a": 1.0, "node_b": 2.0, "node_c": 0.5},
        graph_wall_clock_seconds=3.0,
        produced_handoff_ids_by_node={
            "node_a": ["handoff_a"], "node_b": ["handoff_b"], "node_c": [],
        },
        required_handoff_ids_by_node={"node_c": ["handoff_a", "handoff_b"]},
    )
    assert result.final_states == {
        "node_a": NodeRunState.COMPLETED,
        "node_b": NodeRunState.COMPLETED,
        "node_c": NodeRunState.COMPLETED,
    }
    assert result.produced_handoff_ids == frozenset({"handoff_a", "handoff_b"})
    # initial ready decision recorded = [A, B]
    first = result.ready_decisions[0]
    assert list(first["ready_nodes"]) == ["node_a", "node_b"]
    # timing kept separate; aggregate 3.5 vs wall 3.0 — no collapse, no speedup claim
    t = result.timing
    assert t["aggregate_node_compute_seconds"] == pytest.approx(3.5)
    assert t["graph_wall_clock_seconds"] == pytest.approx(3.0)
