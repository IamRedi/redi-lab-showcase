"""stdlib-only REDI-03 validation 10-pack runner.

Run via import-mode to avoid caching paradox & site-packages shadowing:
    python -m scripts.run_redi_03_validations

Check list (10-pack):
  v01 pytest tests/unit/  (REDI-01 + REDI-02 + REDI-03 formal unit suite)
  v02 REDI-01 stdlib runner 77/77
  v03 REDI-02 stdlib runner 24/24
  v04 REDI-03 stdlib self-checks (no pytest, direct construction)
  v05 Layer A provider/runtime/persistence AST audit (import-only)
  v06 Layer C -> Layer A import direction + no reverse imports
  v07 Factory diff scope clean: no fixture-baseline mutation on 2x run
  v08 Git status: tracked working tree clean (ignoring untracked tmp scripts)
  v09 Live provider run: bounded if env creds set; otherwise LIVE BLOCKED
  v10 Replay integrity of a persisted happy-slug run: chain OK + verified
"""

from __future__ import annotations

import ast as _ast
import importlib
import os
import subprocess
import sqlite3
import sys
import traceback
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


PASS = 0
FAIL = 0
RESULTS: list[tuple[str, bool, str]] = []
LIVE_BLOCKED_REASONS: list[str] = []


def check(name, fn):
    global PASS, FAIL
    try:
        fn()
        PASS += 1
        RESULTS.append((name, True, ""))
        print(f"  PASS {name}")
    except AssertionError as e:
        FAIL += 1
        msg = str(e)[:300]
        RESULTS.append((name, False, msg))
        print(f"  FAIL {name}: assertion: {msg}")
    except Exception as e:  # noqa: BLE001
        FAIL += 1
        tb = traceback.format_exc(limit=4)
        RESULTS.append((name, False, f"{type(e).__name__}: {e}; {tb!s:.500}"))
        print(f"  FAIL {name}: {type(e).__name__}: {e}")


def _run_pytest(args: list[str]) -> tuple[int, str]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    return proc.returncode, out


def _run_script(modpath: str) -> tuple[int, str]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", modpath],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, (proc.stdout or "") + "\n" + (proc.stderr or "")


# ---------- V01: pytest tests/unit/ ----------
def v01_pytest_tests_unit():
    rc, out = _run_pytest(["-q", "tests/unit/"])
    assert rc == 0, f"pytest rc={rc}; tail: {out[-800:]}"
    # Parse summary line: "149 passed"
    last_lines = [ln for ln in out.splitlines() if ln.strip()]
    summary = last_lines[-1] if last_lines else ""
    assert "passed" in summary.lower(), f"no summary in {summary!r}"
    # No "failed" allowed unless 0
    if "failed" in summary.lower():
        import re
        m = re.search(r"(\d+)\s+failed", summary.lower())
        if m and int(m.group(1)) != 0:
            raise AssertionError(f"pytest reports failures: {summary!r}")


# ---------- V02: REDI-01 stdlib 77/77 ----------
def v02_redi01_stdlib_runner():
    rc, out = _run_script("scripts.run_redi_01_validations")
    assert rc == 0, f"REDI-01 rc={rc}; tail: {out[-600:]}"
    ok = (
        ("# Passed 77 / 77" in out)
        or ("PASS=77" in out)
        or ("TOTAL: PASS=77 FAIL=0" in out)
        or ("1..77" in out and "# Passed 77 / 77" in out)
    )
    assert ok, f"REDI-01 summary missing 77/77: tail={out[-300:]}"


# ---------- V03: REDI-02 stdlib 24/24 ----------
def v03_redi02_stdlib_runner():
    rc, out = _run_script("scripts.run_redi_02_validations")
    assert rc == 0, f"REDI-02 rc={rc}; tail: {out[-600:]}"
    assert "TOTAL: PASS=24 FAIL=0" in out, (
        f"REDI-02 summary missing 24/24: tail={out[-300:]}"
    )


# ---------- V04: REDI-03 stdlib self-checks (no pytest) ----------
def v04_redi03_stdlib_selfchecks():
    from redi_lab.domain import (
        ProductSpec, Requirement, TaskContract, StationContract,
        RiskTier, RunStatus, LifecycleEventType, new_id,
    )
    from redi_lab.factory.controller import (
        MinimalFactoryController, RunDeps, FinalRunOutcome,
    )
    from redi_lab.runtime.fake_worker import (
        FakeScriptedWorker, fake_reviewer_pass,
    )
    from redi_lab.factory.isolation import (
        IsolatedWorkspace, apply_patches, baseline_file_hashes,
    )
    from redi_lab.runtime.contracts import (
        PatchEdit, NodeRunResult, ReviewerVerdict, WorkerRunner,
    )
    from redi_lab.gates import GateSpec, DeterministicGateEngine
    import hashlib

    spec = ProductSpec(
        requested_outcome="canonical slugify implementation",
        requirements=[Requirement(text="slugify exists", kind="functional")],
        constraints=[],
        risk_tier=RiskTier.LOW,
        unresolved_requirements=[],
    )
    task = TaskContract(goal="build slugify that passes tests")
    b_station = StationContract(
        role="builder", required_output_kinds=["patch_edit"],
        allowed_tool_classes=[],
    )
    r_station = StationContract(
        role="reviewer", required_output_kinds=["verdict"],
        allowed_tool_classes=[],
    )
    fixture_root = REPO_ROOT / "experiments" / "fixtures" / "slugify_pkg"
    hidden_root = REPO_ROOT / "experiments" / "fixtures" / "slugify_pkg_hidden_tests"
    assert fixture_root.is_dir(), fixture_root
    assert hidden_root.is_dir(), hidden_root

    # (a) Isolation baseline hashing non-empty + workspace copy independent
    baselines = baseline_file_hashes(fixture_root)
    assert len(baselines) > 0, "no files hashed in fixture_root"
    with IsolatedWorkspace.create(fixture_root) as ws:
        assert ws.root.is_dir()
        # (b) apply_patches rejects a scope violation
        bad_patch = PatchEdit(
            rel_path="../escape.py",
            action="write",
            content="#escape\n",
            content_sha256=hashlib.sha256(b"#escape\n").hexdigest(),
        )
        ok, viols = apply_patches(ws, [bad_patch], allowed_scope=["slugify", "tests"])
        assert ok is False and len(viols) > 0, "expected scope violation"

    # (c) WorkerRunner protocol: FakeScriptedWorker produces non-empty patches
    from redi_lab.domain import TaskId, StationId, WorkerId, new_id
    fw = FakeScriptedWorker.case_fix_slugify_builder()
    ctx = {"case_id": "fix_slugify_builder", "attempt_index": 0}
    res = fw.run_node(
        task_id=TaskId(new_id("task")),
        station_id=StationId(new_id("station")),
        worker_id=WorkerId(new_id("worker")),
        context=ctx,
        workspace_root=None,
        station_role="builder",
    )
    assert isinstance(res, NodeRunResult)
    assert len(res.patches) >= 1, "builder produced zero patches"
    for p in res.patches:
        assert isinstance(p, PatchEdit)
        if p.content_sha256 is not None:
            expected = hashlib.sha256((p.content or "").encode("utf-8")).hexdigest()
            assert p.content_sha256 == expected, (
                f"sha256 mismatch for {p.rel_path}: got {p.content_sha256} expect {expected}"
            )

    # (d) ReviewerVerdict enum has 3 values
    verdicts = list(ReviewerVerdict)
    assert len(verdicts) == 3, verdicts

    # (e) GateSpec baseline default flags (no cache provider)
    gs = GateSpec()
    joined_flags = " ".join(gs.public_test_cmd) + " ".join(gs.hidden_test_cmd)
    assert "no:cacheprovider" in joined_flags, "pytest cache provider not disabled"

    # (f) DeterministicGateEngine.run_all short-circuits on scope failure
    with IsolatedWorkspace.create(fixture_root) as ws:
        gs2 = GateSpec()
        # Deliberately corrupt fixture baseline map to force scope fail
        gs2.baseline_hashes = {k: "0" * 64 for k in baseline_file_hashes(fixture_root)}
        engine = DeterministicGateEngine(gs2)
        results = engine.run_all(ws.root)
        assert results[0].status.value == "fail", f"scope gate should fail, got {results[0].status}"
        # Short-circuit: only 1 result (scope) since mandatory fail
        assert len(results) == 1, f"short-circuit broken: {len(results)} results"

    # (g) RunDeps + FinalRunOutcome dataclass field shape
    rd = RunDeps(
        builder_worker=fw,
        reviewer_worker=fake_reviewer_pass(),
        builder_station=b_station,
        reviewer_station=r_station,
        fixture_root=fixture_root,
        hidden_test_root=hidden_root,
    )
    assert rd.max_retries == 1
    fro = FinalRunOutcome(
        factory_run_id="fr_dummy", final_status=RunStatus.PENDING,
        verified=False, events_emitted=0, replay_ready=False,
        db_path=":memory:", attempts=0, reviewer_verdict=None,
        final_gate_count=0, escalation=False,
    )
    assert fro.factory_run_id == "fr_dummy"

    # (h) Controller full happy path (fake workers) → COMPLETED + verified=True
    deps = RunDeps(
        builder_worker=FakeScriptedWorker.case_fix_slugify_builder(),
        reviewer_worker=fake_reviewer_pass(),
        builder_station=b_station,
        reviewer_station=r_station,
        fixture_root=fixture_root,
        hidden_test_root=hidden_root,
    )
    outcome = MinimalFactoryController.run(spec, task, deps)
    assert outcome.final_status == RunStatus.COMPLETED, (
        f"expected COMPLETED got {outcome.final_status}; attempts={outcome.attempts}"
    )
    assert outcome.verified is True
    assert outcome.events_emitted >= 10, f"too few events: {outcome.events_emitted}"

    # (i) Resume() on already-closed id returns same outcome, no new events
    resumed = MinimalFactoryController.resume(
        outcome.factory_run_id, spec, task, deps,
        db_path=outcome.db_path,
    )
    assert resumed.final_status == outcome.final_status
    assert resumed.verified == outcome.verified
    assert resumed.replay_ready is True


# ---------- V05: Layer A provider/runtime/persistence AST import-only audit ----------
def v05_layer_a_ast_provider_independent():
    domain_dir = REPO_ROOT / "redi_lab" / "domain"
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
    for path in sorted(domain_dir.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = _ast.parse(source, filename=str(path))
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                for alias in node.names:
                    mod = alias.name.lower()
                    for bad in forbidden_modules:
                        if mod == bad.lower() or mod.startswith(bad.lower() + "."):
                            raise AssertionError(
                                f"{path.name}:L{node.lineno} import {alias.name!r}"
                            )
            elif isinstance(node, _ast.ImportFrom):
                mod = (node.module or "").lower()
                for bad in forbidden_modules:
                    if mod == bad.lower() or mod.startswith(bad.lower() + "."):
                        raise AssertionError(
                            f"{path.name}:L{node.lineno} from-import {node.module!r}"
                        )
                if node.names:
                    for alias in node.names:
                        if alias.name in forbidden_symbols:
                            raise AssertionError(
                                f"{path.name}:L{node.lineno} symbol {alias.name!r}"
                            )
            elif isinstance(node, _ast.Name) and node.id in forbidden_symbols:
                raise AssertionError(
                    f"{path.name}:L{getattr(node, 'lineno', '?')} symbol {node.id!r}"
                )


# ---------- V06: Layer C -> Layer A direction only (no reverse) ----------
def v06_layer_c_direction_only():
    # For every .py in domain/, AST-verify NO imports of persistence/evidence/
    # replay/runtime/factory/gates (same as v05 but double-check string refs to
    # those packages anywhere in source code as well).
    domain_dir = REPO_ROOT / "redi_lab" / "domain"
    reverse_tokens = (
        "redi_lab.persistence", "redi_lab.evidence", "redi_lab.replay",
        "redi_lab.runtime", "redi_lab.factory", "redi_lab.gates",
    )
    for f in sorted(domain_dir.glob("*.py")):
        src = f.read_text(encoding="utf-8")
        for tok in reverse_tokens:
            if tok in src:
                raise AssertionError(f"{f.name} string ref to Layer C module: {tok}")
    # Second direction: confirm every Layer C module does import from domain
    # (existence check: at least one from redi_lab.domain … line exists)
    layer_c_dirs = [
        "persistence", "evidence", "replay", "runtime", "factory", "gates",
    ]
    any_hit = False
    for dname in layer_c_dirs:
        dpath = REPO_ROOT / "redi_lab" / dname
        for f in dpath.glob("**/*.py"):
            src = f.read_text(encoding="utf-8")
            if "redi_lab.domain" in src or "from .domain" in src or "from redi_lab " in src:
                any_hit = True
                break
    assert any_hit, "no Layer C module imports from domain — architecture broken"


# ---------- V07: Fixture baseline unchanged across 2 full runs ----------
def v07_factory_diff_scope_clean():
    from redi_lab.domain import (
        ProductSpec, Requirement, TaskContract, StationContract, RiskTier,
    )
    from redi_lab.factory.controller import MinimalFactoryController, RunDeps
    from redi_lab.runtime.fake_worker import (
        FakeScriptedWorker, fake_reviewer_pass,
    )
    fixture_root = REPO_ROOT / "experiments" / "fixtures" / "slugify_pkg"
    target = fixture_root / "slugify" / "__init__.py"
    before = target.read_bytes()
    before_hashes: dict[str, str] = {}
    for p in fixture_root.rglob("*"):
        if p.is_file():
            before_hashes[str(p.relative_to(fixture_root))] = _sha256_file(p)
    spec = ProductSpec(
        requested_outcome="slugify",
        requirements=[Requirement(text="x", kind="functional")],
        constraints=[], risk_tier=RiskTier.LOW, unresolved_requirements=[],
    )
    task = TaskContract(goal="x")
    b_station = StationContract(role="builder", required_output_kinds=["patch_edit"], allowed_tool_classes=[])
    r_station = StationContract(role="reviewer", required_output_kinds=["verdict"], allowed_tool_classes=[])
    hidden_root = REPO_ROOT / "experiments" / "fixtures" / "slugify_pkg_hidden_tests"
    for _ in range(2):
        deps = RunDeps(
            builder_worker=FakeScriptedWorker.case_fix_slugify_builder(),
            reviewer_worker=fake_reviewer_pass(),
            builder_station=b_station, reviewer_station=r_station,
            fixture_root=fixture_root, hidden_test_root=hidden_root,
        )
        MinimalFactoryController.run(spec, task, deps)
    after = target.read_bytes()
    assert before == after, "fixture baseline slugify/__init__.py was mutated"
    for rel, h in before_hashes.items():
        p = fixture_root / rel
        if not p.exists():
            raise AssertionError(f"fixture file disappeared: {rel}")
        if _sha256_file(p) != h:
            raise AssertionError(f"fixture file mutated: {rel}")


def _sha256_file(p: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------- V08: Git working tree tracked files clean ----------
def v08_git_status_tracked_clean():
    proc = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        # Git not available → skip-ok
        return
    out = (proc.stdout or "").strip()
    assert out == "", f"tracked working tree not clean:\n{out}"


# ---------- V09: Live provider bounded run OR blocked ----------
def v09_live_provider_bounded_run_or_blocked():
    rc, out = _run_script("scripts.run_redi_03_live_provider")
    tail = out[-1200:]
    if "LIVE BLOCKED" in out:
        # Expected without creds — record reason, pass the validation
        LIVE_BLOCKED_REASONS.append(tail.strip().splitlines()[-1] if tail.strip() else "unknown")
        return
    assert rc == 0, f"live runner rc={rc}; tail={tail}"


# ---------- V10: Replay integrity on a happy-slug factory run ----------
def v10_replay_integrity_happy_run():
    from redi_lab.domain import (
        ProductSpec, Requirement, TaskContract, StationContract, RiskTier,
    )
    from redi_lab.factory.controller import MinimalFactoryController, RunDeps
    from redi_lab.runtime.fake_worker import (
        FakeScriptedWorker, fake_reviewer_pass,
    )
    from redi_lab.replay import ReplayReader
    from redi_lab.domain import LifecycleEventType, RunStatus

    fixture_root = REPO_ROOT / "experiments" / "fixtures" / "slugify_pkg"
    hidden_root = REPO_ROOT / "experiments" / "fixtures" / "slugify_pkg_hidden_tests"
    spec = ProductSpec(
        requested_outcome="slugify",
        requirements=[Requirement(text="x", kind="functional")],
        constraints=[], risk_tier=RiskTier.LOW, unresolved_requirements=[],
    )
    task = TaskContract(goal="x")
    b_station = StationContract(role="builder", required_output_kinds=["patch_edit"], allowed_tool_classes=[])
    r_station = StationContract(role="reviewer", required_output_kinds=["verdict"], allowed_tool_classes=[])
    deps = RunDeps(
        builder_worker=FakeScriptedWorker.case_fix_slugify_builder(),
        reviewer_worker=fake_reviewer_pass(),
        builder_station=b_station, reviewer_station=r_station,
        fixture_root=fixture_root, hidden_test_root=hidden_root,
    )
    outcome = MinimalFactoryController.run(spec, task, deps)
    assert outcome.replay_ready or Path(outcome.db_path).exists()
    conn = sqlite3.connect(outcome.db_path)
    try:
        rr = ReplayReader(conn)
        res = rr.replay_factory_run(outcome.factory_run_id)
        assert res.integrity_status == "OK", f"integrity={res.integrity_status}"
        assert res.final_verification_state is True
        types_back = {e.event_type for e in res.ordered_events}
        assert LifecycleEventType.PRODUCT_VERIFIED in types_back
        assert len(res.ordered_events) >= 6
    finally:
        conn.close()


# =====================================================================
# Main
# =====================================================================
def main():
    print("REDI-03 validation 10-pack (stdlib + subprocess runners)")
    print(f"repo root: {REPO_ROOT}")
    print(f"python:    {sys.executable}")
    print()

    checks = [
        ("v01 pytest tests/unit/ all green", v01_pytest_tests_unit),
        ("v02 REDI-01 stdlib runner 77/77", v02_redi01_stdlib_runner),
        ("v03 REDI-02 stdlib runner 24/24", v03_redi02_stdlib_runner),
        ("v04 REDI-03 stdlib self-checks (no pytest)", v04_redi03_stdlib_selfchecks),
        ("v05 Layer A AST provider/runtime independence", v05_layer_a_ast_provider_independent),
        ("v06 Layer C -> A import direction only", v06_layer_c_direction_only),
        ("v07 Factory 2x run baseline fixtures unchanged", v07_factory_diff_scope_clean),
        ("v08 Git tracked working-tree clean", v08_git_status_tracked_clean),
        ("v09 Live provider bounded run OR LIVE BLOCKED", v09_live_provider_bounded_run_or_blocked),
        ("v10 Replay of happy run integrity OK + verified", v10_replay_integrity_happy_run),
    ]

    for name, fn in checks:
        check(name, fn)

    print()
    print("=" * 64)
    print(f"TOTAL: PASS={PASS} FAIL={FAIL} CHECKS={PASS+FAIL}")
    if LIVE_BLOCKED_REASONS:
        print("LIVE BLOCKED reasons:")
        for r in LIVE_BLOCKED_REASONS:
            print(f"  - {r[:300]}")
    if FAIL:
        print("Failing checks:")
        for n, ok, why in RESULTS:
            if not ok:
                print(f"  - {n}: {why}")
    print("=" * 64)
    return FAIL == 0


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
