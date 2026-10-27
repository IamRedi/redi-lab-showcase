"""REDI-07A controlled cold-start acceptance tests (offline, deterministic)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from redi_lab.cold_start import (
    COLD_START_REASON_NO_RECIPE_SUPPLIED,
    COLD_START_UNKNOWN_PRODUCT,
    CandidateGenerationError,
    ColdStartPolicy,
    ColdStartSelectionError,
    ExplorationBudget,
    canonical_cold_start_replay_view,
    controlled_product_spec,
    controlled_stations,
    derive_product_class,
    derive_product_signature,
    escalation_action,
    failure_action_table,
    generate_recipe_candidate,
    plan_cold_start_v0,
    replay_cold_start,
    run_controlled_cold_start,
    select_cold_start_mode,
)
from redi_lab.dag_scheduler import NodeRunState, product_dag_hash
from redi_lab.domain import (
    FactoryRun,
    FailureCategory,
    LifecycleEventType,
    Mode,
    ProductRecipe,
    RecipeRung,
    RunStatus,
)
from redi_lab.persistence import EvidenceLedger, FactoryRunRepository, bootstrap_schema


@pytest.fixture(scope="module")
def cold_run(tmp_path_factory):
    return run_controlled_cold_start(
        output_root=tmp_path_factory.mktemp("redi07a_verified"),
        known_recipe=None,
    )


@pytest.fixture(scope="module")
def unverified_run(tmp_path_factory):
    return run_controlled_cold_start(
        output_root=tmp_path_factory.mktemp("redi07a_unverified"),
        known_recipe=None,
        force_unverified=True,
    )


@pytest.fixture(scope="module")
def budget_run(tmp_path_factory):
    policy = ColdStartPolicy(
        budget=ExplorationBudget(
            max_candidate_workers_per_node=1,
            max_attempts_per_worker=1,
            max_total_attempts_per_node=1,
            max_total_attempts_per_run=2,
        )
    )
    return run_controlled_cold_start(
        output_root=tmp_path_factory.mktemp("redi07a_budget"),
        known_recipe=None,
        policy=policy,
    )


@pytest.fixture(scope="module")
def material_run(tmp_path_factory):
    return run_controlled_cold_start(
        output_root=tmp_path_factory.mktemp("redi07a_material_change"),
        known_recipe=None,
        material_variant="v2-material-change",
    )


def _plan(material_variant: str = "v1"):
    product = controlled_product_spec(material_variant=material_variant)
    signature = derive_product_signature(product)
    product_class = derive_product_class(product, signature)
    product.product_class_id = product_class.id
    policy = ColdStartPolicy()
    return product, signature, plan_cold_start_v0(
        product=product,
        product_class=product_class,
        signature=signature,
        stations=controlled_stations(),
        policy=policy,
    )


def _count(db_path: Path, sql: str, params=()) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(sql, params).fetchone()[0])
    finally:
        conn.close()


def test_a_unknown_product_requires_explicit_none_recipe_and_selects_mode(cold_run):
    assert cold_run.selection.mode == COLD_START_UNKNOWN_PRODUCT
    assert cold_run.selection.reason == COLD_START_REASON_NO_RECIPE_SUPPLIED
    assert cold_run.selection.recipe_lookup_result == "none"
    with pytest.raises(ColdStartSelectionError):
        select_cold_start_mode(
            known_recipe=ProductRecipe(),
            reason=COLD_START_REASON_NO_RECIPE_SUPPLIED,
        )


def test_b_same_semantics_and_policy_produce_same_signature_and_dag_hash():
    _, signature_a, plan_a = _plan()
    _, signature_b, plan_b = _plan()
    assert signature_a.sha256 == signature_b.sha256
    assert str(plan_a.dag.id) == str(plan_b.dag.id)
    assert product_dag_hash(plan_a.dag) == product_dag_hash(plan_b.dag)


def test_c_material_requirement_change_changes_signature_and_dag_hash():
    _, signature_a, plan_a = _plan("v1")
    _, signature_b, plan_b = _plan("v2-material-change")
    assert signature_a.sha256 != signature_b.sha256
    assert product_dag_hash(plan_a.dag) != product_dag_hash(plan_b.dag)


def test_d_worker_a_failure_is_deterministically_classified(cold_run):
    replay = replay_cold_start(db_path=cold_run.db_path, factory_run_id=cold_run.factory_run_id)
    failed = [item for item in replay["attempt_trajectory"] if item["failure_category"]]
    assert len(failed) == 1
    assert failed[0]["worker_id"] == "worker_cold_cheap_weak_v0"
    assert failed[0]["failure_category"] == FailureCategory.DETERMINISTIC_TEST_FAILURE.value


def test_e_failed_attempt_and_full_boundary_persist_after_success(cold_run):
    replay = replay_cold_start(db_path=cold_run.db_path, factory_run_id=cold_run.factory_run_id)
    failure = replay["failures"][0]
    assert failure["worker_id"] == "worker_cold_cheap_weak_v0"
    assert failure["context_package_hash"]
    assert failure["gate_boundary"]["gate_identity"] == "public_test_execution"
    assert failure["retry_escalation_decision"] == "worker_escalation"
    assert replay["final_verification_state"] is True


def test_candidate_inventory_records_considered_and_rejected_reasons(cold_run):
    replay = replay_cold_start(db_path=cold_run.db_path, factory_run_id=cold_run.factory_run_id)
    inventory = replay["node_exploration"]["implement_normalize"]["candidate_inventory"]
    assert [item["worker_id"] for item in inventory[:2]] == [
        "worker_cold_cheap_weak_v0",
        "worker_cold_strong_fallback_v0",
    ]
    rejected = inventory[2]
    assert rejected["considered"] is False
    assert "rejected_by_max_candidate_workers_per_node" in rejected["rejected_or_ineligible_reasons"]
    assert "availability_status=unavailable" in rejected["rejected_or_ineligible_reasons"]


def test_f_escalation_routing_is_failure_class_specific():
    expected = {
        FailureCategory.DETERMINISTIC_TEST_FAILURE: "worker_escalation",
        FailureCategory.CONTEXT_INSUFFICIENCY: "context_rebuild_then_same_or_next_worker",
        FailureCategory.CAPABILITY_INSUFFICIENCY: "worker_escalation",
        FailureCategory.TOOL_FAILURE: "orchestrator_tool_retry_no_capability_penalty",
        FailureCategory.ENVIRONMENT_FAILURE: "orchestrator_environment_retry_no_capability_penalty",
        FailureCategory.SCOPE_VIOLATION: "fail_closed_scope_violation",
        FailureCategory.INTEGRATION_CONFLICT: "integration_orchestrator_escalation",
        FailureCategory.TIMEOUT_OR_BUDGET_EXHAUSTION: "stop_budget_exhausted",
    }
    assert {category.value: escalation_action(category) for category in expected} == {
        category.value: action for category, action in expected.items()
    }
    assert set(expected).issubset({FailureCategory(key) for key in failure_action_table()})


def test_g_worker_b_succeeds_without_station_or_dag_node_change(cold_run):
    replay = replay_cold_start(db_path=cold_run.db_path, factory_run_id=cold_run.factory_run_id)
    normalize = [item for item in replay["attempt_trajectory"] if item["node_id"] == "implement_normalize"]
    assert [item["worker_id"] for item in normalize] == [
        "worker_cold_cheap_weak_v0",
        "worker_cold_strong_fallback_v0",
    ]
    assert len({item["station_id"] for item in normalize}) == 1
    assert len({item["node_id"] for item in normalize}) == 1


def test_h_exploration_budget_is_enforced_as_typed_failure(budget_run):
    assert budget_run.verified is False
    replay = replay_cold_start(db_path=budget_run.db_path, factory_run_id=budget_run.factory_run_id)
    categories = {item["category"] for item in replay["failures"]}
    assert FailureCategory.TIMEOUT_OR_BUDGET_EXHAUSTION.value in categories
    assert replay["recipe_candidate"] is None


def test_i_independent_roots_use_parallel_executor(cold_run):
    assert cold_run.execution.launch_order[:2] == ("implement_normalize", "implement_reorder")
    assert cold_run.execution.final_states["implement_normalize"] == NodeRunState.COMPLETED
    assert cold_run.execution.final_states["implement_reorder"] == NodeRunState.COMPLETED
    assert cold_run.execution.timing["aggregate_node_compute_seconds"] > 0


def test_j_integration_and_independent_reviewer_are_mandatory(cold_run):
    replay = replay_cold_start(db_path=cold_run.db_path, factory_run_id=cold_run.factory_run_id)
    passing = {item["identity"] for item in replay["gates"] if item["status"] == "pass"}
    assert "integration_verification" in passing
    assert "independent_final_reviewer" in passing
    assert cold_run.independent_reviewer_calls == 1


def test_k_unverified_product_creates_no_candidate(unverified_run):
    assert unverified_run.verified is False
    assert unverified_run.candidate is None
    assert _count(
        unverified_run.db_path,
        "SELECT COUNT(*) FROM evidence_entries WHERE payload_json LIKE '%RECIPE_CANDIDATE_CREATED%'",
    ) == 0
    with pytest.raises(CandidateGenerationError):
        generate_recipe_candidate(
            db_path=unverified_run.db_path,
            factory_run_id=unverified_run.factory_run_id,
        )


def test_l_verified_product_creates_exactly_one_candidate(cold_run):
    assert cold_run.verified is True and cold_run.candidate is not None
    assert cold_run.candidate.recipe.rung == RecipeRung.CANDIDATE
    assert _count(
        cold_run.db_path,
        "SELECT COUNT(*) FROM evidence_entries WHERE payload_json LIKE '%RECIPE_CANDIDATE_CREATED%'",
    ) == 1


def test_m_candidate_references_run_graph_and_evidence(cold_run):
    candidate = cold_run.candidate
    assert candidate is not None
    provenance = candidate.recipe.provenance
    assert provenance["source_factory_run_id"] == cold_run.factory_run_id
    assert provenance["source_graph_id"] == str(cold_run.plan.dag.id)
    assert provenance["source_graph_hash"] == product_dag_hash(cold_run.plan.dag)
    assert provenance["relevant_run_evidence_ids"]
    assert provenance["relevant_failure_record_ids"]
    assert provenance["relevant_gate_result_ids"]


def test_n_same_factory_run_candidate_generation_is_idempotent(cold_run):
    first = cold_run.candidate
    count_before = _count(cold_run.db_path, "SELECT COUNT(*) FROM evidence_entries")
    second = generate_recipe_candidate(
        db_path=cold_run.db_path,
        factory_run_id=cold_run.factory_run_id,
    )
    count_after = _count(cold_run.db_path, "SELECT COUNT(*) FROM evidence_entries")
    assert first is not None
    assert second.identity_hash == first.identity_hash
    assert second.canonical_recipe_sha256 == first.canonical_recipe_sha256
    assert domain_json(second.recipe) == domain_json(first.recipe)
    assert count_after == count_before


def domain_json(recipe: ProductRecipe) -> str:
    from redi_lab.domain import domain_to_json

    return domain_to_json(recipe)


def test_o_candidate_is_not_promoted_and_no_promotion_event_exists(cold_run):
    candidate = cold_run.candidate
    assert candidate is not None
    assert candidate.recipe.metadata["promotion_status"] == "NOT_PROMOTED"
    replay = replay_cold_start(db_path=cold_run.db_path, factory_run_id=cold_run.factory_run_id)
    assert replay["recipe_promoted_event"] is False
    assert LifecycleEventType.RECIPE_PROMOTED.value not in replay["event_types"]


def test_p_historical_replay_twice_is_identical_and_read_only(cold_run):
    before_bytes = cold_run.db_path.read_bytes()
    before_stat = cold_run.db_path.stat()
    first = canonical_cold_start_replay_view(
        replay_cold_start(db_path=cold_run.db_path, factory_run_id=cold_run.factory_run_id)
    )
    second = canonical_cold_start_replay_view(
        replay_cold_start(db_path=cold_run.db_path, factory_run_id=cold_run.factory_run_id)
    )
    after_stat = cold_run.db_path.stat()
    assert first == second
    assert cold_run.db_path.read_bytes() == before_bytes
    assert after_stat.st_size == before_stat.st_size
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns


def test_q_current_pool_context_planner_and_candidate_drift_cannot_change_history(cold_run, monkeypatch):
    import redi_lab.cold_start as cold_start_module

    before = canonical_cold_start_replay_view(
        replay_cold_start(db_path=cold_run.db_path, factory_run_id=cold_run.factory_run_id)
    )
    def forbidden_current_config(*_args, **_kwargs):
        raise AssertionError("historical replay consulted current planner/worker pool")

    monkeypatch.setattr(cold_start_module, "PLANNER_POLICY_ID", "future_planner_v99")
    monkeypatch.setattr(cold_start_module, "CONTEXT_STRATEGY_ID", "future_context_v99")
    monkeypatch.setattr(cold_start_module, "EXPLORATION_POLICY_ID", "future_exploration_v99")
    monkeypatch.setattr(cold_start_module, "plan_cold_start_v0", forbidden_current_config)
    monkeypatch.setattr(cold_start_module, "_controlled_worker_configuration", forbidden_current_config)
    after = canonical_cold_start_replay_view(
        replay_cold_start(db_path=cold_run.db_path, factory_run_id=cold_run.factory_run_id)
    )
    assert before == after
    assert after["recipe_candidate"]["source_factory_run_id"] == cold_run.factory_run_id


def test_r_missing_required_evidence_blocks_candidate_generation(tmp_path: Path):
    db = tmp_path / "missing-evidence.sqlite"
    conn = sqlite3.connect(db)
    bootstrap_schema(conn)
    ledger = EvidenceLedger(conn)
    repo = FactoryRunRepository(conn, ledger)
    run = FactoryRun(mode=Mode.EXPLORATION, final_status=RunStatus.RUNNING)
    repo.store(run)
    repo.close_run(str(run.id), final_status=RunStatus.COMPLETED, verified=True)
    conn.close()
    with pytest.raises(CandidateGenerationError, match="PRODUCT_VERIFIED"):
        generate_recipe_candidate(db_path=db, factory_run_id=str(run.id))


def test_s_controlled_proof_has_zero_provider_and_network_calls(cold_run):
    assert cold_run.provider_calls == 0
    assert cold_run.network_calls == 0
    replay = replay_cold_start(db_path=cold_run.db_path, factory_run_id=cold_run.factory_run_id)
    assert all("provider_calls" not in item or item["provider_calls"] == 0 for item in replay["attempt_trajectory"])


def test_candidate_identity_and_hash_are_immutable_for_exact_source(cold_run):
    candidate = cold_run.candidate
    assert candidate is not None
    assert str(candidate.recipe.id).endswith(candidate.identity_hash[:32])
    assert hashlib.sha256(domain_json(candidate.recipe).encode("utf-8")).hexdigest() == candidate.canonical_recipe_sha256


def test_material_source_change_produces_different_candidate_identity(cold_run, material_run):
    assert cold_run.candidate is not None and material_run.candidate is not None
    assert cold_run.signature.sha256 != material_run.signature.sha256
    assert cold_run.candidate.identity_hash != material_run.candidate.identity_hash
    assert cold_run.candidate.canonical_recipe_sha256 != material_run.candidate.canonical_recipe_sha256


def test_partial_open_run_cannot_create_candidate(tmp_path: Path):
    db = tmp_path / "partial.sqlite"
    conn = sqlite3.connect(db)
    bootstrap_schema(conn)
    ledger = EvidenceLedger(conn)
    repo = FactoryRunRepository(conn, ledger)
    run = FactoryRun(mode=Mode.EXPLORATION, final_status=RunStatus.RUNNING)
    repo.store(run)
    conn.close()
    with pytest.raises(CandidateGenerationError, match="partial/interrupted"):
        generate_recipe_candidate(db_path=db, factory_run_id=str(run.id))


def test_no_experiment_manifest_is_created_for_cold_start(cold_run):
    assert _count(cold_run.db_path, "SELECT COUNT(*) FROM experiment_manifests") == 0


def test_factory_run_persists_explicit_cold_start_selection(cold_run):
    conn = sqlite3.connect(cold_run.db_path)
    try:
        payload = json.loads(conn.execute(
            "SELECT payload_json FROM factory_runs WHERE id = ?", (cold_run.factory_run_id,)
        ).fetchone()[0])
    finally:
        conn.close()
    assert payload["mode"] == Mode.EXPLORATION.value
    assert payload["metadata"]["execution_mode"] == COLD_START_UNKNOWN_PRODUCT
    assert payload["metadata"]["recipe_lookup"]["result"] == "none"
