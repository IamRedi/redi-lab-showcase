"""Shared deterministic REDI-02 test fixture builder.

Builds a complete, reproducible FactoryRun + ExperimentManifest + events/evidence
fixture suitable for both pytest and stdlib test runners.

All timestamps / ids are deterministic (fixed values or deterministic construction)
so that canonical hashes are reproducible across runs.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from redi_lab.domain import (
    SCHEMA_VERSION,
    AcceptanceCriterion,
    DagDependency,
    DagNode,
    ExperimentId,
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
    MonetaryCost,
    ProductClass,
    ProductClassId,
    ProductDAG,
    ProductId,
    ProductSpec,
    Requirement,
    RunEvidence,
    RunStatus,
    StationContract,
    StationId,
    TaskContract,
    TaskId,
    Timing,
    TokenUsage,
    WorkerId,
    WorkerProfile,
    domain_to_json,
    new_id,
)
from redi_lab.persistence import (
    EvidenceLedger,
    ExperimentRepository,
    FactoryRunRepository,
    bootstrap_schema,
)
from redi_lab.evidence import (
    append_event,
    append_gate_result,
    append_run_evidence,
)


_T0 = datetime(2026, 9, 15, 12, 0, 0, 0, tzinfo=timezone.utc)
_TS_FIXED = _T0.isoformat()


def _ts(offset_seconds: int) -> datetime:
    return datetime.fromtimestamp(_T0.timestamp() + offset_seconds, tz=timezone.utc)


def build_small_product_graph() -> dict[str, Any]:
    """Construct small spec + class + 3-node DAG A→B→C + 3 tasks + station + worker.

    Returns dict with all components keyed by name.
    """
    spec_id = ProductId(new_id("product"))
    class_id = ProductClassId(new_id("product_class"))
    station_id = StationId(new_id("station"))
    task_a_id = TaskId(new_id("task"))
    task_b_id = TaskId(new_id("task"))
    task_c_id = TaskId(new_id("task"))
    worker_id = WorkerId(new_id("worker"))

    spec = ProductSpec(
        id=spec_id,
        schema_version=SCHEMA_VERSION,
        requested_outcome="Deterministic 3-node linear DAG calculator — validate input, compute result, format output",
        requirements=(
            Requirement(text="Input must be non-empty string", kind="functional"),
            Requirement(text="Compute must be deterministic", kind="non_functional"),
        ),
        acceptance_criteria=(
            AcceptanceCriterion(description="output length > 0", criterion_type="deterministic"),
        ),
        metadata={"fixture": True},
    )

    cls = ProductClass(
        id=class_id,
        schema_version=SCHEMA_VERSION,
        name="pilot",
        description="Fixture deterministic replay class",
        signature={"type": "linear_compute_3node"},
        metadata={"fixture": True},
    )
    assert isinstance(spec, ProductSpec)
    spec.metadata  # confirm frozen ProductSpec exists; we don't mutate it

    node_a = DagNode(node_id="A", task_id=task_a_id, station_id=station_id, role_hint="input_validator")
    node_b = DagNode(node_id="B", task_id=task_b_id, station_id=station_id, role_hint="compute")
    node_c = DagNode(node_id="C", task_id=task_c_id, station_id=station_id, role_hint="formatter")
    dep_ab = DagDependency(upstream="A", downstream="B", kind="hard")
    dep_bc = DagDependency(upstream="B", downstream="C", kind="hard")
    dag = ProductDAG(
        id=f"dag_{spec_id}",
        schema_version=SCHEMA_VERSION,
        nodes=(node_a, node_b, node_c),
        dependencies=(dep_ab, dep_bc),
    )

    def mk_task(tid, title, desc):
        return TaskContract(
            id=tid,
            schema_version=SCHEMA_VERSION,
            goal=title,
            station_id=station_id,
            constraints=(Requirement(text=desc, kind="functional"),),
            metadata={"title": title, "description": desc},
        )

    task_a = mk_task(task_a_id, "Validate input", "Ensure non-empty input")
    task_b = mk_task(task_b_id, "Compute result", "Perform deterministic computation")
    task_c = mk_task(task_c_id, "Format output", "Render result")

    station = StationContract(
        id=station_id,
        schema_version=SCHEMA_VERSION,
        role="general_worker",
        description="General worker station",
        allowed_tool_classes=("read", "write", "linter"),
        owned_scope=("fs_read", "fs_write"),
        version=1,
        metadata={"security_domain": "sandboxed"},
    )

    worker = WorkerProfile(
        id=worker_id,
        schema_version=SCHEMA_VERSION,
        display_name="fixture_worker_v1",
        version="1.0",
        capability_tags=("general",),
        station_eligibility=(station_id,),
        provider_opaque_reference=None,
    )

    return {
        "spec": spec,
        "class": cls,
        "dag": dag,
        "tasks": (task_a, task_b, task_c),
        "station": station,
        "worker": worker,
        "worker_id": worker_id,
        "task_a_id": task_a_id,
        "task_b_id": task_b_id,
        "task_c_id": task_c_id,
    }


def build_manifest(
    graph: dict[str, Any],
    preregistered_at: datetime = _T0,
) -> ExperimentManifest:
    manifest_id = ExperimentId(new_id("experiment"))
    return ExperimentManifest(
        id=manifest_id,
        schema_version=SCHEMA_VERSION,
        title="Replay calibration baseline 1",
        hypothesis_reference="H1",
        research_question="Does deterministic replay reconstruct recorded state exactly?",
        arms={"arm_baseline": {"worker": str(graph["worker_id"])}},
        baselines={"baseline1": {"mode": "exploration"}},
        task_selection={"class": "pilot"},
        environment=None,
        worker_declarations={graph["worker_id"]: {"version": "1.0"}},
        context_strategy={"strategy": "full_repo", "max_tokens": 128000},
        budgets={
            "total": __import__("redi_lab.domain", fromlist=["Budget"]).Budget(
                wall_clock_seconds=300.0, tokens_total=1_000_000
            )
        },
        acceptance_criteria=(
            AcceptanceCriterion(description="replay produces semantically equivalent state", criterion_type="deterministic"),
        ),
        preregistered_thresholds={"h1_alpha": 0.05},
        randomization={"seed": 42},
        repetition_config={"runs_per_arm": 3},
        measurement_plan={"primary": "per_node_success_rate"},
        pricing_assumptions={"schedule": "v2026Q3"},
        version_assumptions={"redi_schema": SCHEMA_VERSION},
        human_intervention_policy={"allowed": False},
        replay_policy={"required": True, "min_sample": 3},
        validity_controls={"gate_parity": True},
        preregistered_at=preregistered_at,
        created_at=_T0,
    )


def build_factory_run(graph, manifest: ExperimentManifest) -> FactoryRun:
    run_id = FactoryRunId(new_id("factory_run"))
    return FactoryRun(
        id=run_id,
        schema_version=SCHEMA_VERSION,
        product_spec_id=graph["spec"].id,
        dag_id=graph["dag"].id,
        recipe_id=None,
        mode=Mode.EXPLORATION,
        final_status=RunStatus.PENDING,
        verified=False,
        timing=None,
        total_cost=None,
        total_tokens=None,
        metadata={"fixture": True, "experiment_id": str(manifest.id)},
        created_at=_T0,
    )


def _build_event(etype, node, run_id: str, manifest_id: str, ts: datetime):
    eid = "event_fix_" + etype.value + "_" + node + "_" + ts.strftime("%H%M%S")
    from redi_lab.domain import EventId
    return LifecycleEvent(
        id=EventId(eid),
        event_type=etype,
        timestamp=ts,
        schema_version=SCHEMA_VERSION,
        entity_ids={
            "factory_run_id": run_id,
            "experiment_id": manifest_id,
            "node_id": node,
        },
        payload={"node": node},
    )


def build_six_event_sequence(run: FactoryRun, manifest: ExperimentManifest) -> tuple:
    rid = str(run.id)
    mid = str(manifest.id)
    events = [
        _build_event(LifecycleEventType.WORKER_STARTED, "A", rid, mid, _ts(10)),
        _build_event(LifecycleEventType.WORKER_COMPLETED, "A", rid, mid, _ts(20)),
        _build_event(LifecycleEventType.WORKER_STARTED, "B", rid, mid, _ts(30)),
        _build_event(LifecycleEventType.WORKER_COMPLETED, "B", rid, mid, _ts(40)),
    ]
    gid = "event_fix_GATE_PASSED_C_" + _ts(50).strftime("%H%M%S")
    from redi_lab.domain import EventId
    events.append(
        LifecycleEvent(
            id=EventId(gid),
            event_type=LifecycleEventType.GATE_PASSED,
            timestamp=_ts(50),
            schema_version=SCHEMA_VERSION,
            entity_ids={"factory_run_id": rid, "experiment_id": mid, "node_id": "C", "task_id": "task_c_fixture"},
            payload={"gate_name": "integration", "target": "C"},
        )
    )
    pid = "event_fix_PRODUCT_VERIFIED_C_" + _ts(60).strftime("%H%M%S")
    events.append(
        LifecycleEvent(
            id=EventId(pid),
            event_type=LifecycleEventType.PRODUCT_VERIFIED,
            timestamp=_ts(60),
            schema_version=SCHEMA_VERSION,
            entity_ids={"factory_run_id": rid, "experiment_id": mid, "product_id": str(run.product_spec_id) if run.product_spec_id else ""},
            payload={"verified": True},
        )
    )
    return tuple(events)


def build_run_evidence(graph, run: FactoryRun) -> RunEvidence:
    from redi_lab.domain import EvidenceId
    eid = EvidenceId("evidence_fix_nodeB_" + _ts(35).strftime("%H%M%S"))
    from redi_lab.domain import ContextManifestId
    experiment_id_str = run.metadata.get("experiment_id") if isinstance(run.metadata, dict) else None
    return RunEvidence(
        id=eid,
        schema_version=SCHEMA_VERSION,
        experiment_id=experiment_id_str,
        factory_run_id=run.id,
        product_id=run.product_spec_id,
        product_class_id=graph["class"].id,
        recipe_id=None,
        node_id="B",
        station_id=graph["station"].id,
        task_class="compute",
        worker_id=graph["worker_id"],
        worker_version="1.0",
        token_usage=TokenUsage(input=123, output=45),
        timing=Timing(
            started_at=_ts(30),
            ended_at=_ts(40),
            wall_clock_seconds=10.0,
            compute_seconds=9.5,
        ),
        cost=MonetaryCost(amount=Decimal("0.0001"), currency="USD", basis="observed"),
        final_status=RunStatus.COMPLETED,
        metadata={"fixture": True},
        created_at=_ts(40),
    )


def build_gate_result_node_c(graph, run: FactoryRun) -> GateResult:
    from redi_lab.domain import GateResultId
    gid = GateResultId("gate_result_fix_C_" + _ts(50).strftime("%H%M%S"))
    return GateResult(
        id=gid,
        schema_version=SCHEMA_VERSION,
        gate_identity="integration_gate_for_C",
        gate_type=GateType.DETERMINISTIC,
        target_node_id="C",
        target_task_id=graph["task_c_id"],
        status=GateStatus.PASS,
        evidence_refs=(),
        verifier_type="deterministic_code",
        verifier_identity="fixture_gate_runner_v1",
        failure_reasons=(),
        metadata={"fixture": True},
        created_at=_ts(50),
    )


def build_deterministic_ledger(
    db_path_or_memory: str | Path = ":memory:",
) -> tuple[sqlite3.Connection, ExperimentManifest, FactoryRun, tuple[LifecycleEvent, ...]]:
    """Build full deterministic ledger fixture. Returns 4-tuple.

    SIDE EFFECT: writes to the connection.
    """
    if isinstance(db_path_or_memory, Path):
        conn = sqlite3.connect(str(db_path_or_memory))
    else:
        conn = sqlite3.connect(str(db_path_or_memory))
    conn.row_factory = sqlite3.Row
    bootstrap_schema(conn)
    graph = build_small_product_graph()
    manifest = build_manifest(graph)
    run = build_factory_run(graph, manifest)
    events = build_six_event_sequence(run, manifest)
    evidence = build_run_evidence(graph, run)
    gate_res = build_gate_result_node_c(graph, run)

    ledger = EvidenceLedger(conn)
    exp_repo = ExperimentRepository(conn, ledger)
    run_repo = FactoryRunRepository(conn, ledger)

    exp_repo.store_preregistered(manifest)
    exp_repo.freeze(str(manifest.id))

    run_repo.store(run, experiment_id=str(manifest.id))

    for ev in events:
        append_event(ledger, ev, factory_run_id=str(run.id), experiment_id=str(manifest.id), recorded_at=ev.timestamp.isoformat())

    append_run_evidence(ledger, evidence, factory_run_id=str(run.id), experiment_id=str(manifest.id), recorded_at=evidence.created_at.isoformat())
    append_gate_result(ledger, gate_res, factory_run_id=str(run.id), experiment_id=str(manifest.id), recorded_at=gate_res.created_at.isoformat())

    total_cost = MonetaryCost(amount=Decimal("0.0001234567890123456789"), currency="USD", basis="observed", schedule_version="v2026Q3")
    total_tokens = TokenUsage(input=700, output=300, cached_input=None, reasoning=None)
    run_timing = Timing(started_at=_ts(10), ended_at=_ts(60), wall_clock_seconds=50.0, compute_seconds=45.0, queue_seconds=5.0)
    run_repo.close_run(
        str(run.id),
        RunStatus.COMPLETED,
        True,
        total_cost=total_cost,
        total_tokens=total_tokens,
        timing=run_timing,
    )

    return conn, manifest, run, events


__all__ = [
    "build_deterministic_ledger",
    "build_small_product_graph",
    "build_manifest",
    "build_factory_run",
    "build_six_event_sequence",
    "build_run_evidence",
    "build_gate_result_node_c",
]
