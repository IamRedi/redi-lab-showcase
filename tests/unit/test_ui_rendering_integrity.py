"""Exact UI failure, safe generation contract and Teuta completion ordering."""
import hashlib
import json
from pathlib import Path
import threading

import pytest

from redi_lab.workbench import normal_app as app, product_integrity as integrity
from redi_lab.workbench.normal_app_contracts import UI_RENDERING_POLICY
from redi_lab.workbench.normal_app_fakes import SOURCES
from redi_lab.workbench.crew import crew_snapshot

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/mini_crm_wbceab8c955c8b"


def module(source):
    ns = {}; exec(compile(source, "controlled-ui.py", "exec"), ns); return ns


def test_exact_unchanged_ui_reproduces_formatting_failure():
    before = (FIXTURE / "ui.py").read_bytes()
    provenance = json.loads((FIXTURE / "provenance.json").read_text())
    assert hashlib.sha256(before).hexdigest() == provenance["ui_sha256"]
    with pytest.raises(KeyError) as error:
        integrity.smoke_ui(module(before)["render_dashboard"])
    assert str(error.value) == "'font-family'"
    assert (FIXTURE / "ui.py").read_bytes() == before


@pytest.mark.parametrize("source", [
    "x = '<style>body{color:red}</style>{name}'.format(name='A')",
    "x = '<style>body{color:red}</style>'.format_map({})",
    "x = f'<p>{name}</p>'",
    "x = '<p>%s</p>' % name",
    "x = Template('$name').safe_substitute(name='A')",
])
def test_ui_template_operations_rejected_before_execution(source):
    with pytest.raises(integrity.IntegrityError, match="safe-rendering contract"):
        integrity.validate_ui_source(source)


def test_safe_composition_handles_literal_css_and_template_looking_data(tmp_path):
    assert "body{font-family:" in SOURCES["ui.py"]
    integrity.validate_ui_source(SOURCES["ui.py"])
    checks = integrity.smoke_ui(module(SOURCES["ui.py"])["render_dashboard"])
    assert "ui_literal_braces" in checks and "ui_dashboard_placeholders" in checks
    report = integrity.check_product(dict(SOURCES), stage="product", root=tmp_path)
    assert report["status"] == "PASS" and "ui_render_populated" in report["checks"]


@pytest.mark.parametrize("old,new,reason", [
    ("str(dashboard[key])", "'{unfilled}'", "count mismatch"),
    ("str(dashboard[key])", "'0'", "count mismatch"),
    ('action="/clients"', 'action="/missing"', "undeclared POST"),
    ('action="/clients"', 'action="https://example.invalid/clients"', "external"),
    ("<button>Create client", '<button formaction="/missing">Create client', "transport overrides"),
    ('name="client_id" type="number"', 'name="client_id" disabled type="number"', "POST form"),
    ("escape(str(job[key]))", "str(job[key])", "escape"),
])
def test_dynamic_ui_smoke_rejects_unresolved_counts_or_transport_drift(old,new,reason):
    assert old in SOURCES["ui.py"]
    render = module(SOURCES["ui.py"].replace(old,new))["render_dashboard"]
    with pytest.raises(integrity.IntegrityError, match=reason): integrity.smoke_ui(render)


def test_safe_rule_is_in_worker_prompt_before_generation():
    prompt = app.task_prompt("build_ui", {})
    assert UI_RENDERING_POLICY["id"] in prompt
    assert "Do not use .format()" in prompt and "data-count" in prompt


def persona(events, status="RUNNING"):
    return next(c for c in crew_snapshot({"status": status, "live_events": events,
                "component_map": {"agron": "core", "teuta": "ui", "bindus": "dataflow"}})["crew"]
                if c["persona"]["id"] == "teuta")["state"]


def test_teuta_stays_working_until_render_smoke_passes(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = integrity.check_product
    def controlled(*args, **kwargs):
        if kwargs["stage"] == "build_ui":
            entered.set(); assert release.wait(20)
        return original(*args, **kwargs)
    monkeypatch.setattr(integrity, "check_product", controlled)
    events = []
    runner = app.NormalAppMultiStationRunner(fake_providers=app.canonical_fake_providers(),
        ws_root=tmp_path / "gates", progress=events.append)
    results = []
    thread = threading.Thread(target=lambda: results.append(runner.execute())); thread.start()
    try:
        assert entered.wait(20)
        assert runner.task_states["build_ui"] == "RUNNING"
        assert persona(events) == "WORKING"
        assert "component_task_completed:ui" not in events
    finally: release.set(); thread.join(30)
    assert results[0]["verified"]
    assert persona(events, "VERIFIED") == "VERIFIED"
    rows = results[0]["events"]
    smoke_pass = next(i for i,e in enumerate(rows) if e["event"] == "integrity_passed" and e.get("task_id") == "build_ui")
    completed = next(i for i,e in enumerate(rows) if e["event"] == "component_task_completed:ui")
    assert smoke_pass < completed
    assert set(results[0]["gate_states"].values()) == {"PASS"}


def test_render_exception_fails_teuta_and_blocks_assembly(tmp_path):
    providers = app.canonical_fake_providers()
    broken = SOURCES["ui.py"].replace("    counts =", "    raise KeyError('unfilled_dynamic_value')\n    counts =")
    providers["build_ui"] = lambda _: json.dumps({"patches": [{"rel_path": "ui.py", "action": "write", "content": broken}]})
    events = []
    runner = app.NormalAppMultiStationRunner(fake_providers=providers, ws_root=tmp_path / "gates", progress=events.append)
    result = runner.execute()
    assert "unfilled_dynamic_value" in result["failures"]["build_ui"]
    assert result["task_states"]["integration_assembly"] == "PENDING"
    assert persona(events, "FAILED") == "FAILED"
    assert "component_task_completed:ui" not in events
    assert set(result["gate_states"].values()) == {"NOT_RUN"}
