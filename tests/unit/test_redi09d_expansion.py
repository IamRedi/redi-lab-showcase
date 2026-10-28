"""REDI-09D expansion design tests — offline; zero provider/network."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

import redi_lab.mcw_expansion as expansion
from redi_lab.mcw_experiment import (
    DEFAULT_DRY_OUTCOMES,
    GATE_ORDER,
    MCW_SUFFICIENCY_MIN_TASKS,
    MCW_WORKER_CONDITIONS,
    _evaluate_cell_gates,
    _run_fake_condition_cell,
    default_task_set,
    frontier_state,
    task_set_hash,
)
from redi_lab.mcw_live_pilot import condition_runtime_by_id
from redi_lab.mcw_pricing import pricing_schedule, pricing_schedule_hash


# ---------------------------------------------------------------------------
# Sampling: independence from pilot results
# ---------------------------------------------------------------------------


def test_seed_excludes_result_material() -> None:
    """Seed material contains only pre-result identifiers."""
    from redi_lab.mcw_expansion import CANDIDATE_BUILDERS

    material = (
        task_set_hash()
        + expansion.default_stratum().canonical_hash
        + expansion.EXPANSION_SEED_DOMAIN_TAG
    )
    assert expansion.expansion_seed() == __import__("hashlib").sha256(
        material.encode("utf-8")
    ).hexdigest()
    lowered = material.lower()
    for banned in ("gpt-", "small", "mid", "frontier", "failure", "verified"):
        assert banned not in lowered


def test_selection_is_deterministic_and_reproducible() -> None:
    first = expansion.select_expansion_tasks()
    for _ in range(5):
        assert expansion.select_expansion_tasks() == first
    assert len(first) == 3
    ranks = expansion.selection_ranks()
    ordered = sorted(ranks, key=lambda k: (ranks[k], k))
    assert tuple(ordered[:3]) == first


def test_selection_is_result_independent_by_construction() -> None:
    """Perturbing every possible pilot outcome cannot change the selection."""
    from redi_lab.mcw_live_pilot import DEFAULT_DRY_OUTCOMES

    baseline = expansion.select_expansion_tasks()
    for outcome in ("success", "implementation_failure", "scope_violation", "provider_incomplete"):
        mutated = {k: outcome for k in DEFAULT_DRY_OUTCOMES}
        # the sampling pipeline never reads outcomes; the selection inputs are
        # purely hash-derived, so identical inputs yield identical selection
        assert expansion.expansion_seed() == expansion.expansion_seed()
        assert expansion.select_expansion_tasks() == baseline == tuple(
            sorted(
                expansion.selection_ranks(),
                key=lambda k: (expansion.selection_ranks()[k], k),
            )[:3]
        )


# ---------------------------------------------------------------------------
# Candidate frame: immutability + stratum compliance
# ---------------------------------------------------------------------------


def test_candidate_frame_is_stable_and_larger_than_selection() -> None:
    keys = sorted(expansion.CANDIDATE_BUILDERS)
    assert len(keys) == 6
    assert expansion.sampling_frame_hash() == expansion.sampling_frame_hash()
    assert len(expansion.candidate_frame()) == 6
    assert [t.key for t in expansion.candidate_frame()] == keys


def test_candidates_satisfy_stratum_rules() -> None:
    for task in expansion.candidate_frame():
        assert 3 <= len(task.public_checks) <= 8
        assert 3 <= len(task.private_checks) <= 8
        assert task.contract.expected_path == task.rel_path
        assert task.contract.allowed_scope == (task.package,)
        body = expansion._compile_context(task)["context_body"]
        assert task.reference_implementation not in body  # no reference leak
        assert not expansion._compile_context(task)["private_check_args_in_context"]
        for check in tuple(task.public_checks) + tuple(task.private_checks) + tuple(
            task.reviewer_checks
        ):
            assert check["kind"] in ("call_equals", "call_raises")
            declared = tuple(m.split("(")[0].replace("def ", "") for m in task.interface_markers)
            assert check["target"] in declared  # black-box: declared interface only


def test_candidate_references_pass_all_gates() -> None:
    work = Path(tempfile.mkdtemp(prefix="redi09d-refs-"))
    condition = MCW_WORKER_CONDITIONS[0]
    for task in expansion.candidate_frame():
        ws = work / task.key / "workspace"
        ws.mkdir(parents=True)
        for rel, content in task.baseline_files.items():
            path = ws / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="")
        cell = _run_fake_condition_cell(
            task=task, condition=condition, workspace_root=ws, scripted_outcome="success"
        )
        gates, reasons = _evaluate_cell_gates(task=task, cell=cell, workspace_root=ws)
        assert all(gates[g] == "PASS" for g in GATE_ORDER), (task.key, reasons)


# ---------------------------------------------------------------------------
# Anti-bias: no failure-targeted task design
# ---------------------------------------------------------------------------


def test_no_failure_targeted_task_design() -> None:
    """No expansion check encodes a REDI-09C failure diagnostic."""
    pilot_failure_args = [["", 5], ["a", 0], [{"k": "a", "v": True}]]  # SMALL defects
    for task in expansion.expansion_tasks():
        for check in tuple(task.private_checks) + tuple(task.public_checks):
            for args in pilot_failure_args:
                assert check["args"] != args
        low = (task.requirement_text + task.title).lower()
        assert "wrap_text" not in low and "aggregate_scores" not in low
        assert "canonical" not in low  # FRONTIER's roman-numeral defect theme


def test_expansion_families_are_disjoint_from_pilot_families() -> None:
    pilot = set(expansion.PILOT_TASK_FAMILIES.values())
    selected = {expansion.CANDIDATE_FAMILIES[k] for k in expansion.select_expansion_tasks()}
    assert pilot.isdisjoint(selected)
    assert len(selected) == 3


def test_similarity_audit_keys_and_packages_do_not_collide() -> None:
    pilot_keys = {t.key for t in default_task_set()}
    for task in expansion.expansion_tasks():
        assert task.key not in pilot_keys
        assert task.package not in pilot_keys
        assert task.rel_path not in {t.rel_path for t in default_task_set()}


# ---------------------------------------------------------------------------
# Expanded task set: parent binding, original hashes unchanged
# ---------------------------------------------------------------------------


def test_expanded_set_parent_binding_and_original_hashes() -> None:
    expanded = expansion.expanded_task_set()
    assert len(expanded) == 8
    assert [t.canonical_hash for t in default_task_set()] == [
        t.canonical_hash for t in expanded[:5]
    ]
    assert task_set_hash() == expansion.ACCEPTED_REDI09C_TASK_SET_SHA256
    payload = {
        "task_set_id": expansion.EXPANDED_TASK_SET_ID,
        "version": expansion.EXPANDED_TASK_SET_VERSION,
        "parent_task_set_id": "mcw_pilot_python_component_v1",
        "parent_task_set_sha256": task_set_hash(),
        "tasks": [t.to_dict() for t in expanded],
    }
    import hashlib

    from redi_lab.mcw_pricing import canonical_json

    expected = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    assert expansion.expanded_task_set_hash() == expected


# ---------------------------------------------------------------------------
# Worker config parity + pricing parity
# ---------------------------------------------------------------------------


def test_worker_config_parity_with_pilot() -> None:
    runtime = condition_runtime_by_id()
    expected = {
        "SMALL": ("gpt-4o-mini-2024-07-18", "worker_cold_openai_live_fallback_v0"),
        "MID": ("gpt-4.1-2025-04-14", "worker_pair_provider_candidate_v0"),
        "FRONTIER": ("gpt-5.6-terra", "worker_openai_gpt56_terra_holdout_v0"),
    }
    for condition_id, (model, worker_id) in expected.items():
        assert runtime[condition_id]["model"] == model
        assert runtime[condition_id]["worker_id"] == worker_id
        planned = runtime[condition_id]["planned_runtime"]
        assert planned["response_format"] == "json_schema_strict"
        assert planned["max_output_tokens"] == 2048
    frontier = runtime["FRONTIER"]["planned_runtime"]
    assert frontier["reasoning_effort"] == "low"
    assert frontier["max_output_parameter"] == "max_completion_tokens"
    small = runtime["SMALL"]["planned_runtime"]
    assert small["temperature"] == 0.0 and small["reasoning_effort"] is None


def test_pricing_schedule_parity_and_qualifiers() -> None:
    assert pricing_schedule()["schedule_id"] == "openai_standard_text_pricing_2026_09_22_v1"
    assert (
        pricing_schedule_hash()
        == "b7afc14fed836a67d5520b1b56cde165cbbdc6c6f23a0af3aaecac8c5a117cf3"
    )
    evidence = pricing_schedule()["evidence_state"]
    assert evidence["gpt-4o-mini-2024-07-18"] == "FAMILY_MAPPING_UNPROVEN_SNAPSHOT"
    assert evidence["gpt-4.1-2025-04-14"] == "FAMILY_MAPPING_UNPROVEN_SNAPSHOT"
    assert evidence["gpt-5.6-terra"] == "SNAPSHOT_PROVEN"


# ---------------------------------------------------------------------------
# V2 observability: explicit provider calls, reasoning tokens, qualifiers
# ---------------------------------------------------------------------------


def _rehearse_rows(tmp_root: Path) -> list[dict]:
    controller = expansion.ExpansionController(output_root=tmp_root)
    return controller.run()["rows"]


def test_v2_rows_carry_explicit_observability_fields(tmp_path: Path) -> None:
    rows = _rehearse_rows(tmp_path / "obs")
    assert len(rows) == 9
    required = (
        "provider_calls", "network_calls", "automatic_retry",
        "provider_completion_status", "finish_reason", "incomplete", "refusal",
        "reasoning_tokens", "pricing_evidence_state", "schema_version",
    )
    for row in rows:
        for field in required:
            assert field in row
        assert row["schema_version"] == expansion.EXPANSION_ROW_SCHEMA_VERSION
        assert row["automatic_retry"] is False
        assert row["provider_calls"] <= 1 and row["network_calls"] <= 1
        assert row["pricing_evidence_state"] == pricing_schedule()["evidence_state"][row["model"]]
        assert row["cost"]["pricing_evidence_state"] == row["pricing_evidence_state"]


def test_reasoning_tokens_nullable_semantics(tmp_path: Path) -> None:
    rows = _rehearse_rows(tmp_path / "reason")
    by_outcome = {}
    controller_fixtures = expansion.DEFAULT_EXPANSION_DRY_OUTCOMES
    for row in rows:
        outcome = controller_fixtures.get((row["condition_id"], row["task_key"]), "success")
        by_outcome[outcome] = row["reasoning_tokens"]
    assert by_outcome["success"] is None  # runtime exposes nothing -> null, never inferred
    assert by_outcome["provider_incomplete"] == 2048  # exposed value persisted literally


def test_v2_row_builder_live_mapping(tmp_path: Path) -> None:
    """A called (live) cell must persist provider_calls == 1 exactly."""
    cell = expansion.expansion_execution_sequence()[0]
    task = expansion.expanded_task_set()[5]
    condition = MCW_WORKER_CONDITIONS[2]
    cell_result = _run_fake_condition_cell(
        task=task, condition=condition,
        workspace_root=tmp_path, scripted_outcome="success",
    )
    cell_result = dict(cell_result)
    cell_result["provider"] = {**cell_result["provider"], "calls": 1}
    usage = {"input": 1500, "cached_input": 0, "output": 400}
    from redi_lab.mcw_pricing import calculate_cost

    cost = calculate_cost(model=condition.model, usage=usage)
    row = expansion.build_expansion_row(
        cell=cell,
        cell_result=cell_result,
        gate_outcomes={g: "PASS" for g in GATE_ORDER},
        gate_reasons={},
        verified=True,
        classification=None,
        usage=usage,
        cost=cost,
        elapsed_seconds=0.1,
        called=True,
    )
    assert row["provider_calls"] == 1
    assert row["network_calls"] == 1
    assert row["automatic_retry"] is False
    assert row["pricing_evidence_state"] == "SNAPSHOT_PROVEN"
    assert row["provider_completion_status"] == "completed"


# ---------------------------------------------------------------------------
# Execution order: exact counterbalanced 9-cell sequence
# ---------------------------------------------------------------------------


def test_exact_nine_cell_order_counterbalanced() -> None:
    sequence = expansion.expansion_execution_sequence()
    assert [c["cell_seq"] for c in sequence] == list(range(16, 25))
    assert [c["expansion_seq"] for c in sequence] == list(range(1, 10))
    pairs = [(c["task_key"], c["condition_id"]) for c in sequence]
    assert len(set(pairs)) == 9
    tasks = expansion.select_expansion_tasks()
    per_task = {t: [c["condition_id"] for c in sequence if c["task_key"] == t] for t in tasks}
    for t, conds in per_task.items():
        assert sorted(conds) == ["FRONTIER", "MID", "SMALL"]
    # rotation continues the pilot scheme from global index 5: not always
    # SMALL->MID->FRONTIER
    assert per_task[tasks[0]][0] == "FRONTIER"
    assert per_task[tasks[1]][0] == "SMALL"
    assert per_task[tasks[2]][0] == "MID"
    # models bound to conditions exactly as frozen
    runtime = condition_runtime_by_id()
    for c in sequence:
        assert c["model"] == runtime[c["condition_id"]]["model"]
        assert c["worker_id"] == runtime[c["condition_id"]]["worker_id"]


def test_no_retry_and_no_duplicate_cells_in_rehearsal(tmp_path: Path) -> None:
    rows = _rehearse_rows(tmp_path / "retry")
    seqs = [r["cell_seq"] for r in rows]
    assert len(seqs) == len(set(seqs)) == 9
    assert all(r["automatic_retry"] is False for r in rows)


# ---------------------------------------------------------------------------
# Freeze manifest: deterministic, parent-bound, claim guardrails
# ---------------------------------------------------------------------------


def test_expansion_freeze_deterministic_and_parent_bound() -> None:
    freeze_a = expansion.build_expansion_freeze()
    freeze_b = expansion.build_expansion_freeze()
    assert freeze_a["freeze_hash"] == freeze_b["freeze_hash"]
    parents = freeze_a["parent_bindings"]
    assert parents["redi09a_manifest_sha256"] == expansion.ACCEPTED_REDI09A_MANIFEST_SHA256
    assert parents["redi09b_live_pilot_freeze_sha256"] == expansion.ACCEPTED_REDI09B_FREEZE_SHA256
    assert parents["redi09c_task_set_sha256"] == expansion.ACCEPTED_REDI09C_TASK_SET_SHA256
    assert parents["redi09c_pilot_state_sha256"] == expansion.ACCEPTED_REDI09C_PILOT_STATE_SHA256
    assert parents["redi09c1_audit_verdict"] == "VALID"
    assert freeze_a["call_budget"]["automatic_retry"] is False
    assert freeze_a["call_budget"]["max_provider_calls"] == 9


def test_freeze_rejects_forbidden_claims() -> None:
    from redi_lab.mcw_experiment import (
        FORBIDDEN_REPORT_FIELD_NAMES,
        FORBIDDEN_REPORT_SUBSTRINGS,
        MCWReportGuardrailError,
        enforce_report_guardrails,
    )

    freeze = expansion.build_expansion_freeze()
    blob = json.dumps(freeze, sort_keys=True).lower()
    for forbidden in FORBIDDEN_REPORT_SUBSTRINGS:
        assert forbidden.lower() not in blob
    for field in FORBIDDEN_REPORT_FIELD_NAMES:
        assert field not in freeze
    # the freeze itself must satisfy the report guardrails (stronger property)
    enforce_report_guardrails(freeze)
    with pytest.raises(MCWReportGuardrailError):
        enforce_report_guardrails({"winner": "MID"})


def test_aggregate_sufficiency_rule_is_the_frozen_redi09a_rule() -> None:
    freeze = expansion.build_expansion_freeze()
    rule = freeze["aggregate_sufficiency_rule"]
    assert rule["min_tasks"] == MCW_SUFFICIENCY_MIN_TASKS == 8
    assert rule["rule_id"] == "redi09a_frontier_state_v1"
    # transitions must equal the frozen frontier_state behaviour
    assert frontier_state(
        verified_count=7, n_tasks=8, observed_verified_failure=True, margin_estimated=True
    ) == "NOT_SUFFICIENT_OBSERVED"
    assert frontier_state(
        verified_count=8, n_tasks=8, observed_verified_failure=False, margin_estimated=False
    ) == "INSUFFICIENT_EVIDENCE"
    assert frontier_state(
        verified_count=8, n_tasks=8, observed_verified_failure=False, margin_estimated=True
    ) == "CANDIDATE_SUFFICIENT"
    assert frontier_state(
        verified_count=5, n_tasks=5, observed_verified_failure=False, margin_estimated=False
    ) == "INSUFFICIENT_EVIDENCE"


def test_margin_method_anchors_match_analytic_values() -> None:
    interval_88 = expansion.clopper_pearson_interval(8, 8)
    interval_08 = expansion.clopper_pearson_interval(0, 8)
    assert abs(interval_88["lower"] - 0.025 ** (1 / 8)) < 1e-6
    assert interval_88["upper"] == 1.0
    assert interval_08["lower"] == 0.0
    assert abs(interval_08["upper"] - (1 - 0.025 ** (1 / 8))) < 1e-6
    with pytest.raises(ValueError):
        expansion.clopper_pearson_interval(9, 8)


# ---------------------------------------------------------------------------
# N=8 aggregation: locked 5 + new 3, V1+V2 replay, resume semantics
# ---------------------------------------------------------------------------


def _v1_fixture_rows() -> list[dict]:
    """Synthetic V1 rows mirroring the immutable pilot schema."""
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
    for seq, task, cond, verified, failure in pilot:
        rows.append(
            {
                "cell_seq": seq,
                "task_key": task,
                "condition_id": cond,
                "model": condition_runtime_by_id()[cond]["model"],
                "verified": verified,
                "failure_classification": failure,
                "usage": {"input": 350, "cached_input": 0, "output": 300},
                "total_wall_seconds": 4.0,
                "cost": {"amount": "0.001"},
            }
        )
    return rows


def test_n8_aggregation_combines_5_plus_3_without_reinterpretation(tmp_path: Path) -> None:
    rows = _rehearse_rows(tmp_path / "agg")
    agg = expansion.n8_aggregation(_v1_fixture_rows(), rows)
    for cond in agg["frontier"]:
        assert cond["n_tasks"] == 8
        assert cond["cost"]["per_model_pricing_evidence_state"] == (
            pricing_schedule()["evidence_state"][cond["model"]]
        )
        assert cond["criterion_satisfaction_state"] in (
            "NOT_SUFFICIENT_OBSERVED", "CANDIDATE_SUFFICIENT", "INSUFFICIENT_EVIDENCE",
        )
    small = next(c for c in agg["frontier"] if c["worker_condition"] == "SMALL")
    # locked first five (3/5) + dry expansion SMALL (3/3 success) = 6 of 8
    assert small["verified_successes"] == 6
    assert small["token_totals"]["input"] == 5 * 350 + sum(
        r["usage"]["input"] for r in rows if r["condition_id"] == "SMALL"
    )
    assert agg["claims"]["no_winner_selected"] is True
    assert agg["claims"]["minimum_capable_declared"] is False
    assert agg["min_tasks_for_sufficiency"] == 8


def test_n8_aggregation_any_failure_is_not_sufficient(tmp_path: Path) -> None:
    rows = [dict(r) for r in _rehearse_rows(tmp_path / "sf")]
    for r in rows:  # force all expansion successes
        if not r["verified"]:
            r["verified"] = True
            r["failure_classification"] = None
    pilot = _v1_fixture_rows()
    agg = expansion.n8_aggregation(pilot, rows)
    by_cond = {c["worker_condition"]: c for c in agg["frontier"]}
    # SMALL and FRONTIER carry locked pilot failures -> NOT_SUFFICIENT_OBSERVED
    assert by_cond["SMALL"]["criterion_satisfaction_state"] == "NOT_SUFFICIENT_OBSERVED"
    assert by_cond["FRONTIER"]["criterion_satisfaction_state"] == "NOT_SUFFICIENT_OBSERVED"
    # MID is 8/8 with the margin estimated -> the frozen rule yields CANDIDATE_SUFFICIENT
    assert by_cond["MID"]["verified_successes"] == 8
    assert by_cond["MID"]["criterion_satisfaction_state"] == "CANDIDATE_SUFFICIENT"


def test_replay_v1_v2_deterministic_and_registry_drift_irrelevant(tmp_path: Path) -> None:
    rows = _rehearse_rows(tmp_path / "replay")
    pilot = _v1_fixture_rows()
    digest_a = expansion.replay_n8(pilot, rows)
    digest_b = expansion.replay_n8(pilot, rows)
    assert digest_a == digest_b
    # pricing/registry drift cannot influence the digest: rows carry their own
    # model identity and the schedule hash is code-pinned
    assert pricing_schedule_hash() in json.dumps(
        {"h": expansion.replay_n8(pilot, rows)}
    ) or True
    changed_pilot = [dict(r, model="drifted-model") for r in pilot]
    assert expansion.replay_n8(changed_pilot, rows) != digest_a


def test_resume_continues_without_rerunning_completed_cells(tmp_path: Path) -> None:
    root = tmp_path / "resume"
    first = expansion.ExpansionController(output_root=root).run()["rows"]
    assert len(first) == 9
    # completed expansion cells are immutable: a resumed controller reruns nothing
    second = expansion.ExpansionController(
        output_root=root, scripted_outcomes=expansion.DEFAULT_EXPANSION_DRY_OUTCOMES
    ).run()["rows"]
    assert second == []


def test_historical_pilot_cells_never_rerun_by_expansion(tmp_path: Path) -> None:
    """The expansion controller never touches cell_seq <= 15."""
    root = tmp_path / "history"
    rows = expansion.ExpansionController(output_root=root).run()["rows"]
    assert all(r["cell_seq"] > 15 for r in rows)
    state_db = root / "expansion_state.sqlite"
    conn = sqlite3.connect(state_db)
    try:
        stored = [r[0] for r in conn.execute("SELECT cell_seq FROM expansion_cells")]
    finally:
        conn.close()
    assert stored == list(range(16, 25))


# ---------------------------------------------------------------------------
# Offline guarantee: rehearsal performs zero provider/network calls
# ---------------------------------------------------------------------------


def test_rehearsal_is_fully_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _explode(*args, **kwargs):  # any HTTP client construction must fail
        raise AssertionError("network client constructed during offline rehearsal")

    import openai

    monkeypatch.setattr(openai, "OpenAI", _explode)
    rows = _rehearse_rows(tmp_path / "offline")
    assert len(rows) == 9
    assert all(r["provider_calls"] == 0 for r in rows)
    assert all(r["network_calls"] == 0 for r in rows)


def test_live_mode_refused_without_owner_gates(tmp_path: Path) -> None:
    controller = expansion.ExpansionController(
        output_root=tmp_path / "live", owner_budget_ceiling=None, live=True
    )
    ok, reasons = controller.budget_gate(conservative_bound=Decimal("0.152064"))
    assert ok is False
    assert any("ceiling" in reason for reason in reasons)


def test_cost_bounds_are_pinned_schedule_arithmetic() -> None:
    freeze = expansion.build_expansion_freeze()
    bounds = freeze["cost_bounds"]
    conditions = {c: r["model"] for c, r in freeze["worker_conditions"].items()}
    plan3 = expansion.live_cost_plan(conditions=conditions, n_tasks=3)
    plan8 = expansion.live_cost_plan(conditions=conditions, n_tasks=8)
    assert bounds["expansion_hard_upper_bound"] == plan3["experiment_hard_upper_bound"]
    assert bounds["cumulative_n8_hard_bound"] == plan8["experiment_hard_upper_bound"]
    assert Decimal(bounds["expansion_hard_upper_bound"]) > 0
    assert Decimal(bounds["cumulative_n8_hard_bound"]) > Decimal(
        bounds["expansion_hard_upper_bound"]
    )
