"""Exact REDI-08B paired runner; provider disabled unless env + --live agree."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from redi_lab.paired_experiment import (
    CONDITION_COLD,
    CONDITION_WARM,
    execute_paired_experiment,
    prepare_shadow_recipe,
    replay_condition,
    replay_pair,
)
from redi_lab.recipe_store import RecipeStore
from redi_lab.warm_start import inventory_product_class, sibling_product_fixture


ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_LEDGER = ROOT / "artifacts" / "redi07b" / "redi07b-live-3962bb5a22bc" / "evidence.sqlite"
HISTORICAL_RUN = "factory_run_7f21dd99f6b94664be1434e4f6803393"


def _revision() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
    ).strip()


def _default_output() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return ROOT / "build" / f"redi08b-paired-{stamp}"


def _print_estimate(estimate: dict) -> None:
    print("REDI-08B PRE-EXECUTION ESTIMATE")
    print(f"planned_live_calls[cold]={estimate['planned_live_calls'][CONDITION_COLD]}")
    print(f"planned_live_calls[warm]={estimate['planned_live_calls'][CONDITION_WARM]}")
    print(f"planned_live_calls_total={estimate['planned_live_calls_total']}")
    for condition in (CONDITION_COLD, CONDITION_WARM):
        slot = estimate["live_slots"][condition]
        print(
            f"live_slot[{condition}]=provider:{slot['provider']} model:{slot['model']} "
            f"estimated_input_tokens:{slot['estimated_input_tokens']} "
            f"max_output_tokens:{slot['max_output_tokens']}"
        )
    print(f"total_expected_token_upper_bound={estimate['expected_token_upper_bound']}")
    print("automatic_paid_retry=false")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="enable the two preregistered provider slots")
    parser.add_argument("--output-root", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.live and os.environ.get("REDI_LIVE_PROVIDER") != "1":
        parser.error("--live requires REDI_LIVE_PROVIDER=1")
    if not HISTORICAL_LEDGER.exists():
        raise SystemExit(f"accepted REDI-07 ledger missing: {HISTORICAL_LEDGER}")
    output = (args.output_root or _default_output()).resolve()
    product_class = inventory_product_class("product_class_cold_5234225c641e70fba9077fb7")
    product = sibling_product_fixture(str(product_class.id))
    output.mkdir(parents=True, exist_ok=True)
    store = RecipeStore(output / "recipe_store.sqlite")
    try:
        stored = prepare_shadow_recipe(
            store=store,
            source_ledger=HISTORICAL_LEDGER,
            source_factory_run_id=HISTORICAL_RUN,
            product_class=product_class,
        )
        pair_root = output / "pair"
        outcome = execute_paired_experiment(
            output_root=pair_root,
            product=product,
            product_class=product_class,
            store=store,
            recipe_id=str(stored.recipe.id),
            recipe_version=stored.recipe.version,
            source_revision=_revision(),
            live=args.live,
            preregistered_callback=lambda _manifest, estimate: _print_estimate(dict(estimate)),
        )
    finally:
        store.close()
    pair = outcome.paired_result
    cold_replay = replay_condition(db_path=outcome.cold.db_path, factory_run_id=outcome.cold.factory_run_id)
    warm_replay = replay_condition(db_path=outcome.warm.db_path, factory_run_id=outcome.warm.factory_run_id)
    pair_replay_one = replay_pair(outcome.paired_result_path)
    pair_replay_two = replay_pair(outcome.paired_result_path)
    print("REDI-08B PAIRED SUMMARY")
    print(f"mode={'LIVE' if args.live else 'DRY'}")
    print(f"experiment_id={pair['experiment_id']}")
    print(f"manifest_hash={pair['manifest_hash']}")
    print(f"condition_order={json.dumps(pair['condition_order'])}")
    print(f"sibling_product_signature={pair['sibling_product_spec_hash']}")
    print(f"cold_factory_run_id={outcome.cold.factory_run_id}")
    print(f"warm_factory_run_id={outcome.warm.factory_run_id}")
    recipe = pair["recipe"]
    print(f"recipe_id/version/hash={recipe['recipe_id']}/{recipe['recipe_version']}/{recipe['canonical_hash']}")
    print(f"cold_dag_hash={outcome.cold.dag_hash}")
    print(f"warm_dag_hash={outcome.warm.dag_hash}")
    _print_estimate(pair["planned_live_budget"])
    for condition, replay in (("cold", cold_replay), ("warm", warm_replay)):
        metrics = replay["metrics"]
        print(f"condition[{condition}].final_state={'PRODUCT_VERIFIED' if replay['verified'] else 'FAILED'}")
        print(f"condition[{condition}].PRODUCT_VERIFIED={str(replay['verified']).lower()}")
        print(f"condition[{condition}].provider_usage={json.dumps(metrics['provider_usage'], sort_keys=True)}")
        print(f"condition[{condition}].worker_attempts={metrics['orchestration']['worker_attempts']}")
        print(f"condition[{condition}].failures_escalations={metrics['orchestration']['failed_attempts']}/{metrics['orchestration']['escalations']}")
        print(f"condition[{condition}].graph_revisions={metrics['orchestration']['graph_revisions']}")
        print(f"condition[{condition}].wall_clock={metrics['time']['graph_wall_clock_seconds']}")
        print(f"condition[{condition}].aggregate_compute={metrics['time']['aggregate_worker_compute_seconds']}")
        print(f"condition[{condition}].context_totals={json.dumps(metrics['context'], sort_keys=True)}")
        print(f"condition[{condition}].human_interventions={metrics['human']['interventions']}")
    print(f"pair_classification={pair['classification']}")
    print(f"replay_equality={str(pair_replay_one == pair_replay_two).lower()}")
    print(f"self_audit={json.dumps(outcome.audit, sort_keys=True)}")
    print(f"zero_network_provider_proof={str(not args.live and outcome.cold.provider_calls == outcome.warm.provider_calls == outcome.cold.network_calls == outcome.warm.network_calls == 0).lower()}")
    print(f"paired_result={outcome.paired_result_path}")
    return 0 if all(outcome.audit.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
