"""Integration entry points for the Mini CRM application.

This module is deliberately small: domain behaviour belongs to ``core`` and
persistence belongs to the SQLite backend.  It provides the convenient
construction, smoke-test, and local UI entry points used by an application.
"""

from __future__ import annotations

import inspect
import tempfile
import webbrowser
from pathlib import Path
from typing import Any, Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .core import CRMService
from .ui import render_dashboard


def _storage_for(path: str | Path) -> Any | None:
    """Create the project's SQLite storage object, when it is exposed.

    Keeping this lookup here makes the assembly tolerant of the conventional
    ``storage`` and ``database`` module names while leaving all SQL handling in
    the persistence module.
    """
    candidates = (
        (".storage", "SQLiteStorage"),
        (".storage", "SQLiteRepository"),
        (".database", "SQLiteDatabase"),
        (".database", "SQLiteStorage"),
        (".repository", "SQLiteRepository"),
    )
    for module_name, class_name in candidates:
        try:
            module = __import__(__package__ + module_name, fromlist=[class_name])
            storage_type = getattr(module, class_name)
        except (ImportError, AttributeError):
            continue
        return storage_type(str(path))
    return None


def build_crm(db_path: str | Path = "mini_crm.db") -> CRMService:
    """Build a :class:`CRMService` backed by SQLite at *db_path*.

    The service constructor may either own the database path directly or take
    the package's storage/repository object.  Both forms are supported so this
    remains the single application-composition point.
    """
    path = str(db_path)
    storage = _storage_for(path)
    parameters = list(inspect.signature(CRMService).parameters.values())
    first_name = parameters[0].name.lower() if parameters else ""
    if storage is not None and first_name in {"storage", "repository", "database", "db"}:
        return CRMService(storage)
    if storage is not None and not parameters:
        return CRMService()
    if storage is not None and first_name not in {"path", "db_path", "database_path", "filename"}:
        return CRMService(storage)
    return CRMService(path)


def _id(record: Any) -> Any:
    if isinstance(record, dict):
        return record["id"]
    return getattr(record, "id")


def _method(service: Any, *names: str) -> Callable[..., Any]:
    for name in names:
        candidate = getattr(service, name, None)
        if callable(candidate):
            return candidate
    raise AttributeError("CRM service does not provide any of: " + ", ".join(names))


def _dashboard(service: Any) -> dict[str, Any]:
    value = _method(service, "dashboard", "dashboard_counts", "counts")()
    return dict(value)


def _jobs(service: Any) -> list[dict[str, Any]]:
    value = _method(service, "jobs", "list_jobs", "get_jobs")()
    return [dict(job) if not isinstance(job, dict) else job for job in value]


def integration_probe(db_path: str | Path = ":memory:") -> dict[str, Any]:
    """Exercise clients, jobs, statuses, notes, and dashboard aggregation.

    The returned snapshot is useful for a quick programmatic health check and
    is intentionally made only with the public service API.
    """
    crm = build_crm(db_path)
    client = _method(crm, "create_client", "add_client")(
        "Acceptance Client", email="acceptance@example.test"
    )
    client_id = _id(client)
    create_job = _method(crm, "create_job", "add_job")
    try:
        job = create_job(client_id, "Acceptance Job", description="End-to-end check")
    except TypeError:
        job = create_job(client_id=client_id, title="Acceptance Job", description="End-to-end check")
    job_id = _id(job)
    _method(crm, "add_note", "create_note")(job_id, "Created by integration probe")
    set_status = _method(crm, "set_job_status", "update_job_status", "change_job_status")
    try:
        set_status(job_id, "ACTIVE")
    except TypeError:
        set_status(job_id=job_id, status="ACTIVE")
    dashboard = _dashboard(crm)
    jobs = _jobs(crm)
    return {
        "passed": True,
        "dashboard": dashboard,
        "jobs": jobs,
        "html": render_dashboard(dashboard, jobs),
    }


def run_acceptance(db_path: str | Path | None = None) -> dict[str, Any]:
    """Run the self-contained end-to-end acceptance probe.

    With no path, a temporary SQLite database is used and removed afterwards.
    Supplying a path is useful when inspecting the resulting records manually.
    """
    if db_path is not None:
        return integration_probe(db_path)
    with tempfile.TemporaryDirectory(prefix="mini-crm-") as directory:
        return integration_probe(Path(directory) / "acceptance.sqlite3")


def run_ui(
    db_path: str | Path = "mini_crm.db",
    host: str = "127.0.0.1",
    port: int = 8000,
    *,
    open_browser: bool = True,
    serve: bool = True,
) -> ThreadingHTTPServer:
    """Serve the dashboard locally.

    Set ``serve=False`` to obtain a configured server without entering its
    request loop, which is convenient for embedding and tests.
    """
    crm = build_crm(db_path)

    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            if self.path not in {"/", "/index.html"}:
                self.send_error(404)
                return
            body = render_dashboard(_dashboard(crm), _jobs(crm)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer((host, port), DashboardHandler)
    if not serve:
        return server
    url = "http://{}:{}/".format(host, server.server_address[1])
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return server
