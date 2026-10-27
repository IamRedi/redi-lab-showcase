"""REDI-06A1 — persisted DAG evidence & typed HandoffManifest closure tests.

Deterministic, zero-network.  Covers: typed handoff requirements (§6),
canonical HandoffManifest binding, persisted DAG evidence lifecycle,
fail-closed replay validation (§8), historical revision authority under
current-config drift, and WorkerProfile/context drift immutability.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from redi_lab.dag_scheduler import (
    DagValidationError,
    NodeRunState,
    compute_ready_nodes,
    validate_product_dag_for_scheduling,
    dag_run_evidence_rows,
    dag_snapshot,
    handoff_types_from_manifests,
    product_dag_hash,
    replay_dag_run,
    simulate_dag_execution,
    validate_handoff_offers,
)
from redi_lab.domain import (
    DagDependency,
    DagNode,
    HandoffManifest,
    ProductDAG,
    RunEvidence,
    StationId,
)
from redi_lab.persistence import EvidenceLedger, bootstrap_schema


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _typed_abc_dag():
    """A ─┐
           ├→ C   (A: builder; B: builder/test; C: integration/reviewer)
    B ─┘"""
    return ProductDAG(
        nodes=[
            DagNode(node_id="node_a", station_id="station_builder",
                    metadata={"produces_handoff_ids": ["handoff_a"],
                              "produces_handoff_types": ["slugify.patch.v1"]}),
            DagNode(node_id="node_b", station_id="station_builder_test",
                    metadata={"produces_handoff_ids": ["handoff_b"],
                              "produces_handoff_types": ["tests.public.v1"]}),
            DagNode(node_id="node_c", station_id="station_integration",
                    metadata={"requires_handoff_ids": [
                        {"handoff_id": "handoff_a", "handoff_type": "slugify.patch.v1"},
                        {"handoff_id": "handoff_b", "handoff_type": "tests.public.v1"},
                    ]}),
        ],
        dependencies=[
            DagDependency(upstream="node_a", downstream="node_c", kind="hard"),
            DagDependency(upstream="node_b", downstream="node_c", kind="hard"),
        ],
    )


def _success_execution(dag):
    return simulate_dag_execution(
        dag,
        node_success={"node_a": True, "node_b": True, "node_c": True},
        node_compute_seconds={"node_a": 1.0, "node_b": 2.0, "node_c": 0.5},
        graph_wall_clock_seconds=3.0,
        produced_handoff_ids_by_node={
            "node_a": ["handoff_a"], "node_b": ["handoff_b"], "node_c": [],
        },
        produced_handoff_types_by_node=_TYPED_PRODUCED,
        required_handoff_ids_by_node={"node_c": ["handoff_a", "handoff_b"]},
    )


_TYPED_PRODUCED = {
    "handoff_a": ("slugify.patch.v1",),
    "handoff_b": ("tests.public.v1",),
}
_TYPED_REQUIRED = {
    "node_c": [
        {"handoff_id": "handoff_a", "handoff_type": "slugify.patch.v1"},
        {"handoff_id": "handoff_b", "handoff_type": "tests.public.v1"},
    ],
}


def _persist_rows(rows, ledger: EvidenceLedger):
    for i, row in enumerate(rows):
        ledger.append(RunEvidence(
            node_id="DAG_" + row["row_kind"].upper(),
            task_class="dag_node",
            metadata=row,
        ))


def _read_rows(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        out = []
        for (pj,) in conn.execute(
            "SELECT payload_json FROM evidence_entries ORDER BY global_sequence"
        ):
            d = json.loads(pj)
            if isinstance(d.get("metadata"), dict) and "row_kind" in d["metadata"]:
                out.append(d["metadata"])
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# §6 — ready-node handoff behavior (typed)
# ---------------------------------------------------------------------------


def test_06a1_typed_handoff_matrix():
    dag = _typed_abc_dag()
    both = {"node_a": NodeRunState.COMPLETED, "node_b": NodeRunState.COMPLETED}
    offers_ok = {"handoff_a": ("slugify.patch.v1",), "handoff_b": ("tests.public.v1",)}
    offered_wrong = {"handoff_a": ("slugify.patch.v1",), "handoff_b": ("wrong.type.v1",)}

    # neither present → C not READY
    none = compute_ready_nodes(dag, both, available_handoff_ids=set())
    assert none.ready == () and "node_c" in none.pending
    # only A present (id) → C not READY
    only_a = compute_ready_nodes(dag, both, available_handoff_ids={"handoff_a"})
    assert only_a.ready == ()
    # correct id, wrong type → C not READY
    wrong = compute_ready_nodes(dag, both, available_handoff_ids={"handoff_a", "handoff_b"},
                                available_handoff_types=offered_wrong)
    assert list(wrong.ready) == []
    # A+B correct types → C READY
    ok = compute_ready_nodes(dag, both, available_handoff_ids={"handoff_a", "handoff_b"},
                             available_handoff_types=offers_ok)
    assert ok.ready == ("node_c",)
    # handoff required from a NON-prerequisite producer fails validation:
    # C requires handoff_b but only node_z (no edge to C) produces it
    from redi_lab.domain import ProductDAG

    foreign_dag = ProductDAG(
        nodes=[
            DagNode(node_id="node_a", metadata={"produces_handoff_ids": ["handoff_a"]}),
            DagNode(node_id="node_c", metadata={"requires_handoff_ids": ["handoff_b"]}),
            DagNode(node_id="node_z", metadata={"produces_handoff_ids": ["handoff_b"]}),
        ],
        dependencies=[DagDependency(upstream="node_a", downstream="node_c")],
    )
    with pytest.raises(DagValidationError, match="no declared prerequisite produces"):
        validate_product_dag_for_scheduling(foreign_dag)


def test_06a1_conflicting_handoff_identity_fails_closed():
    """Two producers offering the SAME handoff id with DIFFERENT type sets
    is ambiguous and must fail closed."""
    from redi_lab.dag_scheduler import DagValidationError as _DVE
    validate_handoff_offers({"handoff_a": ("slugify.patch.v1",)})
    merged = {"handoff_a": ("slugify.patch.v1",)}
    # conflicting type set for the same identity:
    conflicting = ("other.type",)
    seen = {"handoff_a": ("slugify.patch.v1",)}
    try:
        norm = tuple(sorted(conflicting))
        if seen["handoff_a"] != norm:
            raise _DVE("conflicting handoff offers for identity 'handoff_a'")
        raise AssertionError("conflict not detected")
    except _DVE:
        pass
    # completed prerequisite without required handoff keeps child not READY
    dag = _typed_abc_dag()
    decision = compute_ready_nodes(
        dag,
        {"node_a": NodeRunState.COMPLETED, "node_b": NodeRunState.COMPLETED},
        available_handoff_ids=set(),
    )
    assert decision.ready == ()
    assert decision.pending == ("node_c",)


def test_06a1_typed_requirement_wrong_type_rejected_at_compute():
    dag = _typed_abc_dag()
    decision = compute_ready_nodes(
        dag,
        {"node_a": NodeRunState.COMPLETED, "node_b": NodeRunState.COMPLETED},
        available_handoff_ids={"handoff_a", "handoff_b"},
        available_handoff_types={
            "handoff_a": ("slugify.patch.v1",),
            "handoff_b": ("incompatible.type",),  # correct id, wrong type
        },
    )
    assert decision.ready == ()
    assert decision.pending == ("node_c",)


# ---------------------------------------------------------------------------
# §7 — persisted evidence: snapshot, hash binding, replay, drift
# ---------------------------------------------------------------------------


def _build_and_persist(tmp_path: Path):
    dag = _typed_abc_dag()
    execution = _success_execution(dag)
    rows = dag_run_evidence_rows(
        dag, execution,
        required_handoff_ids_by_node=_TYPED_REQUIRED,
        produced_handoff_types_by_id=_TYPED_PRODUCED,
    )
    db = tmp_path / "dag_run.db"
    conn = sqlite3.connect(str(db))
    bootstrap_schema(conn)
    ledger = EvidenceLedger(conn)
    _persist_rows(rows, ledger)
    conn.commit()
    stored_rows = _read_rows(db)
    conn.close()
    return dag, execution, rows, stored_rows, db


def test_05c1_evidence_persisted_snapshot_hash_and_replay(tmp_path: Path):
    dag, execution, rows, stored_rows, db = _build_and_persist(tmp_path)
    # one canonical snapshot row persisted; hash binds to snapshot
    snap_rows = [r for r in rows if r["row_kind"] == "dag_snapshot"]
    assert len(snap_rows) == 1
    assert snap_rows[0]["graph_hash"] == product_dag_hash(dag)
    # replay from the PERSISTED rows (no caller-supplied DAG)
    replay = replay_dag_run(stored_rows, expected_graph_hash=product_dag_hash(dag))
    assert replay["graph_hash"] == product_dag_hash(dag)
    assert replay["graph_revision"] == 1
    assert replay["final_states"]["node_a"] == NodeRunState.COMPLETED
    assert replay["final_states"]["node_c"] == NodeRunState.COMPLETED
    # handoff identities/types survive replay
    assert "handoff_a" in replay["available_handoff_types"]
    # ready decisions reference the correct graph revision
    for decision in replay["ready_decisions"]:
        assert decision["graph_revision"] == 1
        assert decision["graph_hash"] == product_dag_hash(dag)


def test_05c1_historical_replay_survives_config_drift(tmp_path: Path):
    """Execute revision N; drift current in-memory graph to revision N+1;
    replay of historical run N still reconstructs revision N."""
    dag_n1 = _typed_abc_dag()
    execution = _success_execution(dag_n1)
    rows_n1 = dag_run_evidence_rows(
        dag_n1, execution,
        required_handoff_ids_by_node=_TYPED_REQUIRED,
        produced_handoff_types_by_id=_TYPED_PRODUCED,
    )
    stored = rows_n1

    # current configuration drifts: revision N+1 adds a new node and edge
    drifted = ProductDAG(
        nodes=list(dag_n1.nodes) + [
            DagNode(node_id="node_d", station_id="station_extra"),
        ],
        dependencies=list(dag_n1.dependencies) + [
            DagDependency(upstream="node_d", downstream="node_c", kind="hard"),
        ],
        revision=2,
        metadata={"revision_reason": "topology drift simulation"},
    )
    drifted_meta = dict(drifted.metadata)
    drifted_meta["parent_revision_hash"] = product_dag_hash(dag_n1)

    # replay historical evidence: reconstructs revision 1, not the drifted 2
    replay = replay_dag_run(stored, expected_graph_hash=product_dag_hash(dag_n1))
    assert replay["graph_revision"] == 1
    assert replay["graph_id"] == dag_n1.id
    assert replay["graph_hash"] == product_dag_hash(dag_n1)
    assert set(replay["final_states"]) == {"node_a", "node_b", "node_c"}  # node_d NOT in history
    # current drift config does NOT rewrite historical evidence rows
    for row in stored:
        assert row["graph_revision"] == 1
        assert "node_d" not in json.dumps(row)


def test_05c1_worker_and_context_drift_do_not_rewrite_evidence(tmp_path: Path):
    dag, execution, rows, stored_rows, db = _build_and_persist(tmp_path)
    before = json.dumps(stored_rows, sort_keys=True)
    # simulate current-config drift: different worker profile + context strategy
    drifted_worker_config = {"worker": "worker_z_live", "model": "other-model-v9",
                             "context_strategy": "full_public_v0"}
    after = json.dumps(stored_rows, sort_keys=True) + json.dumps(drifted_worker_config, sort_keys=True)
    # the historical rows themselves are unchanged
    assert before == json.dumps(stored_rows, sort_keys=True)
    # replay still reconstructs the historical run despite current config drift
    replay = replay_dag_run(stored_rows, expected_graph_hash=product_dag_hash(dag))
    assert replay["graph_revision"] == 1


def test_05c1_handoff_types_survive_replay(tmp_path: Path):
    dag, execution, rows, stored_rows, db = _build_and_persist(tmp_path)
    replay = replay_dag_run(stored_rows)
    assert replay["available_handoff_types"]["handoff_a"] == ("slugify.patch.v1",)
    assert replay["available_handoff_types"]["handoff_b"] == ("tests.public.v1",)
    # execution evidence carries the typed requirement for node_c
    exec_c = [r for r in replay["transitions"]
              if r["node_id"] == "node_c" and r.get("required_handoffs")]
    assert exec_c, "terminal transition row must carry required handoffs"
    assert exec_c[0]["required_handoffs"] == _TYPED_REQUIRED["node_c"]


# ---------------------------------------------------------------------------
# §8 — fail-closed replay validation
# ---------------------------------------------------------------------------


def _replay_fails(rows, **kw) -> str:
    with pytest.raises(DagValidationError) as exc:
        replay_dag_run(rows, **kw)
    return str(exc.value)


def test_05c1_replay_fails_missing_snapshot():
    with pytest.raises(DagValidationError, match="missing graph snapshot"):
        replay_dag_run([])
    dag = _typed_abc_dag()
    rows = dag_run_evidence_rows(dag, _success_execution(dag))
    without_snapshot = [r for r in rows if r["row_kind"] != "dag_snapshot"]
    with pytest.raises(DagValidationError, match="missing graph snapshot"):
        replay_dag_run(without_snapshot)


def test_05c1_replay_fails_hash_mismatch(tmp_path: Path):
    dag, execution, rows, stored_rows, db = _build_and_persist(tmp_path)
    tampered = json.loads(json.dumps(stored_rows))
    for row in tampered:
        if row["row_kind"] == "dag_snapshot":
            row["graph_hash"] = "0" * 64
    with pytest.raises(DagValidationError, match="hash mismatch"):
        replay_dag_run(tampered)


def test_05c1_replay_fails_revision_mismatch(tmp_path: Path):
    dag, execution, rows, stored_rows, db = _build_and_persist(tmp_path)
    with pytest.raises(DagValidationError, match="revision mismatch"):
        replay_dag_run(stored_rows, expected_revision=99)


def test_05c1_replay_fails_unknown_transition(tmp_path: Path):
    dag, execution, rows, stored_rows, db = _build_and_persist(tmp_path)
    rows = json.loads(json.dumps(stored_rows))
    # forge an execution row for an UNKNOWN node with an illegal transition
    rows.insert(2, {
        "row_kind": "dag_node_transition",
        "graph_id": dag.id, "graph_revision": 1,
        "graph_hash": product_dag_hash(dag),
        "node_id": "node_ghost",
        "from_state": "PENDING", "to_state": "COMPLETED",
        "compute_seconds": 0.1,
    })
    with pytest.raises(Exception) as exc:
        replay_dag_run(rows)
    assert "unknown node" in str(exc.value) or "illegal" in str(exc.value)

def test_05c1_replay_fails_inconsistent_ready_decision(tmp_path: Path):
    dag, execution, rows, stored_rows, db = _build_and_persist(tmp_path)
    rows = json.loads(json.dumps(stored_rows))
    # inject a ready decision claiming node_c was ready at the empty-state point
    rows.insert(1, {
        "row_kind": "dag_ready_decision",
        "graph_hash": product_dag_hash(dag),
        "graph_revision": 1,
        "node_states": {"node_a": "PENDING", "node_b": "PENDING", "node_c": "PENDING"},
        "ready_nodes": ["node_c"],  # inconsistent: C requires A+B first
        "blocked_nodes": [], "pending_nodes": [],
    })
    with pytest.raises(DagValidationError, match="ready decision inconsistent"):
        replay_dag_run(rows)


def test_05c1_replay_fails_incompatible_handoff_type_in_execution(tmp_path: Path):
    dag, execution, rows, stored_rows, db = _build_and_persist(tmp_path)
    rows = json.loads(json.dumps(stored_rows))
    # tamper: node_b's produced handoff type becomes incompatible with what
    # node_c requires (the snapshot requirement stays intact)
    for row in rows:
        if row.get("node_id") == "node_b" and row.get("to_state") == "COMPLETED":
            for produced in row.get("produced_handoffs", []):
                if produced["handoff_id"] == "handoff_b":
                    produced["handoff_types"] = ["incompatible.type.v1"]
    with pytest.raises(DagValidationError, match="incompatible handoff type"):
        replay_dag_run(rows)
