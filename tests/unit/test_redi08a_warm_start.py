from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from redi_lab.cold_start import run_controlled_cold_start
from redi_lab.domain import (
    ProductRecipe,
    RecipeRung,
    StationId,
    ValidityCondition,
    WorkerId,
)
from redi_lab.recipe_store import (
    ConfidenceMetadata,
    EXPERIMENTAL_PROMOTION_POLICY_ID,
    EXPERIMENTAL_PROMOTION_POLICY_VERSION,
    RecipeApproval,
    RecipeApprovalError,
    RecipeConflictError,
    RecipeStore,
    StablePromotionBlocked,
)
from redi_lab.warm_start import (
    ACCOUNTING_SEMANTICS,
    MatchDecision,
    WARM_START_KNOWN_PRODUCT,
    build_ab_experiment_manifest,
    canonical_warm_start_replay_view,
    compile_warm_start_dag,
    controlled_warm_stations,
    evaluate_warm_start_eligibility,
    import_candidate_from_ledger,
    incompatible_product_fixture,
    inventory_product_class,
    lookup_incompatible_product,
    match_product_class,
    original_product_fixture,
    replay_warm_start,
    resolve_warm_start_provenance,
    run_controlled_warm_start,
    sibling_product_fixture,
)


def _confidence(stored) -> ConfidenceMetadata:
    return ConfidenceMetadata(
        tier="LOW",
        evidence_state="SINGLE_PRODUCT_EVIDENCE",
        source_verified_runs_count=1,
        source_failed_runs_count=0,
        source_failed_attempts_count=1,
        distinct_sibling_products_tested=0,
        last_verified_at=None,
        environment_coverage=("controlled_python_offline",),
        model_coverage=("observed_source_only",),
        station_evidence_coverage=tuple(str(v) for v in stored.recipe.required_stations),
        transfer_evidence_count=0,
    )


def _approval(stored) -> RecipeApproval:
    return RecipeApproval(
        policy_id=EXPERIMENTAL_PROMOTION_POLICY_ID,
        policy_version=EXPERIMENTAL_PROMOTION_POLICY_VERSION,
        approved_by="REDI-08A controlled audit",
        approval_reference="redi08a-predeclared-offline-trial",
        source_evidence_ids=(
            stored.provenance["source_candidate_evidence_id"],
            stored.provenance["source_product_verified_event_id"],
        ),
        approved_at=datetime.now(timezone.utc),
    )


def _fake_preferences():
    return {
        StationId("station_cold_start_implementation_v0"): (
            WorkerId("worker_warm_fake_normalize_v0"),
            WorkerId("worker_warm_fake_reorder_v0"),
        ),
        StationId("station_cold_start_integration_review_v0"): (
            WorkerId("worker_warm_fake_independent_reviewer_v0"),
        ),
    }


@pytest.fixture(scope="module")
def foundation(tmp_path_factory):
    root = tmp_path_factory.mktemp("redi08a")
    source = run_controlled_cold_start(
        output_root=root / "cold",
        known_recipe=None,
    )
    assert source.candidate is not None
    class_id = str(source.candidate.recipe.product_class_id)
    product_class = inventory_product_class(class_id)
    store = RecipeStore(root / "recipes.sqlite")
    candidate = import_candidate_from_ledger(
        store=store,
        source_ledger=source.db_path,
        source_factory_run_id=source.factory_run_id,
        product_class=product_class,
    )
    imported_again = import_candidate_from_ledger(
        store=store,
        source_ledger=source.db_path,
        source_factory_run_id=source.factory_run_id,
        product_class=product_class,
    )
    experimental = store.enable_experimental(
        recipe_id=str(candidate.recipe.id),
        source_version=1,
        product_class=product_class,
        approval=_approval(candidate),
        confidence=_confidence(candidate),
        validity_conditions=(
            ValidityCondition(
                description="controlled offline sibling experiment only",
                scope="environment",
                checked_via="policy",
            ),
        ),
        worker_preferences=_fake_preferences(),
        worker_fallbacks=_fake_preferences(),
        metadata={
            "worker_semantics": {
                "observed_source": "evidence_only",
                "preferred_under_experimental_recipe": "deterministic_fake_workers",
                "globally_best_claimed": False,
            }
        },
    )
    sibling = sibling_product_fixture(class_id)
    warm = run_controlled_warm_start(
        output_root=root / "warm",
        store=store,
        recipe_id=str(experimental.recipe.id),
        recipe_version=experimental.recipe.version,
        product=sibling,
        product_class=product_class,
    )
    yield {
        "root": root,
        "source": source,
        "class": product_class,
        "store": store,
        "candidate": candidate,
        "imported_again": imported_again,
        "experimental": experimental,
        "sibling": sibling,
        "warm": warm,
    }
    store.close()


def test_a_candidate_import_is_idempotent(foundation):
    first = foundation["candidate"]
    second = foundation["imported_again"]
    assert first.canonical_hash == second.canonical_hash
    assert first.recipe.version == second.recipe.version == 1
    assert len(foundation["store"].list_versions(str(first.recipe.id))) == 2


def test_b_conflicting_same_recipe_identity_fails_closed(foundation):
    stored = foundation["candidate"]
    conflicting = replace(
        stored.recipe,
        metadata=dict(stored.recipe.metadata) | {"conflicting": True},
    )
    with pytest.raises(RecipeConflictError, match="different content"):
        foundation["store"].register_version(
            recipe=conflicting,
            product_class=foundation["class"],
            confidence=stored.confidence,
            provenance=stored.provenance,
            parent_version=None,
            rollback_target_version=None,
            revalidation_state=stored.revalidation_state,
            registration_metadata={},
            event_type="CONFLICT",
            policy_id="test",
            policy_version=1,
        )


def test_c_recipe_versions_are_immutable(foundation):
    store = foundation["store"]
    recipe_id = str(foundation["candidate"].recipe.id)
    before = store.read(recipe_id, 1).canonical_hash
    assert store.read(recipe_id, 2).canonical_hash != before
    assert store.read(recipe_id, 1).canonical_hash == before


def test_d_parent_version_lineage_is_preserved(foundation):
    experimental = foundation["experimental"]
    assert experimental.parent_version == 1
    assert experimental.rollback_target_version == 1
    assert experimental.provenance["parent_canonical_hash"] == foundation["candidate"].canonical_hash


def test_e_explicit_experimental_eligibility_is_required(foundation):
    candidate = foundation["candidate"]
    match = match_product_class(foundation["sibling"], foundation["class"])
    stations = [str(v.id) for v in controlled_warm_stations()]
    decision = evaluate_warm_start_eligibility(
        stored=candidate,
        match=match,
        available_station_ids=stations,
        available_worker_ids=tuple(
            str(worker)
            for workers in candidate.recipe.worker_preferences.values()
            for worker in workers
        ),
        available_context_strategies=("selected_v0",),
        available_gate_policies=("cold_start_strict_gates_v0",),
    )
    assert not decision.eligible
    assert "lifecycle" in " ".join(decision.reasons)


def test_f_candidate_cannot_be_selected_silently(foundation):
    candidate = foundation["candidate"]
    with pytest.raises(RecipeApprovalError, match="candidate"):
        foundation["store"].select_version(
            recipe_id=str(candidate.recipe.id),
            version=1,
            policy_id="test",
            policy_version=1,
            evidence={},
            reason="must fail",
        )


def test_g_original_and_materially_different_sibling_match_same_class(foundation):
    product_class = foundation["class"]
    original = original_product_fixture(str(product_class.id))
    sibling = foundation["sibling"]
    assert match_product_class(original, product_class).decision == MatchDecision.COMPATIBLE
    assert match_product_class(sibling, product_class).decision == MatchDecision.COMPATIBLE
    assert original.requested_outcome != sibling.requested_outcome
    assert original.metadata["cold_start_components"] != sibling.metadata["warm_start_components"]


def test_h_incompatible_product_is_rejected(foundation):
    lookup = lookup_incompatible_product(
        product=incompatible_product_fixture(),
        product_class=foundation["class"],
        stored=foundation["experimental"],
        store=foundation["store"],
    )
    assert lookup["match"]["decision"] == MatchDecision.INCOMPATIBLE.value
    assert lookup["recipe_selected"] is False
    assert lookup["next_mode"] == "COLD_START_REQUIRED"
    assert any(
        event["event_type"] == "LOOKUP_BLOCKED"
        and "incompatibility" in event["reason"]
        for event in foundation["store"].events(str(foundation["experimental"].recipe.id))
    )


def test_i_product_variable_fields_do_not_define_class_identity(foundation):
    match = match_product_class(foundation["sibling"], foundation["class"])
    assert match.decision == MatchDecision.COMPATIBLE
    assert match.product_variable_hash is not None
    assert match.evidence["whole_product_spec_hash_compared"] is False


def test_j_warm_start_mode_is_explicitly_persisted(foundation):
    warm = foundation["warm"]
    replay = replay_warm_start(db_path=warm.db_path, factory_run_id=warm.factory_run_id)
    assert replay["mode"] == WARM_START_KNOWN_PRODUCT
    assert replay["selected_recipe"]["recipe_version"] == 2


def test_k_sibling_dag_is_recipe_plus_adaptation_not_cold_planner(foundation):
    plan = foundation["warm"].plan
    assert plan.dag.metadata["recipe_topology_consulted"] is True
    assert plan.dag.metadata["cold_start_planner_used"] is False
    assert plan.dag.metadata["source_recipe_hash"] == foundation["experimental"].canonical_hash
    assert plan.adaptation.topology_preserved is True


def test_l_recipe_does_not_overwrite_sibling_requirements(foundation):
    plan = foundation["warm"].plan
    normalize = plan.tasks_by_node["implement_normalize"]
    reorder = plan.tasks_by_node["implement_reorder"]
    assert "vendor" in normalize.goal.lower()
    assert "safety" in reorder.goal.lower()
    assert "bulk_inventory" in plan.adaptation.changes["sibling_specific_source_paths"][0]


def test_m_adaptation_cannot_weaken_gates(foundation):
    stored = foundation["experimental"]
    weakened = replace(
        stored,
        recipe=replace(
            stored.recipe,
            gate_policies={"policy_id": "weakened", "policy_version": 1},
        ),
    )
    match = match_product_class(foundation["sibling"], foundation["class"])
    eligible = replace(foundation["warm"].eligibility, eligible=True, reasons=("forced test",))
    with pytest.raises(Exception, match="cannot weaken"):
        compile_warm_start_dag(
            product=foundation["sibling"],
            product_class=foundation["class"],
            stored=weakened,
            match=match,
            eligibility=eligible,
        )


def test_n_observed_worker_is_not_a_global_best_claim(foundation):
    metadata = foundation["experimental"].recipe.metadata["worker_semantics"]
    assert metadata["observed_source"] == "evidence_only"
    assert metadata["globally_best_claimed"] is False
    replay = replay_warm_start(
        db_path=foundation["warm"].db_path,
        factory_run_id=foundation["warm"].factory_run_id,
    )
    assert all(item["metadata"].get("globally_best_claimed") is False for item in replay["worker_attempts"] if item["node_id"].startswith("implement_"))


def test_o_confidence_is_descriptive_not_probability(foundation):
    confidence = foundation["experimental"].confidence.to_dict()
    assert confidence["tier"] == "LOW"
    assert confidence["evidence_state"] == "SINGLE_PRODUCT_EVIDENCE"
    assert confidence["probability"] is None
    assert confidence["transfer_evidence_count"] == 0


def test_p_rollback_selects_prior_immutable_version(foundation):
    store = foundation["store"]
    v2 = foundation["experimental"]
    recipe_id = str(v2.recipe.id)
    v3_recipe = replace(
        v2.recipe,
        version=3,
        worker_preferences={
            station: tuple(reversed(tuple(workers)))
            for station, workers in v2.recipe.worker_preferences.items()
        },
        metadata=dict(v2.recipe.metadata) | {"trial_revision": 2},
        created_at=datetime.now(timezone.utc),
    )
    v3 = store.register_version(
        recipe=v3_recipe,
        product_class=foundation["class"],
        confidence=v2.confidence,
        provenance=dict(v2.provenance) | {"parent_recipe_version": 2},
        parent_version=2,
        rollback_target_version=2,
        revalidation_state="VALID_FOR_CONTROLLED_EXPERIMENT",
        registration_metadata={"trial_revision": 2},
        event_type="EXPERIMENTAL_VERSION_CREATED",
        policy_id="redi08a_versioning_v0",
        policy_version=1,
    )
    store.select_version(
        recipe_id=recipe_id,
        version=3,
        policy_id="redi08a_versioning_v0",
        policy_version=1,
        evidence={"version": 3},
        reason="exercise rollback",
    )
    rolled = store.rollback(
        recipe_id=recipe_id,
        target_version=2,
        policy_id="redi08a_governed_rollback_v0",
        policy_version=1,
        approved_by="offline audit",
        approval_reference="rollback-test",
        reason="controlled rollback test",
    )
    assert rolled.recipe.version == 2
    assert store.current_selected_version(recipe_id) == 2
    assert store.read(recipe_id, 3).canonical_hash == v3.canonical_hash
    assert any(event["event_type"] == "ROLLBACK_SELECTED" for event in store.events(recipe_id))


def test_q_no_stable_promotion_path_exists(foundation):
    with pytest.raises(StablePromotionBlocked):
        foundation["store"].promote_to_validated()
    replay = replay_warm_start(
        db_path=foundation["warm"].db_path,
        factory_run_id=foundation["warm"].factory_run_id,
    )
    assert replay["recipe_promoted_event"] is False
    assert foundation["experimental"].lifecycle_state == RecipeRung.SHADOW


def test_r_provenance_references_resolve(foundation):
    warm = foundation["warm"]
    checks = resolve_warm_start_provenance(
        warm_db_path=warm.db_path,
        warm_factory_run_id=warm.factory_run_id,
        stored=foundation["experimental"],
        source_ledger=foundation["source"].db_path,
    )
    assert checks and all(checks.values())


def test_s_replay_twice_is_identical_and_read_only(foundation):
    warm = foundation["warm"]
    before = warm.db_path.read_bytes()
    stat = warm.db_path.stat()
    first = canonical_warm_start_replay_view(
        replay_warm_start(db_path=warm.db_path, factory_run_id=warm.factory_run_id)
    )
    second = canonical_warm_start_replay_view(
        replay_warm_start(db_path=warm.db_path, factory_run_id=warm.factory_run_id)
    )
    assert first == second
    assert warm.db_path.read_bytes() == before
    assert warm.db_path.stat().st_mtime_ns == stat.st_mtime_ns
    assert first["replay_dependencies"] == {
        "recipe_store_consulted": False,
        "current_matching_config_consulted": False,
        "current_adaptation_config_consulted": False,
        "workers_executed": 0,
        "provider_calls": 0,
        "network_calls": 0,
    }


def test_t_store_and_current_config_drift_do_not_rewrite_replay(foundation, monkeypatch):
    import redi_lab.warm_start as module

    warm = foundation["warm"]
    before = canonical_warm_start_replay_view(
        replay_warm_start(db_path=warm.db_path, factory_run_id=warm.factory_run_id)
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("historical replay consulted current config")

    monkeypatch.setattr(module, "WARM_START_MATCH_POLICY_ID", "future_match_v99")
    monkeypatch.setattr(module, "WARM_START_ADAPTATION_POLICY_ID", "future_adapt_v99")
    monkeypatch.setattr(module, "match_product_class", forbidden)
    monkeypatch.setattr(module, "compile_warm_start_dag", forbidden)
    after = canonical_warm_start_replay_view(
        replay_warm_start(db_path=warm.db_path, factory_run_id=warm.factory_run_id)
    )
    assert before == after
    assert after["selected_recipe"]["recipe_version"] == 2


def test_u_ab_harness_conditions_are_comparable(foundation):
    manifest = build_ab_experiment_manifest(
        sibling_product=foundation["sibling"],
        stored=foundation["experimental"],
        source_revision="test-source-revision",
    )
    arm_a = manifest.arms["A_cold_start"]
    arm_b = manifest.arms["B_warm_start"]
    assert arm_a["controlled_variables"] == arm_b["controlled_variables"]
    assert manifest.validity_controls["comparison_executed"] is False
    assert manifest.validity_controls["advantage_claimed"] is False
    assert manifest.measurement_plan["composite_score"] is False
    assert len(manifest.measurement_plan["metrics"]) == 12


def test_v_zero_provider_network_and_separate_accounting(foundation):
    warm = foundation["warm"]
    assert warm.verified is True
    assert warm.provider_calls == warm.network_calls == 0
    replay = replay_warm_start(db_path=warm.db_path, factory_run_id=warm.factory_run_id)
    assert replay["accounting"]["provider"]["calls"] == 0
    assert replay["accounting"]["local_compute"]["synthetic_token_counter_used"] is False
    assert ACCOUNTING_SEMANTICS["provider_token_totals"].startswith("provider-billed")
