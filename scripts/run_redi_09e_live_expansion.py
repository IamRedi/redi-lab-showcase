"""REDI-09E live N8 expansion runner — versioned, governed, fail-closed.

Executes the frozen REDI-09D 9-cell expansion (manifest c7d5606c...) under
the REDI-09E0 execution-harness binding (experiments/redi09e-runner-binding.json).
This runner is orchestration only: every scientific variable — tasks, order,
worker profiles, context, gates, criterion, sufficiency rule, pricing — is
delegated to the frozen REDI-09D code and verified against accepted hashes
before any provider call.

Governance gates (all fail-closed BEFORE state creation or any call):

- --live AND REDI_LIVE_PROVIDER=1 must both be present (no default-live)
- REDI_OPENAI_API_KEY must be present -> else CREDENTIAL_NOT_PROVISIONED,
  zero cells consumed
- explicit --owner-budget-ceiling >= hard bound 0.152064 USD
- HEAD == accepted execution commit; tracked execution-affecting code clean
- expansion manifest/freeze, N5/N8 task sets, pricing schedule, historical
  N5 evidence all hash-verified
- runner identity (working-copy SHA-256) must equal the tracked binding

Cell lifecycle states: PENDING -> CALL_STARTED -> CALL_COMPLETED -> TERMINAL.
A provider-independent pre-call failure (missing credential, wrong HEAD,
drift, budget, isolation) leaves the cell PENDING.  Once a provider request
is submitted the attempt is governed evidence: an interruption between
CALL_STARTED/CALL_COMPLETED and TERMINAL requires owner adjudication and is
never silently retried.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from redi_lab.domain import StationId, TaskId, TokenUsage, WorkerId
from redi_lab.mcw_expansion import (
    _ExpansionState,
    build_expansion_freeze,
    build_expansion_row,
    expansion_execution_sequence,
    expansion_tasks,
    expanded_task_set,
    expanded_task_set_hash,
    load_pilot_rows_v1,
    n8_aggregation,
    replay_n8,
)
from redi_lab.mcw_experiment import (
    MCW_WORKER_CONDITIONS,
    _compile_context,
    _evaluate_cell_gates,
    classify_mcw_failure,
    default_stratum,
    task_set_hash,
)
from redi_lab.mcw_live_pilot import build_live_pilot_freeze
from redi_lab.mcw_pricing import (
    calculate_cost,
    cell_cost_bound,
    pricing_schedule,
    pricing_schedule_hash,
)

ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = "scripts/run_redi_09e_live_expansion.py"
BINDING_PATH = ROOT / "experiments" / "redi09e-runner-binding.json"
EXECUTION_POLICY = "redi09e_live_execution_v1"

ACCEPTED_HEAD = "4d5212383ef08b732e8ec4d9ca5f0b1c4279610e"
ACCEPTED_09D_COMMIT = "a793bc0784515ebfa7a7c69fb129995d04e8d25d"
ACCEPTED_MANIFEST_HASH = "c7d5606c340911fcbf5f2a233ac968de3768b1ef5eae13f108c9c23bc4bfa335"
ACCEPTED_N5_HASH = "8a980e621b206beeea069ac28ff7c72833deff823088f24b7ad23d9b3c30fa94"
ACCEPTED_N8_HASH = "5fdb22aa688b81836aad19e77ce3640b684dbab7be38367cd719b58a526ad4a7"
ACCEPTED_PRICING_HASH = "b7afc14fed836a67d5520b1b56cde165cbbdc6c6f23a0af3aaecac8c5a117cf3"
ACCEPTED_PILOT_STATE_SHA256 = "5bb70a642cf09217107c3d951725d59c90bb99c267037888056bc35323739c9e"
ACCEPTED_09B_FREEZE_SHA256 = "5142cf2da50cd5143a97cf2a75615f6bdb71f6603ee9bc8aa37a514ed9947fde"
PILOT_STATE_DB = ROOT / "build" / "redi09b-mcw-live-pilot" / "pilot_state.sqlite"
HARD_BOUND = Decimal("0.152064000")

# Execution-affecting code that must be tracked and unmodified at live time.
EXECUTION_CODE_PREFIXES = (
    "redi_lab/",
    "scripts/run_redi_09",
    "tests/unit/test_redi09",
)
EXECUTION_CODE_FILES = ("pyproject.toml",)


# ---------------------------------------------------------------------------
# identity / hygiene
# ---------------------------------------------------------------------------


def runner_sha256() -> str:
    return hashlib.sha256((ROOT / RUNNER_PATH).read_bytes()).hexdigest()


def _git(args: list[str]) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True
    ).stdout


def _git_returncode(args: list[str]) -> int:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).returncode


def execution_code_clean() -> tuple[bool, list[str]]:
    """Untracked or modified execution-affecting code must block live runs."""
    lines = [line for line in _git(["status", "--porcelain"]).splitlines() if line.strip()]
    offending = []
    for line in lines:
        path = line[3:].strip().strip('"')
        if any(path.startswith(prefix) for prefix in EXECUTION_CODE_PREFIXES) or path in EXECUTION_CODE_FILES:
            offending.append(line)
    return (not offending), offending


def load_binding() -> dict:
    if not BINDING_PATH.exists():
        raise RuntimeError(f"execution-harness binding missing: {BINDING_PATH}")
    return json.loads(BINDING_PATH.read_text(encoding="utf-8"))


def _env(name: str) -> str:
    import os

    return os.environ.get(name, "")


def credential() -> str | None:
    return _env("REDI_OPENAI_API_KEY") or None


# ---------------------------------------------------------------------------
# preflight (zero provider calls, zero network, zero state mutation)
# ---------------------------------------------------------------------------


def preflight(owner_ceiling: str | None, output_root: Path) -> tuple[list[str], dict]:
    drift: list[str] = []
    rev = _git(["rev-parse", "HEAD"]).strip()
    freeze = build_expansion_freeze()

    print(f"runner_path={RUNNER_PATH}")
    print(f"runner_sha256={runner_sha256()}")
    print(f"git_commit={rev}")
    print(f"redi09d_design_commit={ACCEPTED_09D_COMMIT}")
    print(f"expansion_manifest_hash={freeze['freeze_hash']}")
    print(f"expansion_freeze_hash={freeze['freeze_hash']}")

    def _is_ancestor(commit: str) -> bool:
        return _git_returncode(["merge-base", "--is-ancestor", commit, rev]) == 0

    evidence_head_ancestor = _is_ancestor(ACCEPTED_HEAD)
    design_commit_ancestor = _is_ancestor(ACCEPTED_09D_COMMIT)
    scientific_diff = _git(["diff", "--stat", f"{ACCEPTED_HEAD}..{rev}", "--", "redi_lab/"]).strip()
    print(f"evidence_head_ancestor_ok={str(evidence_head_ancestor).lower()}")
    print(f"redi09d_commit_ancestor_ok={str(design_commit_ancestor).lower()}")
    print(f"scientific_code_diff_since_evidence_head={'none' if not scientific_diff else scientific_diff}")
    if not evidence_head_ancestor:
        drift.append(f"accepted evidence HEAD {ACCEPTED_HEAD} is not an ancestor of {rev}")
    if not design_commit_ancestor:
        drift.append("09D design commit is not an ancestor of the execution HEAD")
    if scientific_diff:
        drift.append("scientific code (redi_lab/) changed since the accepted evidence HEAD")
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
            and binding.get("parent_manifest_hash") == ACCEPTED_MANIFEST_HASH
            and binding.get("redi09d_design_commit") == ACCEPTED_09D_COMMIT
            and binding.get("execution_head") == ACCEPTED_HEAD
        )
        print(f"execution_harness_binding_ok={str(binding_ok).lower()}")
        if not binding_ok:
            drift.append("runner does not match the tracked execution-harness binding")
    except RuntimeError as exc:
        drift.append(str(exc))
    if build_live_pilot_freeze(source_revision="c82c47e3b634a13ae084d37ef368b4a193569e7d")[
        "freeze_hash"
    ] != ACCEPTED_09B_FREEZE_SHA256:
        drift.append("09B freeze drift")
    if freeze["freeze_hash"] != ACCEPTED_MANIFEST_HASH:
        drift.append("expansion manifest/freeze hash drift")
    if task_set_hash() != ACCEPTED_N5_HASH:
        drift.append("N5 task-set drift")
    if expanded_task_set_hash() != ACCEPTED_N8_HASH:
        drift.append("N8 task-set drift")
    for task in expansion_tasks():
        cell = next(c for c in freeze["execution_order"] if c["task_key"] == task.key)
        if cell["task_hash"] != task.canonical_hash:
            drift.append(f"expansion task hash drift: {task.key}")
    if pricing_schedule_hash() != ACCEPTED_PRICING_HASH:
        drift.append("pricing schedule drift")
    if not PILOT_STATE_DB.exists() or hashlib.sha256(PILOT_STATE_DB.read_bytes()).hexdigest() != ACCEPTED_PILOT_STATE_SHA256:
        drift.append("historical N5 pilot evidence missing or changed")
    if owner_ceiling is None:
        drift.append("owner budget ceiling not supplied (never chosen silently)")
    elif Decimal(str(owner_ceiling)) < HARD_BOUND:
        drift.append(f"owner ceiling {owner_ceiling} below hard bound {HARD_BOUND}")
    if _env("REDI_LIVE_PROVIDER") != "1" and not _preflight_only_mode():
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
            [(c["cell_seq"], c["task_key"], c["condition_id"]) for c in expansion_execution_sequence()]
        )
    )
    print(f"output_root={output_root}")
    return drift, freeze


_PREFLIGHT_ONLY = {"active": False}


def _preflight_only_mode() -> bool:
    return _PREFLIGHT_ONLY["active"]


# ---------------------------------------------------------------------------
# cell lifecycle state machine: PENDING -> CALL_STARTED -> CALL_COMPLETED -> TERMINAL
# ---------------------------------------------------------------------------


class CellLifecycle:
    """Explicit per-cell states recorded durably next to terminal evidence."""

    def __init__(self, db_path: Path) -> None:
        self.conn = sqlite3.connect(db_path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS cell_states ("
            " cell_seq INTEGER PRIMARY KEY, state TEXT NOT NULL)"
        )
        self.conn.commit()

    def state(self, cell_seq: int) -> str | None:
        row = self.conn.execute(
            "SELECT state FROM cell_states WHERE cell_seq=?", (cell_seq,)
        ).fetchone()
        return row[0] if row else None

    def set_state(self, cell_seq: int, state: str) -> None:
        self.conn.execute(
            "INSERT INTO cell_states (cell_seq, state) VALUES (?, ?) "
            "ON CONFLICT(cell_seq) DO UPDATE SET state=excluded.state",
            (cell_seq, state),
        )
        self.conn.commit()


# ---------------------------------------------------------------------------
# governed provider slot (exactly one call; mirrors the V1 pilot live path)
# ---------------------------------------------------------------------------


def run_live_cell(task, condition, workspace_root: Path) -> dict:
    from redi_lab.runtime.live_provider_adapter import (
        LiveProviderAdapter,
        LiveProviderConfig,
        render_live_request,
    )

    runtime = dict(condition.planned_runtime)
    config = LiveProviderConfig(
        env_key_name="REDI_OPENAI_API_KEY",
        model=str(condition.model),
        timeout_s=60.0,
        max_output_tokens=int(runtime["max_output_tokens"]),
        temperature=runtime.get("temperature"),
        top_p=runtime.get("top_p"),
        reasoning_effort=runtime.get("reasoning_effort"),
        api_capability_profile=str(runtime["api_capability_profile"]),
        provider_name=str(condition.provider),
        api=str(runtime["api"]),
    )
    config.request_configuration_evidence()
    context = _compile_context(task)
    started = time.perf_counter()
    adapter = LiveProviderAdapter(config=config)
    result = adapter.run_node(
        task_id=TaskId(f"mcw_{task.key}"),
        station_id=StationId(default_stratum().station_id),
        worker_id=WorkerId(condition.worker_id),
        context={
            "role": "builder",
            "instructions": context["context_body"],
            "context_body": "",
            "patch_output_contract": task.contract,
        },
        workspace_root=workspace_root,
        station_role="builder",
    )
    elapsed = time.perf_counter() - started
    usage = result.token_usage or TokenUsage(input=0, output=0, cached_input=0)
    request_view = render_live_request(
        role="builder",
        task_id=f"mcw_{task.key}",
        instructions=context["context_body"],
        context_body="",
        patch_output_contract=task.contract,
    )
    return {
        "patches": list(result.patches),
        "observed_patches": list(result.observed_patches),
        "output_shape": dict(result.output_shape),
        "context": {
            "compiled_bytes": context["compiled_bytes"],
            "estimated_tokens": context["estimated_tokens"],
            "private_check_args_in_context": context["private_check_args_in_context"],
            "reference_leak_detected": context["reference_leak_detected"],
            "full_request_hash": request_view["full_request_hash"],
        },
        "structured_output": {
            "validated": result.error_message is None,
            "schema": "RediBuilderOutput",
            "safe_error": result.error_message,
            "source": "provider_json_schema_plus_local_validation",
        },
        "provider_completion": dict(result.provider_completion),
        "provider": {
            "calls": 1,
            "input_tokens": usage.input,
            "output_tokens": usage.output,
            "cached_input_tokens": usage.cached_input,
            "provider": condition.provider,
            "model": condition.model,
            "observed_provider_access": (
                "live_completion_observed"
                if (result.provider_completion or {}).get("finish_reason")
                else "no_completion_observed"
            ),
        },
        "elapsed_seconds": elapsed,
        "execution_kind": "governed_provider_slot",
    }


def _terminal_row(cell, cell_result, task, workspace_root: Path) -> dict:
    gate_outcomes, gate_reasons = _evaluate_cell_gates(
        task=task, cell=cell_result, workspace_root=workspace_root
    )
    verified = all(gate_outcomes[g] == "PASS" for g in gate_outcomes)
    classification, _ = classify_mcw_failure(
        provider_incomplete=bool(cell_result["provider_completion"].get("incomplete")),
        gate_outcomes=gate_outcomes,
        structured_error=cell_result["structured_output"].get("safe_error"),
        unexpected_error=None,
    )
    usage = {
        "input": int(cell_result["provider"]["input_tokens"] or 0),
        "cached_input": int(cell_result["provider"]["cached_input_tokens"] or 0),
        "output": int(cell_result["provider"]["output_tokens"] or 0),
    }
    cost = calculate_cost(model=cell["model"], usage=usage)
    row = build_expansion_row(
        cell=cell,
        cell_result=cell_result,
        gate_outcomes=gate_outcomes,
        gate_reasons=gate_reasons,
        verified=verified,
        classification=None if verified else classification,
        usage=usage,
        cost=cost,
        elapsed_seconds=cell_result["elapsed_seconds"],
        called=True,
    )
    patch = cell_result["patches"][0] if cell_result["patches"] else None
    row["provider_latency_seconds"] = cell_result["provider_completion"].get(
        "provider_call_elapsed_seconds"
    )
    row["parsed_patch_present"] = patch is not None
    row["patch_artifact_sha256"] = (
        hashlib.sha256((patch.content or "").encode("utf-8")).hexdigest() if patch else None
    )
    row["full_request_hash"] = cell_result["context"].get("full_request_hash")
    return row


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------


def execute(owner_ceiling: str, output_root: Path) -> int:
    drift, _ = preflight(owner_ceiling, output_root)
    if _env("REDI_LIVE_PROVIDER") != "1":
        print("LIVE BLOCKED: REDI_LIVE_PROVIDER=1 is required")
        return 1
    if drift:
        print(f"DRIFT={json.dumps(drift)}")
        print("BLOCKED BEFORE FIRST PROVIDER CALL")
        return 1
    key = credential()
    if key is None:
        print("CREDENTIAL_NOT_PROVISIONED")
        print("zero cells consumed; no state created; run from a shell holding the owner credential")
        return 1

    output_root.mkdir(parents=True, exist_ok=True)
    terminal_state = _ExpansionState(output_root / "expansion_state.sqlite")
    lifecycle = CellLifecycle(output_root / "cell_lifecycle.sqlite")
    per_cell_bound = {
        c.condition_id: Decimal(cell_cost_bound(model=c.model)["hard_upper_bound"]["cost_amount"])
        for c in MCW_WORKER_CONDITIONS
    }

    # adjudication guard: interrupted cells are never silently retried
    for cell in expansion_execution_sequence():
        state = lifecycle.state(cell["cell_seq"])
        if state in ("CALL_STARTED", "CALL_COMPLETED"):
            print(
                f"ADJUDICATION REQUIRED: cell {cell['cell_seq']} is {state} without TERMINAL "
                f"evidence; a provider request may have been submitted. Never silently retried."
            )
            return 1

    for cell in expansion_execution_sequence():
        if terminal_state.is_completed(cell["cell_seq"]):
            print(f"cell {cell['cell_seq']:02d}: TERMINAL (immutable, skipped)")
            continue
        task = next(t for t in expanded_task_set() if t.key == cell["task_key"])
        condition = next(c for c in MCW_WORKER_CONDITIONS if c.condition_id == cell["condition_id"])
        if task.canonical_hash != cell["task_hash"]:
            print("STOP: task baseline drift")
            return 1
        workspace_root = (
            output_root / "conditions" / condition.condition_id / task.key / "workspace"
        )
        if workspace_root.exists() and any(workspace_root.rglob("*")):
            print("STOP: workspace contamination detected before call; cell left PENDING")
            return 1
        workspace_root.mkdir(parents=True, exist_ok=True)
        for rel, content in task.baseline_files.items():
            path = workspace_root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="")
            if (workspace_root / rel).read_text(encoding="utf-8") != content:
                print("STOP: baseline write verification failed; cell left PENDING")
                return 1

        persisted = [
            json.loads(r[0])
            for r in terminal_state.conn.execute(
                "SELECT row_json FROM expansion_cells WHERE status='completed'"
            )
        ]
        spent = sum((Decimal(r["cost"]["amount"]) for r in persisted if r.get("cost")), Decimal("0"))
        remaining = [
            c for c in expansion_execution_sequence() if not terminal_state.is_completed(c["cell_seq"])
        ]
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
            f"finish={row['finish_reason']} in={usage_summary(row)} out_reasoning={row['reasoning_tokens']} "
            f"cost={row['cost']['amount']} evidence_state={row['pricing_evidence_state']} "
            f"fail={row['failure_classification']}"
        )

    expansion_rows = [
        json.loads(rj)
        for (rj,) in terminal_state.conn.execute(
            "SELECT row_json FROM expansion_cells WHERE status='completed' ORDER BY cell_seq"
        )
    ]
    pilot_rows = load_pilot_rows_v1(PILOT_STATE_DB)
    agg = n8_aggregation(pilot_rows, expansion_rows)
    print("N8_AGGREGATION=" + json.dumps(agg, sort_keys=True, default=str))
    d1 = replay_n8(pilot_rows, expansion_rows)
    d2 = replay_n8(load_pilot_rows_v1(PILOT_STATE_DB), expansion_rows)
    print(f"REPLAY_X2_IDENTICAL={str(d1 == d2).lower()} digest={d1}")
    return 0


def usage_summary(row: dict) -> str:
    return f"{row['usage']['input']}/{row['usage']['cached_input']}/{row['usage']['output']}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="explicit live arming (also requires REDI_LIVE_PROVIDER=1)")
    parser.add_argument("--owner-budget-ceiling", type=str, default=None)
    parser.add_argument("--preflight-only", action="store_true", help="verify gates; zero calls, zero state")
    parser.add_argument("--output-root", type=Path, default=ROOT / "build" / "redi09e-mcw-expansion-live")
    args = parser.parse_args(argv)

    _PREFLIGHT_ONLY["active"] = bool(args.preflight_only)
    drift, _ = preflight(args.owner_budget_ceiling, args.output_root.resolve())
    key_present = credential() is not None
    live_flag = _env("REDI_LIVE_PROVIDER") == "1"
    print(f"credential_present={str(key_present).lower()} live_flag={str(live_flag).lower()} live_armed={str(args.live).lower()}")

    if args.preflight_only:
        if drift or not key_present or not live_flag or not args.live:
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
