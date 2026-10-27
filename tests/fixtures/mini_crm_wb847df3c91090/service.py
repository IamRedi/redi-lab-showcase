"""Integration helpers for the Mini CRM / Job Manager.

This module wires the persistent CRM service to the static local dashboard.
"""

from __future__ import annotations

import os
import tempfile
import webbrowser
from pathlib import Path
from typing import Optional

from .crm import CRMService
from .ui import render_dashboard


DEFAULT_DATABASE = "mini_crm.sqlite3"


def build_crm(database_path: str = DEFAULT_DATABASE) -> CRMService:
    """Build a CRM backed by *database_path*."""
    return CRMService(database_path)


def _temporary_database() -> str:
    """Create an empty, unique SQLite file path for an isolated operation."""
    handle = tempfile.NamedTemporaryFile(prefix="mini_crm_", suffix=".sqlite3", delete=False)
    handle.close()
    return handle.name


def _remove_database(database_path: str) -> None:
    """Remove a temporary SQLite database and any SQLite sidecar files."""
    for path in (database_path, database_path + "-wal", database_path + "-shm"):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def run_acceptance() -> dict:
    """Run the deterministic Mini CRM acceptance scenario.

    A new on-disk SQLite database is used for every call because persistence
    operations may use separate database connections.
    """
    database_path = _temporary_database()
    try:
        crm = build_crm(database_path)
        client_id = crm.create_client("Acceptance Client")
        first_job_id = crm.create_job(client_id, "Acceptance active job", notes="Initial note")
        second_job_id = crm.create_job(client_id, "Acceptance completed job")

        crm.set_status(first_job_id, "ACTIVE")
        crm.set_status(second_job_id, "ACTIVE")
        crm.set_status(second_job_id, "DONE")

        return crm.dashboard()
    finally:
        _remove_database(database_path)


def integration_probe() -> dict:
    """Verify that a fresh CRM can persist one client and one job."""
    database_path = _temporary_database()
    try:
        crm = build_crm(database_path)
        client_id = crm.create_client("Probe Client")
        crm.create_job(client_id, "Probe job")
        return {"crm": "ready", "clients": 1, "jobs": 1}
    finally:
        _remove_database(database_path)


def run_ui(database_path: str = DEFAULT_DATABASE, output_path: Optional[str] = None) -> str:
    """Render the local dashboard, open it in a browser, and return its path.

    The dashboard is a self-contained HTML document, so it needs no web
    server.  Supplying ``output_path`` is useful when a caller wants to choose
    where the generated local page is written.
    """
    crm = build_crm(database_path)
    html = render_dashboard(crm.dashboard(), crm.jobs())
    target = Path(output_path or "mini_crm_dashboard.html").resolve()
    target.write_text(html, encoding="utf-8")
    webbrowser.open(target.as_uri())
    return str(target)


if __name__ == "__main__":
    run_ui()
