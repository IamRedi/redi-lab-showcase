"""Core service API for the Mini CRM / Job Manager."""

from __future__ import annotations

import sqlite3
from os import PathLike
from typing import Any, Iterable

from .database import connect, dashboard_counts, initialize_database
from .dataflow import normalize_status


class CRMService:
    """A small, transactional service layer over the Mini CRM SQLite schema.

    ``database`` may be a database filename or an already-open SQLite
    connection.  Returned clients, jobs, and notes are ordinary dictionaries,
    making the service convenient for both a UI and a simple local API.
    """

    def __init__(
        self, database: str | PathLike[str] | sqlite3.Connection = "mini_crm.sqlite3"
    ) -> None:
        self._owns_connection = not isinstance(database, sqlite3.Connection)
        self.connection = connect(database) if self._owns_connection else database
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        initialize_database(self.connection)

    def close(self) -> None:
        """Close a connection opened by this service."""
        if self._owns_connection:
            self.connection.close()

    def __enter__(self) -> "CRMService":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    @staticmethod
    def _required_text(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} is required")
        return value.strip()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def create_client(
        self, name: str, email: str | None = None, phone: str | None = None
    ) -> dict[str, Any]:
        """Create and return a client."""
        name = self._required_text(name, "name")
        if email is not None and not isinstance(email, str):
            raise ValueError("email must be a string or None")
        if phone is not None and not isinstance(phone, str):
            raise ValueError("phone must be a string or None")
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO clients (name, email, phone) VALUES (?, ?, ?)",
                (name, email, phone),
            )
        return self.client(cursor.lastrowid)

    def client(self, client_id: int) -> dict[str, Any]:
        """Return one client, or raise ``ValueError`` when it does not exist."""
        row = self.connection.execute(
            "SELECT id, name, email, phone, created_at FROM clients WHERE id = ?",
            (client_id,),
        ).fetchone()
        result = self._row(row)
        if result is None:
            raise ValueError(f"Unknown client: {client_id}")
        return result

    def create_job(
        self,
        client_id: int,
        title: str,
        description: str = "",
        notes: str | Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Create a NEW job and optionally attach one or more initial notes."""
        self.client(client_id)  # Produce a useful error before attempting INSERT.
        title = self._required_text(title, "title")
        if not isinstance(description, str):
            raise ValueError("description must be a string")
        note_bodies = self._note_bodies(notes)
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO jobs (client_id, title, description, status) VALUES (?, ?, ?, 'NEW')",
                (client_id, title, description),
            )
            job_id = cursor.lastrowid
            self.connection.executemany(
                "INSERT INTO notes (job_id, body) VALUES (?, ?)",
                ((job_id, body) for body in note_bodies),
            )
        return self.job(job_id)

    @staticmethod
    def _note_bodies(notes: str | Iterable[str] | None) -> list[str]:
        if notes is None:
            return []
        values = [notes] if isinstance(notes, str) else list(notes)
        bodies: list[str] = []
        for body in values:
            if not isinstance(body, str) or not body.strip():
                raise ValueError("note body is required")
            bodies.append(body.strip())
        return bodies

    def job(self, job_id: int) -> dict[str, Any]:
        """Return one job, including its client name, or raise ``ValueError``."""
        row = self.connection.execute(
            """
            SELECT j.id, j.client_id, c.name AS client_name, j.title,
                   j.description, j.status, j.created_at, j.updated_at
              FROM jobs AS j
              JOIN clients AS c ON c.id = j.client_id
             WHERE j.id = ?
            """,
            (job_id,),
        ).fetchone()
        result = self._row(row)
        if result is None:
            raise ValueError(f"Unknown job: {job_id}")
        return result

    def jobs(
        self, client_id: int | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        """List jobs, optionally limited to a client and/or status."""
        clauses: list[str] = []
        values: list[Any] = []
        if client_id is not None:
            clauses.append("j.client_id = ?")
            values.append(client_id)
        if status is not None:
            clauses.append("j.status = ?")
            values.append(normalize_status(status))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            """
            SELECT j.id, j.client_id, c.name AS client_name, j.title,
                   j.description, j.status, j.created_at, j.updated_at
              FROM jobs AS j JOIN clients AS c ON c.id = j.client_id
            "" + where + " ORDER BY j.id",
            values,
        ).fetchall()
        return [dict(row) for row in rows]

    def set_status(self, job_id: int, status: str) -> dict[str, Any]:
        """Set a job status after canonical status validation."""
        status = normalize_status(status)
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE jobs SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, job_id),
            )
        if cursor.rowcount == 0:
            raise ValueError(f"Unknown job: {job_id}")
        return self.job(job_id)

    def add_note(self, job_id: int, body: str) -> dict[str, Any]:
        """Add and return a note for an existing job."""
        self.job(job_id)
        body = self._note_bodies(body)[0]
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO notes (job_id, body) VALUES (?, ?)", (job_id, body)
            )
        row = self.connection.execute(
            "SELECT id, job_id, body, created_at FROM notes WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
        return dict(row)

    def notes(self, job_id: int) -> list[dict[str, Any]]:
        """List a job's notes in creation order."""
        self.job(job_id)
        rows = self.connection.execute(
            "SELECT id, job_id, body, created_at FROM notes WHERE job_id = ? ORDER BY id",
            (job_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def dashboard(self) -> dict[str, int]:
        """Return total client and per-status job counts."""
        return dashboard_counts(self.connection)
