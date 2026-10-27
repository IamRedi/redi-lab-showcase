"""REDI-09A MCW experiment foundation validations (offline; zero provider/network)."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import redi_lab.mcw_experiment as mcw
from redi_lab.mcw_experiment import (
    FORBIDDEN_REPORT_FIELD_NAMES,
    FORBIDDEN_REPORT_SUBSTRINGS,
    FRONTIER_CANDIDATE_SUFFICIENT,
    FRONTIER_INSUFFICIENT_EVIDENCE,
    FRONTIER_NOT_SUFFICIENT_OBSERVED,
    GATE_ORDER,
    MCW_EXPERIMENT_FAMILY,
    MCW_MAX_LIVE_CALLS,
    MCW_POLICY_ID,
    MCW_QUALITY_CRITERION,
    MCW_STRATUM_ID,
    MCW_SUFFICIENCY_MIN_TASKS,
    MCW_WORKER_CONDITIONS,
    MCWReportGuardrailError,
    classify_mcw_failure,
    condition_order_for_task,
    default_stratum,
    default_task_set,
    enforce_report_guardrails,
    frontier_state,
    live_budget_plan,
    pilot_confidence,
    replay_mcw_result,
    replay_mcw_snapshot,
    run_mcw_experiment,
    task_set_hash,
    task_set_id,
)

failures: list[str] = []


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="redi09a-validations-"))
    outcome = run_mcw_experiment(output_root=work / "mcw", source_revision="validations")
    checks: list[tuple[str, bool]] = []

    def add(name: str, condition: bool) -> None:
        checks.append((name, bool(condition)))

    stratum = default_stratum()
    add(
        "A stratum identity versioned/bounded/no-model-stratification",
        stratum.stratum_id == MCW_STRATUM_ID == "mcw_python_component_v1"
        and stratum.version == 1
        and len(stratum.inclusion_rules) >= 5
        and all(
            token not in json.dumps(stratum.to_dict()).lower()
            for token in ("gpt-4o-mini", "gpt-4.1", "gpt-5.6")
        )
        and stratum.canonical_hash == default_stratum().canonical_hash,
    )
    tasks = default_task_set()
    add(
        "B task set 4-6 unseen same-stratum tasks, no holdout reuse, stable hash",
        4 <= len(tasks) <= 6
        and all(t.package not in {"depot_replenishment", "bulk_inventory", "clinic_replenishment", "inventory_math"} for t in tasks)
        and all(3 <= len(t.public_checks) <= 8 and 3 <= len(t.private_checks) <= 8 for t in tasks)
        and task_set_id() == "mcw_pilot_python_component_v1"
        and task_set_hash() == task_set_hash(tasks),
    )
    oracle_ok = True
    try:
        for task in tasks:
            namespace: dict = {}
            exec(task.reference_implementation, namespace)  # noqa: S102 - fixture oracle
            for check in list(task.public_checks) + list(task.private_checks) + [task.integration_probe] + list(task.reviewer_checks):
                target = namespace[str(check["target"])]
                args = list(check["args"])
                if check["kind"] == "call_equals":
                    assert target(*args) == check["expected"]
                else:
                    exception = {"ValueError": ValueError, "TypeError": TypeError, "KeyError": KeyError}[str(check["exception"])]
                    try:
                        target(*args)
                        raise AssertionError("expected raise")
                    except exception:
                        pass
    except Exception:
        oracle_ok = False
    add("C predeclared reference solutions satisfy every declared check", oracle_ok)
    add(
        "D tiers are descriptive cost/role classes with exact identities",
        [c.condition_id for c in MCW_WORKER_CONDITIONS] == ["SMALL", "MID", "FRONTIER"]
        and len({c.model for c in MCW_WORKER_CONDITIONS}) == 3
        and all("NOT an intelligence claim" in c.role_description for c in MCW_WORKER_CONDITIONS),
    )
    frontier = next(c for c in MCW_WORKER_CONDITIONS if c.condition_id == "FRONTIER")
    add(
        "E FRONTIER binds the accepted REDI-08 closure runtime exactly",
        frontier.model == "gpt-5.6-terra"
        and dict(frontier.planned_runtime)
        == {
            "api": "chat_completions",
            "api_capability_profile": "openai_chat_gpt56_v1",
            "max_output_parameter": "max_completion_tokens",
            "max_output_tokens": 2048,
            "response_format": "json_schema_strict",
            "reasoning_effort": "low",
            "temperature": None,
            "top_p": None,
        },
    )
    add(
        "F deterministic counterbalanced condition order",
        [condition_order_for_task(i, MCW_WORKER_CONDITIONS) for i in range(3)]
        == [
            ("SMALL", "MID", "FRONTIER"),
            ("MID", "FRONTIER", "SMALL"),
            ("FRONTIER", "SMALL", "MID"),
        ],
    )
    conn = sqlite3.connect("file:" + outcome.db_path.resolve().as_posix() + "?mode=ro&immutable=1", uri=True)
    row = conn.execute(
        "SELECT frozen_ts, payload_sha256, payload_json FROM experiment_manifests WHERE id=?",
        (outcome.manifest_id,),
    ).fetchone()
    manifest_payload = json.loads(row[2])
    attempt_bindings = {
        json.loads(pj)["metadata"]["manifest_sha256"]
        for (pj,) in conn.execute(
            "SELECT payload_json FROM evidence_entries WHERE entity_type='RunEvidence'"
        )
        if json.loads(pj).get("metadata", {}).get("evidence_kind") == "mcw_worker_attempt"
    }
    conn.close()
    add(
        "G manifest preregistered+frozen, hash bound to every attempt",
        row is not None
        and row[0] is not None
        and row[1] == outcome.manifest_sha256
        and attempt_bindings == {outcome.manifest_sha256}
        and manifest_payload["task_selection"]["selection_before_results"] is True
        and manifest_payload["randomization"]["fixed_before_results"] is True,
    )
    add(
        "H quality criterion frozen with PILOT margin",
        MCW_QUALITY_CRITERION["primary"] == "mandatory deterministic verification success"
        and set(MCW_QUALITY_CRITERION["required_gates"]) == set(GATE_ORDER)
        and MCW_QUALITY_CRITERION["statistical_margin"]["status"] == "PILOT_NOT_YET_ESTIMATED"
        and MCW_QUALITY_CRITERION["components"]["subjective_model_scores_used"] is False,
    )
    by_task: dict[str, list[dict]] = {}
    for attempt in outcome.rows:
        by_task.setdefault(attempt["task_key"], []).append(attempt)
    add(
        "I paired cells share task semantics; only worker config differs",
        all(
            len(rows) == 3
            and len({r["task_hash"] for r in rows}) == 1
            and len({json.dumps(r["condition_order"]) for r in rows}) == 1
            for rows in by_task.values()
        ),
    )
    add(
        "J condition isolation; no reference/private leakage",
        all(
            r["context"]["reference_leak_detected"] is False
            and r["context"]["private_check_args_in_context"] is False
            for r in outcome.rows
        )
        and len({(r["condition_id"], r["task_key"]) for r in outcome.rows}) == len(outcome.rows),
    )
    clean = {gate: "PASS" for gate in GATE_ORDER}
    taxonomy_ok = (
        classify_mcw_failure(provider_incomplete=True, gate_outcomes=clean, structured_error=None, unexpected_error=None)[0]
        == "PROVIDER_OUTPUT_TRUNCATED_OR_INCOMPLETE"
        and classify_mcw_failure(
            provider_incomplete=False,
            gate_outcomes={**clean, "structured_output_schema": "FAIL"},
            structured_error="STRUCTURED_OUTPUT_SCHEMA_FAILURE reason=patch_0_path_does_not_match_contract",
            unexpected_error=None,
        )[0]
        == "SCOPE_VIOLATION"
        and classify_mcw_failure(
            provider_incomplete=False,
            gate_outcomes={**clean, "independent_final_reviewer": "FAIL"},
            structured_error=None,
            unexpected_error=None,
        )[0]
        == "REVIEWER_REJECTION"
    )
    add("K predeclared typed failure taxonomy", taxonomy_ok)
    outcome_by_cell = {(r["condition_id"], r["task_key"]): r for r in outcome.rows}
    add(
        "L controlled dry outcomes (all / subset / representative failures)",
        sum(1 for r in outcome.rows if r["condition_id"] == "FRONTIER" and r["verified"]) == 5
        and outcome_by_cell[("MID", "lru_cache_sim")]["failure_classification"] == "WORKER_IMPLEMENTATION_FAILURE"
        and outcome_by_cell[("SMALL", "interval_merge")]["failure_classification"] == "PROVIDER_OUTPUT_TRUNCATED_OR_INCOMPLETE"
        and outcome_by_cell[("SMALL", "scoreboard")]["failure_classification"] == "SCOPE_VIOLATION",
    )
    add(
        "M cost never invented without a versioned pricing schedule",
        all(
            r["cost"] == {"status": "unavailable_unversioned", "pricing_schedule_version": None, "amount": None}
            for r in outcome.rows
        )
        and all(f["cost"]["status"] == "unavailable_unversioned" for f in outcome.report["frontier"]),
    )
    confidence = pilot_confidence(
        [
            {"verified": True, "failure_classification": None},
            {"verified": False, "failure_classification": "WORKER_IMPLEMENTATION_FAILURE"},
        ]
    )
    add(
        "N descriptive confidence only (no capability score, interval unestimated)",
        confidence["method"] == "descriptive_pilot_only"
        and confidence["uncertainty_interval"] is None
        and confidence["interval_method"] == "NOT_ESTIMATED_PILOT_SAMPLE_ASSUMPTIONS_NOT_MET"
        and "coverage_warning" in confidence
        and "global_capability_score" not in confidence,
    )
    states = {f["condition_id"]: f["state"] for f in outcome.report["frontier"]}
    add(
        "O frontier states predeclared; pilot can never elect a winner",
        states == {"SMALL": FRONTIER_NOT_SUFFICIENT_OBSERVED, "MID": FRONTIER_NOT_SUFFICIENT_OBSERVED, "FRONTIER": FRONTIER_INSUFFICIENT_EVIDENCE}
        and MCW_SUFFICIENCY_MIN_TASKS > len(tasks)
        and frontier_state(verified_count=8, n_tasks=8, observed_verified_failure=False, margin_estimated=True)
        == FRONTIER_CANDIDATE_SUFFICIENT,
    )
    guardrails_ok = True
    try:
        enforce_report_guardrails({"winner": "x"})
        guardrails_ok = False
    except MCWReportGuardrailError:
        pass
    try:
        enforce_report_guardrails({"note": "model A is the best model"})
        guardrails_ok = False
    except MCWReportGuardrailError:
        pass
    try:
        enforce_report_guardrails(outcome.report)
    except MCWReportGuardrailError:
        guardrails_ok = False
    add(
        "P reporting guardrails enforced; report claims nothing",
        guardrails_ok
        and "winner" in FORBIDDEN_REPORT_FIELD_NAMES
        and "best model" in FORBIDDEN_REPORT_SUBSTRINGS
        and outcome.report["claims"]["no_winner_selected"] is True
        and outcome.report["claims"]["minimum_capable_declared"] is False
        and outcome.report["counts_as_mcw_conclusion"] is False,
    )
    plan = live_budget_plan()
    bloated = live_budget_plan(tasks=tuple(tasks) * 7)
    add(
        "Q live-budget plan prepared (15 calls, no retries, excess blocked)",
        plan["max_live_calls"] == 15
        and plan["automatic_retries"] is False
        and plan["block_if_excessive"]["within_budget"] is True
        and bloated["block_if_excessive"]["action"] == "BLOCK"
        and bloated["max_live_calls"] > MCW_MAX_LIVE_CALLS,
    )
    one = replay_mcw_result(outcome.result_path)
    two = replay_mcw_result(outcome.result_path)
    snapshot_one = replay_mcw_snapshot(outcome.db_path, outcome.factory_run_id)
    drifted = (
        mcw.MCW_WORKER_CONDITIONS,
        mcw.task_set_hash,
        mcw.MCW_QUALITY_CRITERION,
        mcw.MCW_SUFFICIENCY_MIN_TASKS,
    )
    try:
        mcw.MCW_WORKER_CONDITIONS = ()
        mcw.task_set_hash = lambda tasks=None: "drifted"  # type: ignore[assignment]
        mcw.MCW_QUALITY_CRITERION = {"drifted": True}  # type: ignore[assignment]
        mcw.MCW_SUFFICIENCY_MIN_TASKS = 1  # type: ignore[assignment]
        snapshot_two = replay_mcw_snapshot(outcome.db_path, outcome.factory_run_id)
        result_two = replay_mcw_result(outcome.result_path)
    finally:
        (
            mcw.MCW_WORKER_CONDITIONS,
            mcw.task_set_hash,
            mcw.MCW_QUALITY_CRITERION,
            mcw.MCW_SUFFICIENCY_MIN_TASKS,
        ) = drifted
    add(
        "R replay x2 read-only, drift-immune, zero provider/network",
        one == two
        and snapshot_one == snapshot_two
        and result_two["replay_dependencies"]["provider_calls"] == 0
        and result_two["replay_dependencies"]["network"] == 0
        and outcome.provider_calls == 0
        and outcome.network_calls == 0
        and {r["model"] for r in snapshot_one} == {c.model for c in MCW_WORKER_CONDITIONS},
    )
    add(
        "S policy/family identity persisted",
        outcome.report["policy"] == {"id": MCW_POLICY_ID, "version": 1}
        and outcome.report["experiment_family"] == MCW_EXPERIMENT_FAMILY,
    )

    # ------------------------------------------------------------------
    # REDI-09B — pricing schedule, live-pilot freeze, budget gate
    # ------------------------------------------------------------------
    from decimal import Decimal

    import redi_lab.mcw_live_pilot as mlp
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
        calculate_cost,
        live_cost_plan,
        pricing_schedule,
        pricing_schedule_hash,
    )

    add(
        "T pricing schedule pinned/immutable with explicit evidence states",
        pricing_schedule()["schedule_id"] == "openai_standard_text_pricing_2026_09_22_v1"
        and pricing_schedule()["retrieved_at"] == "2026-09-22"
        and pricing_schedule_hash() == pricing_schedule_hash()
        and pricing_schedule()["records"]["gpt-5.6-terra"]["evidence_status"] == EVIDENCE_SNAPSHOT_PROVEN
        and pricing_schedule()["records"]["gpt-4.1-2025-04-14"]["evidence_status"] == EVIDENCE_FAMILY_MAPPING_UNPROVEN
        and pricing_schedule()["records"]["gpt-4o-mini-2024-07-18"]["evidence_status"] == EVIDENCE_FAMILY_MAPPING_UNPROVEN,
    )
    terra_plain = calculate_cost(model="gpt-5.6-terra", usage={"input": 1500, "cached_input": 0, "output": 200})
    terra_cached = calculate_cost(model="gpt-5.6-terra", usage={"input": 1500, "cached_input": 500, "output": 200})
    add(
        "U cost arithmetic exact (cached split, reasoning not double-counted)",
        Decimal(terra_plain["cost_amount"]) == Decimal("0.0054000")
        and Decimal(terra_cached["cost_amount"]) == Decimal("0.0045000")
        and terra_cached["raw_usage"]["noncached_input"] == 1000
        and "already includes reasoning tokens" in pricing_schedule()["rules"]["reasoning_tokens"],
    )
    plan_b = live_cost_plan(conditions={
        "SMALL": "gpt-4o-mini-2024-07-18",
        "MID": "gpt-4.1-2025-04-14",
        "FRONTIER": "gpt-5.6-terra",
    }, n_tasks=5)
    add(
        "V expected estimate separated from hard configuration upper bound",
        Decimal(plan_b["experiment_hard_upper_bound"]) == Decimal("0.253440000")
        and Decimal(plan_b["experiment_expected_estimate"]) < Decimal(plan_b["experiment_hard_upper_bound"])
        and "EXPECTED ESTIMATE" in plan_b["estimate_boundary"]
        and plan_b["per_condition"]["FRONTIER"]["per_cell"]["long_context_worst_case"] is not None,
    )
    freeze_b = build_live_pilot_freeze(source_revision="validations")
    sequence = freeze_b["execution_order"]
    add(
        "W freeze: 15 cells, per-cell 1 call, no retry, frozen counterbalanced order",
        len(sequence) == 15
        and [c["cell_seq"] for c in sequence] == list(range(1, 16))
        and freeze_b["call_budget"]["per_cell_max_calls"] == 1
        and freeze_b["call_budget"]["automatic_retry"] is False
        and freeze_b["freeze_hash"] == build_live_pilot_freeze(source_revision="validations")["freeze_hash"]
        and [(c["task_key"], c["condition_id"]) for c in sequence[:3]]
        == [("line_wrap", "SMALL"), ("line_wrap", "MID"), ("line_wrap", "FRONTIER")],
    )
    no_ceiling = LivePilotController(output_root=work / "gate1", owner_budget_ceiling=None)
    ok_none, reasons_none = no_ceiling.budget_gate(conservative_bound=Decimal("0.253440000"))
    low = LivePilotController(output_root=work / "gate2", owner_budget_ceiling="0.01")
    ok_low, reasons_low = low.budget_gate(conservative_bound=Decimal("0.253440000"))
    ok_good, _ = LivePilotController(output_root=work / "gate3", owner_budget_ceiling="1000000").budget_gate(
        conservative_bound=Decimal("0.253440000")
    )
    add(
        "X owner budget gate (never silent; below-bound refused)",
        ok_none is False
        and any("not supplied" in r for r in reasons_none)
        and ok_low is False
        and any("below the conservative bound" in r for r in reasons_low)
        and ok_good is True,
    )
    rehearsal = LivePilotController(
        output_root=work / "rehearsal", owner_budget_ceiling="1000000", source_revision="validations"
    )
    rehearsal_result = rehearsal.run()
    executed = [r for r in rehearsal_result["rows"] if "verified" in r]
    add(
        "Y dry budget rehearsal: 15 cells, pinned pricing per cell, no winner",
        len(executed) == 15
        and rehearsal.provider_calls == 0
        and rehearsal.network_calls == 0
        and all(r["cost"]["schedule_hash"] == pricing_schedule_hash() for r in executed)
        and rehearsal_result["report"]["claims"]["no_winner_selected"] is True
        and rehearsal_result["report"]["claims"]["final_mcw_blocked_at_n5"] is True
        and next(f for f in rehearsal_result["report"]["frontier"] if f["worker_condition"] == "FRONTIER")["state"]
        == FRONTIER_PILOT_CANDIDATE_SUFFICIENT,
    )
    partial_ctrl = LivePilotController(
        output_root=work / "resume", owner_budget_ceiling="1000000", source_revision="validations"
    )
    saved_order = partial_ctrl.freeze["execution_order"]
    partial_ctrl.freeze["execution_order"] = saved_order[:4]
    partial = partial_ctrl.run()
    resumed = LivePilotController(
        output_root=work / "resume", owner_budget_ceiling="1000000", source_revision="validations"
    ).run()
    add(
        "Z resume after interruption: no duplicate calls, order preserved",
        len(partial["rows"]) == 4
        and len(resumed["rows"]) == 15
        and [r["cell_seq"] for r in resumed["rows"]] == list(range(1, 16))
        and resumed["rows"][:4] == partial["rows"],
    )
    drift_ctrl = LivePilotController(
        output_root=work / "drift", owner_budget_ceiling="1000000", source_revision="validations"
    )
    saved_criterion = mlp.MCW_QUALITY_CRITERION
    try:
        mlp.MCW_QUALITY_CRITERION = dict(saved_criterion) | {"drifted": True}
        drift_result = drift_ctrl.run()
    finally:
        mlp.MCW_QUALITY_CRITERION = saved_criterion
    pricing_ctrl = LivePilotController(
        output_root=work / "pricingdrift",
        owner_budget_ceiling="1000000",
        pinned_schedule_hash="0" * 64,
        source_revision="validations",
    )
    pricing_result = pricing_ctrl.run()
    redi09a_db = Path(__file__).resolve().parents[1] / "build" / "redi09a-mcw-dry-20260922T205849Z" / "mcw_evidence.sqlite"
    audit_ok = True
    if redi09a_db.exists():
        audit = audit_redi09a_manifest(redi09a_db)
        audit_ok = audit["frozen"] and audit["attempt_bindings_consistent"] and audit["matches_current_task_set"]
    add(
        "AA manifest drift + pricing drift fail-closed; REDI-09A manifest frozen",
        drift_result["stop_reason"] == STOP_REASON_MANIFEST_DRIFT
        and pricing_result["stop_reason"] == STOP_REASON_PRICING_MISMATCH
        and audit_ok,
    )

    print("REDI-09A MCW experiment foundation invariants")
    passed = 0
    for index, (name, ok) in enumerate(checks, 1):
        if ok:
            passed += 1
            print(f"  PASS {index:02d} {name}")
        else:
            print(f"  FAIL {index:02d} {name}")
    print("=" * 64)
    print(f"TOTAL: PASS={passed} FAIL={len(checks) - passed} CHECKS={len(checks)}")
    if passed != len(checks):
        return 1
    print("All REDI-09A validations passed (offline; zero provider/network).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
