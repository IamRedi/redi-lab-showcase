"""Station-contract enforcement and a real generated UI -> HTTP -> SQLite path."""
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

import pytest

from redi_lab.qualification_executor import materialize
from redi_lab.workbench import normal_app as app
from redi_lab.workbench.normal_app_fakes import SOURCES
from redi_lab.workbench.normal_app_gates import evaluate_normal_app
from redi_lab.workbench.product_integrity import IntegrityError, check_product, validate_forms

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/mini_crm_wb67e4bed07ea3"


def test_latest_unchanged_retained_failure(tmp_path):
    evidence = json.loads((FIXTURE / "live_run_evidence.json").read_text())
    patches = {p.name: p.read_text(encoding="utf-8") for p in FIXTURE.glob("*.py")}
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in FIXTURE.iterdir()}
    for task in app.TASKS:
        event = next(e for e in evidence["events"] if e["event"] == "task_completed" and e["task_id"] == task["task_id"])
        assert event["detail"] == "sha256=" + hashes[task["artifact"]]
    with pytest.raises(SyntaxError): ast.parse(patches["core.py"])
    retained = materialize(product_key=app.PACKAGE, arm="offline_replay", model="retained", patches=patches)
    result = evaluate_normal_app(retained, app.CRM_PRODUCT,
        {"required_files": list(patches)}, ws_root=tmp_path / "replay")
    assert result["gates"] == evidence["gates"]
    assert result["reasons"] == {"public_verification": "ModuleNotFoundError: No module named 'mini_crm.crm'"}
    assert hashes == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in FIXTURE.iterdir()}


@pytest.mark.parametrize("name,old,new,reason", [
    ("database.py", "    conn.row_factory = sqlite3.Row", "    pass", "sqlite3.Row"),
    ("database.py", "    conn.execute('PRAGMA foreign_keys = ON')", "    pass", "foreign keys"),
    ("database.py", "def connect(path: str)", "def connect(other: str)", "path"),
    ("dataflow.py", "old == new or new in TRANSITIONS[old]", "True", "transition semantics"),
    ("core.py", "return cur.lastrowid", "return {'id': cur.lastrowid}", "int ID"),
    ("core.py", "from .database import", "from .missing import", "undeclared local import"),
    ("core.py", "    def add_note", "    def absent_note", "add_note"),
    ("core.py", "if row is None or not can_transition(row['status'], status):", "if row is None:", "reverse transition"),
    ("core.py", "        return dashboard_counts(self.conn)", "        return {'jobs': 0}", "empty"),
    ("core.py", "        self.conn = connect(path)", "        self.conn = connect(':memory:')", "persistence"),
    ("core.py", "            self.conn.close()", "            pass", "PermissionError"),
    ("ui.py", 'method="post" action="/clients"', 'method="get" action="/clients"', "POST form"),
    ("ui.py", 'name="client_id"', 'name="client"', "POST form"),
    ("ui.py", "escape(str(job[key]))", "str(job[key])", "escape"),
    ("service.py", "from .dataflow import normalize_status", "", "normalize_status"),
    ("service.py", "def run_acceptance()", "def run_acceptance(required)", "required"),
])
def test_contract_mutations_fail_early(tmp_path, name, old, new, reason):
    sources = dict(SOURCES)
    assert old in sources[name]
    sources[name] = sources[name].replace(old, new)
    with pytest.raises((IntegrityError, ValueError), match=reason):
        check_product(sources, stage="mutation", root=tmp_path)


def test_failed_station_never_completes_or_enters_gates(tmp_path):
    providers = app.canonical_fake_providers()
    sources = dict(SOURCES)
    sources["core.py"] = sources["core.py"].replace("return cur.lastrowid", "return {'id': cur.lastrowid}")
    providers["build_core"] = lambda _: json.dumps({"patches": [{"rel_path": "core.py", "action": "write", "content": sources["core.py"]}]})
    result = app.NormalAppMultiStationRunner(fake_providers=providers, ws_root=tmp_path / "gates").execute()
    assert result["task_states"]["build_core"] == "FAILED"
    assert result["task_states"]["build_ui"] == "PENDING"
    assert set(result["gate_states"].values()) == {"NOT_RUN"}
    assert "int ID" in result["integrity"]["build_core"]["reason"]
    assert not any(e["event"] == "component_task_completed:core" for e in result["events"])


def test_fresh_product_reaches_same_frozen_gates(tmp_path):
    result = app.NormalAppMultiStationRunner(fake_providers=app.canonical_fake_providers(), ws_root=tmp_path / "gates").execute()
    assert result["verified"], result
    assert len(result["integrity"]) == 6
    assert all(r["status"] == "PASS" for r in result["integrity"].values())
    assert set(result["gate_states"].values()) == {"PASS"}


def test_generated_forms_reach_real_backend_and_survive_host_restart(tmp_path):
    package = tmp_path / "mini_crm"; package.mkdir()
    for name, source in SOURCES.items(): (package / name).write_text(source, encoding="utf-8")
    database = tmp_path / "product.sqlite"
    def start():
        proc = subprocess.Popen([sys.executable, "-B", "-m", "redi_lab.workbench.product_host",
            "--project", str(tmp_path), "--database", str(database), "--port", "0"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        line = proc.stdout.readline()
        assert line, proc.stderr.read()
        return proc, json.loads(line)["url"].rstrip("/")
    def stop(proc):
        proc.terminate(); proc.wait(timeout=10); proc.stdout.close(); proc.stderr.close()
    def get(url):
        with urllib.request.urlopen(url) as r: return r.read().decode()
    def post(route, values):
        request = urllib.request.Request(base + route, data=urllib.parse.urlencode(values).encode())
        with urllib.request.urlopen(request) as r: return r.read().decode()
    proc, base = start()
    try:
        forms = validate_forms(get(base))
        assert {f["action"] for f in forms} == {"/clients", "/jobs", "/status", "/notes"}
        assert "succeeded: 1" in post("/clients", {"name": "Persisted client"})
        post("/jobs", {"client_id": 1, "title": "Real job", "notes": "First note"})
        post("/status", {"job_id": 1, "status": "ACTIVE"})
        post("/notes", {"job_id": 1, "note": "Second note"})
        with pytest.raises(urllib.error.HTTPError) as bad:
            post("/status", {"job_id": 1, "status": "NEW"})
        assert bad.value.code == 400
        state = json.loads(get(base + "/api/state"))
        assert state["dashboard"] == {"jobs_total": 1, "clients_total": 1, "NEW": 0, "ACTIVE": 1, "DONE": 0}
        assert "Second note" in get(base)
    finally: stop(proc)
    proc, base = start()
    try:
        assert json.loads(get(base + "/api/state")) == state
        assert "Real job" in get(base) and "Second note" in get(base)
    finally: stop(proc)
