"""Read-only UI projection. Never changes execution, verdicts or retained bytes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from .crew import CREW
from .product_identity import product_name


def _read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def present_snapshot(snapshot, root):
    """Add display evidence without replacing the engine's authoritative fields.

    Qualification and generation are different runs: a hash-bound source link
    is exposed separately, never transplanted into qualification worker states.
    """
    snap = dict(snapshot)
    bid = snap.get("build_id", "")
    if snap.get("error") and not snap.get("status"):
        return snap
    if not re.fullmatch(r"[a-f0-9]{12}", bid):
        return snap
    directory = Path(root) / ("wb-" + bid)
    continuation = snap.get('continuation_segment_id')
    evidence_directory = directory / 'segments' / continuation if continuation else directory
    retained = _read(evidence_directory / "live_run_evidence.json")
    source = snap if continuation and snap.get('status') == 'RUNNING' else retained or snap
    manifest = source.get("manifest") or snap.get("manifest") or _read(directory / "execution_manifest.json")
    events = source.get("events") or [{"event": e} for e in snap.get("live_events", [])]
    if not retained and (directory / "events.jsonl").is_file():
        # A final record does not exist while the professional DAG is running.
        # Read only complete JSONL rows; a concurrent append may be partial.
        try:
            rows = (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()
            recorded = []
            for row in rows:
                try:
                    recorded.append(json.loads(row))
                except ValueError:
                    break
            events = recorded or events
        except OSError:
            pass
    facts = source.get("call_facts") or snap.get("call_facts") or {}
    calls = facts.get("per_task", {})
    providers = sorted({f["provider"] for f in calls.values() if f.get("provider")})
    models = sorted({f["model"] for f in calls.values() if f.get("model")})
    elapsed = source.get("elapsed_seconds")
    provider_calls = facts.get("calls", source.get("provider_calls"))
    if not facts and not retained and snap.get("status") == "RUNNING" and snap.get("backend", "").startswith("openai-live"):
        # The legacy in-flight snapshot initializes calls to zero before it
        # receives provider facts. That is not an observed live measurement.
        provider_calls = None
    # Older qualification records contain a zero placeholder, not a measurement.
    if source.get("execution_mode") == "retained_qualification_offline" and not elapsed:
        elapsed = None
    order = _read(directory / "product_order.json")
    prompts = [s.get("text", "") for s in order.get("sources", []) if s.get("kind") == "owner_prompt"]
    prompt = source.get("prompt") or snap.get("prompt") or "\n".join(prompts) or None
    spec = source.get("product_spec") or _read(directory / "product_spec.json").get("spec")
    identity = product_name(spec, source, prompt) or product_name(metadata=snap, prompt=prompt)
    # A legacy recipe title is safe only when the request itself identifies that recipe.
    if not identity and prompt:
        from .normal_app import resolve_scope
        if resolve_scope(prompt)["supported"]:
            identity = "Mini CRM"
    if not identity and snap.get('can_open_app') and snap.get('status') == 'VERIFIED':
        package = _read(directory / 'app' / 'package.json')
        identity = product_name(metadata=package)
        if not identity and _read(directory / 'app' / 'app.json').get('module') == 'mini_crm.service':
            identity = 'Mini CRM'
    snap["product_name"] = identity
    display = {
        "personas": list(CREW), "manifest": manifest or None,
        "events": events, "call_facts": facts,
        "prompt": prompt,
        "provider": source.get("provider") or ", ".join(providers) or None,
        "model": source.get("model") or ", ".join(models) or None,
        "elapsed_seconds": elapsed,
        "provider_calls": provider_calls,
        "cost_usd": source.get("total_cost_usd", source.get("cost_usd")),
        "input_tokens": sum(f["input"] for f in calls.values()) if calls and all("input" in f for f in calls.values()) else None,
        "output_tokens": sum(f["output"] for f in calls.values()) if calls and all("output" in f for f in calls.values()) else None,
        "gate_reasons": source.get("gate_reasons") or {},
        "failures": source.get("failures") or {},
        "failure_stage": source.get("failure_stage"),
        "evidence_path": str(evidence_directory / "live_run_evidence.json") if retained else None,
        "files": sorted(str(f.relative_to(directory)) for f in directory.glob("*") if f.is_file()),
        "source_run": None,
    }
    for task in (manifest or {}).get("tasks", []):
        display["files"].extend(task.get("owned_paths", []) or ([task["artifact"]] if task.get("artifact") else []))
    origin = _read(directory / "qualification_origin.json")
    source_id = source.get("source_build_id", "")
    if re.fullmatch(r"wb-[a-f0-9]{12}", source_id) and origin.get("source_build_id") == source_id:
        path = Path(root) / source_id / "live_run_evidence.json"
        try:
            matched = hashlib.sha256(path.read_bytes()).hexdigest() == origin.get("source_evidence_sha256")
        except OSError:
            matched = False
        if matched:
            display["source_run"] = {"build_id": source_id[3:], "status": origin.get("original_result"),
                                     "label": "Original generation", "sha256": origin["source_evidence_sha256"]}
    snap["presentation"] = display
    return snap
