"""SQLite storage helpers for the Mini CRM / Job Manager."""

import sqlite3
from os import PathLike
from typing import Dict, Union


DEFAULT_DATABASE_PATH = "mini_crm.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
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

DatabaseTarget = Union[str, bytes, PathLike, sqlite3.Connection]


def connect(database_path: Union[str, bytes, PathLike] = DEFAULT_DATABASE_PATH) -> sqlite3.Connection:
    """Open a SQLite connection configured for this application's schema."""
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(database: DatabaseTarget = DEFAULT_DATABASE_PATH) -> sqlite3.Connection:
    """Create all Mini CRM tables and return an initialized connection.

    Supplying an existing connection leaves ownership of that connection with
    the caller.  Supplying a path opens and returns a new connection.
    """
    if isinstance(database, sqlite3.Connection):
        connection = database
        connection.execute("PRAGMA foreign_keys = ON")
    else:
        connection = connect(database)

    connection.executescript(SCHEMA)
    connection.commit()
    return connection


def init_db(database: DatabaseTarget = DEFAULT_DATABASE_PATH) -> sqlite3.Connection:
    """Backward-friendly alias for :func:`initialize_database`."""
    return initialize_database(database)


def dashboard_counts(database: DatabaseTarget) -> Dict[str, int]:
    """Return the exact set of counts displayed by the dashboard.

    The argument may be an initialized SQLite connection or a database path.
    When a path is supplied, the temporary connection is closed before this
    function returns.
    """
    close_when_done = not isinstance(database, sqlite3.Connection)
    connection = connect(database) if close_when_done else database

    try:
        if not close_when_done:
            connection.execute("PRAGMA foreign_keys = ON")

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
            "jobs_total": int(row["jobs_total"]),
            "NEW": int(row["new_count"]),
            "ACTIVE": int(row["active_count"]),
            "DONE": int(row["done_count"]),
            "clients_total": int(clients_total),
        }
    finally:
        if close_when_done:
            connection.close()
