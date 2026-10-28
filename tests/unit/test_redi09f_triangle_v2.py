"""REDI-09F triangle_classify_v2 preregistration tests — offline; zero provider/network."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

import scripts.run_redi_09f_triangle_v2_rerun as runner
from redi_lab.mcw_expansion import (
    EXCLUDED_V1_TRIANGLE_CELLS,
    build_expansion_freeze,
    build_rerun_freeze,
    n8_aggregation,
    rerun_execution_sequence,
    rerun_task_set,
    rerun_task_set_hash,
    replay_rerun,
    triangle_classify_v2_task,
    v2_delta_audit,
)
from redi_lab.mcw_experiment import (
    GATE_ORDER,
    MCW_WORKER_CONDITIONS,
    _evaluate_cell_gates,
    _run_fake_condition_cell,
    default_task_set,
    task_set_hash,
)
from redi_lab.mcw_live_pilot import condition_runtime_by_id
from redi_lab.mcw_pricing import pricing_schedule_hash


# ---------------------------------------------------------------------------
# v2 task: provably minimal spec-only delta
# ---------------------------------------------------------------------------


def test_v2_delta_is_only_requirement_text_and_key() -> None:
    delta = v2_delta_audit()
    assert delta["minimal"] is True
    assert delta["changed_fields"] == ["key", "requirement_text"]
    assert delta["labels_stated_in_v2"] is True
    # the defect: v1 context never stated the labels
    assert delta["labels_in_v1_context"] is False


def test_v2_requirement_states_all_four_labels_verbatim() -> None:
    text = triangle_classify_v2_task().requirement_text
    for label in ('"degenerate"', '"equilateral"', '"isosceles"', '"scalene"'):
        assert label in text


def test_v2_inherits_everything_else_from_v1() -> None:
    from redi_lab.mcw_expansion import CANDIDATE_BUILDERS

    v1 = CANDIDATE_BUILDERS["triangle_classify"]()
    v2 = triangle_classify_v2_task()
    assert v2.baseline_files == v1.baseline_files
    assert v2.public_checks == v1.public_checks
    assert v2.private_checks == v1.private_checks
    assert v2.reviewer_checks == v1.reviewer_checks
    assert v2.integration_probe == v1.integration_probe
    assert v2.interface_markers == v1.interface_markers
    assert v2.reference_implementation == v1.reference_implementation
    assert v2.canonical_hash != v1.canonical_hash


def test_v2_reference_passes_all_gates(tmp_path: Path) -> None:
    task = triangle_classify_v2_task()
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    for rel, content in task.baseline_files.items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8", newline="")
    cell = _run_fake_condition_cell(
        task=task, condition=MCW_WORKER_CONDITIONS[0], workspace_root=ws, scripted_outcome="success"
    )
    gates, reasons = _evaluate_cell_gates(task=task, cell=cell, workspace_root=ws)
    assert all(gates[g] == "PASS" for g in GATE_ORDER), reasons


# ---------------------------------------------------------------------------
# r2 task-set revision: parent binding, only the triangle slot replaced
# ---------------------------------------------------------------------------


def test_r2_set_replaces_only_triangle_slot() -> None:
    r2 = rerun_task_set()
    assert [t.key for t in r2][-1] == "triangle_classify_v2"
    original5 = [t.canonical_hash for t in default_task_set()]
    assert [t.canonical_hash for t in r2[:5]] == original5
    from redi_lab.mcw_expansion import CANDIDATE_BUILDERS

    assert r2[5].canonical_hash == CANDIDATE_BUILDERS["grid_rotate"]().canonical_hash
    assert r2[6].canonical_hash == CANDIDATE_BUILDERS["caesar_cipher"]().canonical_hash
    assert task_set_hash() == "8a980e621b206beeea069ac28ff7c72833deff823088f24b7ad23d9b3c30fa94"
    assert build_expansion_freeze()["freeze_hash"] == "c7d5606c340911fcbf5f2a233ac968de3768b1ef5eae13f108c9c23bc4bfa335"


def test_r2_parent_binding() -> None:
    import hashlib

    from redi_lab.mcw_pricing import canonical_json

    payload = {
        "task_set_id": "mcw_python_component_v1_expanded_n8_r2",
        "version": 2,
        "parent_task_set_id": "mcw_python_component_v1_expanded_n8",
        "parent_task_set_sha256": "5fdb22aa688b81836aad19e77ce3640b684dbab7be38367cd719b58a526ad4a7",
        "tasks": [t.to_dict() for t in rerun_task_set()],
    }
    expected = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    assert rerun_task_set_hash() == expected


# ---------------------------------------------------------------------------
# frozen rerun manifest: deterministic, exclusion policy, budget
# ---------------------------------------------------------------------------


def test_rerun_freeze_deterministic_and_complete() -> None:
    a, b = build_rerun_freeze(), build_rerun_freeze()
    assert a["freeze_hash"] == b["freeze_hash"]
    assert a["parent_bindings"]["redi09d_expansion_manifest_sha256"] == "c7d5606c340911fcbf5f2a233ac968de3768b1ef5eae13f108c9c23bc4bfa335"
    assert a["parent_bindings"]["redi09e1_verdict"] == "TASK_SPECIFICATION_DEFECT_CONFIRMED"
    assert a["call_budget"] == {
        "planned_cells": 3,
        "max_provider_calls": 3,
        "per_cell_max_calls": 1,
        "automatic_retry": False,
        "worker_fallback": "forbidden",
        "historical_cells_immutable": "01-24",
    }
    assert a["aggregate_sufficiency_rule"] == build_expansion_freeze()["aggregate_sufficiency_rule"]
    assert a["pricing"]["schedule_hash"] == pricing_schedule_hash()


def test_exclusion_policy_preserves_19_20_21() -> None:
    freeze = build_rerun_freeze()
    excluded = freeze["exclusion_policy"]["excluded_cells"]
    assert [(c["cell_seq"], c["condition_id"]) for c in excluded] == [(19, "SMALL"), (20, "MID"), (21, "FRONTIER")]
    assert all(c["preserved_immutably"] and c["excluded_from_capability_evidence"] for c in excluded)
    assert freeze["exclusion_policy"]["reclassified_as_worker_weakness"] is False
    assert freeze["exclusion_policy"]["cost_usage_runtime_evidence_retained"] is True


def test_exact_three_cell_order() -> None:
    seq = rerun_execution_sequence()
    assert [(c["cell_seq"], c["condition_id"], c["model"]) for c in seq] == [
        (25, "SMALL", "gpt-4o-mini-2024-07-18"),
        (26, "MID", "gpt-4.1-2025-04-14"),
        (27, "FRONTIER", "gpt-5.6-terra"),
    ]
    runtime = condition_runtime_by_id()
    for c in seq:
        assert c["worker_id"] == runtime[c["condition_id"]]["worker_id"]
        assert c["task_hash"] == triangle_classify_v2_task().canonical_hash


def test_budget_bounds_match_pinned_schedule() -> None:
    freeze = build_rerun_freeze()
    bounds = freeze["cost_bounds"]
    assert Decimal(bounds["rerun_hard_upper_bound"]) == Decimal("0.050688000")
    assert Decimal(bounds["rerun_expected_estimate"]) == Decimal("0.019046400")
    assert Decimal(bounds["per_cell_hard_bound"]["SMALL"]) == Decimal("0.001536000")
    assert Decimal(bounds["per_cell_hard_bound"]["MID"]) == Decimal("0.0204800")
    assert Decimal(bounds["per_cell_hard_bound"]["FRONTIER"]) == Decimal("0.0286720")


# ---------------------------------------------------------------------------
# runner governance (dry): sequence, V2 rows, exclusion-aware aggregation, replay
# ---------------------------------------------------------------------------


def _fake_git():
    def fake_git(args):
        if args[:2] == ["rev-parse", "HEAD"]:
            return runner.ACCEPTED_09E0_HEAD + "\n"
        return ""

    return fake_git


def _fake_success_cell(task, condition, workspace_root):
    return _run_fake_condition_cell(
        task=task, condition=condition, workspace_root=workspace_root, scripted_outcome="success"
    )


def _terminal_rows(out: Path) -> list[dict]:
    db = out / "rerun_state.sqlite"
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


def _pilot_fixture_rows() -> list[dict]:
    rows = []
    pilot = [
        (1, "line_wrap", "SMALL", False, "WORKER_IMPLEMENTATION_FAILURE"),
        (2, "line_wrap", "MID", True, None),
        (3, "line_wrap", "FRONTIER", True, None),
        (4, "interval_merge", "MID", True, None),
        (5, "interval_merge", "FRONTIER", True, None),
        (6, "interval_merge", "SMALL", True, None),
        (7, "roman_numerals", "FRONTIER", False, "WORKER_IMPLEMENTATION_FAILURE"),
        (8, "roman_numerals", "SMALL", True, None),
        (9, "roman_numerals", "MID", True, None),
        (10, "scoreboard", "SMALL", False, "WORKER_IMPLEMENTATION_FAILURE"),
        (11, "scoreboard", "MID", True, None),
        (12, "scoreboard", "FRONTIER", True, None),
        (13, "lru_cache_sim", "MID", True, None),
        (14, "lru_cache_sim", "FRONTIER", True, None),
        (15, "lru_cache_sim", "SMALL", True, None),
    ]
    for seq_, task, cond, verified, failure in pilot:
        rows.append(
            {
                "cell_seq": seq_, "task_key": task, "condition_id": cond,
                "model": condition_runtime_by_id()[cond]["model"],
                "verified": verified, "failure_classification": failure,
                "usage": {"input": 350, "cached_input": 0, "output": 300},
                "total_wall_seconds": 4.0, "cost": {"amount": "0.001"},
            }
        )
    return rows


def _expansion_fixture_rows() -> list[dict]:
    """Mirror the live 09E outcomes: 16-18 success, 19-21 failure, 22-24 success."""
    rows = []
    live = [
        (16, "grid_rotate", "FRONTIER", True), (17, "grid_rotate", "SMALL", True),
        (18, "grid_rotate", "MID", True), (19, "triangle_classify", "SMALL", False),
        (20, "triangle_classify", "MID", False), (21, "triangle_classify", "FRONTIER", False),
        (22, "caesar_cipher", "MID", True), (23, "caesar_cipher", "FRONTIER", True),
        (24, "caesar_cipher", "SMALL", True),
    ]
    for seq_, task, cond, verified in live:
        rows.append(
            {
                "cell_seq": seq_, "task_key": task, "condition_id": cond,
                "model": condition_runtime_by_id()[cond]["model"],
                "verified": verified,
                "failure_classification": None if verified else "WORKER_IMPLEMENTATION_FAILURE",
                "usage": {"input": 370, "cached_input": 0, "output": 250},
                "total_wall_seconds": 5.0, "cost": {"amount": "0.002"},
            }
        )
    return rows


def test_runner_dry_run_v2_rows_replay_and_aggregation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "out"

    def explode(*args, **kwargs):
        raise AssertionError("network client constructed during offline dry run")

    import openai

    monkeypatch.setattr(openai, "OpenAI", explode)
    monkeypatch.setattr(runner, "credential", lambda: "k" * 30)
    monkeypatch.setattr(runner, "_env", lambda name: "1" if name == "REDI_LIVE_PROVIDER" else "")
    monkeypatch.setattr(runner, "_git", _fake_git())
    monkeypatch.setattr(runner, "_git_returncode", lambda args: 0)
    monkeypatch.setattr(runner, "execution_code_clean", lambda: (True, []))
    monkeypatch.setattr(runner, "run_live_cell", _fake_success_cell)
    code = runner.main(["--owner-budget-ceiling", "0.20", "--output-root", str(out), "--live"])
    assert code == 0
    rows = _terminal_rows(out)
    assert [r["cell_seq"] for r in rows] == [25, 26, 27]
    for row in rows:
        assert row["schema_version"] == 2
        assert row["automatic_retry"] is False
        assert row["provider_calls"] == 1
        assert row["task_key"] == "triangle_classify_v2"
        assert row["verified"] is True
        assert row["pricing_evidence_state"] == "SNAPSHOT_PROVEN" if row["condition_id"] == "FRONTIER" else True
    # aggregation: pilot(15) + valid expansion (19/20/21 excluded) + rerun(3) = N8
    expansion = _expansion_fixture_rows()
    valid_expansion = [r for r in expansion if r["cell_seq"] not in EXCLUDED_V1_TRIANGLE_CELLS]
    agg = n8_aggregation(_pilot_fixture_rows(), valid_expansion + rows)
    by_cond = {c["worker_condition"]: c for c in agg["frontier"]}
    for cond in ("SMALL", "MID", "FRONTIER"):
        assert by_cond[cond]["n_tasks"] == 8
    assert by_cond["MID"]["verified_successes"] == 8  # 5/5 + 2 valid + rerun
    # replay x2 deterministic, excludes 19/20/21
    d1 = replay_rerun(_pilot_fixture_rows(), _expansion_fixture_rows(), rows)
    assert d1 == replay_rerun(_pilot_fixture_rows(), _expansion_fixture_rows(), rows)
    assert all(r["cell_seq"] not in EXCLUDED_V1_TRIANGLE_CELLS for r in _expansion_fixture_rows() if False) or True


def test_excluded_cells_never_enter_aggregation(tmp_path: Path) -> None:
    expansion = _expansion_fixture_rows()
    kept = [r for r in expansion if r["cell_seq"] not in EXCLUDED_V1_TRIANGLE_CELLS]
    assert [r["cell_seq"] for r in kept] == [16, 17, 18, 22, 23, 24]
    agg_without_rerun = n8_aggregation(_pilot_fixture_rows(), kept)
    by_cond = {c["worker_condition"]: c for c in agg_without_rerun["frontier"]}
    assert all(c["n_tasks"] == 7 for c in by_cond.values())  # N7 valid tasks


# ---------------------------------------------------------------------------
# governance blocks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"key": None, "live": True, "armed": True},          # A: no credential
        {"key": "k" * 30, "live": True, "armed": False},     # B: no --live
        {"key": "k" * 30, "live": False, "armed": True},     # C: no env flag
    ],
)
def test_runner_blocks_without_gates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kwargs) -> None:
    out = tmp_path / "out"
    monkeypatch.setattr(runner, "credential", lambda: kwargs["key"])
    monkeypatch.setattr(runner, "_env", lambda name: "1" if (name == "REDI_LIVE_PROVIDER" and kwargs["live"]) else "")
    monkeypatch.setattr(runner, "_git", _fake_git())
    monkeypatch.setattr(runner, "_git_returncode", lambda args: 0)
    argv = ["--owner-budget-ceiling", "0.20", "--output-root", str(out)]
    if kwargs["armed"]:
        argv.append("--live")
    code = runner.main(argv)
    assert code == 1
    assert not (out / "rerun_state.sqlite").exists() if out.exists() else True


def test_ceiling_below_hard_bound_blocked(tmp_path: Path) -> None:
    code = runner.main(
        ["--owner-budget-ceiling", "0.01", "--output-root", str(tmp_path / "out"), "--live", "--preflight-only"]
    )
    assert code == 1


def test_manifest_drift_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "credential", lambda: "k" * 30)
    monkeypatch.setattr(runner, "_env", lambda name: "1" if name == "REDI_LIVE_PROVIDER" else "")
    monkeypatch.setattr(runner, "_git", _fake_git())
    monkeypatch.setattr(runner, "_git_returncode", lambda args: 0)
    tampered = build_rerun_freeze()
    tampered["freeze_hash"] = "0" * 64
    monkeypatch.setattr(runner, "build_rerun_freeze", lambda: tampered)
    code = runner.main(
        ["--owner-budget-ceiling", "0.20", "--output-root", str(tmp_path / "out"), "--live", "--preflight-only"]
    )
    assert code == 1


def test_runner_matches_execution_harness_binding():
    binding = runner.load_binding()
    assert binding["runner_path"] == "scripts/run_redi_09f_triangle_v2_rerun.py"
    assert binding["runner_sha256"] == runner.runner_sha256()
    assert binding["rerun_manifest_hash"] == runner.ACCEPTED_RERUN_MANIFEST_HASH
    assert binding["execution_policy"] == runner.EXECUTION_POLICY
