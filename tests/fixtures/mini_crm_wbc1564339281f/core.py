"""Application service layer for the Mini CRM / Job Manager.

``CRMService`` owns a SQLite connection (unless one is supplied) and exposes
small, UI-friendly operations as ordinary dictionaries.
"""

from __future__ import annotations

import sqlite3
from os import PathLike
from typing import Any, Iterable, Union

from .database import DatabaseTarget, initialize_database
from .dataflow import normalize_status


class CRMService:
    """Service API for clients, jobs, notes, and dashboard data."""

    def __init__(
        self, database: DatabaseTarget = "mini_crm.db"
    ) -> None:
        self._connection = initialize_database(database)
        self._closed = False

    @property
    def connection(self) -> sqlite3.Connection:
        """The initialized SQLite connection used by this service."""
        return self._connection

    def close(self) -> None:
        """Close the service connection.

        Calling this method more than once is harmless.  A supplied connection
        is closed too, because the service is no longer usable afterwards.
        """
        if not self._closed:
            self._connection.close()
            self._closed = True

    def __enter__(self) -> "CRMService":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    @staticmethod
    def _required_text(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string")
        return value.strip()

    @staticmethod
    def _optional_text(value: Any, field: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"{field} must be a string or None")
        return value.strip() or None

    @staticmethod
    def _id(value: Any, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{field} must be a positive integer")
        return value

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("CRMService is closed")

    def _client_exists(self, client_id: int) -> None:
        row = self._connection.execute(
            "SELECT 1 FROM clients WHERE id = ?", (client_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Client {client_id} does not exist")

    def _job_exists(self, job_id: int) -> None:
        row = self._connection.execute(
            "SELECT 1 FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Job {job_id} does not exist")

    def create_client(
        self, name: str, email: str | None = None, phone: str | None = None
    ) -> dict[str, Any]:
        """Create and return a client."""
        self._ensure_open()
        name = self._required_text(name, "name")
        email = self._optional_text(email, "email")
        phone = self._optional_text(phone, "phone")
        cursor = self._connection.execute(
            "INSERT INTO clients (name, email, phone) VALUES (?, ?, ?)",
            (name, email, phone),
        )
        self._connection.commit()
        row = self._connection.execute(
            "SELECT id, name, email, phone, created_at FROM clients WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
        return dict(zip(("id", "name", "email", "phone", "created_at"), row))

    @staticmethod
    def _note_values(notes: str | Iterable[str] | None) -> list[str]:
        if notes is None:
            return []
        values = [notes] if isinstance(notes, str) else list(notes)
        result: list[str] = []
        for note in values:
            if not isinstance(note, str) or not note.strip():
                raise ValueError("each note must be a non-empty string")
            result.append(note.strip())
        return result

    def create_job(
        self,
        client_id: int,
        title: str,
        description: str | None = None,
        notes: str | Iterable[str] | None = None,
        status: str = "NEW",
    ) -> dict[str, Any]:
        """Create a job and optional initial notes.

        Jobs are NEW by default; an explicitly supplied status is normalized in
        the same way as status updates.
        """
        self._ensure_open()
        client_id = self._id(client_id, "client_id")
        title = self._required_text(title, "title")
        description = self._optional_text(description, "description")
        status = normalize_status(status)
        note_values = self._note_values(notes)
        self._client_exists(client_id)
        with self._connection:
            cursor = self._connection.execute(
                "INSERT INTO jobs (client_id, title, description, status) VALUES (?, ?, ?, ?)",
                (client_id, title, description, status),
            )
            job_id = int(cursor.lastrowid)
            self._connection.executemany(
                "INSERT INTO notes (job_id, content) VALUES (?, ?)",
                ((job_id, content) for content in note_values),
            )
        return self._job(job_id)

    def set_status(self, job_id: int, status: str) -> dict[str, Any]:
        """Set a job status after canonical validation by ``normalize_status``."""
        self._ensure_open()
        job_id = self._id(job_id, "job_id")
        status = normalize_status(status)
        self._job_exists(job_id)
        with self._connection:
            self._connection.execute(
                "UPDATE jobs SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, job_id),
            )
        return self._job(job_id)

    def _job(self, job_id: int) -> dict[str, Any]:
        rows = self.jobs()
        for job in rows:
            if job["id"] == job_id:
                return job
        raise ValueError(f"Job {job_id} does not exist")

    def jobs(
        self, client_id: int | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        """Return jobs, including their client details and ordered notes.

        Optional client and status filters are validated before querying.
        """
        self._ensure_open()
        clauses: list[str] = []
        parameters: list[Any] = []
        if client_id is not None:
            clauses.append("j.client_id = ?")
            parameters.append(self._id(client_id, "client_id"))
        if status is not None:
            clauses.append("j.status = ?")
            parameters.append(normalize_status(status))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self._connection.execute(
            "SELECT j.id, j.client_id, j.title, j.description, j.status, "
            "j.created_at, j.updated_at, c.name, c.email, c.phone "
            "FROM jobs AS j JOIN clients AS c ON c.id = j.client_id" + where +
            " ORDER BY j.id",
            parameters,
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            job = dict(zip(
                ("id", "client_id", "title", "description", "status", "created_at", "updated_at", "client_name", "client_email", "client_phone"),
                row,
            ))
            note_rows = self._connection.execute(
                "SELECT id, content, created_at FROM notes WHERE job_id = ? ORDER BY id",
                (job["id"],),
            ).fetchall()
            job["notes"] = [
                dict(zip(("id", "content", "created_at"), note)) for note in note_rows
            ]
            result.append(job)
        return result

    def dashboard(self) -> dict[str, int]:
        """Return dashboard aggregate counts."""
        self._ensure_open()
        row = self._connection.execute(
            "SELECT COUNT(*), "
            "COALESCE(SUM(status = 'NEW'), 0), "
            "COALESCE(SUM(status = 'ACTIVE'), 0), "
            "COALESCE(SUM(status = 'DONE'), 0) FROM jobs"
        ).fetchone()
        clients_total = self._connection.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
        return {
            "jobs_total": int(row[0]),
            "NEW": int(row[1]),
            "ACTIVE": int(row[2]),
            "DONE": int(row[3]),
            "clients_total": int(clients_total),
        }
