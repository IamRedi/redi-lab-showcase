"""REDI-10C0 bootstrap-circularity tests — offline; zero provider/network."""

from __future__ import annotations

import socket
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from redi_lab.recipe_store import RecipeStore
from redi_lab.stable_baseline import (
    BootstrapPriorState,
    MANDATORY_GATES,
    STABLE_BASELINE_CRITERIA_V0,
    STABLE_BASELINE_CRITERIA_V2,
    ProductHoldoutEvidence,
    QualificationApproval,
    QualificationLedger,
    QualificationManifest,
    RecipeRef,
    StableBaselineError,
    StableBaselineGovernor,
    StabilityWindow,
    State,
    digest,
    evaluate_criteria,
    v6_qualification_audit,
)

STATION = "station_mcw_implementation_v0"
TASK_CLASS = "mcw_python_component_v1"
V1_HASH = STABLE_BASELINE_CRITERIA_V0.criteria_hash


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("REDI-10C0 must not use network/provider")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def prior_state(*, replay=True, owner="owner") -> BootstrapPriorState:
    return BootstrapPriorState(
        state_id="pre-promotion-fallback-worker-state",
        captured_state_hash=digest("captured-operational-state"),
        restoration_procedure_hash=digest("restoration-procedure"),
        restoration_replay_x2_identical=replay,
        fallback_description="no promoted recipe; frozen fallback worker configuration",
        owner_approved_by=owner,
        owner_audit_reference="audit-bootstrap-001",
    )


def bootstrap_manifest() -> QualificationManifest:
    return QualificationManifest(
        qualification_id="redi10c0_first_baseline_v1",
        version=1,
        recipe=RecipeRef("synthetic_recipe", 2, digest("first-candidate"), "SHADOW"),
        criteria=STABLE_BASELINE_CRITERIA_V2,
        rollback_target=RecipeRef("synthetic_recipe", 1, digest("captured-operational-state-pin"), "SHADOW"),
        station_id=STATION,
        task_class=TASK_CLASS,
        valid_from="2026-09-25T00:00:00Z",
        valid_until="2026-12-24T00:00:00Z",
        first_baseline=True,
    )


def evidence(product: str) -> ProductHoldoutEvidence:
    return ProductHoldoutEvidence(
        product_id=product,
        transfer_evidence_hash=digest(product),
        verified=True,
        gates={g: True for g in MANDATORY_GATES},
        replay_x2_identical=True,
        quality_non_regression=True,
        provider_calls=0,
        store_metadata_reconciled=True,
    )


# ---------------------------------------------------------------------------
# v1 immutability + v2 minimal delta
# ---------------------------------------------------------------------------


def test_v1_criteria_untouched_and_v2_fixes_only_bootstrap() -> None:
    assert STABLE_BASELINE_CRITERIA_V0.version == 1
    assert not hasattr(STABLE_BASELINE_CRITERIA_V0, "allow_prior_operational_state_rollback")
    v1_fields = asdict(STABLE_BASELINE_CRITERIA_V0)
    v2_fields = asdict(STABLE_BASELINE_CRITERIA_V2)
    added = set(v2_fields) - set(v1_fields)
    assert added == {
        "allow_prior_operational_state_rollback",
        "require_restoration_replay_proof",
        "require_owner_approved_prior_state",
    }
    for name, value in v1_fields.items():
        if name == "version":
            continue
        if name == "justifications":
            # v2 may only ADD the bootstrap justification, never alter v1's
            assert set(value) < set(v2_fields[name])
            assert set(v2_fields[name]) - set(value) == {"bootstrap_rollback"}
            for key, text in value.items():
                assert v2_fields[name][key] == text, key
            continue
        assert v2_fields[name] == value, name
    assert STABLE_BASELINE_CRITERIA_V2.version == 2
    with pytest.raises(StableBaselineError):
        replace(STABLE_BASELINE_CRITERIA_V2, require_owner_approved_prior_state=False)
    with pytest.raises(StableBaselineError):
        replace(STABLE_BASELINE_CRITERIA_V2, allow_prior_operational_state_rollback=False)


def test_v1_hash_stable_by_construction() -> None:
    # v1 is untouched code: its hash must remain the frozen REDI-10B value
    assert len(V1_HASH) == 64
    assert STABLE_BASELINE_CRITERIA_V0.criteria_hash == digest(asdict(STABLE_BASELINE_CRITERIA_V0))


# ---------------------------------------------------------------------------
# bootstrap lifecycle: capture + restoration proof + owner approval
# ---------------------------------------------------------------------------


def test_bootstrap_flow_reaches_eligible_validated_stable(tmp_path) -> None:
    g = StableBaselineGovernor(
        bootstrap_manifest(), QualificationLedger(tmp_path / "q.jsonl"), bootstrap=prior_state()
    )
    result = g.evaluate([evidence(f"product-{n}") for n in range(1, 4)])
    assert result["criteria_met"] is True
    assert result["checks"]["BOOTSTRAP_ROLLBACK_PATH"]["met"] is True
    assert g.state is State.ELIGIBLE_FOR_VALIDATION  # eligibility only
    validated_evidence = digest({"manifest_hash": g.manifest.manifest_hash, "rung": "VALIDATED"})
    g.approve_validated(QualificationApproval("owner", "audit-001", validated_evidence, "VALIDATED"))
    window_hash = digest("dwell-evidence")
    g.record_stability_window(StabilityWindow(14, 0, window_hash))
    g.approve_stable(QualificationApproval("owner", "audit-002", window_hash, "STABLE"))
    assert g.state is State.STABLE
    replay = g.ledger.replay()
    assert replay["state"] == State.STABLE.value
    assert replay["bootstrap"]["captured_state_hash"] == prior_state().captured_state_hash


def test_bootstrap_requires_full_prior_state_proof(tmp_path) -> None:
    m = bootstrap_manifest()
    broken_cases = [
        lambda: prior_state(replay=False),                                   # no restoration proof
        lambda: replace(prior_state(), owner_approved_by=""),                # no owner approval
        lambda: replace(prior_state(), captured_state_hash="short"),         # no exact capture
        lambda: replace(prior_state(), fallback_description=""),             # no fallback description
    ]
    for build in broken_cases:
        with pytest.raises(StableBaselineError):
            build
            StableBaselineGovernor(m, QualificationLedger(tmp_path / "x.jsonl"), bootstrap=build())
    # missing prior state entirely: evaluation records the gap and stays SHADOW
    g = StableBaselineGovernor(m, QualificationLedger(tmp_path / "q.jsonl"))
    result = g.evaluate([evidence(f"product-{n}") for n in range(1, 4)])
    assert result["checks"]["BOOTSTRAP_ROLLBACK_PATH"]["met"] is False
    assert result["criteria_met"] is False
    assert g.state is State.SHADOW


def test_bootstrap_blocked_under_v1_no_silent_weakening(tmp_path) -> None:
    m = replace(bootstrap_manifest(), criteria=STABLE_BASELINE_CRITERIA_V0)
    with pytest.raises(StableBaselineError, match="not silently weakened"):
        StableBaselineGovernor(
            m, QualificationLedger(tmp_path / "q.jsonl"), bootstrap=prior_state()
        )


def test_bootstrap_rollback_restores_exact_prior_state(tmp_path) -> None:
    g = StableBaselineGovernor(
        bootstrap_manifest(), QualificationLedger(tmp_path / "q.jsonl"), bootstrap=prior_state()
    )
    g.evaluate([evidence(f"product-{n}") for n in range(1, 4)])
    g.approve_validated(
        QualificationApproval(
            "owner", "audit-001", digest({"manifest_hash": g.manifest.manifest_hash, "rung": "VALIDATED"}), "VALIDATED"
        )
    )
    window_hash = digest("dwell")
    g.record_stability_window(StabilityWindow(14, 0, window_hash))
    g.approve_stable(QualificationApproval("owner", "audit-002", window_hash, "STABLE"))
    with pytest.raises(StableBaselineError, match="captured prior-state hash"):
        g.rollback({"r": "x"}, QualificationApproval("owner", "a", digest("wrong"), "ROLLED_BACK"))
    g.rollback(
        {"regression": "post-stable quality regression"},
        QualificationApproval("owner", "audit-003", prior_state().captured_state_hash, "ROLLED_BACK"),
    )
    last = g.ledger.events()[-1]["payload"]
    assert last["evidence"]["target"]["prior_operational_state"]["captured_state_hash"] == (
        prior_state().captured_state_hash
    )
    assert g.ledger.replay()["state"] == State.ROLLED_BACK.value


def test_bootstrap_replay_x2_deterministic(tmp_path) -> None:
    g = StableBaselineGovernor(
        bootstrap_manifest(), QualificationLedger(tmp_path / "q.jsonl"), bootstrap=prior_state()
    )
    g.evaluate([evidence(f"product-{n}") for n in range(1, 4)])
    one, two = g.ledger.replay(), g.ledger.replay()
    assert one == two
    assert one["replay_dependencies"] == {
        "provider_calls": 0, "network": 0, "current_registry": False, "current_config": False,
    }


# ---------------------------------------------------------------------------
# non-bootstrap promotions keep the v1 rule under v2
# ---------------------------------------------------------------------------


def test_non_bootstrap_v2_still_requires_recipe_rollback_quality_evidence(tmp_path) -> None:
    m = replace(bootstrap_manifest(), first_baseline=False)
    g = StableBaselineGovernor(m, QualificationLedger(tmp_path / "q.jsonl"))
    result = g.evaluate([evidence(f"product-{n}") for n in range(1, 4)])
    assert result["checks"]["ROLLBACK_TARGET_QUALITY_EVIDENCE"]["met"] is False
    assert "ROLLBACK_TARGET_QUALITY_EVIDENCE" in result["failed_criteria"]
    assert g.state is State.SHADOW
    g2 = StableBaselineGovernor(
        m,
        QualificationLedger(tmp_path / "q2.jsonl"),
        rollback_target_quality_evidence_hash=digest("prior-version-holdout-verified"),
    )
    assert g2.evaluate([evidence(f"product-{n}") for n in range(1, 4)])["criteria_met"] is True


# ---------------------------------------------------------------------------
# v6 dual audit: read-only, NOT eligible under both v1 and v2
# ---------------------------------------------------------------------------


def test_v6_dual_audit_not_eligible_under_v1_and_v2() -> None:
    store_path = Path("build/redi08c14-owner-responses-20260922T195953Z/recipe_store.sqlite")
    if not store_path.exists():
        pytest.skip("historical recipe store not present on this machine")
    with RecipeStore(store_path) as store:
        original = store_path.read_bytes()
        audit = v6_qualification_audit(store)
        unchanged = store_path.read_bytes() == original
    assert unchanged
    assert audit["eligible"] is False
    assert audit["criteria_result"]["failed_criteria"] == [
        "EVIDENCE_COMPLETENESS",
        "INDEPENDENT_PRODUCT_COVERAGE",
        "MANDATORY_GATES",
        "QUALITY_NON_REGRESSION",
        "REPLAY_X2_PER_PRODUCT",
        "VERIFIED_TRANSFERS",
    ]
    assert "ROLLBACK_TARGET_QUALITY_EVIDENCE" in audit["missing_evidence"]
    v2 = audit["v2_audit"]
    assert v2["eligible"] is False
    assert v2["criteria_version"] == 2
    assert v2["failed_criteria"] == sorted(
        audit["criteria_result"]["failed_criteria"] + ["BOOTSTRAP_ROLLBACK_PATH"]
    )
    assert any("restoration replay proof" in gap for gap in v2["missing_evidence"])
