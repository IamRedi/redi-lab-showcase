"""REDI-09B MCW pricing & live-pilot freeze regressions.

Offline only: versioned pricing schedule immutability, exact cost arithmetic,
budget gate, 15-cell freeze, deterministic resume, drift fail-closed, result
blindness, and the pilot-only conclusion boundary.  Zero provider/network.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

import redi_lab.mcw_live_pilot as mlp
import redi_lab.mcw_pricing as pricing
from redi_lab.mcw_experiment import (
    FRONTIER_CANDIDATE_SUFFICIENT,
    FRONTIER_NOT_SUFFICIENT_OBSERVED,
    MCW_SUFFICIENCY_MIN_TASKS,
    default_task_set,
)
from redi_lab.mcw_live_pilot import (
    FRONTIER_PILOT_CANDIDATE_SUFFICIENT,
    LivePilotController,
    STOP_REASON_MANIFEST_DRIFT,
    STOP_REASON_PRICING_MISMATCH,
    audit_redi09a_manifest,
    build_live_pilot_freeze,
    execution_sequence,
    pilot_frontier_state,
)
from redi_lab.mcw_pricing import (
    EVIDENCE_FAMILY_MAPPING_UNPROVEN,
    EVIDENCE_SNAPSHOT_PROVEN,
    MCWPricingMismatchError,
    calculate_cost,
    cell_cost_bound,
    live_cost_plan,
    pricing_schedule,
    pricing_schedule_hash,
)


ROOT = Path(__file__).resolve().parents[2]
REDI09A_ACCEPTANCE_DB = ROOT / "build" / "redi09a-mcw-dry-20260922T205849Z" / "mcw_evidence.sqlite"

MODELS = {
    "SMALL": "gpt-4o-mini-2024-07-18",
    "MID": "gpt-4.1-2025-04-14",
    "FRONTIER": "gpt-5.6-terra",
}


# ---------------------------------------------------------------------------
# Pricing schedule
# ---------------------------------------------------------------------------


def test_pricing_schedule_is_versioned_immutable_snapshot():
    schedule = pricing_schedule()
    assert schedule["schedule_id"] == "openai_standard_text_pricing_2026_09_22_v1"
    assert schedule["schedule_version"] == 1
    assert schedule["provider"] == "openai"
    assert schedule["currency"] == "USD"
    assert schedule["service_tier"] == "standard"
    assert schedule["retrieved_at"] == "2026-09-22"
    # The canonical hash is pinned: any silent rate edit breaks this test and
    # must instead become a NEW schedule version.
    assert pricing_schedule_hash() == (
        "b7afc14fed836a67d5520b1b56cde165cbbdc6c6f23a0af3aaecac8c5a117cf3"
    )


def test_exact_price_evidence_mapping():
    records = pricing_schedule()["records"]
    small = records[MODELS["SMALL"]]
    mid = records[MODELS["MID"]]
    frontier = records[MODELS["FRONTIER"]]
    # Official base-family rates (undated aliases) mapped onto the snapshots.
    assert (small["input_per_1m"], small["cached_input_per_1m"], small["output_per_1m"]) == ("0.15", "0.075", "0.60")
    assert small["pricing_family"] == "gpt-4o-mini"
    assert small["evidence_status"] == EVIDENCE_FAMILY_MAPPING_UNPROVEN
    assert (mid["input_per_1m"], mid["cached_input_per_1m"], mid["output_per_1m"]) == ("2.00", "0.50", "8.00")
    assert mid["pricing_family"] == "gpt-4.1"
    assert mid["evidence_status"] == EVIDENCE_FAMILY_MAPPING_UNPROVEN
    # gpt-5.6-terra is listed directly: short-context standard rates proven.
    assert (frontier["input_per_1m"], frontier["cached_input_per_1m"], frontier["output_per_1m"]) == ("2.00", "0.20", "12.00")
    assert frontier["evidence_status"] == EVIDENCE_SNAPSHOT_PROVEN
    assert frontier["long_context"] == {
        "input_per_1m": "4.00",
        "cached_input_per_1m": "0.40",
        "output_per_1m": "18.00",
        "threshold_tokens_officially_stated": False,
    }
    # Data-residency rule recorded but not applied.
    assert "10% uplift" in pricing_schedule()["rules"]["data_residency"]


def test_cost_arithmetic_cached_noncached_and_output():
    # gpt-5.6-terra: 1500 input (0 cached) + 200 output.
    cost = calculate_cost(model="gpt-5.6-terra", usage={"input": 1500, "cached_input": 0, "output": 200})
    assert cost["cost_amount"] == "0.0054000"  # 1500*2e-6 + 200*12e-6
    assert cost["raw_usage"]["noncached_input"] == 1500
    # Cached input billed at the cached rate; noncached at the input rate.
    cached = calculate_cost(model="gpt-5.6-terra", usage={"input": 1500, "cached_input": 500, "output": 200})
    assert cached["raw_usage"]["noncached_input"] == 1000
    assert Decimal(cached["cost_amount"]) == (
        Decimal(1000) * Decimal("0.000002")
        + Decimal(500) * Decimal("0.0000002")
        + Decimal(200) * Decimal("0.000012")
    )
    assert Decimal(cached["cost_amount"]) < Decimal(cost["cost_amount"])
    # SMALL family arithmetic: 1000 in + 100 out at $0.15/$0.60 per 1M.
    small = calculate_cost(model=MODELS["SMALL"], usage={"input": 1000, "cached_input": 0, "output": 100})
    assert Decimal(small["cost_amount"]) == Decimal("0.00021")
    # Invalid usage shapes fail closed.
    with pytest.raises(MCWPricingMismatchError):
        calculate_cost(model="gpt-5.6-terra", usage={"input": 100, "cached_input": 200, "output": 0})
    with pytest.raises(MCWPricingMismatchError):
        calculate_cost(model="gpt-999-unknown", usage={"input": 1, "output": 1})


def test_reasoning_tokens_are_not_double_counted():
    # Provider output usage already includes reasoning tokens (official
    # semantics); the calculator consumes `output` exactly once and the
    # schedule documents the rule.
    usage = {"input": 1000, "cached_input": 0, "output": 2048}
    cost = calculate_cost(model="gpt-5.6-terra", usage=usage)
    assert Decimal(cost["cost_amount"]) == Decimal(2048) * Decimal("0.000012") + Decimal(1000) * Decimal("0.000002")
    assert "already includes reasoning tokens" in pricing_schedule()["rules"]["reasoning_tokens"]
    assert "reasoning" not in cost["raw_usage"]  # no second reasoning term


def test_schedule_hash_is_bound_into_costs_and_freeze():
    cost = calculate_cost(model="gpt-5.6-terra", usage={"input": 10, "output": 10})
    assert cost["schedule_id"] == "openai_standard_text_pricing_2026_09_22_v1"
    assert cost["schedule_hash"] == pricing_schedule_hash()
    freeze = build_live_pilot_freeze(source_revision="unit")
    assert freeze["pricing"]["schedule_hash"] == pricing_schedule_hash()


def test_cost_bounds_separate_expected_from_hard_upper_bound():
    plan = live_cost_plan(conditions=MODELS, n_tasks=5)
    # Hand-computed hard bounds per condition (2048 in + 2048 out per cell):
    small = Decimal(2048) * (Decimal("0.15") + Decimal("0.60")) / Decimal(1_000_000) * 5
    mid = Decimal(2048) * (Decimal("2.00") + Decimal("8.00")) / Decimal(1_000_000) * 5
    terra = Decimal(2048) * (Decimal("2.00") + Decimal("12.00")) / Decimal(1_000_000) * 5
    assert Decimal(plan["experiment_hard_upper_bound"]) == small + mid + terra
    assert Decimal(plan["experiment_hard_upper_bound"]) == Decimal("0.253440000")
    # Expected estimate uses the typical 512-token output, clearly separated.
    assert Decimal(plan["experiment_expected_estimate"]) < Decimal(plan["experiment_hard_upper_bound"])
    per_condition = plan["per_condition"]
    assert per_condition["FRONTIER"]["per_cell"]["long_context_worst_case"] is not None
    assert per_condition["SMALL"]["per_cell"]["long_context_worst_case"] is None
    assert "EXPECTED ESTIMATE" in plan["estimate_boundary"]


# ---------------------------------------------------------------------------
# Freeze: 15 cells, order, ceiling, retry policy
# ---------------------------------------------------------------------------


def test_freeze_contains_exactly_15_cells_with_one_call_ceiling():
    freeze = build_live_pilot_freeze(source_revision="unit")
    sequence = freeze["execution_order"]
    assert len(sequence) == 15
    assert [cell["cell_seq"] for cell in sequence] == list(range(1, 16))
    assert freeze["call_budget"]["max_provider_calls"] == 15
    assert freeze["call_budget"]["per_cell_max_calls"] == 1
    assert freeze["call_budget"]["automatic_retry"] is False
    assert freeze["call_budget"]["worker_fallback"] == "forbidden"
    assert freeze["freeze_hash"] == build_live_pilot_freeze(source_revision="unit")["freeze_hash"]


def test_execution_order_freezes_preregistered_counterbalancing():
    sequence = execution_sequence()
    assert [(c["task_key"], c["condition_id"]) for c in sequence[:3]] == [
        ("line_wrap", "SMALL"),
        ("line_wrap", "MID"),
        ("line_wrap", "FRONTIER"),
    ]
    assert [(c["task_key"], c["condition_id"]) for c in sequence[3:6]] == [
        ("interval_merge", "MID"),
        ("interval_merge", "FRONTIER"),
        ("interval_merge", "SMALL"),
    ]
    # Every (task, condition) pair appears exactly once.
    pairs = [(c["task_key"], c["condition_id"]) for c in sequence]
    assert len(set(pairs)) == 15
    assert len({(t, c) for t, c in pairs for c in [c]}) == 15
    assert len({c["task_key"] for c in sequence}) == 5


# ---------------------------------------------------------------------------
# Budget gate
# ---------------------------------------------------------------------------


def _controller(tmp_path: Path, ceiling="1000000") -> LivePilotController:
    return LivePilotController(
        output_root=tmp_path / "pilot", owner_budget_ceiling=ceiling, source_revision="unit"
    )


def test_budget_gate_refuses_missing_or_low_owner_ceiling(tmp_path):
    controller = LivePilotController(output_root=tmp_path / "p1", owner_budget_ceiling=None)
    ok, reasons = controller.budget_gate(conservative_bound=Decimal("0.253440000"))
    assert ok is False
    assert any("owner budget ceiling not supplied" in reason for reason in reasons)
    # A ceiling below the conservative bound never passes.
    low = LivePilotController(output_root=tmp_path / "p2", owner_budget_ceiling="0.01")
    ok, reasons = low.budget_gate(conservative_bound=Decimal("0.253440000"))
    assert ok is False
    assert any("below the conservative bound" in reason for reason in reasons)
    # A sufficient explicit ceiling passes every condition.
    ok, reasons = _controller(tmp_path).budget_gate(conservative_bound=Decimal("0.253440000"))
    assert ok is True and reasons == []


def test_budget_gate_fails_when_calls_or_retry_or_pricing_drift(tmp_path):
    controller = _controller(tmp_path)
    controller.freeze["call_budget"]["max_provider_calls"] = 10
    ok, reasons = controller.budget_gate(conservative_bound=Decimal("1"))
    assert ok is False and any("must equal 15" in r for r in reasons)
    controller2 = _controller(tmp_path)
    controller2.freeze["call_budget"]["automatic_retry"] = True
    ok, reasons = controller2.budget_gate(conservative_bound=Decimal("1"))
    assert ok is False and any("retry must be false" in r for r in reasons)
    controller3 = LivePilotController(
        output_root=tmp_path / "p3",
        owner_budget_ceiling="1000000",
        pinned_schedule_hash="drifted-hash",
    )
    ok, reasons = controller3.budget_gate(conservative_bound=Decimal("1"))
    assert ok is False and any("not pinned" in r for r in reasons)


def test_run_without_owner_ceiling_is_refused(tmp_path):
    controller = LivePilotController(output_root=tmp_path / "p", owner_budget_ceiling=None)
    with pytest.raises(RuntimeError, match="owner budget ceiling not supplied"):
        controller.run()


# ---------------------------------------------------------------------------
# Dry budget rehearsal: 15 cells, resume, drift fail-closed
# ---------------------------------------------------------------------------


def test_rehearsal_executes_exactly_15_cells_with_pinned_pricing(tmp_path):
    controller = _controller(tmp_path)
    result = controller.run()
    executed = [row for row in result["rows"] if "verified" in row]
    assert len(executed) == 15
    assert controller.provider_calls == 0 and controller.network_calls == 0
    assert result["stop_reason"] is None
    for row in executed:
        assert row["cost"]["schedule_id"] == "openai_standard_text_pricing_2026_09_22_v1"
        assert row["cost"]["schedule_hash"] == pricing_schedule_hash()
        assert Decimal(row["cost"]["amount"]) >= 0
    # Costs summed from fake known usage: FRONTIER = 5 x (1500 in + 400 out).
    frontier = next(f for f in result["report"]["frontier"] if f["worker_condition"] == "FRONTIER")
    assert Decimal(frontier["cost"]["amount"]) == Decimal("0.0390000")
    assert frontier["token_totals"] == {"input": 7500, "output": 2000}


def test_rehearsal_never_retries_and_preserves_frozen_order(tmp_path):
    controller = _controller(tmp_path)
    result = controller.run()
    executed = [row for row in result["rows"] if "verified" in row]
    assert len(executed) == len({row["cell_seq"] for row in executed}) == 15
    expected_pairs = [(c["task_key"], c["condition_id"]) for c in execution_sequence()]
    assert [(r["task_key"], r["condition_id"]) for r in executed] == expected_pairs
    # Failed worker outcomes remain outcomes (recorded, not retried).
    failures = [row for row in executed if row["verified"] is False]
    assert failures and all(row["gates"] for row in failures)


def test_resume_after_interruption_never_duplicates_calls(tmp_path):
    interrupted = _controller(tmp_path)
    full_order = interrupted.freeze["execution_order"]
    interrupted.freeze["execution_order"] = full_order[:4]  # simulated crash after 4 cells
    partial = interrupted.run()
    assert len(partial["rows"]) == 4
    # Deterministic resume: same manifest, pricing, order; next unexecuted cell.
    resumed = _controller(tmp_path).run()
    assert len(resumed["rows"]) == 15
    assert [row["cell_seq"] for row in resumed["rows"]] == list(range(1, 16))
    # Cells 1-4 identical bytes to the interrupted run (immutable outcomes).
    assert resumed["rows"][:4] == partial["rows"]


def test_manifest_drift_fails_closed(tmp_path, monkeypatch):
    controller = _controller(tmp_path)
    # Drift the experiment definition the way a mid-run edit would: the
    # per-cell rebuild must notice and STOP before the first cell.
    monkeypatch.setattr(
        mlp,
        "MCW_QUALITY_CRITERION",
        dict(mlp.MCW_QUALITY_CRITERION) | {"drifted": True},
    )
    result = controller.run()
    assert result["stop_reason"] == STOP_REASON_MANIFEST_DRIFT
    assert result["rows"][0]["stop_reason"] == STOP_REASON_MANIFEST_DRIFT


def test_pricing_drift_fails_closed(tmp_path):
    controller = LivePilotController(
        output_root=tmp_path / "p",
        owner_budget_ceiling="1000000",
        pinned_schedule_hash="0000000000000000000000000000000000000000000000000000000000000000",
    )
    result = controller.run()
    assert result["stop_reason"] == STOP_REASON_PRICING_MISMATCH


# ---------------------------------------------------------------------------
# Result blindness + pilot-only conclusion boundary
# ---------------------------------------------------------------------------


def test_result_blindness_configuration_fixed_before_first_cell(tmp_path):
    outcomes_a = {("MID", "lru_cache_sim"): "success"}
    run_a = LivePilotController(
        output_root=tmp_path / "a", owner_budget_ceiling="1000000", scripted_outcomes=outcomes_a
    )
    run_b = LivePilotController(
        output_root=tmp_path / "b", owner_budget_ceiling="1000000", scripted_outcomes=None
    )
    freeze_a, freeze_b = run_a.freeze, run_b.freeze
    assert freeze_a["freeze_hash"] == freeze_b["freeze_hash"]
    assert freeze_a["execution_order"] == freeze_b["execution_order"]
    result_a = run_a.run()
    # Changing earlier outcomes must not change any later cell's configuration.
    assert run_a.freeze["freeze_hash"] == freeze_a["freeze_hash"]
    assert [c["condition_id"] for c in run_a.freeze["execution_order"]] == [
        c["condition_id"] for c in freeze_b["execution_order"]
    ]
    mid_rows_a = [r for r in result_a["rows"] if r.get("condition_id") == "MID"]
    assert next(r for r in mid_rows_a if r["task_key"] == "lru_cache_sim")["verified"] is True
    # No adaptive routing field exists anywhere in the freeze.
    assert freeze_a["result_blindness"]["adaptive_routing"] == "forbidden_in_pilot"


def test_pilot_only_boundary_no_final_mcw_at_n5():
    # 5/5 pilot success is capped at the pilot candidate state.
    assert (
        pilot_frontier_state(verified_count=5, n_tasks=5, margin_estimated=False)
        == FRONTIER_PILOT_CANDIDATE_SUFFICIENT
    )
    assert (
        pilot_frontier_state(verified_count=5, n_tasks=5, margin_estimated=True)
        == FRONTIER_PILOT_CANDIDATE_SUFFICIENT
    )
    assert pilot_frontier_state(verified_count=3, n_tasks=5, margin_estimated=False) == (
        FRONTIER_NOT_SUFFICIENT_OBSERVED
    )
    # The final threshold is untouched and cannot be satisfied by the pilot.
    assert MCW_SUFFICIENCY_MIN_TASKS == 8 > 5
    assert FRONTIER_CANDIDATE_SUFFICIENT != FRONTIER_PILOT_CANDIDATE_SUFFICIENT
    freeze = build_live_pilot_freeze(source_revision="unit")
    assert freeze["claim_boundary"]["final_minimum_capable_worker_declared_at_n5"] is False
    assert freeze["claim_boundary"]["best_pilot_state"] == FRONTIER_PILOT_CANDIDATE_SUFFICIENT


def test_rehearsal_report_has_no_winner_and_binds_criterion(tmp_path):
    controller = _controller(tmp_path)
    result = controller.run()
    report = result["report"]
    assert report["claims"] == {
        "no_winner_selected": True,
        "minimum_capable_declared": False,
        "pilot_only": True,
        "final_mcw_blocked_at_n5": True,
    }
    for frontier_row in report["frontier"]:
        assert "winner" not in frontier_row
        assert frontier_row["confidence_metadata"]["method"] == "descriptive_pilot_only"
    freeze = build_live_pilot_freeze(source_revision="unit")
    assert freeze["quality_criterion_hash"] == mlp._hash(dict(freeze["quality_criterion"]))


# ---------------------------------------------------------------------------
# Historical preservation
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not REDI09A_ACCEPTANCE_DB.exists(),
    reason="REDI-09A acceptance dry artifact not present on this machine",
)
def test_redi09a_manifest_remains_frozen_and_bound():
    audit = audit_redi09a_manifest(REDI09A_ACCEPTANCE_DB)
    assert audit["manifest_found"] is True
    assert audit["frozen"] is True
    assert audit["attempt_bindings_consistent"] is True
    assert audit["matches_current_task_set"] is True
