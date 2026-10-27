"""SQLite storage helpers for the Mini CRM / Job Manager."""

from __future__ import annotations

import sqlite3
from os import PathLike
from typing import Union

DatabaseTarget = Union[str, PathLike[str], sqlite3.Connection]

SCHEMA_SQL = """
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


def get_connection(database: Union[str, PathLike[str]] = "mini_crm.db") -> sqlite3.Connection:
    """Open a database connection with SQLite foreign-key enforcement enabled."""
    connection = sqlite3.connect(str(database))
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _as_connection(target: DatabaseTarget) -> tuple[sqlite3.Connection, bool]:
    if isinstance(target, sqlite3.Connection):
        target.execute("PRAGMA foreign_keys = ON")
        return target, False
    return get_connection(target), True


def initialize_database(database: DatabaseTarget = "mini_crm.db") -> sqlite3.Connection:
    """Create the Mini CRM schema if it does not already exist.

    When passed a connection, that same connection is returned.  When passed a
    path, a newly opened initialized connection is returned to the caller.
    """
    connection, _ = _as_connection(database)
    connection.executescript(SCHEMA_SQL)
    connection.commit()
    return connection


def init_db(database: DatabaseTarget = "mini_crm.db") -> sqlite3.Connection:
    """Compatibility alias for :func:`initialize_database`."""
    return initialize_database(database)


def create_tables(database: DatabaseTarget = "mini_crm.db") -> sqlite3.Connection:
    """Compatibility alias for :func:`initialize_database`."""
    return initialize_database(database)


def dashboard_counts(database: DatabaseTarget = "mini_crm.db") -> dict[str, int]:
    """Return the exact set of aggregate counts used by the dashboard."""
    connection, should_close = _as_connection(database)
    try:
        row = connection.execute(
            """
            SELECT
                COUNT(*) AS jobs_total,
                COALESCE(SUM(CASE WHEN status = 'NEW' THEN 1 ELSE 0 END), 0) AS new_count,
                COALESCE(SUM(CASE WHEN status = 'ACTIVE' THEN 1 ELSE 0 END), 0) AS active_count,
                COALESCE(SUM(CASE WHEN status = 'DONE' THEN 1 ELSE 0 END), 0) AS done_count
            FROM jobs
            """
        ).fetchone()
        clients_total = connection.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
        return {
            "jobs_total": int(row[0]),
            "NEW": int(row[1]),
            "ACTIVE": int(row[2]),
            "DONE": int(row[3]),
            "clients_total": int(clients_total),
        }
    finally:
        if should_close:
            connection.close()


class Database:
    """Small convenience wrapper around the Mini CRM SQLite database."""

    def __init__(self, path: Union[str, PathLike[str]] = "mini_crm.db") -> None:
        self.path = str(path)

    def connect(self) -> sqlite3.Connection:
        return get_connection(self.path)

    def initialize(self) -> None:
        connection = initialize_database(self.path)
        connection.close()

    def dashboard_counts(self) -> dict[str, int]:
        return dashboard_counts(self.path)
