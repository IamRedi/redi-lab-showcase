"""Exact latest retained failure; stale-runtime preflight and elapsed-time regression."""
import ast
import hashlib
import json
import socket
import threading
import urllib.request
from decimal import Decimal
from pathlib import Path

import pytest

from redi_lab.qualification_executor import evaluate_arm, materialize
from redi_lab.workbench import engine, runtime_identity
from redi_lab.workbench import normal_app as app
from redi_lab.workbench.normal_app_fakes import SOURCES
from redi_lab.workbench.normal_app_gates import evaluate_normal_app

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "mini_crm_wbc1564339281f"
REASON = "ModuleNotFoundError: No module named 'mini_crm.crm'"
CONTRACT = {"required_files": [t["artifact"] for t in app.TASKS], "action": "write",
            "extra_files": "forbidden", "missing_files": "forbidden"}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    original = socket.socket.connect
    def connect(sock, address):
        if not isinstance(address, tuple) or address[0] not in ("127.0.0.1", "::1"):
            raise AssertionError("external connections forbidden")
        return original(sock, address)
    monkeypatch.setattr(socket.socket, "connect", connect)


def historical():
    return {t["artifact"]: (FIXTURE / t["artifact"]).read_text(encoding="utf-8") for t in app.TASKS}


def corrected_consumer():
    patches = historical()
    service = SOURCES["service.py"]
    # Controlled assembly uses the actual annotated record-returning upstream API.
    for call in ("svc.create_client('Acme Ltd')", "svc.create_client('Probe Client')",
                 "svc.create_job(cid, 'First job', notes='kickoff')",
                 "svc.create_job(cid, 'Second job', notes='follow-up')"):
        service = service.replace(call, call + "['id']")
    patches["service.py"] = service
    return patches


def evaluate(patches, root, *, isolated=False):
    retained = materialize(product_key=app.PACKAGE, arm="latest_offline_replay",
                           model="injected", patches=patches)
    return (evaluate_normal_app if isolated else evaluate_arm)(
        retained, app.CRM_PRODUCT, CONTRACT, ws_root=root)


def test_exact_latest_failed_bytes_and_facts(tmp_path):
    evidence = json.loads((FIXTURE / "live_run_evidence.json").read_text())
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in FIXTURE.iterdir() if p.is_file()}
    for task in app.TASKS:
        event = next(e for e in evidence["events"] if e["event"] == "task_completed" and e["task_id"] == task["task_id"])
        assert event["detail"] == "sha256=" + before[task["artifact"]]
        ast.parse(historical()[task["artifact"]])  # Unlike the earlier build, ALL modules parse.
    assert set(evidence["task_states"].values()) == {"COMPLETED"}
    assert evidence["call_facts"]["calls"] == 5
    assert sum(Decimal(f["cost_usd"]) for f in evidence["call_facts"]["per_task"].values()) == Decimal("0.137448")
    assert all(f["finish_reason"] == "stop" for f in evidence["call_facts"]["per_task"].values())
    result = evaluate(historical(), tmp_path / "shared")
    assert result["gates"] == evidence["gates"]
    assert result["reasons"] == {"public_verification": REASON}
    isolated = evaluate(historical(), tmp_path / "isolated", isolated=True)
    assert isolated["reasons"] == result["reasons"]
    assert isolated["gate_states"]["private_verification"] == "NOT_RUN"
    assert before == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in FIXTURE.iterdir() if p.is_file()}


def test_current_validation_cannot_mark_this_assembly_completed(tmp_path):
    patches = historical()
    providers = {task["task_id"]: (lambda prompt, name=task["artifact"]: json.dumps({"patches": [
        {"rel_path": name, "action": "write", "content": patches[name]}]})) for task in app.TASKS}
    result = app.NormalAppMultiStationRunner(fake_providers=providers, ws_root=tmp_path).execute()
    # Executable producer contracts now reject this legacy output earlier still.
    assert result["task_states"]["build_database"] == "FAILED"
    assert result["task_states"]["integration_assembly"] == "PENDING"
    from redi_lab.workbench.normal_app_contracts import validate_local_imports
    with pytest.raises(ValueError, match="undeclared local import: .crm"):
        validate_local_imports(patches["service.py"])
    assert set(result["gate_states"].values()) == {"NOT_RUN"}
    assert any(e["event"] == "scope_resolved" for e in result["events"])


def test_actual_producer_compatible_assembly_passes_same_frozen_gates(tmp_path):
    patches = corrected_consumer()
    assert all(patches[n] == historical()[n] for n in patches if n != "service.py")
    assert evaluate(patches, tmp_path / "shared")["all_mandatory_gates_pass"]
    assert evaluate(patches, tmp_path / "isolated", isolated=True)["all_mandatory_gates_pass"]


def test_import_fix_alone_exposes_id_contract_error(tmp_path):
    patches = historical()
    patches["service.py"] = patches["service.py"].replace("from .crm import", "from .core import")
    # Replace temporary-directory context only in this diagnostic copy to avoid
    # Windows cleanup masking the primary exception. Child exit closes handles.
    patches["service.py"] = patches["service.py"].replace("import tempfile", "import tempfile\nfrom contextlib import nullcontext")
    patches["service.py"] = patches["service.py"].replace(
        'tempfile.TemporaryDirectory(prefix="mini_crm_acceptance_")', 'nullcontext(tempfile.mkdtemp())')
    result = evaluate(patches, tmp_path / "id-error", isolated=True)
    assert result["reasons"]["public_verification"] == "run_acceptance[] raised ValueError: client_id must be a positive integer"


def test_missing_private_export_is_independent_failure(tmp_path):
    patches = corrected_consumer()
    patches["service.py"] = patches["service.py"].replace("from .dataflow import normalize_status\n", "")
    result = evaluate(patches, tmp_path / "private", isolated=True)
    assert result["gates"]["public_verification"]
    assert "KeyError" in result["reasons"]["private_verification"]


def test_runtime_identity_detects_changed_or_missing_source(tmp_path):
    path = tmp_path / "component.py"
    path.write_text("original")
    identity = runtime_identity.RuntimeIdentity({"component.py": path})
    original_hash = identity.status()["loaded_source_hashes"]["component.py"]
    assert identity.require_current()["fresh"]
    path.write_text("changed")
    with pytest.raises(runtime_identity.StaleRuntimeError, match="restart Workbench"):
        identity.require_current()
    assert identity.status()["loaded_source_hashes"]["component.py"] == original_hash
    assert identity.status()["disk_source_hashes"]["component.py"] != original_hash
    path.unlink()
    assert not identity.status()["fresh"]


def test_stale_engine_blocks_before_client_creation_and_persists_reason(tmp_path, monkeypatch):
    path = tmp_path / "engine.py"
    path.write_text("old source")
    identity = runtime_identity.RuntimeIdentity({"engine.py": path})
    path.write_text("updated source")
    monkeypatch.setattr(runtime_identity, "NORMAL_APP_RUNTIME", identity)
    monkeypatch.setattr(engine, "WORKBENCH_ROOT", tmp_path / "builds")
    def forbidden_factory():
        raise AssertionError("client must not be constructed")
    adapter = engine.WorkbenchAdapter()
    started = adapter.start_build("Build a Mini CRM", backend=app.BACKEND_ID, ceiling="1",
                                  key="offline", live_flag="1", client_factory=forbidden_factory)
    build = adapter._builds[started["build_id"]]
    build._thread.join(10)
    snap = build.snapshot()
    assert snap["status"] == "BLOCKED" and snap["provider_calls"] == 0
    assert "restart Workbench" in snap["detail"]
    evidence = json.loads((engine.WORKBENCH_ROOT / ("wb-" + started["build_id"]) / "live_run_evidence.json").read_text())
    assert evidence["runtime_identity"]["changed_files"] == ["engine.py"]
    assert evidence["call_facts"]["calls"] == 0
    assert set(evidence["gate_states"].values()) == {"NOT_RUN"}
    assert evidence["status"] == "BLOCKED"


def test_source_change_between_calls_blocks_next_dispatch(tmp_path, monkeypatch):
    from tests.unit.test_workbench_crm_live import _fake_crm_client
    path = tmp_path / "normal_app.py"
    path.write_text("old source")
    identity = runtime_identity.RuntimeIdentity({"normal_app.py": path})
    monkeypatch.setattr(runtime_identity, "NORMAL_APP_RUNTIME", identity)
    calls = []
    call, facts = app.build_live_call("offline", "1", client_factory=lambda: _fake_crm_client(calls))
    call("build_database", app.task_prompt("build_database", {}))
    path.write_text("changed source")
    with pytest.raises(runtime_identity.StaleRuntimeError):
        call("build_dataflow", app.task_prompt("build_dataflow", {}))
    assert facts["calls"] == len(calls) == 1


def test_source_change_during_gates_cannot_report_ready(tmp_path, monkeypatch):
    from redi_lab.workbench import normal_app_gates
    path = tmp_path / "gate.py"
    path.write_text("original")
    identity = runtime_identity.RuntimeIdentity({"gate.py": path})
    monkeypatch.setattr(runtime_identity, "NORMAL_APP_RUNTIME", identity)
    original = normal_app_gates.evaluate_normal_app
    def changed_after_gates(*args, **kwargs):
        result = original(*args, **kwargs)
        path.write_text("changed")
        return result
    monkeypatch.setattr(normal_app_gates, "evaluate_normal_app", changed_after_gates)
    runner = app.NormalAppMultiStationRunner(fake_providers=app.canonical_fake_providers(), ws_root=tmp_path / "gates")
    result = runner.execute()
    assert not result["verified"] and result["status"] == "FAILED"
    assert "restart Workbench" in result["gate_reasons"]["gate_executor"]


def test_server_reports_runtime_identity_read_only():
    from redi_lab.workbench.server import serve
    server, url = serve(port=0)
    try:
        with urllib.request.urlopen(url + "api/server") as response:
            state = json.load(response)
        assert state["normal_app_runtime"]["fresh"]
        assert state["normal_app_runtime"]["loaded_source_hashes"] == runtime_identity.normal_app_runtime_status()["loaded_source_hashes"]
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("status", ["FAILED", "VERIFIED", "BLOCKED", "RUNNING"])
def test_normal_app_elapsed_time_uses_build_clock(status, monkeypatch):
    build = engine.WorkbenchBuild.__new__(engine.WorkbenchBuild)
    build.__dict__.update(build_id="clock-test", prompt="CRM", backend=app.BACKEND_ID,
        ceiling="1", status=status, started_at=1000.0,
        finished_at=None if status == "RUNNING" else 1083.6885135,
        live={"status": status, "verified": status == "VERIFIED", "elapsed_seconds": 0},
        live_events=[], error=None, key=None)
    monkeypatch.setattr(engine.time, "time", lambda: 1083.6885135)
    snap = build.snapshot()
    assert snap["elapsed_seconds"] == 83.689


def test_fresh_generic_pipeline_preserves_gates_budget_and_durable_identity(tmp_path, monkeypatch):
    from tests.unit.test_workbench_crm_live import _fake_crm_client
    monkeypatch.setattr(engine, "WORKBENCH_ROOT", tmp_path)
    calls = []
    adapter = engine.WorkbenchAdapter()
    started = adapter.start_build("Build a Mini CRM", backend=app.BACKEND_ID, ceiling="1",
                                  key="offline", live_flag="1", client_factory=lambda: _fake_crm_client(calls))
    build = adapter._builds[started["build_id"]]
    build._thread.join(30)
    snap = build.snapshot()
    assert snap["verdict"] == "APP READY", snap
    assert set(snap["gate_states"].values()) == {"PASS"}
    assert len(calls) == 5 and Decimal(snap["cost_usd"]) < Decimal("1")
    assert snap["elapsed_seconds"] > 0
    evidence = json.loads((tmp_path / ("wb-" + started["build_id"]) / "live_run_evidence.json").read_text())
    assert evidence["runtime_identity"]["fresh"]
    assert evidence["source_hashes"] == evidence["runtime_identity"]["loaded_source_hashes"]
    assert evidence["elapsed_seconds"] > 0
