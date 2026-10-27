"""REDI-06C acceptance preparation tests.  All tests are zero-network."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest

from redi_lab.dag_scheduler import (
    DagValidationError,
    NodeRunState,
    canonical_replay_view,
    replay_dag_run,
)
from redi_lab.domain import FactoryRun, FactoryRunId, Mode, RunStatus, domain_from_json, new_id
from redi_lab.evidence.artifacts import (
    ArtifactRetentionError,
    MAX_RETAINED_ARTIFACT_BYTES,
    retain_validated_artifact,
    verify_retained_artifact,
)
from redi_lab.factory.isolation import IsolatedWorkspace
from redi_lab.parallel_dag_executor import ParallelDagExecutor
from redi_lab.persistence import (
    EvidenceLedger,
    FactoryRunRepository,
    IncompleteEvidenceError,
)
from redi_lab.runtime.contracts import PatchEdit
from redi_lab.runtime.live_provider_adapter import LiveProviderAdapter, LiveProviderConfig
from scripts import run_redi_06_live_parallel as live06c


def _executor(bundle):
    return ParallelDagExecutor(
        dag=bundle.dag,
        station_by_node=bundle.stations,
        task_by_node=bundle.tasks,
        node_workers=bundle.workers,
        required_handoff_ids_by_node={"node_c": ("handoff_alpha", "handoff_beta")},
        produced_handoff_ids_by_node={
            "node_a": ("handoff_alpha",), "node_b": ("handoff_beta",),
        },
        produced_handoff_types_by_id={
            "handoff_alpha": ("alpha.impl.v1",), "handoff_beta": ("beta.impl.v1",),
        },
        fixture_root=live06c.FIXTURE_ROOT,
        product=bundle.product,
        max_parallelism=2,
    )


def test_06c_exact_path_dry_run_green_zero_live_calls(tmp_path: Path, monkeypatch):
    def _network_forbidden(*_args, **_kwargs):
        raise AssertionError("dry run attempted live provider")

    monkeypatch.setattr(LiveProviderAdapter, "run_node", _network_forbidden)
    db_path, result, report = live06c.execute_acceptance(
        live=False, output_root=tmp_path / "runs"
    )
    audit = report["audit"]
    assert db_path.is_file()
    assert result.final_states == {
        "node_a": NodeRunState.COMPLETED,
        "node_b": NodeRunState.COMPLETED,
        "node_c": NodeRunState.COMPLETED,
    }
    assert audit["verified"] is True
    assert audit["provider_calls"] == 0
    assert audit["observed_worker_slots"] == 2
    assert audit["overlap"] is True
    assert audit["checks"]["replay_x2_equal"] is True
    assert audit["checks"]["replay_zero_writes"] is True


def test_06c_live_composition_uses_two_separate_approved_adapters():
    bundle = live06c.build_acceptance(live=True)
    adapters = []
    for nid in ("node_a", "node_b"):
        worker = bundle.workers[nid]
        adapter = next(iter(worker.runners.values()))
        assert isinstance(adapter, LiveProviderAdapter)
        assert adapter.config.model == live06c.MODEL
        assert adapter.config.max_output_tokens == live06c.MAX_OUTPUT_TOKENS
        adapters.append(adapter)
    assert adapters[0] is not adapters[1]
    assert adapters[0].call_observer is not adapters[1].call_observer
    # Provider/model identity is absent from structural ProductDAG nodes.
    assert "openai" not in repr(bundle.dag).lower()
    assert live06c.MODEL not in repr(bundle.dag)


def test_06c_env_guards_block_before_composition(monkeypatch, capsys):
    monkeypatch.delenv("REDI_LIVE_PROVIDER", raising=False)
    monkeypatch.delenv("REDI_OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        live06c, "execute_acceptance",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("execution must not start")),
    )
    assert live06c.main([]) == 2
    assert "REDI-06C LIVE BLOCKED" in capsys.readouterr().out


def test_06c_failed_live_slot_has_no_retry_sibling_finishes_c_blocks():
    bundle = live06c.build_acceptance(live=False)
    worker_a = bundle.workers["node_a"]
    dry_a = next(iter(worker_a.runners.values()))
    dry_a.patch = PatchEdit("alpha/not_allowed.py", "write", "x = 1\n")
    result = _executor(bundle).run()
    assert result.final_states["node_a"] == NodeRunState.FAILED
    assert result.final_states["node_b"] == NodeRunState.COMPLETED
    assert result.final_states["node_c"] == NodeRunState.BLOCKED
    assert set(bundle.observations["node_a"].snapshot()) == {
        "provider_call_started", "provider_call_finished"
    }
    assert set(bundle.observations["node_b"].snapshot()) == {
        "provider_call_started", "provider_call_finished"
    }
    failed = next(
        r for r in result.evidence_rows
        if r.get("row_kind") == "dag_node_failed" and r.get("node_id") == "node_a"
    )
    evidence = failed["execution_evidence"]
    assert evidence["retry_count"] == 0
    assert evidence["automatic_retry"] is False


def test_06c_adapter_observes_actual_sdk_call_boundary(monkeypatch, tmp_path: Path):
    events: list[str] = []

    class _Completions:
        def create(self, **_kwargs):
            events.append("sdk_create")
            usage = types.SimpleNamespace(
                prompt_tokens=7,
                completion_tokens=3,
                prompt_tokens_details=types.SimpleNamespace(cached_tokens=0),
            )
            message = types.SimpleNamespace(content='{"patches":[]}')
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(message=message)],
                usage=usage,
                model=live06c.MODEL,
            )

    class _Client:
        def __init__(self, **_kwargs):
            self.chat = types.SimpleNamespace(completions=_Completions())

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = _Client
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    monkeypatch.setenv("REDI_OPENAI_API_KEY", "test-only-key")
    adapter = LiveProviderAdapter(
        config=LiveProviderConfig(model=live06c.MODEL, max_output_tokens=32),
        before_call=lambda: events.append("before_call"),
        call_observer=lambda event, _ts: events.append(event),
    )
    result = adapter.run_node(
        task_id="task-test",
        station_id="station-test",
        worker_id="worker-test",
        context={"role": "builder", "instructions": "tiny", "context_body": ""},
        workspace_root=tmp_path,
    )
    assert result.error_message is None
    assert events == [
        "before_call", "provider_call_started", "sdk_create", "provider_call_finished"
    ]


def _db_rows(db_path: Path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        factories = list(conn.execute("SELECT * FROM factory_runs ORDER BY created_at"))
        entries = list(conn.execute("SELECT * FROM evidence_entries ORDER BY global_sequence"))
        pricing_count = conn.execute("SELECT count(*) FROM pricing_evidence").fetchone()[0]
        return factories, entries, pricing_count
    finally:
        conn.close()


def _metadata(entries):
    return [dict(domain_from_json(row["payload_json"]).metadata) for row in entries]


def test_06c2_factory_binding_retention_cleanup_and_full_replay(
    tmp_path: Path, monkeypatch,
):
    destroyed: list[Path] = []
    original_destroy = IsolatedWorkspace.destroy

    def _record_destroy(workspace):
        destroyed.append(workspace.root)
        return original_destroy(workspace)

    monkeypatch.setattr(IsolatedWorkspace, "destroy", _record_destroy)
    db_path, result, report = live06c.execute_acceptance(
        live=False, output_root=tmp_path / "runs",
    )
    factory_id = report["factory_run_id"]
    factories, entries, pricing_count = _db_rows(db_path)
    assert len(factories) == 1
    factory = factories[0]
    assert factory["id"] == factory_id
    assert factory["closed_at"] is not None
    assert factory["final_status"] == RunStatus.COMPLETED.value
    assert factory["verified"] == 1
    assert factory["experiment_id"] is None
    assert factory["total_tokens_json"] is not None
    assert pricing_count == 0
    stored_run = domain_from_json(factory["payload_json"])
    assert isinstance(stored_run, FactoryRun)
    assert stored_run.metadata["acceptance_run_id"] == report["run_id"]
    assert stored_run.metadata["pricing_evidence"] == "not_recorded_no_versioned_schedule"

    assert entries
    assert all(row["factory_run_id"] == factory_id for row in entries)
    decoded = [domain_from_json(row["payload_json"]) for row in entries]
    assert all(str(item.factory_run_id) == factory_id for item in decoded)
    assert factory["created_at"] <= entries[0]["recorded_at"]
    assert destroyed and all(not root.exists() for root in destroyed)

    handoffs = [row for row in _metadata(entries) if row.get("row_kind") == "dag_handoff_manifest"]
    retained = [item for row in handoffs for item in row["produced_handoffs"]]
    assert len(retained) == 2
    for item in retained:
        verified = verify_retained_artifact(
            run_artifact_root=db_path.parent,
            retained_path=item["retained_artifact_path"],
            artifact_id=item["artifact_id"],
            expected_sha256=item["artifact_hash"],
            expected_size=item["artifact_size_bytes"],
        )
        assert verified["sha256"] == item["artifact_hash"]

    rows = _metadata(entries)
    replay_1 = replay_dag_run(rows, retained_artifact_root=db_path.parent)
    replay_2 = replay_dag_run(rows, retained_artifact_root=db_path.parent)
    assert canonical_replay_view(replay_1) == canonical_replay_view(replay_2)
    assert report["audit"]["checks"]["factory_run_bound"] is True
    assert report["audit"]["checks"]["retained_artifacts_verified"] is True
    assert report["audit"]["checks"]["replay_x2_equal"] is True


def test_06c2_directory_rename_does_not_change_factory_identity(tmp_path: Path):
    db_path, _result, report = live06c.execute_acceptance(
        live=False, output_root=tmp_path / "runs",
    )
    renamed = db_path.parent.with_name("renamed-run-directory")
    db_path.parent.rename(renamed)
    moved_db = renamed / "evidence.sqlite"
    audit = live06c.audit_evidence(moved_db, factory_run_id=report["factory_run_id"])
    assert audit["factory_run_id"] == report["factory_run_id"]
    assert audit["acceptance_run_id"] == report["run_id"]
    assert audit["verified"] is True


def test_06c2_missing_or_foreign_factory_binding_fails_closed(tmp_path: Path):
    db_path, _result, report = live06c.execute_acceptance(
        live=False, output_root=tmp_path / "missing",
    )
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DELETE FROM factory_runs")
    conn.commit()
    conn.close()
    with pytest.raises(IncompleteEvidenceError, match="FactoryRun"):
        live06c.audit_evidence(db_path, factory_run_id=report["factory_run_id"])

    db_path, _result, report = live06c.execute_acceptance(
        live=False, output_root=tmp_path / "foreign",
    )
    conn = sqlite3.connect(db_path)
    ledger = EvidenceLedger(conn)
    other_id = str(FactoryRunId(new_id("factory_run")))
    FactoryRunRepository(conn, ledger).store(FactoryRun(
        id=FactoryRunId(other_id), mode=Mode.EXPLORATION,
        final_status=RunStatus.PENDING,
    ))
    conn.execute(
        "UPDATE evidence_entries SET factory_run_id=? WHERE entry_id=(SELECT MIN(entry_id) FROM evidence_entries)",
        (other_id,),
    )
    conn.commit()
    conn.close()
    with pytest.raises(IncompleteEvidenceError, match="another FactoryRun"):
        live06c.audit_evidence(db_path, factory_run_id=report["factory_run_id"])


def test_06c2_factory_store_failure_launches_zero_workers(
    tmp_path: Path, monkeypatch,
):
    calls = {"workers": 0}

    def _store_failed(*_args, **_kwargs):
        raise RuntimeError("store failed")

    def _worker_called(*_args, **_kwargs):
        calls["workers"] += 1
        raise AssertionError("worker must not launch")

    monkeypatch.setattr(FactoryRunRepository, "store", _store_failed)
    monkeypatch.setattr(live06c.DryProviderRunner, "run_node", _worker_called)
    with pytest.raises(RuntimeError, match="store failed"):
        live06c.execute_acceptance(live=False, output_root=tmp_path / "runs")
    assert calls["workers"] == 0


def test_06c2_retention_copy_failure_blocks_handoff(tmp_path: Path, monkeypatch):
    import redi_lab.parallel_dag_executor as parallel

    def _copy_failed(**_kwargs):
        raise ArtifactRetentionError("copy failed")

    monkeypatch.setattr(parallel, "retain_validated_artifact", _copy_failed)
    with pytest.raises(ArtifactRetentionError, match="copy failed"):
        live06c.execute_acceptance(live=False, output_root=tmp_path / "runs")
    db_path = next((tmp_path / "runs").glob("*/evidence.sqlite"))
    factories, entries, _ = _db_rows(db_path)
    assert factories[0]["final_status"] == RunStatus.FAILED.value
    assert not any(row.get("row_kind") == "dag_handoff_manifest" for row in _metadata(entries))


def test_06c2_retention_bounds_hash_and_duplicate_identity(tmp_path: Path):
    root = tmp_path / "run"
    source = tmp_path / "source.bin"
    source.write_bytes(b"verified bytes")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    with pytest.raises(ArtifactRetentionError, match="hash mismatch"):
        retain_validated_artifact(
            run_artifact_root=root, source_path=source, artifact_id="artifact::one",
            expected_sha256="0" * 64, expected_size=source.stat().st_size,
        )
    rel = retain_validated_artifact(
        run_artifact_root=root, source_path=source, artifact_id="artifact::one",
        expected_sha256=digest, expected_size=source.stat().st_size,
    )
    source.write_bytes(b"different verified bytes")
    with pytest.raises(ArtifactRetentionError, match="identity conflict"):
        retain_validated_artifact(
            run_artifact_root=root, source_path=source, artifact_id="artifact::one",
            expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            expected_size=source.stat().st_size,
        )
    oversized = tmp_path / "oversized.bin"
    oversized.write_bytes(b"x" * (MAX_RETAINED_ARTIFACT_BYTES + 1))
    with pytest.raises(ArtifactRetentionError, match="size bound"):
        retain_validated_artifact(
            run_artifact_root=root, source_path=oversized, artifact_id="artifact::large",
            expected_sha256=hashlib.sha256(oversized.read_bytes()).hexdigest(),
            expected_size=oversized.stat().st_size,
        )
    with pytest.raises(ArtifactRetentionError, match="path"):
        verify_retained_artifact(
            run_artifact_root=root, retained_path="../escape",
            artifact_id="artifact::one", expected_sha256=digest,
            expected_size=len(b"verified bytes"),
        )
    assert rel.startswith("handoffs/")


def test_06c2_terminalization_failure_is_surfaced(tmp_path: Path, monkeypatch):
    def _close_failed(*_args, **_kwargs):
        raise RuntimeError("terminalization failed")

    monkeypatch.setattr(FactoryRunRepository, "close_run", _close_failed)
    with pytest.raises(RuntimeError, match="terminalization failed"):
        live06c.execute_acceptance(live=False, output_root=tmp_path / "runs")


def test_06c2_replay_snapshot_tamper_artifact_tamper_and_semantic_delta(tmp_path: Path):
    db_path, _result, report = live06c.execute_acceptance(
        live=False, output_root=tmp_path / "runs",
    )
    _factory_id, _factory, rows, _entries = live06c._load_bound_acceptance_rows(
        db_path, factory_run_id=report["factory_run_id"],
    )
    replay = replay_dag_run(rows, retained_artifact_root=db_path.parent)
    canonical = canonical_replay_view(replay)
    changed = copy.deepcopy(canonical)
    changed["token_evidence"]["node_a"]["input"] += 1
    assert canonical != changed

    bad_snapshot = copy.deepcopy(rows)
    next(row for row in bad_snapshot if row["row_kind"] == "dag_snapshot")["graph_hash"] = "0" * 64
    with pytest.raises(DagValidationError, match="hash mismatch"):
        replay_dag_run(bad_snapshot, retained_artifact_root=db_path.parent)

    retained_path = Path(replay["retained_artifacts"][0]["retained_path"])
    target = db_path.parent / retained_path
    target.write_bytes(target.read_bytes() + b"tamper")
    with pytest.raises(DagValidationError, match="retained artifact verification failed"):
        live06c.audit_evidence(db_path, factory_run_id=report["factory_run_id"])
