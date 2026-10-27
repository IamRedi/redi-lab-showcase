"""REDI-08B deterministic preregistered paired-comparison acceptance tests."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from redi_lab.cold_start import replay_cold_start, run_controlled_cold_start
from redi_lab.domain import RecipeRung
from redi_lab.paired_experiment import (
    CONDITION_COLD,
    CONDITION_ORDER,
    CONDITION_WARM,
    MAX_TOTAL_LIVE_CALLS,
    TransferFailureClass,
    WORKER_UNIVERSE,
    assert_retry_blocked,
    build_preregistered_manifest,
    classify_transfer_failure,
    execute_paired_experiment,
    prepare_shadow_recipe,
    product_spec_hash,
    replay_condition,
    replay_pair,
)
from redi_lab.recipe_store import RecipeStore, canonical_recipe_semantic_hash
from redi_lab.warm_start import (
    compile_warm_start_dag,
    controlled_warm_stations,
    evaluate_warm_start_eligibility,
    inventory_product_class,
    match_product_class,
    sibling_product_fixture,
)


ROOT = Path(__file__).resolve().parents[2]
HISTORICAL_LEDGER = ROOT / "artifacts" / "redi07b" / "redi07b-live-3962bb5a22bc" / "evidence.sqlite"
HISTORICAL_RUN = "factory_run_7f21dd99f6b94664be1434e4f6803393"


@pytest.fixture(scope="module")
def paired(tmp_path_factory):
    work = tmp_path_factory.mktemp("redi08b_pair")
    if HISTORICAL_LEDGER.exists():
        source_ledger = HISTORICAL_LEDGER
        source_run = HISTORICAL_RUN
    else:
        cold = run_controlled_cold_start(output_root=work / "source", known_recipe=None)
        source_ledger = cold.db_path
        source_run = cold.factory_run_id
    source = replay_cold_start(db_path=source_ledger, factory_run_id=source_run)
    candidate = json.loads(source["recipe_candidate"]["canonical_recipe_json"])
    product_class = inventory_product_class(str(candidate["product_class_id"]))
    product = sibling_product_fixture(str(product_class.id))
    store = RecipeStore(work / "recipes.sqlite")
    stored = prepare_shadow_recipe(
        store=store,
        source_ledger=source_ledger,
        source_factory_run_id=source_run,
        product_class=product_class,
        approval_reference="redi08b-unit-tests",
    )
    outcome = execute_paired_experiment(
        output_root=work / "pair",
        product=product,
        product_class=product_class,
        store=store,
        recipe_id=str(stored.recipe.id),
        recipe_version=stored.recipe.version,
        source_revision="redi08b-unit-source-revision",
        live=False,
    )
    yield {"work": work, "store": store, "stored": stored, "product_class": product_class, "product": product, "outcome": outcome}
    store.close()


def _views(paired):
    outcome = paired["outcome"]
    cold = replay_condition(db_path=outcome.cold.db_path, factory_run_id=outcome.cold.factory_run_id)
    warm = replay_condition(db_path=outcome.warm.db_path, factory_run_id=outcome.warm.factory_run_id)
    pair = replay_pair(outcome.paired_result_path)
    return cold, warm, pair


def test_a_manifest_created_before_conditions(paired):
    _, _, pair = _views(paired)
    assert pair["preregistration_proof"]["before_both_conditions"] is True
    assert paired["outcome"].preregistration.path.exists()


def test_b_same_sibling_product_in_both_conditions(paired):
    cold, warm, pair = _views(paired)
    assert cold["product_spec_sha256"] == warm["product_spec_sha256"] == pair["sibling_product_spec_hash"]


def test_c_source_baseline_is_byte_identical(paired):
    cold, warm, _ = _views(paired)
    assert cold["fixture_hash"] == warm["fixture_hash"]


def test_d_same_environment_and_gates(paired):
    manifest = paired["outcome"].preregistration.manifest
    cold = manifest.arms[CONDITION_COLD]["controlled_variables"]
    warm = manifest.arms[CONDITION_WARM]["controlled_variables"]
    assert cold["environment_policy_id"] == warm["environment_policy_id"]
    assert cold["mandatory_gates"] == warm["mandatory_gates"]


def test_e_same_worker_universe(paired):
    manifest = paired["outcome"].preregistration.manifest
    assert manifest.arms[CONDITION_COLD]["controlled_variables"]["worker_universe"] == list(WORKER_UNIVERSE)
    assert manifest.arms[CONDITION_WARM]["controlled_variables"]["worker_universe"] == list(WORKER_UNIVERSE)


def test_f_cold_condition_uses_no_recipe(paired):
    cold, _, _ = _views(paired)
    assert cold["recipe"] is None
    assert cold["lineage"]["recipe_consulted"] is False


def test_g_warm_condition_uses_pinned_recipe(paired):
    _, warm, pair = _views(paired)
    assert warm["recipe"] == pair["recipe"]
    assert warm["recipe"]["canonical_hash"] == canonical_recipe_semantic_hash(paired["stored"].recipe)
    assert warm["recipe"]["content_hash"] == paired["stored"].canonical_hash


def test_h_warm_does_not_invoke_cold_planner(paired):
    _, warm, _ = _views(paired)
    assert warm["lineage"]["cold_planner_used"] is False


def test_i_cold_invokes_conservative_planner(paired):
    cold, _, _ = _views(paired)
    assert cold["lineage"]["cold_planner_used"] is True
    assert cold["lineage"]["conservative"] is True


def test_j_no_cross_condition_artifact_leakage(paired):
    outcome = paired["outcome"]
    assert outcome.cold.db_path.parent != outcome.warm.db_path.parent
    assert outcome.cold.db_path.parent not in outcome.warm.db_path.parents
    assert outcome.warm.db_path.parent not in outcome.cold.db_path.parents


def test_k_total_live_call_budget_is_hard_capped(paired):
    _, _, pair = _views(paired)
    assert pair["planned_live_budget"]["planned_live_calls_total"] == 2
    assert pair["planned_live_budget"]["planned_live_calls_total"] <= MAX_TOTAL_LIVE_CALLS


def test_l_provider_retry_is_blocked():
    assert_retry_blocked()


def test_m_paired_metrics_separate_provider_and_local(paired):
    _, _, pair = _views(paired)
    for condition in CONDITION_ORDER:
        accounting = pair["metrics"][condition]["accounting"]
        assert set(accounting) == {"provider_only", "all_system_local"}
        assert accounting["provider_only"]["calls"] == 0
        assert accounting["all_system_local"]["synthetic_fake"]["adapter_calls"] == 1


def test_n_paired_result_contains_no_winner_field(paired):
    _, _, pair = _views(paired)
    assert "winner" not in pair
    assert pair["claims"]["automatic_comparative_claim_emitted"] is False
    assert pair["claims"]["warm_superiority_established"] is False


def test_o_warm_transfer_failure_taxonomy_is_typed():
    assert classify_transfer_failure("execution") == TransferFailureClass.RECIPE_EXECUTION_FAILURE
    assert {class_.value for class_ in TransferFailureClass} == {
        "class_match_failure", "recipe_validity_failure", "adaptation_failure",
        "recipe_worker_unavailable", "recipe_context_invalid", "recipe_gate_incompatibility",
        "recipe_topology_mismatch", "recipe_execution_failure", "recipe_integration_failure",
        "recipe_revalidation_failure",
    }


def test_p_recipe_remains_shadow_not_stable(paired):
    assert paired["stored"].lifecycle_state == RecipeRung.SHADOW
    _, _, pair = _views(paired)
    assert pair["recipe"]["lifecycle_state"] == RecipeRung.SHADOW.value


def test_q_no_recipe_promoted_event(paired):
    cold, warm, _ = _views(paired)
    assert cold["recipe_promoted"] is False
    assert warm["recipe_promoted"] is False


def test_r_newer_recipe_version_does_not_change_history(paired):
    store = paired["store"]
    stored = paired["stored"]
    if len(store.list_versions(str(stored.recipe.id))) < 3:
        store.register_version(
            recipe=replace(stored.recipe, version=3, created_at=datetime.now(timezone.utc), metadata=dict(stored.recipe.metadata) | {"future_trial": True}),
            product_class=paired["product_class"],
            confidence=stored.confidence,
            provenance=dict(stored.provenance) | {"parent_recipe_version": 2},
            parent_version=2,
            rollback_target_version=2,
            revalidation_state="VALID_FOR_CONTROLLED_EXPERIMENT",
            registration_metadata={"future_trial": True},
            event_type="EXPERIMENTAL_VERSION_CREATED",
            policy_id="redi08b_drift_test_v0",
            policy_version=1,
        )
    _, warm, pair = _views(paired)
    assert store.read(str(stored.recipe.id), 3).recipe.version == 3
    assert warm["recipe"]["recipe_version"] == pair["recipe"]["recipe_version"] == 2


def test_s_each_condition_replays_twice_identically(paired):
    outcome = paired["outcome"]
    for condition in (outcome.cold, outcome.warm):
        one = replay_condition(db_path=condition.db_path, factory_run_id=condition.factory_run_id)
        two = replay_condition(db_path=condition.db_path, factory_run_id=condition.factory_run_id)
        assert one == two


def test_t_pair_replays_twice_identically(paired):
    path = paired["outcome"].paired_result_path
    assert replay_pair(path) == replay_pair(path)


def test_u_config_drift_does_not_affect_replay(paired, monkeypatch):
    import redi_lab.paired_experiment as module

    before = replay_pair(paired["outcome"].paired_result_path)
    monkeypatch.setattr(module, "WORKER_UNIVERSE", ("future_worker_v99",))
    monkeypatch.setattr(module, "CONTEXT_POLICY_ID", "future_context_v99")
    monkeypatch.setattr(module, "WARM_START_ADAPTATION_POLICY_ID", "future_adaptation_v99")
    after = replay_pair(paired["outcome"].paired_result_path)
    assert before == after


def test_v_exact_dry_live_runner_path_has_zero_provider_network(paired):
    outcome = paired["outcome"]
    assert outcome.cold.verified and outcome.warm.verified
    assert outcome.cold.provider_calls == outcome.warm.provider_calls == 0
    assert outcome.cold.network_calls == outcome.warm.network_calls == 0
    assert all(outcome.audit.values())


def test_w_semantic_identity_ignores_runtime_identity_and_timestamps(paired):
    product = paired["product"]
    shifted = replace(
        product,
        id=type(product.id)("product_runtime_uuid_changed"),
        created_at=datetime(2040, 1, 1, tzinfo=timezone.utc),
    )
    assert product_spec_hash(product) == product_spec_hash(shifted)

    recipe = paired["stored"].recipe
    shifted_recipe = replace(
        recipe,
        id=type(recipe.id)("recipe_runtime_uuid_changed"),
        created_at=datetime(2040, 1, 1, tzinfo=timezone.utc),
        last_revalidated_at=datetime(2040, 1, 2, tzinfo=timezone.utc),
    )
    assert canonical_recipe_semantic_hash(recipe) == canonical_recipe_semantic_hash(shifted_recipe)
    first_manifest = build_preregistered_manifest(
        product=product, product_class=paired["product_class"], stored=paired["stored"],
        source_revision="semantic-identity-revision",
    )
    second_manifest = build_preregistered_manifest(
        product=shifted, product_class=paired["product_class"],
        stored=replace(paired["stored"], recipe=shifted_recipe),
        source_revision="semantic-identity-revision",
    )
    assert first_manifest.id == second_manifest.id


def test_x_adaptation_and_warm_dag_identity_use_semantic_recipe_hash(paired):
    stored = paired["stored"]
    product = paired["product"]
    product_class = paired["product_class"]
    match = match_product_class(product, product_class)
    eligibility = evaluate_warm_start_eligibility(
        stored=stored,
        match=match,
        available_station_ids=tuple(str(item.id) for item in controlled_warm_stations()),
        available_worker_ids=tuple(str(worker) for values in stored.recipe.worker_preferences.values() for worker in values),
        available_context_strategies=tuple(stored.recipe.context_budgets),
        available_gate_policies=(str(stored.recipe.gate_policies["policy_id"]),),
    )
    semantic_hash = canonical_recipe_semantic_hash(stored.recipe)
    first = compile_warm_start_dag(
        product=product, product_class=product_class, stored=stored, match=match,
        eligibility=eligibility, recipe_identity_hash=semantic_hash,
        product_identity_hash=product_spec_hash(product),
    )
    shifted_product = replace(
        product,
        id=type(product.id)("product_runtime_uuid_changed_for_dag"),
        created_at=datetime(2041, 1, 1, tzinfo=timezone.utc),
    )
    second = compile_warm_start_dag(
        product=shifted_product,
        product_class=product_class, stored=stored, match=match,
        eligibility=eligibility, recipe_identity_hash=semantic_hash,
        product_identity_hash=product_spec_hash(shifted_product),
    )
    assert first.adaptation.canonical_hash == second.adaptation.canonical_hash
    assert first.dag.id == second.dag.id


def test_y_context_attempt_gates_and_accounting_are_durable(paired):
    cold, warm, pair = _views(paired)
    for view in (cold, warm):
        provider_attempts = [item for item in view["attempts"] if item["provider_attempt"]]
        assert len(provider_attempts) == 1
        assert set(view["gate_evidence"]) == {
            "structured_output_schema", "patch_output_contract", "allowed_file_change_scope",
            "python_syntax_compile", "public_test_execution", "private_test_execution",
            "handoff_artifact_validation", "integration_verification", "independent_final_reviewer",
        }
        assert all(item["status"] == "PASS" for item in view["gate_evidence"].values())
        assert view["context_compilation"]["strategy_id"] == "selected_v0"
        assert view["context_compilation"]["exact_interface_present"] is True
        assert view["metrics"]["context"]["compiled_package_bytes"] > 0
        assert view["metrics"]["context"]["rendered_request_bytes"] > 0
        assert view["metrics"]["orchestration"]["actual_worker_attempts"] == 3
        assert view["metrics"]["orchestration"]["synthetic_dry_attempts"] == 1
        assert view["metrics"]["orchestration"]["provider_attempts"] == 1
    assert pair["classification"] == "VALID_PAIRED_SUCCESS"


def test_z_warm_policy_controlled_payloads_and_manifest_mode(paired):
    cold, warm, _ = _views(paired)
    assert warm["lineage"]["execution_policy"]["source"] == "pinned_recipe"
    assert warm["lineage"]["execution_policy"]["worker_order"][0] == WORKER_UNIVERSE[1]
    assert cold["lineage"]["execution_policy"]["source"] == "cold_start_policy"
    assert cold["controlled_payload_hashes"] == warm["controlled_payload_hashes"]
    assert set(cold["controlled_payloads"]) == {
        "product_spec", "product_class", "station_contracts", "gate_policy",
        "worker_profiles", "environment", "output_limits",
    }
    environment = paired["outcome"].preregistration.manifest.environment
    assert environment.metadata["provider_mode"] == "dry_fake"
    assert environment.metadata["dry_provider_disabled"] is True
    assert paired["outcome"].paired_result["preregistration_proof"]["before_both_conditions"] is True


def test_za_failed_provider_slot_persists_attempt_failure_and_not_run_gates(paired, tmp_path, monkeypatch):
    import redi_lab.paired_experiment as module

    def simulated_failed_live_call(self, *, context_package, task_id, station_id, workspace_root):
        self.attempts += 1
        self.provider_calls += 1
        self.network_calls += 1
        return {
            "patches": [],
            "provider": {"calls": 1, "input_tokens": 41, "output_tokens": 7, "cached_input_tokens": 3, "provider": "openai", "model": module.LIVE_MODEL},
            "context": {"compiled_package_bytes": context_package.counts["bytes"], "rendered_request_bytes": 999, "heuristic_tokens": context_package.counts["estimated_tokens"]},
            "structured_output": {"validated": False, "schema": "RediBuilderOutput", "safe_error": "STRUCTURED_OUTPUT_SCHEMA_FAILED simulated"},
            "synthetic_fake": {"adapter_calls": 0, "input_tokens": 0, "output_tokens": 0},
        }

    monkeypatch.setenv("REDI_LIVE_PROVIDER", "1")
    monkeypatch.setattr(module._OneShotProviderBoundary, "run", simulated_failed_live_call)
    outcome = execute_paired_experiment(
        output_root=tmp_path / "failed-pair",
        product=paired["product"], product_class=paired["product_class"],
        store=paired["store"], recipe_id=str(paired["stored"].recipe.id),
        recipe_version=paired["stored"].recipe.version,
        source_revision="redi08b-failed-attempt-regression", live=True,
    )
    cold = replay_condition(db_path=outcome.cold.db_path, factory_run_id=outcome.cold.factory_run_id)
    assert cold["verified"] is False
    assert len(cold["attempts"]) == 1
    assert cold["attempts"][0]["provider_attempt"] is True
    assert cold["metrics"]["orchestration"]["provider_attempts"] == 1
    assert cold["metrics"]["provider_usage"]["calls"] == 1
    assert cold["gate_evidence"]["structured_output_schema"]["status"] == "FAIL"
    assert cold["gate_evidence"]["python_syntax_compile"]["status"] == "NOT_RUN"
    assert cold["gate_evidence"]["public_test_execution"]["status"] == "NOT_RUN"
    assert cold["failures"][0]["failure_class"] == "ordinary_worker_implementation_failure"
    assert cold["failures"][0]["transfer_failure"] is False


def test_zb_live_manifest_metadata_is_truthful_without_calling_provider(paired):
    manifest = build_preregistered_manifest(
        product=paired["product"], product_class=paired["product_class"],
        stored=paired["stored"], source_revision="redi08b-live-manifest-test", live=True,
    )
    assert manifest.environment.metadata["provider_mode"] == "live"
    assert manifest.environment.metadata["dry_provider_disabled"] is False
