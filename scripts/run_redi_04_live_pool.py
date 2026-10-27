"""REDI-04C — bounded live WorkerPool acceptance runner.

One bounded real live production run proving the REDI-04 WorkerPool
machinery (orchestration/fallback — NOT model quality comparison):

    Builder Station
    → Worker A (deterministic controlled-defect worker, in scope)
      selection_kind=initial → public gate FAIL (deterministic)
    → selector excludes A, selects Worker B
      selection_kind=worker_fallback, previous_worker=A
    → Worker B = the existing real OpenAI gpt-4o-mini live adapter
      (bounded L6/L9 context + A's public failure diagnostic)
    → scope/syntax/public/hidden gates
    → independent deterministic Reviewer
    → PRODUCT_VERIFIED

Run (owner-executed only):
    REDI_LIVE_PROVIDER=1  REDI_OPENAI_API_KEY=sk-...  \
        python -m scripts.run_redi_04_live_pool

Environment gates are fail-closed: without both variables the runner
prints a compact LIVE BLOCKED banner and creates no ledger.  The API key
is never printed.  If Worker B fails, it is not retried — the existing
terminal failure/escalation semantics apply and the run is audited after.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def banner(s: str) -> None:
    print(s, flush=True)


def _blocked(reason: str) -> int:
    banner(f"REDI-04 LIVE BLOCKED: {reason}")
    return 0


WORKER_A_ID = "worker_a_deterministic_defect"
WORKER_B_ID = "worker_b_live_openai"

# Controlled, public-safe defect (the exact shape observed in the real L8
# run): in scope, compiles cleanly, but NameError at import time because
# `Optional` is annotated without its typing import → the public pytest
# collection fails with exit code 2.
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
    """Worker A: deterministic in-scope artifact with the controlled defect."""

    calls: int = 0

    def run_node(self, *, task_id, station_id, worker_id, context, workspace_root, station_role="builder"):
        from redi_lab.runtime.contracts import NodeRunResult, PatchEdit
        from redi_lab.runtime.fake_worker import fake_token_usage

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


def _canonical_specs():
    """ProductSpec / TaskContract / StationContracts — identical to the
    accepted REDI-03 live runner (canonical slugify fixture)."""
    from redi_lab.domain import (
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
    return product, task, builder_station, reviewer_station


def build_run_deps(*, live_builder=None, model: str = "gpt-4o-mini"):
    """Compose the Builder WorkerPool + runner bindings.

    ``live_builder`` is the real live adapter (production composition).
    Passing ``live_builder=None`` yields the zero-network dry-run
    composition: Worker B is bound to a deterministic CORRECT worker, so the
    full pool path can be exercised without any provider call.
    """
    from redi_lab.domain import (
        PoolEntry, WorkerId, WorkerPool, WorkerProfile,
    )
    from redi_lab.factory.controller import RunDeps, make_canonical_slug_fixture_refs
    from redi_lab.runtime.fake_worker import FakeScriptedWorker, fake_reviewer_pass

    product, task, builder_station, reviewer_station = _canonical_specs()
    refs = make_canonical_slug_fixture_refs(REPO_ROOT)

    profile_a = WorkerProfile(
        id=WorkerId(WORKER_A_ID),
        display_name="Deterministic controlled-defect worker",
        version="v1",
        availability_status="available",
        station_eligibility=[builder_station.id],
        provider_opaque_reference="redi:deterministic-defect",
    )
    profile_b = WorkerProfile(
        id=WorkerId(WORKER_B_ID),
        display_name="Live OpenAI gpt-4o-mini worker",
        version="v1",
        availability_status="available",
        station_eligibility=[builder_station.id],
        provider_opaque_reference=f"openai:{model}",
    )
    pool = WorkerPool(
        station_id=builder_station.id,
        role="builder",
        entries=[PoolEntry(profile=profile_a, preference_rank=0), PoolEntry(profile=profile_b, preference_rank=1)],
    )

    worker_a = ControlledDefectWorker()
    worker_b = live_builder if live_builder is not None else FakeScriptedWorker.case_fix_slugify_builder()
    runners = {WORKER_A_ID: worker_a, WORKER_B_ID: worker_b}

    deps = RunDeps(
        builder_worker=worker_b,  # unused on the pool path; terminal fallback
        reviewer_worker=fake_reviewer_pass(),  # independent deterministic Reviewer (unchanged from REDI-03)
        builder_station=builder_station,
        reviewer_station=reviewer_station,
        fixture_root=refs["fixture_root"],
        hidden_test_root=refs["hidden_test_root"],
        allowed_scope_prefixes=refs["allowed_scope_prefixes"],
        gate_spec=refs["gate_spec"],
        max_retries=1,  # exactly two Builder executions: A then B
        builder_pool=pool,
        builder_runners=runners,
    )
    return deps, pool, product, task


def _worker_trace(db_path: str, factory_run_id: str) -> tuple[list[str], bool, str | None]:
    """Read-only: ordered builder worker ids, fallback flag, pool hash."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        workers: list[str] = []
        fallback = False
        pool_hash = None
        for r in conn.execute(
            "SELECT payload_json FROM evidence_entries "
            "WHERE factory_run_id = ? AND entity_type = 'RunEvidence' AND payload_json LIKE '%N_BUILDER_0%' "
            "ORDER BY global_sequence",
            (factory_run_id,),
        ).fetchall():
            md = (json.loads(r["payload_json"]).get("metadata") or {})
            sel = md.get("worker_selection") or {}
            if sel.get("worker_id"):
                workers.append(str(sel["worker_id"]))
            if sel.get("selection_kind") == "worker_fallback":
                fallback = True
            if sel.get("pool_snapshot_hash"):
                pool_hash = sel["pool_snapshot_hash"]
        return workers, fallback, pool_hash
    finally:
        conn.close()


def main() -> int:
    if not os.environ.get("REDI_LIVE_PROVIDER", ""):
        return _blocked("REDI_LIVE_PROVIDER not set in environment; live run skipped by policy.")
    if not os.environ.get("REDI_OPENAI_API_KEY", ""):
        return _blocked("REDI_OPENAI_API_KEY not set in environment; live run skipped by policy.")

    from redi_lab.factory.controller import MinimalFactoryController
    from redi_lab.runtime.live_provider_adapter import (
        LiveProviderAdapter, LiveProviderBlockedError, LiveProviderConfig,
    )

    cfg = LiveProviderConfig(
        provider_name="openai",
        model=os.environ.get("REDI_OPENAI_MODEL", "gpt-4o-mini"),
        env_key_name="REDI_OPENAI_API_KEY",
        max_output_tokens=int(os.environ.get("REDI_OPENAI_MAX_TOKENS", "2048")),
        timeout_s=float(os.environ.get("REDI_OPENAI_TIMEOUT", "60.0")),
    )
    try:
        live_builder = LiveProviderAdapter(config=cfg)
    except LiveProviderBlockedError as e:
        return _blocked(str(e))

    deps, pool, product, task = build_run_deps(live_builder=live_builder, model=cfg.model)
    try:
        outcome = MinimalFactoryController.run(product, task, deps)
    except LiveProviderBlockedError as e:
        return _blocked(str(e))
    except Exception as e:  # noqa: BLE001 — surface cleanly, never leak secrets
        banner(f"REDI-04 LIVE BLOCKED: unexpected framework error {type(e).__name__}: {e}")
        traceback.print_exc(limit=5)
        return 0

    workers, fallback, pool_hash = _worker_trace(outcome.db_path, outcome.factory_run_id)
    banner("REDI-04 LIVE OK:")
    banner(f"status={outcome.final_status.value}")
    banner(f"verified={outcome.verified}")
    banner(f"attempts={outcome.attempts}")
    banner(f"workers={'>'.join(workers)}")
    banner(f"fallback={fallback}")
    banner(f"events={outcome.events_emitted}")
    banner(f"gates={outcome.final_gate_count}")
    banner(f"pool_snapshot_hash={pool_hash or ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
