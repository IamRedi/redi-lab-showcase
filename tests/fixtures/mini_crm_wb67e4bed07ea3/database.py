"""SQLite persistence helpers for the Mini CRM / Job Manager.

This module deliberately uses only the Python standard library.  Call
``initialize_database`` once after opening a connection, then use the same
connection (or another connection initialized in the same way) for CRUD
operations.
"""

from __future__ import annotations

import sqlite3
from os import PathLike
from typing import Union


VALID_JOB_STATUSES = ("NEW", "ACTIVE", "DONE")


SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    email       TEXT,
    phone       TEXT,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY,
    client_id   INTEGER NOT NULL,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'NEW'
                    CHECK (status IN ('NEW', 'ACTIVE', 'DONE')),
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY,
    job_id      INTEGER NOT NULL,
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_jobs_client_id ON jobs(client_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_notes_job_id ON notes(job_id);
"""


DatabaseTarget = Union[str, PathLike[str], sqlite3.Connection]


def connect(database: Union[str, PathLike[str]] = "mini_crm.sqlite3") -> sqlite3.Connection:
    """Open a SQLite database connection with foreign-key enforcement enabled."""
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(connection: sqlite3.Connection) -> None:
    """Create the Mini CRM tables and indexes if they do not already exist."""
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA)


# Short aliases are useful to callers that use conventional database helper names.
initialize = initialize_database
init_db = initialize_database


def dashboard_counts(connection: sqlite3.Connection) -> dict[str, int]:
    """Return the five dashboard counters, with exactly the documented keys."""
    row = connection.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM jobs) AS jobs_total,
            (SELECT COUNT(*) FROM jobs WHERE status = 'NEW') AS new_count,
            (SELECT COUNT(*) FROM jobs WHERE status = 'ACTIVE') AS active_count,
            (SELECT COUNT(*) FROM jobs WHERE status = 'DONE') AS done_count,
            (SELECT COUNT(*) FROM clients) AS clients_total
        """
    ).fetchone()

    # COUNT always produces one row, but keeping this fallback makes the helper
    # well-defined even for non-standard SQLite connection wrappers.
    if row is None:
        return {
            "jobs_total": 0,
            "NEW": 0,
            "ACTIVE": 0,
            "DONE": 0,
            "clients_total": 0,
        }

    return {
        "jobs_total": int(row[0]),
        "NEW": int(row[1]),
        "ACTIVE": int(row[2]),
        "DONE": int(row[3]),
        "clients_total": int(row[4]),
    }
