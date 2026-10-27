"""REDI-05C — paired selected_v0 vs full_public_v0 context experiment harness.

Pairing contract (one pair = two factory runs):
    * identical ProductSpec / TaskContract / StationContract objects,
    * identical worker configuration, retry policy, gate spec, Reviewer,
    * identical bounded experiment fixture (slugify_wide_pkg),
    * ONLY the context strategy differs:
        Condition S — selected_v0 (relevant public files only),
        Condition F — full_public_v0 (all eligible PUBLIC files incl.
                      the controlled distractors).

Both runs bind to one ExperimentManifest id (the pair identity).  Order is
recorded explicitly; the harness supports either order for later
counterbalancing.  This is PILOT / FEASIBILITY instrumentation: it records
per-condition measurements without collapsing them into a score and makes
no H2 claim — formal inference requires repeated paired trials under the
experiment policy.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..domain import ExperimentManifest

EXPERIMENT_HYPOTHESIS_REFERENCE = "H2"
EXPERIMENT_TITLE = (
    "REDI-05C pilot: selected_v0 vs full_public_v0 context strategy "
    "(paired, single-pair feasibility)"
)
CONDITION_SELECTED = "selected_v0"
CONDITION_FULL_PUBLIC = "full_public_v0"
DEFAULT_ORDER = (CONDITION_SELECTED, CONDITION_FULL_PUBLIC)
PILOT_LABEL = "PILOT / FEASIBILITY EVIDENCE ONLY"


def experiment_fixture_root() -> Path:
    return Path(__file__).resolve().parents[2] / "experiments" / "fixtures" / "slugify_wide_pkg"


def build_experiment_manifest(*, model: str = "gpt-4o-mini", experiment_id=None) -> ExperimentManifest:
    """Preregistration manifest for the paired context-strategy pilot."""
    from ..domain import ExperimentId

    kwargs: dict[str, Any] = {}
    if experiment_id is not None:
        kwargs["id"] = experiment_id
    return ExperimentManifest(
        title=EXPERIMENT_TITLE,
        hypothesis_reference=EXPERIMENT_HYPOTHESIS_REFERENCE,
        research_question=(
            "Does bounded selected context reduce prompt size versus "
            "full-public context without hurting task success? (feasibility)"
        ),
        arms={
            "S": {"context_strategy": CONDITION_SELECTED, "strategy_version": 1},
            "F": {"context_strategy": CONDITION_FULL_PUBLIC, "strategy_version": 1},
        },
        context_strategy={
            "strategies": [CONDITION_SELECTED, CONDITION_FULL_PUBLIC],
            "version": 1,
            "note": "same firewall; strategy is the ONLY experimental variable",
        },
        worker_declarations={
            "builder": {"role": "builder", "provider_opaque_reference": f"openai:{model}",
                        "same_worker_across_conditions": True},
            "reviewer": {"role": "reviewer", "kind": "independent deterministic reviewer",
                         "same_across_conditions": True},
        },
        repetition_config={"design": "paired", "pairs_per_pilot": 1, "pilot": True},
        validity_controls={
            "same_product_spec": True,
            "same_task_contract": True,
            "same_station_contract": True,
            "same_retry_policy": True,
            "same_gates": True,
            "same_reviewer": True,
            "same_worker": True,
            "order_recorded": True,
            "hidden_tests_excluded_from_all_context": True,
        },
        metadata={"label": PILOT_LABEL},
        **kwargs,
    )


@dataclass
class ConditionResult:
    strategy: str
    order_index: int
    factory_run_id: str
    outcome_status: str
    verified: bool
    attempts: int
    escalation: bool
    events: int
    gates: list[dict[str, str]]
    metrics: dict[str, Any]
    context_package: dict[str, Any]


def condition_deps(*, strategy: str, live_builder=None, experiment_id: str | None = None,
                   max_retries: int = 1, tmp_db: Path | str | None = None):
    """RunDeps for one condition on the wide experiment fixture.

    ``live_builder`` is the real adapter for the owner-executed pilot;
    ``None`` binds the deterministic CORRECT fake worker (dry-run).
    """
    from ..factory.controller import RunDeps, make_canonical_slug_fixture_refs
    from ..runtime.fake_worker import fake_reviewer_pass

    del experiment_id  # binding happens via controller.run(experiment_id=...)
    fixture_root = experiment_fixture_root()
    # Gate identity is held constant across conditions: the canonical gate
    # spec's public command is workspace-generic ("pytest -q tests/") and the
    # hidden suite is the canonical one (never part of any context).
    canonical_refs = make_canonical_slug_fixture_refs(
        Path(__file__).resolve().parents[2]
    )
    worker = live_builder
    if worker is None:
        from ..runtime.fake_worker import FakeScriptedWorker

        worker = FakeScriptedWorker.case_fix_slugify_builder()
    product, task, builder_station, reviewer_station = _canonical_specs()
    # THE EXPERIMENTAL VARIABLE: context strategy drives source selection.
    #   selected_v0    → explicitly selected RELEVANT public inputs only;
    #   full_public_v0 → default public sweep (eligible public files incl.
    #   the controlled distractors), still firewall-filtered.
    relevant = ("slugify/__init__.py", "tests/test_slugify_public.py", "pytest.ini")
    include_globs = relevant if strategy == CONDITION_SELECTED else None
    public_globs = relevant if strategy == CONDITION_SELECTED else None
    return RunDeps(
        builder_worker=worker,
        reviewer_worker=fake_reviewer_pass(),
        builder_station=builder_station,
        reviewer_station=reviewer_station,
        fixture_root=fixture_root,
        hidden_test_root=canonical_refs["hidden_test_root"],
        allowed_scope_prefixes=("slugify", "tests"),
        gate_spec=canonical_refs["gate_spec"],
        max_retries=max_retries,
        context_strategy=strategy,
        builder_include_globs=list(include_globs) if include_globs else None,
        builder_public_test_globs=list(public_globs) if public_globs else None,
    ), product, task



# ---------------------------------------------------------------------------
# REDI-05C live pilot composition: Worker A (deterministic controlled defect)
# → Worker B (the real live adapter).  Orchestration/fallback experiment —
# NOT a model quality comparison.
# ---------------------------------------------------------------------------

WORKER_A_ID = "worker_a_deterministic_defect"
WORKER_B_ID = "worker_b_live_openai"

_DEFECT_PY = (
    "import unicodedata\n"
    "import re\n"
    "\n\n"
    "def slugify(text: str, *, maxlen: Optional[int] = None, separator: str = \"-\") -> str:\n"
    "    if not isinstance(text, str):\n"
    "        return text\n"
    "    t = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('utf-8').lower()\n"
    "    t = re.sub(r\"[^a-z0-9]+\", separator, t)\n"
    "    t = t.strip(separator)\n"
    "    if maxlen is not None and isinstance(maxlen, int) and 0 < maxlen < len(t):\n"
    "        t = t[:maxlen]\n"
    "    return t\n"
)


@dataclass
class ControlledDefectWorker:
    """Deterministic Worker A: in-scope artifact with the controlled
    public-safe defect (missing Optional import → public gate FAIL)."""

    calls: int = 0

    def run_node(self, *, task_id, station_id, worker_id, context, workspace_root, station_role="builder"):
        from ..runtime.contracts import NodeRunResult, PatchEdit
        from ..runtime.fake_worker import fake_token_usage

        self.calls += 1
        return NodeRunResult(
            worker_id=worker_id,
            task_id=task_id,
            station_id=station_id,
            patches=(PatchEdit(rel_path="slugify/__init__.py", action="write", content=_DEFECT_PY),),
            token_usage=fake_token_usage(50, 25),
            attempt_index=int((context or {}).get("attempt_index", 0)),
            opaque_provider_ref="redi:deterministic-defect",
            elapsed_seconds=0.001,
        )


def build_pilot_deps(*, live_builder, strategy: str, max_retries: int = 1):
    """RunDeps for the pilot pool: Worker A (defect, rank 0) then Worker B
    (the live adapter, rank 1) on the canonical builder station."""
    from ..domain import PoolEntry, WorkerId, WorkerPool, WorkerProfile
    from ..runtime.contracts import WorkerRunner

    _, task, builder_station, reviewer_station = _canonical_specs()

    profile_a = WorkerProfile(
        id=WorkerId(WORKER_A_ID), display_name="Deterministic controlled-defect worker",
        version="v1", availability_status="available",
        station_eligibility=[builder_station.id],
        provider_opaque_reference="redi:deterministic-defect",
    )
    profile_b = WorkerProfile(
        id=WorkerId(WORKER_B_ID), display_name="Live OpenAI gpt-4o-mini worker",
        version="v1", availability_status="available",
        station_eligibility=[builder_station.id],
        provider_opaque_reference="openai:gpt-4o-mini",
    )
    pool = WorkerPool(
        station_id=builder_station.id, role="builder",
        entries=[PoolEntry(profile=profile_a, preference_rank=0),
                 PoolEntry(profile=profile_b, preference_rank=1)],
    )
    runners = {WORKER_A_ID: ControlledDefectWorker(), WORKER_B_ID: live_builder}
    deps, product, task2 = condition_deps(strategy=strategy, live_builder=runners[WORKER_B_ID])
    deps.builder_pool = pool
    deps.builder_runners = {WORKER_A_ID: runners[WORKER_A_ID], WORKER_B_ID: runners[WORKER_B_ID]}
    return deps, product, task


_CANONICAL_SPECS_CACHE = None


def _canonical_specs():
    """ONE shared contract instance set per process: both conditions of a
    pair must reference literally identical contracts."""
    global _CANONICAL_SPECS_CACHE
    if _CANONICAL_SPECS_CACHE is not None:
        return _CANONICAL_SPECS_CACHE
    from ..domain import (
        ProductSpec, Requirement, RiskTier, StationContract, TaskContract,
    )

    product = ProductSpec(
        requested_outcome=(
            "Rewrite the stub slugify/__init__.py so it exposes slugify(text, "
            "*, maxlen=None, separator='-') implementing: unicode NFKD -> "
            "ascii ignoring accents -> lowercase -> [^a-z0-9]+ replaced by "
            "separator -> strip separators -> maxlen hard-cut with separator "
            "backoff only when boundary lands exactly on separator."
        ),
        requirements=[
            Requirement(text="slugify('hello world foo', maxlen=10) == 'hello-worl' (hard cut)", kind="functional"),
            Requirement(text="slugify('hello-world-foo', maxlen=10) == 'hello-worl' (separator boundary but no separator left)", kind="functional"),
            Requirement(text="public tests pass; hidden tests pass", kind="functional"),
        ],
        constraints=[
            "Only modify slugify/__init__.py; do not add or remove files.",
            "No external dependencies beyond stdlib (unicodedata, re, typing ok).",
        ],
        risk_tier=RiskTier.LOW,
        unresolved_requirements=[],
    )
    task = TaskContract(goal="produce PatchEdits so slugify passes public and hidden tests")
    builder_station = StationContract(role="builder", required_output_kinds=["patch_edit"], allowed_tool_classes=[])
    reviewer_station = StationContract(role="reviewer", required_output_kinds=["verdict"], allowed_tool_classes=[])
    _CANONICAL_SPECS_CACHE = (product, task, builder_station, reviewer_station)
    return _CANONICAL_SPECS_CACHE


@dataclass
class PairResult:
    experiment_id: str
    order: list[str]
    conditions: dict[str, ConditionResult]
    condition_db_paths: dict[str, str] = field(default_factory=dict)
    label: str = PILOT_LABEL


def preregister_into(db_path: Path | str, manifest: ExperimentManifest) -> str:
    """Persist the shared pair manifest into a run ledger database.

    Each condition run lives in its own database; the SAME manifest object is
    stored in each so both factory_runs reference one experiment identity.
    """
    from ..persistence import EvidenceLedger, ExperimentRepository, bootstrap_schema

    conn = sqlite3.connect(str(db_path))
    try:
        bootstrap_schema(conn)
        repo = ExperimentRepository(conn, EvidenceLedger(conn))
        repo.store_preregistered(manifest)
        conn.commit()
    finally:
        conn.close()
    return str(manifest.id)


def run_pair(
    *,
    live_builder=None,
    manifest: ExperimentManifest | None = None,
    order: Sequence[str] = DEFAULT_ORDER,
    max_retries: int = 1,
    db_dir: Path | str | None = None,
    deps_factory=None,
) -> PairResult:
    """Execute ONE paired pilot (both conditions) against the wide fixture.

    The shared ExperimentManifest is preregistered into every condition
    database, binding both factory runs to one experiment/pair identity.
    """
    from ..factory.controller import MinimalFactoryController

    assert sorted(order) == sorted(DEFAULT_ORDER), f"order must be a permutation of {DEFAULT_ORDER}"
    if manifest is None:
        manifest = build_experiment_manifest()
    experiment_id = str(manifest.id)
    # REDI-05C1 LEDGER OWNERSHIP INVARIANT: every condition gets its own
    # materialized SQLite database, and the SAME immutable ExperimentManifest
    # (identical id/payload/hash) is preregistered into EACH condition
    # database BEFORE its FactoryRun is created — the
    # factory_runs.experiment_id FK must always resolve.  The manifest is
    # never mutated after preregistration.
    import tempfile as _tempfile

    if db_dir is None:
        db_dir = _tempfile.mkdtemp(prefix="redi05_pilot_")
    Path(db_dir).mkdir(parents=True, exist_ok=True)
    condition_db_paths: dict[str, str] = {}
    conditions: dict[str, ConditionResult] = {}
    condition_db_paths: dict[str, str] = {}
    for index, strategy in enumerate(order):
        db_path = Path(db_dir) / f"context_pilot_{index}_{strategy}.db"
        preregister_into(db_path, manifest)
        if deps_factory is not None:
            deps, product, task = deps_factory(strategy=strategy)
        else:
            deps, product, task = condition_deps(strategy=strategy, live_builder=live_builder)
        outcome = MinimalFactoryController.run(
            product, task, deps, db_path=db_path, experiment_id=experiment_id,
        )
        conditions[strategy] = ConditionResult(
            strategy=strategy,
            order_index=index,
            factory_run_id=outcome.factory_run_id,
            outcome_status=outcome.final_status.value,
            verified=outcome.verified,
            attempts=outcome.attempts,
            escalation=outcome.escalation,
            events=outcome.events_emitted,
            gates=[{"gate": g["gate"], "status": g["status"]} for g in _attempt_gates(outcome.db_path)],
            metrics=condition_metrics(outcome.db_path, outcome.factory_run_id, strategy, index),
            context_package=_last_context_package(outcome.db_path),
        )
        condition_db_paths[strategy] = str(db_path)
    return PairResult(
        experiment_id=experiment_id,
        order=list(order),
        conditions=conditions,
        condition_db_paths=condition_db_paths,
    )


def _last_context_package(db_path: str) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT payload_json FROM evidence_entries "
            "WHERE entity_type = 'RunEvidence' AND payload_json LIKE '%N_BUILDER_0%' "
            "ORDER BY global_sequence DESC LIMIT 1"
        ).fetchall()
        md = (json.loads(rows[0][0]).get("metadata") or {}) if rows else {}
        return md.get("context_package") or {}
    finally:
        conn.close()


def _attempt_gates(db_path: str) -> list[dict[str, str]]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT payload_json FROM evidence_entries WHERE entity_type='GateResult' ORDER BY global_sequence"
        ).fetchall()
        out = []
        for (pj,) in rows:
            d = json.loads(pj)
            out.append({"gate": d.get("gate_identity", ""), "status": d.get("status", "")})
        return out
    finally:
        conn.close()


def condition_metrics(db_path: str, factory_run_id: str, strategy: str, order_index: int) -> dict[str, Any]:
    """Per-condition measurement bundle (§9): no collapse into one score."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        run = conn.execute(
            "SELECT final_status, verified, payload_json FROM factory_runs WHERE id = ?",
            (factory_run_id,),
        ).fetchone()
        builder_rows = conn.execute(
            "SELECT payload_json FROM evidence_entries "
            "WHERE factory_run_id = ? AND entity_type = 'RunEvidence' "
            "AND payload_json LIKE '%N_BUILDER_0%' ORDER BY global_sequence",
            (factory_run_id,),
        ).fetchall()
        gate_rows = conn.execute(
            "SELECT payload_json FROM evidence_entries "
            "WHERE factory_run_id = ? AND entity_type = 'GateResult' ORDER BY global_sequence",
            (factory_run_id,),
        ).fetchall()
    finally:
        conn.close()

    tokens = {"input": 0, "output": 0, "total": 0}
    wall_clock = 0.0
    retries = 0
    context_packages = []
    included_paths = []
    for (pj,) in builder_rows:
        d = json.loads(pj)
        md = d.get("metadata") or {}
        tu = d.get("token_usage") or {}
        tokens["input"] += int(tu.get("input") or 0)
        tokens["output"] += int(tu.get("output") or 0)
        tokens["total"] += int((tu.get("other") or {}).get("total") or 0)
        t = d.get("timing") or {}
        wall_clock += float(t.get("wall_clock_seconds") or 0.0)
        retries = int(md.get("retry_count") or retries)
        cp = md.get("context_package")
        if cp:
            context_packages.append(cp)
            included_paths.extend(f["path"] for f in cp.get("included_files", []))
    gates = []
    for (pj,) in gate_rows:
        d = json.loads(pj)
        gates.append({"gate": d.get("gate_identity", ""), "status": d.get("status", "")})

    verified = bool(run["verified"]) if run else False
    return {
        "strategy": strategy,
        "order_index": order_index,
        "factory_run_id": factory_run_id,
        "final_status": (run["final_status"] if run else None),
        "verified": verified,
        "input_tokens": tokens["input"],
        "output_tokens": tokens["output"],
        "total_tokens": tokens["total"],
        "wall_clock_seconds": round(wall_clock, 4),
        "retries": retries,
        "gate_outcomes": gates,
        "context_package_hashes": [cp.get("package_hash") for cp in context_packages],
        "rendered_hashes": [cp.get("rendered_hash") for cp in context_packages],
        "included_source_paths": sorted(set(included_paths)),
        "context_byte_counts": [cp.get("counts", {}).get("bytes") for cp in context_packages],
        "context_char_counts": [cp.get("counts", {}).get("chars") for cp in context_packages],
        "context_estimated_tokens": [cp.get("counts", {}).get("estimated_tokens") for cp in context_packages],
        "provider_input_tokens": tokens["input"],
    }


__all__ = [
    "CONDITION_FULL_PUBLIC",
    "CONDITION_SELECTED",
    "DEFAULT_ORDER",
    "EXPERIMENT_HYPOTHESIS_REFERENCE",
    "PILOT_LABEL",
    "ConditionResult",
    "PairResult",
    "build_experiment_manifest",
    "condition_deps",
    "condition_metrics",
    "experiment_fixture_root",
    "run_pair",
]
