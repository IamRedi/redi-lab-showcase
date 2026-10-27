"""REDI-05C — paired selected_v0 vs full_public_v0 live context pilot.

Owner-executed only.  Fail-closed environment gates (both required):
    REDI_LIVE_PROVIDER=1
    REDI_OPENAI_API_KEY=sk-...

One paired pilot: two factory runs on the wide experiment fixture —
    Condition S: selected_v0 context
    Condition F: full_public_v0 context
Same worker (live gpt-4o-mini), same retry policy, same gates, same
independent deterministic Reviewer; only the context strategy differs.
Both runs bind to one preregistered ExperimentManifest (pair identity).

Output is PILOT / FEASIBILITY EVIDENCE ONLY — no H2 claim is made from a
single pair.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import traceback
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def banner(s: str) -> None:
    print(s, flush=True)


def _blocked(reason: str) -> int:
    banner(f"REDI-05 PILOT BLOCKED: {reason}")
    return 0


def main() -> int:
    if not os.environ.get("REDI_LIVE_PROVIDER", ""):
        return _blocked("REDI_LIVE_PROVIDER not set in environment; pilot skipped by policy.")
    if not os.environ.get("REDI_OPENAI_API_KEY", ""):
        return _blocked("REDI_OPENAI_API_KEY not set in environment; pilot skipped by policy.")

    from redi_lab.domain import ExperimentId
    from redi_lab.experiments import context_pilot as pilot_mod
    from redi_lab.experiments.context_pilot import (
        PILOT_LABEL, build_experiment_manifest, run_pair,
    )
    from redi_lab.persistence import (
        EvidenceLedger, ExperimentRepository, bootstrap_schema,
    )
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

    order_env = os.environ.get("REDI_05_ORDER", "selected_v0,full_public_v0")
    order = [s.strip() for s in order_env.split(",") if s.strip()]

    pilot_db = REPO_ROOT / "experiments" / "context_pilot_ledger.db"
    conn = sqlite3.connect(pilot_db)
    bootstrap_schema(conn)
    ledger = EvidenceLedger(conn)
    repo = ExperimentRepository(conn, ledger)
    manifest = build_experiment_manifest(model=cfg.model)
    repo.store_preregistered(manifest)
    experiment_id = str(manifest.id)
    conn.commit()
    conn.close()
    banner(f"REDI-05 PILOT: experiment {experiment_id} preregistered (order: {' -> '.join(order)})")

    deps_factory = lambda strategy: pilot_mod.build_pilot_deps(
        live_builder=live_builder, strategy=strategy, max_retries=1)
    try:
        pair = run_pair(deps_factory=deps_factory, manifest=manifest, order=order)
    except LiveProviderBlockedError as e:
        return _blocked(str(e))
    except Exception as e:  # noqa: BLE001 — surface cleanly, never leak secrets
        banner(f"REDI-05 PILOT BLOCKED: unexpected framework error {type(e).__name__}: {e}")
        traceback.print_exc(limit=5)
        return 0

    banner("REDI-05 PILOT COMPLETE (PILOT / FEASIBILITY EVIDENCE ONLY):")
    for strategy in order:
        banner(f"condition_ledger[{strategy}]={pair.condition_db_paths[strategy]}")
    for strategy in order:
        c = pair.conditions[strategy]
        m = c.metrics
        banner(
            f"condition={strategy} order_index={c.order_index} run={c.factory_run_id} "
            f"status={c.outcome_status} verified={c.verified} attempts={c.attempts} "
            f"input_tokens={m['input_tokens']} output_tokens={m['output_tokens']} "
            f"wall_seconds={m['wall_clock_seconds']} "
            f"context_bytes={m['context_byte_counts']} "
            f"est_tokens={m['context_estimated_tokens']} "
            f"package_hash={(m['context_package_hashes'] or [''])[0][:16]}"
        )
    banner(
        "interpretation=PILOT/FEASIBILITY ONLY — no conclusion about which "
        "context strategy is better may be drawn from a single pair."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
