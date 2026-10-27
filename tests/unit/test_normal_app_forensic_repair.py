"""Real failed bytes, complete handoffs, isolation, budget and durable diagnostics."""
import hashlib
import importlib
import json
import socket
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from redi_lab.qualification_executor import evaluate_arm, materialize
from redi_lab.workbench import normal_app as app
from redi_lab.workbench.normal_app_contracts import CONTRACT_ID, INTERFACES, handoff
from redi_lab.workbench.normal_app_fakes import SOURCES
from redi_lab.workbench.normal_app_gates import evaluate_normal_app

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "mini_crm_wb847df3c91090"
EXACT_REASON = "ModuleNotFoundError: No module named 'mini_crm.crm'"
CONTRACT = {"required_files": [t["artifact"] for t in app.TASKS],
            "extra_files": "forbidden", "missing_files": "forbidden", "action": "write"}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("external network/provider forbidden in forensic regression")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


def historical():
    return {name: (FIXTURE / name).read_text(encoding="utf-8") for name in INTERFACES}


def corrected_historical():
    """Controlled consumer uses OBSERVED retained dict-returning API, not v2 int API.

    No adapter/patch is applied to historical evidence or production output.
    This isolates why supplying the full upstream interface makes assembly possible.
    """
    patches = historical()
    patches["core.py"] = patches["core.py"].replace('            "" + where', '            """ + where')
    service = SOURCES["service.py"]
    service = service.replace("svc.create_client('Acme Ltd')", "svc.create_client('Acme Ltd')['id']")
    service = service.replace("svc.create_client('Probe Client')", "svc.create_client('Probe Client')['id']")
    service = service.replace("notes='kickoff')", "notes='kickoff')['id']")
    service = service.replace("notes='follow-up')", "notes='follow-up')['id']")
    patches["service.py"] = service
    return patches


def evaluate(patches, root, *, isolated=True, progress=None):
    retained = materialize(product_key=app.PACKAGE, arm="offline_forensic", model="injected", patches=patches)
    return (evaluate_normal_app if isolated else evaluate_arm)(
        retained, app.CRM_PRODUCT, CONTRACT, ws_root=root, progress=progress)


def test_exact_retained_failure_hashes_and_call_facts(tmp_path):
    evidence = json.loads((FIXTURE / "live_run_evidence.json").read_text())
    hashes = {e["task_id"]: e["detail"].split("=")[1] for e in evidence["events"]
              if e["event"] == "task_completed"}
    for task in app.TASKS:
        assert hashlib.sha256((FIXTURE / task["artifact"]).read_bytes()).hexdigest() == hashes[task["task_id"]]
    assert set(evidence["task_states"].values()) == {"COMPLETED"}
    assert evidence["call_facts"]["calls"] == 5
    assert {f["finish_reason"] for f in evidence["call_facts"]["per_task"].values()} == {"stop"}
    from decimal import Decimal
    assert sum(Decimal(f["cost_usd"]) for f in evidence["call_facts"]["per_task"].values()) == Decimal("0.135184")
    # Unmodified shared evaluator reproduces the EXACT historical gate/reason.
    old = evaluate(historical(), tmp_path / "old", isolated=False)
    assert old["reasons"] == {"public_verification": EXACT_REASON}
    assert old["gates"] == evidence["gates"]
    # New normal-app adapter preserves predicates, supplies missing import events.
    events = []
    new = evaluate(historical(), tmp_path / "isolated", progress=events.append)
    assert new["reasons"] == old["reasons"] and new["gates"] == old["gates"]
    assert new["gate_states"] == {"structured_output_schema": "PASS", "scope_security": "PASS",
        "public_verification": "FAIL", "private_verification": "NOT_RUN",
        "integration": "NOT_RUN", "reviewer": "NOT_RUN"}
    assert events[-2:] == ["gate_started:public_verification", "gate_finished:public_verification:fail"]


def test_corrected_consumer_of_retained_interfaces_passes_same_frozen_gates(tmp_path):
    patches = corrected_historical()
    assert all(patches[name] == historical()[name] for name in ("database.py", "dataflow.py", "ui.py"))
    assert evaluate(patches, tmp_path / "shared", isolated=False)["all_mandatory_gates_pass"]
    assert evaluate(patches, tmp_path / "isolated")["all_mandatory_gates_pass"]


def test_import_only_fix_exposes_retained_core_syntax_error(tmp_path):
    patches = historical()
    patches["service.py"] = patches["service.py"].replace("from .crm import", "from .core import")
    result = evaluate(patches, tmp_path / "import-only")
    assert result["reasons"]["public_verification"] == (
        "SyntaxError: unterminated string literal (detected at line 204) (core.py, line 204)")


def test_import_and_syntax_fix_still_fails_on_record_used_as_id(tmp_path):
    patches = historical()
    patches["service.py"] = patches["service.py"].replace("from .crm import", "from .core import")
    patches["core.py"] = corrected_historical()["core.py"]
    # Remove cleanup solely to expose the primary exception on Windows (open file
    # deletion otherwise masks it with PermissionError). Child exit releases handles.
    patches["service.py"] = patches["service.py"].replace("        _remove_database(database_path)", "        pass")
    result = evaluate(patches, tmp_path / "import-only")
    assert "Error binding parameter 1: type 'dict' is not supported" in result["reasons"]["public_verification"]


def test_missing_private_export_remains_a_gate_failure(tmp_path):
    patches = corrected_historical()
    patches["service.py"] = patches["service.py"].replace("from .dataflow import normalize_status\n", "")
    result = evaluate(patches, tmp_path / "missing-export")
    assert result["gate_states"]["public_verification"] == "PASS"
    assert result["gate_states"]["private_verification"] == "FAIL"
    assert "KeyError" in result["reasons"]["private_verification"]
    assert result["gate_states"]["integration"] == "NOT_RUN"


def test_every_consumer_receives_identical_contract_and_transitive_source():
    sources = historical()
    for task in app.TASKS:
        prompt = app.task_prompt(task["task_id"], sources)
        metadata = json.loads(prompt.split("(binding JSON)\n")[1].split("\n")[0])
        assert metadata["contract_id"] == CONTRACT_ID
        assert metadata["interfaces"] == INTERFACES
        assert metadata["acceptance"] == json.loads(json.dumps({
            "public_checks": app.PUBLIC_CHECKS, "private_checks": app.PRIVATE_CHECKS,
            "integration_probe": app.INTEGRATION_PROBE}))
        if task["task_id"] == "integration_assembly":
            assert {m["artifact"] for m in metadata["upstream_artifacts"]} == set(INTERFACES) - {"service.py"}
            for name, source in sources.items():
                if name != "service.py":
                    assert source in prompt
            core = next(m for m in metadata["upstream_artifacts"] if m["artifact"] == "core.py")
            assert "unterminated string literal" in core["parse_error"]
            repaired_metadata = handoff({"core.py": corrected_historical()["core.py"]})[0]
            assert repaired_metadata["symbols"]["CRMService.create_client"]["returns"] == "Dict[str, Any]"


@pytest.mark.parametrize("bad", [
    [{"rel_path": "database.py", "action": "write", "content": "x=1"}] * 2,
    [{"rel_path": "../database.py", "action": "write", "content": "x=1"}],
    [{"rel_path": "database.py", "action": "delete", "content": "x=1"}],
    [{"rel_path": "database.py", "action": "write", "content": "from .invented import x"}],
    [{"rel_path": "database.py", "action": "write", "content": "def broken(:"}],
])
def test_ownership_and_import_errors_fail_closed(tmp_path, bad):
    providers = app.canonical_fake_providers()
    providers["build_database"] = lambda _: json.dumps({"patches": bad})
    runner = app.NormalAppMultiStationRunner(fake_providers=providers, ws_root=tmp_path / "gates")
    result = runner.execute()
    assert result["task_states"]["build_database"] == "FAILED"
    assert result["task_states"]["build_core"] == "PENDING"
    assert not result["verified"]


def test_corrected_factory_system_lifecycle_persistence_ui(tmp_path):
    runner = app.NormalAppMultiStationRunner(fake_providers=app.canonical_fake_providers(), ws_root=tmp_path / "gates")
    result = runner.execute()
    assert result["verified"] and set(result["gate_states"].values()) == {"PASS"}
    sys.path.insert(0, str(tmp_path / "gates"))
    try:
        module = importlib.import_module("mini_crm.service")
        database = str(tmp_path / "persistence.sqlite")
        service = module.build_crm(database)
        client = service.create_client("Client <&>")
        job = service.create_job(client, "Title", notes="Note <&>")
        assert type(client) is int and type(job) is int
        with pytest.raises(ValueError):
            service.set_status(job, "DONE")
        service.set_status(job, " active ")
        service.close()
        service.close()
        reopened = module.build_crm(database)
        assert reopened.jobs() == [{"id": job, "client_id": client, "client_name": "Client <&>",
                                     "title": "Title", "status": "ACTIVE", "notes": "Note <&>"}]
        reopened.close()
        html = module.run_ui(database)
        from redi_lab.workbench.product_integrity import validate_forms
        validate_forms(html, {"clients_total": 1, "jobs_total": 1, "NEW": 0, "ACTIVE": 1, "DONE": 0})
        assert "Client &lt;&amp;&gt;" in html and "Note &lt;&amp;&gt;" in html
        for _ in range(2):
            assert module.run_acceptance() == app.EXPECTED_ACCEPTANCE
            assert module.integration_probe() == {"crm": "ready", "clients": 1, "jobs": 1}
        # SQLite connection was closed before temporary database deletion.
        Path(database).unlink()
    finally:
        sys.path.remove(str(tmp_path / "gates"))
        for name in list(sys.modules):
            if name == "mini_crm" or name.startswith("mini_crm."):
                sys.modules.pop(name)


def test_concurrent_builds_and_parent_module_cannot_contaminate_gates(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "mini_crm.service", SimpleNamespace(run_acceptance=lambda: app.EXPECTED_ACCEPTANCE))
    with ThreadPoolExecutor(max_workers=2) as pool:
        failed = pool.submit(evaluate, historical(), tmp_path / "bad")
        passed = pool.submit(evaluate, dict(SOURCES), tmp_path / "good")
        assert failed.result()["reasons"] == {"public_verification": EXACT_REASON}
        assert passed.result()["all_mandatory_gates_pass"]
    with pytest.raises(RuntimeError, match="must be empty"):
        evaluate(dict(SOURCES), tmp_path / "bad")


def test_parallel_budget_reservation_counts_inflight_call():
    started, release = threading.Event(), threading.Event()
    class Completions:
        def create(self, **kwargs):
            started.set()
            assert release.wait(5)
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=100, completion_tokens=100,
                prompt_tokens_details=None), choices=[SimpleNamespace(finish_reason="stop",
                message=SimpleNamespace(content='{}'))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    call, facts = app.build_live_call("test-key", "0.15", client_factory=lambda: client)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(call, "build_database", "small prompt")
        assert started.wait(5)
        try:
            with pytest.raises(RuntimeError, match="ceiling"):
                call("build_dataflow", "small prompt")
            assert facts["calls"] == 1
        finally:
            release.set()
        first.result()
    with pytest.raises(RuntimeError, match="repeated"):
        call("build_database", "small prompt")


def test_large_handoff_fails_before_provider_dispatch():
    class Completions:
        def create(self, **kwargs):
            raise AssertionError("must not dispatch")
    call, facts = app.build_live_call("test-key", "1", client_factory=lambda:
        SimpleNamespace(chat=SimpleNamespace(completions=Completions())))
    with pytest.raises(RuntimeError, match="input budget"):
        call("build_database", "x" * app.MAX_INPUT_TOKENS)
    assert facts["calls"] == 0


def test_durable_success_and_partial_failure_evidence(tmp_path, monkeypatch):
    from redi_lab.workbench import engine
    from tests.unit.test_workbench_crm_live import _fake_crm_client
    monkeypatch.setattr(engine, "WORKBENCH_ROOT", tmp_path)
    fake_key = "test-secret-never-persisted"
    for broken in (False, True):
        def factory():
            client = _fake_crm_client([])
            original = client.chat.completions.create
            def create(**kwargs):
                if broken and "Implement ONLY core.py" in kwargs["messages"][1]["content"]:
                    raise RuntimeError("provider error " + fake_key)
                return original(**kwargs)
            client.chat.completions.create = create
            return client
        adapter = engine.WorkbenchAdapter()
        started = adapter.start_build("Build a Mini CRM", backend=app.BACKEND_ID, ceiling="1",
                                      key=fake_key, live_flag="1", client_factory=factory)
        adapter._builds[started["build_id"]]._thread.join(30)
        path = tmp_path / ("wb-" + started["build_id"]) / "live_run_evidence.json"
        raw = path.read_text(encoding="utf-8")
        evidence = json.loads(raw)
        assert fake_key not in raw
        assert evidence["manifest"]["interface_contract_id"] == CONTRACT_ID
        assert len(evidence["artifacts"]) == (2 if broken else 5)
        for artifact in evidence["artifacts"]:
            assert hashlib.sha256(artifact["content"].encode()).hexdigest() == artifact["stored_sha256"]
        assert len(evidence["task_inputs"]) == (3 if broken else 5)
        assert len(evidence["task_outputs"]) == (2 if broken else 5)
        assert "normal_app_contracts.py" in evidence["source_hashes"]
        assert set(evidence["gate_states"].values()) == ({"NOT_RUN"} if broken else {"PASS"})
        assert evidence["status"] == ("FAILED" if broken else "VERIFIED")


def test_failed_gate_and_task_personas_are_terminal():
    from redi_lab.workbench.crew import crew_snapshot
    result = crew_snapshot({"status": "FAILED", "live_events": [
        "run_started", "planning_started", "scope_resolved",
        "component_task_started:database", "component_task_failed:database",
        "component_task_started:service", "component_task_failed:service",
        "gate_started:public_verification", "gate_finished:public_verification:fail"]})
    states = {row["persona"]["id"]: row["state"] for row in result["crew"]}
    assert states["andinus"] == states["gentius"] == states["bato"] == "FAILED"
    assert states["redon"] == states["medaurus"] == "WAITING"
    assert result["active_count"] == 0


def test_gate_process_failure_is_terminal_with_exact_stage_reason(tmp_path, monkeypatch):
    from redi_lab.workbench import normal_app_gates
    def failed(*args, **kwargs):
        kwargs["progress"]("gate_started:public_verification")
        raise RuntimeError("controlled evaluator crash")
    monkeypatch.setattr(normal_app_gates, "evaluate_normal_app", failed)
    runner = app.NormalAppMultiStationRunner(fake_providers=app.canonical_fake_providers(), ws_root=tmp_path)
    result = runner.execute()
    assert not result["verified"]
    assert result["gate_states"]["public_verification"] == "FAIL"
    assert result["gate_reasons"] == {"public_verification": "RuntimeError: controlled evaluator crash"}
    assert result["gate_states"]["private_verification"] == "NOT_RUN"


def test_evidence_write_failure_cannot_report_ready(tmp_path, monkeypatch):
    from redi_lab.workbench import engine
    from tests.unit.test_workbench_crm_live import _fake_crm_client
    monkeypatch.setattr(engine, "WORKBENCH_ROOT", tmp_path)
    original_open = Path.open
    def guarded_open(path, *args, **kwargs):
        if path.name == "live_run_evidence.json" and args and args[0] == "x":
            raise OSError("controlled disk failure")
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guarded_open)
    adapter = engine.WorkbenchAdapter()
    started = adapter.start_build("Build a Mini CRM", backend=app.BACKEND_ID, ceiling="1",
                                  key="offline", live_flag="1", client_factory=lambda: _fake_crm_client([]))
    build = adapter._builds[started["build_id"]]
    build._thread.join(30)
    snap = build.snapshot()
    assert snap["verdict"] == "APP FAILED"
    assert "evidence persistence failed" in snap["error"]
