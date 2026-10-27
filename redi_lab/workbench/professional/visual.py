"""ZANA visual station: bounded contracts, retained bytes, and independent gates.

The worker receives only a VisualTask. No provider SDK belongs in this module.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import struct
import zlib
from typing import Protocol
from .recovery import RecoveryFault

VISUAL_TASK_VERSION = "zana_visual_task_v1"
VISUAL_MANIFEST_VERSION = "visual_asset_manifest_v1"


class VisualWorker(Protocol):
    """Replaceable adapter receiving only bounded task and failure evidence."""
    def generate(self, task: dict, *, variant_index: int = 1,
                 failure_evidence: dict | None = None) -> dict: ...
STATION = {
    "schema_version": "station_contract_v1", "id": "zana",
    "role": "Visual Generation / Creative Assets",
    "accepted_inputs": [VISUAL_TASK_VERSION],
    "required_outputs": [VISUAL_MANIFEST_VERSION],
    "owned_scope": ["generated visual files", "visual asset manifest"],
    "forbidden_scope": ["page structure", "frontend state", "database", "API", "application assembly"],
    "downstream": ["teuta", "gentius"],
}
FAILURES = {
    "provider_unavailable": "PROVIDER_TRANSIENT",
    "provider_restriction": "PROVIDER_HARD_BLOCKER",
    "budget_credit": "PROVIDER_HARD_BLOCKER",
    "invalid_output": "WORKER_IMPLEMENTATION_DEFECT",
    "decode": "WORKER_IMPLEMENTATION_DEFECT",
    "dimensions": "WORKER_IMPLEMENTATION_DEFECT",
    "requirement_mismatch": "WORKER_IMPLEMENTATION_DEFECT",
    "retention": "HARNESS_OR_GATE_DEFECT",
    "integration": "HARNESS_OR_GATE_DEFECT",
    "review_rejection": "WORKER_IMPLEMENTATION_DEFECT",
}


class VisualFailure(RecoveryFault):
    def __init__(self, kind: str, detail: str):
        if kind not in FAILURES:
            raise ValueError("Unknown visual failure kind")
        owner_reason = ('CREDITS_REQUIRED' if kind == 'budget_credit' else
                        'SAFETY_OR_POLICY_BLOCKER' if kind == 'provider_restriction' else
                        'MANUAL_APPROVAL_REQUIRED' if kind in ('retention','integration') else None)
        super().__init__(detail, FAILURES[kind], owner_reason=owner_reason)
        self.kind = kind
        self.failure_class = FAILURES[kind]


def _id(value):
    if not isinstance(value, str) or not value or len(value) > 64 or not all(c.islower() or c.isdigit() or c == "_" for c in value):
        raise ValueError("Invalid visual identifier")
    return value


class VisualPlacementError(RecoveryFault):
    """A generated visual points outside the finite declared UI locations."""
    def __init__(self, code, requirement_id, view_id, slot_id, valid_targets):
        self.code = code
        self.requirement_id = requirement_id
        self.view_id = view_id
        self.slot_id = slot_id
        self.valid_targets = valid_targets
        self.failure_signature = 'INVALID_VISUAL_PLACEMENT:' + code
        super().__init__('INVALID_VISUAL_PLACEMENT ' + code + ' for ' + str(requirement_id),
                         'WORKER_IMPLEMENTATION_DEFECT')


def placement_targets(spec):
    """Return bounded, explicit targets suitable for Ora correction evidence."""
    targets = []
    for view in spec.get('views', []):
        for slot in view.get('slots', []):
            if slot.get('accepts_visual') is True:
                targets.append({'view_id': view['id'], 'slot_id': slot['id'],
                                'purpose': slot['purpose'], 'region': slot['region']})
    if len(targets) > 48:
        raise ValueError('Too many visual placement targets')
    return targets


def placement_key(requirement):
    return requirement['view_id'] + ':' + requirement['slot_id']


def validate_requirements(spec):
    requirements = spec.get("visual_requirements", [])
    if not isinstance(requirements, list) or len(requirements) > 12:
        raise ValueError("Visual requirements must be a bounded list")
    seen = set(); used_targets = set()
    views = {view['id']: view for view in spec.get('views', [])}
    if requirements or spec.get('schema_version') == 'professional_project_spec_v2':
        for view in views.values():
            slots = view.get('slots')
            if not isinstance(slots, list) or len(slots) > 12:
                raise ValueError('Visual ProductSpec views require bounded declared slots')
            ids = set()
            for slot in slots:
                if not isinstance(slot, dict) or set(slot) != {'id','purpose','region','accepts_visual'}:
                    raise ValueError('Invalid visual slot declaration')
                sid = _id(slot['id'])
                if sid in ids or not isinstance(slot['purpose'], str) or not slot['purpose'].strip() or len(slot['purpose']) > 240 or slot['region'] not in {'header','content'} or type(slot['accepts_visual']) is not bool:
                    raise ValueError('Invalid or duplicate visual slot declaration')
                ids.add(sid)
    targets = placement_targets(spec)
    for req in requirements:
        if not isinstance(req, dict):
            raise ValueError("Invalid visual requirement")
        required = {"id", "purpose", "asset_type", "aspect_ratio", "min_width", "min_height", "view_id", "slot_id", "style", "subject", "content_constraints", "forbidden_content", "text_in_image", "transparent", "variants", "format", "blocking", "layout_dependency"}
        if set(req) != required:
            if 'placement' in req and ('view_id' not in req or 'slot_id' not in req):
                raise VisualPlacementError('PROSE_ONLY',req.get('id'),req.get('view_id'),req.get('slot_id'),targets)
            raise ValueError("Visual requirement fields are incomplete or unexpected")
        rid = _id(req["id"])
        if rid in seen:
            raise ValueError("Duplicate visual requirement")
        seen.add(rid)
        if not all(isinstance(req[k], str) and req[k].strip() for k in ("purpose", "asset_type", "style", "subject")):
            raise ValueError("Visual purpose, placement and direction required")
        vid, sid = req['view_id'], req['slot_id']
        if not isinstance(vid,str) or vid not in views:
            raise VisualPlacementError('VIEW_NOT_DECLARED',rid,vid,sid,targets)
        slots = {slot['id']:slot for slot in views[vid]['slots']}
        if not isinstance(sid,str) or sid not in slots:
            elsewhere = any(sid == slot['id'] for other in views.values() if other['id'] != vid for slot in other['slots'])
            raise VisualPlacementError('SLOT_BELONGS_TO_OTHER_VIEW' if elsewhere else 'SLOT_NOT_DECLARED',rid,vid,sid,targets)
        if not slots[sid]['accepts_visual']:
            raise VisualPlacementError('SLOT_NOT_VISUAL_CAPABLE',rid,vid,sid,targets)
        if (vid,sid) in used_targets:
            raise VisualPlacementError('SLOT_ALREADY_TARGETED',rid,vid,sid,targets)
        used_targets.add((vid,sid))
        ratio = req["aspect_ratio"]
        if not isinstance(ratio, list) or len(ratio) != 2 or any(type(n) is not int or n <= 0 for n in ratio):
            raise ValueError("Invalid aspect ratio")
        if any(type(req[k]) is not int or req[k] < 1 for k in ("min_width", "min_height", "variants")) or req["variants"] > 8 or max(req["min_width"],req["min_height"]) > 8192 or req["min_width"]*req["min_height"] > 30_000_000:
            raise ValueError("Invalid visual dimensions or variant count")
        if len(json.dumps(req, ensure_ascii=False).encode("utf-8")) > 12000:
            raise ValueError("Visual task context exceeds bound")
        if any(type(req[k]) is not bool for k in ("text_in_image", "transparent", "blocking")):
            raise ValueError("Invalid visual flags")
        if req["format"] != "png" or req["layout_dependency"] not in {"parallel", "visual_first"}:
            raise ValueError("Unsupported visual format or dependency")
        if any(not isinstance(req[k], list) or any(not isinstance(v, str) for v in req[k]) for k in ("content_constraints", "forbidden_content")):
            raise ValueError("Invalid visual constraints")
    return requirements


def visual_task(spec, req, run_id):
    validate_requirements(spec)
    return {"schema_version": VISUAL_TASK_VERSION, "task_id": "visual_" + req["id"],
            "product_run_id": run_id, "visual_requirement_id": req["id"], "purpose": req["purpose"],
            "asset_type": req["asset_type"], "view_id":req['view_id'], "slot_id":req['slot_id'],
            "target_placement": placement_key(req),
            "aspect_ratio": req["aspect_ratio"], "min_width": req["min_width"], "min_height": req["min_height"],
            "style": req["style"], "subject": req["subject"], "content_constraints": req["content_constraints"],
            "forbidden_content": req["forbidden_content"], "text_in_image": req["text_in_image"],
            "transparent": req["transparent"], "variants": req["variants"], "expected_format": req["format"],
            "blocking": req["blocking"], "owning_station": "zana", "downstream_consumers": ["teuta", "gentius"],
            "verification_requirements": ["decode", "dimensions", "aspect_ratio", "retention", "independent_review", "integration"]}


def validate_task(task):
    if task.get("schema_version") != VISUAL_TASK_VERSION or task.get("owning_station") != "zana":
        raise ValueError("Invalid VisualTask version or station")
    fields = {"schema_version", "task_id", "product_run_id", "visual_requirement_id", "purpose", "asset_type",
              "view_id", "slot_id", "target_placement", "aspect_ratio", "min_width", "min_height", "style", "subject",
              "content_constraints", "forbidden_content", "text_in_image", "transparent", "variants",
              "expected_format", "blocking", "owning_station", "downstream_consumers", "verification_requirements"}
    if set(task) != fields:
        raise ValueError("Invalid VisualTask fields")
    _id(task["visual_requirement_id"])
    if task["task_id"] != "visual_" + task["visual_requirement_id"]:
        raise ValueError("Visual task identity mismatch")
    if task['target_placement'] != _id(task['view_id']) + ':' + _id(task['slot_id']):
        raise ValueError('Visual task placement identity mismatch')
    if not isinstance(task["product_run_id"], str) or not task["product_run_id"]:
        raise ValueError("Visual run identity missing")
    if task["expected_format"] != "png" or task["downstream_consumers"] != ["teuta", "gentius"]:
        raise ValueError("Visual output contract mismatch")
    if any(type(task[k]) is not int or task[k] < 1 for k in ("min_width", "min_height", "variants")):
        raise ValueError("Invalid visual dimensions")
    if not isinstance(task["aspect_ratio"], list) or len(task["aspect_ratio"]) != 2 or any(type(n) is not int or n < 1 for n in task["aspect_ratio"]):
        raise ValueError("Invalid visual aspect ratio")
    return task


def png_dimensions(data: bytes):
    """Decode PNG chunks and pixel stream; reject truncated/corrupt image bytes."""
    if not isinstance(data, bytes) or len(data) < 45 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise VisualFailure("invalid_output", "Expected nonempty PNG bytes")
    offset = 8; pixels = bytearray(); width = height = mode = None; ended = False
    while offset + 12 <= len(data):
        size = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset+4:offset+8]; end = offset + 12 + size
        if end > len(data) or zlib.crc32(data[offset+4:end-4]) & 0xffffffff != struct.unpack_from(">I", data, end-4)[0]:
            raise VisualFailure("decode", "Corrupt PNG chunk")
        payload = data[offset+8:end-4]
        if kind == b"IHDR":
            if size != 13 or width is not None:
                raise VisualFailure("decode", "Invalid PNG header")
            width, height, depth, mode, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
            if not width or not height or width * height > 30_000_000 or depth != 8 or mode not in (2, 6) or compression or filtering or interlace:
                raise VisualFailure("decode", "Unsupported or invalid PNG encoding")
        elif kind == b"IDAT": pixels.extend(payload)
        elif kind == b"IEND": ended = True; offset = end; break
        offset = end
    if not ended or offset != len(data) or width is None:
        raise VisualFailure("decode", "Incomplete PNG")
    expected = height * (1 + width * (4 if mode == 6 else 3))
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(bytes(pixels), expected + 1) + decoder.flush(expected + 1)
    except zlib.error as exc: raise VisualFailure("decode", "PNG pixel stream cannot be decoded") from exc
    if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail or len(raw) != expected:
        raise VisualFailure("decode", "PNG pixel length mismatch")
    stride = 1 + width * (4 if mode == 6 else 3)
    if any(raw[y * stride] > 4 for y in range(height)):
        raise VisualFailure("decode", "Unsupported PNG filter")
    return width, height, mode == 6


def retain_visual(task, result, root, *, attempt=1, variant_index=1, independent_review=None):
    """Retain every attempt before gates; a failing attempt never replaces bytes."""
    validate_task(task)
    if type(attempt) is not int or attempt < 1 or attempt > 20 or type(variant_index) is not int or not 1<=variant_index<=task['variants']:
        raise ValueError("Invalid visual attempt")
    if not isinstance(result, dict) or not isinstance(result.get("bytes"), bytes):
        raise VisualFailure("invalid_output", "Visual worker returned no bytes")
    profile = result.get("worker_profile")
    if not isinstance(profile, str) or not profile:
        raise VisualFailure("invalid_output", "Visual worker profile missing")
    cost = Decimal(str(result.get("cost_usd", "0")))
    if not cost.is_finite() or cost < 0:
        raise VisualFailure("budget_credit", "Invalid visual call cost")
    reserved = Decimal(str(result.get("reserved_cost_usd", "0")))
    if not reserved.is_finite() or reserved < 0 or result.get("cost_evidence_state", "USAGE_RECONCILED") not in ("USAGE_RECONCILED", "RESERVATION_PENDING"):
        raise VisualFailure("budget_credit", "Invalid visual cost evidence")
    prompt_hash = result.get("provider_prompt_sha256")
    if prompt_hash is not None and (not isinstance(prompt_hash, str) or len(prompt_hash) != 64 or any(ch not in "0123456789abcdef" for ch in prompt_hash)):
        raise VisualFailure("invalid_output", "Invalid visual prompt hash")
    duration = result.get("duration_seconds")
    if duration is not None and (type(duration) not in (int, float) or not 0 <= duration <= 3600):
        raise VisualFailure("invalid_output", "Invalid visual generation duration")
    parameters = result.get("safe_parameters", {})
    if not isinstance(parameters, dict) or set(parameters) - {"width", "height", "seed", "steps", "quality"} or any(type(value) not in (int, float, str) or len(str(value)) > 40 for value in parameters.values()):
        raise VisualFailure("invalid_output", "Unsafe visual generation metadata")
    for key in ("provider", "model", "provider_call_id"):
        value = result.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > 128 or any(ch in value for ch in "\r\n\\/")):
            raise VisualFailure("invalid_output", "Unsafe visual provider metadata")
    root = Path(root).resolve(); directory = root / "visual" / task["task_id"] / f"attempt-{attempt:03d}"
    directory.mkdir(parents=True, exist_ok=False)
    data = result["bytes"]
    path = directory / "generated.png"
    path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    (directory / "attempt.json").write_text(json.dumps({"task": task, "sha256": digest, "byte_size": len(data),
        "worker_profile": profile, "provider": result.get("provider"), "model": result.get("model"),
        "cost_usd": str(cost), "reserved_cost_usd": str(reserved),
        "cost_evidence_state": result.get("cost_evidence_state", "USAGE_RECONCILED"),
        "provider_prompt_sha256": prompt_hash}, indent=2), encoding="utf-8")
    if not data or path.read_bytes() != data:
        raise VisualFailure("retention", "Visual bytes were not retained exactly")
    width, height, alpha = png_dimensions(data)
    if 'width' in parameters and 'height' in parameters and (width, height) != (parameters['width'], parameters['height']):
        raise VisualFailure("dimensions", "Visual output differs from requested dimensions")
    if width < task["min_width"] or height < task["min_height"]:
        raise VisualFailure("dimensions", "Visual dimensions below minimum")
    target = task["aspect_ratio"][0] / task["aspect_ratio"][1]
    if abs(width / height / target - 1) > 0.02:
        raise VisualFailure("dimensions", "Visual aspect ratio outside 2% tolerance")
    if task["transparent"] and not alpha:
        raise VisualFailure("requirement_mismatch", "Transparency required")
    if independent_review is None:
        raise VisualFailure("review_rejection", "Independent visual review is required")
    review = independent_review(task, path)
    if not isinstance(review, dict) or review.get("reviewer") == "zana" or review.get("verdict") != "PASS":
        raise VisualFailure("review_rejection", "Independent visual review rejected asset")
    row = {"schema_version": VISUAL_MANIFEST_VERSION,
           "asset_id": task["visual_requirement_id"] if variant_index==1 else task["visual_requirement_id"]+f"_v{variant_index:03d}",
           "visual_requirement_id":task["visual_requirement_id"],"variant_index":variant_index,
           "visual_task_id": task["task_id"],
           "product_run_id": task["product_run_id"], "producing_station": "zana",
           "worker_profile": profile, "provider": result.get("provider"), "model": result.get("model"),
           "generation_parameters": parameters, "prompt_context_sha256": hashlib.sha256(json.dumps(task, sort_keys=True).encode()).hexdigest(),
           "provider_prompt_sha256": prompt_hash,
           "generated_file_path": str(path.relative_to(root)).replace("\\", "/"), "file_type": "image/png",
           "width": width, "height": height, "aspect_ratio": [width, height], "byte_size": len(data),
           "content_sha256": digest, "timestamp": datetime.now(timezone.utc).isoformat(),
           "cost_usd": str(cost), "reserved_cost_usd": str(reserved),
           "cost_evidence_state": result.get("cost_evidence_state", "USAGE_RECONCILED"),
           "generation_duration_seconds": duration, "provider_call_id": result.get("provider_call_id"),
           "verification": {"deterministic": "PASS", "independent_review": review},
           "downstream_consumers": task["downstream_consumers"], "view_id":task['view_id'],
           "slot_id":task['slot_id'], "intended_placement": task["target_placement"],
           "semantic_purpose": task["purpose"], "final_integration_path": None, "actually_used": False}
    (directory / "manifest.json").write_text(json.dumps(row, indent=2), encoding="utf-8")
    return row


def verify_manifest(row, root):
    if row.get("schema_version") != VISUAL_MANIFEST_VERSION or row.get("producing_station") != "zana":
        raise VisualFailure("retention", "Invalid visual manifest")
    if row.get('intended_placement') != str(row.get('view_id'))+':'+str(row.get('slot_id')):
        raise VisualFailure('retention','Visual manifest placement identity mismatch')
    root = Path(root).resolve(); path = (root / row["generated_file_path"]).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise VisualFailure("retention", "Visual retained path missing or escaped run")
    data = path.read_bytes(); width, height, _ = png_dimensions(data)
    if hashlib.sha256(data).hexdigest() != row["content_sha256"] or len(data) != row["byte_size"] or (width, height) != (row["width"], row["height"]):
        raise VisualFailure("retention", "Visual manifest does not match retained bytes")
    return path


class DeterministicFakeVisualWorker:
    """Offline injected worker; no network path and no paid calls."""
    profile = "deterministic_fake_visual_v1"
    calls = 0

    def generate(self, task, *, variant_index=1, failure_evidence=None):
        validate_task(task)
        self.calls += 1
        width = max(task["min_width"], task["aspect_ratio"][0] * 80)
        height = max(task["min_height"], task["aspect_ratio"][1] * 80)
        # Preserve exact requested ratio while meeting both minima.
        scale = max((width + task["aspect_ratio"][0] - 1) // task["aspect_ratio"][0],
                    (height + task["aspect_ratio"][1] - 1) // task["aspect_ratio"][1])
        width, height = (n * scale for n in task["aspect_ratio"])
        color = hashlib.sha256((task["visual_requirement_id"]+str(variant_index)).encode()).digest()[:3]
        rows = b"".join(b"\x00" + color * width for _ in range(height))
        def chunk(kind, payload):
            return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)
        data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")
        return {"bytes": data, "worker_profile": self.profile, "provider": "injected_fake", "model": "deterministic_png_v1", "cost_usd": "0", "safe_parameters": {"width": width, "height": height}}
