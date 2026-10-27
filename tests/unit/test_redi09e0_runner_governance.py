"""REDI-09E0 live-runner governance tests — offline; zero provider/network."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

import scripts.run_redi_09e_live_expansion as runner
from redi_lab.mcw_expansion import DEFAULT_EXPANSION_DRY_OUTCOMES, ExpansionController
from redi_lab.mcw_experiment import GATE_ORDER, _run_fake_condition_cell, MCW_WORKER_CONDITIONS


def _no_state(output_root: Path) -> bool:
    return not output_root.exists() or not any(output_root.rglob("*"))


def _run_main(monkeypatch, tmp_path, args, *, key=None, live=None, head=None, manifest=None, ancestor=0):
    """Invoke runner.main() with controlled env/identity; returns exit code."""
    monkeypatch.setattr(runner, "credential", lambda: key)
    monkeypatch.setattr(runner, "_env", lambda name: "1" if (name == "REDI_LIVE_PROVIDER" and live) else "")
    monkeypatch.setattr(runner, "_git", _fake_git(head))
    monkeypatch.setattr(runner, "_git_returncode", lambda args: ancestor)
    if manifest is not None:
        monkeypatch.setattr(runner, "build_expansion_freeze", lambda: manifest)
    return runner.main(["--owner-budget-ceiling", "0.20", "--output-root", str(tmp_path / "out"), *args])


def _fake_git(head):
    def fake_git(args):
        if args[:2] == ["rev-parse", "HEAD"]:
            return (head or runner.ACCEPTED_HEAD) + "\n"
        if args[:2] == ["log", "--format=%H %P"]:
            return f"{runner.ACCEPTED_HEAD} {runner.ACCEPTED_09D_COMMIT}\n"
        if args[:1] == ["status"]:
            return ""
        return ""

    return fake_git


def _fake_success_cell(task, condition, workspace_root):
    result = _run_fake_condition_cell(
        task=task, condition=condition, workspace_root=workspace_root, scripted_outcome="success"
    )
    return result


def _fake_start_failure_cell(task, condition, workspace_root):
    raise RuntimeError("simulated provider start failure (test H)")


# A. no key: exits before state, zero calls, zero cells consumed
def test_a_missing_credential_consumes_nothing(tmp_path, monkeypatch):
    out = tmp_path / "out"
    code = _run_main(monkeypatch, tmp_path, ["--live"], key=None, live=True)
    assert code == 1
    assert _no_state(out)


# B. no --live: blocked, zero state (even with credential and env flag present)
def test_b_missing_live_flag_blocked(tmp_path, monkeypatch):
    out = tmp_path / "out"
    code = _run_main(monkeypatch, tmp_path, [], key="k" * 30, live=True)
    assert code == 1
    assert _no_state(out)


# C. no REDI_LIVE_PROVIDER: blocked, zero state
def test_c_missing_env_flag_blocked(tmp_path, monkeypatch):
    out = tmp_path / "out"
    code = _run_main(monkeypatch, tmp_path, ["--live"], key="k" * 30, live=False)
    assert code == 1
    assert _no_state(out)


# D. ceiling below hard bound: blocked
def test_d_ceiling_below_hard_bound_blocked(tmp_path, monkeypatch):
    out = tmp_path / "out"
    code = runner.main(
        ["--owner-budget-ceiling", "0.10", "--output-root", str(tmp_path / "out"), "--live", "--preflight-only"]
    )
    assert code == 1
    assert _no_state(tmp_path / "out")


# E. broken repository identity (evidence HEAD not an ancestor): blocked
def test_e_wrong_head_blocked(tmp_path, monkeypatch):
    out = tmp_path / "out"
    code = _run_main(monkeypatch, tmp_path, ["--live"], key="k" * 30, live=True, head="c82c47e3b634a13ae084d37ef368b4a193569e7d", ancestor=1)
    assert code == 1
    assert _no_state(out)


# F. manifest drift: blocked
def test_f_manifest_drift_blocked(tmp_path, monkeypatch):
    out = tmp_path / "out"
    tampered = runner.build_expansion_freeze()
    tampered["freeze_hash"] = "0" * 64
    code = _run_main(monkeypatch, tmp_path, ["--live"], key="k" * 30, live=True, manifest=tampered)
    assert code == 1
    assert _no_state(out)


# G. dry successful 9-cell run: exact sequence, V2 rows, one call slot/cell, replay clean
def test_g_dry_nine_cell_run_v2_rows_and_replay(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setattr(runner, "credential", lambda: "k" * 30)
    monkeypatch.setattr(runner, "_env", lambda name: "1" if name == "REDI_LIVE_PROVIDER" else "")
    monkeypatch.setattr(runner, "_git", _fake_git(None))
    monkeypatch.setattr(runner, "_git_returncode", lambda args: 0)
    monkeypatch.setattr(runner, "run_live_cell", _fake_success_cell)
    monkeypatch.chdir(Path(runner.ROOT))  # historical N5 DB path resolution
    code = runner.main(["--owner-budget-ceiling", "0.20", "--output-root", str(out), "--live"])
    assert code == 0
    rows = _terminal_rows(out)
    assert [r["cell_seq"] for r in rows] == list(range(16, 25))
    for row in rows:
        assert row["provider_calls"] == 1 and row["network_calls"] == 1
        assert row["automatic_retry"] is False
        assert row["schema_version"] == 2
        assert row["pricing_evidence_state"] in (
            "SNAPSHOT_PROVEN", "FAMILY_MAPPING_UNPROVEN_SNAPSHOT",
        )
        assert set(row["gates"]) == set(GATE_ORDER)
        assert row["parsed_patch_present"] is True
        assert row["patch_artifact_sha256"]
    lifecycle = _lifecycle_states(out)
    assert [s for _, s in sorted(lifecycle.items())] == ["TERMINAL"] * 9
    # replay must be deterministic on the persisted state
    from redi_lab.mcw_expansion import load_pilot_rows_v1, replay_n8

    pilot = load_pilot_rows_v1(runner.PILOT_STATE_DB)
    assert runner.load_pilot_rows_v1(runner.PILOT_STATE_DB) == pilot
    d1 = replay_n8(pilot, rows)
    assert d1 == replay_n8(pilot, rows)


# H. simulated provider-start failure: cell stays PENDING, zero cells consumed
def test_h_provider_start_failure_leaves_cell_pending(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setattr(runner, "credential", lambda: "k" * 30)
    monkeypatch.setattr(runner, "_env", lambda name: "1" if name == "REDI_LIVE_PROVIDER" else "")
    monkeypatch.setattr(runner, "_git", _fake_git(None))
    monkeypatch.setattr(runner, "_git_returncode", lambda args: 0)
    monkeypatch.setattr(runner, "run_live_cell", _fake_start_failure_cell)
    monkeypatch.chdir(Path(runner.ROOT))
    code = runner.main(["--owner-budget-ceiling", "0.20", "--output-root", str(out), "--live"])
    assert code == 1
    # the armed run may hold durable empty state, but the cell is NOT consumed
    assert _terminal_rows(out) == []
    assert _lifecycle_states(out).get(16) == "PENDING"


# interrupted cell (CALL_STARTED without TERMINAL) requires adjudication, never silent retry
def test_interrupted_cell_requires_adjudication(tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir(parents=True)
    lifecycle = runner.CellLifecycle(out / "cell_lifecycle.sqlite")
    lifecycle.set_state(16, "CALL_STARTED")
    monkeypatch.setattr(runner, "credential", lambda: "k" * 30)
    monkeypatch.setattr(runner, "_env", lambda name: "1" if name == "REDI_LIVE_PROVIDER" else "")
    monkeypatch.setattr(runner, "_git", _fake_git(None))
    monkeypatch.setattr(runner, "_git_returncode", lambda args: 0)
    monkeypatch.chdir(Path(runner.ROOT))
    code = runner.main(["--owner-budget-ceiling", "0.20", "--output-root", str(out), "--live"])
    assert code == 1
    assert _terminal_rows(out) == []


# runner identity must match the tracked execution-harness binding
def test_runner_matches_execution_harness_binding():
    binding = runner.load_binding()
    assert binding["runner_path"] == "scripts/run_redi_09e_live_expansion.py"
    assert binding["runner_sha256"] == runner.runner_sha256()
    assert binding["parent_manifest_hash"] == runner.ACCEPTED_MANIFEST_HASH
    assert binding["redi09d_design_commit"] == runner.ACCEPTED_09D_COMMIT
    assert binding["execution_head"] == runner.ACCEPTED_HEAD
    assert binding["execution_policy"] == runner.EXECUTION_POLICY


# preflight-only mutates nothing
def test_preflight_only_zero_state(tmp_path, monkeypatch):
    out = tmp_path / "out"
    code = _run_main(monkeypatch, tmp_path, ["--live", "--preflight-only"], key="k" * 30, live=True)
    assert code in (0, 1)
    assert _no_state(out)


# rehearsal parity guard: the scientific design must equal the frozen freeze
def test_scientific_design_untouched_by_runner():
    from redi_lab.mcw_expansion import build_expansion_freeze, expansion_execution_sequence

    freeze = build_expansion_freeze()
    assert freeze["freeze_hash"] == runner.ACCEPTED_MANIFEST_HASH
    assert [c["cell_seq"] for c in expansion_execution_sequence()] == list(range(16, 25))
    assert freeze["call_budget"]["max_provider_calls"] == 9
    assert freeze["call_budget"]["automatic_retry"] is False


def _terminal_rows(out: Path) -> list[dict]:
    db = out / "expansion_state.sqlite"
    if not db.exists():
        return []
    conn = sqlite3.connect(db)
    try:
        return [
            json.loads(rj)
            for (rj,) in conn.execute(
                "SELECT row_json FROM expansion_cells WHERE status='completed' ORDER BY cell_seq"
            )
        ]
    finally:
        conn.close()


def _lifecycle_states(out: Path) -> dict:
    db = out / "cell_lifecycle.sqlite"
    if not db.exists():
        return {}
    conn = sqlite3.connect(db)
    try:
        return {seq: state for seq, state in conn.execute("SELECT cell_seq, state FROM cell_states")}
    finally:
        conn.close()
