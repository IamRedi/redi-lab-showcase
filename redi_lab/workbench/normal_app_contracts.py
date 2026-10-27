"""Versioned producer/consumer contract for the existing Mini CRM factory."""

from __future__ import annotations

import ast
import hashlib
from typing import Any, Mapping

CONTRACT_ID = "mini_crm_interfaces_v4"
DASHBOARD_KEYS = ["jobs_total", "NEW", "ACTIVE", "DONE", "clients_total"]
UI_RENDERING_POLICY = {
    "id": "literal_markup_composition_v1",
    "composition": "Build HTML with literal strings plus concatenation and str.join ONLY. "
        "Keep CSS as literal text: its braces are not template placeholders. "
        "Do not use .format(), .format_map(), f-strings, percent interpolation or template engines in ui.py. "
        "Convert each dynamic value with str(), escape user text with html.escape, and insert it once by concatenation. "
        "Example: '<style>body{font-family:sans-serif}</style><p>' + escape(str(job['title'])) + '</p>'. "
        "Do not re-interpret braces, dollar signs or placeholder-looking text inside user data.",
    "counts": "Render each of the five dashboard keys exactly once as "
        "<span data-count=\"KEY\">INTEGER_VALUE</span>, using the actual dashboard value. "
        "Labels can surround these spans. No unfilled placeholders or hard-coded counts.",
    "verification": "Before Teuta completes, isolated checks import and execute render_dashboard on empty, "
        "populated and special-character data; check count spans, escaped job text and every POST route/field. "
        "Any formatting exception, missing field, unknown route or unresolved count placeholder fails the task.",
}
INTERFACES = {
    "database.py": {
        "exports": {"connect(path: str)": "sqlite3.Connection",
                    "dashboard_counts(conn: sqlite3.Connection)": "dict[str, int]"},
        "semantics": "connect initializes schema, enables foreign_keys, uses sqlite3.Row; caller owns connection. "
        "Tables: clients(id INTEGER PRIMARY KEY, name TEXT NOT NULL); "
        "jobs(id INTEGER PRIMARY KEY, client_id REFERENCES clients(id), title TEXT NOT NULL, "
        "status TEXT CHECK status IN ('NEW','ACTIVE','DONE'), notes TEXT NOT NULL DEFAULT ''). "
        "Same connection survives all service operations; commits persist to supplied path. No import-time I/O. "
        "Every dashboard count is an int, including an EMPTY database: every key must be 0. "
        "SQLite SUM over no rows returns NULL; use COUNT(CASE WHEN ... THEN 1 END) or COALESCE(SUM(...),0). "
        "Never call int on an unnormalized SQL aggregate. Empty totals are zero; missing field values are not zero.",
        "dashboard_keys": DASHBOARD_KEYS,
    },
    "dataflow.py": {
        "exports": {"VALID_STATUSES": ["NEW", "ACTIVE", "DONE"],
                    "TRANSITIONS": {"NEW": ["ACTIVE"], "ACTIVE": ["DONE"], "DONE": []},
                    "normalize_status(status: str)": "str",
                    "can_transition(old: str, new: str)": "bool"},
        "semantics": "normalize_status strips/uppercases strings; invalid input raises ValueError. "
        "can_transition normalizes both inputs; same-status is allowed; otherwise use TRANSITIONS.",
    },
    "core.py": {
        "exports": {"CRMService(path: str = ':memory:')": "CRMService",
                    "CRMService.create_client(name: str)": "int",
                    "CRMService.create_job(client_id: int, title: str, notes: str = '')": "int",
                    "CRMService.set_status(job_id: int, status: str)": "str",
                    "CRMService.dashboard()": "dict[str, int]",
                    "CRMService.jobs()": "list[dict]",
                    "CRMService.add_note(job_id: int, note: str)": "None",
                    "CRMService.close()": "None"},
        "job_keys": {"id": "int", "client_id": "int", "client_name": "str",
                     "title": "str", "status": "str", "notes": "str"},
        "semantics": "create_client and create_job return int ids, never records. create_job starts NEW. "
        "set_status uses normalize_status AND can_transition; invalid status/transition/missing job raises ValueError; "
        "returns normalized status. Jobs include notes and client_name. Keep one connection from database.connect(path) "
        "until close(); close is idempotent. add_note appends a nonempty note to jobs.notes, separated by a newline, "
        "and raises ValueError for a missing job. Commit all writes; no implicit demo data; no import-time database access.",
        "dashboard_keys": DASHBOARD_KEYS,
    },
    "ui.py": {
        "exports": {"render_dashboard(dashboard: dict, jobs: list[dict])": "str"},
        "rendering_policy": UI_RENDERING_POLICY,
        "semantics": "Return self-contained HTML. Read EXACT dashboard and job keys above, including clients_total, "
        "jobs_total and notes. Escape all user text. No shape-guessing adapters or I/O during rendering/import. "
        "Include ordinary HTML forms (method=post) for EVERY route in web_contract; render all forms even when empty. "
        "Use numeric inputs for IDs, with visible labels. Display job IDs and client IDs in the table. "
        "The trusted local host handles these forms against the real service/SQLite database and redirects back. "
        "No JavaScript mutation, localStorage, browser-only demo state, or external assets. Keep UI concise.",
    },
    "service.py": {
        "exports": {"build_crm(path: str = ':memory:')": "CRMService",
                    "run_acceptance()": "dict[str, int]", "integration_probe()": "dict",
                    "run_ui(path: str = ':memory:')": "str", "normalize_status(status: str)": "str"},
        "semantics": "Import CRMService from .core, normalize_status from .dataflow (re-export for private checks), "
        "render_dashboard from .ui. build_crm returns empty CRMService. run_ui returns rendered HTML and closes service; "
        "provide python -m mini_crm.service entrypoint to save local HTML. "
        "Acceptance/probe each use a fresh per-call temporary SQLite FILE, close CRMService in finally BEFORE removing "
        "file/directory (required on Windows). run_acceptance creates one client/two jobs with notes; first NEW->ACTIVE; "
        "second NEW->ACTIVE->DONE; return actual dashboard. integration_probe creates one client/one job and derives "
        "clients/jobs from actual dashboard (never literal success counts). Repeated calls/imports remain isolated.",
    },
}

# Machine-readable call shapes used by prompts, integrity checks and the local host.
CALL_SHAPES = {
    "database.py": {"connect": {"path": ":memory:"}, "dashboard_counts": {"conn": None}},
    "dataflow.py": {"normalize_status": {"status": "NEW"}, "can_transition": {"old": "NEW", "new": "ACTIVE"}},
    "core.py": {"CRMService": {"path": ":memory:"},
        "CRMService.create_client": {"name": "Client"},
        "CRMService.create_job": {"client_id": 1, "title": "Job", "notes": "Note"},
        "CRMService.set_status": {"job_id": 1, "status": "ACTIVE"},
        "CRMService.add_note": {"job_id": 1, "note": "Note"},
        "CRMService.dashboard": {}, "CRMService.jobs": {}, "CRMService.close": {}},
    "ui.py": {"render_dashboard": {"dashboard": {}, "jobs": []}},
    "service.py": {"build_crm": {"path": ":memory:"}, "run_acceptance": {},
        "integration_probe": {}, "run_ui": {"path": ":memory:"}, "normalize_status": {"status": "NEW"}},
}
WEB_CONTRACT = {
    "/clients": {"method": "create_client", "fields": {"name": "str"}},
    "/jobs": {"method": "create_job", "fields": {"client_id": "int", "title": "str", "notes": "str"}},
    "/status": {"method": "set_status", "fields": {"job_id": "int", "status": "str"}},
    "/notes": {"method": "add_note", "fields": {"job_id": "int", "note": "str"}},
}


def artifact_metadata(name: str, content: str) -> dict[str, Any]:
    """Observed source signatures, not guessed return shapes; bytes remain authoritative."""
    try:
        tree = ast.parse(content)
    except SyntaxError as exc:
        return {"artifact": name, "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "bytes": len(content.encode("utf-8")), "parse_error": str(exc)}
    symbols: dict[str, Any] = {}
    imports = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            imports.append({"module": "." * node.level + (node.module or ""),
                            "names": [a.name for a in node.names]})
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            children = node.body if isinstance(node, ast.ClassDef) else [node]
            for child in children:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    key = f"{node.name}.{child.name}" if isinstance(node, ast.ClassDef) else node.name
                    symbols[key] = {"arguments": ast.unparse(child.args),
                                    "returns": ast.unparse(child.returns) if child.returns else None}
    return {"artifact": name, "module": "mini_crm." + name.removesuffix(".py"),
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "bytes": len(content.encode("utf-8")), "symbols": symbols, "imports": imports}


def validate_local_imports(content: str) -> None:
    """Fail closed on invented sibling modules; no executing generated source."""
    compile(content, "<owned artifact>", "exec")
    allowed = {name.removesuffix(".py") for name in INTERFACES}
    for node in ast.walk(ast.parse(content)):
        if isinstance(node, ast.ImportFrom) and node.level:
            if node.level != 1 or not node.module or node.module not in allowed:
                raise ValueError(f"undeclared local import: {'.' * node.level}{node.module or ''}")
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("mini_crm."):
            if node.module.split(".", 1)[1] not in allowed:
                raise ValueError(f"undeclared local import: {node.module}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("mini_crm.") and alias.name.split(".", 1)[1] not in allowed:
                    raise ValueError(f"undeclared local import: {alias.name}")


def handoff(artifacts: Mapping[str, str]) -> list[dict[str, Any]]:
    return [artifact_metadata(name, source) for name, source in sorted(artifacts.items())]
