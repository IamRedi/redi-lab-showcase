"""REDI-09B MCW live-pilot runner — prepared here, EXECUTED ONLY BY THE OWNER.

Default mode prints the complete preflight (experiment/manifest/pricing/
sequence/estimates/budget-gate status) and performs zero provider calls.

--rehearsal   runs the exact 15-cell scheduler with deterministic fake
              provider outcomes and fake usage (zero provider/network),
              proving ordering, cost arithmetic, resume and stop rules.
--live        owner-triggered real execution; refuses to start unless
              REDI_LIVE_PROVIDER=1 AND --owner-budget-ceiling is supplied
              AND every budget-gate condition holds.  NOT run in REDI-09B.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from redi_lab.mcw_live_pilot import (
    LivePilotController,
    audit_redi09a_manifest,
    build_live_pilot_freeze,
    execution_sequence,
)
from redi_lab.mcw_pricing import live_cost_plan, pricing_schedule_hash


ROOT = Path(__file__).resolve().parents[1]
REDI09A_ACCEPTANCE_DB = ROOT / "build" / "redi09a-mcw-dry-20260922T205849Z" / "mcw_evidence.sqlite"


def _revision() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
    ).strip()


def _print_preflight(controller: LivePilotController) -> dict:
    pre = controller.preflight()
    print("REDI-09B MCW LIVE PILOT PREFLIGHT")
    print(f"experiment_family={pre['experiment_family']}")
    print(f"freeze_hash={pre['freeze_hash']}")
    print(f"pricing_schedule_id={pre['pricing_schedule_id']}")
    print(f"pricing_schedule_hash={pre['pricing_schedule_hash']}")
    print(f"stratum_id={pre['stratum_id']}")
    print(f"task_set_sha256={pre['task_set_sha256']}")
    print(f"worker_conditions={json.dumps(pre['worker_conditions'])}")
    print(f"exact_model_ids={json.dumps(pre['exact_model_ids'], sort_keys=True)}")
    print(f"total_cells={pre['total_cells']}")
    print(f"max_provider_calls={pre['max_provider_calls']}")
    print(f"automatic_retry={str(pre['automatic_retry']).lower()}")
    print("execution_sequence=")
    for cell in pre["execution_sequence"]:
        print(
            f"  cell {cell['cell_seq']:02d}: task={cell['task_key']:16s} "
            f"condition={cell['condition_id']:8s} model={cell['model']}"
        )
    print(f"expected_cost_estimate={pre['expected_token_cost_estimate']} {controller.freeze['pricing']['currency']}")
    print(f"conservative_cost_bound={pre['conservative_cost_bound']} {controller.freeze['pricing']['currency']}")
    print(f"long_context_worst_case={pre['long_context_worst_case']}")
    print(f"owner_budget_ceiling={pre['owner_budget_ceiling']}")
    print(f"budget_gate_ok={str(pre['budget_gate_ok']).lower()}")
    print(f"budget_gate_reasons={json.dumps(pre['budget_gate_reasons'])}")
    print(f"claim_boundary={json.dumps(pre['claim_boundary'], sort_keys=True)}")
    return pre


def main(argv: list[str] | None = None) -> int:
    import os

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rehearsal", action="store_true", help="dry 15-cell budget rehearsal (fake usage)")
    parser.add_argument("--live", action="store_true", help="owner-triggered live pilot (blocked unless fully authorized)")
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--owner-budget-ceiling", type=str, default=None)
    parser.add_argument("--resume", action="store_true", help="continue at the next unexecuted cell")
    args = parser.parse_args(argv)

    freeze = build_live_pilot_freeze(source_revision=_revision())
    output = args.output_root or (ROOT / "build" / "redi09b-mcw-live-pilot")
    controller = LivePilotController(
        output_root=output,
        owner_budget_ceiling=args.owner_budget_ceiling,
        live=args.live,
        source_revision=_revision(),
    )
    pre = _print_preflight(controller)
    if REDI09A_ACCEPTANCE_DB.exists():
        print(f"redi09a_manifest_audit={json.dumps(audit_redi09a_manifest(REDI09A_ACCEPTANCE_DB), sort_keys=True)}")

    if not args.rehearsal and not args.live:
        print("PREFLIGHT_ONLY (no cells executed; pass --rehearsal or owner-authorize --live)")
        if not pre["budget_gate_ok"]:
            print("BUDGET_GATE=BLOCKED")
            return 1
        print("BUDGET_GATE=READY (owner may authorize)")
        return 0

    if args.live:
        if os.environ.get("REDI_LIVE_PROVIDER") != "1":
            print("LIVE BLOCKED: REDI_LIVE_PROVIDER=1 is required")
            return 1
        if not pre["budget_gate_ok"]:
            print("LIVE BLOCKED: budget gate failed")
            return 1
        if not args.resume and (output / "pilot_state.sqlite").exists():
            print("LIVE BLOCKED: existing pilot state found; pass --resume to continue deterministically")
            return 1

    result = controller.run()
    print("REDI-09B PILOT RESULT")
    print(f"mode={'LIVE' if args.live else 'REHEARSAL'}")
    print(f"cells_resulted={len([r for r in result['rows'] if 'verified' in r])}")
    print(f"stop_reason={result['stop_reason']}")
    print(f"frontier={json.dumps(result['report']['frontier'], sort_keys=True)}")
    print(f"claims={json.dumps(result['report']['claims'], sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
