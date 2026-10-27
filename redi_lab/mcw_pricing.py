"""REDI-09B — versioned MCW pricing schedule and cost calculation contract.

The schedule below is an IMMUTABLE, versioned snapshot of officially
retrieved OpenAI standard-text pricing.  It is never mutated: when prices
change, a NEW schedule version is created and historical results keep
referencing the schedule version they were computed with.

Evidence (official OpenAI sources only, retrieved 2026-09-22):

- https://developers.openai.com/api/docs/pricing
  * gpt-5.6-terra is listed DIRECTLY (Standard tier): short-context
    input $2.00 / cached input $0.20 / output $12.00 per 1M tokens;
    long-context input $4.00 / cached $0.40 / output $18.00; Flex
    short-context $1.00 / $0.10 / $6.00.  Evidence status: SNAPSHOT_PROVEN.
  * gpt-4o-mini-2024-07-18 and gpt-4.1-2025-04-14 appear ONLY in the
    fine-tuning tables; the base-model text tables list only the undated
    aliases gpt-4o-mini ($0.15 / $0.075 / $0.60) and gpt-4.1
    ($2.00 / $0.50 / $8.00).  Per the REDI-09B directive the dated
    snapshots are recorded as an explicit PRICING-FAMILY MAPPING with
    evidence status FAMILY_MAPPING_UNPROVEN_SNAPSHOT — base-model
    inference of these snapshots is EXPECTED to bill at the alias family
    rate, but that relationship is not proven by an official statement.
  * Regional data-residency endpoints carry a 10% uplift for models
    released on or after 2026-03-05 (gpt-5.6 family).  The MCW pilot uses
    default endpoints; the rule is recorded, not applied.
  * The token threshold where gpt-5.6 long-context pricing begins is NOT
    stated on the page; bounds therefore report both the short-context
    (primary, MCW contexts are ~2K tokens) and a long-context worst case.
- https://developers.openai.com/api/docs/models — contains no
  alias→snapshot pinning statement for gpt-4o-mini / gpt-4.1.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any, Mapping

PRICING_SCHEDULE_ID = "openai_standard_text_pricing_2026_09_22_v1"
PRICING_SCHEDULE_VERSION = 1
PRICING_PROVIDER = "openai"
PRICING_CURRENCY = "USD"
PRICING_SERVICE_TIER = "standard"
PRICING_RETRIEVED_AT = "2026-09-22"
COST_CALCULATION_VERSION = "mcw_cost_v1"

EVIDENCE_SNAPSHOT_PROVEN = "SNAPSHOT_PROVEN"
EVIDENCE_FAMILY_MAPPING_UNPROVEN = "FAMILY_MAPPING_UNPROVEN_SNAPSHOT"

_PRICING_SOURCES: tuple[Mapping[str, str], ...] = (
    {
        "url": "https://developers.openai.com/api/docs/pricing",
        "retrieved_at": PRICING_RETRIEVED_AT,
        "note": (
            "official OpenAI pricing: gpt-5.6-terra listed directly; "
            "gpt-4o-mini / gpt-4.1 base tables list undated aliases only; "
            "dated snapshot rows appear only under fine-tuning"
        ),
    },
    {
        "url": "https://developers.openai.com/api/docs/models",
        "retrieved_at": PRICING_RETRIEVED_AT,
        "note": "no alias-to-snapshot pinning statement for gpt-4o-mini / gpt-4.1",
    },
)

# Per-1M-token standard-tier rates as strings to keep Decimal-exact values.
_PRICING_RECORDS: Mapping[str, Mapping[str, Any]] = {
    "gpt-4o-mini-2024-07-18": {
        "pricing_family": "gpt-4o-mini",
        "input_per_1m": "0.15",
        "cached_input_per_1m": "0.075",
        "output_per_1m": "0.60",
        "context_mode": "single_tier",
        "evidence_status": EVIDENCE_FAMILY_MAPPING_UNPROVEN,
        "family_mapping_note": (
            "base-model text pricing lists only the alias gpt-4o-mini; the "
            "dated snapshot row appears exclusively in fine-tuning tables "
            "($0.30/$0.15/$1.20), which do not govern base inference"
        ),
    },
    "gpt-4.1-2025-04-14": {
        "pricing_family": "gpt-4.1",
        "input_per_1m": "2.00",
        "cached_input_per_1m": "0.50",
        "output_per_1m": "8.00",
        "context_mode": "single_tier",
        "evidence_status": EVIDENCE_FAMILY_MAPPING_UNPROVEN,
        "family_mapping_note": (
            "base-model text pricing lists only the alias gpt-4.1; the dated "
            "snapshot row appears exclusively in fine-tuning tables "
            "($3.00/$0.75/$12.00), which do not govern base inference"
        ),
    },
    "gpt-5.6-terra": {
        "pricing_family": "gpt-5.6-terra",
        "input_per_1m": "2.00",
        "cached_input_per_1m": "0.20",
        "output_per_1m": "12.00",
        "context_mode": "short_context",
        "long_context": {
            "input_per_1m": "4.00",
            "cached_input_per_1m": "0.40",
            "output_per_1m": "18.00",
            "threshold_tokens_officially_stated": False,
        },
        "flex_short_context": {
            "input_per_1m": "1.00",
            "cached_input_per_1m": "0.10",
            "output_per_1m": "6.00",
        },
        "evidence_status": EVIDENCE_SNAPSHOT_PROVEN,
    },
}

PRICING_RULES: Mapping[str, Any] = {
    "data_residency": (
        "regional processing endpoints carry a 10% uplift for models released "
        "on or after 2026-03-05 (gpt-5.6 family); the MCW pilot uses default "
        "endpoints, so the uplift is recorded but NOT applied"
    ),
    "batch_flex_tier": "not used by the MCW pilot (interactive standard tier only)",
    "cache_writes": (
        "gpt-5.6 short-context cache writes $2.50 per 1M tokens; MCW cells are "
        "single-shot with no cache priming, so cache-write pricing is not applied"
    ),
    "reasoning_tokens": (
        "provider output-token usage already includes reasoning tokens "
        "(official Responses/Chat semantics); they are never added twice"
    ),
}


def pricing_schedule() -> dict[str, Any]:
    """The immutable schedule payload (hash-frozen; never mutated in place)."""
    return {
        "schedule_id": PRICING_SCHEDULE_ID,
        "schedule_version": PRICING_SCHEDULE_VERSION,
        "provider": PRICING_PROVIDER,
        "currency": PRICING_CURRENCY,
        "service_tier": PRICING_SERVICE_TIER,
        "retrieved_at": PRICING_RETRIEVED_AT,
        "records": {name: dict(record) for name, record in _PRICING_RECORDS.items()},
        "rules": dict(PRICING_RULES),
        "sources": [dict(source) for source in _PRICING_SOURCES],
        "evidence_state": {
            "gpt-5.6-terra": EVIDENCE_SNAPSHOT_PROVEN,
            "gpt-4o-mini-2024-07-18": EVIDENCE_FAMILY_MAPPING_UNPROVEN,
            "gpt-4.1-2025-04-14": EVIDENCE_FAMILY_MAPPING_UNPROVEN,
            "policy": (
                "family mapping means base inference is expected to bill at "
                "the alias family rate; it is NOT proven by an official "
                "snapshot-billing statement, so cost figures for SMALL/MID "
                "carry that uncertainty explicitly"
            ),
        },
        "validity": (
            "immutable versioned snapshot; price changes require a NEW "
            "schedule version and never mutate this one"
        ),
    }


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def pricing_schedule_hash() -> str:
    return hashlib.sha256(canonical_json(pricing_schedule()).encode("utf-8")).hexdigest()


class MCWPricingMismatchError(RuntimeError):
    """A model or schedule is not covered by the pinned pricing schedule."""


def _rates_for(model: str, schedule: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    payload = schedule if schedule is not None else pricing_schedule()
    record = payload["records"].get(model)
    if record is None:
        raise MCWPricingMismatchError(
            f"model {model!r} is not covered by pricing schedule {payload['schedule_id']!r}"
        )
    return record


def calculate_cost(
    *,
    model: str,
    usage: Mapping[str, Any],
    schedule: Mapping[str, Any] | None = None,
    long_context: bool = False,
) -> dict[str, Any]:
    """Exact Decimal cost from persisted usage under the pinned schedule.

    cost = noncached_input * input_rate
         + cached_input   * cached_rate
         + output         * output_rate

    ``usage`` carries raw provider counts: ``input``, ``cached_input``,
    ``output``.  ``output`` already includes reasoning tokens (official
    provider semantics), so reasoning is never double-counted.
    """

    payload = schedule if schedule is not None else pricing_schedule()
    record = _rates_for(model, payload)
    if long_context and "long_context" in record:
        record = record["long_context"]
    input_t = int(usage.get("input", 0) or 0)
    cached_t = int(usage.get("cached_input", 0) or 0)
    output_t = int(usage.get("output", 0) or 0)
    if cached_t < 0 or input_t < 0 or output_t < 0:
        raise MCWPricingMismatchError("negative token counts are invalid")
    if cached_t > input_t:
        raise MCWPricingMismatchError("cached_input cannot exceed total input tokens")
    noncached_t = input_t - cached_t
    per_token = {
        "input": Decimal(str(record["input_per_1m"])) / Decimal(1_000_000),
        "cached_input": Decimal(str(record["cached_input_per_1m"])) / Decimal(1_000_000),
        "output": Decimal(str(record["output_per_1m"])) / Decimal(1_000_000),
    }
    cost = (
        Decimal(noncached_t) * per_token["input"]
        + Decimal(cached_t) * per_token["cached_input"]
        + Decimal(output_t) * per_token["output"]
    )
    return {
        "calculation_version": COST_CALCULATION_VERSION,
        "schedule_id": payload["schedule_id"],
        "schedule_hash": (
            pricing_schedule_hash() if schedule is None else hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        ),
        "model": model,
        "evidence_status": record.get("evidence_status"),
        "raw_usage": {"input": input_t, "cached_input": cached_t, "noncached_input": noncached_t, "output": output_t},
        "rates_per_token": {name: str(rate) for name, rate in per_token.items()},
        "cost_amount": str(cost),
        "currency": payload["currency"],
        "long_context": bool(long_context),
    }


# Conservative frozen bounds for one MCW cell.
MCW_CELL_INPUT_UPPER_TOKENS = 2048  # 6000-byte context budget (~1500 tok) + system/contract envelope
MCW_CELL_EXPECTED_OUTPUT_TOKENS = 512  # typical small component; EXPECTED estimate only


def cell_cost_bound(*, model: str, output_ceiling: int = 2048) -> dict[str, Any]:
    """Hard configuration upper bound for one cell under the pinned schedule."""
    record = _rates_for(model)
    primary_long_context = record.get("context_mode") == "short_context" and "long_context" in record
    bound = calculate_cost(
        model=model,
        usage={"input": MCW_CELL_INPUT_UPPER_TOKENS, "cached_input": 0, "output": output_ceiling},
    )
    worst_case = None
    if primary_long_context:
        worst_case = calculate_cost(
            model=model,
            usage={"input": MCW_CELL_INPUT_UPPER_TOKENS, "cached_input": 0, "output": output_ceiling},
            long_context=True,
        )
    expected = calculate_cost(
        model=model,
        usage={"input": MCW_CELL_INPUT_UPPER_TOKENS, "cached_input": 0, "output": MCW_CELL_EXPECTED_OUTPUT_TOKENS},
    )
    return {
        "model": model,
        "input_upper_tokens": MCW_CELL_INPUT_UPPER_TOKENS,
        "output_ceiling_tokens": output_ceiling,
        "hard_upper_bound": bound,
        "long_context_worst_case": worst_case,
        "expected_estimate": expected,
        "bound_status": (
            "short-context bound (MCW inputs ~2K tokens; official long-context "
            "threshold unstated, worst case reported separately)"
            if primary_long_context
            else "single-tier bound"
        ),
    }


def live_cost_plan(
    *,
    conditions: Mapping[str, str],
    n_tasks: int,
    output_ceiling: int = 2048,
) -> dict[str, Any]:
    """Per-condition and total conservative/expected bounds for the pilot."""
    per_condition: dict[str, Any] = {}
    total_hard = Decimal("0")
    total_expected = Decimal("0")
    total_worst = Decimal("0")
    for condition_id, model in conditions.items():
        bound = cell_cost_bound(model=model, output_ceiling=output_ceiling)
        hard_cells = Decimal(bound["hard_upper_bound"]["cost_amount"]) * n_tasks
        expected_cells = Decimal(bound["expected_estimate"]["cost_amount"]) * n_tasks
        per_condition[condition_id] = {
            "model": model,
            "cells": n_tasks,
            "hard_upper_bound_total": str(hard_cells),
            "expected_estimate_total": str(expected_cells),
            "per_cell": bound,
        }
        total_hard += hard_cells
        total_expected += expected_cells
        if bound["long_context_worst_case"] is not None:
            total_worst += Decimal(bound["long_context_worst_case"]["cost_amount"]) * n_tasks
    return {
        "n_cells": n_tasks * len(conditions),
        "currency": PRICING_CURRENCY,
        "schedule_id": PRICING_SCHEDULE_ID,
        "schedule_hash": pricing_schedule_hash(),
        "per_condition": per_condition,
        "experiment_hard_upper_bound": str(total_hard),
        "experiment_expected_estimate": str(total_expected),
        "experiment_long_context_worst_case": str(total_worst) if total_worst else None,
        "estimate_boundary": (
            "EXPECTED ESTIMATE (typical output) is separated from HARD "
            "CONFIGURATION UPPER BOUND (full input bound + output ceiling); "
            "figures are Decimal-exact, rounded only for display"
        ),
    }


__all__ = [
    "COST_CALCULATION_VERSION",
    "EVIDENCE_FAMILY_MAPPING_UNPROVEN",
    "EVIDENCE_SNAPSHOT_PROVEN",
    "MCWPricingMismatchError",
    "PRICING_CURRENCY",
    "PRICING_RETRIEVED_AT",
    "PRICING_SCHEDULE_ID",
    "PRICING_SCHEDULE_VERSION",
    "PRICING_SERVICE_TIER",
    "calculate_cost",
    "cell_cost_bound",
    "live_cost_plan",
    "pricing_schedule",
    "pricing_schedule_hash",
]
