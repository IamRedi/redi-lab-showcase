"""Single bounded REDI-03 live-provider run or clean LIVE BLOCKED signal.

Run:
    python -m scripts.run_redi_03_live_provider

Environment:
    REDI_LIVE_PROVIDER=1        set to any non-empty value to attempt live run
    REDI_OPENAI_API_KEY=sk-...  single provider key (never logged/ledgered)

Exit 0 in both success (LIVE OK) and blocked (LIVE BLOCKED) cases; exit 1
on unexpected framework errors. Print exactly one banner line: either
"REDI-03 LIVE OK: <summary>"  or  "REDI-03 LIVE BLOCKED: <reason>".
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def banner(s: str) -> None:
    print(s, flush=True)


def _blocked(reason: str) -> int:
    banner(f"REDI-03 LIVE BLOCKED: {reason}")
    return 0


def main() -> int:
    if not os.environ.get("REDI_LIVE_PROVIDER", ""):
        return _blocked("REDI_LIVE_PROVIDER not set in environment; live run skipped by policy.")

    # Import only after sys.path pin (site-packages shadow defense)
    from redi_lab.domain import (
        ProductSpec, Requirement, TaskContract, StationContract, RiskTier,
    )
    from redi_lab.factory.controller import MinimalFactoryController, RunDeps
    from redi_lab.runtime.fake_worker import fake_reviewer_pass
    from redi_lab.runtime.live_provider_adapter import (
        LiveProviderAdapter, LiveProviderConfig, LiveProviderBlockedError,
    )

    fixture_root = REPO_ROOT / "experiments" / "fixtures" / "slugify_pkg"
    hidden_root = REPO_ROOT / "experiments" / "fixtures" / "slugify_pkg_hidden_tests"

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
    b_station = StationContract(
        role="builder",
        required_output_kinds=["patch_edit"],
        allowed_tool_classes=[],
    )
    r_station = StationContract(
        role="reviewer",
        required_output_kinds=["verdict"],
        allowed_tool_classes=[],
    )

    cfg = LiveProviderConfig(
        provider_name="openai",
        model=os.environ.get("REDI_OPENAI_MODEL", "gpt-4o-mini"),
        env_key_name="REDI_OPENAI_API_KEY",
        max_output_tokens=int(os.environ.get("REDI_OPENAI_MAX_TOKENS", "2048")),
        timeout_s=float(os.environ.get("REDI_OPENAI_TIMEOUT", "60.0")),
    )
    try:
        builder = LiveProviderAdapter(
            config=cfg,
        )
    except LiveProviderBlockedError as e:
        return _blocked(str(e))

    deps = RunDeps(
        builder_worker=builder,
        reviewer_worker=fake_reviewer_pass(),  # independent deterministic reviewer
        builder_station=b_station,
        reviewer_station=r_station,
        fixture_root=fixture_root,
        hidden_test_root=hidden_root,
        max_retries=1,
    )

    try:
        outcome = MinimalFactoryController.run(product, task, deps)
    except LiveProviderBlockedError as e:
        return _blocked(str(e))
    except Exception as e:  # noqa: BLE001
        import traceback
        tb = traceback.format_exc(limit=5)
        banner(f"REDI-03 LIVE BLOCKED: unexpected framework error {type(e).__name__}: {e}\n{tb}")
        return 0

    status = outcome.final_status
    verif = outcome.verified
    attempts = outcome.attempts
    escalated = outcome.escalation
    banner(
        f"REDI-03 LIVE OK: status={status.value} verified={verif} attempts={attempts} "
        f"escalation={escalated} events={outcome.events_emitted} gates={outcome.final_gate_count}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
