"""REDI Workbench openai-live backend — bridge to the governed execution path.

Reuses the EXISTING governed machinery only: LiveProviderAdapter (via the
hardened qualification executor boundary), manifest v3 product specs, frozen
gates, evidence rows, replay-from-bytes.  No parallel provider stack.

Scope guard: the first real builds are restricted to the currently qualified
inventory/replenishment product family (manifest v3 selected products).  A
prompt that does not resolve to that family is rejected BEFORE any call.

Fail-closed requirements for a live run (checked before any provider call):
REDI_OPENAI_API_KEY, REDI_LIVE_PROVIDER=1, explicit owner cost ceiling.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

from ..mcw_pricing import cell_cost_bound
from ..stable_baseline import digest
from ..qualification_evidence import QUALIFICATION_OUTPUT_CONTRACT_V3


LIVE_BACKEND_ID = "openai-live"
V6_MODEL = "gpt-5.6-terra"
QUALIFIED_FAMILY = "inventory_replenishment_utility"

# Predefined owner test brief for the first real Workbench build (NOT executed
# in this task; offered by the UI/adapter as the canned first brief).
FIRST_BUILD_BRIEF = {
    "brief_id": "workbench-first-real-build-v1",
    "prompt": (
        "Build a pharmacy supply replenishment package: normalize pharmacy "
        "labels and compute case-packed reorder quantities with a safety "
        "stock floor, verified by the frozen deterministic gates."
    ),
    "product_key": "pharmacy_supply",
}

FAMILY_KEYWORDS: Mapping[str, tuple[str, ...]] = {
    "stationery_supply": ("stationery",),
    "pharmacy_supply": ("pharmacy", "supply"),
}


def live_cost_estimate() -> dict[str, str]:
    """Hard/expected cost for one governed v6 live build (pinned schedule)."""
    bound = cell_cost_bound(model=V6_MODEL)
    return {
        "hard_cost_bound_usd": bound["hard_upper_bound"]["cost_amount"],
        "expected_cost_usd": bound["expected_estimate"]["cost_amount"],
        "model": V6_MODEL,
    }


def resolve_scope(prompt: str, products: Mapping[str, Any]) -> dict[str, Any]:
    """Only the qualified inventory/replenishment family is supported in v1."""
    low = (prompt or "").lower()
    for key, keywords in FAMILY_KEYWORDS.items():
        if key in products and any(k in low for k in keywords):
            return {"supported": True, "product_key": key,
                    "family": QUALIFIED_FAMILY}
    return {"supported": False,
            "reason": ("only the qualified inventory/replenishment product "
                       "family is supported for live builds in v1 "
                       f"({', '.join(sorted(p for p in products if p in FAMILY_KEYWORDS))})")}


def check_live_gates_env(*, key: str | None, live_flag: str | None, ceiling: str | None) -> list[str]:
    missing: list[str] = []
    if not key:
        missing.append("REDI_OPENAI_API_KEY")
    if live_flag != "1":
        missing.append("REDI_LIVE_PROVIDER=1")
    if not ceiling:
        missing.append("owner cost ceiling")
    return missing


@dataclass
class LiveBuildResult:
    product_key: str
    verified: bool
    gates: dict[str, bool]
    reasons: dict[str, str]
    usage: dict[str, int]
    cost_usd: str
    elapsed_seconds: float
    retained: dict[str, Any]
    finish_reason: str
    provider_calls: int = 1


class OpenAILiveBackend:
    """Governed live build of ONE qualified-family product via the existing
    hardened executor boundary.  `client_factory` is the single seam used for
    offline fake-provider proofs — production passes the real OpenAI client."""

    def __init__(self, *, client_factory: Callable[[], Any] | None = None) -> None:
        self._client_factory = client_factory

    def run_build(
        self,
        *,
        prompt: str,
        key: str | None,
        live_flag: str | None,
        ceiling: str | None,
        output_root: Path,
        products: Mapping[str, Any],
        ws_root: Path,
        progress: Any = None,
    ) -> dict[str, Any]:
        if progress:
            progress("planning_started")
        missing = check_live_gates_env(key=key, live_flag=live_flag, ceiling=ceiling)
        if missing:
            return {"status": "BLOCKED", "missing": missing,
                    "detail": "fail-before-provider-call: missing " + ", ".join(missing)}
        scope = resolve_scope(prompt, products)
        if progress:
            progress("scope_resolved")
        if not scope["supported"]:
            return {"status": "REJECTED", "reason": scope["reason"]}
        product = products[scope["product_key"]]
        estimate = live_cost_estimate()
        if Decimal(str(ceiling)) < Decimal(estimate["hard_cost_bound_usd"]):
            return {"status": "BLOCKED",
                    "detail": f"ceiling {ceiling} below hard bound {estimate['hard_cost_bound_usd']}"}
        if self._client_factory is None:
            from openai import OpenAI

            client = OpenAI(api_key=key, timeout=90.0)
        else:
            client = self._client_factory()
        user = (
            product["requirement_text"]
            + "\n\n## Output contract (binding)\nReturn ONLY a JSON object "
            "{\"patches\": [...]}. The patches list must contain EXACTLY three "
            "write patches with these rel_paths and no others: "
            + json.dumps(["normalize.py", "reorder.py", "service.py"]) + ". "
            "Each patch: {\"rel_path\": ..., \"action\": \"write\", \"content\": \"<full file>\"}."
        )
        from .engine import GOVERNED_GATE_ORDER  # same gate order as qualification

        if progress:
            progress("provider_call_started")
        t0 = time.perf_counter()
        resp = client.chat.completions.create(
            model=V6_MODEL,
            messages=[{"role": "system", "content": "You are a deterministic Python builder. Follow the specification exactly."},
                      {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            max_completion_tokens=2048,
            reasoning_effort="low",
        )
        elapsed = time.perf_counter() - t0
        if progress:
            progress("provider_call_completed")
        u = resp.usage
        try:
            parsed = json.loads(resp.choices[0].message.content or "")
            patches = {p["rel_path"]: p["content"] for p in parsed["patches"]}
        except Exception as exc:  # noqa: BLE001 — honest failure, nothing hidden
            return {"status": "FAILED", "product_key": scope["product_key"],
                    "verified": False, "gates": {"structured_output_schema": False},
                    "reasons": {"structured_output_schema": f"{type(exc).__name__}: {exc}"},
                    "usage": {"input": u.prompt_tokens, "output": u.completion_tokens},
                    "cost_usd": estimate["expected_cost_usd"], "elapsed_seconds": elapsed,
                    "finish_reason": str(resp.choices[0].finish_reason)}
        from ..qualification_executor import evaluate_arm, materialize

        retained = materialize(product_key=scope["product_key"], arm="workbench_live",
                               model=V6_MODEL, patches=patches)
        if progress:
            progress("materialized")
        evaluation = evaluate_arm(retained, dict(product),
                                  dict(QUALIFICATION_OUTPUT_CONTRACT_V3),
                                  ws_root=ws_root / scope["product_key"],
                                  progress=progress)
        from ..mcw_pricing import calculate_cost

        cost = calculate_cost(model=V6_MODEL, usage={
            "input": u.prompt_tokens,
            "cached_input": getattr(u.prompt_tokens_details, "cached_tokens", 0) or 0,
            "output": u.completion_tokens,
        })
        return {
            "status": "VERIFIED" if evaluation["all_mandatory_gates_pass"] else "FAILED",
            "product_key": scope["product_key"], "model": V6_MODEL,
            "verified": evaluation["all_mandatory_gates_pass"],
            "gates": evaluation["gates"], "gate_reasons": evaluation["reasons"],
            "usage": {"input": u.prompt_tokens,
                      "cached_input": getattr(u.prompt_tokens_details, "cached_tokens", 0) or 0,
                      "output": u.completion_tokens},
            "cost_usd": cost["cost_amount"], "pricing_evidence_state": cost["evidence_status"],
            "elapsed_seconds": elapsed, "finish_reason": str(resp.choices[0].finish_reason),
            "retained": evaluation["retained"], "provider_calls": 1,
        }


def governed_products() -> dict[str, Any]:
    """The manifest v3 governed product specs (qualified family only)."""
    from ..recipe_store import RecipeStore
    from ..qualification_evidence import build_evidence_manifest_v3, capture_prior_operational_state

    with RecipeStore("build/redi08c14-owner-responses-20260922T195953Z/recipe_store.sqlite") as store:
        cap = capture_prior_operational_state(store)
    m3 = build_evidence_manifest_v3(cap)
    return {s["product"]["key"]: s["product"] for s in m3.paired_specs}
