"""REDI-09A MCW experiment foundation regressions.

The harness only: predeclared stratum/task-set/tiers, paired controls, frozen
ExperimentManifest, quality criterion with PILOT margins, deterministic
failure taxonomy, descriptive confidence, factual frontier states, reporting
guardrails, replay x2, drift immunity, and zero provider/network in dry mode.
No winner selection and no MCW conclusion exists at this stage.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from redi_lab.mcw_experiment import (
    ALLOWED_CLAIM_TEMPLATE,
    DEFAULT_DRY_OUTCOMES,
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
    build_mcw_manifest,
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
    worker_conditions_hash,
)
from redi_lab.persistence import bootstrap_schema  # noqa: F401  (schema reuse reference)
from redi_lab.persistence.integrity import payload_hash_from_json
from redi_lab.domain import domain_to_json

import sqlite3


HOLDOUT_PACKAGES = {"depot_replenishment", "bulk_inventory", "clinic_replenishment", "inventory_math"}


@pytest.fixture(scope="module")
def dry(tmp_path_factory):
    work = tmp_path_factory.mktemp("redi09a")
    return run_mcw_experiment(output_root=work / "mcw", source_revision="redi09a-unit")


# ---------------------------------------------------------------------------
# 3. Stratum identity
# ---------------------------------------------------------------------------


def test_stratum_identity_is_versioned_and_bounded():
    stratum = default_stratum()
    assert stratum.stratum_id == MCW_STRATUM_ID == "mcw_python_component_v1"
    assert stratum.version == 1
    assert stratum.station_role == "implementation_builder"
    # Narrower than "coding", broader than one fixture.
    assert "single pure-Python module" in stratum.inclusion_rules[0]
    assert len(stratum.inclusion_rules) >= 5 and len(stratum.exclusion_rules) >= 4
    # Never stratified by model identity.
    blob = json.dumps(stratum.to_dict()).lower()
    for model in ("gpt-4o-mini", "gpt-4.1", "gpt-5.6", "terra"):
        assert model not in blob
    assert stratum.canonical_hash == default_stratum().canonical_hash


# ---------------------------------------------------------------------------
# 4. Task set
# ---------------------------------------------------------------------------


def test_task_set_is_multiple_unseen_tasks_in_one_stratum():
    tasks = default_task_set()
    assert 4 <= len(tasks) <= 6
    keys = [task.key for task in tasks]
    assert len(set(keys)) == len(keys)
    for task in tasks:
        # No task copied from prior live holdouts.
        assert task.package not in HOLDOUT_PACKAGES
        # Deterministic public AND private verification on the same station duty.
        assert 3 <= len(task.public_checks) <= 8
        assert 3 <= len(task.private_checks) <= 8
        assert task.contract.expected_path == task.rel_path
        # References and private checks exist harness-side for every task.
        assert "NotImplementedError" in task.baseline_files[task.rel_path]
    # Persisted version + hash are stable.
    assert task_set_id() == "mcw_pilot_python_component_v1"
    assert task_set_hash() == task_set_hash(default_task_set())


def test_reference_implementations_satisfy_every_check():
    for task in default_task_set():
        namespace: dict = {}
        exec(task.reference_implementation, namespace)  # noqa: S102 - pilot fixtures
        for check in (
            list(task.public_checks)
            + list(task.private_checks)
            + [task.integration_probe]
            + list(task.reviewer_checks)
        ):
            target = namespace[str(check["target"])]
            args = list(check["args"])
            if check["kind"] == "call_equals":
                assert target(*args) == check["expected"], (task.key, check)
            else:
                exception = {
                    "ValueError": ValueError,
                    "TypeError": TypeError,
                    "KeyError": KeyError,
                }[str(check["exception"])]
                with pytest.raises(exception):
                    target(*args)


# ---------------------------------------------------------------------------
# 5. Worker tiers
# ---------------------------------------------------------------------------


def test_worker_tiers_are_descriptive_roles_with_exact_identities():
    ids = [c.condition_id for c in MCW_WORKER_CONDITIONS]
    assert ids == ["SMALL", "MID", "FRONTIER"]
    models = set()
    for condition in MCW_WORKER_CONDITIONS:
        assert condition.provider == "openai"
        assert condition.model
        assert "NOT an intelligence claim" in condition.role_description
        runtime = dict(condition.planned_runtime)
        assert runtime["max_output_tokens"] == 2048
        assert runtime["response_format"] == "json_schema_strict"
        models.add(condition.model)
    assert len(models) == len(MCW_WORKER_CONDITIONS)
    # The FRONTIER tier binds the accepted REDI-08 closure runtime exactly.
    frontier = next(c for c in MCW_WORKER_CONDITIONS if c.condition_id == "FRONTIER")
    assert frontier.model == "gpt-5.6-terra"
    assert frontier.planned_runtime["api"] == "chat_completions"
    assert frontier.planned_runtime["reasoning_effort"] == "low"
    assert frontier.planned_runtime["api_capability_profile"] == "openai_chat_gpt56_v1"
    assert worker_conditions_hash() == worker_conditions_hash()


# ---------------------------------------------------------------------------
# 6/13. Paired controls, order, isolation
# ---------------------------------------------------------------------------


def test_condition_order_is_deterministic_counterbalancing():
    expected = [
        ("SMALL", "MID", "FRONTIER"),
        ("MID", "FRONTIER", "SMALL"),
        ("FRONTIER", "SMALL", "MID"),
        ("SMALL", "MID", "FRONTIER"),
        ("MID", "FRONTIER", "SMALL"),
    ]
    for index, order in enumerate(expected):
        assert condition_order_for_task(index, MCW_WORKER_CONDITIONS) == order
    assert condition_order_for_task(3, MCW_WORKER_CONDITIONS) == condition_order_for_task(0, MCW_WORKER_CONDITIONS)


def test_paired_cells_share_identical_task_semantics(dry):
    by_task = {}
    for row in dry.rows:
        by_task.setdefault(row["task_key"], []).append(row)
    for task_key, rows in by_task.items():
        assert len(rows) == len(MCW_WORKER_CONDITIONS)
        assert {row["task_hash"] for row in rows} == {rows[0]["task_hash"]}
        # Identical condition orders recorded; only worker config differs.
        assert len({json.dumps(row["condition_order"]) for row in rows}) == 1
        condition_ids = {row["condition_id"] for row in rows}
        assert condition_ids == {c.condition_id for c in MCW_WORKER_CONDITIONS}


def test_condition_isolation_and_no_contamination(dry):
    workspaces = set()
    for row in dry.rows:
        context = row["context"]
        assert context["reference_leak_detected"] is False
        assert context["private_check_args_in_context"] is False
        assert row["retries"] == 0 and row["escalations"] == 0
    for condition in MCW_WORKER_CONDITIONS:
        for task in default_task_set():
            cell_root = dry.root / "conditions" / condition.condition_id / task.key
            assert cell_root.is_dir()
            workspaces.add(str(cell_root / "workspace").lower())
    assert len(workspaces) == len(dry.rows)


# ---------------------------------------------------------------------------
# 7/8. Preregistration and frozen quality criterion
# ---------------------------------------------------------------------------


def test_manifest_is_preregistered_frozen_and_hash_bound(dry):
    assert dry.manifest_id
    assert dry.manifest_sha256
    conn = sqlite3.connect(
        "file:" + dry.db_path.resolve().as_posix() + "?mode=ro&immutable=1", uri=True
    )
    try:
        row = conn.execute(
            "SELECT frozen_ts, payload_sha256, payload_json FROM experiment_manifests WHERE id=?",
            (dry.manifest_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    frozen_ts, payload_sha, payload_json = row
    assert frozen_ts is not None
    assert payload_sha == dry.manifest_sha256
    payload = json.loads(payload_json)
    assert payload["metadata"]["experiment_family"] == MCW_EXPERIMENT_FAMILY
    assert payload["task_selection"]["task_set_sha256"] == task_set_hash()
    assert payload["task_selection"]["selection_before_results"] is True
    assert payload["randomization"]["fixed_before_results"] is True
    assert payload["validity_controls"]["worker_only_treatment_variable"] is True
    # Every attempt evidence row binds the frozen manifest hash.
    for snapshot_row in replay_mcw_snapshot(dry.db_path, dry.factory_run_id):
        pass
    bound = {
        item["manifest_sha256"]
        for item in (
            json.loads(pj)["metadata"]
            for pj in _ledger_payloads(dry.db_path, "RunEvidence")
        )
        if item.get("evidence_kind") == "mcw_worker_attempt"
    }
    assert bound == {dry.manifest_sha256}
    # The registered hash is exactly the hash of the stored payload, and every
    # attempt binds that hash (rebuilding the manifest now would mint a new
    # preregistration timestamp, which must never retroactively match).
    assert payload_hash_from_json(payload_json) == dry.manifest_sha256


def _ledger_payloads(db_path: Path, entity_type: str) -> list[str]:
    conn = sqlite3.connect(
        "file:" + Path(db_path).resolve().as_posix() + "?mode=ro&immutable=1", uri=True
    )
    try:
        return [
            pj
            for (pj,) in conn.execute(
                "SELECT payload_json FROM evidence_entries WHERE entity_type=? ORDER BY global_sequence",
                (entity_type,),
            )
        ]
    finally:
        conn.close()


def test_quality_criterion_is_predeclared_with_pilot_margin():
    criterion = MCW_QUALITY_CRITERION
    assert criterion["primary"] == "mandatory deterministic verification success"
    assert set(criterion["required_gates"]) == set(GATE_ORDER)
    assert criterion["components"]["private_verification_pass"] is True
    assert criterion["components"]["no_scope_violation"] is True
    assert criterion["components"]["subjective_model_scores_used"] is False
    margin = criterion["statistical_margin"]
    assert margin["status"] == "PILOT_NOT_YET_ESTIMATED"
    assert margin["method"] is None and margin["value"] is None
    assert margin["non_inferiority_claimed"] is False


# ---------------------------------------------------------------------------
# 10/11. Confidence + frontier
# ---------------------------------------------------------------------------


def test_pilot_confidence_is_descriptive_without_capability_scores():
    rows = [
        {"verified": True, "failure_classification": None},
        {"verified": False, "failure_classification": "SCOPE_VIOLATION"},
        {"verified": False, "failure_classification": "SCOPE_VIOLATION"},
        {"verified": True, "failure_classification": None},
    ]
    confidence = pilot_confidence(rows)
    assert confidence["method"] == "descriptive_pilot_only"
    assert confidence["n_tasks"] == 4
    assert confidence["successes"] == 2 and confidence["failures"] == 2
    assert confidence["failure_classes"] == {"SCOPE_VIOLATION": 2}
    assert confidence["observed_success_proportion"] == 0.5
    assert confidence["uncertainty_interval"] is None
    assert confidence["interval_method"] == "NOT_ESTIMATED_PILOT_SAMPLE_ASSUMPTIONS_NOT_MET"
    assert "coverage_warning" in confidence
    assert "global_capability_score" not in confidence


def test_frontier_state_rule_cannot_declare_pilot_winners():
    # 5/5 pilot success is still INSUFFICIENT_EVIDENCE (n < 8, margin unestimated).
    assert (
        frontier_state(verified_count=5, n_tasks=5, observed_verified_failure=False, margin_estimated=False)
        == FRONTIER_INSUFFICIENT_EVIDENCE
    )
    assert (
        frontier_state(verified_count=4, n_tasks=5, observed_verified_failure=True, margin_estimated=False)
        == FRONTIER_NOT_SUFFICIENT_OBSERVED
    )
    # The threshold path exists but cannot fire in the REDI-09A pilot shape.
    assert (
        frontier_state(verified_count=8, n_tasks=8, observed_verified_failure=False, margin_estimated=True)
        == FRONTIER_CANDIDATE_SUFFICIENT
    )
    assert MCW_SUFFICIENCY_MIN_TASKS == 8 > len(default_task_set())


def test_dry_frontier_states_and_cost_unavailable(dry):
    states = {row["condition_id"]: row for row in dry.report["frontier"]}
    assert states["FRONTIER"]["state"] == FRONTIER_INSUFFICIENT_EVIDENCE
    assert states["MID"]["state"] == FRONTIER_NOT_SUFFICIENT_OBSERVED
    assert states["SMALL"]["state"] == FRONTIER_NOT_SUFFICIENT_OBSERVED
    for row in dry.report["frontier"]:
        assert row["cost"]["status"] == "unavailable_unversioned"
        assert row["cost"]["pricing_schedule_version"] is None
        assert row["station"] == "station_mcw_implementation_v0"
        assert row["task_class"] == "small_bounded_pure_python_component"


# ---------------------------------------------------------------------------
# 12. Failure taxonomy
# ---------------------------------------------------------------------------


def test_failure_taxonomy_mapping_is_predeclared_and_typed():
    clean = {gate: "PASS" for gate in GATE_ORDER}
    cases = [
        (
            {"provider_incomplete": True, "gate_outcomes": clean, "structured_error": None, "unexpected_error": None},
            ("PROVIDER_OUTPUT_TRUNCATED_OR_INCOMPLETE", "timeout_or_budget_exhaustion"),
        ),
        (
            {
                "provider_incomplete": False,
                "gate_outcomes": {**clean, "structured_output_schema": "FAIL"},
                "structured_error": "STRUCTURED_OUTPUT_SCHEMA_FAILURE role=builder category=JSONDecodeError",
                "unexpected_error": None,
            },
            ("STRUCTURED_OUTPUT_SCHEMA_FAILURE", "deterministic_test_failure"),
        ),
        (
            {
                "provider_incomplete": False,
                "gate_outcomes": {**clean, "structured_output_schema": "FAIL", "patch_output_contract": "NOT_RUN"},
                "structured_error": "STRUCTURED_OUTPUT_SCHEMA_FAILURE reason=patch_0_path_does_not_match_contract",
                "unexpected_error": None,
            },
            ("SCOPE_VIOLATION", "scope_violation"),
        ),
        (
            {
                "provider_incomplete": False,
                "gate_outcomes": {**clean, "private_test_execution": "FAIL"},
                "structured_error": None,
                "unexpected_error": None,
            },
            ("WORKER_IMPLEMENTATION_FAILURE", "deterministic_test_failure"),
        ),
        (
            {
                "provider_incomplete": False,
                "gate_outcomes": {**clean, "independent_final_reviewer": "FAIL"},
                "structured_error": None,
                "unexpected_error": None,
            },
            ("REVIEWER_REJECTION", "deterministic_test_failure"),
        ),
        (
            {"provider_incomplete": False, "gate_outcomes": clean, "structured_error": None, "unexpected_error": "OSError: disk"},
            ("ENVIRONMENT_TOOL_FAILURE", "tool_failure"),
        ),
    ]
    for kwargs, (classification, category) in cases:
        result = classify_mcw_failure(**kwargs)
        assert result == (classification, category), (kwargs, result)


def test_dry_experiment_exercises_controlled_failure_classes(dry):
    outcomes = {}
    for row in dry.rows:
        outcomes[(row["condition_id"], row["task_key"])] = row["failure_classification"]
    # One condition succeeds on all tasks; one on a subset; one fails representatives.
    frontier_successes = sum(1 for row in dry.rows if row["condition_id"] == "FRONTIER" and row["verified"])
    assert frontier_successes == len(default_task_set())
    assert outcomes[("MID", "lru_cache_sim")] == "WORKER_IMPLEMENTATION_FAILURE"
    assert outcomes[("SMALL", "interval_merge")] == "PROVIDER_OUTPUT_TRUNCATED_OR_INCOMPLETE"
    assert outcomes[("SMALL", "scoreboard")] == "SCOPE_VIOLATION"
    assert outcomes[("SMALL", "lru_cache_sim")] == "WORKER_IMPLEMENTATION_FAILURE"
    assert DEFAULT_DRY_OUTCOMES[("FRONTIER", "line_wrap")] == "success"


# ---------------------------------------------------------------------------
# 17. Reporting guardrails
# ---------------------------------------------------------------------------


def test_guardrails_block_forbidden_fields_and_phrases():
    assert "winner" in FORBIDDEN_REPORT_FIELD_NAMES
    assert "best model" in FORBIDDEN_REPORT_SUBSTRINGS
    for bad in (
        {"report": {"winner": "SMALL"}},
        {"report": {"best_model": "gpt-5.6-terra"}},
        {"summary": "model X is smarter than model Y"},
        {"note": "this worker has 90% intelligence"},
        {"text": "the best model overall"},
        {"claim": "globally cheapest capable model"},
        {"frontier": {"leaderboard": []}},
        {"claim": "superior model"},
    ):
        with pytest.raises(MCWReportGuardrailError):
            enforce_report_guardrails(bad)
    # The allowed claim shape passes.
    enforce_report_guardrails(
        {"claim": ALLOWED_CLAIM_TEMPLATE.format(station="s", task_class="t", experiment="e", worker="w", n=5)}
    )


def test_built_report_passes_guardrails_and_claims_nothing(dry):
    report = dry.report
    enforce_report_guardrails(report)
    assert report["claims"]["no_winner_selected"] is True
    assert report["claims"]["minimum_capable_declared"] is False
    assert report["claims"]["model_superiority_claimed"] is False
    assert report["counts_as_mcw_conclusion"] is False
    assert report["experiment_family"] == MCW_EXPERIMENT_FAMILY
    assert report["policy"] == {"id": MCW_POLICY_ID, "version": 1}
    # The report literally cannot carry a forbidden claim field.
    def _all_keys(node):
        if isinstance(node, dict):
            for key, value in node.items():
                yield str(key)
                yield from _all_keys(value)
        elif isinstance(node, list):
            for item in node:
                yield from _all_keys(item)

    assert set(_all_keys(report)) & set(FORBIDDEN_REPORT_FIELD_NAMES) == set()


# ---------------------------------------------------------------------------
# 9. Pricing decision
# ---------------------------------------------------------------------------


def test_cost_is_never_invented_without_versioned_pricing(dry):
    for row in dry.rows:
        assert row["cost"]["status"] == "unavailable_unversioned"
        assert row["cost"]["pricing_schedule_version"] is None
        assert row["cost"]["amount"] is None
    plan = dry.report["live_budget_plan"]
    assert plan["pricing_status"].startswith("unavailable_unversioned")
    assert "no cost estimate is possible or claimed" in plan["pricing_status"]


# ---------------------------------------------------------------------------
# 15. Live-budget preparation (never executed here)
# ---------------------------------------------------------------------------


def test_live_budget_plan_is_prepared_and_blocks_excess():
    plan = live_budget_plan()
    assert plan["n_tasks"] == len(default_task_set()) == 5
    assert plan["n_worker_conditions"] == 3
    assert plan["max_live_calls"] == 15
    assert plan["per_condition_call_budget"] == {"SMALL": 5, "MID": 5, "FRONTIER": 5}
    assert plan["automatic_retries"] is False
    assert plan["block_if_excessive"]["within_budget"] is True
    assert plan["block_if_excessive"]["action"] == "allow"
    models = {entry["model"] for entry in plan["provider_models"]}
    assert models == {c.model for c in MCW_WORKER_CONDITIONS}
    # Excess is blocked, never silently run.
    bloated = live_budget_plan(tasks=tuple(default_task_set()) * 7)
    assert bloated["max_live_calls"] > MCW_MAX_LIVE_CALLS
    assert bloated["block_if_excessive"]["action"] == "BLOCK"


def test_dry_run_rejects_live_execution(tmp_path):
    with pytest.raises(ValueError, match="harness-only"):
        run_mcw_experiment(output_root=tmp_path / "live", dry=False)


# ---------------------------------------------------------------------------
# 14/16. Dry experiment, replay x2, drift immunity
# ---------------------------------------------------------------------------


def test_dry_experiment_replay_x2_and_ledger_agreement(dry):
    one = replay_mcw_result(dry.result_path)
    two = replay_mcw_result(dry.result_path)
    assert one == two
    assert one["replay_dependencies"] == {
        "workers": 0,
        "provider_calls": 0,
        "network": 0,
        "writes": 0,
        "recipe_reselection": False,
        "current_worker_registry": False,
        "current_pricing": False,
    }
    snapshot = replay_mcw_snapshot(dry.db_path, dry.factory_run_id)
    assert len(snapshot) == len(dry.rows)
    by_key = {(row["condition_id"], row["task_key"]): row for row in snapshot}
    for row in dry.rows:
        assert by_key[(row["condition_id"], row["task_key"])]["verified"] == row["verified"]
    assert dry.provider_calls == 0 and dry.network_calls == 0
    assert dry.verified is False  # controlled pilot failures exist by design


def test_replay_is_immune_to_registry_pricing_and_task_drift(dry, monkeypatch):
    import redi_lab.mcw_experiment as mcw

    before_result = replay_mcw_result(dry.result_path)
    before_snapshot = replay_mcw_snapshot(dry.db_path, dry.factory_run_id)
    monkeypatch.setattr(mcw, "MCW_WORKER_CONDITIONS", ())
    monkeypatch.setattr(mcw, "default_task_set", lambda: ())
    monkeypatch.setattr(mcw, "task_set_hash", lambda tasks=None: "drifted")
    monkeypatch.setattr(mcw, "MCW_QUALITY_CRITERION", {"drifted": True})
    monkeypatch.setattr(mcw, "MCW_SUFFICIENCY_MIN_TASKS", 1)
    monkeypatch.setattr(mcw, "default_stratum", lambda: None)
    after_result = replay_mcw_result(dry.result_path)
    after_snapshot = replay_mcw_snapshot(dry.db_path, dry.factory_run_id)
    assert after_result == before_result
    assert after_snapshot == before_snapshot
    # Exact persisted model identifiers survive the drift.
    persisted_models = {row["model"] for row in after_snapshot}
    assert persisted_models == {c.model for c in MCW_WORKER_CONDITIONS}
