"""REDI-09B — MCW live-pilot freeze, budget gate, and deterministic execution.

Prepares the exact 15-cell live MCW pilot WITHOUT executing it.  Everything
the owner needs to authorize one controlled pilot is frozen here:

- immutable freeze record (manifest snapshot + pricing schedule + execution
  order + stop rules + budget gate + claim boundary), hash-bound
- owner budget gate (never silently chooses a monetary ceiling)
- deterministic resume semantics (completed cells immutable, no reruns)
- result blindness (cell configuration is fully fixed before the first cell)
- stop rules (continue for worker outcomes, STOP for harness/pricing/
  manifest/evidence problems)
- pilot-only conclusion boundary (N=5 can yield PILOT_CANDIDATE_SUFFICIENT,
  never a final Minimum Capable Worker)
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from .domain import (
    StationId,
    TokenUsage,
    WorkerId,
)
from .mcw_experiment import (
    DEFAULT_DRY_OUTCOMES,
    GATE_ORDER,
    MCW_EXPERIMENT_FAMILY,
    MCW_POLICY_ID,
    MCW_QUALITY_CRITERION,
    MCW_SUFFICIENCY_MIN_TASKS,
    MCW_WORKER_CONDITIONS,
    _compile_context,
    _evaluate_cell_gates,
    _run_fake_condition_cell,
    classify_mcw_failure,
    default_stratum,
    default_task_set,
    task_set_hash,
    task_set_id,
)
from .mcw_pricing import (
    MCWPricingMismatchError,
    calculate_cost,
    live_cost_plan,
    pricing_schedule,
    pricing_schedule_hash,
)

PILOT_MAX_CELLS = 15
PILOT_MAX_PROVIDER_CALLS = 15
PILOT_PER_CELL_MAX_CALLS = 1
PILOT_AUTOMATIC_RETRY = False
FRONTIER_PILOT_CANDIDATE_SUFFICIENT = "PILOT_CANDIDATE_SUFFICIENT"

CONTINUE_OUTCOME_CLASSES = {
    "WORKER_IMPLEMENTATION_FAILURE",
    "PROVIDER_OUTPUT_TRUNCATED_OR_INCOMPLETE",
    "SCOPE_VIOLATION",
    "REVIEWER_REJECTION",
    "STRUCTURED_OUTPUT_SCHEMA_FAILURE",
}
STOP_REASON_MANIFEST_DRIFT = "MANIFEST_DRIFT"
STOP_REASON_PRICING_MISMATCH = "PRICING_MISMATCH"
STOP_REASON_HARNESS_DEFECT = "HARNESS_DEFECT"
STOP_REASON_EVIDENCE_CORRUPTION = "EVIDENCE_CORRUPTION"

# Deterministic fake per-cell usage (rehearsal only) keyed by scripted outcome.
FAKE_USAGE_BY_OUTCOME: Mapping[str, Mapping[str, int]] = {
    "success": {"input": 1500, "cached_input": 0, "output": 400},
    "implementation_failure": {"input": 1500, "cached_input": 0, "output": 380},
    "scope_violation": {"input": 1500, "cached_input": 0, "output": 210},
    "provider_incomplete": {"input": 1520, "cached_input": 0, "output": 2048},
}


def condition_runtime_by_id() -> dict[str, dict[str, Any]]:
    return {
        c.condition_id: {
            "worker_id": c.worker_id,
            "provider": c.provider,
            "model": c.model,
            "planned_runtime": dict(c.planned_runtime),
        }
        for c in MCW_WORKER_CONDITIONS
    }


def execution_sequence() -> list[dict[str, Any]]:
    """The frozen 15-cell sequence (preregistered counterbalancing)."""
    tasks = default_task_set()
    sequence = []
    for index, task in enumerate(tasks):
        order = condition_order_for_task(index)
        for position, condition_id in enumerate(order):
            sequence.append(
                {
                    "cell_seq": index * len(order) + position + 1,
                    "task_key": task.key,
                    "task_hash": task.canonical_hash,
                    "condition_id": condition_id,
                    "worker_id": condition_runtime_by_id()[condition_id]["worker_id"],
                    "model": condition_runtime_by_id()[condition_id]["model"],
                }
            )
    return sequence


def condition_order_for_task(task_index: int) -> tuple[str, ...]:
    ids = [c.condition_id for c in MCW_WORKER_CONDITIONS]
    offset = task_index % len(ids)
    return tuple(ids[offset:] + ids[:offset])


def build_live_pilot_freeze(*, source_revision: str = "") -> dict[str, Any]:
    """The complete frozen experiment definition, hash-bound."""
    stratum = default_stratum()
    tasks = default_task_set()
    quality_hash = _hash(dict(MCW_QUALITY_CRITERION))
    freeze = {
        "experiment_family": MCW_EXPERIMENT_FAMILY,
        "policy": {"id": MCW_POLICY_ID, "version": 1},
        "stage": "REDI-09B_LIVE_PILOT_FREEZE",
        "source_revision": source_revision,
        "stratum": stratum.to_dict(),
        "stratum_sha256": stratum.canonical_hash,
        "task_set": {
            "id": task_set_id(),
            "sha256": task_set_hash(tasks),
            "n_tasks": len(tasks),
            "tasks": [{"key": t.key, "sha256": t.canonical_hash} for t in tasks],
            "post_result_mutation_forbidden": True,
        },
        "worker_conditions": condition_runtime_by_id(),
        "context_policy": {
            "id": "mcw_component_context_v1",
            "budget_bytes": 6000,
            "includes": ["task requirement", "interface signatures", "builder output contract"],
            "excludes": [
                "private checks",
                "reference implementation",
                "other-condition results",
                "prior-cell outcomes (result blindness)",
            ],
        },
        "output_contract": {"policy_id": "redi_builder_patch_contract_v1", "policy_version": 1},
        "verification_gates": list(GATE_ORDER),
        "quality_criterion": dict(MCW_QUALITY_CRITERION),
        "quality_criterion_hash": quality_hash,
        "execution_order": execution_sequence(),
        "call_budget": {
            "planned_cells": PILOT_MAX_CELLS,
            "max_provider_calls": PILOT_MAX_PROVIDER_CALLS,
            "per_cell_max_calls": PILOT_PER_CELL_MAX_CALLS,
            "automatic_retry": PILOT_AUTOMATIC_RETRY,
            "worker_fallback": "forbidden",
        },
        "stop_rules": {
            "continue_outcome_classes": sorted(CONTINUE_OUTCOME_CLASSES),
            "stop_conditions": [
                STOP_REASON_HARNESS_DEFECT,
                STOP_REASON_EVIDENCE_CORRUPTION,
                STOP_REASON_PRICING_MISMATCH,
                STOP_REASON_MANIFEST_DRIFT,
            ],
            "no_paid_automatic_retries": True,
        },
        "resume_policy": {
            "completed_cells_immutable": True,
            "rerun_completed_cell": "forbidden",
            "failed_worker_outcomes_remain_outcomes": True,
            "restart_continues_at_next_preregistered_cell": True,
        },
        "result_blindness": {
            "cell_configuration_fixed_before_first_cell": True,
            "adaptive_routing": "forbidden_in_pilot",
            "orchestrator_may_not_alter": [
                "prompts",
                "context",
                "worker_selection",
                "budgets",
                "task_ordering",
                "criteria",
            ],
        },
        "pricing": {
            "schedule_id": pricing_schedule()["schedule_id"],
            "schedule_hash": pricing_schedule_hash(),
            "currency": pricing_schedule()["currency"],
            "evidence_state": pricing_schedule()["evidence_state"],
        },
        "budget_gate": {
            "requires_explicit_owner_ceiling": True,
            "silently_chosen_ceiling_forbidden": True,
            "planned_calls_must_equal": PILOT_MAX_PROVIDER_CALLS,
        },
        "human_intervention_policy": {"automatic_retry": False, "escalation": "owner_only"},
        "claim_boundary": {
            "pilot_tasks": len(tasks),
            "final_mcw_threshold_tasks": MCW_SUFFICIENCY_MIN_TASKS,
            "best_pilot_state": FRONTIER_PILOT_CANDIDATE_SUFFICIENT,
            "final_minimum_capable_worker_declared_at_n5": False,
            "threshold_moved_after_results": False,
        },
        "frontier_report_schema": {
            "fields": [
                "worker_condition",
                "n_tasks",
                "verified_successes",
                "failures_by_class",
                "observed_success_proportion",
                "token_totals",
                "latency_seconds_total",
                "cost",
                "confidence_metadata",
            ],
            "winner_field": None,
            "global_ranking": None,
            "automatic_minimum_capable_selection": False,
        },
    }
    freeze["freeze_hash"] = _hash(freeze)
    return freeze


def pilot_frontier_state(*, verified_count: int, n_tasks: int, margin_estimated: bool) -> str:
    """Pilot-only cap: N=5 can never produce a final CANDIDATE_SUFFICIENT."""
    from .mcw_experiment import (
        FRONTIER_INSUFFICIENT_EVIDENCE,
        FRONTIER_NOT_SUFFICIENT_OBSERVED,
    )

    if verified_count < n_tasks:
        return FRONTIER_NOT_SUFFICIENT_OBSERVED
    if n_tasks >= MCW_SUFFICIENCY_MIN_TASKS and margin_estimated:
        from .mcw_experiment import FRONTIER_CANDIDATE_SUFFICIENT

        return FRONTIER_CANDIDATE_SUFFICIENT
    if verified_count == n_tasks:
        return FRONTIER_PILOT_CANDIDATE_SUFFICIENT
    return FRONTIER_INSUFFICIENT_EVIDENCE


def audit_redi09a_manifest(db_path: Path | str) -> dict[str, Any]:
    """Re-open and audit the REDI-09A dry manifest (frozen + hash-bound)."""
    conn = sqlite3.connect(
        "file:" + Path(db_path).resolve().as_posix() + "?mode=ro&immutable=1", uri=True
    )
    try:
        row = conn.execute(
            "SELECT frozen_ts, payload_sha256, payload_json FROM experiment_manifests LIMIT 1"
        ).fetchone()
        bindings = {
            json.loads(pj)["metadata"]["manifest_sha256"]
            for (pj,) in conn.execute(
                "SELECT payload_json FROM evidence_entries WHERE entity_type='RunEvidence'"
            )
            if json.loads(pj).get("metadata", {}).get("evidence_kind") == "mcw_worker_attempt"
        }
    finally:
        conn.close()
    payload = json.loads(row[2]) if row else None
    return {
        "manifest_found": row is not None,
        "frozen": bool(row and row[0] is not None),
        "payload_sha256": row[1] if row else None,
        "task_set_sha256": (payload or {}).get("task_selection", {}).get("task_set_sha256"),
        "stratum_sha256": (payload or {}).get("task_selection", {}).get("stratum_sha256"),
        "attempt_bindings_consistent": bindings == ({row[1]} if row else set()),
        "matches_current_task_set": bool(
            payload
            and payload.get("task_selection", {}).get("task_set_sha256") == task_set_hash()
        ),
    }


class LivePilotController:
    """Deterministic 15-cell pilot executor (live gated; rehearsal default)."""

    def __init__(
        self,
        *,
        output_root: Path | str,
        owner_budget_ceiling: str | None = None,
        live: bool = False,
        scripted_outcomes: Mapping[str, str] | None = None,
        pinned_schedule_hash: str | None = None,
        source_revision: str = "",
    ) -> None:
        self.root = Path(output_root).resolve()
        self.freeze = build_live_pilot_freeze(source_revision=source_revision)
        self.owner_budget_ceiling = owner_budget_ceiling
        self.live = live
        self.scripted_outcomes = dict(
            DEFAULT_DRY_OUTCOMES if scripted_outcomes is None else scripted_outcomes
        )
        self.pinned_schedule_hash = pinned_schedule_hash or pricing_schedule_hash()
        self.provider_calls = 0
        self.network_calls = 0
        self.stop_reason: str | None = None
        self.completed_rows: list[dict[str, Any]] = []

    # -- preflight / gates -------------------------------------------------

    def preflight(self) -> dict[str, Any]:
        conditions = {cid: runtime["model"] for cid, runtime in self.freeze["worker_conditions"].items()}
        plan = live_cost_plan(conditions=conditions, n_tasks=len(default_task_set()))
        conservative = Decimal(plan["experiment_hard_upper_bound"])
        gate_ok, gate_reasons = self.budget_gate(conservative_bound=conservative)
        return {
            "experiment_family": self.freeze["experiment_family"],
            "freeze_hash": self.freeze["freeze_hash"],
            "manifest_binding": self.freeze["quality_criterion_hash"],
            "pricing_schedule_id": self.freeze["pricing"]["schedule_id"],
            "pricing_schedule_hash": self.freeze["pricing"]["schedule_hash"],
            "stratum_id": self.freeze["stratum"]["stratum_id"],
            "task_set_sha256": self.freeze["task_set"]["sha256"],
            "worker_conditions": sorted(self.freeze["worker_conditions"]),
            "exact_model_ids": conditions,
            "total_cells": len(self.freeze["execution_order"]),
            "max_provider_calls": self.freeze["call_budget"]["max_provider_calls"],
            "automatic_retry": self.freeze["call_budget"]["automatic_retry"],
            "execution_sequence": self.freeze["execution_order"],
            "expected_token_cost_estimate": plan["experiment_expected_estimate"],
            "conservative_cost_bound": plan["experiment_hard_upper_bound"],
            "long_context_worst_case": plan["experiment_long_context_worst_case"],
            "currency": plan["currency"],
            "owner_budget_ceiling": self.owner_budget_ceiling,
            "budget_gate_ok": gate_ok,
            "budget_gate_reasons": gate_reasons,
            "live_mode": self.live,
            "claim_boundary": self.freeze["claim_boundary"],
        }

    def budget_gate(self, *, conservative_bound: Decimal) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        cells = self.freeze["execution_order"]
        if len(cells) != PILOT_MAX_CELLS or self.freeze["call_budget"]["max_provider_calls"] != PILOT_MAX_PROVIDER_CALLS:
            reasons.append("planned calls must equal 15")
        if self.freeze["call_budget"]["automatic_retry"] is not False:
            reasons.append("automatic retry must be false")
        if self.freeze["call_budget"]["per_cell_max_calls"] != PILOT_PER_CELL_MAX_CALLS:
            reasons.append("per-cell max calls must be 1")
        if self.freeze["pricing"]["schedule_hash"] != self.pinned_schedule_hash:
            reasons.append("pricing schedule is not pinned to the canonical hash")
        if self.owner_budget_ceiling is None:
            reasons.append("owner budget ceiling not supplied (never chosen silently)")
        else:
            try:
                if Decimal(str(self.owner_budget_ceiling)) < conservative_bound:
                    reasons.append(
                        f"owner ceiling {self.owner_budget_ceiling} is below the conservative bound {conservative_bound}"
                    )
            except Exception:  # noqa: BLE001
                reasons.append("owner budget ceiling is not a valid decimal amount")
        return (not reasons), reasons

    # -- execution ----------------------------------------------------------

    def run(self) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        state = _PilotState(self.root / "pilot_state.sqlite")
        if self.owner_budget_ceiling is None:
            raise RuntimeError("budget gate: owner budget ceiling not supplied")
        conservative_bound = Decimal(self._conservative_bound())
        ok, gate_reasons = self.budget_gate(conservative_bound=conservative_bound)
        if not ok:
            # Rehearsals may use a symbolic ceiling, but a live run never
            # proceeds with a failed gate (e.g. ceiling below the bound).
            if self.live:
                raise RuntimeError("budget gate failed: " + "; ".join(gate_reasons))
            symbolic = Decimal(str(self.owner_budget_ceiling)) >= Decimal("1000000")
            if not symbolic:
                raise RuntimeError("budget gate failed: " + "; ".join(gate_reasons))
        rows: list[dict[str, Any]] = []
        prior = state.completed_rows()  # resume: rows recorded by earlier runs
        for cell in self.freeze["execution_order"]:
            if state.is_completed(cell["cell_seq"]):
                continue  # resume: completed cells are immutable, never rerun
            if self.stop_reason is not None:
                break
            row = self._execute_cell(cell)
            state.record(cell["cell_seq"], row)
            rows.append(row)
            if row.get("stop_reason"):
                self.stop_reason = row["stop_reason"]
                break
        report = self._report(prior + rows)
        return {"rows": prior + rows, "report": report, "stop_reason": self.stop_reason}

    def _conservative_bound(self) -> str:
        conditions = {cid: runtime["model"] for cid, runtime in self.freeze["worker_conditions"].items()}
        return live_cost_plan(conditions=conditions, n_tasks=len(default_task_set()))[
            "experiment_hard_upper_bound"
        ]

    def _execute_cell(self, cell: Mapping[str, Any]) -> dict[str, Any]:
        # Manifest/pricing drift checks BEFORE every cell (fail-closed).
        current_freeze = build_live_pilot_freeze(source_revision=self.freeze["source_revision"])
        if current_freeze["freeze_hash"] != self.freeze["freeze_hash"]:
            return self._stop_row(cell, STOP_REASON_MANIFEST_DRIFT)
        if pricing_schedule_hash() != self.pinned_schedule_hash:
            return self._stop_row(cell, STOP_REASON_PRICING_MISMATCH)
        task = next(t for t in default_task_set() if t.key == cell["task_key"])
        condition = next(
            c for c in MCW_WORKER_CONDITIONS if c.condition_id == cell["condition_id"]
        )
        cell_root = self.root / "conditions" / condition.condition_id / task.key
        workspace_root = cell_root / "workspace"
        workspace_root.mkdir(parents=True, exist_ok=True)
        for rel, content in task.baseline_files.items():
            path = workspace_root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        scripted = self.scripted_outcomes.get((condition.condition_id, task.key), "success")
        try:
            if self.live:
                cell_result = self._run_live_cell(
                    task=task, condition=condition, workspace_root=workspace_root
                )
            else:
                cell_result = _run_fake_condition_cell(
                    task=task,
                    condition=condition,
                    workspace_root=workspace_root,
                    scripted_outcome=scripted,
                )
        except Exception as exc:  # noqa: BLE001 — harness defects stop the experiment
            return self._stop_row(
                cell, STOP_REASON_HARNESS_DEFECT, detail=f"{type(exc).__name__}: {exc}"
            )
        gate_outcomes, gate_reasons = _evaluate_cell_gates(
            task=task, cell=cell_result, workspace_root=workspace_root
        )
        verified = all(gate_outcomes[gate] == "PASS" for gate in GATE_ORDER)
        classification, failure_category = classify_mcw_failure(
            provider_incomplete=bool(cell_result["provider_completion"].get("incomplete")),
            gate_outcomes=gate_outcomes,
            structured_error=cell_result["structured_output"].get("safe_error"),
            unexpected_error=None,
        )
        usage = (
            dict(FAKE_USAGE_BY_OUTCOME[scripted])
            if not self.live
            else {
                "input": int(cell_result["provider"].get("input_tokens", 0) or 0),
                "cached_input": int(cell_result["provider"].get("cached_input_tokens", 0) or 0),
                "output": int(cell_result["provider"].get("output_tokens", 0) or 0),
            }
        )
        try:
            cost = calculate_cost(model=condition.model, usage=usage)
        except MCWPricingMismatchError as exc:
            return self._stop_row(cell, STOP_REASON_PRICING_MISMATCH, detail=str(exc))
        if verified:
            self.provider_calls += 1 if self.live else 0
            self.network_calls += 1 if self.live else 0
        return {
            "cell_seq": cell["cell_seq"],
            "task_key": cell["task_key"],
            "condition_id": cell["condition_id"],
            "worker_id": cell["worker_id"],
            "model": cell["model"],
            "verified": verified,
            "gates": dict(gate_outcomes),
            "gate_reasons": dict(gate_reasons),
            "failure_classification": None if verified else classification,
            "usage": usage,
            "cost": {
                "schedule_id": cost["schedule_id"],
                "schedule_hash": cost["schedule_hash"],
                "amount": cost["cost_amount"],
                "currency": cost["currency"],
                "calculation_version": cost["calculation_version"],
                "raw_usage": cost["raw_usage"],
            },
            "context_bytes": cell_result["context"]["compiled_bytes"],
            "total_wall_seconds": cell_result["elapsed_seconds"],
            "stop_reason": None,
        }

    def _run_live_cell(self, *, task, condition, workspace_root: Path) -> dict[str, Any]:
        from .runtime.live_provider_adapter import (
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

        def observe() -> None:
            self.provider_calls += 1
            self.network_calls += 1

        started = __import__("time").perf_counter()
        adapter = LiveProviderAdapter(config=config, before_call=observe)
        result = adapter.run_node(
            task_id=__import__("redi_lab.domain", fromlist=["TaskId"]).TaskId(f"mcw_{task.key}"),
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
        elapsed = __import__("time").perf_counter() - started
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
                "observed_provider_access": "live_completion_observed"
                if (result.provider_completion or {}).get("finish_reason")
                else "no_completion_observed",
            },
            "elapsed_seconds": elapsed,
            "execution_kind": "governed_provider_slot",
        }

    def _stop_row(self, cell: Mapping[str, Any], reason: str, detail: str = "") -> dict[str, Any]:
        return {
            "cell_seq": cell["cell_seq"],
            "task_key": cell["task_key"],
            "condition_id": cell["condition_id"],
            "verified": False,
            "stop_reason": reason,
            "stop_detail": detail,
        }

    def _report(self, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        frontier = []
        for condition in MCW_WORKER_CONDITIONS:
            cond_rows = [r for r in rows if r.get("condition_id") == condition.condition_id and "verified" in r]
            verified = sum(1 for r in cond_rows if r["verified"])
            cost_total = sum(
                (Decimal(r["cost"]["amount"]) for r in cond_rows if r.get("cost")), Decimal("0")
            )
            tokens_in = sum(r["usage"]["input"] for r in cond_rows if r.get("usage"))
            tokens_out = sum(r["usage"]["output"] for r in cond_rows if r.get("usage"))
            failure_classes: dict[str, int] = {}
            for r in cond_rows:
                if r.get("failure_classification"):
                    key = str(r["failure_classification"])
                    failure_classes[key] = failure_classes.get(key, 0) + 1
            frontier.append(
                {
                    "worker_condition": condition.condition_id,
                    "model": condition.model,
                    "n_tasks": len(cond_rows),
                    "verified_successes": verified,
                    "failures_by_class": failure_classes,
                    "observed_success_proportion": (verified / len(cond_rows)) if cond_rows else None,
                    "token_totals": {"input": tokens_in, "output": tokens_out},
                    "latency_seconds_total": sum(r.get("total_wall_seconds", 0.0) or 0.0 for r in cond_rows),
                    "cost": {
                        "status": "computed_under_pinned_schedule",
                        "amount": str(cost_total),
                        "currency": "USD",
                        "schedule_id": self.freeze["pricing"]["schedule_id"],
                    },
                    "confidence_metadata": pilot_confidence_metadata(cond_rows),
                    "state": pilot_frontier_state(
                        verified_count=verified,
                        n_tasks=len(cond_rows),
                        margin_estimated=False,
                    ),
                }
            )
        return {
            "experiment_family": MCW_EXPERIMENT_FAMILY,
            "freeze_hash": self.freeze["freeze_hash"],
            "stage": "LIVE_PILOT_RESULTS" if self.live else "REHEARSAL_RESULTS",
            "cells_executed": len([r for r in rows if "verified" in r]),
            "stop_reason": self.stop_reason,
            "frontier": frontier,
            "claims": {
                "no_winner_selected": True,
                "minimum_capable_declared": False,
                "pilot_only": True,
                "final_mcw_blocked_at_n5": True,
            },
        }


def pilot_confidence_metadata(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    successes = sum(1 for r in rows if r.get("verified") is True)
    failure_classes: dict[str, int] = {}
    for r in rows:
        if r.get("verified") is not True and r.get("failure_classification"):
            key = str(r["failure_classification"])
            failure_classes[key] = failure_classes.get(key, 0) + 1
    return {
        "method": "descriptive_pilot_only",
        "n_tasks": n,
        "successes": successes,
        "failures": n - successes,
        "failure_classes": failure_classes,
        "observed_success_proportion": (successes / n) if n else None,
        "uncertainty_interval": None,
        "interval_method": "NOT_ESTIMATED_PILOT_SAMPLE_ASSUMPTIONS_NOT_MET",
        "coverage_warning": "pilot counts are descriptive only; no capability estimate",
    }


class _PilotState:
    """Durable per-cell state: completed cells are immutable, never rerun."""

    def __init__(self, path: Path) -> None:
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pilot_cells (
                cell_seq INTEGER PRIMARY KEY,
                cell_id TEXT NOT NULL,
                status TEXT NOT NULL,
                result_sha256 TEXT NOT NULL,
                row_json TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    def is_completed(self, cell_seq: int) -> bool:
        row = self.conn.execute(
            "SELECT status FROM pilot_cells WHERE cell_seq=?", (cell_seq,)
        ).fetchone()
        return bool(row and row[0] == "completed")

    def record(self, cell_seq: int, row: Mapping[str, Any]) -> None:
        if self.is_completed(cell_seq):
            raise RuntimeError(
                f"cell {cell_seq} is already completed; completed cells are immutable"
            )
        encoded = json.dumps(dict(row), sort_keys=True, default=str)
        self.conn.execute(
            "INSERT INTO pilot_cells (cell_seq, cell_id, status, result_sha256, row_json) VALUES (?, ?, 'completed', ?, ?)",
            (cell_seq, f"{row.get('condition_id')}::{row.get('task_key')}", hashlib.sha256(encoded.encode("utf-8")).hexdigest(), encoded),
        )
        self.conn.commit()

    def completed_rows(self) -> list[dict[str, Any]]:
        return [
            json.loads(rj)
            for (rj,) in self.conn.execute(
                "SELECT row_json FROM pilot_cells WHERE status='completed' ORDER BY cell_seq"
            )
        ]


def _hash(value: Any) -> str:
    from .mcw_pricing import canonical_json

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


__all__ = [
    "CONTINUE_OUTCOME_CLASSES",
    "FRONTIER_PILOT_CANDIDATE_SUFFICIENT",
    "LivePilotController",
    "PILOT_AUTOMATIC_RETRY",
    "PILOT_MAX_CELLS",
    "PILOT_MAX_PROVIDER_CALLS",
    "audit_redi09a_manifest",
    "build_live_pilot_freeze",
    "execution_sequence",
    "pilot_confidence_metadata",
    "pilot_frontier_state",
]
