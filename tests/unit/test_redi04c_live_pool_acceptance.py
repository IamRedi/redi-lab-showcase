"""REDI-04C — live WorkerPool acceptance runner tests (zero network).

The exact runner composition is exercised in dry-run mode (Worker B bound
to a deterministic CORRECT worker) proving:

  A fails the public gate deterministically
  → selector excludes A → Worker B selected (worker_fallback, previous=A)
  → B receives the bounded context + A's sanitized public diagnostic
  → all four gates PASS → independent Reviewer PASS → PRODUCT_VERIFIED
  → pool provenance (snapshot/hash/order) bound to plan + attempts
  → replay ×2 deterministic, zero provider calls

Live composition is verified structurally only (bindings/type/policy):
the runner is NEVER executed against the real provider by tests, and every
network-sensitive path is guarded by a provider-call sentinel.  Environment
gates are exercised with both variables removed.
"""

from __future__ import annotations

import json
import py_compile
import sqlite3
import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from redi_lab.domain import RunStatus, domain_from_json
from redi_lab.factory.controller import MinimalFactoryController
from redi_lab.replay import ReplayReader
from redi_lab.runtime.contracts import WorkerRunner
from redi_lab.runtime.fake_worker import FakeScriptedWorker
from redi_lab.worker_selection import (
    SELECTION_KIND_INITIAL,
    SELECTION_KIND_WORKER_FALLBACK,
    pool_snapshot_hash,
)

import importlib
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "run_redi_04_live_pool",
    Path(__file__).resolve().parents[2] / "scripts" / "run_redi_04_live_pool.py",
)
_runner = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _runner
_spec.loader.exec_module(_runner)

_REPO_ROOT = Path(_runner.__file__).resolve().parent.parent
_HIDDEN_MARKERS = ("slugify_pkg_hidden_tests", "test_hidden", "hidden_test_cwd")
_REPO_MARKERS = ("redi_lab/factory/controller.py", "live_provider_adapter", "builder_context.py", "conftest.py")


@dataclass
class _RecordingWorker(WorkerRunner):
    inner: WorkerRunner
    captured: list[dict] = field(default_factory=list)

    def run_node(self, *, task_id, station_id, worker_id, context, workspace_root, station_role="builder"):
        self.captured.append(dict(context))
        return self.inner.run_node(
            task_id=task_id, station_id=station_id, worker_id=worker_id, context=context,
            workspace_root=workspace_root, station_role=station_role,
        )


def _dry_run_deps_with_recorder(tmp_path: Path):
    """Dry-run composition with a recorder around Worker B (fake correct)."""
    deps, pool, product, task = _runner.build_run_deps(live_builder=None)
    recorder = _RecordingWorker(inner=deps.builder_runners[_runner.WORKER_B_ID])
    deps.builder_runners[_runner.WORKER_B_ID] = recorder
    return deps, pool, product, task, recorder


def test_04c_dry_run_full_acceptance_product_verified(tmp_path):
    deps, pool, product, task, recorder = _dry_run_deps_with_recorder(tmp_path)
    outcome = MinimalFactoryController.run(product, task, deps)
    assert outcome.final_status == RunStatus.COMPLETED
    assert outcome.verified is True
    assert outcome.escalation is False
    assert outcome.attempts == 2
    attempts = _builder_attempts(outcome.db_path)
    assert [a["worker_id"] for a in attempts] == [_runner.WORKER_A_ID, _runner.WORKER_B_ID]
    sel0, sel1 = attempts[0]["metadata"]["worker_selection"], attempts[1]["metadata"]["worker_selection"]
    assert sel0["selection_kind"] == SELECTION_KIND_INITIAL
    assert sel1["selection_kind"] == SELECTION_KIND_WORKER_FALLBACK
    assert sel1["previous_worker_id"] == _runner.WORKER_A_ID
    # no same-worker retry anywhere in the ladder
    kinds = {sel0["selection_kind"], sel1["selection_kind"]}
    assert "same_worker_retry" not in kinds
    # PRODUCT_VERIFIED persisted exactly once
    conn = sqlite3.connect(outcome.db_path)
    try:
        pv = conn.execute(
            "SELECT COUNT(*) FROM evidence_entries WHERE payload_json LIKE '%PRODUCT_VERIFIED%'"
        ).fetchone()[0]
        reviewer_pass = conn.execute(
            "SELECT COUNT(*) FROM evidence_entries WHERE entity_type='RunEvidence' "
            "AND payload_json LIKE '%N_REVIEWER_0%' AND payload_json LIKE '%\"verdict\": \"PASS\"%'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert pv == 1
    assert reviewer_pass == 1  # independent Reviewer ran and passed


def test_04c_worker_a_artifact_is_controlled_in_scope_public_safe_failure():
    # single in-scope patch
    worker = _runner.ControlledDefectWorker()
    result = worker.run_node(
        task_id=None, station_id=None, worker_id="worker_a_deterministic_defect",
        context={"attempt_index": 0}, workspace_root=None, station_role="builder",
    )
    patches = list(result.patches)
    assert len(patches) == 1
    assert patches[0].rel_path == "slugify/__init__.py"  # inside allowed scope
    content = patches[0].content
    # compiles cleanly → python_syntax_compile would PASS
    compile(content, "<worker_a_artifact>", "exec")
    # import-time NameError → public collection error (public gate FAIL)
    import os
    import tempfile
    fd, tmp_name = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    tmp = Path(tmp_name)
    tmp.write_text(content, encoding="utf-8")
    try:
        spec = importlib.util.spec_from_file_location("_redi04c_defect_mod", tmp)
        mod = importlib.util.module_from_spec(spec)
        with pytest.raises(NameError):
            spec.loader.exec_module(mod)
    finally:
        tmp.unlink()
    assert worker.calls == 1


def test_04c_pool_provenance_bound_to_plan_and_attempts(tmp_path):
    deps, pool, product, task, recorder = _dry_run_deps_with_recorder(tmp_path)
    outcome = MinimalFactoryController.run(product, task, deps)
    conn = sqlite3.connect(outcome.db_path)
    try:
        plan = json.loads(conn.execute(
            "SELECT payload_json FROM evidence_entries WHERE entity_type='LifecycleEvent' "
            "AND payload_json LIKE '%PLAN_COMPILED%' ORDER BY global_sequence LIMIT 1").fetchone()[0])["payload"]
        attempts = [json.loads(r[0]) for r in conn.execute(
            "SELECT payload_json FROM evidence_entries WHERE entity_type='RunEvidence' ORDER BY global_sequence")
            if json.loads(r[0]).get("node_id") == "N_BUILDER_0"]
    finally:
        conn.close()
    wp = plan["worker_pools"]["builder"]
    snap = wp["snapshot"]
    assert [c["worker_id"] for c in snap["candidates"]] == [_runner.WORKER_A_ID, _runner.WORKER_B_ID]
    assert [c["preference_rank"] for c in snap["candidates"]] == [0, 1]
    assert [c["profile_version"] for c in snap["candidates"]] == ["v1", "v1"]
    assert snap["selection_policy_id"] == "deterministic_v0"
    assert snap["selection_policy_version"] == 1
    assert wp["snapshot_hash"] == pool_snapshot_hash(snap)
    for a in attempts:
        sel = a["metadata"]["worker_selection"]
        assert sel["pool_id"] == snap["pool_id"]
        assert sel["pool_snapshot_hash"] == wp["snapshot_hash"]
        assert sel["policy_id"] == "deterministic_v0" and sel["policy_version"] == 1


def test_04c_worker_b_context_contains_task_and_a_diagnostic_is_isolated(tmp_path):
    deps, pool, product, task, recorder = _dry_run_deps_with_recorder(tmp_path)
    MinimalFactoryController.run(product, task, deps)
    assert len(recorder.captured) == 1
    ctx = recorder.captured[0]
    body = ctx["instructions"] + "\x00" + ctx["context_body"]
    # bounded task context (L6)
    assert "Requested outcome: Rewrite the stub slugify/__init__.py" in ctx["instructions"]
    assert "Goal: produce PatchEdits so slugify passes public and hidden tests" in ctx["instructions"]
    assert "hello-worl" in ctx["instructions"]  # acceptance criteria
    assert "FORBIDDEN" in ctx["instructions"]  # allowed scope boundary statement
    assert "### File: slugify/__init__.py" in ctx["context_body"]
    assert "### File: tests/test_slugify_public.py" in ctx["context_body"]
    # L9 retry diagnostic from Worker A's public failure
    assert '"category": "gate_failed"' in ctx["context_body"]
    assert '"boundary": "public_test_execution"' in ctx["context_body"]
    assert "NameError: name 'Optional' is not defined" in ctx["context_body"]
    # isolation: no hidden tests, no unrelated repo files, no absolute paths
    for marker in _HIDDEN_MARKERS + _REPO_MARKERS:
        assert marker not in body
    import re
    assert not re.search(r"[A-Za-z]:[\\/]", body)
    assert "/home/" not in body and "/Users/" not in body


def test_04c_live_composition_binds_live_adapter_structurally_only():
    from redi_lab.runtime.live_provider_adapter import (
        LiveProviderAdapter, LiveProviderConfig,
    )
    from redi_lab.worker_selection import pool_snapshot
    import redi_lab.runtime.live_provider_adapter as lpa

    calls = {"n": 0}
    original = lpa._sdk_available

    def _counting():
        calls["n"] += 1
        return True

    lpa._sdk_available = _counting
    try:
        adapter = LiveProviderAdapter(config=LiveProviderConfig(env_key_name="REDI_OPENAI_API_KEY"))
        deps, pool, product, task = _runner.build_run_deps(live_builder=adapter, model="gpt-4o-mini")
    finally:
        lpa._sdk_available = original

    b_runner = deps.builder_runners[_runner.WORKER_B_ID]
    assert isinstance(b_runner, LiveProviderAdapter)
    assert b_runner.config.model == "gpt-4o-mini"
    snap = pool_snapshot(pool=deps.builder_pool, station=deps.builder_station, task=task)
    b_entry = [c for c in snap["candidates"] if c["worker_id"] == _runner.WORKER_B_ID][0]
    assert b_entry["provider_opaque_reference"] == "openai:gpt-4o-mini"
    assert calls["n"] == 0  # composition performs zero provider probes/network


def test_04c_env_gates_fail_closed_without_provider(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("REDI_LIVE_PROVIDER", raising=False)
    monkeypatch.delenv("REDI_OPENAI_API_KEY", raising=False)
    rc = _runner.main()
    captured = capsys.readouterr().out
    assert rc == 0
    assert "REDI-04 LIVE BLOCKED: REDI_LIVE_PROVIDER not set in environment" in captured
    assert "sk-" not in captured

    monkeypatch.setenv("REDI_LIVE_PROVIDER", "1")
    rc = _runner.main()
    captured = capsys.readouterr().out
    assert rc == 0
    assert "REDI-04 LIVE BLOCKED: REDI_OPENAI_API_KEY not set in environment" in captured


def test_04c_replay_twice_identical_transitions_zero_providers(tmp_path):
    import redi_lab.runtime.live_provider_adapter as lpa

    deps, pool, product, task, recorder = _dry_run_deps_with_recorder(tmp_path)
    outcome = MinimalFactoryController.run(product, task, deps)
    assert outcome.verified is True

    calls = {"n": 0}
    original = lpa._sdk_available

    def _counting():
        calls["n"] += 1
        return False

    def snapshot():
        reader = ReplayReader(outcome.db_path)
        try:
            res = reader.replay_factory_run(outcome.factory_run_id)
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
    assert [w for w, _ in s1] == [_runner.WORKER_A_ID, _runner.WORKER_B_ID]
    assert [d["selection_kind"] for _, d in s1] == [SELECTION_KIND_INITIAL, SELECTION_KIND_WORKER_FALLBACK]
    assert calls["n"] == 0


# ---------------------------------------------------------------------------


def _builder_attempts(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT payload_json FROM evidence_entries WHERE entity_type='RunEvidence' ORDER BY global_sequence"
        ).fetchall()
        return [json.loads(p) for (p,) in rows if json.loads(p).get("node_id") == "N_BUILDER_0"]
    finally:
        conn.close()
