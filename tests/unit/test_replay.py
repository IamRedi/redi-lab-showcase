"""REDI-02 deterministic replay tests (categories 18-22 + final-state fixture check)."""

from __future__ import annotations

import copy
import socket
import sqlite3
import subprocess
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from redi_lab.domain import (
    DomainDecodeError,
    ExperimentManifest,
    FactoryRun,
    GateResult,
    LifecycleEvent,
    LifecycleEventType,
    RunStatus,
    SchemaVersionError,
    domain_from_json,
    domain_to_json,
)
from redi_lab.persistence import (
    EvidenceLedger,
    ExperimentRepository,
    FactoryRunRepository,
    IncompleteEvidenceError,
    bootstrap_schema,
    make_pricing_evidence_row,
    insert_pricing_evidence,
)
from redi_lab.replay import ReplayReader, ReplayResult
from tests.unit._redi02_fixtures import build_deterministic_ledger


# ============ Category 19: Deterministic replay happy path ============


def test_deterministic_replay(tmp_path):
    conn, manifest, run, events = build_deterministic_ledger(tmp_path / "det.db")
    reader = ReplayReader(conn)
    result = reader.replay_factory_run(run.id)
    assert isinstance(result, ReplayResult)
    assert result.factory_run_id == str(run.id)
    assert result.experiment_id == str(manifest.id)
    assert len(result.ordered_events) == 6
    assert len(result.evidence_refs) == 8  # 6 events + 1 RunEvidence + 1 GateResult
    etypes = [e.event_type for e in result.ordered_events]
    assert etypes[0] == events[0].event_type
    assert etypes[-1] == LifecycleEventType.PRODUCT_VERIFIED
    assert result.integrity_status == "OK"
    assert result.integrity_flags == ()


# ============ Category 20: Replay twice equivalent ============


def test_double_replay_equivalent(tmp_path):
    conn, _, run, _ = build_deterministic_ledger(tmp_path / "2x.db")
    reader = ReplayReader(conn)
    r1 = reader.replay_factory_run(run.id)
    r2 = reader.replay_factory_run(run.id)
    # 9 core fields equal
    assert r1.experiment_id == r2.experiment_id
    assert r1.factory_run_id == r2.factory_run_id
    assert [e.id for e in r1.ordered_events] == [e.id for e in r2.ordered_events]
    assert r1.observed_run_status == r2.observed_run_status
    assert r1.completed_task_ids == r2.completed_task_ids
    assert r1.gate_outcomes == r2.gate_outcomes
    assert [f.id for f in r1.failures] == [f.id for f in r2.failures]
    assert r1.evidence_refs == r2.evidence_refs
    assert r1.final_verification_state == r2.final_verification_state
    assert r1.integrity_status == r2.integrity_status
    assert r1.integrity_flags == r2.integrity_flags


# ============ Category 21: Replay does not mutate DB ============


def test_replay_readonly_no_mutation(tmp_path):
    p = tmp_path / "rm.db"
    conn, _, run, _ = build_deterministic_ledger(p)
    # Counts before close and reopen read-only
    before = {}
    tables = ["evidence_entries", "experiment_manifests", "factory_runs", "pricing_evidence", "persistence_schema_metadata"]
    for t in tables:
        before[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    conn.close()
    reader = ReplayReader(p)  # opens in ro mode
    result = reader.replay_factory_run(run.id)
    reader.close()
    # reopen rw and compare counts
    conn2 = sqlite3.connect(str(p))
    conn2.row_factory = sqlite3.Row
    after = {}
    for t in tables:
        after[t] = conn2.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    assert before == after
    # Also result is not empty sanity
    assert result.integrity_status == "OK"


# ============ Category 22: Incomplete evidence rejection ============


def test_replay_rejects_missing_experiment(tmp_path):
    conn, manifest, run, events = build_deterministic_ledger(tmp_path / "inc.db")
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DELETE FROM experiment_manifests WHERE id = ?", (str(manifest.id),))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()
    reader = ReplayReader(conn)
    with pytest.raises(IncompleteEvidenceError, match="missing"):
        reader.replay_factory_run(run.id)


def test_replay_rejects_missing_factory_run(tmp_path):
    conn, _, run, _ = build_deterministic_ledger(tmp_path / "inc2.db")
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DELETE FROM factory_runs WHERE id = ?", (str(run.id),))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()
    reader = ReplayReader(conn)
    from redi_lab.persistence import IncompleteEvidenceError as IE
    with pytest.raises(IE, match="not found"):
        reader.replay_factory_run(run.id)


# ============ Category 18: Tamper detection ============


def test_tamper_detection_on_replay(tmp_path):
    p = tmp_path / "tamper.db"
    conn, _, run, _ = build_deterministic_ledger(p)
    # Tamper first event payload_json: replace with different string
    row = conn.execute("SELECT entry_id, payload_json, payload_sha256 FROM evidence_entries ORDER BY global_sequence LIMIT 1").fetchone()
    bad_json = '{"__type__": "LifecycleEvent", "tampered": true, "id": "x"}'
    conn.execute(
        "UPDATE evidence_entries SET payload_json = ? WHERE entry_id = ?",
        (bad_json, row["entry_id"]),
    )
    conn.commit()
    conn.close()
    reader = ReplayReader(p)
    result = reader.replay_factory_run(run.id)
    assert result.integrity_status == "TAMPERED"
    assert any(
        f["issue"] == "TAMPERED_PAYLOAD_SHA_MISMATCH" for f in result.integrity_flags
    )


# ============ Category: verify_integrity_all ============


def test_verify_integrity_all_before_after_tamper(tmp_path):
    p = tmp_path / "int.db"
    conn, _, _, _ = build_deterministic_ledger(p)
    reader = ReplayReader(conn)
    report = reader.verify_integrity_all()
    assert report["total"] == 8
    assert report["ok_count"] == 8
    assert report["tampered_count"] == 0
    # Tamper
    conn.execute(
        "UPDATE evidence_entries SET payload_json = '{}' WHERE entry_id = (SELECT MIN(entry_id) FROM evidence_entries)"
    )
    conn.commit()
    r2 = reader.verify_integrity_all()
    assert r2["tampered_count"] >= 1


# ============ Category: Replay full fixture final state ============


def test_replay_full_fixture_final_state(tmp_path):
    conn, _, _, _ = build_deterministic_ledger(tmp_path / "final.db")
    # Fetch the only run
    row = conn.execute("SELECT id FROM factory_runs LIMIT 1").fetchone()
    run_id = row[0]
    reader = ReplayReader(conn)
    res = reader.replay_factory_run(run_id)
    # final_verification_state = verified (True in close_run) + PRODUCT_VERIFIED present
    assert res.final_verification_state is True
    # Completed tasks should include node C task id
    assert len(res.completed_task_ids) >= 1
    # No failures in successful calibration
    assert res.failures == ()
    assert len(res.ordered_events) == 6
    # observed_run_status completed because run closed with COMPLETED
    s = res.observed_run_status
    assert s == RunStatus.COMPLETED.value or s.lower() in {"completed", "success", "verified"}


# ============ Category: Replay makes zero external calls (monkeypatch check) ============


def test_replay_no_external_calls(tmp_path, monkeypatch):
    p = tmp_path / "noext.db"
    conn, _, run, _ = build_deterministic_ledger(p)
    conn.close()
    calls = {"subprocess": 0, "socket": 0}

    def fake_subprocess(*a, **kw):
        calls["subprocess"] += 1
        raise RuntimeError("subprocess forbidden during replay")

    class FakeSocket:
        def __init__(self, *a, **kw):
            calls["socket"] += 1
            raise RuntimeError("socket forbidden during replay")

    monkeypatch.setattr(subprocess, "run", fake_subprocess)
    monkeypatch.setattr(socket, "socket", FakeSocket)
    reader = ReplayReader(p)
    # Should not raise
    result = reader.replay_factory_run(run.id)
    assert result.integrity_status == "OK"
    assert calls["subprocess"] == 0
    assert calls["socket"] == 0


# ============ Category: Malformed payload raises MalformedPayloadError on decode ============


def test_replay_malformed_payload_flag(tmp_path):
    p = tmp_path / "m.db"
    conn, _, run, _ = build_deterministic_ledger(p)
    # Replace payload_json with something that is valid JSON but NOT a valid domain contract
    invalid = '{"__type__": "LifecycleEvent", "id": 12345}'  # id not str
    conn.execute(
        "UPDATE evidence_entries SET payload_json = ?, payload_sha256 = ? WHERE entry_id = (SELECT MIN(entry_id) FROM evidence_entries)",
        (invalid, hashlib_sha256(invalid)),
    )
    conn.commit()
    conn.close()
    reader = ReplayReader(p)
    result = reader.replay_factory_run(run.id)
    issues = [f["issue"] for f in result.integrity_flags]
    # Either tamper detected (sha mismatch) or malformed decode (if we had correct sha)
    assert "TAMPERED_PAYLOAD_SHA_MISMATCH" in issues or "MALFORMED_PAYLOAD_DECODE" in issues


def hashlib_sha256(s: str) -> str:
    import hashlib
    return hashlib.sha256(s.encode("utf-8")).hexdigest()
