"""Executable station contracts, before completion and before frozen gates.

Generated code runs in a fresh credential-free interpreter and temporary package.
This is a fail-fast check, not a replacement for qualification or an OS sandbox.
"""
from __future__ import annotations

import ast
import copy
from html import escape
import importlib
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from .normal_app_contracts import CALL_SHAPES, DASHBOARD_KEYS, WEB_CONTRACT, validate_local_imports


class IntegrityError(ValueError):
    pass


def check_product(artifacts, *, stage, root):
    for name, content in artifacts.items():
        validate_local_imports(content)
        if name == "ui.py":
            validate_ui_source(content)
    Path(root).mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[2]
    command = [sys.executable, "-I", "-B", "-c",
        "import sys; sys.path.insert(0, " + repr(str(repo)) + "); "
        "from redi_lab.workbench.product_integrity import _worker; _worker()"]
    env = {k: v for k, v in os.environ.items() if k.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"}}
    with tempfile.TemporaryDirectory(prefix="integrity-", dir=root) as directory:
        result = subprocess.run(command, input=json.dumps({"artifacts": artifacts, "stage": stage}),
            cwd=directory, env=env, text=True, encoding="utf-8", capture_output=True, timeout=30)
    if result.returncode:
        raise IntegrityError("integrity worker failed: " + result.stderr[-1000:])
    report = json.loads(result.stdout)
    if report["status"] != "PASS":
        raise IntegrityError(report["reason"])
    return report


def _require(condition, reason):
    if not condition:
        raise IntegrityError(reason)


def validate_ui_source(source):
    """Bound UI generation to composition that never interprets literal CSS braces."""
    for node in ast.walk(ast.parse(source)):
        bad_call = isinstance(node, ast.Call) and (
            isinstance(node.func, ast.Attribute) and node.func.attr in {
                "format", "format_map", "substitute", "safe_substitute"}
            or isinstance(node.func, ast.Name) and node.func.id in {"format", "Template"})
        if bad_call or isinstance(node, ast.JoinedStr) or (
                isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod)):
            raise IntegrityError("UI safe-rendering contract: use literal strings + concatenation/join; "
                                 f"template formatting is forbidden (line {node.lineno})")


def validate_forms(html, dashboard=None):
    from html.parser import HTMLParser
    class Forms(HTMLParser):
        def __init__(self):
            super().__init__(); self.forms = []; self.current = None
            self.counts = {}; self.count_key = None; self.count_text = []
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            _require(tag != "script" and not any(k.startswith("on") for k in attrs),
                     "UI must use real POST forms, without browser-only mutation handlers")
            _require(not any(k in attrs for k in ("formaction", "formmethod", "formenctype")),
                     "UI transport overrides are forbidden")
            for attr in ("src", "href", "action"):
                if attr in attrs:
                    target = attrs[attr] or ""
                    _require(target.startswith(("/", "#")) and not target.startswith("//"),
                             "UI external or undeclared resource target")
            _require("srcset" not in attrs, "UI external resources are forbidden")
            if "data-count" in attrs:
                key = attrs["data-count"]
                _require(tag == "span" and key in DASHBOARD_KEYS and key not in self.counts and self.count_key is None,
                         "UI invalid/duplicate dashboard count span")
                self.count_key = key; self.count_text = []
            if tag == "form":
                _require(self.current is None, "UI nested forms are forbidden")
                _require(attrs.get("action") in WEB_CONTRACT and attrs.get("method", "get").lower() == "post",
                         "UI undeclared POST form route/method")
                self.current = {"method": attrs.get("method", "get").lower(),
                                "action": attrs.get("action"), "fields": set(), "submit": False}
                self.forms.append(self.current)
            if self.current is not None:
                if tag in {"input", "select", "textarea"} and attrs.get("name") and "disabled" not in attrs:
                    self.current["fields"].add(attrs["name"])
                if tag == "button" and attrs.get("type", "submit") == "submit" or tag == "input" and attrs.get("type") == "submit":
                    self.current["submit"] = True
        def handle_endtag(self, tag):
            if tag == "form": self.current = None
            if tag == "span" and self.count_key is not None:
                self.counts[self.count_key] = "".join(self.count_text).strip()
                self.count_key = None
        def handle_data(self, data):
            if self.count_key is not None: self.count_text.append(data)
    parser = Forms(); parser.feed(html)
    for route, spec in WEB_CONTRACT.items():
        _require(any(f["action"] == route and f["method"] == "post" and f["submit"]
                     and set(spec["fields"]) <= f["fields"] for f in parser.forms),
                 "UI missing usable POST form contract: " + route)
    if dashboard is not None:
        _require(parser.counts == {key: str(dashboard[key]) for key in DASHBOARD_KEYS},
                 "UI dashboard count mismatch or unresolved template placeholder")
    return [{**f, "fields": sorted(f["fields"])} for f in parser.forms]


def smoke_ui(render):
    """Execute independently of the backend, including literal template-looking data."""
    special = 'Literal {font-family} ${missing} %s <&> "quoted"'
    cases = [
        (dict.fromkeys(DASHBOARD_KEYS, 0), []),
        ({"jobs_total": 3, "clients_total": 2, "NEW": 1, "ACTIVE": 1, "DONE": 1},
         [{"id": i + 11, "client_id": 7, "client_name": "Client " + special,
           "title": "Job " + special, "status": status, "notes": "Note " + special}
          for i, status in enumerate(("NEW", "ACTIVE", "DONE"))]),
        ({"jobs_total": 0, "clients_total": 17, "NEW": 0, "ACTIVE": 0, "DONE": 0}, []),
    ]
    for dashboard, jobs in cases:
        original = copy.deepcopy((dashboard, jobs))
        html = render(dashboard=dashboard, jobs=jobs)
        _require(type(html) is str and "<body" in html.lower(), "UI render must produce an HTML document")
        _require((dashboard, jobs) == original, "UI renderer mutated backend input")
        validate_forms(html, dashboard)
        for job in jobs:
            for key in ("client_name", "title", "notes"):
                _require(escape(job[key]) in html, "UI must render and escape representative " + key)
    return ["ui_safe_composition", "ui_render_empty", "ui_render_populated", "ui_literal_braces",
            "ui_dashboard_placeholders", "ui_declared_transport", "ui_input_unchanged"]


def _signatures(name, module):
    for path, kwargs in CALL_SHAPES[name].items():
        obj = module
        parts = path.split(".")
        for part in parts:
            obj = getattr(obj, part)
        _require(callable(obj), name + ": non-callable " + path)
        # Class methods are unbound here; all public names/keyword shapes are contractual.
        try:
            inspect.signature(obj).bind(*([None] if len(parts) > 1 else []), **kwargs)
        except TypeError as exc:
            raise IntegrityError(f"{name}:{path} incompatible with keywords {list(kwargs)}: {exc}") from exc


def _exercise(modules, stage):
    checks = ["syntax", "package_imports", "public_symbols", "call_signatures"]
    for name, module in modules.items():
        _signatures(name + ".py", module)
    if "ui" in modules:
        checks.extend(smoke_ui(modules["ui"].render_dashboard))
    database = modules.get("database")
    if database:
        import sqlite3
        conn = database.connect(":memory:")
        try:
            _require(isinstance(conn, sqlite3.Connection), "connect must return sqlite3.Connection")
            _require(conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1, "foreign keys disabled")
            _require(conn.row_factory == sqlite3.Row, "sqlite3.Row required")
            try:
                counts = database.dashboard_counts(conn)
            except Exception as exc:
                raise IntegrityError("database.dashboard_counts(empty database) must return integer zeros; "
                                     "normalize SQL NULL aggregates before conversion: " + type(exc).__name__ + ": " + str(exc)) from exc
            _require(counts == dict.fromkeys(DASHBOARD_KEYS, 0) and all(type(v) is int for v in counts.values()),
                     "database.dashboard_counts(empty database) must return integer zeros")
        finally: conn.close()
        checks.append("sqlite_initialization")
    flow = modules.get("dataflow")
    if flow:
        _require(tuple(flow.VALID_STATUSES) == ("NEW", "ACTIVE", "DONE"), "status constants drift")
        _require({k: tuple(v) for k, v in flow.TRANSITIONS.items()} == {"NEW": ("ACTIVE",), "ACTIVE": ("DONE",), "DONE": ()}, "transition constants drift")
        _require(flow.normalize_status(" active ") == "ACTIVE", "normalization drift")
        for old in flow.VALID_STATUSES:
            for new in flow.VALID_STATUSES:
                _require(flow.can_transition(old, new) is (old == new or new in flow.TRANSITIONS[old]), "transition semantics drift")
        try: flow.normalize_status("bogus")
        except ValueError: pass
        else: raise IntegrityError("invalid status accepted")
        checks.append("state_transitions")
    core = modules.get("core")
    if core:
        db = str(Path("smoke.sqlite").resolve())
        factory = modules["service"].build_crm if "service" in modules else core.CRMService
        svc = factory(path=db)
        try:
            _require(svc.dashboard() == dict.fromkeys(DASHBOARD_KEYS, 0), "new database is not empty")
            cid = svc.create_client(name="Client <&>")
            _require(type(cid) is int and cid > 0, "create_client must return int ID")
            jid = svc.create_job(client_id=cid, title="Job <&>", notes="Initial <&> note")
            _require(type(jid) is int and jid > 0, "create_job must return int ID")
            _require(svc.set_status(job_id=jid, status=" active ") == "ACTIVE", "set_status must return normalized str")
            _require(svc.set_status(job_id=jid, status="ACTIVE") == "ACTIVE", "idempotent status failed")
            try: svc.set_status(job_id=jid, status="NEW")
            except ValueError: pass
            else: raise IntegrityError("illegal reverse transition accepted")
            svc.add_note(job_id=jid, note="Persisted follow-up")
            expected = {"jobs_total": 1, "clients_total": 1, "NEW": 0, "ACTIVE": 1, "DONE": 0}
            _require(svc.dashboard() == expected, "dashboard keys/counts drift")
            jobs = svc.jobs()
            _require(type(jobs) is list and len(jobs) == 1, "jobs must return list of records")
            job = jobs[0]
            for key, typ in {"id": int, "client_id": int, "client_name": str, "title": str, "status": str, "notes": str}.items():
                _require(key in job and type(job[key]) is typ, "job key/type drift: " + key)
            _require(job["client_id"] == cid and "Initial <&> note" in job["notes"] and "Persisted follow-up" in job["notes"], "notes/client relationship drift")
            if "ui" in modules:
                html = modules["ui"].render_dashboard(dashboard=svc.dashboard(), jobs=jobs)
                _require(type(html) is str and "Job &lt;&amp;&gt;" in html and "Persisted follow-up" in html, "UI must render and escape actual job data")
                validate_forms(html)
                validate_forms(modules["ui"].render_dashboard(dashboard=dict.fromkeys(DASHBOARD_KEYS, 0), jobs=[]))
                checks.append("frontend_backend_forms")
        finally:
            svc.close(); svc.close()
        svc = factory(path=db)
        try:
            _require(svc.dashboard() == expected and svc.jobs() == jobs, "persistence lost across close/reopen")
        finally: svc.close()
        Path(db).unlink()  # Windows also proves the connection is really closed.
        checks.extend(["return_types", "schema_keys", "sqlite_lifecycle", "persistence", "notes"])
    if "service" in modules:
        svc = modules["service"]
        for _ in range(2):
            _require(svc.run_acceptance() == {"jobs_total": 2, "NEW": 0, "ACTIVE": 1, "DONE": 1, "clients_total": 1}, "acceptance entrypoint contract mismatch")
            _require(svc.integration_probe() == {"crm": "ready", "clients": 1, "jobs": 1}, "integration entrypoint mismatch")
        validate_forms(svc.run_ui(path=":memory:"))
        checks.extend(["assembly_entrypoints", "repeat_isolation"])
    return {"status": "PASS", "stage": stage, "checks": checks}


def _worker():
    import contextlib
    import io
    import socket
    import sqlite3
    payload = json.load(sys.stdin)
    def forbidden(*args, **kwargs): raise RuntimeError("network forbidden in integrity checks")
    socket.socket.connect = forbidden
    socket.create_connection = forbidden
    socket.getaddrinfo = forbidden
    tempfile.tempdir = str(Path.cwd())
    connections = []
    original_connect = sqlite3.connect
    def tracked_connect(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        connections.append(conn)
        return conn
    sqlite3.connect = tracked_connect
    package = Path("mini_crm"); package.mkdir()
    for name, source in payload["artifacts"].items():
        (package / name).write_text(source, encoding="utf-8", newline="")
    sys.path.insert(0, str(Path.cwd()))
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            modules = {name[:-3]: importlib.import_module("mini_crm." + name[:-3]) for name in payload["artifacts"]}
            # Import-time I/O is forbidden by the contract.
            _require(not any(p.is_file() for p in Path.cwd().iterdir()), "import-time filesystem side effect")
            _require(not connections, "import-time SQLite side effect")
            report = _exercise(modules, payload["stage"])
            for conn in connections:
                try: conn.execute("SELECT 1")
                except sqlite3.ProgrammingError: continue
                raise IntegrityError("SQLite connection leaked after lifecycle smoke")
    except Exception as exc:
        report = {"status": "FAIL", "stage": payload["stage"], "reason": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(report))
