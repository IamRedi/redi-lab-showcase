"""REDI-08C holdout warm-transfer acceptance tests + observability regression."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from redi_lab.cold_start import replay_cold_start, run_controlled_cold_start
from redi_lab.holdout_transfer import (
    ACCEPTED_PRODUCT_CLASS_ID,
    ACCEPTANCE_HARNESS_REHEARSAL,
    ALTERNATE_GENUINE_WORKER_ID,
    FROZEN_BUILDER_RENDERED_HASH,
    FROZEN_FIXTURE_TREE_HASH,
    GENUINE_HOLDOUT_FAILURE,
    create_alternate_worker_v3,
    load_genuine_holdout_failure_record,
    PREDECLARED_HOLDOUT_PRODUCT_SHA256,
    WORKER_CLASS_GENUINE,
    WORKER_CLASS_INELIGIBLE,
    WORKER_CLASS_ORACLE,
    WORKER_CLASS_TEST_ONLY,
    audit_worker_registry,
    classify_registry_worker,
    HISTORICAL_RECIPE_ID,
    HISTORICAL_RECIPE_SEMANTIC_HASH,
    HISTORICAL_RECIPE_VERSION,
    HoldoutError,
    OWNER_PAIRED_002_FAILED_WORKER,
    OWNER_PAIRED_002_PAIR_ID,
    PLANNED_LIVE_PROVIDER_CALLS,
    WORKER_SELECTION_POLICY_ID,
    audit_holdout_transfer,
    execute_holdout_warm_transfer,
    holdout_candidate_catalog,
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
from redi_lab.paired_experiment import MANDATORY_GATES, execute_paired_experiment
from redi_lab.recipe_store import RecipeStore, canonical_recipe_semantic_hash
from redi_lab.warm_start import (
    controlled_warm_stations,
    inventory_product_class,
)


ROOT = Path(__file__).resolve().parents[2]
HISTORICAL_LEDGER = ROOT / "artifacts" / "redi07b" / "redi07b-live-3962bb5a22bc" / "evidence.sqlite"
HISTORICAL_RUN = "factory_run_7f21dd99f6b94664be1434e4f6803393"
STRICT_IDENTITY = HISTORICAL_LEDGER.exists()


@pytest.fixture(scope="module")
def holdout(tmp_path_factory):
    work = tmp_path_factory.mktemp("redi08c_holdout")
    if HISTORICAL_LEDGER.exists():
        source_ledger = HISTORICAL_LEDGER
        source_run = HISTORICAL_RUN
    else:
        cold = run_controlled_cold_start(output_root=work / "source", known_recipe=None)
        source_ledger = cold.db_path
        source_run = cold.factory_run_id
    product_class = inventory_product_class(ACCEPTED_PRODUCT_CLASS_ID)
    store = RecipeStore(work / "recipes.sqlite")
    stored_v2 = prepare_holdout_recipe(
        store=store,
        source_ledger=source_ledger,
        source_factory_run_id=source_run,
        product_class=product_class,
        expected_semantic_hash=HISTORICAL_RECIPE_SEMANTIC_HASH if STRICT_IDENTITY else None,
    )
    owner_paired_002 = load_owner_paired_002_record()
    genuine_failure = load_genuine_holdout_failure_record()
    stored_v3 = create_alternate_worker_v3(
        store=store,
        stored_v2=stored_v2,
        product_class=product_class,
        owner_paired_002_record=owner_paired_002,
        genuine_failure_record=genuine_failure,
        alternate_worker_id=ALTERNATE_GENUINE_WORKER_ID,
    )
    selection = select_holdout_sibling(
        product_class=product_class,
        prior_used_registry=prior_sibling_registry(
            product_class_id=str(product_class.id),
            owner_paired_002_record=owner_paired_002,
        ),
    )
    worker_selection = select_holdout_worker(
        stored=stored_v3,
        owner_paired_002_record=owner_paired_002,
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
    outcome = execute_holdout_warm_transfer(
        output_root=work / "run",
        product_class=product_class,
        store=store,
        stored=stored_v3,
        selection=selection,
        worker_selection=worker_selection,
        owner_paired_002_record=owner_paired_002,
        source_revision="redi08c-unit-source-revision",
        live=False,
    )
    yield {
        "work": work,
        "store": store,
        "stored_v2": stored_v2,
        "stored_v3": stored_v3,
        "genuine_failure": genuine_failure,
        "product_class": product_class,
        "owner_paired_002": owner_paired_002,
        "selection": selection,
        "worker_selection": worker_selection,
        "outcome": outcome,
    }
    store.close()


# ---------------------------------------------------------------------------
# REDI-08C observability cleanup regression (section 3)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def paired_audit(tmp_path_factory):
    work = tmp_path_factory.mktemp("redi08c_paired_audit")
    if HISTORICAL_LEDGER.exists():
        source_ledger = HISTORICAL_LEDGER
        source_run = HISTORICAL_RUN
    else:
        cold = run_controlled_cold_start(output_root=work / "source", known_recipe=None)
        source_ledger = cold.db_path
        source_run = cold.factory_run_id
    from redi_lab.paired_experiment import prepare_shadow_recipe

    product_class = inventory_product_class(ACCEPTED_PRODUCT_CLASS_ID)
    from redi_lab.warm_start import sibling_product_fixture

    product = sibling_product_fixture(str(product_class.id))
    store = RecipeStore(work / "recipes.sqlite")
    stored = prepare_shadow_recipe(
        store=store,
        source_ledger=source_ledger,
        source_factory_run_id=source_run,
        product_class=product_class,
        approval_reference="redi08c-regression-paired",
    )
    outcome = execute_paired_experiment(
        output_root=work / "pair",
        product=product,
        product_class=product_class,
        store=store,
        recipe_id=str(stored.recipe.id),
        recipe_version=stored.recipe.version,
        source_revision="redi08c-unit-source-revision",
        live=False,
    )
    yield outcome.audit
    store.close()


def test_a_audit_uses_separated_gate_concepts(paired_audit):
    assert "gate_evidence_complete" not in paired_audit
    assert "gate_records_complete" in paired_audit
    assert "all_required_gates_passed" in paired_audit
    assert paired_audit["gate_records_complete"] is True
    assert paired_audit["all_required_gates_passed"] is True


def test_b_audit_uses_mode_neutral_pair_success_name(paired_audit):
    assert "dry_pair_success" not in paired_audit
    assert "pair_verified_success" in paired_audit
    assert paired_audit["pair_verified_success"] is True


# ---------------------------------------------------------------------------
# Recipe v2 revalidation decision (sections 4/5)
# ---------------------------------------------------------------------------


def test_c_recipe_v2_reproduced_and_still_immutable(holdout):
    stored_v2 = holdout["stored_v2"]
    assert str(stored_v2.recipe.id) == HISTORICAL_RECIPE_ID
    assert stored_v2.recipe.version == HISTORICAL_RECIPE_VERSION
    assert len(holdout["store"].list_versions(HISTORICAL_RECIPE_ID)) == 3
    if STRICT_IDENTITY:
        assert canonical_recipe_semantic_hash(stored_v2.recipe) == HISTORICAL_RECIPE_SEMANTIC_HASH


def test_d_recipe_v2_already_declares_the_genuine_worker(holdout):
    audit = recipe_v2_fallback_audit(holdout["stored_v2"])
    assert audit["decision"] == "REUSE_RECIPE_V2_FALLBACK_LADDER"
    assert audit["new_recipe_version_created"] is False
    assert audit["canonical_transfer_decision"] == "USE_V2_AS_IS_PREFERRED_WORKER_IS_GENUINE"
    assert audit["genuine_worker_is_recipe_preferred"] is True
    assert audit["genuine_execution_workers_in_ladder"] == ["worker_pair_provider_candidate_v0"]


def test_e_canonical_selection_picks_the_alternate_genuine_worker(holdout):
    selection = holdout["worker_selection"]
    assert selection["policy_id"] == WORKER_SELECTION_POLICY_ID
    assert selection["canonical_transfer_only"] is True
    assert selection["ladder_source"] == "pinned_recipe_worker_preferences"
    assert selection["selected_worker_id"] == ALTERNATE_GENUINE_WORKER_ID
    assert selection["selected_worker_classification"]["classification"] == WORKER_CLASS_GENUINE
    assert selection["new_worker_invented"] is False
    assert selection["provider_binding"]["makes_paid_provider_call"] is True
    assert selection["provider_binding"]["model"] == "gpt-4o-mini-2024-07-18"
    skipped = {item["worker_id"]: item for item in selection["skipped"]}
    assert skipped["worker_pair_provider_candidate_v0"]["classification"] == WORKER_CLASS_GENUINE
    assert "already failed this exact holdout" in skipped["worker_pair_provider_candidate_v0"]["reason"]


def test_e2_worker_legitimacy_classification_is_deterministic():
    registry = audit_worker_registry()
    assert registry == audit_worker_registry()
    classes = {item["worker_id"]: item["classification"] for item in registry}
    assert classes["worker_pair_provider_candidate_v0"] == WORKER_CLASS_GENUINE
    assert classes["worker_pair_local_reorder_v0"] == WORKER_CLASS_ORACLE
    assert classes["worker_pair_weak_explorer_v0"] == WORKER_CLASS_INELIGIBLE
    assert classes["worker_pair_independent_reviewer_v0"] == WORKER_CLASS_TEST_ONLY
    assert classes["worker_cold_openai_gpt41_candidate_v0"] == WORKER_CLASS_GENUINE
    assert classes["worker_cold_openai_live_fallback_v0"] == WORKER_CLASS_GENUINE


def test_f_owner_paired_002_negative_evidence_preserved(holdout):
    record = holdout["owner_paired_002"]
    assert record["classification"] == "VALID_PAIRED_FAILURE"
    assert record["failure_class"] == "ordinary_worker_implementation_failure"
    assert record["globally_bad_worker_concluded"] is False
    assert record["transfer_failure"] is False


# ---------------------------------------------------------------------------
# Predeclared holdout selection (sections 6/7)
# ---------------------------------------------------------------------------


def test_g_holdout_selection_is_predeclared_and_unseen(holdout):
    selection = holdout["selection"]
    assert selection.selected_candidate_key == "depot_replenishment"
    assert selection.product_spec_sha256 not in set(selection.prior_used_registry.values())
    catalog = [item.key for item in holdout_candidate_catalog(str(holdout["product_class"].id))]
    assert catalog == ["depot_replenishment", "clinic_supply_replenishment"]


def test_h_holdout_selection_rule_is_deterministic_and_order_fixed(holdout):
    product_class = holdout["product_class"]
    registry = prior_sibling_registry(product_class_id=str(product_class.id))
    first = select_holdout_sibling(product_class=product_class, prior_used_registry=registry)
    second = select_holdout_sibling(product_class=product_class, prior_used_registry=registry)
    assert first.selected_candidate_key == second.selected_candidate_key == "depot_replenishment"
    forced = dict(registry)
    forced["forced_seen"] = first.product_spec_sha256
    third = select_holdout_sibling(product_class=product_class, prior_used_registry=forced)
    assert third.selected_candidate_key == "clinic_supply_replenishment"


def test_i_holdout_differs_materially_from_all_prior_siblings(holdout):
    summary = holdout_difference_summary(holdout["selection"].definition, holdout["product_class"])
    assert summary["materially_different_vs_source"] is True
    assert summary["materially_different_vs_paired_sibling"] is True
    assert summary["class_invariants_equal"] is True
    assert summary["changed_product_variable_fields_vs_paired_sibling"]


# ---------------------------------------------------------------------------
# Dry holdout warm transfer (sections 8-15, 19)
# ---------------------------------------------------------------------------


def test_j_dry_transfer_reaches_product_verified(holdout):
    outcome = holdout["outcome"]
    assert outcome.verified is True
    assert set(outcome.gate_outcomes) == set(MANDATORY_GATES)
    assert all(status == "PASS" for status in outcome.gate_outcomes.values())


def test_k_dry_transfer_uses_zero_provider_and_network(holdout):
    outcome = holdout["outcome"]
    assert outcome.provider_calls == 0
    assert outcome.network_calls == 0
    assert outcome.estimate["planned_live_provider_calls"] == PLANNED_LIVE_PROVIDER_CALLS
    assert outcome.estimate["no_paid_retry"] is True
    assert outcome.estimate["expected_provider_calls_for_selected_worker"] == 1


def test_l_context_reaches_worker_without_private_leakage(holdout):
    snapshot = replay_holdout(
        db_path=holdout["outcome"].db_path, factory_run_id=holdout["outcome"].factory_run_id
    )
    builder = next(item for item in snapshot["context_manifests"] if item["station_role"] == "builder")
    assert builder["holdout_interface_present"] is True
    assert builder["private_value_leakage_detected"] is False
    assert builder["package_bytes"] > 0 and builder["rendered_request_bytes"] > 0


def test_l2_context_manifests_use_canonical_domain_contract(holdout):
    snapshot = replay_holdout(
        db_path=holdout["outcome"].db_path, factory_run_id=holdout["outcome"].factory_run_id
    )
    assert snapshot["context_manifests"], "context manifests must be persisted"
    for manifest in snapshot["context_manifests"]:
        assert manifest["__type__"] == "ContextManifest"
        assert manifest["canonical_contract"] == "ContextManifest"
        assert manifest["id"].startswith("context_manifest_")
        assert manifest["deterministic_hash"]
    # the worker attempt must reference the canonical manifest id
    attempt = next(
        item for item in snapshot["worker_attempts"] if item["node_id"] == "implement_normalize"
    )
    outcome = holdout["outcome"]
    evidence_ids = {item["evidence_id"] for item in snapshot["worker_attempts"]}
    assert attempt["evidence_id"] in evidence_ids


def test_l3_acceptance_semantics_and_claims_are_bounded(holdout):
    snapshot = replay_holdout(
        db_path=holdout["outcome"].db_path, factory_run_id=holdout["outcome"].factory_run_id
    )
    assert snapshot["acceptance_semantics"] == ACCEPTANCE_HARNESS_REHEARSAL
    assert snapshot["counts_as_transfer_evidence"] is False
    assert snapshot["provider_live_acceptance"] is False
    assert snapshot["implementation_source"] == "predeclared_reference_implementation_harness_rehearsal_only"
    assert snapshot["claims"]["worker_capability_transfer_claimed"] is False
    assert snapshot["claims"]["harness_rehearsal_is_not_transfer_evidence"] is True
    assert snapshot["claims"]["provider_live_acceptance_claimed"] is False
    assert snapshot["claims"]["recipe_pipeline_transfer_on_unseen_holdout"] is True


def test_m_replay_x2_literal_equality(holdout):
    outcome = holdout["outcome"]
    one = replay_holdout(db_path=outcome.db_path, factory_run_id=outcome.factory_run_id)
    two = replay_holdout(db_path=outcome.db_path, factory_run_id=outcome.factory_run_id)
    assert one == two
    assert one["replay_dependencies"]["workers"] == 0
    assert one["replay_dependencies"]["provider_calls"] == 0
    assert one["replay_dependencies"]["recipe_reselection"] is False
    result_one = replay_holdout_result(outcome.result_path)
    result_two = replay_holdout_result(outcome.result_path)
    assert result_one == result_two


def test_n_recipe_stays_shadow_and_unpromoted(holdout):
    stored = holdout["store"].read(HISTORICAL_RECIPE_ID, HISTORICAL_RECIPE_VERSION)
    assert stored.lifecycle_state.value == "shadow"
    snapshot = replay_holdout(
        db_path=holdout["outcome"].db_path, factory_run_id=holdout["outcome"].factory_run_id
    )
    assert snapshot["recipe_promoted"] is False
    assert snapshot["recipe"]["lifecycle_state"] == "shadow"
    assert snapshot["recipe_evidence_state"]["recipe_metadata_mutated"] is False


def test_o_full_audit_green(holdout):
    audit = dict(
        audit_holdout_transfer(outcome=holdout["outcome"], strict_identity=STRICT_IDENTITY)
    )
    assert audit, "audit must not be empty"
    failed = {key: value for key, value in audit.items() if not value}
    assert not failed, f"failed invariants: {failed}"


# ---------------------------------------------------------------------------
# Failure taxonomy discipline (section 16)
# ---------------------------------------------------------------------------


def test_p_owner_paired_002_failure_is_not_classified_as_recipe_failure(holdout):
    snapshot = replay_holdout(
        db_path=holdout["outcome"].db_path, factory_run_id=holdout["outcome"].factory_run_id
    )
    record = snapshot["owner_paired_002_evidence"]
    assert record["failure_class"] == "ordinary_worker_implementation_failure"
    assert record["recipe_configuration_causal"] is False
    assert record["transfer_failure"] is False


def test_q_live_provider_execution_requires_explicit_env_guard():
    import tempfile

    from redi_lab.domain import ProductClassId
    from redi_lab.holdout_transfer import HoldoutSelection

    definition = holdout_candidate_catalog(ACCEPTED_PRODUCT_CLASS_ID)[0]
    selection = HoldoutSelection(
        definition=definition,
        match={"decision": "COMPATIBLE"},
        product_spec_sha256="0" * 64,
        selected_candidate_key=definition.key,
        skipped_candidates=(),
        prior_used_registry={},
    )
    with pytest.raises(HoldoutError, match="REDI_LIVE_PROVIDER"):
        execute_holdout_warm_transfer(
            output_root=Path(tempfile.mkdtemp(prefix="redi08c_live_guard_")),
            product_class=inventory_product_class(ACCEPTED_PRODUCT_CLASS_ID),
            store=None,
            stored=None,
            selection=selection,
            worker_selection={
                "selected_worker_id": "worker_pair_provider_candidate_v0",
                "selected_worker_profile": {
                    "id": "worker_pair_provider_candidate_v0",
                    "provider": "openai",
                    "model": "gpt-4.1-2025-04-14",
                },
                "selected_worker_classification": {"classification": WORKER_CLASS_GENUINE},
            },
            owner_paired_002_record=load_owner_paired_002_record(),
            source_revision="guard",
            live=True,
        )


def test_r_reference_oracle_worker_may_not_execute_the_owner_run(holdout):
    """REDI-08C2: a non-genuine worker can never carry the owner acceptance."""

    local_selection = dict(holdout["worker_selection"])
    local_selection["selected_worker_id"] = "worker_pair_local_reorder_v0"
    local_selection["selected_worker_profile"] = dict(
        holdout["worker_selection"]["selected_worker_profile"]
    )
    local_selection["selected_worker_profile"].pop("provider", None)
    local_selection["selected_worker_profile"].pop("model", None)
    local_selection["selected_worker_classification"] = {
        "classification": WORKER_CLASS_ORACLE
    }
    with pytest.raises(HoldoutError, match="genuine"):
        execute_holdout_warm_transfer(
            output_root=tmp_path_factory_dir(),
            product_class=holdout["product_class"],
            store=holdout["store"],
            stored=holdout["stored_v3"],
            selection=holdout["selection"],
            worker_selection=local_selection,
            owner_paired_002_record=holdout["owner_paired_002"],
            source_revision="redi08c-refusal-rehearsal",
            live=True,
        )


def tmp_path_factory_dir() -> Path:
    import tempfile

    return Path(tempfile.mkdtemp(prefix="redi08c_refusal_"))


# ---------------------------------------------------------------------------
# REDI-08C2: oracle isolation, holdout immutability, registry independence
# ---------------------------------------------------------------------------


def test_s_reference_solution_never_enters_worker_input(holdout):
    snapshot = replay_holdout(
        db_path=holdout["outcome"].db_path, factory_run_id=holdout["outcome"].factory_run_id
    )
    builder = next(
        item for item in snapshot["context_manifests"] if item["station_role"] == "builder"
    )
    assert builder["reference_solution_leakage_detected"] is False
    definition = holdout["selection"].definition
    from redi_lab.holdout_transfer import _reference_leakage_scan

    scan_target = "def normalize_depot_code(depot: str, code: str) -> str:"
    assert _reference_leakage_scan(definition, scan_target) is True
    assert _reference_leakage_scan(definition, "public contract text only") is False


def test_t_holdout_definition_is_byte_identical_to_predeclared_pin(holdout):
    from redi_lab.holdout_transfer import product_spec_hash

    definition = holdout["selection"].definition
    assert product_spec_hash(definition.product) == PREDECLARED_HOLDOUT_PRODUCT_SHA256
    assert str(definition.product.id) == "product_redi08c_holdout_depot_replenishment"


def test_u_v3_is_worker_policy_only_with_correct_parentage(holdout):
    from dataclasses import fields

    v2 = holdout["stored_v2"].recipe
    v3 = holdout["stored_v3"].recipe
    assert v3.version == HISTORICAL_RECIPE_VERSION + 1
    changed = set()
    for field in fields(v2):
        if field.name in {"version", "created_at", "last_revalidated_at",
                          "worker_preferences", "worker_fallbacks"}:
            continue
        if getattr(v2, field.name) != getattr(v3, field.name):
            changed.add(field.name)
    assert not changed, f"non-worker-policy fields changed: {sorted(changed)}"
    assert v3.worker_preferences != v2.worker_preferences
    impl = controlled_warm_stations()[0].id
    assert str(v3.worker_preferences[impl][0]) == ALTERNATE_GENUINE_WORKER_ID
    provenance = holdout["stored_v3"].provenance
    assert provenance["parent_recipe_semantic_hash"] == HISTORICAL_RECIPE_SEMANTIC_HASH
    evidence = provenance["revalidation_evidence"]
    assert evidence["owner_paired_002"]["pair_id"] == OWNER_PAIRED_002_PAIR_ID
    assert evidence["genuine_holdout_failure"]["failure_record_id"] == (
        holdout["genuine_failure"]["failure_record_id"]
    )
    assert evidence["oracle_test_workers_barred"] is True


def test_u2_v3_remains_shadow_and_store_records_selection(holdout):
    stored_v3 = holdout["store"].read(HISTORICAL_RECIPE_ID, HISTORICAL_RECIPE_VERSION + 1)
    assert stored_v3.lifecycle_state.value == "shadow"
    events = [e["event_type"] for e in holdout["store"].events(HISTORICAL_RECIPE_ID)]
    assert "RECIPE_PROMOTED" not in events


def test_u3_holdout_and_context_frozen_against_failed_run(holdout):
    outcome = holdout["outcome"]
    assert outcome.selection.product_spec_sha256 == PREDECLARED_HOLDOUT_PRODUCT_SHA256
    assert outcome.fixture_hash == FROZEN_FIXTURE_TREE_HASH
    snapshot = replay_holdout(db_path=outcome.db_path, factory_run_id=outcome.factory_run_id)
    builder = next(
        item for item in snapshot["context_manifests"] if item["station_role"] == "builder"
    )
    assert builder["rendered_hash"] == FROZEN_BUILDER_RENDERED_HASH


def test_v2_historical_ledger_untouched_by_recipe_preparation(holdout, tmp_path):
    ledger = ROOT / "artifacts" / "redi07b" / "redi07b-live-3962bb5a22bc" / "evidence.sqlite"
    if not ledger.exists():
        import pytest

        pytest.skip("accepted REDI-07B ledger not present")
    before = ledger.read_bytes()
    prepare_holdout_recipe(
        store=RecipeStore(tmp_path / "r.sqlite"),
        source_ledger=ledger,
        source_factory_run_id=HISTORICAL_RUN,
        product_class=inventory_product_class(ACCEPTED_PRODUCT_CLASS_ID),
    )
    assert ledger.read_bytes() == before


def test_v_replay_does_not_consult_current_worker_registry(holdout):
    snapshot = replay_holdout(
        db_path=holdout["outcome"].db_path, factory_run_id=holdout["outcome"].factory_run_id
    )
    assert snapshot["replay_dependencies"]["current_worker_registry"] is False
    assert snapshot["replay_dependencies"]["workers"] == 0
