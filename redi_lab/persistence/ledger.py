"""Append-only Evidence Ledger master table.

All writes go through EvidenceLedger.append; no update/delete methods exist.
Duplicate policy: explicit rejection → DuplicateLedgerEntryError on UNIQUE constraint.
"""

from __future__ import annotations

import sqlite3
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, NotRequired, Sequence, TypedDict

from ..domain import domain_from_json, domain_to_json
from .exceptions import DuplicateLedgerEntryError, MalformedPayloadError
from .integrity import compute_prev_hash, payload_hash_from_json


class AppendRequest(TypedDict):
    entity: Any
    factory_run_id: NotRequired[str | None]
    experiment_id: NotRequired[str | None]
    correction_of_entry_id: NotRequired[int | None]
    recorded_at: NotRequired[str | None]


_ALLOWED_ENTITY_TYPES: frozenset[str] = frozenset(
    {
        "RunEvidence",
        "LifecycleEvent",
        "FailureRecord",
        "GateResult",
        "EnvironmentInfo",
        "PricingEvidence",
    }
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EvidenceLedger:
    """Append-only master ledger (evidence_entries table).

    Constructor accepts an already-bootstrapped sqlite3 Connection.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._entity_type_cache: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Write API — append only
    # ------------------------------------------------------------------

    def _append_insert(
        self,
        entity: Any,
        *,
        factory_run_id: str | None,
        experiment_id: str | None,
        correction_of_entry_id: int | None,
        recorded_at: str | None,
    ) -> tuple[Any, str, str, str | None, str]:
        """Internal insert without commit. Returns (cur, entity_id, entity_type, prev_hash, ts)."""
        if not is_dataclass(entity):
            raise TypeError(f"entity must be dataclass domain contract, got {type(entity).__name__}")
        entity_type = type(entity).__name__
        if entity_type not in _ALLOWED_ENTITY_TYPES:
            raise ValueError(
                f"entity_type {entity_type!r} not allowed in master ledger"
            )
        try:
            payload_json = domain_to_json(entity)
        except Exception as exc:  # noqa: BLE001 — wrap all serialization errors explicitly
            raise MalformedPayloadError(
                f"entity {entity_type} failed canonical serialization: {exc}"
            ) from exc
        payload_sha256 = payload_hash_from_json(payload_json)
        try:
            entity_id = str(getattr(entity, "id"))
        except AttributeError as exc:
            raise ValueError(f"entity {entity_type!r} has no 'id' field") from exc
        ts = recorded_at if recorded_at is not None else _utcnow_iso()

        prev = self._conn.execute(
            "SELECT global_sequence, payload_sha256 FROM evidence_entries "
            "ORDER BY global_sequence DESC LIMIT 1"
        ).fetchone()
        if prev is None:
            prev_hash: str | None = None
        else:
            prev_hash = compute_prev_hash(int(prev[0]), str(prev[1]))

        try:
            cur = self._conn.execute(
                """
                INSERT INTO evidence_entries (
                    global_sequence, entity_id, entity_type, factory_run_id,
                    experiment_id, payload_json, payload_sha256, prev_hash,
                    correction_of_entry_id, recorded_at
                ) VALUES (
                    (SELECT COALESCE(MAX(global_sequence), 0) + 1 FROM evidence_entries),
                    ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    entity_id,
                    entity_type,
                    factory_run_id,
                    experiment_id,
                    payload_json,
                    payload_sha256,
                    prev_hash,
                    correction_of_entry_id,
                    ts,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateLedgerEntryError(
                f"Duplicate ledger entity_id={entity_id!r} or payload_sha256: {exc}"
            ) from exc
        return cur, entity_id, entity_type, prev_hash, ts, payload_sha256  # type: ignore[return-value]

    def _result_from_cur(self, cur, entity_id, entity_type, prev_hash, ts, payload_sha256) -> dict[str, Any]:
        entry_id = cur.lastrowid
        seq_row = self._conn.execute(
            "SELECT global_sequence FROM evidence_entries WHERE entry_id = ?", (entry_id,)
        ).fetchone()
        global_seq = int(seq_row[0]) if seq_row else 0
        return {
            "entry_id": entry_id,
            "global_sequence": global_seq,
            "entity_id": entity_id,
            "entity_type": entity_type,
            "payload_sha256": payload_sha256,
            "prev_hash": prev_hash,
            "recorded_at": ts,
        }

    def append(
        self,
        entity: Any,
        *,
        factory_run_id: str | None = None,
        experiment_id: str | None = None,
        correction_of_entry_id: int | None = None,
        recorded_at: str | None = None,
    ) -> dict[str, Any]:
        cur, entity_id, entity_type, prev_hash, ts, payload_sha256 = self._append_insert(
            entity,
            factory_run_id=factory_run_id,
            experiment_id=experiment_id,
            correction_of_entry_id=correction_of_entry_id,
            recorded_at=recorded_at,
        )
        self._conn.commit()
        return self._result_from_cur(cur, entity_id, entity_type, prev_hash, ts, payload_sha256)

    def append_batch(self, items: Sequence[AppendRequest]) -> list[dict[str, Any]]:
        """Append multiple items as a single transaction; any failure rolls back all."""
        results: list[dict[str, Any]] = []
        self._conn.execute("BEGIN")
        try:
            for req in items:
                cur, entity_id, entity_type, prev_hash, ts, payload_sha256 = self._append_insert(
                    req["entity"],
                    factory_run_id=req.get("factory_run_id"),
                    experiment_id=req.get("experiment_id"),
                    correction_of_entry_id=req.get("correction_of_entry_id"),
                    recorded_at=req.get("recorded_at"),
                )
                results.append(self._result_from_cur(cur, entity_id, entity_type, prev_hash, ts, payload_sha256))
        except Exception:
            self._conn.rollback()
            raise
        self._conn.commit()
        return results

    # ------------------------------------------------------------------
    # Read API — query only
    # ------------------------------------------------------------------

    def _row_to_dict(self, row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
        if isinstance(row, sqlite3.Row):
            return dict(row)
        cols = [d[0] for d in getattr(self._conn.execute('SELECT * FROM evidence_entries LIMIT 0').description, '__iter__', lambda: [])()]  # type: ignore[attr-defined]
        # Fallback: return positional tuple with keys in column order (used in tests rarely; prefer sqlite3.Row)
        return {c: v for c, v in zip(cols, row)}

    def query_all(self, order: str = "ASC") -> list[dict[str, Any]]:
        if order.upper() not in ("ASC", "DESC"):
            raise ValueError("order must be 'ASC' or 'DESC'")
        cur = self._conn.execute(
            f"SELECT * FROM evidence_entries ORDER BY global_sequence {order.upper()}"
        )
        return [dict(r) for r in cur.fetchall()]

    def query_by_run(self, run_id: str) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM evidence_entries WHERE factory_run_id = ? ORDER BY global_sequence ASC",
            (run_id,),
        )
        return [dict(r) for r in cur.fetchall()]

    def query_by_experiment(self, exp_id: str) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM evidence_entries WHERE experiment_id = ? ORDER BY global_sequence ASC",
            (exp_id,),
        )
        return [dict(r) for r in cur.fetchall()]

    def query_by_entity_type(self, entity_type: str) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM evidence_entries WHERE entity_type = ? ORDER BY global_sequence ASC",
            (entity_type,),
        )
        return [dict(r) for r in cur.fetchall()]

    def query_by_entity_id(self, entity_id: str) -> dict[str, Any] | None:
        cur = self._conn.execute(
            "SELECT * FROM evidence_entries WHERE entity_id = ? LIMIT 1", (entity_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def count(self) -> int:
        return int(
            self._conn.execute("SELECT COUNT(*) FROM evidence_entries").fetchone()[0]
        )


__all__ = ["AppendRequest", "EvidenceLedger"]
