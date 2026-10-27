"""Exact REDI-08C14 Responses-strategy holdout rehearsal; provider disabled.

This is the exact future C14 live path minus the provider call: immutable
recipe v2..v6 lineage plus the execution-api-only Terra v7 (WorkerProfile v4:
Responses API, reasoning_effort low, 2048 max_output_tokens hard ceiling,
stateless single call), one governed execution slot, strict gates, replay x2.
The Responses result is a deterministic fake envelope that flows through the
SAME normalizer and structured-output pipeline as the live adapter, so zero
provider/network calls are made and nothing counts as transfer evidence.

--incomplete rehearses a status=incomplete / max_output_tokens Responses
result and proves the predeclared provider-incomplete failure mapping:
PROVIDER_OUTPUT_TRUNCATED_OR_INCOMPLETE, downstream gates NOT_RUN, and no
erroneous patch artifact.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from redi_lab.holdout_transfer import (
    ACCEPTED_PRODUCT_CLASS_ID,
    ACCEPTED_SOURCE_FACTORY_RUN_ID,
    ALTERNATE_GENUINE_HOLDOUT_FAILURE,
    ALTERNATE_GENUINE_WORKER_ID,
    EXECUTION_STRATEGY_REVALIDATION_POLICY_ID,
    GPT56_TERRA_WORKER_ID,
    GENUINE_HOLDOUT_FAILURE,
    audit_holdout_transfer,
    create_alternate_worker_v3,
    create_completion_budget_revalidation_v5,
    create_gpt56_terra_worker_v4,
    create_reasoning_revalidation_v6,
    create_responses_strategy_revalidation_v7,
    execute_holdout_warm_transfer,
    load_genuine_holdout_failure_record,
    load_owner_paired_002_record,
    prepare_holdout_recipe,
    prior_sibling_registry,
    replay_holdout,
    select_holdout_sibling,
    select_holdout_worker,
)
from redi_lab.recipe_store import RecipeStore
from redi_lab.warm_start import inventory_product_class


ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_LEDGER = ROOT / "artifacts" / "redi07b" / "redi07b-live-3962bb5a22bc" / "evidence.sqlite"
HISTORICAL_RUN = ACCEPTED_SOURCE_FACTORY_RUN_ID

INCOMPLETE_RESPONSES_RESULT = {
    "id": "resp_fake_rehearsal_incomplete_max_output_tokens",
    "status": "incomplete",
    "incomplete_details": {"reason": "max_output_tokens"},
    "error": None,
    "output": [],
    "usage": {
        "input_tokens": 538,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 2048,
        "output_tokens_details": {"reasoning_tokens": 2048},
        "total_tokens": 2586,
    },
}


def _revision() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
    ).strip()


def _default_output(incomplete: bool) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = "incomplete" if incomplete else "success"
    return ROOT / "build" / f"redi08c14-responses-{suffix}-{stamp}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--incomplete",
        action="store_true",
        help="rehearse an incomplete (max_output_tokens) Responses result",
    )
    parser.add_argument("--output-root", type=Path, default=None)
    args = parser.parse_args(argv)
    if not HISTORICAL_LEDGER.exists():
        raise SystemExit(f"accepted REDI-07 ledger missing: {HISTORICAL_LEDGER}")
    output = (args.output_root or _default_output(args.incomplete)).resolve()
    product_class = inventory_product_class(ACCEPTED_PRODUCT_CLASS_ID)
    owner = load_owner_paired_002_record()
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
            owner_paired_002_record=owner,
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
        stored = create_responses_strategy_revalidation_v7(
            store=store,
            stored_v6=stored,
            product_class=product_class,
        )
        selection = select_holdout_sibling(
            product_class=product_class,
            prior_used_registry=prior_sibling_registry(
                product_class_id=str(product_class.id),
                owner_paired_002_record=owner,
            ),
        )
        worker_selection = select_holdout_worker(stored=stored, owner_paired_002_record=owner)
        if worker_selection["selected_worker_id"] != GPT56_TERRA_WORKER_ID:
            raise RuntimeError("C14 rehearsal must select the pre-registered GPT-5.6 Terra WorkerProfile")
        if worker_selection["selected_worker_profile_version"] != 4:
            raise RuntimeError("C14 rehearsal must bind WorkerProfile v4 (Responses runtime)")
        runtime = worker_selection["provider_binding"]["runtime_request_configuration"]
        if runtime["api"] != "responses" or runtime["max_output_tokens"] != 2048 or runtime["reasoning_effort"] != "low":
            raise RuntimeError("C14 rehearsal must bind responses / low / 2048 exactly")
        print("REDI-08C14 RESPONSES EXECUTION-STRATEGY REHEARSAL")
        print("experimental_delta=execution_api chat_completions->responses (only change; effort low and 2048 ceiling unchanged)")
        print("acceptance_semantics=HARNESS_REHEARSAL_ONLY (not transfer evidence)")
        print(f"revalidation_policy={EXECUTION_STRATEGY_REVALIDATION_POLICY_ID}")
        print(f"mode=DRY_REHEARSAL scenario={'incomplete_max_output_tokens' if args.incomplete else 'completed_valid'}")
        print(f"holdout_sibling={selection.selected_candidate_key}")
        print(f"selected_worker={worker_selection['selected_worker_id']} profile_version=v4")
        print(f"pinned_recipe_version=v{stored.recipe.version} (parent v6, execution-api-only)")
        print(
            f"runtime=api:{runtime['api']} effort:{runtime['reasoning_effort']}"
            f" ceiling:{runtime['max_output_tokens']} ({runtime['max_output_parameter']},"
            " reasoning + visible output share the bound, stateless single call)"
        )
        outcome = execute_holdout_warm_transfer(
            output_root=output / "run",
            product_class=product_class,
            store=store,
            stored=stored,
            selection=selection,
            worker_selection=worker_selection,
            owner_paired_002_record=owner,
            source_revision=_revision(),
            live=False,
            fake_responses_result=INCOMPLETE_RESPONSES_RESULT if args.incomplete else None,
        )
    finally:
        store.close()
    result = json.loads(outcome.result_path.read_text(encoding="utf-8"))
    one = replay_holdout(db_path=outcome.db_path, factory_run_id=outcome.factory_run_id)
    two = replay_holdout(db_path=outcome.db_path, factory_run_id=outcome.factory_run_id)
    print("REDI-08C14 REHEARSAL SUMMARY")
    print(f"factory_run_id={outcome.factory_run_id}")
    print(f"recipe_semantic_hash={outcome.estimate['recipe_semantic_hash']}")
    print(f"responses_request_hash={result.get('responses_request_hash')}")
    print(f"PRODUCT_VERIFIED={str(outcome.verified).lower()}")
    print(f"gate_outcomes={json.dumps(dict(outcome.gate_outcomes), sort_keys=True)}")
    print(f"provider_calls={outcome.provider_calls} network_calls={outcome.network_calls}")
    print(f"failure_classification={result['failure_classification']}")
    print(f"provider_completion={json.dumps(result['provider_completion'], sort_keys=True)}")
    print(
        f"patch_retention={json.dumps({k: result['patch_retention'][k] for k in ('retained', 'retained_artifacts', 'retention_failures')}, sort_keys=True)}"
    )
    print(f"acceptance_semantics={result['acceptance_semantics']}")
    print(f"counts_as_transfer_evidence={str(result['counts_as_transfer_evidence']).lower()}")
    print(f"replay_x2_equal={str(one == two).lower()}")
    if args.incomplete:
        checks = {
            "typed_provider_incomplete_classification": (
                result["failure_classification"] == "PROVIDER_OUTPUT_TRUNCATED_OR_INCOMPLETE"
            ),
            "no_schema_failure_classification": (
                result["failure_classification"] != "STRUCTURED_OUTPUT_SCHEMA_FAILURE"
            ),
            "structured_gate_failed_with_parse_reason": (
                one["gate_records"][0]["gate_identity"] == "structured_output_schema"
                and one["gate_records"][0]["status"] == "FAIL"
                and "JSONDecodeError" in str(one["gate_records"][0]["reason"])
            ),
            "downstream_gates_not_run": all(
                gate["status"] == "NOT_RUN"
                for gate in one["gate_records"]
                if gate["gate_identity"] != "structured_output_schema"
            ),
            "no_erroneous_patch_artifact": (
                result["patch_retention"]["retained"] is None
                and result["patch_retention"]["retained_artifacts"] == []
                and result["patch_retention"]["retention_failures"] == []
            ),
            "zero_provider_network": outcome.provider_calls == 0 and outcome.network_calls == 0,
            "replay_x2_stable": one == two,
        }
        print(f"incomplete_rehearsal_checks={json.dumps(checks, sort_keys=True)}")
        return 0 if all(checks.values()) else 1
    audit = dict(outcome.audit) | audit_holdout_transfer(outcome=outcome, strict_identity=True)
    failed = [key for key, value in audit.items() if not value]
    print(f"self_audit={json.dumps(audit, sort_keys=True)}")
    print(f"self_audit_failures={json.dumps(failed)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
