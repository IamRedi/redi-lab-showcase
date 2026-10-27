"""REDI-04A — station & worker pool foundation tests (deterministic, offline).

Proves: station/worker separation, deterministic selection, eligibility
rejection, first-worker success short-circuit, failure → different-worker
fallback, same-worker retry vs fallback distinction, pool exhaustion,
per-attempt evidence with selection reasons, and replay reconstruction —
with zero provider involvement.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
from redi_lab.factory.controller import MinimalFactoryController, RunDeps, make_canonical_slug_fixture_refs
from redi_lab.replay import ReplayReader
from redi_lab.runtime.contracts import NodeRunResult, PatchEdit, WorkerRunner
from redi_lab.runtime.fake_worker import FakeScriptedWorker, fake_reviewer_pass
from redi_lab.worker_selection import (
    SELECTION_KIND_INITIAL,
    SELECTION_KIND_SAME_WORKER_RETRY,
    SELECTION_KIND_WORKER_FALLBACK,
    SELECTION_POLICY_ID,
    resolve_worker,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Deterministic artifacts -----------------------------------------------------
_CORRECT_PY = (
    '"""Slugify package."""\n\n'
    "from __future__ import annotations\n"
    "import unicodedata\n"
    "import re\n"
    "from typing import Optional\n"
    "\n\n"
    "def slugify(text: str, *, maxlen: Optional[int] = None, separator: str = \"-\") -> str:\n"
    "    if not isinstance(text, str):\n"
    "        return text\n"
    "    t = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('utf-8').lower()\n"
    "    t = re.sub(r\"[^a-z0-9]+\", separator, t)\n"
    "    t = t.strip(separator)\n"
    "    if maxlen is not None and isinstance(maxlen, int) and 0 < maxlen < len(t):\n"
    "        t = t[:maxlen]\n"
    "    return t\n"
)

# Compiles, but crashes at import: NameError (missing Optional import) —
# the exact failure shape from the real L8 live run.
_FAILING_PY = (
    "import unicodedata\n"
    "import re\n"
    "\n\n"
    "def slugify(text: str, *, maxlen: Optional[int] = None, separator: str = \"-\") -> str:\n"
    "    if not isinstance(text, str):\n"
    "        return text\n"
    "    t = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('utf-8').lower()\n"
    "    t = re.sub(r\"[^a-z0-9]+\", separator, t)\n"
    "    t = t.strip(separator)\n"
    "    if maxlen is not None and isinstance(maxlen, int) and 0 < maxlen < len(t):\n"
    "        t = t[:maxlen]\n"
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
    builder_station = StationContract(
        role="builder",
        required_output_kinds=["patch_edit"],
        allowed_tool_classes=[],
        applicable_task_classes=["software_patch"],
    )
    reviewer_station = StationContract(role="reviewer", required_output_kinds=["verdict"], allowed_tool_classes=[])
    return product, task, builder_station, reviewer_station


def _profile(worker_id: str, *, rank_station: str | None = "builder_station", version: str = "v1",
             availability: str | None = "available") -> WorkerProfile:
    eligibility = [StationId(rank_station)] if rank_station else []
    return WorkerProfile(
        id=WorkerId(worker_id),
        display_name=worker_id,
        version=version,
        availability_status=availability,
        station_eligibility=eligibility,
        provider_opaque_reference=f"opaque:{worker_id}",
    )


def _builder_pool(station: StationContract, entries: list[tuple[str, int, str | None, str | None]]) -> WorkerPool:
    return WorkerPool(
        station_id=station.id,
        role="builder",
        entries=[
            PoolEntry(profile=_profile(wid, rank_station=elig, version=ver, availability=av), preference_rank=rank)
            for (wid, rank, ver, av, elig) in [
                (wid, rank, ver if ver is not None else "v1", av, elig if elig != "builder_station" else str(station.id))
                for (wid, rank, ver, av, elig) in entries
            ]
        ],
    )


@dataclass
class _PatchWorker(WorkerRunner):
    """Scripted worker bound to one pool profile; returns one fixed patch set."""

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


def _pool_deps(station: StationContract, pool: WorkerPool, runners: dict[str, WorkerRunner],
               max_retries: int = 1) -> RunDeps:
    refs = make_canonical_slug_fixture_refs(_REPO_ROOT)
    _, _, _, reviewer_station = _specs()
    return RunDeps(
        builder_worker=FakeScriptedWorker.case_fix_slugify_builder(),  # unused when pool configured
        reviewer_worker=fake_reviewer_pass(),
        builder_station=station,
        reviewer_station=reviewer_station,
        fixture_root=refs["fixture_root"],
        hidden_test_root=refs["hidden_test_root"],
        allowed_scope_prefixes=refs["allowed_scope_prefixes"],
        gate_spec=refs["gate_spec"],
        max_retries=max_retries,
        builder_pool=pool,
        builder_runners=runners,
    )


def _fetch_builder_attempts(db_path: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT payload_json FROM evidence_entries WHERE entity_type='RunEvidence' ORDER BY global_sequence"
        ).fetchall()
        out = []
        for (pj,) in rows:
            d = json.loads(pj)
            if d.get("node_id") == "N_BUILDER_0":
                out.append(d)
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Unit: deterministic selection policy
# ---------------------------------------------------------------------------


def test_04a_selection_prefers_lowest_rank_and_is_repeatable():
    _, task, station, _ = _specs()
    pool = _builder_pool(station, [("worker_a", 0, "v1", "available", "builder_station"),
                                   ("worker_b", 1, "v1", "available", "builder_station")])
    runners = {"worker_a": object(), "worker_b": object()}
    picks = [resolve_worker(pool=pool, runners=runners, station=station, task=task, attempt_index=0) for _ in range(5)]
    assert all(p is not None for p in picks)
    assert {str(p.profile.id) for p in picks} == {"worker_a"}
    assert picks[0].policy_id == SELECTION_POLICY_ID
    assert picks[0].selection_kind == SELECTION_KIND_INITIAL
    assert picks[0].to_evidence() == picks[1].to_evidence()


def test_04a_ineligible_and_unavailable_workers_are_rejected():
    _, task, station, _ = _specs()
    other = StationId("station_elsewhere")
    pool = _builder_pool(station, [
        ("worker_foreign", 0, "v1", "available", str(other)),   # ineligible for this station
        ("worker_retired", 1, "v1", "deprecated", "builder_station"),  # unavailable
        ("worker_backup", 2, "v1", "available", "builder_station"),
    ])
    runners = {f"worker_{n}": object() for n in ("foreign", "retired", "backup")}
    sel = resolve_worker(pool=pool, runners=runners, station=station, task=task, attempt_index=0)
    assert sel is not None
    assert str(sel.profile.id) == "worker_backup"
    skipped_ids = [s["worker_id"] for s in sel.skipped]
    assert skipped_ids == ["worker_foreign", "worker_retired"]
    assert "not eligible" in sel.skipped[0]["reason"]
    assert "unavailable" in sel.skipped[1]["reason"]


def test_04a_task_class_mismatch_rejected():
    _, task, station, _ = _specs()
    pool = _builder_pool(station, [("worker_a", 0, "v1", "available", "builder_station")])
    mismatched = TaskContract(goal="docs task", metadata={"task_class": "docs_authoring"})
    assert resolve_worker(pool=pool, runners={"worker_a": object()}, station=station, task=mismatched,
                          attempt_index=0) is None


def test_04a_missing_runner_binding_rejected():
    _, task, station, _ = _specs()
    pool = _builder_pool(station, [("worker_a", 0, "v1", "available", "builder_station")])
    assert resolve_worker(pool=pool, runners={}, station=station, task=task, attempt_index=0) is None


# ---------------------------------------------------------------------------
# Controller: canonical experiment + ladder semantics
# ---------------------------------------------------------------------------


def test_04a_canonical_fallback_experiment_worker_a_fails_worker_b_verifies():
    product, task, station, _ = _specs()
    worker_a, worker_b = _PatchWorker(content=_FAILING_PY), _PatchWorker(content=_CORRECT_PY)
    pool = _builder_pool(station, [("worker_a_small", 0, "v1", "available", "builder_station"),
                                   ("worker_b_correct", 1, "v1", "available", "builder_station")])
    deps = _pool_deps(station, pool, {"worker_a_small": worker_a, "worker_b_correct": worker_b})

    outcome = MinimalFactoryController.run(product, task, deps)
    assert outcome.final_status == RunStatus.COMPLETED
    assert outcome.verified is True
    assert outcome.escalation is False
    assert worker_a.calls == 1 and worker_b.calls == 1

    attempts = _fetch_builder_attempts(outcome.db_path)
    assert len(attempts) == 2
    a0, a1 = attempts
    assert a0["worker_id"] == "worker_a_small"
    assert a1["worker_id"] == "worker_b_correct"
    sel0, sel1 = a0["metadata"]["worker_selection"], a1["metadata"]["worker_selection"]
    assert sel0["selection_kind"] == SELECTION_KIND_INITIAL
    assert sel0["policy_id"] == "deterministic_v0"
    assert sel1["selection_kind"] == SELECTION_KIND_WORKER_FALLBACK
    assert sel1["previous_worker_id"] == "worker_a_small"
    assert "worker_a_small" in sel1["reason"]
    # attempt 1's prompt feedback names the failed boundary from attempt 0
    assert a1["metadata"]["retry_feedback_summary"]["boundary"] == "public_test_execution"
    # gate outcomes per attempt
    conn = sqlite3.connect(outcome.db_path)
    try:
        gate_rows = conn.execute(
            "SELECT payload_json FROM evidence_entries WHERE entity_type='GateResult' ORDER BY global_sequence"
        ).fetchall()
        seq = [(json.loads(r[0])["gate_identity"], json.loads(r[0])["status"]) for r in gate_rows]
    finally:
        conn.close()
    assert seq == [
        ("allowed_file_change_scope", "pass"),
        ("python_syntax_compile", "pass"),
        ("public_test_execution", "fail"),
        ("allowed_file_change_scope", "pass"),
        ("python_syntax_compile", "pass"),
        ("public_test_execution", "pass"),
        ("hidden_test_execution", "pass"),
    ]
    # PRODUCT_VERIFIED persisted
    conn = sqlite3.connect(outcome.db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM evidence_entries WHERE payload_json LIKE '%PRODUCT_VERIFIED%'").fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_04a_first_worker_success_second_worker_never_runs():
    product, task, station, _ = _specs()
    worker_a, worker_b = _PatchWorker(content=_CORRECT_PY), _PatchWorker(content=_CORRECT_PY)
    pool = _builder_pool(station, [("worker_a_ok", 0, "v1", "available", "builder_station"),
                                   ("worker_b_never", 1, "v1", "available", "builder_station")])
    deps = _pool_deps(station, pool, {"worker_a_ok": worker_a, "worker_b_never": worker_b})

    outcome = MinimalFactoryController.run(product, task, deps)
    assert outcome.final_status == RunStatus.COMPLETED
    assert outcome.verified is True
    assert outcome.attempts == 1
    assert worker_a.calls == 1 and worker_b.calls == 0
    attempts = _fetch_builder_attempts(outcome.db_path)
    assert len(attempts) == 1
    assert attempts[0]["metadata"]["worker_selection"]["selection_kind"] == SELECTION_KIND_INITIAL


def test_04a_single_worker_failure_is_same_worker_retry_then_escalation():
    product, task, station, _ = _specs()
    worker_a = _PatchWorker(content=_FAILING_PY)
    pool = _builder_pool(station, [("worker_a_only", 0, "v1", "available", "builder_station")])
    deps = _pool_deps(station, pool, {"worker_a_only": worker_a})

    outcome = MinimalFactoryController.run(product, task, deps)
    assert outcome.final_status == RunStatus.FAILED
    assert outcome.escalation is True
    assert worker_a.calls == 2  # retried, not replaced
    attempts = _fetch_builder_attempts(outcome.db_path)
    kinds = [a["metadata"]["worker_selection"]["selection_kind"] for a in attempts]
    assert kinds == [SELECTION_KIND_INITIAL, SELECTION_KIND_SAME_WORKER_RETRY]
    assert attempts[1]["metadata"]["worker_selection"]["previous_worker_id"] == "worker_a_only"


def test_04a_no_eligible_worker_blocked_with_exhaustion_evidence():
    product, task, station, _ = _specs()
    worker_foreign = _PatchWorker(content=_CORRECT_PY)
    pool = _builder_pool(station, [("worker_foreign", 0, "v1", "available", "station_elsewhere")])
    deps = _pool_deps(station, pool, {"worker_foreign": worker_foreign})

    outcome = MinimalFactoryController.run(product, task, deps)
    assert outcome.final_status == RunStatus.BLOCKED
    assert outcome.escalation is True
    assert worker_foreign.calls == 0
    assert _fetch_builder_attempts(outcome.db_path) == []
    conn = sqlite3.connect(outcome.db_path)
    try:
        fr_rows = conn.execute(
            "SELECT payload_json FROM evidence_entries WHERE entity_type='FailureRecord'"
        ).fetchall()
        assert len(fr_rows) == 1
        fr = json.loads(fr_rows[0][0])
        assert fr["category"] == "capability_insufficiency"
        assert fr["origin"] == "worker_pool_exhausted"
        ev = conn.execute(
            "SELECT payload_json FROM evidence_entries WHERE entity_type='LifecycleEvent' AND payload_json LIKE '%worker_pool_exhausted%'"
        ).fetchall()
        assert len(ev) == 1
    finally:
        conn.close()


def test_04a_station_and_task_identity_unchanged_when_workers_change():
    product, task, station, _ = _specs()
    worker_a, worker_b = _PatchWorker(content=_FAILING_PY), _PatchWorker(content=_CORRECT_PY)
    pool_ab = _builder_pool(station, [("worker_a_small", 0, "v1", "available", "builder_station"),
                                      ("worker_b_correct", 1, "v1", "available", "builder_station")])
    pool_ba = _builder_pool(station, [("worker_a_small", 1, "v1", "available", "builder_station"),
                                      ("worker_b_correct", 0, "v1", "available", "builder_station")])
    out1 = MinimalFactoryController.run(product, task, _pool_deps(station, pool_ab, {"worker_a_small": worker_a, "worker_b_correct": worker_b}))
    out2 = MinimalFactoryController.run(product, task, _pool_deps(station, pool_ba, {"worker_a_small": worker_a, "worker_b_correct": worker_b}))
    conn1 = sqlite3.connect(out1.db_path)
    conn2 = sqlite3.connect(out2.db_path)
    try:
        plan1 = json.loads(conn1.execute("SELECT payload_json FROM evidence_entries WHERE global_sequence=2").fetchone()[0])
        plan2 = json.loads(conn2.execute("SELECT payload_json FROM evidence_entries WHERE global_sequence=2").fetchone()[0])
        assert plan1["payload"]["builder_station_id"] == plan2["payload"]["builder_station_id"]
        assert plan1["payload"]["task_id"] == plan2["payload"]["task_id"]
        assert str(task.id) == plan1["payload"]["task_id"] == plan2["payload"]["task_id"]
        assert str(station.id) == plan1["payload"]["builder_station_id"]
        accepted1 = json.loads(conn1.execute(
            "SELECT payload_json FROM evidence_entries WHERE global_sequence=1").fetchone()[0])["payload"]["product_id"]
        accepted2 = json.loads(conn2.execute(
            "SELECT payload_json FROM evidence_entries WHERE global_sequence=1").fetchone()[0])["payload"]["product_id"]
        assert accepted1 == accepted2 == str(product.id)
    finally:
        conn1.close()
        conn2.close()


def test_04a_no_provider_fields_leak_into_station_contract():
    station = StationContract(role="builder", required_output_kinds=["patch_edit"], allowed_tool_classes=[])
    blob = json.dumps(station.__dict__, default=str).lower()
    for forbidden in ("openai", "gpt-", "gemini", "anthropic", "api_key", "pricing", "temperature"):
        assert forbidden not in blob
    fields = {f for f in StationContract.__dataclass_fields__}
    for forbidden in ("model", "provider", "pricing"):
        assert not any(forbidden == f for f in fields)


# ---------------------------------------------------------------------------
# Replay: reconstruct worker transitions deterministically, zero providers
# ---------------------------------------------------------------------------


def test_04a_replay_reconstructs_worker_transitions_deterministically():
    product, task, station, _ = _specs()
    worker_a, worker_b = _PatchWorker(content=_FAILING_PY), _PatchWorker(content=_CORRECT_PY)
    pool = _builder_pool(station, [("worker_a_small", 0, "v1", "available", "builder_station"),
                                   ("worker_b_correct", 1, "v1", "available", "builder_station")])
    deps = _pool_deps(station, pool, {"worker_a_small": worker_a, "worker_b_correct": worker_b})
    outcome = MinimalFactoryController.run(product, task, deps)

    def _snapshot():
        reader = ReplayReader(outcome.db_path)
        try:
            res = reader.replay_factory_run(outcome.factory_run_id and __import__("redi_lab.domain", fromlist=["FactoryRunId"]).FactoryRunId(outcome.factory_run_id))
        finally:
            reader.close()
        builder_evs = [e for e in res.run_evidences if getattr(e, "node_id", "") == "N_BUILDER_0"]
        selections = [dict(e.metadata["worker_selection"]) for e in builder_evs]
        return res, selections

    import redi_lab.runtime.live_provider_adapter as lpa
    calls = {"n": 0}
    original = lpa._sdk_available

    def _counting():
        calls["n"] += 1
        return False

    lpa._sdk_available = _counting
    try:
        r1, sel1 = _snapshot()
        r2, sel2 = _snapshot()
    finally:
        lpa._sdk_available = original

    assert r1.integrity_status == "OK" == r2.integrity_status
    assert r1.observed_run_status == r2.observed_run_status == "completed"
    assert r1.final_verification_state is True
    assert sel1 == sel2
    assert [s["worker_id"] for s in sel1] == ["worker_a_small", "worker_b_correct"]
    assert [s["selection_kind"] for s in sel1] == [SELECTION_KIND_INITIAL, SELECTION_KIND_WORKER_FALLBACK]
    assert calls["n"] == 0  # zero provider probes during replay
