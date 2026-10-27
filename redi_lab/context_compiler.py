"""REDI-05A — Context Firewall / Compiler foundation.

Promotes context from ad-hoc wiring into a first-class, provider-neutral,
reproducible experimental input.  FOUNDATION ONLY: no controller rewiring,
no full-context vs selected-context experiment, no learned selection.

Core rules
----------
* Allowlist-first firewall: ONLY explicitly allowed context classes may
  enter a compiled package; everything else is excluded unless policy
  explicitly includes it; forbidden inputs (hidden tests, secrets, absolute
  home paths, unrelated repositories, other-station private history, raw
  provider objects, builder private reasoning) fail closed.
* Deterministic strategies: ``selected_v0`` (explicitly selected files
  only) and ``full_public_v0`` (all eligible PUBLIC files inside the
  bounded workspace root).  Same inputs ⇒ same package ⇒ same hashes.
* Deterministic budgeting: byte/char counts and a declared token
  ESTIMATE (chars ÷ 4, no tokenizer dependency).  Budget overflow fails
  closed — never silent truncation, never arbitrary file dropping.
* Audit vs context boundary: gate evidence is always retained in the
  ledger for audit, but hidden-gate evidence is NEVER eligible as station
  context; prior-failure evidence enters only when the
  ``prior_failure_evidence`` class is explicitly allowed AND the supplied
  evidence items pass the same firewall checks.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from .domain import HandoffManifest, ProductSpec, StationContract, TaskContract

# --- deterministic strategies -------------------------------------------------

CONTEXT_STRATEGY_SELECTED_V0 = "selected_v0"
CONTEXT_STRATEGY_FULL_PUBLIC_V0 = "full_public_v0"
CONTEXT_STRATEGY_IDS = (CONTEXT_STRATEGY_SELECTED_V0, CONTEXT_STRATEGY_FULL_PUBLIC_V0)
CONTEXT_STRATEGY_VERSION = 1

# --- context classes ----------------------------------------------------------

CONTEXT_CLASS_TASK_CONTRACT = "task_contract"
CONTEXT_CLASS_STATION_CONTRACT = "station_contract"
CONTEXT_CLASS_PRODUCT_CONTRACT = "product_contract"
CONTEXT_CLASS_SOURCE_FILES = "source_files"
CONTEXT_CLASS_PUBLIC_VERIFICATION = "public_verification"
CONTEXT_CLASS_UPSTREAM_HANDOFFS = "upstream_handoffs"
CONTEXT_CLASS_PRIOR_FAILURE_EVIDENCE = "prior_failure_evidence"

CONTEXT_CLASS_ALL = (
    CONTEXT_CLASS_TASK_CONTRACT,
    CONTEXT_CLASS_STATION_CONTRACT,
    CONTEXT_CLASS_PRODUCT_CONTRACT,
    CONTEXT_CLASS_SOURCE_FILES,
    CONTEXT_CLASS_PUBLIC_VERIFICATION,
    CONTEXT_CLASS_UPSTREAM_HANDOFFS,
    CONTEXT_CLASS_PRIOR_FAILURE_EVIDENCE,
)

#: Classes allowed by default (allowlist-first: anything not listed here and
#: not explicitly enabled on the firewall is excluded).
DEFAULT_ALLOWED_CLASSES = frozenset({
    CONTEXT_CLASS_TASK_CONTRACT,
    CONTEXT_CLASS_STATION_CONTRACT,
    CONTEXT_CLASS_PRODUCT_CONTRACT,
    CONTEXT_CLASS_SOURCE_FILES,
    CONTEXT_CLASS_PUBLIC_VERIFICATION,
})

#: Gate evidence retained for audit; eligible for station context ONLY if its
#: gate identity is listed here.  Hidden-gate evidence is audit-only, always.
CONTEXT_ELIGIBLE_GATE_IDENTITIES = frozenset({"public_test_execution"})

DEFAULT_BUDGET_BYTES = 64 * 1024
TOKEN_ESTIMATE_METHOD = "CHARS_PER_4"

_FORBIDDEN_DIR_MARKERS = (".git", ".venv", "__pycache__", ".trae", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".egg-info")
_HIDDEN_RE = re.compile(r"(^|[/\\])(_hidden|hidden[_-]?tests?|test[_-]?hidden|\\.hidden)", re.IGNORECASE)
_SECRET_NAME_RE = re.compile(r"(HOME|USER(?:NAME)?|KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|APIKEY|\.env)", re.IGNORECASE)
_SECRET_VALUE_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|(?i:\b(?:api[_-]?key|token|secret|password)\s*[:=]\s*\S+))")


class ContextFirewallError(Exception):
    """A forbidden context input was offered to the firewall (fail closed)."""


class ContextBudgetError(Exception):
    """The compiled context exceeds the configured budget (fail closed)."""


def estimate_tokens(text: str) -> int:
    """Declared deterministic token ESTIMATE: len(chars) // 4."""
    return len(text) // 4


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _scrub_text(text: str) -> str:
    return _SECRET_VALUE_RE.sub("[REDACTED]", text)


def gate_evidence_context_eligible(gate_identity: str) -> bool:
    """Audit-vs-context boundary: which gate evidence may become station context.

    ALL gate evidence is retained in the ledger for audit regardless of this
    answer.  Only explicitly listed PUBLIC gate identities are eligible to be
    summarized into station context; hidden-gate evidence never is.
    """
    return gate_identity in CONTEXT_ELIGIBLE_GATE_IDENTITIES


@dataclass(frozen=True)
class ContextFirewall:
    """Allowlist-first classifier.  Unknown/forbidden inputs fail closed."""

    allowed_classes: frozenset = DEFAULT_ALLOWED_CLASSES

    def allows_class(self, context_class: str) -> bool:
        if context_class not in CONTEXT_CLASS_ALL:
            return False
        return context_class in self.allowed_classes

    def check_file(self, rel_posix: str) -> str | None:
        """Return the exclusion reason for a candidate file, or None if allowed.

        Fail-closed checks (order fixed): traversal, hidden-test paths,
        secret-ish names, forbidden directory markers, absolute paths.
        """
        if ".." in PurePosixPath(rel_posix).parts:
            return "FORBIDDEN_TRAVERSAL"
        low = rel_posix.lower()
        for marker in _FORBIDDEN_DIR_MARKERS:
            if marker in low:
                return f"FORBIDDEN_DIR:{marker}"
        if _HIDDEN_RE.search(rel_posix.replace("\\", "/")):
            return "HIDDEN_TEST_BOUNDARY"
        if _SECRET_NAME_RE.search(rel_posix.replace("\\", "/")):
            return "SECRET_BLOCKLIST_PATH"
        if re.match(r"^[A-Za-z]:[\\/]", rel_posix) or rel_posix.startswith("/") or rel_posix.startswith("~"):
            return "ABSOLUTE_PATH_FORBIDDEN"
        return None

    def check_text(self, text: str) -> str | None:
        """Fail-closed scan of rendered text for forbidden content."""
        if _SECRET_VALUE_RE.search(text):
            return "SECRET_VALUE_DETECTED"
        if re.search(r"[A-Za-z]:\\Users\\", text):
            return "HOME_PATH_DETECTED"
        return None


def _contract_sections(task: TaskContract, station: StationContract, product: ProductSpec) -> list[dict[str, Any]]:
    """Deterministic primitive sections for the three contract classes."""
    def _texts(items) -> list[str]:
        out = []
        for it in items or ():
            t = getattr(it, "text", None) or getattr(it, "description", None) or (it if isinstance(it, str) else "")
            if t:
                out.append(str(t))
        return out

    return [
        {"context_class": CONTEXT_CLASS_TASK_CONTRACT, "task_id": str(task.id), "goal": task.goal,
         "allowed_scope": [str(s) for s in (task.allowed_scope or ())],
         "acceptance_criteria": _texts(task.acceptance_criteria)},
        {"context_class": CONTEXT_CLASS_STATION_CONTRACT, "station_id": str(station.id),
         "role": station.role, "required_output_kinds": list(station.required_output_kinds or ())},
        {"context_class": CONTEXT_CLASS_PRODUCT_CONTRACT, "product_id": str(product.id),
         "requested_outcome": product.requested_outcome,
         "requirements": _texts(product.requirements), "constraints": _texts(product.constraints)},
    ]


@dataclass
class CompiledContextPackage:
    """First-class, provider-neutral compiled context package."""

    strategy_id: str
    strategy_version: int
    task_id: str
    station_id: str
    product_id: str
    allowed_classes: tuple[str, ...]
    budget_bytes: int
    sections: tuple[dict[str, Any], ...]
    included_files: tuple[dict[str, Any], ...]   # {path, sha256, bytes, context_class}
    excluded_files: tuple[dict[str, Any], ...]   # {path, reason, context_class}
    upstream_handoffs: tuple[dict[str, Any], ...]  # {handoff_id, producer_station_id, artifact_refs}
    prior_failure_evidence: tuple[dict[str, Any], ...]  # identity entries only
    rendered_text: str = ""
    instructions: str = ""
    context_body: str = ""
    station_role: str | None = None
    counts: dict[str, Any] = field(default_factory=dict)
    package_hash: str = ""
    rendered_hash: str = ""

    def to_evidence(self) -> dict[str, Any]:
        """Primitive evidence dict (identity + hashes + counts; no file bodies)."""
        return {
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "task_id": self.task_id,
            "station_id": self.station_id,
            "product_id": self.product_id,
            "allowed_classes": sorted(self.allowed_classes),
            "budget_bytes": self.budget_bytes,
            "counts": dict(self.counts),
            "included_files": [dict(f) for f in self.included_files],
            "excluded_files": [dict(f) for f in self.excluded_files],
            "upstream_handoffs": [dict(h) for h in self.upstream_handoffs],
            "prior_failure_evidence": [dict(f) for f in self.prior_failure_evidence],
            "package_hash": self.package_hash,
            "rendered_hash": self.rendered_hash,
        }


def compile_context(
    *,
    strategy_id: str,
    task: TaskContract,
    station: StationContract,
    product: ProductSpec,
    workspace_root: Path | str,
    selected_paths: Sequence[str] = (),
    handoff_manifests: Sequence[HandoffManifest] = (),
    prior_failure_evidence: Sequence[Mapping[str, Any]] = (),
    budget_bytes: int = DEFAULT_BUDGET_BYTES,
    firewall: ContextFirewall | None = None,
    station_role: str | None = None,
    allowed_scope: Sequence[str] = (),
    changed_files: Sequence[str] = (),
    diff_text: str | None = None,
    gate_summaries: Sequence[Mapping[str, Any]] = (),
) -> CompiledContextPackage:
    """Compile a deterministic context package.  Fails closed on:
    unknown strategy, forbidden inputs (firewall), and budget overflow.

    ``station_role`` selects the runtime station rendering:
      * None            — identity-only package (REDI-05A behavior);
      * "builder"       — L6 instructions/context_body (task, acceptance,
                          constraints, allowed scope, workspace files, and
                          the sanitized prior-failure section);
      * "reviewer"      — reviewer instructions/context_body (changed files,
                          diff, deterministic gate outcome summaries).
    """
    if strategy_id not in CONTEXT_STRATEGY_IDS:
        raise ContextFirewallError(f"unknown context strategy: {strategy_id!r}")
    fw = firewall or ContextFirewall()
    root = Path(workspace_root).resolve()

    # --- contract sections (allowlist-checked classes) ---
    sections: list[dict[str, Any]] = []
    for sec in _contract_sections(task, station, product):
        if not fw.allows_class(sec["context_class"]):
            continue
        sections.append(sec)

    # --- source files per strategy ---
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    if strategy_id == CONTEXT_STRATEGY_SELECTED_V0:
        candidates = [(str(p), CONTEXT_CLASS_SOURCE_FILES) for p in selected_paths]
    else:  # full_public_v0: every eligible PUBLIC file inside the bounded root
        candidates = []
        for p in sorted(root.rglob("*")):
            if p.is_file():
                candidates.append((p.relative_to(root).as_posix(), CONTEXT_CLASS_SOURCE_FILES))
        candidates = [(c[0], c[1]) for c in sorted(candidates)]

    for rel, context_class in candidates:
        reason = fw.check_file(rel)
        if reason is None and not fw.allows_class(context_class):
            reason = "CONTEXT_CLASS_NOT_ALLOWED"
        if reason is not None:
            if strategy_id == CONTEXT_STRATEGY_SELECTED_V0:
                # The operator EXPLICITLY selected this path — silently
                # dropping it would misrepresent the package.  Fail closed.
                raise ContextFirewallError(
                    f"selected source rejected by firewall: {rel!r} ({reason})"
                )
            excluded.append({"path": rel, "reason": reason, "context_class": context_class})
            continue
        abs_p = root / PurePosixPath(rel)
        if not abs_p.is_file():
            # selected_v0 references must exist — fail closed, never invent.
            raise ContextFirewallError(f"selected source not found in workspace: {rel!r}")
        data = abs_p.read_bytes()
        included.append({
            "path": rel,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "context_class": context_class,
        })

    # --- upstream handoffs (explicit class + explicit arguments only) ---
    handoffs: list[dict[str, Any]] = []
    if fw.allows_class(CONTEXT_CLASS_UPSTREAM_HANDOFFS):
        for h in handoff_manifests or ():
            handoffs.append({
                "handoff_id": str(h.id),
                "producer_node_id": str(h.node_id) if h.node_id else None,
                "producer_station_id": str(h.producer_station_id) if h.producer_station_id else None,
                "consumer_scope": h.consumer_scope,
                "public_interfaces": [str(i) for i in (h.public_interfaces or ())],
                "artifact_refs": [
                    {
                        "ref_id": a.ref_id,
                        "kind": a.kind,
                        "sha256": a.sha256,
                        "location": a.location,
                        "size_bytes": a.metadata.get("size_bytes"),
                    }
                    for a in (h.produced_artifacts or ())
                ],
            })

    # --- prior failure evidence (explicit class + explicit arguments only;
    #     raw hidden-gate evidence is rejected outright — audit-only) ---
    failures: list[dict[str, Any]] = []
    for item in prior_failure_evidence or ():
        gate = str(item.get("gate_identity") or item.get("boundary") or "")
        sanitized = bool(item.get("context_sanitized"))
        # The class gate applies to RAW evidence; L9-sanitized items (produced
        # by bound_retry_feedback and explicitly marked) are the policy opt-in.
        if not sanitized and not fw.allows_class(CONTEXT_CLASS_PRIOR_FAILURE_EVIDENCE):
            continue
        if not sanitized and not gate_evidence_context_eligible(gate):
            raise ContextFirewallError(
                f"prior failure evidence from gate {gate!r} is not context-eligible (audit-only)"
            )
        entry: dict[str, Any] = {
            "attempt": item.get("attempt"),
            "category": item.get("category"),
            "boundary": gate,
            "reasons": [str(r) for r in (item.get("reasons") or [])][:8],
        }
        if item.get("diagnostic"):
            entry["diagnostic"] = str(item["diagnostic"])[:800]
        paths = item.get("offending_paths") or ()
        if paths:
            entry["offending_paths"] = [str(x)[:200] for x in list(paths)[:10]]
        failures.append(entry)

    # --- render deterministically ---
    if station_role not in (None, "builder", "reviewer"):
        raise ContextFirewallError(f"unknown station_role: {station_role!r}")

    if station_role is None:
        # REDI-05A identity-only canonical rendering.
        parts: list[str] = []
        for sec in sections:
            parts.append("## " + str(sec["context_class"]))
            parts.append(json.dumps(
                {k: v for k, v in sec.items() if k != "context_class"},
                sort_keys=True, ensure_ascii=False,
            ))
        if included:
            parts.append("## source_files")
            for f in included:
                data = (root / PurePosixPath(f["path"])).read_text(encoding="utf-8", errors="replace")
                parts.append(f"### File: {f['path']}")
                parts.append(data.rstrip("\n"))
        if handoffs:
            parts.append("## upstream_handoffs")
            parts.append(json.dumps(handoffs, sort_keys=True, ensure_ascii=False))
        if failures:
            parts.append("## prior_failure_evidence")
            parts.append(json.dumps(failures, sort_keys=True, ensure_ascii=False))
        rendered = "\n".join(parts)
        instructions = context_body = ""
        rendered_hash = _sha256_text(rendered)
        combined = rendered
    else:
        # REDI-05B: station runtime rendering (exact accepted L6 formats).
        scope = [str(s) for s in (allowed_scope or ())]

        def _acceptance_lines() -> list[str]:
            lines: list[str] = []
            for item in (
                list(task.acceptance_criteria)
                + list(product.acceptance_criteria)
                + list(getattr(product, "requirements", []) or [])
            ):
                t = getattr(item, "text", None) or getattr(item, "description", None) or (item if isinstance(item, str) else "")
                t = str(t) if t else ""
                if t and t not in lines:
                    lines.append(t)
            return lines

        def _constraint_lines() -> list[str]:
            return [str(c) for c in ((product.constraints or ()) if hasattr(product, "constraints") else ()) if c]

        ins: list[str] = []
        body: list[str] = []
        if station_role == "builder":
            ins.append("## Task")
            requested = getattr(product, "requested_outcome", "")
            if requested:
                ins.append("Requested outcome: " + requested)
            goal = getattr(task, "goal", "")
            if goal:
                ins.append("Goal: " + goal)
            acc = _acceptance_lines()
            if acc:
                ins.append("")
                ins.append("## Acceptance criteria")
                ins.extend("- " + t for t in acc)
            cons = _constraint_lines()
            if cons:
                ins.append("")
                ins.append("## Constraints")
                ins.extend("- " + c for c in cons)
            if scope:
                ins.append("")
                ins.append("## Allowed scope")
                ins.append("Edits are allowed ONLY under these path prefixes: " + ", ".join(scope) + ".")
                ins.append("Any file created, modified, or deleted outside this scope is FORBIDDEN and will be rejected.")
            body.append("## Workspace files (relative path; current content)")
            for f in sorted(included, key=lambda x: x["path"]):
                data = (root / PurePosixPath(f["path"])).read_text(encoding="utf-8", errors="replace")
                body.append("")
                body.append("### File: " + f["path"])
                body.append(data.rstrip("\n"))
            if failures:
                body.append("")
                body.append("## Previous attempt failure (bounded summary; fix this on the retry)")
                body.append(json.dumps(failures[-1], sort_keys=True, ensure_ascii=False))
            if handoffs:
                body.append("")
                body.append("## Governed upstream handoffs")
                body.append(json.dumps(handoffs, sort_keys=True, ensure_ascii=False))
        else:  # reviewer
            ins.append("## Review responsibility")
            ins.append("Independently verify the Builder's changed files against the acceptance criteria below.")
            ins.append("Do NOT trust the Builder's self-assessment; rely on the diff and the deterministic gate outcomes.")
            ins.append("")
            ins.append("## Task")
            requested = getattr(product, "requested_outcome", "")
            if requested:
                ins.append("Requested outcome: " + requested)
            goal = getattr(task, "goal", "")
            if goal:
                ins.append("Goal: " + goal)
            acc = [str(t) for t in _acceptance_lines()]
            if acc:
                ins.append("")
                ins.append("## Acceptance criteria")
                ins.extend("- " + t for t in acc)
            cons = _constraint_lines()
            if cons:
                ins.append("")
                ins.append("## Constraints")
                ins.extend("- " + c for c in cons)
            if scope:
                ins.append("")
                ins.append("## Allowed scope")
                ins.append("Changed files must stay under these path prefixes: " + ", ".join(scope) + ".")
            body.append("## Changed files")
            cf = [str(f) for f in (changed_files or ())]
            if cf:
                body.extend("- " + f for f in cf)
            else:
                body.append("(none)")
            body.append("")
            body.append("## Diff of Builder changes (unified, bounded context)")
            body.append((diff_text or "").rstrip("\n") if (diff_text or "").strip() else "(no textual diff)")
            body.append("")
            body.append("## Deterministic gate outcomes")
            gs = list(gate_summaries or ())
            if gs:
                for g in gs:
                    body.append(
                        "- gate={} status={} reasons_count={}".format(
                            str(g.get("gate_identity", "")),
                            str(g.get("status", "")),
                            str(g.get("failure_reasons_count", 0)),
                        )
                    )
            else:
                body.append("(none recorded)")
            if handoffs:
                body.append("")
                body.append("## Governed upstream handoffs")
                body.append(json.dumps(handoffs, sort_keys=True, ensure_ascii=False))

        instructions = _scrub_text("\n".join(ins))
        context_body = _scrub_text("\n".join(body))
        rendered = instructions + "\x00" + context_body
        combined = instructions + "\n" + context_body
        rendered_hash = _sha256_text(instructions + "\x00" + context_body)

    # --- firewall + budget fail-closed checks ---
    violation = fw.check_text(combined)
    if violation is not None:
        raise ContextFirewallError(f"rendered context violates firewall: {violation}")
    counts = {
        "bytes": len(combined.encode("utf-8")),
        "chars": len(combined),
        "estimated_tokens": estimate_tokens(combined),
        "token_estimate_method": TOKEN_ESTIMATE_METHOD,
        "file_count": len(included),
    }
    if counts["bytes"] > budget_bytes:
        raise ContextBudgetError(
            f"compiled context {counts['bytes']} bytes exceeds budget {budget_bytes} bytes "
            f"(strategy={strategy_id}); fail-closed per REDI-05A"
        )

    # --- deterministic identity hashes ---
    identity = {
        "strategy_id": strategy_id,
        "strategy_version": CONTEXT_STRATEGY_VERSION,
        "station_role": station_role or "",
        "task_id": str(task.id),
        "station_id": str(station.id),
        "product_id": str(product.id),
        "allowed_classes": sorted(fw.allowed_classes),
        "budget_bytes": int(budget_bytes),
        "included_files": included,
        "excluded_files": excluded,
        "upstream_handoffs": handoffs,
        "prior_failure_evidence": failures,
        "changed_files": sorted(str(f) for f in (changed_files or ())),
        "gate_summaries": [dict(g) for g in (gate_summaries or ())],
        "rendered_hash": rendered_hash,
    }
    package_hash = hashlib.sha256(
        json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()

    return CompiledContextPackage(
        strategy_id=strategy_id,
        strategy_version=CONTEXT_STRATEGY_VERSION,
        task_id=str(task.id),
        station_id=str(station.id),
        product_id=str(product.id),
        allowed_classes=tuple(sorted(fw.allowed_classes)),
        budget_bytes=int(budget_bytes),
        sections=tuple(sections),
        included_files=tuple(included),
        excluded_files=tuple(excluded),
        upstream_handoffs=tuple(handoffs),
        prior_failure_evidence=tuple(failures),
        rendered_text=rendered,
        instructions=instructions,
        context_body=context_body,
        station_role=station_role,
        counts=counts,
        package_hash=package_hash,
        rendered_hash=rendered_hash,
    )


__all__ = [
    "CONTEXT_CLASS_ALL",
    "CONTEXT_CLASS_PRIOR_FAILURE_EVIDENCE",
    "CONTEXT_CLASS_UPSTREAM_HANDOFFS",
    "CONTEXT_ELIGIBLE_GATE_IDENTITIES",
    "CONTEXT_STRATEGY_FULL_PUBLIC_V0",
    "CONTEXT_STRATEGY_IDS",
    "CONTEXT_STRATEGY_SELECTED_V0",
    "CONTEXT_STRATEGY_VERSION",
    "DEFAULT_ALLOWED_CLASSES",
    "DEFAULT_BUDGET_BYTES",
    "CompiledContextPackage",
    "ContextBudgetError",
    "ContextFirewall",
    "ContextFirewallError",
    "TOKEN_ESTIMATE_METHOD",
    "compile_context",
    "estimate_tokens",
    "gate_evidence_context_eligible",
]
