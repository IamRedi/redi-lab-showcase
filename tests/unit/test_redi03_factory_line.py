"""REDI-03 minimal factory-line test suite — 32 enumerated categories.

Covers FSM, bounded retry, escalation, reviewer independence, deterministic gates,
crash/resume, replay, fixture integrity, provider independence audit, secret scan.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from redi_lab.domain import (
    ArtifactRef,
    ContextManifest,
    ExperimentManifest,
    FactoryRun,
    FailureCategory,
    FailureRecord,
    GateResult,
    GateStatus,
    HandoffManifest,
    LifecycleEvent,
    LifecycleEventType,
    MonetaryCost,
    ProductDAG,
    ProductRecipe,
    ProductSpec,
    Requirement,
    RiskTier,
    RunEvidence,
    RunStatus,
    SCHEMA_VERSION,
    StationContract,
    TaskContract,
    WorkerProfile,
    domain_from_json,
    domain_to_json,
)
from redi_lab.factory.controller import (
    FinalRunOutcome,
    MinimalFactoryController,
    RunDeps,
    make_canonical_slug_fixture_refs,
    SimulatedCrash,
)
from redi_lab.factory.isolation import (
    IsolatedWorkspace,
    ScopeViolation,
    apply_patches,
    baseline_file_hashes,
)
from redi_lab.factory.builder_context import build_builder_context, build_reviewer_context
from redi_lab.gates import DeterministicGateEngine, GateSpec
from redi_lab.runtime.contracts import NodeRunResult, PatchEdit, ReviewerVerdict, WorkerRunner
from redi_lab.runtime.fake_worker import (
    FakeScriptedWorker,
    _CORRECT_SLUGIFY_PY,
    fake_reviewer_escalate,
    fake_reviewer_fail,
    fake_reviewer_pass,
)
from redi_lab.replay import ReplayReader

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_ALLOWED: tuple[str, ...] = ("slugify", "tests")


def _make_specs(description: str = "REDI-03 canonical slugify implementation"):
    product = ProductSpec(
        requested_outcome=description,
        requirements=[Requirement(text=description, kind="functional")],
        constraints=[Requirement(text="Python package", kind="constraint")],
        unresolved_requirements=[],
        risk_tier=RiskTier.LOW,
    )
    task = TaskContract(goal="Implement correct slugify per public + hidden pytest suite")
    builder_station = StationContract(role="builder", required_output_kinds=["patch"])
    reviewer_station = StationContract(role="reviewer", required_output_kinds=["verdict"])
    return product, task, builder_station, reviewer_station


def _make_default_deps(
    *,
    builder_case: str = "fix_slugify_builder",
    reviewer_factory: Any = None,
    max_retries: int = 1,
) -> RunDeps:
    refs = make_canonical_slug_fixture_refs(_REPO_ROOT)
    if builder_case == "fix_slugify_builder":
        builder = FakeScriptedWorker.case_fix_slugify_builder()
    elif builder_case == "syntax_then_fix_builder":
        builder = FakeScriptedWorker.case_syntax_then_fix_builder()
    else:
        builder = FakeScriptedWorker.case_always_bad_syntax_builder()
    reviewer = reviewer_factory() if reviewer_factory is not None else fake_reviewer_pass()
    _, _, sb, sr = _make_specs()
    return RunDeps(
        builder_worker=builder,
        reviewer_worker=reviewer,
        builder_station=sb,
        reviewer_station=sr,
        fixture_root=refs["fixture_root"],
        hidden_test_root=refs["hidden_test_root"],
        allowed_scope_prefixes=refs["allowed_scope_prefixes"],
        gate_spec=refs["gate_spec"],
        builder_case_id=builder_case,
        reviewer_case_id="reviewer",
        max_retries=max_retries,
    )


def _fetch_events(conn: sqlite3.Connection) -> list[str]:
    cur = conn.execute(
        "SELECT e.payload_json FROM evidence_entries e WHERE e.entity_type = 'LifecycleEvent' ORDER BY e.global_sequence ASC"
    )
    return [domain_from_json(row[0]).event_type.value for row in cur.fetchall()]


def _fetch_run_row(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, experiment_id, payload_json, payload_sha256, final_status, verified, closed_at, created_at "
        "FROM factory_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    conn.row_factory = None
    return dict(row) if row else {}


# ===========================================================================
# 1. Factory-line initialization
# ===========================================================================
def test_01_factory_line_initialization():
    deps = _make_default_deps()
    assert isinstance(deps.builder_worker, FakeScriptedWorker)
    assert isinstance(deps.builder_station, StationContract)
    assert isinstance(deps.reviewer_station, StationContract)
    assert deps.fixture_root.exists()
    assert (deps.fixture_root / "slugify" / "__init__.py").is_file()
    assert (deps.hidden_test_root / "test_slugify_hidden.py").is_file()
    assert isinstance(deps.gate_spec, GateSpec)


# ===========================================================================
# 2. ProductSpec → Task binding
# ===========================================================================
def test_02_product_task_roundtrip_provider_independent():
    product, task, sb, sr = _make_specs()
    j_p = domain_to_json(product); j_t = domain_to_json(task)
    assert domain_from_json(j_p).requested_outcome == product.requested_outcome
    assert domain_from_json(j_t).goal == task.goal
    low = (j_p + j_t + domain_to_json(sb) + domain_to_json(sr)).lower()
    for token in ("openai", "claude", "gemini", "api_key"):
        assert token not in low


# ===========================================================================
# 3. Isolated workspace
# ===========================================================================
def test_03_isolated_workspace_create_then_destroy():
    refs = make_canonical_slug_fixture_refs(_REPO_ROOT)
    ws = IsolatedWorkspace.create(fixture_root=refs["fixture_root"], allowed_scope_prefixes=list(_DEFAULT_ALLOWED))
    assert ws.root.exists()
    assert (ws.root / "slugify" / "__init__.py").is_file()
    assert ws.allowed_scope_rel_prefixes == _DEFAULT_ALLOWED
    ws.destroy()
    assert not ws.root.exists()


# ===========================================================================
# 4. Builder bounded context
# ===========================================================================
def test_04_builder_context_roundtrip_and_scrubbing():
    product, task, sb, sr = _make_specs()
    refs = make_canonical_slug_fixture_refs(_REPO_ROOT)
    with IsolatedWorkspace.create(fixture_root=refs["fixture_root"], allowed_scope_prefixes=list(_DEFAULT_ALLOWED)) as ws:
        ctx = build_builder_context(
            task=task,
            station=sb,
            product=product,
            fixture_root=ws.root,
            include_globs=(),
            public_test_globs=(),
            additional_sources=(),
        )
    assert isinstance(ctx, ContextManifest)
    ctx2 = domain_from_json(domain_to_json(ctx))
    assert isinstance(ctx2, ContextManifest)
    j = domain_to_json(ctx).lower()
    for bad in ("sk-", "'secret'", "apikey", "sk_live"):
        assert bad not in j


# ===========================================================================
# 5. Hidden tests not in builder context
# ===========================================================================
def test_05_builder_context_excludes_hidden():
    product, task, sb, sr = _make_specs()
    refs = make_canonical_slug_fixture_refs(_REPO_ROOT)
    with IsolatedWorkspace.create(fixture_root=refs["fixture_root"], allowed_scope_prefixes=list(_DEFAULT_ALLOWED)) as ws:
        ctx = build_builder_context(
            task=task,
            station=sb,
            product=product,
            fixture_root=ws.root,
            include_globs=(),
            public_test_globs=(),
            additional_sources=(),
        )
    locs: list[str] = []
    for field in ("project_inputs", "source_references", "explicit_exclusions"):
        for ref in getattr(ctx, field, ()) or ():
            if hasattr(ref, "location"):
                locs.append(str(ref.location).lower())
    assert not any("hidden" in x for x in locs), locs


# ===========================================================================
# 6. Builder structured PatchEdit
# ===========================================================================
def test_06_patchedit_structured_contract():
    p = PatchEdit(rel_path="slugify/__init__.py", action="write", content="x = 1\n")
    assert ".." not in p.rel_path.split("/")
    assert not p.rel_path.startswith("/")
    j = json.dumps(p.__dict__)
    assert json.loads(j)["action"] == "write"


# ===========================================================================
# 7. Patch application positive
# ===========================================================================
def test_07_patch_application_positive():
    refs = make_canonical_slug_fixture_refs(_REPO_ROOT)
    with IsolatedWorkspace.create(fixture_root=refs["fixture_root"], allowed_scope_prefixes=list(_DEFAULT_ALLOWED)) as ws:
        t = ws.root / "slugify" / "__init__.py"
        before = t.read_text()
        ok, viols = apply_patches(ws, [PatchEdit(rel_path="slugify/__init__.py", action="write", content="# PATCHED\n")])
        assert ok, viols
        assert t.read_text() == "# PATCHED\n"
        assert before != "# PATCHED\n"


# ===========================================================================
# 8. Scope violation rejected (apply_patches returns False)
# ===========================================================================
def test_08_scope_violation_rejected():
    refs = make_canonical_slug_fixture_refs(_REPO_ROOT)
    with IsolatedWorkspace.create(fixture_root=refs["fixture_root"], allowed_scope_prefixes=list(_DEFAULT_ALLOWED)) as ws:
        ok, viols = apply_patches(ws, [PatchEdit(rel_path="../outside/x.py", action="write", content="x\n")])
        assert ok is False
        assert len(viols) >= 1
        assert any(".." in str(v.args) or ".." in str(v.path if hasattr(v, "path") else "") or ".." in str(v) for v in viols)


# ===========================================================================
# 9. Deterministic gate PASS
# ===========================================================================
def test_09_deterministic_gate_pass():
    refs = make_canonical_slug_fixture_refs(_REPO_ROOT)
    with IsolatedWorkspace.create(fixture_root=refs["fixture_root"], allowed_scope_prefixes=list(_DEFAULT_ALLOWED)) as ws:
        ok, viols = apply_patches(ws, [PatchEdit(rel_path="slugify/__init__.py", action="write", content=_CORRECT_SLUGIFY_PY)])
        assert ok, viols
        gate_spec = refs["gate_spec"]
        gate_spec.baseline_hashes = dict(baseline_file_hashes(ws.root))
        results = DeterministicGateEngine(gate_spec).run_all(ws.root)
    assert len(results) == 4, [r.gate_identity for r in results]
    for r in results:
        assert r.status == GateStatus.PASS, f"{r.gate_identity} fail reasons={getattr(r, 'failure_reasons', None)}"


# ===========================================================================
# 10. Deterministic gate FAIL (stub not patched)
# ===========================================================================
def test_10_deterministic_gate_fail():
    refs = make_canonical_slug_fixture_refs(_REPO_ROOT)
    with IsolatedWorkspace.create(fixture_root=refs["fixture_root"], allowed_scope_prefixes=list(_DEFAULT_ALLOWED)) as ws:
        gate_spec = refs["gate_spec"]
        gate_spec.baseline_hashes = dict(baseline_file_hashes(ws.root))
        results = DeterministicGateEngine(gate_spec).run_all(ws.root)
    assert any(r.status != GateStatus.PASS for r in results)


# ===========================================================================
# 11. Gate timeout tiny -> at least one non-pass
# ===========================================================================
def test_11_gate_timeout_tiny():
    refs = make_canonical_slug_fixture_refs(_REPO_ROOT)
    tiny = GateSpec(
        public_test_cmd=refs["gate_spec"].public_test_cmd,
        hidden_test_cmd=refs["gate_spec"].hidden_test_cmd,
        hidden_test_cwd=refs["gate_spec"].hidden_test_cwd,
        hidden_test_env_extra={},
        per_gate_timeout_s=1e-7,
        output_cap_bytes=refs["gate_spec"].output_cap_bytes,
        allowed_scope_prefixes=refs["allowed_scope_prefixes"],
    )
    with IsolatedWorkspace.create(fixture_root=refs["fixture_root"], allowed_scope_prefixes=list(_DEFAULT_ALLOWED)) as ws:
        ok, viols = apply_patches(ws, [PatchEdit(rel_path="slugify/__init__.py", action="write", content=_CORRECT_SLUGIFY_PY)])
        assert ok
        tiny.baseline_hashes = dict(baseline_file_hashes(ws.root))
        results = DeterministicGateEngine(tiny).run_all(ws.root)
    assert any(r.status != GateStatus.PASS for r in results)


# ===========================================================================
# 12. Reviewer independent execution (two NODE_READY)
# ===========================================================================
def test_12_reviewer_independent_execution():
    product, task, _, _ = _make_specs()
    deps = _make_default_deps(builder_case="fix_slugify_builder", reviewer_factory=fake_reviewer_pass)
    out = MinimalFactoryController.run(product, task, deps)
    assert out.final_status == RunStatus.COMPLETED, (out.final_status, out.reviewer_verdict, out.final_gate_count)
    conn = sqlite3.connect(out.db_path)
    try:
        evs = _fetch_events(conn)
        assert evs.count("NODE_READY") >= 2
    finally:
        conn.close()


# ===========================================================================
# 13. Reviewer context: build_reviewer_context returns dict, no secrets
# ===========================================================================
def test_13_reviewer_context_scrubs_secrets_and_builder_reasoning():
    product, task, sb, sr = _make_specs()
    refs = make_canonical_slug_fixture_refs(_REPO_ROOT)
    from redi_lab.domain import GateType
    with IsolatedWorkspace.create(fixture_root=refs["fixture_root"], allowed_scope_prefixes=list(_DEFAULT_ALLOWED)) as ws:
        ok, viols = apply_patches(ws, [PatchEdit(rel_path="slugify/__init__.py", action="write", content=_CORRECT_SLUGIFY_PY)])
        assert ok
        gr = GateResult(gate_identity="syntax", gate_type=GateType.DETERMINISTIC, status=GateStatus.PASS)
        rctx = build_reviewer_context(
            product=product,
            task=task,
            station=sr,
            workspace_root_before=ws.root,
            patches_applied=[PatchEdit(rel_path="slugify/__init__.py", action="write", content=_CORRECT_SLUGIFY_PY)],
            deterministic_gate_results=[gr],
            allowed_scope_prefixes=list(_DEFAULT_ALLOWED),
        )
    assert isinstance(rctx, dict)
    low = json.dumps(rctx, default=str).lower()
    for bad in ("sk-", "api_key", "'secret'", "sk_live", "password"):
        assert bad not in low, f"{bad} found in reviewer context"


# ===========================================================================
# 14. Reviewer PASS -> COMPLETED verified=True
# ===========================================================================
def test_14_reviewer_pass_verified_true():
    product, task, _, _ = _make_specs()
    deps = _make_default_deps(builder_case="fix_slugify_builder", reviewer_factory=fake_reviewer_pass)
    out = MinimalFactoryController.run(product, task, deps)
    assert out.final_status == RunStatus.COMPLETED, (out.final_status, out.reviewer_verdict)
    assert out.verified is True
    assert out.reviewer_verdict == ReviewerVerdict.PASS
    conn = sqlite3.connect(out.db_path)
    try:
        assert "PRODUCT_VERIFIED" in _fetch_events(conn)
    finally:
        conn.close()


# ===========================================================================
# 15. Reviewer FAIL -> FAILED, reviewer verdict FAIL
# ===========================================================================
def test_15_reviewer_fail_final_failed():
    product, task, _, _ = _make_specs()
    deps = _make_default_deps(builder_case="fix_slugify_builder", reviewer_factory=fake_reviewer_fail)
    out = MinimalFactoryController.run(product, task, deps)
    assert out.final_status == RunStatus.FAILED, out
    assert out.verified is False
    assert out.reviewer_verdict == ReviewerVerdict.FAIL, out.reviewer_verdict


# ===========================================================================
# 16. Reviewer ESCALATE -> BLOCKED + escalation True
# ===========================================================================
def test_16_reviewer_escalate_blocked():
    product, task, _, _ = _make_specs()
    deps = _make_default_deps(builder_case="fix_slugify_builder", reviewer_factory=fake_reviewer_escalate)
    out = MinimalFactoryController.run(product, task, deps)
    assert out.final_status == RunStatus.BLOCKED, out
    assert out.verified is False
    assert out.reviewer_verdict == ReviewerVerdict.ESCALATE
    assert out.escalation is True


# ===========================================================================
# 17. Builder cannot self-approve: gate exhaustion -> reviewer never called
# ===========================================================================
def test_17_builder_cannot_self_approve_gates_mandatory():
    product, task, _, _ = _make_specs()
    deps = _make_default_deps(
        builder_case="always_bad_syntax_builder",
        reviewer_factory=fake_reviewer_pass,
        max_retries=0,
    )
    rw = deps.reviewer_worker
    out = MinimalFactoryController.run(product, task, deps)
    assert out.final_status in (RunStatus.FAILED, RunStatus.BLOCKED), out.final_status
    counter = getattr(rw, "call_counter", None) or {}
    total = sum(counter.values()) if isinstance(counter, dict) else 0
    assert total == 0, counter


# ===========================================================================
# 18. Bounded retry: RETRY_REQUESTED once, 2 attempts -> completed
# ===========================================================================
def test_18_bounded_retry_once():
    product, task, _, _ = _make_specs()
    deps = _make_default_deps(builder_case="syntax_then_fix_builder", reviewer_factory=fake_reviewer_pass, max_retries=1)
    out = MinimalFactoryController.run(product, task, deps)
    assert out.final_status == RunStatus.COMPLETED, (out.final_status, out.reviewer_verdict)
    assert out.attempts == 2
    conn = sqlite3.connect(out.db_path)
    try:
        evs = _fetch_events(conn)
        assert evs.count("RETRY_REQUESTED") == 1
    finally:
        conn.close()


# ===========================================================================
# 19. Retry exhaustion -> FAILED, exactly 2 attempts
# ===========================================================================
def test_19_retry_exhaustion_failed():
    product, task, _, _ = _make_specs()
    deps = _make_default_deps(builder_case="always_bad_syntax_builder", reviewer_factory=fake_reviewer_pass, max_retries=1)
    out = MinimalFactoryController.run(product, task, deps)
    assert out.final_status == RunStatus.FAILED, out
    assert out.attempts == 2


# ===========================================================================
# 20. Escalation event WORKER_ESCALATION_REQUIRED captured
# ===========================================================================
def test_20_escalation_event_captured():
    product, task, _, _ = _make_specs()
    deps = _make_default_deps(builder_case="always_bad_syntax_builder", reviewer_factory=fake_reviewer_pass, max_retries=1)
    out = MinimalFactoryController.run(product, task, deps)
    conn = sqlite3.connect(out.db_path)
    try:
        assert "WORKER_ESCALATION_REQUIRED" in _fetch_events(conn)
    finally:
        conn.close()


# ===========================================================================
# 21. Canonical event ordering (happy path)
# ===========================================================================
def test_21_canonical_event_ordering():
    product, task, _, _ = _make_specs()
    deps = _make_default_deps(builder_case="fix_slugify_builder", reviewer_factory=fake_reviewer_pass)
    out = MinimalFactoryController.run(product, task, deps)
    conn = sqlite3.connect(out.db_path)
    try:
        ev = _fetch_events(conn)
        assert ev[0] == "PRODUCT_ACCEPTED"
        assert "PLAN_COMPILED" in ev[:2]
        assert ev[-1] == "PRODUCT_VERIFIED"
        i_builder_wc = ev.index("WORKER_COMPLETED")  # first one (builder)
        i_gp = ev.index("GATE_PASSED")
        nrs = [i for i, e in enumerate(ev) if e == "NODE_READY"]
        assert len(nrs) >= 2
        i_reviewer_station_ready = nrs[1]
        assert i_builder_wc < i_gp < i_reviewer_station_ready
    finally:
        conn.close()


# ===========================================================================
# 22. Evidence persisted for every stage
# ===========================================================================
def test_22_evidence_every_stage():
    product, task, _, _ = _make_specs()
    deps = _make_default_deps(builder_case="fix_slugify_builder", reviewer_factory=fake_reviewer_pass)
    out = MinimalFactoryController.run(product, task, deps)
    conn = sqlite3.connect(out.db_path)
    try:
        cur = conn.execute("SELECT entity_type, COUNT(*) FROM evidence_entries GROUP BY entity_type")
        counts = {r[0]: r[1] for r in cur.fetchall()}
        assert counts.get("LifecycleEvent", 0) >= 10, counts
        assert counts.get("RunEvidence", 0) >= 2, counts
        assert counts.get("GateResult", 0) >= 4, counts
        # FactoryRun stored in its own table; verify row exists + closed
        run = _fetch_run_row(conn, out.factory_run_id)
        assert run and run["closed_at"] is not None, f"factory_runs row not closed: {run}"
        fr_cnt = conn.execute("SELECT COUNT(*) FROM factory_runs WHERE id = ?", (out.factory_run_id,)).fetchone()[0]
        assert fr_cnt >= 1
    finally:
        conn.close()


# ===========================================================================
# 23. Crash injection after WORKER_COMPLETED
# ===========================================================================
def test_23_crash_injection_after_worker_completed():
    product, task, _, _ = _make_specs()
    deps = _make_default_deps(builder_case="fix_slugify_builder", reviewer_factory=fake_reviewer_pass)
    deps.crash_inject_after_worker_completed = True
    tmpdb = tempfile.mktemp(suffix=".db", prefix="redi03_t23_")
    try:
        with pytest.raises(SimulatedCrash):
            MinimalFactoryController.run(product, task, deps, db_path=tmpdb)
        conn = sqlite3.connect(tmpdb)
        try:
            evs = _fetch_events(conn)
            assert "WORKER_COMPLETED" in evs
            assert "GATE_PASSED" not in evs
            assert "PRODUCT_VERIFIED" not in evs
        finally:
            conn.close()
    finally:
        if os.path.exists(tmpdb):
            os.unlink(tmpdb)


# ===========================================================================
# 24. Resume: zero duplicate builder re-run (call_counter 0)
# ===========================================================================
def test_24_resume_zero_builder_rerun():
    product, task, _, _ = _make_specs()
    deps = _make_default_deps(builder_case="fix_slugify_builder", reviewer_factory=fake_reviewer_pass)
    deps.crash_inject_after_worker_completed = True
    tmpdb = tempfile.mktemp(suffix=".db", prefix="redi03_t24_")
    try:
        with pytest.raises(SimulatedCrash):
            MinimalFactoryController.run(product, task, deps, db_path=tmpdb)
        conn = sqlite3.connect(tmpdb)
        try:
            factory_run_id = conn.execute("SELECT id FROM factory_runs").fetchone()[0]
        finally:
            conn.close()
        resume_builder = FakeScriptedWorker.case_fix_slugify_builder()
        resume_reviewer = fake_reviewer_pass()
        _, _, sb, sr = _make_specs()
        refs = make_canonical_slug_fixture_refs(_REPO_ROOT)
        deps2 = RunDeps(
            builder_worker=resume_builder,
            reviewer_worker=resume_reviewer,
            builder_station=sb,
            reviewer_station=sr,
            fixture_root=refs["fixture_root"],
            hidden_test_root=refs["hidden_test_root"],
            allowed_scope_prefixes=refs["allowed_scope_prefixes"],
            gate_spec=refs["gate_spec"],
            builder_case_id="fix_slugify_builder",
            reviewer_case_id="reviewer",
            max_retries=1,
        )
        out = MinimalFactoryController.resume(factory_run_id, product, task, deps2, db_path=tmpdb)
        assert out.final_status == RunStatus.COMPLETED, (out.final_status, out.reviewer_verdict)
        total = sum(resume_builder.call_counter.values()) if resume_builder.call_counter else 0
        assert total == 0, resume_builder.call_counter
    finally:
        if os.path.exists(tmpdb):
            os.unlink(tmpdb)


# ===========================================================================
# 25. Resume: zero duplicate builder WORKER_COMPLETED evidence
# ===========================================================================
def test_25_resume_no_duplicate_evidence():
    product, task, _, _ = _make_specs()
    deps = _make_default_deps(builder_case="fix_slugify_builder", reviewer_factory=fake_reviewer_pass)
    deps.crash_inject_after_worker_completed = True
    tmpdb = tempfile.mktemp(suffix=".db", prefix="redi03_t25_")
    try:
        with pytest.raises(SimulatedCrash):
            MinimalFactoryController.run(product, task, deps, db_path=tmpdb)
        conn = sqlite3.connect(tmpdb)
        try:
            pre_wc = _fetch_events(conn).count("WORKER_COMPLETED")
            factory_run_id = conn.execute("SELECT id FROM factory_runs").fetchone()[0]
        finally:
            conn.close()
        _, _, sb, sr = _make_specs()
        refs = make_canonical_slug_fixture_refs(_REPO_ROOT)
        deps2 = RunDeps(
            builder_worker=FakeScriptedWorker.case_fix_slugify_builder(),
            reviewer_worker=fake_reviewer_pass(),
            builder_station=sb,
            reviewer_station=sr,
            fixture_root=refs["fixture_root"],
            hidden_test_root=refs["hidden_test_root"],
            allowed_scope_prefixes=refs["allowed_scope_prefixes"],
            gate_spec=refs["gate_spec"],
            builder_case_id="fix_slugify_builder",
            reviewer_case_id="reviewer",
            max_retries=1,
        )
        out = MinimalFactoryController.resume(factory_run_id, product, task, deps2, db_path=tmpdb)
        conn = sqlite3.connect(tmpdb)
        try:
            evs = _fetch_events(conn)
            post_wc = evs.count("WORKER_COMPLETED")
            # 1 (builder crash) + 1 (reviewer resume) = 2. NOT 3.
            assert post_wc == pre_wc + 1, (pre_wc, post_wc, evs)
            assert evs.count("PRODUCT_VERIFIED") == 1
            row = _fetch_run_row(conn, out.factory_run_id)
            assert row["final_status"] == RunStatus.COMPLETED.value
            assert row["verified"] == 1
            assert row["closed_at"] is not None
        finally:
            conn.close()
    finally:
        if os.path.exists(tmpdb):
            os.unlink(tmpdb)


# ===========================================================================
# 26. Final VERIFIED path SQL columns
# ===========================================================================
def test_26_final_verified_sql_columns():
    product, task, _, _ = _make_specs()
    deps = _make_default_deps(builder_case="fix_slugify_builder", reviewer_factory=fake_reviewer_pass)
    out = MinimalFactoryController.run(product, task, deps)
    conn = sqlite3.connect(out.db_path)
    try:
        row = _fetch_run_row(conn, out.factory_run_id)
        assert row["final_status"] == RunStatus.COMPLETED.value
        assert row["verified"] == 1
        assert row["closed_at"] is not None
    finally:
        conn.close()


# ===========================================================================
# 27. Final FAILED path SQL columns
# ===========================================================================
def test_27_final_failed_sql_columns():
    product, task, _, _ = _make_specs()
    deps = _make_default_deps(builder_case="fix_slugify_builder", reviewer_factory=fake_reviewer_fail)
    out = MinimalFactoryController.run(product, task, deps)
    conn = sqlite3.connect(out.db_path)
    try:
        row = _fetch_run_row(conn, out.factory_run_id)
        assert row["final_status"] == RunStatus.FAILED.value
        assert row["verified"] == 0
        assert row["closed_at"] is not None
    finally:
        conn.close()


# ===========================================================================
# 28. Replay after success
# ===========================================================================
def test_28_replay_after_success():
    product, task, _, _ = _make_specs()
    deps = _make_default_deps(builder_case="fix_slugify_builder", reviewer_factory=fake_reviewer_pass)
    out = MinimalFactoryController.run(product, task, deps)
    conn = sqlite3.connect(out.db_path)
    try:
        reader = ReplayReader(conn)
        from redi_lab.domain import FactoryRunId
        res = reader.replay_factory_run(FactoryRunId(out.factory_run_id))
        assert res is not None
        assert len(res.ordered_events) >= 10
    finally:
        conn.close()


# ===========================================================================
# 29. Replay after failure (WORKER_ESCALATION_REQUIRED present)
# ===========================================================================
def test_29_replay_after_failure():
    product, task, _, _ = _make_specs()
    deps = _make_default_deps(builder_case="always_bad_syntax_builder", reviewer_factory=fake_reviewer_pass, max_retries=0)
    out = MinimalFactoryController.run(product, task, deps)
    conn = sqlite3.connect(out.db_path)
    try:
        reader = ReplayReader(conn)
        from redi_lab.domain import FactoryRunId
        res = reader.replay_factory_run(FactoryRunId(out.factory_run_id))
        assert res is not None
        types = [e.event_type.value for e in res.ordered_events]
        assert "WORKER_ESCALATION_REQUIRED" in types
    finally:
        conn.close()


# ===========================================================================
# 30. Original fixture unchanged after runs
# ===========================================================================
def test_30_original_fixture_unchanged():
    refs = make_canonical_slug_fixture_refs(_REPO_ROOT)
    target = refs["fixture_root"] / "slugify" / "__init__.py"
    before = target.read_text()
    product, task, _, _ = _make_specs()
    for _ in range(2):
        deps = _make_default_deps(builder_case="fix_slugify_builder", reviewer_factory=fake_reviewer_pass)
        MinimalFactoryController.run(product, task, deps)
    after = target.read_text()
    assert before == after


# ===========================================================================
# 31. Domain AST provider/runtime/persistence independence
# ===========================================================================
def test_31_domain_ast_provider_independent():
    import ast as _ast

    domain_dir = _REPO_ROOT / "redi_lab" / "domain"

    forbidden_modules = {
        "openai", "anthropic", "google.generativeai", "google.ai",
        "requests", "httpx", "urllib.request", "urllib3",
        "sqlite3", "sqlalchemy", "psycopg2", "psycopg", "pymysql",
        "redis", "pymongo",
        "temporalio", "docker", "kubernetes",
        "boto3", "botocore", "google.cloud", "azure",
        "redi_lab.runtime", "redi_lab.persistence",
        "redi_lab.evidence", "redi_lab.replay",
        "redi_lab.factory", "redi_lab.gates",
    }
    forbidden_symbols = {
        "FakeScriptedWorker", "MinimalFactoryController", "IsolatedWorkspace",
        "DeterministicGateEngine", "LiveProviderAdapter",
    }

    for path in domain_dir.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        try:
            tree = _ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            raise AssertionError(f"{path.name}: syntax error during AST parse: {exc}")

        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                for alias in node.names:
                    mod_name = alias.name
                    low = mod_name.lower()
                    for bad in forbidden_modules:
                        if low == bad.lower() or low.startswith(bad.lower() + "."):
                            raise AssertionError(
                                f"{path.name}:L{node.lineno} forbidden import {mod_name!r}"
                            )
            elif isinstance(node, _ast.ImportFrom):
                mod_name = node.module or ""
                low = mod_name.lower()
                for bad in forbidden_modules:
                    if low == bad.lower() or low.startswith(bad.lower() + "."):
                        raise AssertionError(
                            f"{path.name}:L{node.lineno} forbidden from-import {mod_name!r}"
                        )
                if node.names:
                    for alias in node.names:
                        if alias.name in forbidden_symbols:
                            raise AssertionError(
                                f"{path.name}:L{node.lineno} forbidden symbol import {alias.name!r}"
                            )
            elif isinstance(node, (_ast.Name, _ast.Attribute)):
                if isinstance(node, _ast.Name) and node.id in forbidden_symbols:
                    raise AssertionError(
                        f"{path.name}:L{getattr(node, 'lineno', '?')} forbidden symbol reference {node.id!r}"
                    )



# ===========================================================================
# 32. No secret/API key persisted in ledger rows
# ===========================================================================
def test_32_no_secret_persisted():
    product, task, _, _ = _make_specs()
    deps = _make_default_deps(builder_case="fix_slugify_builder", reviewer_factory=fake_reviewer_pass)
    out = MinimalFactoryController.run(product, task, deps)
    conn = sqlite3.connect(out.db_path)
    try:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
        suspicious: list[str] = []
        patterns = ["sk-", "sk_live", "sk_test", "api_key", "openai_api_key", "secret_key"]
        for tbl in tables:
            for row in conn.execute(f"SELECT * FROM {tbl}").fetchall():
                for val in row:
                    if isinstance(val, bytes):
                        try:
                            txt = val.decode("utf-8", errors="ignore")
                        except Exception:
                            continue
                    elif isinstance(val, str):
                        txt = val
                    else:
                        continue
                    low = txt.lower()
                    for p in patterns:
                        if p.lower() in low:
                            suspicious.append(f"{tbl}::{p!r}")
        assert not suspicious, suspicious
    finally:
        conn.close()


# ===========================================================================
# 33. run_redi_03_live_provider.py StationContract field name regression
#     Ensures Live runner constructs valid StationContract instances using
#     allowed_tool_classes (not allowed_privileges) so that with
#     REDI_LIVE_PROVIDER=1 set but credentials absent, execution reaches the
#     expected LiveProviderBlockedError credential guard instead of TypeError.
# ===========================================================================
def test_33_live_runner_stationcontract_field_regression(monkeypatch):
    from redi_lab.runtime.live_provider_adapter import (
        LiveProviderAdapter, LiveProviderConfig, LiveProviderBlockedError,
    )
    monkeypatch.setenv("REDI_LIVE_PROVIDER", "1")
    monkeypatch.delenv("REDI_OPENAI_API_KEY", raising=False)
    from redi_lab.domain import StationContract
    b = StationContract(
        role="builder",
        required_output_kinds=["patch_edit"],
        allowed_tool_classes=[],
    )
    r = StationContract(
        role="reviewer",
        required_output_kinds=["verdict"],
        allowed_tool_classes=[],
    )
    assert b.role == "builder"
    assert r.role == "reviewer"
    cfg = LiveProviderConfig(
        provider_name="openai",
        model="gpt-4o-mini",
        env_key_name="REDI_OPENAI_API_KEY",
        max_output_tokens=64,
        timeout_s=2.0,
    )
    try:
        _ = LiveProviderAdapter(config=cfg)
    except LiveProviderBlockedError:
        pass
    except TypeError as e:
        raise AssertionError(f"Unexpected TypeError in live runner setup: {e}")


# ===========================================================================
# 34. LiveProviderAdapter TokenUsage.other numeric-only regression.
#     OpenAI ChatCompletion.response exposes a string `model` field; the
#     adapter used to (incorrectly) stash that string inside TokenUsage.other
#     which is contractually Mapping[str, int] → every value must be a
#     non-negative integer.  This test simulates a real openai SDK-style
#     response via pure duck-typed objects, runs run_node(...) and asserts:
#       (a) TokenUsage construction succeeds with no ValueError/TypeError;
#       (b) TokenUsage.other values are all non-negative ints only;
#       (c) the string model reference survives via the provider-agnostic
#           NodeRunResult.opaque_provider_ref (the existing correct field);
#       (d) when the controller-style RunEvidence.metadata dict is assembled
#           using that opaque_provider_ref, the RunEvidence object can be
#           Layer-A-serialized and rehydrated without any provider-specific
#           Python type leaking across the boundary;
#       (e) evidence identity chain (sha/payload) is clean — no openai SDK
#           type, no ChatCompletionResponse, no raw SDK module reference.
# ===========================================================================
def test_34_live_adapter_token_usage_numeric_and_model_metadata():
    import sys
    import types
    import json as _json
    from dataclasses import asdict
    from redi_lab.runtime import live_provider_adapter as _lpa
    from redi_lab.runtime.live_provider_adapter import (
        LiveProviderAdapter,
        LiveProviderConfig,
    )
    from redi_lab.domain import (
        ContextManifestId,
        EvidenceId,
        FactoryRunId,
        ProductId,
        RecipeId,
        RunEvidence,
        RunStatus,
        StationId,
        TaskId,
        Timing,
        TokenUsage,
        WorkerId,
    )

    cfg = LiveProviderConfig(
        provider_name="openai",
        model="gpt-4o-mini",
        env_key_name="REDI_OPENAI_API_KEY",
        max_output_tokens=256,
        timeout_s=10.0,
        temperature=0.0,
    )

    builder_json = _json.dumps({
        "patches": [
            {"rel_path": "pkg/__init__.py", "action": "write", "content": "# slug"},
        ],
    })

    class _FakeChoiceMsg:
        content = builder_json

    class _FakeChoice:
        message = _FakeChoiceMsg()

    class _FakePromptTokensDetails:
        cached_tokens = 42

    class _FakeUsage:
        prompt_tokens = 300
        completion_tokens = 110
        prompt_tokens_details = _FakePromptTokensDetails()

    class _FakeResp:
        choices = (_FakeChoice(),)
        usage = _FakeUsage()
        model = "gpt-4o-mini-2024-07-18"

    class _FakeCompletions:
        def create(self, **_kwargs):  # pragma: no cover - trivial stub
            return _FakeResp()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeOpenAIClient:
        chat = _FakeChat()

        def __init__(self, *, api_key=None, timeout=None):
            pass

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = _FakeOpenAIClient

    def _fake_sdk_available() -> bool:
        return True

    saved_available = _lpa._sdk_available
    saved_env_key = None
    try:
        import os
        saved_env_key = os.environ.get(cfg.env_key_name)
        os.environ[cfg.env_key_name] = "test-key-not-real"
        _lpa._sdk_available = _fake_sdk_available
        sys.modules["openai"] = fake_openai
        adapter = LiveProviderAdapter(config=cfg)
        result = adapter.run_node(
            task_id=TaskId("t_regression_34"),
            station_id=StationId("st_builder"),
            worker_id=WorkerId("w_regression_34"),
            context={
                "role": "builder",
                "instructions": "slugify fixture",
                "context_body": "",
            },
            workspace_root=None,
            station_role="builder",
        )
    finally:
        _lpa._sdk_available = saved_available
        if saved_env_key is None:
            os.environ.pop(cfg.env_key_name, None)
        else:
            os.environ[cfg.env_key_name] = saved_env_key
        sys.modules.pop("openai", None)

    assert result.token_usage is not None, "Expected numeric TokenUsage from live adapter"
    usage: TokenUsage = result.token_usage
    assert isinstance(usage.input, int) and usage.input >= 0, usage.input
    assert isinstance(usage.output, int) and usage.output >= 0, usage.output
    assert isinstance(usage.cached_input, int) and usage.cached_input >= 0, usage.cached_input
    assert isinstance(usage.reasoning, int) and usage.reasoning >= 0, usage.reasoning
    for k, v in usage.other.items():
        assert isinstance(k, str), f"non-str key in other: {k!r}"
        assert isinstance(v, int), f"non-int value in TokenUsage.other[{k!r}]: {v!r} type={type(v).__name__}"
        assert v >= 0, f"negative value in TokenUsage.other[{k!r}]: {v}"
    assert "model" not in usage.other, (
        "String 'model' key must NOT live in TokenUsage.other (contract: int-only). "
        "Use NodeRunResult.opaque_provider_ref + RunEvidence.metadata['opaque_provider_ref']."
    )
    assert usage.other.get("total") == usage.input + usage.output, usage.other

    assert isinstance(result.opaque_provider_ref, str) and result.opaque_provider_ref, (
        "Model reference must survive via the existing provider-agnostic "
        "NodeRunResult.opaque_provider_ref channel."
    )
    assert "gpt-4o-mini-2024-07-18" in result.opaque_provider_ref, result.opaque_provider_ref
    assert result.opaque_provider_ref.startswith("openai:"), result.opaque_provider_ref

    run_ev = RunEvidence(
        id=EvidenceId("e_reg34"),
        factory_run_id=FactoryRunId("fr_reg34"),
        product_id=ProductId("p_slug"),
        recipe_id=RecipeId("r_slug"),
        node_id="builder-0",
        station_id=StationId("st_builder"),
        task_class="software_patch",
        worker_id=WorkerId("w_regression_34"),
        context_manifest_id=ContextManifestId("cm_reg34"),
        token_usage=usage,
        timing=Timing(wall_clock_seconds=0.1, compute_seconds=0.1),
        retry_count=0,
        final_status=RunStatus.RUNNING,
        metadata={
            "attempt_index": 0,
            "opaque_provider_ref": result.opaque_provider_ref,
            "builder_error": "",
        },
    )

    ev_dict = asdict(run_ev)
    serialized = _json.dumps(ev_dict, sort_keys=True, default=str)
    deserialized_metadata = _json.loads(serialized)["metadata"]
    assert deserialized_metadata["opaque_provider_ref"] == result.opaque_provider_ref
    payload = run_ev.token_usage.other
    for k, v in payload.items():
        assert isinstance(v, int), f"post-serialize other[{k!r}] not int: {v!r} type={type(v).__name__}"

    def _contains_sdk_ref(obj, stack=0):
        if stack > 8:  # pragma: no cover - recursion guard
            return False
        cls_name = type(obj).__name__
        if cls_name in {"ChatCompletion", "ChatCompletionMessage", "Choice", "Usage"} and not isinstance(obj, dict):
            return True
        if isinstance(obj, dict):
            return any(_contains_sdk_ref(v, stack + 1) for v in obj.values())
        if isinstance(obj, (list, tuple)):
            return any(_contains_sdk_ref(v, stack + 1) for v in obj)
        return False

    assert not _contains_sdk_ref(ev_dict), (
        "Provider-specific SDK classes leaked into Layer-A RunEvidence; "
        "only plain JSON-friendly (dict/list/str/int/float) allowed."
    )
    assert len(result.patches) == 1
    assert result.patches[0].rel_path == "pkg/__init__.py"
    assert result.reviewer_verdict is None


# ===========================================================================
# 35a..35d. LiveProviderAdapter structured-output parse 4-case regression.
#     Ensures the adapter distinguishes four parse outcomes:
#       Case 1: valid JSON 1 PatchEdit          -> patches=1, no error
#       Case 2: valid JSON patches=[]            -> patches=0, no error
#       Case 3: non-JSON prose "Sure, I can..."    -> patches=0, PARSE_FAILED error
#       Case 4: malformed JSON "{patches:[}"     -> patches=0, PARSE_FAILED error
#     Each case also verifies TokenUsage + opaque_provider_ref behave identically
#     so that gate/retry/isolation are unchanged.
# ===========================================================================
def _stub_openai_env(raw_content: str, model: str, prompt_tokens: int, completion_tokens: int):
    """Return (fake_openai_module, fake_sdk_available_fn, captured_kwargs_list) closure."""
    import sys
    import types
    captured: list[dict] = []

    class _FakeChoiceMsg:
        def __init__(self, rc: str):
            self.content = rc

    class _FakeChoice:
        def __init__(self, msg: object):
            self.message = msg

    class _FakeUsage:
        def __init__(self, pt: int, ct: int):
            self.prompt_tokens = pt
            self.completion_tokens = ct
            self.prompt_tokens_details = None

    class _FakeResp:
        def __init__(self, choices_tup: tuple, usage_obj: object, model_str: str):
            self.choices = choices_tup
            self.usage = usage_obj
            self.model = model_str

    class _FakeCompletions:
        def __init__(self):
            pass
        def create(self, **kwargs):
            captured.append(dict(kwargs))
            msg = _FakeChoiceMsg(raw_content)
            choice = _FakeChoice(msg)
            usage = _FakeUsage(prompt_tokens, completion_tokens)
            return _FakeResp((choice,), usage, model)

    class _FakeChat:
        def __init__(self):
            self.completions = _FakeCompletions()

    class _FakeOpenAIClient:
        def __init__(self, *, api_key=None, timeout=None):
            self.chat = _FakeChat()

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = _FakeOpenAIClient
    def _fake_sdk_available() -> bool:
        return True
    return fake_openai, _fake_sdk_available, captured


def _run_node_captured(raw_content: str):
    """Return (result_node, captured_kwargs) using the shared fake SDK above
    with injected env. The caller asserts against the real adapter.
    """
    import os
    import sys
    from redi_lab.runtime import live_provider_adapter as _lpa
    from redi_lab.runtime.live_provider_adapter import (
        LiveProviderAdapter, LiveProviderConfig,
    )
    from redi_lab.domain import TaskId, StationId, WorkerId
    fake_openai, _fake_sdk, captured = _stub_openai_env(
        raw_content=raw_content,
        model="gpt-4o-mini-2024-07-18",
        prompt_tokens=200,
        completion_tokens=max(1, len(raw_content) // 4),
    )
    cfg = LiveProviderConfig(
        provider_name="openai",
        model="gpt-4o-mini",
        env_key_name="REDI_OPENAI_API_KEY",
        max_output_tokens=2048,
        timeout_s=5.0,
        temperature=0.0,
    )
    saved = _lpa._sdk_available
    saved_env_key = os.environ.get(cfg.env_key_name)
    try:
        os.environ[cfg.env_key_name] = "test-key-not-real"
        _lpa._sdk_available = _fake_sdk
        sys.modules["openai"] = fake_openai
        adapter = LiveProviderAdapter(config=cfg)
        result = adapter.run_node(
            task_id=TaskId("t_parse"),
            station_id=StationId("st_b"),
            worker_id=WorkerId("w_parse"),
            context={"role": "builder", "instructions": "slug", "context_body": ""},
            workspace_root=None,
            station_role="builder",
        )
    finally:
        _lpa._sdk_available = saved
        if saved_env_key is None:
            os.environ.pop(cfg.env_key_name, None)
        else:
            os.environ[cfg.env_key_name] = saved_env_key
        sys.modules.pop("openai", None)
    return result, captured


def test_35a_parse_case1_valid_json_one_patch():
    import json
    payload = json.dumps({"patches": [
        {"rel_path": "pkg/__init__.py", "action": "write", "content": "x=1"}
    ]})
    r, captured = _run_node_captured(payload)
    assert len(r.patches) == 1 and r.patches[0].rel_path == "pkg/__init__.py"
    assert r.error_message is None, "Case1 valid parse must not surface error"
    assert r.token_usage is not None
    assert r.token_usage.input == 200 and isinstance(r.token_usage.other["total"], int)
    assert r.opaque_provider_ref and r.opaque_provider_ref.startswith("openai:")


def test_35b_parse_case2_valid_json_empty_patches():
    import json
    payload = json.dumps({"patches": []})
    r, captured = _run_node_captured(payload)
    assert len(r.patches) == 0
    assert r.error_message is None, "Case2 valid empty patches must NOT surface error"
    assert r.token_usage is not None and r.opaque_provider_ref


def test_35c_parse_case3_non_json_prose():
    payload = "Sure, I can do that. Here is the updated file."
    r, captured = _run_node_captured(payload)
    assert len(r.patches) == 0
    assert isinstance(r.error_message, str) and "STRUCTURED_OUTPUT_PARSE_FAILED" in r.error_message
    assert "category=JSONDecodeError" in r.error_message
    assert f"raw_length_chars={len(payload)}" in r.error_message
    assert r.token_usage is not None and r.opaque_provider_ref


def test_35d_parse_case4_malformed_json():
    payload = "{patches:[{rel_path:a},}"
    r, captured = _run_node_captured(payload)
    assert len(r.patches) == 0
    assert isinstance(r.error_message, str) and "STRUCTURED_OUTPUT_PARSE_FAILED" in r.error_message
    assert "category=JSONDecodeError" in r.error_message
    assert f"raw_length_chars={len(payload)}" in r.error_message
    assert r.token_usage is not None and r.opaque_provider_ref


def test_35e_parse_case5_valid_json_non_dict_root_array():
    """Valid JSON but root is array not dict; category=NonDictRoot expected."""
    import json
    payload = json.dumps([{"patches": []}])
    r, captured = _run_node_captured(payload)
    assert len(r.patches) == 0
    assert isinstance(r.error_message, str) and "STRUCTURED_OUTPUT_PARSE_FAILED" in r.error_message
    assert "category=NonDictRoot" in r.error_message


# ===========================================================================
# 36. LiveProviderAdapter API-level structured output enforcement.
#     Confirms the adapter now passes response_format={"type":"json_schema",
#     "json_schema":{name,strict,schema}} matching the STATION-ROLE-SPECIFIC
#     schema (builder != reviewer, separate). Verifies explicit fallback to
#     json_object when SDK raises TypeError (observed, not silent). Also
#     confirms system/user prompt structure + temp + max_tokens respected.
# ===========================================================================
def test_36_live_adapter_api_level_response_format_enforcement():
    import os, sys, types, json
    from redi_lab.runtime import live_provider_adapter as _lpa
    from redi_lab.runtime.live_provider_adapter import (
        LiveProviderAdapter, LiveProviderConfig,
        BUILDER_OUTPUT_SCHEMA_NAME, REVIEWER_OUTPUT_SCHEMA_NAME,
        _response_format_for_role,
    )
    from redi_lab.domain import TaskId, StationId, WorkerId
    captured: list[dict] = []

    class _FMsg:
        content = json.dumps({"patches": []})

    class _FC:
        message = _FMsg()

    class _FU:
        prompt_tokens = 5; completion_tokens = 3; prompt_tokens_details = None

    class _FR:
        choices = (_FC(),); usage = _FU(); model = "gpt-4o-mini"

    class _FCompletions:
        def create(self, **kw):
            captured.append(dict(kw))
            return _FR()

    class _FChat:
        completions = _FCompletions()

    class _FClient:
        chat = _FChat()
        def __init__(self, *, api_key=None, timeout=None): pass

    fake_openai = types.ModuleType("openai"); fake_openai.OpenAI = _FClient
    def _avail(): return True
    cfg = LiveProviderConfig(provider_name="openai", model="gpt-4o-mini",
                           env_key_name="REDI_OPENAI_API_KEY",
                           max_output_tokens=17, timeout_s=2.0, temperature=0.2)
    saved = _lpa._sdk_available; env_saved = os.environ.get(cfg.env_key_name)
    try:
        os.environ[cfg.env_key_name] = "k-not-real"
        _lpa._sdk_available = _avail; sys.modules["openai"] = fake_openai
        a = LiveProviderAdapter(config=cfg)
        result_builder = a.run_node(task_id=TaskId("t_enf_b"), station_id=StationId("s_enf_b"),
                                    worker_id=WorkerId("w_enf_b"),
                                    context={"role": "builder", "instructions": "slugify",
                                             "context_body": ""},
                                    workspace_root=None, station_role="builder")
        captured.clear()
        result_reviewer = a.run_node(task_id=TaskId("t_enf_r"), station_id=StationId("s_enf_r"),
                                     worker_id=WorkerId("w_enf_r"),
                                     context={"role": "reviewer", "instructions": "review",
                                              "context_body": ""},
                                     workspace_root=None, station_role="reviewer")
    finally:
        _lpa._sdk_available = saved
        if env_saved is None: os.environ.pop(cfg.env_key_name, None)
        else: os.environ[cfg.env_key_name] = env_saved
        sys.modules.pop("openai", None)
    # --- role-specific schema choice test ---
    rf_builder = _response_format_for_role("builder")
    assert rf_builder["type"] == "json_schema"
    assert rf_builder["json_schema"]["name"] == BUILDER_OUTPUT_SCHEMA_NAME
    assert rf_builder["json_schema"].get("strict") is True
    rf_reviewer = _response_format_for_role("reviewer")
    assert rf_reviewer["type"] == "json_schema"
    assert rf_reviewer["json_schema"]["name"] == REVIEWER_OUTPUT_SCHEMA_NAME
    assert rf_reviewer["json_schema"].get("strict") is True
    assert rf_builder != rf_reviewer, "Role schemas must differ (verdict vs patches)"

    # --- API kwargs captured: builder call first then reviewer ---
    assert len(captured) == 1, "Expected 1 capture (reviewer call; builder cleared above)"
    kw0 = captured[0]
    # Reviewer json_schema in captured kwargs
    rf = kw0.get("response_format")
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == REVIEWER_OUTPUT_SCHEMA_NAME
    # temperature + max tokens respected
    assert kw0.get("temperature") == 0.2
    assert kw0.get("max_tokens") == 17
    msgs = kw0.get("messages") or []
    assert msgs and msgs[0]["role"] == "system" and msgs[1]["role"] == "user"
    sys_text = msgs[0]["content"] or ""
    assert "Output ONLY valid JSON" in sys_text, "System prompt demands valid JSON in prose description"
    assert "Builder returns {patches:" in sys_text
    user_text = msgs[1]["content"] or ""
    assert "## Station role: reviewer" in user_text
    # Reviewer schema used → also valid PASS output must be parsed correctly
    # Re-run with reviewer content to prove round-trip
    content_rev = json.dumps({"verdict": "PASS", "reasons": [], "evidence_refs": []})
    class _RevMsg:
        content = content_rev
    class _RevC:
        message = _RevMsg()
    class _RevF:
        choices = (_RevC(),); usage = _FU(); model = "gpt-4o-mini-2024-07-18"
    class _RevCompletions:
        def create(self, **kw):
            captured.append(dict(kw)); return _RevF()
    class _RevChat:
        completions = _RevCompletions()
    class _RevClient:
        chat = _RevChat(); __init__ = _FClient.__init__
    fake_openai2 = types.ModuleType("openai"); fake_openai2.OpenAI = _RevClient
    try:
        os.environ[cfg.env_key_name] = "k-not-real"
        _lpa._sdk_available = _avail; sys.modules["openai"] = fake_openai2
        a2 = LiveProviderAdapter(config=cfg)
        res_rev = a2.run_node(task_id=TaskId("t_r2"), station_id=StationId("s_r2"),
                               worker_id=WorkerId("w_r2"),
                               context={"role": "reviewer", "instructions": "r", "context_body": ""},
                               workspace_root=None, station_role="reviewer")
    finally:
        _lpa._sdk_available = saved
        if env_saved is None: os.environ.pop(cfg.env_key_name, None)
        else: os.environ[cfg.env_key_name] = env_saved
        sys.modules.pop("openai", None)
    assert res_rev.reviewer_verdict == "PASS"
    assert res_rev.reviewer_reasons == ()
    assert res_rev.reviewer_evidence_refs == ()
    assert result_builder.error_message is None, "Valid empty patches builder must NOT error"
    assert result_builder.opaque_provider_ref.startswith("openai:")

    # Fail-closed enforcement (REDI-03L2C policy): simulate legacy SDK that
    # rejects strict json_schema with TypeError. Adapter MUST raise an explicit
    # LiveProviderBlockedError (NOT weaken to json_object). Call count == 1
    # guarantees no second provider request is ever sent under a weaker contract.
    failclosed_log: list[str] = []
    class _RaisesOnceCompletions:
        def __init__(self): self.call_count = 0
        def create(self, **kw):
            self.call_count += 1
            failclosed_log.append("typeerror_raised")
            raise TypeError("response_format type=json_schema unsupported by legacy SDK")
    class _FCChat:
        def __init__(self): self.completions = _RaisesOnceCompletions()
    class _FCClient:
        __init__ = _FClient.__init__
        def __init__(self, *, api_key=None, timeout=None):
            self.chat = _FCChat()
    fake_openai3 = types.ModuleType("openai"); fake_openai3.OpenAI = _FCClient
    captured.clear()
    try:
        os.environ[cfg.env_key_name] = "k-not-real"
        _lpa._sdk_available = _avail; sys.modules["openai"] = fake_openai3
        a3 = LiveProviderAdapter(config=cfg)
        res_fb = a3.run_node(task_id=TaskId("t_fb"), station_id=StationId("s_fb"),
                              worker_id=WorkerId("w_fb"),
                              context={"role": "builder", "instructions": "slug", "context_body": ""},
                              workspace_root=None, station_role="builder")
    except Exception as exc_fb:
        failclosed_log.append(f"blocked_error_type={type(exc_fb).__name__}")
        failclosed_log.append(f"blocked_msg_contains_json_schema={('json_schema' in str(exc_fb))}")
        failclosed_log.append(f"blocked_msg_contains_fail_closed={('fail-closed' in str(exc_fb).lower() or 'fail_closed' in str(exc_fb) or 'REDI-03L2C' in str(exc_fb))}")
        # Capture call_count for the assertion below.
        # The completions instance lives on fake_openai3.OpenAI.chat.completions but we can't easily access it.
        # Instead we rely on len(failclosed_log) and the exception details.
    else:
        # Should NOT succeed — policy prohibits fallback.
        failclosed_log.append("UNEXPECTED_SUCCESS")
    finally:
        _lpa._sdk_available = saved
        if env_saved is None: os.environ.pop(cfg.env_key_name, None)
        else: os.environ[cfg.env_key_name] = env_saved
        sys.modules.pop("openai", None)
    assert failclosed_log[0] == "typeerror_raised", "Exactly one TypeError from legacy SDK"
    # Exactly one provider call attempted (no second call with weaker json_object)
    assert failclosed_log.count("typeerror_raised") == 1, "Fail-closed policy: exactly one SDK call, no retry under weaker contract"
    assert "blocked_error_type=LiveProviderBlockedError" in failclosed_log, "TypeError must raise LiveProviderBlockedError (fail-closed), NOT silently fall back"
    assert any(s.endswith("=True") and s.startswith("blocked_msg_contains_json_schema") for s in failclosed_log)
    assert any(s.endswith("=True") and s.startswith("blocked_msg_contains_fail_closed") for s in failclosed_log)
    # response_format should never have been weakened (we assert no captured second call ever happened)
    assert len(captured) == 0, "Fail-closed: no successful/weakened create call was ever captured"


# ===========================================================================
# 37. RunEvidence evidence preservation for structured outputs.
#     Asserts the chain NodeRunResult.error_message ->
#     controller RunEvidence.metadata["builder_error"] per controller.py:413
#     remains a Layer-A-clean string (no SDK types); TokenUsage numeric only;
#     opaque_provider_ref preserved via metadata; patches_json round-trips
#     with dict list correctly via 0/1 patches.
# ===========================================================================
def test_37_runevidence_structured_output_round_trip():
    import json
    from dataclasses import asdict
    from redi_lab.domain import (
        RunEvidence, EvidenceId, FactoryRunId, ProductId,
        RecipeId, StationId, TaskId, Timing, TokenUsage,
        WorkerId, ContextManifestId, RunStatus,
    )
    usage = TokenUsage(input=100, output=10, cached_input=0, reasoning=0, other={"total":110})
    md = {
        "attempt_index": 1,
        "opaque_provider_ref": "openai:gpt-4o-mini-2024-07-18",
        "builder_error": "STRUCTURED_OUTPUT_SCHEMA_FAILED role=builder reason=missing_patches",
        "patches_json": [],
    }
    ev = RunEvidence(
        id=EvidenceId("e_re"), factory_run_id=FactoryRunId("fr_re"),
        product_id=ProductId("p_re"), recipe_id=RecipeId("r_re"),
        node_id="builder-0", station_id=StationId("st_re"),
        task_class="software_patch", worker_id=WorkerId("w_re"),
        context_manifest_id=ContextManifestId("cm_re"),
        token_usage=usage, timing=Timing(wall_clock_seconds=0.1, compute_seconds=0.1),
        retry_count=0, final_status=RunStatus.RUNNING, metadata=md,
    )
    blob = json.dumps(asdict(ev), sort_keys=True, default=str)
    md2 = json.loads(blob)["metadata"]
    assert md2["builder_error"].startswith("STRUCTURED_OUTPUT_SCHEMA_FAILED")
    assert md2["opaque_provider_ref"] == "openai:gpt-4o-mini-2024-07-18"
    assert md2["patches_json"] == []
    usage2_md = json.loads(blob)["token_usage"]
    for v in usage2_md["other"].values():
        assert isinstance(v, int), "TokenUsage.other values must remain int-only post-serialize"
    # Ensure no provider SDK types leaked into round-tripped JSON bytes
    blob_str = blob
    assert "openai.resources" not in blob_str
    assert "ChatCompletion" not in blob_str


# ===========================================================================
# 38-45. Builder semantic schema-failure regression tests (8 cases per spec).
# Cases 1 (valid 1 patch) → already test_35a; Case 2 (valid 0 patches) →
# already test_35b; remaining 6 invalid cases below.
# ===========================================================================
def test_38_builder_schema_missing_patches_key():
    import json
    payload = json.dumps({"message": "Sure, I can help"})
    r, captured = _run_node_captured(payload)
    assert len(r.patches) == 0
    assert isinstance(r.error_message, str)
    assert r.error_message.startswith("STRUCTURED_OUTPUT_SCHEMA_FAILED")
    assert "role=builder" in r.error_message
    assert "reason=missing_patches" in r.error_message


def test_39_builder_schema_patches_wrong_type():
    import json
    payload = json.dumps({"patches": "none"})
    r, captured = _run_node_captured(payload)
    assert len(r.patches) == 0
    assert "STRUCTURED_OUTPUT_SCHEMA_FAILED" in (r.error_message or "")
    assert "reason=patches_not_array" in (r.error_message or "")


def test_40_builder_schema_patch_missing_rel_path():
    import json
    payload = json.dumps({"patches": [{"action": "write", "content": "x"}]})
    r, captured = _run_node_captured(payload)
    assert len(r.patches) == 0
    assert "reason=patch[0]_missing_rel_path" in (r.error_message or "")


def test_41_builder_schema_patch_invalid_action():
    import json
    payload = json.dumps({"patches": [{"rel_path": "a.py", "action": "other", "content": "x"}]})
    r, captured = _run_node_captured(payload)
    assert len(r.patches) == 0
    assert "reason=patch[0]_invalid_action" in (r.error_message or "")


def test_42_builder_schema_write_missing_content():
    import json
    payload = json.dumps({"patches": [{"rel_path": "x.py", "action": "write"}]})
    r, captured = _run_node_captured(payload)
    assert len(r.patches) == 0
    assert "reason=patch[0]_write_missing_content" in (r.error_message or "")


def test_43_builder_schema_unrelated_json_object():
    import json
    payload = json.dumps({"status": "ok"})
    r, captured = _run_node_captured(payload)
    assert len(r.patches) == 0
    assert "reason=missing_patches" in (r.error_message or "")


def test_44_builder_schema_delete_ok_no_content():
    """Delete action is allowed without content; this is a VALID schema case."""
    import json
    payload = json.dumps({"patches": [{"rel_path": "obsolete.py", "action": "delete"}]})
    r, captured = _run_node_captured(payload)
    assert r.error_message is None
    assert len(r.patches) == 1
    assert r.patches[0].rel_path == "obsolete.py"
    assert r.patches[0].action == "delete"
    assert r.patches[0].content is None


def test_45_builder_schema_delete_with_null_content():
    """Delete action with content=None is also VALID (matches JSON-Schema nullable)."""
    import json
    payload = json.dumps({"patches": [{"rel_path": "old.py", "action": "delete", "content": None}]})
    r, captured = _run_node_captured(payload)
    assert r.error_message is None
    assert len(r.patches) == 1
    assert r.patches[0].content is None


# ===========================================================================
# 46-52. Reviewer semantic schema tests (7 cases per spec: valid PASS/FAIL/
# ESCALATE + 4 invalid: missing verdict, invalid verdict, missing reasons,
# missing evidence_refs).
# ===========================================================================
def _run_reviewer_node(raw_content: str):
    """Mirror _run_node_captured with station_role=reviewer (default fake SDK layout)."""
    import os, sys, types, json
    from redi_lab.runtime import live_provider_adapter as _lpa
    from redi_lab.runtime.live_provider_adapter import (
        LiveProviderAdapter, LiveProviderConfig,
    )
    from redi_lab.domain import StationId, TaskId, WorkerId
    captured: list[dict] = []

    class _Msg:
        content = raw_content

    class _Ch:
        message = _Msg()

    class _U:
        prompt_tokens = 10; completion_tokens = max(1, len(raw_content) // 4); prompt_tokens_details = None

    class _Resp:
        choices = (_Ch(),); usage = _U(); model = "gpt-4o-mini-2024-07-18"

    class _Comp:
        def create(self, **kw):
            captured.append(dict(kw)); return _Resp()

    class _Chat:
        completions = _Comp()

    class _Client:
        def __init__(self, *, api_key=None, timeout=None): self.chat = _Chat()

    fake_openai = types.ModuleType("openai"); fake_openai.OpenAI = _Client
    cfg = LiveProviderConfig(provider_name="openai", model="gpt-4o-mini",
                             env_key_name="REDI_OPENAI_API_KEY",
                             max_output_tokens=256, timeout_s=5.0, temperature=0.0)
    saved_avail = _lpa._sdk_available; env_saved = os.environ.get(cfg.env_key_name)
    try:
        os.environ[cfg.env_key_name] = "sk-test-not-real"
        _lpa._sdk_available = lambda: True
        sys.modules["openai"] = fake_openai
        a = LiveProviderAdapter(config=cfg)
        result = a.run_node(task_id=TaskId("t_rv"), station_id=StationId("st_rv"),
                             worker_id=WorkerId("w_rv"),
                             context={"role": "reviewer", "instructions": "r", "context_body": ""},
                             workspace_root=None, station_role="reviewer")
    finally:
        _lpa._sdk_available = saved_avail
        if env_saved is None: os.environ.pop(cfg.env_key_name, None)
        else: os.environ[cfg.env_key_name] = env_saved
        sys.modules.pop("openai", None)
    return result, captured


def test_46_reviewer_schema_valid_pass():
    import json
    payload = json.dumps({"verdict": "PASS", "reasons": ["clean"], "evidence_refs": ["e1"]})
    r, captured = _run_reviewer_node(payload)
    assert r.error_message is None
    assert r.reviewer_verdict == "PASS"
    assert tuple(r.reviewer_reasons) == ("clean",)
    assert tuple(r.reviewer_evidence_refs) == ("e1",)


def test_47_reviewer_schema_valid_fail():
    import json
    payload = json.dumps({"verdict": "FAIL", "reasons": ["public tests failed"], "evidence_refs": ["ev_fail_0"]})
    r, captured = _run_reviewer_node(payload)
    assert r.error_message is None
    assert r.reviewer_verdict == "FAIL"
    assert "public tests failed" in r.reviewer_reasons


def test_48_reviewer_schema_valid_escalate():
    import json
    payload = json.dumps({"verdict": "ESCALATE", "reasons": ["cannot auto-judge"], "evidence_refs": []})
    r, captured = _run_reviewer_node(payload)
    assert r.error_message is None
    assert r.reviewer_verdict == "ESCALATE"


def test_49_reviewer_schema_missing_verdict():
    import json
    payload = json.dumps({"reasons": ["x"], "evidence_refs": []})
    r, captured = _run_reviewer_node(payload)
    assert isinstance(r.error_message, str) and "STRUCTURED_OUTPUT_SCHEMA_FAILED" in r.error_message
    assert "reason=missing_verdict" in r.error_message
    assert r.reviewer_verdict is None


def test_50_reviewer_schema_invalid_verdict():
    import json
    payload = json.dumps({"verdict": "OK", "reasons": [], "evidence_refs": []})
    r, captured = _run_reviewer_node(payload)
    assert "reason=invalid_verdict" in (r.error_message or "")
    assert r.reviewer_verdict is None


def test_51_reviewer_schema_missing_reasons():
    import json
    payload = json.dumps({"verdict": "PASS", "evidence_refs": []})
    r, captured = _run_reviewer_node(payload)
    assert "reason=missing_reasons" in (r.error_message or "")
    assert r.reviewer_verdict is None


def test_52_reviewer_schema_missing_evidence_refs():
    import json
    payload = json.dumps({"verdict": "PASS", "reasons": []})
    r, captured = _run_reviewer_node(payload)
    assert "reason=missing_evidence_refs" in (r.error_message or "")
    assert r.reviewer_verdict is None


# ===========================================================================
# 53-54. Final integration tests:
#   53. four-evidence-distinction (A/B/C/D) via RunEvidence metadata.
#   54. existing PARSE failure still behaves identically after schema changes.
# ===========================================================================
def test_53_four_way_evidence_distinction():
    import json
    # --- Case A: valid empty patch (0 patches, no error) ---
    a, _ = _run_node_captured(json.dumps({"patches": []}))
    assert len(a.patches) == 0 and a.error_message is None
    # --- Case D: valid non-empty 1 patch ---
    d, _ = _run_node_captured(json.dumps({"patches": [
        {"rel_path": "pkg/__init__.py", "action": "write", "content": "x=1"}
    ]}))
    assert len(d.patches) == 1 and d.error_message is None
    # --- Case B: parse failure (non-JSON prose) ---
    b, _ = _run_node_captured("Sure.")
    assert len(b.patches) == 0 and isinstance(b.error_message, str) and "PARSE_FAILED" in b.error_message
    # --- Case C: schema failure (valid JSON unrelated object) ---
    c, _ = _run_node_captured(json.dumps({"message": "Not even close"}))
    assert len(c.patches) == 0 and isinstance(c.error_message, str) and "SCHEMA_FAILED" in c.error_message
    # Ensure all four signatures differ
    signature_A = (len(a.patches), a.error_message is None,
                   ("PARSE" in (a.error_message or "")), "SCHEMA" in (a.error_message or ""))
    signature_B = (len(b.patches), b.error_message is None,
                   ("PARSE" in (b.error_message or "")), "SCHEMA" in (b.error_message or ""))
    signature_C = (len(c.patches), c.error_message is None,
                   ("PARSE" in (c.error_message or "")), "SCHEMA" in (c.error_message or ""))
    signature_D = (len(d.patches), d.error_message is None,
                   ("PARSE" in (d.error_message or "")), "SCHEMA" in (d.error_message or ""))
    assert len({signature_A, signature_B, signature_C, signature_D}) == 4, (
        "All 4 evidence outcomes MUST have unique observable signatures in metadata"
    )


def test_54_existing_parse_failure_behavior_intact():
    """Non-JSON text → PARSE_FAILED category (not SCHEMA_FAILED), preserves prior
    behavior per regression tests 35c/35d/35e. Guarantees zero regression for
    forensic-signatures from the original live run."""
    r_prose, _ = _run_node_captured("Sure, I can do that.")
    assert "STRUCTURED_OUTPUT_PARSE_FAILED" in (r_prose.error_message or "")
    assert "SCHEMA_FAILED" not in (r_prose.error_message or "")
    assert "category=JSONDecodeError" in (r_prose.error_message or "")
    r_mal, _ = _run_node_captured("{bad json")
    assert "STRUCTURED_OUTPUT_PARSE_FAILED" in (r_mal.error_message or "")
    assert "SCHEMA_FAILED" not in (r_mal.error_message or "")
    r_arr, _ = _run_node_captured('[{"patches":[]}]')
    assert "STRUCTURED_OUTPUT_PARSE_FAILED" in (r_arr.error_message or "")
    assert "category=NonDictRoot" in (r_arr.error_message or "")
    # All have patches=0 (safe gate behavior unchanged)
    for r in (r_prose, r_mal, r_arr):
        assert len(r.patches) == 0
        assert r.token_usage is not None
        assert r.opaque_provider_ref and r.opaque_provider_ref.startswith("openai:")


# ===========================================================================
# REDI-03L2C new tests (Contract-Parity + Fail-Closed enforcement):
#
# L2C-1a:  builder write + string content        -> VALID (no error)
# L2C-1b:  builder write + null content          -> SCHEMA_FAILED content_not_string
# L2C-1c:  builder write missing content         -> SCHEMA_FAILED (covered by test_42
#                                                             already; re-asserted here
#                                                             for block completeness)
# L2C-1d:  builder delete missing content        -> VALID (no error)
# L2C-1e:  builder delete null content           -> VALID (no error, content=None
#                                                             collapses to absent per
#                                                             PatchEdit default)
# L2C-1f:  builder delete non-null string content-> SCHEMA_FAILED delete_unexpected_content
# L2C-2a:  reviewer verdict 'Pass' (mixed case)  -> SCHEMA_FAILED (exact case sensitive)
# L2C-2b:  reviewer verdict 'fail' (lowercase)   -> SCHEMA_FAILED
# L2C-2c:  reviewer verdict 'ok'                 -> SCHEMA_FAILED
# L2C-2d:  reviewer verdict 'PASS' (uppercase exact) -> still VALID  (sanity)
# L2C-3:   local validator vs schema parity table (19 input tuples must produce
#          identical pass/fail reasons for validator vs schema dict).
# ===========================================================================
def test_l2c_1a_builder_write_with_string_valid():
    """write action + string content = VALID. (Baseline sanity, but explicit for L2C.)"""
    import json
    payload = json.dumps({"patches": [{"rel_path": "pkg/__init__.py", "action": "write", "content": "def x():\n    return 42\n"}]})
    r, _ = _run_node_captured(payload)
    assert r.error_message is None
    assert len(r.patches) == 1
    assert r.patches[0].action == "write"
    assert isinstance(r.patches[0].content, str) and r.patches[0].content.startswith("def x()")


def test_l2c_1b_builder_write_with_null_content_invalid():
    """write action + content=null MUST be SCHEMA_FAILED (not a silent 'missing content'
    proxy; reason must distinguish the field PRESENT-but-wrong-type from ABSENT)."""
    import json
    payload = json.dumps({"patches": [{"rel_path": "pkg/__init__.py", "action": "write", "content": None}]})
    r, _ = _run_node_captured(payload)
    assert len(r.patches) == 0
    emsg = r.error_message or ""
    assert "STRUCTURED_OUTPUT_SCHEMA_FAILED" in emsg, f"Expected SCHEMA_FAILED got: {emsg}"
    assert "patch[0]_write_content_not_string" in emsg, (
        f"Expected reason=write_content_not_string (content=null, present but wrong type), got: {emsg}"
    )


def test_l2c_1c_builder_write_missing_content_reasserted():
    """write action missing content key = SCHEMA_FAILED (test_42; re-asserted so the
    full L2C 1a-1f block is self-contained)."""
    import json
    payload = json.dumps({"patches": [{"rel_path": "x.py", "action": "write"}]})
    r, _ = _run_node_captured(payload)
    assert len(r.patches) == 0
    emsg = r.error_message or ""
    assert "STRUCTURED_OUTPUT_SCHEMA_FAILED" in emsg
    assert "patch[0]_write_missing_content" in emsg, f"Expected missing_content, got: {emsg}"


def test_l2c_1d_builder_delete_missing_content_valid():
    """delete action WITHOUT content key = VALID. (Canonical delete has no body.)"""
    import json
    payload = json.dumps({"patches": [{"rel_path": "old.py", "action": "delete"}]})
    r, _ = _run_node_captured(payload)
    assert r.error_message is None, f"Unexpected error: {r.error_message}"
    assert len(r.patches) == 1
    assert r.patches[0].action == "delete"
    assert r.patches[0].content is None


def test_l2c_1e_builder_delete_null_content_valid():
    """delete action WITH content=null = VALID. (Nullable sentinel equivalent to absent.)"""
    import json
    payload = json.dumps({"patches": [{"rel_path": "old.py", "action": "delete", "content": None}]})
    r, _ = _run_node_captured(payload)
    assert r.error_message is None, f"Unexpected error for delete content=null: {r.error_message}"
    assert len(r.patches) == 1
    assert r.patches[0].content is None


def test_l2c_1f_builder_delete_with_string_content_invalid():
    """delete action WITH content=<non-null-string> MUST be SCHEMA_FAILED per canonical
    contract parity: delete never carries a payload body. Provider must NEVER emit
    unrelated content here; fail closed."""
    import json
    payload = json.dumps({"patches": [{"rel_path": "old.py", "action": "delete", "content": "not allowed here"}]})
    r, _ = _run_node_captured(payload)
    assert len(r.patches) == 0
    emsg = r.error_message or ""
    assert "STRUCTURED_OUTPUT_SCHEMA_FAILED" in emsg, f"Expected SCHEMA_FAILED got: {emsg}"
    assert "patch[0]_delete_unexpected_content" in emsg, (
        f"Expected reason=delete_unexpected_content (delete had non-null payload), got: {emsg}"
    )


def test_l2c_2a_reviewer_verdict_mixed_case_pass_invalid():
    """Reviewer verdict must be EXACT case-sensitive enum. 'Pass' != PASS → invalid."""
    import json
    payload = json.dumps({"verdict": "Pass", "reasons": [], "evidence_refs": []})
    r, _ = _run_reviewer_node(payload)
    emsg = r.error_message or ""
    assert "STRUCTURED_OUTPUT_SCHEMA_FAILED" in emsg, f"Mixed case 'Pass' must fail schema, got: {emsg}"
    assert "reason=invalid_verdict" in emsg


def test_l2c_2b_reviewer_verdict_lowercase_fail_invalid():
    """Reviewer verdict 'fail' (lowercase) != FAIL → invalid."""
    import json
    payload = json.dumps({"verdict": "fail", "reasons": ["r"], "evidence_refs": ["e"]})
    r, _ = _run_reviewer_node(payload)
    emsg = r.error_message or ""
    assert "STRUCTURED_OUTPUT_SCHEMA_FAILED" in emsg, f"Lowercase 'fail' must fail schema, got: {emsg}"
    assert "invalid_verdict" in emsg


def test_l2c_2c_reviewer_verdict_ok_invalid():
    """Reviewer verdict 'ok' → invalid; must be one of PASS/FAIL/ESCALATE exactly."""
    import json
    payload = json.dumps({"verdict": "ok", "reasons": [], "evidence_refs": []})
    r, _ = _run_reviewer_node(payload)
    emsg = r.error_message or ""
    assert "STRUCTURED_OUTPUT_SCHEMA_FAILED" in emsg, f"'ok' must fail schema, got: {emsg}"
    assert "invalid_verdict" in emsg


def test_l2c_2d_reviewer_verdict_exact_uppercase_still_valid_sanity():
    """Exact uppercase PASS/FAIL/ESCALATE remain VALID (sanity; proves 2a/2b/2c rejections
    are truly case-sensitivity, not a broken validator)."""
    import json
    for v in ("PASS", "FAIL", "ESCALATE"):
        payload = json.dumps({"verdict": v, "reasons": [], "evidence_refs": []})
        r, _ = _run_reviewer_node(payload)
        assert r.error_message is None, f"Exact {v} must be valid, got: {r.error_message}"
        assert r.reviewer_verdict == v, f"Verbatim case must round-trip for {v}"


def test_l2c_3_validator_against_declared_truth_table_parity():
    """Local validator matches the declared schema-dict semantics for a 21-row
    truth table. (Schema dict is the ground-truth document sent to OpenAI for
    strict Structured Outputs; the local hand-written _validate_station_schema
    must produce the same pass/fail classification and reason code as the
    document declares. NO external jsonschema dep required — we verify
    _validate_station_schema behavior directly.)"""
    from redi_lab.runtime.live_provider_adapter import (
        _validate_station_schema,
        BUILDER_OUTPUT_JSON_SCHEMA,
        REVIEWER_OUTPUT_JSON_SCHEMA,
    )
    # Sanity: schema dicts exist with correct name + strict flag
    assert BUILDER_OUTPUT_JSON_SCHEMA["name"] == "RediBuilderOutput"
    assert BUILDER_OUTPUT_JSON_SCHEMA["strict"] is True
    assert REVIEWER_OUTPUT_JSON_SCHEMA["name"] == "RediReviewerOutput"
    assert REVIEWER_OUTPUT_JSON_SCHEMA["strict"] is True
    # Builder schema MUST now use NESTED anyOf (3 explicit object variants)
    # inside patches.items. This replaces the earlier allOf/if/then representation
    # which is not supported by OpenAI strict Structured Outputs.
    items_node = BUILDER_OUTPUT_JSON_SCHEMA["schema"]["properties"]["patches"]["items"]
    assert set(items_node.keys()) == {"anyOf"}, (
        "Builder patches.items must contain ONLY anyOf; no top-level type/properties/required/addProps at the union wrapper to avoid unsupported composition"
    )
    branches = items_node["anyOf"]
    assert len(branches) == 3, "Builder anyOf must contain exactly 3 branches: write, delete-absent, delete-null"
    branch_actions = [b["properties"]["action"]["enum"][0] for b in branches]
    assert sorted(branch_actions) == ["delete", "delete", "write"], (
        "Expected branches: action=write (1) + action=delete (2 variants: absent/null content)"
    )
    # Write branch: 3 props declared, all 3 required, content=string
    write_branch = next(b for b in branches if b["properties"]["action"]["enum"][0] == "write")
    assert set(write_branch["properties"].keys()) == {"rel_path", "action", "content"}, "Write branch declares 3 properties"
    assert set(write_branch["required"]) == {"rel_path", "action", "content"}, "Write branch requires all 3 declared properties"
    assert write_branch["properties"]["content"] == {"type": "string"}, "Write branch content MUST be type=string (no nullable, no absent)"
    assert write_branch["additionalProperties"] is False
    # Delete-absent branch: 2 props declared (rel_path + action only), both required
    delete_branches = [b for b in branches if b["properties"]["action"]["enum"][0] == "delete"]
    absent_branch = next(b for b in delete_branches if "content" not in b["properties"])
    assert set(absent_branch["properties"].keys()) == {"rel_path", "action"}, "Delete-absent branch declares NO content property"
    assert set(absent_branch["required"]) == {"rel_path", "action"}, "Delete-absent branch requires only the 2 declared props"
    assert absent_branch["additionalProperties"] is False
    # Delete-null branch: 3 props declared, content=type=null, all 3 required
    null_branch = next(b for b in delete_branches if "content" in b["properties"])
    assert set(null_branch["properties"].keys()) == {"rel_path", "action", "content"}, "Delete-null branch declares content property"
    assert null_branch["properties"]["content"] == {"type": "null"}, "Delete-null branch content MUST be type=null"
    assert set(null_branch["required"]) == {"rel_path", "action", "content"}, "Delete-null branch requires all 3 declared props"
    assert null_branch["additionalProperties"] is False
    # Root schema shape (non-anyOf, patches:array required)
    root = BUILDER_OUTPUT_JSON_SCHEMA["schema"]
    assert root["type"] == "object" and "anyOf" not in root, "ROOT must be plain object; NEVER anyOf at root"
    assert set(root["required"]) == {"patches"}
    assert root["additionalProperties"] is False
    # Reviewer schema: enum exact strings, case sensitive
    assert REVIEWER_OUTPUT_JSON_SCHEMA["schema"]["properties"]["verdict"]["enum"] == ["PASS", "FAIL", "ESCALATE"], (
        "Reviewer verdict enum MUST be the exact case-sensitive strings, not lowered/canonicalised"
    )

    # (role, obj, expectation: None if valid, else reason substring expected)
    tt_builder = [
        ("builder", {"patches": []}, None),
        ("builder", {"patches": [{"rel_path": "a.py", "action": "write", "content": "s"}]}, None),
        ("builder", {"patches": [{"rel_path": "a.py", "action": "delete"}]}, None),
        ("builder", {"patches": [{"rel_path": "a.py", "action": "delete", "content": None}]}, None),
        # Invalid:
        ("builder", {"patches": [{"rel_path": "a.py", "action": "write", "content": None}]}, "write_content_not_string"),
        ("builder", {"patches": [{"rel_path": "a.py", "action": "write"}]}, "write_missing_content"),
        ("builder", {"patches": [{"rel_path": "a.py", "action": "delete", "content": "oops"}]}, "delete_unexpected_content"),
        ("builder", {}, "missing_patches"),
        ("builder", {"patches": "nope"}, "patches_not_array"),
        ("builder", {"patches": [{"action": "write", "content": "x"}]}, "missing_rel_path"),
        ("builder", {"patches": [{"rel_path": "x.py", "action": "modify", "content": "x"}]}, "invalid_action"),
    ]
    tt_reviewer = [
        ("reviewer", {"verdict": "PASS", "reasons": [], "evidence_refs": []}, None),
        ("reviewer", {"verdict": "FAIL", "reasons": ["r"], "evidence_refs": ["e"]}, None),
        ("reviewer", {"verdict": "ESCALATE", "reasons": [], "evidence_refs": []}, None),
        ("reviewer", {"verdict": "Pass", "reasons": [], "evidence_refs": []}, "invalid_verdict"),
        ("reviewer", {"verdict": "fail", "reasons": [], "evidence_refs": []}, "invalid_verdict"),
        ("reviewer", {"verdict": "ok", "reasons": [], "evidence_refs": []}, "invalid_verdict"),
        ("reviewer", {"reasons": [], "evidence_refs": []}, "missing_verdict"),
        ("reviewer", {"verdict": "PASS"}, "missing_reasons"),
        ("reviewer", {"verdict": "PASS", "reasons": []}, "missing_evidence_refs"),
    ]
    for role, obj, expected_reason_or_none in tt_builder + tt_reviewer:
        local_reason = _validate_station_schema(role, obj)
        if expected_reason_or_none is None:
            assert local_reason is None, (
                f"Expected VALID (validator returns None) for role={role} obj={obj!r}, got local reason={local_reason!r}"
            )
        else:
            assert isinstance(local_reason, str), (
                f"Expected INVALID reason containing {expected_reason_or_none!r}, got local_reason=None for role={role} obj={obj!r}"
            )
            assert expected_reason_or_none in local_reason, (
                f"Local reason {local_reason!r} did not contain expected substring {expected_reason_or_none!r} for role={role} obj={obj!r}"
            )


def test_l2d_static_strict_schema_openai_compatible():
    """Regression: strict REDI schemas sent to OpenAI use ONLY the supported
    Structured Outputs subset. Fails if any unsupported composition keyword or
    other banned pattern appears. Covers BOTH builder and reviewer schemas.

    Banned (unsupported) keywords: allOf / if / then / else / not /
    dependentRequired / dependentSchemas.

    Also enforces:
      - every object schema must have additionalProperties=False
      - every property declared by an object schema appears in its required array
      - root of every REDI strict schema is a plain type=object (NOT anyOf root)
      - anyOf is used ONLY as a union wrapper in builder patches.items
    """
    from redi_lab.runtime.live_provider_adapter import (
        BUILDER_OUTPUT_JSON_SCHEMA,
        REVIEWER_OUTPUT_JSON_SCHEMA,
    )
    UNSUPPORTED_KEYWORDS = {"allOf", "if", "then", "else", "not",
                           "dependentRequired", "dependentSchemas"}

    anyof_locations_seen: list[tuple[str, ...]] = []
    errors: list[str] = []

    def walk(node: Any, path: tuple[str, ...]) -> None:
        if isinstance(node, dict):
            # 1. Reject ANY occurrence of an unsupported keyword anywhere in the tree.
            bad_keys = set(node.keys()) & UNSUPPORTED_KEYWORDS
            if bad_keys:
                errors.append(
                    f"Unsupported OpenAI strict Structured Output keyword(s) {sorted(bad_keys)} found at path {'/'.join(path) or '<root>'}"
                )
            # 2. Track anyOf usage — only allowed at builder patches.items wrapper.
            if "anyOf" in node:
                anyof_locations_seen.append(path)
            # 3. For any node declaring type=object AND properties (i.e. a concrete
            #    object branch), enforce additionalProperties=False AND that every
            #    declared property is also in the required array.
            if node.get("type") == "object" and isinstance(node.get("properties"), dict):
                addProps = node.get("additionalProperties", "<MISSING>")
                if addProps is not False:
                    errors.append(
                        f"Object schema at path {'/'.join(path) or '<root>'} MUST have additionalProperties=false; got {addProps!r}"
                    )
                declared_props = set(node["properties"].keys())
                required_set = set(node.get("required") or [])
                missing_required = declared_props - required_set
                if missing_required:
                    errors.append(
                        f"Object schema at path {'/'.join(path) or '<root>'} declares properties {sorted(missing_required)} which are NOT in its required array"
                    )
            # Recurse into all dict values.
            for k, v in node.items():
                walk(v, path + (str(k),))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, path + (str(i),))

    for schema_label, schema_doc in (("builder", BUILDER_OUTPUT_JSON_SCHEMA),
                                     ("reviewer", REVIEWER_OUTPUT_JSON_SCHEMA)):
        # Walk only the inner "schema" payload (what OpenAI enforces).
        inner = schema_doc["schema"]
        # Root must be plain object (not anyOf at root).
        if not (isinstance(inner, dict) and inner.get("type") == "object" and "anyOf" not in inner):
            errors.append(f"Root of inner schema for {schema_label} MUST be type=object with no anyOf")
        walk(inner, path=(schema_label, "schema"))

    # Assert anyOf only at the one allowed location: builder → schema → properties → patches → items.
    allowed_anyof_path = ("builder", "schema", "properties", "patches", "items")
    assert anyof_locations_seen == [allowed_anyof_path], (
        f"anyOf may ONLY appear at {allowed_anyof_path}. Found at: {[list(p) for p in anyof_locations_seen]}"
    )
    assert len(errors) == 0, "\n".join(errors)


# ===========================================================================
# REDI-03L2E — Final local/provider schema parity tests.
#
# These prove the local _validate_station_schema() accepts/rejects exactly the
# same object classes as the already-approved strict OpenAI provider schemas.
#
# New tests:
#   55. builder root extra key rejected (provider addProps=false at root).
#   56. builder write patch extra key rejected (write variant addProps=false).
#   57. builder delete patch extra key rejected (delete variant addProps=false).
#   58. reviewer root extra key rejected (reviewer addProps=false at root).
#   59. reviewer reasons array non-string element rejected (array[string]).
#   60. reviewer evidence_refs array non-string element rejected (array[string]).
#   61. bounded direct-parity table (valid/invalid document classes) comparing
#       local validator output against the documented schema-acceptance contract.
# ===========================================================================
def test_l2e_55_builder_root_extra_key_rejected():
    """Builder root allows key 'patches' only. Any other key → SCHEMA_FAILED
    with deterministic `root_extra_key_X` short code. Mirrors provider schema
    root additionalProperties=false."""
    import json
    payload = json.dumps({"patches": [], "trace": "oops"})
    r, _ = _run_node_captured(payload)
    assert len(r.patches) == 0
    emsg = r.error_message or ""
    assert "STRUCTURED_OUTPUT_SCHEMA_FAILED" in emsg, f"Expected SCHEMA_FAILED, got: {emsg}"
    assert "reason=root_extra_key_trace" in emsg, (
        f"Expected deterministic root_extra_key_<name> reason, got: {emsg}"
    )


def test_l2e_56_builder_write_patch_extra_key_rejected():
    """Write patch variant allows keys {rel_path,action,content} only. Any
    other property → SCHEMA_FAILED patch[i]_extra_key_<name>.  Mirrors the
    write-branch additionalProperties=false in the 3-branch anyOf provider
    schema."""
    import json
    payload = json.dumps({"patches": [
        {"rel_path": "pkg/__init__.py", "action": "write", "content": "x=1",
         "sha256": "abc123"}
    ]})
    r, _ = _run_node_captured(payload)
    assert len(r.patches) == 0
    emsg = r.error_message or ""
    assert "STRUCTURED_OUTPUT_SCHEMA_FAILED" in emsg
    assert "patch[0]_extra_key_sha256" in emsg, (
        f"Expected deterministic patch[i]_extra_key_<name>, got: {emsg}"
    )


def test_l2e_57_builder_delete_patch_extra_key_rejected():
    """Both delete variants (absent-content and null-content) also enforce
    strict additionalProperties=false. Extra property → deterministic extra-key
    reason."""
    import json
    # Test delete-absent variant: allowed keys {rel_path,action} only.
    payload_absent = json.dumps({"patches": [
        {"rel_path": "old.py", "action": "delete", "comment": "unused field"}
    ]})
    r_a, _ = _run_node_captured(payload_absent)
    emsg_a = r_a.error_message or ""
    assert "STRUCTURED_OUTPUT_SCHEMA_FAILED" in emsg_a
    assert "patch[0]_extra_key_comment" in emsg_a
    # Test delete-null variant: allowed keys {rel_path,action,content} only.
    payload_null = json.dumps({"patches": [
        {"rel_path": "old.py", "action": "delete", "content": None, "why": "legacy"}
    ]})
    r_n, _ = _run_node_captured(payload_null)
    emsg_n = r_n.error_message or ""
    assert "STRUCTURED_OUTPUT_SCHEMA_FAILED" in emsg_n
    assert "patch[0]_extra_key_why" in emsg_n


def test_l2e_58_reviewer_root_extra_key_rejected():
    """Reviewer root allows only verdict/reasons/evidence_refs. Any extra key
    → SCHEMA_FAILED with root_extra_key_<name> (mirrors reviewer root
    additionalProperties=false)."""
    import json
    payload = json.dumps({
        "verdict": "PASS", "reasons": [], "evidence_refs": [],
        "model_thinking": "chain-of-thought-attempt",
    })
    r, _ = _run_reviewer_node(payload)
    emsg = r.error_message or ""
    assert "STRUCTURED_OUTPUT_SCHEMA_FAILED" in emsg, f"Expected SCHEMA_FAILED got: {emsg}"
    assert "reason=root_extra_key_model_thinking" in emsg


def test_l2e_59_reviewer_reasons_array_non_string_element_rejected():
    """Provider schema: reasons: array[string]. Local validator must reject
    any non-string element with deterministic reasons[i]_not_string."""
    import json
    payload = json.dumps({"verdict": "PASS", "reasons": [1], "evidence_refs": []})
    r, _ = _run_reviewer_node(payload)
    emsg = r.error_message or ""
    assert "STRUCTURED_OUTPUT_SCHEMA_FAILED" in emsg, f"Expected SCHEMA_FAILED got: {emsg}"
    assert "reason=reasons[0]_not_string" in emsg


def test_l2e_60_reviewer_evidence_refs_array_non_string_element_rejected():
    """Provider schema: evidence_refs: array[string]. Local validator must
    reject non-string elements with evidence_refs[i]_not_string."""
    import json
    payload = json.dumps({
        "verdict": "PASS", "reasons": [], "evidence_refs": [{"id": "x"}]
    })
    r, _ = _run_reviewer_node(payload)
    emsg = r.error_message or ""
    assert "STRUCTURED_OUTPUT_SCHEMA_FAILED" in emsg, f"Expected SCHEMA_FAILED got: {emsg}"
    assert "reason=evidence_refs[0]_not_string" in emsg


def test_l2e_61_bounded_direct_parity_validator_vs_schema_document_classes():
    """Bounded direct-parity table (no external jsonschema dep, document-shape
    classes only) that proves the local validator accepts every document
    class that the 3-branch anyOf provider schema + flat reviewer schema are
    declared to accept, AND rejects every document class they reject.

    Each row is a (role, obj, expect_valid:bool, [rejected-by rule label])
    tuple.  Labels match schema/validator rule names for forensics but are not
    mechanically checked; only accept/reject classification equality is
    enforced.
    """
    from redi_lab.runtime.live_provider_adapter import _validate_station_schema

    TABLE: list[tuple[str, Any, bool]] = [
        # --- Builder VALID classes (exactly what 3-branch anyOf accepts):
        ("builder", {"patches": []}, True),
        ("builder", {"patches": [{"rel_path": "a.py", "action": "write", "content": "s"}]}, True),
        ("builder", {"patches": [{"rel_path": "old.py", "action": "delete"}]}, True),
        ("builder", {"patches": [{"rel_path": "old.py", "action": "delete", "content": None}]}, True),
        ("builder", {"patches": [
            {"rel_path": "a.py", "action": "write", "content": "s"},
            {"rel_path": "b.py", "action": "delete"},
            {"rel_path": "c.py", "action": "delete", "content": None},
        ]}, True),
        # --- Builder INVALID classes (all variants disallowed by provider addProps / type rules):
        # Root addProps:
        ("builder", {"patches": [], "_debug": "x"}, False),
        ("builder", {"patches": [], "message": "hi"}, False),
        # Write branch addProps + content rules:
        ("builder", {"patches": [{"rel_path": "a.py", "action": "write", "content": "s", "extra": 1}]}, False),
        ("builder", {"patches": [{"rel_path": "a.py", "action": "write"}]}, False),             # missing content
        ("builder", {"patches": [{"rel_path": "a.py", "action": "write", "content": None}]}, False), # content null
        ("builder", {"patches": [{"rel_path": "a.py", "action": "write", "content": 1}]}, False),    # content int
        # Delete-absent branch addProps:
        ("builder", {"patches": [{"rel_path": "o.py", "action": "delete", "why": "legacy"}]}, False),
        # Delete-null branch addProps / unexpected non-null content:
        ("builder", {"patches": [{"rel_path": "o.py", "action": "delete", "content": None, "meta": 0}]}, False),
        ("builder", {"patches": [{"rel_path": "o.py", "action": "delete", "content": "oops"}]}, False),
        # Missing/wrong patch structure:
        ("builder", {}, False),                                      # missing patches
        ("builder", {"patches": "nope"}, False),                     # patches not array
        ("builder", {"patches": [None]}, False),                     # patch item not object
        ("builder", {"patches": [{"action": "write", "content": "x"}]}, False),  # missing rel_path
        ("builder", {"patches": [{"rel_path": "a.py", "action": "modify", "content": "x"}]}, False), # invalid action

        # --- Reviewer VALID classes (exactly what flat schema accepts):
        ("reviewer", {"verdict": "PASS", "reasons": [], "evidence_refs": []}, True),
        ("reviewer", {"verdict": "FAIL", "reasons": ["public failed"], "evidence_refs": ["e0"]}, True),
        ("reviewer", {"verdict": "ESCALATE", "reasons": ["a","b"], "evidence_refs": ["e1","e2"]}, True),
        # --- Reviewer INVALID classes:
        # Root addProps:
        ("reviewer", {"verdict": "PASS", "reasons": [], "evidence_refs": [], "thoughts": "cot"}, False),
        ("reviewer", {"verdict": "PASS", "reasons": [], "evidence_refs": [], "summary": "text"}, False),
        # Verdict rules:
        ("reviewer", {"reasons": [], "evidence_refs": []}, False),                     # missing verdict
        ("reviewer", {"verdict": "Pass", "reasons": [], "evidence_refs": []}, False),   # wrong case
        ("reviewer", {"verdict": "fail", "reasons": [], "evidence_refs": []}, False),   # wrong case 2
        ("reviewer", {"verdict": "ok",   "reasons": [], "evidence_refs": []}, False),   # non-enum
        ("reviewer", {"verdict": 1,      "reasons": [], "evidence_refs": []}, False),   # not string
        # Missing required arrays:
        ("reviewer", {"verdict": "PASS"}, False),
        ("reviewer", {"verdict": "PASS", "reasons": []}, False),
        # Wrong array type:
        ("reviewer", {"verdict": "PASS", "reasons": "nope", "evidence_refs": []}, False),
        ("reviewer", {"verdict": "PASS", "reasons": [], "evidence_refs": "nope"}, False),
        # Array item type rules (array[string] only, any other type rejected):
        ("reviewer", {"verdict": "PASS", "reasons": [1], "evidence_refs": []}, False),
        ("reviewer", {"verdict": "PASS", "reasons": [None], "evidence_refs": []}, False),
        ("reviewer", {"verdict": "PASS", "reasons": [{}], "evidence_refs": []}, False),
        ("reviewer", {"verdict": "PASS", "reasons": ["ok", 2], "evidence_refs": []}, False),
        ("reviewer", {"verdict": "PASS", "reasons": [], "evidence_refs": [{"id":"x"}]}, False),
        ("reviewer", {"verdict": "PASS", "reasons": [], "evidence_refs": [None]}, False),
        ("reviewer", {"verdict": "PASS", "reasons": [], "evidence_refs": [3.14]}, False),
    ]

    mismatches: list[str] = []
    for (role, obj, expect_valid) in TABLE:
        reason = _validate_station_schema(role, obj)
        actually_valid = reason is None
        if actually_valid != expect_valid:
            mismatches.append(
                f"PARITY MISMATCH role={role} obj={obj!r} expect_valid={expect_valid} got reason={reason!r}"
            )
    assert len(mismatches) == 0, "\n".join(mismatches)


# ============ REDI-03L3B: ScopeViolation canonicalization at the factory/evidence boundary ============


def test_l3b_scope_violation_failure_path_persists_canonical_failure_record():
    """REDI-03L3B regression: the ACTUAL factory failure path (builder emits
    patches outside allowed scope → apply_patches returns ScopeViolation objects)
    must produce a persistable FailureRecord whose details hold canonical
    primitive mappings — never ScopeViolation runtime objects — with deterministic
    payload hashes and replay-preserving violation meaning.
    """
    from redi_lab.factory.isolation import ScopeViolation

    out_of_scope_builder = FakeScriptedWorker(outputs_by_case={
        "default": lambda attempt: {
            "patches": [
                PatchEdit(rel_path="outside_escape/evil.py", action="write", content="x = 1\n"),
            ],
            "tokens": (10, 10),
        }
    })
    deps = _make_default_deps()
    deps.builder_worker = out_of_scope_builder
    product, task, _, _ = _make_specs()

    outcome = MinimalFactoryController.run(product, task, deps)
    assert outcome.verified is False
    assert outcome.escalation is True
    assert outcome.final_status == RunStatus.BLOCKED
    assert outcome.attempts == 2  # max_retries=1 → bounded retry then escalation

    conn = sqlite3.connect(outcome.db_path)
    try:
        failure_rows = conn.execute(
            "SELECT global_sequence, payload_json, payload_sha256 FROM evidence_entries "
            "WHERE entity_type = 'FailureRecord' ORDER BY global_sequence"
        ).fetchall()
        # One canonical FailureRecord per failed attempt.
        assert len(failure_rows) == 2
        seen_payloads = set()
        for _, payload_json, payload_sha in failure_rows:
            # Persisted representation is primitive JSON: no runtime repr / addresses.
            assert "<ScopeViolation object" not in payload_json
            assert "ScopeViolation object at 0x" not in payload_json
            rec = domain_from_json(payload_json)
            assert isinstance(rec, FailureRecord)
            assert rec.category == FailureCategory.SCOPE_VIOLATION
            assert rec.origin == "apply_patches_scope_check"
            violations = rec.details["violations"]
            assert isinstance(violations, list) and violations
            for v in violations:
                # Primitive mappings, not ScopeViolation instances.
                assert isinstance(v, dict)
                assert not isinstance(v, ScopeViolation)
                assert set(v.keys()) == {"type", "category", "message", "offending_path"}
                assert v["type"] == "ScopeViolation"
                assert v["category"] == "scope_violation"
                assert v["offending_path"] == "outside_escape/evil.py"
            seen_payloads.add(payload_json)
        # Same violation shape each attempt → identical canonical payload bytes
        # (only details.attempt differs, so payloads differ per attempt but each
        # re-serialization is stable; verify by re-encoding the decoded record).
        for _, payload_json, _ in failure_rows:
            assert domain_to_json(domain_from_json(payload_json)) == payload_json

        # GATE_FAILED event payloads also carry canonical reasons, not runtime objects.
        gate_failed_rows = conn.execute(
            "SELECT payload_json FROM evidence_entries WHERE entity_type = 'LifecycleEvent'"
        ).fetchall()
        gate_failed_payloads = [
            json.loads(r[0]) for r in gate_failed_rows
            if json.loads(r[0]).get("event_type") == "GATE_FAILED"
        ]
        assert len(gate_failed_payloads) == 2
        for ev in gate_failed_payloads:
            for reason in ev["payload"]["reasons"]:
                assert isinstance(reason, dict)
                assert reason["type"] == "ScopeViolation"
                assert reason["offending_path"] == "outside_escape/evil.py"
    finally:
        conn.close()

    # Replay/replay-again: violation meaning preserved, integrity clean, deterministic.
    from redi_lab.domain import FactoryRunId
    from redi_lab.replay import ReplayReader

    reader = ReplayReader(outcome.db_path)
    try:
        r1 = reader.replay_factory_run(FactoryRunId(outcome.factory_run_id))
        r2 = reader.replay_factory_run(FactoryRunId(outcome.factory_run_id))
        assert r1.integrity_status == "OK"
        assert r1.integrity_flags == ()
        assert r1.observed_run_status == r2.observed_run_status == "blocked"
        replay_failures = [f for f in r1.failures if isinstance(f, FailureRecord)]
        assert len(replay_failures) == 2
        for rec in replay_failures:
            assert rec.category == FailureCategory.SCOPE_VIOLATION
            assert rec.details["violations"][0]["offending_path"] == "outside_escape/evil.py"
            assert rec.details["violations"][0]["type"] == "ScopeViolation"
    finally:
        reader.close()
