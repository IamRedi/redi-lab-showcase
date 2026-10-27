"""REDI-10B stable-baseline qualification tests — offline; zero provider/network."""

from __future__ import annotations

import socket
from dataclasses import replace
from pathlib import Path

import pytest

from redi_lab.recipe_store import RecipeStore
from redi_lab.stable_baseline import (
    MANDATORY_GATES,
    STABLE_BASELINE_CRITERIA_V0,
    ProductHoldoutEvidence,
    QualificationApproval,
    QualificationLedger,
    QualificationManifest,
    RecipeRef,
    StableBaselineError,
    StableBaselineGovernor,
    StabilityWindow,
    State,
    classify_store_baseline,
    digest,
    evaluate_criteria,
    v6_qualification_audit,
)

STATION = "station_mcw_implementation_v0"
TASK_CLASS = "mcw_python_component_v1"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("REDI-10B must not use network/provider")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def manifest(tmp_path: Path) -> QualificationManifest:
    return QualificationManifest(
        qualification_id="redi10b_dry_qualification_v1",
        version=1,
        recipe=RecipeRef("synthetic_recipe", 2, digest("candidate-v6-analogue"), "SHADOW"),
        criteria=STABLE_BASELINE_CRITERIA_V0,
        rollback_target=RecipeRef("synthetic_recipe", 1, digest("baseline-v1"), "SHADOW"),
        station_id=STATION,
        task_class=TASK_CLASS,
        valid_from="2026-09-25T00:00:00Z",
        valid_until="2026-12-24T00:00:00Z",
    )


def governor(tmp_path: Path, m: QualificationManifest | None = None) -> StableBaselineGovernor:
    return StableBaselineGovernor(m or manifest(tmp_path), QualificationLedger(tmp_path / "qualification.jsonl"))


def evidence(product: str, *, verified=True, gates=None, replay=True, nonreg=True, reconciled=True) -> ProductHoldoutEvidence:
    return ProductHoldoutEvidence(
        product_id=product,
        transfer_evidence_hash=digest(product),
        verified=verified,
        gates=gates if gates is not None else {g: True for g in MANDATORY_GATES},
        replay_x2_identical=replay,
        quality_non_regression=nonreg,
        provider_calls=0,
        store_metadata_reconciled=reconciled,
    )


def sufficient_evidence() -> list[ProductHoldoutEvidence]:
    return [evidence(f"product-{n}") for n in range(1, 4)]


# ---------------------------------------------------------------------------
# criteria invariants (predeclared, versioned, cannot be weakened)
# ---------------------------------------------------------------------------


def test_criteria_versioned_and_hash_frozen() -> None:
    assert STABLE_BASELINE_CRITERIA_V0.version == 1
    assert len(STABLE_BASELINE_CRITERIA_V0.criteria_hash) == 64
    assert STABLE_BASELINE_CRITERIA_V0.preregistered_before_results is True
    with pytest.raises(StableBaselineError):
        replace(STABLE_BASELINE_CRITERIA_V0, preregistered_before_results=False)
    with pytest.raises(StableBaselineError):
        replace(STABLE_BASELINE_CRITERIA_V0, min_distinct_products=1)
    with pytest.raises(StableBaselineError):
        replace(STABLE_BASELINE_CRITERIA_V0, require_all_mandatory_gates=False)


def test_manifest_binds_distinct_rollback_and_window() -> None:
    m = manifest(Path("."))
    assert len(m.manifest_hash) == 64
    with pytest.raises(StableBaselineError, match="distinct"):
        replace(m, rollback_target=m.recipe)
    with pytest.raises(StableBaselineError, match="window"):
        replace(m, valid_until=m.valid_from)


# ---------------------------------------------------------------------------
# 9a. insufficient evidence blocks validation
# ---------------------------------------------------------------------------


def test_insufficient_evidence_blocks_validation(tmp_path) -> None:
    g = governor(tmp_path)
    weak = [evidence("only-product")]
    result = g.evaluate(weak)
    assert result["criteria_met"] is False
    assert "INDEPENDENT_PRODUCT_COVERAGE" in result["failed_criteria"]
    assert "VERIFIED_TRANSFERS" in result["failed_criteria"]
    assert g.state is State.SHADOW  # blocked, unchanged
    with pytest.raises(StableBaselineError, match="ELIGIBLE"):
        g.approve_validated(
            QualificationApproval("human", "audit", digest("x"), "VALIDATED")
        )
    blocked = g.ledger.replay()["blocked_evaluations"]
    assert blocked and blocked[-1]["no_promotion"] is True


def test_regressed_or_incomplete_products_fail_criteria(tmp_path) -> None:
    m = manifest(tmp_path)
    bundles = [
        [evidence(f"p{n}", verified=False) for n in range(1, 4)],      # not verified
        [evidence(f"p{n}", replay=False) for n in range(1, 4)],        # replay missing
        [evidence(f"p{n}", nonreg=False) for n in range(1, 4)],        # quality regression
        [evidence(f"p{n}", reconciled=False) for n in range(1, 4)],    # provenance incomplete
        [evidence(f"p{n}", gates={**{g: True for g in MANDATORY_GATES}, "private_verification": False}) for n in range(1, 4)],
        [evidence("a"), evidence("b")],                                # only two products
    ]
    for bundle in bundles:
        result = evaluate_criteria(m, bundle)
        assert result["criteria_met"] is False, result["failed_criteria"]


# ---------------------------------------------------------------------------
# 9b. sufficient evidence yields ELIGIBLE only
# ---------------------------------------------------------------------------


def test_sufficient_evidence_yields_eligible_only(tmp_path) -> None:
    g = governor(tmp_path)
    result = g.evaluate(sufficient_evidence())
    assert result["criteria_met"] is True
    assert result["failed_criteria"] == []
    assert g.state is State.ELIGIBLE_FOR_VALIDATION  # eligibility only, never auto-promotion
    assert g.ledger.replay()["state"] == State.ELIGIBLE_FOR_VALIDATION.value


# ---------------------------------------------------------------------------
# 9c. explicit approval required for VALIDATED and STABLE
# ---------------------------------------------------------------------------


def test_validated_and_stable_require_explicit_approval(tmp_path) -> None:
    g = governor(tmp_path)
    g.evaluate(sufficient_evidence())
    validated_evidence = digest({"manifest_hash": g.manifest.manifest_hash, "rung": "VALIDATED"})
    with pytest.raises(StableBaselineError, match="approval"):
        g.approve_validated(
            QualificationApproval("human", "audit-1", digest("wrong"), "VALIDATED")
        )
    g.approve_validated(QualificationApproval("owner", "audit-001", validated_evidence, "VALIDATED"))
    assert g.state is State.VALIDATED
    with pytest.raises(StableBaselineError, match="dwell"):
        g.approve_stable(
            QualificationApproval("owner", "audit-002", digest("anything"), "STABLE")
        )
    with pytest.raises(StableBaselineError, match="below the predeclared minimum"):
        g.record_stability_window(StabilityWindow(7, 0, digest("short-window")))
    with pytest.raises(StableBaselineError, match=r"regression during the dwell"):
        g.record_stability_window(StabilityWindow(14, 1, digest("regressed-window")))
    window_hash = digest("dwell-evidence")
    g.record_stability_window(StabilityWindow(14, 0, window_hash))
    with pytest.raises(StableBaselineError, match=r"approval does not cite"):
        g.approve_stable(QualificationApproval("owner", "audit-002", digest("wrong"), "STABLE"))
    g.approve_stable(QualificationApproval("owner", "audit-003", window_hash, "STABLE"))
    assert g.state is State.STABLE


# ---------------------------------------------------------------------------
# 9d. regression blocks or rolls back
# ---------------------------------------------------------------------------


def test_regression_rejects_before_validation(tmp_path) -> None:
    g = governor(tmp_path)
    g.evaluate(sufficient_evidence())
    g.reject({"regression": "private_verification gate failed in refreshed evidence"})
    assert g.state is State.VALIDATION_REJECTED
    with pytest.raises(StableBaselineError):
        g.approve_validated(
            QualificationApproval("human", "audit", digest({"manifest_hash": g.manifest.manifest_hash, "rung": "VALIDATED"}), "VALIDATED")
        )


def test_post_stable_regression_rolls_back_to_exact_target(tmp_path) -> None:
    g = governor(tmp_path)
    g.evaluate(sufficient_evidence())
    g.approve_validated(
        QualificationApproval("owner", "audit-001", digest({"manifest_hash": g.manifest.manifest_hash, "rung": "VALIDATED"}), "VALIDATED")
    )
    window_hash = digest("dwell-evidence")
    g.record_stability_window(StabilityWindow(14, 0, window_hash))
    g.approve_stable(QualificationApproval("owner", "audit-003", window_hash, "STABLE"))
    wrong_target = digest("not-the-baseline")
    with pytest.raises(StableBaselineError, match=r"exact immutable prior hash"):
        g.rollback({"regression": "field drift"}, QualificationApproval("owner", "audit-004", wrong_target, "ROLLED_BACK"))
    g.rollback(
        {"regression": "post-stable quality regression"},
        QualificationApproval("owner", "audit-005", g.manifest.rollback_target.canonical_hash, "ROLLED_BACK"),
    )
    replay = g.ledger.replay()
    assert replay["state"] == State.ROLLED_BACK
    last = g.ledger.events()[-1]["payload"]
    assert last["evidence"]["target"]["canonical_hash"] == g.manifest.rollback_target.canonical_hash


# ---------------------------------------------------------------------------
# 9e. replay x2 deterministic + tamper detection + offline/no-store-mutation
# ---------------------------------------------------------------------------


def test_replay_twice_deterministic_and_tamper_evident(tmp_path, monkeypatch) -> None:
    g = governor(tmp_path)
    g.evaluate(sufficient_evidence())
    before = g.ledger.replay()
    raw = g.ledger.path.read_bytes()
    monkeypatch.setattr("redi_lab.stable_baseline.POLICY_ID", "drifted-policy")
    one, two = g.ledger.replay(), g.ledger.replay()
    assert one == two == before
    assert g.ledger.path.read_bytes() == raw
    lines = raw.decode("utf-8").splitlines()
    tampered = __import__("json").loads(lines[0])
    tampered["payload"]["manifest"]["task_class"] = "forged"
    g.ledger.path.write_text(
        __import__("json").dumps(tampered, sort_keys=True, separators=(",", ":"))
        + "\n" + "\n".join(lines[1:]) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(StableBaselineError, match="hash chain"):
        g.ledger.replay()


def test_real_recipe_store_read_only_and_v6_not_eligible(tmp_path) -> None:
    """v6 audit against the real store: read-only, NOT eligible, exact gaps."""
    store_path = Path("build/redi08c14-owner-responses-20260922T195953Z/recipe_store.sqlite")
    if not store_path.exists():
        pytest.skip("historical recipe store not present on this machine")
    with RecipeStore(store_path) as store:
        original = store_path.read_bytes()
        baseline = classify_store_baseline(store, "recipe_cold_39553c39ceb593e2bc24775be1c420e2")
        audit = v6_qualification_audit(store)
        unchanged = store_path.read_bytes() == original
    assert unchanged  # zero store mutation
    assert baseline["classification"] == "STABLE_BASELINE_NOT_YET_ESTABLISHED"
    assert baseline["validated_versions"] == []
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
    assert audit["note"].startswith("read-only")


def test_store_baseline_never_confuses_transfer_with_stability(tmp_path) -> None:
    with RecipeStore(tmp_path / "empty.sqlite") as store:
        verdict = classify_store_baseline(store, "missing")
    assert verdict["classification"] == "STABLE_BASELINE_NOT_YET_ESTABLISHED"


def test_provenance_binding_and_dwell_lifecycle(tmp_path) -> None:
    g = governor(tmp_path)
    g.evaluate(sufficient_evidence())
    with pytest.raises(StableBaselineError, match="provenance record hash required"):
        g.bind_provenance("short", "audit")
    binding = g.bind_provenance("d" * 64, "audit-bound-to-provenance")
    assert binding["bound_at_state"] == "ELIGIBLE_FOR_VALIDATION"
    g.approve_validated(
        QualificationApproval("owner", "audit-v", digest({"manifest_hash": g.manifest.manifest_hash, "rung": "VALIDATED"}), "VALIDATED")
    )
    assert g.state is State.VALIDATED
    dwell = g.start_dwell(started_date="2026-09-25", evidence_hash=digest("dwell-start"))
    assert dwell["required_days"] == 14 and dwell["regression_events_allowed"] == 0
    events = [e["event_type"] for e in g.ledger.events()]
    assert "PROVENANCE_BOUND" in events and "DWELL_STARTED" in events
    r1, r2 = g.ledger.replay(), g.ledger.replay()
    assert r1 == r2 and r1["state"] == "VALIDATED"


def test_stable_refused_without_completed_dwell(tmp_path) -> None:
    g = governor(tmp_path)
    g.evaluate(sufficient_evidence())
    g.approve_validated(
        QualificationApproval("owner", "audit-v", digest({"manifest_hash": g.manifest.manifest_hash, "rung": "VALIDATED"}), "VALIDATED")
    )
    g.start_dwell(started_date="2026-09-25", evidence_hash=digest("dwell-start"))
    with pytest.raises(StableBaselineError, match="completed stability dwell"):
        g.approve_stable(QualificationApproval("owner", "audit-s", digest("dwell-start"), "STABLE"))
    assert g.state is State.VALIDATED
