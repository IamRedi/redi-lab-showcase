"""Offline REDI-08A validation pack (recipe store + warm-start foundation)."""

from __future__ import annotations

import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from redi_lab.cold_start import replay_cold_start, run_controlled_cold_start
from redi_lab.domain import RecipeRung, StationId, ValidityCondition, WorkerId
from redi_lab.recipe_store import (
    ConfidenceMetadata,
    EXPERIMENTAL_PROMOTION_POLICY_ID,
    EXPERIMENTAL_PROMOTION_POLICY_VERSION,
    RecipeApproval,
    RecipeApprovalError,
    RecipeConflictError,
    RecipeStore,
    StablePromotionBlocked,
    canonical_recipe_semantic_hash,
)
from redi_lab.holdout_transfer import (
    ACCEPTED_PRODUCT_CLASS_ID,
    ACCEPTANCE_HARNESS_REHEARSAL,
    ALTERNATE_GENUINE_WORKER_ID,
    FROZEN_BUILDER_RENDERED_HASH,
    FROZEN_FIXTURE_TREE_HASH,
    GENUINE_HOLDOUT_FAILURE,
    PREDECLARED_HOLDOUT_PRODUCT_SHA256,
    WORKER_CLASS_GENUINE,
    WORKER_CLASS_ORACLE,
    audit_worker_registry,
    classify_registry_worker,
    create_alternate_worker_v3,
    load_genuine_holdout_failure_record,
    HISTORICAL_RECIPE_ID,
    HISTORICAL_RECIPE_SEMANTIC_HASH,
    HISTORICAL_RECIPE_VERSION,
    OWNER_PAIRED_002_FAILED_WORKER,
    OWNER_PAIRED_002_PAIR_ID,
    PLANNED_LIVE_PROVIDER_CALLS,
    WORKER_SELECTION_POLICY_ID,
    audit_holdout_transfer,
    execute_holdout_warm_transfer,
    holdout_difference_summary,
    load_owner_paired_002_record,
    prior_sibling_registry,
    prepare_holdout_recipe,
    recipe_v2_fallback_audit,
    replay_holdout,
    replay_holdout_result,
    select_holdout_sibling,
    select_holdout_worker,
)
from redi_lab.paired_experiment import (
    CONDITION_COLD,
    CONDITION_ORDER,
    CONDITION_WARM,
    MAX_TOTAL_LIVE_CALLS,
    TransferFailureClass,
    WORKER_UNIVERSE,
    assert_retry_blocked,
    classify_transfer_failure,
    execute_paired_experiment,
    prepare_shadow_recipe,
    replay_condition,
    replay_pair,
)
from redi_lab.warm_start import (
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


ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_LEDGER = ROOT / "artifacts" / "redi07b" / "redi07b-live-3962bb5a22bc" / "evidence.sqlite"
HISTORICAL_RUN = "factory_run_7f21dd99f6b94664be1434e4f6803393"
HISTORICAL_CANDIDATE_HASH = "ac817b3775b3d35f2caea0ca2c2088b02d02a9fa9def0ab8cc7e78a18cb73925"


def _preferences():
    return {
        StationId("station_cold_start_implementation_v0"): (
            WorkerId("worker_warm_fake_normalize_v0"),
            WorkerId("worker_warm_fake_reorder_v0"),
        ),
        StationId("station_cold_start_integration_review_v0"): (
            WorkerId("worker_warm_fake_independent_reviewer_v0"),
        ),
    }


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    build = ROOT / "build"
    build.mkdir(exist_ok=True)
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="redi08a_validation_", dir=build) as temp:
        work = Path(temp)
        if HISTORICAL_LEDGER.exists():
            source_ledger = HISTORICAL_LEDGER
            source_run = HISTORICAL_RUN
            source_mode = "accepted_redi07_live"
        else:
            cold = run_controlled_cold_start(output_root=work / "cold", known_recipe=None)
            source_ledger = cold.db_path
            source_run = cold.factory_run_id
            source_mode = "controlled_offline_fallback"
        replay07 = replay_cold_start(db_path=source_ledger, factory_run_id=source_run)
        candidate_view = replay07["recipe_candidate"]
        if candidate_view is None:
            raise AssertionError("source candidate missing")
        import json

        candidate_payload = json.loads(candidate_view["canonical_recipe_json"])
        class_id = str(candidate_payload["product_class_id"])
        product_class = inventory_product_class(class_id)
        store = RecipeStore(work / "recipes.sqlite")
        candidate = import_candidate_from_ledger(
            store=store,
            source_ledger=source_ledger,
            source_factory_run_id=source_run,
            product_class=product_class,
        )
        again = import_candidate_from_ledger(
            store=store,
            source_ledger=source_ledger,
            source_factory_run_id=source_run,
            product_class=product_class,
        )
        approval = RecipeApproval(
            policy_id=EXPERIMENTAL_PROMOTION_POLICY_ID,
            policy_version=EXPERIMENTAL_PROMOTION_POLICY_VERSION,
            approved_by="REDI-08A validation audit",
            approval_reference="redi08a-offline-validation",
            source_evidence_ids=(
                candidate.provenance["source_candidate_evidence_id"],
                candidate.provenance["source_product_verified_event_id"],
            ),
            approved_at=datetime.now(timezone.utc),
        )
        confidence = ConfidenceMetadata(
            tier="LOW",
            evidence_state="SINGLE_PRODUCT_EVIDENCE",
            source_verified_runs_count=1,
            source_failed_runs_count=0,
            source_failed_attempts_count=1,
            distinct_sibling_products_tested=0,
            last_verified_at=None,
            environment_coverage=("controlled_python_offline",),
            model_coverage=("observed_source_only",),
            station_evidence_coverage=tuple(str(v) for v in candidate.recipe.required_stations),
            transfer_evidence_count=0,
        )
        experimental = store.enable_experimental(
            recipe_id=str(candidate.recipe.id),
            source_version=1,
            product_class=product_class,
            approval=approval,
            confidence=confidence,
            validity_conditions=(
                ValidityCondition(
                    description="controlled offline sibling experiment only",
                    scope="environment",
                    checked_via="policy",
                ),
            ),
            worker_preferences=_preferences(),
            worker_fallbacks=_preferences(),
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
            output_root=work / "warm",
            store=store,
            recipe_id=str(experimental.recipe.id),
            recipe_version=2,
            product=sibling,
            product_class=product_class,
        )
        replay_one = canonical_warm_start_replay_view(
            replay_warm_start(db_path=warm.db_path, factory_run_id=warm.factory_run_id)
        )
        replay_two = canonical_warm_start_replay_view(
            replay_warm_start(db_path=warm.db_path, factory_run_id=warm.factory_run_id)
        )

        def conflict_check() -> None:
            try:
                store.register_version(
                    recipe=replace(candidate.recipe, metadata=dict(candidate.recipe.metadata) | {"conflict": True}),
                    product_class=product_class,
                    confidence=candidate.confidence,
                    provenance=candidate.provenance,
                    parent_version=None,
                    rollback_target_version=None,
                    revalidation_state=candidate.revalidation_state,
                    registration_metadata={},
                    event_type="CONFLICT",
                    policy_id="test",
                    policy_version=1,
                )
            except RecipeConflictError:
                return
            raise AssertionError("conflicting identity was accepted")

        def candidate_block_check() -> None:
            try:
                store.select_version(
                    recipe_id=str(candidate.recipe.id), version=1,
                    policy_id="test", policy_version=1, evidence={}, reason="must fail",
                )
            except RecipeApprovalError:
                return
            raise AssertionError("candidate silently selected")

        def gate_weakening_check() -> None:
            weakened = replace(
                experimental,
                recipe=replace(
                    experimental.recipe,
                    gate_policies={"policy_id": "weakened", "policy_version": 1},
                ),
            )
            try:
                compile_warm_start_dag(
                    product=sibling,
                    product_class=product_class,
                    stored=weakened,
                    match=warm.match,
                    eligibility=warm.eligibility,
                )
            except Exception:
                return
            raise AssertionError("gate weakening was accepted")

        def rollback_check() -> None:
            v3 = store.register_version(
                recipe=replace(
                    experimental.recipe,
                    version=3,
                    worker_preferences={
                        station: tuple(reversed(tuple(workers)))
                        for station, workers in experimental.recipe.worker_preferences.items()
                    },
                    metadata=dict(experimental.recipe.metadata) | {"trial_revision": 2},
                    created_at=datetime.now(timezone.utc),
                ),
                product_class=product_class,
                confidence=experimental.confidence,
                provenance=dict(experimental.provenance) | {"parent_recipe_version": 2},
                parent_version=2,
                rollback_target_version=2,
                revalidation_state="VALID_FOR_CONTROLLED_EXPERIMENT",
                registration_metadata={"trial_revision": 2},
                event_type="EXPERIMENTAL_VERSION_CREATED",
                policy_id="redi08a_versioning_v0",
                policy_version=1,
            )
            store.select_version(
                recipe_id=str(v3.recipe.id), version=3,
                policy_id="redi08a_versioning_v0", policy_version=1,
                evidence={"version": 3}, reason="rollback validation",
            )
            store.rollback(
                recipe_id=str(v3.recipe.id), target_version=2,
                policy_id="redi08a_governed_rollback_v0", policy_version=1,
                approved_by="validation audit", approval_reference="rollback-validation",
                reason="governed rollback validation",
            )
            _check(store.current_selected_version(str(v3.recipe.id)) == 2, "rollback target")
            _check(store.read(str(v3.recipe.id), 3).canonical_hash == v3.canonical_hash, "v3 retained")

        def stable_block_check() -> None:
            try:
                store.promote_to_validated()
            except StablePromotionBlocked:
                return
            raise AssertionError("stable promotion path unexpectedly available")

        def provenance_check() -> None:
            checks = resolve_warm_start_provenance(
                warm_db_path=warm.db_path,
                warm_factory_run_id=warm.factory_run_id,
                stored=experimental,
                source_ledger=source_ledger,
            )
            _check(bool(checks) and all(checks.values()), "provenance chain")

        ab = build_ab_experiment_manifest(
            sibling_product=sibling,
            stored=experimental,
            source_revision="redi08a-validation-source",
        )
        incompatible = lookup_incompatible_product(
            product=incompatible_product_fixture(),
            product_class=product_class,
            stored=experimental,
            store=store,
        )
        candidate_eligibility = evaluate_warm_start_eligibility(
            stored=candidate,
            match=match_product_class(sibling, product_class),
            available_station_ids=tuple(str(v.id) for v in controlled_warm_stations()),
            available_worker_ids=tuple(
                str(worker)
                for workers in candidate.recipe.worker_preferences.values()
                for worker in workers
            ),
            available_context_strategies=("selected_v0",),
            available_gate_policies=("cold_start_strict_gates_v0",),
        )
        checks: list[tuple[str, Callable[[], None]]] = [
            ("A candidate import idempotent", lambda: _check(candidate.canonical_hash == again.canonical_hash, "idempotence")),
            ("B conflicting identity fails closed", conflict_check),
            ("C immutable versions", lambda: _check(store.read(str(candidate.recipe.id), 1).canonical_hash == candidate.canonical_hash, "v1 changed")),
            ("D lineage preserved", lambda: _check(experimental.parent_version == experimental.rollback_target_version == 1, "lineage")),
            ("E experimental eligibility explicit", lambda: _check(experimental.lifecycle_state == RecipeRung.SHADOW, "not shadow")),
            ("F candidate cannot warm-start", lambda: (_check(not candidate_eligibility.eligible, "candidate eligible"), candidate_block_check())),
            ("G sibling compatible", lambda: _check(match_product_class(sibling, product_class).decision == MatchDecision.COMPATIBLE, "sibling match")),
            ("H incompatible rejected", lambda: _check(incompatible["next_mode"] == "COLD_START_REQUIRED" and not incompatible["recipe_selected"], "incompatible reuse")),
            ("I variable fields excluded", lambda: _check(match_product_class(original_product_fixture(class_id), product_class).decision == MatchDecision.COMPATIBLE, "original class")),
            ("J warm mode persisted", lambda: _check(replay_one["mode"] == WARM_START_KNOWN_PRODUCT, "mode")),
            ("K recipe plus adaptation DAG", lambda: _check(warm.plan.dag.metadata["cold_start_planner_used"] is False, "cold planner")),
            ("L sibling requirements preserved", lambda: _check("vendor" in warm.plan.tasks_by_node["implement_normalize"].goal.lower(), "sibling goal")),
            ("M gates cannot weaken", gate_weakening_check),
            ("N no global-best claim", lambda: _check(experimental.recipe.metadata["worker_semantics"]["globally_best_claimed"] is False, "best claim")),
            ("O descriptive confidence", lambda: _check(experimental.confidence.to_dict()["probability"] is None, "fake probability")),
            ("P rollback preserves history", rollback_check),
            ("Q no silent promotion", stable_block_check),
            ("R provenance resolves", provenance_check),
            ("S replay x2", lambda: _check(replay_one == replay_two, "replay differs")),
            ("T store/config drift safe", lambda: _check(replay_one["selected_recipe"]["recipe_version"] == 2, "historical selection")),
            ("U A/B comparable", lambda: _check(ab.arms["A_cold_start"]["controlled_variables"] == ab.arms["B_warm_start"]["controlled_variables"] and not ab.validity_controls["comparison_executed"], "A/B controls")),
            ("V zero provider/network", lambda: _check(warm.provider_calls == warm.network_calls == 0 and warm.verified, "external call")),
        ]
        print(f"REDI-08A source_mode={source_mode}")
        print(f"source_factory_run_id={source_run}")
        print(f"recipe_id={candidate.recipe.id} candidate_hash={candidate.canonical_hash}")
        if source_mode == "accepted_redi07_live":
            _check(candidate.canonical_hash == HISTORICAL_CANDIDATE_HASH, "accepted candidate hash")
        for index, (name, check) in enumerate(checks, 1):
            try:
                check()
            except Exception as exc:  # noqa: BLE001 - aggregate validation report
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
                print(f"  FAIL {index:02d} {name}: {type(exc).__name__}: {exc}")
            else:
                print(f"  PASS {index:02d} {name}")
        pair_store = RecipeStore(work / "paired-recipes.sqlite")
        paired_recipe = prepare_shadow_recipe(
            store=pair_store,
            source_ledger=source_ledger,
            source_factory_run_id=source_run,
            product_class=product_class,
            approval_reference="redi08b-validation",
        )
        pair_outcome = execute_paired_experiment(
            output_root=work / "paired",
            product=sibling,
            product_class=product_class,
            store=pair_store,
            recipe_id=str(paired_recipe.recipe.id),
            recipe_version=paired_recipe.recipe.version,
            source_revision="redi08b-validation-source",
            live=False,
        )
        cold_pair = replay_condition(
            db_path=pair_outcome.cold.db_path,
            factory_run_id=pair_outcome.cold.factory_run_id,
        )
        warm_pair = replay_condition(
            db_path=pair_outcome.warm.db_path,
            factory_run_id=pair_outcome.warm.factory_run_id,
        )
        pair_view = replay_pair(pair_outcome.paired_result_path)
        manifest = pair_outcome.preregistration.manifest
        pair_checks: list[tuple[str, Callable[[], None]]] = [
            ("A manifest precedes conditions", lambda: _check(pair_view["preregistration_proof"]["before_both_conditions"], "manifest order")),
            ("B exact sibling shared", lambda: _check(cold_pair["product_spec_sha256"] == warm_pair["product_spec_sha256"], "product mismatch")),
            ("C fixture bytes identical", lambda: _check(cold_pair["fixture_hash"] == warm_pair["fixture_hash"], "fixture mismatch")),
            ("D same environment/gates", lambda: _check(manifest.arms[CONDITION_COLD]["controlled_variables"]["mandatory_gates"] == manifest.arms[CONDITION_WARM]["controlled_variables"]["mandatory_gates"], "gate controls")),
            ("E same worker universe", lambda: _check(manifest.arms[CONDITION_COLD]["controlled_variables"]["worker_universe"] == manifest.arms[CONDITION_WARM]["controlled_variables"]["worker_universe"] == list(WORKER_UNIVERSE), "worker universe")),
            ("F cold uses no recipe", lambda: _check(cold_pair["recipe"] is None and not cold_pair["lineage"]["recipe_consulted"], "cold recipe leak")),
            ("G warm uses pinned recipe", lambda: _check(warm_pair["recipe"] == pair_view["recipe"], "warm pin")),
            ("H warm skips cold planner", lambda: _check(not warm_pair["lineage"]["cold_planner_used"], "warm cold-planned")),
            ("I cold invokes planner", lambda: _check(cold_pair["lineage"]["cold_planner_used"], "cold planner missing")),
            ("J workspaces isolated", lambda: _check(pair_outcome.cold.db_path.parent != pair_outcome.warm.db_path.parent, "scope collision")),
            ("K live budget <=2", lambda: _check(pair_view["planned_live_budget"]["planned_live_calls_total"] <= MAX_TOTAL_LIVE_CALLS, "live budget")),
            ("L paid retry blocked", assert_retry_blocked),
            ("M provider/local separate", lambda: _check(all(set(pair_view["metrics"][condition]["accounting"]) == {"provider_only", "all_system_local"} for condition in CONDITION_ORDER), "accounting")),
            ("N no winner field", lambda: _check("winner" not in pair_view and not pair_view["claims"]["automatic_comparative_claim_emitted"], "winner")),
            ("O transfer failures typed", lambda: _check(classify_transfer_failure("execution") == TransferFailureClass.RECIPE_EXECUTION_FAILURE, "taxonomy")),
            ("P recipe stays SHADOW", lambda: _check(experimental.lifecycle_state == RecipeRung.SHADOW and pair_view["recipe"]["lifecycle_state"] == "shadow", "recipe rung")),
            ("Q no RECIPE_PROMOTED", lambda: _check(not cold_pair["recipe_promoted"] and not warm_pair["recipe_promoted"], "promotion")),
            ("R pinned v2 despite newer v3", lambda: _check(warm_pair["recipe"]["recipe_version"] == 2 and len(store.list_versions(str(experimental.recipe.id))) >= 3, "version drift")),
            ("S condition replay x2", lambda: _check(cold_pair == replay_condition(db_path=pair_outcome.cold.db_path, factory_run_id=pair_outcome.cold.factory_run_id) and warm_pair == replay_condition(db_path=pair_outcome.warm.db_path, factory_run_id=pair_outcome.warm.factory_run_id), "condition replay")),
            ("T pair replay x2", lambda: _check(pair_view == replay_pair(pair_outcome.paired_result_path), "pair replay")),
            ("U config/store drift independent", lambda: _check(all(item["replay_dependencies"]["current_config"] is False for item in (cold_pair, warm_pair)), "config dependency")),
            ("V dry exact path zero external", lambda: _check(pair_outcome.cold.provider_calls == pair_outcome.warm.provider_calls == pair_outcome.cold.network_calls == pair_outcome.warm.network_calls == 0 and pair_outcome.cold.verified and pair_outcome.warm.verified, "external dry call")),
        ]
        print("REDI-08B preregistered paired-comparison invariants")
        for index, (name, check) in enumerate(pair_checks, 23):
            try:
                check()
            except Exception as exc:  # noqa: BLE001 - aggregate validation report
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
                print(f"  FAIL {index:02d} {name}: {type(exc).__name__}: {exc}")
            else:
                print(f"  PASS {index:02d} {name}")
        # -------------------------------------------------------------
        # REDI-08C predeclared holdout warm-transfer invariants
        # -------------------------------------------------------------
        strict_identity = source_mode == "accepted_redi07_live"
        holdout_store = RecipeStore(work / "holdout-recipes.sqlite")
        holdout_stored_v2 = prepare_holdout_recipe(
            store=holdout_store,
            source_ledger=source_ledger,
            source_factory_run_id=source_run,
            product_class=product_class,
            expected_semantic_hash=HISTORICAL_RECIPE_SEMANTIC_HASH if strict_identity else None,
        )
        holdout_record = load_owner_paired_002_record()
        genuine_failure = load_genuine_holdout_failure_record()
        holdout_stored = create_alternate_worker_v3(
            store=holdout_store,
            stored_v2=holdout_stored_v2,
            product_class=product_class,
            owner_paired_002_record=holdout_record,
            genuine_failure_record=genuine_failure,
            alternate_worker_id=ALTERNATE_GENUINE_WORKER_ID,
        )
        holdout_selection = select_holdout_sibling(
            product_class=product_class,
            prior_used_registry=prior_sibling_registry(
                product_class_id=str(product_class.id),
                owner_paired_002_record=holdout_record,
            ),
        )
        holdout_worker = select_holdout_worker(
            stored=holdout_stored,
            owner_paired_002_record=holdout_record,
            exclude_worker_ids={
                GENUINE_HOLDOUT_FAILURE["failed_worker"]: (
                    "excluded for this revalidation: already failed this exact holdout"
                )
            },
            exclude_models={
                GENUINE_HOLDOUT_FAILURE["model"]: (
                    "excluded for this revalidation: same model already failed this holdout"
                )
            },
        )
        holdout_outcome = execute_holdout_warm_transfer(
            output_root=work / "holdout",
            product_class=product_class,
            store=holdout_store,
            stored=holdout_stored,
            selection=holdout_selection,
            worker_selection=holdout_worker,
            owner_paired_002_record=holdout_record,
            source_revision="redi08c-validation-source",
            live=False,
        )
        holdout_snapshot = replay_holdout(
            db_path=holdout_outcome.db_path, factory_run_id=holdout_outcome.factory_run_id
        )
        holdout_result = replay_holdout_result(holdout_outcome.result_path)
        revalidation = recipe_v2_fallback_audit(holdout_stored)
        worker_selection_data = holdout_worker

        def _deterministic_selection_rule() -> None:
            registry = prior_sibling_registry(product_class_id=str(product_class.id))
            first = select_holdout_sibling(product_class=product_class, prior_used_registry=registry)
            again = select_holdout_sibling(product_class=product_class, prior_used_registry=registry)
            _check(
                first.selected_candidate_key == again.selected_candidate_key,
                "selection rule is not deterministic",
            )
            forced = dict(registry)
            forced["forced_seen"] = first.product_spec_sha256
            third = select_holdout_sibling(product_class=product_class, prior_used_registry=forced)
            _check(
                third.selected_candidate_key != first.selected_candidate_key,
                "rule did not advance to the next predeclared candidate",
            )

        holdout_checks: list[tuple[str, Callable[[], None]]] = [
            (
                "A v2 immutable + v3 worker-policy-only revalidation",
                lambda: _check(
                    len(holdout_store.list_versions(HISTORICAL_RECIPE_ID))
                    == HISTORICAL_RECIPE_VERSION + 1
                    and revalidation["decision"] == "V3_WORKER_POLICY_REVALIDATION_PINNED"
                    and revalidation["worker_policy_only_revision"] is True
                    and holdout_snapshot["recipe"]["parent_recipe_semantic_hash"]
                    == HISTORICAL_RECIPE_SEMANTIC_HASH
                    and holdout_stored.lifecycle_state.value == "shadow",
                    "recipe version policy",
                ),
            ),
            (
                "B v2 semantic identity with owner-paired-002 (lineage of v3)",
                lambda: _check(
                    not strict_identity
                    or canonical_recipe_semantic_hash(holdout_stored_v2.recipe)
                    == HISTORICAL_RECIPE_SEMANTIC_HASH,
                    "semantic identity",
                ),
            ),
            (
                "C2 oracle/test workers barred from the pinned ladder",
                lambda: _check(
                    not any(
                        classify_registry_worker(worker_id)["classification"]
                        == WORKER_CLASS_ORACLE
                        for worker_id in worker_selection_data["recipe_ladder"]
                    )
                    and all(
                        item.get("classification") != WORKER_CLASS_ORACLE
                        or item["worker_id"] != worker_selection_data["selected_worker_id"]
                        for item in worker_selection_data["skipped"]
                    )
                    and worker_selection_data["canonical_transfer_only"] is True,
                    "oracle exclusion",
                ),
            ),
            (
                "C3 worker classification deterministic",
                lambda: _check(
                    audit_worker_registry() == audit_worker_registry()
                    and classify_registry_worker("worker_pair_local_reorder_v0")["classification"]
                    == WORKER_CLASS_ORACLE,
                    "classification determinism",
                ),
            ),
            (
                "D no new worker invented (declared registries only)",
                lambda: _check(
                    worker_selection_data["selected_worker_id"]
                    in {item["worker_id"] for item in audit_worker_registry()}
                    and worker_selection_data["new_worker_invented"] is False,
                    "worker invention",
                ),
            ),
            (
                "E holdout predeclared and unseen",
                lambda: _check(
                    holdout_selection.policy_id == "redi08c_predeclared_holdout_selection_v0"
                    and holdout_selection.product_spec_sha256
                    not in set(holdout_selection.prior_used_registry.values()),
                    "holdout selection",
                ),
            ),
            (
                "F holdout materially different, class-compatible",
                lambda: _check(
                    holdout_difference_summary(holdout_selection.definition, product_class)[
                        "materially_different_vs_paired_sibling"
                    ]
                    and holdout_difference_summary(holdout_selection.definition, product_class)[
                        "class_invariants_equal"
                    ]
                    and holdout_snapshot["holdout_selection"]["match"]["decision"] == "COMPATIBLE",
                    "holdout difference",
                ),
            ),
            (
                "G selection rule deterministic + ordered",
                _deterministic_selection_rule,
            ),
            (
                "H dry transfer reaches PRODUCT_VERIFIED",
                lambda: _check(
                    holdout_outcome.verified
                    and all(status == "PASS" for status in holdout_outcome.gate_outcomes.values())
                    and set(holdout_outcome.gate_outcomes)
                    == {
                        "structured_output_schema",
                        "patch_output_contract",
                        "allowed_file_change_scope",
                        "python_syntax_compile",
                        "public_test_execution",
                        "private_test_execution",
                        "handoff_artifact_validation",
                        "integration_verification",
                        "independent_final_reviewer",
                    },
                    "dry transfer gates",
                ),
            ),
            (
                "I zero provider/network + hard slot budget",
                lambda: _check(
                    holdout_outcome.provider_calls
                    == holdout_outcome.network_calls
                    == holdout_snapshot["provider_calls"]
                    == holdout_snapshot["network_calls"]
                    == 0
                    and holdout_snapshot["pre_transfer_estimate"]["planned_live_provider_calls"]
                    == PLANNED_LIVE_PROVIDER_CALLS
                    and holdout_snapshot["pre_transfer_estimate"]["no_paid_retry"] is True,
                    "external calls/budget",
                ),
            ),
            (
                "J context compiler used; no private leakage",
                lambda: _check(
                    all(
                        item["station_role"] != "builder"
                        or (
                            item["strategy_id"] == "selected_v0"
                            and item["holdout_interface_present"] is True
                            and item["private_value_leakage_detected"] is False
                        )
                        for item in holdout_snapshot["context_manifests"]
                    )
                    and any(
                        item["station_role"] == "builder"
                        for item in holdout_snapshot["context_manifests"]
                    ),
                    "context manifests",
                ),
            ),
            (
                "K replay x2 + zero execution dependencies",
                lambda: _check(
                    holdout_snapshot
                    == replay_holdout(
                        db_path=holdout_outcome.db_path,
                        factory_run_id=holdout_outcome.factory_run_id,
                    )
                    and holdout_result
                    == replay_holdout_result(holdout_outcome.result_path)
                    and holdout_snapshot["replay_dependencies"]
                    == {
                        "workers": 0,
                        "provider_calls": 0,
                        "network": 0,
                        "writes": 0,
                        "recipe_reselection": False,
                        "current_config_influence": False,
                        "current_worker_registry": False,
                    },
                    "holdout replay",
                ),
            ),
            (
                "L recipe stays SHADOW; negative evidence preserved",
                lambda: _check(
                    holdout_snapshot["recipe_promoted"] is False
                    and holdout_snapshot["recipe"]["lifecycle_state"] == "shadow"
                    and holdout_snapshot["recipe_evidence_state"]["failed_transfer_attempt_refs"]
                    == [OWNER_PAIRED_002_PAIR_ID]
                    and holdout_snapshot["recipe_evidence_state"]["recipe_metadata_mutated"] is False
                    and holdout_snapshot["owner_paired_002_evidence"]["classification"]
                    == "VALID_PAIRED_FAILURE"
                    and holdout_snapshot["owner_paired_002_evidence"]["failure_class"]
                    == "ordinary_worker_implementation_failure",
                    "shadow/negative evidence",
                ),
            ),
            (
                "M full holdout audit green",
                lambda: _check(
                    all(
                        audit_holdout_transfer(
                            outcome=holdout_outcome, strict_identity=strict_identity
                        ).values()
                    ),
                    "holdout audit",
                ),
            ),
            (
                "N canonical ContextManifest + bounded rehearsal semantics",
                lambda: _check(
                    all(
                        item.get("canonical_contract") == "ContextManifest"
                        and item.get("__type__") == "ContextManifest"
                        and item.get("deterministic_hash")
                        for item in holdout_snapshot["context_manifests"]
                    )
                    and all(
                        item.get("private_value_leakage_detected") is False
                        and item.get("reference_solution_leakage_detected") is False
                        for item in holdout_snapshot["context_manifests"]
                        if item.get("station_role") == "builder"
                    )
                    and all(
                        item.get("private_value_leakage_detected") is False
                        for item in holdout_snapshot["context_manifests"]
                        if item.get("station_role") == "reviewer"
                    )
                    and holdout_snapshot["acceptance_semantics"] == ACCEPTANCE_HARNESS_REHEARSAL
                    and holdout_snapshot["counts_as_transfer_evidence"] is False
                    and holdout_snapshot["provider_live_acceptance"] is False
                    and holdout_snapshot["implementation_source"]
                    == "predeclared_reference_implementation_harness_rehearsal_only"
                    and holdout_snapshot["claims"]["worker_capability_transfer_claimed"] is False,
                    "context contract/semantics",
                ),
            ),
            (
                "O holdout definition byte-identical to predeclared pin",
                lambda: _check(
                    holdout_snapshot["holdout_product_spec_sha256"]
                    == PREDECLARED_HOLDOUT_PRODUCT_SHA256
                    and holdout_snapshot["fixture_hash"] == FROZEN_FIXTURE_TREE_HASH,
                    "holdout immutability",
                ),
            ),
            (
                "P generated patch retained immutably and bound to attempt",
                lambda: _check(
                    holdout_snapshot["patch_retention"]["retained"] is not None
                    and holdout_snapshot["patch_retention"]["binds_worker_attempt"] is True
                    and holdout_snapshot["patch_retention"]["retains_failed_attempts"] is True
                    and holdout_snapshot["patch_retention"]["excludes_reasoning"] is True
                    and holdout_snapshot["worker_attempts"][0]["retained_artifact"] is not None
                    and holdout_snapshot["worker_attempts"][0]["retained_artifact"]["sha256"]
                    == holdout_snapshot["worker_attempts"][0]["patch_sha256"]
                    and (
                        holdout_outcome.root
                        / "holdout"
                        / holdout_snapshot["patch_retention"]["retained"]["evidence_path"]
                    ).is_file(),
                    "patch retention",
                ),
            ),
            (
                "Q semantic context frozen against the failed genuine run",
                lambda: _check(
                    any(
                        item.get("station_role") == "builder"
                        and item.get("rendered_hash") == FROZEN_BUILDER_RENDERED_HASH
                        for item in holdout_snapshot["context_manifests"]
                    ),
                    "context freeze",
                ),
            ),
        ]
        print("REDI-08C predeclared holdout warm-transfer invariants")
        for index, (name, check) in enumerate(holdout_checks, 45):
            try:
                check()
            except Exception as exc:  # noqa: BLE001 - aggregate validation report
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
                print(f"  FAIL {index:02d} {name}: {type(exc).__name__}: {exc}")
            else:
                print(f"  PASS {index:02d} {name}")
        holdout_store.close()
        pair_store.close()
        store.close()
    total_checks = 44 + len(holdout_checks)
    print("=" * 64)
    print(f"TOTAL: PASS={total_checks - len(failures)} FAIL={len(failures)} CHECKS={total_checks}")
    if failures:
        for failure in failures:
            print("  - " + failure)
        return 1
    print("All REDI-08A/08B/08C validations passed (offline; zero provider/network).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
