"""stdlib-only REDI-04A validation runner (station & worker pools).

Deterministic, zero-network. Mirrors the REDI-01/02/03 pack pattern:
    python -m scripts.run_redi_04_validations

Covers the REDI-04A acceptance minimum:
  pool construction, deterministic selection, eligibility/availability
  rejection, fallback ladder, same-worker-retry vs fallback distinction,
  pool exhaustion, evidence identity, station stability under worker swap,
  and replay reconstruction with zero provider calls.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

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


def _specs():
    from redi_lab.domain import (
        ProductSpec, Requirement, RiskTier, StationContract, TaskContract,
    )

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


def _profile(worker_id, station, *, version="v1", availability="available", eligibility=None):
    from redi_lab.domain import WorkerProfile, WorkerId

    return WorkerProfile(
        id=WorkerId(worker_id),
        display_name=worker_id,
        version=version,
        availability_status=availability,
        station_eligibility=[station.id] if eligibility is None else eligibility,
        provider_opaque_reference=f"opaque:{worker_id}",
    )


def _pool(station, entries):
    from redi_lab.domain import PoolEntry, WorkerPool

    return WorkerPool(
        station_id=station.id,
        role="builder",
        entries=[PoolEntry(profile=p, preference_rank=r) for (p, r) in entries],
    )


_CORRECT = (
    '"""Slugify package."""\n\nfrom __future__ import annotations\n'
    "import unicodedata\nimport re\nfrom typing import Optional\n\n\n"
    "def slugify(text: str, *, maxlen: Optional[int] = None, separator: str = \"-\") -> str:\n"
    "    if not isinstance(text, str):\n        return text\n"
    "    t = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('utf-8').lower()\n"
    "    t = re.sub(r\"[^a-z0-9]+\", separator, t)\n    t = t.strip(separator)\n"
    "    if maxlen is not None and isinstance(maxlen, int) and 0 < maxlen < len(t):\n        t = t[:maxlen]\n"
    "    return t\n"
)
_FAILING = (
    _CORRECT
    .replace("from __future__ import annotations\n", "")
    .replace("from typing import Optional\n", "")
)
# Sanity: the failing artifact must crash at import time (eager annotation
# evaluation of Optional[int] with no typing import) — public gate → exit 2.
import importlib.util as _ilu
import tempfile as _tf


def _failing_artifact_import_raises_name_error():
    f = _tf.NamedTemporaryFile(suffix=".py", delete=False)
    f.write(_FAILING.encode("utf-8"))
    f.close()
    try:
        spec = _ilu.spec_from_file_location("_redi04_failing_mod", f.name)
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except NameError:
        return
    finally:
        os.unlink(f.name)
    raise AssertionError("expected NameError at import of the failing artifact")


_FAILING_SANITY = _failing_artifact_import_raises_name_error


@dataclass
class _PatchWorker:
    content: str
    calls: int = 0

    def run_node(self, *, task_id, station_id, worker_id, context, workspace_root, station_role="builder"):
        from redi_lab.runtime.contracts import NodeRunResult, PatchEdit

        self.calls += 1
        return NodeRunResult(
            worker_id=worker_id,
            task_id=task_id,
            station_id=station_id,
            patches=(PatchEdit(rel_path="slugify/__init__.py", action="write", content=self.content),),
            attempt_index=int((context or {}).get("attempt_index", 0)),
            elapsed_seconds=0.001,
        )


def _pool_deps(station, pool, runners, max_retries=1):
    from redi_lab.factory.controller import RunDeps, make_canonical_slug_fixture_refs
    from redi_lab.runtime.fake_worker import fake_reviewer_pass

    _, _, _, reviewer = _specs()
    refs = make_canonical_slug_fixture_refs(REPO_ROOT)
    return RunDeps(
        builder_worker=_PatchWorker(content=_CORRECT),
        reviewer_worker=fake_reviewer_pass(),
        builder_station=station,
        reviewer_station=reviewer,
        fixture_root=refs["fixture_root"],
        hidden_test_root=refs["hidden_test_root"],
        allowed_scope_prefixes=refs["allowed_scope_prefixes"],
        gate_spec=refs["gate_spec"],
        max_retries=max_retries,
        builder_pool=pool,
        builder_runners=runners,
    )


def _builder_attempts(db_path):
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT payload_json FROM evidence_entries WHERE entity_type='RunEvidence' ORDER BY global_sequence"
        ).fetchall()
        return [json.loads(p) for (p,) in rows if json.loads(p).get("node_id") == "N_BUILDER_0"]
    finally:
        conn.close()


# ---------------------------------------------------------------- checks


def v01_pool_contract_and_registry_frozen():
    from redi_lab.domain import PoolEntry, WorkerPool, WorkerProfile, WorkerId
    import redi_lab.domain as dom

    station = _specs()[2]
    p = _profile("w1", station)
    pool = _pool(station, [(p, 0)])
    assert pool.selection_policy_id == "deterministic_v0"
    assert pool.entries[0].profile.id == p.id
    assert len(dom.CANONICAL_CONTRACTS) == 14  # governance baseline untouched


def v02_selection_deterministic_lowest_rank():
    from redi_lab.worker_selection import resolve_worker, SELECTION_POLICY_ID

    _, task, station, _ = _specs()
    pool = _pool(station, [(_profile("w_b", station), 1), (_profile("w_a", station), 0)])
    picks = [
        resolve_worker(pool=pool, runners={"w_a": 1, "w_b": 1}, station=station, task=task, attempt_index=0)
        for _ in range(3)
    ]
    assert all(str(p.profile.id) == "w_a" for p in picks)
    assert picks[0].policy_id == SELECTION_POLICY_ID
    assert picks[0].to_evidence() == picks[1].to_evidence()


def v03_eligibility_and_availability_rejection():
    from redi_lab.domain import StationId
    from redi_lab.worker_selection import resolve_worker

    _, task, station, _ = _specs()
    pool = _pool(station, [
        (_profile("w_foreign", station, eligibility=[StationId("station_elsewhere")]), 0),
        (_profile("w_retired", station, availability="deprecated"), 1),
        (_profile("w_ok", station), 2),
    ])
    sel = resolve_worker(pool=pool, runners={"w_foreign": 1, "w_retired": 1, "w_ok": 1},
                         station=station, task=task, attempt_index=0)
    assert str(sel.profile.id) == "w_ok"
    assert [s["worker_id"] for s in sel.skipped] == ["w_foreign", "w_retired"]


def v04_canonical_fallback_experiment():
    from redi_lab.domain import RunStatus
    from redi_lab.factory.controller import MinimalFactoryController
    from redi_lab.worker_selection import (
        SELECTION_KIND_INITIAL, SELECTION_KIND_WORKER_FALLBACK,
    )

    product, task, station, _ = _specs()
    wa, wb = _PatchWorker(content=_FAILING), _PatchWorker(content=_CORRECT)
    pool = _pool(station, [(_profile("worker_a_small", station), 0), (_profile("worker_b_correct", station), 1)])
    deps = _pool_deps(station, pool, {"worker_a_small": wa, "worker_b_correct": wb})
    outcome = MinimalFactoryController.run(product, task, deps)
    assert outcome.final_status == RunStatus.COMPLETED and outcome.verified is True
    assert outcome.escalation is False
    assert (wa.calls, wb.calls) == (1, 1)
    att = _builder_attempts(outcome.db_path)
    assert [a["worker_id"] for a in att] == ["worker_a_small", "worker_b_correct"]
    assert att[0]["metadata"]["worker_selection"]["selection_kind"] == SELECTION_KIND_INITIAL
    sel1 = att[1]["metadata"]["worker_selection"]
    assert sel1["selection_kind"] == SELECTION_KIND_WORKER_FALLBACK
    assert sel1["previous_worker_id"] == "worker_a_small"
    conn = sqlite3.connect(outcome.db_path)
    try:
        gates = conn.execute(
            "SELECT payload_json FROM evidence_entries WHERE entity_type='GateResult' ORDER BY global_sequence"
        ).fetchall()
        seq = [(json.loads(r[0])["gate_identity"], json.loads(r[0])["status"]) for r in gates]
        verified = conn.execute(
            "SELECT COUNT(*) FROM evidence_entries WHERE payload_json LIKE '%PRODUCT_VERIFIED%'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert seq[-4:] == [
        ("allowed_file_change_scope", "pass"),
        ("python_syntax_compile", "pass"),
        ("public_test_execution", "pass"),
        ("hidden_test_execution", "pass"),
    ]
    assert verified == 1


def v05_first_worker_success_short_circuits():
    from redi_lab.domain import RunStatus
    from redi_lab.factory.controller import MinimalFactoryController

    product, task, station, _ = _specs()
    wa, wb = _PatchWorker(content=_CORRECT), _PatchWorker(content=_CORRECT)
    pool = _pool(station, [(_profile("worker_a_ok", station), 0), (_profile("worker_b_never", station), 1)])
    deps = _pool_deps(station, pool, {"worker_a_ok": wa, "worker_b_never": wb})
    outcome = MinimalFactoryController.run(product, task, deps)
    assert outcome.final_status == RunStatus.COMPLETED and outcome.attempts == 1
    assert (wa.calls, wb.calls) == (1, 0)


def v06_same_worker_retry_vs_fallback_and_exhaustion():
    from redi_lab.domain import RunStatus
    from redi_lab.factory.controller import MinimalFactoryController
    from redi_lab.worker_selection import (
        SELECTION_KIND_INITIAL, SELECTION_KIND_SAME_WORKER_RETRY, SELECTION_KIND_WORKER_FALLBACK,
    )

    product, task, station, _ = _specs()
    # two-worker ladder: attempt 1 is a worker fallback
    wa, wb = _PatchWorker(content=_FAILING), _PatchWorker(content=_CORRECT)
    pool2 = _pool(station, [(_profile("worker_a_small", station), 0), (_profile("worker_b_correct", station), 1)])
    out2 = MinimalFactoryController.run(product, task, _pool_deps(station, pool2, {"worker_a_small": wa, "worker_b_correct": wb}))
    kinds2 = [a["metadata"]["worker_selection"]["selection_kind"] for a in _builder_attempts(out2.db_path)]
    assert kinds2 == [SELECTION_KIND_INITIAL, SELECTION_KIND_WORKER_FALLBACK]
    # single-worker ladder: attempt 1 is a same-worker retry, then escalation
    w1 = _PatchWorker(content=_FAILING)
    pool1 = _pool(station, [(_profile("worker_a_only", station), 0)])
    out1 = MinimalFactoryController.run(product, task, _pool_deps(station, pool1, {"worker_a_only": w1}))
    assert out1.final_status == RunStatus.FAILED and out1.escalation is True
    att1 = _builder_attempts(out1.db_path)
    kinds1 = [a["metadata"]["worker_selection"]["selection_kind"] for a in att1]
    assert kinds1 == [SELECTION_KIND_INITIAL, SELECTION_KIND_SAME_WORKER_RETRY]
    assert w1.calls == 2


def v07_no_eligible_worker_blocked_with_evidence():
    from redi_lab.domain import StationId, RunStatus
    from redi_lab.factory.controller import MinimalFactoryController

    product, task, station, _ = _specs()
    wf = _PatchWorker(content=_CORRECT)
    pool = _pool(station, [(_profile("worker_foreign", station, eligibility=[StationId("station_elsewhere")]), 0)])
    deps = _pool_deps(station, pool, {"worker_foreign": wf})
    outcome = MinimalFactoryController.run(product, task, deps)
    assert outcome.final_status == RunStatus.BLOCKED and outcome.escalation is True
    assert wf.calls == 0
    conn = sqlite3.connect(outcome.db_path)
    try:
        fr = conn.execute(
            "SELECT payload_json FROM evidence_entries WHERE entity_type='FailureRecord'"
        ).fetchall()
        assert len(fr) == 1
        assert json.loads(fr[0][0])["origin"] == "worker_pool_exhausted"
    finally:
        conn.close()


def v08_station_and_product_identity_stable_across_worker_change():
    from redi_lab.factory.controller import MinimalFactoryController

    product, task, station, _ = _specs()
    wa, wb = _PatchWorker(content=_FAILING), _PatchWorker(content=_CORRECT)
    pool_ab = _pool(station, [(_profile("worker_a_small", station), 0), (_profile("worker_b_correct", station), 1)])
    pool_ba = _pool(station, [(_profile("worker_a_small", station), 1), (_profile("worker_b_correct", station), 0)])
    out1 = MinimalFactoryController.run(product, task, _pool_deps(station, pool_ab, {"worker_a_small": wa, "worker_b_correct": wb}))
    out2 = MinimalFactoryController.run(product, task, _pool_deps(station, pool_ba, {"worker_a_small": wa, "worker_b_correct": wb}))
    plans = []
    for db in (out1.db_path, out2.db_path):
        conn = sqlite3.connect(db)
        try:
            plans.append(json.loads(conn.execute(
                "SELECT payload_json FROM evidence_entries WHERE global_sequence=2").fetchone()[0])["payload"])
        finally:
            conn.close()
    assert plans[0]["builder_station_id"] == plans[1]["builder_station_id"] == str(station.id)
    assert plans[0]["task_id"] == plans[1]["task_id"] == str(task.id)


def v09_station_contract_has_no_provider_fields():
    from redi_lab.domain import StationContract

    fields = set(StationContract.__dataclass_fields__)
    for forbidden in ("model", "provider", "pricing", "api_key", "temperature"):
        assert forbidden not in fields
    blob = json.dumps(StationContract(role="builder").__dict__, default=str).lower()
    for forbidden in ("openai", "gpt-", "gemini", "anthropic"):
        assert forbidden not in blob


def v10_replay_reconstructs_worker_transitions_zero_providers():
    import redi_lab.runtime.live_provider_adapter as lpa
    from redi_lab.domain import FactoryRunId
    from redi_lab.factory.controller import MinimalFactoryController
    from redi_lab.replay import ReplayReader
    from redi_lab.worker_selection import (
        SELECTION_KIND_INITIAL, SELECTION_KIND_WORKER_FALLBACK,
    )

    product, task, station, _ = _specs()
    wa, wb = _PatchWorker(content=_FAILING), _PatchWorker(content=_CORRECT)
    pool = _pool(station, [(_profile("worker_a_small", station), 0), (_profile("worker_b_correct", station), 1)])
    deps = _pool_deps(station, pool, {"worker_a_small": wa, "worker_b_correct": wb})
    outcome = MinimalFactoryController.run(product, task, deps)

    def snapshot():
        reader = ReplayReader(outcome.db_path)
        try:
            res = reader.replay_factory_run(FactoryRunId(outcome.factory_run_id))
        finally:
            reader.close()
        evs = [e for e in res.run_evidences if getattr(e, "node_id", "") == "N_BUILDER_0"]
        return res, [dict(e.metadata["worker_selection"]) for e in evs]

    calls = {"n": 0}
    original = lpa._sdk_available

    def _counting():
        calls["n"] += 1
        return False

    lpa._sdk_available = _counting
    try:
        r1, sel1 = snapshot()
        r2, sel2 = snapshot()
    finally:
        lpa._sdk_available = original
    assert r1.integrity_status == "OK" == r2.integrity_status
    assert r1.observed_run_status == "completed" and r1.final_verification_state is True
    assert [s["worker_id"] for s in sel1] == ["worker_a_small", "worker_b_correct"]
    assert [s["selection_kind"] for s in sel1] == [SELECTION_KIND_INITIAL, SELECTION_KIND_WORKER_FALLBACK]
    assert sel1 == sel2
    assert calls["n"] == 0


# ---------------------------------------------------------------------------
# REDI-04B — pool provenance & crash/resume symmetry
# ---------------------------------------------------------------------------


def v11_pool_snapshot_hash_deterministic_and_sensitive():
    from redi_lab.worker_selection import pool_snapshot, pool_snapshot_hash

    _, task, station, _ = _specs()
    pool = _pool(station, [(_profile("w_b", station), 1), (_profile("w_a", station), 0)])
    snap = pool_snapshot(pool=pool, station=station, task=task)
    h = pool_snapshot_hash(snap)
    assert pool_snapshot_hash(pool_snapshot(pool=pool, station=station, task=task)) == h
    assert [c["worker_id"] for c in snap["candidates"]] == ["w_a", "w_b"]  # selection order
    snap2 = pool_snapshot(pool=pool, station=station, task=task)
    snap2["candidates"][0]["profile_version"] = "v9"
    assert pool_snapshot_hash(snap2) != h  # version change
    snap3 = pool_snapshot(pool=pool, station=station, task=task)
    snap3["candidates"][0]["preference_rank"] = 5
    assert pool_snapshot_hash(snap3) != h  # rank change
    snap4 = pool_snapshot(pool=pool, station=station, task=task)
    snap4["candidates"][0]["availability_status"] = "unavailable"
    assert pool_snapshot_hash(snap4) != h  # availability change
    snap5 = pool_snapshot(pool=pool, station=station, task=task)
    snap5["selection_policy_id"] = "other_policy"
    assert pool_snapshot_hash(snap5) != h  # policy change


def _fr_id(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT id FROM factory_runs").fetchone()[0]
    finally:
        conn.close()


def _plan_pools(db_path):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT payload_json FROM evidence_entries WHERE entity_type='LifecycleEvent' "
            "AND payload_json LIKE '%PLAN_COMPILED%' ORDER BY global_sequence LIMIT 1"
        ).fetchone()
        return (json.loads(row[0]).get("payload") or {}).get("worker_pools") or {}
    finally:
        conn.close()


def v12_plan_binds_pool_provenance_only_when_pooled():
    from redi_lab.factory.controller import MinimalFactoryController

    product, task, station, _ = _specs()
    wa, wb = _PatchWorker(content=_FAILING), _PatchWorker(content=_CORRECT)
    pool = _pool(station, [(_profile("worker_a_small", station), 0), (_profile("worker_b_correct", station), 1)])
    deps = _pool_deps(station, pool, {"worker_a_small": wa, "worker_b_correct": wb})
    out = MinimalFactoryController.run(product, task, deps)
    conn = sqlite3.connect(out.db_path)
    try:
        plan = json.loads(conn.execute(
            "SELECT payload_json FROM evidence_entries WHERE entity_type='LifecycleEvent' "
            "AND payload_json LIKE '%PLAN_COMPILED%' ORDER BY global_sequence LIMIT 1").fetchone()[0])["payload"]
        att = [json.loads(r[0]) for r in conn.execute(
            "SELECT payload_json FROM evidence_entries WHERE entity_type='RunEvidence' ORDER BY global_sequence")]
    finally:
        conn.close()
    wp = plan["worker_pools"]["builder"]
    assert wp["snapshot_hash"] == att[0]["metadata"]["worker_selection"]["pool_snapshot_hash"]
    assert wp["snapshot_hash"] == att[1]["metadata"]["worker_selection"]["pool_snapshot_hash"]
    # pool-less control: no worker_pools key in the plan payload
    deps_nopool = _pool_deps(station, None, None)
    out2 = MinimalFactoryController.run(product, task, deps_nopool)
    assert out2.verified is True
    conn = sqlite3.connect(out2.db_path)
    try:
        plan2 = json.loads(conn.execute(
            "SELECT payload_json FROM evidence_entries WHERE entity_type='LifecycleEvent' "
            "AND payload_json LIKE '%PLAN_COMPILED%' ORDER BY global_sequence LIMIT 1").fetchone()[0])["payload"]
    finally:
        conn.close()
    assert "worker_pools" not in plan2


def v13_crash_after_a_resume_falls_back_to_b_and_verifies():
    from redi_lab.domain import RunStatus
    from redi_lab.factory.controller import MinimalFactoryController, SimulatedCrash
    from redi_lab.worker_selection import SELECTION_KIND_INITIAL, SELECTION_KIND_WORKER_FALLBACK

    product, task, station, _ = _specs()
    wa, wb = _PatchWorker(content=_FAILING), _PatchWorker(content=_CORRECT)
    pool = _pool(station, [(_profile("worker_a_small", station), 0), (_profile("worker_b_correct", station), 1)])
    deps = _pool_deps(station, pool, {"worker_a_small": wa, "worker_b_correct": wb})
    deps.crash_inject_after_worker_completed = True
    out_db = os.path.join(tempfile.gettempdir(), "_redi04b_pack_crash.db")
    if os.path.exists(out_db):
        os.unlink(out_db)
    try:
        with pytest.raises(SimulatedCrash):
            MinimalFactoryController.run(product, task, deps, db_path=out_db)
        assert (wa.calls, wb.calls) == (1, 0)
        deps_resume = _pool_deps(station, pool, {"worker_a_small": wa, "worker_b_correct": wb})
        out = MinimalFactoryController.resume(_fr_id(out_db), product, task, deps_resume, db_path=out_db)
        assert out.final_status == RunStatus.COMPLETED and out.verified is True
        assert (wa.calls, wb.calls) == (1, 1)  # A never re-executed
        att = _builder_attempts(out_db)
        assert [a["worker_id"] for a in att] == ["worker_a_small", "worker_b_correct"]
        assert att[0]["metadata"]["worker_selection"]["selection_kind"] == SELECTION_KIND_INITIAL
        assert att[1]["metadata"]["worker_selection"]["selection_kind"] == SELECTION_KIND_WORKER_FALLBACK
        assert att[1]["metadata"]["worker_selection"]["previous_worker_id"] == "worker_a_small"
    finally:
        if os.path.exists(out_db):
            os.unlink(out_db)


def v14_resume_uses_persisted_pool_over_drifted_config():
    from redi_lab.domain import RunStatus
    from redi_lab.factory.controller import MinimalFactoryController, SimulatedCrash

    product, task, station, _ = _specs()
    wa, wb, wc = _PatchWorker(content=_FAILING), _PatchWorker(content=_CORRECT), _PatchWorker(content=_CORRECT)
    p1 = _pool(station, [(_profile("worker_a_small", station), 0), (_profile("worker_b_correct", station), 1)])
    deps = _pool_deps(station, p1, {"worker_a_small": wa, "worker_b_correct": wb})
    deps.crash_inject_after_worker_completed = True
    out_db = os.path.join(tempfile.gettempdir(), "_redi04b_pack_drift.db")
    if os.path.exists(out_db):
        os.unlink(out_db)
    try:
        with pytest.raises(SimulatedCrash):
            MinimalFactoryController.run(product, task, deps, db_path=out_db)
        p2 = _pool(station, [(_profile("worker_c_current", station), 0)])
        deps2 = _pool_deps(station, p2, {"worker_a_small": wa, "worker_b_correct": wb, "worker_c_current": wc})
        out = MinimalFactoryController.resume(_fr_id(out_db), product, task, deps2, db_path=out_db)
        assert out.final_status == RunStatus.COMPLETED and out.verified is True
        assert (wa.calls, wb.calls, wc.calls) == (1, 1, 0)  # historical P1 semantics won
        att = _builder_attempts(out_db)
        pools = _plan_pools(out_db)
        h = pools["builder"]["snapshot_hash"]
        assert att[0]["metadata"]["worker_selection"]["pool_snapshot_hash"] == h
        assert att[1]["metadata"]["worker_selection"]["pool_snapshot_hash"] == h
    finally:
        if os.path.exists(out_db):
            os.unlink(out_db)


# ---------------------------------------------------------------------------
# REDI-04C — live WorkerPool acceptance runner (dry-run + composition)
# ---------------------------------------------------------------------------


def _load_live_pool_runner():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_redi04c_runner_pack",
        REPO_ROOT / "scripts" / "run_redi_04_live_pool.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def v15_dry_run_runner_composition_full_acceptance():
    import re as _re
    from redi_lab.domain import RunStatus
    from redi_lab.factory.controller import MinimalFactoryController
    from redi_lab.worker_selection import (
        SELECTION_KIND_INITIAL, SELECTION_KIND_WORKER_FALLBACK,
    )

    runner = _load_live_pool_runner()
    deps, pool, product, task = runner.build_run_deps(live_builder=None)  # dry-run B
    ordered_ids = [str(e.profile.id) for e in
                   sorted(pool.entries, key=lambda e: (e.preference_rank, str(e.profile.id)))]
    assert ordered_ids == [runner.WORKER_A_ID, runner.WORKER_B_ID]

    outcome = MinimalFactoryController.run(product, task, deps)
    assert outcome.final_status == RunStatus.COMPLETED and outcome.verified is True
    assert outcome.escalation is False and outcome.attempts == 2
    att = _builder_attempts(outcome.db_path)
    assert [a["worker_id"] for a in att] == [runner.WORKER_A_ID, runner.WORKER_B_ID]
    sel0 = att[0]["metadata"]["worker_selection"]
    sel1 = att[1]["metadata"]["worker_selection"]
    assert sel0["selection_kind"] == SELECTION_KIND_INITIAL
    assert sel1["selection_kind"] == SELECTION_KIND_WORKER_FALLBACK
    assert sel1["previous_worker_id"] == runner.WORKER_A_ID
    fb = att[1]["metadata"]["retry_feedback_summary"]
    assert fb["boundary"] == "public_test_execution"
    assert "NameError: name 'Optional' is not defined" in fb["diagnostic"]
    assert "same_worker_retry" not in (sel0["selection_kind"], sel1["selection_kind"])
    # context isolation for Worker B's rendered prompt
    fb_json = json.dumps(fb, sort_keys=True)
    assert "slugify_pkg_hidden_tests" not in fb_json and "test_hidden" not in fb_json


def v16_runner_env_gates_fail_closed_and_live_binding_structural():
    import importlib
    import redi_lab.runtime.live_provider_adapter as lpa

    runner = _load_live_pool_runner()
    # env gates (executed twice, both must be blocked and print compact banners)
    old_provider = os.environ.pop("REDI_LIVE_PROVIDER", None)
    old_key = os.environ.pop("REDI_OPENAI_API_KEY", None)
    try:
        buf = io.StringIO()
        _stdout = sys.stdout
        sys.stdout = buf
        try:
            rc = runner.main()
        finally:
            sys.stdout = _stdout
        assert rc == 0
        assert "REDI-04 LIVE BLOCKED: REDI_LIVE_PROVIDER not set" in buf.getvalue()
        os.environ["REDI_LIVE_PROVIDER"] = "1"
        buf = io.StringIO()
        sys.stdout = buf
        try:
            rc = runner.main()
        finally:
            sys.stdout = _stdout
        assert rc == 0
        assert "REDI-04 LIVE BLOCKED: REDI_OPENAI_API_KEY not set" in buf.getvalue()
        assert "sk-" not in buf.getvalue()
    finally:
        if old_provider is not None:
            os.environ["REDI_LIVE_PROVIDER"] = old_provider
        else:
            os.environ.pop("REDI_LIVE_PROVIDER", None)

    # live composition is structural only: adapter bound as Worker B, zero probes
    calls = {"n": 0}
    original = lpa._sdk_available

    def _counting():
        calls["n"] += 1
        return True

    lpa._sdk_available = _counting
    try:
        adapter = lpa.LiveProviderAdapter(config=lpa.LiveProviderConfig(env_key_name="REDI_OPENAI_API_KEY"))
        deps, pool, product, task = runner.build_run_deps(live_builder=adapter, model="gpt-4o-mini")
    finally:
        lpa._sdk_available = original
    from redi_lab.runtime.live_provider_adapter import LiveProviderAdapter

    assert isinstance(deps.builder_runners[runner.WORKER_B_ID], LiveProviderAdapter)
    assert deps.builder_runners[runner.WORKER_B_ID].config.model == "gpt-4o-mini"
    assert calls["n"] == 0


def main() -> int:
    print("=" * 64)
    print("REDI-04A VALIDATIONS — Station & Worker Pools (deterministic)")
    print("=" * 64)
    check("v01 WorkerPool contract; canonical registry stays 14", v01_pool_contract_and_registry_frozen)
    check("v01b failing-artifact fixture raises NameError at import", _FAILING_SANITY)
    check("v02 deterministic selection: lowest rank, repeatable", v02_selection_deterministic_lowest_rank)
    check("v03 eligibility/availability rejection with skip trace", v03_eligibility_and_availability_rejection)
    check("v04 canonical experiment: A fails → B fallback → VERIFIED", v04_canonical_fallback_experiment)
    check("v05 first-worker success: second worker never runs", v05_first_worker_success_short_circuits)
    check("v06 same-worker retry vs fallback distinction + escalation", v06_same_worker_retry_vs_fallback_and_exhaustion)
    check("v07 no eligible worker → blocked + exhaustion evidence", v07_no_eligible_worker_blocked_with_evidence)
    check("v08 station/product identity stable across worker swap", v08_station_and_product_identity_stable_across_worker_change)
    check("v09 StationContract free of provider-specific fields", v09_station_contract_has_no_provider_fields)
    check("v10 replay reconstructs worker transitions; zero providers", v10_replay_reconstructs_worker_transitions_zero_providers)
    check("v11 pool snapshot hash deterministic + sensitive", v11_pool_snapshot_hash_deterministic_and_sensitive)
    check("v12 plan binds pool provenance only when pooled", v12_plan_binds_pool_provenance_only_when_pooled)
    check("v13 crash after A → resume falls back to B → verified", v13_crash_after_a_resume_falls_back_to_b_and_verifies)
    check("v14 resume uses persisted pool over drifted config", v14_resume_uses_persisted_pool_over_drifted_config)
    check("v15 04C runner dry-run: A fail → fallback B → VERIFIED", v15_dry_run_runner_composition_full_acceptance)
    check("v16 04C runner env gates fail-closed; live binding structural", v16_runner_env_gates_fail_closed_and_live_binding_structural)
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
