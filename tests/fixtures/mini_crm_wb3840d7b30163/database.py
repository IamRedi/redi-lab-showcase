"""SQLite persistence helpers for the Mini CRM / Job Manager."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Union

DATABASE_PATH = "mini_crm.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT,
    phone TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'NEW'
        CHECK (status IN ('NEW', 'ACTIVE', 'DONE')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_jobs_client_id ON jobs(client_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_notes_job_id ON notes(job_id);
"""


def get_connection(
    database_path: Union[str, Path] = DATABASE_PATH,
) -> sqlite3.Connection:
    """Open a database connection with SQLite foreign-key enforcement enabled."""
    connection = sqlite3.connect(str(database_path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def create_schema(connection: sqlite3.Connection) -> None:
    """Create all Mini CRM tables and indexes on an existing connection."""
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(_SCHEMA)
    connection.commit()


def init_db(database_path: Union[str, Path] = DATABASE_PATH) -> None:
    """Create the Mini CRM database schema if it does not already exist."""
    connection = get_connection(database_path)
    try:
        create_schema(connection)
    finally:
        connection.close()


def initialize_database(database_path: Union[str, Path] = DATABASE_PATH) -> None:
    """Compatibility-friendly name for :func:`init_db`."""
    init_db(database_path)


def dashboard_counts(connection: sqlite3.Connection) -> dict[str, int]:
    """Return the aggregate values needed by the dashboard.

    Status counts are always present, including when there are no jobs in a
    particular status.
    """
    row = connection.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM clients) AS clients,
            (SELECT COUNT(*) FROM jobs) AS jobs,
            (SELECT COUNT(*) FROM jobs WHERE status = 'NEW') AS new_count,
            (SELECT COUNT(*) FROM jobs WHERE status = 'ACTIVE') AS active_count,
            (SELECT COUNT(*) FROM jobs WHERE status = 'DONE') AS done_count,
            (SELECT COUNT(*) FROM notes) AS notes
        """
    ).fetchone()
    if row is None:  # Defensive only; aggregate SELECT always produces a row.
        return {"clients": 0, "jobs": 0, "new": 0, "active": 0, "done": 0, "notes": 0}

    return {
        "clients": int(row["clients"]),
        "jobs": int(row["jobs"]),
        "new": int(row["new_count"]),
        "active": int(row["active_count"]),
        "done": int(row["done_count"]),
        "notes": int(row["notes"]),
    }
