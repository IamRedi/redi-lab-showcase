"""Application assembly and runnable entry points for Mini CRM.

This module deliberately keeps orchestration separate from the CRM domain
service.  Persistence and validation remain the responsibility of
:class:`mini_crm.crm.CRMService`.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import webbrowser

from .crm import CRMService
from .ui import render_dashboard


def build_crm(database_path: str | Path = "mini_crm.sqlite3") -> CRMService:
    """Create a CRM service backed by ``database_path``.

    ``CRMService`` owns schema setup, so this is the single application-level
    construction point used by the command helpers below.
    """
    return CRMService(str(database_path))


def _fresh_database_path(directory: Path) -> Path:
    """Return an explicitly created, isolated SQLite file path."""
    path = directory / "mini_crm.sqlite3"
    path.touch()
    return path


def run_acceptance() -> dict[str, int]:
    """Run the small deterministic acceptance scenario in an isolated store."""
    with tempfile.TemporaryDirectory(prefix="mini_crm_acceptance_") as temporary:
        crm = build_crm(_fresh_database_path(Path(temporary)))
        client_id = crm.create_client("Acceptance Client")
        first_job_id = crm.create_job(client_id, "First acceptance job", notes="Created for acceptance")
        second_job_id = crm.create_job(client_id, "Second acceptance job")

        # These are the legal forward transitions: NEW -> ACTIVE -> DONE.
        crm.set_status(first_job_id, "ACTIVE")
        crm.set_status(second_job_id, "ACTIVE")
        crm.set_status(second_job_id, "DONE")
        return crm.dashboard()


def integration_probe() -> dict[str, int | str]:
    """Exercise construction and persistence with a fresh database per call."""
    with tempfile.TemporaryDirectory(prefix="mini_crm_probe_") as temporary:
        crm = build_crm(_fresh_database_path(Path(temporary)))
        client_id = crm.create_client("Probe Client")
        crm.create_job(client_id, "Probe Job")
        dashboard = crm.dashboard()
        return {
            "crm": "ready",
            "clients": dashboard["clients_total"],
            "jobs": dashboard["jobs_total"],
        }


def run_ui(
    database_path: str | Path = "mini_crm.sqlite3",
    output_path: str | Path = "mini_crm.html",
    *,
    open_browser: bool = True,
) -> Path:
    """Render the local dashboard to an HTML file and optionally open it.

    The generated document is self-contained and can be opened without a web
    server.  The returned path makes this helper convenient for scripts and
    tests that choose not to launch a browser.
    """
    crm = build_crm(database_path)
    html = render_dashboard(crm.dashboard(), crm.jobs())
    destination = Path(output_path).expanduser().resolve()
    destination.write_text(html, encoding="utf-8")
    if open_browser:
        webbrowser.open(destination.as_uri())
    return destination


if __name__ == "__main__":
    run_ui()
