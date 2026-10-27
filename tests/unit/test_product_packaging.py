"""Delivery boundary tests; loopback only, generated fake providers, no live calls."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from redi_lab.workbench import product_packaging as delivery
from redi_lab.workbench import engine
from redi_lab.workbench.normal_app import canonical_fake_providers


@pytest.fixture
def retained(tmp_path):
    root = tmp_path / "wb-abcdef012345"
    package = root / "gates" / "mini_crm"
    package.mkdir(parents=True)
    artifacts = []
    for provider in canonical_fake_providers().values():
        patch = json.loads(provider(""))["patches"][0]
        content = patch["content"]
        (package / patch["rel_path"]).write_bytes(content.encode())
        sha = hashlib.sha256(content.encode()).hexdigest()
        artifacts.append({"rel_path": patch["rel_path"], "content": content,
                          "sha256": sha, "stored_sha256": sha})
    evidence = {"build_id": root.name[3:], "backend": "openai-live-multistation-normal-app-experimental",
                "status": "VERIFIED", "artifacts": artifacts,
                "events": [{"event": "component_task_completed:ui", "ts": 1},
                           {"event": "gate_finished:reviewer:pass", "ts": 2}],
                "gates": dict.fromkeys(delivery.GATES, True), "gate_states": dict.fromkeys(delivery.GATES, "PASS")}
    delivery.write_json(root / "live_run_evidence.json", evidence)
    return root


def hashes(root):
    return {str(p.relative_to(root)): delivery.digest(p.read_bytes()) for p in root.rglob("*") if p.is_file()}


def test_delivery_runs_real_forms_and_restart_without_changing_evidence(retained, monkeypatch):
    before = hashes(retained)
    result = delivery.package_verified_build(retained)
    assert result["status"] == "PASS"
    assert "persistence" in result["smoke"]["checks"]
    assert not Path(result["database_path"]).exists()
    assert all(hashes(retained)[name] == sha for name, sha in before.items())
    for source in (retained / "gates" / "mini_crm").glob("*.py"):
        assert source.read_bytes() == (retained / "app" / "mini_crm" / source.name).read_bytes()
    assert not (retained / "app" / "requirements.txt").exists()
    assert delivery.package_verified_build(retained) == result  # idempotent, no second smoke
    monkeypatch.setattr(engine, "WORKBENCH_ROOT", retained.parent)
    adapter = engine.WorkbenchAdapter()
    snap = adapter.snapshot(retained.name[3:])
    assert snap["verdict"] == "APP READY" and snap["can_open_app"]
    assert next(c for c in snap["virtutes"]["crew"] if c["persona"]["id"] == "teuta")["state"] == "VERIFIED"
    assert snap["project_path"] == str(retained / "app")
    info = adapter.app_action(retained.name[3:], "open")
    try:
        assert adapter.app_action(retained.name[3:], "open") == info
        assert adapter.snapshot(retained.name[3:])["local_url"] == info["url"]
        state = json.loads(delivery.local_request(info["url"] + "api/state"))
        assert state["dashboard"]["jobs_total"] == state["dashboard"]["clients_total"] == 0
        delivery.local_request(info["url"] + "clients", {"name": "Owner client"})
    finally: adapter.app_action(retained.name[3:], "stop")
    info = adapter.app_action(retained.name[3:], "open")
    try:
        assert json.loads(delivery.local_request(info["url"] + "api/state"))["dashboard"]["clients_total"] == 1
    finally: adapter.app_action(retained.name[3:], "stop")


@pytest.mark.parametrize("gate", delivery.GATES)
def test_any_gate_not_passed_prevents_publication(retained, gate):
    evidence = delivery.read_json(retained / "live_run_evidence.json")
    evidence["gate_states"][gate] = "NOT_RUN"
    delivery.write_json(retained / "live_run_evidence.json", evidence)
    with pytest.raises(ValueError, match="all six"):
        delivery.package_verified_build(retained)
    assert not (retained / "app").exists()


def test_changed_verified_source_is_not_packaged(retained):
    (retained / "gates" / "mini_crm" / "core.py").write_text("# modified")
    with pytest.raises(ValueError, match="hash mismatch"):
        delivery.package_verified_build(retained)
    assert not (retained / "app").exists()


def test_failed_smoke_withholds_ready_and_launch(retained, monkeypatch):
    contract = delivery.crm_delivery_contract()
    contract["smoke"]["forms"][2][1]["status"] = "INVALID"
    with pytest.raises(ValueError, match="packaging smoke failed"):
        delivery.package_verified_build(retained, contract)
    assert delivery.read_json(retained / "packaging_evidence.json")["status"] == "FAIL"
    monkeypatch.setattr(engine, "WORKBENCH_ROOT", retained.parent)
    adapter = engine.WorkbenchAdapter()
    assert adapter.snapshot(retained.name[3:])["verdict"] == "APP FAILED"
    with pytest.raises(ValueError, match="APP READY"):
        adapter.app_action(retained.name[3:], "open")


def test_changed_delivered_code_withholds_ready_and_launch(retained, monkeypatch):
    delivery.package_verified_build(retained)
    (retained / "app" / "mini_crm" / "ui.py").write_text("raise Exception('changed')")
    monkeypatch.setattr(engine, "WORKBENCH_ROOT", retained.parent)
    assert engine.WorkbenchAdapter().snapshot(retained.name[3:])["can_open_app"] is False
    with pytest.raises(ValueError, match="artifact changed"):
        delivery.open_app(retained)


def test_startup_failure_withholds_ready(retained, monkeypatch):
    contract = delivery.crm_delivery_contract()
    contract["factory"] = "absent_entrypoint"
    with pytest.raises(ValueError, match="app startup failed"):
        delivery.package_verified_build(retained, contract)
    monkeypatch.setattr(engine, "WORKBENCH_ROOT", retained.parent)
    assert engine.WorkbenchAdapter().snapshot(retained.name[3:])["verdict"] == "APP FAILED"


def test_extra_unverified_module_cannot_enter_package(retained):
    delivery.package_verified_build(retained)
    (retained / "app" / "mini_crm" / "__init__.py").write_text("# unverified")
    with pytest.raises(ValueError, match="inventory changed"):
        delivery.open_app(retained)


def test_missing_package_is_not_ready_or_launchable(retained, monkeypatch):
    monkeypatch.setattr(engine, "WORKBENCH_ROOT", retained.parent)
    adapter = engine.WorkbenchAdapter()
    assert adapter.snapshot(retained.name[3:])["verdict"] == "APP FAILED"
    with pytest.raises(ValueError): adapter.app_action("../abcdef012345", "open")


def test_package_transport_is_descriptor_driven(retained):
    contract = delivery.crm_delivery_contract()
    contract["module"] = "other_product.service"
    (retained / "gates" / "mini_crm").rename(retained / "gates" / "other_product")
    result = delivery.package_verified_build(retained, contract)
    assert result["status"] == "PASS"
    assert (retained / "app" / "other_product" / "service.py").exists()


def test_app_process_does_not_inherit_credentials(monkeypatch):
    monkeypatch.setenv("REDI_OPENAI_API_KEY", "test-secret")
    monkeypatch.setenv("OTHER_TOKEN", "test-secret")
    monkeypatch.setenv("REDI_LIVE_PROVIDER", "1")
    env = delivery.child_environment()
    assert "REDI_OPENAI_API_KEY" not in env and "OTHER_TOKEN" not in env
    assert env["REDI_LIVE_PROVIDER"] == "0"


def test_launch_route_requires_same_origin_json(retained, monkeypatch):
    from redi_lab.workbench.server import serve
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    monkeypatch.setattr(engine, "WORKBENCH_ROOT", retained.parent)
    server, url = serve(port=0)
    try:
        request = Request(url + "api/builds/abcdef012345/open", data=b"{}",
                          headers={"Content-Type": "application/json", "Origin": "https://untrusted.example"})
        with pytest.raises(HTTPError) as exc: urlopen(request)
        assert exc.value.code == 403
    finally: server.shutdown(); server.server_close()


def test_retained_build_packaging_cli(retained):
    result = subprocess.run([sys.executable, "-B", "-m", "redi_lab.workbench.product_packaging", str(retained)],
                            env=delivery.child_environment(), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["smoke"]["status"] == "PASS"


def test_workbench_withholds_ready_until_delivery_completes(tmp_path, monkeypatch):
    import importlib.util
    import threading
    spec = importlib.util.spec_from_file_location("crm_fake", Path(__file__).with_name("test_workbench_crm_live.py"))
    fake = importlib.util.module_from_spec(spec); spec.loader.exec_module(fake)
    entered, release = threading.Event(), threading.Event()
    def failing_delivery(_root):
        entered.set()
        assert release.wait(30)
        raise ValueError("startup smoke rejected")
    monkeypatch.setattr(delivery, "package_verified_build", failing_delivery)
    monkeypatch.setattr(engine, "WORKBENCH_ROOT", tmp_path)
    adapter = engine.WorkbenchAdapter()
    started = adapter.start_build("Mini CRM", backend="openai-live-multistation-normal-app-experimental",
                                  ceiling="1.00", key="k" * 30, live_flag="1",
                                  client_factory=lambda: fake._fake_crm_client([]))
    build = adapter._builds[started["build_id"]]
    try:
        assert entered.wait(30)
        snap = adapter.snapshot(started["build_id"])
        assert set(snap["gate_states"].values()) == {"PASS"}
        assert snap["status"] == "RUNNING" and snap["verdict"] is None
        assert not snap.get("can_open_app")
    finally:
        release.set(); build._thread.join(30)
    snap = adapter.snapshot(started["build_id"])
    assert snap["status"] == "FAILED" and snap["verdict"] == "APP FAILED"
    assert "startup smoke rejected" in snap["error"]
