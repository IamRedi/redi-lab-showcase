"""Builder/Reviewer context builders.

Deterministic: given identical inputs produce identical ContextManifest / review
context dicts byte-for-byte (sorted, JSON-stable) with explicit exclusion
annotations for hidden tests and secret paths.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from ..domain import (
    AcceptanceCriterion,
    ArtifactRef,
    ContextManifest,
    ContextManifestId,
    ProductSpec,
    StationContract,
    TaskContract,
    new_id,
)
from ..gates import PUBLIC_DIAGNOSTIC_MAX_CHARS
from ..context_compiler import DEFAULT_BUDGET_BYTES
from ..runtime.contracts import PatchEdit


HIDDEN_TEST_EXCLUSION_CODE = "HIDDEN_TEST_BOUNDARY"
SECRET_BLOCKLIST_EXCLUSION_CODE = "SECRET_BLOCKLIST_PATH"

_HIDDEN_PATH_RE: re.Pattern[str] = re.compile(
    r"(^|[/\\])(_hidden|hidden[_-]?tests?|test[_-]?hidden|\\.hidden)",
    re.IGNORECASE,
)

_SECRET_NAME_RE: re.Pattern[str] = re.compile(
    r"(HOME|USER(?:NAME)?|KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|APIKEY|\\.env)",
    re.IGNORECASE,
)

_FORBIDDEN_PATH_SUBSTR = (".git", ".venv", "__pycache__", ".trae", ".pytest_cache", ".egg-info")


def _sha_of_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _artifact_ref(rel_posix: str, content: bytes, *, source: str = "context") -> ArtifactRef:
    digest = hashlib.sha256(content).hexdigest()
    return ArtifactRef(
        ref_id=rel_posix,
        kind="file",
        location=rel_posix,
        sha256=digest,
        source=source,
        metadata={"size_bytes": len(content)},
    )


def _iter_sources(fixture_root: Path, patterns: Sequence[str]) -> list[tuple[str, bytes]]:
    root = Path(fixture_root).resolve()
    pats = [p.lstrip("./") for p in patterns or ["**/*.py", "**/*.txt", "**/*.toml", "**/*.md", "**/*.ini"]]
    seen: set[str] = set()
    out: list[tuple[str, bytes]] = []
    for pat in pats:
        for p in sorted(root.glob(pat)):
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            if rel in seen:
                continue
            seen.add(rel)
            out.append((rel, p.read_bytes()))
    return sorted(out, key=lambda t: t[0])


def _is_hidden(rel: str) -> bool:
    return bool(_HIDDEN_PATH_RE.search(rel.replace("\\", "/")))


def _is_secretish(rel: str) -> bool:
    return bool(_SECRET_NAME_RE.search(rel.replace("\\", "/")))


def _is_excluded_by_default(rel: str) -> bool:
    low = rel.lower()
    return any(s in low for s in _FORBIDDEN_PATH_SUBSTR)


def _ac_text(ac: Any) -> str:
    if isinstance(ac, AcceptanceCriterion):
        return ac.description
    t = getattr(ac, "text", None)
    if isinstance(t, str) and t:
        return t
    d = getattr(ac, "description", None)
    if isinstance(d, str) and d:
        return d
    return str(ac)


def build_builder_context(
    *,
    task: TaskContract,
    station: StationContract,
    product: ProductSpec,
    fixture_root: Path,
    include_globs: Sequence[str] = (),
    public_test_globs: Sequence[str] = (),
    additional_sources: Sequence[Path] = (),
    allowed_scope: Sequence[str] = (),
    retry_feedback_category: str | None = None,
    context_strategy_id: str = "selected_v0",
) -> ContextManifest:
    sources: list[ArtifactRef] = []
    exclusions: list[ArtifactRef] = []
    root = Path(fixture_root).resolve()
    default_sources = _iter_sources(fixture_root, include_globs)
    public_sources = _iter_sources(fixture_root, public_test_globs)
    combined: dict[str, bytes] = {}
    for rel, content in default_sources + public_sources:
        combined.setdefault(rel, content)
    for extra in additional_sources:
        p = Path(extra).resolve()
        if p.is_file():
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                rel = "__extra__/" + PurePosixPath(p.name).as_posix()
            combined.setdefault(rel, p.read_bytes())
    for rel in sorted(combined):
        content = combined[rel]
        reason: str | None = None
        if _is_hidden(rel):
            reason = HIDDEN_TEST_EXCLUSION_CODE
        elif _is_secretish(rel):
            reason = SECRET_BLOCKLIST_EXCLUSION_CODE
        elif _is_excluded_by_default(rel):
            reason = f"DEFAULT_EXCLUDED_DIR:{rel.split('/')[0]}"
        ref = _artifact_ref(rel, content)
        if reason is None:
            sources.append(ref)
        else:
            ref2 = ArtifactRef(
                ref_id=ref.ref_id,
                kind="other",
                location=ref.location,
                sha256=None,
                source="context",
                metadata={"exclusion_reason": reason, "size_bytes": 0},
            )
            exclusions.append(ref2)
    ctx = ContextManifest(
        id=ContextManifestId(new_id("context_manifest")),
        task_id=task.id,
        station_id=station.id,
        product_id=product.id,
        source_references=tuple(sources),
        explicit_exclusions=tuple(exclusions),
        metadata={
            "builder_context": True,
            "format_version": 1,
            "station_role": getattr(station, "role", ""),
            "acceptance_criteria_count": len(task.acceptance_criteria) + len(product.acceptance_criteria),
            # REDI-03L6 evidence bindings (smallest compatible addition):
            # the scope policy and retry-feedback category that governed THIS
            # context build, bound into the deterministic hash below.
            "allowed_scope": list(allowed_scope),
            "retry_feedback_category": retry_feedback_category,
            # REDI-05A: context strategy identity, bound into the hash.
            "context_strategy_id": context_strategy_id,
        },
    )
    acceptance_texts: list[str] = []
    for ac in list(task.acceptance_criteria) + list(product.acceptance_criteria):
        acceptance_texts.append(_ac_text(ac))
    payload = {
        "task_id": str(task.id),
        "product_id": str(product.id),
        "station_id": str(station.id),
        "sources": sorted(s.ref_id + ":" + (s.sha256 or "") for s in sources),
        "exclusions": sorted(
            x.ref_id + "!" + str(x.metadata.get("exclusion_reason", "") if isinstance(x.metadata, dict) else "")
            for x in exclusions
        ),
        "acceptance": sorted(acceptance_texts),
        "allowed_scope": sorted(str(s) for s in allowed_scope),
        "retry_feedback_category": retry_feedback_category or "",
        "context_strategy_id": str(context_strategy_id),
    }
    ctx.deterministic_hash = _sha_of_text(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    return ctx


# ---------------------------------------------------------------------------
# REDI-03L6 — minimum bounded live-station prompt rendering.
# Factory-layer (Layer C) responsibility: turn canonical task/spec state into
# the exact text sections LiveProviderAdapter renders.  Deterministic, bounded
# to the public fixture files already referenced by the ContextManifest; no
# hidden tests, no secrets, no absolute home paths, no repo-wide reads.
# ---------------------------------------------------------------------------

MAX_RETRY_REASON_CHARS = 300
MAX_RETRY_REASONS = 8
MAX_RETRY_OFFENDING_PATH_CHARS = 200
MAX_RETRY_OFFENDING_PATHS = 10


def _scrub_text(text: str) -> str:
    scrubbed = _SECRET_NAME_RE.sub("[REDACTED]", text)
    return _HIDDEN_PATH_RE.sub("[REDACTED_HIDDEN]", scrubbed)


def _mentions_hidden_token(text: str) -> bool:
    """Conservative hidden-test-content detector for bounded feedback.

    Broader than :func:`_is_hidden`: any hidden-test-ish token anywhere in the
    string (``hidden_tests``, ``_hidden``, ``test_hidden``, …) qualifies, so
    retry feedback can never echo hidden paths or hidden-gate output details.
    """
    return bool(
        re.search(
            r"hidden[_-]?tests?|test[_-]?hidden|(^|[^a-z])_hidden|/hidden|\\+hidden",
            text,
            re.IGNORECASE,
        )
    )


def bound_retry_feedback(summary: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return a bounded, scrubbed, deterministic previous-failure summary.

    Keeps only: attempt, category, boundary/gate identity, offending paths and
    deterministic failure reasons — capped in count and length, hidden-path
    mentions filtered, secret-ish patterns redacted.  Never includes hidden
    test source, model reasoning, or raw logs.
    """
    if not summary:
        return None
    out: dict[str, Any] = {}
    for key in ("attempt", "category", "boundary", "gate"):
        if summary.get(key) is not None:
            out[key] = summary[key]
    paths = sorted({
        str(p)[:MAX_RETRY_OFFENDING_PATH_CHARS]
        for p in (summary.get("offending_paths") or [])
        if not _mentions_hidden_token(str(p))
    })
    if paths:
        out["offending_paths"] = paths[:MAX_RETRY_OFFENDING_PATHS]
    reasons = [
        str(r)[:MAX_RETRY_REASON_CHARS]
        for r in (summary.get("reasons") or [])
        if not _mentions_hidden_token(str(r))
    ][:MAX_RETRY_REASONS]
    if reasons:
        out["reasons"] = reasons
    # REDI-03L9: sanitized public diagnostic excerpt (already bounded/sanitized
    # by the gates module; re-capped here as a deterministic belt).
    diagnostic = summary.get("diagnostic")
    if diagnostic:
        out["diagnostic"] = str(diagnostic)[:PUBLIC_DIAGNOSTIC_MAX_CHARS]
    raw = json.dumps(out, sort_keys=True, ensure_ascii=False)
    return json.loads(_scrub_text(raw))


def render_builder_prompt(
    *,
    product: ProductSpec,
    task: TaskContract,
    station_role: str,
    workspace_root: Path,
    ctx_manifest: ContextManifest,
    retry_feedback: Mapping[str, Any] | None = None,
    station: StationContract | None = None,
    budget_bytes: int = DEFAULT_BUDGET_BYTES,
) -> dict[str, str]:
    """Render the exact Builder user-prompt sections FROM the REDI-05
    compiled context package (single canonical context path).

    The manifest's selected sources, scope policy, and retry-feedback
    category drive compilation; the firewall and byte budget are enforced
    here, before any worker is invoked.  Attempt 0 (``retry_feedback=None``)
    has no feedback section.
    """
    from ..context_compiler import compile_context

    effective_station = station or StationContract(
        id=ctx_manifest.station_id, role=station_role, required_output_kinds=[],
    )
    feedback_items = []
    if retry_feedback:
        item = dict(retry_feedback)
        item["context_sanitized"] = True  # produced by the L9 sanitizer
        feedback_items.append(item)
    package = compile_context(
        strategy_id=str(ctx_manifest.metadata.get("context_strategy_id", "selected_v0")),
        station_role="builder",
        task=task,
        station=effective_station,
        product=product,
        workspace_root=workspace_root,
        selected_paths=[r.ref_id for r in ctx_manifest.source_references],
        allowed_scope=list(ctx_manifest.metadata.get("allowed_scope") or []),
        prior_failure_evidence=feedback_items,
        budget_bytes=budget_bytes,
    )
    return {
        "instructions": package.instructions,
        "context_body": package.context_body,
        "sha256": package.rendered_hash,
        "package": package,
    }


def build_reviewer_context(
    *,
    product: ProductSpec,
    task: TaskContract,
    station: StationContract,
    workspace_root_before: Path,
    patches_applied: Sequence[PatchEdit],
    deterministic_gate_results: list[Any],
    allowed_scope_prefixes: Sequence[str],
) -> dict[str, Any]:
    before_root = Path(workspace_root_before).resolve()
    before_texts: dict[str, str] = {}
    patch_by_rel: dict[str, PatchEdit] = {p.rel_path: p for p in patches_applied}
    patch_rels = sorted({p.rel_path for p in patches_applied})
    for rel in patch_rels:
        abs_p = before_root / PurePosixPath(rel)
        if abs_p.is_file():
            try:
                before_texts[rel] = abs_p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                before_texts[rel] = ""
    after_texts = dict(before_texts)
    for rel in list(before_texts):
        patch = patch_by_rel.get(rel)
        if patch and patch.action == "write":
            after_texts[rel] = patch.content or ""
        elif patch and patch.action == "delete":
            after_texts[rel] = ""
    for p in patches_applied:
        if p.action == "write" and p.rel_path not in before_texts:
            before_texts[p.rel_path] = ""
            after_texts[p.rel_path] = p.content or ""
    diff_chunks: list[str] = []
    for rel in sorted(set(list(before_texts) + list(after_texts))):
        a = before_texts.get(rel, "").splitlines(keepends=True)
        b = after_texts.get(rel, "").splitlines(keepends=True)
        diff_chunks.append("--- a/" + rel + "\n+++ b/" + rel + "\n")
        diff_chunks.extend(difflib.unified_diff(a, b, fromfile="a/" + rel, tofile="b/" + rel, n=5))
    diff_text = "".join(diff_chunks).rstrip() + "\n"
    gate_summaries: list[dict[str, Any]] = []
    for g in deterministic_gate_results:
        gid = str(getattr(g, "gate_identity", str(g)))
        status = str(getattr(g, "status", "unknown"))
        reasons = list(getattr(g, "failure_reasons", []) or [])
        reasons_clean = [r for r in reasons if not _is_hidden(str(r))]
        gate_summaries.append({
            "gate_identity": gid,
            "gate_type": str(getattr(g, "gate_type", "")),
            "status": status,
            "verifier": str(getattr(g, "verifier_type", "") or ""),
            "failure_reasons_count": len(reasons_clean),
        })
    acceptance_p: list[str] = [_ac_text(ac) for ac in product.acceptance_criteria]
    constraints_p: list[str] = [getattr(c, "text", str(c)) for c in product.constraints]
    requirements_p: list[str] = [getattr(r, "text", str(r)) for r in (getattr(product, "requirements", None) or [])]
    acceptance_t: list[str] = [_ac_text(ac) for ac in task.acceptance_criteria]
    allowed_scope = list(task.allowed_scope) if task.allowed_scope else list(allowed_scope_prefixes)
    ctx: dict[str, Any] = {
        "format_version": 1,
        "role": "REDI Reviewer station contract execution",
        "product": {
            "requested_outcome": getattr(product, "requested_outcome", ""),
            "acceptance_criteria": acceptance_p,
            "requirements": requirements_p,
            "constraints": constraints_p,
        },
        "task": {
            "goal": getattr(task, "goal", ""),
            "allowed_scope": allowed_scope,
            "acceptance_criteria": acceptance_t,
        },
        "station_role": getattr(station, "role", ""),
        "changed_files": sorted(p.rel_path for p in patches_applied),
        "diff": diff_text,
        "deterministic_gate_summaries": gate_summaries,
    }
    raw = json.dumps(ctx, sort_keys=True, ensure_ascii=False)
    scrubbed = _SECRET_NAME_RE.sub("[REDACTED]", raw)
    scrubbed = _HIDDEN_PATH_RE.sub("[REDACTED_HIDDEN]", scrubbed)
    return json.loads(scrubbed)


def render_reviewer_prompt(*, reviewer_context: Mapping[str, Any], budget_bytes: int = DEFAULT_BUDGET_BYTES) -> dict[str, str]:
    """Render the Reviewer user-prompt sections FROM the REDI-05 compiled
    context package built over the (already scrubbed) reviewer material."""
    from ..context_compiler import compile_context
    from ..domain import ProductSpec, Requirement, RiskTier, TaskContract, StationContract

    product_info = reviewer_context.get("product") or {}
    task_info = reviewer_context.get("task") or {}
    product = ProductSpec(
        requested_outcome=str(product_info.get("requested_outcome") or ""),
        requirements=[Requirement(text=t, kind="functional") for t in (product_info.get("requirements") or [])],
        constraints=[str(c) for c in (product_info.get("constraints") or [])],
        risk_tier=RiskTier.LOW,
        unresolved_requirements=[],
    )
    task = TaskContract(goal=str(task_info.get("goal") or ""))
    station = StationContract(role=str(reviewer_context.get("station_role") or "reviewer"),
                              required_output_kinds=["verdict"])
    package = compile_context(
        strategy_id="selected_v0",
        station_role="reviewer",
        task=task,
        station=station,
        product=product,
        workspace_root=".",
        selected_paths=(),  # reviewer context contains no workspace files
        changed_files=list(reviewer_context.get("changed_files") or ()),
        diff_text=str(reviewer_context.get("diff") or ""),
        gate_summaries=list(reviewer_context.get("deterministic_gate_summaries") or ()),
        allowed_scope=list(task_info.get("allowed_scope") or []),
        budget_bytes=budget_bytes,
    )
    return {
        "instructions": package.instructions,
        "context_body": package.context_body,
        "sha256": package.rendered_hash,
        "package": package,
    }


__all__ = [
    "HIDDEN_TEST_EXCLUSION_CODE",
    "SECRET_BLOCKLIST_EXCLUSION_CODE",
    "bound_retry_feedback",
    "build_builder_context",
    "build_reviewer_context",
    "render_builder_prompt",
    "render_reviewer_prompt",
]
