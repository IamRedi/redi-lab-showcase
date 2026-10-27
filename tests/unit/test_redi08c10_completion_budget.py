"""REDI-08C10 completion-budget revalidation regressions.

The ONLY material execution change authorized by C10 is the Terra hard
completion ceiling 1024 -> 2048 on a new immutable WorkerProfile version,
bound to a new SHADOW recipe version.  Everything else — provider, model,
reasoning effort, holdout, task context, output contract, gates, one-call
policy — must remain byte-frozen, and the historical truncated run must keep
reconstructing exactly 1024.
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
    COMPLETION_BUDGET_REVALIDATION_POLICY_ID,
    FROZEN_BUILDER_RENDERED_HASH,
    FROZEN_FIXTURE_TREE_HASH,
    GPT56_TERRA_WORKER_ID,
    GPT56_TERRA_WORKER_VERSION,
    GENUINE_HOLDOUT_FAILURE,
    HISTORICAL_RECIPE_VERSION,
    HISTORICAL_TERRA_TRUNCATED_RUN,
    PLANNED_LIVE_PROVIDER_CALLS,
    PREDECLARED_HOLDOUT_PRODUCT_SHA256,
    TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION,
    TERRA_PROFILE_VERSION_BY_RECIPE_VERSION,
    audit_holdout_transfer,
    create_alternate_worker_v3,
    create_completion_budget_revalidation_v5,
    create_gpt56_terra_worker_v4,
    execute_holdout_warm_transfer,
    gpt56_terra_holdout_worker_profile,
    load_genuine_holdout_failure_record,
    load_owner_paired_002_record,
    prepare_holdout_recipe,
    prior_sibling_registry,
    replay_holdout,
    replay_holdout_result,
    select_holdout_sibling,
    select_holdout_worker,
    HoldoutError,
)
from redi_lab.paired_experiment import MAX_OUTPUT_TOKENS
from redi_lab.recipe_store import RecipeStore, recipe_semantic_payload
from redi_lab.warm_start import inventory_product_class


ROOT = Path(__file__).resolve().parents[2]
HISTORICAL_LEDGER = ROOT / "artifacts" / "redi07b" / "redi07b-live-3962bb5a22bc" / "evidence.sqlite"
C10_HISTORICAL_RUN = ROOT / "build" / "redi08c8-owner-terra-20260921T164206Z"
C10_HISTORICAL_RUN_ID = HISTORICAL_TERRA_TRUNCATED_RUN["factory_run_id"]


@pytest.fixture(scope="module")
def c10(tmp_path_factory):
    """Recipe lineage v2 -> v3 -> v4 -> v5 plus both frozen selections."""
    work = tmp_path_factory.mktemp("redi08c10")
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
        "selection": selection,
    }


def test_terra_worker_profile_versions_are_immutable_and_minimally_different():
    v1 = gpt56_terra_holdout_worker_profile(version=1)
    v2 = gpt56_terra_holdout_worker_profile(version=2)
    assert TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION == {
        1: 1024,
        2: 2048,
        3: 2048,
        4: 2048,
    }
    assert MAX_OUTPUT_TOKENS == 1024
    # v1 is the byte-exact REDI-08C6 authorization.
    assert v1["version"] == 1
    assert v1["runtime"]["max_output_tokens"] == 1024
    assert v1["runtime"]["max_output_parameter"] == "max_completion_tokens"
    # The canonical registry entry follows the newest frozen revalidation
    # (v3 since REDI-08C12); v2 remains frozen at 2048/medium.
    assert GPT56_TERRA_WORKER_VERSION == 3
    assert gpt56_terra_holdout_worker_profile()["version"] == 3
    assert v2["version"] == 2
    assert v2["runtime"]["reasoning_effort"] == "medium"
    # The ONLY difference between v1 and v2 is version + max_output_tokens.
    assert {k for k in v1 if v1[k] != v2[k]} == {"version", "runtime"}
    assert {k for k in v1["runtime"] if v1["runtime"][k] != v2["runtime"][k]} == {
        "max_output_tokens"
    }
    assert v2["runtime"]["max_output_tokens"] == 2048
    for profile in (v1, v2):
        assert profile["id"] == GPT56_TERRA_WORKER_ID
        assert profile["model"] == "gpt-5.6-terra"
        assert profile["provider"] == "openai"
        assert profile["runtime"]["reasoning_effort"] == "medium"
        assert profile["runtime"]["api"] == "chat_completions"
        assert profile["runtime"]["response_format"] == "json_schema_strict"
        assert profile["runtime"]["temperature"] is None
        assert profile["runtime"]["top_p"] is None
    # Version 4 exists since REDI-08C14 (execution-api-only revalidation);
    # unknown versions still fail closed.
    with pytest.raises(HoldoutError):
        gpt56_terra_holdout_worker_profile(version=5)


def test_recipe_v5_is_budget_only_shadow_child_of_v4(c10):
    v4, v5 = c10["v4"], c10["v5"]
    assert v4.recipe.version == HISTORICAL_RECIPE_VERSION + 2
    assert v5.recipe.version == HISTORICAL_RECIPE_VERSION + 3
    assert str(v5.recipe.id) == str(v4.recipe.id)
    assert v5.lifecycle_state.value == "shadow"
    assert v5.revalidation_state == "VALID_FOR_CONTROLLED_EXPERIMENT"
    assert v5.provenance["parent_recipe_version"] == v4.recipe.version
    # The worker tuple is unchanged: same single Terra worker, no new worker.
    assert v5.recipe.worker_preferences == v4.recipe.worker_preferences
    assert v5.recipe.worker_fallbacks == v4.recipe.worker_fallbacks
    # The ONLY semantic delta vs v4 is the completion-budget reference.
    payload_v4 = recipe_semantic_payload(v4.recipe)
    payload_v5 = recipe_semantic_payload(v5.recipe)
    assert payload_v5.pop("version") == payload_v4.pop("version") + 1
    assert set(payload_v5) == set(payload_v4)
    assert set(payload_v5["metadata"]) - set(payload_v4["metadata"]) == {
        "completion_budget_revalidation"
    }
    for key, value in payload_v4["metadata"].items():
        assert payload_v5["metadata"][key] == value
    added_metadata = payload_v5["metadata"]["completion_budget_revalidation"]
    assert added_metadata == {
        "policy": "REDI-08C10",
        "parameter": "max_completion_tokens",
        "prior_tokens": 1024,
        "tokens": 2048,
        "hard_ceiling_not_expected_usage": True,
        "worker_profile_id": GPT56_TERRA_WORKER_ID,
        "worker_profile_version": 2,
        "reasoning_effort_unchanged": "medium",
    }
    # Provenance records the C9-audited truncated run and no-capability-claim.
    evidence = v5.provenance["revalidation_evidence"]
    assert evidence["historical_terra_truncated_run"] == HISTORICAL_TERRA_TRUNCATED_RUN
    assert HISTORICAL_TERRA_TRUNCATED_RUN["finish_reason"] == "length"
    assert HISTORICAL_TERRA_TRUNCATED_RUN["incomplete"] is True
    assert HISTORICAL_TERRA_TRUNCATED_RUN["reasoning_tokens"] == 1024
    assert HISTORICAL_TERRA_TRUNCATED_RUN["visible_answer_content"] == "none"
    assert evidence["budget_audit_verdict"] == "COMPLETION_BUDGET_REMEDIATION_REQUIRED"
    assert "better" in evidence["no_capability_claim"]
    assert evidence["automatic_paid_retry"] is False
    # Registration metadata pins the exact profile version and budget.
    assert v5.registration_metadata["revision_kind"] == "completion_budget_only"
    assert v5.registration_metadata["authorized_worker_profile_version"] == 2
    assert v5.registration_metadata["authorized_completion_budget"] == 2048
    assert v5.registration_metadata["automatic_paid_retry"] is False


def test_selection_resolves_profile_version_by_recipe_version(c10):
    owner = c10["owner"]
    v4_selection = select_holdout_worker(
        stored=c10["v4"], owner_paired_002_record=owner
    )
    assert v4_selection["selected_worker_profile_version"] == 1
    assert (
        v4_selection["selected_worker_profile"]["runtime"]["max_output_tokens"] == 1024
    )
    v5_selection = select_holdout_worker(
        stored=c10["v5"], owner_paired_002_record=owner
    )
    assert v5_selection["selected_worker_id"] == GPT56_TERRA_WORKER_ID
    assert v5_selection["selected_worker_profile_version"] == 2
    runtime = v5_selection["selected_worker_profile"]["runtime"]
    assert runtime["max_output_tokens"] == 2048
    assert runtime["reasoning_effort"] == "medium"
    assert runtime["max_output_parameter"] == "max_completion_tokens"
    assert runtime["response_format"] == "json_schema_strict"
    assert runtime["temperature"] is None and runtime["top_p"] is None


def test_unmapped_recipe_version_blocks_silent_budget_drift(c10):
    drifted = SimpleNamespace(
        recipe=replace(c10["v5"].recipe, version=HISTORICAL_RECIPE_VERSION + 6)
    )
    with pytest.raises(HoldoutError, match="completion-budget drift"):
        select_holdout_worker(stored=drifted, owner_paired_002_record=c10["owner"])


def _run_fake(c10, stored, selection_worker, name):
    return execute_holdout_warm_transfer(
        output_root=c10["work"] / name,
        product_class=c10["product_class"],
        store=c10["store"],
        stored=stored,
        selection=c10["selection"],
        worker_selection=selection_worker,
        owner_paired_002_record=c10["owner"],
        source_revision="redi08c10-unit",
        live=False,
    )


def test_v4_dry_rehearsal_still_resolves_the_frozen_1024_budget(c10):
    selection = select_holdout_worker(stored=c10["v4"], owner_paired_002_record=c10["owner"])
    outcome = _run_fake(c10, c10["v4"], selection, "v4_rehearsal")
    snapshot = replay_holdout(db_path=outcome.db_path, factory_run_id=outcome.factory_run_id)
    assert snapshot["recipe"]["recipe_version"] == HISTORICAL_RECIPE_VERSION + 2
    assert snapshot["pre_transfer_estimate"]["max_output_tokens"] == 1024
    assert snapshot["worker_selection"]["selected_worker_profile_version"] == 1
    assert snapshot["revalidation_decision"]["decision"] == "V4_TERRA_WORKER_POLICY_REVALIDATION_PINNED"
    assert outcome.verified is True and outcome.provider_calls == outcome.network_calls == 0
    audit = audit_holdout_transfer(outcome=outcome, strict_identity=True)
    assert all(audit.values()), {k: v for k, v in audit.items() if not v}


def test_v5_dry_rehearsal_is_the_exact_future_path(c10):
    selection = select_holdout_worker(stored=c10["v5"], owner_paired_002_record=c10["owner"])
    outcome = _run_fake(c10, c10["v5"], selection, "v5_rehearsal")
    one = replay_holdout(db_path=outcome.db_path, factory_run_id=outcome.factory_run_id)
    two = replay_holdout(db_path=outcome.db_path, factory_run_id=outcome.factory_run_id)
    # Zero provider/network; deterministic replay x2.
    assert outcome.provider_calls == outcome.network_calls == 0
    assert one == two
    # Fake PRODUCT_VERIFIED is rehearsal only and never transfer evidence.
    assert outcome.verified is True
    assert one["acceptance_semantics"] == "HARNESS_REHEARSAL_ONLY"
    assert one["counts_as_transfer_evidence"] is False
    # Holdout + task-context freeze.
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
    assert contract["expected_edit_count"] == 1
    assert contract["expected_path"] == "depot_replenishment/normalize.py"
    # Budget-only config: 2048 ceiling, reasoning medium, one call, no retry.
    estimate = one["pre_transfer_estimate"]
    assert estimate["max_output_tokens"] == 2048
    assert estimate["total_token_upper_bound"] == (
        estimate["estimated_input_tokens"] + 2048
    )
    assert estimate["worker_runtime"]["reasoning_effort"] == "medium"
    assert estimate["planned_live_provider_calls"] == PLANNED_LIVE_PROVIDER_CALLS == 1
    assert estimate["no_paid_retry"] is True
    assert estimate["worker_profile"]["version"] == 2
    assert one["recipe"]["recipe_version"] == HISTORICAL_RECIPE_VERSION + 3
    assert one["recipe"]["metadata"]["completion_budget_revalidation"]["tokens"] == 2048
    assert one["revalidation_decision"]["decision"] == "V5_COMPLETION_BUDGET_REVALIDATION_PINNED"
    # Governed worker attempt + patch retention bound to the attempt.
    assert one["worker_attempts"][0]["worker_id"] == GPT56_TERRA_WORKER_ID
    assert one["patch_retention"]["retained"] is not None
    # All nine gates PASS on the rehearsal.
    assert len(outcome.gate_outcomes) == 9
    assert all(state == "PASS" for state in outcome.gate_outcomes.values())
    # The strict self-audit (exact future live gate check) is fully green.
    audit = audit_holdout_transfer(outcome=outcome, strict_identity=True)
    assert all(audit.values()), {k: v for k, v in audit.items() if not v}


@pytest.mark.skipif(
    not (C10_HISTORICAL_RUN / "run" / "holdout" / "evidence.sqlite").exists(),
    reason="historical C8 Terra run artifact not present on this machine",
)
def test_historical_c8_terra_run_replays_at_1024_forever():
    snapshot = replay_holdout(
        db_path=C10_HISTORICAL_RUN / "run" / "holdout" / "evidence.sqlite",
        factory_run_id=C10_HISTORICAL_RUN_ID,
    )
    assert snapshot["recipe"]["recipe_version"] == HISTORICAL_RECIPE_VERSION + 2
    assert snapshot["pre_transfer_estimate"]["max_output_tokens"] == 1024
    assert (
        snapshot["worker_selection"]["selected_worker_profile"]["runtime"][
            "max_output_tokens"
        ]
        == 1024
    )
    assert snapshot["provider_completion"]["finish_reason"] == "length"
    assert snapshot["provider_completion"]["incomplete"] is True
    assert snapshot["provider_completion"]["completion_token_details"]["reasoning_tokens"] == 1024
    result = replay_holdout_result(C10_HISTORICAL_RUN / "run" / "holdout_result.json")
    assert result["factory_run_id"] == C10_HISTORICAL_RUN_ID
    assert result["provider_completion"]["finish_reason"] == "length"
    assert result["failure_classification"] == "PROVIDER_OUTPUT_TRUNCATED_OR_INCOMPLETE"


@pytest.mark.skipif(
    not (C10_HISTORICAL_RUN / "run" / "holdout" / "evidence.sqlite").exists(),
    reason="historical C8 Terra run artifact not present on this machine",
)
def test_replay_is_independent_of_current_registry_defaults(c10, monkeypatch):
    import redi_lab.holdout_transfer as ht

    # Execute the fresh v5 rehearsal BEFORE corrupting the live constants.
    selection = select_holdout_worker(stored=c10["v5"], owner_paired_002_record=c10["owner"])
    outcome = _run_fake(c10, c10["v5"], selection, "v5_replay_independence")
    monkeypatch.setattr(ht, "TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION", {1: 7777, 2: 7777})
    monkeypatch.setattr(ht, "TERRA_PROFILE_VERSION_BY_RECIPE_VERSION", {})
    # Historical replay reconstructs 1024.
    historical = replay_holdout(
        db_path=C10_HISTORICAL_RUN / "run" / "holdout" / "evidence.sqlite",
        factory_run_id=C10_HISTORICAL_RUN_ID,
    )
    assert historical["pre_transfer_estimate"]["max_output_tokens"] == 1024
    # Fresh v5 replay reconstructs 2048 from its own persisted evidence.
    replayed = replay_holdout(db_path=outcome.db_path, factory_run_id=outcome.factory_run_id)
    assert replayed["pre_transfer_estimate"]["max_output_tokens"] == 2048
    assert (
        replayed["worker_selection"]["selected_worker_profile"]["runtime"][
            "max_output_tokens"
        ]
        == 2048
    )


def test_recipe_store_keeps_full_version_lineage_without_promotion(c10):
    versions = {
        stored.recipe.version: stored.lifecycle_state.value
        for stored in c10["store"].list_versions(str(c10["v5"].recipe.id))
    }
    assert set(versions) == {1, 2, 3, 4, 5}
    assert versions[5] == "shadow"
    assert versions[4] == "shadow"
    assert c10["v5"].provenance["revalidation_policy_id"] == COMPLETION_BUDGET_REVALIDATION_POLICY_ID
    assert TERRA_PROFILE_VERSION_BY_RECIPE_VERSION == {
        HISTORICAL_RECIPE_VERSION + 2: 1,
        HISTORICAL_RECIPE_VERSION + 3: 2,
        HISTORICAL_RECIPE_VERSION + 4: 3,
        HISTORICAL_RECIPE_VERSION + 5: 4,
    }
