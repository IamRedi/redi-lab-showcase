"""Mini CRM live-backend bridge tests — offline; fake client through the live boundary."""

import json
from pathlib import Path

import pytest

from redi_lab.workbench.adapter import WorkbenchAdapter
from redi_lab.workbench.engine import BACKENDS
from redi_lab.workbench.normal_app import (
    PACKAGE,
    build_execution_manifest,
    build_live_call,
    live_cost_estimate,
    task_prompt,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("CRM live bridge tests must not touch network")

    import socket

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def test_backend_registered_live():
    entry = BACKENDS["openai-live-multistation-normal-app-experimental"]
    assert entry["active"] == "true" and "Mini CRM" in entry["label"]


def test_live_cost_estimate_five_calls():
    est = live_cost_estimate()
    assert est["provider_calls"] == 5
    # pinned at MAX_COMPLETION_TOKENS=8192 since the reasoning-cap fix
    # (wb-1ac58eb0bb0d failed closed at 2048: finish_reason="length")
    assert est["hard_cost_bound_usd"] == "0.9415200"
    assert est["expected_cost_usd"] == "0.0512000"   # usage-based, cap-independent
    assert est["recommended_owner_ceiling_usd"] == "1.00"


def test_task_prompt_states_scope_and_json():
    manifest = build_execution_manifest()
    artifacts: dict = {}
    prompts = []
    for task in manifest["tasks"]:
        prompt = task_prompt(task["task_id"], artifacts)
        prompts.append(prompt)
        # simulate execution: later tasks see earlier retained artifacts
        from redi_lab.workbench.normal_app import TASK_SCOPE_LINES

        artifacts[TASK_SCOPE_LINES[task["task_id"]][1]] = "# stub"
    for prompt, task in zip(prompts, manifest["tasks"]):
        assert "JSON" in prompt
        assert "TASK SCOPE (binding)" in prompt
        persona, artifact, _ = TASK_SCOPE_LINES[task["task_id"]]
        assert artifact in prompt
    # later tasks see earlier retained artifacts
    assert "RETAINED BUILDER ARTIFACTS" in prompts[2]  # core sees database+dataflow
    assert "RETAINED BUILDER ARTIFACTS" in prompts[4]  # assembly sees all


def _fake_crm_client(calls: list):
    """Fake provider client through the SAME governed live boundary."""

    class _Msg:
        def __init__(self, content):
            self.content = content

    class _Choice:
        finish_reason = "stop"

        def __init__(self, content):
            self.message = _Msg(content)

    class _Usage:
        prompt_tokens = 320
        completion_tokens = 300
        prompt_tokens_details = None

    class _Resp:
        usage = _Usage()

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            user = kwargs["messages"][1]["content"]
            from redi_lab.workbench.normal_app import canonical_fake_providers

            providers = canonical_fake_providers()
            for task_id, provider in providers.items():
                marker = {
                    "build_database": "database.py",
                    "build_dataflow": "dataflow.py",
                    "build_core": "core.py",
                    "build_ui": "ui.py",
                    "integration_assembly": "service.py",
                }[task_id]
                if f"Implement ONLY {marker}" in user:
                    return type("R", (), {"choices": [type(
                        "C", (), {"finish_reason": "stop",
                                  "message": type("M", (), {"content": provider("")})()})()],
                        "usage": type("U", (), {"prompt_tokens": 320, "completion_tokens": 300,
                                                "prompt_tokens_details": None})()})()
            raise AssertionError("no canned response matched the prompt")

    class _Client:
        chat = type("Chat", (), {"completions": _Completions()})()

    return _Client()


def test_live_mode_end_to_end_app_ready(tmp_path, monkeypatch):
    import os

    monkeypatch.setenv("REDI_OPENAI_API_KEY", "k" * 30)
    monkeypatch.setenv("REDI_LIVE_PROVIDER", "1")
    calls: list = []
    adapter = WorkbenchAdapter()
    owner_prompt = ("Build a Mini CRM / Job Manager app with clients, jobs linked to clients, "
                    "statuses NEW/ACTIVE/DONE, notes, dashboard counts, SQLite persistence "
                    "and a local runnable UI.")
    started = adapter.start_build(
        owner_prompt, backend="openai-live-multistation-normal-app-experimental",
        ceiling="1.00", key=os.environ["REDI_OPENAI_API_KEY"],
        live_flag="1", client_factory=lambda: _fake_crm_client(calls))
    build = adapter._builds[started["build_id"]]
    build._thread.join(timeout=60)
    snap = adapter.snapshot(started["build_id"])
    assert snap["status"] == "VERIFIED" and snap["verdict"] == "APP READY"
    assert snap["provider_calls"] == 5
    assert snap["active_worker"].startswith("gpt-5.6-terra")
    assert all(snap["gates"].values())
    by = {c["persona"]["id"]: c for c in snap["virtutes"]["crew"]}
    assert by["teuta"]["state"] == "VERIFIED"   # real frontend artifact
    assert by["andinus"]["state"] == "VERIFIED"
    assert snap["cost_usd"] and float(snap["cost_usd"]) > 0
    assert Path(snap["project_path"]).exists()


def test_missing_live_env_blocks_before_any_call(tmp_path, monkeypatch):
    monkeypatch.delenv("REDI_OPENAI_API_KEY", raising=False)
    adapter = WorkbenchAdapter()
    missing_budget = adapter.start_build("Build a Mini CRM",
                                         backend="openai-live-multistation-normal-app-experimental")
    assert "overall product ceiling" in missing_budget["error"]
    started = adapter.start_build("Build a Mini CRM",
                                  backend="openai-live-multistation-normal-app-experimental", ceiling="1")
    build = adapter._builds[started["build_id"]]
    build._thread.join(timeout=30)
    snap = adapter.snapshot(started["build_id"])
    assert snap["status"] == "BLOCKED"                        # fail-before-provider-call
    assert snap["missing"] == ["REDI_OPENAI_API_KEY", "REDI_LIVE_PROVIDER=1"]
    assert "fail-before-provider-call" in snap["detail"]
    assert snap["verdict"] != "APP READY"


def test_cheap_ceiling_blocks_mid_run_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("REDI_LIVE_PROVIDER", "1")
    runner_live_call, _ = build_live_call("k" * 30, "0.01")  # client unused offline

    # simulate ceiling exhaustion at the runner level with a stub real_call
    from redi_lab.workbench.normal_app import NormalAppMultiStationRunner

    calls = {"n": 0}

    def real_call(task_id, prompt):
        calls["n"] += 1
        from redi_lab.workbench.normal_app import canonical_fake_providers

        content = json.loads(canonical_fake_providers()[task_id](""))["patches"][0]["content"]
        return json.dumps({"patches": [{"rel_path": {
            "build_database": "database.py", "build_dataflow": "dataflow.py",
            "build_core": "core.py", "build_ui": "ui.py",
            "integration_assembly": "service.py"}[task_id],
            "action": "write", "content": content}]})

    # ceiling far below one terra task bound: first projection must fail-closed
    runner = NormalAppMultiStationRunner(
        fake_providers={}, ws_root=tmp_path / "gates",
        real_call=real_call, owner_ceiling="0.000001")
    result = runner.execute()
    assert result["status"] == "FAILED"
    assert result["task_states"]["build_database"] == "FAILED"
    assert result["task_states"]["build_core"] == "PENDING"
    assert "owner budget ceiling exceeded" in result["failures"]["build_database"]


def test_build_core_provider_failure_persisted_offline(tmp_path, monkeypatch):
    """Reproduce the observed wb-550ed4061319 / 88d8ddf91b8f failure shape
    OFFLINE (zero network): database+dataflow succeed through the governed
    live boundary, the build_core provider call raises a provider-level
    error, the run fails closed at core — and the provider exception is now
    RETAINED (snapshot + live_run_evidence.json) with exception class,
    provider status/code, sanitized message, task/persona, timestamp and
    provider/model, never the API key."""
    import os
    import time as _time

    from redi_lab.workbench import engine as engine_module

    monkeypatch.setattr(engine_module, "WORKBENCH_ROOT", tmp_path)
    fake_key = "sk-test" + "a" * 40
    monkeypatch.setenv("REDI_OPENAI_API_KEY", fake_key)
    monkeypatch.setenv("REDI_LIVE_PROVIDER", "1")

    class _ProviderDown(Exception):
        status_code = 503
        body = {"error": {"code": "provider_overloaded"}}

    key_holder: dict = {}

    def factory():
        client = _fake_crm_client([])
        orig = client.chat.completions.create

        def create(**kwargs):
            user = kwargs["messages"][1]["content"]
            if "Implement ONLY core.py" in user:
                exc = _ProviderDown(
                    f"upstream provider rejected the request for key {key_holder['key']}")
                raise exc
            return orig(**kwargs)

        client.chat.completions.create = create
        return client

    adapter = WorkbenchAdapter()
    started = adapter.start_build(
        "Build a Mini CRM", backend="openai-live-multistation-normal-app-experimental",
        ceiling="1.00", key=fake_key, live_flag="1", client_factory=factory)
    key_holder["key"] = fake_key
    build = adapter._builds[started["build_id"]]
    build._thread.join(timeout=60)
    snap = adapter.snapshot(started["build_id"])

    # same shape as both real live builds: 3 provider calls, FAILED at core
    assert snap["status"] == "FAILED"
    assert snap["provider_calls"] == 3
    failure = snap["call_facts"]["failures"]["build_core"]
    assert failure["exception_class"] == "_ProviderDown"
    assert failure["provider_status"] == 503
    assert failure["provider_code"] == "provider_overloaded"
    assert failure["persona"] == "agron"
    assert failure["provider"] == "openai" and failure["model"] == "gpt-5.6-terra"
    assert failure["task_id"] == "build_core" and 0 < failure["ts"] <= _time.time() + 1
    assert fake_key not in failure["message"] and "***redacted***" in failure["message"]
    assert set(snap["call_facts"]["per_task"]) == {"build_database", "build_dataflow"}
    assert snap["failures"]["build_core"].startswith("_ProviderDown:")

    # forward-only disk persistence survives the process
    evidence = json.loads(
        (tmp_path / f"wb-{started['build_id']}" / "live_run_evidence.json").read_text())
    assert evidence["backend"] == "openai-live-multistation-normal-app-experimental"
    assert evidence["provider"] == "openai" and evidence["model"] == "gpt-5.6-terra"
    assert evidence["call_facts"]["failures"]["build_core"]["provider_status"] == 503
    assert evidence["task_states"]["build_core"] == "FAILED"
    assert evidence["task_states"]["build_ui"] == "PENDING"          # fail-closed downstream
    assert evidence["task_states"]["integration_assembly"] == "PENDING"
    assert evidence["failures"]["build_core"].startswith("_ProviderDown:")
    assert any(e["event"] == "task_failed" and e.get("task_id") == "build_core"
               and e.get("detail") for e in evidence["events"])
    assert fake_key not in json.dumps(evidence)   # never persist the key


def test_process_env_alone_satisfies_preflight_and_reaches_fake_provider(tmp_path, monkeypatch):
    """Diagnostic for the false-BLOCKED incident: with ONLY the process
    environment set — no key/live_flag in the request, exactly the real UI
    request shape — the normal-app preflight must see BOTH values and
    dispatch to the injected fake provider instead of returning BLOCKED."""
    import os

    from redi_lab.workbench import engine as engine_module

    monkeypatch.setenv("REDI_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("REDI_LIVE_PROVIDER", "1")
    monkeypatch.setattr(engine_module, "WORKBENCH_ROOT", tmp_path)
    calls: list = []
    adapter = WorkbenchAdapter()
    started = adapter.start_build(
        "Build a Mini CRM", backend="openai-live-multistation-normal-app-experimental",
        ceiling="1.00", client_factory=lambda: _fake_crm_client(calls))
    build = adapter._builds[started["build_id"]]
    build._thread.join(timeout=60)
    snap = adapter.snapshot(started["build_id"])
    assert snap["status"] == "VERIFIED" and snap["verdict"] == "APP READY"
    assert snap["provider_calls"] == 5
    assert len(calls) == 5


def _length_capped_client(calls: list, cap_that_works: int):
    """Fake provider that reproduces the wb-1ac58eb0bb0d shape: when the call
    is made with a completion cap below cap_that_works, the reasoning budget
    consumes everything -> finish_reason='length', output == cap, EMPTY
    content (JSONDecodeError at char 0 in the runner). At/above the cap the
    real artifact content is returned."""
    from redi_lab.workbench.normal_app import canonical_fake_providers

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            user = kwargs["messages"][1]["content"]
            cap = kwargs["max_completion_tokens"]
            providers = canonical_fake_providers()
            for artifact, task_id in {
                "database.py": "build_database", "dataflow.py": "build_dataflow",
                "core.py": "build_core", "ui.py": "build_ui",
                "service.py": "integration_assembly"}.items():
                if f"Implement ONLY {artifact}" in user:
                    content = json.loads(providers[task_id](""))["patches"][0]["content"]
                    payload = json.dumps({"patches": [
                        {"rel_path": artifact, "action": "write", "content": content}]})
                    length_capped = cap < cap_that_works and artifact == "core.py"
                    resp = type("R", (), {})()
                    resp.choices = [type("C", (), {})()]
                    resp.choices[0].finish_reason = "length" if length_capped else "stop"
                    resp.choices[0].message = type("M", (), {})()
                    resp.choices[0].message.content = "" if length_capped else payload
                    resp.usage = type("U", (), {})()
                    resp.usage.prompt_tokens = 1011 if artifact == "core.py" else 320
                    resp.usage.completion_tokens = cap if length_capped else 300
                    resp.usage.prompt_tokens_details = None
                    return resp
            raise AssertionError("no canned response matched the prompt")

    class _Client:
        def __init__(self):
            self.chat = type("Chat", (), {})()
            self.chat.completions = _Completions()

    return _Client()


def test_reproduce_wb_1ac58eb0bb0d_length_cap_failure(tmp_path, monkeypatch):
    """Replay the persisted evidence shape OFFLINE: build_core's provider call
    returns finish_reason='length' with EMPTY content (2048-token cap consumed
    by reasoning) -> run fails closed at core, ui/assembly PENDING, and the
    evidence file retains finish_reason 'length' + the JSONDecodeError."""
    import os

    from redi_lab.workbench import engine as engine_module

    monkeypatch.setattr(engine_module, "WORKBENCH_ROOT", tmp_path)
    calls: list = []
    adapter = WorkbenchAdapter()
    started = adapter.start_build(
        "Build a Mini CRM", backend="openai-live-multistation-normal-app-experimental",
        ceiling="1.00", key="k" * 30, live_flag="1",
        # cap_that_works unreachable -> core.py is length-capped regardless of
        # the shipped cap, reproducing the persisted old-config failure shape
        client_factory=lambda: _length_capped_client(calls, cap_that_works=10**9))
    build = adapter._builds[started["build_id"]]
    build._thread.join(timeout=60)
    snap = adapter.snapshot(started["build_id"])
    # exactly the persisted shape of wb-1ac58eb0bb0d
    assert snap["status"] == "FAILED"
    assert snap["provider_calls"] == 3
    assert snap["call_facts"]["per_task"]["build_core"]["finish_reason"] == "length"
    assert snap["failures"]["build_core"].startswith("JSONDecodeError:")
    evidence = json.loads(
        (tmp_path / f"wb-{started['build_id']}" / "live_run_evidence.json").read_text())
    assert evidence["task_states"] == {
        "build_database": "COMPLETED", "build_dataflow": "COMPLETED",
        "build_core": "FAILED", "build_ui": "PENDING",
        "integration_assembly": "PENDING"}
    assert evidence["call_facts"]["failures"] == {}   # provider call itself did not raise


def test_raised_completion_cap_lets_build_core_succeed(tmp_path, monkeypatch):
    """Forward fix: with the shipped 8192-token cap the same builder (which
    only produces core.py content when given >= 8192) completes all five
    tasks and the run VERIFIES."""
    import os

    from redi_lab.workbench import engine as engine_module
    from redi_lab.workbench.normal_app import MAX_COMPLETION_TOKENS

    assert MAX_COMPLETION_TOKENS >= 8192
    monkeypatch.setattr(engine_module, "WORKBENCH_ROOT", tmp_path)
    calls: list = []
    adapter = WorkbenchAdapter()
    started = adapter.start_build(
        "Build a Mini CRM", backend="openai-live-multistation-normal-app-experimental",
        ceiling="1.00", key="k" * 30, live_flag="1",
        client_factory=lambda: _length_capped_client(calls, cap_that_works=8192))
    build = adapter._builds[started["build_id"]]
    build._thread.join(timeout=60)
    snap = adapter.snapshot(started["build_id"])
    assert snap["status"] == "VERIFIED" and snap["verdict"] == "APP READY"
    assert all(c["max_completion_tokens"] == MAX_COMPLETION_TOKENS for c in calls)
    assert len(calls) == 5


def test_live_running_snapshot_activates_virtutes_from_real_events(tmp_path, monkeypatch):
    """While a governed live run executes, the snapshot must carry the real
    in-flight events so VIRTUTES activate (the 0/11-WAITING defect): bindus
    shows WORKING while her provider call is still blocked, andinus activates
    from real artifact retention, zojz orchestrates, and gate personas stay
    WAITING (no fake activity). Final states transition on real outcomes."""
    import threading

    from redi_lab.workbench import engine as engine_module

    monkeypatch.setattr(engine_module, "WORKBENCH_ROOT", tmp_path)
    dataflow_blocked = threading.Event()
    dataflow_call_started = threading.Event()

    def factory():
        client = _fake_crm_client([])
        orig = client.chat.completions.create

        def create(**kwargs):
            if "Implement ONLY dataflow.py" in kwargs["messages"][1]["content"]:
                dataflow_call_started.set()
                dataflow_blocked.wait(timeout=30)
            return orig(**kwargs)

        client.chat.completions.create = create
        return client

    adapter = WorkbenchAdapter()
    started = adapter.start_build(
        "Build a Mini CRM", backend="openai-live-multistation-normal-app-experimental",
        ceiling="1.00", key="k" * 30, live_flag="1", client_factory=factory)
    assert dataflow_call_started.wait(timeout=30)
    dataflow_started_snapshot = None
    andinus_retained = None
    for _ in range(200):
        snap = adapter.snapshot(started["build_id"])
        crew = {c["persona"]["id"]: c for c in snap["virtutes"]["crew"]}
        if dataflow_started_snapshot is None and crew["bindus"]["state"] == "WORKING":
            dataflow_started_snapshot = (snap, crew)
        if dataflow_started_snapshot and crew["andinus"]["state"] in ("WORKING", "VERIFIED"):
            andinus_retained = crew["andinus"]
            break
        threading.Event().wait(0.02)
    assert dataflow_started_snapshot, "bindus never activated while her call ran"
    snap, crew = dataflow_started_snapshot
    assert snap["virtutes"]["active_count"] > 0          # the 0/11 defect is gone
    assert crew["zojz"]["state"] == "WORKING"
    assert crew["bindus"]["current_task"] == "dataflow"  # real component fragment
    # no fake activity: personas whose stages have no events stay WAITING
    assert crew["bato"]["state"] == "WAITING"
    assert crew["medaurus"]["state"] == "WAITING"
    assert crew["monunius"]["state"] == "WAITING"
    assert crew["teuta"]["state"] == "WAITING"
    assert andinus_retained, "andinus never activated from real retention"
    dataflow_blocked.set()
    build = adapter._builds[started["build_id"]]
    build._thread.join(timeout=60)
    final = adapter.snapshot(started["build_id"])
    final_crew = {c["persona"]["id"]: c for c in final["virtutes"]["crew"]}
    assert final["status"] == "VERIFIED" and final["verdict"] == "APP READY"
    assert final_crew["bindus"]["state"] == "VERIFIED"   # completed transition
    assert final_crew["agron"]["state"] == "VERIFIED"    # build_core executed
    assert final_crew["teuta"]["state"] == "VERIFIED"
    assert final["virtutes"]["active_count"] == 0        # nothing active after finish




FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "mini_crm_wb3840d7b30163"


def _fixture_patches() -> dict:
    return {name: (FIXTURE_DIR / name).read_text(encoding="utf-8")
            for name in ("database.py", "dataflow.py", "core.py", "ui.py", "service.py")}


def test_replay_wb3840d7b30163_real_artifacts_fail_public_gate(tmp_path):
    """Deterministic offline replay of the final live failure using the REAL
    retained provider artifacts (fixture sha256 matches the evidence events):
    the public gate fails on run_acceptance raising AttributeError ('int'
    object has no attribute 'id') — exactly the recorded gate_reason — and
    fail-fast stops private/integration/reviewer."""
    from redi_lab.qualification_executor import evaluate_arm, materialize
    from redi_lab.workbench.normal_app import CRM_PRODUCT, build_execution_manifest

    retained = materialize(product_key=PACKAGE, arm="wb3840d7b30163_replay",
                           model="gpt-5.6-terra", patches=_fixture_patches())
    manifest = build_execution_manifest()
    contract = {"required_files": [t["artifact"] for t in manifest["tasks"]],
                "extra_files": "forbidden", "missing_files": "forbidden",
                "action": "write"}
    evaluation = evaluate_arm(retained, dict(CRM_PRODUCT), contract,
                              ws_root=tmp_path / "gates")
    assert evaluation["gates"]["structured_output_schema"] is True
    assert evaluation["gates"]["scope_security"] is True
    assert evaluation["gates"]["public_verification"] is False
    assert evaluation["reasons"]["public_verification"] == (
        "run_acceptance[] raised AttributeError: 'int' object has no attribute 'id'")
    assert evaluation["gates"]["private_verification"] is False   # fail-fast, never ran
    assert evaluation["all_mandatory_gates_pass"] is False


def test_real_artifacts_with_contract_conformant_assembly_pass_all_gates(tmp_path):
    """Proof of the prompt-contract fix: the SAME real generated
    database/dataflow/core/ui (from the failed live build) combined with a
    service.py that follows the NOW-BOUND acceptance contracts passes ALL
    gates — the provider output was exactly one binding contract short."""
    from redi_lab.qualification_executor import evaluate_arm, materialize
    from redi_lab.workbench.normal_app import CRM_PRODUCT, build_execution_manifest
    patches = _fixture_patches()
    # apply the NOW-BOUND dashboard key contract to the real generated
    # database.py (mechanical rename: what a build_database run under the
    # bound prompt produces); the original keys were never specified to the
    # builder and are the third hidden contract this failure exposed
    old_counts = '''    return {
        "clients": int(row["clients"]),
        "jobs": int(row["jobs"]),
        "new": int(row["new_count"]),
        "active": int(row["active_count"]),
        "done": int(row["done_count"]),
        "notes": int(row["notes"]),
    }'''
    new_counts = '''    return {
        "clients_total": int(row["clients"]),
        "jobs_total": int(row["jobs"]),
        "NEW": int(row["new_count"]),
        "ACTIVE": int(row["active_count"]),
        "DONE": int(row["done_count"]),
    }'''
    assert old_counts in patches["database.py"]
    patches["database.py"] = patches["database.py"].replace(old_counts, new_counts, 1)
    patches["service.py"] = (
        '"""Integration assembly bound to the retained module APIs."""\n'
        "\n"
        "from pathlib import Path\n"
        "import tempfile\n"
        "\n"
        "from .core import CRMService\n"
        "from .dataflow import normalize_status\n"
        "from .ui import render_dashboard\n"
        "\n"
        "import tempfile\n"
        "\n"
        "def build_crm():\n"
        "    return CRMService(Path(tempfile.mkdtemp(prefix='mini-crm-')) / 'crm.sqlite3')\n"
        "\n"
        "def run_acceptance():\n"
        "    svc = build_crm()\n"
        "    cid = svc.create_client('Acme Ltd')\n"
        "    j1 = svc.create_job(cid, 'First job', notes='kickoff')\n"
        "    svc.set_status(j1, 'ACTIVE')\n"
        "    j2 = svc.create_job(cid, 'Second job', notes='follow-up')\n"
        "    svc.set_status(j2, 'ACTIVE')\n"
        "    svc.set_status(j2, 'DONE')\n"
        "    return svc.dashboard()\n"
        "\n"
        "def integration_probe():\n"
        "    svc = build_crm()\n"
        "    cid = svc.create_client('Probe Client')\n"
        "    svc.create_job(cid, 'Probe job', notes='probe')\n"
        "    d = svc.dashboard()\n"
        "    return {'crm': 'ready', 'clients': d['clients_total'], 'jobs': d['jobs_total']}\n"
        "\n"
        "def run_ui():\n"
        "    svc = build_crm()\n"
        "    return render_dashboard(svc.dashboard(), svc.jobs())\n")
    retained = materialize(product_key=PACKAGE, arm="wb3840_contract_fix",
                           model="gpt-5.6-terra", patches=patches)
    manifest = build_execution_manifest()
    contract = {"required_files": [t["artifact"] for t in manifest["tasks"]],
                "extra_files": "forbidden", "missing_files": "forbidden",
                "action": "write"}
    evaluation = evaluate_arm(retained, dict(CRM_PRODUCT), contract,
                              ws_root=tmp_path / "gates")
    assert evaluation["gates"]["public_verification"] is True
    assert evaluation["gates"]["private_verification"] is True
    assert evaluation["gates"]["integration"] is True
    assert evaluation["gates"]["reviewer"] is True
    assert evaluation["all_mandatory_gates_pass"] is True


def test_task_prompts_now_bind_the_gate_contracts():
    """The three contracts enforced by the gates but previously hidden from
    the builders are now in the binding TASK SCOPE of the relevant prompts."""
    from redi_lab.workbench.normal_app import task_prompt

    db_prompt = task_prompt("build_database", {})
    assert "jobs_total, NEW, ACTIVE, DONE, clients_total" in db_prompt
    assembly_prompt = task_prompt("integration_assembly", {"database.py": "x", "dataflow.py": "x"})
    assert "shape-guessing adapters" in assembly_prompt
    assert "{'jobs_total': 2, 'NEW': 0, 'ACTIVE': 1, 'DONE': 1, 'clients_total': 1}" in assembly_prompt
    assert "{'crm': 'ready', 'clients': 1, 'jobs': 1}" in assembly_prompt
    assert "int ids" in assembly_prompt
