"""Deliver verified local web products without changing the gate workspace.

The transport and packaging are descriptor driven. Product-specific routes and
acceptance workflows belong to the registered product contract, not the host.
No provider client is used here. Smoke records use an isolated database.
"""
from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import urlencode, urlsplit
from urllib.request import build_opener, ProxyHandler, Request

from .product_host import verify_package

GATES = ("structured_output_schema", "scope_security", "public_verification",
         "private_verification", "integration", "reviewer")
_LOCK = threading.RLock()
_PROCESSES: dict[str, subprocess.Popen] = {}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def crm_delivery_contract():
    from .normal_app_contracts import WEB_CONTRACT, CONTRACT_ID
    return {"contract_id": CONTRACT_ID, "module": "mini_crm.service",
            "factory": "build_crm", "render": "run_ui",
            "state": {"dashboard": "dashboard", "jobs": "jobs"}, "routes": WEB_CONTRACT,
            "files": ["core.py", "database.py", "dataflow.py", "service.py", "ui.py"],
            "smoke": {"forms": [
                ["/clients", {"name": "Packaging client"}],
                ["/jobs", {"client_id": 1, "title": "Packaging job", "notes": "First note"}],
                ["/status", {"job_id": 1, "status": "ACTIVE"}],
                ["/notes", {"job_id": 1, "note": "Persistent note"}]],
                "state": {"dashboard": {"jobs_total": 1, "clients_total": 1, "NEW": 0,
                                         "ACTIVE": 1, "DONE": 0},
                          "jobs": [{"id": 1, "client_id": 1, "client_name": "Packaging client",
                                    "title": "Packaging job", "status": "ACTIVE",
                                    "notes": "First note\nPersistent note"}]},
                "html_contains": ["Packaging client", "Packaging job", "Persistent note"]}}


def child_environment():
    # No credentials are needed or inherited by the generated app.
    allowed = {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "HOME", "USERPROFILE",
               "LANG", "LC_ALL", "PYTHONPATH"}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", REDI_LIVE_PROVIDER="0")
    return env


def python_executable():
    # The stdlib app needs no venv. Avoid Windows venv redirector child orphans.
    return getattr(sys, "_base_executable", sys.executable)


def local_request(url, data=None):
    target = urlsplit(url)
    if target.scheme != "http" or target.hostname != "127.0.0.1":
        raise ValueError("only loopback product requests are permitted")
    request = Request(url, data=urlencode(data).encode() if data is not None else None)
    with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
        return response.read().decode("utf-8")


def stop_process(proc):
    if proc.poll() is None:
        proc.terminate()
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait(timeout=5)


def start_process(app, database, ready_file):
    app, ready_file = Path(app).resolve(), Path(ready_file).resolve()
    fingerprint = verify_package(app)
    ready_file.parent.mkdir(parents=True, exist_ok=True)
    # Ready files are unique per launch, so stale metadata cannot signal startup.
    if ready_file.exists():
        raise ValueError("ready file already exists")
    args = [python_executable(), "-B", str(app / "run_app.py"), "--database", str(database),
            "--port", "0", "--ready-file", str(ready_file)]
    log = ready_file.with_suffix(".log").open("wb")
    try:
        proc = subprocess.Popen(args, cwd=app, env=child_environment(), stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    finally: log.close()
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("app startup failed; see " + str(ready_file.with_suffix('.log')))
            if ready_file.exists():
                try:
                    info = read_json(ready_file)
                    health = json.loads(local_request(info["url"] + "api/health"))
                    if health != {"pid": proc.pid, "project_path": str(app),
                                  "database": str(Path(database).resolve()), "package_sha256": fingerprint}:
                        raise ValueError("app process identity mismatch")
                    local_request(info["url"])
                    return proc, info
                except (OSError, json.JSONDecodeError):
                    pass
            time.sleep(.05)
        raise RuntimeError("app startup timed out")
    except BaseException:
        stop_process(proc)
        raise


def smoke_test(app, output):
    """Exercise HTTP forms, rendered data, and persistence across fresh processes."""
    app, output = Path(app).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    contract = read_json(app / "app.json")
    workflow = contract["smoke"]
    database = output / "smoke.sqlite3"
    proc, info = start_process(app, database, output / "first.json")
    try:
        page = local_request(info["url"])
        class Forms(HTMLParser):
            def __init__(self):
                super().__init__(); self.forms = []; self.current = None
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "form":
                    self.current = {"action": attrs.get("action"), "method": attrs.get("method", "get").lower(), "fields": set()}
                    self.forms.append(self.current)
                if tag in ("input", "select", "textarea") and self.current is not None and "disabled" not in attrs:
                    self.current["fields"].add(attrs.get("name"))
            def handle_endtag(self, tag):
                if tag == "form": self.current = None
        parser = Forms(); parser.feed(page)
        for route, spec in contract["routes"].items():
            if not any(f["action"] == route and f["method"] == "post" and set(spec["fields"]) <= f["fields"] for f in parser.forms):
                raise ValueError("missing declared POST form: " + route)
        for route, fields in workflow["forms"]:
            local_request(info["url"].rstrip("/") + route, fields)
        state = json.loads(local_request(info["url"] + "api/state"))
        if state != workflow["state"]:
            raise ValueError("packaged workflow state mismatch")
        page = local_request(info["url"])
        if not all(text in page for text in workflow["html_contains"]):
            raise ValueError("rendered dashboard is missing persisted records")
    finally: stop_process(proc)
    proc, info = start_process(app, database, output / "restart.json")
    try:
        if json.loads(local_request(info["url"] + "api/state")) != state:
            raise ValueError("packaged database did not survive restart")
        if not all(text in local_request(info["url"]) for text in workflow["html_contains"]):
            raise ValueError("rendered dashboard did not survive restart")
    finally: stop_process(proc)
    result = {"status": "PASS", "checks": ["startup", "declared_forms", "create_client",
              "create_job", "update_status", "add_note", "dashboard", "restart", "persistence"],
              "database": str(database), "owner_database_untouched": True,
              "package_sha256": verify_package(app)}
    write_json(output / "result.json", result)
    return result


def package_verified_build(root, contract=None):
    """Publish once, only from persisted passing evidence and matching gate bytes."""
    root = Path(root).resolve()
    contract = contract or crm_delivery_contract()
    with _LOCK:
        evidence_path = root / "live_run_evidence.json"
        evidence = read_json(evidence_path)
        if evidence.get("backend") != "openai-live-multistation-normal-app-experimental":
            raise ValueError("unsupported delivery backend")
        if evidence.get("status") != "VERIFIED" or any(
            evidence.get("gates", {}).get(g) is not True or
            evidence.get("gate_states", {}).get(g) != "PASS" for g in GATES):
            raise ValueError("packaging requires all six frozen gates PASS")
        app = root / "app"
        receipt = root / "packaging_evidence.json"
        if receipt.exists():
            return package_status(root)
        package = contract["module"].split(".")[0]
        if not re.fullmatch(r"[a-zA-Z_]\w*", package):
            raise ValueError("invalid package name")
        artifacts = {a["rel_path"]: a for a in evidence["artifacts"]}
        blobs = {}
        for name in contract["files"]:
            if Path(name).name != name or not name.endswith(".py"):
                raise ValueError("invalid artifact path")
            source = root / "gates" / package / name
            blob = source.read_bytes()
            record = artifacts[name]
            if not (digest(blob) == record["sha256"] == record["stored_sha256"] ==
                    digest(record["content"].encode("utf-8"))):
                raise ValueError("verified source hash mismatch: " + name)
            blobs[package + "/" + name] = blob
        # Never overwrite an existing project or its owner database.
        app.mkdir(exist_ok=False)
        (app / package).mkdir()
        for name, blob in blobs.items(): (app / name).write_bytes(blob)
        (app / "run_app.py").write_bytes(Path(__file__).with_name("product_host.py").read_bytes())
        write_json(app / "app.json", contract)
        (app / "README.md").write_text(
            "# Verified local web app\n\nRequires Python 3.11 or newer; standard library only. "
            "No pip install, provider key, or internet access is needed.\n\n"
            "From this directory run:\n\n```text\npython -B run_app.py --open-browser\n```\n\n"
            "The command prints the loopback URL. Use --port 8766 for a fixed port. "
            "Stop with Ctrl+C; rerun the same command to resume.\n\n"
            "SQLite is stored in data/app.sqlite3 relative to this project, regardless of "
            "your working directory. Override with --database FULL_PATH. Back up this file "
            "while the app is stopped. The owner database starts empty; packaging smoke data "
            "is kept outside this project.\n\nWorkbench OPEN APP starts/reuses this project. "
            "STOP APP releases its server without deleting data.\n", encoding="utf-8")
        (app / "start.cmd").write_bytes(b'@echo off\r\npy -3 -B "%~dp0run_app.py" --open-browser\r\npause\r\n')
        manifest = {"schema_version": 1, "build_id": evidence["build_id"],
                    "evidence_sha256": digest(evidence_path.read_bytes()),
                    "files": {str(p.relative_to(app)).replace("\\", "/"): digest(p.read_bytes())
                              for p in app.rglob("*") if p.is_file()}}
        write_json(app / "package.json", manifest)
        output = root / ("packaging-smoke-" + uuid.uuid4().hex[:12])
        result = {"status": "FAIL", "project_path": str(app), "evidence_sha256": manifest["evidence_sha256"],
                  "package_sha256": verify_package(app)}
        try:
            # A fresh interpreter also isolates HTTP smoke from factory imports/test doubles.
            check = subprocess.run([sys.executable, "-B", "-m", "redi_lab.workbench.product_packaging", "--smoke", str(app), str(output)],
                                   cwd=Path(__file__).resolve().parents[2], env=child_environment(),
                                   capture_output=True, text=True, encoding="utf-8", timeout=120,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if check.returncode:
                raise RuntimeError("packaging smoke failed: " + check.stderr[-4000:])
            result.update(status="PASS", smoke=read_json(output / "result.json"))
        except Exception as exc:
            result["reason"] = str(exc)
        with receipt.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, indent=2)
        return package_status(root)


def package_status(root):
    root = Path(root).resolve()
    result = read_json(root / "packaging_evidence.json")
    if result["status"] != "PASS":
        raise ValueError(result.get("reason", "packaging failed"))
    if 'qualification_sha256' in result:
        from .professional.review import evaluate
        if (digest((root/'qualification.json').read_bytes())!=result['qualification_sha256'] or
                digest((root/'manual_acceptance.json').read_bytes())!=result['manual_sha256']):
            raise ValueError('Qualification approval changed')
        acceptance=(root/result.get('acceptance_directory','acceptance')).resolve()
        if not acceptance.is_relative_to(root):raise ValueError('Invalid acceptance location')
        review=evaluate(read_json(acceptance/'review_evidence.json'),acceptance)
        qualification=read_json(root/'qualification.json')
        if review['status']!='VERIFIED' or review['evidence_hash']!=qualification['medaurus']['evidence_hash']:
            raise ValueError('Qualification acceptance no longer valid')
    app = root / "app"
    if (verify_package(app) != result["package_sha256"] or
            digest((root / "live_run_evidence.json").read_bytes()) != result["evidence_sha256"]):
        raise ValueError("delivery evidence or package changed since smoke test")
    command = '& "' + python_executable() + '" -B "' + str(app / "run_app.py") + '" --open-browser'
    return {**result, "project_path": str(app), "launch_command": command,
            "database_path": str(app / "data" / "app.sqlite3")}


def running_app(root):
    root = Path(root).resolve()
    state_path = root / "app_launch.json"
    if not state_path.exists(): return None
    try:
        state = read_json(state_path)
        health = json.loads(local_request(state["url"] + "api/health"))
        if (health["pid"] == state["pid"] and health["project_path"] == str(root / "app") and
                health["database"] == str(root / "app" / "data" / "app.sqlite3") and
                health["package_sha256"] == package_status(root)["package_sha256"]):
            return state
    except (OSError, ValueError, KeyError): pass
    return None


def open_app(root):
    root = Path(root).resolve()
    with _LOCK:
        status = package_status(root)
        current = running_app(root)
        if current: return current
        app = root / "app"
        proc, info = start_process(app, status["database_path"],
                                   root / "launches" / (uuid.uuid4().hex + ".json"))
        _PROCESSES[str(root)] = proc
        write_json(root / "app_launch.json", info)
        return info


def stop_app(root):
    root = Path(root).resolve()
    with _LOCK:
        proc = _PROCESSES.get(str(root))
        if proc is None or proc.poll() is not None:
            if running_app(root):
                raise ValueError("app was launched by another Workbench process; stop that process's app")
        else:
            stop_process(proc)
            del _PROCESSES[str(root)]
        return {"stopped": True}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("root", type=Path)
    parser.add_argument("output", nargs="?", type=Path)
    args = parser.parse_args()
    print(json.dumps(smoke_test(args.root, args.output) if args.smoke else package_verified_build(args.root)))
