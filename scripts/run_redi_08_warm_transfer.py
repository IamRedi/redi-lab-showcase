"""Exact REDI-08C12 holdout warm-transfer runner; provider disabled unless env + --live agree.

This is the exact future C12 live path: predeclared holdout selection, immutable
recipe v2/v3/v4/v5 lineage and the reasoning-config-only Terra v6 (WorkerProfile
v3: reasoning_effort low at the unchanged 2048 hard completion ceiling), one
governed execution slot, strict gates, replay x2.  Dry mode (default) performs
zero provider/network calls.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from redi_lab.holdout_transfer import (
    ACCEPTED_PRODUCT_CLASS_ID,
    ACCEPTED_SOURCE_FACTORY_RUN_ID,
    ALTERNATE_GENUINE_WORKER_ID,
    ALTERNATE_GENUINE_HOLDOUT_FAILURE,
    GPT56_TERRA_WORKER_ID,
    GENUINE_HOLDOUT_FAILURE,
    create_alternate_worker_v3,
    create_completion_budget_revalidation_v5,
    create_gpt56_terra_worker_v4,
    create_reasoning_revalidation_v6,
    load_genuine_holdout_failure_record,
    audit_holdout_transfer,
    execute_holdout_warm_transfer,
    load_owner_paired_002_record,
    prior_sibling_registry,
    prepare_holdout_recipe,
    select_holdout_sibling,
    select_holdout_worker,
)
from redi_lab.recipe_store import RecipeStore
from redi_lab.warm_start import inventory_product_class


ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_LEDGER = ROOT / "artifacts" / "redi07b" / "redi07b-live-3962bb5a22bc" / "evidence.sqlite"
HISTORICAL_RUN = ACCEPTED_SOURCE_FACTORY_RUN_ID


def _revision() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
    ).strip()


def _default_output() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return ROOT / "build" / f"redi08c-holdout-{stamp}"


def _print_estimate(estimate: dict) -> None:
    print("REDI-08C12 PRE-EXECUTION ESTIMATE")
    print(f"holdout_product={estimate['holdout_product_id']}/{estimate['holdout_product_spec_sha256']}")
    print(f"product_class_signature_hash={estimate['product_class_signature_hash']}")
    print(
        f"recipe={estimate['recipe_id']}/v{estimate['recipe_version']}"
        f"/{estimate['recipe_semantic_hash']}"
    )
    print(f"lifecycle={estimate['lifecycle']}")
    print(f"confidence_state={json.dumps(estimate['confidence_state'], sort_keys=True)}")
    print(f"worker_profile={json.dumps(estimate['worker_profile'], sort_keys=True)}")
    print(f"worker_profile_version={estimate['worker_profile']['version']}")
    print(f"worker_runtime={json.dumps(estimate['worker_runtime'], sort_keys=True)}")
    print(f"reasoning_effort={estimate['worker_runtime']['reasoning_effort']}")
    print(
        "provider_request_configuration="
        f"{json.dumps(estimate['provider_request_configuration'], sort_keys=True)}"
    )
    print(f"provider={estimate['provider']} model={estimate['model']}")
    print(f"planned_live_provider_calls_max={estimate['planned_live_provider_calls']}")
    print(f"planned_provider_calls={estimate['expected_provider_calls_for_selected_worker']}")
    print(f"api_key_required={'true' if estimate['expected_provider_calls_for_selected_worker'] else 'false'}")
    print(f"expected_provider_calls_for_selected_worker={estimate['expected_provider_calls_for_selected_worker']}")
    print(f"context_bytes={estimate['context_bytes']}")
    print(f"rendered_request_bytes={estimate['rendered_request_bytes']}")
    print(f"estimated_input_tokens={estimate['estimated_input_tokens']}")
    contract = estimate.get("builder_output_contract") or {}
    print(
        f"output_contract_policy={contract.get('policy_id')}/v{contract.get('policy_version')}"
        f"/{contract.get('canonical_hash')}"
    )
    print(f"max_completion_tokens_hard_ceiling={estimate['max_output_tokens']} (hard completion ceiling, not expected usage)")
    print(f"total_token_upper_bound={estimate['total_token_upper_bound']}")
    print(f"gate_policy={json.dumps(estimate['gate_policy'], sort_keys=True)}")
    print(f"automatic_retry={'false' if estimate['no_paid_retry'] else 'true'} (one-call policy; on failure persist and STOP)")
    print(f"no_paid_retry={str(estimate['no_paid_retry']).lower()}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "execute the real owner run. When the recipe-ladder worker binds a "
            "provider this is a paid provider run and requires "
            "REDI_LIVE_PROVIDER=1; when the selected worker is local "
            "deterministic this is a real local warm-start execution with "
            "zero provider calls and no API key."
        ),
    )
    parser.add_argument("--output-root", type=Path, default=None)
    args = parser.parse_args(argv)
    if not HISTORICAL_LEDGER.exists():
        raise SystemExit(f"accepted REDI-07 ledger missing: {HISTORICAL_LEDGER}")
    output = (args.output_root or _default_output()).resolve()
    product_class = inventory_product_class(ACCEPTED_PRODUCT_CLASS_ID)
    owner_paired_002 = load_owner_paired_002_record()
    output.mkdir(parents=True, exist_ok=True)
    store = RecipeStore(output / "recipe_store.sqlite")
    try:
        stored_v2 = prepare_holdout_recipe(
            store=store,
            source_ledger=HISTORICAL_LEDGER,
            source_factory_run_id=HISTORICAL_RUN,
            product_class=product_class,
        )
        genuine_failure = load_genuine_holdout_failure_record(ROOT / "build")
        stored_v3 = create_alternate_worker_v3(
            store=store,
            stored_v2=stored_v2,
            product_class=product_class,
            owner_paired_002_record=owner_paired_002,
            genuine_failure_record=genuine_failure,
            alternate_worker_id=ALTERNATE_GENUINE_WORKER_ID,
        )
        stored = create_gpt56_terra_worker_v4(
            store=store,
            stored_v3=stored_v3,
            product_class=product_class,
            gpt41_failure_record=genuine_failure,
            gpt4o_mini_failure_record=ALTERNATE_GENUINE_HOLDOUT_FAILURE,
        )
        stored = create_completion_budget_revalidation_v5(
            store=store,
            stored_v4=stored,
            product_class=product_class,
        )
        stored = create_reasoning_revalidation_v6(
            store=store,
            stored_v5=stored,
            product_class=product_class,
        )
        selection = select_holdout_sibling(
            product_class=product_class,
            prior_used_registry=prior_sibling_registry(
                product_class_id=str(product_class.id),
                owner_paired_002_record=owner_paired_002,
            ),
        )
        worker_selection = select_holdout_worker(
            stored=stored,
            owner_paired_002_record=owner_paired_002,
            exclude_worker_ids={
                GENUINE_HOLDOUT_FAILURE["failed_worker"]: (
                    "excluded for this revalidation: already failed this exact "
                    "holdout (REDI-08C3 governed genuine failure)"
                )
            },
            exclude_models={
                GENUINE_HOLDOUT_FAILURE["model"]: (
                    "excluded for this revalidation: same model already failed this "
                    "holdout; the attempt must change worker identity"
                )
            },
        )
        if worker_selection["selected_worker_id"] != GPT56_TERRA_WORKER_ID:
            raise RuntimeError("C12 runner must select the pre-registered GPT-5.6 Terra WorkerProfile")
        provider_bound = (
            worker_selection["selected_worker_profile"].get("provider") is not None
        )
        if args.live and not provider_bound:
            parser.error(
                "--live requires a genuine provider-backed execution worker; the "
                "recipe-ladder selection is not provider-backed, so a real run "
                "cannot be executed locally without oracle dependency"
            )
        if args.live and provider_bound and os.environ.get("REDI_LIVE_PROVIDER") != "1":
            parser.error("--live with a provider-bound worker requires REDI_LIVE_PROVIDER=1")
        print("REDI-08C12 HOLDOUT WARM TRANSFER")
        print("experimental_delta=reasoning_effort medium->low (only change; completion ceiling stays 2048)")
        print(
            "acceptance_semantics=GENUINE_WORKER_TRANSFER_ACCEPTANCE"
            if args.live
            else "acceptance_semantics=HARNESS_REHEARSAL_ONLY (not transfer evidence)"
        )
        print(f"selected_worker_class={worker_selection['selected_worker_classification']['classification']}")
        print(f"mode={'LIVE' if args.live else 'DRY'}")
        print(f"holdout_sibling={selection.selected_candidate_key}")
        print(f"holdout_product={selection.definition.product.id}")
        print(f"selected_worker={worker_selection['selected_worker_id']}")
        print(f"worker_profile_version=v{worker_selection['selected_worker_profile_version']}")
        print(f"pinned_recipe_version=v{stored.recipe.version} (parent v5, reasoning-config-only)")
        print(f"alternate_model={worker_selection['provider_binding']['model']}")
        runtime = worker_selection["provider_binding"]["runtime_request_configuration"]
        print(
            f"completion_ceiling={runtime['max_output_tokens']}"
            " (max_completion_tokens hard ceiling, unchanged from C10)"
        )
        print(f"reasoning_effort={runtime['reasoning_effort']} (delta: medium -> low)")
        outcome = execute_holdout_warm_transfer(
            output_root=output / "run",
            product_class=product_class,
            store=store,
            stored=stored,
            selection=selection,
            worker_selection=worker_selection,
            owner_paired_002_record=owner_paired_002,
            source_revision=_revision(),
            live=args.live,
            estimate_callback=lambda estimate: _print_estimate(dict(estimate)),
        )
    finally:
        store.close()
    result = json.loads(outcome.result_path.read_text(encoding="utf-8"))
    print("REDI-08C12 HOLDOUT SUMMARY")
    print(f"factory_run_id={outcome.factory_run_id}")
    print(f"recipe_id/version={stored.recipe.id}/v{stored.recipe.version} (parent v5, reasoning-config-only)")
    print(f"worker_profile_version=v{worker_selection['selected_worker_profile_version']}")
    print(f"recipe_semantic_hash={outcome.estimate['recipe_semantic_hash']}")
    print(f"lifecycle={stored.lifecycle_state.value} validity={stored.revalidation_state}")
    print(f"revalidation_decision={outcome.revalidation_audit['decision']}")
    print(f"selected_worker={worker_selection['selected_worker_id']}")
    print(f"recipe_derived_dag={str(result['dag_metadata']['source_recipe_version'] == result['recipe']['recipe_version']).lower()}")
    print(f"dag_hash={outcome.dag_hash}")
    print(f"fixture_tree_hash={outcome.fixture_hash}")
    print(f"PRODUCT_VERIFIED={str(outcome.verified).lower()}")
    print(f"gate_outcomes={json.dumps(dict(outcome.gate_outcomes), sort_keys=True)}")
    print(f"provider_calls={outcome.provider_calls} network_calls={outcome.network_calls}")
    print(f"planned_provider_calls_max=1 (hard ceiling; automatic_paid_retry=false; on failure STOP)")
    print(f"acceptance_semantics={result['acceptance_semantics']}")
    print(f"counts_as_transfer_evidence={str(result['counts_as_transfer_evidence']).lower()}")
    print(f"implementation_source={result['implementation_source']}")
    print(f"worker_capability_transfer_claimed={str(result['claims']['worker_capability_transfer_claimed']).lower()}")
    print(f"recipe_promoted=false lifecycle_stays_shadow=true")
    print(f"owner_paired_002_preserved={result['owner_paired_002_evidence']['pair_id']}")
    print(f"replay_refs={json.dumps({'db_path': str(outcome.db_path), 'factory_run_id': outcome.factory_run_id}, sort_keys=True)}")
    print(f"holdout_result={outcome.result_path}")
    audit = dict(outcome.audit) | audit_holdout_transfer(outcome=outcome, strict_identity=True)
    failed = [key for key, value in audit.items() if not value]
    print(f"self_audit={json.dumps(audit, sort_keys=True)}")
    print(f"self_audit_failures={json.dumps(failed)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
