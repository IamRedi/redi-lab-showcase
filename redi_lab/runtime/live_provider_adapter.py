"""Single live provider adapter.  ONE adapter MAX (REDI-03 constraint).

For REDI-03 we implement exactly one live provider: OpenAI Chat Completions via
optional `openai` SDK (stdlib import guards).  If the SDK is not importable OR
the API key environment variable is not set → all calls raise
`LiveProviderBlockedError` with a clean reason string.  Tests use this signal to
skip gracefully; live-run runner script treats it as CODE PASS / LIVE BLOCKED
verdict per directive.

Secrets are NEVER hard-coded: API key only from `os.environ[env_key_name]`.
Model ID / timeout / token limits are ALL configuration.  Raw provider response
types NEVER cross this file boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from ..domain import (
    ContextManifestId,
    StationId,
    TaskId,
    TokenUsage,
    WorkerId,
)
from .contracts import (
    NodeRunResult,
    ParsedPatchObservation,
    PatchEdit,
    PatchOutputContract,
    ReviewerVerdict,
    WorkerRunner,
)


SECRET_BLOCKLIST_RE: re.Pattern[str] = re.compile(
    r"(HOME|USER(?:NAME)?|KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|APIKEY)",
    re.IGNORECASE,
)

BUILDER_OUTPUT_SCHEMA_NAME = "RediBuilderOutput"
REVIEWER_OUTPUT_SCHEMA_NAME = "RediReviewerOutput"

# Strict JSON-Schema-style dictionaries used for two purposes:
#   (a) sent to OpenAI as response_format={"type":"json_schema","json_schema":{name,strict,schema}}
#       when the installed SDK supports official Structured Outputs;
#   (b) validated locally with a small custom semantic validator in
#       _validate_station_schema() after json.loads, as defense-in-depth.
# These dictionaries live in the adapter (Layer B) only; they never flow into
# Layer A domain objects.  Allowed values match REDI station contracts exactly.
BUILDER_OUTPUT_JSON_SCHEMA: dict[str, Any] = {
    "name": BUILDER_OUTPUT_SCHEMA_NAME,
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "patches": {
                "type": "array",
                "items": {
                    "anyOf": [
                        {
                            "type": "object",
                            "properties": {
                                "rel_path": {"type": "string"},
                                "action": {"type": "string", "enum": ["write"]},
                                "content": {"type": "string"},
                            },
                            "required": ["rel_path", "action", "content"],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "properties": {
                                "rel_path": {"type": "string"},
                                "action": {"type": "string", "enum": ["delete"]},
                            },
                            "required": ["rel_path", "action"],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "properties": {
                                "rel_path": {"type": "string"},
                                "action": {"type": "string", "enum": ["delete"]},
                                "content": {"type": "null"},
                            },
                            "required": ["rel_path", "action", "content"],
                            "additionalProperties": False,
                        },
                    ],
                },
            },
        },
        "required": ["patches"],
        "additionalProperties": False,
    },
}

REVIEWER_OUTPUT_JSON_SCHEMA: dict[str, Any] = {
    "name": REVIEWER_OUTPUT_SCHEMA_NAME,
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": [ReviewerVerdict.PASS, ReviewerVerdict.FAIL, ReviewerVerdict.ESCALATE],
            },
            "reasons": {"type": "array", "items": {"type": "string"}},
            "evidence_refs": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["verdict", "reasons", "evidence_refs"],
        "additionalProperties": False,
    },
}

LIVE_STATION_SYSTEM_PROMPT = (
    "You are a REDI-Lab production station. Output ONLY valid JSON matching the schema requested: "
    "Builder returns {patches:[{rel_path,action,content}]}, Reviewer returns {verdict: 'PASS'|'FAIL'|'ESCALATE', reasons:[...], evidence_refs:[...]} . "
    "Do NOT include secrets, home paths, or credentials in any output. Output code only for assigned scope."
)


def _builder_output_schema(contract: PatchOutputContract | None) -> dict[str, Any]:
    """Build a strict schema from the same immutable task contract."""
    if contract is None:
        return dict(BUILDER_OUTPUT_JSON_SCHEMA)
    return {
        "name": BUILDER_OUTPUT_SCHEMA_NAME,
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "patches": {
                    "type": "array",
                    "minItems": contract.expected_edit_count,
                    "maxItems": contract.expected_edit_count,
                    "items": {
                        "type": "object",
                        "properties": {
                            "rel_path": {"type": "string", "enum": [contract.expected_path]},
                            "action": {"type": "string", "enum": [contract.expected_action]},
                            "content": {"type": "string"},
                        },
                        "required": ["rel_path", "action", "content"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["patches"],
            "additionalProperties": False,
        },
    }


def render_live_request(
    *,
    role: str,
    task_id: str,
    instructions: str,
    context_body: str,
    patch_output_contract: PatchOutputContract | None = None,
) -> dict[str, Any]:
    """Render provider bytes with separate task-semantic and request hashes."""

    user_content = (
        f"## Station role: {role}\n"
        f"## Task id: {task_id}\n"
        f"## Instructions\n{instructions}\n"
        f"## Context\n{context_body}\n"
    )
    if role == "builder" and patch_output_contract is not None:
        user_content += "\n" + patch_output_contract.builder_instruction() + "\n"
    response_format = _response_format_for_role(
        role, patch_output_contract=patch_output_contract
    )
    schema_json = json.dumps(response_format, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    system_bytes = len(LIVE_STATION_SYSTEM_PROMPT.encode("utf-8"))
    user_bytes = len(user_content.encode("utf-8"))
    schema_bytes = len(schema_json.encode("utf-8"))
    semantic_hash = hashlib.sha256((instructions + "\0" + context_body).encode("utf-8")).hexdigest()
    request_view = {
        "system_prompt": LIVE_STATION_SYSTEM_PROMPT,
        "user_content": user_content,
        "response_format": response_format,
    }
    full_request_hash = hashlib.sha256(
        json.dumps(request_view, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "system_prompt": LIVE_STATION_SYSTEM_PROMPT,
        "user_content": user_content,
        "response_format": response_format,
        "system_bytes": system_bytes,
        "user_bytes": user_bytes,
        "schema_bytes": schema_bytes,
        "rendered_request_bytes": system_bytes + user_bytes + schema_bytes,
        "task_semantic_context_hash": semantic_hash,
        "full_request_hash": full_request_hash,
        "output_contract": (
            patch_output_contract.to_dict() | {"canonical_hash": patch_output_contract.canonical_hash}
            if patch_output_contract is not None
            else None
        ),
    }


def _response_format_for_role(
    role: str, *, patch_output_contract: PatchOutputContract | None = None
) -> dict[str, Any] | None:
    """Return the preferred response_format dict for the station role.

    Prefers the official `type=json_schema` Structured Output.  Falls back
    EXPLICITLY (and observably) to `type=json_object` only when the role is
    unknown.  We never silently demote a known role from json_schema to
    json_object on TypeError; instead that failure path is exposed as an
    explicit SCHEMA_FAILED category via the caller.
    """
    if role == "builder":
        return {"type": "json_schema", "json_schema": _builder_output_schema(patch_output_contract)}
    if role == "reviewer":
        return {"type": "json_schema", "json_schema": dict(REVIEWER_OUTPUT_JSON_SCHEMA)}
    return {"type": "json_object"}


def render_responses_request(
    *,
    role: str,
    task_id: str,
    instructions: str,
    context_body: str,
    patch_output_contract: PatchOutputContract | None = None,
    model: str,
    reasoning_effort: str | None,
    max_output_tokens: int,
) -> dict[str, Any]:
    """Render the Responses-native create() kwargs for one stateless call.

    The task-semantic payload (instructions + context body + builder output
    contract instruction) is byte-identical to the Chat Completions rendering —
    only the API envelope differs.  Official Responses semantics used here:
    system prompt travels as top-level ``instructions``, the user turn as
    ``input``, reasoning effort as ``reasoning={effort: ...}``, the total
    generation bound as ``max_output_tokens`` (reasoning AND visible output
    share this bound), and the strict patch schema as
    ``text.format={type: json_schema, name, strict, schema}``.  ``store=False``
    keeps the call stateless; tools, web search, code interpreter,
    ``previous_response_id``, and background execution are never enabled.
    """

    chat_view = render_live_request(
        role=role,
        task_id=task_id,
        instructions=instructions,
        context_body=context_body,
        patch_output_contract=patch_output_contract,
    )
    chat_format = chat_view["response_format"]
    if isinstance(chat_format, dict) and chat_format.get("type") == "json_schema":
        # Responses carries name/strict/schema at the format level (no envelope).
        text_format: dict[str, Any] = {"type": "json_schema", **chat_format["json_schema"]}
    else:
        text_format = dict(chat_format or {"type": "json_object"})
    create_kwargs: dict[str, Any] = {
        "model": model,
        "instructions": chat_view["system_prompt"],
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": chat_view["user_content"]}]}
        ],
        "max_output_tokens": int(max_output_tokens),
        "text": {"format": text_format},
        "store": False,
    }
    if reasoning_effort is not None:
        create_kwargs["reasoning"] = {"effort": str(reasoning_effort)}
    request_hash = hashlib.sha256(
        json.dumps(create_kwargs, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "create_kwargs": create_kwargs,
        "responses_request_hash": request_hash,
        "task_semantic_context_hash": chat_view["task_semantic_context_hash"],
        "chat_full_request_hash": chat_view["full_request_hash"],
    }


def _envelope_get(envelope: Any, name: str, default: Any = None) -> Any:
    """Read a field from either a SDK-style object or a plain dict."""
    if isinstance(envelope, dict):
        return envelope.get(name, default)
    return getattr(envelope, name, default)


def normalize_responses_result(
    envelope: Any, *, elapsed_seconds: float
) -> dict[str, Any]:
    """Normalize a Responses API result into safe REDI telemetry concepts.

    Accepts the SDK response object or an equivalent plain mapping.  The hidden
    reasoning items and raw response object never cross this function: only
    counts, statuses, hashes, and the visible ``output_text`` are returned.
    Field semantics per the official references: ``status`` is one of
    completed/failed/in_progress/cancelled/queued/incomplete;
    ``incomplete_details.reason`` is e.g. ``max_output_tokens``; usage carries
    ``input_tokens``/``output_tokens`` with
    ``output_tokens_details.reasoning_tokens`` and
    ``input_tokens_details.cached_tokens``; visible text comes from
    ``output`` message items' ``output_text`` parts; refusals surface as
    ``refusal`` content parts.
    """

    status = _envelope_get(envelope, "status", None)
    status = str(status) if status is not None else None
    incomplete_details = _envelope_get(envelope, "incomplete_details", None)
    incomplete_reason = _envelope_get(incomplete_details, "reason", None)
    incomplete_reason = str(incomplete_reason) if incomplete_reason is not None else None
    error = _envelope_get(envelope, "error", None)
    error_code = _envelope_get(error, "code", None)
    error_message = _envelope_get(error, "message", None)
    response_id = _envelope_get(envelope, "id", None)
    usage = _envelope_get(envelope, "usage", None)
    input_t = int(_envelope_get(usage, "input_tokens", 0) or 0)
    output_t = int(_envelope_get(usage, "output_tokens", 0) or 0)
    total_t = int(_envelope_get(usage, "total_tokens", 0) or (input_t + output_t))
    cached_t = int(
        _envelope_get(_envelope_get(usage, "input_tokens_details", None), "cached_tokens", 0) or 0
    )
    reasoning_t = int(
        _envelope_get(_envelope_get(usage, "output_tokens_details", None), "reasoning_tokens", 0) or 0
    )
    visible_parts: list[str] = []
    refusal_present = False
    for item in _envelope_get(envelope, "output", None) or ():
        if str(_envelope_get(item, "type", "")) != "message":
            continue  # reasoning/tool items are never surfaced
        for part in _envelope_get(item, "content", None) or ():
            part_type = str(_envelope_get(part, "type", ""))
            if part_type == "output_text":
                visible_parts.append(str(_envelope_get(part, "text", "") or ""))
            elif part_type == "refusal":
                refusal_present = True
    output_text = "".join(visible_parts)
    output_text_bytes = len(output_text.encode("utf-8"))
    finish_reason: str | None
    if status == "completed":
        finish_reason = "stop"
    elif status == "incomplete" and incomplete_reason == "max_output_tokens":
        finish_reason = "length"
    elif status == "incomplete" and incomplete_reason == "content_filter":
        finish_reason = "content_filter"
    elif status == "incomplete":
        finish_reason = "incomplete"
    else:
        finish_reason = None
    provider_completion: dict[str, Any] = {
        "api": "responses",
        "finish_reason": finish_reason,
        "status": status,
        "incomplete_details_reason": incomplete_reason,
        "error_code": str(error_code) if error_code is not None else None,
        "error_message_bytes": (
            len(str(error_message).encode("utf-8")) if error_message is not None else 0
        ),
        "response_id": str(response_id) if response_id is not None else None,
        "refusal_present": refusal_present,
        "incomplete": bool(refusal_present or finish_reason in {"length", "content_filter", "incomplete"}),
        "provider_error": bool(error_code is not None or status == "failed"),
        "completion_token_details": {"reasoning_tokens": reasoning_t},
        "visible_output_present": bool(output_text),
        "output_text_bytes": output_text_bytes,
        "output_text_sha256": (
            hashlib.sha256(output_text.encode("utf-8")).hexdigest() if output_text else None
        ),
        "provider_call_elapsed_seconds": elapsed_seconds,
    }
    return {
        "provider_completion": provider_completion,
        "output_text": output_text,
        "tokens": {
            "input": input_t,
            "output": output_t,
            "cached_input": cached_t,
            "reasoning": reasoning_t,
            "total": total_t,
        },
    }


def _validate_station_schema(
    role: str, obj: Any, *, patch_output_contract: PatchOutputContract | None = None
) -> str | None:
    """Local defense-in-depth semantic validator.

    Returns `None` when `obj` satisfies the REDI station output contract for
    `role`.  Otherwise returns a short safe reason string suitable for the
    `STRUCTURED_OUTPUT_SCHEMA_FAILED` error message (no raw content, no
    secrets).  This validator mirrors the JSON-Schema dictionaries above and
    is intentionally small and dependency-free.

    Parity with the provider schema requires extra-properties rejection at
    every object-typed level AND item-type checking for string arrays.
    """
    if not isinstance(obj, dict):
        return "root_not_object"
    if role == "builder":
        if patch_output_contract is not None:
            if "patches" not in obj:
                return "missing_patches"
            root_extras = set(obj.keys()) - {"patches"}
            if root_extras:
                return f"root_extra_key_{sorted(root_extras)[0]}"
            raw_patches = obj["patches"]
            if not isinstance(raw_patches, list):
                return "patches_not_array"
            if len(raw_patches) != patch_output_contract.expected_edit_count:
                return f"patch_count_expected_{patch_output_contract.expected_edit_count}_got_{len(raw_patches)}"
            for idx, patch in enumerate(raw_patches):
                if not isinstance(patch, dict):
                    return f"patch[{idx}]_not_object"
                extras = set(patch.keys()) - {"rel_path", "action", "content"}
                if extras:
                    return f"patch[{idx}]_extra_key_{sorted(extras)[0]}"
                if "rel_path" not in patch or not isinstance(patch["rel_path"], str):
                    return f"patch[{idx}]_missing_rel_path"
                if "action" not in patch or not isinstance(patch["action"], str):
                    return f"patch[{idx}]_invalid_action"
                if "content" not in patch:
                    return f"patch[{idx}]_write_missing_content"
                if not isinstance(patch["content"], str):
                    return f"patch[{idx}]_write_content_not_string"
            return patch_output_contract.validate_patches(_extract_builder_patches(obj))
        # Required-keys first — matches provider JSON Schema 'required' precedence
        # over 'additionalProperties'.  This ensures deterministic error codes for
        # the documented 8 invalid-object classes (e.g., {} → missing_patches).
        if "patches" not in obj:
            return "missing_patches"
        # Root additionalProperties=false — allowed keys: {patches} only.
        root_allowed = {"patches"}
        root_extras = set(obj.keys()) - root_allowed
        if root_extras:
            extra = sorted(root_extras)[0]
            return f"root_extra_key_{extra}"
        raw_patches = obj["patches"]
        if not isinstance(raw_patches, list):
            return "patches_not_array"
        for idx, p in enumerate(raw_patches):
            if not isinstance(p, dict):
                return f"patch[{idx}]_not_object"
            if "rel_path" not in p or not isinstance(p["rel_path"], str):
                return f"patch[{idx}]_missing_rel_path"
            action = p.get("action")
            if action not in ("write", "delete"):
                return f"patch[{idx}]_invalid_action"
            if action == "write":
                # WRITE variant additionalProperties=false — keys: rel_path,action,content only.
                write_allowed = {"rel_path", "action", "content"}
                extras = set(p.keys()) - write_allowed
                if extras:
                    extra = sorted(extras)[0]
                    return f"patch[{idx}]_extra_key_{extra}"
                if "content" not in p:
                    return f"patch[{idx}]_write_missing_content"
                if not isinstance(p["content"], str):
                    return f"patch[{idx}]_write_content_not_string"
            elif action == "delete":
                content_present = "content" in p
                content_value = p.get("content", None)
                # Choose the matching delete variant and enforce its exact keys.
                if not content_present:
                    # DELETE-absent variant additionalProperties=false — keys: rel_path,action only.
                    delete_absent_allowed = {"rel_path", "action"}
                    extras = set(p.keys()) - delete_absent_allowed
                    if extras:
                        extra = sorted(extras)[0]
                        return f"patch[{idx}]_extra_key_{extra}"
                else:
                    # DELETE-null variant additionalProperties=false — keys: rel_path,action,content only.
                    delete_null_allowed = {"rel_path", "action", "content"}
                    extras = set(p.keys()) - delete_null_allowed
                    if extras:
                        extra = sorted(extras)[0]
                        return f"patch[{idx}]_extra_key_{extra}"
                    if content_value is not None:
                        return f"patch[{idx}]_delete_unexpected_content"
    elif role == "reviewer":
        # Required keys first, matching JSON Schema priority.
        if "verdict" not in obj:
            return "missing_verdict"
        verdict_raw = obj.get("verdict")
        if not isinstance(verdict_raw, str):
            return "invalid_verdict"
        # Exact case-sensitive enum match — NO .upper() forgiveness.
        if verdict_raw not in (ReviewerVerdict.PASS, ReviewerVerdict.FAIL, ReviewerVerdict.ESCALATE):
            return "invalid_verdict"
        if "reasons" not in obj:
            return "missing_reasons"
        if not isinstance(obj["reasons"], list):
            return "reasons_not_array"
        if "evidence_refs" not in obj:
            return "missing_evidence_refs"
        if not isinstance(obj["evidence_refs"], list):
            return "evidence_refs_not_array"
        # Root additionalProperties=false — allowed keys: verdict/reasons/evidence_refs only.
        # (Checked AFTER required-keys pass so unrelated-objects with missing required keys
        # still get the documented missing-verdict error codes.)
        reviewer_allowed = {"verdict", "reasons", "evidence_refs"}
        root_extras = set(obj.keys()) - reviewer_allowed
        if root_extras:
            extra = sorted(root_extras)[0]
            return f"root_extra_key_{extra}"
        # String-item-type enforcement for arrays.
        for i, item in enumerate(obj["reasons"]):
            if not isinstance(item, str):
                return f"reasons[{i}]_not_string"
        for i, item in enumerate(obj["evidence_refs"]):
            if not isinstance(item, str):
                return f"evidence_refs[{i}]_not_string"
    else:
        return f"unknown_role_{role}"
    return None


def _extract_builder_patches(obj: dict) -> tuple[PatchEdit, ...]:
    """Extract PatchEdits from a *validated* builder dict.

    Callers MUST have passed obj through _validate_station_schema first.
    """
    out = []
    for p in obj["patches"]:
        action = "write" if str(p["action"]) == "write" else "delete"
        out.append(PatchEdit(
            rel_path=str(p["rel_path"]),
            action=action,
            content=p.get("content") if action == "write" else None,
        ))
    return tuple(out)


def _observed_builder_patches(obj: Any) -> tuple[ParsedPatchObservation, ...]:
    """Capture every parsed patch shape before acceptance evaluation."""
    if not isinstance(obj, dict) or not isinstance(obj.get("patches"), list):
        return ()
    observations: list[ParsedPatchObservation] = []
    for index, raw_patch in enumerate(obj["patches"]):
        if isinstance(raw_patch, dict):
            action = raw_patch.get("action") if isinstance(raw_patch.get("action"), str) else None
            rel_path = raw_patch.get("rel_path") if isinstance(raw_patch.get("rel_path"), str) else None
            content = raw_patch.get("content") if isinstance(raw_patch.get("content"), str) else None
            observations.append(
                ParsedPatchObservation(
                    index=index,
                    action=action,
                    rel_path=rel_path,
                    content_field_present="content" in raw_patch,
                    content=content,
                )
            )
        else:
            observations.append(ParsedPatchObservation(index, None, None, False, None))
    return tuple(observations)


def parse_builder_output_object(
    obj: Any, *, patch_output_contract: PatchOutputContract | None = None
) -> dict[str, Any]:
    """Validate parsed builder JSON and expose only safe forensic facts.

    Observations retain transient content only so the caller can immediately
    retain its bytes.  The persistable ``output_shape`` has hashes/sizes but
    never raw provider output or reasoning text.
    """
    observations = _observed_builder_patches(obj)
    reason = _validate_station_schema(
        "builder", obj, patch_output_contract=patch_output_contract
    )
    validated = reason is None
    return {
        "patches": _extract_builder_patches(obj) if validated and isinstance(obj, dict) else (),
        "observed_patches": observations,
        "output_shape": {
            "schema_identity": {
                "name": BUILDER_OUTPUT_SCHEMA_NAME,
                "policy_id": patch_output_contract.policy_id if patch_output_contract else None,
                "policy_version": patch_output_contract.policy_version if patch_output_contract else None,
                "policy_hash": patch_output_contract.canonical_hash if patch_output_contract else None,
            },
            "structured_output_validated": validated,
            "parsed_patch_count": (
                len(observations)
                if isinstance(obj, dict) and isinstance(obj.get("patches"), list)
                else None
            ),
            "patches": [item.safe_shape() for item in observations],
        },
        "safe_error": None if validated else f"STRUCTURED_OUTPUT_SCHEMA_FAILURE reason={reason}",
    }


def _extract_reviewer_output(obj: dict) -> tuple[ReviewerVerdict, tuple[str, ...], tuple[str, ...]]:
    """Extract verdict/reasons/evidence_refs from a *validated* reviewer dict.

    Callers MUST have passed obj through _validate_station_schema first.
    Verbatim case preserved (already enforced case-sensitive by validator).
    """
    verdict = ReviewerVerdict(obj["verdict"])
    reasons = tuple(str(x) for x in obj["reasons"])
    evidence_refs = tuple(str(x) for x in obj["evidence_refs"])
    return verdict, reasons, evidence_refs


def interpret_structured_output(
    raw_content: str,
    *,
    role: str,
    patch_output_contract: PatchOutputContract | None = None,
) -> dict[str, Any]:
    """API-agnostic structured-output interpretation of visible model text.

    Both execution APIs (chat_completions and responses) converge here after
    their completion envelopes are normalized, so the parsed RediBuilderOutput /
    PatchEdit pipeline, safe shape evidence, and error taxonomy are identical.
    """

    patches: Sequence[PatchEdit] = ()
    observed_patches: Sequence[ParsedPatchObservation] = ()
    output_shape: Mapping[str, Any] = {}
    verdict: ReviewerVerdict | None = None
    reasons: Sequence[str] = ()
    evidence_refs: Sequence[str] = ()
    error_message: str | None = None
    parsed: Any = None
    parse_error_category: str | None = None
    try:
        parsed = json.loads(raw_content)
    except Exception as exc:
        parse_error_category = (
            "JSONDecodeError"
            if isinstance(exc, (ValueError, json.JSONDecodeError))
            else type(exc).__name__
        )
        parsed = None
    task_specific_builder = role == "builder" and patch_output_contract is not None
    if task_specific_builder:
        parsed_output = parse_builder_output_object(
            parsed, patch_output_contract=patch_output_contract
        )
        patches = parsed_output["patches"]
        observed_patches = parsed_output["observed_patches"]
        output_shape = parsed_output["output_shape"]
        if parse_error_category is not None:
            error_message = (
                f"STRUCTURED_OUTPUT_SCHEMA_FAILURE role={role} category={parse_error_category}"
            )
        else:
            error_message = parsed_output["safe_error"]
    # --- Stage 1: parse failure (invalid JSON OR non-dict root) ---
    if not task_specific_builder and raw_content and not isinstance(parsed, dict):
        raw_len = len(str(raw_content))
        category = parse_error_category or "NonDictRoot"
        error_message = (
            f"STRUCTURED_OUTPUT_PARSE_FAILED role={role} category={category} "
            f"raw_length_chars={raw_len}"
        )
    # --- Stage 2: valid JSON object → enforce REDI station schema locally. ---
    # This defense-in-depth runs even when json_schema enforcement was
    # enabled at the API level; both layers should agree.
    if not task_specific_builder and isinstance(parsed, dict) and error_message is None:
        schema_reason = _validate_station_schema(role, parsed)
        if schema_reason is not None:
            error_message = (
                f"STRUCTURED_OUTPUT_SCHEMA_FAILED role={role} reason={schema_reason}"
            )
        else:
            if role == "builder":
                patches = _extract_builder_patches(parsed)
            elif role == "reviewer":
                verdict, reasons, evidence_refs = _extract_reviewer_output(parsed)
    return {
        "patches": patches,
        "observed_patches": observed_patches,
        "output_shape": output_shape,
        "reviewer_verdict": verdict,
        "reviewer_reasons": reasons,
        "reviewer_evidence_refs": evidence_refs,
        "error_message": error_message,
    }


class LiveProviderBlockedError(RuntimeError):
    """Live provider unavailable: missing SDK, key, or configuration."""


@dataclass(frozen=True)
class OpenAIChatCapabilityProfile:
    """Bounded, model-family API compatibility for Chat Completions.

    A profile is deliberately about API mechanics only.  It contains no
    product, task, fixture, or holdout information, so a caller can select a
    compatible profile without creating experiment-specific request branches.
    """

    profile_id: str
    model_prefixes: tuple[str, ...]
    max_output_parameter: str
    supports_reasoning_effort: bool
    supported_reasoning_efforts: tuple[str, ...]
    supports_temperature: bool
    supports_top_p: bool
    usage_shape: str


OPENAI_CHAT_LEGACY_V1 = OpenAIChatCapabilityProfile(
    profile_id="openai_chat_legacy_v1",
    model_prefixes=(),
    max_output_parameter="max_tokens",
    supports_reasoning_effort=False,
    supported_reasoning_efforts=(),
    supports_temperature=True,
    supports_top_p=True,
    usage_shape="chat_completions_usage_v1",
)

OPENAI_CHAT_GPT56_V1 = OpenAIChatCapabilityProfile(
    profile_id="openai_chat_gpt56_v1",
    model_prefixes=("gpt-5.6",),
    max_output_parameter="max_completion_tokens",
    supports_reasoning_effort=True,
    supported_reasoning_efforts=("none", "low", "medium", "high", "xhigh", "max"),
    supports_temperature=False,
    supports_top_p=False,
    usage_shape="chat_completions_usage_v1",
)

_OPENAI_CHAT_CAPABILITY_PROFILES: Mapping[str, OpenAIChatCapabilityProfile] = {
    OPENAI_CHAT_LEGACY_V1.profile_id: OPENAI_CHAT_LEGACY_V1,
    OPENAI_CHAT_GPT56_V1.profile_id: OPENAI_CHAT_GPT56_V1,
}


@dataclass(frozen=True)
class OpenAIResponsesCapabilityProfile:
    """Bounded, model-family API compatibility for the OpenAI Responses API.

    Like the Chat Completions profiles, this is API mechanics only: no product,
    task, fixture, or holdout information.  Responses-native semantics are
    documented by the current official OpenAI references (see module notes in
    tests): ``max_output_tokens`` bounds reasoning AND visible output together,
    reasoning effort travels as ``reasoning={effort: ...}``, and structured
    outputs travel as ``text.format={type: json_schema, name, strict, schema}``.
    """

    profile_id: str
    model_prefixes: tuple[str, ...]
    max_output_parameter: str
    supports_reasoning_effort: bool
    supported_reasoning_efforts: tuple[str, ...]
    supports_temperature: bool
    supports_top_p: bool
    usage_shape: str
    supports_structured_outputs: bool


OPENAI_RESPONSES_GPT56_V1 = OpenAIResponsesCapabilityProfile(
    profile_id="openai_responses_gpt56_v1",
    model_prefixes=("gpt-5.6",),
    max_output_parameter="max_output_tokens",
    supports_reasoning_effort=True,
    supported_reasoning_efforts=("none", "minimal", "low", "medium", "high", "xhigh", "max"),
    supports_temperature=False,
    supports_top_p=False,
    usage_shape="responses_usage_v1",
    supports_structured_outputs=True,
)

_OPENAI_RESPONSES_CAPABILITY_PROFILES: Mapping[str, OpenAIResponsesCapabilityProfile] = {
    OPENAI_RESPONSES_GPT56_V1.profile_id: OPENAI_RESPONSES_GPT56_V1,
}


@dataclass
class LiveProviderConfig:
    env_key_name: str = "REDI_OPENAI_API_KEY"
    model: str = "gpt-4o-mini"
    timeout_s: float = 60.0
    max_output_tokens: int = 2048
    temperature: float | None = 0.0
    top_p: float | None = None
    reasoning_effort: str | None = None
    api_capability_profile: str = OPENAI_CHAT_LEGACY_V1.profile_id
    provider_name: str = "openai"
    api: str = "chat_completions"

    def capability_profile(self) -> OpenAIChatCapabilityProfile | OpenAIResponsesCapabilityProfile:
        if self.api == "chat_completions":
            profile = _OPENAI_CHAT_CAPABILITY_PROFILES.get(self.api_capability_profile)
        elif self.api == "responses":
            profile = _OPENAI_RESPONSES_CAPABILITY_PROFILES.get(self.api_capability_profile)
        else:
            raise LiveProviderBlockedError(f"unknown OpenAI execution API {self.api!r}")
        if profile is None:
            raise LiveProviderBlockedError(
                f"unknown OpenAI {self.api} capability profile {self.api_capability_profile!r}"
            )
        if profile.model_prefixes and not any(
            self.model.startswith(prefix) for prefix in profile.model_prefixes
        ):
            raise LiveProviderBlockedError(
                f"capability profile {profile.profile_id!r} is not compatible with model {self.model!r}"
            )
        return profile

    def request_configuration_evidence(self) -> dict[str, Any]:
        """Validate and return the exact non-secret request configuration.

        This is local-only architecture validation.  It does not instantiate a
        client or make a provider request, and the returned record intentionally
        excludes the API key and request content.
        """

        profile = self.capability_profile()
        if self.max_output_tokens <= 0:
            raise LiveProviderBlockedError("max_output_tokens must be positive")
        if self.reasoning_effort is not None:
            if not profile.supports_reasoning_effort:
                raise LiveProviderBlockedError(
                    f"{profile.profile_id} does not support reasoning_effort"
                )
            if self.reasoning_effort not in profile.supported_reasoning_efforts:
                raise LiveProviderBlockedError(
                    f"unsupported reasoning_effort {self.reasoning_effort!r} for {profile.profile_id}"
                )
        if not profile.supports_temperature and self.temperature is not None:
            raise LiveProviderBlockedError(
                f"{profile.profile_id} does not support temperature"
            )
        if not profile.supports_top_p and self.top_p is not None:
            raise LiveProviderBlockedError(f"{profile.profile_id} does not support top_p")
        return {
            "api": self.api,
            "capability_profile": profile.profile_id,
            "model": self.model,
            "response_format": "json_schema_strict",
            "max_output_parameter": profile.max_output_parameter,
            "max_output_tokens": self.max_output_tokens,
            "reasoning_effort": self.reasoning_effort,
            "temperature": self.temperature if profile.supports_temperature else None,
            "top_p": self.top_p if profile.supports_top_p else None,
            "usage_shape": profile.usage_shape,
            "provider_access_status": "provider_access_not_yet_proven",
        }


def _scrub_for_ledger(value: str) -> str:
    """Return value with obvious secret patterns masked (belt + suspenders)."""
    if not isinstance(value, str):
        return ""
    return SECRET_BLOCKLIST_RE.sub("[REDACTED_BY_POLICY]", value)


def _sdk_available() -> bool:  # pragma: no cover - depends on environment
    try:
        import openai  # type: ignore[import-not-found]  # noqa: F401
        return True
    except Exception:
        return False


@dataclass
class LiveProviderAdapter(WorkerRunner):
    config: LiveProviderConfig = field(default_factory=LiveProviderConfig)
    # Acceptance-only hooks. They remain inside the provider adapter boundary.
    before_call: Callable[[], None] | None = None
    call_observer: Callable[[str, int], None] | None = None

    def _require(self) -> tuple[Any, str]:
        if not _sdk_available():
            raise LiveProviderBlockedError("openai SDK not installed; run `pip install openai` for live runs")
        key = os.environ.get(self.config.env_key_name, "")
        if not key:
            raise LiveProviderBlockedError(
                f"environment variable {self.config.env_key_name!r} not set; live run skipped."
            )
        import openai  # type: ignore[import-not-found]
        return openai, key

    # ------------------------------------------------------------------
    def run_node(
        self,
        *,
        task_id: TaskId,
        station_id: StationId,
        worker_id: WorkerId,
        context: dict[str, Any],
        workspace_root: Any,
        station_role: str = "builder",
    ) -> NodeRunResult:
        openai_mod, _ = self._require()  # raises LiveProviderBlockedError if blocked
        start = time.perf_counter()
        role = context.get("role") or station_role
        instructions = context.get("instructions") or context.get("prompt") or ""
        extra: str = context.get("context_body") or ""
        patch_output_contract = context.get("patch_output_contract")
        if patch_output_contract is not None and not isinstance(patch_output_contract, PatchOutputContract):
            raise LiveProviderBlockedError("patch_output_contract must be an immutable PatchOutputContract")
        rendered_request = render_live_request(
            role=str(role),
            task_id=str(task_id),
            instructions=str(instructions),
            context_body=str(extra),
            patch_output_contract=patch_output_contract,
        )
        client = openai_mod.OpenAI(
            api_key=os.environ[self.config.env_key_name],
            timeout=self.config.timeout_s,
        )
        request_configuration = self.config.request_configuration_evidence()
        if self.config.api == "responses":
            responses_view = render_responses_request(
                role=str(role),
                task_id=str(task_id),
                instructions=str(instructions),
                context_body=str(extra),
                patch_output_contract=patch_output_contract,
                model=self.config.model,
                reasoning_effort=self.config.reasoning_effort,
                max_output_tokens=self.config.max_output_tokens,
            )
            return self._run_responses_node(
                client=client,
                responses_view=responses_view,
                request_configuration=request_configuration,
                role=str(role),
                task_id=task_id,
                station_id=station_id,
                worker_id=worker_id,
                patch_output_contract=patch_output_contract,
                start=start,
            )
        create_kwargs: dict[str, Any] = {
            "model": self.config.model,
            request_configuration["max_output_parameter"]: self.config.max_output_tokens,
            "messages": [
                {"role": "system", "content": rendered_request["system_prompt"]},
                {"role": "user", "content": rendered_request["user_content"]},
            ],
        }
        if request_configuration["temperature"] is not None:
            create_kwargs["temperature"] = request_configuration["temperature"]
        if request_configuration["top_p"] is not None:
            create_kwargs["top_p"] = request_configuration["top_p"]
        if request_configuration["reasoning_effort"] is not None:
            create_kwargs["reasoning_effort"] = request_configuration["reasoning_effort"]
        # TRUE structured outputs ONLY (fail-closed, REDI-03L2C policy).
        # We never weaken from json_schema -> json_object on TypeError because
        # experimental evidence must have exactly ONE known output contract
        # strength.  If the SDK rejects strict structured outputs the adapter
        # raises an explicit capability error; the caller can audit this.
        preferred = rendered_request["response_format"]
        create_kwargs["response_format"] = preferred
        if self.before_call is not None:
            self.before_call()
        if self.call_observer is not None:
            self.call_observer("provider_call_started", time.perf_counter_ns())
        try:
            resp = client.chat.completions.create(**create_kwargs)
        except TypeError as exc:
            raise LiveProviderBlockedError(
                "strict_response_format_json_schema_unsupported: "
                "installed openai SDK rejected response_format={type:json_schema} "
                f"for role={role!r}; REDI-03L2C fail-closed policy prohibits a "
                "weaker json_object fallback; upgrade openai SDK to >= 2.18 or 3.x "
                f"or use a provider/mode that supports json_schema. Detail: {exc}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 — surface cleanly
            raise LiveProviderBlockedError(f"provider call failed: {exc}") from exc
        finally:
            if self.call_observer is not None:
                self.call_observer("provider_call_finished", time.perf_counter_ns())
        elapsed = time.perf_counter() - start
        choice = resp.choices[0] if getattr(resp, "choices", None) else None
        message = getattr(choice, "message", None)
        # Raw content is parsed locally but never logged or persisted.  A
        # lexical scrub here would alter generated code before validation.
        raw_content = getattr(message, "content", "") or ""
        usage = getattr(resp, "usage", None)
        prompt_t = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_t = int(getattr(usage, "completion_tokens", 0) or 0)
        reasoning_t = int(
            getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", 0)
            or 0
        )
        model_ref = _scrub_for_ledger(getattr(resp, "model", ""))
        opaque_ref = f"{self.config.provider_name}:{model_ref}"
        tokens = TokenUsage(
            input=prompt_t,
            output=completion_t,
            cached_input=int(getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0),
            reasoning=reasoning_t,
            other={"total": int(getattr(usage, "total_tokens", 0) or (prompt_t + completion_t))},
        )
        completion_details = getattr(usage, "completion_tokens_details", None)
        safe_completion_details = {
            name: int(getattr(completion_details, name, 0) or 0)
            for name in ("reasoning_tokens", "accepted_prediction_tokens", "rejected_prediction_tokens", "audio_tokens")
            if getattr(completion_details, name, None) is not None
        }
        finish_reason = getattr(choice, "finish_reason", None)
        finish_reason = str(finish_reason) if isinstance(finish_reason, str) else None
        refusal_present = bool(getattr(message, "refusal", None))
        provider_completion = {
            "finish_reason": finish_reason,
            "refusal_present": refusal_present,
            "incomplete": refusal_present or finish_reason in {"length", "content_filter", "incomplete"},
            "completion_token_details": safe_completion_details,
            "provider_call_elapsed_seconds": elapsed,
        }
        interpreted = interpret_structured_output(
            raw_content, role=str(role), patch_output_contract=patch_output_contract
        )
        return NodeRunResult(
            worker_id=worker_id,
            task_id=task_id,
            station_id=station_id,
            token_usage=tokens,
            patches=interpreted["patches"],
            observed_patches=interpreted["observed_patches"],
            output_shape=interpreted["output_shape"],
            provider_completion=provider_completion,
            reviewer_verdict=interpreted["reviewer_verdict"],
            reviewer_reasons=interpreted["reviewer_reasons"],
            reviewer_evidence_refs=interpreted["reviewer_evidence_refs"],
            opaque_provider_ref=opaque_ref,
            elapsed_seconds=elapsed,
            error_message=interpreted["error_message"],
        )

    # ------------------------------------------------------------------
    def _run_responses_node(
        self,
        *,
        client: Any,
        responses_view: Mapping[str, Any],
        request_configuration: Mapping[str, Any],
        role: str,
        task_id: TaskId,
        station_id: StationId,
        worker_id: WorkerId,
        patch_output_contract: PatchOutputContract | None,
        start: float,
    ) -> NodeRunResult:
        """One stateless Responses API implementation call (no tools/state)."""

        create_kwargs = dict(responses_view["create_kwargs"])
        if self.before_call is not None:
            self.before_call()
        if self.call_observer is not None:
            self.call_observer("provider_call_started", time.perf_counter_ns())
        try:
            resp = client.responses.create(**create_kwargs)
        except TypeError as exc:
            raise LiveProviderBlockedError(
                "strict_text_format_json_schema_unsupported: "
                "installed openai SDK rejected text.format={type:json_schema} "
                f"for role={role!r}; the fail-closed policy prohibits a weaker "
                f"fallback. Detail: {exc}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 — surface cleanly
            raise LiveProviderBlockedError(f"provider call failed: {exc}") from exc
        finally:
            if self.call_observer is not None:
                self.call_observer("provider_call_finished", time.perf_counter_ns())
        elapsed = time.perf_counter() - start
        normalized = normalize_responses_result(resp, elapsed_seconds=elapsed)
        tokens = TokenUsage(
            input=normalized["tokens"]["input"],
            output=normalized["tokens"]["output"],
            cached_input=normalized["tokens"]["cached_input"],
            reasoning=normalized["tokens"]["reasoning"],
            other={"total": normalized["tokens"]["total"]},
        )
        model_ref = _scrub_for_ledger(str(getattr(resp, "model", "") or self.config.model))
        interpreted = interpret_structured_output(
            normalized["output_text"], role=role, patch_output_contract=patch_output_contract
        )
        return NodeRunResult(
            worker_id=worker_id,
            task_id=task_id,
            station_id=station_id,
            token_usage=tokens,
            patches=interpreted["patches"],
            observed_patches=interpreted["observed_patches"],
            output_shape=interpreted["output_shape"],
            provider_completion=normalized["provider_completion"],
            reviewer_verdict=interpreted["reviewer_verdict"],
            reviewer_reasons=interpreted["reviewer_reasons"],
            reviewer_evidence_refs=interpreted["reviewer_evidence_refs"],
            opaque_provider_ref=f"{self.config.provider_name}:{model_ref}",
            elapsed_seconds=elapsed,
            error_message=interpreted["error_message"],
        )


__all__ = [
    "BUILDER_OUTPUT_JSON_SCHEMA",
    "LIVE_STATION_SYSTEM_PROMPT",
    "LiveProviderAdapter",
    "LiveProviderBlockedError",
    "LiveProviderConfig",
    "OPENAI_CHAT_GPT56_V1",
    "OPENAI_CHAT_LEGACY_V1",
    "OPENAI_RESPONSES_GPT56_V1",
    "OpenAIChatCapabilityProfile",
    "OpenAIResponsesCapabilityProfile",
    "interpret_structured_output",
    "normalize_responses_result",
    "parse_builder_output_object",
    "render_live_request",
    "render_responses_request",
]
