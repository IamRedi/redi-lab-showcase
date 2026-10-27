"""Application service layer for the Mini CRM / Job Manager."""

import sqlite3
from os import PathLike
from typing import Any, Dict, Iterable, List, Optional, Union

from .database import DEFAULT_DATABASE_PATH, dashboard_counts, initialize_database
from .dataflow import normalize_status


DatabaseTarget = Union[str, bytes, PathLike, sqlite3.Connection]


class CRMService:
    """A small, database-backed API for clients, jobs, and job notes."""

    def __init__(self, database: DatabaseTarget = DEFAULT_DATABASE_PATH):
        self._owns_connection = not isinstance(database, sqlite3.Connection)
        self.connection = initialize_database(database)

    def close(self) -> None:
        """Close a connection opened by this service.

        Connections supplied by callers remain caller-owned and are not closed.
        """
        if self._owns_connection and self.connection is not None:
            self.connection.close()
            self.connection = None

    def __enter__(self) -> "CRMService":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    @staticmethod
    def _required_text(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} is required")
        return value.strip()

    @staticmethod
    def _optional_text(value: Any, field: str) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError(f"{field} must be text")
        return value

    def _client_exists(self, client_id: int) -> None:
        if self.connection.execute(
            "SELECT 1 FROM clients WHERE id = ?", (client_id,)
        ).fetchone() is None:
            raise ValueError(f"Client {client_id} does not exist")

    def _job_exists(self, job_id: int) -> None:
        if self.connection.execute(
            "SELECT 1 FROM jobs WHERE id = ?", (job_id,)
        ).fetchone() is None:
            raise ValueError(f"Job {job_id} does not exist")

    def create_client(
        self,
        name: str,
        email: str = "",
        phone: str = "",
        address: str = "",
    ) -> Dict[str, Any]:
        """Create and return a client."""
        cursor = self.connection.execute(
            "INSERT INTO clients (name, email, phone, address) VALUES (?, ?, ?, ?)",
            (
                self._required_text(name, "name"),
                self._optional_text(email, "email"),
                self._optional_text(phone, "phone"),
                self._optional_text(address, "address"),
            ),
        )
        self.connection.commit()
        return self.client(cursor.lastrowid)

    def client(self, client_id: int) -> Dict[str, Any]:
        """Return one client, or raise ValueError when it does not exist."""
        row = self.connection.execute(
            "SELECT id, name, email, phone, address, created_at FROM clients WHERE id = ?",
            (client_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Client {client_id} does not exist")
        return dict(row)

    def create_job(
        self,
        client_id: int,
        title: str,
        description: str = "",
        notes: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        """Create a NEW job for a client and optionally attach initial notes."""
        self._client_exists(client_id)
        if notes is None:
            note_values: List[str] = []
        elif isinstance(notes, str):
            note_values = [notes]
        else:
            note_values = list(notes)

        cleaned_notes = [self._required_text(note, "note") for note in note_values]
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO jobs (client_id, title, description, status)
                VALUES (?, ?, ?, 'NEW')
                """,
                (
                    client_id,
                    self._required_text(title, "title"),
                    self._optional_text(description, "description"),
                ),
            )
            job_id = cursor.lastrowid
            for content in cleaned_notes:
                self.connection.execute(
                    "INSERT INTO notes (job_id, content) VALUES (?, ?)",
                    (job_id, content),
                )
        return self.job(job_id)

    def set_status(self, job_id: int, status: str) -> Dict[str, Any]:
        """Set a job status after canonical validation by normalize_status."""
        normalized = normalize_status(status)
        self._job_exists(job_id)
        self.connection.execute(
            "UPDATE jobs SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (normalized, job_id),
        )
        self.connection.commit()
        return self.job(job_id)

    def add_note(self, job_id: int, content: str) -> Dict[str, Any]:
        """Add and return a note for an existing job."""
        self._job_exists(job_id)
        cursor = self.connection.execute(
            "INSERT INTO notes (job_id, content) VALUES (?, ?)",
            (job_id, self._required_text(content, "note")),
        )
        self.connection.commit()
        row = self.connection.execute(
            "SELECT id, job_id, content, created_at FROM notes WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
        return dict(row)

    def notes(self, job_id: int) -> List[Dict[str, Any]]:
        """List a job's notes in creation order."""
        self._job_exists(job_id)
        rows = self.connection.execute(
            "SELECT id, job_id, content, created_at FROM notes WHERE job_id = ? ORDER BY id",
            (job_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def job(self, job_id: int) -> Dict[str, Any]:
        """Return one job, including its client name and notes."""
        row = self.connection.execute(
            """
            SELECT j.id, j.client_id, c.name AS client_name, j.title, j.description,
                   j.status, j.created_at, j.updated_at
            FROM jobs AS j JOIN clients AS c ON c.id = j.client_id
            WHERE j.id = ?
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Job {job_id} does not exist")
        result = dict(row)
        result["notes"] = self.notes(job_id)
        return result

    def jobs(
        self, client_id: Optional[int] = None, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """List jobs, optionally filtered by client and/or status."""
        clauses: List[str] = []
        values: List[Any] = []
        if client_id is not None:
            clauses.append("j.client_id = ?")
            values.append(client_id)
        if status is not None:
            clauses.append("j.status = ?")
            values.append(normalize_status(status))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            """
            SELECT j.id, j.client_id, c.name AS client_name, j.title, j.description,
                   j.status, j.created_at, j.updated_at
            FROM jobs AS j JOIN clients AS c ON c.id = j.client_id
            "" + where + " ORDER BY j.id DESC",
            values,
        ).fetchall()
        return [dict(row) for row in rows]

    def dashboard(self) -> Dict[str, int]:
        """Return the dashboard's client and job status counts."""
        return dashboard_counts(self.connection)
