"""Typed contracts used by the runtime abstraction.

These are LOCAL helper dataclasses for REDI-03; they are never stored directly
as entity_type rows in the ledger (only domain contracts are).  Values are
translated into the six allowed domain contracts before ledger append.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum, unique
from typing import Any, Literal, Mapping, Sequence

from ..domain import (
    ContextManifestId,
    StationId,
    TaskId,
    TokenUsage,
    WorkerId,
)


@unique
class ReviewerVerdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    ESCALATE = "ESCALATE"


@dataclass
class PatchEdit:
    """Concrete patch: write (overwrite or create) or delete a file by rel path.

    rel_path is relative to workspace root, normalized, no .. traversal.
    """

    rel_path: str
    action: Literal["write", "delete"]
    content: str | None = None
    content_sha256: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PatchOutputContract:
    """Immutable, task-derived structural contract for a builder response.

    This is intentionally a runtime view rather than a second Product, Task,
    or Patch domain model.  The holdout factory derives it from the canonical
    TaskContract allowed scope and the compiled component target, then uses the
    same instance to render instructions, structured schema, local validation,
    the patch gate, and evidence interpretation.
    """

    policy_id: str
    policy_version: int
    expected_edit_count: int
    expected_action: str
    expected_path: str
    content_required: bool
    extra_edits_forbidden: bool
    allowed_scope: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.policy_id or self.policy_version <= 0:
            raise ValueError("patch output contract must be explicitly versioned")
        if self.expected_edit_count != 1 or not self.extra_edits_forbidden:
            raise ValueError("bounded builder contract requires exactly one edit and forbids extras")
        if self.expected_action != "write":
            raise ValueError("bounded builder contract requires a write action")
        if not self.expected_path or not self.allowed_scope:
            raise ValueError("patch output contract requires canonical target and allowed scope")
        normalized = self.expected_path.replace("\\", "/")
        if normalized != self.expected_path or normalized.startswith("/") or ".." in normalized.split("/"):
            raise ValueError("patch output contract target must be a safe relative path")
        if not any(
            normalized == scope.rstrip("/") or normalized.startswith(scope.rstrip("/") + "/")
            for scope in self.allowed_scope
        ):
            raise ValueError("patch output contract target is outside the task allowed scope")

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "expected_edit_count": self.expected_edit_count,
            "expected_action": self.expected_action,
            "expected_path": self.expected_path,
            "content_required": self.content_required,
            "extra_edits_forbidden": self.extra_edits_forbidden,
            "allowed_scope": list(self.allowed_scope),
        }

    @property
    def canonical_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def builder_instruction(self) -> str:
        """The sole explicit structural instruction for this response shape."""
        return (
            "## Builder output contract\n"
            f"Return exactly {self.expected_edit_count} patch/edit.\n"
            f"Its action must be `{self.expected_action}`.\n"
            f"Its target must be exactly `{self.expected_path}`.\n"
            "Its `content` field is required and must be a string.\n"
            "Do not return additional edits."
        )

    def validate_patches(self, patches: Sequence[PatchEdit]) -> str | None:
        """Return a safe structural reason, or ``None`` for the contract."""
        if len(patches) != self.expected_edit_count:
            return f"patch_count_expected_{self.expected_edit_count}_got_{len(patches)}"
        patch = patches[0]
        if patch.action != self.expected_action:
            return f"patch_0_action_expected_{self.expected_action}_got_{patch.action}"
        if patch.rel_path != self.expected_path:
            return "patch_0_path_does_not_match_contract"
        if self.content_required and patch.content is None:
            return "patch_0_content_required"
        return None


@dataclass(frozen=True)
class ParsedPatchObservation:
    """Ephemeral parsed patch facts, retained only as safe shape evidence.

    ``content`` is used only long enough to retain generated patch bytes.  It
    must never be serialized; callers use :meth:`safe_shape` for evidence.
    """

    index: int
    action: str | None
    rel_path: str | None
    content_field_present: bool
    content: str | None

    def safe_shape(self) -> dict[str, Any]:
        byte_count = len(self.content.encode("utf-8")) if isinstance(self.content, str) else None
        return {
            "index": self.index,
            "action": self.action,
            "rel_path": self.rel_path,
            "content_present": self.content_field_present,
            "content_byte_count": byte_count,
            "content_sha256": (
                hashlib.sha256(self.content.encode("utf-8")).hexdigest()
                if isinstance(self.content, str)
                else None
            ),
        }


@dataclass
class NodeRunResult:
    """Result of one worker node invocation.

    Returned by WorkerRunner.run_node.  Used by controller to apply patches or
    reviewer verdicts.  Provider-agnostic.
    """

    worker_id: WorkerId | None = None
    context_manifest_id: ContextManifestId | None = None
    task_id: TaskId | None = None
    station_id: StationId | None = None
    token_usage: TokenUsage | None = None
    attempt_index: int = 0
    patches: Sequence[PatchEdit] = field(default_factory=tuple)
    observed_patches: Sequence[ParsedPatchObservation] = field(default_factory=tuple)
    output_shape: Mapping[str, Any] = field(default_factory=dict)
    provider_completion: Mapping[str, Any] = field(default_factory=dict)
    reviewer_verdict: ReviewerVerdict | None = None
    reviewer_reasons: Sequence[str] = field(default_factory=tuple)
    reviewer_evidence_refs: Sequence[str] = field(default_factory=tuple)
    opaque_provider_ref: str | None = None
    elapsed_seconds: float | None = None
    error_message: str | None = None


class WorkerRunner:
    """Abstract interface for a worker (builder or reviewer) execution unit.

    Implementations: FakeScriptedWorker for tests; LiveProviderAdapter for live.
    """

    def run_node(
        self,
        *,
        task_id: TaskId,
        station_id: StationId,
        worker_id: WorkerId,
        context: dict[str, Any],
        workspace_root: Any,
        station_role: str = "builder",
    ) -> NodeRunResult:  # pragma: no cover - interface
        raise NotImplementedError


__all__ = [
    "NodeRunResult",
    "PatchEdit",
    "PatchOutputContract",
    "ParsedPatchObservation",
    "ReviewerVerdict",
    "WorkerRunner",
]
