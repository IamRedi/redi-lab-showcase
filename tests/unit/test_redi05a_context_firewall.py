"""REDI-05A — Context Manifest / Firewall foundation tests.

Deterministic, zero-network. Covers: strategy-deterministic packages,
hash determinism/sensitivity, budget fail-closed behavior, hidden/secret/
unrelated-file exclusion, upstream handoff allowlisting, prior public
failure evidence inclusion with hidden-gate rejection, exact context
reproduction, strategy binding into the accepted ContextManifest builder,
and the audit-vs-context boundary for gate evidence.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from redi_lab.context_compiler import (
    CONTEXT_STRATEGY_FULL_PUBLIC_V0,
    CONTEXT_STRATEGY_SELECTED_V0,
    DEFAULT_BUDGET_BYTES,
    CompiledContextPackage,
    ContextBudgetError,
    ContextFirewall,
    ContextFirewallError,
    compile_context,
    estimate_tokens,
    gate_evidence_context_eligible,
)
from redi_lab.domain import (
    ArtifactRef,
    HandoffManifest,
    ProductSpec,
    Requirement,
    RiskTier,
    StationContract,
    StationId,
    TaskContract,
)
from redi_lab.factory.builder_context import build_builder_context

# ---------------------------------------------------------------- fixtures


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "slugify").mkdir(parents=True)
    (ws / "tests").mkdir()
    (ws / "slugify" / "__init__.py").write_text(
        '"""Stub."""\n\ndef slugify(text, *, maxlen=None, separator="-"):\n    return text\n',
        encoding="utf-8",
    )
    (ws / "tests" / "test_slugify_public.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    (ws / "NOTES.md").write_text("unrelated repository notes", encoding="utf-8")
    (ws / "credentials.py").write_text("API_KEY = 'sk-abcdef1234567890'\n", encoding="utf-8")
    (ws / "tests" / "test_hidden_extra.py").write_text("HIDDEN = True\n", encoding="utf-8")
    return ws


_SPECS_CACHE = None


def _specs():
    """One shared contract instance set: identity hashes must be comparable
    across compilations within a test."""
    global _SPECS_CACHE
    if _SPECS_CACHE is not None:
        return _SPECS_CACHE
    product = ProductSpec(
        requested_outcome="Canonical slugify implementation task.",
        requirements=[Requirement(text="public tests pass", kind="functional")],
        constraints=["Only modify slugify/__init__.py."],
        risk_tier=RiskTier.LOW,
        unresolved_requirements=[],
    )
    task = TaskContract(goal="implement slugify", allowed_scope=["slugify", "tests"])
    station = StationContract(
        role="builder",
        required_output_kinds=["patch_edit"],
        allowed_tool_classes=[],
        applicable_task_classes=["software_patch"],
    )
    _SPECS_CACHE = (product, task, station)
    return _SPECS_CACHE


def _compile(workspace: Path, strategy=CONTEXT_STRATEGY_SELECTED_V0, selected=("slugify/__init__.py", "tests/test_slugify_public.py"), **kw):
    product, task, station = _specs()
    return compile_context(
        strategy_id=strategy, task=task, station=station, product=product,
        workspace_root=workspace, selected_paths=list(selected), **kw,
    )


# ---------------------------------------------------------------------------
# Deterministic packages per strategy
# ---------------------------------------------------------------------------


def test_05a_selected_v0_deterministic_package(workspace: Path):
    p1 = _compile(workspace)
    p2 = _compile(workspace)
    assert isinstance(p1, CompiledContextPackage)
    assert p1.package_hash == p2.package_hash
    assert p1.rendered_hash == p2.rendered_hash
    assert p1.to_evidence() == p2.to_evidence()
    # only explicitly selected files included, in selection order
    assert [f["path"] for f in p1.included_files] == ["slugify/__init__.py", "tests/test_slugify_public.py"]
    assert all(f["sha256"] for f in p1.included_files)
    # unrelated repository files excluded by construction under selected_v0
    assert all(f["path"] != "NOTES.md" for f in p1.included_files)


def test_05a_full_public_v0_deterministic_package_excludes_forbidden(workspace: Path):
    p1 = _compile(workspace, strategy=CONTEXT_STRATEGY_FULL_PUBLIC_V0, selected=())
    p2 = _compile(workspace, strategy=CONTEXT_STRATEGY_FULL_PUBLIC_V0, selected=())
    assert p1.package_hash == p2.package_hash
    included = sorted(f["path"] for f in p1.included_files)
    # all eligible PUBLIC files present…
    assert "slugify/__init__.py" in included
    assert "tests/test_slugify_public.py" in included
    # …hidden and secret files never
    assert all("hidden" not in p for p in included)
    assert all("credentials" not in p for p in included)
    excluded = {f["path"]: f["reason"] for f in p1.excluded_files}
    assert excluded["tests/test_hidden_extra.py"] == "HIDDEN_TEST_BOUNDARY"
    assert excluded["credentials.py"] == "SECRET_BLOCKLIST_PATH"


def test_05a_strategy_change_changes_hash(workspace: Path):
    sel = _compile(workspace)
    full = _compile(workspace, strategy=CONTEXT_STRATEGY_FULL_PUBLIC_V0, selected=())
    assert sel.strategy_id != full.strategy_id
    assert sel.package_hash != full.package_hash
    # the two strategies share the same contracts identity
    assert (sel.task_id, sel.station_id, sel.product_id) == (full.task_id, full.station_id, full.product_id)


def test_05a_file_change_changes_hash(tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / "slugify").mkdir(parents=True)
    (ws / "slugify" / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    p1 = _compile(ws, selected=("slugify/__init__.py",))
    (ws / "slugify" / "__init__.py").write_text("x = 2\n", encoding="utf-8")
    p2 = _compile(ws, selected=("slugify/__init__.py",))
    assert p1.package_hash != p2.package_hash
    assert p1.rendered_hash != p2.rendered_hash
    assert p1.included_files[0]["sha256"] != p2.included_files[0]["sha256"]


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def test_05a_budget_metadata_recorded(workspace: Path):
    pkg = _compile(workspace)
    assert pkg.budget_bytes == DEFAULT_BUDGET_BYTES
    assert pkg.counts["token_estimate_method"] == "CHARS_PER_4"
    assert pkg.counts["bytes"] == len(pkg.rendered_text.encode("utf-8"))
    assert pkg.counts["chars"] == len(pkg.rendered_text)
    assert pkg.counts["estimated_tokens"] == pkg.counts["chars"] // 4
    assert estimate_tokens("abcde") == 1


def test_05a_budget_overflow_fails_closed(workspace: Path):
    with pytest.raises(ContextBudgetError, match="exceeds budget"):
        _compile(workspace, budget_bytes=64)
    # no package is produced — fail closed, no silent truncation
    with pytest.raises(ContextBudgetError):
        _compile(workspace, strategy=CONTEXT_STRATEGY_FULL_PUBLIC_V0, selected=(), budget_bytes=64)


# ---------------------------------------------------------------------------
# Firewall
# ---------------------------------------------------------------------------


def test_05a_selected_v0_firewall_fail_closed(workspace: Path):
    with pytest.raises(ContextFirewallError, match="FORBIDDEN_TRAVERSAL"):
        _compile(workspace, selected=("../outside.py",))
    with pytest.raises(ContextFirewallError, match="SECRET_BLOCKLIST_PATH"):
        _compile(workspace, selected=("credentials.py",))
    with pytest.raises(ContextFirewallError, match="HIDDEN_TEST_BOUNDARY"):
        _compile(workspace, selected=("tests/test_hidden_extra.py",))
    with pytest.raises(ContextFirewallError, match="not found in workspace"):
        _compile(workspace, selected=("missing.py",))


def test_05a_secret_value_in_contract_fails_closed(workspace: Path):
    product, task, station = _specs()
    bad = ProductSpec(
        requested_outcome="call https://api.example.com with api_key = sk-abcdefghijklmnop1234",
        requirements=[], constraints=[], risk_tier=RiskTier.LOW, unresolved_requirements=[],
    )
    with pytest.raises(ContextFirewallError, match="SECRET_VALUE_DETECTED"):
        compile_context(
            strategy_id=CONTEXT_STRATEGY_SELECTED_V0, task=task, station=station, product=bad,
            workspace_root=workspace, selected_paths=["slugify/__init__.py"],
        )


# ---------------------------------------------------------------------------
# Upstream handoffs
# ---------------------------------------------------------------------------


def test_05a_upstream_handoff_included_only_when_allowlisted(workspace: Path):
    _, task, station = _specs()
    product, _, _ = _specs()
    hm = HandoffManifest(
        producer_station_id=StationId("station_upstream"),
        produced_artifacts=[ArtifactRef(ref_id="interface.py", kind="file", location="up/interface.py", sha256="ab" * 32)],
        public_interfaces=["interface.py:parse"],
    )
    base = _compile(workspace, selected=("slugify/__init__.py",))  # upstream_handoffs not allowed by default
    assert base.upstream_handoffs == ()

    fw_open = ContextFirewall(allowed_classes=frozenset({
        "task_contract", "station_contract", "product_contract", "source_files", "upstream_handoffs",
    }))
    with_h = compile_context(
        strategy_id=CONTEXT_STRATEGY_SELECTED_V0, task=task, station=station, product=product,
        workspace_root=workspace, selected_paths=["slugify/__init__.py"],
        handoff_manifests=[hm], firewall=fw_open,
    )
    assert len(with_h.upstream_handoffs) == 1
    h = with_h.upstream_handoffs[0]
    assert h["handoff_id"] == str(hm.id)
    assert h["producer_station_id"] == "station_upstream"
    assert h["artifact_refs"][0]["sha256"] == "ab" * 32
    # handoff inclusion binds into the package hash
    assert with_h.package_hash != base.package_hash
    # valid package with NO handoff remains valid
    assert base.upstream_handoffs == ()


# ---------------------------------------------------------------------------
# Prior failure evidence + audit-vs-context boundary
# ---------------------------------------------------------------------------


def test_05a_prior_public_failure_evidence_included_when_allowed(workspace: Path):
    fw_pf = ContextFirewall(allowed_classes=frozenset({
        "task_contract", "station_contract", "product_contract", "source_files", "prior_failure_evidence",
    }))
    base = _compile(workspace, selected=("slugify/__init__.py",))
    with_ev = compile_context(
        strategy_id=CONTEXT_STRATEGY_SELECTED_V0, task=_specs()[1], station=_specs()[2], product=_specs()[0],
        workspace_root=workspace, selected_paths=["slugify/__init__.py"], firewall=fw_pf,
        prior_failure_evidence=[{
            "attempt": 0, "category": "gate_failed", "boundary": "public_test_execution",
            "reasons": ["non-zero exit: 2"],
        }],
    )
    assert with_ev.prior_failure_evidence[0]["boundary"] == "public_test_execution"
    assert with_ev.package_hash != base.package_hash
    # default firewall: prior failure evidence excluded (not in default classes)
    assert base.prior_failure_evidence == ()


def test_05a_hidden_gate_evidence_never_context_eligible(workspace: Path):
    assert gate_evidence_context_eligible("public_test_execution") is True
    assert gate_evidence_context_eligible("hidden_test_execution") is False
    assert gate_evidence_context_eligible("anything_unknown") is False
    fw_pf = ContextFirewall(allowed_classes=frozenset({
        "task_contract", "station_contract", "product_contract", "source_files", "prior_failure_evidence",
    }))
    with pytest.raises(ContextFirewallError, match="not context-eligible"):
        compile_context(
            strategy_id=CONTEXT_STRATEGY_SELECTED_V0, task=_specs()[1], station=_specs()[2], product=_specs()[0],
            workspace_root=workspace, selected_paths=["slugify/__init__.py"], firewall=fw_pf,
            prior_failure_evidence=[{
                "attempt": 0, "category": "gate_failed", "boundary": "hidden_test_execution",
                "reasons": ["hidden verification failed"],
            }],
        )


def test_05a_hidden_gate_evidence_retained_in_ledger_audit_only(workspace: Path):
    """Gate evidence (incl. hidden stdout) stays in the ledger for audit, yet
    is structurally ineligible for station context."""
    from redi_lab.domain import GateResult, GateStatus, GateResultId, new_id
    from redi_lab.context_compiler import CONTEXT_CLASS_PRIOR_FAILURE_EVIDENCE

    gate = GateResult(
        id=GateResultId(new_id("gate_result")),
        gate_identity="hidden_test_execution",
        criteria=(),
        status=GateStatus.FAIL,
        failure_reasons=("non-zero exit: 1",),
        metadata={"stdout": "hidden suite output that must not become context"},
    )
    ledger_blob = json.dumps(gate.__dict__, default=str)  # audit retention shape
    assert "hidden_test_execution" in ledger_blob  # retained for audit

    fw_pf = ContextFirewall(allowed_classes=frozenset({
        "task_contract", "station_contract", "product_contract", "source_files", "prior_failure_evidence",
    }))
    with pytest.raises(ContextFirewallError):
        compile_context(
            strategy_id=CONTEXT_STRATEGY_SELECTED_V0, task=_specs()[1], station=_specs()[2], product=_specs()[0],
            workspace_root=workspace, selected_paths=["slugify/__init__.py"], firewall=fw_pf,
            prior_failure_evidence=[{
                "attempt": 0, "category": "gate_failed", "boundary": "hidden_test_execution",
                "reasons": ["hidden verification failed"],
            }],
        )


# ---------------------------------------------------------------------------
# Reproduction + strategy binding in the accepted manifest builder
# ---------------------------------------------------------------------------


def test_05a_exact_context_reproduction(tmp_path: Path):
    ws1 = tmp_path / "a"
    ws2 = tmp_path / "b"
    for ws in (ws1, ws2):
        (ws / "slugify").mkdir(parents=True)
        (ws / "slugify" / "__init__.py").write_text("x = 1\n", encoding="utf-8", newline="\n")
    p1 = _compile(ws1, selected=("slugify/__init__.py",))
    p2 = _compile(ws2, selected=("slugify/__init__.py",))
    assert p1.package_hash == p2.package_hash
    assert p1.rendered_text == p2.rendered_text
    # byte-exact reproduction from the evidence identity
    ev = p1.to_evidence()
    assert ev["rendered_hash"] == hashlib.sha256(p1.rendered_text.encode("utf-8")).hexdigest()
    assert ev["included_files"][0]["bytes"] == len("x = 1\n")


def test_05a_strategy_bound_into_accepted_manifest_builder(workspace: Path):
    product, task, station = _specs()
    m_sel = build_builder_context(
        task=task, station=station, product=product, fixture_root=workspace,
        allowed_scope=["slugify", "tests"], context_strategy_id="selected_v0",
    )
    m_full = build_builder_context(
        task=task, station=station, product=product, fixture_root=workspace,
        allowed_scope=["slugify", "tests"], context_strategy_id="full_public_v0",
    )
    assert m_sel.metadata["context_strategy_id"] == "selected_v0"
    assert m_sel.deterministic_hash != m_full.deterministic_hash  # strategy bound into hash
    # default strategy is selected_v0
    m_def = build_builder_context(
        task=task, station=station, product=product, fixture_root=workspace,
        allowed_scope=["slugify", "tests"],
    )
    assert m_def.metadata["context_strategy_id"] == "selected_v0"


def test_05a_no_provider_fields_in_context_contracts(workspace: Path):
    pkg = _compile(workspace)
    ev = pkg.to_evidence()
    blob = json.dumps(ev).lower()
    for forbidden in ("openai", "gpt-", "anthropic", "gemini", "api_key", "temperature"):
        assert forbidden not in blob
    ev2 = pkg.to_evidence()
    assert ev2 == ev  # stable evidence projection
