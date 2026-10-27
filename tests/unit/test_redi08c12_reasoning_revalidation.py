"""REDI-08C12 reasoning-configuration revalidation regressions.

The ONLY material execution change authorized by C12 is reasoning_effort
medium -> low on a new immutable WorkerProfile version, bound to a new SHADOW
recipe version at the UNCHANGED 2048 completion ceiling.  Everything else —
provider, model, holdout, task context, output contract, gates, one-call
policy — must remain byte-frozen, and both historical medium runs must keep
reconstructing exactly their recorded configurations.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from redi_lab.cold_start import run_controlled_cold_start
from redi_lab.holdout_transfer import (
    ACCEPTED_PRODUCT_CLASS_ID,
    ACCEPTED_SOURCE_FACTORY_RUN_ID,
    ALTERNATE_GENUINE_HOLDOUT_FAILURE,
    ALTERNATE_GENUINE_WORKER_ID,
    BUILDER_PATCH_OUTPUT_POLICY_ID,
    FROZEN_BUILDER_RENDERED_HASH,
    FROZEN_FIXTURE_TREE_HASH,
    GPT56_TERRA_WORKER_ID,
    GPT56_TERRA_WORKER_VERSION,
    GENUINE_HOLDOUT_FAILURE,
    HISTORICAL_RECIPE_VERSION,
    HISTORICAL_TERRA_SATURATION_RUNS,
    PLANNED_LIVE_PROVIDER_CALLS,
    PREDECLARED_HOLDOUT_PRODUCT_SHA256,
    REASONING_REVALIDATION_METADATA,
    REASONING_REVALIDATION_POLICY_ID,
    TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION,
    TERRA_PROFILE_VERSION_BY_RECIPE_VERSION,
    TERRA_REASONING_EFFORT_BY_PROFILE_VERSION,
    audit_holdout_transfer,
    create_alternate_worker_v3,
    create_completion_budget_revalidation_v5,
    create_gpt56_terra_worker_v4,
    create_reasoning_revalidation_v6,
    execute_holdout_warm_transfer,
    gpt56_terra_holdout_worker_profile,
    load_genuine_holdout_failure_record,
    load_owner_paired_002_record,
    prepare_holdout_recipe,
    prior_sibling_registry,
    replay_holdout,
    select_holdout_sibling,
    select_holdout_worker,
    HoldoutError,
)
from redi_lab.recipe_store import RecipeStore, recipe_semantic_payload
from redi_lab.warm_start import inventory_product_class


ROOT = Path(__file__).resolve().parents[2]
HISTORICAL_LEDGER = ROOT / "artifacts" / "redi07b" / "redi07b-live-3962bb5a22bc" / "evidence.sqlite"
RUN_A_1024 = ROOT / "build" / "redi08c8-owner-terra-20260921T164206Z"
RUN_A_1024_ID = "factory_run_f4aa3a32e4754bfa94210404c549ccd8"
RUN_B_2048 = ROOT / "build" / "redi08c10-owner-terra-20260921T181641Z"
RUN_B_2048_ID = "factory_run_7e022c35f18e4ee99f68c3a856bcd54d"


@pytest.fixture(scope="module")
def c12(tmp_path_factory):
    """Recipe lineage v2 -> v3 -> v4 -> v5 -> v6 plus the frozen selection."""
    work = tmp_path_factory.mktemp("redi08c12")
    if HISTORICAL_LEDGER.exists():
        source_ledger, source_run = HISTORICAL_LEDGER, ACCEPTED_SOURCE_FACTORY_RUN_ID
    else:
        cold = run_controlled_cold_start(output_root=work / "source", known_recipe=None)
        source_ledger, source_run = cold.db_path, cold.factory_run_id
    product_class = inventory_product_class(ACCEPTED_PRODUCT_CLASS_ID)
    store = RecipeStore(work / "recipes.sqlite")
    owner = load_owner_paired_002_record()
    gpt41_failure = load_genuine_holdout_failure_record(ROOT / "build")
    v2 = prepare_holdout_recipe(
        store=store,
        source_ledger=source_ledger,
        source_factory_run_id=source_run,
        product_class=product_class,
    )
    v3 = create_alternate_worker_v3(
        store=store,
        stored_v2=v2,
        product_class=product_class,
        owner_paired_002_record=owner,
        genuine_failure_record=gpt41_failure,
        alternate_worker_id=ALTERNATE_GENUINE_WORKER_ID,
    )
    v4 = create_gpt56_terra_worker_v4(
        store=store,
        stored_v3=v3,
        product_class=product_class,
        gpt41_failure_record=gpt41_failure,
        gpt4o_mini_failure_record=ALTERNATE_GENUINE_HOLDOUT_FAILURE,
    )
    v5 = create_completion_budget_revalidation_v5(
        store=store,
        stored_v4=v4,
        product_class=product_class,
    )
    v6 = create_reasoning_revalidation_v6(
        store=store,
        stored_v5=v5,
        product_class=product_class,
    )
    selection = select_holdout_sibling(
        product_class=product_class,
        prior_used_registry=prior_sibling_registry(
            product_class_id=str(product_class.id), owner_paired_002_record=owner
        ),
    )
    return {
        "work": work,
        "store": store,
        "product_class": product_class,
        "owner": owner,
        "v4": v4,
        "v5": v5,
        "v6": v6,
        "selection": selection,
    }


def test_terra_profile_v3_changes_only_reasoning_effort():
    v2 = gpt56_terra_holdout_worker_profile(version=2)
    v3 = gpt56_terra_holdout_worker_profile(version=3)
    assert TERRA_REASONING_EFFORT_BY_PROFILE_VERSION == {
        1: "medium",
        2: "medium",
        3: "low",
        4: "low",
    }
    # The completion ceiling is FROZEN at 2048 across v2 and v3.
    assert v2["runtime"]["max_output_tokens"] == 2048
    assert v3["runtime"]["max_output_tokens"] == 2048
    assert TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION[3] == 2048
    # The ONLY difference v2 -> v3 is version + reasoning_effort.
    assert {k for k in v2 if v2[k] != v3[k]} == {"version", "runtime"}
    assert {k for k in v2["runtime"] if v2["runtime"][k] != v3["runtime"][k]} == {
        "reasoning_effort"
    }
    assert v2["runtime"]["reasoning_effort"] == "medium"
    assert v3["runtime"]["reasoning_effort"] == "low"
    for profile in (v2, v3):
        assert profile["id"] == GPT56_TERRA_WORKER_ID
        assert profile["model"] == "gpt-5.6-terra"
        assert profile["provider"] == "openai"
        assert profile["runtime"]["adapter"] == "LiveProviderAdapter"
        assert profile["runtime"]["api_capability_profile"] == "openai_chat_gpt56_v1"
        assert profile["runtime"]["response_format"] == "json_schema_strict"
        assert profile["runtime"]["temperature"] is None
        assert profile["runtime"]["top_p"] is None
        assert profile["station_eligibility"] == v2["station_eligibility"]
        assert profile["task_eligibility"] == v2["task_eligibility"]
        assert profile["structured_output_required"] is True
    # Canonical registry entry is v3; unknown versions fail closed.
    # (Version 4 exists since REDI-08C14 and differs only in execution API.)
    assert GPT56_TERRA_WORKER_VERSION == 3
    assert gpt56_terra_holdout_worker_profile()["version"] == 3
    with pytest.raises(HoldoutError):
        gpt56_terra_holdout_worker_profile(version=5)


def test_recipe_v6_is_reasoning_only_shadow_child_of_v5(c12):
    v5, v6 = c12["v5"], c12["v6"]
    assert v5.recipe.version == HISTORICAL_RECIPE_VERSION + 3
    assert v6.recipe.version == HISTORICAL_RECIPE_VERSION + 4
    assert str(v6.recipe.id) == str(v5.recipe.id)
    assert v6.lifecycle_state.value == "shadow"
    assert v6.revalidation_state == "VALID_FOR_CONTROLLED_EXPERIMENT"
    assert v6.provenance["parent_recipe_version"] == v5.recipe.version
    # Worker tuple unchanged: same single Terra worker, no new worker.
    assert v6.recipe.worker_preferences == v5.recipe.worker_preferences
    assert v6.recipe.worker_fallbacks == v5.recipe.worker_fallbacks
    # The ONLY semantic delta vs v5 is the reasoning_revalidation reference;
    # the C10 budget metadata is inherited unchanged.
    payload_v5 = recipe_semantic_payload(v5.recipe)
    payload_v6 = recipe_semantic_payload(v6.recipe)
    assert payload_v6.pop("version") == payload_v5.pop("version") + 1
    assert set(payload_v6) == set(payload_v5)
    assert payload_v6["metadata"]["completion_budget_revalidation"] == (
        payload_v5["metadata"]["completion_budget_revalidation"]
    )
    assert set(payload_v6["metadata"]) - set(payload_v5["metadata"]) == {
        "reasoning_revalidation"
    }
    assert payload_v6["metadata"]["reasoning_revalidation"] == REASONING_REVALIDATION_METADATA
    assert REASONING_REVALIDATION_METADATA["value"] == "low"
    assert REASONING_REVALIDATION_METADATA["prior_value"] == "medium"
    assert REASONING_REVALIDATION_METADATA["completion_budget_unchanged_tokens"] == 2048
    # Provenance records BOTH saturation runs and the audit verdict.
    evidence = v6.provenance["revalidation_evidence"]
    assert evidence["historical_terra_saturation_runs"] == HISTORICAL_TERRA_SATURATION_RUNS
    runs = evidence["historical_terra_saturation_runs"]
    assert runs["run_a_medium_1024"]["reasoning_tokens"] == 1024
    assert runs["run_b_medium_2048"]["reasoning_tokens"] == 2048
    assert runs["run_a_medium_1024"]["finish_reason"] == "length"
    assert runs["run_b_medium_2048"]["finish_reason"] == "length"
    assert runs["run_a_medium_1024"]["visible_answer_content"] == "none"
    assert runs["run_b_medium_2048"]["visible_answer_content"] == "none"
    assert evidence["budget_audit_verdict"] == "REASONING_BUDGET_SATURATION_REPEATED"
    assert "downgrade" in evidence["no_capability_claim"]
    assert evidence["automatic_paid_retry"] is False
    # Registration metadata pins the exact profile version and effort.
    assert v6.registration_metadata["revision_kind"] == "reasoning_config_only"
    assert v6.registration_metadata["authorized_worker_profile_version"] == 3
    assert v6.registration_metadata["authorized_reasoning_effort"] == "low"
    assert v6.registration_metadata["authorized_completion_budget"] == 2048
    assert v6.registration_metadata["automatic_paid_retry"] is False
    assert v6.provenance["revalidation_policy_id"] == REASONING_REVALIDATION_POLICY_ID


def test_selection_matrix_resolves_all_frozen_configurations(c12):
    owner = c12["owner"]
    expected = {
        HISTORICAL_RECIPE_VERSION + 2: (1, "medium", 1024),
        HISTORICAL_RECIPE_VERSION + 3: (2, "medium", 2048),
        HISTORICAL_RECIPE_VERSION + 4: (3, "low", 2048),
    }
    for stored in (c12["v4"], c12["v5"], c12["v6"]):
        selection = select_holdout_worker(stored=stored, owner_paired_002_record=owner)
        version, effort, budget = expected[stored.recipe.version]
        assert selection["selected_worker_id"] == GPT56_TERRA_WORKER_ID
        assert selection["selected_worker_profile_version"] == version
        profile = selection["selected_worker_profile"]
        runtime = profile["runtime"]
        assert runtime["reasoning_effort"] == effort
        assert runtime["max_output_tokens"] == budget
        assert profile["model"] == "gpt-5.6-terra"
        assert TERRA_PROFILE_VERSION_BY_RECIPE_VERSION[stored.recipe.version] == version


def test_unmapped_recipe_version_blocks_silent_effort_drift(c12):
    drifted = SimpleNamespace(
        recipe=replace(c12["v6"].recipe, version=HISTORICAL_RECIPE_VERSION + 6)
    )
    with pytest.raises(HoldoutError, match="completion-budget drift"):
        select_holdout_worker(stored=drifted, owner_paired_002_record=c12["owner"])


def _run_fake(c12, stored, selection, name):
    return execute_holdout_warm_transfer(
        output_root=c12["work"] / name,
        product_class=c12["product_class"],
        store=c12["store"],
        stored=stored,
        selection=c12["selection"],
        worker_selection=selection,
        owner_paired_002_record=c12["owner"],
        source_revision="redi08c12-unit",
        live=False,
    )


def test_v5_dry_rehearsal_still_resolves_medium_2048(c12):
    selection = select_holdout_worker(stored=c12["v5"], owner_paired_002_record=c12["owner"])
    outcome = _run_fake(c12, c12["v5"], selection, "v5_rehearsal")
    snapshot = replay_holdout(db_path=outcome.db_path, factory_run_id=outcome.factory_run_id)
    assert snapshot["recipe"]["recipe_version"] == HISTORICAL_RECIPE_VERSION + 3
    assert snapshot["pre_transfer_estimate"]["max_output_tokens"] == 2048
    assert snapshot["pre_transfer_estimate"]["worker_runtime"]["reasoning_effort"] == "medium"
    assert snapshot["worker_selection"]["selected_worker_profile_version"] == 2
    assert outcome.provider_calls == outcome.network_calls == 0
    audit = audit_holdout_transfer(outcome=outcome, strict_identity=True)
    assert all(audit.values()), {k: v for k, v in audit.items() if not v}


def test_v6_dry_rehearsal_is_the_exact_future_path(c12):
    selection = select_holdout_worker(stored=c12["v6"], owner_paired_002_record=c12["owner"])
    outcome = _run_fake(c12, c12["v6"], selection, "v6_rehearsal")
    one = replay_holdout(db_path=outcome.db_path, factory_run_id=outcome.factory_run_id)
    two = replay_holdout(db_path=outcome.db_path, factory_run_id=outcome.factory_run_id)
    # Zero provider/network; deterministic replay x2.
    assert outcome.provider_calls == outcome.network_calls == 0
    assert one == two
    # Fake PRODUCT_VERIFIED is rehearsal only and never transfer evidence.
    assert outcome.verified is True
    assert one["acceptance_semantics"] == "HARNESS_REHEARSAL_ONLY"
    assert one["counts_as_transfer_evidence"] is False
    # Holdout + context freeze.
    assert one["holdout_product_spec_sha256"] == PREDECLARED_HOLDOUT_PRODUCT_SHA256
    assert one["fixture_hash"] == FROZEN_FIXTURE_TREE_HASH
    builder = next(m for m in one["context_manifests"] if m["station_role"] == "builder")
    assert builder["rendered_hash"] == FROZEN_BUILDER_RENDERED_HASH
    assert not builder.get("failure_evidence_included")
    assert not builder.get("previous_decisions")
    # Output contract unchanged.
    contract = one["builder_output_contract"]
    assert contract["policy_id"] == BUILDER_PATCH_OUTPUT_POLICY_ID
    assert contract["policy_version"] == 1
    assert contract["canonical_hash"] == (
        "012b881902d568257763311ddd3a340e44f176542525ebba14dd030961b85abc"
    )
    # The experimental configuration: low effort at the SAME 2048 ceiling.
    estimate = one["pre_transfer_estimate"]
    assert estimate["worker_runtime"]["reasoning_effort"] == "low"
    assert estimate["max_output_tokens"] == 2048
    assert estimate["total_token_upper_bound"] == estimate["estimated_input_tokens"] + 2048
    assert estimate["planned_live_provider_calls"] == PLANNED_LIVE_PROVIDER_CALLS == 1
    assert estimate["no_paid_retry"] is True
    assert estimate["worker_profile"]["version"] == 3
    # Recipe/provenance bound to the C12 revalidation.
    assert one["recipe"]["recipe_version"] == HISTORICAL_RECIPE_VERSION + 4
    assert one["recipe"]["metadata"]["reasoning_revalidation"] == REASONING_REVALIDATION_METADATA
    assert (
        one["revalidation_decision"]["decision"] == "V6_REASONING_REVALIDATION_PINNED"
    )
    # Governed worker attempt + patch retention + observed-access metadata.
    attempt = one["worker_attempts"][0]
    assert attempt["worker_id"] == GPT56_TERRA_WORKER_ID
    assert one["patch_retention"]["retained"] is not None
    assert len(outcome.gate_outcomes) == 9
    assert all(state == "PASS" for state in outcome.gate_outcomes.values())
    # The strict self-audit (exact future live gate check) is fully green.
    audit = audit_holdout_transfer(outcome=outcome, strict_identity=True)
    assert all(audit.values()), {k: v for k, v in audit.items() if not v}


@pytest.mark.skipif(
    not (RUN_A_1024 / "run" / "holdout" / "evidence.sqlite").exists()
    or not (RUN_B_2048 / "run" / "holdout" / "evidence.sqlite").exists(),
    reason="historical Terra run artifacts not present on this machine",
)
def test_historical_replay_matrix_stays_medium():
    snapshot_a = replay_holdout(
        db_path=RUN_A_1024 / "run" / "holdout" / "evidence.sqlite",
        factory_run_id=RUN_A_1024_ID,
    )
    estimate_a = snapshot_a["pre_transfer_estimate"]
    assert estimate_a["worker_runtime"]["reasoning_effort"] == "medium"
    assert estimate_a["max_output_tokens"] == 1024
    assert snapshot_a["provider_completion"]["completion_token_details"]["reasoning_tokens"] == 1024
    snapshot_b = replay_holdout(
        db_path=RUN_B_2048 / "run" / "holdout" / "evidence.sqlite",
        factory_run_id=RUN_B_2048_ID,
    )
    estimate_b = snapshot_b["pre_transfer_estimate"]
    assert estimate_b["worker_runtime"]["reasoning_effort"] == "medium"
    assert estimate_b["max_output_tokens"] == 2048
    assert snapshot_b["provider_completion"]["completion_token_details"]["reasoning_tokens"] == 2048


@pytest.mark.skipif(
    not (RUN_A_1024 / "run" / "holdout" / "evidence.sqlite").exists(),
    reason="historical Terra run artifacts not present on this machine",
)
def test_replay_is_independent_of_current_registry_defaults(c12, monkeypatch):
    import redi_lab.holdout_transfer as ht

    # Execute the fresh v6 rehearsal BEFORE corrupting the live constants.
    selection = select_holdout_worker(stored=c12["v6"], owner_paired_002_record=c12["owner"])
    outcome = _run_fake(c12, c12["v6"], selection, "v6_replay_independence")
    monkeypatch.setattr(ht, "TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION", {1: 7777, 2: 7777, 3: 7777, 4: 7777})
    monkeypatch.setattr(ht, "TERRA_REASONING_EFFORT_BY_PROFILE_VERSION", {1: "high", 2: "high", 3: "xhigh", 4: "xhigh"})
    monkeypatch.setattr(ht, "TERRA_PROFILE_VERSION_BY_RECIPE_VERSION", {})
    # Historical medium runs keep their recorded configurations.
    snapshot_a = replay_holdout(
        db_path=RUN_A_1024 / "run" / "holdout" / "evidence.sqlite",
        factory_run_id=RUN_A_1024_ID,
    )
    assert snapshot_a["pre_transfer_estimate"]["worker_runtime"]["reasoning_effort"] == "medium"
    assert snapshot_a["pre_transfer_estimate"]["max_output_tokens"] == 1024
    snapshot_b = replay_holdout(
        db_path=RUN_B_2048 / "run" / "holdout" / "evidence.sqlite",
        factory_run_id=RUN_B_2048_ID,
    )
    assert snapshot_b["pre_transfer_estimate"]["worker_runtime"]["reasoning_effort"] == "medium"
    assert snapshot_b["pre_transfer_estimate"]["max_output_tokens"] == 2048
    # Fresh v6 replay keeps low / 2048 from its own persisted evidence.
    replayed = replay_holdout(db_path=outcome.db_path, factory_run_id=outcome.factory_run_id)
    assert replayed["pre_transfer_estimate"]["worker_runtime"]["reasoning_effort"] == "low"
    assert replayed["pre_transfer_estimate"]["max_output_tokens"] == 2048


def test_recipe_store_keeps_full_lineage_without_promotion(c12):
    versions = {
        stored.recipe.version: stored.lifecycle_state.value
        for stored in c12["store"].list_versions(str(c12["v6"].recipe.id))
    }
    assert set(versions) == {1, 2, 3, 4, 5, 6}
    assert versions[6] == "shadow"
    assert versions[5] == "shadow"
    assert versions[4] == "shadow"
    assert TERRA_PROFILE_VERSION_BY_RECIPE_VERSION == {
        HISTORICAL_RECIPE_VERSION + 2: 1,
        HISTORICAL_RECIPE_VERSION + 3: 2,
        HISTORICAL_RECIPE_VERSION + 4: 3,
        HISTORICAL_RECIPE_VERSION + 5: 4,
    }
    assert PLANNED_LIVE_PROVIDER_CALLS == 1
