"""REDI-05C — paired context-strategy experiment harness tests.

Deterministic, zero-network: both conditions run with fake workers (the
LIVE runner itself is only structurally verified and never executed).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from redi_lab.context_compiler import (
    CONTEXT_STRATEGY_FULL_PUBLIC_V0,
    CONTEXT_STRATEGY_SELECTED_V0,
    compile_context,
)
from redi_lab.domain import ExperimentId, RunStatus
from redi_lab.experiments import context_pilot as pilot
from redi_lab.factory.controller import MinimalFactoryController
from redi_lab.replay import ReplayReader

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _preregister(db_path: Path) -> str:
    from redi_lab.experiments.context_pilot import build_experiment_manifest
    from redi_lab.persistence import EvidenceLedger, ExperimentRepository, bootstrap_schema

    conn = sqlite3.connect(db_path)
    bootstrap_schema(conn)
    repo = ExperimentRepository(conn, EvidenceLedger(conn))
    manifest = build_experiment_manifest()
    repo.store_preregistered(manifest)
    conn.commit()
    conn.close()
    return str(manifest.id)


def _preregister_into(db_path, manifest) -> str:
    from redi_lab.experiments.context_pilot import preregister_into

    return preregister_into(db_path, manifest)


def _pair_manifest() -> "ExperimentManifest":
    from redi_lab.experiments.context_pilot import build_experiment_manifest

    return build_experiment_manifest()

def _run_condition(tmp_path: Path, strategy: str, tag: str):
    deps, product, task = pilot.condition_deps(strategy=strategy)
    outcome = MinimalFactoryController.run(
        product, task, deps, db_path=tmp_path / f"{tag}.db",
        experiment_id=_pair_experiment_id(tmp_path),
    )
    metrics = pilot.condition_metrics(str(tmp_path / f"{tag}.db"), outcome.factory_run_id, strategy, 0)
    return outcome, metrics


def test_05c_wide_fixture_packages_differ_between_strategies(tmp_path: Path):
    """THE core 05C property: the two strategies compile DIFFERENT source
    sets over the wide fixture — selected = relevant only, full = relevant +
    controlled distractors — with identical contract identity."""
    deps_s, product, task = pilot.condition_deps(strategy=CONTEXT_STRATEGY_SELECTED_V0)
    deps_f, _, _ = pilot.condition_deps(strategy=CONTEXT_STRATEGY_FULL_PUBLIC_V0)
    from redi_lab.context_compiler import compile_context

    pkg_s = compile_context(
        strategy_id=CONTEXT_STRATEGY_SELECTED_V0, station_role="builder",
        task=task, station=deps_s.builder_station, product=product,
        workspace_root=deps_s.fixture_root,
        selected_paths=["slugify/__init__.py", "tests/test_slugify_public.py", "pytest.ini"],
        allowed_scope=["slugify", "tests"],
    )
    pkg_f = compile_context(
        strategy_id=CONTEXT_STRATEGY_FULL_PUBLIC_V0, station_role="reviewer" and "builder",
        task=task, station=deps_f.builder_station, product=product,
        workspace_root=deps_f.fixture_root, selected_paths=(),
        allowed_scope=["slugify", "tests"],
    )
    sel_paths = [f["path"] for f in pkg_s.included_files]
    full_paths = [f["path"] for f in pkg_f.included_files]
    # S: relevant public files only
    assert sel_paths == ["slugify/__init__.py", "tests/test_slugify_public.py", "pytest.ini"]
    # F: relevant + controlled distractors, still bounded
    for distractor in ("utils/text_utils.py", "docs/design_notes.md", "examples/usage_example.py"):
        assert distractor in full_paths
    assert "tests/test_text_utils_public.py" in full_paths
    # sets differ; full is a strict superset here
    assert set(sel_paths) != set(full_paths)
    assert set(sel_paths) < set(full_paths)
    assert pkg_s.package_hash != pkg_f.package_hash
    # contracts identical across conditions
    assert (pkg_s.task_id, pkg_s.station_id, pkg_s.product_id) == (pkg_f.task_id, pkg_f.station_id, pkg_f.product_id)
    # full_public is bounded, NOT the machine/repository
    assert pkg_f.counts["bytes"] < 64 * 1024
    assert not any(p.startswith(("/", "C:")) for p in full_paths)


def test_05c_pair_dry_run_both_conditions_complete(tmp_path: Path):
    """Zero-network paired dry-run: both strategies execute with fake
    workers; strategy/packages recorded per condition; verified both."""
    deps_s, product, task = pilot.condition_deps(strategy=pilot.CONDITION_SELECTED)
    deps_f, _, _ = pilot.condition_deps(strategy=pilot.CONDITION_FULL_PUBLIC)
    manifest = _pair_manifest()
    experiment_id = str(manifest.id)
    _preregister_into(tmp_path / "s.db", manifest)
    _preregister_into(tmp_path / "f.db", manifest)
    out_s = MinimalFactoryController.run(product, task, deps_s, db_path=tmp_path / "s.db",
                                         experiment_id=experiment_id)
    out_f = MinimalFactoryController.run(product, task, deps_f, db_path=tmp_path / "f.db",
                                         experiment_id=experiment_id)
    assert out_s.final_status == RunStatus.COMPLETED and out_s.verified is True
    assert out_f.final_status == RunStatus.COMPLETED and out_f.verified is True

    m_s = pilot.condition_metrics(str(tmp_path / "s.db"), out_s.factory_run_id, pilot.CONDITION_SELECTED, 0)
    m_f = pilot.condition_metrics(str(tmp_path / "f.db"), out_f.factory_run_id, pilot.CONDITION_FULL_PUBLIC, 1)
    for m in (m_s, m_f):
        # metrics bundle completeness (§9) — recorded, not collapsed
        for key in ("strategy", "factory_run_id", "final_status", "verified", "input_tokens",
                    "output_tokens", "total_tokens", "wall_clock_seconds", "retries",
                    "gate_outcomes", "context_package_hashes", "rendered_hashes",
                    "included_source_paths", "context_byte_counts", "context_char_counts",
                    "context_estimated_tokens", "provider_input_tokens"):
            assert key in m
    # S selected only relevant files; F includes distractors
    assert "utils/text_utils.py" not in m_s["included_source_paths"]
    assert "utils/text_utils.py" in m_f["included_source_paths"]
    assert m_f["context_byte_counts"][0] > m_s["context_byte_counts"][0] > 0
    # strategy identity preserved per condition
    assert m_s["strategy"] == pilot.CONDITION_SELECTED
    assert m_f["strategy"] == pilot.CONDITION_FULL_PUBLIC


def test_04d_pair_run_harness_links_runs_to_one_experiment(tmp_path: Path):
    deps_s, product, task = pilot.condition_deps(strategy=pilot.CONDITION_SELECTED)
    deps_f, _, _ = pilot.condition_deps(strategy=pilot.CONDITION_FULL_PUBLIC)
    manifest = _pair_manifest()
    experiment_id = str(manifest.id)
    _preregister_into(tmp_path / "s.db", manifest)
    _preregister_into(tmp_path / "f.db", manifest)
    out_s = MinimalFactoryController.run(product, task, deps_s, db_path=tmp_path / "s.db",
                                         experiment_id=experiment_id)
    out_f = MinimalFactoryController.run(product, task, deps_f, db_path=tmp_path / "f.db",
                                         experiment_id=experiment_id)
    for db in (tmp_path / "s.db", tmp_path / "f.db"):
        conn = sqlite3.connect(db)
        try:
            exp_ids = {r[0] for r in conn.execute("SELECT experiment_id FROM factory_runs").fetchall()}
            ev_ids = {r[0] for r in conn.execute(
                "SELECT DISTINCT experiment_id FROM evidence_entries").fetchall() if r[0]}
        finally:
            conn.close()
        assert exp_ids == {experiment_id}
        assert ev_ids == {experiment_id}


def test_04d_order_recorded_in_harness(tmp_path: Path):
    from redi_lab.experiments.context_pilot import run_pair

    manifest = _pair_manifest()
    pair = run_pair(manifest=manifest, order=("selected_v0", "full_public_v0"),
                    db_dir=tmp_path)
    experiment_id = pair.experiment_id
    assert pair.order == ["selected_v0", "full_public_v0"]
    assert pair.experiment_id == experiment_id
    assert pair.conditions["selected_v0"].order_index == 0
    assert pair.conditions["full_public_v0"].order_index == 1
    assert pair.label.startswith("PILOT")


def test_04d_manifest_preregistration_roundtrip(tmp_path: Path):
    from redi_lab.domain import ExperimentManifest
    from redi_lab.experiments.context_pilot import build_experiment_manifest
    from redi_lab.persistence import EvidenceLedger, ExperimentRepository, bootstrap_schema

    db = tmp_path / "exp.db"
    conn = sqlite3.connect(db)
    bootstrap_schema(conn)
    ledger = EvidenceLedger(conn)
    repo = ExperimentRepository(conn, ledger)
    manifest = build_experiment_manifest(model="gpt-4o-mini")
    assert manifest.hypothesis_reference == "H2"
    repo.store_preregistered(manifest)
    row = conn.execute(
        "SELECT title, hypothesis_reference, payload_json FROM experiment_manifests WHERE id = ?",
        (str(manifest.id),)).fetchone()
    assert row[1] == "H2"
    payload = json.loads(row[2])
    assert payload["arms"]["S"]["context_strategy"] == "selected_v0"
    assert payload["arms"]["F"]["context_strategy"] == "full_public_v0"
    assert payload["validity_controls"]["same_worker"] is True
    assert payload["validity_controls"]["hidden_tests_excluded_from_all_context"] is True
    conn.close()


def test_04c_replay_both_conditions_twice(tmp_path: Path):
    deps_s, product, task = pilot.condition_deps(strategy=pilot.CONDITION_SELECTED)
    deps_f, _, _ = pilot.condition_deps(strategy=pilot.CONDITION_FULL_PUBLIC)
    manifest = _pair_manifest()
    experiment_id = str(manifest.id)
    _preregister_into(tmp_path / "s.db", manifest)
    _preregister_into(tmp_path / "f.db", manifest)
    out_s = MinimalFactoryController.run(product, task, deps_s, db_path=tmp_path / "s.db",
                                         experiment_id=experiment_id)
    out_f = MinimalFactoryController.run(product, task, deps_f, db_path=tmp_path / "f.db",
                                         experiment_id=experiment_id)

    import redi_lab.runtime.live_provider_adapter as lpa
    calls = {"n": 0}
    original = lpa._sdk_available

    def _counting():
        calls["n"] += 1
        return False

    runs = {"s": out_s, "f": out_f}

    def replay_snapshot(db):
        tag = Path(db).stem
        reader = ReplayReader(db)
        try:
            r = reader.replay_factory_run(runs[tag].factory_run_id)
        finally:
            reader.close()
        evs = [e for e in r.run_evidences if getattr(e, "node_id", "") == "N_BUILDER_0"]
        cps = [dict(e.metadata["context_package"]) for e in evs]
        return r, cps

    lpa._sdk_available = _counting
    try:
        s_r1, s_c1 = replay_snapshot(tmp_path / "s.db")
        s_r2, s_c2 = replay_snapshot(tmp_path / "s.db")
        f_r1, f_c1 = replay_snapshot(tmp_path / "f.db")
        f_r2, f_c2 = replay_snapshot(tmp_path / "f.db")
    finally:
        lpa._sdk_available = original

    for r in (s_r1, s_r2, f_r1, f_r2):
        assert r.integrity_status == "OK"
    assert s_c1 == s_c2 and f_c1 == f_c2
    assert s_c1[0]["strategy_id"] == "selected_v0"
    assert f_c1[0]["strategy_id"] == "full_public_v0"
    assert calls["n"] == 0


def test_04d_isolation_both_conditions(tmp_path: Path):
    deps_s, product, task = pilot.condition_deps(strategy=pilot.CONDITION_SELECTED)
    deps_f, _, _ = pilot.condition_deps(strategy=pilot.CONDITION_FULL_PUBLIC)
    manifest = _pair_manifest()
    experiment_id = str(manifest.id)
    _preregister_into(tmp_path / "s.db", manifest)
    _preregister_into(tmp_path / "f.db", manifest)
    out_s = MinimalFactoryController.run(product, task, deps_s, db_path=tmp_path / "s.db",
                                         experiment_id=experiment_id)
    out_f = MinimalFactoryController.run(product, task, deps_f, db_path=tmp_path / "f.db",
                                         experiment_id=experiment_id)
    for db in (tmp_path / "s.db", tmp_path / "f.db"):
        conn = sqlite3.connect(db)
        try:
            rows = conn.execute("SELECT payload_json FROM evidence_entries").fetchall()
            blob = "".join(r[0] or "" for r in rows).lower()
            for marker in ("slugify_pkg_hidden_tests", "test_hidden", "api_key", "sk-abcdef"):
                assert marker not in blob
            import re
            assert not re.search(r"[a-z]:\\\\", blob)
        finally:
            conn.close()


def test_04c_live_runner_env_gates_fail_closed(monkeypatch, capsys):
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "run_redi_05_context_pilot",
        Path(__file__).resolve().parents[2] / "scripts" / "run_redi_05_context_pilot.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    monkeypatch.delenv("REDI_LIVE_PROVIDER", raising=False)
    monkeypatch.delenv("REDI_OPENAI_API_KEY", raising=False)
    rc = mod.main()
    out1 = capsys.readouterr().out
    assert rc == 0
    assert "REDI-05 PILOT BLOCKED: REDI_LIVE_PROVIDER not set" in out1

    monkeypatch.setenv("REDI_LIVE_PROVIDER", "1")
    rc = mod.main()
    out2 = capsys.readouterr().out
    assert rc == 0
    assert "REDI-05 PILOT BLOCKED: REDI_OPENAI_API_KEY not set" in out2
    assert "sk-" not in out1 + out2
