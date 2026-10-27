"""REDI-03L9 — public-safe actionable retry feedback tests.

Deterministic, zero-network tests proving:
  * public_test_execution gate failures yield a bounded, sanitized,
    actionable diagnostic (collection/import errors like NameError and
    concise assertion summaries) in the retry feedback,
  * the diagnostic is bounded (~800 chars) and truncation is deterministic,
  * secret-looking strings are scrubbed, absolute workspace/home paths are
    removed, relative public fixture paths are preserved,
  * hidden_test_execution failures stay fully opaque in retry feedback
    (no names, paths, assertions, expected values, or output excerpts),
  * attempt 0 receives no feedback section; attempt 1's prompt differs
    deterministically and the persisted retry_feedback_summary equals the
    rendered feedback.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from redi_lab.domain import (
    GateResult,
    GateStatus,
    domain_from_json,
)
from redi_lab.factory.builder_context import bound_retry_feedback
from redi_lab.factory.controller import MinimalFactoryController, RunDeps, make_canonical_slug_fixture_refs
from redi_lab.gates import PUBLIC_DIAGNOSTIC_MAX_CHARS, public_failure_diagnostic
from redi_lab.runtime.contracts import NodeRunResult, PatchEdit, WorkerRunner
from redi_lab.runtime.fake_worker import FakeScriptedWorker, fake_reviewer_pass

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _mk_gate_result(ident: str, *, status: GateStatus, reasons, metadata) -> GateResult:
    return GateResult(
        gate_identity=ident,
        status=status,
        criteria=(),
        failure_reasons=tuple(reasons),
        metadata=dict(metadata),
    )


_LIVE_STYLE_STDOUT = "\n".join([
    "============================= test session starts ==============================",
    "platform win32 -- Python 3.13.5, pytest-8.x, pluggy-1.x",
    "rootdir: C:\\Users\\builder\\AppData\\Local\\Temp\\redi_ws_abc\\slugify_pkg",
    "collected 4 items / 1 error",
    "",
    "=================================== ERRORS ====================================",
    "________________ ERROR collecting tests/test_slugify_public.py ________________",
    "tests\\test_slugify_public.py:17: in <module>",
    "    from slugify import slugify  # noqa: E402",
    "slugify\\__init__.py:5: in <module>",
    "    def slugify(text: str, *, maxlen: Optional[int] = None, separator: str = \"-\") -> str:",
    "                                      ^^^^^^^^",
    "E   NameError: name 'Optional' is not defined",
    "=========================== short test summary info ============================",
    "ERROR tests/test_slugify_public.py - NameError: name 'Optional' is not defined",
    "!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!",
    "1 error in 0.50s",
])


# ---------------------------------------------------------------------------
# 1. Public collection/import failure → actionable NameError diagnostic
# ---------------------------------------------------------------------------


def test_l9_public_collection_error_yields_actionable_nameerror():
    gate = _mk_gate_result(
        "public_test_execution",
        status=GateStatus.FAIL,
        reasons=["non-zero exit: 2"],
        metadata={"exit_code": 2, "stdout": _LIVE_STYLE_STDOUT, "stderr": ""},
    )
    diag = public_failure_diagnostic(
        gate, workspace_root=r"C:\Users\builder\AppData\Local\Temp\redi_ws_abc\slugify_pkg"
    )
    assert diag is not None
    assert "NameError: name 'Optional' is not defined" in diag
    assert "tests/test_slugify_public.py" in diag  # relative public path preserved
    # absolute workspace/home paths absent
    assert "C:" not in diag
    assert "AppData" not in diag
    assert "redi_ws_abc" not in diag
    assert not re.search(r"[A-Za-z]:[\\/]", diag)
    # deterministic
    again = public_failure_diagnostic(
        gate, workspace_root=r"C:\Users\builder\AppData\Local\Temp\redi_ws_abc\slugify_pkg"
    )
    assert diag == again
    # non-public gates never produce diagnostics
    hidden_gate = _mk_gate_result(
        "hidden_test_execution", status=GateStatus.FAIL, reasons=["hidden verification failed"], metadata={"stdout": "x"}
    )
    scope_gate = _mk_gate_result(
        "allowed_file_change_scope", status=GateStatus.FAIL, reasons=["out-of-scope"], metadata={"stdout": "x"}
    )
    assert public_failure_diagnostic(hidden_gate) is None
    assert public_failure_diagnostic(scope_gate) is None


# ---------------------------------------------------------------------------
# 2. Public assertion failure → concise summary reaches the diagnostic
# ---------------------------------------------------------------------------


def test_l9_public_assertion_failure_yields_concise_summary():
    stdout = "\n".join([
        "================================= FAILURES =================================",
        "________________________________ test_demo ________________________________",
        "    assert slugify('a b') == 'expected-wrong'",
        "E   assert 'a-b' == 'expected-wrong'",
        "=========================== short test summary info ============================",
        "FAILED tests/test_slugify_public.py::test_demo - assert 'a-b' == 'expected-wrong'",
        "========================= 1 failed, 3 passed in 0.10s =========================",
    ])
    gate = _mk_gate_result(
        "public_test_execution",
        status=GateStatus.FAIL,
        reasons=["non-zero exit: 1"],
        metadata={"exit_code": 1, "stdout": stdout, "stderr": ""},
    )
    diag = public_failure_diagnostic(gate, workspace_root=None)
    assert "FAILED tests/test_slugify_public.py::test_demo" in diag
    assert "assert 'a-b' == 'expected-wrong'" in diag
    assert "1 failed, 3 passed" in diag


# ---------------------------------------------------------------------------
# 3. Bounded deterministic truncation
# ---------------------------------------------------------------------------


def test_l9_diagnostic_truncation_deterministic_and_bounded():
    big = "\n".join(["line %04d %s" % (i, "x" * 60) for i in range(80)])
    gate = _mk_gate_result(
        "public_test_execution",
        status=GateStatus.FAIL,
        reasons=["non-zero exit: 1"],
        metadata={"exit_code": 1, "stdout": big, "stderr": ""},
    )
    d1 = public_failure_diagnostic(gate, workspace_root=None)
    d2 = public_failure_diagnostic(gate, workspace_root=None)
    assert d1 == d2
    assert len(d1) <= PUBLIC_DIAGNOSTIC_MAX_CHARS
    assert d1.endswith("...")


# ---------------------------------------------------------------------------
# 4. Secret scrubbing
# ---------------------------------------------------------------------------


def test_l9_diagnostic_scrubs_secret_looking_strings():
    stdout = "\n".join([
        "=========================== short test summary info ============================",
        "ERROR tests/test_slugify_public.py - RuntimeError: bad creds token=abcdef1234567890",
        "api_key=sk-abcdefghijklmnopqrstuvwxyz123456 also leaked",
        "1 error in 0.10s",
    ])
    gate = _mk_gate_result(
        "public_test_execution",
        status=GateStatus.FAIL,
        reasons=["non-zero exit: 2"],
        metadata={"exit_code": 2, "stdout": stdout, "stderr": ""},
    )
    diag = public_failure_diagnostic(gate, workspace_root=None)
    assert "abcdef1234567890" not in diag
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in diag
    assert "[REDACTED]" in diag


# ---------------------------------------------------------------------------
# 5. Hidden gate stays opaque through bound_retry_feedback
# ---------------------------------------------------------------------------


def test_l9_hidden_gate_feedback_is_fully_opaque():
    fb = bound_retry_feedback({
        "attempt": 0,
        "category": "gate_failed",
        "boundary": "hidden_test_execution",
        "reasons": ["hidden verification failed"],
    })
    assert fb["boundary"] == "hidden_test_execution"
    assert fb["reasons"] == ["hidden verification failed"]
    assert "diagnostic" not in fb
    blob = json.dumps(fb)
    for marker in ("test_hidden", "slugify_pkg_hidden_tests", "assert", "expected"):
        assert marker not in blob


def test_l9_bound_feedback_carries_public_diagnostic_and_bounded():
    fb = bound_retry_feedback({
        "attempt": 0,
        "category": "gate_failed",
        "boundary": "public_test_execution",
        "reasons": ["non-zero exit: 2"],
        "diagnostic": "ERROR tests/test_slugify_public.py - NameError: name 'Optional' is not defined",
    })
    assert fb["diagnostic"] == "ERROR tests/test_slugify_public.py - NameError: name 'Optional' is not defined"
    oversized = bound_retry_feedback({
        "attempt": 0,
        "category": "gate_failed",
        "boundary": "public_test_execution",
        "diagnostic": "y" * (PUBLIC_DIAGNOSTIC_MAX_CHARS + 500),
    })
    assert len(oversized["diagnostic"]) <= PUBLIC_DIAGNOSTIC_MAX_CHARS


# ---------------------------------------------------------------------------
# 6-8. Controller wiring: attempt 0 clean, attempt 1 carries the diagnostic,
#      prompt differs, persisted summary == rendered feedback
# ---------------------------------------------------------------------------


@dataclass
class _RecordingWorker(WorkerRunner):
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


# The exact defect shape observed in the real L8 live run: compiles, but
# NameError at import time (Optional used without import) → public gate fail.
_MISSING_OPTIONAL_PY = (
    "import unicodedata\n"
    "import re\n"
    "\n"
    "\n"
    "def slugify(text: str, *, maxlen: Optional[int] = None, separator: str = \"-\") -> str:\n"
    "    t = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('utf-8')\n"
    "    t = re.sub(r'[^a-z0-9]+', separator, t.lower()).strip(separator)\n"
    "    if maxlen is not None and 0 < maxlen < len(t):\n"
    "        t = t[:maxlen]\n"
    "    return t\n"
)


def _make_deps(builder: WorkerRunner, max_retries: int = 1) -> RunDeps:
    refs = make_canonical_slug_fixture_refs(_REPO_ROOT)
    from redi_lab.domain import StationContract

    builder_station = StationContract(role="builder", required_output_kinds=["patch_edit"], allowed_tool_classes=[])
    reviewer_station = StationContract(role="reviewer", required_output_kinds=["verdict"], allowed_tool_classes=[])
    return RunDeps(
        builder_worker=builder,
        reviewer_worker=fake_reviewer_pass(),
        builder_station=builder_station,
        reviewer_station=reviewer_station,
        fixture_root=refs["fixture_root"],
        hidden_test_root=refs["hidden_test_root"],
        allowed_scope_prefixes=refs["allowed_scope_prefixes"],
        gate_spec=refs["gate_spec"],
        max_retries=max_retries,
    )


def _make_specs_for_controller():
    from redi_lab.domain import ProductSpec, Requirement, RiskTier, TaskContract

    product = ProductSpec(
        requested_outcome="Canonical slugify implementation task.",
        requirements=[Requirement(text="public tests pass", kind="functional")],
        constraints=["Only modify slugify/__init__.py."],
        risk_tier=RiskTier.LOW,
        unresolved_requirements=[],
    )
    task = TaskContract(goal="implement slugify")
    return product, task


def test_l9_controller_attempt1_receives_actionable_public_diagnostic():
    @dataclass
    class _NameErrorBuilder(WorkerRunner):
        captured: list = field(default_factory=list)

        def run_node(self, *, task_id, station_id, worker_id, context, workspace_root, station_role="builder"):
            self.captured.append(dict(context))
            return NodeRunResult(
                worker_id=worker_id,
                task_id=task_id,
                station_id=station_id,
                patches=(PatchEdit(rel_path="slugify/__init__.py", action="write", content=_MISSING_OPTIONAL_PY),),
                attempt_index=int(context.get("attempt_index", 0)),
                elapsed_seconds=0.001,
            )

    builder = _NameErrorBuilder()
    deps = _make_deps(builder)
    product, task = _make_specs_for_controller()

    outcome = MinimalFactoryController.run(product, task, deps)
    assert outcome.final_status.value == "failed"
    assert outcome.attempts == 2
    assert len(builder.captured) == 2

    ctx0, ctx1 = builder.captured
    assert "Previous attempt failure" not in ctx0["context_body"]
    assert "## Previous attempt failure" in ctx1["context_body"]
    assert '"category": "gate_failed"' in ctx1["context_body"]
    assert "NameError: name 'Optional' is not defined" in ctx1["context_body"]
    assert "public_test_execution" in ctx1["context_body"]
    # same task/public context; only the feedback section differs
    assert ctx0["instructions"] == ctx1["instructions"]
    assert ctx0["context_body"] != ctx1["context_body"]
    assert ctx0["context_body"].rstrip("\n") == \
        ctx1["context_body"].split("## Previous attempt failure")[0].rstrip("\n")
    for marker in ("slugify_pkg_hidden_tests", "test_hidden"):
        assert marker not in ctx1["context_body"]

    # Persisted retry_feedback_summary for attempt 1 equals the rendered feedback
    conn = sqlite3.connect(outcome.db_path)
    try:
        rows = conn.execute(
            "SELECT payload_json FROM evidence_entries WHERE entity_type='RunEvidence' ORDER BY global_sequence"
        ).fetchall()
        assert len(rows) == 2
        ev1 = domain_from_json(rows[1][0])
        persisted = dict(ev1.metadata)["retry_feedback_summary"]
        expected = bound_retry_feedback({
            "attempt": 0,
            "category": "gate_failed",
            "boundary": "public_test_execution",
            "reasons": json.loads(json.dumps(persisted))["reasons"],
            "diagnostic": persisted["diagnostic"],
        })
        assert persisted == expected  # rendered == persisted (deterministic path)
        assert "NameError: name 'Optional' is not defined" in persisted["diagnostic"]
        # and the rendered prompt embeds exactly that diagnostic
        assert json.dumps(persisted, sort_keys=True) in ctx1["context_body"]
    finally:
        conn.close()


def test_l9_controller_hidden_gate_failure_stays_opaque_in_feedback():
    # Implementation that passes all four public tests but lacks unicode
    # normalization, which the hidden suite verifies → hidden gate fails.
    import re as _re

    public_only = (
        "import re\n"
        "\n"
        "\n"
        "def slugify(text: str, *, maxlen=None, separator=\"-\") -> str:\n"
        "    if not isinstance(text, str):\n"
        "        return text\n"
        "    t = text.lower()\n"
        "    t = re.sub(r\"[^a-z0-9]+\", separator, t)\n"
        "    t = t.strip(separator)\n"
        "    if maxlen is not None and isinstance(maxlen, int) and 0 < maxlen < len(t):\n"
        "        t = t[:maxlen]\n"
        "    return t\n"
    )
    assert _re.sub(r"[^a-z0-9]+", "-", "hello world").strip("-") == "hello-world"

    @dataclass
    class _PublicOnlyBuilder(WorkerRunner):
        captured: list = field(default_factory=list)

        def run_node(self, *, task_id, station_id, worker_id, context, workspace_root, station_role="builder"):
            self.captured.append(dict(context))
            return NodeRunResult(
                worker_id=worker_id,
                task_id=task_id,
                station_id=station_id,
                patches=(PatchEdit(rel_path="slugify/__init__.py", action="write", content=public_only),),
                attempt_index=int(context.get("attempt_index", 0)),
                elapsed_seconds=0.001,
            )

    builder = _PublicOnlyBuilder()
    deps = _make_deps(builder)
    product, task = _make_specs_for_controller()

    outcome = MinimalFactoryController.run(product, task, deps)
    # hidden gate must have been the failure boundary (public passed)
    conn = sqlite3.connect(outcome.db_path)
    try:
        rows = conn.execute(
            "SELECT payload_json FROM evidence_entries WHERE entity_type='GateResult' ORDER BY global_sequence"
        ).fetchall()
        idents = [json.loads(r[0])["gate_identity"] for r in rows]
        assert "hidden_test_execution" in idents
        assert "public_test_execution" in idents
    finally:
        conn.close()

    assert outcome.escalation is True
    assert len(builder.captured) == 2
    ctx1 = builder.captured[1]
    assert "## Previous attempt failure" in ctx1["context_body"]
    assert '"boundary": "hidden_test_execution"' in ctx1["context_body"]
    assert '"reasons": ["hidden verification failed"]' in ctx1["context_body"]
    assert "diagnostic" not in ctx1["context_body"]
    for marker in ("slugify_pkg_hidden_tests", "test_hidden", "testpaths"):
        assert marker not in ctx1["context_body"].split("## Previous attempt failure")[1]

    # persisted summary is exactly the opaque form
    conn = sqlite3.connect(outcome.db_path)
    try:
        row = conn.execute(
            "SELECT payload_json FROM evidence_entries WHERE entity_type='RunEvidence' ORDER BY global_sequence"
        ).fetchall()[1][0]
        persisted = dict(domain_from_json(row).metadata)["retry_feedback_summary"]
        assert persisted == {
            "attempt": 0,
            "category": "gate_failed",
            "boundary": "hidden_test_execution",
            "reasons": ["hidden verification failed"],
        }
    finally:
        conn.close()
