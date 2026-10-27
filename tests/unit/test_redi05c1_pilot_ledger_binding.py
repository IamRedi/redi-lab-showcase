"""REDI-05C1 — ExperimentManifest / FactoryRun ledger binding tests.

Regression for the real blocked live pilot: the runner preregistered the
ExperimentManifest into the master pilot ledger while each condition run
used a separate auto-created temp database that lacked the experiment row —
FactoryRunRepository.store() then failed on the
factory_runs.experiment_id → experiment_manifests.id foreign key BEFORE any
worker invocation (zero provider calls, zero evidence).

The fix (run_pair ledger-ownership invariant): run_pair ALWAYS materializes
a per-condition database and preregisters the SAME immutable
ExperimentManifest (identical id/payload/hash) into it BEFORE the condition
run.  These tests exercise the exact live-runner storage lifecycle
(run_pair without db_dir) and fail under the old bug.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from redi_lab.domain import ExperimentManifest, FactoryRun, Mode, RunStatus
from redi_lab.experiments import context_pilot as pilot
from redi_lab.experiments.context_pilot import (
    build_experiment_manifest,
    preregister_into,
    run_pair,
)
from redi_lab.persistence import EvidenceLedger, IncompleteEvidenceError, bootstrap_schema
from redi_lab.persistence.repositories import FactoryRunRepository
from redi_lab.replay import ReplayReader


def _fr_repo(conn: sqlite3.Connection) -> FactoryRunRepository:
    conn.execute("PRAGMA foreign_keys = ON;")
    return FactoryRunRepository(conn, EvidenceLedger(conn))


def test_05c1_fk_rejects_unknown_experiment_id_without_preregistration(tmp_path: Path):
    """The FK invariant itself: storing a FactoryRun whose experiment_id has
    no ExperimentManifest row in THAT database must fail."""
    db = tmp_path / "condition.db"
    conn = sqlite3.connect(db)
    try:
        bootstrap_schema(conn)
        conn.execute("PRAGMA foreign_keys = ON;")
        repo = _fr_repo(conn)
        with __import__("pytest").raises(IncompleteEvidenceError, match="FK experiment_id unknown"):
            repo.store(FactoryRun(), experiment_id="experiment_does_not_exist")
        # nothing partially stored
        assert conn.execute("SELECT COUNT(*) FROM factory_runs").fetchone()[0] == 0
    finally:
        conn.close()


def test_05c1_preregistration_makes_fk_resolve(tmp_path: Path):
    db = tmp_path / "condition.db"
    manifest = build_experiment_manifest()
    preregister_into(db, manifest)
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        repo = _fr_repo(conn)
        stored = repo.store(FactoryRun(), experiment_id=str(manifest.id))
        assert stored
        row = conn.execute(
            "SELECT experiment_id FROM factory_runs").fetchone()
        assert row[0] == str(manifest.id)
    finally:
        conn.close()


def test_05c1_live_runner_storage_lifecycle_regression(tmp_path: Path):
    """Exact live-runner lifecycle: run_pair WITHOUT db_dir (the runner never
    passes one).  Old bug: each condition got a fresh controller temp DB with
    an empty experiment_manifests table → FK failure before any worker call.
    Fixed: run_pair materializes per-condition DBs and preregisters the same
    manifest into each BEFORE the condition run."""
    manifest = build_experiment_manifest(model="gpt-4o-mini")
    pair = run_pair(live_builder=None, manifest=manifest,
                    order=("selected_v0", "full_public_v0"))

    assert pair.conditions["selected_v0"].outcome_status == "completed"
    assert pair.conditions["full_public_v0"].outcome_status == "completed"
    assert pair.conditions["selected_v0"].verified is True
    assert pair.conditions["full_public_v0"].verified is True

    # every condition database: FK resolves; manifest id/payload/hash
    # identical everywhere; the factory run references the experiment id
    payloads, hashes, run_ids = set(), set(), []
    for strategy, db_path in pair.condition_db_paths.items():
        conn = sqlite3.connect(db_path)
        try:
            em = conn.execute(
                "SELECT id, payload_json, payload_sha256 FROM experiment_manifests"
            ).fetchall()
            assert len(em) == 1
            assert em[0][0] == pair.experiment_id
            payloads.add(em[0][1])
            hashes.add(em[0][2])
            runs = conn.execute("SELECT id, experiment_id FROM factory_runs").fetchall()
            assert len(runs) == 1 and runs[0][1] == pair.experiment_id
            run_ids.append(runs[0][0])
            # the stored payload hash matches a fresh canonical serialization
            from redi_lab.persistence import payload_hash_from_json
            assert em[0][2] == payload_hash_from_json(em[0][1])
        finally:
            conn.close()
    assert len(payloads) == 1 and len(hashes) == 1
    assert len(set(run_ids)) == 2  # two distinct runs, one shared experiment


def test_05c1_replay_per_condition_db(tmp_path: Path):
    manifest = build_experiment_manifest()
    pair = run_pair(live_builder=None, manifest=manifest,
                    order=("selected_v0", "full_public_v0"), db_dir=tmp_path)
    for strategy in pair.order:
        reader = ReplayReader(pair.condition_db_paths[strategy])
        try:
            res = reader.replay_factory_run(strategy_run_id(pair, strategy))
        finally:
            reader.close()
        assert res.integrity_status == "OK"
        assert res.observed_run_status == "completed"


def strategy_run_id(pair, strategy: str):
    return pair.conditions[strategy].factory_run_id


def test_05c1_partial_failure_safety(tmp_path: Path):
    """Condition 1 completes; condition 2 fails before worker execution
    (budget overflow).  First condition evidence stays valid, both DBs keep
    the same manifest/pair identity, no corrupted half-registration."""
    from redi_lab.context_compiler import DEFAULT_BUDGET_BYTES

    deps_s, product, task = pilot.condition_deps(strategy=pilot.CONDITION_SELECTED)
    deps_f, _, _ = pilot.condition_deps(strategy=pilot.CONDITION_FULL_PUBLIC)
    deps_f.context_budget_bytes = 64  # condition F: deterministic compile rejection

    preregistered = []
    real_preregister = preregister_into

    def _spy(db_path, manifest):
        preregistered.append((str(db_path), manifest))
        return real_preregister(db_path, manifest)

    def _deps_factory(strategy: str):
        if strategy == pilot.CONDITION_FULL_PUBLIC:
            deps_f.context_budget_bytes = 64
            return deps_f, product, task
        return deps_s, product, task

    pilot.preregister_into = _spy
    try:
        pair = run_pair(
            deps_factory=_deps_factory,
            order=(pilot.CONDITION_SELECTED, pilot.CONDITION_FULL_PUBLIC),
            db_dir=tmp_path,
        )
    finally:
        pilot.preregister_into = real_preregister

    s = pair.conditions[pilot.CONDITION_SELECTED]
    f = pair.conditions[pilot.CONDITION_FULL_PUBLIC]
    assert s.outcome_status == "completed" and s.verified is True
    assert f.outcome_status == "blocked" and f.verified is False

    db_s, db_f = pair.condition_db_paths[pilot.CONDITION_SELECTED], pair.condition_db_paths[pilot.CONDITION_FULL_PUBLIC]

    # condition 1 evidence intact
    conn = sqlite3.connect(db_s)
    try:
        runs = conn.execute("SELECT experiment_id, verified FROM factory_runs").fetchall()
        assert len(runs) == 1 and runs[0][1] == 1
    finally:
        conn.close()

    # condition 2: explicit failure evidence, no worker RunEvidence
    conn = sqlite3.connect(db_f)
    try:
        frs = conn.execute(
            "SELECT payload_json FROM evidence_entries WHERE entity_type='FailureRecord'").fetchall()
        assert any(json.loads(p[0])["origin"] == "context_compile_rejected" for p in frs)
        assert any(json.loads(p[0])["category"] == "context_insufficiency" for p in frs)
        n_builder_ev = conn.execute(
            "SELECT COUNT(*) FROM evidence_entries WHERE entity_type='RunEvidence' "
            "AND payload_json LIKE '%N_BUILDER_0%'").fetchone()[0]
        assert n_builder_ev == 0  # failed before worker execution
    finally:
        conn.close()

    # no corrupted half-registration: identical manifest payload in both DBs
    payloads = set()
    for db in (db_s, db_f):
        conn = sqlite3.connect(db)
        try:
            payloads.add(conn.execute(
                "SELECT payload_json FROM experiment_manifests").fetchone()[0])
        finally:
            conn.close()
    assert len(payloads) == 1
