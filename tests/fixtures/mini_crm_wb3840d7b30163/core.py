"""Application service layer for the Mini CRM / Job Manager."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Optional, Union

from .database import DATABASE_PATH, dashboard_counts, get_connection, init_db
from .dataflow import can_transition, normalize_status


class CRMService:
    """Small transactional service API over the Mini CRM SQLite database."""

    def __init__(self, database_path: Union[str, Path] = DATABASE_PATH) -> None:
        self.database_path = database_path
        init_db(self.database_path)

    def _connection(self):
        return get_connection(self.database_path)

    @staticmethod
    def _required_text(value: Any, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("{} is required".format(field_name))
        return value.strip()

    @staticmethod
    def _note_values(notes: Optional[Union[str, Iterable[str]]]) -> list[str]:
        if notes is None:
            return []
        if isinstance(notes, str):
            values = [notes]
        else:
            try:
                values = list(notes)
            except TypeError as error:
                raise ValueError("notes must be a string or an iterable of strings") from error

        result = []
        for note in values:
            if not isinstance(note, str) or not note.strip():
                raise ValueError("note content must be non-empty text")
            result.append(note.strip())
        return result

    def create_client(
        self, name: str, email: Optional[str] = None, phone: Optional[str] = None
    ) -> int:
        """Create a client and return its identifier."""
        name = self._required_text(name, "name")
        connection = self._connection()
        try:
            cursor = connection.execute(
                "INSERT INTO clients (name, email, phone) VALUES (?, ?, ?)",
                (name, email, phone),
            )
            connection.commit()
            return int(cursor.lastrowid)
        finally:
            connection.close()

    def create_job(
        self,
        client_id: int,
        title: str,
        description: Optional[str] = None,
        notes: Optional[Union[str, Iterable[str]]] = None,
    ) -> int:
        """Create a NEW job for a client, optionally creating its notes too."""
        title = self._required_text(title, "title")
        note_values = self._note_values(notes)
        connection = self._connection()
        try:
            client = connection.execute(
                "SELECT 1 FROM clients WHERE id = ?", (client_id,)
            ).fetchone()
            if client is None:
                raise ValueError("Client {} does not exist".format(client_id))

            cursor = connection.execute(
                """
                INSERT INTO jobs (client_id, title, description, status)
                VALUES (?, ?, ?, 'NEW')
                """,
                (client_id, title, description),
            )
            job_id = int(cursor.lastrowid)
            connection.executemany(
                "INSERT INTO notes (job_id, content) VALUES (?, ?)",
                [(job_id, content) for content in note_values],
            )
            connection.commit()
            return job_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def set_status(self, job_id: int, status: str) -> None:
        """Move a job through its allowed NEW -> ACTIVE -> DONE workflow."""
        new_status = normalize_status(status)
        connection = self._connection()
        try:
            row = connection.execute(
                "SELECT status FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Job {} does not exist".format(job_id))
            if not can_transition(row["status"], new_status):
                raise ValueError(
                    "Invalid status transition: {} to {}".format(
                        row["status"], new_status
                    )
                )
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (new_status, job_id),
            )
            connection.commit()
        finally:
            connection.close()

    def dashboard(self) -> dict[str, int]:
        """Return aggregate client, job, status, and note counts."""
        connection = self._connection()
        try:
            return dashboard_counts(connection)
        finally:
            connection.close()

    def jobs(
        self, client_id: Optional[int] = None, status: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Return jobs, including their client name and notes.

        Optional client and status filters are useful to both a local UI and
        callers wishing to display a focused job list.
        """
        clauses: list[str] = []
        parameters: list[Any] = []
        if client_id is not None:
            clauses.append("j.client_id = ?")
            parameters.append(client_id)
        if status is not None:
            clauses.append("j.status = ?")
            parameters.append(normalize_status(status))

        query = """
            SELECT j.id, j.client_id, j.title, j.description, j.status,
                   j.created_at, j.updated_at, c.name AS client_name
            FROM jobs AS j
            JOIN clients AS c ON c.id = j.client_id
        """
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY j.id DESC"

        connection = self._connection()
        try:
            result = []
            for row in connection.execute(query, parameters).fetchall():
                job = dict(row)
                notes = connection.execute(
                    """
                    SELECT id, job_id, content, created_at
                    FROM notes WHERE job_id = ? ORDER BY id ASC
                    """,
                    (job["id"],),
                ).fetchall()
                job["notes"] = [dict(note) for note in notes]
                result.append(job)
            return result
        finally:
            connection.close()
