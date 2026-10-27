"""stdlib-only REDI-05A validation runner (Context Firewall / Compiler foundation).

Deterministic, zero-network. Mirrors the REDI-01..04 pack pattern:
    python -m scripts.run_redi_05_validations
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


def _workspace():
    from redi_lab.domain import ProductSpec, Requirement, RiskTier, StationContract, TaskContract

    ws = Path(tempfile.mkdtemp()) / "ws"
    (ws / "slugify").mkdir(parents=True)
    (ws / "tests").mkdir()
    (ws / "slugify" / "__init__.py").write_text(
        '"""Stub."""\n\ndef slugify(text, *, maxlen=None, separator="-"):\n    return text\n', encoding="utf-8")
    (ws / "tests" / "test_slugify_public.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (ws / "NOTES.md").write_text("unrelated notes", encoding="utf-8")
    (ws / "credentials.py").write_text("API_KEY = 'sk-abcdef1234567890'\n", encoding="utf-8")
    (ws / "tests" / "test_hidden_extra.py").write_text("HIDDEN = True\n", encoding="utf-8")
    product = ProductSpec(
        requested_outcome="Canonical slugify implementation task.",
        requirements=[Requirement(text="public tests pass", kind="functional")],
        constraints=["Only modify slugify/__init__.py."],
        risk_tier=RiskTier.LOW, unresolved_requirements=[],
    )
    task = TaskContract(goal="implement slugify", allowed_scope=["slugify", "tests"])
    station = StationContract(
        role="builder", required_output_kinds=["patch_edit"], allowed_tool_classes=[],
        applicable_task_classes=["software_patch"],
    )
    return ws, product, task, station


def c01_selected_v0_deterministic():
    from redi_lab.context_compiler import compile_context, CONTEXT_STRATEGY_SELECTED_V0

    ws, product, task, station = _workspace()
    sel = ["slugify/__init__.py", "tests/test_slugify_public.py"]
    p1 = compile_context(strategy_id=CONTEXT_STRATEGY_SELECTED_V0, task=task, station=station, product=product,
                         workspace_root=ws, selected_paths=sel)
    p2 = compile_context(strategy_id=CONTEXT_STRATEGY_SELECTED_V0, task=task, station=station, product=product,
                         workspace_root=ws, selected_paths=sel)
    assert p1.package_hash == p2.package_hash and p1.rendered_hash == p2.rendered_hash
    assert [f["path"] for f in p1.included_files] == sel
    assert p1.counts["token_estimate_method"] == "CHARS_PER_4"


def c02_full_public_v0_excludes_hidden_secret():
    from redi_lab.context_compiler import compile_context, CONTEXT_STRATEGY_FULL_PUBLIC_V0

    ws, product, task, station = _workspace()
    full = compile_context(strategy_id=CONTEXT_STRATEGY_FULL_PUBLIC_V0, task=task, station=station,
                           product=product, workspace_root=ws)
    included = sorted(f["path"] for f in full.included_files)
    assert "slugify/__init__.py" in included and "tests/test_slugify_public.py" in included
    assert all("hidden" not in p and "credentials" not in p for p in included)
    ex = {f["path"]: f["reason"] for f in full.excluded_files}
    assert ex["tests/test_hidden_extra.py"] == "HIDDEN_TEST_BOUNDARY"
    assert ex["credentials.py"] == "SECRET_BLOCKLIST_PATH"


def c03_strategy_change_changes_hash():
    from redi_lab.context_compiler import compile_context, CONTEXT_STRATEGY_SELECTED_V0, CONTEXT_STRATEGY_FULL_PUBLIC_V0

    ws, product, task, station = _workspace()
    sel = compile_context(strategy_id=CONTEXT_STRATEGY_SELECTED_V0, task=task, station=station, product=product,
                          workspace_root=ws, selected_paths=["slugify/__init__.py"])
    full = compile_context(strategy_id=CONTEXT_STRATEGY_FULL_PUBLIC_V0, task=task, station=station, product=product,
                           workspace_root=ws)
    assert sel.package_hash != full.package_hash
    assert (sel.task_id, sel.station_id, sel.product_id) == (full.task_id, full.station_id, full.product_id)


def c04_file_change_changes_hash():
    from redi_lab.context_compiler import compile_context, CONTEXT_STRATEGY_SELECTED_V0

    ws, product, task, station = _workspace()
    f = ws / "slugify" / "__init__.py"
    f.write_text("x = 1\n", encoding="utf-8", newline="\n")
    p1 = compile_context(strategy_id=CONTEXT_STRATEGY_SELECTED_V0, task=task, station=station, product=product,
                         workspace_root=ws, selected_paths=["slugify/__init__.py"])
    f.write_text("x = 2\n", encoding="utf-8", newline="\n")
    p2 = compile_context(strategy_id=CONTEXT_STRATEGY_SELECTED_V0, task=task, station=station, product=product,
                         workspace_root=ws, selected_paths=["slugify/__init__.py"])
    assert p1.package_hash != p2.package_hash and p1.rendered_hash != p2.rendered_hash


def c05_budget_overflow_fails_closed():
    from redi_lab.context_compiler import compile_context, CONTEXT_STRATEGY_SELECTED_V0, ContextBudgetError

    ws, product, task, station = _workspace()
    try:
        compile_context(strategy_id=CONTEXT_STRATEGY_SELECTED_V0, task=task, station=station, product=product,
                        workspace_root=ws, selected_paths=["slugify/__init__.py"], budget_bytes=64)
    except ContextBudgetError as e:
        assert "exceeds budget" in str(e)
        return
    raise AssertionError("budget overflow did not fail closed")


def c06_selected_v0_firewall_fail_closed():
    from redi_lab.context_compiler import compile_context, CONTEXT_STRATEGY_SELECTED_V0, ContextFirewallError

    ws, product, task, station = _workspace()
    for bad_paths in (["../outside.py"], ["credentials.py"], ["tests/test_hidden_extra.py"], ["missing.py"]):
        try:
            compile_context(strategy_id=CONTEXT_STRATEGY_SELECTED_V0, task=task, station=station, product=product,
                            workspace_root=ws, selected_paths=bad_paths)
        except ContextFirewallError:
            continue
        raise AssertionError(f"selected {bad_paths} did not fail closed")


def c07_prior_failure_evidence_boundary():
    from redi_lab.context_compiler import (
        compile_context, CONTEXT_STRATEGY_SELECTED_V0, ContextFirewall, ContextFirewallError,
    )

    ws, product, task, station = _workspace()
    fw = ContextFirewall(allowed_classes=frozenset({
        "task_contract", "station_contract", "product_contract", "source_files", "prior_failure_evidence"}))
    try:
        compile_context(strategy_id=CONTEXT_STRATEGY_SELECTED_V0, task=task, station=station, product=product,
                        workspace_root=ws, selected_paths=["slugify/__init__.py"], firewall=fw,
                        prior_failure_evidence=[{"attempt": 0, "category": "gate_failed",
                                                 "boundary": "hidden_test_execution", "reasons": ["x"]}])
        raise AssertionError("hidden-gate evidence accepted as context")
    except ContextFirewallError as e:
        assert "not context-eligible" in str(e)
    pkg = compile_context(strategy_id=CONTEXT_STRATEGY_SELECTED_V0, task=task, station=station, product=product,
                          workspace_root=ws, selected_paths=["slugify/__init__.py"], firewall=fw,
                          prior_failure_evidence=[{"attempt": 0, "category": "gate_failed",
                                                   "boundary": "public_test_execution", "reasons": ["non-zero exit: 2"]}])
    assert pkg.prior_failure_evidence[0]["boundary"] == "public_test_execution"
    # default firewall keeps prior-failure evidence out entirely
    base = compile_context(strategy_id=CONTEXT_STRATEGY_SELECTED_V0, task=task, station=station, product=product,
                           workspace_root=ws, selected_paths=["slugify/__init__.py"])
    assert base.prior_failure_evidence == () and pkg.package_hash != base.package_hash


def c08_upstream_handoff_allowlist():
    from redi_lab.context_compiler import compile_context, CONTEXT_STRATEGY_SELECTED_V0, ContextFirewall
    from redi_lab.domain import HandoffManifest, ArtifactRef, StationId

    ws, product, task, station = _workspace()
    hm = HandoffManifest(
        producer_station_id=StationId("station_up"),
        produced_artifacts=[ArtifactRef(ref_id="a.py", kind="file", location="up/a.py", sha256="cd" * 32)],
    )
    base = compile_context(strategy_id=CONTEXT_STRATEGY_SELECTED_V0, task=task, station=station, product=product,
                           workspace_root=ws, selected_paths=["slugify/__init__.py"])
    assert base.upstream_handoffs == ()
    fw = ContextFirewall(allowed_classes=frozenset({
        "task_contract", "station_contract", "product_contract", "source_files", "upstream_handoffs"}))
    with_h = compile_context(strategy_id=CONTEXT_STRATEGY_SELECTED_V0, task=task, station=station, product=product,
                             workspace_root=ws, selected_paths=["slugify/__init__.py"],
                             handoff_manifests=[hm], firewall=fw)
    assert with_h.upstream_handoffs[0]["handoff_id"] == str(hm.id)
    assert with_h.package_hash != base.package_hash


def c09_manifest_strategy_binding():
    from redi_lab.factory.builder_context import build_builder_context

    ws, product, task, station = _workspace()
    m1 = build_builder_context(task=task, station=station, product=product, fixture_root=ws,
                               allowed_scope=["slugify", "tests"], context_strategy_id="selected_v0")
    m2 = build_builder_context(task=task, station=station, product=product, fixture_root=ws,
                               allowed_scope=["slugify", "tests"], context_strategy_id="full_public_v0")
    assert m1.metadata["context_strategy_id"] == "selected_v0"
    assert m1.deterministic_hash != m2.deterministic_hash


# ---------------------------------------------------------------------------
# REDI-05B — runtime compiler integration
# ---------------------------------------------------------------------------


@dataclass
class _PatchWorker:
    content: str
    calls: int = 0

    def run_node(self, *, task_id, station_id, worker_id, context, workspace_root, station_role="builder"):
        from redi_lab.runtime.contracts import NodeRunResult, PatchEdit

        self.calls += 1
        return NodeRunResult(
            worker_id=worker_id, task_id=task_id, station_id=station_id,
            patches=(PatchEdit(rel_path="slugify/__init__.py", action="write", content=self.content),),
            attempt_index=int((context or {}).get("attempt_index", 0)), elapsed_seconds=0.001,
        )


def _pool_deps_05b(station, pool, runners, *, strategy="selected_v0", budget=None, max_retries=1):
    from redi_lab.context_compiler import DEFAULT_BUDGET_BYTES
    from redi_lab.factory.controller import RunDeps, make_canonical_slug_fixture_refs
    from redi_lab.runtime.fake_worker import fake_reviewer_pass

    from redi_lab.domain import StationContract as _SC

    reviewer = _SC(role="reviewer", required_output_kinds=["verdict"])
    refs = make_canonical_slug_fixture_refs(REPO_ROOT)
    return RunDeps(
        builder_worker=_PatchWorker(content="x = 1\n"),
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
        context_strategy=strategy,
        context_budget_bytes=budget if budget is not None else DEFAULT_BUDGET_BYTES,
    )


def _specs_05b():
    from redi_lab.domain import StationContract

    _, _, station, _ = _workspace()
    return station


def _product_task_05b():
    from redi_lab.domain import ProductSpec, Requirement, RiskTier, TaskContract

    product = ProductSpec(
        requested_outcome="Canonical slugify implementation task.",
        requirements=[Requirement(text="public tests pass", kind="functional")],
        constraints=["Only modify slugify/__init__.py."],
        risk_tier=RiskTier.LOW, unresolved_requirements=[],
    )
    task = TaskContract(goal="implement slugify")
    return product, task


def b01_runtime_context_package_evidence():
    from redi_lab.context_compiler import CONTEXT_STRATEGY_SELECTED_V0
    from redi_lab.domain import PoolEntry, RunStatus, WorkerProfile, WorkerId, WorkerPool
    from redi_lab.factory.controller import MinimalFactoryController
    from redi_lab.runtime.fake_worker import FakeScriptedWorker

    ws, product, task, station = _workspace()
    pool = WorkerPool(station_id=station.id, role="builder", entries=[
        PoolEntry(profile=WorkerProfile(id=WorkerId("worker_a_ok"), display_name="worker_a_ok",
                                        version="v1", availability_status="available",
                                        station_eligibility=[station.id]), preference_rank=0)])
    runner = FakeScriptedWorker.case_fix_slugify_builder()
    deps = _pool_deps_05b(station, pool, {"worker_a_ok": runner})
    product, task = _product_task_05b()
    outcome = MinimalFactoryController.run(product, task, deps)
    assert outcome.final_status == RunStatus.COMPLETED and outcome.verified is True
    conn = sqlite3.connect(outcome.db_path)
    try:
        rows = conn.execute(
            "SELECT payload_json FROM evidence_entries WHERE entity_type='RunEvidence' "
            "AND payload_json LIKE '%N_BUILDER_0%' ORDER BY global_sequence").fetchall()
        ev = json.loads(rows[0][0])
        cp = ev["metadata"]["context_package"]
    finally:
        conn.close()
    assert cp["strategy_id"] == CONTEXT_STRATEGY_SELECTED_V0 and cp["strategy_version"] == 1
    assert cp["rendered_hash"] == ev["metadata"]["builder_prompt_sha256"]
    assert [f["path"] for f in cp["included_files"]] == [
        "pytest.ini", "slugify/__init__.py", "tests/test_slugify_public.py"]
    assert cp["counts"]["bytes"] > 0 and cp["budget_bytes"] > 0
    assert cp["prior_failure_evidence"] == []


def _product_task_05b():
    from redi_lab.domain import ProductSpec, Requirement, RiskTier, TaskContract

    product = ProductSpec(
        requested_outcome="Canonical slugify implementation task.",
        requirements=[Requirement(text="public tests pass", kind="functional")],
        constraints=["Only modify slugify/__init__.py."],
        risk_tier=RiskTier.LOW, unresolved_requirements=[],
    )
    task = TaskContract(goal="implement slugify")
    return product, task


def b02_budget_overflow_worker_not_called():
    from redi_lab.domain import RunStatus, WorkerProfile, PoolEntry, WorkerId, WorkerPool
    from redi_lab.factory.controller import MinimalFactoryController
    from redi_lab.runtime.fake_worker import FakeScriptedWorker

    ws, product, task, station = _workspace()
    pool = WorkerPool(station_id=station.id, role="builder", entries=[
        PoolEntry(profile=WorkerProfile(id=WorkerId("worker_a"), display_name="worker_a", version="v1",
                                        availability_status="available",
                                        station_eligibility=[station.id]), preference_rank=0)])
    runner = _PatchWorker(content="x = 1\n")
    deps = _pool_deps_05b(station, pool, {"worker_a": runner}, budget=64)
    product, task = _product_task_05b()
    outcome = MinimalFactoryController.run(product, task, deps)
    assert outcome.final_status == RunStatus.BLOCKED
    assert runner.calls == 0  # fail-closed BEFORE any worker invocation
    conn = sqlite3.connect(outcome.db_path)
    try:
        frs = conn.execute(
            "SELECT payload_json FROM evidence_entries WHERE entity_type='FailureRecord'").fetchall()
        assert any(json.loads(p[0])["origin"] == "context_compile_rejected" for p in frs)
        assert any(json.loads(p[0])["category"] == "context_insufficiency" for p in frs)
    finally:
        conn.close()


def b03_fallback_keeps_strategy_stable_and_changes_package_by_feedback():
    from redi_lab.context_compiler import CONTEXT_STRATEGY_SELECTED_V0
    from redi_lab.domain import RunStatus, WorkerProfile, PoolEntry, WorkerId, WorkerPool
    from redi_lab.factory.controller import MinimalFactoryController
    from redi_lab.runtime.fake_worker import FakeScriptedWorker

    ws, product, task, station = _workspace()
    failing = (
        "import unicodedata\nimport re\n\n\n"
        "def slugify(text: str, *, maxlen=None, separator='-'):\n"
        "    if not isinstance(text, str):\n        return text\n"
        "    t = re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')\n    return t\n"
    )
    pool = WorkerPool(station_id=station.id, role="builder", entries=[
        PoolEntry(profile=WorkerProfile(id=WorkerId("worker_a_fail"), display_name="worker_a_fail",
                                        version="v1", availability_status="available",
                                        station_eligibility=[station.id]), preference_rank=0),
        PoolEntry(profile=WorkerProfile(id=WorkerId("worker_b_ok"), display_name="worker_b_ok",
                                        version="v1", availability_status="available",
                                        station_eligibility=[station.id]), preference_rank=1)])
    runner_a = _PatchWorker(content=failing)
    runner_b = FakeScriptedWorker.case_fix_slugify_builder()
    deps = _pool_deps_05b(station, pool, {"worker_a_fail": runner_a, "worker_b_ok": runner_b}, max_retries=1)
    product, task = _product_task_05b()
    outcome = MinimalFactoryController.run(product, task, deps)
    assert outcome.verified is True
    conn = sqlite3.connect(outcome.db_path)
    try:
        rows = conn.execute(
            "SELECT payload_json FROM evidence_entries WHERE entity_type='RunEvidence' "
            "AND payload_json LIKE '%N_BUILDER_0%' ORDER BY global_sequence").fetchall()
        cps = [json.loads(p[0])["metadata"]["context_package"] for p in rows]
    finally:
        conn.close()
    assert [c["strategy_id"] for c in cps] == [CONTEXT_STRATEGY_SELECTED_V0, CONTEXT_STRATEGY_SELECTED_V0]
    assert cps[0]["package_hash"] != cps[1]["package_hash"]  # feedback legitimately changes the package
    # hidden-gate fallback feedback must stay OPAQUE (no names/paths/assertions)
    assert cps[1]["prior_failure_evidence"][0]["boundary"] == "hidden_test_execution"
    assert cps[1]["prior_failure_evidence"][0]["reasons"] == ["hidden verification failed"]
    assert "diagnostic" not in cps[1]["prior_failure_evidence"][0]


def b04_replay_context_identity_twice():
    from redi_lab.context_compiler import CONTEXT_STRATEGY_SELECTED_V0
    from redi_lab.domain import FactoryRunId, WorkerProfile, PoolEntry, WorkerId, WorkerPool
    from redi_lab.factory.controller import MinimalFactoryController
    from redi_lab.replay import ReplayReader
    from redi_lab.runtime.fake_worker import FakeScriptedWorker

    ws, product, task, station = _workspace()
    pool = WorkerPool(station_id=station.id, role="builder", entries=[
        PoolEntry(profile=WorkerProfile(id=WorkerId("worker_a_ok"), display_name="worker_a_ok",
                                        version="v1", availability_status="available",
                                        station_eligibility=[station.id]), preference_rank=0)])
    runner = FakeScriptedWorker.case_fix_slugify_builder()
    deps = _pool_deps_05b(station, pool, {"worker_a_ok": runner})
    product, task = _product_task_05b()
    outcome = MinimalFactoryController.run(product, task, deps)

    def snapshot():
        reader = ReplayReader(outcome.db_path)
        try:
            res = reader.replay_factory_run(FactoryRunId(outcome.factory_run_id))
        finally:
            reader.close()
        evs = [e for e in res.run_evidences if getattr(e, "node_id", "") == "N_BUILDER_0"]
        return [dict(e.metadata["context_package"]) for e in evs], res

    s1, r1 = snapshot()
    s2, r2 = snapshot()
    assert r1.integrity_status == "OK" and r1.observed_run_status == "completed"
    assert s1 == s2 and len(s1) == 1
    assert s1[0]["strategy_id"] == CONTEXT_STRATEGY_SELECTED_V0


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# REDI-05C — paired context-strategy experiment harness
# ---------------------------------------------------------------------------


def p01_pair_dry_run_and_provenance():
    from redi_lab.context_compiler import CONTEXT_STRATEGY_FULL_PUBLIC_V0, CONTEXT_STRATEGY_SELECTED_V0
    from redi_lab.experiments.context_pilot import run_pair

    manifest = m_build_experiment_manifest()
    pair_dir = tempfile.mkdtemp()
    pair = run_pair(manifest=manifest, order=("selected_v0", "full_public_v0"), db_dir=pair_dir)
    s = pair.conditions["selected_v0"]
    f = pair.conditions["full_public_v0"]
    assert s.outcome_status == "completed" and s.verified
    assert f.outcome_status == "completed" and f.verified
    assert s.metrics["included_source_paths"] != f.metrics["included_source_paths"]
    assert "utils/text_utils.py" in f.metrics["included_source_paths"]
    assert "utils/text_utils.py" not in s.metrics["included_source_paths"]
    assert s.metrics["context_byte_counts"][0] < f.metrics["context_byte_counts"][0]
    # both runs bound to one experiment identity
    pair_dbs = [str(Path(pair_dir) / f"context_pilot_{i}_{sname}.db")
                for i, sname in enumerate(pair.order)]
    for db in pair_dbs:
        assert Path(db).exists()
        conn = sqlite3.connect(db)
        try:
            exp = {r[0] for r in conn.execute("SELECT experiment_id FROM factory_runs").fetchall()}
            assert exp == {manifest.id}
        finally:
            conn.close()


def m_build_experiment_manifest():
    from redi_lab.experiments.context_pilot import build_experiment_manifest

    return build_experiment_manifest()



def p02_pilot_runner_env_gates_and_structural_live_binding():
    import importlib.util
    import redi_lab.runtime.live_provider_adapter as lpa

    spec = importlib.util.spec_from_file_location(
        "_redi05c_pilot_runner", REPO_ROOT / "scripts" / "run_redi_05_context_pilot.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    old_provider = os.environ.pop("REDI_LIVE_PROVIDER", None)
    old_key = os.environ.pop("REDI_OPENAI_API_KEY", None)
    buf = io.StringIO()
    _stdout = sys.stdout
    sys.stdout = buf
    try:
        rc = mod.main()
    finally:
        sys.stdout = _stdout
    assert rc == 0
    assert "REDI-05 PILOT BLOCKED: REDI_LIVE_PROVIDER not set" in buf.getvalue()

    os.environ["REDI_LIVE_PROVIDER"] = "1"
    buf = io.StringIO()
    sys.stdout = buf
    try:
        rc = mod.main()
    finally:
        sys.stdout = _stdout
    assert rc == 0
    assert "REDI-05 PILOT BLOCKED: REDI_OPENAI_API_KEY not set" in buf.getvalue()
    assert "sk-" not in buf.getvalue()

    # structural: pilot pool composition binds the live adapter as Worker B
    # (Worker A = deterministic defect, order A then B) without executing
    calls = {"n": 0}
    original = lpa._sdk_available

    def _counting():
        calls["n"] += 1
        return True

    lpa._sdk_available = _counting
    try:
        adapter = lpa.LiveProviderAdapter(config=lpa.LiveProviderConfig(env_key_name="REDI_OPENAI_API_KEY"))
        deps, product, task = cp_build_pilot_deps(adapter)
    finally:
        lpa._sdk_available = original
    from redi_lab.runtime.live_provider_adapter import LiveProviderAdapter as _LPA

    b_runner = deps.builder_runners["worker_b_live_openai"]
    assert isinstance(b_runner, _LPA)
    ids = [str(e.profile.id) for e in deps.builder_pool.entries]
    assert ids == ["worker_a_deterministic_defect", "worker_b_live_openai"]
    assert calls["n"] == 0


def cp_build_pilot_deps(adapter):
    from redi_lab.experiments.context_pilot import build_pilot_deps

    return build_pilot_deps(live_builder=adapter, strategy="selected_v0")




# ---------------------------------------------------------------------------
# REDI-05C1 — ExperimentManifest / FactoryRun ledger binding
# ---------------------------------------------------------------------------


def c10_fk_rejects_unknown_experiment_id():
    from redi_lab.domain import FactoryRun
    from redi_lab.persistence import EvidenceLedger, IncompleteEvidenceError
    from redi_lab.persistence.repositories import FactoryRunRepository

    db = os.path.join(tempfile.mkdtemp(), "fk.db")
    conn = sqlite3.connect(db)
    try:
        from redi_lab.persistence import bootstrap_schema
        bootstrap_schema(conn)
        conn.execute("PRAGMA foreign_keys = ON;")
        repo = FactoryRunRepository(conn, EvidenceLedger(conn))
        try:
            repo.store(FactoryRun(), experiment_id="experiment_missing")
            raise AssertionError("FK should have rejected the unknown experiment_id")
        except IncompleteEvidenceError:
            pass
        assert conn.execute("SELECT COUNT(*) FROM factory_runs").fetchone()[0] == 0
    finally:
        conn.close()


def c11_live_runner_storage_lifecycle_regression():
    from redi_lab.experiments.context_pilot import build_experiment_manifest, run_pair

    manifest = build_experiment_manifest(model="gpt-4o-mini")
    pair = run_pair(live_builder=None, manifest=manifest,
                    order=("selected_v0", "full_public_v0"))  # no db_dir — runner shape
    payloads, hashes, run_ids = set(), set(), []
    for strategy, db_path in pair.condition_db_paths.items():
        conn = sqlite3.connect(db_path)
        try:
            em = conn.execute(
                "SELECT id, payload_json, payload_sha256 FROM experiment_manifests").fetchall()
            assert len(em) == 1 and em[0][0] == pair.experiment_id
            payloads.add(em[0][1]); hashes.add(em[0][2])
            runs = conn.execute("SELECT id, experiment_id FROM factory_runs").fetchall()
            assert len(runs) == 1 and runs[0][1] == pair.experiment_id
            run_ids.append(runs[0][0])
            from redi_lab.persistence import payload_hash_from_json
            assert em[0][2] == payload_hash_from_json(em[0][1])
        finally:
            conn.close()
    assert len(payloads) == 1 and len(hashes) == 1
    assert len(set(run_ids)) == 2
    assert pair.conditions["selected_v0"].verified is True
    assert pair.conditions["full_public_v0"].verified is True


def c12_partial_failure_safety_budget_condition():
    from redi_lab.context_compiler import DEFAULT_BUDGET_BYTES
    from redi_lab.domain import RunStatus
    from redi_lab.experiments.context_pilot import build_experiment_manifest, run_pair
    from redi_lab.runtime.fake_worker import FakeScriptedWorker

    ws, product, task, station = _workspace()
    deps_s = _pool_deps_05b(station, _pool_for(station, [("worker_a_ok", 0)]),
                             {"worker_a_ok": FakeScriptedWorker.case_fix_slugify_builder()})
    small_pool = _pool_for(station, [("worker_a_budget", 0)])
    deps_f = _pool_deps_05b(station, small_pool, {"worker_a_budget": FakeScriptedWorker.case_fix_slugify_builder()},
                            strategy="full_public_v0")
    deps_f.context_budget_bytes = 64  # force deterministic compile rejection on F

    manifest = build_experiment_manifest(model="gpt-4o-mini")
    pair_dir = tempfile.mkdtemp()
    pair = run_pair(manifest=manifest, order=("selected_v0", "full_public_v0"),
                    db_dir=pair_dir, deps_factory=None) if False else None
    # drive conditions manually through the same public entry point
    pair = run_pair_with_deps(
        manifest=manifest, db_dir=pair_dir,
        deps_by_strategy={"selected_v0": deps_s, "full_public_v0": deps_f})

    assert pair.conditions["selected_v0"].outcome_status == "completed"
    assert pair.conditions["full_public_v0"].outcome_status == "blocked"
    dbs = pair.condition_db_paths
    conn = sqlite3.connect(dbs["selected_v0"])
    try:
        assert conn.execute("SELECT verified FROM factory_runs").fetchone()[0] == 1
    finally:
        conn.close()
    conn = sqlite3.connect(dbs["full_public_v0"])
    try:
        fr = conn.execute(
            "SELECT payload_json FROM evidence_entries WHERE entity_type='FailureRecord'").fetchall()
        assert any(json.loads(p[0])["origin"] == "context_compile_rejected" for p in fr)
    finally:
        conn.close()
    payloads = set()
    for db in dbs.values():
        conn = sqlite3.connect(db)
        try:
            payloads.add(conn.execute("SELECT payload_json FROM experiment_manifests").fetchone()[0])
        finally:
            conn.close()
    assert len(payloads) == 1


def _pool_for(station, ranked):
    from redi_lab.domain import PoolEntry, WorkerProfile, WorkerId, WorkerPool

    entries = [PoolEntry(profile=WorkerProfile(id=WorkerId(wid), display_name=wid, version="v1",
                                               availability_status="available",
                                               station_eligibility=[station.id]), preference_rank=rank)
               for wid, rank in ranked]
    return WorkerPool(station_id=station.id, role="builder", entries=entries)


def run_pair_with_deps(*, manifest, db_dir, deps_by_strategy):
    from redi_lab.experiments.context_pilot import DEFAULT_ORDER, preregister_into
    from redi_lab.factory.controller import MinimalFactoryController

    order = list(DEFAULT_ORDER)
    for db in (Path(db_dir) / "context_pilot_0_selected_v0.db",
               Path(db_dir) / "context_pilot_1_full_public_v0.db"):
        preregister_into(db, manifest)
    outcomes = {}
    for index, strategy in enumerate(order):
        outcome = MinimalFactoryController.run(
            *_specs_pair(), deps_by_strategy[strategy],
            db_path=Path(db_dir) / f"context_pilot_{index}_{strategy}.db",
            experiment_id=str(manifest.id))
        outcomes[strategy] = outcome
    return _PairShim(str(manifest.id), order, outcomes, db_dir)


class _PairShim:
    def __init__(self, experiment_id, order, outcomes, db_dir):
        self.experiment_id = experiment_id
        self._outcomes = outcomes
        self._db_dir = db_dir
        self.conditions = {
            "selected_v0": _CondShim(outcomes["selected_v0"]),
            "full_public_v0": _CondShim(outcomes["full_public_v0"]),
        }
        self.condition_db_paths = {
            "selected_v0": str(Path(db_dir) / "context_pilot_0_selected_v0.db"),
            "full_public_v0": str(Path(db_dir) / "context_pilot_1_full_public_v0.db"),
        }


class _CondShim:
    def __init__(self, outcome):
        self.outcome_status = outcome.final_status.value
        self.verified = outcome.verified


def _specs_pair():
    from redi_lab.domain import ProductSpec, Requirement, RiskTier, TaskContract

    product = ProductSpec(
        requested_outcome="Canonical slugify implementation task.",
        requirements=[Requirement(text="public tests pass", kind="functional")],
        constraints=["Only modify slugify/__init__.py."],
        risk_tier=RiskTier.LOW, unresolved_requirements=[],
    )
    task = TaskContract(goal="implement slugify")
    return product, task


def main() -> int:
    print("=" * 64)
    print("REDI-05 VALIDATIONS — Context Firewall / Compiler (deterministic)")
    print("=" * 64)
    check("c01 selected_v0 deterministic package + budget metadata", c01_selected_v0_deterministic)
    check("c02 full_public_v0 excludes hidden/secret files", c02_full_public_v0_excludes_hidden_secret)
    check("c03 strategy change changes package hash", c03_strategy_change_changes_hash)
    check("c04 file change changes package/rendered hash", c04_file_change_changes_hash)
    check("c05 budget overflow fails closed", c05_budget_overflow_fails_closed)
    check("c06 selected_v0 firewall fail-closed (traversal/secret/hidden/missing)", c06_selected_v0_firewall_fail_closed)
    check("c07 prior failure evidence: public in, hidden rejected", c07_prior_failure_evidence_boundary)
    check("c08 upstream handoff included only when allowlisted", c08_upstream_handoff_allowlist)
    check("c09 context strategy bound into ContextManifest builder", c09_manifest_strategy_binding)
    check("b01 runtime context package evidence (selected_v0)", b01_runtime_context_package_evidence)
    check("b02 budget overflow: worker not called, fail-closed", b02_budget_overflow_worker_not_called)
    check("b03 fallback keeps strategy stable; feedback changes package", b03_fallback_keeps_strategy_stable_and_changes_package_by_feedback)
    check("b04 replay reconstructs context identity twice", b04_replay_context_identity_twice)
    check("p01 05C pair dry-run: packages differ, provenance, one experiment", p01_pair_dry_run_and_provenance)
    check("p02 05C pilot runner env gates fail-closed; live binding structural", p02_pilot_runner_env_gates_and_structural_live_binding)
    check("c10 FK rejects unknown experiment_id (fail-closed)", c10_fk_rejects_unknown_experiment_id)
    check("c11 live-runner storage lifecycle: manifest in every condition DB", c11_live_runner_storage_lifecycle_regression)
    check("c12 partial failure safety: budget condition BLOCKED, S intact", c12_partial_failure_safety_budget_condition)
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
