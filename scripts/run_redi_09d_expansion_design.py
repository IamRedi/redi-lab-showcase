"""REDI-09D MCW N=8 expansion design — offline design freeze & dry rehearsal.

Default mode verifies every historical binding (fail-closed drift checks),
prints the frozen expansion design (sampling proof, 9-cell order, manifest
hash, cost bounds, predeclared sufficiency rule) and performs ZERO provider
calls and ZERO cell executions.

--rehearsal   additionally runs the 9 expansion cells with deterministic
              fake workers into a fresh temp root, proves V1+V2 replay,
              N=8 aggregation (locked 5 + dry 3), and immutable resume.

--live        NOT IMPLEMENTED BY DESIGN.  Live expansion execution is a
              separate owner-authorized stage (REDI-09E); this runner
              refuses to make any provider call.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from redi_lab.mcw_expansion import (
    ACCEPTED_REDI09A_MANIFEST_SHA256,
    ACCEPTED_REDI09B_FREEZE_SHA256,
    ACCEPTED_REDI09C_PILOT_STATE_SHA256,
    ACCEPTED_REDI09C_TASK_SET_SHA256,
    DEFAULT_EXPANSION_DRY_OUTCOMES,
    ExpansionController,
    build_expansion_freeze,
    clopper_pearson_interval,
    expansion_execution_sequence,
    expansion_seed,
    expansion_tasks,
    expanded_task_set_hash,
    load_pilot_rows_v1,
    n8_aggregation,
    replay_n8,
    sampling_frame_hash,
    select_expansion_tasks,
    selection_ranks,
)
from redi_lab import mcw_expansion
from redi_lab.mcw_experiment import MCW_POLICY_ID, MCW_SUFFICIENCY_MIN_TASKS
from redi_lab.mcw_live_pilot import audit_redi09a_manifest, build_live_pilot_freeze
from redi_lab.mcw_pricing import pricing_schedule, pricing_schedule_hash

ROOT = Path(__file__).resolve().parents[1]
REDI09A_ACCEPTANCE_DB = ROOT / "build" / "redi09a-mcw-dry-20260922T205849Z" / "mcw_evidence.sqlite"
PILOT_STATE_DB = ROOT / "build" / "redi09b-mcw-live-pilot" / "pilot_state.sqlite"


def _revision() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
    ).strip()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def historical_bindings_check() -> list[str]:
    """Fail-closed drift checks over every accepted historical artifact.

    Repository HEAD may legitimately advance after REDI-09D; drift is checked
    against the frozen evidence-base revision, never the moving HEAD.
    """
    drift: list[str] = []
    rev = _revision()
    print(f"current_head={rev}")
    tracked = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout.strip()
    if tracked:
        drift.append("tracked worktree is not clean")
    if (
        build_live_pilot_freeze(source_revision=mcw_expansion.EVIDENCE_BASE_REVISION)["freeze_hash"]
        != ACCEPTED_REDI09B_FREEZE_SHA256
    ):
        drift.append("REDI-09B freeze hash drift")
    if task_set_binding_drift():
        drift.append("REDI-09C task-set hash drift")
    if pricing_schedule_hash() != "b7afc14fed836a67d5520b1b56cde165cbbdc6c6f23a0af3aaecac8c5a117cf3":
        drift.append("pricing schedule hash drift")
    if REDI09A_ACCEPTANCE_DB.exists():
        audit = audit_redi09a_manifest(REDI09A_ACCEPTANCE_DB)
        if audit["payload_sha256"] != ACCEPTED_REDI09A_MANIFEST_SHA256 or not (
            audit["frozen"] and audit["attempt_bindings_consistent"] and audit["matches_current_task_set"]
        ):
            drift.append("REDI-09A manifest binding drift")
    else:
        drift.append("REDI-09A acceptance evidence DB not found")
    if PILOT_STATE_DB.exists():
        if _file_sha256(PILOT_STATE_DB) != ACCEPTED_REDI09C_PILOT_STATE_SHA256:
            drift.append("REDI-09C pilot_state.sqlite drift (evidence changed)")
    else:
        drift.append("REDI-09C pilot_state.sqlite not found")
    return drift


def task_set_binding_drift() -> bool:
    from redi_lab.mcw_experiment import task_set_hash

    return task_set_hash() != ACCEPTED_REDI09C_TASK_SET_SHA256 or (
        build_live_pilot_freeze(source_revision=mcw_expansion.EVIDENCE_BASE_REVISION)
        ["task_set"]["sha256"]
        != ACCEPTED_REDI09C_TASK_SET_SHA256
    )


def print_design() -> dict:
    freeze = build_expansion_freeze()
    print("REDI-09D MCW N=8 EXPANSION DESIGN FREEZE")
    print(f"policy={MCW_POLICY_ID} -> {freeze['policy']}")
    print(f"stage={freeze['stage']}")
    print(f"evidence_base_revision={freeze['source_revision']}")
    print(f"parent_bindings={json.dumps(freeze['parent_bindings'], sort_keys=True)}")
    print(f"sampling_policy_id={freeze['sampling_policy']['policy_id']} v{freeze['sampling_policy']['policy_version']}")
    print(f"sampling_frame_hash={freeze['sampling_policy']['frame_hash']}")
    print(f"selection_seed={freeze['sampling_policy']['selection_seed']}")
    print(f"selection_ranks={json.dumps(freeze['sampling_policy']['selection_ranks'], sort_keys=True)}")
    print(f"selected_task_ids={json.dumps(freeze['sampling_policy']['selected_task_ids'])}")
    print(f"expanded_task_set={freeze['expanded_task_set']['task_set_id']}")
    print(f"expanded_task_set_sha256={freeze['expanded_task_set']['sha256']}")
    print(f"original_task_set_unchanged={freeze['original_task_set']['sha256']}")
    print("execution_order=")
    for cell in expansion_execution_sequence():
        print(
            f"  cell {cell['cell_seq']:02d} (expansion {cell['expansion_seq']}): "
            f"task={cell['task_key']:<18} condition={cell['condition_id']:<9} model={cell['model']}"
        )
    rule = freeze["aggregate_sufficiency_rule"]
    print(f"aggregate_sufficiency_rule={json.dumps(rule, sort_keys=True)}")
    print(f"call_budget={json.dumps(freeze['call_budget'], sort_keys=True)}")
    print(f"cost_bounds={json.dumps(freeze['cost_bounds'], sort_keys=True)}")
    print(f"pricing={json.dumps(freeze['pricing'], sort_keys=True)}")
    print(f"observability={json.dumps(freeze['observability'], sort_keys=True)}")
    print(f"expansion_freeze_hash={freeze['freeze_hash']}")
    return freeze


def run_rehearsal() -> int:
    work = Path(tempfile.mkdtemp(prefix="redi09d-rehearsal-"))
    controller = ExpansionController(output_root=work / "expansion")
    pre = controller.preflight()
    print("REDI-09D DRY 9-CELL REHEARSAL (deterministic fake workers; zero provider/network)")
    print(f"budget_gate_ok={str(pre['budget_gate_ok']).lower()} live_flag_required_for_real_calls=true")
    result = controller.run()
    rows = result["rows"]
    print(f"cells_resulted={len(rows)} stop_reason={result['stop_reason']}")
    for r in rows:
        print(
            f"  cell {r['cell_seq']:02d} {r['task_key']:<18} {r['condition_id']:<9} "
            f"verified={str(r['verified']):<5} provider_calls={r['provider_calls']} "
            f"automatic_retry={str(r['automatic_retry']):<5} status={r['provider_completion_status']} "
            f"reasoning={r['reasoning_tokens']} evidence_state={r['pricing_evidence_state']} "
            f"fail={r['failure_classification']}"
        )
    proof = {
        "schema_v2_fields_present": all(
            all(field in r for field in (
                "provider_calls", "network_calls", "automatic_retry",
                "provider_completion_status", "finish_reason", "incomplete",
                "refusal", "reasoning_tokens", "pricing_evidence_state",
            )) for r in rows
        ),
        "no_retry": all(r["automatic_retry"] is False for r in rows),
        "single_slot_per_cell": all(r["provider_calls"] <= 1 for r in rows),
    }
    print(f"v2_observability_proof={json.dumps(proof)}")

    # resume semantics: a second run must execute nothing new (all immutable)
    rerun = ExpansionController(
        output_root=work / "expansion", scripted_outcomes=DEFAULT_EXPANSION_DRY_OUTCOMES
    ).run()
    print(f"resume_executed_zero_new_cells={len(rerun['rows']) == 0}")

    # N=8 aggregation: locked real first-5 (read-only) + dry expansion rows
    pilot_rows = load_pilot_rows_v1(PILOT_STATE_DB)
    print(f"pilot_rows_loaded_read_only={len(pilot_rows)}")
    agg = n8_aggregation(pilot_rows, rows)
    for cond in agg["frontier"]:
        print(
            f"  N8 {cond['worker_condition']:<9} n={cond['n_tasks']} "
            f"verified={cond['verified_successes']} proportion={cond['observed_success_proportion']} "
            f"state={cond['criterion_satisfaction_state']} interval={cond['uncertainty_metadata']}"
        )
    print(f"claims={json.dumps(agg['claims'], sort_keys=True)}")

    # V1+V2 replay x2 (read-only, deterministic)
    digest1 = replay_n8(pilot_rows, rows)
    digest2 = replay_n8(load_pilot_rows_v1(PILOT_STATE_DB), rows)
    print(f"replay_v1v2_deterministic={digest1 == digest2} digest={digest1}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rehearsal", action="store_true", help="dry 9-cell rehearsal (fake workers)")
    parser.add_argument("--live", action="store_true", help="refused by design: live execution is REDI-09E, owner-authorized")
    args = parser.parse_args(argv)

    drift = historical_bindings_check()
    print(f"historical_bindings_drift={json.dumps(drift)}")
    if drift:
        print("BLOCKED: historical REDI-09 evidence differs; expansion design may not proceed")
        return 1

    freeze = print_design()
    if args.live:
        print("LIVE REFUSED BY DESIGN: REDI-09D freezes the expansion only; "
              "live execution requires the separate owner-authorized REDI-09E stage")
        return 1
    if args.rehearsal:
        return run_rehearsal()
    print("DESIGN_ONLY (offline freeze complete; no cells executed; pass --rehearsal for the dry run)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
