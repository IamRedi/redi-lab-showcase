"""REDI-09F triangle_classify_v2 rerun runner — versioned, governed, fail-closed.

Executes the frozen REDI-09F 3-cell rerun (manifest 4cf6ce2f...) of
triangle_classify_v2 under the tracked execution-harness binding.  Cells
19/20/21 (v1 triangle) remain immutable and are EXCLUDED from capability
evidence per REDI-09E1; the final aggregation combines locked N5 + valid
expansion cells + the 3 rerun cells into N8 with v2 in place of v1-triangle.

Governance is identical to REDI-09E0: --live AND REDI_LIVE_PROVIDER=1,
CREDENTIAL_NOT_PROVISIONED on missing key, explicit owner ceiling >= hard
bound 0.050688 USD, execution-code hygiene, runner-hash binding, explicit
PENDING -> CALL_STARTED -> CALL_COMPLETED -> TERMINAL lifecycle, one provider
call per cell, no retry, immutable resume.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.run_redi_09e_live_expansion import (
    CellLifecycle,
    _env,
    _git,
    _git_returncode,
    _terminal_row,
    credential,
    execution_code_clean,
    run_live_cell,
)
from redi_lab.mcw_expansion import (
    ACCEPTED_REDI09D_MANIFEST_SHA256,
    ACCEPTED_REDI09E_EXPANSION_STATE_SHA256,
    EXCLUDED_V1_TRIANGLE_CELLS,
    _ExpansionState,
    build_expansion_row,
    build_rerun_freeze,
    load_pilot_rows_v1,
    n8_aggregation,
    rerun_execution_sequence,
    rerun_task_set,
    rerun_task_set_hash,
    replay_rerun,
    triangle_classify_v2_task,
)
from redi_lab.mcw_experiment import MCW_WORKER_CONDITIONS
from redi_lab.mcw_pricing import cell_cost_bound, pricing_schedule_hash

ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = "scripts/run_redi_09f_triangle_v2_rerun.py"
BINDING_PATH = ROOT / "experiments" / "redi09f-runner-binding.json"
EXECUTION_POLICY = "redi09f_triangle_v2_rerun_v1"

ACCEPTED_09E0_HEAD = "31a299686b173a5bdea5a4cc86681440f1fe8438"
ACCEPTED_RERUN_MANIFEST_HASH = "3aa07cfca0fb37f39b8af091121b54b8de0da7a62842aa321038fac3e0c4be9b"
ACCEPTED_R2_SET_HASH = "71c9909a76721b114be826b02d03addb751394d2b7d282215a421574000b89d2"
ACCEPTED_V2_TASK_HASH = "c04d870fb33b4a8f38c7371e9cc29924eb71c7413cdd627a08e3a640bfd086aa"
ACCEPTED_PRICING_HASH = "b7afc14fed836a67d5520b1b56cde165cbbdc6c6f23a0af3aaecac8c5a117cf3"
PILOT_STATE_DB = ROOT / "build" / "redi09b-mcw-live-pilot" / "pilot_state.sqlite"
EXPANSION_STATE_DB = ROOT / "build" / "redi09e-mcw-expansion-live" / "expansion_state.sqlite"
HARD_BOUND = Decimal("0.050688000")


def runner_sha256() -> str:
    return hashlib.sha256((ROOT / RUNNER_PATH).read_bytes()).hexdigest()


def load_binding() -> dict:
    if not BINDING_PATH.exists():
        raise RuntimeError(f"execution-harness binding missing: {BINDING_PATH}")
    return json.loads(BINDING_PATH.read_text(encoding="utf-8"))


def _live_flag() -> bool:
    return _env("REDI_LIVE_PROVIDER") == "1"


def preflight(owner_ceiling: str | None, output_root: Path) -> list[str]:
    drift: list[str] = []
    rev = _git(["rev-parse", "HEAD"]).strip()
    freeze = build_rerun_freeze()
    print(f"runner_path={RUNNER_PATH}")
    print(f"runner_sha256={runner_sha256()}")
    print(f"git_commit={rev}")
    print(f"expansion_rerun_manifest_hash={freeze['freeze_hash']}")
    print(f"v2_task_hash={freeze['v2_task']['sha256']}")
    print(f"r2_task_set_hash={freeze['rerun_task_set']['sha256']}")
    print(f"parent_09d_manifest_hash={freeze['parent_bindings']['redi09d_expansion_manifest_sha256']}")

    for anchor, name in ((ACCEPTED_09E0_HEAD, "09E0 governance HEAD"),):
        if _git_returncode(["merge-base", "--is-ancestor", anchor, rev]) != 0:
            drift.append(f"{name} {anchor} is not an ancestor of {rev}")
    if _git(["status", "--porcelain", "--untracked-files=no"]).strip():
        drift.append("tracked worktree not clean")
    clean, offending = execution_code_clean()
    print(f"execution_code_clean={str(clean).lower()}")
    if not clean:
        drift.append(f"execution-affecting code untracked/modified: {offending}")
    try:
        binding = load_binding()
        binding_ok = (
            binding.get("runner_path") == RUNNER_PATH
            and binding.get("runner_sha256") == runner_sha256()
            and binding.get("rerun_manifest_hash") == ACCEPTED_RERUN_MANIFEST_HASH
            and binding.get("execution_policy") == EXECUTION_POLICY
        )
        print(f"execution_harness_binding_ok={str(binding_ok).lower()}")
        if not binding_ok:
            drift.append("runner does not match the tracked execution-harness binding")
    except RuntimeError as exc:
        drift.append(str(exc))
    if freeze["freeze_hash"] != ACCEPTED_RERUN_MANIFEST_HASH:
        drift.append("rerun manifest/freeze hash drift")
    if freeze["v2_task"]["sha256"] != ACCEPTED_V2_TASK_HASH:
        drift.append("triangle_classify_v2 task hash drift")
    if freeze["rerun_task_set"]["sha256"] != ACCEPTED_R2_SET_HASH:
        drift.append("r2 task-set hash drift")
    if not freeze["v2_task"]["delta_audit"]["minimal"]:
        drift.append("v2 delta is not minimal (scientific design drift)")
    if pricing_schedule_hash() != ACCEPTED_PRICING_HASH:
        drift.append("pricing schedule drift")
    if not EXPANSION_STATE_DB.exists() or hashlib.sha256(EXPANSION_STATE_DB.read_bytes()).hexdigest() != ACCEPTED_REDI09E_EXPANSION_STATE_SHA256:
        drift.append("REDI-09E expansion evidence missing or changed")
    if owner_ceiling is None:
        drift.append("owner budget ceiling not supplied (never chosen silently)")
    elif Decimal(str(owner_ceiling)) < HARD_BOUND:
        drift.append(f"owner ceiling {owner_ceiling} below hard bound {HARD_BOUND}")
    if not _live_flag():
        drift.append("REDI_LIVE_PROVIDER=1 required for live execution")

    print(f"hard_bound={HARD_BOUND} owner_ceiling={owner_ceiling}")
    print(
        f"max_provider_calls={freeze['call_budget']['max_provider_calls']} "
        f"automatic_retry={freeze['call_budget']['automatic_retry']} "
        f"per_cell={freeze['call_budget']['per_cell_max_calls']}"
    )
    print(
        "sequence="
        + json.dumps(
            [(c["cell_seq"], c["task_key"], c["condition_id"], c["model"]) for c in rerun_execution_sequence()]
        )
    )
    print(f"excluded_v1_cells={list(EXCLUDED_V1_TRIANGLE_CELLS)} (immutable, not capability evidence)")
    print(f"output_root={output_root}")
    return drift


def execute(owner_ceiling: str, output_root: Path) -> int:
    drift = preflight(owner_ceiling, output_root)
    if not _live_flag():
        print("LIVE BLOCKED: REDI_LIVE_PROVIDER=1 is required")
        return 1
    if drift:
        print(f"DRIFT={json.dumps(drift)}")
        print("BLOCKED BEFORE FIRST PROVIDER CALL")
        return 1
    if credential() is None:
        print("CREDENTIAL_NOT_PROVISIONED")
        print("zero cells consumed; no state created; run from a shell holding the owner credential")
        return 1

    output_root.mkdir(parents=True, exist_ok=True)
    terminal_state = _ExpansionState(output_root / "rerun_state.sqlite")
    lifecycle = CellLifecycle(output_root / "cell_lifecycle.sqlite")
    per_cell_bound = {
        c.condition_id: Decimal(cell_cost_bound(model=c.model)["hard_upper_bound"]["cost_amount"])
        for c in MCW_WORKER_CONDITIONS
    }

    for cell in rerun_execution_sequence():
        state = lifecycle.state(cell["cell_seq"])
        if state in ("CALL_STARTED", "CALL_COMPLETED"):
            print(f"ADJUDICATION REQUIRED: cell {cell['cell_seq']} is {state} without TERMINAL evidence.")
            return 1

    for cell in rerun_execution_sequence():
        if terminal_state.is_completed(cell["cell_seq"]):
            print(f"cell {cell['cell_seq']:02d}: TERMINAL (immutable, skipped)")
            continue
        task = next(t for t in rerun_task_set() if t.key == cell["task_key"])
        condition = next(c for c in MCW_WORKER_CONDITIONS if c.condition_id == cell["condition_id"])
        if task.canonical_hash != cell["task_hash"]:
            print("STOP: task baseline drift")
            return 1
        workspace_root = output_root / "conditions" / condition.condition_id / task.key / "workspace"
        if workspace_root.exists() and any(workspace_root.rglob("*")):
            print("STOP: workspace contamination detected before call; cell left PENDING")
            return 1
        workspace_root.mkdir(parents=True, exist_ok=True)
        for rel, content in task.baseline_files.items():
            path = workspace_root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="")

        persisted = [
            json.loads(r[0])
            for r in terminal_state.conn.execute(
                "SELECT row_json FROM expansion_cells WHERE status='completed'"
            )
        ]
        spent = sum((Decimal(r["cost"]["amount"]) for r in persisted if r.get("cost")), Decimal("0"))
        remaining = [c for c in rerun_execution_sequence() if not terminal_state.is_completed(c["cell_seq"])]
        projection = spent + sum(per_cell_bound[c["condition_id"]] for c in remaining)
        if projection > Decimal(str(owner_ceiling)):
            print(f"STOP BEFORE CALL: projected exposure {projection} exceeds ceiling {owner_ceiling}")
            return 1

        lifecycle.set_state(cell["cell_seq"], "PENDING")
        print(
            f"cell {cell['cell_seq']:02d} {cell['task_key']}/{cell['condition_id']} "
            f"projection={projection} <= {owner_ceiling} -> CALL_STARTED ({condition.model})"
        )
        lifecycle.set_state(cell["cell_seq"], "CALL_STARTED")
        try:
            cell_result = run_live_cell(task, condition, workspace_root)
        except Exception as exc:  # noqa: BLE001
            print(
                f"PROVIDER REQUEST DID NOT COMPLETE ({type(exc).__name__}: {exc}); "
                f"cell {cell['cell_seq']} left PENDING for owner retry; no automatic retry"
            )
            lifecycle.set_state(cell["cell_seq"], "PENDING")
            return 1
        lifecycle.set_state(cell["cell_seq"], "CALL_COMPLETED")
        row = _terminal_row(cell, cell_result, task, workspace_root)
        terminal_state.record(cell["cell_seq"], row)
        lifecycle.set_state(cell["cell_seq"], "TERMINAL")
        print(
            f"  -> TERMINAL verified={row['verified']} status={row['provider_completion_status']} "
            f"in={row['usage']['input']}/{row['usage']['cached_input']}/{row['usage']['output']} "
            f"reasoning={row['reasoning_tokens']} cost={row['cost']['amount']} "
            f"evidence_state={row['pricing_evidence_state']} fail={row['failure_classification']}"
        )

    rerun_rows = [
        json.loads(rj)
        for (rj,) in terminal_state.conn.execute(
            "SELECT row_json FROM expansion_cells WHERE status='completed' ORDER BY cell_seq"
        )
    ]
    pilot_rows = load_pilot_rows_v1(PILOT_STATE_DB)
    expansion_rows = expansion_rows_from(EXPANSION_STATE_DB)
    valid_expansion = [
        r for r in expansion_rows if r.get("cell_seq") not in EXCLUDED_V1_TRIANGLE_CELLS
    ]
    agg = n8_aggregation(pilot_rows, valid_expansion + rerun_rows)
    print("N8_AGGREGATION_V2=" + json.dumps(agg, sort_keys=True, default=str))
    d1 = replay_rerun(pilot_rows, expansion_rows, rerun_rows)
    d2 = replay_rerun(load_pilot_rows_v1(PILOT_STATE_DB), expansion_rows, rerun_rows)
    print(f"REPLAY_X2_IDENTICAL={str(d1 == d2).lower()} digest={d1}")
    return 0


def expansion_rows_from(path: Path) -> list[dict]:
    conn = _state_conn(path)
    try:
        return [
            json.loads(rj)
            for (rj,) in conn.execute(
                "SELECT row_json FROM expansion_cells WHERE status='completed' ORDER BY cell_seq"
            )
        ]
    finally:
        conn.close()


def _state_conn(path: Path):
    import sqlite3

    return sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro&immutable=1", uri=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="explicit live arming (also requires REDI_LIVE_PROVIDER=1)")
    parser.add_argument("--owner-budget-ceiling", type=str, default=None)
    parser.add_argument("--preflight-only", action="store_true", help="verify gates; zero calls, zero state")
    parser.add_argument("--output-root", type=Path, default=ROOT / "build" / "redi09f-triangle-v2-rerun")
    args = parser.parse_args(argv)

    drift = preflight(args.owner_budget_ceiling, args.output_root.resolve())
    key_present = credential() is not None
    print(
        f"credential_present={str(key_present).lower()} live_flag={str(_live_flag()).lower()} "
        f"live_armed={str(args.live).lower()}"
    )
    if args.preflight_only:
        if drift or not key_present or not _live_flag() or not args.live:
            print("PREFLIGHT: NOT READY (blocked before any provider call; no state created)")
            return 1
        print("PREFLIGHT: READY")
        return 0
    if not args.live:
        print("LIVE BLOCKED: --live is required (explicit arming; no default-live behavior)")
        return 1
    return execute(args.owner_budget_ceiling, args.output_root.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
