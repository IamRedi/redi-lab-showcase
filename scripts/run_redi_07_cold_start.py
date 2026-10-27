"""Offline controlled REDI-07A cold-start proof runner."""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

from redi_lab.cold_start import replay_cold_start, run_controlled_cold_start


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=Path("build/redi07a-dry"))
    args = parser.parse_args()
    run_root = args.output_root / f"redi07a-dry-{uuid.uuid4().hex[:12]}"
    outcome = run_controlled_cold_start(output_root=run_root, known_recipe=None)
    replay = replay_cold_start(
        db_path=outcome.db_path,
        factory_run_id=outcome.factory_run_id,
    )
    report = {
        "run_root": str(run_root.resolve()),
        "factory_run_id": outcome.factory_run_id,
        "mode": outcome.selection.mode,
        "product_signature_sha256": outcome.signature.sha256,
        "graph_id": str(outcome.plan.dag.id),
        "graph_hash": outcome.execution.graph_hash,
        "graph_revision": outcome.execution.graph_revision,
        "launch_order": list(outcome.execution.launch_order),
        "completion_order": list(outcome.execution.completion_order),
        "verified": outcome.verified,
        "failed_attempts": len(replay["failures"]),
        "failure_categories": sorted({item["category"] for item in replay["failures"]}),
        "recipe_candidate_id": (
            str(outcome.candidate.recipe.id) if outcome.candidate else None
        ),
        "recipe_candidate_hash": (
            outcome.candidate.canonical_recipe_sha256 if outcome.candidate else None
        ),
        "promotion_status": (
            outcome.candidate.recipe.metadata["promotion_status"]
            if outcome.candidate else None
        ),
        "provider_calls": outcome.provider_calls,
        "network_calls": outcome.network_calls,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if outcome.verified and outcome.candidate is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
