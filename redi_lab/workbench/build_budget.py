"""Immutable owner budget for one Workbench build. No credential material is stored."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import time


def _money(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"\d+(?:\.\d{1,2})?", value):
        raise ValueError(f"Enter a positive {label} in USD (up to two decimal places).")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid {label}.") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError(f"Enter a positive {label} in USD.")
    return f"{amount:.2f}"


def _limit(value, label, lower, upper):
    if type(value) is int:
        number = value
    elif isinstance(value, str) and re.fullmatch(r"\d+", value):
        number = int(value)
    else:
        raise ValueError(f"Enter {label} as a whole number from {lower} to {upper}.")
    if not lower <= number <= upper:
        raise ValueError(f"Enter {label} as a whole number from {lower} to {upper}.")
    return number


def make_authorization(build_id, backend, *, overall_ceiling, visual_ceiling=None,
                       max_visual_calls=None, max_visual_recovery=None,
                       visual_required=False, runtime, credential_present, live_arm, visual_arm):
    if not backend.startswith("openai-live"):
        raise ValueError("Budget authorization applies only to live backends.")
    overall = _money(overall_ceiling, "overall product ceiling")
    visual = None
    calls = recovery = None
    if isinstance(visual_ceiling, str) and re.fullmatch(r"0(?:\.0{1,2})?", visual_ceiling):
        # Explicit owner choice: no ZANA visual provider for this build. Visual
        # tasks then fail closed as EXTERNAL_SERVICE_REQUIRED; nothing is spent.
        visual, calls, recovery = "0.00", 0, 0
    elif visual_required or visual_ceiling is not None:
        visual = _money(visual_ceiling, "visual ceiling")
        if Decimal(visual) > Decimal(overall):
            raise ValueError("Visual ceiling cannot exceed the overall product ceiling.")
        calls = _limit(max_visual_calls, "maximum visual calls", 1, 3)
        recovery = _limit(max_visual_recovery, "maximum visual recovery generations", 0, calls - 1)
        if Decimal(visual) < Decimal("1.00") or Decimal(overall) < Decimal("1.00"):
            raise ValueError("Visual and overall ceilings must each reserve at least USD 1.00 for the next visual call.")
    return {"schema_version": "workbench_build_authorization_v1", "logical_run_id": build_id,
            "backend": backend, "created_at": time.time(),
            "overall_ceiling_usd": overall, "visual_ceiling_usd": visual,
            "max_visual_generation_calls": calls, "max_visual_recovery_generations": recovery,
            "visuals_disabled": visual == "0.00",
            "runtime_identity": {"fingerprint": runtime["fingerprint"], "source_contract": runtime["source_contract"],
                                 "fresh_at_authorization": runtime["fresh"], "changed_files": runtime["changed_files"]},
            "authorization_state": {"credential_present": bool(credential_present),
                                    "live_arm": bool(live_arm), "visual_arm": bool(visual_arm)}}


def visuals_disabled(record):
    """True when the owner explicitly authorized this build without visual provider calls."""
    return record.get("visual_ceiling_usd") == "0.00" and record.get("max_visual_generation_calls") == 0


def write_authorization(root, record):
    path = Path(root) / "build_authorization.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(record, stream, sort_keys=True, indent=2)
        stream.flush()
    return path


def read_authorization(root, build_id):
    record = json.loads((Path(root) / "build_authorization.json").read_text(encoding="utf-8"))
    if record.get("schema_version") != "workbench_build_authorization_v1" or record.get("logical_run_id") != build_id:
        raise ValueError("Retained build authorization does not match this run.")
    return record


def budget_telemetry(record, *, total_spent="0", visual_spent="0", total_reserved="0", visual_reserved="0"):
    overall = Decimal(record["overall_ceiling_usd"])
    visual = Decimal(record["visual_ceiling_usd"]) if record["visual_ceiling_usd"] else None
    spent = Decimal(str(total_spent))
    vspent = Decimal(str(visual_spent))
    reserved = Decimal(str(total_reserved))
    vreserved = Decimal(str(visual_reserved))
    return {"authorized_overall_usd": str(overall), "authorized_visual_usd": str(visual) if visual is not None else None,
            "spent_total_usd": str(spent), "spent_visual_usd": str(vspent),
            "reserved_total_usd": str(reserved), "reserved_visual_usd": str(vreserved),
            "remaining_total_usd": str(overall - spent - reserved),
            "remaining_visual_usd": str(visual - vspent - vreserved) if visual is not None else None,
            "max_visual_generation_calls": record["max_visual_generation_calls"],
            "max_visual_recovery_generations": record["max_visual_recovery_generations"]}
