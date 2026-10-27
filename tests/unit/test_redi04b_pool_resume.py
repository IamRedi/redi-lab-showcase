"""REDI-04B — pool provenance & crash/resume symmetry tests.

Deterministic, offline. Covers the crash/resume matrix for pooled runs and
the config-drift requirement: resumed runs continue from the PERSISTED
plan-time pool snapshot, never from mutated current configuration.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from redi_lab.domain import (
    PoolEntry,
    ProductSpec,
    Requirement,
    RiskTier,
    RunStatus,
    StationContract,
    StationId,
    TaskContract,
    WorkerId,
    WorkerPool,
    WorkerProfile,
    domain_from_json,
)
from redi_lab.factory.controller import (
    MinimalFactoryController,
    RunDeps,
    make_canonical_slug_fixture_refs,
    SimulatedCrash,
)
from redi_lab.replay import ReplayReader
from redi_lab.runtime.contracts import NodeRunResult, PatchEdit, WorkerRunner
from redi_lab.runtime.fake_worker import fake_reviewer_pass
from redi_lab.worker_selection import (
    SELECTION_KIND_INITIAL,
    SELECTION_KIND_WORKER_FALLBACK,
    pool_snapshot_hash,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

_CORRECT_PY = (
    '"""Slugify package."""\n\nfrom __future__ import annotations\n'
    "import unicodedata\nimport re\nfrom typing import Optional\n\n\n"
    "def slugify(text: str, *, maxlen: Optional[int] = None, separator: str = \"-\") -> str:\n"
    "    if not isinstance(text, str):\n        return text\n"
    "    t = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('utf-8').lower()\n"
    "    t = re.sub(r\"[^a-z0-9]+\", separator, t)\n    t = t.strip(separator)\n"
    "    if maxlen is not None and isinstance(maxlen, int) and 0 < maxlen < len(t):\n        t = t[:maxlen]\n"
    "    return t\n"
)
_FAILING_PY = (
    "import unicodedata\nimport re\n\n\n"
    "def slugify(text: str, *, maxlen: Optional[int] = None, separator: str = \"-\") -> str:\n"
    "    if not isinstance(text, str):\n        return text\n"
    "    t = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('utf-8').lower()\n"
    "    t = re.sub(r\"[^a-z0-9]+\", separator, t)\n    t = t.strip(separator)\n"
    "    if maxlen is not None and isinstance(maxlen, int) and 0 < maxlen < len(t):\n        t = t[:maxlen]\n"
    "    return t\n"
)


def _specs():
    product = ProductSpec(
        requested_outcome="Canonical slugify implementation task.",
        requirements=[Requirement(text="public tests pass", kind="functional")],
        constraints=["Only modify slugify/__init__.py."],
        risk_tier=RiskTier.LOW,
        unresolved_requirements=[],
    )
    task = TaskContract(goal="implement slugify", metadata={"task_class": "software_patch"})
    builder = StationContract(
        role="builder",
        required_output_kinds=["patch_edit"],
        allowed_tool_classes=[],
        applicable_task_classes=["software_patch"],
    )
    reviewer = StationContract(role="reviewer", required_output_kinds=["verdict"], allowed_tool_classes=[])
    return product, task, builder, reviewer


@dataclass
class _PatchWorker(WorkerRunner):
    content: str
    calls: int = 0

    def run_node(self, *, task_id, station_id, worker_id, context, workspace_root, station_role="builder"):
        self.calls += 1
        return NodeRunResult(
            worker_id=worker_id,
            task_id=task_id,
            station_id=station_id,
            patches=(PatchEdit(rel_path="slugify/__init__.py", action="write", content=self.content),),
            attempt_index=int((context or {}).get("attempt_index", 0)),
            elapsed_seconds=0.001,
        )


def _profile(worker_id: str, station: StationContract) -> WorkerProfile:
    return WorkerProfile(
        id=WorkerId(worker_id),
        display_name=worker_id,
        version="v1",
        availability_status="available",
        station_eligibility=[station.id],
        provider_opaque_reference=f"opaque:{worker_id}",
    )


def _pool(station: StationContract, ranked: list[tuple[str, int]]) -> WorkerPool:
    return WorkerPool(
        station_id=station.id,
        role="builder",
        entries=[PoolEntry(profile=_profile(wid, station), preference_rank=rank) for wid, rank in ranked],
    )


def _deps(station: StationContract, pool: WorkerPool | None, runners: dict[str, WorkerRunner] | None,
          *, crash: bool = False) -> RunDeps:
    refs = make_canonical_slug_fixture_refs(_REPO_ROOT)
    _, _, _, reviewer = _specs()
    return RunDeps(
        builder_worker=_PatchWorker(content=_CORRECT_PY),
        reviewer_worker=fake_reviewer_pass(),
        builder_station=station,
        reviewer_station=reviewer,
        fixture_root=refs["fixture_root"],
        hidden_test_root=refs["hidden_test_root"],
        allowed_scope_prefixes=refs["allowed_scope_prefixes"],
        gate_spec=refs["gate_spec"],
        max_retries=1,
        crash_inject_after_worker_completed=crash,
        builder_pool=pool,
        builder_runners=runners,
    )



def _run_id(db_path) -> str:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT id FROM factory_runs").fetchone()[0]
    finally:
        conn.close()


def _builder_attempts(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT payload_json FROM evidence_entries WHERE entity_type='RunEvidence' ORDER BY global_sequence"
        ).fetchall()
        return [json.loads(p) for (p,) in rows if json.loads(p).get("node_id") == "N_BUILDER_0"]
    finally:
        conn.close()


def _plan_pools(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT payload_json FROM evidence_entries WHERE entity_type='LifecycleEvent' "
            "AND payload_json LIKE '%PLAN_COMPILED%' ORDER BY global_sequence LIMIT 1"
        ).fetchone()
        return (json.loads(row[0]).get("payload") or {}).get("worker_pools") or {}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# T1. Crash after Worker A completes → resume evaluates A, falls back to B,
#     B verifies. A is never re-executed.
# ---------------------------------------------------------------------------


def test_04b_t1_crash_after_a_resume_falls_back_to_b_and_verifies(tmp_path):
    product, task, station, _ = _specs()
    wa, wb = _PatchWorker(content=_FAILING_PY), _PatchWorker(content=_CORRECT_PY)
    pool = _pool(station, [("worker_a_small", 0), ("worker_b_correct", 1)])
    tmpdb = tmp_path / "t1.db"
    with pytest.raises(SimulatedCrash):
        MinimalFactoryController.run(product, task, _deps(station, pool, {"worker_a_small": wa, "worker_b_correct": wb}, crash=True), db_path=tmpdb)
    assert wa.calls == 1 and wb.calls == 0

    out = MinimalFactoryController.resume(_run_id(tmpdb), product, task,
                                          _deps(station, pool, {"worker_a_small": wa, "worker_b_correct": wb}),
                                          db_path=tmpdb)
    assert out.final_status == RunStatus.COMPLETED
    assert out.verified is True
    assert (wa.calls, wb.calls) == (1, 1)  # A never re-run; B ran exactly once

    attempts = _builder_attempts(str(tmpdb))
    assert [a["worker_id"] for a in attempts] == ["worker_a_small", "worker_b_correct"]
    sel0, sel1 = attempts[0]["metadata"]["worker_selection"], attempts[1]["metadata"]["worker_selection"]
    assert sel0["selection_kind"] == SELECTION_KIND_INITIAL
    assert sel1["selection_kind"] == SELECTION_KIND_WORKER_FALLBACK
    assert sel1["previous_worker_id"] == "worker_a_small"
    # provenance binding: persisted plan hash == per-attempt reference
    pools = _plan_pools(str(tmpdb))
    assert sel0["pool_snapshot_hash"] == pools["builder"]["snapshot_hash"]


def test_04b_t2_crash_after_b_completes_resume_evaluates_b_and_verifies(tmp_path):
    product, task, station, _ = _specs()
    wa, wb = _PatchWorker(content=_FAILING_PY), _PatchWorker(content=_CORRECT_PY)
    pool = _pool(station, [("worker_a_small", 0), ("worker_b_correct", 1)])
    tmpdb = tmp_path / "t2.db"
    runners = {"worker_a_small": wa, "worker_b_correct": wb}

    with pytest.raises(SimulatedCrash):  # crash after A completes
        MinimalFactoryController.run(product, task, _deps(station, pool, runners, crash=True), db_path=tmpdb)
    with pytest.raises(SimulatedCrash):  # resume evaluates A (fail), falls back to B, crashes after B completes
        MinimalFactoryController.resume(_run_id(tmpdb), product, task,
                                        _deps(station, pool, runners, crash=True), db_path=tmpdb,
                                        crash_inject_after_worker_completed=True)
    assert (wa.calls, wb.calls) == (1, 1)

    out = MinimalFactoryController.resume(_run_id(tmpdb), product, task, _deps(station, pool, runners), db_path=tmpdb)
    assert out.final_status == RunStatus.COMPLETED and out.verified is True
    assert (wa.calls, wb.calls) == (1, 1)  # neither worker re-executed
    attempts = _builder_attempts(str(tmpdb))
    assert [a["worker_id"] for a in attempts] == ["worker_a_small", "worker_b_correct"]
    sel1 = attempts[1]["metadata"]["worker_selection"]
    assert sel1["selection_kind"] == SELECTION_KIND_WORKER_FALLBACK
    assert sel1["previous_worker_id"] == "worker_a_small"


def test_04b_t4_runner_unbound_after_crash_exhausts_pool_as_blocked(tmp_path):
    product, task, station, _ = _specs()
    wa = _PatchWorker(content=_FAILING_PY)
    pool = _pool(station, [("worker_a_small", 0)])
    tmpdb = tmp_path / "t4.db"
    with pytest.raises(SimulatedCrash):
        MinimalFactoryController.run(product, task, _deps(station, pool, {"worker_a_small": wa}, crash=True), db_path=tmpdb)

    # A's runner is no longer bound (e.g. worker retired before resume) and
    # no other eligible worker exists → pool exhaustion survives resume.
    out = MinimalFactoryController.resume(_run_id(tmpdb), product, task, _deps(station, pool, {}), db_path=tmpdb)
    assert out.final_status == RunStatus.BLOCKED
    assert out.escalation is True
    assert wa.calls == 1  # never re-run
    conn = sqlite3.connect(str(tmpdb))
    try:
        fr = conn.execute(
            "SELECT payload_json FROM evidence_entries WHERE entity_type='FailureRecord'"
        ).fetchall()
        assert any(json.loads(p[0])["origin"] == "worker_pool_exhausted" for p in fr)
    finally:
        conn.close()


def test_04b_t5_reviewer_resume_with_pooled_builder_history(tmp_path):
    product, task, station, _ = _specs()
    wa = _PatchWorker(content=_CORRECT_PY)
    pool = _pool(station, [("worker_a_ok", 0)])
    tmpdb = tmp_path / "t5.db"
    with pytest.raises(SimulatedCrash):
        MinimalFactoryController.run(product, task, _deps(station, pool, {"worker_a_ok": wa}, crash=True), db_path=tmpdb)
    out = MinimalFactoryController.resume(_run_id(tmpdb), product, task, _deps(station, pool, {"worker_a_ok": wa}), db_path=tmpdb)
    assert out.final_status == RunStatus.COMPLETED and out.verified is True
    attempts = _builder_attempts(str(tmpdb))
    assert len(attempts) == 1 and attempts[0]["worker_id"] == "worker_a_ok"
    conn = sqlite3.connect(str(tmpdb))
    try:
        rev = conn.execute(
            "SELECT COUNT(*) FROM evidence_entries WHERE entity_type='RunEvidence' AND payload_json LIKE '%N_REVIEWER_0%'"
        ).fetchone()[0]
        pv = conn.execute(
            "SELECT COUNT(*) FROM evidence_entries WHERE payload_json LIKE '%PRODUCT_VERIFIED%'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert rev == 1 and pv == 1


def test_04b_config_drift_resume_uses_persisted_pool_not_current(tmp_path):
    product, task, station, _ = _specs()
    wa, wb, wc = _PatchWorker(content=_FAILING_PY), _PatchWorker(content=_CORRECT_PY), _PatchWorker(content=_CORRECT_PY)
    p1 = _pool(station, [("worker_a_small", 0), ("worker_b_correct", 1)])
    tmpdb = tmp_path / "drift.db"
    with pytest.raises(SimulatedCrash):
        MinimalFactoryController.run(product, task, _deps(station, p1, {"worker_a_small": wa, "worker_b_correct": wb}, crash=True), db_path=tmpdb)

    # Current POOL configuration drifts to P2 (worker_b removed; only
    # worker_c exists). Runtime runner bindings for the historical workers
    # remain present — bindings are runtime state; the pool config is what
    # must NOT silently replace persisted history.
    p2 = _pool(station, [("worker_c_current", 0)])
    deps2 = _deps(station, p2, {"worker_a_small": wa, "worker_b_correct": wb, "worker_c_current": wc})
    out = MinimalFactoryController.resume(_run_id(tmpdb), product, task, deps2, db_path=tmpdb)

    # Historical selection wins: B from P1 runs and verifies; C never runs.
    assert out.final_status == RunStatus.COMPLETED and out.verified is True
    assert (wa.calls, wb.calls, wc.calls) == (1, 1, 0)
    attempts = _builder_attempts(str(tmpdb))
    assert [a["worker_id"] for a in attempts] == ["worker_a_small", "worker_b_correct"]
    pools = _plan_pools(str(tmpdb))
    persisted_hash = pools["builder"]["snapshot_hash"]
    assert attempts[0]["metadata"]["worker_selection"]["pool_snapshot_hash"] == persisted_hash
    assert attempts[1]["metadata"]["worker_selection"]["pool_snapshot_hash"] == persisted_hash
    assert pool_snapshot_hash(pools["builder"]["snapshot"]) == persisted_hash


def test_04b_replay_reconstructs_transitions_twice_identical(tmp_path):
    product, task, station, _ = _specs()
    wa, wb = _PatchWorker(content=_FAILING_PY), _PatchWorker(content=_CORRECT_PY)
    pool = _pool(station, [("worker_a_small", 0), ("worker_b_correct", 1)])
    tmpdb = tmp_path / "replay.db"
    with pytest.raises(SimulatedCrash):
        MinimalFactoryController.run(product, task, _deps(station, pool, {"worker_a_small": wa, "worker_b_correct": wb}, crash=True), db_path=tmpdb)
    out = MinimalFactoryController.resume(_run_id(tmpdb), product, task, _deps(station, pool, {"worker_a_small": wa, "worker_b_correct": wb}), db_path=tmpdb)
    assert out.verified is True

    import redi_lab.runtime.live_provider_adapter as lpa
    calls = {"n": 0}
    original = lpa._sdk_available

    def _counting():
        calls["n"] += 1
        return False

    def snapshot():
        reader = ReplayReader(str(tmpdb))
        try:
            res = reader.replay_factory_run(out.factory_run_id)
        finally:
            reader.close()
        evs = [e for e in res.run_evidences if getattr(e, "node_id", "") == "N_BUILDER_0"]
        return res, [(e.worker_id, dict(e.metadata["worker_selection"])) for e in evs]

    lpa._sdk_available = _counting
    try:
        r1, s1 = snapshot()
        r2, s2 = snapshot()
    finally:
        lpa._sdk_available = original

    assert r1.integrity_status == "OK" == r2.integrity_status
    assert r1.observed_run_status == r2.observed_run_status == "completed"
    assert r1.final_verification_state is True and r2.final_verification_state is True
    assert s1 == s2
    assert [w for w, _ in s1] == ["worker_a_small", "worker_b_correct"]
    assert [d["selection_kind"] for _, d in s1] == [SELECTION_KIND_INITIAL, SELECTION_KIND_WORKER_FALLBACK]
    assert calls["n"] == 0
