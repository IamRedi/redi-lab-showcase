"""Integration entry points for the Mini CRM / Job Manager."""

from __future__ import annotations

import argparse
from pathlib import Path
import tempfile
import webbrowser

from .crm import CRMService
from .ui import render_dashboard


def build_crm(database_path: str | Path = "mini_crm.sqlite3") -> CRMService:
    """Build a CRM service backed by ``database_path``."""
    return CRMService(str(database_path))


def run_acceptance() -> dict[str, int]:
    """Run the deterministic acceptance scenario in an isolated database."""
    with tempfile.TemporaryDirectory(prefix="mini_crm_acceptance_") as directory:
        crm = build_crm(Path(directory) / "acceptance.sqlite3")
        client_id = crm.create_client("Acceptance Client")
        first_job_id = crm.create_job(
            client_id,
            "Acceptance active job",
            notes="Created by the acceptance scenario.",
        )
        second_job_id = crm.create_job(
            client_id,
            "Acceptance completed job",
            notes="Created by the acceptance scenario.",
        )

        crm.set_status(first_job_id, "ACTIVE")
        crm.set_status(second_job_id, "ACTIVE")
        crm.set_status(second_job_id, "DONE")
        return crm.dashboard()


def integration_probe() -> dict[str, int | str]:
    """Exercise persistence with a minimal, isolated CRM instance."""
    with tempfile.TemporaryDirectory(prefix="mini_crm_probe_") as directory:
        crm = build_crm(Path(directory) / "probe.sqlite3")
        client_id = crm.create_client("Probe Client")
        crm.create_job(client_id, "Probe Job", notes="Integration probe note.")
        dashboard = crm.dashboard()
        return {
            "crm": "ready",
            "clients": dashboard["clients_total"],
            "jobs": dashboard["jobs_total"],
        }


def run_ui(
    database_path: str | Path = "mini_crm.sqlite3",
    output_path: str | Path = "mini_crm_dashboard.html",
    *,
    open_browser: bool = True,
) -> Path:
    """Render the current CRM dashboard to a local HTML file.

    The generated document is self-contained and can be opened without a web
    server.  The output path is returned so callers can display or distribute
    the file themselves.
    """
    crm = build_crm(database_path)
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_dashboard(crm.dashboard(), crm.jobs()), encoding="utf-8")
    if open_browser:
        webbrowser.open(output.as_uri())
    return output


def main() -> None:
    """Render and open the local dashboard when run as a module."""
    parser = argparse.ArgumentParser(description="Render the Mini CRM dashboard")
    parser.add_argument("--database", default="mini_crm.sqlite3", help="SQLite database path")
    parser.add_argument("--output", default="mini_crm_dashboard.html", help="HTML output path")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the rendered file")
    args = parser.parse_args()
    output = run_ui(args.database, args.output, open_browser=not args.no_browser)
    print(output)


if __name__ == "__main__":
    main()
