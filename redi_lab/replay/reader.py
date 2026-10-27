"""Deterministic read-only ReplayReader.

Reconstructs research state purely from immutable persisted evidence.
Zero external calls; zero writes.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Union

from ..domain import (
    DomainDecodeError,
    ExperimentManifest,
    FactoryRun,
    FactoryRunId,
    FailureCategory,
    FailureRecord,
    GateResult,
    GateStatus,
    LifecycleEvent,
    LifecycleEventType,
    RunEvidence,
    RunStatus,
    SchemaVersionError,
    domain_from_json,
)
from ..persistence.exceptions import (
    IncompleteEvidenceError,
    MalformedPayloadError,
)
from ..persistence.integrity import payload_hash_from_json, verify_chain
from .result import ReplayResult


def _row_to_event(row: sqlite3.Row) -> Any:
    payload = row["payload_json"]
    decoded = domain_from_json(payload)
    return decoded


def _decode_payload(payload_json: str, expected_type_name: str | None = None) -> Any:
    try:
        obj = domain_from_json(payload_json)
    except (DomainDecodeError, SchemaVersionError, ValueError) as exc:
        raise MalformedPayloadError(f"Payload decode failed: {exc}") from exc
    if expected_type_name is not None and type(obj).__name__ != expected_type_name:
        raise MalformedPayloadError(
            f"Expected payload type {expected_type_name!r}, got {type(obj).__name__!r}"
        )
    return obj


class ReplayReader:
    """Deterministic read-only replay. Opens in read-only mode when given Path/str."""

    def __init__(
        self,
        conn_or_path: Union[Path, str, sqlite3.Connection],
    ) -> None:
        if isinstance(conn_or_path, sqlite3.Connection):
            self._conn = conn_or_path
            self._owns_conn = False
        else:
            path_str = str(conn_or_path)
            uri = f"file:{path_str}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True)
            self._owns_conn = True
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON;")

    def close(self) -> None:
        if self._owns_conn:
            self._conn.close()

    # ------------------------------------------------------------------
    # Internal helpers (read-only SELECT only)
    # ------------------------------------------------------------------

    def _load_factory_run(self, run_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM factory_runs WHERE id = ? LIMIT 1", (run_id,)
        ).fetchone()
        if row is None:
            raise IncompleteEvidenceError(f"FactoryRun {run_id!r} not found in ledger")
        return row

    def _load_experiment(self, exp_id: str | None) -> sqlite3.Row | None:
        if exp_id is None:
            return None
        row = self._conn.execute(
            "SELECT * FROM experiment_manifests WHERE id = ? LIMIT 1", (exp_id,)
        ).fetchone()
        if row is None:
            raise IncompleteEvidenceError(
                f"FactoryRun references experiment_id {exp_id!r} which is missing"
            )
        return row

    def _load_run_entries(self, run_id: str) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM evidence_entries WHERE factory_run_id = ? ORDER BY global_sequence ASC",
                (run_id,),
            ).fetchall()
        )

    # ------------------------------------------------------------------
    # Replay main
    # ------------------------------------------------------------------

    def replay_factory_run(self, run_id: FactoryRunId) -> ReplayResult:
        run_id_str = str(run_id)
        run_row = self._load_factory_run(run_id_str)
        try:
            run_obj: FactoryRun = _decode_payload(run_row["payload_json"], "FactoryRun")
        except MalformedPayloadError:
            raise

        exp_id_in_run: str | None = run_row["experiment_id"]
        _exp_row = self._load_experiment(exp_id_in_run)

        entries = self._load_run_entries(run_id_str)

        ordered_events: list[Any] = []
        run_evidences: list[Any] = []
        failures: list[Any] = []
        gate_results: list[Any] = []
        evidence_refs: list[tuple[str, str, int]] = []
        integrity_flags: list[dict[str, Any]] = []

        prev_seq: int | None = None
        prev_hash: str | None = None
        chain_broken = False

        for row in entries:
            entity_type = row["entity_type"]
            entity_id = row["entity_id"]
            global_seq = int(row["global_sequence"])
            payload_json = row["payload_json"]
            stored_sha = row["payload_sha256"]
            cur_prev_hash = row["prev_hash"]
            evidence_refs.append((entity_id, entity_type, global_seq))

            # Integrity: sha256 matches stored
            actual_sha = payload_hash_from_json(payload_json)
            if actual_sha != stored_sha:
                integrity_flags.append(
                    {
                        "entry_id": int(row["entry_id"]),
                        "entity_id": entity_id,
                        "issue": "TAMPERED_PAYLOAD_SHA_MISMATCH",
                        "expected": stored_sha,
                        "actual": actual_sha,
                    }
                )

            # Integrity: prev_hash chain (only within the run's entries list order)
            chain_ok = verify_chain(prev_seq, prev_hash, global_seq, stored_sha, cur_prev_hash)
            if not chain_ok:
                integrity_flags.append(
                    {
                        "entry_id": int(row["entry_id"]),
                        "entity_id": entity_id,
                        "issue": "BROKEN_PREV_HASH_CHAIN",
                        "prev_seq_expected": prev_seq,
                    }
                )
                chain_broken = True
            prev_seq = global_seq
            prev_hash = stored_sha

            # Decode and classify
            try:
                obj = _decode_payload(payload_json, None)
            except (MalformedPayloadError, Exception) as exc:
                # Anything (TypeError, ValueError, malformed json, etc) that prevents decode counts as malformed/tampered
                integrity_flags.append(
                    {
                        "entry_id": int(row["entry_id"]),
                        "entity_id": entity_id,
                        "issue": "MALFORMED_PAYLOAD_DECODE",
                        "detail": str(exc)[:200],
                    }
                )
                continue

            tname = type(obj).__name__
            if tname == "LifecycleEvent":
                ordered_events.append(obj)
            elif tname == "RunEvidence":
                run_evidences.append(obj)
            elif tname == "FailureRecord":
                failures.append(obj)
            elif tname == "GateResult":
                gate_results.append(obj)

        # Derive evidence-backed fields (no inference)
        observed_run_status: str
        if run_row["closed_at"] is not None:
            observed_run_status = run_row["final_status"] or "unknown"
        else:
            observed_run_status = RunStatus.PENDING.value if hasattr(RunStatus.PENDING, 'value') else "pending"

        completed_task_ids: list[str] = []
        gate_outcomes: list[tuple[str, str, str | None]] = []
        for g in gate_results:
            if isinstance(g, GateResult):
                status_val = g.status.value if hasattr(g.status, 'value') else str(g.status)
                target = str(g.target_task_id) if g.target_task_id else None
                gate_outcomes.append((str(g.id), status_val, target))
                if g.status == GateStatus.PASS and g.target_task_id:
                    completed_task_ids.append(str(g.target_task_id))

        product_verified_present = False
        for ev in ordered_events:
            if isinstance(ev, LifecycleEvent) and ev.event_type == LifecycleEventType.PRODUCT_VERIFIED:
                product_verified_present = True
                break
        final_verification_state = bool(int(run_row["verified"]) and product_verified_present)

        if any(f["issue"] == "TAMPERED_PAYLOAD_SHA_MISMATCH" for f in integrity_flags):
            integrity_status = "TAMPERED"
        elif chain_broken or any(
            f["issue"] in ("BROKEN_PREV_HASH_CHAIN", "MALFORMED_PAYLOAD_DECODE") for f in integrity_flags
        ):
            integrity_status = "INCOMPLETE_CHAIN"
        else:
            integrity_status = "OK"

        return ReplayResult(
            experiment_id=exp_id_in_run,
            factory_run_id=run_id_str,
            ordered_events=tuple(ordered_events),
            observed_run_status=observed_run_status,
            completed_task_ids=tuple(completed_task_ids),
            gate_outcomes=tuple(gate_outcomes),
            failures=tuple(failures),
            evidence_refs=tuple(evidence_refs),
            final_verification_state=final_verification_state,
            integrity_status=integrity_status,
            integrity_flags=tuple(integrity_flags),
            run_evidences=tuple(run_evidences),
            gate_results=tuple(gate_results),
        )

    def verify_integrity_all(self) -> dict[str, int]:
        """Scan entire evidence_entries table for integrity problems.

        Returns dict {tampered_count, broken_chain_count, malformed_count, ok_count, total}
        """
        rows = list(
            self._conn.execute(
                "SELECT * FROM evidence_entries ORDER BY global_sequence ASC"
            ).fetchall()
        )
        tampered = 0
        broken_chain = 0
        malformed = 0
        ok = 0
        prev_seq: int | None = None
        prev_hash: str | None = None
        for r in rows:
            actual_sha = payload_hash_from_json(r["payload_json"])
            if actual_sha != r["payload_sha256"]:
                tampered += 1
            if not verify_chain(
                prev_seq,
                prev_hash,
                int(r["global_sequence"]),
                r["payload_sha256"],
                r["prev_hash"],
            ):
                broken_chain += 1
            try:
                domain_from_json(r["payload_json"])
            except (DomainDecodeError, SchemaVersionError, ValueError):
                malformed += 1
            prev_seq = int(r["global_sequence"])
            prev_hash = r["payload_sha256"]
        total = len(rows)
        ok = total - (tampered + broken_chain + malformed)
        if ok < 0:
            ok = 0
        return {
            "tampered_count": tampered,
            "broken_chain_count": broken_chain,
            "malformed_count": malformed,
            "ok_count": ok,
            "total": total,
        }


__all__ = ["ReplayReader"]
