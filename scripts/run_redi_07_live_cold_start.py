"""REDI-07B one-call live cold-start acceptance runner.

Default execution is the deterministic dry path.  A real provider call is
possible only with both ``--live`` and ``REDI_LIVE_PROVIDER=1``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import uuid
from pathlib import Path

from redi_lab.live_cold_start import (
    LIVE_MODEL,
    LIVE_PROVIDER,
    LIVE_WORKER_ID,
    PLANNED_LIVE_PROVIDER_CALLS,
    audit_live_cold_start,
    execute_live_cold_start,
    pre_execution_estimate,
)


DEFAULT_OUTPUT_ROOT = Path("artifacts/redi07b")


def _print_preflight(estimate: dict) -> None:
    print("REDI-07B PRE-EXECUTION ESTIMATE")
    print(f"planned_live_provider_calls={estimate['planned_live_provider_calls']}")
    print(f"live_node={estimate['live_node_id']}")
    print(f"context_bytes={estimate['context_bytes']}")
    print(f"estimated_input_tokens={estimate['estimated_input_tokens']}")
    print(f"max_output_tokens={estimate['max_output_tokens']}")
    print(f"expected_upper_bound_tokens={estimate['expected_upper_bound_tokens']}")
    print(f"context_package_hash={estimate['context_package_hash']}")
    print(f"rendered_hash={estimate['rendered_hash']}")
    print("source_list=" + json.dumps(estimate["source_list"], sort_keys=True))


def _print_report(outcome, audit: dict, *, live: bool) -> None:
    replay = audit["replay"]
    normalize = [
        item for item in replay["attempt_trajectory"]
        if item["node_id"] == "implement_normalize"
    ]
    weak = normalize[0] if normalize else {}
    fallback = normalize[1] if len(normalize) > 1 else {}
    candidate = replay.get("recipe_candidate")
    print("=" * 72)
    print("REDI-07B LIVE COLD-START EXPLORATION ACCEPTANCE")
    print("=" * 72)
    print(f"mode={'LIVE' if live else 'DRY-RUN (ZERO PROVIDER/NETWORK)'}")
    print(f"factory_run_id={outcome.factory_run_id}")
    print(f"ledger_path={outcome.db_path}")
    print(f"product_spec_id={outcome.product.id}")
    print(f"product_signature_sha256={outcome.signature.sha256}")
    print(f"cold_start_mode={outcome.selection.mode}")
    print(f"recipe_lookup_result={outcome.selection.recipe_lookup_result}")
    print(f"generated_dag_id={outcome.plan.dag.id}")
    print(f"generated_dag_revision={outcome.plan.dag.revision}")
    print(f"generated_dag_hash={outcome.execution.graph_hash}")
    print(f"weak_worker_a={weak.get('worker_id')}")
    print(f"weak_worker_a_failure_category={weak.get('failure_category')}")
    print(f"escalation_action={weak.get('failure_action')}")
    print(f"selected_live_worker_b={fallback.get('worker_id') or LIVE_WORKER_ID}")
    print(f"provider_model={LIVE_PROVIDER}:{LIVE_MODEL}")
    print(f"actual_provider_calls={audit['actual_provider_calls']}")
    print("actual_provider_tokens=" + json.dumps(audit["live_attempt"].get("tokens"), sort_keys=True))
    print("final_graph_states=" + json.dumps({key: value.value for key, value in outcome.execution.final_states.items()}, sort_keys=True))
    print(f"product_verified={outcome.verified}")
    print(f"recipe_candidate_id={candidate.get('recipe_id') if candidate else None}")
    print(f"recipe_candidate_hash={candidate.get('canonical_recipe_sha256') if candidate else None}")
    print(f"promotion_status={candidate.get('promotion_status') if candidate else None}")
    print(f"replay_x2_identical={audit['checks']['replay_x2_identical']}")
    print(f"replay_read_only={audit['checks']['replay_read_only']}")
    print(f"valid_failed_acceptance={audit['valid_failed_acceptance']}")
    print(f"audit_acceptance_valid={audit['acceptance_valid']}")
    print("audit_checks=" + json.dumps(audit["checks"], sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="deterministic fake live adapter; zero network (default)")
    mode.add_argument("--live", action="store_true", help="authorize the single configured provider slot")
    parser.add_argument("--audit", type=Path, help="read-only audit of an existing evidence.sqlite")
    parser.add_argument("--factory-run-id", help="FactoryRun id for --audit when the ledger is not singular")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    args = parser.parse_args(argv)

    if args.audit is not None:
        report = audit_live_cold_start(
            args.audit,
            factory_run_id=args.factory_run_id,
            expected_live=True,
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["acceptance_valid"] else 1

    live = bool(args.live)
    estimate = pre_execution_estimate()
    _print_preflight(estimate)
    if estimate["planned_live_provider_calls"] != PLANNED_LIVE_PROVIDER_CALLS or PLANNED_LIVE_PROVIDER_CALLS != 1:
        print("REDI-07B BLOCKED: provider-call plan is not exactly one; no provider call made.")
        return 2
    if live:
        if os.environ.get("REDI_LIVE_PROVIDER") != "1":
            print("REDI-07B LIVE BLOCKED: REDI_LIVE_PROVIDER=1 is required; no provider call made.")
            return 2
        if not os.environ.get("REDI_OPENAI_API_KEY"):
            print("REDI-07B LIVE BLOCKED: REDI_OPENAI_API_KEY is required; no provider call made.")
            return 2
        if importlib.util.find_spec("openai") is None:
            print("REDI-07B LIVE BLOCKED: openai SDK is unavailable; no provider call made.")
            return 2

    run_root = args.output_root / f"redi07b-{'live' if live else 'dry'}-{uuid.uuid4().hex[:12]}"
    outcome = execute_live_cold_start(live=live, output_root=run_root)
    audit = audit_live_cold_start(
        outcome.db_path,
        factory_run_id=outcome.factory_run_id,
        expected_live=live,
    )
    _print_report(outcome, audit, live=live)
    return 0 if audit["acceptance_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
