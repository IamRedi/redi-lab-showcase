"""Typed repositories: ExperimentRepository + FactoryRunRepository.

Only TWO explicit SQL UPDATE statements are allowed in the entire REDI-02 codebase:
  (1) ExperimentRepository.freeze — sets frozen_ts once
  (2) FactoryRunRepository.close_run — sets closed_at + final columns once

Both implement set-once semantics: calling twice raises AlreadyFrozenError/ClosedRunError.
All other mutations are append-only inserts.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from ..domain import (
    DomainDecodeError,
    ExperimentId,
    ExperimentManifest,
    FactoryRun,
    FactoryRunId,
    GateResult,
    Mode,
    MonetaryCost,
    RunStatus,
    SchemaVersionError,
    Timing,
    TokenUsage,
    domain_from_json,
    domain_to_json,
)
from .exceptions import (
    AlreadyFrozenError,
    ClosedRunError,
    IncompleteEvidenceError,
    MalformedPayloadError,
    NotFrozenError,
)
from .integrity import payload_hash_from_json
from .ledger import EvidenceLedger


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExperimentRepository:
    def __init__(self, conn: sqlite3.Connection, ledger: EvidenceLedger) -> None:
        self._conn = conn
        self._ledger = ledger

    def store_preregistered(self, manifest: ExperimentManifest) -> str:
        if not isinstance(manifest, ExperimentManifest):
            raise TypeError("manifest must be ExperimentManifest")
        payload_json = domain_to_json(manifest)
        sha = payload_hash_from_json(payload_json)
        created = _utcnow_iso()
        prereg = manifest.preregistered_at.isoformat() if manifest.preregistered_at else created
        try:
            self._conn.execute(
                """
                INSERT INTO experiment_manifests (
                    id, title, hypothesis_reference, schema_version, frozen_ts,
                    latest_rev_of_id, payload_json, payload_sha256,
                    preregistered_at, created_at
                ) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)
                """,
                (
                    str(manifest.id),
                    manifest.title,
                    manifest.hypothesis_reference,
                    int(manifest.schema_version),
                    payload_json,
                    sha,
                    prereg,
                    created,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"ExperimentManifest store failed FK/UNIQUE: {exc}") from exc
        self._conn.commit()
        return str(manifest.id)

    def freeze(self, experiment_id: str) -> None:
        now = _utcnow_iso()
        cur = self._conn.execute(
            "UPDATE experiment_manifests SET frozen_ts = ? WHERE id = ? AND frozen_ts IS NULL",
            (now, experiment_id),
        )
        self._conn.commit()
        if cur.rowcount == 0:
            raise AlreadyFrozenError(
                f"ExperimentManifest {experiment_id!r} already frozen or does not exist"
            )

    def _load_row(self, experiment_id: str) -> sqlite3.Row | None:
        self._conn.row_factory = sqlite3.Row
        row = self._conn.execute(
            "SELECT * FROM experiment_manifests WHERE id = ? LIMIT 1",
            (experiment_id,),
        ).fetchone()
        return row

    def read(self, experiment_id: str) -> ExperimentManifest:
        row = self._load_row(experiment_id)
        if row is None:
            raise KeyError(f"ExperimentManifest id={experiment_id!r} not found")
        payload_json = row["payload_json"]
        try:
            obj = domain_from_json(payload_json)
        except (DomainDecodeError, SchemaVersionError) as exc:
            raise MalformedPayloadError(
                f"ExperimentManifest {experiment_id!r} payload decode failed: {exc}"
            ) from exc
        if not isinstance(obj, ExperimentManifest):
            raise MalformedPayloadError(
                f"Stored experiment payload is not ExperimentManifest: {type(obj).__name__}"
            )
        return obj

    def amend(self, original_id: str, revised_manifest: ExperimentManifest) -> str:
        orig_row = self._load_row(original_id)
        if orig_row is None:
            raise KeyError(f"Original experiment {original_id!r} not found")
        if orig_row["frozen_ts"] is None:
            raise NotFrozenError(
                f"ExperimentManifest {original_id!r} must be frozen before amendment"
            )
        latest_rev_of = orig_row["latest_rev_of_id"] or original_id
        payload_json = domain_to_json(revised_manifest)
        sha = payload_hash_from_json(payload_json)
        created = _utcnow_iso()
        prereg = revised_manifest.preregistered_at.isoformat() if revised_manifest.preregistered_at else created
        try:
            self._conn.execute(
                """
                INSERT INTO experiment_manifests (
                    id, title, hypothesis_reference, schema_version, frozen_ts,
                    latest_rev_of_id, payload_json, payload_sha256,
                    preregistered_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(revised_manifest.id),
                    revised_manifest.title,
                    revised_manifest.hypothesis_reference,
                    int(revised_manifest.schema_version),
                    None,  # amendment is not auto-frozen; caller freeze if needed
                    latest_rev_of,
                    payload_json,
                    sha,
                    prereg,
                    created,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"ExperimentManifest amend failed FK/UNIQUE: {exc}") from exc
        self._conn.commit()
        return str(revised_manifest.id)

    def list_revisions(self, experiment_id: str) -> list[ExperimentManifest]:
        self._conn.row_factory = sqlite3.Row
        rows = self._conn.execute(
            """
            SELECT * FROM experiment_manifests
            WHERE id = ? OR latest_rev_of_id = ?
            ORDER BY created_at ASC
            """,
            (experiment_id, experiment_id),
        ).fetchall()
        results: list[ExperimentManifest] = []
        for r in rows:
            obj = domain_from_json(r["payload_json"])
            if isinstance(obj, ExperimentManifest):
                results.append(obj)
        return results


class FactoryRunRepository:
    def __init__(self, conn: sqlite3.Connection, ledger: EvidenceLedger) -> None:
        self._conn = conn
        self._ledger = ledger

    def store(self, run: FactoryRun, *, experiment_id: str | None = None) -> str:
        if not isinstance(run, FactoryRun):
            raise TypeError("run must be FactoryRun")
        if experiment_id is None:
            md = run.metadata if isinstance(run.metadata, dict) else {}
            exp_v = md.get("experiment_id")
            if exp_v is not None:
                experiment_id = str(exp_v)
        payload_json = domain_to_json(run)
        sha = payload_hash_from_json(payload_json)
        timing_json = domain_to_json(run.timing) if run.timing is not None else None
        cost_json = domain_to_json(run.total_cost) if run.total_cost is not None else None
        tokens_json = domain_to_json(run.total_tokens) if run.total_tokens is not None else None
        created = run.created_at.isoformat() if run.created_at else _utcnow_iso()
        try:
            self._conn.execute(
                """
                INSERT INTO factory_runs (
                    id, product_spec_id, dag_id, recipe_id, experiment_id,
                    mode, final_status, verified, timing_json, total_cost_json,
                    total_tokens_json, payload_json, payload_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(run.id),
                    str(run.product_spec_id) if run.product_spec_id else None,
                    run.dag_id,
                    str(run.recipe_id) if run.recipe_id else None,
                    experiment_id,
                    run.mode.value if hasattr(run.mode, 'value') else str(run.mode),
                    run.final_status.value if hasattr(run.final_status, 'value') else str(run.final_status),
                    1 if run.verified else 0,
                    timing_json,
                    cost_json,
                    tokens_json,
                    payload_json,
                    sha,
                    created,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise IncompleteEvidenceError(
                f"FactoryRun store failed (likely FK experiment_id unknown): {exc}"
            ) from exc
        self._conn.commit()
        return str(run.id)

    def close_run(
        self,
        run_id: str,
        final_status: RunStatus,
        verified: bool,
        total_cost: MonetaryCost | None = None,
        total_tokens: TokenUsage | None = None,
        timing: Timing | None = None,
    ) -> None:
        closed_at = _utcnow_iso()
        timing_json = domain_to_json(timing) if timing is not None else None
        cost_json = domain_to_json(total_cost) if total_cost is not None else None
        tokens_json = domain_to_json(total_tokens) if total_tokens is not None else None
        status_str = final_status.value if hasattr(final_status, 'value') else str(final_status)
        cur = self._conn.execute(
            """
            UPDATE factory_runs
            SET final_status = ?,
                verified = ?,
                timing_json = COALESCE(?, timing_json),
                total_cost_json = COALESCE(?, total_cost_json),
                total_tokens_json = COALESCE(?, total_tokens_json),
                closed_at = ?
            WHERE id = ? AND closed_at IS NULL
            """,
            (
                status_str,
                1 if verified else 0,
                timing_json,
                cost_json,
                tokens_json,
                closed_at,
                run_id,
            ),
        )
        self._conn.commit()
        if cur.rowcount == 0:
            raise ClosedRunError(
                f"FactoryRun {run_id!r} already closed or does not exist"
            )

    def read(self, run_id: str) -> FactoryRun:
        self._conn.row_factory = sqlite3.Row
        row = self._conn.execute(
            "SELECT * FROM factory_runs WHERE id = ? LIMIT 1", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"FactoryRun {run_id!r} not found")
        try:
            obj = domain_from_json(row["payload_json"])
        except (DomainDecodeError, SchemaVersionError) as exc:
            raise MalformedPayloadError(
                f"FactoryRun {run_id!r} payload decode failed: {exc}"
            ) from exc
        if not isinstance(obj, FactoryRun):
            raise MalformedPayloadError(
                f"Stored factory_run payload is not FactoryRun: {type(obj).__name__}"
            )
        return obj

    def list_by_experiment(self, experiment_id: str) -> list[FactoryRun]:
        self._conn.row_factory = sqlite3.Row
        rows = self._conn.execute(
            "SELECT * FROM factory_runs WHERE experiment_id = ? ORDER BY created_at ASC",
            (experiment_id,),
        ).fetchall()
        out: list[FactoryRun] = []
        for r in rows:
            obj = domain_from_json(r["payload_json"])
            if isinstance(obj, FactoryRun):
                out.append(obj)
        return out


__all__ = ["ExperimentRepository", "FactoryRunRepository"]
