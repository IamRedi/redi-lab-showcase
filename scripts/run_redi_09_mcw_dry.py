"""REDI-09A MCW dry experiment runner — harness validation only.

Runs the full paired Minimum-Capable-Worker experiment path with deterministic
fake worker conditions across the predeclared python-component stratum task
set.  Zero provider/network calls; no winner is selected; nothing here is a
scientific conclusion or transfer evidence.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from redi_lab.mcw_experiment import (
    MCW_STRATUM_ID,
    MCW_WORKER_CONDITIONS,
    replay_mcw_result,
    replay_mcw_snapshot,
    run_mcw_experiment,
)


ROOT = Path(__file__).resolve().parents[1]


def _revision() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
    ).strip()


def _default_output() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return ROOT / "build" / f"redi09a-mcw-dry-{stamp}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=None)
    args = parser.parse_args(argv)
    output = (args.output_root or _default_output()).resolve()
    print("REDI-09A MCW EXPERIMENT FOUNDATION — DRY HARNESS RUN")
    print("acceptance_semantics=HARNESS_REHEARSAL_ONLY (no scientific conclusion)")
    print(f"stratum={MCW_STRATUM_ID}")
    print("conditions=" + ",".join(c.condition_id for c in MCW_WORKER_CONDITIONS))
    outcome = run_mcw_experiment(output_root=output, source_revision=_revision())
    result = json.loads(outcome.result_path.read_text(encoding="utf-8"))
    one = replay_mcw_result(outcome.result_path)
    two = replay_mcw_result(outcome.result_path)
    snapshot = replay_mcw_snapshot(outcome.db_path, outcome.factory_run_id)
    print("REDI-09A DRY SUMMARY")
    print(f"manifest_id={outcome.manifest_id}")
    print(f"manifest_sha256={outcome.manifest_sha256}")
    print(f"factory_run_id={outcome.factory_run_id}")
    print(f"cells={len(outcome.rows)} (tasks x conditions, paired)")
    print(f"provider_calls={outcome.provider_calls} network_calls={outcome.network_calls}")
    print(f"replay_x2_result_equal={str(one == two).lower()}")
    print(f"ledger_attempt_rows={len(snapshot)}")
    for frontier_row in outcome.report["frontier"]:
        print(
            f"  frontier {frontier_row['condition_id']:8s} verified="
            f"{frontier_row['verified_count']}/{frontier_row['n_tasks']} "
            f"state={frontier_row['state']} "
            f"cost={frontier_row['cost']['status']}"
        )
    print(f"quality_criterion_margin={outcome.report['quality_criterion']['statistical_margin']['status']}")
    print(f"no_winner_selected={str(outcome.report['claims']['no_winner_selected']).lower()}")
    print(f"live_budget_plan={json.dumps(result['report']['live_budget_plan'], sort_keys=True)}")
    print(f"mcw_result={outcome.result_path}")
    checks = {
        "zero_provider_network": outcome.provider_calls == 0 and outcome.network_calls == 0,
        "replay_x2_equal": one == two,
        "ledger_rows_match": len(snapshot) == len(outcome.rows),
        "guardrails_pass": True,
        "no_winner_selected": outcome.report["claims"]["no_winner_selected"] is True,
        "all_cells_isolated": all(
            row["context"]["reference_leak_detected"] is False
            and row["context"]["private_check_args_in_context"] is False
            for row in outcome.rows
        ),
    }
    print(f"dry_checks={json.dumps(checks, sort_keys=True)}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
