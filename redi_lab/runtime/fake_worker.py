"""Deterministic scripted workers used by REDI-03 tests.

Zero network, zero provider types.  Inputs → outputs are pure functions of
case_id and attempt_index.  Repeatability: same parameters → identical bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..domain import (
    ContextManifestId,
    StationId,
    TaskId,
    TokenUsage,
    WorkerId,
)
from .contracts import NodeRunResult, PatchEdit, ReviewerVerdict, WorkerRunner


def fake_token_usage(prompt: int = 100, completion: int = 50) -> TokenUsage:
    return TokenUsage(
        input=prompt,
        output=completion,
        cached_input=0,
        reasoning=0,
        other={"total": prompt + completion},
    )


_CORRECT_SLUGIFY_PY = '''"""URL-friendly text slugification with unicode normalization."""

from __future__ import annotations

import re
import unicodedata
from typing import Optional


def slugify(text: str, *, maxlen: Optional[int] = None, separator: str = "-") -> str:
    if not isinstance(text, str):
        raise TypeError("text must be str")
    if not text:
        return ""
    norm = unicodedata.normalize("NFKD", text)
    ascii_bytes = norm.encode("ascii", "ignore").decode("ascii")
    lower = ascii_bytes.lower()
    cleaned = re.sub(r"[^a-z0-9]+", separator, lower)
    cleaned = cleaned.strip(separator)
    if not cleaned:
        return ""
    if maxlen is not None and isinstance(maxlen, int) and 0 < maxlen < len(cleaned):
        candidate = cleaned[:maxlen]
        if cleaned[maxlen:maxlen + 1] == separator and separator in candidate[:-1]:
            cut = candidate.rfind(separator)
            if cut > 0:
                candidate = candidate[:cut]
        fallback = cleaned[:maxlen].strip(separator)
        cleaned = candidate.strip(separator) or fallback
    return cleaned
'''


_BAD_SYNTAX_SLUGIFY_PY = '''"""Intentionally bad syntax for REDI-03 retry-escalation tests."""

def slugify(;  # invalid syntax
'''


@dataclass
class FakeScriptedWorker(WorkerRunner):
    """Returns scripted patches / verdicts by (case_id, attempt_index)."""

    outputs_by_case: Mapping[str, Any]
    call_counter: dict[tuple[str, int], int] | None = None

    def __post_init__(self) -> None:
        if self.call_counter is None:
            self.call_counter = {}

    # ------------------------------------------------------------------
    # Public helper case factories
    # ------------------------------------------------------------------
    @classmethod
    def case_fix_slugify_builder(cls) -> "FakeScriptedWorker":
        """Builder case: write correct slugify on first attempt, always."""
        return cls(
            outputs_by_case={
                "default": lambda attempt: {
                    "patches": [
                        PatchEdit(
                            rel_path="slugify/__init__.py",
                            action="write",
                            content=_CORRECT_SLUGIFY_PY,
                        )
                    ],
                    "tokens": (300, 1200),
                }
            }
        )

    @classmethod
    def case_syntax_then_fix_builder(cls) -> "FakeScriptedWorker":
        """attempt 0 → bad syntax; attempt ≥1 → correct patch."""
        def _factory(attempt: int) -> dict:
            if attempt <= 0:
                content = _BAD_SYNTAX_SLUGIFY_PY
            else:
                content = _CORRECT_SLUGIFY_PY
            return {
                "patches": [
                    PatchEdit(
                        rel_path="slugify/__init__.py",
                        action="write",
                        content=content,
                    )
                ],
                "tokens": (300, 1200),
            }
        return cls(outputs_by_case={"default": _factory})

    @classmethod
    def case_always_bad_syntax_builder(cls) -> "FakeScriptedWorker":
        def _factory(_attempt: int) -> dict:
            return {
                "patches": [
                    PatchEdit(
                        rel_path="slugify/__init__.py",
                        action="write",
                        content=_BAD_SYNTAX_SLUGIFY_PY,
                    )
                ],
                "tokens": (300, 1200),
            }
        return cls(outputs_by_case={"default": _factory})

    # ------------------------------------------------------------------
    # run_node
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
        case_id = context.get("case_id", "default") if isinstance(context, dict) else "default"
        attempt = int(context.get("attempt_index", 0)) if isinstance(context, dict) else 0
        key = (str(case_id), attempt)
        if self.call_counter is not None:
            self.call_counter[key] = self.call_counter.get(key, 0) + 1
        spec = self.outputs_by_case.get(case_id)
        if spec is None:
            spec = self.outputs_by_case.get("default")
        if callable(spec):
            data = spec(attempt)
        else:
            data = dict(spec or {})
        patches = data.get("patches", ())
        pt, ct = data.get("tokens", (100, 50))
        return NodeRunResult(
            worker_id=worker_id,
            task_id=task_id,
            station_id=station_id,
            patches=tuple(patches),
            token_usage=fake_token_usage(pt, ct),
            attempt_index=attempt,
            opaque_provider_ref=f"fake:{case_id}@{attempt}",
            elapsed_seconds=0.001,
        )


def _reviewer(verdict: ReviewerVerdict, reasons: tuple[str, ...], refs: tuple[str, ...] = ()) -> type:
    """Build a class that acts as a FakeScriptedWorker for reviewer stations."""

    @dataclass
    class _FakeReviewer(WorkerRunner):
        call_counter: dict[str, int] | None = None

        def __post_init__(self) -> None:
            if self.call_counter is None:
                self.call_counter = {}

        def run_node(
            self,
            *,
            task_id: TaskId,
            station_id: StationId,
            worker_id: WorkerId,
            context: dict[str, Any],
            workspace_root: Any,
            station_role: str = "reviewer",
        ) -> NodeRunResult:
            key = str(verdict.value)
            if self.call_counter is not None:
                self.call_counter[key] = self.call_counter.get(key, 0) + 1
            return NodeRunResult(
                worker_id=worker_id,
                task_id=task_id,
                station_id=station_id,
                reviewer_verdict=verdict,
                reviewer_reasons=reasons,
                reviewer_evidence_refs=refs,
                token_usage=fake_token_usage(200, 100),
                opaque_provider_ref=f"fake-reviewer:{key}",
                elapsed_seconds=0.001,
            )

    return _FakeReviewer


fake_reviewer_pass = _reviewer(
    ReviewerVerdict.PASS,
    reasons=(
        "Output patch implements correct unicode NFKD normalization + ASCII encoding.",
        "Lowercase and separator collapsing correct.",
        "maxlen trims cleanly at separator boundary.",
    ),
    refs=("public-tests-pass", "hidden-tests-pass"),
)

fake_reviewer_fail = _reviewer(
    ReviewerVerdict.FAIL,
    reasons=(
        "Unicode normalization missing: accents not stripped from output sample.",
    ),
    refs=("hidden-test-1-failed",),
)

fake_reviewer_escalate = _reviewer(
    ReviewerVerdict.ESCALATE,
    reasons=("Ambiguous output behavior under combined maxlen + unicode edge case; requires human or architectural decision.",),
    refs=("case-maxlen-unicode-ambiguity",),
)


__all__ = [
    "FakeScriptedWorker",
    "fake_reviewer_pass",
    "fake_reviewer_fail",
    "fake_reviewer_escalate",
]
