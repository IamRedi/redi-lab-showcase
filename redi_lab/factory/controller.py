"""REDI-03 Minimal Factory Controller — Layer C orchestration.

Stateless event-driven FSM: ProductSpec → Builder → deterministic gates →
independent Reviewer → final result. All state transitions are persisted to
the REDI-02 append-only ledger; resume derives everything from persisted
evidence, never in-memory caches.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..domain import (
    ArtifactRef,
    ContextManifest,
    EnvironmentInfo,
    FactoryRun,
    FactoryRunId,
    FailureCategory,
    FailureRecord,
    GateResult,
    LifecycleEvent,
    LifecycleEventType,
    Mode,
    MonetaryCost,
    ProductSpec,
    RunEvidence,
    RunStatus,
    Severity,
    StationContract,
    TaskContract,
    Timing,
    TokenUsage,
    WorkerId,
    WorkerPool,
    new_id,
)
from ..context_compiler import (
    DEFAULT_BUDGET_BYTES,
    ContextBudgetError,
    ContextFirewallError,
    compile_context,
)
from ..worker_selection import (
    SELECTION_POLICY_ID,
    SELECTION_POLICY_VERSION,
    pool_from_snapshot,
    pool_snapshot,
    pool_snapshot_hash,
    resolve_worker,
)
from ..evidence import (
    append_event,
    append_failure_record,
    append_gate_result,
    append_run_evidence,
)
from ..gates import DeterministicGateEngine, GateSpec, public_failure_diagnostic
from ..persistence import (
    ClosedRunError,
    DuplicateLedgerEntryError,
    EvidenceLedger,
    FactoryRunRepository,
    bootstrap_schema,
)
from ..runtime.contracts import (
    NodeRunResult,
    PatchEdit,
    ReviewerVerdict,
    WorkerRunner,
)
from .builder_context import (
    bound_retry_feedback,
    build_builder_context,
    build_reviewer_context,
    render_builder_prompt,
    render_reviewer_prompt,
)
from .isolation import (
    IsolatedWorkspace,
    ScopeViolation,
    apply_patches,
    baseline_file_hashes,
    canonicalize_scope_violations,
)


class SimulatedCrash(Exception):
    """Raised by run() when the caller injects a synthetic crash for tests."""


@dataclass
class RunDeps:
    builder_worker: WorkerRunner
    reviewer_worker: WorkerRunner
    builder_station: StationContract
    reviewer_station: StationContract
    fixture_root: Path
    hidden_test_root: Path | None
    allowed_scope_prefixes: tuple[str, ...] = ("slugify", "tests")
    gate_spec: GateSpec | None = None
    builder_case_id: str = "fix_slugify_builder"
    reviewer_case_id: str = "reviewer_pass"
    max_retries: int = 1
    crash_inject_after_worker_completed: bool = False
    # REDI-04A: optional station worker pools.  When a pool is configured the
    # controller resolves the executing worker per attempt through the
    # deterministic V0 selection policy and records the selection trace in
    # evidence.  The deps.*_worker fields remain the direct pool-less path
    # (unchanged REDI-03 behavior) and the terminal fallback for reviewer
    # pools when the pool yields no eligible worker.
    builder_pool: WorkerPool | None = None
    builder_runners: Mapping[str, WorkerRunner] | None = None
    reviewer_pool: WorkerPool | None = None
    reviewer_runners: Mapping[str, WorkerRunner] | None = None
    # REDI-05B: runtime context compilation (canonical path for all stations).
    context_strategy: str = "selected_v0"
    context_budget_bytes: int = DEFAULT_BUDGET_BYTES
    # selected_v0 file selection (None = compiler default public sweep);
    # explicit globs restrict the selected input set for the strategy.
    builder_include_globs: Sequence[str] | None = None
    # Restricts the PUBLIC-verification sweep merged into every manifest
    # (None = default public file-type sweep over the bounded workspace).
    builder_public_test_globs: Sequence[str] | None = None


@dataclass
class FinalRunOutcome:
    factory_run_id: str
    final_status: RunStatus
    verified: bool
    events_emitted: int
    replay_ready: bool
    db_path: str
    attempts: int
    reviewer_verdict: ReviewerVerdict | None
    final_gate_count: int
    escalation: bool


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _artifact_from_patch(p: PatchEdit, source: str) -> ArtifactRef:
    return ArtifactRef(
        ref_id=p.rel_path.replace("/", "_").replace("\\", "_"),
        kind="patch",
        location=p.rel_path,
        sha256=p.content_sha256,
        source=source,
        metadata={"action": p.action, "size": len(p.content or "")},
    )


def _patches_to_dicts(patches: Sequence[PatchEdit]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in patches:
        out.append({
            "rel_path": p.rel_path,
            "action": p.action,
            "content": p.content,
            "content_sha256": p.content_sha256,
            "metadata": dict(p.metadata or {}),
        })
    return out


def _dicts_to_patches(items: Sequence[Mapping[str, Any]]) -> list[PatchEdit]:
    out: list[PatchEdit] = []
    for d in items:
        out.append(PatchEdit(
            rel_path=str(d["rel_path"]),
            action=str(d["action"]),
            content=d.get("content"),
            content_sha256=str(d.get("content_sha256") or ""),
            metadata=dict(d.get("metadata") or {}),
        ))
    return out


def _new_event(etype: LifecycleEventType, entity_ids: Mapping[str, str], payload: Mapping[str, Any]) -> LifecycleEvent:
    return LifecycleEvent(
        event_type=etype,
        entity_ids=dict(entity_ids),
        payload=dict(payload),
        timestamp=_utcnow(),
    )


def _safe_append(ledger: EvidenceLedger, fn: Callable[..., dict[str, Any]], *args: Any, **kwargs: Any) -> dict[str, Any] | None:
    try:
        return fn(ledger, *args, **kwargs)
    except DuplicateLedgerEntryError:
        return None


def _reconstruct_builder_attempts(conn: sqlite3.Connection, fr_id: str) -> list[dict[str, Any]]:
    """Reconstruct per-attempt builder history from persisted evidence only.

    Walks ordered ledger rows: each builder RunEvidence opens an attempt
    (worker id, selection evidence, patches); subsequent GateResult rows and
    GATE_PASSED/GATE_FAILED lifecycle events attach to it.
    """
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT global_sequence, entity_type, payload_json FROM evidence_entries "
            "WHERE factory_run_id = ? AND entity_type IN ('RunEvidence','GateResult','LifecycleEvent') "
            "ORDER BY global_sequence",
            (fr_id,),
        ).fetchall()
    finally:
        conn.row_factory = None
    attempts: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for r in rows:
        et = r["entity_type"]
        d = json.loads(r["payload_json"])
        if et == "RunEvidence" and d.get("node_id") == "N_BUILDER_0":
            md = d.get("metadata") or {}
            cur = {
                "attempt_index": int(md.get("attempt_index", len(attempts)) or 0),
                "worker_id": str(d.get("worker_id") or ""),
                "worker_selection": md.get("worker_selection"),
                "patches": _dicts_to_patches(md.get("patches_json") or []),
                "gate_results": [],
                "gate_outcome": None,
                "failed_gate": None,
                "failure_reasons": [],
            }
            attempts.append(cur)
        elif et == "GateResult" and cur is not None:
            cur["gate_results"].append(d)
        elif et == "LifecycleEvent" and cur is not None:
            if d.get("event_type") == "GATE_FAILED":
                p = d.get("payload") or {}
                cur["gate_outcome"] = "fail"
                cur["failed_gate"] = p.get("gate")
                cur["failure_reasons"] = list(p.get("reasons") or [])
            elif d.get("event_type") == "GATE_PASSED":
                cur["gate_outcome"] = "pass"
    return attempts


def _reconstruct_plan_payload(conn: sqlite3.Connection, fr_id: str) -> dict[str, Any] | None:
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT payload_json FROM evidence_entries "
            "WHERE factory_run_id = ? AND entity_type = 'LifecycleEvent' AND payload_json LIKE '%PLAN_COMPILED%' "
            "ORDER BY global_sequence LIMIT 1",
            (fr_id,),
        ).fetchone()
    finally:
        conn.row_factory = None
    if row is None:
        return None
    return json.loads(row["payload_json"]).get("payload") or {}


def _reconstruct_reviewer_state(conn: sqlite3.Connection, fr_id: str) -> dict[str, Any] | None:
    """Last persisted reviewer verdict evidence, or None if reviewer never ran."""
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT payload_json FROM evidence_entries "
            "WHERE factory_run_id = ? AND entity_type = 'RunEvidence' AND payload_json LIKE '%N_REVIEWER_0%' "
            "ORDER BY global_sequence DESC LIMIT 1",
            (fr_id,),
        ).fetchone()
    finally:
        conn.row_factory = None
    if row is None:
        return None
    d = json.loads(row["payload_json"])
    md = d.get("metadata") or {}
    verdict = md.get("verdict")
    try:
        verdict_obj = ReviewerVerdict(verdict) if verdict is not None else None
    except ValueError:
        verdict_obj = None
    return {
        "verdict": verdict_obj,
        "reasons": list(md.get("reasons") or []),
        "evidence_refs": list(md.get("evidence_refs") or []),
    }


def _persisted_builder_pool_snapshot(persisted_pools: Mapping[str, Any]) -> dict[str, Any]:
    return (persisted_pools.get("builder") or {}).get("snapshot") or {}


def _conn_for_path(db_path: str | Path | None) -> tuple[sqlite3.Connection, str, bool]:
    if db_path is None:
        tmp = tempfile.NamedTemporaryFile(prefix="redi03_ledger_", suffix=".db", delete=False)
        tmp.close()
        path = tmp.name
        temp_flag = True
    else:
        path = str(db_path)
        temp_flag = False
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON;")
    bootstrap_schema(conn)
    return conn, path, temp_flag


def _last_evidence_for(conn: sqlite3.Connection, fr_id: str) -> dict[str, Any] | None:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT ee.payload_json, ee.entity_id, ee.recorded_at
           FROM evidence_entries ee
           WHERE ee.entity_type = 'RunEvidence'
             AND ee.factory_run_id = ?
           ORDER BY ee.global_sequence DESC LIMIT 1""",
        (fr_id,),
    ).fetchall()
    if not rows:
        return None
    return {"payload_json": rows[0]["payload_json"], "entity_id": rows[0]["entity_id"]}


def _events_types_only(conn: sqlite3.Connection, fr_id: str) -> list[str]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT ee.payload_json
           FROM evidence_entries ee
           WHERE ee.entity_type = 'LifecycleEvent' AND ee.factory_run_id = ?
           ORDER BY ee.global_sequence ASC""",
        (fr_id,),
    ).fetchall()
    from ..domain import domain_from_json
    out: list[str] = []
    for r in rows:
        try:
            ev = domain_from_json(r["payload_json"])
            out.append(str(ev.event_type.value))
        except Exception:
            pass
    return out


def make_canonical_slug_fixture_refs(base: Path | None = None) -> dict[str, Any]:
    if base is None:
        base = Path(__file__).resolve().parents[2]
    base = Path(base)
    fx_root = base / "experiments" / "fixtures" / "slugify_pkg"
    hidden_root = base / "experiments" / "fixtures" / "slugify_pkg_hidden_tests"
    gate_spec = GateSpec(
        public_test_cmd=[sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", "tests/"],
        hidden_test_cmd=[sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"],
        hidden_test_cwd=hidden_root,
        hidden_test_env_extra={},
        per_gate_timeout_s=60.0,
        output_cap_bytes=65536,
        allowed_scope_prefixes=("slugify", "tests"),
    )
    return {
        "fixture_root": fx_root,
        "hidden_test_root": hidden_root,
        "allowed_scope_prefixes": ("slugify", "tests"),
        "gate_spec": gate_spec,
    }


class MinimalFactoryController:
    """Stateless deterministic factory-line controller.

    All state lives in the sqlite ledger; every public method is reproducible
    from the ledger alone. No in-memory caching crosses method calls.
    """

    @staticmethod
    def run(
        product: ProductSpec,
        task: TaskContract,
        deps: RunDeps,
        *,
        db_path: str | Path | None = None,
        experiment_id: str | None = None,
    ) -> FinalRunOutcome:
        conn, db_path_str, _temp = _conn_for_path(db_path)
        try:
            ledger = EvidenceLedger(conn)
            runs_repo = FactoryRunRepository(conn, ledger)

            run = FactoryRun(
                product_spec_id=product.id,
                dag_id="canonical_slug_fixture_line_v1",
                mode=Mode.PRODUCTION,
                final_status=RunStatus.PENDING,
                verified=False,
                metadata=dict(product.metadata or {}) | {"line": "minimal_v1"},
                created_at=_utcnow(),
            )
            runs_repo.store(run, experiment_id=experiment_id)
            fr_id = str(run.id)
            entity_ids = {"factory_run_id": fr_id, "product_id": str(product.id)}
            if experiment_id:
                entity_ids["experiment_id"] = experiment_id

            append_event(ledger, _new_event(LifecycleEventType.PRODUCT_ACCEPTED, entity_ids, {
                "product_id": str(product.id),
                "task_id": str(task.id),
                "mode": run.mode.value,
            }), factory_run_id=fr_id, experiment_id=experiment_id)
            # REDI-04B: one canonical pool-provenance snapshot per station,
            # persisted with the plan so resume/replay reconstruct historical
            # selection state instead of recomputing from current config.
            worker_pools_payload: dict[str, Any] = {}
            if deps.builder_pool is not None:
                snap = pool_snapshot(pool=deps.builder_pool, station=deps.builder_station, task=task)
                worker_pools_payload["builder"] = {
                    "snapshot": snap,
                    "snapshot_hash": pool_snapshot_hash(snap),
                }
            if deps.reviewer_pool is not None:
                snap = pool_snapshot(pool=deps.reviewer_pool, station=deps.reviewer_station, task=task)
                worker_pools_payload["reviewer"] = {
                    "snapshot": snap,
                    "snapshot_hash": pool_snapshot_hash(snap),
                }
            plan_payload: dict[str, Any] = {
                "task_id": str(task.id),
                "builder_station_id": str(deps.builder_station.id),
                "reviewer_station_id": str(deps.reviewer_station.id),
                "max_retries": deps.max_retries,
                "allowed_scope": list(deps.allowed_scope_prefixes),
                # REDI-05B: context strategy/budget persisted for drift-immune
                # resume (historical semantics win over later configuration).
                "context_strategy": deps.context_strategy,
                "context_budget_bytes": int(deps.context_budget_bytes),
            }
            if worker_pools_payload:
                plan_payload["worker_pools"] = worker_pools_payload
            append_event(ledger, _new_event(LifecycleEventType.PLAN_COMPILED, entity_ids, plan_payload), factory_run_id=fr_id, experiment_id=experiment_id)
            append_event(ledger, _new_event(LifecycleEventType.NODE_READY, entity_ids, {
                "node_id": "N_BUILDER_0",
                "station_role": "builder",
                "station_id": str(deps.builder_station.id),
            }), factory_run_id=fr_id, experiment_id=experiment_id)

            gate_spec = deps.gate_spec if deps.gate_spec is not None else GateSpec()
            started_wall = time.monotonic()
            total_tokens = TokenUsage()
            total_cost = MonetaryCost(amount=Decimal("0"))
            attempts = 0
            last_patches: list[PatchEdit] = []
            last_gate_results: list[GateResult] = []
            last_reviewer_verdict: ReviewerVerdict | None = None
            final_status = RunStatus.PENDING  # only set terminal on explicit branches
            verified_flag = False
            escalation_flag = False
            events_count = 3
            # REDI-03L6: bounded previous-failure summary fed to the next
            # builder attempt's prompt; None on attempt 0 (no feedback section).
            retry_feedback: dict[str, Any] | None = None

            max_attempts = max(1, int(deps.max_retries) + 1)
            budget_remaining = max_attempts
            # REDI-04A: worker-selection state for the pool path.
            excluded_builder_worker_ids: list[str] = []
            previous_builder_worker_id: str | None = None

            with IsolatedWorkspace.create(
                fixture_root=Path(deps.fixture_root),
                allowed_scope_prefixes=list(deps.allowed_scope_prefixes),
            ) as ws:
                baseline = baseline_file_hashes(ws.root)
                gate_spec.baseline_hashes = dict(baseline)
                if deps.hidden_test_root is not None and gate_spec.hidden_test_cwd is None:
                    gate_spec.hidden_test_cwd = Path(deps.hidden_test_root)

                for attempt_idx in range(max_attempts):
                    attempts = attempt_idx + 1
                    budget_remaining = max_attempts - attempts

                    ws.reset(Path(deps.fixture_root))
                    baseline = baseline_file_hashes(ws.root)
                    gate_spec.baseline_hashes = dict(baseline)

                    # REDI-04A: deterministic worker selection (pool path).
                    builder_runner = deps.builder_worker
                    builder_selection_evidence = None
                    if deps.builder_pool is not None:
                        sel = resolve_worker(
                            pool=deps.builder_pool,
                            runners=dict(deps.builder_runners or {}),
                            station=deps.builder_station,
                            task=task,
                            exclude_worker_ids=excluded_builder_worker_ids,
                            attempt_index=attempt_idx,
                            previous_worker_id=previous_builder_worker_id,
                        )
                        if sel is None and attempt_idx > 0:
                            # No alternative eligible worker remains — the
                            # policy degrades to a same-worker retry on the
                            # rank-first eligible worker.
                            sel = resolve_worker(
                                pool=deps.builder_pool,
                                runners=dict(deps.builder_runners or {}),
                                station=deps.builder_station,
                                task=task,
                                exclude_worker_ids=(),
                                attempt_index=attempt_idx,
                                previous_worker_id=previous_builder_worker_id,
                            )
                        if sel is None:
                            # Pool exhaustion: no eligible worker at all.
                            append_failure_record(ledger, FailureRecord(
                                category=FailureCategory.CAPABILITY_INSUFFICIENCY,
                                origin="worker_pool_exhausted",
                                node_id="N_BUILDER_0",
                                task_id=task.id,
                                factory_run_id=FactoryRunId(fr_id),
                                details={
                                    "attempt": attempt_idx,
                                    "policy_id": SELECTION_POLICY_ID,
                                    "policy_version": SELECTION_POLICY_VERSION,
                                    "excluded_worker_ids": list(excluded_builder_worker_ids),
                                    "previous_worker_id": previous_builder_worker_id,
                                },
                                severity=Severity.ERROR,
                                recoverable=False,
                                created_at=_utcnow(),
                            ), factory_run_id=fr_id, experiment_id=experiment_id)
                            append_event(ledger, _new_event(LifecycleEventType.WORKER_ESCALATION_REQUIRED, entity_ids, {
                                "escalation_level": "WORKER_ESCALATION",
                                "reason": "worker_pool_exhausted",
                                "attempt_index": attempt_idx,
                            }), factory_run_id=fr_id, experiment_id=experiment_id)
                            events_count += 1
                            escalation_flag = True
                            final_status = RunStatus.BLOCKED
                            break
                        builder_runner = sel.runner
                        builder_selection_evidence = sel.to_evidence()
                        # REDI-04B: lightweight provenance reference to the
                        # canonical pool snapshot persisted with the plan.
                        if "builder" in worker_pools_payload:
                            builder_selection_evidence["pool_id"] = worker_pools_payload["builder"]["snapshot"]["pool_id"]
                            builder_selection_evidence["pool_snapshot_hash"] = worker_pools_payload["builder"]["snapshot_hash"]
                        previous_builder_worker_id = str(sel.profile.id)

                    selected_worker_id = (
                        WorkerId(builder_selection_evidence["worker_id"])
                        if builder_selection_evidence is not None
                        else WorkerId(new_id("worker"))
                    )
                    started_worker_id = (
                        str(builder_selection_evidence["worker_id"])
                        if builder_selection_evidence is not None
                        else str(getattr(builder_runner, "worker_id", "w_builder_fake"))
                    )
                    started_payload: dict[str, Any] = {
                        "node_id": "N_BUILDER_0",
                        "station_role": "builder",
                        "worker_id": started_worker_id,
                        "attempt_index": attempt_idx,
                        "attempts_remaining": budget_remaining,
                        "case_id": deps.builder_case_id,
                    }
                    if builder_selection_evidence is not None:
                        started_payload["selection_kind"] = builder_selection_evidence["selection_kind"]
                        started_payload["selection_policy_id"] = builder_selection_evidence["policy_id"]
                    append_event(ledger, _new_event(LifecycleEventType.WORKER_STARTED, entity_ids, started_payload), factory_run_id=fr_id, experiment_id=experiment_id)
                    events_count += 1

                    ctx_manifest = build_builder_context(
                        task=task,
                        station=deps.builder_station,
                        product=product,
                        fixture_root=ws.root,
                        include_globs=(
                            list(deps.builder_include_globs)
                            if deps.builder_include_globs is not None
                            else None
                        ),
                        public_test_globs=(
                            list(deps.builder_public_test_globs)
                            if deps.builder_public_test_globs is not None
                            else None
                        ),
                        additional_sources=(),
                        allowed_scope=list(deps.allowed_scope_prefixes),
                        retry_feedback_category=(retry_feedback or {}).get("category"),
                        context_strategy_id=deps.context_strategy,
                    )
                    # REDI-05B: SINGLE CANONICAL CONTEXT PATH — compile through
                    # the firewall; budget and firewall violations fail closed
                    # BEFORE any worker invocation (early return, run closed).
                    bound_feedback = bound_retry_feedback(retry_feedback)
                    try:
                        context_package = compile_context(
                            strategy_id=deps.context_strategy,
                            station_role="builder",
                            task=task,
                            station=deps.builder_station,
                            product=product,
                            workspace_root=ws.root,
                            selected_paths=[r.ref_id for r in ctx_manifest.source_references],
                            allowed_scope=list(deps.allowed_scope_prefixes),
                            prior_failure_evidence=(
                                [{**bound_feedback, "context_sanitized": True}] if bound_feedback else []
                            ),
                            budget_bytes=deps.context_budget_bytes,
                        )
                    except (ContextBudgetError, ContextFirewallError) as exc:
                        append_failure_record(ledger, FailureRecord(
                            category=FailureCategory.CONTEXT_INSUFFICIENCY,
                            origin="context_compile_rejected",
                            node_id="N_BUILDER_0",
                            task_id=task.id,
                            factory_run_id=FactoryRunId(fr_id),
                            details={
                                "attempt": attempt_idx,
                                "strategy": deps.context_strategy,
                                "reason": str(exc),
                                "detail": type(exc).__name__,
                            },
                            severity=Severity.ERROR,
                            recoverable=False,
                            created_at=_utcnow(),
                        ), factory_run_id=fr_id, experiment_id=experiment_id)
                        append_event(ledger, _new_event(LifecycleEventType.WORKER_ESCALATION_REQUIRED, entity_ids, {
                            "escalation_level": "WORKER_ESCALATION",
                            "reason": "context_compile_rejected",
                            "attempt_index": attempt_idx,
                            "detail": type(exc).__name__,
                        }), factory_run_id=fr_id, experiment_id=experiment_id)
                        events_count += 1
                        wall_total = max(0.0, time.monotonic() - started_wall)
                        final_timing = Timing(
                            started_at=run.created_at,
                            ended_at=_utcnow(),
                            wall_clock_seconds=round(wall_total, 4),
                            compute_seconds=round(wall_total, 4),
                        )
                        try:
                            runs_repo.close_run(
                                run_id=fr_id,
                                final_status=RunStatus.BLOCKED,
                                verified=False,
                                total_cost=total_cost,
                                total_tokens=total_tokens,
                                timing=final_timing,
                            )
                        except ClosedRunError:
                            pass
                        conn.commit()
                        return FinalRunOutcome(
                            factory_run_id=fr_id,
                            final_status=RunStatus.BLOCKED,
                            verified=False,
                            events_emitted=events_count,
                            replay_ready=True,
                            db_path=db_path_str,
                            attempts=attempt_idx + 1,
                            reviewer_verdict=None,
                            final_gate_count=0,
                            escalation=True,
                        )
                    # REDI-03L6 render format, now derived FROM the package.
                    builder_prompt = {
                        "instructions": context_package.instructions,
                        "context_body": context_package.context_body,
                        "sha256": context_package.rendered_hash,
                    }

                    start_ts = time.monotonic()
                    builder_result: NodeRunResult = builder_runner.run_node(
                        task_id=str(task.id),
                        station_id=str(deps.builder_station.id),
                        worker_id=selected_worker_id,
                        context={
                            "manifest_id": str(ctx_manifest.id),
                            "product_id": str(product.id),
                            "task_id": str(task.id),
                            "attempt_index": attempt_idx,
                            "case_id": deps.builder_case_id,
                            "allowed_scope": list(deps.allowed_scope_prefixes),
                            "root": str(ws.root),
                            "instructions": builder_prompt["instructions"],
                            "context_body": builder_prompt["context_body"],
                        },
                        workspace_root=ws.root,
                        station_role="builder",
                    )
                    elapsed = max(0.0, time.monotonic() - start_ts)

                    patches = list(builder_result.patches or [])
                    last_patches = patches
                    artifact_refs = tuple(_artifact_from_patch(p, "output") for p in patches)

                    if builder_result.token_usage is not None:
                        total_tokens = TokenUsage(
                            input=(total_tokens.input or 0) + (builder_result.token_usage.input or 0),
                            output=(total_tokens.output or 0) + (builder_result.token_usage.output or 0),
                            cached_input=(total_tokens.cached_input or 0) + (builder_result.token_usage.cached_input or 0),
                            reasoning=(total_tokens.reasoning or 0) + (builder_result.token_usage.reasoning or 0),
                            other=dict(total_tokens.other) | dict(builder_result.token_usage.other or {}),
                        )

                    worker_completed_payload: dict[str, Any] = {
                        "node_id": "N_BUILDER_0",
                        "station_role": "builder",
                        "worker_id": str(builder_result.worker_id or getattr(deps.builder_worker, "worker_id", "w_builder_fake")),
                        "context_id": str(ctx_manifest.id),
                        "attempt_index": attempt_idx,
                        "elapsed_seconds": round(elapsed, 4),
                        "patch_count": len(patches),
                    }
                    if builder_result.error_message:
                        worker_completed_payload["error"] = builder_result.error_message
                    append_event(ledger, _new_event(LifecycleEventType.WORKER_COMPLETED, entity_ids, worker_completed_payload), factory_run_id=fr_id, experiment_id=experiment_id)
                    events_count += 1

                    run_ev = RunEvidence(
                        factory_run_id=FactoryRunId(fr_id),
                        product_id=product.id,
                        node_id="N_BUILDER_0",
                        station_id=deps.builder_station.id,
                        task_class="software_patch",
                        worker_id=builder_result.worker_id or WorkerId(new_id("worker")),
                        context_manifest_id=ctx_manifest.id,
                        token_usage=builder_result.token_usage,
                        timing=Timing(
                            started_at=run.created_at,
                            ended_at=_utcnow(),
                            wall_clock_seconds=round(elapsed, 4),
                            compute_seconds=round(elapsed, 4),
                        ),
                        artifacts_changed=artifact_refs,
                        retry_count=attempt_idx,
                        final_status=RunStatus.RUNNING,
                        metadata={
                            "patches_json": _patches_to_dicts(patches),
                            "attempt_index": attempt_idx,
                            "opaque_provider_ref": builder_result.opaque_provider_ref or "",
                            "builder_error": builder_result.error_message or "",
                            # REDI-03L6 context-evidence bindings: reproduce the
                            # exact bounded prompt from canonical state (P9).
                            "context_manifest_hash": ctx_manifest.deterministic_hash,
                            "builder_prompt_sha256": builder_prompt["sha256"],
                            "retry_feedback_summary": bound_retry_feedback(retry_feedback),
                            "context_sources": [r.ref_id for r in ctx_manifest.source_references],
                            # REDI-04A: worker selection identity/reasons.
                            "worker_selection": builder_selection_evidence,
                            # REDI-05B: compiled context package evidence.
                            "context_package": context_package.to_evidence(),
                        },
                        created_at=_utcnow(),
                    )
                    append_run_evidence(ledger, run_ev, factory_run_id=fr_id, experiment_id=experiment_id)

                    if deps.crash_inject_after_worker_completed:
                        conn.commit()
                        raise SimulatedCrash(
                            f"injected after WORKER_COMPLETED for run_id={fr_id} at attempt {attempt_idx}"
                        )

                    try:
                        ok, violations = apply_patches(ws, patches, allowed_scope=list(deps.allowed_scope_prefixes))
                    except ScopeViolation as sv:
                        ok, violations = False, [sv]

                    if not ok:
                        # REDI-03L3B: canonicalize at the factory/evidence boundary —
                        # only JSON primitives enter FailureRecord / LifecycleEvent.
                        violation_records = canonicalize_scope_violations(list(violations))
                        # REDI-03L6: bounded feedback for the next builder attempt.
                        retry_feedback = {
                            "attempt": attempt_idx,
                            "category": "scope_violation",
                            "boundary": "allowed_file_change_scope",
                            "offending_paths": sorted({
                                str(v.offending_path) for v in violations
                                if getattr(v, "offending_path", None)
                            }),
                        }
                        if deps.builder_pool is not None and builder_selection_evidence is not None:
                            excluded_builder_worker_ids.append(builder_selection_evidence["worker_id"])
                        append_failure_record(ledger, FailureRecord(
                            category=FailureCategory.SCOPE_VIOLATION,
                            origin="apply_patches_scope_check",
                            node_id="N_BUILDER_0",
                            task_id=task.id,
                            factory_run_id=FactoryRunId(fr_id),
                            details={"violations": violation_records, "attempt": attempt_idx},
                            severity=Severity.ERROR,
                            recoverable=bool(budget_remaining > 0),
                            created_at=_utcnow(),
                        ), factory_run_id=fr_id, experiment_id=experiment_id)
                        append_event(ledger, _new_event(LifecycleEventType.GATE_FAILED, entity_ids, {
                            "gate": "allowed_file_change_scope",
                            "gate_status": "FAIL",
                            "attempt_index": attempt_idx,
                            "reasons": list(violation_records),
                        }), factory_run_id=fr_id, experiment_id=experiment_id)
                        events_count += 1
                        if budget_remaining > 0:
                            append_event(ledger, _new_event(LifecycleEventType.RETRY_REQUESTED, entity_ids, {
                                "retry_index": attempt_idx + 1,
                                "budget_remaining": budget_remaining - 1,
                                "escalation_level": "RETRY_SAME",
                                "reason": "scope_violation",
                            }), factory_run_id=fr_id, experiment_id=experiment_id)
                            events_count += 1
                            continue
                        append_event(ledger, _new_event(LifecycleEventType.WORKER_ESCALATION_REQUIRED, entity_ids, {
                            "escalation_level": "WORKER_ESCALATION",
                            "reason": "scope_violation_retries_exhausted",
                            "attempt_index": attempt_idx,
                        }), factory_run_id=fr_id, experiment_id=experiment_id)
                        events_count += 1
                        escalation_flag = True
                        final_status = RunStatus.BLOCKED
                        break

                    gate_engine = DeterministicGateEngine(gate_spec)
                    gate_results: list[GateResult] = gate_engine.run_all(
                        ws.root,
                        target_node_id="N_BUILDER_0",
                        target_task_id=task.id,
                    )
                    last_gate_results = gate_results
                    mandatory_fail: GateResult | None = None
                    for g in gate_results:
                        append_gate_result(ledger, g, factory_run_id=fr_id, experiment_id=experiment_id)
                        if g.metadata.get("mandatory", True) and g.status.value != "pass":
                            mandatory_fail = mandatory_fail or g

                    if mandatory_fail is not None:
                        fail_payload = {
                            "gate": mandatory_fail.gate_identity,
                            "gate_status": mandatory_fail.status.value,
                            "attempt_index": attempt_idx,
                            "reasons": list(mandatory_fail.failure_reasons or []),
                            "all_gates": [
                                {"id": g.gate_identity, "status": g.status.value}
                                for g in gate_results
                            ],
                        }
                        append_event(ledger, _new_event(LifecycleEventType.GATE_FAILED, entity_ids, fail_payload), factory_run_id=fr_id, experiment_id=experiment_id)
                        events_count += 1
                        # REDI-03L6: bounded feedback for the next builder attempt.
                        # REDI-03L9: public gate failures additionally carry a
                        # bounded sanitized public diagnostic; hidden-gate
                        # failures stay opaque (no names/paths/assertions).
                        retry_feedback = {
                            "attempt": attempt_idx,
                            "category": "gate_failed",
                            "boundary": str(mandatory_fail.gate_identity),
                            "reasons": list(mandatory_fail.failure_reasons or []),
                        }
                        if str(mandatory_fail.gate_identity) == "public_test_execution":
                            diagnostic = public_failure_diagnostic(mandatory_fail, workspace_root=ws.root)
                            if diagnostic:
                                retry_feedback["diagnostic"] = diagnostic
                        elif str(mandatory_fail.gate_identity) == "hidden_test_execution":
                            retry_feedback["reasons"] = ["hidden verification failed"]
                        if deps.builder_pool is not None and builder_selection_evidence is not None:
                            excluded_builder_worker_ids.append(builder_selection_evidence["worker_id"])
                        if budget_remaining > 0:
                            append_event(ledger, _new_event(LifecycleEventType.RETRY_REQUESTED, entity_ids, {
                                "retry_index": attempt_idx + 1,
                                "budget_remaining": budget_remaining - 1,
                                "escalation_level": "RETRY_SAME",
                                "reason": f"gate_failed:{mandatory_fail.gate_identity}",
                                "failed_gate": mandatory_fail.gate_identity,
                            }), factory_run_id=fr_id, experiment_id=experiment_id)
                            events_count += 1
                            continue
                        append_event(ledger, _new_event(LifecycleEventType.WORKER_ESCALATION_REQUIRED, entity_ids, {
                            "escalation_level": "WORKER_ESCALATION",
                            "reason": f"gate_failed_retries_exhausted:{mandatory_fail.gate_identity}",
                            "attempt_index": attempt_idx,
                            "failed_gate": mandatory_fail.gate_identity,
                        }), factory_run_id=fr_id, experiment_id=experiment_id)
                        events_count += 1
                        escalation_flag = True
                        final_status = RunStatus.FAILED
                        break

                    append_event(ledger, _new_event(LifecycleEventType.GATE_PASSED, entity_ids, {
                        "attempt_index": attempt_idx,
                        "gates_passed": [g.gate_identity for g in gate_results],
                        "gate_count": len(gate_results),
                    }), factory_run_id=fr_id, experiment_id=experiment_id)
                    events_count += 1
                    break

                if final_status == RunStatus.PENDING and len(last_gate_results) == 0:
                    final_status = RunStatus.FAILED

                if final_status in (RunStatus.FAILED, RunStatus.BLOCKED):
                    pass
                else:
                    append_event(ledger, _new_event(LifecycleEventType.NODE_READY, entity_ids, {
                        "node_id": "N_REVIEWER_0",
                        "station_role": "reviewer",
                        "station_id": str(deps.reviewer_station.id),
                    }), factory_run_id=fr_id, experiment_id=experiment_id)
                    events_count += 1
                    append_event(ledger, _new_event(LifecycleEventType.WORKER_STARTED, entity_ids, {
                        "node_id": "N_REVIEWER_0",
                        "station_role": "reviewer",
                        "worker_id": str(getattr(deps.reviewer_worker, "worker_id", "w_reviewer_fake")),
                        "attempt_index": 0,
                        "case_id": deps.reviewer_case_id,
                    }), factory_run_id=fr_id, experiment_id=experiment_id)
                    events_count += 1

                    reviewer_context = build_reviewer_context(
                        product=product,
                        task=task,
                        station=deps.reviewer_station,
                        workspace_root_before=Path(deps.fixture_root),
                        patches_applied=last_patches,
                        deterministic_gate_results=last_gate_results,
                        allowed_scope_prefixes=list(deps.allowed_scope_prefixes),
                    )
                    # REDI-05B: reviewer context compiled through the same
                    # firewall/strategy path; role is pinned to the exact
                    # schema-selection token "reviewer".
                    gate_summaries = [
                            {"gate_identity": g.gate_identity,
                             "status": g.status.value,
                             "failure_reasons_count": len(list(g.failure_reasons or []))}
                            for g in last_gate_results
                    ]
                    try:
                        reviewer_package = compile_context(
                            strategy_id=deps.context_strategy,
                            station_role="reviewer",
                            task=task,
                            station=deps.reviewer_station,
                            product=product,
                            workspace_root=ws.root,
                            selected_paths=(),  # reviewer context carries no workspace files
                            changed_files=sorted({p.rel_path for p in last_patches}),
                            diff_text=str(reviewer_context.get("diff") or ""),
                            gate_summaries=gate_summaries,
                            allowed_scope=list(deps.allowed_scope_prefixes),
                            budget_bytes=deps.context_budget_bytes,
                        )
                    except (ContextBudgetError, ContextFirewallError) as exc:
                        append_failure_record(ledger, FailureRecord(
                            category=FailureCategory.CONTEXT_INSUFFICIENCY,
                            origin="context_compile_rejected_reviewer",
                            node_id="N_REVIEWER_0",
                            task_id=task.id,
                            factory_run_id=FactoryRunId(fr_id),
                            details={"strategy": deps.context_strategy, "reason": str(exc),
                                     "detail": type(exc).__name__},
                            severity=Severity.ERROR,
                            recoverable=False,
                            created_at=_utcnow(),
                        ), factory_run_id=fr_id, experiment_id=experiment_id)
                        wall_total = max(0.0, time.monotonic() - started_wall)
                        final_timing = Timing(
                            started_at=run.created_at,
                            ended_at=_utcnow(),
                            wall_clock_seconds=round(wall_total, 4),
                            compute_seconds=round(wall_total, 4),
                        )
                        try:
                            runs_repo.close_run(
                                run_id=fr_id,
                                final_status=RunStatus.FAILED,
                                verified=False,
                                total_cost=total_cost,
                                total_tokens=total_tokens,
                                timing=final_timing,
                            )
                        except ClosedRunError:
                            pass
                        conn.commit()
                        return FinalRunOutcome(
                            factory_run_id=fr_id,
                            final_status=RunStatus.FAILED,
                            verified=False,
                            events_emitted=events_count,
                            replay_ready=True,
                            db_path=db_path_str,
                            attempts=attempts,
                            reviewer_verdict=None,
                            final_gate_count=len(last_gate_results),
                            escalation=False,
                        )
                    reviewer_prompt = {
                        "instructions": reviewer_package.instructions,
                        "context_body": reviewer_package.context_body,
                        "sha256": reviewer_package.rendered_hash,
                    }

                    # REDI-04A: optional reviewer pool resolution (initial
                    # selection only; the configured deps.reviewer_worker is
                    # the terminal fallback when the pool yields nothing).
                    reviewer_runner = deps.reviewer_worker
                    reviewer_selection_evidence = None
                    if deps.reviewer_pool is not None:
                        rsel = resolve_worker(
                            pool=deps.reviewer_pool,
                            runners=dict(deps.reviewer_runners or {}),
                            station=deps.reviewer_station,
                            task=task,
                            attempt_index=0,
                        )
                        if rsel is not None:
                            reviewer_runner = rsel.runner
                            reviewer_selection_evidence = rsel.to_evidence()

                    rev_start = time.monotonic()
                    rev_result: NodeRunResult = reviewer_runner.run_node(
                        task_id=str(task.id),
                        station_id=str(deps.reviewer_station.id),
                        worker_id=WorkerId(new_id("worker")),
                        context={
                            "product_id": str(product.id),
                            "task_id": str(task.id),
                            "case_id": deps.reviewer_case_id,
                            "allowed_scope": list(deps.allowed_scope_prefixes),
                            **reviewer_context,
                            "role": "reviewer",
                            "instructions": reviewer_prompt["instructions"],
                            "context_body": reviewer_prompt["context_body"],
                        },
                        workspace_root=ws.root,
                        station_role="reviewer",
                    )
                    rev_elapsed = max(0.0, time.monotonic() - rev_start)
                    if rev_result.token_usage is not None:
                        total_tokens = TokenUsage(
                            input=(total_tokens.input or 0) + (rev_result.token_usage.input or 0),
                            output=(total_tokens.output or 0) + (rev_result.token_usage.output or 0),
                            cached_input=(total_tokens.cached_input or 0) + (rev_result.token_usage.cached_input or 0),
                            reasoning=(total_tokens.reasoning or 0) + (rev_result.token_usage.reasoning or 0),
                            other=dict(total_tokens.other) | dict(rev_result.token_usage.other or {}),
                        )
                    last_reviewer_verdict = rev_result.reviewer_verdict

                    append_event(ledger, _new_event(LifecycleEventType.WORKER_COMPLETED, entity_ids, {
                        "node_id": "N_REVIEWER_0",
                        "station_role": "reviewer",
                        "worker_id": str(rev_result.worker_id or "w_reviewer_fake"),
                        "verdict": (last_reviewer_verdict.value if last_reviewer_verdict else None),
                        "reasons": list(rev_result.reviewer_reasons or []),
                        "elapsed_seconds": round(rev_elapsed, 4),
                    }), factory_run_id=fr_id, experiment_id=experiment_id)
                    events_count += 1

                    gate_ids = tuple(g.id for g in last_gate_results)
                    rev_ev = RunEvidence(
                        factory_run_id=FactoryRunId(fr_id),
                        product_id=product.id,
                        node_id="N_REVIEWER_0",
                        station_id=deps.reviewer_station.id,
                        task_class="independent_review",
                        worker_id=rev_result.worker_id or WorkerId(new_id("worker")),
                        token_usage=rev_result.token_usage,
                        timing=Timing(
                            started_at=run.created_at,
                            ended_at=_utcnow(),
                            wall_clock_seconds=round(rev_elapsed, 4),
                            compute_seconds=round(rev_elapsed, 4),
                        ),
                        gate_results=gate_ids,
                        retry_count=0,
                        escalation=False,
                        final_status=RunStatus.RUNNING,
                        metadata={
                            "verdict": (last_reviewer_verdict.value if last_reviewer_verdict else None),
                            "reasons": list(rev_result.reviewer_reasons or []),
                            "evidence_refs": list(rev_result.reviewer_evidence_refs or []),
                            # REDI-03L6: deterministic identity of the exact
                            # reviewer context rendered for this call.
                            "reviewer_prompt_sha256": reviewer_prompt["sha256"],
                            # REDI-04A: worker selection identity/reasons.
                            "worker_selection": reviewer_selection_evidence,
                            # REDI-05B: compiled context package evidence.
                            "context_package": reviewer_package.to_evidence(),
                        },
                        created_at=_utcnow(),
                    )
                    append_run_evidence(ledger, rev_ev, factory_run_id=fr_id, experiment_id=experiment_id)

                    if last_reviewer_verdict == ReviewerVerdict.PASS:
                        append_event(ledger, _new_event(LifecycleEventType.NODE_COMPLETED, entity_ids, {
                            "node_id": "N_REVIEWER_0",
                            "station_role": "reviewer",
                            "final_node": True,
                        }), factory_run_id=fr_id, experiment_id=experiment_id)
                        events_count += 1
                        append_event(ledger, _new_event(LifecycleEventType.PRODUCT_VERIFIED, entity_ids, {
                            "reviewer_verdict": "PASS",
                            "attempts": attempts,
                            "gates": [g.gate_identity for g in last_gate_results],
                            "reviewer_reasons": list(rev_result.reviewer_reasons or []),
                        }), factory_run_id=fr_id, experiment_id=experiment_id)
                        events_count += 1
                        final_status = RunStatus.COMPLETED
                        verified_flag = True
                    elif last_reviewer_verdict == ReviewerVerdict.FAIL:
                        append_failure_record(ledger, FailureRecord(
                            category=FailureCategory.REVIEWER_REJECTION,
                            origin="reviewer_verdict",
                            node_id="N_REVIEWER_0",
                            task_id=task.id,
                            factory_run_id=FactoryRunId(fr_id),
                            details={
                                "reasons": list(rev_result.reviewer_reasons or []),
                                "evidence_refs": list(rev_result.reviewer_evidence_refs or []),
                            },
                            severity=Severity.ERROR,
                            recoverable=False,
                            created_at=_utcnow(),
                        ), factory_run_id=fr_id, experiment_id=experiment_id)
                        final_status = RunStatus.FAILED
                    elif last_reviewer_verdict == ReviewerVerdict.ESCALATE:
                        append_event(ledger, _new_event(LifecycleEventType.WORKER_ESCALATION_REQUIRED, entity_ids, {
                            "escalation_level": "ARCHITECTURAL_DECISION",
                            "reason": "reviewer_escalate",
                            "reviewer_reasons": list(rev_result.reviewer_reasons or []),
                        }), factory_run_id=fr_id, experiment_id=experiment_id)
                        events_count += 1
                        escalation_flag = True
                        final_status = RunStatus.BLOCKED

            wall_total = max(0.0, time.monotonic() - started_wall)
            final_timing = Timing(
                started_at=run.created_at,
                ended_at=_utcnow(),
                wall_clock_seconds=round(wall_total, 4),
                compute_seconds=round(wall_total, 4),
            )
            try:
                runs_repo.close_run(
                    run_id=fr_id,
                    final_status=final_status,
                    verified=verified_flag,
                    total_cost=total_cost,
                    total_tokens=total_tokens,
                    timing=final_timing,
                )
            except ClosedRunError:
                pass

            conn.commit()
            return FinalRunOutcome(
                factory_run_id=fr_id,
                final_status=final_status,
                verified=verified_flag,
                events_emitted=events_count,
                replay_ready=True,
                db_path=db_path_str,
                attempts=attempts,
                reviewer_verdict=last_reviewer_verdict,
                final_gate_count=len(last_gate_results),
                escalation=escalation_flag,
            )
        finally:
            try:
                conn.commit()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    @staticmethod
    def resume(
        factory_run_id: str,
        product: ProductSpec,
        task: TaskContract,
        deps: RunDeps,
        *,
        db_path: str | Path,
        experiment_id: str | None = None,
        builder_run_guard: Callable[[], None] | None = None,
        crash_inject_after_worker_completed: bool = False,
    ) -> FinalRunOutcome:
        """Resume a run after crash using only persisted ledger state.

        REDI-04B: continuation is history-driven — completed builder attempts
        are never re-executed; pending gates are evaluated; a persisted worker
        fallback continues with the next deterministic candidate from the
        PERSISTED pool snapshot (plan-time provenance), not from current
        configuration; ladder/budget exhaustion escalates exactly like run().
        """
        db_path_str = str(db_path)
        conn = sqlite3.connect(db_path_str)
        conn.execute("PRAGMA foreign_keys = ON;")
        bootstrap_schema(conn)
        try:
            ledger = EvidenceLedger(conn)
            runs_repo = FactoryRunRepository(conn, ledger)

            fr = runs_repo.read(factory_run_id)
            fr_id = str(fr.id)
            entity_ids = {"factory_run_id": fr_id, "product_id": str(product.id)}
            if experiment_id:
                entity_ids["experiment_id"] = experiment_id

            event_types = _events_types_only(conn, fr_id)

            # Check if run is already closed by probing SQL closed_at column (not payload_json)
            # to avoid side-effects from calling close_run() as a probe.
            conn.row_factory = sqlite3.Row
            probe_row = conn.execute(
                "SELECT final_status, verified, closed_at FROM factory_runs WHERE id = ?",
                (fr_id,),
            ).fetchone()
            if probe_row is None:
                raise KeyError(f"FactoryRun {fr_id!r} not found in SQL row")
            if probe_row["closed_at"] is not None:
                # Already closed; reconstruct outcome from SQL shadow columns, not immutable payload
                closed_status = probe_row["final_status"]
                closed_verified = bool(probe_row["verified"])
                try:
                    from ..domain.enums import RunStatus as _RS
                    final_status_out = next(s for s in _RS if s.value == closed_status)
                except StopIteration:
                    final_status_out = RunStatus.FAILED
                return FinalRunOutcome(
                    factory_run_id=fr_id,
                    final_status=final_status_out,
                    verified=closed_verified,
                    events_emitted=len(event_types),
                    replay_ready=True,
                    db_path=db_path_str,
                    attempts=0,
                    reviewer_verdict=None,
                    final_gate_count=0,
                    escalation=False,
                )
            conn.row_factory = None

            # REDI-04B: reconstruct builder attempt history, plan-time pool
            # provenance, and reviewer state from persisted evidence ONLY.
            # Historical run semantics win over later configuration changes.
            builder_attempts = _reconstruct_builder_attempts(conn, fr_id)
            plan_payload = _reconstruct_plan_payload(conn, fr_id)
            persisted_pools = (plan_payload or {}).get("worker_pools") or {}
            persisted_max_retries = (plan_payload or {}).get("max_retries")
            persisted_context_strategy = str((plan_payload or {}).get("context_strategy") or deps.context_strategy or "selected_v0")
            persisted_context_budget = (plan_payload or {}).get("context_budget_bytes")
            persisted_context_budget_bytes = int(persisted_context_budget) if persisted_context_budget else deps.context_budget_bytes
            max_attempts = max(
                1, int(persisted_max_retries if persisted_max_retries is not None else deps.max_retries) + 1
            )
            reviewer_history = _reconstruct_reviewer_state(conn, fr_id)

            budget_used = len(builder_attempts)
            attempt_idx = budget_used - 1
            excluded_builder_worker_ids = [a["worker_id"] for a in builder_attempts if a["gate_outcome"] == "fail"]
            previous_builder_worker_id = builder_attempts[-1]["worker_id"] if builder_attempts else None
            saved_patches = builder_attempts[-1]["patches"] if builder_attempts else []
            retry_feedback = None
            if builder_attempts and builder_attempts[-1]["gate_outcome"] == "fail":
                last_failed = builder_attempts[-1]
                retry_feedback = {
                    "attempt": last_failed["attempt_index"],
                    "category": "gate_failed",
                    "boundary": last_failed["failed_gate"],
                    "reasons": list(last_failed["failure_reasons"]),
                }
                failed_gate_store = next(
                    (g for g in reversed(last_failed["gate_results"]) if g.get("gate_identity") == last_failed["failed_gate"]),
                    None,
                )
                if failed_gate_store is not None:
                    diag = public_failure_diagnostic(failed_gate_store)
                    if diag:
                        retry_feedback["diagnostic"] = diag

            total_tokens = TokenUsage()
            total_cost = MonetaryCost(amount=Decimal("0"))
            events_count = len(event_types)
            last_gate_results: list[GateResult] = []
            last_reviewer_verdict: ReviewerVerdict | None = None
            final_status = RunStatus.PENDING  # only set terminal on explicit branches
            verified_flag = False
            escalation_flag = False
            attempts = budget_used
            started_wall = time.monotonic()

            gate_spec = deps.gate_spec if deps.gate_spec is not None else GateSpec()

            with IsolatedWorkspace.create(
                fixture_root=Path(deps.fixture_root),
                allowed_scope_prefixes=list(deps.allowed_scope_prefixes),
            ) as ws:
                baseline = baseline_file_hashes(ws.root)
                gate_spec.baseline_hashes = dict(baseline)
                if deps.hidden_test_root is not None and gate_spec.hidden_test_cwd is None:
                    gate_spec.hidden_test_cwd = Path(deps.hidden_test_root)

                # ------------------------------------------------------------------
                # REDI-04B: history-driven continuation.
                # 1) evaluate pending gates for a completed-but-unjudged attempt;
                # 2) continue the worker ladder (fallback / same-worker retry)
                #    while attempts remain failing and retry budget remains;
                # 3) escalation when the ladder/budget is exhausted.
                # Selection config comes from the PERSISTED pool snapshot.
                # ------------------------------------------------------------------
                gate_engine = DeterministicGateEngine(gate_spec)
                pending = builder_attempts[-1] if builder_attempts and builder_attempts[-1]["gate_outcome"] is None else None
                if pending is not None:
                    try:
                        ok, violations = apply_patches(ws, pending["patches"], allowed_scope=list(deps.allowed_scope_prefixes))
                    except ScopeViolation as sv:
                        ok, violations = False, [sv]
                    if not ok:
                        # REDI-03L3B: canonicalize at the factory/evidence boundary.
                        violation_records = canonicalize_scope_violations(list(violations))
                        append_failure_record(ledger, FailureRecord(
                            category=FailureCategory.SCOPE_VIOLATION,
                            origin="resume_apply_patches_scope_check",
                            node_id="N_BUILDER_0",
                            task_id=task.id,
                            factory_run_id=FactoryRunId(fr_id),
                            details={"violations": violation_records, "resumed": True},
                            severity=Severity.ERROR,
                            recoverable=False,
                            created_at=_utcnow(),
                        ), factory_run_id=fr_id, experiment_id=experiment_id)
                        _safe_append(ledger, append_event, _new_event(LifecycleEventType.GATE_FAILED, entity_ids, {
                            "gate": "allowed_file_change_scope_resume",
                            "gate_status": "FAIL",
                            "attempt_index": pending["attempt_index"],
                            "reasons": list(violation_records),
                            "resumed": True,
                        }), factory_run_id=fr_id, experiment_id=experiment_id)
                        events_count += 1
                        pending["gate_outcome"] = "fail"
                        pending["failed_gate"] = "allowed_file_change_scope"
                        pending["failure_reasons"] = list(violation_records)
                        excluded_builder_worker_ids = sorted(set(excluded_builder_worker_ids) | {pending["worker_id"]})
                    else:
                        if builder_run_guard is not None:
                            builder_run_guard()

                        gate_results = gate_engine.run_all(
                            ws.root,
                            target_node_id="N_BUILDER_0",
                            target_task_id=task.id,
                        )
                        last_gate_results = gate_results
                        mandatory_fail: GateResult | None = None
                        for g in gate_results:
                            _safe_append(ledger, append_gate_result, g, factory_run_id=fr_id, experiment_id=experiment_id)
                            if g.metadata.get("mandatory", True) and g.status.value != "pass":
                                mandatory_fail = mandatory_fail or g

                        if mandatory_fail is not None:
                            payload = {
                                "gate": mandatory_fail.gate_identity,
                                "gate_status": mandatory_fail.status.value,
                                "attempt_index": pending["attempt_index"],
                                "resumed": True,
                                "reasons": list(mandatory_fail.failure_reasons or []),
                            }
                            _safe_append(ledger, append_event, _new_event(LifecycleEventType.GATE_FAILED, entity_ids, payload), factory_run_id=fr_id, experiment_id=experiment_id)
                            events_count += 1
                            pending["gate_outcome"] = "fail"
                            pending["failed_gate"] = str(mandatory_fail.gate_identity)
                            pending["failure_reasons"] = list(mandatory_fail.failure_reasons or [])
                            if pending["worker_id"]:
                                excluded_builder_worker_ids = sorted(
                                    set(excluded_builder_worker_ids) | {pending["worker_id"]}
                                )
                        else:
                            _safe_append(ledger, append_event, _new_event(LifecycleEventType.GATE_PASSED, entity_ids, {
                                "attempt_index": pending["attempt_index"],
                                "gates_passed": [g.gate_identity for g in gate_results],
                                "gate_count": len(gate_results),
                                "resumed": True,
                            }), factory_run_id=fr_id, experiment_id=experiment_id)
                            events_count += 1
                            pending["gate_outcome"] = "pass"

                # --- continue the ladder while the last attempt keeps failing ---
                while (
                    builder_attempts
                    and builder_attempts[-1]["gate_outcome"] == "fail"
                    and len(builder_attempts) < max_attempts
                ):
                    attempt_idx = len(builder_attempts)
                    builder_runner = deps.builder_worker
                    builder_selection_evidence = None
                    pool_hash = None
                    if "builder" in persisted_pools:
                        pool_obj = pool_from_snapshot(_persisted_builder_pool_snapshot(persisted_pools))
                        pool_hash = persisted_pools["builder"]["snapshot_hash"]
                        sel = resolve_worker(
                            pool=pool_obj,
                            runners=dict(deps.builder_runners or {}),
                            station=deps.builder_station,
                            task=task,
                            exclude_worker_ids=excluded_builder_worker_ids,
                            attempt_index=attempt_idx,
                            previous_worker_id=previous_builder_worker_id,
                        )
                        if sel is None:
                            sel = resolve_worker(
                                pool=pool_obj,
                                runners=dict(deps.builder_runners or {}),
                                station=deps.builder_station,
                                task=task,
                                exclude_worker_ids=(),
                                attempt_index=attempt_idx,
                                previous_worker_id=previous_builder_worker_id,
                            )
                        if sel is None:
                            # Pool exhaustion: no eligible worker remains for
                            # this station/task under the persisted snapshot.
                            append_failure_record(ledger, FailureRecord(
                                category=FailureCategory.CAPABILITY_INSUFFICIENCY,
                                origin="worker_pool_exhausted",
                                node_id="N_BUILDER_0",
                                task_id=task.id,
                                factory_run_id=FactoryRunId(fr_id),
                                details={
                                    "attempt": attempt_idx,
                                    "policy_id": SELECTION_POLICY_ID,
                                    "policy_version": SELECTION_POLICY_VERSION,
                                    "excluded_worker_ids": list(excluded_builder_worker_ids),
                                    "previous_worker_id": previous_builder_worker_id,
                                    "resumed": True,
                                },
                                severity=Severity.ERROR,
                                recoverable=False,
                                created_at=_utcnow(),
                            ), factory_run_id=fr_id, experiment_id=experiment_id)
                            append_event(ledger, _new_event(LifecycleEventType.WORKER_ESCALATION_REQUIRED, entity_ids, {
                                "escalation_level": "WORKER_ESCALATION",
                                "reason": "worker_pool_exhausted",
                                "attempt_index": attempt_idx,
                                "resumed": True,
                            }), factory_run_id=fr_id, experiment_id=experiment_id)
                            events_count += 1
                            escalation_flag = True
                            final_status = RunStatus.BLOCKED
                            break
                        builder_runner = sel.runner
                        builder_selection_evidence = sel.to_evidence()
                        builder_selection_evidence["pool_id"] = persisted_pools["builder"]["snapshot"]["pool_id"]
                        builder_selection_evidence["pool_snapshot_hash"] = pool_hash
                        previous_builder_worker_id = str(sel.profile.id)

                    selected_worker_id = (
                        WorkerId(builder_selection_evidence["worker_id"])
                        if builder_selection_evidence is not None
                        else WorkerId(new_id("worker"))
                    )
                    started_payload: dict[str, Any] = {
                        "node_id": "N_BUILDER_0",
                        "station_role": "builder",
                        "worker_id": (
                            str(builder_selection_evidence["worker_id"])
                            if builder_selection_evidence is not None
                            else str(getattr(builder_runner, "worker_id", "w_builder_fake"))
                        ),
                        "attempt_index": attempt_idx,
                        "attempts_remaining": max_attempts - attempt_idx - 1,
                        "case_id": deps.builder_case_id,
                        "resumed": True,
                    }
                    if builder_selection_evidence is not None:
                        started_payload["selection_kind"] = builder_selection_evidence["selection_kind"]
                        started_payload["selection_policy_id"] = builder_selection_evidence["policy_id"]
                    append_event(ledger, _new_event(LifecycleEventType.WORKER_STARTED, entity_ids, started_payload), factory_run_id=fr_id, experiment_id=experiment_id)
                    events_count += 1

                    ctx_manifest = build_builder_context(
                        task=task,
                        station=deps.builder_station,
                        product=product,
                        fixture_root=ws.root,
                        include_globs=None,
                        public_test_globs=None,
                        additional_sources=(),
                        allowed_scope=list(deps.allowed_scope_prefixes),
                        retry_feedback_category=(retry_feedback or {}).get("category"),
                        context_strategy_id=persisted_context_strategy,
                    )
                    # REDI-05B: compile through the firewall using the
                    # PERSISTED strategy/budget (historical semantics win).
                    bound_feedback = bound_retry_feedback(retry_feedback)
                    try:
                        context_package = compile_context(
                            strategy_id=persisted_context_strategy,
                            station_role="builder",
                            task=task,
                            station=deps.builder_station,
                            product=product,
                            workspace_root=ws.root,
                            selected_paths=[r.ref_id for r in ctx_manifest.source_references],
                            allowed_scope=list(deps.allowed_scope_prefixes),
                            prior_failure_evidence=(
                                [{**bound_feedback, "context_sanitized": True}] if bound_feedback else []
                            ),
                            budget_bytes=persisted_context_budget_bytes,
                        )
                    except (ContextBudgetError, ContextFirewallError) as exc:
                        append_failure_record(ledger, FailureRecord(
                            category=FailureCategory.CONTEXT_INSUFFICIENCY,
                            origin="context_compile_rejected",
                            node_id="N_BUILDER_0",
                            task_id=task.id,
                            factory_run_id=FactoryRunId(fr_id),
                            details={
                                "attempt": attempt_idx,
                                "strategy": persisted_context_strategy,
                                "reason": str(exc),
                                "detail": type(exc).__name__,
                                "resumed": True,
                            },
                            severity=Severity.ERROR,
                            recoverable=False,
                            created_at=_utcnow(),
                        ), factory_run_id=fr_id, experiment_id=experiment_id)
                        append_event(ledger, _new_event(LifecycleEventType.WORKER_ESCALATION_REQUIRED, entity_ids, {
                            "escalation_level": "WORKER_ESCALATION",
                            "reason": "context_compile_rejected",
                            "attempt_index": attempt_idx,
                            "detail": type(exc).__name__,
                            "resumed": True,
                        }), factory_run_id=fr_id, experiment_id=experiment_id)
                        events_count += 1
                        wall_total = max(0.0, time.monotonic() - started_wall)
                        final_timing = Timing(
                            started_at=fr.created_at,
                            ended_at=_utcnow(),
                            wall_clock_seconds=round(wall_total, 4),
                            compute_seconds=round(wall_total, 4),
                        )
                        try:
                            runs_repo.close_run(
                                run_id=fr_id,
                                final_status=RunStatus.BLOCKED,
                                verified=False,
                                total_cost=total_cost,
                                total_tokens=total_tokens,
                                timing=final_timing,
                            )
                        except ClosedRunError:
                            pass
                        conn.commit()
                        return FinalRunOutcome(
                            factory_run_id=fr_id,
                            final_status=RunStatus.BLOCKED,
                            verified=False,
                            events_emitted=events_count,
                            replay_ready=True,
                            db_path=db_path_str,
                            attempts=budget_used + 1,
                            reviewer_verdict=None,
                            final_gate_count=0,
                            escalation=True,
                        )
                    builder_prompt = {
                        "instructions": context_package.instructions,
                        "context_body": context_package.context_body,
                        "sha256": context_package.rendered_hash,
                    }

                    start_ts = time.monotonic()
                    builder_result: NodeRunResult = builder_runner.run_node(
                        task_id=str(task.id),
                        station_id=str(deps.builder_station.id),
                        worker_id=selected_worker_id,
                        context={
                            "manifest_id": str(ctx_manifest.id),
                            "product_id": str(product.id),
                            "task_id": str(task.id),
                            "attempt_index": attempt_idx,
                            "case_id": deps.builder_case_id,
                            "allowed_scope": list(deps.allowed_scope_prefixes),
                            "root": str(ws.root),
                            "instructions": builder_prompt["instructions"],
                            "context_body": builder_prompt["context_body"],
                            "resumed": True,
                        },
                        workspace_root=ws.root,
                        station_role="builder",
                    )
                    elapsed = max(0.0, time.monotonic() - start_ts)
                    if builder_result.token_usage is not None:
                        total_tokens = TokenUsage(
                            input=(total_tokens.input or 0) + (builder_result.token_usage.input or 0),
                            output=(total_tokens.output or 0) + (builder_result.token_usage.output or 0),
                            cached_input=(total_tokens.cached_input or 0) + (builder_result.token_usage.cached_input or 0),
                            reasoning=(total_tokens.reasoning or 0) + (builder_result.token_usage.reasoning or 0),
                            other=dict(total_tokens.other) | dict(builder_result.token_usage.other or {}),
                        )

                    worker_completed_payload: dict[str, Any] = {
                        "node_id": "N_BUILDER_0",
                        "station_role": "builder",
                        "worker_id": str(builder_result.worker_id or getattr(builder_runner, "worker_id", "w_builder_fake")),
                        "context_id": str(ctx_manifest.id),
                        "attempt_index": attempt_idx,
                        "elapsed_seconds": round(elapsed, 4),
                        "patch_count": len(list(builder_result.patches or [])),
                        "resumed": True,
                    }
                    if builder_result.error_message:
                        worker_completed_payload["error"] = builder_result.error_message
                    append_event(ledger, _new_event(LifecycleEventType.WORKER_COMPLETED, entity_ids, worker_completed_payload), factory_run_id=fr_id, experiment_id=experiment_id)
                    events_count += 1

                    patches = list(builder_result.patches or [])
                    artifact_refs = tuple(_artifact_from_patch(p, "output") for p in patches)
                    run_ev = RunEvidence(
                        factory_run_id=FactoryRunId(fr_id),
                        product_id=product.id,
                        node_id="N_BUILDER_0",
                        station_id=deps.builder_station.id,
                        task_class="software_patch",
                        worker_id=builder_result.worker_id or selected_worker_id,
                        context_manifest_id=ctx_manifest.id,
                        token_usage=builder_result.token_usage,
                        timing=Timing(
                            started_at=_utcnow(),
                            ended_at=_utcnow(),
                            wall_clock_seconds=round(elapsed, 4),
                            compute_seconds=round(elapsed, 4),
                        ),
                        artifacts_changed=artifact_refs,
                        retry_count=attempt_idx,
                        final_status=RunStatus.RUNNING,
                        metadata={
                            "patches_json": _patches_to_dicts(patches),
                            "attempt_index": attempt_idx,
                            "opaque_provider_ref": builder_result.opaque_provider_ref or "",
                            "builder_error": builder_result.error_message or "",
                            "context_manifest_hash": ctx_manifest.deterministic_hash,
                            "builder_prompt_sha256": builder_prompt["sha256"],
                            "retry_feedback_summary": bound_retry_feedback(retry_feedback),
                            "context_sources": [r.ref_id for r in ctx_manifest.source_references],
                            "worker_selection": builder_selection_evidence,
                            # REDI-05B: compiled context package evidence.
                            "context_package": context_package.to_evidence(),
                        },
                        created_at=_utcnow(),
                    )
                    append_run_evidence(ledger, run_ev, factory_run_id=fr_id, experiment_id=experiment_id)

                    if deps.crash_inject_after_worker_completed:
                        conn.commit()
                        raise SimulatedCrash(
                            f"injected after resumed WORKER_COMPLETED for run_id={fr_id} at attempt {attempt_idx}"
                        )

                    try:
                        ok, violations = apply_patches(ws, patches, allowed_scope=list(deps.allowed_scope_prefixes))
                    except ScopeViolation as sv:
                        ok, violations = False, [sv]

                    if not ok:
                        violation_records = canonicalize_scope_violations(list(violations))
                        if builder_selection_evidence is not None:
                            excluded_builder_worker_ids.append(builder_selection_evidence["worker_id"])
                        retry_feedback = {
                            "attempt": attempt_idx,
                            "category": "scope_violation",
                            "boundary": "allowed_file_change_scope",
                            "offending_paths": sorted({
                                str(v.offending_path) for v in violations
                                if getattr(v, "offending_path", None)
                            }),
                        }
                        append_failure_record(ledger, FailureRecord(
                            category=FailureCategory.SCOPE_VIOLATION,
                            origin="apply_patches_scope_check",
                            node_id="N_BUILDER_0",
                            task_id=task.id,
                            factory_run_id=FactoryRunId(fr_id),
                            details={"violations": violation_records, "attempt": attempt_idx, "resumed": True},
                            severity=Severity.ERROR,
                            recoverable=bool(len(builder_attempts) < max_attempts),
                            created_at=_utcnow(),
                        ), factory_run_id=fr_id, experiment_id=experiment_id)
                        append_event(ledger, _new_event(LifecycleEventType.GATE_FAILED, entity_ids, {
                            "gate": "allowed_file_change_scope",
                            "gate_status": "FAIL",
                            "attempt_index": attempt_idx,
                            "reasons": list(violation_records),
                            "resumed": True,
                        }), factory_run_id=fr_id, experiment_id=experiment_id)
                        events_count += 1
                        builder_attempts.append({
                            "attempt_index": attempt_idx,
                            "worker_id": str(selected_worker_id),
                            "worker_selection": builder_selection_evidence,
                            "patches": patches,
                            "gate_results": [],
                            "gate_outcome": "fail",
                            "failed_gate": "allowed_file_change_scope",
                            "failure_reasons": list(violation_records),
                        })
                        continue

                    gate_results = gate_engine.run_all(
                        ws.root,
                        target_node_id="N_BUILDER_0",
                        target_task_id=task.id,
                    )
                    last_gate_results = gate_results
                    mandatory_fail = None
                    for g in gate_results:
                        append_gate_result(ledger, g, factory_run_id=fr_id, experiment_id=experiment_id)
                        if g.metadata.get("mandatory", True) and g.status.value != "pass":
                            mandatory_fail = mandatory_fail or g

                    if mandatory_fail is not None:
                        append_event(ledger, _new_event(LifecycleEventType.GATE_FAILED, entity_ids, {
                            "gate": mandatory_fail.gate_identity,
                            "gate_status": mandatory_fail.status.value,
                            "attempt_index": attempt_idx,
                            "resumed": True,
                            "reasons": list(mandatory_fail.failure_reasons or []),
                            "all_gates": [
                                {"id": g.gate_identity, "status": g.status.value}
                                for g in gate_results
                            ],
                        }), factory_run_id=fr_id, experiment_id=experiment_id)
                        events_count += 1
                        retry_feedback = {
                            "attempt": attempt_idx,
                            "category": "gate_failed",
                            "boundary": str(mandatory_fail.gate_identity),
                            "reasons": list(mandatory_fail.failure_reasons or []),
                        }
                        if str(mandatory_fail.gate_identity) == "public_test_execution":
                            diagnostic = public_failure_diagnostic(mandatory_fail, workspace_root=ws.root)
                            if diagnostic:
                                retry_feedback["diagnostic"] = diagnostic
                        elif str(mandatory_fail.gate_identity) == "hidden_test_execution":
                            retry_feedback["reasons"] = ["hidden verification failed"]
                        if builder_selection_evidence is not None:
                            excluded_builder_worker_ids.append(builder_selection_evidence["worker_id"])
                        builder_attempts.append({
                            "attempt_index": attempt_idx,
                            "worker_id": str(selected_worker_id),
                            "worker_selection": builder_selection_evidence,
                            "patches": patches,
                            "gate_results": [],
                            "gate_outcome": "fail",
                            "failed_gate": str(mandatory_fail.gate_identity),
                            "failure_reasons": list(mandatory_fail.failure_reasons or []),
                        })
                        continue

                    append_event(ledger, _new_event(LifecycleEventType.GATE_PASSED, entity_ids, {
                        "attempt_index": attempt_idx,
                        "gates_passed": [g.gate_identity for g in gate_results],
                        "gate_count": len(gate_results),
                        "resumed": True,
                    }), factory_run_id=fr_id, experiment_id=experiment_id)
                    events_count += 1
                    builder_attempts.append({
                        "attempt_index": attempt_idx,
                        "worker_id": str(selected_worker_id),
                        "worker_selection": builder_selection_evidence,
                        "patches": patches,
                        "gate_results": [],
                        "gate_outcome": "pass",
                        "failed_gate": None,
                        "failure_reasons": [],
                    })
                    saved_patches = patches
                    break

                if builder_attempts and builder_attempts[-1]["gate_outcome"] == "fail":
                    # Ladder/budget exhausted on a failed attempt → existing escalation.
                    # (Pool exhaustion above already escalated as BLOCKED.)
                    if final_status == RunStatus.PENDING:
                        last_failed = builder_attempts[-1]
                        _safe_append(ledger, append_event, _new_event(LifecycleEventType.WORKER_ESCALATION_REQUIRED, entity_ids, {
                            "escalation_level": "WORKER_ESCALATION",
                            "reason": f"gate_failed_on_resume:{last_failed['failed_gate']}",
                            "failed_gate": last_failed["failed_gate"],
                            "attempt_index": last_failed["attempt_index"],
                            "resumed": True,
                        }), factory_run_id=fr_id, experiment_id=experiment_id)
                        events_count += 1
                        escalation_flag = True
                        final_status = RunStatus.FAILED

                # REDI-04B: a reviewer that already produced evidence is never
                # re-invoked; the terminal state is reconstructed from it.
                if reviewer_history is not None:
                    last_reviewer_verdict = reviewer_history["verdict"]
                    if last_reviewer_verdict == ReviewerVerdict.PASS:
                        _safe_append(ledger, append_event, _new_event(LifecycleEventType.NODE_COMPLETED, entity_ids, {
                            "node_id": "N_REVIEWER_0", "station_role": "reviewer", "final_node": True, "resumed": True,
                        }), factory_run_id=fr_id, experiment_id=experiment_id)
                        events_count += 1
                        _safe_append(ledger, append_event, _new_event(LifecycleEventType.PRODUCT_VERIFIED, entity_ids, {
                            "reviewer_verdict": "PASS",
                            "attempts": attempts,
                            "gates": [g.gate_identity for g in last_gate_results],
                            "resumed": True,
                            "reviewer_reasons": reviewer_history["reasons"],
                        }), factory_run_id=fr_id, experiment_id=experiment_id)
                        events_count += 1
                        final_status = RunStatus.COMPLETED
                        verified_flag = True
                    elif last_reviewer_verdict == ReviewerVerdict.FAIL:
                        final_status = RunStatus.FAILED
                    else:
                        escalation_flag = True
                        final_status = RunStatus.BLOCKED

                if final_status not in (RunStatus.FAILED, RunStatus.BLOCKED) and reviewer_history is None:
                    _safe_append(ledger, append_event, _new_event(LifecycleEventType.NODE_READY, entity_ids, {
                        "node_id": "N_REVIEWER_0",
                        "station_role": "reviewer",
                        "station_id": str(deps.reviewer_station.id),
                        "resumed": True,
                    }), factory_run_id=fr_id, experiment_id=experiment_id)
                    events_count += 1
                    _safe_append(ledger, append_event, _new_event(LifecycleEventType.WORKER_STARTED, entity_ids, {
                        "node_id": "N_REVIEWER_0",
                        "station_role": "reviewer",
                        "worker_id": str(getattr(deps.reviewer_worker, "worker_id", "w_reviewer_fake")),
                        "attempt_index": 0,
                        "case_id": deps.reviewer_case_id,
                        "resumed": True,
                    }), factory_run_id=fr_id, experiment_id=experiment_id)
                    events_count += 1

                    reviewer_context = build_reviewer_context(
                        product=product,
                        task=task,
                        station=deps.reviewer_station,
                        workspace_root_before=Path(deps.fixture_root),
                        patches_applied=saved_patches,
                        deterministic_gate_results=last_gate_results,
                        allowed_scope_prefixes=list(deps.allowed_scope_prefixes),
                    )
                    # REDI-03L6: same bounded reviewer prompt wiring as run().
                    # REDI-05B: reviewer context compiled through the
                    # PERSISTED strategy/budget; fail-closed before invocation.
                    gate_summaries = [
                            {"gate_identity": g.gate_identity,
                             "status": g.status.value,
                             "failure_reasons_count": len(list(g.failure_reasons or []))}
                            for g in last_gate_results
                    ]
                    try:
                        reviewer_package = compile_context(
                            strategy_id=persisted_context_strategy,
                            station_role="reviewer",
                            task=task,
                            station=deps.reviewer_station,
                            product=product,
                            workspace_root=ws.root,
                            selected_paths=(),
                            changed_files=sorted({p.rel_path for p in saved_patches}),
                            diff_text=str(reviewer_context.get("diff") or ""),
                            gate_summaries=gate_summaries,
                            allowed_scope=list(deps.allowed_scope_prefixes),
                            budget_bytes=persisted_context_budget_bytes,
                        )
                    except (ContextBudgetError, ContextFirewallError) as exc:
                        append_failure_record(ledger, FailureRecord(
                            category=FailureCategory.CONTEXT_INSUFFICIENCY,
                            origin="context_compile_rejected_reviewer",
                            node_id="N_REVIEWER_0",
                            task_id=task.id,
                            factory_run_id=FactoryRunId(fr_id),
                            details={"strategy": persisted_context_strategy, "reason": str(exc),
                                     "detail": type(exc).__name__, "resumed": True},
                            severity=Severity.ERROR,
                            recoverable=False,
                            created_at=_utcnow(),
                        ), factory_run_id=fr_id, experiment_id=experiment_id)
                        wall_total = max(0.0, time.monotonic() - started_wall)
                        final_timing = Timing(
                            started_at=fr.created_at,
                            ended_at=_utcnow(),
                            wall_clock_seconds=round(wall_total, 4),
                            compute_seconds=round(wall_total, 4),
                        )
                        try:
                            runs_repo.close_run(
                                run_id=fr_id,
                                final_status=RunStatus.FAILED,
                                verified=False,
                                total_cost=total_cost,
                                total_tokens=total_tokens,
                                timing=final_timing,
                            )
                        except ClosedRunError:
                            pass
                        conn.commit()
                        return FinalRunOutcome(
                            factory_run_id=fr_id,
                            final_status=RunStatus.FAILED,
                            verified=False,
                            events_emitted=events_count,
                            replay_ready=True,
                            db_path=db_path_str,
                            attempts=budget_used,
                            reviewer_verdict=None,
                            final_gate_count=len(last_gate_results),
                            escalation=False,
                        )
                    reviewer_prompt = {
                        "instructions": reviewer_package.instructions,
                        "context_body": reviewer_package.context_body,
                        "sha256": reviewer_package.rendered_hash,
                    }
                    # REDI-04A/04B: optional reviewer pool resolution — the
                    # persisted plan-time snapshot wins over current config;
                    # deps.reviewer_worker is the terminal fallback.
                    reviewer_runner = deps.reviewer_worker
                    reviewer_selection_evidence = None
                    persisted_reviewer_pool = persisted_pools.get("reviewer")
                    if persisted_reviewer_pool is not None or deps.reviewer_pool is not None:
                        pool_obj = (
                            pool_from_snapshot(persisted_reviewer_pool["snapshot"])
                            if persisted_reviewer_pool is not None
                            else deps.reviewer_pool
                        )
                        rsel = resolve_worker(
                            pool=pool_obj,
                            runners=dict(deps.reviewer_runners or {}),
                            station=deps.reviewer_station,
                            task=task,
                            attempt_index=0,
                        )
                        if rsel is not None:
                            reviewer_runner = rsel.runner
                            reviewer_selection_evidence = rsel.to_evidence()
                            if persisted_reviewer_pool is not None:
                                reviewer_selection_evidence["pool_id"] = persisted_reviewer_pool["snapshot"]["pool_id"]
                                reviewer_selection_evidence["pool_snapshot_hash"] = persisted_reviewer_pool["snapshot_hash"]
                    rev_start = time.monotonic()
                    rev_result = reviewer_runner.run_node(
                        task_id=str(task.id),
                        station_id=str(deps.reviewer_station.id),
                        worker_id=WorkerId(new_id("worker")),
                        context={
                            "product_id": str(product.id),
                            "task_id": str(task.id),
                            "case_id": deps.reviewer_case_id,
                            "allowed_scope": list(deps.allowed_scope_prefixes),
                            "resumed": True,
                            **reviewer_context,
                            "role": "reviewer",
                            "instructions": reviewer_prompt["instructions"],
                            "context_body": reviewer_prompt["context_body"],
                        },
                        workspace_root=ws.root,
                        station_role="reviewer",
                    )
                    rev_elapsed = max(0.0, time.monotonic() - rev_start)
                    if rev_result.token_usage is not None:
                        total_tokens = TokenUsage(
                            input=(total_tokens.input or 0) + (rev_result.token_usage.input or 0),
                            output=(total_tokens.output or 0) + (rev_result.token_usage.output or 0),
                        )
                    last_reviewer_verdict = rev_result.reviewer_verdict

                    _safe_append(ledger, append_event, _new_event(LifecycleEventType.WORKER_COMPLETED, entity_ids, {
                        "node_id": "N_REVIEWER_0",
                        "station_role": "reviewer",
                        "worker_id": str(rev_result.worker_id or "w_reviewer_fake"),
                        "verdict": (last_reviewer_verdict.value if last_reviewer_verdict else None),
                        "reasons": list(rev_result.reviewer_reasons or []),
                        "elapsed_seconds": round(rev_elapsed, 4),
                        "resumed": True,
                    }), factory_run_id=fr_id, experiment_id=experiment_id)
                    events_count += 1

                    # REDI-04B: persist the reviewer RunEvidence on resume too
                    # (previously missing — resume reviewer output was only in
                    # lifecycle events, breaking evidence symmetry with run()).
                    gate_ids = tuple(g.id for g in last_gate_results)
                    rev_ev = RunEvidence(
                        factory_run_id=FactoryRunId(fr_id),
                        product_id=product.id,
                        node_id="N_REVIEWER_0",
                        station_id=deps.reviewer_station.id,
                        task_class="independent_review",
                        worker_id=rev_result.worker_id or WorkerId(new_id("worker")),
                        token_usage=rev_result.token_usage,
                        timing=Timing(
                            started_at=_utcnow(),
                            ended_at=_utcnow(),
                            wall_clock_seconds=round(rev_elapsed, 4),
                            compute_seconds=round(rev_elapsed, 4),
                        ),
                        gate_results=gate_ids,
                        retry_count=0,
                        escalation=False,
                        final_status=RunStatus.RUNNING,
                        metadata={
                            "verdict": (last_reviewer_verdict.value if last_reviewer_verdict else None),
                            "reasons": list(rev_result.reviewer_reasons or []),
                            "evidence_refs": list(rev_result.reviewer_evidence_refs or []),
                            "reviewer_prompt_sha256": reviewer_prompt["sha256"],
                            "worker_selection": reviewer_selection_evidence,
                            # REDI-05B: compiled context package evidence.
                            "context_package": reviewer_package.to_evidence(),
                            "resumed": True,
                        },
                        created_at=_utcnow(),
                    )
                    _safe_append(ledger, append_run_evidence, rev_ev, factory_run_id=fr_id, experiment_id=experiment_id)

                    if last_reviewer_verdict == ReviewerVerdict.PASS:
                        _safe_append(ledger, append_event, _new_event(LifecycleEventType.NODE_COMPLETED, entity_ids, {
                            "node_id": "N_REVIEWER_0", "station_role": "reviewer", "final_node": True, "resumed": True,
                        }), factory_run_id=fr_id, experiment_id=experiment_id)
                        events_count += 1
                        _safe_append(ledger, append_event, _new_event(LifecycleEventType.PRODUCT_VERIFIED, entity_ids, {
                            "reviewer_verdict": "PASS",
                            "attempts": attempts,
                            "gates": [g.gate_identity for g in last_gate_results],
                            "resumed": True,
                            "reviewer_reasons": list(rev_result.reviewer_reasons or []),
                        }), factory_run_id=fr_id, experiment_id=experiment_id)
                        events_count += 1
                        final_status = RunStatus.COMPLETED
                        verified_flag = True
                    elif last_reviewer_verdict == ReviewerVerdict.FAIL:
                        _safe_append(ledger, append_failure_record, FailureRecord(
                            category=FailureCategory.REVIEWER_REJECTION,
                            origin="reviewer_verdict_resume",
                            node_id="N_REVIEWER_0",
                            task_id=task.id,
                            factory_run_id=FactoryRunId(fr_id),
                            details={"reasons": list(rev_result.reviewer_reasons or []), "resumed": True},
                            severity=Severity.ERROR,
                            recoverable=False,
                            created_at=_utcnow(),
                        ), factory_run_id=fr_id, experiment_id=experiment_id)
                        final_status = RunStatus.FAILED
                    elif last_reviewer_verdict == ReviewerVerdict.ESCALATE:
                        _safe_append(ledger, append_event, _new_event(LifecycleEventType.WORKER_ESCALATION_REQUIRED, entity_ids, {
                            "escalation_level": "ARCHITECTURAL_DECISION",
                            "reason": "reviewer_escalate_resume",
                            "reviewer_reasons": list(rev_result.reviewer_reasons or []),
                            "resumed": True,
                        }), factory_run_id=fr_id, experiment_id=experiment_id)
                        events_count += 1
                        escalation_flag = True
                        final_status = RunStatus.BLOCKED

            wall_total = max(0.0, time.monotonic() - started_wall)
            final_timing = Timing(
                started_at=fr.created_at,
                ended_at=_utcnow(),
                wall_clock_seconds=round(wall_total, 4),
                compute_seconds=round(wall_total, 4),
            )
            try:
                runs_repo.close_run(
                    run_id=fr_id,
                    final_status=final_status,
                    verified=verified_flag,
                    total_cost=total_cost,
                    total_tokens=total_tokens,
                    timing=final_timing,
                )
            except ClosedRunError:
                pass

            conn.commit()
            return FinalRunOutcome(
                factory_run_id=fr_id,
                final_status=final_status,
                verified=verified_flag,
                events_emitted=events_count,
                replay_ready=True,
                db_path=db_path_str,
                attempts=attempts,
                reviewer_verdict=last_reviewer_verdict,
                final_gate_count=len(last_gate_results),
                escalation=escalation_flag,
            )
        finally:
            try:
                conn.commit()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass


__all__ = [
    "FinalRunOutcome",
    "MinimalFactoryController",
    "RunDeps",
    "SimulatedCrash",
    "make_canonical_slug_fixture_refs",
]
