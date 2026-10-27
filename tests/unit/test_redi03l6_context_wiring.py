"""REDI-03L6 — bounded live-station context wiring tests.

Deterministic, zero-network tests proving:
  * Builder attempt 0 receives the full minimum task context (requested
    outcome, goal, acceptance criteria, constraints, allowed scope, slugify
    stub content, public test content, pytest.ini) and NO hidden tests,
    NO unrelated repo files, NO absolute home paths.
  * Builder retry receives the same task/public context PLUS a bounded
    previous-failure summary; the prompt differs from attempt 0.
  * The rendered prompt is deterministic for identical inputs and fails
    closed when no context file can be read.
  * The controller actually wires instructions/context_body into BOTH
    station calls (builder + reviewer), reviewer role is the exact
    schema-selection token "reviewer", and evidence binds the prompt
    identity into persisted RunEvidence.metadata.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from redi_lab.domain import (
    ProductSpec,
    Requirement,
    RiskTier,
    StationContract,
    TaskContract,
    domain_from_json,
)
from redi_lab.factory.builder_context import (
    MAX_RETRY_REASON_CHARS,
    bound_retry_feedback,
    build_builder_context,
    build_reviewer_context,
    render_builder_prompt,
    render_reviewer_prompt,
)
from redi_lab.factory.controller import MinimalFactoryController, RunDeps, make_canonical_slug_fixture_refs
from redi_lab.factory.isolation import IsolatedWorkspace
from redi_lab.runtime.contracts import WorkerRunner
from redi_lab.runtime.fake_worker import FakeScriptedWorker, fake_reviewer_pass

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCOPE = ["slugify", "tests"]

_HIDDEN_MARKERS = (
    "slugify_pkg_hidden_tests",
    "test_hidden",
    "hidden_test_cwd",
    "hidden_tests/",
)
_UNRELATED_REPO_MARKERS = (
    "redi_lab/factory/controller.py",
    "live_provider_adapter",
    "builder_context.py",
    "conftest.py",
)


def _make_specs() -> tuple[ProductSpec, TaskContract, StationContract, StationContract]:
    product = ProductSpec(
        requested_outcome=(
            "Rewrite the stub slugify/__init__.py so it exposes slugify(text, "
            "*, maxlen=None, separator='-') implementing: unicode NFKD -> "
            "ascii ignoring accents -> lowercase -> [^a-z0-9]+ replaced by "
            "separator -> strip separators -> maxlen hard-cut with separator "
            "backoff only when boundary lands exactly on separator."
        ),
        requirements=[
            Requirement(text="slugify('hello world foo', maxlen=10) == 'hello-worl' (hard cut)", kind="functional"),
            Requirement(text="slugify('hello-world-foo', maxlen=10) == 'hello-worl' (separator boundary but no separator left)", kind="functional"),
            Requirement(text="public tests pass; hidden tests pass", kind="functional"),
        ],
        constraints=[
            "Only modify slugify/__init__.py; do not add or remove files.",
            "No external dependencies beyond stdlib (unicodedata, re, typing ok).",
        ],
        risk_tier=RiskTier.LOW,
        unresolved_requirements=[],
    )
    task = TaskContract(goal="produce PatchEdits so slugify passes public and hidden tests")
    builder_station = StationContract(role="builder", required_output_kinds=["patch_edit"], allowed_tool_classes=[])
    reviewer_station = StationContract(role="reviewer", required_output_kinds=["verdict"], allowed_tool_classes=[])
    return product, task, builder_station, reviewer_station


@dataclass
class _ContextRecorder(WorkerRunner):
    """Wraps a worker and records every context dict it is handed."""

    inner: WorkerRunner = field(default_factory=FakeScriptedWorker)
    captured: list[dict] = field(default_factory=list)

    def run_node(self, *, task_id, station_id, worker_id, context, workspace_root, station_role="builder"):
        self.captured.append(dict(context))
        return self.inner.run_node(
            task_id=task_id,
            station_id=station_id,
            worker_id=worker_id,
            context=context,
            workspace_root=workspace_root,
            station_role=station_role,
        )


def _default_deps(recorder_builder=None, recorder_reviewer=None, max_retries: int = 1) -> RunDeps:
    refs = make_canonical_slug_fixture_refs(_REPO_ROOT)
    builder = recorder_builder or FakeScriptedWorker.case_fix_slugify_builder()
    reviewer = recorder_reviewer or fake_reviewer_pass()
    _, _, sb, sr = _make_specs()
    return RunDeps(
        builder_worker=builder,
        reviewer_worker=reviewer,
        builder_station=sb,
        reviewer_station=sr,
        fixture_root=refs["fixture_root"],
        hidden_test_root=refs["hidden_test_root"],
        allowed_scope_prefixes=refs["allowed_scope_prefixes"],
        gate_spec=refs["gate_spec"],
        max_retries=max_retries,
    )


# ---------------------------------------------------------------------------
# Builder prompt content (§2 / §8 local preview)
# ---------------------------------------------------------------------------


def _render_attempt0():
    product, task, station, _ = _make_specs()
    with IsolatedWorkspace.create(
        fixture_root=_REPO_ROOT / "experiments" / "fixtures" / "slugify_pkg",
        allowed_scope_prefixes=list(_SCOPE),
    ) as ws:
        manifest = build_builder_context(
            task=task, station=station, product=product, fixture_root=ws.root,
            allowed_scope=list(_SCOPE),
        )
        prompt = render_builder_prompt(
            product=product, task=task, station_role="builder",
            workspace_root=ws.root, ctx_manifest=manifest,
        )
    return prompt


def test_l6_builder_prompt_contains_minimum_task_context():
    prompt = _render_attempt0()
    combined = prompt["instructions"] + "\x00" + prompt["context_body"]

    # A. TASK
    assert "Requested outcome: Rewrite the stub slugify/__init__.py" in prompt["instructions"]
    assert "unicode NFKD" in prompt["instructions"]
    assert "Goal: produce PatchEdits so slugify passes public and hidden tests" in prompt["instructions"]
    # B. ACCEPTANCE (requirements rendered as criteria) + constraints
    assert "slugify('hello world foo', maxlen=10) == 'hello-worl' (hard cut)" in prompt["instructions"]
    assert "- Only modify slugify/__init__.py; do not add or remove files." in prompt["instructions"]
    # C. ALLOWED SCOPE + explicit forbidding statement
    assert "Edits are allowed ONLY under these path prefixes: slugify, tests." in prompt["instructions"]
    assert "outside this scope is FORBIDDEN" in prompt["instructions"]
    # D. PUBLIC WORKSPACE INPUTS — each file rendered with rel path + content
    assert "### File: slugify/__init__.py" in prompt["context_body"]
    assert "def slugify(text: str" in prompt["context_body"]
    assert "STUB" in prompt["context_body"]  # stub docstring marker proves real content
    assert "### File: tests/test_slugify_public.py" in prompt["context_body"]
    assert "assert slugify(\"Hello World\") == \"hello-world\"" in prompt["context_body"]
    assert "### File: pytest.ini" in prompt["context_body"]
    assert "testpaths = tests" in prompt["context_body"]
    # deterministic identity present
    assert re.fullmatch(r"[0-9a-f]{64}", prompt["sha256"])


def test_l6_builder_prompt_excludes_hidden_unrelated_and_absolute_paths():
    prompt = _render_attempt0()
    combined = prompt["instructions"] + "\x00" + prompt["context_body"]
    for marker in _HIDDEN_MARKERS:
        assert marker not in combined
    for marker in _UNRELATED_REPO_MARKERS:
        assert marker not in combined
    # no windows drive paths, no home dirs
    assert not re.search(r"[A-Za-z]:[\\/]", combined)
    assert "/home/" not in combined and "/Users/" not in combined
    assert str(Path.home()) not in combined


def test_l6_builder_prompt_deterministic_across_workspaces():
    p1 = _render_attempt0()
    p2 = _render_attempt0()  # fresh IsolatedWorkspace, fresh manifest id
    assert p1["instructions"] == p2["instructions"]
    assert p1["context_body"] == p2["context_body"]
    assert p1["sha256"] == p2["sha256"]


# ---------------------------------------------------------------------------
# Retry feedback (§3)
# ---------------------------------------------------------------------------


def test_l6_attempt0_has_no_feedback_section_and_retry_has_bounded_one():
    product, task, station, _ = _make_specs()
    fb = {
        "attempt": 0,
        "category": "scope_violation",
        "boundary": "allowed_file_change_scope",
        "offending_paths": ["src/config/settings.py"],
    }
    with IsolatedWorkspace.create(
        fixture_root=_REPO_ROOT / "experiments" / "fixtures" / "slugify_pkg",
        allowed_scope_prefixes=list(_SCOPE),
    ) as ws:
        manifest = build_builder_context(
            task=task, station=station, product=product, fixture_root=ws.root,
            allowed_scope=list(_SCOPE),
        )
        p0 = render_builder_prompt(
            product=product, task=task, station_role="builder",
            workspace_root=ws.root, ctx_manifest=manifest,
        )
        p1 = render_builder_prompt(
            product=product, task=task, station_role="builder",
            workspace_root=ws.root, ctx_manifest=manifest, retry_feedback=fb,
        )
    assert "Previous attempt failure" not in p0["context_body"]
    assert "## Previous attempt failure" in p1["context_body"]
    assert '"category": "scope_violation"' in p1["context_body"]
    assert '"offending_paths": ["src/config/settings.py"]' in p1["context_body"]
    # Task/public context is unchanged between the attempts
    assert p0["instructions"] == p1["instructions"]
    assert p0["context_body"].rstrip("\n") == \
        p1["context_body"].split("## Previous attempt failure")[0].rstrip("\n")
    assert p0["sha256"] != p1["sha256"]


def test_l6_retry_feedback_is_bounded_and_scrubbed():
    long_reason = "x" * (MAX_RETRY_REASON_CHARS * 5)
    bounded = bound_retry_feedback({
        "attempt": 0,
        "category": "gate_failed",
        "boundary": "public_test_execution",
        "reasons": ["ok reason", "hidden dir leak: slugify_pkg_hidden_tests/test_x.py", long_reason],
        "offending_paths": ["b" * 500, "a.py"],
    })
    assert bounded is not None
    assert len(bounded["reasons"]) == 2  # hidden-path reason filtered, capped count
    assert bounded["reasons"][1] == "x" * MAX_RETRY_REASON_CHARS  # length capped
    assert all("slugify_pkg_hidden_tests" not in r for r in bounded["reasons"])
    assert bounded["offending_paths"] == ["a.py", "b" * 200]  # sorted, length capped
    assert bound_retry_feedback(None) is None
    assert bound_retry_feedback({}) is None


def test_l6_manifest_hash_binds_scope_and_retry_category():
    product, task, station, _ = _make_specs()
    fx = _REPO_ROOT / "experiments" / "fixtures" / "slugify_pkg"
    with IsolatedWorkspace.create(fixture_root=fx, allowed_scope_prefixes=list(_SCOPE)) as ws:
        m_a = build_builder_context(task=task, station=station, product=product, fixture_root=ws.root,
                                    allowed_scope=list(_SCOPE), retry_feedback_category=None)
        m_b = build_builder_context(task=task, station=station, product=product, fixture_root=ws.root,
                                    allowed_scope=list(_SCOPE), retry_feedback_category="gate_failed")
        m_c = build_builder_context(task=task, station=station, product=product, fixture_root=ws.root,
                                    allowed_scope=["slugify"], retry_feedback_category=None)
    assert m_a.deterministic_hash != m_b.deterministic_hash  # retry category bound
    assert m_a.deterministic_hash != m_c.deterministic_hash  # scope bound
    assert m_a.metadata["allowed_scope"] == list(_SCOPE)
    assert m_a.metadata["retry_feedback_category"] is None


def test_l6_prompt_render_fails_closed_without_readable_files():
    product, task, station, _ = _make_specs()
    fx = _REPO_ROOT / "experiments" / "fixtures" / "slugify_pkg"
    with IsolatedWorkspace.create(fixture_root=fx, allowed_scope_prefixes=list(_SCOPE)) as ws:
        manifest = build_builder_context(
            task=task, station=station, product=product, fixture_root=ws.root,
            allowed_scope=list(_SCOPE),
        )
    from redi_lab.context_compiler import ContextFirewallError
    with pytest.raises(ContextFirewallError, match="not found in workspace"):
        render_builder_prompt(
            product=product, task=task, station_role="builder",
            workspace_root=_REPO_ROOT / "experiments" / "definitely_missing_dir",
            ctx_manifest=manifest,
        )


# ---------------------------------------------------------------------------
# Controller wiring (builder + reviewer) and adapter schema selection
# ---------------------------------------------------------------------------


def test_l6_controller_wires_builder_and_reviewer_context_end_to_end():
    rec_builder = _ContextRecorder(inner=FakeScriptedWorker.case_fix_slugify_builder())
    rec_reviewer = _ContextRecorder(inner=fake_reviewer_pass())
    deps = _default_deps(recorder_builder=rec_builder, recorder_reviewer=rec_reviewer)
    product, task, _, _ = _make_specs()

    outcome = MinimalFactoryController.run(product, task, deps)
    assert outcome.final_status.value == "completed"
    assert outcome.verified is True

    # --- Builder call received the full bounded context ---
    assert len(rec_builder.captured) == 1
    bctx = rec_builder.captured[0]
    assert bctx["instructions"] and bctx["context_body"]
    assert "Requested outcome: Rewrite the stub slugify/__init__.py" in bctx["instructions"]
    assert "FORBIDDEN" in bctx["instructions"]
    assert "### File: slugify/__init__.py" in bctx["context_body"]
    assert "### File: tests/test_slugify_public.py" in bctx["context_body"]
    assert "Previous attempt failure" not in bctx["context_body"]  # attempt 0
    for marker in _HIDDEN_MARKERS:
        assert marker not in bctx["instructions"] + bctx["context_body"]

    # --- Reviewer call received task/artifact/gate context, exact role token ---
    assert len(rec_reviewer.captured) == 1
    rctx = rec_reviewer.captured[0]
    assert rctx["role"] == "reviewer"  # exact schema-selection token
    assert rctx["instructions"] and rctx["context_body"]
    assert "Requested outcome: Rewrite the stub slugify/__init__.py" in rctx["instructions"]
    assert "hello-worl" in rctx["instructions"]  # requirement rendered for reviewer
    assert "Changed files" in rctx["context_body"]
    assert "Diff of Builder changes" in rctx["context_body"]
    assert "slugify/__init__.py" in rctx["context_body"]
    assert "Deterministic gate outcomes" in rctx["context_body"]
    for marker in _HIDDEN_MARKERS:
        assert marker not in rctx["instructions"] + rctx["context_body"]

    # --- Adapter would pick the STRICT reviewer schema for this role token ---
    from redi_lab.runtime.live_provider_adapter import (
        REVIEWER_OUTPUT_JSON_SCHEMA,
        _response_format_for_role,
    )
    fmt = _response_format_for_role(rctx["role"])
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == REVIEWER_OUTPUT_JSON_SCHEMA["name"]

    # --- Persisted RunEvidence binds the prompt identity (reproducibility) ---
    conn = sqlite3.connect(outcome.db_path)
    try:
        rows = conn.execute(
            "SELECT payload_json FROM evidence_entries WHERE entity_type='RunEvidence' ORDER BY global_sequence"
        ).fetchall()
        assert len(rows) == 2  # builder + reviewer
        builder_ev = domain_from_json(rows[0][0])
        md = dict(builder_ev.metadata)
        assert md["context_sources"] == ["pytest.ini", "slugify/__init__.py", "tests/test_slugify_public.py"]
        assert re.fullmatch(r"[0-9a-f]{64}", md["context_manifest_hash"])
        assert re.fullmatch(r"[0-9a-f]{64}", md["builder_prompt_sha256"])
        # prompt sha is exactly the hash of what the worker was handed
        recomputed = __import__("hashlib").sha256(
            (bctx["instructions"] + "\x00" + bctx["context_body"]).encode("utf-8")
        ).hexdigest()
        assert md["builder_prompt_sha256"] == recomputed
        assert md["retry_feedback_summary"] in (None, "", {},)
        reviewer_ev = domain_from_json(rows[1][0])
        assert re.fullmatch(r"[0-9a-f]{64}", dict(reviewer_ev.metadata)["reviewer_prompt_sha256"])
    finally:
        conn.close()


def test_l6_controller_retry_attempt_receives_bounded_scope_failure_feedback():
    out_of_scope = FakeScriptedWorker(outputs_by_case={
        "default": lambda attempt: {
            "patches": [],
            "tokens": (10, 10),
        }
    })
    # Build an out-of-scope patch provider by wrapping the scripted worker:
    from redi_lab.runtime.contracts import PatchEdit, NodeRunResult

    @dataclass
    class _OutOfScopeWorker(WorkerRunner):
        captured: list = field(default_factory=list)

        def run_node(self, *, task_id, station_id, worker_id, context, workspace_root, station_role="builder"):
            self.captured.append(dict(context))
            return NodeRunResult(
                worker_id=worker_id,
                task_id=task_id,
                station_id=station_id,
                patches=(PatchEdit(rel_path="src/config/settings.py", action="write",
                                   content="DEBUG = False\n"),),
                attempt_index=int(context.get("attempt_index", 0)),
                elapsed_seconds=0.001,
            )

    attacker = _OutOfScopeWorker()
    deps = _default_deps(recorder_builder=attacker, max_retries=1)
    product, task, _, _ = _make_specs()

    outcome = MinimalFactoryController.run(product, task, deps)
    assert outcome.final_status.value == "blocked"
    assert outcome.escalation is True
    assert len(attacker.captured) == 2

    ctx0, ctx1 = attacker.captured
    assert "Previous attempt failure" not in ctx0["context_body"]
    assert "## Previous attempt failure" in ctx1["context_body"]
    assert '"category": "scope_violation"' in ctx1["context_body"]
    assert '"offending_paths": ["src/config/settings.py"]' in ctx1["context_body"]
    # Same task/public context on retry; only the feedback section differs.
    assert ctx0["instructions"] == ctx1["instructions"]
    assert ctx0["context_body"] != ctx1["context_body"]
    assert ctx0["context_body"].rstrip("\n") == \
        ctx1["context_body"].split("## Previous attempt failure")[0].rstrip("\n")


def test_l6_controller_gate_failure_feedback_reaches_retry_attempt():
    rec_builder = _ContextRecorder(inner=FakeScriptedWorker.case_syntax_then_fix_builder())
    deps = _default_deps(recorder_builder=rec_builder, max_retries=1)
    product, task, _, _ = _make_specs()

    outcome = MinimalFactoryController.run(product, task, deps)
    assert outcome.final_status.value == "completed"
    assert len(rec_builder.captured) == 2
    ctx0, ctx1 = rec_builder.captured
    assert "Previous attempt failure" not in ctx0["context_body"]
    assert '"category": "gate_failed"' in ctx1["context_body"]
    assert "python_syntax_compile" in ctx1["context_body"]
    assert ctx0["context_body"] != ctx1["context_body"]
